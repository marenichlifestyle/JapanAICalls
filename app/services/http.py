from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class RetryableHTTPError(Exception):
    pass


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, RetryableHTTPError)),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    response = await client.request(method, url, **kwargs)
    if response.status_code >= 500:
        raise RetryableHTTPError(f"HTTP {response.status_code}: {response.text[:300]}")
    return response
