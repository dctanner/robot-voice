import asyncio
import time

import numpy as np
import pytest

from robot_voice.audio.buffers import AudioOverflow, Frame, PacedSender, SampleBuffer
from robot_voice.audio.frontend import Resampler
from robot_voice.audio.playback import Playback
from robot_voice.wake.vad_transcribe import ExplicitWake, Segmenter, merge_overlap


def frame(start=0, count=512, loud=False):
    return Frame(
        start, (np.ones(count, dtype="<i2") * (1000 if loud else 0)).tobytes(), time.monotonic()
    )


@pytest.mark.parametrize(
    "text", ["Hey Spot, pick up the red sock.", "hey spot!", "Please, hey Spot, hello"]
)
def test_explicit_wake_preserves_entire_text(text):
    assert ExplicitWake().accept(text) == text


@pytest.mark.parametrize(
    "text", ["spotless floor", "they said hey spot", "hey spotlight", "hey spotty", "hi there"]
)
def test_not_addressed_rejected(text):
    assert ExplicitWake().accept(text) is None


def test_word_overlap_only():
    assert (
        merge_overlap("pick up the red sock", "red sock on the floor")
        == "pick up the red sock on the floor"
    )
    assert merge_overlap("red", "blue") == "red blue"


def test_cutoff_no_duplicate_audio():
    b = SampleBuffer(1)
    b.append(frame(0))
    b.append(frame(512))
    drained = b.drain_after(700)
    assert drained[0].start == 700
    assert drained[0].end == 1024
    assert b.drain_after(0) == []


def test_startup_overflow_visible():
    b = SampleBuffer(0.032)
    b.append(frame())
    with pytest.raises(AudioOverflow):
        b.append(frame(512))


def test_capture_gap_and_duplicate_fail():
    for index in (0, 513):
        b = SampleBuffer(1)
        b.append(frame())
        with pytest.raises(AudioOverflow):
            b.append(frame(index))


def test_ring_bound():
    b = SampleBuffer(0.064, rolling=True)
    for n in range(10):
        b.append(frame(n * 512))
    assert b.samples == 1024
    assert b.frames[0].start == 4096


def test_segment_preroll_and_whole_long_utterance():
    s = Segmenter(2)
    for n in range(10):
        assert s.feed(frame(n * 512), False) is None
    for n in range(10, 25):
        assert s.feed(frame(n * 512, loud=True), True) is None
    result = None
    for n in range(25, 44):
        result = s.feed(frame(n * 512), False)
    assert result is not None
    assert result.frames[0].start == 0
    assert result.cutoff == 44 * 512


def test_max_utterance_rejects_instead_of_partial_action():
    s = Segmenter(0.1)
    with pytest.raises(AudioOverflow):
        for n in range(5):
            s.feed(frame(n * 512), True)
    assert not s.active and s.rejected
    assert s.feed(frame(6 * 512), True) is None


@pytest.mark.parametrize(
    "source,target", [(48000, 16000), (44100, 16000), (24000, 16000), (16000, 24000)]
)
def test_stateful_resampler_chunk_equivalence(source, target):
    data = (np.sin(np.arange(5000) * 0.02) * 10000).astype("<i2").tobytes()
    one = Resampler(source, target).process(data)
    r = Resampler(source, target)
    chunks = b"".join(r.process(data[n : n + 1024]) for n in range(0, len(data), 1024))
    a, b = np.frombuffer(one, "<i2"), np.frombuffer(chunks, "<i2")
    assert len(a) == len(b)
    assert np.max(abs(a.astype(int) - b.astype(int))) <= 1


async def test_paced_sender_does_not_burst():
    times = []

    async def send(pcm):
        times.append(time.monotonic())

    p = PacedSender(send)
    for n in range(3):
        await p.append(frame(n * 512))
    assert times[-1] - times[0] >= 0.06
    with pytest.raises(AudioOverflow):
        await p.append(frame(0))


def test_playback_accounting_and_bound():
    p = Playback(max_seconds=0.032)
    p.push(frame(loud=True).pcm)
    assert p.busy()
    assert p.pull(256) == frame(count=256, loud=True).pcm
    assert p.played == 256
    with pytest.raises(AudioOverflow):
        p.push(frame(loud=True).pcm)
    p.pull(512)
    assert p.underruns == 1


def test_continuous_output_silence_does_not_prevent_timeout():
    p = Playback(discard=True)
    p.push(bytes(32000))
    assert not p.busy()
    assert p.received == 16000 and p.played == 0


async def test_sender_transport_overhead_does_not_accumulate(monkeypatch):
    import robot_voice.audio.buffers as buffers

    clock = [100.0]
    starts = []

    async def sleep(delay):
        clock[0] += delay

    async def send(pcm):
        starts.append(clock[0])
        clock[0] += 0.006  # realistic websocket serialization overhead

    monkeypatch.setattr(buffers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    sender = PacedSender(send)
    for n in range(5000):
        await sender.append(Frame(n * 512, bytes(1024), clock[0]))
    assert abs(starts[-1] - starts[0] - 4999 * 0.032) < 1e-7


async def test_sender_rebases_after_stall_instead_of_burst(monkeypatch):
    import robot_voice.audio.buffers as buffers

    clock = [100.0]
    starts = []

    async def sleep(delay):
        clock[0] += delay

    async def send(pcm):
        starts.append(clock[0])
        clock[0] += 0.2 if len(starts) == 1 else 0.001

    monkeypatch.setattr(buffers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    sender = PacedSender(send)
    for n in range(3):
        await sender.append(Frame(n * 512, bytes(1024), clock[0]))
    assert starts[1] - starts[0] >= 0.232 - 1e-8
    assert starts[2] - starts[1] >= 0.032 - 1e-8


def test_segmenter_sample_gap_discards_prefix_through_clean_silence():
    s = Segmenter()
    s.feed(frame(0, loud=True), True)
    with pytest.raises(AudioOverflow, match="gap"):
        s.feed(frame(1024, loud=True), True)
    assert not s.active and s.rejected
    for n in range(3, 22):
        assert s.feed(frame(n * 512), False) is None
    assert not s.rejected
    assert s.feed(frame(22 * 512, loud=True), True) is None
    result = None
    for n in range(23, 42):
        result = s.feed(frame(n * 512), False)
    assert result.frames[0].start == 22 * 512


def test_keyword_reset_discards_old_sample_tail():
    from robot_voice.wake.keyword import KeywordWake

    class Model:
        resets = 0

        def reset(self):
            self.resets += 1

    detector = KeywordWake.__new__(KeywordWake)
    detector.pending = bytearray(b"old partial audio")
    detector.model = Model()
    detector.reset()
    assert not detector.pending
    assert detector.model.resets == 1
