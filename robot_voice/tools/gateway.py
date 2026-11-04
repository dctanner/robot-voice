import asyncio
import hashlib
import json

from jsonschema import ValidationError, validate

from robot_voice.memory.store import encode, now
from robot_voice.robot.tasks import ResourceBusy

from .registry import SCHEMAS


def canonical_arguments(arguments):
    """Stable recursive identity; reject NaN/infinities before any persistence."""
    return json.dumps(
        arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


class Gateway:
    def __init__(self, store, runner):
        self.store, self.runner = store, runner
        self.lock = asyncio.Lock()
        self.sleep_requested = False
        self.closed_sessions: set[str] = set()

    def close_session(self, session):
        self.closed_sessions.add(session)

    async def call(
        self,
        session,
        call_id,
        name,
        arguments,
        operation_scope,
        delegation_id=None,
        response_id=None,
    ):
        async with self.lock:
            # Check after acquiring the lock: a waiting worker may have crossed
            # the synchronous close-admission boundary while another call ran.
            if session in self.closed_sessions:
                return {"error": "voice_session_closing", "simulated": True}
            prior = self.store.rows("SELECT result FROM tool_calls WHERE call_id=?", (call_id,))
            if prior:
                return (
                    json.loads(prior[0]["result"])
                    if prior[0]["result"]
                    else {"error": "outcome_unknown_do_not_replay"}
                )
            validation_error = None
            recorded_arguments = arguments
            try:
                canonical = canonical_arguments(arguments)
            except (ValueError, TypeError):
                # Never persist nonstandard numbers, including in rejected calls/events.
                canonical = "null"
                recorded_arguments = None
                validation_error = {
                    "error": "Arguments must be finite JSON values",
                    "simulated": True,
                }
            if validation_error is None:
                try:
                    if name not in SCHEMAS:
                        raise ValueError("Tool not allowlisted")
                    if len(canonical) > 4000:
                        raise ValueError("Arguments too large")
                    validate(arguments, SCHEMAS[name])
                except (ValueError, ValidationError) as exc:
                    validation_error = {"error": str(exc).split("\n")[0][:200], "simulated": True}
            operation = hashlib.sha256((operation_scope + name + canonical).encode()).hexdigest()
            self.store.execute(
                "INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    session,
                    delegation_id,
                    response_id,
                    operation,
                    name,
                    encode(recorded_arguments),
                    None,
                    now(),
                ),
            )
            # Queries may refresh within an utterance; side effects share application identity.
            is_action = name not in ("get_robot_state", "get_task_status", "observe_scene")
            old = (
                self.store.rows("SELECT result FROM operations WHERE id=?", (operation,))
                if is_action and validation_error is None
                else []
            )
            cancellation_requested = False
            if validation_error is not None:
                result = validation_error
            elif old:
                result = (
                    json.loads(old[0]["result"])
                    if old[0]["result"]
                    else {"error": "outcome_unknown_do_not_replay"}
                )
            else:
                if is_action:
                    self.store.execute(
                        "INSERT INTO operations VALUES(?,?,NULL)", (operation, session)
                    )
                retryable_rejection = False
                try:
                    if name == "cancel_task":
                        # Once dispatched, cancellation acknowledgement belongs to the
                        # application, not the voice worker. Defer caller cancellation
                        # until both the robot acknowledgement and ledger writes settle.
                        settlement = asyncio.create_task(
                            self._dispatch_cancellation(session, arguments)
                        )
                        while True:
                            try:
                                result = await asyncio.shield(settlement)
                                break
                            except asyncio.CancelledError:
                                if settlement.cancelled():
                                    raise
                                cancellation_requested = True
                    else:
                        result = await self._dispatch(session, name, arguments)
                except ResourceBusy as exc:
                    # This typed rejection occurs before execution or task persistence.
                    # Keep provider-call dedupe, but let a NEW call recheck current resources.
                    result = {"error": str(exc), "simulated": True}
                    retryable_rejection = True
                except (ValueError, KeyError) as exc:
                    result = {"error": str(exc).split("\n")[0][:200], "simulated": True}
                if is_action:
                    if retryable_rejection:
                        self.store.execute("DELETE FROM operations WHERE id=?", (operation,))
                    else:
                        self.store.execute(
                            "UPDATE operations SET result=? WHERE id=?", (encode(result), operation)
                        )
            self.store.execute(
                "UPDATE tool_calls SET result=? WHERE call_id=?", (encode(result), call_id)
            )
            self.store.event(
                session,
                "tool.result",
                {
                    "call_id": call_id,
                    "name": name,
                    "arguments": recorded_arguments,
                    "result": result,
                    "operation_id": operation,
                },
            )
            if cancellation_requested:
                raise asyncio.CancelledError
            return result

    async def _dispatch_cancellation(self, session, arguments):
        # Creation of the shielded task itself yields. Recheck admission before
        # starting it, so shielding never grants permission to undispatched work.
        if session in self.closed_sessions:
            return {"error": "voice_session_closing", "simulated": True}
        return await self._dispatch(session, "cancel_task", arguments)

    async def _dispatch(self, session, name, args):
        if name in ("navigate", "pick_up", "run_policy"):
            return self.runner.start(session, name, args)
        if name == "get_robot_state":
            return self.runner.snapshot()
        if name == "get_task_status":
            return self.runner.get(args["task_id"])
        if name == "cancel_task":
            return await self.runner.cancel(args["task_id"])
        if name == "steer_task":
            return self.runner.steer(args["task_id"], args["changes"], args["expected_version"])
        if name == "observe_scene":
            return self.runner.robot.scene()
        if name == "set_expression":
            self.runner.robot.expression = args["expression"]
            return self.runner.robot.state()
        if name == "sleep_voice":
            self.sleep_requested = True
            return {"status": "voice_sleep_scheduled", "tasks_cancelled": False}
        raise ValueError("Unknown tool")
