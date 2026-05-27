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

**The first quality analyzer is shipped.**
`scripts/analyze-wake-corpus-quality.sh` runs the deterministic quality
pass and writes `metrics.csv`, `cross_leg.csv`, `events.json`, and
`summary.md`. It now includes LPC-confirmed transient damage hints and a
composite perceptual-damage review score. Use it after rsyncing the Pi
corpus locally; keep heavy analysis off the 1 GB Pi.

**Quality analysis starts with deterministic signal metrics.** For 1-3 s
wake-word clips, trust sample-domain and frame-domain facts first:
exact clipping, near-clipping, flat-top runs, DC offset, RMS, crest
factor, spectral flatness, high-band energy, Nyquist-edge energy, local
MAD transient candidates, and LPC residual outliers.

**Cross-leg comparison is the superpower.** JTS captures simultaneous
legs for the same utterance: XVF-derived raw/WebRTC-AEC/DTLN/raw0,
optional cheap-USB raw/WebRTC-AEC/DTLN, and reference. A suspicious
event that exists only in one processed leg means something very
different from an event that appears at the same aligned timestamp in
every mic leg.

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
| `usb_raw` | Cheap USB mic capture with no JTS software processing | No | Watch hardware AGC/limiter state carefully. |
| `usb_webrtc` | Cheap USB mic through software WebRTC AEC | No | Corpus-only experiment for lower-cost mic paths. |
| `usb_dtln` | Cheap USB mic through DTLN | No | Optional and resource-sensitive. |
| `ref` | Speaker playback reference | No | Use for AEC/post-hoc experiments and alignment; list last in playback UI. |

Leg names in future metadata should stay stable and explicit. Display
labels can be friendlier, but the machine-readable names should not
encode vague words like "enhanced" without saying what processing ran.

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

## 11. Current Tooling And Implementation Plan

Current command:

```sh
bash scripts/analyze-wake-corpus-quality.sh \
  data/enrollment_positives --latest
```

For a specific session:

```sh
bash scripts/analyze-wake-corpus-quality.sh \
  data/enrollment_positives --session 20260527T131954Z-7469
```

The first-pass analyzer currently computes:

- sample-domain metrics: duration, peak, approximate true peak,
  RMS, crest factor, DC offset, exact clipping, near-clipping,
  flat-top runs, near-zero/dropout runs, and repeated-sample runs;
- spectral metrics: flatness, high-band ratio, Nyquist-edge ratio,
  and spectral flux;
- envelope metrics: RMS-envelope modulation peak/prominence and
  crest-vs-RMS correlation;
- transient candidates from local MAD on sample deltas;
- LPC residual outliers on 30 ms speech frames, with confirmed events
  only when residual outliers align with local-MAD transient candidates;
- a bounded perceptual-damage review score that combines LPC-confirmed
  events, clipping/flat-top evidence, transient density, Nyquist-edge
  energy, and spectral flux, with reference-leg review priority
  down-weighted because the reference is not clean speech;
- cross-leg deltas and alignment confidence for sibling legs such
  as `usb_webrtc-usb_raw`, `usb_dtln-usb_raw`, `on-off`, and
  `dtln-off`, including LPC-confirmed and damage-score deltas.

The analyzer is a review-prioritization tool. It should sort clips for
listening review and explain why, not silently reject clips.

Shipped Phase 0/1:

- Deterministic analyzer CLI and shell wrapper.
- CSV/JSON/Markdown artifacts.
- Synthetic tests for clipping, transient candidates, LPC-confirmed
  damage, noisy negative control, corpus artifact writing, and
  latest-session filtering.

Next Phase 1b: broader fixtures and detector calibration.

- Add synthetic fixtures for soft clipping, click bursts, dropout,
  repeated samples, DC offset, AGC pumping, pure fricative negative,
  plosive negative, aliasing, and processed-leg-only artifact.
- Lock expected detector behavior before running on the real corpus.

Phase 2: cross-leg analyzer and HTML review.

- Group utterances by metadata.
- Improve sibling-leg alignment and event coincidence tables.
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

---

## 13. Source Notes

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

- **2026-05-27 (v3):** Added LPC-confirmed transient damage detection,
  a bounded perceptual-damage review score, cross-leg damage deltas, and
  synthetic positive/negative tests.
- **2026-05-27 (v2):** Added the shipped first-pass analyzer command,
  outputs, current metric coverage, and next implementation phases.
- **2026-05-27 (v1):** Initial methodology doc for deterministic and
  advisory quality analysis of short wake-corpus clips, including tear,
  clipping, AGC, spectral, cross-leg, scoring, and review-package plans.

Last verified: 2026-05-27 (v3 - LPC-confirmed perceptual damage layer
shipped and checked against the 2026-05-27 latest-session corpus)
