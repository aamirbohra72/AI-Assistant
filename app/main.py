import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app import redis_store
from app.config import get_settings
from app.db import engine
from app.http_client import close_http_client
from app.logging_setup import setup_logging
from app.routers import candidates, interviews, job_roles, twilio
from app.services.vad import load_vad_session
from app.worker import run_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    await asyncio.to_thread(load_vad_session, settings.VAD_MODEL_PATH)

    stop = asyncio.Event()
    worker_task = asyncio.create_task(run_worker(stop)) if settings.RUN_WORKER_IN_WEB else None
    try:
        yield
    finally:
        stop.set()
        if worker_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(worker_task, timeout=30)
        await close_http_client()
        await redis_store.close()
        await engine.dispose()


app = FastAPI(title="AI Voice Interviewer", version="1.0.0", lifespan=lifespan)
app.include_router(candidates.router)
app.include_router(job_roles.router)
app.include_router(interviews.router)
app.include_router(twilio.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}
