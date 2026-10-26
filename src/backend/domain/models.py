from typing import Optional, Dict, Any
from datetime import datetime
from beanie import Document
from pymongo import IndexModel, ASCENDING

class AnnotationTask(Document):
    task_id: str
    task_type: str 
    status: str = "processing"
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None 

    class Settings:
        name = "annotation_tasks"
        indexes = [
            IndexModel([("task_id", ASCENDING)], unique=True),
            IndexModel([("task_type", ASCENDING)])
        ]