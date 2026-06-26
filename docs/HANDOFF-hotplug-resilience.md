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
| **Output DAC / Apple dongle** | `jasper-outputd` + `jasper-audio-hardware-reconcile` + `jasper-dongle-recover` | clean park / failure-triggered reconcile | udev → reconcile/recover restart | **Fixed 2026-06-22.** ALSA control events plus Apple USB remove helper wake reconcile; outputd refreshes env before retry |
| **Microphone (XVF3800 / USB)** | `jasper-voice` + `jasper-aec-reconcile` | clean park | udev → reconcile restart | **Fixed 2026-06-21.** Was the original gap: crash-loop → reboot |
| **Satellites (dial / AMOLED)** | `jasper-control` (network peers) | reported offline | re-probe online | **Already resilient** — Wi-Fi/HTTP clients, no device-bound unit |
| **HID accessories** | `jasper-input` | in-process udev | in-process udev | **Already resilient** — pyudev monitor, no per-device unit |
| **WiiM Remote 2 BLE mic** | `jasper-accessory-reconcile` + `jasper-wiim-remote-mic` + `jasper-voice` manual mic source | Bluetooth forget/boot reconcile removes the manual source and disables the adapter; voice keeps normal mic path | Bluetooth pair/connect reconcile writes `accessory-mics.env`, enables adapter, restarts active voice | **Fixed 2026-06-26.** Optional push-to-talk path; absent remote costs 0 resident RAM and is not a voice-daemon health failure |

The original Workstream C gap was the **microphone**. A later JTS5
dual-Apple unplug incident found one output-side edge too: when one Apple
DAC disappeared, the reconciler did not always run before `outputd`
restarted against stale dual-DAC env. The output side now has the same
two-direction convergence guarantee.

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

### Layer 3 — observability: idle vs broken (one source of truth)

The read side is unified behind one reader,
[`jasper.mic_presence.read_mic_presence()`](../jasper/mic_presence.py): every
status surface *displays* its verdict instead of independently re-probing
ALSA / `lsusb` / PortAudio (which is how "no mic" used to surface as a scatter
of contradicting lines). **It is mic-agnostic** — `present` is driven by the
generic gate marker (true for the XVF `Array`, the `L16K6Ch` variant, or a
custom non-XVF mic such as a UMIK-2), while the XVF runtime-profile JSON
(`/run/jasper-mic-profile/xvf3800.json`) is XVF-only *enrichment* layered on
top. Driving presence off the XVF profile would report a working non-XVF mic
as "absent"; the separation exists to prevent that, and generalises when a
second mic profile + `jasper/mics/base.py` land (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)).

- **`jasper-doctor`.** One headline, `check_microphone`
  ([`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py)), states
  present/absent + why in a single line — `warn` (one yellow flag) when
  absent, never `fail`. The per-device checks defer to it via
  `read_mic_presence().absent_confirmed`: `check_mic_card_matches_config` no
  longer re-runs `arecord -L` to emit a contradicting red ✗, and
  `check_mic_capture` reports the same expected idle. A genuine open failure
  with a mic *present* (custom/busy) still falls through to the probe + its
  **fail** — a real signal. `check_service_runtime_state`
  ([`jasper/cli/doctor/resilience.py`](../jasper/cli/doctor/resilience.py))
  treats `inactive` as ok and `failed`/`activating` as fail, so a Layer-1 park
  reads ok while a crash reads fail.
- **`/state`.** A top-level `microphone` block carries the full record
  (present, reason, card, variant, channels, a ready-made `summary`); the
  voice block's `parked_no_mic` is derived from the **same** read so the
  boolean and the record can't drift.
- **Open-failure log.** `_log_audio_open_failure`
  ([`jasper/audio_io.py`](../jasper/audio_io.py)) logs one line and skips its
  portaudio/`arecord`/`aplay`/`dmesg` dump when the mic is confirmed-absent —
  the dump is for *surprise* failures, not the expected no-mic state.
- **Journal.** PID 1 logs the condition skip; the reconciler logs the
  marker write with its reason.

## Output side repair (2026-06-22)

The output owner still has a start-time `ExecCondition`: if the resolved
final-output card in `JASPER_AUDIO_DAC_CARD` is gone, and the backend is
not `fake`, `jasper-outputd.service` parks cleanly before the Rust process
opens ALSA. That catches the simple "configured card vanished" case.

The JTS5 dual-Apple incident exposed a subtler case: with two Apple USB-C
DACs saved as one four-channel profile, unplugging one child can leave
`JASPER_AUDIO_DAC_CARD` naming the surviving child. The `ExecCondition`
passes, but outputd then fails while opening the stale second child PCM
(`JASPER_OUTPUTD_DUAL_DAC_B_PCM`, e.g. `hw:CARD=A_1,DEV=0`). If the
reconciler has not already rewritten `/var/lib/jasper/outputd.env`, the
normal `Restart=on-failure` attempt repeats the stale dual-DAC config and
can hit the restart burst.

The repaired output ladder is:

1. **udev add/change/remove on ALSA control nodes** still triggers
   `jasper-audio-hardware-reconcile.service` through
   `SYSTEMD_WANTS`. This remains the generic surface for current and
   future output hardware.
2. **Apple USB remove** additionally runs
   [`jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug),
   which asks systemd to start the reconciler with `--no-block`. This
   covers remove paths where the disappearing `controlC*` device does not
   activate `SYSTEMD_WANTS`.
3. **outputd failure** runs
   [`jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile)
   from `ExecStopPost`. It skips normal stops, `ExecCondition` parks, and
   `EX_CONFIG=78`; for retryable failures it invokes
   `jasper-audio-hardware-reconcile --reason outputd-failure --no-restart`.
   The next built-in `Restart=on-failure` attempt then reads fresh
   `outputd.env` (single-Apple `single_alsa` when one DAC remains, or
   `fake` when none remain).

`/sound/` also keeps the saved speaker topology separate from the current
observed hardware. A saved dual-Apple active topology is not silently
deleted when one DAC is unplugged; the page shows the saved topology, the
currently attached hardware, and a mismatch blocker before active-speaker
commissioning actions. `jasper-doctor` uses the same split: "Output
hardware state" reports the current reconciler-owned hardware, while
"active speaker output hardware" owns saved-topology mismatch.

## Why satellites needed no change

The rotary dial and AMOLED satellite are **Wi-Fi/HTTP clients of
`jasper-control`**, not wired devices with their own systemd units. An
absent satellite is reported "offline" by `jasper-control`'s TCP probe
([`jasper/control/dial.py`](../jasper/control/dial.py)
`_probe_dial_reachable`) — never a crash. The HID accessory bridge
[`jasper-input`](../deploy/systemd/jasper-input.service) runs a pyudev
hot-plug monitor in-process and opens evdev fds as devices appear, so it
already converges both directions without per-device units. The WiiM
Remote 2 microphone is different from ordinary HID buttons because its
audio arrives over a BLE GATT notification stream; its adapter
[`jasper-wiim-remote-mic`](../deploy/systemd/jasper-wiim-remote-mic.service)
is profile-gated by
[`jasper-accessory-reconcile`](../deploy/systemd/jasper-accessory-reconcile.service).
When BlueZ has no paired WiiM Remote 2, the reconciler removes
`/var/lib/jasper/accessory-mics.env` and disables the adapter, so there
is no resident BLE decoder and no UDP listener in `jasper-voice`. When
the profile is paired, the reconciler writes `wiim_remote_2=udp:9892`,
enables/restarts the adapter, and restarts `jasper-voice` only if voice
is already active. Reconcile runs at boot/deploy and after successful
Bluetooth pair/connect/forget operations, so the UI pairing flow converges
without a second deploy. Adapter service changes are queued with
`systemctl --no-block` and the boot reconciler orders only before
`jasper-voice`, not before the adapter it may start, so optional accessory
state cannot wedge voice startup. A paired-but-sleeping remote still self-heals:
missing GATT report logs `event=wiim_remote_mic.not_ready` (throttled
after the first visible event) and retries; `jasper-voice` keeps the
normal primary mic path alive and only routes the manual source when
`/session/start` names it. Adding a satellite-presence concern would
only matter if a future satellite is a wired device whose daemon opens
it directly; that daemon should adopt the same profile/reconciler gate.

## Verified vs needs-hardware

**Verified hardware-free (tests):**

- Reconciler creates the marker on the no-mic paths, removes it on the
  mic-present paths, and removes it for a custom mic
  ([`tests/test_aec_reconcile.py`](../tests/test_aec_reconcile.py)).
- The unit carries `ConditionPathExists=!<marker>` and `66` in both
  `SuccessExitStatus`/`RestartPreventExitStatus`, and the marker path
  agrees across the unit, the reconciler default, and the Python helper
  ([`tests/test_voice_input_gate.py`](../tests/test_voice_input_gate.py)).
- `main()` exits `66` on `InputDeviceUnavailable`; the doctor reports
  expected-idle when the marker is present.
- Output hardware hotplug and outputd-failure helpers request reconcile
  without blocking udev/systemd, skip non-retrying stops, and preserve
  the outputd `EX_CONFIG=78` park
  ([`tests/test_output_recovery_scripts.py`](../tests/test_output_recovery_scripts.py),
  [`tests/test_outputd_wiring.py`](../tests/test_outputd_wiring.py),
  [`tests/test_outputd_systemd.py`](../tests/test_outputd_systemd.py)).
- `/sound/` and `jasper-doctor` keep current output hardware readiness
  separate from saved active-speaker topology mismatch
  ([`tests/test_sound_setup.py`](../tests/test_sound_setup.py),
  [`tests/test_doctor.py`](../tests/test_doctor.py)).

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
5. **Output DAC unplug/replug** converges in both directions. For the
   dual-Apple case, unplug one child and confirm:
   `journalctl -u jasper-audio-hardware-reconcile` shows a hotplug or
   outputd-failure reconcile, `/run/jasper-output-hardware/output_hardware.json`
   reports the single remaining Apple DAC, `/var/lib/jasper/outputd.env`
   switches to `JASPER_OUTPUTD_SINK=single_alsa`, and `jasper-outputd`
   restarts without reaching the start limit. `/sound/` should show
   "Saved speaker topology" separately from "Currently attached hardware"
   and block active-speaker actions with a saved/attached mismatch.

## Files

- [`deploy/systemd/jasper-voice.service`](../deploy/systemd/jasper-voice.service) — `ConditionPathExists` gate, exit-66 park
- [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile) — single writer of the marker
- [`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py) — marker path + `voice_parked_no_mic()`
- [`jasper/mic_presence.py`](../jasper/mic_presence.py) — the mic-presence SSOT reader (mic-agnostic presence + XVF enrichment)
- [`jasper/audio_io.py`](../jasper/audio_io.py) — `InputDeviceUnavailable`; absent-aware open-failure log
- [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) — raise on primary mic-open failure, exit 66
- [`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py) — `check_microphone` headline + mic checks deferring to the reader
- [`jasper/control/state_aggregate.py`](../jasper/control/state_aggregate.py) — `microphone` block + `voice.parked_no_mic`
- [`deploy/udev/99-jasper-audio-hardware-reconcile.rules`](../deploy/udev/99-jasper-audio-hardware-reconcile.rules) — output-DAC add/remove/change triggers
- [`deploy/bin/jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug) — Apple USB remove reconciler request
- [`deploy/bin/jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile) — outputd retry-time env refresh
- [`deploy/systemd/jasper-outputd.service`](../deploy/systemd/jasper-outputd.service) — output device gate + failure-time reconcile hook
- [`deploy/systemd/jasper-accessory-reconcile.service`](../deploy/systemd/jasper-accessory-reconcile.service) — optional accessory mic profile gate
- [`deploy/systemd/jasper-wiim-remote-mic.service`](../deploy/systemd/jasper-wiim-remote-mic.service) — optional BLE remote mic adapter

Last verified: 2026-06-26
