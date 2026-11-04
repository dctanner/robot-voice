import asyncio
import json
from uuid import uuid4

from robot_voice.memory.store import encode, now

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
RESOURCES = {"navigate": "base", "pick_up": "arm", "run_policy": "base"}


class ResourceBusy(ValueError):
    """Known pre-execution rejection; a new call may recheck after resource changes."""


class TaskRunner:
    """Application-owned tasks. Closing a voice connection never cancels this runner."""

    def __init__(self, store, robot):
        self.store, self.robot = store, robot
        self.running: dict[str, asyncio.Task] = {}
        self.arguments: dict[str, dict] = {}
        self.on_terminal = None

    def get(self, identity):
        rows = self.store.rows("SELECT * FROM tasks WHERE id=?", (identity,))
        if not rows:
            raise ValueError("Unknown task")
        row = rows[0]
        for key in ("arguments", "result"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def snapshot(self):
        jobs = self.store.rows(
            "SELECT * FROM tasks ORDER BY CASE WHEN state IN "
            "('accepted','running','cancelling') THEN 0 WHEN announced=0 THEN 1 ELSE 2 END, "
            "created DESC LIMIT 30"
        )
        return {**self.robot.state(), "jobs": jobs}

    def start(self, session, name, arguments):
        self.robot.validate(name, arguments)
        resource = RESOURCES[name]
        conflicts = self.store.rows(
            "SELECT id FROM tasks WHERE resource=? AND state IN ('accepted','running','cancelling')",
            (resource,),
        )
        if conflicts:
            raise ResourceBusy("Resource busy; cancel and await acknowledgement before replacement")
        identity = "task_" + uuid4().hex
        self.store.execute(
            "INSERT INTO tasks(id,session_id,state,version,resource,name,arguments,created,updated) VALUES(?,?,'accepted',1,?,?,?,?,?)",
            (identity, session, resource, name, encode(arguments), now(), now()),
        )
        self.store.event(
            session, "task.accepted", {"task_id": identity, "name": name, "arguments": arguments}
        )
        self.arguments[identity] = arguments
        self.running[identity] = asyncio.create_task(self._run(identity), name=identity)
        return {"task_id": identity, "status": "accepted", "simulated": True, "version": 1}

    def transition(self, identity, state, result=None):
        current = self.get(identity)
        if current["state"] in TERMINAL:
            return
        self.store.execute(
            "UPDATE tasks SET state=?,version=version+1,result=?,updated=? WHERE id=?",
            (state, encode(result) if result is not None else None, now(), identity),
        )
        self.store.event(
            current["session_id"],
            "task." + state,
            {"task_id": identity, "version": current["version"] + 1, "result": result},
        )

    async def _run(self, identity):
        current = self.get(identity)
        try:
            self.transition(identity, "running")

            async def progress(value):
                self.store.event(
                    current["session_id"], "task.progress", {"task_id": identity, "progress": value}
                )

            result = await self.robot.execute(current["name"], self.arguments[identity], progress)
            self.transition(identity, "succeeded", result)
        except asyncio.CancelledError:
            self.transition(identity, "cancelled", {"acknowledged": True, "simulated": True})
        except Exception as exc:
            self.transition(identity, "failed", {"error": type(exc).__name__, "simulated": True})
        finally:
            self.running.pop(identity, None)
            self.arguments.pop(identity, None)
            if self.on_terminal:
                self.on_terminal(self.get(identity))  # callback only enqueues; never awaits voice

    async def cancel(self, identity):
        row = self.get(identity)
        if row["state"] in TERMINAL:
            return row
        self.transition(identity, "cancelling")
        task = self.running.get(identity)
        if task:
            # Let newly accepted tasks enter their try/finally before cancellation.
            await asyncio.sleep(0)
            task.cancel()
            await task
        else:
            self.transition(identity, "interrupted", {"verified": False})
        return self.get(identity)

    def steer(self, identity, changes, expected_version):
        row = self.get(identity)
        if row["version"] != expected_version:
            raise ValueError("Stale task version; get fresh state")
        if row["state"] != "running" or row["name"] != "run_policy":
            raise ValueError("Only running mock policies support speed guidance")
        # Mock speed is reported guidance, not a physical velocity or deadline modification.
        args = self.arguments[identity]
        args.setdefault("parameters", {}).update(changes)
        self.store.execute(
            "UPDATE tasks SET arguments=?,version=version+1,updated=? WHERE id=?",
            (encode(args), now(), identity),
        )
        self.store.event(
            row["session_id"], "task.steered", {"task_id": identity, "changes": changes}
        )
        return self.get(identity)

    async def shutdown(self):
        for identity in list(self.running):
            await self.cancel(identity)
