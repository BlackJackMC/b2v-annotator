import os
from typing import Dict, List
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QPushButton, QMessageBox, QListWidgetItem, QLabel
from PyQt6.QtCore import Qt, QTimer
from tasks.base.async_workflow import AsyncTaskWorkflow
from core.api_client import APIClient
from tasks.aabb.canvas import ImageCanvas


class AABBTaskScreen(AsyncTaskWorkflow, QWidget):
    def __init__(self, api_client: APIClient, data_manager, on_back_callback):
        super().__init__()
        self.api = api_client
        self.data_manager = data_manager
        self.on_back_callback = on_back_callback
        self.current_task = None
        self.task_type = "aabb"
        self.sync_worker = None
        self.prompting_conflict = False

        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_data_timer)

        self.init_ui()
        self.initialize_async_workflow()

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
        self.async_back()

    def start_task(self):
        self._last_activity = __import__("time").monotonic()
        self.sync_timer.start(60000)
        self._renew_timer.start(30000)
        self.async_refresh(scan=True)

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
        self.async_refresh(scan=True)

    def sync_data_timer(self):
        self.async_refresh(scan=False)

    def start_sync(self, scan=False):
        self.async_refresh(scan=scan)

    def sync_data(self):
        self.async_sync_data()

    def keyPressEvent(self, event):
        if not self.canvas.is_editable:
            return

        if event.key() == Qt.Key.Key_Escape:
            if self.current_task:
                self.set_current_task(self.current_task)

        elif event.key() == Qt.Key.Key_S and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            coords = self.canvas.get_coordinates()
            self.save_and_submit(coords)

    def save_and_submit(self, coords):
        if not self.current_task or not coords:
            return
        task_id = self.current_task["task_id"]
        self.async_save(coords)
        self.load_next_task(after_task_id=task_id)

    def load_next_task(self, after_task_id=None):
        self.async_next(after_task_id=after_task_id)

    def set_current_task(self, task, prepared=None):
        if task:
            self.current_task = task
            self.canvas.load_image(prepared if prepared is not None else task['image_path'], task.get('crop_coords'))

            existing_result = task.get("existing_result")
            if existing_result and "points" in existing_result:
                self.canvas.load_existing_points(existing_result["points"])
            self.canvas.setFocus()
        else:
            self.current_task = None
            self.canvas.show_empty_state()

    def on_processing_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=False)

    def on_history_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=True)

