from __future__ import annotations

import logging

from app.schemas import DealerPhoneResolutionResult, ExtractionResult
from app.services.openai_client import OpenAIService
from app.utils.phone import (
    classify_listing_phone,
    extract_phone_candidates_from_text,
    is_special_or_proxy_phone,
    normalize_jp_phone_to_e164,
)

logger = logging.getLogger(__name__)


class DealerPhoneResolver:
    def __init__(self, *, openai_service: OpenAIService):
        self.openai_service = openai_service

    async def resolve(self, *, extracted: ExtractionResult) -> DealerPhoneResolutionResult:
        listing_phone_raw = extracted.phone_from_listing or extracted.dealer_direct_phone or extracted.carsensor_free_phone
        listing_phone_type = classify_listing_phone(listing_phone_raw)
        logger.info(
            "resolver: extracted listing phone",
            extra={
                "status": listing_phone_raw or "missing",
                "listing_phone_type": listing_phone_type,
                "dealer": extracted.dealer,
            },
        )

        if listing_phone_type == "normal":
            e164 = normalize_jp_phone_to_e164(listing_phone_raw)
            if not e164:
                return DealerPhoneResolutionResult(
                    listing_url=extracted.listing_url,
                    dealer_name=extracted.dealer,
                    dealer_address=extracted.dealer_address,
                    listing_phone_raw=listing_phone_raw,
                    listing_phone_type="normal",
                    resolution_status="invalid_number",
                    error_reason="normal number could not be converted to E.164",
                )
            result = DealerPhoneResolutionResult(
                listing_url=extracted.listing_url,
                dealer_name=extracted.dealer,
                dealer_address=extracted.dealer_address,
                listing_phone_raw=listing_phone_raw,
                listing_phone_type="normal",
                resolved_phone_raw=listing_phone_raw,
                resolved_phone_e164=e164,
                resolved_phone_source_url=extracted.listing_url,
                source_type="carsensor",
                confidence_score=100,
                resolution_status="resolved",
                evidence=[
                    {
                        "source_url": extracted.listing_url,
                        "dealer_name_match": True,
                        "address_match": True,
                        "phone_found": listing_phone_raw or "",
                    }
                ],
            )
            logger.info(
                "resolver: selected phone",
                extra={"status": result.resolved_phone_e164, "confidence_score": result.confidence_score},
            )
            return result

        if listing_phone_type == "missing":
            return DealerPhoneResolutionResult(
                listing_url=extracted.listing_url,
                dealer_name=extracted.dealer,
                dealer_address=extracted.dealer_address,
                listing_phone_raw=None,
                listing_phone_type="missing",
                resolution_status="not_found",
                error_reason="listing phone missing",
            )

        # proxy_or_special: resolve via GPT web search
        try:
            logger.info(
                "resolver: phone classification requires web search",
                extra={"status": "proxy_or_special", "listing_phone_raw": listing_phone_raw},
            )
            result = await self.openai_service.resolve_dealer_phone_with_web_search(
                listing_url=extracted.listing_url,
                dealer_name=extracted.dealer,
                dealer_address=extracted.dealer_address,
                listing_phone_raw=listing_phone_raw,
            )
        except Exception as exc:
            logger.exception("resolver: web-search resolution failed")
            return DealerPhoneResolutionResult(
                listing_url=extracted.listing_url,
                dealer_name=extracted.dealer,
                dealer_address=extracted.dealer_address,
                listing_phone_raw=listing_phone_raw,
                listing_phone_type="proxy_or_special",
                resolution_status="not_found",
                error_reason=f"resolver_failed: {exc}",
            )

        # Normalize and validate output from model
        if result.listing_phone_type not in {"proxy_or_special", "normal", "missing"}:
            result.listing_phone_type = "proxy_or_special"
        if result.listing_phone_type == "proxy_or_special":
            result.listing_phone_raw = result.listing_phone_raw or listing_phone_raw

        if result.resolved_phone_raw and not result.resolved_phone_e164:
            result.resolved_phone_e164 = normalize_jp_phone_to_e164(result.resolved_phone_raw)
        if result.resolved_phone_e164 and is_special_or_proxy_phone(result.resolved_phone_e164):
            logger.warning(
                "resolver: rejected selected phone",
                extra={"status": result.resolved_phone_e164, "reason": "special/proxy prefix"},
            )
            result.resolved_phone_e164 = None
            result.resolution_status = "invalid_number"
            result.error_reason = "resolved number is special/proxy and not callable"
        if result.resolved_phone_raw and is_special_or_proxy_phone(result.resolved_phone_raw):
            logger.warning(
                "resolver: rejected selected phone",
                extra={"status": result.resolved_phone_raw, "reason": "special/proxy prefix"},
            )
            result.resolved_phone_raw = None
            result.resolved_phone_e164 = None
            result.resolution_status = "proxy_only"
            result.error_reason = "resolved number is proxy/special"

        # Bound confidence / status consistency
        score = max(0, min(100, int(result.confidence_score)))
        result.confidence_score = score
        if result.resolution_status == "resolved" and score < 80:
            result.resolution_status = "needs_review"
        if not result.resolved_phone_e164 and result.resolution_status == "resolved":
            result.resolution_status = "invalid_number"
            result.error_reason = "resolved status without callable E.164 number"

        for candidate in result.candidates:
            phone = str(candidate.get("phone") or candidate.get("phone_found") or "").strip()
            if not phone:
                continue
            candidate["callable_e164"] = normalize_jp_phone_to_e164(phone)
            candidate["is_special"] = is_special_or_proxy_phone(phone)
            logger.info(
                "resolver: candidate phone",
                extra={"status": phone, "callable": bool(candidate["callable_e164"])},
            )

        evidence_phones = "\n".join([ev.phone_found for ev in result.evidence if ev.phone_found])
        rejected = [p for p in extract_phone_candidates_from_text(evidence_phones) if is_special_or_proxy_phone(p)]
        for ph in rejected:
            logger.info(
                "resolver: rejected phone",
                extra={"status": ph, "reason": "special/proxy prefix"},
            )

        logger.info(
            "resolver: final resolution",
            extra={
                "status": result.resolution_status,
                "confidence_score": result.confidence_score,
                "selected": result.resolved_phone_e164,
            },
        )
        return DealerPhoneResolutionResult.model_validate(result.model_dump())
