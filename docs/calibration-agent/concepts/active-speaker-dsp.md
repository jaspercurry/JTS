# Active Speaker DSP

> **Status: current concept note.** Operational planning lives in
> [`../../HANDOFF-active-speaker-dsp.md`](../../HANDOFF-active-speaker-dsp.md).
> This page is the calibration-agent corpus summary.

## Core Model

Active speaker commissioning is Layer A: the speaker baseline. It is
separate from Layer B room correction and Layer C preference voicing.

- **Layer A speaker baseline**: per-driver linearization, baffle-step
  compensation, acoustic-target crossover, polarity, time alignment,
  gain trim, and per-driver limiters. This is commissioned once per
  hardware build or unit and stored as a versioned speaker profile.
- **Layer B room correction**: listening-position or listening-area
  correction, mostly modal-region and spatial-average behavior. This
  is re-run when the room or placement changes.
- **Layer C preference voicing**: house curve, target tilt, bass/treble
  taste, and subjective "brighter / warmer / more bass" adjustments.
  This is reversible user preference, not accuracy.

The important rule for future agents: do not use room correction to
hide a speaker-baseline problem, and do not bake preference voicing
into the baseline.

## Measurement Triad

The proposed consumer wizard uses three complementary measurements:

1. **Near-field per-driver capture** catches individual driver and
   assembly deviations while overwhelming room reflections.
2. **Null-depth optimization** verifies polarity and delay through each
   crossover by maximizing the inverted-polarity null.
3. **Gated at-position summed measurement** validates the direct
   acoustic sum through the crossover region above the gate-derived
   low-frequency limit.

Below roughly 300 Hz, single-position in-room data is not a clean
speaker-baseline measurement; hand that region to room correction.
Around 300-500 Hz, especially for 3-way lower crossovers, confidence
depends on the available gate length and the engineering preset.

## DSP Shape

CamillaDSP templates should be bounded and preset-driven:

```text
stereo input
  -> room correction / preference layers when enabled
  -> baseline pre-split filters such as BSC
  -> split_2way or split_3way mixer
  -> per-driver crossover(s)
  -> per-driver EQ
  -> per-driver delay
  -> per-driver gain trim
  -> per-driver limiter
  -> physical outputs
```

Polarity belongs in the mixer mapping (`inverted: true`). Limiters
belong last in each per-driver chain. The active baseline profile must
be stored separately from room-correction bundles and preference
profiles.

Implementation note, 2026-06-01: the first active-speaker substrate
lives in `jasper.active_speaker`. It validates presets, output channel
maps, safety envelopes, crossover regions, and baseline acceptance
evidence, and emits muted/protected startup templates for manual
inspection, but does not load CamillaDSP configs yet.

## LLM Boundary

An LLM can explain why a null test failed, ask whether timing
reference and calibration were valid, and recommend which deterministic
check to run next. It must not invent filter taps, remove tweeter
protection, write arbitrary CamillaDSP YAML, or call magnitude-only
data valid for phase alignment.

Last verified: 2026-06-01
