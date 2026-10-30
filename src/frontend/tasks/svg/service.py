import copy
import hashlib
import json
from pathlib import Path
import requests
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QImage, QPainter, QPen, QColor
from tasks.svg.document import (
    new_annotation, compact_annotation, validate_annotation,
    prepare_export, write_export, atomic_write
)


class TaskConflict(RuntimeError):
    pass


class OfflineSyncError(RuntimeError):
    pass


class LocalEditsConflict(TaskConflict):
    def __init__(self, task):
        super().__init__("The server result changed while this session had unsaved edits.")
        self.task = task


class TaskWorker(QThread):
    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.result = None
        self.error = None
        self.conflict_task = None

    def run(self):
        try:
            self.result = self.function()
        except Exception as error:
            self.error = str(error)
            if isinstance(error, LocalEditsConflict):
                self.conflict_task = error.task


class TraceService:
    def __init__(self, api, strategy, config):
        self.api = api
        self.base_url = api.base_url
        self.user_id = api.user_id
        self.strategy = strategy
        self.config = config
        self.tasks = []
        self.statuses = {}
        self.pending = {}
        self.pending_sync = {}
        self.saved_versions = {}
        self.export_pending = None
        self.session = None
        self.source_image = None
        self.source_key = None
        self.online = True
        self.load_pending_queue()

    def pending_queue_path(self):
        output_dir = self.config.get("output")
        return Path(output_dir) / ".pending_sync_svg.json" if output_dir else None

    def load_pending_queue(self):
        path = self.pending_queue_path()
        if not path or not path.is_file():
            self.pending_sync = {}
            return
        try:
            data = json.loads(path.read_bytes())
            self.pending_sync = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.pending_sync = {}

    def save_pending_queue(self):
        path = self.pending_queue_path()
        if not path:
            return
        try:
            if self.pending_sync:
                atomic_write(path, json.dumps(self.pending_sync, ensure_ascii=False, indent=2).encode("utf-8"))
            elif path.exists():
                path.unlink()
        except OSError as error:
            raise OSError(f"Could not persist the sync queue. Keep this session open and retry Save: {error}") from error

    def post(self, path, payload):
        try:
            if self.session is None:
                self.session = requests.Session()
            with self.session.post(f"{self.base_url}{path}", json=payload, timeout=(3, 20)) as response:
                self.online = True
                self.api.online = True
                if response.status_code == 409 or (response.status_code == 404 and (path.endswith("/unclaim") or path.count("/") == 2)):
                    raise TaskConflict("This region is locked by another annotator or its status has changed.")
                response.raise_for_status()
                return response.json()
        except TaskConflict:
            raise
        except (requests.RequestException, ValueError) as error:
            self.online = False
            self.api.online = False
            raise OfflineSyncError(f"Could not sync tracing with the server: {error}") from error

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None
        self.source_image = None
        self.source_key = None

    def document_version(self, document):
        if document is None:
            return None
        encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def local_document(self, task):
        tid = task["task_id"]
        if tid in self.pending_sync and "document" in self.pending_sync[tid]:
            return copy.deepcopy(self.pending_sync[tid]["document"])
        output = Path(task["output_path"])
        if not output.is_file():
            return None
        try:
            raw = json.loads(output.read_bytes())
            doc = compact_annotation(raw, task, self.user_id)
            validate_annotation(doc, task)
            return doc
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def check_status(self, task_ids, include_results=True):
        result = {}
        if not task_ids and not self.online:
            self.post("/tasks", {"task_ids": []})
        for offset in range(0, len(task_ids), 200):
            data = self.post("/tasks", {"task_ids": task_ids[offset:offset + 200]})
            for task in data["tasks"]:
                if not include_results:
                    task.pop("result", None)
                result[task["task_id"]] = task
        return result

    def saved_tracing(self, task):
        document = self.local_document(task)
        if task["task_id"] not in self.pending_sync:
            try:
                status = self.check_status([task["task_id"]]).get(task["task_id"], {})
                if status.get("result") is not None:
                    document = compact_annotation(status["result"], task, self.user_id)
            except OfflineSyncError:
                if document is None:
                    raise
        if document is None:
            raise ValueError("No saved tracing is available for this region.")
        validate_annotation(document, task)
        return {"task": task, "document": document}

    def refresh(self, scan=False):
        if scan:
            self.tasks = self.strategy.scan_local_folder(self.config)
        conflict = None
        try:
            self.flush_pending_sync()
        except LocalEditsConflict as error:
            conflict = error
        except OfflineSyncError:
            pass
        try:
            remote = self.check_status([task["task_id"] for task in self.tasks], include_results=False)
            self.statuses = remote
        except OfflineSyncError:
            for task in self.tasks:
                tid = task["task_id"]
                if tid not in self.statuses and Path(task["output_path"]).is_file():
                    self.statuses[tid] = {"status": "done", "locked_by": None}
        for tid in self.pending_sync:
            self.statuses[tid] = {"status": "done", "locked_by": None, "pending_sync": True}
        if conflict is not None:
            raise conflict
        return {"tasks": self.tasks, "statuses": self.statuses, "errors": list(self.strategy.errors)}

    def claim(self, task, reopen=False):
        action = "reclaim" if reopen else "claim"
        result = self.post(f"/tasks/{task['task_id']}/{action}", {"user_id": self.user_id, "task_type": "svg"})
        self.statuses[task["task_id"]] = {
            "status": "processing",
            "locked_by": self.user_id,
            "has_result": bool(result.get("existing_result")),
            "pending_sync": task["task_id"] in self.pending_sync
        }
        return result

    def release(self, task):
        if task:
            tid = task["task_id"]
            if tid in self.pending_sync:
                self.statuses[tid] = {"status": "done", "locked_by": None, "pending_sync": True}
                return
            status = self.statuses.get(tid, {})
            if status.get("status") == "done" or status.get("locked_by") not in (None, self.user_id):
                self.saved_versions.pop(tid, None)
                return
            try:
                result = self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                previous = self.statuses.pop(tid, {})
                self.saved_versions.pop(tid, None)
                if previous.get("has_result") or "reverted to done" in result.get("message", ""):
                    self.statuses[tid] = {"status": "done", "locked_by": None}
            except (OfflineSyncError, TaskConflict):
                previous = self.statuses.pop(tid, {})
                if previous.get("has_result") or Path(task["output_path"]).is_file():
                    self.statuses[tid] = {"status": "done", "locked_by": None}

    def verify_source(self, task):
        info = self.strategy.image_info(Path(task["image_path"]))
        if info["sha256"] != task["source"]["image_sha256"]:
            raise ValueError("The source image changed. Refresh regions before tracing.")
        data = json.loads(Path(task["aabb_path"]).read_bytes())
        index = task["source"]["region_index"]
        if index >= len(data["boxes"]) or [float(data["boxes"][index][k]) for k in ("x", "y", "w", "h")] != task["source"]["region_xywh"]:
            raise ValueError("The AABB region changed. Refresh regions before tracing.")

    def load_region(self, task, reopen=False, use_local=None):
        if self.export_pending is not None:
            raise RuntimeError("Finish the pending save before opening another region.")
        self.verify_source(task)
        stat = Path(task["image_path"]).stat()
        key = (task["image_path"], stat.st_size, stat.st_mtime_ns)
        if key != self.source_key:
            self.source_image = QImage(task["image_path"])
            self.source_key = key
        original = self.source_image
        if original.isNull():
            raise ValueError("Cannot decode the source image.")
        x, y, width, height = task["source"]["crop_xywh"]
        crop = original.copy(x, y, width, height)
        tid = task["task_id"]
        offline_warning = ""
        result = self.claim(task, reopen)
        previous = result.get("existing_result")

        try:
            if previous:
                previous = compact_annotation(previous, task, self.user_id)
                validate_annotation(previous, task)
        except (ValueError, TypeError, KeyError):
            self.release(task)
            raise

        if tid in self.pending_sync:
            queued_doc = self.pending_sync[tid].get("document")
            if use_local is False:
                self.pending_sync.pop(tid, None)
                self.save_pending_queue()
                if previous:
                    write_export(task, previous)
            elif use_local is True and self.online and queued_doc:
                self.pending[tid] = {"base": self.document_version(previous), "document": queued_doc}
                self.pending_sync.pop(tid, None)
                self.save_pending_queue()
            elif queued_doc:
                previous = queued_doc

        cached = self.pending.get(tid)
        version = self.document_version(previous)
        if cached and (cached["base"] != version or use_local is False):
            if use_local is None:
                self.release(task)
                raise LocalEditsConflict(task)
            if not use_local:
                self.pending.pop(tid, None)
                cached = None
                if previous:
                    write_export(task, previous)
            else:
                cached["base"] = version
        document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
        warning = offline_warning
        if cached:
            document = copy.deepcopy(cached["document"])
            warning = "Restored unsaved edits from this session." + (f" {offline_warning}" if offline_warning else "")
        self.saved_versions[tid] = version
        preview = original.scaledToWidth(240)
        painter = QPainter(preview)
        painter.setPen(QPen(QColor("#ff28b5"), 2))
        scale = preview.width() / original.width()
        painter.drawRect(round(x * scale), round(y * scale), round(width * scale), round(height * scale))
        painter.end()
        return {"task": task, "document": document, "image": crop, "preview": preview, "warning": warning}

    def next_region(self, refresh=True):
        if refresh:
            self.refresh()
        for task in self.tasks:
            tid = task["task_id"]
            if tid in self.pending_sync:
                continue
            status = self.statuses.get(tid, {})
            if status.get("status") == "done":
                continue
            if status.get("status") == "processing" and status.get("locked_by") != self.user_id:
                continue
            if not self.online and Path(task["output_path"]).is_file():
                self.statuses[tid] = {"status": "done", "locked_by": None}
                continue
            try:
                return self.load_region(task)
            except LocalEditsConflict:
                raise
            except TaskConflict:
                continue
        if not refresh:
            return self.next_region(refresh=True)
        return None

    def remember(self, task, document):
        version = self.saved_versions.get(task["task_id"])
        if version == self.document_version(document) or (version is None and not document["strokes"]):
            self.pending.pop(task["task_id"], None)
            return
        self.pending[task["task_id"]] = {
            "base": version,
            "document": document
        }

    def flush_pending_sync(self):
        if not self.pending_sync:
            return
        task_map = {t["task_id"]: t for t in self.tasks}
        changed = False
        conflict_task = None

        for tid in list(self.pending_sync.keys()):
            entry = self.pending_sync[tid]
            result = entry.get("document")
            base = entry.get("base")
            task = task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path", "")})
            try:
                status = self.check_status([tid]).get(tid, {})
            except OfflineSyncError:
                break

            if status.get("status") == "done":
                if status.get("result") == result:
                    self.pending_sync.pop(tid, None)
                    self.statuses[tid] = {"status": "done", "locked_by": None}
                    changed = True
                    continue
                if base is not None and self.document_version(status.get("result")) == base:
                    try:
                        claimed = self.claim(task, reopen=True)
                        if self.document_version(claimed.get("existing_result")) != base:
                            self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                            raise TaskConflict("The server result changed while acquiring the task.")
                        self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                        self.pending_sync.pop(tid, None)
                        self.statuses[tid] = {"status": "done", "locked_by": None}
                        changed = True
                        continue
                    except OfflineSyncError:
                        break
                    except TaskConflict:
                        pass
                self.pending[tid] = {"base": base, "document": result}
                self.statuses[tid] = {"status": "done", "locked_by": None, "pending_sync": True}
                conflict_task = task
                continue

            if status.get("status") == "processing" and status.get("locked_by") not in (None, self.user_id):
                continue

            try:
                self.claim(task)
                self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                self.pending_sync.pop(tid, None)
                self.statuses[tid] = {"status": "done", "locked_by": None}
                changed = True
            except OfflineSyncError:
                break
            except TaskConflict:
                try:
                    recheck = self.check_status([tid]).get(tid, {})
                except OfflineSyncError:
                    break
                if recheck.get("status") == "done":
                    if recheck.get("result") == result:
                        self.pending_sync.pop(tid, None)
                        self.statuses[tid] = {"status": "done", "locked_by": None}
                        changed = True
                    else:
                        self.pending[tid] = {"base": base, "document": result}
                        self.statuses[tid] = {"status": "done", "locked_by": None, "pending_sync": True}
                        conflict_task = task

        if changed:
            self.save_pending_queue()
        if conflict_task is not None:
            raise LocalEditsConflict(conflict_task)

    def sync_lock(self, current_task=None):
        self.flush_pending_sync()
        if current_task:
            try:
                self.claim(current_task)
            except OfflineSyncError:
                pass
            except TaskConflict:
                tid = current_task["task_id"]
                try:
                    status = self.check_status([tid]).get(tid, {})
                except OfflineSyncError:
                    return
                if status.get("status") == "processing" and status.get("locked_by") not in (None, self.user_id):
                    raise TaskConflict(f"Task {tid} is now locked by {status.get('locked_by')}. Your edits remain local; Save queues them until the task is available.")
                if status.get("status") == "done":
                    server_ver = self.document_version(status.get("result"))
                    if server_ver != self.saved_versions.get(tid):
                        raise LocalEditsConflict(current_task)

    def submit(self, task, document):
        tid = task["task_id"]
        self.verify_source(task)
        result = compact_annotation(document, task, self.user_id)
        contents = prepare_export(task, result)
        write_export(task, result, contents=contents)

        base = self.saved_versions.get(tid)
        self.pending_sync[tid] = {
            "task_id": tid,
            "document": result,
            "base": base,
            "output_path": task.get("output_path")
        }
        self.save_pending_queue()
        self.export_pending = None
        self.pending.pop(tid, None)
        self.saved_versions[tid] = self.document_version(result)
        self.statuses[tid] = {"status": "done", "locked_by": None, "pending_sync": True}

        try:
            self.flush_pending_sync()
        except OfflineSyncError:
            pass
        return result
