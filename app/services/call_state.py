from __future__ import annotations

import json
import re
from typing import Any


FINAL_JOB_STATUSES = {
    "completed",
    "no_answer",
    "busy",
    "failed",
    "call_create_failed",
    "canceled",
    "timeout",
    "provider_timeout",
    "needs_review",
    "dealer_phone_needs_review",
}

TWILIO_TO_JOB_STATUS = {
    "queued": "queued",
    "initiated": "initiated",
    "ringing": "ringing",
    "in-progress": "in_progress",
    "completed": "completed",
    "busy": "busy",
    "failed": "failed",
    "no-answer": "no_answer",
    "canceled": "canceled",
}


def normalize_twilio_call_status(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return TWILIO_TO_JOB_STATUS.get(normalized)


def sanitize_payload(value: dict[str, Any]) -> dict[str, Any]:
    sensitive = ("token", "secret", "api_key", "apikey", "authorization", "auth")
    raw = json.loads(json.dumps(value))

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            cleaned: dict[str, Any] = {}
            for k, v in obj.items():
                key = str(k).lower()
                if any(s in key for s in sensitive):
                    cleaned[k] = "***redacted***"
                else:
                    cleaned[k] = walk(v)
            return cleaned
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        return obj

    return walk(raw)


def classify_twilio_create_failure(
    *,
    http_status: int | None,
    provider_error_code: str | None,
    provider_error_message: str | None,
) -> tuple[str, bool]:
    code = str(provider_error_code or "").strip()
    message = (provider_error_message or "").lower()

    if code == "21216" or "account not allowed to call" in message:
        return (
            "Twilio не разрешил звонок на этот номер. Для +1 направлений проверьте Trust Hub: "
            "нужен approved Business Primary Customer Profile, активный Billing, корректный From и Geo Permissions.",
            False,
        )
    if code == "21217":
        return ("Номер To выглядит невалидным. Проверьте формат E.164.", False)
    if "source phone number provided is not yet verified" in message:
        return ("From number не подтверждён или не принадлежит аккаунту.", False)
    if http_status in {401, 403}:
        return (
            "Ошибка авторизации или прав Twilio. Проверьте Account SID/Auth Token/API Key и права аккаунта.",
            False,
        )
    if http_status == 429:
        return ("Rate limit у провайдера. Повторите позже.", True)
    if http_status in {500, 502, 503, 504}:
        return ("Временная ошибка провайдера. Можно повторить попытку позже.", True)
    if "invalid" in message and "number" in message:
        return ("Номер получателя невалиден для звонка.", False)
    return ("Проверьте настройки Twilio/ElevenLabs и доступность направления.", False)


def extract_twilio_error_code(message: str | None) -> str | None:
    if not message:
        return None
    m = re.search(r"\b(21\d{3})\b", message)
    return m.group(1) if m else None
