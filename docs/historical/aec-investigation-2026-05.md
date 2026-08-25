# AEC investigation appendix (2026-05) — historical

> **Status: historical.** Frozen record of the May 2026 AEC investigation,
> kept because the findings cost real measurement time to produce and the
> rejected options are worth not re-litigating. Every number here is from
> its stated date and describes the topology of that date. Current
> operational truth is [HANDOFF-aec.md](../HANDOFF-aec.md); the chip is
> [HANDOFF-xvf3800.md](../HANDOFF-xvf3800.md).

## Why the chip's own AEC does not work in an external-DAC topology

The XVF3800 expects a far-end reference and assumes **the chip's own output
drives the speaker** — in its intended topology the chip can model the
relationship between what it sent to its DAC and what the mic captures,
because there is no external variable. JTS drives the speaker from a separate
USB DAC on a different clock domain, with different USB scheduling latency,
buffering, and path delays.

The mechanism that made the first attempts fail, per XMOS User Guide §4.2.1:

> "AEC_FAR_EXTGAIN: This parameter informs the audio pipeline how much
> external gain has been applied to the AEC reference signal. In the UA
> device variant, when the host sets the output volume, the AEC_FAR_EXTGAIN
> is internally set to be the same as the gain set by the host, so the user
> shouldn't need to set this command externally."

The chip's reference path runs through an internal gain stage that tracks the
host's UAC2 sink volume. With no explicit volume set, the chip parked
`AEC_FAR_EXTGAIN` at its reset default (−40 dB) and internally attenuated the
reference by 40 dB — a full-level reference delivered by the host became
inaudible to the chip's own adaptive filter. Setting the UAC2 PCM mixer to
0 dB unity (`amixer -c Array sset PCM,0 60 unmute`) flipped `AEC_FAR_EXTGAIN`
to 0 dB; effectiveness improved marginally and never approached the −20 dB+
the chip reaches natively.

Measured at the time: `AEC_AECCONVERGED` returned 0 in every test;
`SHF_BYPASS=1` (raw mic) vs `SHF_BYPASS=0` (full pipeline) differed by ≤2 dB
of RMS at every `AUDIO_MGR_SYS_DELAY` from −64 to +256 (values above 256
silently clamp); and a filter-coefficient dump showed the adaptive filter had
run away in some past state (peak magnitudes > 1.0), the signature of a
reference too quiet relative to the capture.

**What changed the verdict.** The variants tested above never fed program
audio to the chip's USB-IN as the reference. On 2026-05-29 direct-fanout
tests did: playing one source buffer to both the external DAC and the XVF
USB-IN held acoustic reference drift around ~1 ppm over 15 minutes,
controlled A/B showed useful chip-AEC reduction, and double-talk sweeps found
the best wake-shaped path — category-7 ASR output (`AEC_ASROUTONOFF=1`) with
fixed gated 150°/210° beams, `AEC_AECEMPHASISONOFF=2` better than baseline,
`AEC_FAR_EXTGAIN=+3/+6 dB` worse. That path is what ships today.

An earlier same-day pass fed the reference from `plug:jasper_capture` and
measured ref→mic delay of 181–209 ms, outside the chip's `SYS_DELAY` clamp —
a feeder-shaped result, not an architecture.

## Why software AEC3 was built, and what it cost

The comparison that justified the WebRTC AEC3 bridge, taken before the
chip-reference fanout existed:

| Dimension | XVF3800 hardware AEC, pre-fanout | WebRTC AEC3 software |
|---|---|---|
| Topology fit | designed for chip-driven speaker; did not work in the external-DAC test path | topology-agnostic |
| Effectiveness | ≤2 dB sustained attenuation | −15 to −18 dB mean on music; deep-cancel windows to −44 dB |
| Host CPU | ~0% | ~3–8% of one A76 core |
| Host RAM | ~0 MB | ~110 MB RSS |
| Latency | <1 ms (chip-internal) | ~40 ms ref-to-mic; AEC3's delay estimator manages alignment |
| Extras | BF, NS, AGC, DoA included | NS built into AEC3; no BF/AGC/DoA |
| Configurability | closed binary, ~30 documented parameters | top-level `AudioProcessing::Config` public; `EchoCanceller3Config` is not |

Engine choices considered:

- **SpeexDSP** — shipped first because the integration path was shortest.
  Speex's own docs warn it cannot model speaker non-linearity at high SPL;
  best measured was −2 to −8 dB. Removed when AEC3 landed.
- **WebRTC AEC3** — Trixie's `libwebrtc-audio-processing-1` v1.3-3 *is* AEC3
  (the 1.x is package-API stability, not algorithm version). JTS wrote its
  own pybind11 binding rather than going through PipeWire, which would have
  required restructuring the ALSA topology and only forwards top-level
  `AudioProcessing::Config` knobs anyway.
- **Neural AEC** (DeepVQE-S, DTLN-aec, GTCRN-AEC) — best on AEC-Challenge
  benchmarks, deferred.

**Why 6-channel firmware.** The 2-channel variant exposes only the chip's
processed conference/ASR channels, which have already had the chip's NS, AGC,
and beamformer applied — non-linear processing that a software linear
adaptive filter struggles to model. The 6-channel variant adds raw mics on
channels 2–5. The DFU flash is in-system and reversible (`sudo dfu-util -R -e
-a 1 -D <6-channel-firmware.bin>`); operator procedure is in `BRINGUP.md`.
`SAVE_CONFIGURATION` had a brick hazard on firmware 2.0.6 (respeaker repo
issue #8) with no release-note confirmation that any version fixed it — JTS
never calls it on any firmware version.

**Why the dsnoop tap.** Early attempts fanned out on the **playback** side
with a `type multi` PCM (two dmix slaves). It silently forwarded frames only
to slave A — verified via `appl_ptr` stuck at 0 on slaves B and C despite
`RUNNING` substreams. Moving the fan-out to the **capture** side via `dsnoop`
worked immediately; dsnoop is the canonical ALSA primitive for "multiple
readers share one capture device".

## The three bridge bugs fixed on 2026-05-19

A multi-day investigation surfaced three independent bugs that had silently
corrupted the AEC reference since the bridge shipped:

1. **ALSA linear resampler** (PR #150) — the plug-layer 44.1→48 kHz
   conversion lost ~12 dB of 4–8 kHz content. The mic captured the speaker's
   full-bandwidth output while AEC got a hollow reference, so music residuals
   in the speech band masked wake-word phonemes. Fixed by installing
   `libasound2-plugins` and setting `defaults.pcm.rate_converter` (rendered
   from `/var/lib/jasper/audio_quality.env`), which replaces ALSA's linear
   interpolator with libsamplerate sinc for every `plug:`/`plughw:`
   conversion on the box.
2. **Silence fallback on an empty ref queue** (PR #154) — replaced
   "ref_bytes = silence" with "carry forward last_ref_bytes". Before the fix
   AEC received zeroed reference ~50% of the time.
3. **Drain-newest discarded burst frames** (PR #157) — replaced
   drain-to-newest with consume-one-per-iteration. Before the fix ~50% of
   frames in `ref.wav` were byte-identical duplicates of their predecessor.

**All wake-rate baseline data from before 2026-05-19 is invalid for
evaluating AEC's contribution.** Every "AEC ON" leg ran with a broken
reference; the "AEC OFF" / chip-direct legs remain valid (the bugs were
bridge-only).

**Why not CamillaDSP for the resampling.** Considered and rejected: the
previous CamillaDSP config had AsyncSinc Balanced doing 1:1 resampling on top
of `enable_rate_adjust=true`, which CamillaDSP itself flagged as "Needless
1:1 sample rate conversion active" and which produced alternating +50/−485 ms
sync errors (HEnquist/camilladsp#207, mikebrady/shairport-sync#1980). Using
CamillaDSP for input resampling would mean disabling `enable_rate_adjust` and
losing the snd-aloop virtual-clock drift correction AirPlay sync depends on.

## The REF_GAIN trap

`REF_GAIN_DB=25` was the production value for a year, from when the bridge
consumed raw mic 0 (channel 2): with no chip AGC on the mic path the mic
arrived at ~−50 dBFS while the digital reference was near full scale, and
AEC3's adaptive filter needs roughly comparable levels.

After the bridge moved to chip channel 1 (chip AGC normalises the mic to
~−24 dBFS), keeping `REF_GAIN_DB=25` drove the reference into 11–44%
hard-clipping on music peaks. AEC3 ran on a saturated reference and produced
wildly variable attenuation (−0.3 to −20.8 dB across consecutive 5 s
windows).

**Rule:** `MIC_CHANNEL_INDEX` (in `jasper/mics/xvf3800.py`) and
`REF_GAIN_DB` are coupled. Channel 1 (chip AGC'd) wants REF_GAIN ≈ 0 dB;
channel 2 (raw, no AGC) wants ≈ +25 dB. Change one, change the other.

## What `SHF_BYPASS=1` actually does

`SHF_BYPASS=1` removes the **entire** SHF block from channels 0 and 1 — AEC
*and* beamformer *and* post-SHF NS/NLP/AGC — not just the adaptive filter.
An earlier revision of the AEC handoff claimed otherwise; it was wrong.

Verified empirically 2026-05-16: with `SHF_BYPASS=1`, toggling `PP_MIN_NS`
from 0.150 to 1.0 and `PP_AGCONOFF` from 1 to 0 changed channel 1's sub-bass
band by 0.6 dB — the same order as measurement noise on channel 2 (0.7 dB).
The chip's post-processing parameters do nothing under bypass. The HPF set by
`AEC_HPFONOFF` still applies; it sits at mic ingress, before SHF.

Implication: the "chip processing" channel 1 appeared to provide under bypass
was illusory. The 2026-05-16 performance win came from `REF_GAIN=0` fixing
the ref-mic level match for AEC3, not from chip BF/NS/AGC.

## Lessons (2026-05-15/16)

1. **Read primary docs before experimenting.** Channel layout, per-parameter
   scope, and pipeline ordering are all in the XMOS User Guide v3.2.1
   (XM-014888-PC) §3.6.1 Table 3.2 and §4.1 Fig. 4.1. Multiple sessions were
   spent rediscovering them. Start with primary sources; use empirical
   testing to verify, not to discover.
2. **The mic is consumed by software, not humans.** Tune for wake-word and
   ASR accuracy, not naturalness. Aggressive band-limiting is on-brand; phase
   distortion below 200 Hz is invisible to mel-spectrogram features.
3. **AEC3 wants symmetric mic/ref filtering.** WebRTC's own commit "AEC3:
   High-pass filter delay estimator signals" documents that matched HPFs on
   both legs improve the matched-filter delay estimator in noise.
4. **A dsnoop tap must be wrapped in `plug:`** when consumed by a client that
   locks a different rate than the loopback's currently-locked rate.
   snd-aloop is first-opener-wins; the bridge requesting 48 kHz from a raw
   dsnoop that shairport had opened at 44.1 kHz returned silence. This
   destroyed AEC silently in production for ~4 days before diagnosis on
   2026-05-15.
5. **Doctor must verify bridge OUTPUT quality, not just service health.**
   That 4-day outage went undetected because doctor only checked
   `systemctl is-active` — the bridge WAS running, it just was not producing
   useful output. Shipped as `check_aec_bridge_output_health`.
6. **That output check then needed a false-positive fix.** It flagged
   `mic > 1500 RMS + ref < 50 RMS` as "ref path broken", but a loud mic can
   also come from sound the speaker never played — room voice and ambient
   noise pumped by the chip's ASR-beam AGC — where `ref = 0` is correct.
7. **`REBOOT 1` in `jasper-aec-init` created a USB renumerate feedback
   loop** (diagnosed 2026-05-16): init wrote `REBOOT 1` → chip reset → USB
   disconnect/reconnect → udev on `controlC*` → `jasper-aec-reconcile` →
   `systemctl restart jasper-aec-init` → repeat, at ~6–12 chip resets/hour
   after every deploy. Removing the REBOOT call was the fix; the parameter
   writes that followed it are idempotent and overwrite chip state directly.
8. **`jasper-aec-init` is `Type=oneshot` + `RemainAfterExit=yes`**, so a code
   deploy does not re-run it. Chip-side parameter changes need an explicit
   `systemctl restart jasper-aec-init`.

## What not to do (so future sessions do not re-investigate)

- **Do not use the XVF3800's "processed left channel" expecting 25–40 dB of
  hardware AEC.** External writeups recommend it; the claim is accurate for
  the chip's intended topology (chip's own codec drives the speaker, as in
  HA Voice PE / Seeed reference designs) and not for an external-DAC build.
  Measured ≤−2 dB at every config tested.
- **Do not pivot to PipeWire `module-echo-cancel`.** It does not expose the
  deep AEC3 knobs and adds an audio server to the dependency graph plus
  shairport-sync/librespot integration churn.
- **Do not wait for Trixie to ship `libwebrtc-audio-processing-2`.** It will
  not happen in Trixie's lifetime per the Debian package tracker.
- **Do not vendor private AEC3 headers against apt's `1.3-3.so`.** ABI
  fragility.
- **Do not pursue WebRTC's field-trial mechanism.** The symbols are exported
  but `field_trial.h` is private, and the registry only flips a dozen named
  killswitches — not the deep config struct.

## Sources

- XMOS XVF3800 v3.2.1 User Guide (§3.5 audio pipeline, §4.2 tuning
  parameters) and Programming Guide (control protocol, parameter table).
- `respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY` (firmware binaries, host
  control README, issues #6 and #8).
- `xiongyihui/speexdsp-python`; `voice-engine/ec`; `SaneBow/alsa-aec` and
  `koniu/sysrecord` (asoundrc `multi` + dsnoop patterns).
- ALSA project Module-aloop documentation (substream and rate semantics).
- Stuart Naylor's writeups on the HA / Rhasspy / OVOS forums; HA Voice PE
  community threads on XU316 AEC behaviour (same chip family).
