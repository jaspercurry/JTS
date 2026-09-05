# JTS voice loop — audit and cleanup program (handoff for the orchestrating agent)

This is the working brief for getting the smart-speaker voice loop — wake
word → endpointing → realtime-LLM turn → tools → TTS and cues, plus the
mic/AEC input side and the provider adapters — into shape. It is a work queue
with an execution ledger, not doctrine: decisions it produces go to
`docs/adr/`; delete this file and its directory when the ledger is done.

Evidence: eight read-only agent audits run 2026-09-05 against `main` at
`8777cff19`, in [`voice-audit-2026-09-05/`](voice-audit-2026-09-05/):
`report-latency`, `-voice-daemon`, `-providers`, `-tools-cues-playback`,
`-input-side`, `-rust-tts-playout`, `-tests`, and `-research-best-practices`
(web sources, cited). Every code finding there cites `file:line` at that SHA;
re-derive before acting — the line numbers age fast in this repo.

Out of scope: the renderer/mux/volume stack, the speaker tuning program, the
web wizards. They were touched only where the voice loop calls into them.

Coordination: the voice loop sits inside the general codebase steward's
territory ([#4030](https://github.com/jaspercurry/JTS/issues/4030), queue in
[#4085](https://github.com/jaspercurry/JTS/issues/4085)). Since the audit SHA
that program landed the measurement-hold extraction
([#4104](https://github.com/jaspercurry/JTS/pull/4104) →
`jasper/voice/measurement_hold.py`), which is this brief's Wave 4.1; its queue
items 2 (`TtsPlayout` collapse) and 3 (Rust daemon skeleton) touch this loop's
edges and are not repeated here. It dropped a provider base class pending a
re-scout; `report-providers` §2 is that re-scout (≈511 identical lines), so
Wave 5 stands. Whether this brief runs as its own orchestrator or as lanes of
the steward is owner decision 5 in §6.

## 0. How the orchestrator works

Same method as [`UX-AUDIT-2026-09-03.md`](UX-AUDIT-2026-09-03.md) §0 — read
it; it is not restated here. Voice-specific additions:

- **Measure before tuning.** Wave 0 lands the per-turn timeline first. No
  endpointing or capture constant changes until the timeline has been read
  on hardware, and none of the ADR-0152 constants change without the same
  five-utterance A/B protocol that set them. A change to those constants
  supersedes ADR-0152; it does not edit it.
- **The hot paths are `_handle_wake_frame`, `_handle_session_frame` and
  `_play_responses`.** A PR that adds an `await` on any of them needs a
  sentence in its description saying what the await waits on and its bound.
- **Cue paths are non-negotiable 6.** A PR touching a refusal or failure path
  adds or keeps the cue and its behavior pin, and says so.
- **One concern per PR, file smaller on exit, tests move with their subject.**
  The tests report (§7 there) says which tests go where.

## 1. Verdict

**The architecture is right and the hard parts are done well.** Local Silero
endpointing on the AEC stream (ADR-0152), the hubless wake arbitration that
costs a solo box nothing (ADR-0127), the acquire buffer that means a slow
provider never clips the command, a playout path with no prebuffer, no
loudness lookahead and no fade-in, a cue system that is pre-rendered and
content-addressed, a tool registry with bounded timeouts and error fencing,
a reconnect supervisor that is genuinely shared, and a wake-events funnel with
millisecond stage stamps. None of that needs redesign.

**What is wrong is concentrated, and most of it is subtraction:**

1. **One god file.** `jasper/voice_daemon.py` was 5,476 lines at the audit
   SHA (4,905 at `5fc3782f3`, after #4104 moved the measurement hold out);
   `WakeLoop` had 96 methods and 102 state fields and a 0.45 prose-to-code
   ratio. The concerns inside it are loosely coupled (cue/output episodes
   500 lines, research announce 360, push-to-talk 349, peering 94) and the
   true wake→turn→end core is about 800 lines of code. A 185-line test-only
   constructor (`for_tests`) ships to the Pi.
2. **Two provider adapters that copy each other.** About 511 lines of
   `gemini_session.py` are byte-identical (comments stripped) to
   `openai_session.py`; the code says "mirrors the other adapter" fourteen
   times. The `LiveTurn` Protocol is enforced nowhere: both adapters sit on
   the mypy ignore baseline and nothing calls `isinstance`. Six of its
   members exist only for server VAD, which ADR-0152 rules off.
3. **Prose.** 0.45 (`voice_daemon.py`), 0.62 (`session.py`), 0.40
   (`gemini_session.py`). Dated anecdotes, phase numbers, design essays, and
   two comments that contradict the code they annotate.
4. **Telemetry on the event loop at the three moments that matter.** A SQLite
   `INSERT` on wake fire, a SQLite `UPDATE` gating the first TTS chunk, and
   four SQLite writes plus a peering round-trip before the end-of-turn chirp.
5. **The one number that looks like provider latency is wrong.** "first audio
   chunk from OpenAI in Nms" is anchored at turn open, so it contains the
   user's whole utterance plus ~1 s of local endpointing. The owner's belief
   that the cloud is fast is probably right; the log cannot show it.
6. **Three real resilience holes.** A push-to-talk press at the spend cap or
   on an acquire error produces silence and no journal line. A Gemini box
   whose link is down at boot exits after one attempt and can walk
   `StartLimitBurst` into `StartLimitAction=reboot`. The daemon opens the
   provider connection before the mic and the cue player exist, so a boot
   outage is a deaf and mute window.

Everything below is sized so that the whole program removes roughly 3,500
lines from the voice loop and adds under 500.

## 2. Latency: what the code says

Typical figures derived from constants at HEAD, default profile (XVF3800 →
`jasper-aec-bridge` → UDP → `jasper-voice` → fan-in → CamillaDSP → outputd →
USB DAC), peering and barge-in off. Details and min/max in `report-latency`.

| Stage | typ | where the time goes |
|---|---|---|
| mic ADC → `_handle_wake_frame` | ~90 ms | PortAudio `latency='high'` (unset knob, est. 35 ms), 20 ms capture block, 80 ms UDP packetization (0–60 ms) |
| wake fire → chirp bytes at fan-in | ~14 ms | one CamillaDSP websocket round-trip before the chirp task exists; tail bounded at 5 s |
| wake fire → first audio at provider (warm) | ~35 ms | pre-opened session; two more Camilla round-trips, a SQLite insert, a duck UDS call |
| end of speech → `end_input` on the wire | ~1,000 ms | 800 ms constant + 80 ms the silence clock never counts (anchored at frame arrival) + 40 ms frame granularity + capture |
| provider first chunk → speaker | ~28 ms | 100 ms-capped SQLite write before the first `write_segment`; then ring + CamillaDSP + DAC, already at the floor |
| barge-in onset → speaker silent (off by default) | ~315 ms | 240 ms sustained-speech gate at 80 ms granularity |

The user-perceived numbers: wake-to-chirp ≈ 150–250 ms from the end of the
wake word (openWakeWord's own window delay dominates; Nielsen's "instant"
band is ~100 ms, so this is fine); end-of-speech to answer ≈ 1 s of ours plus
whatever the provider takes, and today nobody can say what the provider
takes.

**Ranked changes.** ms is per turn; Cx S/M/L; the last column is the gate.

| # | change | ms | Cx | gate / conflict |
|---|---|---|---|---|
| L1 | Per-turn timeline: stamp `wake_fire`, `cue`, `first_audio_to_provider`, `speech_end`, `end_input`, `first_response_chunk`; emit one `event=turn.timeline`; put the last turn's deltas in `/state.voice`. Re-anchor the adapters' first-chunk log on `end_input`. | 0 (it is the ruler) | S | none — do first |
| L2 | Wake-event and usage SQLite writes off the loop: `begin_event` fire-and-forget; `update_stage`/`set_outcome`/`open_session` via one writer task or `to_thread`, as `attach_audio` already does. | 1–100 typ on wake and on first TTS chunk; SD-card tail | S | `tests/test_turn_playback_barge_in.py:172` pins the current ordering — change it deliberately |
| L3 | One CamillaDSP round-trip per turn: cache `effective_volume_context()` and refresh on mutation; drop `content_activity.refresh_now()` from turn open (the poller runs at 1 Hz); call `_prepare_assistant_loudness_context()` once, not twice. | 5–15 typ, up to 5 s tail | M | none |
| L4 | Anchor the end-of-utterance silence clock at the silent frame's start, not its arrival. The 800 ms constant does not change. | 80 | S | ADR-0152 spirit: verify with `scripts/probe-wake-gate.py` on the corpus |
| L5 | End-of-turn chirp before the telemetry, peering and conversation writes. | up to 2.5 s worst case of dead air | S | none |
| L6 | Dispatch a round's tool calls concurrently, not serially. | 0 unless two tools; then the slower one's whole latency | S | none |
| L7 | `JASPER_AEC_CAPTURE_LATENCY=low`. The knob exists; `event=aec.mic_stream_latency` reports the result and the bridge watchdog covers xruns. | 20–30 on every stage | S | **hardware** — one Pi, read xrun counters for a day |
| L8 | `END_OF_UTTERANCE_SILENCE_SEC` 0.8 → 0.6, or adaptive after a complete-sounding phrase. LiveKit ships ~0.55 s; Silero's own default is 0.1 s. | 200 | S / L | **owner + hardware** — the ADR-0152 A/B protocol, supersede the ADR |
| L9 | 20 ms UDP transport for the primary leg with wake framing decoupled from VAD framing (the `usb_host_mic` leg already emits 320-sample packets). | 0–60, avg 30 | M | ADR-0152 constants were swept at 80 ms; re-sweep |
| L10 | Ordinary sound graph onto the certified ring pair (chunk 128 / target 128). | ~25 on cue, TTS and barge-in | M | **non-negotiable tier**: DSP on the output path, listening test |

**Rejected — the code already handles it:** a TTS prebuffer (there is none), the
1.2 s pace-ahead (cannot fire on the first chunk), pre-warming the provider
(the session is persistent and `await_connected` does not yield when
connected), playing the cue before any await (it is already fire-and-forget;
the two awaits ahead of it are L3's target), env reads on the wake path
(nine dict lookups), sub-80 ms wake frames (openWakeWord's window is fixed),
Silero at 32 ms without L9, microWakeWord (a microcontroller tool), and the
LiveKit turn detector (too heavy for 1 GB).

## 3. Target architecture

### 3.1 Package layout after the program

```
jasper/voice/
  daemon_main.py        ~600   table-driven construction; one AsyncExitStack owns every lifecycle
  control_socket.py     ~100   the UDS protocol server, lifted out of daemon_main
  wake_loop.py         ~1400   WakeLoop core: legs, OR-gate, acquire/drain, turn open/end, session_status
  wake_telemetry.py     ~260   WakeFunnel: on_fire/stage/outcome/audio rings; the only SQLite seam; never awaited on a frame path
  assistant_output.py   ~590   cues, chirps, dynamic text, output episodes; FanInDucker converges with camilla.Ducker here
  measurement_hold.py   ~610   landed (#4104): pause/pause_response/resume for the measurement window
  research_announcer.py ~370   announce/confirm state machine over a small TurnHost protocol
  push_to_talk.py       ~370   manual mic set, hold cap, keepalive, manual endpointer
  peering_client.py      ~95   arbitrate/session_started/session_ended
  session.py            ~180   the contract: LiveTurn (13), Interruptible (5), LiveConnection (9), TurnUsage, TurnCapture
  _base.py              ~250   BaseLiveTurn + BaseLiveConnection: queues, counters, state machine, one initial-connect loop, one reconnect watchdog, one receive-loop exit, one event=voice.turn line
  _supervisor.py        ~600   unchanged in spirit; gains close_code_and_reason
  openai_session.py    ~1200   wire only
  gemini_session.py     ~700   wire only
  grok_session.py        ~110  already the model
  turn_playback.py, output_gate.py, input_policy.py, input_presence.py, earcons.py, catalog.py, provider_state.py, model_discovery.py, prompt.py — as they are (prompt.py loses its history to ADR-0158)
```

`jasper/voice_daemon.py` becomes the `WakeLoop` core and moves into the
package; the `jasper-voice` console entry points at
`jasper.voice.daemon_main:main` if it does not already. `trace.py` and
`submit_recorded_audio` move to `tests/voice_eval/` (one production caller
between them). `endpointer.py` (the three endpointing branches as a strategy)
is deliberately **not** in this layout: it is the only extraction that
touches the hot path and needs a `TurnState` object first. Do it last or not
at all.

### 3.2 Contracts

- **`LiveTurn`** keeps `send_audio`, `send_text_context`, `end_input`,
  `audio_out_chunks`, `release`, `last_activity_at`, `last_chunk_at`,
  `bytes_sent`, `chunks_received`, `usage() -> TurnUsage`, `turn_lost`,
  `server_turn_complete`, `wait_for_interrupt`/`clear_interrupted`, plus
  `capture() -> TurnCapture | None` (transcript or metadata, whichever the
  provider has) and `_on_connection_lost` (the shared supervisor already
  calls it). **`Interruptible`** (`request_local_interrupt`,
  `drop_pending_audio`, `cancel_response`, `truncate_assistant_audio`,
  `audio_chunks_pending`) is resolved once with `isinstance` at connection
  open and stored; the spine never `getattr`-probes per turn. The six
  server-VAD members go with the server-VAD path. Both adapters leave the
  mypy ignore baseline.
- **`WakeFunnel`** (`on_fire`, `stage`, `outcome`, `attach_audio`) owns every
  `wake_events` call; its methods return before the row is written.
- **`MeasurementHold`** landed in #4104 (`pause`, `pause_response`,
  `resume`); the per-frame `is_set()` read on the hold event stays where it
  is. Its scalar `pause()` has no production caller (Wave 2.3).
- **`AssistantOutput`** (`play_cue`, `chirp`, `speak_text`, `begin_episode`,
  `end_episode`) owns the output gate, ducker, cue manager and earcon PCM.
- **`TurnHost`** is the small protocol research and push-to-talk call back
  into: `begin_turn`, `end_turn`, `state`, and the refusal guards.
- **Observability** is one `event=turn.timeline` line per turn from the wake
  loop and one `event=voice.turn` line per turn from `BaseLiveTurn.release()`
  (`provider`, `model`, `ttfa_ms`, `duration_ms`, `chunks`, tokens,
  `turn_lost`), plus `last_ttfa_ms`, `last_turn_ms`, `reconnects_session`,
  `silent_responses_session` on `/state.voice`. The bespoke prose latency
  lines in both adapters are deleted, and Gemini's log tag names Gemini.
- **Frame size has one owner.** `MicCapture.OUTPUT_FRAME_SAMPLES` and the
  bridge's `OUT_FRAME_SAMPLES` are one constant, and `_UdpMicProtocol`
  rejects a datagram of any other length.

### 3.3 Leave alone

The constant block `voice_daemon.py:312-497` (every threshold carries its
corpus and its failure mode — the model for the rest of the prose); the
OR-gate critical section and its refractory; `_end_turn`'s re-entrancy
guard; the measurement-pause ordering and its deadline arithmetic; the
output-admission authority asked twice; `_supervisor.py`, `catalog.py`,
`provider_state.py`, `model_discovery.py`, `log_event.py`; the tool registry,
`packs.py`'s transactional registration, `fence_untrusted` and the
consequential-action confirmation; `output_gate.py`'s epoch; the cue cache
with its stale-beats-silent fallback; `openwakeword_guard.py`; `wake_legs.py`;
the bridge-as-process boundary (PortAudio callback wedges need a process to
kill) and its two watchdogs; the `wake_events` hot/cold split; the ring and
CamillaDSP geometry; pacing pinned against the Rust budget by a test. The
input side and the output side need no redesign; their findings are cosmetic.

## 4. Phases and gates

Every PR: `scripts/test-fast`, then `/simplify`, then `/code-review` medium,
findings fixed or wontfixed in the description. Sonnet for moves, deletions
and prose; Opus where a seam, a name, an order, or a hot path is decided.

**Wave 0 — the ruler (Opus, one PR).** L1. Gate: deploy, speak ten turns,
read `event=turn.timeline`; write the typical `speech_end → first_response`
and `first_response → cue` numbers into the ledger row. Nothing in Wave 3 or
6 starts without these.

**Wave 1 — deafness and boot (Opus, three PRs).** (a) Push-to-talk refusals
cue and log like the wake path: `"CAP"` plays `spend_cap_reached`, the
acquire-error path plays `internal_error` when the connection is not paused
(`voice_daemon.py:4645,4695`); one behavior pin each. (b) A lost turn or a
model that returns no audio plays a cue and increments
`silent_responses_session` (`voice_daemon.py:5308`). (c) Boot order: open
mics, TTS and the cue manager first, start the provider connection in the
background and let the supervisor cue the outage; Gemini's initial connect
retries transient failures on the same wall-clock budget OpenAI uses
(`gemini_session.py:1051`, `openai_session.py:1353`); `READY=1` goes out
before the connect or the unit gets a `TimeoutStartSec` above the budget —
one or the other, and the stale claim is deleted. Gate: a spare Pi booted
with the WAN unplugged plays the network-down cue and never reaches
`StartLimitAction`.

**Wave 2 — subtraction (Sonnet, one PR per bullet, all zero-behavior).**
- `WakeFuser` and the per-second condition refresh: offsets are always empty
  and `verify()` always fires; inline `detector.threshold`, classify the
  condition once at fire time, delete `wake_fusion.py`, its test, the dead
  suppression branch and `CONDITION_REFRESH_SEC` (≈ −170).
- The server-VAD path end to end: six Protocol members, ~90 adapter lines,
  ~70 daemon lines, `JASPER_SERVER_VAD_ENABLED` (ADR-0152 is the removal
  condition; **owner decides** whether the experiment knob survives).
- `_synthetic_audio_profile` and the scalar `MeasurementHold.pause()`
  wrapper (zero production callers) deleted; `trace.py` and
  `submit_recorded_audio` → `tests/voice_eval/`. `for_tests` moves to
  `tests/_wake_loop.py` only under owner decision 6.
- Dead re-exports, function-local re-imports, `getattr` ceremony on members
  both adapters implement, `QueueFull` handlers on unbounded queues, the
  unreachable flat-rate warning, `server_vad_active`, `_started_at`,
  `_turn_count`, the Grok `noise_reduction` pop, `ConversationTranscriptTurn`
  and `ConversationMetadataTurn`, the `audio_out` delegate.
- Prose: the 15-span lists in `report-voice-daemon` §4 and
  `report-providers` §6 (≈ −500 lines); the two contradicting comments at
  `voice_daemon.py:3815,3822` and `:4025-4037` go first.
- Four UDS line-protocol clients converge on one `jasper/line_uds.py`; the
  duplicate `GainRamp` in `rust/jasper-fanin/src/tts.rs:1221` imports the
  shared one.

**Wave 3 — our side of the latency (Opus, one PR each).** L2, L3, L4, L5,
L6, in that order. Gate per PR: Wave 0's numbers before and after on the
same Pi, in the PR description.

**Wave 4 — decompose `WakeLoop` (Opus, one PR per module).** 4.1
(`measurement_hold`) landed as #4104. Order for the rest: `peering_client` →
`wake_telemetry` (this one also shrinks `_handle_wake_frame` from 285 lines
to ~150) → `research_announcer` → `push_to_talk` → `assistant_output`. The
steward's scout rejected the research extraction as "a redesign"; it is —
the `TurnHost` seam in §3.2 is that redesign, so it is an Opus PR that lands
the seam and the move together or not at all. Each PR moves its methods, its state
fields and its tests; before moving a test file, convert its `caplog`
substring assertions to `tests/_log_events.py` and delete the misfiled
tests in `test_voice_daemon_wake_triple_stream.py:355-end` into files named
for their subject. Gate: file smaller, no new public surface beyond the
contract in §3.2, `test-merge` green.

**Wave 5 — one provider base (Opus, three PRs).** (a)
`tests/test_voice_supervisor.py` pins `is_transient`, `outage_cue`,
`OutageTracker`, `Deferred`, `run_reconnect_with_backoff` directly, and the
two provider test files lose their duplicated reconnect sections. (b)
`_base.py` with the shared skeletons; Gemini and OpenAI shrink to wire code;
`grok_session.py` is the proof. (c) `session.py` trimmed to the §3.2
contract; adapters off the mypy ignore baseline; one conformance test per
adapter. Gate: every provider's existing behavior pins pass unchanged.

**Wave 6 — hardware-gated tuning (owner).** L7, then L8, then L9, then
L10, each as its own A/B with the timeline as the ruler and a superseding
ADR when a constant moves. `daemon_main.py` table-driving and the
`AsyncExitStack` teardown can ride along with Wave 4 or 5.

## 5. Guards to add (each tied to a recurrence found above)

- **Contract conformance:** one test per adapter that `isinstance(turn,
  LiveTurn)` and `isinstance(conn, LiveConnection)` hold, and that the
  daemon's resolved `Interruptible` set matches `catalog.py`'s declared
  `interrupt_reconcile`. Recurrence: six leaky members, three undeclared.
- **No SQLite on a frame path:** a test drives `_handle_wake_frame`,
  `_handle_session_frame` and `_play_responses` with a store whose writes
  block, and asserts the handlers return. Recurrence: three sites.
- **One frame size:** the datagram-length check in `_UdpMicProtocol` and a
  test that both constants are the same object. Recurrence: two
  declarations kept in sync by a comment.
- **Structured log assertions:** the test rubric already forbids prose
  assertions; `tests/_log_events.py` exists and one file uses it. No new
  guard — the Wave 4 rule "convert before moving" is the mechanism.

## 6. Decisions the owner must make (do not re-ask elsewhere)

1. Server VAD: delete the path (ADR-0152 says it stays off) or keep the env
   knob as a documented experiment. Recommendation: delete; the ADR records
   the evidence and git keeps the code.
2. `END_OF_UTTERANCE_SILENCE_SEC` 0.8 → 0.6 (L8): run the A/B or leave it.
   Recommendation: run it after Wave 0 and Wave 3, since L4 alone gives back
   80 ms with no tuning.
3. Capture latency `low` (L7): try it on one box for a day.
4. Whether `wake_events`' SQLite store stays. The input-side audit judged it
   earned (a query surface the corpus tooling and `flag_recent_issue` use);
   the fix is where its writes run, not whether it exists. Recommendation:
   keep it.
5. Program ownership: run this brief as its own orchestrator session (the
   UX-audit shape) or hand its waves to the #4030 steward as lanes.
   Recommendation: own session, because Waves 0, 1, 3 and 6 need hardware
   gates and one person reading the timeline; tell the steward to skip the
   voice loop meanwhile.
6. `for_tests` classmethods: #4085's came-back-clean list sanctions them as
   a seam across eight modules; the voice-daemon audit found this one is
   185 lines of stub classes plus a `setattr` back door shipped to the Pi.
   Keep the seam or move it to `tests/`. Recommendation: move this one,
   leave the other seven alone.

## 7. Sub-agent prompt template

> Read `AGENTS.md`, then `docs/VOICE-AUDIT-2026-09-05.md` §0–§3 and the
> report section named in your row. Confirm findings `<ids>` still hold at
> HEAD; if one no longer does, say so and skip it. Implement exactly those
> ids under §3's contracts: one concern, every touched file smaller unless
> the feature grew, no compatibility shims or dual paths, no comments
> narrating the change, tests move with their subject and assert structured
> fields via `tests/_log_events.py`, never log prose. Any new `await` on
> `_handle_wake_frame`, `_handle_session_frame` or `_play_responses` is named
> and bounded in the PR description. Run `scripts/test-fast`, then
> `/simplify`, then `/code-review` medium and resolve every finding. Push to
> `<branch>`. Report: diffstat, findings you wontfixed and why, and the one
> thing to verify on the Pi (for Wave 3: the timeline deltas before/after).

## 8. Execution ledger

Tick as merged. Rows condense the reports' finding tables; the reports carry
the per-finding detail and the exact lines at `8777cff19`.

Wave 0 — the ruler
- [ ] 0.1 `event=turn.timeline` + `/state.voice` last-turn fields + re-anchored first-chunk log (latency §G, providers E1–E3) — Opus
- [ ] 0.2 Owner gate: ten turns read, typical numbers recorded here: speech_end→first_response = __ ms, first_response→speaker = __ ms

Wave 1 — deafness and boot
- [ ] 1.1 Push-to-talk `CAP` and acquire-error cues + pins (voice-daemon H1, H2)
- [ ] 1.2 Silent-response / lost-turn cue + counter (providers B2)
- [ ] 1.3 Boot order + Gemini transient initial-connect retry + READY/TimeoutStartSec (providers A1, A2, B1)
- [ ] 1.4 Owner gate: WAN-unplugged boot on a spare Pi

Wave 2 — subtraction
- [ ] 2.1 `WakeFuser` + condition refresh (input-side F1; voice-daemon M4)
- [ ] 2.2 Server-VAD path (providers C1) — after owner decision 1
- [ ] 2.3 `_synthetic_audio_profile`; scalar `MeasurementHold.pause()` (voice-daemon M9, M11); `for_tests` → tests only under decision 6 (H4)
- [ ] 2.4 `trace.py` + `submit_recorded_audio` → `tests/voice_eval/` (providers G6)
- [ ] 2.5 Dead code sweep (voice-daemon L1–L4; providers G1–G5, G7, C2, C3, C4; tools F3–F6)
- [ ] 2.6 Prose sweep, `voice_daemon.py` (voice-daemon §4, M5, M6)
- [ ] 2.7 Prose sweep, `jasper/voice/` (providers §6, I1, I2; tools F10; input-side F4)
- [ ] 2.8 `jasper/line_uds.py` (providers D3); fan-in `GainRamp` import (rust F2)

Wave 3 — our side of the latency
- [ ] 3.1 SQLite off the loop, three sites (voice-daemon H3, M1, M10; rust F1; latency #5)
- [ ] 3.2 One CamillaDSP round-trip per turn (voice-daemon M2, M3; latency #3, #4, #6)
- [ ] 3.3 EOU clock anchored at frame start (latency #2)
- [ ] 3.4 End-of-turn chirp before the writes (voice-daemon M10)
- [ ] 3.5 Concurrent tool dispatch within a round (tools F1)
- [ ] 3.6 Shared arming gate, one unit (voice-daemon M7; audio_buffer L8)
- [ ] 3.7 Owner gate: before/after timeline numbers in each PR

Wave 4 — decompose WakeLoop
- [x] 4.1 `measurement_hold.py` — landed as #4104 before this brief
- [ ] 4.2 `peering_client.py`
- [ ] 4.3 `wake_telemetry.py` (also `_handle_wake_frame` inlay)
- [ ] 4.4 `research_announcer.py` + `TurnHost`
- [ ] 4.5 `push_to_talk.py`
- [ ] 4.6 `assistant_output.py` + `FanInDucker` converged with `camilla.Ducker` (voice-daemon L5)
- [ ] 4.7 `daemon_main.py` table-driven builders + `AsyncExitStack` teardown + `control_socket.py` (providers F1–F3)

Wave 5 — one provider base
- [ ] 5.1 `tests/test_voice_supervisor.py`; provider test files shrink (tests §4, §7.1)
- [ ] 5.2 `_base.py`; adapters become wire-only (providers A3, D1, D2, D4, H1)
- [ ] 5.3 Contract trim + mypy baseline exit + conformance tests (providers A4, C2–C4, J1; §5 guard 1)

Wave 6 — hardware-gated tuning (owner)
- [ ] 6.1 `JASPER_AEC_CAPTURE_LATENCY=low` (latency #1)
- [ ] 6.2 EOU 0.8 → 0.6 or adaptive; supersede ADR-0152 (latency #9)
- [ ] 6.3 20 ms transport, wake framing decoupled (latency #7, #8)
- [ ] 6.4 Certified ring pair for the ordinary graph — non-negotiable tier (latency #10)
