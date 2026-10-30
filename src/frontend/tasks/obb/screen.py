import os
from typing import Dict, List
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QPushButton, QMessageBox, QListWidgetItem, QLabel
from PyQt6.QtCore import Qt, QTimer
from core.data_manager import TaskSyncWorker
from core.api_client import APIClient
from tasks.obb.canvas import ImageCanvas


class OBBTaskScreen(QWidget):
    def __init__(self, api_client: APIClient, data_manager, on_back_callback):
        super().__init__()
        self.api = api_client
        self.data_manager = data_manager
        self.on_back_callback = on_back_callback
        self.current_task = None
        self.task_type = "obb"
        self.sync_worker = None
        self.prompting_conflict = False

        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_data_timer)

        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)

        self.canvas = ImageCanvas()
        layout.addWidget(self.canvas, stretch=4)

        right_panel = QVBoxLayout()

        btn_back = QPushButton("Back to Menu")
        btn_back.clicked.connect(self.stop_and_back)
        right_panel.addWidget(btn_back)

        btn_refresh = QPushButton("Refresh Status")
        btn_refresh.clicked.connect(self.manual_refresh)
        right_panel.addWidget(btn_refresh)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.canvas.message.connect(self.status_label.setText)
        right_panel.addWidget(self.status_label)

        lbl_processing = QLabel("Processing Tasks:")
        right_panel.addWidget(lbl_processing)

        self.processing_list = QListWidget()
        self.processing_list.itemClicked.connect(self.on_processing_clicked)
        right_panel.addWidget(self.processing_list)

        lbl_history = QLabel("Done Tasks:")
        right_panel.addWidget(lbl_history)

        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self.on_history_clicked)
        right_panel.addWidget(self.history_list)

        layout.addLayout(right_panel, stretch=1)

    def stop_and_back(self):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        self.sync_timer.stop()
        self.data_manager.release_task(self.task_type, self.current_task)
        self.current_task = None
        self.on_back_callback()

    def start_task(self):
        self.manual_refresh()
        self.sync_timer.start(10000)

    def update_sync_status(self, extra: str = ""):
        pending = self.data_manager.pending_count(self.task_type)
        if self.api.online:
            text = f"Online · {pending} pending sync" if pending else "Online"
        else:
            text = f"Offline (local-first) · {pending} pending sync"
        if extra:
            text = f"{text}\n{extra}"
        self.status_label.setText(text)

    def manual_refresh(self):
        self.start_sync(scan=True)

    def sync_data_timer(self):
        self.start_sync()

    def start_sync(self, scan=False):
        if self.sync_worker is not None or self.prompting_conflict:
            return
        worker = TaskSyncWorker(self.data_manager, self.task_type, self.current_task, scan, self)
        self.sync_worker = worker
        window = self.window()
        window.installEventFilter(worker)

        def finished():
            window.removeEventFilter(worker)
            self.sync_worker = None
            if worker.error:
                self.status_label.setText(worker.error)
            else:
                try:
                    if worker.task is None:
                        self.set_current_task(worker.next_task)
                    self.sync_data()
                    self.handle_conflicts(worker.conflicts)
                except (OSError, ValueError) as error:
                    self.status_label.setText(str(error))
            worker.deleteLater()

        worker.finished.connect(finished)
        worker.start()

    def sync_data(self):
        self.history_list.clear()
        done_tasks = self.data_manager.get_done_tasks(self.task_type, refresh=False)
        for task in done_tasks:
            label = f"[pending] {task['task_id']}" if task.get("pending_sync") else task["task_id"]
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.history_list.addItem(item)

        self.processing_list.clear()
        processing_tasks = self.data_manager.get_processing_tasks(self.task_type, refresh=False)
        for task in processing_tasks:
            label = f"[expired] {task['task_id']}" if task.get("expired") else task["task_id"]
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.processing_list.addItem(item)

        self.update_sync_status()

    def handle_conflicts(self, conflicts: List[Dict]):
        if not conflicts or self.prompting_conflict:
            return
        self.prompting_conflict = True
        try:
            for conflict in conflicts:
                ctype = conflict.get("type")
                tid = conflict.get("task_id")
                if ctype in ("locked", "locked_active"):
                    owner = conflict.get("locked_by") or "another user"
                    self.update_sync_status(f"Task {tid} is currently locked by {owner}.")
                    continue
                if ctype in ("done", "done_active"):
                    box = QMessageBox(self)
                    box.setWindowTitle("Saved result changed")
                    box.setText(f"Task {tid} has a newer server result. Choose which version to keep.")
                    local_btn = box.addButton("Keep local edits", QMessageBox.ButtonRole.AcceptRole)
                    server_btn = box.addButton("Use server result", QMessageBox.ButtonRole.DestructiveRole)
                    box.addButton(QMessageBox.StandardButton.Cancel)
                    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked is local_btn:
                        if ctype == "done_active" and self.current_task and self.current_task["task_id"] == tid:
                            self.api.reclaim_task(tid, self.task_type)
                            self.current_task["existing_result"] = conflict.get("server_result")
                        else:
                            self.data_manager.resolve_conflict(self.task_type, conflict, use_local=True)
                    elif clicked is server_btn:
                        updated = self.data_manager.resolve_conflict(self.task_type, conflict, use_local=False)
                        if self.current_task and self.current_task["task_id"] == tid and updated:
                            self.api.reclaim_task(tid, self.task_type)
                            self.set_current_task(updated)
            self.sync_data()
        finally:
            self.prompting_conflict = False

    def keyPressEvent(self, event):
        if not self.canvas.is_editable:
            return

        if event.key() == Qt.Key.Key_Escape:
            if self.current_task:
                self.set_current_task(self.current_task)

        elif event.key() == Qt.Key.Key_S and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            coords = self.canvas.get_coordinates()
            if len(coords) == 4:
                self.save_and_submit(coords)

    def save_and_submit(self, coords):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        if not self.current_task:
            return

        image_path = self.current_task['image_path']

        if not os.path.exists(image_path):
            QMessageBox.warning(self, "Error", "Local image file is missing! Action aborted.")
            self.manual_refresh()
            self.load_next_task()
            return

        try:
            conflicts = self.data_manager.save_task_local_first(self.task_type, self.current_task, coords)
        except OSError as error:
            self.status_label.setText(str(error))
            QMessageBox.warning(self, "Save failed", str(error))
            return
        self.handle_conflicts(conflicts)
        self.sync_data()
        self.load_next_task()

    def load_next_task(self):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        task = self.data_manager.get_free_task(task_type=self.task_type)
        self.set_current_task(task)
        self.update_sync_status()

    def set_current_task(self, task):
        if task:
            self.current_task = task
            self.canvas.load_image(task['image_path'])

            existing_result = task.get("existing_result")
            if existing_result and "points" in existing_result:
                self.canvas.load_existing_points(existing_result["points"])
            self.canvas.setFocus()
        else:
            self.current_task = None
            self.canvas.show_empty_state()

    def on_processing_clicked(self, item):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        task_info = item.data(Qt.ItemDataRole.UserRole)

        if not os.path.exists(task_info['image_path']):
            QMessageBox.warning(self, "Error", "Local image file missing! It may have been deleted.")
            self.manual_refresh()
            return

        opened = self.data_manager.open_task(self.task_type, task_info, reopen=False)
        if opened:
            if self.current_task and self.current_task["task_id"] != opened["task_id"]:
                self.data_manager.release_task(self.task_type, self.current_task)
            self.set_current_task(opened)
            self.sync_data()
        else:
            QMessageBox.warning(self, "Error", "Cannot lock this task.")

    def on_history_clicked(self, item):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        task_info = item.data(Qt.ItemDataRole.UserRole)

        if not os.path.exists(task_info['image_path']):
            QMessageBox.warning(self, "Error", "Local image file missing! It may have been deleted.")
            self.manual_refresh()
            return

        opened = self.data_manager.open_task(self.task_type, task_info, reopen=True)
        if opened:
            if self.current_task and self.current_task["task_id"] != opened["task_id"]:
                self.data_manager.release_task(self.task_type, self.current_task)
            self.set_current_task(opened)
            self.sync_data()
        else:
            QMessageBox.warning(self, "Error", "Cannot reclaim this task.")
