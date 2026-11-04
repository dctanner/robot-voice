import time
from collections import deque
from dataclasses import dataclass


class AudioOverflow(RuntimeError):
    """The utterance must be abandoned, not partially executed."""


@dataclass(frozen=True)
class Frame:
    start: int
    pcm: bytes
    captured: float
    rate: int = 16000

    @property
    def end(self) -> int:
        return self.start + len(self.pcm) // 2

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / self.rate


class SampleBuffer:
    def __init__(self, seconds: float, rate: int = 16000, rolling: bool = False):
        self.capacity = int(seconds * rate)
        self.frames: deque[Frame] = deque()
        self.samples = 0
        self.rolling = rolling
        self.end: int | None = None

    def append(self, frame: Frame):
        if self.end is not None and frame.start != self.end:
            raise AudioOverflow("Capture gap or duplicate; please repeat")
        if frame.end - frame.start > self.capacity:
            raise AudioOverflow("Frame larger than audio buffer")
        if not self.rolling and self.samples + frame.end - frame.start > self.capacity:
            raise AudioOverflow("Startup audio overflow; please repeat")
        self.frames.append(frame)
        self.samples += frame.end - frame.start
        self.end = frame.end
        while self.samples > self.capacity:
            self.samples -= len(self.frames.popleft().pcm) // 2

    def drain_after(self, cutoff: int) -> list[Frame]:
        result = []
        while self.frames:
            frame = self.frames.popleft()
            if frame.end > cutoff:
                start = max(frame.start, cutoff)
                result.append(
                    Frame(start, frame.pcm[(start - frame.start) * 2 :], frame.captured, frame.rate)
                )
        self.samples = 0
        return result


class PacedSender:
    """Single owner. Backlog remains latency: never burst or accelerate speech."""

    def __init__(self, send, rate: int = 16000):
        self.send = send
        self.rate = rate
        self.end: int | None = None
        self.deadline = 0.0

    async def append(self, frame: Frame):
        import asyncio

        if self.end is not None and frame.start != self.end:
            raise AudioOverflow("Noncontiguous input; close and repeat")
        if not self.deadline:
            self.deadline = time.monotonic()
        await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        await self.send(frame.pcm)
        self.end = frame.end
        self.deadline += frame.seconds
        # Transport time is part of the frame interval, not an extra delay. Rebase
        # after a full-frame stall instead of emitting a burst to catch up.
        if self.deadline < time.monotonic():
            self.deadline = time.monotonic() + frame.seconds
