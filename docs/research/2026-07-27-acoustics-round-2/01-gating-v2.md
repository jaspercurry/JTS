# Robust Acoustic Gating for the JTS Speaker-Measurement Instrument: A Prior-Art Survey and Recommended Gating-v2 Design

> Verbatim deep-research result, pasted by the owner into the round-2
> coordinator session 2026-07-27/28. Prompt: DEEP-RESEARCH-PROMPT-gating.md
> (same directory). Synthesis target: issue #1790 work order.

## TL;DR
- **The cloud_04 event was almost certainly a detector false-positive, not a real surface, and the fix is a three-part gating-v2:** (1) replace the single-threshold "first peak after direct" picker with an AIC/matched-filter picker gated by a repeatability-and-consistency test; (2) replace the fragile group-max validity floor with per-position masked averaging plus a minimum-contributing-positions rule; and (3) add an explicit anomaly→action policy (bounded auto-retake, else exclude-with-disclosure) so no single collapsed gate silently poisons a session.
- **The `f_valid = 1/T` model is defensible as an *edge* but optimistic as a *floor of trustworthiness*.** The literature supports treating 1/T as roughly the "≈2 dB error" boundary, with the response only trustworthy near 2/T; keep 1/T as the hard floor but add a graded ±uncertainty band between 1/T and ~2/T rather than a hard cliff.
- **Frequency-dependent (constant-cycles) gating is real and shipping (REW, Acourate, WiiM), but for an automated no-acoustician verdict we recommend a conservative two-band gate over full FDW,** because FDW's parameter-dependent smoothing and bass-level effects raise honesty risks that a single auditable band-split does not.

## Key Findings
1. **No shipped loudspeaker tool auto-classifies an early arrival as "real surface vs. artifact vs. source-fixed."** MLSSA, CLIO, ARTA, and REW all put a human at the cursor. The JTS instrument is unusual in needing to make that call automatically; the transferable science lives in the seismology/acoustic-emission arrival-picking literature (STA/LTA, AIC, kurtosis/HOS) and in echo-detection (cepstrum, matched filtering), not in audio product manuals.
2. **The discriminator you need already exists in your data structure.** A source-fixed arrival (horn-rim, ~0.3 ms) has a delay τ that is *invariant* across mic positions; a real room surface has a τ that *moves* across positions; a detector artifact is *non-repeatable* within a position. N-repeat + M-position variance of τ separates all three without any new hardware.
3. **The current group-max floor is the single most fragile design choice** and directly caused the incident. Per-position masking before power-averaging is strictly more honest and, above ~200 Hz, essentially free of bias — provided a minimum-contributing-positions rule prevents the LF edge from being computed from one or two outliers.
4. **`f_valid = 1/T` is the industry-standard rule of thumb** (REW, ARTA, MLSSA, CLIO, Struck & Temme) but it is an *edge*, not a floor of trustworthiness: at f·T = 1 the magnitude error is roughly a couple of dB for a system that rings past the gate, and the response is only "very close to true" near f·T ≈ 2.
5. **Frequency-dependent windowing is mature but has documented side effects** (bass-level loss, parameter-dependent smoothing) that are acceptable when a human tunes by ear but dangerous for an automatic pass/fail verdict.

## Details
### Q1. Robust first-reflection detection — state of the art and an implementable spec
**What shipped products actually do (all human-in-the-loop).**
- **MLSSA** (DRA Labs) measures directly in the time domain via cross-correlation and expects the operator to *place cursors* on the impulse response to select the reflection-free segment; its documentation states plainly that "in typical rooms, such windowed anechoic measurements are valid only down to about 200 Hz using any method," and it then splices a near-field bass measurement. There is no automatic reflection classifier.
- **ARTA** (Ivo Mateljan) likewise has the user set start/end gate markers on the IR; the Charlie Laub tutorial and ARTA manual describe manual identification of "the time-span where there are no room reflections."
- **CLIO** (Audiomatica) exposes "Gating" as a user setting; Joe D'Appolito's CLIO note and the CLIO manual treat gate placement as an operator choice and warn that windowing limits LF response (typically "below 200 to 300 Hz is not possible in reasonably sized rooms").
- **REW** (John Mulcahy) sets left/right IR windows automatically to *include* the room by default. Per the REW V5.20 Analysis Preferences documentation, REW retains "a 1 second period... before the peak, and by default a 1.7 second period... after the peak (this varies a little depending on the sample rate, at 44.1k (or multiples) it is approx 2 seconds, at 48k 1.7 seconds)" — i.e., its automation is aimed at the *room* response, not at auto-detecting the first reflection for quasi-anechoic gating; the user moves the window for gated measurements. Confidence: high for REW default behavior (documented in the REW help); medium for exact internal window-placement heuristics (inferred from help text and Mulcahy's forum posts).
- **Klippel NFS** sidesteps detection entirely: it uses a *double-layer near-field scan* and holographic field separation (spherical harmonics + Hankel functions) to separate outgoing (direct) from incoming (reflected) waves, explicitly because, in Klippel's own NFS manual, "the room reflections can be filtered out for high frequencies using time windowing. However, especially for low frequencies (below 1 kHz) the common windowing techniques are not applicable anymore. Also, room resonances appear which dominate the total sound pressure." This is the gold standard but requires special hardware and a fixed DUT — an anti-goal for JTS.

**Takeaway for JTS:** you cannot copy a shipped detector because none exists; you must build one, and the right source discipline is arrival-picking.

**The transferable science (arrival picking).** The seismology/AE literature has spent decades on exactly this problem — find the onset of a coherent arrival buried in noise, automatically. The main families:
- **STA/LTA (energy ratio):** ratio of short-term to long-term running energy. Efficient, but as summarized by Zhu & Beroza ("PhaseNet: a deep-neural-network-based seismic arrival-time picking method," *Geophysical Journal International* 216(1):261, 2019), the STA/LTA method "(Allen 1978)... is efficient, often effective, but susceptible to noise and has low accuracy for arrival times, particularly for shear waves." (Refs: R. V. Allen, "Automatic earthquake recognition and timing from single traces," *Bull. Seismol. Soc. Am.* 68(5):1521–1532, 1978; M. Baer & U. Kradolfer, "An automatic phase picker for local and teleseismic events," *Bull. Seismol. Soc. Am.* 77:1437–1445, 1987, which "improved the STA/LTA method using the envelope as characteristic function.") STA/LTA is essentially what your current "first peak after the direct sound" detector is — and its known failure mode (pulse-like noise / spike false-triggers) is exactly cloud_04.
- **AIC (Akaike Information Criterion) picker:** models the trace as two adjoining stationary processes (before/after onset) and picks the onset at the *global minimum* of the AIC function. The Maeda (1985) formulation, as adapted for acoustic emission by Kurz, Grosse & Reinhardt (2005), computes AIC directly from the waveform: `AIC(k) = k·log(var(x[1:k])) + (N−k−1)·log(var(x[k+1:N]))`, with the onset at `argmin_k AIC(k)`. Its decisive advantage for JTS: it "automatically returns the optimal phase arrival without threshold settings (Maeda 1985)" — no magic amplitude threshold to be fooled by a distortion spike. (Refs: J. H. Kurz, C. U. Grosse & H. W. Reinhardt, "Strategies for reliable automatic onset time picking of acoustic emissions and of ultrasound signals in concrete," *Ultrasonics* 43(7):538–546, 2005; N. Maeda, *Zisin* 38:365–379, 1985, universally cited via Kurz 2005 as the original text is in Japanese.)
- **Kurtosis / higher-order-statistics (HOS) pickers:** detect the onset as a jump in kurtosis (departure from Gaussianity); good for *emergent* signals and robust at low SNR (Saragiotis 2002; the KVP multiband-kurtosis picker, *GJI* 2025). Useful as a corroborating characteristic function.
- **Cepstral / homomorphic echo detection:** Bogert, Healy & Tukey (1963) introduced the cepstrum precisely to find echoes; an echo appears as a peak at a quefrency equal to its delay. Directly applicable to detecting a discrete reflection, but the literature warns it is "misleading whenever the composite signal contains similar wavelets at multiples of a given delay" (harmonic ambiguity) — a real risk given your LR4+horn rings.
- **Matched filtering / cross-correlation with the direct arrival:** a reflection is a delayed, scaled, possibly filtered copy of the direct sound; cross-correlating the post-direct IR against the (time-reversed) direct arrival yields peaks at reflection delays. This is the "rake/echo-aware" approach used in RIR processing (dEchorate database). It is the most physically principled way to reject artifacts, because a genuine reflection *correlates with the direct arrival's shape* while noise and distortion products do not.

**Recommended JTS detector (implementable spec). ADOPT.**
```
DETECT_FIRST_REFLECTION(ir, fs, direct_idx):
  # Stage 0 — establish the direct arrival and a source-signature template
  peak      = argmax(|ir|) within a small window around direct_idx
  template  = ir[peak - PRE : peak + SRC_SIG_MS*fs]   # SRC_SIG_MS = 0.6 ms (covers horn-rim)
  # Stage 1 — matched-filter characteristic function
  mf[n]     = normalized_xcorr(ir, template)          # peaks where IR looks like the source
  # Stage 2 — AIC characteristic function on the post-direct segment
  seg       = ir[peak : peak + SEARCH_T_MAX_MS*fs]    # SEARCH_T_MAX_MS = 7 ms (unchanged)
  aic[k]    = k*log(var(seg[1:k])) + (N-k-1)*log(var(seg[k+1:N]))
  cand_aic  = argmin(aic)                             # threshold-free onset estimate
  # Stage 3 — dual candidate + hysteresis
  candidates = local maxima of mf above MF_REL (default 0.5 of direct MF peak)
              that also lie within +/- GUARD_MS (0.15 ms) of an AIC break
  if no candidate:
      t_gate = SEARCH_T_MAX_MS                        # gate at ceiling (the 9-capture case)
  else:
      t_gate = earliest candidate delay tau_hat
  return t_gate, tau_hat, mf_peak_ratio
```
Failure modes each stage catches:
- **Matched filter (Stage 1)** rejects noise and harmonic-distortion products, because those do not resemble the direct arrival's waveform — the specific failure that produced cloud_04's 0.56 ms "reflection."
- **AIC (Stage 2)** removes the amplitude threshold that STA/LTA-style detectors rely on, so a single tall distortion spike below the reflection level no longer triggers.
- **Hysteresis / guard (Stage 3)** requires the two independent characteristic functions to agree in time before a detection is accepted, catching one-off glitches in either.
- **Ceiling fallback** reproduces the correct behavior for the 9 well-behaved captures (no reflection in span → gate at 7 ms).
Default parameters (to be tuned on the corpus): `SRC_SIG_MS=0.6`, `MF_REL=0.5` (reflection must be within ~6 dB of direct to be credible as a *near* surface), `GUARD_MS=0.15`, `SEARCH_T_MAX_MS=7`.

### Q2. Real nearby surface vs. detector artifact vs. source-fixed arrival
This is the crux of the incident, and it is solvable with data you already collect. The three classes have distinct signatures in the **arrival-time delay τ** across N repeats (same position) and M positions (same cloud):

| Class | Within-position repeatability (N repeats) | Across-position behavior (M positions) | Correct action |
|---|---|---|---|
| **Real nearby surface** | τ repeatable (low variance) | τ *moves* — different geometry at each mic spot | Gate at τ (it is a room reflection) |
| **Source-fixed arrival** (horn-rim ~0.3 ms) | τ repeatable (low variance) | τ *invariant* across all positions (fixed to the source) | **Do NOT gate** — it is the speaker |
| **Detector artifact** (noise/distortion) | τ *not* repeatable (high variance, or vanishes on repeat) | incoherent across positions | Reject detection; gate at ceiling |

**Discriminator spec. ADOPT.**
```
CLASSIFY(tau_by_repeat[pos][rep], tau_by_pos[pos]):
  # (a) within-position repeatability
  for each position p:
      if std(tau_by_repeat[p]) > REPEAT_TOL  or  detection present in < REPEAT_FRAC of repeats:
          mark p as ARTIFACT
  # (b) source-fixed vs. room across positions (use only positions not marked ARTIFACT)
  tau_med = median(tau_by_pos over surviving positions)
  spread  = MAD(tau_by_pos)
  if spread < SRCFIX_TOL:            # tau essentially identical everywhere
      class = SOURCE_FIXED           # e.g. horn-rim -> DO NOT gate on it; treat as part of DUT
  else:
      class = REAL_SURFACE           # tau varies with geometry -> legitimate gate
```
Defaults: `REPEAT_TOL = 1 sample (≈21 µs at 48 kHz) or 5% of τ, whichever is larger`; `REPEAT_FRAC = 0.6` (must appear in a majority of repeats); `SRCFIX_TOL ≈ 0.05 ms` (source-fixed arrivals move less than capsule-placement jitter). Geometry check: a real domestic reflection's τ should also vary by more than the across-position path-length change implied by the prompted mic moves.

**Why this resolves cloud_04:** the 0.56 ms event was present in one capture only. If it fails to repeat within cloud_04's own repeats, it is classified ARTIFACT and rejected (gate returns to the 7 ms ceiling, floor back to ~143 Hz). If it *were* repeatable within the position but τ were identical across all 10 positions, it would be SOURCE_FIXED (like the horn-rim) and also must not gate. Only if it repeated within-position *and* moved across positions would it be a real surface worth gating — which, given nine cloud-mates found nothing in the 7 ms span, it is not.

### Q3. Group-level aggregation — quantitative case against max, and for masked averaging
**Current design (group floor = max across positions).** Deliberately conservative, but the incident proves it is fragile: one collapsed gate (1778 Hz) set the entire session floor, discarding valid 143–1778 Hz data from the nine good captures. This is a single-point-of-failure aggregation.

**Options evaluated:**
- **Max (current). REJECT.** One outlier poisons the group. No disclosure path.
- **Median with per-position exclusion + disclosure. CONDITIONAL.** Robust to one outlier, but a single group floor still throws away the frequency range where *most* positions are valid.
- **Trimmed max. CONDITIONAL.** Better than max, still a single group floor.
- **Per-position masking before power-averaging (each position's curve masked below its own 1/Tᵢ, then curves power-averaged over whatever positions are valid at each frequency). ADOPT.**

**Does per-position masking bias the average near the floor?** This is the quantitative worry. Power-averaging M curves; at a frequency f, only m(f) ≤ M positions are valid (the rest masked out). Two effects:
- **Above ~200 Hz** in your geometries, essentially all positions gate at or near the 7 ms ceiling, so m(f) = M and masking changes nothing — the average is identical to today's. (This is the regime that matters for the crossover/linearization fit and the flatness verdict.)
- **Near the LF edge**, m(f) shrinks. The *bias* is that survivors are a non-random subset (the longest-gate mics, i.e., those farthest from surfaces), which slightly under-weights near-boundary positions. The *variance penalty* is the usual √(M/m) inflation of the power-average's standard error: with m = 2 survivors out of M = 10, the LF-edge estimate has ~2.2× the standard error of the full-cloud average.

**Recommendation:** adopt per-position masking, but require a **minimum-contributing-positions rule**: a frequency bin is graded only if at least `MIN_POS` positions (default `max(3, ceil(M/2))`) contribute. Below that, the bin is disclosed as "insufficient spatial support," not silently averaged from one or two mics. This preserves the honesty discipline (every exclusion disclosed) and eliminates the single-point-of-failure.

**What multi-mic products do:** they power-average magnitudes across positions (Sonos Trueplay explicitly averages many positions — moving-mic in their case — to "scramble" position-specific artifacts; Audyssey/Dirac average up to 8–9 fixed positions). None expose a per-position *gate*-validity floor, because they correct the in-room response (they *want* the room), not a quasi-anechoic verdict. So the averaging basis is shared precedent; the per-position gate masking is a JTS-specific extension with no direct product analog — flag as an academic-style extension, not shipped-product-proven.

### Q4. Anomaly → action policy
**Definition of "far."** A capture's gate is anomalous when its realized gate Tᵢ is much shorter than its cloud-mates. Use a robust, ratio-based rule (dimensionless, so it works regardless of absolute geometry):
- Let `T_med = median(T over cloud)`, `T_i` = this capture's gate.
- **Anomaly if** `T_i < ANOM_RATIO · T_med` (default `ANOM_RATIO = 0.5`, i.e., gate less than half the cloud median) **or** `f_valid,i > 2 · f_valid,med`.

**Decision table. ADOPT.**

| Condition | Classification (Q2) | Action | Rationale |
|---|---|---|---|
| Tᵢ ≥ 0.5·T_med | — | **Keep** | Normal; within cloud spread |
| Tᵢ < 0.5·T_med AND detection non-repeatable | ARTIFACT | **Reject detection, re-gate to ceiling, keep capture** | It was a false positive; the capture is fine |
| Tᵢ < 0.5·T_med AND repeatable AND τ invariant across positions | SOURCE-FIXED | **Do not gate on it; keep at ceiling** | It is the speaker (horn-rim), not the room |
| Tᵢ < 0.5·T_med AND repeatable AND τ moves | REAL SURFACE | **Auto-retake (bounded), prompt user to move phone** | Genuine nearby surface — mic placement error |
| Retake still anomalous after `MAX_RETAKE` (default 2) | REAL SURFACE | **Exclude-with-disclosure** | Can't fix; don't poison group, but disclose |
| — | — | **Never keep-and-poison (current)** | Violates honesty + robustness |

Numeric anchor for the product incident: the 1.02 ms window (→ ~980 Hz floor) that the verify path already refuses corresponds to `T_i` far below any plausible `T_med` in a 10 ft room — it would be flagged REAL-SURFACE (capsule near a surface) and trigger a retake prompt, which is exactly the correct behavior and closes the measure/verify asymmetry.

**User-facing copy (household member, no acoustics knowledge). ADOPT grammar:**
- *Auto-retake (real surface):* "One spot picked up a nearby surface. Let's redo that one — hold the phone a little farther from walls, shelves, and the speaker edge, then tap Retry."
- *Exclude-with-disclosure (retake failed):* "We left one measurement out because it kept picking up something too close to the phone. Your result is based on the other N."
- *Artifact (silent, logged):* no user prompt; log only. (A one-off glitch shouldn't nag the user.)
- *Source-fixed (silent, logged):* no user prompt; the arrival is part of the speaker.

### Q5. The validity-floor model — is `f_valid = 1/T` right for a half-Hann-tailed gate?
**Two framings.**
1. **Main-lobe / resolution:** the frequency resolution of a window of length T is Δf ≈ k/T, where k depends on window shape. For a **rectangular** window the main-lobe half-width (to first null) is 1/T (k≈1); for a **Hann** window the main lobe doubles (k≈2; two-sided main-lobe width 4/T). A **rectangular-with-half-Hann-tail** window (your case) sits *between* these — closer to rectangular because only the tail is tapered — so an effective **k between 1 and ~1.5**.
2. **Truncation bias:** for a system whose IR decays past the gate (your LR4+horn rings past 7 ms), truncation causes a magnitude error that grows toward low frequency. The established semi-quantitative numbers (Audio Precision engineering note "Loudspeaker Acoustic Measurements in Ordinary Rooms," attributing the underlying 1/T rule to Struck & Temme): for a 5 ms gate (1/T = 200 Hz), the windowed response "is off by about 2 dB right at 200 Hz" (f·T = 1) and "is only very close to the true response from about 400 Hz and above" (f·T ≈ 2), with the "smoothing effect of the windowing" extending up to about 1 kHz (f·T ≈ 5). IEC 60268-5 sets the acceptance benchmark that truncation error "should not exceed 1 dB over the frequency range of interest." (Full canonical reference: C. J. Struck & S. F. Temme, "Simulated Free Field Measurements," *JAES* 42(6):467–482, June 1994; first presented at the 93rd AES Convention, San Francisco, 1992; AES e-library id=6937.)

**Assessment.** `f_valid = 1/T` is the correct *edge* marker (it is what REW, ARTA, MLSSA, CLIO, and Struck & Temme all use), but as a *floor of trustworthiness* it is optimistic: at exactly 1/T you already have ~2 dB of error for a ringing DUT, which exceeds the IEC 1 dB tolerance.

**Recommendation. ADOPT a graded (soft) validity band instead of a hard cliff:**
- **Below 1/T:** invalid — "a window artifact, not a measurement" (unchanged hard floor; excluded from grading).
- **1/T ≤ f < 2/T:** valid-but-uncertain — grade with an added uncertainty of **±2 dB at 1/T tapering to ±0.5 dB at 2/T** (linear-in-log-f interpolation). Spec verdicts in this band should be rendered with the uncertainty (e.g., a "marginal" state rather than hard pass/fail).
- **f ≥ 2/T:** full-confidence.
- Keep `k = 1` for the *hard* floor (consistent with all prior art and your recorded floors), but *report* the graded band so the honesty machinery reflects that 1/T is an edge, not a guarantee. This is more honest than the current hard cliff and is directly supported by the truncation-bias literature.

Note the half-Hann tail *helps* truncation bias (it suppresses the Gibbs ripple from an abrupt cut) at the cost of slightly widening the main lobe — so your window is already a reasonable resolution/bias compromise; the tail fraction is a second-order tuning knob, not a floor-model change.

### Q6. Frequency-dependent gating — worth it for an automated consumer instrument?
**What it is and who ships it.** Frequency-dependent windowing (FDW) uses a window whose length is a fixed number of *cycles* at each frequency (long in time at LF, short at HF), keeping LF resolution while rejecting HF reflections. REW implements it; per the Home Theater Shack FDW feature-request thread, the widely used 15-cycle default originates with Dr. Uli Brüggemann/Acourate — "15 cycles (at 48 kHz)... is Uli's favorite setting. It provides sufficient psychoacoustic width for low frequencies and sufficiently 'anechoic' width for the ear at high frequencies." Acourate's own wiki states its Room Macro 1 FDW default is "set to 15/15... a good compromise between hiding the room information and including enough speaker information." WiiM launched a consumer "Precision Room Correction with Frequency-Dependent Windowing (FDW)" as a public beta on March 24, 2025 (WiiM Team announcement, WiiM Home forum): "FDW focuses on capturing direct sound while minimizing the impact of room reflections." So FDW is real and shipping.

**Documented downsides (the honesty risks):**
- FDW is effectively a *frequency-dependent smoothing*. A WiiM Home beta tester reported (page 3 of the "Beta Test: Precision Room Correction with FDW" thread) that "with 'Precision' RC (FDW) enabled the filters are less sharp... it is IMO apparent right away that the 'Precision' RC has significantly less bass — and IMHO not in a good way. Kick drums and bass lines lose a lot of their impact, and consequently the tonality turns bright-ish." For a *correction* product that's a taste issue; for a *pass/fail measurement* it means the verdict depends on the cycle-count parameter — a hidden knob that reshapes the graded curve.
- The 15-cycle default is a **psychoacoustic** heuristic (Brüggemann/Acourate), not a truncation-bias optimum; it has no principled tie to your flatness spec.
- Splitting into bands with different windows raises **band-edge phase/continuity** and **time-aliasing** risks if implemented naively (VNA time-gating literature documents Gibbs ringing and the need for careful window design when band-limiting).

**Recommendation. CONDITIONAL / lean two-band, not full FDW.** For an automated instrument grading flatness with no acoustician, a **principled two-band gate** beats both a single gate and full FDW:
- **Below a crossover Xf (candidate ~300–500 Hz):** use the longest defensible gate (up to the 7 ms ceiling / near-field-style handling), accepting the honest LF floor.
- **Above Xf:** use a shorter gate sized to reject the first domestic reflection cleanly.
- Splice with a matched, phase-continuous crossover (mirroring the near/far-field splice practice of MLSSA/CLIO/Struck-Temme, which is shipped-product-proven).
This captures most of FDW's benefit (LF resolution without HF reflection contamination) with a *single, auditable* band-split parameter rather than a continuous cycle-count that silently reshapes the graded curve. **Validation:** run both single-gate and two-band on the 26-capture corpus and confirm the two-band curve matches the single 7 ms gate above Xf to within ±0.25 dB (no HF artifact) and only differs, as intended, near the LF edge. If two-band cannot be validated to that tolerance, fall back to single-gate + graded band from Q5. Do **not** ship full continuous FDW into the automatic verdict path.

### Q7. Gate comparability across a session
Your verify path already refuses when the verify gate is much shorter than the measure gate. Generalize this into a small set of named, session-level invariants:
- **INV-1 (Comparable-gate).** For any before/after or measure/verify comparison, require `min(T_a, T_b) ≥ COMPARE_RATIO · max(T_a, T_b)` (default `COMPARE_RATIO = 0.7`). Violation → refuse the comparison (curves are windowed differently and cannot be honestly differenced). This is the generalization of your existing verify refusal.
- **INV-2 (Common-floor for differencing).** When differencing two curves, grade the difference only above `max(f_valid,a, f_valid,b)`. Below that, disclose as non-comparable.
- **INV-3 (Cloud-coherence).** Within a cloud, if the MAD of per-position gates exceeds `CLOUD_MAD_TOL` (default: gates spanning more than 2× median), flag the cloud as geometrically inconsistent (mixed mic placements) and surface a "your measurements varied a lot — consider remeasuring" prompt.
- **INV-4 (Window-shape invariance).** The same window shape (rectangular + half-Hann tail, same tail fraction) must be used across all captures being compared; a shape change silently changes k and the effective floor. Enforce as a config assertion.
**Surfacing:** invariants are checked at aggregation time; violations produce a disclosed exclusion or a refusal (never a silent pass), consistent with the settled honesty machinery.

## Pre-Registered, Falsifiable Predictions (grade against the 26-capture corpus)
Each is stated with mechanism so a failure is diagnostic.
1. **cloud_04 classification.** The recommended detector classifies cloud_04's 0.56 ms event as **ARTIFACT** and re-gates it to the 7 ms ceiling, restoring its floor to ~143 Hz — *because* the matched-filter stage finds the 0.56 ms peak is <0.5 correlation with the direct-arrival template (distortion/noise, not a reflection) and/or it fails to repeat within cloud_04's repeats. *Falsified if* the 0.56 ms event has high matched-filter correlation AND repeats within-position — in which case it is a real near surface and the retake path (not rejection) is correct.
2. **Session-floor recovery.** With per-position masking + `MIN_POS = max(3, ceil(M/2))`, the 10-position session's graded floor returns to ~143 Hz (median of the nine ceiling gates) rather than 1778 Hz. *Falsified if* fewer than `MIN_POS` positions actually reach the ceiling.
3. **Masking bias above 200 Hz.** Per-position masking changes the 10-position power average by **< 0.1 dB above 200 Hz** versus the current group method, *because* essentially all positions are valid there (m(f) = M). *Falsified if* any position gates below 200 Hz-equivalent in the good set.
4. **Masking effect near the LF edge.** Between 143 and 200 Hz the masked average differs from a naive full-average by a measurable but bounded amount (**< 1 dB**), reflecting the non-random survivor subset. *Falsified if* the difference exceeds 1 dB (would indicate the survivors are strongly biased and `MIN_POS` must rise).
5. **Source-fixed arrival is preserved.** The known ~0.3 ms source-fixed (horn-rim) arrival is classified **SOURCE-FIXED** (τ invariant across all positions, MAD < 0.05 ms) and does **not** trigger a gate or a retake. *Falsified if* its τ varies across positions by more than SRCFIX_TOL (then it isn't truly source-fixed).
6. **Graded band vs. hard cliff.** For the ground-plane captures (longer gates), the graded ±uncertainty band between 1/T and 2/T changes zero *pass* verdicts to *fail* but converts some borderline near-floor bins to "marginal," *because* the added uncertainty only matters within ~2 dB of the spec limit. *Falsified if* it flips a clear pass/fail far from the limit (would indicate a coding error in the band).
7. **Two-band vs. single-gate HF agreement.** A two-band gate matches the single 7 ms gate above the splice to within **±0.25 dB**, *because* above the splice both use the same effective HF gate. *Falsified if* band-edge artifacts exceed 0.25 dB (then the splice phase-continuity is wrong).
8. **AIC vs. current detector on the desk-edge set.** On the desk-edge geometries where the first real reflection is beyond the 7 ms span, the AIC+matched-filter detector returns "no detection → ceiling" on the same captures the current detector correctly ceilings, i.e., **no regressions**, while additionally rejecting any spurious early picks. *Falsified if* AIC introduces a spurious early pick the current detector avoided.

## Recommendations
**Stage 1 — ship immediately (highest leverage, lowest risk):**
- Replace the group-max floor with **per-position masked averaging + `MIN_POS` rule** (Q3). This alone would have prevented the incident and changes nothing above 200 Hz (Predictions 2–3).
- Add the **anomaly→action policy** (Q4): auto-retake for real surfaces, exclude-with-disclosure on failure, never keep-and-poison. Wire the existing bounded-retake mechanism to the measure path, closing the measure/verify asymmetry.
**Stage 2 — the detector (needs corpus validation before shipping):**
- Implement the **AIC + matched-filter dual detector with hysteresis** (Q1) and the **N-repeat / M-position τ-variance classifier** (Q2). Grade against Predictions 1, 5, 8 on the 26-capture corpus before enabling in production. Threshold to change the plan: if Prediction 8 shows any regression on the desk-edge ceiling cases, keep the current ceiling-fallback detector and use AIC only as a *veto* on early picks, not as the primary picker.
**Stage 3 — the validity model:**
- Adopt the **graded uncertainty band** (Q5): hard floor at 1/T, ±2 dB→±0.5 dB uncertainty band from 1/T to 2/T, full confidence above 2/T. Keep recorded per-capture floors at 1/T for continuity.
**Stage 4 — frequency-dependent gating (only if Stages 1–3 leave an LF-accuracy gap):**
- Prototype the **two-band gate** (Q6) and validate against Prediction 7. Do not ship continuous FDW into the verdict path.
**Session invariants (cross-cutting):** implement INV-1..4 (Q7) at aggregation time.
**Benchmarks that would change these recommendations:**
- If corpus grading shows per-position masking biases the 143–200 Hz band by >1 dB (Prediction 4 fails), raise `MIN_POS` or fall back to trimmed-max with disclosure.
- If the AIC/matched-filter detector regresses on desk-edge ceilings (Prediction 8 fails), demote AIC to a veto role.
- If two-band cannot match single-gate HF to ±0.25 dB (Prediction 7 fails), drop it and rely on the graded band.

## Caveats
- **No shipped product does automatic reflection classification**, so the Q1/Q2 detector is adapted from seismology/AE arrival-picking and echo-detection literature, not from a proven audio product. It must be validated on your corpus before it governs a consumer verdict. (Klippel's holographic separation is the only shipped "automatic" alternative and it violates the special-hardware anti-goal.)
- **The exact dB-vs-f·T truncation numbers** (≈2 dB at f·T=1, trustworthy near f·T=2) come from an Audio Precision engineering note that attributes the underlying 1/T rule to the peer-reviewed Struck & Temme (1994, JAES 42(6):467–482). The literature gives the semi-quantitative rule, not a published dB table at f·T = 0.5/1/2/3; if you need exact values at 0.5 and 3, compute them from a windowed-IR simulation of your own LR4+horn model.
- **The AIC index convention** varies across papers ((N−k−1) vs (N−k)); Maeda (1985) is in Japanese and is universally cited via Kurz, Grosse & Reinhardt (2005). Use whichever convention you validate; the argmin behavior is unaffected.
- **Per-position gate masking has no direct shipped-product precedent** (multi-mic room-correction products average magnitudes but do not expose per-position quasi-anechoic floors); treat it as a principled extension, and lean on the pre-registered predictions to catch bias.
- **FDW side effects** (bass-level change, parameter-dependent smoothing) are reported from consumer forums (WiiM beta) and product docs (REW/Acourate); they are consistent and credible but are user reports, not controlled studies. The recommendation to avoid full FDW in the verdict path is conservative on that basis.
- All recommendations respect the stated anti-goals: no acoustician, no special hardware, no second session, and every exclusion disclosed. The one place to watch is the graded band (Q5) — ensure "marginal" states are disclosed, not silently rounded to pass.
