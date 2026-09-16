# Tuning playbook

## How to read any round

Read this first. The [runbook](tuning-operator-runbook.md) is the command
reference. The [methodology](tuning-methodology.md) explains the science.
The [doctrine](measurement-loop-doctrine.md) defines roles and physical limits.

Software and a person run the experiment. The LLM reads the completed packet
once, asks analysis views for missing answers, and writes one prescription.
It does not direct an active measurement.

`packet.json` holds the numbers. `index.md` names them and the commands.
`frequency.png` shows the response. Read `result` and `reason`, then `applied`
identity and `layers`, then the program evidence. Speaker evidence is in
`fits`; room and bass evidence is in `packet["room"]` and `packet["bass"]`,
one entry per set. Artifact paths remain as fallbacks for failed views. A
joined bass ladder adds `bass_table`. Read `alignment` per pair. After the
timing block, read each fit's `verdict` and `crossover_band_spread`,
each filter's `position_variance`, and the per-pose null ceiling in
`verdicts`. Then read role gate lines and retake `fault` values. A missing
section is missing evidence.

Numbers below the trusted floor carry `below_trusted_floor` beside their
`value`. They are not speaker evidence. Use `jasper-round-views` for a question
the packet did not answer. Never recompute a number it prints.

## Speaker

The emitter absorbs the largest branch peak plus its margin before the
branches split. Boost spends program headroom (maximum SPL); it cannot raise a
branch above the fader. `bounds.boost_headroom` discloses that cost and the
remaining budget. The door refuses total program absorption above 40 dB
(ADR-0219). Measurement excitation caps do not bound playback; session volume
and measurement SPL headroom are disclosures here. The measurement SPL stop
stays in force. The fitter uses remaining program headroom and the owner's
`fit_budget.max_gain_db`; the mic tier limits where it has evidence.

A fit is already proposed. Each `fits` entry identifies its role and pose.
Read `reason_summary` before `filters`, `residual_rms_db`, `residual_max_db`
and `boost_evidence`. `envelope_fitted` means the bin was fitted.
`position_spread_db` reports standard error across positions in dB at the
`reason_summary` bands; null means fewer than two readable positions.
Weigh this spread yourself: a boost into a dip at one position can harm the
other positions; a droop at every position is a correction target.
`class_prior_hz` gives the declared class's `full_to_hz` and `taper_zero_hz`
as guidance. Neither field limits the fit. `envelope_limited_by_mic_tier`
names the instrument limit; `envelope_limited_by_spatial_exclusion` names
an identified null.

Read three verdicts. A fit's `verdict` gives `repeat_spread_db`,
`residual_within_repeat_spread` and `reason`. A residual at or under the repeat
spread means further correction is not a result. A null `repeat_spread_db`
with `reason=repeat_floor_not_banked` means bank a repeat floor first.
`crossover_band_spread` gives `center_hz`, `sigma_db` and `max_sigma_db`, or
is null with `crossover_band_spread_reason`. Each proposed filter's
`position_variance` gives
`cv_percent`, `frequencies_hz`, `positions_deep`, `positions_total` and
`classification`. On a three-pose round, `insufficient_positions` prints the
CV but cannot separate the 3% and 8% cues; six deep poses can. Each `verdicts`
pose gives `branch_gap_db`, `louder_role`, `null_ceiling_db`, `band_hz` and
`capture_graph`. Its ceiling is the deepest reverse null the branch gap permits
under that graph, so a shallower measured null is not a timing error.

`crossover_band_spread[].sigma_db` near 1 dB or less over the crossover octave
is a working cue, not a universal pass threshold (basis:
`docs/research/2026-07-29-attribution/02-dissertation-measure-diagnose-prescribe.md`).
Repeat spread and pose spread answer different questions. No fit filter should
rest on its `budget.max_gain_db` rail. Measure the composed graph, then stop
when it answers the question.

Settle structure before response. Decide topology and trims in the same document. Re-derive filters fitted to another alignment (`0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md`; `docs/research/2026-08-31-tuning-methodology-deep-research/00-adjudications.md`).

If SNR is low, inspect `snr`, then use `delay-landscape`. If a measured answer would change the prescription, a person starts `jasper-null`; `delay-confirm` compares the result. It plays sound. A reverse null near −20 dB on delay alone is a delay answer. One that will not pass −10 dB at any delay points to level or slope (`02-dissertation-measure-diagnose-prescribe.md`, Stage 3).
A 10 dB branch gap limits cancellation to about 3.3 dB relative to the louder branch: `−20·log10(1 − 10^(−Δ/20))` (derivation in `tuning-methodology.md`). That reference differs from shoulder-based null depth.

Cut peaks; leave dips. A broad, low-Q peak can be audible near a quarter dB;
a high-Q peak can need about 10 dB. These depend on the signal
(`docs/research/2026-08-31-tuning-methodology-deep-research/01-correction-granularity-and-audibility.md`).
A boost must be wide enough for `DRIVER_MAX_BOOST_Q`; a cut may be narrow.
A feature that moves with pose or gate window suggests interference.
Feature-frequency CV, the spread divided by its mean, below 3% suggests
source-fixed; above 8% suggests position-variant; between is uncertain
(`docs/research/2026-07-29-attribution/07-reanalysis-position-variance.md`).
`classify-features` and `sweep` use saved evidence.

Read `gate_window_ms`, `validity_floor_hz`, `trusted_floor_hz` and `floor_source`
per role. Nothing below the trusted floor, 2.5 divided by gate length in
seconds, supports a speaker claim
(`docs/research/2026-08-31-tuning-methodology-deep-research/03-gating-windowing-and-low-frequency-truth.md`).

### Document

`jasper-round run --program speaker --poses baseline/express` collects driver
fits, timing and room evidence in one round; `baseline/full` adds poses.
Write one document with every section the evidence supports.
`jasper-crossover-prescriber contract --round <dir> --section speaker` prints
the schema. Normally omit `alignment`: saved timing carries forward. See Timing
below for when to include it. A refusal names the crossed bound; correct that field.

Trial the whole document with two or three candidates: the fitted totals and
one or two variants. Use `jasper-round trial <FP> --candidates <FP>,<variant-FP>`;
`base` is also allowed. Compare them in one trial and apply the winner.
The trial's packet is the verification; no separate verify round is needed.

## Timing

Timing is geometry. Measure it once with confidence, save its provenance, and keep it until the user resets it. Leave `alignment` out of a document unless the user asked for a new measurement or an explicit value. Reset only for a moved or replaced driver, a changed enclosure, or a crossover change large enough to need a fresh read.

A document without an `alignment` section takes the saved timing, or the round's confident design-axis read on a fresh box. Without either, it keeps the base alignment. The composition records `saved`, `measured`, or `base`; an explicit value records `document`, and `alignment: {}` records `cleared` and removes saved timing on apply.

Read `alignment_verdict.saved` and its `verification` line: `residual_rms_db` against `repeat_noise_db`. Act only on `next_action`; verification never changes the saved value. Pose rows disclose `margin_db`, `residual_rms_db`, `repeat_spread_db`, `repeat_spread_us`, and `repeat_count` (paired driver takes used). A missing spread means the read cannot establish confidence. See [ADR-0319](adr/0319-timing-measured-once-with-confidence.md).

`flatness_improvement_db` compares ripple on the same metric; `refinement_delta_us` is committed minus scored seed, `epsilon_ppm` is clock drift, and `gcc_delay_us` is the bare correlation estimate. Read `parallax_us` with `driver_spacing_source`; a geometric estimate is not a measured delay.

## Room

The room layer reads the median across seats, through the applied speaker
tune. One seat cannot show which features persist. Seek at least three
positions before a room claim; three is the boost-admission minimum, not a
rule that makes smaller clouds unreadable. A boost needs presence at 70% of
positions (`ROOM_BOOST_MIN_POSITIONS`, `ROOM_BOOST_PRESENCE_MIN_FRACTION` in
`jasper/audio_measurement/room_limits.py`; design basis in
`docs/room-correction-regime-plan.md`, D5). The views still answer with the
available count and spread. State what that evidence supports.

Read each `packet["room"]` entry's `set_id`, `median`, `ceiling.hz` and
`ceiling.provenance`, then `persistence`, `limits.cut_floor_db`,
`limits.boost_cap_db` and `admit_boost`. Read `incumbent` and
`incumbent_reason` before comparison. Top-level `limits` carries the contract.
Do not infer an incumbent from a file name when the entry says the set is
ambiguous.

Room correction ends at the printed ceiling. Above it, the speaker owns the
curve. The ceiling follows the applied tune's trusted floor, with its source
and any fallback disclosed
(`0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md`).
Seats are ungated by design: room reflections are part of the response being
measured. A short speaker gate would remove that evidence.

The cut floor varies by frequency with the cross-position sigma. A large
spread supports less correction. A boost is admitted only where seats agree
on a dip with enough width and bounded depth. The current room boost cap is
6 dB; a dip deeper than 10 dB is not filled (`ROOM_MAX_FILTER_BOOST_DB`,
`ROOM_BOOST_MAX_DIP_DB`, `room_limits.py`; regime-plan D5). These are current
implementation limits, not universal audibility thresholds. Read the per-bin
cap before spending the full allowance.

Taper the limit over one-third octave below the ceiling, as ADR-0256 directs
and `ROOM_TAPER_OCTAVES` implements. Do not end correction at a sharp edge.
Check the whole composed response, since overlapping filters add.

`jasper-crossover-prescriber judge --preview` answers limits and predicted
residual without banking a candidate. It previews the room section only.
Good means median residual under the seat spread, no boost into a dip that
changes with position, and a response that respects the ceiling. A preview
can settle which document to measure; it cannot prove the sound of an
unplayed graph.

## Bass

Bass is level-dependent. A result at one level establishes only that level.
ADR-0304 records why the level axis matters
(`0304-the-bass-level-axis-is-fixed-level-windows.md`). Its in-run scheduling
decision was superseded by `0311-a-run-plays-at-one-session-level.md`.
The current ladder joins runs at distinct fixed levels. Keep the same pose
and compatible capture conditions across them. Let the engine run the ladder;
interpret its completed evidence together.

Read each `packet["bass"]` entry's `set_id` and `takes`. Each take has
`diagnostics`, `bands` with `estimated_snr_db` and
`fundamental_qualified`, plus the full `fundamental_qualified` mask. Unknown
harmonic coverage is not low distortion. Read `diagnostics` before comparing
curves. Requested gain alone does not establish actual DSP drive.

The shelf has a fixed corner near 70 Hz and slope 12.
Choose `low_boost_db` to match the measured roll-off: compare the base corner
and per-band `realized_boost_db` in `bass_table.tables[].levels[]`; overshoot
above the corner beyond repeat spread means too much boost for the box.
Keep `delta_highpass_hz` near 25 to 30 Hz for sub-20 Hz protection, never
at the extension target: a high corner tilts boost up and discards extension.
Set `detector_lowpass_hz` at the top of the boosted band.
Unqualified boosted bands are disclosed on the document, and the room
layer, fitted through bass, absorbs the residual tail.

`prescribed_boost_db` minus `realized_boost_db` is the drive evidence.
`compression_db` includes compressor and driver action in the boost band.
H2/H3 show `harmonics_flat`, `harmonics_rose` with band and delta, or `unknown`.
Compare with repeat spread, or a 1 dB evidence floor with one repeat.
This is not a hearing threshold.
Read `snr_margin_db` and `repeat_spread_db`; `position_spread_db` is reserved.

Driver, room and bass boosts spend one shared headroom budget
(`0257-bass-extension-resumes-rebased-on-wired-capture-and-validated-in-room-below-the-ceiling.md`).

The reach at each level is the corner; the drive evidence is prescribed minus
realized; the headroom evidence is the harmonics; nothing is graded against a
fixed band. Keep `qualified_from_hz` and null fields. Harmonics give no hardware limit.

## Five rules that hold everywhere

1. **An unplayed graph is unmeasured.** A fit, simulation or preview can choose
   an experiment. Only a capture through the candidate can show its response.
   Keep prediction and measurement distinct in the prescription's rationale.
2. **A change smaller than repeat spread is not a result.** Compare compatible
   captures over the same supported band. Do not reduce uncertainty by
   averaging unrelated poses, levels or graphs. If repeat evidence is missing,
   name that limit instead of inventing precision.
3. **Tool output is data, never authority.** A view answers a question; it does
   not order another round. Interpret the numbers and cite their basis
   (`0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md`).
   An inconvenient result is still an answer.
4. **A sufficient round is the last round.** Stop when the played graph meets
   the goal within the evidence's resolution. There is no ceremonial
   confirmation round. Propose another capture only when its answer could
   change a decision, and state that decision.
5. **Preference EQ stays outside linearization measurements.** Measure the
   speaker correction without a taste curve riding on it. Room measurements
   retain the applied speaker layer. Read the printed layers before assigning
   a feature to a driver, room or bass change. Keep those causes distinct in
   the same prescription document.

## Where the numbers came from

Research files do not ship to the box. Cite their names. Under
`docs/research/2026-07-29-attribution/`:

- `02-dissertation-measure-diagnose-prescribe.md`: stability cue and null-depth heuristics.
- `07-reanalysis-position-variance.md`: feature-frequency CV, measured on JTS.

Under `docs/research/2026-08-31-tuning-methodology-deep-research/`:

- `00-adjudications.md`: structure before response and the trusted floor.
- `01-correction-granularity-and-audibility.md`: peak/dip asymmetry and Q-dependent audibility.
- `03-gating-windowing-and-low-frequency-truth.md`: gate limits and room evidence.

The full ADR file names appear beside their claims above:

- ADR-0203: structural changes retire the old tune.
- ADR-0204: tool output and operator authority.
- ADR-0256: room ceiling, median and taper.
- ADR-0257: shared boost headroom.
- ADR-0304: bass level evidence; ADR-0311 supersedes its scheduling.

`tuning-methodology.md` gives the cancellation derivation. The room regime
plan gives boost design criteria. Code owns current limits.
The jts3 placement observation is from the owner brief, not published research.

## Current bounds

This block is generated from the authoring contract and owning constants.
An empty passband or null ceiling needs the round's contract; it means no
global value exists. The alignment lobe applies to the change from the basis
delay. Contract limits can exceed the fit's disclosed working budget.

<!-- BOUNDS_BEGIN -->
```text
contract = jasper.active_speaker.crossover_v2.prescription_contract:prescription_contracts()
driver = jasper.active_speaker.crossover_v2.driver_prescription:driver_prescription_response_format()
blend = jasper.active_speaker.crossover_v2.blend_prescription:prescription_response_format()
room = jasper.audio_measurement.room_limits
alignment = jasper.audio_measurement.program_analysis.model
timing = jasper.audio_measurement.program_analysis.response
quality = jasper.audio_measurement.quality_model
safety = jasper.active_speaker.profile
gating = jasper.audio_measurement.gating

Speaker
| Name | Value | Unit | Constant or function field |
|---|---|---|---|
| driver.passband | {} | Hz | contract.speaker.driver.bounds.passbands_hz |
| driver.chain_scope | "for every role you name, prescribe the WHOLE per-driver correction that branch should carry, not a delta. A role you do not name is not changed. An empty filters list clears every role's chain; named trim pins still apply and other trims stay at the base" | rule | contract.speaker.driver.bounds.chain_scope |
| driver.trim_pin_scope | "{<role>: <dB, between -60.0 and 0>} -- pin that driver's LEVEL instead of letting this round re-solve it. Only for a role whose chain you replace or clear; filters: [] clears every role's chain and admits trim pins. Use it when the chain you are prescribing was shaped against a level this round will not re-derive: the trim is re-solved every round from a level-match datum, so a chain carried over from another round otherwise rides a level it was not shaped against. A trim you name is CARRIED, never re-solved, and it is never a measurement of this round" | rule | contract.speaker.driver.bounds.trim_pin_scope |
| driver.cut_Q | [0.0001,1000000.0] | Q | contract.speaker.driver.bounds.q_range_cut |
| driver.boost_Q_max | 8.0 | Q | contract.speaker.driver.bounds.q_max_boost |
| driver.boost_headroom_rule | "Program headroom spent must not exceed 40 dB" | dB | contract.speaker.driver.bounds.boost_headroom_rule |
| driver.cut_rule | "a cut (gain <= 0) carries no depth ceiling and no composed ceiling: it only removes level and cannot clip at any depth, and the round's own measured verify with auto-restore is the net. Its Q must sit in [0.0001, 1e+06] (ADR-0207) -- not a policy ceiling but the range this system's evaluator and emitter realize faithfully. What a cut spends is one of max_filters_per_role's slots" | rule | driver.bounds.cuts_are_free |
| driver.filters_per_role | 8 | count | contract.speaker.driver.bounds.max_filters_per_role |
| driver.shelf_rule | "leading a role's chain, or -- a Highshelf only -- ending it after a Lowshelf lead. Anywhere else the emitter cannot name the filter and the document is refused. Peaking sits anywhere" | rule | contract.speaker.driver.bounds.shelf_rule |
| driver.shelf_Q | 0.7071067811865475 | Q | contract.speaker.driver.bounds.shelf_q |
| driver.subaudible_below | 0.5 | dB | contract.speaker.driver.disclosures.subaudible_below_db |
| driver.declared_tilt | {"type":"number","minimum":-3.0,"maximum":3.0} | dB/octave | contract.speaker.driver.schema.properties.declared_tilt_db_per_octave |
| blend.passband | null | Hz | contract.speaker.blend.bounds.band_hz |
| blend.cut_Q | [0.0001,1000000.0] | Q | contract.speaker.blend.bounds.q_range_cut |
| blend.boost_Q_max | 2.0 | Q | contract.speaker.blend.bounds.q_max_boost |
| blend.filter_boost_max | 3.0 | dB | contract.speaker.blend.bounds.max_filter_boost_db |
| blend.composed_boost_max | 4.0 | dB | contract.speaker.blend.bounds.max_composed_boost_db |
| blend.cut_rule | "a cut (gain <= 0) carries no depth ceiling and no composed ceiling: any depth the arithmetic can evaluate is admitted, and the round's own measured verify with auto-restore is the net. Its Q must sit in [0.0001, 1e+06] (ADR-0207) -- not a policy ceiling but the range this system's evaluator and emitter realize faithfully. A boost (gain > 0) is capped at Q 2 -- its composed SPL spend is read on a sampled grid, and no fixed grid can bound an arbitrarily narrow boost's between-bin peak" | rule | blend.bounds.cuts_are_free |
| blend.filters | 2 | count | contract.speaker.blend.bounds.max_filters |
| blend.filter_type | "Peaking" | type | contract.speaker.blend.schema.properties.filters.items.properties.biquad_type.const |
| blend.boost_route | {"available":false,"reason":"boost_route_unavailable","detail":"The route refuses every boost today."} | rule | contract.speaker.blend.bounds.boost_route |
| alignment.lobe | half_period_us(fc_hz) | us | timing.half_period_us |
| alignment.lobe_applies_to | "abs(delay_us - basis_delay_us)" | us | contract.speaker.alignment.bounds.lobe_applies_to |
| alignment.SNR_floor | 35.0 | dB | quality.DRIVER.alignment_snr_ok_db |
| alignment.SPL_raise_margin | 3.0 | dB | safety.SPL_RAISE_MARGIN_DB |
| gate.trusted_floor_multiplier | 2.5 | cycles | gating.TRUSTED_FLOOR_MULTIPLIER |

Room
| Name | Value | Unit | Constant or function field |
|---|---|---|---|
| passband | null | Hz | contract.room.bounds.band_hz |
| floor | 20.0 | Hz | room.ROOM_FLOOR_HZ |
| ceiling | null | Hz | contract.room.bounds.ceiling_hz |
| cut_Q | [1.0,8.0] | Q | contract.room.bounds.q_range |
| filter_boost_max | 6.0 | dB | contract.room.bounds.max_filter_boost_db |
| total_boost_max | 6.0 | dB | contract.room.bounds.max_total_boost_db |
| cut_floor_before_spread_and_taper | -10.0 | dB | room.ROOM_MAX_CUT_DB |
| spread_tolerance | 6.0 | dB | room.TOLERABLE_STD_DB |
| filters_per_side | 8 | count | contract.room.bounds.max_filters_per_side |
| filter_type | "Peaking" | type | contract.room.schema.properties.sides.additionalProperties.items.properties.biquad_type.const |
| composed_tolerance | 0.5 | dB | contract.room.bounds.composed_tolerance_db |
| boost_dip_max | 10.0 | dB | room.ROOM_BOOST_MAX_DIP_DB |
| boost_dip_min | 3.0 | dB | room.ROOM_BOOST_MIN_DIP_DB |
| boost_positions_min | 3 | count | room.ROOM_BOOST_MIN_POSITIONS |
| boost_presence_min | 0.7 | fraction | room.ROOM_BOOST_PRESENCE_MIN_FRACTION |
| boost_depth_agreement | 3.0 | dB | room.ROOM_BOOST_DEPTH_AGREEMENT_DB |
| boost_width_min | 0.16666666666666666 | octaves | room.ROOM_BOOST_MIN_WIDTH_OCTAVES |
| taper | 0.3333333333333333 | octaves | room.ROOM_TAPER_OCTAVES |

Bass
| Name | Value | Unit | Constant or function field |
|---|---|---|---|
| low_boost | {"type":"number","exclusiveMinimum":0.0,"maximum":20.0} | dB | contract.bass.schema.properties.low_boost_db |
| reference_level | {"type":"number","minimum":-100.0,"maximum":0.0} | dB | contract.bass.schema.properties.reference_level_db |
| detector_lowpass | {"type":"number","minimum":20.0,"maximum":200.0} | Hz | contract.bass.schema.properties.detector_lowpass_hz |
| compressor_threshold | {"type":"number","minimum":-60.0,"maximum":0.0} | dBFS | contract.bass.schema.properties.compressor_threshold_dbfs |
| compressor_factor | {"type":"number","exclusiveMinimum":1.0,"maximum":20.0,"default":10.0} | ratio | contract.bass.schema.properties.compressor_factor |
| compressor_attack | {"type":"number","minimum":0.001,"maximum":0.1,"default":0.01} | s | contract.bass.schema.properties.compressor_attack_s |
| compressor_release | {"type":"number","minimum":0.01,"maximum":2.0,"default":0.25} | s | contract.bass.schema.properties.compressor_release_s |
| delta_highpass | {"type":["number","null"],"minimum":10.0,"default":null} | Hz | contract.bass.schema.properties.delta_highpass_hz |
| delta_highpass_exclusive_upper | "detector_lowpass_hz" | field | contract.bass.bounds.delta_highpass_hz_exclusive_upper_field |
| shared_headroom_layers | ["driver_linearization","room","bass_extension"] | layers | contract.bass.shared_headroom.layers |
```
<!-- BOUNDS_END -->
