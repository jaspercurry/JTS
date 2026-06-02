# Chip-AEC experiment — conclusive test plan

**Status: 2026-05-29 positive lab result; opt-in production path shipped
and deployed on 2026-05-31.**
The experiment is no longer a shelved negative. A same-day lab pass
proved that the XVF3800's on-chip AEC can produce useful cancellation
in JTS's external-DAC topology when the chip receives a clean USB-IN
far-end reference. The wake-corpus recorder has a dedicated chip-AEC
comparison profile that can enter/exit the needed test state and label
the `150°` / `210°` ASR beam outputs explicitly. That profile uses
outputd's direct final-output fanout: a 48 kHz UDP reference for
software AEC/corpus analysis plus a 16 kHz downsampled XVF3800 USB-IN
reference PCM for chip AEC. The `chip_aec_150` / `chip_aec_210` beams
are now **opt-in, hardware-conditional, scored production wake legs**
(see [HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
§2.4). They stay default-OFF on fresh installs and are only armed when
the 6-channel XVF firmware is present and the operator enables chip-AEC
via `/wake/`.

**Current production state (verified on Pi, 2026-05-31, build
`c95bfdd`).**

- `jasper-aec-init` applies a read-back-verified volatile chip-AEC
  profile when `JASPER_AEC_CHIP_AEC_ENABLED=1`, and restores
  `SHF_BYPASS=1` + `AUDIO_MGR_OP_L/R=[8,0]` when chip-AEC is off.
- `jasper-aec-reconcile` maps the single `/wake/` chip-AEC intent
  (`JASPER_WAKE_LEG_CHIP_AEC=1`) to the bridge/init/outputd env needed
  for hardware AEC. It enforces the single-chip exclusion by clearing
  raw/DTLN runtime device vars while chip-AEC is active.
- `jasper-outputd` can fan the final speaker buffer to the XVF USB-IN
  reference PCM and to the bridge's UDP reference tap.
- `jasper-aec-bridge` forwards the selected chip beam into `:9876`
  as the session/heartbeat carrier and emits both explicit scoring legs
  on `:9887` / `:9888`.
- `wake_events` stores chip-beam scores, offsets, RMS, `fired_legs`,
  and explicit per-beam WAV paths
  (`audio_chip_aec_150_path`, `audio_chip_aec_210_path`).
- `/wake/` shows the active mic/topology/wake legs in a compact status
  card, so operators do not have to infer state from env files.
- `jasper-doctor` on the deployed Pi reported all critical checks
  passing and runtime wake legs armed as `on`, `chip_aec_150`, and
  `chip_aec_210`.

**Code-level readiness surface added 2026-06-01.**
`jasper-audio-validate` writes a bounded readiness snapshot as an
immutable timestamped schema-v1 artifact under
`/var/lib/jasper/audio-validation/` and updates `latest.json` as the
cheap status-surface pointer. It uses already-exposed runtime state
(profile/env truth, service state, outputd reference outputs, bridge
counters, wake legs, recent drift-warning evidence when journaled, and
Pi/build identity for attribution). It is advisory, reports clean
runtime readiness as `status=warn` /
`recommendation=run_hardware_validation`, and does not play calibration
audio or write/persist XVF chip settings. This is not a substitute for
the Pi-verified drift/delay and wake-telemetry gate below.

**Operator hardware-validation surface added 2026-06-01.**
`jasper-audio-hw-validate` is the bounded production validation runner
for this path. It is never launched automatically by doctor, `/aec`,
deploy, startup, or the reconciler. Run it explicitly:

```sh
jasper-audio-hw-validate --dry-run
sudo jasper-audio-hw-validate --duration-seconds 10 --stdout
sudo jasper-audio-hw-validate --long-window --stdout
```

The runner samples already-running outputd chip-reference health and
AEC bridge counters, then polls read-only XVF chip profile/convergence
state only after chip-AEC is requested/active and runtime/reference
health passes. `--duration-seconds` is the passive outputd/bridge
observation window, not a hard total wall-clock cap; bounded XVF
readback/poll subprocesses may add time. It refuses inactive chip-AEC
unless `--force` is passed. It writes timestamped schema-v1 artifacts
through `jasper/audio_validation.py` and updates `latest.json` only as
the status-surface pointer. It does not generate playback, open capture
loops, or call XVF write/persist commands (`SAVE_CONFIGURATION` and
`REBOOT` remain forbidden). For DAC8x/outputd stability, supply the
source externally (for example, AirPlay playing into the normal JTS
renderer path) and let the profile observe outputd counters during that
window. Fixed-delay and long-window drift evidence remain `not_run`
until an explicit operator-confirmed playback/capture probe lands, so
clean passive evidence still produces a partial validation result rather
than default-on chip-AEC approval: a `status=warn` artifact with
`recommendation=run_drift_delay_validation`.

For the DAC8x/outputd xrun workstream, use the separate outputd-only
profile:

```sh
sudo jasper-audio-hw-validate --profile hifiberry_dac8x_outputd_stability --long-window --stdout
```

That profile records outputd/content-pipeline service state, outputd DAC
STATUS, and outputd xrun/clipping/progress counters. It intentionally
does **not** require chip-AEC runtime env, XVF readback, bridge counters,
wake legs, or an active `jasper-voice`; it is evidence for DAC8x/outputd
stability, not chip-AEC viability.
Passive `AEC_AECCONVERGED=0` is reported as `not_observed`, not failure,
because the runner may not have observed meaningful far-end audio.

For the concrete HiFiBerry DAC8x workstream, use the same runner with
the explicit DAC8x outputd-stability profile:

```sh
sudo jasper-audio-hw-validate \
  --profile hifiberry_dac8x_outputd_stability \
  --duration-seconds 60 \
  --stdout
sudo jasper-audio-hw-validate \
  --profile hifiberry_dac8x_outputd_stability \
  --long-window \
  --stdout
```

That profile is deliberately narrow: by default it passively samples
outputd before/after the observation window and records DAC/content xrun,
clipping, reference-sequence, watchdog, service-state, and DAC identity
evidence while an operator-controlled source is active. It requires the
installer/reconciler-written `JASPER_AUDIO_DAC_ID=hifiberry_dac8x`
identity and a recognized non-fallback `JASPER_AUDIO_DAC_CARD`
(`sndrpihifiberry` on the observed Pi), and fails if the active output
DAC is Apple, unknown, or still on installer fallback card `A`. It does
not generate playback, restart services, write temporary chip-ref env,
open capture loops, sample bridge counters, or write XVF settings.
Passing this profile only means the concrete DAC8x digital outputd path
stayed stable during the window; acoustic drift/delay, chip convergence,
and wake telemetry remain separate gates.

**HiFiBerry DAC8x alternate-DAC bringup (partial, 2026-06-01).**

Host `jts3.local` is the first concrete non-Apple-DAC validation
target. The deploy manifest after the DAC8x installer patch reported
branch `codex/dac8x-validation`, build `1e2f96f-dirty`, installed at
`2026-06-01T16:46:14-04:00`.

What passed:

- DAC8x ALSA identity: playback card `sndrpihifiberry`
  (`snd_rpi_hifiberry_dac8x`), device `0`.
- DAC8x bounded silent ALSA smoke:
  `hw:CARD=sndrpihifiberry,DEV=0` accepted 48 kHz S16_LE, 8-channel
  playback for 2 seconds; `plughw:CARD=sndrpihifiberry,DEV=0`
  accepted 48 kHz S16_LE stereo for 2 seconds.
- Installer/outputd wiring: `/etc/asound.conf` rendered
  `pcm.outputd_dac` to the recognized DAC8x card, skipping Apple-only
  `jasper-dac-init` and headphone-monitor units. Current 2026-06-02
  code renders a direct alias by default; explicit lab wiring may set
  `JASPER_OUTPUT_DAC_ROUTE=mono:N` or `stereo:L,R` to route outputd's
  stereo surface to selected 1-indexed DAC8x physical outputs.
- outputd physical ownership: while outputd was active, `aplay -l`
  showed the DAC8x playback subdevice as `0/1`; logs showed
  `event=outputd.alsa.opened ... dac_pcm=outputd_dac` and
  `event=outputd.ready`.
- XVF3800 identity after hotplug: USB VID/PID `2886:001a`, ALSA card
  `Array`, firmware `2.0.8`; `/proc/asound/.../stream0` showed the
  expected 16 kHz S16_LE, 2-channel playback endpoint and 16 kHz
  S16_LE, 6-channel capture endpoint.
- outputd chip-reference open: temporary lab env
  `JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0`,
  `JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000`,
  `JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=320`,
  `JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=1280`, and
  `JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:19190` opened
  cleanly. Logs showed `event=outputd.chip_ref.opened` and no
  `queue_full`, `write_failed`, or xrun warnings during a short idle
  soak.
- Bounded digital playback through the normal outputd path worked:
  a 60-second `correction_substream` stimulus produced stable
  outputd DAC progress, nonzero bridge reference RMS, and no
  outputd/chip-ref problem logs.
- After the updated installer was re-run, a 60-second post-install
  smoke artifact
  (`/var/lib/jasper/audio-validation/dac8x-postinstall-smoke-20260601T2051Z.json`)
  passed every check: playback return code, `outputd_dac`, chip-ref
  PCM, DAC/content xrun counters, outputd log health, bridge reference
  signal, and bridge starvation.
- A durable 30-minute digital stability artifact was written on the
  Pi at
  `/var/lib/jasper/audio-validation/dac8x-20260601T201320Z.json`.
  Signal: bounded generated 48 kHz stereo S16_LE sine at `-30 dBFS`
  into `correction_substream`; elapsed playback `1800.171 s`;
  services stayed `jasper-outputd=active`,
  `jasper-aec-bridge=active`, `jasper-voice=inactive`.
  DAC-side checks passed: `outputd_dac` remained open, DAC
  `xrun_count=0`, DAC frames advanced by `86,521,856`, chip-ref PCM
  stayed `plughw:CARD=Array,DEV=0`, and the bridge saw 361 RMS
  windows with `max_ref_rms=733` and `max_ref_starve=0`.
- A follow-up instrumented 30-minute watchdog run wrote
  `/var/lib/jasper/audio-validation/dac8x-watchdog-20260601T2102Z/`.
  It sampled outputd status once per second and captured a trigger
  bundle at
  `trigger-20260601T212153Z-content-xrun/` within about 94 ms of the
  xrun counter changing. The trigger confirmed low system load
  (`0.21 0.19 0.15`), no swap use, services active, no kernel
  USB/ALSA errors in `dmesg`, DAC `xrun_count=0`, TTS queue empty,
  chip-ref outputs still open, and immediate recovery.

What did **not** pass yet:

- The 30-minute artifact recommendation was **fail** because outputd
  logged one upstream content-side xrun:
  `event=outputd.xrun source=content pcm=outputd_content_capture
  count=1` at `2026-06-01T16:30:03-04:00`. This did not coincide with
  a DAC xrun, chip-ref queue/drop/write failure, or bridge starvation,
  so the current read is "upstream content-loop scheduling glitch,"
  not "DAC8x/chip-ref clock failure." Still, do not mark the
  30-minute full-pipeline gate passed until a repeat run is clean or
  the content xrun is explained.
- The instrumented repeat also failed the 30-minute gate with one
  content-side xrun:
  `/var/lib/jasper/audio-validation/dac8x-watchdog-20260601T2102Z/summary.json`
  reported `content_xruns=1`, `dac_xruns=0`, playback return code
  `0`, and one trigger directory. This makes the content-path xrun
  reproducible enough to debug before acoustic/chip convergence.
  Current best read: the failure is in the upstream content loop
  (`correction_substream` → fan-in → Camilla → `outputd_content_capture`
  → outputd), not the DAC8x physical write or XVF USB-IN reference
  path. The trigger journal again shows nearby localhost
  `jasper-control` health/shairport probe traffic, but that is only a
  lead, not proof.
- After the canonical DAC8x artifact profile landed, a 60-second run
  passed and wrote
  `/var/lib/jasper/audio-validation/20260601T220417.929544Z__xvf3800__hifiberry_dac8x__hifiberry_dac8x_outputd_stability__warn.json`.
  The 30-minute canonical repeat then reproduced the blocker and wrote
  `/var/lib/jasper/audio-validation/20260601T220604.408436Z__xvf3800__hifiberry_dac8x__hifiberry_dac8x_outputd_stability__fail.json`.
  Its bounded playback completed (`86,400,000` generated frames), bridge
  counters passed (`ref_starved_frames_delta=0`, drop deltas `0`), DAC
  writes advanced with `dac_xrun_delta=0`, but
  `outputd_reference_health` failed on `content_xrun_delta=1`.
  The matching outputd log line was:
  `event=outputd.xrun source=content pcm=outputd_content_capture count=1
  errno=32 frames_read=56188928 empty_periods=4 partial_periods=0
  eagain_count=3 dac_frames_written=56193024 period_frames=1024
  buffer_frames=4096`.
- No audible DAC8x→air→XVF drift/delay run has been performed. The
  DAC8x HAT had no amplifier/speaker path connected during this pass,
  so the run proved digital fanout only.
- No chip convergence check or wake telemetry review has been
  performed.
- One fresh-install reboot was observed before validation drop-ins were
  added: the unconfigured `jasper-voice` service exited because
  `JASPER_VOICE_PROVIDER` was unset and its systemd
  `StartLimitAction=reboot` escalated the restart loop. This was not a
  DAC8x clock/routing failure, but it is a real fresh-install safety
  gap. The local lab Pi was stabilized by parking `jasper-voice` and
  adding validation-only `StartLimitAction=none`/`Restart=no` drop-ins
  for `jasper-outputd`, `jasper-aec-bridge`, and `jasper-voice`.

Recommendation as of this snapshot: **PASS** for DAC8x hardware
visibility, ALSA shape, outputd DAC ownership, and XVF USB-IN
reference-open compatibility; **WARN** for "DAC8x ready for
chip-AEC validation" because two 30-minute digital artifacts each had
one content-side xrun and the acoustic drift/delay, chip convergence,
and wake telemetry gates below have not run. Do not enable chip-AEC by
default on DAC8x from this result; debug the content-path xrun first.
Future DAC8x repeats should use
`jasper-audio-hw-validate --profile hifiberry_dac8x_outputd_stability`
so clean or failing windows land in the canonical audio-validation
artifact stream instead of temporary lab-script JSON.

**What remains is validation, not plumbing.** Keep chip-AEC opt-in until
a fresh telemetry window shows its recall / false-accept contribution:
record normal use and wake tests with music, fetch the wake-event
corpus, run `scripts/analyze-three-leg.sh`, and inspect solo saves plus
false fires for `chip_aec_150` and `chip_aec_210`. The readiness
and hardware-validation artifacts are the operator-visible product
state; the default-ON flip still requires measured wake telemetry plus
long-window DAC drift and delay-stability evidence from an explicit
playback/capture validation run.

The topology diagram below still records the original 2026-05-23
dmix-era experiment shape, not the current 2026-05-26 fan-in /
outputd production topology. Current production wiring lives in
[`docs/audio-paths.md`](audio-paths.md). The five
`scripts/chip-aec-*.sh` scripts + `jasper/chip_aec_experiment.py` +
this doc remain lab infrastructure for reproducing the original tests.
Do not use those scripts as the production control surface; production
chip-AEC is now controlled by `/wake/` + the reconciler.

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
The chip-AEC beams have been promoted from corpus-only to opt-in,
**default-OFF** production wake legs (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
§2.4). They stay default-OFF: gate any default-ON flip on a ~1-week
telemetry review of each beam's recall / false-accept contribution
against a fresh corpus window (`scripts/analyze-three-leg.sh`).

### Plug-in contract — what you measure off-box and where it lands

The leg *infrastructure* (registry → config → reconciler → bridge/init
producer → voice wiring → telemetry columns/WAVs → `/wake/` toggle/status)
is built and default-OFF. To tune how the chip beams perform, measure the
values below and plug them into the existing slots. Nothing here needs the
leg infrastructure to change — these are values, not new code paths.

| # | Tunable (what you measure) | Plug-in point | Shape | "Good" = |
|---|---|---|---|---|
| 1 | **Chip DSP profile** — `SHF_BYPASS`, the fixed beam azimuths (150°/210°), `AEC_ASROUTONOFF`, `AEC_AECEMPHASISONOFF`, `AEC_FAR_EXTGAIN`, `AUDIO_MGR_OP_L/R` | `jasper/cli/aec_init.py` production chip-AEC mode, gated by the reconciler's `JASPER_AEC_CHIP_AEC_ENABLED=1` | XVF register writes, **read-back-verified** (a failed write is a mode-transition failure, not a warning). Best-known values: the "Best lab configuration" block above. **Brick hazard: never `SAVE_CONFIGURATION` / `REBOOT`.** | the beam carries ASR-shaped speech with music cancelled (the 02-vs-03 ear test + low reference correlation) |
| 2 | **Per-(leg, condition) wake-threshold offsets** | `jasper/wake_fusion.py` `WakeFuser(offsets={...})` (the Phase-1.3 seam — already wired into the OR-gate) | `{(leg_token, condition): float}` additive offset in [0,1] score units, e.g. `{("chip_aec_150","music"): +0.05}`. Absent pair ⇒ base threshold (today's behavior) | per-(leg, condition) FRR improves with no FA/h regression on a fresh labeled corpus window |
| 3 | **Wake model for the beams** — does the active model (`jarvis_v2`) score the chip beam well, or does it need a beam-specific model? | the wake-model registry ([`jasper/wake_models.py`](../jasper/wake_models.py)) + `JASPER_WAKE_THRESHOLD` | a registered model + threshold (same mechanism as every other leg) | the chip-beam score distribution separates real wakes from noise at the chosen threshold (cf. the `raw0` warning in HANDOFF-mic-fusion §2.8 — an unconditioned stream can need its own model) |
| 4 | **Per-leg cost** (RAM / CPU) | the `_LAYERS` row in [`jasper/web/wake_setup.py`](../jasper/web/wake_setup.py) + the AGENTS.md sub-toggle table | `<RAM> · <CPU>` label (currently the estimate `~10 MB · light`) | the displayed figure matches measured Pi-5 resident cost |

**Frozen — do NOT change while tuning** (the historical corpus + analysis
tooling key off these): the leg tokens `chip_aec_150` / `chip_aec_210`,
their UDP ports `9887` / `9888`, the additive `wake_events` score columns,
the additive `audio_chip_aec_{150,210}_path` columns, and the
`fire_chip_aec_{150,210}` `trigger_kind`s. New telemetry is additive only.

The review tooling keeps the historical wrapper name
`scripts/analyze-three-leg.sh`, but the underlying
`scripts/_analyze_three_leg.py` now discovers the available wake legs from
the fetched `wake_events` schema. It reports chip-beam fire patterns,
score distributions, solo-save sets, listening prompts, and funnel
conversion from the same telemetry spine: `fired_legs`, per-leg score
columns, and explicit per-beam WAV paths.

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

## DAC clock-domain dependency — a methodology for evaluating any speaker DAC against chip-AEC

> **Read this before swapping the speaker DAC for a chip-AEC build.**
> This is the reusable decision procedure: the invariant chip-AEC
> needs, how to classify a candidate DAC against it, the empirical
> gate, and the escape hatches if the DAC doesn't satisfy it. The
> HiFiBerry DAC8x is worked as the running example because it's the
> first real candidate, but the *method* is the point — apply it to
> the next DAC too.
>
> History note: this section has been wrong in two different ways and
> corrected both times — read the corrections as a caution about
> reasoning from architecture instead of measurement. (1) An early
> draft asserted the DAC8x "is a self-clocked I2S master with its own
> crystal." Wrong — the kernel proves it's a Pi-clocked I2S *slave*
> (Step 1b). (2) The next draft kept the "DAC8x breaks chip-AEC"
> verdict but justified it with "two independent PLLs off one crystal
> drift tens of ppm." Also wrong, and more subtly so: PLLs that share
> one crystal are **frequency-coherent** (common-mode crystal error
> cancels — Step 2), so the likely drift is near-zero, not tens of ppm.
> The honest verdict is therefore **not** "broken" but "**uncertain,
> lean coherent, measure it**" (Step 3). Step 4's mitigations still
> apply *if* measurement shows real slip.

### Step 0 — The invariant chip-AEC depends on

The XVF3800's on-chip AEC has **no asynchronous sample-rate converter
(ASRC)** on its reference path. What it *does* have: a Software PLL
that synchronises the mic clock to the reference received over I2S or
USB, and — because the audio pipeline runs at 16 kHz — a **synchronous,
fixed-ratio 48→16 kHz (3:1) sample-rate converter** set at firmware
build time. A fixed-ratio SRC is not a drift-compensating ASRC: it
assumes its input and output clocks are already locked. Its only
*alignment* tools are a configurable **reference delay (0–500 ms,
fixed)** plus a **192 ms adaptive tail**; the tuning guide requires the
bulk delay to *remain constant while running* ("any inconsistency …
will result in severely degraded algorithmic performance"). Both
absorb *static delay* and *reverb tail*, not continuous *sample-rate
drift*.

> *(Correction: an earlier draft "proved" the no-ASRC point by claiming
> XMOS ships the XVF3610 as a "Stereo + ASRC" part. That was a
> fabrication — the XVF3610's actual differentiators are stereo AEC
> (225 ms tail) and an ADEC automatic-delay-estimation block for
> unknown output latency, and its reference is still a clock-synchronised
> I2S slave. Neither chip has an ASRC on the reference path; the
> XVF3610 does not prove it, so don't cite it as evidence.)*

So chip-AEC stays converged only if **three clocks are frequency-locked
into one domain**:

1. the **mic A/D**,
2. the **chip USB-IN reference** endpoint, and
3. **the airborne echo** — i.e. the speaker's **D/A conversion clock**.

The chip's USB Adaptive Mode PLL (`lib_sw_pll`) locks (1) and (2) to
the Pi **USB SOF** by design — that pair is never the problem. **The
speaker DAC supplies (3), and it is the only one of the three you
change when you swap DACs.** The entire question of "will this DAC work
with chip-AEC" reduces to: *does this DAC's D/A run in the same
frequency-locked domain as the USB-SOF mic clock?*

Why the production rig satisfies it today: the Apple USB-C dongle is
(almost certainly) a **synchronous/adaptive UAC DAC** — its D/A is
slaved to the same Pi USB SOF the mic rides, so all three clocks are
literally the one USB-SOF timebase. *Caveat: this is an inference, not
a verified teardown — no USB-descriptor capture confirming the dongle's
clocking mode was found, and there are regional hardware variants. It's
the most likely explanation, and the fact that chip-AEC works today is
strong circumstantial support; confirm with `/proc/asound/.../stream0`
(Step 1a) if it ever matters.* This is also why `jasper-outputd` can
get away with **no
drift compensation anywhere**: it paces its loop on the blocking
`outputd_dac` write, fans the *same* mixed period to the chip USB-IN
via an **integer** 48 k→16 k decimator (`ChipRefDownsampler` in
`main.rs`, asserts exact divisibility), and ships it through a bounded
queue that only **drops periods on overflow** / recovers xruns
(`event=outputd.chip_ref.queue_full`). A lossy producer/consumer pair
stays glitch-free long-term *only* if both ends run at one physical
rate — which holds because every endpoint (mic, chip USB-IN, Apple
DAC) rides the one USB SOF. Swap in a DAC on a different timebase and
that no-compensation design has nothing holding it together.

### Step 1 — Classify the candidate DAC by transport, then by clock role

Two questions, in order. The first narrows the failure modes; the
second is the actual verdict.

**(a) USB DAC?** Then ask **synchronous/adaptive vs asynchronous**:
- *Synchronous / adaptive* → D/A is slaved to USB SOF → **same domain
  as the mic. Preserves chip-AEC.** (The Apple dongle is inferred to be
  this — verify via the `stream0` readout below rather than assuming.)
- *Asynchronous* → D/A runs off the DAC's **own crystal**, with a USB
  feedback endpoint → **independent domain. Breaks chip-AEC.** Most
  "audiophile" USB DACs are async — that's the premium feature, and
  it's exactly wrong for us.
- **How to check (one command on the Pi):**
  ```sh
  cat /proc/asound/cardX/stream0   # X = the DAC's card index
  ```
  The playback endpoint is tagged `(SYNC)` / `(ADAPTIVE)` (good — same
  domain) or `(ASYNC)` (bad — own crystal). Marketing rarely states
  it; this readout is authoritative.

**(b) I2S HAT DAC?** Then ask **I2S master (own oscillator) vs I2S
slave (Pi-clocked)** — but note the twist in Step 2:
- *I2S master with onboard crystals* (HiFiBerry "Pro"/"HD", e.g.
  DAC+ Pro / DAC2 Pro) → echo on the board's own crystal →
  independent domain → breaks chip-AEC.
- *I2S slave, Pi-clocked* (HiFiBerry DAC8x, DAC+ Standard/Light/Zero;
  PCM5102A-class) → echo on the Pi's I2S clock. **Necessary but NOT
  sufficient — see Step 2.**
- **How to check (the kernel overlay is authoritative, not the
  marketing):**
  ```sh
  # Read the board's dtoverlay .dts in the rpi kernel tree:
  #   i2s-controller = <&i2s_clk_producer>   → Pi is master (DAC is slave)
  #   dai_fmt ... SND_SOC_DAIFMT_CBS_CFS      → codec slave / Pi master
  #   a fixed-clock / oscillator node present → board has its OWN crystal (master)
  #   snd-soc-dummy codec, no oscillator node → PCM5102A-class Pi-clocked slave
  ```
  For the DAC8x specifically: `hifiberry-dac8x-overlay.dts` binds
  `i2s_clk_producer` + `snd-soc-dummy` with **no oscillator node**, and
  `rpi-simple-soundcard.c` sets `SND_SOC_DAIFMT_CBS_CFS`. So the DAC8x
  (and Studio DAC8x — same overlay) is unambiguously a **Pi-clocked I2S
  slave with no crystal**. This is the fact the earliest draft got
  backwards.

**(c) Secondary checks regardless of transport.** Two more factors that
don't change the clock-domain verdict but can independently break a
build: (i) the XVF3800 AEC reference is **mono** (left channel; right
ignored, and the chip plays left on both outputs) — a multichannel DAC
must still present a coherent mono reference; (ii) confirm a working Pi
5 / RP1 overlay exists and its clock producer/consumer role matches the
intended topology (RP1 changed the master/slave model vs older Pis).

### Step 2 — The subtlety: different *peripheral* clock domains, but probably *frequency-coherent*

Here's where a Pi-clocked I2S DAC differs from the USB-SOF mic — and
where it's easy to over-claim the consequence.

On the **Raspberry Pi 5 / RP1**, the USB-SOF clock and the I2S clock
are generated by **two different PLLs**, but **both descend from the
single 50 MHz RP1 crystal**:
- the **mic** (XVF, USB-SOF) rides the USB controller's **dedicated
  USB PHY PLL** (the SOF/microframe generator);
- an **I2S HAT DAC** rides RP1's **`pll_audio`** (1.536 GHz VCO →
  12.288 MHz I2S).
(Primary sources: RP1 Peripherals Datasheet §2.5; `drivers/clk/clk-rp1.c`
— separate `CLK_USBH*_MICROFRAME` vs `clk_i2s`←`pll_audio` parentage,
both off `clk_xosc`.)

**Different PLLs off one crystal does NOT imply sample-rate drift.**
This is the trap an earlier draft fell into ("separate PLLs → tens of
ppm"). Each PLL output is `crystal × ratio`; if the crystal is off by
δ ppm, *both* outputs shift by δ ppm, so the **ratio between them is
exact and crystal-independent** — the tolerance is common-mode and
cancels. For AEC what matters is *accumulating sample-rate offset*
(frequency coherence), not phase noise or jitter. A fractional-N audio
PLL adds bounded jitter around an exact long-term average, not
accumulating drift. So two clocks derived from one crystal are
**frequency-coherent**; the regime that genuinely drifts tens of ppm is
*separate oscillators* (an async USB DAC with its own crystal), which
is a different case. The likely DAC8x outcome is therefore **near-zero
relative drift**, not the "tens of ppm" the prior draft asserted.

**What's left as a real risk (narrower than drift):**
1. **Non-rational SOF division** — if the USB host's SOF cadence isn't
   an exact rational divisor of `pll_audio`, a small residual
   frequency offset could remain. Unproven either way from the
   datasheets.
2. **Variable end-to-end delay** — even with frequency-coherent clocks,
   two independent ALSA/buffering paths (the chip USB-IN reference vs
   the I2S playback) can let the reference-to-air *delay* wander over
   time. That violates the chip's "bulk delay must remain constant"
   requirement and degrades AEC — a *delay-stability* problem, not a
   sample-rate-drift problem, and the one most worth watching.

**The lesson worth carrying forward:** the Apple dongle's coherence
comes from being a *synchronous USB* device in the **mic's own USB-SOF
domain** — but "same crystal, different PLL" (the I2S HAT case) is also
frequency-coherent, just not delay-stability-guaranteed. "Different
peripheral clock domain" is the right description; "independent
oscillators that drift" is not. Which is exactly why Step 3 is the
decider, not this argument.

### Step 3 — The empirical gate (this decides it — the theory only sets a prior)

The clock-tree argument sets a prior (**lean coherent**, per Step 2),
but it cannot prove the magnitude — that's a hardware fact of the
specific board/OS/scheduler. **Measure before trusting either way.**
Wire the candidate DAC as `outputd_dac`, keep the chip USB-IN reference
fanout on, play wideband content for ≥30 min, and measure ref→air→mic
drift with the same direct-fanout harness that produced the ~1 ppm
Apple-DAC figure. Also watch for the *delay-stability* failure mode
(Step 2.2), not just rate:
```sh
journalctl -u jasper-outputd | grep -E 'chip_ref.(queue_full|xrun|write_failed)'
```
- **≤~1 ppm + clean chip-ref log** ⇒ the domains are effectively
  coherent; chip-AEC is viable as-is. (Given the shared crystal, this
  is the *expected* outcome — but confirm it.)
- **tens of ppm, or steady drops/underruns, or a slowly-wandering
  ref→air delay** ⇒ residual offset or delay instability; chip-AEC
  needs compensation (Step 4) or a different DAC/topology.

A clean cross-domain readout, independently, is the PipeWire/JACK
steady-state resampler ratio between the USB-mic node and the I2S-DAC
node — its deviation from 1.0 is the drift.

### Step 4 — If the DAC is on an independent domain, the options

In rough order of effort/risk:

1. **Use software AEC3 instead (the production default — lowest risk).**
   AEC3's reference is the digital `pcm.jasper_capture` tap, and WebRTC
   AEC3 explicitly handles render/capture clock mismatch (it resamples
   and flags `clock_drift` in the delay controller). Any DAC (DAC8x
   included) on AEC3 is a routing + delay/level re-tune, **not** an
   architecture break. It's **low/negligible risk and DAC-agnostic** —
   not literally "zero risk": AEC3 tolerates drift *within bounds*, and
   very large drift or a delay-estimator lock-on error can still drop
   ERLE (the "echo breakthrough" mode). If AEC3 ERLE sits below ~10 dB,
   check the delay estimator + render/capture buffer alignment before
   blaming the DAC. If you don't specifically need chip-AEC, stop here.
2. **Software SRO compensation of the chip reference feed (keeps
   chip-AEC, DAC-agnostic).** Estimate the DAC-vs-mic sample-rate
   offset and asynchronously *pre-warp* the reference fanned to the
   chip USB-IN so that, in the mic's clock frame, it tracks the
   drifting echo — then the chip's fixed-rate AEC sees a synchronous
   pair. This is the field's standard solution (2024 AudioLabs SRO-AEC,
   Microsoft ICASSP'04, Paderborn DXCP-PhaT) and the exact role
   PipeWire/PulseAudio/zita-ajbridge already play in production
   (few-ppm→sub-ppm match, ~15 s convergence). Design rule: **your
   resampler owns *rate*; leave residual *delay* to the chip's 192 ms
   tail** — don't let both loops chase the same knob. Requires a
   polyphase/sinc resampler (not linear — see the 2026-05-19 bridge
   HF-loss bug) and an online SRO estimator (crystals drift with
   temperature). It would live in `jasper-outputd` before the 48→16k
   downsample.
3. **Put the whole path in one clock domain in hardware (XVF-master
   I2S).** The XMOS-sanctioned topology is XVF3800 as I2S *master* →
   an I2S-*slave* DAC slaved to the chip's BCLK/LRCLK/MCLK (this is
   how HA Voice PE gets on-chip AEC). A Pi HAT can't be that slave (it
   wires to the Pi, not the chip), and moving the XVF to I2S firmware
   is a large rebuild — drops USB Audio + the 6-ch raw-mic capture the
   multi-leg wake architecture depends on, moves control to I²C,
   invalidates `jasper/xvf/` + `jasper/mics/xvf3800.py` + the outputd
   USB-IN fanout. Last resort.
4. **Keep a synchronous USB DAC for the speaker.** Stay all-USB-SOF.
   The constraint is "synchronous/adaptive, not async" (Step 1a), and
   the quality ceiling is real — the Apple dongle already measures
   near-transparent and most better USB DACs are async.

### Worked verdict — HiFiBerry DAC8x / Studio DAC8x

Pi-clocked I2S slave, four PCM5102A-class Burr-Brown DACs, **no onboard
crystal** (kernel-confirmed, Step 1b). On Pi 5 its `pll_audio` I2S clock
is a *different RP1 PLL* from the USB-SOF the chip mic rides (Step 2) —
but both descend from the one 50 MHz crystal, so they're **frequency-
coherent** and the likely relative drift is near-zero. **Verdict:
uncertain, lean coherent — not "broken," but not a blind drop-in
either.** The deciding factors are residual non-rational SOF offset and
ref→air *delay stability* (Step 2), which only the Step 3 gate settles.

Practical path for JTS:
- **Production: run the DAC8x on software AEC3 (Step 4.1).** You get the
  balanced outputs + better DAC at low/negligible AEC risk, with no
  dependence on how the Step 3 measurement turns out. Recommended
  default.
- **If chip-AEC on the DAC8x is specifically wanted, run the Step 3
  gate first** (don't assume it's broken — given the shared crystal it
  may well pass). If it shows real slip, add SRO compensation
  (Step 4.2) or move to an XVF-master topology (Step 4.3).

See [HANDOFF-aec.md](HANDOFF-aec.md) "DAC clock-domain dependency" for
the condensed version.

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

Last operational verification: 2026-05-31 for the Apple-DAC production
chip-AEC path (deployed build `c95bfdd`; `jasper-doctor` critical checks
passed; runtime wake legs were armed as `on`, `chip_aec_150`, and
`chip_aec_210`; `/wake/` exposes the mic/topology status card). DAC8x
alternate-DAC partial verification: 2026-06-01 on `jts3.local`, build
`1e2f96f-dirty`; final install time `2026-06-01T16:46:14-04:00`;
hardware visibility, ALSA smoke, outputd ownership, and XVF USB-IN
chip-reference open passed. A 60-second post-install smoke artifact
(`/var/lib/jasper/audio-validation/dac8x-postinstall-smoke-20260601T2051Z.json`)
passed all checks. A 30-minute digital artifact
(`/var/lib/jasper/audio-validation/dac8x-20260601T201320Z.json`) passed
DAC/chip-ref checks but failed the full-pipeline stability gate on one
content-side xrun. An instrumented repeat
(`/var/lib/jasper/audio-validation/dac8x-watchdog-20260601T2102Z/summary.json`
plus trigger bundle) reproduced one content-side xrun with DAC/chip-ref
healthy, pointing at the upstream content loop rather than DAC8x.
After rebasing onto the canonical validation runner work, `jts3.local`
was redeployed at build `4c8c29f-dirty` with install time
`2026-06-01T18:01:14-04:00`. The new
`hifiberry_dac8x_outputd_stability` profile wrote a clean 60-second
artifact and then a failing 30-minute artifact with
`content_xrun_delta=1`, `dac_xrun_delta=0`, no bridge drops/starvation,
and recommendation
`fix_outputd_content_or_reference_xruns_before_dac8x_timing` in that
pre-rebase prototype. The rebased canonical outputd-only profile uses
`fix_outputd_stability_before_dac_validation` for the same outputd xrun
class.
Acoustic drift/delay, chip convergence, and wake telemetry remain open.
This doc still preserves a dmix-era experiment snapshot in places;
current production topology lives in `docs/audio-paths.md`.
