import asyncio

from robot_voice.memory.store import now


class MockRobot:
    def __init__(self, duration=10):
        self.duration = duration
        self.location = "living room"
        self.held_object = None
        self.expression = "neutral"
        self.objects = ["red sock", "blue sock", "white mug"]

    def state(self):
        return {
            "simulated": True,
            "location": self.location,
            "held_object": self.held_object,
            "expression": self.expression,
            "at": now(),
            "capabilities": ["navigate", "pick_up", "walk", "jump", "set_expression"],
        }

    def scene(self):
        return {"simulated": True, "objects": self.objects.copy(), "at": now()}

    def validate(self, name, arguments):
        if name == "pick_up":
            description = arguments["object_description"].lower()
            matches = [o for o in self.objects if description in o or o in description]
            if len(matches) != 1:
                raise ValueError("Object ambiguous or not found in mocked scene")
            if self.held_object:
                raise ValueError("Mock gripper already holds an object")

    async def execute(self, name, arguments, progress):
        duration = arguments.get("parameters", {}).get("duration_seconds", self.duration)
        for i in range(4):
            await asyncio.sleep(duration / 4)
            await progress((i + 1) / 4)
        if name == "navigate":
            self.location = arguments["destination"]
        elif name == "pick_up":
            self.validate(name, arguments)
            self.held_object = next(
                o
                for o in self.objects
                if arguments["object_description"].lower() in o
                or o in arguments["object_description"].lower()
            )
            self.objects.remove(self.held_object)
        return {"simulated": True, "verified": True, "state": self.state()}
