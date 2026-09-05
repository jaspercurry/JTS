# Audit: provider layer and daemon_main

## 1. Contract quality — `LiveTurn` / `LiveConnection`

**The seam is right; the enforcement is absent.** One connection per daemon, turns acquired per wake, provider-neutral `ConnectionState` — that shape is correct and `jasper/voice/daemon_main.py:285` is a genuine single switch point. But the Protocol enforces nothing:

- Neither adapter inherits from the Protocols (`grep 'class .*(LiveTurn)'` → zero hits). Conformance would be structural, checked by mypy at `acquire_turn() -> LiveTurn` (`jasper/voice/openai_session.py:1020`, `jasper/voice/gemini_session.py:749`) — but **both adapter modules are on the mypy `ignore_errors = true` baseline** (`pyproject.toml:414-417`, `ignore_errors` at `:434`). So the return annotations are never checked.
- Both Protocols are `@runtime_checkable`, and no code anywhere calls `isinstance(x, LiveTurn)`. `tests/test_voice_barge_in_contract.py:26-31` states outright that `isinstance(turn, LiveTurn)` **is already `False` for Gemini**.

So `jasper/voice/session.py` is 476 lines of which 296 are prose (62%) documenting a contract that no checker, test, or runtime path validates.

### Every `LiveTurn` member, and who really implements it

| # | Method | session.py | OpenAI/Grok | Gemini |
|---|---|---|---|---|
| 1 | `send_audio` | :68 | real :247 | real :226 |
| 2 | `send_text_context` | :71 | real :263 | real :242 — **no try/except, unlike its siblings** |
| 3 | `end_input` | :80 | real :338 | real :247 |
| 4 | `audio_out` | :86 | :361 delegate | :259 delegate |
| 5 | `audio_out_chunks` | :91 | real :365 | real :263 |
| 6 | `release` | :99 | real :374 | real :272 |
| 7 | `last_activity_at` | :106 | real :433 | real :300 |
| 8 | `last_chunk_at` | :114 | real :436 | real :303 |
| 9 | `bytes_sent` | :120 | real :445 | real :313 |
| 10 | `chunks_received` | :127 | real :448 | real :316 |
| 11 | `usage_tokens` | :132 | real :451 | real :319 |
| 12 | `usage_breakdown` | :143 | real :454 | **stub → `None`** :337 |
| 13 | `turn_lost` | :164 | real :464 | real :354 |
| 14 | `server_turn_complete` | :170 | real :439 | real :306 |
| 15 | `mark_server_vad` | :177 | real :630 | **ABSENT** |
| 16 | `server_speech_started` | :187 | real :621 | **ABSENT** |
| 17 | `wait_for_server_eou` | :195 | real :627 | **ABSENT** |
| 18 | `audio_chunks_pending` | :204 | real :442 | **ABSENT** |
| 19 | `wait_for_interrupt` | :215 | real :473 | real :357 |
| 20 | `clear_interrupted` | :221 | real :476 | real :360 |
| 21 | `cancel_response` | :245 | real :491 | **no-op stub** :377 |
| 22 | `truncate_assistant_audio` | :268 | real :511 | **no-op stub** :383 |
| 23 | `request_local_interrupt` | :301 | real :587 | real :392 |
| 24 | `drop_pending_audio` | :317 | real :597 | real :400 |

`LiveConnection`: 9 of 11 shared; `set_turn_detection` (`:458`) and `create_response_only` (`:469`) are **OpenAI-only, absent on Gemini**.

**Leaky (single-provider) members on the shared contract: 6 of 35** — items 15-18 plus the two connection methods. All exist solely to serve `JASPER_SERVER_VAD_ENABLED`, which **ADR-0152 rules off and says stays off**. That is six Protocol members, ~55 lines of Protocol prose, ~90 lines of adapter code (`openai_session.py:616-648, 1158-1192, 1825-1844`), and ~70 lines of daemon branch (`voice_daemon.py:703-734, 4390-4441, 4964-5018, 5064-5068`) serving a documented-dead experiment.

### Members the daemon uses that the Protocol does not declare (undeclared contract)

| Member | Called at | Declared? |
|---|---|---|
| `server_speech_detected` | `voice_daemon.py:4436` | **No** — only `server_speech_started` is |
| `set_billable_activity_meter` | `daemon_main.py:120` | **No** |
| `_on_connection_lost` | `_supervisor.py:517` (shared supervisor reaches into turn privates) | **No** |
| `user_transcript` / `assistant_transcript` | `voice_daemon.py:5289-5290` | on `ConversationTranscriptTurn` (`session.py:335`) — **never imported anywhere** |
| `conversation_metadata` | `voice_daemon.py:5291` | on `ConversationMetadataTurn` (`session.py:354`) — **never imported anywhere** |
| `submit_recorded_audio` | `tests/voice_eval/harness.py:435` | No (ships in prod adapter, `openai_session.py:276-336`, 61 lines) |

### Every `getattr` probe instead of the Protocol — 17 sites, 7 of them pure ceremony

| Site | Member | Verdict |
|---|---|---|
| `voice_daemon.py:706` | `wait_for_server_eou` | real hole (OpenAI-only) |
| `voice_daemon.py:718` | `create_response_only` | real hole |
| `voice_daemon.py:4161` | `request_local_interrupt` | **ceremony — both implement** |
| `voice_daemon.py:4410` | `server_speech_started` | real hole |
| `voice_daemon.py:4436` | `server_speech_detected` | **undeclared** |
| `voice_daemon.py:4957` | `send_text_context` | **ceremony**; raises `RuntimeError` if missing |
| `voice_daemon.py:4992` | `set_turn_detection` | real hole |
| `voice_daemon.py:5004` | `mark_server_vad` | real hole |
| `voice_daemon.py:5277` | `usage_breakdown` | **ceremony — both implement** |
| `voice_daemon.py:5433` ×2 | `user_transcript`/`assistant_transcript` | OpenAI-only |
| `voice_daemon.py:5448` | `conversation_metadata` | Gemini-only |
| `turn_playback.py:23` | `audio_out_chunks` | **ceremony** |
| `turn_playback.py:84` | `drop_pending_audio` | **ceremony** |
| `turn_playback.py:109` | `cancel_response` | **ceremony** |
| `turn_playback.py:112` | `truncate_assistant_audio` | **ceremony** |
| `turn_playback.py:299` | `audio_chunks_pending` | real hole |
| `daemon_main.py:120` | `set_billable_activity_meter` | **undeclared** |

Notice the shape: the capability seam's own docstrings (`session.py:226-243`) argue that getattr-probing is the *design* — "capability-based, not provider-name-based". That argument holds only for members some adapter might genuinely omit. Six of these probe methods **both shipped adapters implement**, and `catalog.py` already carries a declared, test-pinned `interrupt_reconcile` kind for exactly this. The probing and the declaration are two mechanisms for one decision.

### Minimal clean contract

**Keep on `LiveTurn` (13):** `send_audio`, `send_text_context`, `end_input`, `audio_out_chunks`, `release`, `last_activity_at`, `last_chunk_at`, `bytes_sent`, `chunks_received`, `usage`, `turn_lost`, `server_turn_complete`, `wait_for_interrupt` + `clear_interrupted` (one pair).

**Merge:** `audio_out` into `audio_out_chunks` (drop `session.py:86`, the two 3-line delegates, and `turn_playback.py:23-31`'s fallback). `usage_tokens` + `usage_breakdown` into one `usage() -> TurnUsage` dataclass — the caller already handles a `None` breakdown, so a dataclass with optional detail fields removes a probe and 20 lines of Protocol prose (`session.py:132-162`).

**Move to a declared optional sub-Protocol** the daemon `isinstance`-checks once at connection open (not getattr per turn): `Interruptible` = `request_local_interrupt` + `drop_pending_audio` + `cancel_response` + `truncate_assistant_audio` + `audio_chunks_pending`. Resolve once, store the resolved capability on the WakeLoop, and the spine becomes straight-line.

**Drop entirely:** the six server-VAD members (ADR-0152), `ConversationTranscriptTurn` and `ConversationMetadataTurn` (unused), `audio_out`, `server_vad_active`.

**Where provider-specific behaviour belongs:** transcripts/metadata are the same concern with two shapes — collapse to one `capture() -> TurnCapture | None` on `LiveTurn` that each adapter fills with what it has (OpenAI: text; Gemini: tool names). Billing shape belongs in `catalog.py` next to `pricing_buckets`, resolved by `daemon_main`, not getattr-probed on the connection.

---

## 2. OpenAI ↔ Gemini duplication

I diffed every same-named member with comments and docstrings stripped. `sim` is a character-level similarity of the code alone.

### `LiveTurn` implementations

| Member | OpenAI | Gemini | sim |
|---|---|---|---|
| `_note_activity` | :689-704 | :502-515 | **1.00** |
| `audio_out` | :361-363 | :259-261 | **1.00** |
| `audio_out_chunks` | :365-372 | :263-270 | **1.00** |
| `drop_pending_audio` | :597-614 | :400-415 | **1.00** |
| `request_local_interrupt` | :587-595 | :392-398 | **1.00** |
| `bytes_sent` / `chunks_received` / `turn_lost` / `last_activity_at` / `last_chunk_at` / `server_turn_complete` / `wait_for_interrupt` / `clear_interrupted` | :433-477 | :300-361 | **1.00** each |
| `send_audio` | :247-261 | :226-240 | 0.94 |
| `_on_connection_lost` | :781-786 | :522-533 | 0.71 |

**≈95 raw lines of `GeminiLiveTurn` are a copy of `OpenAIRealtimeTurn`.**

### `LiveConnection` implementations

| Member | OpenAI | Gemini | sim |
|---|---|---|---|
| `_open_session` | :1484-1494 | :1140-1150 | **1.00** |
| `is_paused` / `last_failure_detail` / `wake_cue` / `set_failure_escalation_cb` / `request_reconnect_now` / `start` | :982-1075 | :698-813 | **1.00** each |
| `_do_initial_connect` | :1341-1351 | :1041-1049 | 0.94 |
| `supports_server_vad` | :1077 | :815 | 0.93 |
| `_maybe_reset_context` | :1848-1872 | :1502-1532 | 0.91 |
| `_set_state` | :942-951 | :670-687 | 0.86 |
| `stop` | :1000-1018 | :723-747 | 0.84 |
| `acquire_turn` | :1020-1047 | :749-785 | 0.75 (same skeleton + one provider hook) |
| `_on_turn_released` | :1205-1229 | :900-914 | 0.63 (same skeleton) |
| `_teardown_session` | :1544-1580 | :1246-1301 | 0.41 (same skeleton) |
| `_open_session_attempt` | :1496-1542 | :1152-1191 | 0.37 (same skeleton) |

**≈148 raw lines of `GeminiLiveConnection` are a copy**, plus three more shared skeletons.

### Concern-by-concern

| Concern | Shared today? | Where |
|---|---|---|
| Reconnect supervisor + backoff | **Yes, fully** | `_supervisor.run_supervisor_loop` / `run_reconnect_with_backoff`, `jasper/backoff.py` |
| Transient/terminal classification | **Yes** | `_supervisor.is_transient:166` |
| Outage cue + escalation | **Yes** | `_supervisor.OutageTracker:247` |
| Deferred mid-turn reconnect | **Yes** | `_supervisor.Deferred:338` |
| Tool dispatch (timeout, error shaping, timing logs) | **Yes** | `jasper/tools/__init__.py:766` |
| **Initial connect** | **NO — two divergent loops** | `openai_session.py:1353-1466` (114 lines, wall-clock budget) vs `gemini_session.py:1051-1138` (88 lines, fixed 5-step schedule, **409-only**) |
| **Pre-cap reconnect watchdog** | **NO — two loops** | `openai_session.py:1582-1654` (73) vs `gemini_session.py:1193-1244` (52). Identical shape: sleep → check terminal states → defer if `_active_turn` → else `_reconnect_event.set()` |
| **Receive-loop close-code extraction** | **NO — three copies** | `_supervisor.provider_code:132`, `gemini_session._close_code_and_reason:144`, inline `getattr(getattr(e,"rcvd",None),"code",None)` at `openai_session.py:1690` |
| **Receive-loop exception tail** | **NO** | `openai_session.py:1689-1709` ≈ `gemini_session.py:1486-1500` |
| Audio in/out queues | copies (see table) | unbounded `asyncio.Queue()` in both |
| Transcript capture | genuinely different | OpenAI text deltas; Gemini has none |
| Usage accounting | genuinely different | OpenAI sums per-response; Gemini deltas a cumulative counter |
| Interrupt/truncate | genuinely different | `needs_client_truncate` vs `server_self_truncates` (already declared in `catalog.py:119`) |
| Server VAD | OpenAI only | ADR-0152 says it stays off |
| Pause/resume | **Yes** | `_supervisor.await_connected` / `request_planned_reopen` |
| Failure escalation cues | **Yes** | `OutageTracker` |

The code itself names the duplication 14 times — "Mirrors the Gemini Live adapter" (`openai_session.py:7`), "Filter mirrors gemini_session" (`:886`), "Mirrors `OpenAIRealtimeTurn._note_activity()`" (`gemini_session.py:510`), "Matches OpenAIRealtimeConnection" (`:574`), "mirrors the OpenAI proactive-watchdog deferral idiom" (`:72`), "parity with the OpenAI adapter's ... log" (`:486`), and more. Each is an acknowledged convergence that was written down instead of done.

### Proposed shared base

`jasper/voice/_base.py`:

```
class BaseLiveTurn:
    # owns: _audio_q, _interrupt_event, _last_activity_at, _last_chunk_at,
    #       _bytes_sent, _chunks_received, _released, _turn_lost,
    #       _server_turn_complete, _started_at_monotonic
    # concrete: audio_out_chunks, release skeleton, all accessors,
    #           wait_for_interrupt/clear_interrupted, request_local_interrupt,
    #           drop_pending_audio, _note_activity, _on_connection_lost,
    #           send_audio (guard + delegate to _wire_send_audio + lost-marking)
    # abstract hooks: _wire_send_audio, _wire_end_input, _wire_release_extra, usage

class BaseLiveConnection:   # satisfies _supervisor.SupervisedConnection by construction
    # concrete: _set_state, start, stop, is_paused, last_failure_detail,
    #           wake_cue, request_reconnect_now, set_failure_escalation_cb,
    #           supports_server_vad(False), _open_session, _do_initial_connect,
    #           _open_session_with_retry (ONE time-budgeted loop),
    #           _maybe_reset_context, _teardown_session skeleton,
    #           acquire_turn skeleton, _on_turn_released skeleton,
    #           _scheduled_reconnect_watchdog(delay, reason)  # rotate + pre-cap
    #           _receive_loop_exit(exc)                       # one close-code tail
    # abstract hooks: _connect(), _configure_session(), _new_turn(),
    #                 _close_transport(), _on_reconnect_attempt_failed()
```

**Estimated delta:** ~511 duplicated lines removed across the two adapters, replaced by a ~250-line base → **net ≈ −350 lines of code plus the ~200 lines of mirror-prose those copies carry**. `gemini_session.py` 1,580 → ~950; `openai_session.py` 2,229 → ~1,500. `grok_session.py` is already the right pattern and stays 122.

**Genuinely provider-specific and must stay in the adapter:** wire encoding (base64 JSON events vs `types.Blob`/`send_realtime_input`), session config building (`_build_session_payload` vs `_build_config`), the event dispatcher (`_dispatch_event` vs `_on_response`), tool-call wire packaging, usage normalization (sum vs delta), session-resumption handles + 409 handling, GoAway parsing, the `un-ack activity_end` stale-turn logic, and the truncate/cancel pack.

---

## 3. `daemon_main.py`

**Responsibilities of `run()` (`:648-1290`, 643 lines in one function, ~23 concerns):** config + logging + flight recorder (`:649-655`) · pricing/spend cap + input-policy logging (`:657-716`) · conversation store (`:717-720`) · camilla/renderer/weather (`:722-733`) · transit (`:735-752`) · Google Routes (`:753-758`) · Home Assistant (`:759-773`) · volume stack (`:774-859`) · timer scheduler (`:861-867`) · research scheduler (`:869-891`) · cue manager (`:893-898`) · wake-event store (`:900-927`) · registry build (`:929-948`) · prompt overrides + catalog write (`:950-966`) · signal handlers (`:976-985`) · wake-leg plan + ready log (`:987-1002`) · connection + billable meter (`:1004-1022`) · `connection.start` (`:1024-1061`) · mic/TTS `AsyncExitStack` (`:1062-1174`) · cue attach/regen/loudness seed (`:1176-1186`) · heartbeat + `WakeLoop` + 6 wiring callbacks (`:1188-1245`) · hand-rolled teardown `finally` (`:1257-1290`).

**Is 1,346 lines justified? No — and the file is not really "the entry point".** It imports 12 names from `voice_daemon.py` (`:75-88`), six of them underscore-private (`_LegRuntime`, `_ManualMicRuntime`, `_cancel_tracked_tasks`, `_configured_wake_legs`, `_track_task`, plus `_build_system_instruction` at `:65`), and `voice_daemon.py:5466` imports `run` back from it via a function-local import to break the cycle — plus a forwarding shim `voice_daemon._active_model` at `:5418-5420`. The split is by file size, not by concern; the boundary does not exist.

Concretely reducible:

- **Table-drive the optional-subsystem builders.** Eight blocks follow one pattern (build client → log configured/disabled → set a `*_configured` flag consumed by the prompt): weather, transit, Google Routes, HA, Google clients, research, Spotify router, wake-event store. A `(name, factory, log_line, prompt_flag)` table collapses `:722-927` from ~205 lines to ~60. **≈ −145.**
- **Replace the hand-rolled `finally` with the `AsyncExitStack` already open.** `:1257-1290` (34 lines) closes 10 subsystems in reverse order by hand while `:1084` already uses a stack. Register each closeable at construction via `stack.push_async_callback`. **≈ −30**, and it removes a real ordering hazard (today `content_activity`/`volume_observer` need `is not None` guards because construction can fail after the `try:`).
- **Move `_start_control_socket` (`:545-645`, 101 lines) to `jasper/voice/control_socket.py`.** It is a protocol server, not wiring, and the module docstring at `:552-582` is the protocol spec. Relocation, not deletion.
- **Move `_build_registry`/`_build_router` next to `jasper/tools/packs`** and `_build_cues_manager`/`_schedule_cue_regen` next to `jasper/cues`. ~180 lines relocated.
- **Delete `_wire_billable_activity_meter`'s warning branch (`:120-134`).** Unreachable: the only flat-rate provider is Grok, whose adapter inherits `set_billable_activity_meter` from OpenAI. **−15.**
- `_active_model` (`:93-99`) is a one-line wrapper over `cfg.active_voice_model` reached through a forwarding shim in another module. **−7.**

Realistic target: **daemon_main ~600 lines** with `run()` around 250, the rest relocated to the subsystems they build.

**Is `_start_control_socket` a duplicate of `jasper/control/`'s HTTP API? No — I read both.** `jasper/control/handlers/voice.py` is a *bridge into* this socket: `_get_mic` (`:25-31`), `_post_session` (`:80`), `_post_cue_play` (`:129`), `_post_mic_mute` (`:170`) all call `_server._voice_socket_command(self._voice_socket_path, ...)`, which is `jasper/control/uds.py:47`. Different processes, one direction, no duplication.

**But the *client* of that socket is duplicated four times**, which is the real boundary defect and which the code admits:

| Copy | Lines | Comment |
|---|---|---|
| `jasper/control/uds.py:47-68` | 22 | canonical |
| `jasper/mux.py:1931-1950` | 20 | none |
| `jasper/measurement_window.py:375-401` | 27 | "Same wire format as `jasper.control.server._voice_socket_command`, not imported here to avoid a circular dependency" |
| `jasper/peering/uds.py:165-191` | 27 | "Mirrors `jasper.control.server._voice_socket_command`'s shape" |

AGENTS.md: "Two implementations of one concern in reach: converge them or open an issue — never add a third." There are four. The fix is a `jasper/line_uds.py` (~30 lines) owned by neither daemon, which also breaks the circular-import excuse. **≈ −65.**

---

## 4. Resilience

### Reconnect / backoff — the strong part
`_supervisor.run_reconnect_with_backoff:494` is correct and shared: `_planned_rotate` spends a zero-delay first attempt; `reconnect_delay(attempt, transient=)` (`backoff.py:52`) ramps 1→60 s with ±25% jitter while transient and drops to a 900 s poll when terminal; `attempt` resets to 0 the moment a terminal failure turns transient again (`:562-570`); `sleep_or_nudge` (`backoff.py:90`) makes the 15-minute terminal poll interruptible by a wake word via `request_reconnect_now`, rate-gated by `ReconnectNudge` (`backoff.py:65`). `OutageTracker` (`_supervisor.py:247`) is edge-triggered, holds the cue until a player exists (`Deferred`, `:265`), re-announces on a changed remedy, and waits 4 consecutive failures before the network-down cue so a Wi-Fi roam never speaks. This is well-built.

### Failure-mode walkthrough

| Event | Behaviour | Cue? |
|---|---|---|
| WebSocket drop mid-turn | `_on_connection_lost` → `turn_lost` + sentinel → `_idle_watchdog:291` returns → `_end_turn`; supervisor reconnects | **No** — user hears wake chirp, silence, end chirp |
| Auth failure (401/403) | `is_transient` False → `outage_cue` → `provider_needs_attention` / `provider_out_of_credit`; supervisor polls at 900 s forever; `survive_terminal_initial_connect:195` keeps the daemon alive | **Yes**, correctly, incl. held-until-player-exists |
| Rate limit (429) | transient → exponential ramp | No (correct) |
| **Network down at boot, Gemini** | `_open_session_with_409_retry:1102-1104` re-raises **any non-409 immediately** — one attempt, no retry → `survive_terminal_initial_connect` sees transient → re-raises → `run()` unwinds → traceback → exit 1 | **No.** With `RestartSec=5` a cycle is ~5-10 s → **20 starts inside `StartLimitIntervalSec=300` → `StartLimitAction=reboot`** |
| **Network down at boot, OpenAI** | 600 s wall-clock budget (`openai_session.py:97`) — but `READY=1` is only sent at `daemon_main.py:1196`, *after* `connection.start()`. The unit is `Type=notify` with **no `TimeoutStartSec`** → systemd's 90 s default kills it first | **No.** The documented 600 s budget is unreachable |
| Provider returns no audio (the Gemini silent-session bug ADR-0159 exists to keep visible) | `voice_daemon.py:5308-5326` logs `event=turn.silent_response` once per daemon (`_silent_response_warned` latch) | **No cue, no `/state` field, no counter** |
| Tool exception / timeout | `dispatch_tool` (`jasper/tools/__init__.py:818-835`) bounds every call at `tool.timeout` and returns `{"error": …}`; the prompt tells the model to speak `error` verbatim; a non-JSON-serializable result is contained (`openai_session.py:2072-2082`) rather than reconnecting | Model speaks it — good |

### Silent-deafness assessment

The wake path is well covered: spend cap (`voice_daemon.py:3838`), paused connection with a bounded re-check (`:3844-3857`), acquire failure (`:3897-3900`), no room mic (`:4620`), manual start (`:4658`, `:4700`). All play cues.

Two genuine gaps, neither strictly a non-negotiable violation (the wake *did* respond with chirps) but both leave the household with no information:

- **B1 — Deaf window at boot.** `connection.start()` (`daemon_main.py:1048`) blocks before any mic is opened (`:1084`) and before the cue player exists (`:1220`). During a transient initial-connect retry the speaker cannot hear *or* speak, for up to the systemd start timeout. Opening the mic + cue path first and letting the supervisor connect in the background would turn a silent window into a cued one.
- **B2 — No cue for "the model said nothing" or "the turn was lost."** The Protocol carries `bytes_sent`/`chunks_received` (`session.py:120-130`) whose docstring says they exist "to detect the silent-failure mode where Gemini Live accepts the connection but never produces any output." The detection fires and only logs.

### Bounded timeouts

Bounded: `await_connected` (15 s prod, `_supervisor.py:439-447`), every `_teardown_session` step (3 s ×4, both adapters), `end_input` at teardown (`voice_daemon.py:5264`, 2 s), `_server_vad_response_trigger` (`voice_daemon.py:710`), control-socket read (`daemon_main.py:588`, 2 s), tool dispatch (12 s default), response observer (`turn_playback.py:19`, 0.1 s).

**Unbounded at the JTS layer** (relying entirely on SDK/`websockets` defaults):
- `cm.__aenter__()` — the connect handshake, `openai_session.py:1501`, `gemini_session.py:1168`.
- `self._conn.send(event)` under `_send_lock` (`openai_session.py:1098-1099`). A wedged send holds the lock and blocks **every** subsequent audio frame for the turn. Gemini's `send_realtime_input` (`:885`) has neither lock nor timeout.
- `turn.release()` at teardown (`voice_daemon.py:5268`) — Gemini's `release` sends `activity_end` on the wire (`:287`), OpenAI's sends `response.cancel` (`:399`), both unbounded, while the `end_input` immediately above them *is* bounded at 2 s. Asymmetric.
- `_flush_for_interrupt`'s `cancel(...)` / `truncate(...)` (`turn_playback.py:111, 114`) — on the latency-critical barge-in path.

**Unbounded queues:** `self._audio_q = asyncio.Queue()` in both adapters (`openai_session.py:172`, `gemini_session.py:181`). Bounded in practice by turn length, but both files carry a `QueueFull` handler that can never fire (`openai_session.py:747, 785`; `gemini_session.py:531-533` — whose comment literally reads `# pragma: no cover — unbounded queue`). `_unack_activity_end_times` is age-bounded (`gemini_session.py:836`), `_received_ms_by_item` and the transcript-parts lists are per-turn.

---

## 5. Observability

**What's good:** `jasper/log_event.py` is an excellent primitive — logfmt with real quoting/escaping, JSON sink via `JASPER_LOG_JSON`, `jasper_event` on the record for the flight recorder. `wake_events.py` records a real per-turn funnel with `voice_provider` on every row (`:99, :140, :156`), so **time-to-first-audio *is* recorded per turn per provider** as `ts_turn_opened` → `ts_response_started`, set at `voice_daemon.py:3862` and `:3755`. Transcript content is deliberately kept out of logs (only `chars=`, `openai_session.py:412-417, 1803-1808`).

**The gaps:**

**E1 — The latency facts are unstructured prose.** `log_event` vs raw `logger.*` counts: `openai_session` 19 / 47, **`gemini_session` 2 / 34**, `daemon_main` 12 / 25. And the specific "it felt slow" lines are all in the unstructured half:

| Line | File |
|---|---|
| `"first audio chunk from OpenAI in %.0fms"` | `openai_session.py:673` |
| `"first audio chunk from Gemini in %.0fms"` | `gemini_session.py:431` |
| `"openai turn: ended in %.0fms, %d chunks…"` | `openai_session.py:421, 429` |
| `"live turn: ended in %.0fms…"` | `gemini_session.py:296` |
| `"gemini turn complete: in=%d out=%d…"` | `gemini_session.py:494` |
| `"connect ok in %.0fms"` | `openai_session.py:1510`, `gemini_session.py:1184` |
| `"session torn down in %.0fms"` | `openai_session.py:1580`, `gemini_session.py:1301` |
| `"turn acquire done in %.0fms (sched_lag=… duck=… acquire=…)"` | `voice_daemon.py:5021` |

Every one of these is a per-turn latency you'd want to aggregate, and none is greppable as `event=` or parseable as logfmt.

**E2 — Gemini's log lines don't say "gemini."** `_log_tag = "live connection:"` (`gemini_session.py:551`); turn lines are `"live turn: started"` (`:784`), `"live turn: ended"` (`:296`), `"activity_start sent"` (`:831`), `"model interrupted by user"` (`:462`). OpenAI's are `"openai connection:"` / `"openai turn:"`. On a Gemini box, `journalctl | grep gemini` finds two lines out of ~34. Cross-provider comparison by journal is impossible.

**E3 — `/state.voice` carries no latency at all.** `_VOICE_STATUS_DIRECT_KEYS` (`state_aggregate.py:103-117`) publishes endpointer, spend, connection paused/error, mic mute, measurement, duck, music dBFS, wake legs, ptt-only, tool packs — and nothing about time-to-first-audio, last-turn duration, session uptime, reconnect count, or silent-response count. `session_status()` (`voice_daemon.py:4724-4812`) doesn't produce them either. So "it felt slow" is answerable only by shelling into the Pi and reading journald, or by querying `wake-events.sqlite3` by hand.

**E4 — Cost accounting is sound but latency-blind.** `usage.py`'s `sessions` table (`:413-423`) stores `started_at`/`ended_at`/tokens/cost/provider — per *session*, not per turn, and with no first-audio timestamp. `usage_tracking_degraded` reaching `/state` (`voice_daemon.py:4743`) is a nice touch. `BillableActivityMeter` correctly counts only active turn time for Grok's flat rate.

**E5 — `text_out` trace is OpenAI-only.** `trace.emit("text_out", …)` fires only from `openai_session.py:1751-1752`, so `TurnTrace.spoken_text()` (`trace.py:85`) is always empty on Gemini and every voice-eval text assertion silently skips on that provider. `trace.py:88-98`'s docstring claims "All three current providers … stream these natively" — false for the Gemini adapter as written.

**Minimal fix for the whole section:** one `log_event(logger, "voice.turn", provider=…, model=…, ttfa_ms=…, duration_ms=…, chunks=…, bytes_sent=…, tokens_in=…, tokens_out=…, endpointer=…, turn_lost=…)` emitted from the shared `BaseLiveTurn.release()`, plus three rolling fields on `session_status()` (`last_ttfa_ms`, `last_turn_ms`, `reconnects_session`). That replaces ~8 bespoke prose lines with one structured line and closes E1/E2/E3 together.

---

## 6. Dead code and prose

### Verified dead (grepped across `jasper/`, `tests/`, `scripts/`, `deploy/`, `experiments/`, `docs/`)

| Item | Location | Evidence |
|---|---|---|
| `server_vad_active()` | `openai_session.py:618-619` | **zero references anywhere**, prod or test |
| `ConversationTranscriptTurn` | `session.py:334-351` (18 lines) | never imported; daemon getattr-probes instead |
| `ConversationMetadataTurn` | `session.py:353-364` (12 lines) | never imported |
| `GeminiLiveTurn._started_at` | `gemini_session.py:201` | written once, never read (OpenAI's turn has no such field) |
| `GeminiLiveTurn._turn_count` | `gemini_session.py:194, 449` | written and incremented, never read |
| Grok `noise_reduction` pop | `grok_session.py:107` | `GrokRealtimeConnection.__init__` never accepts `noise_reduction`; parent gets `""` → the key is never added, so the pop can't fire |
| Flat-rate meter warning | `daemon_main.py:120-134` | only flat-rate provider is Grok, which inherits the hook |
| `QueueFull` handlers | `openai_session.py:747, 785`; `gemini_session.py:531-533` | queues are unbounded |
| `audio_out()` fallback | `turn_playback.py:30` | both adapters implement `audio_out_chunks` |

### Production-dead (test-only, but shipped to the Pi)

`trace.py` is 268 lines of which only `emit` (`:188`) has a production caller (`openai_session.py:1751`). `TurnTrace`, `TraceEvent`, `set_active`, `reset_active`, `active`, `traced_registry`, `_TracingExecutor`, `tool_calls`, `tool_returns`, `tool_pairs`, `spoken_text` are consumed **only** by `tests/voice_eval/`. `submit_recorded_audio` (`openai_session.py:276-336`, 61 lines) has one caller: `tests/voice_eval/harness.py:435` — its own docstring says so at `:304-308`.

### Speculative branches (guards for hypotheticals)

| Location | What it defends |
|---|---|
| `openai_session.py:2138-2143` | "Keeps the token counter working **if a future SDK release** changes its model representation" |
| `gemini_session.py:86-112` | `time_left` "may also arrive as a protobuf Duration or a plain number **depending on SDK version**" — three shapes, one is real |
| `gemini_session.py:131-141` | 409 substring scan: "**forward-compat fallback if a future** websockets / SDK release restructures" |
| `gemini_session.py:961-965` | bare `try/except` that silently drops `thinking_level` |
| `openai_session.py:2202-2229` | 28 lines validating a debug env var for non-numeric and negative values |
| `openai_session.py:1110-1133` + `:222-228, 380-391` | ~40 lines of debug-WAV tee, with an `os.environ.get(...).strip()` **per audio chunk** in the hot send path |

### Comment/docstring ratio (tokenized)

| File | Lines | Comment | Docstring | Prose % |
|---|---|---|---|---|
| `session.py` | 476 | 22 | 274 | **62%** |
| `prompt.py` | 381 | 131 | 30 | 42% (prompt text — justified) |
| `gemini_session.py` | 1580 | 404 | 227 | **40%** |
| `turn_playback.py` | 320 | 54 | 73 | 40% |
| `provider_state.py` | 319 | 50 | 70 | 38% |
| `_supervisor.py` | 590 | 81 | 140 | 37% |
| `trace.py` | 268 | 31 | 69 | 37% |
| `openai_session.py` | 2229 | 405 | 342 | **34%** |
| `daemon_main.py` | 1346 | 248 | 148 | 29% |
| `grok_session.py` | 122 | 22 | 11 | 27% |
| `catalog.py` | 426 | 72 | 33 | 25% |
| `model_discovery.py` | 335 | 15 | 10 | **7%** ← the right ratio |

### The 15 worst prose spans

AGENTS.md: comments carry only non-derivable constraints and why-pointers — no narration, no history, no dates, no PR numbers, no reviewer-addressed text.

| # | Location | Lines | Why it fails |
|---|---|---|---|
| 1 | `session.py:226-243` | 18 | Pure design essay ("They are capability-based, not provider-name-based… the spine never branches on `isinstance`") on a Protocol that isn't checked. Belongs in an ADR |
| 2 | `session.py:268-299` `truncate_assistant_audio` | 32 | Re-derives ADR-0115 in full, including "a `None` here is not always an edge case… for two distinct reasons" |
| 3 | `gemini_session.py:981-1028` | 42 | The longest comment block in the package: manual-VAD rationale, `NO_INTERRUPTION` rationale, barge-in "option (a) vs option (b)", "Pinned by tests/test_gemini_barge_in.py", and a redundant second manual-VAD paragraph at `:1007-1022` |
| 4 | `openai_session.py:1272-1294` | 23 | Transcription rationale ending in a dated forensic anecdote — `"kitchen medium" routed to set_volume(50) on 2026-05-24 — STT mishearing or model mis-routing? Could not tell` |
| 5 | `openai_session.py:1918-1946` | 29 | Narrates two hypothetical orphan-response shapes as `(a)`/`(b)`, then the WARNING string itself gives operator advice ("raise `JASPER_IDLE_TIMEOUT_SEC` or look at why…") |
| 6 | `trace.py:127-150` | 24 | Pure history: "Originally this was `ContextVar`… **Confirmed 2026-05-21** by logging server events: OpenAI emitted `response.output_item.added`…" — a bug post-mortem in a comment |
| 7 | `openai_session.py:277-308` `submit_recorded_audio` | 32 | 32-line docstring on a method with one test caller, incl. "(2026-05-21 finding)" |
| 8 | `prompt.py:8-38` and `:132-150` | 50 | Prompt-engineering history: "An earlier version of this prompt had ~15 'Do NOT' clauses… **Verified 2026-05-21** via voice-eval: that prompt produced ZERO tool calls"; "**Path B applied 2026-05-23**"; "added **2026-05-24** after the VAD test matrix" — ADR-0158 owns this |
| 9 | `gemini_session.py:1429-1454` | 26 | Narrates the un-ack bookkeeping as a four-case truth table in prose instead of naming the two predicates in code |
| 10 | `gemini_session.py:770-780` | 11 | "observed on the **2026-06-11** eval runs: one ConnectionClosed here wedged the whole suite" |
| 11 | `openai_session.py:479-489` / `gemini_session.py:363-375` | 22 | Both re-explain the barge-in pack that `session.py:226-243` **and** `catalog.py:25-53` already explain. Three copies of one explanation; both cite `PR-2` / `PR-5` |
| 12 | `grok_session.py:110-122` | 13 | **Factually wrong**: "We don't currently consume text deltas". `openai_session.py:1742-1753` consumes `response.output_text.delta` into the transcript *and* the trace. A wrong comment misleads more than a missing one |
| 13 | `daemon_main.py:1062-1083` | 22 | Restates systemd's `SuccessExitStatus`/`RestartPreventExitStatus`/`ConditionPathExists` semantics, all of which are already documented at length in `jasper-voice.service` |
| 14 | `openai_session.py:1353-1383` | 31 | Docstring re-argues why a budget is wall-time not a retry count — and the sibling adapter does the opposite anyway (see A1) |
| 15 | `openai_session.py:5-51` | 47 | Module docstring is a Gemini-vs-OpenAI comparison ("Mirrors the Gemini Live adapter… same overall shape as Gemini's `activity_start`… the same 12 s timeout the Gemini adapter uses"). The timeout claim is already stale — it moved to `jasper/tools/__init__.py:190` |

Also present: `_supervisor.py:473` cites `#3915`; `catalog.py:136` cites `#2197`; `daemon_main.py:195, 249` cite `#2205`; `provider_state.py:232` cites `#3133/#2212/#3129`; `daemon_main.py:1046` and `prompt.py:360` cite "May 22 2026".

---

## 7. What is genuinely good

1. **`_supervisor.py`.** The best module in scope. `is_transient` (`:166`) is a tight, correct, ADR-backed classifier; `peer_initiated_close` (`:154`) correctly refuses to read `websockets`' deprecated `.code`; `provider_code` (`:132`) documents exactly why. `OutageTracker` (`:247`) gets the hard part right — edge-triggered, held until a player exists, re-announced on a changed remedy, with a network-down streak so a Wi-Fi roam never speaks. `Deferred` (`:338`) is a 40-line primitive genuinely shared by three call sites. `failure_detail` (`:110`) redacts **before** truncating so a clipped tail can't leave half a credential (`:96-97`) — a real, non-obvious correctness property, and secrets never reach `/state`.
2. **`jasper/log_event.py`.** Correct logfmt quoting, control-character escaping, `bool`-before-`int`, `float` via `repr`, positional-only `logger`/`name` so a field can be called `name`, a `fields=` escape hatch for keys that collide with `level`, and `exc_info` threaded only when asked so the common path produces a byte-identical `LogRecord`. Stdlib-only.
3. **`catalog.py` as a registry.** `interrupt_reconcile` is **required with no default** (`:119`) so a new provider fails loudly rather than inheriting wrong barge-in behaviour; `runtime_imports` (`:141`) is required for the same reason and encodes the lazy-import fact (`openai` listed separately from the adapter) that `jasper-doctor` needs; `resolve_interrupt_reconcile` (`:370`) follows `INHERITS` with cycle detection. `default_model_id`/`default_voice_id` raise if a provider doesn't have exactly one default. This is the self-similar registry the rest of the layer should have been built to.
4. **`provider_state.py`.** ADR-0165 implemented properly: one file, no default, fail-soft with a five-state `status` that distinguishes first-time setup from a permission problem (`:80-121`); `read_active_model_from_env_files` (`:208`) deliberately bypasses `os.environ` because a shell export outranks both files there; the mtime+size cache (`:287`) keeps a per-turn read off a stalled filesystem while still honouring a live wizard toggle. The docstring at `:22-29` names the exact stale-`/system/` bug it prevents.
5. **`dispatch_tool` as the cross-provider contract** (`jasper/tools/__init__.py:766`). Timeout, `{"error": …}` shaping, scalar wrapping, observer lifecycle, and identical timing logs live in one place; each adapter keeps only argument parsing and wire packaging. This is exactly the factoring the *session* layer needs and doesn't have.
6. **`grok_session.py`.** 122 lines, three overrides, zero duplication — proof the base-class approach works. It is the model for what `gemini_session.py` should look like.
7. **The playout-ledger truncation contract.** `_flush_for_interrupt` (`turn_playback.py:34`) flushes locally first (latency-critical), refuses to reconcile after a failed flush because there's then no trustworthy boundary, and passes the ledger's `max_audio_played_ms`. The adapter clamps to the item's own received duration (`openai_session.py:550-563`) and no-ops with a WARN on `played_ms <= 0` (`:542-548`) rather than guessing from bytes received. Faithful to ADR-0115.
8. **`_play_responses`' cleanup contract** (`turn_playback.py:139-148, 245-253`). The interrupt waiter, the in-flight write, and the drain waiter are all cancelled and awaited in a `finally`. The docstring names the reference cycle through `turn._interrupt_event` that would otherwise leak a task every turn.
9. **`wake_events.py` funnel.** Validated stage names, per-leg scores/offsets, `voice_provider` on the row, idempotent `PRAGMA table_info` migrations, byte-capped audio retention. The right telemetry substrate — `/state` just doesn't read from it.
10. **`model_discovery.py`.** 7% prose, lazy `httpx`, generic error strings so URLs can't leak keys, atomic `0o600` temp-file-plus-rename, operator-triggered rather than background. Nothing to cut.
11. **Cost correctness under pressure.** Gemini's cumulative counter is baselined per turn with a reset-guard for a counter that restarts under you (`gemini_session.py:328-335`); OpenAI sums across a tool-using turn's multiple responses so a tool round isn't under-counted (`openai_session.py:706-740`); Grok's flat rate meters active turn time only, not warm idle socket time (`:958-980`). Three genuinely different billing models, all right.

---

## Findings

| ID | Sev | file:line | Finding | Recommended change | Δ lines |
|---|---|---|---|---|---|
| **A1** | **HIGH** | `gemini_session.py:1051-1104` | Initial connect re-raises **any non-409 immediately** — one attempt. A network-down boot exits the daemon in seconds; `RestartSec=5` + `StartLimitBurst=20`/`300 s` → **`StartLimitAction=reboot`**. OpenAI's sibling (`openai_session.py:1353`) has a 600 s wall-clock budget whose docstring (`:1372-1378`) names this exact hazard | One shared time-budgeted initial-connect loop in the base, with a provider `_on_initial_attempt_failed` hook for Gemini's handle-drop | −80 |
| **A2** | **HIGH** | `openai_session.py:97` + `jasper-voice.service` | `DEFAULT_INITIAL_CONNECT_BUDGET_SEC = 600` is unreachable: `READY=1` fires at `daemon_main.py:1196`, after `connection.start()`; the unit is `Type=notify` with no `TimeoutStartSec`, so systemd's 90 s default kills it first. The unit comment claims the budget works | Send `READY=1` before the connect (see B1), or set `TimeoutStartSec` above the budget. Pick one and delete the other's claim | ±5 |
| **A3** | **HIGH** | `openai_session.py:170-786` vs `gemini_session.py:174-533`; `:806-1872` vs `:555-1532` | ~511 lines duplicated. 14 same-named members diff at **sim = 1.00** with prose stripped; 14 in-code comments say "mirrors the other adapter" | `jasper/voice/_base.py` with `BaseLiveTurn` + `BaseLiveConnection` (~250 lines) — see §2 sketch | −350 |
| **A4** | **HIGH** | `pyproject.toml:414-417` + `session.py` (476 lines) | Both adapters are on the mypy `ignore_errors` baseline, and nothing calls `isinstance(x, LiveTurn)`. The Protocol enforces nothing at any layer | Remove the two adapter modules from the baseline (a shared base makes this tractable — most surface moves to `_base.py`, which is already checked); or add one conformance test per adapter | +0 |
| **B1** | **HIGH** | `daemon_main.py:1048` vs `:1084`, `:1220` | `connection.start()` blocks before the mic opens and before the cue player is wired. A transient boot outage leaves the speaker unable to hear *or* speak, with no cue | Open mics + TTS + cue manager first, start the connection in the background, let the supervisor cue the outage. Also fixes A2 | ≈ ±20 |
| **B2** | MED | `voice_daemon.py:5308-5326`; `_idle_watchdog` `turn_lost` path | The model returning no audio, and a turn lost mid-reply, both log and play nothing. `bytes_sent`/`chunks_received` exist expressly to detect this (`session.py:120-130`) | Play `internal_error` (or a new `no_answer` cue) and surface a `silent_responses_session` counter on `session_status()` | +15 |
| **C1** | MED | `session.py:177,187,195,204,458,469` + adapters + `voice_daemon.py:703-734,4390-4441,4964-5018` | 6 Protocol members + ~160 lines of adapter/daemon code serve server VAD, which **ADR-0152 rules permanently off** | Delete the server-VAD path with the ADR as the removal condition; keep `JASPER_SERVER_VAD_ENABLED` only if the owner still wants the experiment | −220 |
| **C2** | MED | `turn_playback.py:23,84,109,112`; `voice_daemon.py:4161,4957,5277` | 7 `getattr` probes for members **both** adapters implement — ceremony that hides the real holes | Call directly once the base class guarantees them | −25 |
| **C3** | MED | `session.py:334-364`; `voice_daemon.py:5432-5462` | `ConversationTranscriptTurn`/`ConversationMetadataTurn` declared, never imported; capture goes through `getattr` | One `capture() -> TurnCapture \| None` on `LiveTurn`; delete both Protocols and both `_optional_turn_*` helpers | −60 |
| **C4** | MED | `voice_daemon.py:4436`, `daemon_main.py:120`, `_supervisor.py:517` | Three members used across the boundary but absent from any Protocol (`server_speech_detected`, `set_billable_activity_meter`, `_on_connection_lost`) | Declare `_on_connection_lost` on `LiveTurn` (the shared supervisor depends on it); move billing shape to `catalog.py`; `server_speech_detected` goes with C1 | −10 |
| **D1** | MED | `openai_session.py:111-130` vs `input_policy.py:23-31,76-84` | `_NOISE_REDUCTION_DISABLED` / `_WIRE_VALUES` / `_normalize_noise_reduction` duplicate `OPENAI_NOISE_REDUCTION_*` / `validate_openai_noise_reduction`, differing only by `"auto"` | Import from `input_policy`; delete the adapter copies | −25 |
| **D2** | MED | `_supervisor.py:132-151`, `gemini_session.py:144-162`, `openai_session.py:1690-1691` | Three implementations of "pull the WS close code off this exception" | One `_supervisor.close_code_and_reason`; adapters call it from a shared `_receive_loop_exit` | −35 |
| **D3** | MED | `control/uds.py:47`, `mux.py:1931`, `measurement_window.py:375`, `peering/uds.py:165` | **Four** copies of the one-line-request/one-line-JSON UDS client. Two carry comments admitting it | `jasper/line_uds.py` (~30 lines) owned by neither daemon; also removes the circular-import excuse at `measurement_window.py:380-381` | −65 |
| **D4** | MED | `openai_session.py:1582-1654` vs `gemini_session.py:1193-1244` | Two implementations of "sleep, then reconnect unless a turn is in flight" | One `BaseLiveConnection._scheduled_reconnect_watchdog(delay, reason)` | −85 |
| **E1** | MED | `openai_session.py:673,421,429,1510,1580`; `gemini_session.py:431,296,494,1184,1301`; `voice_daemon.py:5021` | Every per-turn latency fact is unstructured `logger.info` prose. `gemini_session` has 2 `log_event` calls to 34 raw logger calls | One `event=voice.turn` line from `BaseLiveTurn.release()` carrying `provider`, `model`, `ttfa_ms`, `duration_ms`, `chunks`, `bytes_sent`, tokens, `turn_lost` | −30 |
| **E2** | MED | `gemini_session.py:551,296,462,784,831,870` | Gemini's log tag is `"live connection:"` / `"live turn:"` — the provider name appears in 2 of ~34 lines. OpenAI's is `"openai …"` | `_log_tag = f"{PROVIDER_NAME} connection:"` in the base (OpenAI already does this at `:874`) | −2 |
| **E3** | MED | `state_aggregate.py:103-117`; `voice_daemon.py:4724-4812` | `/state.voice` has no latency, no session uptime, no reconnect count, no silent-response count. "It felt slow" needs SSH + journald | Add `last_ttfa_ms`, `last_turn_ms`, `reconnects_session`, `silent_responses_session` to `session_status()` and `_VOICE_STATUS_DIRECT_KEYS` | +12 |
| **E4** | LOW | `trace.py:88-98` vs `openai_session.py:1751` | Docstring claims all three providers stream text deltas; Gemini's adapter emits none, so `spoken_text()` is always empty there and eval text assertions silently skip | Emit `text_out` from Gemini's `output_transcription` path, or correct the docstring | ±8 |
| **F1** | MED | `daemon_main.py:648-1290` | `run()` is 643 lines / ~23 concerns; 8 optional-subsystem builders repeat one shape; the `finally` at `:1257-1290` hand-rolls a reverse-order teardown beside an already-open `AsyncExitStack` | Table-drive the builders; `stack.push_async_callback` each closeable at construction | −175 |
| **F2** | LOW | `daemon_main.py:545-645`; `:435-542`; `:336-432` | Control socket, registry/router build, and cue scheduling are subsystems, not wiring | `jasper/voice/control_socket.py`; move builders next to `jasper/tools/packs` and `jasper/cues` | −380 moved |
| **F3** | LOW | `daemon_main.py:65,75-88`; `voice_daemon.py:5418-5420,5466` | Circular import between the two halves of one daemon, broken by a function-local import, plus 6 underscore-private names crossing the boundary and an `_active_model` forwarding shim | Move `WakeLoop` and the runtime dataclasses into `jasper/voice/`; the boundary either exists or it doesn't | −10 |
| **G1** | LOW | `openai_session.py:618-619` | `server_vad_active()` — zero references anywhere | Delete | −2 |
| **G2** | LOW | `gemini_session.py:194,201,449` | `_started_at` and `_turn_count` written, never read | Delete | −4 |
| **G3** | LOW | `grok_session.py:105-108` | The `noise_reduction` pop can never fire — the subclass never accepts the kwarg | Delete the override | −4 |
| **G4** | LOW | `daemon_main.py:120-134` | Flat-rate-without-meter warning is unreachable (Grok inherits the hook) | Delete | −15 |
| **G5** | LOW | `openai_session.py:747,785`; `gemini_session.py:531-533` | `QueueFull` handlers on unbounded queues; one comment says so | Delete | −8 |
| **G6** | LOW | `openai_session.py:276-336`; `trace.py` (all but `emit`) | 61 + ~230 lines of test-harness code shipped in the production package | Move to `tests/voice_eval/`; keep `trace.emit` (or drop the `text_out` hook with E4) | −290 moved |
| **G7** | LOW | `openai_session.py:86` / `session.py:86` | `audio_out()` is a delegate on both adapters; `turn_playback.py:30`'s fallback is unreachable in production | Drop from the Protocol and both adapters | −20 |
| **H1** | MED | `openai_session.py:1098-1099`; `gemini_session.py:885`; `voice_daemon.py:5268`; `turn_playback.py:111,114` | Wire sends are unbounded at the JTS layer. A wedged send holds `_send_lock` and blocks every subsequent frame; `release()` is unbounded while the `end_input` above it is bounded at 2 s | One `_send_timeout_sec` in the base wrapping every wire send; bound `release()` like `end_input` | +10 |
| **H2** | LOW | `openai_session.py:1111` | `os.environ.get(...).strip()` per audio chunk in the hot send path for a debug tee | Resolve once at turn construction | −3 |
| **I1** | LOW | `grok_session.py:113-116` | Comment says "we don't currently consume text deltas"; `openai_session.py:1742-1753` does consume `response.output_text.delta` into both the transcript and the trace | Delete the sentence — the remap is live, not forward-compat | −4 |
| **I2** | LOW | 15 spans in §6 | ~200 lines of narration, history, dated anecdotes, and PR/issue numbers, incl. three copies of one barge-in explanation (`session.py:226-243`, `openai_session.py:479-489`, `gemini_session.py:363-375`) | Delete; keep only non-derivable constraints and `See ADR-NNNN` pointers | −200 |
| **J1** | LOW | `session.py:106-107` vs `turn_playback.py:294`, `voice_daemon.py:5184` | Protocol states loop time (`loop.time()`); consumers compare against `time.monotonic()`. Equal on CPython by accident, not by contract. Both adapters also keep two clocks per turn (`_started_at` loop, `_started_at_monotonic`) | State `time.monotonic()` in the Protocol and use it in the base; one clock per turn | −6 |

**Net: roughly −1,400 lines of code and ~200 lines of prose from `jasper/voice/`, plus ~670 lines relocated** (test harness out, subsystem builders next to their subsystems).

---

## Proposed target structure for `jasper/voice/`

```
session.py          ~180  (was 476)  Contract only: ConnectionState, AudioOutChunk,
                                     TurnUsage, TurnCapture, LiveTurn (13 members),
                                     Interruptible (5, isinstance-resolved once),
                                     LiveConnection (9). No server-VAD members,
                                     no unused capture Protocols, no design essays.
_base.py            ~250  (new)      BaseLiveTurn + BaseLiveConnection. Owns the
                                     ~511 duplicated lines: queues, counters,
                                     accessors, interrupt plumbing, state machine,
                                     start/stop/acquire/teardown skeletons, ONE
                                     time-budgeted initial connect, ONE reconnect
                                     watchdog, ONE receive-loop exit, ONE
                                     event=voice.turn latency line.
_supervisor.py      ~600  (≈same)    Unchanged in spirit; gains close_code_and_reason
                                     and the SupervisedConnection Protocol now
                                     satisfied by BaseLiveConnection by construction.
openai_session.py  ~1200  (was 2229) Wire only: session payload, event dispatch,
                                     tool packaging, usage sum, truncate/cancel pack,
                                     resampler.
gemini_session.py   ~700  (was 1580) Wire only: LiveConnectConfig, _on_response,
                                     tool packaging, usage delta, resumption handle,
                                     GoAway, un-ack activity_end bookkeeping.
grok_session.py      110  (≈same)    Already right.
catalog.py           426  (≈same)    The registry other layers should imitate.
provider_state.py    319  (≈same)    ADR-0165, done properly.
model_discovery.py   335  (≈same)    7% prose. Nothing to cut.
prompt.py           ~250  (was 381)  Prompt text + the additive per-provider delta;
                                     history moves to ADR-0158.
turn_playback.py    ~230  (was 320)  Direct calls once the base guarantees the
                                     capability set.
control_socket.py   ~100  (new)      Lifted out of daemon_main.
input_policy.py      197             Owns the one noise-reduction vocabulary.
input_presence.py     95  · output_gate.py 190 · earcons.py 392 — unchanged, clean.
daemon_main.py      ~600  (was 1346) Table-driven subsystem construction, one
                                     AsyncExitStack for every lifecycle, run() ~250.
trace.py             ->  tests/voice_eval/   (production has one caller)
```

Two facts to weigh before acting. First, `_supervisor.py` and `catalog.py` prove the team already knows how to build this layer — the shared-base work in A3 is applying an established local pattern, not importing a new one. Second, A1 is the only finding that can reboot a speaker, and it is independent of the refactor: the one-line fix is to make Gemini's initial-connect loop retry transient failures, which can ship on its own today.