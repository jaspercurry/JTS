# Recon 04 — the correction/tuning web front end

Scope: `jasper/web/correction_*.py`, `active_speaker_flow.py`, `balance_*.py`,
`deploy/assets/correction/`, and `jasper/web/_common.py` as used by those.
Read-only at `c032503` (branch `claude/busy-goodall-mz0gvv`).

## 0. Sizes and prose ratio (reproducible)

`python3` tokenize pass over `jasper/web/{correction_*,balance_*,active_speaker_flow,_common}.py`
(comments + docstrings counted as prose):

| file | lines | prose | % | code |
|---|---:|---:|---:|---:|
| correction_crossover_v2.py | 7832 | 3535 | **45.1** | 3608 |
| correction_setup.py | 7481 | 1684 | 22.5 | 5077 |
| _common.py | 1385 | 565 | 40.8 | 617 |
| correction_crossover_v2_relay.py | 1100 | 555 | 50.5 | 458 |
| correction_crossover_v2_status.py | 859 | 529 | **61.6** | 260 |
| correction_crossover_v2_wired.py | 1187 | 498 | 42.0 | 581 |
| correction_crossover_v2_republish.py | 375 | 198 | 52.8 | 144 |
| correction_crossover_backend.py | 1749 | 165 | 9.4 | 1435 |
| balance_flow.py | 832 | 148 | 17.8 | 603 |
| correction_crossover_flow.py | 522 | 121 | 23.2 | 347 |
| correction_tuning.py | 336 | 62 | 18.5 | 217 |
| active_speaker_flow.py | 91 | 60 | **65.9** | **14** |
| correction_bass_flow.py | 138 | 32 | 23.2 | 83 |
| correction_crossover_context.py | 64 | 31 | 48.4 | 24 |
| correction_room_flow.py | 437 | 29 | 6.6 | 369 |
| balance_volume_guard.py | 332 | 25 | 7.5 | 269 |
| balance_level.py | 132 | 13 | 9.8 | 99 |
| correction_report.py | 101 | 13 | 12.9 | 72 |
| correction_hub.py | 37 | 8 | 21.6 | 22 |
| correction_measurements.py | 100 | 6 | 6.0 | 74 |
| **total** | **25090** | **8277** | **33.0** | — |

Citation density (`grep -oE '#[0-9]{3,4}'` etc.): `correction_crossover_v2.py`
carries **115 issue numbers, 20 dates, 51 "ruling/§/work order/Wn" references**;
`correction_setup.py` 34/4/20. Every other file in scope is near zero. The
prose problem is two files.

Assets: `deploy/assets/correction/` is 7,004 lines — `js/main.js` 3,647 (one
IIFE, 127 functions, 125 `relay` references), `js/crossover/main.js` 1,137,
`correction.css` 557, `crossover.css` 431, rest small. Structurally fine except
`main.js`'s size, which #3661 step 2/6 already halves-ish.

---

## 1. The headline: the web layer is the engine

`jasper/web/correction_crossover_v2.py` (7,832 lines, 95 top-level defs, 8
classes) **imports nothing web**. Its only two `_common`-looking imports are
`jasper.active_speaker._common` (lines 4281, 4363). No `http.server`, no
`BaseHTTPRequestHandler`, no `jasper.web._common`, no HTML. Same for
`correction_crossover_backend.py`, `_wired.py`, `_relay.py`, `_status.py`,
`_republish.py`, `correction_tuning.py`, `correction_report.py`,
`balance_level.py`, `balance_volume_guard.py`, `active_speaker_flow.py`
(`grep -cE "^\s*(from|import).*(_common|http\.server|BaseHTTPRequestHandler)"`).

**Every HTTP route in the whole correction service is registered in
`correction_setup.py`** — `_POST_ROUTES` at :541 plus the `_make_handler`
dispatch at :6132. So the file named "the v2 crossover conductor's *web host*"
hosts no web at all. Its location is historical.

### 1.1 Engine work in `correction_crossover_v2.py`, quantified

Line spans measured from `^def|^class` to the next such marker:

| concern (belongs in) | lines | principal functions |
|---|---:|---|
| **session assembly** (`crossover_v2_flow` / `crossover_v2/session_seams.py`) | **2089** | `prepare_v2_session` (965), `PositionGate` (322), `resolve_conductor_context` (240), `attach_stage2_preflight` (143), `bind_v2_stage_seams` (129), `bind_v2_engine_seams` (54), `_build_source_run`, `_mint_source_session`, `_assert_stage_2_can_open`, `V2PreparedSession` |
| **save/bank/serialize** (`crossover_v2/record_store.py`, `durable_state.py`) | **1055** | `bind_position_retention` (219), `bind_findings_publisher` (118), `_bank_household_findings` (102), `_publish_findings` (77), `persist_conductor_state` (66), `_persist_terminal_failure` (65), `bind_evidence_publishers` (64), `_capture_evidence_blocks` (60), `bind_cloud_publisher` (51), `open_v2_evidence_store` (25), `_bankable`, `_add_capture_block`, `CaptureEvidenceCarry` |
| **play transaction** (`crossover_v2/playback_transaction.py` — REFACTOR §1: "lives INSIDE measure") | **826** | `bind_production_play` (477), `_bind_engine_measure_leg` (206), `_start_speculative_group_close` (53), `drive_group_close` (27), `ProductionPlay`, `_HeldSession` |
| **volume ownership** (`active_speaker/session_volume_plan.py`, `crossover_v2/volume_claim.py`) | **750** | `_volume_hooks` (173), `reconcile_session_volume_for_new_session` (110), `enforce_session_volume_ceiling_if_stale` (57), `_play_under_session_pause` (50), `_release_pause_best_effort` (43), `recover_session_volume` (42), `_volume_door` (37), `_refuse_without_a_volume_owner`, `_session_volume_claim`, `_session_volume_read`, `acquire/release_session_measurement_pause`, `V2VolumeHooks` |
| **apply / rollback transaction** (engine; REFACTOR §1 says it is *not* a verb but it is not web either) | **644** | `handle_v2_apply` (403), `bind_delta_probe_rollback` (169), `_blocking_apply_issue`, `_dsp_apply_is_known_inactive`, `_consume_auto_revert_pairing` |
| **session-state store** (`crossover_v2/durable_state.py`) | **514** | `reset_v2_journey_state` (136), `observe_apply_success` (101), `save_v2_state` (49), `profile/apply gates`, `load_v2_state`, `clear_v2_state` |
| **prescription / walk staging** (`crossover_v2/prescription_spool.py`, `angle_capture_spool.py`) | **510** | `_take_staged_angle_walk` (310), `_take_staged_prescription` (96), `_resolve_measurement_level_trims` (47), `_verify_plan_shape`, `_hand_released_plan_shape` |
| **grading / verdict** (`crossover_v2/verification.py`) | **390** | `_post_apply_grade` (356), `_spatial_grade` (34) |
| **graph install / inspect** (`crossover_v2/session_graph.py`) | **379** | `ensure_crossover_preview_ready` (70), `_applied_profile_now` (64), `_active_graph_fingerprint` (56), `_applied_graph_boosts` (46), `_previous_candidate_known`, `_resolve_driver_class_by_role`, `_resolve_radiating_diameter_by_role` |
| **analyze binding** (the `analyze` verb) | **172** | `bind_production_analyze` |
| refusal plumbing | 218 | `CrossoverV2Refused`, `classify_program_failure` (76), `refused_from_flow_error` (46), `profile_refusal_code` |
| *categorized total* | **7547** of 7670 def-lines | |

**Zero of it is front-end work.** By the REFACTOR §1 bar — "a front end picks
positions and candidates and shows results; it does not own volume, install a
graph, or decide what banks" — this file violates all three, plus grading and
serialization.

### 1.2 The twin, precisely

`jasper/active_speaker/crossover_v2_flow.py::CrossoverV2Session` is 156 methods
with a **44-keyword constructor** (`crossover_v2_flow.py:1516-1600+`).
`jasper/web/correction_crossover_v2.py::prepare_v2_session` (:6105, **965
lines**, of which ~120 are the docstring) is the only thing in the tree that
knows how to fill those 44 kwargs. So the engine's session object **cannot
construct itself**; a web module owns its construction contract. There is no
engine twin *function* to point at — that is the finding: the assembly half of
the session lives at the wrong altitude, and the seam modules that should host
it (`crossover_v2/session_seams.py` 274 lines, `composition.py` 255,
`playback_transaction.py` 220, `session_graph.py`) are stubs by comparison.

`prepare_v2_session`'s own docstring admits the shape: *"Exactly nine of them
read `verify_only`"* — a 965-line function with nine internal forks is not one
function, it is a class that was never written.

Consequence: `tests/test_correction_crossover_v2_endpoints.py` is **9,309
lines**. A thin front end does not need a 9k-line endpoint suite.

### 1.3 Engine work in `correction_setup.py`

Same measurement, grouped by concern (relay-flavoured members flagged — see §2):

| concern | lines | note |
|---|---:|---|
| mic-calibration domain (`_save_household_mic`, `_resolve_relay_calibration` 132, `_stored_calibration_model_mismatch` 57, the 3 binding dataclasses, …) | 823 | ~half is relay-setup binding, dies with #3661; the rest belongs in `jasper/audio_measurement/calibration.py` / `jasper/correction/household_mic.py` |
| level-match transaction (`_run_relay_level_match` **562**, `_handle_relay_level_match` 100) | 662 | largest function in the file; #3661 claims the relay leg |
| graph install / snapshot / reset (`_load_measurement_baseline` 123, `_resolve_reset_target_async` 91, `_write_no_room_correction_config` 66, `_snapshot_running_room_graph` 61, `_running_graph_snapshot_text` 49, `_restore_protected_neutral_program_graph` 39, `_maybe_auto_revert` 38, `_run_locked_room_reset` 34, `_pre_measurement_restore_target` 51) | 590 | belongs in `jasper/correction/` |
| readiness / decide (`_normalize_room_readiness` **161**, `_assert_room_authority_current` 60, `_classify_live_bass_extension_graph` 37, `_room_readiness` 25, `_RoomReadiness`) | 354 | pure decision code |
| volume ownership (`_maybe_restore_main_volume` 114, `_crossover_volume_safety_refusal` 78, `_household_level_door` 58, `_assert_level_match_level` 51, `_enforce_session_volume_ceiling` 30, `_take_autolevel_claim` 22) | 353 | the fourth and fifth volume owners in this scope |
| mutex/slot (`_reserve_start_slot`, `active_correction_phase`, `_claim_crossover_state_owners` 52, …) | 128 | fine where it is, but see §5 `active_speaker_flow.py` |
| **engine-shaped subtotal** | **~2910** | |

`CrossoverLevelLease` in `correction_crossover_backend.py:136` is **926 lines in
one class** — durable volume-safety state, ramp config, level-match runs,
per-driver locks, repeat sessions, snapshots. Its own docstring calls it "this
thin domain owner". It is a domain object in `jasper/web/`. Nothing about it is
HTTP.

---

## 2. Overlap with in-flight work (do not double-count)

PR **#3724** (`pull_request_read get_files`, 22 files) touches, in my area:

| file | +/− |
|---|---|
| `jasper/web/correction_crossover_v2.py` | +84 / −296 |
| `jasper/web/correction_crossover_v2_wired.py` | +46 / −79 |
| `jasper/web/correction_setup.py` | +2 / −54 |
| `jasper/web/correction_room_flow.py` | +4 / −11 |

Net ≈ −304 lines in scope. It collapses the per-source fork
(`_resolve_prepare_capture_source`, `_hand_released_plan_shape`,
`_mint_source_session`, `_build_source_run` — 174 lines of the "session
assembly" bucket) and the room `capture_transport`.

Issue **#3661** (read via `issue_read`) is broader than the brief states. Its
"What goes" list explicitly includes **"the `/relay/*` and `/sync/relay-capture`
routes in `correction_setup.py`"** and **"relay branches in
`deploy/assets/correction/js/main.js`"**. Measured against HEAD that claims:

- `correction_crossover_v2_relay.py` — **1,100 lines**
- relay-named defs in `correction_setup.py` — **2,306 lines across 41 defs**
  (`_run_relay_level_match` 562, `_handle_relay_capture` 189, `_handle_relay_verify`
  177, `_run_relay_capture` 140, `_resolve_relay_calibration` 132, `_relay_failure_message`
  93, …)
- ~125 relay references in `deploy/assets/correction/js/main.js`

**Total already claimed by #3661/#3724 in my area: ≈ 3,700 lines.** Everything
below is stated *net of that*.

One orphan #3661 does **not** list: `deploy/assets/shared/js/qr.js` (**1,440
lines**) has exactly two consumers, `renderRelayQr` in `correction/js/main.js`
and `correction/js/crossover/main.js`, both relay-QR call sites. It dies with
the relay. Flag it to the relay PR chain — risk low, check is
`grep -rn "qr.js\|renderRelayQr" deploy/assets jasper`.

---

## 3. Route table

All routes register in `correction_setup.py` (`_POST_ROUTES` :541 + `_make_handler`
:6132). Reachability checked against `deploy/assets/**` and against server-supplied
`endpoint` strings (the crossover UI posts `action.endpoint` from the envelope —
`crossover/js/main.js:975`; those come from
`jasper/active_speaker/crossover_envelope_v2.py`).

| route | reached from | verdict |
|---|---|---|
| `/`, `/room`, `/crossover`, `/bass`, `/measurements`, `/balance`, `/sync` (pages) | nav + `correction_hub.SECTIONS` | live |
| `/status`, `/envelope`, `/entry-status`, `/sessions`, `/session-report`, `/session/delete` | `correction/js/main.js` | live |
| `/start`, `/next-position`, `/repeat-position`, `/verify`, `/apply`, `/reset` | `correction/js/main.js` | live |
| `/upload-noise`, `/upload-capture`, `/local-capture/setup` | `correction/js/main.js` | live |
| `/autolevel/start`, `/autolevel/lock`, `/autolevel/cancel` | `correction/js/main.js` | live |
| `/calibration/fetch`, `/calibration/upload` | `correction/js/main.js` | live |
| `/interpret`, `/propose`, `/propose/apply` | `correction/js/main.js` + `correction_tuning.py` | live |
| `/measurements/data` | `measurements.js:54` builds relative `data?a=…` | live (my first grep false-negatived it) |
| `/bass/status` | `bass/main.js` | live |
| `/crossover/status`, `/crossover/envelope`, `/crossover/reset`, `/crossover/relay-cancel` | `crossover/main.js`, `arm_walk.py`, `wizard_client.py` | live |
| `/crossover/v2/{session,verify,apply,republish,decline,complete,retake}` | `crossover_envelope_v2.py` emits each as `endpoint` | live (envelope-driven, not grep-visible in JS) |
| `/crossover/v2/position-ready` | `arm_walk.py` + `tests/js/crossover_wired_walk_test.mjs` | live (LLM/arm path only) |
| `/crossover/recover-volume` | `crossover_envelope_v2.py` endpoint | live |
| `/balance/*`, `/sync/*` | `deploy/assets/balance/js/main.js`, `deploy/assets/sync/js/main.js` | live (assets outside `correction/`) |
| `/healthz` | systemd/nginx probe | live |
| `/relay/capture`, `/relay/level-match`, `/relay/verify` | `correction/js/main.js` | **deleted by #3661 step 2** |
| **`/test-tone`** | nothing — only `tests/test_web_correction_setup.py` | **DEAD** (handler :3319, 23 lines; sole caller of `jasper/correction/playback.play_test_tone`) |
| **`/calibration/models`** | nothing anywhere | **DEAD** (handler :3350, 10 lines) |

No route is registered outside `correction_setup.py`; there is no second router
to reconcile. That part is clean.

---

## 4. `correction_setup.py` at 7,481 lines — what it is

It is a **grab-bag**, and the module docstring says so out loud
(`correction_setup.py:25`): *"HTTP routes … this module now serves far more
routes than fit a comment table."* By function-name partition:

| region | lines | defs |
|---|---:|---:|
| room wizard + misc | 2656 | 69 |
| relay / phone capture | 2306 | 41 |
| `_make_handler` (one nested `Handler` class) | 1119 | 1 |
| mic calibration | 414 | 14 |
| crossover shims | 330 | 10 |
| server plumbing (`make_server`, `main`, `_run_async`, …) | 210 | 9 |
| autolevel | 200 | 5 |
| sync | 130 | 2 |

It hosts **five** different products: the room-correction wizard, the crossover
wizard's route shims, bass, pair balance, and pair sync — plus the process
(loop, session singleton, systemd idle-hold, `main()`).

### Proposed split

1. **`correction_server.py`** (~450) — `main`, `make_server`, `_make_handler`'s
   dispatch table, `_ensure_loop`/`_run_async`, `_camilla`, idle-hold,
   `_reserve_start_slot`. The router becomes a `dict[str, handler]` instead of
   an 1,119-line `if/elif` ladder; the ladder is why `_POST_ROUTES` needed a
   comment explaining that deleting a line silently 404s a route (:541).
2. **`correction_room_routes.py`** (~500) — the `_handle_*` room wizard
   handlers, *after* their bodies push graph/readiness/volume work down (§1.3).
3. Keep `correction_room_flow.py` / `correction_crossover_flow.py` /
   `correction_bass_flow.py` / `correction_measurements.py` as they are —
   these four are already correct: page render + status projection, 6–23% prose,
   no engine work. They are the model the rest should copy.
4. **Move down** (§1.3): ~2,910 lines of graph/readiness/volume/calibration to
   `jasper/correction/` and `jasper/audio_measurement/`.
5. Balance/sync route shims move to `balance_flow.py` / `sync_flow.py`, which
   already own their handlers (`balance_flow.handle_start/handle_meter/…`) —
   the shim in `_make_handler` is a pure `elif` relay.

After #3661 (−2,306) and the moves (−2,910) and the split (−450 out),
`correction_setup.py` lands at **~1,300–1,500 lines**.

---

## 5. Duplication

| A | B | verdict |
|---|---|---|
| `correction_setup._serve_json_route` (:6142, wraps route failures into a 500 JSON body) | `_common.send_route_failure` (:1358, same concern, 502, `log_event`) | Two route-failure primitives, one per wizard. `send_route_failure`'s only consumer is `sound_setup.py` (9 call sites). Converge on one; ~25 lines. |
| `correction_crossover_v2._candidate_summary` / `_cloud_summary` (:1834-1835) | `crossover_v2/durable_state._candidate_summary` (:1092) / `_cloud_summary` (:1210) | The real code already moved; these two lines are compat re-export aliases. One consumer (`correction_crossover_v2_republish.py:225` calls `_host._candidate_summary`). Import from `durable_state` and delete both aliases. |
| `correction_crossover_v2.py` volume plan (750 lines) | `active_speaker/session_volume_plan.py` (1,291) + `crossover_v2/volume_claim.py` (280) + `correction_crossover_backend.CrossoverLevelLease`'s volume-safety block (~180) + `correction_setup` volume block (353) | **Four** volume owners inside the tuning scope. REFACTOR §1 demands one. This is the single worst duplication in my area. |
| `correction_crossover_v2_status.py` imports the host **by module object** (`from jasper.web import correction_crossover_v2 as _host`) and the host imports the status block back from inside a function | — | A deliberate two-way import managed by late binding, documented in a 19-line docstring. It exists only because both halves are in `jasper/web/`. Disappears when the projection moves next to `crossover_envelope_v2.py`. |
| `correction_crossover_context.py` (64 lines, one frozen dataclass, one producer `resolve_conductor_context`, one consumer `_volume_hooks`) | — | A module that exists only to break the cycle above. Merge into the engine session module. |

**`_common.py` is not a problem for this area.** The correction/balance stack
uses only 13 of its ~50 exports (`canonical_page` ×13, `canonical_header` ×10,
`begin_request` ×8, `read_json_object` ×4, `JsonBodyError` ×4,
`bonded_follower_active` ×4, `guard_mutating_request` ×3, plus 2 each of
`send_json_response`, `send_html_response`, `reject_csrf`, `json_island`,
`guard_read_request`, `bonded_follower_leader_web_url`). No correction module
re-rolls a `_common` helper. `_common.py`'s own 40.8% prose ratio is a separate
report's problem.

---

## 6. Prose over the AGENTS.md bar

Bar: *"only non-derivable constraints (units, ranges, timing, hardware quirks)
and `why`-pointers … no narration of what code does, no history, no
dates/PR numbers, no text addressed to a reviewer."*

**Example 1 — `jasper/web/active_speaker_flow.py:5-48.** A 44-line module
docstring over a 91-line file whose entire executable content is **14 lines**
(two functions that read one dict key and check three others in order):

> "These checks are advisory and NON-ATOMIC: unlike the `_window_active` mutex
> (which serializes the other three among themselves), a sub-second
> start-vs-start race between commission-load and a correction/balance/sync
> start can slip both past their checks. The cost of losing that race is one
> corrupted measurement someone re-runs — never unsafe output, per the
> protections above — so a cooperative check is the right weight rather than a
> heavier shared lock."

Design-review argument, not a constraint. The whole module is 2 functions;
fold them into the mutex owner with a 2-line pointer to the ADR.

**Example 2 — `correction_crossover_v2.py:1508-1530`** (`_post_apply_grade`,
356 lines of which ~150 are docstring), narrating an incident with a date and a
measured number:

> "a post-apply group that closed with `overall_passed=False` reaches
> `GRADE_GRADED`, because a graded-and-failed group IS graded. It also rendered
> as 'applied and graded' — measured on jts3 2026-08-07, a −4.63 dB spatial
> miss under a green tick."

That belongs in the ADR/PR that fixed it. AGENTS.md: "no history, no dates/PR
numbers."

**Example 3 — `correction_crossover_v2.py:66-78`**, a 13-line comment on an
`import` statement:

> "EAGER, unlike every other `jasper.active_speaker` import here … The cost is
> real and worth stating: the `crossover_v2` package's convenience re-exports
> pull `branch_chain` and with it numpy, so importing THIS module went from
> ~0.05 s to ~0.34 s. It is paid by nobody new … The three `X as X` lines are
> PEP 484's redundant-alias form: they are re-exports this module names but
> never calls, and the alias is what says so without spending suppression debt
> the tree is actively paying down."

Two sentences of real constraint (import cost, PEP 484 alias) buried in a
paragraph of self-justification.

Honourable mention: `prepare_v2_session`'s ~120-line docstring (:6113-6230)
which contains "ONE entry point, two shapes (two-stage commission work order D2,
owner-confirmed 2026-07-29)". Owner-confirmation dates in code.

**Estimated recoverable prose, net of #3661:** ~2,400 lines from
`correction_crossover_v2.py` (target ~15% ratio, matching the healthy files in
the same directory), ~700 from `correction_setup.py`, ~350 from
`correction_crossover_v2_status.py`, ~250 from `_wired.py`, ~120 from
`_republish.py`, ~45 from `active_speaker_flow.py`. **≈ 3,900 lines.** Risk:
low — no behaviour. Check: `scripts/test-merge` (no test may assert on prose;
AGENTS.md forbids it, worth verifying with
`grep -rn "docstring\|__doc__" tests/test_correction*`).

---

## 7. Dead code (zero callers)

Verified with `grep -rn "\bNAME\b" --include=*.py jasper/ deploy/ scripts/ tests/`:

| symbol | file:line | lines | callers |
|---|---|---:|---|
| `begin_commissioning_run` | `correction_crossover_backend.py:1159` | 28 | **zero, including tests** |
| `_bind_relay_setup` | `correction_setup.py:2094` | 17 | **zero, including tests** |
| `_begin_relay_commit` | `correction_setup.py:785` | 24 | tests only (3 asserts) — dies with #3661 |
| `_begin_relay_finishing` | `correction_setup.py:809` | 23 | tests only (2 asserts) — dies with #3661 |
| `_handle_test_tone` + `/test-tone` route + `jasper/correction/playback.play_test_tone` | `correction_setup.py:3319` | 23 + ~40 | no UI reference; tests only |
| `_handle_calibration_models` + `/calibration/models` route | `correction_setup.py:3350` | 10 | no reference anywhere |
| `ProductionPlay.__call__` | `correction_crossover_v2.py:3621` | 11 | self-documented test-only shim: *"Delete it when they stop."* |
| `_candidate_summary` / `_cloud_summary` aliases | `correction_crossover_v2.py:1834-1835` | 2 | one indirect consumer; import from `durable_state` instead |
| `deploy/assets/shared/js/qr.js` | — | 1440 | two relay-QR call sites only — orphaned by #3661 |

Subtotal (excluding qr.js and the #3661-claimed relay pair): **~131 Python
lines**, low risk. Small, but they are exactly the "random stuff that adds no
value" the owner named.

Not dead but worth naming: `/crossover/v2/position-ready` is reachable **only**
from `jasper/active_speaker/arm_walk.py` (the LLM/arm front end) and a JS test
harness. That is correct per REFACTOR §1 — two front ends, same verbs — but it
means the wired human walk and the arm walk already share this route, which is
proof the thin-front-end model works when someone builds it.

---

## 8. Stale prose that contradicts HEAD

`jasper/active_speaker/` carries **30+ prose references pointing *up* into
`jasper.web`** (`grep -rn "correction_crossover_v2\|correction_setup"
jasper/active_speaker/`). The import graph is clean —
`test_correction_boundary_ssot` enforces no upward import — but the *documentation*
graph is inverted: the truth layer explains itself by pointing at the web layer.
Seven of those are now **wrong**:

- `crossover_envelope_v2.py:295, 310, 335, 413, 420, 506` and
  `crossover_v2_flow.py:5863` all say `_candidate_summary` lives in
  `jasper.web.correction_crossover_v2`. It moved to
  `crossover_v2/durable_state.py:1092`; only a 1-line alias remains.
- `crossover_v2_flow.py:6295` says `_cloud_summary` is
  "`jasper.web.correction_crossover_v2`'s". Same — `durable_state.py:1210`.
- `driver_acoustics.py:678` reasons about "`jasper.web.correction_setup`'s route
  table" — a route table that no longer exists as a table (see the docstring at
  `correction_setup.py:25` admitting it outgrew one).
- `crossover_envelope_v2.py:2624` and `capture_geometry.py:376` cite
  `correction_setup._begin_relay_capture` / `_relay_calibration_from_setup`,
  both about to be deleted by #3661.

Per AGENTS.md ("when you can't verify a comment against the code, delete it"),
these are net-negative today. ~35 lines, risk nil.

---

## 9. Proposed end state

**Survives in `jasper/web/` (front end only — picks WHAT and WHERE):**

| module | owns | est. lines |
|---|---|---:|
| `correction_server.py` (from `correction_setup.py`) | process, loop, systemd idle-hold, CSRF/guards, **route table → handler dict** | ~450 |
| `correction_room_routes.py` | room wizard request/response only | ~500 |
| `correction_room_flow.py` | room page render + follower page | 437 (unchanged) |
| `correction_crossover_flow.py` | crossover page render + envelope/status/reset/decline routes | ~520 (unchanged) |
| `correction_bass_flow.py` | bass page + status | 137 (unchanged) |
| `correction_measurements.py` | measurements page + `build_data` | 99 (unchanged) |
| `correction_report.py` | session bundle report payload | 100 (unchanged) |
| `correction_hub.py` | section tabs | 36 (unchanged) |
| `balance_flow.py`, `balance_level.py`, `balance_volume_guard.py` | pair balance | ~1,290 (unchanged; healthy prose ratios) |
| `correction_tuning.py` | LLM advisor spend gate + `interpret`/`propose` | 335 (candidate to move to `jasper/tools/`, low priority) |
| **web total** | | **≈ 3,900** (from 25,090) |

**Moves into the engine** (`jasper/active_speaker/crossover_v2/`,
`jasper/correction/`, `jasper/audio_measurement/`) — arriving smaller because
the prose comes off in transit:

| new/target home | absorbs | est. lines after trim |
|---|---|---:|
| `crossover_v2/session_assembly.py` (or `CrossoverV2Session.open()` classmethod) | `prepare_v2_session`, `resolve_conductor_context`, `V2ConductorContext`, `attach_stage2_preflight`, `bind_v2_*_seams`, `V2PreparedSession` | ~1,100 (from 2,089) |
| `crossover_v2/position_cycle.py` (exists) | `PositionGate` | +200 (from 322) |
| `crossover_v2/verification.py` (exists) | `_post_apply_grade`, `_spatial_grade` | +180 (from 390) |
| `crossover_v2/playback_transaction.py` (exists, 220) | `bind_production_play`, `_bind_engine_measure_leg`, `ProductionPlay` | +480 (from 826) |
| `crossover_v2/record_store.py` + `durable_state.py` (exist) | all `bind_*_publisher`, `persist_conductor_state`, `_bank_household_findings`, `_capture_evidence_blocks` | +600 (from 1,055 + 514) |
| `active_speaker/session_volume_plan.py` (exists, 1,291) | `_volume_hooks`, the pause/claim/door/ceiling set, `correction_setup`'s volume block, `CrossoverLevelLease`'s volume-safety half | +550 (from 750 + 353 + ~180) — **and three owners become one** |
| `crossover_v2/apply_transaction.py` (new) | `handle_v2_apply`, `bind_delta_probe_rollback` | ~400 (from 644) |
| `active_speaker/crossover_level_lease.py` (new) | `CrossoverLevelLease` minus its volume half | ~600 (from 926) |
| `crossover_v2/capture_wired.py` | `correction_crossover_v2_wired.py` | ~600 (from 1,187) |
| `crossover_envelope_v2.py` (exists) | `correction_crossover_v2_status.py`'s projection | +300 (from 859) |
| `jasper/correction/graph.py` (new) or existing `jasper/correction/*` | room graph install/snapshot/reset/readiness | ~700 (from 944) |
| `jasper/audio_measurement/calibration.py` / `jasper/correction/household_mic.py` (exist) | non-relay half of the calibration domain | ~350 (from ~420) |

**Deleted outright:** `correction_crossover_v2.py` (7,832 → 0, dissolved),
`correction_crossover_v2_relay.py` (1,100, #3661),
`correction_crossover_context.py` (64, merged),
`active_speaker_flow.py` (91, merged into the mutex owner),
`correction_crossover_v2_republish.py` (375 → ~150 in the engine).

**Rough arithmetic.** In-scope Python today: 25,090. #3661/#3724 claim ~3,700.
Prose trim ~3,900. Dead code ~131. Structural dedup (four volume owners → one,
the twin's assembly collapsing into a constructor, the status/host cycle
disappearing) ~1,500. Net: **≈ −9,200 lines**, landing at ~3,900 in
`jasper/web/` plus ~6,000 relocated into the engine. Confidence on the prose
and dead-code numbers is high; on the structural-dedup number, ±40%.

---

## 10. Top moves, ranked

| # | move | Δ lines | risk | proof |
|---|---|---:|---|---|
| 1 | **Land #3724 + the #3661 chain unchanged.** Add `deploy/assets/shared/js/qr.js` (1,440) to the sweep. | −3,700 (−5,100 with qr.js) | low | already in flight; `scripts/test-merge` |
| 2 | **Prose pass on `correction_crossover_v2.py` alone** (45.1% → ~15%). Strip the 115 issue numbers, 20 dates, 51 ruling/§ references; keep unit/range/timing constraints and one-line ADR pointers. Do it *before* moving code, so the move is a clean `git mv`-shaped diff. | −2,400 | low | `scripts/test-merge`; no test may assert prose |
| 3 | **Delete the dead 9** (§7): `begin_commissioning_run`, `_bind_relay_setup`, `/test-tone` + `play_test_tone`, `/calibration/models`, `ProductionPlay.__call__`, the two `_durable` aliases. Delete the 9 stale upward-pointing comments in `jasper/active_speaker/` (§8). | −170 | low | grep shown in §7; `scripts/test-fast` |
| 4 | **Collapse the four volume owners into `session_volume_plan.py`.** This is a non-negotiable-tier diff (hearing clamp) — needs `/adversarial-review`. | −450 net, 4 owners → 1 | **high** | non-negotiable #1: `volume_limit` 0.0 in every CamillaDSP config, `set_volume_db` positive clamp; commissioning SPL stop; hardware pass on a Pi |
| 5 | **Move the grading, save/bank and status-projection blocks into the engine** (`verification.py`, `record_store.py`/`durable_state.py`, `crossover_envelope_v2.py`). Pure relocation; kills the `_status.py`↔host module-object cycle and `correction_crossover_context.py`. | −700 net, ~1,900 relocated | med | `test_correction_boundary_ssot`; `test_correction_crossover_v2_endpoints.py` should *shrink* — if it doesn't, the move was incomplete |
| 6 | **Turn `prepare_v2_session` into `CrossoverV2Session.open(...)`** — the 44-kwarg constructor absorbs its own assembly; the nine `verify_only` forks become two classmethods (`open_measure` / `open_verify`). This is the strangler's last mile. | −900 net, ~2,100 relocated | **high** | `test_crossover_v2_stage_bridge.py`, `test_crossover_v2_remote_tier.py`, `test_correction_crossover_v2_endpoints.py`; hardware pass |
| 7 | **Split `correction_setup.py`** into `correction_server.py` (router as a dict) + `correction_room_routes.py`, pushing graph/readiness/calibration down. Do it *after* 1 and 5, when the file is already ~4,000. | −450 net, ~2,900 relocated | med | route-membership test currently pinned via `_POST_ROUTES`; keep it against the dict |
| 8 | **Move `CrossoverLevelLease` (926) to `jasper/active_speaker/`.** Mechanical. | 0 net, 926 relocated | low | `test_correction_level_match.py` |
| 9 | **Prose pass on the rest** (`correction_setup`, `_status`, `_wired`, `_republish`, `active_speaker_flow`). | −1,500 | low | `scripts/test-merge` |
| 10 | Fold `active_speaker_flow.py`'s two functions into the measurement-window mutex owner; delete the module. | −77 | low | `scripts/test-fast` |

Order matters: **1 → 2 → 3 → 9** are independent and safe and should land first
(they remove ~5,400 lines with no behaviour change and make every later diff
readable). **5 → 8 → 7** are relocations. **4 and 6** are the two that need
hardware validation and the non-negotiable-tier review; do them last, one PR
each, and do not combine them.

### Uncertainty

- The line estimates for moves 4–7 assume the prose pass (move 2) lands first;
  if it doesn't, every relocation diff is ~2× larger and much harder to review.
- I did not read `deploy/assets/correction/js/main.js` closely (out of scope per
  the brief) — the 3,647-line figure and the 125 relay references are structural
  only. Whether the non-relay remainder wants splitting is a separate question.
- `correction_tuning.py` (the LLM spend-capped advisor) sits oddly in `jasper/web/`
  but it *is* only reachable from two routes and has a clean 18.5% prose ratio.
  Leaving it is defensible; I have not costed a move.
- I could not verify whether any test asserts on docstring text. `grep -rn
  "__doc__" tests/` before move 2.
