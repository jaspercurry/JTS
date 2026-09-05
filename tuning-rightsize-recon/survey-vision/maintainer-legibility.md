# Lane: maintainer-legibility — can an external maintainer arrive and start modifying the tuning toolbox?

HEAD = `f4ff89731`. Read-only. Every number below is measured, not estimated.
Scripts (all under the scratch dir, none in the repo):

- `work/maintainer-legibility/impmap.py` — AST import closure per binary (§1)
- `work/maintainer-legibility/pathtrace.py` — BFS shortest import path between two modules (§1)
- `work/maintainer-legibility/flagged.py` — banners / top-level classes+defs / docstring / reach per big module (§2)
- `work/maintainer-legibility/asmap.py` — the `jasper/active_speaker/` cluster map (§4)

---

## §1 Binary → engine map

Method: `ast` walk of each CLI module for direct `jasper.*` imports; then BFS over
the whole `jasper/` import graph for the closure and for the shortest hop count to
any `jasper.audio_measurement.*` module ("truth-layer depth"). `from X import Y`
resolves `X.Y` to a module when one exists on disk, so submodule pulls are counted.

| binary | CLI module | CLI LOC | direct `jasper/` imports | **full import closure** (modules) | truth-layer depth | `audio_measurement` modules pulled | >1500-line modules on the path |
|---|---|---|---|---|---|---|---|
| jasper-declare-geometry | `jasper/cli/declare_geometry.py` | 165 | 3 | **9** | 1 | 3 | **0** |
| jasper-basic-profile | `jasper/cli/basic_profile.py` | 460 | 4 | **213** | 3 | 41 | 21 |
| jasper-round | `jasper/cli/round.py` | 452 | 6 | **264** | 2 | 42 | 29 |
| jasper-audition | `jasper/cli/audition.py` | 292 | 9 | **245** | 3 | 42 | 24 |
| jasper-angle-capture | `jasper/cli/angle_capture.py` | 988 | 13 | **263** | 1 | 42 | 28 |
| jasper-round-views | `jasper/cli/round_views/__init__.py` | 177 (pkg 2,157 / 14 files) | 16 | **328** | 2 | 45 | 32 |
| jasper-crossover-prescriber | `jasper/cli/crossover_prescriber.py` | 1,370 | 16 | **330** | 1 | 45 | 32 |
| jasper-seat-level | `jasper/cli/seat_level.py` | 739 | 24 | **230** | 1 | 44 | 23 |
| jasper-null | `jasper/cli/null_door.py` | 925 | 32 | **294** | 1 | 43 | 30 |
| jasper-measure | `jasper/cli/measure.py` | 1,143 | 32 | **402** | 1 | 44 | **38** |

**The shape finding.** Nine of the ten binaries have a two-hop-deep truth layer and a
200–400-module import closure. `jasper-basic-profile` is 460 lines of CLI with four
direct imports and **213 modules behind them**; `jasper-round` is 452 lines with six
direct imports and **264**. The CLI files are honest and small; the closure behind
them is the whole product. There is no tuning-only subgraph — a maintainer who opens
`jasper/cli/round.py` and follows imports lands in the whole speaker.

`declare_geometry` is the single counter-example and the shape to point at: 165 lines,
9-module closure, depth 1, zero big modules
(`jasper/cli/declare_geometry.py:1`; its only engine import is
`jasper/audio_measurement/measurement_geometry.py`, 183 lines).

### Surprising edges (BFS-traced, shortest path)

- **`jasper-measure` pulls three web wizards.** `j.cli.measure → j.web.correction_crossover_v2_wired → j.web.correction_crossover_v2` (7,167) `→ j.web.sound_setup` (5,550); `j.web.correction_setup` (5,204) and `j.web.rooms_setup` (1,999) are in the same closure. A measurement CLI's import graph contains ~19k lines of HTTP wizard.
- **`jasper-audition` pulls the multiroom reconciler** via `j.cli.audition → j.env_load → j.multiroom.reconcile` (2,508). Nine of ten binaries reach `jasper/multiroom/reconcile.py` this way.
- **`jasper-basic-profile` reaches the truth layer at depth 3**: `basic_profile → active_speaker.baseline_profile (4,182) → active_speaker.driver_acoustics → audio_measurement.analysis`. The engine layer is thicker than the CLI+truth layers combined.
- **`jasper-round-views` reaches its own engine through a name twin**: `j.cli.round_views → j.cli.round_views._common → j.active_speaker.crossover_v2.round_views` (`jasper/cli/round_views/_common.py:21`).

### Modules >1,500 lines on a binary's path — reached by how many of the ten

| module | LOC | reached by | directly imported by |
|---|---|---|---|
| `jasper/active_speaker/runtime_contract.py` | 5,152 | 9/10 | none (always transitive) |
| `jasper/active_speaker/crossover_v2_flow.py` | 4,636 | 6/10 | angle-capture |
| `jasper/active_speaker/baseline_profile.py` | 4,182 | 9/10 | basic-profile, measure, null |
| `jasper/active_speaker/camilla_yaml.py` | 4,028 | 9/10 | none |
| `jasper/active_speaker/commissioning_evidence.py` | 3,498 | 9/10 | none |
| `jasper/active_speaker/crossover_envelope_v2.py` | 3,497 | 1/10 (measure) | none |
| `jasper/active_speaker/crossover_v2/evidence_packet.py` | 3,144 | 5/10 | crossover-prescriber |
| `jasper/volume_coordinator.py` | 2,815 | 9/10 | audition, measure, null |
| `jasper/active_speaker/driver_safety.py` | 2,682 | 9/10 | none |
| `jasper/multiroom/reconcile.py` | 2,508 | 9/10 | none |
| `jasper/correction/session.py` | 2,444 | 1/10 (measure) | none |
| `jasper/active_speaker/crossover_v2/feature_classifier.py` | 2,453 | 2/10 | none |
| `jasper/output_topology.py` | 2,387 | 9/10 | measure, seat-level |
| `jasper/active_speaker/crossover_v2/spatial.py` | 2,368 | 7/10 | none |
| `jasper/active_speaker/web_commissioning.py` | 2,334 | 5/10 | **null** |
| `jasper/active_speaker/staging.py` | 2,315 | 9/10 | measure |
| `jasper/fanin/coupling_reconcile.py` | 2,153 | 9/10 | none |
| `jasper/active_speaker/commissioning_run.py` | 2,129 | 9/10 | none |
| `jasper/active_speaker/crossover_v2/round_views.py` | 2,091 | 2/10 | none |
| `jasper/audio_runtime_plan.py` | 2,065 | 9/10 | none |
| `jasper/active_speaker/crossover_v2/capture_plan.py` | 2,048 | 6/10 | none |
| `jasper/active_speaker/linearization_fit.py` | 2,028 | 9/10 | none |
| `jasper/active_speaker/crossover_v2/verification.py` | 2,023 | 7/10 | none |
| `jasper/source_intent.py` | 1,953 | 9/10 | none |
| `jasper/active_speaker/crossover_v2/driver_prescription.py` | 1,846 | 6/10 | crossover-prescriber |
| `jasper/active_speaker/commissioning_receipt.py` | 1,825 | 9/10 | none |
| `jasper/active_speaker/measurement.py` | 1,764 | 9/10 | measure, null, seat-level |
| `jasper/active_speaker/seat_level_ramp.py` | 1,753 | 1/10 (seat-level) | seat-level |
| `jasper/active_speaker/crossover_v2/intervention.py` | 1,701 | 6/10 | none |
| `jasper/web/correction_crossover_backend.py` | 1,700 | 1/10 (measure) | none |
| `jasper/audio_measurement/playback.py` | 1,611 | 9/10 | seat-level |
| `jasper/ring_assets.py` | 1,609 | 9/10 | none |
| `jasper/active_speaker/crossover_v2/durable_state.py` | 1,592 | 6/10 | none |
| `jasper/audio_measurement/spatial_combine.py` | 1,586 | 9/10 | none |
| `jasper/active_speaker/design_draft.py` | 1,553 | 9/10 | measure, seat-level |
| `jasper/audio_measurement/ramp.py` | 1,543 | 2/10 | none |
| `jasper/cli/active_speaker.py` | 1,510 | 9/10 | none |
| `jasper/web/correction_crossover_v2.py` | 7,167 | 1/10 (measure) | none |
| `jasper/web/sound_setup.py` | 5,550 | 1/10 (measure) | none |
| `jasper/web/correction_setup.py` | 5,204 | 1/10 (measure) | none |
| `jasper/web/rooms_setup.py` | 1,999 | 1/10 (measure) | none |

Note the third column: **almost none of these are imported directly by a CLI.**
The big modules arrive by transit. A maintainer changing `runtime_contract.py`
(5,152 lines) is changing something nine of ten tuning binaries load and none of
them names.

---

## §2 Are the flagged modules single-concern?

Evidence per line: `path:line` for the docstring, banner count from
`grep -n "^# ===\|^# ---\|^#####"`, top-level `class`/`def` counts from AST, and
"reached for different reasons" = ≥2 of the ten binaries reach it via
*different* direct importers.

| module:line | LOC | classes / top-level defs | banners | verdict | evidence |
|---|---|---|---|---|---|
| `jasper/active_speaker/runtime_contract.py:5` | 5,152 | 5 / **97** | **0** | **NOT single-concern** | 97 module-level functions, 5 classes, zero section markers, one 8-word docstring ("Runtime safety contract for roleful active-speaker CamillaDSP graphs"). Reached by 9/10 binaries, directly imported by 0 — nobody owns its entry. |
| `jasper/active_speaker/crossover_v2_flow.py:5` | 4,636 | 4 / 15 | 8 | **NOT single-concern** — the banners say so | `:232` "phase vocabulary (owned by crossover_v2.journey)", `:264` "the walk this session will do", `:454` "tuning constants", `:512` "pure helpers", `:614` "seams + snapshot", `:752` "cloud group bands + honesty pipeline", `:809` "what one candidate build produced", `:871` "the session". Eight named concerns, three of which the banner itself says are *owned elsewhere*. |
| `jasper/active_speaker/baseline_profile.py:5` | 4,182 | **0** / 48 | **0** | **NOT single-concern** | Docstring nouns: compile, apply, accepted, baseline, profile (four). Zero classes, 48 free functions, no sections. Reached by 9/10; directly imported by basic-profile (to *read* a profile), measure and null (to *write* one) — two lifecycles. |
| `jasper/active_speaker/camilla_yaml.py:5` | 4,028 | 1 / **98** | 4 | **NOT single-concern** | Banners at `:881` subwoofer/bass-management names, `:912` public filter-name vocabulary, `:1133` Layer-1a linearization emission, `:2804` channel-routed program graph. Emission + naming vocabulary + measurement-session graph in one file. |
| `jasper/active_speaker/commissioning_evidence.py:5` | 3,498 | 16 / 39 | 0 | **PARTIAL** | 16 dataclasses around one noun ("per-region evidence authority"), but zero section markers over 3.5k lines and no direct importer among the ten. Single-concern by docstring, unnavigable by structure. |
| `jasper/active_speaker/crossover_envelope_v2.py:5` | 3,497 | **0** / 87 | 7 | **NOT single-concern** | Banners: `:832` review interlude, `:1506` "G1's ripple reservation (#2087)", `:1822` tier chooser, `:2049` session recency, `:2173` banked findings, plus an empty title-less rule pair at `:2903`/`:2905`. Five unrelated screen concerns. Reached by 1/10 (measure). |
| `jasper/active_speaker/crossover_v2/evidence_packet.py:5` | 3,144 | 1 / 51 | 1 | **PARTIAL** | One noun ("one round's banked evidence"), one class, but 51 free functions and a single banner at `:2959`. Reached by 5/10 binaries. |
| `jasper/active_speaker/driver_safety.py:5` | 2,682 | 3 / 47 | 1 | **NOT single-concern** | Docstring names two things: "Driver-research **request** and confirmed **safety-profile** contracts". Its one banner, `:1283` "One implausible low limit, two authors, two answers", is an admission that two owners disagree inside this file. Non-negotiable tier. |
| `jasper/active_speaker/crossover_v2/feature_classifier.py:5` | 2,453 | 5 / 40 | **0** | **PARTIAL** | One question in the docstring ("What KIND of feature is that"), but 2.4k lines with no section markers. Reached by 2/10 for the same reason (both grade features). |
| `jasper/active_speaker/crossover_v2/spatial.py:5` | 2,368 | 13 / 41 | 19 | **NOT single-concern** | Docstring already lists three questions ("is it evidence, what does it record, does the group want another one"). 19 banners, most title-less rules; only `:457` "lateral evidence" names anything. Reached by 7/10. |
| `jasper/active_speaker/web_commissioning.py:5` | 2,334 | 2 / 53 | 0 | **NOT single-concern** | Docstring says "**Shared web/operator** orchestration" — two audiences named in the docstring. Directly imported by `jasper/cli/null_door.py` (a CLI) and by `jasper/web/correction_setup.py`, `jasper/web/sound_setup.py`, `jasper/mux.py`, `jasper/fanin/ring_health.py` — 16 importers across web, CLI, mux and fan-in. |
| `jasper/active_speaker/staging.py:5` | 2,315 | 1 / 32 | 0 | **PARTIAL** | One verb ("stage protected startup configs without loading hardware"), one class, 32 functions, no sections. Reached by 9/10, directly imported by 1 (measure). |
| `jasper/active_speaker/commissioning_run.py:5` | 2,129 | 8 / 24 | 0 | **NOT single-concern** | Docstring names two: "Durable **identity** and **lifecycle journal**". Reached by 9/10, imported directly by none. |
| `jasper/active_speaker/crossover_v2/round_views.py:5` | 2,091 | 15 / 29 | **22** | **NOT single-concern** | 22 banners for 15 classes — the file is 22 sections wearing one filename, and the CLI package `jasper/cli/round_views/` splits the same surface into 14 files. Reached by 2/10. |

**Not on any of the ten binaries' paths:** none of the fourteen named. All fourteen
are reachable from at least one binary. The nearest thing to an off-path giant is
`jasper/web/correction_crossover_v2.py` (7,167 lines, `:5`, 7 classes / 94 top-level
defs / 24 banners), which is on `jasper-measure`'s path only, and by accident of
`correction_crossover_v2_wired`.

**The pattern across all fourteen:** the two navigation signals disagree. Files with
many banners (`spatial.py` 19, `round_views.py` 22, `verification.py` 28) tend to be
title-less horizontal rules that mark *nothing*; files with none
(`runtime_contract.py` 5,152 lines, `baseline_profile.py` 4,182,
`commissioning_evidence.py` 3,498, `feature_classifier.py` 2,453) are the largest.
A maintainer gets no table of contents in exactly the files that need one.

---

## §3 The `program_analysis` facade: yes, it is a maintainer trap

`jasper/audio_measurement/program_analysis/__init__.py` — 312 lines,
9 `from .x import (…)` blocks at `:24, :111, :112, :119, :120, :121, :141, :155, :165`,
`__all__` at `:171` with **140 names**.

Counts (`git grep -ln`, `jasper` + `tests`):

| what | count |
|---|---|
| files mentioning `program_analysis` | 98 |
| files importing **the facade** (`from jasper.audio_measurement.program_analysis import …`) | **66** |
| files importing **a submodule** (`…program_analysis.model` / `.dispatch` / …) | **0** |

Facade importers by area: `tests` 44, `jasper/active_speaker/crossover_v2` 17,
`jasper/active_speaker` 3, `jasper/web` 1, `jasper/cli` 1.

**Does the import path hide where the name is defined? Yes, for every name.**
The 140 exported names resolve to: `model.py` 85, `response.py` 18, `check.py` 12,
`dispatch.py` 8, `locate.py` 5, `summary.py` 3, `signals.py` 3, `drift.py` 3,
`verify_integrity.py` 1. Two more (`KIND_SWEEP` at `:23`, `parabolic_peak` at `:22`)
are not in the package at all — they are pass-throughs from
`jasper/audio_measurement/program.py` and `jasper/audio_measurement/alignment.py`, so
importing `KIND_SWEEP` from `program_analysis` actively teaches the wrong location.

**Does go-to-definition land on the facade? Yes.** Every consumer writes
`from jasper.audio_measurement.program_analysis import X`, and the facade line
that binds `X` is a `from .model import (…)` member, so the first jump is
`__init__.py:24`–`:170` — one hop short of the definition, in every case, for all
66 importers. `analyze_program_capture` is at `dispatch.py:95`,
`ProgramAnalysis` at `model.py:905`, `MeasurementPriors` at `model.py:443`,
`CrossoverCandidate` at `model.py:697`, `solve_branch_trims` at `response.py:895`.

**Aggravating factor: 32 of the 140 exported names are `_`-prefixed private.**
`_locate_segments`, `_verify_capture_integrity`, `_driver_response`,
`_build_candidate`, `_estimate_drift`, `_global_offset`, `_solve_gain_plan`,
`_select_alignment_pair`, … Product code reaches through the facade for them:
`jasper/active_speaker/crossover_v2/harmonic_evidence.py:799-808` imports
`_estimate_drift`, `_global_offset`, `_locate_segments`;
`tests/crossover_v2_fixtures.py:78` imports `_verify_capture_integrity`;
`tests/_flat_lin_corpus.py:486` calls `pa._driver_response`. The facade's own
docstring at `:17-18` says "the names below are the surface every importer outside it
uses" — which is true and is the problem: the published surface includes the
internals, so nothing inside the package is actually free to move.

Verdict: **TRUE, maintainer trap.** 66 importers, 0 of which name a submodule; every
go-to-definition lands on a re-export line; 32 private names are load-bearing across
package boundaries. The docstring is otherwise honest and the boundary it claims
("no `jasper.active_speaker` import", pinned by
`tests/test_correction_boundary_ssot.py`) is **TRUE** — I checked
`jasper/audio_measurement/program_analysis/model.py` for real import statements
naming `active_speaker`: zero (the six matches there are comments/docstrings only).

---

## §4 One-screen map of `jasper/active_speaker/`

174 `.py` files, 140,985 lines. Produced by
`work/maintainer-legibility/asmap.py` (basename/prefix rules; group membership
dumped to `asmap.json`).

| group | files | lines | largest members |
|---|---|---|---|
| **crossover_v2 engine** (`crossover_v2/`) | 63 | **44,204** | `evidence_packet.py` 3,144 · `feature_classifier.py` 2,453 · `spatial.py` 2,368 · `round_views.py` 2,090 |
| **commissioning** | 18 | 22,725 | `commissioning_evidence.py` 3,498 · `web_commissioning.py` 2,333 · `commissioning_run.py` 2,128 · `commissioning_receipt.py` 1,824 |
| **measurement doors + walks** | 27 | 17,484 | `linearization_fit.py` 2,027 · `measurement.py` 1,763 · `seat_level_ramp.py` 1,752 · `delta_probe.py` 1,252 |
| **safety / limits** | 9 | 12,503 | `runtime_contract.py` 5,152 · `driver_safety.py` 2,682 · `path_safety.py` 1,073 · `graph_safety.py` 1,013 |
| **crossover v1 + envelope** (top-level, *outside* `crossover_v2/`) | 8 | 11,788 | `crossover_v2_flow.py` 4,636 · `crossover_envelope_v2.py` 3,497 · `crossover_level_run.py` 1,037 |
| **other / unclassifiable** | 31 | 11,752 | `setup_status.py` 1,215 · `session_volume_plan.py` 1,068 · `measured_candidate.py` 840 · `flat_spec.py` 813 |
| **storage / records** | 8 | 8,648 | `baseline_profile.py` 4,182 · `design_draft.py` 1,552 · `bundles.py` 1,080 · `profile.py` 906 |
| **dsp / camilla emit + staging** | 4 | 8,011 | `camilla_yaml.py` 4,028 · `staging.py` 2,314 · `startup_load.py` 1,425 |
| **bench / tools** | 4 | 2,147 | `bench/loop.py` 923 |
| **web hosts / clients** | 2 | 1,723 | `web_measurement.py` 1,382 · `wizard_client.py` 341 |

Two structural facts the map exposes:

1. **The v2 engine is split across a package boundary by filename only.**
   `crossover_v2_flow.py` (4,636) and `crossover_envelope_v2.py` (3,497) — 8,133 lines
   of v2 engine — sit at `jasper/active_speaker/` top level, *outside*
   `jasper/active_speaker/crossover_v2/`. This is deliberate and pinned:
   `jasper/active_speaker/crossover_v2/__init__.py:7-11` says the package "imports
   nothing from `crossover_v2_flow`; the flow imports these modules, and the reverse
   import is what the migration exists to prevent". A maintainer reading a file
   listing has no way to know that `crossover_v2_flow.py` is *above* `crossover_v2/`
   in the layering rather than beside it.
2. **31 files / 11,752 lines are unclassifiable** by name. `setup_status.py`,
   `flat_spec.py`, `flat_spec_views.py`, `measured_candidate.py`,
   `measured_crossover_candidate.py`, `program_admission.py`, `repeat_admission.py`,
   `repeat_floor.py`, `graph_evidence.py`, `attempts_loop.py`, `volume_latch.py`,
   `runtime_convergence.py`, `tuning_handoff.py`, `revalidation.py`, `tone_plan.py`,
   `branch_target.py`, `level_trim.py`… — 8% of the package's lines have no group a
   newcomer could guess from the filename.

### Five files to read first

1. `docs/tuning-operator-runbook.md:1` (1,375 lines) — the only document with an
   explicit owner table (`:11-18`) routing "what may I try", "session boundary",
   "where is this going", "why is it like this" to four other owners. It is the map.
2. `jasper/cli/_refusal.py:1` (109 lines) — the contract every one of the ten
   binaries uses: three failure kinds, three exit codes (`:32-39`), a
   `STATUS_BY_CODE` table (`:42`) so "callers name the CODE and this picks the word",
   and an explicit named exception list `OWN_EXIT_VOCABULARY = {"jasper.cli.declare_geometry"}`
   at `:28-31`. This is the single best-designed thing in the toolbox and it explains
   the whole CLI surface in one screen.
3. `jasper/active_speaker/crossover_v2/contracts.py:1` (1,376 lines) — the only
   module re-exported by `crossover_v2/__init__.py:17-33`; the 16 names there
   (`InterventionProposal`, `RoundReceipt`, `VerificationResult`, `AdoptionDecision`,
   `CaptureValidity`, `PlanRefusal`…) are the engine's whole type vocabulary.
4. `jasper/active_speaker/crossover_v2/door.py:43-62` (324 lines) — the measurement
   door: three refusal slugs and `OpenMeasurementDoor`. Small, named, and the thing
   that stands between a CLI and audio.
5. `jasper/audio_measurement/program_analysis/model.py:1` (979 lines) — 85 of the
   140 truth-layer names live here; reading it once removes the facade's fog.

(Honourable mention: `jasper/active_speaker/crossover_v2/journey.py`, 471 lines —
the phase vocabulary that `crossover_v2_flow.py:232` says it borrows.)

### Five files that would most mislead

1. **`jasper/active_speaker/__init__.py:20`** — a `_LAZY_ATTRS` dict of **207
   entries** mapping name → submodule *as a string*. `from jasper.active_speaker
   import ActiveSpeakerPreset` cannot be followed by any tool; go-to-definition lands
   on `"profile"` (a string literal) or on `__getattr__` at the tail. The top 12
   targets carry 15, 15, 12, 12, 12, 11, 10, 10, 10, 10, 9, 9 names each. The
   docstring at `:5-9` also asserts "Most of this package does not emit audio — the
   exception is `web_commissioning`", which a maintainer will read as *one* audio
   emitter in a 174-file package.
2. **`jasper/audio_measurement/program_analysis/__init__.py:171`** — see §3. 140
   re-exports, 66 importers, 0 submodule importers, 32 private names published.
3. **`jasper/active_speaker/crossover_v2_flow.py:1`** — the name says it belongs to
   `crossover_v2/`; the layering says the opposite
   (`jasper/active_speaker/crossover_v2/__init__.py:7-11`). 4,636 lines, 8 banners
   naming concerns, three of them ("phase vocabulary", `:232`; "the walk", `:264`;
   "cloud group bands", `:752`) explicitly owned by other modules.
4. **`jasper/cli/round_views/` vs `jasper/active_speaker/crossover_v2/round_views.py`**
   — two modules named `round_views`, and the CLI one imports the engine one at
   `jasper/cli/round_views/_common.py:21`. Compounding it,
   `jasper/cli/crossover_prescriber.py:31` does `from .round_views import default_out`
   (the CLI package) while `:202` comments about
   `jasper.cli.round_views.ARTIFACT_BY_VIEW`. In a stack trace or an editor tab
   strip, the two are indistinguishable.
5. **`jasper/active_speaker/web_commissioning.py:5`** — named `web_*`, docstring says
   "Shared **web/operator** orchestration", and it is directly imported by
   `jasper/cli/null_door.py` (a CLI binary), `jasper/mux.py` and
   `jasper/fanin/ring_health.py`. Sixteen importers spanning four subsystems. A
   maintainer who reads it as web-only will not expect `jasper-null` to load it.

Runner-up: **`jasper/active_speaker/measurement.py`** (1,764) is one of *ten*
`measurement`-named modules in `jasper/` — alongside `web_measurement.py`,
`measurement_programs.py`, `measurement_archive.py`, `measurement_document.py`,
`measurement_emit.py`, `jasper/measurement_window.py`,
`jasper/audio_measurement/`, `jasper/control/measurement_hold.py`,
`jasper/web/correction_measurements.py`. Its docstring ("Durable
active-speaker driver-check and measurement **evidence**", `:5`) makes it a *record
store*, not a measurer.

---

## §5 Glossary of drift — one thing, several words

Each row is one concept the tree spells more than one way, with a `file:line` per
spelling.

**A. The directory holding one measurement's artifacts** — `round` / `bundle` /
`session`, and all three appear in one API.
- `round_dir` — `jasper/active_speaker/crossover_v2/close_reference.py:612`
- `bundle_dir` — `jasper/active_speaker/baseline_profile.py:3316`
- `session_dir` — `jasper/active_speaker/crossover_v2/evidence_packet.py:491`
  (`def round_artifact_dir(session_dir: Path)` — both words in one signature)
- **The clearest single line:** `jasper/active_speaker/bundles.py:430` —
  `bundle_id=session_id`, two names for one value in one keyword argument, with
  `"session_id": session_id` written into the same document eight lines later
  (`:436`).
- ID spellings: `round_id` (`jasper/active_speaker/crossover_v2/contracts.py:1086`),
  `bundle_id` (`jasper/active_speaker/commissioning_admission.py:334`),
  `session_id` (93 files).

**B. One recording of one stimulus at one place** — `take` / `capture` / `occurrence`
/ `walk` / `pose`.
- `take` — `jasper/active_speaker/crossover_v2/evidence_packet.py:846`
  ("One banked **take**"), `jasper/active_speaker/crossover_v2/delay_landscape.py:439`
  ("the **take** it was read off"),
  `jasper/active_speaker/capture_provenance.py:72` ("One-**capture** handoff… `take`
  CONSUMES" — both words, one docstring)
- `capture` index — `jasper/active_speaker/angle_capture.py:58`
  (`announced_capture_indexes`)
- `occurrence` index — `jasper/audio_measurement/program_analysis/drift.py:44`
  (`_sweep_occurrence_index`)

**C. The thing being proposed** — `candidate` / `proposal` / `prescription` /
`profile` / `draft` / `preset` / `plan`. Seven nouns, all live:
- `BankedCandidate` — `jasper/active_speaker/candidate_bank.py:54`
- `CrossoverAlignmentProposal` — `jasper/active_speaker/crossover_alignment.py:98`
- `InterventionProposal` — `jasper/active_speaker/crossover_v2/contracts.py:496`
- `AlignmentPrescription` — `jasper/active_speaker/crossover_v2/alignment_prescription.py:132`
- `BlendPrescription` — `jasper/active_speaker/crossover_v2/blend_prescription.py:373`
- `ActiveSpeakerPreset` — `jasper/active_speaker/profile.py:632`
- `ActiveSpeakerDesignDraftError` — `jasper/active_speaker/design_draft.py:123`
- `DriverProtectionProfile` — `jasper/active_speaker/driver_protection.py:60`
- `RegionEvidencePlan` — `jasper/active_speaker/commissioning_evidence.py:640`,
  `RequiredTargetPlan` — `jasper/active_speaker/commissioning_receipt.py:300`
- Two near-identical module names: `measured_candidate.py` (840) and
  `measured_crossover_candidate.py` (683).

**D. "The mechanism that lets sound out under control"** — six spellings:
- **door** — `MeasurementDoorRefused` `jasper/active_speaker/crossover_v2/door.py:52`;
  `OwnerVolumeDoor` `jasper/active_speaker/crossover_v2/volume_claim.py:92`;
  `VolumeDoor` (Protocol) + `FaderVolumeDoor` `jasper/active_speaker/session_volume_plan.py:568,605`;
  `_DoorUnreachable` `jasper/cli/basic_profile.py:98`
- **gate** — `build_stage5_ramp_gate` `jasper/active_speaker/commission_ramp.py:317`;
  `_verify_gate` `jasper/active_speaker/crossover_envelope_v2.py:457`
- **lease** — `IsolationLease` `jasper/cli/aec_commission.py:155`;
  `CrossoverLevelLease` (named in `jasper/active_speaker/volume_latch.py:8`)
- **latch** — `jasper/active_speaker/volume_latch.py:5`
- **hold** — `release_staged_startup_hold` `jasper/active_speaker/startup_hold.py:95`;
  `jasper/control/measurement_hold.py`
- **admission** — `ActiveCommissioningAdmissionError`
  `jasper/active_speaker/commissioning_admission.py:118`;
  `open_bundle_admission_authority` `jasper/active_speaker/bundles.py:474`
- **screen** — `CheckScreens` / `MeasureScreen`
  `jasper/active_speaker/crossover_v2/capture_dispatch.py:128,194`;
  `_screen_policy` `jasper/active_speaker/angle_capture.py:499`

**E. Campaign shorthand a new maintainer cannot resolve.** 120 comment/docstring hits
across 18 files in `jasper/active_speaker` + `jasper/audio_measurement` + `jasper/cli`
match `Wave N` / `PR-XX` / `WO-N` / `panel lens` / `W5b` / `G1`:
- `jasper/active_speaker/crossover_envelope_v2.py:5` — "(Wave 5a; two-stage since PR-T3)"
- `jasper/active_speaker/crossover_envelope_v2.py:1506` — "G1's ripple reservation (#2087)"
- `jasper/active_speaker/crossover_envelope_v2.py:2173` — "banked findings (WO-1's read half; panel lens C, CC1)"
- `jasper/active_speaker/baseline_profile.py:1417` — "every profile written before PR-L5"
- `jasper/active_speaker/linearization_fit.py:5` — "The Layer-1a driver-linearization fit engine (#1668 PR-C)"
- `jasper/active_speaker/attempts_loop.py:139` — "WO-7's planning number"
- `jasper/active_speaker/camilla_yaml.py:2804` — "(crossover measurement session, W2)"

These are the highest-density legibility tax in the tree: each is a pointer into a
campaign record the maintainer does not have, and AGENTS.md's Defaults already
forbid the class ("no history, no dates/PR numbers").

---

## §6 Tests as documentation: which binaries have a refusing end-to-end `main()`?

| binary | dedicated test | `main()` exercised end to end? | refusing input asserted? | evidence |
|---|---|---|---|---|
| jasper-declare-geometry | `tests/test_cli_declare_geometry.py` (135L) | **yes** | **yes** | `:65` `main(["show", …])`; `:129` `assert code == declare_geometry.EXIT_REFUSED`; `:134-135` `main(["show", … absent.json])` → `EXIT_NOT_FOUND` |
| jasper-seat-level | `tests/test_cli_seat_level.py` (1,163L) | **yes** | **yes** | `:1010` `seat_level.main([...])`, `:1013` `assert code == 1`, `:1014` `assert "refused (spl_ceiling_exceeded)" in err` |
| jasper-measure | `tests/test_cli_measure.py` (948L) | **yes** | **yes** | 11 `main([` sites, 9 refusing-exit assertions |
| jasper-null | `tests/test_null_confirm_door.py` (881L) | **yes** | **yes** | 3 `main([` sites, 3 refusing-exit assertions; 55 `refus*` mentions |
| jasper-round | `tests/test_cli_round.py` (403L) | **yes** (via `_run` at `:117` → `cli.main([*argv, "--json"], opener=…)`) | **yes** | `:240, :253, :280, :315` `assert code == cli.EXIT_REFUSED` |
| jasper-basic-profile | `tests/test_cli_basic_profile.py` (285L) | **yes** (via `_run` at `:129` → `cli.main(…)`) | **partial** — 11 runs, only 2 refusing-exit assertions |
| jasper-crossover-prescriber | `tests/test_crossover_v2_prescription_spool.py` + `_prescriber_status.py` + `_driver_prescription.py` | **yes** | **yes** | `tests/test_crossover_v2_prescription_spool.py:1300` `cli.main(["stage", …])`, `:1305` `assert code == cli.EXIT_REFUSED` |
| jasper-round-views | `tests/test_active_speaker_crossover_v2_round_views.py` (2,822L) | **yes** | **yes** | 31 `main([` sites, 11 refusing-exit assertions |
| **jasper-angle-capture** | `tests/test_angle_capture_trigger.py` (1,226L) | **only once, in a subprocess** | **helper level** | The four refusal assertions call **private subcommand handlers**, not `main`: `:302` `cli._cmd_plan(...) == cli.EXIT_REFUSED`, `:806` and `:943` `cli._cmd_stage(args) == cli.EXIT_REFUSED`, `:1045` `cli._cmd_plan(args)`. The one `main([` (`:1088`) is inside a `_STAGE_IN_A_REAL_PROCESS` script string run by `subprocess.run` at `:1113-1120`, and it asserts a *successful* stage's stderr event, not a refusal. |
| **jasper-audition** | **none** | **NO** | **NO** | `git grep -n "cli.audition\|audition.main"` in `tests/` returns only `tests/test_canonical_target_registration.py:99` `"jasper/cli/audition.py": "main"` — a registration-table entry, not a call. The 800-line `tests/test_active_speaker_audition.py` tests the engine `jasper/active_speaker/audition.py` only (`:23`, `:163`, `:224`, `:253`…). `jasper/cli/audition.py` (292 lines) has **zero** direct test coverage of `main()`. |

**Summary: 8 of 10 have a refusing end-to-end `main()` pin. `jasper-angle-capture`
tests its refusals one layer below `main()` (private `_cmd_*` handlers, so argparse
wiring and the exit-code mapping are unpinned on the refusal paths). `jasper-audition`
has no `main()` test at all.**

Positive counter-note: every one of the ten imports `jasper/cli/_refusal.py`
(`git grep -ln "_refusal" jasper/cli` → 25 files, all ten binaries included), and the
one deviation is *declared in code* at `jasper/cli/_refusal.py:28-31`
(`OWN_EXIT_VOCABULARY = frozenset({"jasper.cli.declare_geometry"})`, matching
`jasper/cli/declare_geometry.py:31` `EXIT_NOT_FOUND = 2`). A maintainer can learn the
entire exit-code surface of the toolbox from one 109-line file — the single strongest
legibility asset found in this lane.

---

## Could not determine

- Whether the ~200-400 module import closures cost real startup time on the Pi
  (would need `python -X importtime` on hardware; this lane is read-only and
  laptop-side). The graph shape is measured; the runtime cost is not.
