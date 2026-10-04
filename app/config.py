from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    # Public HTTPS origin Twilio can reach, e.g. https://ai-voice-interviewer.onrender.com
    PUBLIC_BASE_URL: str

    ADMIN_API_KEY: str = Field(min_length=16)
    INTERNAL_API_TOKEN: str = Field(min_length=16)
    STREAM_TOKEN_SECRET: str = Field(min_length=16)

    DATABASE_URL: str
    REDIS_URL: str

    GEMINI_API_KEY: str
    GEMINI_FLASH_MODEL: str = "gemini-flash-latest"
    GEMINI_RESUME_FALLBACK_MODEL: str = "gemini-3.7-flash"
    GEMINI_PRO_MODEL: str = "gemini-pro-latest"
    # Used for scoring when the Pro model fails (e.g. Pro has no free-tier quota).
    GEMINI_SCORING_FALLBACK_MODEL: str = "gemini-flash-latest"
    # 0 disables "thinking" on Flash for lowest latency; set empty to use the model default.
    GEMINI_FLASH_THINKING_BUDGET: int | None = 0

    RESUME_PROVIDER: Literal["gemini", "groq"] = "gemini"
    GROQ_API_KEY: str = ""
    GROQ_RESUME_MODEL: str = "llama-3.3-70b-versatile"

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    TWILIO_VALIDATE_SIGNATURES: bool = True

    DEEPGRAM_API_KEY: str = ""
    DEEPGRAM_MODEL: str = "nova-3"
    DEEPGRAM_LANGUAGE: str = "en"
    STT_FINALIZE_TIMEOUT_MS: int = 400

    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = ""
    ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"

    VAD_MODEL_PATH: str = "models/silero_vad.onnx"
    VAD_THRESHOLD: float = 0.5
    VAD_MIN_SPEECH_MS: int = 200
    VAD_SILENCE_MS: int = 600
    BARGE_IN_MS: int = 500

    RESPONSE_DELAY_MIN_MS: int = 300
    RESPONSE_DELAY_MAX_MS: int = 700

    INTERVIEWER_NAME: str = "Alex"
    COMPANY_NAME: str = "our company"

    RUN_WORKER_IN_WEB: bool = True
    # Each poll costs ~3 Redis commands; 30s keeps Upstash free tier (~500k/month) safe.
    WORKER_POLL_SECONDS: float = 30.0
    CALL_STATE_TTL_SECONDS: int = 7200
    MAX_STREAM_RECONNECTS: int = 3

    HTTP_TIMEOUT_SECONDS: float = 20.0
    HTTP_MAX_RETRIES: int = 3
    MAX_RESUME_BYTES: int = 10 * 1024 * 1024

    @property
    def public_base(self) -> str:
        return self.PUBLIC_BASE_URL.rstrip("/")

    @property
    def public_ws_base(self) -> str:
        base = self.public_base
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base


@lru_cache
def get_settings() -> Settings:
    return Settings()
