# E — Engine + runtime substrate (bottom-up sizing)

HEAD `c0325038`, read-only. Counts reproducible with the scripts saved beside
this file: `prose2.py` (AST+tokenize prose/code split), `defs.py` (top-level
def sizes), `cut3.py` (top-level-only import closure, with package `__init__`
execution modelled — the thing `reach.py` did not model).

**Two totals:**

| half | files | lines today | clean, first principles | delta |
|---|---:|---:|---:|---:|
| 1 — product runtime substrate | 17 py + 2 preset json | **32,521** | **~9,300** | −23,200 (71%) |
| 2 — session engine | 101 py | **100,552** | **~23,600** | −77,000 (77%) |
| combined | | **133,073** | **~32,900** | −100,200 (75%) |

Composition today: half 1 is 22,456 code / 7,326 prose (23%) / 2,607 blank.
Half 2 is 49,703 code / 41,220 prose (41%) / 9,629 blank.

---

# HALF 1 — what the product runtime needs from the tuning scope

## 1.1 Per-component sizing

| component | files (lines) | today | clean | top reason for the gap |
|---|---|---:|---:|---|
| Speaker profile record | `profile.py` 893, `presets/*.json` 132 | 1,025 | **550** | closest to right-sized in the whole scope (11% prose, 44 consumers). Only fat: hand-rolled per-field validation that a shared record kit would own. |
| CamillaDSP emit | `camilla_yaml.py` 4,551, `jasper/camilla_emit.py` 410 | 4,961 | **1,800** | **seven emitters, one concern.** Latency prelude verbatim ×5, `devices:` block ×4, write-tail ×5, hearing clamp ×6. |
| Runtime contract + graph view | `runtime_contract.py` 5,529, `graph_safety.py` 1,201, `environment.py` 780 | 7,510 | **2,000** | one 779-line function with 47 hand-written check blocks; the classifier, the selector and topology-contract classification share a file with no seam. |
| Path safety | `path_safety.py` 1,092 | 1,092 | **350** | one 218-line evidence builder; the same `volume_limit_ok` dict bool re-read at 474/556/687/696/728. |
| Driver safety + protection | `driver_safety.py` 3,258, `driver_protection.py` 1,061 | 4,319 | **1,100** | 327 lines of **LLM prompt copy** inside a schema validator (`driver_safety.py:1552-1885`); `driver_protection.py` is 48% prose incl. a 99-line comment run on a 25-line function. |
| Startup / staging / ramp | `staging.py` 2,498, `startup_load.py` 2,352, `commission_ramp.py` 1,366, `commission_wiring.py` 236, `startup_hold.py` 193 | 6,645 | **1,900** | **two near-identical load transactions** (`load_protected_startup_config` 230L + `load_driver_commissioning_config` 472L) each with its own preflight builder (260L / 281L), its own rollback and its own state file. |
| Applied-profile record + apply path | `baseline_profile.py` 4,192, `commissioning_apply.py` 1,460, `commissioning_service.py` 1,317 | 6,969 | **1,600** (+600 moves to half 2) | a 986-line candidate **builder** — engine work — living inside the runtime's applied-profile record; a *third* apply/rollback transaction. |
| **total** | | **32,521** | **~9,300** | |

**First-principles shape of the clean 9,300** (this is what the runtime genuinely
owns, and nothing here is optional):

- a profile/preset record + JSON I/O + role helpers — 550
- one graph-spec → Camilla-YAML writer, filter/mixer builders, 7 graph-kind spec
  builders, emit-time re-proof asserts, one hearing clamp — 1,800
- a `GraphView` parser + ~50 predicates + a *declared table* of ~50 checks + the
  graph-class names + one selector `(topology contract × graph class × applied
  state) → decision` + statefile read/write — 2,000
- a path-proof requirement list, evidence writer/reader, evaluator — 350
- a driver-profile schema spec + generic validator + normalisers + protection
  policy constants (prompt copy moves to a text resource) — 1,100
- stage(compile→bind→write+lock) / load(preflight→apply-with-hold→verify) /
  rollback, **once**, plus per-kind glue, plus the audible ramp state machine — 1,900
- an applied-profile record (read/write/fingerprint) + **one** apply transaction
  reused by the three call sites + YAML recompose from the persisted snapshot — 1,600

## 1.2 Why `runtime_contract` pulls in `baseline_profile → linearization_fit →
## crossover_v2.intervention → program_analysis` — traced

It doesn't, at module level. The L0 finding in BRIEF2 counted **function-local
(lazy) imports as graph edges**. Traced symbol by symbol:

| edge | site | symbol actually used | what it is |
|---|---|---|---|
| `runtime_contract` → `baseline_profile` | `runtime_contract.py:4692`, `:4997` (both **inside** function bodies) | `baseline_profile_state_path` | `baseline_profile.py:218-219` — a **2-line** function: `return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)` |
| `runtime_contract` → `staging` | `runtime_contract.py:4693`, `:4998` (lazy) | `staged_metadata_path` | `staging.py:139` — 6 lines, same shape |
| `baseline_profile` → `crossover_v2.intervention` | `baseline_profile.py:1972` (lazy, with a comment saying why) | `LEVEL_MATCH_AXIS` | `intervention.py:560` — `LEVEL_MATCH_AXIS = "design_axis_0deg"`. **One string.** |
| `baseline_profile` → `linearization_fit` | `baseline_profile.py:2628` (lazy) | `linearization_filters_by_role` | one reducer over a persisted filter mapping |
| `linearization_fit` → `program_analysis` | `linearization_fit.py:127` (**top level**) | `DriverResponse` | a dataclass used as a type |
| `crossover_v2.intervention` → `program_analysis` | `intervention.py:45` (top level) | `solve_branch_trims`, `ripple_at_trim`, … | real DSP math — legitimate, and squarely engine-side |

So what the *runtime* needs from those four modules is: **two default file paths
and one string constant.** It then reads those JSON files itself
(`runtime_contract.py:4712-4719` builds an `authority` dict of four `Path`s and
hands it to `classify_bass_extension_graph`).

**The actual leak is `jasper/active_speaker/__init__.py`.** 480 lines, 209
`__all__` entries, and it eagerly imports 26 submodules — including
`baseline_profile` (:232), `driver_safety` (:154), `design_draft` (:142),
`commissioning_capture` (:252), `measurement` (:221). Python executes a package
`__init__` before any submodule, so **every** `import
jasper.active_speaker.anything` anywhere in the product runtime loads the whole
engine. Measured with `cut3.py` (top-level imports only, package `__init__`
execution modelled):

| entry point | tuning-scope lines at import, today | with `__init__.py` trimmed |
|---|---:|---:|
| `jasper.voice_daemon` | **47,777** | **797** (`volume_latch` 317 + a stub `__init__`) |
| `jasper.mux` | 0 | 0 |
| `jasper.active_speaker.runtime_contract` (the boot classifier) | 47,460 | **18,979** |
| `jasper.cli.active_speaker` (what the boot guard runs) | 48,048 | 37,833 |
| `jasper.control.state_aggregate` | 48,806 | 39,701 |
| `jasper.cli.doctor.audio` | 52,670 | 24,806 |

Recon 01 §5 already established that **146 of the 209 `__all__` names are never
imported through the façade** and only 3 production call sites use it at all.

### What it would take to cut the runtime loose — four mechanical moves, ~450 lines touched

1. **Trim `jasper/active_speaker/__init__.py`** to the two names the CLIs want
   (`ActiveSpeakerConfigError`, `ActiveSpeakerPreset`). −330 lines. This alone
   takes `jasper-voice`'s tuning-scope import surface from 47,777 → 797.
2. **A leaf `state_paths.py`** (~30 lines) holding the four default state-file
   paths (`baseline_profile_state_path`, `staged_metadata_path`,
   `commission_load_state_path`, `startup_load_state_path`). Severs
   `runtime_contract → {baseline_profile, staging}` outright; 12 other call
   sites (`cli/active_speaker.py:18`, `web/correction_setup.py:2218`,
   `multiroom/follower_config.py:518`, `cli/doctor/audio.py:1238`, …) follow.
3. **Move `LEVEL_MATCH_AXIS`** into the shared vocabulary (`profile.py` or a leaf
   `axes.py`), and give `linearization_fit` a local `DriverResponse` Protocol
   instead of importing `audio_measurement.program_analysis`. That is the last
   engine edge reachable from a runtime module.
4. **Split `jasper/cli/active_speaker.py`.** `deploy/bin/jasper-camilla-crossover-guard:91`
   runs `jasper-active-speaker runtime-safe-graph` **at boot**, but that file's
   12 subcommands share one top-level import block that pulls `staging`,
   `startup_load`, `commission_ramp`, `measurement` and `baseline_profile`
   (`cli/active_speaker.py:18-62`). `_cmd_runtime_safe_graph` (`:329`) needs
   only `safe_graph_for_current_topology`, `apply_safe_graph_decision_to_statefile`,
   `compose_selected_flat_graph`, the environment probe and path-safety
   evidence. A dedicated boot entry point lands at ~19k today, ~7k after the
   half-1 cleanup.

Then pin it: `crossover_v2` already ships
`test_the_package_import_graph_stays_acyclic`; add the sibling that asserts
`jasper.voice_daemon`'s (and `jasper.mux`'s, and the boot CLI's) import closure
contains no `crossover_v2`, no `linearization_*`, no `commissioning_*`, no
`audio_measurement.program_analysis`. Without that test the boundary regresses
the next time someone adds a convenience re-export.

**Owner's line, stated plainly:** the smart speaker does not need the tuning
engine to boot. It needs a profile record, a YAML emitter with the clamps, a
graph classifier, a loader, and an apply transaction — the 9,300 lines above.
Everything else it currently imports is an accident of one façade file.

## 1.3 Half-1 gap breakdown (23,221 lines removed)

| category | lines | evidence |
|---|---:|---|
| verbosity in legitimate code | 10,600 | 26 functions ≥200 lines (8.6k lines): `baseline_profile:2035` 986, `runtime_contract:2776` 779, `startup_load:1737` 472, `commission_ramp` 447, `baseline_profile:798` 420, `runtime_contract:4950` 408, `staging:2078` 380. Each is a check-list written as prose-in-code. |
| prose over the AGENTS.md bar | 5,900 | 7,326 prose lines today; a 9,300-line body at 15% carries ~1,400. Worst ratios: `startup_hold` 58%, `volume_latch` 53%, `driver_protection` 48%, `graph_safety` 34%, `camilla_yaml` 32%. |
| duplication | 3,800 | 7 emitters (−1,600); 2 load transactions + 2 preflight builders (−1,100); 3 apply transactions (−700); 7 copies of the hearing clamp (−12); hand-rolled record validation (−400). |
| over-abstraction / wrong altitude | 2,500 | the 209-name façade (−330); 327 lines of prompt copy inside a validator (−250 as data); 47 inline check blocks → one declared table (−500); 4 one-function modules (`passive_profile`, `revalidation`, `restore_wait`, `tuning_handoff`); two backend view models over one journey (`setup_status:read_active_speaker_setup_status` 513L vs `commissioning_coordinator.build_commissioning_view` 431L). |
| dead / test-only | 450 | `staging.prepare_summed_commissioning_config:2460` (39L, `__init__` re-export + 1 test), `camilla_yaml.ACTIVE_STARTUP_CONFIG_NAME:108`, plus the half of recon 01 §4's 1,015 dead lines that sits in these files. |
| speculative / parked | ~0 | half 1 has essentially none — it is all live. |

## 1.4 Three biggest single deltas, half 1

1. **`jasper/active_speaker/camilla_yaml.py:{2348, 2512, 2957, 3603, 3940, 4228, 4450}`**
   — seven `emit_active_speaker_*_config` functions, 1,463 lines between them,
   repeating one concern. The **hearing clamp is written six times**
   (`if volume_limit_db > 0:` at 2403, 2602, 3028, 3735, 4071, 4319) plus a
   seventh implementation at `jasper/camilla_config_contract.py:431
   ensure_volume_limit_db`, whose own docstring says it "Mirrors the guard in
   `jasper.active_speaker.camilla_yaml`". A non-negotiable with seven owners.
   **−1,600**, adversarial-review tier.
2. **`jasper/active_speaker/runtime_contract.py:2776 _active_graph_evidence`** —
   779 lines, 47 `issues.append(_issue(severity, code, message))` blocks, each
   ~12 lines of inline predicate + literal. As a table of 47 check descriptors
   plus a 60-line evaluator: **−500**, and the safety story becomes readable in
   one screen instead of thirteen.
3. **`jasper/active_speaker/baseline_profile.py:2035 build_baseline_profile_candidate`**
   — 986 lines, the largest function in the tuning scope, and it is *engine*
   work (compose a measured candidate from banked evidence) sitting inside the
   module the product runtime reads its applied profile from. Split: the record
   + apply transaction stay in the runtime (~1,600 total), the builder moves to
   half 2 (~600 there). **−400 net, and it is the single move that makes the
   runtime/engine line drawable at all.**

---

# HALF 2 — the session engine the tools drive

Scope: `crossover_v2/` (63 modules, 54,553) + `crossover_v2_flow.py` (7,839) +
the 11 `commissioning_*` evidence modules (15,316) + the candidate / round /
linearization / branch / `crossover_*` top-level modules (22,844). **100,552
lines, 41% prose, 201 dataclasses, 58 exception classes, 201 hand-written
`to_dict`/`from_mapping`/`_core`/`__post_init__` methods totalling 5,717 lines,
30 `SCHEMA_VERSION` constants.**

## 2.1 Per-component sizing

| # | component (what it must do) | modules today | today | clean | top reason for the gap |
|---|---|---|---:|---:|---|
| 1 | **Session / walk state machine** — arm, authorize, phase dispatch, capture ingest, snapshot/hydrate | `crossover_v2_flow` 7839, `session` 733, `session_seams` 274, `journey` 606, `measurement_phase` 111, `composition` 255, `session_graph` 486, `volume_claim` 280, `playback_transaction` 220, `program_transaction` 388, `door` 387, `tuning_scope` 158 | 11,737 | **1,700** | one god class (154 methods, a 796-line `__init__` with 46 params and 111 attrs) **and** a second session object (`TuningSession`) constructed 150 lines away in the same web function. No state record, so the ctor *is* the record. |
| 2 | **Round runner** — measure → bank → grade → keep/restore | `coordinator` 1410, `round_evidence` 1001, `round_captures` 383, `round_inputs` 200, `round_bank` 277, `candidate_bank` 268, `attempt_grading` 88 | 3,627 | **850** | 57% prose in `coordinator`; `attempt_grading` is 88 lines, 82% prose, **three constants and no code**. |
| 3 | **Record contract with fingerprints** | `contracts` 1617, `record_store` 330, `record_index` 200, `priors` 471, `accountability` 493 | 3,111 | **1,030** | the `_core()`/`to_dict()`/`json_fingerprint` triple is copied 3× *inside `contracts.py` alone* and never factored; 30 loose `SCHEMA_VERSION`s with no envelope; zero `TypedDict`/schema validation anywhere. |
| 4 | **The four prescription doors (one kit)** | `driver_` 2508, `blend_` 1688, `topology_` 964, `alignment_` 751, `prescription_spool` 953, `handoff_doors` 45, `proposal` 321, `candidates` 237, `planning` 996 | 8,463 | **1,900** | **four copies of one contract.** Each restates kind, schema version, field allow-list, refusal frozenset, refusal class, reader, `from_mapping`, `response_format`, `_finite_number`. The copies have **already diverged into a live bug** (§2.4). |
| 5 | **Evidence packet the LLM reads** | `evidence_packet` 3753, `round_views` 1810, `feature_optics` 143, `diagnostics` 700, `operator_notes` 436 | 6,842 | **1,350** | a projection of banked records into JSON, written as 3,753 lines of bespoke assembly; `evidence_packet:156` imports the *writing* doors' schema emitters (L3→L4 inversion). |
| 6 | **Spatial cloud close** | `spatial` 3148, `capture_plan` 3050, `position_cycle` 587 | 6,785 | **1,000** | `spatial` 59% prose, `capture_plan` 54% incl. a 50-line comment run on one integer (`capture_plan.py:132-181`). |
| 7 | **Verification / grading / classification** | `verification` 2186, `feature_classifier` 2599, `harmonic_evidence` 1264, `gate_sweep` 1140, `close_reference` 673, `admission` 617, `feature_classification` 602, `delay_landscape` 603, `ring_projection` 385 | 10,069 | **2,700** | each organ carries its own refusal class, its own field validators and its own verdict vocabulary. |
| 8 | **Durable state** | `durable_state` 2198 | 2,198 | **350** | it serialises a class that has no record — 21 of the flow's 37 externally-used public members exist **only** so this module can read them; its three seam params are `conductor: Any`, untyped duck-typing of a 6,248-line class. |
| 9 | **Refusal registry + household copy** | `refusal_copy` 1578 | 1,578 | **430** | the registry (44 codes) is right; around it sit **58 exception classes**, 6 module-local `_refuse()` helpers, 17 loose `REFUSE_*` constants and 6 `*_REFUSAL_REASONS` frozensets, 5 of which have zero production readers. |
| 10 | **Linearization solver + DSP math** | `linearization_fit` 3567, `intervention` 1718, `linearization_envelope` 860, `branch_chain` 859, `branch_peak` 692, `blend_correction` 763, `commanded` 481, `plan_assembly` 425, `forward_model` 405, `branch_target` 312, `frequency_view` 234 | 10,316 | **3,650** | **the most legitimate complexity in the whole scope** — this is real measurement science. The only gap is prose (61% in `linearization_fit`, 69% in `branch_target`, 60% in `branch_chain`). |
| 11 | **Delta probe** | `delta_probe` 2494, `delta_probe_run` 489 | 2,983 | **600** | 64% prose. "Play A, play B, measure both, report the delta with a confidence" is a 400-line idea. |
| 12 | **Capture plan / dispatch / spec** | `sweep_spec` 1576, `capture_dispatch` 936, `programs` 501, `measure_spec` 443, `capture_source` 196, `fc_sweep` 138 | 3,790 | **950** | `capture_dispatch` 60% prose, `capture_source` 70% (being deleted by #3724), `fc_sweep`'s own docstring says its filename is a lie. |
| 13 | **The `commissioning_*` evidence machine** | `_evidence` 3501, `_run` 2141, `_receipt` 1831, `_capture` 1520, `_evidence_store` 1433, `_admission` 1238, `_runtime` 1145, `_isolated_producer` 925, `_verification` 845, `_host` 405, `_lifecycle` 332 | 15,316 | **3,150** | 24 frozen dataclasses with an identical hand-rolled validate→canonicalise→fingerprint→serialise quartet: 55% of `_evidence` and 67% of `_receipt` (3,138 of 5,332 lines) is that boilerplate. Plus 418 orphaned lines in `_runtime` (recon 01 §4). |
| 14 | **Candidate + declared-crossover modules** | `crossover_envelope_v2` 4417, `design_draft` 1570, `driver_acoustics` 1207, `crossover_level_run` 1038, `measured_crossover_candidate` 892, `crossover_preview` 879, `measured_candidate` 840, `crossover_contract` 640, `driver_base_trim` 463, `crossover_declaration` 448, `crossover_alignment` 422, `crossover_eligibility` 371, `driver_pad` 209, `level_trim` 67, `crossover_envelope` 52 | 13,515 | **3,900** | **two candidate records** (`measured_candidate` / `measured_crossover_candidate`, 1,732 lines) for one concept; `crossover_level_run` (1,038) + `crossover_eligibility` (371) have exactly **one** consumer each — the v1 backend (§2.5). |
| 15 | `crossover_v2/__init__` | 222 (81% prose) | 222 | **40** | a 178-line module docstring. |
| | **total** | | **100,552** | **~23,600** | |

## 2.2 First-principles shape of the clean 23,600

A session/walk record + phase dispatch (1,700). A round runner over a bank
(850). One `JsonRecord` kit + `fields.py` + ~15 domain records + one store
(1,030). One `PrescriptionDoor` descriptor + `read_prescription(door, raw)` +
four domain rule sets + four response schemas **as data** (1,900). One packet
projector table (1,350). Cloud plan + close (1,000). Verification, admission,
classification on one refusal primitive (2,700). Persist = `record.to_dict()`
(350). One `Refusal`/`Refused`/`refuse()` + a 44-entry registry carrying copy
(430). The DSP solver, essentially intact minus prose (3,650). One probe (600).
Sweep/measure spec + dispatch (950). One commissioning evidence lane on the
shared record kit (3,150). One candidate record + the declared-crossover
compile path (3,900).

## 2.3 Half-2 gap breakdown (76,950 lines removed)

| category | lines | share | evidence |
|---|---:|---:|---|
| prose over the AGENTS.md bar | 37,700 | 49% | 41,220 prose today (41%); a 23,600-line body at 15% carries ~3,500. Reproducible floor: recon 03 counted **9,898 lines in paragraphs citing an issue/ADR/date/ruling or narrating what the code used to be**. 606 `#NNNN`, 45 ADR refs, 232 dates in `crossover_v2/` alone. |
| whitespace, proportional | 7,200 | 9% | 9,629 blank lines today. |
| duplication | 10,200 | 13% | four doors → one kit (−2,300); **201 hand-written serializer/`__post_init__` methods, 5,717 lines** → one kit (−4,200); 58 exception classes → 1 (−900); the two evidence machines' record models (−2,500); the flow's 21 durable-state-only getters (−300). |
| over-abstraction / wrong altitude | 9,000 | 12% | the 6,248-line god class → record + organs; v1 lane still routed (§2.5, ~6,000); one-function modules (`attempt_grading` 88, `handoff_doors` 45, `fc_sweep` 138); 55 public functions with no caller outside their own module (2,599 lines of *apparent* API). |
| dead / test-only | 600 | <1% | recon 03 §G: 297 lines with zero production references anywhere, 13 prod-dead constants, `note_apply_complete` (documented at `crossover_v2_flow.py:85` as a host lifecycle hook, called by tests only). |
| speculative / parked | 1,000 | 1% | the four hand-written `*_response_format()` JSON-schema docs (502 lines) read by two callers; 5 of 6 `*_REFUSAL_REASONS` frozensets with no production reader; `record_index`'s surviving SQLite vocabulary after ADR-0198 deleted its SQLite half. |
| verbosity in legitimate code | 11,250 | 15% | the DSP solver and the analysis organs are real work written long. |

## 2.4 Three biggest single deltas, half 2

1. **The four prescription doors — `driver_prescription.py` 2,508 /
   `blend_prescription.py` 1,688 / `topology_prescription.py` 964 /
   `alignment_prescription.py` 751 = 5,911 lines of one contract written four
   times.** Each has its own `*_SCHEMA_VERSION`, `*_KIND`, `*_MALFORMED`,
   `_PRESCRIPTION_FIELDS`, `*_REFUSAL_REASONS`, `*PrescriptionRefused`,
   `read_*` (105-158L), `*_from_mapping` (41-86L), `*_response_format`
   (59-256L) and `_finite_number`. The divergence is **already a live bug**:
   `blend` and `driver` catch `OverflowError` in `_finite_number`; `alignment`
   and `topology` do not, so a legal JSON integer (`10**400`) escapes the
   alignment door as an `OverflowError` instead of a refusal code — reproduced
   at HEAD in recon 03 §E.2. `blend_prescription`'s own comment describes what
   that costs: the CLI "blam[es] the round for a fault in the document."
   **−2,300, and it fixes two doors.**
2. **`crossover_v2_flow.py` — one class, 6,248 lines, 154 methods, a 796-line
   `__init__` (46 keyword-only params, 111 `self.` assignments, 557 comment
   lines).** It has no state record, so `durable_state.py` (2,198 lines) reads
   it through 21 getters that exist for no other reason, over three `conductor:
   Any` seams. A `SessionInputs` + `WalkState` record collapses the ctor, the
   getters, and most of `durable_state` at once. **−2,700 across the two files**
   (before the separate prose pass), and it is the prerequisite for every other
   extraction.
3. **Two evidence machines — do both need to exist?** *The store does not: it is
   already one.* `crossover_v2/record_store.py:53` delegates to
   `CommissioningEvidenceStore`, and five more `crossover_v2` modules
   (`evidence_packet:144`, `feature_classifier:89`, `position_cycle:78`,
   `record_index:28`, `ring_projection:81`) read `EVIDENCE_ROOT` from
   `commissioning_evidence_store`. The store docstring says so explicitly:
   "This is the writer that already exists, not a second one."
   **What is genuinely duplicated is one layer up:** the record model
   (`commissioning_evidence` + `_receipt` hand-roll 24 frozen dataclasses in one
   dialect; `crossover_v2` hand-rolls 40 `to_dict`s in another, with no base in
   either), the admission gate (`commissioning_admission` 1,238 vs
   `crossover_v2/admission` 617), and verification (`commissioning_verification`
   845 vs `crossover_v2/verification` 2,186). The two lanes are two *eras* of one
   loop, not two concerns: the crossover-v2 engine never imports the 5,332-line
   evidence/receipt model at all — only `measured_candidate.py:38` bridges them.
   **One record kit + one refusal primitive across both lanes: −6,700.**

## 2.5 The v1 leftovers, named

`jasper/web/correction_crossover_backend.py` (1,748) is the pre-crossover-v2
summed-region backend, still routed from `/correction/`
(`correction_setup.py:5296`, `correction_crossover_flow.py:131`,
`correction_crossover_v2.py:7615`). It is the **only** consumer of
`crossover_level_run.py` (1,038), `crossover_eligibility.py` (371),
`commissioning_service.py` (1,317) and `commissioning_isolated_producer.py`
(925). Together with the orphaned summed-graph lane recon 01 §4 verified
(418 lines in `commissioning_runtime.py:181-801` + 549 in
`web_commissioning.py`, tests-only), that is **~6,400 lines of a superseded
flow still wired to a live web surface.** Deciding whether `/correction/` keeps
two crossover flows is an owner call, not a refactor — but it is the single
largest yes/no in this unit.

## 2.6 Ordering note

Half 2's moves have one hard prerequisite chain: prose pass → `SessionInputs`/
`WalkState` record → organ extraction. Everything else (the door kit, the
refusal primitive, the record kit, the v1 decision) is independent and can land
in parallel. Half 1's boundary fix (§1.2 moves 1-4) is independent of all of it
and is the cheapest high-value change in either half: **~450 lines touched,
−47,000 lines off the product runtime's import surface.**

## 2.7 Uncertainty, stated

- The 23,600 and 9,300 figures are first-principles targets from the component
  list, not a measured refactor. The reproducible floors underneath them are:
  9,898 history-marked prose lines in `crossover_v2/`, 5,717 lines of
  hand-written serialisation in half 2, 5,911 lines in four door copies, 1,463
  lines in seven emitters, 1,015 verified-dead lines (recon 01 §4).
- I did not verify that `_core()` dict ordering is the only fingerprint input
  for all 24 commissioning records; the record-kit move is safe only per class.
- The half-1/half-2 split of `baseline_profile.py` (record stays, 986-line
  builder moves) is my judgement call, not something HEAD makes explicit.
- `cut3.py` models package-`__init__` execution and top-level imports only. It
  under-counts what a *long-running* process eventually imports through lazy
  edges; it is the right measure for "what does booting cost", not for "what
  can this process reach".
