"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("resume_json", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_candidates"),
    )
    op.create_index("ix_candidates_email", "candidates", ["email"])

    op.create_table(
        "job_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("rubric_json", postgresql.JSONB(), server_default=EMPTY_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_roles"),
    )

    op.create_table(
        "interviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("job_role_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("twilio_call_sid", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], name="fk_interviews_candidate_id_candidates", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["job_role_id"], ["job_roles.id"], name="fk_interviews_job_role_id_job_roles", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_interviews"),
        sa.UniqueConstraint("twilio_call_sid", name="uq_interviews_twilio_call_sid"),
    )
    op.create_index("ix_interviews_candidate_id", "interviews", ["candidate_id"])
    op.create_index("ix_interviews_job_role_id", "interviews", ["job_role_id"])
    op.create_index("ix_interviews_status", "interviews", ["status"])

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("interview_id", sa.Integer(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["interviews.id"], name="fk_transcripts_interview_id_interviews", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transcripts"),
        sa.UniqueConstraint("interview_id", "turn_index", name="uq_transcripts_interview_turn"),
    )
    op.create_index("ix_transcripts_interview_id", "transcripts", ["interview_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("interview_id", sa.Integer(), nullable=False),
        sa.Column("scores_json", postgresql.JSONB(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("recommendation", sa.String(16), nullable=False),
        sa.Column("strengths", postgresql.JSONB(), nullable=False),
        sa.Column("red_flags", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["interviews.id"], name="fk_reports_interview_id_interviews", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.UniqueConstraint("interview_id", name="uq_reports_interview_id"),
    )


def downgrade() -> None:
    op.drop_table("reports")
    op.drop_index("ix_transcripts_interview_id", table_name="transcripts")
    op.drop_table("transcripts")
    op.drop_index("ix_interviews_status", table_name="interviews")
    op.drop_index("ix_interviews_job_role_id", table_name="interviews")
    op.drop_index("ix_interviews_candidate_id", table_name="interviews")
    op.drop_table("interviews")
    op.drop_table("job_roles")
    op.drop_index("ix_candidates_email", table_name="candidates")
    op.drop_table("candidates")
