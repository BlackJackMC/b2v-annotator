import json
from pathlib import Path
from typing import Dict, List
import requests
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QImage, QPainter, QPen, QColor, QPixmap, QKeySequence, QShortcut
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QMessageBox
from tasks.base.async_workflow import AsyncTaskWorkflow
from core.api_client import APIClient
from tasks.classify.canvas import ImageCanvas


class ClassifyTaskScreen(AsyncTaskWorkflow, QWidget):
    def __init__(self, api_client: APIClient, data_manager, on_back_callback):
        super().__init__()
        self.api = api_client
        self.data_manager = data_manager
        self.on_back_callback = on_back_callback
        self.task_type = "classify"
        self.current_task = None
        self.selected_label = None
        self.initial_label = None
        self.export_pending = None
        self.sync_worker = None
        self.prompting_conflict = False
        self.source_key = None
        self.source_image = None
        self.source_preview = None
        self.history = []
        self.shortcuts = []
        self.close_target = None
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_data_timer)
        self.init_ui()
        self.init_shortcuts()
        self.set_current_task(None)
        self.initialize_async_workflow()

    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setSpacing(24)
        left = QVBoxLayout()
        title = QLabel("Classify content")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        left.addWidget(title)
        self.region_label = QLabel()
        self.region_label.setWordWrap(True)
        left.addWidget(self.region_label)
        self.canvas = ImageCanvas()
        left.addWidget(self.canvas, stretch=1)
        choices = QHBoxLayout()
        choices.setSpacing(24)
        self.label_buttons = {}
        for label, text in [("handwriting", "Handwriting\nLetters, words, numbers, formulas\n[1] or [H]"),
                            ("sketch", "Sketch\nDrawings, diagrams, visual shapes\n[2] or [K]")]:
            button = QPushButton(text)
            button.setObjectName(label + "Button")
            button.setCheckable(True)
            button.setMinimumHeight(150)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("1 / H" if label == "handwriting" else "2 / K")
            button.clicked.connect(lambda checked, value=label: self.choose_label(value))
            self.label_buttons[label] = button
            choices.addWidget(button, stretch=1)
        left.addLayout(choices)
        self.selection_label = QLabel()
        left.addWidget(self.selection_label)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        left.addWidget(self.status_label)
        layout.addLayout(left, stretch=4)

        sidebar = QWidget()
        sidebar.setFixedWidth(280)
        right = QVBoxLayout(sidebar)
        back_button = QPushButton("Back to Menu")
        back_button.clicked.connect(self.stop_and_back)
        right.addWidget(back_button)
        refresh_button = QPushButton("Refresh Status")
        refresh_button.clicked.connect(self.manual_refresh)
        right.addWidget(refresh_button)
        self.context_label = QLabel()
        self.context_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.context_label)
        right.addWidget(QLabel("Processing Tasks:"))
        self.processing_list = QListWidget()
        self.processing_list.itemClicked.connect(self.on_processing_clicked)
        right.addWidget(self.processing_list, stretch=1)
        right.addWidget(QLabel("Done Tasks:"))
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self.on_history_clicked)
        right.addWidget(self.history_list, stretch=1)
        self.errors_button = QPushButton("Input details")
        self.errors_button.clicked.connect(self.show_input_errors)
        self.errors_button.hide()
        right.addWidget(self.errors_button)
        fit_button = QPushButton("Fit image (F)")
        fit_button.setToolTip("Fit image (F)")
        fit_button.clicked.connect(self.canvas.fit_image)
        right.addWidget(fit_button)
        self.clear_button = QPushButton("Clear choice (Esc)")
        self.clear_button.setToolTip("Clear choice (Esc); Undo (Ctrl+Z) restores it.")
        self.clear_button.clicked.connect(lambda: self.choose_label(None))
        right.addWidget(self.clear_button)
        self.save_button = QPushButton("Save && Next (Ctrl+S)")
        self.save_button.setObjectName("saveButton")
        self.save_button.setToolTip("Save and open the next region (Ctrl+S)")
        self.save_button.clicked.connect(self.save_and_submit)
        self.save_button.setMinimumHeight(52)
        right.addWidget(self.save_button)
        layout.addWidget(sidebar)

    def init_shortcuts(self):
        for key, callback in [
            ("1", lambda: self.choose_label("handwriting")), ("H", lambda: self.choose_label("handwriting")),
            ("2", lambda: self.choose_label("sketch")), ("K", lambda: self.choose_label("sketch")),
            ("Escape", lambda: self.choose_label(None)), ("Ctrl+Z", self.undo),
            ("Ctrl+S", self.save_and_submit), ("F", self.canvas.fit_image)
        ]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def choose_label(self, label):
        if self.current_task is None or self.export_pending is not None:
            return
        if label != self.selected_label:
            self.history.append(self.selected_label)
            self.selected_label = label
        self.update_choice()

    def undo(self):
        if self.history and self.export_pending is None:
            self.selected_label = self.history.pop()
            self.update_choice()

    def update_choice(self):
        editable = self.current_task is not None and self.export_pending is None
        for label, button in self.label_buttons.items():
            button.setChecked(label == self.selected_label)
            button.setEnabled(editable)
        self.clear_button.setEnabled(editable)
        self.save_button.setEnabled(self.current_task is not None and self.selected_label is not None)
        self.selection_label.setText(f"Selected: {self.selected_label.title()} · Ctrl+S to save"
                                     if self.selected_label else "Choose one label, then save.")

    def sync_suffix(self) -> str:
        pending = self.data_manager.pending_count(self.task_type)
        if not self.api.online:
            return f" (Offline — {pending} pending sync)" if pending else " (Offline — local-first mode)"
        if pending:
            return f" ({pending} pending sync)"
        return ""

    def start_task(self):
        self._last_activity = __import__("time").monotonic()
        self.sync_timer.start(60000)
        self._renew_timer.start(30000)
        self.async_refresh(scan=True)

    def stop_and_back(self):
        self.async_back()

    def manual_refresh(self):
        self.async_refresh(scan=True)

    def sync_data_timer(self):
        self.async_refresh(scan=False)

    def start_sync(self, scan=False):
        self.async_refresh(scan=scan)

    def sync_data(self):
        self.async_sync_data()

    def show_input_errors(self):
        box = QMessageBox(self)
        box.setWindowTitle("Input details")
        box.setText("These AABB regions could not be loaded.")
        box.setDetailedText("\n".join(self.data_manager.strategies[self.task_type].errors))
        box.exec()

    def verify_source(self, task):
        strategy = self.data_manager.strategies[self.task_type]
        if strategy.image_info(Path(task["image_path"]))[2] != task["image_sha256"]:
            raise ValueError("The image changed. Refresh inputs before classifying it.")
        data = json.loads(Path(task["aabb_path"]).read_bytes())
        box = data["boxes"][task["region_index"]]
        if [box[key] for key in ("x", "y", "w", "h")] != task["box"]:
            raise ValueError("The AABB changed. Refresh inputs before classifying it.")

    def load_next_task(self, after_task_id=None):
        self.async_next(after_task_id=after_task_id)

    def set_current_task(self, task, prepared=None):
        if task:
            if prepared is not None:
                # QImage decoded in FILE lane, QPixmap produced only in GUI thread.
                original = prepared
                self.source_image = prepared
                self.source_key = (task["image_path"], task.get("image_sha256"))
                self.source_preview = original.scaledToWidth(240)
            else:
                self.verify_source(task)
                stat = Path(task["image_path"]).stat()
                key = (task["image_path"], stat.st_size, stat.st_mtime_ns)
                if key != self.source_key or self.source_image is None:
                    self.source_image = QImage(task["image_path"])
                    self.source_key = key
                    self.source_preview = self.source_image.scaledToWidth(240) if not self.source_image.isNull() else None
                original = self.source_image
            if original.isNull():
                raise ValueError("Cannot decode the source image.")
            crop = original.copy(*task["crop"])
            result = task.get("existing_result") or {}
            saved = result["points"][0] if result.get("points") else result
            if saved.get("image_sha256", task["image_sha256"]) != task["image_sha256"]:
                raise ValueError("The saved label uses an older source image. Use a new image name for a revised source.")
            if saved.get("crop", task["crop"]) != task["crop"]:
                raise ValueError("The saved label uses a different AABB crop. Use a new frame name for revised regions.")
            label = result.get("label")
            if label is None and result.get("points"):
                label = result["points"][0].get("label")
            if label not in (None, "handwriting", "sketch"):
                raise ValueError("Unknown saved classification label.")
            self.canvas.load_image(crop)
            preview = self.source_preview.copy()
            painter = QPainter(preview)
            painter.setPen(QPen(QColor("#ff28b5"), 2))
            scale = preview.width() / original.width()
            painter.drawRect(*(round(value * scale) for value in task["crop"]))
            painter.end()
            self.context_label.setPixmap(QPixmap.fromImage(preview))
            self.region_label.setText(task["label"])
            self.selected_label = label
            self.status_label.setText(f"Choose the content type of this region.{self.sync_suffix()}")
        else:
            self.canvas.show_empty_state()
            self.context_label.clear()
            self.region_label.setText("No available regions")
            self.selected_label = None
            self.status_label.setText(f"Refresh inputs or select a saved task to edit.{self.sync_suffix()}")
        self.current_task = task
        self.initial_label = self.selected_label
        self.export_pending = None
        self.history = []
        self.update_choice()


    def save_and_submit(self):
        if self.current_task is None or self.selected_label not in ("handwriting", "sketch"):
            return
        task = self.current_task
        task_id = task["task_id"]
        self.async_save([{"label": self.selected_label,
                          "image_sha256": task["image_sha256"], "crop": task["crop"]}])
        self.load_next_task(after_task_id=task_id)

    def confirm_switch(self):
        if self.export_pending is not None:
            QMessageBox.warning(self, "Export pending", "Retry Ctrl+S to finish exporting this saved result first.")
            return False
        if self.selected_label == self.initial_label:
            return True
        choice = QMessageBox.question(self, "Unsaved choice", "Discard the current choice and continue?",
                                      QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        return choice == QMessageBox.StandardButton.Discard

    def open_task(self, task, reopen=False):
        self.async_open(task, reopen=reopen)

    def on_processing_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=False)

    def on_history_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.close_target is None:
            self.close_target = self.window()
            self.close_target.installEventFilter(self)
        self.canvas.fit_image()

    def eventFilter(self, watched, event):
        if watched is self.close_target and event.type() == QEvent.Type.Close:
            if not self.confirm_switch():
                event.ignore()
                return True
            self.sync_timer.stop()
        return super().eventFilter(watched, event)
