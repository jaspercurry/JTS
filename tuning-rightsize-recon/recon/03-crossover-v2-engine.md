# Recon 03 — the crossover-v2 engine

Scope: `jasper/active_speaker/crossover_v2/` (63 modules, 54,553 lines) and
`jasper/active_speaker/crossover_v2_flow.py` (7,840 lines, one class).
HEAD `c032503`. Read-only. All counts reproducible; commands cited inline or in
`scratchpad/recon/stats.py` (AST-based; comments/docstrings excluded from every
"caller" count, which matters here — 48% of the package is prose).

## Headline

| | lines | prose (docstring+comment) |
|---|---|---|
| `crossover_v2/` 63 modules | 54,553 | 26,068 (**47.8%**) |
| `crossover_v2_flow.py` | 7,840 | 4,322 (**55.1%**) |
| combined | 62,393 | 30,390 (48.7%) |

`python3 scratchpad/recon/stats.py`

Of 27,936 prose lines, **9,898 (35%) sit in a paragraph that cites an issue
number, an ADR, a date, a "Phase/PR-/wave/ruling", or narrates what the code
"used to" be** — the four things AGENTS.md's comment rule forbids by name.
That is the floor, not the estimate: blocks that merely narrate what the code
does carry no marker and are not in the 35%.
Raw markers: 606 `#NNNN`, 45 `ADR-NNNN`, 232 dates in the package; 205 `#NNNN`
and 54 dates in the flow file alone.

Three structural facts dominate everything below:

1. **Two session objects are alive at once**, constructed 150 lines apart in
   the same web function: `CrossoverV2Session` (flow, 154 methods) at
   `jasper/web/correction_crossover_v2.py:6807` and `TuningSession`
   (`crossover_v2/session.py`) at `:6964`. This is not a strangler in flight —
   they are two halves of one session that nobody joined.
2. **21 of the flow class's 37 externally-used public members exist so that one
   module can serialize it** (`durable_state.py`). The god object has ~30
   getters because it has no state record.
3. **Four prescription doors are four copies of one contract**, and the copies
   have already diverged into a live bug (§E.2).

---

## A. The map — 63 modules in six layers

Per-file detail (lines · prose% · imports-within-package · imported-by-within-package)
is in `scratchpad/recon/stats.json`. Layer totals:

| layer | files | lines | prose |
|---|---:|---:|---:|
| L0 vocabulary/contracts — `contracts` `journey` `programs` `priors` `measure_spec` `measurement_phase` `attempt_grading` `tuning_scope` `feature_optics` `__init__` | 10 | 4,360 | 52% |
| L1 engine (new, ADR-0198) — `session` `session_seams` `session_graph` `volume_claim` `playback_transaction` `program_transaction` `composition` `door` `record_store` `record_index` | 10 | 3,553 | 54% |
| L2 capture/plan — `capture_plan` `sweep_spec` `capture_dispatch` `capture_source` `position_cycle` `round_captures` `admission` `fc_sweep` | 8 | 7,483 | 49% |
| L3 analysis — `spatial` `verification` `feature_classifier` `evidence_packet` `round_views` `gate_sweep` `harmonic_evidence` + 13 more | 20 | 22,929 | 42% |
| L4 recommend/prescription — 4× `*_prescription` `prescription_spool` `handoff_doors` `intervention` `plan_assembly` `planning` `candidates` `proposal` | 11 | 10,606 | 50% |
| L5 legacy-session organs — `coordinator` `durable_state` `refusal_copy` `operator_notes` | 4 | 5,622 | 58% |

Hubs (imported-by count, within package): `contracts` 23, `journey` 13,
`verification` 7, `spatial` 6, `plan_assembly`/`round_evidence`/`evidence_packet` 5.
Sinks (imported-by 0 inside the package; driven only by the flow or a CLI):
16 modules incl. `capture_plan`, `round_views`, `harmonic_evidence`,
`diagnostics`, `close_reference`, `prescription_spool`, `door`, `record_store`.

### Layering violations (evidence)

| # | violation | evidence | why it is a smell |
|---|---|---|---|
| A1 | analysis imports the recommend layer | `evidence_packet.py:156,158` imports `blend_prescription.prescription_response_format` and `driver_prescription.*` | The packet is the LLM's read of a round; it should not depend on the *writing* door's schema emitter. Inverts L3→L4. |
| A2 | household copy sits on top of analysis | `refusal_copy.py` imports `capture_dispatch`, `spatial`, `verification` (`stats.json`) | Rendering one sentence pulls in 6,270 lines of analysis. Copy should be a leaf. |
| A3 | walk vocabulary leaks into analysis | `journey` is imported by `evidence_packet`, `feature_classifier`, `harmonic_evidence`, `position_cycle`, `round_views`, `spatial` | An offline re-analysis of a banked round should not need the wizard's phase names. |
| A4 | engine imports the room-correction package | `crossover_v2/door.py:163` `from jasper.correction import coordinator` (for `MEASUREMENT_GATE_OWNER`, `correction/coordinator.py:121`) | Cross-domain; also collides with `crossover_v2/coordinator.py` — two modules named `coordinator`, one importing the other's package. |

No `crossover_v2 → jasper.web` import exists (the MS-17 pin holds). A4 is the
only upward import, and it is a one-constant dependency.

---

## B. `crossover_v2_flow.py` — what the class owns vs delegates

`CrossoverV2Session`: 6,248 lines, **154 methods** (5,972 lines of method
bodies), plus a 1,488-line preamble (56% prose) holding 16 module functions,
`AnalyzeCapture`, `RecordModelError` and `V2FlowSeams`.

**`__init__` is 796 lines: 557 comment (70%), 2 blank, 237 code.**
46 keyword-only ctor params, 111 `self.<attr>` assignments. There is no state
record — the ctor *is* the record, and the comment block on each param is where
its contract lives (e.g. lines 1568-1573 for `_tier`).

### Method groups (line totals, generated)

| group | methods | lines | already has a home |
|---|---:|---:|---|
| `__init__` state bag | 1 | 796 | — (needs a `SessionInputs`/`WalkState` record) |
| cloud group close (spatial) | 19 | 1,020 | `spatial.py` (3,148) |
| verify + grade + round | 14 | 892 | `verification.py` (2,186), `coordinator.py` (1,410) |
| capture ingest state machine | 21 | 812 | **keep — this IS the session** |
| measure verdict + trust reservations | 13 | 567 | `verification.py` / new organ |
| candidate build / commit / publish | 10 | 564 | `planning.py`, `proposal.py` |
| read accessors (properties) | 41 | 377 | a snapshot record |
| prescription accessors | 9 | 204 | the four `*_prescription.py` |
| lateral walk | 7 | 203 | `capture_plan.LATERAL_POSE_PROMPTS` / `angle_capture` |
| refusal + diagnostics logging | 5 | 155 | `refusal_copy.py`, `diagnostics.py` |
| commanded / predicted sum | 3 | 143 | `commanded.py` (481) |
| entry baseline | 4 | 138 | `round_evidence.py` (1,001) |
| durable snapshot / hydrate | 5 | 97 | `durable_state.py` (2,198) |

Biggest single methods: `_close_cloud_group` 256, `_grade_verify_attempt` 235,
`_verify_verdict` 201, `_measure_verdict` 196, `_run_cloud_pipeline` 169,
`consume_capture` 168, `_resolve_spent_slot` 159, `_grade_round_once` 148,
`authorize_begin` 147.

### The serializer tax

Of 64 public members, 37 are touched by production code outside the flow —
and **21 of those 37 are touched only by `durable_state.py`**
(`accepted_phases`, `attempt_history`, `delta_probe`, `group_*`,
`last_attempt_decision`, `measure_*`, `session_phases`, `verify_*` ×9…).
27 public members have **zero** production callers outside the flow, including
`note_apply_complete` — documented at `crossover_v2_flow.py:85` as a
"lifecycle hook the host needs", called by tests only (production instead
passes `applied=True` at construction, `correction_crossover_v2.py:6829`).

### Proposed decomposition (6 extractions + 2 collapses)

Order matters: 1 and 2 are prerequisites; 3-6 are then mechanical.

| # | move | to | est. lines out of the flow | risk | proof |
|---|---|---|---:|---|---|
| 1 | Prose to the AGENTS.md bar across the whole file (55.1% → ~12%) | — | −3,000 | low | `scripts/test-fast`; no behavior touched. Do it first so every later diff is readable. |
| 2 | `__init__`'s 46 params → a frozen `SessionInputs` record in `contracts.py`; the 111 attrs → a `WalkState` dataclass; `snapshot()` returns it | `contracts.py` + new `walk_state.py` | −600 (after 1) | med | `durable_state` round-trip tests; `test_every_host_owned_apply_key_survives_persist_conductor_state` |
| 3 | Delete the 21 durable-state-only getters; `build_conductor_state(state: WalkState)` reads the record | `durable_state.py` | −300 | low | same tests as 2 |
| 4 | Cloud group close → `spatial.close_cloud_group(state, …) -> CloudGroupResult` | `spatial.py` | −800 | med | `tests/test_crossover_v2_spatial.py` behavior pins |
| 5 | Verify/grade → `verification.grade_verify(state, analysis)`; round grading → `coordinator.run_round` (already the shape) | `verification.py`, `coordinator.py` | −700 | med | `tests/test_crossover_v2_round_wiring.py` |
| 6 | Measure verdict + the three `_note_*_reservation` disclosures → one `measure_verdict.py` organ | new organ | −450 | med | verdict-code pins |
| 7 | Candidate build/commit → `planning.build_candidate` / `proposal.build_intervention_proposal` (both already exist and are prod-dead, §G) | `planning.py`, `proposal.py` | −450 | med | candidate fingerprint pins |
| 8 | Lateral walk → `angle_capture` (only the operator's staged walk still builds one; no stage-1 plan does — `crossover_v2_flow.py:60-64`) | `angle_capture.py` | −200 | low | `tests/test_angle_capture_take.py` |

**Realistic end-state: ~1,200-1,500 lines** — a state record, `authorize_begin`
/ `on_armed` / `program_for_phase` / `consume_capture`, the phase dispatch
table, `snapshot`/`hydrate`, and delegation. That matches
`docs/REFACTOR-TUNING-2026-08.md` §1's ~1,500 figure, and the arithmetic above
gets there (7,840 − 6,500). The class that remains is the **walk state machine**
— the thing the four verbs do not express (`measurement_phase.py:36-42` already
says so: `check`, `applying`, `lateral` and the two clouds "have no engine kind
at all… reachable only through the flow's own walk").

`TuningSession` is the `measure` verb (ADR-0198 narrowed it to
`open`/`measure`/`close`). The end-state is **flow = walk, TuningSession =
measure**, with the flow's `program_for_phase` feeding the engine's compose
seam exactly as it already does at `correction_crossover_v2.py:6975`.

---

## C. Records and serialization

- **121 `@dataclass` in the package. 40 classes hand-write a `to_dict`-ish
  method; only 7 have a `from_dict`-ish inverse.** Write-only serialization is
  the norm; readers re-parse by hand.
- **No shared base or helper.** The nearest thing is the `_core()` +
  `to_dict()` + `json_fingerprint(self._core())` triple in `contracts.py`
  (`CandidateAcousticContext:471`, `InterventionProposal`, `RoundReceipt`) —
  3 copies of the pattern, in one file, not factored.
  `record_store.py` routes and publishes but does not serialize;
  `contracts.detached_json` is the only shared primitive (a deep JSON copy).
- **19 independent `*_SCHEMA_VERSION` constants** with no shared envelope:
  `contracts.SCHEMA_VERSION=2`, plus 18 module-local `=1`s
  (`grep -rn "^[A-Z_]*SCHEMA_VERSION" crossover_v2/*.py`).
- **One schema-validation approach: none.** No `jsonschema`, no `pydantic`, no
  `TypedDict` anywhere in the package. Every reader hand-rolls, which is how
  §E.2's divergence happened.
- **Field-validator re-rolls:** `_finite_number` ×4 (alignment, topology,
  driver, blend), `_finite_or_none` ×4, `_optional_number` ×2, `_text` ×3
  (`contracts`, `record_index`, `feature_classification`), `_mapping` ×2.
- **Fingerprints:** only 2 `hashlib.sha256` call sites in the package
  (`round_captures._declared_program_sha:119`, `session_graph._fingerprint:99`);
  everything else goes through the one shared
  `audio_measurement.evidence_identity.json_fingerprint`. **This is the one
  place the package already did the right thing** — the 15-helper problem the
  brief cites lives outside this area.

**Proposed:** one `record.py` with a `JsonRecord` mixin (`_core()` →
`to_dict()`/`from_dict()`/`fingerprint`) plus a single `fields.py` with
`finite_number`, `finite_or_none`, `text`, `mapping` taking a
`refuse: Callable`. Est. −1,200 lines including the docstrings that currently
argue each copy is not the other. Risk: med (fingerprints must not move —
`_core()` output is hashed; the pin is `test_crossover_v2_contracts.py`).

---

## D. Refusals — one registry, and everything else

`refusal_copy.REASON_REGISTRY` holds **44 codes** and is the SSOT for
household-facing copy. Consumers: `crossover_v2_flow.py` (9 sites),
`crossover_envelope_v2.py` (4), plus 5 package modules import the module —
**16 production files total**, 27 test files.

Everything that is *not* household copy re-rolls:

| shape | count | evidence |
|---|---:|---|
| exception classes | **29** | `admission`, 4× `*PrescriptionRefused`, `CaptureBeginRefused`/`Deferred`/`Failed`/`Stopped`, `FeatureClassificationRefused`, `HarmonicEvidenceRefused`, `RingProjectionRefused`, `RoundCapturesRefused`, `DelayLandscapeError`, … |
| near-identical `(reason, detail\|evidence)` `__init__` | **7** | `AlignmentPrescriptionRefused`, `TopologyPrescriptionRefused`, `BlendPrescriptionRefused`, `FeatureClassificationRefused`, `HarmonicEvidenceRefused`, `RingProjectionRefused`, `RoundCapturesRefused` — differing only in base class (`ValueError`/`RuntimeError`/`Exception`) and whether the payload field is `detail` or `evidence` |
| module-local `_refuse()` NoReturn helper | 3 identical | `blend_prescription`, `driver_prescription`, `prescription_spool` |
| `*_REFUSAL_REASONS` frozensets outside the registry | 6 | `ALIGNMENT_`, `BLEND_`, `TOPOLOGY_`, `DRIVER_`, `CLASSIFICATION_`, `PRESCRIPTION_SPOOL_` — **5 of the 6 have zero production readers**; only tests assert their exhaustiveness |
| module-level `REFUSE_*` constants outside the registry | 17 | `admission` 3, `close_reference` 2, `door` 3, `gate_sweep` 2, `round_captures` 7 |
| `VERDICT_/PHASE_/CODE_/GATE_` constants | 29 | `journey` 11, `close_reference` 5, `delay_landscape` 4, … |
| naming variants for the same act | — | `verdict` 9 defs, `gate` 19, `screen` 17, `refuse` 12, `refusal` 4, `door` 5, `stop` 3 |

**Proposed single primitive** (`refusal.py`, ~80 lines):

```
@dataclass(frozen=True)
class Refusal:            # one carrier
    code: str             # the ONE vocabulary; registry membership checked once
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: ...

class Refused(Exception): # one exception, carrying one Refusal
    def __init__(self, refusal: Refusal) -> None: ...

def refuse(code: str, detail: str, **evidence: Any) -> NoReturn: ...
```

`REASON_REGISTRY` grows a `copy: str | None` field — `None` means "named, no
household sentence" (which is what `attempt_grading.ATTEMPT_REASON_NO_FLOOR`
already is, documented at `attempt_grading.py:9-13`). The 6 `*_REFUSAL_REASONS`
frozensets become registry queries; the 29 exception classes become one.
Est. −900 lines, med risk (each `except SpecificRefused` site becomes
`except Refused as e: if e.refusal.code in …`); proof is the existing
per-door refusal-code tests, which assert codes, not classes.

---

## E. The prescription doors — four copies of one contract

`driver_prescription` (2,508) · `blend_prescription` (1,688) ·
`topology_prescription` (964) · `alignment_prescription` (751) = **5,911 lines**.

### E.1 There is no shared prescription contract. Each door restates all of it:

| element | driver | blend | topology | alignment |
|---|---|---|---|---|
| `*_SCHEMA_VERSION` / `*_KIND` | ✓ | ✓ | ✓ | ✓ |
| `*_MALFORMED` / `*_SCHEMA_UNSUPPORTED` / `*_PROVENANCE_MISSING` | ✓ | ✓ | ✓ | ✓ |
| `_PRESCRIPTION_FIELDS` allow-list | ✓ | ✓ | ✓ | ✓ |
| `*_REFUSAL_REASONS` frozenset | ✓ | ✓ | ✓ | ✓ |
| `*PrescriptionRefused` class | ✓ | ✓ | ✓ | ✓ |
| `read_*_prescription()` | 124 L | 109 L | 158 L | 105 L |
| `*_prescription_from_mapping()` | 54 L | 86 L | 41 L | 43 L |
| `*_response_format()` (the LLM-facing schema) | 256 L | 125 L | 62 L | 59 L |
| `_finite_number` / `_finite_or_none` | ✓ | ✓ | ✓ | ✓ |

The four `*_response_format()` functions (502 lines, hand-written JSON schema
docs) are consumed by `evidence_packet.py` and `handoff_doors.py` only.

**Four parallel paths for "prescription onto candidate":**
`driver_prescription_to_candidate_fields` (137 L),
`blend_prescription_to_candidate_fields` (40 L),
`planning.alignment_to_candidate_fields`,
`topology_prescription.apply_topology_pin` + `candidate_topology`.

### E.2 The divergence is already a bug (reproduced)

`blend` and `driver` catch `OverflowError` in `_finite_number` (a legal JSON
integer like `10**400` is a legal Python `int` that `float()` refuses).
`alignment` and `topology` do not. Reproduced at HEAD:

```
$ python3 -c "... ap.read_alignment_prescription({'kind':'jts_crossover_alignment_prescription',
      'artifact_schema_version':1,'delay_us':10**400,'polarity':'positive'}, fc_hz=2000.0,
      declared_bounds_us=(-2000.0,2000.0))"
*** OverflowError escapes the alignment door: int too large to convert to float
```

`blend_prescription._finite_number`'s own comment says what this costs: "it
escaped the gate entirely as an OverflowError, which the CLI then reported with
the evidence-unreadable exit code — blaming the round for a fault in the
document." Two doors still have that bug because the fix was applied to a copy.

The two `max_q_for_gain` twins are the same story in miniature:
`blend_prescription.max_q_for_gain` (25 lines) and
`driver_prescription.driver_max_q_for_gain` (29 lines) are the identical
one-line function `return X if gain_db > 0.0 else EVALUABLE_Q_MAX`, each
carrying a ~20-line docstring explaining why it is not the other. 46 lines of
prose for one parameter.

**Proposed:** one `prescription/` door kit — a `PrescriptionDoor` descriptor
(kind, schema version, field allow-list, refusal codes, per-field validators)
and one generic `read_prescription(door, raw)`. The four modules keep only
their domain rules (passband checks, lobe window, slope floor, composed-boost
grid). Est. **−2,000 to −2,500 lines**, med-high risk; proof is the four
existing per-door test files (`tests/test_crossover_v2_*_prescription.py`),
which assert codes and accepted/refused outcomes, not internals.

---

## F. Prose over the bar

Per-file prose share (top offenders, `stats.py`):

Small modules: `session_seams` 78.5%, `capture_source` 70.4%, `fc_sweep` 68.1%,
`tuning_scope` 65.8%, `playback_transaction` 65.0%, `volume_claim` 63.2%,
`programs` 61.9%, `candidates` 61.2%, `admission` 60.0%.
Large modules: `capture_dispatch` (936) 60.3%, `spatial` (3,148) 59.5%,
`durable_state` (2,198) 59.4%, `session` (733) 59.3%, `refusal_copy` (1,578)
58.9%, `planning` (996) 58.9%, `coordinator` (1,410) 57.1%, `capture_plan`
(3,050) 55.0%, `evidence_packet` (3,753) 39.5%.

Longest single docstrings: `__init__.py` module 178 lines,
`spatial.assemble_cloud_group_result` 176, `driver_prescription` module 153,
`topology_prescription` module 147, `evidence_packet` module 118.

**Quote 1 — `capture_plan.py:132-181`, a 50-line comment run on one integer
constant.** It is a change log with arithmetic:

> `**12 → 11 when #2291's entry baseline landed**, because that claim is what
> this number IS… **That row is the shipped one since the 2026-08-24 geometry
> ruling**, which put the design axis into the post-apply pose set and took
> `DEFAULT_CLOUD_VERIFY_POSITIONS` from 5 to 6. It used to be 31 at M=5 — one
> index under — and this comment used to say raising N would spend that last
> index. The ruling spent it on a capture instead.`

The non-derivable constraint is two lines: the ceiling is the relay's blob-index
space, and `assert_cloud_plan_fits_relay_capacity` is the executable claim.
The other 48 are history.

**Quote 2 — `spatial.py:179-206`, a comment that records a withdrawn argument:**

> `A "replacing is better physics" argument was made and WITHDRAWN under review
> (2026-07-26): the reviewer computed the power-mean counterexample, where
> APPENDING a wide position to a clustered cloud fills a −15 dB null further
> than replacing does (−6.1 dB vs −7.7 dB)… PR-4 carries it further… but no
> household-facing surface renders it yet (zero JS/asset changes in PR-4).
> PR-7 renders it.`

Text addressed to a reviewer, about a PR that has landed, describing a UI state
("PR-7 renders it") no reader can verify at HEAD.

**Quote 3 — `durable_state.py:1986-2009`, incident narration inside a function:**

> `THREE separate P0s have now been caused by a host-owned key being added to
> observe_apply_success without a line here (the way-back stash W6.12; cloud B1;
> this offset, #1811). test_every_host_owned_apply_key_survives_persist_conductor_state
> derives the host-owned set mechanically… and fails on the FOURTH.`

The mechanical test *is* the guard. The paragraph is the incident log the guard
replaced.

**`evidence_packet.py:5-50`** is the fourth pattern — a 118-line module
docstring that spends 30 lines justifying the module's existence against a
sibling ("deliberately no import in either direction… different domain") and
20 more enumerating what it does *not* compute.

Also worth naming: **`fc_sweep.py`'s docstring says the filename is a lie** —
"The filename is historical… there is no sweep here, and a reader who came
looking for one is reading about cancelled work." 138 lines, 68% prose, kept
under a wrong name because renaming "would touch every module, test and document
that names it". That is exactly the owner's "anyone would understand it from the
folder structure and files" failing, and the rename is a one-command change.

**Proposed:** prose to the AGENTS.md bar file by file, biggest-prose-first,
one PR per 3-4 modules. Est. **−12,000 to −16,000 lines** across the area at a
12-15% target (the 35% history-marked floor is 9,900 lines on its own). Risk:
low — but do NOT script it; the constraint lines (units, ranges, hardware
quirks, the relay blob-index ceiling, the `EVALUABLE_Q_MAX` fidelity note) must
survive.

---

## G. Dead and test-only code

Method: AST reference scan over `jasper/ tests/ scripts/ experiments/ deploy/`,
counting only *code* positions (Name/Attribute/ImportFrom/identifier-strings),
excluding `__all__` entries and prose — which matters, because a plain `grep`
finds every one of these alive in a docstring.

**Zero production code references anywhere (incl. their own module) — 297 lines:**

| symbol | file:line | lines | test files |
|---|---|---:|---:|
| `confirmation_verdict` | `delay_landscape.py:492` | 93 | 9 |
| `driver_prescription_from_mapping` | `driver_prescription.py:2169` | 54 | 11 |
| `read_position_cycle` | `position_cycle.py:519` | 43 | 13 |
| `candidate_priors` | `priors.py:314` | 41 | 2 |
| `CaptureAnswer` | `capture_source.py:165` | 32 | 7 | (deleted by in-flight #3724) |
| `takes_by_position` | `position_cycle.py:564` | 24 | 5 |
| `set_prescription_spool_path_for_tests` | `prescription_spool.py:341` | 10 | 10 |

**Prod-dead but same-module-alive (public API that should be private):**
55 public functions have no caller outside their own module, 2,599 lines of
definition. Making them private is free and shrinks the package's apparent
surface by ~40%. Notable large ones: `close_reference.compare_impulse_responses`
(253), `harmonic_evidence.rebuild_measure_program` (171),
`blend_prescription.positional_support` (117), `intervention.decide_trim` (102),
`commanded.profile_graph_summation` (96).

**Prod-dead constants:** 13 module-level `UPPER_CASE` names with zero production
reads (`admission.DECISION_KINDS`/`SETTLE_KINDS`, `coordinator.REFUSAL_KINDS`,
`feature_classification.CLASSIFICATIONS`/`UNCERTAINTY_KINDS`,
`measure_spec.STUB_CODES`, …), 5 of them the `*_REFUSAL_REASONS` frozensets
from §D. Each exists so one test can assert a vocabulary is exhaustive.

**Dead flow API:** `CrossoverV2Session.note_apply_complete`
(`crossover_v2_flow.py:3249`) — documented at `:85` as a host lifecycle hook,
zero production callers; production passes `applied=True` at construction
instead. 27 public members of the class have no production caller outside the file.

**Not dead** (checked and cleared): every module is reachable from a runbook CLI
or the wizard. `feature_optics`, `session_seams`, `handoff_doors` have no
external importer but real in-package consumers; `record_index` is alive via
`evidence_packet`/`ring_projection` despite ADR-0198 deleting its SQLite half.

---

## H. Abstractions that don't earn their keep

| # | thing | evidence | move |
|---|---|---|---|
| H1 | **`attempt_grading.py`** — 88 lines, 81.8% prose, **three constants, no code** | whole file | fold into `admission.py` or `contracts.py`; −70 |
| H2 | **`handoff_doors.py`** — 45 lines, one 20-line function with one caller (`evidence_packet`) that returns two `*_response_format()` calls | whole file | inline into `evidence_packet`; −45 |
| H3 | **`fc_sweep.py`** — 138 lines under a name its own docstring calls historical | `fc_sweep.py:33-40` | rename to `corner.py` (2 importers); the paragraph goes |
| H4 | **`composition.py`** — 3 functions, 255 lines, named for "composing the engine", while `plan_assembly.compose_linearized_prediction` is a different "compose" | `composition.py:5-27` | rename `engine_binding.py`; −0 lines, −1 collision |
| H5 | **`candidates.py`** (237, 61% prose) — 3 dataclasses that exist "because a fact tried to travel on `self`" | `candidates.py:9-14` | merge into `planning.py` with move B.7 |
| H6 | **`V2FlowSeams`** — 15 seams, 10 `Optional[…] = None`, one paragraph each on what `None` means: **106 lines for a dataclass** | `crossover_v2_flow.py:1071-1176` | one sentence per field; converge on the engine's `EngineSeams` |

### Three names for one concept

| concept | names in use | count |
|---|---|---|
| "what to do next" | **prescription** (4 modules + spool), **proposal**, **plan** (`plan_assembly`, `LinearizationPlan`), **candidate** (`candidates`, `planning`), **intervention**, **correction** (`blend_correction`), **recommend** (the doc's settled word) | 7 names, 11 modules, 12,000 lines |
| "the thing driving the session" | **`CrossoverV2Session`** (flow), **`TuningSession`** (`session.py`), **`CommissionJourney`** (`journey.py`), **`coordinator.run_round`**, **conductor** — the class was deleted in Phase 5c-iv but **388 `conductor` references remain**, including the live API `build_conductor_state` / `persist_conductor_state` / `V2ConductorSnapshot` and `durable_state.py`'s `conductor: Any` parameters | 5 names |
| "no" | **refusal / refuse / refused / verdict / gate / screen / door / stop / reject** | 9 (§D) |
| `coordinator` | `crossover_v2/coordinator.py` **and** `correction/coordinator.py`, the second imported by `crossover_v2/door.py:163` | 2 modules, 1 name |

`durable_state.py`'s `conductor: Any` parameters (`:650`, `:664`, `:680`) are
the boundary between the flow and its organs, and it is **untyped duck-typing
of a 6,248-line class** — which is why the 21 getters in §B exist.

---

## Top moves, ranked

| # | move | Δ lines | risk | proof |
|---|---|---:|---|---|
| 1 | Prose to the AGENTS.md bar, biggest-prose-first, 3-4 modules per PR. Start `session_seams`, `capture_source`, `fc_sweep`, `tuning_scope`, `volume_claim`, `playback_transaction` (all >63%), then `spatial`, `durable_state`, `capture_plan`, `coordinator`, `refusal_copy`. | **−12,000…−16,000** | low | `scripts/test-fast`; delete only blocks that cite an issue/date/ruling or narrate what the code does |
| 2 | Prose pass on `crossover_v2_flow.py` (55.1% → 12%), `__init__` first (557 comment lines) | **−3,000** | low | as above; it makes moves 4-8 reviewable |
| 3 | One prescription door kit; collapse the 4 readers, 4 `from_mapping`, 4 `response_format`, 8 field validators. **Fixes the live `OverflowError` escape in the alignment and topology doors.** | **−2,000…−2,500** | med-high | the four `tests/test_crossover_v2_*_prescription.py` files assert codes and outcomes; add one parametrized `10**400` case per door |
| 4 | One refusal primitive: `Refusal` + `Refused` + `refuse()`; registry gains `copy: str \| None`; 29 exception classes → 1; the 6 `*_REFUSAL_REASONS` frozensets → registry queries | **−900** | med | existing per-door refusal-code tests |
| 5 | `SessionInputs` + `WalkState` records; `snapshot()` returns the record; delete the 21 durable-state-only getters; `durable_state` stops taking `conductor: Any` | **−900** | med | `test_every_host_owned_apply_key_survives_persist_conductor_state` + durable round-trip |
| 6 | Extract cloud-group close → `spatial`; verify/grade → `verification`/`coordinator`; measure verdict → a new organ; candidate build → `planning`/`proposal` | **−2,400** (out of the flow) | med | per-organ behavior pins; verdict codes and fingerprints must not move |
| 7 | One `JsonRecord` mixin + one `fields.py`; retire the 40 hand-written `to_dict`s and the 19 loose `SCHEMA_VERSION`s behind one envelope | **−1,200** | med | fingerprint stability pins (`_core()` output is hashed) |
| 8 | Make the 55 module-private-in-practice public functions private; delete the 7 test-only public symbols and `note_apply_complete`; delete or registry-fold the 13 prod-dead constants | **−300**, −40% of apparent API surface | low | grep + `scripts/test-merge` |
| 9 | Naming: `conductor` → `session` across 388 refs (incl. `build_conductor_state`); `fc_sweep` → `corner`; `composition` → `engine_binding`; fold `attempt_grading` and `handoff_doors` into their consumers | −120 | low | mechanical rename; one PR each |
| 10 | Layering: move `MEASUREMENT_GATE_OWNER` out of `jasper.correction` (A4); stop `evidence_packet` importing the prescription doors (A1); make `refusal_copy` a leaf (A2) | −0 | med | `test_no_domain_module_imports_the_host_or_the_legacy_flow`, `test_the_package_import_graph_stays_acyclic` |

**Order:** 1 → 2 → 8 (free, makes the surface legible) → 5 → 6 → 4 → 3 → 7 →
9 → 10. Moves 1, 2 and 8 are independent of everything else and of the in-flight
PRs; move 3 must land after #3724 (which deletes `capture_source.py`), and
move 6 will conflict with any PR touching `correction_crossover_v2.py`.

### Uncertainty, stated

- The −12,000…−16,000 prose figure is a target, not a measurement. The
  reproducible floor is 9,898 lines in history-marked paragraphs.
- The B-table extraction sizes assume move 1 lands first; before it, the same
  extractions move roughly twice the lines and are twice as hard to review.
- I did not verify that the alignment/topology `OverflowError` is reachable
  from `jasper-crossover-prescriber stage` end to end — only that it escapes
  `read_alignment_prescription`, which is that CLI's door.
- `tests/test_angle_capture_take.py:1042` asserts on *source text*
  (`assert "lateral_prompts=lateral_prompts" in source`,
  `source.count(...) == 3`) — an AGENTS.md violation that will block move 8 in
  that file. Tests are another agent's area; flagging the collision only.
