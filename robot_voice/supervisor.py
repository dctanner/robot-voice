import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from robot_voice.audio.buffers import AudioOverflow, PacedSender
from robot_voice.budget import BudgetExceeded
from robot_voice.memory.daily_context import daily_context
from robot_voice.voice.openai_live import LiveVoice
from robot_voice.wake.vad_transcribe import UTTERANCE_SILENCE_SECONDS


@dataclass
class IdlePolicy:
    last_user: float
    speaking: bool = False
    generation: int = 0
    assistant_until: float = 0

    def user(self, now, speaking):
        if speaking or self.speaking:
            self.last_user = now
            self.generation += 1
        self.speaking = speaking

    def eligible(self, now, config, playback_busy, pending, tasks_running, sleep=False):
        if self.speaking or playback_busy or pending:
            return False
        if now < self.assistant_until:
            return False
        if sleep:
            return True
        if now - self.last_user < config.idle_timeout:
            return False
        if tasks_running and config.keep_open_while_task_running:
            if now - self.last_user < max(config.idle_timeout, config.task_hold_max_seconds):
                return False
        return True


class Supervisor:
    def __init__(self, config, client, store, budget, gateway, playback, voice_factory=LiveVoice):
        self.config, self.client, self.store, self.budget = config, client, store, budget
        self.gateway, self.playback = gateway, playback
        self.voice_factory = voice_factory
        self.voice = None
        self.state = "STANDBY"
        self.queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, int(config.startup_seconds / 0.032))
        )
        self.policy = IdlePolicy(time.monotonic())
        self.failed = None
        self.completions: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.gateway.runner.on_terminal = self._terminal
        self.sessions = 0
        self.last_session = None
        self.last_speech_sample = 0
        self.last_capture_sample = 0
        self.sent_sample = 0
        self.utterance_started = False
        self.utterance_silence_samples = 0
        self.close_allows_continuation = False
        self.close_reason: str | None = None
        self.capture_discard_generation = 0
        self.status = print
        self.wake_lock = asyncio.Lock()

    def _terminal(self, job):
        if self.state == "ACTIVE":
            try:
                self.completions.put_nowait(job)
            except asyncio.QueueFull:
                # Durable unannounced state remains available on next wake.
                self.store.event(job["session_id"], "announcement.deferred", {"task_id": job["id"]})

    def feed(self, frame, speech):
        if self.state not in ("CONNECTING", "ACTIVE"):
            return False
        self.policy.user(time.monotonic(), speech)
        if frame.start != self.last_capture_sample:
            # Missing capture is not evidence of silence.
            self.utterance_silence_samples = 0
        self.last_capture_sample = frame.end
        if speech:
            self.last_speech_sample = frame.end
            if (
                not self.utterance_started
                or self.utterance_silence_samples >= UTTERANCE_SILENCE_SECONDS * frame.rate
            ) and self.voice:
                assert self.last_session is not None
                self.voice.scope = self.last_session + ":" + uuid4().hex
            self.utterance_started = True
            self.utterance_silence_samples = 0
        else:
            self.utterance_silence_samples += frame.end - frame.start
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.failed = "audio_overflow_repeat_required"
        return True

    async def _sender(self):
        assert self.voice is not None
        sender = PacedSender(self.voice.audio)
        first = True
        while True:
            frame = await self.queue.get()
            if first:
                first = False
                self.store.event(
                    self.last_session,
                    "audio.input_origin",
                    {
                        "first_sample": frame.start,
                        "rate": frame.rate,
                        "capture_utc_approximate": (
                            datetime.now(UTC)
                            - timedelta(seconds=max(0, time.monotonic() - frame.captured))
                        ).isoformat(),
                        "startup_latency_seconds": max(0, time.monotonic() - frame.captured),
                    },
                )
            await sender.append(frame)
            was_pending = self.sent_sample < self.last_speech_sample
            self.sent_sample = frame.end
            if was_pending and self.sent_sample >= self.last_speech_sample:
                self.policy.assistant_until = time.monotonic() + max(
                    self.config.reply_grace, self.config.quiet_guard
                )

    async def wake(self, initial, backlog=(), startup_delay=0, initial_at=None, cutoff=None):
        if self.wake_lock.locked():
            raise RuntimeError("Only one paid voice session is permitted")
        async with self.wake_lock:
            return await self._wake(initial, backlog, startup_delay, initial_at, cutoff)

    async def _wake(self, initial, backlog, startup_delay, initial_at, cutoff):
        self.budget.check()
        if self.budget.max_seconds - self.budget.active_seconds <= self.config.close_timeout * 2:
            raise BudgetExceeded("Insufficient remaining voice time for bounded finalization")
        self.state = "CONNECTING"
        self.status("WAKE DETECTED — CONNECTING (not ready)")
        if self.config.wake_chime:
            self.playback.chime()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.failed = None
        self.policy = IdlePolicy(time.monotonic())
        self.last_capture_sample = self.last_speech_sample = self.sent_sample = 0
        self.utterance_started = False
        self.utterance_silence_samples = 0
        self.gateway.sleep_requested = False
        for frame in backlog:
            # Conservatively drain all startup audio before considering an idle close.
            self.feed(frame, True)
        self.policy.speaking = False
        context = {
            "history": daily_context(self.store, self.config.timezone),
            "fresh_simulated_state": self.gateway.runner.snapshot(),
        }
        session = self.store.session(self.config)
        self.store.event(
            session,
            "memory.loaded",
            {
                "slices": [
                    {"session": entry["session"], "kind": entry["kind"]}
                    for entry in context["history"]
                ]
            },
        )
        self.last_session = session
        self.sessions += 1
        self.voice = self.voice_factory(
            self.client,
            self.config,
            self.store,
            self.budget,
            self.gateway,
            self.playback,
            session,
            context,
            initial,
        )
        self.voice.initial_at = initial_at
        self.store.event(
            session,
            "wake.handoff",
            {
                "cutoff_sample": cutoff,
                "provenance": "transcription_text_once" if initial else "paced_audio",
                "initial_capture_utc_approximate": initial_at,
            },
        )
        reason = "error"
        sender = None
        started = time.monotonic()
        was_busy = False
        playback_start = (self.playback.received, self.playback.played, self.playback.underruns)
        try:
            if startup_delay:
                await asyncio.sleep(startup_delay)
            # Abort BEFORE seeding the command if we already know capture was lost.
            if self.failed:
                raise AudioOverflow(self.failed)
            async with asyncio.timeout(self.config.backend_timeout):
                await self.voice.start()
            self.state = "ACTIVE"
            self.status("ACTIVE — cloud audio on; mute does not stop billing")
            sender = asyncio.create_task(self._sender())
            while True:
                await asyncio.sleep(0.05)
                elapsed = time.monotonic() - started
                self.budget.record(session, "live", {"seconds": elapsed}, False)
                if (
                    self.budget.active_seconds + self.config.close_timeout * 2
                    >= self.budget.max_seconds
                ):
                    reason = "budget_cap"
                    break
                if self.failed or self.voice.error or self.voice.closed.is_set():
                    reason = self.failed or self.voice.error or "remote_closed"
                    break
                if sender.done():
                    sender.result()
                if elapsed >= self.config.max_session_seconds:
                    reason = "session_duration_cap"
                    break
                pending = self.voice.pending_since is not None or bool(self.voice.workers)
                if (
                    pending
                    and self.voice.pending_since
                    and time.monotonic() - self.voice.pending_since > self.config.backend_timeout
                ):
                    reason = "backend_deadline"
                    break
                while not self.completions.empty():
                    job = self.completions.get_nowait()
                    fresh = self.gateway.runner.get(job["id"])
                    if fresh["version"] == job["version"] and await self.voice.announce(fresh):
                        # Means context sent, NOT proof it was spoken/heard.
                        self.store.execute("UPDATE tasks SET announced=1 WHERE id=?", (job["id"],))
                        self.store.event(
                            session, "announcement.context_sent", {"task_id": job["id"]}
                        )
                        self.policy.assistant_until = time.monotonic() + self.config.reply_grace
                busy = self.playback.busy(self.config.quiet_guard)
                if was_busy and not busy:
                    self.policy.assistant_until = time.monotonic() + self.config.reply_grace
                was_busy = busy
                generation = self.policy.generation
                if self.policy.eligible(
                    time.monotonic(),
                    self.config,
                    busy,
                    pending or self.sent_sample < self.last_speech_sample,
                    bool(self.gateway.runner.running),
                    self.gateway.sleep_requested,
                ):
                    await asyncio.sleep(0)  # capture gets one chance before close commit
                    if generation != self.policy.generation:
                        continue
                    reason = "explicit_sleep" if self.gateway.sleep_requested else "idle_timeout"
                    break
        except BudgetExceeded:
            reason = "budget_cap"
            raise
        except Exception as exc:
            reason = type(exc).__name__
            self.status("Session failed: " + reason + "; please repeat if opening speech was lost.")
        finally:
            # Only idle closure after a captured utterance boundary permits handoff.
            # Other closes (or stalled capture) can cut a word during a VAD pause.
            # Keep this generation observable even if capture misses CLOSING entirely.
            self.close_reason = reason
            self.close_allows_continuation = reason == "idle_timeout" and (
                not self.last_speech_sample
                or self.last_capture_sample - self.last_speech_sample
                >= self.config.sample_rate * UTTERANCE_SILENCE_SECONDS
            )
            if not self.close_allows_continuation:
                self.capture_discard_generation += 1
            # Commit close admission boundary synchronously before awaiting provider teardown.
            self.state = "CLOSING"
            self.voice.begin_close()
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            try:
                finalized = await self.voice.close()
            except Exception:
                finalized = self.voice.finalized
            self.store.event(
                session,
                "playback.accounting",
                {
                    "received_samples": self.playback.received - playback_start[0],
                    "submitted_samples": self.playback.played - playback_start[1],
                    "underruns": self.playback.underruns - playback_start[2],
                    "hardware_verified": False,
                    "sink_discarded": self.playback.discard,
                },
            )
            self.store.finish(session, reason, finalized)
            self.voice = None
            self.state = "STANDBY"
            self.status(
                f"STANDBY — {reason}; final usage {'confirmed' if finalized else 'unconfirmed'}"
            )
        return session
