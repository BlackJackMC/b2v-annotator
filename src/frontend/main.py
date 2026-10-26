import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QFileDialog
from core.api_client import APIClient
from core.data_manager import DataManager
from ui.screens.login import LoginScreen
from ui.screens.task_select import TaskSelectScreen

from tasks.obb.screen import OBBTaskScreen
from tasks.aabb.screen import AABBTaskScreen
from tasks.svg.screen import SVGTaskScreen

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Board2Vec Annotator")
        self.resize(1200, 800)

        self.api = APIClient()
        self.data_manager = DataManager(self.api)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        self.status_bar = self.statusBar()

        self.login_screen = LoginScreen(self.goto_task_select)
        self.task_select_screen = TaskSelectScreen(self.goto_workspace)
        self.workspace_screens = {}

        self.stacked_widget.addWidget(self.login_screen)
        self.stacked_widget.addWidget(self.task_select_screen)

        self.stacked_widget.setCurrentWidget(self.login_screen)

    def goto_task_select(self, username):
        self.api.set_user(username)
        self.status_bar.clearMessage()
        self.stacked_widget.setCurrentWidget(self.task_select_screen)

    def goto_menu(self):
        self.status_bar.clearMessage()
        self.stacked_widget.setCurrentWidget(self.task_select_screen)

    def goto_workspace(self, task_type):
        if task_type not in self.data_manager.task_configs:
            required_folders = self.data_manager.get_required_folders(task_type)
            if not required_folders:
                print(f"No strategy defined for {task_type}")
                return
                
            config_dict = {}
            for key, display_name in required_folders.items():
                folder = QFileDialog.getExistingDirectory(self, f"Select {display_name} for {task_type.upper()}")
                if not folder:
                    return
                config_dict[key] = folder
                
            if task_type == "svg":
                self.data_manager.task_configs[task_type] = config_dict
            else:
                self.data_manager.set_task_config(task_type, config_dict)

        if task_type not in self.workspace_screens:
            if task_type == "obb":
                self.workspace_screens[task_type] = OBBTaskScreen(self.api, self.data_manager, self.goto_menu)
            elif task_type == "aabb":
                self.workspace_screens[task_type] = AABBTaskScreen(self.api, self.data_manager, self.goto_menu)
            elif task_type == "svg":
                self.workspace_screens[task_type] = SVGTaskScreen(self.api, self.data_manager, self.goto_menu)
            
            if task_type in self.workspace_screens:
                self.stacked_widget.addWidget(self.workspace_screens[task_type])
        
        if task_type in self.workspace_screens:
            screen = self.workspace_screens[task_type]
            screen.start_task()
            self.stacked_widget.setCurrentWidget(screen)
            
            if task_type == "obb":
                self.status_bar.showMessage("Shortcuts: [Click] Add Point | [Esc] Reset | [Ctrl + S] Save & Next | [Middle/Right Drag] Pan")
            elif task_type == "aabb":
                self.status_bar.showMessage("Shortcuts: [Left Drag] Draw Box | [Left Click] Select Box | [Left Drag on Edge] Move edge | [Delete] Delete selected | [Ctrl + S] Save & Next | [Middle Drag] Pan")
            elif task_type == "svg":
                self.status_bar.showMessage("[P] Freehand | [L] Polyline | [V] Edit | [Enter] Finish | [Ctrl+Z] Undo | [Ctrl+Shift+Z] Redo | [Ctrl+S] Save | [F] Fit | [Tab] View | [Middle/Right Drag] Pan")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    qss_path = Path(__file__).resolve().parent / "ui" / "styles.qss"
    if qss_path.exists():
        print(f"Loaded stylesheet: {qss_path}")
        with open(qss_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    else:
        print(f"QSS file not found at {qss_path}, using default theme.")
        app.setStyle("Fusion")
        
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
