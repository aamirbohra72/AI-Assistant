import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InterviewStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    CALLING = "calling"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SCORING = "scoring"
    SCORED = "scored"
    INCOMPLETE = "incomplete"
    NO_ANSWER = "no_answer"
    FAILED = "failed"
    SCORING_FAILED = "scoring_failed"


class Speaker(enum.StrEnum):
    AGENT = "agent"
    CANDIDATE = "candidate"


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str] = mapped_column(String(320), index=True)
    resume_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRole(Base):
    __tablename__ = "job_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    jd_text: Mapped[str] = mapped_column(Text)
    rubric_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_role_id: Mapped[int] = mapped_column(ForeignKey("job_roles.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default=InterviewStatus.SCHEDULED)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped[Candidate] = relationship()
    job_role: Mapped[JobRole] = relationship()
    report: Mapped["Report | None"] = relationship(back_populates="interview", uselist=False)


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("interview_id", "turn_index", name="uq_transcripts_interview_turn"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    interview_id: Mapped[int] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), unique=True)
    scores_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    overall_score: Mapped[float] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(16))
    strengths: Mapped[list[str]] = mapped_column(JSONB)
    red_flags: Mapped[list[str]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    interview: Mapped[Interview] = relationship(back_populates="report")
