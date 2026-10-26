import json, os
from typing import List, Dict
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QPushButton, QMessageBox, QListWidgetItem
from PyQt6.QtCore import Qt
from core.api_client import APIClient
from ui.components.canvas import ImageCanvas

class OBBTaskScreen(QWidget):
    def __init__(self, api_client: APIClient, data_manager, on_back_callback):
        super().__init__()
        self.api = api_client
        self.data_manager = data_manager
        self.on_back_callback = on_back_callback
        self.current_task = None
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)
        
        self.canvas = ImageCanvas()
        layout.addWidget(self.canvas, stretch=4)
        
        right_panel = QVBoxLayout()
        
        btn_back = QPushButton("Back to Menu")
        btn_back.clicked.connect(self.on_back_callback)
        right_panel.addWidget(btn_back)
        
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self.on_history_clicked)
        right_panel.addWidget(self.history_list)
        
        layout.addLayout(right_panel, stretch=1)

    def start_task(self):
        self.history_list.clear()
        self.load_next_task()

    def keyPressEvent(self, event):
        if not self.canvas.is_editable:
            return

        if event.key() == Qt.Key.Key_Escape:
            if self.current_task:
                self.canvas.load_image(self.current_task['image_path'])
        
        elif event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            coords = self.canvas.get_coordinates()
            if len(coords) == 4:
                self.save_and_submit(coords)

    def save_and_submit(self, coords):
        task_id = self.current_task['task_id']
        output_path = self.current_task['output_path']
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump({"board_obb": coords}, f)
        
        self.api.submit_task(task_id, coords)
        
        item = QListWidgetItem(task_id)
        item.setData(Qt.ItemDataRole.UserRole, self.current_task)
        self.history_list.addItem(item)
        
        self.load_next_task()

    def load_next_task(self):
        task = self.data_manager.get_free_task(task_type="obb")
        
        if task:
            self.current_task = task
            self.canvas.load_image(task['image_path'])
        else:
            self.current_task = None
            self.canvas.show_empty_state()

    def on_history_clicked(self, item):
        task_id = item.text()
        task_info = item.data(Qt.ItemDataRole.UserRole)
        
        old_data = self.api.reclaim_task(task_id)
        if old_data:
            self.current_task = task_info
            self.canvas.load_image(self.current_task['image_path'])
            
            existing_result = old_data.get("existing_result")
            if existing_result and "points" in existing_result:
                self.canvas.load_existing_points(existing_result["points"])
            
            self.history_list.takeItem(self.history_list.row(item))
        else:
            QMessageBox.warning(self, "Error", "Cannot reclaim this task.")