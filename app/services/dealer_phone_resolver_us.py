from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.schemas import DealerPhoneResolutionResult, ExtractionResult
from app.services.http import request_with_retry
from app.utils.phone import classify_us_phone, extract_us_phone_candidates_from_text, normalize_us_phone_to_e164

logger = logging.getLogger(__name__)

PHONE_LABEL_HINTS = {
    "sales": ("sales", "sales department", "call sales", "contact sales"),
    "main": ("main", "general", "contact us", "call us"),
    "service": ("service", "service department"),
    "parts": ("parts", "parts department"),
}


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def _label_from_context(context: str) -> str:
    ctx = _norm_text(context)
    for label, hints in PHONE_LABEL_HINTS.items():
        if any(h in ctx for h in hints):
            return label
    return "unknown"


def _domain(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower()


class DealerPhoneResolverUS:
    def __init__(self, *, timeout_sec: float = 30.0):
        self.timeout_sec = timeout_sec

    async def resolve(self, *, extracted: ExtractionResult, listing_html: str | None = None) -> DealerPhoneResolutionResult:
        listing_phone_raw = extracted.phone_from_listing
        listing_phone_type = classify_us_phone(listing_phone_raw)
        logger.info(
            "resolver_us: extracted listing phone",
            extra={
                "status": listing_phone_raw or "missing",
                "listing_phone_type": listing_phone_type,
                "dealer": extracted.dealer,
                "listing_url": extracted.listing_url,
            },
        )

        candidates: list[dict] = []
        evidence: list[dict] = []
        discovered_hours: list[str] = []

        if listing_phone_raw:
            candidate = self._build_candidate(
                phone_raw=listing_phone_raw,
                phone_label="unknown",
                source_type="cars.com",
                source_url=extracted.listing_url,
                extracted=extracted,
                source_text=listing_html or "",
            )
            if candidate:
                candidates.append(candidate)
                evidence.append(candidate["evidence"])

        external_sources = self._build_external_sources(extracted)
        logger.info(
            "resolver_us: source urls",
            extra={"status": ",".join([src["url"] for src in external_sources]) or "none"},
        )
        for source in external_sources:
            source_url = source["url"]
            try:
                html = await self._fetch_text(source_url)
            except Exception as exc:
                logger.warning(
                    "resolver_us: failed fetch",
                    extra={"status": f"{source_url}: {exc}"},
                )
                continue
            source_candidates = self._extract_candidates_from_html(
                html=html,
                source_url=source_url,
                source_type=source["source_type"],
                extracted=extracted,
            )
            candidates.extend(source_candidates)
            evidence.extend([row["evidence"] for row in source_candidates])
            parsed_hours = self._extract_dealer_hours_from_html(html)
            if parsed_hours:
                discovered_hours.append(parsed_hours)

        for row in candidates:
            if row.get("rejected_reason"):
                logger.info(
                    "resolver_us: rejected phone",
                    extra={"status": f"{row.get('phone_raw')} ({row['rejected_reason']})"},
                )

        valid = [row for row in candidates if not row.get("rejected_reason") and row.get("phone_e164")]
        selected = self._select_candidate(valid)
        resolution_status = self._resolution_status(selected, valid, candidates)

        resolved_phone_raw = selected["phone_raw"] if selected else None
        resolved_phone_e164 = selected["phone_e164"] if selected else None
        resolved_phone_source_url = selected["source_url"] if selected else None
        source_type = selected["source_type"] if selected else None
        phone_type = selected["phone_label"] if selected else None
        score = int(selected["score"]) if selected else self._max_candidate_score(candidates, resolution_status)
        error_reason = None
        if resolution_status == "invalid_number":
            error_reason = "resolved phone is invalid"
        elif resolution_status == "not_found":
            error_reason = "dealer phone not found"

        logger.info(
            "resolver_us: final resolution",
            extra={
                "status": resolution_status,
                "confidence_score": score,
                "selected_phone": resolved_phone_e164 or "none",
            },
        )

        dealer_hours = discovered_hours[0] if discovered_hours else None

        return DealerPhoneResolutionResult(
            listing_url=extracted.listing_url,
            source="cars.com",
            dealer_name=extracted.dealer,
            dealer_address=extracted.dealer_address,
            dealer_business_hours=dealer_hours,
            listing_phone_raw=listing_phone_raw,
            listing_phone_type="normal" if listing_phone_type == "normal" else ("missing" if listing_phone_type == "missing" else "invalid"),
            resolved_phone_raw=resolved_phone_raw,
            resolved_phone_e164=resolved_phone_e164,
            resolved_phone_source_url=resolved_phone_source_url,
            source_type=source_type,
            phone_type=phone_type,
            confidence_score=score,
            resolution_status=resolution_status,
            evidence=evidence,
            candidates=[
                {
                    "phone": row.get("phone_raw"),
                    "phone_e164": row.get("phone_e164"),
                    "source_url": row.get("source_url"),
                    "source_type": row.get("source_type"),
                    "phone_label": row.get("phone_label"),
                    "score": row.get("score"),
                    "dealer_name_match": row.get("dealer_name_match"),
                    "address_match": row.get("address_match"),
                    "rejected_reason": row.get("rejected_reason"),
                }
                for row in candidates
            ],
            error_reason=error_reason,
        )

    def _build_external_sources(self, extracted: ExtractionResult) -> list[dict[str, str]]:
        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for url, source_type in [
            (extracted.dealer_vehicle_url, "dealer_vehicle_page"),
            (extracted.dealer_website_url, "official_dealer_website"),
        ]:
            if not url:
                continue
            normalized = url.strip()
            if normalized.startswith("/"):
                normalized = urljoin(extracted.listing_url, normalized)
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append({"url": normalized, "source_type": source_type})
        return result

    async def _fetch_text(self, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(timeout=self.timeout_sec, headers=headers, follow_redirects=True) as client:
            response = await request_with_retry(client, "GET", url)
            response.raise_for_status()
            return response.text

    def _extract_candidates_from_html(
        self,
        *,
        html: str,
        source_url: str,
        source_type: str,
        extracted: ExtractionResult,
    ) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        source_text = soup.get_text("\n", strip=True)
        candidates: list[dict] = []

        # Priority source: explicit tel links, then regex from text.
        for a in soup.select("a[href^='tel:']"):
            phone = (a.get("href") or "").replace("tel:", "").strip()
            if not phone:
                continue
            context = " ".join(
                [
                    a.get_text(" ", strip=True),
                    a.parent.get_text(" ", strip=True) if a.parent else "",
                    " ".join(a.get("class", [])),
                ]
            )
            built = self._build_candidate(
                phone_raw=phone,
                phone_label=_label_from_context(context),
                source_type=source_type,
                source_url=source_url,
                extracted=extracted,
                source_text=source_text,
            )
            if built:
                candidates.append(built)

        for phone in extract_us_phone_candidates_from_text(source_text):
            context = self._find_phone_context(source_text, phone)
            built = self._build_candidate(
                phone_raw=phone,
                phone_label=_label_from_context(context),
                source_type=source_type,
                source_url=source_url,
                extracted=extracted,
                source_text=source_text,
            )
            if built:
                candidates.append(built)
        return self._dedupe_candidates(candidates)

    @staticmethod
    def _extract_dealer_hours_from_html(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        hour_lines: list[str] = []
        for line in lines:
            low = line.lower()
            if "hour" not in low and "mon" not in low and "tue" not in low and "wed" not in low:
                continue
            if not re.search(r"\d{1,2}\s*[:\.]\s*\d{2}", line):
                continue
            hour_lines.append(re.sub(r"\s+", " ", line))
            if len(hour_lines) >= 3:
                break
        if not hour_lines:
            return None
        return " | ".join(hour_lines)

    @staticmethod
    def _find_phone_context(text: str, phone: str) -> str:
        idx = text.lower().find(phone.lower())
        if idx == -1:
            return text[:180]
        start = max(0, idx - 100)
        end = min(len(text), idx + len(phone) + 100)
        return text[start:end]

    def _build_candidate(
        self,
        *,
        phone_raw: str,
        phone_label: str,
        source_type: str,
        source_url: str,
        extracted: ExtractionResult,
        source_text: str,
    ) -> dict | None:
        phone_raw = phone_raw.strip()
        if not phone_raw:
            return None

        phone_e164 = normalize_us_phone_to_e164(phone_raw)
        dealer_name_match = self._is_dealer_name_match(extracted.dealer, source_text, source_url)
        address_match = self._is_address_match(extracted.dealer_address, source_text)
        score = self._score_candidate(
            source_type=source_type,
            phone_label=phone_label,
            dealer_name_match=dealer_name_match,
            address_match=address_match,
            source_url=source_url,
            extracted=extracted,
        )

        rejected_reason = None
        if not phone_e164:
            rejected_reason = "invalid_number"
        elif phone_label in {"service", "parts"}:
            rejected_reason = "service_or_parts_phone"

        logger.info(
            "resolver_us: candidate phone",
            extra={
                "status": phone_raw,
                "phone_label": phone_label,
                "source_type": source_type,
                "score": score,
                "callable": bool(phone_e164 and not rejected_reason),
            },
        )

        return {
            "phone_raw": phone_raw,
            "phone_e164": phone_e164,
            "phone_label": phone_label,
            "source_type": source_type,
            "source_url": source_url,
            "score": score,
            "dealer_name_match": dealer_name_match,
            "address_match": address_match,
            "rejected_reason": rejected_reason,
            "evidence": {
                "source_url": source_url,
                "dealer_name_match": dealer_name_match,
                "address_match": address_match,
                "phone_found": phone_raw,
                "phone_label": phone_label,
            },
        }

    @staticmethod
    def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
        best_by_phone: dict[str, dict] = {}
        for candidate in candidates:
            key = candidate.get("phone_e164") or candidate.get("phone_raw")
            if not key:
                continue
            existing = best_by_phone.get(key)
            if not existing or int(candidate.get("score") or 0) > int(existing.get("score") or 0):
                best_by_phone[key] = candidate
        return list(best_by_phone.values())

    @staticmethod
    def _is_dealer_name_match(dealer_name: str | None, source_text: str, source_url: str) -> bool:
        if not dealer_name:
            return False
        normalized = _norm_text(dealer_name)
        if normalized and normalized in _norm_text(source_text):
            return True
        tokens = [t for t in re.split(r"\W+", normalized) if len(t) >= 4]
        if tokens and all(t in _norm_text(source_url) for t in tokens[:2]):
            return True
        return False

    @staticmethod
    def _is_address_match(dealer_address: str | None, source_text: str) -> bool:
        if not dealer_address:
            return False
        normalized = _norm_text(dealer_address)
        digits = re.sub(r"\D", "", normalized)
        text_norm = _norm_text(source_text)
        if digits and digits in re.sub(r"\D", "", text_norm):
            return True
        words = [w for w in re.split(r"\W+", normalized) if len(w) >= 4]
        if not words:
            return False
        match_count = sum(1 for w in words[:5] if w in text_norm)
        return match_count >= 2

    def _score_candidate(
        self,
        *,
        source_type: str,
        phone_label: str,
        dealer_name_match: bool,
        address_match: bool,
        source_url: str,
        extracted: ExtractionResult,
    ) -> int:
        score = 0
        if source_type in {"official_dealer_website", "dealer_vehicle_page"}:
            score += 50
        elif source_type == "cars.com":
            score += 15
        elif source_type == "directory":
            score += 10

        if phone_label == "sales":
            score += 30

        if dealer_name_match:
            score += 25
        else:
            score -= 50

        if address_match:
            score += 25
        elif extracted.dealer_address:
            score -= 50

        if source_type == "dealer_vehicle_page":
            score += 20
        if "contact" in _norm_text(source_url):
            score += 20

        return max(0, min(100, score))

    @staticmethod
    def _select_candidate(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        label_rank = {"sales": 4, "main": 3, "unknown": 2, "service": 1, "parts": 0}
        ranked = sorted(
            candidates,
            key=lambda row: (label_rank.get(row.get("phone_label") or "unknown", 0), int(row.get("score") or 0)),
            reverse=True,
        )
        return ranked[0]

    @staticmethod
    @staticmethod
    def _max_candidate_score(candidates: list[dict], resolution_status: str) -> int:
        if resolution_status != "needs_review":
            return 0
        return max((int(row.get("score") or 0) for row in candidates), default=0)

    @staticmethod
    def _resolution_status(selected: dict | None, valid_candidates: list[dict], all_candidates: list[dict]) -> str:
        if not all_candidates:
            return "not_found"
        if not selected:
            has_callable_rejected = any(row.get("phone_e164") and row.get("rejected_reason") for row in all_candidates)
            if has_callable_rejected:
                return "needs_review"
            return "invalid_number"
        score = int(selected.get("score") or 0)
        label = selected.get("phone_label") or "unknown"
        if score >= 80 and label in {"sales", "main", "unknown"}:
            return "resolved"
        if valid_candidates and score < 80:
            return "needs_review"
        return "needs_review"
