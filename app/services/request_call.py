from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import phonenumbers
from aiogram import Bot
from phonenumbers import NumberParseException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import request_call_next_keyboard
from app.config import Settings
from app.models import CallReport, DealerCallTarget, Job, RequestCallCampaign
from app.schemas import GoalGenerationResult, RequestCallReportResult
from app.services.call_state import classify_twilio_create_failure, extract_twilio_error_code, sanitize_payload
from app.services.elevenlabs_client import ElevenLabsService, ProviderCallCreateError
from app.services.openai_client import OpenAIService
from app.services.request_call_context import RequestCallContextExtractor
from app.services.telegram_delivery import safe_delete_message, safe_send_message
from app.utils.phone import is_special_or_proxy_phone, normalize_jp_phone_to_e164, normalize_us_phone_to_e164

logger = logging.getLogger(__name__)

PHONE_LIKE_RE = re.compile(r"(?:\+?\d[\d\s\-\(\)\.]{6,}\d)")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
PHONE_MATCH_REGIONS = ("US", "JP", "RU", "GB", "FR", "DE", "AE", "KZ", "TR", "KR", "CN")
HEADER_HINTS = ("номер телефона", "phone", "телефон", "официальный дилер", "dealer")
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
VAGUE_GOALS = {
    "узнать по машинам",
    "позвонить дилерам",
    "прозвонить дилеров",
    "узнать по авто",
    "узнать по автомобилям",
}
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
DEALER_BRAND_LABELS = (
    ("ram", "a RAM dealership"),
    ("dodge", "a RAM dealership"),
    ("chrysler", "a RAM dealership"),
    ("ford", "a Ford dealership"),
    ("форд", "a Ford dealership"),
    ("toyota", "a Toyota dealership"),
    ("lexus", "a Lexus dealership"),
    ("bmw", "a BMW dealership"),
    ("mercedes", "a Mercedes-Benz dealership"),
    ("mercedes-benz", "a Mercedes-Benz dealership"),
    ("chevrolet", "a Chevrolet dealership"),
    ("chevy", "a Chevrolet dealership"),
    ("tesla", "a Tesla dealership"),
    ("porsche", "a Porsche dealership"),
    ("honda", "a Honda dealership"),
    ("nissan", "a Nissan dealership"),
    ("audi", "an Audi dealership"),
    ("jeep", "a Jeep dealership"),
    ("gmc", "a GMC dealership"),
    ("cadillac", "a Cadillac dealership"),
    ("hyundai", "a Hyundai dealership"),
    ("kia", "a Kia dealership"),
)

REQUEST_CALL_FINALIZATION_LOCK_CLASS_ID = 62041


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

    @property
    def phone_regions(self) -> set[str]:
        return {dealer.phone_region for dealer in self.dealers if dealer.phone_region}

    @property
    def has_mixed_phone_regions(self) -> bool:
        return False

    @property
    def status(self) -> str:
        has_phones = bool(self.dealers)
        has_goal = bool(self.raw_user_goal.strip())
        if has_phones and has_goal:
            return "ready_to_confirm"
        if has_goal:
            return "needs_phones"
        if has_phones:
            return "needs_goal"
        return "needs_phones_and_goal"


def _clean_spaces(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\t", " ")).strip(" ,;-—")


def _is_header_line(line: str) -> bool:
    normalized = line.lower()
    return any(h in normalized for h in HEADER_HINTS) and not PHONE_LIKE_RE.search(line)


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


def _find_allowed_phone_matches(line: str) -> list[tuple[int, int, str, str, str]]:
    matches: list[tuple[int, int, str, str, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for region in PHONE_MATCH_REGIONS:
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


def build_request_target_report_html(target: DealerCallTarget, report: CallReport) -> str:
    availability = _join_compact_values(report.availability_result, report.incoming_result)
    payment_docs = _join_compact_values(report.payment_result, report.paperwork_result)
    next_action = _join_compact_values(report.next_action, report.important_notes)
    lines = [
        f"<b>Отчёт: {html.escape(target.dealer_name)}</b>",
        "━━━━━━━━━━━━",
        f"<b>Статус:</b> {html.escape(_status_label(report.call_status))}",
        f"<b>Номер:</b> <code>{html.escape(target.phone_e164)}</code>",
    ]
    if report.ai_quality_score is not None:
        reason = _html_value(report.ai_quality_reason, limit=180)
        suffix = f" ({reason})" if reason else ""
        lines.append(f"<b>Оценка AI:</b> <code>{report.ai_quality_score}/100</code>{suffix}")
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


def parse_request_call_input(text: str) -> ParsedRequestInput:
    result = ParsedRequestInput()
    goal_lines: list[str] = []

    for raw_line in (text or "").splitlines():
        line = _clean_spaces(raw_line)
        if not line or _is_header_line(line):
            continue

        line_without_urls, urls = _extract_urls(line)
        result.source_urls.extend(url for url in urls if url not in result.source_urls)
        line = line_without_urls
        if not line:
            continue

        matches = _find_allowed_phone_matches(line)
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

        goal_lines.append(line)

    goal = "\n".join(goal_lines)
    goal = re.sub(r"^\s*задача\s*:\s*", "", goal, flags=re.IGNORECASE)
    result.raw_user_goal = _clean_spaces(goal)
    return result


def is_goal_too_vague(raw_goal: str) -> bool:
    normalized = _clean_spaces(raw_goal).lower()
    if not normalized:
        return False
    if normalized in VAGUE_GOALS:
        return True
    has_vehicle_or_task = bool(
        re.search(
            r"\b("
            r"ford|raptor|ram|trx|srt|dodge|chrysler|bmw|mercedes|tesla|toyota|honda|porsche|"
            r"m3|m4|g63|gls|машин|авто|налич|постав|заказ|заказать|ценник|цена|комплектац|"
            r"форд|раптор|рам|тойот|мерседес"
            r")\b",
            normalized,
            re.IGNORECASE,
        )
    )
    if len(normalized.split()) < 4 and not has_vehicle_or_task:
        return True
    return not has_vehicle_or_task


def _constraint_parts(raw_user_goal: str) -> tuple[list[str], list[str]]:
    lower = raw_user_goal.lower()
    constraints: list[str] = []
    constraint_phrases: list[str] = []
    if "без кредит" in lower:
        constraints.append("no_credit")
        constraint_phrases.append("no financing")
    if "лизинг" in lower or "leasing" in lower or "lease" in lower:
        constraints.append("no_lease")
        constraint_phrases.append("no leasing")
    if "перевод" in lower:
        constraints.append("bank_transfer")
        constraint_phrases.append("payment by bank wire")
    return constraints, constraint_phrases


def _dealer_goal_label(*values: str | None, call_language: str = "en") -> str:
    source = " ".join(value or "" for value in values).lower()
    if call_language == "ja":
        ja_labels = (
            ("ford", "フォード販売店"),
            ("フォード", "フォード販売店"),
            ("toyota", "トヨタ販売店"),
            ("トヨタ", "トヨタ販売店"),
            ("bmw", "BMW販売店"),
            ("mercedes", "メルセデス・ベンツ販売店"),
            ("メルセデス", "メルセデス・ベンツ販売店"),
            ("honda", "ホンダ販売店"),
            ("ホンダ", "ホンダ販売店"),
            ("nissan", "日産販売店"),
            ("日産", "日産販売店"),
        )
        for marker, label in ja_labels:
            if marker in source:
                return label
        return "販売店"
    for marker, label in DEALER_BRAND_LABELS:
        if marker in source:
            return label
    return "the dealership"


def _contains_dealer_label(goal_text: str | None) -> bool:
    normalized = (goal_text or "").lower().replace("’", "'")
    if not normalized:
        return False
    english_labels = {label.lower().replace("’", "'") for _, label in DEALER_BRAND_LABELS}
    english_labels.update(
        {
            "a dodge dealership",
            "a jeep dealership",
            "a ram dealership",
            "a ram dealership's",
            "a ram dealership’s",
            "a jeep dealership's",
            "a jeep dealership’s",
        }
    )
    japanese_labels = {
        "フォード販売店",
        "トヨタ販売店",
        "レクサス販売店",
        "bmw販売店",
        "メルセデス・ベンツ販売店",
        "ホンダ販売店",
        "日産販売店",
        "ポルシェ販売店",
        "販売店の販売部門",
    }
    return any(label in normalized for label in english_labels) or any(label in (goal_text or "") for label in japanese_labels)


def _compact_goal_text(
    dealer_name: str,
    vehicle: str,
    constraint_phrases: list[str],
    raw_goal: str = "",
    *,
    call_language: str = "en",
) -> str:
    constraints_text = ", ".join(constraint_phrases) or "none specified"
    if call_language == "ja":
        constraints_ja = "、".join(_constraint_phrases_ja(constraint_phrases)) or "指定条件なし"
        return (
            f"販売部門に電話し、{vehicle}について確認する。"
            f"条件: {constraints_ja}。在庫または入庫予定、時期、価格と諸費用、グレードや色、"
            "VIN/在庫番号、取り置きや申込金、支払い方法、書類手続きの時期を自然に確認する。"
            "無い場合は次回入庫や近い仕様、次の連絡先を聞く。質問は短く一つずつ行い、重要な回答が曖昧なら一度だけ丁寧に聞き直す。"
        )
    return (
        f"Call the sales department about {vehicle}: available now or nearest incoming unit. "
        f"Customer constraints: {constraints_text}. Confirm availability or ETA, price/OOD plus MSRP/markup/fees, "
        "configuration/color, VIN/stock, hold or deposit option, payment method, and paperwork timing. If unavailable, "
        "ask for nearest delivery or a similar option and best next contact. Ask short questions, one at a time. "
        "If a critical answer is vague, ask one concise follow-up, but keep the call natural and not like an interrogation."
    )


def _constraint_phrases_ja(constraint_phrases: list[str]) -> list[str]:
    mapping = {
        "no financing": "ローンなし",
        "no leasing": "リースなし",
        "payment by bank wire": "銀行振込での支払い",
    }
    return [mapping.get(value, value) for value in constraint_phrases]


def fallback_goal_generation(
    dealer: ParsedDealerLine | DealerCallTarget,
    raw_user_goal: str,
    *,
    call_language: str = "en",
    vehicle_context: list[dict[str, Any]] | None = None,
) -> GoalGenerationResult:
    vehicle = _extract_vehicle_hint(raw_user_goal, vehicle_context or []) or "the requested vehicle or task"
    constraints, constraint_phrases = _constraint_parts(raw_user_goal)
    dealer_name = dealer.dealer_name
    goal_ru = _compact_goal_text(
        dealer_name,
        vehicle,
        constraint_phrases,
        raw_user_goal,
        call_language=call_language,
    )
    return GoalGenerationResult(
        status="ready",
        goal_ru=goal_ru,
        target_vehicle=vehicle,
        main_intent="availability_or_nearest_incoming_unit",
        constraints=constraints,
        required_questions=[
            "availability",
            "nearest_incoming_unit",
            "price_or_price_range",
            "configuration",
            "color",
            "vin_or_stock_number",
            "delivery_timing",
            "paperwork_timing",
            "payment_terms",
        ],
        fallback_questions=["nearest_expected_delivery", "reservation_or_waitlist", "similar_configuration"],
        completion_criteria=[
            "availability_or_incoming_answer_received",
            "price_answer_received",
            "timing_answer_received",
            "payment_answer_received",
            "missing_answers_marked_or_followed_up",
        ],
    )


def _extract_vehicle_hint(raw_user_goal: str, vehicle_context: list[dict[str, Any]] | None = None) -> str | None:
    for context in vehicle_context or []:
        for key in ("vehicle_title", "title"):
            value = _clean_spaces(context.get(key) if isinstance(context, dict) else None)
            if value:
                return value
    match = re.search(
        r"\b("
        r"RAM\s+TRX\s+SRT(?:\s+\d{4})?|RAM\s+TRX(?:\s+\d{4})?|"
        r"Ford\s+Raptor\s+R|BMW\s+M3\s+Competition|BMW\s+M4\s+Competition|Mercedes[-\s]AMG\s+G\s*63"
        r")\b",
        raw_user_goal,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    if re.search(r"\b(форд|ford)\s+(раптор|raptor)\s+r\b", raw_user_goal, re.IGNORECASE):
        return "Ford Raptor R"
    after = re.search(r"(?:по|уточнить|интересует)\s+([A-ZА-ЯЁ][\wА-Яа-яЁё\-\s]{2,60}?)(?:\s+из|\s+в\s+налич|\s+без|\.|,|$)", raw_user_goal)
    if after:
        return _clean_spaces(after.group(1))
    return None


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
    ) -> RequestCallCampaign:
        campaign = RequestCallCampaign(
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
            telegram_source_message_id=source_message_id,
            status="draft",
            call_sequence_mode="manual",
            rejected_phones_json=[],
            telegram_service_message_ids=[],
        )
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
        return campaign

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

    async def update_campaign_from_text(
        self,
        *,
        session: AsyncSession,
        campaign: RequestCallCampaign,
        text: str,
        source_message_id: int | None = None,
    ) -> RequestCallCampaign:
        parsed = parse_request_call_input(text)
        campaign.telegram_source_message_id = source_message_id or campaign.telegram_source_message_id
        previous_region = campaign.phone_region

        # Follow-up messages are incremental: a user may first send phones, then a
        # goal, or clarify the goal after LLM validation. Keep the existing side
        # of the campaign unless the new message actually contains replacements.
        campaign.raw_input = "\n".join(part for part in (campaign.raw_input, text) if part).strip()
        if parsed.raw_user_goal:
            campaign.raw_user_goal = parsed.raw_user_goal
        if parsed.source_urls:
            campaign.source_urls_json = self._merge_source_urls(campaign.source_urls_json or [], parsed.source_urls)
            campaign.vehicle_context_json = await self.context_extractor.extract_many(campaign.source_urls_json)
        if parsed.dealers:
            await self._replace_targets(session, campaign, parsed)

        campaign.rejected_phones_json = [row.__dict__ for row in parsed.rejected_phones]
        await self._add_targets_from_vehicle_context_if_needed(session, campaign)
        await self._refresh_counts(session, campaign)
        if parsed.dealers and previous_region and campaign.phone_region and campaign.phone_region != previous_region:
            campaign.call_language = None

        if not campaign.valid_numbers and not (campaign.raw_user_goal or "").strip():
            campaign.status = "needs_phones_and_goal"
        elif not campaign.valid_numbers:
            campaign.status = "needs_phones"
        elif not (campaign.raw_user_goal or "").strip():
            campaign.status = "needs_goal"
        elif is_goal_too_vague(campaign.raw_user_goal or ""):
            campaign.status = "needs_goal_clarification"
        elif not campaign.call_language:
            campaign.status = "needs_language"
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
        contexts = campaign.vehicle_context_json or []
        for context in contexts:
            raw_phone = context.get("dealer_phone")
            source_url = context.get("source_url") or ""
            if raw_phone and ("carsensor.net" in source_url.lower() or ".jp" in source_url.lower()):
                if is_special_or_proxy_phone(raw_phone):
                    continue
                phone_e164, phone_region = _normalize_allowed_phone(raw_phone, "JP")
            else:
                phone_e164 = None
                phone_region = None
                for region in PHONE_MATCH_REGIONS:
                    phone_e164, phone_region = _normalize_allowed_phone(raw_phone or "", region)
                    if phone_e164 and phone_region:
                        break
            phone_region = phone_region or self._region_from_e164(phone_e164)
            if not phone_e164 or not phone_region:
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
        campaign.phone_region = next(iter(regions)) if len(regions) == 1 else None

    async def _generate_target_goals(self, session: AsyncSession, campaign: RequestCallCampaign) -> None:
        targets = await self.list_targets(session, campaign.id)
        if not targets:
            campaign.status = "needs_phones"
            return

        # goal_ru no longer contains dealer-specific wording, so one generation
        # per campaign is enough even for large dealer lists.
        result = await self._generate_goal_for_target(
            targets[0],
            campaign.raw_user_goal or "",
            call_language=campaign.call_language or "en",
            vehicle_context=campaign.vehicle_context_json or [],
        )
        status = (result.status or "").strip().lower()
        if status == "needs_goal_clarification" or status not in READY_GOAL_STATUSES or not result.goal_ru:
            campaign.status = "needs_goal_clarification"
            campaign.goal_meta_json = result.model_dump()
            return

        result.status = "ready"
        for target in targets:
            result.status = "ready"
            target.goal_ru = result.goal_ru
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
        campaign.call_language = "ja" if call_language == "ja" else "en"
        if not campaign.valid_numbers:
            campaign.status = "needs_phones"
        elif not (campaign.raw_user_goal or "").strip():
            campaign.status = "needs_goal"
        elif is_goal_too_vague(campaign.raw_user_goal or ""):
            campaign.status = "needs_goal_clarification"
        else:
            await self._generate_target_goals(session, campaign)
        await session.commit()
        await session.refresh(campaign)
        return campaign

    async def _generate_goal_for_target(
        self,
        target: DealerCallTarget,
        raw_goal: str,
        *,
        call_language: str,
        vehicle_context: list[dict[str, Any]],
    ) -> GoalGenerationResult:
        try:
            result = await self.openai_service.generate_goal_ru(
                dealer_name=target.dealer_name,
                city=target.city,
                phone_e164=target.phone_e164,
                raw_user_goal=raw_goal,
                call_language=call_language,
                vehicle_context=vehicle_context,
            )
            return self._ensure_compact_goal(target, raw_goal, result, call_language=call_language, vehicle_context=vehicle_context)
        except Exception:
            logger.exception(
                "goal generation failed; using deterministic fallback",
                extra={"campaign_id": target.campaign_id, "target_id": target.id},
            )
            return fallback_goal_generation(
                target,
                raw_goal,
                call_language=call_language,
                vehicle_context=vehicle_context,
            )

    @staticmethod
    def _ensure_compact_goal(
        target: DealerCallTarget,
        raw_goal: str,
        result: GoalGenerationResult,
        *,
        call_language: str,
        vehicle_context: list[dict[str, Any]],
    ) -> GoalGenerationResult:
        goal_text = result.goal_ru or ""
        lower_goal = goal_text.lower()
        dealer_name = (target.dealer_name or "").strip()
        contains_exact_dealer = bool(dealer_name and dealer_name.lower() in lower_goal)
        contains_dealer_label = _contains_dealer_label(goal_text)
        too_forceful = "do not end until" in lower_goal or "every mandatory item" in lower_goal
        if (
            goal_text
            and _word_count(goal_text) <= REQUEST_GOAL_MAX_WORDS
            and not contains_exact_dealer
            and not contains_dealer_label
            and not too_forceful
        ):
            return result
        original_words = _word_count(result.goal_ru)
        constraints, constraint_phrases = _constraint_parts(raw_goal)
        vehicle = result.target_vehicle or _extract_vehicle_hint(raw_goal, vehicle_context) or "the requested vehicle or task"
        result.goal_ru = _compact_goal_text(
            target.dealer_name,
            vehicle,
            constraint_phrases,
            raw_goal,
            call_language=call_language,
        )
        result.status = "ready"
        result.target_vehicle = result.target_vehicle or vehicle
        if not result.constraints:
            result.constraints = constraints
        logger.info(
            "request-call goal compacted",
            extra={
                "campaign_id": target.campaign_id,
                "target_id": target.id,
                "dealer_name": target.dealer_name,
                "original_words": original_words,
                "compacted_words": _word_count(result.goal_ru),
                "max_words": REQUEST_GOAL_MAX_WORDS,
            },
        )
        return result

    @staticmethod
    def _goal_summary(raw_goal: str, meta: dict[str, Any]) -> str:
        vehicle = meta.get("target_vehicle") or _extract_vehicle_hint(raw_goal) or "задача прозвона"
        intent = meta.get("main_intent") or "наличие/условия"
        constraints = ", ".join(meta.get("constraints") or []) or "условия из сообщения"
        return f"{vehicle}. {intent}. {constraints}."

    async def list_targets(self, session: AsyncSession, campaign_id: int) -> list[DealerCallTarget]:
        stmt = select(DealerCallTarget).where(DealerCallTarget.campaign_id == campaign_id).order_by(DealerCallTarget.id)
        return list((await session.execute(stmt)).scalars().all())

    async def list_reports(self, session: AsyncSession, campaign_id: int) -> list[CallReport]:
        stmt = select(CallReport).where(CallReport.campaign_id == campaign_id).order_by(CallReport.id)
        return list((await session.execute(stmt)).scalars().all())

    async def start_next_call(self, *, session: AsyncSession, campaign: RequestCallCampaign, bot: Bot) -> Job | None:
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
        await self._persist_report(session=session, campaign=campaign, target=target, report=report)
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
        report_html = build_request_target_report_html(target, report)
        targets = await self.list_targets(session, campaign.id)
        remaining = any(row.status == "pending" for row in targets)
        sent = await safe_send_message(bot, campaign.telegram_chat_id, report_html, parse_mode="HTML")
        success = bool(sent)
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

    async def send_campaign_summary(self, *, session: AsyncSession, campaign: RequestCallCampaign, bot: Bot) -> bool:
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
        for label, key in (("VIN", "vin"), ("stock", "stock_number"), ("цена", "price"), ("цвет", "color")):
            value = _compact_value(context.get(key), limit=50)
            if value:
                parts.append(f"{label}: {value}")
    if not parts:
        return None
    return _compact_value(", ".join(parts), limit=280)
