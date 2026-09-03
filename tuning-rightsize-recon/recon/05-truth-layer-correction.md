# Recon 05 — truth layer (`audio_measurement`) + `correction`, `attribution`, `calibration_agent`

HEAD `c032503`, 2026-09-02. Read-only. All counts reproducible from the commands cited.

## 0. Sizes and prose (baseline)

```
python3 -c "…ast docstring/comment counter…"   # see §5 for the script shape
```

| package | lines | comment | docstring | prose % |
|---|---|---|---|---|
| `jasper/audio_measurement` | 31,943 | 4,179 | 8,397 | **39.4 %** |
| `jasper/correction` | 16,883 | 1,247 | 2,538 | 22.4 % |
| `jasper/attribution` | 2,688 | 407 | 717 | **41.8 %** |
| `jasper/calibration_agent` | 4,633 | 175 | 407 | 12.6 % |
| **total** | **56,147** | 6,008 | 12,059 | **32.2 %** |

Tests for the four packages: 68,282 lines (`wc -l tests/test_audio_measurement*.py tests/test_correction*.py tests/test_attribution*.py tests/test_calibration_agent*.py`) — a 1.2:1 test-to-code ratio.

Narration markers: 183 `#NNNN` issue citations (131 in `audio_measurement` alone), 87 embedded `YYYY-MM-DD` dates, 14 "ruling N / W6.7 / campaign #NNNN / hardware run 18" references, 6 ADR citations. The AGENTS.md bar ("non-derivable constraints and `why`-pointers"; "no history, no dates/PR numbers") is missed by roughly 25:1 in favour of narration over pointers.

---

## 1. `audio_measurement` — what the modules actually are

The boundary rule **holds and is clean**. An AST walk over every import in the package (`ast.Import`/`ast.ImportFrom`, including relative and function-local) returns **zero** imports of `jasper.active_speaker`, `jasper.correction`, `jasper.web`, `jasper.cli`, `jasper.attribution`, `jasper.calibration_agent`. It is pinned by `tests/test_correction_boundary_ssot.py`. The 50 `grep` hits for `jasper.active_speaker` inside the package are **all** docstrings/comments (`grep -rn "jasper.active_speaker" jasper/audio_measurement/*.py`) — i.e. the boundary is enforced in code and then re-narrated 50 times in prose.

Classification by import profile (`ast` scan of top-level imports per module):

| layer | modules | lines |
|---|---|---|
| **Pure DSP / analysis** (numpy in, numbers out; no fs, no subprocess, no asyncio) | `analysis`, `deconv`, `gating`, `distortion`, `olive_metrics`, `snr_policy`, `gate_disclosure`, `spatial_combine`, `interference_nulls`, `program_analysis`, `quality`, `frame_fit`, `timeline_slip`, `alignment` | ~18,300 |
| **Pure data / vocabulary** (no imports at all beyond stdlib typing) | `comparison_bands`, `room_boundary`, `quality_model`, `mic_identity`, `frame_ledger`, `excitation`, `level_solver`, `delay_graph` | ~2,000 |
| **File readers / evidence persistence** | `bundles`, `calibration` (also HTTP: `urllib`), `evidence_identity`, `excitation_artifacts`, `excitation_admission`, `measurement_geometry`, `null_walk` | ~5,000 |
| **Live transport / side effects** (`subprocess`, `asyncio`, `threading`) | `playback` (1,611), `ramp` (1,752), `admitted_playback` (654), `wired_capture` (674), `correction_lane` (369), `wired_level_meter` (230), `sweep` (350) | ~5,640 |
| **Program authoring** | `program` (2,159 — builds the excitation program *and* renders PCM) | 2,159 |

**Finding (structure).** One flat package holds four altitudes. A reader cannot tell from the folder that `ramp.py` spawns `aplay` and `analysis.py` is a numpy library. Proposed: `audio_measurement/{dsp,evidence,transport}/` subpackages, `program.py` staying at the root as the contract both sides share. Zero behaviour change, ~40 import-line edits; low risk (mypy + the enumeration test catch it).

### 1a. `program_analysis.py` — 6,572 lines

Structure (`ast` top-level scan; the file already carries 10 banner comments that name the split):

| band | section (its own banner) | lines |
|---|---|---|
| 1–612 | module docstring, imports, **constants + their essays** | 612 |
| 613–842 | `sweep_band_crest_factor_db` + more constants | 230 |
| 843–1632 | **16 dataclasses** (`MeasurementPriors`, `CrossoverCandidate`, `ProgramAnalysis`, …) | 790 |
| 1635–1744 | low-level signal helpers | 110 |
| 1745–2189 | locate + integrity (all phases) | 445 |
| 2190–2511 | drift (MEASURE) | 322 |
| 2512–2728 | capture integrity (VERIFY) | 217 |
| 2729–4099 | per-driver response + alignment + candidate (MEASURE) | 1,371 |
| 4100–5027 | CHECK helpers (pilot/ambient/channel-map/gain solve) | 928 |
| 5028–6232 | phase dispatch (`analyze_program_capture`, `_analyze_measure`, `_build_candidate` 438 lines, `_analyze_verify` 244) | 1,205 |
| 6233–6572 | diagnostic summary (for `jasper.web.correction_crossover_v2`) | 340 |

48.7 % of the file is prose (1,485 comment + 1,716 docstring lines).

**It splits along its own banners, cleanly**, because the sections are phase-scoped and the cross-section coupling is the dataclasses:
`program_contracts.py` (the 16 dataclasses, 790) · `capture_locate.py` (locate + drift + integrity, ~1,100) · `driver_response.py` (per-driver + alignment + candidate, ~1,370) · `check_phase.py` (~930) · `program_analysis.py` (phase dispatch, ~1,200) · `analysis_summary.py` (340). Risk: **medium** — 30 external modules import from it (`grep -rl program_analysis jasper/`), so the split must keep `program_analysis` re-exporting. Proof: `scripts/test-merge` (mypy will catch every moved name).

**Dead analysis at HEAD.** A transitive reachability pass (AST identifier call-graph over the four packages, seeded from every identifier used in production code *outside* them plus each module's own module-level code, docstrings excluded) yields **24 unreachable defs / 1,035 lines**:

| lines | file | symbol | note |
|---|---|---|---|
| 144+126+61+53+36+5 = **425** | `audio_measurement/delay_graph.py` | `DelayGraphSnapshot`, `confirm_delay_candidate`, `DelayLaneBinding`, `_lane_proof`, `_candidate_lane`, `_topology_id` | 58 % of a 734-line module. Live half is only `quantized_delay_ms`, `prove_static_delay_binding`, `DelayGraphProofError` |
| 102+45 = **147** | `audio_measurement/snr_policy.py` | `sweep_excitation_bands`, `cap_null_depth_db` | only a `:func:` docstring reference survives in `driver_acoustics.py` |
| **326** | `correction/applied_speaker_evidence.py` | whole module | see §3 |
| **48** | `audio_measurement/level_solver.py` | whole module | see below |
| 123+20+10 = **153** | `correction/level_match.py` | `check_level_drift`, `DriftResult`, `DriftVerdict` | |
| 49 | `correction/runtime_safety.py` | `reset_config_path` | only a docstring mention in `active_speaker/runtime_contract.py:426` |
| 38 | `audio_measurement/program.py` | `courtesy_beep_to_stimulus_gap_s` | |
| 21 | `audio_measurement/room_boundary.py` | `room_boundary_hz` | the module's headline function |
| 16+8 | `audio_measurement/excitation_artifacts.py` | `refuse_historical_evidence`, `HistoricalExcitationEvidence` | |
| 6 | `correction/household_mic.py` | `clear_household_mic` | |

`level_solver.py` **says so itself** (`jasper/audio_measurement/level_solver.py:5-9`): *"Nothing in production reads this module today: the `CrossoverLevelLease` correction methods that consumed all of it are gone."* A module that documents its own deadness and stays is the clearest case in the scope. Delete it and `driver_solve_requirement_db`'s test.

The refactor doc's named cases (`forward_model.predict_sum`, `flat_spec_views.directivity_table`) are in `active_speaker`, not here; the equivalents here are the delay-graph confirmation half and `snr_policy.sweep_excitation_bands`.

---

## 2. Duplicated DSP primitives — every copy, and the one home

`smooth_fractional_octave` is **already a correct SSOT** (`audio_measurement/analysis.py:176`, 14 external consumers). The refactor doc's implied "smoothing is re-rolled" claim is **refuted**: `olive_metrics._smoothed_curve`, `correction/envelope._smoothed_curve` and `correction/session._smooth_capture` all delegate to it. Leave them.

| primitive | copies (file:line) | proposed one home |
|---|---|---|
| **Window** | `np.hanning` inlined at `program_analysis.py` (×2), `spatial_combine.py`, `snr_policy.py`, `gating.py`, `distortion.py`, `deconv.py`, plus `active_speaker/crossover_v2/feature_classifier.py` (×3) and `multiroom/sync_measure.py` — **10 call sites, no shared helper** | `analysis.analysis_window(n)`; each call site keeps its own length. Low value alone — bundle with a `dsp/` move. |
| **dB/linear** | `quality.dbfs` (the real one) · `snr_policy._dbfs:98` (byte-identical body, different floor constant) · `program_analysis._peak_dbfs:1640` (same body + a peak) · `correction/acoustic_quality.dbfs:116` (delegates — fine) · `correction/session._dbfs:130` (delegates — fine) · `active_speaker/program_admission._dbfs:193` · `chip_aec_alignment._dbfs_power/dbfs_rms` | `quality.dbfs(value, floor)`. Delete the two verbatim re-rolls in `snr_policy` and `program_analysis`. |
| **`20*log10` inline** | 19 occurrences in `program_analysis.py`, 9 in `spatial_combine.py`, 7 in `interference_nulls.py`, 5 in `snr_policy.py`, 4 in `distortion.py` (`grep -rn log10`) | leave — a bare `log10` is derivable; only the *floored* form deserves a helper. |
| **sha256 / fingerprint** | `evidence_identity.json_fingerprint:78` is the intended SSOT (38 production consumers). Re-rolls: `excitation_artifacts._canonical_json:239` + `_sha256:233` · `excitation_admission._canonical_json:84` + `_content_fingerprint:97` · `null_walk._canonical_payload:175` + `_payload_fingerprint:190` · `delay_graph._graph_fingerprint:105` · `admitted_playback._sha256:76` (identical to `excitation_artifacts._sha256` except the kwarg is `field_name` vs `field`) · `active_speaker/commissioning_evidence_store._canonical_json:168` · `active_speaker/driver_safety._canonical_json:174` | `evidence_identity`. 7 copies → 1; ~90 lines. |
| **`sha256_file`** | `audio_measurement/bundles.sha256_file:189` · `correction/fir_runtime._sha256_file:38` · `cli/doctor/_shared._sha256_file:244` · `bass_extension/bench/render._sha256_file:180` · `model_downloads.sha256_file:62` | `audio_measurement/bundles.sha256_file` for tuning; leave `model_downloads`. |
| **`_text`** | `excitation_artifacts:221` (`field=`) · `evidence_identity:36` (`field_name=`) — plus 8 more in `active_speaker` | `evidence_identity._text`, exported. |
| **`_mapping`** | none in `audio_measurement`/`correction`; 9 copies live in `active_speaker` (`controllability_ledger`, `crossover_contract`, `crossover_envelope_v2`, `setup_status`, `crossover_v2/evidence_packet`, `crossover_v2/frequency_view`, `design_draft`, `branch_peak`, `web_measurement`) | not my area — flag to the `active_speaker` agent. |
| **Bundle-manifest helpers** | `correction/bundles.py:24-30` imports **four private names** across a package boundary (`_is_exact_version`, `_manifest_path`, `_read_json`, plus `record_artifact`/`write_json_artifact`/`sha256_file` re-aliased) *and* re-declares `_is_positive_int:92` (copy of `audio_measurement/bundles.py:265`) and `_bundle_byte_size:68` (copy of `active_speaker/bundles.py:320`) | promote the three to public in `audio_measurement/bundles`; delete both copies. ~40 lines. |
| **RMS** | `np.sqrt(np.mean(x**2))` inlined 25× across the tuning tree (`grep -rn "np.sqrt(np.mean("`) | **leave it.** A one-liner helper here would be gold-plating; it is derivable and the AGENTS.md bar does not ask for it. |
| **Resampling** | `analysis.resample_log:277` is the SSOT; `calibration_agent/proposal_sim._resample:267` and `active_speaker/crossover_v2/delay_landscape._resample:187` are local linear-interp re-rolls on different grids | verify then converge `proposal_sim` onto `resample_log`; medium risk (grid semantics differ). |

Estimated: ~250 lines removed, all mechanical, proven by `scripts/test-merge`.

---

## 3. `correction/` — the room-correction wizard's engine, plus two things that are not

Yes, this is the PEQ room-correction wizard's engine, driven by `jasper/web/correction_setup.py` and shipped (`deploy/nginx-jasper.conf:467` `location /correction/`).

**Relation to `crossover_v2`: it shares the DSP substrate and re-rolls the orchestration.** `correction/acoustic_quality.py` imports `audio_measurement.{analysis, calibration, deconv, quality, snr_policy, sweep}` — the same primitives `crossover_v2` uses. What it does **not** share is the capture *program*: `crossover_v2` uses `program.py` + `program_analysis.py` (one multi-segment excitation capture, phase-dispatched); room correction uses `session.py`'s per-position sweep loop with `acoustic_quality.analyze_capture`. Room correction also never gates (`gating.py` has zero `correction/` importers). Two orchestrations over one substrate — defensible (ungated multi-position room average vs gated single-driver), but it means "one engine, four verbs" (REFACTOR §1) is not true today and cannot be made true by moving files alone.

**`session.py` (2,881) owns a 2,555-line god class.** `MeasurementSession` has 87 methods spanning: state machine + locking (`_set_state`, `state_changed_from`, `SessionStateGuard`), artifact writing (8 `_write_*`/`_record_*` methods), DSP invocation (`_smooth_capture`, `_quality_report_dict`, `_noise_report_dict`), acceptance judging (`_evaluate_acceptance`, 122 lines), apply/revert (`apply` 126, `auto_revert` 63), autolevel (7 methods, `run_autolevel` 99) and level-match (8 methods, `run_level_match` 115). Autolevel and level-match are already separate modules (`autolevel.py` 463, `level_match.py` 1,082) — the session holds only their lifecycle/locking, so lifting those 15 methods into the modules that own the work is the cheapest first cut (~350 lines out of the class).

**`envelope.py` (1,567) is a web presentation layer living in the engine package.** Its own docstring says so: *"This module is a pure presentation boundary… the browser is a pure renderer, and the Pi hands it one JSON object per step describing everything to draw"* (`jasper/correction/envelope.py:5-20`). It contains screen names, headline copy, `_verdict_text` (111 lines), `_next_action_for` (128 lines), `_nudges`, `_band_word`, `room_position_label`. Its only production callers are `jasper/web/correction_setup.py:3721,3738,3794`. It also reaches *up* into `jasper.calibration_agent.key_provisioning` (line 1486) for LLM availability copy.

It is **not** a duplicate of `status.py` — `status.py:392 session_snapshot` serializes the session for `/status` (capture/upload mechanics), `envelope.py` renders `/envelope` (screens and copy) — but two hand-written serializers of one object is the shape the refactor doc flagged, and only one of them belongs in `jasper/correction/`. **Move `envelope.py` to `jasper/web/correction_envelope.py`**; `-1,567` from the engine package, no line delta overall, low risk (2 call sites, `tests/test_correction_envelope.py` moves with it).

**`coordinator.py` (987) is mis-homed.** `measurement_window()` pauses every music lane, the wake loop and the outputd content meter — a *system-wide* facility. Its importers: 6 in `jasper/web/`, 2 in `jasper/active_speaker/`, 3 CLIs (`live_proof.py`, `measure.py`, `null_door.py`) — and effectively none that are room-correction-specific. `correction_lane.py:24-33` already documents exactly why a shared facility must not live in either feature package ("`jasper.correction` and `jasper.active_speaker` import each other, so neither package is 'below' the other"). The same argument applies here and was not followed. **Move to `audio_measurement/measurement_window.py`.**

**The circular dependency is real and is nearly all removable.**
- `correction → active_speaker`: `applied_speaker_evidence.py:74,213,216` and `runtime_safety.py:17`.
- `active_speaker → correction`: `seat_level_ramp.py:43` + `crossover_v2/door.py:163` (coordinator), `linearization_fit.py:130` (`correction.peq.design_peq`), `web_commissioning.py:608` (**imports the private `correction.playback._ensure_tone_wav`**), `:2126,:2723` (`play_sweep`).

Deleting `applied_speaker_evidence.py` (dead, below) + moving `coordinator.py` and `playback.py` down to `audio_measurement` leaves only `peq.design_peq` and `runtime_safety` — a one-directional edge. **This is the single highest-leverage structural move in my area.**

### Verdict on the named suspect modules

| module | lines | verdict | evidence |
|---|---|---|---|
| `applied_speaker_evidence.py` | 326 | **DEAD — delete** (+436-line test file) | Zero production importers (`grep -rn "applied_speaker_evidence import"` → only `tests/`). Its own docstring, line 15: *"Nothing in the room layer consumes this yet. It exists so the RC4 Tier B residual-trend correction has a seam to build on."* Speculative machinery for an unstarted feature; also the only `correction → active_speaker.candidate_bank` edge. |
| `interop.py` | 166 | **KEEP, trim** | Live via `bundle_tools.py:22` and `replay_artifacts.py:23`. Two of its five functions are dead: `format_frequency_response_text` (34) and `sweep_from_meta` (17). |
| `replay_artifacts.py` | 157 | **KEEP** | Live via `artifacts.py:27,159`. |
| `browser_audio.py` | 212 | **KEEP, review after the relay deletion** | One production caller (`web/correction_setup.py:2678`). It assesses `getUserMedia` metadata — check against PR #3724's stack; if the phone capture path goes, so does this. |
| `household_mic.py` | 310 | **KEEP** | Live via `web/correction_setup.py:1474`; also read by `crossover_v2/sweep_spec.py`. `clear_household_mic` (6 lines) is dead. |
| `state_guard.py` | 101 | **KEEP or inline** | One caller (`session.py:91`). A 101-line module for one class used once — inline into `session.py` only if `session.py` is being split anyway; otherwise leave. |
| `_numbers.py` | 21 | **KEEP** | `round_finite` used by `evidence.py:24` and `acoustic_quality.py:42`. A 21-line module for one function is thin but it is genuinely shared and costs nothing. |
| `spatial.py` | 189 | **KEEP — live** | `confidence.py:172,201,354,375,390,437`, `artifacts.py:523`, `strategy.py:326,373,704`, `variance_cap.py:436`. (Do not confuse with `active_speaker/crossover_v2/spatial.py`, a different module.) |

---

## 4. `attribution/` (2,688) — shipped substrate, no detector, no UI

**What it is:** the WO-1 slice of the attribution stage — closed sets, a mechanism registry, a `Finding`/`FindingSet` artifact, promotion paths, per-position evidence, session identity, bundle persistence.

**Is it live?** Yes, but only as a *writer*. Production callers:
- `web/correction_crossover_v2.py:3268-3287` calls `promote_carve_outs`; `:3467-3502` calls `promote_level_frame_disagreement`; `:3376` calls `read_finding_set`.
- `crossover_v2/record_store.py:45-51` and `ring_projection.py:74` use `FINDING_SET_SCHEMA` / `session_identity` / `findings_relative_path`.
- `crossover_v2/spatial.py:2988` calls `position_evidence_block`.

**Is it parked?** Partly, and the package says so. `jasper/attribution/__init__.py:35-38`: *"There is also **no detector** yet — WO-4 owns per-mechanism signature functions — and **no UI**, which is WO-6's."* `docs/historical/attribution-stage-plan.md` is tagged historical and says the open work orders are *"absorbed, not pursued independently from here"* while *"the shipped code under `jasper/attribution/` stands"*.

Concretely:
- `mechanisms.py` (287) is a registry with **3 entries** (`MECHANISM_HF_REFLECTION`/`M2`, `MECHANISM_BOUNDARY_SBIR`/`M5`, `MECHANISM_LEVEL_FRAME`/`M7`) and no consumer outside the package + tests.
- `closed_sets.py` (136) exports `FIX_CLASSES`, `CONFIDENCE_TIERS`, `EVIDENCE_TIERS`, `PROBES` — **zero** consumers outside `jasper/attribution/` (`grep -rn "FIX_CLASSES\|CONFIDENCE_TIERS\|EVIDENCE_TIERS\|PROBES" jasper/ | grep -v attribution/` → empty).
- It is absent from `docs/tuning-operator-runbook.md` entirely — the LLM driver cannot reach a finding through any documented tool.
- It carries **2,585 lines of tests** for 2,688 lines of code, for a stage with no detector.

**Recommendation: keep, do not grow, and shrink the test surface.** The findings written into bundles are durable evidence; deleting the writer strands them. But `closed_sets.py` and 2 of the 3 registry entries are vocabulary nobody reads. Proposed: fold `closed_sets.py` into `findings.py` (−80), keep only the mechanism entries the shipped promoters actually mint, and parametrize `test_attribution_findings.py` / `test_attribution_persistence.py` (they pin one schema fact per function; a 2,585→~900 collapse is realistic). Risk: low. If the owner does not intend WO-4/WO-6, the honest move is to delete the package and keep only what `record_store` and `promote_*` need (~800 lines) — that is an owner call, not a recon call.

---

## 5. `calibration_agent/` (4,633) — two halves, one live, one parked

**Live half (consumed by the room-correction wizard's "ask the tuning LLM" feature):**
`key_provisioning` ← `web/correction_tuning.py:147`, `web/correction_setup.py:5598`, `correction/envelope.py:1486` · `model_client` ← `web/correction_tuning.py:298,322` · `correction_advisor` ← `web/correction_setup.py:5608,5648` · `proposal_sim` + `response` ← `web/correction_setup.py:5694` · `advisor_context`, `curves`, `prompt` ← reached through `correction_advisor`. Also `scripts/tuning-llm-live-check.py:47`. ≈ 3,100 lines, live.

**Parked half — the `jasper-calibration-agent` CLI harness** (`pyproject.toml:204`):
`cli.py` (523) · `tools.py` (340) · `actions.py` (450) · `sound_actions.py` (93) = **1,406 lines**, plus `jasper/calibration_agent/corpus/` (12 markdown files, 1,347 lines, shipped as package data at `pyproject.toml:287`).

Evidence it is parked:
- `cli.py:30 render_markdown` — **229 lines** — has zero callers anywhere, including tests (`deadscan`, `NOPROD … test=0 doc=0`).
- `tools.get_measurement_summary`, `tools.analyze_peaks_nulls`, `tools.compute_schroeder` — zero production and zero test callers.
- `tools.build_intake`, `tools.look_up`, `prompt.build_advisor_prompt_package`, `actions.run_validated_action_plan`, `sound_actions.build_sound_audition_executor` are reachable **only from `cli.py`**.
- `tools.py:5-8` describes itself in the future tense: *"The **future** LLM layer should call this module for facts instead of reparsing bundles."* The shipped LLM layer (`correction_advisor` + `advisor_context`) does not.
- `jasper-calibration-agent` appears in **no** document outside its own corpus README (`grep -rn "jasper-calibration-agent" --include=*.md .` → one hit, `corpus/README.md:130`). It is not in `docs/tuning-operator-runbook.md`'s tool table.

**Is it superseded by the crossover_v2 loop?** Not by crossover_v2 — by `web/correction_tuning.py`, which is the shipped path to the same model for the same room-correction domain. The CLI is a second, unused front door onto a subset of the same contract.

**Proposed:** delete the CLI harness + its `pyproject.toml` entry point, or (if the owner wants an offline advisor) keep `cli.py` and delete `render_markdown` + `tools.py`'s three orphan functions and use `--json`. Risk **medium** — it is a declared console script, so this needs an owner yes. Line delta: −1,406 code, −1,347 corpus markdown, −789 tests (`test_calibration_agent_actions.py` 334, `_tools.py` 238, `_sound_actions.py` 117, minus what `response.py` still needs).

`prompt.py` (82) must stay — `correction_advisor.py` imports it.

Note a coupling to fix while there: `calibration_agent/proposal_sim.py:27` does `from jasper.correction import peq as _peq`, and `correction/envelope.py:1486` imports `calibration_agent.key_provisioning`. Two packages import each other for one constant each.

---

## 6. Prose over the bar — three representative blocks

Worst offenders by prose fraction (files ≥ 150 lines): `room_boundary.py` **86.5 %** · `correction_lane.py` **80.5 %** · `quality_model.py` 77.2 % · `mic_identity.py` 70.4 % · `timeline_slip.py` 69.5 % · `frame_fit.py` 67.9 % · `frame_ledger.py` 65.7 % · `variance_cap.py` 64.6 % · `promotion.py` 61.8 % · `interference_nulls.py` 56.8 % · `spatial_combine.py` 56.1 % · `gating.py` 55.1 % · `program_analysis.py` 48.7 %.

**(a) `jasper/audio_measurement/frame_ledger.py:5-122` — a 118-line lab notebook on a 312-line module.** It contains an incident date and forensics narrative ("On 2026-08-03 a MEASURE session lost frames in discrete steps of almost exactly 128"), an 8-row RST table of browser audio-graph hops, a prediction-and-outcome story ("That prediction was tested on 2026-08-15 and returned hop A"), an argument-from-elimination about which hop is the "leading candidate", a citation of a research doc's differing threshold, and a closing paragraph about what the module does *not* own. The derivable rule is three lines: *"Any nonzero frame discrepancy fails: a capture glitch is a splice, not drift, and a deconvolution has no tolerance proportional to fraction lost (#1765). Owns arithmetic and vocabulary only; the caller maps to a verdict."* **Additionally: the whole docstring describes the phone/browser capture chain, which PR #3724's stack is deleting.** The module survives (the wired path also emits `encoded_frames` — `wired_capture.py`), but the docstring will be describing a chain that no longer exists. Delta: −110.

**(b) `jasper/audio_measurement/correction_lane.py:5-183` — 183 prose lines for 2 constants and 4 functions (369-line module, 80.5 % prose).** It narrates the pre-refactor state ("the name was independently re-declared as `DEFAULT_ALSA_DEVICE`, `CORRECTION_SUBSTREAM`, `COMMISSION_TONE_ALSA_DEVICE`, `VOLUME_FLOOR_TONE_ALSA_DEVICE`, and two identically-named `PLAYBACK_DEVICE` constants … five distinct names spread across eleven files"), cites campaign IDs and arc labels ("Before P6c-0 (campaign #2285, U3 arc) ten call sites across six files each assembled their own inline argv"), and explains the module's own package placement at length. The non-derivable content is two facts: *"`correction_substream` is the one snd-aloop lane (`hw:Loopback,0,4`) shared by room correction and commissioning; `tests/test_correction_substream_ssot.py` is the drift guard."* and *"module-scope imports stay stdlib-only — socket-activated wizards must not pull numpy."* Delta: −150.

**(c) `jasper/audio_measurement/program_analysis.py:308-420` — 113 lines to declare 12 string constants.** Sample: `ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR` carries a 12-line `#:` essay ending *"Deliberately not spelled as `ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION` plus a suffix: that would make the plain value a strict PREFIX of this one, and a consumer matching an objective by substring would read the two as the same commitment."* — a defence against a hypothetical substring-matching consumer that does not exist. `ALIGNMENT_DECLARED_POLARITY_OBJECTIVES` gets 17 lines. Same file, line 521: `# jasper.active_speaker.level_trim.MAX_ATTENUATION_DB. Mirrored locally rather than imported because this module does not import jasper.active_speaker (see …)` — a mirrored constant whose comment restates the boundary rule the boundary test already pins, and which drifts silently if the other side changes. Delta for the constants band: −350 to −450.

**(d) `jasper/audio_measurement/__init__.py` — 136 lines, 132 of them a docstring that enumerates every module in the folder**, enforced by `tests/test_package_enumeration_contract.py` (122 lines). This is a hand-maintained `ls` with a CI gate. AGENTS.md: *"Do not restate here, in README, or in code what another file owns."* Delete both: **−250, low risk**, and it removes a merge-conflict magnet.

---

## 7. Refusal / gate vocabularies in these packages

**The "22 files re-roll `_refuse`" claim does not hold here.** In my four packages there are exactly **two** refusal helpers (`delay_graph._refuse:61` — in the dead half — and `spatial_combine._refused:1147`) and four `_issue`-shaped ones (`correction/evidence._issue_dicts`, `correction/runtime_safety._issue_detail`, `calibration_agent/actions._issue:439`, `calibration_agent/response._issue:346`). The re-roll problem is concentrated in `active_speaker`.

What **is** here is **44 exception/refusal classes** (`grep -rn "^class .*\(Error\|Refus\|Blocked\|Abort\|Cancelled\|Denied\|Absent\)"`) across 56k lines, and **five separate vocabularies of "this did not happen and here is why"**, each with its own shape:

| module | shape | scope | doctrine tier |
|---|---|---|---|
| `excitation_admission.py` (820) | closed-bounds admission, raises + `ExcitationRefusalReason` | excitation safety plan | **CLAMP** (non-negotiable) |
| `snr_policy.py` (840) | per-band, per-decision-class verdicts (`ok`/reduced/refused + dB missing) | measurement trust | INTEGRITY |
| `gate_disclosure.py` (585) | `GateDisclosure` record + `describe_gate` sentence — *never blocks* | gate effect | DISCLOSURE |
| `correction/acceptance.py` (565) | `accept` / `surface` / `revert_pending_confirm` / `revert` + per-band table | post-verify judgement | INTEGRITY |
| `correction/failures.py` (214) | 22 flat homeowner-facing string codes + retryability + recovery action | wizard copy | DISCLOSURE |

**These are genuinely five different concerns** (a clamp, a trust verdict, a disclosure record, an accept/revert decision, a copy vocabulary) and should not be collapsed into one primitive — the doctrine's CLAMP/INTEGRITY/DISCLOSURE split is exactly this. The smell is not that there are five; it is that **the verdict token spellings are not shared**: `grep -rhoE '"(ok|fail|refused|blocked|insufficient|unavailable|absent|degraded|unknown|surface|revert|accept)"'` over the two packages returns `"fail"` 69×, `"unknown"` 44×, `"ok"` 25×, `"unavailable"` 10×, `"revert"` 6×, `"insufficient"` 5×, `"surface"` 4×, `"refused"` 4×, `"blocked"` 3×, `"accept"` 3×, `"pass"` 1×, `"absent"` 1× — as bare literals, with no shared enum. One `audio_measurement/verdicts.py` holding the ~12 tokens (not the policies) is the 80/20 fix: it makes drift a mypy error instead of a test-string comparison, and it does not merge the five decision domains.

Serializers: 65 `to_dict`-shaped methods vs 24 `from_dict`-shaped in these packages (`grep -rc "def to_dict\|def as_dict\|def to_payload"` vs `"def from_dict\|def from_mapping"`). `audio_measurement` alone: 40 vs 19. Asymmetric hand-rolled serialization is real here but it is a whole-tree problem; I would not open it before the deletions land.

---

## 8. Stale docs / contradicted-at-HEAD

- `docs/historical/attribution-stage-plan.md` — correctly tagged historical, but `jasper/attribution/__init__.py:8` still cites it as the package's spec, and the module docstrings speak in WO-numbers ("as of WO-1", "WO-4 seeds the rest"). A reader at HEAD cannot tell which WOs exist.
- `jasper/audio_measurement/frame_ledger.py` docstring vs PR #3724/#3719 (phone-relay deletion): the entire narrative describes a chain being removed.
- `jasper/audio_measurement/level_solver.py:5` documents its own deadness rather than being deleted.
- `jasper/calibration_agent/tools.py:5` and `prompt.py:4` describe "the future LLM layer" / "a future OpenAI/Anthropic/Gemini adapter" that shipped 3,100 lines away in the same package (`model_client.py`).
- `jasper/correction/applied_speaker_evidence.py:15` — "Nothing in the room layer consumes this yet."
- `docs/tuning-operator-runbook.md` covers only `crossover_v2`: room correction (`jasper/correction/`), `attribution`, and `calibration_agent` are not in the tool menu at all. If the LLM driver is the primary operator, three of my four packages are unreachable from the documented surface.

---

## 9. Top moves (ranked)

| # | move | Δ lines | risk | proof |
|---|---|---|---|---|
| 1 | Delete `correction/applied_speaker_evidence.py` + its test. Removes the speculative Tier-B seam **and** the `correction → active_speaker.candidate_bank` edge. | −762 | **low** | zero prod importers; `scripts/test-merge` |
| 2 | Delete the dead half of `delay_graph.py` (`DelayGraphSnapshot`, `confirm_delay_candidate`, `DelayLaneBinding` + helpers) and prune `tests/test_audio_measurement_delay_graph.py` accordingly. | −425 code, −~500 test | low | reachability §1a; `prove_static_delay_binding` path still tested |
| 3 | Delete `audio_measurement/level_solver.py` (self-declared dead), `snr_policy.sweep_excitation_bands`/`cap_null_depth_db`, `correction/level_match.check_level_drift`+`DriftResult`+`DriftVerdict`, `runtime_safety.reset_config_path`, `room_boundary.room_boundary_hz`, `program.courtesy_beep_to_stimulus_gap_s`, `excitation_artifacts.refuse_historical_evidence`, `household_mic.clear_household_mic`, `interop.format_frequency_response_text`/`sweep_from_meta`. | −~560 | low | reachability §1a |
| 4 | Delete `audio_measurement/__init__.py`'s module enumeration + `tests/test_package_enumeration_contract.py`. | −250 | low | it restates `ls` |
| 5 | Move `correction/envelope.py` → `jasper/web/correction_envelope.py`. Presentation out of the engine package. | 0 net (−1,567 from `correction`) | low | 2 call sites; mypy |
| 6 | Move `correction/coordinator.py` → `audio_measurement/measurement_window.py` and `correction/playback.py` → `audio_measurement/` (making `_ensure_tone_wav` public). Together with #1 this leaves one directed edge (`active_speaker → correction.peq`) instead of a cycle. | 0 net (−1,141 from `correction`) | med | 11 importers; mypy + `scripts/test-merge` |
| 7 | Prose pass on the 12 files above the bar (`frame_ledger`, `correction_lane`, `room_boundary`, `quality_model`, `mic_identity`, `timeline_slip`, `frame_fit`, `variance_cap`, `promotion`, `gating`, `interference_nulls`, `spatial_combine`) + `program_analysis`'s constants band. Keep units/ranges/hardware quirks and one-line `See #NNNN` pointers; delete incident narratives, dates, campaign IDs, prediction/outcome stories, and defences of hypotheticals. | −2,500 to −3,500 | low, **but do it by hand, file by file** | reviewer read; no test asserts on prose (AGENTS.md forbids it) |
| 8 | Converge the fingerprint/sha/`_text` re-rolls onto `evidence_identity`; promote the three private names `correction/bundles.py` reaches for; delete the two verbatim `dbfs` copies. | −250 | low | `scripts/test-merge` |
| 9 | Split `program_analysis.py` along its own 10 banners into 6 modules (contracts / locate+drift+integrity / driver-response+alignment+candidate / check / dispatch / summary), keeping `program_analysis` as the re-export façade. | 0 net | **med** | 30 importers; mypy is the whole proof |
| 10 | Owner decision: retire the `jasper-calibration-agent` CLI harness (`cli.py`, `tools.py`, `actions.py`, `sound_actions.py`, `corpus/`) — superseded by `web/correction_tuning.py`. | −1,406 code, −1,347 md, −~790 test | **med** (declared console script) | needs owner yes; then `pyproject.toml:204` + `:287` |
| 11 | Introduce `audio_measurement/verdicts.py` for the ~12 shared verdict *tokens* (not the five policies). | +30, −~60 literals | low | mypy |
| 12 | Lift the autolevel/level-match lifecycle methods off `MeasurementSession` into `autolevel.py`/`level_match.py`. | −~350 from the god class | med | `tests/test_correction_session.py` |
| 13 | Attribution: fold `closed_sets.py` into `findings.py`; parametrize `test_attribution_findings.py` + `test_attribution_persistence.py` (2,585 → ~900). | −80 code, −1,700 test | low | `scripts/test-merge` |

**Rough total, moves 1–8 (all low risk, no owner decision needed): ≈ −4,000 to −5,000 lines of code and ≈ −2,200 lines of test, with zero behaviour change.** Moves 9–13 add another ~3,500 but need a decision or a bigger review.

## 10. Uncertainty

- The reachability pass (§1a) uses AST identifier edges with docstrings excluded and seeds from every non-test file outside the four packages plus `pyproject.toml`, `deploy/*.service`, shell and JS. It will **miss** dynamic dispatch (`getattr`, `importlib`, a name assembled from a string). I found no `importlib`/`getattr` string dispatch into these packages, but every deletion in moves 1–3 should be re-verified with a plain `grep` on the exact symbol before it lands.
- `browser_audio.py`'s fate depends on PR #3724's stack; I did not read that branch.
- Whether `attribution` should shrink or be finished (WO-4 detectors) is an owner question; I only established that no detector, no UI and no runbook entry exist at HEAD.
- I did not run the test suite. Every "proof" column is a proposal, not an observation.
