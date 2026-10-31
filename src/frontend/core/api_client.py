"""Shared client for non-SVG task screens.

Network calls are synchronous by design; call them from a worker, not the Qt UI
thread. Session objects are thread-local to prevent concurrent requests sharing
one mutable requests.Session.
"""
import os
import threading
from typing import Dict, List, Optional, Union

import requests
from dotenv import load_dotenv

load_dotenv()


class OfflineError(RuntimeError):
    pass


class TaskConflictError(RuntimeError):
    pass


class APIClient:
    def __init__(self):
        self.user_id = None
        self.base_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
        self._local = threading.local()
        self.online = True
        self.last_error: Optional[str] = None

    @property
    def session(self):
        """Existing clients may use api.session; each thread has its own."""
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

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
                    except (ValueError, AttributeError):
                        detail = res.text
                    raise TaskConflictError(detail or "Task is locked by another annotator or its state changed.")
                res.raise_for_status()
                data = res.json()
                self.online = True
                self.last_error = None
                return data
        except TaskConflictError:
            raise
        except requests.HTTPError as error:
            # A HTTP 500 is a real backend error, not an offline socket.
            self.online = True
            self.last_error = str(error)
            raise RuntimeError(f"Backend HTTP error at {path}: {error}") from error
        except (requests.RequestException, ValueError) as error:
            self.online = False
            self.last_error = str(error)
            raise OfflineError(str(error)) from error

    def check_status(self, task_ids: List[str], raise_on_error: bool = False, include_results: bool = True) -> List[Dict]:
        if not task_ids:
            return []
        tasks = []
        try:
            for offset in range(0, len(task_ids), 200):
                data = self.post("/tasks", {"task_ids": task_ids[offset:offset + 200]})
                for item in data.get("tasks", []):
                    if not include_results:
                        item.pop("result", None)
                    tasks.append(item)
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

    def renew_tasks(self, task_ids: List[str], task_type: str = "obb") -> List[str]:
        """Legacy backend: renew a lease by claiming it again as its current owner.

        This uses the existing /claim endpoint, NOT /tasks/renew.  The server
        atomically refuses another owner's live lock, so renewal never steals it.
        Old servers require one request per task; batching is not available.
        """
        renewed = []
        for task_id in dict.fromkeys(task_ids):
            try:
                self.claim_task(task_id, task_type, raise_on_error=True)
                renewed.append(task_id)
            except TaskConflictError:
                # Lost lock (or server marked Done): refresh will resolve status.
                continue
        return renewed

    def unclaim_task(self, task_id: str, raise_on_error: bool = False) -> Optional[Dict]:
        try:
            return self.post(f"/tasks/{task_id}/unclaim", {"user_id": self.user_id})
        except (OfflineError, TaskConflictError):
            if raise_on_error:
                raise
            return None
