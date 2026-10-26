from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from backend.infra.config import settings
from backend.domain.models import AnnotationTask

async def init_db():
    client = AsyncIOMotorClient(settings.MONGO_URI)
    db = client[settings.DB_NAME]
    
    await init_beanie(database=db, document_models=[AnnotationTask])
