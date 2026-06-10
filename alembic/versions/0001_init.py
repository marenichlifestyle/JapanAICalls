"""init

Revision ID: 0001_init
Revises:
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_chat_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("listing_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("car", sa.Text(), nullable=True),
        sa.Column("price_total_jpy", sa.Integer(), nullable=True),
        sa.Column("vehicle_price_jpy", sa.Integer(), nullable=True),
        sa.Column("year", sa.String(length=64), nullable=True),
        sa.Column("mileage", sa.String(length=64), nullable=True),
        sa.Column("repair_history", sa.String(length=255), nullable=True),
        sa.Column("inspection", sa.String(length=255), nullable=True),
        sa.Column("dealer", sa.Text(), nullable=True),
        sa.Column("dealer_address", sa.Text(), nullable=True),
        sa.Column("carsensor_free_phone", sa.String(length=32), nullable=True),
        sa.Column("dealer_direct_phone", sa.String(length=32), nullable=True),
        sa.Column("extracted_phone", sa.String(length=32), nullable=True),
        sa.Column("call_phone", sa.String(length=32), nullable=True),
        sa.Column("possibly_not_callable_internationally", sa.Boolean(), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("missing_fields", sa.JSON(), nullable=True),
        sa.Column("raw_html_snapshot", sa.Text(), nullable=True),
        sa.Column("extracted_text_snapshot", sa.Text(), nullable=True),
        sa.Column("car_spoken_ru", sa.Text(), nullable=True),
        sa.Column("price_total_spoken_ru", sa.Text(), nullable=True),
        sa.Column("vehicle_price_spoken_ru", sa.Text(), nullable=True),
        sa.Column("year_spoken_ru", sa.Text(), nullable=True),
        sa.Column("mileage_spoken_ru", sa.Text(), nullable=True),
        sa.Column("inspection_spoken_ru", sa.Text(), nullable=True),
        sa.Column("elevenlabs_conversation_id", sa.String(length=255), nullable=True),
        sa.Column("elevenlabs_call_sid", sa.String(length=255), nullable=True),
        sa.Column("call_status", sa.String(length=64), nullable=True),
        sa.Column("call_transcript", sa.Text(), nullable=True),
        sa.Column("call_summary", sa.Text(), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("analysis_available", sa.Boolean(), nullable=True),
        sa.Column("analysis_price_confirmed", sa.Boolean(), nullable=True),
        sa.Column("analysis_actual_price", sa.Text(), nullable=True),
        sa.Column("analysis_price_change_reason", sa.Text(), nullable=True),
        sa.Column("analysis_condition_notes", sa.Text(), nullable=True),
        sa.Column("analysis_seller_mood", sa.Text(), nullable=True),
        sa.Column("analysis_next_step", sa.Text(), nullable=True),
        sa.Column("analysis_final_summary_ru", sa.Text(), nullable=True),
        sa.Column("analysis_conclusion", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_call_status"), "jobs", ["call_status"], unique=False)
    op.create_index(op.f("ix_jobs_elevenlabs_conversation_id"), "jobs", ["elevenlabs_conversation_id"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_index(op.f("ix_jobs_telegram_chat_id"), "jobs", ["telegram_chat_id"], unique=False)
    op.create_index(op.f("ix_jobs_telegram_user_id"), "jobs", ["telegram_user_id"], unique=False)

    op.create_table(
        "job_errors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_errors_error_code"), "job_errors", ["error_code"], unique=False)
    op.create_index(op.f("ix_job_errors_job_id"), "job_errors", ["job_id"], unique=False)

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_webhook_idempotency"),
    )
    op.create_index(op.f("ix_webhook_events_conversation_id"), "webhook_events", ["conversation_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_webhook_events_conversation_id"), table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index(op.f("ix_job_errors_job_id"), table_name="job_errors")
    op.drop_index(op.f("ix_job_errors_error_code"), table_name="job_errors")
    op.drop_table("job_errors")
    op.drop_index(op.f("ix_jobs_telegram_user_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_telegram_chat_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_elevenlabs_conversation_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_call_status"), table_name="jobs")
    op.drop_table("jobs")
