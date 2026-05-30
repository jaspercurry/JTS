# HANDOFF - Wake-corpus audio quality audit

> **Current operational truth as of 2026-05-27.** This doc is the
> canonical methodology for programmatic quality analysis of deliberate
> wake-corpus WAVs captured by the browser recorder at
> `http://jts.local/wake-corpus/`.
>
> Read this before building or expanding a wake-corpus quality analyzer.
> Read [`HANDOFF-wake-training-experiment.md`](HANDOFF-wake-training-experiment.md)
> for the recording protocol and training plan. Read
> [`testing-tooling.md`](testing-tooling.md) to avoid duplicating an
> existing measurement tool.

---

## TL;DR

**The shipped audit is intentionally basic.** `scripts/audit-wake-corpus.sh`
answers "did the corpus record what we think it recorded?" It checks
metadata, expected legs, coverage, WAV format, duration, RMS, and peak.
It is not a deep audio-quality analyzer.

**Quality analysis starts with deterministic signal metrics.** For 1-3 s
wake-word clips, trust sample-domain and frame-domain facts first:
exact clipping, near-clipping, flat-top runs, DC offset, RMS, crest
factor, spectral flatness, high-band energy, Nyquist-edge energy, local
MAD transient candidates, and LPC residual outliers.

**Cross-leg comparison is the superpower.** JTS captures simultaneous
legs for the same utterance: XVF-derived raw/WebRTC-AEC/DTLN/raw0,
optional AEC3 sweep variants, optional cheap-USB raw/WebRTC-AEC/DTLN,
and reference. A suspicious event that exists only in one processed leg
means something very different from an event that appears at the same
aligned timestamp in every mic leg.

**MOS-style neural predictors are advisory only.** SQUIM, DNSMOS, NISQA,
UTMOS, SRMR, and related no-reference metrics can help rank clips, but
absolute scores on 1-3 s wake-word audio are fragile. Use them as soft
relative signals after deterministic metrics and human listening.

**Do not run heavy quality models on the Pi.** The 1 GB Raspberry Pi path
should stay light: recording, metadata, basic health checks, and maybe
cheap deterministic metrics. Neural quality models and review packages
belong on the laptop/offline side after rsync.

**The output is a review queue, not an auto-reject oracle.** The analyzer
should sort clips by suspicion, explain why, and generate review packages
that make it easy for Jasper to listen and inspect. It should not silently
delete or exclude clips from training.

**Waveform fusion is an offline hypothesis test.** The experimental
`scripts/_waveform_fusion_experiment.py` harness can align/mix paired
same-utterance legs such as `on + dtln` or `usb_webrtc + usb_dtln` and
score the generated WAVs. Use it to test whether a mixed waveform adds
evidence over score fusion; do not treat it as a production-quality
enhancement chain without hard-negative and multi-session validation.

---

## 1. Scope

This doc covers deliberate wake-corpus recordings under
`/var/lib/jasper/enrollment_positives/` on the Pi and
`./data/enrollment_positives/` after rsync.

It is for:

- Jarvis positives in Session A and held-out Session B.
- Hard negatives recorded through the same browser recorder.
- Per-utterance, per-leg WAV QA.
- Diagnosing artifacts such as tearing, clipping, AGC pumping, limiter
  behavior, dropouts, rough high-band distortion, and AEC/NS artifacts.
- Producing human-review packages that combine metrics, plots, and audio.
- Offline research experiments that compare same-utterance leg fusion
  strategies, as long as the output is reviewed against ordinary
  score/decision fusion and hard negatives.

It is not for:

- Production wake-event telemetry. That lives in
  [`HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md).
- Wake-model scoring. Use the offline wake-word scoring tools described
  in [`testing-tooling.md`](testing-tooling.md).
- Changing the AEC topology. Architecture constraints still live in
  [`AGENTS.md`](../AGENTS.md) and [`HANDOFF-aec.md`](HANDOFF-aec.md).
- Treating the speaker-reference leg as clean speech. The reference leg
  is the playback reference consumed by AEC experiments, not a clean
  target recording of Jasper's voice.

---

## 2. Recorder Legs

The analyzer must be leg-aware. Do not collapse all WAVs into a flat pile.

| Leg | Meaning | Production wake input? | Notes |
|---|---|---:|---|
| `aec` / WebRTC AEC | XVF mic path after software WebRTC AEC3 | Yes | Label clearly as WebRTC AEC, not just "WebRTC". |
| `raw` / chip-direct | XVF chip-direct stream used by the current raw wake leg | Yes | This is the useful XVF raw-ish production leg. |
| `dtln` | XVF path through the DTLN neural AEC leg | Yes when enabled in production | High resource cost; useful as corpus data when enabled. |
| `raw0` | Truly raw XVF mic 0 / chip channel 2, no chip DSP and no software AEC | No | Corpus-only future-proofing for cheap-mic portability work. |
| `chip_aec_150` | XVF on-chip AEC ASR output with fixed gated 150° beam | No | Corpus-only chip-AEC comparison profile. Treat as hardware-AEC evidence, not a production wake leg yet. |
| `chip_aec_210` | XVF on-chip AEC ASR output with fixed gated 210° beam | No | Corpus-only chip-AEC comparison profile. Keep paired with 150° because mic orientation changes can swap the winner. |
| `xvf_raw0_webrtc_aec3` | Truly raw XVF mic 0 through software WebRTC AEC3 | No | Same raw channel as `raw0`, processed with the current software AEC3 baseline for chip-vs-software comparison. |
| `xvf_raw0_dtln` | Truly raw XVF mic 0 through DTLN | No | Optional and resource-sensitive; useful when comparing neural AEC against chip AEC and WebRTC AEC3. |
| `usb_raw` | Cheap USB mic capture with no JTS software processing | No | Watch hardware AGC/limiter state carefully. |
| `usb_webrtc` | Cheap USB mic through software WebRTC AEC | No | Corpus-only experiment for lower-cost mic paths. |
| `usb_dtln` | Cheap USB mic through DTLN | No | Optional and resource-sensitive. |
| `ref` | Speaker playback reference | No | Use for AEC/post-hoc experiments and alignment; list last in playback UI. |
| `aec3_variant_1` | Corpus-only parallel WebRTC AEC3 slot 1 | No | Stable slot; current label/knobs live in session metadata and `/var/lib/jasper/aec3_sweep_variants.json` when overridden. `aec3_sweep_source` says whether this slot was XVF-fed or USB-fed. |
| `aec3_variant_2` | Corpus-only parallel WebRTC AEC3 slot 2 | No | Stable slot; current label/knobs live in session metadata and `/var/lib/jasper/aec3_sweep_variants.json` when overridden. `aec3_sweep_source` says whether this slot was XVF-fed or USB-fed. |
| `aec3_variant_3` | Corpus-only parallel WebRTC AEC3 slot 3 | No | Stable slot; current label/knobs live in session metadata and `/var/lib/jasper/aec3_sweep_variants.json` when overridden. `aec3_sweep_source` says whether this slot was XVF-fed or USB-fed. |

Leg names in future metadata should stay stable and explicit. For AEC3
sweeps, the machine-readable names are intentionally generic stable
slots because the hypothesis changes often; the session sidecar's
`aec3_sweep_source`, `aec3_sweep_variants`, and `aec3_sweep_config.hash`
are the source of truth for the actual input mic and knobs behind each
slot. As of 2026-05-28, new recorder-created sweep sessions default
these slots to the cheap USB mic while retaining the XVF `on` leg as
the comparison reference. The current built-in USB pilot labels
`usb_webrtc` as the 40 ms edge-combo delay-hint baseline and the three
variant slots as 80/120/160 ms delay hints; older sessions without the
source field were XVF-fed.

As of 2026-05-29, the chip-AEC comparison profile uses explicit leg
names instead of generic sweep slots for the fixed XVF hardware-AEC
outputs and raw0-derived software legs. Analyze `chip_aec_150` and
`chip_aec_210` separately; do not average them away, because orientation
and room geometry are exactly what this profile is meant to reveal.

---

## 3. Metric Tiers

### Tier A: Compute Always

These are cheap, deterministic, and reliable on 1-3 s mono 16 kHz int16
clips.

| Metric | What it catches | First threshold, tune from data |
|---|---|---|
| Peak dBFS | Too-hot recordings | Warn above -1 dBFS; exact full-scale samples are critical. |
| True peak dBTP | Inter-sample clipping risk | Warn above -1 dBTP if implemented offline. |
| Exact clip count | Digital clipping | Any `-32768` or `32767` sample is critical. |
| Near-clip count | Limiter/overload near rails | Track samples within 0.5, 1, and 3 dB of full scale. |
| Flat-top runs | Hard clip or limiter ceiling | Runs >= 3 samples are suspicious; >= 6 severe. |
| RMS and gated RMS | Too quiet / too hot speech | Use VAD-gated speech RMS where available. |
| Crest factor | Compression, limiter, impulses | Low crest suggests compression/clip; very high crest suggests impulse. |
| DC offset | Bad coupling, biased frontend | Warn around `abs(mean / full_scale) > 0.001`. |
| Dropout / silence spans | Stream stalls, packet loss | Flag zero or near-zero runs inconsistent with normal speech. |
| Repeated-sample runs | USB/buffer glitch | Flag repeated non-zero samples across several frames. |
| Local MAD on sample delta | Candidate tears/clicks | Candidate generator only; confirm before escalating. |

### Tier B: Diagnostic Metrics

These are still deterministic, but interpretation needs context.

| Metric | What it catches | Notes |
|---|---|---|
| Spectral flatness | Whitened/noisy speech, musical noise | Aggregate p50/p90 over VAD-active frames. |
| Spectral centroid / rolloff | Muffled, harsh, or aliased audio | Short clips can be phoneme-biased; compare legs. |
| High-band energy ratio | Sibilant harshness, clipping harmonics | Compare against local baseline and sibling legs. |
| Nyquist-edge energy | Bad resampling or alias artifacts | Watch 7.2-8 kHz on 16 kHz audio. |
| Spectral flux | Frame-to-frame discontinuity | Useful with LPC residual for tear confirmation. |
| LPC residual outliers | Speech-inconsistent clicks/tears | Prefer over raw derivative thresholds for speech. |
| RMS envelope FFT | AGC pumping / breathing | Look for coherent 1-10 Hz gain movement. |
| Crest-vs-RMS correlation | AGC/limiter behavior | Negative correlation suggests gain riding. |
| Cross-leg alignment | Clock drift and event coincidence | GCC-PHAT first; sliding alignment for USB drift if needed. |

### Tier C: Advisory Neural / Perceptual Metrics

These are optional and offline-only. They should never be the only reason
a clip is rejected.

| Metric | Why use it | Caveat |
|---|---|---|
| TorchAudio SQUIM objective | Native 16 kHz reference-less estimates of STOI/PESQ/SI-SDR | Best first neural candidate, but still high variance on very short clips. |
| DNSMOS / DNSMOS P.835 | Useful relative speech/noise/overall ranking | Short clips are repeated to the model window; absolute MOS is biased. |
| NISQA | Multi-dimensional MOS including discontinuity/coloration/loudness | Training domain is longer, often 48 kHz speech; 1-3 s wake clips are out-of-domain. |
| SRMR / SRMRnorm | Reverberation and modulation structure | Designed for longer speech; useful mainly as a relative feature. |
| UTMOS / MOSNet | Additional MOS sanity check | More TTS-oriented; lowest priority for this corpus. |

---

## 4. Tear / Click Detection

"Tear" here means a short, broadband, unphysical discontinuity: USB
underrun, DMA glitch, buffer repeat/drop, or a processing frame-boundary
artifact. Plosives and fricatives can look similar in raw samples, so
single-threshold derivative checks are not enough.

Recommended pipeline:

1. Generate candidates with local MAD/Hampel-style outlier detection on
   first derivative (`delta x`) and optionally second derivative.
2. Confirm candidates with LPC prediction residual outliers on short
   speech frames. Use order 10-12 as the starting point for 16 kHz speech.
3. Require spectral-flux or high-band-energy corroboration for major
   flags.
4. Suppress false positives with duration and context:
   - single-sample or few-sample impulses are more suspicious than
     10-50 ms consonant evolution;
   - unvoiced consonants should not be treated as digital tears by
     default;
   - repeated events every 10, 20, or 30 ms suggest frame-boundary or
     processing artifacts.
5. Use cross-leg coincidence as the final arbiter:
   - present in all mic legs at the same aligned time: probably speech or
     room event;
   - present only in `usb_raw`: likely USB mic/hardware path;
   - present only in `usb_webrtc`: likely WebRTC processing artifact;
   - present only in `dtln` or `usb_dtln`: likely neural AEC artifact.

The first implementation can emit candidate timestamps and confidence
instead of pretending the labels are perfect.

---

## 5. Clipping And Overload

Digital clipping is easy: count exact full-scale samples. The cheap USB
mic concern is harder because analog frontend overload, firmware AGC, or
firmware limiting may distort the signal before it reaches int16 full
scale.

Detect both:

- exact full-scale sample count;
- near-clip mass at 0.5, 1, and 3 dB below full scale;
- flat-top run length with both exact-LSB and near-peak tolerances;
- peak histogram concentration around a target level;
- crest-factor collapse;
- asymmetric positive/negative peak behavior;
- DC offset;
- high-band burst around near-peak events.

Analog overload should be represented as "saturation suspected," not
"digital clipping," unless exact full-scale samples are present.

---

## 6. AGC / Limiter Detection

The cheap USB mic may expose hardware AGC. Even when AGC is toggled off
through ALSA, verify with audio because USB firmware controls are not
always truthful.

Per clip:

- compute a 10 ms RMS envelope, then downsample/retain it at 100 Hz;
- compute crest factor in 250 ms windows;
- measure correlation between windowed RMS and crest factor;
- estimate pause noise floor before and after speech if pauses exist;
- compute envelope modulation spectrum and look for coherent 1-10 Hz
  peaks;
- compare peak distribution across utterances in the same session.

Across paired captures:

- compare USB raw against XVF raw/chip-direct for the same utterance;
- compare AGC-on and AGC-off sessions only when environment and music
  level are reasonably controlled;
- treat the 2026-05-27 AGC-off pilot as a useful clue, not as final
  proof, because music level and session context changed.

The analyzer should record the ALSA control state in its report whenever
available, but audio evidence wins over control labels.

---

## 7. Spectral Analysis Defaults

Use one consistent framing policy unless data proves otherwise:

- sample rate: 16 kHz;
- frame: 25 ms / 400 samples;
- hop: 10 ms / 160 samples;
- window: Hann;
- FFT: 512 bins;
- spectral aggregates: p10, p50, p90, p95, max, and event-local values;
- high band: start with 3-7.5 kHz;
- Nyquist edge: start with 7.2-8 kHz or top 5% of FFT bins.

Do not overfit one global spectral threshold. A one-syllable "Jarvis"
clip can be dominated by the /j/, /r/, /v/, or /s/ region depending on
pronunciation and timing. Cross-leg deltas and event-local spikes are
more actionable than single absolute values.

---

## 8. Cross-Leg Analysis

Cross-leg analysis should be a first-class object in the JSON output.

Minimum plan:

1. Group files by utterance/session metadata, not filename guessing.
2. Resample only if needed; the corpus target is 16 kHz mono int16.
3. Align sibling legs with GCC-PHAT or normalized cross-correlation.
4. Report lag and alignment confidence per leg pair.
5. Build an event-coincidence matrix for transient candidates.
6. Compute per-metric deltas from baseline legs:
   - `usb_webrtc - usb_raw`;
   - `usb_dtln - usb_raw`;
   - `aec - raw`;
   - `dtln - raw`.
7. Mark processed-leg regressions where processed output is worse than
   the baseline leg by corpus-relative percentile.

For AEC-specific metrics, ERLE is only meaningful in far-end-only windows
where the speaker reference is active and Jasper is not speaking. A wake
utterance with near-end speech is not a clean ERLE measurement.

USB and XVF clocks may drift. If full-clip alignment is weak, use sliding
window alignment and report drift rather than forcing a single lag.

---

## 9. Scoring And Output

Avoid one magic "quality score." Use a sortable suspicion model with
explanations.

Recommended top-level fields:

```json
{
  "session_id": "20260527T...",
  "utterance_id": "0003",
  "condition": "music",
  "distance": "far",
  "legs": {
    "usb_raw": {
      "duration_s": 1.42,
      "peak_dbfs": -3.1,
      "rms_dbfs": -28.4,
      "crest_db": 25.3,
      "critical": [],
      "major": ["transient_candidate"],
      "minor": ["agc_suspect"],
      "events": [
        {"t_s": 0.618, "kind": "lpc_residual", "confidence": 0.71}
      ]
    }
  },
  "cross_leg": {
    "alignment": {},
    "event_coincidence": {},
    "processed_regressions": []
  },
  "review_priority": 87,
  "review_reasons": ["usb_raw transient candidate not present in XVF legs"]
}
```

Sorting should be by critical count, major count, suspicion score, and
confidence. Normalize event counts per second so short and long clips are
comparable.

---

## 10. Review Packages

The most useful deliverable is an HTML review package per session or per
high-priority subset.

Each utterance row should include:

- metadata: session, condition, distance, label, utterance id;
- one audio player per leg, in stable order with `ref` last;
- compact waveform per leg with event markers;
- spectrogram or mel spectrogram per leg;
- RMS envelope overlay for AGC inspection;
- peak/RMS/crest/clip/flat-top summary;
- cross-leg event matrix;
- direct links to the WAV files.

This is where neural metrics belong if enabled: visible, contextual, and
clearly marked as advisory.

---

## 11. Implementation Plan

Phase 0: promote this methodology into tests and fixtures.

- Add synthetic fixtures for hard clipping, soft clipping, isolated
  click, click burst, dropout, repeated samples, DC offset, AGC pumping,
  pure fricative negative, plosive negative, aliasing, and processed-leg-
  only artifact.
- Lock expected detector behavior before running on the real corpus.

Phase 1: deterministic analyzer.

- Build a laptop-side script that reads `data/enrollment_positives/`.
- Emit JSON and CSV.
- Compute Tier A and selected Tier B metrics.
- Keep dependencies small: Python stdlib WAV reading plus numpy/scipy is
  the right first shape.

Phase 2: cross-leg analyzer and HTML review.

- Group utterances by metadata.
- Align sibling legs.
- Build event coincidence tables and processed-minus-baseline deltas.
- Generate review packages with audio players and plots.

Phase 3: optional neural metrics.

- Add SQUIM objective first.
- Add DNSMOS only with the short-clip loop/repetition caveat in output.
- Add NISQA/UTMOS only if they change decisions in listening review.
- Pin model versions and checksums.

Phase 4: USB AGC characterization.

- Run a controlled AGC-on/off experiment if the USB mic exposes a real
  toggle.
- Tune AGC thresholds from paired data.
- Record final threshold changes in this doc or a small thresholds file
  referenced from this doc.

---

## 12. Known Pitfalls

- Integrated LUFS and Loudness Range are unstable on 1-3 s clips. Prefer
  momentary or percentile loudness if loudness is needed.
- DNSMOS repeating short clips to fit its model window can amplify
  periodic artifacts and bias the score.
- NISQA and UTMOS are useful research tools, but their training domains
  do not match short wake-word corpus clips cleanly.
- Sample-delta click detectors false-positive on plosives and fricatives
  unless confirmed with LPC residual, spectral context, and cross-leg
  coincidence.
- The reference leg is not clean speech.
- AEC metrics require the right acoustic segment. Do not compute ERLE
  across Jasper saying "Jarvis" and call it meaningful.
- A polished score is dangerous if it hides localized artifacts. A single
  click can matter more than a good average MOS.
- A waveform mix that improves recall on positives can still be the wrong
  answer if it destroys per-leg diversity, loses clips that score fusion
  catches, or raises hard-negative false accepts.

---

## 13. Waveform Fusion Experiment

`scripts/_waveform_fusion_experiment.py` is the current offline harness
for testing whether aligned AEC3/DTLN waveform mixes are useful. It:

- reads local recorder metadata under `enrollment_positives/metadata`;
- pairs same-utterance legs, defaulting to `on + dtln` and
  `usb_webrtc + usb_dtln`;
- sweeps delays, weights, and normalization modes;
- writes generated WAVs and CSV/Markdown summaries under
  `captures/waveform-fusion/<session>/`;
- optionally scores originals and mixes with an openWakeWord ONNX model;
- explicitly compares mixed-waveform hits against same-pair max-score /
  OR fusion.

The decision bar is intentionally high: a mixed waveform must beat
score-level fusion across multiple sessions and hard negatives before it
is worth considering as a live or training leg. A single-session recall
gain is evidence to investigate, not a production recommendation.

First 2026-05-28 result on session `20260528T184424Z-d205`: best XVF
mix (`on + dtln`, RMS-matched, `dtln` delayed +10 ms, 50/50) hit 20/27
as a single waveform and added one new clip over the original all-leg
union. Best USB mix (`usb_webrtc + usb_dtln`, native levels,
`usb_dtln` delayed +20 ms, 50/50) hit 14/27 and also only added that
same clip over the full original union. Net read: promising research
candidate; score/decision fusion remains the default architecture.

---

## 14. Source Notes

Primary sources and official implementation docs to prefer when extending
this work:

- ITU-R BS.1770 for loudness and true-peak measurement.
- EBU R 128 and EBU Tech 3341 for loudness/true-peak practice.
- FFmpeg `ebur128`, `astats`, `volumedetect`, and `silencedetect`
  filters for fast sanity checks.
- SciPy signal docs for resampling, filtering, peak finding, and robust
  statistics primitives.
- librosa feature docs for STFT, spectral flatness, centroid, rolloff,
  flux, MFCCs, and LPC helpers.
- Essentia ClickDetector / LPC-based declicking lineage for impulsive
  artifact detection.
- Vaseghi, *Advanced Digital Signal Processing and Noise Reduction*, and
  Godsill/Rayner, *Digital Audio Restoration*, for AR/LPC click models.
- Microsoft DNSMOS / DNSMOS P.835 papers and `microsoft/DNS-Challenge`
  implementation.
- Gabriel Mittag's NISQA paper and `gabrielmittag/NISQA` implementation.
- TorchAudio SQUIM paper and official TorchAudio tutorial.
- Kim and Stern's WADA-SNR paper if WADA-SNR becomes useful for relative
  corpus comparisons.
- Falk et al. SRMR work if reverberation/modulation metrics become useful.

The local research reports reviewed on 2026-05-27 are good seed material,
but this HANDOFF is now the repo-facing source of truth. If the research
and this doc diverge, update this doc or add a dated appendix here.

---

## Change Log

- **2026-05-29 (v10):** Added chip-AEC comparison profile legs
  (`chip_aec_150`, `chip_aec_210`, `xvf_raw0_webrtc_aec3`,
  `xvf_raw0_dtln`) to the leg-aware quality-analysis contract.
- **2026-05-28 (v9):** Added waveform-fusion experiment guidance and
  first-session result. The script is offline-only and must be judged
  against score/decision fusion plus hard negatives.
- **2026-05-28 (v8):** AEC3 sweep slots are now source-aware. New
  recorder-created sweep sessions default to USB-fed stream-delay
  variants and metadata records `aec3_sweep_source`; older sessions
  without that field remain XVF-fed.
- **2026-05-28 (v7):** Added runtime-configured AEC3 sweep slots
  (`aec3_variant_1`..`3`) so labels/knobs can change without full
  deploys while metadata records the exact config hash.
- **2026-05-27 (v6):** Retargeted corpus-only AEC3 sweep legs to
  isolate dominant-near-end detection effects (`hf_slow_only`,
  `edge_combo`, `gentle_dnd`).
- **2026-05-27 (v5):** Retargeted corpus-only AEC3 sweep legs to
  keep `hf_relaxed` and `slow_attack` while adding the combined
  `edge_combo` variant.
- **2026-05-27 (v4):** Retargeted corpus-only AEC3 sweep legs to
  edge-preservation under far+music (`hf_relaxed`, `nearend_fast`,
  `slow_attack`).
- **2026-05-27 (v3):** Retargeted corpus-only AEC3 sweep legs to the
  HF-preservation 2×2 (`hf_relaxed`, `hf_mask_upstream`,
  `hf_wide_open`).
- **2026-05-27 (v2):** Added corpus-only AEC3 sweep legs to the
  leg-aware quality-analysis contract.
- **2026-05-27 (v1):** Initial methodology doc for deterministic and
  advisory quality analysis of short wake-corpus clips, including tear,
  clipping, AGC, spectral, cross-leg, scoring, and review-package plans.

Last verified: 2026-05-29 (v10 - chip-AEC comparison profile legs added)
