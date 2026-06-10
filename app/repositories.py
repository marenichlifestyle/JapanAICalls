from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallEvent, CallReport, DealerCallTarget, Job, JobError, ProviderError, RequestCallCampaign, WebhookEvent
from app.utils.listing import listing_fingerprint, normalized_listing_url


PRE_CALL_RETRYABLE_STATUSES = {
    "canceled",
    "parsing_failed",
    "normalization_failed",
    "dealer_phone_resolution_failed",
    "dealer_phone_not_found",
    "call_create_failed",
    "dynamic_variables_invalid",
}


async def create_job(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    source_message_id: int | None = None,
    call_language: str | None = None,
    source: str | None = None,
    listing_url: str,
    status: str = "pending_confirmation",
) -> Job:
    job = Job(
        telegram_chat_id=chat_id,
        telegram_user_id=user_id,
        telegram_source_message_id=source_message_id,
        telegram_service_message_ids=[],
        call_language=call_language,
        source=source,
        listing_url=listing_url,
        listing_fingerprint=listing_fingerprint(listing_url),
        status=status,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_job(session: AsyncSession, job_id: int) -> Job | None:
    return await session.get(Job, job_id)


async def get_job_by_conversation_id(session: AsyncSession, conversation_id: str) -> Job | None:
    stmt = select(Job).where(Job.elevenlabs_conversation_id == conversation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_job_by_provider_call_sid(session: AsyncSession, provider_call_sid: str) -> Job | None:
    stmt = select(Job).where(Job.provider_call_sid == provider_call_sid)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _blocks_duplicate_call(job: Job) -> bool:
    if job.elevenlabs_conversation_id or job.provider_call_sid or job.elevenlabs_call_sid:
        return True
    return job.status not in PRE_CALL_RETRYABLE_STATUSES


async def find_duplicate_listing_job(
    session: AsyncSession,
    *,
    listing_url: str,
    fingerprint: str | None = None,
    exclude_job_id: int | None = None,
) -> Job | None:
    fingerprint = fingerprint or listing_fingerprint(listing_url)
    candidates: list[Job] = []
    if fingerprint:
        fingerprint_token = fingerprint.split(":", 1)[-1]
        stmt = (
            select(Job)
            .where(
                or_(
                    Job.listing_fingerprint == fingerprint,
                    Job.listing_url.ilike(f"%{fingerprint_token}%"),
                )
            )
            .order_by(Job.id.desc())
            .limit(50)
        )
        candidates.extend((await session.execute(stmt)).scalars().all())

    normalized = normalized_listing_url(listing_url)
    stmt = select(Job).order_by(Job.id.desc()).limit(200)
    for job in (await session.execute(stmt)).scalars().all():
        if normalized_listing_url(job.listing_url) == normalized:
            candidates.append(job)

    seen: set[int] = set()
    for job in candidates:
        if job.id in seen:
            continue
        seen.add(job.id)
        if exclude_job_id is not None and job.id == exclude_job_id:
            continue
        if _blocks_duplicate_call(job):
            return job
    return None


async def list_due_call_queue_jobs(session: AsyncSession, *, limit: int = 20) -> list[Job]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(
            Job.status.in_(("queued_office_hours", "queued_retry", "queued", "retry_scheduled")),
            Job.next_attempt_at.is_not(None),
            Job.next_attempt_at <= now,
        )
        .order_by(Job.next_attempt_at.asc(), Job.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_active_call_jobs(session: AsyncSession, *, limit: int = 20) -> list[Job]:
    stmt = (
        select(Job)
        .where(
            Job.status.in_(
                (
                    "creating_call",
                    "call_created",
                    "queued",
                    "initiated",
                    "ringing",
                    "in_progress",
                    "answered",
                    "call_in_progress",
                    "call_started",
                )
            ),
            or_(Job.elevenlabs_conversation_id.is_not(None), Job.provider_call_sid.is_not(None)),
            or_(Job.final_outcome.is_(None), Job.final_outcome == ""),
        )
        .order_by(Job.updated_at.asc(), Job.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_notification_retry_jobs(session: AsyncSession, *, limit: int = 20) -> list[Job]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(
            Job.final_report_sent_at.is_(None),
            Job.next_notification_retry_at.is_not(None),
            Job.next_notification_retry_at <= now,
        )
        .order_by(Job.next_notification_retry_at.asc(), Job.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def add_job_error(
    session: AsyncSession,
    *,
    code: str,
    message: str,
    job_id: int | None = None,
    details: dict | None = None,
) -> None:
    row = JobError(job_id=job_id, error_code=code, message=message, details=details)
    session.add(row)
    await session.commit()


async def add_call_event(
    session: AsyncSession,
    *,
    job_id: int | None,
    provider: str,
    provider_call_sid: str | None,
    event_type: str,
    raw_call_status: str | None,
    normalized_status: str | None,
    from_phone: str | None,
    to_phone: str | None,
    duration_seconds: int | None,
    error_code: str | None,
    error_message: str | None,
    raw_payload_json: dict | None,
) -> None:
    row = CallEvent(
        job_id=job_id,
        provider=provider,
        provider_call_sid=provider_call_sid,
        event_type=event_type,
        raw_call_status=raw_call_status,
        normalized_status=normalized_status,
        from_phone=from_phone,
        to_phone=to_phone,
        duration_seconds=duration_seconds,
        error_code=error_code,
        error_message=error_message,
        raw_payload_json=raw_payload_json,
    )
    session.add(row)
    await session.commit()


async def add_provider_error(
    session: AsyncSession,
    *,
    job_id: int | None,
    provider: str,
    stage: str,
    http_status: int | None,
    provider_error_code: str | None,
    provider_error_message: str | None,
    provider_more_info_url: str | None,
    from_phone: str | None,
    to_phone: str | None,
    human_readable_hint: str | None,
    raw_payload_json: dict | None,
) -> None:
    row = ProviderError(
        job_id=job_id,
        provider=provider,
        stage=stage,
        http_status=http_status,
        provider_error_code=provider_error_code,
        provider_error_message=provider_error_message,
        provider_more_info_url=provider_more_info_url,
        from_phone=from_phone,
        to_phone=to_phone,
        human_readable_hint=human_readable_hint,
        raw_payload_json=raw_payload_json,
    )
    session.add(row)
    await session.commit()


async def append_service_message_id(session: AsyncSession, *, job: Job, message_id: int) -> None:
    ids = list(job.telegram_service_message_ids or [])
    if message_id not in ids:
        ids.append(message_id)
        job.telegram_service_message_ids = ids
        await session.commit()


REQUEST_CAMPAIGN_INPUT_STATUSES = {
    "draft",
    "needs_phones",
    "needs_goal",
    "needs_goal_clarification",
    "needs_language",
    "mixed_phone_regions",
    "ready_to_confirm",
    "ready_to_call",
}

REQUEST_CAMPAIGN_RUNNING_STATUSES = {
    "calling",
    "waiting_call_result",
    "waiting_next",
}

REQUEST_CAMPAIGN_TERMINAL_STATUSES = {
    "completed",
    "stopped",
    "canceled",
}

REQUEST_CAMPAIGN_OPEN_STATUSES = REQUEST_CAMPAIGN_INPUT_STATUSES | REQUEST_CAMPAIGN_RUNNING_STATUSES


async def create_request_campaign(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    source_message_id: int | None = None,
    raw_input: str | None = None,
    raw_user_goal: str | None = None,
    status: str = "draft",
) -> RequestCallCampaign:
    campaign = RequestCallCampaign(
        telegram_chat_id=chat_id,
        telegram_user_id=user_id,
        telegram_source_message_id=source_message_id,
        raw_input=raw_input,
        raw_user_goal=raw_user_goal,
        status=status,
        rejected_phones_json=[],
        telegram_service_message_ids=[],
    )
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return campaign


async def get_request_campaign(session: AsyncSession, campaign_id: int) -> RequestCallCampaign | None:
    return await session.get(RequestCallCampaign, campaign_id)


async def get_latest_open_request_campaign(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> RequestCallCampaign | None:
    stmt = (
        select(RequestCallCampaign)
        .where(
            RequestCallCampaign.telegram_chat_id == chat_id,
            RequestCallCampaign.telegram_user_id == user_id,
            RequestCallCampaign.status.in_(REQUEST_CAMPAIGN_OPEN_STATUSES),
        )
        .order_by(RequestCallCampaign.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_latest_input_request_campaign(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> RequestCallCampaign | None:
    stmt = (
        select(RequestCallCampaign)
        .where(
            RequestCallCampaign.telegram_chat_id == chat_id,
            RequestCallCampaign.telegram_user_id == user_id,
        )
        .order_by(RequestCallCampaign.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    campaign = result.scalar_one_or_none()
    if campaign and campaign.status in REQUEST_CAMPAIGN_INPUT_STATUSES:
        return campaign
    return None


async def list_request_targets(session: AsyncSession, campaign_id: int) -> list[DealerCallTarget]:
    stmt = select(DealerCallTarget).where(DealerCallTarget.campaign_id == campaign_id).order_by(DealerCallTarget.id)
    return list((await session.execute(stmt)).scalars().all())


async def list_request_reports(session: AsyncSession, campaign_id: int) -> list[CallReport]:
    stmt = select(CallReport).where(CallReport.campaign_id == campaign_id).order_by(CallReport.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_request_target(session: AsyncSession, target_id: int) -> DealerCallTarget | None:
    return await session.get(DealerCallTarget, target_id)


async def clear_service_message_ids(session: AsyncSession, *, job: Job) -> None:
    job.telegram_service_message_ids = []
    await session.commit()


async def save_webhook_event_if_new(
    session: AsyncSession,
    *,
    idempotency_key: str,
    event_type: str,
    conversation_id: str | None,
    payload: dict,
) -> bool:
    stmt = select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        return False
    row = WebhookEvent(
        idempotency_key=idempotency_key,
        event_type=event_type,
        conversation_id=conversation_id,
        payload=payload,
    )
    session.add(row)
    await session.commit()
    return True
