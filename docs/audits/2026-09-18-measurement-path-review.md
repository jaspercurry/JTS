# 2026-09-18 speaker-measurement path review — scoped

| field | value |
|---|---|
| audited SHA | `63914527e` |
| date | 2026-09-18 |
| scope | the tuning umbrella: programs `speaker` → `rear` → `bass` → `room`, run by `jasper-round`, measured with a mic and a turntable arm, read by an LLM. Code: `jasper/active_speaker/` (217 files, ~104k lines), `jasper/audio_measurement/` (57 files, ~36k lines), `jasper/cli/round*.py`, `jasper/cli/round_views/`, `jasper/cli/crossover_prescriber.py`, `jasper/web/correction_*.py` |
| method | 3 read-only agents. **A** — refusal inventory: every gate on the path (174 codes, 12 live stages + 1 orphan lane). **B** — the program × layout matrix (252 cells) and the level path; the matrix was **measured** by driving the production plan builder, composer and admission gate, not by reading them. **C** — separation-of-concerns / single-source-of-truth map. No repo writes, no hardware, no sound, no test lane. |
| tracking issue | #5384 |
| evidence | the three per-agent reports and the adversarial review of the proposed ADR-0328 are comments on #5384 — never committed ([ADR-0284](../adr/0284-audits-are-frozen-reports-and-issues-are-the-ledger.md)) |

**This is a frozen snapshot** of `63914527e`, never edited after landing work. Disposition
lives on #5384 and in issues labelled `audit`. Ids are the per-agent ids (A: `S1`–`S7`,
`A-D1`–`A-D10`; B: `F1`–`F6`, `X1`–`X8`; C: `C-PR1`–`C-PR12` and §4 row numbers). See
[README.md](README.md) for the archive rules and
[docs/measurement-loop-doctrine.md](../measurement-loop-doctrine.md) for the program's
own doctrine.

---

## Bottom line

1. **Nothing owns "how loud this take plays."** The run fader reaches the DSP main volume
   and nothing else; the composer never sees the target SPL.
2. **The predicted SPL is a tuned-graph number played on other graphs.** The `drivers`
   graph is ~11 dB louder than the `candidate` graph the anchor was banked on: 74.0 dB SPL
   predicted, 85.67 heard, 85.0 allowed.
3. **jts3's `program_segment_outside_limits` was never a level problem.** The failing leg
   is `high_ok` — a fixed 150 Hz–20 kHz summed sweep judged against each driver's
   *declared* band — refused at all three levels.
4. **That refusal reaches the operator as `internal_error`**: the code is not in the
   registry, and the run ladder never calls `classify_program_failure`.
5. **Layout coverage is not uniform.** 153 of 252 cells pass. A 1-way passive main runs no
   summed take and no bass program; a 3-way runs no driver (`speaker`-purpose) program.
6. **`--levels auto` works and is off for every program that needs it** — only the four
   `bass/*` rows carry it.
7. **174 refusal codes**: 145 keep, 12 do not name themselves, 9 delete, 5 demote, 3 merge.
   Worst offender: `active_excitation_request_outside_limits` folds band, level, duration
   and repeats into one slug.
8. **~2,900 lines are deletable**, dominated by one dead 22-code excitation-admission lane
   that duplicates a live comparison.
9. **The damage sits in five files** — 5059, 4064, 3640, 2715, 2423 lines; two of them
   carry the hearing clamps.
10. **The layout is decided in six places**, and one of them refuses cardioid harmonic
    evidence by inferring roles from a count.

---

## 1. Level — one fader, three graphs, ~11 dB apart

### 1.1 Who decides a level today — eleven touch points, no owner

| # | owner | file:line | what it decides |
|---|---|---|---|
| 1 | CLI flags | `cli/round.py:287-289` | `--levels` (auto \| list), `--spl`, `--level-db`. Mutually exclusive. |
| 2 | program default | `cli/_run_request.py:86` | `program.levels` — **[MEASURED]** `'auto'` only for `bass/*`, else `None`. |
| 3 | seat reference | `angle_capture.py:767-778` `default_run_level` | `LevelPolicy(level_db=seat_level_reference_volume_db())`. |
| 4 | ladder expansion | `run_levels.py:108-127` `preflight_levels` | `auto` → `level_ladder`; `--spl` → `anchor.fader_db_for(x)`; a list → one plan each. |
| 5 | anchor resolve | `preflight.py:186-192` → `resolve_anchor_level` | `predicted = anchor.db_spl_at(fader)` (`seat_level_reference.py:289`). |
| 6 | rung admission | `preflight.py:196-236` | `unmeasured_stimulus_opener` / `measured_rung_admission` / `validate_commissioning_spl`. May lower the fader. |
| 7 | door open | `plan_run.py:346,552` | `level = request.level.volume_db` → `level_window(...)` → CamillaDSP main volume. |
| 8 | composer input | `correction_run_host.py:180` | the run fader becomes the composer's `session_volume_db`, overwriting `conductor_context.py:411`. |
| 9 | composition | `crossover_v2/programs.py:87-101,196-295` | `back_off_gain(gain, sv, cap) = min(gain, cap − sv − 0.01)`. |
| 10 | play-time gate | `program_admission.py:834-861` (summed), `:406-527` (drivers) | per segment: `low_ok and high_ok and peak <= cap and duration <= limit`. |
| 11 | live SPL stop | `wired_stimulus.py:43-49` → `wired_capture.WiredSplCeilingExceeded` | stops the take above the commissioning ceiling. |

None of the eleven knows the target SPL of the take.

### 1.2 The composed peak does not move with the fader

**[MEASURED]** cardioid, composer caps `{woofer: 0.0, tweeter: -25.2}`:

```
   fader  check pk  measure pk  verify pk  verify eff  <= cap?
    -8.0    -29.21      -12.00     -17.21      -25.21     True
   -13.2    -24.01      -12.00     -12.01      -25.21     True
   -16.7    -24.00      -12.00     -12.00      -28.70     True
   -23.0    -24.00      -12.00     -12.00      -35.00     True
   -30.0    -24.00      -12.00     -12.00      -42.00     True
```

`peak(sv) = min(BASE, cap − sv − 0.01) + sv ≤ cap` for every finite `sv`, so the
`peak <= cap` leg (`program_admission.py:854`) cannot be the jts3 refusal.

### 1.3 The anchor has no term for the graph it plays

`ResolvedAnchor.db_spl_at(fader) = anchor_db_spl + (fader − reference_volume_db)`
(`seat_level_reference.py:289`); the anchor's provenance hard-codes
`"graph_scope": "candidate"` (`:126`). **[MEASURED]** the three scopes are different graphs:

| scope | static gains emitted |
|---|---|
| `candidate` | `active_baseline_headroom −5.61`, `as_woofer_baseline_gain 0.0`, `as_tweeter_baseline_gain −3.0`, rear stage |
| `timing` | the same minus the linearization / room / blend filters |
| `drivers` (neutral emit) | `active_startup_headroom −0.0`, commission mutes only — **no role baseline gains at all** |

**[READ]** the product sizes the gap itself — "raw drivers can be louder by headroom + the
fit budget's largest cut (jts3: 3.1 + 8 = 11.1 dB, rounded up)"
(`crossover_v2/programs.py:53-57`), the stated rationale for `CHECK_PROBE_BACKOFF_DB =
12.0`. 74.0 dB SPL at the −16.7 dB seat reference + ~11.7 dB ≈ **85.67 dB SPL**, the
observed stop. Nothing measures that offset. The one guard that notices a different
stimulus, `unmeasured_stimulus_opener` (`preflight.py:213-221`), compares *program ids*
(`stimulus_mismatch`, `seat_level_reference.py:105`) and then bounds the run at the
anchor's own fader — a same-fader clamp, not a same-SPL clamp. On a `speaker` run it is
not even informed: `preflight_live.program_ids()` returns `()` whenever the plan holds any
`drivers`-scope take (`preflight_live.py:86`).

### 1.4 The box, in numbers

| wall | rule | number |
|---|---|---|
| live SPL stop | observed window ≤ commissioning ceiling | 85.0 dB SPL (jts3 saw 85.67) |
| predictive SPL bound | `spl_raise_bound_db_spl(85, margin=max(tolerance, SPL_RAISE_MARGIN_DB=3))` | 82.0 dB SPL |
| SNR floor | `target_capture_dbfs − worst_ambient_dbfs ≥ DRIVER.snr_ok_db` | **25.0 dB** (`quality_model.py:105`; test at `check.py:820`) |
| target capture | `sensitivity.dbfs_from_db_spl(predicted) + SWEEP_PEAK_TO_RMS_DB` | +3.01 dB peak allowance |
| tweeter cap | `min(lf_cap − (sens_hf − sens_lf), MAX_TEST_LEVEL_DBFS=0)` | `0.0 − 25.2 = −25.2 dBFS` |

**[MEASURED]** with declared sensitivities woofer 84.0 / tweeter 109.2 dB the production
resolver returns exactly `-25.2` dBFS, and `delegation=undeclared, anchor_cap_dbfs=0.0`
means the *woofer* declared no level limit, so its class default (full scale) is the
anchor. The cap says "the tweeter may make the same acoustic level as a full-scale
woofer" — it caps nothing in dB SPL.

**[MEASURED]** SNR points (UMIK sensitivity factor −12 dB): 74.0 dB SPL needs ambient
below −53.99 dBFS, 64.7 dB SPL below −63.29 dBFS, so dropping the fader 9.3 dB tightens
the required room quiet by the same 9.3 dB. That is why −26 dB failed `snr_floor` four
times while the room was ~11 dB louder than the number being judged. In one sentence:
**the SPL stop measures the `drivers` graph, the SNR floor predicts the `candidate` graph,
they are ~11 dB apart, and the third wall is not a level at all.**

### 1.5 The real refusing leg (`F6`, the jts3 failure)

**[MEASURED]** through `readmit_summed_program_from_wav`, by changing one declared fact —
crossover order 4 (24 dB/oct) → 2 (12 dB/oct):

```
timing/verify  crossover order 2 (12 dB/oct)  sv=-16.7  allowed=False
   refusals=['program_segment_outside_limits']
   bad=[('sweep_verify','woofer',-28.7,(150.0,20000.0)),
        ('sweep_verify','woofer:rear',-28.7,(150.0,20000.0))]
   … identical at sv=-23.0 (eff -35.0) and sv=-30.0 (eff -42.0)

timing/verify  woofer upper 1200 (below sweep top)  refused at all three levels
timing/verify  order 2 + woofer band widened to 20 kHz   allowed=True
```

The composer builds the summed sweep over a **fixed** band — `f1 = min(VERIFY_F_LO_HZ=150,
fc/2)`, `f2 = VERIFY_F_HI_HZ = 20 000` (`program.py:121-122`, `:1004-1023`) — consulting
**no driver's declared band**. The gate judges that sweep against **each** driver's
`hard_excitation_band_hz`, accepting a breach only via a declared low-pass or one proven
at ≥ `PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE = 24` dB/oct (`program_admission.py:841-852`,
`driver_protection.py:342`). LR2, or a woofer whose declared top sits below the sweep top,
fails on every summed take, on every layout, at every level — and the summed path logs
only `refusals=…`.

### 1.6 Three more level-path facts

* **Composer and gate caps differ in coverage on a cardioid.** Both call
  `resolve_driver_excitation_ceilings(program_admission=True, declared_sensitivities=…)`
  (`conductor_context.py:375`, `session_volume_plan.py:155`), but the composer keys by
  primary *role* (`conductor_context.py:365`) and holds `{woofer, tweeter}` while the gate
  iterates `role_targets` and holds `{woofer, tweeter, woofer:rear}`
  (`program_admission.py:796`). Today the rear shares the front's declaration, so the
  values agree — nothing enforces that.
* **`internal_error` is a registry gap, one line.** The transaction raises incident
  `program_admission_refused` (`program_transaction.py:66,254`); **[MEASURED]** that string
  is **not** in `REASON_REGISTRY`, and `plan_run.py:591` maps an unregistered incident to
  `REASON_INTERNAL_ERROR`. The per-segment detail log exists only on the driver path
  (`program_admission.py:543-580`).
* **`--levels auto` is a closed loop almost nothing uses.** `level_ladder` builds four
  rungs at `LEVEL_OFFSETS_DB = (0, −5, −10, −15)` (`run_levels.py:30,88`) and sets rung
  *n+1* from rung *n*'s measured SPL window (`seat_level_reference.py:76-104`).
  **[MEASURED]** only `bass/*` carries it; `speaker`, `room`, `rear`, `seat`, `baseline`
  and `tournament` all resolve `levels=None`.

---

## 2. The program × layout matrix (252 cells, measured)

**Method.** For each layout fixture and each runnable row of `measurement_plans.json`, the
harness drove the production plan builder (`run_program` → `request_for_program` →
`prepare_plan_captures`, the three calls behind `jasper-round run --dry-run`), composed
every scheduled take with `programs.program_for_spec`, and admitted it with
`readmit_program_from_wav` (drivers) or `readmit_summed_program_from_wav` (candidate /
timing / candidate_branches) against a graph from `measurement_emit.compile_tuning_graph`.
Session levels −16.7, −23.0, −30.0; declared sensitivities woofer 84.0 / tweeter 109.2
(reproducing jts3's `derived_ceiling_dbfs=-25.2`); caps woofer 0.0 / tweeter −65.0;
`max_sweep_duration_s` 4.0; crossover order 4. All four layouts came from
`driver_safety.compute_driver_safety_profile` with no blocker issues; the product does
model a 3-way (`profile.py:45-53 DRIVER_ROLES_BY_WAY[3]`). **[MEASURED]** 153 pass, 99
fail; a verdict is the worst outcome over that row's takes, and it was identical at all
three levels in every cell — **no cell changed verdict with level.**

| program row          | one_way_passive  | two_way_active   | three_way_active | cardioid         |
|----------------------|------------------|------------------|------------------|------------------|
| speaker/mark         | ADMIT-REF        | PASS             | COMP-REF         | PASS             |
| baseline/express     | ADMIT-REF        | PASS             | COMP-REF         | PASS             |
| baseline/full        | ADMIT-REF        | PASS             | COMP-REF         | PASS             |
| tournament/express   | ADMIT-REF        | PASS             | COMP-REF         | PASS             |
| tournament/full      | ADMIT-REF        | PASS             | COMP-REF         | PASS             |
| branches/express     | PLAN-REF         | PASS             | PLAN-REF         | PASS             |
| front_rear/express   | ADMIT-REF        | ADMIT-REF        | ADMIT-REF        | PASS             |
| room/arm             | ADMIT-REF        | PASS             | PASS             | PASS             |
| room/seat            | ADMIT-REF        | PASS             | PASS             | PASS             |
| room/cloud           | ADMIT-REF        | PASS             | PASS             | PASS             |
| seat/cloud           | ADMIT-REF        | PASS             | PASS             | PASS             |
| seat/cube            | ADMIT-REF        | PASS             | PASS             | PASS             |
| seat/express         | ADMIT-REF        | PASS             | PASS             | PASS             |
| bass/axis            | COMP-REF         | PASS             | PASS             | PASS             |
| bass/cloud           | COMP-REF         | PASS             | PASS             | PASS             |
| bass/quick           | COMP-REF         | PASS             | PASS             | PASS             |
| bass/nearfield       | COMP-REF         | PASS             | PASS             | PASS             |
| rear/express         | ADMIT-REF        | PASS             | PASS             | PASS             |
| rear/wide            | ADMIT-REF        | PASS             | PASS             | PASS             |
| rear/pair            | ADMIT-REF        | ADMIT-REF        | ADMIT-REF        | PASS             |
| rear/pair_behind     | ADMIT-REF        | ADMIT-REF        | ADMIT-REF        | PASS             |

`PASS` = every scheduled take composed and was admitted at all three levels. `COMP-REF` =
the production composer raised. `ADMIT-REF` = the production gate refused. `PLAN-REF` =
the production plan builder raised.

### 2.1 Failing cells

| # | cells | verdict / code | root cause |
|---|---|---|---|
| **F1** | `one_way_passive` × {speaker/mark, baseline/*, tournament/*, room/*, seat/*, rear/express, rear/wide} — every summed take | `ADMIT-REF program_graph_not_proven` | **Product defect.** `readmit_summed_program_from_wav` proves the graph with `classify_bass_extension_graph`, which requires a *roleful* active graph; `runtime_contract.py:310` defines `roleful = role != "full_range"`, so a passive main has none. **[MEASURED]** `classify_output_contract` returns `roleful_outputs_present=False` for both the 1-channel mono *and* the 2-channel `passive_stereo_output_topology` fixture — structural, not a channel-count artifact. |
| **F2** | `one_way_passive` × {bass/axis, bass/cloud, bass/quick, bass/nearfield} | `COMP-REF BassStimulusRefused: bass_stimulus_targets_missing` | **Product defect (wrong altitude).** `bass_stimulus.py:45` hard-requires `"woofer" in role_targets`; a 1-way declares `full_range`. Arguably "does not apply", but it surfaces as a composer exception mid-run, not a plan-time refusal. |
| **F3** | `three_way_active` × {speaker/mark, baseline/express, baseline/full, tournament/express, tournament/full} — the MEASURE take | `COMP-REF ValueError: MEASURE takes one or two drivers` | **Product defect.** `audio_measurement/program.py:813` refuses `len(roles) > 2`. The product declares 3-way support and builds a valid 3-way context (**[MEASURED]** caps `{woofer 0.0, mid 0.0, tweeter −25.2}`, three role bands), but no driver-linearization program can be composed for it. |
| **F4** | `one_way_passive` × branches/express; `three_way_active` × branches/express | `PLAN-REF ValueError: a candidate_branches capture names two distinct measurement target ids, got ('full_range',) / ('woofer','mid','tweeter')` | 1-way: **legitimate** (nothing to branch). 3-way: **product defect** — `branch_target_ids_for` returns all declared roles and the branch program is hard-wired to two channels. Refused at plan time, which is the right altitude. |
| **F5** | {one_way_passive, two_way_active, three_way_active} × {front_rear/express, rear/pair, rear/pair_behind} | `ADMIT-REF program_target_not_mapped` | **Legitimate but at the wrong altitude.** `branch_target_ids_for(BRANCH_PAIR_FRONT_REAR, …)` (`measure_spec.py:431`) returns `('woofer', 'woofer:rear')` unconditionally, without consulting the topology. The box has no rear, so the gate refuses — at play time, after the mic is placed, instead of at plan time. |
| **F6** | `cardioid`/`two_way_active`/`three_way_active` × every summed row, **when the crossover order is 2 or a woofer's declared band top sits below the sweep top** | `ADMIT-REF program_segment_outside_limits` | **Product defect — the jts3 failure.** Fixed-band summed sweep (150 Hz–20 kHz) vs per-driver declared bands; rescued only by a ≥ 24 dB/oct low-pass. **[MEASURED]** refused at all three levels; **[MEASURED]** passes once the woofer's declared band is widened to 20 kHz. Not in the default-fixture matrix above (LR4) — a declared-fact-dependent failure, which is why it did not show up until jts3. |

### 2.2 Smallest fix per defect

| id | fix | file / function | covers |
|---|---|---|---|
| **X1** | Compose the summed sweep over the **declared** band, not a constant | `programs.py::SessionExcitation._summed_sweep` — pass `sweep_band_hz=measurement_band_hz(self.roles)` so `build_verify_program` stops falling back to `VERIFY_F_LO_HZ..VERIFY_F_HI_HZ` | **F6**; makes X2 a backstop |
| **X2** | Run the gate's own band/duration predicate at **plan** time | `preflight_live.py::read_preflight_facts` — drop the `if any(... == "drivers"): return ()` bail (`:86`), compose and `readmit_*` each scheduled take, surface refusals as `PreflightIssue` | **F1, F2, F5, F6** |
| **X3** | Register the refusal code | `crossover_v2/refusal_copy.py` — add `program_admission_refused` to `REASON_REGISTRY` | one line + copy |
| **X4** | Log the refused segments on the summed path | `program_admission.py::readmit_summed_program_from_wav` — reuse the `segments_refused` / `role_caps_dbfs` block written for `_evaluate_program` (`:543-580`) | pairs with X3 |
| **X5** | Teach the graph proof the passive main | same function — branch on `classify_output_contract(topology).roleful_outputs_present` and prove the flat contract via `runtime_contract.topology_allows_flat_dac_graph` | **F1** |
| **X6** | Let MEASURE take three drivers | `audio_measurement/program.py::build_measure_program:813` — widen `1 <= len(roles) <= 2` to `<= 3`, add the third sweep/segment id | **F3** (F4's 3-way half needs its own decision) |
| **X7** | Refuse an inapplicable program at plan time | `measure_spec.py::branch_target_ids_for` takes the topology and raises when the named pair is absent; `bass_stimulus.py:45`'s `woofer` requirement moves into `request_for_program` | **F2, F5** |
| **X8** | Make the composer's caps cover every physical target | `conductor_context.py:365` — iterate `role_targets` and key `caps_dbfs` by target id, so `min(caps)` covers `woofer:rear` | stands alone |

X1 and X8 are one principle twice: **compose against exactly what the gate enforces.**

---

## 3. Refusals that do not name themselves

174 distinct codes across 12 live stages plus 1 orphan lane. Verdicts: **KEEP 145 ·
KEEP-but-name-itself 12 · DEMOTE-to-disclosure 5 · DELETE 9 · MERGE 3.** Closed
vocabularies measured: `WALK_REFUSAL_REASONS` 18, `ProgramAdmissionRefusal` 10,
`ExcitationSafetyPlanRefusal` 3, `ExcitationRefusalReason` 22, alignment 10, topology 10,
driver 15, blend 15, room 17.

### 3.1 Misleading surfaces

| surface | where | the real cause it hides |
|---|---|---|
| **`internal_error`** | `plan_run.py:655` (`except BaseException`), `correction_run_host.py:223` | `ProgramPlaybackRefused` (`program_playback.py:44-50`), `ProgramAdmissionError`, `SessionVolumePlanError` and `NoProgramForPhaseError` carry no `.code`, and none is in any `aborts` map. `classify_program_failure`, which maps them correctly, is wired only into `web/_common.refusal_envelope` (`_common.py:163`) — the HTTP path. The admission refusal the gate logged in full detail (`program_admission.py:566-580`) reaches `jasper-round wait` as `internal_error`. |
| **`internal_error`** (analysis) | `correction_run_host.py:61` | every analyzer exception becomes `{"code": "internal_error", "error_type": <name>}`; the type name is banked, the code is not derived from it. |
| **`user_stopped`** | `arm_walk.py:575`, defaults at `:282,461,797` | *every* arm exit but `EXIT_STUCK` parks the session as "you stopped the measurement" — `EXIT_POWER_VOID`, `EXIT_MOVE_FAILED`, `EXIT_SESSION_FAILED`, `EXIT_IDLE_CEILING`, `EXIT_SETTLE_FLOOR`, `EXIT_REFUSED`, `EXIT_RELEASE_REJECTED`, `EXIT_STATUS_UNREACHABLE`, `EXIT_SESSION_STOPPED` and the signal-parked codes (`:112-150`). `EXIT_NAMES` (`:152`) already maps each one. |
| **`unreadable`** | `cli/crossover_prescriber.py:241-242` | `round_inputs.contract_sources:303-305` raises `CrossoverEvidencePacketError` when a bundle has no round artifacts — "this speaker has no banked round yet" — arriving as a generic `unreadable` beside real I/O faults. Downstream it reads as `prescription_fc_unknown` (`alignment_prescription.py:382,388`). |
| **`walk_level_policy_invalid`** | `angle_capture.py:371-394,466-488`; `preflight.py:188,201,226,233` | 14 conditions, from "mode unknown" to "predicted SPL would breach the commissioning stop by N dB". The last is an NN1 fact and deserves its own code. |
| **`active_excitation_request_outside_limits`** | `excitation_safety_plan.py:266-276,819` | four physical facts — band, level, duration, repeats — in one slug. `program_admission.py:462-479` records the bench misdiagnosis it caused ("read as a woofer level breach when the real refusal was DURATION") and works around it by re-formatting a log line. |
| **`program_segment_outside_limits`** | `program_admission.py:483,606,864` | the *default* arm of `_map_safety_plan_error` (`:598-606`), so unrelated safety-plan errors land here; and the rich detail computed at `:552-576` is log-only, never on the exception. |
| **`reset_compose_failed`** | `cli/round.py:247` | five exception classes on one slug, including `CandidateBankRefusal`, which already carries a specific `.code`. |
| **`capture_bundle_unavailable`** | `cli/round.py:184` via `:51-67` | no bundle / two bundles / `sessions_dir()` unreadable all return `""`. |
| **`drift_baselines_disagree`** | `capture_dispatch.py:198,222,226` | frame loss, glitch, discontinuity, sweep-schedule mismatch; two of the four set `evidence["guard"]`, two do not. |
| `refuse_non_retriable` / `refuse_extras_spent` | `admission.py:242-244` | **the counter-example, done right** — both carry `code=last_reason`, so the real fault is in the field. |

### 3.2 Stacks — two or more gates guarding one concept

| id | concept | gates | named owner |
|---|---|---|---|
| **S1** | band edges | `resolve_driver_excitation_ceilings` (`excitation_safety_plan.py:600-622`), `PreparedDriverExcitationPlan.__new__` (`:266-271`), `bass_stimulus.build_bass_program` (`bass_stimulus.py:56`), plus a dead fourth in `ExcitationRefusalReason` | the resolver computes the band; the prepared-plan check is the one comparison; `bass_stimulus` should consume it, not re-decide |
| **S2** | level | 5: session volume (`session_volume_plan.py:155-250`), composer back-off (`programs.py:196-290`), admission per segment/channel (`program_admission.py:483,520-527`), preflight SPL margin (`preflight.py:230-237`), live SPL stop | keep the composer and the tripwire (the NN2 pair); the preflight margin is a *prediction* of the live stop and should be non-blocking |
| **S3** | evidence sufficiency | 3 tiers: view refusals (disclosures), contract `evidence_status` (disclosures), and judges that **raise** (`RoundCapturesRefused`, `HarmonicEvidenceRefused`, `FeatureClassificationRefused`, `MeasurementAnalysisRefused`, `RoundViewsError`) | tier 3 should return the tier-1/2 shape `{"status": "unavailable", "reason": …}` — 3 vocabularies for one question |
| **S4** | level/session-shape validation | the dataclasses (`angle_capture.py:371-394,456-498`) and `preflight.preflight` (`:127-253`), which calls `replace(plan, mover=...)` purely to trigger the first (`:128-131`) | the dataclass; preflight adds only the facts it cannot see |
| **S5** | SPL calibration required | `preflight.py:165` and `door.py:136`, same code | the door (the live check) |
| **S6** | graph proof | `prove_candidate_config` (`preflight.py:154`), `compile_tuning_graph` (`measurement_emit.py:168-190`), Camilla `_admit_graph` (`camilla.py:890-896`) | **all three — see §6** |
| **S7** | mover / envelope | `WALK_OVER_MOVER_ENVELOPE` at statement time (`angle_capture.py:457,593`), `WALK_MOVER_MISMATCH` at session time (`:1109`) | different facts, correctly split — no change |

### 3.3 Deletions and demotions, ranked by (blocks real work) × (lines removed)

| id | change | est. lines | non-negotiable tier? |
|---|---|---:|---|
| **A-D1** | Delete the orphan excitation-admission lane: `ExcitationRefusalReason` (22 codes), `admit_excitation`, `excitation_artifacts.py`, `admitted_playback.py`, `validate_capture_admission_handoff` + `ActiveCaptureAdmissionHandoff`, and their two test files. Dead by a four-hop chain: `admit_excitation` ← `excitation_artifacts.readmit_excitation_for_playback` (`:889`) only; its entry points have zero callers in `jasper/`; `excitation_safety_plan.py` imports only the types (`:26-30`) and re-implements the comparison at `:266-271`. | **~2,700** | **YES** — it claims NN2 |
| **A-D2** | Split `active_excitation_request_outside_limits` into `..._band`, `..._level`, `..._duration`, `..._repeats`, carry the binding comparison on the exception, then delete the log-only re-formatting at `program_admission.py:552-576`. | −40 net | **YES** (NN2 caps) |
| **A-D3** | Route run-ladder failures through `classify_program_failure` (`correction_run_host.py:223`, `plan_run.py:655`); give `ProgramPlaybackRefused` and `SessionVolumePlanError` a `.code`. | ~+15 | NO |
| **A-D4** | Drop `measurement_band[0]` from the excitation floor for every role, keeping the `excitation_floor_widened_to_hard_band` log (`excitation_safety_plan.py:597-615,608`) as the disclosure. The module's own docstring already calls `measurement_band[1]` analysis-window metadata and excludes it from the upper edge. | ~−25 | **YES** (NN2 band) |
| **A-D5** | Name the arm's real exit — use the mapped `EXIT_NAMES` reason at `arm_walk.py:282,461,575,797`. | ~+20/−5 | NO |
| **A-D6** | Delete `walk_repeats_unsupported_yet` (`angle_capture.py:969,1094,1039`, `refusal_copy.py:136`) after verifying `plan_run`'s repeat loop end to end — a "yet" gate with no removal condition that makes `jasper-round run --repeats N` unusable. | ~−30 | NO |
| **A-D7** | Split the SPL-margin breach out of `walk_level_policy_invalid`, with the margin arithmetic in `evidence` (`preflight.py:230-237`). | ~+15 | **YES** (SPL margin) |
| **A-D8** | Demote `run_level_pilots_under_ambient` to non-blocking; its own comment names the removal condition (`preflight.py:243`), and capture-time `snr_floor` catches the real case with evidence. | ~−15 | NO |
| **A-D9** | Delete `walk_lateral_group_already_planned` (declared `angle_capture.py:996`, exported `:138`, member `:1045`, registry copy — **zero raisers**) and `boost_route_unavailable`'s permanently-closed route (`blend_prescription.py:209`, `prescription_contract.py:232-233`), which publishes `{"available": False, "detail": "The route refuses every boost today."}`. | ~−35 | NO |
| **A-D10** | Converge the evidence-sufficiency judges (S3) onto the view shape, so the round banks with the gap disclosed. | −60 to −120 | NO — largest behavioural change |

Aggregate if all ten land: roughly **−2,900 lines**, dominated by A-D1. A-D3 and A-D5
remove no lines and are the two that would have made the worst incidents self-diagnosing.

---

## 4. Separation of concerns and single source of truth

The stage sequence is real and mostly single-owner: declare → level → plan → compose →
admit → play → analyze → bank → views → contract → document → judge → compose candidate →
trial → apply, each with a named module, a named contract and one of **72 declared JSON
artifact kinds**. The leakage is **28 cross-module private imports** on this path.

**God files.** `runtime_contract.py` 5059 lines, 9 concerns, including
`_active_graph_evidence` @2590 — **820 lines** — plus a parked-graph builder and statefile
writer. `camilla_yaml.py` 4064: 7 emitters + the filter-name vocabulary (@895–@1030) +
linearization/blend validation (@1237–@1500) + headroom math (@1757–@1845) + 4
hearing-clamp asserts (@663,@730,@765,@2404). `crossover_v2_flow.py` 3640, holding
`CrossoverV2Session` — **2968 lines, 133 methods** (@623, `__init__` alone 340) — and
**106 pure re-export aliases** (@116–@500) that 67 test files import.
`evidence_packet.py` 2715: ~30 `_*_block` builders + a 278-line assembler @2223.
`feature_classifier.py` 2423: WAV loading + DSP primitives + the classifier @2136 +
operator copy @2407. Also over 1500: `spatial.py` 2119, `linearization_fit.py` 2096,
`staging.py` 2087, `verification.py` 1904, `round_views.py` 1901, `baseline_profile.py`
1860, `capture_plan.py` 1795, `intervention.py` 1646, `spatial_combine.py` 1585.

### 4.1 Concepts decided in more than one place

* **Speaker layout — 6 deciders**: two role tables (`profile.py:45`,
  `output_topology.py:97`), three adjacency tables (`profile.py:54`, `_common.py:25`,
  `staging.py:724` inline), a mode↔way bridge (`runtime_contract.py:1668`) and a
  hand-copied mirror (`:2556`). Two adjacency tables have no 1-way row, and
  `staging.py:630` derives `way_count` from the *length* of a second table. Nominated
  owner: `output_topology.py`.
* **"Measurement band" names two quantities**:
  `excitation_safety_plan.resolve_driver_measurement_band_hz` (`:673`) is the *declared*
  band, `crossover_v2/programs.measurement_band_hz` (`:137`) the union of
  *excitation-ceiling* bands. `capture_plan.py:93,1335` feeds the second into a kwarg named
  `measurement_band_hz=`; `prescription_contract.py:168-169` uses the first.
* **Two applied identities one word apart**: `applied["candidate_fingerprint"]`
  (`baseline_profile.py:249,365,887`) is the emitted **graph**,
  `applied["source"]["measured_candidate_fingerprint"]` (`:528,1649,1682`) the banked
  **document**; two surfaces disagree about which the operator sees
  (`web/correction_crossover_v2.py:237`, `applied_identity.py:14`).
* **One summed-compose body written twice** (`programs.py:118-128` and `:355-363`) and
  **one headroom-cost adapter written twice** (`crossover_v2_flow.py:2694`,
  `durable_state.py:575`), with a *third* headroom derivation parsing YAML text at
  `bench/derivation.py:303`.
* **`sets[].takes[]`: one minting site, eight raw readers.** `run_manifest.py:216` mints
  `set_id` and `RunManifest` (`:73`) is writer-side only, so `round_packet.py`,
  `round_copy.py`, `round_verdicts.py`, `round_packet_report.py`, `round_view_builders.py`,
  `speaker_fit.py`, `bass_comparison.py` and `bass_table_inputs.py` destructure raw dicts.
* **410 distinct refusal/issue code literals**, 19 `Enum` classes and ~80 refusal exception
  classes, against **27** keys in the one registry carrying operator copy
  (`refusal_copy.py:419`) — ~93% of refusals reach the operator with no owned sentence.
  *(A measured 95 entries and 1,302 lines in that file; not reconciled — see §7.)*
* **508 of 650 function-local imports carry no `# lazy:` reason**, against the
  [AGENTS.md](../../AGENTS.md) rule; only 3 real cycles are declared. Worst:
  `correction_crossover_v2_evidence.py` 34, `correction_crossover_v2.py` 32,
  `sound_active_speaker.py` 28.

### 4.2 Where "2-way" is assumed (C §4 row numbers)

| row | site | what breaks |
|---|---|---|
| 1–3 | `_common.py:25`, `staging.py:724-727`, `staging.py:630` | a 1-way gets no crossover UI (`.get(mode, ())`), falls into the 3-way branch and reports `crossover_preview_pair_missing` twice, and would `KeyError` on the way count (guarded by filtering the mode out) |
| 5 | `camilla_yaml.py:192` + `_PROGRAM_PROTECTION_RE` @:161 | the origin classifier looks only for woofer/tweeter program-protection filters; a `full_range` or `mid` filter is unrecognised |
| 6 | `crossover_v2_flow.py:682` | `self._tweeter = roles[1] if len(roles) == 2 else None` — **`None` on a 3-way**, so every tweeter-keyed flow path silently disables |
| 7 | `crossover_v2/diagnostics.py:378` | on a 3-way every `tweeter_*` journal field carries the **mid** (log-only) |
| 8 | `baseline_profile.py:1613` | `delay_role == roles[1]`: the sign may be wrong for a 3-way tweeter delay — **unverified** |
| **12** | `crossover_v2/harmonic_evidence.py:279` | the role set is inferred from the **count** of banked gains, so a front/rear branch round banks `{woofer, woofer:rear}` → count 2 → `("woofer","tweeter")` → mismatch → `()` → the caller refuses. **Cardioid harmonic evidence is refused, and it looks unintentional.** |
| 13–19 | `profile.py:333-335`, `camilla_yaml.py:550-566`, `measure_spec.py:431`, `output_topology.py:152`, `measurement_programs.py:87,395`, `cli/_run_request.py:75-76` | the cardioid's six *deliberate* special cases (ADR-0316/0318): rear requires the woofer role; the rear stage requires mono + exactly 3 outputs + no local sub; the front/rear pair is spelled, not resolved; `cardioid_cabinet_channels` needs exactly one "other role"; a rear purpose and a trial row; and a planning-time candidate write |

`fc_hz` is **not** a 2-way assumption — it is `float | None` throughout and
`ADJACENT_PAIRS_BY_WAY[1] = ()` is explicit. The 1-way main is a *declared* shape; what it
lacks is coverage in rows 1–3 and 5.

### 4.3 C's ranked simplification findings (ordering and disposition on #5384)

| id | what | net lines | tier |
|---|---|---:|---|
| **C-PR1** | Fix cardioid harmonic-evidence role inference (`harmonic_evidence.py:271-281`) — use the round's declared `branch_target_ids` | +2 | default |
| **C-PR2** | One layout table in `output_topology.py:97`; delete `_common.py:25`, `staging.py:724-727`, `runtime_contract.py:2556` | −70 | adversarial |
| **C-PR3** | Delete the `crossover_v2_flow.py:116-500` re-export block (106 aliases, ~15 prod + 39 test importers) | −150 | default |
| **C-PR4** | Rename the band collision → `excitation_hull_hz` | 0 | default |
| **C-PR5** | One summed-compose body (`programs.py:355-363` calls `compose_summed_program`) | −8 | default |
| **C-PR6** | One headroom-cost adapter (delete `durable_state.py:575-586`) | −12 | default |
| **C-PR7** | Typed reader for the run manifest (`BankedSets`), consumed by the 8 raw readers | −40 | default |
| **C-PR8** | Two applied identities, unambiguously named (`applied_identity.py` returns `{graph, document}`) | −10 | default |
| **C-PR9** | Split the graph-evidence walk into `graph_evidence_chain.py` (~1400 lines move, ~0 delete; `runtime_contract` 5059 → ~3600) | 0 | adversarial |
| **C-PR10** | Lift the shared Camilla vocabulary into `camilla_names.py` + `camilla_headroom.py`; delete `bench/derivation.py:303` | −40 | adversarial |
| **C-PR11** | Merge the two admission refusal enums (`ProgramAdmissionRefusal` onto `ExcitationRefusalReason`) | −30 | adversarial |
| **C-PR12** | Give every operator-visible refusal a registry entry, plus a `scripts/test-fast` guard | +200 / −? | default |

**Not proposed, deliberately:** splitting `CrossoverV2Session` — the largest win on paper,
but its 133 methods were not classified, so the boundary cannot be sized honestly.

---

## 5. Adversarial review of the proposed ADR-0328 (not in the tree at this SHA)

ADR-0328 ("the measurement band is the audio band") had **not** landed at `63914527e` —
`docs/adr/` stops at 0327. The review targeted a separate branch worktree, one commit
`3a389e330` over `origin/main`, and is recorded here because it settles the disposition of
the `measurement_band[0]` floor in A-D4. **Verdict: no blockers**; test selections in that
worktree ran 567 passed (133 s), then 1326 passed / 14 skipped / 0 failed (169 s).

| id | note |
|---|---|
| **N1** | The high-frequency upper edge stops passing through `MAX_DRIVER_TEST_FREQUENCY_HZ` (23 000 Hz): `upper = VERIFY_F_HI_HZ if role in HIGH_FREQUENCY_ROLES else …` (line 604). Safe only because `VERIFY_F_HI_HZ` happens to be 20 000, and nothing pins that ordering. **The only note touching the closed list.** Fix: `min(MAX_DRIVER_TEST_FREQUENCY_HZ, VERIFY_F_HI_HZ)`. |
| **N2** | A new module-scope import (`excitation_safety_plan.py:31`) drags numpy into every `jasper-doctor` run: doctor roster 557 modules with numpy vs 463 without; `cli.doctor.correction` 431 vs 336; `session_volume_plan` 321 vs 226. The Pi Zero 2 W (415 MB) is a supported target. |
| **N3** | A second copy of the MEASURE band rule survives in `harmonic_evidence._banked_sweep_durations_s` (`:208-258`), and its docstring — which promises no second copy — becomes false. A banked duration in `[0.011, 0.132)` would raise a bare `ValueError` instead of the typed refusal (unreachable with composer-realized values). |
| **N4** | Every round banked before that commit becomes unreplayable for harmonic evidence: same round, `program_id 88f46028de9b` (f1 = 30.0 Hz) vs `e99bf7f4a9a9` (f1 = 150.0 Hz), so `jasper-round-views distortion` raises `PROGRAM_NOT_REPRODUCIBLE` and blames the duration fit. Inherent to the change; undisclosed. |
| **N5** | A 1-way `full_range` role matches neither `LOW_FREQUENCY_ROLES` nor `HIGH_FREQUENCY_ROLES` (`driver_protection.py:26-30`), so it gets neither audio-band edge and its summed VERIFY top **narrows** from 20 000 to 18 000 Hz, while the ADR and both doc edits state the 20 Hz–20 kHz claim unconditionally. |
| **N6** | The SNR cost is real and unnamed: same energy over more octaves — woofer MEASURE 150–4 000 → 20–4 000 Hz is **−2.1 dB per octave**, the 2-way summed VERIFY **−1.5 dB**; 38% of the woofer sweep's time now lands below 150 Hz. The lever for the old per-octave SNR is duration, not the band. |
| **N7** | A second disclosure shape for a refused segment (a per-segment WARNING) duplicates the isolated path's aggregate `segments_refused` line (`program_admission.py:546-578`) — converge or cut. No production reader breaks. |
| **N8** | The new log renders a Python tuple into logfmt (`band_hz="(150.0, 20000.0)"`) where neighbouring band fields use `f"{lo:.1f}-{hi:.1f}"`, and the new test pins the repr. |

---

## 6. What is NOT a finding — kept on purpose

* **The hearing clamps.** `volume_limit_missing` / `volume_limit_positive`
  (`camilla_config_contract.py:161,169`), `patch_touches_devices`
  (`camilla.py:1090-1097`) and the three `CamillaVolumeError` raises
  (`camilla.py:1203,1228,1233`) are KEEP. CamillaDSP defaults to +50 dB when
  `devices.volume_limit` is absent, so the "missing" check *is* the clamp.
* **The commissioning SPL stop.** The live wired-capture ceiling and `spl_ceiling_exceeded`
  stay. §1 says the *prediction* is computed on the wrong graph, not that the stop should
  move; even with a one-owner level resolver, `peak <= cap` and the SPL stop remain
  backstops that should then never fire.
* **Declared driver caps and durations.** Every `program_admission` leg that re-derives
  peak, energy and channel mapping from the rendered PCM (`program_channel_peak_over_cap`,
  `program_manifest_peak_mismatch`, `program_out_of_segment_energy`,
  `program_gate_not_applied`) is KEEP; A-D2 is about the *name* of a refusal, never the
  check.
* **S6, the three-gate graph proof.** `prove_candidate_config` (`preflight.py:154`),
  `compile_tuning_graph`'s proof (`measurement_emit.py:168-190`) and the Camilla
  `_admit_graph` door (`camilla.py:890-896`) all stay: the one stack where the duplication
  *is* the non-negotiable. The Camilla door is the last word; the other two are cheap early
  reports of the same rule.
* **Patterns to copy, not remove.** `round_bank.py:299,317,322` computes a view, reports
  `{"status":"unavailable","reason":…}` and banks the round anyway; `capture_dispatch.assess`
  returns a fault *and* a next action (`retake_same` / `retake_louder` / `fix_and_retake` /
  `stop` / `accept`); `admission.py:242-244` carries the real fault in a field; the door
  re-emits the plan stage's own code (`correction_crossover_v2.py:698,724`).
* **Two matrix refusals are correct**: `one_way_passive × branches/express` (F4's first
  half — nothing to branch), and a cardioid whose rear was never commissioned refusing its
  summed takes with `program_graph_not_proven / excited_output_muted`.

---

## 7. Unverified, and the limits of this run

* **No hardware, no sound, no network, no test lane** for A, B and C; only the ADR-0328
  review ran pytest, in its own worktree.
* **A did not reproduce the jts3 admission incident on hardware.** Its claim that
  `program_segment_outside_limits` was the tweeter's level cap versus the −23 dB session
  level is **unverified**, and B's measurement contradicts it: the composer already clamps
  with `back_off_gain`, so the band or duration leg is the reachable one.
* **A's MERGE verdict** for `walk_stimulus_not_accepted` / `walk_polarity_not_accepted` /
  `walk_delay_not_accepted` (`angle_capture.py:343-346`) is a design call, not a proof of
  redundancy, and **A's line counts** come from `wc -l` and inspected ranges, not a diff.
* **B could not read jts3's own declared driver rows** (crossover order, the woofer's
  declared band top, `max_effective_peak_dbfs`, `max_sweep_duration_s`). F6 was reproduced
  by varying those facts on a fixture until the production gate emitted jts3's exact
  refusal, role set and band; naming *which* jts3 row is the one needs
  `curl -s http://jts.local:8780/state` or the saved declaration off the box.
* **Two of B's fixture inputs are authored, not device facts**: the applied candidate
  (`graphs.candidate_for`) and the cardioid rear stage
  (`rear_calibration.diagnostic_seed(48000)`, `rear_muted: False`).
* **C's refusal-code count (410) is a lower bound** — codes built by f-string or passed
  positionally are missed. C also marked unverified: the `baseline_profile.py:1613` 3-way
  delay sign; whether `bench/derivation` needs a text-only headroom path;
  `spatial_combine.py`'s concern split; and the `CrossoverV2Session` method classification.
* **One count is not reconciled between agents**: A measured `REASON_REGISTRY` at **95**
  entries in a 1,302-line `refusal_copy.py`; C measured **27** keys at
  `refusal_copy.py:419`. Any ratio derived from either carries that uncertainty.
