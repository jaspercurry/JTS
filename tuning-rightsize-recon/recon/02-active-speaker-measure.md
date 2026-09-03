# Recon 02 — `jasper/active_speaker/` measurement / analysis / candidates half

Scope: the 58 top-level `*.py` files not assigned elsewhere, plus `bench/` and
`presets/`. **41,121 lines of Python, 38% prose** (5,626 comment lines +
10,192 docstring lines), plus `bench/` 2,266 lines (38% prose) and
`presets/` 132 lines of JSON.

Reproduce every number below with the scripts in
`/tmp/.../scratchpad/{mine,prose,table,unused3,syms,moddoc}.py` (commands cited
per finding). Import graph built by AST (`imports.py`), not grep — a plain
grep for `from .X import` cross-matches `jasper/web/_common.py` and inflates
`_common`'s caller list from 4 to 39.

---

## 1. One line per file

`lines` = physical. `prose%` = (comments + docstring lines)/lines.
`P` = production (non-test) importers.
`Reach` = **MENU** if a runbook tool-menu CLI imports it directly; **web** if a
`jasper/web/*` module does and no menu CLI does; **indirect** if neither (only
other `active_speaker`/`crossover_v2` modules, non-menu CLIs, or `scripts/`).
Command: `python3 scratchpad/table.py`.

| file | lines | prose% | P | Reach | owns | direct consumers |
|---|--:|--:|--:|---|---|---|
| crossover_envelope_v2 | 4417 | 53 | 1 | indirect | v2 wizard **screen copy + layout** (82 defs, mostly `_*_lines`) | `crossover_envelope.py` only |
| linearization_fit | 3567 | 62 | 12 | web | Layer-1a per-driver PEQ/shelf fit engine | xo_v2 intervention/planning, bench, web, scripts |
| delta_probe | 2494 | 65 | 6 | indirect | realized-vs-commanded verdict **classifier** (pure) | xo_v2 verification/refusal_copy, bench, flow |
| seat_level_ramp | 2245 | 39 | 1 | MENU | closed-loop seat-SPL volume ramp | `cli/seat_level` only |
| measurement | 1792 | 14 | 25 | MENU | durable driver-check / measurement evidence store | commissioning_*, web, 4 CLIs |
| design_draft | 1570 | 12 | 17 | MENU | persisted design draft (roles, geometry, notes) | web, CLIs, multiroom |
| flat_spec | 1288 | 56 | 14 | indirect | the flat-linearization spec grader (bands, verdict) | xo_v2 (10 modules), audio_measurement |
| driver_acoustics | 1207 | 34 | 10 | MENU | mic-backed driver-check analysis | commissioning_*, xo_v2 sweep_spec, `cli/null_door` |
| flat_spec_views | 1169 | 49 | 2 | indirect | re-readings of a graded spec (pooling, directivity) | xo_v2 round_views/verification, `scripts/render-metric-views.py` |
| crossover_level_run | 1038 | 7 | 1 | web | durable id/timeout for **phone-relay** level runs (55 relay/phone mentions) | `web/correction_crossover_backend` |
| arm_walk | 1036 | 34 | 3 | MENU | turntable-arm position-gate driver | `cli/arm_walk`, xo_envelope_v2, `scripts/run-crossover-round.py` |
| angle_capture | 1027 | 50 | 5 | MENU | one stated angle walk (stops, regime, mover) | `cli/angle_capture`, spool, arm_walk, web |
| attempts_loop | 923 | 42 | 6 | indirect | improve/stop policy kernel for the tuning loop | xo_v2 durable_state/round_evidence, flow |
| measured_crossover_candidate | 892 | 42 | 5 | web | **v2** candidate model (trims + delay/polarity) | xo_v2 planning/record_store, web, candidate_bank |
| crossover_preview | 879 | 10 | 17 | MENU | no-audio preview: draft → future protected config | commissioning_*, web, 3 CLIs, multiroom |
| branch_chain | 859 | 60 | 22 | MENU | one driver branch's emitted chain → transfer | 22 modules; the most-shared truth helper here |
| linearization_envelope | 860 | 57 | 5 | indirect | per-bin correction-depth ceiling | linearization_fit, xo_v2 intervention |
| measured_candidate | 840 | 2 | 4 | indirect | **v1** candidate (`MeasuredElectricalCandidate`) | commissioning_{apply,service,isolated_producer}, baseline_profile |
| program_admission | 723 | 25 | 3 | web | excitation-program admission (peak/quiet-floor) | xo_v2 composition, program_playback, web |
| capture_geometry | 706 | 23 | 14 | web | mic-placement policy + placement proof | commissioning_*, web, xo_v2 sweep_spec |
| branch_peak | 692 | 36 | 1 | MENU | peak one branch receives for one stimulus | `cli/seat_level` only |
| audition | 654 | 25 | 2 | MENU | play at a reduced DSP layer, always restore | `cli/audition`, `control/state_aggregate` |
| crossover_contract | 640 | 9 | 6 | indirect | applied-graph ownership/readiness predicates | baseline_profile, setup_status, commissioning_* |
| repeat_admission | 604 | 18 | 5 | web | fail-closed repeat-playback admission | measurement, web, crossover_eligibility |
| angle_capture_spool | 604 | 50 | 4 | MENU | one staged walk waiting for a session | `cli/angle_capture`, arm_walk, web |
| graph_evidence | 598 | 30 | 7 | indirect | CamillaDSP graph verification vocabulary | runtime_contract, staging, startup_load |
| test_signal_plan | 523 | 22 | 14 | MENU | preset → per-driver test signal | commissioning_*, camilla_yaml, `cli/seat_level` |
| driver_base_trim | 463 | 46 | 1 | indirect | measured per-driver base trim, one writer | `baseline_profile` only |
| crossover_declaration | 448 | 39 | 4 | web | Sound's declared geometry vs a candidate's | web sound_setup / xo_v2 host, angle_capture |
| crossover_alignment | 422 | 37 | 9 | indirect | L2 polarity proposal + measurement-mode gate | xo_v2 alignment_prescription/planning, v1 commissioning |
| capture_provenance | 409 | 49 | 1 | web | acoustic context one capture was taken under | `web/correction_crossover_v2` only |
| seat_level_reference | 394 | 30 | 7 | MENU | the banked seat-SPL reference volume | 4 CLIs, seat_level_ramp, repeat_floor |
| model_error_store | 380 | 25 | 2 | web | durable per-speaker floor + misses | web v2 host, `cli/active_speaker_attempts_replay` |
| crossover_eligibility | 371 | 20 | 1 | web | automatic-measurement eligibility predicate | `web/correction_crossover_backend` only |
| runtime_convergence | 352 | 14 | 3 | web | park/commit/converge topology through Camilla | `output_topology_runtime`, web, `cli/active_speaker` |
| calibration_level | 337 | 14 | 18 | web | calibration-level contract for channel tests | commissioning_*, web, 2 CLIs |
| branch_target | 312 | **70** | 3 | indirect | the fit's target shape + gain mask | linearization_fit, xo_v2 intervention |
| measurement_document | 277 | 3 | 2 | MENU | saved JSON → frequency view | measurement_archive, `cli/round_views` |
| round_bank | 277 | 36 | 1 | MENU | bank a live session into the campaign home | `cli/round_bank` only |
| candidate_bank | 268 | 51 | 4 | MENU | find a banked candidate by fingerprint | web v2 host/republish, `cli/angle_capture`, correction |
| topology_tone | 244 | 4 | 3 | web | summed-crossover tone plans | web_commissioning, `web/sound_setup` |
| speech_stimulus | 232 | 3 | 2 | MENU | cached spoken stimulus (paid TTS) | web_commissioning, `cli/seat_level` |
| _common | 215 | 53 | 39 | web | shared issue/gate/finite-float vocabulary | 35 active_speaker modules + 4 web/cli |
| driver_pad | 209 | 40 | 2 | indirect | L-pad / series-resistor modeling | baseline_profile, design_draft |
| measurement_archive | 204 | 7 | 2 | MENU | saved measurements → frequency view (file adapter) | `cli/round_views`, `web/correction_measurements` |
| repeat_floor | 195 | 33 | 4 | MENU | measured repeat floor, one durable file | xo_v2 evidence/round_views, `cli/round_views` |
| program_playback | 179 | 42 | 4 | MENU | program-playback entry for a v2 session | xo_v2 composition/program_transaction, `cli/null_door` |
| frequency_view | 173 | 8 | 5 | MENU | renderer-neutral `jts_frequency_view/1` contract | archive, document, xo_v2 view, `cli/round_views`, web |
| measurement_programs | 160 | 29 | 2 | MENU | named measurement programs as data | angle_capture, `cli/angle_capture` |
| capture_entry_anchor | 143 | 36 | 2 | web | production-config stash for capture sequences | web_commissioning, xo backend |
| measurement_emit | 132 | 61 | 4 | MENU | the measurement graph's one `emit` callable | xo_v2 door, `cli/measure`, `cli/null_door`, web |
| controllability_ledger | 113 | 46 | 1 | web | read per-band rows out of banked round receipts | `web/correction_crossover_v2_status` only |
| audible_policy | 80 | 14 | 2 | indirect | which roles may be driven audibly | playback, topology_tone |
| delay_sweep | 75 | 43 | 3 | MENU | rename-only wrapper over `alignment_walk` + 2 bars | `cli/delay_sweep`, `cli/null_door`, xo_v2 delay_landscape |
| level_trim | 67 | 18 | 5 | indirect | attenuation-only adjacent-band level match | baseline_profile, driver_base_trim, both candidates |
| crossover_envelope | 52 | 46 | 1 | web | **log-and-forward shim** over `crossover_envelope_v2` | `web/correction_crossover_flow` only |
| alignment_walk | 49 | 35 | 2 | indirect | 12-line wrapper over `null_walk.NullWalkSpec` | commissioning_service, delay_sweep |
| tone_plan | 42 | 12 | 6 | indirect | tone vocabulary + preset loader | staging, commission_wiring, `__init__` |

`bench/` (2,266): `derivation.py` 605 · `compare.py` 675 · `loop.py` 923 ·
`__init__.py` 63 (95% prose). Sole consumer `jasper/cli/active_speaker_emit_bench.py`
— **not on the tool menu**; documented only in `docs/testing-tooling.md:625`.

`presets/`: two 66-line JSON worked examples. `epique_…_safe_v1.json` is
`tone_plan.DEFAULT_PRESET_RESOURCE` (live). `bc_de250_dayton_e150he44_v1.json`
has **no production reference** — only tests and `docs/historical/`.

---

## 2. The "v1 vs v2" pairs — verdict per pair

I checked each pair the brief named. Three are real strangler leftovers; four
are **complements that were mis-labelled as pairs** — worth stating so nobody
"finishes the strangler" by deleting a live half.

| pair | verdict | evidence |
|---|---|---|
| `crossover_envelope.py` vs `crossover_envelope_v2.py` | **v1 is a 52-line shim; delete it** | envelope.py has exactly one function, `build_crossover_envelope_logged`, which calls `build_crossover_envelope_v2(status)` and emits one `log_event`. Its only caller, `web/correction_crossover_flow.handle_envelope` (:173), already imports `correction_crossover_v2` on the same lines. |
| `measured_candidate.py` vs `measured_crossover_candidate.py` | **v1 alive only through the legacy apply branch** | `measured_crossover_candidate.py:6` states it is "a **new, standalone candidate model** — it does not extend or reuse `MeasuredElectricalCandidate` … the null-walk/evidence-store candidate built for the v1 flow". v1's only prod importers are `commissioning_{apply,service,isolated_producer}` + `baseline_profile`; `commissioning_service` is imported only by `web/correction_crossover_backend`, whose `apply_profile()` is reached only from the legacy-migration branch in `correction_crossover_flow.py:394` (`event=correction.crossover_legacy_profile_autopreserve`, fires when `active_applied_profile_snapshot_missing`). `baseline_profile.py` carries a `MeasuredElectricalCandidate \| MeasuredCrossoverCandidate` union with `# legacy … (no .linearization attribute)` branches at :2615, :2636, :2645, :2813. |
| `crossover_level_run.py` | **relay-era; dies with #3724/#3661** | 55 `relay`/`phone` mentions; exports `PHONE_TRANSPORT_GRACE_S`; only consumer is `correction_crossover_backend`, which takes 4 of its 8 exported names. 4 more exports (`CrossoverLevelRunConflict`, `…Disposition`, `…Failure`, `build_level_run_request`) have **zero** production importers. |
| `linearization_fit` vs `crossover_v2/driver_prescription` | **both live, not a pair** | Two *routes to the same candidate field*: the automatic solver (`intervention.py:1074 fit_driver_linearization`) and the LLM-authored prescription reader (`planning.py:895 driver_prescription_to_candidate_fields`, plus `cli/crossover_prescriber.py:400`). Neither supersedes the other. |
| `flat_spec` vs `crossover_v2/feature_classifier` | **not a pair** | `flat_spec` grades a curve against the spec bands (verdict/BandResult); `feature_classifier` classifies defects as driver/interference/room. `feature_classifier` does not import `flat_spec`; `crossover_v2/frequency_view.py:18` imports `evaluate_flat_spec`. Both live. |
| `delta_probe` vs `crossover_v2/delta_probe_run` | **not a pair — split by concern, misnamed** | `delta_probe.py` is the pure classifier (`classify_delta_probe`, verdict vocabulary); `delta_probe_run.py` (489 lines, 4 functions) is the session-side runner that calls it. The names read as v1/v2; they are `…_policy` and `…_run`. |
| `crossover_contract` / `crossover_declaration` / `crossover_eligibility` / `crossover_preview` | **mixed** | `crossover_preview` (17 importers) and `crossover_contract` (6) are shared v1-and-v2 truth and stay. `crossover_eligibility` is single-consumer v1-flow-only (`correction_crossover_backend.automatic_measurement_eligibility`) and 5 of its 10 public names have no production caller. `crossover_declaration` is v2-live (`web/sound_setup`, `correction_crossover_v2`). |

---

## 3. Dead code (verified across the whole repo, incl. tests, docs, `pyproject.toml`, `deploy/`)

Command: `python3 scratchpad/unused3.py` (token index over every `.py/.md/.toml/.json/.sh/.yaml/.js/.html/.service` file).

**Zero references anywhere — not even a test:**

| file:def | what | est. lines |
|---|--:|--:|
| `capture_geometry.py:269 summed_capture_geometry` + its only feeder `SUMMED_CAPTURE_GEOMETRY_BY_POLICY` (:85) | summed-analysis geometry resolver | ~30 |
| `capture_geometry.py:297 CrossoverLevelReference` + `:316 crossover_level_reference` | v1 automatic-level-match reference builder | ~55 |
| `speech_stimulus.py:32 SOURCE_SAMPLE_RATE_HZ` | stale constant | 2 |

**Defined and referenced only by tests** (no production caller, and not used
inside its own module either — the test is the only client):

`alignment_walk.DRIVER_DELAY_WALK_SCOPE` · `attempts_loop.useful_repeats` ·
`crossover_eligibility.render_repeat_progress` ·
`linearization_fit.LIFT_SUPPRESSION_REASONS` ·
`measured_crossover_candidate.build_and_prove_candidate_config` ·
`measurement.clear_active_comparison_set` · `repeat_admission.failure_status` ·
`repeat_admission.reservation_is_finished` ·
`seat_level_reference.LEVEL_REFUSAL_REASONS`.

**Whole-module dead-on-arrival:** none. Every one of the 58 files has at least
one production importer. The dead weight here is *within* files, not whole modules.

---

## 4. Prose over the AGENTS.md bar

AGENTS.md: comments are "only non-derivable constraints (units, ranges, timing,
hardware quirks) and `why`-pointers … No narration of what code does, no
history, no dates/PR numbers".

- **38% of the scope is prose** (15,818 of 41,121 lines).
- **1,852 lines are module docstrings alone** (`scratchpad/moddoc.py`), led by
  `delta_probe.py` **259 lines**, `linearization_fit.py` 114, `branch_peak.py` 86,
  `branch_target.py` 78, `capture_provenance.py` 68, `arm_walk.py` 68.
- **442 issue citations (`#NNNN`), 25 ADR citations, 135 dates** in this scope
  (`crossover_envelope_v2` 137 issues + 25 dates; `linearization_fit` 111 + 40;
  `delta_probe` 82 + 29).

Per-file estimate of prose **over the bar** (history/narration/reviewer-address,
excluding legitimate units/ranges pointers) — I sampled ~15 blocks per file in
the top ten and extrapolated; call it ±25%:

| file | prose lines | est. over bar |
|---|--:|--:|
| crossover_envelope_v2 | 2,346 | ~1,600 |
| linearization_fit | 2,203 | ~1,500 |
| delta_probe | 1,616 | ~1,250 |
| flat_spec + flat_spec_views | 1,297 | ~700 |
| seat_level_ramp | 882 | ~450 |
| branch_chain + branch_target | 787 | ~450 |
| linearization_envelope | 486 | ~250 |
| angle_capture + spool | 816 | ~400 |
| arm_walk | 349 | ~180 |
| everything else | ~5,000 | ~1,700 |
| **total** | **15,818** | **~8,500** |

Three representative blocks:

**(a) Incident history as a module docstring** — `capture_entry_anchor.py:5-36`
(32 docstring lines on a 143-line module):

> Why this exists: every automatic driver measurement (the level-match tone and
> each repeat sweep attempt) used to restore CamillaDSP's persisted production
> config path in its own per-attempt teardown. … **Hardware-reproduced on JTS3
> 2026-07-16 as deterministic sweep transport timeouts**: the double config
> bounce stalls the fan-in -> loopback-ring -> CamillaDSP capture chain and the
> sweep writer starves.

The non-derivable fact is one line ("de-anchor production once; per-attempt
teardown rolls back only the running graph"). The rest is a git-log entry.

**(b) A tuning-session diary in a comment** — `linearization_fit.py:166-175`:

> Why 18 (6 → 12 on 2026-07-24, → 18 after that night's JTS3 hardware run):
> the owner's "flat as a table top" directive requires the spend to actually
> REACH the measured deficit. The live JTS3 tweeter measured a 14.2–14.3 dB
> deficit at the reference-tier confidence ceiling (~16.4 kHz, the pre-ruling
> ceiling then in effect), but the 12 dB budget capped spend at ~9.2 across
> both quiet-room runs …

Three superseded values, two dates, one rig name. The constraint is "18 dB;
covers the measured JTS3 tweeter deficit with margin" — one line.

**(c) A docstring defending a duplicate** — `flat_spec_views.py:104-116`,
12 docstring lines on a **one-line** function:

> Deliberately a local definition rather than an import: the equivalent private
> helper in `jasper.active_speaker.flat_spec` is that module's own, and reaching
> across a module boundary for a private name would couple this view to an
> implementation detail it does not own.

The correct move is to promote one copy, not to write a paragraph explaining
why there are two (see §5).

---

## 5. Duplicated helpers (both copies named)

| concern | copies | note |
|---|---|---|
| **power mean** `10*log10(mean(10**(dB/10)))` | `flat_spec.py:518 _power_mean_db` · `flat_spec_views.py:104 _power_mean_scalar` · `flat_spec_views.py:775 _power_mean_across` (axis variant) · `audio_measurement/interference_nulls.py:941 _power_mean_db` · `audio_measurement/spatial_combine.py:2168` (inline) · `spatial_combine.py:1988` (inline) | **six** implementations of one identity; three carry docstrings explaining why they are not shared. |
| **magnitude→dB with epsilon floor** `20*log10(max(abs(x), ε))` | `bench/loop.py:391` · `crossover_v2/blend_correction.py:473` · `crossover_v2/gate_sweep.py:284` (ε=1e-15) · `crossover_v2/delay_landscape.py:262` · `crossover_v2/plan_assembly.py:252` · `branch_chain.py:822` · `linearization_fit.py` ×9 · `audio_measurement/program_analysis.py` ×4 | **three different epsilons** (1e-9 / 1e-12 / 1e-15) for the same floor. No shared helper exists. |
| **fractional-octave smoothing** | `audio_measurement/analysis.py:176 smooth_fractional_octave` (public) · `linearization_envelope.py:171 _ladder_smooth` · `crossover_v2/feature_classifier.py:938 smoothed_curve` + `:1009 _complex_smooth` · `audio_measurement/olive_metrics.py:174 _smoothed_curve` · `correction/envelope.py:447 _smoothed_curve` | five private re-rolls beside one public function. |
| **`finite_float`** | `jasper/json_fields.py:22` (repo-wide) · `active_speaker/_common.py:152` · plus 6 private `_finite_float` in this package (`design_draft:196`, `measurement_document:46`, `driver_pad:70`, `driver_safety:386`, `profile:146`, `camilla_yaml:474`, `bench/derivation:179`) | 9 copies, 4 distinct signatures. |
| **`_text` / `_mapping`** | `_text`: `commissioning_evidence`, `commissioning_receipt`, `crossover_level_run`, `design_draft`, `driver_safety`, `measurement`, `crossover_v2/{contracts,feature_classification,record_index}` (9). `_mapping`: `branch_peak`, `controllability_ledger`, `crossover_contract`, `crossover_envelope_v2`, `design_draft`, `setup_status`, `crossover_v2/{evidence_packet,frequency_view}` (8). | identical 2-line bodies. |
| **sha256** | `_common.py:178 require_sha256_hex` (the intended one) vs private `_sha256` in `measured_candidate:167`, `crossover_level_run:128`, `commissioning_{evidence,run,receipt,host}`, plus raw `hashlib.sha256(...).hexdigest()` in `measurement:113`, `capture_geometry:514`, `crossover_preview:{89,117}`, `baseline_profile:233`. | ≥12 sites, 6 signatures. |
| **serialization** | 47 hand-written `to_dict` vs 5 `from_dict`/`from_mapping` in this scope, nearly all on `@dataclass(frozen=True)`. | asymmetry means round-trip is untested by construction. |

---

## 6. Boundary violations / wrong altitude

1. **`crossover_envelope_v2.py` (4,417 lines) is wizard presentation living in
   the domain package.** 82 top-level defs; the bulk are `_verify_gate_lines`,
   `_flatness_details_lines`, `_per_band_flatness_lines`, `_carve_out_expert_lines`,
   `_attribution_lines`, `_done_nudges`… and 7 of its 11 public names are
   user-facing copy strings (`KEEP_ITERATING_TEXT`, `RIPPLE_RESERVATION_COPY`,
   `MIC_CALIBRATION_RESERVATION_COPY`, …) referenced **only by its 6,216-line
   test file**. REFACTOR-TUNING §1's "truth layer with no upward import" is
   satisfied on imports but violated on *content*: screen copy is front-end.
2. **`crossover_level_run.py` binds relay transport state inside the domain
   package** (`PHONE_TRANSPORT_GRACE_S`, 55 relay/phone mentions) for one web
   consumer. Overlaps PR #3724 / issue #3661 — do not touch until that lands.
3. **Same module name twice**: `active_speaker/frequency_view.py` (the
   contract) and `active_speaker/crossover_v2/frequency_view.py` (an adapter
   that imports the first). Legitimate layering, terrible naming.
4. **`bench/` ships to the Pi but is a laptop diagnostic.** 2,266 lines +
   4 test files, driven only by `jasper-active-speaker-emit-bench`, which is
   *not on the tool menu* and needs the pinned CamillaDSP binary. It is real
   work (it caught the shelf-Q class), but it is test tooling in the product
   package.

---

## 7. Abstractions that don't earn their keep

| what | evidence | move |
|---|---|---|
| **`delay_sweep.sweep_spec` → `alignment_walk.driver_delay_walk_spec` → `NullWalkSpec`** | Three layers, 124 lines, for one dataclass construction. `delay_sweep.py:65` is `return driver_delay_walk_spec(**kwargs)` with only a parameter rename (`upper_role`→`positive_delay_target_role`). `alignment_walk.py` is 12 lines of body. | collapse both into one `delay_sweep.py` (keep the two depth bars, which *are* non-derivable). −~80 |
| **`crossover_envelope.py`** | 52 lines; one function; one caller that already imports the v2 host. | inline the `log_event` at `correction_crossover_flow.py:187`, delete the module. −52 |
| **`controllability_ledger.py`** | 113 lines exporting **one** used function; its 3 module constants (`ROUND_RECEIPT_GLOB`, `MAX_RECEIPTS_SCANNED`, `MAX_RECEIPT_BYTES`) have no reference outside the file; sole consumer is `web/correction_crossover_v2_status`. | fold into the consumer or into `crossover_v2/round_views`. −~60 after prose trim |
| **`crossover_eligibility.py`** | 371 lines, one consumer, and 5 of 10 public names (`RepeatProgress`, `repeat_progress`, `render_repeat_progress`, `driver_repeat_completed`, `driver_acoustic_usable`, `AutomaticMeasurementEligibility`) have no production caller. A "pure predicate" module where half the surface is unreached. | keep `automatic_measurement_eligibility`; delete the rest with the v1 flow. −~150 |
| **253 public names that are module-private** | `scratchpad/unused3.py`: names with no reference outside their own file (+tests). Worst: `seat_level_ramp` 42/51, `arm_walk` 21/47, `audition` 19/30, `delta_probe` 17/43, `driver_base_trim` 13/32. These are constants exported so a test can assert them — the AGENTS.md test rule ("assert types, codes, and structured fields", "prefer one parametrized test") points the other way. | prefix `_`, delete the assert-the-constant tests. Large test-side delta. |
| **`presets/bc_de250_dayton_e150he44_v1.json`** | zero production reference; used by 4 test files and `docs/historical/`. | move to `tests/data/`. −66 from the shipped package |
| **Three modules for "saved measurement → frequency view"** | `frequency_view` (173) + `measurement_document` (277) + `measurement_archive` (204) = 654 lines, consumed only by `cli/round_views` and `web/correction_measurements`. | plausible 2-module merge (contract + adapter); low priority, all three are lean (3–8% prose). |

---

## 8. Reachability from the LLM's tool menu

Of 58 files: **25 MENU** (a runbook tool-menu CLI imports them directly),
**16 web-only**, **17 indirect** (reached only via other domain modules, a
non-menu CLI, or `scripts/`).

Never reached from the tool menu *or* the wizard, only from another domain
module: `alignment_walk`, `audible_policy`, `branch_target`, `crossover_alignment`,
`crossover_contract`, `crossover_envelope_v2`, `delta_probe`, `driver_base_trim`,
`driver_pad`, `flat_spec`, `flat_spec_views`, `graph_evidence`, `level_trim`,
`linearization_envelope`, `measured_candidate`, `tone_plan`, `attempts_loop`.
Most of those are legitimately truth-layer (the LLM sees their *output* through
`jasper-round-views` / `jasper-crossover-prescriber`), which is correct.

The exception worth flagging: **the LLM driver cannot reach the offline emit
bench.** `jasper-active-speaker-emit-bench` is the one instrument that grades
what CamillaDSP actually emits against the fit's claim, and it is absent from
the runbook's tool menu — so an LLM tuning this speaker has no way to know it
exists. Either add it to `scripts/generate-tuning-tool-menu.py`'s source or
retire it; leaving 2,266 lines of instrument invisible to its only driver is
the worst of both.

---

## 9. Top moves, ranked

| # | move | Δ lines | risk | proof |
|--:|---|--:|---|---|
| 1 | **Prose bar pass**, file by file, starting `delta_probe` (259-line docstring), `linearization_fit`, `crossover_envelope_v2`, `flat_spec`+`views`, `branch_target`. Keep units/ranges/hardware quirks and one-line ADR/issue pointers; delete history, superseded values, dated session diaries, reviewer-address. **Not a script — one PR per file.** | **−8,500** | low | no behavior change; `scripts/test-merge` green. Danger is deleting a real constraint: rule is "if you cannot re-derive the number from the code, keep one line naming it". |
| 2 | **Move `crossover_envelope_v2`'s screen copy + `_*_lines` renderers to `jasper/web/`**, leaving the verdict/state derivation in the domain package. | ~0 net, re-homes ~2,500 | med | the 6,216-line test file must split with it; keep `CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION` pinned. |
| 3 | **Delete verified dead code** (§3): `summed_capture_geometry`+map, `CrossoverLevelReference`+builder, `SOURCE_SAMPLE_RATE_HZ`, and the 9 tests-only symbols with their tests. | −250 (+ ~400 test) | low | grep proof in §3; `test-merge`. |
| 4 | **Collapse `alignment_walk` + `delay_sweep`** into one module; drop the rename layer. | −80 | low | `cli/delay_sweep`, `cli/null_door`, `commissioning_service`, `crossover_v2/delay_landscape` are the only callers. |
| 5 | **Delete `crossover_envelope.py`**, inline its `log_event`. | −52 (+1 file) | low | single caller at `correction_crossover_flow.py:187`. |
| 6 | **Converge the six power-mean copies** onto one public helper (`audio_measurement/analysis.py` is the natural home, next to `smooth_fractional_octave`), then the dB-floor helper with **one** epsilon. | −60 code, −120 prose | med | the epsilons differ (1e-9/1e-12/1e-15); pick per call-site deliberately, pin with one parametrized test. |
| 7 | **Privatize the 253 pseudo-public names** and delete the tests that exist only to assert a constant equals itself. Biggest wins: `seat_level_ramp`, `arm_walk`, `audition`, `driver_base_trim`. | −0 code, **large test delta** | low | mypy + `test-merge`; a genuinely-imported name will fail immediately. |
| 8 | **Retire the v1 candidate/commissioning stack** once the legacy-apply migration branch (`correction_crossover_flow.py:394`) can be dropped: `measured_candidate.py` (840), `crossover_eligibility.py` (371), the `baseline_profile` union branches. **Coordinate with the commissioning-half agent — most of this is their scope.** | −1,500+ | **high** | needs an owner ruling that no speaker still has `active_applied_profile_snapshot_missing`. |
| 9 | **Hold `crossover_level_run.py` (1,038)** until #3724/#3661 land, then re-check: it is relay machinery with one consumer and half its exports unused. | −600? | — | blocked on the relay-deletion stack. |
| 10 | **Decide on `bench/`**: add `jasper-active-speaker-emit-bench` to the tool menu, or move `bench/` out of the shipped package. | 0 or −2,266 | low | owner decision, not a code question. |

**Uncertainty I want to flag:** (a) the "over the bar" prose estimate is a
sample-and-extrapolate, ±25%; (b) I did not verify `seat_level_ramp`'s 2,245
lines against hardware behavior — it is the largest single-consumer module in
my scope and deserves its own read; (c) move #8 depends on facts owned by the
commissioning-half agent, and PR #3724 may already be moving #9's ground.
