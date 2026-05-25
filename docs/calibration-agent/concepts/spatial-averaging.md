# Spatial Averaging

> **Status: distilled from 2026-05-25 deep-research intake.**
> This file captures measurement-area logic for future correction and
> tuning flows.

## Operational Summary

A single mic point is one exact interference pattern. A household
listening area is a volume. If JTS wants the couch to sound better,
not just one capsule location, it needs multi-position or moving-mic
measurement and an averaging policy that matches the filter being
designed.

## Measurement Modes

| Mode | What it preserves | What it loses | Best JTS use |
|---|---|---|---|
| Single sweep | Magnitude, phase, impulse timing at one point | Spatial robustness | Setup smoke test, desk/single-seat mode, validation. |
| Multi-position sweeps | Per-seat response and repeatability | More user effort | Default for living-room correction. |
| RMS / power average | Listening-area magnitude | Phase coherence | Magnitude EQ and target matching. |
| Vector average | Complex magnitude + phase | High-frequency robustness across space | Subwoofer / low-frequency alignment only. |
| Moving mic method | Fast spatial steady-state magnitude | Phase / impulse details | Consumer-friendly target/preference measurement. |

## Averaging Rules

- Use RMS / power averaging for broad magnitude correction across a
  listening area.
- Use vector averaging only where wavelengths are long enough for
  phase to remain meaningful across positions.
- Never let a peak at one point and a null at another average into
  false "flatness" without exposing seat variance.
- Keep individual positions in the bundle even when the UI shows an
  average.

## UX Implications

For a phone-first JTS flow, multi-position should feel ordinary:

1. Measure the main listening position.
2. Move the phone slightly left, right, forward, and back.
3. Show whether the bass problems are shared across positions.
4. Correct only what is repeatable enough to trust.

The moving-mic method may become a useful later mode for quick
steady-state preference tuning, but it cannot replace sweeps when JTS
needs impulse response, phase, group delay, or excess-phase data.

## Bundle Requirements

Persist:

- each raw capture separately;
- position labels and measurement order;
- derived impulse response per position;
- per-position smoothed and unsmoothed response;
- spatial average method and settings;
- seat-variance/confidence metrics;
- which averaged response drove each generated filter.

## JTS Design Implications

- Multi-position data is a prerequisite for broader correction and
  any claim about a room/listening area.
- Single-position data can still support conservative low-frequency
  cuts, but should carry lower confidence.
- Future LLM guidance should reference seat variance: "this peak is
  shared across positions" is very different from "this dip appears
  only in one spot."

## Key Sources

- Elliott and Nelson, *Multiple-Point Equalization in a Room Using
  Adaptive Digital Filters*.
- Welti and Devantier, *Low-Frequency Optimization Using Multiple
  Subwoofers*.
- HouseCurve documentation on multi-position measurement.
- REW documentation on averages, phase, and impulse interpretation.

Last verified: 2026-05-25
