import os
from pathlib import Path
from typing import Dict
from core.api_client import APIClient
from tasks.obb.strategy import OBBStrategy
from tasks.aabb.strategy import AABBStrategy

class DataManager:
    def __init__(self, api_client: APIClient):
        self.api = api_client
        self.task_configs = {}
        self.cached_tasks = {}
        
        self.strategies = {
            "obb": OBBStrategy("obb"),
            "aabb": AABBStrategy("aabb")
        }

    def get_required_folders(self, task_type: str) -> Dict[str, str]:
        strategy = self.strategies.get(task_type)
        return strategy.get_required_folders() if strategy else {}

    def set_task_config(self, task_type: str, config_dict: dict):
        for key, path_str in config_dict.items():
            if "output" in key.lower():
                Path(path_str).mkdir(parents=True, exist_ok=True)
                
        self.task_configs[task_type] = config_dict
        self.scan_local_folder(task_type)

    def scan_local_folder(self, task_type: str):
        strategy = self.strategies.get(task_type)
        config = self.task_configs.get(task_type)
        
        if not strategy or not config:
            self.cached_tasks[task_type] = []
            return
            
        self.cached_tasks[task_type] = strategy.scan_local_folder(config)

    def get_free_task(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return None

        task_ids = [t["task_id"] for t in tasks]
        tasks_status = self.api.check_status(task_ids)
        
        my_user = self.api.user_id
        unavailable_ids = set()
        existing_results = {}
        
        for t in tasks_status:
            if t.get("status") == "done":
                unavailable_ids.add(t["task_id"])
            elif t.get("status") == "processing" and t.get("locked_by") != my_user:
                unavailable_ids.add(t["task_id"])
            elif t.get("status") == "processing" and t.get("locked_by") == my_user:
                existing_results[t["task_id"]] = t.get("result")

        free_tasks = [t for t in tasks if t["task_id"] not in unavailable_ids and os.path.exists(t["image_path"])]

        for task in free_tasks:
            claim_res = self.api.claim_task(task["task_id"], task_type)
            if claim_res:
                if task["task_id"] in existing_results:
                    task["existing_result"] = existing_results[task["task_id"]]
                elif claim_res.get("existing_result"):
                    task["existing_result"] = claim_res.get("existing_result")
                return task
        return None

    def get_done_tasks(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return []
        
        task_ids = [t["task_id"] for t in tasks]
        task_statuses = self.api.check_status(task_ids)
        done_ids = {t["task_id"] for t in task_statuses if t.get("status") == "done"}

        return [t for t in tasks if t["task_id"] in done_ids and os.path.exists(t["image_path"])]

    def get_processing_tasks(self, task_type: str):
        tasks = self.cached_tasks.get(task_type, [])
        if not tasks:
            return []
        
        task_ids = [t["task_id"] for t in tasks]
        task_statuses = self.api.check_status(task_ids)
        my_user = self.api.user_id
        
        processing_tasks = []
        status_dict = {t["task_id"]: t for t in task_statuses}

        for t in tasks:
            tid = t["task_id"]
            if tid in status_dict and os.path.exists(t["image_path"]):
                st = status_dict[tid]
                if st.get("status") == "processing" and st.get("locked_by") == my_user:
                    task_copy = t.copy()
                    task_copy["existing_result"] = st.get("result")
                    processing_tasks.append(task_copy)
                    
        return processing_tasks