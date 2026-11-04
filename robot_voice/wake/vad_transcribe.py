import io
import re
import wave
from collections import deque
from dataclasses import dataclass
from uuid import uuid4

import numpy as np

from robot_voice.audio.buffers import AudioOverflow, Frame


def words(text):
    return re.findall(r"\w+", text.casefold())


class ExplicitWake:
    def __init__(self, phrase="hey spot"):
        self.phrase = words(phrase)
        if not self.phrase:
            raise ValueError("Wake phrase must contain words")

    def accept(self, text):
        tokens = words(text)
        # Only an optional politeness prefix is allowed, never arbitrary background mentions.
        start = 1 if tokens[:1] == ["please"] else 0
        if tokens[start : start + len(self.phrase)] == self.phrase:
            return text  # original whole utterance is seeded once, not reconstructed
        return None


class SileroSpeech:
    def __init__(self):
        try:
            import torch
            from silero_vad import load_silero_vad
        except ImportError as exc:
            raise RuntimeError("Install robot-voice[vad] for microphone Silero VAD") from exc
        self.torch = torch
        torch.set_num_threads(1)
        self.model = load_silero_vad()

    def speech(self, frame):
        data = np.frombuffer(frame.pcm, dtype="<i2").astype(np.float32) / 32768
        if len(data) != 512:
            raise ValueError("Silero input must be 512 samples at 16 kHz")
        with self.torch.no_grad():
            return self.model(self.torch.from_numpy(data), 16000).item() >= 0.5


class FixtureSpeech:
    """Deterministic fixture detector, deliberately not offered as room VAD."""

    def speech(self, frame):
        data = np.frombuffer(frame.pcm, dtype="<i2").astype(np.float32)
        return bool(len(data) and np.sqrt(np.mean(data * data)) > 200)


@dataclass
class Segment:
    frames: list[Frame]

    @property
    def cutoff(self):
        return self.frames[-1].end

    @property
    def pcm(self):
        return b"".join(frame.pcm for frame in self.frames)


# A boundary requires continuous captured silence, not just elapsed wall time.
UTTERANCE_SILENCE_SECONDS = 0.6


class Segmenter:
    def __init__(self, max_seconds=45):
        self.pre: deque[Frame] = deque(maxlen=10)
        self.active: list[Frame] = []
        self.silent = 0.0
        self.max_seconds = max_seconds
        self.rejected = False
        self.end: int | None = None

    def reset(self, *, discard_until_silence=False):
        self.pre.clear()
        self.active.clear()
        self.silent = 0
        self.end = None
        self.rejected = discard_until_silence

    def feed(self, frame, speech):
        if self.end is not None and frame.start != self.end:
            already_discarding = self.rejected
            self.reset(discard_until_silence=True)
            self.end = frame.end
            if already_discarding:
                return None
            raise AudioOverflow("Capture gap or duplicate; discard through silence and repeat")
        self.end = frame.end
        if self.rejected:
            if not speech:
                self.silent += frame.seconds
                if self.silent >= UTTERANCE_SILENCE_SECONDS:
                    self.rejected = False
                    self.silent = 0
            else:
                self.silent = 0
            return None
        if not self.active:
            if not speech:
                self.pre.append(frame)
                return None
            self.active = list(self.pre)
            self.pre.clear()
        self.active.append(frame)
        self.silent = 0 if speech else self.silent + frame.seconds
        if len(self.active) * 0.032 > self.max_seconds:
            self.active.clear()
            self.rejected = True
            self.silent = 0
            raise AudioOverflow("Utterance too long; no partial command executed. Please repeat.")
        if self.silent >= UTTERANCE_SILENCE_SECONDS:
            segment = Segment(self.active)
            self.active = []
            self.silent = 0
            return segment
        return None


def wav_bytes(pcm, rate=16000):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return data.getvalue()


def merge_overlap(left: str, right: str) -> str:
    # Only the bounded overlap is deduplicated; intentional repetitions elsewhere survive.
    a, b = left.split(), right.split()
    for size in range(min(12, len(a), len(b)), 0, -1):
        if words(" ".join(a[-size:])) == words(" ".join(b[:size])):
            return left + " " + " ".join(b[size:])
    return left + " " + right


class Transcriber:
    def __init__(self, client, config, budget, store):
        self.client, self.config, self.budget, self.store = client, config, budget, store

    async def transcribe(self, segment):
        pcm = segment.pcm
        chunk_size = int(self.config.max_clip_seconds * 16000) * 2
        overlap = 16000  # 0.5s; included in cost
        parts = []
        for start in range(0, len(pcm), max(2, chunk_size - overlap)):
            clip = pcm[start : start + chunk_size]
            identity = "transcribe_" + uuid4().hex
            self.budget.reserve(identity)
            response = await self.client.audio.transcriptions.create(
                model=self.config.transcribe_model,
                file=("synthetic-or-mic.wav", wav_bytes(clip), "audio/wav"),
            )
            duration = len(clip) / 32000
            self.budget.record(identity, "transcribe", {"seconds": duration})
            self.store.usage(
                identity,
                None,
                self.config.transcribe_model,
                {"seconds": duration, "estimated": True},
                False,
            )
            parts.append(response.text)
            if start + chunk_size >= len(pcm):
                break
        result = parts[0] if parts else ""
        for part in parts[1:]:
            result = merge_overlap(result, part)
        return result.strip()
