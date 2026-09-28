import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import get_settings
from app.http_client import request_with_retry, stream_with_retry

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(RuntimeError):
    pass


def user_message(*parts: dict[str, Any] | str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": p} if isinstance(p, str) else p for p in parts]}


def _headers() -> dict[str, str]:
    return {"x-goog-api-key": get_settings().GEMINI_API_KEY}


def _body(contents: list[dict[str, Any]], system: str | None, generation_config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _candidate_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


async def generate_json(
    *,
    model: str,
    contents: list[dict[str, Any]],
    schema: dict[str, Any],
    system: str | None = None,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Structured output: Gemini is constrained to `schema`, so the reply is JSON by construction."""
    config = {"temperature": temperature, "responseMimeType": "application/json", "responseSchema": schema}
    response = await request_with_retry(
        "POST",
        f"{_BASE}/{model}:generateContent",
        json=_body(contents, system, config),
        headers=_headers(),
        timeout=httpx.Timeout(timeout, connect=5.0),
    )
    payload = response.json()
    text = _candidate_text(payload)
    if not text:
        reason = (payload.get("promptFeedback") or {}).get("blockReason") or (
            (payload.get("candidates") or [{}])[0].get("finishReason")
        )
        raise GeminiError(f"Empty response from {model} (reason: {reason})")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"{model} returned invalid JSON") from exc


async def stream_text(
    *,
    model: str,
    contents: list[dict[str, Any]],
    system: str | None = None,
    temperature: float = 0.7,
    max_output_tokens: int = 300,
    thinking_budget: int | None = None,
) -> AsyncIterator[str]:
    config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
    if thinking_budget is not None:
        config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    async with stream_with_retry(
        "POST",
        f"{_BASE}/{model}:streamGenerateContent",
        params={"alt": "sse"},
        json=_body(contents, system, config),
        headers=_headers(),
        timeout=httpx.Timeout(30.0, connect=5.0),
    ) as response:
        finished = False
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            payload = json.loads(data)
            if "error" in payload:
                raise GeminiError(f"{model} stream error: {payload['error'].get('message', '')[:200]}")
            text = _candidate_text(payload)
            if text:
                yield text
            if any(c.get("finishReason") for c in payload.get("candidates") or []):
                finished = True
        # Under load the server can close the stream early without an error; surface it.
        if not finished:
            raise GeminiError(f"{model} stream ended without a finish reason")
