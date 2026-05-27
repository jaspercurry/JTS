# Multi-Position Measurement Confidence and Per-Position Variance Reporting for JTS

## Executive Summary

The acoustics literature is consistent on five points that should structure JTS's confidence engine. (1) Below the Schroeder frequency `fs ≈ 2000·√(T60/V)`, the small room behaves modally and is reasonably correctable with parametric EQ; above fs (and certainly above ~4·fs or ~500 Hz, whichever is lower) the response is dominated by direct sound, early reflections, and SBIR/LBIR interference that vary strongly with position and should be left alone or treated only with broad shelves (Toole; Skålevik on Schroeder revisited; REW docs). (2) Spatial averaging across multiple seats is the single most important defense against equalizing seat-local artifacts; Olive's Harman work explicitly used 6-position spatial averages "to avoid equalizing spectral artifacts that are very localized to the position of the microphone," and Welti & Devantier formalize Mean Spatial Variance (MSV) as the variance of SPL in dB across typically 4–6 seats, averaged 20–80 Hz. (3) Peaks (resonances) are minimum-phase and EQ-correctable; narrow dips are usually non-minimum-phase (SBIR, modal cancellation, comb filtering) and should never be boosted — Toole's "avoid filling narrow dips" rule and Mulcahy's excess-group-delay test in REW are the canonical discriminators. (4) Detection of resonances depends on Q counter-intuitively: lower-Q (broader) features are MORE audible per dB than higher-Q (narrower) ones (Toole & Olive 1988: ~0.25 dB threshold at Q=1, 5 kHz, pink noise), and changes in modal Q stop being audible above Q ≈ 16 (Avis/Fazenda/Davies 2007). This justifies aggressive correction of broad, low-Q peaks and conservative or no correction of high-Q narrow notches. (5) For ~5-position phone-based measurement, the practically achievable confidence is *medium*: enough to identify modal peaks under fs, set a gentle bass tilt, and avoid most foot-guns; not enough to drive FIR phase correction or perform real Sound Field Management. Single-position fallback is acceptable for tonal balance and broad peaks but must explicitly forbid narrow notch-filling and assertive correction above the modal range.

This report proposes (a) a deterministic confidence schema with per-band and per-filter confidence; (b) science-based, conservative thresholds and pseudocode; (c) a measurement-bundle persistence format suitable for replay by future LLM assistants or FIR back-ends; (d) clear gating from "single-position safe" through "multi-position balanced" to "many-position assertive/FIR" modes.

---

## 1. Findings from Acoustics Literature and Prior Art

### 1.1 Modal vs. statistical regimes
- **Schroeder frequency** `fs = 2000·√(T60/V)` marks the threshold of 3-fold modal overlap (Schroeder 1962; Skålevik "Schroeder Frequency Revisited"). For a typical 30–50 m³ living room with T60 ≈ 0.3–0.5 s, fs falls between roughly 150 and 250 Hz.
- The **transition zone fs … 4·fs** is recognized in practice as the region where both modal and ray models partially apply; Treble Technologies' docs default the transition to 4·fs.
- **Toole** (*Sound Reproduction*; "Measurement and Calibration of Sound Reproducing Systems," JAES 2015, open access) is explicit: "find prominent spectral peaks below about 500 Hz and attenuate them … Avoid filling narrow dips. They are not as audible as they are visible." This sets JTS's primary correction window.

### 1.2 Multi-position averaging and seat-to-seat variance
- **Sean Olive** (*Audio Musings*, Nov 2009): "we did spatial-averages over 6 microphone positions to avoid equalizing spectral artifacts that are very localized to the position of the microphone." Two calibration variants were compared in the Harman study: a multipoint 6-seat average and a 6-mic spatial average focused on the primary listening seat.
- **Welti & Devantier 2006** ("Low-Frequency Optimization Using Multiple Subwoofers," JAES 54(5)): defines **Mean Spatial Variance (MSV)**: "The variance of the sound level in dB as a function of the seating location (typically four to six seats) is calculated for each frequency, and from this the mean variance is calculated. … typically 20 to 80 Hz."
  - 4 subwoofers at wall midpoints (configuration 11) was the best practical configuration in terms of MSV. 2 subwoofers at opposing wall midpoints (configuration 6) was nearly as good. Optimized 4-sub configurations reduced MSV by ~16.6 dB² vs. single-sub reference.
  - Without bass management, single-sub configurations show "seat-to-seat variations of 40 dB or more at some frequencies" and "the system cannot be equalized effectively."
- **MMM (Moving Microphone Method)**, formalized by Jean-Luc Ohl, is functionally equivalent to many-position RMS averaging and is supported in REW's RTA. Useful as a sanity check on the area average, especially below ~300 Hz. The trade-off: noise from microphone motion contaminates above ~1 kHz unless motion is very slow.

### 1.3 Averaging math
**REW** distinguishes:
- **Vector average** — complex average of impulse responses; requires time alignment; tends to cancel reflections and approximates direct-sound response. Only appropriate when measurements are from the same point or carefully aligned.
- **RMS average** — averages magnitude on a linear power scale (`√(Σ|H|²/N)`), no phase; the correct method for combining truly independent listening positions.
- **dB average** — arithmetic mean of log magnitude; biased toward dips because dB is logarithmic; not recommended for room-correction averaging.
- **RMS + phase avg / dB + phase avg** — keep magnitude as RMS/dB but average phase by vector method; useful when both magnitude and phase are needed for downstream FIR work.

Practical rule, codified by REW docs, the rePhase tutorials, and HouseCurve: **use RMS magnitude averaging across truly different positions; reserve vector averaging for same-position repeats or near-coincident time-aligned points.**

### 1.4 Minimum-phase, narrow dips, and what to correct
- **John Mulcahy / REW Help**: "Room responses are mixed phase. … Anywhere the excess group delay plot is flat is a minimum phase region. … At 110 Hz, where there is a sharp dip in the response, there is a sharp peak in the excess group delay. Attempting to EQ the response to flat in this region would be foolish. … Low frequency peaks on the other hand are usually in minimum phase regions, which bodes well for attempts to apply EQ to them."
- **REW**: "Narrow bandwidth EQ adjustments should not be used outside the modal range."
- Multiple practitioner and engineering sources (callensaudiolabs, demaudio/LinFIR docs, BMC AV) converge on the canonical SBIR/cancellation rule: "Deep bass nulls caused by destructive interference cannot be meaningfully corrected. Boosting the signal at that frequency only increases the direct and reflected energy equally — the cancellation remains."

### 1.5 Detection thresholds (audibility)
- **Toole & Olive 1988**: at Q=1, 5 kHz, in pink noise, the just-detectable resonance threshold is **~0.25 dB**; with less revealing program material the threshold rises by ~5× (~1.25 dB).
- **Olive, Schuck, Sally, Bonneville 1997** ("The Detection Thresholds of Resonances at Low Frequencies," JAES 45(3)): 70.7% UDTR thresholds depend on Q, center frequency, and signal in non-monotonic ways. Qualitative conclusion: low-Q (broader) resonances have lower (more sensitive) dB thresholds than high-Q ones.
- **Avis, Fazenda & Davies 2007** ("Thresholds of Detection for Changes to the Q-Factor of Low-Frequency Modes in Listening Environments," JAES 55(7/8), pp. 611–622, U. Salford / U. Huddersfield), verbatim: "A threshold value of Q = 16 is suggested, below which further changes are unlikely to be detected." (Note the abstract phrases the threshold from the opposite direction; the operative datum is Q ≈ 16 as a soft audibility ceiling.)
- **Toole's qualitative summary**: "our sensitivity to the timbral changes was very much dependent on the Q, or bandwidth of the phenomenon — with much lower 'thresholds' being found for wider bandwidth spectral changes."

### 1.6 Coherence and SNR as data-quality indicators
- **Merlijn van Veen** ("Coherence and Reverberation," merlijnvanveen.nl), verbatim: "From this chart we can conclude that 10 dB of SNR should suffice for approximately 95% coherence." Meyer Sound's M-Noise procedure independently reports 91% on SIM and 95% on Smaart at 10 dB SNR.
- **Rational Acoustics Smaart docs**: coherence "uses the averaging buffer to show how stable/consistent your transfer function data is." Industry practice is to **blank bins below ~70 %** coherence and refuse to draw EQ decisions from them.
- **HouseCurve docs**: "HouseCurve allocates filters to regions with the largest deviation from the target curve, preferring lower frequencies and ignoring areas with low coherence (SNR)." This is the most directly portable quality metric for JTS: gate filter generation by per-bin coherence (or its sweep-deconvolution analog, the per-bin SNR estimated from pre-sweep silence and noise floor).

### 1.7 Time-variance of room measurements
**Prawda, Schlecht & Välimäki 2024**, "Short-time coherence between repeated room impulse response measurements," J. Acoust. Soc. Am. 156(2): 1017–1028, DOI 10.1121/10.0028172, verbatim: "Room impulse responses (RIRs) vary over time due to fluctuations in atmospheric temperature, humidity, and pressure." Coherence between repeated measurements decays exponentially with time and frequency: in their data the 20-kHz coherence fell by a third within 1 s, while 1–2 kHz coherence remained largely stable. **Implication for JTS**: high-frequency correction at fine resolution is intrinsically unstable across sessions; mid-frequency and lower data is reproducible enough to act on. Re-measurement is justified after large changes in room conditions (HVAC, furniture, season).

### 1.8 Prior-art summary by product

| Product | Mic / array | Positions | Averaging | Correction range | Confirmed vs. marketing | Notable confidence behavior |
|---|---|---|---|---|---|---|
| **REW** (John Mulcahy) | Any (UMIK-1/2 preferred) | User chooses | Vector / RMS / dB / RMS+phase, all documented | Both regimes; user judgment | Fully documented; open for measurement | Min-phase/excess-GD test exposed to user; FDW (15-cycle convention) for psychoacoustic smoothing |
| **Dirac Live** | UMIK-1 / proprietary | "Focused" 9 / "Wide" 13 / "Expanded" 17 (per SoundStage! Simplifi) | Proprietary mixed-phase FIR; multi-position weighted | Both, stronger LF focus | Public: center position is the sweet spot, other positions bias the correction across the volume (Dirac helpdesk, miniDSP forum). Private: optimizer math, weighting. | Refuses (in practice) to brute-force fill deep bass nulls; FIR phase correction; Dirac Bass Control / ART for multi-sub |
| **Audyssey MultEQ XT32** | Proprietary | 8 (3 around MLP, +5) | Per Chris Kyriakakis on Ask Audyssey: "Audyssey does *not* use averaging. … we use a non-linear way to combine the measurements based on the severity of the problems found" | Both | Documented: 8 positions, non-linear severity-weighted combine. "32" = filter resolution, not positions | Penalizes worst-position problems; pro variant supports 32 positions |
| **Trinnov Optimizer** | Proprietary tetrahedral 3D mic (4 capsules) | Single or multi | Time + frequency joint optimization with 3D speaker localization | Both, full-range time alignment | Confirmed: tetrahedral mic, ±0.1 dB cal 20 Hz–24 kHz, 2°/2°/1 cm localization. Optimizer math: proprietary | 3D info separates direct/reflected; multi-position "wider sweet spot" mode |
| **Genelec GLM / AutoCal 2** | Calibrated reference mic | 1, 3, or multi (per system) | Cloud-based using database of measurements | Both | Confirmed: network adapter + per-monitor DSP. Marketing: "more precise in less time"; AutoCal algorithm secret | GRADE Room Acoustic Report exposes seat-to-seat and reverb metrics |
| **Sonarworks SoundID Reference** | Proprietary or third-party measurement mic | 37 measurements per support docs: "Room Response: A series of 37 measurements of your room and speakers" | Multi-point with documented guided sequence | Both, full-range | Position pattern documented in support docs; correction math proprietary | Automated guided sequence; multi-position calibration for asymmetric rooms (marketing) |
| **Lyngdorf RoomPerfect** | Proprietary | 1 Focus + ≥5 random "Room" | Power-response averaging; separates "speaker character" from "room" via Focus measurement | Both; deliberately avoids forcing linear target | Confirmed by SoundStage! Hi-Fi review of TDAI-1120 (R. Kanno): "After I'd taken measurements at five mike positions, the system indicated that it had 96% RoomKnowledge (Lyngdorf recommends a RoomKnowledge level of 95% or higher). Two more measurements at two different positions yielded an increase of only 1%, so I stopped there." | Exposes a confidence-like "RoomKnowledge %" — JTS's closest UX prior art |
| **Acourate** (Brüggemann) | Any | Typically 1, user-managed | Macro-driven; FDW from 3/3 to 15/15 cycles | Both; deep FIR | Fully documented for experts | Excess-phase correction and FDW exposed; bass pre-filter and ICPA add-ons |
| **DRC-FIR** (Sbragion) | Any | Typically 1 listening position | Pulse-response inversion with FDW; FIR output | Both | Open source, GPL, detailed math | Sbragion: "as a rough estimation … no more than 2 % to 3 % of the whole response gets corrected" — sets the right humility |
| **Multi-Sub Optimizer (MSO)** | Any (REW measurements) | Per-sub-to-seat TFs | Numerical optimization of per-sub PEQ/delay/gain to minimize seat-to-seat variance | Strictly modal (subs only) | Open source, Andy Carlson; explicit math | Directly implements Welti SFM-style optimization for DIY users |
| **HouseCurve** | iPhone/iPad internal mic (compensated) or UMIK-1 | 3–7 typical (3–5 desk, 3–7 living room) | dB avg magnitude, vector avg phase, arithmetic avg group delay | Both | Documented in user manual | Explicit coherence-blanking threshold — directly relevant to JTS's "don't EQ where SNR is bad" |

---

## 2. Practical Recommendations for JTS

### 2.1 Default workflow (stereo pair)
1. **Pre-flight checks** (per channel): SPL of sweep, noise-floor estimate from 1 s pre-sweep silence, clipping detector (peak < −1 dBFS on capture), THD floor from sweep deconvolution residual. Refuse to proceed if broadband SNR < 25 dB at the mic or peak > −1 dBFS.
2. **Mic identification**: read UMIK-1/2 serial, fetch or load calibration file; otherwise read iMM-6/6C/UMM-6 cal; if uncalibrated phone mic, set `mic_class = "uncalibrated_phone"` and globally cap correction to ≤ 200 Hz.
3. **5-position default sequence** for a stereo pair (per channel measured separately):
   - P1: Main listening position (MLP), ear height (target 95–110 cm seated).
   - P2: MLP + 30 cm left at ear height.
   - P3: MLP + 30 cm right at ear height.
   - P4: MLP + 30 cm forward.
   - P5: MLP + 30 cm back, or a secondary seat if there is one.
   - All mic-up (0° cal file). Reject if any P2–P5 differs from P1 by > 6 dB in broadband SPL (likely mic was moved or covered).
4. **Per-channel processing**: deconvolve, window IR to ~1.0 s, RMS-magnitude-average the 5 sweeps in linear power, retain individual per-position magnitude curves for variance analysis.
5. **Variance analysis (deterministic)**: at every ERB/Bark bin compute mean μ(f), standard deviation σ(f), and (max − min) across positions. This drives per-band confidence (Section 3).
6. **Excess-group-delay surrogate**: REW-style; flag bins where the inferred minimum-phase magnitude and the measured magnitude diverge by > 6 dB as "non-minimum-phase, do not boost."
7. **Filter design**: cuts-first, bounded Q (≤ 6 default, ≤ 10 in "assertive" mode), bounded gain (cut up to 9 dB; boost ≤ 3 dB and only when σ(f) < 1.5 dB across positions AND f < fs AND the feature is a peak below target by > 3 dB).
8. **Audit and write-back to CamillaDSP**: design audit must pass (Section 4 pseudocode); on fail, downgrade strategy or refuse and ask for re-measurement.

### 2.2 Mono speaker fallback
Treat as a single-channel JTS instance. Same 5-position default. Stereo cancellation diagnostics are skipped (no "L − R imbalance at MLP" check). Cross-position variance analysis still applies. Because a lone speaker has no stereo phantom-center to lose, JTS may safely allow slightly higher Q on cuts (up to 8) and a small bass shelf (≤ +3 dB below 80 Hz) provided σ(f) < 2 dB across positions. No FIR phase tricks are warranted from 5 mono positions.

### 2.3 Single-position fallback
With one good measurement at the MLP, JTS can confidently do:
- A broad target-curve tilt (e.g., −1 dB/oct house curve from ~200 Hz to 10 kHz).
- Cut-only PEQ on **broad, low-Q peaks below fs** that are ≥ 3 dB above the smoothed target, with Q ≤ 3 and conservative gain (≤ 6 dB).
- A high-shelf cut if measured response above 8 kHz is > 4 dB hot vs. target (valid only with a calibrated mic pointed up using 0° cal).

JTS cannot, from one position, safely do:
- Any boost (single-position dips are almost always SBIR/LBIR/modal nulls; boosting wastes headroom and worsens off-MLP seats).
- Narrow notch filters above 200 Hz.
- FIR phase work.

The confidence label reported to the user must read: "Single-position: limited modal correction only."

### 2.4 If user opts in to many positions (9-point grid, MMM, denser)
Unlocks:
- **Tighter MSV estimate** (4–6+ seats approximates Welti's working definition) → can identify common modal peaks that survive across positions and reject seat-local artifacts more aggressively.
- **Optional modal identification** by fitting Lorentzian/biquad poles to the averaged low-frequency response; report modal frequencies, Qs, and inferred per-mode T60.
- **FIR-style work**: with ≥ 9 positions and time-aligned sweeps (acoustic timing reference), JTS may compute a vector average for the low-frequency / direct-sound portion and design a mixed-phase FIR for f < 300 Hz. Above 300 Hz, FIR must default to linear-phase magnitude only, with FDW (15-cycle Mulcahy/Acourate convention).
- **MMM** is accepted as a single-position-replacement spatial average for bass only (< 300 Hz). It must not be used as the sole input for any mid/high band correction because mic-motion noise contaminates above ~1 kHz unless motion is very slow.

### 2.5 Pros and cons of low vs. high position counts
- **1 position**: fastest, sufficient for sanity; cannot distinguish modal from local; must forbid boosts and narrow filters.
- **3–5 positions**: JTS default; reliably separates modal peaks from seat-local artifacts in the bass; supports a coarse confidence map. Lyngdorf's documented experience is that ~5 random positions plus the Focus reach ≥ 95 % "RoomKnowledge."
- **9–17 positions (Dirac Focused/Wide/Expanded; Audyssey 8-point; RoomPerfect denser)**: better statistical separation; allows a broader correction window (up to ~500 Hz with confidence); approaches MSV-style optimization if multiple speakers/subs.
- **20+ positions / MMM / Sonarworks 37-point**: enables tight modal identification, FIR mixed-phase work, multi-sub optimization. Time cost is high and the marginal information drops past ~9 positions, consistent with Dirac and Lyngdorf practitioner reports.

---

## 3. Proposed Confidence Schema (Data Structures)

All times, frequencies, levels, and sample rates are explicit. Format is JSON-compatible; binary IRs are stored as separate WAV/FLAC.

```jsonc
{
  "bundle_version": "1.0",
  "created_utc": "2026-05-26T18:00:00Z",
  "speaker": {
    "form_factor": "stereo_pair",     // or "mono"
    "channels": ["L","R"],
    "fs_estimate_hz": 175,            // Schroeder estimate
    "rt60_seconds_estimate": 0.45,
    "volume_m3_estimate": 38          // optional, user-supplied
  },
  "mic": {
    "model": "UMIK-1",
    "serial": "7000xxxx",
    "calibration_file_sha256": "...",
    "class": "calibrated_external"    // or "calibrated_phone","uncalibrated_phone"
  },
  "measurement": {
    "sweep_seconds": 8,
    "sample_rate_hz": 48000,
    "start_freq_hz": 18,
    "end_freq_hz": 22000,
    "pre_silence_seconds": 1.0,
    "post_silence_seconds": 2.0,
    "positions": [
      {
        "id": "P1",
        "role": "MLP",
        "x_y_z_m_estimated": [0.0, 0.0, 1.05],
        "channel": "L",
        "noise_floor_dbfs": -78.0,
        "peak_dbfs": -6.2,
        "broadband_snr_db": 42.1,
        "clipping_detected": false,
        "ambient_event_detected": false,
        "ir_path": "ir/P1_L.wav",
        "thd_residual_db": -45.0
      }
      // ... P1_R, P2_L, P2_R ...
    ],
    "averaging": {
      "method": "rms_magnitude_linear_power",
      "phase_method": "discard_above_fs_minimum_phase_below",
      "smoothing": "psychoacoustic_15cycle_fdw"
    }
  },
  "per_band_confidence": [
    {
      "f_low_hz": 20, "f_high_hz": 80,
      "regime": "modal",
      "mean_db": 5.2, "sigma_db": 1.1, "max_minus_min_db": 3.3,
      "snr_db_min": 28.0, "min_phase_likely": true,
      "confidence": "high",
      "recommend_correct": true,
      "rationale": "Repeatable across positions (sigma < 1.5 dB), minimum-phase, in modal region; safe to cut peaks."
    },
    {
      "f_low_hz": 80, "f_high_hz": 300,
      "regime": "transition",
      "sigma_db": 4.2,
      "confidence": "medium",
      "recommend_correct": true,
      "narrow_features_recommend_correct": false,
      "rationale": "Higher variance; correct only broad (Q<3) peaks >=3 dB above target."
    },
    {
      "f_low_hz": 300, "f_high_hz": 1000,
      "regime": "statistical",
      "sigma_db": 6.7,
      "confidence": "low",
      "recommend_correct": false,
      "rationale": "Seat-to-seat variance >=5 dB; correction would chase local artifacts."
    }
  ],
  "filters": [
    {
      "type": "peaking_eq",
      "channel": "L",
      "fc_hz": 62, "gain_db": -5.4, "Q": 4.0,
      "rationale": "Common modal peak across 5 positions; sigma=0.8 dB at fc; min-phase; below fs.",
      "confidence": "high",
      "feature_id": "M1"
    }
  ],
  "design_audit": {
    "total_boost_db": 0.0,
    "max_cut_db": 6.1,
    "filter_overlap_warnings": [],
    "post_correction_predicted_response_path": "predict/post_corr.png",
    "passes": true
  },
  "global_confidence": {
    "level": "medium",                          // low | medium | high
    "allowed_strategies": ["safe","balanced"],  // gates "assertive" off
    "warnings": [
      "Mic was uncalibrated phone above 8 kHz; HF correction disabled.",
      "Position P3 SNR low at 50 Hz; that band weight reduced."
    ],
    "remeasure_triggers": []
  },
  "remeasure_triggers_definition": [
    "ambient_event_detected_in_any_position",
    "broadband_snr_db < 25 in any position",
    "max_minus_min_db > 12 in any band below fs (suggests one mic position was bad)",
    "design_audit.passes == false after downgrade"
  ]
}
```

The bundle should also persist the raw recordings (WAV) and the deconvolved IRs (WAV/FLAC) so that:
- An LLM assistant can explain decisions verbatim from the JSON.
- A future FIR back-end can re-derive filters without re-measuring.
- Users can A/B retrospective strategies.

---

## 4. Proposed Deterministic Algorithms (Pseudocode)

### 4.1 Per-position quality gate
```python
def position_quality(ir, pre_silence, sample_rate):
    rms_signal = rms(ir.windowed(1.0))               # 1 s window
    rms_noise  = rms(pre_silence)
    snr_db     = 20*log10(rms_signal / rms_noise)
    peak_dbfs  = 20*log10(max(abs(ir.raw)))
    clipping   = peak_dbfs > -1.0
    thd_db     = sweep_residual_estimate(ir)         # nonharmonic in deconv result
    return PositionQ(snr_db, peak_dbfs, clipping, thd_db)

def position_acceptable(pq):
    return pq.snr_db >= 25 and not pq.clipping and pq.thd_db < -35
```

### 4.2 Spatial averaging
```python
def rms_magnitude_average(magnitude_curves_db):
    # magnitude_curves_db: [N_positions, N_freqs] dB SPL
    lin = 10 ** (magnitude_curves_db / 20.0)
    power = lin ** 2
    mean_power = power.mean(axis=0)
    return 20 * log10(sqrt(mean_power))

def per_band_stats(magnitude_curves_db, bands):
    out = []
    for (flo, fhi) in bands:
        sel = magnitude_curves_db[:, freq_index(flo): freq_index(fhi)]
        mean_db = rms_magnitude_average(sel).mean()
        sigma_db = sel.std(axis=0).mean()
        spread_db = (sel.max(axis=0) - sel.min(axis=0)).mean()
        out.append(BandStats(flo, fhi, mean_db, sigma_db, spread_db))
    return out
```

### 4.3 Minimum-phase / SBIR-null detector
```python
def is_minimum_phase_region(measured_mag_db, measured_phase, f_grid):
    min_phase = hilbert_min_phase_from_mag(measured_mag_db)
    excess_gd = group_delay(measured_phase) - group_delay(min_phase.phase)
    flag = (abs(excess_gd) > THRESHOLD_GD) | \
           (abs(measured_mag_db - min_phase.db) > 6)
    return ~flag
```

### 4.4 Feature classifier
```python
def classify_feature(fc, gain_db, Q, sigma_db_at_fc, regime, min_phase_ok):
    # gain_db = deviation from smoothed target at fc; + means peak
    if regime == "statistical":
        return "ignore"
    if not min_phase_ok and gain_db < 0:
        return "do_not_boost"             # SBIR / cancellation null
    if sigma_db_at_fc > 4 and gain_db > 0:
        return "seat_local_peak"          # don't EQ; report only
    if gain_db > 3 and sigma_db_at_fc < 2 \
       and regime in ("modal","transition") and Q < 10:
        return "correct_cut"
    if gain_db < -3 and sigma_db_at_fc < 1.5 \
       and regime == "modal" and Q < 3 and min_phase_ok:
        return "small_boost_allowed"
    return "leave_alone"
```

### 4.5 Confidence aggregation
```python
def band_confidence(stats, snr_db_min, min_phase_ok, regime, n_positions):
    if snr_db_min < 20:        return "low"
    if regime == "statistical":
        return "medium" if n_positions >= 5 else "low"
    if stats.sigma_db > 4:     return "low"
    if stats.sigma_db > 2:     return "medium"
    if not min_phase_ok:       return "medium"
    return "high"

def global_strategy_gate(per_band_confs, n_positions, mic_class):
    if any(c == "low" for c in per_band_confs_below_fs):
        return ["safe"]
    if mic_class == "uncalibrated_phone":
        return ["safe","balanced"]          # never assertive without cal mic
    if n_positions < 5:
        return ["safe","balanced"]
    return ["safe","balanced","assertive"]
```

### 4.6 Design audit
```python
def audit(filters, target, positions, snr_floor):
    sum_boost = sum(max(0, f.gain_db) for f in filters)
    sum_cut   = sum(max(0,-f.gain_db) for f in filters)
    if sum_boost > 6:        return Fail("Too much aggregate boost")
    if any(f.Q > 10 for f in filters): return Fail("Q exceeds 10")
    for f in filters:
        if f.gain_db > 0 and f.fc_hz > fs_hz:
            return Fail("Boost above Schroeder")
        if f.gain_db > 0 and not stable_across_positions(f.fc_hz, positions):
            return Fail("Boost where seat variance is high")
    predicted = apply(filters, mean_response_db)
    if any(predicted - target > 2 for f in band(20, fs_hz)):
        return Fail("Under-correction in modal band — try one more pass")
    return Pass()
```

---

## 5. Suggested Thresholds with Rationale

| Parameter | Default | Rationale / source |
|---|---|---|
| Schroeder fs | computed; fallback 200 Hz | Schroeder formula; Toole's ~500 Hz upper EQ bound caps further correction |
| Modal-band σ(f) → "high confidence" | < 1.5 dB | Well below mid-band JNDs; gives margin over Toole & Olive's 0.25 dB Q=1 floor |
| σ(f) → "medium confidence" | 1.5 – 3 dB | Tolerable for cuts, no boosts |
| σ(f) → "low confidence", no EQ | > 4 dB | Seat-local; Olive's spatial-averaging rationale: avoid equalizing localized artifacts (HEURISTIC — no primary-source dB threshold) |
| Spread (max−min) per band, single-position trust | < 6 dB | Beyond this, multi-position required |
| Min broadband SNR | 25 dB | ~95 % coherence per Van Veen/Smaart; safety margin over the ≥10 dB minimum |
| Per-bin coherence-blanking threshold | 70 % | Industry convention (Smaart, HouseCurve) |
| Max cut gain (safe) | −6 dB | Conservative; preserves headroom |
| Max cut gain (balanced) | −9 dB | Matches REW/Dirac practitioner guidance |
| Max cut gain (assertive) | −12 dB | Only with ≥ 8 positions and σ < 1.5 dB |
| Max boost gain | +3 dB | Only below fs, σ < 1.5 dB, min-phase, Q ≤ 3, ≥ 5 positions |
| Max Q (safe/balanced) | 6 | Avis/Fazenda/Davies threshold near Q=16; stay well under, leaving user-side margin |
| Max Q (assertive, cuts only) | 10 | Mulcahy/REW caution: narrow EQ corrupts off-axis seats |
| Min peak width to correct | ≥ 1/12 octave | Narrower features are usually mic-position artifacts |
| Min peak height to correct (above smoothed target) | 3 dB | Psychoacoustically meaningful margin; matches Toole's "prominent peaks" |
| Lowest frequency to correct | max(15 Hz, speaker f3 − ½ octave) | Avoid driving cone below f3 |
| Highest frequency for narrow EQ | min(fs, 400 Hz) | REW: "narrow EQ … not outside modal range" |
| Highest frequency for any EQ (calibrated mic) | 10 kHz with broad shelf only | Toole: above ~500 Hz prefer broad/shelf, not narrow |
| Highest frequency for any EQ (uncalibrated phone) | 200 Hz | Reflects mic uncertainty |
| Minimum positions for boost | 5 | Reliability of "modal not local" judgement |
| Minimum positions for FIR phase work | 9 | Matches Dirac and Audyssey conventions |
| Excess-group-delay flag | "non-min-phase" if local \|excess_gd\| > 5 ms or min-phase magnitude vs. measured magnitude diverges > 6 dB | REW excess-GD methodology |
| Re-measure trigger: SNR | < 25 dB any band below fs in any position | Data quality |
| Re-measure trigger: ambient | LF transients > 6 dB above noise floor in pre-silence | Dryer/door/HVAC |
| Re-measure trigger: σ(f) | > 12 dB in any modal band | One position likely bad |

---

## 6. Risks and Edge Cases

- **Speaker placement problems disguised as room problems.** A speaker placed 0.4–0.8 m from the front wall has an SBIR null around 100–200 Hz that is not a room mode. EQ cannot fix it; JTS should *report* the null and recommend speaker repositioning rather than silently boosting.
- **Pair-cancellation in stereo bass.** Mono content from a stereo pair can null acoustically between the speakers around 80–120 Hz. Detect by comparing L-only, R-only, and stereo measurements at the MLP; if a deep null appears only in the stereo sum, label it "geometric cancellation, not EQ-correctable."
- **Phone-mic non-linearities.** Uncalibrated internal mics have AGC, limited dynamic range, and HF rolloff variation. Force "phone-mic mode" with HF correction disabled above 2 kHz unless an external calibrated mic is detected.
- **Bluetooth/AirPlay loop in capture path.** Wireless monitoring breaks deconvolution due to variable latency; JTS must detect an unstable round-trip and refuse.
- **Mode shifts with humidity/temperature.** Per Prawda, Schlecht & Välimäki 2024 (JASA 156(2)), modal-frequency drift of tens of cents over an hour and high-frequency coherence drops are observable even in one session. JTS should warn after ~3 months and re-measure on furniture changes.
- **Subwoofer integration in mono speakers with an external sub.** If a sub is present, JTS should *not* attempt SFM-style multi-sub optimization with ≤ 5 positions; the Welti result requires per-sub-to-seat transfer functions. Recommend the user run Multi-Sub Optimizer with REW exports if they want that.
- **Asymmetric rooms.** Treating L and R independently rather than as a stereo pair generally helps in asymmetric rooms; this is JTS's default and should be retained even when the same target curve is applied.
- **Boost-induced clipping** under correction. The audit must compute aggregate boost and reserve headroom; CamillaDSP's gain stage must reflect the worst-case boost across all bands plus a 3 dB safety margin.
- **User reports it sounds "thin."** Usually means the in-room target was too flat (no Harman-style downward tilt). The schema exposes target choice and allows a −1 dB/oct house curve from ~200 Hz.

---

## 7. Sources

- Floyd Toole, *Sound Reproduction* (4th ed.); "The Measurement and Calibration of Sound Reproducing Systems," JAES 2015 (open access via AES).
- Sean Olive, *Audio Musings* (2009), "The Subjective and Objective Evaluation of Room Correction Products" (seanolive.blogspot.com).
- T. Welti, A. Devantier, "Low-Frequency Optimization Using Multiple Subwoofers," JAES 54(5), 2006 (audioroundtable.com/misc/Welti_Multisub.pdf); Welti, "How Many Subwoofers are Enough?" AES Preprint 5602, 2002.
- S. Olive, P. Schuck, S. Sally, M. Bonneville, "The Detection Thresholds of Resonances at Low Frequencies," JAES 45(3), 1997.
- Toole & Olive, "The Modification of Timbre by Resonances: Perception and Measurement," JAES 1988.
- D. Avis, B. Fazenda, W. Davies, "Thresholds of Detection for Changes to the Q-Factor of Low-Frequency Modes in Listening Environments," JAES 55(7/8), pp. 611–622, July 2007.
- M. Schroeder, "The 'Schroeder frequency' revisited"; M. Skålevik, "Schroeder Frequency Revisited" (akutek.info).
- J. Mulcahy, REW Help (roomeqwizard.com/help): All SPL Graph, Minimum Phase, Group Delay, FDW.
- A. Goertz et al., "Optimization of Sound Reproduction in Listening Rooms" (Klein+Hummel, 2001); A. Celestinos, S. Nielsen, "Controlled Acoustic Bass System (CABS)," JAES 56(11), 2008.
- D. Sbragion, DRC-FIR documentation (drc-fir.sourceforge.net).
- U. Brüggemann, Acourate manuals and tutorials (audiovero.de).
- A. Carlson, Multi-Sub Optimizer (andyc.diy-audio-engineering.org/mso).
- Lyngdorf RoomPerfect technical pages; SoundStage! Hi-Fi TDAI-1120 review (R. Kanno).
- Genelec GLM 5 System Operating Manual; AutoCal 2 product pages.
- Trinnov "Room Correction Explained" and 3D Microphone technical pages.
- Sonarworks SoundID Reference Support — "Setting up with SoundID Reference on speakers" (37-measurement procedure).
- Audyssey MultEQ-X User Guide; Chris Kyriakakis Q&A on Ask Audyssey (non-linear weighting confirmation).
- Dirac Live Helpdesk; SoundStage! Simplifi review of Dirac Live Room Correction Suite (Focused/Wide/Expanded position counts).
- HouseCurve documentation (housecurve.com/docs).
- M. van Veen, "Coherence and Reverberation" (merlijnvanveen.nl); Rational Acoustics Smaart documentation.
- K. Prawda, S. J. Schlecht, V. Välimäki, "Short-time coherence between repeated room impulse response measurements," J. Acoust. Soc. Am. 156(2), 1017–1028 (2024), DOI 10.1121/10.0028172.
- J.-L. Ohl, "MMM Moving Mic Measurement" (ohl.to/audio/downloads/MMM-moving-mic-measurement.pdf).

---

## Confidence Labels Summary

- **High confidence** (well-sourced primary literature):
  - Modal/diffuse divide and Schroeder formula.
  - Welti & Devantier MSV definition, 4-subs-at-wall-midpoints result, "40 dB seat variation is unequalizable" finding.
  - Olive's 6-position spatial-averaging rationale (verbatim).
  - Toole's "avoid filling narrow dips" and "peaks below ~500 Hz are correctable."
  - REW's minimum-phase / excess-group-delay methodology and FDW behavior.
  - Detection threshold ~0.25 dB at Q=1, 5 kHz, pink noise (Toole & Olive 1988); audibility increases with low Q.
  - Coherence ≈ 95 % at ~10 dB SNR in Smaart (Van Veen).
  - Lyngdorf's "≈ 96 % RoomKnowledge from 5 random positions" (SoundStage Hi-Fi).
  - Sonarworks "37 measurements" position count (vendor docs).
  - Audyssey "non-linear severity-weighted, not averaging" (Kyriakakis Q&A).
  - Dirac Focused/Wide/Expanded = 9/13/17 positions (SoundStage Simplifi).

- **Medium confidence**:
  - Numeric per-position thresholds proposed here (σ ≤ 1.5 dB, max-boost +3 dB, etc.). Engineering choices consistent with the literature but not numerically endorsed by any single paper.
  - Q ≤ 6 (safe) / ≤ 10 (assertive), chosen from Avis/Fazenda/Davies Q ≈ 16 audibility ceiling plus Mulcahy practitioner warnings.
  - The 4·fs transition-zone upper bound: practitioner convention with partial primary-source support.

- **Unresolved / heuristic**:
  - There is no primary-source numerical "seat-to-seat dB threshold above which not to EQ." Our σ > 4 dB rule is heuristic.
  - Audyssey's exact weighting math beyond Kyriakakis's statement is proprietary.
  - Dirac's exact mixed-phase optimization across positions is undocumented in public; we rely on Sound on Sound's qualitative description and Dirac's forum statements.
  - Trinnov's Optimizer math and Genelec AutoCal 2's cloud algorithm are proprietary and treated as marketing claims unless confirmed by the documented mic hardware specifications.