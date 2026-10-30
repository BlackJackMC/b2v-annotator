import copy
from html import escape
import math
from PyQt6 import sip
from PyQt6.QtCore import Qt, QPointF, QTimer, QEvent, QSettings, QVariantAnimation, QEasingCurve
from PyQt6.QtGui import QCursor, QImage, QKeySequence, QMouseEvent, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QComboBox, QDoubleSpinBox, QSlider,
    QScrollArea, QMessageBox, QTabWidget, QFormLayout, QKeySequenceEdit,
    QCheckBox, QAbstractItemView, QDialog, QGridLayout
)
from tasks.svg.canvas import ImageCanvas
from tasks.svg.document import canvas_strokes, stroke_data
from tasks.svg.service import TraceService, TaskWorker, LocalEditsConflict


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
        self.busy = False
        self.job_blocking = False
        self.save_requested = False
        self.saving = False
        self.tool_menu = None
        self.loading = False
        self.dirty = False
        self.input_errors = []
        self.shortcuts = []
        self.close_target = None
        self.settings = QSettings("board2vec", "annotator")
        self.stroke_list_dirty = True
        self.click_keys = set()
        self.click_target = None
        self.dispatching_click = False
        self.menu_shortcuts = {}
        self.init_ui()
        self.init_shortcuts()
        QApplication.instance().installEventFilter(self)
        self.panel_animation = QVariantAnimation(self)
        self.panel_animation.setDuration(140)
        self.panel_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.panel_animation.setStartValue(300)
        self.panel_animation.setEndValue(300)
        self.panel_animation.valueChanged.connect(self.animate_panel)
        self.panel_animation.finished.connect(self.finish_panel_animation)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.renew_lock)
        self.canvas.show_empty_state()

    def init_ui(self):
        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        toolbar = QHBoxLayout()
        self.tool_combo = QComboBox()
        self.tool_combo.addItems(["Freehand", "Polyline", "Select / nodes", "Stroke eraser", "Pan"])
        self.tool_combo.currentIndexChanged.connect(self.change_tool)
        toolbar.addWidget(self.tool_combo)
        toolbar.addWidget(QLabel("Width"))
        self.width_input = QDoubleSpinBox()
        self.width_input.setRange(0.5, 50)
        self.width_input.setSingleStep(0.5)
        self.width_input.setValue(1)
        self.width_input.setSuffix(" px")
        self.width_input.setToolTip("Width in crop pixels. In Select, changes selected strokes; otherwise sets the new pen width.")
        toolbar.addWidget(self.width_input)
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.fit_button = QPushButton("Fit")
        self.focus_button = QPushButton("Focus")
        self.focus_button.setCheckable(True)
        self.pan_button = QPushButton("Hand")
        self.pan_button.setCheckable(True)
        self.pan_button.clicked.connect(lambda checked: self.tool_combo.setCurrentIndex(4 if checked else 0))
        for button in (self.undo_button, self.redo_button, self.pan_button, self.fit_button, self.focus_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            toolbar.addWidget(button)
        toolbar.addStretch()
        left.addLayout(toolbar)
        self.help_label = QLabel("Drag to trace. Hover an endpoint, then Shift + drag to continue that stroke.")
        self.help_label.setWordWrap(True)
        left.addWidget(self.help_label)
        self.canvas = ImageCanvas()
        left.addWidget(self.canvas, stretch=1)
        self.footer = QWidget()
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(2)
        footer = QHBoxLayout()
        self.status_label = QLabel("Choose a region to trace.")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel()
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.count_label.setStyleSheet("font-size: 11px; color: #a6bacb;")
        footer.addWidget(self.status_label, stretch=1)
        footer.addWidget(self.count_label)
        footer_layout.addLayout(footer)
        left.addWidget(self.footer)
        layout.addLayout(left, stretch=4)

        self.sidebar = QWidget()
        self.sidebar.setMinimumWidth(280)
        self.sidebar.setMaximumWidth(340)
        right = QVBoxLayout(self.sidebar)
        right.setContentsMargins(0, 0, 0, 0)
        self.shortcuts_button = QPushButton("Shortcuts")
        self.shortcuts_button.setCheckable(True)
        self.shortcuts_button.setChecked(True)
        self.shortcuts_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        right.addWidget(self.shortcuts_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.shortcut_guide = QTabWidget()
        self.shortcut_guide.setMaximumHeight(280)
        self.shortcut_guide.setMinimumHeight(230)
        self.shortcut_guide.setStyleSheet("QLabel { font-size: 12px; } QTabBar::tab { padding: 4px 8px; }")
        self.shortcuts_button.toggled.connect(self.shortcut_guide.setVisible)
        right.addWidget(self.shortcut_guide)
        self.guide_labels = []

        navigation = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.stop_and_back)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.manual_refresh)
        navigation.addWidget(self.back_button)
        navigation.addWidget(self.refresh_button)
        right.addLayout(navigation)
        self.tabs = QTabWidget()
        right.addWidget(self.tabs, stretch=1)

        regions = QWidget()
        region_layout = QVBoxLayout(regions)
        self.region_label = QLabel("No region selected")
        self.region_label.setWordWrap(True)
        region_layout.addWidget(self.region_label)
        self.context_label = QLabel()
        self.context_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        region_layout.addWidget(self.context_label)
        self.progress_label = QLabel("Done: 0 / 0")
        region_layout.addWidget(self.progress_label)
        region_layout.addWidget(QLabel("Processing / available"))
        self.region_list = QListWidget()
        self.region_list.itemDoubleClicked.connect(self.on_region_clicked)
        region_layout.addWidget(self.region_list, stretch=1)
        region_layout.addWidget(QLabel("Done"))
        self.history_list = QListWidget()
        self.history_list.itemDoubleClicked.connect(self.on_region_clicked)
        region_layout.addWidget(self.history_list, stretch=1)
        self.errors_button = QPushButton("Input details")
        self.errors_button.clicked.connect(self.show_input_errors)
        self.errors_button.hide()
        region_layout.addWidget(self.errors_button)
        self.tabs.addTab(regions, "Regions")

        options = QWidget()
        option_layout = QVBoxLayout(options)
        form = QFormLayout()
        self.view_combo = QComboBox()
        self.view_combo.addItems(["Overlay", "Image only", "Traces only"])
        form.addRow("View", self.view_combo)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(65)
        form.addRow("Image opacity", self.opacity_slider)
        self.smoothing_input = QCheckBox("Adaptive")
        self.smoothing_input.setToolTip("Reduces hand jitter while drawing. Off preserves sampled positions.")
        form.addRow("Smoothing", self.smoothing_input)
        self.width_step_input = QDoubleSpinBox()
        self.width_step_input.setRange(0.01, 10)
        self.width_step_input.setSingleStep(0.05)
        self.width_step_input.setValue(self.width_input.singleStep())
        self.width_step_input.setSuffix(" px")
        self.width_step_input.setToolTip("Width change per key press or button click, including the Q menu. For example: 0.10 or 0.15 px.")
        form.addRow("Width step", self.width_step_input)
        self.corner_input = QDoubleSpinBox()
        self.corner_input.setRange(0, 10)
        self.corner_input.setSingleStep(0.5)
        self.corner_input.setSuffix(" px")
        self.corner_input.setSpecialValueText("Off")
        self.corner_input.setToolTip("Round polyline corners by this distance in crop pixels. Off keeps every clicked corner.")
        form.addRow("Round corners", self.corner_input)
        self.nodes_input = QCheckBox("Edit nodes")
        self.nodes_input.setToolTip("Show individual points for the selected stroke. Turn off to move whole strokes.")
        form.addRow("Selection", self.nodes_input)
        self.eraser_input = QDoubleSpinBox()
        self.eraser_input.setRange(4, 100)
        self.eraser_input.setSingleStep(2)
        self.eraser_input.setValue(24)
        self.eraser_input.setSuffix(" px")
        self.eraser_input.setToolTip("Eraser diameter on screen. A sweep removes whole strokes.")
        form.addRow("Eraser size", self.eraser_input)
        self.tolerance_input = QDoubleSpinBox()
        self.tolerance_input.setRange(0, 3)
        self.tolerance_input.setSingleStep(0.05)
        self.tolerance_input.setValue(0.35)
        self.tolerance_input.setSuffix(" px")
        self.tolerance_input.setToolTip("Maximum simplification error in crop pixels. Applied only when requested.")
        form.addRow("Simplify error", self.tolerance_input)
        option_layout.addLayout(form)
        self.simplify_button = QPushButton("Simplify selected")
        option_layout.addWidget(self.simplify_button)
        transforms = QHBoxLayout()
        self.transform_buttons = {}
        for name, label, scale, angle in [("Shrink selection", "Scale −", 1 / 1.05, 0),
                                         ("Grow selection", "Scale +", 1.05, 0),
                                         ("Rotate left", "-5°", 1, -5), ("Rotate right", "+5°", 1, 5)]:
            button = QPushButton(label)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet("padding: 7px 5px;")
            button.clicked.connect(lambda checked=False, s=scale, a=angle: self.canvas.transform_selected(s, a))
            transforms.addWidget(button)
            self.transform_buttons[name] = button
        option_layout.addLayout(transforms)
        option_layout.addWidget(QLabel("Strokes"))
        self.stroke_list = QListWidget()
        self.stroke_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.stroke_list.itemSelectionChanged.connect(self.select_strokes)
        option_layout.addWidget(self.stroke_list, stretch=1)
        self.delete_button = QPushButton("Delete selected")
        option_layout.addWidget(self.delete_button)
        self.tabs.addTab(options, "Tools")

        keys = QWidget()
        key_layout = QVBoxLayout(keys)
        key_panel = QWidget()
        self.key_form = QFormLayout(key_panel)
        self.key_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        key_scroll = QScrollArea()
        key_scroll.setWidgetResizable(True)
        key_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        key_scroll.setWidget(key_panel)
        key_layout.addWidget(key_scroll, stretch=1)
        self.key_status = QLabel("Record a key or choose Unbind, then Apply. Cancel keeps the active bindings.")
        self.key_status.setWordWrap(True)
        key_layout.addWidget(self.key_status)
        key_buttons = QHBoxLayout()
        apply_keys = QPushButton("Apply")
        reset_keys = QPushButton("Defaults")
        cancel_keys = QPushButton("Cancel")
        cancel_keys.clicked.connect(self.cancel_bindings)
        apply_keys.clicked.connect(self.apply_bindings)
        reset_keys.clicked.connect(self.reset_bindings)
        key_buttons.addWidget(apply_keys)
        key_buttons.addWidget(reset_keys)
        key_buttons.addWidget(cancel_keys)
        key_layout.addLayout(key_buttons)
        self.tabs.addTab(keys, "Keys")

        self.save_button = QPushButton("Save && Next")
        self.save_button.setObjectName("saveButton")
        self.save_button.clicked.connect(self.save_and_submit)
        self.next_button = QPushButton("Next available region")
        self.next_button.clicked.connect(self.load_next_task)
        right.addWidget(self.save_button)
        right.addWidget(self.next_button)
        layout.addWidget(self.sidebar, stretch=1)

        self.undo_button.clicked.connect(self.canvas.undo)
        self.redo_button.clicked.connect(self.canvas.redo)
        self.delete_button.clicked.connect(self.canvas.delete_selected)
        self.fit_button.clicked.connect(self.canvas.fit_image)
        self.focus_button.toggled.connect(self.toggle_focus)
        self.simplify_button.clicked.connect(self.canvas.simplify_selected)
        self.view_combo.currentTextChanged.connect(self.canvas.set_view_mode)
        self.opacity_slider.valueChanged.connect(self.change_opacity)
        self.tolerance_input.valueChanged.connect(self.change_tolerance)
        self.width_input.valueChanged.connect(self.change_width)
        self.width_step_input.valueChanged.connect(self.change_width_step)
        self.eraser_input.valueChanged.connect(self.change_eraser_size)
        self.smoothing_input.toggled.connect(self.change_smoothing)
        self.nodes_input.toggled.connect(self.change_nodes)
        self.corner_input.valueChanged.connect(lambda value: setattr(self.canvas, "corner_radius", value))
        self.canvas.strokes_changed.connect(self.on_strokes_changed)
        self.canvas.selection_changed.connect(self.on_selection_changed)
        self.canvas.message.connect(self.show_status)
        self.canvas.view_changed.connect(self.update_canvas_status)
        self.tabs.currentChanged.connect(self.refresh_stroke_list)

    def init_shortcuts(self):
        self.shortcut_actions = [
            ("Save", "Ctrl+S", self.save_and_submit, self),
            ("Undo", "Ctrl+Z", self.canvas.undo, self),
            ("Redo", "Ctrl+Shift+Z", self.canvas.redo, self),
            ("Delete", "Delete", self.canvas.delete_selected, self.canvas),
            ("Clear region", "Escape", self.canvas.clear_strokes, self.canvas),
            ("Finish polyline", "Return", self.canvas.finish_drawing, self.canvas),
            ("Freehand", "P", lambda: self.tool_combo.setCurrentIndex(0), self.canvas),
            ("Polyline", "L", lambda: self.tool_combo.setCurrentIndex(1), self.canvas),
            ("Select / nodes", "V", lambda: self.tool_combo.setCurrentIndex(2), self.canvas),
            ("Toggle nodes", "N", self.nodes_input.toggle, self.canvas),
            ("Stroke eraser", "E", lambda: self.tool_combo.setCurrentIndex(3), self.canvas),
            ("Pan", "H", lambda: self.tool_combo.setCurrentIndex(4), self.canvas),
            ("Fit", "F", self.canvas.fit_image, self.canvas),
            ("Focus canvas", "Tab", self.focus_button.toggle, self.canvas),
            ("Cycle view", "O", self.cycle_view, self.canvas),
            ("Tool menu", "Q", self.show_tool_menu, self.canvas),
            ("Zoom in", "Ctrl++", lambda: self.canvas.zoom(1.15), self.canvas),
            ("Zoom out", "Ctrl+-", lambda: self.canvas.zoom(1 / 1.15), self.canvas),
            ("Smaller brush", "[", lambda: self.step_size(-1), self.canvas),
            ("Larger brush", "]", lambda: self.step_size(1), self.canvas),
            ("Select all", "Ctrl+A", self.select_all, self.canvas),
            ("Simplify", "Ctrl+L", self.canvas.simplify_selected, self.canvas),
            ("Pan left", "Left", lambda: self.canvas.pan_by(-40, 0), self.canvas),
            ("Pan right", "Right", lambda: self.canvas.pan_by(40, 0), self.canvas),
            ("Pan up", "Up", lambda: self.canvas.pan_by(0, -40), self.canvas),
            ("Pan down", "Down", lambda: self.canvas.pan_by(0, 40), self.canvas),
            ("Shrink selection", "Alt+-", lambda: self.canvas.transform_selected(1 / 1.05), self.canvas),
            ("Grow selection", "Alt+=", lambda: self.canvas.transform_selected(1.05), self.canvas),
            ("Rotate left", "Alt+Left", lambda: self.canvas.transform_selected(angle=-5), self.canvas),
            ("Rotate right", "Alt+Right", lambda: self.canvas.transform_selected(angle=5), self.canvas),
            ("Left click", "D", None, self.canvas),
            ("Temporary pan", "Space", None, self.canvas),
            ("Smooth stroke", "S", self.canvas.smooth_current, self.canvas),
            ("Adaptive smoothing", "Shift+S", self.smoothing_input.toggle, self.canvas),
            ("Show shortcuts", "F1", self.shortcuts_button.toggle, self.canvas)
        ]
        self.key_editors = {}
        self.shortcut_objects = {}
        self.shortcut_descriptions = {
            "Save": "Save JSON and SVG, then open the next region.",
            "Undo": "Undo a point or an edit; up to 200 completed edits per region.",
            "Redo": "Restore an undone point or edit.",
            "Delete": "Delete all selected strokes in one undoable edit.",
            "Clear region": "Clear the drawing; Undo restores strokes, including the current draft.",
            "Finish polyline": "End the current open polyline without another point.",
            "Freehand": "Drag to draw; release to finish a stroke.",
            "Polyline": "Click points; click the last point again to finish.",
            "Select / nodes": "Select strokes; drag a box for multiple selection.",
            "Toggle nodes": "Show or hide editable points on one selected stroke.",
            "Stroke eraser": "Hover to erase whole strokes. A pause or leaving the canvas ends an undoable sweep.",
            "Pan": "Drag with the left button to move the canvas freely.",
            "Fit": "Fit and recenter the crop without changing its coordinates.",
            "Focus canvas": "Slide the side panel out or back in.",
            "Cycle view": "Switch between overlay, image and traces.",
            "Tool menu": "Toggle tools and width controls at the pointer.",
            "Zoom in": "Zoom in around the center; the wheel follows the pointer.",
            "Zoom out": "Zoom out without changing saved coordinates.",
            "Smaller brush": "Reduce the active pen or eraser size.",
            "Larger brush": "Increase the pen, selected stroke width or eraser size.",
            "Select all": "Select every stroke in this region.",
            "Simplify": "Reduce selected nodes within the chosen pixel error; Undo restores them.",
            "Left click": "Press/hold at the pointer to click/draw on the canvas or this screen, including Q.",
            "Temporary pan": "Hold this key and drag to pan without changing tools.",
            "Smooth stroke": "Finish and beautify the active stroke, or adjust selected strokes. Undo restores the original.",
            "Adaptive smoothing": "Toggle jitter filtering for new freehand strokes.",
            "Show shortcuts": "Toggle the shortcut reference table.",
            **{f"Pan {direction}": "Move the canvas; hold to repeat." for direction in ("left", "right", "up", "down")},
            **{name: "Transform selected geometry around its center; width stays unchanged. Hold to repeat."
               for name in self.transform_buttons}
        }
        self.secondary_names = {}
        for name, key in {"Redo": "Ctrl+Y", "Delete": "Backspace", "Finish polyline": "Enter", "Zoom in": "Ctrl+="}.items():
            secondary = name + " (secondary)"
            self.secondary_names[name] = secondary
            action = next(action for action in self.shortcut_actions if action[0] == name)
            self.shortcut_actions.append((secondary, key, action[2], action[3]))
            self.shortcut_descriptions[secondary] = self.shortcut_descriptions[name]
        for name, default, callback, target in self.shortcut_actions:
            setting = f"svg/shortcuts/{name}"
            key = self.settings.value(setting, default)
            if not self.settings.contains(setting) and key in [editor.keySequence().toString() for editor in self.key_editors.values()]:
                key = ""
            editor = QKeySequenceEdit(QKeySequence(key))
            editor.setMaximumSequenceLength(1)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            editor.setMinimumWidth(0)
            row_layout.addWidget(editor, stretch=1)
            clear = QPushButton("Unbind")
            clear.setStyleSheet("padding: 4px; font-size: 11px;")
            clear.clicked.connect(editor.clear)
            row_layout.addWidget(clear)
            self.key_form.addRow(name, row)
            self.key_editors[name] = editor
        if self.binding_error():
            self.default_bindings()
            self.key_status.setText("Stored bindings conflicted. Defaults have been loaded.")
        self.install_bindings()

    def install_bindings(self):
        self.active_bindings = {name: editor.keySequence().toString() for name, editor in self.key_editors.items()}
        self.canvas.pan_binding = QKeySequence(self.active_bindings["Temporary pan"])
        self.install_shortcuts(self.shortcut_objects)
        self.shortcuts = list(self.shortcut_objects.values())
        if self.tool_menu is not None:
            self.install_shortcuts(self.menu_shortcuts, self.tool_menu)
        buttons = {"Save": self.save_button, "Undo": self.undo_button, "Redo": self.redo_button,
                   "Fit": self.fit_button, "Focus canvas": self.focus_button, "Pan": self.pan_button,
                   "Delete": self.delete_button, "Simplify": self.simplify_button, **self.transform_buttons}
        for name, button in [*buttons.items(), *getattr(self, "menu_buttons", {}).items()]:
            button.setToolTip(self.action_hint(name))
        self.tool_combo.setToolTip(self.action_hint(["Freehand", "Polyline", "Select / nodes", "Stroke eraser", "Pan"][self.tool_combo.currentIndex()]))
        self.width_input.setToolTip( "Width in crop pixels; while drawing, tap the width keys to taper the new part. In Select, replaces selected widths. " +
                                   self.binding_text("Smaller brush") + " / " + self.binding_text("Larger brush"))
        self.update_shortcut_guide()

    def install_shortcuts(self, shortcuts, popup=None):
        for name, default, callback, target in self.shortcut_actions:
            if callback is None:
                continue
            identifier = (name, 0)
            shortcut = shortcuts.get(identifier)
            if shortcut is None:
                shortcut = QShortcut(popup or target)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.setAutoRepeat(name.startswith("Pan ") or name.removesuffix(" (secondary)") in
                                      ("Zoom in", "Zoom out", "Smaller brush", "Larger brush", *self.transform_buttons))
                shortcut.activated.connect(lambda n=name, f=callback: self.invoke_action(n, f))
                shortcuts[identifier] = shortcut
            shortcut.setKey(QKeySequence(self.active_bindings[name]))

    def invoke_action(self, name, callback):
        if self.tool_menu is not None and self.tool_menu.isVisible() and name in (
                "Save", "Focus canvas", "Freehand", "Polyline", "Select / nodes", "Stroke eraser", "Pan"):
            self.tool_menu.close()
        callback()

    def binding_text(self, name):
        keys = [self.active_bindings[name]]
        secondary = self.secondary_names.get(name)
        if secondary:
            keys.append(self.active_bindings[secondary])
        return " / ".join(key for key in keys if key)

    def action_hint(self, name):
        return f"{name} ({self.binding_text(name) or 'Unbound'})\n{self.shortcut_descriptions[name]}"

    def binding_error(self):
        assigned = set()
        for editor in self.key_editors.values():
            key = editor.keySequence().toString()
            if key and key in assigned:
                return f"{key} is already used. Choose a different key."
            if key:
                assigned.add(key)
        return ""

    def apply_bindings(self):
        error = self.binding_error()
        if error:
            self.key_status.setText(error)
            return
        self.install_bindings()
        for name, editor in self.key_editors.items():
            self.settings.setValue(f"svg/shortcuts/{name}", editor.keySequence().toString())
        self.key_status.setText("Bindings saved. Left click works across this screen and Q; text and shortcut editors keep normal typing.")

    def update_shortcut_guide(self):
        groups = [
            ("Draw", "Choose Freehand or Polyline first. Draw with the left mouse button or hold Left click. "
             "While drawing, use Smaller/Larger brush to change the new part only. Hover an endpoint, then Shift + drag to continue.",
             ["Freehand", "Polyline", "Left click", "Smaller brush", "Larger brush", "Finish polyline", "Smooth stroke", "Adaptive smoothing"]),
            ("Edit", "Choose Select before editing existing strokes. Shift + click adds to selection. "
             "Width replaces the selected stroke width; Undo restores its previous profile. Eraser deletes on hover.",
             ["Select / nodes", "Select all", "Toggle nodes", "Stroke eraser", "Delete", "Undo", "Redo", "Simplify",
              "Shrink selection", "Grow selection", "Rotate left", "Rotate right"]),
            ("View", "Hold Temporary pan and drag to move the image. Zoom and Fit do not change saved coordinates. "
             "Focus hides the sidebar and footer.",
             ["Temporary pan", "Pan", "Pan left", "Pan right", "Pan up", "Pan down", "Zoom in", "Zoom out", "Fit",
              "Focus canvas", "Cycle view", "Tool menu", "Show shortcuts"]),
            ("Save", "Save finishes the current stroke and opens the next region. "
             "In Keys, Unbind removes an assignment after Apply; Cancel discards un-applied changes.",
             ["Save", "Clear region", *self.secondary_names.values()])
        ]
        for index, (title, workflow, actions) in enumerate(groups):
            if index == len(self.guide_labels):
                label = QLabel()
                label.setWordWrap(True)
                label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                label.setMargin(4)
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(label)
                self.shortcut_guide.addTab(scroll, title)
                self.guide_labels.append(label)
            entries = [f"<p><b>{escape(self.binding_text(name) or 'Unbound')}</b> — {escape(name)}<br>"
                       f"{escape(self.shortcut_descriptions[name])}</p>" for name in actions]
            self.guide_labels[index].setText(f"<p>{escape(workflow)}</p>" + "".join(entries))

    def cancel_bindings(self):
        for name, editor in self.key_editors.items():
            editor.setKeySequence(QKeySequence(self.active_bindings[name]))
        self.key_status.setText("Changes cancelled. Active bindings have been kept.")

    def default_bindings(self):
        for name, default, callback, target in self.shortcut_actions:
            self.key_editors[name].setKeySequence(QKeySequence(default))

    def reset_bindings(self):
        self.default_bindings()
        self.key_status.setText("Defaults are ready. Apply to save, or Cancel to keep the active bindings.")

    def animate_panel(self, width):
        if isinstance(width, (int, float)) and math.isfinite(width):
            self.sidebar.setMaximumWidth(max(0, round(width)))

    def toggle_focus(self, focused):
        self.canvas.finish_drawing()
        self.panel_animation.stop()
        start = self.sidebar.width() if self.sidebar.isVisible() else 0
        self.sidebar.setMinimumWidth(0)
        self.sidebar.setMaximumWidth(start)
        self.sidebar.show()
        self.help_label.setVisible(not focused)
        self.footer.setVisible(not focused)
        self.focus_button.setText("Show panels" if focused else "Focus")
        self.panel_animation.setStartValue(start)
        self.panel_animation.setEndValue(0 if focused else 300)
        self.panel_animation.start()
        self.canvas.setFocus()

    def finish_panel_animation(self):
        self.sidebar.setVisible(not self.focus_button.isChecked())
        self.sidebar.setMinimumWidth(280)
        self.sidebar.setMaximumWidth(340)

    def init_tool_menu(self):
        self.tool_menu = QDialog(self, Qt.WindowType.Popup)
        self.tool_menu.setStyleSheet("QPushButton { padding: 8px; } QPushButton:checked { border: 2px solid #00b7ff; }")
        layout = QVBoxLayout(self.tool_menu)
        grid = QGridLayout()
        self.menu_tools = []
        self.menu_buttons = {}
        for index, (label, row, column) in enumerate([
            ("Freehand", 0, 1), ("Polyline", 1, 2), ("Select", 2, 1), ("Eraser", 1, 0), ("Hand", 1, 1)
        ]):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, value=index: self.choose_menu_tool(value))
            self.menu_buttons[["Freehand", "Polyline", "Select / nodes", "Stroke eraser", "Pan"][index]] = button
            grid.addWidget(button, row, column)
            self.menu_tools.append(button)
        layout.addLayout(grid)
        size_row = QHBoxLayout()
        self.menu_size_label = QLabel("Width")
        self.menu_size = QDoubleSpinBox()
        self.menu_size.setSuffix(" px")
        self.menu_size.valueChanged.connect(self.change_menu_size)
        for name, label, direction in [("Smaller brush", "−", -1), ("Larger brush", "+", 1)]:
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setAutoRepeat(True)
            button.clicked.connect(lambda checked=False, step=direction: self.step_size(step))
            self.menu_buttons[name] = button
            size_row.addWidget(button)
        size_row.insertWidget(0, self.menu_size_label)
        size_row.insertWidget(2, self.menu_size)
        layout.addLayout(size_row)
        self.tool_menu.finished.connect(self.close_tool_menu)
        self.install_bindings()

    def sync_menu_size(self):
        if self.tool_menu is not None:
            control = self.eraser_input if self.canvas.tool == "erase" else self.width_input
            self.menu_size_label.setText("Eraser" if self.canvas.tool == "erase" else "Width")
            self.menu_size.blockSignals(True)
            self.menu_size.setRange(control.minimum(), control.maximum())
            self.menu_size.setSingleStep(control.singleStep())
            self.menu_size.setValue(control.value())
            self.menu_size.setToolTip(control.toolTip())
            self.menu_size.blockSignals(False)

    def change_menu_size(self, value):
        control = self.eraser_input if self.canvas.tool == "erase" else self.width_input
        control.setValue(value)

    def choose_menu_tool(self, index):
        self.tool_combo.setCurrentIndex(index)
        self.tool_menu.close()
        self.canvas.setFocus()

    def show_tool_menu(self):
        if not self.canvas.isEnabled():
            return
        if self.tool_menu is not None and self.tool_menu.isVisible():
            self.tool_menu.close()
            return
        if self.canvas.tool != "polyline":
            self.canvas.finish_drawing()
        if self.tool_menu is None:
            self.init_tool_menu()
        self.sync_menu_size()
        for index, button in enumerate(self.menu_tools):
            button.setChecked(index == self.tool_combo.currentIndex())
        self.tool_menu.adjustSize()
        position = QCursor.pos()
        bounds = self.screen().availableGeometry()
        self.tool_menu.move(max(bounds.left(), min(position.x() - self.tool_menu.width() // 2, bounds.right() - self.tool_menu.width())),
                            max(bounds.top(), min(position.y() - 60, bounds.bottom() - self.tool_menu.height())))
        self.tool_menu.show()
        self.set_busy(self.busy, self.job_blocking)
        self.tool_menu.setFocus()

    def close_tool_menu(self):
        self.set_busy(self.busy, self.job_blocking)
        self.canvas.setFocus()

    def change_tool(self, index):
        self.canvas.set_tool(["draw", "polyline", "edit", "erase", "pan"][index])
        self.pan_button.setChecked(index == 4)
        self.tool_combo.setToolTip(self.action_hint(["Freehand", "Polyline", "Select / nodes", "Stroke eraser", "Pan"][index]))
        self.help_label.setText([
            "Drag to trace. Hover an endpoint, then Shift + drag to continue that stroke.",
            "Click the last point again or double-click to finish. Click the green start marker to close. Shift + endpoint continues a stroke.",
            ("Drag white nodes to reshape. Double-click near a segment to add a node. Turn off Edit nodes to move strokes."
             if self.canvas.show_nodes else "Shift + click or drag a box to select several strokes. Drag to move; enable Edit nodes in Tools to reshape."),
            "Hover across strokes to erase, without clicking. Pause or leave the canvas to finish a sweep; Undo restores it.",
            f"Drag to move the canvas, even after Fit. Wheel to zoom; {self.binding_text('Temporary pan')} + drag temporarily pans."
        ][index])
        self.canvas.setFocus()
        self.sync_selection_width()
        self.sync_menu_size()

    def change_opacity(self, value):
        self.canvas.image_opacity = value / 100
        if self.canvas.image_item is not None and self.canvas.view_mode != "Traces only":
            self.canvas.image_item.setOpacity(value / 100)

    def change_width(self, value):
        if self.canvas.tool == "edit" and self.canvas.selected_ids:
            self.canvas.set_selected_width(value)
        else:
            self.canvas.set_brush_width(value)
        self.sync_menu_size()

    def step_size(self, direction):
        control = self.eraser_input if self.canvas.tool == "erase" else self.width_input
        control.stepBy(direction)

    def change_width_step(self, value):
        self.width_input.setSingleStep(value)
        self.sync_menu_size()

    def change_eraser_size(self, value):
        position = self.canvas.cursor_point
        self.canvas.update_cursor(None)
        self.canvas.eraser_size = value
        self.canvas.update_cursor(position)
        self.sync_menu_size()

    def change_smoothing(self, enabled):
        self.canvas.smoothing = enabled

    def change_nodes(self, enabled):
        self.canvas.show_nodes = enabled
        if enabled and self.tool_combo.currentIndex() != 2:
            self.tool_combo.setCurrentIndex(2)
        else:
            self.change_tool(self.tool_combo.currentIndex())

    def change_tolerance(self, value):
        self.canvas.tolerance = value

    def cycle_view(self):
        self.view_combo.setCurrentIndex((self.view_combo.currentIndex() + 1) % 3)

    def select_strokes(self):
        if not self.loading:
            self.canvas.finish_drawing()
            self.canvas.select_strokes({item.data(Qt.ItemDataRole.UserRole) for item in self.stroke_list.selectedItems()})

    def select_all(self):
        self.tool_combo.setCurrentIndex(2)
        self.canvas.select_all()

    def on_selection_changed(self, index):
        self.update_canvas_status()
        self.sync_selection_width()
        if self.tabs.currentIndex() != 1:
            self.stroke_list_dirty = True
            return
        self.stroke_list.blockSignals(True)
        for row in range(self.stroke_list.count()):
            item = self.stroke_list.item(row)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in self.canvas.selected_ids)
        self.stroke_list.blockSignals(False)

    def sync_selection_width(self):
        strokes = [self.canvas.strokes[self.canvas.stroke_indices[key]] for key in self.canvas.selected_ids]
        widths = {width for stroke in strokes for width in stroke.get("widths", (stroke.get("width", 1),))}
        value = next(iter(widths)) if self.canvas.tool == "edit" and len(widths) == 1 else self.canvas.brush_width
        self.width_input.blockSignals(True)
        self.width_input.setValue(value)
        self.width_input.setPrefix("Mixed " if self.canvas.tool == "edit" and len(widths) > 1 else "")
        self.width_input.blockSignals(False)
        self.sync_menu_size()

    def update_canvas_status(self):
        image = self.canvas.image
        self.count_label.setText(f"{image.width()}×{image.height()} · {self.canvas.transform().m11():.0%} · "
                                 f"{len(self.canvas.selected_ids)}/{len(self.canvas.strokes)} selected" if image else "")

    def show_status(self, text):
        self.status_label.setText(text)
        if self.focus_button.isChecked():
            self.help_label.setText(text)
            self.help_label.show()

    def refresh_stroke_list(self, *args):
        if self.tabs.currentIndex() != 1 or not self.stroke_list_dirty:
            return
        self.stroke_list.blockSignals(True)
        while self.stroke_list.count() > len(self.canvas.strokes):
            self.stroke_list.takeItem(self.stroke_list.count() - 1)
        for index, stroke in enumerate(self.canvas.strokes):
            tool_name = "Freehand" if stroke["tool"] == "draw" else "Polyline"
            if index == self.stroke_list.count():
                self.stroke_list.addItem(QListWidgetItem())
            item = self.stroke_list.item(index)
            item.setText(f"{index + 1}. {tool_name} | {len(stroke['points'])} nodes")
            item.setData(Qt.ItemDataRole.UserRole, stroke["id"])
            item.setSelected(stroke["id"] in self.canvas.selected_ids)
        self.stroke_list.blockSignals(False)
        self.stroke_list_dirty = False

    def on_strokes_changed(self):
        self.stroke_list_dirty = True
        QTimer.singleShot(0, self.refresh_stroke_list)
        self.update_canvas_status()
        if not self.loading and self.document:
            self.mark_dirty()

    def mark_dirty(self, *args):
        if not self.loading and self.document:
            self.dirty = True
            key = self.binding_text("Save") or "Save & Next"
            self.status_label.setText(f"Unsaved changes. {key} saves this region.")

    def snapshot(self):
        if self.document is None:
            raise RuntimeError("No tracing document is currently loaded.")
        document = {key: copy.deepcopy(value) for key, value in self.document.items() if key != "strokes"}
        document["strokes"] = [stroke_data(stroke) for stroke in self.canvas.strokes]
        return document

    def run_job(self, function, callback, message, block=False, quiet=False):
        if self.worker:
            return False
        if not quiet:
            self.status_label.setText(message)
        self.job_blocking = block
        self.set_busy(True, block)
        worker = TaskWorker(function, self)
        self.worker = worker

        def finish():
            self.worker = None
            self.job_blocking = False
            self.saving = False
            self.set_busy(False, block)
            if worker.error:
                suffix = " Retry Save to finish the pending export." if self.service.export_pending is not None else " Your local drawing has been kept."
                self.show_status(worker.error + suffix)
                if worker.conflict_task is not None:
                    self.resolve_local_conflict(worker.conflict_task)
                elif not quiet:
                    QMessageBox.warning(self, "Tracing", worker.error)
            else:
                callback(worker.result)
            worker.deleteLater()
            if self.save_requested:
                self.save_requested = False
                QTimer.singleShot(0, self.save_and_submit)

        worker.finished.connect(finish)
        worker.start()
        return True

    def set_busy(self, busy, block):
        self.busy = busy
        self.job_blocking = block if busy else False
        pending = self.service.export_pending is not None
        blocked = (busy and block) or pending
        for widget in (self.back_button, self.refresh_button, self.next_button, self.region_list, self.history_list):
            widget.setEnabled(not busy and not pending)
        self.save_button.setEnabled(not busy and self.current_task is not None)
        self.save_button.setText("Saving…" if self.saving else "Retry Save" if pending else "Save && Next")
        for widget in (self.canvas, self.tool_combo, self.undo_button, self.redo_button, self.pan_button,
                       self.delete_button, self.simplify_button, self.stroke_list, self.tabs, self.width_input, self.focus_button):
            widget.setEnabled(not blocked)
        for shortcut in self.shortcuts:
            shortcut.setEnabled(not blocked and not (self.tool_menu is not None and self.tool_menu.isVisible()))
        self.shortcut_objects[("Save", 0)].setEnabled(not (busy and block) and not (self.tool_menu is not None and self.tool_menu.isVisible()))
        if blocked and self.tool_menu is not None and self.tool_menu.isVisible():
            self.tool_menu.close()

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
        self.history_list.clear()
        done = 0
        for task in self.service.tasks:
            st = self.service.statuses.get(task["task_id"], {})
            status = st.get("status", "available")
            if status == "done":
                label = f"[pending] {task['label']}" if st.get("pending_sync") else task["label"]
            else:
                label = f"[{status}] {task['label']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, task)
            if status == "done":
                self.history_list.addItem(item)
                done += 1
            else:
                self.region_list.addItem(item)
            if self.current_task and task["task_id"] == self.current_task["task_id"]:
                item.setSelected(True)
        pending_count = len(self.service.pending_sync)
        pending_text = f" ({pending_count} pending sync)" if pending_count else ""
        self.progress_label.setText(f"Done: {done} / {len(self.service.tasks)}{pending_text}")
        self.input_errors = list(self.service.strategy.errors)
        self.errors_button.setVisible(bool(self.input_errors))
        suffix = f" · {len(self.input_errors)} input issues" if self.input_errors else ""
        mode = "" if self.service.online else " · Offline (local-first)"
        self.status_label.setText(f"{len(self.service.tasks)} regions{suffix}{mode}")

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
                self.context_label.setPixmap(QPixmap.fromImage(result["preview"]).scaled(
                    240, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
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
        self.open_region(item.data(Qt.ItemDataRole.UserRole))

    def resolve_local_conflict(self, task):
        box = QMessageBox(self)
        box.setWindowTitle("Saved result changed")
        box.setText("This region has a newer server result. Choose which drawing to open. Keeping local edits will replace the server result only when you save.")
        local = box.addButton("Keep local edits", QMessageBox.ButtonRole.AcceptRole)
        server = box.addButton("Use server result", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() in (local, server):
            use_local = box.clickedButton() is local
            QTimer.singleShot(0, lambda: self.open_region(task, use_local))

    def open_region(self, task, use_local=None):
        if self.worker or self.service.export_pending is not None:
            return
        if (self.current_task and self.current_task["task_id"] == task["task_id"] and use_local is None
                and self.service.statuses.get(task["task_id"], {}).get("status") != "expired"):
            return
        self.canvas.finish_drawing()
        previous = self.current_task
        document = self.snapshot() if previous else None
        status = self.service.statuses.get(task["task_id"], {}).get("status")

        def switch():
            if previous:
                self.service.remember(previous, document)
            result = self.service.load_region(task, reopen=status == "done", use_local=use_local)
            try:
                if previous and previous["task_id"] != task["task_id"]:
                    self.service.release(previous)
            except Exception:
                self.service.release(task)
                raise
            return result

        self.run_job(switch, self.set_current_task, "Opening region…", block=True)

    def save_and_submit(self):
        if not self.current_task:
            return
        if self.worker:
            if not self.job_blocking:
                self.save_requested = True
                self.status_label.setText("Save queued until the current server request finishes.")
            return
        self.canvas.finish_drawing()
        document = self.snapshot()
        if self.crop_image is None:
            QMessageBox.warning(self, "Tracing", "No crop image is loaded.")
            return
        task = self.current_task

        def saved(result):
            self.dirty = False
            self.set_current_task(result["next"])
            if result["error"]:
                self.status_label.setText("Saved successfully. Could not open the next region: " + result["error"])
            if result.get("conflict"):
                self.resolve_local_conflict(result["conflict"])

        def submit():
            conflict = None
            try:
                self.service.submit(task, document)
            except LocalEditsConflict as error:
                conflict = error.task
            try:
                return {"next": self.service.next_region(refresh=False), "error": "", "conflict": conflict}
            except LocalEditsConflict as error:
                return {"next": None, "error": str(error), "conflict": conflict or error.task}
            except Exception as error:
                return {"next": None, "error": str(error), "conflict": conflict}

        self.saving = True
        self.run_job(submit, saved, "Saving JSON and SVG…", block=True)

    def renew_lock(self):
        if self.worker or self.service.export_pending is not None:
            return
        task = self.current_task
        if task is None:
            self.run_job(self.service.refresh, self.update_regions, "Checking connection…", quiet=True)
            return
        self.run_job(lambda: self.service.sync_lock(task), lambda result: self.update_regions(),
                     "Renewing task lock…", quiet=True)

    def stop_and_back(self):
        if self.worker or self.service.export_pending is not None:
            return
        self.canvas.finish_drawing()
        task = self.current_task
        document = self.snapshot() if task else None

        def release():
            if task:
                self.service.remember(task, document)
                self.service.release(task)
            self.service.close()

        def finished(result):
            self.sync_timer.stop()
            self.set_current_task(None)
            self.on_back_callback()

        self.run_job(release, finished, "Releasing region…", block=True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.close_target is None:
            self.close_target = self.window()
        self.canvas.fit_image()

    def hideEvent(self, event):
        self.pointer_click(False)
        self.click_keys.clear()
        super().hideEvent(event)

    def owns_widget(self, widget):
        while isinstance(widget, QWidget):
            if widget is self:
                return True
            widget = widget.parentWidget()
        return False

    def pointer_click(self, pressed, modifiers=Qt.KeyboardModifier.NoModifier):
        target = QApplication.widgetAt(QCursor.pos()) if pressed else self.click_target
        if target is None or sip.isdeleted(target):
            self.click_target = None
            return
        if pressed and (not self.owns_widget(target) or not target.isEnabled()):
            return
        self.click_target = target if pressed else None
        if target is self.canvas.viewport() or self.canvas.isAncestorOf(target):
            self.canvas.keyboard_click(pressed, modifiers)
            return
        self.dispatching_click = True
        try:
            event = QMouseEvent(QEvent.Type.MouseButtonPress if pressed else QEvent.Type.MouseButtonRelease,
                                QPointF(target.mapFromGlobal(QCursor.pos())), QPointF(QCursor.pos()),
                                Qt.MouseButton.LeftButton,
                                Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton, modifiers)
            QApplication.sendEvent(target, event)
        finally:
            self.dispatching_click = False

    def eventFilter(self, watched, event):
        if self.dispatching_click or not self.isVisible():
            return False
        if event.type() == QEvent.Type.ApplicationDeactivate:
            self.pointer_click(False)
            self.click_keys.clear()
        if event.type() == QEvent.Type.MouseMove and self.click_target is not None and self.click_target is not self.canvas.viewport():
            target = self.click_target
            if not sip.isdeleted(target):
                self.dispatching_click = True
                try:
                    QApplication.sendEvent(target, QMouseEvent(QEvent.Type.MouseMove,
                        QPointF(target.mapFromGlobal(event.globalPosition().toPoint())), event.globalPosition(),
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, event.modifiers()))
                finally:
                    self.dispatching_click = False
                return True
        if event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if event.type() == QEvent.Type.KeyRelease and event.key() in self.click_keys:
                if not event.isAutoRepeat():
                    self.click_keys.discard(event.key())
                    self.pointer_click(False, event.modifiers())
                return True
            if not self.owns_widget(watched):
                return False
            focus = watched
            while focus is not None and focus is not self:
                if isinstance(focus, QKeySequenceEdit):
                    return False
                focus = focus.parentWidget()
            if isinstance(watched, (QTextEdit, QPlainTextEdit)) or (
                    isinstance(watched, QLineEdit) and not isinstance(watched.parentWidget(), QAbstractSpinBox)):
                return False
            key = QKeySequence(event.keyCombination()).toString()
            binding = self.active_bindings.get("Left click", "")
            sequence = QKeySequence(binding)
            shifted = bool(sequence.count() and event.key() == sequence[0].key() and
                           event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier == sequence[0].keyboardModifiers() and
                           key not in self.active_bindings.values())
            if binding and (key == binding or shifted):
                if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self.click_keys.add(event.key())
                    self.pointer_click(True, event.modifiers())
                event.accept()
                return True
        if watched is self.close_target and event.type() == QEvent.Type.Close:
            if self.worker:
                self.status_label.setText("Please wait for the current save or server request, then close again.")
                event.ignore()
                return True
            if self.service.export_pending is not None:
                self.status_label.setText("Retry Save to finish the pending export before closing.")
                event.ignore()
                return True
            self.dirty = False
            self.sync_timer.stop()
            self.service.close()
        return super().eventFilter(watched, event)
