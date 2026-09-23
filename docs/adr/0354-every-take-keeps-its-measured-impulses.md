# ADR-0354: Every take keeps its measured impulses

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** A take's analysis deconvolves every sweep it plays into an
  impulse, then kept only the raw recording and a 121-point, 1/12-octave
  curve with wrapped phase (`pose_curve.py`). Nothing could read a take's
  impulse, phase or group delay afterwards without deconvolving the WAV
  again: the shared reader rebuilt a summed take against the whole program
  file and refused every driver role except a branch take's (the 2026-09-22
  toolbox review, R1-16; issue #5659). Room EQ Wizard keeps the impulse as
  the measurement and derives every view from it.
- **Decision:**
  1. Every analysed sweep's deconvolved impulse rides its response as a
     `RecordedImpulse`: the raw deconvolution, with no microphone correction
     and no configured-path composition, kept through
     `DEFAULT_VERIFY_TAIL_S` past the direct peak, with the sample of the
     segment's scheduled start (`origin_index`) and the drift accumulated
     there (`clock_shift_samples`). `(index - origin_index -
     clock_shift_samples) / rate` is one clock for every role and repeat of
     one recording.
  2. The capture host writes a take's impulses to `impulses/<take-id>.npz` in
     its bundle, recorded in the artifact manifest (kind
     `jts_speaker_take_impulses`, depending on the capture WAV), and indexes
     them on the take record under `impulses` (`jts_take_impulses/1`). A
     failed write costs only the saved copy and logs
     `active_speaker.take_impulses_not_saved`; the WAV stays.
  3. `round_captures` reads a take's kept impulse first, for any role the take
     recorded, and needs no program file to do it. A take banked before this
     keeps the old routes.
- **Consequences:** Every view can read a driver's impulse, phase and group
  delay from what the analysis measured, on any speaker layout, without
  deconvolving again on the Pi. Each kept impulse costs about 150 KB
  (float32, about 0.75 s), so a 13-pose speaker round grows by about 13 MB,
  inside the bundle store's retention. Across recordings the origin is each
  take's own anchor, so only relative time compares (ADR-0355). Rejected:
  keeping the full-resolution complex response instead (it is the impulse's
  transform and loses the time axis), and JSON sample lists like the branch
  diagnostic's (several times larger and slow to parse).
