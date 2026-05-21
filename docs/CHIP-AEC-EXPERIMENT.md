# Chip-AEC experiment — conclusive test plan

**Branch:** `chip-aec-experiment` (not for merge to main as-is — this is
exploratory infrastructure).

**Goal:** Definitively answer whether the XVF3800's hardware AEC can
produce useful echo cancellation in JTS's external-DAC topology when we
feed music to the chip's USB-IN as the AEC reference signal. The 2025
investigation that concluded "≤2 dB attenuation, won't converge" was
done in the dongle topology with no USB-IN reference at all — the chip
was running blind. The 2026-05-19 `SHF_BYPASS=0` wake-rate test (15%)
also ran without USB-IN reference. **Neither result applies to the
topology being tested here.** See [HANDOFF-aec.md option D](HANDOFF-aec.md)
for the deeper rationale.

**Decisive signal:** `AEC_AECCONVERGED` chip parameter. If the chip's
adaptive filter converges (flag flips to 1) during steady-state music
playback, the topology works. If it doesn't converge after sensible
`AUDIO_MGR_SYS_DELAY` tuning, chip-AEC is dead in this topology and the
question is closed.

---

## Topology

```
renderers (shairport-sync, librespot, bluealsa-aplay)
    │
    │ each writes to hw:Loopback,0,0
    ▼
snd-aloop kernel    hw:Loopback,0,sub0 ─cross-wired→ hw:Loopback,1,sub0
                                                          │
                                                          ▼
                                                  pcm.jasper_capture (dsnoop)
                                                          │
                                            ┌─────────────┼────────────────────┐
                                            ▼             ▼                    ▼
                                   jasper-camilla   chip-aec-experiment    (others)
                                            │             │
                                            ▼             ▼
                                     pcm.jasper_out   hw:CARD=Array,DEV=0  (chip USB-IN, 16k stereo)
                                            │             │
                                            ▼             ▼
                                     Apple dongle      XVF3800 chip
                                            │              │ (AEC processes ref vs mic internally)
                                            ▼              ▼
                                     ─────── speaker ────  6-ch mic capture
                                            ▲              │  ch1 = AEC'd ASR beam
                                            │              ▼
                                       airborne     udp://127.0.0.1:9876  ← pumped by chip-aec-experiment
                                                          │
                                                          ▼
                                                    jasper-voice
```

Key differences from production:
- WebRTC AEC bridge is stopped + masked
- `SHF_BYPASS = 0` (chip AEC engaged on ch0/ch1)
- New daemon `jasper.chip_aec_experiment` does two things in parallel:
  - **Reference feeder**: reads `plug:jasper_capture` (pre-CamillaDSP
    music tap), mixes L+R to mono, duplicates to stereo, writes to
    `hw:CARD=Array,DEV=0` at 16 kHz S16_LE (the only rate/format the
    chip's USB-IN endpoint advertises — verified empirically and via
    XMOS docs, see HANDOFF-xvf3800.md §1)
  - **UDP mic pump**: reads chip's 6-ch mic capture, extracts ch1,
    sends to `udp://127.0.0.1:9876` (same UDP port the WebRTC bridge
    normally writes to → no `jasper-voice` changes)

---

## Files added on this branch

| Path | Purpose |
|---|---|
| `jasper/chip_aec_experiment.py` | The daemon (feeder + UDP pump) |
| `scripts/chip-aec-setup.sh` | Phase 1: deploy + start daemon |
| `scripts/chip-aec-teardown.sh` | Revert everything to production state |
| `scripts/chip-aec-poll-convergence.sh` | Phase 3: poll `AEC_AECCONVERGED` |
| `scripts/chip-aec-capture-comparison.sh` | Phase 4: 4-file ear test |
| `docs/CHIP-AEC-EXPERIMENT.md` | This file |

Nothing else in the repo is modified by `chip-aec-setup.sh` at runtime
beyond:
- Chip params (`SHF_BYPASS`, `AUDIO_MGR_SYS_DELAY`) — reverted by teardown
- `jasper-aec-bridge.service` masked — unmasked by teardown
- `/var/log/chip-aec-experiment.log` written

Production CamillaDSP config, reconciler, voice daemon, wake-word, mic
mute, etc. all untouched.

---

## How to run

```sh
# 1. Switch to this branch on the laptop
git checkout chip-aec-experiment

# 2. Set up (rsync code, stop bridge, start experiment daemon, set chip params)
bash scripts/chip-aec-setup.sh

# 3. Start music playing through the speaker at production volume.
#    AirPlay or Spotify or BT — any source. Let it play continuously.

# 4. Verify the chip AEC converges within ~60 s
bash scripts/chip-aec-poll-convergence.sh
# Pass criterion: AEC_AECCONVERGED flips to [1]
# If it doesn't flip after sensible SYS_DELAY sweep, conclusive negative.

# 5. Capture the 4 audio files for ear-test A/B
bash scripts/chip-aec-capture-comparison.sh
# Walk through the prompts (start music, mute music, speak, etc.)
# Files land under captures/chip-aec-experiment/<timestamp>/

# 6. Listen with your ears. The ear test is the truth test.
#    Recommended: open all 4 .wav files in Audacity side-by-side, or use
#    sox -m to subtract two recordings and listen to the residual.

# 7. Revert to production (WebRTC AEC bridge)
bash scripts/chip-aec-teardown.sh
```

---

## What to listen for

| File | What it is | What should it sound like |
|---|---|---|
| `01_reference.wav` | The signal we feed to chip USB-IN | Like the music you played — confirms the feeder is working |
| `02_mic_aec_off.wav` | Chip ch1 with `SHF_BYPASS=1` | Music dominates, similar to current WebRTC bridge's *input* |
| `03_mic_aec_on.wav` | Chip ch1 with `SHF_BYPASS=0` | **Music substantially reduced** if chip AEC is working |
| `04_speech_only.wav` | Chip ch1, AEC on, no music | Your voice clearly, **not** muffled/robotic |

The decisive comparison is **02 vs 03**:
- 03 noticeably quieter on music than 02: ✅ chip AEC is doing useful work
- 03 sounds about the same as 02: ❌ chip AEC isn't cancelling

The sanity comparison is **04**:
- Voice sounds clear: ✅ chip AEC's residual stage isn't damaging speech
- Voice sounds muffled or robotic: ⚠️ chip's residual is over-aggressive
  (this is the same failure mode we saw with WebRTC AEC3 NS=moderate
  before the 2026-05-20 NS=low tuning — chip may have a similar knob)

---

## Decision tree

```
AEC_AECCONVERGED flips to 1?
├── YES → run ear test → 03 noticeably quieter than 02?
│         ├── YES → 04 voice quality OK?
│         │         ├── YES → ✅ chip-AEC works. Scope productionization
│         │         │         (Phase 5 wake-rate sweep, then CamillaDSP
│         │         │         multi-output, reconciler mode, 2-ch firmware
│         │         │         migration, jasper-doctor convergence check)
│         │         └── NO → flag in HANDOFF-aec.md as "chip AEC converges
│         │                  but residual stage damages speech — needs chip
│         │                  param tuning". Maybe AUDIO_MGR_AEC_RER_THRESHOLD
│         │                  or similar. Open question.
│         └── NO → 03 doesn't cancel meaningfully despite convergence flag.
│                  Surprising. Check ref signal level (REF_GAIN), maybe
│                  EXTGAIN mirroring is still sabotaging. Update HANDOFF
│                  with finding.
└── NO (after sensible SYS_DELAY sweep ±64 around the measured value)
         → CONCLUSIVE NEGATIVE. Update HANDOFF-aec.md option D as closed.
           The chip cannot handle the JTS topology even with USB-IN
           reference. WebRTC AEC3 bridge remains the production path.
```

---

## Known limitations of the experiment

1. **Pre-CamillaDSP reference tap.** The feeder reads `plug:jasper_capture`,
   which is pre-CamillaDSP (before `main_volume` ducking). So:
   - During steady-state music with no wake events: chip ref level ≡
     speaker level (up to constant gain). **Fine for the convergence
     test and the ear test.**
   - During wake events: speaker ducks but chip ref doesn't. Impulse
     response shifts. Chip AEC adapts but slower. **Avoid wake events
     during Phase 3/4 testing** — don't say "Hey Jarvis" until Phase 4
     step 3 (speech-only capture).
   - For productionization: would need to switch to a post-CamillaDSP
     tap. Options: CamillaDSP multi-output (verify version supports
     it), or a second snd-aloop card downstream of CamillaDSP. Phase 6
     work.

2. **TTS not in reference.** TTS bypasses CamillaDSP (writes directly to
   `pcm.jasper_out` dmix). Chip's AEC reference doesn't see TTS. This
   matches current production behavior (WebRTC bridge also doesn't see
   TTS). The 0.7 s wake refractory + `NO_INTERRUPTION` flag handle
   TTS-self-trigger. **Not a regression.**

3. **`AUDIO_MGR_SYS_DELAY` starts at 12** (Seeed default). If the
   measured chip-USB-IN → mic delay is materially different, convergence
   may need a sweep. The poll script's troubleshooting section walks
   through this.

4. **Reconciler interaction.** Setup masks `jasper-aec-bridge.service`
   so the reconciler can't restart it mid-experiment. Teardown unmasks
   and runs the reconciler to restore production. If someone runs
   `bash scripts/deploy-to-pi.sh` during the experiment, `install.sh`
   will re-enable the reconciler — re-run `chip-aec-setup.sh` after.

5. **Not testing wake-rate yet.** Phase 5 (forensic wake-rate
   methodology) is *not* automated in this branch. If Phase 3+4 are
   favorable, we'd run wake-rate as a separate pass using the existing
   methodology from `project_aec_wake_rate_forensic_methodology.md`.

---

## Empirical facts confirmed before the experiment (2026-05-21)

From `/proc/asound/Array/stream0` on the live Pi + cross-referenced
against XMOS docs + `docs/HANDOFF-xvf3800.md`:

- **Chip USB-IN endpoint:** 16 kHz S16_LE 2-channel — *only*. No
  build-time switch to 48 kHz on shipped firmware. The HANDOFF-aec.md
  option-D writeup that suggested "48 kHz to USB-IN is fine, chip
  transparently SRCs" was wrong; the runtime SRC the XMOS datasheet
  documents is for the I²S path, not USB. **This file overrides those
  lines.**
- **Chip mic capture:** 16 kHz S16_LE 6-channel on the 6-ch firmware
  variant currently flashed (`ua-io16-6ch-sqr` v2.0.8).
- **AEC reference channel:** Left (ch 0 of the 2-channel playback
  endpoint). Right is ignored by the chip's AEC. We duplicate L to R
  for endpoint cleanliness.
- **USB Adaptive Mode PLL:** The chip syncs MCLK to the Pi's USB SOF
  clock when USB-IN audio flows. Mic clock locks to that MCLK. So the
  mic and reference share a timebase — no cross-clock drift in this
  topology (this is the key reason option D might actually work where
  the dongle topology didn't).

---

## Source citations

- HANDOFF-aec.md "D — Chip-AEC with USB-in reference topology" (lines
  1015–1177) — the 2026-05-21 docs-review writeup. *Note:* the rate
  claim is wrong; the rest is correct.
- HANDOFF-aec.md "What we found about chip-side AEC in our topology"
  (lines 1603–1729) — original 2025 investigation, dongle topology
- HANDOFF-aec.md "Chip-pipeline-only alternative considered + rejected"
  (lines 505–534) — May 2026 wake-rate test, no USB-IN reference, not
  applicable to this experiment
- HANDOFF-xvf3800.md §1 (lines 60–62) — canonical USB endpoint table
- HANDOFF-xvf3800.md §5.2 (lines 771–779) — "No 48 kHz USB capture rate"
- `jasper/mics/xvf3800.py:50–59` — firmware variant table
- [XMOS XVF3800 Datasheet §"USB Audio Interface"](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html#usb-audio-interface)
- [XMOS lib_sw_pll](https://github.com/xmos/lib_sw_pll) — the SW PLL used for USB→mic clock sync
