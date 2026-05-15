# Acoustic Echo Cancellation — investigation, design, and current state

This document describes the AEC subsystem in detail: why it exists,
what we tried, what failed, what shipped, and what's still open. It
is the canonical source for anyone touching `jasper-aec-bridge`,
`jasper-aec-init`, the `pcm.jasper_capture` dsnoop, the bridge↔voice
UDP transport (see [HANDOFF-resilience.md](HANDOFF-resilience.md)
for why it's UDP and not a second snd-aloop card), the
`jasper/xvf/` vendored XMOS control library, or any of the
supporting documentation in `BRINGUP.md` and
`docs/audit-pending-followups.md`.

**Companion doc**: [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) is the
chip-side canonical reference — full parameter space, firmware
variants, DFU flow, ALSA mixer invariants, ranked hypothesis ladder
for raw-mic-silence symptoms, and diagnostic cookbook. This doc
(HANDOFF-aec.md) explains the *engine* and the *why* (why software
AEC, why not chip AEC); HANDOFF-xvf3800.md explains the *chip*.
The `jasper/mics/xvf3800.py` profile module is the canonical
source for chip-specific constants consumed at runtime.

The goal is to make this enough context that a future session can
pick up the work without re-doing the investigation.

---

## TL;DR / current state

**The software AEC bridge is shipped and auto-enabled when the chip
is on the 6-channel firmware variant.** `install.sh` seeds
`/var/lib/jasper/aec_mode.env` with `JASPER_AEC_MODE=auto`, enables
`jasper-aec-reconcile.service`, and runs the reconciler once. The
reconciler flips `JASPER_MIC_DEVICE` to `udp:9876` only when the
configured AEC mic is actually present with 6-channel firmware, then
enables / starts `jasper-aec-init` + `jasper-aec-bridge`. The chip's
on-board AEC is not in the audio path — this doc explains why.

To turn the bridge OFF (or back to chip-direct mic for A/B testing),
set the state file to disabled and run the reconciler:

```sh
printf 'JASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

To return to auto mode:

```sh
printf 'JASPER_AEC_MODE=auto\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

The reconciler also handles stale hardware state. If the Array is
absent after a previous AEC-enabled boot, it clears the stale
`JASPER_MIC_DEVICE=udp:9876`, disables the bridge, and stops voice
instead of leaving wake-word on an unfed UDP socket.

The bridge→voice transport is **UDP localhost** (default
`127.0.0.1:9876`), not snd-aloop. The original `LoopbackAEC`
two-card snd-aloop topology was retired in May 2026 after a
kernel-state-corruption incident — see
[HANDOFF-resilience.md](HANDOFF-resilience.md) for the rationale.

---

## The problem

A smart speaker that **plays music** and **listens for a wake word**
in the same physical box has a fundamental signal-processing
problem. The microphone hears:

- the user's voice (what we want), at typical levels of −30 to
  −50 dBSPL at the mic;
- the speaker's own output, reflected/refracted/reverberating
  through the room and back to the mic, at levels that can be
  20–40 dB louder than the voice when music is playing at any
  meaningful volume.

If we feed the raw mic signal to the wake-word detector
(openWakeWord), the speaker's own output dominates the signal and
the detector fires on phonemes from the music or — worse — on the
TTS responses we just synthesised, causing a feedback loop. **Echo
cancellation** is the standard fix: subtract the known speaker
signal (the "far-end reference") from the mic capture, leaving
only the voice (the "near-end signal"). The closer to perfect the
cancellation, the better the wake-word reliability and the better
the dialog UX (allowing barge-in over TTS, etc.).

There are three well-known places to do this work:

1. **In the mic chip** (hardware-accelerated AEC running on a
   dedicated DSP next to the ADC). Fast, low-power, no host CPU
   cost, but the chip has to be designed for this and the
   topology has to match what the chip's firmware expects.
2. **In the host** (software AEC running on the Pi CPU). Flexible,
   tweakable, but costs CPU and RAM and is generally less effective
   at high SPL than hardware AEC.
3. **Avoid it entirely.** Push-to-talk, physical mic-speaker
   isolation, or "duck the music to silence on wake" as workarounds.
   These eliminate the AEC requirement at the cost of UX
   compromises.

This doc is about getting (1) or (2) working in our specific
hardware topology.

---

## Hardware overview

| Component | Role |
|---|---|
| Raspberry Pi 5 (1GB or 2GB) | Host running the jasper daemons |
| Apple USB-C → 3.5mm dongle | The actual speaker output. 48 kHz native, simple UAC2 device. |
| TPA3255 amp + speakers | Driven from the dongle's 3.5mm output |
| Seeed ReSpeaker XVF3800 (USB UA variant) | 4-mic array with on-board XMOS DSP. Connected over USB. |

The crucial topological fact: **the speaker is driven by the Apple
dongle, not by the XVF3800's onboard codec.** The XVF chip has its
own AIC3104 codec with a 3.5mm jack, but it's electrically
disconnected on this build (it was tried originally and produces
unacceptable hiss).

This decision — external DAC for speaker output — is what makes
this project off the beaten path. Every published XVF3800 reference
design (Seeed wiki tutorials, FormatBCE's ESPHome integration,
HA Voice PE with the related XU316 chip) drives the speaker from
the chip's own codec.

---

## What the XVF3800 is designed to do

The XVF3800 is a purpose-built voice DSP. Its on-chip pipeline is:

```
4 PDM mics → mic array preamp → AEC (BeClear adaptive filter)
          → beamformer → noise suppression → AGC
          → conference channel + ASR channel → USB capture out
                                              (or I²S out)
```

It expects a **far-end reference** signal — the audio that the
speaker is being asked to play — so the AEC adaptive filter can
learn the room transfer function and subtract the echo. Per the
XMOS XVF3800 v3.2.1 User Guide §3.5 ("Audio Pipeline"), every
defined "Far end" source category in the chip's `AUDIO_MGR` enum
is documented as **"Far end data received over I²S, post sample
rate conversion to 16 kHz if required"**. The chip's design
assumption is that whatever comes out the chip's own DAC pin is
also what's playing in the room — they are the same node in the
data plane.

The chip exposes two firmware variants over USB (full table of
published versions in [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) §2.1):

- **2-channel firmware**: USB capture has 2 channels — channel 0
  is "conference" (post-AEC + BF + NS + AGC), channel 1 is "ASR"
  (different post-processing tuned for speech recognition). No
  raw mic access. The boards we received from Seeed shipped on
  v2.0.6 of this variant; v2.0.5 and v2.0.7 are also 2-channel.
- **6-channel firmware** (the `_6chl_` filename variant): adds
  raw mics on USB capture channels 2–5. The processed
  conference/ASR channels stay on 0/1. As of 2026-05-15 the only
  6-channel build in upstream `master` is v2.0.8.

The chip's USB UAC2 endpoint also has a **playback** direction —
the host can write audio TO the chip — and the chip's firmware
documents that this can serve as the AEC reference *when running
in the UA configuration with no I²S input*.

So in principle, our setup should work: write the same audio to
the chip's USB-IN that we're playing on the dongle, the chip's AEC
sees a reference, cancels echo, and emits a clean processed mic
on USB-OUT.

---

## What we found about chip-side AEC in our topology

We pursued the chip-side AEC path first. It would be ideal — zero
host CPU cost, lowest latency, the chip is purpose-built. The
investigation took roughly half a session and ultimately concluded
that the chip's AEC does not work usefully in our external-DAC
topology, regardless of configuration.

### What we built

- A two-stage ALSA fan-out so the same audio that goes to the
  dongle also goes to the chip's USB-IN endpoint at 16 kHz S16_LE
  (the only rate/format the chip's USB-IN endpoint advertises).
  The first attempt used `type plug → type multi → 2x[plug → dmix]`
  with mismatched leg rates and failed with `EINVAL` (`type multi`
  requires identical period_size across slaves; the underlying
  cause was period-size negotiation, not the rate mismatch as
  initially blamed). The working topology used `type plug` with
  `route_policy "duplicate"` over `type multi` with all legs at
  48 kHz, paired with a second CamillaDSP instance acting as a
  rate-conversion bridge from 48k to 16k.
- A `jasper-aec-init.service` that runs at boot to apply
  `AUDIO_MGR_SYS_DELAY` (the chip's bulk-delay tuning knob).
- A `jasper-aec-tune` CLI that does white-noise cross-correlation
  to measure the host-to-mic round-trip delay and program the
  chip with the result.
- The vendored `xvf_host.py` (from
  `respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/`)
  for talking to the chip's parameter API over USB vendor control
  transfers.

All of this is still in the repo, partly because the architecture
(snd-aloop + chip control) is still useful and partly as
investigation history.

### What we measured

With the bridge confirmed running and the chip's USB-IN endpoint
in `state: RUNNING` with `appl_ptr` advancing (i.e. real audio
data physically reaching the chip):

- `AEC_AECCONVERGED` returned 0 in every test. The chip's own
  convergence flag never flipped to "converged."
- A controlled-sweep test with `SHF_BYPASS=1` (raw mic) vs
  `SHF_BYPASS=0` (full pipeline) at a range of
  `AUDIO_MGR_SYS_DELAY` values from −64 to +256 samples
  (the chip's accepted range — values >256 silently clamp)
  showed bypass-vs-AEC RMS differing by ≤2 dB at every setting.
- A filter coefficient dump (`SPECIAL_CMD_AEC_FILTER_COEFFS`)
  showed the adaptive filter HAD adapted in some past state
  (RMS 0.224, peaks at taps 2 and 243), but with peak magnitudes
  >1.0 — indicating the LMS algorithm had run away due to a
  reference signal that was too quiet relative to the mic
  capture.

### Why it doesn't work — the discovery

The XMOS User Guide §4.2.1 documents:

> "AEC_FAR_EXTGAIN: This parameter informs the audio pipeline how
> much external gain has been applied to the AEC reference signal.
> In the UA device variant, when the host sets the output volume,
> the AEC_FAR_EXTGAIN is internally set to be the same as the gain
> set by the host, so the user shouldn't need to set this command
> externally."

Translation: the chip's AEC reference path runs through an internal
gain stage that automatically tracks whatever the host has set as
the chip's USB-OUT (playback) volume control. If the host's ALSA
mixer hasn't explicitly set a volume on the chip's UAC2 sink, the
chip parks `AEC_FAR_EXTGAIN` at the default reset value (in our
case −40 dB), and **internally attenuates the AEC reference signal
by 40 dB**. We then deliver our reference signal at full level via
the dsnoop tap, but the chip's internal reference becomes
inaudible to its own AEC adaptive filter — which then either
gives up or runs away trying to compensate.

We confirmed this by setting the chip's UAC2 PCM mixer to 0 dB
unity (`amixer -c Array sset PCM,0 60 unmute`) and observing
`AEC_FAR_EXTGAIN` flip from −40 dB to 0 dB. AEC effectiveness
improved marginally (still ≤2 dB attenuation) but never approached
the −20+ dB the chip is capable of in its native topology.

The deeper issue: **even with EXTGAIN fixed, the chip's AEC is
designed assuming the chip's own audio output drives the speaker.**
In that intended topology, the chip can perfectly model the
relationship between what it sent to its DAC and what the mic
captures, because there's no external variable. In our topology,
the speaker is driven by a different USB device (the dongle) on a
different clock domain with different USB scheduling latency,
different output buffering, and different hardware path delays.
The chip's AEC isn't designed to handle that mismatch — and the
public XMOS documentation never describes a working configuration
for it.

We searched the respeaker repo issues, the XMOS forums, and the
broader open-source voice-assistant community (Stuart Naylor's
Rhasspy/HA Voice writeups, the FutureProofHomes Satellite1
project, the ESPHome XVF3800 integration, the HA Voice PE
community) for any working external-DAC + USB-IN-as-reference
setup on Linux. Found none. We would be the first.

### Pros and cons of chip-side AEC (in summary)

**Pros:**
- Zero host CPU cost (DSP runs on the chip's dedicated cores).
- Lowest possible latency (sub-millisecond from mic to AEC
  output).
- Includes 4-mic beamforming, dereverberation, noise suppression,
  AGC, and direction-of-arrival as part of the pipeline — much
  more than just AEC.
- Tuned by professionals for high-SPL speech recognition.

**Cons in our specific topology:**
- The AEC pipeline assumes the chip drives the speaker. With an
  external DAC, the chip can't observe the actual speaker output
  and the volume-tracking internal gain mechanism actively
  sabotages the reference signal.
- No public documentation or community prior art for our
  topology. We'd be guessing at undocumented chip behavior.
- The XMOS firmware is closed (binaries downloadable, source
  gated behind XMOS developer registration + XTAG-4 hardware
  for re-flashing custom builds). Modifying chip behavior is a
  significant project.
- Even with the volume-mirror workaround, measured attenuation
  was ≤2 dB — not useful.

---

## The pivot: software AEC

After confirming the chip-side path was a dead end, we pivoted to
software AEC running on the Pi. The architecture for the chip-side
attempt — capture-side fan-out, snd-aloop loopback, dedicated
bridge process — happened to be exactly the right shape for
software AEC too. Most of the work transferred over; the bridge
just changed what it does internally.

### The architecture

```
renderers (shairport-sync, librespot, bluealsa-aplay)
    │
    │  each writes directly to hw:Loopback,0,0
    ▼
hw:Loopback,0,sub0  ← snd-aloop card 6, kernel-clocked
    │  cross-wired by snd-aloop
    ▼
hw:Loopback,1,sub0
    │
    ▼
pcm.jasper_capture  ← type plug → type dsnoop on Loopback,1,sub0
    │  dsnoop allows multiple readers each to get an independent
    │  copy of the audio
    │
    ├──► reader A: jasper-camilla
    │       main_volume ducking + flat passthrough
    │       writes to → pcm.jasper_out (dmix on Apple dongle)
    │       → speaker (audible path)
    │
    └──► reader B: jasper-aec-bridge (Python, alsaaudio + jasper_aec3)
            captures jasper_capture (48k stereo) for FAR-END REFERENCE
            captures hw:Array,0 (XVF, 16k 6ch) for NEAR-END MIC
            takes channel 2 of the chip capture (raw mic 0)
            downsamples ref 48k → 16k mono on left
            runs WebRTC AEC3 (10ms windows) frame by frame
            sends AEC'd mono 16k via UDP → 127.0.0.1:9876
                                              │
                                              ▼
                                           jasper-voice
                                              UdpMicCapture binds the same port
                                              openWakeWord + Gemini Live
```

One snd-aloop card. "Loopback" (card 6) carries the music chain —
renderer → camilla → dongle, with the bridge tapping the camilla
input via dsnoop. The AEC'd mic from bridge to voice rides UDP
localhost instead of a second snd-aloop card; see
[HANDOFF-resilience.md](HANDOFF-resilience.md) for why we retired
the original `LoopbackAEC` snd-aloop topology in May 2026 (short
version: snd-aloop's kernel-side `loopback_cable` wedges when a
consumer is SIGKILL'd, requiring a reboot to clear; UDP localhost
has no kernel state to corrupt).

Card 5 specifically (rather than 1) because Pi 5's HDMI audio
already occupies index 1 — the snd-aloop kernel module silently
drops the second card on index collision.

### Why the dsnoop tap

Initial attempts used a `type multi` fan-out on the **playback**
side (CamillaDSP outputs to a multi PCM with two slaves: dongle
dmix + AEC-leg dmix). After significant debugging, this was found
to silently fail to write data to slaves beyond the first — the
multi accepted frames but only forwarded them to slave A. We
verified via `appl_ptr` on the snd-aloop substreams (stuck at 0
on slaves B and C despite the substreams showing `RUNNING`).
Switching the fan-out to the **capture** side via `dsnoop` made
this work cleanly: dsnoop is the canonical ALSA primitive for
"multiple readers share one capture device" and it does what it
says.

### Engine choice: WebRTC AEC3 via direct pybind11 binding

The software AEC engine landscape, with how we ended up where we
are now:

- **SpeexDSP** (xiph, `libspeexdsp-dev`). Mature, small, simple
  Python bindings. Project initially shipped this because the
  integration path was the shortest — `xiongyihui/speexdsp-python`
  wraps the C library and slots into the bridge pattern cleanly.
  Speex's own docs warn it can't model speaker non-linearity at
  high SPL — falls over on music. Best measured was −2 to −8 dB.
  Removed when AEC3 landed; see git log for the historical
  config.
- **WebRTC AEC3** (current production). The modern Google echo
  controller — frequency-domain canceler with residual suppressor
  and drift-tolerant delay estimator. Trixie's apt ships
  `libwebrtc-audio-processing-1` v1.3-3, which IS AEC3 (the 1.x
  is package-API stability, not algorithm version). We wrote our
  own pybind11 binding (`jasper_aec3/`) rather than going through
  PipeWire — PipeWire would have required restructuring our ALSA
  topology and only forwards top-level `AudioProcessing::Config`
  knobs anyway (the deep AEC3 config struct isn't exposed; see
  "Deep tuning landscape" below).
- **Neural AEC** (DeepVQE-S, DTLN-aec, GTCRN-AEC, etc.). Best
  quality on AEC-Challenge benchmarks. Deferred — see "Deep
  tuning landscape" below for staging.

### Why alsaaudio for the reference capture

The reference signal lives at `pcm.jasper_capture` — a
custom-named PCM defined in `/root/.asoundrc`. PortAudio's device
enumeration only sees `hw:N,M` style devices and a few standard
aliases (`default`, `sysdefault`, `pulse`); custom asoundrc PCMs
aren't enumerated. The Python `pyalsaaudio` library calls
`snd_pcm_open(name)` directly via libasound and respects asoundrc,
so we use it for the ref capture path. The mic capture and AEC
output paths use sounddevice/PortAudio (existing daemon
convention) since they go through plain `hw:N,M` devices.

The bridge runs as root (no `User=` in the systemd unit) because
`/root/.asoundrc` is mode 0600. This matches the existing
jasper-camilla/jasper-voice pattern.

### Why 6-channel firmware

The 2-channel firmware exposes only the chip's processed channels
(conference, ASR). Both have already had the chip's broken AEC
applied, plus its NS, AGC, and beamformer — non-linear processing
that distorts the residual and makes software AEC's linear
adaptive filter struggle to model the echo path. Per Stuart
Naylor's writeups, software AEC over chip-processed audio is
generally a bad idea.

The 6-channel firmware (single DFU command to flash, fully
reversible) adds raw mics on channels 2–5. Software AEC on raw
mic 0 sees a clean linear input — much better convergence. The
DFU mechanism is in-system: the chip exposes its DFU interface in
normal runtime mode, no Safe Mode entry or button combo required.
Full operator procedure (download URL, verification, what each
flag does) is in [BRINGUP.md](../BRINGUP.md) Phase 2A.5. Headline:

```
sudo dfu-util -R -e -a 1 -D <6-channel-firmware.bin>
```

The chip's `SAVE_CONFIGURATION` op had a brick hazard on firmware
2.0.6 (respeaker repo issue #8) and the upstream issue is still
open as of 2026-05-15 with no release-note confirmation that any
version fixed it — we never call it regardless of firmware version.

---

## Hardware vs software AEC — comparison summary

| Dimension | XVF3800 hardware AEC | WebRTC AEC3 software AEC (current) |
|---|---|---|
| **Topology fit for our setup** | Designed for chip-driven speaker; doesn't work with external DAC | Topology-agnostic — bridge can capture any reference and any mic |
| **Effectiveness in our setup** | ≤2 dB sustained attenuation (measured) | −15 to −18 dB mean on music with production tuning; deep-cancel windows to −44 dB |
| **Host CPU cost** | ~0% (chip handles it) | ~3-8% of one A76 core |
| **Host RAM cost** | ~0 MB | ~110 MB RSS (Python + numpy + scipy + sounddevice + jasper_aec3) |
| **Latency** | <1 ms (chip-internal) | ~40 ms ref-to-mic measured; AEC3's delay estimator manages alignment internally |
| **Beamforming, NS, AGC, DoA** | Included, professional-grade | NS at kModerate is built into AEC3; no BF/AGC/DoA |
| **Configurability** | Closed binary, ~30 documented parameters | Top-level `AudioProcessing::Config` is public; deep `EchoCanceller3Config` isn't (see "Deep tuning landscape") |
| **Drift handling** | Internal (chip is single clock domain) | Two-clock-domain capture; AEC3 tolerates some drift via its built-in delay estimator |
| **Convergence** | Stable when working | Stable; residual suppressor + drift-tolerant delay estimator keep it consistent across music passes |
| **Worst-case (loud music + soft voice + far-field)** | Designed to handle this | Marginal — see Tuning findings for current numbers and remaining levers |

The honest summary: hardware AEC would be much better for our use
case if it worked, but it doesn't work in our topology. Software
AEC is what we have because nothing else is available to us
without significant additional engineering (custom firmware,
mic/speaker swap, hardware redesign).

---

## Resource cost (measured on Pi 5)

```
jasper-aec-bridge:  3-8% of one A76 core,  ~110 MB RSS
jasper-camilla:     0.5%,                    8 MB RSS
jasper-voice:      11.3%,                  265 MB RSS
                  ----                     -----
                   ~15-20% of one core      ~380 MB total
```

Relative to baseline (Pi 5 idle ≈ 270 MiB used), the bridge adds
~110 MB which puts the 1GB Pi 5 at ~38% memory usage. The 2GB
Pi 5 (which BRINGUP.md and PLAN.md have always recommended as the
v1 target) has comfortable headroom.

For the engine's actual attenuation numbers, see the Tuning
findings section below.

---

## Caveats and open issues

### Cross-clock-domain drift between reference and mic

The reference is captured from the snd-aloop loopback (kernel
timer-driven), and the mic is captured from the XVF chip (USB
UAC2 SYNC-clocked). These are independent clocks that drift by
~tens of ppm relative to each other. Over time, the AEC's
filter alignment slides. AEC3's delay estimator tolerates some
drift but not unbounded — over long sessions effectiveness
degrades.

The classical fix is async resampling on one leg to lock both
to the same clock (e.g. resample the mic to match the reference
clock via a second CamillaDSP instance with `enable_rate_adjust:
true`). We haven't implemented this; AEC3 currently rides on its
own delay-estimator robustness. Listed as a Tier 2 item in
PLAN.md's tuning roadmap.

### Reference tap is pre-CamillaDSP, speaker is post

`jasper_capture` taps the dsnoop on the renderer→camilla
loopback, *before* CamillaDSP applies `main_volume` ducking.
What hits the speaker is what comes out of CamillaDSP, *after*
ducking. So when the bridge ducks during a wake event, the
reference signal stays at full level while the speaker output
drops — meaning AEC3 momentarily sees a louder reference than
the actual echo. AEC3's residual suppressor masks most of this,
but the architecturally clean fix is to move the dsnoop tap to
a post-CamillaDSP slave. Listed as a Tier 2 item in PLAN.md.

### Bridge is Python (RAM-heavy)

The ~110 MB RSS for the bridge is mostly Python interpreter +
numpy + scipy + sounddevice. The `jasper_aec3` native binding
itself is tiny (~5 MB plus the AEC3 library it links against).
On the 1GB Pi 5 this is a noticeable fraction; on the 2GB Pi 5
it's fine.

If RAM becomes a constraint, the highest-impact savings are:
1. Drop scipy (~30 MB). Replace `resample_poly` with a
   pre-computed FIR + numpy.convolve.
2. Drop sounddevice (~15 MB). The bridge already uses alsaaudio
   for ref capture; could use it for everything.
3. Rewrite as Rust or C (~80–100 MB, ~1–2 days work). Bridge
   becomes a 10–20 MB process.

### The chip-side AEC infrastructure is still installed

`jasper-aec-init.service` still runs at boot (now just resets
chip state and sets UAC2 PCM to unity volume). `jasper-aec-tune`
is still installed but no longer relevant — the chip's AEC isn't
in the audio path. Both could be removed but kept for now
because (a) the chip control path remains useful for diagnostic
work, (b) re-introducing chip-side AEC if a topology change
makes it viable would be easier, (c) the doctor checks that
verify the chip is responsive depend on this code.

### We haven't run the wake-word reliability test

The whole point of AEC is to make wake-word detection work
during music playback. We've measured RMS attenuation but not
end-to-end "say 'Hey Jarvis' over loud music, count detections."
That test requires sitting in front of the speaker and
listening; it hasn't been done yet. Until it is, "this is
better than no AEC" is reasoning, not measurement.

---

## File map

Files involved in the AEC subsystem:

- `jasper/cli/aec_bridge.py` — the software AEC daemon (WebRTC
  AEC3 via the `jasper_aec3` pybind11 binding)
- `jasper_aec3/` — sibling package, pybind11 binding for WebRTC AEC3
  (`libwebrtc-audio-processing-1` v1.3-3 from Trixie's apt)
- `jasper/cli/aec_init.py` — boot-time chip init (resets chip,
  sets UAC2 PCM to unity)
- `jasper/cli/aec_tune.py` — calibrator for chip-side
  `AUDIO_MGR_SYS_DELAY` (vestigial; kept for diagnostic use)
- `jasper/xvf/xvf_host.py` — vendored from
  respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY
- `jasper/cli/doctor.py` — `check_aec_bridge_running`,
  `check_mic_capture`, `check_xvf_firmware_6ch`
- `deploy/alsa/asoundrc.jasper` — defines `pcm.jasper_capture`
  (the dsnoop tap) and `pcm.jasper_out` (dongle dmix)
- `deploy/modprobe.d/snd-aloop.conf` — single-card music-chain
  snd-aloop config (`index=6 id=Loopback`)
- `deploy/modules-load.d/snd-aloop.conf` — auto-load at boot
- `deploy/systemd/jasper-aec-bridge.service` — runs
  `jasper-aec-bridge` Python daemon
- `deploy/systemd/jasper-aec-init.service` — oneshot at boot
- `deploy/bin/jasper-aec-reconcile` +
  `deploy/systemd/jasper-aec-reconcile.service` — keeps
  `JASPER_MIC_DEVICE`, AEC service enablement, and current mic
  hardware in sync so stale `udp:9876` does not strand voice when
  the Array is absent
- `deploy/install.sh` — installs all of the above; builds the
  `jasper_aec3` pybind11 binding against `libwebrtc-audio-processing-dev`;
  installs `dfu-util` for chip firmware operations; seeds
  `/var/lib/jasper/aec_mode.env` and runs the reconciler once
- `pyproject.toml` — registers `jasper-aec-bridge`,
  `jasper-aec-init`, `jasper-aec-tune` console scripts; adds
  `pyusb`, `libusb_package`, `pyalsaaudio` deps
- `.env.example` — mic/AEC env knobs:
  `JASPER_AEC_MIC_DEVICE`, `JASPER_MIC_DEVICE_CANDIDATES`, UDP
  transport settings, and tuning gains
- `scripts/aec-probe-latency.sh` — chirp + cross-correlation
  measurement of end-to-end ref-to-mic delay (used to set the AEC3
  binding's `stream_delay_ms` default)
- `scripts/aec-probe-pinknoise.sh` — runs the bridge against
  stationary pink noise to measure the AEC engine's plateau
  attenuation (the upper bound for this setup, since music is
  documented as harder for AEC3)

---

## Tuning findings (2026-05-08)

After landing the WebRTC AEC3 engine option, we ran a structured
tuning pass to characterize attenuation against the actual hardware.
Logged here as the calibration baseline.

**Setup measured against:**

- Apple USB-C dongle → user's TPA3255 amp → bookshelf speakers
  (free-floating, not in a sealed cabinet)
- ReSpeaker XVF3800 6-ch firmware, raw mic 0 (channel 2, BYPASS
  mode = no chip-side AGC/BF/NS in path)
- WebRTC AEC3 via `libwebrtc-audio-processing-1` v1.3-3
- Mic placement: free-floating on desk ~3 ft from speakers
- `main_volume` at 0 dB (the dial's "100%")

**Measurements (baseline, REF_GAIN_DB=0):**

| What | How | Result |
|------|------|--------|
| End-to-end ref→mic delay | Chirp cross-correlation, `scripts/aec-probe-latency.sh` | **40 ms** (peak/median 5.2×) |
| AEC3 plateau on stationary content | 30 s pink noise, `scripts/aec-probe-pinknoise.sh` | **−11 dB**, converges in ~10 s |
| AEC3 on real-world music (AirPlay) | 90 s sustained streaming | **−2 to −7 dB**, oscillates with content, no convergence trend |
| Loop gain (digital ref RMS → mic RMS) | Bridge log RMS averages | **+27 to +30 dB** on music |

**Measurements (with REF_GAIN_DB=20, the loop-gain-correction lever):**

| What | How | Result |
|------|------|--------|
| AEC3 plateau on pink noise | Same probe + `REF_GAIN_DB=20` | **−16 to −18 dB**, converges in ~5 s (+5 to +7 dB lift) |
| AEC3 on music | 60 s music + `REF_GAIN_DB=20` | **−12 to −20 dB**, mean ~−15 dB, stable across loud and quiet passages (+10 dB lift) |
| Loop gain after the boost | Same RMS averages | **+7 to +9 dB** (was +27 to +30 dB) — inside AEC3's design window |

**Interpretation (with literature cross-reference):**

The headline "20-40 dB ERLE" attributed to AEC3 is for ideal
conferencing — near-field mic, integrated speaker, moderate SPL.
On real-world far-field recordings AEC3 alone delivers single-digit
to low-double-digit ERLE; the ICASSP AEC challenges stopped reporting
ERLE entirely circa 2022 because the metric becomes misleading on
real hardware. **Our −11 dB on pink noise is consistent with what
AEC3 actually delivers on realistic setups.**

The −5 to −10 dB gap between music and pink noise is the documented
non-stationarity penalty: AEC3's linear adaptive filter can't model
loudspeaker non-linearity, and music's transient content keeps the
filter in a perpetual re-converge state. RFC 7874 explicitly says
AEC SHOULD be turn-offable for music.

**The dominant problem is loop gain inversion.** AEC3 was designed
for setups where the digital reference is comparable to or louder
than the mic capture (typical conferencing has loop gain of −7 to
−10 dB; pro AEC guides — Bose, Biamp — recommend ref 7-10 dB
*louder* than mic). Our smart-speaker setup inverts that: the amp
+ speakers + room + chip mic preamp chain produces +27 to +30 dB
of round-trip gain. AEC3's adaptive filter math expects loop gain
near unity; ours sits well outside its design point.

**Mitigations tested:**

1. ✅ **Boost the digital reference** before it enters AEC3 — closes
   the loop gain gap directly. Implemented as the
   `JASPER_AEC_REF_GAIN_DB` env var on the bridge (default 0 dB).
2. ✅ **Hint AEC3's delay estimator** with the measured 40 ms via
   `set_stream_delay_ms`. Wired up as the AEC3 binding's
   constructor default. Convergence speeds up modestly (5 s vs 10 s
   on pink noise); steady-state plateau unchanged within
   measurement noise.
3. ✅ **AGC2 toggle** — `JASPER_AEC_AGC2=1` enables WebRTC's modern
   post-AEC gain controller. See sweep results below.

**Sweep matrix (pink noise, 30 s per config):**

| Config (AGC2, REF_GAIN_DB) | Mean attenuation | Peak attenuation | Variability |
|---|---|---|---|
| off, 20 | −17.8 dB | −21.6 dB | low |
| **off, 25** ← chosen | **−24.8 dB** (incl. deep-cancel moments) | **−43.8 dB** | high (deep moments + −16 dB floor) |
| off, 30 | −21.9 dB | −38.9 dB | medium |
| on, 20 | −14.8 dB | −17.5 dB | medium |
| on, 25 | −16.5 dB | −17.1 dB | low |
| on, 30 | −16.7 dB | −16.8 dB | very low |

**Reading the matrix:**

- **AGC2 ON looks like it makes attenuation worse by 3 dB on the metric, but that's measurement bias.** AGC2 sits *after* AEC and amplifies the residual back up to a target level. The actual residual echo isn't worse; the *amplified output* is louder, which makes the dB ratio look smaller. AGC2's value is in giving openWakeWord a normalized input, not in adding raw cancellation. The right judge of AGC2 is wake-word detection rate, not RMS attenuation.
- **AGC2 OFF lets AEC3 reach much deeper cancellation when its filter is well-converged.** The −38 to −44 dB windows are real deep-cancel moments. With AGC2 ON, those moments still happen at the AEC3 layer but get masked in the metric.
- **REF_GAIN above +25 dB hard-clips the digital reference at peaks** (np.clip is hard-clip; pink noise peak factor ≈ 3× RMS). The +30 dB config injects distortion AEC3 has to work around — fewer deep-cancel windows than +25 dB, suggesting the clipping is mildly hurting convergence. If we want to push beyond +25 dB cleanly we need to swap the hard-clip in `_ref_thread` for a soft-limiter (~15 lines of NumPy).

**Chosen production config: `JASPER_AEC_AGC2=0`, `JASPER_AEC_REF_GAIN_DB=25`.** Best peak attenuation, hits the loop-gain target zone closely without excessive clipping, simplest signal path for openWakeWord. If real-world wake-word testing later shows level instability at high SPL, flipping `JASPER_AEC_AGC2=1` is one env edit + bridge restart.

**Mitigations still on the table:**

3. **Enable WebRTC AGC2** as the post-AEC stage. AGC2 is the
   modern modular gain-controller (newer than AGC1; the "2" is
   per-module numbering, not "older than AGC3"). One-line config
   flip in the binding. Adds level normalization that helps
   downstream wake-word detection too. Worth trying if the
   wake-word-during-music acceptance test undershoots.
4. **Neural residual stage (DeepVQE)**. Skips the linear-filter
   fundamental limitation entirely. ~2-3 days of work per the
   project plan; treat as Stage 4, only if AEC3 + REF_GAIN_DB +
   AGC2 prove insufficient for the actual acceptance test. Given
   we're now at −15 dB on music, this stage is probably not needed.

**The acceptance test that matters** is end-to-end wake-word
detection rate during music at conversational distance, not raw
ERLE. We may already be close to passing with current attenuation
+ ducking + good mic placement (the desk + free-floating mic
geometry is favorable). That test is on the agenda for the next
session.

---

## Deep tuning landscape — research notes (2026-05-09)

After landing the production tuning above, we did an OSS-ecosystem
research pass on what AEC3 levers remain if the wake-word acceptance
test undershoots. The findings calibrate whether deeper AEC3 work
pays back vs pivoting to other architectural changes.

### Realistic ceiling on deep AEC3 tuning

The honest expected payoff for getting at AEC3's internal config:
**a few extra dB at most beyond our current −15 to −18 dB on
music.** AEC3 was tuned for conferencing topologies (near-field
mic, integrated speaker, moderate SPL); smart-speaker problems
(loud non-stationary content + far-field mic + speaker
non-linearity) sit at the edge of what any linear adaptive filter
can handle. The ICASSP AEC challenges stopped reporting ERLE
around 2022 because the metric becomes misleading on real
hardware. To reach the −25 to −35 dB band that commercial smart
speakers achieve, the ecosystem consensus is hybrid: linear AEC +
neural residual + retrained wake word.

### `EchoCanceller3Config` is not in the public API

The meaningful AEC3 tuning levers — `filter.refined.length_blocks`,
`ep_strength.bounded_erl`,
`suppressor.use_subband_nearend_detection`,
`dominant_nearend_detection.snr_threshold` — all live inside
`webrtc::EchoCanceller3Config`, which is **not in the public
headers** of either v1.x or v2.x of the pulseaudio fork. Trixie's
`libwebrtc-audio-processing-dev` 1.3-3 only ships
`webrtc::AudioProcessing::Config` (the top-level), not the
AEC3-specific config struct.

Cross-reference of the OSS ecosystem confirms this is universal:

- **PipeWire's** `spa/plugins/aec/aec-webrtc{,2}.cpp` only forwards
  `high_pass_filter`, `noise_suppression`, `gain_control`,
  `voice_detection`, `extended_filter` (legacy AEC2-only), plus a
  handful of beamforming/intelligibility flags that are no-ops on
  the v1.x/v2.x fork. Never instantiates `EchoCanceller3Config`,
  never calls `SetEchoControlFactory`. The ArchWiki page for
  `module-echo-cancel` documents this small surface and notes
  "documentation for the WebRTC echo cancellation library is
  difficult to find."
- **GStreamer's** `webrtcdsp` (`gst-plugins-bad`), Mumble, Linphone,
  Jitsi, Janus, Mediasoup: same pattern — only wrap the top-level
  config.
- **The single OSS project that exposes deep AEC3 config** is the
  Rust crate `tonarino/webrtc-audio-processing`, behind the
  `experimental-aec3-config` Cargo feature flag. The pattern: vendor
  the private aec3 headers, build `webrtc-audio-processing` bundled
  + static, expose a custom `EchoControlFactory` that constructs
  `EchoCanceller3` with a mutated config, pass through
  `AudioProcessingBuilder::SetEchoControlFactory`. The README
  explicitly disclaims semver — these private headers churn between
  WebRTC milestones. **This is the canonical reference if we ever
  go deep.**

### If we ever want the deep knobs: vendor v2.1 as a Meson subproject

**Anti-pattern (do not do):** vendoring private aec3 headers against
apt's `libwebrtc-audio-processing-1.so.3`. Vtable layouts of
`EchoCanceller3` and the surrounding classes are not ABI-stable
across Debian rebuilds — compiler version, abseil version, and
`-D_GLIBCXX_USE_CXX11_ABI` setting all matter. The `auto-abseil`
transition flagged on `tracker.debian.org/pkg/webrtc-audio-processing`
is exactly this risk. Header version skew is also acute (v1.3 was cut
from Chromium WebRTC ~M114; field names inside `EchoCanceller3Config`
are M-version-dependent). No widely-cited public recipe exists for
this pattern on Debian/Ubuntu — the closest thing (tonarino) deliberately
doesn't link against the system .so for this exact reason.

**Clean path:** mirror PipeWire 1.4.x's pattern. Vendor
`webrtc-audio-processing` v2.1 from upstream as a Meson subproject:

```
subprojects/webrtc-audio-processing.wrap
  [wrap-git]
  url = https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing.git
  revision = v2.1
  [provide]
  dependency_names = webrtc-audio-processing-2
```

Build flags `-Dc_args=-fPIC -Dcpp_args=-fPIC -Ddefault_library=static`
(plus `-march=armv8.2-a+crypto -mtune=cortex-a76` for NEON on Pi 5).
Static archive ~8-12 MB, RPi5 builds in 3-5 minutes. Bridge links
statically; we own both sides of the ABI boundary, CI-reproducible
across Trixie point releases. Reference implementations to crib from:

- `tonarino/webrtc-audio-processing` —
  `webrtc-audio-processing-sys/src/wrapper.cpp` and `experimental.rs`
  for the `SetEchoControlFactory` + `EchoCanceller3` construction
  pattern.
- PipeWire 1.4's `subprojects/webrtc-audio-processing.wrap` for the
  Meson wrap file shape.

Bring-up: ~1-3 days. Per-upgrade maintenance: low (pin to upstream
tag, bump deliberately).

**Don't wait for Trixie to ship `libwebrtc-audio-processing-2`.** The
Debian package tracker note dated 2025-11-26 says "A new upstream
version 2.1 is available, you should consider packaging it" but no
v2.x package exists in trixie-backports, sid, or experimental. v2.x
is shipping in Arch, Alpine, FreeBSD, and is bundled by
PipeWire 1.2+ — but Trixie stable won't see it in its lifetime.
Forky timeline at earliest.

### Updated staged options if AEC3 isn't enough

Run in roughly this cost-ordered sequence; stop early if any stage
passes the acceptance test. (Supersedes the pre-AEC3 list near the
top of this section.)

1. **Run the wake-word acceptance test.** Haven't done it. If
   detection rate ≥ 80% at 75 dB SPL music with current bridge
   config, no further AEC work needed. ~½ day.
2. **Drift / reference-tap diagnosis** (per the Caveats section
   above). ERLE decay over 10 min indicates clock-domain drift; the
   `jasper_capture` tap is PRE-CamillaDSP while the speaker is POST,
   so a divergence-fix is moving the dsnoop to a post-camilla
   slave. ~1-2 days each.
3. **Vendor v2.1 + custom `EchoCanceller3Config`** (per "Clean
   path" above). ~1-3 days. Bounded upside (a few extra dB).
   Suggested config to start from per the cross-reference research:
   `filter.refined.length_blocks=30`, `ep_strength.bounded_erl=true`,
   `suppressor.use_subband_nearend_detection=true`,
   `suppressor.dominant_nearend_detection.snr_threshold=20`.
4. **Neural residual stage.** `breizhn/DTLN-aec` (Interspeech 2021,
   MIT-licensed, TFLite, <4 ms/frame on Pi 3B+) is the most-cited
   option; `SaneBow/PiDTLN` and `rolyantrauts/PiDTLN2` are working
   RPi integrations. 256-unit quantized model is the RPi5 sweet
   spot. Alternatives: GTCRN-AEC (ICASSP 2024, smaller), Ultra
   Dual-Path Compression. Pipeline: AEC3 → neural residual → wake
   word. ~2-5 days.
5. **Custom-train "Hey Jarvis" with music/echo augmentation.**
   dscripka's openWakeWord training notebook explicitly supports
   mixing positive samples with realistic background music + room-
   impulse-response convolution. With the AEC3-residual-shaped
   noise distribution as the augmentation distribution, false-
   reject rate at −15 dB SNR drops substantially. The
   cross-reference research flags this as the lever commercial
   smart speakers actually ship. Highest engineering cost but also
   highest upside on the user-facing metric — if attenuation is in
   the right range and detection still misses, retraining the
   model to expect the residual is more transformative than
   squeezing another 3 dB out of the canceler. ~1 week.
6. **Push-to-talk fallback** for residual cases. Already implemented
   on the dial (long-press) and AMOLED satellite (in progress). ~30
   LoC if extending to other surfaces.

### What not to do (recorded so future sessions don't re-investigate)

External recommendations that look reasonable but are wrong for our
build, with reasoning so we don't keep re-litigating:

- **Don't use the XVF3800's "processed left channel" expecting
  25-40 dB hardware AEC.** External writeups (and ad-hoc research
  reports) recommend this — the claim is accurate for the chip's
  intended topology (chip's own codec drives the speaker, as in
  HA Voice PE / Seeed reference designs) but **not for ours.** The
  architectural mismatch is documented at length above (XMOS User
  Guide §3.5, §4.2.1, the `AEC_FAR_EXTGAIN` auto-mirror). Measured
  ≤−2 dB at every config tested. Already a feedback-memory rule;
  the rule stands.
- **Don't pivot to PipeWire `module-echo-cancel`.** Doesn't expose
  the deep AEC3 knobs (only the top-level `AudioProcessing::Config`
  surface) and adds an audio server to the dependency graph plus
  shairport-sync/librespot integration churn.
- **Don't wait for Trixie to ship `libwebrtc-audio-processing-2`.**
  Won't happen in Trixie's lifetime per the Debian package tracker.
- **Don't vendor private AEC3 headers against apt's `1.3-3.so`.**
  ABI fragility per the anti-pattern note above.
- **Don't pursue the field-trial mechanism** (e.g.
  `field_trial::IsEnabled("WebRTC-Aec3ShortHeadroomKillSwitch")`).
  Symbols are exported in the .so but `field_trial.h` is private,
  the registry only flips ~a dozen named killswitches (not the
  deep config struct), and you'd be vendoring private headers
  anyway with worse ergonomics than the v2.1 path.

---

## Sources we relied on

- XMOS XVF3800 v3.2.1 User Guide (the binding reference for chip
  behavior — particularly §3.5 audio pipeline, §4.2 tuning
  parameters)
- XMOS XVF3800 v3.2.1 Programming Guide (control protocol,
  parameter table)
- `respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY` GitHub repo
  (firmware binaries, `xvf_host.py`, host control README, issues
  #6 and #8 for documented bugs)
- `xiongyihui/speexdsp-python` (Python bindings for SpeexDSP)
- `voice-engine/ec` (reference implementation of the
  bridge-shaped SpeexDSP integration)
- `SaneBow/alsa-aec` and `koniu/sysrecord` (reference asoundrc
  patterns for `multi` + dsnoop fan-out)
- ALSA project Module-aloop documentation (substream and rate
  semantics)
- Stuart Naylor's writeups on the HA / Rhasspy / OVOS forums on
  software AEC limitations and SpeexDSP-vs-WebRTC tradeoffs
- HA Voice PE community forum threads on XU316 AEC behavior
  (closest neighbor; same chip family)
