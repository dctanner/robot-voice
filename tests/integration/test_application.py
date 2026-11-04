"""Default capture routing exercised without PortAudio, credentials or network."""

import asyncio
import time

import numpy as np
import pytest
from httpx2 import Request
from openai import APIConnectionError

from robot_voice.audio.buffers import Frame
from robot_voice.wake.vad_transcribe import FixtureSpeech


async def install_runtime(monkeypatch, config, answers, blocked_first=False, close_delay=0.1):
    import robot_voice.application as app
    from robot_voice.supervisor import Supervisor
    from tests.integration.test_supervisor import FakeVoice

    frames = asyncio.Queue()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    holder = {}

    class Frontend:
        def __init__(self, *args):
            holder["frontend"] = self
            self.closed = False

        async def read(self):
            return await frames.get()

        def close(self):
            self.closed = True

    class Client:
        async def close(self):
            holder["client_closed"] = True

    class Transcriber:
        def __init__(self, *args):
            pass

        async def transcribe(self, segment):
            calls.append(segment.cutoff)
            if blocked_first and len(calls) == 1:
                entered.set()
                await release.wait()
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

    class Worker:
        def __init__(self, *args):
            pass

        async def run(self):
            await asyncio.Event().wait()

    class SlowCloseVoice(FakeVoice):
        async def close(self):
            await asyncio.sleep(close_delay)
            return await super().close()

    def make_supervisor(*args):
        value = Supervisor(*args, voice_factory=SlowCloseVoice)
        value.status = lambda *args: None
        holder["supervisor"] = value
        return value

    monkeypatch.setattr(app, "Microphone", Frontend)
    monkeypatch.setattr(app, "SileroSpeech", FixtureSpeech)
    monkeypatch.setattr(app, "cloud_client", Client)
    monkeypatch.setattr(app, "Transcriber", Transcriber)
    monkeypatch.setattr(app, "SummaryWorker", Worker)
    monkeypatch.setattr(app, "Supervisor", make_supervisor)
    task = asyncio.create_task(
        app.run(
            config.model_copy(
                update={
                    "idle_timeout": 0.06,
                    "reply_grace": 0,
                    "quiet_guard": 0.01,
                    "wake_chime": False,
                }
            )
        )
    )
    index = 0

    async def feed(speech_frames=3, silence_frames=19):
        nonlocal index
        for n in range(speech_frames + silence_frames):
            pcm = (np.ones(512, dtype="<i2") * (1000 if n < speech_frames else 0)).tobytes()
            await frames.put(Frame(index, pcm, time.monotonic()))
            index += 512
            await asyncio.sleep(0.001)

    return task, holder, feed, entered, release, calls


async def test_rejected_wake_does_not_drop_next_speech_during_transcription(monkeypatch, config):
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, ["unrelated nearby speech", "Hey Spot, pick up the red sock."], True
    )
    try:
        async with asyncio.timeout(2):
            await feed()
            await entered.wait()
            await feed()
            release.set()
            while not holder["supervisor"].sessions:
                await feed(0, 1)
            assert holder["supervisor"].voice.initial == "Hey Spot, pick up the red sock."
            assert len(calls) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert holder["frontend"].closed and holder["client_closed"]


async def test_speech_after_close_commit_is_seeded_into_new_session(monkeypatch, config):
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, ["Hey Spot, hello.", "And what is the robot doing now?"]
    )
    try:
        async with asyncio.timeout(2):
            await feed()
            while holder["supervisor"].state != "CLOSING":
                await feed(0, 1)
            await feed()
            while holder["supervisor"].sessions < 2:
                await feed(0, 1)
            assert holder["supervisor"].voice.initial == "And what is the robot doing now?"
            assert len(calls) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rejected_transcription_overflow_discards_entire_affected_utterance(
    monkeypatch, config, capsys
):
    config = config.model_copy(update={"startup_seconds": 0.128})
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, ["unrelated nearby speech", "Hey Spot, fresh complete request."], True
    )
    try:
        async with asyncio.timeout(3):
            await feed()
            await entered.wait()
            # Retain a possible wake prefix, then overflow while its qualification is spoken.
            await feed(12, 0)
            release.set()
            await feed(20, 19)  # discard through a clean boundary, not a truncated replay
            assert len(calls) == 1
            assert holder["supervisor"].sessions == 0
            assert "overflow" in capsys.readouterr().out.lower()
            await feed()
            while not holder["supervisor"].sessions:
                await feed(0, 1)
            assert len(calls) == 2
            assert holder["supervisor"].voice.initial == "Hey Spot, fresh complete request."
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("request timed out"),
        ConnectionError("network unavailable"),
        APIConnectionError(
            message="private response body", request=Request("POST", "https://example.invalid")
        ),
    ],
)
async def test_transcription_failure_keeps_standby_and_independent_job(
    monkeypatch, config, error, capsys
):
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, [error, "Hey Spot, try a fresh request."], True
    )
    await asyncio.sleep(0)
    runner = holder["supervisor"].gateway.runner
    runner.robot.duration = 10
    origin = runner.store.session(config)
    job = runner.start(origin, "navigate", {"destination": "bedroom"})
    try:
        async with asyncio.timeout(3):
            await feed()
            await entered.wait()
            release.set()
            await feed(0, 25)
            assert not task.done()
            assert job["task_id"] in runner.running
            assert runner.get(job["task_id"])["state"] == "running"
            assert holder["supervisor"].state == "STANDBY"
            assert len(calls) == 1  # no automatic paid retry
            output = capsys.readouterr().out.lower()
            assert "transcription failed" in output
            assert "private response body" not in output
            await feed()
            while not holder["supervisor"].sessions:
                await feed(0, 1)
            assert len(calls) == 2
            assert job["task_id"] in runner.running
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("closure", ["duration", "error"])
@pytest.mark.parametrize("short_vad_pause", [False, True])
@pytest.mark.parametrize("miss_closing_state", [False, True])
async def test_forced_close_discards_interrupted_utterance_suffix(
    monkeypatch, config, capsys, closure, short_vad_pause, miss_closing_state
):
    answers = ["Hey Spot, hello.", "pick up the red sock."]
    if miss_closing_state:
        answers[1] = (
            "Hey Spot, pick up the red sock."  # even a wake phrase in a suffix is incomplete
        )
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, answers, close_delay=0 if miss_closing_state else 0.1
    )
    try:
        async with asyncio.timeout(3):
            await feed()
            s = holder["supervisor"]
            while s.state != "ACTIVE":
                await feed(0, 1)
            first_session = s.last_session
            close_committed = asyncio.Event()
            original_begin_close = s.voice.begin_close

            def signal_close():
                original_begin_close()
                close_committed.set()

            monkeypatch.setattr(s.voice, "begin_close", signal_close)
            await feed(3, 0)  # active prefix, e.g. "Do not ..."
            if short_vad_pause:
                await feed(0, 1)  # a gap between words, not an utterance boundary
            if closure == "duration":
                s.config.max_session_seconds = 0
                expected_reason = "session_duration_cap"
            else:
                s.voice.error = "injected_transport_failure"
                expected_reason = "injected_transport_failure"
            await close_committed.wait()
            if miss_closing_state:
                await asyncio.sleep(0.01)
                assert s.state == "STANDBY"  # capture did not observe transient CLOSING
            await feed(12, 19)  # the unqualified suffix straddles the forced close
            await feed(0, 100)  # let close finish and any incorrect handoff execute
            assert s.sessions == 1
            assert len(calls) == 1  # suffix was not even sent for transcription
            assert (
                s.store.rows("SELECT reason FROM sessions WHERE id=?", (first_session,))[0][
                    "reason"
                ]
                == expected_reason
            )
            assert "repeat" in capsys.readouterr().out.lower()
            # A genuinely fresh, explicit wake after clean silence still works.
            answers[1] = "Hey Spot, this is a complete fresh request."
            s.config.max_session_seconds = 2
            await feed()
            while s.sessions < 2:
                await feed(0, 1)
            assert len(calls) == 2
            assert s.voice.initial == answers[1]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_idle_close_without_captured_utterance_boundary_discards_suffix(monkeypatch, config):
    task, holder, feed, entered, release, calls = await install_runtime(
        monkeypatch, config, ["Hey Spot, hello.", "pick up the red sock."]
    )
    try:
        async with asyncio.timeout(2):
            await feed()
            s = holder["supervisor"]
            while s.state != "ACTIVE":
                await feed(0, 1)
            close_committed = asyncio.Event()
            original_begin_close = s.voice.begin_close

            def signal_close():
                original_begin_close()
                close_committed.set()

            monkeypatch.setattr(s.voice, "begin_close", signal_close)
            await feed(3, 1)
            # Capture stalls after only one silent frame. Wall-time idle eligibility
            # is not proof of a complete captured utterance, even with VAD false.
            await close_committed.wait()
            assert s.close_reason == "idle_timeout"
            await feed(12, 100)
            assert s.sessions == 1
            assert len(calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
