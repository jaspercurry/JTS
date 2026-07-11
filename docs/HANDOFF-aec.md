# Acoustic Echo Cancellation — investigation, design, and current state

This document describes the AEC subsystem in detail: why it exists,
what we tried, what failed, what shipped, and what's still open. It
is the canonical source for anyone touching `jasper-aec-bridge`,
`jasper-aec-init`, the `pcm.jasper_capture` dsnoop, the bridge↔voice
UDP transport (see [HANDOFF-resilience.md](HANDOFF-resilience.md)
for why it's UDP and not a second snd-aloop card), the
`jasper/xvf/` XMOS control helper, or any of the
supporting documentation in `BRINGUP.md` and
`docs/audit-pending-followups.md`.

**Companion doc**: [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) is the
chip-side canonical reference — full parameter space, firmware
variants, DFU flow, ALSA mixer invariants, ranked hypothesis ladder
for raw-mic-silence symptoms, and diagnostic cookbook. This doc
(HANDOFF-aec.md) explains the *engine* and the *why*: WebRTC AEC3 as
fallback, plus Option D — the chip-AEC variant that became a positive
lab result on 2026-05-29 and now backs the recommended XVF3800 input
profile, see [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md).
HANDOFF-xvf3800.md explains the *chip*.
The `jasper/mics/xvf3800.py` profile module is the canonical
source for chip-specific constants consumed at runtime.

**Open architectural question**: [HANDOFF-barge-in.md](HANDOFF-barge-in.md)
surveys the option space for upgrading barge-in from today's
VAD-only filtering to AEC-cancellation-of-TTS. **Read it before
any change that touches the music↔TTS↔AEC topology** —
particularly the "trap" section, which preserves the analysis of
why several obvious-looking fixes (route TTS through CamillaDSP,
second dsnoop tap + software-sum, ALSA `multi` plugin, UDP-tee)
are all wrong for the same underlying single-reference /
delay-alignment reason.

The goal is to make this enough context that a future session can
pick up the work without re-doing the investigation.

---

## TL;DR / current state

**Input selection is profile-first.** `install.sh` seeds
`/var/lib/jasper/aec_mode.env` with `JASPER_AUDIO_INPUT_PROFILE=auto`,
enables `jasper-aec-reconcile.service`, and runs the reconciler once.
On the recommended 6-channel XVF3800 shape plus an approved output-DAC
gate, `auto` resolves to `xvf_chip_aec`: `jasper-outputd`
fans out the final speaker buffer to the XVF USB-IN reference,
`jasper-aec-init` applies the volatile 150°/210° ASR beam profile, and
`jasper-aec-bridge` forwards the primary/session chip beam to `:9876`.
Optional extra wake/scoring beams on `:9887`/`:9888` are custom opt-ins, not
part of the default chip-AEC profile. Software AEC3 remains the fallback profile
(`xvf_software_aec3`) when chip-AEC is unavailable, the output DAC still
needs calibration, or software AEC3 is explicitly selected. To validate a
new output DAC without promoting it to production, select
`xvf_chip_aec_testing`; it runs the same physical chip-AEC path as
`xvf_chip_aec`, surfaces the DAC gate as `testing`, and never makes
`auto` choose that DAC.

The current `xvf_chip_aec` baseline is **not** the older
`SHF_BYPASS=1` software-AEC profile. With chip-AEC armed,
`jasper-aec-init` writes `SHF_BYPASS=0`, `AUDIO_MGR_SYS_DELAY=12`,
`AUDIO_MGR_REF_GAIN=8.0`, `AEC_FAR_EXTGAIN=0.0`,
`AEC_ASROUTONOFF=1`, fixed gated 150°/210° beams, and
`AUDIO_MGR_OP_L/R=[7,0]/[7,1]`. When chip-AEC is unavailable or
disabled, the software fallback restores `SHF_BYPASS=1` and
`AUDIO_MGR_OP_L/R=[8,0]/[8,0]` so host-side AEC3 receives a
non-silent raw-ish XVF channel.

**Hardware adaptability contract:** chip-AEC support is a property of
the resolved input/output hardware profile, not of one known DAC. The
central policy in `jasper/chip_aec_policy.py` owns the DAC gate vocabulary
(`approved`, `testing`, `needs_calibration`) and every status surface
consumes that same decision. The architecture must support Apple USB-C
dongles, HiFiBerry/DAC hats, DAC8x-style active profiles, multiple Apple
dongles, and future USB DACs through the same ownership lines:
reconcile/config owns profile defaults and fallback/degraded states;
outputd owns final-output reference publication and timing health; audio
validation reports DAC-gate readiness and doctor surfaces the same state
for the active DAC. Prefer live timing/level measurement and outputd
`/state` evidence over profile folklore.
Only carry per-profile residual
`AUDIO_MGR_SYS_DELAY` trim after dynamic timing is exhausted and the
calibration artifact is codified with that hardware profile. Old taps such
as `pcm.jasper_capture` remain diagnostic only.

In the software-AEC3 path, the bridge consumes outputd's 48 kHz stereo
speaker monitor over UDP and downmixes/resamples it for WebRTC AEC3. The
same outputd monitor feeds chip-AEC/corpus/diagnostics; chip-AEC
additionally gets the downsampled XVF USB-IN reference PCM. Explicit
`JASPER_AEC_REF_SOURCE=alsa` remains a fallback/diagnostic mode for the
pre-DSP `pcm.jasper_ref` path. Do not add another ad-hoc ALSA tap to solve
reference alignment.

The `jasper-aec-bridge` service is therefore a shared mic-to-voice carrier,
not synonymous with the WebRTC AEC3 engine. Under `xvf_chip_aec` /
`xvf_chip_aec_testing`, the bridge process stays active so it can forward the
selected hardware-AEC chip beam to `:9876`, but the WebRTC AEC3 engine is not
instantiated. Operator surfaces expose this as `software_aec3.bypassed=true`;
turning off the software-AEC3 layer must not stop the chip-AEC carrier. To
stop the carrier entirely, choose the `direct_mic` profile.

The chip-AEC profile's default wake surface is deliberately one detector:
the primary/session beam (`JASPER_MIC_DEVICE=udp:9876`, wake leg `on`).
The 150-degree and 210-degree chip beams are advanced custom opt-ins via
`JASPER_WAKE_LEG_CHIP_AEC_150=1` and
`JASPER_WAKE_LEG_CHIP_AEC_210=1`; only then does the reconciler publish
`JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887` or
`JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888`. Selecting any named profile
resets those optional beams to `0`; `custom` preserves them. This ties active
wake-word instances to the audio-processing channels the reconciler actually
applied, which avoids hidden extra Silero/openWakeWord model instances on
chip-AEC hardware.

`/aec` separates saved intent from applied runtime truth. `raw_intent` mirrors
`/var/lib/jasper/aec_mode.env`; active fields such as `mode`, `bridge_role`,
`software_aec3`, `legs`, `audio_profile.active`, and `/wake/`'s
`mic_settings` are derived from the reconciler-applied `/etc/jasper/jasper.env`
snapshot. If an explicit hardware-AEC request is rejected by the mic/DAC gate,
the reconciler falls back to software AEC3 and `/aec` reports software AEC3 as
active while leaving the selected hardware profile visible with the fallback
reason. If runtime env is stale during a mic-card change, `/aec.bridge_role`
reports `pending` and `software_aec3.active=false` until a concrete profile is
actually applied. Status surfaces must not infer the active AEC engine from
`JASPER_AUDIO_INPUT_PROFILE` alone or from `jasper-aec-bridge.service` being
up.

To turn the bridge OFF entirely (or back to direct mic for A/B testing),
choose the `direct_mic` profile or set the state file to disabled and run the
reconciler:

```sh
printf 'JASPER_AUDIO_INPUT_PROFILE=direct_mic\nJASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

To return to auto mode:

```sh
printf 'JASPER_AUDIO_INPUT_PROFILE=auto\nJASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\nJASPER_WAKE_LEG_CHIP_AEC=0\nJASPER_WAKE_LEG_CHIP_AEC_150=0\nJASPER_WAKE_LEG_CHIP_AEC_210=0\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

`JASPER_AUDIO_INPUT_PROFILE=auto` is the authoritative selector in that
state file; the legacy wake-leg booleans are kept for rollback
compatibility and are re-resolved by the reconciler from live hardware.

The reconciler also handles stale hardware state. If the Array is
absent after a previous AEC-enabled boot, it clears the stale
`JASPER_MIC_DEVICE=udp:9876`, disables the bridge, and stops voice
instead of leaving wake-word on an unfed UDP socket.

> ### Important: three bridge bugs fixed on 2026-05-19
>
> A multi-day investigation surfaced three independent bugs that
> had been silently corrupting AEC's reference signal since the
> bridge shipped. All are fixed in current production. Briefly:
>
> 1. **ALSA linear resampler** (PR #150) — `libasound2-plugins` +
>    the JTS audio-quality rate converter in `/etc/asound.conf`
>    (default `samplerate_medium`; `samplerate_best` optional from
>    `/system/`; legacy location was `/root/.asoundrc` before PR #223).
>    Without these, the plug-layer 44.1→48 conversion lost ~12 dB
>    of 4-8 kHz content.
> 2. **Silence fallback on empty ref_q** (PR #154) — replaced
>    "ref_bytes = silence" with "carry forward last_ref_bytes."
>    Without this, AEC received zeroed reference 50 % of the time.
> 3. **Drain-newest discarded burst frames** (PR #157) — replaced
>    drain-to-newest with consume-one-per-iteration. Without this,
>    50 % of frames in ref.wav were byte-identical duplicates of
>    their predecessor.
>
> All three were documented separately in **"Resampler quality —
> the 2026-05-19 finding"** and **"Bridge ref starvation bug —
> fixed (2026-05-19)"** below. The deployment now feeds AEC3 a
> continuous, full-bandwidth reference for the first time.
>
> ⚠ **All wake-rate baseline data from before 2026-05-19 is
> invalid for evaluating AEC's contribution.** The "AEC ON" leg of
> every previous test ran with broken reference. The "AEC OFF" /
> chip-direct legs remain valid (the bugs were bridge-only). Any
> future "does AEC help?" question requires fresh measurement
> after these fixes.

> ### NS=low + AGC1 — 2026-05-20 production tuning
>
> Post-ref-fix wake-rate sweep surfaced two further knobs that
> moved the needle. **Both are now production defaults:**
>
> - `JASPER_AEC_NS_LEVEL=low` (was `moderate`) — less aggressive
>   noise suppression preserves HF speech consonants the wake model
>   relies on. Wake rate: 5/20 vs prev 4/20 in the same data.
> - `JASPER_AEC_AGC1_ENABLED=1` (default off in binding) — WebRTC
>   AGC1 in `kAdaptiveDigital` mode replaces the static `MIC_GAIN_DB`
>   approach for level normalization. Fixes "some Jarvises overblown,
>   some too quiet" — uniform output across utterances regardless of
>   instantaneous music level.
>
> Full details in **"NS aggressiveness + AGC1 dynamic gain — 2026-05-20
> findings"** below. Sweep methodology is captured in user memory
> `project_aec_wake_rate_forensic_methodology.md`.

### High-pass filter architecture

The mic in this project is consumed only by software (openWakeWord
at 16 kHz mono, then a real-time speech LLM). No human listens to
it. Per [memory note](https://github.com/jaspercurry/JTS — internal
memory `project_mic_consumed_by_robots_only`) and the research
findings below, we band-limit the signal to the speech range
because everything outside it is noise the consumers don't use AND
content AEC3's adaptive filter wastes capacity trying to model.

The HPF stack, layered defense:

| Layer | Filter | Cutoff | Where | Tuning knob |
|---|---|---|---|---|
| Chip mic ingress | 4th-order Butterworth | 125 Hz | XVF3800 `AEC_HPFONOFF`, set in `jasper-aec-init` | `JASPER_AEC_CHIP_HPF_HZ` env, values 0/70/125/150/180 |
| AEC3 internal capture | 2nd-order Butterworth | 100 Hz | `AudioProcessing` upstream of `EchoCanceller3`, enabled in `jasper_aec3/src/aec3_binding.cpp` | always on (compile-time) |
| Bridge ref pipeline | 2nd-order Butterworth | **125 Hz** | `_ref_thread` in `jasper/cli/aec_bridge.py`, after `resample_poly`, before REF_GAIN | `JASPER_AEC_REF_HPF_HZ` env, default 125 Hz |

**Why HPFs on both legs are not redundant**: AEC3 applies its
internal HPF to the **capture** (mic) signal only. The reference
signal arrives untouched at AEC3's adaptive filter. Without the
bridge-side ref HPF, AEC3 sees asymmetric inputs and its matched
filter wastes coefficients on an LF relationship that doesn't
exist in the capture. Symmetric HPF at both legs is the documented
design intent (see WebRTC commit "AEC3: High-pass filter delay
estimator signals").

**Why 125 Hz on both**: openWakeWord's preprocessor
(Google `speech_embedding`) has a 60 Hz mel floor. 125 Hz nulls
2-3 of 32 mel bins (small unverified risk to wake accuracy) but
provides more LF rejection at the source and matches XMOS's
shipped smart-speaker default. The ref HPF cutoff matches the
chip-side mic HPF cutoff so AEC3 sees symmetric bands. If wake
accuracy regresses, drop to 70 Hz via the env var without a code
change (both `JASPER_AEC_CHIP_HPF_HZ` and `JASPER_AEC_REF_HPF_HZ`).

The bridge→voice transport is **UDP localhost** (default
`127.0.0.1:9876`), not snd-aloop. The original `LoopbackAEC`
two-card snd-aloop topology was retired in May 2026 after a
kernel-state-corruption incident — see
[HANDOFF-resilience.md](HANDOFF-resilience.md) for the rationale.

---

## Software-AEC tuning (2026-05-16) — fallback baseline

This is the authoritative section for the `xvf_software_aec3`
fallback profile: what is tuned, where, and why when chip-AEC is
unavailable or explicitly bypassed. It supersedes the older "Tuning
findings" section below for the software path. Current profile
selection starts in the TL;DR above: on recommended 6-channel XVF3800
hardware, `auto` resolves to `xvf_chip_aec`; software AEC3 remains the
fallback, not the default recommended path.

### Software-AEC fallback architecture

```
4 mics → preamp → MIC_GAIN → AEC_HPFONOFF=125 Hz → [SHF_BYPASS=1: entire SHF block off]
                                                          │
                                                          ▼
                                                   chip channel 1 carries
                                                   raw-ish mic data
                                                   (NO BF, NO NS, NO AGC,
                                                    NO chip AEC; possibly
                                                    post-MIC_GAIN per
                                                    output mux routing)
                                                       │
                                                       ▼
              jasper-aec-bridge:
                ref = outputd UDP speaker monitor
                      (final electrical samples: renderer/content plus
                       TTS/cues, post-CamillaDSP/outputd)
                     → resample 48k → 16k → HPF 125 Hz → REF_GAIN +0 dB
                mic = chip ch 1 (16k mono, raw-ish)
                     → AudioProcessing internal HPF 100 Hz
                     → WebRTC AEC3 (does ALL the work: linear cancellation,
                       residual suppression, internal NS at kModerate)
                     → MIC_GAIN +6 dB
                     → UDP 127.0.0.1:9876
                                                                │
                                                                ▼
                                                       jasper-voice:
                                                         UdpMicCapture
                                                         → openWakeWord
                                                         → real-time LLM
```

### Caveat: what `SHF_BYPASS=1` actually does

When the chip's `SHF_BYPASS=1`, **the entire SHF block on the
chip is removed from channels 0 and 1**, not just the AEC
adaptive filter. SHF includes AEC + beamformer + post-SHF DSP
(NS, NLP, AGC). So with SHF_BYPASS=1, channels 0/1 carry
**raw-ish mic data**, similar to channels 2-5.

Empirically verified 2026-05-16: with `SHF_BYPASS=1`, toggling
`PP_MIN_NS` from 0.150 to 1.0 (NS off) and `PP_AGCONOFF` from
1 to 0 changes channel 1's sub-bass band by **0.6 dB** — same
order as the measurement noise on channel 2 (0.7 dB). The chip
post-processing parameters do nothing when SHF_BYPASS=1.

The HPF set via `AEC_HPFONOFF=2` may still apply (it lives at
mic ingress before the SHF block), but BF / NS / AGC do not.

The level difference between ch 1 (SHF_BYPASS=1) and ch 2 (raw
mic 0) is about 1 dB across all bands — likely just MIC_GAIN
being applied on ch 1 via the output mux but not on ch 2's
Category 1 tap. The two channels are functionally similar.

**Implication**: the "chip processing" we previously thought we
were getting from channel 1 + SHF_BYPASS=1 was illusory. The
performance win measured on 2026-05-16 came from `REF_GAIN=0`
correcting the ref-mic level match for AEC3, NOT from chip BF /
NS / AGC. If we ever want the chip's actual post-processing, we
need `SHF_BYPASS=0` — which puts the chip's own AEC back in the
signal path. That was incompatible with our external-DAC topology
until outputd grew the XVF USB-IN reference fanout; the current
`xvf_chip_aec` profile is the supported way to run that path.

### Tuning values, with rationale per knob

| Knob | Software fallback value | Where | Why this value |
|---|---|---|---|
| **Mic channel** | **1 (ASR beam, post-SHF tap)** | `jasper/mics/xvf3800.py` `MIC_CHANNEL_INDEX` | Canonical XVF3800 voice-assistant channel choice. With fallback `SHF_BYPASS=1`, ch 1 effectively carries raw-ish mic data; the chip-processing benefit usually associated with ch 0/1 is gated on SHF_BYPASS=0. Channel 2 (explicit raw mic 0, Category 1) would be functionally similar in the fallback config — see "Caveat" above. |
| **Chip output mux** | **OP_L=`(8,0)`, OP_R=`(8,0)`** | `jasper-aec-init` | The bridge reads channel 1. Seeed's firmware default for channel 1 is OP_R=`(0,0)` (silence), so fallback init must keep OP_R on a non-silent route. 2026-05-31 failure mode: restoring OP_R to the firmware default made `jasper-aec-bridge` report `mic=0` even though ALSA capture and UDP output were healthy. |
| **Chip SHF** | **BYPASSED (`SHF_BYPASS=1`)** | `jasper-aec-init` | The software fallback uses host-side AEC3 and intentionally keeps the chip AEC out of the near-end path. **SHF_BYPASS=1 disables the ENTIRE SHF stage (AEC + BF + NS + AGC) on channels 0/1**, not just AEC — see Caveat above. The chip-side HPF stays. The recommended chip-AEC profile is separate and uses `SHF_BYPASS=0` with a live XVF USB-IN reference from outputd. |
| **Chip HPF** | **125 Hz, 4th-order Butter (`AEC_HPFONOFF=2`)** | `jasper-aec-init` | XMOS shipping default for smart-speaker presets. Applied at mic ingress before the SHF block (so survives SHF_BYPASS). Cuts LF rumble at the source. Configurable via `JASPER_AEC_CHIP_HPF_HZ` (off/70/125/150/180). |
| **Ref-side HPF** | **125 Hz, 2nd-order Butter** | `_ref_thread` in `jasper/cli/aec_bridge.py` | Matches chip mic-side HPF cutoff so AEC3 sees symmetric bands. Configurable via `JASPER_AEC_REF_HPF_HZ`. |
| **`JASPER_AEC_REF_GAIN_DB`** | **0** | `/etc/jasper/jasper.env` + `.env.example` | The single most impactful knob in the 2026-05-16 tuning. The fallback raw-ish mic input (ch 1 with SHF_BYPASS=1) arrives at ~-22 dBFS RMS due to chip MIC_GAIN preamp + speaker-room-mic acoustic path. Digital ref is at -10 to -25 dBFS depending on music dynamics. AEC3's design point is ref ~= mic; +0 dB matches this. **Any positive REF_GAIN drives ref into hard clipping** — see "REF_GAIN trap" below. |
| **`JASPER_AEC_MIC_GAIN_DB`** | **+6 dB** | `/etc/jasper/jasper.env` | Boosts AEC3 output to openWakeWord's training distribution (~-18 dBFS RMS). Static gain, doesn't reshape envelopes. Soft-clipped via tanh on the way out. With `AGC1_ENABLED=1` this stacks on top of AGC1's dynamic gain — drop to 0 if too hot. |
| **`JASPER_AEC_AGC2`** | **0** (off) | `/etc/jasper/jasper.env` | Was investigated as a level-stabilizer; turns out our binding only sets `gain_controller2.enabled = true`, while the `adaptive_digital` sub-config defaults off in libwebrtc-audio-processing-1 v1.3-3. Net result: AGC2=on is a no-op for level control on this Trixie build. Use AGC1 instead (below). Kept env-tunable for backwards compatibility; recommended off. |
| **`JASPER_AEC_AGC1_ENABLED`** + **`_TARGET_DBFS`** + **`_MAX_GAIN_DB`** | **1, 9, 18** | `/etc/jasper/jasper.env` | WebRTC AGC1 in `kAdaptiveDigital` mode. `TARGET_DBFS=9` → −9 dBFS target via `target_level_dbfs` (positive value = dBFS-below-zero; range 0–31). `MAX_GAIN_DB=18` → `compression_gain_db=18` (soft-knee compressor parameter, range 0–90 — *not* a "max gain ceiling" despite our env-var name; that name is misleading and retained only for compat with the shipped binding). Wake-rate sweep on 2026-05-20 showed these params have minimal observable effect on Trixie's `libwebrtc-audio-processing-1` v1.3-3 (the built-in limiter dominates); all configs converged to ~RMS 1213 output. The shipped benefit is *consistency*, not detection rate: vs static `MIC_GAIN_DB=+12` (also 5/20, RMS=2229 with audible inter-utterance level variance), AGC1 produces uniform output across utterances. **WebRTC AGC1 has no public attack-time or release-time parameter** — any earlier doc claim referencing "150 ms attack" was incorrect (audit 2026-05-21). |
| **AEC3 internal capture HPF** | 100 Hz 2nd-order Butter | `jasper_aec3/src/aec3_binding.cpp` | Enabled via `cfg.high_pass_filter.enabled = true`. Defense in depth with chip + ref HPFs. |
| **AEC3 internal NS** | **`kLow`** (was `kModerate` until 2026-05-20) | binding default + `/etc/jasper/jasper.env` `JASPER_AEC_NS_LEVEL` | Post-AEC noise/music suppression. More aggressive NS strips more HF speech-consonant features that openWakeWord depends on. Wake-rate sweep on 2026-05-20: `kLow` 5/20, `kModerate` (prev) 4/20, `kHigh` 3/20, `kVeryHigh` 2/20. `kLow` is the sweet spot; lower not exposed by Trixie v1.3-3 (no `kVeryLow`). Disable entirely via `JASPER_AEC_NS_ENABLED=0` for max HF preservation at the cost of residual music passing through. |

### Corpus-only AEC3 sweep knobs

The production `on` leg still defaults to BEST_A. For wake-corpus
pilot tuning, `jasper_aec3/src/aec3_binding_v2.cpp` and
`_Aec3V2Engine` in `jasper/cli/aec_bridge.py` expose a small set of
additional WebRTC AEC3 suppressor knobs as env overrides so the corpus
bridge can run same-utterance variants without changing the production
chain:

| Env var | BEST_A/default | Purpose |
|---|---:|---|
| `JASPER_AEC_NEAREND_AVERAGE_BLOCKS` | `4` | Near-end smoothing window. |
| `JASPER_AEC_NEAREND_MASK_HF_ENR_T` / `_ENR_S` / `_EMR_T` | `0.1` / `0.3` / `0.3` | HF masking thresholds when AEC3 believes near-end speech dominates. |
| `JASPER_AEC_NEAREND_MAX_DEC_LF` | `0.25` | Near-end suppressor gain-decrease rate. |
| `JASPER_AEC_NEAREND_MAX_INC` | `2.0` | Near-end suppressor gain-increase rate. |
| `JASPER_AEC_DND_SNR_THRESHOLD` | `30` | Dominant-near-end SNR threshold. Lower values trigger near-end mode sooner. |
| `JASPER_AEC_DND_HOLD_DURATION` | `50` | Dominant-near-end hold duration in AEC3's native block units. |
| `JASPER_AEC_DND_ENR_THRESHOLD` | `0.25` | Dominant-near-end echo-to-near-end-ratio threshold. |
| `JASPER_AEC_DND_TRIGGER_THRESHOLD` | `12` | Number of detector hits required before dominant-near-end mode engages. |
| `JASPER_AEC_STREAM_DELAY_MS` | `40` | WebRTC AEC3 stream-delay hint in milliseconds. The canceller still adapts internally; this is the coarse delay prior used by the binding. Corpus-only sweeps may vary it to test USB/ref alignment. |

As of 2026-05-28, the corpus AEC3 sweep registry in `jasper/aec_sweep.py`
owns three stable pilot slots (`aec3_variant_1`, `aec3_variant_2`,
and `aec3_variant_3`). The code defaults now match the current USB
alignment pilot: USB `usb_webrtc` runs the edge-combo tuning at the
baseline 40 ms delay hint, while the three variant slots run the same
edge-combo tuning at 80, 120, and 160 ms. Labels and knob overrides
can still be changed at runtime via
`/var/lib/jasper/aec3_sweep_variants.json`; apply a validated file with
`jasper-aec-sweep-config apply <file> --restart-bridge` to restart only
`jasper-aec-bridge`. The sweep input source is explicit:
`JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=xvf` feeds variants from the XVF
mic path, while `usb` feeds them from the cheap USB mic and requires
`JASPER_AEC_CORPUS_USB_ENABLED=1` plus reference capture. New
wake-corpus UI sessions default to USB-fed variants so the same
utterance captures USB baseline + three USB AEC3 variants while keeping
the XVF `on` leg as the comparison reference. Do not promote a sweep
variant to production until it beats BEST_A on same-utterance listening
review, corpus-quality metrics, and wake scoring under the far+music
condition.

### Measured outcome at this tuning

Bridge log during AirPlay music playback:

```
ref=1675 mic=2549 aec=383 → attenuation=-16.5 dB (ref_clip=0.00%)
ref=2482 mic=3246 aec=599 → attenuation=-14.7 dB (ref_clip=0.00%)
ref=2581 mic=3693 aec=460 → attenuation=-18.1 dB (ref_clip=0.00%)
ref=2614 mic=3822 aec=403 → attenuation=-19.5 dB (ref_clip=0.00%)
```

Steady-state attenuation -14 to -20 dB. Zero ref clipping. Stable
across consecutive 5-second windows (previous architecture
oscillated between -0.3 dB and -20.8 dB chaotically).

### The REF_GAIN trap (don't repeat this)

`REF_GAIN_DB=25` was the production value for a year, dating back
to when the bridge consumed raw mic 0 (channel 2). At that point
there was no chip AGC on the mic path, so the mic side arrived at
~-50 dBFS while the digital ref was at full scale — and AEC3's
adaptive filter needed roughly comparable levels for good
convergence. +25 dB on the ref closed that gap.

After the bridge moved to chip channel 1 (chip AGC normalizes mic
to ~-24 dBFS), keeping `REF_GAIN_DB=25` drove the digital ref
into 11–44% hard-clipping during music peaks. AEC3 was operating
on a saturated reference and produced wildly variable attenuation
(observed -0.3 to -20.8 dB across consecutive 5 s windows).

**Rule of thumb**: if you change `MIC_CHANNEL_INDEX` (in
`jasper/mics/xvf3800.py`), `REF_GAIN_DB` almost certainly needs to
change too. The two are coupled. Production today: ch 1 + REF_GAIN=0.
If anyone reverts to channel 2 (raw mic) for any reason,
`REF_GAIN_DB` must be raised back to ~25 to compensate for the
missing chip AGC.

---

## Resampler quality — the 2026-05-19 finding

This section documents a major discovery from the wake-rate
investigation on 2026-05-19. **Until this date the bridge's
reference signal was being silently degraded by ALSA's built-in
linear resampler**, which lost ~12 dB of 4-8 kHz content during
the unavoidable 44.1→48 kHz conversion. The mic captures speakers'
full-bandwidth output; AEC was given a hollow reference; AEC could
not cancel content its reference didn't contain; music residuals in
the speech band masked wake-word phonemes; wake detection was
intermittent.

### The chain (mandatory rates, with no escape)

The Apple USB-C dongle hardware-locks at 48 kHz (`/proc/asound/A/stream0`
shows `Rates: 48000 - 48000 (continuous)`). CamillaDSP runs at 48 kHz
to match. The snd-aloop "Loopback" card is locked at 48 kHz when
CamillaDSP opens it.

So *every renderer* that's not natively 48 kHz must resample
somewhere in the chain:

| Source | Native rate | Resampling site |
|---|---|---|
| AirPlay (shairport-sync) | 44.1 kHz | shairport writes `shairport_substream` → ALSA plug |
| Spotify Connect (librespot) | 44.1 / 48 kHz | librespot → snd-aloop, plug if mismatch |
| Bluetooth A2DP (bluealsa-aplay) | 44.1 / 48 kHz | bluealsa-aplay → snd-aloop, plug if mismatch |
| AEC bridge ref read | 16 kHz (internal) | Normal path: outputd UDP speaker monitor at 48 kHz → bridge resamples/downmixes. Explicit fallback: `pcm.jasper_ref` plug → bridge requests 48k from 48k loopback. |

The bridge's *own* 48→16 resample is scipy `resample_poly`, which
is high-quality polyphase. That step has never been the issue.

### What "linear resampler" actually meant in practice

Without `libasound2-plugins` installed, ALSA's `plug:` plugin
falls back to a built-in linear interpolator. It is famously poor
for audio — the linux-audio mailing list has been complaining
about it for ~15 years.

Measured impact on our system (10 s window during AirPlay of
Pink Floyd "Money", same physical mic, same speakers):

| Band | mic_ch1 (chip raw, captures speakers) | ref.wav (post linear-resample plug) | gap |
|---|---|---|---|
| 0-200 Hz | 47.8 dB | 31.0 dB | — (chip HPF doing work; speakers + mic LF) |
| 200-1000 Hz | 37.9 dB | 26.1 dB | — |
| 1000-4000 Hz | 24.3 dB | 18.2 dB | — |
| **4000-7000 Hz** | **15.8 dB** | **4.1 dB** | **−12 dB** |
| **7000-8000 Hz** | **12.1 dB** | **1.8 dB** | **−10 dB** |

The mic captures what the speakers emit. The ref tells AEC what we
*sent to* the speakers. The 10-12 dB hole at 4-8 kHz in the ref is
spectrally what the linear resampler dropped during 44.1→48. AEC
cannot subtract content that isn't in its reference, so music
energy at 4-8 kHz passes through aec_output uncancelled. Wake-word
phonemes live in roughly this band. Music masks them.

The user with audio-trained ears caught this listening to ref.wav
before any measurement was done — "this sounds pixel-crushed."

### The fix (current production)

Installed `libasound2-plugins`, added one line at the top of
`/etc/asound.conf`, rendered from `/var/lib/jasper/audio_quality.env`
by `jasper-render-asound-conf`:

```
defaults.pcm.rate_converter "samplerate_medium"
```

This replaces ALSA's linear interpolator with libsamplerate's
`SRC_SINC_MEDIUM_QUALITY` by default for every `plug:` and
`plughw:` rate conversion on the system. `/system/` can flip the
setting to `samplerate_best` when critical-listening CPU cost is
acceptable. Effects:
- shairport-sync's 44.1→48 write to plughw:Loopback now uses
  libsamplerate sinc conversion
- bluealsa-aplay's rate conversion (if any) uses the same configured
  converter
- `pcm.jasper_ref`'s plug wrapper (in case it ever does rate
  conversion) uses the same configured converter
- Same fix benefits the speaker playback chain too — music quality
  is incidentally improved

Historical cost for `samplerate_best` on Pi 5: ~3-5% of one A76
core on the resampler thread, ~15 MB resident memory. The current
`samplerate_medium` default is expected to be cheaper while keeping
the speech/AEC band intact.

Both the `libasound2-plugins` install and the rate_converter line
are now baked into `deploy/install.sh` + `deploy/alsa/asoundrc.jasper`
so fresh deploys keep the fix.

### Why not CamillaDSP for the resampling

Considered. The "elegant" alternative would be to have CamillaDSP
handle 44.1→48 internally via `capture_samplerate: 44100`,
`samplerate: 48000`, `resampler: AsyncSinc Balanced`. Same
quality (Rubato AsyncSinc Balanced has a ~-170 dB noise floor,
comparable to libsamplerate sinc-best).

**We already tried this.** The previous CamillaDSP config had
AsyncSinc Balanced doing 1:1 resampling on top of
`enable_rate_adjust=true`, which CamillaDSP itself flagged as
"Needless 1:1 sample rate conversion active" and which produced
the alternating +50/-485 ms sync errors documented in
[HEnquist/camilladsp#207](https://github.com/HEnquist/camilladsp/issues/207)
and [mikebrady/shairport-sync#1980](https://github.com/mikebrady/shairport-sync/issues/1980).

To use CamillaDSP for input resampling we'd have to disable
`enable_rate_adjust` (lose the snd-aloop virtual-clock drift
correction we depend on for stable AirPlay sync) and rely on
AsyncSinc Balanced's own ratio adjustment for drift. That's the
"Option B" path in the canonical CamillaDSP setups — viable in
principle but would re-enter shairport-sync#1980 territory and
require re-validating all the AirPlay sync work documented in
[HANDOFF-airplay.md](HANDOFF-airplay.md).

The libasound2-plugins route gets the same audio quality with
none of that risk. CamillaDSP option remains documented here for
future reference if there's ever a reason to revisit.

### Wake-rate measurements that led to this discovery

Phase 2 wake-rate test on 2026-05-19, 4 runs all at SHF_BYPASS=1
production state, music at "indicative" home listening level
playing Pink Floyd "Money" via AirPlay, 20 × 'Jarvis' utterances
from a phone speaker placed in the room:

| Run | AEC ON (aec_output) | AEC OFF (mic_ch1) |
|---|---|---|
| v2 (clean Jarvis vol) | 7/20 (35%) | 12/20 (60%) |
| v3 | 6/20 (30%) | 4/20 (20%) |
| v4 | 0/20 (0%) | 1/20 (5%) |
| v5 | 2/20 (10%) | 4/20 (20%) |
| **Total** | **15/80 (19%)** | **21/80 (26%)** |

Within-capture A/B is clean (both files derived from identical
mic input, only the bridge processing differs). AEC OFF won 3 of
4 runs and tied the 4th. AEC ON was never significantly better.
This *contradicted* the Phase 1 ERLE measurements (which showed
the bridge attenuating −15 dB speech-band), motivating the deeper
investigation into *what* the bridge was actually attenuating. The
answer: it was over-suppressing both echo AND wake utterances
because its reference signal was missing the upper-frequency
content needed to cancel echo cleanly.

Whether the libasound2-plugins fix moves the wake-rate numbers
back into "AEC helps" territory is an **open empirical question**
as of 2026-05-19. Re-running Phase 2 with the fix in place is the
next-step validation. Tools to do that re-test are in
[`scripts/aec-erle-record.sh`](../scripts/aec-erle-record.sh),
[`scripts/wake-rate-test.sh`](../scripts/wake-rate-test.sh),
[`scripts/aec_erle_analyze.py`](../scripts/aec_erle_analyze.py),
and [`scripts/_offline_wake_count.py`](../scripts/_offline_wake_count.py).

### What's still unknown

- **Does fixing the resampler make AEC a net positive again?** Open.
- **The chip HPF was bypassed in the then-production software profile — by design.**
  We initially flagged the measured ~6 dB/oct rolloff in `mic_ch1`
  (vs the −24 dB/octave a documented 4th-order Butter at 125 Hz
  should give) as a mystery. Verified 2026-05-19 against two
  independent literature reviews of the v3.2.1 User Guide +
  Programming Guide: the chip HPF lives *inside* the SHF block.
  `SHF_BYPASS=1` in the software fallback bypasses the entire SHF
  block, which means the HPF is *not* applied in that fallback even
  though `AEC_HPFONOFF=2` is set. The 6 dB/oct slope we measured is the
  **MEMS capsule's natural mechanical low-frequency rolloff** (per
  TDK InvenSense AN-1112: "the low frequency roll-off below the
  lower −3 dB point is first order"). No firmware anomaly; no bug
  to file. The recommended chip-AEC profile now gets real chip HPF
  action by running `SHF_BYPASS=0` with outputd feeding the XVF USB-IN
  reference. The software fallback keeps a host-side HPF instead.
- **Phase 2 testing methodology improvements.** Phone playback volume
  drifted between v2 and v3-v5 (the operator accidentally adjusted
  it), which produced a lot of the run-to-run variance. Future
  Phase 2 runs should pin phone volume via a setup checklist.

### Subtleties worth knowing (literature review 2026-05-19)

These come from a thorough review of the XVF3800 v3.2.1 docs +
Seeed README + community signals against our actual chip state.
None of them change our architecture — but all of them have
mis-led prior thinking at some point.

- **The chip HPF is inside the SHF block.** Both reviewers
  independently confirmed this. The Programming Guide §4.2.1
  states `SHF_BYPASS=1` produces "the raw (but amplified)
  microphone signals" on output channels — i.e. HPF, AEC, BF, NS,
  AGC all skipped. The `AEC_*` parameter prefix and the
  auto-generated `shf_aec_cmds.yaml` file corroborate that the
  HPF is part of the licensed Philips BeClear SHF library.
- **`AUDIO_MGR_OP_R` category 7 is dual-purpose.** Source N can
  mean either "AEC residual for mic N" (when `AEC_ASROUTGAIN=0`)
  OR "ASR output for beam N" (when `AEC_ASROUTGAIN > 0`). The
  chip-AEC profile's `AEC_ASROUTGAIN=1.0` (Seeed default) means cat-7
  routes output the ASR variant; the software fallback's
  `SHF_BYPASS=1` makes both interpretations moot. If we ever need to
  verify the HPF curve in isolation, we'd set `AEC_ASROUTGAIN=0` first.
- **There is no mux tap between HPF and AEC.** The chip exposes
  the input to SHF (cat 3 = amplified-with-system-delay) and the
  output of AEC (cat 7 = AEC residual or ASR), but nothing in
  between. Verifying HPF behavior in isolation requires the
  **difference of two captures** at different `AEC_HPFONOFF`
  settings, not a single direct tap.
- **Seeed channel layouts under `SHF_BYPASS=0` (not the software fallback config).**
  Reference for future debugging: stock 2-ch USB firmware emits
  ch 0 = conference-tuned beam, ch 1 = ASR-tuned beam (both
  fully processed). Seeed's 6-ch USB firmware (which we use)
  adds ch 2-5 = raw mics 0-3 (mux category 1, pre-amplification).
  None of this matters in the `SHF_BYPASS=1` software fallback — the
  bypass means all of ch 0/1 carry raw-amplified mic data regardless
  of mux routing — but it is exactly what the `xvf_chip_aec` profile
  relies on.
- **The "8-hour audio watchdog" only applies to the XK-VOICE-SQ66
  dev kit**, not the Seeed ReSpeaker (which is a licensed
  production XVF3800 device). Per User Guide §2.1: "Licensed
  production XVF3800 devices do not have this restriction."
  External "Google research" sources have been seen claiming this
  is a runtime concern on Seeed boards and recommending
  `SHF_BYPASS=1` as a workaround. That recommendation was correct
  for the software fallback but for the *wrong* reason — the fallback
  uses SHF_BYPASS=1 to skip chip AEC when the USB-IN reference is not
  armed, not to avoid a non-existent watchdog.
- **`PP_AGCTIME` (slow constant) is the right name; `PP_AGCALPHASLOW`
  is fabricated.** Some external analyses cite the latter; it
  doesn't exist in the User Guide. Confirmed on our device:
  `PP_AGCTIME=0.9` (matches doc), `PP_AGCFASTTIME=0.1` (doc claims
  0.6 — may be a firmware difference or a Seeed override).
- **DO NOT use `SAVE_CONFIGURATION`.** Some external sources
  recommend it for persisting tuning changes. Per memory note
  `project_xvf_alsa_mixer_mute_trap.md` + this doc, it's a brick
  hazard on certain firmware versions (respeaker repo issue #8).
  Our pattern: chip params reset on boot, `jasper-aec-init`
  reapplies them — that's the safe path.

### Chip-pipeline-only alternative considered + rejected

External literature occasionally recommends "trust the chip's
internal pipeline, drop the host-side WebRTC AEC." That would
mean `SHF_BYPASS=0` + taking ch 0 (full conference output) as
the wake-word input.

We rejected this on two grounds:

1. **Topology mismatch:** the chip's AEC pipeline assumes the
   chip drives the speaker via its own codec, which we do NOT
   do — speakers are driven by a separate USB DAC (Apple dongle).
   Per User Guide §4.2.1, `AEC_FAR_EXTGAIN` auto-mirrors the
   host's USB-OUT volume; in our topology it parks at −40 dB
   internally, sabotaging the chip's own AEC reference. We
   measured ≤2 dB attenuation across every configuration tested.
   Documented at length in "What we found about chip-side AEC in
   our topology" below.

2. **Direct empirical evidence:** Phase 2 wake-rate test 2026-05-19
   included a run with `SHF_BYPASS=0` (chip pipeline fully
   engaged). Result: 15% wake-rate on both AEC ON and AEC OFF —
   *worse* than `SHF_BYPASS=1` runs (35% and 60% respectively).
   So even before the resampler fix, the chip pipeline was worse
   than our hybrid.

The recommendation is sound for the chip's intended geometry
(chip-driven speaker). It's wrong for ours, **for the variants
tested here.** Future sessions should not re-litigate this without
first measuring under the new resampler-fix conditions.

**What was NOT tested in either rejection:** Option D's variant —
feed mono music to the chip's USB-IN as the AEC reference signal,
then read its hardware-AEC'd mic stream. The 2025 dongle-topology
test had no USB-IN reference. The 2026-05-19 wake-rate test ran
with `SHF_BYPASS=0` but also without a USB-IN reference (chip was
running its adaptive filter blind). Option D specifically supplies
the reference signal the chip's AEC was designed to consume and
uses the chip's USB Adaptive Mode PLL to share clock between mic
and reference. That variant was tested on 2026-05-29 and produced a
**positive lab result** when JTS fed the chip via direct source fanout.
Current status: no-USB-IN variants remain rejected; USB-IN Option D is
viable but not productionized. The test record and next steps live at
[CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md).

---

## Bridge ref starvation bug — fixed (2026-05-19)

After the resampler fix above shipped, listening tests on the
bridge's debug-record `ref.wav` revealed two more reference-signal
bugs in series. Both are now fixed but worth documenting because
they invalidate every AEC-side measurement made before this date.

### The mechanism

The bridge's `_aec_loop` consumed reference frames from `ref_q`,
which is filled by `_ref_thread` reading from ALSA at 48 kHz
stereo. The two threads were ostensibly running at 50 Hz each (one
20 ms frame per iteration). They are not.

ALSA negotiates the bridge's requested `periodsize=960` up to
`1024` (to match dsnoop's underlying period). With buffer_size =
4× period, `pcm.read()` delivers **two 1024-frame periods
back-to-back every ~40 ms** — a 25 Hz "burst" cadence at the ALSA
layer, despite the bridge requesting smooth 50 Hz. Mic delivery
remains a smooth 50 Hz via the sounddevice callback.

Result: on alternating main-loop iterations, `ref_q` is empty
because the next burst hasn't arrived yet. The original code
substituted `silence` (a zero-frame) on those iterations. A first
fix attempt drained the newest frame and reused it on the empty
iteration; this produced byte-identical duplicate frames. Each
failure mode is independently audible and measurable.

### The three layered fixes

| Layer | Symptom | Fix | PR |
|---|---|---|---|
| ALSA plug 44.1→48 | ~12 dB HF loss in ref | `libasound2-plugins` + configured libsamplerate rate_converter (`samplerate_medium` default, `samplerate_best` optional) | [#150](https://github.com/jaspercurry/JTS/pull/150) |
| `_aec_loop` empty-queue fallback to `silence` | 50 % of AEC frames received zero ref | Carry-forward `last_ref_bytes` | [#154](https://github.com/jaspercurry/JTS/pull/154) |
| `_aec_loop` drain-newest discarded burst frames | 50 % of frames byte-identical duplicates | Consume one frame per iteration in order | [#157](https://github.com/jaspercurry/JTS/pull/157) |

### Diagnostic methodology

The bug was hidden because none of these failure modes raised log
warnings. The `drained > 5` warning guards the *over*-full case;
there was no instrumentation on the *under*-full path. Discovery
required listening to `ref.wav` (the user's audio-trained ears
caught a "25 Hz fan-like pulsing" with the original code and "50 Hz
buzzing" with the PR #154 fix) and validating with frame-by-frame
WAV analysis:

- Original: 49.6 % silent frames in `ref.wav`, 98 % at one parity
- PR #154: 0 % silent but 50.1 % byte-identical consecutive pairs
- PR #157: 0 % silent, 0 % duplicates — verified working

The verification script `scripts/verify-ref-no-silence-bug.sh`
captures 30 s of `ref.wav` via the bridge's debug-record mode and
returns a pass/fail verdict. It also catches the duplicate-pair
regression implicitly (no carry-forward can ever produce
byte-identical duplicates with the new consumer).

### Visibility going forward

The bridge's periodic RMS log line now includes
`ref_starve=N` — the count of iterations in the 5 s window that
hit the empty-queue path and carried forward. Under normal
operation this should remain non-zero (the underlying ALSA
bursting is still there, only the consequence is now harmless)
but stable. A sudden rise indicates ref delivery has degraded.

### Why this destroys all pre-fix AEC measurements

Every "AEC ON" wake-rate measurement up to 2026-05-19 ran with
the bridge feeding AEC3 a reference that was 50 % silent (and
half-bandwidth in the speech band on top of that). The adaptive
filter could not converge. The "AEC OFF" / chip-direct
measurements remain valid because the bridge is bypassed in
those.

The wake-rate test protocol in `scripts/wake-rate-test.sh` is
unchanged. Re-running it is the next step. Pre-fix data is in
`logs/wake-rate/SHF_1_v*` for reference but should be cited only
for the "AEC OFF" leg.

---

## NS aggressiveness + AGC1 dynamic gain — 2026-05-20 findings

Once the bridge ref-starvation bugs were fixed (above), wake-rate was
still well below pre-degradation baselines. A forensic per-utterance
analysis surfaced two further tuning knobs that move the needle.
**These are now production defaults.**

### What changed in the binding + bridge

`jasper_aec3/src/aec3_binding.cpp` constructor now accepts:
- `ns_enabled: bool` (default `true`) — toggle the post-AEC noise-
  suppression stage entirely
- `ns_level: str` (default **`"low"`**, was `"moderate"`) — one of
  `low / moderate / high / very_high` (Trixie's
  `libwebrtc-audio-processing-1` v1.3-3 doesn't expose `kVeryLow`)
- `agc1_enabled: bool` (default `false`, **enabled in production via
  env**) — turns on WebRTC AGC1 in `kAdaptiveDigital` mode for
  per-utterance dynamic gain
- `agc1_target_dbfs: int` (default `9`) — peak target for AGC1
- `agc1_max_gain_db: int` (default `18`) — maps to WebRTC's
  `compression_gain_db` (soft-knee compressor parameter, 0–90 dB
  range). Note: this is *not* a "max gain ceiling" despite the
  variable name; the name is retained for backwards compat. WebRTC
  has no attack-time / release-time parameter at all.

`jasper/cli/aec_bridge.py` reads matching env vars and wires them
through. Existing knobs (`JASPER_AEC_MIC_GAIN_DB`, `JASPER_AEC_AGC2`,
etc.) preserved for backwards compat; AGC2 is a documented no-op on
this binding/libwebrtc combination and should stay off.

### Why these two knobs

**NS level**: openWakeWord (the `speech_embedding` CNN + small Dense
head) is brittle to spectral distortion. AEC3's post-cancellation
noise-suppression makes per-frequency-band gain decisions; more
aggressive NS = more HF speech-consonant features masked out =
more silent misses on Jarvis utterances that humans hear cleanly.
The 2026-05-20 NS sweep on the test-1 capture:

| `JASPER_AEC_NS_LEVEL` | Wake rate | Notes |
|---|---|---|
| `off` (NS disabled) | 5/20 (25%) | tied for best |
| **`low`** | **5/20 (25%)** | **new default** |
| `moderate` (prev default) | 4/20 (20%) | baseline |
| `high` | 3/20 (15%) | worse |
| `very_high` | 2/20 (10%) | worst |

`low` ties `off` for wake rate but retains some residual noise
suppression for non-detection paths, so it ships as the default.

**AGC1**: Static `MIC_GAIN_DB=+12` won 5/20 in the same sweep but
produced uneven output — manual listening showed some Jarvises
peak-clipped while others stayed quiet, depending on the AEC's
instantaneous output level (which varies with music content). AGC1
in `kAdaptiveDigital` mode levels per-utterance, fixing the
"overblown vs quiet" inconsistency. Wake rate is the same as
static +12 dB (5/20), but the output is uniformly ~RMS 1213 across
every utterance, no clipping, no inter-utterance pumping. AGC1's
`target_dbfs` / `max_gain_db` parameters had minimal effect in our
sweep (limiter dominates); the defaults (9/18) are inherited from
HA Voice PE's same-purpose configuration.

### Forensic methodology that surfaced this

Per-utterance analysis on the AEC output, NOT the wake-rate summary.
The full methodology is documented in memory note
`project_aec_wake_rate_forensic_methodology.md`. Key principle: don't
trust `_offline_wake_count.py`'s cross-correlation utterance finder
alone — it picks the top-N normalized peaks globally, which can
include pre/post-track noise. Use the AEC ON output's energy envelope
(200 ms windows, ≥3s spacing, t=10-118s range) to locate the real
Jarvises, then extract ≥2s pre / 4s post around each peak (the model
needs ~1-2s of trailing context to commit a wake score), then run the
model in isolation with 4s silence padding to flush state between
utterances. Save WAVs and listen — the operator's ear catches
misalignment and audio degradation that the analyzer doesn't.

### Sweep tooling (one-off scripts retained for next investigation)

The 2026-05-20 sweep used three Python scripts that lived in `/tmp`
during the session; they're not in `scripts/` because each is a
single-purpose forensic tool, but if a future session needs the same
analysis they can be reconstructed from the memory note or pulled
from the session transcript. The pipeline:

1. Run `wake-rate-test.sh test-N` to capture `(mic, ref, aec_output)`
   into `logs/wake-rate/<session>/test-N/`
2. Offline replay through `jasper_aec3.Aec3(...)` with arbitrary
   config — the binding's expanded constructor (above) enables
   sweeping NS / AGC1 settings without rebuilds
3. Run `openwakeword.Model.predict()` on each output to count fires
4. Extract per-utterance WAVs for manual listening sanity-check

The same `(mic, ref)` pair was used across all 23 NS×gain configs in
the 2026-05-20 sweep — eliminates phone-playback variance entirely,
since every config sees identical physical input.

### Open question

14 of the 20 utterances in test-1 stayed at 0.001 confidence across
ALL 23 sweep configs (every NS level, every gain, AGC1 on/off). NS
and AGC1 reach 6/20 → 5/20 worth of Jarvises (≈10% absolute
improvement). The remaining 14 fail for reasons NS/AGC1 don't touch
— hypotheses include the openWakeWord head being miscalibrated for
TTS Jarvis in music ([Agent 1 research, 2026-05-20 session]) and the
phone playback level being lower than pre-fix baselines. Open
research directions:
- DTLN-aec as a learned echo-residual-suppressor that replaces AEC3's
  destructive nonlinear stage ([breizhn/DTLN-aec](https://github.com/breizhn/DTLN-aec); MIT licensed; Pi-validated via
  [PiDTLN](https://github.com/SaneBow/PiDTLN))
- Retrain the openWakeWord head on AEC-passed Jarvis positives via
  `automatic_model_training.ipynb` (free Colab T4, ~2 weekends of
  work; the path most likely to recover the remaining 14)

---

## Wake-rate forensic test methodology — 2026-05-20

This section captures the test methodology that surfaced the
NS-level and AGC1 findings above. It's documented here (in the
repo) so future sessions can reproduce the analysis without
re-deriving it. Cross-referenced as user memory note
`project_aec_wake_rate_forensic_methodology.md`.

### Why a forensic methodology was needed

`scripts/wake-rate-test.sh` gives a summary number (e.g. "AEC ON
20% wake rate"). It doesn't tell you *which* Jarvises succeeded or
failed, *why* the failures happened, or whether the
cross-correlation utterance finder was even pointing at real
Jarvises. The 2026-05-20 investigation found multiple non-obvious
gotchas the summary numbers don't surface.

### The pipeline (one capture, many analyses)

1. **Capture once.** Run `scripts/wake-rate-test.sh test-1` with
   music + the wake-test-track playing. Produces
   `logs/wake-rate/<session>/test-1/{aec-on,aec-off,reference}.wav`
   + a `result.txt` summary. AEC ON and AEC OFF are produced from
   the *same* physical mic capture (the bridge debug-records both
   pre- and post-AEC streams), so they're time-aligned.

2. **Locate utterances by energy envelope, not by xcorr.**
   `_offline_wake_count.py`'s cross-correlation utterance finder
   picks the top-N normalized peaks globally; when fewer than N
   actual Jarvises are in the capture window (pre-tap noise,
   early/late timing), it picks random music transients as
   "utterances" and the per-utterance scoring becomes unreliable.
   Instead, compute the energy envelope of the AEC ON output
   (200 ms windows; ≥3 s spacing; restrict to t=10–118 s to
   exclude pre/post-track noise). With music suppressed by AEC,
   the loudest 200 ms windows ARE the Jarvises. The xcorr finder
   gave misaligned-by-up-to-5-seconds results on test-1, which
   alone moved one Jarvis from "silent" to "fires" once corrected.

3. **Extract wide windows.** ≥2 s pre / 4 s post around each
   envelope peak (6 s total file). The wake model needs ~1–2 s of
   trailing context after the word to commit a wake score; tighter
   windows produce false negatives at the analysis layer that
   weren't real misses in production.

4. **Run the model in isolation.** For each utterance window,
   pad with 4 s of silence at the start (2× 2-s `SIL_2S`) and 2 s
   at the end, then run `openwakeword.Model.predict()` chunk-by-
   chunk. The silence prefix flushes any state from the previous
   utterance's call.

5. **Always save WAVs for ear sanity-check.** The operator's ear
   catches misalignment, truncation, and audio degradation that
   the analyzer doesn't. Several methodology fixes in the
   2026-05-20 session came from "this file sounds silent" /
   "this file sounds clipped" observations on extracted WAVs.

### The aligned A/B technique

Use the AEC ON envelope to find Jarvis wall-clock times, then
extract from BOTH `aec-on.wav` AND `aec-off.wav` at those *same*
wall-clock times. The xcorr utterance-finder is independent across
the two files and will produce different finds; the energy
envelope on AEC OFF will find music peaks (since music isn't
suppressed there), not Jarvises. Always use the AEC ON envelope
for both legs.

### The offline AEC replay technique

The AEC3 binding's expanded constructor (NS level, AGC1 params,
etc.) supports offline replay: load `(mic_ch1.wav, ref.wav)` from
a debug-record capture, instantiate `jasper_aec3.Aec3(...)` with
arbitrary config, call `.process(mic_bytes, ref_bytes)`, and you
have an AEC output that would have been produced live under that
config. Saved one or more `aec-output-<config>.wav` files per
sweep iteration. **One physical capture → unlimited AEC
configurations tested in minutes** — eliminates phone-playback /
room / music variance across configs entirely. The 2026-05-20
sweep tested 23 NS×gain×AGC2 configs in ~10 minutes from a single
test-1 capture.

### What we extracted into reusable infrastructure

The 2026-05-20 sweep scripts (`jts_aec_sweep.py`,
`jts_agc1_sweep.py`, `jts_extract_v2.py`,
`jts_aligned_ab_v2.py`) lived in `/tmp` during the session. They
weren't promoted to `scripts/` because each is a single-purpose
forensic tool. If a future session needs the same analysis,
reconstruct them from the memory note's documented pipeline.

### What's verified vs unverified by this methodology

- ✅ Verified: which AEC config produces best per-utterance scores
- ✅ Verified: which Jarvis utterances are intrinsically silent
  vs config-recoverable
- ❌ Unverified: false-positive rate per AEC config (we measure
  wake scores at the 20 Jarvis locations, not at music-only
  stretches). Counting peaks ≥0.30 in inter-utterance windows
  would give the per-config FP rate — quick add-on for any future
  sweep.

---

## Open work streams — 2026-05-21 roadmap

Forward-looking inventory of paths we've investigated but haven't
shipped, ordered roughly by value-per-effort. Each entry includes
what it is, why it's on the list, cost, risk, and prerequisites.
Updated as paths get picked up or new ones surface.

### A — livekit-wakeword (parked 2026-05-21 — modest gains, not worth training)

**Status:** Explored on 2026-05-21 via the LAION BUD-E
`hey_buddy_en_medium.onnx` model as a no-training smoke test
(conv-attention head, same architecture livekit-wakeword would
train for Jarvis). **Verdict: real but marginal architectural
improvement. Not worth a weekend of training given today's
empirical evidence.** Keeping on the list because the rationale
still has theoretical merit and the runtime compatibility is
proven — if priorities ever shift, the door is open.

**What it is:** Train a "Jarvis" wake-word model with LiveKit's
training pipeline. The head is 1D-conv + multi-head self-attention
instead of openWakeWord's flatten + Dense MLP; the audio front-end
(mel-spec → Google `speech_embedding` CNN → 16×96 feature matrix)
is identical, so the output ONNX is openWakeWord-runtime-
compatible — drop into our existing wake-word loader with no
infrastructure change. **Verified 2026-05-21: BUD-E
`hey_buddy_en_medium.onnx` loads in our openWakeWord runtime on
Pi 5 and produces predictions.**

**Why originally on the list:** The 0.997/0.001 bimodal behavior
we observe (every Jarvis either fires confidently or silently
misses, nothing in between) is the textbook failure mode of an
uncalibrated flatten + BCE sigmoid head. Conv + attention gives
the network temporal inductive bias the flatten step destroys,
and is more robust to spectral distortion in the academic
small-footprint KWS literature. The hope: recover some of the
14/20 silent-in-every-config Jarvises from the 2026-05-20 sweep.

**What we found empirically (2026-05-21, same music level, same
acoustic conditions, MIC_GAIN_DB=0 for both runs):**

| | `jarvis_v2` (openWakeWord flatten+Dense) | `hey_buddy_en_medium` (livekit-wakeword conv+attention) |
|---|---|---|
| Max model score | 0.997 | 0.702 |
| AEC ON fires @ threshold 0.30 (per apples-to-apples envelope rescore) | 4 | 3 |
| AEC OFF fires @ threshold 0.30 | 5 | 1 |
| Frames in 0.10-0.30 ("near-miss") | ~5 each | OFF: 10, ON: 17 |
| AEC ON > AEC OFF? | No (5 OFF > 4 ON) | **Yes (3 ON > 1 OFF)** |

**Two genuine findings from the experiment:**

1. **Conv-attention IS somewhat more AEC-robust.** For Hey Buddy
   the AEC ON leg actually beats AEC OFF (3 vs 1) — the opposite
   of Jarvis (4 ON vs 5 OFF). Two specific moments where the
   conv-attention head fired through AEC distortion that the
   chip-direct OFF leg missed entirely: t=20.96 (ON 0.638 vs OFF
   0.062) and t=84.16 (ON 0.332 vs OFF 0.103). The architectural
   argument has empirical support, just for AEC-specific
   robustness, not overall sensitivity.

2. **But LAION's `hey_buddy` is much less sensitive overall**
   than fwartner's `jarvis_v2`. Max scores of 0.7 vs 0.99, many
   more frames in the [0.10, 0.30] "near-miss" band. Different
   training data, different calibration. At identical thresholds,
   `jarvis_v2` produces more confident, less-borderline detections.
   The two factors (better topology, less sensitive training)
   roughly cancel out in our data.

**Why this might still be worth pursuing later:**

The Hey Buddy test compared a *different model* with a *different
phrase* in a *different architecture*. A true same-phrase
architectural A/B (training a Jarvis-specific livekit-wakeword
model and running it head-to-head against fwartner's jarvis_v2)
would tell us whether the conv-attention head's specific
AEC-robustness advantage transfers — and whether the LAION
calibration issue was the dominant variable, not the architecture
itself. If a future session has time to invest in training, this
question remains genuinely open. The runtime compatibility is
proven; the only blocker is the training step.

**Cost (if revisited):** ~1 weekend. Training on free Colab T4
(~1 hour active); data pipeline reuses Piper TTS positives +
ACAV negatives + RIRs; the model exports as `.onnx`, scp into
the JTS, point `JASPER_WAKE_MODEL` at it, restart `jasper-voice`.

**Risk (if revisited):** Low. Reversible in seconds (point
`JASPER_WAKE_MODEL` back to `jarvis_v2.onnx`). The 100× FPPH /
17% recall numbers on LiveKit's PyPI page are vendor self-
reported on their test set; absolute multipliers will differ on
ours. Architectural improvement is theoretically durable; the
empirical magnitude per the 2026-05-21 BUD-E test is modest.

**Infrastructure that landed and stays useful** (regardless of
whether livekit training is pursued):
- `scripts/wake-rate-test.sh` and `scripts/make-wake-test-track.sh`
  now accept `PHRASE` and `MODEL` env vars (PR #167). Any future
  cross-model or cross-phrase A/B uses the existing harness with
  one extra env var.

**To repeat the smoke test in the future**:
```sh
curl -L -o /tmp/hey_buddy_en_medium.onnx \
  "https://huggingface.co/laion/bud-e_wakeword-models_livekit-wakeword/resolve/main/en_medium/hey_buddy_en_medium.onnx"
scp /tmp/hey_buddy_en_medium.onnx pi@jts.local:/tmp/
ssh pi@jts.local "sudo install -m 0644 /tmp/hey_buddy_en_medium.onnx /var/lib/jasper/wake/"
```
(The model was installed on 2026-05-21 for the smoke test, then
removed to keep `/var/lib/jasper/wake/` clean. Re-installing takes
seconds when a future session wants to retest.)

**Prerequisites:** None. Can start anytime; not currently planned.

**Refs:** [livekit-wakeword PyPI](https://pypi.org/project/livekit-wakeword/),
[piper-sample-generator](https://github.com/rhasspy/piper-sample-generator)
(LibriTTS-R, 904 voices — not 2,456 as some external sources cite).

### B — Per-AEC-config false-positive rate measurement (tiny)

**What:** Add a step to the forensic methodology: count wake-score
peaks ≥0.30 in the inter-utterance windows (music-only stretches)
of each `aec-<config>.wav`. Gives a per-config FP rate alongside
the existing per-config wake rate.

**Why on the list:** The 2026-05-20 sweep didn't measure FP rate
per config. Dual-stream wake (C below) crucially depends on this:
if AEC OFF has high FPs from music, OR-gating with it gives best-
of-both-worlds wake rate but worst-of-both-worlds FP rate. We need
the FP number before committing to C.

**Cost:** ~1 hour. Extension to existing sweep scripts.

**Risk:** None — measurement only.

**Prerequisites:** None. Can run on existing test-1 data without
recapturing.

### C — Dual-stream wake-word with reference-coherence gating

> **Status: the OR-gate half shipped (PR #191), default-off.** Running
> the wake-word detector on both the AEC ON stream (post-bridge UDP
> output) and the AEC OFF stream (chip-direct mic, no AEC processing)
> and OR-ing the detections is live — `jasper/wake_legs.py` registers
> the `chip_direct`/`off` leg on UDP `:9877` as a `wake_input=True`
> production leg alongside `aec3`/`on`, and `WakeLoop` OR-gates fires
> across every `wake_input` leg with a shared refractory window. What
> remains open is the coherence-gating refinement below — suppressing
> an OFF-leg fire when mic-vs-reference correlation is high.

**What (open part):** Gate the OR with a coherence check: if
mic-vs-reference correlation at the detection moment is high (AEC's
adaptive filter coefficients indicate strong self-talk), suppress.

**Why on the list:** Test-1 aligned A/B data (in this doc above)
shows AEC ON and AEC OFF catch *mostly disjoint* sets of Jarvises:

| | AEC ON unique fires | Both fire | AEC OFF unique fires | Union |
|---|---|---|---|---|
| Test 1 | 3 (j-03, j-08, j-12) | 1 (j-10) | 4 (j-05, j-15, j-18, j-20) | **8/20 (40%)** |

The union is **+15 percentage points over the better single leg
(25%)**. That's the highest single-experiment wake-rate gain on
the table, IF the FP cost is acceptable (option B above bounds
it).

The coherence-gating idea has named patent prior art —
[Sonos US 11,769,505](https://patents.google.com/patent/US11769505)
explicitly describes selective AEC activation based on wake-word
detection; [Amazon US 12,361,942](https://patents.google.com/patent/US12361942)
uses variable-step-size (Vss) trends in the AEC adaptive filter
to distinguish user wake-word from wake-word-in-playback. The
patterns exist in commercial smart speakers but have no canonical
open-source implementation; we'd build it.

**Cost:** ~3 days. The wake-word loop needs a second stream
ingestion path; the AEC adaptive-filter state needs to be exposed
from the binding (it's there internally); the gate logic is a
few hundred lines of Python.

**Risk:** Medium. Failure mode: more FPs from music. Mitigated by
option B and by the coherence gate.

**Prerequisites:** Option B (FP-rate measurement) to bound the
worst-case FP cost.

### D — Chip-AEC with USB-in reference topology

> **Status: positive lab result; production profile shipped.** On 2026-05-29
> we proved the XVF3800's on-chip AEC can do useful cancellation in
> JTS's external-DAC topology when JTS feeds the chip a clean USB-IN
> reference. Fresh installs use `JASPER_AUDIO_INPUT_PROFILE=auto`, which
> resolves to chip-AEC only when the detected XVF mic profile has a
> validated chip beam plan and the output DAC profile is supported /
> calibrated; it falls back to software AEC3 when chip-AEC is
> unavailable. The wake-corpus recorder can
> still enter reversible chip-AEC comparison profiles that use outputd
> direct source fanout, apply the volatile chip profile, and capture
> explicit `chip_aec_150` / `chip_aec_210` legs for validation.
> Full test record and current lab recipe live in
> [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md). The checked-in
> `scripts/chip-aec-*` helpers are still lab infrastructure; teardown
> fully reverts. User-authorized carve-out from the "Architecture is
> fixed" policy in [AGENTS.md](../AGENTS.md).

As of 2026-07-09, corpus chip-AEC mode has its own capture-plan
contract. `JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1` is enough for
`jasper-aec-bridge` to emit the dedicated `chip_aec_150` and
`chip_aec_210` UDP legs on `:9887/:9888`; it no longer depends on
production per-beam wake-device vars being configured. Bridge stats
schema v2 publishes `active_capture_plan` with the wake-corpus
`plan_id`, emitted legs, corpus flags, beam plan, ports, and mic /
reference identity summary. The recorder uses that stats payload, not
env inference alone, to block clip start when the active bridge no
longer matches the stored session plan.

**What:** Feed mono music to the XVF3800's USB-in left channel as the
AEC reference signal, then read the chip's hardware-AEC'd USB capture
stream. The winning lab shape uses the current 6-channel firmware:
category 7 ASR output (`AEC_ASROUTONOFF=1`), fixed gated beams at
`150°/210°`, and `AEC_AECEMPHASISONOFF=2`. A future production version
should fan out one rendered source buffer directly to both the DAC and
XVF3800 USB-IN reference. Do not ship the old `plug:jasper_capture`
feeder as the architecture; it was the source of the apparent drift.

**Why on the list:** The original chip-AEC rejection (documented
in "What we found about chip-side AEC in our topology" and
"Chip-pipeline-only alternative considered + rejected" below) was
based on a *different* topology — the chip driving its own
codec/speaker, where `AEC_FAR_EXTGAIN` auto-mirroring host UAC
volume sabotaged the reference. With music routed via USB-in as
a known-amplitude reference (the proposed new topology), that
specific failure mode goes away. The May 2026 chip-AEC empirical
test (15% wake rate with `SHF_BYPASS=0`) ran without a USB-in
reference at all and is not directly applicable. **This is the
"canned worms" path — re-opening a previously-closed investigation
with a new variable.**

**Cost:** Now mostly integration work rather than feasibility research.
Expected next slice: direct source fanout + a lab/recorder capture mode
for the chip leg, followed by a wake/corpus validation pass. The old
multi-month "maybe clocks make this impossible" concern is retired for
the direct-fanout topology.

**Risk:** High. Topology change touches CamillaDSP routing,
shairport-sync output target, firmware variant, and the bridge
service unit. Reversible but with friction.

**Prerequisites:** Read the chip-AEC delay-tolerance subsection
below before scoping. Don't start the work without the delay
budget.

**2026-05-29 live gate update:** Option D moved from "maybe plausible"
to positive lab result. The first fan-in-era chirp baseline measured
ref→mic around `181-209 ms`, outside the chip's direct
`AUDIO_MGR_SYS_DELAY` clamp (`[-64,+256]` samples), but that was the
old delayed feeder harness. A direct dual-playback harness then showed
the source-fanout topology is clock-stable (`~1 ppm` drift over 15
minutes), and controlled direct A/B showed useful chip-AEC reduction
with `SHF_BYPASS=0`. The best double-talk/wake-shaped output was not
ch0 alone; it was category 7 ASR output with fixed gated `150°/210°`
virtual beams, especially the `150°` beam. `AEC_FAR_EXTGAIN=+3/+6 dB`
made results worse; `AEC_AECEMPHASISONOFF=2` improved the final
strength/edge sweep.

#### Chip-AEC delay tolerance — what the chip can and can't do

Researched 2026-05-21 to scope option D before any future commits.
Sources at end of subsection. The "out-of-sync" worry from prior
sessions turns out to conflate **bulk delay** (real concern, easy
fix) with **clock drift** (non-issue in the proposed topology).

**`AUDIO_MGR_SYS_DELAY` — the explicit bulk-delay knob:**

| Property | Value | Source |
|---|---|---|
| Type / unit | `int32`, samples at 16 kHz (62.5 µs/sample) | `jasper/xvf/xvf_host.py` command table; XMOS Tuning Guide §4 |
| Empirical accepted range | **−64 to +256** (values >256 silently clamp) | HANDOFF-aec.md line 1461, our 2025 sweep |
| Sign convention | positive delays the reference; negative delays the mic (used to fix acausal systems) | XMOS Tuning Guide §4 |
| Seeed default | `AUDIO_MGR_SYS_DELAY = 12` (≈0.75 ms) | respeaker/host_control README |
| XMOS-documented target | impulse-response peak within first 40 samples (≈2.5 ms) of the tail after compensation | XMOS Tuning Guide §4 |

**Adaptive-filter tail = 192 ms.** lib_aec runs a main filter plus
shadow filter; total tail length is 192 ms. Within that window the
LMS adapts taps freely without retuning `SYS_DELAY`. *Quality*
convergence (vs just "not divergent") needs the peak within the
first 40 samples of the tail; beyond that, ERLE degrades and
convergence slows. Beyond the 192 ms tail, the AEC has no
tracking — those echoes are unmodelled.

**Cross-clock drift is NOT a problem in the proposed topology**
(this is the key insight). The XVF3800 USB UA runs in **USB
Adaptive Mode**: a software PLL (`lib_sw_pll`) generates the
chip's internal MCLK *from* the USB host's SOF clock, and the mic
clock is locked to that same MCLK. In option D's topology (Pi USB
host driving both music-out *and* mic-in via the same XVF3800
device), mic and reference clocks derive from the same physical
timebase. No SRC needed for clock matching, no host-feedback-
endpoint negotiation, no long-term drift. This is the exact
topology XMOS designed the chip for.

> **DAC clock-domain dependency — "no drift" is conditional on the
> speaker sharing the mic's clock domain.** The clause above holds only
> because the *speaker* is a USB-SOF-disciplined device (the Apple
> USB-C dongle, an adaptive/synchronous UAC DAC). Chip-AEC needs
> **three** clocks frequency-locked, not two: the mic A/D, the chip
> USB-IN reference, **and the airborne echo** (the speaker's D/A
> conversion). The chip's Adaptive Mode PLL co-clocks the first two to
> the Pi USB SOF by design; the third is coherent only because the
> Apple dongle's D/A is *also* locked to that same USB SOF. The
> production fanout (`jasper-outputd`) relies on this with **no drift
> compensation anywhere** (integer 48k→16k decimator + a bounded
> queue that only drops periods on overflow), which is glitch-free
> only because every endpoint rides the one USB SOF.
>
> **Swapping the speaker DAC changes only the echo clock — so the whole
> question is "does the new DAC's D/A run in the mic's clock domain?"**
> Don't shortcut it from the board's marketing tier. The HiFiBerry
> DAC8x is **not** a self-clocked master (an early draft said so —
> wrong): the kernel overlay proves it's a *Pi-clocked I2S slave*,
> PCM5102A-class, no crystal. On Pi 5 its I2S clock (`pll_audio`) and
> the USB-SOF the mic rides are *different RP1 PLLs* — **but both
> descend from the one 50 MHz crystal, so they're frequency-coherent**
> (common-mode crystal error cancels in the ratio; a second draft's
> "separate PLLs → drift tens of ppm" claim was also wrong). So the
> DAC8x is **uncertain, lean coherent — not a confirmed break.** The
> residual risks are narrower than drift: a non-rational SOF divisor, or
> a ref→air *delay* that wanders over time past the chip's fixed bulk
> delay (0–500 ms) + 192 ms tail. Only measurement settles it. Software
> AEC3 (the fallback for unvalidated DACs) sidesteps the whole question — its
> reference is the digital `pcm.jasper_capture` tap and AEC3 handles
> render/capture clock mismatch in software; a DAC8x on AEC3 is a
> routing + re-tune at low/negligible risk, **recommended until that
> DAC passes the chip-AEC validation gate**.
>
> **The full decision procedure for evaluating any candidate DAC**
> (transport/clock-role classification, why "different PLL off the same
> crystal" is still coherent, the empirical gate, and the escape hatches
> — software SRO compensation, XVF-master I2S, synchronous-USB-only)
> lives in [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) "DAC
> clock-domain dependency — a methodology for evaluating any speaker
> DAC." Before committing a DAC for a chip-AEC build, run the gate
> there: measure ref→air→mic drift (and watch ref→air delay stability)
> while checking outputd chip-ref health via
> `/state.outputd.reference_outputs.chip_ref_writer` (`dropped_periods_due_to_full_queue`,
> `write_xrun_count`, `write_underrun_count`, `reference_sequence_lag`) and
> `event=outputd.chip_ref.queue_full` / `event=outputd.chip_ref.write_failed` —
> ≤~1 ppm clean ⇒ viable (the expected outcome given the shared crystal),
> tens of ppm or a wandering delay ⇒ needs compensation.

(USB-IN endpoint advertises 16 kHz S16_LE 2-channel only — verified
empirically on 2026-05-21 against firmware `ua-io16-6ch-sqr` v2.0.8,
and cross-referenced against the XMOS Datasheet (USB Audio Interface
section: "the XVF3800 audio sample rate can be either 16 kHz or
48 kHz **fixed at build time**" — UAC2 Adaptive Mode, one rate per
build). The runtime SRC documented in the datasheet is for the I²S
path only, not USB. Seeed ships only `ua-io16-*` configs across all
firmware variants. **Drive CamillaDSP's route to USB-IN at 16 kHz.**
The earlier draft of this section said "48 kHz to USB-in is fine —
chip transparently SRCs" — that was wrong, corrected
[CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md).)

**Failure modes:**
- **Acausal** (ref arrives *after* mic, negative effective delay):
  filter peak at tap 0, no useful tail → ≤2 dB attenuation. **This
  matches what we observed in the dongle topology** — the chip
  couldn't see the Apple dongle's playout at all, so effective
  delay was nonsense. Fix: more-negative `SYS_DELAY` to shift mic
  later. (The proposed option-D topology removes this failure mode
  entirely by routing music through the chip itself.)

> **Important caveat about the prior chip-AEC test result.** The
> 2026-05-19 `SHF_BYPASS=0` run that produced 15% wake rate (cited
> below in "Chip-pipeline-only alternative considered + rejected"
> as evidence against chip AEC) ran in the dongle topology — chip
> had NO USB-in reference signal at all. The chip's USB Adaptive
> Mode PLL only engages when USB-in audio is flowing as the AEC
> reference; with no reference, the PLL question doesn't even
> arise, and the chip's AEC was running blind. **So the 15%
> result tells us nothing about how chip AEC would perform in
> option D's topology** (where music IS routed through USB-in).
> The acausal-delay diagnosis above is the actual mechanism — and
> it doesn't apply to option D. Don't cite the May 2026 result as
> evidence against option D; the experimental conditions don't
> match.
- **Excessive positive delay** (peak >> 40 samples): filter still
  adapts but spends tail budget on bulk delay → less budget for
  room IR → poorer cancellation, slow convergence.
- **Beyond 192 ms tail**: silent partial cancellation. Early energy
  attenuated, late reflections pass through unmodelled.
- **`AEC_AECCONVERGED` flag is the canary.** In all failure modes
  above, the convergence flag stays at 0. If it won't flip to 1
  after 30 s of music playing, something's wrong with
  `SYS_DELAY`, `REF_GAIN`, or the routing. **Add this to
  `jasper-doctor` checks before any production deploy of option D.**

**Pi-side alignment plan (concrete, draft):**

Current fan-in-era measurements exposed two different paths:

- The old `plug:jasper_capture` feeder path measured about `181-209 ms`
  ref→mic before Pi-side compensation. The chip cannot eat that
  host/speaker delay on its own. A test-only upstream reference delay of
  `180 ms` left a residual `+114` sample chip delay, which is writable.
- The production-shaped direct source fanout path (one source buffer to
  DAC + XVF3800 USB-IN) removed the apparent clock drift and held about
  `~1 ppm` over a 15-minute run. This is the path to productionize.

Conclusion: if Option D graduates, do not rely on
`AUDIO_MGR_SYS_DELAY` to compensate a late feeder tap. Feed the chip at
the same source-fanout point as the physical DAC, then use
`AUDIO_MGR_SYS_DELAY` only for the small residual static offset.

Realistic bring-up sequence:

1. Add a CamillaDSP output route that delivers mono music to
   XVF3800 USB-in left channel at **16 kHz S16_LE stereo** (the only
   rate/format the endpoint accepts — see correction above). CamillaDSP
   does the 48 k → 16 k SRC internally with its AsyncSinc resampler.
   Right channel can be a copy of left or zero — the chip's AEC uses
   left only.
2. Use the smallest stable ALSA period (~5–10 ms) on the USB-in
   stream — caps host-side latency tightly. Target end-to-end
   host→chip-USB→mic < 16 ms (256 samples).
3. Re-measure outputd-reference-to-mic timing with chirp
   cross-correlation (`scripts/aec-probe-latency.sh`). If total is
   10–15 ms, set
   `AUDIO_MGR_SYS_DELAY` to compensate (positive value, 62.5 µs
   per sample) until the impulse-response peak lands at taps 5–30.
4. Do not assume coefficient-dump tooling exists in the local helper:
   `SPECIAL_CMD_AEC_FILTER_COEFFS` is not currently exposed by
   `jasper/xvf/xvf_host.py`. If coefficient inspection is needed,
   add the single command from XMOS documentation and validate it on
   hardware before relying on that workflow.
5. Confirm `AEC_AECCONVERGED = 1` after 30 s of music, with the
   latency/chirp evidence above as the primary sanity check.

**Effort estimate:**

| Phase | Duration |
|---|---|
| Weekend prototype: route music to USB-in, measure, tune `SYS_DELAY`, verify convergence | 2–3 days |
| Corpus pilot integration: outputd direct reference fanout, recorder-owned env lifecycle, volatile aec-init chip profile, explicit chip-AEC corpus legs | Landed 2026-05-29 |
| Productionize: production policy/reconciler logic for chip-AEC mode vs current bridge mode, boot-time persistence choices, bridge `:9876` repoint, outputd reference producer, and basic on-device validation | Landed and deployed 2026-05-31 (`c95bfdd`); doctor/live-arm verification passed. Wake-event telemetry review and any default-ON decision remain follow-up. |
| Risk: PLL loop bandwidth on the chip's USB Adaptive Mode could introduce timing jitter that pushes the AEC peak past tap 40 intermittently. If so, the fallback is making the host-side ALSA period smaller (already in the bring-up plan above). No CamillaDSP-side SRC bypass possible since USB-IN is 16 kHz only — see correction note above. | — |

**Verdict for future scoping:** feasibility is confirmed in lab, and the
production profile path now exists. Fresh installs use `auto`, which
resolves to chip-AEC only when the detected XVF mic profile has a
validated chip beam plan and the output DAC profile is supported /
calibrated; otherwise it falls back to software AEC3. Continue recording
and scoring wake events across chip AEC, WebRTC AEC3, raw, USB corpus
legs, and optional DTLN legs. Current best square-board chip settings:
`AEC_ASROUTONOFF=1`, fixed gated `150°/210°`, `AEC_AECEMPHASISONOFF=2`,
`AEC_FAR_EXTGAIN=0 dB`. Keep WebRTC AEC3 available as the explicit
fallback profile and avoid stacking it under chip-AEC.

**Sources** (verified URLs as of 2026-05-21):
- [XMOS XVF3800 User Guide v3.2.1 — Tuning the Application](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/04_tuning_the_application.html) — `AUDIO_MGR_SYS_DELAY` definition, 40-sample target, causality / coefficient-inspection workflow
- [XMOS XVF3800 Datasheet — Voice Processing Pipeline](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html) — pipeline at 16 kHz, AEC tail = 192 ms, integrated SRC, ref signal on USB-in left channel
- [XMOS XVF3800 Programming Guide — Theory of Operation](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/programming_guide/02_theory_of_operation.html) — SW PLL, USB Adaptive Mode, MCLK derivation
- [XMOS lib_aec Overview](https://www.xmos.com/documentation/XM-014785-PC/html/modules/voice/modules/lib_aec/doc/src/overview.html) — main/shadow adaptive filter design, 15 ms frame, phases + tail math
- [XMOS lib_adec Overview](https://www.xmos.com/documentation/XM-014785-PC/html/modules/voice/modules/lib_adec/doc/src/overview.html) — Automatic Delay Estimation; estimation auto-triggers at power-up
- [XMOS lib_sw_pll on GitHub](https://github.com/xmos/lib_sw_pll) — the software PLL implementation used for USB→mic clock sync
- [reSpeaker XVF3800 host_control README](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md) — Seeed default `AUDIO_MGR_SYS_DELAY = 12`
- In-repo: `jasper/xvf/xvf_host.py` (`AUDIO_MGR_SYS_DELAY` command
  definition) and this doc's prior sweep history: range −64 to +256,
  convergence flag never flipped, and the REF_GAIN trap that bricked
  the previous attempt.

### E — Vendor newer libwebrtc as a Meson subproject

**What:** Build `libwebrtc-audio-processing` v2.x (or upstream
WebRTC `main`) from source as a Meson subproject of our pybind11
binding, exposing the deeper `EchoCanceller3Config` knobs that
Debian Trixie's v1.3-3 public headers omit
(`suppressor.dominant_nearend_detection`,
`suppressor.nearend_threshold`, per-band gain masks, etc.).

**Why on the list:** Our 2026-05-20 NS×AGC1 sweep showed we've
saturated the tunable surface of Trixie's libwebrtc — every AGC1
parameter combination converged to the same RMS=1213 output;
`compression_gain_db` is essentially inert; the limiter dominates.
Newer libwebrtc exposes residual-suppressor knobs that would let
us reduce HF over-suppression (the documented mechanism for our
silent misses).

**Cost:** Multi-day initial build work (Meson + cross-compile for
Pi 5 ARM); ongoing maintenance burden (each Debian upgrade
potentially conflicts with the vendored lib). See "Deep tuning
landscape — research notes" below for the full prior investigation.

**Risk:** High maintenance cost. Long-term technical debt.

**Prerequisites:** Measurement showing the existing AEC3 surface
is genuinely the limit. As of 2026-05-21 we don't have that
measurement — options A, B, C may reach the goal without needing
E.

### F — DTLN-aec post-AEC residual suppressor

**What:** Insert [DTLN-aec](https://github.com/breizhn/DTLN-aec)
between AEC3's linear stage and the wake-word detector. DTLN-aec
takes `(mic, far-end reference)` — same inputs as AEC3 — and
applies a learned residual suppressor designed to preserve speech
features the rule-based residual stage destroys.

**Why on the list:** MIT licensed, ~1.8M parameters, runs
real-time on Pi 4 per community reports; Pi 5 has ~2.4× the
multi-core perf. The fix is targeted at the exact failure mode we
documented (HF speech-consonant stripping).

**Cost:** ~half-day prototype on Pi via
[PiDTLN](https://github.com/SaneBow/PiDTLN); validation against
test-1 captures via the offline replay technique above.

**Risk:** Medium. **DTLN-aec was trained on speech far-end (the
ICASSP 2021 AEC Challenge dataset) — music-as-far-end performance
is community-reported but not published.** Could be marginal or
significantly helpful; only measurement will tell.

**Prerequisites:** None for the prototype. None for measurement.

### G — Custom wake-word retraining on AEC-passed positives

**What:** Generate ~10k Piper TTS Jarvis utterances, pipe each
through our actual JTS audio chain (play → capture → AEC), collect
post-AEC positives, train a new openWakeWord Dense head against
those positives + standard negatives.

**Why on the list:** The path most likely to recover the 14/20
silent-in-every-config Jarvises, because the training distribution
would match the deployment distribution exactly.

**Cost:** ~2 weekends. Compute on free Colab T4.

**Risk:** **The resulting model is JTS-specific** — captures
our DAC, speaker, room, mic placement. Doesn't transfer to other
users / other JTS-style builds. Per project preference (this is a
hobby project that might be useful to others), this is a
last-resort option, not a first move.

**Prerequisites:** Options A and (especially) C have been
measured and found insufficient. Without that measurement, we'd
be over-fitting to a problem partially addressable by cheaper
interventions.

### What's intentionally NOT on the list

- **Hardware migration to a different mic.** We have the XVF3800
  already. Option D is the only hardware-path remaining; everything
  else is software.
- **Switching to Picovoice Porcupine.** January 2025 license update
  restricts the Free Plan to "personal non-commercial projects"
  only — fine for the JTS-as-hobby case but creates risk if the
  project's distribution model ever changes. openWakeWord (Apache)
  + livekit-wakeword (Apache) are the only safe-by-default
  choices.
- **PipeWire / topology re-architecture.** Per the user-memory
  feedback note, we don't redo the audio topology unless
  measurement localizes the root cause there. None of the open
  work streams above are in that category.

---

## Channel choice — how we got here

This section documents an architectural decision that previously
took multiple sessions to investigate. Future debugging: consult
[HANDOFF-xvf3800.md §3](HANDOFF-xvf3800.md) for the per-channel
DSP reference table BEFORE making channel-choice changes.

### Why we used to use channel 2 (raw mic 0)

The original design for the WebRTC AEC3 bridge captured chip
channel 2 = raw mic 0. The stated rationale was "AEC3 wants clean
linear input; the chip's AGC introduces non-linearity that could
confuse the adaptive filter." Defensible in isolation, but:

1. **No public XVF3800 deployment does this.** Every reference
   design we could find (Seeed's own examples, formatBCE ESPHome
   integration, Pollen Robotics Reachy Mini) uses chip channel 0
   or 1 — the chip-processed beam output.
2. **Channels 2–5 bypass every chip DSP stage, not just AEC.**
   Per XMOS User Guide §3.6.1 Table 3.2, "Category 1: Raw
   microphone data — before amplification, no system delay
   applied." That includes MIC_GAIN, HPF, beamforming, NS, AGC.
   We were giving up the entire chip pipeline to preserve
   linearity that AEC3's residual echo suppressor is designed to
   tolerate.
3. **Empirically verified 2026-05-15**: toggling chip NS/AGC
   parameters caused 1.5–8 dB of variation on channels 0/1 and
   0.0–0.4 dB on channels 2–5. Channels 2–5 are inert to every
   chip-side parameter.

### Why we switched to channel 1 (ASR beam) on 2026-05-15

The decision was made on the assumption — based on the XMOS audio
pipeline diagram and our reading of the SHF_BYPASS docs — that
channel 1 with `SHF_BYPASS=1` would give us **chip BF + NS + AGC +
HPF** without the chip's AEC. That assumption turned out to be
wrong (see Caveat above): SHF_BYPASS bypasses the entire SHF block,
not just the AEC adaptive filter. So channel 1 with SHF_BYPASS=1
is effectively raw-ish mic data — similar to channel 2.

That said, the architecture as deployed is still a win over the
previous state because:

- **`REF_GAIN=0` is correctly tuned for this raw-ish input.** The
  previous architecture's `REF_GAIN=25` was wrong for raw mics too
  (mismatched against AEC3's design point given our specific
  acoustic path levels). Both architectures used a raw-ish mic;
  the new one just has the gain knob in the right place.
- **Channel 1 has chip MIC_GAIN applied** (the output mux preserves
  MIC_GAIN even with SHF_BYPASS=1; channel 2 does not). The level
  difference is small (~1 dB) but channel 1 is slightly more
  predictable.
- **`AEC_HPFONOFF=2` (125 Hz HPF) still applies** to ch 0/1 with
  SHF_BYPASS=1 because the HPF lives at mic ingress, before the
  SHF block. Channel 2 has no HPF.

Historical note: the open question above was answered by the later
Option D work. The current `xvf_chip_aec` profile keeps
`SHF_BYPASS=0`, feeds the chip a known-good USB-IN reference from
outputd, and uses fixed ASR beams. The software-AEC fallback still
uses `SHF_BYPASS=1` for the reasons in this section.

### Why SHF_BYPASS=1 instead of relying on chip AEC

In normal XVF3800 deployments, the chip drives the speaker via
its own AIC3104 codec, and the chip's AEC handles echo
cancellation. In JTS, the speaker is driven by an external Apple
USB-C dongle. The chip's AEC reference signal arrives via the
chip's USB-IN endpoint, and the chip's firmware auto-mirrors the
host's UAC playback volume control into `AEC_FAR_EXTGAIN`. In our
topology, the host writes essentially zero to the chip's USB-IN
(we play to the dongle, not the chip), so the chip thinks the
reference is silent and the AEC adaptive filter freezes with
useless coefficients.

`SHF_BYPASS=1` removes the entire SHF stage from the signal path
on channels 0/1 — AEC, BF, NS, AGC all become passthrough.
In the software-AEC fallback, jasper-aec-bridge handles post-mic
processing (echo cancellation + residual noise suppression via
AEC3's internal NS).

---

## Lessons learned (2026-05-15/16)

Captured here so future sessions don't repeat the mistakes.

1. **Read primary docs before experimenting.** Channel layout,
   per-parameter scope, and chip pipeline ordering are all
   documented in the XMOS User Guide v3.2.1 (XM-014888-PC).
   Multiple sessions were spent rediscovering things that are in
   §3.6.1 Table 3.2 and §4.1 Fig. 4.1. The user pushed back
   correctly: *"there's an entire GitHub repo, document, and
   website, and there are so many people that have worked with
   this microphone before. Why are we guessing?"* — and the answer
   was that nobody on the implementation side had consulted the
   primary sources. Future debug sessions: **start with primary
   sources, use empirical testing to verify** rather than as the
   first line of investigation.

2. **The mic is consumed by software, not humans.** Tune for
   wake-word + ASR accuracy, not naturalness. See memory note
   `project_mic_consumed_by_robots_only`. Aggressive band-limiting
   is on-brand. Phase distortion at sub-200 Hz is invisible to
   mel-spectrogram features. AGC compression is OK for ASR models
   trained on similar distributions.

3. **MIC_CHANNEL_INDEX and REF_GAIN_DB are coupled.** Any change
   to which channel the bridge consumes requires a corresponding
   change to REF_GAIN_DB. Channel-2 (raw mic, no AGC) needs
   REF_GAIN ≈ +25 dB to align levels with the digital ref.
   Channel-1 (chip AGC'd) needs REF_GAIN ≈ 0 dB. Mismatched gain
   causes ref clipping → chaotic AEC behavior.

4. **`SHF_BYPASS=1` bypasses the entire SHF block, not just AEC.**
   We discovered this empirically on 2026-05-16 after initially
   claiming (incorrectly, in this very document on 2026-05-15) that
   SHF_BYPASS only removed the AEC adaptive filter. The correct
   reading: SHF includes AEC + beamformer + NS + AGC, all gated on
   the same bypass flag. With SHF_BYPASS=1, channels 0/1 carry
   raw-ish mic data; the only chip processing that survives is the
   AEC_HPFONOFF stage (which sits at mic ingress, before SHF). If
   you want chip BF / NS / AGC, you need SHF_BYPASS=0 — and in our
   external-DAC topology that puts chip AEC back in the path.

5. **AEC3 wants symmetric mic/ref filtering.** WebRTC's commit
   "AEC3: High-pass filter delay estimator signals" documents that
   matched HPFs on both legs improve the matched-filter delay
   estimator's adaptation in noisy environments. Our bridge applies
   AEC3's internal capture HPF automatically; the ref-side HPF in
   `_ref_thread` brings the ref to the same band.

6. **`pcm.jasper_capture` (dsnoop) must be wrapped in `plug:`**
   when consumed by clients that lock a different rate than the
   loopback's currently-locked rate. snd-aloop is first-opener-
   wins; when shairport opens it at 44.1 kHz (post PR #75), the
   bridge requesting 48 kHz from a raw dsnoop returns silence
   instead of resampled audio. `plug:jasper_capture` (exposed as
   `pcm.jasper_ref` in our asoundrc) auto-converts. This bug
   destroyed AEC silently in production for ~4 days before
   diagnosis on 2026-05-15.

7. **Doctor checks should verify bridge OUTPUT quality, not just
   service health.** The 4-day silent-AEC outage went undetected
   because doctor only checked `systemctl is-active jasper-aec-
   bridge` — the bridge WAS running, it just wasn't producing
   useful output. A check that parses the bridge's `rms over` log
   lines (verifying `ref` is non-zero during music and `attenuation`
   reaches a healthy range) would have surfaced the bug immediately.
   Shipped as `check_aec_bridge_output_health` in
   `jasper/cli/doctor/aec.py`; its later false-positive mode and fix
   are #10 below.

8. **`jasper-aec-init` doesn't re-run after a code deploy.** It's
   `Type=oneshot` with `RemainAfterExit=yes`, so the bridge unit's
   `Wants=jasper-aec-init` only triggers it on fresh boot. After
   deploying changes to chip-side params (HPF cutoff, SHF_BYPASS,
   etc.), the operator must manually `systemctl restart
   jasper-aec-init` to apply them. **Queued as follow-up** — fix
   would be one line in `deploy/install.sh` to
   `systemctl try-restart jasper-aec-init` after the rsync.

9. **`REBOOT 1` in `jasper-aec-init` created a USB renumerate
   feedback loop.** Diagnosed 2026-05-16. The chain:
   `jasper-aec-init` ExecStart wrote `REBOOT 1` to the chip → chip
   reset → USB disconnect/reconnect on the bus → kernel udev event
   on `controlC*` → `99-jasper-aec-reconcile.rules` triggered
   `jasper-aec-reconcile.service` → reconciler called
   `enable_start_aec` → `systemctl restart jasper-aec-init.service`
   → **goto top**. Sustained ~6–12 chip resets per hour after any
   `--reason install` invocation (i.e. after every deploy). The
   bridge correctly responded to each chip outage by stall-restarting
   on `mic queue empty for 5s`; that's not a bug, that's the
   `BridgeStalled` recovery path doing its job. **Removing the
   REBOOT call** was the fix: in our pipeline the chip's AEC
   adaptive filter is gated off by `SHF_BYPASS=1`, so REBOOT's only
   documented benefit ("clear adaptive-filter state", per Reachy
   Mini #389) is moot, and the three parameter writes that follow
   it are idempotent and overwrite the chip's current state
   directly. No conditional needed, just delete.
   See `jasper/cli/aec_init.py` for the post-fix shape.

10. **Doctor's `check_aec_bridge_output_health` had a false-positive
    failure mode.** Same investigation date. The check flagged
    `mic > 1500 RMS + ref < 50 RMS` as "ref path broken," but the
    mic-loud signal can also come from sources that **bypass the
    loopback by design**: TTS / wake cues enter `jasper-outputd`
    directly on current main (or `pcm.jasper_out` on pre-outputd
    rollbacks), and loud ambient voice gets pumped by the chip's ASR-beam AGC. In both
    cases `ref = 0` is correct —
    nothing was supposed to be in the loopback. **Fix**: count
    `healthy_ref_windows` (any window where `ref ≥ 50`) and only
    fail when zero healthy windows exist in the assessment period
    AND the silent-ref pattern persists. PR #75's failure was
    sustained `ref = 0` across all windows for days; this check
    still catches that. See `_assess_aec_bridge_output` in
    `jasper/cli/doctor/aec.py`.

    The same investigation produced `jasper-doctor --probe-aec`,
    which actively plays a quiet sine into `correction_substream`
    and verifies the bridge sees ref signal — useful when the
    bridge's recent journal has no music for the passive check to
    learn from.

    Refined further on 2026-05-17 (PR #134) for the corner case
    where the entire assessment window has no music at all (a
    pure-voice session). Even with `healthy_ref_windows = 0`, the
    silent-ref + mic-loud pattern proves nothing when no renderer
    is writing the loopback — every ref sample is correctly silent
    and the mic-loud bursts come from the TTS path. Added a
    `music_chain_active` gate that reads
    `/proc/asound/Loopback/pcm0p/sub*/status`; when every sub is
    `closed`, the FAIL demotes to OK with a "re-run doctor while
    music is playing" hint. The rate-lock catch from PR #75 still
    fires when a renderer is actively writing the loopback.

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
- The JTS-owned `xvf_host.py` helper for talking to the chip's
  parameter API over USB vendor control transfers.

All of this is still in the repo, partly because the architecture
(snd-aloop + chip control) is still useful and partly as
investigation history.

### What we measured

**2026-05-29 fan-in-era pre-corpus re-check:** Option D was re-run as
a bounded gate before wake-corpus recording. The first pass used the
current fan-in / outputd topology and fed the chip USB-IN reference from
`plug:jasper_capture`. That feeder path measured ref→mic delay around
`181-209 ms`, outside the chip's `AUDIO_MGR_SYS_DELAY` clamp. Adding
`--ref-delay-ms 180` left residual chip delay at `+114` samples and
produced a partial ch0 positive, but the setup was still too feeder-
shaped to trust as the final architecture.

The later same-day direct-fanout tests changed the conclusion. Playing
one source buffer directly to both the external DAC and XVF3800 USB-IN
held the acoustic reference drift around `~1 ppm` over 15 minutes.
Controlled direct A/B then showed useful chip-AEC reduction, and
double-talk/listening sweeps found the best wake-shaped path:
category-7 ASR output (`AEC_ASROUTONOFF=1`) with fixed gated
`150°/210°` beams. Final strength sweep found `AEC_AECEMPHASISONOFF=2`
better than baseline, while `AEC_FAR_EXTGAIN=+3/+6 dB` was worse.

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
- A historical filter coefficient dump (`SPECIAL_CMD_AEC_FILTER_COEFFS`,
  from earlier external tooling; not currently exposed by JTS's local
  `xvf_host.py` subset)
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
renderers / internal producers
    │
    ├─ librespot          → librespot_substream  → hw:Loopback,0,0
    ├─ shairport-sync     → shairport_substream  → hw:Loopback,0,1
    ├─ bluealsa-aplay     → bluealsa_substream   → hw:Loopback,0,2
    ├─ jasper-usbsink     → usbsink_substream    → hw:Loopback,0,3
    └─ correction/probes  → correction_substream → hw:Loopback,0,4
                                              │
                                              ▼
                         hw:Loopback,1,0..4 → jasper-fanin
                                              │ sums private lanes
                                              ▼
                                      hw:Loopback,0,7
                                              │
                                              ▼
                         pcm.jasper_capture  ← dsnoop on Loopback,1,7
                                              │
                    ┌─────────────────────────┴─────────────────────────┐
                    │                                                   │
                    ▼                                                   ▼
        reader A: jasper-camilla, via plug:jasper_capture   reader B: jasper-aec-bridge, via pcm.jasper_ref
          main_volume ducking + flat passthrough              captures jasper_ref (48k stereo) for FAR-END REFERENCE
          writes to → pcm.jasper_out (dmix on dongle)         captures hw:Array,0 (XVF, 16k 6ch) for NEAR-END MIC
          → speaker (audible path)                            takes channel 1 (ASR beam; chip AEC disabled via SHF_BYPASS=1)
                                                               downsamples ref 48k → 16k mono on left, HPF at 125 Hz
                                                               runs WebRTC AEC3 (10ms windows)
                                                               sends AEC'd mono 16k via UDP → 127.0.0.1:9876
                                                                                                   │
                                                                                                   ▼
                                                                                                jasper-voice
                                                                                                   UdpMicCapture
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
- **WebRTC AEC3** (current software-AEC fallback). The modern Google echo
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
custom-named PCM defined in `/etc/asound.conf`. PortAudio's device
enumeration only sees `hw:N,M` style devices and a few standard
aliases (`default`, `sysdefault`, `pulse`); custom asoundrc PCMs
aren't enumerated. The Python `pyalsaaudio` library calls
`snd_pcm_open(name)` directly via libasound and respects asoundrc,
so we use it for the ref capture path. The mic capture and AEC
output paths use sounddevice/PortAudio (existing daemon
convention) since they go through plain `hw:N,M` devices.

The bridge runs as root (no `User=` in the systemd unit), matching the
existing jasper-camilla/jasper-voice daemon posture for realtime audio
and `/dev/snd` access. The ALSA graph itself now lives in
`/etc/asound.conf` at mode 0644 so non-root renderers resolve the same
named PCMs.

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
flag does) is in
[BRINGUP.md "XVF firmware: switch to 6-channel variant via DFU"](../BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu).
Headline:

```
sudo dfu-util -R -e -a 1 -D <6-channel-firmware.bin>
```

The chip's `SAVE_CONFIGURATION` op had a brick hazard on firmware
2.0.6 (respeaker repo issue #8) and the upstream issue is still
open as of 2026-05-15 with no release-note confirmation that any
version fixed it — we never call it regardless of firmware version.

---

## Historical comparison before the chip-AEC profile

This table captures the pre-2026-05-29 state that justified building
the WebRTC AEC3 bridge. At that point, JTS had an external-DAC speaker
path without outputd's direct USB-IN reference fanout to the chip, so
the XVF3800's built-in AEC could not converge usefully. The current
recommended path is different: `xvf_chip_aec` arms outputd's chip
reference producer and uses fixed 150°/210° ASR beams. Keep this
comparison as history for why the software fallback exists, not as the
current profile recommendation.

| Dimension | Legacy XVF3800 hardware-AEC attempt | WebRTC AEC3 software-AEC fallback |
|---|---|---|
| **Topology fit for our setup** | Designed for chip-driven speaker; did not work in the pre-outputd external-DAC test path | Topology-agnostic — bridge can capture any reference and any mic |
| **Effectiveness in our setup** | ≤2 dB sustained attenuation (measured) | −15 to −18 dB mean on music with production tuning; deep-cancel windows to −44 dB |
| **Host CPU cost** | ~0% (chip handles it) | ~3-8% of one A76 core |
| **Host RAM cost** | ~0 MB | ~110 MB RSS (Python + numpy + scipy + sounddevice + jasper_aec3) |
| **Latency** | <1 ms (chip-internal) | ~40 ms ref-to-mic measured; AEC3's delay estimator manages alignment internally |
| **Beamforming, NS, AGC, DoA** | Included, professional-grade | NS at kModerate is built into AEC3; no BF/AGC/DoA |
| **Configurability** | Closed binary, ~30 documented parameters | Top-level `AudioProcessing::Config` is public; deep `EchoCanceller3Config` isn't (see "Deep tuning landscape") |
| **Drift handling** | Internal (chip is single clock domain) | Two-clock-domain capture; AEC3 tolerates some drift via its built-in delay estimator |
| **Convergence** | Stable when working | Stable; residual suppressor + drift-tolerant delay estimator keep it consistent across music passes |
| **Worst-case (loud music + soft voice + far-field)** | Designed to handle this | Marginal — see Tuning findings for current numbers and remaining levers |

The historical conclusion was: without a chip-owned or chip-fed
speaker reference, hardware AEC was worse than software AEC3 in this
external-DAC build. The production chip-AEC profile that shipped later
changes that premise by feeding the XVF USB-IN reference through
outputd and applying a volatile fixed-beam chip profile.

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
but the architecturally clean fix is to consume the outputd speaker
reference fanout once it is exposed to AEC/corpus consumers.

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

### Chip-side control infrastructure

`jasper-aec-init.service` still runs when the bridge is enabled. In the
software fallback it applies the raw-ish XVF profile (`SHF_BYPASS=1`) for
host-side WebRTC AEC3. In chip-AEC profiles it applies the volatile fixed-beam
hardware-AEC profile and prepares the chip USB-IN reference path. The older
`jasper-aec-tune` calibrator is still installed for diagnostics / future
manual delay work, but the supported production path is profile-managed
through the reconciler and `jasper-aec-init`.

### What's still unmeasured: live in-person wake attempts

The whole point of AEC is to make wake-word detection work
during music playback. The 2026-05-20 forensic sweep (below) DID
measure end-to-end wake detection over music — captured audio,
offline-scored against the wake model, with concrete pass/fail
counts (e.g. "Wake rate: 5/20 vs prev 4/20"). What that sweep
does NOT cover is a human standing in the room saying "Hey
Jarvis" live and being interrupted/talked over by playback in
real time, or per-config false-positive rate against ordinary
household noise. That test requires sitting in front of the
speaker and listening; it hasn't been done yet.

---

## File map

Files involved in the AEC subsystem:

- `jasper/cli/aec_bridge.py` — the AEC bridge daemon: WebRTC AEC3 in the
  software fallback, chip-beam carrier in chip-AEC profiles
- `jasper_aec3/` — sibling package, pybind11 binding for WebRTC AEC3
  (`libwebrtc-audio-processing-1` v1.3-3 from Trixie's apt)
- `jasper/cli/aec_init.py` — boot-time chip init (resets chip,
  sets UAC2 PCM to unity)
- `jasper/cli/aec_tune.py` — calibrator for chip-side
  `AUDIO_MGR_SYS_DELAY` (vestigial; kept for diagnostic use)
- `jasper/xvf/xvf_host.py` — JTS-owned XVF3800 USB control helper
- `jasper/cli/doctor/aec.py` — `check_aec_bridge_running`,
  `check_xvf_firmware_6ch`
- `jasper/cli/doctor/audio.py` — `check_mic_capture`
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
  profile-managed `JASPER_AEC_MIC_DEVICE` derived by the reconciler,
  `JASPER_MIC_DEVICE_CANDIDATES` direct-fallback hints, UDP transport
  settings, and tuning gains
- `scripts/aec-probe-timing.py` — current multi-source diagnostic timing
  probe for outputd's final speaker-reference UDP stream, outputd's
  chip-ref writer tee, the legacy `jasper_capture` tap, and selected
  XVF3800 channels. It writes JSON/CSV/Markdown artifacts and can repeat
  the standard 1024/3072, 1024/2048, and 512/1024 outputd profiles. See
  [AEC-DIAG-03 Timing Probe](AEC-DIAG-03-timing-probe.md).
- `scripts/aec-probe-latency.sh` — older chirp + cross-correlation
  diagnostic. Existing historical results from this script must be read
  with their run-era reference source in mind; early runs measured the
  `jasper_capture` pre-DSP tap and must not be confused with production
  outputd final-reference timing.
- `scripts/aec-probe-pinknoise.sh` — runs the bridge against
  stationary pink noise to measure the AEC engine's plateau
  attenuation (the upper bound for this setup, since music is
  documented as harder for AEC3)

---

## Tuning findings (2026-05-08) — HISTORICAL

> **⚠️ HISTORICAL.** This section documents tuning for the
> previous architecture (raw mic 0 / channel 2, no chip processing
> on the mic path). The "production config" recommended below
> (`REF_GAIN_DB=25, AGC2=0`) is **no longer current** — see
> [Software-AEC tuning (2026-05-16)](#software-aec-tuning-2026-05-16--fallback-baseline)
> for the current software-fallback values. Retained for context: the
> sweep matrix below shows AEC3 behavior on a raw-mic input, which
> may be useful if someone ever needs to revisit that architecture.

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
   so the clean divergence-fix is moving the AEC reference consumer to
   outputd's speaker reference fanout. ~1-2 days each.
3. **Vendor v2.1 + custom `EchoCanceller3Config`** (per "Clean
   path" above). ~1-3 days. **DONE 2026-05-22 night (offline laptop
   spike); BEST_A config identified.** The cross-reference research's
   suggested starting config had a critical bug
   (`ep_strength.bounded_erl=true` silently disables WebRTC
   Transparent Mode) AND was insufficient on its own — the real
   winning knobs were `erle.max_l=1.5, erle.max_h=1.0`,
   `filter.refined.length_blocks=30`, `use_stationarity_properties=true`,
   plus reverting `bounded_erl` to `false`. Full BEST_A config
   + sweep methodology preserved at
   `experiments/aec3-v2-deep-tune-spike/`. Results: rescues whisper-
   music silent miss (peak 0.76 vs stock 0.28), beats AEC3-stock on
   every failing music cell, competitive with DTLN-aec (BEST_A 27
   events on music cells vs D256 31, AEC3-stock 23).
   **Next move:** the triple-stream architecture in
   [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) ships
   BEST_A alongside raw mic and DTLN-aec as a 3-leg OR-fused
   wake-word system.
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

Last verified: 2026-07-09 (wake-corpus chip-AEC capture-plan gating and
bridge stats schema v2 rechecked against `jasper/cli/aec_bridge.py` and
`jasper/wake_corpus/bridge_session.py`. Prior 2026-06-30 pass rechecked
chip-AEC one-detector default and optional 150/210 beam opt-ins against
`jasper/audio_profile_state.py`, `deploy/bin/jasper-aec-reconcile`,
`jasper/cli/aec_bridge.py`, `jasper/control/aec_endpoints.py`, `/wake/`,
doctor, validation, and reconcile tests. Prior pass 2026-06-26: `/aec`
intent-vs-applied-runtime status contract rechecked against
`jasper/audio_profile_state.py`, `jasper/control/aec_endpoints.py`, and
`tests/test_control_aec_state.py`. Prior pass 2026-06-25: central chip-AEC
DAC gate, `xvf_chip_aec_testing`, profile-managed XVF mic-card derivation,
and the chip-AEC bridge-carrier / software-AEC3-bypass distinction rechecked
against the reconciler, `/aec`, doctor, and validation paths).
