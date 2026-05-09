# Audio paths and software volume knobs

Two paths to the dongle, processed differently. Knowing which is which
matters when you're testing volume-controlled output and when you're
trying to understand the loudness-tracking compensation in
`jasper-voice`.

## How we got here

A smart speaker plays music AND voice prompts. They want different
processing: music tolerates latency and benefits from EQ / room
correction; voice prompts need to be heard above music and shouldn't
be re-processed by an EQ tuned for music. This is the standard
"two-bus" pattern (HA Voice PE, OVOS, Alexa AVS Dialog/Content,
broadcast PA hardware).

The Linux-on-a-single-Pi version of this pattern has one constraint:
**CamillaDSP supports only one ALSA capture device per process.**
Combining music and TTS into one DSP pipeline would require either
pre-mixing them upstream (which would mean ducking music ducks TTS
too — wrong) or the fragile ALSA `multi` plugin (xrun storms with
bursty writers). So we route TTS around CamillaDSP into the dongle's
dmix instead, and compensate for the bypass in software (see
"TtsVolumeTracker" below).

## The two paths

```
MUSIC chain (gets CamillaDSP processing)
    renderers → hw:Loopback,0,0 → snd-aloop → plughw:Loopback,1,0
              → jasper-camilla (main_volume + filters)
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers

TTS / TEST-TONE chain (BYPASSES CamillaDSP)
    jasper-voice TtsPlayout → pcm.jasper_out (dmix on dongle)
                            → dongle → amp → speakers
```

Both legs converge at `pcm.jasper_out`, a dmix on the dongle. dmix
sums the two writers' streams sample-wise and sends one stream to the
DAC. CamillaDSP is upstream of dmix only on the music leg.

## Volume knobs and which path each affects

| Knob | Where it lives | Music | TTS / `aplay -D jasper_out` |
|------|----------------|-------|----------------------------|
| CamillaDSP `main_volume` (the ducker) | DSP, websocket port 1234 | yes | no |
| Source slider (iPhone, Spotify Connect, BT phone) | Renderer-side, before Loopback | yes | no |
| Source amplitude (PCM data) | The WAV / sounddevice buffer | yes | yes |
| `JASPER_TTS_GAIN_DB` | TtsPlayout source-side | n/a | yes |
| `TtsVolumeTracker` (auto) | TtsPlayout source-side | n/a | yes — auto-tracks music |
| Apple dongle Headphone | Hardware mixer | (pinned 100%) | (pinned 100%) |
| TPA3255 amp | Physical knob | yes | yes |

Two notes:
- `master_gain` is a CamillaDSP mixer named in `v1.yml` but currently
  configured as identity. The Ducker operates on `main_volume`, not
  `master_gain`. Old comments/docs that called master_gain "the
  ducking knob" are wrong.
- `listening_level` is the canonical user-facing volume in the
  VolumeCoordinator (see [HANDOFF-volume.md](HANDOFF-volume.md)). It
  maps to `main_volume` for IDLE and AirPlay; for Spotify and BT,
  `main_volume` stays pinned at 0 dB and the source slider carries
  `listening_level`.

## Why TTS still tracks user volume changes

Since TTS bypasses CamillaDSP, naively it would always play at fixed
amplitude regardless of how the user set volume. To preserve the
property "however the user adjusted volume — iPhone slider, AirPlay,
Spotify, the dial — TTS matches," `TtsVolumeTracker` in
[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) measures
CamillaDSP's `playback_rms` (the actual signal hitting the DAC, after
every upstream attenuator) and scales TTS to sit a configurable
headroom above it. A "loudness anchor" persists across boots so a
quiet bedroom from yesterday is still quiet today until someone
changes it.

This compensation is load-bearing — it's what makes the bypass invisible
to the user. Don't remove it without first removing the bypass.

## Operational notes

**Test the music chain** (volume-controlled): `aplay -D plughw:Loopback,0,0 file.wav`.
Goes through CamillaDSP, so `main_volume` applies.

**Test the TTS chain**: `aplay -D plug:jasper_out file.wav`. Bypasses
CamillaDSP. Source amplitude is the only software attenuator —
`main_volume` does nothing to this path.

The Apple dongle Headphone is pinned at 100% by `jasper-dac-init`,
watched by `jasper-headphone-monitor`, checked by `jasper-doctor`.
Software never touches it. The amp gain is a physical knob set at
install time.

## AEC bridge implications

The bridge taps `pcm.jasper_capture`, a dsnoop on `hw:Loopback,1,0` —
the music chain reference, BEFORE CamillaDSP processing. So:

- TTS bleed through the mic isn't in the AEC reference; the bridge
  cancels music bleed only.
- A 25 dB ducking step is a transient the AEC's adaptive filter has
  to re-converge through. Acceptable today; if it becomes a problem,
  move the dsnoop tap downstream of CamillaDSP.

## Related

- [HANDOFF-volume.md](HANDOFF-volume.md) — VolumeCoordinator and
  source-aware dispatch.
- [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md) —
  voice tool transport routing.
- [HANDOFF-aec.md](HANDOFF-aec.md) — why the AEC bridge taps pre-DSP.
