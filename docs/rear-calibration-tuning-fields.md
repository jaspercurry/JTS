# Rear calibration handoff — tuning fields

Field-by-field reference for the `jts_rear_calibration` document
(`jasper/active_speaker/rear_calibration.py`, `read_rear_calibration`).
Decisions live in [ADR-0316](adr/0316-rear-woofer-outputs-have-a-physical-variant-identity.md),
[ADR-0317](adr/0317-wall-placement-starts-at-the-cabinet-back.md),
[ADR-0318](adr/0318-rear-calibration-separates-acoustic-targets-from-electrical-settings.md)
and [ADR-0322](adr/0322-rear-calibration-is-a-candidate-section.md) — this
page only enumerates fields, units, and ranges taken from the validator.
Examples: [`docs/examples/rear_calibration_handoff.json`](examples/rear_calibration_handoff.json)
(blank `acoustic_targets` skeleton) and
[`docs/examples/rear_calibration_electrical_example.json`](examples/rear_calibration_electrical_example.json)
(`diagnostic_seed(48000)` output).

## Authoring path

An authored `electrical_dsp` document enters as the `rear_calibration`
section of a `jts_prescription` document. `jasper-crossover-prescriber
compose --base saved|<fingerprint>` banks it onto a candidate; the existing
baseline-profile apply (`jasper-round apply <fingerprint>`) is the only
path that carries a banked candidate onto the box, and the wizard's
editing panel calls that same judge/compose/apply path in-process rather
than writing around it. See [ADR-0322](adr/0322-rear-calibration-is-a-candidate-section.md).

## Document header (both cases)

| Field | Type / range |
|---|---|
| `kind` | must equal `"jts_rear_calibration"` |
| `schema` | must equal `1` |
| `case` | `"acoustic_targets"` or `"electrical_dsp"` |
| `sample_rate_hz` | positive int; must match the DSP's selected rate |
| `phase_convention` | must equal `"positive_delay_has_negative_phase"` |
| `valid_band_hz` | `[low_hz, high_hz]`, `0 < low < high < sample_rate_hz/2`. Required for `acoustic_targets`; may be `null` only for `electrical_dsp` |
| `assumptions` | list of strings, may be empty |
| `included_stages.front` / `.rear` | each `null`, or a list drawn from `{crossover, driver_correction, boundary_correction, protection}` — stages already applied upstream of this document, so a composer does not double them. May be `null` for `acoustic_targets`; both sides must be non-`null` lists for `electrical_dsp` |

## Geometry

| Field | Type / range |
|---|---|
| `geometry.cabinet_back_wall_m` | `null`, or a number `>= 0` (metres); ADR-0317's perpendicular rear-panel-centre-to-wall distance |
| `geometry.sources.front` / `.rear` | exactly these two keys present; values are not otherwise validated (free-form source identity/position) |
| `geometry.details` | not validated (free-form: toe-in, cabinet depth, room dimensions, etc.) |

## Conditions

`conditions` is an arbitrary mapping — any keys, not otherwise validated.
Use it for measurement setup notes (mic distance, ambient noise, dataset id).

## Reference

| Field | Type / range |
|---|---|
| `reference.quantity` | `acoustic_targets`: `"acoustic_motion"` or `"pressure_per_electrical_input"`. `electrical_dsp`: must equal `"electrical_filter_transfer"` |
| `reference.units` | non-empty string, free text (e.g. `"m"`, `"linear output/input"`) |
| `reference.level` | not validated (any JSON value, including `null`) |

## Acoustic targets (`case == "acoustic_targets"` only)

| Field | Type / range |
|---|---|
| `targets.frequency_hz` | list of >= 2 numbers, strictly increasing, each within `[valid_band_hz[0], valid_band_hz[1]]` |
| `targets.front` / `.rear` | list of `[real, imaginary]` finite-number pairs, same length as `frequency_hz` |

## Front chain, bass branch, cancellation branch (`case == "electrical_dsp"` only)

`front`, `rear.bass`, and `rear.cancellation` are each a **chain**:

| Field | Type / range |
|---|---|
| `gain_db` | number, CamillaDSP's own `-150..150` dB range (runtime adoption further clamps rear branches to `<= 0` dB, ADR-0322) |
| `inverted` | bool |
| `delay_ms` | any finite number. `front.delay_ms` is relative to the stage input; branch `delay_ms` is relative to the front reference. The compiler refuses a branch whose `common_delay_ms + front.delay_ms + branch.delay_ms` is negative — realize a negative relative rear delay by raising `common_delay_ms` instead |
| `muted` | bool |
| `filters` | list of filter entries, see below |

## Boundary filters

| Field | Type / range |
|---|---|
| `boundary.front` / `.rear` | each a filter list (same schema as chain `filters`). `"boundary_correction"` cannot appear in `included_stages.<side>` while `boundary.<side>` is non-empty — declare it upstream-included, or here, never both |

**Filter entries** (chain `filters` and `boundary.*`): `{type, parameters}`.

| `type` | `parameters.type` (kind) | Required keys | Range |
|---|---|---|---|
| `Biquad` | `Highpass`, `Lowpass`, `Allpass` | `type, freq, q` | `0 < freq < sample_rate_hz/2`; `q > 0` |
| `Biquad` | `Peaking`, `Lowshelf`, `Highshelf` | `type, freq, q, gain` | as above, plus `gain` finite (dB) |
| `BiquadCombo` | `ButterworthHighpass`, `ButterworthLowpass` | `type, freq, order` | `order` a positive int |
| `BiquadCombo` | `LinkwitzRileyHighpass`, `LinkwitzRileyLowpass` | `type, freq, order` | `order` a positive **even** int |

## FIR alternative (`rear.mode == "fir"`)

| Field | Type / range |
|---|---|
| `rear.coefficients` | non-empty list of finite numbers |
| `rear.sample_rate_hz` | int, must equal the document's own `sample_rate_hz` |
| `rear.normalization` | must equal `"as_supplied"` — the compiler applies coefficients as given, no re-normalization |
| `rear.added_latency_ms` | finite number `>= 0`; the FIR's own declared group delay. A composer aligning the full front/rear/tweeter graph must account for it — it is not added again |
| `rear.sha256` | hex string; must equal `coefficient_sha256(rear.coefficients)` (SHA-256 over consecutive little-endian float64 values) |

v1 note: the format and `compile_rear_stage` both accept `fir`, but ADR-0322
refuses `rear.mode == "fir"` at the runtime-adopted candidate boundary; only
`branches` compiles into the live graph today.

## Common delay and mutes

| Field | Type / range |
|---|---|
| `common_delay_ms` | finite number `>= 0`; added to all three declared cabinet outputs (front, rear branch(es), tweeter) when a negative relative rear delay must be realized. The compiler never emits a negative `Delay` |
| `rear_muted` | bool; when `true`, the compiled stage's final rear output gain stage mutes the summed rear channel regardless of branch gains |

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

**REQUIRES MEASUREMENT** (nothing below has been established on real
hardware): routing/polarity — which physical channel is the rear driver and
whether its wiring matches `inverted` — qualified by the front/rear/both
take ADR-0322 adds (one recording clock, one level, summed verify, run
through the candidate-branches baseline-shaped graph with crossover,
protection, delay, and limiter already present; only the take's own
excited target is left unmuted, an untaken rear still mutes byte-identically);
the driver's electrical-to-motion transfer, needed to turn acoustic
source-motion weights into an actual amplifier/DSP gain; forward (on-axis)
response with the calibration applied; rear suppression at multiple
listener positions and angles.
