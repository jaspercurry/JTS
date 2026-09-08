# Measurement Quality

Measurement quality determines which claims the evidence supports. A corrupt
capture cannot establish a response. An uncertain but valid result can still
inform a bounded experiment. Read the [methodology's setup guidance](../../../../docs/tuning-methodology.md#1-prove-the-measurement-setup)
for uncertainty types and the [doctrine](../../../../docs/measurement-loop-doctrine.md)
for the distinction between physical protection and evidence limits.

## Evidence To Read

- Mic calibration, its frequency coverage, orientation, sign convention, and
  capture gain. Relative dBFS level is not calibrated acoustic SPL.
- Capture identity, sample rate, channel count, duration, clipping, and SNR.
  Keep measured noise separate from an assumed instrument floor.
- Browser processing and selected-device metadata when present. Requested
  settings alone do not prove that automatic gain, echo cancellation, or noise
  suppression was disabled; a device label alone does not prove mic identity.
- Per-position curves, spatial average, and same-position repeats. Seat spread
  describes different sound fields; it is not repeated measurement error.
- Raw recording and stimulus, analysis window, smoothing, and timing basis.
  Magnitude-only evidence cannot establish phase alignment. A short clean gate
  limits the lowest frequency a speaker-only claim can cover.
- Target, filter prediction, played graph, and measured verification. Keep
  expected change distinct from what a new capture actually established.

Room bundles expose these facts through the existing intake and evidence
readers; use `jasper-calibration-agent --help` and
`jasper-correction-bundle --help`. Missing evidence remains unavailable.

## Choosing A Follow-Up

Clipping, low SNR, a wrong mic, or an unsupported timing claim can require a
new capture before answering the affected question. A small dB difference
without suitable calibration remains uncertain. A dip that changes with pose
or a verify result that differs from prediction calls for interpretation;
another capture is useful only if it answers the remaining question.

Retain raw captures and reproducible stimulus with their identities. Derived
curves, impulse responses, and analysis settings let later tools inspect the
same evidence. They cannot recover missing captures or remove systematic
calibration and room errors merely by repeating the analysis.

## Sources

- [2026-05-25 research](../../../../docs/research/2026-05-25-calibration-agent/README.md)
- [2026-05-27 research and syntheses](../../../../docs/research/2026-05-27-room-correction-research/README.md)
- [HouseCurve file formats](https://housecurve.com/docs/manual/file_formats)
- [Dayton Audio Microphone Calibration Tool](https://support.daytonaudio.com/MicrophoneCalibrationTool)
- [Dayton Audio iMM-6C](https://www.daytonaudio.com/product/1974/imm-6c-idevice-usb-c-calibrated-microphone)
- [miniDSP UMIK-1](https://www.minidsp.com/products/acoustic-measurement/umik-1?format=pdf&type=raw)
- [miniDSP UMIK-2 manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf)
