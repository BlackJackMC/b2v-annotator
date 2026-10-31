import copy
import os
import shutil
import threading
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
        self.exception = None
        self.conflict_task = None

    def run(self):
        try:
            self.result = self.function()
        except Exception as error:
            self.exception = error
            self.error = str(error)
            if isinstance(error, LocalEditsConflict):
                self.conflict_task = error.task


class TraceService:
    def __init__(self, api, strategy, config):
        self.api = api
        self._lock = threading.RLock()
        self.base_url = api.base_url
        self.user_id = api.user_id
        self.strategy = strategy
        self.config = config
        self.tasks = []
        self.statuses = {}
        # Tasks successfully claimed by this user, including those not currently open.
        self.claimed_task_ids = set()
        self.pending = {}
        self.conflicted = set()
        # Server version last observed while trying to open a region.
        self.observed_versions = {}
        self._source_validation_cache = {}
        self._preview_cache = {}
        self._draft_payload_cache = None
        self.migrate_legacy_state()
        self.load_local_drafts()
        self.pending_sync = {}
        self.saved_versions = {}
        self.export_pending = None
        self._http_local = threading.local()
        self.session = None
        self.source_image = None
        self.source_key = None
        self.active_saves = set()
        self.claiming_task_ids = set()
        self.committed_saves = set()
        self.online = True
        self.last_export_warning = ""
        self.load_pending_queue()

    def state_dir(self):
        """Private app state is a sibling of the output folder, never inside it."""
        output = self.config.get("output")
        if not output:
            raise OSError("No output folder configured")
        folder = Path(output)
        return folder.parent / (".board2vec_state_" + folder.name)

    def pending_queue_path(self):
        return self.state_dir() / "pending_sync_svg.json"

    def draft_queue_path(self):
        return self.state_dir() / "local_drafts_svg.json"

    def migrate_legacy_state(self):
        """Preserve previous drafts before cleaning hidden files from the result directory."""
        output = self.config.get("output")
        if not output:
            return
        output = Path(output)
        state = self.state_dir()
        files = {
            ".local_drafts_svg.json": state / "local_drafts_svg.json",
            ".pending_sync_svg.json": state / "pending_sync_svg.json",
        }
        archive = output / ".svg_draft_archive"
        if archive.exists():
            state.mkdir(parents=True, exist_ok=True)
            target = state / "svg_draft_archive"
            if target.exists():
                for item in archive.iterdir():
                    destination = target / item.name
                    if destination.exists():
                        raise FileExistsError(f"Backup destination already exists: {destination}")
                    shutil.move(str(item), str(destination))
                archive.rmdir()
            else:
                shutil.move(str(archive), str(target))
        for old_name, target in files.items():
            old = output / old_name
            if not old.exists():
                continue
            state.mkdir(parents=True, exist_ok=True)
            if target.exists():
                import uuid
                backup = state / (target.stem + "_legacy_" + uuid.uuid4().hex + target.suffix)
                shutil.move(str(old), str(backup))
            else:
                shutil.move(str(old), str(target))

    def load_local_drafts(self):
        with self._lock:
            path = self.draft_queue_path()
            if path is None or not path.exists():
                return
            try:
                raw = path.read_bytes()
                data = json.loads(raw)
                self._draft_payload_cache = raw
                if not isinstance(data, dict):
                    raise ValueError("Invalid local draft file")
                self.pending = {
                    key: value for key, value in data.items()
                    if isinstance(value, dict) and isinstance(value.get("document"), dict)
                }
                self.conflicted = {key for key, value in self.pending.items() if value.get("conflict")}
            except (OSError, ValueError) as error:
                raise OSError(f"Cannot read local drafts at {path}; preserve this file and restore it: {error}") from error

    def save_local_drafts(self):
        with self._lock:
            path = self.draft_queue_path()
            if path is None:
                raise OSError("No output folder configured for persistent local drafts")
            if self.pending:
                payload = json.dumps(self.pending, ensure_ascii=False, indent=2).encode("utf-8")
                if self._draft_payload_cache != payload:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, payload)
                    self._draft_payload_cache = payload
            elif path.exists():
                path.unlink()
                self._draft_payload_cache = None

    def archive_local(self, tid, entry):
        """Keep an immutable backup before dismissing a local edit or sync request."""
        import datetime
        import uuid
        path = self.draft_queue_path()
        if path is None:
            raise OSError("No output folder configured for local backups")
        folder = path.parent / "svg_draft_archive"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex}.json"
        atomic_write(folder / name, json.dumps({"task_id": tid, "entry": entry}, ensure_ascii=False, indent=2).encode("utf-8"))
        return folder / name

    def keep_conflict(self, tid, document=None, base=None):
        with self._lock:
            if document is not None:
                self.pending[tid] = {"base": base, "document": copy.deepcopy(document), "conflict": True}
            elif tid in self.pending:
                self.pending[tid]["conflict"] = True
            self.conflicted.add(tid)
            self.save_local_drafts()

    def use_server_result(self, tid):
        """Archive local work; never erase it silently."""
        with self._lock:
            if tid in self.pending:
                self.archive_local(tid, self.pending[tid])
            if tid in self.pending_sync:
                self.archive_local(tid, self.pending_sync[tid])
            self.pending.pop(tid, None)
            self.pending_sync.pop(tid, None)
            self.conflicted.discard(tid)
            self.save_local_drafts()
            self.save_pending_queue()

    def load_pending_queue(self):
        with self._lock:
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
        with self._lock:
            path = self.pending_queue_path()
            if not path:
                return
            try:
                if self.pending_sync:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, json.dumps(self.pending_sync, ensure_ascii=False, indent=2).encode("utf-8"))
                elif path.exists():
                    path.unlink()
            except OSError as error:
                raise OSError(f"Could not persist the sync queue. Keep this session open and retry Save: {error}") from error

    def post(self, path, payload):
        """Make one API request; preserve the backend's actual conflict reason."""
        try:
            # A Session must not be shared by the GUI, disk and network lanes.
            if not hasattr(self._http_local, "session"):
                self._http_local.session = requests.Session()
            with self._http_local.session.post(
                f"{self.base_url}{path}", json=payload, timeout=(3, 20)
            ) as response:
                self.online = True
                self.api.online = True
                if response.status_code in (404, 409) and path.startswith("/tasks/"):
                    try:
                        detail = response.json().get("detail")
                    except (ValueError, AttributeError, TypeError):
                        detail = None
                    raise TaskConflict(
                        str(detail or "This task has already been selected or its state changed. Please choose another task.")
                    )
                response.raise_for_status()
                return response.json()
        except TaskConflict:
            raise
        except requests.HTTPError as error:
            # A server-side 500 is not an offline connection. Keep its HTTP
            # status and URL visible so a backend traceback can be correlated.
            self.online = True
            self.api.online = True
            raise RuntimeError(f"Server error while accessing {path}: {error}") from error
        except (requests.RequestException, ValueError) as error:
            self.online = False
            self.api.online = False
            raise OfflineSyncError(
                f"Could not sync tracing with the server: {error}"
            ) from error


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
        with self._lock:
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

    def cleanup_available_json(self, task):
        """Delete only validated stale JSON; never delete pending/in-flight work."""
        tid = task["task_id"]
        with self._lock:
            if (self.statuses.get(tid, {}).get("status") != "available"
                    or tid in self.active_saves or tid in self.committed_saves
                    or tid in self.pending_sync
                    or tid in self.pending or tid in self.claimed_task_ids
                    or tid in self.claiming_task_ids
                    or self.export_pending is not None):
                return False
            output = Path(task["output_path"])
            if not output.is_file():
                return False
            try:
                raw = json.loads(output.read_bytes())
                document = compact_annotation(raw, task, self.user_id)
                validate_annotation(document, task)
            except (OSError, ValueError, TypeError, KeyError):
                return False
            output.unlink()
            return True

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
        with self._lock:
            is_pending = task["task_id"] in self.pending_sync
        if not is_pending:
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

    def _apply_pending_overlay(self):
        with self._lock:
            for tid in self.pending_sync:
                self.statuses[tid] = {
                    "status": "unknown",
                    "locked_by": None,
                    "has_result": True,
                    "status_unverified": True,
                    "pending_sync": True,
                }

    def refresh(self, scan=False):
        """Read-only status refresh; clicking a task is the only way to claim."""
        if scan:
            self.refresh_local_scan()
        with self._lock:
            snapshot = list(self.tasks)
        try:
            remote = self.check_status([t["task_id"] for t in snapshot], include_results=False)
            # The original backend does not return rows for unclaimed task IDs.
            remote = {**{t["task_id"]: {"task_id": t["task_id"], "status": "available",
                                            "locked_by": None} for t in snapshot}, **remote}
            with self._lock:
                self.statuses = remote
                # Do not erase this process's known claims from a status snapshot:
                # a concurrent /claim may have succeeded after the snapshot.
                # Renew/unclaim/submit explicitly resolve ownership instead.
            for task in snapshot:
                if remote.get(task["task_id"], {}).get("status") == "available":
                    self.cleanup_available_json(task)
        except OfflineSyncError:
            # Offline is UNKNOWN, not Done even if a previous export exists.
            # Also prevent stale cached Available entries from Auto Next.
            with self._lock:
                for task in snapshot:
                    tid = task["task_id"]
                    self.statuses[tid] = {"task_id": tid, "status": "unknown",
                                          "locked_by": None, "status_unverified": True}
        self._apply_pending_overlay()
        with self._lock:
            return {"tasks": list(self.tasks), "statuses": dict(self.statuses),
                    "errors": list(self.strategy.errors)}

    def claim(self, task, reopen=False):
        """Atomically acquire any task (new, Done, expired, or already ours)."""
        tid = task["task_id"]
        result = self.post(
            f"/tasks/{tid}/claim",
            {"user_id": self.user_id, "task_type": "svg"},
        )
        with self._lock:
            self.claimed_task_ids.add(tid)
            self.statuses[tid] = {
                "status": "processing",
                "locked_by": self.user_id,
                "has_result": result.get("existing_result") is not None,
                "pending_sync": tid in self.pending_sync,
            }
        return result

    def release(self, task):
        if task:
            tid = task["task_id"]
            with self._lock:
                if tid in self.pending_sync:
                    self.statuses[tid] = {
                        "status": "done",
                        "locked_by": None,
                        "has_result": True,
                        "pending_sync": True,
                    }
                    return
                status = dict(self.statuses.get(tid, {}))
                if status.get("status") == "done" or status.get("locked_by") not in (None, self.user_id):
                    self.saved_versions.pop(tid, None)
                    return
            try:
                result = self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                with self._lock:
                    self.claimed_task_ids.discard(tid)
                    previous = self.statuses.pop(tid, {})
                    self.saved_versions.pop(tid, None)
                    if previous.get("has_result") or "reverted to done" in result.get("message", ""):
                        self.statuses[tid] = {"status": "done", "locked_by": None}
            except (OfflineSyncError, TaskConflict):
                with self._lock:
                    self.claimed_task_ids.discard(tid)
                    previous = self.statuses.pop(tid, {})
                    if previous.get("has_result") or Path(task["output_path"]).is_file():
                        self.statuses[tid] = {"status": "done", "locked_by": None}

    def verify_source(self, task):
        """Hash each unchanged source image once, not on each task switch/save."""
        image = Path(task["image_path"])
        meta = image.stat()
        signature = (str(image), meta.st_size, meta.st_mtime_ns)
        info = self._source_validation_cache.get(signature)
        if info is None:
            info = self.strategy.image_info(image)
            if len(self._source_validation_cache) >= 32:
                self._source_validation_cache.clear()
            self._source_validation_cache[signature] = info
        if info["sha256"] != task["source"]["image_sha256"]:
            raise ValueError("The source image changed. Refresh regions before tracing.")
        aabb = Path(task["aabb_path"])
        meta = aabb.stat()
        aabb_signature = (str(aabb), meta.st_size, meta.st_mtime_ns)
        data = self._source_validation_cache.get(aabb_signature)
        if data is None:
            data = json.loads(aabb.read_bytes())
            self._source_validation_cache[aabb_signature] = data
        index = task["source"]["region_index"]
        if index >= len(data["boxes"]) or [float(data["boxes"][index][k]) for k in ("x", "y", "w", "h")] != task["source"]["region_xywh"]:
            raise ValueError("The AABB region changed. Refresh regions before tracing.")

    def load_region(self, task, reopen=False, use_local=None):
        if self.export_pending is not None:
            raise RuntimeError("Finish the pending save before opening another region.")
        try:
            claim_result = self.claim(task, reopen)
        except OfflineSyncError:
            claim_result = {"existing_result": self.local_document(task), "offline": True}
            with self._lock:
                self.claimed_task_ids.add(task["task_id"])
                self.statuses[task["task_id"]] = {
                    "status": "processing",
                    "locked_by": self.user_id,
                    "pending_sync": task["task_id"] in self.pending_sync,
                }
        return self._load_claimed_region(task, claim_result, use_local=use_local)

    def _load_claimed_region(self, task, claim_result, use_local=None):
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
        offline_warning = "Offline mode: changes will be saved locally and synced when reconnected." if claim_result.get("offline") else ""
        previous = claim_result.get("existing_result")
        if previous:
            previous = compact_annotation(previous, task, self.user_id)
            validate_annotation(previous, task)

        with self._lock:
            if tid in self.pending_sync:
                queued = self.pending_sync.get(tid, {})
                queued_doc = queued.get("document")
                queued_base = queued.get("base")
                if use_local is False:
                    self.use_server_result(tid)
                    if previous:
                        write_export(task, previous)
                elif use_local is True and queued_doc:
                    self.pending[tid] = {"base": self.document_version(previous), "document": copy.deepcopy(queued_doc)}
                    self.pending_sync.pop(tid, None)
                    self.conflicted.discard(tid)
                    self.save_pending_queue()
                    self.save_local_drafts()
                elif queued_doc:
                    if not claim_result.get("offline") and previous is not None and self.document_version(previous) != queued_base and previous != queued_doc:
                        raise LocalEditsConflict(task)
                    previous = copy.deepcopy(queued_doc)

            cached = self.pending.get(tid)
            version = self.document_version(previous)
            if cached and (cached.get("base") != version or use_local is False or cached.get("conflict")):
                if use_local is None:
                    raise LocalEditsConflict(task)
                if not use_local:
                    self.use_server_result(tid)
                    cached = None
                    if previous:
                        write_export(task, previous)
                else:
                    cached["base"] = version
                    cached.pop("conflict", None)
                    self.conflicted.discard(tid)
                    self.save_local_drafts()

            document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
            warning = offline_warning
            if cached and isinstance(cached.get("document"), dict):
                document = copy.deepcopy(cached["document"])
                warning = "Restored unsaved edits from this session." + (f" {offline_warning}" if offline_warning else "")
            self.saved_versions[tid] = version

        preview_key = (self.source_key, x, y, width, height)
        preview = self._preview_cache.get(preview_key)
        if preview is None:
            preview = original.scaledToWidth(240)
            painter = QPainter(preview)
            painter.setPen(QPen(QColor("#ff28b5"), 2))
            scale = preview.width() / original.width()
            painter.drawRect(round(x * scale), round(y * scale), round(width * scale), round(height * scale))
            painter.end()
            if len(self._preview_cache) >= 24:
                self._preview_cache.clear()
            self._preview_cache[preview_key] = preview

        return {"task": task, "document": document, "image": crop, "preview": preview, "warning": warning}

    def load_region_local(self, task):
        """Load a region locally onto the canvas immediately without blocking on network."""
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

        with self._lock:
            previous = self.local_document(task)
            version = self.document_version(previous)
            cached = self.pending.get(tid)
            document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
            warning = "" if self.online else "Offline mode: changes will be saved locally and synced when reconnected."
            if cached and isinstance(cached.get("document"), dict):
                document = copy.deepcopy(cached["document"])
                warning = "Restored unsaved edits from this session." + (f" {warning}" if warning else "")
            if tid not in self.saved_versions:
                self.saved_versions[tid] = version
            self.claimed_task_ids.add(tid)
            self.statuses[tid] = {
                "status": "processing",
                "locked_by": self.user_id,
                "has_result": previous is not None,
                "pending_sync": tid in self.pending_sync,
            }

        preview_key = (self.source_key, x, y, width, height)
        preview = self._preview_cache.get(preview_key)
        if preview is None:
            preview = original.scaledToWidth(240)
            painter = QPainter(preview)
            painter.setPen(QPen(QColor("#ff28b5"), 2))
            scale = preview.width() / original.width()
            painter.drawRect(round(x * scale), round(y * scale), round(width * scale), round(height * scale))
            painter.end()
            if len(self._preview_cache) >= 24:
                self._preview_cache.clear()
            self._preview_cache[preview_key] = preview

        return {"task": task, "document": document, "image": crop, "preview": preview, "warning": warning}

    def next_region_local(self):
        """Select and load the next available region from local state without network calls."""
        with self._lock:
            tasks = list(self.tasks)
            statuses = dict(self.statuses)
            pending_sync_ids = set(self.pending_sync.keys())

        for task in tasks:
            tid = task["task_id"]
            if tid in pending_sync_ids:
                continue
            status = statuses.get(tid, {})
            if status.get("status") == "done":
                continue
            if status.get("status") == "processing" and status.get("locked_by") not in (None, self.user_id):
                continue
            if not self.online and Path(task["output_path"]).is_file():
                with self._lock:
                    self.statuses[tid] = {"status": "done", "locked_by": None}
                continue
            try:
                return self.load_region_local(task)
            except (OSError, ValueError):
                continue
        return None

    def next_region(self, refresh=True):
        if refresh:
            self.refresh()
        with self._lock:
            tasks = list(self.tasks)
        for task in tasks:
            tid = task["task_id"]
            with self._lock:
                if tid in self.pending_sync:
                    continue
                status = dict(self.statuses.get(tid, {}))
            if status.get("status") == "done":
                continue
            if status.get("status") == "processing" and status.get("locked_by") != self.user_id:
                continue
            if not self.online and Path(task["output_path"]).is_file():
                with self._lock:
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
        tid = task["task_id"]
        local_version = self.document_version(document)
        with self._lock:
            version = self.saved_versions.get(tid)
            if (tid not in self.conflicted and tid not in self.pending_sync
                    and (version == local_version or (version is None and not document["strokes"]))):
                if tid in self.pending:
                    self.pending.pop(tid, None)
                    self.save_local_drafts()
                return

            old = self.pending.get(tid, {})
            new_entry = {
                "base": old.get("base", version),
                "document": copy.deepcopy(document),
                "conflict": old.get("conflict", False) or tid in self.conflicted,
            }
            if new_entry != old:
                self.pending[tid] = new_entry
                self.save_local_drafts()

    def flush_pending_sync(self):
        with self._lock:
            if not self.pending_sync:
                return
            task_map = {t["task_id"]: t for t in self.tasks}
            entries = [(tid, copy.deepcopy(entry)) for tid, entry in self.pending_sync.items()]

        remote_statuses = self.check_status([tid for tid, _ in entries])
        changed = False
        conflict_task = None

        def mark_synced(tid_key, synced_doc):
            nonlocal changed
            with self._lock:
                current = self.pending_sync.get(tid_key)
                if current and current.get("document") == synced_doc:
                    self.pending_sync.pop(tid_key, None)
                    changed = True
                self.claimed_task_ids.discard(tid_key)
                self.statuses[tid_key] = {
                    "status": "done",
                    "locked_by": None,
                    "has_result": True,
                    "pending_sync": tid_key in self.pending_sync,
                }

        for tid, entry in entries:
            with self._lock:
                latest = self.pending_sync.get(tid)
            if not latest:
                continue
            entry = copy.deepcopy(latest)
            task = task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path", "")})
            status = remote_statuses.get(tid, {})
            result = entry.get("document")
            base = entry.get("base")
            server_status = status.get("status")
            server_result = status.get("result")

            if server_status == "done":
                if server_result == result:
                    mark_synced(tid, result)
                    continue
                if base is not None and self.document_version(server_result) == base:
                    try:
                        claimed = self.claim(task, reopen=True)
                        if self.document_version(claimed.get("existing_result")) != base:
                            self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                            raise TaskConflict("The server result changed while acquiring the task.")
                        self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                        mark_synced(tid, result)
                        continue
                    except OfflineSyncError:
                        break
                    except TaskConflict:
                        pass
                with self._lock:
                    self.keep_conflict(tid, result, base)
                    self.statuses[tid] = {"status": "done", "locked_by": None, "has_result": True, "pending_sync": True}
                conflict_task = task
                continue

            if server_status == "processing" and status.get("locked_by") not in (None, self.user_id):
                continue

            if server_result is not None and self.document_version(server_result) != base and server_result != result:
                with self._lock:
                    self.keep_conflict(tid, result, base)
                    self.statuses[tid] = {"status": "done", "locked_by": None, "has_result": True, "pending_sync": True}
                conflict_task = task
                continue

            try:
                if server_status == "processing" and status.get("locked_by") == self.user_id:
                    try:
                        self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                    except TaskConflict:
                        claimed = self.claim(task)
                        if claimed.get("existing_result") is not None and self.document_version(claimed.get("existing_result")) != base and claimed.get("existing_result") != result:
                            self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                            raise TaskConflict("The server result changed while acquiring the task.")
                        self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                else:
                    claimed = self.claim(task)
                    if claimed.get("existing_result") is not None and self.document_version(claimed.get("existing_result")) != base and claimed.get("existing_result") != result:
                        self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})
                        with self._lock:
                            self.keep_conflict(tid, result, base)
                            self.statuses[tid] = {"status": "done", "locked_by": None, "has_result": True, "pending_sync": True}
                        conflict_task = task
                        continue
                    self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
                mark_synced(tid, result)
            except OfflineSyncError:
                break
            except TaskConflict:
                try:
                    recheck = self.check_status([tid]).get(tid, {})
                except OfflineSyncError:
                    break
                if recheck.get("status") == "done":
                    if recheck.get("result") == result:
                        mark_synced(tid, result)
                    else:
                        with self._lock:
                            self.keep_conflict(tid, result, base)
                            self.statuses[tid] = {"status": "done", "locked_by": None, "has_result": True, "pending_sync": True}
                        conflict_task = task

        if changed:
            with self._lock:
                self.save_pending_queue()
        if conflict_task is not None:
            raise LocalEditsConflict(conflict_task)

    def sync_lock(self, current_task=None):
        """Flush pending saves and renew/acquire all of our claims."""
        conflict_error = None
        try:
            self.flush_pending_sync()
        except LocalEditsConflict as exc:
            conflict_error = exc
        except OfflineSyncError as exc:
            return {"renewed": 0, "errors": [str(exc)], "active_locked": None}

        with self._lock:
            by_id = {task["task_id"]: task for task in self.tasks}
            ids = list(self.claimed_task_ids)
            if current_task and current_task["task_id"] not in self.pending_sync and current_task["task_id"] not in ids:
                ids.append(current_task["task_id"])

        errors = []
        active_locked = None
        for tid in ids:
            with self._lock:
                if tid in self.pending_sync:
                    continue
            task = by_id.get(tid) or (current_task if current_task and current_task["task_id"] == tid else None)
            if task is None:
                continue
            try:
                claim_res = self.claim(task)
                if current_task and tid == current_task["task_id"]:
                    existing = claim_res.get("existing_result")
                    if existing is not None:
                        server_ver = self.document_version(existing)
                        with self._lock:
                            local_base = self.saved_versions.get(tid)
                        if server_ver != local_base and conflict_error is None:
                            conflict_error = LocalEditsConflict(current_task)
            except TaskConflict as exc:
                with self._lock:
                    self.claimed_task_ids.discard(tid)
                errors.append(str(exc))
                if current_task and tid == current_task["task_id"]:
                    active_locked = str(exc)
            except OfflineSyncError as exc:
                errors.append(str(exc))
                break

        if errors:
            try:
                self.refresh(scan=False)
            except OfflineSyncError:
                pass
        if conflict_error is not None:
            raise conflict_error
        return {"renewed": len(ids) - len(errors), "errors": errors, "active_locked": active_locked}

    def save_local_first(self, task, document):
        """Write local JSON/SVG export and enqueue task for background server sync."""
        tid = task["task_id"]
        self.verify_source(task)
        result = compact_annotation(document, task, self.user_id)
        validate_annotation(result, task)
        contents = prepare_export(task, result)
        write_export(task, result, contents=contents)

        with self._lock:
            base = self.saved_versions.get(tid)
            if tid in self.pending_sync and "base" in self.pending_sync[tid]:
                base = self.pending_sync[tid].get("base")
            self.pending_sync[tid] = {
                "task_id": tid,
                "document": result,
                "base": base,
                "output_path": task.get("output_path")
            }
            self.save_pending_queue()
            self.export_pending = None
            self.pending.pop(tid, None)
            self.conflicted.discard(tid)
            self.save_local_drafts()
            self.saved_versions[tid] = self.document_version(result)
            self.statuses[tid] = {
                "status": "done",
                "locked_by": None,
                "has_result": True,
                "pending_sync": True,
            }
        self.last_export_warning = ""
        return result

    def submit(self, task, document):
        result = self.save_local_first(task, document)
        try:
            self.flush_pending_sync()
        except OfflineSyncError:
            pass
        return result

    def renew_tasks(self, task_ids):
        """Renew existing locks using the unchanged backend's atomic /claim.

        No /tasks/renew route exists on the legacy server. One claim is sent
        for each task, so large outstanding selections remain a TTL risk.
        """
        renewed = []
        for tid in dict.fromkeys(task_ids):
            try:
                self.post(f"/tasks/{tid}/claim", {
                    "user_id": self.user_id, "task_type": "svg"})
                renewed.append(tid)
            except TaskConflict:
                continue
        return renewed

    def refresh_local_scan(self):
        """Disk-only scanning. Call from the file lane, never the Qt UI thread."""
        scanned = self.strategy.scan_local_folder(self.config)
        destinations = {}
        for task in scanned:
            output = task.get("output_path")
            if not output:
                continue
            key = str(Path(output).resolve()).casefold() if os.name == "nt" else str(Path(output).resolve())
            old = destinations.setdefault(key, task["task_id"])
            if old != task["task_id"]:
                raise ValueError(f"Output JSON collision: {output} belongs to {old} and {task['task_id']}")
        with self._lock:
            self.tasks = scanned
        return len(scanned)


    def prepare_submission(self, task, document):
        """CPU/disk validation only; safe in an image/file worker."""
        self.verify_source(task)
        result = compact_annotation(document, task, self.user_id)
        validate_annotation(result, task)
        return result


    def submit_remote(self, task, result):
        """Network lane ONLY: HTTP commit; no disk writes or draft mutations.

        Callers must update GUI status after acknowledgment and schedule
        export_submitted() on file lane; errors preserve their snapshot there.
        """
        tid = task["task_id"]
        try:
            self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
        except (OfflineSyncError, TaskConflict) as original:
            # An accepted commit can lose its response. Query the authoritative
            # saved payload; do not retry a possibly successful write blindly.
            try:
                remote = self.check_status([tid], include_results=True).get(tid)
            except OfflineSyncError:
                raise original
            if not (remote and remote.get("status") == "done"
                    and remote.get("result") == result):
                raise original
        return result


    def export_submitted(self, task, result):
        """FILE lane ONLY, after DB acknowledgment. Never resubmit on export failure."""
        tid = task["task_id"]
        with self._lock:
            self.saved_versions[tid] = self.document_version(result)
            self.pending.pop(tid, None)
            self.conflicted.discard(tid)
            self.save_local_drafts()
        contents = prepare_export(task, result)
        write_export(task, result, contents=contents)
        return result


    def load_claimed_region(self, task, claim_result):
        """File/image phase only. Caller handles releasing a failed lease via
        the network lane; NEVER do synchronous HTTP in this file worker.
        """
        return self._load_claimed_region(task, claim_result)


    def preserve_failed_save(self, task, document):
        """Persist an immutable snapshot after failed network commit.

        No server request here. The retry draft is kept separately until an
        authorized claim checks the server result again.
        """
        tid = task["task_id"]
        with self._lock:
            self.pending[tid] = {
                "base": self.saved_versions.get(tid),
                "document": copy.deepcopy(document),
                "conflict": False,
                "save_failed": True,
            }
            self.save_local_drafts()

