"""add call language for multilingual outbound calls

Revision ID: 0004_job_call_language
Revises: 0003_job_telegram_messages
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_job_call_language"
down_revision = "0003_job_telegram_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("call_language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "call_language")
