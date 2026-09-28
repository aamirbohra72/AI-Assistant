"""Deepgram streaming STT over WebSocket (httpx has no WebSocket support, so `websockets` is used)."""

import asyncio
import json
import logging
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.protocol import State

from app.config import get_settings
from app.logging_setup import log_event
from app.tls import client_ssl_context

logger = logging.getLogger(__name__)

_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramStream:
    def __init__(self) -> None:
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._final_parts: list[str] = []
        self._interim = ""
        self._finalized = asyncio.Event()
        self._reconnect_lock = asyncio.Lock()
        self._closed = False
        self._sent_s = 0.0
        self._final_end_s = 0.0

    @staticmethod
    def _url() -> str:
        settings = get_settings()
        params = {
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "model": settings.DEEPGRAM_MODEL,
            "language": settings.DEEPGRAM_LANGUAGE,
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
        }
        return f"{_URL}?{urlencode(params)}"

    async def connect(self) -> None:
        settings = get_settings()
        if not settings.DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not configured")
        last_exc: Exception | None = None
        for attempt in range(settings.HTTP_MAX_RETRIES + 1):
            try:
                self._ws = await connect(
                    self._url(),
                    additional_headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"},
                    ssl=client_ssl_context(),
                    open_timeout=5,
                    ping_interval=10,
                    max_size=2**20,
                )
                break
            except (OSError, TimeoutError, WebSocketException) as exc:
                last_exc = exc
                log_event(logger, "deepgram connect retry", logging.WARNING, attempt=attempt + 1, error=type(exc).__name__)
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
        else:
            raise RuntimeError("Could not connect to Deepgram") from last_exc
        self._sent_s = self._final_end_s = 0.0
        self._recv_task = asyncio.create_task(self._receive_loop(self._ws))

    async def _receive_loop(self, ws: ClientConnection) -> None:
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                data = json.loads(message)
                if data.get("type") != "Results":
                    continue
                alternatives = (data.get("channel") or {}).get("alternatives") or [{}]
                text = (alternatives[0].get("transcript") or "").strip()
                if data.get("is_final"):
                    if text:
                        self._final_parts.append(text)
                    self._final_end_s = max(self._final_end_s, float(data.get("start", 0)) + float(data.get("duration", 0)))
                    log_event(logger, "stt final", logging.DEBUG, text=text, final_end_s=round(self._final_end_s, 2))
                    self._interim = ""
                    if data.get("from_finalize"):
                        self._finalized.set()
                else:
                    self._interim = text
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("deepgram receive loop failed")

    async def send(self, audio: bytes) -> None:
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(audio)
            self._sent_s += len(audio) / 8000
        except ConnectionClosed:
            logger.warning("deepgram connection dropped; reconnecting")
            await self._reconnect()

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            if self._closed or (self._ws is not None and self._ws.state is State.OPEN):
                return
            if self._recv_task:
                self._recv_task.cancel()
            self._ws = None
            try:
                await self.connect()
            except Exception:
                logger.exception("deepgram reconnect failed")

    async def finalize_utterance(self, timeout_s: float, trailing_silence_s: float) -> str:
        """Return everything heard since the last call, flushing Deepgram only if speech is still pending."""
        ws = self._ws
        speech_ended_at = self._sent_s - trailing_silence_s
        if ws is not None and self._final_end_s < speech_ended_at:
            self._finalized.clear()
            try:
                await ws.send(json.dumps({"type": "Finalize"}))
                await asyncio.wait_for(self._finalized.wait(), timeout_s)
            except (ConnectionClosed, TimeoutError):
                pass
        text = " ".join(self._final_parts).strip() or self._interim
        self._final_parts.clear()
        self._interim = ""
        return text

    async def close(self) -> None:
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
                await ws.close()
            except (ConnectionClosed, OSError):
                pass
        if self._recv_task:
            self._recv_task.cancel()
