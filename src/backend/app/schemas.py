from pydantic import BaseModel
from typing import List, Dict, Any

class CheckTasksRequest(BaseModel):
    task_ids: List[str]

class ClaimTaskRequest(BaseModel):
    user_id: str
    task_type: str

class UnclaimTaskRequest(BaseModel):
    user_id: str

class SubmitTaskRequest(BaseModel):
    user_id: str
    result: Dict[str, Any]