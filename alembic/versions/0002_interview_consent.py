"""record candidate consent for automated interviews

Revision ID: 0002_interview_consent
Revises: 0001_initial
Create Date: 2026-09-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_interview_consent"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("interviews", sa.Column("consent_confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("interviews", "consent_confirmed_at")