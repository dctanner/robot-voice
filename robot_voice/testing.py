"""Explicitly synthetic, opt-in paid verification. Never record the room microphone."""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from robot_voice.audio.buffers import Frame
from robot_voice.audio.frontend import Resampler
from robot_voice.audio.playback import Playback
from robot_voice.budget import Budget
from robot_voice.config import cloud_client
from robot_voice.memory.daily_context import daily_context
from robot_voice.memory.store import Store, encode
from robot_voice.memory.summaries import LunaSummarizer, SummaryWorker
from robot_voice.robot.mock import MockRobot
from robot_voice.robot.tasks import TaskRunner
from robot_voice.supervisor import Supervisor
from robot_voice.tools.gateway import Gateway
from robot_voice.wake.vad_transcribe import ExplicitWake, FixtureSpeech, Segment, Transcriber


async def offline_demo(config):
    store = Store(config.database)
    runner = TaskRunner(store, MockRobot(0.04))
    gateway = Gateway(store, runner)
    session = store.session(config)
    first = await gateway.call(
        session, "demo-first", "pick_up", {"object_description": "red sock"}, session
    )
    duplicate = await gateway.call(
        session, "demo-first", "pick_up", {"object_description": "red sock"}, session
    )
    assert first == duplicate
    print("Mock pickup accepted; conversation could continue while the job runs.")
    await asyncio.gather(*runner.running.values())
    task = runner.get(first["task_id"])
    assert task["state"] == "succeeded"
    store.fragment(session, "demo-text-" + session, "user", "The laundry basket is in the bedroom.")
    store.finish(session, "offline_demo", False)
    context = daily_context(store, config.timezone)
    print(
        encode(
            {
                "task_state": task["state"],
                "duplicate_executions": 0,
                "memory_slices": len(context),
                "cloud_requests": 0,
            }
        )
    )
    store.close()


async def synthesize(client, config, budget, text, cache: Path):
    import hashlib

    await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True, mode=0o700)
    path = cache / (hashlib.sha256((config.tts_model + text).encode()).hexdigest() + ".pcm")
    if path.exists():
        return path.read_bytes()
    identity = "tts_" + uuid4().hex
    budget.reserve(identity)
    response = await client.audio.speech.create(
        model=config.tts_model,
        voice="alloy",
        input=text,
        response_format="pcm",
        instructions="Speak clearly in English at a natural pace.",
    )
    pcm24 = response.content
    budget.record(identity, "tts", {"seconds": len(pcm24) / 48000})
    pcm16 = Resampler(24000, 16000).process(pcm24)
    path.write_bytes(pcm16)
    path.chmod(0o600)
    return pcm16


async def live_test(config, output: Path, seconds: float, cycles: int, startup_delay: float):
    if not 5 <= seconds <= 900 or not 1 <= cycles <= 6 or seconds * cycles > 1100:
        raise ValueError("Require 5..900 seconds per cycle, 1..6 cycles, <=1100 requested seconds")
    if (output / "test.sqlite3").exists():
        raise ValueError(
            "Use a new output directory; existing test databases are never overwritten"
        )
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True, mode=0o700)
    config = config.model_copy(
        update={
            "database": output / "test.sqlite3",
            "max_session_seconds": seconds + 12,
            "idle_timeout": 22,
            "reply_grace": 2,
            "task_hold_max_seconds": 25,
            "mock_task_seconds": 10,
        }
    )
    store = Store(config.database)
    store.recover()
    budget = Budget(min(config.budget_dollars, 4.5), min(config.max_active_seconds, 1200), 100)
    client = cloud_client()
    runner = TaskRunner(store, MockRobot(config.mock_task_seconds))
    gateway = Gateway(store, runner)
    playback = Playback(discard=True)
    supervisor = Supervisor(config, client, store, budget, gateway, playback)
    worker = SummaryWorker(store, LunaSummarizer(client, config, budget, store), config.timezone)
    summaries = asyncio.create_task(worker.run())
    transcriber = Transcriber(client, config, budget, store)
    detector = FixtureSpeech()
    started = time.monotonic()
    report: dict[str, Any] = {
        "synthetic_only": True,
        "hardware_tested": False,
        "started_utc": datetime.now(UTC).isoformat(),
        "requested_cycle_seconds": seconds,
        "cycles_requested": cycles,
        "cycles": [],
        "rate_assumptions": {
            "live_per_minute": 0.05,
            "transcribe_per_minute": 0.0045,
            "luna_input_per_million": 0.10,
            "luna_output_per_million": 0.50,
            "tts_per_minute_estimate": 0.02,
        },
        "errors": [],
    }
    openings = [
        "Hey Spot, pick up the red sock on the floor.",
        "Hey Spot, run the walk policy for two hundred and forty seconds at speed zero point five.",
        "Hey Spot, what task is still running? Please check its status and tell me.",
        "Hey Spot, where did I say the laundry basket is? Check today's conversation memory.",
        "Hey Spot, navigate to the kitchen.",
        "Hey Spot, tell me your current robot state.",
    ]
    followups = [
        "The laundry basket is in the bedroom. Please remember that for today.",
        "While the robot works, tell me a short fun fact about socks.",
        "What is the robot doing now? Please check its actual state.",
        "Tell me a short joke about laundry.",
        "What have we talked about today?",
        "What room did I say the laundry basket is in?",
    ]
    try:
        # Finite fixture set, reused from ignored cache; no retry loops.
        clips = {
            text: await synthesize(client, config, budget, text, output / "fixtures")
            for text in openings[:cycles]
            + followups
            + (
                [
                    "Please slow the current walking task to speed zero point two.",
                    "Cancel the walking task, wait for cancellation, then navigate to the kitchen.",
                ]
                if cycles >= 3
                else []
            )
        }
        for cycle in range(cycles):
            if budget.active_seconds + seconds + 12 > budget.max_seconds:
                break
            initial_pcm = clips[openings[cycle]] + bytes(19200)
            frames = [
                Frame(i // 2, initial_pcm[i : i + 1024], time.monotonic())
                for i in range(0, len(initial_pcm), 1024)
            ]
            text = await transcriber.transcribe(Segment(frames))
            if not ExplicitWake(config.wake_phrase).accept(text):
                raise RuntimeError("Synthetic wake transcription did not preserve explicit phrase")
            index = len(initial_pcm) // 2
            active_before = budget.active_seconds
            wall_before = time.monotonic()
            wake_task = asyncio.create_task(supervisor.wake(text, startup_delay=startup_delay))
            await asyncio.sleep(0)
            scheduled = []
            if seconds >= 60:
                scheduled.append((0.5, clips[followups[0]]))
                for n, at in enumerate(range(12, max(13, int(seconds) - 23), 23)):
                    phrase = followups[n % len(followups)]
                    if cycle == 2 and n == 0:
                        phrase = "Please slow the current walking task to speed zero point two."
                    elif cycle == 2 and n == 1:
                        phrase = "Cancel the walking task, wait for cancellation, then navigate to the kitchen."
                    scheduled.append((at, clips[phrase]))
            pending = bytearray()
            clock = time.monotonic()
            tick = clock
            try:
                while not wake_task.done():
                    elapsed = time.monotonic() - clock
                    if scheduled and elapsed >= scheduled[0][0]:
                        pending.extend(scheduled.pop(0)[1])
                    pcm = bytes(pending[:1024])
                    del pending[:1024]
                    pcm += bytes(1024 - len(pcm))
                    frame = Frame(index, pcm, time.monotonic())
                    index = frame.end
                    supervisor.feed(frame, detector.speech(frame))
                    tick += 0.032
                    await asyncio.sleep(max(0, tick - time.monotonic()))
                session = await wake_task
            finally:
                if not wake_task.done():
                    wake_task.cancel()
                    await asyncio.gather(wake_task, return_exceptions=True)
            row = store.rows("SELECT * FROM sessions WHERE id=?", (session,))[0]
            calls = store.rows("SELECT name,result FROM tool_calls WHERE session_id=?", (session,))
            report["cycles"].append(
                {
                    "session": session,
                    "wall_seconds": time.monotonic() - wall_before,
                    "active_seconds": budget.active_seconds - active_before,
                    "close_reason": row["reason"],
                    "finalized": bool(row["finalized"]),
                    "tools": [c["name"] for c in calls],
                    "running_jobs_after_voice_closed": len(runner.running),
                    "memory_slices_at_next_wake": len(daily_context(store, config.timezone)),
                    "startup_delay": startup_delay,
                }
            )
            (output / "report.json").write_text(encode(report))
            await asyncio.sleep(1)
        # Bounded drain: summaries are independent of voice and may fail without losing history.
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if not store.rows("SELECT * FROM summary_jobs WHERE state IN ('pending','running')"):
                break
            await asyncio.sleep(0.25)
        completed = store.rows("SELECT * FROM tasks WHERE name='pick_up' AND state='succeeded'")
        if len(completed) != 1 or not all(c["finalized"] for c in report["cycles"]):
            raise RuntimeError(
                "Live acceptance failed: opening pickup or finalization not confirmed"
            )
        if seconds * cycles >= 600 and budget.active_seconds < 600:
            raise RuntimeError("Long-test acceptance failed: less than 600 active seconds")
        if any(c["close_reason"] != "idle_timeout" for c in report["cycles"]):
            raise RuntimeError("Live acceptance failed: expected ordinary idle closure")
        if store.rows("SELECT * FROM summary_jobs WHERE state!='completed'"):
            raise RuntimeError("Live acceptance failed: summary jobs did not complete")
        if store.rows(
            "SELECT * FROM events WHERE kind IN ('error','voice.error','backend.error','backend.failed')"
        ):
            raise RuntimeError("Live acceptance failed: protocol/backend error recorded")
    except Exception as exc:
        report["errors"].append(type(exc).__name__)
        raise
    finally:
        await runner.shutdown()
        summaries.cancel()
        await asyncio.gather(summaries, return_exceptions=True)
        report.update(
            wall_seconds=time.monotonic() - started,
            active_seconds=budget.active_seconds,
            estimated_dollars=budget.estimate,
            unresolved_reserves_dollars=sum(budget.reservations.values()),
            request_count=budget.requests,
            budget_usage=budget.usage,
            summary_jobs=store.rows(
                "SELECT state,COUNT(*) AS count FROM summary_jobs GROUP BY state"
            ),
            final_usage=store.rows("SELECT model,data,final FROM usage"),
            tasks=store.rows("SELECT name,state,COUNT(*) AS count FROM tasks GROUP BY name,state"),
            protocol_errors=store.rows(
                "SELECT kind,payload FROM events WHERE kind IN ('error','voice.error','backend.error','backend.failed')"
            ),
        )
        (output / "report.json").write_text(encode(report))
        (output / "report.json").chmod(0o600)
        await client.close()
        store.close()
    print(
        encode(
            {
                k: report[k]
                for k in ("active_seconds", "estimated_dollars", "request_count", "errors")
            }
        )
    )
    return report
