"""PortAudio never supplies AEC. External AEC must include ALL output in its reference."""

import asyncio
import queue
import time
from typing import Protocol

import numpy as np

from .buffers import AudioOverflow, Frame
from .playback import Playback


def sounddevice():
    try:
        import sounddevice as sd

        return sd
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "PortAudio unavailable. Install libportaudio2 (Linux), portaudio "
            "(Homebrew), or the Windows sounddevice wheel; run doctor."
        ) from exc


class AudioFrontend(Protocol):
    async def read(self) -> Frame: ...
    def close(self) -> None: ...


class Resampler:
    """Stateful linear interpolation, suitable for prototype speech, not mastering audio."""

    def __init__(self, source: int, target: int):
        self.step = source / target
        self.position = 0.0
        self.total = 0
        self.previous: float | None = None

    def process(self, pcm: bytes) -> bytes:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if not len(samples):
            return b""
        start = self.total
        self.total += len(samples)
        if self.previous is not None:
            samples = np.concatenate(([self.previous], samples))
            start -= 1
        positions = np.arange(self.position, self.total - 1, self.step)
        self.position += len(positions) * self.step
        result = np.interp(positions, np.arange(start, self.total), samples)
        self.previous = float(samples[-1])
        return np.rint(result).clip(-32768, 32767).astype("<i2").tobytes()


class Microphone:
    def __init__(self, config, playback: Playback):
        sd = sounddevice()
        self.config = config
        self.playback = playback
        self.capture: queue.Queue[tuple[bytes, float]] = queue.Queue(maxsize=100)
        self.failed = False
        self.index = 0
        self.resampler = Resampler(config.hardware_rate, config.sample_rate)
        self.pending = bytearray()
        self.input = sd.RawInputStream(
            device=config.input_device,
            samplerate=config.hardware_rate,
            channels=1,
            dtype="int16",
            blocksize=int(config.hardware_rate * 0.032),
            callback=self._capture,
        )
        self.output = sd.RawOutputStream(
            device=config.output_device,
            samplerate=16000,
            channels=1,
            dtype="int16",
            blocksize=512,
            callback=self._play,
        )
        self.input.start()
        self.output.start()

    def _capture(self, data, frames, info, status):
        if status:
            self.failed = True
        try:
            self.capture.put_nowait((bytes(data), time.monotonic()))
        except queue.Full:
            self.failed = True

    def _play(self, data, frames, info, status):
        data[:] = self.playback.pull(frames)
        if status:
            self.playback.underruns += 1

    async def read(self) -> Frame:
        stamp = time.monotonic()
        while len(self.pending) < 1024:
            if self.failed:
                self.failed = False
                raise AudioOverflow("Microphone overflow/device fault; repeat the utterance")
            try:
                pcm, stamp = self.capture.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            self.pending.extend(
                self.resampler.process(pcm) if self.config.hardware_rate != 16000 else pcm
            )
        pcm = bytes(self.pending[:1024])
        del self.pending[:1024]
        if self.config.audio_mode == "half-duplex" and self.playback.busy(0.3):
            pcm = bytes(len(pcm))  # explicit no-barge-in mode; billing continues
        frame = Frame(self.index, pcm, stamp)
        self.index = frame.end
        return frame

    def close(self):
        self.input.close()
        self.output.close()
