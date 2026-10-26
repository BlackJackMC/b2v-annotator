from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox
from PyQt6.QtCore import Qt

class LoginScreen(QWidget):
    def __init__(self, on_login_callback):
        super().__init__()
        self.on_login = on_login_callback
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Board2Vec Annotator")
        title.setStyleSheet("font-size: 24px; font-weight: bold; margin-bottom: 20px;")
        
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter Username...")
        self.username_input.setFixedWidth(400)
        self.username_input.returnPressed.connect(self.handle_login)

        btn_login = QPushButton("Login")
        btn_login.setFixedWidth(400)
        btn_login.clicked.connect(self.handle_login)

        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.username_input, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(btn_login, alignment=Qt.AlignmentFlag.AlignCenter)

    def handle_login(self):
        username = self.username_input.text().strip()
        if not username:
            QMessageBox.warning(self, "Error", "Please enter a username.")
            return
        
        self.on_login(username)