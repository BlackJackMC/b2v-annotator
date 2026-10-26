import os
import requests
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

class APIClient:
    def __init__(self):
        self.user_id = None
        self.base_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip('/')

    def set_user(self, username: str):
        self.user_id = username

    def check_status(self, task_ids: List[str]) -> List[Dict]:
        res = requests.post(f"{self.base_url}/tasks", json={"task_ids": task_ids})
        return res.json().get("tasks", []) if res.status_code == 200 else []

    def claim_task(self, task_id: str, task_type: str) -> Dict:
        res = requests.post(f"{self.base_url}/tasks/{task_id}/claim", json={"user_id": self.user_id, "task_type": task_type})
        return res.json() if res.status_code == 200 else None

    def submit_task(self, task_id: str, points: List[Dict]) -> bool:
        res = requests.post(f"{self.base_url}/tasks/{task_id}", json={"user_id": self.user_id, "result": {"points": points}})
        return res.status_code == 200

    def reclaim_task(self, task_id: str) -> Dict:
        res = requests.post(f"{self.base_url}/tasks/{task_id}/reclaim", json={"user_id": self.user_id, "task_type": "obb"})
        return res.json() if res.status_code == 200 else None