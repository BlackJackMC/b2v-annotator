import copy
import hashlib
import json
from pathlib import Path
import requests
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QImage, QPainter, QPen, QColor
from tasks.svg.document import new_annotation, compact_annotation, validate_annotation, prepare_export, write_export


class TaskConflict(RuntimeError):
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
        self.base_url = api.base_url
        self.user_id = api.user_id
        self.strategy = strategy
        self.config = config
        self.tasks = []
        self.statuses = {}
        self.pending = {}
        self.saved_versions = {}
        self.export_pending = None
        self.session = None
        self.source_image = None
        self.source_key = None

    def post(self, path, payload):
        try:
            if self.session is None:
                self.session = requests.Session()
            with self.session.post(f"{self.base_url}{path}", json=payload, timeout=(3, 20)) as response:
                if response.status_code == 409 or (response.status_code == 404 and (path.endswith("/unclaim") or path.count("/") == 2)):
                    raise TaskConflict("This region is locked by another annotator or its status has changed.")
                response.raise_for_status()
                return response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(f"Could not sync tracing with the server: {error}") from error

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

    def check_status(self, task_ids, include_results=True):
        result = {}
        for offset in range(0, len(task_ids), 200):
            data = self.post("/tasks", {"task_ids": task_ids[offset:offset + 200]})
            for task in data["tasks"]:
                if not include_results:
                    task.pop("result", None)
                result[task["task_id"]] = task
        return result

    def refresh(self, scan=False):
        if scan:
            self.tasks = self.strategy.scan_local_folder(self.config)
        self.statuses = self.check_status([task["task_id"] for task in self.tasks], include_results=False)
        return {"tasks": self.tasks, "statuses": self.statuses, "errors": list(self.strategy.errors)}

    def claim(self, task, reopen=False):
        action = "reclaim" if reopen else "claim"
        result = self.post(f"/tasks/{task['task_id']}/{action}", {"user_id": self.user_id, "task_type": "svg"})
        self.statuses[task["task_id"]] = {"status": "processing", "locked_by": self.user_id, "has_result": bool(result.get("existing_result"))}
        return result

    def release(self, task):
        if task:
            status = self.statuses.get(task["task_id"], {})
            if status.get("status") == "done" or status.get("locked_by") not in (None, self.user_id):
                self.saved_versions.pop(task["task_id"], None)
                return
            result = self.post(f"/tasks/{task['task_id']}/unclaim", {"user_id": self.user_id})
            previous = self.statuses.pop(task["task_id"], {})
            self.saved_versions.pop(task["task_id"], None)
            if previous.get("has_result") or "reverted to done" in result.get("message", ""):
                self.statuses[task["task_id"]] = {"status": "done", "locked_by": None}

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
        result = self.claim(task, reopen)
        previous = result.get("existing_result")
        try:
            if previous:
                previous = compact_annotation(previous, task, self.user_id)
                validate_annotation(previous, task)
        except (ValueError, TypeError, KeyError):
            self.release(task)
            raise
        cached = self.pending.get(task["task_id"])
        version = self.document_version(previous)
        if cached and (cached["base"] != version or use_local is False):
            if use_local is None:
                self.release(task)
                raise LocalEditsConflict(task)
            if not use_local:
                self.pending.pop(task["task_id"], None)
                cached = None
            else:
                cached["base"] = version
        document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
        warning = ""
        if cached:
            document = copy.deepcopy(cached["document"])
            warning = "Restored unsaved edits from this session."
        self.saved_versions[task["task_id"]] = version
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
            status = self.statuses.get(task["task_id"], {})
            if status.get("status") == "done":
                continue
            if status.get("status") == "processing" and status.get("locked_by") != self.user_id:
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

    def submit(self, task, document):
        attempt = self.export_pending
        if attempt is not None and attempt["task_id"] != task["task_id"]:
            raise RuntimeError("Finish the previous save before saving another region.")
        if attempt is None:
            self.verify_source(task)
            result = compact_annotation(document, task, self.user_id)
            contents = prepare_export(task, result)
            attempt = {"task_id": task["task_id"], "document": result,
                       "contents": contents, "submitted": False}
        result = attempt["document"]
        if not attempt["submitted"]:
            status = self.check_status([task["task_id"]]).get(task["task_id"], {})
            if status.get("status") == "done":
                if status.get("result") != result:
                    self.export_pending = None
                    self.remember(task, result)
                    self.statuses[task["task_id"]] = {"status": "done", "locked_by": None}
                    raise LocalEditsConflict(task)
                attempt["submitted"] = True
                self.export_pending = attempt
            else:
                try:
                    self.claim(task)
                except TaskConflict:
                    self.export_pending = None
                    raise
                self.export_pending = attempt
                try:
                    self.post(f"/tasks/{task['task_id']}", {"user_id": self.user_id, "result": result})
                    attempt["submitted"] = True
                except RuntimeError as error:
                    if isinstance(error, TaskConflict):
                        self.export_pending = None
                    status = self.check_status([task["task_id"]]).get(task["task_id"], {})
                    if status.get("status") == "done" and status.get("result") == result:
                        attempt["submitted"] = True
                        self.export_pending = attempt
                    elif status.get("status") == "done":
                        self.export_pending = None
                        self.remember(task, result)
                        self.statuses[task["task_id"]] = {"status": "done", "locked_by": None}
                        raise LocalEditsConflict(task)
                    else:
                        raise
        write_export(task, result, contents=attempt["contents"])
        self.export_pending = None
        self.pending.pop(task["task_id"], None)
        self.saved_versions.pop(task["task_id"], None)
        self.statuses[task["task_id"]] = {"status": "done", "locked_by": None}
        return result
