# Wake-corpus audio quality — operational spine

Methodology for programmatic quality analysis of the deliberate wake-corpus
WAVs captured by the browser recorder at `http://jts.local/wake-corpus/`. Read
this before building or extending a corpus quality analyzer.

The governing decisions are **ADR-0135** (the analyzer ranks clips for human
review and never rejects one; deterministic metrics first, neural predictors
advisory, cross-leg coincidence as arbiter) and **ADR-0136** (waveform fusion
must beat score fusion across sessions and hard negatives before it is a
candidate). The 2026-05→07 build record — phased plan, AEC3 sweep-slot
retargeting, metadata-contract growth, the one fusion result — is
[historical/wake-corpus-quality-methodology-2026-05.md](historical/wake-corpus-quality-methodology-2026-05.md).

## What exists today

| Tool | Does |
|---|---|
| `scripts/audit-wake-corpus.sh` | Deliberately basic: "did the corpus record what we think it recorded?" — metadata, expected legs, coverage, WAV format, duration, RMS, peak. |
| `scripts/analyze-wake-corpus-quality.sh` (→ `scripts/_analyze_wake_corpus_quality.py`) | The deterministic analyzer. Laptop-side pass over an rsynced corpus emitting `metrics.csv` / `cross_leg.csv` / `events.json` / `summary.md`; Tier A + selected Tier B; LPC-residual + cross-leg confirmation of transients. Fixtures: `tests/test_analyze_wake_corpus_quality.py`. |
| `scripts/_waveform_fusion_experiment.py` | Offline-only hypothesis harness (ADR-0136). |
| `scripts/export-wake-corpus-bundle.sh`, `scripts/build-wake-feature-bank.sh` | **Training dataset assembly, not QA.** Run after the audit; do not fold their semantics into the quality analyzer. |

HTML review packages and the neural (Tier C) metrics have not been built.

## Scope

Covers deliberate recordings under `/var/lib/jasper/enrollment_positives/` on
the Pi and `./data/enrollment_positives/` after rsync: Jarvis positives in
Session A and held-out Session B, hard negatives from the same recorder,
per-utterance per-leg WAV QA, and diagnosis of tearing, clipping, AGC pumping,
limiting, dropouts, high-band roughness and AEC/NS artifacts.

Not this doc: production wake-event telemetry
([HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md)); wake-model scoring
([testing-tooling.md](testing-tooling.md)); AEC topology
([HANDOFF-aec.md](HANDOFF-aec.md)); the recording protocol and training plan
([HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md)).
The `ref` leg is a playback reference for AEC experiments and alignment — it is
**not** a clean recording of Jasper's voice, and must never be treated as a
clean-speech target.

## Recorder legs

The analyzer must be leg-aware; never collapse WAVs into a flat pile. Tokens
are frozen on-disk keys owned by `jasper/wake_legs.py`.

| Leg token | Meaning | Production wake input? |
|---|---|---|
| `on` | XVF mic after software WebRTC AEC3 (label it WebRTC AEC, not just "WebRTC") | yes |
| `off` | XVF chip-direct stream — the raw-ish production leg | yes |
| `dtln` | XVF path through the DTLN neural AEC | yes when enabled |
| `chip_aec_150` / `chip_aec_210` | XVF on-chip AEC ASR output, fixed gated 150°/210° beams | yes in chip-AEC mode |
| `raw0` | Truly raw XVF mic 0 / chip channel 2 — no chip DSP, no software AEC | no |
| `xvf_raw0_webrtc_aec3` / `xvf_raw0_dtln` | `raw0` through software AEC3 / DTLN, for chip-vs-software comparison | no |
| `usb_raw` / `usb_webrtc` / `usb_dtln` | Cheap USB mic, unprocessed / WebRTC AEC / DTLN | no |
| `ref` | Speaker playback reference. List last in any playback UI | no |
| `aec3_variant_1..3` | Corpus-only parallel AEC3 sweep slots (`jasper/aec_sweep.py`) | no |

Two things follow:

- **`chip_aec_150` and `chip_aec_210` are analyzed separately** — never
  averaged. Orientation and room geometry are exactly what pairing them
  reveals. Keep them explicit even when `chip_aec_150` duplicates `on`, which
  happens when the primary stream is repointed to the selected chip beam.
- **The sweep slots are stable names for an unstable hypothesis.** What a slot
  meant in a given session lives in that session's sidecar
  (`aec3_sweep_source`, `aec3_sweep_variants`, `aec3_sweep_config.hash`), not
  in any doc. Read it; never infer a slot's meaning from its number.

### Reading session metadata

Prefer stored metadata over filename heuristics, in this order:

1. `capture_plan.legs` — the canonical layered graph (physical mic, native
   stream, source channel, software/hardware transform, required bridge
   outputs/env, wake/corpus role, coarse resource load, expected UDP legs,
   `plan_id`, mic/DAC/reference fingerprints). Clips also carry
   `capture_plan_id` and a clip-start conformance result.
2. `audio_context.corpus.selected_legs` / `.leg_details`.
3. Legacy `enabled_legs` / `files` maps, for old sessions.

Use `capture_health` + `capture_plan_conformance` to tell a **deliberately
absent** leg from a **compromised** recording. Sessions predating
`audio_context` or `capture_plan.plan_id` remain valid data: display the
absence, never fail the corpus over it.

## Metric tiers

### Tier A — always computed

Cheap, deterministic, reliable on 1-3 s mono 16 kHz int16 clips.

| Metric | Catches | First threshold, tune from data |
|---|---|---|
| Peak dBFS | Hot recordings | warn above -1 dBFS |
| True peak dBTP | Inter-sample clipping risk | warn above -1 dBTP (offline only) |
| Exact clip count | Digital clipping | any `-32768`/`32767` sample is critical |
| Near-clip count | Limiter/overload near the rails | sample mass within 0.5, 1, 3 dB of FS |
| Flat-top runs | Hard clip or limiter ceiling | ≥ 3 samples suspicious, ≥ 6 severe |
| RMS + gated RMS | Too quiet / too hot speech | prefer VAD-gated speech RMS |
| Crest factor | Compression, limiting, impulses | low ⇒ compression/clip; very high ⇒ impulse |
| DC offset | Bad coupling, biased frontend | warn above `abs(mean/FS) > 0.001` |
| Dropout / silence spans | Stream stalls, packet loss | zero-runs inconsistent with speech |
| Repeated-sample runs | USB/buffer glitch | repeated non-zero samples across frames |
| Local MAD on sample delta | Tear/click candidates | **candidate generator only** |

### Tier B — diagnostic, needs context

Spectral flatness (p50/p90 over VAD-active frames); spectral centroid/rolloff
(phoneme-biased on short clips — compare legs); high-band energy ratio;
Nyquist-edge energy (7.2-8 kHz, catches bad resampling/aliasing); spectral flux;
LPC residual outliers (prefer over raw derivative thresholds on speech); RMS
envelope FFT for AGC pumping (coherent 1-10 Hz gain movement);
crest-vs-RMS correlation (negative ⇒ gain riding); cross-leg alignment
(GCC-PHAT first, sliding alignment if USB drift shows).

### Tier C — advisory, offline only

SQUIM objective is the best first candidate (native 16 kHz, reference-less
STOI/PESQ/SI-SDR estimates, still high-variance on very short clips). DNSMOS /
P.835 give usable *relative* ranking but repeat short clips to fill their model
window, which biases absolute MOS and can amplify periodic artifacts. NISQA,
SRMR, UTMOS/MOSNet are out-of-domain for 1-3 s wake clips — relative features
at best. None of these may be the sole reason a clip is flagged (ADR-0135).

## Detecting the specific failures

### Tears and clicks

A "tear" is a short, broadband, unphysical discontinuity: USB underrun, DMA
glitch, buffer repeat/drop, or a processing frame-boundary artifact. Plosives
and fricatives look similar in raw samples, so a single derivative threshold is
not enough.

1. Generate candidates with local MAD/Hampel outlier detection on `delta x`
   (optionally the second derivative too).
2. Confirm with LPC prediction residual outliers on short speech frames —
   order 10-12 for 16 kHz speech.
3. Require spectral-flux or high-band corroboration before a major flag.
4. Suppress by duration and context: single-sample impulses are more suspicious
   than 10-50 ms consonant evolution; unvoiced consonants are not tears by
   default; events recurring every 10/20/30 ms are frame-boundary artifacts.
5. Arbitrate by cross-leg coincidence — all mic legs at the same aligned time
   ⇒ speech or a room event; `usb_raw` only ⇒ USB hardware path;
   `usb_webrtc` only ⇒ WebRTC processing; `dtln`/`usb_dtln` only ⇒ neural AEC.

Emitting candidate timestamps with confidence beats pretending the labels are
exact.

### Clipping and overload

Digital clipping is easy (count exact full-scale samples). Analog frontend
overload, firmware AGC or firmware limiting on the cheap USB mic can distort
well before int16 full scale, so detect both: exact FS count; near-clip mass at
0.5/1/3 dB; flat-top run length at exact-LSB and near-peak tolerances; peak
histogram concentration around a target level; crest-factor collapse;
asymmetric positive/negative peaks; DC offset; high-band bursts at near-peak
events. Report analog overload as **"saturation suspected"** — reserve "digital
clipping" for clips with actual full-scale samples.

### AGC and limiting

USB firmware controls are not always truthful: verify with audio even when AGC
is toggled off through ALSA. Per clip — 10 ms RMS envelope retained at 100 Hz;
crest factor in 250 ms windows; correlation between windowed RMS and crest;
pause noise floor before/after speech where pauses exist; envelope modulation
spectrum (coherent 1-10 Hz peaks); peak distribution across the session's
utterances. Across paired captures — USB raw against XVF chip-direct for the
same utterance, and AGC-on vs AGC-off sessions only when environment and music
level were controlled. Record the ALSA control state in the report, but **audio
evidence wins over control labels**.

### Spectral framing defaults

One consistent policy unless data proves otherwise: 16 kHz; 25 ms / 400-sample
frame; 10 ms / 160-sample hop; Hann; 512-bin FFT; aggregates at p10/p50/p90/p95
/max plus event-local values; high band 3-7.5 kHz; Nyquist edge 7.2-8 kHz (or
the top 5 % of bins). Do not overfit one global spectral threshold — a
one-syllable "Jarvis" can be dominated by its /j/, /r/, /v/ or /s/ region
depending on pronunciation. Cross-leg deltas and event-local spikes are more
actionable than absolute values.

### Cross-leg analysis

A first-class object in the output, not a post-hoc join:

1. Group by session/utterance metadata (above), never by filename guessing.
2. Resample only if needed — the corpus target is 16 kHz mono int16.
3. Align sibling legs with GCC-PHAT or normalized cross-correlation; report lag
   and alignment confidence per pair.
4. Build an event-coincidence matrix over transient candidates.
5. Compute per-metric deltas from baseline legs: `on - off`, `dtln - off`,
   `usb_webrtc - usb_raw`, `usb_dtln - usb_raw`.
6. Flag processed-leg regressions by corpus-relative percentile.

ERLE is only meaningful in far-end-only windows where the reference is active
and Jasper is not speaking — a wake utterance carries near-end speech, so ERLE
across it is not a measurement. USB and XVF clocks drift: if full-clip
alignment is weak, use sliding-window alignment and report the drift instead of
forcing one lag.

## Output

No single magic quality score — a sortable suspicion model with explanations:

```json
{
  "session_id": "20260527T...", "utterance_id": "0003",
  "condition": "music", "distance": "far",
  "legs": {
    "usb_raw": {
      "duration_s": 1.42, "peak_dbfs": -3.1, "rms_dbfs": -28.4, "crest_db": 25.3,
      "critical": [], "major": ["transient_candidate"], "minor": ["agc_suspect"],
      "events": [{"t_s": 0.618, "kind": "lpc_residual", "confidence": 0.71}]
    }
  },
  "cross_leg": {"alignment": {}, "event_coincidence": {}, "processed_regressions": []},
  "review_priority": 87,
  "review_reasons": ["usb_raw transient candidate not present in XVF legs"]
}
```

Sort by critical count, then major count, then suspicion score, then
confidence. Normalize event counts per second so short and long clips compare.

A review package, when one is built, is an HTML page per session or
high-priority subset: per-utterance metadata; one audio player per leg in
stable order with `ref` last; compact waveform with event markers; spectrogram;
RMS envelope overlay for AGC inspection; the peak/RMS/crest/clip/flat-top
summary; the cross-leg event matrix; direct WAV links. Tier C metrics belong
here — visible, contextual, and marked advisory.

## Known pitfalls

- Integrated LUFS and Loudness Range are unstable on 1-3 s clips — use
  momentary or percentile loudness if loudness is needed at all.
- Sample-delta click detectors false-positive on plosives and fricatives
  without LPC, spectral and cross-leg confirmation.
- The reference leg is not clean speech.
- ERLE computed across a wake utterance is meaningless.
- A polished aggregate score hides localized damage; one click can outweigh a
  good average MOS.

## Reading list

ITU-R BS.1770 and EBU R 128 / Tech 3341 for loudness and true peak. FFmpeg
`ebur128` / `astats` / `volumedetect` / `silencedetect` for fast sanity checks.
SciPy signal for resampling, filtering, peak finding and robust statistics;
librosa for STFT, flatness, centroid, rolloff, flux, MFCC and LPC helpers.
Essentia's ClickDetector / LPC declicking lineage, with Vaseghi (*Advanced
Digital Signal Processing and Noise Reduction*) and Godsill & Rayner (*Digital
Audio Restoration*) for the AR/LPC click models. For Tier C: TorchAudio SQUIM,
Microsoft DNSMOS / DNSMOS P.835 (`microsoft/DNS-Challenge`), NISQA
(`gabrielmittag/NISQA`), and WADA-SNR or SRMR if relative SNR/reverberation
comparisons become useful.

Last verified: 2026-08-26 (leg tokens rechecked against `jasper/wake_legs.py`;
sweep slots against `jasper/aec_sweep.py`; the shipped-tool table against
`scripts/analyze-wake-corpus-quality.sh`,
`scripts/_analyze_wake_corpus_quality.py` and
`tests/test_analyze_wake_corpus_quality.py`; the metadata contract against
`jasper/web/wake_corpus_setup.py`. Metric-tier and detector methodology is
prescriptive guidance, unchanged from the 2026-07-09 pass.)
