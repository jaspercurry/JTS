# Audio commissioning roadmap (working document)

The cross-subsystem working roadmap for the audio commissioning program —
the measurement substrate, crossover and linearization commissioning, and
room correction — distilled from the 2026-08-13/14 architect survey and the
validation rounds that preceded it. Owner-ratified 2026-08-14.

This doc holds **sequence and rationale**. It does not hold definitions,
architecture, or campaign state, all of which have owners:

- **D1–D8 gating decisions and the PR-G ladder** —
  [`gating-v2-plan.md`](gating-v2-plan.md) (issue #1790)
- **Room tiers, the room-boundary ceiling, the two-instrument boundary** —
  [`room-correction-regime-plan.md`](room-correction-regime-plan.md)
  (issue #1791)
- **Campaign state, owed hardware slices, CURRENT POSITION** —
  [`HANDOFF-correction-revision-plan.md`](HANDOFF-correction-revision-plan.md)
- **Commission flow architecture, grading, file map** —
  [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md)

Where this doc and one of those disagree, the owning doc wins. Items here
move to issues and PRs as they start; session-level detail (what a given
run measured, what a given agent found) lives in session artifacts under
`captures/`, not here.

Every measurement below is stamped with when it was taken. Re-probe before
acting on one: the first revision of this doc shipped a blocker whose
premise had been false for four hours.

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

Release order is **direction-dependent**, not a constant — see
[`capture-page/README.md`](../capture-page/README.md) "Release order
(direction matters)": the narrowing side always goes last, which puts the
page first when adding a protocol and the Pi first when removing one. A
plain Pi redeploy with no capture-page or relay-Worker change has no page
or Worker component; it is one deploy.

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
`XOVER_CAPTURE_DUMP_ENABLED_MARKER` in the dump directory keeps the anchor
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
hardware boost run.** Two known sites describe a cut-only world the code
left behind at PR-L5: `LinearizationFilter.gain` in
`jasper/active_speaker/linearization_fit.py` is annotated
`dB; always <= 0 (cut-only invariant)` on the field that carries boosts,
and the emitter docstring in `jasper/active_speaker/camilla_yaml.py` calls
`linearization` "the per-driver cut-only EQ/shelf stage" requiring a
"non-positive `gain`" while `_validated_linearization` accepts up to
`MAX_LINEARIZATION_BOOST_DB` (12.0). **That enumeration is a hypothesis,
not a bound** — a review of the first revision of this doc found a third
cut-only sentence on the same path (`camilla_yaml.py`'s
`_validated_linearization` docstring, citing "the fit engine's own
explicit-raise cut-only invariant"). So the item is a mechanical sweep,
not a hand list: run `bash scripts/tense-grep.sh` plus a grep for
cut-only and non-positive-gain claims across the boost path, and fix what
it returns.

---

## The DoD hardware run

The owed slices live in
[`HANDOFF-correction-revision-plan.md`](HANDOFF-correction-revision-plan.md)'s
CURRENT POSITION block. **Read it live rather than any summary** — it is the
campaign's fastest-moving fact, it carries its own date marker, and a copy
of it here would be stale within the week.

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
([`gating-v2-plan.md`](gating-v2-plan.md)), with two additions (items 10
and 11) that are this roadmap's own rather than the plan's.

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

**8. PR-G1 as gating-v2 wrote it.** (Large — an estimator and
consumer-contract change, not bookkeeping; scope it as such.) D1
per-position per-bin masking plus `MIN_POS = max(3, ceil(C/2))` where **C
counts curves entering the combine, not prompted positions** (the plan
names that trap explicitly — a cloud carries N−1 summed curves for N
positions), plus INV-2 and D8, plus the consumer-contract rewrite in
`correction_crossover_v2.py`, plus the mandated re-pin of
`tests/test_flat_spec_ssot.py`'s incident figures with the re-pin
procedure block that file lacks. After it lands, no single capture's
collapsed floor can poison a session — the #1790 aggregation defect at its
root, and the prerequisite for treating a floor as evidence rather than as
a veto.

**9. PR-G2 as gating-v2 wrote it.** D2's anomaly→action policy plus INV-3
and the suspect-near-search-start guard: conductor policy, retake wiring,
the invariant-family short-circuit, retained-anomalous provenance
annotation, disclosure copy, `event=` logs. This is the ladder's mandatory
second rung, not an optional extra — the plan's acceptance bar needs its
disclosure, and D3's warrant is read off the retake rate observed after G2
ships.

**10. Distance-derived gate sanity band.** (Small; can land any time in
this wave.) Predict the plausible first-reflection window from the
mic-to-speaker distance the session already measures, optionally plus a
one-question nearest-surface prompt. Detections far outside that window are
flagged suspect and feed the masking and corroboration machinery as a
physics-based plausibility term. **Disclose, never block.** Field
precedent: Klippel ISC computes its time window from exactly these two
entered distances, VituixCAD ships the calculator, and AP publishes the
formula. Scope note: the owner ruling banned *assumed* room dimensions;
measured and user-entered distances are the permitted class, and are what
the field actually uses.

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
[`gating-v2-plan.md`](gating-v2-plan.md)'s D4 in the same change.

### Skipped, with reasons

- **D3 detector v2 stays skipped.** A 2026-08-14 prior-art pass found the
  field ships no automated first-reflection detection at all — six
  professional tools and twelve consumer products, all using a manual
  cursor, geometry arithmetic, or spatial diversity instead. Our shipped
  detector measured `P_D = 0.712` over the frozen criteria region against
  a pre-registered criterion of `P_D ≥ 0.9` with ToA error ≤ 0.15 ms; that
  pass's reading is that the timing accuracy sits in the band a
  48-microphone research array achieves (Remaggi et al. 2017), i.e. near
  the information limit for one channel. If D3 is ever opened: first split
  the certified metric by reflection delay (sub-1.5 ms may be ill-posed and
  the aggregate may hide a ceiling), then prefer template-subtraction of
  the measured direct sound (subtract-then-pick) over blind pickers; AIC is
  a refiner only, and matched-filter *detection* raises false positives.
  Validate any new leg against dEchorate before shipping.
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
- **The room-layer bandwidth policy**, per the regime plan's adopted D1:
  Tier A runs 20 Hz to a per-room transition `f_t` clamped to [250, 500]
  with 350 as the disclosed fallback whenever estimation is uncertain,
  Tier B runs `f_t` to a 1 kHz hard stop, and above 1 kHz the room layer
  does nothing. The genuinely open remainder is per-strategy: `assertive`
  keeps its shipped 500 Hz upper edge, which D1 never adjudicated, and
  RC3 must decide it explicitly.

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
