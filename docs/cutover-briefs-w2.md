# W2 brief — the analyze registry

> **This file dies when W2 lands.** It is an implementation brief for the four
> work items of [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md) §2,
> not a durable reference. The last W2 PR deletes it, and nothing may cite it —
> a fact that belongs to the tree after W2 belongs in a docstring or an ADR,
> never here.

Re-derived at `c253c3cf1`. Every citation below was read at that commit.

---

## 1. The analysis-unit enumeration

### 1.0 Two counts, and §2 conflates them

§2's *PREMISE FAILED* block holds at HEAD in both halves: **79 modules**
reproduces (35 `audio_measurement` + 44 `crossover_v2`) and so does the flagged
coincidence (`jasper/active_speaker/*.py` = 92); **92 analysis units** does not.

**Recorded disagreement with §2's W2-d.** It offers *"restate acceptance row 3c's
analysis claim against the pinnable 79"* as an **alternative** to enumerating 92.
It is not one — the numbers answer different questions. **79 is
boundary-membership** (*which modules are in the truth layer*), pinned by an
import test (`tests/test_correction_boundary_ssot.py:175`, scoped to
`jasper/audio_measurement` at `:187`); widening that scope is cheap and W2-d
should do it, but it says nothing about the registry. **The registry's acceptance
claim needs a UNIT count** (*how many named analyses the table holds*), because
"wholesale" is a coverage claim. So W2-d ships **both**, and the boundary
widening must not be read as having settled the coverage claim.

### 1.1 The unit definition

A registry unit exists to be **gated**. `AnalysisUnit(name, run, gate)` buys
nothing over a bare function unless `gate` can answer `False` for a reason that
is a property of the capture. So the unit is defined by its gate, not by its
call graph:

> **A unit is a maximal set of `ProgramAnalysis` fields that are present or
> absent together over every `(program, capture, priors)` triple.**
>
> A field is **produced** — and so eligible for a unit — when its value at the
> construction site is bound from a statement that reads the capture samples. A
> field that copies an input parameter (**passthrough**), that re-reads another
> field's value (**projection**), or that is a predicate over inputs alone, is
> not produced and belongs to no unit.

Two properties this buys, both load-bearing:

- **Grouping is forced, not chosen.** `linearity_ok`, `channel_map_ok` and
  `pilot_snr_ok` are total reductions of `pilots` (`:5541-5543`) — no capture
  makes one present and another absent — so they are one unit with `pilots`,
  not four rows. A four-row table would need three gates that can never differ,
  and a gate that cannot answer `False` is the `_crossover_region_null_registry`
  defect wearing a table.
- **It is mechanically checkable at review time.** Splitting a unit obliges the
  author to name a bank where one half runs and the other does not. That is the
  same discipline `measure_spec._ROWS` (`:159`) enforces by construction.

### 1.2 The method, committed

Reproducible in one pass. Step 1 is fully mechanical; step 2 is a read of the
guards, each cited, so a reviewer re-checks rather than re-derives.

**Step 1 — the field census (script).** Parse
`jasper/audio_measurement/program_analysis.py` with `ast`; collect
`ProgramAnalysis` `AnnAssign` targets; collect every `ProgramAnalysis(...)`
construction and every `replace(...)` whose keywords are *all* `ProgramAnalysis`
fields; classify each keyword's value expression as passthrough (attribute of a
call parameter), projection (attribute of a local bound from another field),
or produced. Ship this as the W2-d pin so the census re-runs in CI rather than
living in prose.

At `c253c3cf1` it returns:

| Class | n | Fields |
|---|---|---|
| passthrough | 3 | `phase` (`program.phase`), `program_id` (`program.program_id`), `mic_tier` (`priors.mic_tier`) |
| projection | 1 | `glitch_detected` — `drift.glitch_detected` (`:6450` block) / `integrity.glitched` (`:7235` block); the field's own comment at `:1929-1934` calls it a one-bit projection with a single assignment site |
| input predicate | 1 | `configured_path_composed` = `priors.configured_crossover_response_by_role is not None` |
| **produced** | **20** | the rest |

25 fields, all written at a construction site, none unwritten. The four
construction sites are `analyze_program_capture:6134` (the shared `replace`),
`_analyze_check:6160`, `_analyze_measure:6450`, `_analyze_verify:7235`.

**Step 2 — group the 20 by gate.** Read each produced field's binding statement,
then the guard that can make the statement not run or return no-evidence. Fields
with identical guards merge.

### 1.3 The table — 15 units over 20 produced fields

`P` = the three-way phase dispatch (`analyze_program_capture:6114-6128`,
`ValueError` at `:6128`). Everything in the *prologue* runs on all three.

| # | Unit name | Fields | Binding site | What it computes | Gate (the input whose absence skips it) |
|---|---|---|---|---|---|
| 1 | `frame_ledger` | `frame_ledger` | `:6089` `reconcile_capture_frames` | phone-reported vs received frame accounting | ⊤ — runs on every capture. `capture_report=None` grades **not-evaluated**, not loss (`:6064-6072`) |
| 2 | `anchor` | `anchor_ambiguous` | `:6110` `_global_offset` | did the timeline arbitration have one winner (#2644) | ⊤ |
| 3 | `locations` | `locations` | `:6113` `_locate_segments` | per-segment found/offset/confidence | ⊤ |
| 4 | `ambient` | `ambient_report` | `:6145` `_ambient_from_capture` | per-band room noise floor over the 12 s window | an `ambient` segment (`program.AMBIENT_SEGMENT_ID`, `program.py:178`) |
| 5 | `pilots` | `pilots`, `linearity_ok`, `channel_map_ok`, `pilot_snr_ok` | `:6151-6158` (CHECK) / `_pilot_verdicts:5537-5543`, called `:6447`, `:7225` | per-role two-level behavioral linearity + channel map + in-band SNR | ≥1 pilot segment. A legacy program carries none and the three verdicts stay `None` (`_pilot_verdicts` docstring `:5517-5520`) |
| 6 | `gain_plan` | `gain_plan` | `:6159` `_solve_gain_plan` | per-role digital gain to hit `target_capture_dbfs` | unit 5 + unit 4. **Total today** — no pilots yields empty `gains` silently (`:5953-5956`) |
| 7 | `drift` | `drift` | `:6317` `_estimate_drift` | clock epsilon ppm + glitch bit from first-vs-last woofer sweep | a `sweep_w` segment (hard literal, `:2953-2954`). **Total today** — <2 occurrences yields `epsilon = 0.0` (`:2957-2959`) |
| 8 | `driver_responses` | `driver_responses` | `:6349` `_driver_response` ×2 + `_repeat_driver_responses` | per-role gated magnitude/complex TF, SNR block, validity floor | ≥1 per-role sweep segment |
| 9 | `alignment` | `alignment` | `:6383` `_estimate_alignment` | inter-branch polarity + delay from the two branch IRs | **both** branch sweeps + `priors.crossover_fc_hz` |
| 10 | `candidate` | `candidate`, `predicted_sum` | `:6401` `_build_candidate` (one call, two returns) | the crossover candidate and its predicted applied sum | unit 9's gate. See 1.4 |
| 11 | `summed_response` | `summed_response` | `:7030` `_driver_response("summed", …)` | gated TF of the one summed sweep | a `sweep_verify` segment |
| 12 | `summed_ripple` | `summed_ripple_db` | `:7066` `_ripple_db` | peak-to-peak ripple across the overlap band | unit 11 + `priors.crossover_fc_hz` (`:7060`) |
| 13 | `verify_tracking` | `verify_tracking`, `verify_tracking_curve` | `:7117` / `:7116` | measured-vs-predicted rms/max + the smoothed triple they reduce from | unit 12 + `priors.predicted_sum` (`:7067`) |
| 14 | `verify_absolute` | `verify_absolute` | `:7241` `_verify_absolute_result:6952` | measured summed vs the candidate's own crossover target | unit 11 + `crossover_fc_hz` + `priors.configured_crossover_response_by_role` + a trusted band |
| 15 | `capture_integrity` | `capture_integrity` | `:7232` `_verify_capture_integrity` | per-check heard / on-schedule / unclipped / frame-accounted | unit 11 + unit 1 |

**20 produced fields → 15 units.** The count is the number of distinct gates,
and it is reproducible: re-run the census script, then re-read the fifteen
guards cited above.

### 1.4 Where the grouping is a judgment call — three, all named

Each is a row the executor may split, and splitting one obliges the author to
name a bank that separates it.

1. **9 and 10 share a gate today** and merge under the strict rule. Kept apart
   because `_build_candidate` consumes unit 9's *output* (`:6401` takes
   `alignment`, which is then rewritten from the candidate at `:6412-6437`) — a
   dependency edge, not a gate difference. **A registry with no dependency edges
   should merge them into one 14-unit table.** Recommend keeping 15 and giving
   `AnalysisUnit` no `requires` field: the walker runs 9 then 10 in table order,
   which `packs.py:281` already makes load-bearing and pinned.
2. **6 and 5.** `gain_plan` never skips today; it degrades to an empty solve.
   Its own unit because its gate is a *conjunction* (pilots **and** ambient) that
   neither input's unit carries.
3. **7 and 8.** `drift` and `driver_responses` both need sweeps but different
   ones — drift needs a *repeat*, responses need a *role*. A near-field single-
   driver capture (R-3) separates them, which is exactly the bank W2-c must
   disclose against.

### 1.5 Who calls the analysis layer today — TWO production entries, not one

**Correction to §2.** §2's *"Who calls analysis today"* enumerates the wizard
path only. There is a **second production entry**, reached from a shipped
console script, and it is the one W2 most needs to know about.

`analyze_program_capture` (`jasper/audio_measurement/program_analysis.py:6054`)
takes `(program, samples, sample_rate, *, calibration, geometry, priors,
capture_report)`. §2's two corrections hold: the WAV arrives **already decoded**
as `(np.ndarray, int)`, and `capture_report` is a fourth optional input.

**Entry 1 — the wizard seam (live session).** Call at
`web/correction_crossover_v2.py:3144`, inside `_analyze` (`:3074`), the closure
`bind_production_analyze` (`:3037`) returns; bound at `:5742` in the only product
`V2FlowSeams(...)` (`:5740`). Seam type `AnalyzeCapture` — a Protocol
(`crossover_v2_flow.py:1437`, `__call__` **`:1462-1470`**; §2 cites `:1463-1471`,
one line late at HEAD), field `:1504`, required-keyword argument `:1447-1461`.
One flow call at `crossover_v2_flow.py:4207` inside `consume_capture` (`:4183`).
Debug sidecar `analysis_diagnostic_summary` (`program_analysis.py:7345`) at
`web/…:3297`. One direct layer call from the flow, `polarity_label`
(`program_analysis.py:4215`) at `crossover_v2_flow.py:8538`; everything else the
flow imports at `:237-249` is a type or a constant.

**Entry 2 — `jasper-read-distortion` (OFFLINE, over banked captures).**

```
jasper/cli/read_distortion.py:211      read_round_harmonics(bundle, --dumps, --state, …)
  → crossover_v2/harmonic_evidence.py:1057   read_round_harmonics
     → :1139 → :831                          _read_one_capture
        → :867                               analyze_program_capture(...)
        → :873                               analysis_diagnostic_summary(analysis)
```

Console script `jasper-read-distortion` (`pyproject.toml:205`). Four things about
it matter to W2, each verified at HEAD:

1. **It is already the offline analyze ruling S3 describes**, and predates the
   engine — `TuningSession.analyze` is the *second* such path, not the first.
2. **It already gates on what the bank carries.** `_read_one_capture:859-865`
   refuses a sidecar holding none of `FIDELITY_FIELDS` (`:199-204`) with an
   explicit *"nothing was validated"* reason, then compares replayed vs banked at
   `FIDELITY_TOLERANCE` (`:211`). A bank-content gate with a stated reason,
   shipped.
3. **It imports three PRIVATE analysis-layer symbols** — `_estimate_drift`,
   `_global_offset`, `_locate_segments` (`:847-849`) — exactly units 7, 2 and 3
   above. A partial unit decomposition already exists, outside the layer,
   undeclared; `scripts/harmonic-distortion-replay.py:335-344` duplicates that
   import block verbatim.
4. **It reads the ring, not the bank.** `--dumps` (`read_distortion.py:102-105`)
   is §1's sidecar family (c), behind an `ENABLED` marker, **off by default** —
   a store an ordinary household run does not write.

It calls with `MeasurementGeometry()` and `MeasurementPriors(crossover_fc_hz=fc_hz)`
(`:868-870`) — defaults for every input the wizard binding assembles. So a second
caller already proves the layer runs on defaults; what it cannot produce is units
9, 10, 13 or 14, whose gates need priors it never supplies.

### 1.5a Field consumption — §2's "18 of the 25" is understated

Counted at HEAD across `crossover_v2_flow.py`: **22 of 25** fields have a real
read there (comment/docstring mentions at `:5935`, `:6143`, `:8817` excluded).
The three absent from the flow are consumed one hop away:

| Field | Consumer |
|---|---|
| `program_id` | `crossover_v2/planning.py:320` (`analysis_json`), `:917` (`build_candidate`); `round_evidence.py:287`; `coordinator.py:252` |
| `mic_tier` | `crossover_v2/planning.py:416` (`ineligible_reason`) |
| `frame_ledger` | `web/correction_crossover_v2.py:3318`; `program_analysis.py:7558` (`analysis_diagnostic_summary`) |

**All 25 fields have a live consumer; none is dead.** A W2 PR that trims
"unconsumed" fields from a `crossover_v2_flow.py`-only scan breaks
`planning.ineligible_reason`, `planning.analysis_json` and the #2094 frame-ledger
sidecar. Do not trim fields in W2 at all — the layer ports **whole**.

Four more `crossover_v2` modules read fields directly and are therefore
downstream of any registry output shape: `capture_dispatch.py` (10 reads),
`spatial.py` (`:470`, `:472`, `:488`), `planning.py` (`:229`, `:314-322`, `:416`,
`:917`), `round_evidence.py` (`:286-287`, `:746`, `:884`).

### 1.6 The input/output shapes, and the one that does not exist

| Unit input | Where it comes from today | Shape |
|---|---|---|
| `program` | `crossover_v2_flow.program_for_phase(phase)` → `programs.program_for_phase:415` | `ExcitationProgram` — one of **four** composed objects, by identity (`:423-429`) |
| `samples`, `sample_rate` | decoded from `CaptureResult`'s WAV inside the production binding | `(np.ndarray, int)`, rate-checked equal to `program.sample_rate_hz` (`:6084-6087`) |
| `priors` | the **six-way** dispatch at `crossover_v2_flow.py:4190-4198` | `MeasurementPriors` (`program_analysis.py:1149`) |
| `geometry` | `self._geometry` | `MeasurementGeometry` (`:1091`) |
| `calibration` | resolved in the binding from the phone-reported setup/device | `CalibrationCurve \| None` |
| `capture_report` | `CaptureResult.capture_integrity` | `Mapping \| None` |

**Output** is one `ProgramAnalysis` (`:1852`) per capture. The registry's
`results` is keyed by unit name, so the wire shape a unit returns is *the fields
that unit owns*, not the whole dataclass — otherwise fifteen keys each hold the
same 33.6 MB object.

**And here is the gap W2 cannot paper over.** The engine's banked record
(`TuningSession._record`, `crossover_v2/session.py:655`, returning at `:692-708`)
carries thirteen fields and **not one of the six inputs above**. It carries no
`phase`, no `program_id`, no WAV path, no sample rate. `PlaybackOutcome`
(`crossover_v2/playback_transaction.py:106`) is two fields — `stage_reached` and
`incident` — so the play seam never hands a capture back either. `RecordStore.read`
(`session_seams.py:240`) returns that same thirteen-field mapping.

So at HEAD, `TuningSession.analyze` can reach **zero** of the fifteen units'
inputs from a banked record id. §3 of this brief is about that, and it is the
reason W2 cannot be scheduled as if it were independent of W1.

---

## 2. The registry shape

### 2.1 What `AnalyzeOutcome` must carry — and the third field it needs

`AnalyzeOutcome` (`crossover_v2/session.py:194-215`) is two fields today:
`results: Mapping[str, Any]` (`:214`) keyed by analysis name, and
`disclosures: tuple[CapabilityStub, ...]` (`:215`). Two contract lines the
registry inherits unchanged: `:202-207` and `:209-211` (hand a **copy**, not the
dict still being filled).

**Recommendation: add `skipped: tuple[AnalysisSkip, ...]`. Do not render a
skipped unit as a `CapabilityStub`.** §2's W2-c says *"render each skipped unit
as a `CapabilityStub`"*; four things at HEAD say that does not fit, and all four
are mechanical rather than stylistic:

1. **The message is hard-wired to "not implemented".** `_stub`
   (`measure_spec.py:134`) composes `f"{row.capability} not implemented; …"`
   (`:154-157`). A unit that *is* implemented and merely had no input here is not
   "not implemented" — the disclosure would say the opposite of the truth.
2. **`aborted()` raises on any code outside the table.**
   `CapabilityStub.aborted` (`:110`) does `_ROWS[self.code]` at `:121`; `_ROWS`
   (`:159`) is four rows. A hand-built skip stub is a `KeyError` waiting for the
   first spec that trips two stubs at once.
3. **`STUB_CODES` would stop being countable.** `frozenset(_STUBS)` (`:184`),
   documented at `:181-183` as existing *"so a reader can count the holes"*.
4. **The merge rule is wrong for skips.** `_merge_disclosures`
   (`session.py:95-120`) keys on `code` and **upgrades** `captured=False` to
   `True` when a later entry banked evidence (`:118-119`) — *"evidence is now
   waiting for an analysis nobody built"*. A skip has no such upgrade: a bank
   either carries the input or does not.

**Keep the two vocabularies apart, by their subject:**

| | Subject | Type | Owner |
|---|---|---|---|
| `disclosures` | the ENGINE never built this analysis | `CapabilityStub` | `measure_spec._ROWS` — closed, 4 rows |
| `skipped` | THIS BANK lacks the input this analysis needs | `AnalysisSkip` | the registry table — 15 rows |

They meet in exactly two places, and both are informative rather than awkward:
`NEAR_FIELD_SPLICE_NOT_IMPLEMENTED` (`measure_spec.py:78-79`, R-3) and
`DISTORTION_VS_LEVEL_NOT_IMPLEMENTED` (`:84-86`, R-4) each say in as many words
that the missing piece is an `analyze` consumer. Those are **table rows W2 does
not add** — units 16 and 17, owed to a later wave. They stay `CapabilityStub`s
until the row exists, and the row's arrival is what retires the stub.

`AnalysisSkip` is two fields: `name` (the unit) and `missing` (the input's
reason code). Nothing else — a message belongs to `refusal_copy`, which is
already the repo's one household-wording owner, and a second sentence-composer
here is the shape this refactor exists to remove.

**Not in conflict with §6.3's "no third `AnalyzeOutcome` field."** That ruling
(landed in #3151) refutes turning the four diagnostic emitters into `analyze`'s
journal, on the ground that doing so would mean *"invent a third `AnalyzeOutcome`
field **and** wire the conductor to the engine"* — i.e. a §6 row quietly
acquiring W5-b. This field is neither: it is W2-c's own deliverable, named in
§2's work items, carrying the not-run disclosure and nothing else. **No journal,
no conductor wiring.** If W2-c is built as a third field, §6.3's sentence should
be narrowed to *"no third field for the emitters"* in the same PR.

### 2.2 The reason vocabulary is already in the layer — generalize it

`_verify_absolute_result` (`program_analysis.py:6952`) **already implements
W2-c's whole contract** for unit 14:

- three named codes — `ABSOLUTE_NO_FC`, `ABSOLUTE_NO_TARGET`,
  `ABSOLUTE_NO_TRUSTED_BAND` (`:6947-6949`), with the comment at `:6944-6946`
  saying they are named precisely because *a screen and a gate* read them;
- returned as `{"not_evaluated": <code>}` at `:6977`, `:6980`, `:6987`, `:6990`
  — the skip carries **which** input was missing, not merely that one was;
- pinned by behavior at `tests/test_audio_measurement_program_analysis.py:7997`
  (`test_absolute_claim_records_why_it_could_not_be_evaluated`) and `:8009`.

The registry's skip vocabulary is a generalization of these three codes, not a
new idiom. Two consequences for W2-a:

- **Import the constants; never re-spell them.** Unit 14's gate returns
  `ABSOLUTE_NO_FC` / `ABSOLUTE_NO_TARGET` by importing them from
  `program_analysis`. `_verify_absolute_result` keeps its in-band return for its
  existing consumer (`round_evidence.py:884`). Two readers, one vocabulary — the
  `refusal_copy.REASON_REGISTRY` discipline (`:1307-1311`).
- **Accept the one duplication this leaves, and name it.** The gate re-tests
  conditions the function tests again internally. Removing that would mean
  editing `_verify_absolute_result`, i.e. not a wholesale port. **Recommend:
  leave it, and pin the two answers equal** — one property test asserting the
  gate's code equals the dict's `not_evaluated` for every input. That is cheaper
  than a lift and it catches the drift the lift would prevent.

Also note the shape units 6 and 7 have *instead*: `_solve_gain_plan`
(`:5935`) returns an empty solve when `pilots` is empty (`:5953-5956`), and
`_estimate_drift` (`:2928`) returns `epsilon = 0.0` when the woofer has fewer
than two occurrences (`:2957-2959`). **Neither says it could not answer.** That
is the `_crossover_region_null_registry` defect (`verification.py:2294`,
docstring `:2301-2313`) inside the layer W2 is registering — *"it did not return
'unknown,' it was never asked"* — and it is the concrete win W2-c buys.

### 2.3 Keyed how — not by `MEASURE_KINDS`

**`MEASURE_KINDS` cannot key the registry.** Four vocabularies say "which
analysis applies" at HEAD and no two agree:

| Vocabulary | Where | n | What it describes |
|---|---|---|---|
| `PROGRAM_PHASES` | `audio_measurement/program.py:153-155` | 3 | what `analyze_program_capture` dispatches on (`:6114-6128`) |
| `journey.PHASE_*` | `crossover_v2/journey.py:49-131` | 11 | the session state machine; 8 reach `program_for_phase` |
| `MEASURE_KINDS` | `crossover_v2/contracts.py:1430-1437` | 3 | capture *parameterization*; the engine record's `kind` (`session.py:695`) |
| `STIMULUS_KINDS` | `audio_measurement/program.py:162` | 3 | **what a capture actually contains** |

`MEASURE_KIND_VERIFY == PROGRAM_PHASE_VERIFY == "verify"` **by spelling only**.
There is no `check` kind, and `baseline` and `candidate` are both MEASURE-phase
— so a `kind → phase` map is 3→2 and loses CHECK entirely. §1's record table
already flags `kind` as a name collision; this is the operational cost of it.

The journey→program collapse is the same trap one level up: `program_for_phase`
(`programs.py:415-471`) answers 8 journey phases with **4 program objects** by
identity, and every cloud position gets the `verify`-phase object (`:456-459`).
`AnalyzeCapture`'s required keyword-only `phase` (`crossover_v2_flow.py:1447-1461`)
exists *because* of that collapse — #1855, 32 of 45 mislabeled sidecars. **Keep
`phase` non-defaulted**, as §2 says.

**So key the gate on CONTENT.** `results` is keyed by unit `name`; the gate reads
the program's segments and the priors:

| Unit | Gate reads |
|---|---|
| 1 `frame_ledger`, 2 `anchor`, 3 `locations` | ⊤ |
| 4 `ambient` | a segment id `AMBIENT_SEGMENT_ID` (`program.py:178`) |
| 5 `pilots` | ≥1 segment of `KIND_PILOT` |
| 6 `gain_plan` | 4 ∧ 5 |
| 7 `drift` | a segment id `sweep_w` (hard literal, `program_analysis.py:2953-2954`) |
| 8 `driver_responses` | ≥1 `KIND_SWEEP` segment carrying a `role` |
| 9 `alignment`, 10 `candidate` | `sweep_w` ∧ `sweep_t` ∧ `priors.crossover_fc_hz` |
| 11 `summed_response` | a `KIND_SUMMED_SWEEP` segment (`sweep_verify`) |
| 12 `summed_ripple` | 11 ∧ `priors.crossover_fc_hz` |
| 13 `verify_tracking` | 12 ∧ `priors.predicted_sum` |
| 14 `verify_absolute` | 11 ∧ `crossover_fc_hz` ∧ `priors.configured_crossover_response_by_role` |
| 15 `capture_integrity` | 11 ∧ 1 |

**Trap in the seed.** `KIND_SILENCE` (`program.py:158`) and `KIND_COURTESY_TONE`
(`:166`) are both deliberately **outside** `STIMULUS_KINDS`, with the courtesy
tone's exclusion comment (`:167-169`) saying it must stay invisible to the
locate and deconvolution machinery. A gate that iterates all segments rather
than `STIMULUS_KINDS` members counts a courtesy tone as content.

### 2.4 The table — `packs.py`'s shape, with one deviation

Agreeing with §2: a **module-level literal tuple** in a new `crossover_v2/`
module, walked by a function. No decorator, no entry point —
`measure_spec.py:157-158` and `refusal_copy.py:788` both argue the
one-place-to-add-a-row property explicitly, and no decorator registry exists
anywhere in `jasper/`.

| Element | Prior art | Note |
|---|---|---|
| `AnalysisUnit(name, run, gate)` frozen dataclass | `CapabilityPack` `tools/packs.py:131` | `gate` at `:147` |
| `ANALYSIS_UNITS` module-level tuple | `TOOL_PACKS` `packs.py:281` | order load-bearing (9→10, 11→12→13) and pinned, per `packs.py:14` |
| derived name set, never re-listed | `STUB_CODES` `measure_spec.py:184`; `refusal_copy.py:1307-1311` | `frozenset(u.name for u in ANALYSIS_UNITS)` |
| per-unit `try/except` isolation | `register_packs` `packs.py:436-441` | a raising unit is **failed**, not skipped — `PackOutcome`'s docstring draws that line at `:160-180` |
| one wire shape | `outcomes_to_state` `packs.py:187` | for `/state` and the evidence packet |

**The one deviation: `gate` returns a reason string, not `bool`.**
`packs.py`'s `gate: Callable[[Any], bool]` cannot carry *which* input was
missing, and W2-c requires exactly that. A bool gate plus a parallel
reason-lookup is two writers of one fact. Signature:
`gate: Callable[[AnalysisInputs], str]`, where `""` means run and any other
value is the skip code. In-layer precedent: §2.2's `_verify_absolute_result`.

**Home: an in-layer import, not a sixth seam** — agreeing with §2 and its
argument. `EngineSeams` (`session_seams.py:275`) has exactly five fields
(`:299-303`); adding `analyze` would make the registry injectable, i.e.
optional, which is the opposite of wholesale.

### 2.5 "Replace the CALLER" — four call sites, and only one flips in W2

The phrase invites the wrong reading. Naming all four, with what dies at each:

**Site A — `TuningSession.analyze` (`session.py:428`). THIS is W2's caller.**

- *Flips:* `results={}` (`:461`) → the walker over `ANALYSIS_UNITS`.
- *Dies:* the `{}` literal; the docstring's *"Today it runs nothing"* paragraph
  (`:439-443`); `AnalyzeOutcome`'s forward reference to wave 2 (`:202-207`).
- *Does NOT die:* the prior-bank merge (`:461-462`), the disclosure half, and
  the OWED note at `:453-457` about a missing "before" — that gap is §3's
  recommender work, not W2's, and deleting the note would claim a fix W2 did
  not make.
- **Semantic flip, and a trap:** `:202-207` today says read an empty `results`
  as *"nothing is wired to run yet"*. After W2 an empty `results` means **every
  gate said no**. The sentence must be rewritten in the same PR that lands the
  walker, or the contract line will say the opposite of the code.

**Site B — `bind_production_analyze._analyze`
(`web/correction_crossover_v2.py:3074-3179`). Does not flip in W2.** It is the
flow's seam and the flow lives to W5/W7. But it **owns the input assembly W2
needs**, and that is a scheduling fact, not a detail:

| Input | Assembled at |
|---|---|
| `samples`, `rate` | `:3081` `_wav_bytes_to_samples(wav)` (defined `web/…:2935`) |
| `calibration` | `:3084-3105`; `curve` at `:3099`, WARN + `meta` annotation at `:3109-3126` |
| `priors.mic_tier` | `:3141` `dataclasses.replace(priors, mic_tier=mic_tier_for_model(...))` |
| `capture_report` | `:3159` `getattr(result, "capture_integrity", None)` |

**That assembly is in `jasper/web/` and `crossover_v2/` imports nothing from
`jasper/web/`** (verified: zero hits at HEAD; the dependency runs the other way —
224 `crossover_v2` references in that one web module). W2's walker cannot import
it, and lifting it is a W1/W5 move. §3 is about what that leaves W2 able to do.

**Site C — `consume_capture` (`crossover_v2_flow.py:4183`). Does not flip in W2.**
Its two dispatches have different owners and different waves:

- the **priors** dispatch (`:4190-4198`, six-way) is analysis input assembly —
  it becomes the registry's gate input in W5;
- the **consumer** dispatch (`:4210-4227` → `_consume_check` `:4211`,
  `_consume_measure` `:4213`, `_consume_lateral_pose` `:4215`,
  `_consume_cloud_position` `:4217`, `_consume_entry_baseline` `:4225`,
  `_consume_verify` `:4227`) is **verdict and state-machine logic, not
  analysis**. §6 routes those verdict blocks (`:4627-5279`, `:7305-7924`) — and,
  per §6.3, their diagnostic emitters with them — in its **own** dissolution
  rows. A W2 PR that touches them is out of scope and will collide with §6.

**Site D — `harmonic_evidence._read_one_capture` (`:831`, call `:867`). Does not
flip in W2, and is where units 16–17 will come from.** R-4's stub
(`measure_spec.py:84-86`) says the missing piece is *"the `analyze` consumer that
turns the set into a measured floor"*. The **per-capture** half of that consumer
already exists here; what does not exist is the reduction **across ladder rungs**
(`stimulus_dbfs`). When unit 16 lands, `read_round_harmonics` becomes its
duplicate unless the same PR retires it. Record it now; do not build it in W2.

---

## 3. Dependencies — what W2 needs from W1

### 3.1 The twin does not sidestep the gap, and "wired-but-inert" is a fallback

**No, the registry cannot land against `FakeRecords` first.** `FakeRecords`
(`tests/engine_twin.py:199-234`) banks the same thirteen-field mappings the real
store will (`_mint` `:181`, `read` `:216`). It carries no capture either, so
wiring against it **reproduces** the input gap rather than deferring it.

And a walker whose gates all answer "no input" for *structural* reasons is
indistinguishable, at the wire, from a walker with an empty table. That is
precisely the ambiguity `AnalyzeOutcome:202-207` forbids — *read an empty
`results` as "nothing is wired to run yet", never as "everything ran and found
nothing"*. Shipping the inert walker ships that ambiguity into production and
then needs deleting. **No fallbacks: build new → prove → delete old in one
wave.**

### 3.2 Three things W2 needs; W1 plans one

| Need | Status in §1's work items |
|---|---|
| (a) a banked record on **every** phase | **Planned.** W1-c obligation 1 — CHECK/MEASURE/VERIFY bank no take today (§1, `:219-223`) |
| (b) a pointer **record → WAV** | **NOT PLANNED — a fourteenth field** |
| (c) enough persisted state to rebuild `program` | **NOT PLANNED** |

**(b) in detail.** The WAV path is **not derivable** from the take id:
`capture_artifact_relpath` (`bundles.py:205`) appends `uuid.uuid4().hex` at
`:219`, and its docstring `:208-212` says the caller mints the path *before* the
write precisely so the record can carry it. Today's family-(a) record does carry
it — `record["wav_path"] = wav_rel` (`web/correction_crossover_v2.py:3554`) and
`record["wav_bytes"]` (`:3555`) — but only when `wav` is bytes (`:3548`).
`TuningSession._record()` (`session.py:692-708`) carries **neither**. §1's
record-gap table compares thirteen engine fields against three builders, so it
structurally cannot see a field the engine does not have.

Do not confuse this with W1-d's `path` column: that is where the store put the
**record** (`session.py:667-670`), a different fact from where the capture went.
`crossover_v2` has no `bundle_ref`/`artifact_path` concept at all — those live
only on the legacy `measurement.py` / `commissioning_capture.py` records
(verified: zero `bundle_ref` hits under `crossover_v2/`).

**(c) in detail.** `save()`'s five keys (`session.py:501-510`) are `session_id`,
`graph_fingerprint`, `measurement_level_db`, `record_ids`, `disclosures`. No
program id, no gain plan. But the reconstruction is **already solved in-package**:
`rebuild_measure_program` (`harmonic_evidence.py:359`) rebuilds the MEASURE
program from `state["gain_plan_db"]` and `state["candidate"]["program_id"]`,
brute-forces the two never-banked parameters (session volume, courtesy prelude),
and accepts **only** on `program.program_id == want` (`:453`) — *"a
reconstruction that cannot prove itself must not be read"* (`:364-367`). Its
refusals are already `{"missing": "candidate.program_id"}` (`:411`) and
`{"missing": "gain_plan_db.woofer/tweeter"}` (`:422`) — the same structured shape
§2.2 recommends for the skip.

**So W1-b owes `wav_path`, and `save()` owes the two fields
`rebuild_measure_program` reads.** Both are small; neither is in the plan.

### 3.3 Recommended slicing — and one disagreement with §2

| # | PR | Lands | Depends on |
|---|---|---|---|
| 1 | **W2-d** — settle the count | the census script as a CI pin + widen `test_correction_boundary_ssot.py:175` to `crossover_v2` | nothing. **Schedulable first**, in parallel with all of W1 |
| 2 | **W2-a** — the table | `AnalysisUnit`, `ANALYSIS_UNITS` (15 rows), gates, `AnalysisSkip`. **No caller** | W2-d for the name set's pin |
| 3 | **W2-b + W2-c together** — the walker *and* its skip disclosure | `TuningSession.analyze` walks the table; `AnalyzeOutcome` gains `skipped` | W1-a, W1-b (incl. `wav_path`), W1-c, W4-a |

**Disagreement with §2: do not ship W2-b before W2-c.** §2 lists them as separate
items with c depending on b. A walker that skips **without saying why** is
exactly the `_crossover_region_null_registry` defect (`verification.py:2301-2313`)
the registry exists to remove — *"it did not return 'unknown,' it was never
asked."* Shipping it for one merge window ships that defect deliberately. Under
build-new → prove → delete-old-in-one-wave, the skip and its reason are **one
behavior**, so they are one PR. Combined size ~270 lines, still under the
400-line target.

W2-a is **not** a fallback and does not violate the rule: it adds a data
structure and its tests, runs nothing, and claims nothing. That is what §2's
*"No caller yet"* already specifies.

**Rejected option, recorded.** Giving `analyze` an inputs argument —
`analyze(inputs: Sequence[AnalysisInputs])` — would unblock the whole registry
against the twin today. Reject it: `analyze()` takes no arguments by contract
(`session.py:428`) because a session's verb reads its *own* bank, and a second
input path is a fallback that must later be deleted.

### 3.4 What W2 must not do

**Do not assemble analysis inputs inside `crossover_v2/`.** §2.5's Site-B table
is the one decoder + calibration resolver + `mic_tier` + `capture_report`
assembly in the tree, and it lives in `jasper/web/`. A second copy under
`crossover_v2/` is two implementations of one concern. If W2 needs it before
W5 lifts it, **lift it — do not copy it**, and lift it in W1's wave where the
store already touches those paths.

---

## 4. Slicing, bars, tiers, symbols, traps

### 4.1 Verification bar per PR

Every bar is a behavior pin — types, codes, structured fields. Never source text,
never log or error prose.

| PR | Behavior pin | Watched-fail target (mutate, watch it go red) |
|---|---|---|
| W2-d | the census script's four class counts (3 / 1 / 1 / 20) and the 25-field total, asserted from a fresh `ast.parse`; `test_correction_boundary_ssot` covers `crossover_v2` | add a `ProgramAnalysis` field without a construction-site write → the "never written" list is non-empty → red. Add a `jasper/active_speaker/crossover_v2/x.py` importing `jasper.web` → boundary test red |
| W2-a | every `name` unique; `ANALYSIS_NAMES` derived, not re-listed; **every gate total** — a property test over generated programs asserting no gate raises; unit 14's gate code `==` `_verify_absolute_result`'s `not_evaluated` for every input | re-list `ANALYSIS_NAMES` as a literal and drop a row → the derived-set pin red. Make one gate raise on an empty segment tuple → the totality property red |
| W2-b+c | a bank missing one input kind produces **N−1** results and **one** `AnalysisSkip` whose `missing` names the input; a raising unit yields `failed`, not `skipped`; `results` is a copy (mutating it does not mutate the walker's dict) | **delete the gate on one unit and watch it raise instead of skip** (§2's own mutation). Return the live dict instead of a copy → the copy pin red. Swap `skipped` for `disclosures` → the four `measure_spec` pins red |

Two bars that are **not** worth building, said so it is a decision rather than an
omission: a golden-output pin on `ProgramAnalysis` (the layer ports whole — the
existing `test_audio_measurement_program_analysis.py` suite, 75
`analyze_program_capture` call sites, already pins it), and a per-unit timing
budget (§2's journal-volume note is real but is an observability item, not a gate).

### 4.2 Tiers

All four items are **default tier — one `/code-review` pass**. None touches
`devices.volume_limit`, `set_volume_db`, the SPL stop, the XVF, secrets, DSP math
on the output path, or `deploy/install.sh`. W2 reads captures and writes a
mapping; it emits no audio and changes no graph.

One caveat: **W2-b+c changes what a session reports about its own evidence.** That
is not a safety tier, but it is the diff to read hardest — an over-eager gate
turns a real analysis into a silent skip, which is the failure mode this whole
section exists to prevent.

### 4.3 Content-grep symbols

Grep these before writing; each has an owner that must be extended or consumed,
never re-spelled.

| Symbol | Owner |
|---|---|
| `STIMULUS_KINDS`, `KIND_PILOT`, `KIND_SWEEP`, `KIND_SUMMED_SWEEP`, `KIND_SILENCE`, `KIND_COURTESY_TONE`, `AMBIENT_SEGMENT_ID` | `audio_measurement/program.py:158-178` |
| `PROGRAM_PHASES` | `audio_measurement/program.py:153` |
| `MEASURE_KINDS`, `MEASURE_REGIMES`, `POLARITY_*` | `crossover_v2/contracts.py:1430-1450` |
| `ABSOLUTE_NO_FC`, `ABSOLUTE_NO_TARGET`, `ABSOLUTE_NO_TRUSTED_BAND` | `program_analysis.py:6947-6949` |
| `CapabilityStub`, `STUB_CODES`, `stubbed_capabilities`, `_ROWS` | `crossover_v2/measure_spec.py:88-315` |
| `FIDELITY_FIELDS`, `FIDELITY_TOLERANCE`, `rebuild_measure_program` | `crossover_v2/harmonic_evidence.py:199-359` |
| `CapabilityPack`, `PackOutcome`, `TOOL_PACKS`, `register_packs`, `outcomes_to_state` | `tools/packs.py:131-437` |
| `REASON_REGISTRY` | `crossover_v2/refusal_copy.py:790` |
| `polarity_label` / `polarity_sign_of` | `program_analysis.py:4215` / `:4220` — *"the ONE spelling of the map"* |

### 4.4 Traps

1. **The empty-`results` semantic flip.** `AnalyzeOutcome:202-207` must be
   rewritten in the same PR as the walker, or the contract says the opposite of
   the code (§2.5, Site A).
2. **`phase` stays non-defaulted.** `crossover_v2_flow.py:1447-1461`. Dropping it
   reintroduces #1855 — 32 of 45 mislabeled sidecars — silently.
3. **Four vocabularies, no two equal.** `MEASURE_KIND_VERIFY` and
   `PROGRAM_PHASE_VERIFY` are both `"verify"` **by spelling only** (§2.3). A
   `kind → phase` map loses CHECK.
4. **`CapabilityStub.aborted()` raises on an unknown code.**
   `measure_spec.py:120`, `_ROWS[self.code]`. Never hand-build a stub.
5. **Do not trim "unconsumed" fields.** All 25 have a live consumer; three of them
   only outside `crossover_v2_flow.py` (§1.5a). The layer ports **whole**.
6. **`harmonic_evidence.py:847-849` imports three private analysis symbols**
   (`_estimate_drift`, `_global_offset`, `_locate_segments`), duplicated verbatim
   at `scripts/harmonic-distortion-replay.py:339-341`. Any signature change to
   units 2, 3 or 7's producers breaks a shipped console script with no type error.
7. **`polarity_label(int(cand.seed_polarity_sign))` is written twice** —
   `crossover_v2_flow.py:8538` and `crossover_v2/planning.py:337`. Not W2's to
   fix; know it before adding a third.
8. **The courtesy tone is not content.** A gate that walks all segments instead
   of `STIMULUS_KINDS` members counts it (`program.py:166-169`).
9. **`consume_capture` is not W2's caller.** Touching `:4210-4227` collides with
   W5/W7 (§2.5, Site C).
10. **The one shipped offline analyze reads a store that is off by default** —
    `--dumps` is the `ENABLED`-gated ring (§1.5). Do not model W2's store read
    on it, and do not assume a household run has anything there.
