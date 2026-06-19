# Handoff: Chip-AEC DAC portability

> **Status: design-of-record (living draft).** This is the canonical
> plan for making XVF3800 chip-AEC work across *any* output DAC. It
> supersedes the dynamic-aligner mechanism proposed in the
> `AEC-DIAG-07` production-design draft (§3, the per-period
> `snd_pcm_delay` delay line) — see [Design decision](#design-decision)
> for why. Engine internals live in
> [HANDOFF-aec.md](HANDOFF-aec.md); the chip itself in
> [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md); the lab evidence in
> [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) and
> [AEC-DIAG-01-baseline.md](AEC-DIAG-01-baseline.md). This doc does not
> restate them — it owns the *clock-domain* design, the *DAC test
> matrix*, and the *layered roadmap*.

## TL;DR — current state

- **Today:** `jasper-outputd` is the final audio owner; it already writes
  the 16 kHz chip-ref to the XVF USB-IN and exposes per-endpoint timing
  (`reference_outputs.chip_ref_writer.*`, `dac.snd_pcm_delay_*`). There is
  **no drift compensation** on the chip-ref path — only a fixed 3:1
  downsample. That is *correct* for the one shipping profile (Apple), and
  insufficient for independent-clock DACs.
- **The goal:** plug in any DAC + the XVF3800 mic and have chip-AEC either
  just work or degrade *visibly* to software AEC3 — never brick, never a
  calibration ritual before sound comes out.
- **The mechanism:** a self-calibrating **digital clock-recovery (SRO)
  loop** inside outputd, built only as deployed hardware proves it's
  needed. Most of the code is gated behind a measurement we have not yet
  run on the production path.
- **Latency:** this whole effort is on the AEC *reference* side-branch and
  is **latency-neutral for the user**. See [Latency posture](#latency-posture).

## Design decision

The chip-ref alignment problem decomposes into three physical classes, and
the right mechanism differs per class. The `AEC-DIAG-07` draft used one
tool (a per-period delay line servo'd on `snd_pcm_delay`) for all three;
that is wrong.

| Variability source | Right mechanism |
|---|---|
| Fixed offsets (converter/amp/acoustic/USB-IN/beamformer) | Static residual trim (chip `SYS_DELAY` + 192 ms tail absorbs most of it) |
| Step changes (startup, xrun, stream restart) | Event-driven re-init, once per event |
| **Continuous cross-clock drift** (DAC crystal vs XVF clock) | **Async rate-match (resampler)** — *not* a delay line |
| Slow motion within the chip's 192 ms tail | Nobody — the chip's adaptive filter owns it |
| ALSA queue-occupancy jitter | Ignore (it is noise, not signal) |

Two facts make this concrete (sources: CHIP-AEC-EXPERIMENT.md,
HANDOFF-aec.md):

1. **`snd_pcm_delay` is buffer *occupancy*, not presentation delay.** On a
   loaded Pi it is dominated by scheduler-wakeup jitter. Servoing a
   per-period loop on it chases noise (the draft's required "smoothing" is
   the tell). The clean drift signal is a **long-window rate slope**.
2. **A delay line is the wrong instrument for continuous drift.** It can
   only counter a rate ramp by inserting/dropping samples — degenerating
   into a crude resampler — and a ramping delay length *is* a time-varying
   bulk delay, which the chip's adaptive filter explicitly rejects. For
   genuine drift the field-standard fix is rate-matching (SRO compensation;
   AudioLabs/Microsoft/Paderborn), which presents the chip a *constant*
   bulk delay while the resampler owns rate.

**Our advantage:** outputd owns *both* ALSA endpoints (the DAC and the XVF
USB-IN), so it recovers the clock ratio from **ground-truth counters** —
`dac_frames_written − dac_snd_pcm_delay` vs `chip_ref_frames_written −
chip_ref_snd_pcm_delay`, plus `chip_ref_sequence_lag` (`state.rs:934`).
This is easier than the blind acoustic SRO estimation the academic work
does. The control machinery already exists: the `RateController` (PI servo)
+ windowed-sinc resampler in `content_bridge.rs` (`render_period` /
`RateController::next_ratio`). Layer 2 lifts it onto the chip-ref path.

**Invariant — one rate owner.** If a chip-ref resampler is live, nothing
else may correct the same drift (e.g. CamillaDSP `rate_adjust` on that
path). This is the documented `rate_adjust + AsyncSinc` oscillation
footgun; it must be a test, not a hope.

## Compensate vs unify

Two solution families for an independent clock: **compensate** the drift
(software resampler — universal, works for any truthful-driver DAC) or
**unify** the clock domains (make both devices share one timebase — elegant
but rarely available off-the-shelf). Compensate-by-default is the
foundation; unification is a per-DAC bonus the loop *auto-detects* (it
measures ~0 ppm and correctly does nothing). The Apple dongle gets
unification for free because it rides the same USB SOF the XVF locks to.

## Latency posture

Keep two latency axes separate:

- **Axis A — user-perceived latency** (music/TTS → speaker; wake →
  response). This is the one to protect.
- **Axis B — AEC reference timing** (when the echo reference reaches the
  chip). The entire portability effort operates here.

The clock-recovery loop, resampler, and chip-ref buffer all live on the
reference **side-branch** (outputd taps final content → downsample →
chip-ref writer). That branch is not in the playback or wake path, so
**building it adds no user-perceived latency.** The real Axis-A lever is
the separate outputd **period/buffer profile** (`1024/4096/3072`, candidate
`512`), gated on first solving the content-side xruns — tracked as the
[orthogonal roadmap row](#layered-roadmap), not part of this effort.

How we honor "we care about latency" without optimizing prematurely:

- **Make latency visible now.** Layer 0's `/state` block also surfaces the
  measured latency budget (DAC presentation delay, playback queue depth) so
  it is always known and hand-tunable later. Visibility ≠ optimization.
- **Do not chase a smaller chip-ref buffer (1280 → 640).** It is a
  reference path; the 40 ms "saving" buys the user nothing.
- **No latency auto-tuner.** Surface it; tune by hand when evidence warrants.

## Hardware test matrix

Build-for is gated by **deployed hardware**: we build and test for the
three boxes below because all three are real, used speakers. We do **not**
build for hypothetical DAC classes (>2 independent DACs, exotic async USB)
until such hardware exists.

| Box | DAC | Clock topology | What it proves |
|---|---|---|---|
| **JTS** | Apple USB-C dongle (single) | Coherent — DAC + mic + reference all on USB SOF (~1 ppm) | The coherent path: SRO ≈ 0, loop is a no-op, chip-AEC "just works." Regression guard that we never break the working case. |
| **JTS3** | HiFiBerry DAC8x (I2S HAT) | Independent crystal vs XVF clock | **The drift box.** Runs the still-`not_run` production-path long-window SRO measurement; decides whether Layer 2 is needed and supplies the servo gains. Also surfaces out-of-window delay + non-48k risks. |
| **JTS5** | Dual Apple USB-C (composite) | Likely coherent per child (both on USB SOF) + inter-child sync | **The composite box.** Tests the composite *reference fold* and the inter-child delay-divergence guard. Most-likely-coherent composite shape, so it tests fold > drift. Verifies fail-closed when composite can't support chip-AEC. |

Per-box expected verdict (to be confirmed on hardware): JTS → `coherent`;
JTS3 → `compensable` (or `coherent` if DAC8x drift is negligible — the
measurement decides); JTS5 → `coherent composite` if both children lock to
USB SOF, else `fallback` until per-child composite design exists.

## Layered roadmap

Each layer is gated by evidence from the layer below. The bulk of the code
is gated; "now" is small.

| Layer | What | Build trigger | Size | User latency |
|---|---|---|---|---|
| **0 — Observe (NOW)** | `/state`: chip-ref SRO/drift estimate **+ latency budget**; doctor surface; HW-free tests | Build now — it is the instrument that gates everything above | ~tens of lines, reuses existing counters | neutral; *adds latency visibility* |
| **1 — Classify + fail-safe (NOW, thin)** | coherent-vs-not verdict + pointer-sanity check, wired to the **existing** reconciler AEC3 fallback; surface the reason | Build now, minimal | small (mostly wiring) | neutral |
| **2 — Compensate (LATER, gated)** | extract `ChipRefRateController` + sinc resampler + servo; gains tuned **from Layer-0 data**; one-rate-owner test | Only when Layer 0 shows real drift on a real independent-clock DAC (JTS3) | the bulk (~550 lines) | reference branch → neutral |
| **3 — Delay trim (LATER, gated)** | one-time `SYS_DELAY` auto-trim at classify time | Only when a DAC's bulk delay lands out of the chip's window | tiny | neutral |
| **Orthogonal — Playback-latency retune** | outputd `512`-frame profile | Only after content-side xruns solved + a clean 30-min run | config + validation | **this is the Axis-A lever** |

**Layer 0 is the seed and the gatekeeper.** When a new DAC arrives, Layer 0
tells you instantly whether it is coherent (done, no new code), compensable
(then — and only then — build Layer 2), or a fallback case (Layer 1 already
handles it). You never build Layer 2/3 on spec.

## What we are NOT building (yet)

YAGNI lines, to resist over-engineering:

- The resampler/servo (Layer 2) before JTS3 measurement proves real drift —
  it is dead code on the coherent Apple profile.
- The full failure-mode taxonomy (composite, non-48k, out-of-window, …) up
  front. Grow one branch as each real DAC hits it.
- Residual-trim machinery / `SYS_DELAY` auto-trim before a DAC needs it.
- Any latency auto-tuner.
- Support for >2 independent DACs (a new sink design, not a config row).

## Build now — Layer 0 + thin Layer 1

One small PR, reviewable hardware-free, then deployed to JTS/JTS3/JTS5 to
capture reality.

**outputd (`rust/jasper-outputd`):** add an `aec_clock` block under
`reference_outputs` in `state.rs`, computed from the four existing counters:

```jsonc
"reference_outputs": {
  "aec_clock": {
    "chip_ref_sro_ppm": 1.2,          // long-window rate slope, ground-truth
    "sro_estimator_status": "observing", // observing | locked | untrusted
    "latency": {
      "dac_presentation_ms": 21.3,    // from dac.snd_pcm_delay
      "playback_queue_ms": 64.0,      // content + DAC buffer depth
      "chip_ref_queue_ms": 80.0       // chip-ref writer queue depth
    }
    // Layer 1 adds: "verdict": "coherent|compensable|fallback", "verdict_reason"
  }
}
```

- **No control, no resampler.** Pure measurement. This is how we run the
  `not_run` production-path drift measurement (deploy → watch SRO ≥30 min).
- **Thin Layer 1:** a minimal verdict (`coherent` if |SRO| ≈ 0 and pointers
  sane; else `compensable` candidate; `untrusted` if counters are
  implausible) wired to the AEC3 fallback `jasper-aec-reconcile` *already*
  has (`xvf_software_aec3` / `direct_mic`). Surface the reason in `/state`
  and `jasper-doctor` (`jasper/cli/doctor/audio.py`).
- **Tests (HW-free):** pin the SRO arithmetic, the latency math, the
  verdict thresholds, and the one-rate-owner invariant. No device I/O.

**Fallback contract (the safety floor):** every failure path —
untrusted pointers, USB downgraded to Full-Speed, composite without
per-child design, delay out of window, non-48k — degrades to **software
AEC3**, which needs no clock coherence. The speaker always plays and always
cancels echo *somehow*. Never a brick.

## Open unknowns / risks

- **Production-path long-window drift is `not_run`.** Every drift number so
  far is the old feeder harness (37–46 ppm, an artifact) or direct fanout
  (~1 ppm) — not the production outputd path. Layer 0 on JTS3 closes this.
- **`snd_pcm_delay` truthfulness** on the XVF USB endpoint is unverified.
  The classifier's pointer-sanity check is a mitigation, not a proof; budget
  a nonzero fallback rate.
- **PI-gain stability for a phase servo** is unverified until Layer-0 data
  exists. Do not ship gains derived from theory (the `content_bridge` gains
  are tuned for a buffer-fill servo, not a clock-phase servo).
- **RP1 USB PLL jitter** vs the chip tail's tolerance to delay *jitter*
  (distinct from accumulating drift) is unmeasured.

None of these brick the speaker (the AEC3 floor catches all); they bound how
often "just works" means *chip-AEC* vs *fell back to software*.

## Pointers

- Engine internals + software AEC3: [HANDOFF-aec.md](HANDOFF-aec.md)
- XVF3800 chip control / firmware / mixer: [HANDOFF-xvf3800.md](HANDOFF-xvf3800.md)
- Output owner + speaker-reference direction: [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
- Lab evidence (Option D, clock coherence): [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md)
- Diagnostic baseline / observability: [AEC-DIAG-01-baseline.md](AEC-DIAG-01-baseline.md), [AEC-DIAG-02-observability.md](AEC-DIAG-02-observability.md)
- DAC registry: [`jasper/audio_hardware/dac.py`](../jasper/audio_hardware/dac.py); reconciler: [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile)

Last verified: 2026-06-19
