import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import require_api_key
from app.models import Candidate
from app.schemas import CandidateOut
from app.services.resume_parser import UnsupportedResumeError, parse_resume, resume_to_parts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/candidates", tags=["candidates"], dependencies=[Depends(require_api_key)])

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


@router.post("", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
async def create_candidate(
    name: Annotated[str, Form(min_length=1, max_length=200)],
    phone: Annotated[str, Form(max_length=32)],
    email: Annotated[EmailStr, Form()],
    resume: Annotated[UploadFile, File(description="PDF or DOCX")],
    db: AsyncSession = Depends(get_db),
) -> Candidate:
    phone = re.sub(r"[\s\-().]", "", phone)
    if not _E164.match(phone):
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
        logger.exception("resume parsing failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Resume parsing failed, please retry") from exc

    candidate = Candidate(name=name.strip(), phone=phone, email=str(email).lower(), resume_json=resume_json)
    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)
    return candidate


@router.get("/{candidate_id}", response_model=CandidateOut)
async def get_candidate(candidate_id: int, db: AsyncSession = Depends(get_db)) -> Candidate:
    candidate = await db.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return candidate
