# Sanitized verification — 2026-11-04

This document contains results, not conversation logs, credentials, audio, provider
identifiers or operator-specific paths. All cloud audio was synthesized. **No room
microphone, physical speaker, AEC device or robot was tested.**

## Final checks

Python 3.12.16; OpenAI SDK 3.45.0; Linux.

| Check | Result |
| --- | --- |
| `python -m pytest -q` | Final lifecycle and request-identity fixes: **143 passed**, approximately 12 seconds; no network/payment/hardware required. Earlier lifecycle result was 129 passed; first review result was 116 passed; pre-review result was 80 passed. |
| `ruff check robot_voice tests` | Passed |
| `ruff format --check robot_voice tests` | Passed |
| `mypy robot_voice` | Passed: 30 source files |
| `python -m build` | Built wheel and source distribution |
| Installed `robot-voice --help`, offline demo/history/export/delete | Passed |
| `robot-voice doctor` | **Historical expected failure**: system PortAudio and optional Silero runtime were absent during the initial campaign; hardware/AEC not verified |
| Production Silero software check after installation | Parent confirmed, then independently repeated during review fixes: three synthetic fixtures segmented successfully; 40 silence frames produced zero detections. This is **not** microphone, speaker, AEC or room-wake acceptance. |

Coverage includes explicit phrase boundaries, whole-command preservation, 0.5s and
3s startup delays, exact sample cutoffs, bounded queues, speech during transcription,
speech racing with closure, sample continuity, stateful resampling, paced-send drift
and stalls, continuous silent output, nested function events, result continuation,
duplicate calls/operations, invalid arguments, resource conflicts, versioned guidance,
cancellation acknowledgement, independent long jobs, timeout/backend guards,
crash recovery, usage deduplication, summary retries/cancellation, midnight/DST/zone
changes, latest-ten memory, redaction, export/delete and runtime locking.

## Review-fix revision and evidence boundary

Independent review found additional safety/lifecycle faults despite the original
80 passing tests. Added regression cases reproduced 32 failures on the pre-fix
source. The corrected source rejects incomplete capture after wake-check overflow,
closes tool admission synchronously (including queued/cancelled-response work),
records no voice usage for never-started sessions, and keeps independent jobs alive
through expected transcription/network failures. Operation arguments are finite JSON
with recursive key-order-independent identity; known resource conflicts can be
rechecked with a new call. Session closure/summary enqueue is atomic, recovery
backfills missing jobs, and exhausted interrupted summary attempts become failed.
Dictionary keys are redacted; fragmented credentials remain an explicitly documented
limitation, not an absolute credential-stripping claim.

The 632-second soak and its next-wake recall below are **historical pre-review
results after the pacing fix**, not proof that every later review fix underwent
another ten-minute soak. Pacing code is unchanged by this review-fix revision.
The first review fixes have dedicated offline race, non-finite-value, recovery and real
subprocess-crash regressions. At that revision, the required gateway/protocol/memory subset passed
**66 tests**; the complete suite passed **116 tests**. A single authorized post-review
cloud smoke subsequently passed: **19 finalized voice seconds**, one successful
pickup, a status query, ordinary idle closure and one completed summary, with no
protocol errors. It used six cloud requests and cached synthetic PCM (no new TTS),
adding approximately **$0.01684** and taking **31.61 seconds wall time**. No paid
retry loop or repeat long soak was run. Private source/test SHA-256 manifests tie
this smoke to that review-fix code rather than the older soak.

### Final lifecycle fixes — offline evidence only

A subsequent independent recheck found two remaining lifecycle defects. Forced
closure could promote an interrupted command suffix into a new session, and voice
worker cancellation could abandon an already-admitted robot cancellation. Five
new regression cases initially failed on the prior source. An additional stalled-
capture case confirmed that elapsed idle time alone does not establish an utterance
boundary.

The final revision carries a persistent close/discard boundary across capture awaits,
checks captured silence before allowing idle-close continuation, clears affected
wake/pre-roll state and visibly discards through clean silence. Duration/error cases
cover brief VAD pauses and capture missing the transient CLOSING state, then verify
a successful fresh wake. Genuine new speech after a safe idle close remains covered.
Admitted cancellation is shielded through acknowledgement and durable operation/tool
results before propagating caller cancellation. Admission is rechecked before the
shielded dispatch starts; repeated close cancellation cannot abandon settlement.

At that lifecycle revision, the complete suite passed **129 tests**, including deterministic production
LiveVoice/Gateway/TaskRunner/MockRobot close-at-cancellation-yield coverage. Lint,
format, type checks and distribution builds passed. **No paid calls were made for
this final revision.** The preceding 19-second smoke and its source manifest describe
the earlier review revision, not this final code. Hardware and real-robot limitations
are unchanged; the campaign totals below are unchanged.

### Request-identity fix — offline evidence only

The next independent review identified a duplicate-action path: one 32ms false VAD
frame rotated the operation identity inside the same utterance. An offline production
reproduction completed a short mock policy, inserted that brief pause, then delivered
identical arguments with different response/call IDs. It created two tasks.

The corrected supervisor requires 0.6 seconds of contiguous captured silence before
rotating an established utterance identity. Missing samples do not count as silence.
LiveVoice binds delegation and response identities at their origin events, retaining
them for delayed function items and continuations across newer speech. A genuinely
new utterance with a new delegation can still intentionally repeat a completed action.
Fourteen additional regression cases cover brief pauses, different provider call and
delegation IDs, delayed backend events, intentional repetition and capture gaps.
Follow-up review reproduced two late-correlation variants that each created two tasks:
an initially uncorrelated response acquired its delegation only after newer speech.
The binding now inherits either already-known origin before consulting current speech;
conflicting established identities fail closed rather than guessing. Three further
regressions failed before this correction and now pass.
The complete suite passes **143 tests**, with lint/format/type checks and distribution
builds passing. The built wheel also installs and runs its offline demo in a fresh
Python environment outside the source tree, with no API key or cloud requests.
This revision used no paid calls; the historical cloud evidence above and campaign
totals below are unchanged.

## Paid campaign: successes and failures are both counted

The authorization was initially 20 aggregate active minutes and was explicitly
extended to **40 minutes** for a continuous post-fix soak. The local estimated
cutoff remained **$4.50**, with a $5 total-cloud authorization. No background or
unbounded paid retry loop was used.

| Run | Sessions | Active voice seconds | Outcome |
| --- | ---: | ---: | --- |
| Initial schema probe | 1 | 9.676 estimated | Failed: transcript INSERT column mismatch; fixed. Final usage unconfirmed. |
| Delegation correlation probe | 1 | 61.674 estimated | Failed: granular function events omitted response ID; fixed by delegation-to-response mapping. Final usage unconfirmed. |
| Initial corrected tool smoke, 3s startup delay | 1 | 47 confirmed | One pickup succeeded, continuation worked, summary completed; exposed over-conservative silence/playback idle guard. |
| First multi-cycle soak | 4 | 585 confirmed | **Failed acceptance**: bounded input queues overflowed after accumulated send overhead; summary output cap also exhausted. Real tools and fallback recall worked, but this was not a passing soak. |
| Corrected multi-cycle regression | 3 | 381 confirmed | All ordinary idle closes, all final usages confirmed, all three summaries completed on first attempt; no protocol errors. |
| Final short smoke, 0.5s startup delay | 1 | 19 confirmed | Passed: one pickup, idle close and summary. |
| Completed-summary/fallback recall check | 1 | 19 confirmed | Correct recall with two completed summaries plus one appropriately stale extractive fallback. An overly strict harness assertion expecting three current summaries was corrected against saved evidence, without a paid rerun. |
| **Continuous post-fix soak** | **1** | **632 confirmed (10m32s)** | **Passed**: ordinary idle close, one successful opening pickup, completed summary, no protocol/backend errors or buffer overflow. |
| **Next wake after continuous soak** | **1** | **19 confirmed** | **Passed**: loaded the completed summary and correctly recalled the synthetic basket-location fact; ordinary idle close, confirmed usage. |
| Post-review-fix short smoke | 1 | 19 confirmed | Passed: one pickup, status query, ordinary idle close and completed summary; zero protocol errors. |
| **Total** | **15** | **1,792.350 (29.87 minutes)** | Includes all diagnostic failures and the single post-review smoke. |

Provider-finalized voice duration totals **1,721 seconds**; the remaining
**71.350 seconds** are wall-time estimates from the two unconfirmed early probes.
The continuous soak took **644.60 seconds wall time**, including startup,
transcription, closure and summary work. It used a 650-second requested fixture
schedule, a 662-second hard session limit, and a 22-second idle deadline refreshed
by periodic synthetic user interactions (not an artificially idle open socket).
No buffer overflow occurred. Queue-depth trend/high-water telemetry was not saved,
so this is not a claim of measured zero backlog growth at every instant; the
separate 5,000-frame latency regression verifies the corrected pacing arithmetic.
Its following recall session took
**24.74 seconds wall time** excluding fixture transcription.

The corrected three-cycle run exercised actual model-selected pickup, a 240-second
walking job surviving voice timeout, fresh-state/version checks, speed guidance,
acknowledged cancellation and subsequent navigation. All those side-effect tool
results were successful. Assistant transcript fragments arrived while each job was
running. The opening pickup executed exactly once, despite a 3-second startup delay
and additional synthetic speech arriving while connecting. Same-day recall worked
both with pending/stale fallback and with completed summaries.

The continuous session received **244 user and 502 assistant transcript fragments**.
Those are fragments, **not turns**, and the output sink deliberately discarded PCM:
these counts do not demonstrate audible delivery or hardware duplex behavior.

### Explicit scenario acceptance audit

An empty `errors` list in an **in-progress** report is not a pass. In particular,
the first 147-second cycle of the original soak already failed its expected closure:
`audio_overflow_repeat_required` was not an injected fault and was never accepted as
successful merely because it accumulated voice time. The finalized original report
contains the overall acceptance exception.

| Scenario | Expected closure | Actual closure | Task/memory assertions | Verdict |
| --- | --- | --- | --- | --- |
| Original four-cycle soak | `idle_timeout` on all four | `audio_overflow_repeat_required` on all four | Pickup and task survival/steer/cancel-before-navigation succeeded; all four summaries failed after three attempts | **FAIL** |
| Corrected three-cycle regression | `idle_timeout` on all three | `idle_timeout` on all three | One successful pickup; long job alive after voice close; successful steering; persisted cancellation acknowledgement precedes navigation acceptance; all three summaries completed first attempt | **PASS** |
| Continuous post-fix soak | `idle_timeout`, at least 600 confirmed seconds | `idle_timeout`, 632 confirmed seconds | Exactly one successful opening pickup; summary completed first attempt; no protocol errors | **PASS** |
| Following summary-recall wake | `idle_timeout` | `idle_timeout` | Completed summary actually injected; returned model transcript contains expected location; final usage confirmed | **PASS** |
| Post-review-fix short smoke | `idle_timeout` | `idle_timeout` | One successful pickup and status query; completed summary; final usage confirmed; no protocol errors | **PASS** |

The standalone recall harness deliberately does not start a new summary worker;
its own closure leaves a durable pending summary job, as expected. It verifies
recall from the preceding completed summary, not generation of a second summary.
This audit was derived from saved SQLite events/results and reports with **zero
additional cloud requests**. Cancellation ordering is checked using persisted event
sequence numbers, not inferred from model narration. No separate event-loop-stall
root cause was established; the demonstrated backlog-growth cause was accumulated
per-frame transport overhead. The fixes and later tests address that cause without
claiming every possible network/event-loop stall is eliminated.

### Fixes demonstrated by the regression

- Function calls are read from nested granular events, never lifecycle output arrays.
- WebSocket send time is included in the pacing interval, rather than added to every
  32ms frame. Whole-frame stalls rebase the clock instead of causing a catch-up burst.
- Live output includes continuous silence; silence does not hold the session open.
- Buffered user speech must drain before ordinary idle closure.
- Summarization compacts fragments into bounded complete evidence records, limits
  citations, and uses a larger bounded output allocation with Luna reasoning disabled.
  Two rejected configuration probes established that `minimal` is unsupported for
  this model; the supported `none` setting was then validated.

## Usage and estimates, not an invoice

Deduplicated provider records across the campaign:

- GPT-Live: **15 sessions**, 1,721 finalized seconds plus 71.350 estimated seconds.
- Luna: **104 Responses results**, **418,581 input tokens** and **17,099 output tokens**
  (output includes reasoning). Input totals include cached/cache-write categories.
- Transcription: **15 requests**, **68.4 submitted seconds**, including clip padding.
- Fixture TTS: **19 requests**; cached PCM reused thereafter. Binary responses did
  not expose token usage to this harness.
- Two rejected summary-configuration requests returned no usage.

The updated cache-aware estimate is **$1.549**, including all known retries, failed
probes, backend/summary usage and a **$0.0243 fixture-TTS duration estimate**.
Rates used: Live $0.05/minute; transcription $0.0045/minute; standard Luna $0.10/M
ordinary input, $0.01/M cached input, $0.125/M cache writes and $0.50/M output.
TTS uses a $0.02/minute estimate, **not a published duration price**; its published
rates are $0.60/M text input and $12/M audio output tokens.

Provider usage events confirm quantities, **not final dollar billing**. The account
invoice was not available. Early per-run reports used simpler input-token estimates;
the aggregate estimate above recomputes cached/cache-write usage and deduplicates
copied recall databases. Unknown finalization and TTS token usage remain uncertainty,
not fabricated confirmation. The campaign stayed below its time and estimated-cost
limits; local guards cannot guarantee a provider-enforced billing ceiling.

## Reproduction

Install `.[dev]`, provide `.env` privately, and use a fresh ignored directory per run.
No audio hardware or Silero runtime is needed for these synthetic commands.

```sh
python -m pytest -q
ruff check robot_voice tests
ruff format --check robot_voice tests
mypy robot_voice
python -m build

robot-voice live-test --allow-paid --seconds 35 --cycles 1 \
  --startup-delay .5 --output .robot-voice/smoke
robot-voice live-test --allow-paid --seconds 135 --cycles 3 \
  --startup-delay 3 --output .robot-voice/multi-cycle
# Linux foreground wall-time guard; no detached spend:
timeout --signal=INT 800s robot-voice live-test --allow-paid \
  --seconds 650 --cycles 1 --startup-delay 3 --output .robot-voice/continuous
```

The next-wake check used the production supervisor/provider with paced synthetic
silence, an OpenAI-transcribed synthetic recall question and a private SQLite backup
of the completed soak. It asserted that the injected context was a completed summary
and that the assistant's returned transcript contained the expected location. The
instrumented replay script, PCM, SQLite evidence and reports are private test
artifacts, not distributed source. On configured hardware, the analogous check is
`robot-voice --database .robot-voice/continuous/test.sqlite3 run --wake-mode keyboard`,
then ask about today's basket location after the summary completes.

## Genuine limitations

- No physical microphone/speaker, AEC, barge-in, real-room wake or latency acceptance.
  The Silero and optional user-supplied keyword adapters were not hardware-tested.
- No real robot/controller safety claim. Mock speed guidance changes reported mock
  parameters, not a physical velocity; task durations remain bounded simulation time.
- Wake transcription and long-clip overlap stitching are approximate. False wakes
  from nearby speech/TV are possible; there is no speaker authentication.
- Playback speech/quiet guards are heuristic. Transcript fragments and sample counters
  cannot prove word-level audible delivery.
- The network and provider behavior is nondeterministic; these results are one
  campaign, not a reliability guarantee. Hardware gates in the design remain open.
- Optional local keyword models, WebRTC, custom wake training, vision and physical
  robot integration are not validated. No custom “Hey Spot” model ships.
- Redaction is best effort per dictionary key/string value; credentials split across
  transcript fragments can survive. Logs and exports remain private. Dependencies
  and build tools are not fully locked; exact environment reproduction is unsupported.
