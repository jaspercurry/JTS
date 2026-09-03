# Tuning right-sizing — plan of record

**Status:** active. Wave 1 MERGED to main 2026-09-03 (#3837, 0649334a). Wave 2 not started. Owner-approved direction:
per-concern branches, one PR each, `/simplify` + `/code-review` before merge,
merge in waves so CI is not run once per PR.
**Owner:** jaspercurry. **Orchestrating session:**
https://claude.ai/code/session_014c7gAw7jA2r1pvBDE5wXkN
**Visual plan:** https://claude.ai/code/artifact/e6a5df33-4c86-48f0-9905-f2d35920b81d
**Evidence:** branch `claude/tuning-rightsize/recon-reports` holds this file
plus the nine top-down recon reports, the five bottom-up sizing reports, and
the re-runnable census/reachability scripts (`tuning-rightsize-recon/`). Never
merge that branch; fetch it.

## How to resume this program from a fresh session

1. `git fetch origin main claude/tuning-rightsize/recon-reports` and read
   `tuning-rightsize-recon/PLAN.md` (this file) from that branch. Read AGENTS.md.
2. List open PRs whose branch starts with `claude/tuning-rightsize/`; those are
   the in-flight wave. Check each for CI and review state.
3. Pick up the next unstarted rows in §5 in order; every row names its branch
   slug, its evidence report, its risk tier, and its proof.
4. Rules that do not change: one concern per PR; a rewrite PR deletes the old
   unit in the same PR; non-negotiable-tier diffs (hearing clamp, limiter,
   excitation ledger, commissioning SPL stop, `camilla_yaml` output path,
   `deploy/install.sh`) get `/adversarial-review` and a hardware pass; no CI
   line-count gate; no new doc tier; decisions go to `docs/adr/`.
5. The execution brief agents work from is §8. Keep it verbatim in the
   prompts you hand to sub-agents.

## 1. What this is

JTS's tuning side (active_speaker, audio_measurement, correction, attribution,
calibration_agent, the tuning CLIs, the correction/sound web surfaces, the
tuning docs and their tests) is 263k product lines, 357k test lines and 55k doc
lines. The operating model it serves is small: a person SSHes into the Pi and
asks an LLM agent to make the speaker sound better. The person fills in the
basic crossover configuration at jts.local (`/sound/`). The agent has a toolbox
of discrete CLIs; one is `measure`, whose mover is a turntable or a human, and
for a human the agent hands over a URL to the measurement-walk page. The other
tools analyze banked evidence, recommend, stage and apply. There is one
methodology document (`docs/tuning-methodology.md`), one doctrine
(`docs/measurement-loop-doctrine.md`) and one tool manual
(`docs/tuning-operator-runbook.md`). Things may have no code caller and still be
live: the LLM invokes them from the shell.

The goal: smaller, tighter, clear boundaries, legible from the folder
structure, easy to extend by hand. Separation of concerns, single source of
truth, clear contracts, 80/20.

## 2. Top-down findings (nine recon passes, 2026-09-02)

Reports: `tuning-rightsize-recon/recon/01…09-*.md`; census scripts in
`recon/census/`. Numbers at HEAD `c032503`.

Confirmed:
- Prose is 30.6% of product code, 22% of tests. In the engine
  (`crossover_v2/` + `crossover_v2_flow.py`) 48.7%; 9,898 lines sit in
  paragraphs citing an issue, date, ADR or ruling.
- Serialization is hand-rolled and write-only: 204 `to_dict` vs 23
  `from_dict`; 179 dataclasses have no inverse; 19 loose `SCHEMA_VERSION`s.
- Helpers re-rolled: `_utc_now` byte-identical ×12; `_text` ×11, fingerprint
  ×8-10, `_mapping` ×9, all divergent.
- Tests: 10,765 functions, 10.6% parametrized, 190 source-text pins, 805
  prose `match=` pins, 80 sibling clusters (507 tests); no marker/lane split.
- Canonical docs are healthy (<1% mutual overlap, 93-100% symbols resolve).
  The waste is the plan/cutover tier.

Corrected:
- Graph safety is NOT fragmented (one authority in `runtime_contract`).
- Smoothing is already SSOT (`analysis.smooth_fractional_octave`).
- Import boundaries hold in the AST. Real breaks: three CLI→web imports; the
  `correction ↔ active_speaker` cycle; lazy upward imports in `attribution`.
- Refusal re-rolls are concentrated in the engine. The truth layer's five
  vocabularies are five real concerns (CLAMP / INTEGRITY / DISCLOSURE); they
  need shared tokens, not a merge.
- The twin: `web/correction_crossover_v2.py` (7,832) imports nothing web and
  registers no route; all of it is engine work. The engine session cannot
  construct itself (`prepare_v2_session`, 965 lines, is the only filler of the
  44-kwarg constructor); two session objects (`CrossoverV2Session`,
  `TuningSession`) are alive at once.
- Four prescription doors are four copies (5,911 lines) with a reproduced bug:
  a legal JSON `10**400` escapes the alignment and topology doors as a bare
  `OverflowError`.
- Four volume owners; seven implementations of the hearing clamp
  `volume_limit ≤ 0`.
- Two "measure once" stacks in the CLI (`measure`, `null_door`); 24 tuning
  binaries, six invisible to the LLM's generated tool menu; five exit-code
  vocabularies.
- Dead code is modest but real (~3.5k zero-caller lines; 23 production symbols
  alive only through tests, 1,224 lines; 590 of 1,418 `__all__` exports with
  no importer).

Policy needs no change: AGENTS.md already states the comment bar, the
converge-duplicates rule and the parametrize rule. The debt predates them.

## 3. Bottom-up findings (five sizing passes, 2026-09-03)

Reports: `tuning-rightsize-recon/bottomup/A…E-*.md`; reachability script
`bottomup/reach.py`. Each unit was read, its primary engine modules attributed,
and a clean 80/20 size estimated on top of a shared substrate.

| unit | today | clean | cut | report |
|---|---:|---:|---:|---|
| Measurement-side tools (geometry, basic-profile, seat-level, angle-capture, arm-walk, measure, null, audition, round, round-bank, turntable adapter, `jasper-active-speaker`) | 16,985 | 3,080 | 82% | A |
| Analyze/recommend/save tools (round-views, prescriber, classify, distortion, delay-sweep, forward-model, gate-sweep, close-reference, project-ring; 6 off-menu binaries; 4 laptop scripts) | 43,059 | 8,200 | 81% | B |
| Web: config wizard + walk page, net of the relay chain (room-PEQ wizard is a separate product, +700) | 28,988 Py / 17,082 JS | 1,900 Py / ~4,750 JS+CSS | 93% / 72% | C |
| Shared measurement substrate (audio_measurement + measurement-side active_speaker) | 46,188 | 12,000 | 74% | D |
| Product runtime substrate (profile, camilla_yaml, runtime_contract, safety, startup, baseline apply) | 32,521 | 9,300 | 71% | E |
| Session engine (crossover_v2, the flow, the commissioning evidence machine, candidate modules) | 100,552 | 23,600 | 77% | E |
| **Python total** (units overlap ~9k; actual scope 263k) | **~272k** | **~58k** | **~78%** | |

Canonical docs (3.4k) are about right; the runbook's hand-written tables
should be generated.

Two inherited numbers were wrong:
1. "~23k lines of honest DSP" is ~6.5k: 6,504 code lines sit in functions that
   touch numpy at all. `deconv.py` and `analysis.py` are already right-sized.
2. The voice daemon imports 47,777 tuning-scope lines, but the cause is one
   file: `active_speaker/__init__.py` eagerly imports 26 submodules. Trim the
   façade, add a leaf `state_paths.py`, move one string constant
   (`LEVEL_MATCH_AXIS`), split the boot verb out of `cli/active_speaker.py`:
   797 lines. Pinned by an import-closure test. (Wave 1, PR 1-2b.)

Model check: today "measure with a mover" is `jasper-round open/wait/bank`
driving the web wizard session; `jasper-angle-capture` stages poses;
`jasper-arm-walk` answers the wizard's `/position-ready` for the turntable, or a
person does at the URL. `jasper-measure` is a lower single-pose instrument and
`jasper-null` a second copy of it. Four capture stacks exist (measure, null,
seat-level, wizard); the room-PEQ wizard is a fifth. The CLIs are HTTP clients
of the web page (`wizard_client.py`), which is the inversion that keeps the
engine in `jasper/web/`.

### Why top-down said 210k and bottom-up says 58k

| category | top-down removes | bottom-up removes | gap | why |
|---|---:|---:|---:|---|
| prose over the bar (80.6k today) | 32k | ~70k | +38k | top-down extrapolated from the history-marked floor; bottom-up applies the bar fully (clean body ≈ 7k prose) |
| blank lines, proportional (18k) | 0 | ~13k | +13k | never counted top-down |
| duplication (4 doors, 4 capture stacks, 3 playback stacks, 4 sine generators, 3 volume generations, 2 evidence record models, 2 load + 3 apply transactions, 7 emitters, 5 banked-round readers, 201 serializers) | 15k | ~30k | +15k | top-down converged helpers and doors; bottom-up converges every stack |
| over-abstraction + verbosity in legitimate code (26 functions ≥200 lines; 124+ frozen dataclasses; `seat_level_ramp` 4.3k→450; `program_analysis` 6.5k→2k; excitation clamp 3.5k→0.9k; evidence machine 15k→3k) | ~6k | ~55k | +50k | reachable only by rewriting a unit against its tests, which top-down excluded |
| dead / ghost / parked / v1 lane | 3.5k | ~25k | +10k | owner decisions not budgeted top-down |

### The ladder — and the chosen target

| tier | what it takes | lands at |
|---|---|---:|
| A | Phases 1–4 as first written, conservative prose | ~210k |
| B | same phases, prose bar applied fully, owner deletions taken, every duplicate stack converged | ~140–150k |
| **C** | **B plus rewrite-against-contract of the ~12 worst units, one PR each, old unit deleted in the same PR** | **~90–100k** |
| D | first-principles rebuild — the floor, not a path | ~58k |

**Target: C** (owner decision D15, default; confirm or override). Safety rule:
a rewrite PR carries the unit's existing behavior tests as its contract
(parametrized in Phase 1.4 first), and deletes the old unit in the same PR.
Never two copies alive across a merge.

## 4. Target shape

```
jasper/audio_measurement/      truth layer; optionally dsp/ evidence/ transport/
jasper/correction/             room-correction engine only (separate product, D11).
                               envelope.py → web; coordinator.py + playback.py →
                               audio_measurement. Cycle gone.
jasper/active_speaker/
  commissioning/               lane B: lifecycle · model · store · runtime · service
  crossover_v2/                THE engine. flow = walk state machine (~1.4k);
                               session.py = the measure verb; one refusal
                               primitive; one prescription door kit; one record
                               base; session_assembly / apply_transaction /
                               playback_transaction absorb the web twin.
  (top level)                  profile, camilla_yaml (one emitter), runtime_contract,
                               lane A (staging/startup, one load transaction),
                               measurement substrate, safety. Screen copy → web.
jasper/web/                    thin: correction_server (router as a dict), room
                               routes, page renders, balance. ~4k lines. No engine work.
jasper/cli/                    ~8 binaries: measure --mover human|turntable (absorbs
                               round, angle-capture, arm-walk, null) · read (absorbs
                               the advisory views) · seat-level · crossover-prescriber
                               · round-bank · basic-profile · audition · declare-geometry.
                               One exit vocabulary. Bench tools via python -m.
docs/                          doctrine · methodology · runbook (tables generated) ·
                               master plan · two information-design docs · ADRs ·
                               research/ · historical/. Plan/cutover tier deleted.
tests/                         ~150–180k lines; one parametrized family per behavior;
                               fixture modules, not imported test files; clamps heavy.
```

## 5. Phases and PR rows

Order: prose first (halves every later diff) → dead code and ghosts →
relocations at zero net (folder legibility) → consolidation → engine →
rewrite-against-contract. Every row: branch `claude/tuning-rightsize/<slug>`.
Risk tiers: low · med · high · **NN** (non-negotiable: adversarial review +
hardware pass).

### Phase 0 — unblock (owner)
- Merge **#3724** (CI green except `local-links`). Its stacked routes/deploy PRs
  delete the relay module, `jasper/capture_relay/`, ~7k test lines. Add
  `deploy/assets/shared/js/qr.js` (1,440) to that sweep. Until the chain lands,
  no row touches: `active_speaker/angle_capture.py`, `crossover_v2/capture_source.py`,
  `audio_measurement/wired_capture.py`, `correction/envelope.py`,
  `web/correction_crossover_v2*.py`, `web/correction_room_flow.py`,
  `web/correction_setup.py`, `capture_relay/**`, and their tests.
- **#3748** moves the XVF `SAVE_CONFIGURATION` ban; ask for a positive pin at
  the new site (its two tests only assert a denylist string).

### Phase 1 — free deletions (no behavior change; only D1/D2 items held)
| row | slug | concern | Δ | risk | proof | status |
|---|---|---|---:|---|---|---|
| 1.1a | `1-1a-doc-tier` | delete REFACTOR-CUTOVER (after reading §6.1–6.3 rulings), cutover maps ×2, cutover-briefs-acceptance, REVIEW-deep-audit-ledger, correction-journey-design, gating-v2-plan, correction-ux-wave3/**; archive 5 landed/historical docs; 2 research docs → research/; fix links, doc-map.toml | −12k | low | docs-impact / link check | wave 1 |
| 1.1b | `1-1b-runbook-fixes` | runbook: `gate_sweep_*`→`round_*` codes; door table regenerated; `project_ring` added to the generator roster; exit-code table generated; doctrine deviation map deleted; `docs/REFACTOR-2026-08.md` citations fixed | ~0 | low | menu generator `--check` | wave 1 |
| 1.1c | `1-1c-refactor-tuning` | promote REFACTOR-TUNING §4 rulings S1–S12 still binding to ADRs; restate doctrine's "112/100" census as families; delete the file | −1.9k | med | ADR review | **D2** |
| 1.2a | `1-2a-as-dead` | summed-graph lane (`commissioning_runtime`, `web_commissioning`), `staging.prepare_summed_commissioning_config`, 23 test-only symbols (1,224), 3 stale docstrings | −2.3k | low | grep proof per symbol | wave 1 |
| 1.2b | `1-2b-runtime-severance` | façade trim 209→~63, `state_paths.py` leaf, `LEVEL_MATCH_AXIS` leaf, TYPE_CHECKING for `DriverResponse`, boot verb split if needed, `tests/test_runtime_import_closure.py` | −330 | low | voice_daemon import closure 47,777→~800 | wave 1 |
| 1.2c | `1-2c-am-dead` | `delay_graph` dead half, `level_solver`, `snr_policy` orphans, `analysis.before_after_fill_segments`, `room_boundary_hz`, `courtesy_beep…`, `refuse_historical_evidence`, `__init__` enumeration + its test | −1.2k | low | grep proof | wave 1 |
| 1.2d | `1-2d-correction-dead` | `applied_speaker_evidence` (+test), `check_level_drift`+records, `reset_config_path`, `clear_household_mic`, 2 `interop` fns; fold `attribution/closed_sets` if unreferenced | −1.1k | low | grep proof | wave 1 |
| 1.2e | `1-2e-cli-dups` | `delay_sweep`/`forward_model` exit-code re-declarations; `crossover_envelope.py` shim; `alignment_walk`+`delay_sweep` collapse; `_REGIME_STOPS` → `measurement_programs`; `capture_geometry.summed_capture_geometry`, `CrossoverLevelReference`, `SOURCE_SAMPLE_RATE_HZ`, 9 test-only symbols; stray preset → tests/data | −500 | low | grep proof | wave 1 |
| 1.3 | `1-3-p<N>-<area>` | **prose to the bar, by hand**, 3-8 files per PR, file-disjoint. Wave 1: p1 flow · p2 spatial/capture_plan/durable_state/seams · p3 packet/coordinator/refusal_copy/verification · p4 the four doors + spool/planning · p5 truth layer big four + notebooks · p6 linearization/delta_probe/envelope_v2/flat_spec · p7 runtime half. Wave 2: remaining crossover_v2 modules; commissioning_*; staging/startup/baseline_profile; CLI; web (after #3724); correction/; attribution; calibration_agent | −55k…−65k total | low | tests for each module + test-fast; before/after prose counts in the PR | wave 1 (7 PRs) |
| 1.4 | `1-4-t<N>-<area>` | tests to the bar: one-line docstrings; delete 160 non-clamp source-text pins (sibling behavior pin named); parametrize 80 sibling clusters; prose `match=` → codes as touched. Keep the 15 non-negotiable files heavy (recon 09 §4) | −72k | low | collected count; assertion count unchanged for parametrization | wave 2 |
| 1.5 | `1-5-privatize-<pkg>` | ~310 pseudo-public names → `_`; delete assert-the-constant tests | −0.3k + tests | low | mypy + test-merge | wave 2 |
| 1.6 | `1-6-ghosts` | `active_speaker_attempts_replay` (+entry point, +664 tests); `scripts/harmonic-distortion-replay.py`, `severed-twin-replay.py`, `render-metric-views.py` (+`flat_spec_views.directivity_table` and 3 records) | −2.5k | low | grep + ci-classify | wave 1 |

### Phase 2 — homes and boundaries (≈0 net; after #3724)
| row | slug | concern | risk |
|---|---|---|---|
| 2.1 | `2-1-correction-cycle` | `correction/coordinator.py` → `audio_measurement/measurement_window.py`; `correction/playback.py` → `audio_measurement/`; `correction/envelope.py` → `web/correction_envelope.py`. One directed edge remains | low-med |
| 2.2 | `2-2-cli-web-imports` | `WiredStimulusCapture`, `resolve_conductor_context`, `status_payload` → `crossover_v2/`; prescriber's 550-line analyze/recommend block → `crossover_v2/status.py` | med |
| 2.3 | `2-3-envelope-copy-to-web` | `crossover_envelope_v2.py` screen copy + `_*_lines` renderers (~2.5k) → `jasper/web/`; verdict derivation stays; its 6.2k test splits | med |
| 2.4 | `2-4-dissolve-web-twin` | `web/correction_crossover_v2.py` → engine by concern: session assembly → `session_assembly.py`; play → `playback_transaction.py`; save/bank → `record_store`/`durable_state`; grading → `verification`; apply/rollback → `apply_transaction.py`; `_wired.py` → `capture_wired.py`; status projection → `crossover_envelope_v2`; `CrossoverLevelLease` → `active_speaker/` | med |
| 2.5 | `2-5-split-correction-setup` | `correction_server.py` (router as a dict) + `correction_room_routes.py`; graph/readiness/calibration (~2.9k) down into `correction/` and `calibration.py` | med |
| 2.6 | `2-6-regroup-rename` | `commissioning/` subpackage; `commissioning_capture`→`driver_capture_bridge`; `commissioning_coordinator` beside `setup_status`; `graph_safety`→`graph_view`; `fc_sweep`→`corner`; `composition`→`engine_binding`; `conductor`→`session` (388 refs); fold 6 one-function modules; research-prompt copy out of `driver_safety`; `web_commissioning`/`web_measurement` → `jasper/web/` | low |
| 2.7 | `2-7-am-subpackages` | optional: `audio_measurement/{dsp,evidence,transport}/`; `program_analysis` split along its 10 banners behind a façade | med |

### Phase 3 — one implementation per concern (fingerprints byte-stable)
| row | slug | concern | Δ | risk |
|---|---|---|---:|---|
| 3.1 | `3-1-refusal-primitive` | `Refusal` + `Refused` + `refuse()` in crossover_v2; registry gains optional household copy; 29→1 exception classes; 6 `*_REFUSAL_REASONS` → registry queries; 17 stray `REFUSE_*` join; `audio_measurement/verdicts.py` tokens | −900 | med |
| 3.2 | `3-2-door-kit` | `PrescriptionDoor` descriptor + generic reader; four modules keep domain rules; **fixes the OverflowError escape**; one parametrized contract suite (14.6k→5.5k test) | −2.2k / −9k test | med-high |
| 3.3 | `3-3-record-kit` | `JsonRecord` + `fields.py`; field-spec dataclasses replace `object.__setattr__` ladders in `commissioning_evidence/_receipt/_run`, `excitation_safety_plan` | −2.9k | med |
| 3.4 | `3-4-helpers` | fingerprint/sha/`_text`/`_mapping` → `evidence_identity`; power-mean ×6 + dB-floor → `analysis`; `_utc_now` ×12 → 1; `correction/bundles` private imports | −400 | low |
| 3.5 | `3-5-cli-surface` | `measure --mover human\|turntable` absorbs round/angle-capture/arm-walk/null (one `TuningSession`); `read` absorbs the 7 advisory tools; bench binaries un-shipped to `python -m`; one exit vocabulary; `run-crossover-round.py` folded into `jasper-round`; regenerate menu | −1.5k, 24→~8 binaries | med (null: **NN**) |
| 3.6 | `3-6-camilla-emitters` | seven emitters → one prelude / devices block / write tail; hearing clamp ×7 → `ensure_volume_limit_db` | −1.6k | **NN** |
| 3.7 | `3-7-stacks` | playback stacks 3→1; sine/WAV generators 4→1; volume/level generations 3→1 (`ramp`, `session_volume_plan`, `autolevel`); two evidence record models → one (store already shared); two startup load transactions → one; three apply/rollback transactions → one; five banked-round readers → one `BankedRound` | −8k | **NN** for volume/playback |
| 3.8 | `3-8-correction-session` | lift autolevel/level-match lifecycle off `MeasurementSession` | −350 | med |
| 3.9 | `3-9-test-fixtures` | runtime-contract five-file pile → one suite + `tests/active_speaker_fixtures.py`; de-dup 46 builders, 8 fake-Camilla copies | −19k test | med |

### Phase 4 — the engine (one PR each; hardware pass; last)
| row | slug | concern | Δ | risk |
|---|---|---|---:|---|
| 4.1 | `4-1-walk-state` | `SessionInputs` + `WalkState` records; `snapshot()` returns the record; 21 durable-state-only getters deleted; `durable_state` typed | −900 | med |
| 4.2 | `4-2-flow-extractions` | cloud close → `spatial`; verify/grade → `verification`/`coordinator`; measure verdict organ; candidate build → `planning`/`proposal`; lateral walk → `angle_capture` | −2.4k | med |
| 4.3 | `4-3-session-open` | `prepare_v2_session` → `CrossoverV2Session.open_measure()/open_verify()`; flow = walk, `TuningSession` = measure; flow lands ~1.4k | −900 | high |
| 4.4 | `4-4-one-volume-owner` | four volume owners → `session_volume_plan.py` | −450 | **NN** |

### Phase 5 — rewrite against contract (after 1–3; old unit deleted in the same PR)
Order by lines-saved over risk: `delta_probe` (3k→600) · `durable_state`
(2.2k→350) · `evidence_packet` (3.7k→1.3k) · `spatial`+`capture_plan`
(6.8k→1k) · `program_analysis` (6.5k→2k) · `seat_level_ramp`+`branch_peak`
(4.3k→450, **NN**) · excitation admission (3.5k→900, **NN**) · the
`commissioning_*` evidence machine (15k→3k) · `baseline_profile`'s 986-line
candidate builder → engine · `runtime_contract._active_graph_evidence`
(779 lines) → a declared check table. Slugs `5-<n>-<unit>`.

## 6. Owner decisions
| # | decision | default | status |
|---|---|---|---|
| D0 | per-concern branches + one PR each | — | **granted 2026-09-03** |
| D1 | `HANDOFF-bass-extension-plan.md` violates ADR-0199 (deleted, restored same day, no superseding ADR). ADR amending 0199 + rename out of `HANDOFF-`, or delete. `bass-extension-waves/**` (6,323) goes either way | rename + short ADR | open |
| D2 | `REFACTOR-TUNING-2026-08.md` + `REFACTOR-CUTOVER` §6 carry 15 rulings: promote still-binding ones to ADRs, restate the doctrine's census as families, delete both | do it | open (1.1c held) |
| D3 | `jasper-calibration-agent` CLI harness (1,406 + 1,347 md + 790 tests): unreferenced, superseded by `web/correction_tuning.py` | retire | open |
| D4 | `attribution/`: keep-and-shrink (tests 2,585→~900) or delete to ~800 | keep-and-shrink | open |
| D5 | `active_speaker_attempts_replay` study closed? `bass_extension_bench` live? | attempts-replay deleted (1.6, hard-wired to absent banks); bench un-shipped | 1.6 proceeds; confirm |
| D6 | `bench/` (2,266, invisible to the LLM): add emit-bench to the menu or move out | add to menu | open |
| D7 | superseded by D13 | | |
| D8 | two view models over one setup journey (`setup_status` vs `commissioning_coordinator`) | leave | open |
| D9 | `experiments/usb-turntable` → `movers/usb-turntable` | skip | open |
| D10 | one short ADR recording this program | write it | open |
| D11 | room-correction PEQ wizard (8.6k Py + 4.9k JS + `correction/` 17k) is a separate product with its own walk | keep outside; prose only | open |
| D12 | `jasper-project-ring`: methodology names its step; menu never listed it; nothing writes the layout it feeds | delete, amend methodology | open (1.1b adds it to the menu meanwhile) |
| D13 | v1 lane still wired to `/correction/` (`correction_crossover_backend`, `crossover_level_run`, `crossover_eligibility`, `commissioning_service`/`_isolated_producer`, ~6.4k): retire once no speaker carries the legacy-apply state | hold for owner's word | open |
| D14 | `calibration_agent` in-product LLM client stack (3.2k): the SSH-agent model replaces it | retire with D3 | open |
| D15 | target tier B (~145k) or C (~95k) | C | open |

## 7. Execution model
- The orchestrating session plans, dispatches, sniff-tests. Opus sub-agents
  execute one row each in an isolated worktree from `origin/main`, run
  `scripts/test-fast` (and the module's own tests; `test-merge` for
  import-structure changes), `/simplify` (code PRs) then `/code-review`
  (medium), fix what is real, push, open the PR with the line delta and the
  review record in the body.
- PRs merge in waves of 6–8 after the owner's triage; the rest of the wave
  rebases after each merge. Never one CI run per PR.
- Prose passes run 6–8 agents wide, file-disjoint. Rewrites (Phase 5) run one
  unit at a time.
- Every PR description carries its line delta. No CI line-count gate.
- Conflict policy with open PRs: avoid #3724's files until its chain lands;
  otherwise the smaller PR rebases.

## 8. Execution brief for sub-agents (verbatim)
See `tuning-rightsize-recon/EXEC.md` on the evidence branch — branch/PR
mechanics, the exclusion list, the prose bar, deletion rules, report format.

## 9. Current state (update this section as waves land)
- 2026-09-03 15:40Z: **wave 1 merged** — PR #3837 (25 branches integrated) is on
  main at 0649334a: 281 files, +20,221 / −61,631. Constituent PRs closed.
  #3836 (active_speaker summed-graph lane deletion) held open for the owner:
  REFACTOR-TUNING:771 row 4g rules that lane repaired-not-abandoned.
- Wave 2 backlog (start each on a fresh branch from main): finish the partial
  lanes (p12b measurement-side prose, p13a CLI prose, t1 source pins, t2 the
  other 11 heavy test files); 1.1c REFACTOR-TUNING/CUTOVER retirement (D2;
  ADR-0227 holds the §6.2 ruling); t3 test docstrings; the remaining
  runtime-severance moves (state_paths leaf, LEVEL_MATCH_AXIS leaf,
  TYPE_CHECKING DriverResponse, import-closure test — the façade is #3766's);
  second prose passes where the first landed above ~35% (p2, p6, p5); three
  pre-existing dead defs found by the simplify orphan scan; a handler-level
  test to replace the restored getsource pin in test_doctor_core; the LOW
  findings in review-unreviewed-branches.md; the doctrine line naming
  restore_pending_candidate_apply; driver_safety.py's #2874 pointer → ADR-0227.
- Then Phase 2 (relocations), reviewed PR by PR rather than as a batch.
- Lessons (keep): agents share the scratchpad (use mktemp); ≤6 full test
  lanes at once on the box; commit with explicit paths, never `git add -A` in
  an agent worktree; strip-and-diff ASTs is the proof for prose PRs; a
  `-X theirs` merge of a deletion branch can keep tests for deleted classes —
  re-run the branch's touched test files after integration; compression can
  falsify counterfactual comments.
