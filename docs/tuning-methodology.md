# Tuning methodology — optional science reference

Start with the [runbook entry contract](tuning-operator-runbook.md#entry-contract).
Read the sections here that answer the current question. This is one useful
method, not a required order or a campaign controller. The
[doctrine](measurement-loop-doctrine.md) owns authority and layer rules; tool
help owns schemas and physical limits. Source discussion is in the
[research index](research/2026-08-31-tuning-methodology-deep-research/00-adjudications.md).

## 0. Declarations and measurement scope

Read the speaker's declared design before interpreting its response. A horn's
raw high-frequency fall can call for compensation; the same curve on a dome
can have another cause. Driver class is context, not a diagnosis by itself.

| Input | Owner or evidence | Use |
|---|---|---|
| Driver sensitivity, protection floor, required slope | `DriverSpec` and declared design | Initial level estimate and protected crossover |
| Excitation band, level, duration | `excitation_safety_plan.py` | Physical protection |
| Mic calibration and capture gain | Take calibration record | SPL and magnitude interpretation |
| Gate window | Per-take gate disclosure | Frequency range the capture can resolve |
| Rig geometry | `jasper-declare-geometry set|show` | Reflection-path estimate and its source |
| Repeat floor | Banked `repeat-floor.json` | Random measurement uncertainty |

Geometry is useful when available: speaker acoustic-centre height, mic height,
distance, and optional ceiling height. `jasper-declare-geometry --help` lists
units. Derived floors must use each capture's own distance. An absent declaration
is unknown, not zero. Centre spacing and waveguide coverage may exist only in
operator notes; label that source instead of pretending the schema measured it.

For speaker linearization, measure the base plus the deliberate candidate layer.
Exclude previous correction from the initial baseline and exclude room/preference
layers from all linearization captures. Room work later holds the accepted
speaker tune fixed and still excludes preference EQ. The played graph is the
proof; a saved household profile need not be flat.

## 1. Prove the measurement setup

Before sound, use the established protected measurement path and the selected
speaker's declared caps. `jasper-seat-level` can establish a calibrated SPL
reference. A mic sensitivity quoted at maximum capture gain is valid only with
that capture control at the matching setting. Absolute SPL is unavailable when
its calibration cannot be established.

Inspect take validity, channel mapping, timeline anchor, clipping, level, and
calibration before reading acoustic differences. An invalid capture is an error,
not a poor speaker result. A repeated coordinate can price the noise floor of a
comparison. `repeat-floor` banks that analysis for later rounds.

Keep uncertainty kinds distinct:

- **In-capture repeat spread:** random variation during one held take.
- **Pose spread:** response differences across positions; not a repeated
  estimate of one response.
- **Re-placement error:** variation after moving and returning the mic.
- **Systematic error:** calibration, gate, geometry, and model assumptions.

Do not pool these into one sigma or call repeatability absolute accuracy. Read
`thresholds.source`: a banked measurement and a code assumption are different
evidence. A small improvement inside repeat spread is uncertain; the LLM may
finish, measure a useful repeat, or try a different intervention.

## 2. Base and solo measurements

The base contains declared routing/topology, crossover, required protection,
trims, and applicable alignment. `jasper-basic-profile review` shows its durable
apply alternative; use a scoped temporary graph for measurement.

A solo capture answers a driver question. A summed capture answers how branches
combine. Store magnitude and phase when available. Do not infer a summed delay
or polarity effect by adding driver magnitudes: the complex responses must be
summed. Even identical solo magnitudes can produce a different total response.

Use named measurement programs and inspect their pose/capture cost. One pose
batch can measure baseline and several candidates before asking the human to
move. The human must deliberately start that batch. A fixture or an offline
forward model does not perform it.

## 3. Declared crossover and useful geometry

Crossover design is declared and compiled through the supported design path.
A candidate batch holds crossover and topology fixed. Do not use that contract
to sneak in a new corner with old protection limits.

For a different base design, consider declared protection first, then off-axis
response and driver spacing. Directivity is how output changes with angle.
`directivity` can compare measured coverage; missing vertical poses cannot
establish vertical behavior. Use the round's speed of sound in wavelength and
path-delay calculations. A geometry estimate is a prior to test, not acoustic
proof or a veto on a safe experiment.

## 4. Time alignment

Delay errors can look like response errors near the crossover. Summed EQ fitted
to an old alignment can compensate that error; changing alignment then changes
what those filters are correcting (see [ADR-0203](adr/0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md)).
Per-driver evidence can still be useful. Compare actual layer/phase composition
before reusing it.

1. **Inspect phase composition.** MEASURE analysis can replace emitted
   protection with the configured crossover; lateral solos can retain protection.
   Read `phase_composition`: `crossover_composed`, `protection_retained`, or
   unknown. A retained protection phase can bias a predicted alignment.
2. **Compute a hypothesis.** `delay-landscape` complex-sums banked branches over
   a bounded delay grid. `τ = Δpath/c` gives a geometric scale; at 343 m/s,
   1 mm is about 2.915 µs. Use code for the calculation and the take's own `c`.
3. **Measure if useful.** `jasper-null` plays a protected summed probe;
   `delay-confirm` compares its rows with the landscape. A proposed optimum is
   unmeasured until the corresponding summed graph has played. The returned
   command emits sound; confirm the mic placement and human start before using
   it. The small neighbour set is one economical test, not a required round.

Read branch levels before blaming a shallow reverse null on timing. With a
branch level gap Δ dB, cancellation relative to the louder branch is limited by
`−20 log10(1 − 10^(−Δ/20))`. It is about 3.3 dB for a 10 dB gap. This is a
level ceiling, with a different reference from a measured shoulder-based depth.
The graph's actual levels and repeated summed captures are stronger evidence
than datasheet sensitivity alone.

`delay_sweep.py` owns usable/robust null depth bars. When a computed optimum and
a measured sum disagree, report the difference and consider an in-phase delay
grid with a repeated coordinate. Do not claim a predicted delay was confirmed.
Nor must every resolved small change be adopted: judge its value for the goal.

The alignment door uses µs and `(D_woofer − D_tweeter)`; a positive value delays
the tweeter. Read `jasper-crossover-prescriber contract --round <dir> --section speaker`
for its alignment basis and lobe bound. A one-period shift can yield a similar null; an honest
basis and suitable summed evidence distinguish it.

## 5. Level match and candidate trims

Sensitivity-derived trims are initial estimates. They may describe the maker's
cabinet or waveguide rather than this speaker. Read whether the base trim was
measured, declared, or pinned. A pin is carried intent, not a new measurement.

Candidate trims are part of the candidate graph. Keep a reused filter chain's
level context explicit: copy the intended role trim when it is part of the
intervention, or refit against the new trim. The driver document's
`pinned_trim_db` provides this pin. Do not substitute a shared baseline trim for
different candidates and then claim their full configurations were compared.

## 6. Linearize per driver

Ask whether the feature belongs to the speaker and whether the proposed filter
can control it. `classify-features`, `gate-sweep`, `close-reference`, and
`distortion` provide distinct evidence. Run useful enrichment before freezing
the prescription packet. Unavailable classification is not a negative verdict.

| Observation | Useful interpretation or next test |
|---|---|
| Feature moves with pose or gate | Interference may dominate; compare a held-pose candidate and another relevant pose |
| Broad stable peak with modest excess group delay | A cut is a plausible experiment |
| Deep dip with excess group-delay or position dependence | Boost may spend headroom without recovering output; inspect a bounded probe or placement change |
| Predicted boost exceeds measured gain-back | Check level dependence, limiting, and the evidence frame |
| Distortion rises at a changed level | Inspect per-order validity and overlap before attributing a nonlinear cause |
| High-Q correction is smaller than repeat spread | Widen/drop it, collect a useful repeat, or end with uncertainty disclosed |

These are hypotheses, not automatic verdicts. Unknown benefit and an ideal
flatness miss cannot veto a protected reversible test. Driver bands, composed
boost/headroom, and emitter limits still apply. A summed blend deficit cannot
identify a driver to boost; the supported driver door owns that experiment.

### 6a. Gate and harmonic traps

A time gate limits frequency resolution. The trusted floor is approximately
`2.5/T` Hz for gate duration `T` in seconds; `gating.py` owns the exact rules.
The reflection-path floor and the gate's validity floor answer different
questions. Read `entanglement_floor_source` and its uncertainty before claiming
speaker-only evidence. More repeats do not remove a systematic room contribution.

A gate sweep varies the analysis window over existing raw captures. It costs no
new sound, but it cannot recover missing WAVs or make an invalid capture valid.
`close-reference` compares a suitable local reference with the pose response;
inspect reference compatibility before assigning a physical mechanism.

ESS harmonic extraction separates orders in time. The window may overlap a
neighboring order or extend past available samples. Read per-order status,
window, and contamination disclosure; unavailable orders are not zero distortion.
A single level does not establish level independence, and a short sweep does not
measure sustained thermal compression.

## 7. Compare measured results

Read realized effect, target error, and repeatability separately. If realization
matches but the target still fails, re-commanding the same correction harder does
not solve the target problem.

`level_deviation_db` is the band mean against the report's reference;
`max_ripple_db` is deviation from the band's own level. A trim changes level;
filters address shape. Read `graded_lo_hz` and `graded_hi_hz`, not nominal band
edges. `evaluable=false` means not graded, never passed.

Compare on-axis and relevant off-axis sums for the stated goal. `co-metrics`
provides another view of broad smoothness; it is advisory, not a compulsory
single score. Label predictions, measured changes, and interpretation separately.
If several parts changed together, their individual causes are unresolved.

## 8. Voicing and listening

Subjective bass, warmth, or tilt belongs in preference EQ after speaker
linearization. Keep it out of every linearization baseline and candidate graph.
Room correction remains a separate higher layer. Do not fit speaker sound power
flat merely because the on-axis target is flat; narrowing directivity changes
those responses differently.

The human judges the final sound. `jasper-audition` can compare supported layers
with temporary playback and restoration. Verify which layers its current help
and receipt select before attributing a listening difference.

## 9. Below the gate floor

A gated capture cannot establish response below its validity floor. Disclose the
unverified band. Nearfield or another supported measurement regime may answer
that question, with its own scope and limits. Repeating one in-room seat cannot
turn room modes into a speaker-only response; the room itself is measured on
the seat cube (§11). The toolbox has no electrical impedance instrument;
external data must retain its source.

## 10. Decide whether to continue

A sufficient first or current round can end the campaign. Otherwise state the
remaining question, choose a useful next experiment, and reuse the same tools.
There is no required three-round sequence, plateau termination, or last round
whose only purpose is to be final.

A restored losing candidate stays banked. Combine useful role/filter parts into
a child with source references and its own identity. Parent observations support
the hypothesis; the child remains unmeasured until tested as that combination.
When suitable evidence already measures the chosen candidate, adopt it explicitly
by fingerprint and state coverage limits. Improved acoustics require real
measurements; listening quality also needs the human's judgment.

The [instruction comparison](../tests/fixtures/tuning_instruction_comparison.json)
records a controlled planning trial, including its frozen inputs and limits.
For future executed evaluations, add actual mic placements/moves, Start actions,
recovery interventions, elapsed seconds, and input/output tokens to the result
record, with links to measurements and final graph/volume readback. Separate
planned effort from observed effort; record unavailable counts as unknown.
After a model upgrade, compare the same cases with one old workaround removed.
Judge evidence use, recovery, and resulting state; no tool sequence is required.

## 11. Room

Room correction is a layer of this toolbox, not a separate product
([ADR-0259](adr/0259-room-correction-and-bass-extension-are-layers-of-the-one-tuning-toolbox.md)).
The room is measured where it is heard: a cube around the listener's head, the
head centre and the six face centres 0.30 m out (`seat/cube`; `seat/express` is
the head, right and forward). Each pose is one summed sweep through the applied
tune, analyzed ungated so the reflections stay in. A seat take records its
kind, its offset from the head and its window
([ADR-0260](adr/0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md));
it is not a bearing at the mark, and no gated reader treats it as one. The
close reference (`close/spot`, about 0.3 m on the design axis) stays the
room-suppressed diagnostic of the speaker's own share.

The seam is the ceiling: the applied candidate's trusted floor (2.5/T of the
gate it earned), clamped to the room boundary's bounds, with the shipped
default disclosed when no applied floor is readable
([ADR-0256](adr/0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md)).
Above it the speaker stage has authority and the room layer does nothing.
Below it speaker, room and bass are read together on the cube.

`jasper-round-views room --set <set-id>` writes one `room.json` document:

- `ceiling`: where the room layer stops, and which source set it.
- `median`: per frequency the median across positions (the trend), the
  spread (population sigma, the confidence) and each position's deviation,
  20 Hz to the ceiling.
- `persistence`: the peaks and dips each position shows against its own
  local level, clustered across the cube with the fraction of positions that
  carry each at an agreeing depth. A feature most positions share is the
  room's; one position's is that seat's.
- `limits`, `incumbent`, and `boundary`: correction bounds, the applied room
  set, and the boundary prior when room geometry is declared.

Deliberately not done here: nothing above the ceiling is graded or corrected
from the cube; the median is a trend, never a per-position target; a dip is not
boosted on this evidence alone, the room candidate kind decides; no room volume
or Schroeder estimate is derived.

## 12. Bass

The [Bass runbook](tuning-operator-runbook.md#bass) is the operator entry point;
[ADR-0304](adr/0304-the-bass-level-axis-is-fixed-level-windows.md) owns its level axis.
