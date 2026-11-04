import os
from pathlib import Path
from typing import Dict, List
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QPushButton, QMessageBox, QListWidgetItem, QLabel, QFileDialog
from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QImage
from core.homography import board_homography, rectify_image, homography_transform, project_boxes
from tasks.aabb.importer import index_detection_files, import_boxes, image_key
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
        self.homography = None
        self.board_image = None
        self.rectifying = False
        self.projection_snapshot = None
        self.detection_folder = None
        self.detection_index = {}
        self.importing = False

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
        tools_row = QHBoxLayout()
        tools_row.setSpacing(4)
        self.homography_button = QPushButton("Flatten")
        self.homography_button.setCheckable(True)
        self.homography_button.setEnabled(False)
        self.homography_button.setToolTip("Toggle flattened/original board coordinates. Existing boxes are projected to enclosing rectangles; unchanged round trips restore the exact boxes.")
        self.homography_button.clicked.connect(self.toggle_homography)
        self.import_folder_button = QPushButton("JSON folder")
        self.import_folder_button.setToolTip("Choose the folder containing detection JSON files.")
        self.import_folder_button.clicked.connect(self.choose_detection_folder)
        self.import_button = QPushButton("Import")
        self.import_button.setEnabled(False)
        self.import_button.setToolTip("Ctrl+I: import boxes for the current image locally. Only Save submits to the database.")
        self.import_button.clicked.connect(self.import_current_detections)
        for button in (self.import_folder_button, self.import_button, self.homography_button):
            button.setFixedHeight(36)
            button.setStyleSheet("padding: 4px 10px;")
            tools_row.addWidget(button)
        right_panel.addLayout(tools_row)

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
        elif event.key() == Qt.Key.Key_I and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.import_current_detections()

    def save_and_submit(self, coords):
        if not self.current_task or not coords or self.rectifying or self.importing:
            return
        if not self.canvas.valid_coordinates(coords):
            self.status_label.setText("Cannot save: finish drawing and keep every box inside the image crop.")
            return
        task_id = self.current_task["task_id"]
        result = {"points": coords}
        if self.homography is not None:
            result["homography"] = self.homography
        self.async_save(result)
        self.load_next_task(after_task_id=task_id)

    def load_next_task(self, after_task_id=None):
        self.async_next(after_task_id=after_task_id)

    def set_current_task(self, task, prepared=None):
        self.projection_snapshot = None
        if task:
            self.current_task = task
            existing_result = task.get("existing_result")
            self.homography = (existing_result or {}).get("homography")
            self.board_image = prepared.get("source_image") if isinstance(prepared, dict) else prepared
            if self.board_image is None:
                self.board_image = QImage(task["image_path"])
            if self.homography is not None:
                if board_homography(task["crop_coords"], [self.board_image.width(), self.board_image.height()]) != self.homography:
                    raise ValueError("The saved homography uses different board corners. Refresh inputs or use a separate output folder.")
                image = prepared.get("rectified") if isinstance(prepared, dict) and prepared.get("rectified_homography") == self.homography else None
                self.canvas.load_image(image if image is not None else rectify_image(self.board_image, self.homography))
            else:
                self.canvas.load_image(prepared if prepared is not None else task['image_path'], task.get('crop_coords'))
            self.canvas.coordinate_space = self.homography
            if existing_result and "points" in existing_result:
                self.canvas.load_existing_points(existing_result["points"])
            self.canvas.setFocus()
        else:
            self.current_task = None
            self.homography = None
            self.board_image = None
            self.canvas.show_empty_state()
        self.homography_button.setChecked(self.homography is not None)
        self.homography_button.setEnabled(task is not None and not self.rectifying)
        self.import_button.setEnabled(task is not None and bool(self.detection_index) and not self.importing)

    def toggle_homography(self, enabled):
        if self.current_task is None or self.rectifying or self.importing:
            self.homography_button.setChecked(self.homography is not None)
            return
        if enabled == (self.homography is not None):
            return
        task = self.current_task
        image = self.board_image
        before = self.homography
        boxes = self.canvas.get_coordinates()
        snapshot = self.projection_snapshot
        self.rectifying = True
        self.homography_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.canvas.setEnabled(False)

        def prepare():
            data = board_homography(task["crop_coords"], [image.width(), image.height()]) if enabled else None
            transformed = rectify_image(image, data)
            if snapshot and snapshot["after_space"] == before and snapshot["after_boxes"] == boxes and snapshot["before_space"] == data:
                projected, adjusted = snapshot["before_boxes"], 0
            else:
                transform = homography_transform(data) if enabled else homography_transform(before).inverted()[0]
                projected, adjusted = project_boxes(boxes, transform, QRectF(transformed.rect()))
            return data, transformed, projected, adjusted

        def finished(result):
            self.rectifying = False
            self.canvas.setEnabled(True)
            if self.current_task is task:
                self.homography, transformed, projected, adjusted = result
                self.canvas.load_image(transformed, None if enabled else task["crop_coords"])
                self.canvas.load_existing_points(projected)
                self.canvas.coordinate_space = self.homography
                self.projection_snapshot = {"before_space": before, "before_boxes": boxes,
                                            "after_space": self.homography, "after_boxes": projected}
                self.homography_button.setChecked(enabled)
                mode = "Rectified board coordinates." if enabled else "Original image coordinates (dehomography)."
                self.status_label.setText(f"{mode} Boxes enclose projected corners. {adjusted} boxes clipped or outside the image.")
            self.homography_button.setEnabled(self.current_task is not None)
            self.import_button.setEnabled(self.current_task is not None and bool(self.detection_index))

        def failed(error):
            self.rectifying = False
            self.canvas.setEnabled(True)
            self.homography_button.setEnabled(self.current_task is not None)
            self.homography_button.setChecked(self.homography is not None)
            self.import_button.setEnabled(self.current_task is not None and bool(self.detection_index))
            self.status_label.setText(str(error))

        self._enqueue("file", prepare, finished, failed)

    def choose_detection_folder(self):
        if self.importing or self.rectifying:
            return
        folder = QFileDialog.getExistingDirectory(self, "Detection JSON folder", self.detection_folder or "")
        if not folder:
            return
        self.importing = True
        self.import_folder_button.setEnabled(False)
        self.import_button.setEnabled(False)

        def ready(index):
            self.detection_folder = folder
            self.detection_index = index
            self.importing = False
            self.import_folder_button.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None)
            self.status_label.setText(f"Indexed {len(index)} detection files. Import applies only to the current image; DB state is unchanged.")

        def failed(error):
            self.importing = False
            self.import_folder_button.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None and bool(self.detection_index))
            self.status_label.setText(str(error))

        self._enqueue("file", lambda: index_detection_files(folder), ready, failed)

    def import_current_detections(self):
        if self.current_task is None or self.importing or self.rectifying:
            return
        if not self.detection_index:
            self.status_label.setText("Choose a detection JSON folder first.")
            return
        task = self.current_task
        root = Path(self.data_manager.task_configs[self.task_type]["keyframes"]).resolve()
        try:
            name = Path(task["image_path"]).resolve().relative_to(root).as_posix()
        except ValueError:
            self.status_label.setText("Current image is outside the configured keyframe folder.")
            return
        path = self.detection_index.get(image_key(name))
        if path is None:
            self.status_label.setText("No detection JSON matches " + name)
            return
        homography = self.homography
        image_size = [self.board_image.width(), self.board_image.height()]
        bounds = self.canvas.image_item.boundingRect().translated(self.canvas.crop_offset_x, self.canvas.crop_offset_y)
        self.importing = True
        self.import_button.setEnabled(False)
        self.import_folder_button.setEnabled(False)
        self.canvas.setEnabled(False)

        def ready(result):
            self.importing = False
            self.canvas.setEnabled(True)
            self.import_folder_button.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None)
            if self.current_task is not task or self.homography != homography:
                return
            imported, adjusted = result
            boxes = self.canvas.get_coordinates()
            known = {tuple(box[key] for key in ("x", "y", "w", "h")) for box in boxes}
            added = 0
            for box in imported:
                key = tuple(box[k] for k in ("x", "y", "w", "h"))
                if key not in known:
                    boxes.append(box)
                    known.add(key)
                    added += 1
            self.canvas.load_existing_points(boxes)
            self.status_label.setText(f"Imported {added} new boxes locally; {adjusted} clipped or outside the board. Review, then Save to submit to DB.")
            self.canvas.setFocus()

        def failed(error):
            self.importing = False
            self.canvas.setEnabled(True)
            self.import_folder_button.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None and bool(self.detection_index))
            self.status_label.setText(str(error))

        self._enqueue("file", lambda: import_boxes(path, name, image_size, homography, bounds), ready, failed)

    def on_processing_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=False)

    def on_history_clicked(self, item):
        self.async_open(item.data(Qt.ItemDataRole.UserRole), reopen=True)

