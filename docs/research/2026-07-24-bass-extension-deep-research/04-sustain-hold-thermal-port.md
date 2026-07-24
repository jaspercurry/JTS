# Sustain-Hold Testing for Thermal & Port Compression in Loudspeaker Commissioning: An Engineering Assessment

> **Provenance:** external deep-research report commissioned by the maintainer,
> delivered 2026-07-24. Archived verbatim below (research artifact, not a
> contract — actionable deltas route through wave revisions; see this
> directory's [README](README.md)).

## TL;DR

* The 30/60/90 s holds are long enough to characterize voice-coil (fast) heating but far too short to reach thermal equilibrium; they capture only roughly 45%/58%/62% of the eventual steady-state thermal compression, systematically missing the magnet/frame heat-soak contribution (~35–40% of the total), which develops over 15–35 minutes. Extend the hold, fit-and-extrapolate the sag curve, or apply a runtime derating margin.
* Port compression is fluid-dynamic and essentially instantaneous (onset within the first cycles/seconds), so any of the three hold durations detects it; hold length is irrelevant for the port. The real problem is that the 5% corner-shift criterion is too tight and confounded — it sits inside the measurement/mechanical-warm-up noise floor and mixes up two opposite-direction effects (port compression raises fb; suspension warm-up lowers Fs).
* Recommended: convert the 1.5 dB sag limit into an extrapolated asymptotic-compression limit (~2.5–3 dB), loosen and make directional the corner-shift criterion (≈+8–10% upward, treat downward shifts as mechanical), and apply ~1.0–1.5 dB of headroom derating on the boosted LF target to cover un-captured magnet heat soak and program-dependent cooling variance.

## Key Findings

1. Two- to three-stage thermal model is the correct mental model. A small woofer heats in nested stages: voice coil (τv ≈ 5–25 s), pole-tips/gap (τg ≈ tens of seconds), and magnet/frame/basket (τm ≈ 15–35 min). The coil's final temperature — and therefore its final compression — depends on all three because the coil sits on top of the slowly-charging magnet reservoir.
2. A ≤90 s hold saturates the coil's own fast transient but sees essentially none of the magnet soak. With representative measured parameters, the coil reaches ~45–65% of its steady-state temperature rise within 30–90 s; the remaining ~35–40% accrues over the next 15–35+ minutes.
3. Port compression is a Reynolds-number/velocity phenomenon, not a thermal one. It appears within seconds, manifests as reduced port output plus an upward shift of the tuning frequency fb (effective port shortening from turbulence), and can reach ~10 dB at tuning at extreme levels. Thermal heating of port air is negligible.
4. The 5% corner-shift threshold is poorly calibrated. Klippel's own AN42 data show that measurement-condition factors alone — drive amplitude (±3.01%), climate (±4.05%), and test history (±4.62%) — each move the measured resonance by roughly the same 5%, and unit-to-unit production spread averages ±4.9%. Suspension warm-up (a mechanical, viscoelastic effect) lowers Fs during a hold and directly fights the port/thermal signature that raises the corner.
5. A 1.5 dB sag limit is reasonable in magnitude but tier-inconsistent. Because each tier captures a different fraction of the asymptote, a fixed observed-dB limit means the aggressive tier actually tolerates more eventual compression than the conservative tier.

## Details

### 1. Voice-coil vs. magnet thermal time constants (4–8" woofers)

The standard lumped model (Henricksen 1987; Zuccatti 1990; Button 1992; Chapman 1998; Klippel 2004) represents the driver as a cascade of RC "integrators": Rtv·Ctv for the coil, Rtg·Ctg for the pole tips/gap, and Rtm·Ctm for the magnet/frame, with the ordering τv ≪ τg ≪ τm. Klippel's formulation gives the steady-state coil rise as ΔTvss = (Rtv + Rtg + Rtm)·P, the magnet rise as ΔTmss = Rtm·P, and the coil step response as a sum of three decaying exponentials.

Representative measured numbers (primary sources):

* Klippel, "Nonlinear Modeling of the Heat Transfer in Loudspeakers" (JAES vol. 52, 2004), example Driver A (woofer): Rtv = 5.9 K/W, Ctv = 3.6 Ws/K → τv ≈ 21 s; Rtm = 4.1 K/W, Ctm = 272 Ws/K → τm ≈ 18.6 min. Measured effective total thermal resistance varied from 4.6 to 7.5 K/W (≈60% spread) depending on program spectrum — a critical caveat: "Significant variation (60%) are found in the measured thermal resistance depending on the properties of the signal."
* Chapman, "Thermal Simulation of Loudspeakers" (AES preprint 4667, 1998), measured drivers:
   * 170 mm woofer: coil τ ≈ 9–15 s; intermediate gap/pole τg ≈ 83 s; magnet τ ≈ 29 min (3rd-order fit); total Rt ≈ 7.7 K/W.
   * 70 mm driver: coil τ ≈ 5.6 s; magnet τ ≈ 33 min; total Rt ≈ 11.7 K/W.
   * 19 mm tweeter: coil τ ≈ 0.84 s; magnet τ ≈ 6.5 min (much higher Rt ≈ 62 K/W).
* General engineering figures: loudspeaker-protection patents state the coil constant is "of the order of a few seconds," and a "typical loudspeaker" implementation uses ~2 s; practitioner summaries put the coil at "seconds" and magnet saturation at "minutes to hours."

What a 60 s hold reveals vs. misses. Using Driver-A-like values (τv ≈ 20 s, τg ≈ 83 s, τm ≈ 1115 s; Rtv ≈ 5.9, Rtg ≈ 1, Rtm ≈ 4.1 K/W, total ≈ 11 K/W), the coil temperature reaches the following fractions of its steady-state rise:

* 30 s → ≈ 45%
* 60 s → ≈ 58%
* 90 s → ≈ 62%

The magnet path alone accounts for Rtm/Rt ≈ 37% of the final coil temperature and charges with τm ≈ 15–35 min, so at 90 s essentially none of it is captured. To bring "the pole plates, magnet and frame in thermal equilibrium," Klippel specifies a hold > 4·τm (i.e. > ~60–140 min); its standard power-test template uses a 3-hour total time with 25-min ON / 5-min OFF cycles. For the coil alone, Klippel reads equilibrium at ≈ 5·tslope (a few × τv, i.e. well under 2 minutes). This is the crux: ≤90 s is adequate for the coil transient, but the sag will keep growing for tens of minutes as the motor heat-soaks — a passing short hold does not bound steady-state compression.

Because compression (in dB) is approximately proportional to coil temperature rise for modest rises (Button/Klippel: PC ≈ 10·log10(1 + δ·ΔTv) plus the efficiency term, with δ ≈ 0.0393 K⁻¹ for copper), the captured-fraction figures above map roughly onto captured-fraction-of-compression. A 1.5 dB sag observed at 60 s therefore implies an eventual steady-state compression on the order of 1.5/0.58 ≈ 2.6 dB — about 1 dB more develops after the hold ends.

### 2. What the standards say about duration and compression

* AES2-2012 (revision of AES2-1984): band-limited pink noise over the driver's working range, crest factor raised to 12 dB (4:1) from the older 6 dB (2:1) — the standard states this "is the expected crest factor for a pink noise source of that length" — with a two-hour driven test period (per B&C and EAW application notes implementing the standard). The 2-hour figure is chosen so the motor structure — not just the coil — reaches thermal steady state. AES2-1984 also prescribes the loudspeaker be "'broken in' for approximately 30 minutes at a low power level" before rating, with "30 minutes between increments for cooling."
* IEC 60268-5 (loudspeakers): pink noise weighted per IEC 60268-1, crest factor 3–4 (~6 dB), with a long-term (100-hour) rated-noise-power test and preconditioning requirements; defines power compression as "reduction in sensitivity of a loudspeaker with increasing input voltage or power."
* IEC 60268-21:2018 (acoustical, output-based measurements) is the modern, system-level standard most relevant here. It explicitly defines "time-varying amplitude compression of the fundamental component" and amplitude compression at maximum input, plus short-term and long-term maximum SPL, and mandates preconditioning and stated ambient/climatic conditions. Its whole premise — output-based "black-box" measurement of active DSP systems with no access to internal states — matches a feedforward active speaker with no internal sensing.
* EIA/CEA-426-B adds an explicit power-compression test (sweep + band-limited pink noise at half rated power).

The unifying message: every power/compression standard uses durations of hours, not seconds, precisely because thermal equilibrium in the motor takes that long. A commissioning hold of ≤90 s is a deliberately abbreviated screen, not a steady-state compression measurement, and should be treated as such.

### 3. Klippel large-signal / power-compression literature

Klippel distinguishes instantaneous (per-cycle, causing thermal distortion at low frequencies), short-term (coil-dominated, seconds), and long-term/steady-state (motor-dominated, minutes-to-hours) compression. The Thermal Parameter Measurement (AN18/AN19) and Power Test (PWT) modules extract τv from the fast decay in the OFF-phase (reading temperature at 5·tslope) and τm from the slow decay, and the Klippel Endurance Test (KET) steps level every 5 minutes, noting that "Thermal equilibrium may take hours to settle, especially for larger woofers." The core quantitative takeaways: the coil stabilizes in a handful of τv (seconds to ~2 min); the magnet/frame stabilizes only after several τm (many minutes to hours); and effective thermal resistance varies by ~60% with program spectrum, so a single noise band under-represents worst-case bass-heavy content.

### 4. Port compression: fluid-dynamic and essentially instantaneous

Port ("vent") compression is governed by air velocity and Reynolds number, not temperature. Key references and thresholds:

* Onset of turbulence/chuffing is most commonly stated as the engineering rule "design for a max velocity of 5% of the speed of sound, or about 17 m/s (~55 ft/s)"; flared/double-flared aero ports raise the usable velocity (roughly to ~25 m/s), and a widely cited absolute limit of ~34–35 m/s appears in the literature — e.g. US Patent 8,019,088 (loudspeaker LF-extension/protection) states "An example of a maximum port velocity is approximately 35 m/s," with the object of limiting the "extraneous noise called 'chuffing.'"
* Reynolds-number framing (audioXpress, "How Good Is Your Port"): turbulence onset near Re ≈ 20,000, with clear compression by Re ≈ 50,000 ("When the Reynolds number hits 50000, your vent is compressing").
* Klippel ("Loudspeaker Nonlinearities"): port loss factor Q stays > 50 at low amplitude but "goes down to 10 and less for particle velocities above 20 m/s," as the air plug breaks up and kinetic energy is convected into the far field — a nonlinear flow resistance Rp(v).
* Salvatti, Button & Devantier, "Maximizing Performance from Loudspeaker Ports" (AES preprint 4855; JAES vol. 50, 2002): measured port compression "on the order of 10 dB at port tuning at the highest power levels," and — critically for the corner-shift metric — "Resonance increases as ports compress," because the effective port length decreases as turbulence increases, raising fb.

Because the mechanism is aerodynamic, port compression appears within the first cycles (sub-second to a few seconds) and is fully present long before 30 s. Heating of the air in the port is negligible (the air is continuously exchanged; Klippel notes the thermal capacity of the air layer is negligible). Therefore hold duration is irrelevant to detecting port compression — even a 1 s hold at level would show it. A sustained-drive fb/corner shift shows up as an upward movement of the lower impedance minimum / system corner and a drop in port output relative to cone output.

### 5. Accept/reject thresholds in industry QA

* Sag/compression: Real drivers routinely show 3–4 dB compression at nominal (rated) power (e.g. a published 15" pro driver: 3.9 dB at nominal, 2.8 dB at half power, 1.0 dB at one-tenth power). Production/spec practice commonly treats 1 dB as "mild," ~3 dB as the "significant" onset used to define usable max SPL. Listen Inc. SoundCheck's Max-SPL sequence uses Compression as an explicit user-set limit driven up in 3 dB then 0.5 dB steps. A 1.5 dB sag limit is therefore a sensitive screen — reasonable, on the tighter side of published practice, and well inside the level where drivers are still safe.
* Corner/resonance-frequency tolerance: Klippel AN42 ("Tolerances of Resonance Frequency fs") reports unit-to-unit (Inter-ICI) production spread of ±3.65% (4" neodymium), ±1.43% (4" ferrite), ±6.58% (6.5" 4Ω) and ±6.88% (6.5" 8Ω), mean ±4.9%, and shows that single measurement-condition factors move fs by ±3.01% (drive voltage), ±4.05% (climate), and ±4.62% (test history) — concluding "The voltage, climate and history are the dominant factors causing variation of the measured resonance frequency fs." End-of-line fs tolerances are typically set at ±5% to ±10%, and AN42 explicitly advises that "the tolerances for fs should be larger than required by other factors" because of climate/history drift.

Verdict on the current criteria. The 1.5 dB sag limit is well-calibrated in magnitude (sensitive but not absurd). The 5% corner-shift limit is too tight and mis-specified: it equals the unit-to-unit production spread and the single-factor measurement-condition variation, so it will generate false rejects from measurement drift and mechanical warm-up rather than genuine port/thermal compression.

### 6. Measurement caveats and confounders

* Room/mic drift: Over 30–90 s, ambient and mic drift are usually < 0.1 dB but not zero. Use a near-field or sealed-cavity capture and a fixed reference; compare start-of-hold and end-of-hold within the same acquisition to cancel slow drift.
* Amplifier / DSP limiter action: A level drop from the amp clipping or a DSP limiter engaging looks exactly like thermal sag. With no current/voltage sensing you cannot measure it directly, so log the commanded DSP output level and any limiter/gain-reduction state during the hold and reject holds where the limiter acted; verify the amplifier is not voltage- or current-limited at the boosted target.
* Suspension warm-up / mechanical break-in (the key corner-shift confounder): Per AN42, driving the suspension at large amplitude causes an irreversible compliance rise ("break-in") — "Operating a suspension at high amplitudes over some time causes an irreversible rise of the compliance Cms which is well known from long-term power testing after 'breaking in'" — plus a short-term visco-elastic softening ("The compliance Cms of the suspension decreases for a short time (a few seconds) after having a larger displacement"). Both lower Fs. This is a mechanical effect, not thermal. During a hold it pushes the resonance/corner down, partly cancelling the upward shift from port compression — so a "small" measured net corner shift can hide a large port compression, and a "large" negative shift can be pure mechanical warm-up. A ±5% symmetric corner-shift gate cannot distinguish these. AN42's explicit remedy: "Perform the small signal measurement before the large signal measurements."
   * Interaction with a 5% fail criterion: first-minutes suspension softening alone can approach or exceed 5% Fs shift, so a conservative (90 s) hold is more likely to false-fail on mechanical warm-up than the 30 s hold — the opposite of the intended safety ordering.

## Recommendations

Stage 1 — Fix the corner-shift criterion (highest priority, do first).

* Replace the single symmetric ±5% gate with a directional, wider gate: flag an upward corner/fb shift (port compression signature) at ~+8–10%, and treat downward shifts as suspension warm-up (mechanical) rather than a thermal/port fault.
* Add a short (5–15 s) large-signal pre-conditioning burst before the measured hold so the fast visco-elastic softening has largely settled, and always run the small-signal impedance/corner measurement before any large-signal steps (per AN42). Benchmark: if repeated holds on the same unit disagree by > 3–4% in corner frequency, the metric is noise-limited and must be widened or the pre-conditioning lengthened.

Stage 2 — Make the sag criterion tier-consistent via extrapolation.

* Log the full sag(t) trajectory during the hold and fit a two-term exponential (fast τv term + slow τm term). Extrapolate to the asymptote and apply a single asymptotic-compression limit of ~2.5–3 dB across all tiers, instead of a fixed observed-dB limit. This removes the tier inconsistency (currently 1.5 dB observed = ~2.6 dB eventual at 60 s but a larger eventual value at 30 s).
* If curve-fitting is not feasible at runtime, apply tier-specific observed limits that back out to the same asymptote — e.g. roughly 1.2 dB (30 s) / 1.5 dB (60 s) / 1.6 dB (90 s) to represent the same ~2.6 dB steady-state target, using the captured-fraction table above (re-measure the fractions on your actual driver, since Rtm/Rt varies). Note that a two-term fit from a ≤90 s window constrains only the fast (τv) term well; the slow (τm) term is poorly identified from such a short record, so treat the extrapolated asymptote as a lower bound and pair it with Stage 3.

Stage 3 — Apply a runtime headroom derating on the boosted LF target.

* Because a passing short hold does not bound the magnet-soak compression (~35–40% of steady state, developing over 15–35 min) or worst-case bass-heavy program cooling (~60% Rt spread), apply ~1.0–1.5 dB of headroom derating on the boosted bass-extension target. Justification: the ~40% uncaptured magnet fraction turns a 1.5 dB screen into ~2.6 dB eventual (~1.1 dB gap); round up to ~1.5 dB to also cover spectral/cooling uncertainty and driver-to-driver Rtm variation.
* Benchmarks that would change this: if you extend the conservative-tier hold to capture the magnet term, or add a fitted-asymptote accept criterion, the derating can shrink toward ~0.5 dB. If field program material is known to be bass-heavy and continuous (e.g. EDM, cinema LFE), hold the full ~1.5 dB or more.

Stage 4 — Consider a longer conservative tier.

* The current tiers (30/60/90 s) all live on the coil transient. To make the "conservative" tier meaningfully address motor heat soak, it would need to be many minutes (ideally > 4·τm ≈ tens of minutes) — impractical on a line. The pragmatic substitute is the fit-and-extrapolate approach of Stage 2 plus the derating of Stage 3.

## Caveats

* Compression-vs-temperature linearity is an approximation; the captured-fraction figures (45/58/62%) are illustrative using Driver-A-like parameters. Re-measure τv, τg, τm and Rtm/Rt on representative units (via a Klippel-style OFF-phase decay read or a controlled long power test) before finalizing thresholds — the magnet-fraction Rtm/Rt is the single most important number and varies with motor topology (ferrite vs. neodymium, vented vs. sealed pole, aluminum vs. polyimide former).
* Patent literature loosely calls the magnet constant "tens of seconds," which conflicts with peer-reviewed measured woofer values of 15–35 minutes; trust the measured JAES/AES data for 4–8" woofers. (The "tens of seconds" figure appears to describe microspeakers or is an over-simplification.)
* Standards durations (2 h AES2, 100 h IEC) are destructive/endurance ratings, not production screens; they are cited here to establish the equilibrium timescale, not as a target for line testing.
* Several corroborating velocity/compression figures come from enthusiast/manufacturer web sources; the primary quantitative anchors (Salvatti/Button/Devantier JAES 2002, Klippel 2004, Chapman AES 1998, Klippel AN42/AN18/AN19, AES2-2012, IEC 60268-21:2018) are authoritative and mutually consistent.
* The device has no current/voltage sensing, so limiter/clipping confounds cannot be measured directly — the recommendation to log commanded DSP level and limiter state is a mitigation, not a substitute for sensing.
