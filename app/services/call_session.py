"""One live phone call: Twilio audio -> Silero VAD + Deepgram -> Gemini Flash -> ElevenLabs -> Twilio."""

import asyncio
import base64
import json
import logging
import random
import time
from collections.abc import Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.dialects.postgresql import insert

from app import redis_store
from app.config import get_settings
from app.db import SessionLocal
from app.logging_setup import interview_id_var, log_event
from app.models import Speaker, Transcript
from app.services import interviews, tts, twilio_client
from app.services.interviewer import (
    RECONNECT_DIRECTIVE,
    ConversationState,
    InterviewAgent,
    TurnMeta,
    opening_directive,
)
from app.services.stt import DeepgramStream
from app.services.vad import SileroVAD, TurnDetector, load_vad_session

logger = logging.getLogger(__name__)

SORRY_REPEAT = "Sorry, I didn't quite catch that. Could you say that again?"
SORRY_TECHNICAL = (
    "I'm sorry, we're having technical difficulties on our side. The recruiting team will reach out to "
    "reschedule. Goodbye."
)
HARD_TIME_LIMIT_GRACE_S = 120


def _ms_since(t: float) -> int:
    return int((time.monotonic() - t) * 1000)


class CallSession:
    def __init__(self, ws: WebSocket, interview_id: int) -> None:
        self.ws = ws
        self.interview_id = interview_id
        self.settings = get_settings()
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.agent: InterviewAgent | None = None
        self.stt: DeepgramStream | None = None
        self.detector: TurnDetector | None = None

        self._send_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._response_task: asyncio.Task | None = None
        self._response_interruptible = False
        self._response_audio_started = False
        self._pending_mark: str | None = None
        self._mark_seq = 0
        self._carry_text = ""
        self._end_after_playback = False
        self._finishing = False
        self._shutting_down = False
        self._stopped_by_twilio = False
        self._ws_open = True

        self._transcripts: asyncio.Queue[tuple[int, str, str, datetime] | None] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        token = interview_id_var.set(self.interview_id)
        interviews.ACTIVE_SESSIONS.add(self.interview_id)
        started = False
        try:
            await self.ws.accept()
            async for raw in self.ws.iter_text():
                message = json.loads(raw)
                event = message.get("event")
                if event == "media":
                    if started:
                        await self._on_media(message.get("media") or {})
                elif event == "start":
                    started = await self._on_start(message.get("start") or {})
                    if not started:
                        break
                elif event == "mark":
                    self._on_mark((message.get("mark") or {}).get("name"))
                elif event == "stop":
                    self._stopped_by_twilio = True
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("media stream failed")
        finally:
            self._ws_open = False
            if started:
                await self._shutdown()
            interviews.ACTIVE_SESSIONS.discard(self.interview_id)
            interview_id_var.reset(token)

    async def _on_start(self, start: dict[str, Any]) -> bool:
        params = start.get("customParameters") or {}
        if not twilio_client.verify_stream_token(self.interview_id, params.get("token", "")):
            log_event(logger, "rejected media stream: invalid token", logging.WARNING)
            await self.ws.close(code=1008)
            return False
        self.stream_sid = start.get("streamSid")
        self.call_sid = start.get("callSid")

        ctx = await interviews.load_interview_context(self.interview_id)
        if ctx is None or not await interviews.mark_in_progress(self.interview_id, self.call_sid):
            log_event(logger, "rejected media stream: interview not callable", logging.WARNING)
            await self.ws.close(code=1008)
            return False

        cached = await redis_store.load_state(self.interview_id)
        state = ConversationState.from_dict(cached) if cached else ConversationState()
        self.agent = InterviewAgent(ctx, state)
        s = self.settings
        self.detector = TurnDetector(
            SileroVAD(load_vad_session(s.VAD_MODEL_PATH)), s.VAD_THRESHOLD, s.VAD_MIN_SPEECH_MS, s.VAD_SILENCE_MS
        )
        self.stt = DeepgramStream()
        await self.stt.connect()

        self._writer_task = asyncio.create_task(self._transcript_writer())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        directive = RECONNECT_DIRECTIVE if state.history else opening_directive(ctx)
        self._start_response(directive, log_user=False, interruptible=False, turn_end=time.monotonic())
        log_event(logger, "media stream started", call_sid=self.call_sid, resumed=bool(state.history))
        return True

    async def _shutdown(self) -> None:
        self._shutting_down = True
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._response_task
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        if self.stt:
            await self.stt.close()
        if self._writer_task:
            self._transcripts.put_nowait(None)
            with suppress(Exception):
                await asyncio.wait_for(self._writer_task, timeout=15)
        if self.agent is None:
            return
        try:
            if self._stopped_by_twilio or self._finishing:
                await interviews.finalize_interview(self.interview_id)
            else:
                # Unexpected disconnect: keep state so Twilio's <Redirect> can resume the call.
                await redis_store.save_state(self.interview_id, self.agent.state.to_dict())
                log_event(logger, "media stream dropped; state kept for reconnect", logging.WARNING)
        except Exception:
            logger.exception("failed to finalize interview")

    async def _finish_call(self, reason: str) -> None:
        if self._finishing:
            return
        self._finishing = True
        log_event(logger, "ending call", reason=reason)
        if self.call_sid:
            try:
                await twilio_client.hangup(self.call_sid)
            except Exception:
                logger.exception("twilio hangup failed")
        if self._ws_open:
            with suppress(Exception):
                await self.ws.close()

    async def _watchdog(self) -> None:
        assert self.agent is not None
        while True:
            await asyncio.sleep(2)
            if self._end_after_playback and not self._agent_active():
                await self._finish_call("interview complete")
                return
            if self.agent.elapsed_s > self.agent.ctx.max_duration_s + HARD_TIME_LIMIT_GRACE_S:
                await self._finish_call("hard time limit")
                return

    # ------------------------------------------------------------------ inbound audio

    async def _on_media(self, media: dict[str, Any]) -> None:
        if media.get("track", "inbound") != "inbound" or not media.get("payload"):
            return
        assert self.stt is not None and self.detector is not None
        audio = base64.b64decode(media["payload"])
        await self.stt.send(audio)
        for event in self.detector.process(audio):
            if event == "speech_end":
                self._spawn(self._on_turn_end(time.monotonic()))
        if (
            self.detector.in_speech
            and self.detector.speech_ms >= self.settings.BARGE_IN_MS
            and self._response_interruptible
            and self._agent_active()
        ):
            await self._interrupt()

    def _agent_active(self) -> bool:
        return bool(self._response_task and not self._response_task.done()) or self._pending_mark is not None

    async def _interrupt(self) -> None:
        task = self._response_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._response_audio_started or self._pending_mark:
            await self._send({"event": "clear", "streamSid": self.stream_sid})
        self._pending_mark = None
        log_event(logger, "barge-in: agent interrupted")

    async def _on_turn_end(self, turn_end: float) -> None:
        assert self.stt is not None and self.agent is not None
        async with self._turn_lock:
            text = await self.stt.finalize_utterance(
                self.settings.STT_FINALIZE_TIMEOUT_MS / 1000, self.settings.VAD_SILENCE_MS / 1000
            )
            stt_ms = _ms_since(turn_end)
            if not text.strip() or self.agent.state.ended:
                return
            if self._agent_active():
                task = self._response_task
                if task and not task.done() and not self._response_audio_started and self._response_interruptible:
                    # Candidate kept talking before we spoke: drop the draft and answer everything together.
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                else:
                    log_event(logger, "ignored short utterance during agent speech", chars=len(text))
                    return
            user_text = " ".join(filter(None, [self._carry_text, text.strip()]))
            self._carry_text = ""
            log_event(logger, "candidate turn end", stt_finalize_ms=stt_ms, chars=len(user_text))
            self._start_response(user_text, log_user=True, interruptible=True, turn_end=turn_end)

    # ------------------------------------------------------------------ outbound speech

    def _start_response(self, user_text: str, *, log_user: bool, interruptible: bool, turn_end: float) -> None:
        if self._shutting_down:
            return
        self._response_interruptible = interruptible
        self._response_audio_started = False
        self._response_task = asyncio.create_task(self._respond(user_text, log_user, turn_end))

    async def _respond(self, user_text: str, log_user: bool, turn_end: float) -> None:
        assert self.agent is not None
        s = self.settings
        # Natural pacing: audio never starts before this, but LLM/TTS work runs concurrently with the wait.
        play_at = turn_end + random.uniform(s.RESPONSE_DELAY_MIN_MS, s.RESPONSE_DELAY_MAX_MS) / 1000
        pieces: asyncio.Queue[str | None] = asyncio.Queue()
        spoken: list[str] = []
        timings: dict[str, int] = {}

        async def produce() -> None:
            try:
                async for piece in self.agent.respond(user_text):
                    timings.setdefault("llm_first_piece_ms", _ms_since(turn_end))
                    pieces.put_nowait(piece)
            finally:
                pieces.put_nowait(None)

        producer = asyncio.create_task(produce())
        try:
            while (piece := await pieces.get()) is not None:
                piece_spoken = False
                async for audio in tts.stream_speech(piece):
                    if not self._response_audio_started:
                        timings["tts_first_byte_ms"] = _ms_since(turn_end)
                        delay = play_at - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._response_audio_started = True
                        timings["first_audio_ms"] = _ms_since(turn_end)
                        if self.agent.state.wrapping_up:
                            self._response_interruptible = False
                    if not piece_spoken:
                        spoken.append(piece)
                        piece_spoken = True
                    await self._send_media(audio)
            await producer
        except asyncio.CancelledError:
            producer.cancel()
            if self._response_audio_started:
                self._commit_turn(user_text, spoken, log_user, meta=None)
            elif log_user:
                self._carry_text = " ".join(filter(None, [user_text, self._carry_text]))
            raise
        except Exception:
            producer.cancel()
            logger.exception("response pipeline failed")
            if self._response_audio_started:
                self._commit_turn(user_text, spoken, log_user, meta=None)
                await self._send_mark()
            elif log_user:
                await self._speak(SORRY_REPEAT)
            else:
                self._end_after_playback = True
                await self._speak(SORRY_TECHNICAL)
            return

        self._commit_turn(user_text, spoken, log_user, meta=self.agent.last_meta)
        await self._send_mark()
        log_event(logger, "agent turn latency", pieces=len(spoken), **timings)

    async def _speak(self, text: str) -> None:
        try:
            async for audio in tts.stream_speech(text):
                await self._send_media(audio)
            await self._send_mark()
        except Exception:
            logger.exception("fallback speech failed")

    def _commit_turn(self, user_text: str, spoken: list[str], log_user: bool, meta: TurnMeta | None) -> None:
        assert self.agent is not None
        agent_text = " ".join(spoken).strip()
        if not agent_text:
            return
        self.agent.commit(user_text, agent_text, meta)
        if log_user:
            self._queue_transcript(Speaker.CANDIDATE, user_text)
        self._queue_transcript(Speaker.AGENT, agent_text)
        self._spawn(redis_store.save_state(self.interview_id, self.agent.state.to_dict()))
        if self.agent.state.ended:
            self._end_after_playback = True
            self._response_interruptible = False

    # ------------------------------------------------------------------ twilio I/O

    async def _send(self, message: dict[str, Any]) -> None:
        if not self._ws_open:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(message))
            except (WebSocketDisconnect, RuntimeError):
                self._ws_open = False

    async def _send_media(self, audio: bytes) -> None:
        await self._send(
            {"event": "media", "streamSid": self.stream_sid, "media": {"payload": base64.b64encode(audio).decode()}}
        )

    async def _send_mark(self) -> None:
        self._mark_seq += 1
        name = f"turn-{self._mark_seq}"
        self._pending_mark = name
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    def _on_mark(self, name: str | None) -> None:
        # Twilio echoes a mark once all audio sent before it has finished playing.
        if name and name == self._pending_mark:
            self._pending_mark = None
            if self._end_after_playback:
                self._spawn(self._finish_call("interview complete"))

    # ------------------------------------------------------------------ persistence

    def _queue_transcript(self, speaker: Speaker, text: str) -> None:
        assert self.agent is not None
        index = self.agent.state.next_turn_index
        self.agent.state.next_turn_index += 1
        self._transcripts.put_nowait((index, speaker.value, text, datetime.now(UTC)))

    async def _transcript_writer(self) -> None:
        while (item := await self._transcripts.get()) is not None:
            turn_index, speaker, text, ts = item
            try:
                async with SessionLocal() as db:
                    await db.execute(
                        insert(Transcript)
                        .values(interview_id=self.interview_id, turn_index=turn_index, speaker=speaker, text=text, timestamp=ts)
                        .on_conflict_do_nothing(constraint="uq_transcripts_interview_turn")
                    )
                    await db.commit()
            except Exception:
                logger.exception("failed to persist transcript turn")

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background task failed", exc_info=t.exception())

        task.add_done_callback(_done)
