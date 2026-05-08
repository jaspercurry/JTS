# Audio paths and software volume knobs

The JTS speaker has **two distinct audio paths** to the dongle. They look
similar at the bottom (both end at `pcm.jasper_out`) but they're
processed very differently. Knowing which path you're on matters
whenever you're tuning volume, testing the chain, or debugging "why is
this so loud / so quiet."

## The two paths

```
                          MUSIC CHAIN (CamillaDSP processes here)
                          ───────────────────────────────────────
  renderers (librespot, shairport-sync, bluealsa-aplay)
      │
      ▼
  hw:Loopback,0,0          (snd-aloop playback side)
      │
      │  loop
      ▼
  plughw:Loopback,1,0      (snd-aloop capture side)
      │
      ▼
  jasper-camilla (CamillaDSP, port 1234)
      ├─ main_volume                  ← canonical software volume
      ├─ master_gain mixer            ← ducking on wake
      └─ filters, EQ, etc.
      │
      ▼ writes to dmix below
                      ┌─────────────────────────────────────────┐
                      │                                         │
                      ▼                                         │
                  pcm.jasper_out (dmix on Apple USB-C dongle)   │
                      ▲                                         │
                      │ writes to dmix above                    │
                      │                                         │
                          TTS / TEST-TONE CHAIN                 │
                          (BYPASSES CamillaDSP)                 │
                          ─────────────────────                 │
  jasper-voice TtsPlayout ─────────────────────────────────────┘
      │  Gemini-generated PCM, JASPER_TTS_GAIN_DB applied at the source

                      pcm.jasper_out
                              │
                              ▼
                      Apple USB-C dongle (Headphone pinned 100%)
                              │
                              ▼
                      TPA3255 amp (physical gain knob)
                              │
                              ▼
                              speakers
```

## What `pcm.jasper_out` actually is

`pcm.jasper_out` is a **dmix instance on the dongle hardware**, not a
process. dmix is the ALSA primitive for letting multiple writers share
one output device — every writer's stream is mixed sample-wise and sent
to the slave (`hw:CARD=A,DEV=0`).

Two writers share `jasper_out`:

1. **CamillaDSP** writes to it from the music chain (post-processing).
2. **jasper-voice TtsPlayout** writes to it for spoken responses (pre-
   processing, except for `JASPER_TTS_GAIN_DB` applied at the source).

Both streams hit dmix at the same dongle clock. CamillaDSP's processing
is upstream of dmix only for path 1.

## The volume knobs and which path they affect

| Knob | Where it lives | Music path | TTS / `aplay` to `jasper_out` |
|------|----------------|------------|-------------------------------|
| CamillaDSP `main_volume` | DSP chain (port 1234) | ✅ attenuates | ❌ no effect |
| CamillaDSP `master_gain` mixer | DSP chain (port 1234, used for ducking) | ✅ attenuates | ❌ no effect |
| Source amplitude (PCM data) | The WAV / sounddevice buffer | ✅ attenuates | ✅ attenuates |
| `JASPER_TTS_GAIN_DB` | TtsPlayout source-side gain | n/a (not on music) | ✅ attenuates (TTS only) |
| Apple dongle "Headphone" | Hardware mixer on the dongle | ✅ but **pinned at 100%** | ✅ but **pinned at 100%** |
| TPA3255 amp gain | Physical knob on the amp | ✅ | ✅ |

**Architectural rules** (don't violate, don't propose to violate):

- The dongle Headphone is the **fixed analog ceiling**, pinned at 100%
  by `jasper-dac-init`, watched by `jasper-headphone-monitor`, checked
  by `jasper-doctor`. Software never moves it.
- The amp gain is a physical knob set by the operator at install time.
  Software has no access.
- `main_volume` is the canonical software volume. The dial, voice
  tools, and the volume coordinator all converge on it.

## Operational consequences

### When you want to play a test tone at a controlled level

**Use the music chain** (so `main_volume` applies):

```sh
# Generate /tmp/tone.wav (440 Hz, 0 dBFS, 300 ms, stereo, 48 kHz S16_LE)
sudo /opt/jasper/.venv/bin/python <<'PY'
import numpy as np, wave
fs, dur, freq = 48000, 0.3, 440
N = int(fs * dur); t = np.arange(N) / fs
sine = np.sin(2 * np.pi * freq * t)
fade_n = int(0.010 * fs)
window = np.ones(N)
window[:fade_n] = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade_n) / fade_n)
window[-fade_n:] = window[:fade_n][::-1]
samples = (sine * window * 32767).astype(np.int16)
stereo = np.column_stack([samples, samples]).flatten()
with wave.open("/tmp/tone.wav", "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(fs)
    w.writeframes(stereo.tobytes())
PY

# Lower main_volume first (the dial range floor is around -50 dB / 0%)
curl -s -X POST -H "Content-Type: application/json" \
    -d '{"db": -40.0}' http://localhost:8780/volume/set

# THEN play through the music chain (gets CamillaDSP processing)
sudo aplay -D plughw:Loopback,0,0 /tmp/tone.wav

# Restore main_volume
curl -s -X POST -H "Content-Type: application/json" \
    -d '{"db": 0.0}' http://localhost:8780/volume/set
```

**Do not** play test tones via `aplay -D plug:jasper_out` if you've
lowered `main_volume` and expect attenuation — that target bypasses
CamillaDSP and the tone will play at full source amplitude.

### When you're testing TTS specifically

`aplay -D plug:jasper_out` is correct — that's the TTS path. Source
amplitude (the WAV) and `JASPER_TTS_GAIN_DB` are your only software
attenuators.

### When the AEC bridge is involved

The bridge taps `pcm.jasper_capture` which is a **dsnoop on
`hw:Loopback,1,0`** — that's the **music chain reference, BEFORE
CamillaDSP processing.** Implications:

- TTS doesn't show up in the AEC reference. The bridge can't cancel
  TTS bleed through the mic, only music bleed.
- When CamillaDSP ducks music (master_gain ↓), the AEC reference still
  shows pre-duck music. AEC3's adaptive filter handles modest gain
  errors, but a sudden 25 dB ducking step is a transient the filter has
  to re-converge through. Currently acceptable; if it becomes a
  problem, the fix is to move the dsnoop tap downstream of CamillaDSP
  (architectural change documented as Stage 3 in the AEC plan).

## Why TTS bypasses CamillaDSP

A historical / design choice: `jasper-voice` needs to be heard during
voice sessions when the music chain is ducked. If TTS went through the
same `master_gain` mixer that ducks music, ducking would also duck the
TTS — the user wouldn't hear the response. Routing TTS post-CamillaDSP
keeps it independent.

The downside is the operational confusion this doc is for. Worth it on
balance, given the alternative is a more complex CamillaDSP pipeline
(per-input mixers).

## Related docs

- [README.md § Architecture](../README.md) — high-level diagram (the
  same one this doc breaks down).
- [docs/HANDOFF-volume.md](HANDOFF-volume.md) — the source-aware
  VolumeCoordinator that decides whether `main_volume`, an AirPlay
  slider, a Spotify slider, or an MPD slider receives a given volume
  command.
- [docs/HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md)
  — voice tool transport (play / pause / skip) routing across renderers.
- [docs/HANDOFF-aec.md](HANDOFF-aec.md) — why the AEC bridge taps
  `jasper_capture` (pre-CamillaDSP music) and not `jasper_out`.
