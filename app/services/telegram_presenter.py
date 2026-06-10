from __future__ import annotations

from datetime import timezone, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.models import Job

TELEGRAM_TEXT_LIMIT = 4096


def truncate_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1] + "…"


def build_transcript_expandable_html(transcript: str) -> tuple[str, str | None]:
    prefix = "<b>Транскрипт звонка</b>\n<blockquote expandable>"
    suffix = "</blockquote>"
    raw = (transcript or "").strip() or "Транскрипт отсутствует."
    escaped = escape(raw)
    full = f"{prefix}{escaped}{suffix}"
    if len(full) <= TELEGRAM_TEXT_LIMIT:
        return full, None

    notice = "\n\n<i>Текст сокращён. Полная версия в transcript.txt</i>"
    available = TELEGRAM_TEXT_LIMIT - len(prefix) - len(suffix) - len(notice)
    if available < 32:
        available = 32

    left, right = 1, len(raw)
    best = raw[:1]
    while left <= right:
        mid = (left + right) // 2
        candidate = escape(raw[:mid] + " ...")
        if len(candidate) <= available:
            best = raw[:mid] + " ..."
            left = mid + 1
        else:
            right = mid - 1

    shortened = f"{prefix}{escape(best)}{suffix}{notice}"
    return shortened, raw


def build_final_report_html(job: Job) -> str:
    def _yes_no(value: bool) -> str:
        return "да" if value else "нет"

    def _field(label: str, value: str | int | bool | None) -> str:
        safe = "—" if value in (None, "") else escape(str(value))
        return f"<b>{escape(label)}:</b> {safe}"

    def _format_local(dt) -> str | None:
        if not dt:
            return None
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz_name = job.office_tz or "Asia/Tokyo"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone(timedelta(hours=9))
        return dt.astimezone(tz).strftime(f"%Y-%m-%d %H:%M {tz_name}")

    test_mode = bool(job.call_phone and job.call_phone != job.extracted_phone)
    call_language_key = job.call_language or "ru"
    call_language = {"ja": "японский", "en": "английский"}.get(call_language_key, "русский")
    link = f'<a href="{escape(job.listing_url, quote=True)}">открыть объявление</a>'
    retry_outcome_map = {
        "success": "успех",
        "no_answer_3_attempts": "не ответили 3 раза",
        "analysis_failed": "анализ звонка не выполнен",
    }
    retry_outcome = retry_outcome_map.get(job.final_outcome or "", job.final_outcome)
    timeline = " | ".join(
        [
            f"последняя попытка: {_format_local(job.last_attempt_at) or '—'}",
            f"следующая попытка: {_format_local(job.next_attempt_at) or '—'}",
            f"первый ответ: {_format_local(job.first_answered_at) or '—'}",
        ]
    )
    lines = [
        "<b>Финальный отчёт</b>",
        _field("Язык звонка", call_language),
        _field("Авто (полное)", job.car_full or job.car),
        _field("Авто (короткое)", job.car_short or job.car),
        _field("VIN", job.vin),
        _field("Stock #", job.stock_number),
        f"<b>Ссылка:</b> {link}",
        _field("Номер из объявления", job.extracted_phone),
        _field("Номер фактического звонка", job.call_phone),
        _field("Тестовый режим", _yes_no(test_mode)),
        _field("Телефон в листинге (raw)", job.listing_phone_raw or job.phone_from_listing),
        _field("Тип номера листинга", job.listing_phone_type),
        _field("Resolver статус", job.resolver_status),
        _field("Resolver score", job.resolver_confidence_score),
        _field("Resolver source", job.resolved_phone_source_type),
        _field("Resolver URL", job.resolved_phone_source_url),
        _field("Resolver ошибка", job.resolver_error_reason),
        _field("Статус прозвона", job.call_status or job.status),
        _field("Попытки", f"{job.attempt_count}/{job.max_attempts}" if job.max_attempts else job.attempt_count),
        _field("Итог retry-логики", retry_outcome),
        _field("Таймлайн попыток", timeline),
        _field("Цена для звонка", f"{job.price_used_jpy} ({job.price_used_type})" if job.price_used_jpy else None),
        _field("Цена подтверждена", job.analysis_price_confirmed),
        _field("Новая цена", job.analysis_actual_price),
        _field("Причина изменения", job.analysis_price_change_reason),
        _field("Состояние", job.analysis_condition_notes),
        _field("Вывод", job.analysis_conclusion),
    ]
    if job.analysis_ai_quality_score is not None:
        reason = f" ({job.analysis_ai_quality_reason})" if job.analysis_ai_quality_reason else ""
        lines.append(_field("Оценка AI", f"{job.analysis_ai_quality_score}/100{reason}"))
    return "\n".join(lines)
