# USB Gadget Audio Source (`jasper-usbsink`) — Operational Reference

**Status**: shipped 2026-05-23 (rebased onto main with Tier 1 fixes)
**Branch**: `feat/usb-gadget-source` → merged via `claude/usbsink-rebase-tier1`
**Owner**: Jasper
**Predecessor project**: [PiCorrect](https://github.com/jaspercurry/PiCorrect) — proves the
UAC2 gadget + CamillaDSP stack on Pi 5 hardware

> ### Current operational truth (updated 2026-07-04)
>
> USB Audio Input is shipped and off by default. **Gadget ownership moved
> to a composite model** — the ConfigFS descriptor is now owned by
> `jasper-usbgadget.service`, which composes an always-on USB management
> network (`ncm.usb0`) alongside the wizard-toggled audio function
> (`uac2.usb0`). The old audio-only `jasper-usbsink-init.service` is
> deleted. Gadget composition, the function truth table, the management
> network (NM keyfile + scoped dnsmasq), OS support for the network side,
> and the hardware-validation checklist are now owned by
> [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) — this doc keeps only the
> audio-source concerns (volume model, fan-in wiring, low-latency route).
>
> The installer writes the gadget overlay/config and requires reboot for
> the dwc2 controller to enter peripheral mode; `/sources/` toggles the
> disabled-by-default USB intent unit (`jasper-usbsink.service`). The
> host-visible audio device is composed by `jasper-usbgadget.service`, not
> owned by it alone: turning the `/sources/` toggle on/off both changes
> `jasper-usbsink.service`'s enablement **and** restarts
> `jasper-usbgadget.service` so it recomposes with or without `uac2.usb0` —
> stopping the intent unit alone no longer touches the gadget descriptor at
> all (the old "stopping the intent unit alone does not remove the
> host-visible device" trap is gone: the `/sources/` handler now explicitly
> restarts the gadget on both directions). When this speaker is a bonded
> multiroom follower, the local-source lifecycle registry
> (`jasper/local_sources/registry.py`) parks USB audio by stopping the
> audio units and **restarting** (not stopping) the gadget unit, so the
> host-visible audio device disappears while the always-on management
> network keeps serving that follower's own management UI; unparking
> mirrors this. `jasper-usbsink.service` keeps its own
> `ExecCondition=/opt/jasper/.venv/bin/jasper-local-source-allowed`, so a
> boot or manual start while the speaker is a parked bonded follower skips
> before the audio daemon can run — the gadget's network function is
> unaffected by that gate. At runtime, `jasper-usbsink`
> is a peer music renderer: the production bridge is the Rust
> `jasper-usbsink-audio` binary, which captures S32_LE stereo/48 kHz
> from `hw:UAC2Gadget`, narrows deterministically to S16_LE by signed
> high-word truncation, and writes period-aligned blocks into
> `usbsink_substream`. `jasper-fanin` then sums that lane with AirPlay,
> Spotify, Bluetooth, TTS, and correction audio before CamillaDSP/AEC.
> Diagrams below that show direct writes to `hw:Loopback,0,0`, Python
> PortAudio data-plane behavior, or the lean FIFO route are historical
> unless explicitly marked as future/lab work.
>
> The production claiming route is `usb_low_latency_48k`: Rust bridge
> `256` frames / `3` periods, fan-in USB input resampler enabled with
> target `512` + warm-up cushion `1536` (held target `2048`), fan-in
> input buffer `4096`, fan-in output buffer `1024`, CamillaDSP
> `256/1536`, and outputd `128/256` on the Apple USB-C DAC profile.
> A clean 5-minute jts.local steady-state sample on 2026-07-02 produced
> zero new bridge xruns/underflows, zero fan-in resampler relocks, and
> zero resampler silence. The low-latency claim still requires a
> route-latency click/capture artifact before doctor will pass it. Produce that
> artifact with `sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact`
> after an external click/capture harness has measured per-impulse latencies;
> the canonical gate and command examples live in
> [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md).
>
> Cross-cutting source metadata lives in `jasper/music_sources.py`:
> `Source.USBSINK` uses `VolumeMode.CAMILLA_MASTER`, so CamillaDSP is
> the outbound volume carrier and the host slider is observed inbound.
> Operational lifecycle resources live in `jasper/local_sources/registry.py`,
> which separates the USB bridge daemon from the host-visible gadget.
> `jasper-mux` owns source selection/preemption, and the landing-page
> `/source/select` surface can choose USB without enabling/disabling
> the source.
>
> The bridge publishes `/run/jasper-usbsink/state.json` with
> `{playing, preempted, host_connected, rms_dbfs, ring, counters,
> period_frames, updated_at, tap, host_clock}`. `tap` (the Stage 0
> impulse-click ingress evidence tap) and `host_clock` (the Stage 1
> default-OFF host-slaved USB clock ladder/telemetry) are documented in
> [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md), not here — that
> doc is their single source of truth.
> `rms_dbfs` is a finite JSON number or `null` before any finite sample exists;
> the bridge may use `-inf` internally, but state files and `/state` stay
> standards-compliant JSON.
> `playing` is the RMS/hysteresis signal from the host stream;
> `preempted` is separate mux state and does not change `playing`.
> Mux preempts USB via the local `/preempt` endpoint on port 8781.
> USB capture idleness is normal: the feature may be enabled while no
> host is plugged in, while a host is plugged in but paused, or while
> another renderer is being used. `jasper-usbsink` therefore treats
> ALSA playback-period forward progress as daemon liveness for the systemd
> watchdog, because the Rust bridge should keep writing either host audio or
> explicit silence into `usbsink_substream`.
>
> Disabled cost is effectively zero resident daemon memory; enabled
> bridge cost is about 2 MB Pss for the Rust data plane, plus the
> non-real-time host-volume observer when enabled. When adding another music source, use
> `docs/audio-paths.md#adding-a-new-music-source` as the canonical
> checklist. This document's phase plan below is retained for
> historical implementation context.
>
> `deploy/lib/install/renderers.sh` writes
> `dtoverlay=dwc2,dr_mode=peripheral` under `[all]`, not `[pi5]`.
> That is deliberate: streambox installs expose USB Audio Input on
> Zero-class hardware so JTS4 can validate a powered USB splitter plus
> DAC topology. The Pi 5 splitter-backed path remains the proven path;
> the Zero 2 W path is an allowed hardware experiment because the same
> OTG controller may also be needed for the DAC. If the powered splitter
> cannot keep the host-facing gadget and Apple DAC stable at the same
> time, remove or gate USB Audio Input for Zero-class streamboxes rather
> than letting it silently compromise playback.
>
> Current production-boundary pins:
>
> 1. **Rust is the only USB data plane.**
>    [`deploy/systemd/jasper-usbsink.service`](../deploy/systemd/jasper-usbsink.service)
>    runs `/opt/jasper/bin/jasper-usbsink-audio`. The old Python/PortAudio
>    bridge (`jasper/usbsink/daemon.py`, `audio_bridge.py`, `usbsink_main.py`,
>    the `jasper-usbsink-python-lab` entrypoint) and its lean-FIFO delivery
>    variant were **deleted** in the USB dead-pipeline sweep; the sections below
>    that describe them are archaeology, not current code.
> 2. **Asoundrc path migration.** The codebase moved
>    `/root/.asoundrc` → `/etc/asound.conf` (mode 0644, world-readable)
>    in PR #223. usbsink doesn't reference asoundrc by path; the
>    `usbsink_substream` name resolves through the system-wide
>    `/etc/asound.conf` that `install.sh` writes.

## Status & scope

USB gadget audio becomes a fourth music source alongside AirPlay, Spotify
Connect, and Bluetooth A2DP. The user plugs a computer into the Pi via
USB-C, and when this speaker is solo or a pair leader the computer sees
the configured speaker name as a USB audio output device; audio flows
through the existing CamillaDSP chain to the speakers. A bonded follower
parks the host-visible gadget so the computer does not see that follower
as an independent output device.

PLAN.md previously marked this as v8 "Blocked on Pi linux #6289 / #6569
being fixed". That deferral is obsolete: PiCorrect resolves #6289 by
introducing the **8086 Consultancy USB-C/PWR Splitter** between the Pi
and the host computer (the host sees USB-A on its end, sidestepping the
USB-C-to-USB-C enumeration quirk on kernels >6.6.42). The historical
risk register below keeps the kernel-quirk archaeology; current
operation assumes the splitter-backed path.

**In scope**
- Host computer → configured speaker name as a USB audio output
  (unidirectional, host-side is playback-only)
- Host volume slider drives JTS canonical `listening_level` (Mac volume
  feels like spinning the dial)
- Latest-source-wins arbitration via `jasper-mux`
- On/off toggle in `/sources/` wizard
- Disabled by default
- Zero RAM cost when disabled (no kernel modules loaded, no daemon
  running, no ALSA card present)
- AEC works transparently (USB audio enters `usbsink_substream`;
  `jasper-fanin` sums it into substream 7, which `pcm.jasper_capture`
  exposes as the music reference)

**Out of scope (explicit non-goals)**
- USB-side capture (host recording from JTS mic over USB) — would
  require a UAC2 input endpoint, no use case for it now
- Multi-host (two computers plugged in at once) — UAC2 gadget is
  single-host by spec
- Bit-perfect / high-resolution audio (96k/192k, DSD, etc.) — the
  gadget is fixed at 48 kHz S32_LE stereo, which downmixes inside the
  host's audio stack with no loss for any practical music source
- Routing JTS speaker output back over USB (loopback-to-host) — host
  sees the speaker as a one-way sink
- Configurable gadget VID/PID/manufacturer strings via wizard — single
  set baked into the boot script, settable via env if a user needs it
- Hot-changing the dtoverlay state at runtime — requires a reboot, and
  install.sh writes it once

## Executive summary

The USB gadget feature reuses the existing renderer-into-Loopback
pattern. A oneshot service `jasper-usbsink-init.service` performs the
ConfigFS gadget setup at start. The runtime is split deliberately:

1. `jasper-usbsink.service` starts the Rust `jasper-usbsink-audio` data
   plane, looping audio from the gadget capture endpoint into
   `usbsink_substream` so it joins the fan-in music chain.
2. `jasper-usbsink-volume.service` is a non-real-time helper that polls the
   gadget's `PCM Capture Volume` mixer control and forwards changes to
   `VolumeCoordinator.observe_source_volume()`.
3. The Rust bridge computes RMS-based playing state and publishes ring/counter
   health to `/run/jasper-usbsink/state.json`.

Total new RAM when enabled: low single-digit MB for the Rust data plane plus
the non-real-time volume helper.
Total new RAM when disabled: **0 MB** (no service runs, no kernel
modules loaded, no gadget descriptor present). The dtoverlay
`dwc2,dr_mode=peripheral` is permanently set after install but costs
only the ~50 KB dwc2 kernel module loaded at boot.

The user-facing model is exactly AirPlay's: camilla-as-master for
volume, mux-arbitrated for source. Implementation mirrors the
`Source.AIRPLAY` case in `volume_coordinator.py` and the AirPlay branch
in `mux.py`, with USB-specific transports (ALSA mixer instead of
DACP/MPRIS, RMS instead of MPRIS PlaybackStatus).

## 1. Hardware setup

### Required hardware (one-time purchase)

| Item | Cost | Purpose |
|---|---|---|
| 8086 Consultancy USB-C/PWR Splitter | ~$30 | Splits Pi USB-C into data leg (to host) + power leg (to wall PSU). Bypasses Pi 5 USB-C-to-USB-C kernel issue #6289. User has confirmed they already have one. |
| USB-A-to-USB-C cable | ~$10 | Pi-side leg of the splitter to host computer. USB-A end at the host side is what sidesteps the kernel quirk. |
| Existing 27W USB-C PSU | $0 | Stays connected to the splitter's power leg, replaces the direct-to-Pi connection. |

### Physical topology

```
Wall outlet
   │
   ▼
27W USB-C PSU ───► 8086 Splitter ◄─── USB-A cable ◄─── Host computer
                       │
                       ▼ (combined power + data over USB-C)
                  Pi 5 USB-C port
```

The splitter stays permanently installed. The user's day-to-day:
unplug/plug the USB-A leg into whatever computer they want to use JTS
with. When no host is connected, JTS still powers up normally from the
wall PSU through the splitter; nothing about its standalone behavior
changes.

### Boot config change (one-time, requires reboot)

`/boot/firmware/config.txt` gains one line under `[pi5]`:

```
dtoverlay=dwc2,dr_mode=peripheral
```

This puts the BCM2712 SoC's DWC2 USB OTG controller into peripheral
mode permanently. The dtoverlay alone is a no-op from the host's
perspective — it just makes the port gadget-capable. `libcomposite` and the
ConfigFS descriptor are now owned by `jasper-usbgadget.service` (which
replaced the retired `jasper-usbsink-init.service`), and because that unit
carries the always-on USB *management network* it modprobes `libcomposite`
and composes the descriptor **by default at boot** — the `uac2.usb0` audio
function is the only part gated behind the `/sources/` toggle. See
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) for the composite-gadget
function truth table; it is canonical for gadget ownership.

**Side effect to document in BRINGUP.md**: the Pi 5 USB-C port is no
longer available for plugging USB host devices (e.g. flash drives). The
four USB-A ports remain in host mode unchanged.

## 2. RAM budget

**The gadget-level RAM contract (kernel modules, ConfigFS descriptor,
network-vs-audio composition, kill-switch behavior) now lives in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) "RAM contract"** — the
gadget is composite now, and the old "0 KB whenever USB audio is disabled"
framing no longer holds by itself, because the USB management network
defaults to **on** independent of the audio toggle. This section covers
only the **audio daemon's own** marginal cost on top of whatever the
gadget itself is already costing.

| Component | RAM (Pss) | Notes |
|---|---|---|
| `jasper-usbsink.service` (Rust bridge) | **~2 MB** | ALSA capture/playback bridge + state/preempt publisher. Runs only when the `/sources/` toggle is on. |
| `jasper-usbsink-volume.service` | non-real-time helper | Host volume observer; separate from the audio data plane. |
| **Total new RAM for audio, on top of the gadget's own baseline** | **low single-digit MB** | The audio bridge is no longer a Python/PortAudio process. |

The old Python/PortAudio bridge budget is preserved below as history only.
It is not the claiming `usb_low_latency_48k` data plane.

### Enforcement, post-composite-gadget

- `jasper-usbgadget.service` (not `jasper-usbsink-init.service`, which is
  deleted) owns `modprobe libcomposite` and the ConfigFS descriptor for
  **both** functions. See HANDOFF-usb-gadget.md for the full truth table.
- `jasper-usbsink.service` remains the disabled-by-default `/sources/`
  intent unit for the **audio** function specifically. Toggling it now
  also restarts `jasper-usbgadget.service` so the gadget recomposes with
  or without `uac2.usb0` — see HANDOFF-usb-gadget.md "Toggling audio from
  `/sources/`".
- Doctor verification: `jasper-doctor`'s composite-aware checks (rewritten
  for the new model — see `jasper/cli/doctor/usbsink.py`) confirm gadget
  composition matches intent, rather than the old binary
  "libcomposite loaded ⟺ usbsink active" invariant.

## 3. Architecture

### 3.1 Audio path

```
Host computer (USB-C via 8086 splitter)
   │ UAC2 OUT endpoint, 48 kHz S32_LE stereo
   ▼
hw:CARD=UAC2Gadget,DEV=0  (gadget capture endpoint, Pi-side)
   │
   │ jasper-usbsink-audio reads frames here
   │   (Rust ALSA capture, 256-frame periods, 3-period bounded ring)
   │
   ▼ writes here when not preempted; writes silence when preempted
pcm.usbsink_substream ──► hw:Loopback,0,3
                                          ▼ (loop)
                              hw:Loopback,1,3 ──► jasper-fanin
                                          ▼
                              pcm.jasper_capture (summed substream 7)
                                          ▼
                                  jasper-camilla
                                  (main_volume — the dial knob)
                                          │
                                          ▼
                                   pcm.jasper_out
                                  (dmix on Apple dongle)
                                          │
                                          ▼
                                    speakers
```

The gadget capture endpoint is the *Pi-side* read of audio the host is
sending. UAC2 terminology is host-relative: "playback" on the host
side = "capture" on the device side. That's why the ALSA control is
`PCM Capture Volume` even though logically it's the host's playback
volume.

Why the bridge instead of having CamillaDSP capture directly from the
gadget (as PiCorrect does):

PiCorrect's topology is single-source — the host is the *only* audio
input. JTS is multi-source — AirPlay, Spotify Connect, Bluetooth, and
now USB must all sum before CamillaDSP/AEC. The fan-in topology is the
mixing point, and the clean way to add USB is to make it a peer writer
into `usbsink_substream`. Bridging UAC2Gadget → fan-in is a small Rust ALSA
daemon and keeps CamillaDSP's capture configuration unchanged.

Latency budget (updated 2026-07-01 for the Rust bridge, fan-in USB
resampler, transport-pipe coupling, and Apple DAC-profile latency floor;
component estimates only until a route-latency artifact exists):
- Host → gadget USB endpoint: ~3-5 ms
- `jasper-usbsink-audio` bridge: 256-frame ALSA period with a bounded
  3-period ring. 128-frame periods and 256/2 failed on jts.local; 256/3
  is the stable floor.
- snd-aloop usbsink lane → fan-in: fan-in keeps the global 4096-frame
  input buffer because lower global input buffers (512/1024/2048/3072)
  failed the USB resampler lock tests and would regress AirPlay burst
  absorption. The USB resampler is the latency-control point, not the
  global input ring.
- fan-in USB resampler: route-owned target 512 + cushion 1536 (jts.local's
  box tuning; the shipped code default cushion is 2048), held target 2048.
  This replaces the old catch-up sawtooth for the USB lane.
- fan-in output buffer: 1024 frames ≈ 21 ms.
- CamillaDSP / outputd: Apple USB-C dongle profile is chunk 256 /
  target 1536, outputd period 128 / DAC buffer 256 after the
  2026-07-01 jts.local tuning pass. Camilla target 1024 and outputd
  64/128 both produced bridge playback xruns and are not the shipped floor.
- outputd content capture: direct ALSA loopback with buffer 1536. Lower
  640/768/1024/1280 content-buffer probes produced content-side xruns.
- **Claim status**: unproven until click/capture measurement. Doctor fails
  `route latency evidence` without a matching artifact: p95 needs >=200
  impulses over >=5 minutes and p99 promotion needs >=1000 impulses over
  >=30 minutes with jittered spacing. Run
  `sudo /opt/jasper/.venv/bin/jasper-route-latency-artifact` to write the
  artifact from measured samples or aggregate p95/p99 values and bind it to the
  live route identity; it is not itself the audio measurement harness.

> **Production low-latency knobs (2026-07-01).** The current claiming route
> uses the Rust bridge with `JASPER_USBSINK_BLOCK_FRAMES=256` and
> `JASPER_USBSINK_RING_PERIODS=3`. `JASPER_USBSINK_LATENCY=low` is now a route
> hint/state label, not a PortAudio runtime selector. `JASPER_USBSINK_OUTPUT_MODE`
> is always `aloop` now — the lean-FIFO (`fifo`) delivery variant and its
> runtime flip were **deleted** (the Rust bridge has only the aloop lane; the
> route reconciler records `aloop` for route identity and the daemon does not
> read it). The production
> `usb_low_latency_48k` route keeps USB in fan-in and uses the fan-in USB input
> resampler plus direct ALSA loopback through Camilla/outputd. Env action validation lives
> in `jasper.audio_runtime_plan`; the reconciler still owns the env write,
> restart, and rollback. Grammar lives
> in `.env.example`; the lane design is in
> [HANDOFF-audio-latency-foundation.md](HANDOFF-audio-latency-foundation.md).

The shared downstream tail is no longer one fixed global number: fan-in
output is the 1024-frame production floor, while CamillaDSP/outputd floors
come from the active DAC profile. See
[HANDOFF-audio-latency-foundation.md](HANDOFF-audio-latency-foundation.md)
for the current low-latency Apple-dongle budget.

The tuned values are stable enough for the fallback route, but they are **not**
a 40 ms end-to-end result. The fan-in USB resampler held target alone is 2048
frames (~42.7 ms at 48 kHz), before fan-in output, CamillaDSP, outputd content,
and DAC delay. UAC2 also has no AirPlay-style back-channel to tell the host to
delay video, so video/lip-sync remains unclaimed until a measured route artifact
proves otherwise. For now, a host-side manual audio offset is the only honest
video workaround.

### 3.2 Volume model — USB gadget is camilla-as-master

The clearest mental model: **USB gadget behaves like AirPlay**.
CamillaDSP's `main_volume` is the user-perceived speaker volume.
The host's slider is treated as an upstream observation, not as the
master.

Concretely, in `jasper.volume_coordinator.VolumeCoordinator`:
- `jasper/music_sources.py` declares `Source.USBSINK` with
  `VolumeMode.CAMILLA_MASTER` — joins `AIRPLAY` and `IDLE` as a
  camilla-as-master source
- Inbound observer: `volume_bridge.py` polls the gadget mixer controls
  `PCM Capture Volume` and `PCM Capture Switch` at 4 Hz (`amixer cget`;
  polling was chosen over pyalsa event subscription for restart
  robustness and one fewer dependency — see the module docstring). When
  the user moves the Mac slider, it calls
  `coordinator.observe_source_volume(Source.USBSINK, value_pct)`,
  which translates and updates `listening_level` + `camilla.main_volume`
- Outbound: dial twist / voice "louder" goes through the normal
  `_set_camilla` path. **No write back to the gadget mixer**.

#### Why no outbound write back to the host

PiCorrect uses `link_volume_control: "PCM Capture Volume"` in its
CamillaDSP config, which makes the host's slider drive CamillaDSP
volume directly. We don't do that for JTS because:

1. `main_volume` is also the ducking knob (`jasper.camilla.Ducker`).
   Voice sessions duck music, then restore. If the host's slider were
   wired directly to `main_volume`, ducking would push the host's
   slider visually down on the Mac, which is wrong.
2. The dial / voice / "louder" path must remain authoritative.
   Bidirectional sync would require either echo prevention on both
   ends (complex) or always-wins logic on one end (confusing).

The accepted UX consequence: if the user dials JTS up to 80% via the
rotary knob, and then later moves the Mac slider, the Mac slider jumps
to wherever the user set it on Mac (say 50%), and JTS instantly
follows to 50%. The Mac slider is a remote control; touching it
overrides JTS's current state. This is the same UX as AirPlay sender
sliders.

#### Translation: gadget mixer value → listening_level

UAC2 Volume Control is signed 16.16 dB (or signed 8.8 on some hosts).
The Linux `u_audio` driver normalizes this to ALSA `Volume` integer
units. The mapping is straightforward — `amixer cget` reports the control's
`min`/`max` integer units alongside the current value:

```python
# `amixer cget numid=N` reports `min=..,max=..` for the control;
# volume_bridge.py parses those (regex) — no Python ALSA binding.
# Integer units map 1:1 to dB at 0.01-dB resolution on UAC2.
min_v, max_v = (-12800, 0)   # parsed from amixer cget; e.g. -128.00..0.00 dB
def gadget_to_pct(raw: int) -> int:
    # Linear-in-dB mapping from gadget range to 0..100%
    # 0.00 dB at host slider 100%, min_v at host slider 0%.
    span = max_v - min_v
    return max(0, min(100, round((raw - min_v) / span * 100)))
```

The translation lives in `jasper/usbsink/volume_bridge.py` (new file).
A `gadget_pct_to_listening_level()` helper is the analogue of
`spotify_percent_to_listening_level()` in `volume_coordinator.py:99`.

#### Mute handling

Host's mute toggle hits `PCM Capture Switch`. Observer treats
mute-on as `level=0` (no separate mute concept exposed to
VolumeCoordinator; coordinator's own `mute()/unmute()` flow is for
the dial's mute button and voice). When the host unmutes, observer
reads the current volume value and sets that as `listening_level`.

### 3.3 Source arbitration

Mux integration follows the AirPlay pattern with one wrinkle: we
can't tell the host to pause. So when USB is preempted, the daemon
silences its own output (writes zeros to `usbsink_substream`) until
the host transitions in a way that mux recognizes.

#### Playing-state detection: RMS-based

Rather than relying on ALSA PCM state (`/proc/asound/UAC2Gadget/pcm0c0/sub0/status`),
which can return `RUNNING` while the host streams silence after pause,
we use RMS on the input audio:

```python
# In jasper-usbsink's audio callback (per-block):
rms_dbfs = 20 * math.log10(max(rms_linear, 1e-6))
# A block is RMS-active if rms_dbfs > -50 dBFS.
# State "playing" = sustained active for ≥1.0 s.
# State "idle" = sustained inactive for ≥2.0 s.
# Hysteresis keeps transient silence (track changes, brief pauses) from
# flapping the state.
```

The daemon publishes state to `/run/jasper-usbsink/state.json`:

```json
{
  "playing": true,
  "preempted": false,
  "host_connected": true,
  "rms_dbfs": -18.3,
  "updated_at": "2026-05-16T18:30:42.123Z"
}
```

`jasper.source_state.usbsink_playing()` reads this file (same shape
as `librespot_state.is_playing()` at `jasper/source_state.py:32`).

#### Preempt protocol

When mux detects a transition newly-started → other-source-now-winning,
it tells the USB daemon to silence:

```python
# In Mux._pause(Source.USBSINK):
# POST http://127.0.0.1:8780/usbsink/preempt {"silenced": true}
# (jasper-control hosts the endpoint; daemon polls or has its own
# HTTP listener.)
```

Two design choices for the daemon's preempt receiver:
- **A**: daemon listens on its own HTTP port (e.g. 8781 localhost).
  Mux posts directly.
- **B**: daemon polls `jasper-control` for its preempt state, or
  reads a state file `/run/jasper-usbsink/preempt.state` that mux
  writes.

**Recommended: option A** — the daemon already runs an async event
loop (sounddevice callback + mixer thread + state publisher), adding
a tiny `aiohttp` or stdlib `http.server` listener on localhost is
clean. Mux's `_pause()` method gains a USBSINK branch that POSTs to
`http://127.0.0.1:8781/preempt`.

A dropped POST (daemon restarting) is recoverable: on (re)start, the
daemon reads `/run/jasper-usbsink/preempt.state` written by mux as
a backup, so the silenced state survives daemon restarts.

#### Resumption protocol

When USB is preempted but the host is still streaming audio, RMS
stays high. The daemon publishes `playing=true` regardless of
preempted state — `playing` reflects what the user wants, `preempted`
reflects mux's mute. Mux uses this to decide:

1. **All other sources go idle** → mux clears all preempt flags
   (POSTs `preempt=false` to USB daemon). USB resumes forwarding.
2. **User pauses on host, then plays again** → RMS drops, then rises.
   Daemon sees this as a fresh inactive→active transition and
   publishes a `playing` edge to its state file. Mux's next tick sees
   USB as newly-started, preempts the current winner, and clears USB's
   preempt flag.

This requires a small Mux behavior change beyond just adding USBSINK
to the enum:

```python
# After detecting newly_started, if all sources are inactive, clear
# any per-source preempt overrides that might be lingering.
if not any(current.values()):
    # No source is playing right now. Release any USB preempt.
    if self._usbsink_preempted:
        await self._usbsink_release_preempt()
        self._usbsink_preempted = False
```

#### Bluetooth analogy

For AirPlay and Spotify, mux's `_pause()` calls a clean API. For
Bluetooth, mux now tries BlueZ AVRCP `MediaPlayer1.Pause` when the
phone/player exposes an AVRCP player object and logs a concrete fallback
when it cannot. For USB, the silencing-the-daemon approach gives us a
clean local pause that does not depend on upstream host cooperation —
the daemon silences its own output deterministically.

## 4. Component design (file map)

### 4.1 Boot-time gadget setup

> **Historical — superseded by the composite gadget.** The file names,
> `jasper-usbsink-init.service` unit listing, and single-function framing
> below are the pre-composite-gadget design. Current file names are
> `deploy/usbsink/jasper-usbgadget-{up,down,wanted}` and
> `deploy/systemd/jasper-usbgadget.service`; the gadget composes an
> always-on network function alongside this audio function. See
> [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) for the current unit and
> script design. Kept here for the PiCorrect-adaptation history and the
> UAC2 attribute rationale, which are still accurate.

**Files** (installed to `/usr/local/sbin/` by `install.sh`):

```
deploy/usbsink/jasper-usbsink-gadget-up     # ConfigFS gadget creation
deploy/usbsink/jasper-usbsink-gadget-down   # ConfigFS gadget teardown
deploy/usbsink/jasper-usbsink-wait-card     # waits for the ALSA card
deploy/systemd/jasper-usbsink-init.service
```

`jasper-usbsink-gadget-up` is adapted from
[PiCorrect's setup.sh:68-123](https://github.com/jaspercurry/PiCorrect/blob/main/setup.sh#L68)
with these modifications:
- Manufacturer/product strings: "Jasper Tech Speaker" /
  `"<speaker name> USB Audio"` (default "JTS USB Audio")
- Serial number derived from the same `/proc/cpuinfo` line PiCorrect
  uses
- Idempotency check: skip if `${CONFIGFS}/${NAME}` already exists and is
  bound to a UDC

`jasper-usbsink-gadget-down` is the inverse: unbind UDC, remove configs
and strings, rmdir the gadget directory. Best-effort — if the descriptor
is partially broken (rmdir fails because something is still bound), we
log and continue. The next gadget-up will handle it.

`jasper-usbsink-init.service`:

```ini
[Unit]
Description=Jasper USB sink — ConfigFS UAC2 gadget setup
After=systemd-modules-load.service sys-kernel-config.mount
Before=jasper-usbsink.service
DefaultDependencies=no
ConditionPathExists=/sys/kernel/config

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/sbin/modprobe libcomposite
ExecStartPre=-/usr/local/sbin/jasper-usbsink-name-patch
ExecStart=/usr/local/sbin/jasper-usbsink-gadget-up
ExecStartPost=/usr/local/sbin/jasper-usbsink-wait-card 30
ExecStop=/usr/local/sbin/jasper-usbsink-gadget-down
ExecStopPost=-/sbin/rmmod u_audio
ExecStopPost=-/sbin/rmmod libcomposite

[Install]
WantedBy=multi-user.target
```

`jasper-usbsink-wait-card 30` polls `/proc/asound/UAC2Gadget` for up to
30 seconds (PiCorrect's pattern). The enumeration race is real:
ConfigFS write returns before ALSA registers the card. (The
`jasper-usbsink-name-patch` ExecStartPre is the device-name patch —
see §4.1a.)

### 4.1a Host-visible device name (tracks the Speaker Name)

A connected Mac shows the speaker in its audio-output list using the
UAC2 **AudioStreaming interface string**, which the kernel hardcodes as
`"Playback Inactive"` (`STR_AS_OUT_ALT0`) / `"Playback Active"`
(`STR_AS_OUT_ALT1`) in `drivers/usb/gadget/function/f_uac2.c`. macOS
prefers this over the configfs-settable `iProduct` string, so the
"JTS USB Audio" product string the gadget-up script sets is *not* what
the Mac displays. As of Trixie's 6.12 kernel these AS strings are the
**one** gadget string not exposed through configfs (everything else —
`function_name`, `c_it_name`, clock names — is), so the compiled module
is the only lever. (Windows uses `iProduct` and already shows the
product string correctly; this is macOS-specific.)

We make the host label track the **Speaker Name** (`/system/` wizard →
`speaker_name.env`) by overwriting those two strings in a patched copy
of `usb_f_uac2.ko`:

- [`deploy/usbsink/uac2_name_patch.py`](../deploy/usbsink/uac2_name_patch.py)
  — stdlib-only byte transform (`patch_module_bytes`): finds the
  null-terminated tokens *by content* (offset-independent, so it
  survives the strings moving between kernel builds), overwrites them
  in place preserving length, null-padded. Bounded to **15 chars** (the
  shorter `"Playback Active"` slot), so both alt strings carry the same
  name and the label never flickers between idle/streaming. Names
  longer than 15 chars are truncated *only for this USB label* — the
  full name still drives `iProduct`, Bluetooth, etc.
- [`deploy/usbsink/jasper-usbsink-name-patch`](../deploy/usbsink/jasper-usbsink-name-patch)
  — bash orchestrator (mirrors the `jasper-wifi-guardian` self-heal
  idiom). Builds the patched module into the kernel's
  `/lib/modules/$(uname -r)/updates/usb_f_uac2.ko` override (modprobe
  searches `updates/` before `kernel/`), runs `depmod`, and `rmmod`s a
  stale in-memory module so the next gadget-up autoloads the override.
  A marker (`kernel ver + name + stock-module hash`) makes the
  steady-state boot a millisecond no-op. Structured `event=usbsink_name.*`
  logs.

Wired as `jasper-usbsink-init`'s **best-effort** `ExecStartPre`
(leading `-`): it runs before gadget-up so the patched module is loaded
when the function is created. This one hook covers boot, feature-enable,
**rename** (the speaker-name save handler in
[`jasper/web/speaker_setup.py`](../jasper/web/speaker_setup.py)
`_apply_name()` already restarts `jasper-usbsink-init` when USB sink is
active; the marker flips and the module is rebuilt + reloaded), and
**kernel updates** (a
new kernel boots with no override → it's rebuilt from the new stock
module before the gadget comes up).

**Why a binary patch and not DKMS.** This codebase ships no kernel
headers / compiler / dkms, and a 1 GB appliance shouldn't grow them. A
binary patch needs none of that and **degrades cosmetically**: if a
future kernel renames the string, the content search misses, the script
logs `event=usbsink_name.patch_failed`, removes any stale override, and
the gadget comes up with the stock `"Playback Inactive"` label — USB
audio is never broken, only the name reverts. `jasper-doctor`'s
`check_usbsink_name` warns when the override is absent/stale so the drift
is visible. (Reverting entirely: `rm /lib/modules/*/updates/usb_f_uac2.ko`
+ `depmod`, or a reboot if only an in-memory `insmod` was used.)

### 4.2 jasper-usbsink daemon

> **Historical Python bridge design — code deleted.** The production service
> runs `/opt/jasper/bin/jasper-usbsink-audio`. The Python package described below
> (`daemon.py`, `audio_bridge.py`, `state_publisher.py`, `preempt_listener.py`,
> `usbsink_main.py`) was **removed** in the USB dead-pipeline sweep — only
> `volume_bridge.py` (the `jasper-usbsink-volume` poller) survives. The
> package/script sketch below is retained purely as archaeology of the original
> implementation; the files it names no longer exist.

**Original Python package**:

```
jasper/usbsink/
  __init__.py
  daemon.py            # Main async loop, wires the pieces together
  audio_bridge.py      # sounddevice InputStream → ALSA OutputStream
  volume_bridge.py     # amixer-cget mixer poller (4 Hz)
  state_publisher.py   # /run/jasper-usbsink/state.json writer
  preempt_listener.py  # localhost HTTP receiver for mux preempt
```

`jasper-usbsink.service`:

```ini
[Unit]
Description=Jasper USB sink — audio bridge + volume observer
After=jasper-usbsink-init.service jasper-camilla.service
Requires=jasper-usbsink-init.service
PartOf=jasper-usbsink-init.service

[Service]
Type=notify
EnvironmentFile=/etc/jasper/jasper.env
EnvironmentFile=-/var/lib/jasper/usbsink.env
ExecStart=/opt/jasper/.venv/bin/jasper-usbsink
Restart=on-failure
RestartSec=2s
WatchdogSec=15s
Nice=-10
RuntimeDirectory=jasper-usbsink
RuntimeDirectoryMode=0755

# RAM caps — generous but bounded. If we ever go over, that's a bug
# in the daemon (memory leak) and we want OOM-killer to catch it.
MemoryMax=64M
MemoryHigh=48M

[Install]
WantedBy=multi-user.target
```

**Daemon entry point** (`jasper/cli/usbsink_main.py`, mirrors
`jasper/cli/aec_bridge.py`):

```python
import asyncio
import logging
import signal
import sys
from jasper.usbsink.daemon import UsbSinkDaemon

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = UsbSinkDaemon.from_env()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_stop)
    return loop.run_until_complete(daemon.run())

if __name__ == "__main__":
    raise SystemExit(main())
```

Registered in `pyproject.toml` `[project.scripts]` alongside
`jasper-aec-bridge`, `jasper-doctor`, etc.

#### `audio_bridge.py` — sounddevice loop

```python
import sounddevice as sd
import numpy as np

class AudioBridge:
    """sounddevice InputStream from gadget → OutputStream to Loopback.

    Single shared callback to avoid two-thread latency stacking.
    Silenced output when `preempted` is True, computed RMS published
    via `last_rms_dbfs` for the state publisher to read."""

    def __init__(self, capture_device: str, playback_device: str,
                 samplerate: int = 48000, blocksize: int = 480):
        self.capture_device = capture_device
        self.playback_device = playback_device
        self.samplerate = samplerate
        self.blocksize = blocksize  # 10 ms @ 48k
        self.preempted = False
        self.last_rms_dbfs: float = -120.0
        self._stream: sd.Stream | None = None

    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            logger.warning("sounddevice status: %s", status)
        # Compute RMS for state publisher.
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        self.last_rms_dbfs = 20 * math.log10(max(rms, 1e-6))
        if self.preempted:
            outdata.fill(0)
        else:
            outdata[:] = indata
```

`sd.Stream` is duplex — one device for capture, another for playback,
single callback fires per block. PortAudio handles the rate matching;
both ends are 48 kHz so it's a straight copy.

#### `volume_bridge.py` — `amixer` mixer poller

Polls the gadget's `PCM Capture Volume` and `PCM Capture Switch`
controls at 4 Hz via `amixer cget` (subprocess), maps the raw value to
a 0-100 listening-level, and POSTs changes to jasper-control's
`/volume/set` with `source="usbsink"`. Polling — not pyalsa event
subscription — was chosen for restart robustness and one fewer
dependency: `amixer` ships with `alsa-utils`, which install.sh already
requires. The 4 Hz cadence (`POLL_INTERVAL_SEC = 0.25`) is imperceptible
for the sparse, user-driven slider moves it tracks.

The canonical implementation (and the full polling-vs-event rationale)
lives in
[`jasper/usbsink/volume_bridge.py`](../jasper/usbsink/volume_bridge.py)
— intentionally not reproduced here, since a code sketch duplicated into
this doc is exactly what drifted out of sync with the shipped daemon.

The `source: "usbsink"` field is new — jasper-control's `/volume/set`
gains a `source` field so the coordinator can call
`observe_source_volume(Source.USBSINK, ...)` (which goes through echo
prevention) instead of `set_listening_level(...)` (which is for
"authoritative" writes like the dial).

This is a small surgical addition to
[jasper/control/server.py:496-624](../jasper/control/server.py:496) — see
§4.7 below.

#### `state_publisher.py`

Writes `/run/jasper-usbsink/state.json` at 1 Hz, plus on every state
transition. Same atomic tempfile+rename pattern as
`jasper/mic_mute_persistence.py`. Schema:

```python
@dataclass
class UsbSinkState:
    playing: bool         # RMS-based, with hysteresis
    preempted: bool       # mux-induced silence
    host_connected: bool  # /proc/asound/UAC2Gadget present
    rms_dbfs: float
    updated_at: str       # ISO 8601 UTC
```

#### `preempt_listener.py`

stdlib `http.server` running in a worker thread on
`127.0.0.1:8781`. One route: `POST /preempt {"silenced": bool}`.
Writes to `/run/jasper-usbsink/preempt.state` (so survives daemon
restart) and toggles `audio_bridge.preempted`.

### 4.3 Volume observer integration

**Modified file**: `jasper/volume_observers.py` (already exists, hosts
the Spotify and Bluetooth inbound observers started by voice_daemon).

Add `UsbSinkObserver`:

```python
class UsbSinkObserver:
    """Polls jasper-usbsink's published state at 1 Hz and feeds
    volume changes into the coordinator. This is a thin polling
    observer because jasper-usbsink itself does the `amixer`-poll
    mixer observation and POSTs to jasper-control; the observer
    only kicks in for cases where the coordinator instance isn't
    the one inside jasper-control (e.g. voice_daemon's own
    coordinator, which receives volume changes through this path)."""

    async def run(self, coordinator: VolumeCoordinator):
        while True:
            state_path = "/run/jasper-usbsink/state.json"
            try:
                with open(state_path) as f:
                    state = json.load(f)
                if state.get("playing") and not state.get("preempted"):
                    # No-op for already-published values; coordinator
                    # has echo prevention.
                    ...
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            await asyncio.sleep(1.0)
```

Actually, on reflection, this observer may not be needed at all. The
authoritative volume routing is `volume_bridge.py` POSTing to
jasper-control's `/volume/set`. jasper-control's coordinator updates
the persistence file. voice_daemon's coordinator refreshes from disk
on every operation (see `_refresh_from_disk()` at
[jasper/volume_coordinator.py:348](../jasper/volume_coordinator.py:348)).
So volume changes propagate to voice_daemon without a dedicated
observer. **Decision: no new UsbSinkObserver.** Saves complexity.

### 4.4 Source-state probe

**Owner**: `jasper/source_state.py`.

`usbsink_playing()` reads `/run/jasper-usbsink/state.json` and returns
the bridge's RMS-gated `playing` flag. That remains the truth on solo /
aloop boxes, where the bridge owns the gadget capture.

Combo boxes are different: `JASPER_FANIN_USB_DIRECT=enabled` means
jasper-fanin DIRECT-captures the gadget and `jasper-usbsink` runs in
standby. Its `playing:false` / `rms_dbfs:-120` are frozen idle defaults.
The source-state module therefore also owns the pure helpers used by
`jasper-mux`: `usbsink_bridge_in_standby()` gates combo mode from the
bridge's `standby` flag; `usbsink_direct_frames_read()` extracts the
direct-lane liveness counter from fan-in `STATUS`, preferring
`resampler.input_frames` and falling back to lane-level `frames_read`;
and `usbsink_direct_rms_dbfs()` / `usbsink_direct_audible()` read the
direct lane's live per-period level (`rms_dbfs`, added to every fan-in
`STATUS` input lane) and compare it against the shared
`USBSINK_PLAYING_RMS_DBFS` gate (`-60.0` dBFS, pinned equal to the solo
bridge's `PLAYING_RMS_DBFS` by `tests/test_usbsink_playing_rms_contract.py`).
That level gate is what makes combo == solo: the fan-in DIRECT lane keeps
clocking silence frames when the host is connected but muted (a muted Zoom,
an idle tab), so **frames-advanced alone would seize the speaker on
silence**, where a solo box reads `playing=false`.

**Owner**: `jasper/renderer.py`. `active_renderers()` exports
`usbsinkactive` from `usbsink_playing()`:

```python
async def active_renderers(self) -> dict[str, bool]:
    spot, ap, bt, usb = await asyncio.gather(
        spotify_playing(self._librespot_state_path),
        airplay_playing(),
        bluetooth_playing(),
        usbsink_playing(),
        return_exceptions=False,
    )
    return {
        "aplactive": ap,
        "btactive": bt,
        "spotactive": spot,
        "usbsinkactive": usb,
}
```

On combo boxes this raw renderer flag can stay false during live USB
audio. Voice transport detection therefore prefers mux's
`selected_source` / `winner` before falling back to raw renderer flags.

### 4.5 Mux integration

**Modified files**: `jasper/music_sources.py`, `jasper/mux.py`.

Current source metadata lives in `jasper/music_sources.py`; USB sink is
declared there with fan-in label `usbsink`, active-renderer key
`usbsinkactive`, wizard key `usbsink`, and
`VolumeMode.CAMILLA_MASTER`. `jasper-mux` consumes that registry for its
playing-state map and source-selection status.

The USB source-specific probe is combo-aware:

```python
async def _usbsink_playing(self) -> bool:
    state = read_usbsink_state()
    if not usbsink_bridge_in_standby(state):
        return await usbsink_playing()
    fanin = await self._fanin_status_best_effort()
    frames = usbsink_direct_frames_read(fanin)
    rms_dbfs = usbsink_direct_rms_dbfs(fanin)
    self._usbsink_combo = step_combo_liveness(..., rms_dbfs=rms_dbfs)
    return self._usbsink_combo.playing
```

`step_combo_liveness()` is a pure tick-state machine. A combo box plays on
a tick iff the DIRECT-lane counter **advanced** since the previous tick AND
the lane's most-recent-period **level is above the `-60` dBFS gate**. The
first advancing-and-audible tick starts USB immediately; flat / missing
counters OR advancing-but-silent readings stop only after the debounce
window (so a beat-gap in music does not drop the winner, but a muted host
does); counter resets re-baseline without waiting for the new counter to
overtake the old one. `/source/state` exposes `sources.usbsink.playing`
from that mux view plus `usbsink.combo` so the landing page and operators
can tell which regime they are seeing.

Extend `_pause()`:

```python
elif source == Source.USBSINK:
    ok = await self._usbsink_set_preempt(True)
    if not ok:
        logger.warning("usbsink preempt POST failed; "
                       "USB audio may continue mixing briefly")
```

Add a release-preempt path. When mux observes all sources idle, clear
USB preempt:

```python
# At the end of _tick, after handling transitions:
if self._usbsink_preempted and no_other_sources_are_playing:
    ok = await self._usbsink_set_preempt(False)
    if ok:
        self._usbsink_preempted = False
        logger.info("usbsink preempt released (all sources idle)")
```

The `_usbsink_set_preempt` method POSTs to `127.0.0.1:8781/preempt`
via httpx. Add httpx as a dep if not already (it's already used in
`volume_coordinator.py:175`).

### 4.6 Wizard integration

> **Historical note.** This phase-plan sketch predates both the
> local-source lifecycle registry and the composite gadget. Current code
> treats `jasper-usbsink.service` as the `/sources/` intent unit for the
> **audio function only**; the gadget descriptor (both network and audio
> functions) is owned by `jasper-usbgadget.service`. Enabling/disabling
> USB Audio Input from `/sources/` toggles `jasper-usbsink.service` **and**
> restarts `jasper-usbgadget.service` so it recomposes with or without
> `uac2.usb0` — see [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md)
> "Toggling audio from `/sources/`". The always-on management network is
> unaffected by this toggle either way. The toggle then also **kicks
> `jasper-fanin-coupling-auto.service`** (start-only, via the restart broker)
> so the USB low-latency combo arms/disarms this session rather than only at
> the next reboot — see
> [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) "USB DIRECT (combo
> mode)".

**Modified file**: `jasper/web/sources_setup.py`.

Two systemd units exist (init + main). The intent unit is
`jasper-usbsink.service`; `jasper-usbsink-init.service` owns the
host-visible ConfigFS gadget. `Requires=` brings init up when the intent
unit starts, and `PartOf=` stops the bridge when init stops. Stopping the
bridge does **not** stop init, so disable/parking paths must explicitly
stop the init/gadget unit.

Add to constants:

```python
USBSINK_UNIT = "jasper-usbsink.service"
VALID_SOURCES = ("airplay", "bluetooth", "spotify_connect", "usbsink")
```

Add to `_gather_state()`:

```python
"usbsink": {
    "enabled": _unit_active(USBSINK_UNIT),
    "available": _usbsink_available(),  # See below
},
```

`_usbsink_available()` returns False if the dtoverlay isn't set in
config.txt (user hasn't run the install.sh that adds it, or has
manually removed it). Surfaced in the UI as a disabled toggle with
the note "USB gadget mode not enabled — re-run install.sh and reboot".

Add to `_apply()`:

```python
elif source == "usbsink":
    _set_unit(USBSINK_UNIT, enabled)
```

Add a row to the HTML rendering. New row with the source name "USB
Audio Input" and a note "Plug a computer into the Pi's USB-C port via
the 8086 splitter. Mac/Windows/Linux will see the speaker as a USB
audio output.".

The wizard's optimistic-UI JavaScript already loops over `SOURCES`;
just add `usbsink` to that array (line 247). No further JS changes
required.

### 4.7 jasper-control `/volume/set` source field

**Modified file**: `jasper/control/server.py`.

The existing `/volume/set` endpoint takes `{percent}`. Extend the
schema to accept an optional `source` field:

```json
{"percent": 50}                       // existing: authoritative set
{"percent": 50, "source": "usbsink"}  // new: observed from host
```

When `source` is provided and equals a known source, the handler
routes through `coordinator.observe_source_volume(source, percent)`
rather than `coordinator.set_listening_level(percent)`. This goes
through echo-prevention and only updates if the change isn't our own
write echoing back.

```python
# In the POST handler for /volume/set:
percent = int(body["percent"])
source = body.get("source")
if source == "usbsink":
    await coordinator.observe_source_volume(Source.USBSINK, percent)
else:
    await coordinator.set_listening_level(percent)
```

This is ~10 lines of change in
[jasper/control/server.py](../jasper/control/server.py).

### 4.8 jasper-doctor checks

**Modified file**: `jasper/cli/doctor.py`. Add three checks, all
under the `usbsink` namespace (analogue of the AEC checks):

```python
def check_usbsink_dtoverlay() -> CheckResult:
    """Verify dtoverlay=dwc2,dr_mode=peripheral is present in
    config.txt. Without it, USB-C is power-only and the gadget cannot
    enumerate."""
    cfg = Path("/boot/firmware/config.txt")
    if not cfg.exists():
        return CheckResult("usbsink dtoverlay", "warn",
                           "config.txt missing — not a Pi?")
    content = cfg.read_text()
    if "dtoverlay=dwc2,dr_mode=peripheral" in content:
        return CheckResult("usbsink dtoverlay", "ok",
                           "dwc2 peripheral mode enabled")
    return CheckResult("usbsink dtoverlay", "warn",
                       "not set; USB gadget source unavailable "
                       "(set via install.sh, requires reboot)")


def check_usbsink_state() -> CheckResult:
    """When jasper-usbsink is enabled, verify it's healthy. When
    disabled, verify libcomposite is NOT loaded (drift detection)."""
    active = _systemd_is_active("jasper-usbsink.service")
    libcomp_loaded = _is_module_loaded("libcomposite")
    if active:
        # Check that the daemon is publishing state recently.
        state_path = Path("/run/jasper-usbsink/state.json")
        if not state_path.exists():
            return CheckResult("usbsink state", "fail",
                               "service active but no state file")
        state = json.loads(state_path.read_text())
        updated = datetime.fromisoformat(state["updated_at"])
        if (datetime.now(timezone.utc) - updated).total_seconds() > 10:
            return CheckResult("usbsink state", "warn",
                               f"state file is "
                               f"{(datetime.now(...) - updated).total_seconds():.0f}s "
                               "stale — daemon may be wedged")
        return CheckResult("usbsink state", "ok",
                           f"playing={state['playing']} "
                           f"host_connected={state['host_connected']}")
    else:
        if libcomp_loaded:
            return CheckResult("usbsink state", "warn",
                               "service disabled but libcomposite still loaded — "
                               "RAM drift; reboot or manually rmmod")
        return CheckResult("usbsink state", "ok", "disabled (no RAM)")


def check_usbsink_card() -> CheckResult:
    """When usbsink is enabled, verify the UAC2Gadget ALSA card exists."""
    if not _systemd_is_active("jasper-usbsink.service"):
        return CheckResult("usbsink card", "ok", "service disabled (skip)")
    if not Path("/proc/asound/UAC2Gadget").exists():
        return CheckResult("usbsink card", "fail",
                           "service active but no UAC2Gadget card — "
                           "init service may have failed")
    return CheckResult("usbsink card", "ok", "UAC2Gadget present")
```

Hooked into the main check list in `doctor.py`'s `_all_checks()`.

### 4.9 jasper-control `/state` aggregator

**Owner (as shipped)**: `jasper/control/state_aggregate.py`
(`_build_usbsink_renderer_state`) — *not* `server.py`.

`/state.renderers.usbsink` projects the bridge's `state.json`:

```json
{
  "usbsink": {
    "combo": false,
    "playing": false,
    "preempted": false,
    "host_connected": true,
    "rms_dbfs": -85.4,
    "updated_at": "2026-05-16T00:00:00+00:00"
  }
}
```

The section is `null` (not the object) when the feature is off (no
`state.json`), so consumers distinguish "off" from "on but idle". Fail-soft per
the existing pattern.

On a **combo box** (`JASPER_FANIN_USB_DIRECT` — fan-in DIRECT-captures the
gadget, bridge in standby) *the bridge* measures nothing, but fan-in's USB
DIRECT lane measures the live capture level per period, so the projection sets
`combo: true` with `playing` / `rms_dbfs` derived from that lane
(`usbsink_direct_audible()` / `usbsink_direct_rms_dbfs()`), **not** the standby
bridge's stale idle defaults: audible content → `playing:true` with the real
level; a muted host → `playing:false`. This is a single-snapshot *level* read
(no temporal frames-advanced hysteresis — that lives in mux); it matches the
solo box's `/state`, which reads the bridge's per-period `playing` flag with no
hysteresis either. Both fall back to `null` only when fan-in gives no level (an
older build, or the STATUS is unreachable and combo is detected via the bridge
`standby` fallback). USB *selection* on a combo box is still read from
`/state.active_source` / `source_selection` (mux), and the raw capture from
`/state.audio.fanin.usbsink_input`. Combo detection reuses
`jasper.fanin.status.fanin_usbsink_lane_is_direct` (the `source=="direct"`
signal — the one owner of that contract, shared with the route-latency harness /
mux), with the bridge's `standby` flag as an equivalent-by-design fallback.

Note: the landing-page Source selector reads `/source/state` (mux), not this
section; no `/system/` card renders it today.

### 4.10 install.sh additions

**Modified file**: `deploy/lib/install/renderers.sh`.

One section adding the dtoverlay (idempotent, sandboxed under a
function like `set_usb_gadget_mode()`):

```bash
set_usb_gadget_mode() {
  local cfg="${JTS_BOOT_CONFIG_FILE:-/boot/firmware/config.txt}"
  if grep -qE '^dtoverlay=dwc2,dr_mode=peripheral' "$cfg"; then
    return 0
  fi
  # Append under [all] so streambox installs can validate Zero-class
  # powered-splitter USB Audio Input alongside the DAC.
  printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$cfg"
  echo "USB gadget dtoverlay added to $cfg (reboot required to apply)."
}
```

One section installing the gadget scripts:

```bash
install -d -m 0755 /opt/jasper/deploy/usbsink
install -m 0755 \
  "${REPO_DIR}/deploy/usbsink/uac2-gadget-up.sh" \
  "${REPO_DIR}/deploy/usbsink/uac2-gadget-down.sh" \
  "${REPO_DIR}/deploy/usbsink/wait-for-uac2-card.sh" \
  /opt/jasper/deploy/usbsink/
```

One section installing the systemd units (alongside existing
`jasper-aec-bridge.service` etc.):

```bash
install -m 0644 \
  "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
  "${REPO_DIR}/deploy/systemd/jasper-usbsink-init.service" \
  /etc/systemd/system/

# Do NOT auto-enable. The /sources/ wizard owns the toggle. The user
# has to opt in.
systemctl daemon-reload
```

A `REBOOT_REQUIRED` flag at the end of install.sh: if the dtoverlay
was newly added, print a clear warning that a reboot is needed before
the toggle in `/sources/` will work.

### 4.11 Web wizard JS — disabled-toggle reason text

The `available: false` branch already exists for Bluetooth ("not
available on this device"). For USB, the reason is "needs dtoverlay +
reboot". UI surfaces the specific reason in the row's note text.

## 5. File map summary

### New files

| Path | Purpose | LoC est. |
|---|---|---|
| `deploy/usbsink/uac2-gadget-up.sh` | ConfigFS gadget creation | ~80 |
| `deploy/usbsink/uac2-gadget-down.sh` | Teardown | ~30 |
| `deploy/usbsink/wait-for-uac2-card.sh` | Enumeration race wait | ~20 |
| `deploy/systemd/jasper-usbsink.service` | Main daemon unit | ~30 |
| `deploy/systemd/jasper-usbsink-init.service` | Init unit | ~25 |
| `jasper/usbsink/__init__.py` | Package marker | ~5 |
| `jasper/usbsink/daemon.py` | Orchestration | ~120 |
| `jasper/usbsink/audio_bridge.py` | sounddevice loop | ~80 |
| `jasper/usbsink/volume_bridge.py` | amixer-cget mixer poller | ~100 |
| `jasper/usbsink/state_publisher.py` | /run/jasper-usbsink/state.json | ~60 |
| `jasper/usbsink/preempt_listener.py` | localhost HTTP receiver | ~50 |
| `jasper/cli/usbsink_main.py` | Entry point | ~30 |
| `tests/test_usbsink_volume.py` | Vol math, state parsing | ~80 |
| `tests/test_usbsink_state.py` | State publisher | ~60 |
| `docs/HANDOFF-usbsink.md` | This document | (this file) |

### Modified files

| Path | Change |
|---|---|
| `deploy/install.sh` | Add dtoverlay, install scripts + units (no auto-enable) |
| `jasper/web/sources_setup.py` | Add USB toggle |
| `jasper/source_state.py` | Add `usbsink_playing()` |
| `jasper/renderer.py` | Add `usbsinkactive` to `active_renderers()` |
| `jasper/music_sources.py` | Add `Source.USBSINK` metadata with `VolumeMode.CAMILLA_MASTER` |
| `jasper/mux.py` | Add USB preempt POST + release-on-idle behavior |
| `jasper/volume_coordinator.py` | USB sink uses the shared `music_sources` volume mode; no source-local push writeback |
| `jasper/control/server.py` | `/volume/set` accepts optional `source` field |
| `jasper/control/system_metrics.py` | `/state` exposes `usbsink` section |
| `jasper/cli/doctor.py` | Three new checks |
| `pyproject.toml` | Register `jasper-usbsink` script (no new mixer dep — uses `amixer` from alsa-utils) |
| `BRINGUP.md` | New phase: 8086 splitter setup + reboot for dtoverlay |
| `README.md` | Sources list gains USB; RAM table gains usbsink row |
| `CLAUDE.md` / `AGENTS.md` | Operational section: how to debug, on/off, doctor checks |
| `PLAN.md` | v8 USB gadget moves from "Blocked" to "Shipped" once landed |

### Total new code

~640 LoC of Python, ~130 LoC of shell, ~55 LoC of systemd units. About
the same size as the AEC bridge subsystem.

## 6. Resilience design

### Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| Host unplugs USB cable | Capture callbacks may idle, PortAudio may report status flags, or the stream may fail depending on host/kernel behavior. | Capture-only idle publishes `playing=false` and logs `event=usbsink.capture_idle`. PortAudio status flags are surfaced in the journal. If the stream failure takes down the daemon, systemd restarts it via `Restart=on-failure`. |
| Host enters suspend (Mac sleeps) | Capture endpoint goes silent (no errors, just zeros); RMS drops below threshold | Daemon publishes `playing=false`. Mux releases USB winner. No special handling needed. |
| Host suspends and re-enumerates on wake | sounddevice stream may error, emit PortAudio status flags, or temporarily stop capture callbacks. | Errors/status flags are surfaced in the journal. Capture-only idle publishes `playing=false` and logs `event=usbsink.capture_idle`; it does not watchdog-restart because host silence is valid. If the output callback also stops, the systemd watchdog restarts the daemon. |
| Pi reboots mid-session | systemd starts `jasper-usbgadget.service` then `jasper-usbsink.service`; the composite ConfigFS gadget (network + audio) comes back up. Host re-detects JTS. | Standard boot path. The gadget owner handles idempotency (existing descriptor → skip create). |
| libcomposite fails to load (kernel module corrupted) | `modprobe libcomposite` returns non-zero in `jasper-usbgadget.service`'s ExecStartPre | The gadget unit fails; jasper-usbsink doesn't start. Doctor catches it on the next run. Sources wizard shows toggle as "available: false" because card isn't present. |
| ConfigFS write fails (e.g. UDC already bound to a different gadget) | `jasper-usbgadget-up` returns non-zero | The gadget unit fails; same as above. Operator restarts `jasper-usbgadget.service` or reboots. |
| Audio bridge daemon crashes | systemd `Restart=on-failure` | Restarts within 2 s. Audio gap is ~2 s; state file might have stale `playing=true` for a tick. Mux re-evaluates next tick. |
| Audio bridge output wedges (playback callback stops firing) | `WatchdogSec=15s` + `Type=notify` heartbeat from the daemon's main loop, bumped only by playback/output callback progress | systemd kills + restarts the daemon. Same as crash. |
| Mixer observer thread dies | Daemon's main loop checks observer task with `task.done()` every 5 s | Logs warning, restarts the observer task. Volume bridge degraded but audio continues. |
| Mux POST to preempt endpoint fails | httpx exception in `Mux._pause(USBSINK)` | Logs warning; USB audio continues mixing briefly. Documented limitation (matches Bluetooth's behavior, but rarer because the local HTTP path is more reliable than DBus). |
| Two USB hosts plugged in simultaneously (impossible by UAC2 spec) | Splitter physically prevents this | Hardware-enforced; nothing to do |
| Sample rate negotiation failure (host requests 44.1k, gadget descriptor only offers 48k) | sounddevice opens at the descriptor's rate; host resamples its own output | No issue — host always resamples to the device's reported rate. Documented in BRINGUP.md so users know JTS doesn't do 192k. |

### Watchdog pattern

The daemon uses `Type=notify` with `WatchdogSec=15s`. The main async
loop uses `jasper.watchdog.Heartbeat`, but it bumps the progress
sentinel only when the playback/output callback has advanced. That
callback should keep running while USB input is idle because it writes
silence into the private fan-in lane. Capture callback progress is
source-activity evidence, not daemon-health evidence; an idle host must
not look like a crash.

If the playback/output callback wedges, heartbeat pats stop, the
watchdog fires, and systemd restarts.

Mirrors the pattern in
[jasper/watchdog.py](../jasper/watchdog.py) — same helper, no new
code needed.

### Log conventions

All log lines use the JTS `event=` shorthand for machine-readable
events:

```
event=usbsink.started host_connected=true
event=usbsink.host_connected serial=ABC123 vid=1d6b pid=0104
event=usbsink.host_disconnected duration_sec=312
event=usbsink.playing_started rms_dbfs=-15.2
event=usbsink.playing_stopped rms_dbfs=-88.1 inactive_sec=2.0
event=usbsink.preempted by=airplay
event=usbsink.preempt_released reason=all_idle
event=usbsink.volume_change pct=42 raw=-3200 source=host_slider
```

These appear in `scripts/jasper-trace.sh` output alongside the
existing cross-daemon events. No changes to the trace script needed —
it's a substring filter on `event=`.

## 7. Phased delivery

> **Historical section.** This was the implementation plan used to ship
> USB Audio Input. It is retained for archaeology; do not use it as the
> current add-a-source checklist. Use
> [`audio-paths.md`](audio-paths.md#adding-a-new-music-source) instead.

Eight phases, each independently mergeable (each ends with the repo
in a working state). Estimated 16-22 hours of focused work.

### Phase 1 — Boot config + gadget script (~2 h)

**Goal**: dtoverlay in place, libcomposite loads cleanly, gadget
descriptor creates and registers an ALSA card.

**Deliverables**:
- `deploy/usbsink/uac2-gadget-up.sh` + `down.sh` + `wait-for-uac2-card.sh`
- `deploy/install.sh` gains `set_usb_gadget_mode()` (additive only)
- `deploy/systemd/jasper-usbsink-init.service`
- Manual test on the Pi: deploy, reboot, `systemctl start
  jasper-usbsink-init`, verify `/proc/asound/UAC2Gadget` exists
- Plug Mac in: Mac should see "<speaker name> USB Audio" in Audio Devices

**Acceptance**: Mac System Settings → Sound shows the speaker as an
output device. Setting it as output and playing audio: Mac says it's
streaming, JTS speakers play nothing (no daemon yet). RAM cost
verified: `systemctl stop jasper-usbsink-init` brings it back to
baseline minus the ~50 KB dwc2 module.

### Phase 2 — Audio bridge daemon (~3 h)

**Goal**: Audio flows from gadget → Loopback → speakers.

**Deliverables**:
- `jasper/usbsink/audio_bridge.py`
- `jasper/usbsink/daemon.py` (skeleton — audio only)
- `jasper/cli/usbsink_main.py`
- `deploy/systemd/jasper-usbsink.service`
- `pyproject.toml` script registration

**Acceptance**: From Mac, set JTS as output, play music. JTS speakers
play it. CamillaDSP `main_volume` attenuates it (verify via
`watch -n 0.5 'cdspctl get volume'` while spinning the dial).

### Phase 3 — Source-state probe + mux wiring (~3 h)

**Goal**: USB shows up in mux's arbitration. Latest-source-wins
between USB and existing sources.

**Deliverables**:
- `jasper/usbsink/state_publisher.py`
- `jasper/source_state.py` gains `usbsink_playing()`
- `jasper/renderer.py` gains `usbsinkactive`
- `jasper/music_sources.py` gains `Source.USBSINK`; `jasper/mux.py`
  gains the USB preempt POST (still no preempt listener on the daemon
  side — POST fails harmlessly)
- `jasper/usbsink/preempt_listener.py` (receives the POST, sets
  internal flag)
- Mux's "release on all-idle" logic

**Acceptance**:
- Mac plays → USB wins (other sources paused)
- Start AirPlay → mux POSTs preempt → USB daemon silences output
- Stop AirPlay → mux releases preempt → USB audio resumes (if Mac
  still playing)
- All three transitions logged in `jasper-trace.sh`

### Phase 4 — Volume bridge (~2 h)

**Goal**: Mac slider drives JTS volume (feels like dial twist).

**Deliverables**:
- `jasper/usbsink/volume_bridge.py`
- `jasper/control/server.py` `/volume/set` accepts `source` field
- `jasper/music_sources.py` declares `Source.USBSINK` as
  `VolumeMode.CAMILLA_MASTER`

**Acceptance**:
- Move Mac slider → JTS volume changes within ~100 ms
- Spin dial up → JTS volume changes; Mac slider stays put (one-way
  is acceptable)
- Mac mute toggle → JTS goes silent; unmute restores

### Phase 5 — Wizard toggle (~1.5 h)

**Goal**: `/sources/` has a fourth toggle. On/off cycles cleanly.

**Deliverables**:
- `jasper/web/sources_setup.py` modifications
- Wizard JS unchanged structurally (just `SOURCES.push('usbsink')`)
- New row in the HTML with a note

**Acceptance**:
- Toggle off → daemon stops, init stops, ALSA card disappears,
  libcomposite unloads (verify with `lsmod | grep libcomposite`)
- Toggle on → init runs, descriptor created, daemon starts, ALSA card
  appears, host re-detects the speaker as an audio device
- Off→on cycle <3 s end-to-end

### Phase 6 — Doctor checks (~1 h)

**Goal**: `jasper-doctor` reports usbsink state accurately on/off.

**Deliverables**:
- `jasper/cli/doctor.py` three new checks
- All three return ok/warn/fail with useful detail strings

**Acceptance**:
- With dtoverlay set + usbsink disabled: all three checks ok
- With dtoverlay missing: usbsink_dtoverlay warns clearly
- With usbsink enabled + state file stale: usbsink_state warns
- With usbsink disabled but libcomposite loaded: usbsink_state warns
  about RAM drift

### Phase 7 — Resilience + observability (~3 h)

**Goal**: Daemon survives host unplug/replug, suspend/resume; logging
sufficient for incident debugging.

**Deliverables**:
- sounddevice exception handling + stream reopen loop
- Watchdog notify integration via the existing helper
- `event=usbsink.*` log lines at all key transitions
- `/state` endpoint exposure

**Acceptance** (manual stress test):
- Unplug Mac while music playing → daemon logs disconnect, stays
  running, state.json updates
- Replug → daemon reopens, music resumes within ~2 s
- Mac goes to sleep → daemon publishes idle → mux releases USB
- Mac wakes → daemon resumes
- Pi reboots → all comes back automatically

### Phase 8 — Docs + final polish (~2 h)

**Deliverables**:
- `BRINGUP.md` Phase: 8086 splitter setup + reboot
- `README.md` updates (sources list, RAM table)
- `CLAUDE.md` / `AGENTS.md` operational section
- `PLAN.md` v8 status flip
- This HANDOFF doc — final review pass after implementation surfaces
  any design changes
- Hardware-free test suite

## 8. Testing strategy

### Hardware-free tests (`.venv/bin/pytest`)

`tests/test_usbsink_volume.py`:
- `gadget_raw_to_pct()` math: 0 dB → 100%, min → 0%, mid → 50%
- listening-level conversion symmetry
- amixer stub: feed synthetic `amixer cget` output, verify POST payload

`tests/test_usbsink_state.py`:
- State file write atomicity (tempfile+rename)
- RMS hysteresis: feed synthetic frames, verify playing-state
  transitions debounce correctly
- State file parse on read (corrupted JSON → False, missing file →
  False)

`tests/test_source_state.py` (modify existing):
- `usbsink_playing()` returns False when state file missing
- Returns True when file says `playing: true`
- Returns False when file says `playing: false`; `preempted` remains a
  separate mux-silence field and does not change `usbsink_playing()`

`tests/test_mux.py` (modify if exists, or new):
- USB transitions inactive→active triggers preempt of currently-playing
  AirPlay
- All-idle tick clears USB preempt flag

### Pi-side smoke tests

Codified in `jasper-doctor` (§4.8). Plus a one-shot manual checklist
in `BRINGUP.md`:

1. After install + reboot, `lsmod | grep libcomposite` shows nothing
2. `systemctl status jasper-usbsink-init` is `inactive (dead)`
3. Toggle USB on in `/sources/` → both units active within ~3 s
4. Plug Mac in → Mac sees the speaker as an audio output
5. Play music from Mac → audible from JTS speakers
6. Adjust Mac volume → JTS volume follows (verify via dashboard or
   `curl :8780/state | jq .voice.listening_level`)
7. Spin dial up → JTS volume changes
8. Start AirPlay from phone → JTS stops playing Mac audio, starts
   playing phone audio
9. Stop AirPlay → JTS resumes Mac audio (if Mac still playing)
10. Wake voice ("Jarvis, what's the weather?") with Mac playing → music
    ducks, voice plays, music returns

### AEC test

USB audio enters `usbsink_substream`; `jasper-fanin` sums it into
substream 7, and `pcm.jasper_capture` exposes that summed music stream
as the AEC reference. So AEC sees USB audio in the reference signal
automatically. Verify:

1. Mac plays music loud (75 dB at speaker)
2. Wake word triggered → AEC kicks in
3. Voice session completes → cancellation effective (no obvious
   feedback or self-trigger)

If AEC degrades because USB-source music has different spectral
characteristics than AirPlay/Spotify music… that's an AEC tuning
issue independent of usbsink, and out of scope here.

## 9. Open questions

These need explicit calls before or during implementation. None are
blockers; defaults are documented for each.

1. **Sample rate**: Lock the gadget to 48 kHz S32_LE stereo
   (PiCorrect's choice, matches our snd-aloop and DAC). Multi-rate
   gadget descriptors are possible but add complexity. **Default:
   single rate 48k.**
2. **macOS "Playback Inactive" cosmetic bug**: ~~PiCorrect's
   DEBUGGING.md documents that macOS labels the device as "Playback
   Inactive" due to hardcoded strings in
   `drivers/usb/gadget/function/f_uac2.c`.~~ **RESOLVED** — the host
   label now tracks the Speaker Name via a name-patched module override.
   See §4.1a "Host-visible device name". (Confirmed end-to-end on macOS
   2026-06-04: a connected Mac shows "JTS".)
3. **Volume curve**: gadget mixer range is symmetric in dB; CamillaDSP
   `main_volume` is also dB-linear. A direct linear-in-dB mapping
   feels natural. **Default: linear in dB, 100% gadget = 0 dB camilla,
   0% gadget = camilla min (~−96 dB).**
4. **Preempt-release window**: how long after all other sources go
   idle before USB un-mutes? Instant feels right (matches mux's tick
   cadence). **Default: instant on next mux tick (1 s max delay).**
5. **State file ownership**: `/run/jasper-usbsink/` created via
   `RuntimeDirectory=jasper-usbsink` in the systemd unit. Owned by
   the service user. Other daemons (jasper-voice, jasper-control,
   jasper-mux) read it. **Default: world-readable (0644 file mode).**
6. **VID/PID/serial**: PiCorrect uses VID 0x1d6b PID 0x0104 (Linux
   Foundation Multifunction Composite Gadget). We could use the
   same, or claim a JTS-specific PID. **Default: same as PiCorrect.
   Two devices on one Mac with same VID/PID but different serial is
   fine — host disambiguates by serial.**
7. **Should the daemon ever exit when host disconnects?** No — the
   service stays running so it's ready to bridge the moment the host
   plugs back in. The state file just reflects `host_connected:
   false`. **Default: stay running, idle.**

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Pi linux issue #6569 (related kernel quirk) still alive on Trixie | Medium | Medium — could prevent gadget from enumerating | Historical pre-ship risk. Current shipped path uses the 8086 splitter-backed topology; re-check this only if changing USB-C cabling or removing the splitter assumption. |
| Python ALSA binding (pyalsa) needs a Trixie source build | Avoided | — | volume_bridge polls `amixer cget` (alsa-utils, already required) instead of a Python ALSA binding, so this never applied. |
| sounddevice/PortAudio doesn't handle gadget endpoint cleanly (e.g. XRUNs on host hot-plug) | Medium | Medium | Stream reopen-on-error pattern documented in §6. If chronic, fall back to `alsaloop` subprocess and lose the per-frame RMS (would need separate RMS via `arecord | tee`). |
| Host changes sample rate mid-session and gadget descriptor doesn't permit | High on Mac (auto-rate-switching) | Low — host resamples its own output to match the gadget's advertised rate | Document in BRINGUP.md. JTS doesn't aspire to bit-perfect. |
| RAM drift on disable (libcomposite stays loaded after stop) | Medium | Low — 60 KB | Doctor warns. Manual `rmmod` or reboot recovers. |
| Volume bridge race: Mac slider moves rapidly, POSTs pile up | Low | Low | Debounce inside `volume_bridge.py` — coalesce events within 50 ms windows. |
| Mac mute path doesn't propagate as expected (some hosts mute via gain=−inf rather than Switch) | Medium | Low — JTS still respects gain | Volume bridge reads both `Volume` and `Switch`; mute is `max(volume_mute, switch_mute)`. |
| dtoverlay conflicts with future audio-related dtoverlays | Low | Low | Tested as additive; the Pi 5 specifically supports concurrent dtoverlays. |
| Splitter cable fails / wears out | Long-term | Medium — USB stops working | Documented as a hardware item the user owns; spare cables are cheap. |

## 11. References

### Internal (JTS)

- [README.md](../README.md) — architecture overview, music chain
- [CLAUDE.md](../CLAUDE.md) — file ownership, deploy pattern
- [PLAN.md](../PLAN.md) — v8 USB gadget previously deferred (line 71)
- [jasper/volume_coordinator.py](../jasper/volume_coordinator.py) —
  Source-aware volume model; USBSINK joins as camilla-as-master
- [jasper/mux.py](../jasper/mux.py) — Source arbitration; USBSINK
  added with HTTP-POST preempt
- [jasper/source_state.py](../jasper/source_state.py) — Playing-state
  probes; `usbsink_playing()` added here
- [jasper/web/sources_setup.py](../jasper/web/sources_setup.py) —
  Toggle wizard; pattern mirrored for USB
- [jasper/cli/doctor/](../jasper/cli/doctor/__init__.py) — Check pattern;
  three new checks added
- [deploy/install.sh](../deploy/install.sh) — dtoverlay handler
  pattern (cf. `country_code` at line 359)
- [docs/HANDOFF-aec.md](HANDOFF-aec.md) — AEC bridge as a similar
  RAM-budgeted optional subsystem
- [docs/HANDOFF-volume.md](HANDOFF-volume.md) — Volume coordinator
  deep-dive; USBSINK behaves like AIRPLAY for volume
- [docs/HANDOFF-resilience.md](HANDOFF-resilience.md) —
  Watchdog/notify patterns; usbsink uses Tier-1 + Tier-2

### External

- [PiCorrect repo](https://github.com/jaspercurry/PiCorrect) — proves
  the stack on Pi 5; reuse:
  - `setup.sh:68-123` — uac2-gadget ConfigFS script template
  - `setup.sh:180-219` — CamillaDSP wiring reference (we deviate
    from this since we route via snd-aloop)
  - `DEBUGGING.md` — "Playback Inactive" cosmetic note, kernel
    issue history
- [raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289) —
  Pi 5 USB-C-to-USB-C enumeration quirk; sidestepped by the 8086
  splitter (host sees USB-A end of the cable)
- [raspberrypi/linux#6569](https://github.com/raspberrypi/linux/issues/6569) —
  Open question §11; verify still relevant on Trixie before Phase 1
- [Linux UAC2 gadget docs](https://www.kernel.org/doc/Documentation/usb/gadget-testing.txt) —
  ConfigFS attributes for `uac2.usb0`
- [8086 Consultancy USB-C/PWR Splitter](https://www.8086.net/products/usb-c-pwr-splitter) —
  Hardware datasheet
- [sounddevice](https://python-sounddevice.readthedocs.io/) — PortAudio
  binding; same lib used by jasper-aec-bridge

## Appendix A — Worked example: end-to-end signal trace

User plugs Mac into the 8086 splitter, opens Music.app, plays a song.
What happens:

1. **Mac side**: macOS Audio MIDI Setup negotiates with JTS. UAC2
   handshake: descriptor advertises 48k S32_LE stereo. Mac sets JTS
   as its output device (manually, first time only; auto thereafter).

2. **Pi USB stack**: dwc2 → libcomposite → u_audio. The gadget's
   `OUT` endpoint receives the host's PCM.

3. **ALSA**: `hw:CARD=UAC2Gadget,DEV=0` capture endpoint exposes the
   audio to userspace.

4. **jasper-usbsink daemon**: sounddevice Stream callback fires every
   10 ms. Per callback:
   - Reads ~480 frames (10 ms @ 48k stereo) from the gadget
   - Computes RMS, updates `last_rms_dbfs`
   - If `preempted` is False: writes the same frames into
    `usbsink_substream`
   - If `preempted` is True: writes zeros

5. **State publisher** (1 Hz tick or on RMS-state transition):
   - Reads `last_rms_dbfs` from audio_bridge
   - Applies hysteresis: if RMS > -50 dB for ≥1 s, set playing=true;
     if < -50 dB for ≥2 s, set playing=false
   - Atomic-writes `/run/jasper-usbsink/state.json`

6. **jasper-mux**:
   - 1-Hz tick: probes all source-state files. Sees
     `usbsinkactive=true`.
   - Detects transition from previous tick (usbsinkactive was false).
   - `newly_started = [USBSINK]`. `new_winner = USBSINK`.
   - For each other source currently active: `_pause(source)`. If
     AirPlay was playing, MPRIS Pause. If Spotify, Web API. If BT,
     log no-op.
   - Now USB is the lone winner.

7. **Volume bridge** (concurrent, 4 Hz poll):
   - User opens Mac volume slider. Mac writes UAC2 Volume Control
     Unit value.
   - Linux's `u_audio` driver updates the ALSA mixer value on
     `PCM Capture Volume`.
   - On its next 4 Hz tick, volume_bridge.py reads the new value via
     `amixer cget`, converts to listening-level percent.
   - POSTs `{"percent": 65, "source": "usbsink"}` to
     `http://127.0.0.1:8780/volume/set`.
   - jasper-control routes through
     `coordinator.observe_source_volume(Source.USBSINK, 65)`.
   - Coordinator's echo-prevention check: not our own write. Echo
     check via persistence: not a recent cross-process write.
   - Updates `_level=65`, persists, calls `_set_camilla(65)` which
     writes `main_volume = -10.5 dB` via the pycamilladsp websocket.

8. **CamillaDSP**:
   - Reads frames from `plug:jasper_capture` (which dsnoops fan-in's
     summed output on `hw:Loopback,1,7`). These include the frames
     usbsink wrote into `usbsink_substream` when USB-in is active.
   - Applies main_volume attenuation. Passes through the (currently
     identity) master_gain mixer.
   - Writes to `pcm.jasper_out` (dmix on the Apple dongle).

9. **Dongle**: USB Audio → analog out → speaker amp → speakers.

10. **User hears the music at 65% volume**.

11. **AEC bridge** (concurrent, running in parallel):
    - Reads from `pcm.jasper_capture` (the same dsnoop as
      CamillaDSP). Sees the same audio CamillaDSP is processing
      *before* it hits the speaker.
    - Reads from the XVF3800 ASR beam on channel 1 (the chip side).
    - Computes echo cancellation. The music in the reference IS the
      music CamillaDSP is about to play, so the reference perfectly
      tracks what comes back through the air to the mic. AEC works
      identically whether the source was USB, AirPlay, or Spotify.

12. **User says "Hey Jarvis"**:
    - Wake word detected on the AEC'd mic signal.
    - voice_daemon's Ducker calls `set_volume_db(current - 12)` —
      music ducks.
    - VolumeCoordinator's `note_voice_session(True)` is called;
      coordinator suppresses its own camilla writes until session
      ends.
    - LLM responds via TTS. Music remains ducked.
    - Session ends. Ducker calls `coordinator.get_camilla_target_db()`
      to get the correct restore value (which respects any volume
      changes during the session — including ones that came in via
      Mac slider).
    - Ducker writes the absolute restore value. Music returns to
      pre-duck level.

Total moving parts touched by a single USB-sink user action: 7
daemons, 4 file system locations, 1 ConfigFS tree, 1 dmix mixer,
and CamillaDSP's websocket. All of which already exist; this feature
adds one daemon (jasper-usbsink) and a few hundred lines of
integration glue.

## Appendix B — Why this design over alternatives

### Alternative A: Replace snd-aloop with the gadget as the primary capture (PiCorrect-style)

Pros: simpler — no bridge daemon, CamillaDSP captures directly from
UAC2Gadget.

Cons: breaks AirPlay, Spotify, and Bluetooth. They all need to write
into the music chain through private fan-in lanes. Routing them through
the gadget capture endpoint is not possible (UAC2 endpoint is
host-driven).

Rejected: makes JTS single-source.

### Alternative B: Use `alsaloop` (alsa-utils C binary) instead of a Python daemon

Pros: ~3 MB RAM vs. ~18 MB.

Cons: alsaloop is rigid — no mixer subscription, no state publishing,
no preempt control. Need a second daemon for those, splitting the
USB-sink concerns across two units. Cohesion loss > 15 MB savings
given total Pss budget headroom (~280 MB on 1 GB Pi).

Rejected: complexity beats RAM at this margin.

### Alternative C: Rust binary instead of Python

Pros: ~3 MB RAM, fast.

Cons: Adds a second language to the codebase (no Rust elsewhere). The
firmware/ subdirectory is C++ via PlatformIO, but that's a separate
target. Build pipeline complexity, less DRY. The RAM win
(~15 MB) is marginal in the overall JTS RAM picture.

Rejected: Python keeps the stack uniform.

### Alternative D: Bidirectional volume (Mac slider ↔ dial in sync)

Pros: Mac UI always shows JTS's true volume.

Cons: Bidirectional sync requires echo prevention on both ends. The
dial writes camilla; observer reads camilla; observer would have to
ignore its own observer-induced writes. Double-bookkeeping for
minimal UX gain. Today's Mac shows the slider where the user last
left it, which is the OS-native behavior.

Rejected: complexity > UX value.

### Alternative E: Use CamillaDSP's `link_volume_control` (PiCorrect's trick)

Pros: simpler — no Python observer for volume.

Cons: `main_volume` is also the ducking knob. Voice session would
duck the music AND move the Mac slider visually downward, which is
wrong. Also can't be combined with dial-driven volume (the link is
one-to-one).

Rejected: violates ducker semantics.

---

**End of historical implementation plan.** Current operational truth
lives at the top of this file; the canonical "add another music source"
checklist lives in `docs/audio-paths.md#adding-a-new-music-source`.

Last verified: 2026-07-10 (§4.4/§4.5/§4.9 updated for the combo silence gate:
fan-in now serialises a per-lane `rms_dbfs` in every `STATUS` input; combo mux
liveness is frames-advanced **AND** level above the shared `-60` dBFS
`USBSINK_PLAYING_RMS_DBFS` gate — a muted host no longer seizes the speaker; the
`/state.renderers.usbsink` combo projection reports TRUE `playing`/`rms_dbfs`
from that fan-in level instead of `null`. Prior recheck 2026-07-07: §4.4/§4.5
corrected for combo-aware mux liveness:
solo boxes use bridge RMS `playing`; combo boxes use fan-in direct-lane
`resampler.input_frames` with `frames_read` fallback. Prior recheck 2026-07-06:
§4.9 corrected to the shipped `/state.renderers.usbsink`
shape — owner is `jasper/control/state_aggregate.py`, not `server.py`; documented
the combo-mode projection: `combo:true` with nulled `playing`/`rms_dbfs` when
fan-in DIRECT-captures the gadget. Prior recheck 2026-07-04: §1 boot-config
paragraph corrected: libcomposite +
the ConfigFS descriptor are owned by `jasper-usbgadget.service` and composed by
default at boot for the always-on USB network, not gated behind the retired
`jasper-usbsink-init.service` — HANDOFF-usb-gadget.md is canonical for gadget
ownership. Prior recheck 2026-07-02 against
`deploy/systemd/jasper-usbsink.service`, `rust/jasper-usbsink-audio`,
`jasper.audio_runtime_plan`, and jts.local hardware tuning: Rust bridge 256/3,
fan-in USB resampler held target 2048, fan-in output 1024, CamillaDSP 256/1536,
outputd 128/256, outputd content buffer 1536, direct ALSA loopback. Route-latency
evidence remains missing, so doctor correctly fails the low-latency claim. The
old Python/PortAudio bridge and lean-FIFO path were deleted (USB dead-pipeline
sweep); the Rust bridge is the sole data plane. The state.json field list above now
includes `tap` and `host_clock`, both pointed at
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) as their single
source of truth per the documentation paradigm.)
