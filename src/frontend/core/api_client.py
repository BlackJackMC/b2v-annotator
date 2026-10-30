import os
import requests
from typing import List, Dict, Optional, Union
from dotenv import load_dotenv

load_dotenv()


class OfflineError(RuntimeError):
    pass


class TaskConflictError(RuntimeError):
    pass


class APIClient:
    def __init__(self):
        self.user_id = None
        self.base_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip('/')
        self.session = requests.Session()
        self.online = True
        self.last_error: Optional[str] = None

    def set_user(self, username: str):
        self.user_id = username

    def post(self, path: str, payload: Dict, timeout=(3, 15)) -> Dict:
        try:
            with self.session.post(f"{self.base_url}{path}", json=payload, timeout=timeout) as res:
                if res.status_code in (404, 409):
                    self.online = True
                    self.last_error = None
                    try:
                        detail = res.json().get("detail", "")
                    except ValueError:
                        detail = res.text
                    raise TaskConflictError(detail or "Task is locked by someone else or its status changed.")
                res.raise_for_status()
                self.online = True
                self.last_error = None
                return res.json()
        except TaskConflictError:
            raise
        except (requests.RequestException, ValueError) as error:
            self.online = False
            self.last_error = str(error)
            raise OfflineError(str(error)) from error

    def check_status(self, task_ids: List[str], raise_on_error: bool = False) -> List[Dict]:
        if not task_ids:
            return []
        tasks = []
        try:
            for offset in range(0, len(task_ids), 200):
                chunk = task_ids[offset:offset + 200]
                data = self.post("/tasks", {"task_ids": chunk})
                tasks.extend(data.get("tasks", []))
            return tasks
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return []

    def claim_task(self, task_id: str, task_type: str, raise_on_error: bool = False) -> Optional[Dict]:
        try:
            return self.post(f"/tasks/{task_id}/claim", {"user_id": self.user_id, "task_type": task_type})
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return None

    def submit_task(self, task_id: str, points: Union[List[Dict], Dict], raise_on_error: bool = False) -> bool:
        result = points if isinstance(points, dict) else {"points": points}
        try:
            self.post(f"/tasks/{task_id}", {"user_id": self.user_id, "result": result})
            return True
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return False

    def reclaim_task(self, task_id: str, task_type: str = "obb", raise_on_error: bool = False) -> Optional[Dict]:
        try:
            return self.post(f"/tasks/{task_id}/reclaim", {"user_id": self.user_id, "task_type": task_type})
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return None

    def unclaim_task(self, task_id: str, raise_on_error: bool = False) -> Optional[Dict]:
        try:
            return self.post(f"/tasks/{task_id}/unclaim", {"user_id": self.user_id})
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return None
