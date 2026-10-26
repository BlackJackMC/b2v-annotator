from datetime import datetime, timedelta, timezone
from pymongo.errors import DuplicateKeyError
from fastapi import HTTPException, status
from typing import List
from beanie import UpdateResponse

from backend.domain.models import AnnotationTask
from backend.infra.config import settings
from backend.app.schemas import ClaimTaskRequest, SubmitTaskRequest

class TaskUseCase:
    @staticmethod
    async def check_status(task_ids: List[str]):
        from beanie.operators import In
        tasks = await AnnotationTask.find(In(AnnotationTask.task_id, task_ids)).to_list()
        return [{"task_id": t.task_id, "status": t.status, "locked_by": t.locked_by} for t in tasks]

    @staticmethod
    async def claim_task(task_id: str, request: ClaimTaskRequest):
        now = datetime.now(timezone.utc)
        
        try:
            new_task = AnnotationTask(
                task_id=task_id,
                task_type=request.task_type,
                status="processing",
                locked_by=request.user_id,
                locked_at=now
            )
            await new_task.insert()
            return {"message": "Task claimed successfully"}
            
        except DuplicateKeyError:
            timeout_threshold = now - timedelta(minutes=settings.LOCK_TIMEOUT_MINUTES)
            
            updated_task = await AnnotationTask.find_one(
                AnnotationTask.task_id == task_id,
                AnnotationTask.status == "processing",
                {"locked_at": {"$lt": timeout_threshold}} 
            ).update(
                {"$set": {"locked_by": request.user_id, "locked_at": now}},
                response_type=UpdateResponse.NEW_DOCUMENT
            )
            
            if not updated_task:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Task is currently locked by someone else or already done."
                )
                
            return {"message": "Task claim stolen (previous lock timed out)"}

    @staticmethod
    async def reclaim_task(task_id: str, request: ClaimTaskRequest):
        now = datetime.now(timezone.utc)
        
        task = await AnnotationTask.find_one(
            AnnotationTask.task_id == task_id,
            AnnotationTask.status == "done"
        ).update(
            {"$set": {"status": "processing", "locked_by": request.user_id, "locked_at": now}},
            response_type=UpdateResponse.NEW_DOCUMENT
        )
        
        if not task:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task is not in 'done' state or is currently being edited by someone else."
            )
            
        return {
            "message": "Task reclaimed for editing",
            "existing_result": task.result
        }

    @staticmethod
    async def unclaim_task(task_id: str, user_id: str):
        task = await AnnotationTask.find_one(
            AnnotationTask.task_id == task_id,
            AnnotationTask.locked_by == user_id,
            AnnotationTask.status == "processing"
        )
        
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="Task not found or you don't own the lock."
            )
            
        if task.result:
            await task.update({"$set": {"status": "done", "locked_by": None, "locked_at": None}})
            return {"message": "Edit cancelled, task reverted to done"}
        else:
            await task.delete()
            return {"message": "Task unclaimed and removed from DB"}

    @staticmethod
    async def submit_task(task_id: str, request: SubmitTaskRequest):
        task = await AnnotationTask.find_one(
            AnnotationTask.task_id == task_id,
            AnnotationTask.locked_by == request.user_id,
            AnnotationTask.status == "processing"
        )
        if not task:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="You don't hold the lock for this task."
            )
        await task.update({"$set": {"status": "done", "result": request.result, "locked_at": None}})
        return {"message": "Task completed successfully"}