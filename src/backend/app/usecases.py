from datetime import datetime, timedelta, timezone
from typing import List

from beanie.operators import In
from fastapi import HTTPException, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.app.schemas import ClaimTaskRequest, SubmitTaskRequest
from backend.domain.models import AnnotationTask
from backend.infra.config import settings


class TaskUseCase:
    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _threshold(now):
        return now - timedelta(minutes=settings.LOCK_TIMEOUT_MINUTES)

    @staticmethod
    def _collection():
        # Beanie in the user's environment exposes get_motor_collection().
        return AnnotationTask.get_motor_collection()

    @staticmethod
    async def check_status(task_ids: List[str]):
        if not task_ids:
            return []
        rows = await AnnotationTask.find(In(AnnotationTask.task_id, task_ids)).to_list()
        threshold = TaskUseCase._threshold(TaskUseCase._now())
        result = []
        for row in rows:
            item = row.model_dump()
            stamp = row.locked_at
            if stamp is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if row.status == "processing" and (stamp is None or stamp < threshold):
                item["status"] = "expired"
            result.append(item)
        return result

    @staticmethod
    async def _acquire(task_id: str, request: ClaimTaskRequest):
        """Obtain exactly one lock. A live lock by another user never changes."""
        now = TaskUseCase._now()
        collection = TaskUseCase._collection()
        try:
            await AnnotationTask(
                task_id=task_id,
                task_type=request.task_type,
                status="processing",
                locked_by=request.user_id,
                locked_at=now,
            ).insert()
            return {"message": "Task claimed", "existing_result": None}
        except DuplicateKeyError:
            pass

        doc = await collection.find_one_and_update(
            {
                "task_id": task_id,
                "task_type": request.task_type,
                "$or": [
                    {"status": "done", "locked_by": None},
                    {"status": "processing", "locked_by": request.user_id},
                    {
                        "status": "processing",
                        "$or": [
                            {"locked_at": {"$lt": TaskUseCase._threshold(now)}},
                            {"locked_at": None},
                        ],
                    },
                ],
            },
            {"$set": {
                "status": "processing",
                "locked_by": request.user_id,
                "locked_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This task is claimed by another annotator or cannot be opened. Choose another task.",
            )
        return {"message": "Task claimed", "existing_result": doc.get("result")}

    @staticmethod
    async def claim_task(task_id: str, request: ClaimTaskRequest):
        return await TaskUseCase._acquire(task_id, request)

    @staticmethod
    async def reclaim_task(task_id: str, request: ClaimTaskRequest):
        return await TaskUseCase._acquire(task_id, request)

    @staticmethod
    async def submit_task(task_id: str, request: SubmitTaskRequest):
        now = TaskUseCase._now()
        doc = await TaskUseCase._collection().find_one_and_update(
            {
                "task_id": task_id,
                "status": "processing",
                "locked_by": request.user_id,
                "locked_at": {"$gte": TaskUseCase._threshold(now)},
            },
            {"$set": {
                "status": "done",
                "result": request.result,
                "locked_by": None,
                "locked_at": None,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot save: your lock expired or another annotator owns this task.",
            )
        return {"message": "Task saved successfully"}

    @staticmethod
    async def unclaim_task(task_id: str, user_id: str):
        now = TaskUseCase._now()
        collection = TaskUseCase._collection()
        lease = {
            "task_id": task_id,
            "status": "processing",
            "locked_by": user_id,
            "locked_at": {"$gte": TaskUseCase._threshold(now)},
        }
        doc = await collection.find_one_and_update(
            {**lease, "result": {"$exists": True, "$ne": None}},
            {"$set": {"status": "done", "locked_by": None, "locked_at": None}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return {"message": "Task released, saved result preserved"}

        doc = await collection.find_one_and_delete(
            {**lease, "$or": [{"result": None}, {"result": {"$exists": False}}]},
        )
        if doc is not None:
            return {"message": "Unfinished task released"}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot release: your task lock expired or ownership changed.",
        )
