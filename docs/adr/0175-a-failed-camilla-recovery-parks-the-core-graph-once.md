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
and — once the 300 s cooldown expired — stopped all eleven units again. Steady
state was silence plus stop-the-world churn every ~5 minutes: bounded and
reboot-free, but a worse availability floor than the out-of-band park
[ADR-0141](0141-outputd-parks-out-of-band-rather-than-riding-its-restart-limit-to-a-reboot.md)
gave `jasper-outputd` (park once, stay parked, doctor-visible). Observed by
#2534's SF2 disclosure and tracked on #2564.

## Decision

**A recovery pass that cannot converge the graph parks it once, on a written
record, and disarms its own trigger.**

- Both failure legs (`camilla_start_failed`, `outputd_restart_failed`) call
  `park_core_graph`: write `/run/jasper-camilla-recover.state`, then
  `reset-failed` + `stop jasper-camilla.service`. A stopped unit cannot reach
  `failed`, so `OnFailure=` cannot re-enter — the disarm is the floor.
- The record carries the ADR-0141 field vocabulary — `parked_utc`, `reason`,
  `detail`, `action`, `re_arm` — with the `action` naming the daemon that
  actually failed, per ADR-0141's lane-specific rule. It is read by one reader,
  `jasper.control.camilla_recover_state`, with two consumers:
  `/state.resilience.camilla_recover` and jasper-doctor's
  `check_camilla_recover_park` (`fail`, remedy surfaced verbatim).
- A standing record is checked before anything else the handler does: any
  later trigger is a `suppressed reason=parked` skip with no unit action.
- **Re-entry is conditional on the cause clearing, not on a clock.** The park
  is retired by CamillaDSP starting again — `jasper-camilla.service` gained
  `ExecStartPost=-/bin/rm -f /run/jasper-camilla-recover.state`, so an operator
  start, a deploy, or a reboot ends it. The record is therefore written after
  the stop, never before it: a pending auto-restart landing between the two
  would otherwise delete the record of a park that still stood.
- The successful ladder is untouched: same captures, same unit sequence, same
  `recovered` event.

## Consequences

- The floor is stable and diagnosable instead of churning: one park, one
  journal event, one doctor row, one `/state` key.
- **An intermittent recovery band is deliberately converted to a park.** The
  old loop re-tried every cooldown, so a holder that went away on its own after
  ten minutes used to restore audio unattended; now it needs
  `systemctl start jasper-camilla.service`, a deploy, or a reboot. This does
  not create a new silent-deafness path — the pre-existing failed leg already
  left `jasper-voice` and the whole graph stopped, so wake response was already
  gone on this path — but it does make that state persist rather than flap. It
  is disclosed through the park vocabulary the doctor and `/state` already
  speak, plus the existing `path.camilla_stopped` incident row.
- An external actor that keeps starting CamillaDSP into the same broken graph
  still gets one bounded pass per start (cooldown-gated), and parks again. That
  is the same residual ADR-0141 named for outputd and is deliberate: the
  handler gives up on its own re-entry, not on a human's.
- Rejected: keeping the retry but lengthening the cooldown. That trades the
  churn's period for nothing — the box is still silent between passes, with no
  record saying why.
