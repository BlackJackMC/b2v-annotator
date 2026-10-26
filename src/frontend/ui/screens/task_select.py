from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel, QMessageBox
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

        tasks_wip = ["Detect Content (AABB)", "Classify Content", "Trace SVG"]
        for name in tasks_wip:
            btn = QPushButton(name)
            btn.setFixedSize(300, 50)
            btn.clicked.connect(self.show_wip_message)
            layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def show_wip_message(self):
        QMessageBox.information(self, "Info", "This feature is currently under development.")