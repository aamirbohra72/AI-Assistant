from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.http_client import stream_with_retry

_BASE = "https://api.elevenlabs.io/v1"


async def stream_speech(text: str) -> AsyncIterator[bytes]:
    """Yields 8 kHz mu-law audio (Twilio's native format) as ElevenLabs produces it."""
    settings = get_settings()
    if not settings.ELEVENLABS_API_KEY or not settings.ELEVENLABS_VOICE_ID:
        raise RuntimeError("ElevenLabs is not configured (ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID)")
    async with stream_with_retry(
        "POST",
        f"{_BASE}/text-to-speech/{quote(settings.ELEVENLABS_VOICE_ID, safe='')}/stream",
        params={"output_format": "ulaw_8000"},
        headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
        json={
            "text": text,
            "model_id": settings.ELEVENLABS_MODEL_ID,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=httpx.Timeout(15.0, connect=5.0),
    ) as response:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
