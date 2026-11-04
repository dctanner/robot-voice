import asyncio
import json

import pytest

from robot_voice.memory.store import encode


async def test_first_command_provider_and_application_deduplication(rig, store):
    session, runner, gateway = rig
    args = {"object_description": "red sock on the floor"}
    a = await gateway.call(session, "call1", "pick_up", args, "utterance1")
    b = await gateway.call(session, "call1", "pick_up", args, "utterance1")
    c = await gateway.call(session, "new-call-id", "pick_up", args, "utterance1")
    assert a == b == c
    assert len(runner.running) == 1
    assert a["status"] == "accepted"
    await asyncio.gather(*runner.running.values())
    assert runner.get(a["task_id"])["state"] == "succeeded"
    assert len(store.rows("SELECT * FROM tasks")) == 1


async def test_resource_conflict_cancel_ack_then_switch(rig):
    session, runner, gateway = rig
    a = await gateway.call(session, "a", "navigate", {"destination": "kitchen"}, "u1")
    b = await gateway.call(session, "b", "navigate", {"destination": "bedroom"}, "u2")
    assert "Resource busy" in b["error"]
    result = await gateway.call(session, "cancel", "cancel_task", {"task_id": a["task_id"]}, "u3")
    assert result["state"] == "cancelled"
    c = await gateway.call(session, "c", "navigate", {"destination": "bedroom"}, "u4")
    assert c["status"] == "accepted"
    await runner.shutdown()


@pytest.mark.parametrize(
    "name,args",
    [
        ("shell", {"command": "anything"}),
        ("navigate", {"destination": "outside"}),
        ("pick_up", {"object_description": "sock"}),
        ("pick_up", {"object_description": "green sock"}),
        ("navigate", {"destination": "kitchen", "ignore_safety": True}),
        ("run_policy", {"policy": "walk", "parameters": {"duration_seconds": 301, "speed": 0.5}}),
        ("run_policy", {"policy": "walk", "parameters": {"duration_seconds": True, "speed": 0.5}}),
        ("get_robot_state", None),
        ("cancel_task", {"task_id": "unknown"}),
    ],
)
async def test_invalid_or_ambiguous_calls_are_recorded_errors(rig, store, name, args):
    session, runner, gateway = rig
    result = await gateway.call(session, "bad", name, args, "u1")
    assert "error" in result
    assert not runner.running
    assert json.loads(store.rows("SELECT result FROM tool_calls")[0]["result"]) == result


async def test_steering_version_and_independent_expression(rig):
    session, runner, gateway = rig
    job = runner.start(
        session,
        "run_policy",
        {"policy": "walk", "parameters": {"duration_seconds": 0.2, "speed": 0.5}},
    )
    await asyncio.sleep(0)
    current = runner.get(job["task_id"])
    with pytest.raises(ValueError, match="Stale"):
        runner.steer(job["task_id"], {"speed": 0.2}, 0)
    updated = runner.steer(job["task_id"], {"speed": 0.2}, current["version"])
    assert updated["version"] > current["version"]
    assert updated["arguments"]["parameters"]["speed"] == 0.2
    expression = await gateway.call(
        session, "expr", "set_expression", {"expression": "happy"}, "u2"
    )
    assert expression["expression"] == "happy"
    await runner.shutdown()


async def test_voice_sleep_never_cancels_job(rig):
    session, runner, gateway = rig
    job = runner.start(session, "navigate", {"destination": "kitchen"})
    result = await gateway.call(session, "sleep", "sleep_voice", {}, "u1")
    assert gateway.sleep_requested and not result["tasks_cancelled"]
    assert job["task_id"] in runner.running
    await asyncio.gather(*runner.running.values())
    assert runner.get(job["task_id"])["state"] == "succeeded"


async def test_unknown_operation_not_replayed(rig, store):
    import hashlib

    session, runner, gateway = rig
    args = {"destination": "kitchen"}
    operation = hashlib.sha256(("u1" + "navigate" + encode(args)).encode()).hexdigest()
    store.execute("INSERT INTO operations VALUES(?,?,NULL)", (operation, session))
    result = await gateway.call(session, "retry", "navigate", args, "u1")
    assert result["error"] == "outcome_unknown_do_not_replay"
    assert not runner.running


async def test_task_failure_one_terminal_event(rig, store):
    session, runner, gateway = rig

    async def fail(*args):
        raise RuntimeError("injected")

    runner.robot.execute = fail
    job = runner.start(session, "navigate", {"destination": "bedroom"})
    await asyncio.gather(*runner.running.values())
    assert runner.get(job["task_id"])["state"] == "failed"
    await runner.cancel(job["task_id"])
    assert len(store.rows("SELECT * FROM events WHERE kind='task.failed'")) == 1


async def test_intentional_repeat_new_utterance(rig):
    session, runner, gateway = rig
    for n in range(2):
        job = await gateway.call(
            session, f"c{n}", "navigate", {"destination": "bedroom"}, f"utterance{n}"
        )
        assert job["status"] == "accepted"
        await asyncio.gather(*runner.running.values())


async def test_reordered_nested_arguments_deduplicate_completed_operation(rig, store):
    session, runner, gateway = rig
    first = {"policy": "walk", "parameters": {"duration_seconds": 0.1, "speed": 0.5}}
    reordered = {"parameters": {"speed": 0.5, "duration_seconds": 0.1}, "policy": "walk"}
    a = await gateway.call(session, "first", "run_policy", first, "same-request")
    await asyncio.gather(*runner.running.values())
    try:
        b = await gateway.call(session, "reordered", "run_policy", reordered, "same-request")
        assert a == b
        assert len(store.rows("SELECT * FROM tasks")) == 1
    finally:
        await runner.shutdown()


async def test_resource_rejection_retries_after_cancel_in_same_scope(rig):
    session, runner, gateway = rig
    try:
        current = await gateway.call(
            session, "initial", "navigate", {"destination": "kitchen"}, "u1"
        )
        rejected = await gateway.call(
            session, "busy", "navigate", {"destination": "bedroom"}, "switch"
        )
        assert "Resource busy" in rejected["error"]
        result = await gateway.call(
            session, "cancel", "cancel_task", {"task_id": current["task_id"]}, "switch"
        )
        assert result["state"] == "cancelled"
        # A repeated provider call remains deduplicated even after resource state changes.
        assert (
            await gateway.call(session, "busy", "navigate", {"destination": "bedroom"}, "switch")
            == rejected
        )
        accepted = await gateway.call(
            session, "retry", "navigate", {"destination": "bedroom"}, "switch"
        )
        assert accepted["status"] == "accepted"
    finally:
        await runner.shutdown()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["duration_seconds", "speed"])
async def test_nonfinite_policy_arguments_rejected_before_persistence(rig, store, value, field):
    session, runner, gateway = rig
    params = {"duration_seconds": 0.1, "speed": 0.5, field: value}
    try:
        result = await gateway.call(
            session, "bad-number", "run_policy", {"policy": "walk", "parameters": params}, "u1"
        )
        assert "error" in result
        assert not runner.running
        persisted = store.rows("SELECT arguments FROM tool_calls")[0]["arguments"]
        assert not any(token in persisted for token in ("NaN", "Infinity"))
        assert json.loads(persisted) is None  # invalid numeric payload is not retained
    finally:
        await runner.shutdown()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_steering_never_changes_authoritative_state(rig, store, value):
    session, runner, gateway = rig
    job = runner.start(
        session,
        "run_policy",
        {"policy": "walk", "parameters": {"duration_seconds": 0.5, "speed": 0.5}},
    )
    await asyncio.sleep(0)
    before = runner.get(job["task_id"])
    try:
        result = await gateway.call(
            session,
            "steer",
            "steer_task",
            {
                "task_id": job["task_id"],
                "changes": {"speed": value},
                "expected_version": before["version"],
            },
            "u2",
        )
        assert "error" in result
        after = runner.get(job["task_id"])
        assert after["version"] == before["version"]
        assert after["arguments"]["parameters"]["speed"] == 0.5
        assert json.loads(store.rows("SELECT arguments FROM tool_calls")[0]["arguments"]) is None
    finally:
        await runner.shutdown()


async def test_gateway_rechecks_close_admission_after_waiting_for_lock(rig, store):
    session, runner, gateway = rig
    await gateway.lock.acquire()
    waiting = asyncio.create_task(
        gateway.call(session, "late", "navigate", {"destination": "bedroom"}, "u1")
    )
    await asyncio.sleep(0)
    gateway.close_session(session)
    gateway.lock.release()
    result = await waiting
    assert result["error"] == "voice_session_closing"
    assert not runner.running
    assert not store.rows("SELECT * FROM tool_calls")


async def test_secret_dictionary_key_in_rejected_arguments_is_redacted(rig, store, monkeypatch):
    session, runner, gateway = rig
    secret = "sk-synthetic-argument-secret-123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    result = await gateway.call(session, "bad-key", "get_robot_state", {secret: "value"}, "u1")
    assert "error" in result
    assert secret not in json.dumps(store.export(session))


async def test_cancel_not_dispatched_before_close_remains_rejected(rig, store):
    session, runner, gateway = rig
    runner.robot.duration = 0.3
    job = runner.start(session, "navigate", {"destination": "bedroom"})
    await asyncio.sleep(0)
    call = asyncio.create_task(
        gateway.call(session, "cancel", "cancel_task", {"task_id": job["task_id"]}, "u2")
    )
    try:
        await asyncio.sleep(0)  # gateway created its settlement task, which has not run
        gateway.close_session(session)
        result = await call
        assert result["error"] == "voice_session_closing"
        assert runner.get(job["task_id"])["state"] == "running"
        assert not store.rows("SELECT * FROM events WHERE kind='task.cancelling'")
    finally:
        await runner.shutdown()


async def test_admitted_cancel_defers_repeated_caller_cancellation_until_durable_ack(
    rig, store, monkeypatch
):
    session, runner, gateway = rig
    acknowledgement_pending = asyncio.Event()
    acknowledge = asyncio.Event()
    original_execute = runner.robot.execute

    async def delayed_ack(*args):
        try:
            return await original_execute(*args)
        except asyncio.CancelledError:
            acknowledgement_pending.set()
            await acknowledge.wait()
            raise

    monkeypatch.setattr(runner.robot, "execute", delayed_ack)
    runner.robot.duration = 10
    job = runner.start(session, "navigate", {"destination": "bedroom"})
    await asyncio.sleep(0)
    call = asyncio.create_task(
        gateway.call(session, "cancel", "cancel_task", {"task_id": job["task_id"]}, "u2")
    )
    try:
        async with asyncio.timeout(2):
            await acknowledgement_pending.wait()
            gateway.close_session(session)
            call.cancel()
            await asyncio.sleep(0)
            call.cancel()
            await asyncio.sleep(0)
            assert not call.done()
            assert runner.get(job["task_id"])["state"] == "cancelling"
            assert (
                store.rows("SELECT result FROM tool_calls WHERE call_id='cancel'")[0]["result"]
                is None
            )
            acknowledge.set()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert runner.get(job["task_id"])["state"] == "cancelled"
            row = store.rows("SELECT result,operation_id FROM tool_calls WHERE call_id='cancel'")[0]
            result = json.loads(row["result"])
            assert result["state"] == "cancelled"
            assert result["result"]["acknowledged"] is True
            operation = store.rows(
                "SELECT result FROM operations WHERE id=?", (row["operation_id"],)
            )[0]
            assert json.loads(operation["result"]) == result
    finally:
        acknowledge.set()
        await asyncio.gather(call, return_exceptions=True)
        await runner.shutdown()
