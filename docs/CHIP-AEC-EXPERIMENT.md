# Chip-AEC experiment — conclusive test plan

**Status: 2026-05-29 positive lab result; chip-AEC leg promotion IN
PROGRESS (2026-05-31).**
The experiment is no longer a shelved negative. A same-day lab pass
proved that the XVF3800's on-chip AEC can produce useful cancellation
in JTS's external-DAC topology when the chip receives a clean USB-IN
far-end reference. The wake-corpus recorder has a dedicated chip-AEC
comparison profile that can enter/exit the needed test state and label
the `150°` / `210°` ASR beam outputs explicitly. **The corpus-only
guardrail is now being lifted:** the `chip_aec_150` / `chip_aec_210`
beams are being promoted from corpus-only capture to **opt-in,
hardware-conditional, scored production wake legs** (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
§2.4). Landed so far, all **default OFF**: the leg
registry/config/telemetry (chip-AEC promotion P1) and the control
surface — the reconciler `JASPER_WAKE_LEG_CHIP_AEC` boolean + single-chip
mutual exclusion, `/aec` status + `available` flag, the `/wake/` toggle,
install seed/migrate (P2 hardware-free). Still pending on-device
validation: the production chip-AEC profile in `jasper-aec-init`, the
bridge Option-A `:9876` repoint + `:9887`/`:9888` emit (P2
hardware-coupled), and the deploy + on-device validation (P4). **Until
those land and validate, the chip-AEC leg is wired but inert** — the
current production wake path is still the WebRTC AEC3 bridge, and chip
AEC is **not yet a functional `jasper-voice` wake input.**

The topology diagram below still records the original 2026-05-23
dmix-era experiment shape, not the current 2026-05-26 fan-in /
outputd production topology. Current production wiring lives in
[`docs/audio-paths.md`](audio-paths.md). The five
`scripts/chip-aec-*.sh` scripts + `jasper/chip_aec_experiment.py` +
this doc remain lab infrastructure: production state is untouched until
someone explicitly opts in via
[`scripts/chip-aec-setup.sh`](../scripts/chip-aec-setup.sh), and
[`scripts/chip-aec-teardown.sh`](../scripts/chip-aec-teardown.sh)
fully reverts.

**2026-05-29 headline result.** Option D is viable, with two important
corrections to the earlier mental model:

1. The scary ~40 ppm drift was a test-harness artifact from feeding the
   chip reference through `plug:jasper_capture`. When the same source
   buffer was played directly to both the external DAC and the XVF3800
   USB-IN endpoint, the DAC→air→mic reference drift collapsed to about
   `~1 ppm` over a 15-minute run. That means the split-DAC topology is
   not inherently doomed by clock drift when JTS owns the source fanout.
2. The useful output is the chip's **ASR output path** with fixed gated
   beams around the room-facing direction, not the early ch0-only
   hypothesis. The best tested setup routes category 7 with
   `AEC_ASROUTONOFF=1`, fixed beams at `150°` and `210°`, gating on,
   and `AEC_AECEMPHASISONOFF=2` (`on_eq`). The `150°` virtual beam was
   the clear listening and metric winner in Jasper's room orientation.

**Best lab configuration so far.** With the XVF3800 oriented such that
`0°` is the USB-C side and `180°` points into the room:

```text
SHF_BYPASS=0
AUDIO_MGR_SYS_DELAY=114              # only for the old delayed-feeder harness
AEC_ASROUTONOFF=1
AEC_ASROUTGAIN=1.0
AEC_FIXEDBEAMSONOFF=1
AEC_FIXEDBEAMSGATING=1
AEC_FIXEDBEAMSAZIMUTH_VALUES=[2.61799, 3.66519]  # 150°, 210°
AEC_FIXEDBEAMSELEVATION_VALUES=[0.0, 0.0]
AEC_AECEMPHASISONOFF=2               # on_eq; best strength/edge sweep result
AEC_FAR_EXTGAIN=0.0                  # +3/+6 dB were worse
AUDIO_MGR_OP_L=[7, 0]
AUDIO_MGR_OP_R=[7, 1]
```

Notes:
- `150°` and `210°` are **virtual beam directions**, not physical
  microphones. The chip has four physical mics; the beamformer combines
  them to listen in chosen directions.
- `150°` likely won because Jasper's voice/direct-room path was not
  symmetrical: that beam caught more near speech and/or less speaker
  reflection than `210°`. This is exactly the spatial leverage we
  hoped the chip beamformer would provide.
- `AEC_FAR_EXTGAIN=+3 dB` and `+6 dB` were worse than `0 dB` in the
  strength sweep. Do not interpret "more AEC" as "more far-end gain."
- The old `--ref-delay-ms 180` + `AUDIO_MGR_SYS_DELAY=114` alignment
  was only needed for the **old feeder harness**. The production-shaped
  path should fan one source buffer directly to the DAC and XVF3800
  USB-IN reference so the long feeder delay/drift path disappears.

**Production implication.** The final gold corpus may include chip AEC
through the recorder's chip-AEC comparison profile, which uses outputd's
direct final-output fanout rather than the old delayed feeder harness.
The profile is owned by `jasper-aec-init`: entering corpus mode
read-back verifies every critical chip write before recording can
continue, and exiting corpus mode explicitly restores the production
`SHF_BYPASS=1` + OP_L/OP_R routing overlay. A failed chip write is a
mode-transition failure, not a best-effort warning, because mislabeled
corpus audio is worse than no corpus audio.
The chip-AEC beams are being promoted from corpus-only to opt-in,
**default-OFF** production wake legs (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
§2.4). They stay default-OFF: gate any default-ON flip on a ~1-week
telemetry review of each beam's recall / false-accept contribution
against a fresh corpus window (`scripts/analyze-three-leg.sh`).

> ⚠️ **Policy carve-out.** [AGENTS.md](../AGENTS.md) "AEC bridge —
> reconciler toggle" says *"Architecture is fixed; swap the engine,
> not the topology"* and names "dual-USB-sink hardware-AEC retry"
> and "custom XVF firmware" as paths agents must not propose.
> [HANDOFF-barge-in.md](HANDOFF-barge-in.md) "Hardware AEC, revisited"
> repeats this as `Policy status: rejected by name`. **This doc and
> the five `scripts/chip-aec-*.sh` scripts are the user-authorized
> exception** — narrow, scoped to empirical resolution of
> [HANDOFF-aec.md](HANDOFF-aec.md) Option D ("Chip-AEC with USB-in
> reference topology"). The carve-out does not re-open the rejected
> paths elsewhere (PipeWire `module-echo-cancel`, dual-USB-sink,
> custom firmware) and does not license re-derivation of the
> question outside this infrastructure. Agents working on AEC
> remain bound by the policy for everything else.

**Goal:** Determine whether the XVF3800's hardware AEC can produce
useful echo cancellation in JTS's external-DAC topology when we feed
music to the chip's USB-IN as the AEC reference signal. The answer as
of 2026-05-29 is **yes in lab conditions**, provided the reference is
fed by a clean source fanout and the output uses the ASR fixed-beam
path rather than the old default ch1 assumptions.

The 2025 investigation that concluded "≤2 dB attenuation, won't
converge" was done in the dongle topology with no USB-IN reference at
all — the chip was running blind. The 2026-05-19 `SHF_BYPASS=0`
wake-rate test (15%) also ran without USB-IN reference. **Neither
result applies to the topology that succeeded here.** See
[HANDOFF-aec.md option D](HANDOFF-aec.md) for the deeper rationale.

**Decisive signals:** no single chip flag was enough. The
`AEC_AECCONVERGED` flag did flip to `[1]` in later lab state checks, but
the more reliable truth tests were:

- direct A/B audio: same source buffer to DAC + XVF3800 reference,
  `SHF_BYPASS=0` vs bypassed;
- reference-envelope correlation against the captured far-end tap;
- Jasper's ear on double-talk/music segments, because wake-word
  usefulness depends on speech shape surviving, not just residual
  music energy disappearing.

---

## 2026-05-29 findings

### 1. The drift problem was the feeder, not the hardware topology

The first same-day tests measured roughly `37-46 ppm` ref→air→mic drift
and made the split-DAC topology look doomed. A later dual-playback
harness invalidated that conclusion: when one in-memory source buffer
was played directly to both the external DAC and the XVF3800 USB-IN
reference endpoint, the drift collapsed to about `~1 ppm` over a
15-minute run. That points at the old `plug:jasper_capture` feeder path
as the drift source, not the Apple DAC + XVF3800 being inherently
clock-incoherent.

Production-shaped implication: do **not** build a large Rust
rate-matcher for this path yet. First build the direct source fanout:
one decoded/rendered source stream duplicated to the physical DAC and
the XVF3800 USB-IN reference endpoint. The old feeder is still useful
for lab sweeps, but it is not the architecture to ship.

### 2. The chip AEC core works

With the direct source fanout, controlled A/B captures showed about
`14.5 dB` far-end reduction with chip AEC on versus bypassed. A
double-talk capture was also promising: the AEC-on segment reduced
reference correlation while preserving more speech than the aggressively
post-processed category-6 beam path.

This supersedes the older "≤2 dB" chip-AEC rejection. That rejection was
true for the old topology without a proper USB-IN reference; it is not
true for Option D with a real reference.

### 3. Category 7 ASR output beat category 6 processed beam

The strongest output path was not the generic processed beam. The useful
route was category 7 with `AEC_ASROUTONOFF=1`, which makes category 7
carry ASR beam outputs instead of per-mic AEC residuals. In practical
terms:

- `AEC_ASROUTONOFF=0`: category 7 is the AEC core residual per mic
  source. Useful for diagnostics.
- `AEC_ASROUTONOFF=1`: category 7 is ASR beam output. This was the
  best wake-word-shaped signal in the listening tests.
- Category 6 / regular processed beam sounded over-processed in this
  room and should not be the first candidate for wake-corpus capture.

### 4. Fixed gated beams around 180° are the current winner

Jasper rotated the XVF3800 so the USB-C port points away from him. Per
the Seeed/XMOS direction convention used here, `0°` is the USB-C side
and `180°` is the opposite side. Fixed beams around that room-facing
direction outperformed auto/free-running beams:

| Test | Result |
|---|---|
| Auto/free-running ASR beams | Worse reference correlation; unstable listening quality |
| Fixed `170°/190°` gated | Too narrow; did not improve enough |
| Fixed `160°/200°` gated | Good, but not best |
| Fixed `150°/210°` gated | Best balance; `150°` was the standout ear winner |
| Fixed `120°/240°` gated | Strong cancellation but thinner / more attenuated |

The best `150°` beam had the lowest far-end correlation while keeping
voice-band energy intact. The likely reason is room geometry: Jasper's
voice and the speaker/wall reflections are not symmetric, so the `150°`
virtual beam happens to aim more at direct near speech and/or less at a
dominant echo/reflection path.

### 5. Strength/edge tuning: `on_eq` helped, far-end gain did not

Final four-way strength sweep, all with fixed gated `150°/210°` ASR
beams:

| Variant | Finding |
|---|---|
| Baseline: `AEC_AECEMPHASISONOFF=1`, `AEC_FAR_EXTGAIN=0 dB` | Good previous winner |
| `AEC_FAR_EXTGAIN=+3 dB` | Worse; more reference/music leaked through |
| `AEC_FAR_EXTGAIN=+6 dB` | Worse again; do not pursue "turn it up" this way |
| `AEC_AECEMPHASISONOFF=2` (`on_eq`), `AEC_FAR_EXTGAIN=0 dB` | Best metric result and current recommendation |

For the final sweep, `test-04-emphasis-on-eq / 150°` had the lowest
reference correlation (`+0.158`) while keeping voice-band energy roughly
flat versus raw (`-0.2 dB`). The `210°` sibling beam also improved but
was not as good. Current candidate: **ASR fixed gated `150°`, with
`on_eq` emphasis, plus optional `210°` as a secondary/backup beam.**

---

## Topology

```
renderers (shairport-sync, librespot, bluealsa-aplay, jasper-usbsink)
    │
    │ each writes to pcm.jasper_renderer_in   ← PR #214: dmix in front
    ▼                                            of hw:Loopback,0,0
pcm.jasper_renderer_mix (dmix, 48 kHz S16_LE, ipc_key 7779)
    │
    ▼
hw:Loopback,0,0
    │
snd-aloop kernel    hw:Loopback,0,sub0 ─cross-wired→ hw:Loopback,1,sub0
                                                          │
                                                          ▼
                                                  pcm.jasper_capture (dsnoop, 48 k)
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
                                            ▲              │  ch0 = conference; ch1 = ASR
                                            │              ▼
                                       airborne     udp://127.0.0.1:9876  ← pumped by chip-aec-experiment
                                                          │
                                                          ▼
                                                    jasper-voice
```

Topology shifts since the 2026-05-21 branch base, all transparent
to the experiment but worth noting:
- **PR #214** inserted a userspace dmix (`pcm.jasper_renderer_mix`,
  ipc_key 7779) between renderers and `hw:Loopback,0,0`. Renderers
  now write to `pcm.jasper_renderer_in` (a plug wrapper). Rate is
  deterministically 48 kHz instead of first-renderer-wins. The
  experiment's `plug:jasper_capture` tap is downstream of all of
  this and unaffected.
- **PR #223** moved ALSA config from `/root/.asoundrc` (mode 0600,
  root-only) to `/etc/asound.conf` (mode 0644, world-readable).
  This is what lets `chip-aec-capture-comparison.sh`'s `arecord
  -D plug:jasper_capture` work as the `pi` user.
- The bridge now emits *three* UDP streams in production
  (`:9876` AEC'd, `:9877` raw chip mic, `:9878` DTLN). The
  experiment's daemon still only feeds `:9876`. See limitation 6.
- **2026-05-26 fan-in topology replaced renderer-side dmix.** Current
  production sources write to private snd-aloop lanes; `jasper-fanin`
  sums them onto substream 7; `pcm.jasper_capture` / `pcm.jasper_ref`
  read that summed music reference. The experiment daemon still taps
  `plug:jasper_capture`, so the core reference-feeder idea should
  remain valid, but the topology diagram above is historical. Before
  running, verify `/etc/asound.conf` maps `pcm.jasper_capture` to
  `hw:Loopback,1,7`, `jasper-fanin.service` is active, and music played
  through normal sources appears on `plug:jasper_capture`.

Key differences from production:
- WebRTC AEC bridge **and its full lifecycle chain** are stopped +
  masked: `jasper-aec-bridge`, `jasper-aec-reconcile`,
  `jasper-aec-init`, `jasper-dongle-recover`. Masking only the
  bridge is insufficient — the reconciler can fire from udev
  (dongle replug), `install.sh`, or dongle-recover, and re-runs
  `jasper-aec-init` which unconditionally writes `SHF_BYPASS=1`.
  See limitation 4 for the full picture.
- `SHF_BYPASS = 0` (chip AEC engaged on ch0/ch1)
- New daemon `jasper.chip_aec_experiment` does two things in parallel:
  - **Reference feeder**: reads `plug:jasper_capture` (pre-CamillaDSP
    music tap), mixes L+R to mono, duplicates to stereo, writes to
    `hw:CARD=Array,DEV=0` at 16 kHz S16_LE (the only rate/format the
    chip's USB-IN endpoint advertises — verified empirically and via
    XMOS docs, see [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) §1)
  - **UDP mic pump**: reads chip's 6-ch mic capture, extracts the
    selected processed channel (default `ch0` for this chip-AEC
    experiment; override with `MIC_CHANNEL` /
    `JASPER_CHIP_AEC_MIC_CHANNEL`), and sends it to
    `udp://127.0.0.1:9876` (same UDP port the WebRTC bridge normally
    writes to → no `jasper-voice` changes for the AEC ON leg)

---

## Files added on this branch

| Path | Purpose |
|---|---|
| `jasper/chip_aec_experiment.py` | The daemon (feeder + UDP pump) |
| `scripts/chip-aec-setup.sh` | Phase 1: deploy + start daemon |
| `scripts/chip-aec-baseline-check.sh` | Phase 2: route sanity + repeatable delay baseline |
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

The checked-in scripts are the **old feeder-harness gate**. They remain
useful for route sanity and quick regression checks, but they do not
implement the production-shaped direct source fanout that made the
2026-05-29 result strong. Use them only with that limitation in mind.
For the next serious pass, build/run a direct fanout harness first:
one source buffer to the external DAC and to XVF3800 USB-IN, then capture
category-7 ASR fixed-beam output.

```sh
# 1. Run from a current checkout after re-reading the historical warning
#    at the top of this file and reviewing the scripts below.

# 1a. Fan-in-era preflight before touching the Pi:
#     - /etc/asound.conf maps pcm.jasper_capture to hw:Loopback,1,7
#     - jasper-fanin.service is active
#     - normal music playback is visible at plug:jasper_capture
#     - jasper-aec-bridge.service is active before setup
#     - SHF_BYPASS is 1 before setup

# 2. Set up (rsync code, stop bridge, start experiment daemon, set chip params)
bash scripts/chip-aec-setup.sh

# 3. Start music playing through the speaker at production volume.
#    AirPlay or Spotify or BT — any source. Let it play continuously.

# 4. Measure the baseline before judging chip AEC.
#    This temporarily switches the daemon to ref-only, bypasses chip AEC,
#    injects a short chirp train through correction_substream by default,
#    captures reference + chip mic three times, and estimates a first
#    AUDIO_MGR_SYS_DELAY candidate by cross-correlation. It restores
#    SHF_BYPASS=0 and the full daemon before exiting.
bash scripts/chip-aec-baseline-check.sh
# If the reported candidate delay is stable and within the firmware's
# read-back-confirmed range, apply it:
# ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY --values <samples>'

# 5. Verify the chip AEC convergence flag within ~60 s
bash scripts/chip-aec-poll-convergence.sh
# Positive signal: AEC_AECCONVERGED flips to [1].
# Caveat: the flag was not the only truth source on 2026-05-29.
# Still run an A/B capture + metrics/ear test.

# 6. Capture the 4 audio files for ear-test A/B
bash scripts/chip-aec-capture-comparison.sh
# Walk through the prompts (start music, mute music, speak, etc.)
# Files land under captures/chip-aec-experiment/<timestamp>/

# 7. Listen with your ears. The ear test is the truth test.
#    Recommended: open all 4 .wav files in Audacity side-by-side, or use
#    sox -m to subtract two recordings and listen to the residual.

# 8. Revert to production (WebRTC AEC bridge)
bash scripts/chip-aec-teardown.sh

# 9. Verify production recovered before recording corpus:
#    - jasper-aec-bridge.service active
#    - jasper-voice.service active
#    - SHF_BYPASS back to 1
#    - wake-corpus page can enter its own corpus test mode cleanly
```

---

## Baseline gate before convergence

`scripts/chip-aec-baseline-check.sh` exists so the experiment does not
start by guessing `AUDIO_MGR_SYS_DELAY`. It is still a quick lab gate,
not a production calibrator.

By default the gate injects a short chirp train through
`correction_substream`, the same dedicated fan-in lane used by correction
tests. That gives the cross-correlation a controlled calibration signal
while still exercising the normal fan-in → CamillaDSP → outputd → DAC →
speaker → mic path. Set `STIMULUS=none` only when intentionally testing
with ambient music alone; music proved too ambiguous on 2026-05-29 and
picked different room-reflection peaks across repeats.

What it verifies:
- The experiment daemon is alive and the chip USB-IN playback endpoint
  is active.
- `plug:jasper_capture` carries non-silent reference audio while music
  plays.
- With `SHF_BYPASS=1`, the chip mic actually hears the speaker echo.
- Repeated ref→mic cross-correlation measurements land close enough
  together to provide a credible first `AUDIO_MGR_SYS_DELAY` candidate.

Failure interpretation:
- Near-silent reference: routing/music is wrong; do not poll
  `AEC_AECCONVERGED`.
- Weak mic correlation: the mic capture is not seeing a useful copy of
  the speaker output; re-run with broader/louder music before judging.
- Large lag spread between repeats: the path is unstable enough that a
  short convergence result would be suspect. Fix or document before
  spending corpus time.
- Stable delay outside the firmware's confirmed `AUDIO_MGR_SYS_DELAY`
  range: add a test-only upstream reference delay with
  `REF_DELAY_MS=<ms>` / `--ref-delay-ms <ms>` and re-run. If the
  residual still cannot land inside `[-64, +256]`, stop; the chip
  cannot be aligned far enough with this firmware/topology.

---

## What to listen for

| File | What it is | What should it sound like |
|---|---|---|
| `01_reference.wav` | The signal we feed to chip USB-IN | Like the music you played — confirms the feeder is working |
| `02_mic_aec_off.wav` | Selected chip channel with `SHF_BYPASS=1` | Music dominates, similar to current WebRTC bridge's *input* |
| `03_mic_aec_on.wav` | Selected chip channel with `SHF_BYPASS=0` | **Music substantially reduced** if chip AEC is working |
| `04_speech_only.wav` | Selected chip channel, AEC on, no music | Your voice clearly, **not** muffled/robotic |

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
Need a production/corpus leg?
├── NO → Keep WebRTC AEC3 as production default; preserve this doc.
└── YES → first build direct source fanout:
         one decoded/rendered source → DAC + XVF3800 USB-IN reference
         ├── Long drift check still ~1 ppm?
         │   ├── NO → revisit timing/rate matching before corpus work.
         │   └── YES → enable chip SHF and route category 7 ASR outputs.
         │
         ├── Capture ASR fixed gated 150°/210° with on_eq emphasis.
         │   ├── 150° still clearly best → use it as primary chip-AEC leg.
         │   └── 210° adds saves / better direction coverage → keep as
         │       secondary or fusion candidate.
         │
         └── Wake-corpus decision:
             ├── Recorder can capture chip leg cleanly and restore state
             │   → include chip-AEC pilot leg in a fresh corpus session.
             └── No clean recorder integration yet
                 → do not spend gold corpus time on ad-hoc lab harness WAVs.
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

6. **Dual / triple-stream wake will be silently degraded.**
   `JASPER_MIC_DEVICE_RAW=udp:9877` (PR #191 dual-stream OR-gate)
   and `JASPER_MIC_DEVICE_DTLN=udp:9878` (triple-stream OR-gate)
   are default-off, but if the household has enabled them in
   `/etc/jasper/jasper.env`, the experiment's daemon only feeds
   `:9876` — the OFF/DTLN legs starve. Wake still works on the AEC
   leg, but every wake-event row gets `score_off=none` /
   `score_dtln=none`. **`chip-aec-setup.sh` defensively comments
   these env lines out for the experiment duration and `chip-aec-
   teardown.sh` restores from `.chip-aec.bak`** — but worth knowing
   if you're sanity-checking voice behavior mid-experiment.

7. **Wake-event corpus contamination.** `WakeEventStore`
   (`jasper/wake_events.py`) writes every wake to
   `/var/lib/jasper/wake-events/wake-events.sqlite3` + a 1 GB WAV
   ring, with no env knob to disable. During the experiment, fires
   land in the same corpus as production WebRTC-AEC data — bad for
   future wake-rate analysis. The setup script drops a timestamp
   sentinel at `.chip-aec-experiment-start.ts`; the teardown reads
   it and prints SQL one-liners to either label experiment-window
   events as `chip-aec-experiment` (recommended; preserves
   forensics) or delete them (cleans corpus). Operator's choice.

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

References are by section header / identifier, not line number, so
they survive future doc edits (per [AGENTS.md](../AGENTS.md)
"Documentation paradigm" rule 5).

- [HANDOFF-aec.md](HANDOFF-aec.md) "D — Chip-AEC with USB-in reference
  topology" — the 2026-05-21 docs-review writeup. *Note:* the
  pre-correction rate claim ("48 kHz to USB-in is fine") was wrong;
  this branch's commit corrected it in-place.
- [HANDOFF-aec.md](HANDOFF-aec.md) "What we found about chip-side
  AEC in our topology" — original 2025 investigation, dongle
  topology, no USB-IN reference. Conclusion does not apply here.
- [HANDOFF-aec.md](HANDOFF-aec.md) "Chip-pipeline-only alternative
  considered + rejected" — May 2026 wake-rate test, no USB-IN
  reference. Conclusion does not apply here.
- [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) §1 "Hardware identity"
  — canonical USB endpoint table (16 kHz S16_LE fixed at build time).
- [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) §5.2 "No 48 kHz USB
  capture rate" — explicit confirmation no shipped firmware
  exposes 48 kHz USB.
- [`jasper/mics/xvf3800.py`](../jasper/mics/xvf3800.py) —
  `VARIANT_2CH`, `VARIANT_6CH`, `RECOMMENDED_FIRMWARE` constants
  carrying the firmware variant table.
- [XMOS XVF3800 Datasheet §"USB Audio Interface"](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html#usb-audio-interface)
- [XMOS lib_sw_pll](https://github.com/xmos/lib_sw_pll) — the SW PLL used for USB→mic clock sync

---

Last operational verification: 2026-05-30 (live Pi lab pass found the
old feeder-path drift was a harness artifact; direct source fanout held
about `~1 ppm` over 15 minutes; controlled direct A/B showed useful chip
AEC reduction; ASR fixed gated `150°/210°` with `AEC_AECEMPHASISONOFF=2`
was the best tested wake-shaped output, with `150°` the standout beam;
the wake-corpus recorder now has a corpus-only chip-AEC comparison
profile for collecting those legs intentionally; `jasper-aec-init`
now read-back verifies entry and explicitly restores production chip
routing on exit). This doc still preserves a dmix-era experiment
snapshot in places; current production topology lives in
`docs/audio-paths.md`.
