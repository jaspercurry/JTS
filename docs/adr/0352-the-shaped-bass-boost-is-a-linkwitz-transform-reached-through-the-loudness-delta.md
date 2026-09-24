# ADR-0352: The shaped bass boost is a Linkwitz transform reached through the Loudness delta

- **Date:** 2026-09-23
- **Status:** Accepted. §2–§4, §1's delta-zero rule and the fader part of §5
  are superseded by [ADR-0359](0359-the-bass-boost-plays-at-every-volume-and-gives-way-only-near-clip.md).

## Context

[Issue #5692](https://github.com/jaspercurry/JTS/issues/5692): jts3's woofer
pair rolls off like a 90 Hz, Q 0.6 alignment, and the native Loudness shelf
(fixed 70 Hz corner) matches it at no setting. The owner wants the Linkwitz
transform T from the pair's alignment (f0, Q0) to a target (ft, Qt) at full
boost. The block must keep the volume taper (the native Loudness on `Aux1`),
the compressor with one reduction per cardioid pair (ADR-0335), the sub-bass
roll-off, and the shared headroom charge (ADR-0257). Aux1's only readers in
CamillaDSP v4.1.3 are `Volume` and `Loudness`, so no native filter scales an
arbitrary shape with the volume.

## Decision

1. The bass section may carry `linkwitz_transform`: `source_hz`, `source_q`,
   `target_hz`, `target_q`. `jasper/bass_extension/dynamic.py` owns its
   bounds. The judge refuses a shape without `delta_highpass_hz`, a target
   at or above the source, and a transform whose delta zero,
   (f0² − ft²) / (f0/Q0 − ft/Qt), is not in (0, 20 kHz]. One `LowshelfFO`
   cannot place such a zero.
2. The Loudness delta L − 1 stays the only volume-dependent element. A fixed
   stage on each formed delta, S = (T − 1) / (L_B − 1) at the full boost B,
   makes the full-boost response exactly T. The stage is a gain on the
   `form_delta` mixer, a `LinkwitzTransform` that moves the shelf's pole pair
   (70 Hz / 10^(B/80), Q 0.7071) to the target, and a `LowshelfFO` that moves
   the shelf delta's zero to the transform's. The delta high-pass, the
   compressors and the reduce mixer follow unchanged.
3. Below full boost, the shelf's own growth scales the shaped delta. The
   boost reaches zero at the reference. |L_g − 1| grows with g at every
   frequency (checked 0–20 dB on a 5,000-point grid), so the full-boost
   reserve bounds every fader.
4. The detector keeps reading the shelf-boosted front woofer (ADR-0335).
   For jts3 at full boost, it reads within 2.5 dB of the shaped output from
   15 to 125 Hz.
5. `expected_boost_db` evaluates the emitted stage with CamillaDSP v4.1.3's
   own coefficient formulas. `dynamic_bass_gain_reserve_db` takes an optional
   fader. For a shape, it is the peak of 1 + |delta| on a 1/48-octave grid,
   plus 0.01 dB. That grid reads a Q ≤ 1.5 peak at most 0.003 dB low. The
   shelf keeps its closed form.

Proof, jts3 (B = 20 dB, delta high-pass 15 Hz): the stage is −0.11 dB,
`LinkwitzTransform` 39.36 Hz / 0.7071 → 22 Hz / 0.707, and `LowshelfFO`
86.15 Hz / −5.15 dB. The model differs from the analog closed form
1 + HP·(T − 1) by 0.00014 dB at most, 5 Hz–20 kHz. Across 4,728 bounded
descriptors (B from 1 to 20 dB, high-pass 10–19 Hz), the maximum is
0.005 dB. The full-boost boost is +21.6 dB at 20 Hz, +20.5 at 25, +15.2 at
40, +9.3 at 63, +4.6 at 100 and +1.9 at 160. At 25 Hz, the taper gives
+20.5, +15.8, +10.8, +5.6 and +2.3 dB at 20, 15, 10, 5 and 2 dB below the
reference. The reserve is 21.7 dB. On an ideal 90 Hz, Q 0.6 woofer, the
result is −3 dB at 22.7 Hz and within −2.0/+1.1 dB from 25 to 250 Hz.

## Consequences

- A document without the field emits the same graph bytes as before. It
  gives the same model, reserve and payload, so banked fingerprints do not
  move (tests pin this).
- With a shape, `low_boost_db` sets how the boost fades and what the
  detector reads: the detector sees the shelf at `low_boost_db`, not the
  shape. It belongs at or just above the shape's peak boost (the reserve at
  full boost); below it the compressor acts late by the difference. Nothing
  bounds that yet (#5704); the owner limiter, admission's reserve charge and the SPL
  stop hold either way. The delta high-pass is now required for a shape,
  and its phase adds about 1 dB near 80 Hz at 15 Hz under a 22 Hz target.
- The static graph proof and `graph_transfer` do not model
  `LinkwitzTransform` or `LowshelfFO`. They skip or strip the dynamic block,
  as before.
- Rejected: the stage without its zero correction (3.8 dB error at 90 Hz);
  a coordinator-written taper fader (its 0 dB default is full boost); a
  Loudness `attenuate_mid` taper (half the fade falls in the top 2 dB);
  a detector on the shaped output (one more channel and mixer per pair); a
  `Gain` filter for the constant (graph verifiers scan `Gain` filters); raw
  `Free` biquads (bound to one sample rate).
- Owed: the live ladder (65/75/82 dB) and a near-field check on jts3.
