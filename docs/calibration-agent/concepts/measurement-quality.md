# Measurement Quality

> **Status: current guidance, updated 2026-05-26.** This file captures
> the measurement checks the correction engine and a future
> calibration/tuning agent should understand before making
> recommendations.

## Operational Summary

Bad measurements make confident bad filters. JTS should treat
measurement quality as the first gate in any correction or tuning
flow. The system should prefer asking for a re-measurement over
designing filters from ambiguous data.

## What JTS Can Do Today

- Play a deterministic 48 kHz exponential sine sweep through the same
  path real music uses.
- Capture mono browser audio with echo cancellation, noise suppression,
  and auto gain control requested off.
- Verify browser-reported capture settings before enabling the
  measurement controls.
- Auto-level the speaker against the browser mic's observed RMS.
- Average multiple listening-position captures in linear power.
- Pick the browser input device, fetch known mic
  calibration files by serial, upload unsupported mic calibration
  files manually, and record mic/device metadata in session bundles.
- Assess capture quality before deconvolution, including clipping,
  low level, short captures, sample-rate mismatch, uncalibrated mic
  warnings, and browser-processing warnings.
- Persist target profile, correction strategy, and a design audit into
  session bundles for later review.
- Build a first-pass confidence report from completed position count,
  calibrated-mic presence, input-device metadata, capture-quality
  issues, per-position variance, and strategy gates.

## Quality Flags To Add

- Research-tuned confidence thresholds for per-filter correction
  decisions, rather than today's intentionally simple report-level
  heuristics.
- Sweep SNR estimate against pre-sweep ambient noise.
- Repeatability score between nearby positions or repeated sweeps.
- Calibration coverage warning when the selected curve does not span
  the full analysis band.
- Browser device mismatch warning when the selected USB mic is not the
  track actually granted by `getUserMedia`.
- Vendor lookup provenance: fetched URL, file hash, model, serial hash,
  orientation, and parser sign convention.
- Windowing provenance: impulse window, smoothing, FDW settings where
  available, and whether phase/group-delay claims are supported.
- Timing-reference provenance: electrical loopback, acoustic timing
  reference, or magnitude-only capture. Mark magnitude-only data
  unsafe for driver alignment or mixed-phase decisions.
- Early-arrival / ETC warning when reflections arrive too close to
  the direct sound for the intended analysis.

## Bundle Artifacts To Preserve

The deep-research reports were unanimous that raw data retention is
the highest-leverage future-proofing move. Keep enough data that a
future FIR or LLM-advisor pass can rerun the analysis without asking
the user to re-measure:

- raw sweep recording, preferably float PCM;
- sweep/excitation configuration and identifier;
- derived impulse response before smoothing or gating;
- per-position captures and derived responses;
- spatial-average method and settings;
- target curve data;
- generated filters and predicted corrected response;
- applied headroom offset;
- mic calibration record and input-device metadata;
- audit log of deterministic decisions and any LLM-advisory request.
- visualization-ready artifacts: impulse, ETC/early-arrival view,
  step response, waterfall/spectrogram or decay view, excess group
  delay, and THD-vs-frequency where ESS harmonic separation supports
  it.

## Agent Guidance

The agent should say "measure again" when:

- the mic was uncalibrated and the recommendation depends on a small
  dB distinction;
- the selected device label does not look like the intended mic;
- clipping or very low SNR is detected;
- a narrow null appears at one position but not in the spatial average;
- verify disagrees with design in a way consistent with position
  variance rather than a filter failure.
- the requested advice depends on phase, delay, group delay, or
  crossover alignment but the bundle lacks timing-reference proof.

## Sources

- 2026-05-25 deep-research reports.
- [HouseCurve file formats](https://housecurve.com/docs/manual/file_formats)
- [Dayton Audio Microphone Calibration Tool](https://support.daytonaudio.com/MicrophoneCalibrationTool)
- [Dayton Audio iMM-6C](https://www.daytonaudio.com/product/1974/imm-6c-idevice-usb-c-calibrated-microphone)
- [miniDSP UMIK-1](https://www.minidsp.com/products/acoustic-measurement/umik-1?format=pdf&type=raw)
- [miniDSP UMIK-2 manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf)

Last verified: 2026-05-26
