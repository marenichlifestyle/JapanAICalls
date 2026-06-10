"""add call queue/retry fields

Revision ID: 0006_call_queue_retry
Revises: 0005_dealer_phone_resolver
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_call_queue_retry"
down_revision = "0005_dealer_phone_resolver"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("jobs", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("queued_reason", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("office_tz", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("office_hours_json", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("first_answered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("final_outcome", sa.String(length=64), nullable=True))
    op.create_index("ix_jobs_next_attempt_at", "jobs", ["next_attempt_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_next_attempt_at", table_name="jobs")
    op.drop_column("jobs", "final_outcome")
    op.drop_column("jobs", "first_answered_at")
    op.drop_column("jobs", "last_progress_at")
    op.drop_column("jobs", "office_hours_json")
    op.drop_column("jobs", "office_tz")
    op.drop_column("jobs", "queued_reason")
    op.drop_column("jobs", "last_attempt_at")
    op.drop_column("jobs", "next_attempt_at")
    op.drop_column("jobs", "max_attempts")
    op.drop_column("jobs", "attempt_count")

