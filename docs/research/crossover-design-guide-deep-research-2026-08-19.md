# Crossover design guide — owner-supplied research (2026-08-19)

> **Status: research.** Verbatim primary source — an owner-supplied
> deep-research report on first-principles active-DSP crossover design and
> optimization. The "Adoptions" line below is a pointer, not this file's own
> status: what the build-out took from the report is tracked in the build
> dispatches and PR coordination sections, not here. This file preserves the
> report as received; every numeric anchor in it is a PRIOR, not a shipped
> spec.

**Provenance:** deep-research report supplied by the owner during the
crossover-optimization build-out; reviewed by the conductor the same day.
**Adoptions** (recorded in the build dispatches and PR coordination
sections; corrected against the tree at `2c191417e`, 2026-08-25): two died
with `a31f1fa24` (#2832)'s deletion of
`crossover_v2/{search,objective,candidate_space}.py`, with no successor
anywhere in the tree — measured directivity bounds for candidate_space
(woofer −6 dB @ 30° ceiling; matched-beamwidth prior; three-source
precedence: declared-safety hard / measured-directivity hard-ceiling-soft-
prior / geometry-prior seed-only), and a DI-continuity term in the
objective. The reverse-null test was never part of that deleted machinery:
its code ships and is live (`profile.py`'s `SUPPORTED_POLARITY`;
`commissioning_evidence.py`'s and `commissioning_host.py`'s `"reverse"`
evidence/graph kind; `crossover_alignment.py`'s
`POLARITY_KEEP`/`POLARITY_INVERT` decision on measured null depth) — the
analyzer that produced that null depth, `analyze_summed_crossover`, has
since been deleted as a zero-caller orphan, so a rebuilt summed probe must
supply it. And no
physical inverted-polarity probe has ever been banked:
`commissioning_capture_producer.SummedCaptureProducer` is a confirmed
runtime orphan nothing instantiates, `docs/attribution-stage-plan.md`
records "No P1 has ever been run" (P1 = `PROBE_REVERSE_NULL` in
`attribution/closed_sets.py`), and `null_walk.py`'s walk executor
`run_null_walk` was deleted because nothing called it. The slope-aware
distortion-informed tweeter floor split: the candidate_space refinement was
deferred prose that never shipped as code and died with the file; the
#2736 apply gate it named as backstop is live and deliberately slope-blind
(`camilla_yaml.EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR`,
`crossover_declaration.CROSSOVER_BELOW_DECLARED_FLOOR`,
`fc_sweep.FC_REJECT_BELOW_DECLARED_FLOOR`).
**Standing caution:** every numeric anchor below is a PRIOR that seeds
the search space — in-situ measurement decides (the report's own rule,
and the house fresh-eyes rule).
**Archive status:** landed on `main` from `claude/night-driver` on
2026-08-19 as the primary-source research archive for this report;
later crossover-agent documentation will cite it rather than restating it.
Verbatim report follows.

---

# Designing and Optimizing Active DSP Crossovers: A First-Principles Guide

## TL;DR
- A crossover point is chosen at the intersection of three constraints: **directivity matching** (the woofer's narrowing beam at the top of its band should match the tweeter/waveguide's beamwidth at the bottom of its band, so the power response has no discontinuity), **driver limits** (tweeter Fs/excursion/distortion at the bottom; woofer breakup/beaming at the top), and **geometry** (driver center-to-center spacing vs. wavelength, which sets vertical lobing). The famous "6.5" woofer beams around 1.5–2 kHz" rule is just the ka≈2 point of the piston-directivity model.
- A DSP crossover per channel consists of: a high-pass or low-pass **filter** (corner frequency + slope/order + alignment type, with LR4/24 dB-oct the default), **gain trim**, **polarity**, **delay** (time alignment), and **parametric EQ** to flatten each driver to a target. The key subtlety is that you target the **acoustic** summed response, so the DSP's electrical filter values are almost never the textbook filter values — the driver's own rolloff combines with the electrical filter.
- The professional workflow is **measure (gated, in-cabinet, on- and off-axis) → flatten each driver with PEQ → apply crossover filters to hit an acoustic target → optimize on-axis, phase tracking (reverse-null test), and off-axis/power response in simulation (VituixCAD/REW) → verify by measurement → iterate.**

## Key Findings

**1. Directivity, not just frequency response, drives crossover-point selection.** The heart of the problem is that a cone behaves as a rigid piston only at low frequencies; as wavelength shrinks toward the cone's size, it "beams." The controlling variable is the dimensionless product **ka** (k = 2π/λ, a = piston radius). Below ka ≈ 0.5 the source is omnidirectional; the pattern starts narrowing above ka ≈ 1; smooth off-axis response is maintained to ka ≈ 2; and by ka ≈ 5 there is severe beaming with lobing. You want to cross over **where both drivers have similar directivity** so that the off-axis and power response transition smoothly.

**2. The DSP outputs are a small, well-defined parameter set** per channel: filter type/frequency/slope, gain, polarity, delay, and PEQ. Everything else is workflow.

**3. LR4 is the default because it sums flat and in-phase** with both drivers −6 dB at the crossover, producing no on-axis lobe tilt when acoustic centers are aligned.

## Details

### PART 1 — THE INPUTS (what constrains the crossover choice)

#### 1a. Piston directivity and the "beaming" math

The standard model is a **flat, rigid, circular piston in an infinite baffle**. Its far-field directivity (pressure) is:

**D(θ) = 2·J₁(ka·sinθ) / (ka·sinθ)**

where J₁ is the first-order Bessel function of the first kind, k = 2π/λ, a = piston radius, θ = angle off-axis. This is the Kinsler & Frey / Beranek textbook result (Kinsler & Frey, "Fundamentals of Acoustics," Ch. 7; Beranek & Mellow, "Acoustics: Sound Fields and Transducers"). Key quantitative behavior:

- **ka < 0.5:** omnidirectional (in the forward hemisphere). Directivity Index (DI) ≈ 3 dB (half-space radiation — Beranek: "If the source in an infinite baffle is nondirectional in the hemisphere, which is usually the case for ka < 0.5, then the directivity factor Q = 2, that is, DI = 3 dB").
- **First null** appears at sin(θ_null) = 3.83/ka (3.83 = first zero of J₁). No null exists in the forward hemisphere below ka ≈ 3.83; at ka = 6 the first null is at ~40° (arcsin 3.83/6 = 39.7°).
- **−6 dB full beamwidth** (the angle between the half-level points, useful for matching a horn's rated coverage; computed from the exact Bessel formula, half-level at ka·sinθ = 2.215):
  - ka ≲ 2.2: the pattern never falls 6 dB within the forward hemisphere (effectively very wide).
  - ka = 3 → ≈ **95°**
  - ka = π (λ = diameter) → ≈ **90°**
  - ka = 4 → ≈ **67°**
  - ka = 6 → ≈ **43°**
  - ka = 8 → ≈ **32°**
  - Large-ka approximation: full −6 dB beamwidth ≈ **253.8/ka degrees**; full −3 dB beamwidth ≈ 184.9/ka degrees (half-power at ka·sinθ = 1.6137; cross-checked against published values: ka=6 → −3 dB full angle 31.2°).
- **Directivity Index** rises ~6 dB/octave once the piston is directional. Exact values from Q = (ka)²/[1 − J₁(2ka)/(ka)]: DI ≈ 3.7 dB at ka=1, ≈ 5.9 dB at ka=2, ≈ 12.3 dB at ka=4, ≈ 18.1 dB at ka=8. For large ka, DI ≈ 20·log₁₀(ka). Beranek's Fig. 4.30 plots exactly this curve for the baffled piston.

**Beaming frequency, worked for a 6.5" woofer** (effective radiating diameter ≈ 13 cm, a ≈ 6.5 cm, c = 343 m/s), using f = ka·c/(2πa):
- ka = 1 (λ = circumference): ≈ **840 Hz**
- ka = 2 (onset of significant directivity): ≈ **1.68 kHz**
- ka = π (λ = diameter): ≈ **2.64 kHz**

This confirms the common rule of thumb that "a 6.5" woofer starts beaming around 1.5–2 kHz" — it corresponds to ka ≈ 2. Beranek states that above ka = 2 (usually between 800 and 2000 Hz) a direct radiator radiates progressively less power, at 6–12 dB per octave. A convenient practitioner shortcut is the **ka=2 chart**: e.g., a 5" driver should be crossed around 1700 Hz or lower to respect ka=2; a 15" piston is already at ka=2 by ~500 Hz (ka=1 ≈ 250 Hz, severe beaming by ka=5 ≈ 1250 Hz).

**Limitations of the model:** Real cones are not rigid pistons — they flex, have surrounds and dust caps, and the baffle is finite (it acts as a partial waveguide, and edge diffraction perturbs the polar response). Manufacturers striving for pistonic behavior (stiff metal cones) match the model closely up to breakup; drivers with soft cones or "controlled flexure" deliberately deviate. This is why **in-situ measurement beats datasheet curves**.

#### 1b. Directivity matching / power-response continuity — the core of the user's intuition

The classic problem: a 1" dome tweeter is nearly omnidirectional at 2 kHz (a ≈ 1.3 cm → ka ≈ 0.5 at ~2 kHz), while a 6.5" woofer is already beaming (ka ≈ 2.4) at the same frequency. If you cross them at 2–3 kHz, the on-axis response can be flat while the **off-axis and power response show a discontinuity**: the woofer's radiation has narrowed just before handoff, then the tweeter suddenly radiates widely — a "flare" or step-up in off-axis energy right in a region the ear is very sensitive to. Amphion quantifies the raw numbers: the DI of a 126 mm plane piston begins rising (3 dB) at ~867 Hz, while a 26 mm dome doesn't begin until ~4200 Hz — a wide gap at a typical 2.5 kHz crossover. On a spinorama this shows as a bump/dip in the Sound Power and a discontinuity ("kink") in the Directivity Index curve. Toole's framework holds that a smoothly-changing DI is one of the strongest correlates of listener preference; Manny LaCarrubba (Sausalito Audio), interpreting Toole: "DI curves that show strong discontinuities indicate design flaws."

Two ways to fix the mismatch:
1. **Cross lower / use a smaller mid** so both drivers are in their wide-dispersion (pistonic) range at the crossover — e.g., a woofer-mid crossover below ~900 Hz where both are wide, then a mid-tweeter crossover where a small (3–4") mid still has wide dispersion. This is why 3-way and 4-way designs get better DI matching.
2. **Load the tweeter with a waveguide/horn** to narrow its dispersion at the bottom of its band to match the woofer's beaming. Per Arendal's engineering description, the waveguide "broaden[s] top-end dispersion, which the tweeter naturally wants to beam straight ahead, and focus[es] frequencies at the bottom end of the tweeter's range, which naturally wants to spread out in all directions," providing a consistent polar response and "a close match to the dispersion pattern of the mid-bass driver." Neumann KH80, Revel Salon2 (elliptical waveguide matching a 1" dome to a 4" mid), and YG Acoustics use exactly this. A useful empirical note: it is often easier to match directivity at a **higher** crossover than a lower one; one designer found a plain dome + 13 cm mid only matched both SPL and power response well at ~5 kHz, not 2–3 kHz.

**Horn/waveguide directivity is set by mouth size and coverage angle** — Don Keele's 1975 Constant Directivity result (AES 51st Convention preprint, "What's So Sacred About Exponential Horns?"): the frequency below which pattern control is lost is

**f_break = K / (θ · d)**, with K = 25,400 (deg·m·Hz) [or 1×10⁶ deg·in·Hz], θ = coverage angle, d = mouth dimension.

Consequences (directly from Keele's relation): for a fixed coverage angle, every octave lower of pattern control requires **doubling the mouth dimension**; and (counterintuitively) a **narrower** coverage angle requires a **larger** mouth. Above f_break the beamwidth is roughly constant; below it, the horn loses control and the beam widens ("pattern flip" can occur, where vertical and horizontal beamwidths swap — Charlie Hughes/Excelsior Audio). This is why you can't cross a small 1" horn very low and still keep the pattern.

Worked matching example (Geddes-style, oblate-spheroid waveguide with 90° coverage): 15" woofer + 15" waveguide → ~800–900 Hz; 12" + 12" → ~1.2 kHz; 10" + 10" → ~1.5 kHz; 8" + 8" → ~1.8 kHz. The woofer's beaming frequency (where its beam narrows to the waveguide's ~90°) sets where they mate. A 12" cone (~10" effective diameter) narrows to ±50° around 1.2 kHz, which is why 12"+1" compression-driver PA two-ways are routinely crossed ~1.2–1.5 kHz, and 15"+1.4" horns ~800–900 Hz. Geddes' spreadsheet approach (via diyAudio) makes this explicit: e.g., a 12P80Nd woofer matches the 80° beamwidth of a TPL-150H tweeter at 1,420 Hz on an infinite baffle.

#### 1c. Tweeter low-frequency limits

The lower bound on the tweeter crossover is set by **resonance (Fs), excursion, power handling, and distortion**:
- **Fs rule of thumb:** cross at least 1 octave (conservatively 1.5–2 octaves) above Fs. A common conservative commercial rule (per Steve Feinstein, Audioholics) is "18 dB down at resonance": if Fs = 1500 Hz, use a 3000 Hz crossover minimum. Excursion — not thermal power — is usually the binding constraint, because below Fs a constant-voltage drive demands rapidly increasing excursion (diyAudio: "the main obstacle to going close to Fs with tweeters is excursion").
- **Distortion rises steeply below a driver's comfortable range.** Even with a nominally low Fs, measured THD (especially H3) climbs fast: e.g., the Vifa/Peerless XT25 has Fs in the 500–600 Hz range but "should not be used below 2500 Hz, preferably not below 3 kHz" because H2/H3 exceed ~1% below ~2 kHz.
- **Steeper slopes let you cross lower** because they attenuate the sub-Fs excursion/distortion region faster. A 5th-order/steep high-pass essentially eliminates over-excursion and over-power, at the cost of complexity (trivial in DSP).

#### 1d. Woofer/midrange upper limits: breakup and beaming

The upper bound is set by whichever comes first: **beaming** (Section 1a — often the practical limit) or **cone breakup**. Stiff cones (aluminum, ceramic, some Kevlar) are pistonic up to a sharp, high-Q breakup resonance that can be 10–18 dB tall (e.g., SEAS aluminum-cone drivers, Dayton RS180 ~6 kHz, SEAS L18 ~6–7 kHz). Amphion's example: an aluminum cone with a 12 dB resonance peak at 5000 Hz — a 12 dB/octave filter at 2500 Hz only reduces it by 12 dB (just matching the peak), whereas a crossover at 1200 Hz (2 octaves down) reduces it by more than 24 dB. The rule: place the crossover far enough below breakup that the **filter attenuates the peak below audibility** — practitioners often aim for the breakup to be pushed down by more than its height, sometimes to 1/3 or 1/5 of the raw peak. Because of this, metal-cone drivers demand **steep slopes (4th order or higher) and a low crossover** — at least one octave below breakup, usually with an added **notch PEQ right on the breakup frequency**. Note that a notch fixes the on-axis peak but not the associated nonlinear distortion (the cone still resonates and "amplifies" distortion at that frequency), so the safest approach is a low crossover plus a notch. Soft cones (paper, poly, silk domes) roll off gently with self-damping breakup, allowing a higher crossover with a gentler slope but generally more distortion and less "detail."

Voice-coil **inductance** also causes a natural rolloff of the woofer's top end (an electrical low-pass), and this natural rolloff is useful: it adds to the DSP filter to form the acoustic slope (Section 2f).

#### 1e. Baffle geometry: baffle step, diffraction, spacing, acoustic centers

- **Baffle step / diffraction loss:** at low frequencies a speaker radiates into full space (4π); at high frequencies into the forward half-space (2π). The transition produces a ~6 dB "step" (bass is effectively 6 dB lower). Using John Murphy's (True Audio) model, derived from Olson (1969), the 3 dB-down point is **f₃ = 115/W** (W = baffle width in meters), or the common approximation f ≈ 380/W_ft. Rod Elliott (sound-au.com), after Olson, notes the baffle-step effect vanishes when the baffle is wider than ~3 wavelengths and begins ~0.2 wavelength. For a typical ~20–25 cm baffle this lands around 450–575 Hz. In a DSP crossover this is corrected with a **low-shelf or baffle-step-compensation PEQ**, and because it affects broadband tonal balance and driver level matching, it must be included in the design (a driver rated 91 dB can effectively become ~85 dB in the midband after baffle-step compensation).
- **Edge diffraction** creates ripples (peaks/dips) in the on- and off-axis response depending on baffle shape, driver offset, and roundovers; it is best captured by measurement and modeled (VituixCAD has a diffraction/enclosure tool that exports a "cabinet impact response").
- **Driver center-to-center (C-C) spacing vs. wavelength** sets vertical lobing (below).
- **Acoustic center offset:** the tweeter's acoustic origin is usually several cm forward of the woofer's (the woofer's is behind the cone, near the voice coil). This offset must be corrected with **delay** for proper phase summation.

#### 1f. Vertical/horizontal lobing through the crossover

In the crossover region both drivers radiate the same frequencies from two separated points, so their outputs interfere angle-dependently, producing **lobes and nulls**. Rules:
- The interference is worst when C-C spacing is a **large fraction of a wavelength** at the crossover. Keeping C-C spacing under ~1/2 wavelength (some say ~1/4 for the cleanest, widest main lobe) keeps the main lobe broad with no deep off-axis nulls. When ideal ¼-wavelength spacing is unachievable, VituixCAD author Kimmo Saunisto recommends a C-C spacing of ~**1.1–1.3× the crossover wavelength (≈1.2λ)** for the best power-response/vertical-lobing integration, while ~0.5–0.7× the wavelength gives the worst result — so the "best" spacing depends on whether you want the widest clean lobe or the best power-response integration.
- Because drivers are stacked vertically, lobing is primarily a **vertical** phenomenon (horizontal stays clean if drivers are vertically aligned) — this is why the "listening window" is usually wider horizontally than vertically. With wide-spaced designs the clean vertical window can shrink to ~20°.
- **Crossover order and phase set the lobe tilt.** With a symmetric in-phase crossover (LR) and aligned acoustic centers, the main lobe points straight on-axis. If the acoustic centers are offset and uncorrected, the lobe tilts (typically upward, toward the tweeter). Odd-order Butterworth (quadrature summation) inherently tilts the lobe. **Time-aligning the drivers with DSP delay makes the lobe point straight** — PURIFI's tech note states verbatim: "In the case that the EQ filter also compensate for differences in the latency time between the drivers by having an excess phase part in the form of a pure delay, we get a perfect radiation pattern with the main lobe pointing straight out. By reversing the polarity of one driver we can verify that we get a null on axis (the reverse null)."
- The **reverse-null test:** invert one driver's polarity; a correctly-summed crossover then produces a deep null on the design axis. The depth/centering of that null is the standard diagnostic for correct level/phase/delay tracking (Linkwitz used it as his own execution check).

#### 1g. Room, application, and the target

Whether you optimize for **on-axis flat**, **listening-window flat**, or **smooth power/predicted-in-room** depends on the application (near-field studio monitor vs. far-field living room). The dominant modern framework is Toole/Olive's **spinorama**: 70 anechoic measurements on horizontal and vertical orbits (10° increments, at 2 m), reduced to On-Axis, Listening Window (avg of ±10–30°), Early Reflections, Sound Power, and the two Directivity Index curves. The research finding: the **Listening Window should be flat/smooth and the DI curves smooth and monotonic**; the in-room target is a **gently downward-sloping curve**. The seminal 1974 Brüel & Kjær AES study of critical-listening rooms/studios found a "6 dB decrease from 160 Hz to 20 kHz, about 0.9 dB/octave," and the JBL Synthesis and Olive/Harman targets similarly slope roughly 10 dB across the spectrum (~1 dB/octave). This slope arises naturally from a flat, smoothly-directive speaker in a normal room. Critically, you do **not** EQ sound power flat — for a normal narrowing-directivity speaker that would make the on-axis too bright (Olive: equalizing sound power flat "will be done at the expense of the on-axis response, which will be too bright"). And the target itself is room- and speaker-dependent: Dr. Sean Olive (Harman), Nov 2 2009: "If the room is acoustically dead with few reflections and/or the directivity of the loudspeaker is quite high, the in-room response will represent a higher proportion of the direct sound, which should be flat. Using a target curve with large downward tilt will make the loudspeaker sound too dull."

#### 1h. Measurement inputs required

Datasheet curves are for preliminary study only (VituixCAD's manual: "graphically traced or downloaded manufacturer's response data is okay for preliminary studies, but not for final crossover design"). For a real crossover you need, **measured in the actual cabinet at the actual mic position**:
- **Quasi-anechoic (gated) frequency response — magnitude AND phase** for each driver, on-axis and at a full set of off-axis angles (e.g., every 10° to ±90° or ±180°, horizontal and vertical). Phase (or a consistent timing reference) is mandatory so the acoustic offset/delay between drivers is captured.
- **Near-field measurement spliced to the gated far-field** to recover the low-frequency response (gating destroys LF resolution below a few hundred Hz).
- **Impedance (magnitude + phase)** of each driver in its enclosure.
- **Distortion (THD/IMD) vs. level vs. frequency** to set safe crossover points.
VituixCAD's measurement guides insist on a **dual-channel setup with a timing reference** and a consistent mic distance/time offset across all angles (e.g., remove the same ~2.907 ms flight time for 1000 mm from every measurement), because the whole off-axis/power/DI simulation depends on accurate relative phase between drivers. Saunisto explicitly warns against single-channel USB mics for final design work.

### PART 2 — THE OUTPUTS (what a DSP crossover consists of, per channel)

For each driver output channel (miniDSP, Hypex Fusion, Xilica, DBX, Rane, Danville, camilladsp, etc.), the designer sets:

#### 2a. High-pass / low-pass filter: frequency, slope, alignment
- **Corner frequency** and **slope/order:** 6/12/18/24/48 dB per octave (1st–8th order).
- **Alignment / type:**
  - **Butterworth (BW):** maximally flat passband, each section −3 dB at Fc. Odd-order BW sums flat in **quadrature** (outputs 90° apart) — per the Rane primer, the summed vector is +3 dB and 90° out of phase with the input, and the main lobe tilts off-axis. It is polarity-sensitive.
  - **Linkwitz-Riley (LR):** two cascaded Butterworth sections of half the order → even orders (LR2, LR4, LR8), each output **−6 dB at Fc**. Outputs are **in-phase**, sum to **flat** magnitude, and (for LR4) produce **no lobe tilt** on the design axis. **LR4 (24 dB/oct)** is the de-facto standard for active/DSP crossovers (Rane: "the de facto standard for professional audio active crossovers is the 4th-order Linkwitz-Riley"): steep enough to protect drivers and suppress breakup, well-behaved phase, flat in-phase summation. LR2 sums flat only if **one driver's polarity is inverted** (otherwise a deep null); even inverted, LR2 has a 180° phase difference away from Fc. LR8 (48 dB/oct) returns the outputs to fully in-phase with the input at Fc.
  - **Bessel:** maximally flat group delay (best transient/phase linearity), gentler effective slope; sums with some ripple. Used when phase linearity is prized over steepness.
  - **Elliptic/Cauer:** very steep transition with passband/stopband ripple; occasionally used to notch a nearby breakup while crossing.

  Note on power response: even a perfect LR crossover sums flat in **voltage** but produces a ~3 dB **power** dip at Fc for non-coincident drivers (Rane/Vanderkooy-Lipshitz) — another reason directivity matching matters.

#### 2b. Gain / level trim
Each driver is level-matched to the others (sensitivity matching). Determined from the **measured** in-cabinet sensitivities **after baffle-step loss is accounted for** (the woofer's midband is effectively attenuated ~4–6 dB by baffle step, so the tweeter must be padded to match). In DSP this is a simple per-channel gain (dB).

#### 2c. Polarity
Per-channel polarity invert (0°/180°). Needed for **LR2** (invert one driver), for **odd-order** alignments, and sometimes to optimize the summation/null when acoustic offsets and driver phase don't cooperate. The reverse-null test is used to confirm the correct choice. (Note: inverting a driver makes the response correct at Fc but 180° off elsewhere — an audible tradeoff for some listeners, which is why in-phase LR alignments are preferred.)

#### 2d. Delay / time alignment
Per-channel delay (µs/ms or mm) corrects the **acoustic center offset**. Measured by:
- **Impulse/step response:** run each driver, find the impulse arrival-time difference (needs a consistent timing reference — e.g., REW's "Use Loopback as Timing Reference" or a dual-channel soundcard). The difference in impulse peaks = required delay. miniDSP's procedure: add a known delay (e.g., 1.0 ms) to separate the impulses, measure the peak-to-peak gap, subtract the added delay.
- Or drive both simultaneously and read the time gap between impulses.
The subtlety: **fixed delay corrects a pure time offset, but true phase alignment through the crossover region requires the two drivers' phase to track across the whole overlap**, not just at one point. A minimum-phase IIR filter carries its own phase shift; DSP delay adds a linear (excess) phase term. Getting a deep, centered reverse-null across a band (not just a point) is the sign that delay + filter + level all track. Only DSP (or a stepped/sloped baffle, or a tweeter waveguide that recesses the acoustic center) can add the pure delay a passive minimum-phase network cannot — Rod Elliott: an offset uncorrected produces "a significant response dip at the crossover frequency."

#### 2e. Parametric EQ (PEQ)
Per-channel PEQ (peak/notch, low-shelf, high-shelf) is used to:
- Flatten each driver's raw in-cabinet response over its passband (± an octave beyond the crossover).
- Apply **baffle-step compensation** (low-shelf).
- **Notch breakup peaks** and resonances (negative-gain peak filters).
- Correct diffraction ripples.

This supports the **"flatten then cross" philosophy:** miniDSP's official guidance is to "use the PEQ blocks on each output channel to shape the response of each driver so that it is flat over its operating range… ideally, flatten the response to an octave above or below the crossover frequency," then apply the crossover block. The alternative is to **use the driver's natural acoustic rolloff as part of the target slope** (fewer filters, but you must model the combination). Most DSP designers flatten first because DSP filters are "free."

#### 2f. Electrical vs. acoustic slope — the crucial distinction
The target is always the **acoustic summed response**, not the electrical filter shape. Each driver's **own natural rolloff** (woofer inductance rolloff/breakup at the top; tweeter Fs rolloff at the bottom) **adds to** the DSP's electrical filter to produce the **acoustic slope**. Therefore the DSP settings are **rarely the textbook filter values.** Example: to get an **acoustic LR4** on a woofer that already rolls off naturally, you might dial in only an electrical 2nd-order low-pass (or an asymmetric slope) plus PEQ. Designers explicitly speak of achieving an "acoustic LR4 target" via electrical 2nd-order filters shaped by EQ; Zaph Audio famously "use[d] a couple of components combined with the driver's natural rolloff to reach 4th-order target slopes." This is why you can't just read the crossover off the box — you design toward the acoustic curve and let the tools tell you the electrical settings. (This is also why an "asymmetric LR4" exists in practice — a steeper electrical slope on one driver to compensate a delay mismatch and keep the lobe centered.)

#### 2g. FIR vs. IIR
- **IIR (biquad) filters** are minimum-phase, low-latency, cheap; they carry phase shift (miniDSP: a summed LR4 at 300 Hz "shifts by 360 degrees from low frequencies to high"). This is the normal choice for the crossover itself.
- **FIR filters** can be **linear-phase** (miniDSP: "the phase shift is very close to zero across the audio band"), and can realize arbitrary magnitude+phase targets. Worth it when you want linear phase (especially the low-mid crossover), very steep transitions, or complex correction. Costs: **latency** (proportional to tap count/steepness — can exceed 10 ms; a problem for AV sync and live monitoring), high DSP load, and **pre-ringing** on steep filters (a non-causal artifact). Practical guidance from listening-test research: keep FIR crossover order under ~600 taps at 1–3 kHz to avoid audible ringing; at 100–300 Hz thousands of taps are fine. A common hybrid: **IIR for the crossover + a global FIR all-pass to linearize phase**, or FIR only where its benefit (low-frequency linear phase) is greatest — note some processors (e.g., certain miniDSP/DEQX) can't run FIR much below ~90–300 Hz, so low-frequency IIR + FIR-above is typical. The Hypex/AES caution (AES 123rd preprint): overly steep FIR crossovers can worsen **off-axis** response and add pre-ring even when on-axis is perfect, because the two drivers' ringing only cancels on the design axis.

#### 2h. Limiters and system EQ
Final stages: per-driver **limiters** (peak/RMS, look-ahead) for driver protection (look-ahead adds latency, so studios may avoid it), and an overall **system/house-curve EQ** (the gentle downward tilt / room target) applied globally after the crossover and per-driver correction. In processors with room correction (Dirac Live), the per-driver PEQ flattens the drivers and Dirac does the global response shaping and room correction.

### PART 3 — THE PROCESS / WORKFLOW

1. **Measure each driver individually, in the final cabinet, on and off axis** (gated/quasi-anechoic for the far field, near-field spliced for the bass), plus impedance and distortion. Use a dual-channel setup with a consistent timing reference so relative phase/acoustic offset is preserved. Name/organize files by angle for the simulator (VituixCAD parses angle from the filename).
2. **Choose a candidate crossover frequency** from the intersection of: directivity matching (ka/beamwidth vs. the tweeter/waveguide coverage), tweeter Fs/excursion/distortion (≥1–2 octaves above Fs), woofer breakup (≥1 octave below, with margin for the slope), and C-C spacing (keep lobing acceptable at that wavelength).
3. **Import measurements into simulation software** — **VituixCAD** (the modern free standard), **REW** (measurement + basic crossover/EQ, plus Charlie Laub's Active Crossover Designer), ARTA, Praxis, LspCAD, SoundEasy, or the FRD/ZMA workflow — and **simulate the crossover before implementing it.** VituixCAD simulates full polar/power/DI response from your angle set.
4. **Flatten each driver with PEQ** to a target over its band (± an octave past Fc), then **apply crossover filters** to hit the **acoustic** target (commonly acoustic LR4). Set **level, polarity, and delay.** With a dual-channel FFT (ARTA/REW) you can watch each driver's transfer function flatten in real time, then choose the crossover.
5. **Optimize** against multiple targets simultaneously:
   - Summed **on-axis / listening-window** flat (to the chosen house curve).
   - **Phase tracking** through the crossover — verified by a deep, centered **reverse-null** when one driver is inverted.
   - **Off-axis / polar** smoothness and **power response / DI** continuity (no bump at the crossover).
   - **Vertical lobing** (main lobe on the listening axis, no null at ear height).
   These goals conflict (e.g., time-aligning for a straight lobe vs. flat on-axis vs. flat power), so the designer weights an **error function** across on-axis + a set of off-axis angles. Some use VituixCAD's **optimizer**; many tune manually because the optimizer can chase on-axis flatness at the expense of polar behavior.
6. **Verify by measurement, then iterate.** Small sim-vs-measurement discrepancies are expected and are hunted down (timing offset, mic position, diffraction).

**Rules of thumb practitioners actually use:**
- Cross a tweeter ≥ 1.5–2× its Fs (steeper slope → can go lower); the conservative "18 dB down at Fs" rule for reliability.
- Cross a woofer ≤ its ka≈2 beaming frequency, and ≥ 1 octave below breakup with a steep slope + notch for metal cones.
- Use the woofer's **−6 dB @ 30° off-axis** frequency as the highest sensible crossover point (a directivity-based ceiling).
- LR4/24 dB-oct as the default alignment; go steeper to protect drivers or suppress breakup.
- Keep C-C spacing small; cross low if drivers are far apart (or target ~1.2λ per Saunisto when ¼λ is impossible).
- Match a waveguide's rated coverage angle to the woofer's beamwidth at Fc.
- Flatten-then-cross with DSP because filters are free; always design to the **acoustic** curve.
- Always run the reverse-null test.

### Worked examples

- **6.5" woofer + 1" dome tweeter (2-way):** woofer beams at ka≈2 ≈ 1.7 kHz; tweeter wants ≥ 2× Fs (~1.2–1.8 kHz typical). Classic conflict: crossing at 2–2.5 kHz keeps the tweeter safe but the woofer is already narrowing → a power-response bump. Mitigations: (a) a **waveguide** on the tweeter to narrow its low end to match the woofer (best), (b) a lower crossover (~1.8 kHz) with a steep LR4/LR8 and a robust low-Fs tweeter, or (c) accept a small DI bump. Typical DSP: acoustic LR4 ~2.0–2.2 kHz, tweeter padded ~4–6 dB (after baffle step), tweeter delayed a few hundred µs, PEQ to flatten both and notch any woofer breakup. (The industry example: crossing at ~2.2 kHz is "high enough to keep the tweeter well above its Fs and reduce excursion/distortion, low enough to avoid significant beaming from the 6.5" woofer and below its breakup.")
- **12" woofer + 1" compression driver on a 90° horn (2-way PA):** 12" (~10" effective) narrows to ~±50° by ~1.2 kHz; a 1" CD on a 90° horn keeps pattern control down to ~1.2–1.5 kHz (mouth-size limited). Cross ~1.2–1.5 kHz, LR4 or steeper, to protect the CD (well above its usable limit, e.g., B&C DE250 capable of ~1.2–1.3 kHz) and match directivity. For 15" + 1.4" on a larger horn, ~800–900 Hz (Beyma's engineered designs cross a 15" to a 1.4" CD at ~800–900 Hz for a smooth polar match).
- **3-way (e.g., 10–12" woofer + 4–5" mid + 1" tweeter/waveguide):** low crossover (woofer→mid) placed where both are wide/pistonic (~300–500 Hz), and mid→tweeter where a small mid still disperses widely (~2–3 kHz) or where a waveguide matches the mid. 3-way exists largely to keep DI matched at both crossovers — you avoid asking any one driver to work near its beaming or breakup limit.

## Recommendations

**Stage 1 — Measure properly (do this before touching a filter).** Build the final cabinet, then take gated on/off-axis magnitude+phase, near-field bass, impedance, and distortion-vs-level for each driver with a dual-channel timing reference. If you only have a single-channel USB mic, fix that first — the entire off-axis/phase optimization depends on accurate relative timing. *Benchmark to proceed:* clean gated data with a usable time window down to a few hundred Hz, plus a near-field splice for the bass.

**Stage 2 — Pick the crossover from directivity + limits, not habit.** Compute the woofer's ka≈2 beaming frequency and the tweeter/waveguide coverage; pick Fc where their beamwidths match. Cross-check against tweeter Fs (≥1.5–2×) and woofer breakup (≥1 octave below). *Threshold that changes the plan:* if the directivity-matched Fc is below what the tweeter can safely handle, add a waveguide or go 3-way rather than forcing a low crossover on a bare dome.

**Stage 3 — Flatten, then cross, to an acoustic target in simulation.** PEQ each driver flat ± an octave past Fc, target acoustic LR4, set level/polarity/delay, and simulate the full polar/power/DI in VituixCAD before committing. *Benchmark:* smooth, monotonic DI with no bump at Fc; deep centered reverse-null; main vertical lobe on the listening axis.

**Stage 4 — Decide FIR only if you have a specific reason.** Use IIR for the crossover by default. Add FIR/linear-phase only for a low-mid crossover where phase linearity is audible, or global phase linearization — and only if the added latency is acceptable for your use (not live/AV-sync-critical). *Threshold:* if latency budget < ~10 ms or you watch video, stay IIR or restrict FIR to low frequencies; keep FIR taps modest (<~600) at 1–3 kHz to avoid audible pre-ring.

**Stage 5 — Verify, iterate, then apply the house curve and limiters.** Re-measure the summed speaker, confirm the sim, then apply a gentle downward in-room tilt (~0.9–1 dB/oct, per the B&K/Harman findings) and set protective limiters last. *Threshold:* if the in-room top end sounds bright/dull, adjust the tilt rather than the crossover (flatter for dead/high-directivity rooms, more tilt for live rooms); if a driver is at risk, tighten the limiter, don't move Fc up blindly.

## Caveats
- The piston-in-infinite-baffle model is idealized; real cones flex, baffles are finite, and diffraction perturbs the polars — always trust in-situ measurement over the model or datasheet.
- The −6 dB beamwidth-vs-ka figures are computed from the exact Bessel formula (cross-checked against published −3 dB angles); the −6 dB beamwidth is undefined below ka≈2.2 (the pattern never falls 6 dB in the forward hemisphere), and at ka=3 it is ~95°, slightly wider than the often-quoted 80–90°.
- "Best" target (on-axis flat vs. power-flat vs. sloped in-room) is application- and room-dependent; the downward in-room tilt (~1 dB/oct) is a preference finding, not a law, and shifts toward flat for dead rooms or high-directivity speakers (Olive).
- Center-to-center spacing guidance is not one-size-fits-all: the goal (widest clean lobe vs. best power-response integration) changes whether you want spacing well under ½λ or nearer ~1.2λ (Saunisto).
- Notching a metal-cone breakup fixes the on-axis peak but not the underlying nonlinear distortion; a low crossover is the safer fix.
- FIR is not automatically better: steep FIR crossovers can worsen off-axis response and introduce pre-ringing even with perfect on-axis summation (Hypex/AES).
- Some quantitative anchors (Keele's K constant, Beranek's DI curve, the B&K slope) are well-established textbook/AES results; forum-sourced worked crossover points (specific driver combos) are practitioner consensus, not lab-certified, and should be validated by your own measurements.
