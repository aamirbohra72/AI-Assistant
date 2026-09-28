import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app import redis_store
from app.config import get_settings
from app.db import SessionLocal
from app.logging_setup import log_event
from app.models import Candidate, Interview, InterviewStatus, Report
from app.schemas import Rubric
from app.services import twilio_client
from app.services.interviewer import InterviewContext

logger = logging.getLogger(__name__)

# Interviews with a live media stream in this process; they finalize themselves on hang-up.
ACTIVE_SESSIONS: set[int] = set()

_UNANSWERED = {
    "busy": InterviewStatus.NO_ANSWER,
    "no-answer": InterviewStatus.NO_ANSWER,
    "canceled": InterviewStatus.NO_ANSWER,
    "failed": InterviewStatus.FAILED,
}


class InterviewNotFound(Exception):
    pass


class InvalidInterviewState(Exception):
    pass


async def _set_status(interview_id: int, new: InterviewStatus, *, only_from: set[InterviewStatus], ended: bool = False) -> bool:
    values: dict = {"status": new}
    if ended:
        values["ended_at"] = func.now()
    async with SessionLocal() as db:
        result = await db.execute(
            update(Interview)
            .where(Interview.id == interview_id, Interview.status.in_(only_from))
            .values(**values)
            .returning(Interview.id)
        )
        changed = result.scalar_one_or_none() is not None
        await db.commit()
    return changed


async def start_interview_call(interview_id: int) -> str:
    async with SessionLocal() as db:
        result = await db.execute(
            update(Interview)
            .where(Interview.id == interview_id, Interview.status == InterviewStatus.SCHEDULED)
            .values(status=InterviewStatus.CALLING)
            .returning(Interview.candidate_id)
        )
        candidate_id = result.scalar_one_or_none()
        if candidate_id is None:
            await db.rollback()
            current = await db.scalar(select(Interview.status).where(Interview.id == interview_id))
            if current is None:
                raise InterviewNotFound(interview_id)
            raise InvalidInterviewState(f"interview is '{current}', expected 'scheduled'")
        phone = await db.scalar(select(Candidate.phone).where(Candidate.id == candidate_id))
        await db.commit()

    try:
        call_sid = await twilio_client.create_call(phone, interview_id)
    except Exception:
        await _set_status(interview_id, InterviewStatus.FAILED, only_from={InterviewStatus.CALLING}, ended=True)
        raise

    async with SessionLocal() as db:
        await db.execute(update(Interview).where(Interview.id == interview_id).values(twilio_call_sid=call_sid))
        await db.commit()
    log_event(logger, "outbound call placed", call_sid=call_sid)
    return call_sid


async def mark_in_progress(interview_id: int, call_sid: str | None) -> bool:
    async with SessionLocal() as db:
        result = await db.execute(
            update(Interview)
            .where(
                Interview.id == interview_id,
                Interview.status.in_([InterviewStatus.CALLING, InterviewStatus.IN_PROGRESS]),
            )
            .values(
                status=InterviewStatus.IN_PROGRESS,
                started_at=func.coalesce(Interview.started_at, func.now()),
                twilio_call_sid=func.coalesce(Interview.twilio_call_sid, call_sid),
            )
            .returning(Interview.id)
        )
        ok = result.scalar_one_or_none() is not None
        await db.commit()
    return ok


async def load_interview_context(interview_id: int) -> InterviewContext | None:
    async with SessionLocal() as db:
        interview = await db.scalar(
            select(Interview)
            .options(selectinload(Interview.candidate), selectinload(Interview.job_role))
            .where(Interview.id == interview_id)
        )
    if interview is None:
        return None
    return InterviewContext(
        interview_id=interview_id,
        candidate_name=interview.candidate.name,
        job_title=interview.job_role.title,
        jd_text=interview.job_role.jd_text,
        resume_json=interview.candidate.resume_json or {},
        rubric=Rubric.model_validate(interview.job_role.rubric_json),
    )


async def finalize_interview(interview_id: int) -> bool:
    """Idempotent: first caller moves in_progress -> completed and queues scoring."""
    done = await _set_status(
        interview_id, InterviewStatus.COMPLETED, only_from={InterviewStatus.IN_PROGRESS}, ended=True
    )
    if done:
        await redis_store.enqueue_scoring(interview_id)
        await redis_store.delete_state(interview_id)
        log_event(logger, "interview completed; scoring queued")
    return done


async def handle_call_status(interview_id: int, call_status: str) -> None:
    log_event(logger, "twilio call status", call_status=call_status)
    if call_status in _UNANSWERED:
        await _set_status(interview_id, _UNANSWERED[call_status], only_from={InterviewStatus.CALLING}, ended=True)
    elif call_status == "completed":
        # Answered but the media stream never connected.
        await _set_status(interview_id, InterviewStatus.FAILED, only_from={InterviewStatus.CALLING}, ended=True)
        if interview_id not in ACTIVE_SESSIONS:
            await finalize_interview(interview_id)


async def allow_stream_reconnect(interview_id: int) -> bool:
    async with SessionLocal() as db:
        status = await db.scalar(select(Interview.status).where(Interview.id == interview_id))
    if status != InterviewStatus.IN_PROGRESS:
        return False
    attempts = await redis_store.incr_reconnects(interview_id)
    allowed = attempts <= get_settings().MAX_STREAM_RECONNECTS
    log_event(logger, "media stream reconnect requested", attempt=attempts, allowed=allowed)
    return allowed


async def requeue_pending_work() -> None:
    """Recover jobs lost from Redis or interrupted by a restart."""
    async with SessionLocal() as db:
        scheduled = (
            await db.execute(select(Interview.id, Interview.scheduled_at).where(Interview.status == InterviewStatus.SCHEDULED))
        ).all()
        unscored = (
            await db.scalars(
                select(Interview.id)
                .outerjoin(Report, Report.interview_id == Interview.id)
                .where(
                    Interview.status.in_(
                        [InterviewStatus.COMPLETED, InterviewStatus.SCORING, InterviewStatus.SCORING_FAILED]
                    ),
                    Report.id.is_(None),
                )
            )
        ).all()
    for interview_id, scheduled_at in scheduled:
        await redis_store.enqueue_call(interview_id, scheduled_at or datetime.now(UTC), only_if_missing=True)
    for interview_id in unscored:
        await redis_store.enqueue_scoring(interview_id)
    if scheduled or unscored:
        log_event(logger, "requeued pending work", scheduled=len(scheduled), unscored=len(unscored))
