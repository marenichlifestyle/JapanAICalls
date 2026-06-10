"""call state machine and provider events

Revision ID: 0007_call_state_machine
Revises: 0006_call_queue_retry
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_call_state_machine"
down_revision = "0006_call_queue_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("provider_call_sid", sa.String(length=255), nullable=True))
    op.add_column("jobs", sa.Column("from_phone_e164", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("last_error_code", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("last_error_message", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("last_error_hint", sa.Text(), nullable=True))
    op.create_index(op.f("ix_jobs_provider_call_sid"), "jobs", ["provider_call_sid"], unique=False)

    op.create_table(
        "call_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_call_sid", sa.String(length=255), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("raw_call_status", sa.String(length=64), nullable=True),
        sa.Column("normalized_status", sa.String(length=64), nullable=True),
        sa.Column("from_phone", sa.String(length=32), nullable=True),
        sa.Column("to_phone", sa.String(length=32), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_call_events_job_id"), "call_events", ["job_id"], unique=False)
    op.create_index(op.f("ix_call_events_provider_call_sid"), "call_events", ["provider_call_sid"], unique=False)
    op.create_index(op.f("ix_call_events_normalized_status"), "call_events", ["normalized_status"], unique=False)

    op.create_table(
        "provider_errors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_error_code", sa.String(length=64), nullable=True),
        sa.Column("provider_error_message", sa.Text(), nullable=True),
        sa.Column("provider_more_info_url", sa.Text(), nullable=True),
        sa.Column("from_phone", sa.String(length=32), nullable=True),
        sa.Column("to_phone", sa.String(length=32), nullable=True),
        sa.Column("human_readable_hint", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_errors_job_id"), "provider_errors", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_provider_errors_provider_error_code"),
        "provider_errors",
        ["provider_error_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_provider_errors_provider_error_code"), table_name="provider_errors")
    op.drop_index(op.f("ix_provider_errors_job_id"), table_name="provider_errors")
    op.drop_table("provider_errors")

    op.drop_index(op.f("ix_call_events_normalized_status"), table_name="call_events")
    op.drop_index(op.f("ix_call_events_provider_call_sid"), table_name="call_events")
    op.drop_index(op.f("ix_call_events_job_id"), table_name="call_events")
    op.drop_table("call_events")

    op.drop_index(op.f("ix_jobs_provider_call_sid"), table_name="jobs")
    op.drop_column("jobs", "last_error_hint")
    op.drop_column("jobs", "last_error_message")
    op.drop_column("jobs", "last_error_code")
    op.drop_column("jobs", "completed_at")
    op.drop_column("jobs", "answered_at")
    op.drop_column("jobs", "started_at")
    op.drop_column("jobs", "from_phone_e164")
    op.drop_column("jobs", "provider_call_sid")
    op.drop_column("jobs", "provider")
