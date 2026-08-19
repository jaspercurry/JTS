# Research brief: measurement-driven loudspeaker linearization at JTS

**Audience:** a deep-research agent tasked with finding prior art.
**Date:** 2026-08-19. **Status:** context document + research prompt, written
at the close of an overnight measurement campaign. Everything here is dated
evidence, not doctrine — where a claim is unconfirmed it says so.

---

## 1. What we are trying to do (the vision)

We build a DIY active loudspeaker (two-way: woofer + tweeter, DSP crossover
on a Raspberry Pi running CamillaDSP) and we want its **direct sound to be
flat**: every frequency reproduced at equal loudness, as measured by a
calibrated microphone, across a small window of listening angles. The bar
the owner set: *"flat like a ping-pong ball would not roll — but also honest
flat, no tricks."* Honesty means: pre-registered predictions, falsification
thresholds declared before playing a change, automatic rollback when a
change measures worse, and no metric choices that flatter the result.

The larger program is a layered cake: **(1) linearize the speaker itself**
(this brief), then **(2) correct the room**, then **(3) apply taste curves**.
We deliberately refuse to EQ room artifacts into the speaker layer.

## 2. The system under study

- Two-way active speaker ("JTS3", a lab unit). Signal chain: audio sources →
  DSP (CamillaDSP): digital crossover into two driver branches, per-branch
  time-alignment delay, a shared parametric-EQ stage (the "blend" stage),
  correction filters, protection/headroom management, volume ceiling →
  multichannel DAC → class-D amps → drivers.
- The crossover topology (filter type, slopes, **crossover frequency**) is
  fixed at commissioning time. The acoustic crossover region falls inside
  824–3297 Hz (the window our EQ stage is allowed to touch — see §4).
- Time alignment IS measurement-tuned: a −350 µs woofer delay was adopted
  2026-08-18 after winning on measurement (the model's prediction was
  anti-correlated with the measured outcome — a recurring theme).

## 3. Measurement methodology

**Rig.** Calibrated USB measurement mic (miniDSP UMIK-2, serial-fetched
calibration file) wired directly into the Pi. The mic sits on a motorized
turntable arm at constant radius from the speaker; angle is a controlled
attribute. Positions: 0°, ±7°, ±22° (center + symmetric pairs), measured
position-major, 5–10 s mechanical settle after each arm move.

**Stimulus.** Exponential sine sweep (ESS). Harmonic distortion is extracted
via the Novak/Farina method from the same sweeps (we have distortion data
banked but do not yet gate EQ decisions on it).

**Processing.** Impulse response via deconvolution; **7 ms reflection gate**
(the room is a ~10 ft cube; first strong reflection ≈7 ms), which limits the
trusted band to **~357 Hz – 16 kHz** (below 357 Hz the gate truncates; the
room has modes ~56 Hz). Magnitude smoothed at 1/12 octave.

**Known room artifact:** a comb feature near ~1 kHz was attributed in a
prior campaign (2026-08-18) to a reflection path with τ ≈ 4 ms that tracks
measurement geometry, not the speaker; we treat it as a room feature and
forbid EQ'ing it. A fresh-eyes re-examination this night attributed a
1006 Hz feature as speaker-own-or-vertical-bounce (pinned across 44° of
rotation; side-wall path excluded).

**Repeatability (this rig, this night):** 46 sweeps attempted, 45 clean, 0
integrity failures (the capture engine keeps a frame ledger and refuses
captures with gaps/zero-runs). Pooled-view repeat agreement 0.002–0.020 dB;
per-seat per-band values are ~10× noisier (session sd 0.21–0.47 dB).

**Grading (the score).** Per position: deviation = measured curve − that
curve's **power-mean level over 250 Hz–8 kHz** (i.e., the curve is
referenced to its own average), then evaluated against per-band tolerances.
Views: **on-axis pool (0°, ±7°) = the primary score**, off-axis pool (±22°),
plus log-pooled and lin-pooled aggregates. Units are dB RMS deviation;
session noise floor on pooled views ≈ 0.03–0.07 dB (2σ ≈ 0.057 on-axis).

**A hard-won metric lesson (2026-08-19):** referencing each config to *its
own* average is **exactly invariant to global level changes and it flatters
broadband cuts** — a cut lowers the reference too (measured 0.4–0.75 dB),
partially forgiving itself, and per-config "target-relative" tables inherit
the same flattery. Re-grading with the reference **frozen to the baseline
config's** is the honest comparator; under it, apparent off-axis
improvements from our cuts reversed into losses. Research question 2 below
asks what prior art does here.

## 4. Intervention methodology (how we change the speaker)

- **The knob:** parametric EQ (minimum-phase peaking filters) in the shared
  "blend" stage, acting identically on both driver branches, restricted to
  **824.35–3297.4 Hz** (the crossover neighborhood — the only region this
  stage was designed and safety-reviewed for).
- **Bounds (enforced in code, not convention):** cuts only, per-filter depth
  ≤ 3 dB, composed ≤ 4 dB, **Q clamped to 0.5–2.0**. Boosts are structurally
  blocked pending per-driver headroom/clipping evidence; a five-condition
  boost bar exists (≤+3 dB, dip ≥0.5 dB, ≥3 of 5 angles testify, ≤1
  dissenter, composed ≤+4 dB) but the route has never been exercised.
- **Two prescribers, same safety gate:**
  1. a deterministic solver (least-squares-flavored candidate generation);
  2. an **LLM prescriber harness**: the system emits a versioned JSON
     evidence packet (pooled + per-angle deviations, feature tables,
     repeatability stats); an LLM returns a candidate JSON; deterministic
     validators check bounds, evidence sufficiency, and packet freshness
     (content-hash match) before anything is staged.
- **Protocol per attempt:** bank a falsifiable prediction → apply → full
  5-angle re-measure → compare against prediction and noise thresholds →
  keep or **roll back** (rollback has fired on every attempt so far).

## 5. What we have found (dated, high-level)

**Prior campaign (through 2026-08-18):** large wins were mechanical/topological,
not EQ: a −6.9 dB worst-position crossover-region dip was reduced to −1.07 dB
primarily via **time alignment** (woofer delay), with pooled deviation
1.22 → 0.84 dB. High-frequency deviations off-axis are **beaming**
(directivity narrowing), on-axis stays flat there. Phase/alignment, not
magnitude EQ, was "the lever."

**This campaign (2026-08-19, wired mic + turntable):**

1. Baseline (best-measuring config): on-axis ≈ 0.85 dB RMS deviation.
   In the EQ-allowed window three features, consistent at all five angles:
   a −1.6 dB dip @ ~1037 Hz, +0.8 dB peak @ ~1406 Hz, +0.7 dB peak @
   ~2057 Hz (per-seat spreads 0.11–0.46 dB — strongly common-mode).
2. **Every EQ attempt made the primary score worse** and was rolled back:
   deterministic attempt +3.3σ worse; one-cut prescription (−1.2 dB @ 1400,
   Q 2.0) +8.2σ; two-cut prescription (−0.8 @ 1406 + −0.65 @ 2057) +15.2σ
   (σ = session noise sd; all under frozen-reference grading).
3. **Five mechanistic hypotheses were killed by measurement** in one night
   (not-common-mode; sizing-to-detrended-excursion; directivity trade;
   skirt-deepens-dip in the per-config frame; metric-hides-improvement).
   The surviving *unconfirmed* candidate: with Q clamped to 2.0, only
   ~28–43% of a filter's depth lands on the target feature (features have
   natural Q 3.6–6.6); the wide skirts depress already-below-reference
   neighbors (e.g., a −1.2 dB @ 1400 Q 2.0 filter puts ≈ −0.5 dB on the
   1037 dip), netting a loss under honest grading.
4. **Boosts were never played.** The only in-window dip (1037 Hz) is too
   small to matter: a *perfect* boost there predicts −0.033 to −0.046 dB
   on the primary score — below the 0.057 dB detection threshold.
5. **The real remaining targets are outside the EQ window:** six
   common-mode features in 4.1–9.5 kHz — peaks +0.83 @ 4149, +1.13 @ 5396,
   +1.01 @ 9509; dips −1.46 @ 4582, −0.70 @ 6245, −2.00 @ 8530 — with no
   route to touch them. Above ~3.6 kHz a separate class of features agrees
   in sign but splits in magnitude across angles (ratios up to 89×): we
   classify those as beaming and bar them from shared EQ.
6. Conclusion as declared: **the current route (shared, cuts-only, wide-Q,
   crossover-window EQ) is at its floor.** The speaker's baseline already
   is the flat point reachable this way.

## 6. Questions the owner has asked us to be honest about

**Did we ever try moving the crossover point (frequency) itself?**
**No.** The crossover frequency and slopes have been treated as fixed
commissioning-time topology throughout. What *was* measurement-optimized:
inter-driver time alignment (delay) and inter-driver level (blend trims).
Why Fc was never swept: (a) it sits inside a safety envelope (tweeter
low-frequency excursion/distortion limits) that our runtime candidate
machinery deliberately cannot touch; (b) the solvers parameterize filters
*around* a fixed topology; (c) nobody demonstrated it as the binding
constraint. That last reason is weak — the features we failed to EQ away
(1037 dip, 1406/2057 peaks) bracket the crossover region, and crossover-
frequency/slope choice is exactly the kind of lever that could move
interference structure there rather than fighting it with EQ. **This is an
open research question, not a considered rejection.**

**Why is the EQ range so narrow (824–3297 Hz)?** Because the only shipped
EQ stage lives in the crossover blend path and was safety-reviewed for that
window only. The *measurement* trusts 357 Hz–16 kHz. Extending correction
to the full trusted band is desired and unbuilt; the out-of-window features
in §5.5 are the motivation.

## 7. The research task

Find prior art — commercial, academic, and serious-DIY — that addresses the
problems above, and map it to our situation. For each area: what is the
established method, what evidence supports it, and what specifically would
it change about our approach?

1. **Reference/normalization when scoring EQ.** How do others normalize
   level when comparing an EQ'd config against baseline (fixed-voltage
   sensitivity, fixed target curves, regression-fitted targets)? Is
   self-referenced deviation a known pitfall? (Our frozen-reference lesson,
   §3.)
2. **Cut-only vs boost policy, and filter-width limits.** What do
   Genelec GLM, Neumann MA-1, Dirac, Trinnov, KEF/room-EQ products, and the
   Toole/Olive research lineage do about (a) boost caps and headroom
   accounting, (b) matching correction bandwidth to feature bandwidth,
   (c) minimum audible correction (our 1037 boost is predicted
   sub-detectable — is there literature on audibility thresholds for
   0.5–1.5 dB narrow-band deviations?), (d) only correcting minimum-phase
   features?
3. **Multi-angle weighting.** We pool 0/±7 (primary) and ±22 (secondary).
   Prior art: CTA-2034/spinorama listening window, early-reflections and
   sound-power curves, directivity index, estimated in-room response. Is
   there evidence for a better target than "flatten the on-axis pool" —
   e.g., flatten listening window while only *monitoring* DI smoothness?
   When on-axis and off-axis disagree, who should win and why?
4. **Crossover parameter optimization as an alternative to EQ.** Methods
   that jointly optimize crossover frequency, slopes, polarity, and delay
   against multi-angle measurements (active-crossover optimizers, Linkwitz
   practice, VituixCAD-style workflows, Klippel near-field-scanner-driven
   design). Evidence on whether crossover-region ripple of our shape
   (±0.8 dB features bracketing Fc) is better addressed by moving
   Fc/slopes than by shared EQ. This is the owner's direct question.
5. **Full-band (357 Hz–16 kHz) speaker linearization in a room.** Best
   practice for anechoic-approximating measurement beyond our 7 ms gate:
   near-field + far-field splicing, ground-plane measurement,
   moving-mic/MMM averaging, windowing trade-offs, klippel-style field
   separation. What would let us *trust* corrections at 400 Hz and at
   10 kHz, and how do others separate speaker-own features from room
   contamination and from beaming (our ≥3.6 kHz sign-agree/size-split
   discriminator — is there a standard version of this test)?
6. **Sizing and expected-yield modeling.** Our model predictions have been
   anti-correlated with measured outcomes twice (alignment model bias
   +4.2 dB near crossover; EQ predictions wrong in sign). Prior art on
   predicting realized acoustic change from a DSP filter change under
   gated multi-angle measurement — including known reasons predictions
   fail (gating artifacts, smoothing interactions, position pooling).
7. **Closed-loop automated tuning.** Any published systems (commercial
   auto-cal, academic, hobbyist) doing measure → propose → verify →
   rollback loops with pre-registered falsification, and what convergence
   criteria they use. LLM-in-the-loop examples are a bonus, not the point.
8. **Distortion-aware linearization.** Should harmonic-distortion data
   (we have per-sweep Novak ESS extractions) veto or reweight EQ decisions
   (e.g., don't boost into a distortion ridge; don't cut where distortion
   masks the benefit)?

**Deliverable:** a report that (a) answers each numbered question with
citations/links, (b) states concretely which of our choices prior art
contradicts or improves (metric, Q clamp, cut-only policy, window, angle
weighting, fixed Fc), and (c) proposes the 2–3 highest-leverage changes to
try next, each with the evidence that motivates it and a falsifiable
success criterion in our units (dB RMS deviation on the pooled views).

## 8. Primary artifacts (for humans; paths are repo-relative)

- Campaign evidence: `captures/wired-night-2026-08-19/` — `run-log.md`
  (~2,840 lines; §8.9 = frozen-reference verdict), `charts/01–06` +
  `charts/README.md` (every plotted number line-referenced), per-seat and
  per-round JSON under `analysis/` and `receipts/`.
- Harness proposal ratified for this campaign:
  `PROPOSAL-llm-prescriber-harness-2026-08-18.md` (repo root, this branch).
- Grading implementation: `jasper/active_speaker/flat_spec.py` (reference
  and banded evaluation), `jasper/active_speaker/flat_spec_views.py`
  (views); prescription intake: `jasper/active_speaker/crossover_v2/`
  (`blend_prescription.py`, `prescription_spool.py`,
  `evidence_packet.py`); CLI: `jasper/cli/crossover_prescriber.py`.

---

## 9. Errata + post-brief evidence (appended 2026-08-19, run-log §9)

Corrections to this brief's own claims, found by a verification pass over
the banked record, plus one decisive new result. Researchers working from
this brief should prefer the statements below where they conflict with
the text above.

1. **All nine named features measured MINIMUM-PHASE** (excess-group-delay
   test with positive and negative synthetic controls; largest excursion
   ≤8% of what a genuine same-frequency cancellation produces through the
   identical pipeline; verdicts angle-invariant; all gate-stable). The
   hypothesis that the in-window features live in a non-minimum-phase
   summation zone is **not supported**. The EQ failures in §5 re-attribute
   to *selectivity*: features have natural Q 3.6–6.6 (some reading Q≈12
   at the smoothing floor), while the correction stage clamps filters to
   Q ≤ 2.0 — every filter was ~3× wider than its target, and measured
   filter efficiency at Q 2.0 was 28–43%, so skirt damage exceeded center
   repair. This makes the Q clamp a *parameter under suspicion*, not
   physics; the declared EQ floor stands until a narrow-Q round is
   actually measured. The out-of-window dips at 4582/6245/8530 Hz
   classified as boostable minimum-phase defects (high confidence);
   the peaks at 4149/5396/9509 as cuttable (4149 medium — possible
   3–7 ms-path contamination unresolved).
2. **Per-seat spreads (§5.1):** correct values are 1037 → 0.46 dB,
   1406 → 0.20 dB; **2057 Hz has no per-seat breakout banked** (its five
   seat values were reproduced later by re-running the night's own tool,
   not read from the log). The "spread 0.11" figure belongs to that
   reconstruction, not the banked record.
3. **The "up to 89×" magnitude split (§5.5)** belongs to a *separately
   barred* beaming set (3608–15113 Hz, e.g. 11373 Hz = 88.9). The six
   out-of-window features named in §5.5 split only **1.2–2.5×** across
   angles.
4. **Driver delay (§2):** the −350 µs woofer delay was the 2026-08-18
   config. The baseline config this brief's numbers grade (the current
   best-measuring one) commits **+24.06 µs on the tweeter** instead.
5. **"Trusted band 357 Hz–16 kHz" (§3)** is an analysis convention (a
   shipped floor constant plus an analysis edge), not a derived quantity
   with a banked derivation.
6. **Measured cross-capture timing stability of this rig:** after
   alignment on a common event, sd 7.33 µs / worst 14.51 µs over 24
   same-angle cross-session pairs (an upper bound — it sits at the
   integer-sample quantization floor). The ±100 µs-class USB-mic jitter
   assumed for rigs like ours is refuted here by an order of magnitude;
   raw capture-start offsets (±54 ms) still make a common timing
   reference mandatory for per-driver phase. See the companion brief
   `RESEARCH-BRIEF-self-referencing-timing-2026-08-19.md`.
