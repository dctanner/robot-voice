import asyncio
import json
import time
from uuid import uuid4

import pytest

from robot_voice.audio.playback import Playback
from robot_voice.budget import Budget
from robot_voice.voice.openai_live import LiveVoice


class Connection:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(event)


def voice(config, store, rig):
    session, runner, gateway = rig
    value = LiveVoice(
        None, config, store, Budget(), gateway, Playback(discard=True), session, {}, "opening"
    )
    value.connection = Connection()
    return value


async def backend(value, nested, delegation="delegation-one"):
    await value.handle(
        {
            "type": "response.event",
            "event_id": uuid4().hex,
            "delegation_id": delegation,
            "event": nested,
        }
    )


async def test_nested_function_mapping_results_then_one_continuation(config, store, rig):
    v = voice(config, store, rig)
    await backend(v, {"type": "response.created", "response": {"id": "resp1"}})
    # Actual API granular events have no response_id; lifecycle output arrays are empty.
    await backend(
        v,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "a",
                "name": "pick_up",
                "arguments": '{"object_description":"red sock on the floor"}',
            },
        },
    )
    await backend(
        v,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "b",
                "name": "get_robot_state",
                "arguments": "{}",
            },
        },
    )
    await backend(
        v,
        {
            "type": "response.completed",
            "response": {
                "id": "resp1",
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    )
    await asyncio.gather(*v.workers)
    assert [e["type"] for e in v.connection.sent] == [
        "response.item.create",
        "response.item.create",
        "response.create",
    ]
    assert v.connection.sent[0]["item"]["call_id"] == "a"
    assert json.loads(v.connection.sent[0]["item"]["output"])["status"] == "accepted"
    assert rig[1].running  # tool receiver did NOT wait for motion completion
    await backend(v, {"type": "response.created", "response": {"id": "resp2"}})
    await backend(v, {"type": "response.completed", "response": {"id": "resp2", "output": []}})
    assert v.pending_since is None
    await rig[1].shutdown()


async def test_function_lifecycle_output_is_never_dispatched(config, store, rig):
    v = voice(config, store, rig)
    await backend(
        v,
        {
            "type": "response.completed",
            "response": {
                "id": "resp",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "a",
                        "name": "navigate",
                        "arguments": '{"destination":"bedroom"}',
                    }
                ],
            },
        },
    )
    assert not rig[1].running and not v.connection.sent


async def test_malformed_json_is_a_tool_result_not_receiver_crash(config, store, rig):
    v = voice(config, store, rig)
    await backend(v, {"type": "response.created", "response": {"id": "r"}})
    await backend(
        v,
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "a", "name": "navigate", "arguments": "{"},
        },
    )
    await backend(v, {"type": "response.completed", "response": {"id": "r"}})
    await asyncio.gather(*v.workers)
    assert "error" in json.loads(v.connection.sent[0]["item"]["output"])
    assert not rig[1].running


async def test_transcript_deltas_no_turn_done_and_event_dedupe(config, store, rig):
    v = voice(config, store, rig)
    event = {
        "type": "session.input_transcript.delta",
        "event_id": "input1",
        "delta": " hi",
        "start_ms": 5,
        "end_ms": 30,
    }
    await v.handle(event)
    await v.handle(event)
    await v.handle(
        {
            **event,
            "event_id": "output1",
            "type": "session.output_transcript.delta",
            "delta": " hello",
        }
    )
    assert len(store.rows("SELECT * FROM transcript_fragments")) == 2
    assert v.last_input > 0


async def test_usage_cumulative_final_and_unconfirmed_close(config, store, rig):
    v = voice(config, store, rig)
    for n in [10, 20, 30]:
        await v.handle(
            {"type": "session.usage.updated", "event_id": str(n), "usage": {"seconds": n}}
        )
    await v.handle({"type": "session.closed", "event_id": "closed", "usage": {"seconds": 35}})
    assert v.finalized and v.budget.active_seconds == 35
    assert len(store.rows("SELECT * FROM usage")) == 1
    other = voice(config.model_copy(update={"close_timeout": 0.01}), store, rig)
    other.started_at = time.monotonic()
    assert not await other.close()
    assert other.connection.sent[-1]["type"] == "session.close"
    assert other.budget.usage[rig[0]]["final"] is False


async def test_completion_is_session_wide_not_second_tool_output(config, store, rig):
    v = voice(config, store, rig)
    job = {"id": "task", "state": "succeeded", "version": 3, "result": {"verified": True}}
    assert await v.announce(job)
    event = v.connection.sent[-1]
    assert event["type"] == "session.commentary.append" and event["delegation_id"] is None
    v.closing = True
    assert not await v.announce(job)


async def test_duplicate_call_after_new_provider_response_does_not_execute_twice(
    config, store, rig
):
    v = voice(config, store, rig)
    for n in range(2):
        await backend(v, {"type": "response.created", "response": {"id": f"r{n}"}})
        await backend(
            v,
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "duplicate",
                    "name": "navigate",
                    "arguments": '{"destination":"bedroom"}',
                },
            },
        )
        await backend(v, {"type": "response.completed", "response": {"id": f"r{n}"}})
        await asyncio.gather(*v.workers)
    assert len(store.rows("SELECT * FROM tasks")) == 1
    await rig[1].shutdown()


async def function_response(
    v,
    rid="r",
    terminal="response.completed",
    arguments='{"destination":"bedroom"}',
    name="navigate",
):
    await backend(v, {"type": "response.created", "response": {"id": rid}})
    await backend(
        v,
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": rid + "-call",
                "name": name,
                "arguments": arguments,
            },
        },
    )
    await backend(v, {"type": terminal, "response": {"id": rid}})


@pytest.mark.parametrize(
    "terminal", ["response.cancelled", "response.failed", "response.incomplete"]
)
async def test_unsuccessful_responses_cannot_dispatch_completed_function_items(
    config, store, rig, terminal
):
    v = voice(config, store, rig)
    try:
        await function_response(v, terminal=terminal)
        await asyncio.gather(*v.workers)
        assert not store.rows("SELECT * FROM tasks")
        assert not store.rows("SELECT * FROM tool_calls")
        assert not v.connection.sent
    finally:
        await rig[1].shutdown()


@pytest.mark.parametrize("terminal", ["response.completed", "response.cancelled"])
async def test_terminal_drain_keeps_usage_and_text_but_admits_no_actions(
    config, store, rig, terminal
):
    v = voice(config, store, rig)
    closing = asyncio.create_task(v.close())
    await asyncio.sleep(0)
    assert v.closing
    try:
        await function_response(v, terminal=terminal)
        await asyncio.gather(*v.workers)
        await v.handle(
            {
                "type": "session.output_transcript.delta",
                "event_id": "last-text",
                "delta": " Goodbye",
                "start_ms": 100,
                "end_ms": 200,
            }
        )
        await v.handle(
            {"type": "session.closed", "event_id": "last-usage", "usage": {"seconds": 4}}
        )
        assert await closing
        assert not store.rows("SELECT * FROM tasks")
        assert not store.rows("SELECT * FROM tool_calls")
        assert store.rows("SELECT text FROM transcript_fragments")[0]["text"] == " Goodbye"
        assert v.budget.active_seconds == 4
    finally:
        v.closed.set()
        await asyncio.gather(closing, return_exceptions=True)
        await rig[1].shutdown()


async def test_queued_tool_worker_cannot_start_after_close_commit(config, store, rig):
    v = voice(config, store, rig)
    await rig[2].lock.acquire()
    closing = None
    try:
        await function_response(v)
        await asyncio.sleep(0)  # worker waits for the gateway lock
        closing = asyncio.create_task(v.close())
        await asyncio.sleep(0)
        rig[2].lock.release()
        await asyncio.sleep(0)
        await v.handle({"type": "session.closed", "event_id": "done", "usage": {"seconds": 2}})
        await closing
        assert not store.rows("SELECT * FROM tasks")
    finally:
        if rig[2].lock.locked():
            rig[2].lock.release()
        v.closed.set()
        if closing:
            await asyncio.gather(closing, return_exceptions=True)
        await rig[1].shutdown()


async def test_close_preserves_job_already_accepted_before_boundary(config, store, rig):
    v = voice(config, store, rig)
    rig[1].robot.duration = 0.2
    await function_response(v)
    await asyncio.gather(*v.workers)
    task_id = store.rows("SELECT id FROM tasks")[0]["id"]
    closing = asyncio.create_task(v.close())
    await asyncio.sleep(0)
    await v.handle({"type": "session.closed", "event_id": "closed", "usage": {"seconds": 1}})
    await closing
    assert task_id in rig[1].running
    await asyncio.gather(*rig[1].running.values())
    assert rig[1].get(task_id)["state"] == "succeeded"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_wire_nonfinite_json_never_reaches_robot(config, store, rig, constant):
    v = voice(config, store, rig)
    try:
        await function_response(
            v,
            name="run_policy",
            arguments='{"policy":"walk","parameters":{"duration_seconds":0.1,"speed":'
            + constant
            + "}}",
        )
        await asyncio.gather(*v.workers)
        result = json.loads(v.connection.sent[0]["item"]["output"])
        assert "error" in result
        assert not store.rows("SELECT * FROM tasks")
        assert not any(
            token in store.rows("SELECT arguments FROM tool_calls")[0]["arguments"]
            for token in ("NaN", "Infinity")
        )
    finally:
        await rig[1].shutdown()


async def test_close_at_admitted_cancellation_yield_persists_acknowledged_result(
    config, store, rig, monkeypatch
):
    session, runner, gateway = rig
    runner.robot.duration = 0.25
    job = runner.start(session, "navigate", {"destination": "bedroom"})
    await asyncio.sleep(0)
    v = voice(config, store, rig)
    boundary = asyncio.Event()
    original_transition = runner.transition

    def close_at_cancelling(identity, state, result=None):
        original_transition(identity, state, result)
        if state == "cancelling":
            v.begin_close()  # cancel the voice worker before TaskRunner.cancel's yield
            boundary.set()

    monkeypatch.setattr(runner, "transition", close_at_cancelling)
    try:
        async with asyncio.timeout(2):
            await function_response(
                v,
                rid="cancel",
                name="cancel_task",
                arguments=json.dumps({"task_id": job["task_id"]}),
            )
            await boundary.wait()
            await v.handle(
                {"type": "session.closed", "event_id": "cancel-close", "usage": {"seconds": 1}}
            )
            await v.close()  # may cancel the same worker again while settling
            row = runner.get(job["task_id"])
            assert row["state"] == "cancelled"
            assert row["result"]["acknowledged"] is True
            assert job["task_id"] not in runner.running
            call = store.rows(
                "SELECT result,operation_id FROM tool_calls WHERE call_id='cancel-call'"
            )[0]
            result = json.loads(call["result"])
            assert result["state"] == "cancelled"
            operation = store.rows(
                "SELECT result FROM operations WHERE id=?", (call["operation_id"],)
            )[0]
            assert json.loads(operation["result"]) == result
            assert (
                store.rows("SELECT COUNT(*) AS n FROM events WHERE kind='tool.result'")[0]["n"] == 1
            )
            assert not any(e["type"] == "response.create" for e in v.connection.sent)
    finally:
        v.closed.set()
        await runner.shutdown()
