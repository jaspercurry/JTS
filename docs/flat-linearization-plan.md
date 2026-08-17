# Flat linearization — measurement basis, spec, and closed loop (plan)

> **Status: adopted plan, pre-implementation.** Owner-approved direction
> 2026-07-25 after (a) offline comb forensics on the 2026-07-24/25 JTS3
> session WAVs and (b) an owner-run deep-research pass on industry practice.
> This doc is the execution plan for making the speaker layer's measured
> summed response *actually* flat — it changes the measurement **instrument**
> and adds a closed loop; the layer architecture itself is
> [active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md)
> (unchanged, still canonical). Shipped-flow operational truth stays in
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).
> **Amended 2026-07-25** after the executed S0 studio session: the "S0
> executed — results and the attribution correction" section below
> supersedes the original comb-attribution evidence in "Evidence — why the
> instrument must change" § 1.
> **Next-campaign note (2026-08-04; not shipped):**
> [crossover-linearization-80-20-plan.md](crossover-linearization-80-20-plan.md)
> preserves this plan's gated/spatial honesty research but gives the anchor,
> lateral samples, candidate solver, and later Room cloud separate jobs. It
> wins for future implementation sequencing; this document remains the
> research/spec authority.

## Mission

Linearize any measured hardware (1-way, 2-way, 3-way) so the measured
spatially-averaged direct sound — and the reality it represents — is flat
within a declared tolerance, using the household's own mics, with no
acoustic-treatment steps in the UX. Measurement decides; the owner's ear is
an acceptance test, never the sizing instrument.

## The spec — what "flat" means here

The observable is the **spatially-averaged gated direct sound**: N gated
sweeps captured at mic positions spread over a small cloud around the
listening axis at ~1 m, each reflection-gated as today (~7 ms in the JTS3
room), combined as a **power average** (CTA-2034 Listening-Window-inspired;
honestly named a capture cloud, not certified LW angles). Pass/fail is
evaluated at 1/3-oct smoothing (1/6-oct retained for diagnostics),
**relative to the power mean over 250 Hz–8 kHz** computed at the same
smoothing over non-excluded bins — the reference deliberately comes from
the tight-tolerance bands so a top-octave deficit cannot re-center the
target — excluding interference-flagged bins (below) from both the
reference and the deviation metric:

| Band | Tolerance |
|---|---|
| ~250 Hz – 2 kHz | ±1.5 dB |
| 2 – 8 kHz | ±2.0 dB |
| 8 – 16 kHz | ±2.5 dB |
| > 16 kHz | best-effort, disclosed, never specced |

> **The lower edges above are nominal, and the graded ones are their
> intersection with the session's own trusted floor (#2551).** 250 Hz is a
> room-agnostic constant; a gated capture is trustworthy only above `2.5/T`
> for its own reflection-free window `T` (~357 Hz at the JTS3 room's 7 ms).
> `evaluate_flat_spec` raises every band's lower edge — and the reference
> band's — to `max(f_lo, 2.5/T)`, publishing the edge it graded from as each
> band's `graded_lo_hz`, and flagging `max_at_graded_edge` when the band was
> cut by the floor **and** the reported extremum is its lowest graded bin
> (#2599) — two conjuncts, no slope test; "the band may still be rising below
> the floor" is the reading that flag licenses, not something the code
> measures. A band left entirely below the floor is **unevaluable, never
> failed**. This is an instrument-honesty layer on the
> table, not a revision of it: the tolerances and the nominal edges are
> unchanged, and grading 250 Hz at the code's own 2.5-cycle rule would need
> `T ≥ 10 ms`, which the JTS3 room's ~7 ms reflection-free ceiling does not
> allow. Clamping is not free — it re-centres the reference and so moves the
> headline, on the S0 corpus by +0.0611 dB in the flattering direction — and
> that cost is pinned in `tests/test_flat_spec_ssot.py` rather than left to
> be discovered.

> **8–16 kHz row, amended 2026-07-25:** S0 showed this ±2.5 dB tolerance
> is not achievable by EQ against the 5.4–7.0 dB source-fixed comb that
> sits inside the band ("S0 executed" § e.2). Per this section's own rule
> below ("If S0 contradicts a tolerance, the table is revised with the
> S0 data attached"), the revision is **in progress, not resolved**: the
> S0 data is attached (§ e.2) and the table number is deliberately left
> as-is pending the owner's carve-out-vs-source-fix decision (open
> question 8) rather than silently edited here.
>
> **Resolved 2026-07-25 by owner decision 1 — the carve-out is adopted**
> (recorded here 2026-07-27; the "in progress, not resolved" paragraph
> above is superseded by this one, kept per the documentation paradigm's
> annotate-don't-delete rule). Identified interference nulls — the ones
> the null-ID gate attributes to a measured arrival, with τ and r recorded
> — are excluded from spec evaluation **and** from correction. The ±2.5 dB
> tolerance applies to the **surviving envelope** around them, and the
> report discloses the carved-out ranges with their numbers ("EQ cannot
> fill these"). **The table number above is not changed**: the carve-out
> is disclosed, not re-specified. A source fix (horn redesign) proceeds in
> parallel as the owner's hardware project and is not a precondition for
> anything here. The same decision resolves open question 8; its product
> mechanics are the work order's PR-6
> ([`flat-linearization-productization-plan.md`](flat-linearization-productization-plan.md)),
> shipped as the per-band carve-out disclosure in PR-6b.
>
> **New-horn pointer (2026-07-27, exploratory) — read as history, not a
> contradiction.** The owner's new horn, measured on JTS3, removed the deep
> 8–16 kHz comb this section documents: the first real cloud session under
> the flow-simplification instrument (session `cap_4NUGqx3yIzSuv4ta2ozfKw`,
> bundle `d5b171fa81a5`) read every null depth ≤ 1.6 dB — below the 2.5 dB
> materiality floor, registry honestly empty — against this section's
> 5.4–7.0 dB source-fixed figure above. That session ran with an
> uncalibrated echo-analysis band (issue #1763, fixed by PR #1764 before
> the flow-simplification ladder), so the 1.6 dB figure is **exploratory,
> pending the confirmatory calibrated session** — and it is a
> **single-speaker hardware fact about the new horn**, not a revision of
> this section's 5.4–7.0 dB figure for the old one. The comb, the carve-out
> policy, and the ±2.5 dB 8–16 kHz row above remain canon for any speaker
> whose horn or baffle still combs. See
> [`flat-linearization-flow-simplification-plan.md`](flat-linearization-flow-simplification-plan.md)
> §0 for the full context — including why a comb-free source is the
> premise that makes a smaller Express cloud honest, which does not touch
> this doc's spec or instrument.

Rationale, briefly: run 7's single-point 2–7 kHz spread at 1/3-oct was
2.6 dB total (fits ±1.5 only about a band-local center; 3.3 dB at 1/6-oct)
— close enough that ±1.5 mid-band is plausible once the cloud removes
bounce ripple, but it is **S0-contingent, not demonstrated**, and below
2 kHz it is purely the cloud-removes-the-bounce hypothesis (run 7 read
−3.1/+5.7 dB there against this reference — that band is where the bounce
lives). **Superseded (2026-07-25):** the ~320 µs arrival is only
resolvable in the 5–19 kHz tweeter band ("S0 executed" § f); the sub-2 kHz
hump's attribution to it has reverted to baffle-step/room pending the
near-field instrument, so "that band is where the bounce lives" is the
pre-S0 hypothesis, not current truth. Above 8 kHz, UMIK-2-class unit
uncertainty is ~3 dB, so a tighter
spec there would live inside the instrument's error bars. The ~250 Hz
lower edge sits above the 7 ms gate's ~143 Hz validity floor and is
provisional (250 vs 300 Hz is settled by the validation session's LF
spread data). Below the lower edge is Layers 2–3 territory (bass program
+ room correction, in-room instruments). The 8–16 kHz tolerance may
tighten to ±2.0 after the loop demonstrates margin, never tighter than
mic uncertainty. If S0 contradicts a tolerance, the table is revised with
the S0 data attached — the spec serves the measurement, not the reverse.

## S0 executed — results and the attribution correction (2026-07-25)

> **This section supersedes the comb-attribution conclusion in "Evidence —
> why the instrument must change" § 1 below.** That section is kept in
> place per this repo's documentation paradigm (superseded claims are
> annotated, not silently deleted) — read this section first for current
> truth, then Evidence § 1 for how the original (wrong) attribution was
> reasoned, with inline corrections.

S0 ran 2026-07-25 evening on JTS3: two pre-registered legs — main (leg A,
10-position desk cloud) and ground plane (leg B, 3 positions, speaker on
the hard floor) — plus the owner's **improvised** desk-edge experiment
(3 positions, speaker moved to the desk front edge), added on the night
and not part of either leg's pre-registered prediction set. The desk edge
nonetheless produced the night's best desk top-octave reference
(−6.94 dB — § d). Every number below is quoted from the five primary
forensics documents — the only sources used for this section — preserved
at `captures/flat-linearization-20260725/s0-analysis/REPORT.md`,
`.../horn-cad/GEOMETRY.md`, `.../loopback/LOOPBACK-REPORT.md`, and the two
pre-registered graders, `s0-session-main/analysis/SCORECARD.md` and
`s0-session-groundplane/analysis/SCORECARD.md` (full corpus in § g below).

### a. The attribution correction

**The ~320 µs echo is the horn/cabinet's own rim-region reflection — not a
mic-side boundary bounce.** The mouth-radius transit (320.70 µs) is the
best-matching of 15 candidate CAD paths, but horn-vs-baffle/cabinet is not
fully separated (item 2 below). Evidence § 1 below got the *mechanism and
numbers* right (a discrete ~0.31 ms arrival, r ≈ 0.36–0.4, that combs the
8–16 kHz band) but the *attribution* wrong. Three independent lines
converge on the correction:

1. **Acoustics — source-fixed across every geometry tried.** The arrival is
   present at −7.05…−10.43 dB re direct on all 16 positions across all
   three sessions. It barely moves across the 10-position mic cloud
   (median pairwise τ shift **1.6 %**, the kit's own `geometry_locked`
   classification), survives moving the *speaker* to the desk front edge
   (τ 319.2–321.4 µs, level unchanged), and survives removing the desk
   entirely onto a hard floor (present on all 3 floor positions). A
   mic-side or desk-side boundary reflection cannot do any of that — it
   travels with the speaker, not the mic or the furniture.
2. **Geometry — the mouth-radius transit is the best-matching of 15
   candidate CAD paths, though horn-vs-baffle/cabinet is not fully
   separated.** The horn's B-Rep mouth radius (outermost lip, confirmed by
   a real tight-bbox B-Rep evaluation, not raw STEP points or a naive
   bounding box) is exactly **110.000 mm**, giving a one-way transit
   r/c = **320.70 µs** — within **0.30 µs (0.09 %)** of the measured
   median **321.0 µs**, the tightest of all 15 CAD paths tried (mouth
   diameter; axial one-way and round-trip across four depth definitions;
   wall-arc-minus-axial; throat-to-rim hypotenuse). The next-closest
   candidate, the inner-wall rim-opening radius (109.686 mm → 319.79 µs),
   misses by a looser **~1.2 µs (~0.4 %)** — that looser bound, not the
   0.30 µs one, is what "within 1.3 µs (0.4 %)" in the source CAD report
   describes when it speaks of the mouth-radius definitions collectively.
   **Two caveats, not yet closed:** (i) the most generous axial-depth
   definition (register-back to the curled lip's deepest point) is
   **109.2 mm** — numerically close enough to the mouth radius that this
   horn alone cannot cleanly separate "radius" from "depth" as the driving
   dimension, and a geometrically-similar rescale (the planned
   200/180/160 mm mouth experiments) can't disambiguate it either, since
   similarity preserves the ratio; (ii) the CAD study is horn-only — it
   did not model the baffle or cabinet — so it cannot positively rule out
   a similar-path reflection off another fixed-enclosure edge, and
   nothing tonight isolates the horn acoustically either (item 1's
   speaker-move evidence and item 3's electrical exclusion both bound the
   defect to "somewhere on the fixed speaker," not to the horn
   specifically). A **non-similar** horn variant on the same cabinet —
   same mouth, different depth, or vice versa — is the discriminator for
   both the radius-vs-depth and the horn-vs-cabinet question.
3. **Electrical exclusion — the live DSP graph is not the source.** A
   file-in/file-out loopback of the exact live CamillaDSP graph (confirmed
   live by a **read-only websocket `GetConfigJson` query**, not just the
   statefile pointer) contributes an echo of **r ≈ 0.021** at the same
   ~300 µs mark, against the acoustic **r ≈ 0.34–0.40** on the same graph
   and program — about **0.3 %** of the echo's energy
   ((0.02/0.37)² ≈ 0.003). Three independent stimuli (impulse, the exact
   verify sweep, MLS) agree to 0.04 dB; no audio was played, every run was
   file-in/file-out with no ALSA device opened. The electrical hypothesis
   is dead — **though not by the pre-registered rule's letter, an honesty
   disclosure worth carrying forward.** The rule was ≥−20 dB ⇒ electrical
   cause found, no arrival above −40 dB ⇒ hypothesis dead, in between ⇒
   report the numbers; the raw **−22.3 dB** electrical read technically
   lands "in between." But the **−40 dB threshold was unreachable by this
   instrument** — a bandlimited analytic envelope of a provably echo-free
   signal floors at **−24.1 dB** in this exact window, so no measurement
   could ever have cleared it (a flaw in the pre-registered threshold, not
   a hedge on the result). Read against that calibrated floor instead —
   −22.3 dB is only 1.8 dB above it, reproduced to 0.04 dB by three
   stimuli, against the loopback's own acoustic control read
   (−8.3/−8.5/−7.4 dB on cloud_01/cloud_03/gp_01, run through the
   identical envelope metric) **13.8–14.9 dB higher** — the verdict is
   unambiguous.

### b. The two-mechanism verdict (the owner's driver-isolation razor, vindicated)

The 1.8 kHz woofer-band dip and the 8–16 kHz comb are **not the same
mechanism** — confirming the reasoning that an isolated horn can carry a
comb the woofer's enclosure never sees:

- **The 1.8 kHz dip is position-*dependent*:** depth goes **10.7 dB**
  (desk, tweeter height, n=6) → **4.1 dB** (same desk, mic a hand-width
  low, n=4) → **1.7 dB** (ground plane, n=3). It is uncorrelated with the
  HF null depth (Pearson **r = −0.05**, leg A, n=13). It is also
  **physically impossible** for the measured ~320 µs arrival to be causing
  it: that arrival's r ≈ 0.37 caps any null it can cut at **6.81 dB**; the
  measured 1.8 kHz dip is **10.71 dB** (up to 14.6). A desk-surface bounce
  for this dip is separately refuted **by magnitude, not direction**: the
  measured ratio (275.7 → 253.3 µs, **0.919**, on a mic drop) is the
  direction a desk bounce predicts, but the magnitude demands a mic
  **62–123 cm above the desk** for a plausible 5–10 cm drop — physically
  implausible at this rig — and forward-checking the plausible 21.7 cm
  geometry predicts nulls at 2356/2870/3359 Hz vs. the measured
  **1974 Hz** (19–70 % error). Discriminator that would settle
  it outright: a per-driver `MEASURE` sweep (`sweep_w` / `sweep_t`) at the
  same positions — a crossover summation null cannot exist in either
  driver alone; tonight's three sessions are VERIFY (summed) only.
- **The 8–16 kHz comb IS the rim wave**, confirmed by two independent
  instruments agreeing to within **0.03** in implied r: time-domain
  r = **0.373** (desk, n=6) vs. frequency-domain r = **0.342** from the
  measured 6.19 dB HF null depth.

### c. Prediction scorecard

- **Predictions 1, 2, and 4 (main leg) all graded FAIL** by the kit's own
  pre-registered scorer (`s0-session-main/analysis/SCORECARD.md`) — not
  the "held" verdict an earlier pass over this data gave prediction 2. All
  three were built on the assumption that the ~320 µs arrival was a
  mic/boundary-side effect that spatial diversity would shift or reveal;
  the **geometry-locked** result falsifies that shared assumption, but a
  falsified assumption does not make a graded prediction PASS.
  - **1 FAIL:** required ≥8 % median pairwise null-frequency shift;
    measured **1.6 %** (`geometry_locked = true` overrides the pairwise
    numbers per the grader's own `verdict_reason`).
  - **2 FAIL — corrected from an earlier "held."** 42 of 210 six-of-ten
    subsets breach the ±1 dB, 300 Hz–8 kHz bar
    (`worst_case_max_deviation_db` **2.634 dB** masked, **2.826 dB**
    unmasked); the worst-case subset is `cloud_01…06` — exactly the six
    tweeter-height positions. Most consistent explanation, from `findings.json`'s
    `group_stats`: the **position-dependent** 1.8 kHz lobing dip
    destabilizes the in-band (300 Hz–8 kHz) cloud — **10.71 dB
    [6.61–14.61]** on 01–06 vs. **4.08 dB [4.01–4.59]** on 07–10 — not
    the comb, which sits entirely above 8 kHz, outside this prediction's
    grading band. A subset skewed toward the deeper-dip positions
    genuinely differs from the full 10-position average — this is § b's
    real, position-dependent lobing feature doing what a real
    (correctly-excluded) mic-position-dependent defect does to a spatial
    average, not the comb. (Corroborating asymmetry: the exclusion mask
    covers 1729–1882 Hz — containing the tweeter-height group's dip
    [1778–1864] entirely while excluding the low group's [1925–2021]
    entirely — which is why the masked and unmasked worst cases nearly
    coincide, 2.634 vs 2.826 dB.)
  - **4 FAIL:** `combine_positions` excluded 0 of 5462 bins in 8–16 kHz,
    only the 1.7 kHz region — the screen never got a chance to flag the
    comb because power-vs-median there measures only **+1.27 dB**, under
    its own >2 dB trigger (§ e.1).

  **Prediction 5's full contingency, and which half survived:** "if nulls
  do *not* move, the bounce is speaker-fixed diffraction — the exclusion
  screen carries more weight and the fundamentals survive unchanged." The
  antecedent fired exactly as written (median pairwise τ shift 1.6 %,
  `geometry_locked`), and the first half of the consequent is confirmed
  (the bounce is speaker-fixed diffraction, § a). The **second half is
  corrected by § e.1**: for a position-invariant defect the screen carries
  *less* weight, not more (0 of 5462 8–16 kHz bins excluded; +1.27 dB
  measured vs. its own >2 dB trigger), and the fundamentals did not
  survive unchanged — they gained the orthogonal interference-null gate
  precisely because the screen alone proved insufficient.
- **Prediction 6 (leg B, "no detectable echo") "PASSed" as a grader false
  negative**, not a real result. `grade_prediction_6` checks `credible`,
  then `clean_no_echo`, then the energy rule, in that order; all three
  floor positions had `refusal == ""` and `confidence = 0.000`, so they
  took the `clean_no_echo` branch — but `detect_echo`'s own cepstrum still
  landed on **327.2 / 270.2 / 342.8 µs**, and replaying the energy rule
  with a real leg-A reference gives deltas of **+5.46 / +5.04 / +4.28 dB**
  (thresholds: ≤ median−10 PASS, ≥ median−6 FAIL), all `energy_comparable`
  → **FAIL**, the opposite verdict. A leg-B-only session directory also
  makes the energy discriminator structurally unreachable
  (`leg_a_median_energy_db` is `None`) — a second, independent way the
  same PASS was wrong. Kit fix queued (§ f).
- **Prediction 8 is structurally unevaluable — the same cross-leg gap as
  prediction 6's missing reference.** Both session-dir SCORECARDs return
  an empty result for it: the main-leg scorer has 10 leg-A positions and
  0 leg-B ("no leg-B (ground-plane) captures in this session/corpus"),
  the ground-plane scorer has 3 leg-B positions and 0 leg-A ("no leg-A
  cloud available") — prediction 8 needs both legs in one corpus and
  neither session directory has that. Prediction 7 is INCONCLUSIVE for
  the identical reason (`leg_a_positions=0` in the groundplane scorer).
  The leg-B decision rule "(6)+(7)+(8) hold" could therefore never fire
  as written: 6 "PASSed" only as the grader false negative above, 7 and 8
  are both INCONCLUSIVE by construction.
- **Prediction 9 (leg B, floor position agreement) FAILed** — the kit's
  own grading (300 Hz–8 kHz, masked) reports worst_case **10.52 dB**
  (worst pair `ground_plane_02` vs `ground_plane_03`) — **dominated by
  one outlier, but not "localized to" it**: even the surviving pair,
  gp_01 vs gp_02, still misses the graded ±1 dB bar at **1.88 dB** in the
  kit's own band (REPORT.md's wider 250 Hz–20 kHz re-derivation of the
  same pair gives 1.73 dB — same conclusion, different band). gp_01 and
  gp_02 both disagree with gp_03 by up to **11.21 dB at 10.1 kHz**.
  Position 03's own fingerprint (strongest 125 µs floor arrival at
  −0.64 dB / r = 0.93, deepest HF nulls at 17.7/15.4 dB, an 8–16 kHz mean
  of −26.3 dB vs. −19.3/−20.2 dB for the other two) points to a different
  cabinet tilt or mic seating on that one position, not proof
  ground-plane captures are inherently irreproducible — but the
  flush-capsule protocol redo (§ f; "S0 leg B" below) is required
  regardless of which pair you look at.
- **The ground-plane protocol itself failed**, independent of the
  predictions above — see "S0 leg B" in Implementation stages below.

### d. The honest top-octave statement

Through the shipped `evaluate_flat_spec`, the top-octave residual
attributable to the speaker's own on-axis response is **bounded above by
~8 dB and below by ~2–3 dB** (the tweeter-height desk read of −8.00 dB,
minus the 5.4–7.0 dB of comb-null depth it contains). That is a wide
range, and **tonight produced no comb-free top-octave measurement** — the
ground plane, which might have supplied one, was instead the *worst*
top-octave reference of the night (see "S0 leg B" below). **The −8.94 dB
all-positions number must not be quoted as "the speaker's top octave."**
It reproduces the kit's own prediction-3 number to the digit (validating
this analysis pipeline against it), but it is a comb-contaminated read,
not a clean one.

### e. Doctrine consequences

1. **The position-invariance exclusion screen is necessary but not
   sufficient.** It correctly excludes the 1.7 kHz dip (real
   mic-position disagreement), but by the same logic it structurally
   cannot flag a position-invariant defect — it excluded the
   (irrelevant-to-correction) lobing dip and passed the (uncorrectable)
   source comb straight through (0 of 5462 8–16 kHz bins excluded). **The
   plan adopts a second, orthogonal gate: interference-null
   identification** — a null ladder `(n+½)/τ` consistent with a single τ,
   corroborated by a matching time-domain arrival at the r the ladder
   implies. Tonight's data is the demonstration that this gate works (§ b's
   0.03 r agreement). A bin identified this way is **excluded from
   correction and from pass/fail**, with the τ/r recorded as the reason;
   the **envelope around it is still corrected**.
2. **The 8–16 kHz ±2.5 dB tolerance is not achievable by EQ** against a
   5.4–7.0 dB source-fixed comb sitting inside that band. The spec needs a
   documented carve-out for identified interference nulls, **pending the
   owner's decision on a source fix** (horn redesign). Scaling note for
   that conversation: shrinking the mouth 220 mm → 160 mm moves the whole
   null ladder up ~37 % (null-0 ~1.56 → 2.14 kHz) — it relocates the comb,
   it does not remove it. Per standard diffraction reasoning, more lip
   roll-back (this horn's is already ≈36 % of the mouth radius) should
   reduce the echo's amplitude r **without** materially shifting τ, because
   delay tracks mouth radius while amplitude tracks how abruptly the rim's
   curvature changes — design guidance only, not yet confirmed
   acoustically.

### f. Queued follow-ups

- **Kit fixes (plan-consistent, small).** `grade_prediction_6` — move the
  energy discriminator before the `clean_no_echo` branch (or make
  `clean_no_echo` also require collapsed energy), and let a leg-B-only
  session directory load a leg-A reference from a sibling session instead
  of silently returning `None`. `detect_echo` — add an in-band-SNR
  refusal (`band_below_passband`): the loopback run found it return a
  confident-looking result (confidence 0.275, empty refusal) on a
  woofer-branch signal that was **49.7 dB below its own passband** — pure
  stopband residue and quantisation noise.
- **Per-driver `MEASURE` session** — the discriminator for the
  crossover-lobing-vs-intrinsic-woofer-feature question (§ b).
- **Flush-capsule ground-plane redo** — see "S0 leg B" below.
- **Near-field workstream for the 400–1500 Hz hump.** Its earlier partial
  attribution to the bounce (Evidence § 1 below) is *also* superseded: the
  rim wave is only resolvable in the tweeter's 5–19 kHz band (cepstrally
  unanswerable in the woofer band — the search rails to the window edge on
  15 of 16 positions), so there is no acoustic evidence it reaches down to
  400–1500 Hz. The hump's attribution reverts to baffle-step/room, pending
  the near-field instrument.
- **A non-similar horn variant** (different depth-to-mouth-radius ratio,
  same cabinet) to resolve both the radius-vs-depth AND the
  horn-vs-cabinet ambiguity in § a.2.
- **Pre-closed, do not re-open:** the tweeter branch's `inverted: true` is
  **correct**. The live DSP graph's electrical sum shows a −25.8 dB notch
  at 3.34 kHz from the polarity inversion against the LR4 crossover; it is
  **absent acoustically** (−3.3/−3.5 dB, a local peak, in
  cloud_01/cloud_03) — the physical driver's own polarity/phase
  compensates the DSP inversion. Recorded so the electrical notch isn't
  mistaken for a defect later.

### g. Artifacts

Laptop-durable, gitignored, under `captures/flat-linearization-20260725/`:

- `s0-session-main/` — 10-position desk cloud + `OPERATOR-NOTES.md`
- `s0-session-deskedge/` — 3-position desk-front-edge leg
- `s0-session-groundplane/` — 3-position floor leg
- `s0-analysis/` — `REPORT.md` (this section's primary source), five
  charts (`chart1-three-way-overlay.png` through
  `chart5-echo-vs-null-consistency.png`), `findings.json`, `horn-cad/`
  (`GEOMETRY.md` + `geometry.json` + `profile_chart.png`), `loopback/`
  (`LOOPBACK-REPORT.md` + configs/WAVs/findings JSON)

## Evidence — why the instrument must change

### 1. Offline comb forensics (2026-07-25, no new captures)

> **Superseded by the executed S0 session (2026-07-25) — see "S0 executed —
> results and the attribution correction" above.** The mechanism and
> numbers below hold up; the *attribution* does not. This section calls
> the ~320 µs arrival a mic-side/desk-side boundary bounce. S0 found it is
> the speaker's own horn mouth-rim wave: it survived a 10-position mic
> cloud, a speaker move to the desk front edge, and removal of the desk
> entirely, and a CamillaDSP electrical loopback ruled out the DSP graph
> (r ≈ 0.021 electrical vs. r ≈ 0.34–0.40 acoustic). Kept in place, not
> deleted, per this repo's documentation paradigm.

Reanalysis of the 2026-07-24/25 session WAVs (runs 5 and 7, MEASURE and
VERIFY frames), preserved with scripts and charts under
`captures/flat-linearization-20260725/` (laptop-durable, gitignored):

- The band-limited (6–19 kHz) IR envelope shows a **discrete echo train at
  +0.31 ms (−8.8 dB, r≈0.36) with 2τ/3τ repeats** — byte-similar in the
  summed VERIFY frame and the tweeter-alone MEASURE frame, and unchanged
  between run 5 and run 7 (~1.6 h apart, entirely different DSP).
- Interference-null ladder `(n+½)/τ` for τ≈298 µs (~10 cm path delta) puts
  rungs at 1.68 / 5.04 / 8.40 / 11.75 / 15.11 kHz; the measured dip set
  (1707, 8396, 8924, 11507, 15559 Hz, identical bins run 5 vs run 7)
  tracks it, with the split pairs explained by the cepstrum's 286+357 µs
  delay doublet (present in every HF frame; the woofer-alone band is too
  narrow to resolve it). Rung 1 (~5 kHz) appears only as a shallow ~−1.5 dB
  dip — directivity-weakened, below the >2 dB screen threshold — which is
  why S0 expects no 2–7 kHz flags.
- **The 1.7 kHz "crossover dip" is in the woofer-ALONE capture** (−9 dB at
  1712 Hz): it is the same bounce's null 0, not a crossover integration
  failure. The bounce also predicts ~+2.7 dB coherent lift below the first
  null — a large share of the 400–1500 Hz hump, consistent with the hump's
  measured cross-placement scatter (±1–2.6 dB across placements vs
  0.2–0.7 dB within-placement, from the 2026-07-24 Phase-0 87-capture
  replay, preserved under
  `captures/flat-linearization-20260725/phase0-forensics/`).

  > **Superseded by S0 (2026-07-25):** this bullet's "same bounce's null 0"
  > claim conflated two mechanisms. S0's per-position tracking shows the
  > 1.7–1.8 kHz dip and the 8–16 kHz comb behave oppositely (10.7 → 4.1 →
  > 1.7 dB filling across desk/low-mic/floor vs. 5.9–7.0 dB holding then
  > *deepening* to 10.4–12.7 dB on the floor; Pearson r = −0.05 between
  > them, leg A), and the measured echo's r ≈ 0.37 physically cannot cut a
  > 10.71 dB dip (6.81 dB ceiling). The 1.8 kHz dip is very likely
  > crossover-region vertical lobing, not this echo's null 0. Because the
  > echo is only resolvable in the tweeter's 5–19 kHz band, there is also
  > no acoustic evidence it reaches the 400–1500 Hz hump; that attribution
  > reverts to baffle-step/room pending the near-field instrument. See
  > "S0 executed" § b and § f above.
- Therefore the MEASURE-vs-VERIFY "frame discrepancy" was reporting
  (band/point-probe averaging riding comb peaks), not physics; and **the
  true top-octave residual is unknowable from any existing capture** —
  every capture is bounce-contaminated.
- Caveats kept honest: the parallel iMM-6C's upper dips nearly coincide
  with the UMIK-2's (similar rig geometry can do that), so definitive
  position-dependence proof is the validation session's mic moves; comb
  *depths* vary with directivity vs the simple model; cross-mic HF levels
  are additionally contaminated by cal pedigree (#1672).

A ~0.3 ms echo **cannot be time-gated**: it arrives essentially glued to
the direct sound, and a gate short enough to exclude it destroys all
resolution below ~3 kHz. Gating handles late (wall) reflections; only
spatial diversity handles early boundary interference.

### 2. Industry research (owner deep-research pass, 2026-07-25)

Verbatim report preserved at
[research/2026-07-25-flat-linearization/](research/2026-07-25-flat-linearization/01-robust-measurement-and-flat-spec.md)
(house pattern: this plan wins where they disagree); design-relevant
conclusions:

- **No shipped consumer product removes an early bounce from a single
  capture.** Every mass-market system averages it away spatially: Sonos
  Trueplay moving-mic PSD (power) averaging over >150 positions (Sonos
  engineering blog; US 10,045,138), Dirac Live 9–17 positions, Audyssey 8
  positions with fuzzy c-means weighting (US 8,005,228), and "four
  farfield locations are ideal" (US 8,130,966, Performance Media
  Industries). All correct **minimum-phase only** and decline to fill
  non-minimum-phase interference nulls (Toole doctrine).
- **Estimator:** power (energy) mean of magnitude spectra across
  decorrelated positions is the proven combiner; median is a robustness
  cross-check; max-hold is positively biased (rejected); complex averaging
  needs phase coherence a hand-moved mic cannot give. Exact estimator-bias
  dB figures near comb nulls are not tabulated anywhere — characterize on
  our rig (validation session).
- **Decorrelation physics:** spatial correlation follows sinc(kr) (Cook
  1955); nulls decorrelate at ~λ/2 spacing — ~10 cm at 1.7 kHz, ~2 cm at
  8.6 kHz; ±1 dB at 1 kHz with 1/6-oct smoothing needs on the order of
  8–12 independent captures (1/√N).
- **Cepstral/homomorphic echo removal is academic-only** for this use, and
  fails exactly on our shape (directivity-weighted r, 2τ/3τ repeats,
  consumer SNR). Use the cepstrum to **detect** and flag, never to remove.
- **Spec practice:** CTA-2034 Listening Window (spatial average) is the
  direct-sound curve that best tracks preference (Olive model, US
  8,311,232; smoothness terms carry the largest weights); credible
  manufacturer tolerances cluster at ±1–3 dB, 1/3-oct-ish smoothing;
  sub-±2 dB above 8 kHz is not meaningful at UMIK-2-class uncertainty.
- **Closed loop:** consumer systems are mostly open-loop single-pass; REW
  practice and our own realization-shortfall data argue for
  measure → correct → **re-measure at target SPL** → residual trim;
  thermal power compression is a plausible (unconfirmed for our rig)
  mechanism for commanded-vs-realized shortfall that only a re-measure
  catches. Loop convergence: residual < ~1 dB RMS 300 Hz–8 kHz; roll back
  any pass that worsens error.

## The six fundamentals

1. **Spatial multi-capture is THE measurement.** N≈8–12 gated sweeps at
   guided positions (≥10 cm spread for HF null decorrelation; ≥~30 cm
   spread to support the LF edge), per-capture quality gates (SNR, and the
   existing repeat/drift machinery within each position), combined by
   power average. Single-point measurement is demoted to a diagnostic.
   Discrete prompted positions first (lab UMIK-2 flow); Trueplay-style
   continuous moving capture is a later UX layer on the same combiner
   seam.
2. **Interference honesty screen.** Per-capture cepstral echo detection
   stamps τ/r diagnostics; across positions, bands where power-mean and
   median disagree by >2 dB are flagged interference-dominated. Flagged
   bins are excluded from correction **and** pass/fail, and reported.
   Detection only — no echo removal in production.

   > **Note (2026-07-25):** S0 found this screen is necessary but **not
   > sufficient** — see "S0 executed" § e.1. It correctly excludes the
   > position-dependent 1.8 kHz lobing dip, but a position-*invariant*
   > defect cannot diverge across positions in the first place: the
   > measured 8–16 kHz power-vs-median gap was only **+1.27 dB**, under
   > this screen's own >2 dB trigger, so the source-fixed comb passed
   > through unflagged (0 of 5462 bins excluded). The plan adds a second,
   > orthogonal instrument — interference-null identification (τ-ladder
   > consistency + matching time-domain arrival) — specifically to catch
   > what this screen structurally cannot.
3. **Minimum-phase, cut-biased correction only** (existing house rule:
   cut-domain + anchored give-back). The fit engine consumes the combined
   curve + exclusion mask; only features that survive spatial averaging
   get corrected.
4. **The spec above is the definition of done** for the speaker layer's
   "top of the table" contract in the layer doc.
5. **Closed loop at target SPL.** measure(cloud) → fit → apply →
   re-measure(cloud) → residual trim; converge at <~1 dB RMS
   300 Hz–8 kHz (8–16 kHz reported against its own tolerance); any pass
   that increases residual error rolls back on the existing apply/undo
   rails.
6. **Role-count-blind.** Spec + loop operate on the summed system curve;
   per-driver machinery (linearization fit, alignment, protection) sits
   beneath, unchanged in ownership. 1-way = one full-range role; 3-way
   rides #1703's conductor generalization.

## Layer-stack seams (what this changes, what it does not)

The five-layer model is unchanged. This program changes the **instrument**
for the speaker layer (1a driver linearization + 1b crossover integration):
single-point gated sweep → spatially-averaged gated cloud. Gating removes
late (wall) reflections; spatial averaging removes early boundary
interference — together they finally deliver the "reflections excluded"
promise Layer 1a/1b already makes. It also *repairs a live layer
violation*: the bounce was leaking measurement-geometry content into the
speaker layer, so speaker EQ was partly fitting the rig (the 1.7 kHz dip;
the top-octave sizing). With the cloud + exclusion screen, the speaker
layer can only correct speaker-intrinsic features.

> **Note (2026-07-25):** S0 found the top-octave share of "measurement-
> geometry content" above is not that at all — the 8–16 kHz comb is the
> speaker's own horn rim wave, source-fixed across every mic position and
> every geometry tried, including the ground plane (see "S0 executed" § a
> above). "Spatial averaging removes early boundary interference" remains
> true as a general principle, and still holds for the 1.7 kHz dip (which
> genuinely is mic-position-dependent) — but it does not apply to this
> specific arrival: no amount of spatial diversity averages away a defect
> that is fixed to the source. It is still correctly excluded from
> correction, but as an *identified interference null* (§ e above), not as
> content that belongs to the rig.

Seams, precisely:

- **Observable seam:** speaker layer = gated cloud at ~1 m on the design
  axis; Layers 2–3 = in-room, ungated, at the listening position. Two
  instruments, no shared writer, no double correction: room correction
  composes on top of a genuinely flat speaker and corrects only what the
  room adds (modal peaks below the transition, at most a gentle broad
  tilt above — its existing philosophy).
- **Frequency seam:** speaker layer owns the gate-valid band (≥ the
  ~250 Hz spec edge); Layer 2 (bass) and Layer 3 own below, as today. The
  parked near-field workstream (`build_bass_nearfield_spec` consumer)
  remains the future instrument for sub-edge *speaker* truth (baffle
  step), distinct from room modes.
- **Alignment nuance:** 1b's delay/polarity solve keeps its single-position
  design-axis reference program — **because** inter-driver timing is
  position-*dependent* across a cloud (with ~25 cm driver separation at
  ~1 m, an off-axis mic move shifts the inter-driver path delta by tens of
  µs — lobing physics), alignment must never be cloud-averaged; it is
  solved at the design axis, where same-position repeatability is proven
  at 2.77 µs. The cloud is the instrument for magnitude spec,
  linearization, and VERIFY only. The cloud average grades the crossover
  region the way CTA-2034's listening window does (slightly gentler than
  a single on-axis point — by design).
- **Non-min-phase doctrine is now uniform across layers:** narrow
  interference dips are excluded from correction and metrics in the
  speaker layer (this plan) and remain uncorrected in room correction
  (its existing conservative-above-transition philosophy). A broad
  "boundary/desk mode" shelf, if ever wanted, is a Layer-3 product
  feature, not linearization — out of scope here.

## Implementation stages

> **Execution work order (2026-07-25):** the productization of S1b/S2 —
> PR ladder, integration seams, acceptance gates, and the hardware
> runbooks — lives in
> [`flat-linearization-productization-plan.md`](flat-linearization-productization-plan.md).
> This section stays the stage-level strategy; that doc is the work
> order implementer sessions execute.

Process for every stage: owner go at stage boundaries; branch + PR always;
independent adversarial review (canonical prompt) to 0 blockers /
0 should-fixes; hardware-affecting changes validated on JTS3 with charts
against pre-registered predictions; audible playback only after an owner
ping. Opus-tier implementers for the estimator/loop cores, Sonnet-tier for
plumbing/tests/wizard copy. The bass session's lane
(`jasper/bass_extension/*`, `correction_bass_flow`, bench) is not touched.

- **S0 — Validation session (hardware, owner at studio, ~30 min).**
  Mic-move-only: ~10 positions, N=2 gated sweeps each, current DSP
  untouched. Pre-registered predictions: (1) per-position HF null
  frequencies shift ≥8 % position-to-position; (2) the power-averaged
  curve is stable — any 6-of-10 subset agrees within ±1 dB, 300 Hz–8 kHz;
  (3) the average reveals the true top-octave residual (sizes S3);
  (4) power-vs-median flags the 1.7 k and 8–16 k null regions and nothing
  in 2–7 kHz; (5) if nulls do *not* move, the bounce is speaker-fixed
  diffraction — the exclusion screen carries more weight and the
  fundamentals survive unchanged. Also settles N, achievable spread, the
  250-vs-300 Hz edge, and empirical estimator bias (power vs median) on
  this rig. Analysis is offline against these captures before any code
  ships.

  > **Amended 2026-07-25:** prediction 5's antecedent fired — nulls did
  > not move (median pairwise τ shift 1.6 %, `geometry_locked`) — but its
  > consequent above is only half right. "The bounce is speaker-fixed
  > diffraction" is confirmed ("S0 executed" § a). "The exclusion screen
  > carries more weight" is **corrected by § e.1**: for a position-fixed
  > defect the screen structurally carries *less* weight — it excluded
  > 0 of 5462 8–16 kHz bins because the real power-vs-median gap there
  > measured only +1.27 dB, under its own >2 dB trigger. "The
  > fundamentals survive unchanged" is also corrected: they gained the
  > new orthogonal interference-null-identification gate specifically
  > because the screen alone proved insufficient. Also graded FAIL:
  > predictions 1, 2, and 4 (§ c) — the geometry-locked outcome falsifies
  > the shared assumption behind them but does not make any of them PASS.

  **S0 leg B — ground-plane configuration (~10 min, owner-approved
  2026-07-25).** The one-time-commissioning reframe revises research
  brief 01's "no ground-plane" UX constraint: floor placement is an
  instruction, not equipment. Protocol: speaker moved to the hard floor
  and **tilted so the design axis aims at the mic**; UMIK-2 lying flat
  on the floor (capsule on the surface, end-on toward the speaker — the
  0° cal remains valid), ~1 m, 2–3 positions, N=2 sweeps each. Physics:
  with the mic at the boundary, image-source geometry makes the bounce
  path equal to the direct path at any speaker height — coherent +6 dB
  (normalized out) instead of a comb; ~8 mm of capsule height keeps the
  residual null above ~30 kHz. Pre-registered predictions: (6) no
  detectable echo in the 0.15–1.0 ms window (the shipped detector
  refuses / reports no credible peak), vs every tabletop capture firing
  at ~0.31 ms; (7) no `(n+½)/τ` null ladder — the 1.7 k and
  8.4/11.5/15.5 k dips fill by ≥4 dB relative to tabletop single-point;
  (8) a ground-plane single position agrees with the mic-move cloud
  power average within ±1.5 dB over 500 Hz–8 kHz (below 500 Hz,
  half-space loading may differ; above 8 kHz, aim/incidence effects
  apply); (9) floor position-to-position agreement within ±1 dB,
  300 Hz–8 kHz — geometry restores single-point honesty. Decision rule,
  pre-registered: (6)+(7)+(8) hold → ground-plane becomes the
  **recommended one-time commissioning protocol**, with the cloud as
  the carpet-home/immovable-speaker fallback and standing cross-check,
  and the echo detector wired as the protocol's acceptance guard at
  S1b (echo found ⇒ tell the user and fall back to the cloud — the
  self-verification that makes the technique consumer-safe); (6) fails
  ⇒ the residual echo is speaker-fixed (edge/horn), which is prediction
  (5)'s branch by other means. Known biases to disclose, not spec:
  half-space loading colors the baffle-step region relative to
  free-field; imperfect tilt shows as a crossover-region lobing
  deviation (a protocol confound, not a failed prediction).

  > **S0 result (2026-07-25): the core assumption above is falsified, and
  > the protocol itself needs a fix.** The image-source argument predicts
  > the ~320 µs arrival should coherently sum away at the boundary; instead
  > it **survives the ground plane** — present on all 3 floor positions at
  > −7.05, −9.96, −10.43 dB re direct, the same order of magnitude as every
  > tabletop position (curve construction itself reports
  > `geometry_locked = False` / `geometry_insufficient_usable_estimates`,
  > 0 of 3 positions credible under the leg-A default echo window). That
  > is expected once the arrival is understood as speaker-fixed (see "S0
  > executed" § a above): there is no image source for a horn-internal
  > reflection to cancel against. Separately, the protocol's own "~8 mm of
  > capsule height" assumption did not hold in execution: the ground-plane
  > captures carry their own dominant early arrival at **125–146 µs**
  > (4.3–5.0 cm of path, −0.64…−2.57 dB re direct, r = 0.74–0.93) —
  > consistent with the capsule sitting several centimeters proud of the
  > actual boundary ("a mic lying on the floor is not at the floor"). That
  > extra arrival made the ground plane the **worst** top-octave reference
  > of the night (−24.43 dB vs. the desk edge's −6.94 dB — see § d above),
  > and it is also why prediction 7's "≥4 dB fill" claim is itself mixed:
  > the 1.7 kHz dip fills sharply (10.7 → 1.7 dB) but the 8.4/11.5 kHz
  > nulls *deepen* instead (7.0 → 10.7 dB; 6.7 → 15.7 dB) — consistent with
  > the added near-field arrival, not boundary cancellation. Predictions
  > 6 and 9 above are covered in detail in "S0 executed" § c. A valid
  > ground-plane redo needs a **flush-capsule protocol** — the capsule
  > genuinely at the boundary, not resting a few centimeters above it —
  > before this leg can be trusted as a comb-free top-octave reference.
- **S1 — Instrument.** Conductor position-group choreography (prompted
  moves between capture groups; position metadata; per-position quality
  gates) + the combiner/screen estimator module (power mean, median
  cross-check, exclusion mask, cepstral τ detector). Offline-replayable
  against S0's corpus before it touches the live flow.

  > **S1 landed (2026-07-25/26).** The combiner/screen estimator module
  > (`combine_positions`/`detect_echo`/`assess_geometry` in
  > `jasper/audio_measurement/spatial_combine.py`) shipped offline-first
  > via #1741, then the orthogonal interference-null identification gate
  > (`identify_interference_nulls`,
  > `jasper/audio_measurement/interference_nulls.py`) via **PR-1, #1751**,
  > and `detect_echo` hardening (`band_below_passband`,
  > `earlier_dominant_arrival`, thin-evidence lock, `effective_floor_us`
  > disclosure) via **PR-2, #1749**. The conductor position-group
  > choreography shipped via **PR-3a, #1754** (relay capture-plan
  > capacity) and **PR-3b, #1755** (the choreography itself — the
  > shipped main-session plan becomes the 16-entry cloud). See
  > `flat-linearization-productization-plan.md`'s PR ladder for the
  > mechanism deviations recorded against each.
- **S2 — Spec + gauges.** Spec bands/tolerances/1-3-oct evaluation;
  exclusion-aware flatness gauges; VERIFY widened from the ~2·Fc
  integration band to the full spec band; wizard + `/state` surfacing. The
  observe ledger, fit working curve, gauges, and VERIFY all consume one
  shared curve construction (kills the frame-discrepancy class for good).

  > **S2 landed (2026-07-26/27).** The live-flow wiring (combine →
  > identify_interference_nulls → evaluate_flat_spec, assembled by
  > `assemble_cloud_group_result`) shipped via **PR-4, #1756**; the
  > shared spec-curve construction (one `combine_positions` spec curve →
  > one `evaluate_flat_spec` call → one `flat_spec.spec_flatness_gauge`
  > reduction, consumed byte-identically by `/state`, the envelope, the
  > doctor, and the wizard) shipped via **PR-5, #1757**, retiring
  > `_flatness_tracking`'s separate per-capture construction. The
  > exclusion-aware fit envelope (`compose_envelope`'s cloud-derived
  > `spatial_exclusion_limit` / `position_stability_limit`) shipped via
  > **PR-6a, #1753**; the carve-out disclosure half (null registry →
  > spec-band disclosure with τ/r/rung/depth, plus the owner's fit-timing
  > move to the pre-apply group's close) shipped via **PR-6b, #1760**.
  > Before/after wizard visualization + anomaly callouts — the "wizard +
  > `/state` surfacing" above — shipped via **PR-7, #1761**. VERIFY's
  > widening from the ~2·Fc integration band to the full spec band is
  > part of the PR-5 construction above.
- **S3 — Closed loop.** measure → fit (existing cut-domain engine +
  anchored give-back, now fed the combined curve) → apply → re-measure →
  residual trim; convergence + divergence/rollback policy on the existing
  apply/undo rails; charts each iteration. Then spend what the honest
  measurement says is real: top-octave realization beyond the single-shelf
  cap (stacked shelf / literal boost per the standing adjudication) only
  if S0/S3 data demands it. Note: the literal-boost branch would amend
  the layer doc's decision 4 (emitted per-driver correction gains stay
  non-positive — a do-not-re-litigate safety posture) and is **not
  authorized by this plan**; taking it requires an explicit owner
  amendment plus its own headroom/safety design. The stacked-shelf
  branch stays inside the cut-domain contract.
  (*Superseded 2026-07-27:* the owner amendment this paragraph asks for was
  given — decision 4 now permits a bounded positive gain, and the layer doc
  owns its current wording. Read this note as the 2026-07-25 record of what
  the plan itself authorized.)
- **S4 — Generalization.** Loop core stays role-count-blind (consumes
  topology roles); 3-way lands with #1703's conductor; passive/1-way =
  one full-range role through the same loop.

## Adjudicated: single-point time-selection methods (2026-07-25)

A second research pass
([research/2026-07-25-flat-linearization/02](research/2026-07-25-flat-linearization/02-time-selective-excitation-viability.md))
evaluated the owner's pulsed-sweep / fast-sawtooth proposal ("time the
lulls so the echo lands in silence, excise, stitch"). Verdict, adopted:

- **Pulsed lull-excision and fast tracking sweeps are killed as
  measurement methods — by theorem, not experiment.** For an LTI system
  both are mathematically a ~0.31 ms time gate (the fast-sweep variant is
  Heyser's TDS; TDS ≡ windowing per Vanderkooy 1986 and Müller &
  Massarani 2001). Separating arrivals τ apart forfeits resolution below
  ~1/τ ≈ 3.2 kHz, and the DUT's own LR4+horn response rings past τ, so no
  echo-free lull exists under any excitation. Do not revisit; there is no
  threshold that reopens a theorem.
- **The surviving core ships as QA: the ultra-short-gate HF cross-check.**
  A ~0.3 ms gate on sweeps we already capture yields a comb-free (but
  truncation-biased, >3.2 kHz-only) view of the direct sound. S0's
  offline analysis computes it alongside the cloud average; if the two
  agree above ~3.2 kHz within tolerance it becomes a standing
  consistency gauge at wiring time, never a calibrated measurement below
  3.2 kHz.
- **The single-point bet — cepstrum-seeded regularized two-path
  inversion: gate run 2026-07-25, verdict NO-GO, lane SHELVED.** The
  prototype (fit H_meas(f) = H_d(f)·[1 + r(f)·e^(−j2πfτ)] + geometric
  2τ/3τ repeats, P-spline smoothness priors, variable projection;
  artifacts + GATE-REPORT.md at
  `captures/flat-linearization-20260725/inversion-prototype/`) failed
  the pinned gate decisively: worst-case recovery 4.84 dB vs the
  1.0/1.5 dB threshold (6/24 gate cases pass). Two independent fatal
  mechanisms, either sufficient: (1) the fit needs a τ seed good to
  ~±1 %, while the shipped detector honestly delivers ~3–4 % at our
  ~310 µs (bottom of the default window) — a 3–4× precision gap; (2)
  below 1/τ the comb completes less than one cycle, so r(f) there is
  extrapolated by its smoothness prior, and a non-monotone
  (field-shaped) r(f) is unrecoverable even with oracle tuning
  (3.2 dB). The method is bias-limited, not noise-limited — SNR was
  never the binding axis. Revisit only with a fundamentally better τ
  seed AND real-data evidence of monotone r(f); the τ gap alone is
  independently fatal.
- **Material side-finding for the primary method (disclosed, encoded):**
  on the same synthetic truths, the spatial cloud average retains
  **2.1–3.8 dB of common-mode bounce lift below ~1/τ and does not
  converge with N** at realistic spreads — at 250 Hz a 180–480 µs
  bounce-delay spread is only 0.28–0.75 rad, so every position sees
  nearly the same coherent lift and averaging has nothing to cancel;
  only wide spread helps (~120–700 µs → ~2 dB residual). Consequences:
  the fundamentals' ≥30 cm LF-spread guidance is load-bearing, S0
  leg A should include at least two wide-offset positions, leg B
  (ground-plane — no bounce at all) is the strongest LF-truth
  instrument, and prediction 8's leg-A-vs-leg-B comparison over
  500–1500 Hz doubles as the direct measurement of leg A's residual
  common-mode lift (a systematic leg-A-high reading there IS that
  bias, not a failed prediction).

  > **Note (2026-07-25):** this side-finding's synthetic model assumed a
  > genuine boundary bounce, and on that assumption "leg B (ground-plane
  > — no bounce at all)" was a fair modeling shorthand. S0 found the real
  > ~320 µs arrival is not a boundary bounce at all — it is present on
  > all 3 ground-plane positions at −7.05, −9.96, −10.43 dB re direct
  > ("S0 executed" § a) — and the ground-plane leg additionally picked up
  > its *own* new near-field arrival at 125–146 µs, r = 0.74–0.93 ("S0 leg
  > B" above). "Wide spread helps" still holds for a genuine
  > boundary/room reflection, which decorrelates with position; it does
  > not apply to the source-fixed arrival S0 actually found, which no
  > spread — leg B included — removes.
- **Slower sweeps do not help separation** (stationary interference;
  LTI excitation-invariance; verified empirically — the N=3 in-place
  repeats change the comb by <0.1 dB) and SNR is already ~124 dB. Sweep
  length remains an SNR knob only. Compact mic arrays and moving-mic
  capture are spatial-channel methods, not single-point escapes; not
  pursued.

## Non-goals / guardrails

- No cepstral or parametric echo *removal* in production (detection only).
- No max-hold estimator; no complex averaging of hand-moved captures.
- No EQ of interference-flagged bins, ever; they are reported instead.
- No absorber pads, tripods, or treatment steps in any user flow.
- No change to CamillaDSP safety ceilings (`devices.volume_limit` 0.0,
  positive-gain clamps) or driver-protection floors.
- Room correction's scope is untouched; no layer eats another's job.

## Open questions (tracked; S0-resolved items marked below)

1. N and spread achievable on the lab rig; empirical estimator bias near
   nulls (S0 decides). **Resolved by S0 (2026-07-25):** N = 10 (desk
   cloud) / 3 (desk edge) / 3 (ground plane) achieved in one evening
   session. The deeper framing — "estimator bias near nulls" — is
   superseded by a more consequential, structural finding: the
   position-disagreement exclusion screen cannot flag a position-invariant
   defect at all (`combine_positions` excluded 0 of 5462 bins in
   8–16 kHz). See "S0 executed" § e for the new orthogonal gate this
   motivates.
2. Spec lower edge 250 vs 300 Hz (S0 LF spread data decides). Still open —
   S0's three sessions did not produce LF-spread data addressing this
   specific edge.
3. Phone moving-capture UX (Trueplay-style) — later layer on the S1
   combiner seam; browser-capture constraints already cataloged in the
   measurement-v2 research.
4. Thermal-compression attribution for the realization shortfall —
   candidate mechanism; the loop handles it agnostically either way.
5. Whether 8–16 kHz can tighten to ±2.0 after realization headroom is
   measured bounce-free. **Resolved-superseded by S0 (2026-07-25):**
   "bounce-free" measurement is not the blocker — the comb is source-fixed
   (§ a of "S0 executed"), not a measurement-geometry artifact a better
   protocol removes. Tightening now depends on question 8 below, not on
   refining the measurement.
6. **New (S0, 2026-07-25).** Radius-vs-depth AND horn-vs-cabinet
   ambiguity: the CAD transit-time match ties τ to the horn's mouth
   radius (110.000 mm → 320.70 µs, the tightest of 15 candidate CAD
   paths), but (a) the most generous axial-depth definition (109.2 mm) is
   numerically close enough that this horn cannot rule out depth as the
   true driver, and a geometrically-similar rescale (the planned
   200/180/160 mm experiments) can't disambiguate either; (b) the CAD
   study is horn-only and cannot rule out a similar-path reflection off
   the baffle/cabinet instead of the horn's own rim. Needs a non-similar
   horn variant on the same cabinet (different depth-to-mouth-radius
   ratio) to discriminate both.
7. **New (S0, 2026-07-25).** Whether the 1.8 kHz dip is crossover-region
   vertical lobing or an intrinsic woofer-response feature that merely
   shifts with angle — the evidence favors lobing ("S0 executed" § b), but
   the discriminating measurement (a per-driver `MEASURE` sweep) has not
   been run.
8. **New (S0, 2026-07-25) — OWNER-PENDING.** Adopt a documented spec
   carve-out for identified interference nulls in the 8–16 kHz band now,
   or hold the spec open until the horn's source fix (redesign) lands?
   See "S0 executed" § e.2.
   **Resolved 2026-07-25 by owner decision 1 — adopt the carve-out now
   (recorded 2026-07-27; the OWNER-PENDING tag above is superseded by this
   line, not deleted).** Identified interference nulls (τ/r recorded) are
   excluded from spec evaluation AND from correction; the band's ±2.5 dB
   tolerance applies to the surviving envelope; the report discloses the
   carved-out ranges with the numbers. The spec table's number is
   unchanged — see the table's own 8–16 kHz annotation. Horn redesign
   proceeds in parallel as the owner's hardware project rather than as the
   alternative this question posed; it is no longer what the spec waits
   on. Product mechanics: the work order's PR-6, shipped as the per-band
   carve-out disclosure in PR-6b.

## References

Repo: [active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md),
[HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md),
`jasper/audio_measurement/program.py` (`build_measure_program`,
`build_verify_program`, `render_program_pcm`),
`jasper/audio_measurement/program_analysis.py`,
`jasper/active_speaker/linearization_fit.py`,
`jasper/capture_relay/spec.py` (`build_bass_nearfield_spec`); issues
#1703 (three-way), #1672 (mic HF trust arbitration). Evidence corpus:
`captures/flat-linearization-20260725/` (runs 1–7 WAVs, forensics scripts,
`comb-verdict.png`).

External (from the owner's research pass): Sonos Trueplay engineering blog
+ US 10,045,138; Audyssey US 8,005,228; US 8,130,966 (Performance Media
Industries); Cook et al.,
JASA 1955 (sinc(kr) correlation); Müller & Massarani, JAES 2001;
ANSI/CTA-2034; Devantier AES 5638; Olive AES 6113/6190 + US 8,311,232;
Toole, *Sound Reproduction* 3rd ed.

Last verified: 2026-07-25
