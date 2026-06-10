"""add telegram message tracking fields to jobs

Revision ID: 0003_job_telegram_messages
Revises: 0002_job_fields
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_job_telegram_messages"
down_revision = "0002_job_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("telegram_source_message_id", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("telegram_service_message_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "telegram_service_message_ids")
    op.drop_column("jobs", "telegram_source_message_id")
