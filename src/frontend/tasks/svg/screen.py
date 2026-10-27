import copy
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QComboBox, QDoubleSpinBox, QSlider,
    QScrollArea, QMessageBox
)
from tasks.svg.canvas import ImageCanvas
from tasks.svg.document import canvas_strokes, validate_annotation
from tasks.svg.service import TraceService, TaskWorker


class SVGTaskScreen(QWidget):
    def __init__(self, api_client, data_manager, on_back_callback):
        super().__init__()
        self.task_type = "svg"
        self.on_back_callback = on_back_callback
        self.service = TraceService(api_client, data_manager.strategies["svg"], data_manager.task_configs["svg"])
        self.current_task = None
        self.document = None
        self.crop_image = None
        self.worker = None
        self.loading = False
        self.dirty = False
        self.input_errors = []
        self.shortcuts = []
        self.close_target = None
        self.init_ui()
        self.init_shortcuts()
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.renew_lock)
        self.canvas.show_empty_state()

    def init_ui(self):
        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        toolbar = QHBoxLayout()
        self.tool_combo = QComboBox()
        self.tool_combo.addItems(["Freehand (P)", "Polyline (L)", "Edit nodes (V)"])
        self.tool_combo.currentIndexChanged.connect(self.change_tool)
        toolbar.addWidget(self.tool_combo)
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.delete_button = QPushButton("Delete stroke")
        self.fit_button = QPushButton("Fit (F)")
        for button in (self.undo_button, self.redo_button, self.delete_button, self.fit_button):
            toolbar.addWidget(button)
        left.addLayout(toolbar)

        options = QHBoxLayout()
        self.view_combo = QComboBox()
        self.view_combo.addItems(["Overlay", "Image only", "Traces only"])
        options.addWidget(self.view_combo)
        options.addWidget(QLabel("Image opacity"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(65)
        options.addWidget(self.opacity_slider)
        options.addWidget(QLabel("Simplify (px)"))
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setRange(0, 3)
        self.tolerance_input.setSingleStep(0.05)
        self.tolerance_input.setValue(0.35)
        options.addWidget(self.tolerance_input)
        self.simplify_button = QPushButton("Apply to stroke")
        options.addWidget(self.simplify_button)
        left.addLayout(options)
        self.help_label = QLabel("Drag to trace the center of a mark. Release to end a stroke; click for a dot.")
        self.help_label.setWordWrap(True)
        left.addWidget(self.help_label)
        self.canvas = ImageCanvas()
        left.addWidget(self.canvas, stretch=1)
        self.status_label = QLabel("Choose a region to trace.")
        self.status_label.setWordWrap(True)
        left.addWidget(self.status_label)
        layout.addLayout(left, stretch=4)

        panel = QWidget()
        right = QVBoxLayout(panel)
        self.back_button = QPushButton("Back to Menu")
        self.back_button.clicked.connect(self.stop_and_back)
        right.addWidget(self.back_button)
        self.refresh_button = QPushButton("Refresh regions")
        self.refresh_button.clicked.connect(self.manual_refresh)
        right.addWidget(self.refresh_button)
        self.errors_button = QPushButton("Input details")
        self.errors_button.clicked.connect(self.show_input_errors)
        self.errors_button.hide()
        right.addWidget(self.errors_button)
        self.context_label = QLabel()
        self.context_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.context_label)
        self.region_label = QLabel("No region selected")
        self.region_label.setWordWrap(True)
        right.addWidget(self.region_label)
        right.addWidget(QLabel("Regions — double-click to open or edit"))
        self.region_list = QListWidget()
        self.region_list.setMaximumHeight(160)
        self.region_list.itemDoubleClicked.connect(self.on_region_clicked)
        right.addWidget(self.region_list)
        right.addWidget(QLabel("Strokes — select to edit"))
        self.stroke_list = QListWidget()
        self.stroke_list.setMaximumHeight(120)
        self.stroke_list.currentRowChanged.connect(self.select_stroke)
        right.addWidget(self.stroke_list)
        self.count_label = QLabel("0 strokes")
        right.addWidget(self.count_label)
        self.save_button = QPushButton("Save && Next (Ctrl+S)")
        self.save_button.clicked.connect(self.save_and_submit)
        self.next_button = QPushButton("Next available region")
        self.next_button.clicked.connect(self.load_next_task)
        right.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        sidebar = QWidget()
        sidebar.setMinimumWidth(310)
        sidebar.setMaximumWidth(360)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(scroll, stretch=1)
        sidebar_layout.addWidget(self.save_button)
        sidebar_layout.addWidget(self.next_button)
        layout.addWidget(sidebar, stretch=1)

        self.undo_button.clicked.connect(self.canvas.undo)
        self.redo_button.clicked.connect(self.canvas.undo_stack.redo)
        self.delete_button.clicked.connect(self.canvas.delete_selected)
        self.fit_button.clicked.connect(self.canvas.fit_image)
        self.simplify_button.clicked.connect(self.canvas.simplify_selected)
        self.view_combo.currentTextChanged.connect(self.canvas.set_view_mode)
        self.opacity_slider.valueChanged.connect(self.change_opacity)
        self.tolerance_input.valueChanged.connect(self.change_tolerance)
        self.canvas.strokes_changed.connect(self.on_strokes_changed)
        self.canvas.selection_changed.connect(self.on_selection_changed)
        self.canvas.message.connect(self.status_label.setText)

    def init_shortcuts(self):
        for key, callback, target in [
            ("Ctrl+S", self.save_and_submit, self),
            ("Ctrl+Z", self.canvas.undo, self),
            ("Ctrl+Shift+Z", self.canvas.undo_stack.redo, self),
            ("Ctrl+Y", self.canvas.undo_stack.redo, self),
            ("Delete", self.canvas.delete_selected, self.canvas),
            ("Backspace", self.canvas.delete_selected, self.canvas),
            ("Escape", self.canvas.clear_strokes, self),
            ("Return", self.canvas.finish_drawing, self.canvas),
            ("Enter", self.canvas.finish_drawing, self.canvas),
            ("P", lambda: self.tool_combo.setCurrentIndex(0), self.canvas),
            ("L", lambda: self.tool_combo.setCurrentIndex(1), self.canvas),
            ("V", lambda: self.tool_combo.setCurrentIndex(2), self.canvas),
            ("F", self.canvas.fit_image, self.canvas),
            ("Tab", self.cycle_view, self.canvas)
        ]:
            shortcut = QShortcut(QKeySequence(key), target)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            if key == "Escape":
                shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def change_tool(self, index):
        self.canvas.set_tool(["draw", "polyline", "edit"][index])
        self.help_label.setText([
            "Drag to trace the center of a mark. Release to end a stroke; click for a dot.",
            "Click along a stroke. Enter finishes it; Ctrl+Z removes the last point; Esc clears the region.",
            "Select a stroke, then drag its nodes. Double-click near a segment to insert a node. Delete removes the stroke."
        ][index])
        self.canvas.setFocus()

    def change_opacity(self, value):
        self.canvas.image_opacity = value / 100
        self.canvas.update_view()

    def change_tolerance(self, value):
        self.canvas.tolerance = value

    def cycle_view(self):
        self.view_combo.setCurrentIndex((self.view_combo.currentIndex() + 1) % 3)

    def select_stroke(self, index):
        if not self.loading:
            self.canvas.select_stroke(index)

    def on_selection_changed(self, index):
        self.stroke_list.blockSignals(True)
        self.stroke_list.setCurrentRow(index)
        self.stroke_list.blockSignals(False)

    def on_strokes_changed(self):
        self.stroke_list.blockSignals(True)
        self.stroke_list.clear()
        for index, stroke in enumerate(self.canvas.strokes):
            tool_name = "Freehand" if stroke["tool"] == "draw" else "Polyline"
            self.stroke_list.addItem(f"{index + 1}. {tool_name} · {len(stroke['points'])} nodes")
        self.stroke_list.setCurrentRow(self.canvas.selected_index)
        self.stroke_list.blockSignals(False)
        points = sum(len(stroke["points"]) for stroke in self.canvas.strokes)
        self.count_label.setText(f"{len(self.canvas.strokes)} strokes · {points} nodes")
        if not self.loading and self.document:
            self.mark_dirty()

    def mark_dirty(self, *args):
        if not self.loading and self.document:
            self.dirty = True
            self.status_label.setText("Unsaved changes. Ctrl+S saves this region.")

    def snapshot(self):
        if self.document is None:
            raise RuntimeError("No tracing document is currently loaded.")
        document = copy.deepcopy(self.document)
        document["strokes"] = [
            {"tool": "freehand" if stroke["tool"] == "draw" else stroke["tool"],
             "points": copy.deepcopy(stroke["points"])}
            for stroke in self.canvas.strokes
        ]
        return document

    def run_job(self, function, callback, message, block=False, quiet=False):
        if self.worker:
            return False
        self.status_label.setText(message)
        self.set_busy(True, block)
        worker = TaskWorker(function, self)
        self.worker = worker

        def finish():
            self.worker = None
            self.set_busy(False, block)
            if worker.error:
                self.status_label.setText(worker.error + " Your local drawing has been kept.")
                if not quiet:
                    QMessageBox.warning(self, "Tracing", worker.error)
            else:
                callback(worker.result)
            worker.deleteLater()

        worker.finished.connect(finish)
        worker.start()
        return True

    def set_busy(self, busy, block):
        for widget in (self.back_button, self.refresh_button, self.save_button, self.next_button, self.region_list):
            widget.setEnabled(not busy)
        if block:
            for widget in (self.canvas, self.tool_combo, self.undo_button, self.redo_button,
                           self.delete_button, self.simplify_button, self.stroke_list):
                widget.setEnabled(not busy)
            for shortcut in self.shortcuts:
                shortcut.setEnabled(not busy)

    def start_task(self):
        self.sync_timer.start(30000)
        self.manual_refresh()

    def manual_refresh(self):
        def refreshed(data):
            self.update_regions(data)
            if self.current_task is None:
                self.load_next_task()
        self.run_job(lambda: self.service.refresh(scan=True), refreshed, "Scanning AABB regions and checking status…")

    def update_regions(self, data=None):
        self.region_list.clear()
        for task in self.service.tasks:
            status = self.service.statuses.get(task["task_id"], {}).get("status", "available")
            item = QListWidgetItem(f"[{status}] {task['label']}")
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.region_list.addItem(item)
        self.input_errors = list(self.service.strategy.errors)
        self.errors_button.setVisible(bool(self.input_errors))
        suffix = f" · {len(self.input_errors)} input issues" if self.input_errors else ""
        self.status_label.setText(f"{len(self.service.tasks)} regions{suffix}")

    def show_input_errors(self):
        box = QMessageBox(self)
        box.setWindowTitle("Input details")
        box.setText("These inputs could not be turned into tracing regions.")
        box.setDetailedText("\n".join(self.input_errors))
        box.exec()

    def load_next_task(self):
        if self.current_task:
            self.status_label.setText("Save this region first, or double-click another region. Unsaved edits stay in memory when switching.")
            return
        self.run_job(self.service.next_region, self.set_current_task, "Claiming the next available region…", block=True)

    def set_current_task(self, result):
        self.loading = True
        try:
            if result:
                self.current_task = result["task"]
                self.document = result["document"]
                self.crop_image = result["image"]
                self.canvas.load_image(self.crop_image, canvas_strokes(self.document))
                self.context_label.setPixmap(QPixmap.fromImage(result["preview"]))
                self.region_label.setText(self.current_task["label"])
                self.update_regions()
                self.status_label.setText(result["warning"] or "Trace each visible stroke centerline, then save the region.")
                self.canvas.setFocus()
            else:
                self.current_task = None
                self.document = None
                self.crop_image = None
                self.context_label.clear()
                self.region_label.setText("No available regions")
                self.canvas.show_empty_state()
                self.update_regions()
                self.status_label.setText("No available regions. Refresh inputs or double-click a completed region to edit.")
            self.dirty = bool(result and result["task"]["task_id"] in self.service.pending)
            self.next_button.setVisible(result is None)
            self.save_button.setEnabled(result is not None)
        finally:
            self.loading = False

    def on_region_clicked(self, item):
        if self.worker:
            return
        task = item.data(Qt.ItemDataRole.UserRole)
        if self.current_task and self.current_task["task_id"] == task["task_id"]:
            return
        self.canvas.finish_drawing()
        previous = self.current_task
        document = self.snapshot() if previous else None
        status = self.service.statuses.get(task["task_id"], {}).get("status")

        def switch():
            if previous:
                self.service.remember(previous, document)
                self.service.release(previous)
            return self.service.load_region(task, reopen=status == "done")

        self.run_job(switch, self.set_current_task, "Opening region…", block=True)

    def save_and_submit(self):
        if self.worker or not self.current_task:
            return
        self.canvas.finish_drawing()
        document = self.snapshot()
        try:
            validate_annotation(document, self.current_task, complete=True)
        except (ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, "Tracing", str(error))
            return
        if self.crop_image is None:
            QMessageBox.warning(self, "Tracing", "No crop image is loaded.")
            return
        task, image = self.current_task, self.crop_image.copy()

        def saved(result):
            self.dirty = False
            self.current_task = None
            self.document = None
            self.canvas.show_empty_state()
            self.update_regions()
            self.load_next_task()

        self.run_job(lambda: self.service.submit(task, document, image), saved, "Exporting JSON, SVG and crop; syncing result…", block=True)

    def renew_lock(self):
        if self.worker or not self.current_task:
            return
        task = self.current_task
        self.run_job(lambda: self.service.claim(task), lambda result: self.status_label.setText("Task lock renewed."),
                     "Renewing task lock…", quiet=True)

    def stop_and_back(self):
        if self.worker:
            return
        self.canvas.finish_drawing()
        task = self.current_task
        document = self.snapshot() if task else None

        def release():
            if task:
                self.service.remember(task, document)
                self.service.release(task)

        def finished(result):
            self.sync_timer.stop()
            self.set_current_task(None)
            self.on_back_callback()

        self.run_job(release, finished, "Releasing region…", block=True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.close_target is None:
            self.close_target = self.window()
            if self.close_target is not None:
                self.close_target.installEventFilter(self)
        self.canvas.fit_image()

    def eventFilter(self, watched, event):
        if watched is self.close_target and event.type() == QEvent.Type.Close:
            if self.worker:
                self.status_label.setText("Please wait for the current save or server request, then close again.")
                event.ignore()
                return True
            self.dirty = False
            self.sync_timer.stop()
        return super().eventFilter(watched, event)
