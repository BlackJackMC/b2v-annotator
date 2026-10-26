from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.infra.database import init_db
from backend.app.routes import router as tasks_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Server starting up")
    await init_db()
    yield
    print("Server shutting down")

app = FastAPI(
    title="Board2Vec Annotation API", 
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks_router)

@app.get("/")
async def root():
    return {"status": "Board2Vec Backend is running"}