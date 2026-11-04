def obj(properties=None):
    properties = properties or {}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def text(max_length=200):
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def enum(*values):
    return {"type": "string", "enum": list(values)}


SCHEMAS = {
    "navigate": obj({"destination": enum("living room", "kitchen", "bedroom")}),
    "pick_up": obj({"object_description": text()}),
    "run_policy": obj(
        {
            "policy": enum("walk", "jump"),
            "parameters": obj(
                {
                    "duration_seconds": {"type": "number", "minimum": 0.1, "maximum": 300},
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 1},
                }
            ),
        }
    ),
    "set_expression": obj({"expression": enum("neutral", "happy", "curious")}),
    "get_robot_state": obj(),
    "get_task_status": obj({"task_id": text(80)}),
    "steer_task": obj(
        {
            "task_id": text(80),
            "changes": obj({"speed": {"type": "number", "minimum": 0.1, "maximum": 1}}),
            "expected_version": {"type": "integer", "minimum": 1},
        }
    ),
    "cancel_task": obj({"task_id": text(80)}),
    "observe_scene": obj(),
    "sleep_voice": obj(),
}
DESCRIPTIONS = {
    "navigate": "Start simulated navigation to a known room; accepted is not done.",
    "pick_up": "Start simulated pickup of exactly one matching object in the fake scene.",
    "run_policy": "Start a bounded simulated movement job. No hardware is controlled.",
    "set_expression": "Set a mock expression independently of motion.",
    "get_robot_state": "Read authoritative fresh simulated state, task IDs and versions.",
    "get_task_status": "Read fresh task state and version.",
    "steer_task": "Set reported mock policy speed only. Requires current version; duration unchanged.",
    "cancel_task": "Cancel a job and wait for actual mock cancellation acknowledgement.",
    "observe_scene": "Read a timestamped fake scene, NOT camera perception.",
    "sleep_voice": "Request voice sleep after bookkeeping. Does not cancel robot jobs.",
}


def tools():
    return [
        {
            "type": "function",
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": schema,
            "strict": True,
        }
        for name, schema in SCHEMAS.items()
    ]
