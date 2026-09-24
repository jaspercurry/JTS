# ADR-0354: Near-field driver takes are reference evidence, one driver per pose

- **Date:** 2026-09-23
- **Status:** Accepted. Supersedes in part [ADR-0192](0192-the-campaign-is-the-validation.md) §3
  (R-3's capture is built; its splice stays the named hole
  `near_field_splice_not_implemented`; R-4 stays parked). Amends
  [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md) §4,
  [ADR-0278](0278-measurement-purpose-is-independent-of-position.md) and
  [ADR-0200](0200-the-measurement-toolbox-is-microphone-only.md) point 3.

## Context

On jts3 the cardioid band (≈90–400 Hz) sits below what a gated far-field
take resolves in a 3 m room, so a close microphone plus a radiation model is
the only room-free view of it ([#5684](https://github.com/jaspercurry/JTS/issues/5684)).
No program played one chosen woofer with the rest silent: the stopgap runs
(ADR-0353) played both woofers or the whole candidate, and the pair check
refused their takes. The drivers graph already names every physical output by
measurement target id (ADR-0316) and parks the ones a take does not route;
what was missing was a take naming the one target it plays.

## Decision

1. **A pose names the one driver it plays exactly when it is a reference
   near-field pose.** The driver is a measurement target id (`woofer`,
   `woofer:rear`, `tweeter`, …). It is part of the pose's place, so the front
   and rear woofer at one distance are two placements, and of the banked pose
   key, as a suffix only when set, so no existing key moves. The far-field
   one-driver take is [#5696](https://github.com/jaspercurry/JTS/issues/5696).
2. **Near-field driver takes are purpose `reference`.** No reader of a tuning
   purpose admits them: not the seat reference, room, bass, the rear-pair
   level match or the speaker packet. The splice
   ([#5695](https://github.com/jaspercurry/JTS/issues/5695)) opts in by name.
3. **A driver's pose is a close pose, 0 < d ≤ 100 mm** from the dust-cap
   centre along the driver's axis. Distances are per-pose layout data,
   recorded on every take; nothing downstream reads the bundled 15/30 mm as
   constants.
4. **The take plays that driver alone** through the protected neutral drivers
   graph (raw driver plus protection; no bass extension, rear stage or
   loudness): pilots and three bit-identical sweeps from the driver's floor to
   about 2 kHz, about 8 s each, with no silence over 0.5 s after the first
   sound, so a signal-sensing amplifier stays awake.
5. **The take is read ungated.** The pose's driver sets the `near_field` gate
   exemption: at the cone the room is about 40 dB down, and the gate would cut
   the band the take exists for. It keeps the long window a seat take uses,
   because a protected woofer rings past the 60 ms arrival window, and claims
   no validity floor.

## Consequences

- Each driver is measurable on its own near the cone, on a 2-way and on a
  cardioid cabinet, beside the far-field programs rather than instead of them.
- Near-field evidence stays out of every fit until the splice lands.
- Preflight refuses a plan naming a driver the speaker does not declare.
- Stereo presets are not offered a near-field program until
  [#5697](https://github.com/jaspercurry/JTS/issues/5697): a target id names a
  role within a speaker group, so it would play in both cabinets.
- Rejected: purpose `speaker` (it adds a whole-speaker timing take first,
  which plays the tweeter into a 15 mm microphone, and it would replace the
  latest speaker round); purpose `bass` (bass tables, ADR-0260 §3); a new
  purpose (it would duplicate `reference`).
