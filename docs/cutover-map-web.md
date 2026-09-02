# Dissolution map — `jasper/web/correction_crossover_v2.py`

> **This document dies when tier 7 completes.** It maps one file, concern by
> concern, so the executors cutting that file up can find every seam without
> re-reading 8,088 lines. When the file is gone the map has no subject: delete
> it, and its `docs/doc-map.toml` row with it. Do not grow it into a design doc
> — [`crossover-v2-engine-design.md`](crossover-v2-engine-design.md) owns the
> engine's shape and [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md)
> owns the schedule and the work-item ids. This is a map, and only a map.

Scope is this file. The twin, `jasper/active_speaker/crossover_v2_flow.py`, is
mapped separately; this one names it only where a cut crosses between them.

---

## 0. Method, and what the measurement changed

### How every number here was taken

Ranges come from the file's own AST at `c253c3cf1`, not from a prior inventory.
Prose is `tokenize` COMMENT tokens unioned with every docstring span, so a line
carrying both is never counted twice. Callers were followed to their sites
rather than inferred from an import list.

Run from the repo root. Prints `8088 lines; 3796 prose`:

```python
import ast, tokenize
p = "jasper/web/correction_crossover_v2.py"
src = open(p).read(); lines = src.splitlines(); tree = ast.parse(src)
com = {t.start[0] for t in tokenize.tokenize(open(p, 'rb').readline)
       if t.type == tokenize.COMMENT}
doc = set()
HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
for n in ast.walk(tree):
    if not isinstance(n, HOLDERS) or not n.body:
        continue
    d = n.body[0]
    if isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) \
       and isinstance(d.value.value, str):
        doc |= set(range(d.lineno, d.end_lineno + 1))
print(len(lines), "lines;", len(com | doc), "prose")
```

*(The `HOLDERS` guard is load-bearing, not tidiness: `ast.walk` also reaches
`Lambda` and `IfExp`, whose `.body` is a single node rather than a list, and an
unguarded `body[0]` raises on this file.)*

**Result: 8,088 lines. 3,796 prose lines (1,533 comment + 2,263 docstring,
deduped) = 46.9%. 685 blank. 3,861 lines of actual code.** So the file is
under 3,900 lines of logic wearing 4,200 lines of packaging.

### The premise, re-verified

The cutover plan's §0 corrected the handed brief: this file is not a page. That
still holds at `c253c3cf1`, and it is the single most load-bearing fact in this
map, so it is re-measured rather than cited:

| Claim | Grep | Count |
|---|---|---|
| zero route decorators / handlers | `@(app\|router)\.(get\|post\|route)\|def do_(GET\|POST)\|add_route\|path == ` | **0** |
| zero HTML / DOM | `<div\|<html\|<script\|<style\|innerHTML\|document\.getElementById` | **0** |
| zero CSRF / static assets / socket writes | `csrf\|_static\|send_header\|wfile` | **5, all false** — every hit was the substring `_static` inside `restore_anchor_static_prefix_refusal` (deleted 2026-08-31 with the restore verb; **0** at HEAD) |

**It is an orchestration host.** Routes are `correction_setup.py`'s
`_dispatch_crossover` (`:7366`), reached from the funnel at **`:8015`** — §5
says `:8014`, and it drifted a line. The loop
bridge is `_ensure_loop` (`:1275`) / `_run_async` (`:1292`). Every entry point
below is a plain `def` that the dispatch chain calls, and every import of this
module from `correction_setup.py` is **lazy, inside a function body** — there
is no module-scope edge between them in either direction.

### What this measurement changed against §6

§6 says its own ranges are approximate and asks to be re-derived. It survives
well; four things moved.

1. **§6's table starts at line 189. Lines 1–188 are unmapped** — 188 lines of
   docstring, imports and a re-export block that is itself a barrel (row B).
2. **There is a fifth re-export barrel, in this file, and no §6 row covers it**
   — `:2559-2578`, sixteen names re-bound off `durable_state` under their
   historical names (row U). §6's deletion order step 1 kills four barrels in
   the twin; this is the fifth and it is the same shape.
3. **§7's kept-lines figure double-counts.** `:6904-7360` is **457** lines, not
   the 1,185 §7 labels it with; 1,185 is `:6904-8088`, which already contains
   the `:7363-8088` tail §7 then adds again. Honest sum of §7's own three kept
   ranges: **1,380**, not ≈2,000–2,400. See §4 — it moves the floor bracket by
   about 600 lines in the project's favour.
4. **The module-level mutables are five, not four.** §6 names `:235`, `:1126`,
   `:1127`, `:1191`. `_state_path_override` (`:232`) is a fifth, mutated through
   `global` at `:460`. It is a test seam, which is presumably why §6 skipped it
   — but a mover who relocates "the four" and leaves it behind splits the
   durable-state globals across two modules. See §3.

Everything else in §6's web-file table reproduced within a line or two.

**One structural fact neither §5 nor §6 carries, and it changes the blast
radius:** the production caller surface is **38 names across 8 modules**, not
the handful the dispatch chain suggests. Three of the eight are outside the
`/correction` web tier entirely — `correction_crossover_v2_republish.py`
(which reaches **four private names**), `jasper/cli/doctor/correction.py`, and
`scripts/run-crossover-round.py`. A caller census done by grepping the
`v2host.` alias finds none of them, because they bind the host to `_host`, take
it as a function parameter, or import symbols directly. Enumerate by import
*statement*; see the table at the end of §1.

---

## 1. Concern inventory

47 rows, **contiguous and exhaustive**: row *n*'s start is row *n−1*'s end plus
one, and the last ends at 8,088. Nothing is double-counted and nothing is
missing, which is what makes §4's arithmetic checkable.

**Caller codes.** `setup` = `jasper/web/correction_setup.py` · `backend` =
`correction_crossover_backend.py` · `xflow` = `correction_crossover_flow.py` ·
`relay` / `wired` / `republish` = `correction_crossover_v2_relay.py` /
`_wired.py` / `_republish.py` · `doctor` = `jasper/cli/doctor/correction.py` ·
`v2-status` = `correction_crossover_v2_status.py` (rows Q/R/S, lifted by
#3286) · `in-file` = no consumer outside this module · `tests` = test
suite only.

**Disposition codes.** `→W*` absorbed by that plan work item · `SUPERSEDED` the
engine module named already owns it · `KEEP` survives the dissolution, with
where it lives · `LIFTS WHOLE` / `OWN MODULE` moves intact to a new file.
There is deliberately no `NO HOME` row — see §4 for why this file has none,
unlike its twin.

| # | Lines | *n* | Concern — what it does | Called by | Disposition |
|---|---|--:|---|---|---|
| **A** | 1–43 | 43 | SPDX header + module docstring. Describes the file as the host between the v2 POST routes and the pure conductor. | — | **KEEP**, shrunk. The surviving apply host needs ~8 lines of it; the other 35 describe organs that will have left. |
| **B** | 44–161 | 118 | Imports, and a **re-export barrel** inside them: 6 names re-exported with PEP 484 `X as X` redundant-alias form (3 from `journey` at `:77`, `:78`, `:80`; 3 from `relay` at `:157-159`) that this module names but never calls. The comment at `:63-71` records the cost: the eager `crossover_v2` import pulls `branch_chain` and numpy, taking import time **0.05 s → 0.34 s**. | `journey`/`refusal_copy`/`capture_source`/`verification` names reached through here by `setup`, `relay`, `wired`, tests | **SUPERSEDED** — every re-exported name already lives in a `crossover_v2/*` organ. Repoint importers at the organ and the barrel goes. Precedent in-file: `:150-155` records three such names already deleted once no external caller arrived. |
| **C** | 162–188 | 27 | `logger`; durable-state schema constants (`STATE_SCHEMA_VERSION`, `STATE_KIND`, `DEFAULT_V2_STATE_PATH`); the two relay-kind labels; two more `durable_state` re-exports (`:186-187`). | setup, tests | **SPLIT** — schema constants →W1-a with the store; relay-kind labels →W5-a with the preparers; `:186-187` join row U's barrel. |
| **D** | — | — | Operator capture-dump retention: dir, `ENABLED` marker filename, ring caps (90 files / 300 MB). 26 of 31 lines were the justifying essay. **Gone** as of #3250 — deleted whole, not migrated. | — | **EXECUTED BY #3250** — the owner's ruling was to drop operator retention; the banked record store is the only retention path. |
| **E** | — | — | `capture_dump_enabled` — was operator retention switched on right now. **Gone** as of #3250, with row D's marker it read. | — | **EXECUTED BY #3250** |
| **F** | 230–236 | 7 | `_state_lock` (RLock), `_state_path_override`, `_volume_plan_lock`, `_volume_plan` — four of the file's seven module-level mutables. | in-file; **republish `:205`** holds `_state_lock` | **→W5** with the singletons they guard. **Move as one unit** — see §3. |
| **G** | 237–447 | 211 | Refusal / error taxonomy: `CrossoverV2Refused`, `CrossoverV2LocalSeamError`, `refusal_next_action`, `classify_program_failure` (§5.10 reason codes), `refused_from_flow_error`, `profile_refusal_code`. | setup `:1010`, `:7394`, `:7536`; **relay `:574`, `:587`, `:978`; wired `:580`, `:609`, `:871`; republish `:60`** | **SUPERSEDED** → `crossover_v2/refusal_copy.py` (1,609 lines; 12 public functions/classes and 51 public constants, already the home of the `REASON_*` codes this row imports at `:118-122`). A clean leaf; no seam involved. |
| **H** | 448–577 | 130 | Durable JSON state file I/O: path resolution, `load_v2_state`, `save_v2_state` (fsync decision), `_update_current_review`, `clear_v2_state`, `set_state_path_for_tests`. | xflow `:272`; **republish `:206`, `:269`** (and `_state_lock` at `:205`); **`v2-status`** (`load_v2_state`, reached late-bound off this module — that module's own docstring says why); tests | **→W1-a** (`RecordStore`). The *document* already lives in `crossover_v2/durable_state.py`; what is here is the **file** — path, envelope, atomic write, durability verdict (`:2550-2555` says exactly this). |
| **I** | 578–1012 | 435 | Journey-state observers: `reset_v2_journey_state`, `observe_apply_success` (137 L), `observe_restore` (131 L), `observe_review_decline`, `review_declined`, model-error snapshot/record. 246 lines prose. | xflow `:219`, `:272`; setup; **`v2-status`** (`review_declined`) | **→W5** + `crossover_v2/journey.py` (597 L). The transitions are journey facts; the persistence half rides W1-a. |
| **J** | 1013–1071 | 59 | Three conductor seams reading the durable state: `_applied_gate`, `_applied_offset_gate`, `_apply_failure_gate`. | in-file (row AK binds them) | **→W1-a** — they are reads of the record store wearing a seam. |
| **K** | 1072–1098 | 27 | `session_volume_plan()` — the one durable-state-backed `SessionVolumePlan` this process owns, double-checked under `_volume_plan_lock`; plus its test setter. | **`v2-status`**, in-file, tests | **→W5-c** |
| **L** | 1099–1181 | 83 | Session-scoped measurement pause: `acquire`/`release` (idempotent), `session_measurement_pause_held`, test reset. Holds ONE exclusive `measurement_window` for the whole session so jasper-voice's idle reconciler cannot revert the −20 dB session volume (`:1105-1113` records the hardware finding: reverted within ~200 ms). | in-file (rows AB, AH, O) | **→W5-b** — becomes `TuningSession.open()`/`close()`. |
| **M** | — | — | `_play_under_session_pause` — the abort-target dance that keeps the coordinator's isolation-loss cancel effective under a held window. The registration pair beside it (`register_session_measurement_graph` / `release_session_measurement_graph`) is **gone** as of W5-c2. | in-file (rows AB, AH, O) | **→W5-b** done — the graph is `EngineSeams.graph` on the session (`crossover_v2/session_graph.py`). |
| **N** | 1279–1307 | 29 | `_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0` and `_session_volume_io` — the `(set, get)` fader-door factory, fail-closed on `CamillaUnavailable`. **5 call sites: `:1367`, `:1409`, `:1446`, `:4187` (discards `_set`), `:5397`.** | in-file | **→W5-c** — `_set` dies with its four consumers; `_get` survives for `:4187`'s read-only hold. |
| **O** | — | — | `_release_pause_best_effort` — for the three drains that run outside the runner. Holds no graph since W5-c2; releases isolation only when no `SESSION_MEASUREMENT` claim is held, gated on the **owner's knowledge, never a restore outcome**. | in-file (rows P) | **→W5-b** done. The claim gate is a preserved invariant. See §3. |
| **P** | 1347–1461 | 115 | Out-of-runner volume drains: `enforce_session_volume_ceiling_if_stale` (lazy 1800 s ceiling — W6.1 found it had zero callers so it never existed at runtime), `v2_volume_recovery_active`, `recover_session_volume`, `reconcile_session_volume_for_new_session`. | setup `:685`, `:7648`, `:7649`; in-file `:6039` | **→W5-c** |
| **Q** | — | — | Status projection, part 1: `_phase_from_state`, `_provenance_note`, `_compact_cloud_status`. Pure read-side. **Moved** as of #3286. | — | **EXECUTED BY #3286** — lifted whole into `jasper/web/correction_crossover_v2_status.py` with rows R and S. |
| **R** | — | — | Status projection, part 2: `CHART_CURVE_MAX_JSON_POINTS = 256`, `_decimate_curve_for_chart`, `_chart_cloud_status`, `_prediction_status`. **Moved** as of #3286. | — | **EXECUTED BY #3286**, same module as Q. |
| **S** | — | — | `crossover_v2_status_block()` — the file's one read entry point — and `_household_findings_status`. Still embedded as `payload["crossover_v2"]` by the backend; that is what feeds the human's `/correction/crossover` page. **Moved** as of #3286. | — | **EXECUTED BY #3286.** Both production importers (backend, doctor ×2) were repointed at the new module; no re-export barrel was left behind. |
| **T** | 2166–2545 | 380 | Post-apply grading: the grade/scope/spatial vocabularies (`GRADE_*`, 13 constants), `_spatial_grade`, `_post_apply_grade` (296 L) — *"was the correction now ON the speaker ever checked after it landed?"* | **`v2-status`** (`_post_apply_grade` — row S's own module since #3286, so this row's move now repoints a cross-module caller); **`jasper/cli/doctor/correction.py` imports all nine `GRADE_*`** | **→§2** (the analyze registry). It is an analysis, and it is the largest single analysis in the file. |
| **U** | 2546–2582 | 37 | **The fifth barrel.** 16 names re-bound off `durable_state` under historical names (`:2559-2578`), because *"`prepare_v2_session`'s verify-only stage reaches them as module globals and the stage-bridge suite names them off this module"* (`:2557-2558`). **Five have zero in-file uses**, and **two of those five have no consumer anywhere** — see below. | in-file (11 of 16); **republish `:182` reaches `_candidate_summary`**; tests | **SUPERSEDED** → `crossover_v2/durable_state.py` (1,869 L). Two lines deletable **today**. **This row's stated unblock condition did NOT come true.** It read *"the rest the moment W5-a converges the preparers and the tests are repointed"*; W5-a landed (#3166) and the tests are repointed, and the converged preparer **still reaches 8 of the 16** as module globals. The reach was never the duplication — each stage used them itself, so folding the two changed nothing about it. The real condition is a preparer that imports from `durable_state` directly. Not in §6's table. |
| **V** | 2583–2806 | 224 | Staged-prescription / angle-walk intake: `_take_staged_prescription`, `_take_staged_angle_walk`, `_fc_hz_label`. | in-file (rows AM, AN) | **→W3-b** (recommender binding). |
| **W** | 2807–2929 | 123 | Conductor persistence, write side: `persist_conductor_state`, `_persist_terminal_failure`. | in-file (rows AM, AN); **relay `:589`, `:875`, `:954`, `:994`, `:1005`; wired `:611`, `:862`, `:883`, `:888`** | **→W1-a**. This is what `TuningSession.save()` replaces. |
| **X** | 2930–3035 | 106 | Calibration resolution: `_wav_bytes_to_samples`, `resolve_relay_calibration`, `default_setup_calibration_for_v2`, `_setup_calibration_observation`. | in-file (row Y); named in `setup` prose `:2352`, `:3762`, `:3921` | **→W2-b** |
| **Y** | 3036–3177 | 142 | `bind_production_analyze` — the real `analyze` seam, `CaptureResult` → `analyze_program_capture`, with the 101-line `_analyze` closure. | in-file (row AK) | **→W2-b** — this is the registry's production entry. |
| **Z** | — | — | Capture-dump retention, implementation: `_prune_capture_dump` (oldest-first, bounded by count **and** bytes), `_maybe_retain_capture`. **Gone** as of #3250, same ruling as row D. | — | **EXECUTED BY #3250** |
| **AA** | 3363–3963 | 601 | Evidence store + publishers — the file's largest concern: `open_v2_evidence_store`, `bind_evidence_publishers`, `bind_round_receipt`, `bind_position_retention`, `v2_session_identity`, `_publish_findings`, `_bank_household_findings`, `bind_findings_publisher`, `bind_cloud_publisher`. | in-file (row AK) | **→W1-a** (`RecordStore`). Cross-file note, corrected: retention is **three** sites, not the plan §0's four. `crossover_v2_flow.py:6879` is a *comment* inside a `publish_cloud` except arm, not an inlined retention copy; `self._seams.retain_position` occurs exactly twice in that file, both inside `_hand_to_retention`. |
| **AB** | 3964–4340 | 377 | `bind_production_play` — the real `play` seam. 9 nested functions, 6 of them `async`. Contains `_hold_fader` (`:4173`), the 5th `_session_volume_io` site, which **discards `_set`** and re-proves the measurement volume per stimulus (`:4187`); and `run_async(_emit())` at `:4336`, a sync→async bridge fired from inside a play. | in-file (row AK) | **→W5-b** — `PlaybackTransaction` binding. |
| **AC** | 4341–4435 | 95 | What the host hands the capture provider: `V2VolumeHooks` (frozen dataclass of three async callables), `drive_group_close`, `_start_speculative_group_close`. | relay `:411`→`:450`, `:616`; wired `:418`→`:619`, `:790` — **runtime**. Only `V2VolumeHooks` itself is `TYPE_CHECKING` | **→W5-b**. §6 flags this as *"one concern split 1,000 lines apart"* from row AH — correct, and the split is the interface half here, the implementation half there. |
| **AD** | 4436–4850 | 415 | Conductor context resolution: `V2ConductorContext`, `ensure_crossover_preview_ready`, per-role driver class / radiating diameter resolvers, `resolve_conductor_context` (210 L) — preset/bands/caps/targets/volume from live status + topology. | in-file (rows AM, AN) | **→W5-b** — becomes `EngineSeams` construction input. |
| **AE** | 4851–4991 | 141 | `attach_stage2_preflight` — the stage-2 openability predicate for the REVIEW screen (D3). 92 lines prose. | **xflow `:160`** | **→W5-b** |
| **AF** | 4992–5352 | 361 | `PositionGate` (280 L, 6 methods) plus its banner and 8 constants, incl. `POSITION_READY_ENDPOINT` (`:5069`). Holds a gated session's begin until the angle reached is reported. | the class: relay `:49`, wired `:97` — **`TYPE_CHECKING` only**. But `POSITION_GATE_TERMINAL_CODES` is read at runtime by relay `:889`. tests | **OWN MODULE** — §6 calls it *"cleanest extractable class"* and the **class** is: both importers take it as a type annotation. The **row** is not quite as clean, because `POSITION_GATE_TERMINAL_CODES` is a runtime dependency of `relay:889`; move it with the class and repoint that one line. Converge `:5069` with `arm_walk.py:427` while there (§3). |
| **AG** | 5353–5388 | 36 | `V2PreparedSession` — what the dispatch needs to host one session. | **setup `:6356`** (`.capture_source`), `:6342` (the return) | **→W5-a** |
| **AH** | 5389–5459 | 71 | `_volume_hooks` — the in-runner drains. `_open` acquires the pause **before** the volume and releases it in a `finally` if the open did not take; `_put_the_graph_back` restores the graph **before** the pause release in both `_close` and `_abandon`. **The two safety ordering sites** — the only places the graph goes back on a teardown. | in-file (rows AM, AN) | **→W5-c**, ordering preserved. See §3. |
| **AI** | 5460–5475 | 16 | Stage-capabilities banner — 15 of 16 lines are the essay recording that the declarations moved to `journey` in #2291 Phase 4. | — | **SUPERSEDED** — pure pointer at a completed move. Delete with row B. |
| **AJ** | 5476–5673 | 198 | Applied-graph introspection: `_active_graph_fingerprint`, `_previous_candidate_known` (was `_rollback_anchor_available`), `_applied_graph_boosts`, `_applied_profile_now`. | in-file (rows AP, AQ) | **KEEP** — apply-adjacent; lives in the surviving host beside the apply transaction. |
| **AK** | 5674–5784 | 111 | `bind_v2_stage_seams` — builds one stage's `V2FlowSeams` and journals what it opened with. **The convergence point**: rows J, Y, AA, AB, AQ all arrive here. | in-file (rows AM, AN) | **→W5-b** — `EngineSeams` replaces `V2FlowSeams`. This is the file's single most consequential line-range for the cutover. |
| **AL** | 5785–5942 | 158 | Capture-source resolution and run building: `_resolve_prepare_capture_source`, `_hand_released_plan_shape`, `_mint_source_session`, `_build_source_run`. | in-file (rows AM, AN) | **→W5-a** — shared tail of both preparers; converges with them. |
| **AM** | 5943–6518 | 576 | `prepare_v2_session` (534 L) + the three `VERIFY_STAGE_*` constants + `_verify_plan_shape`. Stage-1 preparer: gate, build the conductor, hand the walk to the capture source. Holds the conductor in a bare `holder: dict[str, Any]` at `:6258`, filled `:6432`, drained by `_run` at `:6448`. | **setup `:6341`** | **→W5-a**, then W5-b. |
| **AN** | 6519–6903 | 385 | The verify-only preparer (382 L) — the near-duplicate twin. Same `_open`/`_run`/`holder` shape (`:6711`, `:6856`, `:6872`), same `bind_v2_stage_seams` call shape, same `_build_source_run` tail. | **setup `:6341`**; `VERIFY_STAGE_*` also by **`scripts/run-crossover-round.py:188`, the file's one top-level production import** | **W5-a LANDED (#3166).** This range no longer exists: the twin folded into row AM's preparer under a `verify_only` flag, and `bind_v2_stage_seams` is now ONE call for both stages. Rows AL+AM+AN were **1,119 lines** and the fold returned **−94 of code**, not the whole 385 — the two bodies shared a ~95-line scaffold and little else; the rest was per-stage prose and per-stage ctor kwargs, which do not fold. Read that number before sizing any sibling row's de-duplication win. Row AM is now the whole preparer, and carries on to W5-b. |
| **AO** | 6904–6960 | 57 | Apply banner + `_assert_stage_2_can_open` — refuse an apply this speaker could not then verify (D3). | in-file (row AP) | **KEEP** |
| **AP** | 6961–7361 | 401 | `handle_v2_apply` — the apply transaction. | **setup `:6390`** | **KEEP.** This is §6's *"NO ENGINE HOME"* ruling resolved in this file's favour: the apply transaction is *"not a target. Ever."*, and its two options were a publish/commit organ **or a thin surviving host module**. This file is that host. |
| **AQ** | 7362–7462 | 101 | `bind_delta_probe_rollback` — the conductor's `rollback` seam. Since 2026-08-31 it presses the NORMAL doors: `handle_v2_republish`, then `handle_v2_apply`. | in-file (row AK) | **KEEP** — it is the apply transaction's inverse, through the apply transaction. |
| **AR** | 7463–7620 | 158 | Was the Sound-declaration Undo. | — | **DELETED (2026-08-31)** with the restore verb; only `_crossover_label` survives (republish + apply read it). |
| **AS** | 7621–7831 | 211 | Was the rollback-anchor refusal vocabulary. | — | **DELETED (2026-08-31)** — `rollback_available` became "a prior candidate fingerprint is recorded", and the normal apply path carries its own refusals. |
| **AT** | 7832–7972 | 141 | Was `handle_v2_restore` — the v2-aware Undo. | — | **DELETED (2026-08-31)** — the way back is republish-then-apply (owner ruling: configs get applied; an earlier config gets applied the same way). |
| **AU** | 7973–8088 | 116 | Apply-blocked tail: `_blocking_apply_issue`, `_dsp_apply_is_known_inactive`, `_persist_apply_blocked`, `_reopen_candidate_artifact` (`_restore_refusal_code` deleted with row AT). | in-file (row AP) | **KEEP** |

### The whole production caller surface, in one place

**38 names across 8 modules.** Enumerated by matching import *statements* — not
by grepping an alias prefix, which misses the four modules that bind the host
to a local name (`_host`) or take it as a parameter:

```
grep -rnE "^\s*(from [a-z_.]*correction_crossover_v2 import|from jasper\.web import correction_crossover_v2($| )|from \. import correction_crossover_v2($| as )|import jasper\.web\.correction_crossover_v2($| ))" \
  jasper/ scripts/ deploy/ relay/ experiments/ --include="*.py" \
  | grep -v '^jasper/web/correction_crossover_v2.py:'
```

*(The `($| )` / `($| as )` tails are load-bearing: without them the pattern also
matches the `correction_crossover_v2_relay`, `_wired` and `_republish` siblings
and over-counts. It returns **20** import sites.)*

| Caller | Import site | Name(s) reached | Use sites |
|---|---|---|---|
| `correction_setup.py` | 1010 | `CrossoverV2LocalSeamError`, `classify_program_failure` | 1029, 1031 |
| | 6419 (`v2host`) | `prepare_v2_session`, `SOURCE_WIRED`, `handle_v2_apply` (`handle_v2_restore` deleted 2026-08-31) | 6341, 6356, 6390 |
| | 7394 | `refusal_next_action` | 7399 |
| | 7536 | `CrossoverV2Refused` | 7539 |
| | 7646 | `v2_volume_recovery_active`, `recover_session_volume` | 7648–7649 |
| | 7858, 7876 | `enforce_session_volume_ceiling_if_stale` | **685, indirectly** — both routes pass the module object into `_enforce_session_volume_ceiling(v2host)` (`:665`), which makes the call |
| `correction_crossover_v2_relay.py` | 49 (`TYPE_CHECKING`) | `PositionGate`, `V2VolumeHooks` | 310, 307 — annotations only |
| | **411 (`_host`, runtime)** | `drive_group_close`, `CrossoverV2LocalSeamError`, `persist_conductor_state`, `_start_speculative_group_close`, `_persist_terminal_failure`, `POSITION_GATE_TERMINAL_CODES`, `classify_program_failure` | 450, 574, 587, 589, 616, 875, 889, 954, 978, 994, 1005 |
| `correction_crossover_v2_wired.py` | 97 (`TYPE_CHECKING`) | `PositionGate`, `V2VolumeHooks` | 321, 314 — annotations only |
| | **418 (`_host`, runtime)** | same set minus `POSITION_GATE_TERMINAL_CODES` | 580, 609, 611, 619, 790, 862, 871, 883, 888 |
| `correction_crossover_v2_republish.py` | **58, 131 (`_host`)** | `CrossoverV2Refused`, **`_crossover_label`**, **`_candidate_summary`**, **`_state_lock`**, `load_v2_state`, `save_v2_state` | 60, 169, 171, 182, 205, 206, 269 |
| `correction_crossover_backend.py` | 2097 | `crossover_v2_status_block` | 2099 |
| `correction_crossover_flow.py` | 160 / 219 / 272 | `attach_stage2_preflight`, `reset_v2_journey_state`, `load_v2_state`, `observe_review_decline` | 169, 221, 277, 301 |
| `jasper/cli/doctor/correction.py` | 593, 703 | `crossover_v2_status_block` + **all nine `GRADE_*` constants** | 596, 717, 758–819 |
| `scripts/run-crossover-round.py` | **188 — top-level** | `VERIFY_STAGE_KEY`, `VERIFY_STAGE_POST_APPLY` | 877 |

**Four consequences for sequencing.**

1. **`correction_crossover_v2_republish.py` reaches four *private* names** —
   `_crossover_label`, `_candidate_summary`, `_state_lock`, and the pair of
   state readers. Rows AR, U, F and H therefore cannot move without moving
   republish's reach with them. This is the tightest coupling in the file and
   the easiest to miss, because nothing about a leading underscore suggests a
   cross-module consumer.
2. **The two providers *do* have a runtime edge**, at `relay:411` and
   `wired:418` — 20 call sites between them, into rows G, W and AC. Only
   `PositionGate` and `V2VolumeHooks` themselves are annotation-only. So row AF
   (the class) is still cheap to extract, but `POSITION_GATE_TERMINAL_CODES`
   (`:5059`) travels with it and **is** read at runtime by `relay:889`.
3. **The grade vocabulary has a consumer outside the web tier entirely** —
   `jasper-doctor` imports all nine `GRADE_*` constants. Row T's move to §2 is
   not a web-internal refactor; it repoints a CLI diagnostic.
4. **`scripts/run-crossover-round.py:188` is the only unconditional
   module-level production import of this module anywhere.** Of the 20 import
   sites, 17 are function-local and 2 more (`relay:49`, `wired:97`) sit at
   module level but under `if TYPE_CHECKING:`. That one unguarded import is
   what makes row B's 0.29 s numpy cost unavoidable for that script — and it
   is the only place a module-scope import cycle could ever bite.

---

## 2. Deletion order

§6's global order interleaves both files. This is the web file's own thread
through it, ordered so that each step shrinks the next one's diff.

1. **Row S+R+Q — the status projection lifts whole.** Executed by #3286 —
   `jasper/web/correction_crossover_v2_status.py`; the host lost 692 lines net.
   The map's "one importer" was two: `backend:2097` **and** the doctor's own
   pair. Both repointed, along with 118 test reaches; nothing was re-exported
   from the host. The host reaches back once, from inside
   `persist_conductor_state`.
2. **Rows U, B, AI — the barrels and the pointer essays (536 lines).**
   §6's step 1 applied to this file's own fifth barrel. Row U's 16 names go the
   moment W5-a converges the preparers and the test suite is repointed at
   `durable_state`; five of them already have zero in-file consumers today. Row
   B's 6 `X as X` re-exports go when their importers point at the organs — and
   that also pays back the **0.29 s import cost** the comment at `:63-71`
   records.
3. **Row G — the refusal taxonomy (211 lines) → `refusal_copy.py`.**
   Independent of everything; a clean leaf with an existing home.
4. **Rows AL+AM+AN — the two preparers converge (W5-a, 1,119 lines).**
   §6's step 3, and the precondition for a single construction site.
5. **Rows L, M, O, AB, AC, AK — the walk lifts (W5-b).**
   `consume_capture` and the retention sites go together with the twin's. Row
   AK is the join: when `EngineSeams` replaces `V2FlowSeams`, rows J, Y, AA and
   AB lose their binder in one edit.
6. **Rows F, K, N, P, AH — volume (W5-c).**
   §6's step 5: *after* the walk, because the claim's lifetime is the walk's.
   **The three ordering sites in §3 must survive this step intact.**
7. **Rows D, E, Z — capture-dump retention (W1-c).** Executed by #3250 — the
   owner's ruling was to delete the ring outright, not schedule it.
8. **Row AF — `PositionGate` to its own module.** Cheap at any point: two
   `TYPE_CHECKING` lines for the class plus one runtime line for
   `POSITION_GATE_TERMINAL_CODES` (`relay:889`). Do it when convenient, not on
   the critical path.
9. **Row A — the docstring is rewritten last**, describing what actually
   remains rather than what used to.

What is left standing: rows AJ, AO, AP, AQ, AR, AS, AT, AU, and a shrunk row A.

---

## 3. Load-bearing oddities

Things a mover will break by accident because they look incidental.

### 3.1 The restore-before-pause-release ordering — treat as safety

**The property.** The measurement graph must be restored **before** the
measurement pause is released. Once the pause goes, the household programme can
resume; a graph swapped after that lands under live audio, which is the exact
condition an un-ducked swap is only safe in the absence of (the restore stopped
ducking in wave 6d). Reverse the two statements anywhere and the failure is
silent, intermittent, and only reproduces when music is playing.

**Two sites encode it now, and both must keep it** (W5-c2 removed the third).

| Site | Shape | Serves |
|---|---|---|
| `_volume_hooks._close` | `_put_the_graph_back()` then `plan.close()`, pause released in `finally` | the runner's normal end |
| `_volume_hooks._abandon` | same shape | the runner's failure end |

The graph goes back through the **session** and nowhere else — `_close`/
`_abandon` call `TuningSession.close`, and a failed open restores through the
session's own teardown. No out-of-runner drain touches the graph.

**`_release_pause_best_effort` keeps a different rule.** The three drains that
reach it — `enforce_session_volume_ceiling_if_stale`, `recover_session_volume`,
`reconcile_session_volume_for_new_session` — hold no graph, so they gate the
pause release on the **owner's** knowledge instead: isolation is freed only
when no `SESSION_MEASUREMENT` claim is held. It is deliberately NOT gated on
the restore outcome, because an outcome cannot tell a finished session from a
deferral, from a drain that raised (no outcome at all), or from a coincidental
`LANDED` when the household level already equals the measurement level.

**Two subordinate rules ride with it**, and both are written into the code as
prose that will not survive a careless rewrite:

- *Never at the pause's expense.* A graph restore that fails logs **CRITICAL**
  and the pause is released anyway. Stranding the speaker in the measurement
  graph **and** at measurement volume is the worse of the two failures;
  stranding voice paused with music gated is also unacceptable. The catch arm
  in `_put_the_graph_back` exists for that reason.
- *Idempotent both ways.* Every acquire/release is a safe no-op when nothing is
  held — a session that never played a routed stimulus, a crash-fresh process,
  a second drain.

**And the mirror on the way in:** `_volume_hooks._open` (`:5399-5412`) acquires
the pause **before** setting the volume, and releases it in a `finally` when the
open returns anything but `"opened"` — because W6.1's hardware run 2 measured
the idle reconciler reverting the −20 dB session volume within **~200 ms** of
`session_volume_opened` when nothing held voice paused (`:1105-1113`). A failed
open that strands voice paused is the symmetric bug.

**Verification bar for any PR that touches these sites:** mutate — swap the
two statements at an ordering site, or remove the claim gate in
`_release_pause_best_effort` — and watch a named pin fail; restore; re-run
green. A pin that stays green under its own mutation is not covering this.

> `docs/REFACTOR-CUTOVER-2026-08.md` §5 records a *second*, separate ordering
> invariant in the same neighbourhood — `SessionVolumePlan._clear_resolved`
> (`session_volume_plan.py:858`) dropping in-memory intent **before**
> persisting, with a measured **+47.5 dB** hazard in the other order. That one
> lives in the plan module, not this file. Do not confuse them; both are real.

### 3.2 Four process-global mutables, and they do not travel alone

| Name | Line | Mutated at | Guarded by |
|---|---|---|---|
| `_state_path_override` | 232 | `:460` | `_state_lock` (231) |
| `_volume_plan` | 235 | `:1080`, `:1095` | `_volume_plan_lock` (234) |
| `_session_pause_cm` | 1126 | `:1138`, `:1159`, `:1176` | *(none — loop-thread confinement)* |
| `_session_abort_target` | 1127 | `:1138`, `:1159`, `:1176` | *(none)* |

§6 named four and `_state_path_override` was the fifth it missed. `_session_graph`
was one of those four, and W5-c2 deleted it — the measurement graph is now a
field on the `TuningSession` that dies with the run, reachable through no
module global. Four remain, the table above.
The two unguarded ones are safe only because they are entered and exited on
jasper-web's **single** background loop thread (`jasper-correction-loop`) —
that confinement *is* the lock. Anything that moves them to a
differently-threaded owner must supply a real one.

`reset_session_measurement_pause_for_tests` clears **two** of them in one
`global` statement.

### 3.3 `_run_async` — eleven bridging points, and the reap trap

`run_async` is never imported; it is threaded in as a parameter from
`correction_setup._run_async` (`:1292`). **Eleven functions take it:** `:1309`,
`:1349`, `:1395`, `:1429`, `:3967`, `:5688`, `:5948`, `:6524`, `:6964`, `:7363`,
`:7834`. One further site calls it from *inside* a coroutine closure —
`run_async(_emit())` at `:4336`, within `bind_production_play`.

Every one of these is a sync-thread → loop-thread hop, and each is where a
`TuningSession` verb becomes awaitable under W4-a. Carry the plan's trap
verbatim: `_run_async`'s timeout path (`correction_setup.py:1310-1328`, measured)
cancels the loop task (`:1315`), waits `_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S` for
the drain (`:1316`), logs **CRITICAL** if it does not arrive (`:1317-1322`) —
**and then calls a bare, unbounded `drained.wait()` at `:1327` anyway**, because
a terminal response must never release measurement ownership while a
graph/volume finalizer can still mutate the speaker. The alarm is
observability, not permission to abandon cleanup.

The four drains additionally pass `_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0`
(`:1286`) — sized for a few CamillaDSP RPCs; longer means the DSP is wedged and
the drain should surface a failure rather than hang the request thread.

### 3.4 One concern, split a thousand lines apart

`V2VolumeHooks` (row AC, `:4352-4358`) declares three async callables.
`_volume_hooks` (row AH, `:5390-5457`) is the only thing that builds one. They
sit **1,038 lines apart** with four unrelated concerns between them (rows AD,
AE, AF, AG — context resolution, the preflight, the gate, the prepared
session). §6 flags
this; the practical instruction is that a mover who greps for `V2VolumeHooks`
finds the dataclass and can easily believe that is the whole concern. It is
the interface half. The safety-critical half is row AH.

### 3.5 The barrels are load-bearing for the *tests*, not for production

Row U's own comment (`:2557-2558`) says the barrel exists because the
preparer's verify-only stage reaches those names as module globals **and the
stage-bridge suite names them off this module**. Five of the sixteen have no
in-file consumer at all — but "no in-file consumer" is not "no consumer", and
the difference matters per line:

| Barrel line | Name | Who reaches it *through this module* |
|---|---|---|
| `:2563` | `_candidate_summary` | **`correction_crossover_v2_republish.py:182` — production.** Plus `tests/test_crossover_v2_conductor.py:10094`, `:10115`, `:10132`. |
| `:2566` | `verify_measured_curve_from_state` | `tests/test_crossover_v2_stage_bridge.py` — 5 sites, `:1193`–`:1364` |
| `:2561` | `_decimate_delta` | `tests/test_crossover_v2_stage_bridge.py:1759`, `:1779` |
| `:2562` | `_decimate_verify_measured` | **nobody.** `tests/test_active_speaker_crossover_v2_round_views.py:284-286` imports it from `durable_state` directly. |
| `:2565` | `_delta_probe_summary` | **nobody.** Its only other mention in the tree is a comment at `tests/test_crossover_envelope_v2.py:1079`. |

**`:2562` and `:2565` are two orphan lines deletable today**, no repointing,
no work item — a free two-line down-payment on step 2. **`:2563` is the
opposite** and is the trap in this row: a private, underscored name that reads
as internal but carries a live production consumer in another module. Deleting
the barrel without repointing `republish:182` breaks the republish path, and no
test will tell you, because the tests reach the same name through the same
door.

Row B's `X as X` block carries
the same note at `:146-148`: *"this HOST calls `build_v2_run_and_consume`,
`relay_link_ttl_s`, and `PlaybackStartSignal` through these bindings, so
patching them on this module reaches the preparers."*

So the barrels are a **patch surface**. Deleting one without repointing the
monkeypatch targets produces tests that pass while patching nothing — the
silent-both-ways failure mode. Repoint the patch targets in the same PR, and
prove it by mutating the patched function and watching the test fail.

### 3.6 The position-ready URL exists four times

| Site | Form |
|---|---|
| `correction_crossover_v2.py:5069` | `"/correction/crossover/v2/position-ready"` |
| `jasper/active_speaker/arm_walk.py:427` | `"/correction/crossover/v2/position-ready"` — **byte-identical** |
| `correction_setup.py:589` | `"/crossover/v2/position-ready"` (dispatch-relative) |
| `correction_setup.py:7433` | `"/crossover/v2/position-ready"` (dispatch-relative) |

§6 says converge the first two. They are the same string with two names
(`POSITION_READY_ENDPOINT` / `POSITION_READY_PATH`); the second pair is a
different, relative vocabulary and is *not* the same constant — do not
over-converge and break the dispatch match.

### 3.7 A resolved ruling and a formatting tell

Rows D, E and Z (226 lines) are no longer scheduled: #3250 ruled on
`XOVER_CAPTURE_DUMP_ENABLED_MARKER` by deleting the ring outright rather than
carrying it into W1-c. Operator capture retention does not survive the
cutover; the banked record store is the only retention path.
**These were the only rows in the file whose disposition waited on a person.**

Minor: `:2579-2582` is four consecutive blank lines, immediately below row U's
barrel — harmless, but two more than this tree's spacing, and a tell that
something was already lifted out of that seam. Treat it as a hint that the
neighbourhood has been cut before.

---

## 4. Size accounting

Every row from §1, bucketed. Because §1 is contiguous and exhaustive, these sum
to 8,088 exactly — no residual, no double count.

| Disposition bucket | Rows | Lines | Share |
|---|--:|--:|--:|
| **KEEP** (the surviving apply host) | 9 | **1,426** | 17.6% |
| →W5-b (`TuningSession` in production) | 8 | 1,358 | 16.8% |
| →W5-a (preparers converge) | 4 | 1,155 | 14.3% |
| →W1-a (`RecordStore`) | 4 | 913 | 11.3% |
| **LIFTS WHOLE** (status projection) | 3 | 704 | 8.7% |
| →W5 + `journey.py` (observers) | 1 | 435 | 5.4% |
| **SUPERSEDED** (barrels + taxonomy + essays) | 4 | 382 | 4.7% |
| →§2 (post-apply grading) | 1 | 380 | 4.7% |
| **OWN MODULE** (`PositionGate`) | 1 | 361 | 4.5% |
| →W2-b (analyze binding + calibration) | 2 | 248 | 3.1% |
| →W5-c (volume) | 4 | 242 | 3.0% |
| →W1-c (capture-dump retention) | 3 | 226 | 2.8% |
| →W3-b (prescription intake) | 1 | 224 | 2.8% |
| →W1-a / W5-a (split row C) | 1 | 27 | 0.3% |
| →W5 (the global block) | 1 | 7 | 0.1% |
| **TOTAL** | **47** | **8,088** | 100% |

**Nothing in this file is NO HOME.** Every row has a destination. The one §6
ruling that *would* have produced one — *"candidate build · publish · commit …
NO ENGINE HOME"* — offered two resolutions, a publish/commit organ **or a thin
surviving host module**, and rows AO–AU are that host. The apply transaction is
*"not a target. Ever."* Rows D, E and Z were never homeless either — #3250
resolved the owner's ruling by deleting them outright, not by giving them a
home.

### Against the floor

| | Lines |
|---|--:|
| Row-4/6 baseline | 9,563 |
| HEAD (`c253c3cf1`) | **8,088** |
| Banked already | **−1,475** |
| Kept, measured (rows A, AJ, AO–AU) | 1,426 |
| Kept, §7's own three ranges at HEAD | 1,380 |
| **Remaining contribution from this file** | **≈−6,660 to −6,710** |

**That is 560 to 1,010 lines better than §7 booked**, depending which end of
each bracket you take. §7 states *"≈2,000–2,400 lines kept"*, giving a remaining
contribution of ≈−5,700 to −6,100. That bracket comes from
2,108 = 1,185 + 726 + 197 — and the 1,185 is `:6904-8088`, which **already
contains** the `:7363-8088` tail that the same sentence then adds a second time.
The label `:6904-7360` beside it is 457 lines. Summing §7's three stated ranges
honestly gives **1,380**.

Do not re-quote §7's ≈2,000–2,400 for this file. Use 1,380–1,426, and re-take
the count per wave with `scripts/right-size-report.sh` against the actual tree
rather than by hand — the same instruction §7 attaches to its own rows.

Two smaller adds are worth booking against the deletions, because they land in
*new* files rather than removing lines: the status projection and
`PositionGate` (~361 lines) **move**, they do not vanish. Net deletion from the
tree is therefore ≈1,065 lines smaller than the file's own shrinkage — the file
loses ≈6,660, the tree loses ≈5,600. §7's floor counts the tree.

The status half of that is now measured rather than estimated. #3286 removed
**703** lines from the host (the 698-line projection, its 3-line section
banner, 2 blank separators) and added back 11 (a 6-line tombstone, a 5-line
lazy import in `persist_conductor_state`), so the host lost **692** net; the
new `correction_crossover_v2_status.py` is **730** lines. The row-Q/R/S
estimate of 704 was 1% high, and the tree **grew by 38 lines** where this
section booked the move as a wash — new-file header, tombstone and re-import
are what a lift costs.

---

*Measured at `c253c3cf1` on 2026-08-26. Re-derive before cutting. Drift is not
hypothetical: the plan's §0 records two of its handed numbers moving within a
single merge, and re-deriving for this map moved one more — the dispatch funnel
from `:8014` to `:8015`. This map will drift the same way.*
