import os
from pathlib import Path
from core.api_client import APIClient

class DataManager:
    def __init__(self, api_client: APIClient):
        self.api = api_client
        self.task_configs = {}
        self.cached_tasks = []

    def set_task_config(self, task_type: str, input_dir: str, output_dir: str):
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        self.task_configs[task_type] = {
            "input": Path(input_dir),
            "output": out_path
        }
        self.scan_local_folder(task_type)

    def scan_local_folder(self, task_type: str):
        if task_type not in self.task_configs:
            return
        
        config = self.task_configs[task_type]
        input_dir = config["input"]
        output_dir = config["output"]

        if not input_dir.exists():
            self.cached_tasks = []
            return

        local_tasks = []
        for video_dir in input_dir.iterdir():
            if not video_dir.is_dir():
                continue
            
            video_name = video_dir.name
            for img_file in video_dir.glob("*"):
                if img_file.suffix.lower() not in ['.png', '.jpg', '.jpeg']:
                    continue
                
                frame = img_file.stem
                task_id = f"{video_name}_{frame}_{task_type}"
                
                local_tasks.append({
                    "task_id": task_id,
                    "video_name": video_name,
                    "frame": frame,
                    "image_path": str(img_file),
                    "output_path": str(output_dir / video_name / f"{frame}.json")
                })
        self.cached_tasks = local_tasks

    def get_free_task(self, task_type: str):
        if not self.cached_tasks:
            return None

        task_ids = [t["task_id"] for t in self.cached_tasks]
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

        free_tasks = [t for t in self.cached_tasks if t["task_id"] not in unavailable_ids and os.path.exists(t["image_path"])]

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
        if not self.cached_tasks:
            return []
        
        task_ids = [t["task_id"] for t in self.cached_tasks]
        task_statuses = self.api.check_status(task_ids)
        done_ids = {t["task_id"] for t in task_statuses if t.get("status") == "done"}

        return [t for t in self.cached_tasks if t["task_id"] in done_ids and os.path.exists(t["image_path"])]

    def get_processing_tasks(self, task_type: str):
        if not self.cached_tasks:
            return []
        
        task_ids = [t["task_id"] for t in self.cached_tasks]
        task_statuses = self.api.check_status(task_ids)
        my_user = self.api.user_id
        
        processing_tasks = []
        status_dict = {t["task_id"]: t for t in task_statuses}

        for t in self.cached_tasks:
            tid = t["task_id"]
            if tid in status_dict and os.path.exists(t["image_path"]):
                st = status_dict[tid]
                if st.get("status") == "processing" and st.get("locked_by") == my_user:
                    task_copy = t.copy()
                    task_copy["existing_result"] = st.get("result")
                    processing_tasks.append(task_copy)
                    
        return processing_tasks