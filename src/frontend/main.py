import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QFileDialog
from core.api_client import APIClient
from core.data_manager import DataManager
from ui.screens.login import LoginScreen
from ui.screens.task_select import TaskSelectScreen
from ui.screens.obb_task import OBBTaskScreen

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Board2Vec Annotator")
        self.resize(1200, 800)

        self.api = APIClient()
        self.data_manager = DataManager(self.api)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        self.login_screen = LoginScreen(self.goto_task_select)
        self.task_select_screen = TaskSelectScreen(self.goto_workspace)
        self.workspace_screens = {}

        self.stacked_widget.addWidget(self.login_screen)
        self.stacked_widget.addWidget(self.task_select_screen)

        self.stacked_widget.setCurrentWidget(self.login_screen)

    def goto_task_select(self, username):
        self.api.set_user(username)
        self.stacked_widget.setCurrentWidget(self.task_select_screen)

    def goto_menu(self):
        self.stacked_widget.setCurrentWidget(self.task_select_screen)

    def goto_workspace(self, task_type):
        if task_type not in self.data_manager.task_configs:
            input_dir = QFileDialog.getExistingDirectory(self, f"Select Input Folder for {task_type.upper()}")
            if not input_dir:
                return
            
            output_dir = QFileDialog.getExistingDirectory(self, f"Select Output Folder for {task_type.upper()}")
            if not output_dir:
                return
                
            self.data_manager.set_task_config(task_type, input_dir, output_dir)

        if task_type not in self.workspace_screens:
            if task_type == "obb":
                self.workspace_screens[task_type] = OBBTaskScreen(self.api, self.data_manager, self.goto_menu)
                self.stacked_widget.addWidget(self.workspace_screens[task_type])
        
        screen = self.workspace_screens[task_type]
        screen.start_task()
        self.stacked_widget.setCurrentWidget(screen)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())