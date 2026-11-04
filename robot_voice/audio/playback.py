import threading
import time
from collections import deque

import numpy as np

from .buffers import AudioOverflow


class Playback:
    """Callback-safe bounded PCM queue; played counts hardware-submitted samples, not words."""

    def __init__(self, rate=16000, max_seconds=10, discard=False):
        self.rate = rate
        self.capacity = int(rate * max_seconds * 2)
        self.queue: deque[bytes] = deque()
        self.lock = threading.Lock()
        self.queued = 0
        self.received = 0
        self.played = 0
        self.underruns = 0
        self.last_output = 0.0
        self.discard = discard
        self.virtual_end = 0.0
        self.speech_until = 0.0

    def push(self, pcm: bytes):
        with self.lock:
            now = time.monotonic()
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            speech = bool(len(samples) and np.sqrt(np.mean(samples * samples)) > 100)
            self.received += len(pcm) // 2
            duration = len(pcm) / 2 / self.rate
            if self.discard:
                self.virtual_end = max(self.virtual_end, now) + duration
                if speech:
                    self.last_output = now
                    self.speech_until = self.virtual_end
                return
            if speech:
                self.last_output = now
                self.speech_until = now + (self.queued + len(pcm)) / 2 / self.rate
            if self.queued + len(pcm) > self.capacity:
                raise AudioOverflow("Speaker queue overflow; output delivery unconfirmed")
            self.queue.append(pcm)
            self.queued += len(pcm)

    def chime(self):
        samples = np.arange(int(self.rate * 0.08))
        envelope = np.sin(np.pi * samples / len(samples)) ** 2
        pcm = 2000 * envelope * np.sin(2 * np.pi * 880 * samples / self.rate)
        self.push(pcm.astype("<i2").tobytes())

    def pull(self, samples: int) -> bytes:
        with self.lock:
            need = samples * 2
            parts = []
            while need and self.queue:
                data = self.queue.popleft()
                part, rest = data[:need], data[need:]
                parts.append(part)
                need -= len(part)
                self.queued -= len(part)
                self.played += len(part) // 2
                if rest:
                    self.queue.appendleft(rest)
            if need:
                self.underruns += 1
            return b"".join(parts) + bytes(need)

    def busy(self, quiet_guard: float = 1.5) -> bool:
        with self.lock:
            # Live sends continuous PCM, including silence; silence is not assistant speech.
            return time.monotonic() < self.speech_until + quiet_guard
