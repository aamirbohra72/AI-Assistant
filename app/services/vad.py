"""Silero VAD (ONNX, self-hosted) + end-of-utterance detection on Twilio's 8 kHz mu-law audio."""

import threading
from pathlib import Path

import httpx
import numpy as np
import onnxruntime as ort

from app.tls import client_ssl_context

SAMPLE_RATE = 8000
CHUNK_SAMPLES = 256  # window size Silero v5 expects at 8 kHz
CONTEXT_SAMPLES = 32
CHUNK_MS = CHUNK_SAMPLES * 1000 // SAMPLE_RATE
MODEL_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"


def _build_ulaw_table() -> np.ndarray:
    codes = ~np.arange(256, dtype=np.uint8)
    sign = codes & 0x80
    exponent = ((codes >> 4) & 0x07).astype(np.int32)
    mantissa = (codes & 0x0F).astype(np.int32)
    magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return (np.where(sign != 0, -magnitude, magnitude) / 32768.0).astype(np.float32)


_ULAW_TABLE = _build_ulaw_table()


def ulaw_to_float(data: bytes) -> np.ndarray:
    return _ULAW_TABLE[np.frombuffer(data, dtype=np.uint8)]


def ensure_model_file(path: str) -> Path:
    target = Path(path)
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60, verify=client_ssl_context()) as client:
        response = client.get(MODEL_URL)
        response.raise_for_status()
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(target)
    return target


_session: ort.InferenceSession | None = None
_session_lock = threading.Lock()


def load_vad_session(path: str) -> ort.InferenceSession:
    global _session
    with _session_lock:
        if _session is None:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            _session = ort.InferenceSession(
                str(ensure_model_file(path)), sess_options=options, providers=["CPUExecutionProvider"]
            )
    return _session


class SileroVAD:
    """Per-call recurrent state around the shared ONNX session."""

    def __init__(self, session: ort.InferenceSession) -> None:
        self._session = session
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self._context, chunk.reshape(1, -1)], axis=1)
        out, self._state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(out[0][0])


class TurnDetector:
    """Emits "speech_start" / "speech_end" events from a stream of mu-law frames."""

    def __init__(self, vad: SileroVAD, threshold: float, min_speech_ms: int, silence_ms: int) -> None:
        self._vad = vad
        self._threshold = threshold
        self._neg_threshold = max(threshold - 0.15, 0.01)
        self._min_speech_ms = min_speech_ms
        self._silence_limit_ms = silence_ms
        self._pending = np.zeros(0, dtype=np.float32)
        self._onset_ms = 0
        self._silence_ms = 0
        self.in_speech = False
        self.speech_ms = 0

    def process(self, mulaw: bytes) -> list[str]:
        self._pending = np.concatenate([self._pending, ulaw_to_float(mulaw)])
        events: list[str] = []
        while len(self._pending) >= CHUNK_SAMPLES:
            chunk, self._pending = self._pending[:CHUNK_SAMPLES], self._pending[CHUNK_SAMPLES:]
            prob = self._vad(chunk)
            if not self.in_speech:
                self._onset_ms = self._onset_ms + CHUNK_MS if prob >= self._threshold else 0
                if self._onset_ms >= self._min_speech_ms:
                    self.in_speech = True
                    self.speech_ms = self._onset_ms
                    self._silence_ms = 0
                    events.append("speech_start")
                continue
            if prob < self._neg_threshold:
                self._silence_ms += CHUNK_MS
            else:
                self.speech_ms += CHUNK_MS
                if prob >= self._threshold:
                    self._silence_ms = 0
            if self._silence_ms >= self._silence_limit_ms:
                self.in_speech = False
                self.speech_ms = 0
                self._onset_ms = 0
                self._silence_ms = 0
                events.append("speech_end")
        return events
