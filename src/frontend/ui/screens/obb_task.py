import json, os
from typing import List, Dict
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QPushButton, QMessageBox, QListWidgetItem, QLabel
from PyQt6.QtCore import Qt, QTimer
from core.api_client import APIClient
from ui.components.canvas import ImageCanvas

class OBBTaskScreen(QWidget):
    def __init__(self, api_client: APIClient, data_manager, on_back_callback):
        super().__init__()
        self.api = api_client
        self.data_manager = data_manager
        self.on_back_callback = on_back_callback
        self.current_task = None
        
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_data_timer)
        
        self.init_ui()

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
        self.sync_timer.stop()
        self.on_back_callback()

    def start_task(self):
        self.manual_refresh()
        self.load_next_task()
        self.sync_timer.start(10000)

    def manual_refresh(self):
        self.data_manager.scan_local_folder("obb")
        self.sync_data()
        
        if self.current_task is None:
            self.load_next_task()

    def sync_data_timer(self):
        self.manual_refresh()

    def sync_data(self):
        self.history_list.clear()
        done_tasks = self.data_manager.get_done_tasks("obb")
        for task in done_tasks:
            item = QListWidgetItem(task["task_id"])
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.history_list.addItem(item)
            
        self.processing_list.clear()
        processing_tasks = self.data_manager.get_processing_tasks("obb")
        for task in processing_tasks:
            item = QListWidgetItem(task["task_id"])
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.processing_list.addItem(item)

    def keyPressEvent(self, event):
        if not self.canvas.is_editable:
            return

        if event.key() == Qt.Key.Key_Escape:
            if self.current_task:
                self.set_current_task(self.current_task)
        
        elif event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            coords = self.canvas.get_coordinates()
            if len(coords) == 4:
                self.save_and_submit(coords)

    def save_and_submit(self, coords):
        if not self.current_task:
            return
            
        image_path = self.current_task['image_path']
        
        if not os.path.exists(image_path):
            QMessageBox.warning(self, "Error", "Local image file is missing! Action aborted.")
            self.manual_refresh()
            self.load_next_task()
            return
            
        task_id = self.current_task['task_id']
        output_path = self.current_task['output_path']
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump({"board_obb": coords}, f)
        
        self.api.submit_task(task_id, coords)
        self.sync_data()
        self.load_next_task()

    def load_next_task(self):
        task = self.data_manager.get_free_task(task_type="obb")
        self.set_current_task(task)

    def set_current_task(self, task):
        if task:
            self.current_task = task
            self.canvas.load_image(task['image_path'])
            
            existing_result = task.get("existing_result")
            if existing_result and "points" in existing_result:
                self.canvas.load_existing_points(existing_result["points"])
        else:
            self.current_task = None
            self.canvas.show_empty_state()

    def on_processing_clicked(self, item):
        task_id = item.text()
        task_info = item.data(Qt.ItemDataRole.UserRole)
        
        if not os.path.exists(task_info['image_path']):
            QMessageBox.warning(self, "Error", "Local image file missing! It may have been deleted.")
            self.manual_refresh()
            return
        
        claim_res = self.api.claim_task(task_id, "obb")
        if claim_res:
            if claim_res.get("existing_result"):
                task_info["existing_result"] = claim_res.get("existing_result")
            self.set_current_task(task_info)
        else:
            QMessageBox.warning(self, "Error", "Cannot lock this task.")

    def on_history_clicked(self, item):
        task_id = item.text()
        task_info = item.data(Qt.ItemDataRole.UserRole)
        
        if not os.path.exists(task_info['image_path']):
            QMessageBox.warning(self, "Error", "Local image file missing! It may have been deleted.")
            self.manual_refresh()
            return
        
        old_data = self.api.reclaim_task(task_id)
        if old_data:
            task_info["existing_result"] = old_data.get("existing_result")
            self.set_current_task(task_info)
            self.sync_data()
        else:
            QMessageBox.warning(self, "Error", "Cannot reclaim this task.")