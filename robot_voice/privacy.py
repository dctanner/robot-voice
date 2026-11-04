"""Best-effort per-value redaction, not streaming or general personal-data scrubbing.

Credentials split across transcript fragments can survive; keep all logs/exports private.
Audio payloads are never event-log inputs.
"""

import os
import re
from typing import Any

SENSITIVE = {"authorization", "api_key", "openai_api_key", "audio", "pcm", "headers"}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            sanitize(str(k)): "[redacted]" if str(k).lower() in SENSITIVE else sanitize(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        secret = os.environ.get("OPENAI_API_KEY")
        if secret:
            value = value.replace(secret, "[redacted]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[redacted]", value)
    return value
