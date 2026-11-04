# Contributing

Use Python 3.12 and a virtual environment. Install `.[dev]`; install `.[vad]` only
for the microphone path (see the README's CPU-only PyTorch guidance).

Before proposing a change:

```sh
python -m pytest -q
ruff check robot_voice tests
ruff format --check robot_voice tests
mypy robot_voice
python -m build
```

Normal tests must not require credentials, real audio devices, network access or
paid APIs. Use fake clocks, provider events and synthetic PCM. Keep names aligned
with the module/protocol under test. Never commit `.env`, audio, transcripts,
SQLite databases, exports or private test reports. Sanitize any public verification
summary; do not attach raw conversation logs to an issue.

Paid tests require explicit opt-in, bounded duration and a user-approved budget.
Track totals across invocations. Do not claim physical duplex success from a
synthetic sink or claim a mocked action controls hardware.

Tool changes need strict schemas, authoritative validation, idempotency, resource
checks, cancellation/restart tests and an explicit safety boundary. Do not add shell
execution, unrestricted robot commands or model-controlled permissions. Keep future
robot adapters independently safe when voice is asleep, disconnected or incorrect.
