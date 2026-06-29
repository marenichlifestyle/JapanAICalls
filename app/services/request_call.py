from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import phonenumbers
from aiogram import Bot
from aiogram.types import BufferedInputFile
from phonenumbers import NumberParseException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import request_call_cancel_keyboard, request_call_next_keyboard
from app.config import Settings
from app.models import CallReport, DealerCallTarget, DealerPhoneContext, Job, RequestCallCampaign
from app.schemas import GoalGenerationResult, RequestCallReportResult
from app.services.call_state import classify_twilio_create_failure, extract_twilio_error_code, sanitize_payload
from app.services.elevenlabs_client import ElevenLabsService, ProviderCallCreateError
from app.services.openai_client import OpenAIService
from app.services.request_call_context import RequestCallContextExtractor
from app.services.telegram_delivery import safe_delete_message, safe_send_document, safe_send_message
from app.utils.phone import is_special_or_proxy_phone, normalize_jp_phone_to_e164, normalize_us_phone_to_e164

logger = logging.getLogger(__name__)

PHONE_LIKE_RE = re.compile(r"(?:\+?\d[\d\s\-\(\)\.]{6,}\d)")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
PHONE_MATCH_REGIONS = ("US", "JP", "RU", "GB", "FR", "DE", "AE", "KZ", "TR", "KR", "CN")
KNOWN_CITY_SUFFIXES = (
    "Jacksonville",
    "Gainesville",
    "St. Augustine",
    "Miami",
    "Brooklyn",
    "Orlando",
    "Tampa",
    "Atlanta",
    "Chicago",
    "Hodgkins",
    "Willowbrook",
    "Tokyo",
    "Osaka",
    "Sapporo",
    "Nagoya",
    "Yokohama",
)
READY_GOAL_STATUSES = {"ready", "ok", "success", "ready_to_confirm"}
REQUEST_CALL_SEQUENCE_MODES = {"manual", "auto"}
REQUEST_CALL_PROCESSING_MESSAGE = "Принято, формирую список дилеров, контекст авто и цель прозвона..."
REQUEST_GOAL_MAX_WORDS = 100
REQUEST_CONFIRMATION_QUESTION_LABELS = (
    "наличие/поставка",
    "цена/MSRP/markup/fees",
    "конфигурация/цвет",
    "VIN/stock",
    "оплата/документы",
)
TELEGRAM_MESSAGE_LIMIT = 4096
EMPTY_REPORT_VALUES = {
    "",
    "—",
    "-",
    "null",
    "none",
    "not_answered",
    "нет",
    "нет данных",
    "не получено",
    "не выяснили",
}
REQUEST_CALL_FINALIZATION_LOCK_CLASS_ID = 62041
PHONE_CONTEXT_MAX_ITEMS = 3
PHONE_CONTEXT_PREFIX_MAX_WORDS = 70
PHONE_CONTEXT_GOAL_MAX_WORDS = 180
PHONE_CONTEXT_PREFIXES = {
    "en": (
        "Previous call context: We previously spoke with this number. "
        "Briefly mention that we contacted them before, summarize the prior result, "
        "then move to the new follow-up question."
    ),
    "ja": (
        "前回の通話内容: 以前この番号と話しました。"
        "最初に以前連絡したことを伝え、前回の結果を短く説明してから、今回の追加確認に進んでください。"
    ),
    "ru": (
        "Предыдущий контекст: ранее уже связывались с этим номером. "
        "Начни с того, что мы уже общались, кратко напомни прошлый результат и перейди к новому уточнению."
    ),
}
PHONE_CONTEXT_TERMINAL_SKIP_STATUSES = {
    "no_answer",
    "busy",
    "failed",
    "call_create_failed",
    "provider_timeout",
    "timeout",
    "canceled",
}
REQUEST_CAMPAIGN_INPUT_STATUSES = {
    "draft",
    "needs_country",
    "needs_phones",
    "needs_goal",
    "needs_phones_and_goal",
    "needs_goal_clarification",
    "needs_language",
    "mixed_phone_regions",
    "ready_to_confirm",
    "ready_to_call",
}
REQUEST_CAMPAIGN_RUNNING_STATUSES = {"calling", "waiting_call_result", "waiting_next"}
REQUEST_CAMPAIGN_TERMINAL_STATUSES = {"completed", "stopped", "canceled"}
REQUEST_TARGET_TERMINAL_STATUSES = {
    "completed",
    "no_answer",
    "busy",
    "failed",
    "refused",
    "asked_to_message",
    "call_create_failed",
    "provider_timeout",
    "timeout",
    "canceled",
}
REQUEST_JOB_TERMINAL_STATUSES = {
    "completed",
    "no_answer",
    "busy",
    "failed",
    "canceled",
    "canceled_by_review",
    "call_create_failed",
    "provider_timeout",
    "timeout",
}


@dataclass
class ParsedDealerLine:
    dealer_name: str
    city: str | None
    phone_raw: str
    phone_e164: str
    phone_region: str
    original_line: str


@dataclass
class RejectedPhone:
    original_line: str
    reason: str


@dataclass
class ParsedRequestInput:
    dealers: list[ParsedDealerLine] = field(default_factory=list)
    rejected_phones: list[RejectedPhone] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    raw_user_goal: str = ""
    has_goal_text: bool = False

    @property
    def phone_regions(self) -> set[str]:
        return {dealer.phone_region for dealer in self.dealers if dealer.phone_region}

    @property
    def has_mixed_phone_regions(self) -> bool:
        return len(self.phone_regions) > 1

    @property
    def status(self) -> str:
        has_phones = bool(self.dealers)
        has_goal = self.has_goal_text
        if has_phones and has_goal:
            return "ready_to_confirm"
        if has_goal:
            return "needs_phones"
        if has_phones:
            return "needs_goal"
        return "needs_phones_and_goal"


def _clean_spaces(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\t", " ")).strip(" ,;-—")


def _normalize_allowed_phone(raw: str, default_region: str) -> tuple[str | None, str | None]:
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException:
        return None, None
    if not phonenumbers.is_valid_number(parsed):
        return None, None
    if parsed.country_code == 81:
        if is_special_or_proxy_phone(raw):
            return None, None
        e164 = normalize_jp_phone_to_e164(raw)
        if not e164:
            e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        return (e164, "JP") if e164 else (None, None)
    if parsed.country_code == 1:
        e164 = normalize_us_phone_to_e164(raw) or phonenumbers.format_number(
            parsed,
            phonenumbers.PhoneNumberFormat.E164,
        )
        return (e164, "US") if e164 else (None, None)
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed) or f"CC{parsed.country_code}"
    return (e164, region[:8]) if e164 else (None, None)


def _phone_match_regions(default_region: str | None = None) -> tuple[str, ...]:
    normalized = (default_region or "").upper()
    if normalized in PHONE_MATCH_REGIONS:
        return (normalized, *(region for region in PHONE_MATCH_REGIONS if region != normalized))
    return PHONE_MATCH_REGIONS


def _find_allowed_phone_matches(line: str, default_region: str | None = None) -> list[tuple[int, int, str, str, str]]:
    matches: list[tuple[int, int, str, str, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for region in _phone_match_regions(default_region):
        for match in phonenumbers.PhoneNumberMatcher(line, region):
            span = (match.start, match.end)
            if span in seen_spans:
                continue
            phone_e164, phone_region = _normalize_allowed_phone(match.raw_string, region)
            if not phone_e164 or not phone_region:
                continue
            seen_spans.add(span)
            matches.append((match.start, match.end, match.raw_string, phone_e164, phone_region))
    return sorted(matches, key=lambda row: (row[0], row[1]))


def _extract_urls(line: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(".,;)")
        urls.append(raw)
        return " "

    return _clean_spaces(URL_RE.sub(repl, line)), urls


def _word_count(value: str | None) -> int:
    return len(re.findall(r"\S+", value or ""))


def _limit_words(value: str | None, *, limit: int) -> str:
    words = re.findall(r"\S+", value or "")
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip() + "…"


def _compact_value(value: str | None, *, limit: int = 220) -> str | None:
    cleaned = _clean_spaces(value or "")
    if cleaned.lower() in EMPTY_REPORT_VALUES:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _join_compact_values(*values: str | None, limit: int = 260) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = _compact_value(value, limit=limit)
        if not compact:
            continue
        key = compact.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(compact)
    if not parts:
        return None
    return _compact_value("; ".join(parts), limit=limit)


def _is_meaningful_phone_context_report(report: CallReport) -> bool:
    status = (report.call_status or "").strip().lower()
    if status in PHONE_CONTEXT_TERMINAL_SKIP_STATUSES:
        return False
    if status in {"completed", "refused", "asked_to_message"}:
        return True
    return bool(report.reached_sales or _report_has_useful_result(report))


def _phone_context_item_summary(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    parts = [
        _compact_value(str(item.get("goal_summary") or ""), limit=170),
        _compact_value(str(item.get("summary") or ""), limit=170),
        _compact_value(str(item.get("availability") or ""), limit=120),
        _compact_value(str(item.get("incoming") or ""), limit=120),
        _compact_value(str(item.get("price") or ""), limit=120),
        _compact_value(str(item.get("vin_or_stock") or ""), limit=120),
        _compact_value(str(item.get("payment") or ""), limit=120),
        _compact_value(str(item.get("paperwork") or ""), limit=120),
        _compact_value(str(item.get("important_notes") or ""), limit=140),
        _compact_value(str(item.get("next_action") or ""), limit=140),
    ]
    return _join_compact_values(*parts, limit=360)


def _build_phone_context_summary(items: list[dict[str, Any]]) -> str | None:
    summaries: list[str] = []
    for item in items[:PHONE_CONTEXT_MAX_ITEMS]:
        summary = _phone_context_item_summary(item)
        if not summary:
            continue
        called_at = _compact_value(str(item.get("called_at") or ""), limit=24)
        prefix = f"{called_at}: " if called_at else ""
        summaries.append(prefix + summary)
    return _compact_value(" | ".join(summaries), limit=900)


def _goal_with_phone_context(base_goal: str, context: DealerPhoneContext | None, call_language: str | None) -> str:
    context_summary = _compact_value(context.context_summary if context else None, limit=900)
    if not context or not context_summary:
        return base_goal
    language = "ja" if call_language == "ja" else "en" if call_language == "en" else "ru"
    prefix = PHONE_CONTEXT_PREFIXES[language]
    result_label = {"en": "Prior result", "ja": "前回の結果", "ru": "Прошлый результат"}[language]
    goal_label = {"en": "New call goal", "ja": "今回の目的", "ru": "Новая цель звонка"}[language]
    context_text = _limit_words(context_summary, limit=PHONE_CONTEXT_PREFIX_MAX_WORDS)
    goal = f"{prefix} {result_label}: {context_text}\n\n{goal_label}: {base_goal}"
    if _word_count(goal) <= PHONE_CONTEXT_GOAL_MAX_WORDS:
        return goal
    allowed_prefix_words = max(20, PHONE_CONTEXT_GOAL_MAX_WORDS - _word_count(base_goal) - 18)
    context_text = _limit_words(context_summary, limit=min(PHONE_CONTEXT_PREFIX_MAX_WORDS, allowed_prefix_words))
    return f"{prefix} {result_label}: {context_text}\n\n{goal_label}: {base_goal}"


def _status_label(status: str | None) -> str:
    return {
        "completed": "дозвонились",
        "no_answer": "нет ответа",
        "busy": "занято",
        "failed": "ошибка",
        "refused": "отказ",
        "asked_to_message": "попросили написать",
        "call_create_failed": "ошибка создания звонка",
        "provider_timeout": "таймаут провайдера",
        "timeout": "таймаут",
        "canceled": "отменён",
    }.get(status or "", status or "—")


async def _try_acquire_request_call_finalization_lock(session: AsyncSession, job_id: int) -> bool:
    """Prevent webhook and fallback workers from finalizing the same call at once."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:class_id, :job_id)"),
        {"class_id": REQUEST_CALL_FINALIZATION_LOCK_CLASS_ID, "job_id": int(job_id)},
    )
    return bool(result.scalar())


def _html_value(value: str | None, *, limit: int = 260) -> str | None:
    compact = _compact_value(value, limit=limit)
    return html.escape(compact) if compact else None


def _goal_answer_status_label(status: str | None) -> str:
    return {
        "answered": "получено",
        "not_answered": "не получено",
        "unknown": "не знают",
        "refused": "отказались отвечать",
        "not_applicable": "не применимо",
    }.get((status or "").strip(), "не получено")


def _goal_answer_marker(item: dict) -> str:
    marker = str(item.get("result_marker") or "").strip().lower()
    if marker == "green":
        return "✅"
    if marker == "yellow":
        return "⚠️"
    if marker == "red":
        return "❌"
    status = str(item.get("status") or "").strip().lower()
    if status == "answered":
        return "✅"
    if status in {"unknown", "not_applicable"}:
        return "⚠️"
    return "❌"


def _raw_goal_answer_value(value: object, *, limit: int = 180) -> str | None:
    cleaned = _clean_spaces(str(value or ""))
    if cleaned.lower() in {"", "null", "none", "—", "-"}:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _goal_answer_lines(report: CallReport, *, limit: int = 8) -> list[str]:
    raw = getattr(report, "raw_report_json", None) or {}
    answers = raw.get("goal_answers") if isinstance(raw, dict) else None
    if not isinstance(answers, list):
        return []
    lines: list[str] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        question = _raw_goal_answer_value(item.get("question"), limit=90)
        if not question:
            continue
        status = str(item.get("status") or "not_answered").strip()
        answer = _raw_goal_answer_value(item.get("answer"), limit=180)
        if status != "answered" and not answer:
            answer = _goal_answer_status_label(status)
        elif answer and status != "answered" and answer.lower() not in {"не получено", "not_answered"}:
            answer = f"{answer} ({_goal_answer_status_label(status)})"
        elif not answer:
            answer = "не получено"
        marker = _goal_answer_marker(item)
        reason = _raw_goal_answer_value(item.get("reason"), limit=140)
        suffix = f" — {html.escape(reason)}" if reason else ""
        lines.append(f"{marker} <b>{html.escape(question)}:</b> {html.escape(answer)}{suffix}")
        if len(lines) >= limit:
            break
    return lines


def _critical_missing_lines(report: CallReport, *, limit: int = 6) -> list[str]:
    raw = getattr(report, "raw_report_json", None) or {}
    missing = raw.get("critical_missing") if isinstance(raw, dict) else None
    if not isinstance(missing, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in missing:
        text = _raw_goal_answer_value(item, limit=100)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(html.escape(text))
        if len(result) >= limit:
            break
    return result


def _commitment_lines(report: CallReport, *, limit: int = 4) -> list[str]:
    raw = getattr(report, "raw_report_json", None) or {}
    commitments = raw.get("commitments") if isinstance(raw, dict) else None
    if not isinstance(commitments, list):
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for item in commitments:
        text = _raw_goal_answer_value(item, limit=180)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• {html.escape(text)}")
        if len(lines) >= limit:
            break
    return lines


def _contact_detail_lines(report: CallReport, *, limit: int = 5) -> list[str]:
    raw = getattr(report, "raw_report_json", None) or {}
    contacts = raw.get("contact_details") if isinstance(raw, dict) else None
    if not isinstance(contacts, list):
        return []
    labels = {
        "phone": "телефон",
        "email": "email",
        "whatsapp": "WhatsApp",
        "person": "контакт",
        "other": "контакт",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for item in contacts:
        if not isinstance(item, dict):
            continue
        value = _raw_goal_answer_value(item.get("value"), limit=120)
        if not value:
            continue
        contact_type = str(item.get("type") or "other").strip().lower()
        purpose = _raw_goal_answer_value(item.get("purpose"), limit=120)
        key = f"{contact_type}:{value}".lower()
        if key in seen:
            continue
        seen.add(key)
        label = labels.get(contact_type, "контакт")
        suffix = f" — {html.escape(purpose)}" if purpose else ""
        lines.append(f"• <b>{html.escape(label)}:</b> <code>{html.escape(value)}</code>{suffix}")
        if len(lines) >= limit:
            break
    return lines


def _report_has_useful_result(report: CallReport) -> bool:
    if (report.call_status or "") == "asked_to_message":
        return True
    useful_fields = (
        report.availability_result,
        report.incoming_result,
        report.price_result,
        report.configuration_result,
        report.vin_or_stock_result,
        report.payment_result,
        report.paperwork_result,
        report.important_notes,
        report.next_action,
    )
    return any(_compact_value(value, limit=180) for value in useful_fields)


def _campaign_owner_mention(campaign: RequestCallCampaign | None) -> str | None:
    if campaign is None:
        return None
    username = _clean_spaces(campaign.telegram_username or "").lstrip("@")
    if username:
        return f"@{html.escape(username)}"
    if not campaign.telegram_user_id:
        return None
    display_name = _compact_value(campaign.telegram_user_display_name, limit=80) or f"user {campaign.telegram_user_id}"
    return f'<a href="tg://user?id={int(campaign.telegram_user_id)}">{html.escape(display_name)}</a>'


def build_request_target_report_html(
    target: DealerCallTarget,
    report: CallReport,
    campaign: RequestCallCampaign | None = None,
) -> str:
    availability = _join_compact_values(report.availability_result, report.incoming_result)
    payment_docs = _join_compact_values(report.payment_result, report.paperwork_result)
    next_action = _join_compact_values(report.next_action, report.important_notes)
    owner = _campaign_owner_mention(campaign)
    lines = [
        f"<b>Отчёт: {html.escape(target.dealer_name)}</b>",
        "━━━━━━━━━━━━",
        f"<b>Статус:</b> {html.escape(_status_label(report.call_status))}",
    ]
    if owner:
        lines.append(f"<b>Поставил задачу:</b> {owner}")
    lines.append(f"<b>Номер:</b> <code>{html.escape(target.phone_e164)}</code>")
    if report.ai_quality_score is not None:
        reason = _html_value(report.ai_quality_reason, limit=180)
        suffix = f" ({reason})" if reason else ""
        lines.append(f"<b>Оценка AI:</b> <code>{report.ai_quality_score}/100</code>{suffix}")
    goal_lines = _goal_answer_lines(report)
    if goal_lines:
        lines.append("<b>Ответы по цели:</b>")
        lines.extend(goal_lines)
    missing_lines = _critical_missing_lines(report)
    if missing_lines:
        lines.append(f"<b>Не закрыто:</b> {', '.join(missing_lines)}")
    commitment_lines = _commitment_lines(report)
    if commitment_lines:
        lines.append("<b>Договорённости:</b>")
        lines.extend(commitment_lines)
    contact_lines = _contact_detail_lines(report)
    if contact_lines:
        lines.append("<b>Контакты/обратная связь:</b>")
        lines.extend(contact_lines)
    fact_rows = (
        ("Итог", report.summary),
        ("Наличие/поставка", availability),
        ("Цена", report.price_result),
        ("Конфигурация", report.configuration_result),
        ("VIN/stock", report.vin_or_stock_result),
        ("Оплата/документы", payment_docs),
        ("Следующее действие", next_action),
    )
    for label, value in fact_rows:
        escaped = _html_value(value)
        if escaped:
            lines.append(f"<b>{html.escape(label)}:</b> {escaped}")
    return "\n".join(lines)


def _summary_line_for_report(report: CallReport, *, limit: int = 190) -> str:
    parts = [
        _compact_value(report.summary, limit=limit),
        _compact_value(report.availability_result, limit=limit),
        _compact_value(report.incoming_result, limit=limit),
        _compact_value(report.price_result, limit=limit),
        _compact_value(report.next_action, limit=limit),
    ]
    text = "; ".join(part for part in parts if part)
    return _compact_value(text, limit=limit) or "см. индивидуальный отчёт"


def _chunk_html_lines(lines: list[str], *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_request_campaign_summary_html_chunks(reports: list[CallReport]) -> list[str]:
    counts = {
        "completed": 0,
        "no_answer": 0,
        "busy": 0,
        "failed": 0,
        "asked_to_message": 0,
    }
    for report in reports:
        status = report.call_status or "failed"
        if status in counts:
            counts[status] += 1
        elif status in {"call_create_failed", "canceled", "provider_timeout", "timeout"}:
            counts["failed"] += 1

    useful = [report for report in reports if _report_has_useful_result(report)]
    incoming = [report for report in reports if _compact_value(report.incoming_result)]
    lines = [
        "<b>Прозвон завершён.</b>",
        "━━━━━━━━━━━━",
        "<b>Итог:</b>",
        f"• дозвонились: <code>{counts['completed']}</code>",
        f"• нет ответа: <code>{counts['no_answer']}</code>",
        f"• занято: <code>{counts['busy']}</code>",
        f"• ошибка: <code>{counts['failed']}</code>",
        f"• попросили написать: <code>{counts['asked_to_message']}</code>",
        f"• полезных результатов: <code>{len(useful)}</code>",
        f"• ближайшая поставка: <code>{len(incoming)}</code>",
        "",
        "<b>Варианты:</b>",
    ]
    if not useful:
        lines.append("Полезных вариантов не найдено.")
    else:
        for idx, report in enumerate(useful, start=1):
            summary = _summary_line_for_report(report)
            lines.append(
                f"{idx}. <b>{html.escape(report.dealer_name)}</b> "
                f"(<code>{html.escape(report.phone_e164)}</code>): {html.escape(summary)}"
            )
    return _chunk_html_lines(lines)


def _detect_city(dealer_text: str) -> str | None:
    for city in sorted(KNOWN_CITY_SUFFIXES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b$", dealer_text, flags=re.IGNORECASE):
            return city
    return None


def _dedupe_duplicate_city(dealer_text: str, city: str | None) -> str:
    if not city:
        return dealer_text
    pattern = rf"(.+)\b{re.escape(city)}\s+{re.escape(city)}$"
    match = re.match(pattern, dealer_text, flags=re.IGNORECASE)
    if match:
        return _clean_spaces(f"{match.group(1)} {city}")
    return dealer_text


def parse_request_call_input(text: str, *, default_region: str | None = None) -> ParsedRequestInput:
    result = ParsedRequestInput()
    default_region = (default_region or "").upper() or None
    result.raw_user_goal = _clean_spaces(text)

    for raw_line in (text or "").splitlines():
        line = _clean_spaces(raw_line)
        if not line:
            continue

        line_without_urls, urls = _extract_urls(line)
        result.source_urls.extend(url for url in urls if url not in result.source_urls)
        line = line_without_urls
        if not line:
            continue

        matches = _find_allowed_phone_matches(line, default_region=default_region)
        if default_region in {"US", "JP"} and matches:
            country_matches = [match for match in matches if match[4] == default_region]
            if not country_matches:
                result.rejected_phones.append(
                    RejectedPhone(original_line=line, reason=f"wrong_country_expected_{default_region.lower()}")
                )
                continue
            matches = country_matches
        if matches:
            start, end, phone_raw, phone_e164, phone_region = matches[-1]
            dealer_text = _clean_spaces(line[:start] + " " + line[end:])
            city = _detect_city(dealer_text)
            dealer_name = _dedupe_duplicate_city(dealer_text, city) or "Unknown dealer"
            result.dealers.append(
                ParsedDealerLine(
                    dealer_name=dealer_name,
                    city=city,
                    phone_raw=phone_raw,
                    phone_e164=phone_e164,
                    phone_region=phone_region,
                    original_line=line,
                )
            )
            continue

        if PHONE_LIKE_RE.search(line):
            result.rejected_phones.append(RejectedPhone(original_line=line, reason="invalid_phone"))
            continue

        result.has_goal_text = True
    return result


def _has_request_goal_text(text: str | None, default_region: str | None = None) -> bool:
    return parse_request_call_input(text or "", default_region=default_region).has_goal_text


def _has_vehicle_context(vehicle_context: list[dict[str, Any]] | None) -> bool:
    for context in vehicle_context or []:
        if not isinstance(context, dict):
            continue
        if _clean_spaces(context.get("vehicle_title")):
            return True
        if any(_clean_spaces(context.get(key)) for key in ("make", "model", "year", "vin", "stock_number")):
            return True
    return False


class RequestCallService:
    def __init__(
        self,
        *,
        settings: Settings,
        openai_service: OpenAIService,
        elevenlabs_service: ElevenLabsService,
    ):
        self.settings = settings
        self.openai_service = openai_service
        self.elevenlabs_service = elevenlabs_service
        self.context_extractor = RequestCallContextExtractor(settings=settings, openai_service=openai_service)

    async def create_draft(
        self,
        *,
        session: AsyncSession,
        chat_id: int,
        user_id: int,
        source_message_id: int | None = None,
        status: str = "draft",
        username: str | None = None,
        display_name: str | None = None,
    ) -> RequestCallCampaign:
        campaign = RequestCallCampaign(
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
            telegram_username=username,
            telegram_user_display_name=display_name,
            telegram_source_message_id=source_message_id,
            status=status,
            call_sequence_mode="manual",
            rejected_phones_json=[],
            telegram_service_message_ids=[],
        )
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
        return campaign

    async def update_campaign_owner(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> None:
        campaign.telegram_user_id = user_id
        campaign.telegram_username = username
        campaign.telegram_user_display_name = display_name
        await session.commit()

    async def record_service_message(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        message_id: int | None,
    ) -> None:
        if not message_id:
            return
        ids = list(campaign.telegram_service_message_ids or [])
        if message_id not in ids:
            ids.append(message_id)
            campaign.telegram_service_message_ids = ids
            await session.commit()

    async def send_service_message(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        bot: Bot,
        text: str,
        **kwargs: Any,
    ):
        if "reply_markup" not in kwargs and campaign.status not in REQUEST_CAMPAIGN_TERMINAL_STATUSES:
            kwargs["reply_markup"] = request_call_cancel_keyboard(campaign.id)
        message = await safe_send_message(bot, campaign.telegram_chat_id, text, **kwargs)
        if message is not None:
            await self.record_service_message(session=session, campaign=campaign, message_id=message.message_id)
        return message

    async def cleanup_service_messages(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        bot: Bot,
    ) -> None:
        ids = list(campaign.telegram_service_message_ids or [])
        for message_id in ids:
            await safe_delete_message(bot, campaign.telegram_chat_id, message_id)
        campaign.telegram_service_message_ids = []

        stmt = select(Job).where(Job.request_campaign_id == campaign.id)
        jobs = list((await session.execute(stmt)).scalars().all())
        for job in jobs:
            for message_id in list(job.telegram_service_message_ids or []):
                await safe_delete_message(bot, job.telegram_chat_id, message_id)
            job.telegram_service_message_ids = []
        await session.commit()

    async def cancel_campaign(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        bot: Bot,
    ) -> None:
        campaign.status = "canceled"
        targets = await self.list_targets(session, campaign.id)
        for target in targets:
            if target.status not in REQUEST_TARGET_TERMINAL_STATUSES:
                target.status = "canceled"
        stmt = select(Job).where(Job.request_campaign_id == campaign.id)
        jobs = list((await session.execute(stmt)).scalars().all())
        now = datetime.now(timezone.utc)
        for job in jobs:
            if job.status not in REQUEST_JOB_TERMINAL_STATUSES:
                job.status = "canceled"
                job.call_status = "canceled"
                job.completed_at = job.completed_at or now
        await session.commit()
        await self.cleanup_service_messages(session=session, campaign=campaign, bot=bot)

    async def cancel_input_campaigns_for_owner(
        self,
        *,
        session: AsyncSession,
        chat_id: int,
        user_id: int,
        bot: Bot,
    ) -> int:
        stmt = select(RequestCallCampaign).where(
            RequestCallCampaign.telegram_chat_id == chat_id,
            RequestCallCampaign.telegram_user_id == user_id,
            RequestCallCampaign.status.in_(REQUEST_CAMPAIGN_INPUT_STATUSES),
        )
        campaigns = list((await session.execute(stmt)).scalars().all())
        for campaign in campaigns:
            logger.info(
                "request-call stale input campaign canceled before new session",
                extra={"campaign_id": campaign.id, "chat_id": chat_id, "user_id": user_id, "status": campaign.status},
            )
            await self.cancel_campaign(session=session, campaign=campaign, bot=bot)
        return len(campaigns)

    async def get_running_campaign_for_owner(
        self,
        *,
        session: AsyncSession,
        chat_id: int,
        user_id: int,
    ) -> RequestCallCampaign | None:
        stmt = (
            select(RequestCallCampaign)
            .where(
                RequestCallCampaign.telegram_chat_id == chat_id,
                RequestCallCampaign.telegram_user_id == user_id,
                RequestCallCampaign.status.in_(REQUEST_CAMPAIGN_RUNNING_STATUSES),
            )
            .order_by(RequestCallCampaign.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def update_campaign_from_text(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        text: str,
        source_message_id: int | None = None,
    ) -> RequestCallCampaign:
        selected_region = (campaign.phone_region or "").upper()
        if selected_region not in {"US", "JP"}:
            campaign.status = "needs_country"
            campaign.telegram_source_message_id = source_message_id or campaign.telegram_source_message_id
            campaign.raw_input = "\n".join(part for part in (campaign.raw_input, text) if part).strip()
            campaign.raw_user_goal = campaign.raw_input
            await session.commit()
            await session.refresh(campaign)
            return campaign

        parsed = parse_request_call_input(text, default_region=selected_region)
        campaign.telegram_source_message_id = source_message_id or campaign.telegram_source_message_id

        # Follow-up messages are incremental: a user may first send phones, then a
        # goal, or clarify the goal after LLM validation. Keep the existing side
        # of the campaign unless the new message actually contains replacements.
        campaign.raw_input = "\n".join(part for part in (campaign.raw_input, text) if part).strip()
        campaign.raw_user_goal = campaign.raw_input
        if parsed.source_urls:
            campaign.source_urls_json = self._merge_source_urls(campaign.source_urls_json or [], parsed.source_urls)
            campaign.vehicle_context_json = await self.context_extractor.extract_many(campaign.source_urls_json)
        if parsed.dealers:
            await self._replace_targets(session, campaign, parsed)

        campaign.rejected_phones_json = [row.__dict__ for row in parsed.rejected_phones]
        await self._add_targets_from_vehicle_context_if_needed(session, campaign)
        await self._refresh_counts(session, campaign)
        has_wrong_country_phone = any(
            str(row.reason).startswith("wrong_country_expected_") for row in parsed.rejected_phones
        )

        has_context = _has_vehicle_context(campaign.vehicle_context_json or [])
        has_goal_text = _has_request_goal_text(campaign.raw_input, selected_region)
        if parsed.has_mixed_phone_regions or has_wrong_country_phone:
            campaign.status = "mixed_phone_regions"
        elif not campaign.valid_numbers and not has_goal_text and not has_context:
            campaign.status = "needs_phones_and_goal"
        elif not campaign.valid_numbers:
            campaign.status = "needs_phones"
        elif not has_goal_text and not has_context:
            campaign.status = "needs_goal"
        elif not campaign.call_language:
            campaign.call_language = "ja" if selected_region == "JP" else "en"
            await self._generate_target_goals(session, campaign)
        else:
            await self._generate_target_goals(session, campaign)

        await session.commit()
        await session.refresh(campaign)
        return campaign

    @staticmethod
    def _merge_source_urls(existing: list[str], incoming: list[str]) -> list[str]:
        result: list[str] = []
        for url in [*existing, *incoming]:
            cleaned = (url or "").strip()
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    @staticmethod
    def _region_from_e164(phone_e164: str | None) -> str | None:
        if not phone_e164:
            return None
        try:
            parsed = phonenumbers.parse(phone_e164, None)
        except NumberParseException:
            return None
        return phonenumbers.region_code_for_number(parsed) or f"CC{parsed.country_code}"

    async def _has_mixed_target_regions(self, session: AsyncSession, campaign_id: int) -> bool:
        return False

    async def _add_targets_from_vehicle_context_if_needed(
        self,
        session: AsyncSession,
        campaign: RequestCallCampaign,
    ) -> None:
        if (await self.list_targets(session, campaign.id)):
            return
        selected_region = (campaign.phone_region or "").upper()
        contexts = campaign.vehicle_context_json or []
        for context in contexts:
            raw_phone = context.get("dealer_phone")
            source_url = context.get("source_url") or ""
            if selected_region == "JP":
                if is_special_or_proxy_phone(raw_phone):
                    continue
                phone_e164, phone_region = _normalize_allowed_phone(raw_phone, "JP")
            else:
                phone_e164, phone_region = _normalize_allowed_phone(raw_phone or "", selected_region or "US")
            phone_region = phone_region or self._region_from_e164(phone_e164)
            if not phone_e164 or not phone_region or (selected_region in {"US", "JP"} and phone_region != selected_region):
                continue
            dealer_name = _clean_spaces(context.get("dealer_name") or "Dealer from URL")
            session.add(
                DealerCallTarget(
                    campaign_id=campaign.id,
                    dealer_name=dealer_name,
                    city=None,
                    phone_raw=raw_phone,
                    phone_e164=phone_e164,
                    phone_region=phone_region,
                    original_line=f"{dealer_name} {raw_phone} ({source_url})",
                    status="pending",
                )
            )
            await session.flush()
            return

    async def _replace_targets(
        self,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        parsed: ParsedRequestInput,
    ) -> None:
        await session.execute(delete(DealerCallTarget).where(DealerCallTarget.campaign_id == campaign.id))
        for row in parsed.dealers:
            session.add(
                DealerCallTarget(
                    campaign_id=campaign.id,
                    dealer_name=row.dealer_name,
                    city=row.city,
                    phone_raw=row.phone_raw,
                    phone_e164=row.phone_e164,
                    phone_region=row.phone_region,
                    original_line=row.original_line,
                    status="pending",
                )
            )
        await session.flush()

    async def _refresh_counts(self, session: AsyncSession, campaign: RequestCallCampaign) -> None:
        targets = await self.list_targets(session, campaign.id)
        rejected = campaign.rejected_phones_json or []
        campaign.valid_numbers = len(targets)
        campaign.invalid_numbers = len(rejected)
        campaign.total_numbers = campaign.valid_numbers + campaign.invalid_numbers
        regions = {target.phone_region for target in targets if target.phone_region}
        if regions:
            campaign.phone_region = next(iter(regions)) if len(regions) == 1 else None

    async def _generate_target_goals(self, session: AsyncSession, campaign: RequestCallCampaign) -> None:
        targets = await self.list_targets(session, campaign.id)
        if not targets:
            campaign.status = "needs_phones"
            return

        result = await self._generate_campaign_goal(
            campaign,
            targets,
            call_language=campaign.call_language or "en",
            vehicle_context=campaign.vehicle_context_json or [],
        )
        status = (result.status or "").strip().lower()
        if status == "needs_goal_clarification" or status not in READY_GOAL_STATUSES or not result.goal_ru:
            campaign.status = "needs_goal_clarification"
            campaign.goal_meta_json = result.model_dump()
            return

        result.status = "ready"
        contexts = await self._phone_contexts_by_phone(session, [target.phone_e164 for target in targets])
        for target in targets:
            target.goal_ru = _goal_with_phone_context(
                result.goal_ru,
                contexts.get(target.phone_e164),
                campaign.call_language,
            )
        first_meta = result.model_dump()
        campaign.goal_meta_json = first_meta
        campaign.normalized_goal_summary = self._goal_summary(campaign.raw_user_goal or "", first_meta)
        campaign.status = "ready_to_confirm"

    async def set_language_and_generate_goals(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        call_language: str,
    ) -> RequestCallCampaign:
        normalized_language = "ja" if call_language == "ja" else "en"
        existing_targets = await self.list_targets(session, campaign.id)
        existing_goal_status = str((campaign.goal_meta_json or {}).get("status") or "").strip().lower()
        if (
            campaign.call_language == normalized_language
            and campaign.status == "ready_to_confirm"
            and existing_goal_status in READY_GOAL_STATUSES
            and existing_targets
            and all((target.goal_ru or "").strip() for target in existing_targets)
        ):
            return campaign

        campaign.call_language = normalized_language
        if not campaign.phone_region:
            campaign.phone_region = "JP" if campaign.call_language == "ja" else "US"
        has_context = _has_vehicle_context(campaign.vehicle_context_json or [])
        has_goal_text = _has_request_goal_text(campaign.raw_input or campaign.raw_user_goal, campaign.phone_region)
        if not campaign.valid_numbers:
            campaign.status = "needs_phones"
        elif not has_goal_text and not has_context:
            campaign.status = "needs_goal"
        else:
            await self._generate_target_goals(session, campaign)
        await session.commit()
        await session.refresh(campaign)
        return campaign

    async def _generate_campaign_goal(
        self,
        campaign: RequestCallCampaign,
        targets: list[DealerCallTarget],
        *,
        call_language: str,
        vehicle_context: list[dict[str, Any]],
    ) -> GoalGenerationResult:
        dealer_targets = [
            {
                "dealer_name": target.dealer_name,
                "city": target.city,
                "phone_e164": target.phone_e164,
                "phone_region": target.phone_region,
            }
            for target in targets
        ]
        try:
            result = await self.openai_service.generate_goal_ru(
                dealer_name="",
                city=None,
                phone_e164="",
                raw_user_goal=campaign.raw_user_goal or campaign.raw_input or "",
                call_language=call_language,
                vehicle_context=vehicle_context,
                dealer_targets=dealer_targets,
            )
            return self._validate_campaign_goal(campaign, targets, result)
        except Exception:
            logger.exception(
                "request-call campaign goal generation failed",
                extra={"campaign_id": campaign.id},
            )
            return GoalGenerationResult(
                status="needs_goal_clarification",
                goal_ru=None,
                target_vehicle=None,
                main_intent=None,
                constraints=[],
                required_questions=[],
                fallback_questions=[],
                completion_criteria=[],
                clarification_questions=[
                    "Не удалось надёжно сформировать цель прозвона. Пришлите задачу ещё раз или уточните её."
                ],
            )

    @staticmethod
    def _validate_campaign_goal(
        campaign: RequestCallCampaign,
        targets: list[DealerCallTarget],
        result: GoalGenerationResult,
    ) -> GoalGenerationResult:
        goal_text = result.goal_ru or ""
        status = (result.status or "").strip().lower()
        if status == "needs_goal_clarification" or status not in READY_GOAL_STATUSES:
            return result
        if not goal_text or _word_count(goal_text) > REQUEST_GOAL_MAX_WORDS:
            result.status = "needs_goal_clarification"
            result.goal_ru = None
            result.clarification_questions = result.clarification_questions or [
                "Сформулируйте цель короче или уточните, что именно нужно сказать/спросить."
            ]
            return result
        lower_goal = goal_text.lower()
        for target in targets:
            dealer_name = (target.dealer_name or "").strip()
            if dealer_name and dealer_name.lower() in lower_goal:
                result.status = "needs_goal_clarification"
                result.goal_ru = None
                result.clarification_questions = result.clarification_questions or [
                    "Цель не должна содержать точное название дилера. Переформулируйте задачу без названия дилера."
                ]
                logger.warning(
                    "request-call goal rejected because it contains exact dealer name",
                    extra={"campaign_id": campaign.id, "target_id": target.id, "dealer_name": target.dealer_name},
                )
                return result
        result.status = "ready"
        return result

    @staticmethod
    def _goal_summary(raw_goal: str, meta: dict[str, Any]) -> str:
        vehicle = meta.get("target_vehicle") or _compact_value(raw_goal, limit=80) or "задача прозвона"
        intent = meta.get("main_intent") or "наличие/условия"
        constraints = ", ".join(meta.get("constraints") or []) or "условия из сообщения"
        return f"{vehicle}. {intent}. {constraints}."

    async def list_targets(self, session: AsyncSession, campaign_id: int) -> list[DealerCallTarget]:
        stmt = select(DealerCallTarget).where(DealerCallTarget.campaign_id == campaign_id).order_by(DealerCallTarget.id)
        return list((await session.execute(stmt)).scalars().all())

    async def list_reports(self, session: AsyncSession, campaign_id: int) -> list[CallReport]:
        stmt = select(CallReport).where(CallReport.campaign_id == campaign_id).order_by(CallReport.id)
        return list((await session.execute(stmt)).scalars().all())

    async def _phone_contexts_by_phone(
        self,
        session: AsyncSession,
        phones: list[str],
    ) -> dict[str, DealerPhoneContext]:
        unique_phones = sorted({phone for phone in phones if phone})
        if not unique_phones:
            return {}
        stmt = select(DealerPhoneContext).where(DealerPhoneContext.phone_e164.in_(unique_phones))
        rows = list((await session.execute(stmt)).scalars().all())
        return {row.phone_e164: row for row in rows}

    async def start_next_call(self, *, session: AsyncSession, campaign: RequestCallCampaign, bot: Bot) -> Job | None:
        await session.refresh(campaign)
        if campaign.status in REQUEST_CAMPAIGN_TERMINAL_STATUSES:
            logger.info(
                "request-call next call skipped: campaign terminal",
                extra={"campaign_id": campaign.id, "status": campaign.status},
            )
            return None
        targets = await self.list_targets(session, campaign.id)
        pending = [target for target in targets if target.status == "pending"]
        if not pending:
            campaign.status = "completed"
            await session.commit()
            await self.send_campaign_summary(session=session, campaign=campaign, bot=bot)
            return None

        target = pending[0]
        index = targets.index(target) + 1
        call_language = campaign.call_language or "en"
        await self.send_service_message(
            session=session,
            campaign=campaign,
            bot=bot,
            text=f"Звонок {index} из {len(targets)}: {target.dealer_name}.",
        )

        job = Job(
            telegram_chat_id=campaign.telegram_chat_id,
            telegram_user_id=campaign.telegram_user_id,
            telegram_source_message_id=campaign.telegram_source_message_id,
            telegram_service_message_ids=[],
            listing_url=f"request-call://campaign/{campaign.id}/target/{target.id}",
            source="request_call",
            status="creating_call",
            provider="twilio",
            dealer=target.dealer_name,
            extracted_phone=target.phone_e164,
            call_phone=target.phone_e164,
            call_language=call_language,
            max_attempts=1,
            request_campaign_id=campaign.id,
            request_target_id=target.id,
            request_goal_ru=target.goal_ru,
        )
        session.add(job)
        await session.flush()
        target.status = "creating_call"
        target.attempt = int(target.attempt or 0) + 1
        target.last_call_job_id = job.id
        campaign.status = "calling"
        await session.commit()

        dynamic_variables = {"goal_ru": target.goal_ru or ""}
        agent_id = (
            self.settings.elevenlabs_request_agent_id_ja
            if call_language == "ja"
            else self.settings.elevenlabs_request_agent_id
        )
        if call_language == "ja" and not (agent_id or "").strip():
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_message = "ELEVENLABS_REQUEST_AGENT_ID_JA is not configured"
            job.last_error_hint = "Добавьте ELEVENLABS_REQUEST_AGENT_ID_JA в .env для японского request-call агента."
            target.status = "call_create_failed"
            campaign.status = "waiting_next"
            await session.commit()
            await self._create_status_report(
                session=session,
                campaign=campaign,
                target=target,
                call_status="call_create_failed",
                summary="Японский request-call агент ElevenLabs не настроен.",
                next_action=job.last_error_hint,
            )
            await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)
            return job
        logger.info(
            "request-call outbound dynamic variables prepared",
            extra={
                "campaign_id": campaign.id,
                "target_id": target.id,
                "dealer_name": target.dealer_name,
                "phone_e164": target.phone_e164,
                "call_language": call_language,
                "agent_kind": "request_ja" if call_language == "ja" else "request_en",
                "phone_region": target.phone_region,
                "dynamic_variable_keys": sorted(dynamic_variables.keys()),
            },
        )
        try:
            payload = await self.elevenlabs_service.start_outbound_call(
                call_phone=target.phone_e164,
                dynamic_variables=dynamic_variables,
                agent_id_override=agent_id or None,
            )
        except ProviderCallCreateError as exc:
            hint, _can_retry = classify_twilio_create_failure(
                http_status=exc.http_status,
                provider_error_code=exc.provider_error_code or extract_twilio_error_code(exc.provider_error_message),
                provider_error_message=exc.provider_error_message,
            )
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_code = exc.provider_error_code or extract_twilio_error_code(exc.provider_error_message)
            job.last_error_message = exc.provider_error_message
            job.last_error_hint = hint
            target.status = "call_create_failed"
            campaign.status = "waiting_next"
            await session.commit()
            await self._create_status_report(
                session=session,
                campaign=campaign,
                target=target,
                call_status="call_create_failed",
                summary=f"Не удалось создать звонок: {exc.provider_error_message}",
                next_action=hint,
                raw={"provider_error": sanitize_payload(exc.payload_without_secrets or {})},
            )
            await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)
            return job

        call_sid = payload.get("callSid")
        if not call_sid:
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_message = "Provider returned success without CallSid"
            target.status = "call_create_failed"
            campaign.status = "waiting_next"
            await session.commit()
            await self._create_status_report(
                session=session,
                campaign=campaign,
                target=target,
                call_status="call_create_failed",
                summary="Провайдер не вернул CallSid, звонок не считается созданным.",
                next_action="Проверить provider response.",
            )
            await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)
            return job

        now = datetime.now(timezone.utc)
        job.elevenlabs_conversation_id = payload.get("conversation_id")
        job.elevenlabs_call_sid = call_sid
        job.provider_call_sid = call_sid
        job.call_status = "call_created"
        job.status = "call_created"
        job.started_at = now
        job.last_attempt_at = now
        job.last_progress_at = now
        target.status = "waiting_call_result"
        campaign.status = "waiting_call_result"
        await session.commit()
        await self.send_service_message(
            session=session,
            campaign=campaign,
            bot=bot,
            text=f"Звонок создан. CallSid: {call_sid}\nОжидаю результат звонка.",
        )
        logger.info(
            "request-call outbound created",
            extra={"campaign_id": campaign.id, "target_id": target.id, "job_id": job.id, "call_sid": call_sid},
        )
        return job

    async def finalize_job_from_transcript(
        self,
        *,
        session: AsyncSession,
        job: Job,
        bot: Bot | None,
        transcript: str,
        summary: str,
    ) -> None:
        if not job.request_campaign_id or not job.request_target_id:
            return
        if not await _try_acquire_request_call_finalization_lock(session, job.id):
            logger.info(
                "request-call finalization skipped: another worker holds lock",
                extra={"job_id": job.id},
            )
            return
        await session.refresh(job)
        campaign = await session.get(RequestCallCampaign, job.request_campaign_id)
        target = await session.get(DealerCallTarget, job.request_target_id)
        if not campaign or not target:
            return
        if campaign.status == "canceled":
            logger.info(
                "request-call finalization skipped: campaign canceled",
                extra={"job_id": job.id, "campaign_id": campaign.id, "target_id": target.id},
            )
            if job.status not in REQUEST_JOB_TERMINAL_STATUSES:
                job.status = "canceled"
                job.call_status = "canceled"
                await session.commit()
            return
        existing_report = await self._latest_target_report(
            session=session,
            campaign_id=campaign.id,
            target_id=target.id,
        )
        if existing_report is not None:
            logger.info(
                "request-call finalization skipped: target already has report",
                extra={
                    "job_id": job.id,
                    "campaign_id": campaign.id,
                    "target_id": target.id,
                    "report_id": existing_report.id,
                },
            )
            if bot is not None and job.final_report_error and not job.final_report_sent_at:
                await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)
            return
        report = await self._extract_call_report(transcript=transcript, goal_ru=job.request_goal_ru or target.goal_ru or "")
        report_row = await self._persist_report(session=session, campaign=campaign, target=target, report=report)
        await self._update_dealer_phone_context(
            session=session,
            campaign=campaign,
            target=target,
            report=report_row,
            goal_ru=job.request_goal_ru or target.goal_ru or "",
        )
        target.status = report.call_status if report.call_status != "completed" else "completed"
        job.status = "completed"
        job.call_status = report.call_status
        job.call_transcript = transcript
        job.call_summary = summary or report.summary
        job.completed_at = datetime.now(timezone.utc)
        campaign.status = "completed" if await self._is_campaign_done(session, campaign.id) else "waiting_next"
        await session.commit()
        if bot is not None:
            await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)

    async def finalize_job_status(
        self,
        *,
        session: AsyncSession,
        job: Job,
        bot: Bot | None,
        call_status: str,
        summary: str,
    ) -> None:
        if not job.request_campaign_id or not job.request_target_id:
            return
        if not await _try_acquire_request_call_finalization_lock(session, job.id):
            logger.info(
                "request-call status finalization skipped: another worker holds lock",
                extra={"job_id": job.id, "call_status": call_status},
            )
            return
        await session.refresh(job)
        campaign = await session.get(RequestCallCampaign, job.request_campaign_id)
        target = await session.get(DealerCallTarget, job.request_target_id)
        if not campaign or not target:
            return
        if campaign.status == "canceled":
            logger.info(
                "request-call status finalization skipped: campaign canceled",
                extra={"job_id": job.id, "campaign_id": campaign.id, "target_id": target.id, "call_status": call_status},
            )
            if job.status not in REQUEST_JOB_TERMINAL_STATUSES:
                job.status = "canceled"
                job.call_status = "canceled"
                await session.commit()
            return
        existing_report = await self._latest_target_report(
            session=session,
            campaign_id=campaign.id,
            target_id=target.id,
        )
        if existing_report is not None:
            logger.info(
                "request-call status finalization skipped: target already has report",
                extra={
                    "job_id": job.id,
                    "campaign_id": campaign.id,
                    "target_id": target.id,
                    "report_id": existing_report.id,
                    "call_status": call_status,
                },
            )
            if bot is not None and job.final_report_error and not job.final_report_sent_at:
                await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)
            return
        next_action = None
        if call_status in {"no_answer", "busy"}:
            next_action = "Можно перейти к следующему дилеру."
        elif call_status == "provider_timeout":
            next_action = "Проверить Twilio/ElevenLabs: звонок был создан, но не дошёл до реального вызова."
        await self._create_status_report(
            session=session,
            campaign=campaign,
            target=target,
            call_status=call_status,
            summary=summary,
            next_action=next_action,
        )
        target.status = call_status
        job.status = call_status
        job.call_status = call_status
        job.completed_at = datetime.now(timezone.utc)
        campaign.status = "completed" if await self._is_campaign_done(session, campaign.id) else "waiting_next"
        await session.commit()
        if bot is not None:
            await self.send_target_report(session=session, campaign=campaign, target=target, bot=bot, job=job)

    async def _latest_target_report(
        self,
        *,
        session: AsyncSession,
        campaign_id: int,
        target_id: int,
    ) -> CallReport | None:
        stmt = (
            select(CallReport)
            .where(CallReport.campaign_id == campaign_id, CallReport.target_id == target_id)
            .order_by(CallReport.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _mark_report_delivery(self, *, session: AsyncSession, job: Job | None, success: bool) -> None:
        if job is None:
            return
        now = datetime.now(timezone.utc)
        if success:
            job.final_report_sent_at = now
            job.final_report_error = None
            job.next_notification_retry_at = None
        else:
            job.notification_attempt_count = int(job.notification_attempt_count or 0) + 1
            delay_min = min(60, 2 ** min(5, job.notification_attempt_count))
            job.final_report_error = "Telegram request-call report delivery failed after retries"
            job.next_notification_retry_at = now + timedelta(minutes=delay_min)
        await session.commit()

    async def _extract_call_report(self, *, transcript: str, goal_ru: str) -> RequestCallReportResult:
        try:
            return await self.openai_service.extract_request_call_report(transcript=transcript, goal_ru=goal_ru)
        except Exception:
            logger.exception("request-call report extraction failed")
            return RequestCallReportResult(
                call_status="completed",
                reached_sales=None,
                summary="Звонок завершён, но структурный анализ не удался.",
                important_notes=(transcript or "")[:1000],
                next_action="Проверить транскрипт вручную.",
            )

    async def _persist_report(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        target: DealerCallTarget,
        report: RequestCallReportResult,
    ) -> CallReport:
        row = CallReport(
            campaign_id=campaign.id,
            target_id=target.id,
            dealer_name=target.dealer_name,
            phone_e164=target.phone_e164,
            call_status=report.call_status,
            reached_sales=report.reached_sales,
            target_vehicle_or_task=report.target_vehicle_or_task,
            summary=report.summary,
            availability_result=report.availability_result,
            incoming_result=report.incoming_result,
            price_result=report.price_result,
            configuration_result=report.configuration_result,
            vin_or_stock_result=report.vin_or_stock_result,
            payment_result=report.payment_result,
            paperwork_result=report.paperwork_result,
            important_notes=report.important_notes,
            next_action=report.next_action,
            ai_quality_score=report.ai_quality_score,
            ai_quality_reason=report.ai_quality_reason,
            raw_report_json=report.model_dump(),
        )
        session.add(row)
        await session.flush()
        return row

    async def _update_dealer_phone_context(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        target: DealerCallTarget,
        report: CallReport,
        goal_ru: str,
    ) -> DealerPhoneContext | None:
        if not target.phone_e164 or not _is_meaningful_phone_context_report(report):
            return None
        stmt = select(DealerPhoneContext).where(DealerPhoneContext.phone_e164 == target.phone_e164).limit(1)
        context = (await session.execute(stmt)).scalar_one_or_none()
        if context is None:
            context = DealerPhoneContext(
                phone_e164=target.phone_e164,
                phone_region=target.phone_region or campaign.phone_region,
                successful_call_count=0,
                context_items_json=[],
            )
            session.add(context)
            await session.flush()

        now = datetime.now(timezone.utc)
        item = {
            "called_at": now.isoformat(timespec="seconds"),
            "campaign_id": campaign.id,
            "target_id": target.id,
            "report_id": report.id,
            "call_language": campaign.call_language or "en",
            "dealer_name": target.dealer_name,
            "goal_summary": _compact_value(goal_ru, limit=260),
            "summary": _compact_value(report.summary, limit=220),
            "availability": _compact_value(report.availability_result, limit=160),
            "incoming": _compact_value(report.incoming_result, limit=160),
            "price": _compact_value(report.price_result, limit=160),
            "configuration": _compact_value(report.configuration_result, limit=160),
            "vin_or_stock": _compact_value(report.vin_or_stock_result, limit=160),
            "payment": _compact_value(report.payment_result, limit=160),
            "paperwork": _compact_value(report.paperwork_result, limit=160),
            "important_notes": _compact_value(report.important_notes, limit=180),
            "next_action": _compact_value(report.next_action, limit=180),
        }
        previous_items = context.context_items_json or []
        already_recorded = any(row.get("report_id") == report.id for row in previous_items)
        items = [item]
        items.extend(row for row in previous_items if row.get("report_id") != report.id)
        items = items[:PHONE_CONTEXT_MAX_ITEMS]
        context.phone_region = target.phone_region or campaign.phone_region or context.phone_region
        context.last_dealer_name = target.dealer_name
        context.last_campaign_id = campaign.id
        context.last_target_id = target.id
        context.last_report_id = report.id
        context.last_called_at = now
        if not already_recorded:
            context.successful_call_count = int(context.successful_call_count or 0) + 1
        context.context_items_json = items
        context.context_summary = _build_phone_context_summary(items)
        await session.flush()
        logger.info(
            "dealer phone context updated",
            extra={
                "campaign_id": campaign.id,
                "target_id": target.id,
                "report_id": report.id,
                "phone_e164": target.phone_e164,
                "items": len(items),
            },
        )
        return context

    async def _create_status_report(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        target: DealerCallTarget,
        call_status: str,
        summary: str,
        next_action: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> CallReport:
        row = CallReport(
            campaign_id=campaign.id,
            target_id=target.id,
            dealer_name=target.dealer_name,
            phone_e164=target.phone_e164,
            call_status=call_status,
            reached_sales=False,
            summary=summary,
            availability_result="not_answered",
            incoming_result="not_answered",
            price_result="not_answered",
            configuration_result="not_answered",
            vin_or_stock_result="not_answered",
            payment_result="not_answered",
            paperwork_result="not_answered",
            next_action=next_action,
            raw_report_json=raw or {"call_status": call_status, "summary": summary},
        )
        session.add(row)
        await session.flush()
        return row

    async def _is_campaign_done(self, session: AsyncSession, campaign_id: int) -> bool:
        targets = await self.list_targets(session, campaign_id)
        return bool(targets) and all(target.status not in {"pending", "creating_call", "waiting_call_result"} for target in targets)

    async def send_target_report(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        target: DealerCallTarget,
        bot: Bot,
        job: Job | None = None,
    ) -> bool:
        reports = [row for row in await self.list_reports(session, campaign.id) if row.target_id == target.id]
        report = reports[-1] if reports else None
        if not report:
            return False
        if campaign.status == "canceled":
            logger.info(
                "request-call report skipped: campaign canceled",
                extra={"campaign_id": campaign.id, "target_id": target.id},
            )
            return False
        report_html = build_request_target_report_html(target, report, campaign=campaign)
        targets = await self.list_targets(session, campaign.id)
        remaining = any(row.status == "pending" for row in targets)
        if job is not None:
            await self._send_request_call_audio(campaign=campaign, target=target, job=job, bot=bot)
        sent = await safe_send_message(bot, campaign.telegram_chat_id, report_html, parse_mode="HTML")
        success = bool(sent)
        await session.refresh(campaign)
        if campaign.status == "canceled":
            await self._mark_report_delivery(session=session, job=job, success=success)
            return success
        if remaining and success:
            if (campaign.call_sequence_mode or "manual") == "auto":
                await self.send_service_message(
                    session=session,
                    campaign=campaign,
                    bot=bot,
                    text="Автоматический режим: запускаю следующий звонок.",
                )
                await self.start_next_call(session=session, campaign=campaign, bot=bot)
            else:
                prompt = await self.send_service_message(
                    session=session,
                    campaign=campaign,
                    bot=bot,
                    text="Выберите следующее действие:",
                    reply_markup=request_call_next_keyboard(campaign.id),
                )
                success = bool(prompt)
        elif not remaining:
            await self.cleanup_service_messages(session=session, campaign=campaign, bot=bot)
            summary_sent = await self.send_campaign_summary(session=session, campaign=campaign, bot=bot)
            success = bool(sent) and summary_sent
        await self._mark_report_delivery(session=session, job=job, success=success)
        return success

    async def _send_request_call_audio(
        self,
        *,
        campaign: RequestCallCampaign,
        target: DealerCallTarget,
        job: Job,
        bot: Bot,
    ) -> bool:
        if not job.elevenlabs_conversation_id:
            return False
        audio: bytes | None = None
        for attempt in range(1, 4):
            try:
                audio = await self.elevenlabs_service.fetch_conversation_audio(job.elevenlabs_conversation_id)
                if audio:
                    break
            except Exception as exc:
                logger.warning(
                    "request-call audio fetch failed",
                    extra={
                        "job_id": job.id,
                        "conversation_id": job.elevenlabs_conversation_id,
                        "status": f"attempt {attempt}/3: {type(exc).__name__}: {exc}",
                    },
                )
            if attempt < 3:
                await asyncio.sleep(2)
        if not audio:
            logger.warning(
                "request-call audio is not available",
                extra={"job_id": job.id, "conversation_id": job.elevenlabs_conversation_id},
            )
            return False
        document = BufferedInputFile(audio, filename=f"request_call_{job.id}.mp3")
        sent = await safe_send_document(
            bot,
            campaign.telegram_chat_id,
            document,
            caption=f"Аудио звонка: {target.dealer_name}",
            reply_to_message_id=campaign.telegram_source_message_id,
        )
        return bool(sent)

    async def send_campaign_summary(self, *, session: AsyncSession, campaign: RequestCallCampaign, bot: Bot) -> bool:
        await session.refresh(campaign)
        if campaign.status == "canceled":
            return False
        reports = await self.list_reports(session, campaign.id)
        targets = await self.list_targets(session, campaign.id)
        if not reports and targets:
            return False
        await self.cleanup_service_messages(session=session, campaign=campaign, bot=bot)
        campaign.status = "completed"
        await session.commit()
        chunks = build_request_campaign_summary_html_chunks(reports)
        sent_all = True
        for chunk in chunks:
            sent = await safe_send_message(bot, campaign.telegram_chat_id, chunk, parse_mode="HTML")
            sent_all = bool(sent) and sent_all
        return sent_all


def build_request_confirmation_text(campaign: RequestCallCampaign, targets: list[DealerCallTarget]) -> str:
    lines = [f"Нашёл {campaign.valid_numbers} валидных номеров.", "", "Дилеры:"]
    for idx, target in enumerate(targets, start=1):
        city = f", {target.city}" if target.city else ""
        lines.append(f"{idx}. {target.dealer_name}{city}, {target.phone_e164}")
    targets_with_context = [
        target.phone_e164
        for target in targets
        if any(prefix in (target.goal_ru or "") for prefix in PHONE_CONTEXT_PREFIXES.values())
    ]
    if targets_with_context:
        lines.extend(["", f"Есть предыдущий контекст по номеру: {', '.join(targets_with_context)}"])
    language_label = "японский" if campaign.call_language == "ja" else "английский"
    lines.extend(["", f"Язык звонка: {language_label}"])
    context_summary = _vehicle_context_summary(campaign.vehicle_context_json or [])
    if context_summary:
        lines.extend(["", f"Контекст из ссылок: {context_summary}"])
    agent_goal = next((target.goal_ru for target in targets if target.goal_ru), None)
    goal_language = "JA" if campaign.call_language == "ja" else "EN"
    lines.extend(["", f"Цель для агента ({goal_language}):", agent_goal or campaign.normalized_goal_summary or "—"])
    lines.extend(["", "Ключевые вопросы:", ", ".join(REQUEST_CONFIRMATION_QUESTION_LABELS)])
    rejected = campaign.rejected_phones_json or []
    if rejected:
        lines.extend(["", "Не смог распознать эти номера:"])
        for row in rejected:
            lines.append(f"- {row.get('original_line')}, причина: {row.get('reason')}")
    lines.extend(["", f"Выберите режим запуска прозвона {campaign.valid_numbers} номеров."])
    return "\n".join(lines)


def _vehicle_context_summary(contexts: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    for context in contexts[:2]:
        title = _compact_value(context.get("vehicle_title"), limit=90)
        if title:
            parts.append(title)
        if not title:
            make_model = _compact_value(
                " ".join(
                    part
                    for part in (
                        context.get("year"),
                        context.get("make"),
                        context.get("model"),
                        context.get("trim"),
                    )
                    if part
                ),
                limit=90,
            )
            if make_model:
                parts.append(make_model)
        for label, key in (
            ("цвет", "color"),
            ("пробег", "mileage"),
            ("цена", "price"),
            ("VIN", "vin"),
            ("stock", "stock_number"),
            ("телефон дилера", "dealer_phone"),
            ("адрес дилера", "dealer_address"),
        ):
            value = _compact_value(context.get(key), limit=50)
            if value:
                parts.append(f"{label}: {value}")
    if not parts:
        return None
    return _compact_value(", ".join(parts), limit=280)
