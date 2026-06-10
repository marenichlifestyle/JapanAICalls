"""listing fingerprint and notification delivery state

Revision ID: 0008_listing_fix
Revises: 0007_call_state_machine
Create Date: 2026-05-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_listing_fix"
down_revision = "0007_call_state_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("listing_fingerprint", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("final_report_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("final_report_error", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("notification_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("jobs", sa.Column("next_notification_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_jobs_listing_fingerprint"), "jobs", ["listing_fingerprint"], unique=False)
    op.create_index(
        op.f("ix_jobs_next_notification_retry_at"),
        "jobs",
        ["next_notification_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_next_notification_retry_at"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_listing_fingerprint"), table_name="jobs")
    op.drop_column("jobs", "next_notification_retry_at")
    op.drop_column("jobs", "notification_attempt_count")
    op.drop_column("jobs", "final_report_error")
    op.drop_column("jobs", "final_report_sent_at")
    op.drop_column("jobs", "listing_fingerprint")
