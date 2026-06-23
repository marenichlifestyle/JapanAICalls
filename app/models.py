from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_service_message_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    call_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    listing_url: Mapped[str] = mapped_column(Text)
    listing_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("request_call_campaigns.id"), nullable=True, index=True
    )
    request_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("dealer_call_targets.id"), nullable=True, index=True
    )
    request_goal_ru: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(64), default="pending_confirmation", index=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)

    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    car: Mapped[str | None] = mapped_column(Text, nullable=True)
    car_full: Mapped[str | None] = mapped_column(Text, nullable=True)
    car_short: Mapped[str | None] = mapped_column(Text, nullable=True)
    vin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stock_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_total_jpy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vehicle_price_jpy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_total_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_price_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_used_jpy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_used_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    year: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mileage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repair_history: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inspection: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dealer: Mapped[str | None] = mapped_column(Text, nullable=True)
    dealer_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    dealer_business_hours: Mapped[str | None] = mapped_column(Text, nullable=True)
    dealer_closed_days: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_from_listing: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carsensor_free_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dealer_direct_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extracted_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    call_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    possibly_not_callable_internationally: Mapped[bool] = mapped_column(default=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    missing_fields: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    raw_html_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    car_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_total_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_price_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    year_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    mileage_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    inspection_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_used_spoken_ru: Mapped[str | None] = mapped_column(Text, nullable=True)

    listing_phone_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_phone_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_phone_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_phone_e164: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_phone_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_phone_source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolver_confidence_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolver_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolver_error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolver_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    elevenlabs_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    elevenlabs_call_sid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    call_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_call_sid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    from_phone_e164: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    office_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    office_hours_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_report_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_report_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_notification_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    call_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    analysis_available: Mapped[bool | None] = mapped_column(nullable=True)
    analysis_price_confirmed: Mapped[bool | None] = mapped_column(nullable=True)
    analysis_actual_price: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_price_change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_condition_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_seller_mood: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_final_summary_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_ai_quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_ai_quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    errors: Mapped[list[JobError]] = relationship(back_populates="job")
    call_events: Mapped[list[CallEvent]] = relationship(back_populates="job")
    provider_errors: Mapped[list[ProviderError]] = relationship(back_populates="job")
    request_campaign: Mapped[RequestCallCampaign | None] = relationship(back_populates="jobs")
    request_target: Mapped[DealerCallTarget | None] = relationship(back_populates="jobs")


class JobError(Base):
    __tablename__ = "job_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    error_code: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[Job | None] = relationship(back_populates="errors")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_webhook_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_call_sid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_call_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    from_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[Job | None] = relationship(back_populates="call_events")


class ProviderError(Base):
    __tablename__ = "provider_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_more_info_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    human_readable_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[Job | None] = relationship(back_populates="provider_errors")


class RequestCallCampaign(Base):
    __tablename__ = "request_call_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_user_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_service_message_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="request_call")
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_user_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_goal_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    call_sequence_mode: Mapped[str] = mapped_column(String(16), default="manual")
    phone_region: Mapped[str | None] = mapped_column(String(8), nullable=True)
    source_urls_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    vehicle_context_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="draft", index=True)
    total_numbers: Mapped[int] = mapped_column(Integer, default=0)
    valid_numbers: Mapped[int] = mapped_column(Integer, default=0)
    invalid_numbers: Mapped[int] = mapped_column(Integer, default=0)
    rejected_phones_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    goal_meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    targets: Mapped[list[DealerCallTarget]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="DealerCallTarget.id",
    )
    reports: Mapped[list[CallReport]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="CallReport.id",
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="request_campaign")


class DealerCallTarget(Base):
    __tablename__ = "dealer_call_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("request_call_campaigns.id"), index=True)
    dealer_name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone_raw: Mapped[str] = mapped_column(String(64))
    phone_e164: Mapped[str] = mapped_column(String(32), index=True)
    phone_region: Mapped[str | None] = mapped_column(String(8), nullable=True)
    original_line: Mapped[str] = mapped_column(Text)
    goal_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_call_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped[RequestCallCampaign] = relationship(back_populates="targets")
    reports: Mapped[list[CallReport]] = relationship(back_populates="target", cascade="all, delete-orphan")
    jobs: Mapped[list[Job]] = relationship(back_populates="request_target")


class CallReport(Base):
    __tablename__ = "call_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("request_call_campaigns.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("dealer_call_targets.id"), index=True)
    dealer_name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str] = mapped_column(String(32))
    call_status: Mapped[str] = mapped_column(String(64), index=True)
    reached_sales: Mapped[bool | None] = mapped_column(nullable=True)
    target_vehicle_or_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    availability_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    incoming_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    vin_or_stock_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    paperwork_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    important_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[RequestCallCampaign] = relationship(back_populates="reports")
    target: Mapped[DealerCallTarget] = relationship(back_populates="reports")
