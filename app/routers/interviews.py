import logging
import re
from typing import Annotated
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis_store
from app.config import get_settings
from app.db import get_db
from app.deps import require_api_key, require_internal_token
from app.logging_setup import interview_id_var
from app.models import Candidate, Interview, InterviewStatus, JobRole, Report, Transcript
from app.schemas import InterviewConfig, InterviewIntakeOut, InterviewOut, ReportOut, ScheduleRequest, TranscriptTurnOut
from app.services.interviews import InterviewNotFound, InvalidInterviewState, start_interview_call
from app.services.resume_parser import UnsupportedResumeError, parse_resume, resume_to_parts
from app.services.rubric import generate_rubric

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/interviews", tags=["interviews"])

_REPORT_PENDING = {
    InterviewStatus.SCHEDULED,
    InterviewStatus.CALLING,
    InterviewStatus.IN_PROGRESS,
    InterviewStatus.COMPLETED,
    InterviewStatus.SCORING,
}

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_FDE_TITLE = "Forward Deployed Engineer"
_FDE_JOB_DESCRIPTION = (
    "We are hiring a Forward Deployed Engineer with 4-5 years of professional software engineering experience. "
    "Work directly with customer engineering teams to understand requirements, deliver production integrations, "
    "and troubleshoot issues in deployed systems. Strong hands-on Python and FastAPI skills are required, along "
    "with React experience for building customer-facing interfaces. Design and integrate REST APIs, build reliable "
    "asynchronous services, work with relational databases and cloud infrastructure, and communicate technical "
    "trade-offs clearly. Demonstrate ownership from discovery through implementation, deployment, and support."
)


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


@router.post(
    "/intake",
    response_model=InterviewIntakeOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def intake_candidate(
    email: Annotated[EmailStr, Form()],
    phone: Annotated[str, Form()],
    candidate_consented: Annotated[
        bool,
        Form(description="Set true only after the candidate agreed to an AI-led interview and transcription."),
    ],
    resume: Annotated[UploadFile, File(description="Candidate resume in PDF or DOCX format")],
    db: AsyncSession = Depends(get_db),
) -> InterviewIntakeOut:
    if not candidate_consented:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Candidate consent is required before placing a call")

    normalized_phone = re.sub(r"[\s\-().]", "", phone)
    if not _E164.match(normalized_phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "phone must be E.164, e.g. +14155550123")

    max_bytes = get_settings().MAX_RESUME_BYTES
    data = await resume.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Resume exceeds {max_bytes} bytes")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Resume file is empty")

    try:
        parts = await resume_to_parts(data, resume.filename or "")
    except UnsupportedResumeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    try:
        resume_json = await parse_resume(parts)
    except Exception as exc:
        logger.exception("resume parsing failed during interview intake")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Resume parsing failed, please retry") from exc

    candidate_name = str(resume_json.get("full_name") or "").strip()
    if not candidate_name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Resume did not contain a candidate name")

    role = await db.scalar(
        select(JobRole).where(JobRole.title == _FDE_TITLE, JobRole.jd_text == _FDE_JOB_DESCRIPTION).limit(1)
    )
    if role is None:
        interview_config = InterviewConfig(
            topics=["experience_validation", "technical_depth", "behavioral"],
            max_questions=8,
            max_duration_minutes=15,
        )
        rubric = await generate_rubric(_FDE_TITLE, _FDE_JOB_DESCRIPTION, interview_config)
        role = JobRole(title=_FDE_TITLE, jd_text=_FDE_JOB_DESCRIPTION, rubric_json=rubric.model_dump())
        db.add(role)
        await db.flush()

    consent_confirmed_at = datetime.now(UTC)
    candidate = Candidate(
        name=candidate_name,
        phone=normalized_phone,
        email=str(email).lower(),
        resume_json=resume_json,
    )
    db.add(candidate)
    await db.flush()

    interview = Interview(
        candidate_id=candidate.id,
        job_role_id=role.id,
        status=InterviewStatus.SCHEDULED,
        scheduled_at=consent_confirmed_at,
        consent_confirmed_at=consent_confirmed_at,
    )
    db.add(interview)
    await db.commit()
    await db.refresh(interview)

    try:
        await redis_store.enqueue_call(interview.id, interview.scheduled_at)
    except Exception as exc:
        logger.exception(
            "failed to enqueue automatically created interview",
            extra={"fields": {"interview_id": interview.id}},
        )
        interview.status = InterviewStatus.FAILED
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Scheduling queue unavailable") from exc

    return InterviewIntakeOut(
        candidate_id=candidate.id,
        job_role_id=role.id,
        interview_id=interview.id,
        status=interview.status,
        scheduled_at=interview.scheduled_at,
        consent_confirmed_at=consent_confirmed_at,
    )


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
