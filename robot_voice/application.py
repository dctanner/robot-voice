import asyncio
import contextlib
import os
import sys
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openai import APIError

from robot_voice.audio.buffers import AudioOverflow, Frame, SampleBuffer
from robot_voice.audio.frontend import Microphone
from robot_voice.audio.playback import Playback
from robot_voice.budget import Budget
from robot_voice.config import cloud_client
from robot_voice.memory.store import Store
from robot_voice.memory.summaries import LunaSummarizer, SummaryWorker
from robot_voice.robot.mock import MockRobot
from robot_voice.robot.tasks import TaskRunner
from robot_voice.supervisor import Supervisor
from robot_voice.tools.gateway import Gateway
from robot_voice.wake.keyword import KeywordWake
from robot_voice.wake.vad_transcribe import ExplicitWake, Segmenter, SileroSpeech, Transcriber


class RuntimeLock:
    """Prevent two runners or destructive CLI maintenance on the same database."""

    def __init__(self, database):
        self.path = Path(str(database) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                windows: Any = msvcrt
                self.file.seek(0)
                self.file.write("0")
                self.file.flush()
                self.file.seek(0)
                windows.locking(self.file.fileno(), windows.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Database in use by another robot-voice process") from exc
        return self

    def __exit__(self, *args):
        assert self.file is not None
        self.file.close()


async def run(config):
    print(
        "SIMULATED robot only. Standby VAD speech is uploaded for wake transcription; "
        "non-wake text is discarded. Active voice sends continuous audio. Ctrl+C exits."
    )
    print(
        f"Audio mode: {config.audio_mode}. External AEC must be configured/tested by you; "
        "half-duplex loses barge-in. No automatic echo cancellation."
    )
    # Diagnose hardware/VAD before creating any paid connection.
    playback = Playback()
    detector = SileroSpeech()
    frontend = Microphone(config, playback)
    client = cloud_client()
    store = Store(config.database)
    store.recover()
    budget = Budget(config.budget_dollars, config.max_active_seconds, config.max_cloud_requests)
    runner = TaskRunner(store, MockRobot(config.mock_task_seconds))
    gateway = Gateway(store, runner)
    supervisor = Supervisor(config, client, store, budget, gateway, playback)
    transcriber = Transcriber(client, config, budget, store)
    wake = ExplicitWake(config.wake_phrase)
    segmenter = Segmenter(config.max_utterance_seconds)
    keyword = KeywordWake(config.keyword_model) if config.wake_mode == "keyword" else None
    ring = SampleBuffer(3, rolling=True)
    startup = SampleBuffer(config.startup_seconds)
    checking = False
    check_failed = False
    work = None
    pending_wake = None
    closing_speech = False
    discarding_capture = False
    capture_discard_generation = supervisor.capture_discard_generation
    replay: deque[tuple[Frame, bool]] = deque()
    check_frames: list[tuple[Frame, bool]] = []
    summary = SummaryWorker(store, LunaSummarizer(client, config, budget, store), config.timezone)
    summaries = asyncio.create_task(summary.run())
    keyboard = asyncio.Event()
    loop = asyncio.get_running_loop()
    keyboard_installed = False
    if config.wake_mode == "keyboard":
        if os.name == "nt":
            print("Keyboard wake: press Enter (Windows polling).")
        else:

            def keyboard_ready():
                sys.stdin.readline()
                keyboard.set()

            loop.add_reader(sys.stdin.fileno(), keyboard_ready)
            keyboard_installed = True
        print("Press Enter to wake, then speak. Ctrl+C exits.")

    def discard_wake_capture():
        nonlocal pending_wake, closing_speech, discarding_capture, ring
        # Once capture is lost, neither a retained prefix nor the remaining suffix
        # is a complete request. Require a fresh speech boundary before listening.
        replay.clear()
        pending_wake = None
        closing_speech = False
        discarding_capture = True
        ring = SampleBuffer(3, rolling=True)
        keyboard.clear()
        if keyword:
            keyword.reset()
        segmenter.reset(discard_until_silence=True)

    async def evaluate(segment, continuation=False):
        nonlocal checking, startup, check_failed, check_frames
        accepted = False
        backlog = []
        try:
            try:
                async with asyncio.timeout(config.backend_timeout):
                    text = await transcriber.transcribe(segment)
            except (TimeoutError, ConnectionError, OSError, APIError) as exc:
                discard_wake_capture()
                # No server exception body, credential, transcript, or paid retry.
                store.event(None, "wake.transcription_failed", {"error_type": type(exc).__name__})
                print(
                    "Wake transcription failed ("
                    + type(exc).__name__
                    + "); standby resumed. Please repeat after a pause. No automatic retry."
                )
                return
            if check_failed:
                discard_wake_capture()
                raise AudioOverflow(
                    "Wake transcription/startup overflow; repeat the whole request after a pause"
                )
            accepted = bool(wake.accept(text) or (continuation and text.strip()))
            if accepted:
                backlog = startup.drain_after(segment.cutoff)
                # Accepted audio goes to Live, not back into the standby segmenter.
                segmenter.reset()
            else:
                store.event(None, "wake.rejected", {"duration_seconds": len(segment.pcm) / 32000})
                replay.extend(check_frames)
        finally:
            checking = False
            startup = SampleBuffer(config.startup_seconds)
            check_frames = []
        if accepted:
            initial_at = (
                datetime.now(UTC)
                - timedelta(seconds=max(0, time.monotonic() - segment.frames[0].captured))
            ).isoformat()
            await supervisor.wake(text, backlog, initial_at=initial_at, cutoff=segment.cutoff)

    try:
        while True:
            budget.check()
            if work and work.done():
                try:
                    work.result()
                except AudioOverflow as exc:
                    print(str(exc))
                work = None
            if pending_wake is not None and work is None:
                segment, continuation = pending_wake
                pending_wake = None
                work = asyncio.create_task(evaluate(segment, continuation))
            if replay:
                frame, speech = replay.popleft()
            else:
                frame = await frontend.read()
                speech = await asyncio.to_thread(detector.speech, frame)
                ring.append(frame)
                # Transcription may have completed while read/inference awaited.
                # Deliver its replay prefix before this newly captured frame.
                if replay:
                    replay.append((frame, speech))
                    frame, speech = replay.popleft()
            if capture_discard_generation != supervisor.capture_discard_generation:
                capture_discard_generation = supervisor.capture_discard_generation
                discard_wake_capture()
                checking = False
                check_failed = False
                startup = SampleBuffer(config.startup_seconds)
                check_frames = []
                store.event(
                    supervisor.last_session,
                    "wake.capture_discarded",
                    {"close_reason": supervisor.close_reason, "until_clean_silence": True},
                )
                print(
                    "Voice closed before a safe capture handoff; remaining speech discarded. "
                    "Pause, then repeat with a fresh wake."
                )
            if supervisor.feed(frame, speech):
                continue
            if discarding_capture:
                segmenter.feed(frame, speech)
                keyboard.clear()
                if not segmenter.rejected:
                    discarding_capture = False
                    ring = SampleBuffer(3, rolling=True)
                continue
            if checking:
                try:
                    startup.append(frame)
                    check_frames.append((frame, speech))
                except AudioOverflow:
                    check_failed = True
                continue
            if supervisor.state == "CLOSING" and supervisor.close_allows_continuation and speech:
                closing_speech = True
            if config.wake_mode == "keyboard":
                if os.name == "nt":
                    import msvcrt

                    windows: Any = msvcrt
                    if windows.kbhit() and windows.getwch() == "\r":
                        keyboard.set()
                if keyboard.is_set() and work is None:
                    keyboard.clear()
                    work = asyncio.create_task(supervisor.wake(None))
                continue
            if keyword:
                if keyword.feed(frame) and work is None:
                    work = asyncio.create_task(supervisor.wake(None, list(ring.frames)))
                continue
            try:
                segment = segmenter.feed(frame, speech)
                if segment:
                    checking = True
                    check_failed = False
                    startup = SampleBuffer(config.startup_seconds)
                    check_frames = []
                    pending_wake = (segment, closing_speech)
                    closing_speech = False
            except AudioOverflow as exc:
                closing_speech = False
                print(str(exc))
    finally:
        if keyboard_installed:
            loop.remove_reader(sys.stdin.fileno())
        if work:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await runner.shutdown()
        summaries.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await summaries
        frontend.close()
        await client.close()
        store.close()
