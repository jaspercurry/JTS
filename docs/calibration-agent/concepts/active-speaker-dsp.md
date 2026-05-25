# Active Speaker DSP

> **Status: initial concept.** This file captures the overlap between
> JTS room correction and active speaker commissioning for speakers
> where CamillaDSP drives woofer and tweeter channels separately.

## Operational Summary

Active crossover tuning is a different layer from room correction.
Room correction asks, "what should we compensate at the listening
position?" Active speaker commissioning asks, "what should this
speaker be before the room is considered?"

For a two-way active JTS speaker, CamillaDSP may own:

- woofer/tweeter crossover filters;
- per-driver gain trims;
- polarity;
- driver delay / acoustic-center alignment;
- phase behavior through the crossover region;
- driver protection filters and limiters;
- optional driver-response equalization.

That baseline should be measured, stored, and versioned separately
from room correction. Room correction should not silently rewrite the
crossover.

## Relationship To Phase Correction

Driver phase alignment around the crossover is one of the clearest
places where phase work can be useful. Unlike room reflections, the
woofer/tweeter relationship is repeatable and tied to the speaker's
physical design. A bad delay, polarity choice, or crossover topology
can create a cancellation or lobe around the crossover frequency that
no normal room-correction pass should try to hide.

FIR may be useful later, but first-class active speaker DSP does not
require jumping straight to mixed-phase FIR. The staged path is:

1. known-safe IIR crossover and limiter profile;
2. measure woofer-only and tweeter-only responses safely;
3. inspect summed response, polarity, delay, and phase around the
   crossover;
4. tune delay/gain/polarity and crossover parameters deterministically;
5. consider FIR or mixed-phase work only after the basic acoustic sum
   is understood.

## JTS Guardrails

- Always provide a safe mute / restore path for individual driver
  measurement.
- Do not run tweeter sweeps at unsafe levels or below its protected
  range.
- Store speaker-baseline profiles separately from room-correction
  sessions.
- Make the active profile explicit in every measurement bundle so a
  later room-correction pass knows what speaker baseline was active.
- Treat crossover/driver alignment as an expert or guided setup flow,
  not as a casual end-user "make it sound better" control.
- Keep preference EQ after the speaker baseline and room correction.

## Open Questions

- What crossover topology should be the safe JTS default for the
  selected woofer/tweeter pair?
- What measurement protocol gives enough nearfield / listening-axis
  data without requiring lab equipment?
- How should CamillaDSP channel routing represent "speaker profile"
  versus "room profile" so rollback is obvious?
- What phase / group-delay plots are necessary before an LLM can give
  useful guided explanations?

## Sources To Research

- Linkwitz-Riley / Butterworth / Bessel active crossover design.
- Loudspeaker Measurement and Design / VituixCAD workflows.
- REW driver timing and acoustic-center alignment workflows.
- CamillaDSP channel routing, IIR filters, FIR convolution, and
  limiter behavior.
- AES / loudspeaker engineering references on directivity, lobing,
  crossover phase, and driver protection.

Last verified: 2026-05-25
