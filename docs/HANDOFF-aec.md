# Handoff: acoustic echo cancellation — engine, topology, lifecycle

Canonical for `jasper-aec-bridge`, `jasper-aec-init`,
`jasper-aec-commission`, `jasper-aec-reconcile`, how the bridge CONSUMES its
reference, and the `jasper/xvf/` XMOS control helper.

Neighbouring owners — do not restate their content here:
[HANDOFF-xvf3800.md](HANDOFF-xvf3800.md) (the chip itself; `jasper/mics/xvf3800.py`
is the runtime source for its constants) ·
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
(outputd's UDP speaker monitor — read before changing reference routing) ·
[HANDOFF-enhanced-aec.md](HANDOFF-enhanced-aec.md) (the mandatory-v1 /
optional-v2 delivery lifecycle) ·
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) (the USB-mic relay writer and
descriptor) · [audio-paths.md](audio-paths.md) (the signal path) ·
[HANDOFF-barge-in.md](HANDOFF-barge-in.md) (the option analysis behind today's
topology; its warning against routing TTS through CamillaDSP describes an older
topology — TTS now crosses CamillaDSP with program audio and reaches outputd's
final-speaker reference safely) ·
[historical/aec-investigation-2026-05.md](historical/aec-investigation-2026-05.md)
(why the chip's own AEC fails in an external-DAC topology, the three 2026-05
bridge bugs, the REF_GAIN trap, the rejected options).

## Managed XVF invariant

A detected XVF3800 in any reconciler-managed profile is a chip-AEC product: it
either runs a commissioned, verified `xvf_chip_aec` path or voice remains
visibly parked with an action. It never silently becomes software AEC3 or a
direct microphone. WebRTC AEC3 remains supported for non-XVF microphones and the
explicit `custom` lab route.

`jasper/mics/xvf3800.py` owns one fixed production profile — gains, HPF, ASR
mode, emphasis, fixed 150°/210° gated beams, muxing, and bypass/arm sequence are
**not tunables**. Commissioning varies only `AUDIO_MGR_SYS_DELAY`; boot and
replug merely reapply the resulting volatile profile. No production path calls
`SAVE_CONFIGURATION` (brick hazard — AGENTS.md non-negotiable 2).

The speaker reference goes directly to the XVF USB-IN ALSA `hw` endpoint at
16,000 Hz, stereo, S16_LE, period 128, buffer 256. `jasper-outputd` derives it
from its final electrical speaker buffer using the fixed
`stereo_mean_boxcar_decimate_dual_mono_v1` transform and rejects any different
ALSA-installed geometry at the writer boundary. **Do not add an ALSA plug
conversion, a second reference route, or a rate matcher.**

## Commissioning

Explicit and foreground-only:

```sh
sudo jasper-aec-commission
```

That command is the only path allowed to play its bounded fixed signal or
issue the single volatile XVF reset that clears stale adaptive state. It
starts from the shipped `SYS_DELAY=-37` hardware baseline and plays one
discarded sweep as the operator's quiet cue before collecting evidence. It
then measures three accepted trials on all four physical microphones, chooses
one global delay nearest the causal window's centre, requires at least
`MIN_EDGE_MARGIN` (8) samples of worst-case edge margin, verifies
stable/current writer geometry and queue placement, observes convergence
`0 → 1`, and requires (thresholds in `jasper/chip_aec_alignment.py`):

- timing peak-to-competitor ratio ≥ 1.10 and normalized peak ≥ 0.20;
- all raw microphones ≥ 10 dB above room noise, with zero clipping;
- both AEC-off beams ≥ 8 dB above room noise;
- both production beams ≥ 10 dB conservative suppression.

Success atomically publishes only `/var/lib/jasper/chip-aec-alignment.json`:
schema-v3 identity (XVF factory iSerial, firmware/beam/fixed-profile identity,
physical USB-output serial or I²S profile/card identity, the final-edge sample
format outputd NEGOTIATED and reports as `dac.format`, and negotiated output
geometry) plus `K` and the commissioned `sys_delay`. Adding a field to that
identity force-recommissions the fleet by design — existing artifacts fail the
check, `jasper-aec-init` parks the managed-XVF stack (`jasper-voice` stopped and
gated off by the reboot-surviving `/var/lib/jasper/voice-input-absent`), and a
human runs `sudo jasper-aec-commission` at the speaker, whose own reconcile
cleanup unparks voice. See
[ADR-0106](adr/0106-a-verification-artifact-is-never-migrated-in-place.md).

## The K lifecycle

`K = commissioned SYS_DELAY + commissioned median reference queue`. On boot,
update, reconcile, and same-identity replug, `jasper-aec-init` samples a run of
progressing queue positions and applies
`runtime SYS_DELAY = K - median(live queue)`. An unstable queue, identity
mismatch, or out-of-range result is **rejected, never clamped**.

**The window comes out of outputd's per-write sample ring, and both ends
measure it the same way.** outputd records `snd_pcm_delay` once per completed
chip-reference write and publishes its most recent 256 observations as
`reference_outputs.chip_ref_writer.recent_writes`. The ring is the transport
because outputd's state server is one thread with a 500 ms accept poll and one
command per connection — a sequential reader gets about two STATUS reads a
second while the writer writes 47–375 times a second. An outputd without the
ring is refused by name, not guessed around.

Spread is a property of the mix cadence, not a fault (a burst wider than the
writer's 256-frame ALSA ring makes every write block, so the reading lands
wherever scheduling slack leaves it — a 128-frame mix period spread 11 frames,
a 1024-frame period spread 86). **The window therefore holds the MEDIAN's
precision constant rather than the raw spread**, plus a minimum span and a
split-half median-drift bound the count rule cannot see. The derivation of every
constant lives beside them in `jasper/chip_aec_alignment.py` — read it there,
not here.

`collect_reference_queue` and `runtime_sys_delay` (both `jasper/cli/aec_init.py`)
read that one rule, so boot cannot reject a window on a criterion commissioning
never applied. The symmetry is of the RULE, not the outcome: boot's window is a
fresh measurement of a different stretch of time and can fail on its own
numbers — that is the check working.

**Boot bounds the delay against the commissioned one.** Because
`K = commissioned SYS_DELAY + commissioned median`, the gap between boot's delay
and the commissioned one IS the difference between the two windows' medians —
the only error term between the alignment the commissioner verified and what
boot applies. `choose_delay` reserves `MIN_EDGE_MARGIN` frames on both
causal-window edges, so that is the bound; past it `jasper-aec-init` still
applies the delay and discloses it with the numbers as **disclosed_stale**
(ADR-0101) — the artifact stopped describing the box, but nothing broke. The
chip's own −64..256 range is checked FIRST and still refuses outright: it is
the declared driver cap, and checking it ahead of the margin is what makes the
margin safe to disclose rather than park. It spans 320 frames against a
39-frame causal window and was never a substitute for the margin.

The same disposition covers a commissioned identity that moved and an artifact
from a superseded schema. Identity divergence is split by meaning: the fields
in `PER_UNIT_IDENTITY_FIELDS` name this physical box, everything else names the
hardware class `K` was measured against, and the disclosure says which. An
artifact that is absent or unusable — unreadable, malformed, from a schema this
build does not know, or banking a `sys_delay` outside the driver cap — still
exits `2`: there is no `K` to run from, and the shipped hardware-class default
that would close that half is #2984.

### The cross-transaction ordering guard

Within one reconcile pass, outputd's final native-plus-UDP configuration is
installed while bridge/voice stay parked, that critical restart must succeed,
and only then does init sample the live writer. **Across passes that ordering
does not hold**: `jasper-audio-hardware-reconcile` writes
`/var/lib/jasper/outputd.env` and kicks `jasper-outputd` and
`jasper-aec-reconcile` as two separate `--no-block` transactions, and udev
starts the reconciler in a transaction of its own, so
`jasper-aec-init.service`'s `After=jasper-outputd.service` orders nothing here.
The live outputd can still be answering STATUS with the previous geometry and
final-edge format.

`require_outputd_env_loaded` (`jasper/cli/aec_init.py`) closes that: before
sampling any STATUS it compares outputd's `ExecMainStartTimestamp` against the
env file's mtime, waits a bounded 10 s for a queued restart, then exits `3`
rather than certify a stale edge. That is an ordering race, not a moved
artifact, so the reconciler deliberately does **not** ask for a recommission:
it runs software AEC3, publishes `disclosed_stale` with "wait for
jasper-outputd to restart", and the box keeps hearing.

Three details of that comparison are easy to get wrong. **Both stamps must be
the same clock**: comparing a CLOCK_REALTIME age against a CLOCK_MONOTONIC age
fails *open* under a forward NTP step — routine on an RTC-less Pi at boot —
certifying exactly the stale outputd the guard exists to catch, so the guard
compares recorded realtime *instants* and "now" is not an input. The residual is
inherent (only a backward step landing between the two stamps and exceeding
their separation inverts them; fake-hwclock restores a past time so NTP's first
correction goes the harmless way) and cannot be closed by unit ordering, because
both stamps are written by other processes. **A stopped outputd is caught, not
ignored**: systemd retains `ExecMainStartTimestamp` after a stop, so only a unit
that never started this boot reports an empty value and leaves the guard inert —
there `collect_reference_queue`'s writer-not-ready path owns the diagnosis.
**An inert guard is logged**: a non-zero `systemctl` exit, an unparseable value,
or a non-`ENOENT` `OSError` emits `event=chip_aec_init.ordering_probe` at WARN,
because a guard that silently does not run looks identical to one that passed.

## Reconciler and status

`jasper-aec-reconcile` owns the lifecycle. An alignment it cannot apply drops
the box to software AEC3 rather than parking it, and `disclose_stale_chip_aec`
normalises `JASPER_MIC_DEVICE` off the chip's UDP carrier whenever the bridge
does not come up — voice on an unfed socket stalls into `WatchdogSec=30s` and
the unit's `StartLimitAction=reboot`. Ordinary lifecycle handling never
plays audio, resets the chip, searches parameters, rewrites the artifact,
starts a timer, or runs a servo. `/aec`, `/state`, and `jasper-doctor` expose
`ready`, `disclosed_stale`, `unavailable`, or `fault` with the
reconciler-provided reason/action. If the XVF is absent after a previous
AEC-enabled boot, the reconciler clears the stale
`JASPER_MIC_DEVICE=udp:9876`, disables the bridge, and stops voice instead of
leaving wake-word on an unfed UDP socket.

`/aec` separates saved intent from applied runtime truth. `raw_intent` mirrors
`/var/lib/jasper/aec_mode.env`; active fields (`mode`, `bridge_role`,
`software_aec3`, `legs`, `audio_profile.active`, and `/wake/`'s `mic_settings`)
come from the reconciler-applied `/etc/jasper/jasper.env` snapshot. A managed
XVF rejected by the mic/DAC/alignment gates reports a parked chip profile and
action, never an active AEC3/direct fallback. If runtime env is stale during a
mic-card change, `/aec.bridge_role` reports `pending`. **Status surfaces must
not infer the active engine from saved profile intent or bridge service state.**

## The bridge

`jasper-aec-bridge` is a shared mic-to-voice carrier, not a synonym for WebRTC
AEC3. With commissioned chip AEC it forwards the selected hardware beam to
`:9876` while AEC3 is bypassed. In non-XVF/custom software-AEC paths it consumes
outputd's 48 kHz monitor and runs AEC3.

**CamillaDSP is a soft startup dependency, not a lifecycle owner.** The bridge
reads the XVF mic directly and consumes outputd's final-reference UDP stream,
its only reference source (the pre-Camilla `pcm.jasper_ref` ALSA fallback was
retired). So `jasper-aec-bridge.service` uses `After=` plus `Wants=` for
CamillaDSP and deliberately has neither `Requires=` nor `PartOf=`: a brief
Camilla pause must leave the UDP mic producer running so `jasper-voice` keeps
making watchdog progress. This closes the #1264 failure where a USB combo
toggle clean-stopped the bridge and voice watchdog-aborted 30 seconds later.

**Reference-input health is receiver-owned.** Bridge stats schema v4
(`/run/jasper/aec_bridge_stats.json`) publishes one bounded `reference_input`
block: runtime source and endpoint, the lifetime count of complete 20 ms frames
converted and accepted by the bounded AEC reference queue, monotonic
`last_frame_age_ms` (`null` before the first frame), the same-boot monotonic
snapshot instant, and process age at that instant. Reusing `last_ref_bytes`
while the receiver is starved does not advance this telemetry. For
`outputd_udp`, doctor gives a new bridge 10 s to start, then fails when no frame
has arrived or the newest receiver frame is more than 5 s old; all freshness
arithmetic uses the monotonic fields, so RTC/NTP steps cannot change a verdict.
Precedence is one-way: a v4 freshness FAIL wins over historical RMS and the
USB-blind loopback-activity heuristic, so USB Audio Input cannot hide a stale
transport, while freshness OK proves only transport and bounded-queue admission
— the 90-second journal assessment still owns reference signal content and clock
drift. Missing, older, and unknown-future schemas retain that journal fallback
for rolling-deploy compatibility; exact v4 is a declared contract and fails
closed when malformed, future-monotonic, or stale. **UDP send success is never
receiver proof.** This is observability only: capture, queue bounds, frame
carry-forward, DSP, and routing are unchanged.

Outputd `STATUS` (`reference_outputs.udp_target`, `udp_active`,
`udp_error_count`) is consulted only to localize a receiver failure, and only
for an `outputd_udp` bridge — that is the one provenance doctor will name a
producer for. Missing, stale, malformed, or unknown provenance, including the
retired `alsa` spelling, stays source-neutral rather than guessing a legacy path.

### Optional computer microphone carrier and source selection

The optional computer microphone is a second consumer of the carrier, not a
voice socket takeover. When `/var/lib/jasper/usb_mic.env` explicitly enables the
feature, the bridge emits one selected 16 kHz mono source to localhost UDP
`:9894`; `jasper-usbmic` alone consumes that port and writes the UAC2
Pi-to-host direction. This consumer emits each native 320-sample AEC frame
immediately (20 ms) with a 16-byte v2 `JM` header carrying a uint32 sequence and
`CLOCK_MONOTONIC` bridge-emit timestamp. **Voice/wake legs keep their raw
1280-sample / 80 ms packet contract with no header.**

`JASPER_USB_MIC_LEG` independently selects the computer-only export. Its
default, `primary`, preserves the production-clean carrier sent to `:9876`. When
the reconciler-applied `ChipBeamPlan` proves a supported six-channel XVF
capture, `/wake/` also offers `raw0` as **Raw microphone (no echo
cancellation)** — comparison-only, reusing physical channel 2 already captured
by the bridge, with neither chip/software AEC nor JTS voice gain. The plan's
fixed chip beams remain additional choices. `/wake/` renders the server-provided
list and the control endpoint rejects a token the active plan does not publish,
so the persistence and UI contracts do not hard-code today's `chip_aec_150` /
`chip_aec_210` vocabulary or advertise raw capture without validated geometry.

Selection happens in-process immediately before the `usb_host_mic` emitter, so
it adds no queue or frame latency. An explicitly selected chip frame receives
the same post-AEC gain and soft-limit as `primary`; if it is absent for one
iteration — including when software AEC3 is active — the export falls back to
that iteration's final `clean` frame. **`raw0` is deliberately different: a
missing physical raw frame is skipped and logged, never replaced**, because
comparison audio must not silently change identity. Bridge stats publish the
bridge-applied selection separately from the resolved mode/physical leg, so no
surface mistakes saved intent for applied source. This branch is
computer-microphone-only: it changes neither the `:9876` session stream, any
wake detector, the wake-leg wire format, nor the chip primary-beam policy. The
relay accepts the old raw 20 ms and legacy 80 ms packet shapes, but
compatibility is one-way — an old relay cannot decode the 656-byte v2 packet, so
deploy and rollback restart the bridge and its `PartOf=` relay at one revision
and a staged rollout is unsupported. When the feature is off the extra emitter is
not created; the `/wake/` switch restarts the bridge so intent and producer agree.

`JASPER_AEC_CAPTURE_LATENCY` is an evidence-gated experiment knob on the shared
capture stream: unset preserves PortAudio's default, `low` requests the device's
low-latency default, and a positive seconds value up to 0.25 requests an
explicit buffer (0.01–0.08 is the useful range). Because this one stream also
feeds voice/wake, **do not set a production value** until hardware A/B evidence
shows lower negotiated latency with no stalls, queue drops, or wake-rate
regression — the 2026-07-16 A/B is in
[historical/usb-gadget-hardware-evidence-2026-07.md](historical/usb-gadget-hardware-evidence-2026-07.md).
The bridge logs the negotiated input latency as `event=aec.mic_stream_latency`
and publishes the negotiated capture rate, block size, and input-latency frames
under `capture_stream` in the stats file.

### Wake legs

The chip-AEC profile's default wake surface is deliberately one detector: the
primary/session beam (`JASPER_MIC_DEVICE=udp:9876`, wake leg `on`). The 150°
and 210° chip beams are advanced custom opt-ins via
`JASPER_WAKE_LEG_CHIP_AEC_150=1` / `_210=1`; only then does the reconciler
publish `JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887` or `..._210=udp:9888`.
Selecting any named profile resets those optional beams to `0`; `custom`
preserves them. This ties active wake-word instances to the channels the
reconciler actually applied, avoiding hidden extra Silero/openWakeWord model
instances on chip-AEC hardware.

## Escape hatches

Managed-XVF custom/lab A/B — the low-level direct route requires `custom`;
`direct_mic` is the ordinary no-AEC selector for non-XVF microphones and cannot
bypass commissioning on a managed XVF. Then back to auto:
`JASPER_AUDIO_INPUT_PROFILE` is the authoritative selector, and the wake-leg
booleans are kept for rollback compatibility and re-resolved by the reconciler
from live hardware.

```sh
printf 'JASPER_AUDIO_INPUT_PROFILE=custom\nJASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile

printf 'JASPER_AUDIO_INPUT_PROFILE=auto\nJASPER_AEC_MODE=auto\nJASPER_WAKE_LEG_RAW=1\nJASPER_WAKE_LEG_DTLN=0\nJASPER_WAKE_LEG_CHIP_AEC=0\nJASPER_WAKE_LEG_CHIP_AEC_150=0\nJASPER_WAKE_LEG_CHIP_AEC_210=0\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

The legacy `xvf_chip_aec_testing` token stays parser-compatible for stored state
and automation, but `/wake/` does not render it as an operator choice.

## High-pass filter architecture

The mic is consumed only by software (openWakeWord at 16 kHz mono, then a
real-time speech LLM), so everything outside the speech band is noise the
consumers do not use AND content AEC3's adaptive filter wastes capacity
modelling. The stack, layered:

| Layer | Filter | Cutoff | Where |
|---|---|---|---|
| Chip mic ingress | 4th-order Butterworth | 125 Hz | `AEC_HPFONOFF=2`, fixed in the `jasper/mics/xvf3800.py` production profile |
| AEC3 internal capture | 2nd-order Butterworth | 100 Hz | `AudioProcessing` upstream of `EchoCanceller3`, compile-time on in `jasper_aec3/src/aec3_binding.cpp` |
| Bridge ref pipeline | 2nd-order Butterworth | 125 Hz | `_ReferenceFrameConverter` in `jasper/cli/aec_bridge.py`, after `resample_poly`, before REF_GAIN. `JASPER_AEC_REF_HPF_HZ`, default 125 |

The chip-side cutoff is **not** an env knob — it is part of the fixed volatile
profile. (`JASPER_AEC_CHIP_HPF_HZ` is read only by `voice_daemon.py` when
stamping a wake event's bridge-config snapshot; nothing applies it to the chip.)

**HPFs on both legs are not redundant.** AEC3 applies its internal HPF to the
**capture** signal only; the reference arrives untouched at the adaptive filter,
so without the bridge-side ref HPF the matched filter wastes coefficients on an
LF relationship that does not exist in the capture. 125 Hz on both matches
XMOS's shipped smart-speaker default; it nulls 2–3 of openWakeWord's 32 mel bins
(60 Hz mel floor), an accepted, unmeasured risk.

## Software AEC3 tuning — non-XVF and custom routes only

Managed XVFs never enter this path; they park per the invariant above.

| Knob | Value | Where | Why |
|---|---|---|---|
| Mic channel | 1 (ASR beam) | `xvf3800.MIC_CHANNEL_INDEX` | Canonical XVF3800 voice-assistant channel. Under the fallback's `SHF_BYPASS=1` it carries raw-ish mic data. |
| Chip output mux | OP_L=`(8,0)`, OP_R=`(8,0)` | `jasper-aec-init` | The bridge reads channel 1; Seeed's firmware default for channel 1 is OP_R=`(0,0)` (silence), which presents as `mic=0` with healthy ALSA capture and UDP output. |
| Chip SHF | bypassed (`SHF_BYPASS=1`) | `jasper-aec-init` | Keeps the chip AEC out of the near-end path. This disables the ENTIRE SHF stage (AEC + BF + NS + AGC) on channels 0/1, not just AEC; the chip-side HPF survives. |
| `JASPER_AEC_REF_GAIN_DB` | `0` | `jasper.env` + `.env.example` | AEC3's design point is ref ≈ mic. Any positive value drives the reference into hard clipping on this channel — see the REF_GAIN trap in the historical appendix. Coupled to the mic channel: change one, change the other. |
| `JASPER_AEC_MIC_GAIN_DB` | `6` | `jasper.env` | Boosts AEC3 output toward openWakeWord's training distribution (~−18 dBFS RMS). Static, soft-clipped via tanh; stacks on AGC1's dynamic gain. |
| `JASPER_AEC_AGC2` | `0` | `jasper.env` | The binding only sets `gain_controller2.enabled`; `adaptive_digital` defaults off in libwebrtc-audio-processing-1 v1.3-3, so AGC2 is a no-op for level control on this build. Kept for compat; recommended off. |
| `JASPER_AEC_AGC1_ENABLED` / `_TARGET_DBFS` / `_MAX_GAIN_DB` | `1`, `9`, `18` | `jasper.env` | AGC1 in `kAdaptiveDigital`. `TARGET_DBFS=9` → −9 dBFS via `target_level_dbfs` (0–31). `MAX_GAIN_DB=18` → `compression_gain_db` (0–90) — a soft-knee compressor parameter, **not** a gain ceiling despite the env-var name. The shipped benefit is uniform output across utterances, not detection rate. AGC1 has no public attack/release parameter. |
| `JASPER_AEC_NS_LEVEL` | `low` | binding default + `jasper.env` | More aggressive NS strips HF speech consonants openWakeWord depends on. `kVeryLow` is not exposed by v1.3-3; `JASPER_AEC_NS_ENABLED=0` disables NS entirely at the cost of residual music. |

**Corpus-only sweep knobs.** `jasper/aec_sweep.py` owns three stable pilot slots
(`aec3_variant_1..3`) exposing additional AEC3 suppressor knobs through
`jasper_aec3/src/aec3_binding_v2.cpp` and `_Aec3V2Engine`. They require the
verified v2 enhancement and are unavailable on a v1-only install. Labels and
overrides come from `/var/lib/jasper/aec3_sweep_variants.json`, applied with
`jasper-aec-sweep-config apply <file> --restart-bridge`. Do not promote a sweep
variant to production until it beats BEST_A on same-utterance listening review,
corpus-quality metrics, and wake scoring under the far+music condition.

## Caveats

- **The legacy ALSA tap is pre-CamillaDSP.** `jasper_capture` remains a
  CamillaDSP-only tap on the renderer→Camilla loopback *before* CamillaDSP, and
  its `pcm.jasper_ref` alias has no reader at all. Do not infer speaker
  amplitude or final reference timing from it; timing numbers `jasper-aec-tune`
  printed before it moved to the outputd monitor are not comparable — re-run.
- **Cross-clock-domain drift.** Reference and mic ride independent clocks that
  drift by tens of ppm. AEC3's delay estimator tolerates some drift but not
  unbounded; no async resampling is implemented on either leg.
- **`jasper-aec-tune` is diagnostic.** It prints a candidate without changing
  state by default. `--apply` requires finite confidence ≥ 0.001, a candidate in
  the firmware-confirmed `[-64,+256]` range, a present XVF, and matching write
  readback; it retains the current chip value and restores it on failure,
  reporting an unsuccessful rollback as uncertain chip state. It stops and
  restores both capture participants so its direct XVF capture cannot race the
  bridge. The write is volatile — no state file, no `SAVE_CONFIGURATION`; the
  next reconcile/init or reboot overwrites it from the active profile.
- **The bridge is Python (~110 MB RSS)** — interpreter + numpy + scipy +
  sounddevice; the `jasper_aec3` binding is ~5 MB plus the AEC3 library.
  Measured on a Pi 5: bridge 3–8% of one A76 core / ~110 MB, camilla 0.5% /
  8 MB, voice 11.3% / 265 MB. If RAM becomes the constraint, in order: drop
  scipy (~30 MB), drop sounddevice (~15 MB, one mic-capture path left to port),
  rewrite in Rust/C (~80–100 MB).
- **Live in-person wake attempts are still unmeasured.** Offline sweeps score
  captured audio against the wake model; nobody has measured a human in the room
  being talked over in real time, or per-config false-positive rate.

## File map

- `jasper/cli/aec_bridge.py` — the bridge daemon (AEC3 in the software path,
  chip-beam carrier in chip-AEC profiles); `jasper_aec3/` — pybind11 binding for
  WebRTC AEC3 (`libwebrtc-audio-processing-1` v1.3-3 from Trixie's apt).
- `jasper/cli/aec_init.py` — boot/replug reapply, read-back verification, queue
  collection, ordering guard. Never resets the chip.
  `jasper/cli/aec_commission.py` — the foreground alignment command.
  `jasper/chip_aec_alignment.py` — artifact schema, K math, window rule,
  thresholds. `jasper/cli/aec_tune.py` — diagnostic delay estimator.
  `jasper/xvf/xvf_host.py` — XVF3800 USB control helper.
- `jasper/cli/doctor/aec.py` — `check_aec_bridge_running`,
  `check_aec_bridge_output_health`, `check_aec_bridge_dtln_engine`,
  `check_xvf_firmware_6ch`, `check_xvf_mixer_state`;
  `jasper/cli/doctor/audio.py` — `check_mic_capture`.
- `deploy/systemd/jasper-aec-{bridge,init,reconcile}.service`,
  `deploy/bin/jasper-aec-reconcile`, `deploy/udev/99-jasper-aec-reconcile.rules`,
  `deploy/alsa/asoundrc.jasper` (defines `pcm.jasper_capture`),
  `deploy/modprobe.d/snd-aloop.conf`, `deploy/modules-load.d/snd-aloop.conf`.
- `deploy/install.sh` builds mandatory `jasper_aec3._aec3`, installs the
  optional-v2 lifecycle and `dfu-util`, seeds `/var/lib/jasper/aec_mode.env`,
  and runs the reconciler once. `pyproject.toml` registers
  `jasper-aec-{bridge,init,commission,tune,sweep-config}`.
- `scripts/aec-probe-timing.py` — the current multi-source timing probe over
  outputd's reference UDP stream, the chip-ref writer tee, and selected XVF
  channels; see [AEC-DIAG-03](AEC-DIAG-03-timing-probe.md). Also
  `scripts/aec-probe-latency.sh` (older chirp/cross-correlation — read its
  historical results with their run-era reference source in mind) and
  `scripts/aec-probe-pinknoise.sh` (plateau attenuation against pink noise).

Last verified: 2026-08-25 (triage pass — file map, console scripts, doctor check
names, commissioning thresholds, artifact schema, queue-window constants, and
bridge stats schema rechecked against the code. The chip-side HPF was found to
be fixed in the production profile rather than env-tunable; that claim was
corrected. Investigation history moved to
`docs/historical/aec-investigation-2026-05.md`.)
