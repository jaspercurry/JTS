# Acoustic Echo Cancellation — investigation, design, and current state

This document describes the AEC subsystem in detail: why it exists,
what we tried, what failed, what shipped, and what's still open. It
is the canonical source for anyone touching `jasper-aec-bridge`,
`jasper-aec-init`, the `pcm.jasper_capture` dsnoop, the snd-aloop
two-card setup, the `jasper/xvf/` vendored XMOS control library, or
any of the supporting documentation in `BRINGUP.md` and
`docs/audit-pending-followups.md`.

The goal is to make this enough context that a future session can
pick up the work without re-doing the investigation.

---

## TL;DR / current state

**The software AEC bridge is built but DISABLED by default.** It's
shipped as installed-but-not-enabled because measured attenuation
(−2 to −8 dB) is modest and RAM cost (~110 MB) is significant on
the 1GB Pi 5. The code is preserved so it can be flipped on for
A/B testing; CLAUDE.md has the toggle commands. The chip's
on-board AEC is not in the audio path either — this doc explains
why.

**To turn the bridge on**: see CLAUDE.md "Acoustic echo
cancellation (software AEC bridge)" section. Two-line operation:
flip `JASPER_MIC_DEVICE` from `Array` to `hw:5,1` in
`/etc/jasper/jasper.env`, then
`systemctl enable --now jasper-aec-init jasper-aec-bridge` and
restart `jasper-voice`.

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
| Raspberry Pi 5 (1GB or 2GB) | Host running moOde audio + jasper daemons |
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

The chip exposes two firmware variants over USB:

- **2-channel firmware** (`v2.0.6` shipped on this board, also
  `v2.0.5`/`v2.0.7`): USB capture has 2 channels — channel 0 is
  "conference" (post-AEC + BF + NS + AGC), channel 1 is "ASR"
  (different post-processing tuned for speech recognition). No
  raw mic access.
- **6-channel firmware** (`v2.0.8`, `_6chl_` variant): adds raw
  mics on USB capture channels 2–5. The processed
  conference/ASR channels stay on 0/1.

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
moOde renderers (MPD, shairport-sync, librespot, bluealsa)
    │
    │  via /etc/alsa/conf.d/zz-jts-loopback.conf
    │  (rewrites pcm._audioout to point at Loopback,0,sub0)
    ▼
hw:Loopback,0,sub0  ← snd-aloop card 0, kernel-clocked
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
    │       master_gain ducking + flat passthrough
    │       writes to → pcm.jasper_out (dmix on Apple dongle)
    │       → speaker (audible path)
    │
    └──► reader B: jasper-aec-bridge (Python, alsaaudio + SpeexDSP)
            captures jasper_capture (48k stereo) for FAR-END REFERENCE
            captures hw:Array,0 (XVF, 16k 6ch) for NEAR-END MIC
            takes channel 2 of the chip capture (raw mic 0)
            downsamples ref 48k → 16k mono on left
            runs SpeexDSP EchoCanceller frame by frame
            writes AEC'd mono 16k to → hw:LoopbackAEC,0
                                          │
                                          ▼ (snd-aloop card 5)
                                       hw:LoopbackAEC,1 (= hw:5,1)
                                          │
                                          ▼
                                       jasper-voice
                                          openWakeWord + Gemini Live
```

Two snd-aloop cards. Card 0 ("Loopback") carries the music chain
(moOde → camilla → dongle, with the bridge tapping the camilla
input via dsnoop). Card 5 ("LoopbackAEC") carries only the AEC'd
mono mic from the bridge to jasper-voice. Two cards instead of
multiple substreams of one card because PortAudio (sounddevice's
backend, which jasper-voice uses) doesn't expose ALSA substream
selection — it addresses cards by name and would default to
sub0, colliding with the music chain.

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

### Why SpeexDSP

Three options were considered for the software AEC implementation:

- **WebRTC AEC3** (Google, used by PipeWire's `module-echo-cancel`
  and Chrome). High quality. But the canonical integration path
  is via PipeWire, which we can't run alongside moOde without
  significant restructuring. Standalone ALSA-only WebRTC AEC
  packages don't really exist; we'd be writing or porting one.
- **SpeexDSP** (xiph). Mature, packaged in Debian
  (`libspeexdsp-dev`), small. Stuart Naylor's writeups
  (the most-cited voice on Pi-AEC) report SpeexDSP holds up at
  higher speaker SPL than WebRTC AEC. The `voice-engine/ec`
  project is an existing reference implementation that uses
  SpeexDSP in exactly the bridge pattern we want.
- **Neural AEC (DeepVQE-S, EchoFree, etc.)**. Best quality on
  AEC-Challenge benchmarks. But no production-ready
  ALSA-bridge-shaped integration; we'd be writing a real-time
  Python or C++ pipeline from scratch. Defer until SpeexDSP
  proves insufficient.

Picked SpeexDSP. The Python bindings are
`xiongyihui/speexdsp-python` (small SWIG wrapper around the C
library), with a known packaging quirk on Python 3.13 — the
`__init__.py` references a SWIG-generated wrapper file that
isn't actually built; we patch `__init__.py` post-install to
import the SWIG extension module directly.

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

The 6-channel firmware (`v2.0.8`, single DFU command to flash,
fully reversible) adds raw mics on channels 2–5. Software AEC
on raw mic 0 sees a clean linear input — much better convergence.

DFU procedure:
```
sudo dfu-util -R -e -a 1 \
    -D respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin
```

The chip's `SAVE_CONFIGURATION` op had a brick hazard on firmware
2.0.6 (respeaker repo issue #8); we never call it regardless of
firmware version.

---

## Hardware vs software AEC — comparison summary

| Dimension | XVF3800 hardware AEC | SpeexDSP software AEC (current) |
|---|---|---|
| **Topology fit for our setup** | Designed for chip-driven speaker; doesn't work with external DAC | Topology-agnostic — bridge can capture any reference and any mic |
| **Effectiveness in our setup** | ≤2 dB sustained attenuation (measured) | −2 to −8 dB (measured); held convergence requires sustained signal |
| **Host CPU cost** | ~0% (chip handles it) | ~3% of one A76 core |
| **Host RAM cost** | ~0 MB | ~110 MB RSS (Python + numpy + scipy + speexdsp) |
| **Latency** | <1 ms (chip-internal) | ~20 ms (frame size) + queue jitter |
| **Beamforming, NS, AGC, DoA** | Included, professional-grade | Not included; would need separate processing |
| **Configurability** | Closed binary, ~30 documented parameters | Source-available, fully tunable |
| **Drift handling** | Internal (chip is single clock domain) | Two-clock-domain capture causes perpetual re-adaptation |
| **Convergence** | Stable when working | Holds during sustained signal, decays when far-end goes silent |
| **Worst-case (loud music + soft voice + far-field)** | Designed to handle this | Marginal — limited by SpeexDSP's algorithm |

The honest summary: hardware AEC would be much better for our use
case if it worked, but it doesn't work in our topology. Software
AEC is what we have because nothing else is available to us
without significant additional engineering (custom firmware,
mic/speaker swap, hardware redesign).

---

## Current measured performance

Setup: controlled log sweep (200–3400 Hz, 5% FS, 30 sec) played
through `_audioout` (the moOde-hijacked path that flows through
the Loopback chain), with `jasper-aec-bridge` instrumentation
logging per-frame RMS for raw mic, reference, and AEC output.

| Time | Reference RMS | Raw mic RMS | AEC out RMS | Attenuation |
|---|---|---|---|---|
| 5 sec | 523 | 279 | 219 | −2.1 dB |
| 10 sec | 818 | 353 | 309 | −1.1 dB |
| 15 sec | 818 | 323 | 270 | −1.6 dB |
| 20 sec | 818 | 452 | 418 | −0.7 dB |
| 25 sec | 817 | 689 | 472 | −3.3 dB |
| **30 sec** | **818** | **673** | **247** | **−8.7 dB** |

Convergence trajectory: SpeexDSP's adaptive filter takes 20–30
seconds of sustained reference signal to learn the room transfer
function. Once converged, it produces ~−8 dB of attenuation at
peak. After a silent gap, the filter coefficients decay back
toward neutral; the next adaptation cycle re-converges.

This is meaningfully better than the chip's ≤2 dB but
substantially worse than the −20 dB that hardware AEC delivers
in topologies it's designed for.

### Resource cost (measured on Pi 5 1GB)

```
jasper-aec-bridge:  3.3% of one CPU core,  110 MB RSS
jasper-camilla:     0.5%,                    8 MB RSS
jasper-voice:      11.3%,                  265 MB RSS
                  ----                     -----
                   ~15% of one core        ~380 MB total
```

Relative to baseline (Pi 5 idle with moOde = ~50% RAM used), the
bridge adds ~110 MB which puts the 1GB Pi 5 at ~60% memory usage
and ~160 MB into swap. The 2GB Pi 5 (which BRINGUP.md and PLAN.md
have always recommended as the v1 target) has comfortable
headroom.

---

## Caveats and open issues

### Convergence doesn't hold

Currently the AEC re-adapts when reference reappears after a
silent gap. SpeexDSP's NLMS adaptive filter naturally relaxes
toward zero coefficients during silence. In a real "music
playing → user speaks → music ducks → user finishes → music
restores" cycle, the AEC may need a few seconds of sustained
music to re-converge. Probably acceptable in practice — the
critical thing is that AEC works **during** music playback,
which it does once converged.

### Cross-clock-domain drift between reference and mic

The reference is captured from the snd-aloop loopback (kernel
timer-driven), and the mic is captured from the XVF chip (USB
UAC2 SYNC-clocked). These are independent clocks that drift by
~tens of ppm relative to each other. Over time, the AEC's
filter alignment slides and effectiveness degrades. SpeexDSP
auto-adapts but the perpetual re-tracking limits peak
effectiveness.

The classical fix is to add async resampling on one leg to lock
both to the same clock (e.g. resample the mic to match the
reference clock, or vice versa). We haven't implemented this.
The bridge's drift-compensation is currently "let SpeexDSP
re-adapt every cycle."

### Modest peak attenuation

−8 dB peak is functional but not transformative. For comparison,
HA Voice PE in its native topology reportedly delivers −15 to
−25 dB. Three avenues to push higher:

1. **Tune SpeexDSP**: longer filter (currently 200 ms tail —
   matches chip's native 192 ms), more aggressive adaptation
   step. Quick to test.
2. **Add a nonlinear residual suppressor** (Speex's
   `EchoSuppress`, or a separate post-filter). Helps with
   speaker non-linearity at high SPL. Moderate effort.
3. **Drift compensation** as described above. Significant
   engineering.

### Bridge is Python (RAM-heavy)

The 110 MB RSS for the bridge is mostly Python interpreter +
numpy + scipy + sounddevice + speexdsp libraries. The bridge
logic itself is tiny. On the 1GB Pi 5 this is a noticeable
fraction; on the 2GB Pi 5 it's fine.

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

## What we'd try if SpeexDSP isn't enough

In rough order of effort:

1. **Tune SpeexDSP parameters** — filter length, adaptation
   constants. Hours of effort.
2. **Add nonlinear residual suppressor** post-AEC. Day or two
   of work.
3. **Drift compensation between ref and mic clocks**. Several
   days. Could do this in CamillaDSP by routing the mic through
   a second CamillaDSP instance with `enable_rate_adjust:
   true` to lock its clock to the snd-aloop, then read from the
   resulting loopback — this shape was used successfully for
   the chip-side bridge before we abandoned that path.
4. **Try DeepVQE or another neural AEC** as a drop-in
   replacement for SpeexDSP in the bridge. Quality upside is
   significant but engineering cost is real (real-time Python
   ML pipeline on Pi 5 ARM).
5. **Push-to-talk fallback** for the cases AEC can't handle.
   Cheap insurance, ~30 LoC.
6. **Wyoming satellite pattern** — physical mic-speaker
   separation. Architecturally elegant but means a hardware
   addition (an ESP32 satellite or HA Voice PE elsewhere in the
   room). Ducks the AEC problem entirely by separating the
   two transducers in space.

---

## File map

Files involved in the AEC subsystem:

- `jasper/cli/aec_bridge.py` — the SpeexDSP software AEC daemon
- `jasper/cli/aec_init.py` — boot-time chip init (resets chip,
  sets UAC2 PCM to unity)
- `jasper/cli/aec_tune.py` — calibrator for chip-side
  `AUDIO_MGR_SYS_DELAY` (vestigial; kept for diagnostic use)
- `jasper/xvf/xvf_host.py` — vendored from
  respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY
- `jasper/cli/doctor.py` — `check_aec_bridge_running`,
  `check_aec_output_card`, `check_xvf_firmware_6ch`
- `deploy/alsa/asoundrc.jasper` — defines `pcm.jasper_capture`
  (the dsnoop tap) and `pcm.jasper_out` (dongle dmix)
- `deploy/modprobe.d/snd-aloop.conf` — two-card snd-aloop
  config (`index=0,5 id=Loopback,LoopbackAEC`)
- `deploy/modules-load.d/snd-aloop.conf` — auto-load at boot
- `deploy/systemd/jasper-aec-bridge.service` — runs
  `jasper-aec-bridge` Python daemon
- `deploy/systemd/jasper-aec-init.service` — oneshot at boot
- `deploy/install.sh` — installs all of the above, fetches
  speexdsp-python from git, patches its broken `__init__.py`,
  installs swig + libspeexdsp-dev + dfu-util
- `pyproject.toml` — registers `jasper-aec-bridge`,
  `jasper-aec-init`, `jasper-aec-tune` console scripts; adds
  `pyusb`, `libusb_package`, `pyalsaaudio` deps
- `.env.example` — `JASPER_MIC_DEVICE=hw:5,1` (the LoopbackAEC
  capture-side substream)

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
