from fastapi import APIRouter
from backend.app.schemas import (
    CheckTasksRequest, ClaimTaskRequest, 
    UnclaimTaskRequest, SubmitTaskRequest
)
from backend.app.usecases import TaskUseCase

router = APIRouter(prefix="/tasks", tags=["Tasks"])

@router.post("")
async def check_status(request: CheckTasksRequest):
    tasks = await TaskUseCase.check_status(request.task_ids)
    return {"tasks": tasks}

@router.post("/{task_id}/claim")
async def claim_task(task_id: str, request: ClaimTaskRequest):
    return await TaskUseCase.claim_task(task_id, request)

@router.post("/{task_id}/unclaim")
async def unclaim_task(task_id: str, request: UnclaimTaskRequest):
    return await TaskUseCase.unclaim_task(task_id, request.user_id)

@router.post("/{task_id}")
async def submit_task(task_id: str, request: SubmitTaskRequest):
    return await TaskUseCase.submit_task(task_id, request)