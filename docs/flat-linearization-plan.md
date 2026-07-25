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
evaluated at 1/3-oct smoothing (1/6-oct retained for diagnostics), relative
to the spec-band power mean, excluding interference-flagged bins (below):

| Band | Tolerance |
|---|---|
| ~250 Hz – 2 kHz | ±1.5 dB |
| 2 – 8 kHz | ±2.0 dB |
| 8 – 16 kHz | ±2.5 dB |
| > 16 kHz | best-effort, disclosed, never specced |

Rationale, briefly: ±1.5 dB mid-band is demonstrated-feasible (run 7 held
±1.5 over 2–7 kHz single-point); above 8 kHz, UMIK-2-class unit uncertainty
is ~3 dB, so a tighter spec there would live inside the instrument's error
bars; the ~250 Hz lower edge sits above the 7 ms gate's ~143 Hz validity
floor and is provisional (250 vs 300 Hz is settled by the validation
session's LF spread data). Below the lower edge is Layers 2–3 territory
(bass program + room correction, in-room instruments). The 8–16 kHz
tolerance may tighten to ±2.0 after the loop demonstrates margin, never
tighter than mic uncertainty.

## Evidence — why the instrument must change

### 1. Offline comb forensics (2026-07-25, no new captures)

Reanalysis of the 2026-07-24/25 session WAVs (runs 5 and 7, MEASURE and
VERIFY frames), preserved with scripts and charts under
`captures/flat-linearization-20260725/` (laptop-durable, gitignored):

- The band-limited (6–19 kHz) IR envelope shows a **discrete echo train at
  +0.31 ms (−8.8 dB, r≈0.36) with 2τ/3τ repeats** — byte-similar in the
  summed VERIFY frame and the tweeter-alone MEASURE frame, and unchanged
  between run 5 and run 7 (2.5 h apart, entirely different DSP).
- Interference-null ladder `(n+½)/τ` for τ≈298 µs (~10 cm path delta)
  lands at 1.7 / 5.1 / 8.6 / 12 / 15.5 kHz — matching the measured dip set
  (1707, 8396, 8924, 11507, 15559 Hz, identical bins run 5 vs run 7). The
  cepstrum shows a 286+357 µs doublet in every frame.
- **The 1.7 kHz "crossover dip" is in the woofer-ALONE capture** (−9 dB at
  1712 Hz): it is the same bounce's null 0, not a crossover integration
  failure. The bounce also predicts ~+2.7 dB coherent lift below the first
  null — a large share of the 400–1500 Hz hump, consistent with its known
  cross-placement scatter.
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

Full report in the owner's research thread; design-relevant conclusions:

- **No shipped consumer product removes an early bounce from a single
  capture.** Every mass-market system averages it away spatially: Sonos
  Trueplay moving-mic PSD (power) averaging over >150 positions (Sonos
  engineering blog; US 10,045,138), Dirac Live 9–17 positions, Audyssey 8
  positions with fuzzy c-means weighting (US 8,005,228), Harman's "four
  farfield locations are ideal" (US 8,130,966). All correct
  **minimum-phase only** and decline to fill non-minimum-phase
  interference nulls (Toole doctrine).
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
  reference program — relative inter-driver timing within one capture is
  position-robust, and the anchor+snap selector's 2.77 µs repeatability is
  already proven. The cloud is the instrument for magnitude spec,
  linearization, and VERIFY. The cloud average grades the crossover region
  the way CTA-2034's listening window does (slightly gentler than a
  single on-axis point — by design).
- **Non-min-phase doctrine is now uniform across layers:** narrow
  interference dips are excluded from correction and metrics in the
  speaker layer (this plan) and remain uncorrected in room correction
  (its existing conservative-above-transition philosophy). A broad
  "boundary/desk mode" shelf, if ever wanted, is a Layer-3 product
  feature, not linearization — out of scope here.

## Implementation stages

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
- **S1 — Instrument.** Conductor position-group choreography (prompted
  moves between capture groups; position metadata; per-position quality
  gates) + the combiner/screen estimator module (power mean, median
  cross-check, exclusion mask, cepstral τ detector). Offline-replayable
  against S0's corpus before it touches the live flow.
- **S2 — Spec + gauges.** Spec bands/tolerances/1-3-oct evaluation;
  exclusion-aware flatness gauges; VERIFY widened from the ~2·Fc
  integration band to the full spec band; wizard//state surfacing. The
  observe ledger, fit working curve, gauges, and VERIFY all consume one
  shared curve construction (kills the frame-discrepancy class for good).
- **S3 — Closed loop.** measure → fit (existing cut-domain engine +
  anchored give-back, now fed the combined curve) → apply → re-measure →
  residual trim; convergence + divergence/rollback policy on the existing
  apply/undo rails; charts each iteration. Then spend what the honest
  measurement says is real: top-octave realization beyond the single-shelf
  cap (stacked shelf / literal boost per the standing adjudication) only
  if S0/S3 data demands it.
- **S4 — Generalization.** Loop core stays role-count-blind (consumes
  topology roles); 3-way lands with #1703's conductor; passive/1-way =
  one full-range role through the same loop.

## Non-goals / guardrails

- No cepstral or parametric echo *removal* in production (detection only).
- No max-hold estimator; no complex averaging of hand-moved captures.
- No EQ of interference-flagged bins, ever; they are reported instead.
- No absorber pads, tripods, or treatment steps in any user flow.
- No change to CamillaDSP safety ceilings (`devices.volume_limit` 0.0,
  positive-gain clamps) or driver-protection floors.
- Room correction's scope is untouched; no layer eats another's job.

## Open questions (tracked, none blocking S0)

1. N and spread achievable on the lab rig; empirical estimator bias near
   nulls (S0 decides).
2. Spec lower edge 250 vs 300 Hz (S0 LF spread data decides).
3. Phone moving-capture UX (Trueplay-style) — later layer on the S1
   combiner seam; browser-capture constraints already cataloged in the
   measurement-v2 research.
4. Thermal-compression attribution for the realization shortfall —
   candidate mechanism; the loop handles it agnostically either way.
5. Whether 8–16 kHz can tighten to ±2.0 after realization headroom is
   measured bounce-free.

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
+ US 10,045,138; Audyssey US 8,005,228; Harman US 8,130,966; Cook et al.,
JASA 1955 (sinc(kr) correlation); Müller & Massarani, JAES 2001;
ANSI/CTA-2034; Devantier AES 5638; Olive AES 6113/6190 + US 8,311,232;
Toole, *Sound Reproduction* 3rd ed.

Last verified: 2026-07-25
