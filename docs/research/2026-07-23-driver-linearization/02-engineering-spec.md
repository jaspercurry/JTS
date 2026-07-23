# Driver Linearization — Engineering Spec

> Verbatim owner-commissioned deep-research artifact (2026-07-23), produced
> after clarifying back-and-forth with the research agent. Its layer
> numbering is inverted relative to its own pipeline (see this directory's
> README); the repo's 1a/1b naming is canonical. Adopted with the
> reconciliations recorded in `active-speaker-tuning-layers-design.md`.

**Layer 2 of 4.** Handoff document for implementation.
---
## 0. Scope and framing
### What this layer does
Takes per-driver gated quasi-anechoic measurements and produces a per-driver magnitude correction profile that makes each driver's on-axis response flat **within the band that driver honestly occupies and the measurement chain honestly resolves**.
### Layer boundaries
| Layer | Job | Target | Not this layer's problem |
|---|---|---|---|
| 1. Crossover commissioning | Trim, delay, polarity, crossover point/slopes | Correct acoustic summation | — |
| **2. Driver linearization** | **Flatten each driver in its own passband** | **Flat gated on-axis, per driver** | Room, taste, directivity |
| 3. Room correction | Steady-state in-room | **Sloped**, not flat (see §9) | Driver behavior |
| 4. Preference EQ | Taste | User's | Everything else |
**Ordering matters and must be enforced in the pipeline.** Layer 2 runs on raw per-driver measurements *before* crossover filters are applied, and its output sits *underneath* the crossover filters in the DSP chain. This is because an LR4 (or any) crossover target describes the *acoustic* slope, which is the product of the driver's natural response and the applied filter. If you apply an LR4 highpass to a driver that is already rolling off, the acoustic result is not LR4 and summation misbehaves.
Corollary: **each driver must be linearized past the crossover region**, not just within its final passband — roughly an octave beyond on the crossover side, far enough that the opposite filter has attenuated meaningfully. ("One octave" is a sound engineering heuristic, not a published constant. Implement it as a configurable margin defaulting to one octave.)
### Out of scope for this document
Bass/LF handling, room modes, crossover commissioning internals (already solved), preference layer.
### The framing that should govern every design decision
We are not a loudspeaker manufacturer. KEF and Adam achieve flat-to-20 kHz by **transducer selection and design-time linearization** — anechoic chamber or Klippel NFS (±0.1 dB on-axis accuracy), lab-grade fixtures, sub-mm repeatability, producing one fixed filter per model. KEF's Uni-Q aluminium dome has its first breakup mode engineered up to ~40 kHz; Adam's X-ART AMT is specified to 50 kHz. Neither is boost-EQing a rolloff.
We are doing **field calibration** of arbitrary off-the-shelf hardware with a $150–400 USB microphone that a human placed on a stand. Our uncertainty budget above 10 kHz is one to two orders of magnitude worse. That is not a failure — it is the correct calibration of ambition, and the software should be explicit about it rather than pretending otherwise.
**Design consequence:** the product's value is not "flat to 20 kHz." It is *"we made your specific drivers measurably neutral across the band where neutrality is both achievable and audible, we told you honestly where that band ends and why, and we didn't damage anything getting there."* Build the UX around that promise.
---
## 1. The core architectural idea
**Do not hardcode a correction ceiling. Compute an allowed-correction-depth envelope per frequency, per driver, per session.**
A hardcoded ceiling ("fit to 16 kHz") is a folk number wearing a lab coat. It is wrong in both directions: too aggressive for a horn-loaded compression driver measured with a phone in a small room, too timid for a beryllium dome measured with a calibrated UMIK-2 on a fixed stand.
Instead, the ceiling emerges from a `min()` of independent limits, evaluated per frequency bin:
```
allowed_depth(f) = min(
    mic_trust_limit(f, mic_tier),        # can we see it?
    repeatability_limit(f, sweep_set),   # measured, not assumed
    invertibility_limit(f, driver),      # will EQ actually work?
    linearity_limit(f, multi_level),     # is it level-dependent?
    class_prior_limit(f, driver_class)   # safety floor
)
```
Correction magnitude at any frequency is clamped to `allowed_depth(f)`. The envelope tapers smoothly rather than cliffing, which also avoids introducing filter artifacts at the boundary.
The three limits worth understanding deeply:
**Mic trust** — what the measurement chain can resolve. Grows worse with frequency for every mic tier. This is usually the binding constraint above ~10 kHz.
**Invertibility** — whether magnitude EQ will produce the acoustic result you predict. A driver's response is invertible where it is **minimum phase**. Single drivers are largely minimum phase within their passband; the documented exceptions are breakup regions, multi-source interference (a compression driver's phase plug summing multiple annular exits at the throat), horn higher-order modes, ports, and diffraction. Where excess phase appears, magnitude EQ stops doing what the fit predicts.
**Linearity** — whether the transfer function is level-independent. Where it isn't, you're correcting a curve that changes under you.
---
## 2. Measurement protocol (inputs to this layer)
### Per driver
- **N ≥ 3 sweeps at the identical mic position** (5 preferred). Not different positions — *identical* position, repeated. The variance across these is the single most useful signal in the whole system (§4.1).
- **Same mic position for all drivers** in the speaker. Relative delay and level are position-dependent; two positions means two incompatible answers.
- **On the intended design axis**, mic aimed at the driver (0° incidence), using the **0° calibration file**. Using the 90° file for on-axis work injects several dB of error in the top octave and near-zero below 5 kHz — this is a silent, systematic, unrecoverable error. Validate the loaded cal file against the measurement geometry and hard-fail if mismatched.
- **Two drive levels** for the linearity test (§4.3) — recommend 9–12 dB apart.
- Gated to the reflection-free window. Gating costs LF resolution, not HF; for tweeter/midrange linearization above ~1 kHz the gated window is entirely adequate.
### Sweep design at 48 kHz fs
Sweep **beyond the analysis band** — 10 Hz to 22–24 kHz — with proper fade-in/fade-out windows, then analyze only up to the ceiling. Deconvolution produces pre/post-ringing at the sweep band edges; ending the sweep at 18 kHz places those artifacts *inside* the band you care about. Sweeping past the analysis band improves the analysis band.
There is no measurement-quality argument for stopping at 18 kHz. That number is a driver spec, not a sweep-design optimum.
### Do not average across positions
For per-driver anechoic linearization, spatial averaging **hurts** at HF: each position has different comb structure and different off-axis content, so averaging smears genuine on-axis detail. Repeat at one position for confidence; do not average positions. (This is the opposite of the correct advice for Layer 3 room correction, where spatial averaging is essential. Don't let a shared helper function conflate them.)
### Distance
1 m is the sane default. 0.5 m improves SNR and lengthens the reflection-free window, at the cost of near-field error for larger drivers (the mic is no longer far-field relative to the radiating area). Offer it as an option for tweeter-only measurement; keep 1 m for anything cone-sized. Log the distance — it affects the confidence model.
---
## 3. Microphone tier priors
These are **starting envelopes**, immediately refined by measured repeatability (§4.1). They exist so the system degrades sensibly before it has evidence, and so it can never be talked into trusting a phone at 16 kHz.
| Tier | Example | Full correction to | Taper to zero at | Above taper | Rationale |
|---|---|---|---|---|---|
| **Reference** | UMIK-2 / UMIK-1 with per-serial cal (ideally third-party comparison-grade), fixed stand | 8 kHz | 16 kHz | Broad shelf only, cut-preferred | National-lab free-field calibration uncertainty widens from ±0.2 dB (≤5 kHz) to ±0.3 dB (6.3–10 kHz) to ±0.4 dB (12.5–20 kHz) at k=2; comparison-grade consumer cal files land ~±0.5 dB, factory files ~±2 dB above 10 kHz |
| **Consumer** | Dayton iMM-6C, EMM-6, factory-cal-only UMIK | 6 kHz | 12 kHz | Cut only | Factory electret cal files carry ~2 dB HF error; capsule unit-to-unit spread widens past 8–10 kHz |
| **Phone / built-in** | Device mic, no cal file | 3 kHz | 8 kHz | None | Sonos will not trust phone-mic HF calibration across Android hardware and says so publicly; treat this tier as Layer 1 + Layer 3 capable, **driver linearization degraded to LF/mid only** |
Two supporting facts worth surfacing in the UI: miniDSP's own UMIK-2 manual states the 0°/90° files "differ only above a few kHz, due to the high-frequency directionality of the microphone," and that for speaker work "at high frequencies (above 5 kHz) adjustment of EQ or target curve by ear is likely to be needed anyway." Anthem's ARC ships a **5,000 Hz** default correction ceiling with the stated reason: *"At higher frequencies, the microphone becomes directional thus affecting measurement accuracy."*
Our reference tier sits above Anthem's default because our case is materially better: gated (reflections removed), fixed on-axis aim, repeated measurement, single known distance. That's the justification for 8 kHz rather than 5 kHz — and it's also the ceiling on how much further we should push on priors alone.
**Aiming tolerance.** A ±15° aiming error is sub-dB through most of the band; ±30° costs on the order of 1–3 dB at 20 kHz. Below ~8 kHz both are negligible. Give the user an aiming aid and log the confidence penalty if they can't confirm it.
---
## 4. Discovering the ceiling from measurement
Four tests, in recommended build order. **Ship #1 and #2 first — they deliver most of the value and are the most robust.**
### 4.1 Repeatability gate — PRIMARY, build first
Across the N repeated sweeps, compute per-frequency standard deviation of magnitude, σ(f), smoothed.
```
if σ(f) > σ_threshold:  allowed_depth(f) = 0   # observe, don't fit
```
Suggested thresholds: **0.5 dB (reference tier), 1.0 dB (consumer), 1.5 dB (phone)**. Taper rather than cliff: scale allowed depth by `clamp(1 - (σ(f) - σ_lo)/(σ_hi - σ_lo), 0, 1)`.
Why this is the primary gate: it is empirical, it requires no driver database, and it automatically folds in mic tier, cal-file quality, placement stability, room noise, ambient HVAC, driver self-noise, and clock drift — every error source at once, measured rather than modeled. It is also trivially explainable to the user: *"your measurements disagreed with each other above 11 kHz, so we didn't fit there."*
Wavelength intuition for why σ explodes at HF: 34 mm at 10 kHz, 21 mm at 16 kHz, 17 mm at 20 kHz. A centimetre of mic drift is a large fraction of a wavelength at 16 kHz. On a horn with steep off-axis rolloff, a few degrees of aim error is directly multi-dB.
### 4.2 Closed-loop verification — build second
Apply the fitted correction, re-measure, and compare **achieved** vs **predicted** correction per frequency band.
```
divergence(f) = |achieved_delta(f) - predicted_delta(f)|
if divergence(f) > 1.5 dB:
    back off correction in that band by the divergence
    re-verify (max 2 iterations)
    mark band as "not correctable" and log
```
This is self-calibrating and works identically on a DE250, a silk dome, and a ribbon. It catches invertibility failures, nonlinearity, and measurement error without needing to diagnose which one occurred. The ±1.5 dB gate aligns with the existing tracking-error gate elsewhere in the system.
**This is the single most important mechanism in the spec.** It converts every modeling assumption in this document into a hypothesis that gets tested against reality on every run.
### 4.3 Multi-level linearity test — build third
Sweep at two levels 9–12 dB apart. Normalize both to the same reference level and compare.
```
if |response_high(f) - response_low(f)| > 0.5 dB:
    mark f as nonlinear → boost = 0, cut only, flag to user
```
A band that linearizes at low level but not at high level is telling you the mechanism is nonlinear (breakup, excursion limit, thermal compression, or DSP clipping) and should not be magnitude-corrected. Extends the two-level pilot-tone AGC check already in the system.
### 4.4 Excess-phase gate — build fourth, treat as advisory
Compute the minimum-phase response via Hilbert transform of the log-magnitude, remove bulk propagation delay, and subtract from measured phase to obtain excess phase / excess group delay.
Where excess phase is small and smooth, the response is minimum phase and magnitude EQ will do what the fit predicts. Where excess phase rises, it won't — this is exactly the boundary between a mass-controlled rolloff (invertible) and an interference/breakup regime (not).
**Practical warnings — this test is fragile:**
- It requires an accurate bulk-delay estimate. Raw excess phase is dominated by an arbitrary time-of-flight term. Get delay from broadband cross-correlation on the *linearized* response (see §8), not from the narrow crossover overlap.
- It requires good phase SNR.
- It requires an assumption about response beyond the measured band; naive extrapolation produces large errors near the band edge — precisely where you're trying to make a decision.
Suggested heuristic (tune empirically): flag where smoothed excess phase deviates more than ~45° from zero after bulk-delay removal, sustained over more than ~1/6 octave. Treat as *advisory input to the envelope*, not a hard gate, until validated against §4.2 outcomes on real hardware.
**Known open question, preserve it as one:** whether breakup resonances are minimum phase is genuinely contested in the literature. One position holds each individual resonance is minimum-phase and therefore correctable; the other holds that a multi-modal radiating surface produces a far-field *sum* of spatially distributed sources that is non-minimum-phase. Both appear in credible sources. Fortunately the practical guidance is identical under either: **EQ the smooth part, never magnitude-EQ into a high-Q breakup peak.** Don't let the implementation depend on resolving this.
---
## 5. Driver-class priors
Collected from the user, used to seed the envelope before measurement refines it. Every one of these is a **prior that measurement overrides**, in both directions.
| Class | Rolloff / failure mechanism | Initial fit ceiling | Boost policy | Class-specific hazard |
|---|---|---|---|---|
| **Compression driver on horn** | Phase-plug path-length interference between annular exits + diaphragm breakup + chamber air load | ~10–12 kHz | Cut-only above ~12 kHz; broad shelf ok below if excess phase clean | **Low end**: cut-only near/below horn cutoff — loading collapses, excursion rises, this is the real damage risk. Steep horn directivity makes aiming error brutal at HF |
| **Soft / fabric dome** | Gentle, self-damped breakup ~15–20 kHz, low Q | ~14–15 kHz | +3 to +6 dB shelf often genuinely works | Small voice coil, limited power handling; verify at level |
| **Metal dome (Al / Ti)** | Pistonic then sharp high-Q breakup ~20–28 kHz (out of band but excitable) | ~16 kHz | Cut-only above ~14 kHz; **never boost toward the breakup peak** | Out-of-band breakup can be excited and may fold IMD back into the audible band — contested, so *measure it* via §4.3 rather than assume |
| **Beryllium / diamond dome** | First mode pushed to ~30–40 kHz+ | ~16–18 kHz | Tolerant; still cut-only into any residual peak | **The mic becomes the binding limit, not the driver.** Ceiling is set by §3 and §4.1 |
| **Ribbon / AMT** | Very low mass, extended response; no classic dome breakup | ~16–18 kHz | Most tolerant of the classes | **Narrow vertical directivity** — measurement height error dominates; require ear-height on-axis measurement and warn if the crossover is too low for the element |
| **Unknown / user unsure** | — | Consumer-tier prior, one class more conservative | Cut-only above 8 kHz | Degrade gracefully; never punish the user for not knowing |
Note what happens in the bottom rows: for a beryllium dome or an AMT, **the driver stops being the limit and the microphone becomes the limit**. The class prior and the mic prior are not competing models — they're both terms in the same `min()`.
### What to collect from the user
Driver class (from the table above), nominal passband from the datasheet, recommended minimum crossover, power handling, sensitivity, and — for horns — the horn's cutoff frequency and nominal coverage angle. Make every field optional with a documented conservative default. **Never let a missing field produce a more aggressive correction than a filled one.**
---
## 6. Fitting policy
### Cut is nearly free. Boost is the dangerous direction.
Default strategy: **normalize downward.** Establish the target level at or near the *minimum* of the usable passband rather than the mean, so most corrections are cuts. Spend the sensitivity headroom rather than the driver's excursion and thermal headroom. On a 108 dB/W/m compression driver padded ~25 dB down, you have enormous level headroom and essentially zero thermal concern at home levels — spend it.
### Boost limits
| Rule | Value | Basis |
|---|---|---|
| Global max boost | **+6 dB** | Anthem ARC's dip-fill cap; widely used precedent |
| Max boost above 0.7 × ceiling | **+3 dB** | Confidence tapers before the ceiling does |
| Max boost above ceiling | **0 dB** | Non-negotiable |
| Max Q on any boost filter | **~2.0** | Narrow dips are usually interference or position artifacts, not driver behavior. Filling them chases a measurement ghost and can excite a resonance |
| Boost into any band flagged nonlinear (§4.3) | **0 dB** | — |
| Boost into any band with rising excess phase (§4.4) | **0 dB** | — |
| Boost near/below horn cutoff | **0 dB** | Excursion; this is where compression drivers actually die |
| Boost toward a known breakup peak | **0 dB** | — |
### Cut limits
Up to **−12 dB**, Q up to ~8. Cuts don't create excursion, don't create thermal load, and don't excite resonances. Be generous here — this is where the linearization actually happens.
### Smoothing schedule
Fit to a smoothed target. Smoothing should widen with frequency to track *both* measurement confidence and auditory frequency resolution:
| Band | Smoothing |
|---|---|
| 200 Hz – 1 kHz | 1/6 octave |
| 1 – 4 kHz | 1/6 octave |
| 4 – 10 kHz | 1/3 octave |
| > 10 kHz | 1/2 to 1/1 octave |
Fit against the smoothed curve; **verify against a less-smoothed curve** so you catch narrow problems the fit smoothed away. If a narrow feature survives at 1/12 octave across all N sweeps with low σ, it's real and worth reporting to the user even if you correctly declined to correct it.
---
## 7. Pipeline
```
FOR each driver:
  1. Load driver class + datasheet params (optional, defaults conservative)
  2. Validate mic cal file matches measurement geometry (0° on-axis) — HARD FAIL if not
  3. Capture N≥3 sweeps at identical position, plus 2-level pair
  4. Deconvolve, gate, compute magnitude per sweep
  5. σ(f) across sweeps → repeatability gate            [§4.1]
  6. Two-level comparison → linearity gate              [§4.3]
  7. Bulk delay estimate → minimum phase → excess phase [§4.4, advisory]
  8. allowed_depth(f) = min(mic_prior, class_prior, repeatability, linearity, invertibility)
  9. Smooth target per schedule                          [§6]
 10. Fit correction: cut-preferred, clamped to allowed_depth, boost caps enforced
 11. Apply to DSP, re-measure
 12. achieved vs predicted per band; back off divergent bands; re-verify (≤2 iters) [§4.2]
 13. Emit: correction profile + ceiling + per-band reason codes
```
Then Layer 1 crossover filters are applied *on top of* the linearized drivers, and Layer 3 room correction on top of that.
### Reason codes — emit one per band
`FITTED` · `LIMITED_BY_MIC_TIER` · `LIMITED_BY_REPEATABILITY` · `LIMITED_BY_NONLINEARITY` · `LIMITED_BY_EXCESS_PHASE` · `LIMITED_BY_CLASS_PRIOR` · `LIMITED_BY_VERIFY_DIVERGENCE` · `OUT_OF_BAND`
These drive the UI, the logs, and the "how do I do better" guidance. They are also the debugging surface when a user reports a bad result — you can reconstruct exactly why the system stopped where it did.
---
## 8. Interaction with Layer 1 (delay estimation)
Brief, since crossover commissioning is already solved — but one dependency runs the other way and matters here.
Estimate inter-driver delay on the **broadband linearized** responses, not on the narrow post-crossover overlap band. Cross-correlation peak width is inversely proportional to the bandwidth correlated (Knapp & Carter's generalized correlation framework; the Cramér–Rao bound for delay variance scales inversely with bandwidth × SNR × observation time). A narrow 2–4 kHz overlap therefore yields a comb of near-equal optima spaced ~1/Fc apart, and capture noise picks the winner — the off-by-one-lobe failure mode.
Use coarse-to-fine: get an unambiguous coarse estimate from a low-frequency-inclusive correlation where the cycle period is long, then refine within that lobe at HF. Emit a **confidence metric** (peak sharpness / ratio of primary to secondary peak); if ambiguous, don't auto-apply — fall back to polarity plus reverse-null checks at the crossover and ask the user.
This also feeds §4.4: the excess-phase gate is only as good as the bulk-delay estimate it subtracts.
---
## 9. Verification targets — the boundary that's easiest to get wrong
**Layer 2 verifies against FLAT.** Gated quasi-anechoic per-driver on-axis response should be flat. This is well-supported (Toole/Olive).
**Layer 3 verifies against a DOWNWARD SLOPE.** Preferred in-room steady-state response slopes down with frequency — roughly **1 dB/octave** in the Harman target formulation, shallower (~6 dB total span) in the older Brüel & Kjær house curve. Sean Olive states it directly: a flat in-room target is clearly not optimal; preferred corrections have a smooth downward slope with increasing frequency.
**If a shared verification helper checks in-room flatness against flat, the system will over-brighten every result.** Make the target an explicit parameter of the verify function, not a default. The exact slope should be directivity-aware and user-adjustable — studies converge on "downward" but the magnitude depends on loudspeaker directivity and room reflectivity, so don't hardcode one number.
---
## 10. Explicit non-goals — state these in the UI
The software should say what it cannot do, plainly. This prevents the most common disappointment.
**It cannot fix directivity.** On-axis magnitude EQ cannot correct a power-response or directivity-index step. If a beaming cone hands off to a horn opening into its designed coverage, that mismatch survives any amount of on-axis flattening — and flattening on-axis can make the off-axis balance *worse* by pushing energy into a band where the two drivers' radiation patterns disagree.
Give the user a `ka` warning at crossover selection time: for a cone of effective radius *a*, `ka = 1` at `f = c/(2πa)`. For a 5.5" driver (a ≈ 57 mm) that's ~958 Hz, and `ka = 2` at ~1.9 kHz — so a 2 kHz crossover already has that cone beaming. Rule of thumb: match the cone's beamwidth to the horn's nominal coverage at the crossover point, which often implies a *lower* crossover than intuition suggests. This is a geometry fix (crossover point, horn choice, baffle), never a filter fix.
**It cannot extend a driver beyond its passband.** A 1" compression driver is not a beryllium dome or an AMT. If the user wants genuine 16 kHz+ air, the honest levers are a bounded preference shelf in Layer 4 (taste, disclosed, cut into headroom) or different hardware.
**It cannot beat its own microphone.** Say which tier they're on and what it costs them.
---
## 11. UX surface
Show three bands per driver, with the reason code:
- **Corrected** — fitted, verified, here's the before/after
- **Observed only** — measured and reported, not corrected, and *why* (reason code in plain language)
- **Out of band** — outside the driver's passband
Offer concrete ceiling-improvement actions, ranked by impact:
1. Fixed mic stand instead of handheld (biggest single win on σ)
2. More repeat sweeps (cheap, directly lowers σ)
3. Quieter room / turn off HVAC
4. Third-party comparison-grade cal file instead of factory file
5. Better mic tier
6. Confirm on-axis aim
Never display "flat to 20 kHz" as a goal or a score. Score against *achieved neutrality within the honest band*, and show the band.
---
## 12. Logging / data model
Persist per session so ceiling decisions are auditable and re-runnable offline:
- Raw sweep set (all N), mic tier, cal file hash + incidence, distance, drive levels, gate window
- σ(f) curve, two-level delta curve, excess-phase curve, bulk-delay estimate + confidence
- `allowed_depth(f)` envelope with per-band reason codes
- Fitted filters, predicted response, achieved response, divergence curve, iteration count
- Driver class + datasheet params as entered
This makes the §4.4 heuristics tunable against real outcomes later: once you have a corpus of sessions where §4.2 verification passed or failed, you can calibrate the excess-phase threshold empirically instead of guessing.
---
## 13. Preserve these uncertainties in the product
Three places where credible sources genuinely disagree. In each case the correct engineering response is *measure, don't assume* — which the architecture already does.
1. **Are breakup regions minimum phase?** Contested (§4.4). Practical guidance is identical either way.
2. **Does metal-dome out-of-band breakup produce audible IMD?** Contested and under-measured; isolated measurements are scarce. The §4.3 multi-level test detects it empirically if present.
3. **What is the correct in-room target slope?** Studies converge on "downward," diverge on magnitude (~1 dB/oct Harman vs shallower B&K), and it depends on directivity and room. Make it a parameter, not a constant.
Where the honest answer is "no published data found," say so in the code comments rather than inventing a threshold that looks authoritative.
---
## 14. Strongest citations
**Minimum phase / excess phase (the pivotal question)**
- Lipshitz, S.P., Pocock, M., Vanderkooy, J., "On the Audibility of Midrange Phase Distortion in Audio Systems," *J. Audio Eng. Soc.* 30(9):580–595 (1982), AES e-lib 3824
- Mateljan, I. (ARTA), "Loudspeaker Minimum Phase Estimation," ICA'89
**Compression driver phase-plug interference**
- Smith, B.H., "An Investigation of the Air Chamber of Horn Type Loudspeakers," *J. Acoust. Soc. Am.* 25(2):305–312 (1953), DOI 10.1121/1.1907038
- Dodd, M. & Oclee-Brown, J., "A New Methodology for the Acoustic Design of Compression Driver Phase-Plugs with Concentric Annular Channels," *J. Audio Eng. Soc.* 57(10):771–787 (2009)
- Geddes, E., *Audio Transducers* (GedLee, 2002) — higher-order modes in waveguides
**Delay estimation**
- Knapp, C.H. & Carter, G.C., "The Generalized Correlation Method for Estimation of Time Delay," *IEEE Trans. ASSP* 24(4):320–327 (1976), DOI 10.1109/TASSP.1976.1162830
**Targets and audibility**
- Toole, F., *Sound Reproduction: The Acoustics and Psychoacoustics of Loudspeakers and Rooms*, 3rd ed. (Focal/Routledge, 2017)
- Toole, F. & Olive, S., "The Modification of Timbre by Resonances: Perception and Measurement," *J. Audio Eng. Soc.* 36(3):122–142 (1988), AES e-lib 5163
- Olive, S., "Subjective and Objective Evaluation of Room Correction Products" (Harman, 2009)
**Measurement uncertainty**
- Klippel Near Field Scanner 3D datasheet — ±0.1 dB on-axis, ±1 dB all directions, 10 Hz–20 kHz (the design-time benchmark we are explicitly *not* meeting)
- IEC 61672-1:2013 Class 1 tolerances — ±1.1 dB @1 kHz widening to +3.5/−17 dB @16 kHz
- miniDSP UMIK-2 User Manual §5, §5.3, §7.1.1
**Crossover / acoustic target**
- Linkwitz, S., linkwitzlab.com — acoustic vs electrical target functions
- Rane Note 160 (Bohn) — acoustic response = electrical filter × driver response
- VituixCAD documentation (Saunisto) — de-facto DIY standard methodology
**Sweep design**
- Farina, A., "Simultaneous Measurement of Impulse Response and Distortion with a Swept-Sine Technique," AES 108th Conv. (2000), paper 5093
- Müller, S. & Massarani, P., "Transfer-Function Measurement with Sweeps," *J. Audio Eng. Soc.* 49(6) (2001)
---
## 15. Build order
1. **Repeatability gate (§4.1)** — highest value per line of code; makes every ceiling empirical immediately
2. **Closed-loop verification (§4.2)** — turns every assumption in this doc into a tested hypothesis
3. **Cut-preferred fitting + boost caps (§6)** — the safety envelope
4. **Mic tier + class priors (§3, §5)** — sensible cold-start behavior
5. **Multi-level linearity (§4.3)** — extends existing two-level infrastructure
6. **Excess phase (§4.4)** — highest sophistication, lowest robustness; validate against #2 before trusting it
7. **UX reason codes and ceiling-improvement guidance (§11)**
Steps 1–3 alone produce a system that is safe, honest, and better than a hardcoded ceiling.
