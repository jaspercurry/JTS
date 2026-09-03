# Crossover measurement v2 — the spine's verification log

> **Status: historical.** This is the per-pass verification archaeology that
> rode `HANDOFF-crossover-measurement-v2.md`'s live spine until wave 7e merged
> that spine into [`tuning-operator-runbook.md`](../tuning-operator-runbook.md),
> whose architecture, contracts-and-invariants and file-map sections then moved
> on to
> [`crossover-v2-engine-design.md`](../crossover-v2-engine-design.md) — so a
> claim annotated here now lives in one of those two files.
> Each entry records what one pass re-derived against code and — the load-bearing
> half — what it deliberately did **not**. Section names quoted below are the
> pre-merge spine's; most of the sections they annotate are in
> [`crossover-measurement-v2-campaign-record.md`](crossover-measurement-v2-campaign-record.md).
> Measured figures quoted here are pinned by the tests each entry names, or
> quoted from the issue it names. Frozen: read it for *what was warranted when*,
> never for current state.

**Scope of this verification.** #2291 Phase 5c-v rewrote the **live spine**
above and verified every
claim in it against the code at `main` — module and class names, the seam list,
the stage flags, the phase vocabulary, the four verdict functions, the file
map, and the numeric constants in "Contracts & invariants" — by reading the
named symbols, not by trusting the previous text. Hardware claims were not
re-measured: the memory figures under "Boundaries" are quoted from the
2026-08-06 jts3 measurement, not re-taken. **The campaign record was
NOT re-verified**; per the documentation paradigm, historical sections are
deliberately not kept in sync with code, and the date below is not a warranty
on any fact in them.

**Addendum, 2026-08-14 — the remote tier.** "The remote tier" section and the
`TIER_REMOTE` bullet in "What it is" were written against the code that shipped
them and verified by test: the behavioural claims (the derived walk, the
angles, the gate, the dropped
confirm tap, the disclosure, the geometry-retake refusal) are pinned by
`tests/test_crossover_v2_remote_tier.py`. No capture count is restated in this
doc, so none needs a pin. The three-gesture start and the
rejected-capture stall were re-derived during the adversarial review of
PR #2505 — the acknowledgement gate
(`acceptedAcknowledgement`, and the `refs.acknowledgement` block that holds
every `begin_capture` control disabled until the box is ticked), the Start tap
from `main.js`'s `onPlanStart`, and the stall from `main.js`'s
`advanceAfterAccepted`, which routes on the UPCOMING entry's policy only after
a capture is ACCEPTED. **The date below is deliberately NOT
bumped**: that addendum re-verified only what it added, not the rest of the
spine, and moving the date would claim a sweep that did not happen.

**Addendum, 2026-08-15 — the delta probe's band, frame, and retained curve
(#2521 / #2522).** The delta-probe paragraphs under "The delta probe verifies
the apply", the `correction_model_error` and `correction_level_shortfall` rows
in the refusal table, the "VERIFY
discloses the FRAME it compared across" section's closing note, and the stage
bridge's key list were re-derived against `delta_probe.classify_delta_probe`,
`crossover_v2_flow._run_delta_probe`, `capture_dispatch._gate_trusted_band_hz`,
and `correction_crossover_v2.persist_conductor_state` as landed. The measured
figures quoted there (the keystone's 0.575 → 0.307 octaves under a flat 1.0 dB
floor, its 0.575 → 0 under a graded-bin frame fit, and the ~36 KB retention
cost) were computed on this branch and are pinned by
`tests/test_active_speaker_delta_probe.py` and
`tests/test_crossover_v2_stage_bridge.py`; the live-session numbers (357–20,000
vs 325–22,480, `max_error_db=23.4` at 21,266 Hz, the −7.8 dB / ≈2.02 pair) are
quoted from issue #2521's diagnosis and were NOT re-measured here. The stage
bridge's key list also gained `proposal_fingerprint`, which had been live since
#2392 and unlisted — a drift found while updating the count, not a change this
work made. The adversarial review of PR #2530 then moved the frame gate ahead of
BOTH rollback doors (it had guarded only the shape one, leaving the same class
walking through the scale door) and added the frame's fitted span to the two
surfaces that report its terms; the numbers quoted for both — 203 of 4,000 swept
draws, and p95 |tilt| 10.5 dB/octave over a 10-bin quiet span — are the gate's,
re-derived on this branch before being written here. **The date below is
deliberately NOT bumped**, for the same reason as the addendum above.

**Addendum, 2026-08-15 — the residual becomes a change, and its band claim is
bounded (#2533).** The three new paragraphs under "The delta probe verifies the
apply" and the durable-summary sentence a few paragraphs below it were written
against `delta_probe.classify_delta_probe` and
`crossover_v2_flow._entry_delta_db` as landed on this branch. The cycle-4 figures
quoted there — the reported −3.342 dB decomposing as −1.660 standing anchoring,
−1.457 real measured change confined to 12–20 kHz and −0.221 declared graph move;
the quiet set's 158-of-160 bins above 12 kHz with strays at 493 Hz and 1.9 kHz —
are an OFFLINE re-derivation of session `cap_M_7TWNJJenpHAa4olM7tEA`'s retained
`verify_priors`, not a fresh hardware run. Re-grading that record through the
patched classifier reproduces the live `residual_offset_db` (−3.338 against the
persisted −3.342, the difference being the 512-point decimation of a
163,574-bin grid), removes a −1.528 anchor, and scores `quiet_probe_coverage`
**0.239** against a graded band of 539.6–9,970.6 Hz. The synthetic pins are in
`tests/test_active_speaker_delta_probe.py`, the retention/re-grade contract in
`tests/test_crossover_v2_stage_bridge.py`.

Two claims in the first draft of this addendum were wrong and are corrected
here rather than quietly rewritten. (1) The coverage ratio divided by the graded
band's WHOLE span and was justified by a log-uniform derivation — but production
grids are linear, and that form scored 0.303 for a perfect uniform sampling of a
real 357 Hz–10 kHz band, i.e. it was unclearable on any real capture at any band
width. The shipped ratio divides by the band's own interquartile span, which is
grid-invariant (1.000 co-spanning, on both grid shapes), and the cycle-4 figure
above is the new metric's; the draft's 0.079 was the old one's. (2) The
repeat-round contaminant was called "bounded"; it is not, and the caveat above
now states the fabricate and mask cases the adversarial gate constructed. Both
corrections came from that gate's review of PR #2545, and every figure in them
was measured on this branch. **The date below is deliberately NOT bumped**, for
the same reason as the two addenda above.

Addendum 2026-08-18 (session trims): the courtesy-tone section, the stage-2
tables, the tier capture totals, and the remote-tier comparison were rewritten
against the code in the same diff — the prelude now announces a SESSION
(`courtesy_prelude_for_phase`) and `DEFAULT_CLOUD_VERIFY_POSITIONS` sits at its
floor of 5. **The date below is deliberately NOT bumped**: nothing outside
those sections was re-verified.

Addendum 2026-08-19 (Fc/slope apply path): "Recommending an Fc" was rewritten
against the code in the same diff. Three claims in it had become false: the
apply is no longer gated on `fc_selection` (it derives the change from the
candidate's own preset, so the dormancy of the sweep no longer implies the
dormancy of the route); the declaration writer carries slope as well as
frequency and is now `apply_measured_crossover_geometry`; and a crossover below
the tweeter's declared protection floor is refused at the apply boundary,
before anything is written. The Undo paragraph gained the geometry the record
now carries. **The date below is deliberately NOT bumped**: nothing outside
that section was re-verified.

Addendum 2026-08-21 (CHECK channel map — the CROSS test is a ratio): the
fixed additive cross-rise bound `CHANNEL_MAP_CROSS_RISE_DB` (6.0 dB) is
retired and replaced by `CHANNEL_MAP_MIN_ISOLATION_DB` (12.0 dB) applied to
`target_rise − cross_rise`. Three sections were re-verified against the code
in the same diff and edited: the `crossover_v2_check_diag` field list (it now
publishes `channel_map_isolation_db` per role plus the bound, beside the two
raw rises), the analysis-constants paragraph, and gotcha #6 — whose "cross-band
rise <6 dB" sentence had become false. New gotcha #25 carries the hardware
table, the baseline-graph discriminator that ruled out crosstalk, and the
derivation of the bound. Amended in the same PR's gate round: the ratio is only
JUDGED above `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB` (the ungated form raised
the effective target floor by `cross_rise` and newly hard-stopped a
quiet-but-correct capture), and the claim that a swap collapses isolation to ~0
was measured false and replaced — the TARGET floor is the mis-wire catcher; the
CROSS half guards abnormal cross-band energy. **The date below is deliberately
NOT bumped**: nothing outside those sections was re-verified.

Addendum 2026-08-21 (the timeline anchor's witness score): a jts3 per-driver
MEASURE round failed 3/3 on the G2 schedule gate after re-anchoring a full
pilot spacing, because `_locate_in_window` returned only the aligner's
PEAKEDNESS margin and that margin cannot tell an empty search window from an
occupied one. Candidates are now ranked on the aligner's other score, the
correlation SIMILARITY, and `ANCHOR_DISCRIMINATION_MARGIN` (a 0.05 difference)
becomes `ANCHOR_DISCRIMINATION_RATIO` (50x); its derivation, including what the
ratio does and does not buy, lives at that constant in `program_analysis.py`
and is summarised under "Timeline anchor". Three sections were re-verified
against the code in the same diff and edited: that discussion (its
margin-does-not-separate-populations paragraph described the retired quantity),
the `event=program_analysis.anchor` field list, and both the `anchor_ambiguous`
and `channel_map_mismatch` rows of the refusal table. **The date below
is deliberately NOT bumped**: nothing outside those sections was re-verified.

2026-08-24 — the geometry ruling (fixlist T1-5/T1-6). What was re-verified
against the code in the same diff and edited, and nothing else: in the LIVE
spine, the two tier-count bullets, the stage-2 heading and index table, the
Full-vs-Remote table's stage-2 row, the `remote_cloud_verify_positions()`
paragraph (which gained the pose-set-is-a-parameter and
pose-geometry-as-fields notes), and the stage-2 wall-clock ceiling; in the
CAMPAIGN RECORD, only the sections the 2026-08-18 pass already states it maintains —
the stage-2 table, the walk enumeration, the constants list, the prompt-table
ORDER claim, the artifacts bullet, the `position_evidence_block` field list,
and the courtesy-prelude saving. **The date below is deliberately NOT bumped**:
nothing outside those was re-read.

Last verified: 2026-08-24 (#2929 — the fader-hold block was re-read against the
shipped code and CORRECTED, because #2925 had recorded the wrong mechanism for
it: item 1's mechanism and its acceptance criterion (now two lines read
together, `result=held` plus zero `result=disagreed` lines — wave 5 removed the
hold's repair write, so what used to be a repair PAIR is now a single
disagreement line before a refusal), item 4's racing-writer bound
(neither shape is bounded; the second `min` operand is `current + |depth|`),
the `measurement_volume_drift` row's closing clause, and the
capture-provenance section's retention note. Every claim in them was written
against `volume_latch.hold_fader_at`,
`camilla._duck_release_target_db`/`_graph_mutation`, and
`SessionVolumePlan.owned_measurement_volume_db_nowait` in the same diff, and
against CamillaDSP v4.1.3 at tag `05e9cfc`. **Scope: only those paragraphs**;
nothing else in the live spine was re-verified this pass. The prior pass's
reading, carried forward unchanged: 2026-08-18 — the lateral pause — the stage-1 capture flow, both
capture tables, the tier capture/duration totals, the remote wall-clock
ceiling, the fit-timing rule, and the "Recommending an Fc" section were
re-verified against the shipped `STAGE1_INCLUDES_LATERAL = False` and the
values `tier_display_info()` / `session_wall_clock_ceiling_s` actually return.
**Scope: only those sections.** The prior pass's reading, carried forward
unchanged: 2026-08-17 — series-2 D1 — the safety-axis section was rewritten
against the code in the same diff: the anchored directional findings, the two
SAFE reasons and their five surfaces, the comparability rule, what the anchored
rule cannot see, and the `safety_only` block. Two paragraphs that D1's own fix
round falsified were re-read against code and corrected in it — the seam-fence
paragraph, which had said the fence "needed no edit" and that an unanchored map
defers, and the surface count. **The date is deliberately NOT bumped**: nothing
outside the safety axis and the delta-probe section was re-verified.
Carried forward: #2600 — the "round, graded" section gained the
blend-region subsection, whose every claim was written against the code it
describes in the same diff, and the file map gained
`crossover_v2/blend_correction.py`. Nothing else in the live spine was
re-verified that pass. Carried forward: #2609/#2641/#2639 — the paragraphs that round's
change falsified were re-read against code and corrected: the headroom axis's
endings against `evaluate_iteration_headroom`, the receipt paragraph against
`coordinator._write_round_receipt` and `evaluate_round_quality`'s probe
escalation, and the review screen's decline and re-measure tier against
`_review_envelope`, `handle_v2_decline`, `_phase_from_state`, and
`prepare_v2_session`. Carried forward: #2602 — the live
spine's adoption-axis count, row count, and file-map rows re-read against
`decide_adoption`; #2611 — the delta-probe section's commanded-axis and
chained-round paragraphs re-read against `crossover_v2.commanded` and
`classify_delta_probe`; #2662 — the `driver_levels_disagree` row and the two
level-estimator event paragraphs re-read against
`intervention.plan_linearization`'s anchor block,
`compare_level_definitions`, `accountability`'s `EVENT_LEVEL_ESTIMATOR_FINDING`
payload, and the `…_linearization_giveback` emit. All three named a level-datum
owner the code does not have, through two symbols
(`summed_level_reference_db`, `trim_band_delta_db`/`core_level_delta_db`) that
do not exist repo-wide. Carried forward: #2698 — the blend-region section's
restored-graph bullet re-read against `_blend_prescription`,
`coordinator._write_round_receipt`, and `observe_restore`, and extended to name
the household-Undo door the same rule now closes. Carried forward: #2738 — the
spec-verdict consumer bullet and the cloud-`flatness` "gates?" cell were both
re-read against `_done_nudges` and the done-screen assembly, which had
falsified them (the terminal result code overrode badge and copy on every
post-R18 session); the bullet now names the cap and its one capped code, and
the table cell was found true again as written. Carried forward: the
realized-level demotion (`measurement-loop-doctrine.md` deviation (i)) — the
`driver_levels_disagree` row was re-read against `accountability`'s item 1 arm
and `refusal_copy`'s registry, found falsified in both (the code and its row
are deleted, the gate banks a finding and the round proceeds), and rewritten as
a struck RETIRED row on the `correction_not_an_improvement` pattern; the two
journalctl recipes naming `…_level_match_refused` were repointed at
`…_level_match_finding`. Only that row and those two recipes were re-derived
that pass. **Scope: only the paragraphs named above were re-verified this
pass**; the rest of the live spine carries
its 2026-08-16 reading, and the campaign record's dated narrative was NOT re-verified
and still shows the pre-#2602 five-row table, as its own status callout says
it will)
