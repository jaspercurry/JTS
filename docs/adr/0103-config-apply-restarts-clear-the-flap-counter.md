# ADR-0103: A deliberate config-apply restart clears `NRestarts`, and that erasure is accepted

- **Date:** 2026-08-25
- **Status:** Accepted (ratified 2026-08-14, #2234)

## Context

T5.1 puts `StartLimitAction=reboot` on the critical daemons (outputd, fan-in,
aec-bridge, voice, control): when one of them exceeds its `StartLimitBurst=`
inside `StartLimitIntervalSec=`, systemd cleanly reboots the box. That budget
is for **crash** loops, so a reconciler that restarts one of those daemons to
apply new configuration runs `systemctl reset-failed <unit>` first — six
`/grouping/set` POSTs in 44 s once tripped outputd's start limit and rebooted
a follower (2026-06-24), and repeatedly toggling Bluetooth rebooted a Zero
2 W (#2175). A daemon's own `Restart=` path never runs that reset, so genuine
crash loops still escalate.

The cost: `reset-failed` also clears `NRestarts`, which is a live flapping
signal and not just a failed-state latch. Doctor's
`check_service_runtime_state` warns while it is non-zero, `/system/data.json`
carries it, and the dashboard sorts a unit toward the top of the services
table on it. So a fan-in that crash-restarted four times reads `NRestarts=0`
after one household source toggle: the reboot is correctly prevented, and the
evidence that it nearly happened is not preserved. This is not one caller's
doing — the reconcilers (`jasper.fanin.coupling_reconcile`,
`jasper.multiroom.reconcile`), the recovery helpers
(`jasper-audio-hardware-reconcile`, `jasper-camilla-recover`,
`jasper-outputd-failure-reconcile`, the udev-driven `jasper-dongle-recover`)
and every deploy (`deploy/lib/install/systemd-units.sh`, over the core-graph
restart targets) all clear it.

## Decision

**Accept the erasure. Do not build a control-owned restart counter to buy the
signal back.**

The obvious fix — generalising CamillaDSP's `_camilla_stopped` shape to
fan-in and outputd — would not restore what was lost. That is a *stoppage*
detector ("not running is the fact", per its own docstring), and a daemon
that crash-restarts four times and comes back up is `active` at every sample;
flapping is exactly the state it cannot see. The fix that would work is a
restart counter `reset-failed` cannot clear, which is an L-sized build to buy
back an early-warning signal.

## Consequences

- What still catches the hard states is unchanged: `check_fanin_service`
  FAILs on disabled/inactive and on a STATUS probe that cannot be read or
  does not parse, so a fully-down or wedged fan-in is reported whatever
  `NRestarts` says.
- What is given up is only the warning that would have arrived *before* one
  of those hard states — a box can reach a hard failure without the services
  table having flagged the flapping that preceded it.
- CamillaDSP keeps its compensating not-running detector; outputd and voice
  deliberately do not get one, because both have legitimate parked states
  that a stoppage detector would misread as failure.
- The reboot budget stays honest: N control-plane applies never accumulate
  toward a reboot, which is the property that made the reset necessary.
