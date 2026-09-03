# ADR-0224: The AEC bridge starts on a reconciler-published ready marker

- **Date:** 2026-09-03
- **Status:** Accepted

## Context

`jasper-aec-bridge.service` carried
`ExecCondition=/usr/local/sbin/jasper-aec-reconcile --check-aec-ready`. That
flag set `CHECK_ONLY=1` at the top of a ~2,150-line bash script and did not
reach its verdict until the bottom, running `observe_mic_profile_state`
(`python -m jasper.cli.xvf_profile`, ~18 MB) and `refresh_chip_aec_dac_gate`
(`python -m jasper.cli.chip_aec_policy`, ~20 MB) on the way. Every start
*attempt* therefore cost ~38 MB of CPython plus a full bash pass, on a Pi Zero
2 W with 415 MB of RAM (issue #3697).

The bridge is `Restart=on-failure`/`RestartSec=2` under
`StartLimitBurst=4`/`StartLimitIntervalSec=300`/**`StartLimitAction=reboot`**,
and `jasper-usbmic-apply.service` restarts it too — so a restart spiral spent
~150 MB of probes on its way to a reboot. `ExecCondition=` is evaluated inside
the start job, so those attempts also consumed the burst slots they were
feeding.

`jasper-aec-reconcile.service` was already the declared single owner of whether
the bridge should run. It simply never published a boolean the unit could read
for free. ADR-0221 solved the same problem for source start gates.

## Decision

**The reconciler publishes its bridge verdict to
`/run/jasper-aec-reconcile/aec-bridge-ready`, and the unit gates on
`ConditionPathExists=`.** Every pass **withdraws the marker before it starts
re-deriving** and republishes it only where a verdict settles on "the bridge
should be carrying the mic" — `enable_start_aec`, the settled-alignment branch
of `activate_managed_chip_aec`, and each branch that skips the bounce because
the stack is already up. Teardown paths mostly just never republish; the two
reachable *after* a publish in the same pass (`stop_disable_aec`,
`park_managed_xvf`) withdraw again explicitly. `--check-aec-ready` and the
`CHECK_ONLY` plumbing are deleted.

The path lives in the `/run` directory the reconciler already creates for its
voice-restart stamp — no `RuntimeDirectory=` (which a `Type=oneshot` would
delete on every stop), no tmpfiles entry, no new knob.
`jasper/aec_ready.py` owns the literal for the Python readers.

## Consequences

- **Zero processes on the start path.** systemd stats one file. The condition
  is evaluated inside `unit_start()` when the job runs, but *before* the unit
  type's start handler — so the job completes as done, the unit is not marked
  failed, and the `StartLimitBurst` slot that handler would have spent is not
  spent (verified against systemd v252/v255). `ExecCondition=` ran past that
  point, inside the start job, and did spend one.
- **No ordering edge between the two units, deliberately.** `/run` is empty at
  boot, so the bridge's first boot start is skipped; the reconciler's own pass
  publishes the marker and then restarts the bridge, which is what starts it.
  `After=jasper-aec-reconcile.service` on the bridge would **deadlock**: the
  reconciler blocking-restarts the bridge from inside its own `ExecStart`, so
  the bridge's job would wait for the reconciler and the reconciler for the
  bridge, until its `TimeoutStartSec=120` — leaving the box deaf for two
  minutes on every boot.
- **Withdrawing first is what keeps the reboot escalation out of reach.** When
  the XVF is pulled from under a *running* bridge its `ExecStart` genuinely
  fails, and `Restart=on-failure`/`RestartSec=2` retries; four real failures
  inside 300 s reboot the box. A marker left standing until the udev-triggered
  pass reached its park — two CPython probes later — is a wide enough window to
  spend all four. Withdrawn as fast as the reconcile job is scheduled, the
  first retry is condition-skipped instead, which neither fails the unit nor
  re-arms `Restart=`. The same reasoning puts the withdrawal at the top of
  `park_to_reference_leg`, which is reached from a bridge restart that has
  already failed for real. This diverges from ADR-0221, which leaves the
  previous verdict standing while its coordinator is wedged; there the guard's
  own footprint was the cause of the wedging, and there is no per-unit reboot
  action on the other side.
- **Residual, accepted rather than designed around:** "as fast as the job is
  scheduled" is not immediate. `jasper-aec-reconcile.service` is
  `After=sound.target jasper-camilla.service`, so a DAC event that also bounces
  Camilla queues the reconcile pass behind Camilla's job while the bridge fails
  every 2 s; four failures inside that window still reach
  `StartLimitAction=reboot`. Closing it would mean a udev `RUN+=` remover — a
  second writer of the marker, on the hotplug path, for a coincidence — which
  costs more than the residual.
- **A pass that dies leaves no verdict, with no exit trap to arrange it** — it
  has already withdrawn and never republished. A `SIGTERM` at the reconciler's
  `TimeoutStartSec` is likewise unchanged from before: it lands on the same
  `checking`/parked state, now with the verdict withdrawn rather than refused
  live.
- **The cost is one pass's width.** While a pass is deciding, another owner's
  `systemctl restart jasper-aec-bridge` (`jasper-usbmic-apply`, a gadget
  converge) is condition-skipped and exits 0. The pass restarts the bridge
  itself wherever the verdict is yes, so the request converges one pass later
  instead of being lost.
- **A skipped bounce still republishes.** `aec_stack_bounce_can_be_skipped` is
  unchanged, but each branch that takes the skip re-admits the bridge, so the
  gate that exists to stop per-udev-event deafness keeps working without
  leaving a live bridge that no restart could bring back.
- **`jasper-aec-init`'s liveness is no longer re-checked per bridge start.**
  The old probe demanded `is-active jasper-aec-init` on a chip-AEC box every
  time; the published verdict is only as fresh as the last pass. An init that
  fails *after* a pass admitted the bridge is caught by aec-init's own
  `OnFailure=jasper-aec-reconcile.service`, which re-derives and withdraws.
- **A custom `JASPER_MIC_DEVICE` box is now tighter.** The old probe would
  admit the bridge on any 6-channel `auto` box even where the reconciler had
  stopped and disabled it; the verdict now matches the reconciler's intent.
- **Observability is the contract.** `/state.aec.bridge_ready` carries
  `{ready, reason, marker}`, and `jasper-doctor`'s *AEC bridge service* row
  splits a down bridge into "no pass has admitted it" (reconciler problem) and
  "admitted and still not running" (bridge problem). No new cue: the verdict
  itself is unchanged, so this adds no path that prevents wake response — the
  park and disclosure routes that do already own their marker, `/state` and
  doctor surfaces (ADR-0101).
- **The streambox park clears it too.** `/run` covers the boot path, but an
  in-place full-speaker → streambox conversion `disable --now`s the
  reconciler while its `Wants=jasper-aec-bridge.service` drop-in on
  `jasper-voice` survives — so `park_streambox_brain_units` removes this
  marker alongside the `voice-input-absent` one it already removed, leaving no
  verdict that nothing is left to withdraw.
