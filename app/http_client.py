import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log_event
from app.tls import client_ssl_context

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            verify=client_ssl_context(),
            timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30),
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _safe_url(url: str) -> str:
    return str(httpx.URL(url).copy_with(query=None))


async def _backoff(attempt: int, response: httpx.Response | None = None) -> None:
    delay = min(0.25 * 2**attempt, 4.0) + random.uniform(0, 0.1)
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            delay = min(float(retry_after), 10.0)
    await asyncio.sleep(delay)


def _can_retry_exception(exc: httpx.TransportError, idempotent: bool) -> bool:
    if idempotent:
        return True
    # Non-idempotent requests (e.g. placing a call) are only retried if they never reached the server.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _can_retry_status(status: int, idempotent: bool) -> bool:
    return status == 429 or (idempotent and status in RETRYABLE_STATUS)


async def _raise_for_status(response: httpx.Response, url: str) -> None:
    if response.is_error:
        body = (await response.aread())[:500].decode(errors="replace")
        log_event(logger, "upstream http error", logging.ERROR, url=_safe_url(url), status=response.status_code, body=body)
        response.raise_for_status()


async def request_with_retry(
    method: str, url: str, *, idempotent: bool = True, retries: int | None = None, **kwargs: Any
) -> httpx.Response:
    client = get_http_client()
    retries = get_settings().HTTP_MAX_RETRIES if retries is None else retries
    for attempt in range(retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt < retries and _can_retry_exception(exc, idempotent):
                log_event(logger, "http retry", logging.WARNING, url=_safe_url(url), attempt=attempt + 1, error=type(exc).__name__)
                await _backoff(attempt)
                continue
            raise
        if attempt < retries and _can_retry_status(response.status_code, idempotent):
            log_event(logger, "http retry", logging.WARNING, url=_safe_url(url), attempt=attempt + 1, status=response.status_code)
            await _backoff(attempt, response)
            continue
        await _raise_for_status(response, url)
        return response
    raise RuntimeError("unreachable")


@asynccontextmanager
async def stream_with_retry(
    method: str, url: str, *, retries: int | None = None, **kwargs: Any
) -> AsyncIterator[httpx.Response]:
    """Streaming request; retries only until response headers arrive (never mid-stream)."""
    client = get_http_client()
    retries = get_settings().HTTP_MAX_RETRIES if retries is None else retries
    response: httpx.Response | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.send(client.build_request(method, url, **kwargs), stream=True)
        except httpx.TransportError as exc:
            if attempt < retries:
                log_event(logger, "http stream retry", logging.WARNING, url=_safe_url(url), attempt=attempt + 1, error=type(exc).__name__)
                await _backoff(attempt)
                continue
            raise
        if attempt < retries and _can_retry_status(response.status_code, True):
            await response.aclose()
            log_event(logger, "http stream retry", logging.WARNING, url=_safe_url(url), attempt=attempt + 1, status=response.status_code)
            await _backoff(attempt, response)
            continue
        break
    assert response is not None
    try:
        await _raise_for_status(response, url)
        yield response
    finally:
        await response.aclose()
