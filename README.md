# Robot Voice

A Python voice interface that **sleeps independently of its robot jobs**.
Say “Hey Spot, pick up the red sock” in one breath, keep chatting while a simulated
job runs, let voice time out, then wake it again with today's context.

**This is an experimental mock-robot CLI, not a production robot controller.**
No motors, camera, ROS, emergency stop, or autonomous physical actions are implemented.
GPT-Live handles conversation; its managed Luna backend requests allowlisted tools;
Python validates and runs the jobs. SQLite, not the language model, owns task state.

## What works—and what has not been proved

- Explicit configurable wake phrase: local Silero VAD → bounded OpenAI transcription.
- The entire opening utterance goes into Live **once as text**; only later audio is
  streamed, at microphone rate. Startup buffers are bounded; overflow fails visibly.
- Independent simulated navigation, pickup, movement policies, expressions, task status,
  cancellation and version-checked speed guidance. Chat does not cancel jobs.
- Idle sleep (30s default), bounded task hold (120s), maximum session duration and
  local estimated-cost/request/time guards. Muting **does not** stop billing.
- Durable events, transcript fragments, usage, capped asynchronous summaries, and the
  latest ten local-day memory slices, with an extractive fallback while summaries run.
- Offline demo, developer tests, and explicit opt-in synthesized-audio cloud tests.

The paid test campaign exercised **29.87 active minutes across 15 sessions**,
including unsuccessful diagnostic runs. A **10m32s continuous session after the pacing
fix** and its next-wake summary recall passed on the pre-review implementation; a
separate 381-second regression exercised independent jobs and steering/cancellation.
The first review fixes passed **116 offline tests** and a **19-second confirmed
cloud smoke** (pickup, idle close and summary), not another ten-minute soak.
The final lifecycle and request-identity fixes pass **143 offline tests**; no further
cloud smoke was run for those revisions.
See [verification details and limitations](docs/VERIFICATION.md).
**Microphone/speaker hardware, echo cancellation and room wake accuracy are not verified.**
The test host has no PortAudio system library; synthetic cloud success is not a
laptop-speaker/full-duplex acceptance result.

[ROBOT_VOICE_PLAN.md](ROBOT_VOICE_PLAN.md) is the original design, not a list of
hardware-tested promises. Custom acoustic “Hey Spot”, semantic addressing, WebRTC,
vision and real robot adapters remain future work.

## Quickstart (Python 3.12 recommended)

You need one `OPENAI_API_KEY` with access to `gpt-live-1`, `gpt-6-luna` and
`gpt-transcribe`. Access and pricing are account/model dependent. No other cloud
credential, GPU, web service or UI is required.

Install PortAudio for microphone/speaker use:

| OS | Setup |
| --- | --- |
| Debian/Ubuntu | `sudo apt install libportaudio2` (development headers only needed for source builds) |
| macOS | `brew install portaudio`; allow microphone permission for your terminal |
| Windows | `sounddevice` pip wheels normally include PortAudio; allow microphone access |

The intended public repository is `https://github.com/dctanner/robot-voice`.
The clone steps below are usable after publication; release review does not itself
create or publish the repository.

With [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/dctanner/robot-voice.git
cd robot-voice
uv venv --python 3.12
# Linux: install CPU-only PyTorch first to avoid unnecessary CUDA packages.
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
uv pip install -e '.[vad,dev]'
cp .env.example .env
chmod 600 .env             # Unix; restrict ACLs on Windows instead
# Edit .env locally: OPENAI_API_KEY=your-key
uv run --no-project robot-voice doctor
uv run --no-project robot-voice devices
uv run --no-project robot-voice run --audio-mode headphones
```

On macOS/Windows you may omit the CPU-index step and let the `vad` extra install
PyTorch. Without uv:

```sh
git clone https://github.com/dctanner/robot-voice.git
cd robot-voice
python3.12 -m venv .venv
. .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
# Same optional Linux CPU-only PyTorch step as above, using python -m pip.
python -m pip install -e '.[vad,dev]'
cp .env.example .env
chmod 600 .env             # Unix; restrict ACLs on Windows instead
# Edit .env locally: OPENAI_API_KEY=your-key
robot-voice doctor
robot-voice run --audio-mode headphones
```

Dotenv loads `.env` without overwriting existing environment variables. Never put
keys in TOML, command-line arguments, issues, exported sessions or commits.
`doctor` is offline: it reports key **presence**, package/device availability and
format support, not credential validity. A missing VAD extra or PortAudio library
is an actionable diagnostic, not a passed audio test.

For a quick **zero-cloud, no-audio** check, install just `'.[dev]'` and run:

```sh
robot-voice demo
python -m pytest -q
```

`demo` demonstrates one deduplicated pickup job and pending-summary memory; it does
not synthesize speech or imitate a successful cloud connection. Use a fresh database
for a repeatable demo: `robot-voice --database .robot-voice/demo.sqlite3 demo`.

## Conversation and CLI

```sh
robot-voice run --wake-phrase "hey spot" --idle-timeout 30 --audio-mode headphones
robot-voice run --wake-mode keyboard --audio-mode headphones
robot-voice --config examples/config.toml run
```

Try: “Hey Spot, pick up the red sock on the floor.” Then ask what it is doing,
request navigation to the kitchen, or ask for the simulated scene. During a walking
policy, ask for lower speed or cancellation. A destination switch must acknowledge
cancellation before starting conflicting movement. “Go to sleep” closes voice,
**not the robot job**. Ctrl+C exits the application and cancels its in-process mock
jobs. After a crash, outstanding mock jobs are marked interrupted, never falsely
reported as having kept running. An already-dispatched cancellation still waits for
robot acknowledgement and durably records its result if voice closes; undispatched
work remains rejected after close commitment.

The local chime means **wake detected**, not connection ready. The CLI distinguishes
CONNECTING, ACTIVE and STANDBY. Keyboard mode wakes on Enter; speak after waking.
Global `--config` and `--database` options go **before** the command.

### Audio modes

| Mode | Intended setup | Limitation |
| --- | --- | --- |
| `half-duplex` (default) | Speakers without an established AEC path | Mic forwarding becomes silence during playback/chimes. **No barge-in**; not full duplex. |
| `headphones` | Headset or well-isolated headphones | Sends input while output plays; echo isolation is your responsibility. |
| `external-aec` | OS/hardware echo-cancelled input, explicitly selected | Full-duplex transport is available, but AEC must actually include the selected speaker output **and chime** as its reference. |

```sh
robot-voice devices
robot-voice run --audio-mode external-aec \
  --input-device "Your echo-cancelled source" --output-device "Your speakers"
```

PortAudio does **not** supply automatic echo cancellation. VAD and noise suppression
are not substitutes. On Linux, configure an OS echo-cancel source/sink pair (for
example PipeWire's echo-cancel module) and select the matching input **and output**;
selecting a name containing “echo” proves nothing. Test with low volume, then actual
speakers, checking for feedback/self-wakes and overlapping speech. No OS AEC setup is
installed or certified by this project.

The Live wire format is mono PCM16 at **16 kHz**. Input devices at other supported
rates can use `hardware_rate` in TOML; a stateful linear resampler supplies 16 kHz.
The selected output device must accept 16 kHz. This is speech-prototype resampling,
not a high-quality audio production pipeline. Callback queues only move PCM; cloud
calls, SQLite and inference run outside PortAudio callbacks.

Standby transcription uploads **nearby speech even without a wake phrase**. A TV or
recording saying the phrase can wake it; this is not speaker identification. Clips
are at most 15s, assembled utterances at most 45s, with bounded overlap and queues.
Long split-clip transcription remains approximate. Non-wake text is discarded locally.
Genuinely new speech during an idle close is retained for a follow-on session in the
default transcription wake mode, provided the preceding captured utterance ended.
Duration/error closes, explicit sleep, or an incomplete capture boundary discard the
remaining speech through 0.6s of continuous silence and visibly request a fresh wake.
Other wake modes still require their wake trigger; discarded audio is not replayed.

### Optional local keyword mode

Install `'.[keyword]'`, provide your own compatible licensed ONNX acoustic model in
`keyword_model`, and choose `wake_mode = "keyword"`. No standby transcription is
used in that mode; detection runs continuously, not behind VAD. The last three
seconds of audio are replayed **at 1×** after startup, so backlog remains latency.
This adapter is unvalidated with real rooms/models. No custom “Hey Spot” model ships.
Check openWakeWord's code and pretrained-model licenses separately; included models
are not automatically licensed for commercial deployment.

## Configuration, costs and privacy

Copy [examples/config.toml](examples/config.toml), then set only what you need.
Unknown settings and invalid limits are rejected. Defaults and types live in
[`config.py`](robot_voice/config.py). Useful controls:

- `idle_timeout=30`, `reply_grace=5`, `task_hold_max_seconds=120`
- `keep_open_while_task_running=true`; the hold is anchored to the user-idle episode,
  not refreshed by progress or newly chained jobs
- `max_session_seconds=300`, `max_active_seconds=1200`, `max_cloud_requests=100`
- `budget_dollars=4.5`; `backend_timeout=30`, `close_timeout=8`
- `startup_seconds=10`, `timezone` (system IANA zone by default), `wake_chime=true`
- model names, selected devices and `database` (default `.robot-voice/voice.sqlite3`)

These are **application-local estimates and limits, not a provider billing cap**.
The budget resets when the application process restarts. It reserves $0.10 for each
unresolved request; uncertainty is not silently charged as zero. Session silence,
waiting and muted input still cost money. SDK automatic retries are disabled;
summary retries are capped. No automatic paid reconnect occurs after a network loss.

Default estimates use Live $0.05/active minute, transcription $0.0045/submitted
minute, and standard Luna $0.10/M input, $0.50/M output (including reasoning),
$0.01/M cached input and $0.125/M cache writes. **Recheck pricing before use or
changing models.** Fixture TTS uses a $0.02/minute *estimate*; its published rates
are token-based. The default `gpt-4o-mini-tts` fixture generator is configurable and
marked deprecated upstream; it is not required for normal microphone operation.
See [OpenAI pricing](https://developers.openai.com/api/docs/pricing).

Live usage updates replace previous cumulative values; backend usage is deduplicated
by response ID. Only `session.closed` confirms final voice usage. A lost connection
is labeled unconfirmed with a wall-time estimate. This is not an invoice reconciliation
or a guarantee against unexpected provider billing.

Cloud audio/text is subject to your provider/account retention policies. Live
recording storage and standalone summary Responses storage are disabled, which
**does not imply zero provider retention**. SQLite stores private conversation text,
arguments, results and approximate transcript fragments. No raw audio is recorded by
`run`; synthetic test fixtures and evidence are saved separately under ignored local
runtime directories. Playback counters measure submitted samples, not proof a person
heard particular words. Transcripts may overlap and are not complete turns.

### Inspect, export and delete

```sh
robot-voice history --today
robot-voice export --session SESSION_ID --format text
robot-voice export --session SESSION_ID --format json > .robot-voice/private-session.json
robot-voice export --session SESSION_ID --format jsonl > .robot-voice/private-session.jsonl
robot-voice delete --session SESSION_ID --yes
robot-voice delete --before 2026-01-01T00:00:00+00:00 --yes
```

**Exports are private.** Best-effort redaction checks dictionary keys and complete
string values and omits audio payloads; it is not an absolute credential-removal
guarantee or a personal-data scrubber. In particular, credentials split across
transcript fragments can survive and be reconstructed when fragments are joined:
streaming credential redaction is not implemented. Do not speak credentials or
commit logs/exports. Use a private output directory.
Storage uses SQLite foreign keys/WAL, owner-only database permissions on Unix, and
an exclusive runtime lock. Maintenance commands require the runner to be stopped.
No automatic retention deletion runs: use `delete --before` on your schedule.
Deletion cascades session records and associated task history and vacuums SQLite;
filesystem snapshots, backups, provider retention and independently stored synthetic
fixtures are not erased. Usage-only request records without a session are retained;
remove the stopped runtime directory for a complete local reset.

Recall selects up to ten current-local-day slices, chronologically injected within
a ~9,000-character prompt budget. Calendar midnight is converted to UTC; DST days
are not treated as fixed 24-hour intervals. Cross-midnight sessions are sliced by
UTC event timestamps; a changed timezone derives new extractive slices from evidence.
Summaries are generated in the configured worker timezone, with fallback after a
zone change. Current task facts can cross midnight; yesterday's conversation does not.
Memory, summaries and tool strings are data, **never permissions or executable policy**.

## Development and verification

```sh
python -m pytest -q                  # deterministic; no paid calls or hardware
ruff check robot_voice tests
ruff format --check robot_voice tests
mypy robot_voice
python -m build
```

The OpenAI SDK version is pinned, but transitive dependencies and build tools are
not locked; exact environment reproduction is not guaranteed.

Cloud tests are explicit and synthetic only; **never substitute a room recording**:

```sh
robot-voice live-test --allow-paid --seconds 35 --cycles 1 \
  --startup-delay .5 --output .robot-voice/smoke
# One continuous session exceeding 10 active minutes; use a foreground timeout.
robot-voice live-test --allow-paid --seconds 650 --cycles 1 \
  --startup-delay 3 --output .robot-voice/soak
# Separate close/wake, long-job, steering and cancellation regression:
robot-voice live-test --allow-paid --seconds 135 --cycles 3 \
  --startup-delay 3 --output .robot-voice/multi-cycle
```

Use a **new output directory** for each campaign. The harness generates/reuses TTS
fixtures, transcribes the opening wake command, sends subsequent PCM at 1×, and
writes private SQLite evidence plus `report.json`. It never opens an audio device;
output is accounted for but discarded. It checks first-command pickup/finalization,
ordinary idle closure, summary completion, protocol errors and minimum duration for long runs. Inspect tool results, close reasons, summaries
and the report; absence of an exception alone is not proof of every behavior.
There are finite request/token/time/duration caps and no infinite retries. Aggregate
limits across multiple invocations are **your responsibility**. Use a foreground
wall-time timeout for manual paid testing. No GitHub Actions workflows are included;
run the checks above locally.

### Extending it

| Seam | Contract |
| --- | --- |
| `audio/frontend.py:AudioFrontend` | Ordered sample-indexed `Frame`s; report loss, never hide it |
| `wake/base.py` | Speech and explicit wake interfaces; never treat VAD as identity |
| `voice/base.py:VoiceProvider` | Capabilities distinguish duplex, images and paced audio; `begin_close()` synchronously closes tool admission |
| `robot/base.py:RobotAdapter` | Validate and run bounded jobs; real controllers must own safety |
| `memory/summaries.py:SummaryProvider` | Structured, attributable historical data, no actions |

Add a tool's strict JSON schema and description in `tools/registry.py`, validation
and dispatch in `tools/gateway.py`, and robot behavior/resource requirements in
`robot/`. Test duplicate provider calls and application operation identities,
resource conflicts, cancellation acknowledgement, stale versions and failures.
Queries may refresh state; repeated side effects in the same utterance use recursively
key-sorted operation identities. A new utterance requires at least 0.6 seconds of
contiguous captured silence; brief VAD dropouts do not reset its identity. Backend
delegations and responses retain their originating identity even when their function
items arrive during newer speech. Late correlation inherits an already-known identity;
conflicting established identities close the voice session rather than guessing.
Non-finite numbers are rejected before storage or
dispatch. A known pre-execution resource conflict may be retried with a new provider
call after cancellation; the original provider call remains deduplicated. Closing
voice synchronously shuts tool admission before terminal events are drained.
Unknown outcomes are never automatically replayed. Intentional new utterances can
repeat a completed action.

The current transport is **GPT-Live WebSocket**, not the older Realtime protocol.
Function calls arrive inside `response.event` → `response.output_item.done`;
lifecycle snapshots have empty output arrays. Results use `response.item.create`
then one `response.create` continuation. Job completion is a new session-wide
commentary update with `delegation_id: null`, not a second function result. Output
PCM includes silence and has no voice-turn-done marker: speech/queue quiet guards
are heuristic. A real robot integration additionally needs robot-owned arbitration,
watchdogs, collision avoidance, disconnect policy and a physical emergency stop.
Voice “stop” is never a safety-rated stop.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and the [MIT license](LICENSE).
