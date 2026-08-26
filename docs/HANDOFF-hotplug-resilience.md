# HANDOFF — runtime hardware hot-plug / unplug resilience

Treat the speaker like a computer: the microphone (XVF3800), the output
DAC/dongle, USB host, and HID accessories can be **attached or detached while
the speaker is running**, and the system must converge on its own in **both**
directions — no redeploy, no manual restart, **no crash-loop**. On unplug the
dependent function parks cleanly and says so; on plug-in it comes back
automatically via udev, not only on boot/deploy/timer.

Neighbouring owners — do not restate their content here:
[HANDOFF-resilience.md](HANDOFF-resilience.md) (the cross-cutting resilience
ladder) · [HANDOFF-aec.md](HANDOFF-aec.md) (mic/AEC reconciler internals) ·
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
(the output owner).

The decisions behind this doc live in ADRs, not here:
[ADR-0139](adr/0139-the-voice-input-gate-is-one-reconciler-owned-negative-marker.md)
(the voice-input gate) ·
[ADR-0140](adr/0140-a-missing-microphone-degrades-aec-never-output.md)
(a missing mic degrades AEC, never output) ·
[ADR-0141](adr/0141-outputd-parks-out-of-band-rather-than-riding-its-restart-limit-to-a-reboot.md)
(outputd's out-of-band park) ·
[ADR-0142](adr/0142-the-ble-connection-event-reservation-is-re-requested-never-watched.md)
(the BLE connection-event reservation).

## The invariant

For every hot-pluggable component, all four must hold:

1. **Unplug never crash-loops.** A sustained restart spiral escalates to
   `StartLimitAction=reboot`, so a crash-loop on a *missing optional device*
   reboots the speaker, repeatedly.
2. **Unplug parks cleanly and observably** — `inactive`, not `failed`, and
   reported as *expected idle* (distinct from *broken*) in `jasper-doctor`,
   `/state`, and the journal.
3. **Plug-in converges automatically and promptly** — a udev event re-runs the
   owning reconciler within seconds; no human action.
4. **No false health.** An update must not report the function healthy when it
   is only idle for missing hardware, and must not fail because the hardware
   is absent.

## Status by component

| Component | Owner | Unplug | Plug-in |
|---|---|---|---|
| **Output DAC / Apple dongle** | `jasper-outputd` + `jasper-audio-hardware-reconcile` + `jasper-dongle-recover` | clean park, or failure-triggered reconcile | udev → reconcile/recover restart |
| **Microphone (XVF3800 / USB)** | `jasper-voice` + `jasper-aec-reconcile`; the optional chip reference is owned by `jasper-outputd` | voice parks clean; outputd keeps DAC playback running and retries the reference sink in the background (ADR-0140) | udev → reconcile restart; outputd reconnects its reference writer without a playback restart |
| **HID accessories** | `jasper-input` | in-process pyudev monitor, no per-device unit | same |
| **WiiM Remote 2 BLE mic** | `jasper-accessory-reconcile` + `jasper-wiim-remote-mic` + `jasper-wiim-remote-ce` + `jasper-voice`'s manual mic source | forget/boot reconcile removes the manual source and disables the adapter; the normal mic path is untouched | pair/connect reconcile writes `accessory-mics.env`, enables the adapter, then refreshes voice |

The absent remote costs 0 resident RAM and is not a voice-daemon health
failure. A paired remote satisfies the voice-input gate on its own; a
no-local-mic box plans zero wake legs and serves the button instead of parking.
End-to-end "the speaker wakes via the remote" is **not yet hardware-verified**,
and realtime delivery additionally depends on the CE reservation (below).

## Layer 1 — the presence gate (primary)

`jasper-voice.service` carries:

```ini
ConditionPathExists=!/var/lib/jasper/voice-input-absent
```

A negated condition evaluated by PID 1 before the unit's sandbox/`User=`/
`StateDirectory=` exist. When the marker is present the unit is **skipped
cleanly**: `ActiveState=inactive`, not counted as a start, not subject to
`Restart=`, never escalating to `StartLimitAction=reboot`.

`jasper-aec-reconcile` is the **single writer**, and the marker is the AND of
two absences — no local mic *and* no paired accessory mic. Polarity, storage,
the OR of two owners, and the published `JASPER_LOCAL_MIC_PRESENT` tri-state
are ADR-0139; the module docstring in
[`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py) restates
the contract for readers of the code.

- every path that **stops voice for no input** (`stop_voice`) creates it;
- every path that **(re)starts voice** (`restart_voice`) removes it;
- the **custom-`JASPER_MIC_DEVICE`** early exit removes it, so an operator's
  own device is never gated by us (Layer 2 enforces its openability instead).

**Which boxes reach `refresh_voice_input`** — read from
`deploy/lib/install/systemd-units.sh`, and the reason the accessory half checks
unit state instead of starting the gate owner and hoping:

- **Full speaker.** `install_systemd_units` installs *and* enables both
  reconcilers → `systemctl start jasper-aec-reconcile`. The path the fix exists
  for.
- **Fresh streambox.** `install_streambox_systemd_units` installs neither
  reconciler, so nothing calls `refresh_voice_input` at all.
- **Converted full → streambox.** `park_streambox_brain_units` disables the AEC
  reconciler but no path removes the unit *files*, so the accessory reconciler
  keeps running while the gate owner sits installed-and-disabled. `start` on a
  disabled unit succeeds, and `restart_voice` would `systemctl enable
  jasper-voice` — re-arming the voice brain on a Zero-class box whose profile
  exists to keep it off. Hence `enabled`, not a zero exit, is the permission.

The two shapes that do not start the owner fall back to `try-restart
jasper-voice`: a no-op unless voice is already running (measured on jts4,
2026-08-07: rc=0 with `ExecMainStartTimestamp` untouched on both an `inactive`
and a `failed` loaded unit, while a plain `start` does re-run it).

### Push-to-talk-only mode and its runtime gap

The daemon derives the mode **once** — `WakeLoop._push_to_talk_only`, from what
it actually opened (zero wake legs plus at least one manual source) — and every
consumer reads that derivation: the keepalive branch, the `NO_ROOM_MIC`
refusal, and `session_status()` → **`/state.voice.push_to_talk_only`**. The
`/state` field is why the mode is visible rather than inferred: an empty
`wake_legs` list alone reads identically to a daemon whose legs all failed to
open, which is the opposite diagnosis. `jasper-doctor`'s wake-leg check reads
the same two published facts and reports `n/a` on this box instead of a
permanent yellow.

On a zero-leg box the daemon substitutes a keepalive tick
(`PTT_KEEPALIVE_INTERVAL_SEC`, 2 s) for the primary mic's frame stream, which
was also the Tier-1 heartbeat's liveness proof. The tick is an honestly weaker
claim — a frame proves capture *and* the loop, a tick only the loop — which is
why `_require_usable_input` refuses to run a daemon that opened neither a wake
leg nor a manual mic; otherwise it would pat its watchdog forever while deaf.

**Startup only.** Nothing watches the accessory's liveness at runtime:
`UdpMicCapture.frames()` has no timeout, so a dead remote blocks its task
rather than ending it, and the tick keeps patting `WatchdogSec` — a dead remote
reads as a healthy speaker to systemd, `/state`, and the doctor alike. Tracked
as issue #2243. The detector cannot be a frame timeout (silence is a
push-to-talk device's steady state); it is Bluetooth connection state, which
the accessory reconciler owns.

Runtime, not gate: `manual_session_start` with **no source** on such a speaker
is refused (`NO_ROOM_MIC`) with a WARN and the `no_room_microphone` cue.
Accepting it opened a turn nothing could feed — music ducked, chirp played,
zero bytes sent, killed by the idle watchdog ~20 s later with no warning,
because both end-of-turn warnings are keyed on `bytes_sent > 0`.

### Restart hygiene

`restart_voice` queues `systemctl --no-block restart`: `jasper-voice` is
`Type=notify` and may itself be waiting on startup dependencies, so the AEC
oneshot must apply its verdict and exit rather than blocking inside PID 1's job
graph. `jasper-aec-reconcile.service` carries `TimeoutStartSec=120` (sized for
the chip-AEC alignment sequence), so a future blocking child becomes a visible
failed unit instead of an indefinite `activating` state.

`restart_voice` is also **change-gated**: a pass that proves no voice-relevant
change skips the voice restart and the chip-AEC / software-AEC3 stack bounce —
stable journal events `event=aec_reconcile.voice_restart_skipped`,
`…chip_aec_bounce_skipped`, `…aec_stack_bounce_skipped` — so a measurement-mic
hotplug no longer costs ~8 s of mid-song deafness. "No change" is proven, never
assumed: env writes are compared against a pass-start snapshot of `jasper.env`,
and a `/run` stamp records the non-env inputs voice starts from (the install
manifest, the published accessory-mic sources, `grouping-voice.env` content).
Callers whose kick means "restart" rather than "hardware may have drifted"
declare intent — install passes via `--reason install`, enhanced-AEC v2
activation via the one-shot `/run/jasper-aec-reconcile/voice-restart-intent`
marker. Every unknown fails toward restarting. Truth lives in
`voice_restart_can_be_skipped` / `voice_start_inputs` in
[`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile).

## Layer 2 — clean exit (backstop)

Layer 1 cannot cover three residual cases: a custom mic the reconciler will not
gate; a mic ALSA enumerates but PortAudio cannot *open* (busy, firmware
glitch); and the first boot of a fresh no-mic box before any reconcile.

In [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) the leg
factory opens the **primary** ("on") wake leg's mic. On failure it raises
`InputDeviceUnavailable` ([`jasper/audio_io.py`](../jasper/audio_io.py));
`main()` catches it, logs, and exits **66** (`os.EX_NOINPUT`,
`VOICE_MIC_UNAVAILABLE_EXIT`). The unit lists `66` in both
`SuccessExitStatus=` and `RestartPreventExitStatus=` — the same treatment
provider-unset gets with `78` — so the daemon parks clean.

Deliberate trade-off: a parked daemon does **not** auto-retry; it waits for a
reconcile/udev event or a manual restart. Standard mics recover on replug via
udev → reconcile → `restart_voice`. A custom mic, or a transient "device busy"
with no re-enumeration event, parks until the next reconcile. That is the price
of "never reboot-loop": a persistent open failure is a real fault that should
be visible, not retried into a reboot.

## Layer 3 — idle vs broken, from one reader

Every status surface *displays*
[`jasper.mic_presence.read_mic_presence()`](../jasper/mic_presence.py) instead
of independently re-probing ALSA / `lsusb` / PortAudio (which is how "no mic"
used to surface as a scatter of contradicting lines). The reader is
**mic-agnostic**: `present` is driven by the generic gate marker (true for the
XVF `Array`, the `L16K6Ch` variant, or a custom non-XVF mic such as a UMIK-2),
while the XVF runtime-profile JSON (`/run/jasper-mic-profile/xvf3800.json`) is
XVF-only *enrichment*. Driving presence off the XVF profile would report a
working non-XVF mic as absent; the separation generalises when a second mic
profile lands (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)).

- **`jasper-doctor`.** `check_microphone`
  ([`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py)) states
  present/absent + why in one line — `warn` when absent, never `fail`. The
  per-device checks defer via `read_mic_presence().absent_confirmed` rather
  than re-running `arecord -L` into a contradicting red. A genuine open failure
  with a mic *present* still falls through to the probe and its fail. The
  resilience domain's service-runtime check treats `inactive` as ok and
  `failed`/`activating` as fail, so a Layer-1 park reads ok and a crash reads
  fail.
- **`/state`.** A top-level `microphone` block carries the record (present,
  reason, card, variant, channels, a ready-made `summary`); the voice block's
  `parked_no_mic` derives from the **same** read, so boolean and record cannot
  drift.
- **`/mic` and the landing page.** The endpoint returns first-class `status`
  values: `parked` for a bonded follower, `starting` while systemd reports
  `jasper-voice` activating/reloading/deactivating, `offline` for a real
  unreachable daemon. The page is a dumb renderer: `starting` reads as
  "Reloading", not permanent failure.
- **Open-failure log.** `_log_audio_open_failure` logs one line and skips its
  portaudio/`arecord`/`aplay`/`dmesg` dump when the mic is confirmed-absent —
  the dump is for *surprise* failures.
- **Journal.** PID 1 logs the condition skip; the reconciler logs the marker
  write with its reason (`event=aec_reconcile…`).

## The output ladder

`jasper-outputd.service`'s `ExecCondition` parks the unit cleanly when the
resolved `JASPER_AUDIO_DAC_CARD` is gone and the backend is not `fake` — the
simple "configured card vanished" case, caught before the Rust process opens
ALSA. Everything beyond that is out-of-band (ADR-0141):

1. **udev add/change/remove on ALSA control nodes** triggers
   `jasper-audio-hardware-reconcile.service` through `SYSTEMD_WANTS` — the
   generic surface for current and future output hardware.
2. **Apple USB remove** additionally runs
   [`jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug),
   covering remove paths where the disappearing `controlC*` node does not
   activate `SYSTEMD_WANTS`.
3. **outputd failure** runs
   [`jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile)
   from `ExecStopPost`. It skips normal stops and `ExecCondition` parks;
   refreshes `outputd.env` before the next built-in restart; gives `EX_CONFIG=78`
   one bounded reconcile plus restart; and parks the unit on the 4th
   consecutive content-lane open failure, recording reason, lane-specific
   action, and re-arm paths in `/run/jasper-outputd-content-lane.state`.
   `jasper-doctor` and `/state.resilience.content_lane` read that record
   through [`jasper/control/content_lane_state.py`](../jasper/control/content_lane_state.py)
   — a surface, so do not reword the record's fields casually.

Half-present composites: a saved **roleful** composite (declared drivers
needing per-driver DSP) parks to `JASPER_OUTPUTD_BACKEND=fake` rather than
letting a surviving child take the box; a saved **passive** composite keeps
`single_alsa`. Owner: `apply_saved_topology_policy` in
[`jasper/output_hardware.py`](../jasper/output_hardware.py).

`/sound/` keeps the saved speaker topology separate from the currently attached
hardware: a saved dual-Apple active topology is not silently deleted when one
DAC is unplugged; the page shows both plus a mismatch blocker before
active-speaker commissioning actions. `jasper-doctor` uses the same split —
"Output hardware state" reports current reconciler-owned hardware, "active
speaker output hardware" owns saved-topology mismatch.

## HID and Bluetooth accessories

[`jasper-input`](../deploy/systemd/jasper-input.service) runs a pyudev hot-plug
monitor in-process and opens evdev fds as devices appear, so it converges both
directions without per-device units.

The WiiM Remote 2 mic differs because its audio arrives over a BLE GATT
notification stream. Its adapter
[`jasper-wiim-remote-mic`](../deploy/systemd/jasper-wiim-remote-mic.service) is
profile-gated by
[`jasper-accessory-reconcile`](../deploy/systemd/jasper-accessory-reconcile.service):

- **No paired remote** — `accessory-mics.env` is removed and the adapter
  disabled, so there is no resident BLE decoder and no UDP listener in voice.
- **Paired** — the reconciler writes `wiim_remote_2=udp:9892`, enables the
  adapter, and restarts `jasper-voice` only when the published manual-mic env
  changes and voice is already active.
- Install/env-change reconciles **restart** the adapter so code/config updates
  take effect; no-change boot/connect reconciles **start** it, a no-op for an
  already-running adapter that avoids interrupting a live mic session.
- Reconcile runs at boot/deploy and after successful pair/connect/forget, so
  the UI pairing flow converges without a second deploy. Adapter changes are
  queued `--no-block` and the boot reconciler orders only before `jasper-voice`,
  never before the adapter it may start, so optional accessory state cannot
  wedge voice startup. `TimeoutStartSec=60` on the accessory reconciler bounds
  future blocking mistakes (grouping's own bound is 300 s because it may join a
  pre-existing accessory and fan-in USB activation before queuing fresh
  post-role passes).
- A paired-but-sleeping remote self-heals: a missing GATT report logs
  `event=wiim_remote_mic.not_ready` (throttled after the first) and retries.

### The connection-event reservation

Right after `char.call_start_notify()`, the adapter asks jasper-control's
restart broker to start
[`jasper-wiim-remote-ce`](../deploy/systemd/jasper-wiim-remote-ce.service), a
root oneshot that issues one `HCI_LE_Connection_Update` on the live link
(privilege model in
[HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md); the
decision and the declined re-arm machinery in ADR-0142). Without it the mic
runs at roughly a quarter of realtime. **Realtime delivery therefore also needs
`jasper-control` up** — the broker socket is the non-root adapter's only route
to the helper.

Reading the journal, worst-to-best:

| event | meaning |
|---|---|
| `wiim_remote_mic.ce_request ok=0` | broker unreachable / unit missing / request rejected. Audio flows starved; the mic is not dead |
| `wiim_remote_ce.skipped` (WARNING) | helper ran, changed nothing — `no_le_links`, `no_match`, `ambiguous`, `handle_reused`, `bluez_unavailable`, `conn_list_failed`. In production any of these is an anomaly |
| `wiim_remote_ce.not_applied` (WARNING) | controller refused or never confirmed; the unit exits 1 so it shows in `systemctl --failed` |
| `wiim_remote_ce.applied` | requested and confirmed. The `ce_*_requested` field names are deliberate: `LE Connection Update Complete` carries no CE fields, so interval/latency/timeout are confirmed and the CE values are only what we asked for |

**Applied is not a guarantee it is still in force** — a peripheral-initiated
parameter update silently reverts it with no log line at all (ADR-0142). Read
the delivery *rate*, not a packet count:

```sh
journalctl -u jasper-wiim-remote-mic | grep 'event=wiim_remote_mic.segment'
# event=wiim_remote_mic.segment packets=625 duration_ms=9984 rate_hz=62.5
```

One line per stream segment — in practice one push-to-talk hold — emitted when
the next hold begins or the link drops mid-hold. **`rate_hz` near 62.5 is
realtime; near 15 is a starved link.** The count alone has no denominator (the
same 10 s hold reads `packets=625` healthy and `packets=148` starved), the
packets still decode so `bad_packets` stays 0, and the starved ~67.5 ms
inter-packet gap is well under the 250 ms `WIIM_STREAM_GAP_SEC`, so
`stream_reset` cannot fire either. The cumulative `packets` in
`event=wiim_remote_mic.disconnected` spans idle time as well as holds — do not
derive a rate from it. Keep two symptoms apart: low `rate_hz` with
`bad_packets=0` is a *delivery* problem and points here; distorted audio is
decode and does not.

## Still needs a hardware pass

Hardware-free coverage exists for the marker's write/remove paths, the unit's
gate and exit codes, the exit-66 park, the output hotplug/failure helpers, and
the `/sound/` + doctor saved-vs-attached split. What no box has confirmed:

1. **Cold boot with no mic** never starts `jasper-voice` (`NRestarts=0`,
   `ActiveState=inactive`) and no reboot occurs.
2. **Hot-unplug the XVF3800 while running** — udev fires the reconciler, voice
   stops cleanly, the marker appears, `/state.voice.parked_no_mic=true`, no
   restart spiral.
3. **Hot-plug it back** — marker removed, voice up within seconds, wake works
   with no manual step.
4. **Deploy a box with no mic attached** — install succeeds, voice parks
   labelled rather than failed, and post-deploy verification does not call
   voice healthy.
5. **Output DAC unplug/replug**, including the dual-Apple case: reconcile runs,
   `/run/jasper-output-hardware/output_hardware.json` reports the survivor, and
   a **roleful** saved composite yields `status=partial` +
   `saved_composite_partially_present`, `JASPER_OUTPUTD_BACKEND=fake`, and
   `event=audio_hardware_reconcile.output_parked`. Either way `jasper-outputd`
   must not reach its start limit, and replug must un-park with no manual step.
6. **An accessory-only speaker answering end to end** via the remote's button.

## Files

- [`deploy/systemd/jasper-voice.service`](../deploy/systemd/jasper-voice.service) — `ConditionPathExists` gate, exit-66 park
- [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile) — single writer of the marker; publisher of `JASPER_LOCAL_MIC_PRESENT`
- [`deploy/systemd/jasper-aec-reconcile.service`](../deploy/systemd/jasper-aec-reconcile.service) — bounded oneshot wrapper for mic/AEC/voice convergence
- [`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py) — marker path + `voice_parked_no_mic()`
- [`jasper/voice_daemon.py`](../jasper/voice_daemon.py) — `_configured_wake_legs`, the push-to-talk keepalive, the `NO_ROOM_MIC` refusal + cue
- [`jasper/accessories/mic_env.py`](../jasper/accessories/mic_env.py) — accessory-mic env path + entry format
- [`jasper/accessories/wiim_remote_mic.py`](../jasper/accessories/wiim_remote_mic.py) — BLE adapter, packet stream, per-segment rate log
- [`jasper/mic_presence.py`](../jasper/mic_presence.py) — the voice-input SSOT reader
- [`jasper/audio_io.py`](../jasper/audio_io.py) — `InputDeviceUnavailable`; absent-aware open-failure log
- [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) — raise on primary mic-open failure, exit 66
- [`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py) — `check_microphone` headline + mic checks deferring to the reader
- [`jasper/control/server.py`](../jasper/control/server.py) — `/mic` parked/starting/offline surface
- [`jasper/control/state_aggregate.py`](../jasper/control/state_aggregate.py) — `microphone` block, `voice.parked_no_mic`, `resilience.content_lane`
- [`deploy/udev/99-jasper-audio-hardware-reconcile.rules`](../deploy/udev/99-jasper-audio-hardware-reconcile.rules) — output-DAC add/remove/change triggers
- [`deploy/bin/jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug) — Apple USB remove reconciler request
- [`deploy/bin/jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile) — retry-time env refresh + content-lane park
- [`deploy/systemd/jasper-outputd.service`](../deploy/systemd/jasper-outputd.service) — output device gate + failure-time hook
- [`deploy/systemd/jasper-accessory-reconcile.service`](../deploy/systemd/jasper-accessory-reconcile.service) — optional accessory mic profile gate
- [`deploy/systemd/jasper-wiim-remote-mic.service`](../deploy/systemd/jasper-wiim-remote-mic.service) — optional BLE remote mic adapter
- [`deploy/systemd/jasper-wiim-remote-ce.service`](../deploy/systemd/jasper-wiim-remote-ce.service) + [`jasper/cli/wiim_remote_ce.py`](../jasper/cli/wiim_remote_ce.py) — per-connection BLE connection-event reservation

Last verified: 2026-08-26 (triage pass — the gate marker path and its unit
condition, exit-66 in `SuccessExitStatus`/`RestartPreventExitStatus`,
`JASPER_LOCAL_MIC_PRESENT` and its `Config`/`_configured_wake_legs` readers,
`push_to_talk_only`, `PTT_KEEPALIVE_INTERVAL_SEC`, `NO_ROOM_MIC`, the mic
presence reader's warn-never-fail doctor headline, the outputd `ExecCondition`
and `ExecStopPost` helper's content-lane park and its `/state` reader, and the
WiiM segment/CE event names all rechecked against their owning files. No box
was probed: the jts4 `try-restart` and CE measurements keep their 2026-08-06/07
dates as stated. Decisions moved to ADR-0139..0142.)
