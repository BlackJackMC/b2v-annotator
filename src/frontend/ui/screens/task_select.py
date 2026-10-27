from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt

class TaskSelectScreen(QWidget):
    def __init__(self, on_task_selected_callback):
        super().__init__()
        self.on_task_selected = on_task_selected_callback
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Select Task Type")
        title.setStyleSheet("font-size: 20px; font-weight: bold; margin-bottom: 20px;")
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_obb = QPushButton("Detect Whiteboard (OBB)")
        btn_obb.setFixedSize(300, 50)
        btn_obb.clicked.connect(lambda: self.on_task_selected("obb"))
        layout.addWidget(btn_obb, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_aabb = QPushButton("Detect Content (AABB)")
        btn_aabb.setFixedSize(300, 50)
        btn_aabb.clicked.connect(lambda: self.on_task_selected("aabb"))
        layout.addWidget(btn_aabb, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_svg = QPushButton("Trace SVG")
        btn_svg.setFixedSize(300, 50)
        btn_svg.clicked.connect(lambda: self.on_task_selected("svg"))
        layout.addWidget(btn_svg, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_classify = QPushButton("Classify Content")
        btn_classify.setFixedSize(300, 50)
        btn_classify.clicked.connect(lambda: self.on_task_selected("classify"))
        layout.addWidget(btn_classify, alignment=Qt.AlignmentFlag.AlignCenter)
