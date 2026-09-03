# Cutover map — `jasper/active_speaker/crossover_v2_flow.py`

> **This map dies when tier 7 completes.** It is scaffolding for the
> dissolution of one file, not a description of the system. When the last
> concern below has left `crossover_v2_flow.py`, delete this document and its
> `docs/doc-map.toml` row. Never cite it as an authority for how the tuning
> flow works — [crossover-v2-engine-design.md](crossover-v2-engine-design.md)
> owns that, and [REFACTOR-CUTOVER-2026-08.md](REFACTOR-CUTOVER-2026-08.md)
> §6 owns the plan this map serves.

**STATUS:** derived at HEAD `c253c3cf1`, file = **9,228 lines**. Every line
number below is that tree. Re-derive before you cut (§8's last trap).

Scope: this file only. `jasper/web/correction_crossover_v2.py` has its own map.

---

## 0. Method, and four corrections to §6

Everything here is AST-derived, not grepped: `ast.parse` on the file for the
structure, `ast.walk` over every `.py` in the tree for importers, and a
receiver-filtered attribute scan for method reach. Prose is classified by node
span (docstrings) and first-non-space `#` (comments). That matters because §8's
wrap-safe warning is real — a single-line grep for a re-exported name misses
the wrapped half.

§6 was written as "a reliable map, not a line-by-line audit," and it holds up
at that altitude. Four things it says about this file do not survive a
line-grained re-derivation:

1. **`__all__` was 61 names, not 63; it is 53 at head.** No duplicates. #3248
   removed 8 — four with no reader of any kind, the two test-only volume
   wrappers, and two names that moved to their organ. **Zero of the 53 are
   unread**: the earlier "17 imported by nobody" counted `from ...
   crossover_v2_flow import X` only, and 13 of that 17 are live through
   `import ... as flow` plus `flow.X` attribute reads (§3 item 6).
2. **The 255–630 row says "delete. Historical-name doors."** It is not one
   thing. 133 names are bound in that range, and **50 of them are read by this
   file** — `PhaseVerdict` alone has 55 reads. Deleting the block breaks the
   module on import. The real split is three ways (§1 band C, §3 oddity 1).
3. **Two rows overlap by 45 lines.** "attempt-history serialization 1699–1923"
   and "cloud combine · candidate-build shims 1879–2153" both claim 1879–1923,
   so the table double-counts that slice.
4. **Two gaps.** 5767–5838 (`_close_measure_cloud_candidate`, 72 lines) falls
   between the "cloud group close" row and the "candidate build" row and is
   assigned to neither; 1396–1427 and 2154–2180 are likewise unassigned. The
   bands below tile 1–9,228 exactly, with no overlap and no gap.

Prose accounting also drifts: **2,106 docstring + 2,483 comment = 4,589 prose
lines, 50%** — §6 says ~2,381 docstring and 53%. The comment figure reproduces
exactly; the docstring one does not, and this map's number is the AST span.

### Disposition vocabulary

| Tag | Means |
|---|---|
| `W#-x` | absorbed by that cutover work item |
| `→ W#-x` | **destination, not the work item** — these lines eventually live where that item builds, on their own schedule, and the item's own size and tier are not theirs. `→ W2-b` names 3,734 lines of this file; `W2-b` the work item is the ~150-line walker (`cutover-briefs-w2.md` §2.5) |
| `ENGINE` | already superseded — the named engine module owns it today |
| `NO HOME 1/2/3` | needs one of §6's three rulings for this file (below) |
| `KEEPER` | survives the dissolution — the row says where it should live |
| `DIES` | deletable outright, nothing is stranded |

§6's three rulings for this file, numbered here so rows can cite them. **All
three are RULED, not open** — see
[REFACTOR-CUTOVER-2026-08.md](REFACTOR-CUTOVER-2026-08.md) §6.1/§6.2/§6.3
(owner, 2026-08-26). The bullets below point at each ruling rather than
restate it:

- **NO HOME 1** — the tuning constants (band D): ruled in §6.1.
- **NO HOME 2** — candidate build · publish · commit (band V): ruled in §6.2.
- **NO HOME 3** — the diagnostic emitters (band AC): ruled in §6.3.

---

## 1. The concern table

Bands tile 1–9,228. `code` is executable lines only (prose and blanks
excluded), and it is the number the floor math should use — see §4.

| # | Lines | n | code | Concern | Callers | Disposition |
|---|---|--:|--:|---|---|---|
| A | 1–107 | 107 | 0 | Module charter: the flow→organ direction, the two-stage journey, the tier arithmetic, the D1/D2 history | none (prose) | `DIES` — the design doc owns this |
| B | 108–253 | 146 | 120 | Imports + `TYPE_CHECKING` block (6 deferred prescription/coordinator types) | internal | `DIES` with the code that reads them |
| C | 254–630 | 377 | 165 | **Four "barrels" — but 133 names in three populations.** 23 pure re-exports with no importer anywhere; 60 pure re-exports with a live importer (26 prod, 34 test-only); **50 names this file actually reads** | 7 prod modules take the 26 door names: the web host (12), `angle_capture.py` (11), `crossover_envelope_v2.py` (4), `cli/doctor/correction.py`, `correction_crossover_v2_relay.py`, 2 scripts | **split.** 23 → `DIES` today. 60 → `W5-d` (repoint the importer at the organ, then delete the door). 50 → `KEEPER` until their reader moves; they are imports, not doors |
| D | 631–877 | 247 | **21** | Tuning constants + their justification essays. 21 lines of code carrying 185 comment lines. Also holds `CrossoverV2FlowError` (:875) — a re-export of `_contracts.CrossoverV2FlowError` and **the most-imported name in the file, 10 files, 3 of them prod** (the web host now takes it from `contracts`) | tests mostly; `severed-twin-replay.py` takes `ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS`; the error class is load-bearing | **NO HOME 1** for what remains. **PARTLY EXECUTED** — `verify_absolute_tolerance_db` moved to `crossover_v2/verification.py`, beside the claim record that is its only caller; a thin re-export stays. The three `VERIFY_*` grading constants did NOT go with it — they stay with their readers, per REFACTOR-CUTOVER-2026-08.md §6. Move `CrossoverV2FlowError`'s door first and separately — it is a barrel entry misfiled in a constants band, and it gates three prod modules |
| E | 878–1395 | 518 | 218 | Pure helpers, four clusters: alignment plausibility (:898–963), capture-integrity/SNR log fields (:970–1117), VERIFY evidence + claims + frame (:1123–1304), driver/pilot SNR fields (:1316–1393) | 4 prod-imported names: `alignment_delay_search_bounds_us` + `CLAIM_PASS`/`CLAIM_FAIL` → web host, `CLAIM_NO_PER_BRANCH_CAPTURE` → `crossover_envelope_v2.py`. The other ~19 are tests-only | **PARTLY EXECUTED** — the VERIFY evidence + claims + frame cluster and the four `CLAIM_*` codes moved whole to `crossover_v2/verification.py`, beside the verdicts they feed; thin re-exports stay for the web host and `crossover_envelope_v2.py`. The other three clusters are still `→ W2-a` + `→ W2-b` — they are analyses, and they become registry units |
| F | 1396–1427 | 32 | **0** | A pure comment block: where the Layer-1a linearization policy went (`crossover_v2.intervention`) and where the deleted level tolerance went | none | `DIES` — 32 lines, zero code, a tombstone for two completed moves |
| G | 1428–1698 | 271 | 78 | Seam protocols + snapshot: `AnalyzeCapture`, `RecordModelError`, `V2FlowSeams` (**17 fields**), `V2ConductorSnapshot` (**11 fields**) | web host binds `V2FlowSeams` at 5 sites; `V2ConductorSnapshot` + `AnalyzeCapture` prod | `W5-b` for `V2FlowSeams` → `EngineSeams` (`session_seams.py:275`). **PARTLY EXECUTED** — `V2ConductorSnapshot` moved to `crossover_v2/durable_state.py`, beside the document it is persisted into; a thin re-export stays for the web host. `V2FlowSeams` and the seam protocols are still `W5-b` |
| H | 1699–1878 | 180 | 102 | Attempt-history serialization: `attempt_history_from_state`, `attempt_record_from_verify`, two optional-coercers | `attempt_history_from_state` → web host (prod). `attempt_record_from_verify` is in `__all__` and imported by **nobody** | `W1-a` / `W1-b` (`persist`/`read_state`), **EXECUTED out of this file** — the whole band moved to `crossover_v2/durable_state.py`, beside `build_conductor_state`, which writes the `attempts_loop` key these read back; thin re-exports stay for the web host and for the flow's own call. `RecordStore` itself is still unbuilt; it lands on this shape |
| I | 1879–2006 | 128 | 62 | Cloud combine, emitting half: `combine_cloud_positions`, `cloud_geometry_verdict`, `assemble_cloud_group_result`, echo-band derivation, one diagnostic emitter | tests x4; 5 prod modules cite it in prose only | `→ W2-b` — the walker absorbs the emit |
| J | 2007–2153 | 147 | 42 | Candidate-build shims: `spec_report_for_predicted_sum`, `_commanded_delta`, `_curve_points`, plus 6 organ-alias binds | tests x4 | `→ W2-b` |
| K | 2154–2180 | 27 | 1 | `class CrossoverV2Session` header + its "what belongs on this class" docstring | — | `DIES` with the class |
| L | **2181–2957** | **777** | **229** | **`__init__`.** 229 lines of code and **546 comment lines (70% prose)** — the field-declaration essays | constructed at 9 sites; 1 prod (`correction_crossover_v2.py`) | `W5-a` then `W5-b` — dissolves into the acceptance row's four destinations. The prose is the biggest single deletion in the file |
| M | 2958–3197 | 240 | 87 | Program composition + priors per phase. Almost entirely one-line delegates: 8 calls into `_priors`/`_programs`/`_planning` | internal | `ENGINE` — `crossover_v2/programs.py` + `priors.py` already own the bodies; the parameterization is `MeasureSpec`'s |
| N | 3198–3211 | 14 | 3 | Journey delegation — one property, `return self._journey.plan.post_apply_verifies` | tests x2 | `ENGINE` — `crossover_v2/journey.py`. Deletes |
| O | **3212–3742** | **531** | **185** | **43 `@property` + 9 accessor methods.** 255 docstring lines against 185 of code | **the dominant consumer is `crossover_v2/durable_state.py` (22 of these names), not the web status block.** `build_conductor_state(conductor, …)` reads the session positionally as `Any` | **re-triage, and §6's row is wrong about who reads them.** The persistence serializer is the reader, so most of these become `RecordStore` fields under `W1-a`/`W1-b`, not `MeasureOutcome`/`AnalyzeOutcome` under `W5-b`. 11 have no caller at all (§3) |
| P | 3743–3865 | 123 | 56 | UI prompt payload: `_cloud_prompt`, `_prompt_shown_for`, `_pilot_heard_for`, `_reflection_measured_for` | internal only | `W5-b` — `MeasureSpec.pose_prompts` + the record's `prompt` |
| Q | 3866–3969 | 104 | 77 | Lifecycle + `snapshot`/`hydrate`: `note_apply_complete`, `note_restore_observed`, `_apply_observed` | `hydrate` → web host; `note_restore_observed` → `durable_state.py` | `W1-a` — this is the record store's job |
| R | **3970–4626** | **657** | **391** | **The relay callbacks.** `authorize_begin` (147), `consume_capture` (152), `_resolve_spent_slot` (152), `on_armed`, `program_for_phase`, slot/verdict/logging helpers | `authorize_begin` / `on_armed` / `consume_capture` → `correction_crossover_v2_relay.py` + `_wired.py` (prod). This is the live capture path | **`W5-b`** — this is exactly what `TuningSession.measure()` replaces |
| S | 4627–5279 | 653 | 333 | CHECK · MEASURE · lateral · cloud-position verdicts and their retention. `_measure_verdict` (173), `_cloud_position_verdict` (77), `_retain_cloud_position` (65), `_consume_lateral_pose` (78) | internal; `_run_cloud_pipeline` reached from the capture session runner | `W1-c` (two of the three retention sites: `:5056`, `:5254`) + `→ W2-b` (the verdicts) |
| T | 5280–5766 | 487 | 149 | Cloud group close + speculative close. `_close_cloud_group` is **256 lines**; `run_speculative_group_close`, `note_group_close_started`, `confirm_cloud_measure_group`, `cloud_close_state` | three public methods → web host (prod); `cloud_close_state` has no caller | `→ W2-b` + `W5-b` |
| U | **5767–5838** | 72 | 15 | `_close_measure_cloud_candidate` — **§6 assigns this to nothing.** It is the seam between the group close and the candidate build | internal (1 call site) | `→ W2-b`, by adjacency — but say so explicitly, because today it is unassigned and would be stranded between two PRs |
| V | 5839–6334 | 496 | 198 | **Candidate build · publish · commit.** `_build_measure_candidate`, `_publish_measure_candidate`, `_previous_graph_predicted_sum` (143), `commit_intervention_proposal` (103), `_commit_measure_candidate` | `commit_intervention_proposal` tests-only; the rest internal, downstream of the web host's close call | **NO HOME 2** |
| W | 6335–6728 | 394 | 139 | Findings/evidence publishing + refusal: `_publish_level_frame_finding`, `_refuse` (43), `_assert_accountable` (86), `_mic_trust_ceiling_hz` (84), `_cloud_fit_evidence` | internal | refusal → `crossover_v2/refusal_copy.py` (`ENGINE`, 1,609 lines already there); findings → `W1-a` |
| X | 6729–6913 | 185 | 90 | Cloud pipeline runner (`_run_cloud_pipeline`, 160) + its diagnostic. **No retention here** — `:6879` is a comment in a `publish_cloud` except arm, not an inlined retention copy (`cutover-briefs-w1.md` D6) | the capture session runner, web host | `→ W2-b` + `W1-c` |
| Y | 6914–7048 | 135 | 79 | Entry baseline: consume / verdict / retain / graph fingerprint | internal | `W1-b` — the 13 fields |
| Z | 7049–7304 | 256 | 109 | The round, graded: `_grade_round_once` (144), `_round_refusal_for`, `_round_ports`, `_applied_candidate_id` | internal; delegates into `crossover_v2.coordinator` at 3 sites | `→ W2-b` |
| AA | **7305–7924** | **620** | **281** | **VERIFY verdict + attempt grading.** `_grade_verify_attempt` (**270**), `_verify_verdict` (**194**), `_set_verify_outcome`, `_note_verify_mismatch`, `_note_level_reference_reset` | `_set_verify_outcome` cited by `crossover_envelope_v2.py` prose; otherwise internal | `→ W2-b` — `measure(kind=verify)` then `analyze` |
| AB | 7925–8355 | 431 | 225 | Delta probe: `_run_delta_probe` (238), `_entry_delta_db` (134), offset/transfer helpers | internal; `delta_probe` property → `durable_state.py` | **EXECUTED** — the axes, the classifier call and the journal line moved whole to `crossover_v2/delta_probe_run.py`; a short `_run_delta_probe` stays as the session's gather-and-stamp wrapper, and it is the wrapper that is `→ W2-b` |
| AC | 8356–8728 | 373 | **250** | **Four per-phase diagnostic emitters** — `_log_measure_diag` (154), `_log_verify_diag` (103), `_log_check_diag`, `_log_measure_level_solve`, `_safe_log_diag`. **They feed no verdict.** Highest code density of any prose-light band | none — internal, and no consumer reads their output | **EXECUTED** — moved whole to `crossover_v2/diagnostics.py`; three thin methods stay as the `log_diag` seam, and the **NO HOME 3** references below now read historically |
| AD | 8729–8978 | 250 | 93 | Gate / candidate / linearization helpers: `_build_candidate` (82), `_plan_linearization` (51), `_journal_linearization` (45), `_exclusion_evidence_json`, four one-line gate accessors | `_plan_linearization` cited by `crossover_v2/planning.py`; rest tests-only. ~~**`_linearization_ineligible_reason` has zero references tree-wide**~~ **DELETED** | organs (`intervention.py`, `planning.py`). The orphan is gone |
| AE | 8979–9094 | 116 | 59 | Production playback seams | — | **EXECUTED** — both moved whole to `crossover_v2/composition.py` (wave B3); callers repointed, a tombstone comment remains |
| AF | 9095–9165 | 71 | 35 | Session-volume lifecycle: `derive_session_volume_db`. (`open_measurement_volume` / `abandon_measurement_volume` were here and are deleted — no production caller; the preparer runs its own `needs_recovery` gate) | `derive_session_volume_db` → web host (prod) | **`W5-c`** — and `W4-c` for the claim. Adversarial tier |
| AG | 9166–9228 | 63 | 63 | `__all__` — **53 names** at head (was 61; #3248 pruned 8), **0 of them unread** once attribute reads are counted | — | `DIES` with the barrels |

---

## 2. Deletion order

§6's six-step order is right in shape. Two amendments, both from §0's
corrections:

**Step 0 (new, and it is free).** Four things can land today, before any tier-4
join, with zero coordination and zero stranded callers:

| What | Lines | Why it is safe |
|---|---|---|
| Band C's 23 importer-less re-exports | ~65 | **Count is from-import-only — RE-DERIVE both-forms before executing.** That instrument is what made the `__all__` row below wrong by 13; at head roughly 12 of `__all__`'s 53 names are attribute-only readers, so expect this 23 to shrink materially |
| Band F, the Layer-1a tombstone | 32 | Zero code lines |
| ~~`_linearization_ineligible_reason`~~ **EXECUTED** | 13 | Zero references anywhere, including its own class |
| ~~`__all__`'s 17 never-imported entries~~ **EXECUTED BY #3248 — and the 17 was wrong** | 4 | Only **four** had no reader of any kind (`CAPTURE_PLAN_TARGET`, `EXPRESS_CLOUD_VERIFY_POSITIONS`, `LateralPoseCurve`, `VERIFY_MARK_PROMPT`); those are deleted. The other 13 are live through `flow.X` attribute reads and must NOT be cut — see §3 item 6 |

**~127 lines** for one mechanical PR that needs no ruling and blocks nothing.
Add oddity 3 below — repointing `attempts_loop.py:853` — and the same PR kills
an import cycle for three more lines.

**Step 1 — the doors, not the barrel.** §6 says the barrels go first because
they have "zero dependencies." Half of band C *is* a dependency. The order
inside band C is:

1. Delete the 23 (step 0).
2. Repoint the 26 prod-imported doors at the owning organ, one importing module
   per PR — the web host (12 names), `angle_capture.py` (11),
   `crossover_envelope_v2.py` (4), `cli/doctor/correction.py` (1),
   `correction_crossover_v2_relay.py` (1), `scripts/render-metric-views.py` (1),
   `scripts/run-crossover-round.py` (1). Then delete those 26 doors.
3. Repoint the 34 test-only doors the same way. `test_crossover_v2_fc_candidates.py`
   and the two suites on `LINEARIZATION_MIN_PAIRED_OCCURRENCES` are named in the
   file's own comments (:374–377, :398–404) as the callers that keep those doors
   open — repoint them and the doors close.
4. **Leave the 50 read-here imports alone.** They are ordinary imports and they
   die with their readers, in the bands below.

**Steps 2–5** follow §6: the status projection (in the web file, not this one),
then `W5-a`, then the walk (`W5-b` → band R), then `W1-c` (bands S, X, Y), then
volume (`W5-c` → band AF).

**Step 6 — the shell.** Bands A, K, L's residue, O's orphaned accessors, AC (once
NO HOME 3 rules), and AG. Band U must be named in whichever PR carries band T or
V, or it strands.

**One ordering hazard worth stating.** Band O's accessors lose their reader when
`durable_state.build_conductor_state` is replaced, not when the web status block
is. Those are different work items. Do not delete an accessor because the status
block stopped reading it — grep `durable_state.py` first.

---

## 3. Load-bearing oddities

**1. The barrel is half a dependency.** Covered in §0 and §1 band C; it is the
single most consequential correction in this map. `PhaseVerdict` (55 reads),
`REASON_REGISTRY` (7), `_screen_refusal_code` (7), `_gate_window_ms` (8) and 46
more come in through lines the plan marks *delete*.

**2. There is no module-level mutable state.** None. The only lock is
`self._close_lock` (`:2769`), instance-scoped. The web file has four module-level
mutables that §6 warns "move them together or they race" — **this file has the
opposite property, and that is worth writing down**, because it means every band
here can be cut in any order without a race, and the ordering constraints are
purely about callers.

**3. An import cycle whose entire content is a barrel hop.** This file imports
`attempts_loop` at module scope (`:155`). `attempts_loop.py:853` defers an import
back to this file — inside a function, with a comment explaining it is avoiding
"a cycle" — to fetch exactly one name: `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`.
That name is a re-export of `crossover_v2.attempt_grading`'s, and
`attempt_grading.py` imports nothing but `dataclasses` and `typing`. **Repoint
that one deferred import at `attempt_grading` and the cycle disappears** — along
with the reason the deferred import exists. Three lines, and it can land in
step 0.

**4. The `CrossoverV2Session` boundary is not where the class is.** The class is
`:2154–8976`, 6,823 lines, 158 members. Its *contract* boundary is much smaller:
**63 public members, 33 with a production caller — and 18 of those 33 callers
are inside `crossover_v2/` itself** (17 in `durable_state.py`, 1 in
`capture_plan.py`), not in the web layer. The web host, its relay and its wired
variant together reach only about 13. **The session's largest production
consumer is the persistence serializer, not the host.**

11 public members have no caller at all — not production, not tests, not the
class itself:

`attempt_history`¹ · `cloud_close_state`² · `last_attempt_decision`¹ ·
`last_failure_pilot_heard` · `last_failure_rollback_anchor` ·
`lateral_mark_return_drift_db`³ · `measure_alignment_reservation` ·
`measure_ripple_reservation` · `topology_prescription_record` ·
`verify_tracking_curve` · `alignment_prescription_record`

² `cloud_close_state` **is** load-bearing after all: `snapshot()` reads it at
`:3918`. It follows band Q, not the deletion pile.

¹ **Do not delete these two on this evidence.** `V2ConductorSnapshot` has
dataclass fields of the same names, and `snapshot()` fills them from the
*private* `self._attempt_history` / `self._last_attempt_decision` (`:3919–3920`),
not from these properties. So the property is unread — but every external
mention of the name resolves to the snapshot field or to
`attempt_history_from_state`, and a name-level scan cannot separate them.
Confirm at the call site before cutting. This is the "don't trust names" trap
in its exact shape: two live things and one dead thing sharing one spelling.

³ read once by `_close_lateral_walk` (`:5092`), which is itself part of the
lateral walk paused on 2026-08-18.

The rest are read by `durable_state.py` and nothing else. **73 private members
(3,335 lines) have no external reference of any kind** — that is the shell
step 6 sweeps.

**5. `CrossoverV2FlowError` is in the wrong band.** It sits at `:875`, inside
the tuning-constants band that NO HOME 1 covers, but it is a `_contracts`
re-export with the widest reach of any name in the file. **Two counts, and say
which:** by from-import only it is **10 files** (`angle_capture.py`,
`angle_capture_spool.py`, `cli/angle_capture.py`, plus 7 test suites — the web
host was the fourth prod reader and now imports it from `contracts`); counting
`flow.X` attribute reads as well it is **15**, the extra five being
`test_angle_capture_seam`, `test_crossover_v2_admission_wiring`,
`test_crossover_v2_lateral_evidence`, `test_crossover_v2_planner_wiring` and
`test_crossover_v2_programs`. If NO HOME 1 is ruled "each constant to its consuming organ,"
this one is not a constant and does not follow that rule. Move it with band C's
doors.

**6. The `__all__` names nobody imports — count them BOTH ways.** A
`from ...crossover_v2_flow import X` scan alone finds 17; thirteen of those are
live through `import ...crossover_v2_flow as flow` plus `flow.X` attribute
reads in the suites, which a from-import scan cannot see. Counting both forms,
**four** names had no reader of any kind and were deleted with their doors:
`CAPTURE_PLAN_TARGET`, `EXPRESS_CLOUD_VERIFY_POSITIONS`, `LateralPoseCurve`,
`VERIFY_MARK_PROMPT`. Any future prune must count attribute reads too, or it
deletes live surface.

**7. The four "READ-ONLY door" comments are correct and should survive as a
rule, not as prose.** `:385–389`, `:428–432`, `:536–539` all say the same thing:
`monkeypatch.setattr(flow, name, …)` rebinds this module's name and nothing
production reads. Two suites had to be repointed for exactly that reason. When
the doors go, that hazard goes with them — but until then, do not let a
dissolution PR "fix" a test by patching the door.

**8. `__init__`'s prose is the largest single deletable object in the file.**
546 comment lines in one function. It is 12% of the whole file's comment budget
and 5.9% of the file. It dies with `W5-b`, and nothing needs to rule on it.

---

## 4. Size accounting

Lines by disposition bucket. `n` is total lines; `code` is executable lines.
The two matter differently: `n` is what the floor counts, `code` is what has to
be understood before it can move.

| Bucket | Bands | n | code | % of file |
|---|---|--:|--:|--:|
| `W5-b` — the walk, prompts, and production construction | G(seams) P R AE(bind) L | 1,809 | 758 | 19.6% |
| `→ W2-a` / `→ W2-b` — analyze registry and the walker | E I J S T U X Z AA AB AD(minus orphan) | 3,734 | 1,612 | 40.5% |
| `W1-a` / `W1-b` / `W1-c` — record store, the 13 fields, retention | G(snapshot) H Q Y W(findings) **O** | 1,228 | 547 | 13.3% |
| `W5-c` / `W4-c` — session volume and the claim | AF | 71 | 35 | 0.8% |
| `W4-a` — playback transaction binding | AE(confirm) | 45 | 22 | 0.5% |
| `W5-d` — barrel doors repointed, then deleted | C(60 names) | 170 | 74 | 1.8% |
| `ENGINE` — already superseded by a named organ | M N W(refusal) C(50 names) | 602 | 220 | 6.5% |
| **NO HOME 1** — tuning constants | D | 247 | 21 | 2.7% |
| **NO HOME 2** — candidate build / publish / commit | V | 496 | 198 | 5.4% |
| **NO HOME 3** — the four diagnostic emitters | AC | 373 | 250 | 4.0% |
| `DIES` outright — nothing stranded | A B F K AG C(23 names) AD:8866–8878 | 453 | 218 | 4.9% |
| **TOTAL** | | **9,228** | **3,955** | **100.0%** |

Verified to tile: 40 sub-bands, no band used twice, no line unassigned, both
columns summing to the file exactly.

Three splits are judgment, not measurement. **G** divides at :1608 —
`V2FlowSeams` and the seam protocols to `W5-b`, `V2ConductorSnapshot` to
`W1-a`. **AE** divides at :9023 — `confirm_graph_is_live` to `W4-a`,
`bind_program_playback_seams` to `W5-b`. **W** divides at :6522 — findings to
`W1-a`, refusal and accountability to the organs. **C** is allocated
name-proportionally over its 133 bound names (23 / 60 / 50), because its
imports are multi-name blocks that do not divide by line. Sub-band line and code
counts are exact; the allocation of C is not.

**Band O sits in the `W1` bucket, not `W5-b`.** That is this map's main
disagreement with §6, and it moves 531 lines between work items — see §1 band O
and §3 oddity 4.

### Against the plan's floor

§7 gives this file: HEAD 9,228, target ≈1,500, **remaining ≈−7,730**. That
holds, and this map says where it comes from:

- **Three rulings gate 1,116 lines (12.1%)** — NO HOME 1 + 2 + 3. Until they
  land, the reachable floor is 9,228 − 7,730 + 1,116 = **2,614**, not 1,500.
  The rulings are not paperwork; they are 14% of the target gap, and NO HOME 3
  alone is 250 lines of the densest code in the file.
- **4,589 lines (50%) are prose** and follow their code without a decision.
  The largest concentrations: `__init__`'s 546 comment lines, band O's 255
  docstring lines, band D's 185 comment lines against 21 of code, band A's 91.
- **Only 3,955 lines are code.** A 1,500-line target means keeping ~38% of the
  code and almost none of the prose, or keeping less code and some charter.
  Worth saying out loud before someone reads the target as "delete 84% of the
  logic."
- **~127 lines are free today** (§2 step 0) with no ruling and no dependency.
- **`→ W2-b` is the destination of 40.5% of the file** — 3,734 lines across
  eleven bands, the largest single obligation on this file by a factor of two.
  It is §6's dissolution work, and §8's default single review pass is the tier
  on W2-b the ~150-line **walker**, not on this. The two stopped sharing a name
  in `cutover-briefs-w2.md` §2.5; the arrow is that resolution.

The largest single band is AA (VERIFY verdict + attempt grading, 620 lines /
281 code), and the largest single member is `__init__` (777 / 229). The
densest-in-code band is AC (250 code in 373 lines) — which is also the one
§6 flags as feeding no verdict.
