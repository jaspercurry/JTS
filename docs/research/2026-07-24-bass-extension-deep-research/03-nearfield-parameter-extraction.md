# Extracting Loudspeaker LF Alignment Parameters from a Single Near-Field Magnitude Capture: A Measurement-Science Assessment for an Open-Source Commissioning Flow

> **Provenance:** external deep-research report commissioned by the maintainer,
> delivered 2026-07-24. Archived verbatim below (research artifact, not a
> contract — actionable deltas route through wave revisions; see this
> directory's [README](README.md)).

## TL;DR
- A single fixed-position near-field (NF) magnitude capture can recover sealed-box f0/Qtc to roughly ±3–8% (f0) and ±10–25% (Qtc) with a good calibrated USB mic, but ported fb and passive-radiator (PR) parameters are materially harder because they require correct area-weighted magnitude+phase summation that a single magnitude-only capture cannot fully supply.
- Microphone calibration should be REQUIRED for smartphone mics and merely RECOMMENDED for known-model USB measurement mics: absolute-sensitivity error cancels in a shape-relative fit, but the sloped LF roll-off of phone mics does not, and a ~1 dB tilt near the knee biases fitted Qtc by ≈10–12% (fitted f0 far less).
- Fit-residual thresholds should trigger warnings around ~1 dB RMS / ~3 dB max over the fitted band, with residual *shape* used to diagnose the failure mode (mic too far, mic moved, room contamination, unmeasured/mis-weighted port, clipping, low SNR, wrong box model).

## Key Findings

1. **Keele's method is valid only in the piston band.** The upper frequency limit is ka ≈ 1 (soft limit; some authorities use ka = ½), giving f_max ≈ 10950/D (D in cm) or 5475/(π·a) — e.g. ~1043 Hz for a 10.5 cm effective diameter. The mic must sit within 0.11× the effective cone radius for <1 dB error. For LF alignment fitting (typically <200 Hz) this ceiling is not the binding constraint; the binding constraints are baffle/diffraction effects, port summation, and SNR at the bottom of the band.

2. **Near-field ≠ anechoic.** NF captures the 2π (half-space) infinite-baffle response and omits baffle-step diffraction (a transition from 4π to 2π that adds ~6 dB and ripples across roughly 100–800 Hz for a small baffle). For pure LF shape-fitting below ~150 Hz on a small enclosure this is a second-order effect, but it contaminates the fit if the fitted band extends up into the baffle-step region.

3. **Port/PR summation is the hard problem.** The port (or PR) NF response must be scaled by √(S_port/S_driver) — i.e. 20·log10(√(S_i/S_1)) in dB — and *vectorially* summed (magnitude AND phase). A single magnitude-only capture of one radiator cannot reconstruct fb correctly on its own; the cone NF response shows a null at fb whose depth encodes box losses (Ql), and this null (not a full summation) is the most robust single-magnitude route to fb.

4. **Magnitude-only fitting is well-conditioned for sealed boxes, poorly conditioned for ported/PR.** A 2nd-order high-pass has two shape parameters (f0, Qtc) plus a level scalar; these are cleanly identifiable from the knee region. A 4th-order ported alignment has fb, Qb (box/leakage Ql), plus driver/box parameters that trade off against each other in magnitude-only data (fb vs Ql vs leakage degeneracy).

5. **Commercial auto-setup systems do NOT fit a physical box model.** SVS, KEF, Neumann MA 1, Genelec GLM, Dirac, Audyssey, Anthem ARC, Trinnov, and Sonos Trueplay all measure an in-room *listening-position* response and apply corrective EQ to it. None of them fit f0/Qtc/fb to a near-field capture. The professional systems (Neumann, Genelec) ship an individually-calibrated measurement mic; the consumer systems either accept a phone mic with per-model calibration curves (Sonos) or offer a cheap optional calibrated mic (SVS).

6. **Absolute-sensitivity calibration cancels in shape-relative fits; LF frequency-response calibration does not.** A flat gain offset is absorbed by the fit's level scalar. A sloped LF error (phone-mic roll-off, or a mic's model-typical LF droop) tilts the curve and biases the shape parameters.

## Details

### 1. The Keele near-field method and its validity limits

D.B. Keele Jr.'s paper, "Low-Frequency Loudspeaker Assessment by Nearfield Sound-Pressure Measurement" (JAES Vol. 22, Issue 3, pp. 154–162, April 1974; AES E-Library elib=2774; an AES Publication Award winner), is the foundation. Its core result: for a rigid circular piston in an infinite baffle at low frequency (ka < 1), the near-field pressure at the cone center is directly proportional to the far-field pressure, independent of frequency and independent of the acoustic environment: P_N = (2r/a)·P_F. This is what makes non-anechoic LF measurement possible.

**Upper frequency limit.** Keele shows the NF pressure is constant up to a/λ = 0.26 (ka = 1.6), where it is down 1 dB; nulls appear when the piston radius equals a wavelength. The practical "soft" ceiling is ka ≈ 1. Klippel AN38 states it as f_NF,max = c/(2πa) = 5475/a[cm] Hz. Audioholics and Audio Judgement give the equivalent f_max = 10950/D (D = effective diameter in cm), e.g. 1043 Hz for D = 10.5 cm. Note the various forms use ka = 1 vs ka = 1.6 vs ka = ½ — the literature is explicitly loose here ("it can only be defined loosely," Audioholics).

**Mic-to-cone distance.** Keele's Fig. 4 shows the mic must be within 0.11·a of the cone center for <1 dB error (Klippel AN38 and audioXpress both cite the 0.11× effective radius rule). For a 6.5" driver (~2.5" effective radius), that is within ~0.275" of the dust cap. Claudio Negro cites a stricter 0.055·D. This is a hard practical constraint for a phone held by hand — distance error is a dominant, systematic, low-frequency-flattening error source.

**Mic position across the cone.** Keele took the NF sample where pressure is maximum — near the cone apex/dust dome. His Fig. 5 (after McLachlan) shows the surface pressure varies only gradually with radial position for ka < 1 (a few dB from center to edge at ka = 2, less at ka = 0.5), so modest positioning error over the dust cap is tolerable at LF, but placement over the surround or near the cone edge introduces error and, at higher frequencies, non-pistonic/breakup effects where "pressure waves from various areas of the diaphragm may arrive at the microphone out-of-phase" (audioXpress).

**Diffraction/baffle.** Keele assumes an infinite baffle (2π). Real narrow-baffle speakers become omnidirectional at LF, so the true far-field is not the NF-implied 2π response; the difference is the baffle step. Jeff Bagby's white paper ("Accurate In-Room Frequency Response to 10 Hz") and the ProSoundWeb/Bagby method add a simulated diffraction curve to the NF measurement before splicing; without it the spliced curve is "significantly off at low frequency." For a small 3D-printed box the baffle step sits well above the LF alignment band, so it mostly matters if the fit band creeps upward.

**Cone breakup / non-pistonic motion.** Above the piston band the NF pressure distribution becomes complex (Zemanek's analysis, cited by Keele); NF magnitude no longer maps to far-field. Irrelevant for f0/Qtc/fb fitting below ~200 Hz but relevant as an upper bound on the fit band.

### 2. Port + cone summation, and passive radiators

**The scaling law.** Klippel AN38 gives the summation explicitly: H_NF(f) = Σ (S_i/S_1)·H_i(f), i.e. each radiator's NF response is weighted by the ratio of effective radiating areas (a *pressure* weighting that goes as area, because each NF magnitude already reflects 1/area; when combining as equivalent volume velocities the pressure contributions scale by area ratio). The common DIY formulation (Jeff Bagby, VituixCAD, diyAudio) scales the *smaller* radiator down by 20·log10(√(S_port/S_driver)) = 20·log10(d_port/d_driver) in dB — e.g. a 2" port with a 5" cone: 20·log10(2/5) = −7.96 dB. Keele's own worked 15" example: a_D/a_v = √(S_D/S_v) = √(133/75) = 1.33 → +2.5 dB in favor of the diaphragm.

**Errors from wrong weighting.** Getting the area ratio wrong directly mis-levels the port relative to the cone, which changes the depth of the summed notch and the apparent alignment. VituixCAD/Bagby guidance stresses aligning the roll-off *slopes* (informed by a box sim) rather than the leveled region, because "the port and woofer roll off don't always align in level" (parts-express/Bagby). For a *flared* port the effective area is the mouth area, which is ill-defined (diyAudio).

**Phase/polarity with magnitude-only data.** Keele is explicit: "the near-field pressures are complex quantities… a simple pressure magnitude measurement is not enough; you need a system that measures both magnitude and phase to sum the responses correctly." Port and cone are roughly in phase above fb and out of phase below fb (for high box Q); at fb system output is predominantly the port. **This is the crux for a magnitude-only, single-capture flow: you cannot correctly sum port+cone without phase.** The defensible workaround is to NOT sum, and instead extract fb from the *frequency of the null* in the driver-only NF magnitude (Keele's method; D'Appolito confirms this).

**Port artifacts.** Vent NF measurements are corrupted by diaphragm crosstalk above ~1.6·fb (Keele). Ports also have quarter-wave organ-pipe resonances at f = (2n+1)·c/4L that appear as spikes in the port NF response (often several hundred Hz), well above the alignment band but a source of residual if the fit band is too wide.

**Passive radiators.** PRs differ from ports in three ways relevant here: (a) the PR has a large membrane area (often ≥ the driver Sd), so its area weighting is near unity, not a large attenuation; (b) the PR adds its own mass-compliance resonance and an additional null/notch below the system tuning (the PR "free-air" resonance appears as a null in the driver NF response, analogous to fb); (c) PR NF measurement is contaminated when driver and PR are physically close (side-by-side), so the NF capture is "not clean" (diyAudio). PR effective area and mass are only approximately known, adding parameter uncertainty. Practitioners measure PR and driver separately with the other covered by a pillow (VituixCAD practice).

### 3. Robustness of magnitude-only, no-phase, no-impedance parameter extraction

**Sealed box (2nd-order HP): well-conditioned.** The transfer function |H|² = x⁴/[(1−x²)² + x²/Qtc²] with x = f/f0 has exactly two shape parameters plus a level scalar. Two robust, near-orthogonal features identify them: the −3 dB knee frequency sets f0, and the amount of peaking/droop at the knee sets Qtc. A key exact identity (derived from the transfer function): the level at resonance relative to the passband asymptote is exactly L(f0) = 20·log10(Qtc) dB — so Qtc = 0.707 → −3.0 dB, Qtc = 1.0 → 0 dB, Qtc = 2.0 → +6 dB. This is the same fact the miniDSP/Linkwitz-Transform workflow relies on: the LT needs only f0 and Qtc, both obtainable "by performing a nearfield measurement of the woofer" (miniDSP LT app note).

**Ported box (4th-order): poorly conditioned in magnitude-only.** The 4th-order alignment has fb, box Q / leakage Ql, plus driver Fs, Qts and box volume. In magnitude-only data these trade off: fb vs Ql vs leakage are partially degenerate (the notch depth is set by total losses, so you cannot separate box leakage from driver mechanical loss without impedance or phase). The *frequency* of the cone null gives fb robustly; the *depth* gives only lumped Ql. The traditional, more accurate route is impedance-based (the double-peak impedance saddle).

**Impedance-based vs response-based accuracy.** The added-mass and delta-compliance (sealed-box) impedance methods are the reference standard (REW, Dayton DATS, ARTA LIMP, Listen SoundCheck, Klippel LPM). Klippel's LPM manual states the between-methods agreement directly: "you should obtain a deviation of the Bl and Mms parameters of maximum 5%. By optimizing the measurement, a typical deviation of 2% to 3% is standard and should be aimed." Klippel's SNR→accuracy table gives 20 dB SNR+D → 10% error, 30 dB → 3%, 40 dB → 1%. Crucially, Klippel AN25 ("Maximizing LPM Accuracy") also warns that the driver itself moves: "Cms and fs may easily vary by 10-20% with (small signal) excitation level [1]. Furthermore Cms and fs can change by 50% (!) if the temperature is increased from normal ambient temperature to 40 °C [2]" (citing Klippel & Seidel, AES 110th, 2001; Hutt, AES 112th, 2002) — so the *physical* driver variability often exceeds the *instrument* repeatability. Manufacturer QC commonly uses a 10% Fs tolerance (SB Acoustics specifies ±2% Re; delta-mass method). The Candy & Futtrup dual-added-mass method (JAES Vol. 65 No. 12, pp. 1005–1016, Dec 2017, doi:10.17743/jaes.2017.0040), implemented in REW and speakerbench, is described by REW as giving "the most accurate results" by over-determining the fit with multiple masses.

**fb: impedance saddle vs NF null.** The impedance saddle (minimum between the two peaks) locates fb to ~±1 Hz / 1–2% but can be mildly pulled by leakage and voice-coil inductance. The driver NF null is theoretically cleaner — D'Appolito (Audiomatica CLIO app note, "Measuring Loudspeaker Low-Frequency Response") states it "is more accurate than one obtained from the impedance curve since it is not corrupted by voice coil inductance" — BUT it is vulnerable to woofer↔port crosstalk when the radiators are close: he documents a case (10 cm woofer-port spacing) where the woofer NF response indicated an fB of 45.9 Hz while the impedance saddle gave 54.8 Hz, an ~8.9 Hz (~16%) error, concluding "a large difference between the two is a good indication of crosstalk in the near-field data. In this case the impedance minimum is a better estimate of fB."

**Room/boundary contamination even in near field.** NF strongly suppresses room reflections (high direct-to-reflected ratio) but does not eliminate the LF room *gain*/boundary loading, which raises the very bottom of the band and can masquerade as lower f0 / higher Qtc. Placing the woofer near a floor/wall to enforce half-space (VituixCAD practice) stabilizes the LF asymptote.

### 4. How commercial auto-setup systems capture low frequencies

None of the commercial systems fit a physical box model; all apply corrective EQ to a measured curve. Summary:

- **SVS Auto EQ**: announced June 1, 2026 and went live around June 8, 2026 as a free over-the-air update to the SVS Subwoofer Control app for the 3000/5000/17-Ultra R|Evolution subs, enabled by "a 295 MHz Analog Devices DSP" (SVS press release via The Absolute Sound). It performs a multi-position, *listening-position* in-room capture and applies DSP EQ inside the sub. It accepts the phone's built-in mic OR an optional calibrated "SVS Auto EQ Mic" that SVS sells for $45 "for greater measurement accuracy"; only the SVS mic or the phone mic are permitted. Purely corrective EQ; no box model.
- **KEF** (KEF Connect app for LS60/LS50 W II/KC62): offers coarse DSP presets (bass extension Standard/Less/Extra, wall/desk modes, treble trim) rather than a mic-based room measurement. KEF's position (per a KEF rep on ASR) is that "for the average user the settings in the app are enough." No measurement mic; no fitting. Third parties (Wiim, REW, EQ APO) are used for actual room correction.
- **Neumann MA 1** (KH-line monitors + KH 750 sub): ships an *individually calibrated* Class-1 MA 1 measurement mic; guided multi-position capture; algorithm co-developed with Fraunhofer IIS; applies amplitude AND phase correction stored in the monitors' DSP; "ensures an optimal summation in the low frequencies when a subwoofer is used." 48 kHz required. Corrective EQ/room-mode compensation, not box fitting.
- **Genelec GLM** (SAM monitors + 8300A/factory-calibrated reference mic): AutoCal/AutoCal 2 with SinglePoint or MultiPoint options; the mic's individual frequency response is retrieved from the cloud by serial number; corrects level, distance/delay, sub crossover phase, and LF/low-mid room response. Corrective EQ, not box fitting.
- **Dirac Live**: requires multiple mic positions for good results; accepts a calibrated UMIK-1 (calibration file supported). Corrective EQ + (in ART/DLBC) bass management. No box model.
- **Audyssey**: bundled (non-user-calibrated) mic; can work single-position with low furniture but multi-position recommended. Corrective EQ.
- **Anthem ARC Genesis**: supplied calibration mic (each mic has a cal file); multi-position; corrects to a target curve. Corrective EQ.
- **Trinnov Optimizer**: proprietary 3D/4-capsule mic; multi-position; corrects frequency and time domain. Corrective EQ.
- **Sonos Trueplay**: two variants. Manual Trueplay is *iOS-only* precisely because Sonos maintains a per-iOS-model microphone calibration curve — the Sonos tech blog states "We measure every new iOS device and create a Trueplay calibration curve for it… It's not presently feasible to do this for the enormous range of non-iOS phones, which vary too much from device to device, sometimes even depending on the carrier they're connected to." Quick Tune / Auto Trueplay use the speaker's *own* built-in mics. A moving-mic room sweep, corrective EQ only.
- **Apple HomePod**: uses built-in mics for automatic room sensing/"Adaptive EQ"; no external mic, corrective EQ.

**The takeaway for this flow:** every serious LF-capable commercial system either (a) ships an individually-calibrated mic (Neumann, Genelec, Anthem, Trinnov) or (b) maintains per-device mic calibration curves for the phones it accepts (Sonos), or (c) offers a cheap calibrated mic as the accuracy path (SVS). None trusts an *uncalibrated* phone mic for accurate LF work. This is strong convergent evidence for requiring calibration when a phone mic is used.

### 5. Microphone calibration: when it matters vs cancels

**(a) Sensitivity / absolute level — cancels.** A shape-relative fit includes a free level scalar; any frequency-independent gain error is absorbed entirely and does not bias f0, Qtc, or fb. So absolute SPL calibration is correctly optional for this flow.

**(b) LF frequency-response calibration — does NOT cancel.** A sloped LF error tilts the curve and biases shape. Measurement-mic LF behavior:
- **UMIK-1**: spec'd 20 Hz–20 kHz ±1 dB with calibration loaded; the individual cal file extends to 10 Hz. Unit-to-unit LF differences are small but real — one user's comparison of two UMIK-1s found the miniDSP files agreed to ~2 dB and the tighter Cross-Spectrum Labs files to ~0.5 dB vs an Earthworks reference. miniDSP acknowledges "it's hard to make very accurate calibration at very low frequency." Below ~15 Hz cal differences of 1–2 dB are reported.
- **Dayton EMM-6 / UMM-6 / iMM-6**: individually hand-calibrated against a B&K reference; cal file included; regarded as effectively flat once cal is loaded.
- **Behringer ECM8000**: no cal file shipped; spec'd 15 Hz–20 kHz (elsewhere 20 Hz–20 kHz); "recent Behringers are very poor" per some practitioners; needs phantom power + interface.
- General practitioner view: for sub work even an Audyssey/phone mic "should get the job done under 100 Hz," but "for full-range measurements you want the most accurate mic you can afford."

**(c) Smartphone mics — the problem case.** MEMS phone mics are flat-ish 100 Hz–4 kHz but roll off steeply below ~60–100 Hz, and historically had a hard high-pass in the audio path (pre-iOS 6 devices; Apple removed the built-in low-cut in iOS 6). Studio Six Digital / dsp mobile document that iOS 5 devices had a "heavy low cut filter" requiring per-device compensation. WiiM's testing (using a PCB model 378B02 half-inch reference microphone) found the iPhone mic "not too bad between 100 Hz and 4 kHz" but that "Below 60 Hz the iPhone mic is not really usable for room correction as it is," and recommends limiting phone-mic correction "to the frequency range of min. 60 Hz to max. 700 Hz." Additional un-calibratable hazards: AGC/auto-gain, noise suppression, wind/voice-processing DSP, and variable high-pass that changes with OS/app state — these are nonlinear/adaptive and cannot be removed by a static calibration curve. NIOSH smartphone SLM validation work (Kardous & Shaw, evaluation of sound-measurement apps) found iOS apps could be brought within IEC 61672 class-2 accuracy *only with an external calibrated mic* (e.g. MicW i436), not with the built-in mic.

**Quantified error propagation (the money question).** Using the sealed-box |H|² above, the level at the knee relative to the passband is exactly 20·log10(Qtc). Therefore a measurement magnitude error of Δ dB localized at the knee maps directly onto fitted Qtc:

  Qtc_fit / Qtc_true = 10^(−Δ/20)

- Δ = 1 dB droop → 0.891 → Qtc fitted ~11% low
- Δ = 2 dB → 0.794 → ~21% low
- Δ = 3 dB → 0.708 → ~29% low

Differentially, d(Qtc)/Qtc ≈ 0.115 per dB → **rule of thumb: ~10–12% Qtc error per dB of tilt at the knee**, in the direction that a downward roll-off makes the box look more damped (lower Qtc). f0 is far more robust because it is set by a *frequency* feature: at Qtc = 0.707 the local slope at the knee is ~6 dB/octave, so a naive single-point −3 dB search shifts f0 by ~Δ/6 octave (~12%/dB worst case), but a full least-squares fit over the whole curve constrains f0 much more tightly — **estimated f0 bias ~1–5% per dB of localized tilt, typically one-third to one-half of the fractional Qtc error.** (Note: no *published* analytic sensitivity coefficient exists for HP-parameter fitting to SPL data; the Qtc identity is exact, the f0 figure is a reasoned engineering estimate.)

For **ported fb**: a mic LF tilt shifts the *level* of the cone response but the null *frequency* (which sets fb) is comparatively insensitive to a smooth tilt — fb error from mic LF roll-off is small (order 1–2%) compared with the crosstalk and no-phase-summation errors, which dominate (up to ~16% per D'Appolito's crosstalk example). So mic calibration helps sealed-Qtc the most, sealed-f0 modestly, and fb least.

### 6. Typical error bars

- **NF magnitude vs anechoic/ground-plane**: <1 dB within the piston band and mic-distance constraint (Keele; Klippel AN38), rising once diffraction/baffle-step and the ka≈1 ceiling are approached. Keele's own experimental comparison showed "good agreement below 500 Hz" across five methods for a small closed box.
- **Impedance-based T/S**: 2–3% typical / 5% max method-to-method deviation for Bl and Mms with good SNR (Klippel LPM); degrades to ~10% at 20 dB SNR+D. Physical driver variability (level, temperature, break-in) adds 10–20% on fs/Cms — often the dominant term.
- **fb from impedance saddle**: ~±1 Hz / 1–2%. **fb from NF null**: comparable when radiators are well separated, but up to ~16% error under crosstalk.
- **Test-retest repeatability**: instrument repeatability for good impedance setups is a few percent; NF-magnitude fitting repeatability is dominated by mic repositioning (distance/centering) between captures — for a single fixed capture this is removed, but a phone held by hand across captures reintroduces it.

## Recommendations

**A. Expected error bounds for a single fixed-position NF magnitude capture** (assumptions: mic within 0.11·a of dust cap; fit band from ~0.3·f0 to ~3·f0 but kept below the baffle-step/ka limit; SNR > 20 dB across the fit band; woofer placed to approximate half-space). These are engineering estimates combining the published NF/impedance error bars with the derived mic-tilt propagation; treat percentages as ~1σ:

| Box type | Calibrated USB mic (UMIK/EMM-6 + cal) | Uncalibrated USB measurement mic (ECM8000, no cal) | Smartphone mic (no cal) |
|---|---|---|---|
| **Sealed f0** | ±3–5% | ±4–7% | ±5–10% |
| **Sealed Qtc** | ±10–15% | ±15–25% | ±25–40% |
| **Ported fb** | ±2–4% (from cone null) | ±3–6% | ±4–8% |
| **Ported Qb/Ql** | ±30–50% (magnitude-only, degenerate) | ±40–60% | poorly determined |
| **PR system tuning** | ±4–8% | ±6–10% | ±8–15% |
| **PR mass/area-dependent params** | ±20–40% (area/mass only approximate) | worse | not recommended |

Interpretation: for **sealed boxes**, even a phone mic gives usable f0, but Qtc from a phone mic can be off by enough (25–40%) to noticeably mis-set the Linkwitz-Transform pole Q — audible as a bass hump or over-damping. For **ported/PR**, fb (the LT-relevant tuning) is recoverable from the null, but the loss parameters are weak in magnitude-only data regardless of mic. Validate the table empirically against a driver independently characterized by Klippel/DATS before shipping.

**B. Should calibration be REQUIRED or RECOMMENDED?** Staged recommendation:

1. **Require a calibration file for smartphone mics, unconditionally.** The LF roll-off, adaptive DSP, and per-device variation make uncalibrated phone captures unfit for Qtc/Qb even though f0/fb may survive. If no per-device calibration is available, restrict the phone-mic fit to f0/fb only and refuse to report Qtc/Qb (mirror Sonos's per-iOS-model calibration philosophy and WiiM's 60 Hz floor).
2. **Recommend (not require) calibration for known-model USB measurement mics** (UMIK-1/2, EMM-6, UMM-6): their model-typical LF response is close enough that an *uncalibrated* fit of f0 is fine and Qtc is within ~15–25%; loading the individual cal file tightens Qtc to ~10–15%. Make cal-file loading a one-click default keyed to the mic serial number.
3. **Middle path — conditional escalation:** require a calibration file *only when the fitted Qtc lands outside a plausible range* (e.g. Qtc < 0.45 or > 1.2 for a sealed box, or an implausibly deep/shallow ported null), since those are exactly the cases where an uncalibrated LF tilt is the likely culprit. This gives a low-friction default with a guardrail.
4. **Differ by box type:** calibration matters most for **sealed Qtc**; for **ported/PR** the dominant errors are the no-phase summation and crosstalk, so calibration is less decisive there — but still require it for phones because the whole capture is suspect.

Benchmarks that would change these: if you add an impedance measurement (even a cheap DATS-style sweep), fb/Ql become well-conditioned and the calibration requirement can relax to sealed-Qtc only. If you add a second mic position or a phase-coherent two-channel capture, port+cone summation becomes valid and PR/ported accuracy improves to near the sealed-box level.

**C. Fit-residual thresholds and diagnostic signatures.** Fit over the alignment band (roughly 0.3·f0 to the lesser of 3·f0 and the ka/baffle limit). Compute RMS and max residual (measured minus model, in dB):

- **Green (accept):** RMS ≤ ~1.0 dB and max ≤ ~2.5–3 dB over the band.
- **Yellow (warn):** RMS ~1–2 dB or max ~3–5 dB — report parameters with widened error bars and surface the residual plot.
- **Red (refuse / re-capture):** RMS > ~2 dB or max > ~5 dB, or any diagnostic signature below.

Residual-shape → cause map (the shape vs frequency is the diagnostic):

- **Mic too far from cone:** broadband gentle LF *droop* / flattened knee (the 0.11·a violation flattens the NF pressure); residual is a smooth downward tilt increasing toward LF. Distinguish from mic LF roll-off by its dependence on capture geometry (re-capture closer fixes it).
- **Mic moved mid-capture:** discontinuity / level step or comb-like irregularity in the residual not explained by any 2nd/4th-order model; time-varying SNR. Refuse.
- **Room/boundary contamination:** narrow peaks/dips (room modes) superimposed on an otherwise good fit, typically 20–120 Hz, and a raised LF asymptote (room gain) that pulls fitted f0 down / Qtc up. Signature: isolated ripples the model cannot fit.
- **Port not measured / mis-weighted (ported box fit):** the model cannot reproduce the notch depth or the sub-fb slope; residual shows a systematic error clustered around fb and below (24 dB/oct model vs measured). If the cone null is present but the level below fb is wrong, suspect area-weight/summation error.
- **Clipping / compression:** flattened peak, harmonic artifacts, level-dependent residual that grows with drive level; the knee peak is suppressed (looks like low Qtc) but re-capturing at lower level changes the answer. Signature: residual improves at lower stimulus level.
- **Insufficient SNR at band bottom:** rising, noisy residual toward the lowest frequencies; parameter estimates unstable across repeat fits. Restrict the fit band upward or raise stimulus level.
- **Wrong box-type model selected:** large, *structured* residual with the wrong asymptotic slope — a 2nd-order (12 dB/oct) model fitted to a ported (24 dB/oct) roll-off leaves a characteristic steepening residual below f0; a 4th-order model fitted to a sealed box leaves an unfittable phantom notch. Auto-select box type by testing both and comparing residual + asymptotic slope.

Implement the band-limited fit, report both RMS and max residual, and gate on both. Prefer extracting fb from the cone null frequency (not port+cone magnitude summation) and cross-check: if a summation is attempted and disagrees with the null by more than ~5–10%, flag crosstalk (per D'Appolito) and trust the null/impedance.

## Caveats

- **Where the literature is thin / numbers are engineering judgment:** There is no published closed-form sensitivity analysis for least-squares fitting of a 2nd-order high-pass (or 4th-order ported alignment) to *SPL magnitude* data. The Qtc-vs-dB identity (20·log10(Qtc) at the knee) is exact and the ~10–12%/dB rule follows rigorously; the f0 (~1–5%/dB) and the box-type error-bar table in Recommendation A are reasoned extrapolations combining the exact identity with published NF and impedance error bars — they are not, to my knowledge, directly measured in any single publication and should be validated empirically against a known reference (e.g. a driver characterized by Klippel/DATS) as part of commissioning the flow.
- The Candy & Futtrup dual-added-mass paper's specific test-retest repeatability percentage is not quotable from open sources (paywalled); its documented contribution is reducing systematic mass-coupling error by over-determining the fit, not a stated repeatability figure.
- Commercial-system internals are described from vendor documentation and reputable press; none publish an accuracy spec for *parameter* extraction because none extract parameters — they EQ a measured curve. Their mic-calibration practices, however, are well documented and are the most relevant transferable evidence for the calibration-requirement decision.
- fs/Qts physical variability (10–20% with level, up to 50% with a 40 °C temperature rise, per Klippel AN25 citing Klippel & Seidel 2001 and Hutt 2002) means the "true" target itself moves; a single capture at one drive level captures one operating point, and the LT alignment should be validated at the intended playback level.
- All numeric error bars assume the mic-distance and half-space constraints are met; a hand-held phone violating the 0.11·a rule will exceed these bounds regardless of calibration.
- Dates and product details reflect information as of July 24, 2026 (e.g. SVS Auto EQ launched June 2026); firmware/app behavior and shipped calibration practices may change.
