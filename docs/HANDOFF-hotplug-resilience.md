# Handoff: runtime hardware hot-plug / unplug resilience

Treat the speaker like a computer: the microphone (XVF3800), the
output DAC/dongle, the USB host, and satellites can be **attached or
detached while the speaker is running**, and the system must converge
to a correct state on its own — in **both** directions, with no
redeploy, no manual restart, and **no crash-loop**. On **unplug** the
dependent function parks cleanly and says so; on **plug-in** it comes
back automatically and promptly (event-driven via udev, not only on
boot/deploy/timer).

This doc is the canonical reference for that convergence model. It is
the home for Workstream C of
[`install-update-resilience-plan.md`](install-update-resilience-plan.md)
(problem #6). The cross-cutting resilience ladder lives in
[`HANDOFF-resilience.md`](HANDOFF-resilience.md); the mic/AEC reconciler
internals in [`HANDOFF-aec.md`](HANDOFF-aec.md); the output owner in
[`HANDOFF-speaker-output-reference.md`](HANDOFF-speaker-output-reference.md).

## The invariant (what "converges" means here)

For every hot-pluggable component, all four must hold:

1. **Unplug never crash-loops.** The dependent daemon must not
   start-fail-restart in a tight loop. On JTS a sustained restart
   spiral escalates to `StartLimitAction=reboot` (Tier 4.5/T5.1), so a
   crash-loop on a *missing optional device* is not merely ugly — it
   reboots the speaker, repeatedly.
2. **Unplug parks cleanly and observably.** The function goes
   `inactive` (not `failed`), and the state is reported as *expected
   idle* — distinct from *broken* — in `jasper-doctor`, `/state`, and
   the journal.
3. **Plug-in converges automatically and promptly.** A udev event
   re-runs the owning reconciler within seconds; no human action.
4. **No false health.** An update/deploy must not report the function
   healthy when it is only idle for missing hardware (and must not fail
   just because the hardware is absent).

## Status by component

| Component | Owner | Unplug | Plug-in | Notes |
|---|---|---|---|---|
| **Output DAC / Apple dongle** | `jasper-outputd` + `jasper-audio-hardware-reconcile` + `jasper-dongle-recover` | clean park (ExecCondition) | udev → reconcile/recover restart | **Already converged** — the pattern the mic side now copies |
| **Microphone (XVF3800 / USB)** | `jasper-voice` + `jasper-aec-reconcile` | clean park (this PR) | udev → reconcile restart | **Fixed here.** Was the gap: crash-loop → reboot |
| **Satellites (dial / AMOLED)** | `jasper-control` (network peers) | reported offline | re-probe online | **Already resilient** — Wi-Fi/HTTP clients, no device-bound unit |
| **HID accessories** | `jasper-input` | in-process udev | in-process udev | **Already resilient** — pyudev monitor, no per-device unit |

The only gap was the **microphone**. This PR closes it by adopting the
exact pattern the output owner already uses.

## The mechanism (mic)

Three layers, defence-in-depth. Layers 1 and 2 mirror the
`voice-provider-unset` handling (reconciler park + daemon `EX_CONFIG`
exit) and the **output** `ExecCondition` gate; nothing here is a new
mechanism.

### Layer 1 — presence gate: `ConditionPathExists` on a reconciler-owned marker (primary)

`jasper-voice.service` carries:

```ini
ConditionPathExists=!/var/lib/jasper/voice-input-absent
```

A **negated** condition evaluated by PID 1 (as root, *before* the unit's
sandbox/`User=`/`StateDirectory=` are set up — no permission or ordering
traps). When the marker exists, the unit is **skipped cleanly**:
`ActiveState=inactive`, *not* counted as a start, *not* subject to
`Restart=`, *never* escalates to `StartLimitAction=reboot`. This is the
same clean-skip property the output owner relies on (see
[`jasper-outputd.service`](../deploy/systemd/jasper-outputd.service)
`ExecCondition`).

The marker is the **negative** ("voice input is known-absent") and lives
in **persistent** storage (`/var/lib/jasper`, not `/run`). Both choices
are load-bearing:

- **Negative + fail-open.** No marker ⇒ condition true ⇒ voice runs.
  A fresh box, or one where the reconciler never ran (bug, missing
  prereq), behaves exactly as today. The gate can only *withhold* voice
  when the reconciler positively determined there is no mic.
- **Persistent + boot-safe.** The marker survives reboot, so on a no-mic
  box PID 1 knows "no mic, skip voice" at the very first moment of boot —
  voice never even attempts to start. A `/run` (tmpfs) marker would be
  cleared on boot and re-introduce the start-before-reconcile race that
  caused the original incident.

`jasper-aec-reconcile` is the **single writer**. It already owns the
"is there a usable mic, and should voice run" decision (it resolves
`JASPER_MIC_DEVICE`, starts/stops the AEC bridge, and starts/parks
voice). It now also expresses that verdict as the marker:

- every path that **stops voice for no mic** (`stop_voice`, reached only
  when no candidate mic is present) → **creates** the marker;
- every path that **(re)starts voice because a mic is present**
  (`restart_voice`) → **removes** the marker;
- the **custom-`JASPER_MIC_DEVICE`** early-exit (nonstandard hardware /
  corpus rigs, which the reconciler deliberately does not manage) →
  **removes** the marker, so a custom-mic operator is never gated by us
  (their device's openability is enforced by Layer 2 instead).

Why a reconciler-written marker instead of a direct `/proc/asound/$card`
check like the output owner's `ExecCondition`: the mic is reached via
`udp:PORT` (the AEC bridge) or a candidate list, and "is there a usable
mic" depends on firmware channel count and the owned-vs-custom
distinction. That resolution is the reconciler's job and lives in one
place; the unit stays a dumb gate. The richer "why" is in the journal
(`event=aec_reconcile…`) and the doctor.

### Layer 2 — clean exit: daemon parks instead of crashing (backstop)

Layer 1 cannot cover three residual cases: a custom mic the reconciler
won't gate; a mic that ALSA enumerates but PortAudio can't *open* (busy,
firmware glitch); and the very first boot of a fresh no-mic box before
any reconcile has written the marker. For those, the daemon itself must
not crash-loop.

In [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) the
leg factory opens the **primary** ("on") wake leg's mic. On failure it
now raises `InputDeviceUnavailable`
([`jasper/audio_io.py`](../jasper/audio_io.py)); `main()` catches it,
logs, and exits **`66`** (`os.EX_NOINPUT`, `VOICE_MIC_UNAVAILABLE_EXIT`).
The unit lists `66` in both `SuccessExitStatus=` and
`RestartPreventExitStatus=` — exactly the treatment provider-unset gets
with `78` — so the daemon **parks clean** instead of looping toward a
reboot.

Trade-off (deliberate): a parked daemon does **not** auto-retry; it
waits for a reconcile/udev event (or a manual `systemctl restart`).
Standard mics recover on replug via the existing udev → reconcile →
`restart_voice` path. A custom mic, or a *transient* "device busy" with
no re-enumeration event, parks until the next reconcile/restart rather
than retrying. This is the price of "never reboot-loop," and it is the
right call: a persistent open failure is a real fault that should be
visible (doctor/journal), not retried into a reboot.

### Layer 3 — observability: idle vs broken

- **`jasper-doctor`.** `check_mic_capture`
  ([`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py)) returns
  **ok** with "no microphone present (expected) — voice parked; plug a
  mic and it starts automatically" when the marker is present, mirroring
  the `_parked_as_bonded_follower()` idiom. `check_service_runtime_state`
  ([`jasper/cli/doctor/resilience.py`](../jasper/cli/doctor/resilience.py))
  already treats `inactive` as ok and `failed`/`activating` as fail — so
  a Layer-1 park reads ok there, while a genuine crash still reads fail.
  When the marker is **absent** but the device still won't open
  (custom/busy), `check_mic_capture` keeps its existing **fail** — a real
  signal.
- **`/state`.** The voice block gains `parked_no_mic` (read fresh from
  the marker by
  [`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py)),
  so a consumer can tell `reachable:false` "idle, no mic" from
  `reachable:false` "crashed."
- **Journal.** PID 1 logs the condition skip; the reconciler logs the
  marker write with its reason.

## Why the output side needed no change

[`jasper-outputd.service`](../deploy/systemd/jasper-outputd.service)
already gates on the DAC card with an `ExecCondition` that parks the unit
cleanly when `/proc/asound/$JASPER_AUDIO_DAC_CARD` is gone, and the
[`99-jasper-audio-hardware-reconcile.rules`](../deploy/udev/99-jasper-audio-hardware-reconcile.rules)
udev rule (plus
[`jasper-dongle-recover.service`](../deploy/systemd/jasper-dongle-recover.service)
for the Apple dongle) `reset-failed`s and restarts it when a DAC
returns. `jasper-camilla` writes only to snd-aloop in the outputd
topology, so an absent DAC does not crash it. Output is already
bidirectional; the mic side simply adopts the same shape.

## Why satellites needed no change

The rotary dial and AMOLED satellite are **Wi-Fi/HTTP clients of
`jasper-control`**, not wired devices with their own systemd units. An
absent satellite is reported "offline" by `jasper-control`'s TCP probe
([`jasper/control/dial.py`](../jasper/control/dial.py)
`_probe_dial_reachable`) — never a crash. The HID accessory bridge
[`jasper-input`](../deploy/systemd/jasper-input.service) runs a pyudev
hot-plug monitor in-process and opens evdev fds as devices appear, so it
already converges both directions without per-device units. Adding a
satellite-presence concern would only matter if a future satellite is a
wired device whose daemon opens it directly; that daemon should adopt
the same `ConditionPathExists`/`ExecCondition` gate.

## Verified vs needs-hardware

**Verified hardware-free (this PR's tests):**

- Reconciler creates the marker on the no-mic paths, removes it on the
  mic-present paths, and removes it for a custom mic
  ([`tests/test_aec_reconcile.py`](../tests/test_aec_reconcile.py)).
- The unit carries `ConditionPathExists=!<marker>` and `66` in both
  `SuccessExitStatus`/`RestartPreventExitStatus`, and the marker path
  agrees across the unit, the reconciler default, and the Python helper
  ([`tests/test_voice_input_gate.py`](../tests/test_voice_input_gate.py)).
- `main()` exits `66` on `InputDeviceUnavailable`; the doctor reports
  expected-idle when the marker is present.

**Needs a real plug/unplug hardware pass (flag for the next on-Pi
session):**

1. **Cold boot with no mic** never starts `jasper-voice` (condition
   skip), `jasper-doctor` is all-green with "no microphone present
   (expected)", and **no reboot** occurs. Confirm
   `systemctl show jasper-voice -p NRestarts` stays `0` and
   `ActiveState=inactive`.
2. **Hot-unplug the XVF3800 while running**: udev fires
   `jasper-aec-reconcile`, voice stops cleanly, marker appears,
   `/state.voice.parked_no_mic=true`. Confirm no restart spiral in
   `journalctl -u jasper-voice`.
3. **Hot-plug the XVF3800 back**: udev → reconcile removes the marker and
   `restart_voice` brings voice up within seconds; "Hey Jarvis" works
   without any manual step.
4. **Deploy/update a box with no mic attached**: install succeeds, leaves
   voice cleanly parked (not failed), and the post-deploy verification
   does not report voice healthy (Workstream B owns broadening that
   verification; this PR ensures the parked state is *correct and
   labelled*).
5. **Output DAC unplug/replug** still converges (regression check —
   unchanged by this PR, but exercise it on the same pass).

## Files

- [`deploy/systemd/jasper-voice.service`](../deploy/systemd/jasper-voice.service) — `ConditionPathExists` gate, exit-66 park
- [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile) — single writer of the marker
- [`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py) — marker path + `voice_parked_no_mic()`
- [`jasper/audio_io.py`](../jasper/audio_io.py) — `InputDeviceUnavailable`
- [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) — raise on primary mic-open failure, exit 66
- [`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py) — marker-aware `check_mic_capture`
- [`jasper/control/state_aggregate.py`](../jasper/control/state_aggregate.py) — `voice.parked_no_mic`

Last verified: 2026-06-21
