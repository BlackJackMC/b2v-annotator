import json
from pathlib import Path
from typing import Dict, List
import requests
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QImage, QPainter, QPen, QColor, QPixmap, QKeySequence, QShortcut
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, QMessageBox
from core.data_manager import TaskSyncWorker
from core.api_client import APIClient
from tasks.classify.canvas import ImageCanvas


class ClassifyTaskScreen(QWidget):
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
        self.history = []
        self.shortcuts = []
        self.close_target = None
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_data_timer)
        self.init_ui()
        self.init_shortcuts()
        self.set_current_task(None)

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
        self.manual_refresh()
        self.sync_timer.start(30000)

    def stop_and_back(self):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        if not self.confirm_switch():
            return
        self.sync_timer.stop()
        self.data_manager.release_task(self.task_type, self.current_task)
        self.set_current_task(None)
        self.on_back_callback()

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
                    self.errors_button.setVisible(bool(self.data_manager.strategies[self.task_type].errors))
                    self.handle_conflicts(worker.conflicts)
                except (OSError, ValueError) as error:
                    self.status_label.setText(str(error))
            worker.deleteLater()

        worker.finished.connect(finished)
        worker.start()

    def sync_data(self):
        done_tasks = self.data_manager.get_done_tasks(self.task_type, refresh=False)
        processing_tasks = self.data_manager.get_processing_tasks(self.task_type, refresh=False)
        for widget, tasks in [(self.history_list, done_tasks), (self.processing_list, processing_tasks)]:
            widget.clear()
            for task in tasks:
                label = f"[pending] {task['label']}" if task.get("pending_sync") else task["label"]
                if task.get("expired"):
                    label = f"[expired] {label}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, task)
                widget.addItem(item)

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
                    self.status_label.setText(f"Region {tid} is currently locked by {owner}.{self.sync_suffix()}")
                    continue
                if ctype in ("done", "done_active"):
                    box = QMessageBox(self)
                    box.setWindowTitle("Saved result changed")
                    box.setText(f"Region {tid} has a newer server result. Choose which classification to keep.")
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

    def load_next_task(self):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        try:
            task = self.data_manager.get_free_task(self.task_type)
            self.set_current_task(task)
        except (requests.RequestException, OSError, ValueError, KeyError, IndexError, TypeError) as error:
            self.status_label.setText(f"Could not open the next region: {error}")

    def set_current_task(self, task):
        if task:
            self.verify_source(task)
            original = QImage(task["image_path"])
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
            preview = original.scaledToWidth(240)
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
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        if not self.current_task or self.selected_label not in ("handwriting", "sketch"):
            return
        task = self.current_task
        output = Path(task["output_path"])
        try:
            if output.exists():
                existing = json.loads(output.read_bytes())
                if not isinstance(existing, dict) or existing.get("task_id") != task["task_id"]:
                    raise ValueError("This output file belongs to another task. Choose a separate classification output folder.")
            self.verify_source(task)
            points = [{"label": self.selected_label, "image_sha256": task["image_sha256"], "crop": task["crop"]}]
            conflicts = self.data_manager.save_task_local_first(self.task_type, task, points)
        except (requests.RequestException, OSError, ValueError, KeyError, IndexError, TypeError) as error:
            self.status_label.setText(str(error))
            QMessageBox.warning(self, "Save failed", f"{error}\nYour choice is still open. Retry Ctrl+S after resolving the problem.")
            return
        self.set_current_task(None)
        self.handle_conflicts(conflicts)
        try:
            self.sync_data()
            self.load_next_task()
        except (requests.RequestException, OSError, ValueError) as error:
            self.status_label.setText(f"Saved locally. Could not refresh tasks: {error}")

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
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        if self.current_task and self.current_task["task_id"] == task["task_id"] and not task.get("expired"):
            return
        if not self.confirm_switch():
            return
        try:
            self.verify_source(task)
            opened = self.data_manager.open_task(self.task_type, task, reopen=reopen)
            if not opened:
                raise ValueError("Cannot lock this task. Your current choice has been kept.")
            if self.current_task and self.current_task["task_id"] != opened["task_id"]:
                self.data_manager.release_task(self.task_type, self.current_task)
            self.set_current_task(opened)
            self.sync_data()
        except (requests.RequestException, OSError, ValueError, KeyError, IndexError, TypeError) as error:
            QMessageBox.warning(self, "Classification", str(error))

    def on_processing_clicked(self, item):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        self.open_task(item.data(Qt.ItemDataRole.UserRole))

    def on_history_clicked(self, item):
        if self.sync_worker is not None:
            self.status_label.setText("Synchronization is running. Please retry this action when it finishes.")
            return
        self.open_task(item.data(Qt.ItemDataRole.UserRole), reopen=True)

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
