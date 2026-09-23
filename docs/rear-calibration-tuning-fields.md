# Rear calibration handoff — tuning fields

What the fields of the `jts_rear_calibration` document mean
(`jasper/active_speaker/rear_calibration.py`, `read_rear_calibration`).
Decisions live in [ADR-0316](adr/0316-rear-woofer-outputs-have-a-physical-variant-identity.md),
[ADR-0317](adr/0317-wall-placement-starts-at-the-cabinet-back.md),
[ADR-0318](adr/0318-rear-calibration-separates-acoustic-targets-from-electrical-settings.md)
and [ADR-0322](adr/0322-rear-calibration-is-a-candidate-section.md).
Examples: [`docs/examples/rear_calibration_handoff.json`](examples/rear_calibration_handoff.json)
(blank `acoustic_targets` skeleton) and
[`docs/examples/rear_calibration_electrical_example.json`](examples/rear_calibration_electrical_example.json)
(`diagnostic_seed(48000)` output).

## Authoring path

An authored `electrical_dsp` document enters as the `rear_calibration`
section of a `jts_prescription` document. `jasper-crossover-prescriber
compose` banks it onto the document's `base` (`saved` or a fingerprint); the existing
baseline-profile apply (`jasper-round apply <fingerprint>`) is the only
path that carries a banked candidate onto the box, and the wizard's
editing panel calls that same judge/compose/apply path in-process rather
than writing around it. See [ADR-0322](adr/0322-rear-calibration-is-a-candidate-section.md).
The branch filters themselves can be fitted from a target table, or re-fitted
from measured front-alone and rear-alone responses, with
[`scripts/fit-rear-branches.py`](../scripts/fit-rear-branches.py) — its
`--help` carries the input shapes and the phase/sign conventions.

## Fields and bounds

`jasper-crossover-prescriber contract --section rear` prints every field, type
and range a candidate may carry. The playbook's generated
[Current bounds](tuning-playbook.md#current-bounds) block lists the same Rear
rows. `read_rear_calibration` also reads the `acoustic_targets` case and the
`fir` rear mode, which a candidate cannot carry; its code owns their fields.

The schema does not state these meanings:

- `included_stages.<side>` lists the stages already applied upstream of the
  document, so a composer does not apply them twice.
- `geometry.cabinet_back_wall_m` is ADR-0317's perpendicular distance from the
  rear-panel centre to the wall.
- `front.delay_ms` is relative to the stage input. A rear branch's `delay_ms`
  is relative to the front reference.
- `common_delay_ms` is added to all three declared cabinet outputs: front,
  rear branches and tweeter.
- `rear_muted: true` mutes the summed rear output, whatever the branch gains.
- `conditions` is free-form. Use it for measurement setup notes.
- In `fir` mode the coefficients replace both branches. `added_latency_ms` is
  the FIR's own declared group delay. A composer that aligns the whole graph
  must account for it; the compiler does not add it again.

## Phase convention

`positive_delay_has_negative_phase`: a pure delay of τ seconds at frequency
f Hz produces phase `-360 · f · τ` degrees. Boundary Lab / CAD BEM data is
declared and stored as `exp(-iωt)`; **negate its phase angle** before
importing into a `targets.front`/`targets.rear` pair — the two conventions
are opposite in sign, magnitude is unaffected.

## Acoustic targets vs. electrical settings

`acoustic_targets` is a raw complex-valued transfer (simulated source
weights or a measured capture) with no claim of causal-DSP realizability.
`electrical_dsp` is a concrete, causal set of CamillaDSP filters that
`compile_rear_stage` compiles directly into pipeline steps. An
`acoustic_targets` document validates but `compile_rear_stage` refuses to
compile it (`case != "electrical_dsp"`) — it still needs electrical
conversion and causal fitting.

## What's operational, provisional, and unmeasured

**OPERATIONAL** (live code): document validation (`read_rear_calibration`);
stage compile for `electrical_dsp`/`branches` (`compile_rear_stage`),
reachable via `jasper-crossover-prescriber rear-calibration --document ...
--channels --front --rear --tweeter`; the seed CLI
(`rear-calibration --seed --sample-rate <hz>`).

**PROVISIONAL** (untuned, seeded from a single idealized snapshot, not
measured): the diagnostic seed's cancellation branch (`-0.84 dB`,
inverted, `1.14 ms`) reproduces only the ideal CAD model's ~200 Hz ratio,
not a broadband fit; the seed's `cabinet_back_wall_m` default (`0.2032 m`)
is a CAD reference geometry, not this cabinet's declared value (compare
against `jasper-declare-geometry`; a mismatch is disclosed, never blocked,
ADR-0322); any `valid_band_hz` or filter fit an acoustic task later derives
from the CAD dataset, since that model is an idealized free-field (plus one
image-source wall estimate) BEM computation, not a measurement.

**MEASURED:** the front/rear/both pair take reports `rear_polarity` and
`arrival_gap` in `crossover_v2/rear_views.py`. Measured tunes exist; see the
[playbook's Rear chapter](tuning-playbook.md#rear) for results and their limits.

**MODEL ONLY:** the CAD ideal target remains an acoustic model, not a
measured driver transfer or proof that its source-motion weights give the
required amplifier/DSP gains. The measured pair and tune results do not
validate that ideal target.
