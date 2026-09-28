"""Background worker: dispatches due calls from the Redis schedule and runs post-call scoring.

Runs inside the web process by default (RUN_WORKER_IN_WEB=true) or standalone: `python -m app.worker`.
"""

import asyncio
import logging
import signal
import time
from contextlib import suppress

from app import redis_store
from app.config import get_settings
from app.logging_setup import interview_id_var, log_event, setup_logging
from app.services.interviews import InvalidInterviewState, requeue_pending_work, start_interview_call
from app.services.scoring import score_interview

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_SCORING = 3


async def _place_call(interview_id: int) -> None:
    token = interview_id_var.set(interview_id)
    try:
        await start_interview_call(interview_id)
    except InvalidInterviewState as exc:
        log_event(logger, "skipping scheduled call", logging.WARNING, reason=str(exc))
    except Exception:
        logger.exception("failed to place scheduled call")
    finally:
        interview_id_var.reset(token)


async def _score(interview_id: int, semaphore: asyncio.Semaphore) -> None:
    token = interview_id_var.set(interview_id)
    try:
        async with semaphore:
            await score_interview(interview_id)
    except Exception:
        logger.exception("scoring job crashed")
    finally:
        interview_id_var.reset(token)


async def run_worker(stop: asyncio.Event) -> None:
    settings = get_settings()
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SCORING)
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        await requeue_pending_work()
    except Exception:
        logger.exception("startup requeue failed")
    log_event(logger, "worker started", poll_seconds=settings.WORKER_POLL_SECONDS)

    while not stop.is_set():
        try:
            for interview_id in await redis_store.claim_due_calls(time.time()):
                spawn(_place_call(interview_id))
            for interview_id in await redis_store.claim_due_scoring_retries(time.time()):
                spawn(_score(interview_id, semaphore))
            while (interview_id := await redis_store.pop_scoring()) is not None:
                spawn(_score(interview_id, semaphore))
        except Exception:
            logger.exception("worker iteration failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.WORKER_POLL_SECONDS)

    # Unfinished scoring is recovered by requeue_pending_work on next start.
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _main() -> None:
    setup_logging(get_settings().LOG_LEVEL)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_worker(stop)
    finally:
        await redis_store.close()


if __name__ == "__main__":
    asyncio.run(_main())
