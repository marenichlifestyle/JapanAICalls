from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapi import Request

from app.config import Settings

ParamPairs = Sequence[tuple[str, str]]


def _coerce_param_pairs(params: Mapping[str, Any] | ParamPairs) -> list[tuple[str, str]]:
    if isinstance(params, Mapping):
        items = params.items()
    else:
        items = params
    return [(str(key), "" if value is None else str(value)) for key, value in items]


def compute_twilio_signature(*, auth_token: str, url: str, params: Mapping[str, Any] | ParamPairs) -> str:
    """Compute Twilio's form-encoded webhook signature.

    Twilio signs the exact public callback URL plus POST params sorted by name.
    Status callbacks are form-encoded, so this covers the production path.
    """
    payload = url + "".join(f"{key}{value}" for key, value in sorted(_coerce_param_pairs(params)))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(
    *,
    auth_token: str,
    signature: str | None,
    urls: Iterable[str],
    params: Mapping[str, Any] | ParamPairs,
) -> bool:
    if not auth_token:
        return True
    if not signature:
        return False
    for url in dict.fromkeys(urls):
        expected = compute_twilio_signature(auth_token=auth_token, url=url, params=params)
        if hmac.compare_digest(expected, signature):
            return True
    return False


def twilio_signature_candidate_urls(request: Request, settings: Settings) -> list[str]:
    """Return public URL candidates that may have been used by Twilio to sign.

    Behind a reverse proxy, FastAPI may see an internal URL. We always validate
    against WEBHOOK_BASE_URL first, then request/proxy variants for local tests.
    """
    query = f"?{request.url.query}" if request.url.query else ""
    path_and_query = f"{request.url.path}{query}"
    candidates = [settings.twilio_call_status_callback_endpoint]
    candidates.append(str(request.url))

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if forwarded_proto and forwarded_host:
        candidates.append(f"{forwarded_proto}://{forwarded_host}{path_and_query}")

    return list(dict.fromkeys(candidates))
