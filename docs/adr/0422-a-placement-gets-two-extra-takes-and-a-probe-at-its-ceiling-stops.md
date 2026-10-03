# ADR-0422: A placement gets two extra takes, and a probe at its ceiling stops

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0365](0365-a-drivers-pose-finds-its-level-with-a-probe.md) §3
  ("if it does not, the probe asks for the microphone again (`snr_floor`)") for a probe that played
  to its ceiling. Supersedes in part the owner's bounded-retry ruling of 2026-08-03
  ([#2086](https://github.com/jaspercurry/JTS/issues/2086), recorded in
  `docs/historical/crossover-measurement-v2-campaign-record.md`): its count of three extra attempts.
  Its one pooled ledger per placement stays.
- **Context:** Finding F10 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): retakes that cannot
  help keep playing. A placement had two caps in `crossover_v2/admission.py`: three extra takes an
  operator could ask for (ruling #2086), and six of any charge. The six came with the executor's
  ledger on 2026-09-11 to bound USB-fault work; no ADR or ruling set it. On jts3, a dead rear woofer
  cost about 16 min across runs 2 and 3 of [#6113](https://github.com/jaspercurry/JTS/issues/6113):
  its probes played to full scale and read only the room, each asked for the microphone again, and
  the run then waited out the 10-minute hold. A failed room round played 12 of 12 takes that failed
  the same way at the same level.
- **Decision:**
  1. **One cap.** A placement gets its first take free and at most two more
     (`MAX_EXTRA_ATTEMPTS_PER_POSITION`), of every charge that counts: an operator's retry and a
     speaker's retake alike. A `replay` charge, a level probe's next play, stays free. A run's
     operator share (`retries_per_pose`) defaults to the same two and never raises the cap.
  2. **A probe at its ceiling stops the run.** A level probe that its SPL watch did not stop played
     every burst, up to its take's ceiling (ADR-0365 §1). If it then has no reading it trusts
     (`snr_floor`) or locates no burst (`locate_failed`), no more level is available. Its verdict is
     `level_unreachable` with `next="stop"`, and the run stops. The verdict tells the two cases apart
     by the capture's SPL block: `stopped_at_db_spl` is there only when the watch ended the play at
     the ramp bound (ADR-0365 §2). A probe the watch stopped keeps ADR-0365 §3 and asks for the
     microphone again: the room was loud, not the driver weak. A muted output still stops as
     `measurement_output_muted`. The rule holds for every level probe: a driver's pose, a branch,
     a close set and a run's first spot.
- **Consequences:**
  - This change only removes plays. No level, ramp bound, stop, clamp or driver cap changes.
  - A placement plays at most two charged plays after its first, where it could play six. A driver's
    take that lands off its target gets two level retakes.
  - A dead driver costs one probe. The run stops with copy that names the driver's wiring and amp,
    in place of a new placement prompt and a 10-minute hold. The stop verdict keeps the probe's
    `level_db_spl` and `level_floor_db_spl` when it read a burst, and
    `event=active_speaker.level_probe` logs it.
  - A room that is loud for the whole probe, but under the ramp bound, also stops the run. The copy
    also tells the household to quiet the room.
  - Not decided here: stopping after two attempts with the same fault and the same reading,
    ADR-0183's repeatability idea ([#1873](https://github.com/jaspercurry/JTS/issues/1873)). It needs
    the executor (`plan_run.py`) and follows later.
  - Rejected:
    - Keeping a separate operator cap under the placement's cap. Two caps let a placement play takes
      that cannot help, and one constant says the rule once.
    - Stopping on every probe with no reading it trusts. A room sound that reaches the ramp bound
      stops the probe early (ADR-0365's Consequences), and a new attempt can then read it.
