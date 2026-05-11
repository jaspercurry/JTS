# HANDOFF — AirPlay 2 audio glitches with CamillaDSP in the chain

**Status as of 2026-05-11:** Source of truth for an open audio-chain problem.
Two PRs shipped that materially improved it but did not eliminate it. Final
solution still TBD; possible paths laid out below.

This document is the canonical reference for the synchronization glitch on
JTS's AirPlay receiver. It captures the problem, the architecture context,
what we've measured, what we've tried, the first-principles mechanism we've
confirmed from source code, and the remaining options ranked by cost. If
you're touching this subsystem, read this before changing anything.

---

## TL;DR

shairport-sync's drift-correction algorithm assumes its ALSA output handle
is the actual DAC. In JTS's chain (shairport → snd-aloop → CamillaDSP →
dmix → USB DAC), the output handle is a snd-aloop ring, so
`snd_pcm_delay()` returns the loopback ring fill, not DAC latency.
shairport reads the ring fill as drift, periodically misfires its
resync-threshold path, and emits an audible artifact (drop ~6,600 frames
+ inject up to 250 ms of silence) every ~63 seconds.

We've materially reduced the frequency (every ~11 s → every ~60 s via
[PR #75](https://github.com/jaspercurry/JTS/pull/75)) and shipped a
user-facing toggle to opt out of sync entirely
([PR #76](https://github.com/jaspercurry/JTS/pull/76)). The default is
"free-running" because that eliminates the glitch outright at the cost of
A/V lip-sync (only matters when AirPlaying video) and multi-room sync
(only matters with multiple AirPlay speakers — we don't do that).

A path to truly eliminating the glitch *while keeping sync* is laid out
in the [Path forward](#path-forward) section.

---

## The chain (architecture context)

```
AirPlay sender (Mac / iPhone)
        │  RTP audio + PTP timestamps over WiFi
        ▼
shairport-sync (AirPlay 2 receiver, source-built v4.3.7)
        │  44.1 kHz S32 → plughw:Loopback,0,0 (kernel resamples 44.1→48)
        ▼
snd-aloop (Linux kernel module — Card 6 "Loopback")
        │  pcm.jasper_capture (dsnoop on Loopback,1,0 @ 48 kHz S16_LE)
        ▼
CamillaDSP (Rust, capture+playback, AsyncSinc Balanced, enable_rate_adjust=true)
        │  48 kHz S16_LE → pcm.jasper_out (dmix on Apple USB-C dongle)
        ▼
Apple USB-C → 3.5mm dongle (USB 1.1, 12 Mbit/s, async UAC2)
        │
        ▼
TPA3255 class-D amp + speakers
```

snd-aloop is configured with `timer_source="hw:A,0"` so the loopback's
`hw_ptr` advances on the **dongle's** PCM clock rather than the kernel
`jiffies` tick. CamillaDSP's `enable_rate_adjust=true` + no software
resampler is the canonical config when capture rate == playback rate
(both 48 kHz) — confirmed by CamillaDSP's own startup warning if
violated.

Volume control: shairport's drift-correction is independent of the
ducking knob (`main_volume` on CamillaDSP). The audible glitch we
describe here is independent of volume level.

---

## The four clocks at play

| Clock | What it is | What it drives |
|---|---|---|
| **A** | Mac's audio clock (CoreAudio's internal sample clock) | `rtptime` stamps on RTP audio packets |
| **B** | Mac's PTP master clock | PTP Sync/Follow_Up messages over WiFi UDP 319/320 |
| **C** | Pi's CPU clock (nqptp-disciplined to B) | `should_be_frame` in shairport's player.c |
| **D/E** | Apple dongle's USB-audio crystal (= snd-aloop's `hw_ptr` via `timer_source="hw:A,0"`) | actual audio playback rate AND what `snd_pcm_delay()` reports |

**The trick:** shairport's drift correction wants the difference between
(A,C) and (rtptime, real DAC latency). What it gets is the difference
between (A,C) and (rtptime, **loopback ring fill**). The loopback ring
fill is not a clock — it's `writes − reads`, a function of shairport's
write rate vs CamillaDSP's drain rate.

---

## The observed log signature

Every ~63 seconds, in shairport-sync's journal (verbosity=1):

```
"player.c:2883" Large positive (i.e. late) sync error of 2210 frames (0.050294 seconds)
"audio_alsa.c:1823" alsa: recovering from a previous underrun.
"audio_alsa.c:1823" alsa: recovering from a previous underrun.
"player.c:2908" Large negative (i.e. early) sync error of -21000 frames (-0.480 seconds)
"player.c:2908" Large negative (i.e. early) sync error of -10000 frames (-0.230 seconds)
```

Then quiet for another ~60 s. The pattern is one event observed at two
stages of its own aftermath, not two independent drift incidents.

---

## First-principles mechanism (source-cited)

### The "50 ms" in the log is the trigger threshold, not the measurement

[`shairport.c:2054`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/shairport.c#L2054)
defines `config.resync_threshold = 0.050` seconds. The log message at
[`player.c:2880-2936`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2880-L2936)
fires the instant `sync_error` exceeds that threshold for 4 consecutive
packets. The "2210 frames (0.050294 seconds)" we see is the trigger
value — not the drift magnitude.

### What shairport does when it fires

[`player.c:2894-2905`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2894-L2905):
on **"Large positive"** (audio is late), shairport calls
`do_flush(flush_to_frame)` with `source_frames_to_drop = sync_error +
input_rate * resync_recovery_time` — defaulting to ~6,600 dropped
source frames. **Audible cut.**

[`player.c:2915-2935`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2915-L2935):
on **"Large negative"** (audio is early), shairport writes up to
`5 * filler_length = 11,025` frames of zero PCM to the output handle.
**Audible drop-to-silence.**

These are large discrete corrections, NOT subtle ±1-sample stuffing.

### `drift_tolerance_in_seconds` is the wrong knob

[`player.c:2950-2989`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2950-L2989)
holds the in-bounds continuous-stuffing path (±1 sample per packet,
chosen randomly weighted toward sync). That path is gated by
`config.tolerance` (`drift_tolerance_in_seconds`). **The audible events
we see fire on a different code path** (`config.resync_threshold`), so
raising `drift_tolerance` does nothing. Empirically confirmed: PR #75
set `drift_tolerance_in_seconds=0.1` (50× the default) and the audible
events continued unchanged.

### What `sync_error` actually computes

[`player.c:2722-2756`](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c#L2722-L2756):

```c
sync_error = should_be_frame − will_be_frame;
//   should_be_frame: local_time_to_frame(now) — based on PTP-disciplined CPU clock
//   will_be_frame:   inframe.given_timestamp − current_delay
//   current_delay:   snd_pcm_delay(output_handle)
```

shairport's output handle is `plughw:Loopback,0,0`. So
`current_delay = snd_pcm_delay()` returns the **loopback ring buffer
fill** (`appl_ptr − hw_ptr`), not the dongle's actual DAC latency.

Mike Brady's own diagnosis on
[issue #1980](https://github.com/mikebrady/shairport-sync/issues/1980),
2025-02-20:

> "At a guess, I'd say that the latency being reported back to
> Shairport Sync is wrong. I'll take a look."

He returned 2025-03-02 with *"Still looking at this … but it doesn't
look great"* and let the issue go stale unresolved. moodeaudio reports
the same symptom on a chain that has NO snd-aloop
(shairport → `alsa_cdsp` → CamillaDSP → DAC), confirming the issue is
fundamentally "shairport vs any DSP downstream" — snd-aloop is not the
gating factor, just one common manifestation.

### Why the same stuffing is inaudible direct but audible through DSP

CamillaDSP's AsyncSinc resampler (rubato crate,
[`asynchro_sinc.rs:170-298`](https://github.com/HEnquist/rubato/blob/master/src/asynchro_sinc.rs#L170-L298))
uses a wide sinc kernel (64-256 samples for Balanced). When shairport
drops a chunk of source frames or injects zeros upstream, AsyncSinc
convolves that discontinuity through its kernel and produces audible
pre- and post-ringing — a smeared transient on each side of the cut.

A direct `shairport → plughw:DAC` chain runs through ALSA's plug-layer
resampler which has a much narrower kernel — same discontinuity is
sharper in time and less audibly conspicuous, especially at headphone
volume. Through a DSP plus an amplifier at music level, it's clearly
audible.

---

## What we've measured

Two 5-minute polls of `/proc/asound/Loopback/pcm0p/sub0/status` at
2 Hz, with shairport's journal captured in parallel. Both runs with
`disable_synchronization=no` (sync mode) and `timer_source="hw:A,0"`.

### Run 1 — `enable_rate_adjust=true` (current canonical config)

```
fill min:       0 frames     (0 ms)
fill max:       25,600       (533 ms at 48 kHz)
fill mean:      23,581       (491 ms)  ← matches our audio_backend_buffer setpoint
fill p2p swing: 25,600       (533 ms)
Large positive events: 5 in 300 s    interval: 64/63/61/66 s   (mean ~63 s)
camilla underruns:     0
camilla short reads:   0
```

### Run 2 — `enable_rate_adjust=false`

```
fill min:       0 frames     (0 ms)
fill max:       25,600       (533 ms)
fill mean:      23,572       (491 ms)
fill p2p swing: 25,600       (533 ms)
Large positive events: 5 in 300 s    interval: 62/64/64/62 s   (mean ~63 s)
```

**Identical.** CamillaDSP's `enable_rate_adjust` is NOT the source of
the ~63 s cycle. The cycle is real physical clock drift between
shairport's nominal write rate (host CPU's 48 kHz) and snd-aloop's
effective drain rate (= dongle's actual crystal). At ~667 ppm drift,
buffer fill grows by ~0.67 ms/s = ~42 ms over 60 s — and that's the
slow ramp we see in every cycle.

The fill peaks at exactly 25,600 frames (533 ms) every time, suggesting
shairport has an internal cap that prevents further growth — when fill
reaches that level, the next sync check fires `resync_threshold` and we
get the visible event.

---

## What we've tried (chronological)

### Initial state (before fixes)
- shairport-sync default config
- CamillaDSP with `enable_rate_adjust: true` + `resampler: AsyncSinc Balanced`
- snd-aloop with default `timer_source=` (jiffies)
- Audible glitches every ~11 seconds

### [PR #75](https://github.com/jaspercurry/JTS/pull/75) — multi-knob tuning (merged)
- CamillaDSP: dropped the redundant AsyncSinc resampler (capture and
  playback both at 48 kHz; rate_adjust does drift correction; running
  both AsyncSinc AND rate_adjust caused them to fight, per
  [HEnquist/camilladsp#207](https://github.com/HEnquist/camilladsp/issues/207))
- shairport: bumped `audio_backend_buffer_desired_length_in_seconds` to
  0.5, `drift_tolerance_in_seconds` to 0.1, `interpolation` to "auto"
- shairport systemd unit: `Nice=-10`, `IOSchedulingClass=realtime`
- install.sh: disable WiFi power-save on the active wlan0 connection

**Result:** glitch interval went from ~11 s to ~60 s. CamillaDSP's
`Capture read short` warnings (~28,000/hr) and playback underruns
(every ~5 s) went to **zero**. The remaining glitch is what this
document is about. `drift_tolerance_in_seconds=0.1` turned out to be
irrelevant — it gates a different code path. The CamillaDSP rate_adjust
fix is the part that mattered.

### [PR #76](https://github.com/jaspercurry/JTS/pull/76) — toggleable mode (merged)
- `disable_synchronization` knob now controlled by
  `/var/lib/jasper/airplay_mode.env`
- Web UI at `http://jts.local/airplay/` (free-running vs synced radio)
- CLI: `sudo jasper-airplay-mode set [free-running|synced]`
- shairport-sync.service uses `ExecStartPre=+/usr/local/sbin/jasper-apply-airplay-mode`
  to render the conf from a template on every restart
- Default: free-running

**Result:** free-running mode eliminates the audible event completely
(zero sync errors over 180 s sample). Trade-off: A/V lip-sync drifts
for video AirPlay (not used today) and inter-speaker sync drifts for
multi-room AirPlay (not used today). Set as production default; user
can toggle when they need sync.

### Diagnostic experiments (this session, not shipped)

- **snd-aloop `timer_source="hw:A,0"`** — slaves loopback `hw_ptr` to
  the dongle's PCM clock. Empirical effect: per-event underrun
  recoveries dropped 10 → 3, broken-pipes dropped 1-2 → 0, but sync
  error event count and period unchanged. Marginal quality improvement.
  Not yet shipped; on Pi temporarily for testing.
- **USB port move** — separated mic and dongle onto independent Pi 5
  USB host hubs (Bus 001 + Bus 003 instead of both on Bus 003). No
  measurable effect on the glitch. Hardware change preserved.
- **AEC bridge stop** — eliminated the AEC bridge as a suspect.
  Stopping it had no significant effect on the glitch frequency.
- **CamillaDSP `enable_rate_adjust=false`** — see Run 2 above. No
  measurable effect. Reverted after test.

---

## Currently in production

- `/etc/shairport-sync.conf` rendered from
  [`deploy/shairport-sync.conf.template`](../deploy/shairport-sync.conf.template)
  with `disable_synchronization = "yes"` (free-running, default).
- `/etc/modprobe.d/snd-aloop.conf` has `timer_source="hw:A,0"` —
  applied live but NOT yet in repo. Will be reverted or codified
  depending on which solution we pick next.
- All shipped tuning from PR #75 + PR #76.

User can toggle to synced mode via `/airplay/` web UI, but it will
exhibit the glitch every ~63 s in synced mode until we ship a real fix.

---

## Path forward

Options ranked from cheapest to biggest lift. They are not mutually
exclusive.

### Option A — Raise `resync_threshold_in_seconds` (~5 min test)
shairport has two correction paths:
- Continuous ±1-sample stuffing — gated by `drift_tolerance`, smooth
  and inaudible. Max correction rate ~124 Hz at 44.1 kHz chunk rate
  (plenty to absorb the observed ~32 Hz drift).
- Discrete `do_flush` + silence injection — gated by
  `resync_threshold`, audible.

Raising `resync_threshold_in_seconds` from 0.050 to ~0.2 s would keep
sync_error below the discrete-correction trigger, leaving only the
continuous path active. The continuous path should comfortably absorb
667 ppm drift since the buffer cap is at 533 ms and the continuous
adjustment headroom is ~5× the observed drift rate.

**Pros:** one config knob. Reversible. No architectural change.
**Cons:** if continuous stuffing can't actually keep up under our exact
conditions, buffer might keep growing until a larger correction is
forced eventually.
**Verdict:** the obvious first test.

### Option B — Direct shairport → stdin → CamillaDSP pipe
shairport supports `output_backend = "stdio"` (write raw PCM to
stdout). CamillaDSP can capture from stdin. This eliminates snd-aloop
from the AirPlay path:

```
shairport-sync --output-backend=stdio | camilladsp --capture-stdin → dongle
```

shairport's output handle is a pipe, not an ALSA device, so
`snd_pcm_delay()` is never called — drift correction falls back to a
simpler chunk-counting model.

**Pros:** Eliminates the `snd_pcm_delay()`-misreporting at the root.
Keeps CamillaDSP (and its room correction) on AirPlay audio.
**Cons:** Different runtime model. Need to keep snd-aloop alive for
librespot + bluealsa anyway (separate substreams). Effectively
different chains per renderer. install.sh and ducking integration both
need updates.
**Verdict:** clean fix but architecturally inconsistent.

### Option C — Minimal PipeWire as the audio bus (~1-2 weeks)
Install minimal `pipewire` daemon (no wireplumber, no pulse/jack compat
layers). Migrate shairport → pipewire output, CamillaDSP →
pipewire capture/playback, librespot → pipewire, bluealsa →
pipewire.

PipeWire's link graph provides sample-accurate scheduling. Each node
reports true end-to-end latency to its writer, so shairport's
`snd_pcm_delay()`-equivalent (PipeWire's own delay reporting) is
honest.

**RAM cost on 2 GB Pi 5:** ~10-20 MB for the pipewire daemon
(wireplumber not needed for fixed-config). Negligible — current
available RAM is ~1 GB.
**Implementation cost:** Significant. Every renderer + the mux + the
volume-coordinator integration needs revalidation. New failure modes
(pipewire daemon wedge = all audio gone, versus snd-aloop = kernel
module that "just works").
**Memory rule:** the `feedback_aec_keep_bridge_architecture.md` rule
("No PipeWire") was scoped to AEC engine choice, not audio routing.
PipeWire-as-bus is a separate decision.
**Verdict:** the "right" structural fix. Largest lift.

### Option D — Patch shairport-sync to take a delay source
Fork shairport, add a config option to source the actual DAC delay
from somewhere outside the ALSA output handle (e.g., a sidecar daemon
that queries the dongle's USB UAC2 delay endpoint directly).

**Verdict:** open-ended upstream contribution; skip for now.

---

## Decision pending

**Plan:** test Option A. If it works (continuous path absorbs the
drift, no audible artifacts), ship it via the
[`shairport-sync.conf.template`](../deploy/shairport-sync.conf.template)
and we're done. If it doesn't, escalate to Option B or C.

---

## References

### Internal
- [PR #75 — camilla rate_adjust + shairport tuning](https://github.com/jaspercurry/JTS/pull/75)
- [PR #76 — user-toggleable sync mode](https://github.com/jaspercurry/JTS/pull/76)
- [deploy/shairport-sync.conf.template](../deploy/shairport-sync.conf.template) — current shairport config template
- [deploy/camilladsp/v1.yml](../deploy/camilladsp/v1.yml) — current CamillaDSP config
- [docs/audio-paths.md](audio-paths.md) — generic audio path reference

### Upstream / external
- [shairport-sync issue #1980 — CamillaDSP-in-chain sync errors, maintainer's diagnosis](https://github.com/mikebrady/shairport-sync/issues/1980) (THE canonical issue, unresolved)
- [shairport-sync issue #1768 — clock model statement](https://github.com/mikebrady/shairport-sync/issues/1768)
- [shairport-sync source, player.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/player.c) — sync error code
- [shairport-sync source, audio_alsa.c (4.3.7)](https://github.com/mikebrady/shairport-sync/blob/4.3.7/audio_alsa.c) — precision_delay_and_status
- [shairport-sync TROUBLESHOOTING.md](https://github.com/mikebrady/shairport-sync/blob/master/TROUBLESHOOTING.md)
- [HEnquist/camilladsp issue #207 — rate_adjust + AsyncSinc oscillation](https://github.com/HEnquist/camilladsp/issues/207) (root cause of pre-PR-#75 issue)
- [rubato AsyncSinc source](https://github.com/HEnquist/rubato/blob/master/src/asynchro_sinc.rs)
- [Linux aloop.c (kernel snd-aloop driver)](https://github.com/torvalds/linux/blob/master/sound/drivers/aloop.c)
- [ALSA Project Matrix:Module-aloop](https://www.alsa-project.org/wiki/Matrix:Module-aloop)
- [nqptp (PTP daemon for shairport-sync)](https://github.com/mikebrady/nqptp)
