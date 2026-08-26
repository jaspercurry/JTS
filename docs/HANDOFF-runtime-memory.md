# Handoff: runtime memory on the 1 GB Pi

Where always-on RAM goes, what already shrank it, and what is left. The
per-daemon cap question is [ADR-0104](adr/0104-per-daemon-memory-caps-stay-deferred.md);
this doc is the runtime side of it.

## What already shrank

- **One wake detector per applied channel.** A selected audio-input profile
  writes its whole leg set, so `xvf_chip_aec` runs the primary chip beam only
  (`JASPER_MIC_DEVICE=udp:9876`) instead of three Silero/openWakeWord
  instances. The fixed 150°/210° beams (`udp:9887` / `udp:9888`) exist only
  under `custom`, via `JASPER_WAKE_LEG_CHIP_AEC_150` /
  `JASPER_WAKE_LEG_CHIP_AEC_210` —
  [ADR-0170](adr/0170-a-selectable-audio-input-profile-owns-its-whole-wake-leg-set.md).
- **Home Assistant status probes out of process.** `jasper-control` keeps only
  the small JSON status dict; the import graph lives in a short-lived
  `jasper.control.ha_probe_child` —
  [ADR-0171](adr/0171-rarely-viewed-dashboard-probes-run-in-short-lived-child-processes.md).
- **Memory attribution on `/system/`.** The sampler reads root cgroup-v2
  accounting when the controller is enabled (`memory.current` for the total,
  `memory.stat` for anon / file / kernel / other) alongside the per-service
  cgroup figures, so anonymous daemon RSS is separable from page cache when the
  box looks tight (`jasper/control/system_metrics.py`).

## What is left, in leverage order

1. **Voice provider import/client laziness.** The largest remaining import
   graph. Do it behind the `LiveConnection` provider registry, not by adding
   provider branches in `voice_daemon.py`.
2. **Park follower voice brains.** A multiroom follower not accepting local
   wake events should not hold a resident provider client. Blocked on a product
   decision about local wake availability and its failure cue, not on code.
3. **Wake model lifecycle in the wake loop.** ADR-0170 closes the leak at the
   profile boundary; making the loop itself refuse to hold a model for a channel
   the reconciler did not apply would make the contract structural.
4. **Further probe isolation.** Only where a measurement shows meaningful RSS
   retained in `jasper-control` — see ADR-0171 for when this shape is and is not
   the right answer. The opt-in USB gadget forensics sampler is deliberately
   outside the always-on path (no process while disabled, 512 KiB `/run`
   timeline, 32 MiB ceiling while enabled):
   [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md#opt-in-rolling-usb-forensics).
5. **Tighter systemd limits.** Sizing needs a Pi 5 1 GB soak against the
   dashboard's root and per-service figures, never dev-machine RSS. The trigger
   list is ADR-0104's.

## Pins

- Wake/AEC leg contract: `tests/test_aec_reconcile.py`,
  `tests/test_aec_bridge_stall.py`, `tests/test_control_aec_state.py`,
  `tests/test_audio_validation.py`, `tests/test_doctor_aec.py`.
- HA status cache: `tests/test_ha_status_cache.py`, `tests/test_control_server.py`.
- Dashboard memory breakdown: `tests/test_system_metrics.py`.

Last verified: 2026-08-26 (triage pass — profile leg-set resets and the
`custom`-only exception rechecked against `jasper/audio_profile_state.py` and
`deploy/bin/jasper-aec-reconcile`; the 9876/9887/9888 carriers against
`jasper/cli/aec_bridge.py`; the HA child cache against
`jasper/control/ha_status_cache.py` and `jasper/control/ha_probe_child.py`; the
root cgroup buckets against `jasper/control/system_metrics.py`; every pin file
confirmed present.)
