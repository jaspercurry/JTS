# Room Correction Limits

> **Status: distilled from 2026-05-25 deep-research intake.**
> This file is about physics boundaries, not JTS UI copy.

## Operational Summary

Room correction is a constrained inverse problem. EQ can reduce
repeatable excess energy, especially low-frequency modal peaks. It
cannot make every mic position flat, repair deep physical
cancellations, or turn poor loudspeaker directivity into good
directivity. JTS should explain these limits before offering more
powerful filters.

## Correctable Vs Not Correctable

| Observation | Usually safe to correct? | JTS handling |
|---|---:|---|
| Broad, repeatable bass peak | Yes | Cut toward target with bounded PEQ or FIR. |
| Long-decay room mode | Often | Prefer cuts; show decay plot when available. |
| Deep narrow null at one position | No | Warn, avoid boost, suggest placement or re-measure. |
| SBIR cancellation | No | Suggest moving speaker/listener or boundary treatment. |
| Broad tonal tilt | Yes, as target/preference | Keep separate from physical correction. |
| Narrow high-frequency combing | No | Treat as position/reflection artifact unless repeatable and speaker-derived. |
| Speaker directivity mismatch | No, not with room EQ | Explain hardware/placement limitation. |

## Transition Region

Small rooms behave differently below and above the transition /
Schroeder region. Below roughly 200-300 Hz in many domestic rooms,
modal behavior dominates and EQ is often useful. Above that region,
the listener hears a mixture of direct sound, early reflections, and
room power; narrow steady-state correction becomes increasingly
position-sensitive.

JTS should therefore default to:

- bass/modal correction first;
- broad correction only through the transition region;
- no automatic narrow correction above the transition region;
- target/preference shaping as a distinct layer.

## Nulls And SBIR

A null is a cancellation geometry. Boosting the speaker also boosts
the delayed reflection, so the cancellation remains while headroom,
distortion, and adjacent-seat boom get worse. A deterministic JTS
filter designer should flag likely nulls when a deep dip is narrow,
varies strongly across positions, or is paired with non-minimum-phase
behavior / excess group delay.

## JTS Design Implications

- Prefer cuts over boosts in the room-correction layer.
- Cap boosts tightly and reject attempts to fill deep nulls.
- Store enough measurement artifacts to distinguish modal peaks from
  cancellations: raw capture, impulse response, smoothed and
  unsmoothed response, phase/group delay when available, and
  multi-position measurements.
- Explain that acoustic placement can solve some problems DSP cannot.
- Keep "sounds better to me" controls in preference EQ, not the
  physical correction bank.

## Key Sources

- Neely and Allen, *Invertibility of a Room Impulse Response*.
- Floyd Toole, *The Measurement and Calibration of Sound Reproducing
  Systems*.
- REW help: "Why can't I fix all my acoustic problems with EQ?"
- HouseCurve documentation on broad correction and multi-position
  measurement.

## Open Questions

- What JTS-specific transition-frequency heuristic should we expose
  when the user has not entered room dimensions?
- How much boost is acceptable below 100 Hz when multiple positions
  agree and the speaker has known headroom?
- Which null detector should be used in v1: magnitude-only,
  seat-variance, group-delay, or a composite confidence score?

Last verified: 2026-05-25
