# ADR-0175: a failed Camilla recovery parks the core graph once

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`jasper-camilla.service` routes an exhausted restart burst to
`OnFailure=jasper-camilla-recover.service` instead of the T5.1
`StartLimitAction=reboot` ladder, because the observed failure class (ALSA
`Device or resource busy` during deploy/renderer churn) needs the `/dev/snd`
holder evidence a reboot destroys.

The handler's terminal state was the problem. When its one bounded pass could
not converge the graph it returned early with all eleven
`JASPER_CORE_GRAPH_PARK_UNITS` stopped and CamillaDSP still armed under
`Restart=always`, so the unit exhausted a fresh burst, re-entered the handler,
and — once the 300 s cooldown expired — stopped all eleven units again.
Steady state was silence plus stop-the-world churn every ~5 min: bounded and
reboot-free, but a worse availability floor than the out-of-band park
[ADR-0141](0141-outputd-parks-out-of-band-rather-than-riding-its-restart-limit-to-a-reboot.md)
gave `jasper-outputd` (park once, stay parked, doctor-visible). Observed by
#2534's SF2 disclosure and tracked on #2564.

This ADR is about the **failing legs** only. A second route into the same
cooldown loop is out of scope and tracked separately (#3096): `systemctl start`
of a `Type=simple` unit returns 0 at fork, so a CamillaDSP that starts and then
exits on a busy device reads as success here, and the handler declares
`recovered` on a graph that is about to fail again.

## Decision

**A recovery pass that cannot converge the graph parks it once, on a written
record, and disarms its own trigger.**

- Both failure legs (`camilla_start_failed`, `outputd_restart_failed`) call
  `park_core_graph`: `reset-failed` + `--no-block stop jasper-camilla.service`,
  then write `/run/jasper-camilla-recover.state`. A stopped unit cannot reach
  `failed`, so `OnFailure=` cannot re-enter — the disarm is the floor. The
  stop does not block: the unit declares no `TimeoutStopSec` (90 s default)
  and the motivating failure is a daemon wedged on a busy device, inside a
  handler whose own `TimeoutStartSec` is 45.
- The record carries the ADR-0141 field vocabulary — `parked_utc`, `reason`,
  `detail`, `action`, `re_arm` — with the `action` naming the daemon that
  actually failed, per ADR-0141's lane-specific rule. It is read by one reader,
  `jasper.control.camilla_recover_state`, with two consumers:
  `/state.resilience.camilla_recover` and jasper-doctor's
  `check_camilla_recover_park` (`fail`, remedy surfaced verbatim).
- A standing record is checked as soon as the handler holds its lock and has
  emitted its `start` line, before the captures and every unit action: any
  later trigger is a `suppressed reason=parked` skip.
- **Re-entry is conditional on the cause clearing, not on a clock.** The park
  is retired by CamillaDSP starting again — `jasper-camilla.service` gained
  `ExecStartPost=-/bin/rm -f /run/jasper-camilla-recover.state`, so an operator
  start, a deploy, or a reboot ends it. That makes the write/stop order a trade
  between two interleavings rather than a safe choice: writing the record first
  lets a pending auto-restart delete the record of a park that still stands (a
  silent park), and writing it second lets a start that beats the stop job
  leave a record saying parked while CamillaDSP runs (a wrong doctor row the
  next start retires). The recoverable half is the one taken.
- The successful ladder is untouched: same captures, same unit sequence, same
  `recovered` event.

## Consequences

- The floor is stable and diagnosable instead of churning: one park, one
  journal event, one doctor row, one `/state` key.
- **An intermittent recovery band is deliberately converted to a park, and
  what it converts is wake response, not just audio.** `jasper-voice` is in the
  park set, so the parked box is **deaf until a human acts**: the old loop's
  only automatic way back was a later pass succeeding and restarting voice, and
  that is what this decision gives up. Recovery is now
  `systemctl start jasper-camilla.service`, a deploy, or a reboot. This is not
  a NEW silent-deafness path — the pre-existing failed leg already left
  `jasper-voice` and the whole graph stopped, so no cue is owed under
  non-negotiable #6 (and none could play with the graph down) — but it does
  make that state persist rather than flap. It is disclosed through the park
  vocabulary the doctor and `/state` already speak, plus the existing
  `path.camilla_stopped` incident row.
- An external actor that keeps starting CamillaDSP into the same broken graph
  still gets one bounded pass per start (cooldown-gated), and parks again. That
  is the same residual ADR-0141 named for outputd and is deliberate: the
  handler gives up on its own re-entry, not on a human's.
- Rejected: keeping the retry but lengthening the cooldown. That trades the
  churn's period for nothing — the box is still silent between passes, with no
  record saying why.
