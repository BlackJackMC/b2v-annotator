"""Shared, explicit-click task workflow for OBB/AABB/Classification.

All HTTP and disk I/O happens in two QThreads. Only UI callbacks touch Qt
widgets. The server is authoritative for ownership and Done status.
"""
import copy
import hashlib
import json
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QTimer, pyqtSlot
from PyQt6.QtGui import QImageReader
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from tasks.svg.background import BackgroundLane
from tasks.svg.document import atomic_write


class AsyncTaskWorkflow:
    def initialize_async_workflow(self):
        self._network_lane = BackgroundLane(self)
        # Claims run independently of the Save/Submit network queue.
        self._claim_lane = BackgroundLane(self)
        self._file_lane = BackgroundLane(self)
        self._callbacks = {}
        self._job_number = 0
        self._select_number = 0
        self._claimed_tasks = {}  # multiple explicitly selected locks
        self._claiming = set()
        self._saving = set()
        self._failed_saves = {}  # in-memory fallback if even the disk write fails
        self._last_activity = time.monotonic()
        self._last_list_signature = None
        self._shutting_down = False
        self._network_lane.completed.connect(self._task_job_completed)
        self._claim_lane.completed.connect(self._task_job_completed)
        self._file_lane.completed.connect(self._task_job_completed)
        self._network_lane.start()
        self._claim_lane.start()
        self._file_lane.start()
        self._renew_timer = QTimer(self)
        self._renew_timer.timeout.connect(self._renew_leases)
        self._renew_timer.start(30000)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):
        # User activity, not periodic status refresh, controls AFK lease renewal.
        if (self.isVisible() and isinstance(watched, QWidget) and
                (watched is self or self.isAncestorOf(watched)) and
                event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                                 QEvent.Type.MouseMove, QEvent.Type.TabletMove)):
            self._last_activity = time.monotonic()
        return super().eventFilter(watched, event)

    def _enqueue(self, lane, function, done, failed=None, priority=5):
        if self._shutting_down:
            return
        self._job_number += 1
        job_id = self._job_number
        self._callbacks[job_id] = (done, failed)
        worker = (self._claim_lane if lane == "claim" else
                  self._network_lane if lane == "network" else self._file_lane)
        worker.submit(job_id, function, priority=priority)

    @pyqtSlot(int, object, object)
    def _task_job_completed(self, job_id, result, error):
        callbacks = self._callbacks.pop(job_id, None)
        if not callbacks:
            return
        done, failed = callbacks
        try:
            if error is not None:
                if failed:
                    failed(error)
                else:
                    self.status_label.setText(str(error))
            elif done:
                done(result)
        except Exception as exc:
            self.status_label.setText("Background callback failed: " + str(exc))

    def _server_state(self, tid, status, owner=None, result=None):
        self.data_manager.status_cache.setdefault(self.task_type, {})[tid] = {
            "task_id": tid, "status": status, "locked_by": owner,
            "result": result,
        }
        self.sync_data()

    def async_refresh(self, scan=False):
        if getattr(self, "_refreshing", False) or self._shutting_down:
            return
        self._refreshing = True
        def fetch(_=None):
            def done(statuses):
                self._refreshing = False
                self.sync_data()
                if hasattr(self, "errors_button"):
                    errors = getattr(self.data_manager.strategies.get(self.task_type), "errors", [])
                    self.errors_button.setVisible(bool(errors))
                if hasattr(self, "update_sync_status"):
                    self.update_sync_status()
            def failed(exc):
                self._refreshing = False
                self.status_label.setText("Could not refresh task status: " + str(exc))
            self._enqueue("network", lambda: self.data_manager.refresh_statuses(self.task_type), done, failed)
        if scan:
            self._enqueue("file", lambda: self.data_manager.scan_local_folder(self.task_type), fetch,
                          lambda exc: (setattr(self, "_refreshing", False),
                                       self.status_label.setText("Scan failed: " + str(exc))))
        else:
            fetch()

    def async_sync_data(self):
        """Create list items only when scanned inputs change; update other rows in place."""
        from PyQt6.QtWidgets import QListWidgetItem
        from PyQt6.QtCore import Qt
        tasks = self.data_manager.cached_tasks.get(self.task_type, [])
        statuses = self.data_manager.status_cache.get(self.task_type, {})
        ids = tuple(task["task_id"] for task in tasks)
        if ids != getattr(self, "_last_task_ids", None):
            self.history_list.setUpdatesEnabled(False)
            self.processing_list.setUpdatesEnabled(False)
            self.history_list.clear()
            self.processing_list.clear()
            self._task_row_items = {}
            self._row_states = {}
            for task in tasks:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, task)
                self.processing_list.addItem(item)
                self._task_row_items[task["task_id"]] = item
            self._last_task_ids = ids
            self.history_list.setUpdatesEnabled(True)
            self.processing_list.setUpdatesEnabled(True)
        self.history_list.setUpdatesEnabled(False)
        self.processing_list.setUpdatesEnabled(False)
        try:
            for task in tasks:
                tid = task["task_id"]
                st = statuses.get(tid, {})
                status = st.get("status")
                if tid in self._saving:
                    label = "[saving] "
                elif tid in self._claiming:
                    label = "[claiming…] "
                elif status == "done":
                    label = "[done] "
                elif tid in self._claimed_tasks or (status == "processing" and st.get("locked_by") == self.api.user_id):
                    label = "[claimed by you] "
                elif status == "processing":
                    label = "[claimed by another annotator] "
                elif status == "expired":
                    label = "[expired] "
                elif status is None or status == "unknown":
                    label = "[unknown / offline] "
                else:
                    label = "[available] "
                text = label + task.get("label", tid)
                target = self.history_list if status == "done" and tid not in self._saving else self.processing_list
                state = (text, target is self.history_list)
                if self._row_states.get(tid) == state:
                    continue
                item = self._task_row_items[tid]
                if item.text() != text:
                    item.setText(text)
                old_list = item.listWidget()
                if old_list is not target:
                    if old_list is not None:
                        old_list.takeItem(old_list.row(item))
                    target.addItem(item)
                self._row_states[tid] = state
        finally:
            self.history_list.setUpdatesEnabled(True)
            self.processing_list.setUpdatesEnabled(True)

    def _load_image_file(self, task):
        # QImage/QImageReader are usable in a worker; QPixmap/QWidget are not.
        if self.task_type == "classify":
            strategy = self.data_manager.strategies[self.task_type]
            from pathlib import Path
            import json
            if strategy.image_info(Path(task["image_path"]))[2] != task["image_sha256"]:
                raise ValueError("The source image has changed; refresh inputs.")
            source = json.loads(Path(task["aabb_path"]).read_bytes())
            box = source["boxes"][task["region_index"]]
            if [box[k] for k in ("x", "y", "w", "h")] != task["box"]:
                raise ValueError("The source AABB has changed; refresh inputs.")
        reader = QImageReader(str(task["image_path"]))
        image = reader.read()
        if image.isNull():
            raise ValueError("Cannot decode selected image: " + reader.errorString())
        if self.task_type == "aabb" and task.get("crop_coords"):
            from PyQt6.QtCore import Qt, QPointF
            from PyQt6.QtGui import QPainter, QPolygonF, QPainterPath, QImage
            polygon = QPolygonF([QPointF(point["x"], point["y"])
                                 for point in task["crop_coords"]])
            rect = polygon.boundingRect().toAlignedRect().intersected(image.rect())
            if rect.isEmpty():
                raise ValueError("Board quadrilateral lies outside its source image")
            cropped = QImage(rect.size(), QImage.Format.Format_ARGB32_Premultiplied)
            cropped.fill(Qt.GlobalColor.transparent)
            painter = QPainter(cropped)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                path = QPainterPath()
                path.addPolygon(polygon)
                painter.setClipPath(path.translated(-rect.x(), -rect.y()))
                painter.drawImage(-rect.x(), -rect.y(), image)
            finally:
                painter.end()
            image = {"pre_cropped": True, "image": cropped,
                     "full_size": (image.width(), image.height()),
                     "offset": (rect.x(), rect.y())}
        pending_result = None
        recovery = self._pending_path(task)
        if recovery.is_file():
            try:
                pending = json.loads(recovery.read_text(encoding="utf-8"))
                if pending.get("task_id") == task["task_id"]:
                    pending_result = pending.get("result")
            except (OSError, ValueError, TypeError):
                pass
        return image, pending_result

    def confirm_unsaved_switch(self):
        if hasattr(self, "confirm_switch"):
            return self.confirm_switch()
        if self.current_task is None:
            return True
        points = self.canvas.get_coordinates()
        previous = (self.current_task.get("existing_result") or {}).get("points", [])
        if points and points != previous:
            choice = QMessageBox.question(
                self, "Unsaved annotation", "Discard unsaved changes and switch?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            return choice == QMessageBox.StandardButton.Discard
        return True

    def async_open(self, task, reopen=False, auto_next=False, skipped=None):
        if not task or self._shutting_down:
            return
        tid = task["task_id"]
        if tid in self._saving or tid in self._claiming:
            return
        if self.current_task and self.current_task["task_id"] == tid:
            return
        if not self.confirm_unsaved_switch():
            return
        self._last_activity = time.monotonic()
        self._select_number += 1
        request_number = self._select_number
        self._claiming.add(tid)
        with self.data_manager._lock:
            self.data_manager.claiming_tasks.add(tid)
        self.sync_data()
        self.status_label.setText("Claiming task on server…")
        def claimed(opened):
            # Auto Next is read-only verified before claim, then checked again
            # using the atomic claim response. The legacy backend permits
            # claiming Done, so pre-check alone is not enough.
            if auto_next and opened.get("_auto_skip"):
                self._claiming.discard(tid)
                with self.data_manager._lock:
                    self.data_manager.claiming_tasks.discard(tid)
                status = opened.get("_auto_status")
                if status == "done":
                    self._server_state(tid, "done", result=opened.get("_auto_result"))
                else:
                    self.sync_data()
                if request_number == self._select_number:
                    self.async_next(after_task_id=tid, skipped=(skipped or set()) | {tid})
                return
            self._claiming.discard(tid)
            with self.data_manager._lock:
                self.data_manager.claiming_tasks.discard(tid)
                self.data_manager.held_tasks.add(tid)
                self.data_manager.local_locks[self.task_type] = tid
            self._claimed_tasks[tid] = dict(task)
            self._server_state(tid, "processing", self.api.user_id, opened.get("existing_result"))
            if request_number != self._select_number:
                return  # Still registered on DB, but a newer click selects canvas.
            def decoded(prepared):
                image, local_pending = prepared
                if request_number != self._select_number:
                    return
                local_pending = self._failed_saves.get(tid, local_pending)
                if local_pending is not None and local_pending != opened.get("existing_result"):
                    choice = QMessageBox.question(
                        self, "Unsynced local annotation",
                        "A local snapshot was not confirmed by MongoDB. Use local changes?\n"
                        "Yes = local snapshot, No = saved server result, Cancel = keep task unopened.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                        QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel)
                    if choice == QMessageBox.StandardButton.Cancel:
                        return
                    if choice == QMessageBox.StandardButton.Yes:
                        opened["existing_result"] = local_pending
                    else:
                        self._failed_saves.pop(tid, None)
                try:
                    self.set_current_task(opened, image)
                except Exception as exc:
                    image_error(exc)
            def image_error(exc):
                self.status_label.setText("Could not open image: " + str(exc))
                self._claimed_tasks.pop(tid, None)
                self._enqueue("network", lambda: self.api.unclaim_task(tid, raise_on_error=True),
                              lambda _: self.async_refresh(), lambda _: self.async_refresh())
            self._enqueue("file", lambda: self._load_image_file(opened), decoded, image_error)
        def claim_failed(exc):
            self._claiming.discard(tid)
            with self.data_manager._lock:
                self.data_manager.claiming_tasks.discard(tid)
            self.sync_data()
            QMessageBox.information(self, "Task already selected",
                                    "Cannot claim this task. It may already belong to another annotator.\n" + str(exc))
            self.async_refresh()
        def verified_claim():
            if auto_next:
                # A successful response with no row means never claimed.
                rows = self.api.check_status([tid], raise_on_error=True, include_results=True)
                state = next((row for row in rows if row.get("task_id") == tid), None)
                if state and state.get("status") == "done":
                    return {"_auto_skip": True, "_auto_status": "done",
                            "_auto_result": state.get("result")}
                if state and state.get("result") is not None:
                    return {"_auto_skip": True, "_auto_status": state.get("status")}
                if state and state.get("status") == "processing" and state.get("locked_by") != self.api.user_id:
                    return {"_auto_skip": True, "_auto_status": "processing"}
            opened = self.data_manager.open_task(self.task_type, task, reopen=reopen)
            if auto_next and opened is not None and opened.get("existing_result") is not None:
                # Done may have been committed between GET status and claim.
                # Unclaim preserves the saved result and returns the task to Done.
                self.api.unclaim_task(tid, raise_on_error=True)
                with self.data_manager._lock:
                    self.data_manager.held_tasks.discard(tid)
                    if self.data_manager.local_locks.get(self.task_type) == tid:
                        self.data_manager.local_locks[self.task_type] = None
                return {"_auto_skip": True, "_auto_status": "done",
                        "_auto_result": opened["existing_result"]}
            return opened
        self._enqueue("claim", verified_claim,
                      lambda opened: claimed(opened) if opened is not None else claim_failed("Claim was refused"),
                      claim_failed, priority=0)

    def _pending_path(self, task):
        # Durable per-task recovery, not shared mutable service state.
        digest = hashlib.sha256(task["task_id"].encode("utf-8")).hexdigest()
        output = Path(self.data_manager.task_configs[self.task_type]["output"]).resolve()
        # Keep the user's annotation output folder free of bookkeeping files.
        return (output.parent / (".board2vec_state_" + self.task_type + "_" + output.name)
                / (digest + ".json"))

    def _submit_with_verification(self, tid, result):
        """Resolve ambiguous HTTP outcomes. Never resend an uncertain POST.

        A completed POST may lose its HTTP response. Only matching server
        Done + identical result counts as committed; otherwise keep recovery.
        """
        try:
            return self.api.submit_task(tid, result, raise_on_error=True)
        except (OfflineError, TaskConflictError) as original:
            try:
                rows = self.api.check_status([tid], raise_on_error=True,
                                             include_results=True)
            except (OfflineError, TaskConflictError):
                raise original
            match = next((row for row in rows if row.get("task_id") == tid), None)
            if match and match.get("status") == "done" and match.get("result") == result:
                return True
            raise original

    def async_save(self, points):
        if not self.current_task:
            return
        task = dict(self.current_task)
        tid = task["task_id"]
        if tid in self._saving:
            return
        result = {"points": copy.deepcopy(points)}
        self._saving.add(tid)
        with self.data_manager._lock:
            self.data_manager.active_saves.add(tid)
        self._last_activity = time.monotonic()
        # Freeze UI's current task, but another task may be opened immediately.
        self.set_current_task(None)
        self.sync_data()
        self.status_label.setText("Saving " + tid + " in background…")
        pending_path = self._pending_path(task)
        def prepare():
            pending = {"task_id": tid, "task_type": self.task_type,
                       "user_id": self.api.user_id, "result": result}
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(pending_path, json.dumps(pending, ensure_ascii=False).encode("utf-8"))
            # Recovery only: do not publish a completed output before MongoDB confirms.
            return True
        def file_ready(_):
            # Do not auto-claim/reclaim during Save: ownership was acquired on click.
            self._enqueue("network", lambda: self._submit_with_verification(tid, result),
                          remote_done, save_error, priority=1)
        def remote_done(_):
            with self.data_manager._lock:
                self.data_manager.committed_saves.add(tid)
                self.data_manager.held_tasks.discard(tid)
            self._claimed_tasks.pop(tid, None)
            with self.data_manager._lock:
                if self.data_manager.local_locks.get(self.task_type) == tid:
                    self.data_manager.local_locks.pop(self.task_type, None)
            self._failed_saves.pop(tid, None)
            self._server_state(tid, "done", None)
            self.status_label.setText("Saved to MongoDB: " + tid)
            # Acknowledged result supersedes any older legacy pending-queue entry.
            old_queue = self.data_manager.pending_sync.get(self.task_type, {})
            if tid in old_queue:
                old_queue.pop(tid, None)
                self._enqueue("file", lambda: self.data_manager.save_pending_queue(self.task_type),
                              lambda _: None, lambda err: self.status_label.setText(
                                  "Server saved; old pending-queue cleanup failed: " + str(err)))
            # Publish final JSON only AFTER MongoDB acknowledges Save.
            # On export failure, keep recovery snapshot for a disk-only retry.
            def export_local():
                self.data_manager.write_local_output(self.task_type, task, result)
                pending_path.unlink(missing_ok=True)
                return True
            def exported(_):
                with self.data_manager._lock:
                    self.data_manager.active_saves.discard(tid)
                self.status_label.setText("Saved to server and exported: " + tid)
            def export_failed(exc):
                with self.data_manager._lock:
                    self.data_manager.active_saves.discard(tid)
                self.status_label.setText(
                    "Saved to MongoDB but local export failed. Recovery retained: " + str(exc))
            self._enqueue("file", export_local, exported, export_failed, priority=1)
            self._saving.discard(tid)
            self.sync_data()
        def save_error(exc):
            with self.data_manager._lock:
                self.data_manager.active_saves.discard(tid)
            self._saving.discard(tid)
            self._failed_saves[tid] = result
            self.sync_data()
            saved_locally = pending_path.is_file()
            msg = ("An on-disk recovery snapshot was kept." if saved_locally else
                   "Disk save also failed: edits are kept in memory only. Do not close the app.")
            self.status_label.setText("Save not confirmed: " + str(exc))
            QMessageBox.warning(self, "Background save failed",
                "The server did not confirm this Save. " + msg + "\n" + str(exc) +
                "\nReopen the task and retry Save after resolving the issue.")
        self._enqueue("file", prepare, file_ready, save_error, priority=1)

    def async_next(self, after_task_id=None, skipped=None):
        if self.current_task is not None or self._claiming:
            return
        skipped = skipped or set()
        tasks = list(self.data_manager.cached_tasks.get(self.task_type, []))
        states = self.data_manager.status_cache.get(self.task_type, {})
        start = next((i for i, t in enumerate(tasks)
                      if t["task_id"] == after_task_id), -1)
        # Prefer the next item in dataset order, then wrap to earlier items.
        candidates = tasks[start + 1:] + tasks[:max(0, start)]
        for task in candidates:
            tid = task["task_id"]
            st = states.get(tid, {})
            if tid in skipped or tid in self._saving or tid in self._claiming or st.get("status") not in ("available", "expired", "processing"):
                continue
            if st.get("status") == "processing" and st.get("locked_by") != self.api.user_id:
                continue
            self.async_open(task, auto_next=True, skipped=skipped)
            return
        self.status_label.setText("No verified available task; Refresh to check new tasks.")

    def _renew_leases(self):
        if (not self.isVisible() or self._shutting_down or
                time.monotonic() - self._last_activity > 300):
            return
        ids = [tid for tid in self._claimed_tasks
               if tid not in self._saving and tid not in self._claiming]
        if not ids:
            return
        def completed(renewed_ids):
            lost = set(ids) - set(renewed_ids)
            if lost:
                for tid in lost:
                    self._claimed_tasks.pop(tid, None)
                self.status_label.setText(f"Lost or expired {len(lost)} task lock(s); refreshing status.")
                self.async_refresh()
        self._enqueue("network", lambda: self.api.renew_tasks(ids, self.task_type),
                      completed, lambda exc: self.status_label.setText("Lease renewal failed: " + str(exc)),
                      priority=2)

    def async_back(self):
        if not self.confirm_unsaved_switch():
            return
        self._select_number += 1
        self.sync_timer.stop()
        self._renew_timer.stop()
        # Release only unsaved tasks. A task being saved must keep its lock.
        for tid in list(self._claimed_tasks):
            if tid not in self._saving and tid not in self._failed_saves:
                self._claimed_tasks.pop(tid, None)
                self._enqueue("network", lambda t=tid: self.api.unclaim_task(t, raise_on_error=True),
                              lambda _: None, lambda _: None, priority=1)
        self.set_current_task(None)
        self.on_back_callback()

    def has_active_jobs(self):
        return bool(self._callbacks or self._saving)

    def shutdown_async(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self._renew_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        for lane in (self._network_lane, self._claim_lane, self._file_lane):
            lane.stop_after_jobs()
