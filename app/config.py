from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="TEST_TOKEN", alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_ids: str = Field(default="1", alias="TELEGRAM_ADMIN_IDS")
    telegram_allowed_chat_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_CHAT_IDS")
    telegram_webhook_enabled: bool = Field(default=False, alias="TELEGRAM_WEBHOOK_ENABLED")
    telegram_webhook_path: str = Field(default="/webhooks/telegram", alias="TELEGRAM_WEBHOOK_PATH")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.5", alias="OPENAI_MODEL")

    elevenlabs_api_key: str = Field(default="", alias="ELEVENLABS_API_KEY")
    elevenlabs_agent_id: str = Field(default="", alias="ELEVENLABS_AGENT_ID")
    elevenlabs_agent_id_ja: str = Field(
        default="agent_6001kqfa77j6e3s8f6kqyq132ff5",
        alias="ELEVENLABS_AGENT_ID_JA",
    )
    elevenlabs_agent_id_en: str = Field(
        default="agent_1801kqywpnrze4y8xwt25gcz5e9z",
        alias="ELEVENLABS_AGENT_ID_EN",
    )
    elevenlabs_request_agent_id: str = Field(default="", alias="ELEVENLABS_REQUEST_AGENT_ID")
    elevenlabs_request_agent_id_ja: str = Field(default="", alias="ELEVENLABS_REQUEST_AGENT_ID_JA")
    elevenlabs_phone_number_id: str = Field(default="", alias="ELEVENLABS_PHONE_NUMBER_ID")
    elevenlabs_webhook_secret: str = Field(default="", alias="ELEVENLABS_WEBHOOK_SECRET")
    elevenlabs_allow_first_message_override: bool = Field(
        default=True, alias="ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE"
    )
    twilio_status_callback_enabled: bool = Field(default=True, alias="TWILIO_STATUS_CALLBACK_ENABLED")
    twilio_webhook_auth_token: str = Field(default="", alias="TWILIO_WEBHOOK_AUTH_TOKEN")
    twilio_plus_one_allowed: bool = Field(default=True, alias="TWILIO_PLUS_ONE_ALLOWED")
    twilio_billing_active: bool = Field(default=True, alias="TWILIO_BILLING_ACTIVE")
    twilio_geo_us_ca_enabled: bool = Field(default=True, alias="TWILIO_GEO_US_CA_ENABLED")
    twilio_from_number_verified: bool = Field(default=True, alias="TWILIO_FROM_NUMBER_VERIFIED")
    twilio_marked_as_allowed_for_plus_one: bool = Field(default=False, alias="TWILIO_MARKED_AS_ALLOWED_FOR_PLUS_ONE")

    database_url: str = Field(default="sqlite+aiosqlite:///./dev.db", alias="DATABASE_URL")
    auto_create_tables: bool = Field(default=False, alias="AUTO_CREATE_TABLES")

    test_mode: bool = Field(default=True, alias="TEST_MODE")
    test_call_phone: str = Field(default="+33768013446", alias="TEST_CALL_PHONE")

    webhook_base_url: str = Field(
        default="https://your-ngrok-url.ngrok-free.app",
        alias="WEBHOOK_BASE_URL",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="LOG_LEVEL")

    request_timeout_sec: float = 30.0
    post_call_fallback_enabled: bool = Field(default=True, alias="POST_CALL_FALLBACK_ENABLED")
    post_call_fallback_sec: int = Field(default=60, alias="POST_CALL_FALLBACK_SEC")
    post_call_fallback_attempts: int = Field(default=20, alias="POST_CALL_FALLBACK_ATTEMPTS")
    post_call_fallback_interval_sec: int = Field(default=30, alias="POST_CALL_FALLBACK_INTERVAL_SEC")
    office_timezone: str = Field(default="Asia/Tokyo", alias="OFFICE_TIMEZONE")
    us_timezone_fallback: str = Field(default="America/New_York", alias="US_TIMEZONE_FALLBACK")
    office_hours_fallback: str = Field(default="09:00-19:00", alias="OFFICE_HOURS_FALLBACK")
    call_attempt_max: int = Field(default=3, alias="CALL_ATTEMPT_MAX")
    call_ring_timeout_sec: int = Field(default=60, alias="CALL_RING_TIMEOUT_SEC")
    call_retry_interval_sec: int = Field(default=7200, alias="CALL_RETRY_INTERVAL_SEC")
    call_progress_ping_sec: int = Field(default=15, alias="CALL_PROGRESS_PING_SEC")
    queue_worker_poll_sec: int = Field(default=10, alias="QUEUE_WORKER_POLL_SEC")
    call_create_timeout_sec: int = Field(default=60, alias="CALL_CREATE_TIMEOUT_SEC")
    provider_progress_timeout_sec: int = Field(default=180, alias="PROVIDER_PROGRESS_TIMEOUT_SEC")
    max_call_duration_seconds: int = Field(default=1800, alias="MAX_CALL_DURATION_SECONDS")

    @field_validator("telegram_admin_ids")
    @classmethod
    def _validate_admins(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("TELEGRAM_ADMIN_IDS must not be empty")
        return value

    @field_validator("telegram_webhook_path")
    @classmethod
    def _validate_webhook_path(cls, value: str) -> str:
        value = value.strip() or "/webhooks/telegram"
        if not value.startswith("/"):
            value = "/" + value
        return value

    @property
    def admin_ids(self) -> set[int]:
        raw = self.telegram_admin_ids.replace(" ", "")
        return {int(x) for x in raw.split(",") if x}

    @property
    def allowed_chat_ids(self) -> set[int]:
        raw = self.telegram_allowed_chat_ids.replace(" ", "")
        return {int(x) for x in raw.split(",") if x}

    @staticmethod
    def telegram_chat_id_aliases(chat_id: int | None) -> set[int]:
        if chat_id is None:
            return set()
        raw = int(chat_id)
        abs_raw = abs(raw)
        aliases = {raw, abs_raw, -abs_raw}
        digits = str(abs_raw)
        if digits.startswith("100") and len(digits) > 3:
            bare = int(digits[3:])
            aliases.update({bare, -bare, int(f"-100{bare}")})
        else:
            aliases.add(int(f"-100{digits}"))
        return aliases

    def is_allowed_telegram_chat(self, chat_id: int | None, chat_type: str | None = None) -> bool:
        if chat_type == "private":
            return True
        allowed_aliases: set[int] = set()
        for allowed_id in self.allowed_chat_ids:
            allowed_aliases.update(self.telegram_chat_id_aliases(allowed_id))
        return bool(allowed_aliases and not self.telegram_chat_id_aliases(chat_id).isdisjoint(allowed_aliases))

    @property
    def normalized_webhook_base_url(self) -> str:
        return self.webhook_base_url.rstrip("/")

    @property
    def elevenlabs_webhook_endpoint(self) -> str:
        return f"{self.normalized_webhook_base_url}/webhooks/elevenlabs"

    @property
    def effective_elevenlabs_agent_id_ja(self) -> str:
        return (self.elevenlabs_agent_id_ja or "").strip() or "agent_6001kqfa77j6e3s8f6kqyq132ff5"

    @property
    def effective_elevenlabs_agent_id_en(self) -> str:
        return (self.elevenlabs_agent_id_en or "").strip() or "agent_1801kqywpnrze4y8xwt25gcz5e9z"

    @property
    def twilio_call_status_callback_endpoint(self) -> str:
        return f"{self.normalized_webhook_base_url}/webhooks/twilio/call-status"

    def runtime_warnings(self) -> list[str]:
        warnings: list[str] = []
        parsed = urlparse(self.webhook_base_url)
        host = (parsed.hostname or "").lower()

        if not self.webhook_base_url.startswith("https://"):
            warnings.append("WEBHOOK_BASE_URL should start with https://")

        is_localhost = host in {"localhost", "127.0.0.1"} or "localhost" in self.webhook_base_url
        if self.test_mode and is_localhost:
            warnings.append(
                "ElevenLabs cannot call localhost. Use ngrok and set WEBHOOK_BASE_URL to https://...ngrok-free.app"
            )
        if self.twilio_status_callback_enabled and not self.twilio_webhook_auth_token:
            warnings.append(
                "TWILIO_WEBHOOK_AUTH_TOKEN is not set; Twilio status callbacks are accepted without signature validation"
            )
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
