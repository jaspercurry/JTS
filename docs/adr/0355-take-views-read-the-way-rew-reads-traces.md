# ADR-0355: Take views read the way REW reads traces

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** The toolbox had no impulse, phase or group-delay view, no
  general A−B and no predicted-vs-measured overlay (issue #5659; the
  2026-09-22 toolbox review, R1-14, R1-16 and R1-17). Candidates, bass,
  room grading and the forward model each differenced two curves their own
  way. Room EQ Wizard reads every trace through a stated window and
  smoothing, compares traces as |B|/|A| in dB, and compares phase or delay
  only against a shared timing reference, which JTS takes do not have
  across recordings (ADR-0354).
- **Decision:**
  1. Three views read a take's kept impulse by take id and role:
     `impulse` (arrival, ISO 3382 onset, peak-to-noise, reflection-free
     time, energy-time curve), `group-delay` (phase, group delay and excess
     group delay by octave, time zero at the direct peak) and `compare`
     (b minus a in dB).
  2. A take is read through the window its own analysis used unless the
     caller names one. `compare` reads both sides through the shorter of the
     two, over the band both trust, at one smoothing (1/6 octave by default).
  3. Across recordings only magnitude compares. `compare` reports relative
     arrival only for two roles of one recording, and discloses a calibration
     or capture-basis difference instead of refusing it.
  4. `compare --a-preview` takes a `judge --preview --out` forecast as side
     A and reads the measured take through the forecast's own window, with
     the level removed (the forecast has no absolute level).
  5. One kernel function, `series_stats.curve_difference`, differences two
     curves; `candidates` and the forward model's reconstruction use it too.
- **Consequences:** An agent can ask the questions a person asks REW first
  without writing analysis code, and every answer names its window and
  smoothing, so two answers compare only when those match. Phase and group
  delay carry the microphone's own phase, since calibration is magnitude
  only. Not built: decay views (RT60, waterfall, spectrogram). `bass-compare`
  and `room-grade` keep their own difference rules until one formula per
  figure is chosen (#5661).
