# Handoff: runtime memory investigation

This handoff records the current memory-reduction work for the always-on
speaker runtime. It is not a replacement for the subsystem docs; it links the
RAM-specific decisions that cross wake/AEC, the system dashboard, and Home
Assistant status.

## Shipped current state

### 1. Chip-AEC default opens one wake detector

The `xvf_chip_aec` profile now treats the XVF3800's primary/session chip-AEC
beam as the default wake input. That path feeds `JASPER_MIC_DEVICE=udp:9876`
and one wake leg (`on` / "Primary chip beam").

The extra XVF beams are advanced custom opt-ins:

- `JASPER_WAKE_LEG_CHIP_AEC_150=1` exposes `JASPER_MIC_DEVICE_CHIP_AEC_150`
  on `udp:9887`.
- `JASPER_WAKE_LEG_CHIP_AEC_210=1` exposes `JASPER_MIC_DEVICE_CHIP_AEC_210`
  on `udp:9888`.

Selectable profiles (`auto`, `xvf_chip_aec`, `xvf_chip_aec_testing`,
`xvf_software_aec3`, `direct_mic`) reset those optional beam toggles to `0`.
Only the `custom` profile preserves them. This keeps a chip-AEC install from
silently running three Silero/openWakeWord instances after the profile says it
is using hardware echo cancellation.

The active wake channels are tied to reconciler-applied audio processing
channels:

- The reconciler is the only writer of concrete `JASPER_MIC_DEVICE*` values.
- `/aec` and `/wake/` render optional channel availability from applied runtime
  state, not from front-end guesses.
- `jasper-aec-bridge` only emits optional chip UDP streams when the matching
  runtime device env exists.
- Doctor and validation fail on unexpected extra wake legs, so hidden resource
  burn is caught instead of normalized.

### 2. Home Assistant status no longer probes in-process

`jasper-control` now owns a small `HomeAssistantStatusCache` for both
`/state.home_assistant` and the `/system/snapshot` card. The cache starts a
short-lived child process (`python -m jasper.control.ha_probe_child`) when
status is stale. The child imports `jasper.home_assistant`, reads the wizard
env file, runs the existing async probe, prints JSON, then exits.

The parent process keeps only the small JSON status dict. A stale dashboard
or state read returns immediately with `checking=true` (or stale cached
status) while one background refresh runs. Failures are bounded by the child
timeout and logged as `event=ha.status_probe_failed`.

### 3. Dashboard memory attribution is more useful

The system sampler now reads root cgroup-v2 memory accounting when available:

- `memory.current` -> total cgroup memory
- `memory.stat` -> anon / file / kernel / other buckets

`/system/` shows the breakdown on the Memory tile. Per-service cgroup memory
still comes from the existing service inventory. The new root breakdown helps
separate anonymous daemon RSS from page cache and kernel accounting when a
1 GB Pi looks tight.

## Remaining big RAM options

1. **Voice provider import/client laziness (deferred P4).** The voice runtime
   still has the biggest potential import/client graph win. Do this behind the
   existing `LiveConnection` provider registry, not by adding provider branches
   in `voice_daemon.py`.

2. **Park follower voice brains.** Multiroom followers that are not accepting
   local wake events should not keep a full voice provider client resident.
   This needs a product decision around local wake availability and audible
   failure cues before implementation.

3. **Wake model lifecycle by channel.** The wake stack should keep one loaded
   model instance per active audio-processing channel, and no instance for a
   channel the reconciler did not apply. The chip-AEC profile change closes the
   immediate leak; a future cleanup could make the lifecycle contract explicit
   in the wake loop itself.

4. **System-dashboard probe isolation.** HA was the obvious retained import
   graph. Similar treatment may be worthwhile for other rarely viewed dashboard
   probes only if measurements show meaningful RSS retained in
   `jasper-control`.

5. **Memory cgroup soak before tighter limits.** The dashboard can now expose
   root and service memory. Use a Pi 5 1 GB soak to size any future
   `MemoryHigh=` / `MemoryMax=` changes; do not guess from dev-machine RSS.

## Validation pointers

- Wake/AEC contract: `tests/test_aec_reconcile.py`,
  `tests/test_aec_bridge_stall.py`, `tests/test_control_aec_state.py`,
  `tests/test_audio_validation.py`, `tests/test_doctor.py`.
- HA cache contract: `tests/test_ha_status_cache.py`,
  `tests/test_control_server.py`.
- Dashboard memory breakdown: `tests/test_system_metrics.py`.

Last verified: 2026-06-30 (`xvf_chip_aec` one-detector default rechecked
against `jasper/audio_profile_state.py`, `deploy/bin/jasper-aec-reconcile`,
`jasper/cli/aec_bridge.py`, `/aec`, `/wake/`, doctor, and validation tests;
HA child cache rechecked against `jasper/control/ha_status_cache.py`,
`jasper/control/ha_probe_child.py`, `/state`, and `/system/snapshot` tests).
