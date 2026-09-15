# ADR-0318: Rear calibration separates acoustic targets from electrical settings

- **Date:** 2026-09-15
- **Status:** Accepted
- **Extends:** ADR-0316, ADR-0317

## Decision

`jasper.active_speaker.rear_calibration` owns the version-1
`jts_rear_calibration` document and its CamillaDSP stage builder. A separate
acoustic task returns a document; it does not edit the audio software.
One document describes one cabinet. Front/rear source identities bind it to
that cabinet; stereo has two documents. Unknown identities, reference levels,
geometry details and valid frequency range remain null in the diagnostic seed.

The `case` is either `acoustic_targets` or `electrical_dsp`. Both declare
sample rate, phase convention, source geometry, cabinet-back wall distance,
reference quantity/units/level, conditions, assumptions, valid band and
included correction stages. `cabinet_back_wall_m` uses ADR-0317's perpendicular
rear-panel-centre measurement. Geometry details carry source positions, cabinet
depth, toe-in and room dimensions when known; conditions carry measurement
positions, timing reference and dataset identity. No gap-to-delay conversion,
wall gain or cardioid bandwidth change is inferred.

Acoustic targets carry `targets.frequency_hz` and complete `front` and `rear`
arrays of `[real, imaginary]` values on that grid, with positive delay having
negative phase. A zero rear target is `[0, 0]`; no phase is invented for it.
Quantity distinguishes acoustic motion from pressure per electrical input.
The full saved dataset belongs here, not just selected ratio-table rows.
Electrical conversion, stable causal fitting, achieved magnitude/phase error,
forward response and rear suppression remain separate acoustic-task work.
An acoustic target document cannot compile as an electrical stage.

Electrical documents expose front correction and either two rear branches
(`bass` and `cancellation`) or a complete rear FIR. Each chain has dB gain,
inversion, delay in ms, mute and explicit filters. Front delay is from the
stage input; rear branch delays are relative to that front reference.
`common_delay_ms` adds latency to all three declared cabinet outputs when a
negative relative delay requires it. The compiler never emits negative delay.

Filters use CamillaDSP's native Biquad or BiquadCombo parameters: frequency
in Hz, Q and gain for peaking/shelves, frequency/Q for high-pass, low-pass and
all-pass, or frequency/order for Butterworth and Linkwitz-Riley combinations.
Fractional delay uses `subsample: true`. Syntax follows the
[CamillaDSP 4.1.3 documentation](https://github.com/HEnquist/camilladsp/blob/v4.1.3/README.md).

A rear FIR replaces both branches. Version 1 carries inline coefficients,
sample rate, `normalization: as_supplied`, declared added latency and SHA-256
of the consecutive little-endian float64 coefficients. It does not normalize
or add the declared latency again. The composer must account for that latency
when aligning the complete front/rear/tweeter graph. Boundary filter lists are
separate; adding them when the imported path already includes boundary
correction is refused. Included crossover, driver correction and protection
stages remain explicit so the composer can avoid applying them twice.

## Interface and integration boundary

`jasper-crossover-prescriber rear-calibration --seed --sample-rate <installed-Hz>`
prints editable data: 0.2032 m gap, neutral front and bass, and the diagnostic
cancellation seed of -0.84 dB / inverted / 1.14 ms. Rear output starts muted;
filter lists are empty pending fitting. This is not a broadband electrical tune.

`rear-calibration --document <file>` validates either case. Adding explicit
`--channels`, `--front`, `--rear`, and `--tweeter` assignments compiles an
electrical stage, using zero-based indices supplied by the caller. The result
contains filters, mixers and pipeline steps, without devices or an apply call.
It preserves all other physical channels and sums both branches into one rear
output. No new configuration store or writer is introduced.

This is the handoff/stage slice of #5161. Runtime adoption through the existing
candidate composer, common headroom accounting, protection proof, and timed
measurement routing must consume this stage before removing ADR-0316's pending
rear mute. The tool does not bank, adopt, play or claim a measured calibration.
