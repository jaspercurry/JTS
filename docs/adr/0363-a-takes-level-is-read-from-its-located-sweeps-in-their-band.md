# ADR-0363: A take's level is read from its located sweeps, in their band

- **Date:** 2026-09-24
- **Status:** Accepted. Supersedes (partial) [ADR-0361](0361-a-near-field-take-levels-itself-to-80-db-at-the-microphone.md)
  §3: where a take's level is read, and where one retake aims.
- **Context:** A near-field take was levelled on `max_window_db_spl`, the loudest broadband 21 ms
  period of its whole capture, pre-roll included. On 09-24 the street already read 55–57 dB there
  before the sound, so every quiet opener read high, every solve undershot, and 4 takes cost 14 plays
  ([#5714](https://github.com/jaspercurry/JTS/issues/5714)). The same reading lets a burst during a
  take land it in band by accident.
- **Decision:**
  1. A take's level is the loudest capture period (the 21 ms the SPL stop judges) of each located
     sweep of its one driver, in the sweeps' band, the median across its sweeps. Its floor is the
     median period of the silence before its pilots, in the same band.
     `jasper/audio_measurement/level.py` owns the reading; `analyze_program_capture` is its one
     writer on a take (`stimulus_level`, dBFS), and the take's SPL block records the
     `sens_factor_db` its dB SPL figures use.
  2. The near-field check reads this level instead of `max_window_db_spl`. One level retake steps
     1:1 to 1 dB under the target through `ramp.capped_gap_step_db`, raised at most 15 dB.
  3. The 85 dB stop, its broadband reading and the rest of the SPL block are unchanged.
- **Consequences:** Sound before or after the sweeps, or outside their band, no longer moves a
  take's level, and a burst in one sweep leaves the median alone. A retake lands 1 dB under the
  target instead of on it. The verdict's level evidence is `level_db_spl` and `level_floor_db_spl`,
  so the floor under every reading is on the record; takes banked before this show no level in the
  near-field view. Not decided here: the probe that replaces the opener take. Rejected:
  subtracting the floor from the reading (a floor read wrong then moves an absolute level);
  per-period FFT band levels (a 21 ms period's 47 Hz bins cannot hold a woofer's 20 Hz band edge);
  a deconvolution read (not needed while a take reads 20 dB or more over the room).
