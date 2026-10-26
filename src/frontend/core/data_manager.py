import os
from pathlib import Path
from core.api_client import APIClient

class DataManager:
    def __init__(self, api_client: APIClient):
        self.api = api_client
        self.task_configs = {}

    def set_task_config(self, task_type: str, input_dir: str, output_dir: str):
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        self.task_configs[task_type] = {
            "input": Path(input_dir),
            "output": out_path
        }

    def get_free_task(self, task_type: str):
        if task_type not in self.task_configs:
            return None
        
        config = self.task_configs[task_type]
        input_dir = config["input"]
        output_dir = config["output"]

        if not input_dir.exists():
            return None

        local_tasks = []
        for file_path in input_dir.rglob("*"):
            if file_path.suffix.lower() not in ['.png', '.jpg', '.jpeg']:
                continue
            
            rel_path = file_path.relative_to(input_dir)
            base_name = str(rel_path.with_suffix('')).replace(os.sep, '_')
            task_id = f"{base_name}_{task_type}"
            
            local_tasks.append({
                "task_id": task_id,
                "image_path": str(file_path),
                "output_path": str(output_dir / rel_path.with_suffix('.json'))
            })

        if not local_tasks:
            return None

        task_ids = [t["task_id"] for t in local_tasks]
        busy_tasks = self.api.check_status(task_ids)
        busy_ids = {t["task_id"] for t in busy_tasks if t["status"] in ["processing", "done"]}

        free_tasks = [t for t in local_tasks if t["task_id"] not in busy_ids]

        for task in free_tasks:
            if self.api.claim_task(task["task_id"], task_type):
                return task

        return None