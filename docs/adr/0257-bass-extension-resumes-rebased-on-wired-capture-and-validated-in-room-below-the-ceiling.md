# ADR-0257: Bass extension resumes, rebased on wired capture and validated in-room below the ceiling

- **Date:** 2026-09-08
- **Status:** Accepted. Supersedes
  [ADR-0018](0018-bass-extension-stays-parked.md).

## Context

ADR-0018 parked `jasper/bass_extension/` by owner ruling on 2026-08-25 and
said only a fresh owner ruling may change that. This is that ruling.

State at HEAD. The package is about 10,100 lines of Python: a measured,
volume-scheduled Linkwitz-transform family for the household's one bass
system — the local-DAC sub chain if present, else the lowest driver way,
resolved through `output_topology.bass_management_corner_hz()`
(`jasper/output_topology.py:1077`; ADR-0236) — plus a mandatory subsonic
high-pass. Waves 1–3 are merged (numerics, profile, graph emission) with zero
production callers, enforced by `tests/test_bass_extension_plan_status.py`.
Wave 4 is partial: the pure limiter-evidence producer and the bench runner are
merged, but `jasper-bass-extension-bench --live` fails closed on two unbound
collaborators — each target's `TargetPlan` binding and the on-device
`PlayAndCapture` (`jasper/bass_extension/bench/executor.py:148`, a Protocol
with no implementation) — tracked as
[#1738](https://github.com/jaspercurry/JTS/issues/1738). Waves 5–7 are
unbuilt.

The unbuilt waves are written around the relay ADR-0222 deleted
(`docs/HANDOFF-bass-extension-plan.md` §0;
`docs/bass-extension-waves/wave-4-commissioning-backend.md:362,389`;
`bass-commissioning-ux.md:230-234`; `wave-7-hardware-validation.md:24-26`).
The measurement substrate they reuse — sweep, deconvolution, quality gate, SNR
policy, calibration, ramp, excitation admission
(the plan's "Reused as-is" table) — is transport-agnostic. The fit is
nearfield only (plan §5.3, §6.1): room gain is neither modeled nor
measured, and room-correction boosts stack acoustically with the transform's
boost with only a WARN (plan §8.4). The limiter-evidence protocol
(plan §8.2) exists because none of the retained facts bounds arbitrary
program peaks at the detector.

## Decision

Owner ruling, 2026-09-08:

1. **The program resumes.** ADR-0018's park is lifted. The park's enforcement
   tests (`tests/test_bass_extension_plan_status.py`) stay exactly as they are
   until the PR that lands the first production caller; that PR deletes them.
2. **Transport.** Waves 4 (backend), 5 (runtime), 6 (UI) and 7 (validation)
   are rebased on the wired capture kernel
   (`jasper/audio_measurement/wired_capture.py`) and the wired session shape
   of ADR-0255. The bench's `PlayAndCapture` is implemented against the wired
   microphone. The relay text in the wave docs is stale as of this ADR and is
   rewritten when each wave is picked up, not now.
3. **Science.** The nearfield fit and the limiter-evidence protocol stay as
   the protection basis. In addition, the extended family is validated
   in-room: through the applied speaker tune, on the room cloud, below the
   room ceiling of ADR-0256. Room gain — in-room response minus the nearfield
   model — is published as a number. Room boosts, linearization boosts and
   the transform's boost share one disclosed headroom budget (ADR-0121's
   compensation), with its cost stated in maximum level.
4. **Scope.** Sealed, ported and passive-radiator plants: the adapters that
   exist. A cardioid bass channel is out of scope for the transform until
   ADR-0258's variant is designed.

## Consequences

- ADR-0229's exemption continues: `docs/HANDOFF-bass-extension-plan.md` and
  `docs/bass-extension-waves/` are the live plan, not a handoff to recreate.
  The plan's header carries a pointer to this ADR; the wave files are
  untouched until their wave.
- The sequence of later waves is not decided here. Only the constraints are:
  wired transport, in-room validation below the ceiling, one headroom ledger.
- Latency is unchanged: the family is minimum-phase IIR
  (plan §8.3).
- The two loose ends ADR-0018 §3 named — the bench onto the hardened
  `play_program`, `bench/excitation.py` with no importer — are wave 4's.
- Gives up: the cheap park. ADR-0018's cost line reverses: the lines start
  earning, and the deadness tests stop being what keeps them honest — the
  production caller and its hardware evidence do.
