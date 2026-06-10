from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings
from app.services.http import request_with_retry

logger = logging.getLogger(__name__)


class ProviderCallCreateError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        stage: str,
        http_status: int | None,
        provider_error_code: str | None,
        provider_error_message: str,
        provider_more_info_url: str | None = None,
        payload_without_secrets: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(provider_error_message)
        self.provider = provider
        self.stage = stage
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        self.provider_more_info_url = provider_more_info_url
        self.payload_without_secrets = payload_without_secrets or {}


class ElevenLabsService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _year_value(dynamic_variables: dict[str, Any]) -> str | None:
        value = str(dynamic_variables.get("year_spoken_ru") or "").strip()
        if re.fullmatch(r"(19|20)\d{2}", value):
            return value
        return None

    def _build_first_message(self, *, call_language: str, dynamic_variables: dict[str, Any]) -> str:
        year = self._year_value(dynamic_variables)
        if call_language == "en":
            if year:
                return (
                    f"Hello. I'm calling about the listing for {{{{car_spoken_ru}}}}, {year}. "
                    "Could you please confirm if this vehicle is still available?"
                )
            return (
                "Hello. I'm calling about the listing for {{car_spoken_ru}}. "
                "Could you please confirm if this vehicle is still available?"
            )
        if call_language == "ja":
            if year:
                return (
                    f"こんにちは。{{{{car_spoken_ru}}}}、{year}年のお車についてお電話しています。"
                    "まだ販売中か教えていただけますか。"
                )
            return "こんにちは。{{car_spoken_ru}}のお車についてお電話しています。まだ販売中か教えていただけますか。"
        if year:
            return (
                f"Здравствуйте. Я звоню по объявлению: {{{{car_spoken_ru}}}}, {year} года. "
                "Подскажите, пожалуйста, автомобиль ещё продаётся?"
            )
        return (
            "Здравствуйте. Я звоню по объявлению: {{car_spoken_ru}}. "
            "Подскажите, пожалуйста, автомобиль ещё продаётся?"
        )

    async def start_outbound_call(
        self,
        *,
        call_phone: str,
        dynamic_variables: dict[str, Any],
        agent_id_override: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        if not self.settings.elevenlabs_agent_id or not self.settings.elevenlabs_phone_number_id:
            raise RuntimeError("ELEVENLABS_AGENT_ID or ELEVENLABS_PHONE_NUMBER_ID missing")

        goal_only_request = set(dynamic_variables.keys()) == {"goal_ru"}
        first_message_override_enabled = self.settings.elevenlabs_allow_first_message_override and not goal_only_request

        payload: dict[str, Any] = {
            "agent_id": agent_id_override or self.settings.elevenlabs_agent_id,
            "agent_phone_number_id": self.settings.elevenlabs_phone_number_id,
            "to_number": call_phone,
            "conversation_initiation_client_data": {
                "dynamic_variables": dynamic_variables,
            },
            "call_recording_enabled": True,
        }
        if self.settings.twilio_status_callback_enabled:
            payload["telephony_call_config"] = {
                "status_callback_url": self.settings.twilio_call_status_callback_endpoint
            }
        if first_message_override_enabled:
            call_language = str(dynamic_variables.get("call_language") or "ru")
            first_message = self._build_first_message(call_language=call_language, dynamic_variables=dynamic_variables)
            payload["conversation_initiation_client_data"]["conversation_config_override"] = {
                "agent": {
                    "first_message": first_message
                }
            }

        logger.debug(
            "elevenlabs outbound payload meta: prompt_override=%s first_message_override=%s intro_compact=%s dynamic_variables keys=%s",
            False,
            first_message_override_enabled,
            True,
            sorted(dynamic_variables.keys()),
        )
        logger.debug(
            "elevenlabs outbound key vars: call_language=%s car_spoken_ru=%s year_spoken_ru=%s price_used_spoken_ru=%s price_used_jpy=%s",
            dynamic_variables.get("call_language"),
            dynamic_variables.get("car_spoken_ru"),
            dynamic_variables.get("year_spoken_ru"),
            dynamic_variables.get("price_used_spoken_ru"),
            dynamic_variables.get("price_used_jpy"),
        )
        if goal_only_request:
            logger.debug("elevenlabs request-call payload contains only goal_ru dynamic variable")

        headers = {
            "xi-api-key": self.settings.elevenlabs_api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            response = await request_with_retry(
                client,
                "POST",
                "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                headers=headers,
                json=payload,
            )
            if response.status_code >= 400:
                message, code, more_info = self._extract_error(response)
                # Compatibility fallback: if telephony_call_config is unsupported, retry once without it.
                if (
                    response.status_code == 422
                    and "telephony_call_config" in payload
                    and (
                        "telephony_call_config" in (message or "")
                        or "extra fields not permitted" in (message or "").lower()
                    )
                ):
                    payload.pop("telephony_call_config", None)
                    response = await request_with_retry(
                        client,
                        "POST",
                        "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                        headers=headers,
                        json=payload,
                    )
                    if response.status_code >= 400:
                        message, code, more_info = self._extract_error(response)
                # Compatibility fallback: if first_message override is disabled in agent config, retry without override.
                msg_lower = (message or "").lower()
                first_message_not_allowed = (
                    response.status_code == 422
                    and first_message_override_enabled
                    and "conversation_config_override" in (payload.get("conversation_initiation_client_data") or {})
                    and "first_message" in msg_lower
                    and "not allowed by config" in msg_lower
                )
                if first_message_not_allowed:
                    logger.warning(
                        "elevenlabs first_message override is disabled in agent security; retrying without first_message override"
                    )
                    cicd = payload.get("conversation_initiation_client_data") or {}
                    cicd.pop("conversation_config_override", None)
                    payload["conversation_initiation_client_data"] = cicd
                    response = await request_with_retry(
                        client,
                        "POST",
                        "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                        headers=headers,
                        json=payload,
                    )
                if response.status_code >= 400:
                    message, code, more_info = self._extract_error(response)
                    raise ProviderCallCreateError(
                        provider="twilio",
                        stage="create_call",
                        http_status=response.status_code,
                        provider_error_code=code,
                        provider_error_message=message,
                        provider_more_info_url=more_info,
                        payload_without_secrets={
                            "agent_id": payload.get("agent_id"),
                            "agent_phone_number_id": payload.get("agent_phone_number_id"),
                            "to_number": payload.get("to_number"),
                            "call_recording_enabled": payload.get("call_recording_enabled"),
                            "has_dynamic_variables": bool(
                                (
                                    (payload.get("conversation_initiation_client_data") or {}).get("dynamic_variables")
                                    or {}
                                )
                            ),
                            "has_conversation_config_override": bool(
                                (
                                    (payload.get("conversation_initiation_client_data") or {}).get(
                                        "conversation_config_override"
                                    )
                                    or {}
                                )
                            ),
                            "telephony_call_config": payload.get("telephony_call_config"),
                        },
                    )
            return response.json()

    async def fetch_conversation_details(self, conversation_id: str) -> dict[str, Any]:
        headers = {"xi-api-key": self.settings.elevenlabs_api_key}
        url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            response = await request_with_retry(client, "GET", url, headers=headers)
            response.raise_for_status()
            return response.json()

    async def fetch_conversation_audio(self, conversation_id: str) -> bytes | None:
        headers = {"xi-api-key": self.settings.elevenlabs_api_key}
        url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}/audio"
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_sec) as client:
            response = await request_with_retry(client, "GET", url, headers=headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content

    def verify_webhook(self, *, raw_body: bytes, signature: str | None) -> bool:
        secret = self.settings.elevenlabs_webhook_secret
        if not secret:
            return True
        if not signature:
            return False

        # Legacy support: plain sha256=<hex> over raw body.
        digest_legacy = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(f"sha256={digest_legacy}", signature):
            return True

        # ElevenLabs signature style with timestamp and v0/v1 hash.
        # Example formats supported:
        # - t=1710000000,v0=<hex>
        # - t=1710000000,v1=<hex>
        # - timestamp=...,v1=...
        sig_map: dict[str, str] = {}
        for part in signature.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            sig_map[key.strip()] = value.strip()

        timestamp = sig_map.get("t") or sig_map.get("timestamp")
        provided = sig_map.get("v1") or sig_map.get("v0")
        if not timestamp or not provided:
            # Fallback: if header contains only v1=... etc.
            m = re.search(r"v[01]=([0-9a-fA-F]+)", signature)
            if m:
                provided = m.group(1)
            timestamp = timestamp or ""

        payload = (timestamp + ".").encode("utf-8") + raw_body
        computed = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if provided and hmac.compare_digest(computed, provided):
            return True

        # Last fallback: raw-body signature without timestamp prefix.
        return hmac.compare_digest(digest_legacy, provided or "")

    @staticmethod
    def build_idempotency_key(payload: dict[str, Any]) -> str:
        event_type = payload.get("type", "unknown")
        ts = payload.get("event_timestamp", "")
        conversation_id = ((payload.get("data") or {}).get("conversation_id") or "unknown")
        raw = json.dumps([event_type, ts, conversation_id], ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str | None, str | None]:
        text = response.text or f"HTTP {response.status_code} error"
        error_code: str | None = None
        more_info: str | None = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                # ElevenLabs may wrap provider error in different shapes.
                detail = payload.get("detail") or payload.get("message") or payload.get("error") or payload
                text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                error_code = str(
                    payload.get("code")
                    or payload.get("error_code")
                    or payload.get("provider_error_code")
                    or ""
                ).strip() or None
                more_info = payload.get("more_info") or payload.get("provider_more_info_url")
        except Exception:
            pass
        if not error_code:
            m = re.search(r"\b(21\d{3})\b", text)
            if m:
                error_code = m.group(1)
        return text, error_code, more_info
