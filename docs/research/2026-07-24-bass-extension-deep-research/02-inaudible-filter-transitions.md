# Retuning IIR Biquads Inaudibly on a Live Stereo Music Stream: A Technical Deliverable

> **Provenance:** external deep-research report commissioned by the maintainer,
> delivered 2026-07-24. Archived verbatim below (research artifact, not a
> contract — actionable deltas route through wave revisions; see this
> directory's [README](README.md)).

## TL;DR
- **You can walk a Linkwitz Transform (Fp/Qp) and a subsonic high-pass corner inaudibly, but not by raw coefficient swapping at arbitrary rates.** Constrain each update so the magnitude response changes by no more than ~0.1 dB at any frequency per step, run updates on a fixed cadence (every audio block, ~10–50 updates/s), and either interpolate parameters through a well-behaved structure or crossfade parallel chains — do not hard-swap Direct-Form biquad coefficients on the running filter without smoothing.
- **Your measured −75 dBFS artifact floor is almost certainly inaudible in practice, but the margin is calibration-dependent.** At a typical domestic calibration (0 dBFS ≈ 100–105 dB SPL), −75 dBFS ≈ 25–30 dB SPL — at or below the absolute threshold of hearing in the deep bass and comparable to a quiet room's noise floor — and it is further masked by the −20 dBFS (80–85 dB SPL) program. The only real risk is a low-frequency burst fired during near-silence.
- **You can prove inaudibility on-device with 2 listeners using forced-choice ABX (~25 trials each) on worst-case material, but you must frame the result as an equivalence/confidence bound, not merely "failed to reject."** Pool trials, use exact binomial thresholds, insert catch trials, and pre-register the detectability ceiling you will accept.

## Key Findings

1. **Direct-form biquads are the worst structure for coefficient modulation; the click is a state/coefficient mismatch, not a magnitude error.** When you swap coefficients of a Direct-Form filter under signal, the delay-line state (which encodes recent signal history *as interpreted by the old transfer function*) becomes momentarily inconsistent with the new coefficients, and the filter's internal state rings toward the new steady state. Wishnick (DAFx-14, 2014) showed with a 100 Hz sinusoid that an instantaneous parameter change produces spectral sidebands audible as an impulsive "click," and that **sideband energy — not peak transient level — correlates with perceived degradation** (the paper frames low sideband energy as "most desirable," with a significant negative correlation, Pearson r ≈ −0.59, between sideband RMS and MUSHRA quality score).

2. **Some filter structures are unconditionally stable and artifact-light under time-varying coefficients; Direct-Form II is not.** Coupled form (Gold–Rader / normal form) and the trapezoidal-integrated State Variable Filter (SVF/TPT) are provably BIBO-stable under time-varying coefficients (Laroche, JAES 2007; Wishnick 2014 gives a novel stability proof for the SVF). Direct-Form II and transposed DF-II can become unstable when time-varying even if every instantaneous coefficient set is individually stable.

3. **"Zipper noise" persists even in stable structures if you step coefficients too coarsely.** Practitioners report that even the very stable TPT-SVF needs per-sample (or per-few-sample) coefficient interpolation to avoid audible zipper artifacts on cutoff sweeps. The fix is to make parameter/coefficient changes quasi-continuous, not merely to change structure.

4. **Level JND is ~0.5–1 dB broadband; spectral/resonance JND can be far smaller (~0.25 dB) but only for broadband, favorable material.** Toole & Olive (JAES 36(3):122–142, March 1988) found a 5 kHz, Q=1 resonance just detectable at **0.25 dB using pink noise** (their most revealing signal); on the least revealing music the threshold rose roughly 5×. Sensitivity to spectral changes is *higher* for low-Q broad features and *lower* for high-Q narrow ones — and much lower in the deep bass.

5. **In the 20–120 Hz region the absolute threshold of hearing is high and rising steeply, which is your friend.** ISO 226 equal-loudness contours show the ear needs far higher SPL at 20–50 Hz to perceive sound at all; artifacts and small magnitude changes concentrated in the deep bass are intrinsically harder to hear than broadband ones.

6. **Group-delay/phase audibility thresholds rise steeply toward low frequency.** Blauert & Laws (JASA 63(5):1478–1483, 1978) measured thresholds of **3.2 ms at 500 Hz, 2 ms at 1 kHz, 1 ms at 2 kHz, 1.5 ms at 4 kHz, 2 ms at 8 kHz** — with **no data below 500 Hz**. Liski, Mäkivirta & Välimäki (IEEE/ACM TASLP, 2021) report for real signals that a positive GD peak is audible at ~1.5–4.5 ms and a negative peak at −1.0 to −2.3 ms (tested 500 Hz–4 kHz). Bass rules of thumb allow tens of ms (~20–50 ms at 20 Hz). A Linkwitz Transform corner shift changes phase, but in the bass the tolerances are large, so *magnitude* audibility, not phase, dominates.

7. **Shipped products change bass EQ dynamically with smoothing time constants of milliseconds to hundreds of ms — never instantaneously.** TI smart-amp DRC, hearing-aid syllabic compressors (attack <10 ms, release 5–200 ms), and plugin parameter smoothers (one-pole, ~5–50 ms) all ramp gain rather than stepping it. **CamillaDSP itself ramps its Volume/Loudness filters over a `volume_ramp_time` defaulting to 400 ms** (the older 0.5.x Volume filter `ramp_time` defaulted to 200 ms).

## Details

### 1. The three classic time-varying-IIR approaches and their artifacts

**(a) Direct coefficient swapping.** Cheapest, worst. Rossum ("Making Digital Filters Sound Analog," ICMC 1992) framed the core insight: changing a feedback coefficient injects a discontinuity into the feedback signal that is *equivalent to a discontinuity in the input signal* — so the audible result is a filtered click, and if coefficient changes are not band-limited they behave like aliased input. His prescription was to update coefficients at the sample rate (band-limiting the change), otherwise images of the update rate appear in the signal path. Wishnick (DAFx-14) confirmed that hard "output switching" (the idealized instantaneous transfer-function change) is perceptually the *wrong* goal for music: it maximizes the audible sideband click. Counterintuitively, the transient a well-behaved filter structure produces actually *smooths* the transition.

**(b) Crossfading between two parallel filter banks.** Run the old and new filters in parallel, both fed the live signal, and crossfade the outputs over N samples. This is robust and structure-agnostic (works for any biquad form) because each filter always has self-consistent state. It costs 2× compute during the fade and needs an appropriate window (equal-power/constant-power for uncorrelated outputs, linear for highly correlated ones) and a length long enough to cover the ring. iZotope-style and GlissEQ-style implementations run one path with parameter interpolation plus one static path, then crossfade; residual artifacts are described as small "plops" on very abrupt changes.

**(c) Smooth coefficient / parameter interpolation (the "zipper" domain).** Linearly interpolating raw biquad coefficients can pass through nonsensical (and in principle unstable) intermediate transfer functions. Better is to interpolate the *parameters* (fc/Q/gain), or to work in a domain where intermediates are well-behaved: the SVF/TPT maps fc and Q to nearly independent coefficients and yields sensible intermediate shapes; the pole-zero and normalized-ladder/lattice domains keep poles inside the unit circle by construction. Vassilevsky's classic comp.dsp advice: convert the denominator to a lattice and interpolate the PARCOR (reflection) coefficients — the filter then stays stable regardless of the transition trajectory.

**Structure ranking under modulation (Wishnick's MUSHRA test, 21 expert listeners):**
- **Best / provably stable & efficient:** the TPT State Variable Filter (SVF), and stabilized transposed Direct-Form II (TDF-II + Rabenstein–Czarnach state transform). Stabilized TDF-II scored best on average; the SVF was close behind and better for lowpass/frequency sweeps.
- **Stable but poorer sounding:** Coupled form (Gold–Rader) scored surprisingly low.
- **Worst:** Direct-Form II and "output switching"/transient-minimization (Zetterberg–Zhang, Rabenstein) consistently scored "bad"–"poor" for music.
- **Objective DC test:** only the SVF (and stabilized SVF) achieved an ideal DC response — no switch-time transient — because for DC input its state is provably independent of the filter parameters.

**Why direct forms ring and others don't.** Direct-form state variables are large and strongly parameter-dependent, so a coefficient change maps to a big, audible state mismatch. State-variable / coupled / normalized-ladder forms have states corresponding to more physically meaningful quantities (band outputs, orthogonal components) that stay meaningful across parameter changes, so the mismatch and its ring are smaller. Wishnick's own diagnosis is that changes to the filter's *zeros* (which set DC gain) cause the audible discontinuities, whereas "the transient response resulting from changes in the poles is perceptually pleasant." Unconditional time-varying BIBO stability is proven for coupled form and the TPT-SVF; Direct-Form II and lattice/normalized-ladder are NOT unconditionally stable under arbitrary coefficient variation.

### 2. JNDs and audibility thresholds in the 20–120 Hz region

**Level JND.** Classic difference-limen data (summarized across Fletcher/Munson, Riesz, and modern reviews): for SPL > 40 dB and f > 100 Hz the level JND is ~1 dB or less, dropping to 0.25–0.4 dB at 1–4 kHz and high SPL. Recent piano-tone work found a loudness JND of ~0.68–1.22 dB. Toole distinguishes broadband "turn the volume up" loudness JND (~0.5–1 dB) from a partial-spectrum *timbre* JND — the latter is what a filter change actually produces.

**Spectral / resonance JND.** Toole & Olive (1988) is the anchor: a Q=1 resonance detectable to 0.25 dB with pink noise, rising ~5× on the least revealing music. Their key result for you: detectability is *worse* (higher threshold) for high-Q narrow features and *better* for low-Q broad ones, and worse in the deep bass. Olive et al. (1997), "The Detection Thresholds of Resonances at Low Frequencies" (JAES 45(3):116–128), is the directly relevant paper: low-frequency resonances have substantially higher detection thresholds than midband ones.

**Absolute threshold / equal-loudness.** ISO 226:2003/2023 contours: the ear is dramatically less sensitive at 20–50 Hz (threshold ~40 dB SPL at 50 Hz, ~55–60 dB SPL at 30 Hz vs. ~0–10 dB SPL at 1–4 kHz). This raises the effective JND for magnitude changes in the deep bass and raises the audibility floor for any artifact concentrated there.

**Slow drift vs. abrupt step.** The "dynamic range paradox" central-adaptation work (ramped increment detection on pseudo-continuous noise) shows the intensity JND *increases* for slowly ramped changes — a slow gain drift is harder to detect than an abrupt step of equal magnitude. This is the perceptual basis for scheduling: spread the total change over time and keep each step below the step-JND.

**Temporal masking.** Forward masking extends up to ~100–200 ms after a masker; backward masking up to ~20 ms before. A 10 dB rise in masker level raises the masked threshold by only ~3 dB (compressive). For a short artifact burst riding on continuous program, simultaneous + forward masking substantially raise its detection threshold — except in gaps/silence, where masking vanishes.

**Group delay / phase.** Blauert & Laws (1978) and Liski et al. (2021) as above. Below 500 Hz there is no controlled data; the practical bass rule of thumb is to keep GD below ~1–2 periods of the frequency (≈20–50 ms at 20 Hz; ~10–20 ms at 50–100 Hz). A Linkwitz Transform shift changes phase, but bass tolerances are large enough that phase is a non-issue relative to magnitude.

### 3. Known-good schedules from shipped products

- **CamillaDSP itself:** Volume and Loudness filters ramp gain smoothly over `volume_ramp_time`, **default 400 ms** (older 0.5.x Volume filter `ramp_time` default 200 ms). This is your existing in-engine precedent for smoothing.
- **Texas Instruments smart amps (TAS-series, e.g. TAS2563):** SmartAmp DRC plus excursion/thermal protection continuously modulate gain and per-band levels with configurable compressor attack/release; TI's tuning guide recommends worst-case tuning with music containing piano, bass, drums, and voice.
- **Hearing-aid dynamic-range compression:** fast (syllabic) compressors use attack <10 ms, release 5–200 ms; slow compressors use release up to seconds. Gain modulation creates distortion sidebands whose amplitude grows with modulation rate — the direct analog of your problem — which is why adaptive-time-constant designs lengthen the constants for small fluctuations to suppress modulation distortion.
- **Plugin conventions (JUCE, chowdsp):** one-pole or linear parameter smoothing with ramp lengths typically **5–50 ms**; JUCE `SmoothedValue` supports linear and multiplicative (dB/Hz) ramps. Multiplicative smoothing is recommended for frequency and dB-gain because it steps in perceptually uniform increments.
- **Rossum/E-mu digital samplers:** interpolate coefficient sets at the sample rate (log-spaced frequency values for formant filters) to eliminate audible artifacts.

### 4. State-mismatch click mechanism and mitigations

**Mechanism.** A biquad's output at sample n depends on its stored state (delay-line contents), which were produced under the *old* coefficients. Swap in new coefficients and the recursion reinterprets that state, producing (i) a possible step discontinuity in the output sample, (ii) a DC-offset step if the DC gain changed, and (iii) a transient that decays with the *new* filter's time constant. Because decay time ∝ Q/f_c, **high-Q, low-corner filters ring longest and loudest** — exactly the Linkwitz-Transform/subsonic regime. This is why your artifact is a low-frequency "thump," not a broadband tick.

**Mitigations, in rough order of robustness:**
1. **Per-sample (or per-small-block) parameter interpolation in a good structure (SVF/TPT).** Most robust for continuous walking; no second filter needed.
2. **State transform / stabilization (Rabenstein–Czarnach)** to keep a DF-II-family filter well-behaved.
3. **Parallel-instance equal-power crossfade** over a window long enough to cover the ring (tens of ms at these corners); switch at a zero crossing if doing a hard handoff.
4. **State rescaling/remapping** for small coefficient changes (approximate; historically Hypersignal spread each coefficient change over ~10 samples).
5. **"Warming up" the new filter** with recent input history before handoff.

**Quantification.** Published absolute click levels are scarce; the literature (Wishnick) treats severity via sideband energy within ~1 ERB of the tone rather than a single dBFS figure. Your bench numbers (−75 to −94 dBFS bursts on a −20 dBFS signal, i.e. artifact 55–74 dB below program) are a concrete datum that most papers lack — use it directly.

### The CamillaDSP-specific reality

A full config reload resets filter state (guaranteeing a transient); an incremental patch swaps coefficients on running filters (your −75 dBFS case). Because CamillaDSP biquads are Direct-Form (f64) and you cannot easily drop in an SVF, your practical levers are: (i) make each coefficient patch tiny so the state mismatch is tiny; (ii) patch on a regular cadence so any residual images sit at a fixed, high, maskable rate; and (iii) if needed, run two bass chains and crossfade via the mixer/gain. The f64 arithmetic gives huge headroom against coefficient-quantization limit cycles, so numerical noise is not your concern — **state mismatch is.**

## Recommendations

### (a) Recommended maximum step size + cadence

**Primary recommendation — parameter-scheduled small steps on a fixed cadence:**

- **Cap the per-update magnitude change at ≤ 0.1 dB at every frequency.** Before applying a step, compute max |ΔdB| across ~10–500 Hz between the old and new response. 0.1 dB is ~2.5× below the best-case ideal spectral JND (0.25 dB) and ≥10× below the realistic music JND (>1 dB), with additional deep-bass margin from ISO 226. This response-delta cap — not a raw Hz number — is the single most important constraint.
- **Translate to parameters empirically.** For a subsonic HP corner near 20–40 Hz and an LT with Qp ~0.5–1.0, 0.1 dB/step is typically on the order of ≤0.5–1% change in Fp and ≤0.01–0.02 in Qp per update — but **verify by computing the response delta**, because near a high-Q corner a small Fp move produces a large *local* dB change.
- **Cadence:** apply updates once per audio block on a regular clock, ~10–50 updates/s (every 20–100 ms). Regular cadence matters: per Rossum, block-rate steps put images at the update rate; keeping that rate fixed and moderately high — with each step tiny — turns any residual product into a stationary, maskable, very-low-level tone rather than a random click.
- **Interpolate on top of stepping.** Within CamillaDSP's Direct-Form constraint, the cleanest paths are (1) a **parallel-chain equal-power crossfade** per committed step if bursts remain audible, or (2) migrating the LT/HP walk into an **SVF/TPT stage interpolated per-sample** in a small pre-processor. If you can only patch coefficients, hold steps at ≤0.05 dB and cadence ≥20/s to approximate continuous interpolation.
- **Total transition time:** there is no rush. At 0.1 dB/step and 20 steps/s you get 2 dB/s of local change — imperceptibly slow, and squarely inside the "slow drift is harder to detect than a step" regime. A full volume-scheduled bass-extension walk can comfortably take 0.5–3 s.

**Margin reasoning:** ideal-condition spectral JND ≈ 0.25 dB; music raises it >1 dB; deep bass raises it further. A 0.1 dB cap sits below even the ideal JND, giving ≥10 dB of realistic margin. Phase/GD changes from the corner shift are far below bass GD thresholds (tens of ms) and can be ignored.

### (b) Assessment of the −75 dBFS artifact floor

**Arithmetic (domestic nearfield; 0 dBFS = 100 dB SPL peak, and a hotter 105 dB SPL case):**

| Quantity | dBFS | SPL @ 100 dB cal | SPL @ 105 dB cal |
|---|---|---|---|
| Program signal (your test) | −20 | 80 dB | 85 dB |
| Worst-case artifact burst | −75 | **25 dB** | **30 dB** |
| Best-case artifact burst | −94 | 6 dB | 11 dB |

**Compare to thresholds:**
- **Absolute threshold of hearing:** at 1–4 kHz ATH ≈ 0–10 dB SPL, so a *broadband* 25–30 dB SPL burst would be audible in perfect silence. **But your artifact is LF-concentrated** (the ring of a low-corner, moderate-Q filter). At 50 Hz ATH ≈ 40 dB SPL; at 30 Hz ≈ 55–60 dB SPL (ISO 226). **A 25–30 dB SPL burst centered below ~50 Hz sits at or below the absolute threshold of hearing** — inaudible even in silence. Any broadband/click component above ~200 Hz is the real risk.
- **Room noise floor:** a quiet domestic room is ~25–40 dBA (roughly NR/NC 20–30). Your 25–30 dB SPL burst is at or below typical residential ambient noise.
- **Masking by program:** with −20 dBFS (80–85 dB SPL) music playing, simultaneous + forward masking raise the detection threshold for a 55–74 dB-down burst far above its physical level. During sustained bass, a co-located LF artifact is deeply masked.

**Verdict:** The −75 dBFS floor is **safely inaudible during program material at normal levels.** Caveats: (1) the risk case is a burst during **near-silence or a quiet gap**, where masking vanishes and only ATH/room-noise protect you — here the LF concentration of the artifact is what saves you, so confirm by spectral analysis that the burst has no broadband/click content leaking above ~200 Hz; (2) if a user calibrates hotter than 105 dB SPL, or listens nearfield in a very quiet room at night, the margin shrinks. **Net: −75 dBFS is fine, but treat it as your *worst* burst and drive it lower (toward −90 dBFS) by shrinking step size — cheap insurance for the silence case.**

### (c) On-device blind listening-test protocol (2 listeners)

**Method: forced-choice ABX** (equivalently 2AFC same/different). ABX is the right tool for near-threshold "is there any difference" questions and matches ITU-R BS.1116's double-blind, hidden-reference philosophy while being far simpler to automate than full BS.1116 triple-stimulus grading. Use MUSHRA (BS.1534) only if you later want to *rank* severity; for pure inaudibility, ABX.

**A vs. B definition:** A = the stream with your live retuning engine active (walking Fp/Qp/corner through a transition *during* the excerpt); B = the identical excerpt with the endpoints applied statically (no live retuning). The listener identifies which of A/B matches X. The transition must fire inside the excerpt so the artifact is actually present in "A."

**Trials and statistics (small-N):**
- Run **~25 trials per listener** (a practical ceiling before fatigue); 2 listeners → **50 pooled trials**.
- Under H0 (inaudible, p = 0.5), exact one-sided binomial 95% thresholds: per listener, 16/25 gives p ≈ 0.054 and 17/25 gives p ≈ 0.022 — use **17/25** for a clean p < 0.05. Pooled, **≈32/50** is the p < 0.05 boundary. Pre-register the pooled threshold.
- **Prove the negative properly.** "Failed to reach 17/25" is NOT proof of inaudibility. Pre-register an **equivalence bound**: the largest detection probability you will tolerate (e.g., p_c ≤ 0.6). Then require the **upper 95% (Clopper–Pearson) confidence bound on the observed proportion correct to lie below that bound.** Note the power limit: with 50 pooled trials, scoring near chance only lets you exclude roughly p_c > 0.67. To tighten the claim to p_c < 0.6 you need ~100–150 pooled trials — so either add sessions/listeners or state the looser bound explicitly. **Report the confidence interval, not just the p-value.**

**Program material (worst-case for bass artifacts):**
- Solo/sparse bass lines; sustained low sine/organ tones (30–60 Hz); quiet passages; and **silence/near-silence with a transition fired in the gap** (the true worst case — no masking).
- Sustained low synth pads that let the ring decay be heard.
- One broadband pink-noise excerpt (most revealing per Toole & Olive) as a sensitivity check.
- Avoid dense, loud, broadband music for the critical trials — it masks everything and inflates the pass rate misleadingly; use it only as a labelled "real-world reference" condition.

**Level calibration:** set playback so 0 dBFS equals your worst-case field calibration (the loudest a user might run), e.g. 100–105 dB SPL peak, verified with an SPL meter and pink noise. Test at that level *and* at a realistic listening level — higher level is more revealing for LF artifacts near ATH.

**Randomization / bias control:**
- Fully automate on the Pi: a script (pyCamillaDSP over the websocket) randomizes X per trial with a seeded RNG, logs the truth table, plays A/B/X on demand, and records responses — **listener and operator must both be blind to X** (double-blind). Write results to CSV; compute stats afterward.
- Randomize trial order and A/B assignment independently. Insert **catch trials** (A vs. A, expected at chance) to detect cueing or rig artifacts.
- Level-match A and B exactly (ITU-R BS.1770 loudness, or bit-level where possible) so loudness cannot cue the answer.
- Do not reveal running scores mid-session.

**Practical scripting:** wrap CamillaDSP with pyCamillaDSP; where feasible, pre-render A and B WAVs and switch instantaneously (avoids the documented ABX pitfall where file-load latency cues X). If testing the live engine, ensure the transition fires identically each trial. Log per trial: excerpt ID, level, truth, response, reaction time. After the session, compute the exact binomial p and the Clopper–Pearson CI.

## Caveats

- **No lab data exists for group-delay audibility below 250 Hz**, and none for the specific task of "detecting a slow Linkwitz-Transform walk on music." The ≤0.1 dB/update recommendation is an engineering extrapolation from spectral-JND data (Toole & Olive 1988; Olive et al. 1997) plus large deep-bass safety factors, not a directly measured threshold. It is deliberately conservative.
- **The −75 dBFS→SPL conversion depends entirely on your calibration assumption** (I used 0 dBFS = 100–105 dB SPL). If your product runs hotter, or a reviewer tests in an anechoic chamber at high gain, re-run the arithmetic. The burst's frequency content matters as much as its level — confirm by FFT that it is LF-dominated.
- **CamillaDSP's Direct-Form biquads are the least-friendly structure for this** (per Wishnick's ranking). If artifacts persist after step-size reduction, the highest-leverage fix is architectural — move the LT/HP walk into an SVF/TPT stage interpolated per-sample, or crossfade parallel chains — rather than pushing coefficient patches faster.
- **Small-N listening tests have low power.** Two listeners × 25 trials can *detect* a gross problem but only weakly *bound* inaudibility. Treat a pass as "no evidence of audibility up to a detection probability of ~0.67," and say so; add listeners/trials for a tighter equivalence claim, or supplement with the objective sideband-energy metric.
- Cited level/resonance JND figures come from steady-signal lab studies; real-time modulation on music is a different, generally *easier* task to pass, so these thresholds are conservative when used as design limits.
