from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_TOPICS = ["experience_validation", "technical_depth", "behavioral", "culture_fit"]


class Criterion(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    weight: float = Field(default=1.0, gt=0, le=100)


class InterviewConfig(BaseModel):
    topics: list[str] = Field(default_factory=lambda: list(DEFAULT_TOPICS), min_length=1, max_length=10)
    max_questions: int = Field(default=10, ge=1, le=30)
    max_duration_minutes: int = Field(default=15, ge=2, le=60)


class Rubric(BaseModel):
    criteria: list[Criterion] = Field(min_length=1, max_length=15)
    technical_focus_areas: list[str] = Field(default_factory=list, max_length=20)
    interview_config: InterviewConfig = Field(default_factory=InterviewConfig)


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    email: str
    resume_json: dict[str, Any]
    created_at: datetime


class JobRoleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=20, max_length=20000)
    rubric_json: Rubric | None = Field(default=None, description="Omit to have Gemini generate a rubric from the JD.")
    interview_config: InterviewConfig | None = Field(default=None, description="Overrides rubric_json.interview_config.")


class JobRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    jd_text: str
    rubric_json: dict[str, Any]
    created_at: datetime


class ScheduleRequest(BaseModel):
    candidate_id: int
    job_role_id: int
    scheduled_at: datetime | None = Field(default=None, description="Timezone-aware ISO 8601; defaults to now.")

    @field_validator("scheduled_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone offset, e.g. 2026-10-01T15:00:00+05:30")
        return value.astimezone(UTC)


class InterviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    candidate_id: int
    job_role_id: int
    status: str
    scheduled_at: datetime
    consent_confirmed_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    twilio_call_sid: str | None


class InterviewIntakeOut(BaseModel):
    candidate_id: int
    job_role_id: int
    interview_id: int
    status: str
    scheduled_at: datetime
    consent_confirmed_at: datetime


class TranscriptTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    turn_index: int
    speaker: str
    text: str
    timestamp: datetime


class CriterionScore(BaseModel):
    criterion: str
    score: float = Field(ge=0, le=10)
    evidence: str


class ReportLLM(BaseModel):
    """Shape Gemini Pro must return (enforced via response_schema, validated again here)."""

    scores: list[CriterionScore]
    overall_score: float = Field(ge=0, le=100)
    recommendation: Literal["proceed", "borderline", "reject"]
    strengths: list[str]
    red_flags: list[str]
    summary: str


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    interview_id: int
    scores_json: list[dict[str, Any]]
    overall_score: float
    recommendation: str
    strengths: list[str]
    red_flags: list[str]
    summary: str
    created_at: datetime
