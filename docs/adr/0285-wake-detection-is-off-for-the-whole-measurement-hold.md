# ADR-0285: Wake detection is off for the whole measurement hold

- **Date:** 2026-09-10
- **Status:** Accepted. Amended by
  [ADR-0305](0305-the-measurement-hold-spans-a-whole-run.md).

## Context

R-008 (2026-09-05 register, reconfirmed by the 2026-09-09 deep audit): a wake
arriving while a measurement held the microphone queued unbounded on
`AssistantOutputGate.begin_turn` — deaf up to the 120 s `MEASUREMENT_HOLD_TTL_SEC`
autoclear (`jasper/control/measurement_hold.py`), then a turn opened into the
room the sweep had just measured. Three designs failed adversarial review by
interleaving with barge-in: preempting the turn claim without cancelling the
writer that held it; a duck-restore gated on an epoch barge-in could also
advance; a 10 s bound that refused turns and blew the control-socket deadline.

## Decision

Owner ruling, issue #4789 (2026-09-10): the speaker is not listening while it
measures — wake detection is off for the whole hold, no cue, no bounded
wait. On release or expiry it returns exactly to the state it found: the
source of truth for "prior" is the persisted mic-mute file
(`mic_mute_state_path`, `jasper/mic_mute_persistence.py`), which the
measurement window never writes, so a prior mute stays muted and a prior
listen resumes listening. Two holes closed this:

1. **The race.** `_begin_turn_output_episode` races the turn's claim on
   `begin_turn_episode` against `WakeLoop._measurement_active` with
   `asyncio.wait(..., FIRST_COMPLETED)`. A wake already mid-acquire when the
   hold's `_set_active_local` sets the gate is dropped silently
   (`event=wake.late_cancel, reason=measurement_active`), same shape
   `_wake_late_cancelled` uses elsewhere. The uncontended path (gate free)
   still takes the episode in-line — no task, no extra hop.
2. **Adopt on restart.** `_measurement_active` is in-memory, so a restart
   mid-sweep would otherwise listen until the coordinator's next
   `MEASURE_PAUSE` renewal (`MEASUREMENT_LEASE_REFRESH_SEC` = 60 s).
   `MeasurementHold.adopt_live_window()` runs in `daemon_main.run()` before
   the first mic frame and asks jasper-control's `read_measurement_hold()`
   — the copy that survives the restart — re-arming the pause if live.
   Unreachable-control and nothing-held both mean "stay listening": the
   coordinator's own renewal is the guarantee, not this call.

`begin_turn` itself is untouched; barge-in stays separate and must never
reopen a turn while `_measurement_active` is set — the race above is the
only place a turn claim and a hold may interleave. Observability reuses
`/state.voice.measurement_active` and `event=measurement.reconcile_guard`
(`_set_active_local`); no new field.

**Non-goal:** a cue during measurement. NN-6 ("no silent deafness") normally
requires one for any path blocking wake; this is a deliberate, bounded
exception — operator-initiated, capped by the hold's 120 s TTL.

## Consequences

- A wake during a hold is unconditionally dropped, not queued — closes the
  deafness window by making it explicit and bounded, not by shortening it.
- Rejected: preempt-without-cancel (stranded the prior writer), epoch-gated
  duck-restore (barge-in could advance the epoch underneath it), and the
  10 s bound (refused legitimate turns, exceeded the control-socket
  deadline) — all three collided with barge-in; none is revived here.
