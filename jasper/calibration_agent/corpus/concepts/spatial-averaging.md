# Spatial Averaging

> **Status: distilled from 2026-05-25 deep-research intake.**
> This note explains averaging choices; tool help owns supported capture modes.

## Operational Summary

A single mic point is one exact interference pattern. A household
listening area is a volume. If JTS wants the couch to sound better,
not just one capsule location, it needs multi-position or moving-mic
measurement and an averaging policy that matches the filter being
designed.

## Measurement Modes

| Mode | What it preserves | What it loses | Useful for |
|---|---|---|---|
| Single sweep | Magnitude, phase, impulse timing at one point | Spatial robustness | Setup smoke test, desk/single-seat mode, validation. |
| Multi-position sweeps | Per-seat response; repeats when captured | More user effort | Listening-area correction. |
| RMS / power average | Listening-area magnitude | Phase coherence | Magnitude EQ and target matching. |
| Vector average | Complex magnitude + phase | Robustness when phase varies across space | Alignment with a suitable timing and spatial basis. |
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

The moving-mic method can measure steady-state magnitude, but cannot replace
sweeps when the question needs impulse response, phase, or group delay.
This comparison does not imply that JTS has a moving-mic capture mode.

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
