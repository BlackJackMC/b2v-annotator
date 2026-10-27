import copy
import json
from pathlib import Path
import requests
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QThread
from PyQt6.QtGui import QImage, QPainter, QPen, QColor
from tasks.svg.document import new_annotation, compact_annotation, validate_annotation, write_export


class TaskConflict(RuntimeError):
    pass


class TaskWorker(QThread):
    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.function()
        except Exception as error:
            self.error = str(error)


class TraceService:
    def __init__(self, api, strategy, config):
        self.base_url = api.base_url
        self.user_id = api.user_id
        self.strategy = strategy
        self.config = config
        self.tasks = []
        self.statuses = {}
        self.pending = {}
        self.saved_results = {}

    def post(self, path, payload):
        try:
            response = requests.post(f"{self.base_url}{path}", json=payload, timeout=(3, 20))
            if response.status_code == 409:
                raise TaskConflict("This region is locked by another annotator or its status has changed.")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(f"Could not sync tracing with the server: {error}") from error

    def check_status(self, task_ids):
        result = {}
        for offset in range(0, len(task_ids), 200):
            data = self.post("/tasks", {"task_ids": task_ids[offset:offset + 200]})
            for task in data["tasks"]:
                result[task["task_id"]] = task
        return result

    def refresh(self, scan=False):
        if scan:
            self.tasks = self.strategy.scan_local_folder(self.config)
        self.statuses = self.check_status([task["task_id"] for task in self.tasks])
        for status in self.statuses.values():
            status.pop("result", None)
        return {"tasks": self.tasks, "statuses": self.statuses, "errors": list(self.strategy.errors)}

    def claim(self, task, reopen=False):
        action = "reclaim" if reopen else "claim"
        result = self.post(f"/tasks/{task['task_id']}/{action}", {"user_id": self.user_id, "task_type": "svg"})
        self.statuses[task["task_id"]] = {"status": "processing", "locked_by": self.user_id, "has_result": bool(result.get("existing_result"))}
        return result

    def release(self, task):
        if task:
            result = self.post(f"/tasks/{task['task_id']}/unclaim", {"user_id": self.user_id})
            previous = self.statuses.pop(task["task_id"], {})
            if previous.get("has_result") or "reverted to done" in result.get("message", ""):
                self.statuses[task["task_id"]] = {"status": "done", "locked_by": None}

    def verify_source(self, task):
        info = self.strategy.image_info(Path(task["image_path"]))
        if info["sha256"] != task["source"]["image_sha256"]:
            raise ValueError("The source image changed. Refresh regions before tracing.")
        data = json.loads(Path(task["aabb_path"]).read_bytes())
        index = task["source"]["region_index"]
        if index >= len(data["boxes"]) or [float(data["boxes"][index][k]) for k in ("x", "y", "w", "h")] != task["source"]["region_xywh"]:
            raise ValueError("The AABB region changed. Refresh regions before tracing.")

    def load_region(self, task, reopen=False):
        self.verify_source(task)
        original = QImage(task["image_path"])
        if original.isNull():
            raise ValueError("Cannot decode the source image.")
        x, y, width, height = task["source"]["crop_xywh"]
        crop = original.copy(x, y, width, height)
        result = self.claim(task, reopen)
        previous = result.get("existing_result")
        if previous:
            previous = compact_annotation(previous, task, self.user_id)
        cached = self.pending.get(task["task_id"])
        if cached and cached["base"] != previous:
            raise TaskConflict("The saved result changed. Your unsaved session edits are still in memory; save or discard them before reopening.")
        document = copy.deepcopy(previous) if previous else new_annotation(task, self.user_id)
        warning = ""
        if cached:
            document = copy.deepcopy(cached["document"])
            warning = "Restored unsaved edits from this session."
        self.saved_results[task["task_id"]] = copy.deepcopy(previous)
        preview = original.scaledToWidth(240)
        painter = QPainter(preview)
        painter.setPen(QPen(QColor("#ff28b5"), 2))
        scale = preview.width() / original.width()
        painter.drawRect(round(x * scale), round(y * scale), round(width * scale), round(height * scale))
        painter.end()
        return {"task": task, "document": document, "image": crop, "preview": preview, "warning": warning}

    def next_region(self):
        self.refresh()
        for task in self.tasks:
            status = self.statuses.get(task["task_id"], {})
            if status.get("status") == "done":
                continue
            if status.get("status") == "processing" and status.get("locked_by") != self.user_id:
                continue
            try:
                return self.load_region(task)
            except TaskConflict:
                continue
        return None

    def remember(self, task, document):
        self.pending[task["task_id"]] = {
            "base": copy.deepcopy(self.saved_results.get(task["task_id"])),
            "document": copy.deepcopy(document)
        }

    def submit(self, task, document, image):
        self.verify_source(task)
        result = compact_annotation(document, task, self.user_id)
        validate_annotation(result, task, complete=True)
        self.remember(task, result)
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise OSError("Could not encode the region image.")
        buffer.close()
        status = self.check_status([task["task_id"]]).get(task["task_id"], {})
        if status.get("status") == "done":
            if status.get("result") != result:
                raise TaskConflict("A different result is already saved. Your drawing is still open; refresh the region status before reopening it.")
        else:
            self.claim(task)
            try:
                self.post(f"/tasks/{task['task_id']}", {"user_id": self.user_id, "result": result})
            except RuntimeError:
                status = self.check_status([task["task_id"]]).get(task["task_id"], {})
                if status.get("status") != "done" or status.get("result") != result:
                    raise
        write_export(task, result, bytes(encoded))
        self.pending.pop(task["task_id"], None)
        self.saved_results[task["task_id"]] = copy.deepcopy(result)
        self.statuses[task["task_id"]] = {"status": "done", "result": result}
        return result
