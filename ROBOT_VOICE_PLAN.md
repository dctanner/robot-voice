# Robot Voice: Python prototype plan

## 1. Recommendation

Build a **voice interface that can sleep independently of the robot**, not a voice model that owns the robot’s execution loop.

The first prototype is a Python CLI running on a laptop, using its microphone and speakers, one OpenAI API key, mocked robot capabilities, and SQLite. It should prove this interaction:

> “Hey Spot, pick up the red sock.” → wake acknowledgement → natural conversation → simulated pickup runs in the background → conversation continues → verified completion is spoken → after inactivity the voice session closes → a later wake restores today’s conversation context.

Use **GPT-Live for conversation**, **Luna for tool selection and summaries**, and a **local speech detector plus OpenAI transcription for the initial configurable wake phrase**. Add a fully local wake-word detector as the next optimization. Do not require Deepgram, a GPU, ROS, a web UI, or policy training.

The long-term unified audio/video/action model is a plausible direction, not a dependency. Even with a future unified model, robot-side safety, task state, and control deadlines should remain independently enforced.

**This document is a design, not an implemented or hardware-tested system.** API details below were checked against the linked current documentation. Account access, SDK compatibility, microphone behavior, and latency still need a practical spike.

## 2. What the current APIs actually support

| Component | Proposed choice | Important distinction |
|---|---|---|
| Live conversation | `gpt-live-1` | Full-duplex audio conversation; delegates reasoning/tool use to a backend. |
| Backend reasoning | `gpt-6-luna`, configurable | Start with GPT-Live’s managed Responses delegation. Python executes the actual function tools. |
| Wake transcription | `gpt-transcribe` | Submit bounded speech clips to `/v1/audio/transcriptions`. No continuous cloud connection is necessary. |
| Optional wake classification | Decisions API with `gpt-6-luna` | Classifies transcribed text; it does not directly accept audio. Not needed for the first explicit-wake implementation. |
| Session summarization | Luna through Responses | Produces structured factual summaries after voice closes. |
| Speech/noise detection | Local Silero VAD | Detects speech, not the intended listener, a particular person, or a wake phrase. |
| Local keyword detection | Optional openWakeWord adapter | Avoids standby cloud transcription, but “Hey Spot” needs a suitable model. |

GPT-Live and the older Realtime API are **different protocols**. GPT-Live uses `session.start`, `session.input_audio.append`, and its delegation events—not Realtime’s `input_audio_buffer.commit` and conversation-item workflow. Keep any future Realtime adapter separate. [1–4]

Current GPT-Live is audio/text, not direct image/video input. Future camera questions should go to a vision-capable backend, which returns timestamped observations to the voice layer. [1, 10]

The current transcription guide recommends `gpt-transcribe`; it marks the older Whisper/OpenAI transcription models as deprecated, with shutdown scheduled for February 26, 2027. Do not base a new default on an obsolete example. Local Whisper implementations are a separate option. [5]

## 3. Scope and architectural boundary

### Prototype includes

- Laptop microphone and speaker selection, audio diagnostics, and clear CLI state.
- Configurable wake phrase, initially “Hey Spot”; keyboard wake for debugging.
- Continuous conversation after waking, without repeating the wake phrase.
- Mock navigation, pickup, movement policies, expressions, task cancellation, and guidance.
- Conversation while tasks run, including switching tasks.
- A default **30-second inactivity timeout**, configurable to 40 seconds or another value.
- A bounded hold-open policy for short tasks and independent execution for long ones.
- Persistent transcripts, tool calls/results, task events, usage, and session summaries.
- At each wake, the latest **up to 10 conversation summaries from the current local day**.

### Deliberately excluded

Actual motors, ROS integration, navigation/perception stacks, policy training, continuous video, avatars, user identification, an always-listening semantic assistant, and a web UI. A mocked observation tool can demonstrate the interface without pretending the laptop sees anything.

### Logical architecture

```text
Laptop microphone
    │
    ▼
Audio frontend: capture / resampling / echo control
    ├── bounded RAM audio buffer
    ├── local VAD + wake strategy ─────────────┐
    └── active audio stream                  │ wake
                                             ▼
                                    Session supervisor
                                             │
                                  GPT-Live voice session
                                    │                │
                               speaker audio    Luna backend
                                                      │ tool requests
                                                      ▼
                                           Validating tool gateway
                                                      │
                                           Independent task runner
                                                      │
                                           Mock robot adapter
                                                      │
                                  task events / verified robot state

SQLite event log + transcripts + task ledger + summaries
             ▲                          │
             └──── background summary worker
```

Implement these as modules and asynchronous workers in **one Python process** initially. They are separate lifecycles, not separate services. A voice-session teardown must not cancel the application-owned task runner.

## 4. Wake strategies

### Default prototype: VAD → transcription → explicit phrase

This gives “Hey Spot” without first training a custom keyword model and keeps cloud services behind one API key.

1. Capture continuously into a short in-memory ring buffer.
2. Local VAD assembles a speech segment with approximately 300 ms of pre-roll.
3. End the segment after roughly 600 ms of silence; tune this on the laptop.
4. Transcribe that bounded clip using OpenAI.
5. Normalize case/punctuation and require a word-boundary wake phrase near the beginning, not an arbitrary substring such as “spot” anywhere.
6. On a match, play a brief local chime, load memory/state, and start GPT-Live.
7. Preserve any command spoken in the same utterance as the wake phrase.

Use bounded clip length, request concurrency, and queue length. A starting maximum clip length of 15 seconds is reasonable, but a forced split is not a complete turn: preserve overlap, avoid duplicate text, and assemble the wake command before acting. Discard non-wake clips and their transcripts after the decision by default.

**Trade-off:** this waits for an utterance boundary and a transcription request. It is not the fastest wake experience, and speech around the robot is uploaded even when it is not addressed to the robot. Show that clearly at startup. It saves voice-session cost, not all standby cost.

### Next optimization: local keyword detector

Use a `WakeDetector` adapter backed by openWakeWord. Once loaded, no standby audio needs to leave the laptop. Start with an existing supported phrase for benchmarking, then train/evaluate “Hey Spot” or select a properly licensed equivalent.

Do not advertise arbitrary wake words as a configuration-only feature for an acoustic keyword model. openWakeWord’s repository distinguishes Apache-2.0 code from its included noncommercial pretrained model licenses; check redistribution and commercial use separately. [8]

Run keyword inference continuously, optionally validating detections with VAD; do not gate away the beginning of the keyword. Calibrate false accepts and missed wakes using actual room noise and voices.

### Later: semantic addressing

Optionally send bounded transcription context to Decisions and ask whether the utterance is addressed to Spot. Start with conservative classification and an explicit-name requirement. Calling this classifier does not grant permission to execute a robot task.

Text alone cannot reliably distinguish a TV saying “Hey Spot,” a person discussing the robot, and a person addressing it. Speaker direction, visual attention, or enrollment would be additional features—not guarantees provided by VAD or an LLM. Do not add this extra inference hop to the default explicit-wake path. [6]

## 5. Preserve the first command through startup

This is the most important integration spike.

### Shared buffering contract

- Assign captured audio monotonically increasing sample indexes and a capture timestamp.
- Keep a configurable rolling buffer, initially 3 seconds, and pin the current wake segment while it is being evaluated.
- Once a wake candidate is accepted, retain newly arriving samples in a separate bounded startup queue, initially capped at 10 seconds.
- One sender owns ordered input delivery. It switches from backlog to live capture without duplicate samples or gaps.
- If startup fails or the queue overflows, report it and ask the user to repeat. Never silently drop the beginning and execute the remainder.
- A local chime means “wake detected,” not “connection ready.” The CLI shows both states separately. Capture continues during the chime; echo control must remove its speaker contribution.

### Audio handoff for a local keyword detector

After `session.started`, send preserved audio followed by live microphone input using GPT-Live’s supported continuous stream. The docs explicitly recommend buffering opening speech across restart, but also specify microphone-rate pacing. They do **not** establish that an arbitrary backlog can be flushed instantly. [2, 3]

Therefore:

- Do not borrow Realtime’s recorded-audio upload assumptions for GPT-Live.
- First implement chronological, paced replay and measure the resulting delay.
- Sending continuously at 1× speed leaves the startup backlog as latency; it does not magically catch up.
- Any faster catch-up or silence-only backlog reduction must be validated separately for this API. Do not discard speech, accelerate speech samples, or claim zero latency without evidence.

### Text handoff for the default transcription-based wake

The complete first utterance has already been transcribed. Seed it **once** as a user message in `session.input`, after the day’s memory. Mark its provenance in local logs. The voice prompt tells GPT-Live to address that pending request rather than greet again or treat it as a completed exchange.

Do not also replay the same utterance’s audio. Keep an exact sample cutoff and stream only subsequent captured audio. Preserve speech arriving during transcription/startup; do not treat those samples as disposable silence.

This avoids replaying a several-second initial command at microphone speed, at the cost of losing that utterance’s prosody and relying on its transcription. Test startup behavior explicitly: history insertion is not proof the request was handled. The spike must demonstrate that the pending command is answered/delegated exactly once before this becomes the default.

**Required test:** say “Hey Spot, pick up the red sock on the floor” without pausing after the wake phrase, inject 0.5–3 seconds of connection delay, and verify the complete command survives without a duplicate pickup.

## 6. Conversation and timeout state machine

```text
STANDBY → WAKE_CHECK → CONNECTING → ACTIVE → CLOSING → STANDBY
                └─ reject ────────────────→ STANDBY
                             failure ─────→ STANDBY / visible error

Summary generation runs separately after closure.
Robot tasks run independently across all voice states.
```

Track user speech, assistant speech/playback, pending backend work, and robot jobs separately. Use a monotonic clock for durations, UTC timestamps for persistence, and local calendar time only for memory selection.

### Default close policy

- Start/restart the 30-second idle interval from the end of genuine user speech or an explicit CLI interaction.
- While the user is speaking, never time out mid-utterance.
- Do not close while assistant speech is still being generated or played. GPT-Live has no WebSocket `output_audio.done` marker; use the application’s playback tracking and a conservative speech/queue quiet guard, not a nonexistent event.
- Assistant chatter and robot progress do not continually reset the user-idle clock. After speech or a completion announcement finishes, allow a short configurable reply grace, initially 5 seconds.
- Pending managed Responses tool continuations must settle before an ordinary idle close. Give backend calls deadlines so a stuck call cannot keep the session open indefinitely.
- Default `keep_open_while_task_running=true`, bounded by `task_hold_max_seconds=120`. Short tasks can finish and announce their result. Progress events and newly chained jobs must not endlessly renew a silent hold; anchor it to the current user-idle episode.
- Once the hold expires, if the user is still idle, close voice while the task continues. Record its eventual result and report it at the next wake. An optional completion chime is local; do not reopen a paid session automatically by default.
- Explicit “go to sleep” closes voice once required backend bookkeeping is safe; it does **not** mean “cancel my robot task.”

The user-requested behavior—remaining available while a policy runs—is the default for short jobs. The bounded exception makes long-running trajectories compatible with the cost-saving goal.

### Graceful close and races

Persist the transcript/task snapshot, stop admitting new session work, send `session.close`, receive final events through `session.closed`, save final usage, then release the connection. Apply a finite close timeout and mark finalization/usage unconfirmed if the terminal event never arrives. GPT-Live cancels queued Responses on closure, so do not close with an unresolved function-result continuation you intend to finish. [2, 4]

Keep microphone capture alive. Speech arriving before close is committed cancels an idle close; speech arriving after that boundary is buffered for a new session rather than sent into the closing one. Permit only one active paid voice session. Summary work must not delay returning to wake detection.

## 7. Tools are asynchronous jobs, not blocking motions

### Initial tool set

| Tool | Mock behavior |
|---|---|
| `navigate(destination)` | Validate a known room; simulate navigation progress and arrival. |
| `pick_up(object_description)` | Resolve against a small fake scene; simulate pickup or return ambiguity/not-found. |
| `run_policy(policy, parameters)` | Allowlisted `walk`, `jump`, or other configured mock policies; bounded typed arguments. |
| `set_expression(expression)` | Set an allowlisted mock expression. |
| `get_robot_state()` | Return location, capabilities, held object, active jobs, and timestamps. |
| `get_task_status(task_id)` | Return authoritative job state. |
| `steer_task(task_id, changes, expected_version)` | Apply supported, validated guidance; reject stale or unsupported edits. |
| `cancel_task(task_id)` | Request cancellation and report the actual cancellation acknowledgement. |
| `observe_scene()` | Return an explicitly mocked, timestamped scene—not a claimed camera observation. |

Expose a separate `sleep_voice()` application-control tool for “go to sleep.” It schedules closure through the supervisor after its tool result/continuation and any acknowledgement settle; it must not close the socket from inside the tool handler. It never cancels robot jobs.

### Job contract

Long operations return promptly:

```json
{"task_id":"task_123","status":"accepted","simulated":true}
```

The independent runner then emits `started`, bounded `progress`, and one terminal state: `succeeded`, `failed`, or `cancelled`. `accepted` is never described as “done.” A job can also be `cancelling`; cancellation is not complete until acknowledged.

Persist task state and tool-call identity before starting execution. Deduplicate repeated delivery of the same provider call. Add application operation identity for reconnect reconciliation; provider call IDs alone do not protect against a model generating a new call after reconnect. Never automatically replay an action whose outcome is unknown. Intentional repeated commands remain possible.

### Keep conversation alive during work

A mock job runs as an application-level asyncio task, not an awaited sleep in the audio receiver or a child task destroyed with the session. The tool returns its job ID immediately, allowing backend continuation and further questions/cancellations. On completion, persist the outcome first, then provide verified session context and a concise announcement if voice is active.

With managed Responses delegation:

1. Read completed function calls from nested `response.event` → `response.output_item.done`.
2. Validate and dispatch them through the tool gateway.
3. Return `function_call_output` with the matching `call_id` through `response.item.create`.
4. Supply all pending results, then send the Live `response.create` continuation.
5. Track `delegation_id`, backend response ID, call ID, and application task ID separately.

Do not infer calls from a backend completion event’s output array: the documented forwarded lifecycle snapshots omit those outputs. [4]

Later job completions are **new application events**, not a second output for the original accepted call. In an active managed session, send session-wide factual/context or commentary updates with `delegation_id: null`. Reconcile current job versions before announcing late events. On the next backend request, require fresh state for operational decisions rather than relying on an earlier acceptance response.

### Switching and guiding policies

- Ordinary conversation or an interruption of speech does not cancel movement.
- “Actually, go to the bedroom instead” cancels/preempts the navigation job and waits for its acknowledgement before starting conflicting motion.
- “Slower” modifies only supported parameters on the identified active job; ambiguity prompts a question.
- Resource locks prevent conflicting policies: for example, navigation and walking both require base control, while an expression may run concurrently.
- The gateway—not the LLM—enforces capabilities, argument limits, resources, and allowed transitions.

For future hardware, collision avoidance, watchdogs, emergency stop, and servo control stay local. A spoken stop is a convenience command, never the only safety stop. Robot disconnect behavior must be defined by the robot controller, not by whether the cloud voice socket is open.

## 8. Audio engineering: the main practical risk

Use `sounddevice`/PortAudio for device I/O and a stateful resampler when hardware rates differ. Start with raw mono PCM16 at 24 kHz for GPT-Live, and a 16 kHz branch for VAD/keyword detection. GPT-Live also documents 16 kHz PCM, which can be benchmarked as a simplification. Pin actual formats in the adapter. [3]

Audio callbacks only move samples into bounded buffers. They must not call the API, run inference, write SQLite, or block on playback. Keep capture, network receive, network send, playback, tools, and summarization independently scheduled.

**Laptop speakers feeding the microphone can make the robot talk to itself.** PortAudio does not automatically provide acoustic echo cancellation. Select and test an echo-cancellation path during the spike: a suitable OS echo-cancelled input or a maintained WebRTC-audio-processing integration, with the actual playback signal as reference.

- Distinguish speech detection, noise suppression, and echo cancellation; none substitutes for the others.
- Include chimes and assistant output in the echo reference.
- Track captured, queued, and actually played samples separately.
- GPT-Live manages full-duplex overlap. Do not copy Realtime-specific cancel/truncate events into it, or cancel robot work whenever a user starts speaking.
- A headset is useful for isolating API problems, but **headset-only success is not acceptance** of the requested laptop-speaker experience.
- A half-duplex fallback may mute mic forwarding during playback, but must be explicitly labeled as losing barge-in. It cannot be reported as full-duplex success.

WebRTC is OpenAI’s recommended Live audio transport, including server integrations. WebSocket is chosen here as a supported, inspectable Python CLI path. If echo handling/network behavior proves unacceptable, evaluate Python WebRTC as a transport change; do not add a web product just to hide an audio problem. [3]

## 9. SQLite persistence and daily memory

### Suggested tables

| Table | Key contents |
|---|---|
| `sessions` | Local ID, provider ID, UTC start/end, local timezone, model/config version, close reason, finalization status. |
| `transcript_fragments` | Session, speaker, provider event ID, exact text delta, session-relative times, capture-time mapping/provenance. |
| `events` | Ordered sanitized protocol/application events, timestamps, related task/call IDs, structured payload. |
| `tool_calls` | Provider call ID, session/delegation IDs, name, validated arguments, returned result/error, application operation ID. |
| `tasks` | Task ID, originating session, current state/version, resources, arguments, verified result, timestamps. |
| `summaries` | Session ID, local-date slice, source event range, summary JSON, model/version, creation timestamp, status. |
| `summary_jobs` | Durable pending/retry/completed jobs, attempts, source version, error metadata. |
| `usage` | Provider request/session identity, model, voice seconds or token usage, estimate/final status. |

Use migrations, foreign keys, WAL mode, and a serialized writer. Store all conversation text and tool arguments/results for debugging, but omit credentials and base64 audio from ordinary event logs. Raw audio recording is opt-in and separate. Support session export to JSON/JSONL and a readable transcript, plus deletion/retention controls.

GPT-Live provides transcript deltas for both speakers with approximate timeline offsets, not guaranteed complete turns or word-perfect transcription. Preserve original fragments exactly; derive readable utterances separately. Overlapping speakers must not be flattened into misleading sequential turns. A generated transcript is not proof all of its audio reached the speaker; record playback interruptions/gaps and avoid claiming precise word-level delivery. [2]

### Summarization after each session

1. Commit all received events and closure status.
2. Enqueue a durable summary job and return immediately to standby.
3. Ask Luna to summarize transcript evidence plus tool/job outcomes using structured output.
4. Store the validated summary with source references, timestamps, and version.
5. Retry transient failures with a cap. On a new wake before summary completion, use a small clearly labeled extractive fallback from saved conversation and verified task events.

Suggested summary fields:

```json
{
  "topics": ["Laundry cleanup"],
  "user_requests": ["Pick up the red sock"],
  "confirmed_outcomes": [{"task_id":"task_123","state":"succeeded"}],
  "unfinished_tasks": [],
  "relevant_facts": ["User said the laundry basket is in the bedroom"],
  "open_questions": [],
  "source_event_ids": ["evt_42","evt_57"]
}
```

Target roughly 150–250 tokens per summary. Preserve uncertainty and attribution; “user said” is different from “robot verified.” Never promote a tool request or acceptance to success. If a job finishes after voice closure, the historical summary can correctly say it was unfinished at that time; fresh task state overrides it at the next wake.

### Exactly what loads at a new wake

1. Resolve the laptop’s current local timezone, with an optional explicit IANA-zone override.
2. Compute **local midnight for the current calendar date**, then convert boundaries to UTC. Do not subtract a fixed 24 hours; daylight-saving days can differ.
3. Select the latest up to 10 relevant conversation summary slices for that local day, newest first for selection, then chronological order for injection.
4. For a session crossing midnight, create date-scoped summary slices from the corresponding transcript/tool events. Retain one full session transcript. Do not leak yesterday’s entire conversation through a summary merely because the session ended today.
5. Bound the memory prompt, initially around 2,500 tokens. Preserve recent requests and unfinished work first.
6. Add a fresh robot/task snapshot and unannounced terminal outcomes. These operational facts can cross midnight; current task state is not yesterday’s conversational memory.
7. Inject memory as explicitly labeled historical data, separate from trusted behavior instructions. Historical text must never become executable policy or tool authorization.

Keep the full database across days. **Automatic conversational recall resets at local midnight as requested**; this does not imply durable user preferences across days. Add opt-in long-term memory later if wanted. If the system timezone changes, regenerate date-scoped slices from UTC event timestamps for the new zone rather than trusting old date labels.

On application restart, recover summary jobs and close orphaned sessions as interrupted. Mark previously running mock jobs interrupted/unknown unless the adapter can verify their state. Do not pretend an in-process task kept running after the process died. Distinguish voice sleep from CLI exit: Ctrl+C gracefully closes voice and cancels/records in-process mock jobs before exit; a future external robot controller needs its own explicit shutdown policy.

## 10. Python implementation shape

```text
robot_voice/
  cli.py                    # run, devices, doctor, history, export, delete
  config.py                 # typed config; secrets from environment
  supervisor.py             # voice lifecycle and inactivity policy
  audio/
    frontend.py             # devices, echo processing, resampling
    buffers.py              # sample-indexed bounded buffers
    playback.py             # queue and played-sample accounting
  wake/
    base.py                 # WakeDetector protocol
    vad_transcribe.py       # default “Hey Spot” mode
    keyword.py              # optional local model
    semantic.py             # optional Decisions classifier
  voice/
    base.py                 # provider-neutral events and capabilities
    openai_live.py          # Live transport, transcripts, delegation
  tools/
    registry.py             # schemas and allowlist
    gateway.py              # validation, idempotency, resource checks
  robot/
    base.py                 # RobotAdapter contract
    mock.py                 # deterministic fake scene/policies
    tasks.py                # application-owned job lifecycle
  memory/
    store.py                # SQLite and migrations
    summaries.py            # background Responses worker
    daily_context.py        # timezone-aware retrieval
  tests/
    unit/
    protocol/
    integration/
    fixtures/
```

Use Python 3.11+ subject to audio/wake-wheel compatibility, `asyncio`, a pinned OpenAI SDK with Live support, `sounddevice`, `numpy`, a tested resampler, local VAD runtime, Pydantic, SQLite, and pytest. A small Rich/Typer CLI is optional. Pin SDK/model configuration after the spike rather than assuming old installed SDKs expose `client.live`.

Define plug points for `AudioFrontend`, `WakeDetector`, `VoiceProvider`, `RobotAdapter`, and `SummaryProvider`. Capability flags should expose differences such as image support, native full duplex, or buffered-audio handling. Implement one provider and one mock robot well before building a general plugin ecosystem.

Proposed CLI—not commands that exist yet:

```text
robot-voice doctor
robot-voice devices
robot-voice run --wake-mode vad-transcribe --wake-phrase "hey spot" --idle-timeout 30
robot-voice run --wake-mode keyboard
robot-voice history --today
robot-voice export --session <id> --format jsonl
```

The only cloud credential is `OPENAI_API_KEY`, read from the environment or an appropriately protected local secret mechanism. Never log it. Add visible cloud-audio/mute status, per-session usage, a maximum session duration, and an application budget cutoff to prevent TV speech or false wakes from creating unbounded charges.

## 11. Cost model

Current published GPT-Live pricing is **$0.05 per active minute, billed per second**, plus backend/tool usage. Silence and time waiting on backend work still count. Muting does not stop billing. This is distinct from the older token-priced Realtime conversation model. [7]

Illustrative voice-only costs at that rate:

| Usage | Cost |
|---|---:|
| 20 connected minutes/day | $1.00/day |
| 1 connected hour | $3.00 |
| 16 connected hours/day | $48.00/day |
| 24 connected hours/day | $72.00/day |
| One 30-second idle tail | $0.025 |
| Twenty such idle tails/day | $0.50/day |

The idle-tail examples are already part of connected time, not an extra charge on top of it.

At the published `gpt-transcribe` estimate of $0.0045/minute, 60 minutes of submitted standby clips is approximately $0.27. Measure actual submitted duration including padding, overlaps, and false detections. Local keyword mode avoids that cloud standby transcription charge but still uses local compute/power. [5, 7]

Total cost is:

```text
Live voice duration
+ wake transcription
+ optional Decisions calls
+ delegated backend inference
+ summaries
+ retries and any future vision/tool charges
```

Save the latest cumulative `session.usage.updated` value, not their sum; finalize from `session.closed`. Deduplicate backend usage by response ID. If closure is unconfirmed, report an estimate, not a final bill. If WebRTC is adopted, account for its documented 15-second initialization charge/credit correctly rather than adding it twice. [7]

## 12. Delivery sequence and acceptance gates

### Phase 0 — De-risk the API and laptop audio

- Verify model/project access and a pinned Python SDK with Live support.
- Connect using a manual wake, stream microphone audio, play replies, collect both transcripts, and close gracefully with usage.
- Exercise a single managed function call through result and continuation.
- Compare paced pre-roll versus text handoff under injected startup delay.
- Establish an echo-control path using the actual laptop microphone and speakers.

**Gate:** no lost opening command, no duplicate tool execution, and no persistent self-conversation. Measure latency; do not substitute documentation assumptions for these tests.

### Phase 1 — Complete the mock interaction loop

- Implement supervisor, bounded queues, CLI diagnostics, and task runner.
- Add navigation, pickup, expression, state, cancellation, and guidance tools.
- Demonstrate chatting during a 10-second task and switching a running task.
- Add the 30-second idle close and bounded task hold.

**Gate:** voice can close without cancelling a long mock task; short task completion is announced from a verified terminal event.

### Phase 2 — Wake, persistence, and same-day coherence

- Add default VAD/transcription phrase detection and first-utterance handoff.
- Implement SQLite event/transcript logging and summary worker.
- Restore up to 10 same-day summaries plus current task state.
- Handle summary failures, rapid re-wake, network loss, and application restart.

**Gate:** the user can run the complete wake → task → chat → timeout → wake → recall demo from the CLI.

### Phase 3 — Optimize standby and harden

- Add local keyword mode; evaluate or train “Hey Spot.”
- Measure false accepts, missed wakes, echo-induced wakes, latency, CPU, and daily cost.
- Add optional Decisions addressing only if evidence shows a benefit.
- Finish privacy/retention controls and packaging on the supported laptop OS.

**Gate:** local-wake standby sends no audio to cloud, and the supported microphone/speaker configuration passes the duplex test.

For one experienced engineer, budget roughly **5–10 focused engineering days for a testable MVP**, with additional time for robust cross-platform echo cancellation and a custom wake model. Treat this as an estimate conditional on Phase 0, not a delivery promise.

## 13. Test matrix

| Scenario | Required outcome |
|---|---|
| Wake phrase + command in one breath | Full request preserved; one tool invocation. |
| User speaks while connecting | Speech retained in order; overflow is visible, never silently ignored. |
| Unrelated conversation/background noise | No action; log wake metrics without retaining unrelated content by default. |
| Robot audio/chime reaches its mic | No self-trigger or self-conversation. |
| User overlaps assistant speech | Conversation remains usable; existing robot job is not cancelled accidentally. |
| Ask a question during navigation | Spoken reply while the same job continues. |
| Change destination | Old conflicting job acknowledges cancellation before replacement starts. |
| Steer unsupported parameter | Explicit rejection/clarification, no invented capability. |
| Mock failure/object ambiguity | No success claim; ask for clarification or report failure. |
| 30 seconds idle | Close around the deadline when no speech/work guard applies; return to wake detection. |
| Short task crosses idle deadline | Stay connected within hold budget and announce its result. |
| Long task outlives hold budget | Voice closes; task continues; next wake receives accurate state. |
| New speech races with close | Existing session continues or a new one gets the preserved speech; no double session. |
| Repeated provider call/reconnect | No duplicate execution; unknown outcomes are reconciled. |
| Summarizer fails or user wakes immediately | Available history remains usable; wake is not blocked. |
| More than 10 conversations today | Only the latest 10 selected, injected chronologically within budget. |
| Midnight, DST, or timezone change | Correct local-day memory boundaries; live task state remains available. |
| Process crash/network drop | Durable records retained; usage uncertainty and interrupted jobs labeled honestly. |

Use fake audio clocks, prerecorded fixtures, synthetic protocol events, and deterministic mock tasks for most tests. Real microphone/speaker and paid-API tests are separate integration checks.

Instrument wake detection time, transcription time, connection-ready time, first audible useful reply, tool acceptance/completion, playback underruns, buffer overflows, missed/false wakes, idle-close timing, and cost per successful interaction. Initial targets: local acknowledgement within 200 ms of an accepted wake; no lost speech in the fixture corpus; idle close within roughly one second of eligibility. Network/model response latency is measured separately and is not guaranteed.

## 14. Later robot integration

Replace `MockRobotAdapter` with a ROS 2 action client or another robot-specific transport without changing the wake/memory/voice modules. Advertise only installed policies and their real parameter schemas.

For vision, add an on-demand `observe_scene` backend that captures a timestamped frame and uses a vision-capable model. Return observations, uncertainty, object references, and age. Revalidate a target before manipulation; an old caption is not a live world model. Do not stream video continuously just to answer occasional questions.

For deeper control, add capability-specific guidance and a robot-owned arbiter. Never expose arbitrary joint commands, unrestricted shell access, or policy code execution directly to the conversational model.

**Bottom line:** the prototype should prove a conversational control layer with cheap standby, independent jobs, and durable same-day memory. It does not need a new robotics foundation model to be useful.

## Sources

1. [GPT-Live overview](https://developers.openai.com/api/docs/guides/live) — full-duplex voice/backend separation.
2. [Managing GPT-Live sessions](https://developers.openai.com/api/docs/guides/live-conversations) — history, transcript semantics, idle closure, buffering, finalization.
3. [GPT-Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live) — Python connection, PCM formats, pacing, event names.
4. [GPT-Live delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation) — managed/client delegation, function result continuation, context updates.
5. [OpenAI file transcription](https://developers.openai.com/api/docs/guides/speech-to-text) — current transcription model and deprecations.
6. [Decisions API](https://developers.openai.com/api/docs/guides/decisions) and [voice integration](https://developers.openai.com/api/docs/guides/decisions-voice) — typed addressing decisions and supported inputs.
7. [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and [voice cost accounting](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live) — duration billing, backend costs, cumulative usage, initialization accounting.
8. [openWakeWord](https://github.com/dscripka/openWakeWord) — local keyword detection, pretrained phrases, VAD filtering, model licensing.
9. [Silero VAD](https://github.com/snakers4/silero-vad) — local speech activity detection.
10. [GPT-Live 1 model](https://developers.openai.com/api/docs/models/gpt-live-1) — audio/text modalities and pricing.
