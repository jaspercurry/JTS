# S5-wake — end-to-end wake→response scenario @ 2d571e6b8

## A. Verdict

The **single-speaker** wake path is the best-instrumented flow in this repo: every
*known* blocking condition either cues or logs a structured `event=`, the funnel is
persisted per-event, and `/state.voice` publishes runtime truth (`wake_legs`,
`tool_packs`, `endpointer`, `push_to_talk_only`) rather than intent. Three seams
break that record. (1) **Non-negotiable 6 has holes at the edges, not the middle**:
four branches drop a fired wake with a log and no cue, and one (`AssistantOutputGate.
begin_turn`) can block the chirp *and* the turn for up to 120 s. (2) The
**multi-speaker "exactly one responder" election is not one** — `PeeringStateMachine`
drops a local bid whenever a peer's WAKE multicast beats the local detector (so
`rank.py`'s six tiers effectively never run), and three state/event combinations
return no action at all, leaving the ARBITRATE RPC to expire into a **fail-open WIN**
— i.e. the suppression path produces a *second* answering speaker 500 ms late. (3)
The **tool safety seam is two mechanisms pretending to be one**: `untrusted_output`
is a declarative flag nothing reads at runtime, and the taint window is armed by
hand-written `monitor.mark()` calls in two of the four tools that return third-party
text. Separately, the whole "unknown deafness" class — mic streaming silence, model
degraded, threshold drifted after an AEC change — has **no detector anywhere**: the
wake_events DB holds exactly that fact and no live surface reads it.

## B1. Happy path — hop list (⟂ = process boundary)

| # | hop | file:function |
|---|---|---|
| 1 | mic → ALSA capture, AEC3/DTLN/chip beams, per-leg UDP emit ⟂ **jasper-aec-bridge** | `jasper/cli/aec_bridge.py:_aec_loop` (708 LOC); ports from `jasper/wake_legs.py:REGISTRY` |
| 2 | playback reference UDP in ⟂ **jasper-outputd** → bridge | `jasper/cli/aec_bridge_reference.py` |
| 3 | UDP datagram → `asyncio.Queue(64)` ⟂ **jasper-voice** | `audio_io.py:_UdpMicProtocol.datagram_received` → `UdpMicCapture.frames` |
| 4 | primary "on" leg drives the main loop; other legs run parallel tasks | `voice_daemon.py:WakeLoop.run` (2432‑2500), `_wake_leg_loop` (2546) |
| 5 | gates: measurement (2454) → mute (2465) → pre-roll/capture-ring append → acquiring buffer (2480) | `voice_daemon.py:run` |
| 6 | openWakeWord score, refractory, OR-gate race, `WakeFuser.verify` | `_handle_wake_frame` (3360‑3646), `wake.py:WakeWordDetector.score_frame`, `wake_fusion.py:74` |
| 7 | research-window cancel (3460‑3478); `wake.detected`; condition classify; **synchronous** SQLite INSERT | `wake_events.py:begin_event:419` |
| 8 | spawn `_arbitrate_acquire_drain`; `_acquiring=True` buffers ≤250 frames (20 s) | `voice_daemon.py:3634`, `audio_buffer.py:46` |
| 9 | `ARBITRATE` over UDS ⟂ **jasper-control** (peering thread) → state machine → rank → WIN/LOSE | `voice_daemon.py:_peer_arbitrate:3984` → `peering/uds.py:send_request` → `peering/daemon.py:_handle_arbitrate:349` → `peering/state.py` → `peering/rank.py:rank` |
| 10 | gate cues (spend cap / paused conn), then `_begin_turn(listening_feedback=True)` | `voice_daemon.py:3833‑3859` |
| 11 | `AssistantOutputGate.begin_turn` → chirp (fire-and-forget) → duck ⟂ **jasper-fanin** UDS → `acquire_turn` ⟂ **provider WSS** | `voice/output_gate.py:89`, `voice_daemon.py:_begin_turn_inner:4891` |
| 12 | pre-roll (7 frames / 560 ms) + acquire drain → session frames; Silero EOU / server VAD / PTT cap | `_handle_session_frame:4364` |
| 13 | tool calls → one dispatch seam ⟂ (network per tool) | `tools/__init__.py:dispatch_tool:766` |
| 14 | TTS PCM → emission-admission check → tts.sock ⟂ **jasper-fanin** → CamillaDSP ⟂ **jasper-outputd** → DAC | `audio_io.py:write_segment:554‑587`, `tts_routing.py:FANIN_TTS_SOCKET` |
| 15 | `_end_turn_inner`: SESSION_ENDED to peering, end_input, usage close, off-chirp, drain, unduck, conversation row | `voice_daemon.py:5176‑5417`, `conversation_history.py` |

Five processes on the critical path (bridge, voice, control/peering, fanin, outputd)
plus the provider. Peering adds one extra ⟂ **before** the chirp.

## B2. Every branch that ends a wake with no audible response

`cue` = user hears something · `log-only` = journal/DB only · `silent` = neither.

| # | file:line | condition | class | surface |
|---|---|---|---|---|
| 1 | `voice_daemon.py:2454` | measurement window open — frame dropped pre-scoring | **silent** | `/state.voice.measurement_active` |
| 2 | `voice_daemon.py:2465` | mic muted — frame dropped | **silent** (mute click played at mute time, `_play_mute_click:3027`) | `/state.voice.mic_muted` |
| 3 | `voice_daemon.py:3448` | `wake.suppressed` — fuser veto | log-only (**unreachable**: `wake_fusion.py:74` returns True) | `event=wake.suppressed` |
| 4 | `voice_daemon.py:3473‑3478` | research-window cancel timed out (20 s) → `return` | **silent** | `logger.warning` **prose, no `event=`** |
| 5 | `voice_daemon.py:3812‑3815`, `:3826‑3829` | `_wake_late_cancelled` pre/post-arb (mute or measurement raced the wake) | **silent** | `event=wake.late_cancel` |
| 6 | `voice_daemon.py:3821‑3828` | peer arbitration LOSE | silent **by design** (another speaker answers) | `event=peering.wake.lost` |
| 7 | `voice_daemon.py:3841` | spend cap reached | **cue** `spend_cap_reached` | + funnel `gate_blocked` |
| 8 | `voice_daemon.py:3855` | connection paused after 1.2 s re-check | **cue** `connection.wake_cue()` | + funnel |
| 9 | `voice_daemon.py:3886‑3899` | `_arbitrate_acquire_drain` raises | **cue** `internal_error` / conn cue | `logger.exception` |
| 10 | `output_gate.py:105` via `voice_daemon.py:5075` | admission paused (MEASURE_PAUSE) while `_acquiring` → `await resumed.wait()` **unbounded**, ≤120 s (`MEASUREMENT_AUTOCLEAR_SEC:573`) | **silent**, and the chirp is behind it | none — `/state` shows `state:WAKE` |
| 11 | `voice_daemon.py:4645` | button/`POST /session/start` + spend cap → `return "CAP"` | **silent** — no cue, **no log** | HTTP 503 only |
| 12 | `voice_daemon.py:4633`/`4643` | button + mute / measurement | log-only (deliberate) | `event=session.manual_refused` + 503 |
| 13 | `voice_daemon.py:4699‑4701` | button, `_begin_turn` raises, connection **not** paused → `return "ERROR"` | **silent** (wake path plays `internal_error` for the same condition, `:3898`) | 502 |
| 14 | `voice_daemon.py:2377‑2389` | a `_wake_leg_loop`/`_manual_mic_loop` task raises (e.g. `score_frame` ONNX error, `wake.py:68` unguarded) | **silent** — bare `create_task`, no done-callback; `finally` swallows the traceback | **`/state.voice.wake_legs` still lists the dead leg → it lies** |
| 15 | `voice/_supervisor.py:299‑301` | outage-escalation cue task untracked (no ref, no error cb) | **silent** if GC'd or it raises | none |
| 16 | `voice_daemon.py:2225‑2234` | `_play_cue` while the gate is busy (timer/research announcement) | log-only | `event=cue.skipped reason=output_active` |
| 17 | `voice_daemon.py:2208‑2224` | no cue manager (bake/API-key failure) | log-only, once per daemon | `event=cue.skipped reason=cues_unconfigured` |
| 18 | `audio_io.py:568‑581` | cue/chirp bytes refused at emission admission | log-only, once per streak | `event=tts_write.refused` |
| 19 | `audio_io.py:328` | bridge dies → `frames()` blocks forever; no bumps → systemd `WatchdogSec=30s` restarts voice | silent, self-healing | journal restart only |
| 20 | `peering/state.py:294`, `:314`, `:408‑412` | three LocalWake/PeerClaim combinations emit **no action** → RPC expires → **fail-open WIN** | worse than silent: **a second speaker answers** | `logger.warning` prose |

Anything reaching the cue *does* get heard: `_play_cue_owned:2237‑2266` survives duck
failure, and `find(slug)` coverage for every slug this path plays was re-checked
against `cues/registry.py` — no drift (confirms p1-T01's cross-tile note).

## B3. Multi-speaker election — mechanism and races

**Mechanism.** No leader, no consensus: every peer runs the identical pure
`PeeringStateMachine` (`peering/state.py`) over the same admin-local multicast
stream (239.192.0.1:5354) and the identical pure `rank()` (`peering/rank.py`), so
they are *supposed* to converge. voice blocks on one UDS RPC
(`_peer_arbitrate:3984`, **client timeout 0.5 s**) while the daemon collects peer
WAKE reports for `arb_window_ms` (default 150, clamp ≤500) and then emits
`StartSession`→WIN or `StandDown`→LOSE (`daemon.py:_execute:447`).

| race | evidence | effect |
|---|---|---|
| **Local bid dropped** — a peer's WAKE arrives while we are IDLE → we adopt the foreign epoch and become CANDIDATE (`state.py:330‑337`); our own LocalWake then hits the CANDIDATE branch and returns `[]` (`:308‑314`) | detection jitter is 30‑150 ms (`config.py:57`), LAN multicast <1 ms | our report never enters `rank()`; the six-tier ranking degrades to **"first detector to fire wins"**. `rank.py` (162 LOC) is dead in the common multi-waker case |
| **Suppression becomes a delayed double-answer** — SUPPRESSED + `score < break_threshold` returns `[]` (`state.py:289‑294`); nothing resolves `_pending_decision`; `daemon.py:391` would fail open at 0.65 s but voice's own 0.5 s timeout fires first (`voice_daemon.py:3945`) → `_peering_send` returns None → `_peer_arbitrate` returns **WIN** | `daemon.py:96` computes a 0.15 s margin the client never honours | the peer that was meant to stay quiet opens a turn 0.5 s late; two speakers answer |
| **Foreign CLAIM for a different epoch during our CANDIDATE window** — `state.py:408‑412` appends `StandDown` *only* when epochs match, then cancels the arb timer and resets the epoch | the comment at `:406` names the exact hazard ("would hang until its hard timeout") and the guard leaves it open | same fail-open WIN, plus 0.5 s added to time-to-chirp |
| **Concurrent multi-waker split-brain** — `_on_peer_wake` records a foreign report only when the incoming epoch is equal or *smaller* (`:340‑357`); a larger epoch's report is discarded | both peers can reach WINNER on different report sets, both `StartSession` | both voices open turns; the later mutual CLAIM makes both concede *in the state machine only* — `_resolve_pending:470` is a silent no-op once the future is done, and **peering has no way to revoke a turn already started** |
| **SNR tier is dead** | `voice_daemon.py:3818` passes `snr_db=None` with the comment "needs rolling-noise-floor state nothing tracks" — but `_ring_noise_floor_dbfs(self._capture_ring_on)` is computed 80 lines later in the same method (`:3598`) | tier 4 never fires; ties fall to RMS, which `rank.py:44` itself calls gain-sensitive across heterogeneous mics |

The `test_wake_propagation_picks_exactly_one_winner_and_suppresses_rest` docstring
(`tests/test_peering_state.py:294‑309`) already names the multi-waker race as a known
gap; the **daemon-level consequences above (fail-open WIN, unrevokable turn) are not
covered by any test** — `test_peering_daemon.py` never asserts what happens when the
state machine returns no action.

**Peering dead weight.** `_handle_status` and `PING` (`uds.py:22`, `:110`) have zero
production consumers: `/rooms.json` reads only `peering.env` (`web/rooms_setup.py:298‑
317`) and `doctor/peering.py:82` shells `avahi-browse`. `_known_peers` is written by
`PeerDiscovery` (287 LOC) and the 30 s HELLO loop and read only by that unread STATUS.

## B4. Assistant quality — what the household feels

| item | evidence | verdict |
|---|---|---|
| time-to-chirp | chirp is fire-and-forget *after* `begin_turn()` (`voice_daemon.py:4852‑4862`), so duck + `acquire_turn` do not delay it — good. But `_handle_wake_frame` first does a **synchronous** `sqlite3.execute` on the loop (`wake_events.py:419`; the module's own `:41` note says SD-card stalls are why other ops use `to_thread`), 9 `os.environ.get` calls, and a 75-frame numpy percentile | +10‑20 ms of avoidable pre-chirp work; unbounded on a busy SD card |
| peering adds a hop before the chirp | 150 ms arb window (default) on the WIN path | felt; unavoidable by design, but see the 0.5 s fail-open cases |
| double processing | one openWakeWord ONNX instance **per leg** (up to 5) at 12.5 fps + a shadow Silero on the "off" leg during sessions (`daemon_main.py:1117‑1124`) | real CPU on a 1 GB Pi; the extra legs exist for corpus work |
| condition machinery | `_maybe_refresh_condition:3339` runs `_ring_noise_floor_dbfs` at 1 Hz to feed `WakeFuser`, whose offsets are always empty (`wake_fusion.py:51,74`) | pure Phase-1.3 scaffolding cost; confirms p1-T02 F13 |
| refractory / follow-up | `WAKE_REFRACTORY_SEC = 0.2` (`:319`); `_end_turn_inner:5414` re-arms it. **There is no follow-up listening window at all** — every exchange needs the wake word | `home_assistant.py:307‑310` calls this out itself: a consequential confirmation must be answered in a *new* wake turn. If the model answers "yes" via `home_assistant("yes")` instead of `home_assistant_confirm`, `store.clear()` (`:302`) silently drops the pending action |
| barge-in | detection-only: `_handle_playback_frame:4160` sets a local interrupt; the provider may resume (`_barge_in_reconcile`) | honest and surfaced in `/state.voice.barge_in` |
| barge-in armed on un-cancelled audio | `_aec_reference_available:501` (True for any `udp:`) disagrees with `input_policy.py:122‑131` (`custom_udp` ⇒ `echo_cancelled=False`) | confirms p1-T01 F5; only bites a custom `JASPER_MIC_DEVICE`, since the reconciler ships `udp:9876` |
| nested timeouts on the button path | accessory HTTP 2.0 s (`control/client.py:72`) < control→voice UDS 5.0 s (`control/uds.py:48`) < `manual_session_start` unbounded | a slow START answers 503 "voice_daemon unreachable" **while the turn actually opens**; the bridge then never sends END on release (`accessories/bridge.py:218`) |
| PTT hold cap | `_ptt_input_cap_sec:4227` derives the cap from `idle_timeout_sec` and warns in both degraded bands | earns its keep — the best-reasoned function in the file |
| no-speech abort | 5 s, then the off-chirp bookends it (`:5375`) | good |

## B5. Tool safety seam — completeness per tool

`untrusted_output` is **declarative only**: `dispatch_tool` (`tools/__init__.py:766‑
840`) never reads it. Arming the taint window is a hand-written `monitor.mark()`.

| tool | returns 3rd-party text | `untrusted_output` | fenced | `monitor.mark()` | gap |
|---|---|---|---|---|---|
| `list_recent_emails` / `read_email` | yes | ✅ `gmail.py:248,358` | ✅ `:325‑342,414‑431` | ✅ `:347,426` | — |
| `list_calendar_events` (both) | yes | ✅ `calendar.py:152,213` | ✅ `:85,98` | ✅ `:201,262` | — |
| `get_travel_routes` | yes (Google line/headsign/stop names) | ✅ `travel_routes.py:145` | ✅ `google_routes.py:198‑289` | ❌ **never** — `monitor` is not even a parameter of `make_travel_routes_tools`, and `packs.py:314‑318` passes only `d.google_routes` | injected text does **not** arm the HA confirmation |
| `home_assistant` | yes — HA's own `spoken_response` + device names, named as untrusted at `tools/__init__.py:60‑62` | ❌ not declared | ❌ `result.as_tool_result()` unfenced (`:338`) | ❌ | an HA-side entity name is a live injection vector into the same session that can call `home_assistant_confirm` |
| transit / weather / spotify / bus / subway / citibike | upstream API strings | ❌ | ❌ | ❌ | accepted risk; not re-reported |

Consequential gate: `classify_consequential:132` is six English regexes over the
**model-written** `query` string; the module says so (`:105‑110`). `monitor=None` is
fail-safe (always confirm), and `packs.py:324‑328` does wire the real monitor —
so the gate is only as good as the two tools that arm it. Confirms and extends
p1-T02 F12. The guard meant to catch this (`tests/test_tools_have_regression_
scenarios.py:44‑50`) only sees `@tool`-decorated defs; `get_weather`,
`get_travel_routes`, `get_current_time` are `Tool(ToolDefinition(...))` literals and
are invisible to it — its own comment "the shape every tool module uses" is false at
HEAD (confirms p1-T02 F6b).

## C. Findings, ranked

| # | sev | file:line | what | evidence | cleanest fix |
|---|---|---|---|---|---|
| 1 | **Blocker** | `peering/state.py:289‑294`, `:408‑412`; `voice_daemon.py:3945` vs `peering/daemon.py:96` | Three LocalWake/PeerClaim combinations return no action; the unresolved RPC fails open as **WIN**, so the *suppressed* speaker answers too | `state.py:406` comment names the hazard; voice's 0.5 s client timeout beats the daemon's 0.65 s fail-open, making `ARBITRATE_RPC_MARGIN_SEC` dead | make every `_on_local_wake` terminal path emit `StandDown`; log in `_resolve_pending` when the future is already done; raise the client timeout above `ARBITRATE_RPC_TIMEOUT_SEC` (or derive both from one constant) |
| 2 | **Blocker** | `peering/state.py:308‑314` + `:330‑337` | A local wake is discarded whenever a peer's WAKE beat our detector, so `rank.py`'s six tiers never see our score — "best-positioned speaker answers" degrades to "first to fire wins" | jitter 30‑150 ms vs <1 ms multicast (`config.py:57`); acknowledged as a gap in `tests/test_peering_state.py:305‑308` but only for the *state machine*, not for the daemon | in CANDIDATE, if the epoch has no report for `self._p.peer_id`, add our report and `BroadcastWake` it instead of returning `[]` |
| 3 | **Blocker** | `voice/output_gate.py:104‑107` reached from `voice_daemon.py:5075` | Wake landing in the acquire window while `MEASURE_PAUSE` opens blocks the chirp **and** the turn for up to 120 s, no cue (NN-6) | `_measurement_pause_detailed:2654` only refuses on `State.SESSION`; during `_arbitrate_acquire_drain` the state is WAKE with `_acquiring=True`; `MEASUREMENT_AUTOCLEAR_SEC=120` | *confirms p1-T01 F3.* Bound `begin_turn()` and treat expiry as a refusal that plays `internal_error`; or refuse `MEASURE_PAUSE` while `_acquiring` |
| 4 | **Blocker** | `voice_daemon.py:2377‑2389` | Secondary wake-leg and manual-mic tasks are bare `create_task`s: a raise kills the leg **silently**, and `/state.voice.wake_legs` keeps listing it | `_track_task:150` exists and logs; `run()`'s `finally` swallows the traceback with `except (CancelledError, Exception): pass`; `wake.py:68 score_frame` is unguarded | route both through `_create_fire_and_forget_task`, and drop the leg from `self._legs` on death so `/state` stops lying |
| 5 | **Should-fix** | `tools/__init__.py:766‑840` + `travel_routes.py:145` + `home_assistant.py:338` | The untrusted-output seam is two mechanisms: a flag nothing reads and a hand-written `mark()`. Two of four text-returning tools miss the arm; HA's reply is neither declared nor fenced | table §B5; `dispatch_tool` never touches `tool.untrusted_output` | make `dispatch_tool` call `monitor.mark()` when `tool.untrusted_output` and the payload is non-empty — the flag becomes load-bearing and the per-tool call sites delete. Then declare + fence HA's `spoken_response`. *(extends p1-T02 F12)* |
| 6 | **Should-fix** | `voice_daemon.py:3466‑3478` | Research-window cancel timeout drops the wake with a prose WARN and no cue, **and awaits up to 20 s inside the main mic loop** | `RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC = 20.0` (`:435`); the await sits between `run()`'s frame reads, so the 64-frame queue (5.1 s) overflows and `Heartbeat.bump` stops for 20 s against `WatchdogSec=30s` | *confirms p1-T01 F11 and raises it:* move the wait into `_arbitrate_acquire_drain` (already a background task), cut the bound to ~2 s, emit `event=` and play `internal_error` |
| 7 | **Should-fix** | `voice_daemon.py:4645`, `:4699‑4701` | Button/HTTP refusals diverge from the wake path: spend cap returns `"CAP"` with **no cue and no log**; a non-connection `begin_turn` failure returns `"ERROR"` with no cue | every neighbouring refusal logs `event=session.manual_refused`; the wake path cues both conditions (`:3841`, `:3898`) | *confirms p1-T01 F4, extends it to the ERROR path.* Add the log + `spend_cap_reached` cue and the `internal_error` cue |
| 8 | **Should-fix** | `peering/uds.py:114`, `:22`; `peering/daemon.py:_handle_status:422`, `discovery.py` (287 LOC), `_hello_loop:524` | The STATUS/PING RPCs, `_known_peers`, the HELLO broadcaster and the whole zeroconf browser have **no production consumer** — `/rooms.json` reads the env file and doctor shells `avahi-browse` | `grep PEERING_UDS_PATH` outside the package: only `config.py:956`; `uds.py:22` claims doctor uses PING — `doctor/peering.py` does not | delete `discovery.py`, `_known_peers`, `_prune_stale_peers`, HELLO, STATUS and PING; or wire STATUS into `/rooms.json` and doctor. Do one, not neither |
| 9 | **Should-fix** | `control/uds.py:48` (5.0 s) vs `control/client.py:72` (2.0 s) vs `manual_session_start` (unbounded) | Nested timeouts shrink outward, so a slow START answers 503 `voice_daemon_unreachable` while the turn is opening; the accessory then withholds END on release | `accessories/bridge.py:218‑243` retries only on that structured reason; `_HoldController` sends END only if START succeeded | give START a deadline inside `manual_session_start` and return a real refusal code, or make the outer budgets exceed the inner ones |
| 10 | **Should-fix** | no file | **No surface reports wake recency or rate.** A speaker whose mic streams silence looks healthy in `/state` and `jasper-doctor` | `doctor/wake.py` has 2 checks (model file, legs configured); `doctor/memory.py:605` checks only *disk size*; `/state.voice` has no wake counter; only `jasper-wake-review` (offline) reads the corpus | one doctor check + one `/state.voice.last_wake_at` from the row the store already writes. This is the only detector for the deafness class the cue registry cannot cover |
| 11 | Should-fix | `wake_events.py:419` | Synchronous `sqlite3.execute` on the event loop on the wake hot path | the module's own `:41` docstring says SD-card stalls are why the heavy ops use `to_thread`; `attach_audio:508` and the sweep `:820` do | move the INSERT to `to_thread`, or spawn `_arbitrate_acquire_drain` *before* the telemetry block |
| 12 | Should-fix | `voice_daemon.py:3818` vs `:3598` | `snr_db=None` is hardcoded "because nothing tracks a rolling noise floor" — `_ring_noise_floor_dbfs` is computed 80 lines later in the same method; rank tier 4 is dead | `rank.py:44‑48` calls the RMS fallback gain-sensitive across mics | pass the noise floor through, or delete tier 4 and the comment |
| 13 | Should-fix | `voice/_supervisor.py:299‑301` | The provider-outage escalation cue is an untracked `create_task` with no strong ref and no error callback | the only untracked task in the voice tile; `_track_task:150` is right there | *confirms p1-T01 F10* — hold it on `OutageTracker` with a done-callback |
| 14 | Should-fix | `voice_daemon.py:501` vs `voice/input_policy.py:122‑131` | Two implementations of "does this mic leg carry an AEC reference?" that disagree on non-9876 UDP | barge-in would arm against un-cancelled audio on a custom UDP mic | *confirms p1-T01 F5* — delete `_aec_reference_available`, read `contract_from_config(cfg).echo_cancelled` |
| 15 | Should-fix | `home_assistant.py:302`, `:307‑310` | Confirmation requires a **new wake turn**; if the model routes the user's "yes" back through `home_assistant` rather than `home_assistant_confirm`, `store.clear()` drops the pending action silently | the code comment admits "no follow-up-listening yet" | either open a short post-turn listening window for a pending confirmation, or make `home_assistant` treat a bare affirmative with a live pending as a confirm |
| 16 | Should-fix | `tests/test_tools_have_regression_scenarios.py:44‑50` | The "every tool ships a regression scenario, no exceptions" guard sees only `@tool`-decorated defs and misses the three `Tool(ToolDefinition(...))` tools | its own comment claims that is "the shape every tool module uses"; `weather.py:152`, `travel_routes.py:145` refute it | *confirms p1-T02 F6b* — enumerate from the built registry |
| 17 | Nit | `wake_fusion.py:51,74` + `voice_daemon.py:3339‑3358`, `:3448` | The fuser is `base + 0.0` / `return True`; the 1 Hz condition estimator and the `wake.suppressed` branch exist only to feed it and a telemetry column | `_offsets` is non-empty only in one test | *confirms p1-T02 F13* — inline the threshold read, keep `classify_condition` for the column only |
| 18 | Nit | `wake_events.py` (no `DELETE`) | wake_events rows never age out; only WAVs roll off | `grep DELETE\|VACUUM` → none | *confirms p1-T02's unpinned failure mode* — cap rows in the same sweep |
| 19 | Nit | `voice_daemon.py:3838` | `"spend_cap_reached"` is a bare literal while every sibling path uses a `*_CUE_SLUG` constant | `:3858` uses `self._connection.wake_cue()`, `:3898` `INTERNAL_ERROR_CUE_SLUG` | one constant |
| 20 | Nit | `voice_daemon.py:3519` and `:3595` | The primary leg's fire-frame RMS is computed twice (`_frame_rms_dbfs(frame)` and `_tail_frame_rms_dbfs(rt.capture_ring)` over the same last frame) | both on the hot path | reuse the first value for the "on" leg |

**Earns its keep (tried to cut, could not):** `_ptt_input_cap_sec` (`:4227`) — the
two degraded bands are real and unguessable. `_wake_late_cancelled` (`:3916`) — the
double check either side of the arbitration await is load-bearing. `AssistantOutput
Gate.wait_idle`'s re-check loop (`output_gate.py:71‑79`) and the emission-admission
seam (`audio_io.py:568`) — both close real windows. `_configured_wake_legs` (`:835`)
— the `None`-vs-`False` tri-state genuinely distinguishes "no room mic" from "the
room mic should be here and isn't".

## D. What only hardware/runtime can prove

- AEC3/DTLN convergence and residual echo after the ref path changes — nothing here is
  measurable statically; the entire multi-leg corpus rig exists for it.
- Real wake rates: false-accept/false-reject per leg, whether the OR-gate over 5 legs
  actually raises recall enough to pay for 5 ONNX inferences per 80 ms on a 1 GB Pi.
- Whether the 150 ms arb window absorbs real fleet detection jitter, and how often the
  §B3 races fire in a real two-speaker household (needs two Pis + multicast).
- Time-to-chirp and time-to-first-audio budgets (bridge → voice → provider → fanin →
  Camilla → outputd → DAC); whether the SQLite INSERT (#11) is measurable.
- Whether a 20 s block in `_handle_wake_frame` (#6) actually trips `WatchdogSec=30s`
  — it depends on where the block lands relative to the 10 s heartbeat tick.
- Whether the fan-in `PROGRAM_DUCK_ON` UDS round-trip (1.0 s socket timeout,
  `voice_daemon.py:296`) ever stalls under load.

## E. Coverage

**Read in full:** `jasper/peering/{state,daemon,rank,uds,config}.py`;
`jasper/voice/{output_gate,input_policy,input_presence}.py`; `jasper/wake_legs.py`;
`jasper/wake_fusion.py`; `jasper/cues/registry.py`; `jasper/tools/home_assistant.py`
(gate half); `jasper/control/handlers/voice.py`; `jasper/control/uds.py`.

**Read at the relevant altitude (every function on the flow, verbatim):**
`voice_daemon.py` — `FanInDucker`, `_aec_reference_available`, `_frame_rms_dbfs`,
`_ring_noise_floor_dbfs`, `_configured_wake_legs`, `run`, `_wake_leg_loop`,
`_manual_mic_loop`, `_measurement_pause_detailed`, `_output_admission_refusal`,
`_drain_inflight_output`, `_maybe_refresh_condition`, `_handle_wake_frame`,
`_telemetry_stage/_outcome`, `_arbitrate_acquire_drain`, `_wake_late_cancelled`,
`_peering_send`, `_peer_arbitrate`, `_notify_peering_session_*`,
`_resolve_barge_in_for_turn`, `_handle_playback_frame`, `_send_session_audio`,
`_end_session_input`, `_ptt_input_cap_sec`, `_handle_manual_session_frame`,
`_handle_session_frame`, `_await_connection`, `manual_session_start/end`,
`session_status`, `_begin_turn(_inner)`, `_begin_turn_output_episode`,
`_cleanup_after_failed_begin`, `_end_turn(_inner)`, `play_cue`,
`play_supervisor_cue`, `_play_cue(_owned)`, `_play_dynamic_text`,
`_play_listening_chirp`, `_prepare_assistant_loudness_context`,
`announce_research_ready`, `_queue/_drain_pending_research`, `_speak_research_job`,
`_open_confirmation_window`, `_track_task`, `_create_fire_and_forget_task`.
`audio_io.py` — `MicCapture`, `UdpMicCapture`, `_UdpMicProtocol`,
`parse_udp_device`, `make_mic_capture`, `TtsPlayout.{set_emission_admission,
write_segment,wait_drained}`. `voice/daemon_main.py` — `_start_control_socket`,
`build_ducker`, the leg/manual-mic open block, `_require_usable_input`.
`tools/__init__.py` — fencing seam, `UntrustedContentMonitor`, `dispatch_tool`.
`wake_events.py` — `begin_event`, retention block. `wake.py` — `WakeWordDetector`.
`control/server.py` — `_voice_cmd_or_error`, POST routes.
`cli/doctor/{peering,wake,memory}.py`. `web/rooms_setup.py` header + `_read_peering_
block`. `accessories/bridge.py:190‑260`. `tests/test_peering_state.py`,
`tests/test_tools_have_regression_scenarios.py:30‑70`.

**Grep-only (labelled as such):** consumer counts for `PEERING_UDS_PATH`,
`voice_parked_no_mic`, `untrusted_output`, `manual_session_start`, `JASPER_MIC_DEVICE`;
absence of `DELETE`/`VACUUM` in `wake_events.py`; absence of any wake-recency reader.

**Not opened:** `cli/aec_bridge.py` internals beyond the stall watchdog (owned by
p1-T07); `peering/{transport,avahi,discovery}.py` bodies (only their consumers);
the Gemini/OpenAI adapters' turn internals (p1-T01); `rust/` and `c/` — no branch of
this scenario's decision logic lives there.
