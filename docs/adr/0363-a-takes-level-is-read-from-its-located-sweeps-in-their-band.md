# ADR-0363: A take's level is read from its located sweeps, in their band

- **Date:** 2026-09-24
- **Status:** Accepted. Supersedes (partial) [ADR-0361](0361-a-near-field-take-levels-itself-to-80-db-at-the-microphone.md)
  §3: where a take's level is read, and how one retake is solved.
- **Context:** A near-field take was levelled on `max_window_db_spl`, the loudest broadband 21 ms
  period of its whole capture, pre-roll included. On 09-24 the street already read 55–57 dB there
  before the sound, so every quiet opener read high, every solve undershot, and 4 takes cost 14 plays
  ([#5714](https://github.com/jaspercurry/JTS/issues/5714)). The same reading lets a burst during a
  take land it in band by accident.
- **Decision:**
  1. A take's level is the loudest 21 ms window of each located sweep, in the sweeps' shared band,
     the median across its sweeps. Its floor is the median window of the silence before its pilots,
     in the same band. `jasper/audio_measurement/level.py` owns the reading and the solve.
     `analyze_program_capture` is the one writer of a take's level (`stimulus_level`, dBFS), and the
     take's SPL block records the `sens_factor_db` its dB SPL figures use.
  2. A reading is trusted at 10 dB or more over its floor, where the room adds at most 0.46 dB
     (ISO 3744's K1 correction). One solve steps 1:1 from the loudest trusted reading to 1 dB under
     the target: raised at most 15 dB, lowered freely, never above its ceiling. With none trusted it
     solves from the loudest reading, which the room inflates, so the take lands at or under the
     target.
  3. The near-field check reads this level instead of `max_window_db_spl`. The 85 dB stop, its
     broadband reading and the rest of the SPL block are unchanged.
- **Consequences:** Sound before or after the sweeps, or outside their band, no longer moves a
  take's level, and a burst in one sweep leaves the median alone. A retake lands 1 dB under the
  target instead of on it. The verdict's level evidence is `level_db_spl` and `level_floor_db_spl`;
  the near-field view reads the old `max_window_db_spl` on takes banked before this. Not decided
  here: the probe that replaces the opener take. Rejected: subtracting the floor from the reading
  (a floor read wrong then moves an absolute level); per-window FFT band levels (a 21 ms window's
  47 Hz bins cannot hold a woofer's 20 Hz band edge); a deconvolution read (not needed while a take
  reads 20 dB or more over the room).
