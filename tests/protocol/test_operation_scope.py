"""Real lifecycle/tool boundaries with only the network transport replaced."""

import asyncio
import json
import time

import pytest

from robot_voice.audio.buffers import Frame
from robot_voice.supervisor import Supervisor
from tests.protocol.test_openai_live import backend, voice


def supervisor(config, store, rig):
    value = voice(config, store, rig)
    result = Supervisor(config, None, store, value.budget, rig[2], value.playback)
    result.voice = value
    result.last_session = rig[0]
    result.state = "ACTIVE"
    return result, value


def feed(supervisor, speech, count=1):
    for _ in range(count):
        supervisor.feed(
            Frame(supervisor.last_capture_sample, b"\0" * 1024, time.monotonic()), speech
        )


async def created(value, rid, delegation):
    await backend(
        value, {"type": "response.created", "response": {"id": rid}}, delegation=delegation
    )


async def action(value, runner, rid, call_id, delegation):
    await backend(
        value,
        {
            "type": "response.output_item.done",
            "response_id": rid,
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": "run_policy",
                "arguments": json.dumps(
                    {"policy": "walk", "parameters": {"duration_seconds": 0.1, "speed": 0.5}}
                ),
            },
        },
        delegation=delegation,
    )
    await backend(
        value,
        {"type": "response.completed", "response": {"id": rid, "output": []}},
        delegation=delegation,
    )
    await asyncio.gather(*value.workers)
    await asyncio.gather(*runner.running.values())


@pytest.mark.parametrize("silent_frames", [0, 1, 18])
@pytest.mark.parametrize("new_delegation", [False, True])
async def test_brief_vad_pause_cannot_repeat_completed_action(
    config, store, rig, silent_frames, new_delegation
):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    original_scope = v.scope
    await created(v, "first", "d1")
    await action(v, rig[1], "first", "call1", "d1")
    feed(s, False, silent_frames)
    feed(s, True)
    delegation = "d2" if new_delegation else "d1"
    await created(v, "retry", delegation)
    await action(v, rig[1], "retry", "call2", delegation)
    assert v.scope == original_scope
    assert len(store.rows("SELECT id FROM tasks")) == 1
    assert len(store.rows("SELECT DISTINCT operation_id FROM tool_calls")) == 1
    await rig[1].shutdown()


@pytest.mark.parametrize("origin", ["delegation", "response", "no_delegation"])
async def test_delayed_function_keeps_originating_scope(config, store, rig, origin):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    original_scope = v.scope
    delegation = None if origin == "no_delegation" else "old"
    if origin == "delegation":
        await v.handle(
            {
                "type": "session.delegation.created",
                "delegation": {"id": delegation, "target": "responses"},
            }
        )
    else:
        await created(v, "delayed", delegation)
    feed(s, False, 19)  # 608ms: a genuinely complete captured utterance boundary.
    feed(s, True)
    assert v.scope != original_scope
    if origin == "delegation":
        await created(v, "delayed", delegation)
    await action(v, rig[1], "delayed", "old-call", delegation)
    await created(v, "new", "new-delegation")
    await action(v, rig[1], "new", "new-call", "new-delegation")
    assert len(store.rows("SELECT id FROM tasks")) == 2
    assert len(store.rows("SELECT DISTINCT operation_id FROM tool_calls")) == 2
    await rig[1].shutdown()


async def test_new_utterance_can_repeat_but_old_delegation_cannot(config, store, rig):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    await created(v, "first", "old")
    await action(v, rig[1], "first", "call1", "old")
    feed(s, False, 19)
    feed(s, True)
    # A late continuation belongs to its original utterance, not newer speech.
    await created(v, "retry", "old")
    await action(v, rig[1], "retry", "call2", "old")
    assert len(store.rows("SELECT id FROM tasks")) == 1
    await created(v, "repeat", "new")
    await action(v, rig[1], "repeat", "call3", "new")
    assert len(store.rows("SELECT id FROM tasks")) == 2
    await rig[1].shutdown()


def test_uncaptured_sample_gap_does_not_authorize_new_scope(config, store, rig):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    original_scope = v.scope
    feed(s, False, 18)
    # Missing audio is not proof of silence. Only this one valid silent frame counts.
    s.feed(Frame(s.last_capture_sample + 16000, b"\0" * 1024, time.monotonic()), False)
    feed(s, True)
    assert v.scope == original_scope


@pytest.mark.parametrize("late_origin", ["function_item", "delegation_event"])
async def test_late_delegation_correlation_inherits_known_response_scope(
    config, store, rig, late_origin
):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    await created(v, "first", None)
    feed(s, False, 19)
    feed(s, True)
    if late_origin == "delegation_event":
        await v.handle(
            {
                "type": "session.delegation.created",
                "delegation": {"id": "old", "target": "responses", "response_id": "first"},
            }
        )
    await action(v, rig[1], "first", "call1", "old")
    await created(v, "continuation", "old")
    await action(v, rig[1], "continuation", "call2", "old")
    assert len(store.rows("SELECT id FROM tasks")) == 1
    assert len(store.rows("SELECT DISTINCT operation_id FROM tool_calls")) == 1
    await rig[1].shutdown()


async def test_conflicting_established_scopes_fail_closed(config, store, rig):
    s, v = supervisor(config, store, rig)
    feed(s, True)
    await created(v, "first", None)
    feed(s, False, 19)
    feed(s, True)
    await v.handle(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "uncorrelated", "target": "responses"},
        }
    )
    # No correlation identifies this late event as old work. Do not guess when
    # the subsequent item connects two already-established, different scopes.
    with pytest.raises(RuntimeError, match="Conflicting backend request identities"):
        await action(v, rig[1], "first", "call1", "uncorrelated")
    assert not store.rows("SELECT id FROM tasks")
    await rig[1].shutdown()
