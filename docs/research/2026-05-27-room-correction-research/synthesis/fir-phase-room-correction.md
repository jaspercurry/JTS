# FIR And Phase Room Correction - Synthesis

> **Status: research synthesis.** Distilled from
> [`../raw/fir-phase-room-correction-chatgpt.md`](../raw/fir-phase-room-correction-chatgpt.md),
> [`../raw/fir-phase-room-correction-claude.md`](../raw/fir-phase-room-correction-claude.md),
> and
> [`../raw/fir-phase-room-correction-gemini.md`](../raw/fir-phase-room-correction-gemini.md)
> on 2026-05-27. This is not current operational truth; use it to
> guide implementation planning.

## Bottom Line

FIR should be a staged capability ladder, not a promise that "FIR is
better." The reports agree that FIR is powerful for high-resolution
magnitude shaping, linear-phase work, and phase/excess-phase
correction, but also that it is easy to make worse than PEQ when the
measurement does not justify it.

JTS should build:

1. FIR runtime import/export and CamillaDSP `Conv` validation.
2. A FIR readiness validator before any auto-generation.
3. Minimum-phase FIR magnitude correction before linear or mixed phase.
4. Explicit latency, headroom, CPU, and pre-ringing reporting.
5. FDW-conditioned correction only when bundle artifacts are complete.
6. Mixed-phase/excess-phase correction as opt-in and evidence-gated.

The most important product decision is what JTS refuses to do
automatically.

## PEQ, Minimum Phase, Linear Phase, Mixed Phase

The reports align on the conceptual split:

- PEQ/IIR is already a minimum-phase correction tool. For broad
  low-frequency modal peaks, it is often the right tool.
- Minimum-phase FIR can express denser magnitude shaping than a small
  PEQ set while avoiding pre-ringing and large algorithmic latency.
- Linear-phase FIR preserves phase relationships but adds half-filter
  delay and can pre-ring around sharp corrections.
- Mixed-phase/excess-phase FIR can correct time-domain behavior that is
  not implied by magnitude alone, but only when the timing and spatial
  evidence prove the feature is real and stable.
- FDW is not a correction topology; it is measurement conditioning. It
  limits what the solver "sees" at each frequency so high-frequency
  correction is less likely to chase late reflections.

## Phase Correction Is Part Of FIR, But Not All FIR

The user's phase-correction question is answered clearly by the
reports: advanced room-correction products often use FIR or hybrid
FIR/IIR systems for phase/excess-phase correction, but FIR can also be
used for magnitude-only or minimum-phase work.

For JTS:

- Phase correction belongs on the roadmap.
- It should not be the default output of phone-browser measurements.
- It requires timing provenance, impulse responses, complex transfer
  functions, unwrapped phase, group delay, excess group delay,
  windows/FDW settings, and multi-position stability.
- It is especially relevant to speaker baseline / active crossover
  commissioning and stable low-frequency excess-phase features.
- It is risky for high-frequency room reflections, cancellation nulls,
  or any feature that changes across seats.

## FIR Ladder Recommendation

Use these stages as product and code gates:

### Stage 0: Runtime FIR Import/Export

- Verify CamillaDSP `Conv` filter support.
- Accept externally generated WAV/IR filters from REW, rePhase,
  DRC-FIR, Acourate, or similar tools.
- Validate config and filter file existence before apply.
- Persist filter file checksums and source metadata.
- Benchmark on Pi 5 1 GB.

This gives power users value without JTS taking responsibility for
automatic FIR design.

### Stage 1: Minimum-Phase FIR Magnitude Correction

- First automatic FIR stage.
- Same correction philosophy as current PEQ: bounded boosts, no null
  chasing, target discipline.
- Useful when more resolution than a small PEQ set is needed.
- Should still be explainable as magnitude correction, not "time-domain
  magic."

### Stage 2: Latency And Headroom-Audited FIR

- Every generated FIR reports:
  - sample rate
  - tap count
  - phase topology
  - estimated filter delay
  - CamillaDSP chunk/buffer latency estimate
  - predicted peak gain
  - required preamp/headroom reserve
  - CPU/memory estimate
- Filters with excessive boost or clipping risk are rejected or softened.

### Stage 3: FDW-Conditioned FIR

- Requires explicit window settings and multi-position data.
- Corrects from a response conditioned to emphasize direct sound at
  high frequencies and longer integration at low frequencies.
- This is the point where JTS must label the operation carefully:
  "speaker/direct-sound shaping plus bass room correction," not generic
  full-range room inversion.

### Stage 4: Mixed-Phase / Excess-Phase Opt-In

- Requires trusted timing reference.
- Requires multi-position stability in the target band.
- Requires broad, reproducible excess group delay rather than narrow,
  seat-specific artifacts.
- Requires pre-ringing risk analysis.
- Requires a one-click fallback to Stage 1/2 or PEQ.

## FIR Readiness Validator

The reports strongly agree that the validator is the central piece of
the architecture. It should produce machine-readable and user-readable
states such as:

- `PEQ_READY`
- `MIN_PHASE_FIR_READY`
- `SHORT_FIR_READY_WITH_LATENCY_WARNING`
- `FDW_FIR_READY`
- `MIXED_PHASE_PROVISIONAL`
- `MIXED_PHASE_UNSAFE`
- `UNSAFE`

Each verdict must include reasons, not just a label.

Recommended score inputs:

- data quality: clipping, SNR, coherence/repeatability, ambient noise
- timing confidence: loopback > acoustic reference > no reference
- phase trust: unwrap stability, plausible delay, excess group delay
- minimum-phase likelihood: flat excess group delay, measured phase
  close to minimum phase after pure delay removal
- spatial stability: variance across positions and channels
- boost safety: no large inverse gain into notches or nulls
- pre-ringing risk: filter impulse energy before the main peak,
  target sharpness, topology, and predicted step response
- runtime budget: latency, CPU, memory, CamillaDSP chunk behavior

## Measurement Bundle Requirements

Before generated FIR, persist:

- raw capture or reproducible sweep record
- deconvolved impulse response before and after windowing
- complex transfer function
- magnitude, wrapped phase, unwrapped phase
- group delay and excess group delay
- window and FDW settings
- timing-reference provenance
- per-position and per-channel responses before averaging
- spatial variance metrics
- noise, clipping, coherence/repeatability metrics
- mic calibration file and checksum
- target curve and smoothing/regularization settings
- generated filter files with checksums
- design report and rejected-candidate reasons
- post-apply verification linkage

The key detail is not to collapse artifacts too early. Keep
per-position, per-channel complex data. A mixed-phase decision made
from a prematurely averaged magnitude curve cannot be audited later.

## Runtime And Pi 5 Notes

The reports are optimistic about Pi 5 performance, but the estimates
are extrapolated. The common public reference is CamillaDSP performing
large FIR workloads on Pi 4; Pi 5 should have ample headroom for
sensible 2-channel FIR, but JTS still needs local benchmarks.

Important runtime details:

- CamillaDSP uses FFT/segmented convolution for long FIR.
- Chunk size trades CPU for latency and underrun safety.
- Larger chunks reduce FIR CPU but increase latency.
- Minimum-phase FIR does not add half-filter latency the way symmetric
  linear-phase FIR does.
- Linear-phase FIR delay is about `(taps - 1) / (2 * sample_rate)`.
- Long linear-phase filters are not appropriate for interactive/video
  use unless the user accepts the delay.
- CamillaDSP hot reload may require new coefficient filenames for FIR
  file changes to be picked up reliably.
- Even cut-only filters can increase peak sample values because phase
  relationships change, so headroom audit remains mandatory.

## Pre-Ringing Policy

The reports agree that pre-ringing risk is tied to acausal or
symmetric energy before a transient:

- Minimum-phase filters are the safest default because they do not
  create pre-impulse energy.
- Linear-phase filters can pre-ring around sharp, narrow, high-gain
  corrections.
- Mixed-phase excess-phase inversion can pre-echo if it tries to cancel
  reflections or unstable spatial artifacts.
- FDW and excess-phase windowing are main defenses.
- JTS should simulate the designed FIR before applying it.

Recommended checks:

- pre-peak energy ratio
- max pre-peak amplitude relative to main impulse
- step-response undershoot or oscillation
- narrow/high-Q correction detection
- excessive boost detection
- unstable excess group delay detection

If JTS cannot explain where the pre-peak energy came from, it should
not auto-apply the filter.

## Prior-Art Lessons

- REW is the template for artifact visibility: phase, group delay,
  minimum phase, excess phase, windows, FDW, and averaging choices.
- DRC-FIR is the strongest open-source conceptual reference for guarded
  FIR inversion, dip limiting, FDW, and excess-phase controls.
- rePhase is important for manual FIR workflows and phase/crossover
  work, but does not solve automated validation.
- Acourate and Audiolense show that serious FIR workflows require
  disciplined measurement conditioning.
- Dirac and Trinnov make mixed-phase market-legible, but public docs do
  not reveal enough validator logic to copy.
- Anthem ARC is a useful counterpoint because it reportedly avoids
  mixed phase to avoid artifacts. "Sophisticated" can mean refusing a
  risky feature.

## Implementation Implications For JTS

- Do not build a generated FIR path before the readiness validator.
- Extend current bundle schema toward FIR-readiness artifacts even
  before using them.
- Add Pi-side benchmark commands and doctor surfaces for FIR runtime
  capacity.
- Keep FIR import/export separate from FIR generation.
- Keep FIR generation strategy explicit in the design audit.
- Make all FIR output reversible through the existing DSP apply
  substrate.
- Treat active crossover FIR work as related but separate from room
  correction; it has different evidence and safety assumptions.

## Open Questions To Verify

- Real CamillaDSP FIR CPU, memory, thermals, and underrun behavior on a
  Pi 5 1 GB with JTS's actual services running.
- Whether JTS can obtain timing provenance good enough for excess-phase
  work with browser/phone capture.
- Which FDW algorithm should ship first: REW-style cycles, DRC-FIR
  band-windowing, or a simpler validated subset.
- What pre-ringing thresholds are conservative enough for automatic
  deployment.
- Whether mixed-phase correction should ever be automatic, or always a
  power-user opt-in with validation.
- How to represent FIR latency to users without making the normal room
  correction flow intimidating.

Last synthesized: 2026-05-27
