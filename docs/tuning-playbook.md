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

Band rows name their `ladder` from `band_ladders.py`: `rear_upper`, `rear_level`, `rear_late_energy`, `rear_arrival_gap`, `bass`, `third_octave_bass`, `octave`, `room` (fixed split edges, with outer edges set by coverage and ceiling), `speaker_spec`, `snr`, or `crossover_snr`.

Numbers below the trusted floor carry `below_trusted_floor` beside their
`value`. They are not speaker evidence. Use `jasper-round-views` for a question
the packet did not answer. Never recompute a number it prints.

## What we are tuning for

The goal is the listening position. Measure the speaker layer at the mark:
a gated direct-sound read is the only way to separate speaker from room.
Judge the result at the seat.

The [Seat loop](#seat) reads the listening-position figures owned by
`jasper/audio_measurement/seat_figures.py`. Rear and room views use the same
takes; each view states its limits. `rear_preview` owns predicted trough fill.
The Rear chapter's hardware examples and pattern ratios are measured guidance,
not software ranks.

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
`residual_within_repeat_spread` and `reason`. Repeat spread is the largest
pair RMS over the fit band among this driver/set's mark takes with the mic
held still; `n_pairs` gives the pair count ([ADR-0341](adr/0341-fit-repeat-spread-comes-from-the-rounds-mark-pairs.md)).
Fewer than two mark takes give null with `repeat_basis: "no_mark_pairs"`;
this discloses missing evidence and does not refuse the fit.
`crossover_band_spread` gives `center_hz`, `sigma_db` and `max_sigma_db`, or
is null with `crossover_band_spread_reason`. Each proposed filter's
`position_variance` gives
`cv_percent`, `frequencies_hz`, `positions_deep`, `positions_total` and
`classification`. On a three-pose round, `too_few_positions` prints the
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

Read packet `alignment` / `alignment_verdict` (ADR-0319) for timing. Use `delay-landscape` for a prediction, then author candidate variants with the residual delay changes and compare real captures with `jasper-round trial`.
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
`base` is also allowed. Read `jasper-round-views candidates <round-dir>` and
its `candidates.json`: each pose has pairwise deltas per role. `window` is present
only when reading the frequency view. `level_offset_db` is median A minus median B on A's grid;
`mean_abs_db`, `max_abs_db`, `max_abs_hz`, and `rms_db` describe the remaining
shape difference over `band_hz`, with `bins` giving the count. Base keeps its
fingerprint. These numbers do not rank candidates or establish a repeat floor.
Compare them in one trial and apply the winner.
The trial's packet is the verification; no separate verify round is needed.

## Timing

`delay-landscape` needs both driver curves on one take. Its
`delay_landscape_no_banked_curves` detail lists `phases_searched`, `takes_seen`,
`roles_required`, `roles_per_take` counts, `poses`, `bundle_dir`, and `message`. Separate driver takes
cannot supply this sum.

Timing is geometry. Measure it once with confidence, save its provenance, and keep it until the user resets it. Leave `alignment` out of a document unless the user asked for a new measurement or an explicit value. Reset only for a moved or replaced driver, a changed enclosure, or a crossover change large enough to need a fresh read.

A document without an `alignment` section takes the saved timing, or the round's confident design-axis read on a fresh box. Without either, it keeps the base alignment. The composition records `saved`, `measured`, or `base`; an explicit value records `document`, and `alignment: {}` records `cleared` and removes saved timing on apply.

Read `alignment_verdict.saved` and its `verification` line: `residual_rms_db` asks for reset only when it exceeds both three times `repeat_noise_db` and the `residual_floor_db` value of 0.5 dB. Act only on `next_action`; verification never changes the saved value. Pose rows disclose `margin_db`, `residual_rms_db`, `repeat_spread_db`, `repeat_spread_us`, and `repeat_count` (paired driver takes used). A missing spread means the read cannot establish confidence. See [ADR-0319](adr/0319-timing-measured-once-with-confidence.md).

`flatness_improvement_db` compares ripple on the same metric; `refinement_delta_us` is committed minus scored seed, `epsilon_ppm` is clock drift, and `gcc_delay_us` is the bare correlation estimate. Read `parallax_us` with `driver_spacing_source`; a geometric estimate is not a measured delay.

## Room

The room layer reads the seat median through the applied speaker tune.
One seat cannot show which features persist. Seek at least three
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
Read `spread_rms_db` beside `median`: RMS of the per-bin cross-position spread from the coverage floor to the ceiling, with `n_positions`; `None` below two positions.

Room correction ends at the printed ceiling. Above it, the speaker owns the
curve. The ceiling follows the highest trusted floor from the round's gated
summed or driver takes, with its source take and any pure-room fallback
disclosed. The clamp and room/speaker ownership remain defined in ADR-0256
(`0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md`).
Full-speaker sweeps use the resolved 20 Hz–20 kHz audio band (ADR-0328); room prescriptions still start at the evidence floor, `coverage_hz[0]`.
Seats are ungated so room reflections remain in the response.

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

Shape the cut to the feature. Read each `persistence.features[]` row's
`band_hz` and `width_octaves` before you choose a Q. A room peak wider than
about half an octave is usually several modes side by side; one bell wide
enough to cover it also takes 1–3 dB from the half octave on each side. Put
two or three narrower bells (Q 5–8) across the feature's band as a second
candidate, preview both shapes, and compare the predicted residual inside the
band AND in the half octave either side; trial the best two. On jts3 two Q 7
bells at 54 and 59 Hz cut a 47–67 Hz peak as much as one Q 3.5 bell
(−4.5 against −4.9 dB) while the shoulders lost 0–1 dB instead of 1–3 dB.
A bell's skirt is electrical, so it is the same at every position: a change
beside the feature that differs between positions is noise or the room, not
the filter. Near the sweep's low edge, where the level is 10–15 dB down, a
single take can swing ±4 dB.

`jasper-crossover-prescriber judge --preview` answers limits and predicted
residual without banking a candidate; `--vary PATH[,PATH]=v1,v2 --out-dir DIR` expands a seed over a grid and previews every variant. It previews a room section, or a `rear_calibration` section against `--round <pair round>`.
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
The harmonic knee across levels is the measured headroom edge; a knee above the top rung is extrapolated and the headroom row says so.

Driver, room and bass boosts spend one shared headroom budget
(`0257-bass-extension-resumes-rebased-on-wired-capture-and-validated-in-room-below-the-ceiling.md`).

The reach at each level is the corner; the drive evidence is prescribed minus
realized; the headroom evidence is the harmonics; nothing is graded against a
fixed band. Keep `qualified_from_hz` and null fields. Harmonics give no hardware limit.

## Rear

A rear woofer near a wall does two jobs. In its cancellation band it is an
inverted, delayed copy that lowers what the box sends to the wall. Below
that band it adds in-phase bass and the wall is part of the speaker. The
`rear_calibration` section sets both (fields:
`rear-calibration-tuning-fields.md`; decisions: ADR-0318, ADR-0322,
ADR-0324, ADR-0325, ADR-0326, ADR-0327).

The goal is less reflected energy and a filled wall trough at the listening
positions, with the band above the rear stage unchanged. A front-side sweep
cannot prove a polar pattern. At the mark, late energy helps distinguish
cancellation from in-phase fill; the [Seat section](#seat) gives its seat limit.

The [Seat loop](#seat) is the loop of record. The pair take plays the front
woofer alone, the rear alone and both on one clock, with the rear stage cleared.
Read `packet["rear"][].pair.positions[*]`: `superposition_residual_db` tests
the model; `arrival_gap` gives the rear-minus-front gap, confidence and
`search_ms`; `rear_polarity` gives the measured sign. The gap uses the applied
rear document's band or `ARRIVAL_GAP_BAND_HZ`, clipped to sweep coverage;
`band_hz` and `arrival_gap_band_source` disclose `rear_document` or `default`.

Preview predicts F·H_front + R·(H_bass + H_cancel) at each measured position.
Read `positions[*]`: `trough_fill_db` is the rise at the muted curve's deepest
dip in `figures_band_hz`, named by `figures.muted.dip.hz`. If that frequency
is a band edge instead of the wall trough, read `curve.change_db` at the
trough. `bands[].change_db` includes the headroom charge against this document
with its rear muted; `front_chain_db` is the front chain's electrical level,
the only prediction above the pair's coverage. `stage.headroom_charge_db`
is the broadband attenuation cost already included in `change_db`.

At the mark, positive `late_energy.early_late_change_db` means a higher
early-to-late energy ratio. Read `arrival_shift_ms` beside it.
`gradient_residual.db` measures distance from an ideal gradient at the measured
gap: hardware cardioids read below about −6 dB at every bearing, while fills
read −2 to −4. `figures.muted` and `figures.predicted` use the packet's figures.
Vary the rear-weight Peaking gain on both branches together, cancellation
`delay_ms`, or cancellation low-pass corner; judge the seat trial yourself.
`upper_bands` at bearings uses `UPPER_BANDS_HZ`; seats carry `bands` instead.
Compare within one round: the muted trough's depth moved by up to 5 dB between
rounds at the same bearing while its frequency held.

What held on jts3, and what the preview should show before a document is
worth playing:

- Bass (30–100 Hz) at or above rear-muted plus the in-phase lift; do not
  spend it.
- 350 Hz–5 kHz within about 0.4 dB of rear-muted. The front chain is the
  whole front-woofer path, never a level lever: a rear weight above 1 is
  the same Peaking boost on both rear branches (ADR-0327); front
  attenuation measured as a 2–8 dB hole from 350 Hz up.
- 200–300 Hz within ±1 dB; leakage or over-cancellation shows here first.
- `gradient_residual.db` measures closeness to a TRUE cardioid (pattern
  ratio 1, below). At −2 to −4 with a long delay the document is fill; with
  a delay shorter than the gap it is a supercardioid, which the seat
  preferred.
- The dip ruler is relative. `dip.depth_db` against the one-octave trend
  read about 2 dB kinder than the raw curve, but differences between
  candidates held. Compare candidates; never read a depth as absolute.
- Judge the rear stage at the listening seat with the speaker at its wall.
  Cabinet 0.2 m from the wall, mic 2 m away: the fair "off" had a wall
  hole of −11 to −13 dB near 134 Hz and a roughness (RMS against the
  curve's own one-octave trend, 80–350 Hz) of 5.6–5.8 dB; rear tunes cut
  that to −3 dB and 2.9 dB. A laptop measurement found that 37 % → 51 % of
  the 100–350 Hz energy arrived within 20 ms. Mid-room, or with the mic
  0.6–0.8 m away, the same tunes showed almost nothing: a near mic is a workshop tool. Roughness
  repeated to ±0.2 dB over two hours, hole depth only to ±1.4 dB.

The pattern is a spectrum, and the delay picks the point on it. The pattern
ratio is the rear's effective lateness (the cancellation `delay_ms` plus
the group delay of that branch's own low-pass, about 225 / corner-Hz ms for
a second-order Butterworth) over the measured arrival gap: 1 is a cardioid,
about 0.58 a supercardioid, about 0.33 a hypercardioid. A delay-only sweep at
the seat (jts3, 0.95 ms gap) put the best roughness and the shallowest wall
hole at ratio 0.65; ratios 0.65 down to 0.2 sat within 0.3 dB of each other,
and only the true cardioid was clearly worse (+0.7 dB). The rear also combs
the FRONT response near 1 / (gap + delay): a shorter delay moves that dip up
(about 300 Hz → 840 Hz across the sweep) and shrinks it, while the seat's
early-to-late share falls about 1 dB. So do not pin the delay to the gap:
start near ratio 0.6, sweep 1.0 → 0.3 with every filter held, and expect a
broad optimum. A rear that is short of level in part of the band wants a
NARROW Peaking lift there (q 2–3); one wide bell spoils its neighbours.

An older room layer can over-correct once the rear stage works: a −6 dB cut
fitted at 106 Hz without a cardioid dug a −19 dB notch after the cardioid
had already flattened that peak, while the cuts on the 54–59 Hz room mode
still earned their place. Refit room after the rear stage changes.

First tune, from the pair take: give the bass branch a Linkwitz-Riley
low-pass and the cancellation branch a Linkwitz-Riley high-pass at ONE
shared corner near 80–100 Hz; complementary slopes leave no hole at the
hand-over. Put the cancellation low-pass below c / (4·D), where D is the
measured arrival gap times the speed of sound. Invert the cancellation
branch and set its delay from the pattern ratio above. Start its gain at 0 dB; if the
rear reads louder than the front at the low end of the band, shape it with
a low shelf rather than a flat cut. A delay-and-invert pair loses forward
level below c / (4·D); the same Peaking boost on BOTH rear branches in that
band (up to +6 dB, ADR-0326) pays it back, and the stage's realised peak is
charged to program headroom. Keep the bass branch in phase. Carry a filter
that flattens the front woofer itself on all three chains, so the rear/front
ratio stays the one you fitted.

A `rear/pair_behind` round takes one pass per pose: 1 m in front, then behind
the cabinet, halfway to the wall at woofer height. Each take repeats both
woofers on one clock. Use `--repeats 2` for two passes per pose. Its `behind`
position is a peer row in the pair block and in every preview: `change_db` in the
cancellation band is the predicted wall-ward null, and a real cardioid
drives it negative while the front positions hold their figures. Read
`bands[].superposition_residual_db` in the bands your document acts on
(the cancellation band), not only the whole-band scalar.
A low band below the first room mode can carry rumble without hurting
the model in the cancellation band.

After a preview predicts the wall-ward null, play the candidates:
`jasper-round run --program rear --poses rear/behind --candidates base,<a>,<b>,<rear-muted>`
uses two person-held poses, in front and behind the cabinet. Read the `behind`
row's `bands[].change_db` only, against rear-muted next to the preview's;
`late_energy` has no meaning there (no direct arrival behind the cabinet).
The full trial curves are in `frequency_view.json` (the `frequency` view).

To judge the cardioid by ear, flip between it and a fair "off". A rear-muted
copy alone is not fair: the rear stage also changes the bass at the mic, so
the ear judges tone and level, not the pattern. Write a second document
with `rear_muted: true` and `front.filters` (Peaking or Lowshelf, inside
the +6 dB chain cap) that put its previewed curve on tune A's from 30 to
350 Hz at the 0° position (`judge --preview`, `figures.predicted`; `--vary`
the filter gains), `compose` it, and trial both in ONE round:
`jasper-round trial <A fp> --candidates base,<A fp>,<off fp> --wait`. Keep
the pair when `low_bass` and `band_level_db` agree within about 1 dB at the
repeated bearing.

The EQ page's Cardioid On|Off switch mutes the applied tune's rear output
in place, at runtime only; Done or expiry restores normal playback (ADR-0329).
When a match is available, it trims the louder side using the newest banked
front/rear pair round.
The match is broadband power over 40 Hz–16 kHz, not bass tone.
The switch compares only the applied tune with its rear off, not two banked tunes.

The stack plays as composed: room and bass stay in, the same in every
candidate. After a rear change is adopted, check the room and bass
responses and refit them if needed.

## Seat

This is the hand loop of record for a cardioid box. Keep the cabinet at its wall.

1. At the mark, run `jasper-round run --program speaker --poses speaker/mark --wait`.
   Fit, trial and apply the speaker there, then bank the model:
   `jasper-round run --program rear --poses rear/pair_mark --wait`.
2. Write the rear seed and preview variants from that pair round:
   `jasper-crossover-prescriber judge --preview <seed-doc> --round <pair-round> --vary '<path>=<value>,<value>' --out-dir <variants-dir>`.
   Compose the seed, selected variants and a copy with `rear_muted: true`:
   `jasper-crossover-prescriber compose <doc> --base saved --round <pair-round>`.
3. Compare them at the seats:
   `jasper-round trial <seed-fp> --candidates base,<seed>,<v1>,<v2>,<muted> --wait`.
   These placeholders are composed fingerprints. Read the figures below and choose.
4. Join the chosen candidate to `packet["sets"]` by `candidate_id`, then to
   `packet["room"]` by `set_id`; it holds a room document per candidate set.
   Write the room fit from that set and keep the chosen rear stage as its base:
   `jasper-crossover-prescriber compose <room-doc> --base <chosen-fp> --round <seat-round> --set <chosen-set-id>`.
5. Measure the composed document: `jasper-round trial <document-fp> --wait`.
   If its packet supports adoption, run `jasper-round apply <document-fp>`.

Placement budget: **1 + 3 + 3** — the shared mark, the comparison seats, then
the composed-document seats. Counts come from `measurement_plans.json`'s
`layouts.speaker_mark` and `layouts.seat_express`, loaded by `measurement_programs._PROGRAMS`.
Each candidate plays at each seat before the mic moves: candidates per seat,
**candidates × seats** sweeps per trial; repeats add sweeps, not placements.
The hand rear trial is `rear/seat`, with `co_purposes: ["room"]`; the banker
runs rear and room views on the same takes. The arm uses `rear_express`.

Read `packet["rear"][].candidates[]`, then each candidate's `positions` seat
rows, then `across_positions`: per-figure median, worst value and
`worst_regression` against base. Keep missing-position reasons. Software never ranks.

- Band levels (`band_levels`, reported as `bands`, ladder `rear_level`) are against
  rear-muted over `band_ladders.LEVEL_BANDS_HZ`. Read level beside every shape.
- `dip` is the front-wall hole only when front-wall geometry is declared:
  the band's `source` is `declared_geometry`, printed as `comparison.band_source`.
  Otherwise read `geometry_reason`: `geometry_undeclared`,
  `front_baffle_geometry_undeclared` or `walls_undeclared`; a dip in the fallback
  `section_band` or `coverage` is not proof of a wall hole.
- `ripple_db` is mean-removed RMS against the rear-muted reference trend;
  it also charges an intended broad re-tilt. `own_trend_ripple_db` measures
  roughness against the candidate's own trend. Smoothing and trend use
  `seat_figures.FIGURE_FRACTION` and `REFERENCE_FRACTION`.
- `late_energy` at a seat describes modal decay, not "early arriving sound".
  Read `early_late_change_db`, `band_energy_change_db` and `arrival_shift_ms`;
  parameters are `band_ladders.LATE_ENERGY_BAND_HZ` and `seat_figures`'
  `EARLY_WINDOW_MS`, `LATE_WINDOW_MS`, `CENTROID_WINDOW_MS`.
- `comparison.repeat_spread` is variation for the same candidate, position
  and level. Seat trials disclose `too_few_repeats`; cross-seat spread cannot replace it.
- Room `spread_rms_db`, beside `median`, is RMS of per-bin cross-seat standard
  deviations from coverage floor to ceiling. Read `median.n_positions` with it.
  Three seats make this estimate noisy; seven or eleven use `run --layout seat_cube`
  or `run --layout seat_cloud` (counts: `measurement_plans.json`'s named layouts).

`room-grade` across this trial's candidate sets is not a candidate comparison:
rear weight also changes band level. For a plain box, skip the rear pair and
rear variants; use `jasper-round run --program room --poses room/seat --wait`,
then compose the room fit with `--set`, trial it and apply the chosen document.

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
The rear pattern-ratio, seat and room-layer figures are from the jts3 wall
rounds of 2026-09-20 (issues #5405, #5438, #5439).

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

Rear
| Name | Value | Unit | Constant or function field |
|---|---|---|---|
| document_section | "rear_calibration" | field | contract.rear.document_section |
| case | "electrical_dsp" | field | contract.rear.case |
| mode | "branches" | field | contract.rear.mode |
| freq_hz_upper_bound_rule | "every filter's freq must stay strictly below the document's own sample_rate_hz / 2 (Nyquist); freq is otherwise required to be > 0" | rule | contract.rear.bounds.freq_hz_upper_bound_rule |
| max_filters_per_chain | 16 | count | contract.rear.bounds.max_filters_per_chain |
| chain_gain_db | [-150.0,0.0] | dB | contract.rear.bounds.chain_gain_db |
| chain_gain_rule | "front, rear.bass and rear.cancellation gain_db is an attenuation between -150 and 0 dB: a rear weight above 1 is the same filter boost on both rear branches (ADR-0327), never front attenuation" | rule | contract.rear.bounds.chain_gain_rule |
| resonant_Q_max | 1.0 | Q | contract.rear.bounds.resonant_q_max |
| allpass_Q_max | 10.0 | Q | contract.rear.bounds.allpass_q_max |
| combo_order_max | 8 | count | contract.rear.bounds.combo_order_max |
| biquad_kinds | ["Allpass","Highpass","Highshelf","Lowpass","Lowshelf","Peaking"] | type | contract.rear.bounds.biquad_kinds |
| combo_kinds | ["ButterworthHighpass","ButterworthLowpass","LinkwitzRileyHighpass","LinkwitzRileyLowpass"] | type | contract.rear.bounds.combo_kinds |
| gain_kinds | ["Highshelf","Lowshelf","Peaking"] | type | contract.rear.bounds.gain_kinds |
| gain_rule | "Peaking, Lowshelf and Highshelf gain must not exceed +6 dB. A boost uses shared program headroom (ceiling 40 dB), applied as broadband attenuation pre-split to every driver, including the tweeter (ADR-0327)." | rule | contract.rear.bounds.gain_rule |
| emitted_delay_rule | "common_delay_ms + front.delay_ms + a rear branch's own delay_ms must sum to >= 0; add common delay to realize a negative relative rear delay" | rule | contract.rear.bounds.emitted_delay_rule |
| branch_delay_is_not_acoustic_delay | "a branch's raw delay_ms is not its acoustic delay: the branch's own filters add delay" | rule | contract.rear.bounds.branch_delay_is_not_acoustic_delay |
| stage_kinds | ["boundary_correction","crossover","driver_correction","protection"] | type | contract.rear.bounds.stage_kinds |
| boundary_correction_rule | "included_stages.<side> must not list boundary_correction while boundary.<side> carries filters" | rule | contract.rear.bounds.boundary_correction_rule |
| comparison_scope | "change ONE family: rear gain, rear relative delay, or one band edge; copy all other incumbent fields verbatim, including the front chain and filter structure" | rule | contract.rear.bounds.comparison_scope |
| rear_muted_reference | "the same section with rear_muted: true is the rear-muted reference" | rule | contract.rear.bounds.rear_muted_reference |
| inheritance_rule | "an absent rear_calibration key inherits the base's section; null clears the stage and the rear output is then muted" | rule | contract.rear.bounds.inheritance_rule |
```
<!-- BOUNDS_END -->
