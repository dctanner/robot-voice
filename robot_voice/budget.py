"""Local conservative guard, NOT a provider billing cap."""

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    limit: float = 4.5
    max_seconds: float = 1200
    max_requests: int = 100
    active_seconds: float = 0
    estimate: float = 0
    requests: int = 0
    reservations: dict[str, float] = field(default_factory=dict)
    usage: dict[str, dict] = field(default_factory=dict)

    def check(self):
        if (
            self.estimate + sum(self.reservations.values()) >= self.limit
            or self.active_seconds >= self.max_seconds
            or self.requests >= self.max_requests
        ):
            raise BudgetExceeded("Local cloud budget/time/request cap reached; no automatic retry")

    def reserve(self, identity: str, dollars: float = 0.10):
        if identity in self.reservations or identity in self.usage:
            return
        self.check()
        if self.estimate + sum(self.reservations.values()) + dollars >= self.limit:
            raise BudgetExceeded("Insufficient conservative cloud reserve")
        self.reservations[identity] = dollars
        self.requests += 1

    def record(self, identity: str, kind: str, usage: dict, final: bool = True):
        old = self.usage.get(identity, {})
        if old.get("final") and not final:
            return
        if kind == "live":
            seconds = float(usage.get("seconds", 0))
            cost = seconds * 0.05 / 60
        elif kind == "transcribe":
            seconds = 0
            cost = float(usage.get("seconds", 0)) * 0.0045 / 60
        elif kind == "tts":
            seconds = 0
            # Conservative fixture estimate; token usage not exposed by binary TTS response.
            cost = float(usage.get("seconds", 0)) * 0.02 / 60
        else:
            seconds = 0
            details = usage.get("input_tokens_details") or {}
            cached = details.get("cached_tokens", 0) or 0
            written = details.get("cache_write_tokens", 0) or 0
            ordinary = max(0, usage.get("input_tokens", 0) - cached - written)
            cost = (
                ordinary * 0.10
                + cached * 0.01
                + written * 0.125
                + usage.get("output_tokens", 0) * 0.50
            ) / 1_000_000
        self.estimate += cost - old.get("cost", 0)
        self.active_seconds += seconds - old.get("seconds", 0)
        self.usage[identity] = {
            "kind": kind,
            "cost": cost,
            "seconds": seconds,
            "usage": usage,
            "final": final,
        }
        if final:
            self.reservations.pop(identity, None)
        self.check()
