# Audio commissioning roadmap

> **Status: historical.** Snapshot as of **2026-08-20**, its last substantive
> revision — not its ratification date: the roadmap was owner-ratified
> 2026-08-14 and kept taking rulings after that (its Ethos heading below is
> marked "extended 2026-08-16"). Tagged historical 2026-08-22, superseded by
> [`tuning-master-plan.md`](../tuning-master-plan.md) — "the previous
> program-wide roadmap for the identical scope" (that plan's Supersessions
> section, which states the one exception below), now the planning authority
> for the measurement/tuning program.
>
> **The one carve-out is closed (#2865).** All five rulings of the
> [Ethos](#ethos-owner-ratified-2026-08-14-extended-2026-08-16) section below —
> tinker-first, the reserved-rollback rule, "Least-bad measured, honed in
> bites", the probabilistic posture, and the investment split — moved on
> 2026-08-23 to the guiding-principle section of
> [`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md#3-the-guiding-principle--least-bad-measured-honed-in-bites),
> which is now their one home and what production code cites. This whole file,
> that section included, is preserved for primary-source archaeology — specific
> facts (sequencing, wave contents, ratification-pending lists, measurement
> snapshots) will drift over time. Read it for the narrative, not for current
> state.

The cross-subsystem working roadmap for the audio commissioning program —
the measurement substrate, crossover and linearization commissioning, and
room correction — distilled from the 2026-08-13/14 architect survey and the
validation rounds that preceded it. Owner-ratified 2026-08-14.

This doc holds **sequence and rationale**. It does not hold definitions,
architecture, or campaign state, all of which have owners:

- **D1–D8 gating decisions and the PR-G ladder** —
  [`gating-v2-plan.md`](../gating-v2-plan.md) (issue #1790)
- **Room tiers, the room-boundary ceiling, the two-instrument boundary** —
  [`room-correction-regime-plan.md`](../room-correction-regime-plan.md)
  (issue #1791)
- **Commission flow grading** —
  [`tuning-operator-runbook.md`](../tuning-operator-runbook.md); **architecture
  and file map** —
  [`crossover-v2-engine-design.md`](crossover-v2-engine-design.md)

Where this doc and one of those disagree, the owning doc wins. Items here
move to issues and PRs as they start; session-level detail (what a given
run measured, what a given agent found) lives in session artifacts under
`captures/`, not here.

Every measurement below is stamped with when it was taken. Re-probe before
acting on one: the first revision of this doc shipped a blocker whose
premise had been false for four hours.

The prior-art findings cited below come from the 2026-08-14 measurement
prior-art pass, archived at
`captures/research-2026-08-14-measurement-prior-art/` (untracked, same
convention as `captures/detector-certification-20260801`). Those reports
are sourced hypotheses, not authorities — verify a load-bearing claim
against its primary source before acting on it.

---

## Ethos (owner-ratified 2026-08-14; extended 2026-08-16)

> Live home since 2026-08-23 (#2865): the guiding-principle section of
> [`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md#3-the-guiding-principle--least-bad-measured-honed-in-bites).
> What follows is the original text, kept as archaeology.

These were ratified here as binding product principles, not preferences. They
are binding still — from their live home in the doctrine linked above, which is
where all five now live; this file is the record of where they were written.

**Tinker-first, never-nanny.** A partially-working speaker beats one aired
out by an error. The system always adopts the best configuration available
given current evidence. Imperfect-but-best-known is the bar — a defect that
degrades a claim does not withhold the tune that claim describes.

**Restore and rollback are reserved for measured regression.** Hard stops
are reserved for the safety class: driver protection, hearing safety, and
the clipping/volume ceiling. Every other defect **discloses and recommends
a next action**. It never blocks. A gate that refuses on suspicion rather
than on a measured regression is a bug against this principle.

**Least-bad measured, honed in bites.** (Owner-ratified 2026-08-16.) The
target of an intervention cycle is the least bad **measured** configuration,
not a match to the prediction — a realized result will likely never perfectly
match what the model commanded. So a series gets a few bites at that apple:
up to three rounds to hone, which the owner may extend to four (#2602 owns
the cap).

Realized-versus-predicted mismatch is a **learning signal**, never by itself a
reason to retreat. The bites exist to separate what is in our control to fix —
model and accounting defects, which get fixed — from what is not — driver and
room physics, which get commanded for the achievable instead — and then to land
somewhere better than we started.

Two decisions the machinery must not conflate. **What plays** is always the
least-bad measured configuration, so a round that measures worse than the
previous state restores that state: that restore is this principle working, not
a violation of it. **Whether the series learns and continues** has one answer
every time — it does. A worse round is a gradient sample, not a stop. Every
round, kept or restored or refused, banks its measurement into the series state
so the next bite is commanded from it. Only the round budget, the plateau, and
the safety class end a series; rollback ends one only for the safety class or
for genuine corruption — an unmeasured or integrity-lost state the model cannot
reason about.

So the rule a verdict class is held to: measured no-worse than the previous
state → keep, bank, continue; measured worse → restore the playing
configuration, bank, continue. A class that retreats from a measured-acceptable
state on realized ≠ commanded alone is a bug against this principle. What forced
that into writing is the 2026-08-16 shortfall rollback (`gain_factor` 0.664;
tracking and integration passing but the verify absolute claim failing at
−2.83 dB @ 1935 Hz on a crossover band realized +3.10 dB over commanded — a real
measured regression, so the restore was right; the bug is that no round receipt
was written on the failed verify, leaving that round's realization only in
journal events), and the probe's verdict classes are being re-audited against
it. None of this is new: it restates tinker-first, restore-reserved, and the
2026-08-15 least-bad-**measured** adoption ruling. What is new is the
explicitness, not the idea.

**Probabilistic posture, 80/20 execution.** Per-frequency graded evidence —
support counts, confidence margins, tapered authority — beats a binary
per-session verdict. The implementation is **deterministic decision tables
fed by graded evidence**, extending the envelope min-composition pattern the
code already uses. No Bayesian machinery, no inference engine.

**Investment split.** The measurement substrate gets foundation-grade
investment, because wrong measurements poison every layer downstream of
them. Intervention layers get the 80/20 lens. When those two pull against
each other, the substrate wins.

---

## Pre-run wave

Before the next jts3 hardware session. Every item here is small.

**1. Redeploy jts3 to current `main`.** Measured 2026-08-14 by a read-only
probe of `/var/lib/jasper/build.txt`: jts3 runs `7a1a84a8e` (installed
2026-08-13T21:29:48-04:00, `status=ok`, from branch
`claude/crossover-completion-review-21f4b1`). That commit sits on `main`'s
history and already carries `5c6a7cf15` (#2450, merged 21:02 EDT the same
evening), so **the Stage-1 silent-capture blocker is cleared** — the first
revision of this item claimed otherwise from a stale reading.

It is 5 commits behind `origin/main` (`c10070a64` at the time of writing):
`6370dbf1f` (U4/P7-4, drops fan-in's aloop MIRROR write), `d7204b9a0`
(U4/P7-5, doctor re-points), `0620206b2` (#2381, parked-speaker
visibility), `0214b4333` (experimental USB turntable control), and
`c10070a64` (#2245, `_finite` catches OverflowError). Two are in the
fan-in/output path this run exercises and one is on the measurement path,
so redeploy before the session.

**2. Surface the crossover-region verdict as a first-class outcome.** The
R18 absolute claim grades `[Fc/2, 2·Fc]` (`OVERLAP_OCTAVE_RATIO = 2.0`,
band derived from Fc) at 2.0 dB **for the shipped 2 kHz two-way** —
`verify_absolute_tolerance_db` derives the tolerance from `SPEC_BANDS`
rather than choosing it, so a different Fc can land a different number. A
failure there should reach the household with a recommended next action.
**No new hard gate** — per the ethos, adoption stays best-available and
benefit-gated. **No retry budget**: a deterministic blend defect is not
fixed by re-measuring the same speaker in the same room, so the retriable
`REASON_VERIFY_CROSSOVER_REGION` vocabulary is either repurposed as
proceed-with-guidance copy or retired. It is excluded from the
`verify_regressed` keep-previous path already, so nothing consumes it as a
consequence.

Companion: sharpen the benefit verdict from pooled RMS toward per-band, so
a localized crossover-region regression cannot hide inside a full-spectrum
average that improves elsewhere.

**3. Reconcile the validity-floor semantics mismatch (#2425).**
`crossover_region_band_hz` takes a parameter named `trusted_floor_hz`,
documented as the trusted floor `2.5/T` (`gating.TRUSTED_FLOOR_MULTIPLIER`),
and receives `summed.validity_floor_hz` — the nominal `1/T` floor. The
absolute band is therefore graded from a floor about 2.5× lower than the
docstrings claim, on the one path where an empty band already gates
(`ABSOLUTE_NO_TRUSTED_BAND`). Decide which floor the absolute claim should
carry, then make the parameter name, the two docstrings, and the call site
agree. #2425 also asks for a test that pins whichever floor is chosen —
the existing tests do not carry that pin, so the mismatch could recur
silently.

**4. Set the stage-2 anchor-capture retention marker for the DoD run.**
[The marker died with #3250; the store banks every accepted capture
unconditionally now.] `XOVER_CAPTURE_DUMP_ENABLED_MARKER` in the dump
directory keeps the anchor
VERIFY captures on disk. Receipts store identity only by default, so
without the marker the run's anchor curves cannot be re-graded offline once
the session closes — and re-grading is exactly what items 2 and 3 want a
real session for.

**5. Measure #2168 co-residency on a 1 GB Pi as a bounded pre-test.** A
production-shaped MEASURE-accept analysis peaks around 400–430 MB and
cannot complete under a 384 MB cgroup — measured on jts3 under the bounded
runner, and it stalls in reclaim-thrash rather than OOMing, which a
household reads as a hang mid-session. That is a cgroup result, not a
verdict on a 1 GB target: co-residency with the resident daemon set on a
literal 1 GB Pi is the unmeasured number, and it is the one to get. Decide
#2321 in the same pass: the relay journey budget is saturated at 32/32, so
the next stage-1 capture has zero retake headroom.

**6. Sweep the boost path for stale cut-only claims, before the first
hardware boost run.** [Done — #2603.] Two known sites described a cut-only
world the code left behind at PR-L5: `LinearizationFilter.gain` in
`jasper/active_speaker/linearization_fit.py` was annotated
`dB; always <= 0 (cut-only invariant)` on the field that carries boosts,
and the emitter docstring in `jasper/active_speaker/camilla_yaml.py` called
`linearization` "the per-driver cut-only EQ/shelf stage" requiring a
"non-positive `gain`" while `_validated_linearization` accepted up to
`MAX_LINEARIZATION_BOOST_DB` (12.0). **That enumeration was a hypothesis,
not a bound** — a review of the first revision of this doc found a third
cut-only sentence on the same path (`camilla_yaml.py`'s
`_validated_linearization` docstring, citing "the fit engine's own
explicit-raise cut-only invariant"). All four were trued up:
`LinearizationFilter.gain`'s comment, the `_validated_linearization`
docstring's cut-only-invariant phrase, the emitter's "non-positive `gain`"
clause, and the emitter's stage NAME — "the per-driver cut-only EQ/shelf
stage" — which is the same staleness class, a name asserting cut-only while
a 12 dB boost cap exists.

And the caveat earned its keep a second time: a mechanical re-run after those
four returned **six more**, none of them on the hand list. Also trued up —
`linearization_fit.py`'s module docstring and `fit_driver_linearization`'s own
first line (both said "cut-only" while the same docstrings described the boost
vocabulary below); this file's live-spine sibling
[`crossover-v2-engine-design.md`](crossover-v2-engine-design.md)
invariant 11, which claimed the emitter re-proves a *non-positive gain* when it
re-proves the cap; the duplicated cut-only justification in
`deploy/assets/correction/js/crossover/{chart,cloud}.js` (the per-curve
reference rule there never depended on the sign, so only the premise moved);
and a dated superseded note on
[`linearization-campaign-2026-07.md`](linearization-campaign-2026-07.md)'s S3 paragraph,
which still asks for the owner amendment that was granted 2026-07-27.

**7. Collapse the driver low-limit to one declared owner (#2603).**
[Code path delivered in this PR; jts3's own stored value has not been
re-entered — that is owner/operator work, still outstanding.] Ruled
2026-08-17 after the series-1 convergence run: a driver's bottom allowed
frequency *is* the manufacturer's minimum recommended crossover frequency
with its slope condition, entered once at component entry, and every
consumer derives from that one field. As of the ruling, jts3 carries two
values for it. #2603 owns the collapse and the companion fix to the
research prompt that produced the second one; the rule, the datasheet
evidence, and the published-field convention live in
[`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)
decisions 8–9. Small, and sequenced early for the reason recorded in
decision 8 — anything measured before it grades itself against a number
nobody stands behind.

**Sequencing note (not a pre-run item).** The same ruling set puts a second
piece of work between this wave and the next tuning series: correction
inside the crossover blend region moves from the (blind) per-driver fit to
the summed response. That contract is a design-and-build, not a small item,
and it is owned by
[`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md)
("The region-based adjustment contract", decisions 10–12) — this roadmap
carries only its position in the order: upstream truth (item 7) → the
contract → a hardware series that proves the dip moves → then the
capture-source seam (#2662), whose "pull forward if lateral aborts recur
during the series" trigger can no longer fire: the lateral walk was paused on
2026-08-18, so a series run takes no lateral captures to abort unless an
operator stages an angle walk for one (#2732). Series 1 is why: four rounds converged a trim prescription
against a crossover-region dip that a scalar trim cannot address.

---

## The DoD hardware run

What this roadmap adds is only the framing: there are five owed slices,
every one of them is evidence debt rather than implementation debt, and no
round is blocked on code. That is why the DoD run is a scheduling problem,
not an engineering one — and why the pre-run wave above is worth finishing
first.

---

## Measurement-integrity wave

Post-run. This is the deliberate deeper investment named in the ethos, and
it is largely the probabilistic posture made concrete — mostly by executing
the adopted-but-unstarted gating-v2 ladder
([`gating-v2-plan.md`](../gating-v2-plan.md)). Three pieces are this
roadmap's own rather than the plan's: item 7's prominence-margin
persistence, item 10's distance prior, and item 11's placement
recommendation.

**7. Persist the accepted gate candidate's prominence margin, and add a
`detector` provenance field.** (Small.) The prominence vote already
computes the confidence evidence and discards the margin after the
accept/reject decision; persisting it is genuinely new work. The `detector`
field is **not** new — it is PR-G3's deliverable landing early, so it must
ship with D3's three-era vocabulary (`"v1"` / `"v1-vote"` / `"v2"`, since
R9 WO-6 moved v1's decision surface without a marker) and must state the
retroactive ambiguity for records written before it rather than paper over
it. Together they turn a floor from a bare number into a claim carrying its
own confidence and provenance, which is what every graded consumer below
needs.

**8. PR-G1 as gating-v2 wrote it** (D1 masked combine + INV-2 + D8, the
consumer-contract rewrite, and the `tests/test_flat_spec_ssot.py` re-pin).
Large — an estimator and consumer-contract change, not bookkeeping; scope
it as such. Read the ladder entry for the full contents; the one thing
worth carrying here is the trap it names, because it is easy to get
backwards: **MIN_POS counts curves entering the combine, not prompted
positions.** After G1 lands, no single capture's collapsed floor can
poison a session — the #1790 aggregation defect at its root, and the
prerequisite for treating a floor as evidence rather than as a veto.

**9. PR-G2 as gating-v2 wrote it** (D2's anomaly→action policy, INV-3, and
the `suspect_near_search_start` guard). This is the ladder's mandatory
second rung, not an optional extra: the plan's acceptance bar needs its
disclosure, and D3's warrant is read off the retake rate observed after G2
ships.

**10. Distance-derived gate sanity band.** (Small; can land any time in
this wave.) Predict the plausible first-reflection window from the
mic-to-speaker distance the session already measures, optionally plus a
one-question nearest-surface prompt. Detections far outside that window are
flagged suspect and feed the masking and corroboration machinery as a
physics-based plausibility term. **Disclose, never block.** Field
precedent leads with Klippel ISC, the one shipping tool that computes the
gate for you: it derives Time Window Length automatically from two entered
distances — `In Situ Meas. Distance` (speaker-to-mic) and
`1st Reflection – Distance to Wall` — behind a Tukey α = 0.5 window, which
`10-pro-tool-gating-survey.md` calls the ARTA AN4 hand-calculation
productized. The rest of the field stops at handing you the arithmetic:
VituixCAD ships a calculator (three entered distances), Audio Precision
publishes the formula `T = (2dR − d)/c` with a stated typical 5–6 ms, and
ARTA/CLIO state expected bands
(`09-gate-placement-prior-art-verdict.md`). Not one of them inspects the
impulse response — every one trusts a tape measure, which is exactly the
disclose-not-block posture this item wants. Whether the distance prior
extends the existing `suspect_near_search_start` annotation (gating-v2
D2's cheap suspicion guard, likewise non-gating) or lands as a sibling
flag is the first implementation decision, deliberately left to the PR
that builds it.
Scope note: the owner ruling banned *assumed* room dimensions; measured
and user-entered distances are the permitted class, and are what the field
actually uses.

**11. Group-scoped placement recommendation at `Fc/2`, against the
support-derived floor.** When the group's floor sits above `Fc/2`, the
session proceeds and adopts best-available, then discloses that crossover
evidence below N Hz is thin and recommends moving the speaker away from
nearby surfaces and re-measuring. Respect the #2085 copy hazard: speak from
the measured floor and the gate window that produced it, never from
inferred geometry the system did not measure.

**12. Graded validity band (D4), disclosure-only.** Blocked behind item 3,
and pending one ratification. This is a **semantic merge, not a ratio
swap**: the shipped `2.5/T` is a binary disclosed floor that never gates,
while D4's `2/T` is the endpoint of a confidence taper. The proposal is to
keep D4's taper and put its full-authority endpoint at the shipped `2.5/T`,
so the system carries two ratios rather than three. If ratified, amend
[`gating-v2-plan.md`](../gating-v2-plan.md)'s D4 in the same change.

### Skipped, with reasons

- **D3 detector v2 stays skipped.** The prior-art pass found the field
  ships no automated first-reflection detection at all — six professional
  tools (`10-pro-tool-gating-survey.md`) and twelve consumer products, all
  using a manual cursor, geometry arithmetic, or spatial diversity instead.
  Our shipped operating point misses **both halves** of pre-registered
  criterion C1 (`P_D ≥ 0.9 at P_FA ≤ 0.05` for reflections ≥ −12 dB re
  direct, delay ≥ 1 ms, SNR ≥ 20 dB — see `criteria.md` in
  `captures/detector-certification-20260801`): measured `P_D = 0.712`
  against 0.9, and
  `P_FA = 0.268` against 0.05. The false-alarm miss is by far the wider of
  the two — more than 5× the bar, against a detection shortfall of 0.19 —
  so a D3 that improved timing without moving P_FA would address the
  smaller gap. C2 (`ToA error ≤ 0.15 ms`) is a separate criterion, not
  part of C1.

  What keeps D3 closed anyway is the ceiling argument in
  `09-gate-placement-prior-art-verdict.md`: Remaggi et al. 2017 reach
  0.14 ms RMSE with a 48-microphone array **in a treated room only**, and
  0.32 ms with 8.3% gross errors averaged across four real rooms. Comparing
  their RMSE to our P_D is **not like-for-like** — P_D folds in detection,
  not just timing — so read it as a direction, not a scoreboard: one
  browser microphone and one sweep is closer to the practical ceiling than
  0.712 makes it sound. If D3 is ever opened, `09` carries the ordering
  (split the certified metric by reflection delay first — sub-1.5 ms may be
  ill-posed — and validate against dEchorate before shipping) and
  `12-seismology-onset-picking-transfer.md` carries the method verdict:
  AIC is a refiner, never a detector, and template-subtraction of the
  measured direct sound (subtract-then-pick) beats a blind picker.
- **Corpus replay as a new false-positive-measurement project** — skipped,
  and only that. The false-positive rate is already measured by the
  detector-certification harness (12,750 positive and 6,000 negative
  trials, exact ground truth, criteria frozen before the run). The S0
  corpus replay is **not** skipped: it stays inside PR-G1 exactly as the
  plan wrote it, grading predictions 2, 3 and 4 with results recorded in
  the PR body.
- **Full cross-position τ-corroboration** — position-invariance cannot
  distinguish a speaker-borne arrival from a stable room path; rotation is
  the probe that adjudicates that. Sub-0.5 ms source arrivals are
  structurally protected already (`SEARCH_T_MIN_MS = 0.5` classifies them
  `DUT_internal_ungateable`, so they can never set the gate window).
- **Cepstral echo band and window changes** — the hazards are documented;
  changing them without a discriminating probe trades a known instrument
  for an unknown one.

---

## Intervention roadmap

After the substrate. Each item is a pointer, not a plan.

**Room-correction campaign Gate 0 (#1791).** Three inputs feed it:

- The **Tier B gated-speaker subtraction** consuming
  `jasper/correction/applied_speaker_evidence.py` — the seam shipped and
  has zero consumers, so the residual target it exists to produce is
  unbuilt.
- **MMM for the room magnitude layer.** Adopt-with-conditions is the
  *recommended ruling, pending ratification*: the regime plan's D7 records
  MMM as a possible future rather than scheduled work, so adopting it is a
  change to that decision, not an execution of it. The conditions: MMM for
  the average, discrete sweeps for the variance — chunked pseudo-positions
  must never feed the spatial-variance tiers, which read position count as
  independent evidence — and an AGC empirical check first. If ratified,
  amend the regime plan's D7 in the same change.
- **The room-layer bandwidth policy** — the regime plan's adopted D1 owns
  the numbers: Tier A up to a per-room transition, Tier B from there to a
  hard stop, nothing above it. The genuinely open remainder is
  per-strategy: `assertive` keeps a shipped upper edge that D1 never
  adjudicated, and RC3 must decide it explicitly.

**Position roles into the spatial combiner.** The `onax` / `offax` / `xovr`
roles are recorded at capture time and unread by the combiner. Consuming
them is a straight win: the combiner stops treating structurally different
positions as interchangeable samples.

Optional, in rough value order:

- **UMIK-2 direct-to-Pi capture tier** — robustness and automation, not a
  data-quality prerequisite. A UMIK in the phone or laptop already earns
  reference tier with its per-serial calibration, so this buys operator
  convenience, not better numbers.
- **Sub-sample anchor arrival estimate.**
- **Excess-group-delay and Peak-Energy-Time diagnostic overlays.**

---

## Decisions pending ratification

- **The absolute claim's floor (item 3)** — which floor
  `crossover_region_band_hz` should carry: the nominal `1/T` it receives,
  or the trusted `2.5/T` its parameter name and docstrings promise. Every
  other floor decision below reads cleaner once this one is settled.
- **D4's taper endpoint (item 12)** — put the full-authority end of the
  taper at the shipped `2.5/T` rather than introducing D4's `2/T` as a
  third ratio.
- **The level datum's single owner** — ratified in principle 2026-08-16: the
  measured summed response (deconvolved) owns the level datum outright.
  Per-driver estimates become subordinate consistency checks whose
  disagreement flags a suspect capture — retriable, never a discarded datum.
  No arbitration between voters, no exclusion cliff, no taper between the
  estimators. It supersedes the taper direction recorded on #2609.
  **What shipped is the second half only.** The single owner, the retriable
  finding, and the removal of the cliff all landed; the owner is the **raw
  per-branch trim solve**, not the summed response. The summed capture rides
  the applied incumbent graph while the per-branch sweeps ride the
  protected-neutral one, so combining them double-counts the incumbent's own
  trims — see the anchor block in
  `crossover_v2.intervention.plan_linearization`. Still pending, and tracked
  together because neither is worth anything alone:
  [#2653](https://github.com/jaspercurry/JTS/issues/2653)'s frame-coherence
  condition and the re-place-anchor lifecycle change.
- **MMM adopt-with-conditions** — changes the regime plan's D7, which
  records MMM as unscheduled.
- **#2321** — the relay journey-budget product decision. The budget is
  saturated at 32/32; the next stage-1 capture needs a ruling on what
  gives.
- **#2181 −3 dB downmix** — ruled 2026-08-13, implementation queued in the
  backlog waves with a clip-margin panel. The shipped −6.02 dB
  (`MONO_SUM_GAIN_DB = 20·log10(0.5)`) is itself a recorded decision backed
  by a containment proof: at half amplitude per source, any `|L|, |R| ≤ 1`
  sums to at most 0 dBFS, so identical L==R content lands exactly on the
  ceiling and nothing can clip under `volume_limit: 0.0`. Moving to −3 dB
  **reverses that guarantee**, which is precisely why the ruling routes
  through a clip-margin panel before implementation.

---

Last verified: 2026-08-14
