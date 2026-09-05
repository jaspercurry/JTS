# Voice Pipeline Test Suite Audit

Scope: 51 files, 27,901 lines, 994 collected test items (`pytest --collect-only` on the full in-scope set — clean, no import errors, no moved/missing subjects). Baseline confirmed at `jasper/voice_daemon.py` (5,476 lines, `WakeLoop`, 114 methods) and `jasper/voice/{openai_session,gemini_session,grok_session,_supervisor}.py`.

## 1. Inventory (grouped; full per-file lines/tests already tallied)

| File | Lines | Tests | Subject | Construction | Coupling |
|---|---|---|---|---|---|
| test_voice_daemon_measurement_inflight.py | 2421 | 40 | measurement-pause/resume cancellation-safety (duck/mute-click/cue ownership across cancel points) | `WakeLoop.for_tests()` + custom fake gates/TTS/clock classes (lines 67–221) | **Very high** — 35 unique private attrs, ~219 refs |
| test_voice_daemon_push_to_talk_endpointer.py | 1201 | 38 | PTT owns end-of-input over Silero; hold-cap math | `for_tests()`; also `_log_events.py` helper (only user in scope) | High — 31 attrs/165 refs |
| test_voice_daemon_research_announce.py | 878 | 26 | research-job announce/confirmation-window state machine | `for_tests()` | High — 37 attrs/136 refs |
| test_voice_daemon_wake_triple_stream.py | 1025 | 39 | DTLN/chip-AEC 3rd wake leg OR-gate **+** leg-planning, `session_status`, `read_music_dbfs`, `maybe_refresh_condition` (grab-bag) | `for_tests()` via local `_make_wake_loop_triple()` | High — 36 attrs/111 refs |
| test_voice_daemon_manual_start_guard.py | 593 | 21 | `manual_session_start` refusal parity with wake path (mute/measurement) | `for_tests()` | High — 29 attrs/90 refs |
| test_voice_daemon_mute_privacy.py | 440 | 14 | mute drops buffered audio; `_play_cue` never silently no-ops | `for_tests()` | High — 21 attrs/68 refs |
| test_voice_daemon_end_turn_reentry.py | 284 | 6 | `_end_turn` idempotency/non-reentrancy | `for_tests()` | High density — 28 attrs/61 refs over 6 tests |
| test_voice_daemon_wake_dual_stream.py | 320 | 11 | 2-leg wake OR-gate, refractory dedupe | `for_tests()` | High — 17 attrs/65 refs |
| test_voice_daemon_barge_in.py | 294 | 11 | provider-agnostic in-session barge-in spine | `for_tests()` | High — 14 attrs/~54 refs |
| test_voice_daemon_defects.py | 437 | 13 | regression grab-bag (idle watchdog, GC/weakref, turn-completion signals) | `for_tests()` + free fn `_idle_watchdog` called directly | Med-high — 22 attrs/43 refs |
| test_voice_daemon_peering.py | 309 | 18 | multi-Pi arbitration (`_peer_arbitrate`), dBFS helper | `for_tests()` | Med — 15 attrs/44 refs |
| test_voice_daemon_conversation_capture.py | 292 | 10 | conversation-history recording | `for_tests()` | Med — 16 attrs/28 refs |
| test_voice_daemon_teardown_end_segment.py | 140 | 2 | `_end_turn_inner` finalizes TTS segment before provider release | `for_tests()` | Med — 23 attrs/27 refs (dense) |
| test_voice_daemon_measurement_gate.py | 267 | 10 | assistant audio refusal during measurement window | `for_tests()` | Med — 8 attrs/16 refs |
| test_voice_daemon_mute_click.py | 154 | 4 | synthetic mute-click / listening-chirp profile selection | `for_tests()` | Low-med — 11 attrs/12 refs |
| test_voice_daemon_wake_funnel_telemetry.py | 124 | 2 | tool-dispatch → `WakeEventStore` bridge | `for_tests()` + real `WakeEventStore`, `ToolRegistry` | Low |
| test_voice_daemon_observability.py | 141 | 8 | pure helpers already extracted to `jasper.voice.daemon_main` (`_tts_ready_detail`, `_wake_ready_detail`, `_warn_if_research_model_unpriced`) | **No WakeLoop at all** — plain function calls | **None** — this is the template post-decomposition tests should look like |
| test_openai_session.py | 3036 | 85 | OpenAI realtime adapter: connect lifecycle, barge-in truncate, retry budget, server-VAD, Grok-subclass smoke, billing meter, reconnect cadence | Fake connect factory (`_FakeConn`/`_FakeConnectFactory`), no WakeLoop | High to `OpenAIRealtimeConnection`/`_supervisor` internals (`_state`, `_reconnect_event`, `_deferred_reconnect`…) |
| test_gemini_connection.py | 1431 | 33 | Gemini Live adapter: resumption handle, go-away/1008, reconnect nudge | Fake SDK plumbing, no WakeLoop | High to same `_supervisor`-shaped internals |
| test_gemini_session.py | 252 | 9 | Gemini turn/session-level behavior | similar fakes | Med |
| test_grok_session.py | 224 | 7 | `GrokRealtimeConnection(OpenAIRealtimeConnection)` meter wiring only | reuses OpenAI fakes | Low — thin, by design |
| test_turn_playback_barge_in.py | 447 | 13 | turn-playback flush/cancel/truncate ordering on barge-in | direct turn objects, `caplog` | Med |
| test_voice_setup.py | 1382 | 73 | `/voice` wizard (provider CRUD, pricing overrides, redaction) | HTTP-level wizard client | Low to WakeLoop; couples to wizard routes |
| test_doctor_voice.py | 480 | 17 | `jasper-doctor` voice checks | `Config`/`SimpleNamespace`, mocks doctor internals | Low |
| test_control_server_voice.py | 309 | 17 | control-daemon proxy to voice UDS socket | `MagicMock`/`AsyncMock` over `uds` protocol | Low |
| test_voice_input_gate.py | 315 | 15 | mute-state reconciler script + systemd unit contract | reads shell/unit **files**, not WakeLoop | Low to WakeLoop, but couples to shell/unit text |
| test_voice_provider_runtime_imports.py | 465 | 22 | `runtime_imports` catalog vs. real adapter imports, via `ast` | `ast.parse(path.read_text())` — structural, not prose | Low coupling, but hard-coded to `daemon_main._make_connection` location |
| test_lazy_imports.py | 1052 | 19 | Pi RAM-budget import-graph guards (subprocess `sys.modules` checks) | subprocess + `ast` | Low — pins `import jasper.voice_daemon` boundary itself |
| test_tools_registry.py | 537 | 33 | tool decorator/build contract (timeout, labels, coroutine guard) | `ToolRegistry` directly | Low |
| test_tool_pack_contract.py / _tool_pack_contract.py | 364 / 317 | 10 / 0 (shared helper) | capability-pack registration contract | `ToolRegistry`+`ToolDeps` via shared contract asserts | Low — good shared-seam example |
| test_wake_events.py | 1200 | 42 | `WakeEventStore` SQLite schema/retention/atomicity | real sqlite `WakeEventStore`, no WakeLoop | None |
| test_wake_fusion.py | 49 | 5 | `WakeFuser` OR-gate threshold math | pure `WakeFuser` | None |
| test_wake_conditions.py | 64 | 6 | condition classifier | pure function | None |
| test_audio_io.py | 1333 | 47 | mic capture / TTS-fanin adapter timeouts | fakes at socket boundary | Med (caplog-heavy) |
| test_audio_buffer_drain.py | 238 | 6 | ring-buffer drain | pure buffer object | Low |
| test_cues_cli.py/_factory.py/_generator.py/_manager.py | 203/64/392/544 | 12/1/29/22 | cue CLI, catalog dispatch, TTS generation, on-disk cache | real `AudioCueManager`/catalog | Low |
| test_earcons_wire_width.py | 283 | 11 | PCM wire-width guard for earcons | pure | None |
| test_aec_bridge_*.py (9 files) | 45–1578 | 1–32 | Rust AEC bridge config/engines/telemetry/systemd — **separate Rust component**, not WakeLoop | subprocess/JSON/unit-file fakes | None to WakeLoop |
| conftest.py | 569 | 0 (fixtures) | global autouse isolation (env, doctor evidence, logger level) | — | None to WakeLoop |

## 2. Prose-asserting tests (rubric violation: "never assert on log/error prose")

A shared, well-built antidote already exists — `tests/_log_events.py` (`event_fields`/`event_records`) parses `jasper.log_event`'s logfmt lines into an `(event, {k:v})` structure specifically so tests stop doing this. **Only `test_voice_daemon_push_to_talk_endpointer.py` uses it.** Everywhere else in scope, 21 other files do raw string containment on `caplog.text` / `r.getMessage()` / `r.message`, ~89 assertion sites total:

- **Free-text log prose** (breaks on any wording change, not just field reshuffle):
  - `tests/test_aec_bridge_optional_engines.py:45` — `"optional path failed: inference failed" in caplog.text`
  - `tests/test_aec_bridge_reference.py:72` — `"ref queue full, dropped 3 frames in last 1.0s" in caplog.text`
  - `tests/test_cues_factory.py:53` — `"falling back" in r.getMessage()`
  - `tests/test_voice_daemon_defects.py:188` — `"response stalled" in caplog.text`
  - `tests/test_voice_daemon_research_announce.py:478` — `"RECORDING TIMEOUT" not in caplog.text`
  - `tests/test_gemini_connection.py:248-263` — startswith/endswith on full sentences: `"live connection: connect ok in "` … `"ms (resumption=<new>)"`, `"live connection: session torn down in "` … `"ms"`.
  - `tests/test_openai_session.py:807-810,835`, `831` — `"chars=16" in message"`, `"Transport error." not in message"` (privacy-critical transcript-redaction guard — good behavior, wrong altitude).
  - `tests/test_cues_manager.py:219-220` — `"cue.regenerate_failed" in r.message or failed_slug in r.message`.

- **`event=` logfmt substring checks done ad hoc instead of via `_log_events.py`** (semi-structured but still brittle to formatting — spread-across-records false positives are exactly what `_log_events.py`'s docstring warns against): `test_voice_daemon_manual_start_guard.py:92-93,105,108`; `test_voice_daemon_measurement_gate.py:59-61,99`; `test_voice_daemon_measurement_inflight.py:322-323,345-346` (+8 more sites); `test_voice_daemon_mute_click.py:72-75`; `test_voice_daemon_observability.py:118,140`; `test_voice_daemon_wake_dual_stream.py:119,153-154`; `test_voice_daemon_wake_triple_stream.py:347,368`; `test_openai_session.py:567,607,831,2192-2223`; `test_tools_registry.py:434,449`; `test_turn_playback_barge_in.py:335,446`; `test_voice_daemon_barge_in.py:223,226`.

- **`match=` on `pytest.raises`**: present in 10 files (`test_audio_buffer_drain.py`, `test_audio_io.py`, `test_cues_generator.py`, `test_gemini_connection.py`, `test_gemini_session.py`, `test_openai_session.py`, `test_voice_daemon_measurement_inflight.py`, `test_voice_daemon_push_to_talk_endpointer.py`, `test_voice_setup.py`, `test_wake_events.py`) — not individually audited for exact-text vs. anchored-pattern risk, but every one is a candidate to replace with an exception type/attribute check.

- **`inspect.getsource`**: `test_voice_daemon_push_to_talk_endpointer.py` (one site) — literal source-text assertion, the most direct rubric violation in scope.

**Not violations** (verified, despite matching the grep): `test_voice_provider_runtime_imports.py`, `test_voice_daemon_wake_triple_stream.py:855-865` (Heartbeat-call arity), and `test_aec_bridge_capture.py:203` all use `ast.parse(path.read_text())` to check *structure* (import targets, call arity), not prose — legitimate, though still file-path-coupled (see §3/§7). `test_aec_bridge_systemd.py`, `test_voice_input_gate.py` read shell/systemd-unit **config** files, not source/log prose — a different, lower-severity coupling (to deploy-script wording), out of the `WakeLoop` blast radius.

## 3. Private-internal coupling

Every `test_voice_daemon_*.py` file except `test_voice_daemon_observability.py` and `test_voice_daemon_wake_funnel_telemetry.py` reaches into `WakeLoop._*` — **~130 unique private attributes/methods, >1,050 references** in total (`monkeypatch.setattr(wl, "_name", ...)` string-literal patches add a handful more not caught by attribute-syntax grep, e.g. `test_voice_daemon_measurement_inflight.py` ×7, `test_voice_daemon_barge_in.py` ×1). Grouped by the concern the decomposition should carve along, so tests can move with their subject:

| Concern | Representative attrs/methods | Files most exposed |
|---|---|---|
| **Measurement pause** | `_measurement_active`, `_measurement_transition_lock`, `_measurement_safety_task`, `_set_measurement_active_local`, `_restore_measurement_step_before_deadline` | `test_voice_daemon_measurement_inflight.py` (dominant), `test_voice_daemon_measurement_gate.py` |
| **Research announce** | `_pending_research`, `_research_window_active/_job/_opening_done/_decided/_cancelled_by_wake`, `_drain_pending_research`, `_queue_pending_research`, `_open_confirmation_window`, `_research_failure_cooldown_sec` | `test_voice_daemon_research_announce.py` |
| **PTT / manual mic** | `_manual_endpoint_this_turn`, `_ptt_input_cap_sec`, `_active_manual_source`, `_push_to_talk_only`, `_manual_mics`, `_manual_mic_loop`, `_corpus_endpointer_label`/`_endpointer_label` | `test_voice_daemon_push_to_talk_endpointer.py`, `test_voice_daemon_manual_start_guard.py` |
| **Peering** | `_peer_arbitrate`, `_peering_current_epoch`, `_notify_peering_session_started/_ended`, `_arbitrate_acquire_drain` | `test_voice_daemon_peering.py` (+ scattered in `manual_start_guard`, `end_turn_reentry`) |
| **Wake legs / fusion** | `_legs`, `_detector`, `_fuser`, `_handle_wake_frame`, `_wake_leg_loop`, `_wake_fire_lock`, `_silero_*_armed_at_ms`, `_current_condition`/`_maybe_refresh_condition` | `test_voice_daemon_wake_dual_stream.py`, `test_voice_daemon_wake_triple_stream.py`, `test_voice_daemon_research_announce.py` |
| **Endpointing** | `_silence_started_at`, `_input_ended`, `_user_speech_seen`, `_server_vad_this_turn`, `_pre_roll`, `_vad` | spread across PTT + barge-in files |
| **Mute** | `_mic_muted`, `_mute_click_on/_off_pcm/_profile`, `_chirp_on/_off_pcm/_profile`, `_warned_cues_unconfigured` | `test_voice_daemon_mute_click.py`, `test_voice_daemon_mute_privacy.py` |
| **Barge-in** | `_barge_in_active`, `_resolve_barge_in_for_turn`, `_barge_in_reference_available`, `_barge_in_run_started_at/_peak`, `_barge_in_no_ref_warned` | `test_voice_daemon_barge_in.py`, `test_turn_playback_barge_in.py` |
| **Turn lifecycle core** | `_state`, `_turn`, `_begin_turn`/`_begin_turn_inner`, `_end_turn`/`_end_turn_inner`, `_cleanup_after_failed_begin`, `_acquiring`, `_acquire_buffer`, `_session_id` | nearly every file — the true god-object seam |
| **Background-task plumbing** | `_bg_tasks`, `_fire_and_forget`, `_create_fire_and_forget_task`, `_cancel_fire_and_forget_tasks`, `_watch_session_tasks`, `_arm_session_task_watcher` | `test_voice_daemon_measurement_inflight.py`, `test_voice_daemon_end_turn_reentry.py` |
| **Conversation capture** | `_record_conversation_turn`, `_conversation_store`/`_conversation_store_path`, `_finalize_event_audio` | `test_voice_daemon_conversation_capture.py` |
| **Cues/output** | `_output_gate` (80 refs — single most-touched attr in the whole suite), `_cues`, `_play_dynamic_text`, `_play_listening_chirp`, `_play_cue`, `_play_mute_click`, `_ducker`, `_content_activity`, `_volume_coordinator` | measurement_inflight, mute_privacy, mute_click, measurement_gate |
| **Config** | `_cfg` (55 refs) | all |

Provider-session files (`openai_session`/`gemini_connection`) show the same pattern one layer down: `_state`, `_reconnect_event`, `_deferred_reconnect`, `_connect_factory`, `_supervisor_task` are poked directly in **both** files (`test_openai_session.py` and `test_gemini_connection.py`) — see §4, this is the exact vocabulary a shared base would need to preserve or the tests break in lockstep.

## 4. Duplication / example clusters / altitude mismatches

- **Parametrizable near-copies, confirmed by reading the code**: `test_aec_on_fire_still_records_fire_aec_on` (`test_voice_daemon_wake_triple_stream.py:183-193`) and `test_aec_off_fire_still_records_fire_aec_off` (`:196-206`) are structurally identical (build a wake loop, feed one frame on `leg=`, assert `trigger_kind`/`peak_score_aec_*`), differing only in leg name and expected field — a clean `@pytest.mark.parametrize("leg,...")` candidate. `test_voice_daemon_wake_dual_stream.py` and `_triple_stream.py` are NOT simple duplicates of each other despite adjacent naming — triple_stream adds a third leg's distinct trigger-kind semantics — but triple_stream also bundles unrelated tests (`session_status` reporting, `read_music_dbfs`, `maybe_refresh_condition`, leg-planning derivation, zero-leg run behavior — lines 335 to end) that have nothing to do with "triple stream" and should be split into their own files before decomposition, not carried along by name.

- **Same behavior pinned at two altitudes, no unit test at the shared altitude**: `jasper/voice/_supervisor.py` (`SupervisedConnection` protocol, `run_reconnect_with_backoff`, `is_transient`, `outage_cue`, `OutageTracker`, `Deferred`, `survive_terminal_initial_connect`) is imported directly by both `test_openai_session.py:28` and `test_gemini_connection.py:32`, and each file re-derives ~30 assertions against it (`test_openai_session.py:2806-2900` "Post-connect reconnect cadence (issue #3855)" vs. `test_gemini_connection.py:1224-1260` "Reconnect nudge (issue #3855)" — the latter's own comment says *"Gemini's wait is its own implementation, so the OpenAI pins do not cover it"*, implicitly admitting the backoff-classification part **is** shared and untested once). **There is no `test_supervisor.py`/`test__supervisor.py`** — the `test_*supervisor*.py` files that exist (`test_grouping_supervisor.py`, `test_shairport_supervisor.py`, `test_supervisor_runtime.py`, `test_supervisor_escalation.py`, `test_supervisor_start_wrappers.py`, `test_system_supervisor.py`) are an unrelated subsystem (name collision, not a wake/session concern). This is the single highest-leverage duplication finding for the "shared base" work: pure functions in `_supervisor.py` (`is_transient`, `outage_cue`, `failure_detail`, `http_status`, `provider_code`, `OutageTracker`) are fully unit-testable without any WebSocket fake and currently have zero direct tests.

- **Good counter-examples already in the suite** (keep as models): `test_aec_bridge_engines.py:175-178` and `test_aec_bridge_corpus_lanes.py:240-243` are single parametrized tests over case tables — exactly the "one property test over an example cluster" pattern the rubric wants. `_tool_pack_contract.py` is a well-factored shared contract module with zero WakeLoop coupling.

- **No moved/stale subjects**: all 994 collected items resolve; every private attribute spot-checked (`_output_gate`, `_measurement_transition_lock`, `_peer_arbitrate`, `_corpus_endpointer_label`, `_watch_session_tasks`, `_mute_click_on_profile`) still exists at the cited line in `jasper/voice_daemon.py`.

## 5. Size

Ten largest (already earned vs. not):

1. `test_openai_session.py` — 3036 lines / 85 tests. Structure (via `# ---` section markers): fake SDK plumbing + pure helpers (~200 lines scaffolding), general connection tests (203-446), **barge-in capability ~1500 lines (447-1955, essentially half the file)**, initial-connect retry budget (1956-2284), Grok-subclass smoke (2285-2382, thin by design), proactive reconnect watchdog (2383-2515), server-VAD (2516-2763), billing meter (2764-2805), reconnect cadence (2806-3036). **Not fully earned**: the retry-budget/reconnect-cadence/proactive-watchdog sections (~700 lines) substantially duplicate `_supervisor.py` behavior also pinned in `test_gemini_connection.py` (§4) — extracting a `test_supervisor.py` would let both provider files shrink to "does this provider wire into the shared loop" (a fraction of current size).
2. `test_voice_daemon_measurement_inflight.py` — 2421 lines / 40 tests, ~60 lines/test. Covers genuinely hard concurrency surface (cancellation ordering across duck/mute-click/cue ownership boundaries during measurement pause) with 6 custom fake classes (lines 67-221) needed to stage races. **Earned** — this is real complexity, not padding — but it is also the single highest private-internal-coupling file in the suite (35 attrs / ~219 refs), so it will need the most rework of any file when `WakeLoop` splits.
3. `test_gemini_connection.py` — 1431/33. Mirrors openai_session's connection-lifecycle/reconnect sections; same duplication-with-`_supervisor.py` concern.
4. `test_voice_setup.py` — 1382/73. Wizard CRUD surface, low coupling to WakeLoop, earned by the breadth of the provider catalog it exercises.
5. `test_audio_io.py` — 1333/47. Earned — many distinct timeout/adapter-lock scenarios at the fanin socket boundary.
6. `test_wake_events.py` — 1200/42. Earned — SQLite schema/migration/retention has genuine combinatorial surface (legs × migrations × retention policies).
7. `test_voice_daemon_push_to_talk_endpointer.py` — 1201/38. Earned; also the model citizen for structured-log assertions (`_log_events.py`).
8. `test_lazy_imports.py` — 1052/19 (55 lines/test). Earned — each test needs its own subprocess harness to get a clean `sys.modules`; this is measuring a real Pi RAM budget, not padding.
9. `test_aec_bridge_stall.py` — 1578/32. Outside the WakeLoop blast radius (Rust AEC bridge); not assessed further here.
10. `test_voice_daemon_wake_triple_stream.py` — 1025/39. Partially earned — the wake-leg-fusion tests are earned; the bundled `session_status`/`read_music_dbfs`/leg-planning tests (§4) are misfiled padding relative to the file's stated subject.

## 6. Gaps

Real, verifiable gaps (checked against actual code, not assumed):

- **Tool timeout, end-to-end through a live turn.** `jasper/tools/__init__.py:818-860` enforces per-tool `asyncio.wait_for(..., timeout=tool.timeout)`, and `tests/test_tools_dispatch.py::test_timeout_returns_error_and_respects_per_tool_budget` (out of this scope's file list but adjacent) pins the dispatch-level contract. **Nothing in `test_voice_daemon_*.py` exercises a tool timing out while `WakeLoop` holds a live turn** — no test asserts what happens to `_turn`/`_state`/TTS output when `dispatch_tool` returns the timeout error payload mid-conversation. `test_voice_daemon_defects.py:174-188`'s `_idle_watchdog` test covers a generic stalled-response timeout, not a tool-triggered one. Grep confirms: no `dispatch_tool`/timeout co-occurrence in any `test_voice_daemon_*.py`.
- **`_supervisor.py` unit coverage** (see §4) — `is_transient`, `outage_cue`, `OutageTracker`'s state transitions, `Deferred` have no dedicated test file; they are only reachable through two full provider-integration suites, so a bug isolated to classification logic surfaces as a failure in either/both 3000-line files with no minimal repro.
- **Listening-cue-before-acquire ordering** is tested only at the mock-choreography altitude: `test_voice_daemon_measurement_inflight.py:1583-1614` (`test_begin_turn_centralizes_feedback_prefix_without_reordering`) monkeypatches `_prepare_assistant_loudness_context`, `_begin_turn_inner`, and `_create_fire_and_forget_task` all away and just records call order — it proves the orchestration skeleton calls things in the right sequence, not that the real listening chirp is audible before the real `acquire_turn()` network call lands. No test exercises this against anything resembling real timing.
- **Provider reconnect mid-turn**: reasonably covered (`test_openai_session.py:1595,1684,1893`; `test_gemini_connection.py:380,886,932,988,1026,1059`) — not a gap.
- **Barge-in truncation timing**: reasonably covered (`test_openai_session.py:456-627`, `test_turn_playback_barge_in.py:387`) — not a gap.

## 7. Migration recommendation

Order the decomposition by which tests are cheapest to keep vs. must be rewritten:

1. **First, before touching `WakeLoop`: extract and unit-test `_supervisor.py`.** Write one `tests/test_voice_supervisor.py` pinning `is_transient`, `outage_cue`, `OutageTracker`, `survive_terminal_initial_connect`, `run_reconnect_with_backoff` directly (pure/near-pure, no WebSocket fakes needed). Then shrink the "Post-connect reconnect cadence" (`test_openai_session.py:2806-3036`) and "Reconnect nudge" (`test_gemini_connection.py:1224-1260`) sections to a couple of "this provider wires the shared loop" integration checks each. This is pure size reduction with no coverage loss and de-risks the "shared base" work directly.
2. **Keep as-is, no rewrite needed**: `test_wake_events.py`, `test_wake_fusion.py`, `test_wake_conditions.py`, `test_audio_buffer_drain.py`, `test_earcons_wire_width.py`, `_tool_pack_contract.py`/`test_tool_pack_contract.py`, `test_tools_registry.py`, `test_lazy_imports.py`, `test_voice_provider_runtime_imports.py`, `test_doctor_voice.py`, `test_control_server_voice.py`, `test_grok_session.py` — all construct their subject through a real public object or a narrow fake at a stable boundary; none reach into `WakeLoop._*`.
3. **`test_voice_daemon_observability.py` is the template, not an outlier** — it already tests free functions lifted into `jasper.voice.daemon_main`. As `WakeLoop` splits, each extracted concern (measurement, research-announce, PTT, peering, mute, barge-in — see §3 table) should get an observability-style file of plain-function tests against the new module, and the corresponding block of `wl._foo` pokes in the old file gets deleted, not translated 1:1.
4. **Rewrite against new seams, in this order, following the concern groups in §3** (each is a self-contained extraction with its own `for_tests()`-style fixture once split out): mute → barge-in → measurement-gate/measurement-inflight (biggest, do last within this batch, since it touches the most cross-cutting state) → PTT/manual-start → research-announce → peering → wake-legs/fusion. For each, first fix the prose-asserting tests in it (§2) to use `tests/_log_events.py`'s `event_fields`/`event_records` — that decoupling should happen *before* the file is moved, since it's independent, low-risk, and prevents the migration diff from being blamed for pre-existing brittleness.
5. **Delete rather than migrate**: the `session_status`/`read_music_dbfs`/`maybe_refresh_condition`/leg-planning tests bundled into `test_voice_daemon_wake_triple_stream.py` (lines ~355 to end) should be split into their own file(s) by actual subject *before* migration, not carried into whichever new module happens to inherit the file's name.
6. **`test_openai_session.py`/`test_gemini_connection.py`**: after step 1 shrinks them, the remaining provider-specific sections (barge-in capability, server-VAD, billing meter) are legitimately provider-specific and should stay as separate files even after a shared base exists — but any test currently poking `conn._state`/`_reconnect_event`/`_deferred_reconnect` directly should be rewritten against whatever public state-query method the shared base exposes (both files already use the *same* private names today, which is the tell that these belong on the base class, not duplicated per subclass).