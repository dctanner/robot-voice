import asyncio
import time

import pytest

from robot_voice.audio.buffers import Frame
from robot_voice.audio.playback import Playback
from robot_voice.budget import Budget
from robot_voice.supervisor import Supervisor


class FakeVoice:
    instances = []

    def __init__(self, client, config, store, budget, gateway, playback, session, context, initial):
        self.initial, self.context, self.session = initial, context, session
        self.closed = asyncio.Event()
        self.finalized = False
        self.pending_since = None
        self.workers = set()
        self.error = None
        self.audio_received = []
        self.scope = session
        self.instances.append(self)

    async def start(self):
        await asyncio.sleep(0.01)

    async def audio(self, pcm):
        self.audio_received.append(pcm)

    def begin_close(self):
        self.closing = True

    async def close(self):
        self.begin_close()
        self.finalized = True
        self.closed.set()
        return True

    async def announce(self, job):
        return True


def supervisor(config, store, rig, **updates):
    cfg = config.model_copy(
        update={
            "idle_timeout": 0.08,
            "quiet_guard": 0.01,
            "reply_grace": 0.01,
            "task_hold_max_seconds": 0.1,
            "max_session_seconds": 2,
            **updates,
        }
    )
    s = Supervisor(cfg, None, store, Budget(), rig[2], Playback(discard=True), FakeVoice)
    s.status = lambda *args: None
    return s


@pytest.mark.parametrize("delay", [0.5, 3])
async def test_first_utterance_once_and_speech_during_connection_ordered(config, store, rig, delay):
    s = supervisor(config, store, rig, max_session_seconds=delay + 0.5)
    task = asyncio.create_task(
        s.wake("Hey Spot, pick up the red sock on the floor.", startup_delay=delay)
    )
    await asyncio.sleep(0)
    # Exact cutoff skips opening utterance. Subsequent speech retains sample order.
    for n in range(8):
        s.feed(Frame(32000 + n * 512, bytes([n]) * 1024, time.monotonic()), True)
    s.policy.user(time.monotonic(), False)
    await task
    voice = FakeVoice.instances[-1]
    assert voice.initial == "Hey Spot, pick up the red sock on the floor."
    assert voice.audio_received == [bytes([n]) * 1024 for n in range(8)]
    assert s.state == "STANDBY"


async def test_long_job_survives_idle_hold_and_summary_is_durable(config, store, rig):
    session, runner, gateway = rig
    runner.robot.duration = 0.4
    job = runner.start(session, "navigate", {"destination": "bedroom"})
    s = supervisor(config, store, rig)
    voice_session = await s.wake("hello")
    assert job["task_id"] in runner.running
    assert (
        store.rows("SELECT reason FROM sessions WHERE id=?", (voice_session,))[0]["reason"]
        == "idle_timeout"
    )
    assert (
        store.rows("SELECT state FROM summary_jobs WHERE session_id=?", (voice_session,))[0][
            "state"
        ]
        == "pending"
    )
    await asyncio.gather(*runner.running.values())
    assert runner.get(job["task_id"])["state"] == "succeeded"


async def test_overflow_before_start_never_seeds_partial_request(config, store, rig):
    s = supervisor(config, store, rig, startup_seconds=0.032)
    t = asyncio.create_task(s.wake("opening", startup_delay=0.04))
    await asyncio.sleep(0)
    for n in range(2):
        s.feed(Frame(n * 512, bytes(1024), time.monotonic()), True)
    await t
    assert FakeVoice.instances[-1].audio_received == []
    assert (
        store.rows("SELECT reason FROM sessions ORDER BY start DESC")[0]["reason"]
        == "AudioOverflow"
    )


async def test_one_paid_session_and_post_commit_frames_not_sent(config, store, rig):
    s = supervisor(config, store, rig)
    task = asyncio.create_task(s.wake("first", startup_delay=0.1))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="one paid"):
        await s.wake("second")
    s.state = "CLOSING"
    assert not s.feed(Frame(0, bytes(1024), time.monotonic()), True)
    await task
    assert s.sessions == 1


async def test_backend_deadline_overrides_unbounded_pending(config, store, rig):
    class StuckVoice(FakeVoice):
        async def start(self):
            self.pending_since = time.monotonic()

    s = supervisor(config, store, rig, backend_timeout=0.05)
    s.voice_factory = StuckVoice
    session = await s.wake("hello")
    assert (
        store.rows("SELECT reason FROM sessions WHERE id=?", (session,))[0]["reason"]
        == "backend_deadline"
    )


async def test_prestart_overflow_with_real_live_voice_has_no_billing(config, store, rig):
    from robot_voice.voice.openai_live import LiveVoice

    s = supervisor(config, store, rig, startup_seconds=0.032)
    s.voice_factory = LiveVoice  # client None proves no connection attempt is allowed
    backlog = [Frame(n * 512, bytes(1024), time.monotonic()) for n in range(2)]
    session = await s.wake("Hey Spot, do not execute a partial request", backlog)
    assert (
        store.rows("SELECT reason FROM sessions WHERE id=?", (session,))[0]["reason"]
        == "AudioOverflow"
    )
    assert s.budget.active_seconds == 0
    assert s.budget.estimate == 0
    assert s.budget.requests == 0
    assert not store.rows("SELECT * FROM usage WHERE session_id=?", (session,))
