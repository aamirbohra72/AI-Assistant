from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_api_key
from app.models import JobRole
from app.schemas import InterviewConfig, JobRoleCreate, JobRoleOut
from app.services.rubric import generate_rubric

router = APIRouter(prefix="/job-roles", tags=["job-roles"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=JobRoleOut, status_code=status.HTTP_201_CREATED)
async def create_job_role(body: JobRoleCreate, db: AsyncSession = Depends(get_db)) -> JobRole:
    if body.rubric_json is not None:
        rubric = body.rubric_json
        if body.interview_config is not None:
            rubric = rubric.model_copy(update={"interview_config": body.interview_config})
    else:
        rubric = await generate_rubric(body.title, body.jd_text, body.interview_config or InterviewConfig())

    role = JobRole(title=body.title.strip(), jd_text=body.jd_text, rubric_json=rubric.model_dump())
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


@router.get("/{job_role_id}", response_model=JobRoleOut)
async def get_job_role(job_role_id: int, db: AsyncSession = Depends(get_db)) -> JobRole:
    role = await db.get(JobRole, job_role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job role not found")
    return role
