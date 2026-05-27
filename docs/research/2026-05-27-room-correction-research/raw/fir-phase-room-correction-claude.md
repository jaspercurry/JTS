# FIR Readiness for Room Correction in JTS (Raspberry Pi 5 + CamillaDSP)
## A Staged Engineering Path from PEQ to Mixed-Phase Convolution

---

## Executive Summary

JTS is correct to be cautious. FIR room correction is a 10× force multiplier over PEQ — and a 10× foot-gun when the measurement does not justify it. The literature and 20 years of practitioner experience converge on a small number of hard rules: (1) **above the Schroeder transition (≈200–300 Hz in a typical small room), the response is no longer minimum-phase and aggressive narrow correction is destructive at any single point**; (2) **linear-phase FIR is the only family that can introduce audible pre-ringing in narrow notches, and minimum-phase FIR mathematically cannot**; (3) **mixed-phase / excess-phase correction (Dirac, Acourate, Audiolense) only helps when seat-to-seat variance is low enough that the excess group delay being corrected is real, not artifactual**; and (4) **on a Pi 5 / 1 GB / CamillaDSP, taps and CPU are not the constraint — measurement quality and bundle hygiene are.**

The Pi 5 single-core Geekbench 6 score is 764±6 (4 KB page size), a "×2.4 speed increase over Raspberry Pi 4" per Alasdair Allan, "Benchmarking Raspberry Pi 5," raspberrypi.com/news, 20 Oct 2023. The only authoritative published CamillaDSP convolution benchmark is the HEnquist README's "A Raspberry Pi 4 doing FIR filtering of 8 channels, with 262k taps per channel, at 192 kHz. CPU usage about 55%." A 2-channel JTS configuration at 48 kHz with a 65 536-tap minimum-phase FIR will sit well under 5% CPU on one Pi 5 A76 core. Latency, not CPU, is the binding constraint and the Pi 5 / CamillaDSP v2/v3 floor for partitioned convolution at 48 kHz is ~25 ms with chunksize 256 and target_level 768 per the mdsimon2/RPi-CamillaDSP tutorial.

This report recommends a five-stage FIR ladder (Stage 0–4) where each stage unlocks only when measurement-quality gates are met by an automated **FIR Readiness Validator** — implemented as a structured report consumed by both the UI and CI. The validator uses excess group delay, coherence across MLP positions, and pre-ring energy as gates. Concrete pseudocode for each gate is included.

---

## TL;DR — three bullets

- **Use minimum-phase FIR (Stage 1) as soon as you have a stable IR and want richer corrections than PEQ can express; use linear-phase or mixed-phase FIR (Stages 3–4) only when multi-position coherence is high and excess group delay is reproducible** — otherwise you are correcting noise, and linear/mixed-phase FIR makes that audible as pre-ring.
- **CamillaDSP on a Pi 5 is not the bottleneck.** Power-of-two chunksize 256 at 48 kHz with target_level 768 gives ~25 ms latency (mdsimon2/RPi-CamillaDSP tutorial) and a 65 536-tap stereo FIR sits within budget. **The bottleneck is measurement quality**: SNR, timing-reference provenance (loopback vs. acoustic), multi-position variance.
- **Build the FIR Readiness Validator before you ship FIR generation.** It is a ~200-line analyzer that emits one of `{PEQ_ONLY, MIN_PHASE_FIR_READY, LINEAR_PHASE_FIR_READY, MIXED_PHASE_FIR_READY, UNSAFE}` plus structured reasons; it is the single most important piece of code in the FIR pipeline because it prevents "auto-FIR" disasters.

---

## 1. When Is FIR Actually Justified Over PEQ? (Decision Theory)

The PEQ–FIR boundary is not "FIR sounds better." It is a function of what your measurement can prove.

**PEQ is mathematically equivalent to minimum-phase FIR** for the same magnitude target — IIR biquads *are* a minimum-phase representation. The advantages of FIR begin where PEQ ends:

| What you want to do | PEQ can | FIR can |
|---|---|---|
| Magnitude EQ in modal region | ✓ (bounded Q, gain limits) | ✓ |
| High-resolution magnitude shaping (e.g. 1/96-octave-resolved house curve) | partial (filter count grows) | ✓ |
| Linear-phase EQ (no phase rotation through the cut) | ✗ | ✓ |
| Excess-phase / all-pass correction (group-delay flattening) | ✗ | ✓ |
| Linear-phase crossover | ✗ | ✓ |
| Driver time-alignment via fractional-sample delay | partial | ✓ |
| Frequency-dependent windowed correction | ✗ | ✓ |

**Justification thresholds (literature + practitioner consensus):**

- **Below Schroeder (~200–300 Hz in domestic rooms; Schroeder F_S = 2000·√(T_60/V) per Manfred Schroeder's classical formulation, summarized at prosoundtraining.com/2021/10/14/divide-and-conquer-the-schroeder-frequency/)**: PEQ is sufficient because the response is dominated by sparse, mostly minimum-phase modes. REW's own help page is explicit: *"Room measurements are typically not minimum phase except in some regions, mainly at low frequencies"* (roomeqwizard.com/help/help_en-GB/html/graph_splphase.html). Anthem's ARC engineering page argues this so strongly they refuse mixed-phase correction entirely: *"ARC does not use mixed-phase equalization due to the likelihood of harmful artifacts"* (anthemarc.com/advanced-topics/the-science-of-arc.php).
- **Above Schroeder**: Floyd Toole and Sean Olive's Harman research (referenced repeatedly throughout the DRC and Acourate literature; see Archimago's Acourate walkthrough citing Toole's "Measurement and Calibration of Sound Reproducing Systems") is that EQ should be gentle, broad, and based on spatially-averaged data. Narrow above-transition correction is invalid because it is point-specific. The DRC documentation makes this explicit and limits high-frequency correction by FDW; DRC's docs note that with the default `normal.drc` settings only ~2–3% of the time/frequency plane is actually corrected, rising to ~20–30% if interpreted psychoacoustically.
- **The practical inflection** to graduate from PEQ to minimum-phase FIR is when (a) you want correction resolution finer than ~10 PEQ biquads can express, or (b) you need to embed a linear-phase crossover, or (c) you need driver time-alignment with sub-sample resolution. Below that, PEQ is *better* — fewer artifacts, lower memory, hot-reloadable per-biquad, and trivially auditable.

**The complexity cost of FIR is real.** Pre-ringing risk (linear/mixed-phase only), latency, bundle size, debugging difficulty, and the temptation to overcorrect with high-resolution targets all weigh against. The Dirac whitepaper, Trinnov marketing, and the ASR/diyAudio forums all describe systems where mixed-phase correction worked beautifully or sounded "thin" or "bright" — the difference is almost always measurement quality and target-curve discipline.

---

## 2. Correction Approaches — Practical Differences

### Magnitude-only correction
Smoothed magnitude vs. target; phase ignored. Realizable as biquad PEQ (minimum-phase implicitly) or as a zero-phase FIR ("magnitude FIR") via inverse FFT of the desired magnitude with zero phase — but a zero-phase FIR is non-causal and identical in IR to a linear-phase FIR after a delay. **Use case in JTS**: current PEQ pipeline.

### Minimum-phase FIR
FIR designed so all zeros lie inside the unit circle. Constructed by taking the desired log-magnitude, computing the Hilbert transform of `log|H(ω)|` to get the minimum-phase, then `H_min(ω) = exp(log|H(ω)| + j·θ_min(ω))` and inverse-FFT. **Properties**: no pre-ring, lowest group delay realizable for that magnitude, identical magnitude correction to PEQ. **Use case**: Stage 1 in JTS — drop-in replacement for PEQ that allows much higher magnitude resolution.

### Linear-phase FIR
Symmetric impulse response → constant group delay = (N–1)/2 samples. **Properties**: phase distortion of cuts is zero, but symmetry means **pre-ring before each impulse equal to post-ring**, which becomes audible on narrow notches and high-Q boosts. Latency is half the filter length. The Bodzio "Pre_Ringing.pdf" demonstration (bodziosoftware.com.au/Pre_Ringing.pdf) walks through audibility: pre-ring is generally inaudible for cuts ≤ ~3 dB and Q ≤ ~3 because of psychoacoustic pre-masking, but becomes audible quickly above that. **Use case**: linear-phase crossovers, flat-phase EQ on broad-band shaping. Risky for modal correction.

### Mixed-phase / excess-phase FIR
The transfer function is factored: `H = H_min · H_ap` where `H_ap` is an all-pass containing the excess (non-minimum) phase. Correcting only `H_min` is minimum-phase EQ; additionally inverting `H_ap` removes group-delay irregularities. Dirac's whitepaper (dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf) calls this *"mixed-phase inversion"* and notes that *"even in a good listening room with good speakers a substantial improvement is possible using a careful mixed-phase design"*. The Dirac AVForums summary states: *"There's a look-ahead buffer that enables impulse response correction. In addition, minimum-phase and linear-phase room correction filters can't physically optimise the acoustic impulse response in a room. … Room-acoustic responses are non-minimum-phase, which is why Dirac Live uses mixed-phase correction."* This look-ahead buffer is engineered pre-ring. **Use case**: Stage 4 only, with multi-position averaged measurements and bounded excess-phase windowing.

### Frequency-Dependent Windowed (FDW) correction
Apply a time window whose length, expressed in cycles at each frequency, decreases as frequency rises — REW's docs (roomeqwizard.com/help/help_en-GB/html/analysis.html) make this concrete: *"If the width is in cycles a 15 cycle window (for example) would have a width of 150 ms at 100 Hz (15 times 10 ms), 15 ms at 1 kHz (15 times 1 ms) and 1.5 ms at 10 kHz (15 times 0.1 ms)."* This emulates how the ear integrates low frequencies over long windows and high frequencies anechoically. **DRC-FIR's documentation describes its algorithm as "band windowing and sliding lowpass linear time variant filtering"** (drc-fir.sourceforge.net/doc/drc.html). FDW is the **single most important psychoacoustic prior** in room-correction algorithms after Schroeder.

---

## 3. How to Determine Minimum-Phase Safe Regions Programmatically

This is the core of the FIR Readiness Validator. The canonical method is **excess group delay**:

1. Compute the measurement complex transfer function `H(ω)` from the windowed IR via FFT.
2. Compute the minimum-phase version `H_min(ω)`: take `log|H(ω)|`, apply the discrete Hilbert transform to derive `θ_min(ω)` (this is exactly the algorithm REW's "Generate Minimum Phase" button uses, per the REW Group Delay help page).
3. Compute **excess group delay**: `τ_excess(ω) = -d(θ_meas(ω) - θ_min(ω))/dω`.
4. **Minimum-phase regions** are those where `τ_excess(ω)` is flat. REW's documentation states explicitly: *"Anywhere the excess group delay plot is flat is a minimum phase region of the response. We can see there are regions even at very low frequencies where the response is not minimum phase, between about 44 and 56 Hz for example"* (roomeqwizard.com/help/help_en-GB/html/minimumphase.html).

Practical implementation (cepstral method, the canonical Oppenheim & Schafer recipe also used by `scipy.signal.minimum_phase`):

```python
# Pseudocode for FIR Readiness Validator: minimum-phase region detection
import numpy as np

def minimum_phase_from_magnitude(H_mag, eps=1e-9):
    """Derive minimum-phase from magnitude via real-cepstrum folding.
       Wikipedia 'Minimum phase': arg[H(jω)] = -Hilbert{log|H(jω)|}."""
    log_mag = np.log(np.maximum(np.abs(H_mag), eps))
    cepstrum = np.fft.ifft(log_mag).real
    n = len(cepstrum)
    folded = np.zeros_like(cepstrum)
    folded[0] = cepstrum[0]
    folded[1:n//2] = 2 * cepstrum[1:n//2]
    folded[n//2] = cepstrum[n//2]
    min_phase_log_spectrum = np.fft.fft(folded)
    return np.exp(min_phase_log_spectrum)

def excess_group_delay(H_meas, fs):
    H_min = minimum_phase_from_magnitude(np.abs(H_meas))
    phase = np.unwrap(np.angle(H_meas) - np.angle(H_min))
    omega = 2*np.pi*np.fft.fftfreq(len(H_meas), 1/fs)
    return -np.gradient(phase, omega)

def minimum_phase_mask(tau_excess, freqs, slope_threshold_us_per_oct=200):
    """Boolean mask: True where |dτ_excess / d(log2 f)| < threshold,
       i.e., τ_excess is locally flat in log-frequency."""
    # ...
```

**Coherence analysis** (cross-spectral magnitude squared between repeat sweeps, or between sweep and reference) provides the SNR floor below which any phase analysis is noise. Rational Acoustics' Smaart documentation describes the canonical use: *"Coherence is technically an estimation of linearity in transfer function measurements. In practical terms, it tends to be an indicator of a signal-to-noise ratio"* (support.rationalacoustics.com/support/solutions/articles/150000214546). For JTS the simplest test is γ²(f) between two consecutive sweeps at the same MLP — any frequency with γ² < 0.9 is not safe for narrow correction.

**Multi-position variance** is the third signal: compute the variance across N MLP measurements of magnitude (in dB) and of unwrapped phase. Frequencies where σ_mag(f) > 6 dB or σ_phase(f) > 90° across positions are by definition not minimum-phase safe at the listening *area* — they describe a spatial cancellation that no point-EQ can fix. The Olivier Hoel MMM paper ("Back to pink? MMM Moving Mic Measurement," ohl.to/audio/downloads/MMM-moving-mic-measurement.pdf) documents variations up to 16 dB across 9 positions separated by ~10 cm in a treated cinema mixing room from 0.5 to 2 kHz — the kind of variance that must not be "corrected" point-wise.

---

## 4. Required Measurement Artifacts Before FIR Generation

A FIR-ready bundle must contain (per measurement and per channel):

1. **Raw capture WAV** — the recorded mic signal of the exponential sine sweep, plus the sweep parameters (start_f, end_f, duration, fade_in, fade_out, level). This is what REW, Acourate, and DRC's `glsweep`+`lsconv` toolchain require.
2. **Impulse response** (mono, ≥ 24-bit float, length ≥ 1 s) derived from deconvolution. Save before *and* after windowing.
3. **Complex transfer function** `H(ω)` at 1/48-octave or finer (DRC's typical default).
4. **Phase and unwrapped phase**, plotted and stored as text alongside SPL/phase per REW's `.frd` export format.
5. **Group delay and excess group delay** — derived per the procedure in §3.
6. **Window settings**: left-window type (rectangular for IIR-derived MP, Hann/Tukey for measured), left window width in ms, right window width in ms, FDW cycles (low-f, high-f) per REW's `Analysis Preferences` page. Store as YAML so the bundle can be replayed. REW notes: *"by default REW will set the widths of the windows automatically … with a 500 ms right side window and a 125 ms left side window if the end frequency of the sweep is above 200 Hz."*
7. **Timing reference provenance**: explicit field `{loopback, acoustic, none}`. Loopback timing is mandatory for any excess-phase work; without it, phase is referenced to an arbitrary `t=0` and the "excess group delay" measurement is contaminated by the sound card's input/output delay.
8. **Multi-position metrics**: positions list, σ_mag(f), σ_phase(f), coherence γ²(f) between repeats.
9. **Equipment chain**: mic serial + calibration file SHA-256, DAC + interface, sample rate, bit depth.
10. **Target curve** as a 2-column text or as parameters (Harman tilt, ~6–8 dB below 1 kHz to 20 kHz per the Dirac/Harman literature: Dirac's target-curve page describes the rationale as "Harman's study … connects psychoacoustic data with controlled testing").

---

## 5. The Staged FIR Ladder — Per-Stage Decision Logic

| Stage | What it adds | Gate to enable | YAML/code surface |
|---|---|---|---|
| **0** | FIR runtime import/export only; no generation | `camilladsp[Conv]` filter compiled in (always on) | `type: Conv` `parameters: {type: Wav, filename:, channel:}` |
| **1** | Minimum-phase FIR for magnitude correction | At least 1 stable IR with valid timing; SNR ≥ 40 dB in target band; coherence γ² ≥ 0.9 in 50 Hz–10 kHz | Generator: `H_min = exp(log|H_target/H_meas| + j·Hilbert(log|·|))` |
| **2** | Same as 1, but with declared latency / headroom / clip-risk report exposed to user | All Stage 1 gates + automated headroom analysis (peak gain across band ≤ +12 dB; otherwise force gain reduction) | Adds bundle field `headroom: {peak_gain_dB, clip_margin_dB, latency_ms}` |
| **3** | FDW-windowed FIR (default 15/15-cycle psychoacoustic window per Acourate convention) for higher-resolution but conservative correction | All Stage 2 gates + multi-position N ≥ 3, σ_mag < 4 dB in correction band, σ_phase < 60° below 500 Hz | Generator wraps measurement IR with FDW before inversion |
| **4** | Mixed-phase / excess-phase opt-in | All Stage 3 gates + excess group delay reproducible across two independent measurement sessions (Δτ_excess < 0.5 ms in 80–500 Hz) + opt-in flag in config | Generator factors `H = H_min · H_ap`, designs causal+pre-ring-bounded inverse of `H_ap` with bounded look-ahead window |

### Per-stage decision logic — narrative

**Stage 0** is unconditional. The Pi 5 build of CamillaDSP must ship with `Conv` filter support so users can hand-load filters from REW/rePhase/DRC/Acourate/Audiolense regardless of JTS's own generator. This is what the third-party `VilhoValittu/CamillaFIR` (closed-source per its README, Python+WebUI, "currently developed as a closed-source / proprietary project") consumes; JTS should be at least that compatible.

**Stage 1** unlocks when a single MLP measurement is "good." Definition: the impulse response shows a clean direct sound, the noise floor in the target band is ≥ 40 dB below the peak, there are no clipping markers in the raw WAV, and SNR-by-octave (computed from the noise tail of the IR) is ≥ 30 dB up to 10 kHz. This is *not stricter than what JTS already needs for PEQ.* The minimum-phase FIR at this stage is a strict superset of PEQ — the user gains resolution but cannot get pre-ring.

**Stage 2** unlocks when Stage 1's filter passes a peak-gain audit. The validator simulates `H_filter · H_target` and reports the maximum boost. If any 1/24-octave band shows > +12 dB of boost, the filter is rejected and a smoothed target with a deeper dip is offered. This stage exists to prevent the classic "auto-EQ tried to fill a null" failure mode.

**Stage 3** requires multi-position data because FDW is psychoacoustically motivated by spatial integration — using FDW on a single point and then sitting elsewhere is worse than no correction. Acourate, Audiolense, and DRC all enforce this implicitly via their workflows (Acourate's macros, Audiolense's "sweetspot definition," DRC's `Measure` script).

**Stage 4** is the most dangerous and the highest-reward. The key insight from Dirac's whitepaper and Acourate's macros is that **excess phase that is not reproducible is not real**. JTS should require *two independent sweep sessions*, computed minutes apart with the user briefly disturbing and resetting the mic, and accept Stage 4 only if `‖τ_excess,1(f) - τ_excess,2(f)‖_∞ < 0.5 ms` in 80–500 Hz. This avoids the problem documented by Archimago in his 2015 Acourate measurements article: *"if I created filters with 'Excessphase window' as 6/6/6/6 rather than 5/5/5/5, this is the step response I get with the test convolution: There's now an unfortunate ringing in the left channel which was not there before"* (archimago.blogspot.com/2015/11/measurements-digital-room-correction.html).

---

## 6. Prior-Art Comparison

| Tool | License | Phase approach | FDW support | IR-domain or freq-domain | Notes for JTS |
|---|---|---|---|---|---|
| **REW** | Closed source, free | Generates IIR PEQ (min-phase). FIR export via inverse impulse / generic EQ → WAV | ✓ (cycles or octaves, default 15 cyc) | Both | Source of truth measurement tool; UMIK-1 friendly; can act as JTS's UI for FIR export today |
| **rePhase** | Free, closed source (Pos) | Linear-phase, minimum-phase, and arbitrary phase EQ; linearizes Linkwitz-Riley XO | partial via measurement import | freq-domain design | Excellent manual phase-correction; output WAV → CamillaDSP `Conv` |
| **DRC-FIR (Sbragion)** | GPL (open source) | Inversion with Toeplitz; psychoacoustic target; mixed-phase via excess-phase component | ✓ Two algorithms: band windowing + sliding low-pass LTI; supports `erb.drc` ERB-matched config | Time-domain inversion with Kirkeby regularization (`kirkebyfd.h` in source) | Most open algorithmic detail; algorithms reusable in JTS |
| **Acourate** | €380, closed | Mixed-phase: phase extraction into min-phase + excess-phase; macros 1–4 build, correct, and time-align | ✓ FDW in cycles low/high | Pulse-domain (impulse response) | Reference target for serious DRC; Bob Katz-endorsed; uses 32 768-bin default frequency resolution per AudioVero docs |
| **Audiolense (XO)** | ~$500, closed | Mixed-phase with user-tunable min/linear blend; TTD time-domain target; minimum or linear-phase XO | ✓ | Pulse-domain | Closest workflow analog to a JTS-style appliance: target + filter export to convolver |
| **Dirac Live** | Proprietary | "Mixed-phase" via patented IIR+FIR hybrid; uses look-ahead buffer; multi-position modeling | implicit via multi-position averaging | Frequency + time | Reference for mixed-phase outcome; whitepaper at dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf |
| **Trinnov Optimizer** | Proprietary | Full time/frequency optimization; per-channel + global; uses tetrahedral 3D mic for spatial info | proprietary equivalent | Time + frequency + spatial | Inferred: similar to Dirac in mixed-phase but with spatial decomposition. Not reproducible without 3D mic |
| **Anthem ARC Genesis** | Proprietary | Explicitly **minimum-phase only** — refuses mixed-phase due to artifacts | proprietary | FFT-based | Useful counterpoint: serious vendor that says "no" to mixed-phase |
| **CamillaDSP** | GPLv3 | Convolution engine only — accepts arbitrary FIR | n/a | n/a | Target runtime. v3.0.0 (released ~Jan 2025 per GitHub release notes) added optional multithreaded filter processing and removed FFTW option |
| **BruteFIR** | ISC (open) | Convolution only; partitioned frequency-domain | n/a | n/a | Lower-level than CamillaDSP; FIR-only; "I/O-delay becomes twice of the partition length" (torger.se/anders/brutefir.html) |
| **CamillaFIR (Vilho Valittu)** | Closed-source freeware | Asymmetric linear phase, A-FDW (Adaptive FDW), automatic target/preset search; mixed-phase | ✓ | Both | Python+WebUI; consumes REW WAV; not redistributable, but a useful reference UX |
| **HiFiBerry DAC8x guide** | Open docs | YAML-only crossovers; PEQ; FIR via WAV | n/a | n/a | Reference for Pi 5 + I2S DAC + CamillaDSP topology (hifiberry.com/blog/camilladsp-in-the-pi5-dac8x/) |

---

## 7. CamillaDSP / Raspberry Pi 5 Runtime — Concrete Numbers

### CamillaDSP convolution architecture

From the official README (github.com/HEnquist/camilladsp):

> "FIR filters are automatically padded as needed, so there is no need match chunk size and filter length. CamillaDSP uses FFT for convolution, with an FFT length of 2 * chunksize. … When a FIR filter is longer than the chunk size, the convolver uses segmented convolution. The number of segments is calculated as filter_length / chunk size, and rounded up to the nearest integer."

CamillaDSP therefore implements **uniform partitioned convolution** (UPOLS) with partition length = chunksize. From v3.0.0 (released approximately January 2025 per github.com/HEnquist/camilladsp/releases/tag/v3.0.0: *"New features: Optional multithreaded filter processing … Changes: Remove the optional use of FFTW instead of RustFFT"*) the option `multithreaded: true` distributes filters across worker threads. The default FFT is RustFFT (NEON-accelerated on ARM). HEnquist's own offline benchmark on RPi 4 @ 1500 MHz (diyAudio CamillaDSP thread page 49, 7 Oct 2020, 44.1 kHz/16-bit WAV, 2^16 taps): wall time 8.787 s (RustFFT) vs. 8.176 s (FFTW3), a 7.5% gap; BruteFIR completed in 7.297 s (~12% faster than RustFFT). On power-of-2 chunksizes the gap is negligible — which is why FFTW was eventually removed in v3.0.0.

### Latency formula

End-to-end latency ≈ (capture buffer + 2 × chunksize + playback buffer). In CamillaDSP v2/v3 the default playback buffer is 4 × chunksize. `target_level` (samples) controls steady-state buffer fill; lower = lower latency, higher = more underrun margin.

The mdsimon2/RPi-CamillaDSP tutorial recommends:

| Sample rate | chunksize | target_level | Resulting end-to-end latency |
|---|---|---|---|
| 44.1 / 48 kHz | 256 | 768 | ~25 ms |
| 88.2 / 96 kHz | 512 | 1 536 | ~25 ms |
| 176.4 / 192 kHz | 1 024 | 3 072 | ~25 ms |

Verbatim from that README: *"All configurations use target level of 3 x chunk size. These chunksize / target level settings result in ~25 ms latency. If lower latency is desired, considering reducing chunksize / target level by factors of two."*

### Tap count vs. latency (FIR-specific)

**Linear-phase FIR** introduces additional latency equal to **(N–1)/2 samples** beyond the partitioned-convolution buffer (the impulse is centered at the midpoint). **Minimum-phase FIR** introduces effectively zero IR-domain latency beyond the partition cost (the impulse energy is concentrated at the start). For JTS this is decisive: minimum-phase FIR at any tap count adds only the partitioned-convolution buffer; linear-phase FIR adds half the filter length.

| Taps | Linear-phase added latency @ 48 kHz | Linear-phase added latency @ 96 kHz |
|---|---|---|
| 4 096 | 42.7 ms | 21.3 ms |
| 8 192 | 85.3 ms | 42.7 ms |
| 16 384 | 170.7 ms | 85.3 ms |
| 32 768 | 341.3 ms | 170.7 ms |
| 65 536 | 682.7 ms | 341.3 ms |
| 131 072 | 1.365 s | 682.7 ms |

For a JTS smart speaker, anything above ~85 ms is unacceptable for any visual sync or interactive use. **This is why minimum-phase FIR is the default workhorse stage.** A 65 536-tap minimum-phase FIR at 48 kHz adds only the partitioned-convolution buffer cost (≈10–30 ms) and gives ~1.4 Hz frequency resolution — far more than necessary for room correction. A linear-phase 65 536-tap at 48 kHz adds 683 ms and is unacceptable.

### CPU budget on Pi 5

The only authoritative figure published is the Pi 4 case: HEnquist's README states *"A Raspberry Pi 4 doing FIR filtering of 8 channels, with 262k taps per channel, at 192 kHz. CPU usage about 55%."* HEnquist's own granular sweep on diyAudio (CamillaDSP thread page 99) for the same 8-channel/262 144-tap/192 kHz, no resampling case:

> "chunksize: 8192 → 71% CPU usage; chunksize: 16384 → 58% CPU usage; chunksize: 32768 → 55% CPU usage."

Scaling roughly linearly with channels and inversely with sample rate, a 2-ch / 65 536-tap / 48 kHz JTS case on the same Pi 4 should be in the ~3–5% range. The Pi 5 is ~2.4× faster single-core per Alasdair Allan, "Benchmarking Raspberry Pi 5," raspberrypi.com/news, 20 Oct 2023 (*"764±6 for Raspberry Pi 5 using a 4 KB page size … That's a ×2.4 speed increase over Raspberry Pi 4"*; the multi-core score was 1604±22, ×2.2 over Pi 4). The JTS budget on Pi 5 is therefore approximately 1–3% of one A76 core for the convolution itself.

**CamillaDSP's default filter processing is single-threaded**; from the README: *"The audio pipeline in CamillaDSP runs in three separate threads. One thread handles capturing audio, one handles the playback, and one does the processing in between."* Enable `multithreaded: true` (v3+) only for very long FIRs or many channels — HEnquist's caveat on diyAudio is that *"It needs quite heavy filter tasks to actually help, with too 'easy' filters the overhead of passing things back and forth between threads gets larger than the actual processing time."*

### Memory budget on Pi 5 / 1 GB

Per-filter memory ≈ N_taps × 4 bytes (f32 coefficients) × ~2–3 (FFT working buffers, complex segments). A 65 536-tap stereo (one per channel) filter is ~1 MB raw + a few MB of FFT scratch. Even 262 144 taps × 2 channels is ~4 MB raw. This is trivial on 1 GB.

### Concrete recommended JTS YAML for Stage 1

```yaml
devices:
  samplerate: 48000
  chunksize: 256
  target_level: 768
  capture:
    type: Alsa
    channels: 2
    device: "hw:Loopback,1"
    format: S32LE
  playback:
    type: Alsa
    channels: 2
    device: "hw:0,0"
    format: S32LE

filters:
  rc_min_phase_L:
    type: Conv
    parameters:
      type: Wav
      filename: /var/jts/bundles/active/correction_L_minphase_48k.wav
      channel: 0
  rc_min_phase_R:
    type: Conv
    parameters:
      type: Wav
      filename: /var/jts/bundles/active/correction_R_minphase_48k.wav
      channel: 0

pipeline:
  - type: Filter
    channel: 0
    names: [rc_min_phase_L]
  - type: Filter
    channel: 1
    names: [rc_min_phase_R]
```

For hot-reload, write a new WAV file with a different filename and either rename atomically into place + SIGHUP (the README notes the filename must change for FIR coefficient updates to be picked up on SIGHUP: *"Note that for this to update the coefficients for a FIR filter, the filename of the coefficients file needs to change"*), or use the websocket `SetConfig` API for atomic config swap.

---

## 8. Pre-Ringing — Detection and Avoidance

### Why it exists and why minimum-phase is immune

A linear-phase FIR has a symmetric impulse response. Any deep narrow notch in the magnitude response, when realized as the inverse FFT of that magnitude with linear phase, produces a `sinc`-like impulse with **equal pre-impulse and post-impulse oscillation**. The Wikipedia "Minimum phase" article summarizes the math: *"the minimum-phase part of a general causal system implements its amplitude response with minimal group delay, while its all-pass part corrects its phase response."* Because a minimum-phase filter places all zeros inside the unit circle, the energy of its impulse response is concentrated at the start (technical statement: of all filters with a given magnitude, the minimum-phase one has maximum partial energy `Σ_{n=0}^{M}|h[n]|²` for every M). Mathematically: **no causal minimum-phase filter can have pre-impulse energy beyond t=0**.

Uli Brüggemann (author of Acourate) himself confirmed the mechanism on ASR (audiosciencereview.com/forum/index.php?threads/pre-ringing-with-linear-phase-room-eq-filters.11121/): *"The preringing is caused by an 'excessive' excessphase treatment. The excessphase has an all pass behaviour, also after treatment. Thus the spectrum remains unchanged."* Acourate's documentation echoes this — too many excess-phase cycles in Macro 4 produces pre-ringing without changing the magnitude response.

### Time-domain Gibbs phenomena

Truncating a long ideal FIR to N taps with a rectangular window produces Gibbs ripples in both magnitude (frequency-domain) and IR (time-domain pre/post ring). Standard mitigation: window with Hann/Tukey/Blackman before truncation. Bodzio's `Pre_Ringing.pdf` shows aggressive elliptic 8th-order filters produce visible audible pre-ring while typical loudspeaker filters do not.

### Detection metrics for JTS

Compute these on every generated FIR before approval:

```python
def pre_ring_energy_dB(h, t_main_peak_idx):
    pre  = np.sum(h[:t_main_peak_idx]**2)
    post = np.sum(h[t_main_peak_idx:]**2)
    return 10*np.log10(pre / (post + 1e-20))

def pre_ring_max_amplitude_dB(h, t_main_peak_idx):
    return 20*np.log10(np.max(np.abs(h[:t_main_peak_idx])) /
                       np.max(np.abs(h)))
```

Heuristic gates:

- `pre_ring_energy_dB > −40 dB` → reject (or flag warning).
- `pre_ring_max_amplitude_dB > −30 dB` → reject.
- For any 1/24-octave band where the filter applies > +6 dB of boost or > +0 dB at Q > 5: refuse linear-phase realization, force minimum-phase.

### EQ depth limit

The DRC documentation and the Acourate-vs-Audiolense discussions converge on a soft cap of **about +6 dB of correction above Schroeder** and **+10 to +12 dB below Schroeder**, with the proviso that filling a deep null is futile (a 30 dB null at 67 Hz from a floor bounce will eat all the power amp's headroom for no audible benefit). The validator should hard-cap correction at +12 dB anywhere and warn above +6 dB above Schroeder.

### Q-limiting via smoothing kernel

For inversion-based design (`H_filter = H_target / H_meas_smoothed`), the smoothing kernel **is** the Q limit. DRC uses FDW (Gabor-inequality–constrained windowing) to control resolution per band: from the DRC docs, *"Applying the Gabor inequality to the window length between the two curves of pre-echo and ringing truncation it is pretty easy to get an equivalent frequency resolution, as a function of center frequency, of the frequency dependent windowing procedure."* JTS should follow the same idea: choose FDW cycles per band such that effective Q is bounded.

### FDW window construction pseudocode

```python
def fdw_window(impulse, fs, low_cycles=15, high_cycles=15,
               low_freq=20, high_freq=20000):
    """Apply DRC-style frequency-dependent window via band synthesis."""
    n = len(impulse)
    bands = log_bands(low_freq, high_freq, n_bands=24)
    out = np.zeros_like(impulse)
    for f_lo, f_hi in bands:
        cycles = interp_log(np.sqrt(f_lo*f_hi),
                            (low_freq, low_cycles),
                            (high_freq, high_cycles))
        center_f = np.sqrt(f_lo*f_hi)
        win_len_samples = int(cycles * fs / center_f)
        band_filtered = bandpass(impulse, f_lo, f_hi, fs)
        win = hann(win_len_samples)
        peak_idx = np.argmax(np.abs(band_filtered))
        windowed = apply_window_centered(band_filtered, win, peak_idx)
        out += windowed
    return out
```

This matches the "band windowing" path described in the DRC source documentation. The alternative sliding low-pass LTI path is also implemented in DRC; for JTS the band synthesis is simpler to validate.

---

## 9. The FIR Readiness Validator — Output Schema and Decision Logic

### Output schema (JSON)

```json
{
  "schema_version": "1.0",
  "bundle_id": "uuid",
  "timestamp": "2026-05-26T12:00:00Z",
  "verdict": "MIN_PHASE_FIR_READY",
  "max_recommended_stage": 1,
  "reasons": [
    {"code": "GATE_OK",   "stage": 1, "check": "snr_target_band",        "value_db": 47.2, "threshold_db": 40.0},
    {"code": "GATE_FAIL", "stage": 3, "check": "multi_position_count",   "value": 1, "threshold": 3,
     "message": "FDW correction requires ≥3 MLP measurements; only 1 provided."}
  ],
  "metrics": {
    "schroeder_hz_estimate": 240,
    "snr_per_octave_db": {"31": 22, "63": 38, "125": 48, "250": 52, "500": 54, "1000": 56, "2000": 56, "4000": 54, "8000": 49, "16000": 42},
    "coherence_p10": 0.78,
    "coherence_p50": 0.93,
    "coherence_p90": 0.99,
    "min_phase_mask_fraction": {"below_300hz": 0.68, "above_300hz": 0.31},
    "excess_gd_max_us_below_500hz": 4200,
    "excess_gd_reproducibility_us_between_sessions": null,
    "multi_position": {"n": 1, "sigma_mag_db_p50": null, "sigma_phase_deg_p50": null},
    "headroom": {"peak_gain_db_proposed": 8.4, "clip_margin_db": 3.6}
  },
  "recommended_target_curve": "harman_-6_at_20k",
  "recommended_filter": {
    "stage": 1, "type": "min_phase_fir",
    "taps": 65536, "samplerate": 48000,
    "estimated_latency_ms": 11.5,
    "estimated_cpu_pct_pi5": 2.0
  }
}
```

### Decision logic (pseudocode)

```python
def validate(bundle):
    reasons = []

    # Universal Stage-0 readiness
    if not bundle.has_ir() or bundle.timing_reference == "none":
        return verdict("UNSAFE", reasons + [r("NO_TIMING_REFERENCE", 0)])

    # Stage 1 gates: min-phase FIR
    snr_ok  = bundle.snr_per_octave_min(40, band=(50, 10000))
    coh_ok  = bundle.coherence_pct(>=0.9, band=(50, 10000))
    no_clip = not bundle.raw_clipped
    if not (snr_ok and coh_ok and no_clip):
        reasons += diagnose([snr_ok, coh_ok, no_clip])
        return verdict("PEQ_ONLY", reasons)

    # Stage 2 gates: same as 1 + headroom
    h = simulate_filter_headroom(bundle.proposed_min_phase_fir)
    if not (h.peak_gain_db <= 12 and h.clip_margin_db >= 1):
        return verdict("MIN_PHASE_FIR_READY",
                       reasons + [r("HEADROOM_FAIL", 2, h)])

    # Stage 3 gates: FDW
    if not (bundle.mlp_count >= 3
            and bundle.sigma_mag_db_below_500hz < 4
            and bundle.sigma_phase_deg_below_500hz < 60):
        return verdict("MIN_PHASE_FIR_READY_PLUS_HEADROOM_OK",
                       reasons + diagnose_mlp(bundle))

    # Stage 4 gates: mixed-phase
    if not (bundle.has_two_independent_sessions()
            and bundle.excess_gd_reproducibility_us_below_500hz < 500
            and bundle.user_opt_in_mixed_phase):
        return verdict("FDW_FIR_READY",
                       reasons + [r("MIXED_PHASE_REQUIRES_REPRODUCIBILITY", 4)])

    return verdict("MIXED_PHASE_FIR_READY", reasons)
```

### Verdict strings — semantics

- `PEQ_ONLY`: any FIR generation refused; user gets PEQ as today.
- `MIN_PHASE_FIR_READY`: Stage 1 enabled. Linear-phase / mixed-phase explicitly refused.
- `MIN_PHASE_FIR_READY_PLUS_HEADROOM_OK`: Stage 2.
- `FDW_FIR_READY`: Stage 3.
- `MIXED_PHASE_FIR_READY`: Stage 4 — always behind a user opt-in toggle (`"i_accept_pre_ring_risk": true`).
- `MIXED_PHASE_UNSAFE`: bundle contains data that *looks* like it might justify mixed-phase but fails reproducibility.
- `UNSAFE`: bundle is unsuitable for any correction beyond conservative PEQ.

### What makes the staged decision easy vs. hard

**Easy** is the SNR / clipping / coherence gate; those are scalar thresholds on universally well-defined metrics.

**Hard** is everything spatial. The validator must distinguish between (a) a deep null that is real and unfixable, (b) a deep null that is a measurement artifact (mic placement near a node), and (c) a feature that varies wildly across MLPs and therefore should not be touched. The literature consensus (Toole 2008, Olive Harman papers, Dirac whitepaper) is that **only what is common across the listening area should be corrected**, which is operationally implemented as "average in some way and discard features with high spatial variance." The validator can express this as a spatial mask: at frequencies where σ_mag > 4 dB across MLPs, set the correction gain to 0 dB (i.e., decline to correct).

---

## 10. JTS Bundle — Required Artifacts for FIR Replay

A FIR-ready bundle is a versioned directory:

```
bundle-<uuid>/
├── manifest.yaml                    # schema_version, created, equipment, room hash
├── raw/
│   ├── sweep_params.yaml            # f0, f1, T, fade, level, samplerate
│   ├── sweep_signal.wav             # the played signal (for re-deconv)
│   ├── L_pos1_capture.wav           # raw mic captures
│   ├── L_pos2_capture.wav
│   └── ...
├── ir/
│   ├── L_pos1_raw.wav               # deconvolved IRs, no windowing
│   ├── L_pos1_windowed.wav          # with declared window settings
│   └── ...
├── analysis/
│   ├── L_pos1_complex_tf.npz        # H(ω) complex
│   ├── L_pos1_min_phase_tf.npz      # H_min(ω) for excess-phase math
│   ├── L_pos1_group_delay.npz
│   ├── L_pos1_excess_gd.npz
│   ├── coherence_inter_sweep.npz
│   ├── multi_pos_variance.npz       # σ_mag(f), σ_phase(f)
│   └── windows.yaml                 # left, right, FDW(low_cyc, high_cyc)
├── target/
│   ├── target_curve.csv
│   └── target_meta.yaml             # named curve + tilt + house-curve params
├── filter/
│   ├── readiness_report.json        # validator output
│   ├── correction_L_minphase_48k.wav
│   ├── correction_R_minphase_48k.wav
│   ├── correction_L_minphase_96k.wav
│   ├── correction_R_minphase_96k.wav
│   ├── design_params.yaml           # all knobs that produced this filter
│   └── camilladsp.yaml              # ready-to-hot-load config
├── verification/
│   ├── simulated_corrected_tf.npz
│   ├── post_install_remeasure.wav   # optional after-listen sweep
│   └── delta_report.json
└── provenance/
    ├── mic_calibration.txt
    ├── mic_calibration.sha256
    ├── camilladsp_version.txt
    ├── jts_version.txt
    └── git_commits.txt
```

**Replay rule**: given `manifest.yaml + raw/ + target/ + design_params.yaml`, the JTS pipeline must regenerate `filter/` deterministically (same SHA-256). This is the audit/debug contract.

---

## 11. Pre-Ringing and Latency Risk — Summary

| Risk | Stage where it appears | Mitigation in JTS |
|---|---|---|
| Pre-ring from linear-phase realization of narrow notches | Stage 3+ only | Refuse linear-phase if any 1/24-oct band has > +6 dB boost or Q > 5; enforce pre-ring energy ≤ −40 dB |
| Pre-ring from mixed-phase inversion of unstable excess-phase | Stage 4 only | Require excess-GD reproducibility across two sessions; bounded look-ahead window |
| Latency unacceptable for visual sync | Linear-phase at high tap counts | Cap linear-phase filter length so added latency ≤ 25 ms (≤ 2 400 taps at 48 kHz) |
| End-to-end latency on Pi 5 | All stages | chunksize 256 @ 48 kHz, target_level 768 → ~25 ms; expose to user; lower if smart-speaker latency budget allows |
| CPU surge → buffer underrun | Any stage with long FIR + small chunksize | `silence_threshold` and conservative target_level; rate-adjust + capture-clock tuning per CamillaDSP docs |
| Memory exhaustion on 1 GB | Many channels × very long FIR | Cap default to 65 536 taps stereo; explicit warning above |
| User adopts Stage 4 then moves furniture | All stages | Periodically re-run validator against fresh sweep; auto-fallback to Stage 2 if drift detected |

---

## 12. Recommendations — Staged, Concrete Next Steps

1. **Ship Stage 0 immediately.** Verify that the JTS CamillaDSP build statically links `Conv` filter support, document the YAML format for hand-loaded FIRs (`type: Conv`, `parameters: {type: Wav, filename:, channel:}`), and accept WAV files exported from REW/rePhase as-is. **Benchmark to advance**: 100% of REW WAV exports load and play without dropouts at chunksize 256 / target_level 768 on Pi 5.
2. **Implement the FIR Readiness Validator next, before any generator.** Output the JSON schema above. **Benchmark to advance**: validator produces deterministic verdicts on a corpus of ≥ 20 archived JTS measurements covering known-good and known-bad rooms.
3. **Implement Stage 1 minimum-phase FIR generator** as a strict superset of the current PEQ pipeline. Reuse the same target-curve subsystem; only the filter realization changes. **Benchmark to advance**: corrected magnitude response within ±1 dB of target on synthetic IR; CPU < 5% one core on Pi 5; latency ≤ 30 ms.
4. **Add Stage 2 headroom audit** and bundle-emission. Refuse filters with > +12 dB peak boost; offer the user a regenerated target with the null softened. **Benchmark to advance**: zero clipping events in 24-h listening tests at -6 dBFS reference.
5. **Implement FDW (Stage 3)** following DRC's band-windowing algorithm with 15/15-cycle default. Require ≥ 3 MLP measurements before exposing the option in UI. **Benchmark to advance**: σ between simulated post-correction responses at 3 MLPs < 3 dB above Schroeder.
6. **Defer Stage 4 (mixed-phase)** until Stages 1–3 are field-proven for at least a quarter of usage data. When implemented, require the two-session reproducibility test, the explicit opt-in flag, and a 1-click rollback to Stage 2.
7. **Publish a Pi 5–specific benchmark table** (48/96 kHz × {4 096, 16 384, 65 536, 131 072} taps × {2, 4} channels) to close the gap left by HEnquist's Pi 4–only published numbers.

**Stop conditions / fallback triggers**: at any point if (a) measurement σ across MLPs grows above the Stage 3 gate, (b) the validator's coherence falls below 0.85 over more than 1/3 octave bands, or (c) the user reports listening-test regressions, automatically fall back one stage and surface the reason in the UI.

---

## 13. Caveats

1. **Pi 5 native CamillaDSP benchmarks at JTS-relevant tap/SR combinations are not published.** Only the Pi 4 8-ch/262k/192 kHz @ 55% datapoint (HEnquist README) and the Pi 5's ~2.4× single-core advantage (raspberrypi.com/news, 20 Oct 2023) exist publicly. The 1–3% CPU figure used here is an extrapolation, not measured.
2. **Audibility of mixed-phase pre-ring at the bass region remains contested.** Dirac's whitepaper, Acourate practitioners, and Uli Brüggemann himself (ASR pre-ringing thread) all acknowledge that you can over-do it; what is missing is a published audibility threshold (e.g., "preserve ≤ X dB of pre-impulse energy at Y ms ahead of main"). JTS should err strongly conservative until in-house listening data exists.
3. **Closed-source comparisons are inferences.** Dirac's whitepaper and Acourate's wiki provide partial algorithmic detail; Trinnov's documentation is essentially product marketing. The "mixed-phase IIR+FIR hybrid" description of Dirac comes from the AVForums/Datasat reproductions of Dirac's marketing copy and may compress important detail.
4. **CamillaFIR is closed-source per its own README**; its "asymmetric linear phase" approach is documented in prose only. JTS cannot inherit code, only reproduce the idea.
5. **The Pi 5 / 1 GB RAM target is generous for these workloads**; if JTS later shrinks to a Pi Zero 2 W or CM4 with 512 MB, the per-channel FIR length caps will need re-derivation.
6. **Excess-phase reproducibility threshold of 0.5 ms** in 80–500 Hz used here is engineered, not measured against a published audibility study. Calibrate against listener data when available.
7. **Loopback vs. acoustic timing for the JTS appliance**: with a built-in mic and a built-in DAC, the path from sweep-out to mic-in includes both DAC latency and ALSA buffer; achieving a usable loopback time reference may require capturing the sweep at the DAC output via an ADC loopback channel, which the chosen sound card may not offer. This is the single most likely "FIR doesn't work" failure mode in deployment.

---

## 14. Open Questions / Unresolved Risks

1. Should JTS attempt mixed-phase at all on a 1 GB device aimed at a single listening seat that may be moved around? Strong product argument *against*; strong differentiation argument *for*.
2. CamillaFIR's "asymmetric linear phase" approach (closed-source) — its premise that *"most of the energy occurs after the main impulse while allowing controlled asymmetry"* (CamillaFIR docs) is exactly the right idea for a smart-speaker product, but absence of source code means JTS would need to re-derive from rePhase-equivalent freq-domain design (left-window shorter than right-window during inverse-FFT design).
3. **FDW algorithmic choice**: DRC uses "band windowing and sliding lowpass linear time variant filtering" (drc-fir source); REW exposes only cycles or octaves; Acourate uses psychoacoustic smoothing (linear-frequency interpolation from 16 Hz to Nyquist, per Hometheatershack page-6 thread). JTS should default to DRC's algorithm with `15/15` cycles, expose `low_cycles, high_cycles` as advanced controls, and consider Acourate's psychoacoustic smoothing as an alternative.
4. Does JTS benefit from CamillaDSP v3.0's `multithreaded: true`? Only for very long FIRs / many channels; HEnquist warns that for "easy" filters overhead exceeds gain. The 4-core A76 on Pi 5 is enough headroom that the default single-thread path is likely best for a 2-channel smart speaker.
5. Whether to expose FDW *parameters* to end users or only the "Stage 3" toggle. The DRC, Acourate, and REW communities all converge on 15/15 cycles as a sensible default; exposing the knob invites users to make it worse.
6. **MMM (moving-microphone measurement)** as an alternative to discrete N-position sweeps. The Hoel paper (ohl.to/audio/downloads/MMM-moving-mic-measurement.pdf) argues it is more representative for the listening area. JTS could support both, but the validator's σ-based gates assume discrete positions.

---

*End of report.*