from datetime import datetime, timedelta, timezone
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument
from fastapi import HTTPException, status
from typing import List
from beanie.operators import In

from backend.domain.models import AnnotationTask
from backend.infra.config import settings
from backend.app.schemas import ClaimTaskRequest, SubmitTaskRequest

class TaskUseCase:
    @staticmethod
    async def check_status(task_ids: List[str]):
        tasks = await AnnotationTask.find(
            In(AnnotationTask.task_id, task_ids)
        ).to_list()
        
        now = datetime.now(timezone.utc)
        timeout_threshold = now - timedelta(minutes=settings.LOCK_TIMEOUT_MINUTES)
        
        result = []
        for t in tasks:
            task_dict = t.model_dump()
            if t.status == "processing" and t.locked_at:
                locked_at = t.locked_at
                if locked_at.tzinfo is None:
                    locked_at = locked_at.replace(tzinfo=timezone.utc)
                if locked_at < timeout_threshold:
                    task_dict["status"] = "expired" 
            result.append(task_dict)
            
        return result

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
            
            collection = AnnotationTask.get_motor_collection()
            updated_doc = await collection.find_one_and_update(
                {
                    "task_id": task_id,
                    "status": "processing",
                    "$or": [
                        {"locked_at": {"$lt": timeout_threshold}},
                        {"locked_by": request.user_id} 
                    ]
                },
                {"$set": {"locked_by": request.user_id, "locked_at": now}},
                return_document=ReturnDocument.AFTER
            )
            
            if not updated_doc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Task is currently locked by someone else or already done."
                )
                
            return {
                "message": "Task claim stolen or resumed",
                "existing_result": updated_doc.get("result") 
            }

    @staticmethod
    async def submit_task(task_id: str, request: SubmitTaskRequest):
        collection = AnnotationTask.get_motor_collection()
        updated_doc = await collection.find_one_and_update(
            {
                "task_id": task_id,
                "locked_by": request.user_id,
                "status": "processing",
                "locked_at": {"$gte": datetime.now(timezone.utc) - timedelta(minutes=settings.LOCK_TIMEOUT_MINUTES)}
            },
            {"$set": {"status": "done", "result": request.result, "locked_by": None, "locked_at": None}},
            return_document=ReturnDocument.AFTER
        )
        
        if not updated_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found or lock expired."
            )
        return {"message": "Task completed successfully"}

    @staticmethod
    async def reclaim_task(task_id: str, request: ClaimTaskRequest):
        now = datetime.now(timezone.utc)
        
        collection = AnnotationTask.get_motor_collection()
        updated_doc = await collection.find_one_and_update(
            {
                "task_id": task_id,
                "status": "done"
            },
            {"$set": {"status": "processing", "locked_by": request.user_id, "locked_at": now}},
            return_document=ReturnDocument.AFTER
        )
        
        if not updated_doc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task is not in 'done' state or is currently being edited by someone else."
            )
            
        return {
            "message": "Task reclaimed for editing",
            "existing_result": updated_doc.get("result") 
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