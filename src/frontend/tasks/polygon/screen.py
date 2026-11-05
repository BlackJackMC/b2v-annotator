from pathlib import Path
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QPushButton
from tasks.aabb.screen import AABBTaskScreen
from tasks.polygon.canvas import ImageCanvas
from tasks.polygon.importer import import_polygons, project_polygons
from tasks.aabb.importer import image_key
from core.homography import board_homography, homography_transform, rectify_image


class PolygonTaskScreen(AABBTaskScreen):
    def __init__(self, api_client, data_manager, on_back_callback):
        super().__init__(api_client, data_manager, on_back_callback)
        self.task_type = "polygon"

    def init_ui(self):
        super().init_ui()
        old_canvas = self.canvas
        self.canvas = ImageCanvas()
        self.layout().replaceWidget(old_canvas, self.canvas)
        old_canvas.deleteLater()
        self.canvas.message.connect(self.status_label.setText)
        self.import_button.setToolTip("Ctrl+I: import four-corner polygons or boxes for this image. Save submits changes.")
        self.homography_button.setToolTip("Flatten the board for editing. TXT output always uses original image coordinates.")
        save = QPushButton("Save & Next (Ctrl+S)")
        save.setObjectName("saveButton")
        save.clicked.connect(lambda: self.save_and_submit(self.canvas.get_coordinates()))
        self.layout().itemAt(1).layout().insertWidget(2, save)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_S and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.save_and_submit(self.canvas.get_coordinates())
        elif event.key() == Qt.Key.Key_Escape:
            self.canvas.keyPressEvent(event)
        elif event.key() == Qt.Key.Key_I and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.import_current_detections()

    def save_and_submit(self, coords):
        if not self.current_task or self.rectifying or self.importing:
            return
        if not self.canvas.valid_coordinates(coords):
            self.status_label.setText("Finish or cancel the current polygon before saving. All corners must stay inside the image.")
            return
        task_id = self.current_task["task_id"]
        try:
            if self.homography is not None:
                coords = project_polygons(coords, homography_transform(self.homography).inverted()[0], QRectF(self.board_image.rect()))
        except ValueError as error:
            self.status_label.setText(str(error))
            return
        self.async_save(coords)
        self.load_next_task(after_task_id=task_id)

    def confirm_unsaved_switch(self):
        if self.rectifying or self.importing or self.canvas.pending or self.canvas.drag:
            self.status_label.setText("Finish the polygon or press Esc to cancel before switching tasks.")
            return False
        return super().confirm_unsaved_switch()

    def set_current_task(self, task, prepared=None):
        self.current_task = task
        self.homography = None
        self.board_image = None
        if task:
            self.board_image = prepared if isinstance(prepared, QImage) else QImage(task["image_path"])
            if self.board_image.isNull():
                self.current_task = None
                self.canvas.show_empty_state()
                raise ValueError("Cannot decode the selected image.")
            self.canvas.load_image(self.board_image)
            self.canvas.load_existing_points((task.get("existing_result") or {}).get("points", []))
            self.canvas.setFocus()
        else:
            self.canvas.show_empty_state()
        self.homography_button.setChecked(False)
        self.homography_button.setEnabled(bool(task and task.get("crop_coords")))
        self.import_button.setEnabled(bool(task and self.detection_index) and not self.importing)

    def toggle_homography(self, enabled):
        if not self.current_task or self.rectifying or self.importing or self.canvas.pending or self.canvas.drag:
            self.homography_button.setChecked(self.homography is not None)
            self.status_label.setText("Finish or cancel drawing before changing projection.")
            return
        if enabled == (self.homography is not None):
            return
        task, image, before = self.current_task, self.board_image, self.homography
        if enabled and not task.get("crop_coords"):
            self.homography_button.setChecked(False)
            self.status_label.setText("This task has no board corners. Refresh inputs first.")
            return
        polygons = self.canvas.get_coordinates()
        self.rectifying = True
        self.canvas.setEnabled(False)
        self.homography_button.setEnabled(False)

        def prepare():
            data = board_homography(task["crop_coords"], [image.width(), image.height()]) if enabled else None
            transformed = rectify_image(image, data)
            transform = homography_transform(data) if enabled else homography_transform(before).inverted()[0]
            return data, transformed, project_polygons(polygons, transform, QRectF(transformed.rect()))

        def ready(result):
            self.rectifying = False
            self.canvas.setEnabled(True)
            self.homography_button.setEnabled(self.current_task is not None)
            if self.current_task is not task:
                return
            self.homography, transformed, polygons = result
            self.canvas.load_image(transformed)
            self.canvas.load_existing_points(polygons)
            self.homography_button.setChecked(enabled)
            self.status_label.setText("Flattened board." if enabled else "Original image coordinates.")

        def failed(error):
            self.rectifying = False
            self.canvas.setEnabled(True)
            self.homography_button.setEnabled(self.current_task is not None)
            self.homography_button.setChecked(self.homography is not None)
            self.status_label.setText(str(error))

        self._enqueue("file", prepare, ready, failed)

    def import_current_detections(self):
        if not self.current_task or self.importing or self.rectifying or self.canvas.pending or self.canvas.drag:
            self.status_label.setText("Finish or cancel drawing before importing.")
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
            self.status_label.setText("Choose a JSON folder containing detections for " + name)
            return
        data = self.homography
        size = [self.board_image.width(), self.board_image.height()]
        bounds = self.canvas.image_item.boundingRect()
        self.importing = True
        self.canvas.setEnabled(False)
        self.import_button.setEnabled(False)

        def ready(polygons):
            self.importing = False
            self.canvas.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None)
            if self.current_task is not task:
                return
            current = self.canvas.get_coordinates()
            def key(points):
                return tuple(sorted((round(p["x"], 6), round(p["y"], 6)) for p in points))
            known = {key(p) for p in current}
            added = 0
            for polygon in polygons:
                if key(polygon) not in known:
                    current.append(polygon)
                    known.add(key(polygon))
                    added += 1
            self.canvas.load_existing_points(current)
            self.status_label.setText(f"Imported {added} polygons locally. Save to submit.")
            self.canvas.setFocus()

        def failed(error):
            self.importing = False
            self.canvas.setEnabled(True)
            self.import_button.setEnabled(self.current_task is not None)
            self.status_label.setText(str(error))

        self._enqueue("file", lambda: import_polygons(path, name, size, data, bounds), ready, failed)
