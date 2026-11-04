"""Non-secret TOML settings; the API key is loaded only into the client."""

import os
import tomllib
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator
from tzlocal import get_localzone_name


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: Path = Path(".robot-voice/voice.sqlite3")
    timezone: str = Field(default_factory=get_localzone_name)
    live_model: str = "gpt-live-1"
    backend_model: str = "gpt-6-luna"
    transcribe_model: str = "gpt-transcribe"
    tts_model: str = "gpt-4o-mini-tts"
    voice: str = "marin"
    wake_chime: bool = True
    wake_mode: Literal["vad-transcribe", "keyboard", "keyword"] = "vad-transcribe"
    wake_phrase: str = "hey spot"
    keyword_model: Path | None = None
    audio_mode: Literal["headphones", "external-aec", "half-duplex"] = "half-duplex"
    input_device: int | str | None = None
    output_device: int | str | None = None
    hardware_rate: int = Field(default=16000, ge=16000, le=96000)
    sample_rate: Literal[16000] = 16000
    idle_timeout: float = Field(default=30, ge=1, le=300)
    reply_grace: float = Field(default=5, ge=0, le=30)
    quiet_guard: float = Field(default=1.5, ge=0.1, le=10)
    keep_open_while_task_running: bool = True
    task_hold_max_seconds: float = Field(default=120, ge=0, le=300)
    max_session_seconds: float = Field(default=300, ge=5, le=1200)
    max_active_seconds: float = Field(default=1200, ge=5, le=1200)
    budget_dollars: float = Field(default=4.5, gt=0, le=100)
    max_cloud_requests: int = Field(default=100, ge=1, le=1000)
    backend_timeout: float = Field(default=30, ge=2, le=120)
    close_timeout: float = Field(default=8, ge=1, le=30)
    startup_seconds: float = Field(default=10, ge=1, le=30)
    max_clip_seconds: float = Field(default=15, ge=1, le=30)
    max_utterance_seconds: float = Field(default=45, ge=2, le=60)
    mock_task_seconds: float = Field(default=10, ge=0.01, le=300)

    @model_validator(mode="after")
    def valid(self):
        ZoneInfo(self.timezone)
        if not self.wake_phrase.strip():
            raise ValueError("wake_phrase must not be blank")
        if self.audio_mode == "external-aec" and self.input_device is None:
            raise ValueError("external-aec requires an explicit echo-cancelled input_device")
        return self


def load_config(path: str | None = None, **overrides) -> Config:
    data = tomllib.loads(Path(path).read_text()) if path else {}
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Config.model_validate(data)


def cloud_client():
    from openai import AsyncOpenAI

    load_dotenv(override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY in .env (see .env.example)")
    return AsyncOpenAI(max_retries=0, timeout=30)
