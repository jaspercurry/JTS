# ADR-0141: outputd parks out-of-band rather than riding its restart limit to a reboot

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the 2026-06-22 output-side repair and the
  content-lane park that followed; recorded here when
  HANDOFF-hotplug-resilience.md was trimmed to its operational spine)

## Context

`jasper-outputd.service` carries `Restart=on-failure` with
`StartLimitBurst=5` / `StartLimitIntervalSec=300` and
`StartLimitAction=reboot`. Two failure shapes ride that ladder into a reboot
loop instead of a diagnosis:

- **A stale configuration** (`EX_CONFIG=78`). The JTS5 dual-Apple unplug left
  `JASPER_OUTPUTD_DUAL_DAC_B_PCM` naming a child that was gone; the start-time
  `ExecCondition` (resolved DAC card present) passed, and every retry repeated
  the same stale env.
- **A content-lane open that never succeeds** (exit 1). That open is
  restart-class *on purpose*: on boot and on a width-flip deploy it means
  CamillaDSP has not yet opened its half of the pair, and restarting is how
  outputd waits it out. The identical signature is permanent when the peer is
  locked at another width, when the env drifted, or when a roleful box that is
  not ring-armed names an ACTIVE-lane PCM that no longer exists. The two
  categories differ only by **count**.

## Decision

**`ExecStopPost` runs an out-of-band helper that refreshes the environment and,
when the failure proves permanent, parks the unit with a start still unspent.**

- Ordinary retryable failures: run `jasper-audio-hardware-reconcile
  --reason outputd-failure --no-restart`, so the next built-in restart attempt
  reads fresh `outputd.env`.
- `EX_CONFIG=78`, which `RestartPreventExitStatus=78` would otherwise park
  immediately: one short-window reconcile plus one explicit restart. A second
  config exit inside that window skips the retry and leaves the unit parked.
- Content-lane opens: count consecutive failures whose journal tail carries
  outputd's own content-lane contexts and park on the **4th** —
  `reset-failed` plus `stop`, leaving start 5 of `StartLimitBurst=5` unspent.
- **The recorded remedy is lane-specific**, selected on the PCM *name* in the
  failing context (both sinks carry the name; only the composite sink spells
  `active` in its context prefix, so a prefix-keyed selector would miss the
  roleful single-DAC shape). The passive lane still has both snd-aloop halves,
  so its fix is the width; the ACTIVE lane has no snd-aloop transport at all,
  so its only fix is re-arming the ring.
- The park is **terminal for the boot** and says so: `/run` state records the
  reason, the lane-specific action, and the paths that re-arm it.

## Consequences

- The reboot escalation stops being the system's answer to a permanent output
  fault; an operator gets a stopped unit with a written remedy instead.
- **A recovery band is deliberately converted to a park.** The floor on the
  count is fan-in, not outputd: `jasper-camilla.service` orders after
  `jasper-fanin.service`, which is `Type=notify` with no `TimeoutStartSec`, so
  four failures at `RestartSec=5` gives the peer ~15 s plus CamillaDSP's own
  device open. A peer arriving in roughly the **18–24 s band previously
  recovered and now parks**; beyond ~24 s the same stall used to exhaust the
  start limit and reboot, so past that point the trade is a park for a reboot.
- Nothing re-arms a parked outputd on its own — by design, so the fault stays
  visible. Recovery is a sound-card udev event, a deploy,
  `jasper-camilla-recover`'s `OnFailure` pass, a reboot (`/run` wipes, so the
  park is per-boot), or an operator restart.
- The streak state is a plain `/run` file, not the unit's `RuntimeDirectory`:
  the count must survive the unit's own auto-restarts and the record must
  outlive the stop that ends them.
- Rejected: marking the content-lane open park-class in the Rust backend. That
  would turn the routine boot-time wait into a silent speaker.
- A saved *roleful* composite with one child missing parks to
  `JASPER_OUTPUTD_BACKEND=fake` rather than letting the survivor take the box;
  a saved *passive* composite keeps `single_alsa`, matching the calibration
  the doctor makes when it downgrades a non-roleful saved/attached mismatch.
