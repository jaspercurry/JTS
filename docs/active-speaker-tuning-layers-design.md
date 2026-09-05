# Active speaker tuning — the layer model (design)

> **Status: adopted design.** This is the design rationale for the speaker
> layer's five-layer split, the correction envelope, and the seed →
> crossover-science → EQ tuning order. Operational truth for the shipped
> flow stays in
> [tuning-operator-runbook.md](tuning-operator-runbook.md);
> [tuning-master-plan.md](tuning-master-plan.md) is the planning authority
> and owns the shipped measurement-program constants. A corner exactly at a
> driver's declared low limit is a legal, sanctioned operating point — no
> nanny margin.

## The five layers

**The order below is the commissioning order — what must be measured and
applied before what.** Each layer is its own artifact with its own owner,
measurement instrument, and re-run cadence. One fact, one owner — shape
never hides inside a level knob, level never hides inside a shape. All five
compose into one CamillaDSP graph, but the *signal* order inside that graph
is different — room and preference ride the stereo bus ahead of the split
mixer, so they are emitted before everything numbered 1a–2. Do not read
these numbers as filter order; the graph shape is in the
["Layer Boundary"](historical/active-speaker-dsp-investigation-history.md#layer-boundary)
section of the active-speaker DSP investigation history.

| # | Layer | Job | Instrument | Re-runs when |
|---|---|---|---|---|
| 1a | **Driver linearization** | each driver flat *within its own band* on the design axis (CD-horn compensation, baffle step, breakup) | gated/quasi-anechoic sweep at the listening axis (already captured to 18 kHz every MEASURE); optional near-field supplement for the woofer below the gate validity floor | hardware changes (driver, horn, pad) |
| 1b | **Crossover integration** | drivers sum correctly: crossover filters, **scalar** trim per driver, relative delay, polarity | same gated session as 1a | hardware/geometry changes |
| 2 | **Bass** | extension/sub integration below the gated validity floor | near-field (extension); in-room, ungated (sub integration) | hardware/placement |
| 3 | **Room correction** | what the room does: modal peaks below the transition (~300–500 Hz here), at most a gentle broadband tilt above | in-room at the listening position | placement/room changes |
| 4 | **Preference** | declared taste on top of honest-flat | the household's ears | whenever |

Layers 1a+1b together are **the speaker layer**: they make the *device*
measure flat in direct sound, like a factory-tuned active monitor, and they
travel with the speaker. Layer 3 belongs to a room+position. Keeping that
boundary is load-bearing: 1a/1b are measured gated (reflections excluded);
3 is measured in-room; conflating them EQs directivity artifacts and ruins
off-axis sound. Room correction may *lightly* touch speaker-response
residuals only for speaker classes that have no Layer 1 (passive),
inside its conservative-above-transition philosophy.

**The "top of the table" contract (the owner's flatness vision), stated
precisely:** after Layers 1a+1b, the gated direct-sound magnitude on the
design axis is flat within a declared tolerance from the measurement
validity floor (≈143–200 Hz in the JTS3 room; set by the reflection-gate
window) up to ≈16 kHz. Below the floor, flatness is Layers 2–3's contract
with in-room instruments. Preference (4) then deviates deliberately and
visibly.

## Decisions already made (do not re-litigate)

1. **Linearization lives in the crossover program** (same wizard surface,
   same gated instrument, one commissioning session). It produces a
   *separate artifact* from the trim: per-driver EQ curves.
2. **The trim stays a scalar** level anchor. Frequency-dependent balance is
   linearization's job.
3. **Verification splits into two named claims.** Integration-verify (the
   existing 1–4 kHz tracking gate: "the correction realized the predicted
   summation") and a **flatness-verify** ("gated response within tolerance
   from validity floor to 16 kHz"). Envelope/report copy must never let one
   imply the other.
4. **Safety posture:** per-driver linearization gains may be positive, up to
   `MAX_LINEARIZATION_BOOST_DB` (12 dB) per filter — refused rather than
   clamped above that cap, and absorbed by `active_baseline_headroom`; an HF
   shelf is emitted as attenuation elsewhere plus headroom accounting, never
   a positive ceiling raise. The two-invariant protection model and
   declared-sensitivity ceilings stand.
5. **Simple-first execution:** the reference rig is a headless direct-Pi
   drive path (no capture relay) with a reference-tier mic. Relay/phone/
   product-UX hardening is a later concern than getting the acoustics right.
6. **The correction envelope replaces every fixed fit-ceiling number** (see
   "The correction envelope" below). Session enforcement rides with it: 0°
   orientation confirmed plus on-axis aim for fit-eligible sessions, sweep
   upper edge decoupled from the declared driver band. The declared-band
   invariant is asymmetric: the LOWER edge plus the proven high-pass stay
   absolute (excursion protection); the UPPER edge is not a protection
   boundary — low-level ultrasonic sweep content has no damage mechanism,
   and the sweep needs headroom past the analysis band.
7. **Sources:** the verbatim research artifacts live in
   [`research/2026-07-23-driver-linearization/`](research/2026-07-23-driver-linearization/README.md);
   this doc is the adopted synthesis and wins where they disagree.
8. **A driver's low limit has exactly ONE declared owner.** The bottom
   allowed frequency for a driver *is* the manufacturer's minimum
   recommended crossover frequency, carrying whatever slope condition the
   manufacturer attaches to it, entered ONCE through the operator's
   driver-research response at component entry. One field, one owner. Every
   consumer **derives** from it: the linearization fit band, the protection
   posture, the Fc sweep bounds, and the grading bands — never a second
   declared value for the same driver's low limit.
9. **A research prompt asks for published facts; margins are computed
   downstream.** A research prompt asks the manufacturer's published
   facts — minimum crossover frequency and its slope condition — and nothing
   else. A derived safety margin is computed downstream by named code with a
   named rationale, and is never smuggled into a datasheet field as though
   the manufacturer had published it. Horn/compression-driver spec sheets
   usually give the crossover a dedicated line (phrased "Recommended
   Crossover" or "Minimum Crossover Frequency") with the slope condition in
   a footnote; dome tweeters usually carry no such line, and the same figure
   has to be read out of the power-handling rating's test condition instead.
   Absent is a legitimate answer — a driver whose maker publishes nothing
   must report absent, never a guess.
10. **The crossover blend region is summed-response-owned correction
    territory.** Per-driver fitting is deliberately blind across the blend,
    and stays that way — that honesty is correct and is not the defect.
    What moves is who is allowed to act there. Full contract:
    ["The region-based adjustment contract"](#the-region-based-adjustment-contract).
11. **The contract is prescriber-agnostic; the harness is deterministic
    forever.** The region vocabulary and its bounds are defined independently
    of who prescribes. Measurement, safety clamps, keep/restore on measured
    evidence, and receipts carrying commanded-vs-realized per band never
    become a model's judgement call. The prescription *policy* is a
    pluggable seam — see
    ["The prescriber seam"](#the-prescriber-seam).
12. **Sequencing precondition for any prescriber policy (satisfied):**
    upstream truth (decisions 8–9, so the fit band and the protection
    posture derive from one honest number), then the blend-region contract
    (decision 10), then hardware proof that a blend-region dip is
    correctable at all. Prescriber policy is only worth deciding once all
    three hold.
13. **Two capture sources, both first-class.** The commissioning flow
    supports a microphone plugged directly into the Pi — the Pi plays and
    records on one clock, which removes the relay/upload/cross-device-desync
    class structurally rather than diagnosing it — *and* the existing
    web/phone relay flow, kept with its known unreliability accepted and
    disclosed. Where a local mic is detected the flow may recommend it:
    disclose-and-recommend, never nanny. The shape is a **capture-source
    seam**: the conductor asks for a capture of program X at position Y, a
    provider answers with WAV plus metadata, so the relay choreography is
    the relay provider's private internals
    (see [`capture_source.py`](../jasper/active_speaker/crossover_v2/capture_source.py)).
14. **A measurement is (stimulus regime) × (angle) × (level); angle is an
    ATTRIBUTE, not an identity.** Every capture banks as
    `{regime, angle_deg, repeats, level_re_anchor}`; two stimulus regimes
    (per-driver, summed) cover the whole grid; the unit is degrees
    everywhere. The shipped pose set and program menu are
    [tuning-master-plan.md](tuning-master-plan.md)'s "Measurement program
    constants" — that section is canonical for what actually runs; this
    schema is the vocabulary it runs on.
15. **Linearization is a strictly ordered pipeline — seed, then crossover
    science, then EQ.** It is BOTH-AND, never either-or, and the order is
    the ruling. **(P1)** The operator enters driver information, and the
    flow derives basic trims and a basic crossover placement from it.
    **(P2)** The crossover is then tuned with maths, science, and experiment
    until there is high confidence it is as good as it is going to get.
    **(P3)** ONLY THEN does EQ iron out the rest, across the entire trusted
    measurable region, to super flat. What the order buys: a filter spent
    flattening a summation error that a still-free crossover parameter could
    have removed is aimed at the wrong cause, and it hides that cause from
    every later measurement rather than fixing it. The stages and their
    build status:
    ["The linearization pipeline"](#the-linearization-pipeline--seed--crossover-science--eq).

## Layer 1a concretely — UX and data flow

**Linearization adds no flow of its own.** One commissioning flow — CHECK →
MEASURE → explicit household Apply → VERIFY, Apply a separate
`POST /correction/crossover/v2/apply` with verification after it — and
linearization is not a second wizard or an extra sweep; it is a new consumer
of data every session already records. The flow's current shape (capture
count, guided walk) is [tuning-operator-runbook.md](tuning-operator-runbook.md)'s
to state; this section is scoped to Layer 1a's own footprint within it.

1. **MEASURE (richer capture + richer analysis).** The per-driver gated
   sweeps repeat **N≥3 times at the identical position** (the σ(f)
   repeatability input) and every sweep runs **past the analysis band**
   (~22–24 kHz at 48 k fs with proper fades) so deconvolution edge
   artifacts fall outside anything analyzed. The analysis then fits each
   driver's linearization curve under the **correction envelope** (below),
   **then** computes integration (trim/delay/polarity) against the
   LINEARIZED branch responses. Linearization *aims* to extend ~an octave
   past Fc so acoustic slopes approach textbook LR, but only
   opportunistically within the envelope and boost caps — for a driver
   rolling off AT Fc, full through-region flattening is unreachable within
   safe boost, and **empirical integration on the actual responses remains
   the backstop**; textbook slopes are never assumed. The candidate grows a
   `linearization` member; re-runs refit atomically; profiles without the
   artifact stay valid (absent = no stage emitted).
2. **APPLY (one more emitted stage).** The household explicitly posts the
   accepted candidate. Baseline emission then gains one per-role linearization
   filter stage, with the same transaction and safety posture (non-positive
   gains + headroom accounting).
3. **VERIFY (same capture, more claims — three honesty levels).** *Fit* to
   the envelope, *verify* roughly an octave above the fit band's top, and
   *observe/report* to 20 kHz — the top octave appears in the technical
   disclosure as the driver's measured natural response, never as a
   pass/fail. Verification itself splits by target: **gated per-driver and
   summed checks verify against FLAT; in-room (Layer 3) verification uses a
   downward-sloping target (~1 dB/oct Harman-class, directivity-aware,
   user-adjustable)** — the target is an explicit parameter of the verify
   function, never a shared default (research artifact 03, claim K: an
   in-room check against flat over-brightens every result). Closed-loop
   linearization verification (achieved-vs-predicted per band, back off
   divergent bands, ≤2 iterations) rides the same explicit-apply→re-measure
   machinery and is the mechanism that turns every contested modeling
   question into a per-session empirical test.

**The correction envelope.** No hardcoded ceiling. Per driver, per session,
per frequency bin:

```
allowed_depth(f) = min(
    mic_trust_limit(f, tier),      # prior: artifact 01's metrology table
    repeatability_limit(f, σ(f)),  # measured across the N in-capture repeats
    linearity_limit(f),            # two-level test (extends existing pilots)
    invertibility_limit(f),        # excess-phase ADVISORY — build last
    class_prior_limit(f, class)    # artifact 02 §5 driver-class table
)
```

**Two further terms join that `min` when — and only when — the session has
a spatial capture cloud.** `spatial_exclusion_limit(f)` zeroes
allowed depth on the merged honesty mask (the combiner's power-vs-median
screen ∪ the identified-null registry), which is how the plan's "no EQ of
interference-flagged bins, ever" reaches the fit; `position_stability_limit(f)`
shrinks allowed depth where the cloud's cross-position band levels disagree,
reading the standard error `σ_band/√N` through the *same* σ→depth mapping and
per-tier tolerance table `repeatability_limit` uses. Both are optional, both
can only narrow, and a per-driver session with no cloud composes exactly the
five-term envelope above. Their two reason codes
(`LIMITED_BY_SPATIAL_EXCLUSION`, `LIMITED_BY_POSITION_STABILITY`) join the
same closed vocabulary. The exclusion is applied *after* the smoothing pass
rather than inside it, so masking a null cannot bleed correction depth out of
the correctable response beside it — see `compose_envelope`'s docstring in
[`jasper/active_speaker/linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py)
for the measured counterfactual.

Correction is clamped to the envelope, which tapers smoothly (no cliffs).
Cold-start mic-trust priors, per tier (`_MIC_TRUST_TABLE_HZ` in
[`linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py)):
reference full-correction-to/taper-zero 12 kHz/20 kHz, consumer 6 kHz/12 kHz,
phone 3 kHz/8 kHz. **Evidence can EARN depth beyond the priors** (clean
measured excess phase plus closed-loop verification passing), **but never
beyond what the measurement chain resolves: the repeatability and mic-trust
terms always bind.** No class-table row is permission. Fitting policy:
cut-preferred / normalize-downward (spend the sensitivity headroom — this
IS the existing non-positive-gain posture); boosts are capped per filter at
`MAX_LINEARIZATION_BOOST_DB` (12 dB, decision 4) with no separate global
boost ceiling — total boost is deliberately unbounded at the fit-engine
level, none above the envelope, none near horn cutoff or into flagged
nonlinear/excess-phase bands; cuts are generous (−12 dB, Q≤8); smoothing
widens with frequency (1/6 oct to 4 k → 1/3 oct to 10 k → 1/2–1 oct above);
fit-against-smoothed / verify-against-less-smoothed. Every band emits a
reason code (`FITTED`, `LIMITED_BY_REPEATABILITY`, …) — the same
honesty-guard culture as the acceptance gates, per frequency.

**Consistency without extra user steps:** σ(f) comes from the in-capture
repeats — no extra taps. The tolerable-σ thresholds behind
`repeatability_limit` are 0.5 dB (reference tier), 1.0 dB (consumer), 1.5 dB
(phone) (`_SIGMA_TOLERABLE_DB` in `linearization_envelope.py`). The woofer's
low edge honestly stops at the gate validity floor (~150–200 Hz here); below
that is Layers 2–3 by contract. Do NOT average across mic positions for
linearization — position-averaging is Layer-3 practice and smears genuine
on-axis HF detail here.

**Measurement instrument, in one paragraph:** the gated far-field sweep at the
listening axis is the Layer-1 instrument. The analysis finds the direct arrival per driver,
windows the IR before the first strong reflection (adaptive per capture —
~7 ms on the JTS3 rig, i.e. a ~143 Hz validity floor via `f_valid = 1/T`),
and claims nothing below the floor; VERIFY refuses comparison when its own
gate is forced shorter than MEASURE's. Near-field is a supplement, never
the instrument: valid only where the driver is acoustically small, so it
may extend the WOOFER's linearization below the floor via the classic
near-field-splice — while integration and horn linearization must stay
far-field (near-field destroys inter-driver geometry, and a horn's response
does not exist at its mouth). Below the near-field splice's own limits,
bass and room layers own the problem with in-room instruments.

### CD-horn compensation — the top-octave HF stage

A tweeter-on-a-horn's falling top octave is the **horn's constant-directivity
rolloff, not driver mass** — a broad, real, EQ-able trend **sized from
measurement, not from the driver class**. **Owner ethos: no subjectivity** —
sizing is measurement; where measurement runs out, a declared-driver-type
continuation policy takes over, disclosed as such.

The stage (`_hf_continuation_stage`,
[jasper/active_speaker/linearization_fit.py](../jasper/active_speaker/linearization_fit.py))
runs AFTER the flattening peaking loop:

- **Confidence ceiling from mic trust.** The ceiling is the mic-trust term's
  taper-zero (reference tier: 20 kHz); the knee is where its taper begins
  (reference tier: 12 kHz — "Cold-start priors" above). Eligible only when
  the fit band reaches the ceiling region (`fit_hi ≥ knee`) — woofers/mids
  fall out with no per-role branch.
- **Repeat-agreement gate (objective, replaces judgment).** Per-bin spread
  across the capture's repeats must stay under a tier tolerance, else the
  stage is suppressed (`repeat_disagreement`); fewer than 2 repeats →
  `insufficient_repeats`.
- **Class-blind sizing (measured inverse).** C(f) = max(0, target − working)
  over [onset, ceiling], rescaled to `spend = min(measured deficit, remaining
  budget)`. Identical for two hold-class drivers on the same curve — the
  mic-trust taper dominates the per-bin cap in the taper region, so the class
  never touches the sizing. `measured_deficit_at_ceiling_db` reports the
  UNCAPPED deficit so a budget-bound partial correction stays visible.
- **Cut-domain realization + give-back.** cut_target = C − spend (≤ 0
  everywhere) is realized with a Lowshelf backbone near the onset + peaking
  cuts in the TRUSTED band; the top octave gets no filter. Cutting everything
  below the compensation region by `spend` lets the flow's trim give-back
  level the branches back, raising the top octave RELATIVELY — the acoustic
  lift with cut-only (hardware-safe) filters. A fit-quality gate suppresses a
  mis-shaped correction. The shelf's own gain is CLAMPED at the 12 dB
  per-filter cut cap (decision 4) — a hard per-filter invariant that the
  larger total budget below may exceed; when spend is deeper, the peaking
  residual absorbs the remainder.
- **Plateau vs taper by declared type — the class's ONLY authority.** Above
  the ceiling nothing is measurable, so correction must not RISE.
  `HF_CONTINUATION_POLICY`: **hold** (compression horn, soft/beryllium/diamond
  dome, ribbon/AMT) keeps the lift constant; **taper** (metal dome, unknown)
  appends one trailing Highshelf CUT that walks the lift back down over the
  unseen band. Unknown → taper is the conservative default.
- **A max-SPL ledger.** `MAX_NORMALIZATION_SPEND_DB` (18 dB) bounds
  (plateau − target) + spend so the stage can reach a real measured deficit.
  The spend drops the system's absolute ceiling by ~spend (ordinary
  listening recovers via the volume knob) — it is NOT a listening-level
  cost, and it is disclosed.
- **Single-shelf realization ceiling.** The spend is bounded by three
  independent ceilings: the measured deficit, the ledger budget above, and
  `HF_SINGLE_SHELF_SPEND_CAP_DB` (11 dB) — what ONE Lowshelf plus bell
  residuals can actually realize on a real curve. The last ~3 dB toward true
  tabletop flatness needs a different REALIZATION, not a bigger number — a
  stacked-shelf realization or a literal-boost realization once closed-loop
  verify can bound a boost claim, neither built.
- **Anchored give-back (the trim).** Each branch's linearized trim is its own
  COMMITTED raw trim plus that branch's **level-band give-back**: the
  measured before-vs-after level delta read by `solve_branch_trims` over
  `branch_level_bands_hz` — the same estimator, averaging domain, and bands
  that solved `raw_trim_db` and that `realized_level_match` uses to grade the
  committed pair. A shared shift then normalizes the pair non-positive so a
  branch whose give-back exceeds its raw attenuation can never become a
  boost. The invariant: a give-back spent against a trim must be measured in
  that trim's frame, so the committed pair lands level by construction
  (`realized = (level_t_pre − level_w_pre) + (raw_t − raw_w)`, which `raw` is
  defined to zero) — restoring equality between the branches at the handoff,
  not a monotone reduction in level; a branch whose correction lives inside
  the graded band can legitimately commit **hotter** than before, still under
  the non-positive clamps.

  The invariant has a precondition: the give-back is the right adjustment
  only for a base that came from the same solve. The MEASURE path may instead
  hand over the **ripple-polished** tweeter trim (`solve_ripple_optimal_trim`,
  a flatness choice), admitted only while it sits within
  `REALIZED_LEVEL_MATCH_TOLERANCE_DB` (3.0 dB) of the band average — the
  level gate's own tolerance, not a separately picked number, so a polish
  legal by its own guard cannot push the pair past the level gate. A
  rejected polish falls back to the band-average seed, disclosed; the
  polish delta is published on every round (`polish_delta_db`).
- **Guard.** The wild-trim guard in `crossover_v2.intervention.decide_trim`
  ([crossover_v2_flow.py](../jasper/active_speaker/crossover_v2_flow.py))
  measures the ripple scan's drift from the give-back anchor and falls back
  to the anchored pair — never raw + emitted filters — beyond that drift
  bound. The anchor is measured give-back, not a prediction, so only the
  scan can drift. Magnitude protection lives in the fit engine's structural
  caps (per-filter 12 dB, total budget, realization tolerance) plus the
  VERIFY gate.

Disclosure: octave centers above the ceiling report
`envelope_beyond_measurement_confidence`; beyond the ceiling the lift is
declared best-effort, never a measured claim.

## The region-based adjustment contract

**Why a region-based contract, not a bigger trim.** A scalar level knob
cannot fill a localized notch: the per-driver instrument is blind exactly at
the crossover (each driver's fit sees only its own branch, not the summed
dip that only the alignment/crossover layer can see —
`_blind_zone_placements` in
[`jasper/active_speaker/linearization_fit.py`](../jasper/active_speaker/linearization_fit.py)
reports this rather than refusing), no per-driver lever reaches a dip that
sits between the two bands, and a whole-driver trim is the wrong shape for
a notch regardless. Fixing this needs a correction owner that can see the
summed response, not a larger per-driver correction.

**The contract.** The frequency axis divides into regions with different
measurement trust and therefore different allowed tools. This is what ANY
prescriber consumes — the regions, the vocabulary, and the bounds are the
contract; the code that fills them in is not.

**(a) Inside each driver's own band, away from the crossover.** Per-driver
linearization under the correction envelope. Existing, unchanged — Layer 1a
above owns it.

**(b) The crossover/blend region — summed-response-owned.** Per-driver
fitting stays instrument-blind here by design. What changes is the owner: the
summed at-the-mark measurement *is* trusted in this region and sees the dip
at every position, so it owns **bounded shape correction** there. The initial
posture is **cuts-first**. Every existing safety cap is unchanged, the
verification is the same summed verify, and the outcome is banked in the same
receipts. This is a change of owner and of allowed tool — not a new safety
class, not a new instrument, and not a new flow.

**(c) Level, alignment, and Fc keep their own tools.** Level stays a scalar
per-driver trim. Alignment (delay/polarity) and Fc selection stay their own
tools with their own evidence. Nothing about the safety class changes.

**What this contract does not decide.** The HOW of (b) — filter form, band
edges, how much depth the summed evidence earns — is left open, inside these
boundaries. One named prerequisite: reading per-role quantities out of a
summed capture needs a frame-coherence condition, because the summed
capture rides the applied incumbent graph while per-branch sweeps ride the
protected-neutral one.

### The prescriber seam

The contract above is deliberately defined without saying who prescribes.

- **Deterministic forever:** the harness. Measurement, the safety clamps,
  keep/restore on measured evidence, and receipts carrying
  commanded-vs-realized per band. None of this becomes a model's judgement
  call, ever.
- **Pluggable:** the prescription *policy* — what to try next, given the
  banked trend. Both a deterministic trend engine and an LLM prescriber
  exist behind the same deterministic validators
  (`jasper/cli/crossover_prescriber.py`,
  [`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py),
  [`evidence_packet.py`](../jasper/active_speaker/crossover_v2/evidence_packet.py)).
  [`tuning-master-plan.md`](tuning-master-plan.md) is the planning authority
  for the LLM-prescriber shape.

The open question is not *who prescribes* — both exist — but *what the
prescriber is allowed to shape* and *when it may act at all*, which is
stage **P3** of
[the linearization pipeline](#the-linearization-pipeline--seed--crossover-science--eq).

## Measurement Program v2 — the capture schema

**Status: schema ratified; the position-major capture schedule it was
designed for was never built.** A measurement is **(stimulus regime) ×
(angle) × (level)**. Angle is an **attribute** of a capture, not an
identity: every capture banks as `{regime, angle_deg, repeats,
level_re_anchor}` (decision 14). Two stimulus regimes cover the grid:
**D — per-driver** (each driver swept alone; repeats stay 3 — the
linearization eligibility gate's `LINEARIZATION_MIN_PAIRED_OCCURRENCES`, the
envelope's repeatability-σ composition, the HF agreement gate's
`_HF_MIN_OCCURRENCES`, and `MEASURE_REPEAT_COUNT` all read that same floor,
so it is not a schedule knob) and **S — summed** (one sweep through the
applied or candidate graph).

**The pose set this schema was designed around — 0°, ±7°, ±22° — was never
built and is superseded by what shipped.**
[tuning-master-plan.md](tuning-master-plan.md)'s "Measurement program
constants" is canonical for the pose set and repeat structure that actually
runs (the `baseline` program in `measurement_programs.py`: 13 poses,
`ANCHOR_REPEATS = 4`). Angle stays degrees everywhere.

Elevation is sampled by neither the schema's design nor the shipped program.
The household string-and-protractor method reads azimuth only; a vertical
extension needs its own rig-support answer and household technique before it
needs a pose schedule.

## The linearization pipeline — seed → crossover science → EQ

> **Status: ratified design; one stage exists, two are partly built.**
> **Every stage below carries an explicit STATUS label — EXISTS / IN FLIGHT /
> MISSING — and nothing outside an EXISTS label describes what the speaker
> does today.** Where a stage states shipped behaviour it names the symbol it
> was read from. Read everything else as ratified plan. Operational truth
> for the shipped flow stays
> [tuning-operator-runbook.md](tuning-operator-runbook.md).

### The ruling

Three stages, run in order. In the owner's framing it is **BOTH-AND, strictly
ordered** — not a choice between tuning the crossover and applying EQ:

1. **P1 — seed.** The operator enters driver information; the flow derives
   basic trims and a basic crossover placement from it.
2. **P2 — crossover science.** The crossover is tuned with maths, science, and
   experiment until there is high confidence it is as good as it is going to
   get.
3. **P3 — EQ.** *Only then* does EQ iron out the rest, across the entire
   trusted measurable region, to super flat.

**Why the order is the ruling and not a preference.** EQ can flatten a
magnitude error whatever caused it, which is precisely the hazard: a filter
spent hiding a summation error that a still-free crossover parameter could have
removed is aimed at the wrong cause, costs headroom permanently, and — because
the correction is now baked into every subsequent measurement — removes the
evidence that the parameter was ever wrong. Ordering the stages keeps each
lever answerable for its own class of defect. It is the same separation the
five-layer model already enforces between shape and level; P1–P3 apply it along
the *time* axis of a commissioning session.

### How the stages map onto the five layers

This is not a second taxonomy. The pipeline is the **tuning order of the layers
already named above**, and each stage's contract is owned where it always was:

| stage | decides | owning layer / contract | status |
|---|---|---|---|
| **P1** seed | initial Fc, protection posture, polarity, geometry-bounded delay, first trims — all from declarations, no audio | Layer **1b**, from the component-entry declarations and decisions 8–9 | **EXISTS** |
| **P2** crossover science | the final non-EQ parameters: polarity, per-branch delay, Fc, slopes/order, branch gains | Layer **1b** again — contract **(c)** reserves all but slopes/order, which is 1b's own | at-mark substrate **EXISTS**; per-angle replay is **operator-driven** (no automatic schedule); search + guards **MISSING** (4 named gaps) |
| **P3** EQ | the minimum-phase residue across the whole trusted band | Layer **1a** per-driver, plus the blend region's summed owner under contract **(b)** | **partially EXISTS** |

Layers 2–4 (bass, room, preference) are untouched by this ruling: the pipeline
runs entirely inside the speaker layer, and its output is the flat device the
room layer then corrects for a position.

### Stage P1 — seed from driver knowledge

**STATUS: EXISTS.** This is the one stage that runs today, end to end, with no
measurement involved.

The operator enters driver information — the component-entry surface,
persisted by
[`design_draft.py`](../jasper/active_speaker/design_draft.py), which by its own
contract "records what the operator is trying to build and any externally
researched driver facts" and deliberately does not compile filters or authorize
playback. The externally-researched half arrives through the driver-research
prompt in
[`driver_safety_prompt.py`](../jasper/active_speaker/driver_safety_prompt.py),
against the request and result contract owned by
[`driver_safety.py`](../jasper/active_speaker/driver_safety.py)
(`driver_research_targets`, `validate_driver_research_result_shape`,
`finalise_research_result`), which is decision 9's rule in code: it asks for the
manufacturer's **published** facts and nothing else, and a derived margin is
never smuggled into a datasheet field.

From those declarations the seed is computed deterministically by
[`crossover_preview.py`](../jasper/active_speaker/crossover_preview.py)
(`build_crossover_preview`) — "the deterministic bridge from a saved design
draft to a future protected startup config… bounded filter intent only: no
CamillaDSP YAML, no config load, no playback authority, and no sound." What it
seeds, and from what:

- **Fc, out of the driver's safety envelope.** The tweeter's minimum
  recommended crossover frequency *with its slope condition* is decision 8's
  single declared owner, resolved by
  [`driver_protection.py`](../jasper/active_speaker/driver_protection.py)'s
  `resolve_driver_low_limit` / `declared_protection_highpass_floor_hz` off the
  `recommended_highpass_hz` field the operator's research response fills in.
  `crossover_preview.SCHEMA_VERSION` is 2, so a preview saved before the
  collapse to one owner — carrying un-derived driver payloads — cannot be
  reused. The woofer's breakup ceiling and the horn's coverage bound the
  choice from above **as acoustics**, but only the first of the two has a
  declared field the code reads: `radiating_diameter_mm` feeds a ka-beaming
  hint (`branch_chain.beaming_onset_hz`); coverage rides the driver notes as
  operator prose (no structured field).
- **A protection slope, not a crossover order.** Worth separating, because the
  two are easy to conflate: the *crossover* filter's type and order are
  declared, not derived. What the low limit's slope condition derives is the
  **protective** high-pass floor, `PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE` in the
  same module. P1 seeds a protection posture; choosing the crossover's order
  against measurement is P2's.
- **Polarity, and a geometry-bounded delay.** Declared geometry does not
  produce a free delay guess; it produces a **bound around a seed** —
  `null_walk.geometry_seed_us` converts the signed path difference into
  microseconds, `delay_sweep.sweep_spec` bounds "one
  driver-to-driver walk from an a-priori geometry estimate", and
  `measured_candidate.py`'s input contract fixes `delay_bound` at
  `declared_geometry_plus_minus_half_period`. The acoustic-centre provenance is
  explicitly operator-attested
  (`commissioning_evidence.RegionGeometryAttestation`). So the physical offset
  sets the window; the value inside it is P2's, from measurement.
- **First trims,** from declared sensitivities and the declared in-line pad
  ([`driver_pad.py`](../jasper/active_speaker/driver_pad.py),
  `effective_sensitivity_db`).

**What P1 is not.** It is a *starting point*, not an answer, and this stage's
one honesty rule is that nothing it produces is ever reported as measured. The
seed is an intent artifact with no acoustic evidence behind it, which is exactly
why P2 exists.

### Stage P2 — crossover tuning by measurement

**STATUS: method ratified; the at-mark measurement substrate EXISTS,
per-angle replay RUNS when an operator stages a walk, there is no automatic
per-angle schedule, and the search and its guards are MISSING.** No
crossover parameter is chosen by measurement today. The per-driver complex
capture this stage consumes is already shipped **at the mark** (see "how
much of it already exists" below), so the gap is narrower than "P2 is
unbuilt" suggests. Off the mark, an operator can capture per-driver
responses at stated angles as **forward-model input**:
[`angle_capture.py`](../jasper/active_speaker/angle_capture.py) resolves
`{per-driver | summed} x {angles} x {arm | human-guided}` onto the shipped
program, pose and gate machinery; `jasper-angle-capture`
([`jasper/cli/angle_capture.py`](../jasper/cli/angle_capture.py)) is the door
that stages a walk, banked in a single-use mailbox
([`angle_capture_spool.py`](../jasper/active_speaker/angle_capture_spool.py))
for the next `/correction/crossover/v2/session` open to take as its lateral
group, banking every accepted pose's raw WAV with an angle-stamped sidecar.
There is no automatic off-axis capture — nothing captures off-axis unless an
operator stages a walk — and the angle SCHEDULE a search would need is still
something a session has to be told, not something it produces.

**The goal, stated as a stopping condition.** Drive the non-EQ parameters —
polarity, per-branch delay, Fc, slopes/order, and branch gains — to the point
where there is high confidence they are as good as *these drivers in this
cabinet* permit, and only then unfreeze EQ. Four of those five are the tools
region contract **(c)** above reserves to themselves — it enumerates level,
alignment (delay and polarity), and Fc. **Slopes/order is not in (c)'s
enumeration**; its owner is Layer 1b's own job description at the top of this
doc ("drivers sum correctly: crossover filters, **scalar** trim per driver,
relative delay, polarity"). So P2 is what (c)'s "their own evidence" turns out
to require, plus the one lever (c) never names.

**The measurement it needs, and how much of it already exists.** Per-driver
**complex** responses — magnitude *and* phase — at every angle. `build_measure_program`
([`program.py`](../jasper/audio_measurement/program.py)) schedules the woofer
and tweeter sweeps **non-overlapping inside ONE capture**, routed by channel
("ch0 → woofer output path, ch1 → tweeter output path" — its own docstring —
"per-driver sequencing lives in the WAV channels so the CamillaDSP
commissioning graph stays static and provable"). Consequences: per-driver
**complex** transfer functions are produced and direct-arrival gated
(`DriverResponse.complex_tf` in
[`program_analysis/`](../jasper/audio_measurement/program_analysis/)); the
two drivers share an **exact** common time origin (same capture, so there is
no cross-capture alignment problem for the A/B pair — that is a separate
question from the intra-capture desync gap 1 below records); and in-capture
drift is estimated (`DriftEstimate`), with the drift-corrected
woofer-versus-tweeter anchor shipping as `anchor_delay_us`. Off the mark,
per-angle per-driver capture is BUILT and OPERATOR-DRIVEN — a pose "replays
MEASURE's program"
([`spatial.py`](../jasper/active_speaker/crossover_v2/spatial.py)) — but
nothing captures off-axis automatically; **an implementer scoping P2 should
read the angle schedule as work, not as a given.**

**Branch muting is not the route, and must not be proposed as one.** A
commission-mute overlay would break the graph classifier:
`protected_neutral_program_origin`
([`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py)) accepts the
program origin only when every commission-mute filter is exactly pass-through
(`{"gain": 0.0, "inverted": False, "mute": False}`) and returns `False`
otherwise. The interleaved-channel design exists precisely so the commissioning
graph can stay static and provable; muting a branch trades that away for
something the one-capture schedule already provides.

**What a timing pilot buys.** A **self-referencing acoustic timing pilot** —
the DUT's own tweeter playing a short chirp whose arrival is the reference —
would give two things, neither of which is the A/B common origin (already
solved above): a sharper slip estimator on the existing capture path (gap 1
below), and any future genuinely **cross-capture** comparison such as
session-to-session absolute phase. Its accuracy bar is loose in a useful
way: what a slip measurement needs is **repeatability, not correctness** —
the pilot path is the same physical path on both sides of any comparison, so
multipath bias cancels and only its **variance** enters the error budget. Not
to be confused with the **behavioural-linearity pilot** pair
(`DEFAULT_PILOT_DURATION_S`) every MEASURE/VERIFY capture opens on, whose job
is proving the chain responded linearly — a timing pilot is an arrival
reference, a linearity pilot is not designed, placed, or gated for that, and
the two must never be conflated. The **summed-capture self-consistency
solve** — recovering the offset that makes the separately measured branch
responses sum to the measured summed response — is an independent
cross-check either way, not a replacement for either pilot.

**The search.** An offline **complex-summation forward model** predicts the
summed response from the per-driver complex responses and a candidate parameter
set, computed with **the same biquad math CamillaDSP runs** — a model that
disagrees with the shipped filter realization is measuring its own arithmetic.
Its objective must be **commensurate with the on-device grade**: same smoothing,
same pooling, same frozen baseline reference (rule 5 of P3 below), so that a
predicted win and a measured win are the same quantity. The search is an
**outer discrete enumeration** (filter type, order, polarity) wrapped around an
**inner continuous optimization** (Fc, Q, per-branch delay and gain), bounded
throughout by the declared driver-safety envelope — P1's declared low limit with
its slope condition is a hard wall, not a starting guess.

**The on-device confirmation schedule**, coarsest lever first: **polarity →
delay → Fc → slopes → Bayesian refinement**. Each step carries a
**pre-registered acceptance** in the same shape P3 uses — a prediction banked in
the grading view's own units *before* the change is played, **2–3σ** on
frozen-reference pooled views, **≥3 pooled repeats**, and **rollback on loss**.
A **sim-to-real discrepancy term** is calibrated on **≥3–5 real trials before
the model's predicted signs are trusted at all**: this program's model
predictions have been anti-correlated with measurement more than once, so the
forward model earns its sign empirically or it does not get a vote.

**The exit criterion.** **K ≈ 3–5 consecutive rounds with no statistically real
improvement** freezes the crossover. Freezing is the event that hands control to
P3. An unfrozen crossover is a reason for P3 *not to run*, never a thing for P3
to compensate.

**What does not exist — the honest inventory.**

1. **A sharper capture-integrity slip estimator — MISSING.** The per-driver
   timed capture it would guard already EXISTS. The gap is the **sensitivity
   floor of the guard on the capture path that already runs**: today's desync
   guard rejects a 4-sample silent slip and passes a 2-sample one (pinned in
   [`tests/test_audio_measurement_program_analysis.py`](../tests/test_audio_measurement_program_analysis.py),
   `test_desync_guard_keeps_its_teeth_after_d7`), and at 48 kHz 2 samples is
   41.7 µs — over twice the 20 µs relative-phase budget (gap 4) — so a
   capture carrying a phase error twice the whole budget passes the gate
   clean. The fix-shape is a sharper slip estimator on the existing capture
   path, explicitly **not** a new capture mode and **not** branch muting.
2. **Vertical polar capability — MISSING.** The crossover's primary artifact
   is **vertical** lobing, and this rig measures horizontal angles only — the
   measurement program samples zero vertical offsets, the household
   string-and-protractor method does not generalize to elevation, and the lab
   arm's elevation capability is undetermined. P2 is the consumer that turns
   that gap from tidy-later into blocking.
3. **The forward model — EXISTS** (`crossover_v2/forward_model.py`:
   `SummationCandidate` / `BranchPair` / `predict_sum`) as offline
   **simulated evaluation**: corners declared by the operator, and the
   predictor saying what a variation of one would measure, at zero capture
   cost. It has no ranking/optimization search over it — it needs an
   objective in the grade's own currency and a delay axis graded against
   measurement, neither of which exist.
4. **A Stage-0 timing acceptance test — MISSING.** Pass bar: relative-phase
   alignment residual ≤ 20 µs (3σ) — the ~15° at 2 kHz that a ±0.5 dB
   summation prediction near Fc can absorb. No implementation of that test
   exists in the tree, and the bar sits at the same order as the per-role
   integer-sample alignment quantization on a 48 kHz chain (±20.833 µs) — a
   quantization floor this close to the acceptance bar can consume the whole
   budget before the timing pilot's own estimator contributes anything.
   Nothing downstream should be built until the test passes on a
   de-quantized measurement.

### Stage P3 — EQ the minimum-phase residue

**STATUS: partially EXISTS.** What ships is the machinery *around* the
decisions — the fitting engine, the safety clamps and their bounds, the pooled
grading views, the predict-apply-remeasure-rollback protocol, and both
prescribers, all of which have run on hardware. What does not ship is the part
that decides: taking **"is there code in the tree that makes this rule's
decision?"** as the test, **two of the six rules below fail it outright and
three more pass only on the prescribed path** (rule 6 is a review discipline
rather than code at all), and the stage's *scope* is narrower than this ruling
requires. The per-rule table at the end of this stage makes that count
reconstructable. Read "partially" strictly.

**When it runs.** Only after P2 freezes the crossover — not before, not
alongside.

**What it covers.** The **entire trusted band**, roughly **357 Hz to 20 kHz**
on this rig — a gate-derived floor (`gating.f_trusted_floor_hz`, `2.5 /
window_s`, so 357.14 Hz at this rig's 7 ms gate) and a mic-derived ceiling
(`linearization_envelope.mic_trust_limit`'s taper zero, 20 kHz on a
`reference` mic) — not merely the crossover window. The only **shared** EQ
stage that exists today is the blend stage, safety-reviewed for the
crossover neighbourhood alone; Layer 1a's *per-driver* linearization EQ is a
separate shipped stage that does reach the emitted graph
([`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py),
`fit_driver_linearization`) — which is why rule 3 below is about *which* of
the two owns a given defect, not about acquiring the first one. Extending
correction across the full trusted band is the scope this stage ratifies,
and it is unbuilt.

**The rules.**

1. **Classify before correcting; EQ only defects.** Every feature is typed
   first — a controls-verified **excess-group-delay minimum-phase test**
   (with positive and negative synthetic controls pushed through the
   identical pipeline), a **gate-invariance** check, and **cross-angle
   behaviour** — and only minimum-phase, speaker-own defects are eligible.
   Interference, beaming, and room features are **barred**, the same
   refusal the correction envelope's `spatial_exclusion_limit` term already
   encodes for the per-driver fit. **Status: the instrument ships** as
   [`crossover_v2/feature_classifier.py`](../jasper/active_speaker/crossover_v2/feature_classifier.py)
   (`jasper-round-views classify-features`), which runs the
   excess-group-delay test, the gate-invariance check against a matched-Q
   null model, and the timing-scatter test over one round's banked
   captures, and refuses to emit a verdict at all unless its known-answer
   controls pass. It is an OFFLINE run over a banked round rather than a
   stage of one, so a round carries verdicts when somebody classified it.
   What it cannot do is the vertical plane: every capture shape it reads is
   horizontal, so no verdict it emits has ever been sighted off that plane
   — disclosed in the evidence packet's `not_evaluated` block. `PositionalSupport`
   in
   [`blend_prescription.py`](../jasper/active_speaker/crossover_v2/blend_prescription.py)
   remains the cross-position half for the BLEND class.
2. **Match filter width to feature width.** A filter's Q should follow a
   feature's own measured width, not a fixed clamp — a filter much wider
   than its feature under-corrects the centre while damaging already-good
   neighbours. **Status: not built.** The prescriber's width bound is a
   single scalar, `BLEND_FILTER_Q = 2.0` in
   [`blend_correction.py`](../jasper/active_speaker/crossover_v2/blend_correction.py),
   applied to every feature regardless of its measured width; a banked
   feature's `measured_q` is reported to a prescriber in the packet's
   classification block, but nothing in the tree chooses a Q from it.
3. **Correct in the branch that owns the defect.** A per-driver defect gets
   a per-driver filter in that branch — Layer 1a's existing per-role stage
   — and the shared stage is reserved for genuinely system-level shaping. A
   shared filter is the wrong instrument for a one-driver problem, and it
   charges both branches for it. **Status:** ships for the **prescribed**
   path — the two classes have separate gates, bands and candidate fields,
   so a per-driver defect can only reach `linearization` and a region-wide
   one can only reach `blend_correction`; the **deterministic** path makes
   no such routing decision.
4. **Cuts are bounded and free; boosts pay an evidence bar.** Cuts ride the
   existing caps and the cut-preferred posture unchanged. A boost requires
   a **minimum-phase dip**, **multi-angle testimony**, and an **excursion /
   thermal / harmonic-distortion budget** showing the driver can spend it.
   **Status:** the SPEND half ships on the per-driver prescribed class — a
   per-role composed budget bounding maximum-SPL spend at 13.0 dB, plus the
   per-filter caps and the crossover-knee bar
   ([`driver_prescription.py`](../jasper/active_speaker/crossover_v2/driver_prescription.py));
   Layer 1a's boost bounds ship and are enforced in `runtime_contract.py`
   (`MAX_LINEARIZATION_BOOST_DB`). The blend stage's own multi-condition
   evidence bar for a boost has not been exercised.
5. **Grade against a FROZEN baseline reference.** Referencing each
   configuration to its own average is invariant to level and therefore
   **flatters broadband cuts** — the cut lowers its own reference too, so
   it partially forgives itself. Freezing the reference to the *baseline*
   configuration is the honest comparator. Predictions are **pre-banked in
   the grading view's own units**, and anything that measures worse is
   **rolled back**. **Status: SHIPS.** `evaluate_flat_spec`
   ([`flat_spec.py`](../jasper/active_speaker/flat_spec.py)) takes an
   explicit `reference_db_override: float | None = None` parameter,
   threaded through
   [`flat_spec_views.py`](../jasper/active_speaker/flat_spec_views.py)'s
   `_evaluate_position`;
   [`round_views.py`](../jasper/active_speaker/crossover_v2/round_views.py)'s
   `frozen_reference_grade` is the product caller, operator-facing as
   `jasper-round-views frozen` — it grades a target round both shipped
   (self-referencing) and frozen to a baseline's per-position reference
   levels.
6. **Respect the audibility floors.** Broad, low-Q deviations are worth
   correcting down to roughly **0.5–1 dB**; a **narrow** feature must be
   several dB before it earns a filter at all; and nothing below the
   **session noise floor** is a target. The floor is measured per session,
   not assumed. **Status:** a review discipline, not code either way.

**What "partial" is buying, said plainly.** A bar that refuses cannot make a
round better, it can only stop one specific way of making it worse. Rule 1's
bar turns "a reader believes this is a driver defect" into "a classifier
said so and the verdict is on the receipt"; it does not classify anything.

**Two rulings the prescribed path forced, recorded here because they are
stage decisions rather than module details.**

*The nearest verdict decides.* Rule 1's bar needs a rule for matching a
filter's centre to a classified feature, and "any eligible verdict inside
the match radius vouches" is the wrong one when a dip and a peak sit closer
together than the match radius — a cut aimed at the dip could borrow the
neighbouring peak's verdict and get accepted, deepening a minimum-phase dip.
The rule is therefore the ordinary one for a claim about a frequency: **the
closest claim owns it**, and the tolerance's only job is to absorb the
evidence's own locating error.

*Merge by role.* Rule 3 routes a per-driver defect into the
role-keyed Layer-1a field, which then has two producers: the fit writes every
eligible role, a prescription names a subset. Three options, and the ruling is
the third:

| option | what it does | verdict |
|---|---|---|
| replace wholesale | a document's roles become the whole field | **rejected** — a one-role document silently discards the other driver's *fitted* filters |
| compose (append) | prescribed filters added to fitted ones | **rejected** — doubles corrections at a shared target and can breach the eight-filter branch ceiling from two authors, neither of whom sees the total |
| **merge by role** | named roles replace **their own** filters; unnamed roles keep the fit's | **adopted** — the only option under which "a role you do not name is not changed" is true, and it keeps one author per branch so the ceiling has one owner |

The seam implements the merge rather than documenting a protocol for its
caller, and its fit argument is required-and-undefaulted precisely because
forgetting it is the failure that looks like success until somebody measures.

### What this section supersedes

This section **supersedes nothing** in the five-layer model, the correction
envelope, the region-based adjustment contract, or the Measurement Program v2
schema — it orders them. This section is the single source of truth for the
pipeline; do not fork its content elsewhere.

## Composition & code seams

The config emitter already composes in the right order and most seams
exist empty: `emit_active_speaker_baseline_config`
([jasper/active_speaker/camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py))
emits per-role `[crossover, delay, baseline_gain, limiter]`; the
`/sound/` recomposition (`_recompose_active_baseline_with_eq` in
[jasper/sound/graph_carrier.py](../jasper/sound/graph_carrier.py)) already
threads `preference_filters` + `room_peqs` slots (audited live: currently
empty). Layer 1a adds a per-role linearization stage to the *baseline*
emission (owned by the speaker layer, NOT injected through the sound-profile
seam — different owner, different cadence). The measured tweeter/woofer TFs
that the fit consumes are already produced by every MEASURE
(`analyze_program_capture` → `DriverResponse` to 18 kHz with per-serial cal
applied) — the data pipeline needs zero new capture work for 2-way.

**The capture-source seam (decision 13).** The
capture → analysis layer contract is: one provider per source answers each
capture with WAV + metadata (mic identity, mic/cal identity reference, and
the frame-ledger integrity counters), the provider mints the session id the
durable state and evidence key on, and the host owns the mapping onto the
persisted failure codes — the provider speaks only the flow's reason
vocabulary. The contract itself lives in
[jasper/active_speaker/crossover_v2/capture_source.py](../jasper/active_speaker/crossover_v2/capture_source.py)
(do not restate it here); the relay provider was deleted (ADR-0222) and the
wired (Pi-mic) provider is the seam's occupant.

## Speaker-class applicability

Component entry declares the class; the class drives which layers' wizard
steps exist. Per-driver `driver_class`
(compression_horn/soft_dome/metal_dome/beryllium_diamond_dome/ribbon_amt/unknown
— `DRIVER_CLASSES` in
[`jasper/active_speaker/_common.py`](../jasper/active_speaker/_common.py))
feeds the correction-envelope's `class_prior_limit` term, which takes the
declared class and nothing else; the declared in-line pad
(`jasper/active_speaker/driver_pad.py`) feeds the effective-sensitivity
readers (`declared_effective_driver_sensitivities`); and
`radiating_diameter_mm` feeds a ka-beaming crossover hint in `/sound/`. A
waveguide's identity and rated coverage travel as operator prose in the
driver notes (no structured coverage field). **Still open:** the
SPEAKER-level class this table's columns describe (2-way / 3-way / passive)
is not yet driven by a component-entry step.

| Class | 1a | 1b | 2 | 3 | 4 |
|---|---|---|---|---|---|
| Active 2-way (today) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Active 3-way | ✓ ×3 | ✓ ×2 regions | ✓ | ✓ | ✓ |
| Passive | — | — | ✓ | ✓ (may absorb gentle speaker residuals) | ✓ |

## Microphone doctrine

Both household mics carry **per-serial** calibrations. The distinction that
matters is NOT per-serial-vs-generic; it is pedigree/uncertainty above
~8 kHz (two calibrated readings of the same horn can disagree by several dB
up top) and incidence-angle sensitivity (a mic's HF reading can shift when
physically handled). Rule: Layer-1a HF fitting requires the reference-tier
mic; consumer-tier mics remain integration-tier (1b) under the shipped
honesty gates. A future option with real leverage: deriving a unit-specific
**transfer calibration** for a consumer mic against the reference — a
"calibrate-your-cheap-mic-once" product story.
