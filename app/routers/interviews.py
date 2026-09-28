import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis_store
from app.db import get_db
from app.deps import require_api_key, require_internal_token
from app.logging_setup import interview_id_var
from app.models import Candidate, Interview, InterviewStatus, JobRole, Report, Transcript
from app.schemas import InterviewOut, ReportOut, ScheduleRequest, TranscriptTurnOut
from app.services.interviews import InterviewNotFound, InvalidInterviewState, start_interview_call

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/interviews", tags=["interviews"])

_REPORT_PENDING = {
    InterviewStatus.SCHEDULED,
    InterviewStatus.CALLING,
    InterviewStatus.IN_PROGRESS,
    InterviewStatus.COMPLETED,
    InterviewStatus.SCORING,
}


async def _get_interview(db: AsyncSession, interview_id: int) -> Interview:
    interview = await db.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    return interview


@router.post(
    "/schedule",
    response_model=InterviewOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def schedule_interview(body: ScheduleRequest, db: AsyncSession = Depends(get_db)) -> Interview:
    if await db.get(Candidate, body.candidate_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    if await db.get(JobRole, body.job_role_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job role not found")

    when = body.scheduled_at or datetime.now(UTC)
    interview = Interview(
        candidate_id=body.candidate_id, job_role_id=body.job_role_id, status=InterviewStatus.SCHEDULED, scheduled_at=when
    )
    db.add(interview)
    await db.commit()
    await db.refresh(interview)

    try:
        await redis_store.enqueue_call(interview.id, when)
    except Exception as exc:
        logger.exception("failed to enqueue interview", extra={"fields": {"interview_id": interview.id}})
        interview.status = InterviewStatus.FAILED
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Scheduling queue unavailable") from exc
    return interview


@router.post("/{interview_id}/start", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_internal_token)])
async def start_interview(interview_id: int) -> dict:
    token = interview_id_var.set(interview_id)
    try:
        call_sid = await start_interview_call(interview_id)
    except InterviewNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found") from exc
    except InvalidInterviewState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:
        logger.exception("failed to start interview call")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to place the call") from exc
    finally:
        interview_id_var.reset(token)
    return {"interview_id": interview_id, "twilio_call_sid": call_sid}


@router.get("/{interview_id}", response_model=InterviewOut, dependencies=[Depends(require_api_key)])
async def get_interview(interview_id: int, db: AsyncSession = Depends(get_db)) -> Interview:
    return await _get_interview(db, interview_id)


@router.get(
    "/{interview_id}/transcript", response_model=list[TranscriptTurnOut], dependencies=[Depends(require_api_key)]
)
async def get_transcript(interview_id: int, db: AsyncSession = Depends(get_db)) -> list[Transcript]:
    await _get_interview(db, interview_id)
    turns = await db.scalars(
        select(Transcript).where(Transcript.interview_id == interview_id).order_by(Transcript.turn_index)
    )
    return list(turns)


@router.get(
    "/{interview_id}/report",
    response_model=ReportOut,
    dependencies=[Depends(require_api_key)],
    responses={202: {"description": "Report not ready yet"}},
)
async def get_report(interview_id: int, db: AsyncSession = Depends(get_db)):
    interview = await _get_interview(db, interview_id)
    report = await db.scalar(select(Report).where(Report.interview_id == interview_id))
    if report is not None:
        return report
    if interview.status in _REPORT_PENDING:
        return JSONResponse(status_code=202, content={"interview_id": interview_id, "status": interview.status})
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"No report: interview status is '{interview.status}'")
