import os
from pathlib import Path
from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent

class Settings(BaseSettings):
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    DB_NAME: str = os.getenv("DB_NAME", "board2vec_db")
    LOCK_TIMEOUT_MINUTES: int = int(os.getenv("LOCK_TIMEOUT_MINUTES", 1))

    class Config:
        env_file = str(ROOT_DIR / ".env")

settings = Settings()