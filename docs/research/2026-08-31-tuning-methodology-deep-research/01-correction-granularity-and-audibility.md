# Deep research 1 — correction granularity, filter budgets, and audibility

> Owner-run deep research, received 2026-08-31, answering Wave 6 ticket
> 6.9's first assignment (see `docs/tuning-master-plan.md`). Banked
> verbatim below the rule; synthesis and any re-adjudication ADRs live
> outside this file. Frozen: no further edits.

---

Automated Loudspeaker EQ: What the Literature Actually Establishes vs. Community Habit

TL;DR

* The "under 5 filters per driver / ±3 dB" rule has no basis in the perceptual literature; it is a live-sound rule of thumb. The published science (Toole & Olive 1988; Bücklein 1981; Olive et al. 1997; Moore & Tan 2003) shows audibility thresholds depend on Q, level, program material, and peak-vs-dip — not filter count. Broad, low-Q resonances are audible at ~0.25–1 dB, so a smooth on-axis target well under ±1 dB is a legitimate goal, not overkill.
* 24 moderate-Q minimum-phase biquads reaching ~0.07 dB RMS is defensible engineering only if the correction is validated against off-axis/spinorama data and multiple measurements. The real risk is not filter count, phase accumulation, or DSP numerics (CamillaDSP's 64-bit double path is essentially immune) — it is overfitting a single gated measurement and correcting non-minimum-phase or direction-dependent features that should not be EQ'd.
* Serious commercial systems (Genelec, Neumann, Dirac, Trinnov, Dutch & Dutch) do NOT limit themselves to a handful of filters, but they DO limit where they correct — heavy correction below the Schroeder frequency, gentle broadband shelving above it, spatial averaging, and explicit refusal to fill narrow dips. That discipline, not a filter budget, is the defensible principle.

Key Findings

1. Resonance audibility is governed by Q, not filter count. Toole & Olive found high-Q resonances need to be ~10 dB above the mean to be heard, while low-Q resonances are audible at 1–2 dB; per Audioholics' summary of the 1988 paper, "a 5 kHz resonance, with Q = 1 was just detectible at .25 dB" using pink noise. Pink noise is the most revealing signal; multi-mic pop/rock is the least; per the same summary, "when using the least revealing of these [signals] that just detectible threshold increased by a factor of 5."
2. Peaks are far more audible than dips (Bücklein 1981): peaks are detected when equal-magnitude dips are inaudible. This directly justifies asymmetric EQ strategy (cut peaks aggressively, do not fill nulls).
3. The room does not mask resonances — Toole & Olive found resonances are more detectable in reflective/echoic conditions than anechoic; as a 2022 Frontiers in Psychology paper summarizes, "Toole and Olive (1988) observed a better detectability of signal resonances in reverberant rooms compared to anechoic conditions."
4. A published, peer-reviewed perceptually-weighted deviation metric exists: Olive's preference model (2004) with NBD (narrow-band deviation), AAD, SM (smoothness), SL (slope), LFX/LFQ terms and published regression coefficients. This is the strongest published alternative to a flat ±dB tolerance.
5. Phase/group-delay accumulation from minimum-phase biquads is not an audibility problem at these gains. Blauert & Laws thresholds run 1–3.2 ms; Liski/Mäkivirta/Välimäki found <1.0 ms group delay from 300 Hz–1 kHz is inaudible. A few-dB moderate-Q peaking biquad produces well under a millisecond of group delay, and minimum-phase filters' phase is fully determined by (and co-located with) the magnitude correction — there is no independent "accumulated phase" penalty.
6. "Filter count" as a DSP numerical problem is folklore in CamillaDSP's case. CamillaDSP uses 64-bit double-precision biquads; numerical noise in a 24-biquad cascade is negligible. The real quantization/noise concern applies to 32-bit float cascades with extreme low-frequency high-Q filters — not this case.

Details

1. Resonance audibility — the actual numbers

Toole & Olive, "The Modification of Timbre by Resonances: Perception and Measurement," JAES Vol. 36, No. 3, pp. 122–142 (March 1988); originally AES Convention 83 preprint 2487 (Oct 1987); preceded by "The Perception of Sound Coloration Due to Resonances in Loudspeakers and Other Audio Components," AES 81st Convention preprint 2406 (Nov 1986).

Key findings, reported numerically as far as the literature permits:

* Q dependence: high-Q (narrow) resonances must be roughly 10 dB above the mean level to be detected; very broad (low-Q) resonances are audible at only 1–2 dB. Per Audioholics' technical summary citing the paper: "a 5 kHz resonance, with Q = 1 was just detectible at .25 dB" using pink noise (the most revealing signal). This inverse relationship — low-Q resonances audible at much smaller amplitudes because the ear integrates energy over a broad band — is counterintuitive but central.
* Program material: pink noise most revealing; densely orchestrated, reverberant (e.g., symphonic) music fairly revealing; multi-mic close-recorded rock/pop poorest. Per the Audioholics summary, "when using the least revealing of these [signals] that just detectible threshold increased by a factor of 5" relative to pink noise.
* Anechoic/headphone vs. reverberant room: resonances were more detectable in echoic/reverberant conditions than anechoic — "the room does not mask resonances." A 2022 Frontiers in Psychology study (Fleßner/Biberger et al.) restates it: "Toole and Olive (1988) observed a better detectability of signal resonances in reverberant rooms compared to anechoic conditions." Toole's own summary: audibility is strongly dependent on Q, slightly on frequency, and strongly on program.
* Peaks vs. dips: covered most directly by Bücklein, "The Audibility of Frequency Response Irregularities," JAES Vol. 29, No. 3, pp. 126–131 (March 1981) (translation of 1962 work): "Peaks in the frequency response are far more audible than equivalent valleys or dips. Peaks are detected even when valleys of the same size are not perceptible." Bücklein used music, speech, and white noise.

Toole's book treatment: Sound Reproduction: The Acoustics and Psychoacoustics of Loudspeakers and Rooms (3rd ed., Routledge/Focal 2017) consolidates these into the guidance that resonances (minimum-phase phenomena) are the audible enemy and are the legitimate target of EQ, whereas broad tilts are matters of preference.

Successor/low-frequency work: Olive, Schuck, Ryan, Sally & Bonneville, "The Detection Thresholds of Resonances at Low Frequencies," JAES Vol. 45, No. 3, pp. 116–128 (March 1997) measured 70.7% detection thresholds (UDTR adaptive procedure) for single added peaks and notches across Q and center frequency, using pink noise and pulses through earphones. Conclusion: thresholds depend "in complicated ways" on Q, center frequency, and signal type — hard to predict from a magnitude measurement alone.

Room-mode Q thresholds: Fazenda, Avis et al., "Thresholds of detection for changes to the Q-factor of low frequency modes" (AES/Proc. IoA, 2007) suggest a modal Q ≈ 16 threshold below which further Q changes are undetectable; Karjalainen/Mäkivirta et al. found modal decay T60 at 100 Hz can rise from ~0.3 s by 0.1–0.4 s before audible, and at 50 Hz decays up to ~2 s are inaudible.

Linear-distortion / spectral-ripple audibility: Moore & Tan, "Perceived Naturalness of Spectrally Distorted Speech and Music," J. Acoust. Soc. Am. Vol. 114, No. 1, pp. 408–419 (July 2003), DOI 10.1121/1.1577552 rated 168 filtered conditions (tilts, ripples of varying density/depth, bandwidth limits). Ripples of 5 dB depth degraded naturalness progressively with ripple density; naturalness fell as bandwidth narrowed (increasing the low cutoff from 55 Hz clearly degraded music; decreasing the upper cutoff from ~16.8 kHz progressively degraded it). This dataset underlies Moore & Tan's model of perceived quality for linear distortion (the "D" measure — standard deviation of spectrally-weighted excitation-pattern differences plus the SD of their slopes), replicated/extended by Biberger et al. (2018). This is a published, perceptually-weighted linear-distortion metric.

The strongest published basis for an audibility-weighted (not flat-±dB) error metric

Olive, "A Multiple Regression Model for Predicting Loudspeaker Preference Using Objective Measurements," Part I (Listening Test Results), AES 116th Convention, preprint 6113 (2004); Part II (Development of the Model), AES 117th Convention, preprint 6190 (2004); embodied in US Patent 8,311,232 B2 (and application US 2005/0195982 A1), "Method for predicting loudspeaker preference."

The model computes preference from weighted deviation metrics measured on spinorama curves, smoothed in 1/20-octave bands:

* NBD (Narrow Band Deviation): mean absolute deviation of each 1/20-octave band from a broader running mean — a direct "how bumpy" measure applied to on-axis (ON) and predicted-in-room (PIR) curves.
* AAD (Absolute Average Deviation).
* SM (Smoothness): the r² goodness-of-fit of a regression line to the curve.
* SL (Slope), LFX (low-frequency extension), LFQ (low-frequency quality).

The published 4-variable model, verbatim from US Patent 8,311,232 B2, Eq. (9): Pref. Rating = 12.69 − 2.49·NBD_ON − 2.99·NBD_PIR − 4.31·LFX + 2.32·SM_PIR, with all metrics "smoothed in 1/20 octave bands." In the model, NBD and SM of the predicted in-room response contribute ~38% combined and low-frequency extension ~30.5%. This is the single strongest published, replicated basis for weighting narrow-band deviation and smoothness rather than applying a flat ±dB tolerance — and it explicitly rewards flat, smooth response.

Verdict on the community rule: "under 5 filters per driver" appears nowhere in this literature. "Within ±3 dB" is looser than what Toole/Olive/Bücklein show is audible for low-Q features (audible at ~1 dB or less). The literature supports a Q-weighted, peak-prioritized, smoothness-oriented target — not a filter budget.

2. Accumulated phase / group delay, and DSP-numerics folklore

Published group-delay audibility thresholds:

* Blauert & Laws, "Group Delay Distortions in Electroacoustical Systems," J. Acoust. Soc. Am. Vol. 63, No. 5, pp. 1478–1483 (May 1978): thresholds (allpass, headphones), verbatim table — 500 Hz: 3.2 ms; 1 kHz: 2 ms; 2 kHz: 1 ms; 4 kHz: 1.5 ms; 8 kHz: 2 ms (nothing tested below 500 Hz). A parallel reporting of the same data gives 3.2 / 2.1 / 0.9 / 1.3 / 1.9 ms respectively; the shape (most sensitive ~1–2 kHz, ~1 ms) is consistent, and training lowers these values.
* Lipshitz, Pocock & Vanderkooy, "On the Audibility of Midrange Phase Distortion in Audio Systems," JAES Vol. 30, No. 9, pp. 580–595 (Sept 1982): using 1st/2nd-order allpass networks (f₀ ~100 Hz–3 kHz, Q 0.5–2). Conclusions verbatim: "(1) Even quite small midrange phase nonlinearities can be audible on suitably chosen signals. (2) Audibility is far greater on headphones than on loudspeakers... (4) On normal music or speech signals phase distortion appears not to be generally audible." Crucially: "none of these experiments... has indicated a present requirement for phase linearity in loudspeakers for the reproduction of music and speech."
* Flanagan, Moore & Stone, "Discrimination of Group Delay in Clicklike Signals...," JAES Vol. 53, No. 7/8, pp. 593–611 (2005).
* Møller et al.; Deer et al. (~2 ms at 2 kHz); Hansen & Madsen, "On Aural Phase Detection," JAES 22 (1974); Preis, "Phase Distortion and Phase Equalization — A Tutorial Review," JAES Vol. 30, No. 11, pp. 774–794 (1982); Suzuki, Morita & Shindo (1980) — gradual phase changes over a wide band produce no discernible effects.
* Liski, Mäkivirta & Välimäki, "Audibility of Loudspeaker Group-Delay Characteristics," AES 144th Convention paper 10008 (2018) and "Audibility of Group-Delay Equalization," IEEE/ACM Trans. Audio, Speech, Lang. Process. Vol. 29, pp. 2189–2201 (2021): headphone ABX. Key result, verbatim from the 2018 abstract: "when the group delay in the frequency range from 300 Hz to 1 kHz is below 1.0 ms, it is inaudible. With low-frequency emphasis, the group delay variations can be heard more easily." The 2021 study found the unit impulse and "pink impulse" the most critical signals, with the smallest mean thresholds of −0.56 ms (negative GD) and 0.64 ms (positive GD), obtained with a pink impulse.

What ~24 minimum-phase biquads actually do: A moderate-Q (say Q≈1–3) peaking filter of a few dB produces a group-delay bump on the order of tenths of a millisecond, localized near its center frequency. Because these are minimum-phase filters, their phase response is uniquely determined by their magnitude response — the phase shift is exactly what is required to produce the magnitude correction and is co-located with it. They do not "stack up" into a large broadband delay the way a cascade of allpasses would; group delays at different center frequencies do not simply sum across the whole band. The aggregate group delay from 24 small-gain corrective biquads stays far below the Blauert–Laws (1–3.2 ms) and Liski (~0.56–1 ms) thresholds. There is no published evidence that a large number of low-gain minimum-phase corrective filters degrades sound independent of what they correct. (The audibility that does exist attaches to non-minimum-phase / linear-phase FIR correction, which introduces pre-ringing — Mäkivirta, Liski & Välimäki, "Modeling and Delay-Equalizing Loudspeaker Responses," JAES Vol. 66, No. 11, pp. 922–934, Nov 2018, DOI 10.17743/jaes.2018.0053: full-band group-delay EQ causes pre-ringing, while mid/HF-only delay EQ shortens the impulse response without pre-ringing.)

Filter count as a DSP numerical problem — separating engineering from folklore: In fixed/single-precision (32-bit float) biquad cascades, quantization of coefficients and accumulation of rounding noise can be a genuine issue, especially for high-Q filters at very low frequencies (small coefficient differences), and is mitigated by higher-order sections, double precision, or restructured topologies (e.g., state-variable/transposed forms). CamillaDSP processes in 64-bit double precision, for which a 24-biquad cascade has utterly negligible numerical noise. So the real engineering issue exists but does not apply to this user's toolchain. Dirac's own white paper corroborates the general point: "If you don't have sufficient word lengths, you also get problems in maintaining the resolution in the bass region" — a word-length issue, not a filter-count-per-se issue.

3. What serious commercial/pro systems actually do

Genelec GLM / AutoCal / AutoCal2 (primary: Goldberg & Mäkivirta, room-response optimization work, AES 114th Conv. paper 5730, 2003; Mäkivirta et al. modal-EQ AES preprint 5480, 2001; Genelec white papers and support documentation):

* Room correction uses minimum-phase IIR filters (parametric notches + LF/HF shelving). Genelec's stated reason (support documentation): "We compensate the room response using minimum phase filters. This avoids adding too much latency and pre-ringing problems when applying room equalization." FIR/linear-phase is used only for the speaker's own crossover phase (Extended Phase Linearity in "The Ones," linear phase down to ~100 Hz, at a cost of "+3.7 ms more delay").
* Filter count is model-dependent: older 8200-series ~4 notch + 4 shelving; the current flagship 8380A three-way SAM main monitor specifies, verbatim from Genelec's product page, "GLM network and calibration with 16 parametric notch filters and 2 LF + 2 HF shelving filters." AutoCal2/GLM 4.1 increased filter counts and added limited positive-gain filters (typically for low-Q 100–300 Hz notches).
* Correction is concentrated at low/low-mid frequencies below the Schroeder frequency (critical frequency ~70–200 Hz in small rooms, per Goldberg & Mäkivirta 2003); above it, only broadband shelving preserves the neutral factory balance. Stated rationale (Goldberg & Mäkivirta 2003): "room response controls are not intended to correct narrow-band deviations in the loudspeaker frequency response... These should be solved acoustically rather than electronically," and "the goal of equalisation is usually not to convert the listening room to anechoic."
* Smoothing: proprietary AccuSmooth — "a narrower smoothing bandwidth at low frequencies compared with the standard 1/3 octave smoothing and similar resolution at high frequencies." Historically 1/3-octave input.
* On-axis vs. spatial average: supports both; multipoint spatial averaging weights the first mic 50% for EQ; corrects the in-room listening-area response, not a pure anechoic on-axis curve (Mäkivirta & Lund, AES 141st Conv., 2016).

Neumann MA 1 (with Fraunhofer IIS): DSP correction stored in the monitors; optimizes both amplitude and phase (including phase-linearization of the analog KH-line crossovers via the KH 750 DSP); generates a room-adaptive target curve; emphasizes modal compensation ("precise and effective compensation of room modes"). Type/count of filters not published in detail (flag: primarily marketing-level documentation).

Kii Three: six drivers, six amps, DSP-based cardioid directivity down to ~60–100 Hz; FIR/linear-phase processing (Kii/Bang & Olufsen-lineage patents describe FIR linear-phase filter coefficients derived from a directivity cost-minimization). User room adaptation is limited to boundary EQ + contour, not fine-grained room EQ. Constant directivity is the design's core premise.

Dutch & Dutch 8c: cardioid above 100 Hz; the designer (Martijn Mensink) states explicitly the speaker should not be EQ'd above the Schroeder frequency (~200 Hz); their REW RoomMatching guide restricts user filters to below 1 kHz, ideally below Schroeder, using a spatial average of 9 measurements. Notably, they DO apply factory PEQ targeting midrange breakup modes (>4 kHz) despite steep crossovers, reporting reduced long-term fatigue — an internal manufacturer example of fine-grained resonance EQ on the direct sound.

KEF LS50 Wireless II / LS60 / Blade: time-correcting DSP crossover; Uni-Q + MAT; limited user EQ (Room/Desk/Wall modes, treble trim, phase-correction toggle). KEF's own white paper describes the system as non-minimum-phase (from crossover/driver summation). Correction philosophy is factory-voiced + coarse room modes, not user fine-grained EQ.

Dirac Live (primary: Dirac white paper "On Room Correction and Equalization of Sound Systems"): proprietary mixed-phase (IIR+FIR) structure — "neither plain FIR nor plain IIR." Corrects magnitude to a target curve AND the impulse/time-domain response ("reduce the pre-ringings"). Uses many measurement points to optimize over a listening window/volume, not a single seat. Default boost limited to ~10 dB; narrow dips/nulls are never filled ("always position dependent in real acoustic spaces"). The white paper explicitly notes: with few resources ("≈10 biquads or 20–40 FIR taps") impulse correction isn't possible and "it is wiser to focus on the magnitude response and use minimum-phase biquad filters," and warns that group delay "only measures the delay of the envelope... It is nonsense to interpret this as a physical delay."

Trinnov Optimizer (primary: Trinnov reference/user manuals; first presented AES 2005): combines FIR (amplitude+phase across the full band) and minimum-phase IIR (default 10, adjustable up to 50, applied from a maximum frequency default of 150 Hz downward); FIR length default 100 ms; corrects direct sound, early reflections, late reverb, and modes with different techniques; uses a proprietary 4-capsule 3D microphone to separate direct sound from reflections; 64-bit float processing; a pre-ringing-reduction option was added in firmware v4.3.1 (acknowledging FIR pre-ringing as a real concern). "All the subtlety of the Optimizer resides in knowing which defects may be corrected without creating additional problems."

Sonarworks SoundID Reference / Reference 4: measures across a ~37-step position set (speakers); corrects to a target curve; user can limit the calibration frequency range; offers a "Zero Latency" (IIR) mode and higher-latency modes; phase correction not currently implemented for user speakers ("A separate Phase Correction feature might be implemented in the future"). PAPFR (Perceived Acoustic Power Frequency Response) perceptual measurement is used for headphones.

Lyngdorf RoomPerfect, Devialet SAM, Acourate (Uli Brüggemann), Audiolense (Bernt Rønningsbakk): Acourate and Audiolense are FIR-based convolution tools offering full mixed/linear-phase correction with user-controlled frequency-dependent windowing (FDW) and smoothing; both let the user restrict correction bandwidth and windowing to avoid overfitting the room's high-frequency fine structure. RoomPerfect deliberately does NOT correct to a single point — it builds a room "knowledge" model from many random positions and corrects the room's global behavior while preserving the speaker's direct-sound character. (Flag: specifics for Devialet SAM, Meyer Sound, d&b audiotechnik, and L-Acoustics presets are thinner in accessible primary literature; those pro presets are primarily system-voicing + array processing rather than per-room fine EQ.)

Common thread: none of these limits itself to "5 filters," but every serious system limits where and how it corrects — heavy modal correction below Schroeder, gentle broadband shaping above, spatial averaging, refusal to fill nulls, and preservation of the speaker's neutral direct sound.

4. The two cases, steelmanned

FOR fine-grained on-axis correction:

* Toole/Olive: low-Q resonances audible at ~1 dB or less, and pink noise reveals coloration "at levels that challenge our ability to measure accurately" — so driving on-axis error well below 1 dB removes audible resonances, not phantom ones.
* Olive's preference model rewards low NBD and high smoothness of the on-axis and in-room curves — a smoother direct sound predicts higher preference.
* On a well-designed 2-way with smooth directivity, the on-axis, listening-window, and early-reflection curves are similar in shape, so minimum-phase correction of the on-axis curve improves all of them simultaneously.
* Dutch & Dutch's own practice of EQ'ing midrange breakup modes (fine-grained, on the direct sound) for reduced fatigue is a manufacturer data point for benefit.

AGAINST (substantive, beyond convenience):

* Overfitting a single gated/quasi-anechoic measurement: a gated measurement has limited frequency resolution at low frequencies and includes measurement noise; fitting to 0.07 dB RMS can chase noise, mic artifacts, and position-specific diffraction ripple rather than real, stable driver behavior.
* Minimum-phase vs. non-minimum-phase: only minimum-phase features can be correctly EQ'd. Diffraction ripple and reflection-induced comb filtering are direction/position-dependent and largely non-minimum-phase; EQ'ing them flat on-axis makes off-axis worse.
* On-axis at the expense of power response / listening window / early reflections (Toole's spinorama reasoning): correcting a direction-dependent on-axis wiggle flat can degrade the off-axis response and hence the reflected field that dominates what the listener hears in-room. Because "the room does not mask resonances," a correction that helps on-axis but harms the listening window can be a net loss.
* Measurement uncertainty: mic calibration, positioning sensitivity, and sample-to-sample/thermal/level-dependent driver variation all mean the "true" response is a distribution, not a single curve; 0.07 dB is far below the reproducibility of the measurement itself.
* No controlled listening test has shown audible benefit of pushing on-axis RMS from, say, 0.5 dB to 0.07 dB; the incremental gain below ~0.5–1 dB is unsupported by the audibility data.

5. Directivity changes the answer

Yes — fine-grained on-axis correction is more defensible on a constant-directivity speaker. Rationale from the literature:

* Toole/Olive and the CTA-2034 / spinorama framework: what a listener hears in a normally reflective room is a blend of direct sound (on-axis/listening window) and reflected sound (early reflections + sound power). If directivity is smooth and constant, all these curves have the same shape, so a minimum-phase correction derived from the on-axis (or listening-window) response improves every curve at once. "Reflected sounds should resemble the direct sound in timbre, otherwise they draw attention to themselves"; a smooth directivity index (DI) with no discontinuities is the marker of a correctable speaker.
* On a wide-dispersion or irregularly-directive speaker, an on-axis dip may be a diffraction/interference artifact that does not exist off-axis; flattening it on-axis then tilts the power response and worsens the reflected field. Here fine on-axis correction is actively risky.
* Geddes (waveguide/constant-directivity advocacy) and Linkwitz both argued that controlled directivity reduces the room's contribution and makes the direct-sound correction meaningful; Linkwitz EQ'd his own dipoles to a target but relied on their well-behaved (if wide) directivity.
* Toole's "the room does not mask resonances" means a genuine resonance in the direct sound is audible through reflections and should be corrected regardless of directivity — but a direction-dependent wiggle is not a resonance and should not be treated as one.

Recommendations

Stage 1 — Validate that the 0.07 dB fit is not overfitting (do this first).

* Re-measure 3–5 times, moving the mic a few cm each time and re-seating it; compare. Any feature that moves or changes between measurements is position/measurement-dependent and must NOT be EQ'd. Keep only correction of features stable across all measurements.
* Confirm your gated/quasi-anechoic window's frequency resolution. Below roughly c/(gate length) you have no valid data; do not place filters there from the gated measurement.
* Benchmark that changes the plan: if features you're correcting are not reproducible across re-measurements, delete those filters — target the stable response, expecting on-axis RMS to settle around a few tenths of a dB rather than 0.07 dB.

Stage 2 — Check the correction against directivity, not just on-axis.

* Measure (or obtain) off-axis responses (at least ±30°/±60° horizontal) and construct at least a crude listening-window and early-reflections average. Apply your on-axis correction to those curves and verify it doesn't create new off-axis bumps.
* Threshold: only apply full-strength EQ to features present in BOTH the on-axis and off-axis/listening-window curves (i.e., minimum-phase, direction-independent — real resonances). For features that appear on-axis but not off-axis (diffraction/interference), either leave them or correct at reduced strength.

Stage 3 — Constrain the filters by the perceptual data.

* Prioritize cutting peaks; do not fill narrow dips (Bücklein; universal commercial practice). A deep narrow null on-axis is almost always interference — filling it wastes headroom and helps nothing.
* Limit maximum Q. High-Q corrective boosts ring and are position-sensitive; keep corrective Q moderate (the moderate-Q choice you already made is well-aligned). Reserve high-Q cuts only for verified, stable, high-Q driver/cabinet resonances (which a CSD/decay plot should confirm).
* Above the Schroeder/transition frequency (~200–500 Hz depending on room), prefer broadband/gentle correction of the speaker's own smooth trends; below it, room-mode correction belongs to an in-room measurement, not the gated one. Your gated speaker EQ and any in-room/modal EQ are different jobs — keep them separate, as Genelec, Dutch & Dutch, and Dirac all do.

Stage 4 — Numerics and phase (reassurance, minimal action).

* No action needed on phase/group delay: 24 moderate-Q minimum-phase biquads are far below all published GD thresholds. CamillaDSP's 64-bit double path makes cascade numerical noise a non-issue. Do not add allpass "phase correction" chasing linear phase — that introduces audible pre-ringing risk (Mäkivirta 2018; Trinnov's v4.3.1 pre-ringing mitigation) for no demonstrated benefit on a 2-way.

Stage 5 — Adopt a perceptually-weighted success metric.

* Instead of (or alongside) tilt-removed RMS, evaluate NBD and smoothness (Olive) on the on-axis AND an estimated in-room/listening-window curve. This rewards a smooth, resonance-free response rather than an arbitrarily tiny RMS number that may reflect overfitting.
* Benchmark that would change recommendations: if you can run a blind ABX between your 0.07 dB tune and a conservatively re-fit ~0.3–0.5 dB tune (validated across directivity) and reliably prefer the former, keep it; the literature predicts you will not hear a difference once both are free of audible resonances.

Bottom line for the user's specific case: 24 moderate-Q minimum-phase biquads on a 2-way is not inherently too many, and 0.07 dB tilt-removed on-axis RMS is not inherently a problem — phase, group delay, and 64-bit-double numerics are all comfortably below any published audibility or engineering concern. But 0.07 dB is almost certainly below the reproducibility of your own gated measurement, which strongly suggests some of those filters are fitting measurement artifacts. The defensible version of your tune is the subset of those filters that (a) corrects features stable across repeated measurements, (b) survives an off-axis/listening-window check, (c) cuts peaks and verified resonances rather than filling nulls, and (d) uses moderate Q. Expect that disciplined re-fit to land nearer a few tenths of a dB RMS — and that is the number the literature actually supports, not 0.07 dB and not the community's "5 filters / ±3 dB."

Caveats

* Exact Toole & Olive threshold curves (dB vs. Q at each frequency) are behind the AES paywall; the specific figures cited here (≈10 dB high-Q; 1–2 dB low-Q; 0.25 dB at Q=1/5 kHz; 5× program factor) are drawn from Toole's own summaries (harman.com "Audio-Science in the Service of Art"), the audioXpress technical synthesis, and Audioholics' direct citation of the paper — they are consistent across sources but are summaries, not the primary tables. Treat the precise numbers as well-corroborated approximations of the published curves.
* Blauert & Laws numbers vary slightly by secondary source (e.g., 1 kHz quoted as both 2.0 and 2.1 ms; 2 kHz as 0.9 or 1.0 ms) depending on rounding; the shape (most sensitive ~1–2 kHz, ~1 ms) is consistent.
* The "no independent penalty for many minimum-phase filters" conclusion is an inference from established minimum-phase theory plus the GD-audibility literature; no paper has specifically tested "N corrective biquads vs. fewer." It is a well-supported inference, not a direct experimental result — flagged as such.
* No published controlled listening test directly compares coarse vs. fine correction resolution on a single speaker. This is a genuine gap; claims on both sides of the "for/against fine correction" debate rest partly on inference from adjacent studies.
* The "under 5 filters per driver" community rule has no located published basis whatsoever. "Within ±3 dB" is a workable field tolerance for live sound but is looser than laboratory audibility thresholds for low-Q features.
* Some manufacturer details (Neumann MA 1 filter type/count; Devialet SAM, Meyer Sound, d&b, L-Acoustics internal EQ specifics) are documented only at marketing level in accessible sources and are flagged accordingly; per-model Genelec filter counts across the full current range are confirmed in primary sources only for specific models (e.g., 16 notch + 4 shelving on the 8380A).
