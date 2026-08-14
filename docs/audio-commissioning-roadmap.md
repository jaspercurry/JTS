# Audio commissioning roadmap (working document)

The cross-subsystem working roadmap for the audio commissioning program —
the measurement substrate, crossover and linearization commissioning, and
room correction — distilled from the 2026-08-13/14 architect survey and the
validation rounds that preceded it. Owner-ratified 2026-08-14.

This doc holds **sequence and rationale**. It does not hold definitions,
architecture, or campaign state, all of which have owners:

- **D1–D8 gating decisions and the PR-G ladder** —
  [`gating-v2-plan.md`](gating-v2-plan.md) (issue #1790)
- **Room tiers, the Schroeder-derived ceiling, the two-instrument
  boundary** — [`room-correction-regime-plan.md`](room-correction-regime-plan.md)
  (issue #1791)
- **Campaign state, owed hardware slices, CURRENT POSITION** —
  [`HANDOFF-correction-revision-plan.md`](HANDOFF-correction-revision-plan.md)
- **Commission flow architecture, grading, file map** —
  [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md)

Where this doc and one of those disagree, the owning doc wins. Items here
move to issues and PRs as they start; session-level detail (what a given
run measured, what a given agent found) lives in session artifacts under
`captures/`, not here.

---

## Ethos (owner-ratified 2026-08-14)

These are binding product principles for everything below, not preferences.

**Tinker-first, never-nanny.** A partially-working speaker beats one aired
out by an error. The system always adopts the best configuration available
given current evidence. Imperfect-but-best-known is the bar — a defect that
degrades a claim does not withhold the tune that claim describes.

**Restore and rollback are reserved for measured regression.** Hard stops
are reserved for the safety class: driver protection, hearing safety, and
the clipping/volume ceiling. Every other defect **discloses and recommends
a next action**. It never blocks. A gate that refuses on suspicion rather
than on a measured regression is a bug against this principle.

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

**1. Deploy jts3 to current `main`.** The installed build predates #2450,
the silent-capture fix (Stage 1 emitting a program config whose capture
defaulted to the aloop tap, so it measured silence on ring transport). That
is an explicit DoD-run blocker: a run on the installed build measures
nothing. Release order is **capture page → relay Worker → Pi**.

**2. Surface the crossover-region verdict as a first-class outcome.** The
R18 absolute claim grades `[Fc/2, 2·Fc]` at 2.0 dB (`OVERLAP_OCTAVE_RATIO`;
`verify_absolute_tolerance_db` derives the number from the spec table rather
than choosing it). A failure there should reach the household with a
recommended next action. **No new hard gate** — per the ethos, adoption
stays best-available and benefit-gated. **No retry budget**: a deterministic
blend defect is not fixed by re-measuring the same speaker in the same room,
so the retriable `REASON_VERIFY_CROSSOVER_REGION` vocabulary is either
repurposed as proceed-with-guidance copy or retired. It is excluded from the
`verify_regressed` keep-previous path already, so nothing consumes it as a
consequence.

Companion: sharpen the benefit verdict from pooled RMS toward per-band, so
a localized crossover-region regression cannot hide inside a full-spectrum
average that improves elsewhere.

**3. Reconcile the validity-floor semantics mismatch (#2425).**
`crossover_region_band_hz` takes a parameter named `trusted_floor_hz`, whose
documented convention is the trusted floor `2.5/T`
(`gating.TRUSTED_FLOOR_MULTIPLIER`), and receives `summed.validity_floor_hz`
— the nominal valid floor `1/T`. The absolute band is therefore graded about
2.5× more generously than the docstrings claim, on the one path where an
empty band already gates (`ABSOLUTE_NO_TRUSTED_BAND`). Decide which floor
the absolute claim should carry, then make the parameter name, the
docstrings, and the call site agree.

**4. Set the stage-2 anchor-capture retention marker for the DoD run.**
`XOVER_CAPTURE_DUMP_ENABLED_MARKER` in the dump directory keeps the anchor
VERIFY captures on disk. Receipts store identity only by default, so
without the marker the run's anchor curves cannot be re-graded offline once
the session closes — and re-grading is exactly what items 2 and 3 want a
real session for.

**5. Measure #2168 memory co-residency on the Pi as a bounded pre-test.** A
MEASURE-accept analysis peaks around 400–430 MB. On a 1 GB target that
fails by stalling, which a household reads as a hang mid-session rather than
as an error. Run it under `scripts/pi-run-diagnostic.sh` and get a number
before the DoD run rather than after it. Decide #2321 in the same pass: the
relay journey budget is saturated at 32/32, so the next stage-1 capture has
zero retake headroom.

**6. Fix the two boost-path docstrings before the first hardware boost
run.** Both describe a cut-only world the code left behind at PR-L5:

- `jasper/active_speaker/linearization_fit.py` — `LinearizationFilter.gain`
  is annotated `dB; always <= 0 (cut-only invariant)`, on the field that
  carries boosts. The module header is already correct (the invariant holds
  only for a vocabulary that forbids boost); the field comment is stale.
- `jasper/active_speaker/camilla_yaml.py` — the emitter docstring calls
  `linearization` "the per-driver cut-only EQ/shelf stage" and says the
  re-validation requires a "non-positive `gain`", while
  `_validated_linearization` accepts gain up to
  `MAX_LINEARIZATION_BOOST_DB` (12.0).

A stale safety-shaped comment on the exact path about to run for the first
time on hardware is the worst possible time to leave it stale.

---

## The DoD hardware run

The owed slices are enumerated in
[`HANDOFF-correction-revision-plan.md`](HANDOFF-correction-revision-plan.md)'s
CURRENT POSITION block — read that, not a copy of it. In outline: the
new-floor CHECK/MEASURE slice and the six-pose selection walk, which
together exercise R16/R17/R18 on hardware for the first time; the
first-ever hardware boost prescription (the boost gate is live, but no
boosting prescription has run on a real speaker); the #2233 `/sound/`
browser pass; and live `SetConfig`→`GetConfig`, which the flow's readback
gate exercises but which has been probe-measured on the load path only.
Every one of these is evidence debt. No round is waiting on code.

---

## Measurement-integrity wave

Post-run. This is the deliberate deeper investment named in the ethos, and
it **is** the probabilistic posture — built on the adopted-but-unexecuted
gating-v2 ladder ([`gating-v2-plan.md`](gating-v2-plan.md)), not on
something new.

**7. Persist the accepted gate candidate's prominence margin, and add a
`detector` provenance field.** (Small.) The prominence vote already computes
the confidence evidence and discards the margin after the accept/reject
decision. Persisting it, plus which detector produced the pick, turns a
floor from a bare number into a claim carrying its own confidence — which
is what every graded consumer below needs from it.

**8. PR-G1 as gating-v2 wrote it: D1 per-position masked combine, plus
INV-2 and D8.** (Large — an estimator and consumer-contract change, not
bookkeeping; scope it as such.) After it lands, no single capture's
collapsed floor can poison a session. That is the #1790 aggregation defect
at its root, and it is the prerequisite for treating a floor as evidence
rather than as a veto.

**9. Group-scoped placement recommendation at `Fc/2`, against the
support-derived floor.** When the group's floor sits above `Fc/2`, the
session proceeds and adopts best-available, then discloses that crossover
evidence below N Hz is thin and recommends moving the speaker away from
nearby surfaces and re-measuring. Respect the #2085 copy hazard: speak from
the measured floor and the gate window that produced it, never from inferred
geometry the system did not measure.

**10. D4 graded validity disclosure.** Blocked behind item 3, and pending
one ratification: use the shipped, evidenced `2.5/T` trusted ratio rather
than the plan's `2/T` taper endpoint. If ratified, amend
[`gating-v2-plan.md`](gating-v2-plan.md)'s D4 in the same change so the plan
and the code state one ratio between them.

### Skipped, with reasons

- **D3 detector v2** — its own warrant is pre-registered as measured, not
  assumed, and a prior-art research pass is in flight. Executing it ahead of
  that warrant inverts the plan's own gate.
- **Full cross-position τ-corroboration** — position-invariance cannot
  distinguish a speaker-borne feature from a stable room path; rotation is
  the probe that adjudicates that. Sub-0.5 ms features are structurally
  protected already.
- **Corpus replay as false-positive measurement** — already measured at
  12,750-trial scale against exact ground truth. Re-running it measures the
  harness.
- **Cepstral echo band and window changes** — the hazards are documented;
  changing them without a discriminating probe trades a known instrument for
  an unknown one.

---

## Intervention roadmap

After the substrate. Each item is a pointer, not a plan.

**Room-correction campaign Gate 0 (#1791).** Three inputs feed it:

- The **Tier B gated-speaker subtraction** consuming
  `jasper/correction/applied_speaker_evidence.py` — the seam shipped and has
  zero consumers, so the residual target it exists to produce is unbuilt.
- The **MMM decision**: adopt for the room magnitude layer, with conditions.
  MMM for the average, discrete sweeps for the variance — chunked
  pseudo-positions must never feed the spatial-variance tiers, which read
  position count as independent evidence. An AGC empirical check comes
  first.
- The **above-350 Hz policy**, per the regime plan's per-strategy treatment
  of that boundary.

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

- **D4 taper ratio** — `2.5/T` (recommended: it is what the code ships and
  what the gate evidence supports) versus the plan's `2/T`. Blocks item 10.
- **#2321** — the relay journey-budget product decision. The budget is
  saturated at 32/32; the next stage-1 capture needs a ruling on what gives.
- **#2181 −3 dB downmix** — ruled 2026-08-13, implementation queued in the
  backlog waves with a clip-margin panel. The code ships −6.02 dB: the
  ruling and the runtime disagree, and closing that gap is the queued work.

---

Last verified: 2026-08-14
