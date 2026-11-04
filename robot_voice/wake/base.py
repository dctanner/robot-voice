from typing import Protocol

from robot_voice.audio.buffers import Frame


class WakeDetector(Protocol):
    def accept(self, text: str) -> str | None: ...


class SpeechDetector(Protocol):
    def speech(self, frame: Frame) -> bool: ...
