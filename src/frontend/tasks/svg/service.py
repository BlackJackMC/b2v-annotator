import copy
import os
import shutil

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

        self.session = None

        self.source_image = None

        self.source_key = None

        self.online = True

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
                # Preserve both copies for manual recovery, never overwrite one.
                import uuid
                backup = state / (target.stem + "_legacy_" + uuid.uuid4().hex + target.suffix)
                shutil.move(str(old), str(backup))
            else:
                shutil.move(str(old), str(target))

    def load_local_drafts(self):

        path = self.draft_queue_path()

        if path is None or not path.exists():

            return

        try:

            raw = path.read_bytes()
            data = json.loads(raw)
            self._draft_payload_cache = raw

            if not isinstance(data, dict):

                raise ValueError("Invalid local draft file")

            self.pending = {key: value for key, value in data.items()

                            if isinstance(value, dict) and isinstance(value.get("document"), dict)}

            self.conflicted = {key for key, value in self.pending.items() if value.get("conflict")}

        except (OSError, ValueError) as error:

            raise OSError(f"Cannot read local drafts at {path}; preserve this file and restore it: {error}") from error

    def save_local_drafts(self):

        path = self.draft_queue_path()

        if path is None:

            raise OSError("No output folder configured for persistent local drafts")

        if self.pending:
            payload = json.dumps(self.pending, ensure_ascii=False, indent=2).encode("utf-8")
            # Avoid repeated disk writes for identical drafts.
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

        if document is not None:

            self.pending[tid] = {"base": base, "document": copy.deepcopy(document), "conflict": True}

        elif tid in self.pending:

            self.pending[tid]["conflict"] = True

        self.conflicted.add(tid)
        self.save_local_drafts()

    def use_server_result(self, tid):

        """Archive local work; never erase it silently."""

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

                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, json.dumps(self.pending_sync, ensure_ascii=False, indent=2).encode("utf-8"))

            elif path.exists():

                path.unlink()

        except OSError as error:

            raise OSError(f"Could not persist the sync queue. Keep this session open and retry Save: {error}") from error

    def post(self, path, payload):
        """Make one API request; preserve the backend's actual conflict reason."""
        try:
            if self.session is None:
                self.session = requests.Session()
            with self.session.post(
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
        """Read MongoDB ownership; do not erase previously confirmed claims on navigation.

        A missing/stale status is rechecked with the authoritative claim endpoint,
        never just painted as owned in the UI.
        """
        if scan:
            self.tasks = self.strategy.scan_local_folder(self.config)
        previously_claimed = set(self.claimed_task_ids)
        try:
            remote = self.check_status(
                [task["task_id"] for task in self.tasks], include_results=False
            )
            self.statuses = remote
            self.claimed_task_ids = {
                tid for tid, st in remote.items()
                if st.get("status") == "processing"
                and st.get("locked_by") == self.user_id
            }
            # If the server did not report a previously acknowledged lease,
            # confirm/reacquire it before removing the Claimed by you label.
            by_id = {task["task_id"]: task for task in self.tasks}
            for tid in previously_claimed - self.claimed_task_ids:
                task = by_id.get(tid)
                if task is None:
                    continue
                state = remote.get(tid, {})
                # A submitted (Done) task must remain Done, not be auto-reopened.
                if state.get("status") == "done":
                    continue
                try:
                    self.claim(task)
                except TaskConflict:
                    self.claimed_task_ids.discard(tid)
                except OfflineSyncError:
                    # Keep last known local state on temporary connectivity loss.
                    self.claimed_task_ids.add(tid)
                    self.statuses[tid] = {
                        "status": "processing", "locked_by": self.user_id,
                        "unverified": True,
                    }
                    break
        except OfflineSyncError:
            for task in self.tasks:
                tid = task["task_id"]
                if tid not in self.statuses and Path(task["output_path"]).is_file():
                    self.statuses[tid] = {"status": "done", "locked_by": None}
        return {"tasks": self.tasks, "statuses": self.statuses,
                "errors": list(self.strategy.errors)}

    def claim(self, task, reopen=False):
        """Atomically acquire any task (new, Done, expired, or already ours).

        The server is authoritative: the local Done/Available label NEVER
        determines which API endpoint to call. Reclaim is only a compatibility
        alias on the backend. A 409 means the task was not acquired.
        """
        tid = task["task_id"]
        result = self.post(
            f"/tasks/{tid}/claim",
            {"user_id": self.user_id, "task_type": "svg"},
        )
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


            status = self.statuses.get(tid, {})

            if status.get("status") == "done" or status.get("locked_by") not in (None, self.user_id):

                self.saved_versions.pop(tid, None)

                return

            try:

                result = self.post(f"/tasks/{tid}/unclaim", {"user_id": self.user_id})

                self.claimed_task_ids.discard(tid)
                previous = self.statuses.pop(tid, {})

                self.saved_versions.pop(tid, None)

                if previous.get("has_result") or "reverted to done" in result.get("message", ""):

                    self.statuses[tid] = {"status": "done", "locked_by": None}

            except (OfflineSyncError, TaskConflict):

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

        # Acquire the DB lease first; never display an editable canvas before claim.
        claim_result = self.claim(task, reopen)
        # A click must remain registered even if the local image fails to load.
        return self._load_claimed_region(task, claim_result)

    def _load_claimed_region(self, task, claim_result):
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

        previous = claim_result.get("existing_result")

        try:

            if previous:

                previous = compact_annotation(previous, task, self.user_id)

                validate_annotation(previous, task)

        except (ValueError, TypeError, KeyError):

            raise

        # Server result is authoritative whenever a task is freshly claimed.
        # Old drafts and pending-sync requests must NEVER auto-overwrite a new owner.
        stale = self.pending.pop(tid, None)
        if stale is not None:
            self.archive_local(tid, stale)
            self.save_local_drafts()
        queued = self.pending_sync.pop(tid, None)
        if queued is not None:
            self.archive_local(tid, queued)
            self.save_pending_queue()
        self.conflicted.discard(tid)
        document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
        self.saved_versions[tid] = self.document_version(previous)
        warning = ""

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

    def next_region(self, refresh=True):

        if refresh:

            self.refresh()

        for task in self.tasks:

            tid = task["task_id"]


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
        tid = task["task_id"]
        version = self.saved_versions.get(tid)
        local_version = self.document_version(document)
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

        if not self.pending_sync:

            return

        task_map = {t["task_id"]: t for t in self.tasks}
        entries = list(self.pending_sync.items())
        remote_statuses = self.check_status([tid for tid, _ in entries])

        for tid, entry in entries:

            task = task_map.get(tid, {"task_id": tid, "output_path": entry.get("output_path", "")})

            status = remote_statuses.get(tid, {})

            result = entry.get("document")

            base = entry.get("base")

            if status.get("status") == "done":

                if status.get("result") == result:

                    self.pending_sync.pop(tid, None)

                    self.save_pending_queue()

                    self.statuses[tid] = {"status": "done", "locked_by": None}

                    continue

                self.keep_conflict(tid, result, base)

                raise LocalEditsConflict(task)

            if status.get("status") == "processing" and status.get("locked_by") != self.user_id:

                continue

            # Without server-side revision CAS, an edit to an existing result

            # must never be replayed automatically after an ambiguous timeout.

            if base is not None:

                self.keep_conflict(tid, result, base)

                raise LocalEditsConflict(task)

            if status.get("result") is not None:

                self.keep_conflict(tid, result, base)

                raise LocalEditsConflict(task)

            # New, previously empty task: the server still enforces ownership.

            try:

                self.claim(task)

                self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})

            except OfflineSyncError:

                break

            except TaskConflict:

                continue

            self.pending_sync.pop(tid, None)

            self.save_pending_queue()

            self.statuses[tid] = {"status": "done", "locked_by": None}

    def sync_lock(self, current_task=None):
        """Renew ALL of our claims, not only the task currently on the canvas.

        Each renewal is checked by MongoDB; we do not overwrite another user's
        ownership. Refresh afterward to correct labels if a lease was lost.
        """
        by_id = {task["task_id"]: task for task in self.tasks}
        ids = list(self.claimed_task_ids)
        errors = []
        for tid in ids:
            task = by_id.get(tid)
            if task is None:
                continue
            try:
                self.claim(task)
            except TaskConflict as exc:
                self.claimed_task_ids.discard(tid)
                errors.append(str(exc))
            except OfflineSyncError as exc:
                errors.append(str(exc))
                break
        if errors:
            self.refresh(scan=False)
        return {"renewed": len(ids) - len(errors), "errors": errors}

    def submit(self, task, document):
        tid = task["task_id"]
        self.verify_source(task)
        result = compact_annotation(document, task, self.user_id)
        validate_annotation(result, task)
        # Server-first: backend atomically validates unexpired lock + owner.
        # NEVER queue a result for automatic replay after ownership is lost.
        try:
            self.post(f"/tasks/{tid}", {"user_id": self.user_id, "result": result})
        except (TaskConflict, OfflineSyncError):
            # Preserve draft in memory/on disk for emergency recovery, never auto-submit.
            self.remember(task, document)
            raise
        self.claimed_task_ids.discard(tid)
        self.statuses[tid] = {"status": "done", "locked_by": None, "has_result": True}
        self.saved_versions[tid] = self.document_version(result)
        self.pending.pop(tid, None)
        self.conflicted.discard(tid)
        self.save_local_drafts()
        # Server save succeeded. Local export failure must not resubmit the task.
        try:
            contents = prepare_export(task, result)
            write_export(task, result, contents=contents)
        except Exception as exc:
            self.last_export_warning = str(exc)
        else:
            self.last_export_warning = ""
        return result
