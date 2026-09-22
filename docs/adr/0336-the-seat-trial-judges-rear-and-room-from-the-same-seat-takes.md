# ADR-0336: The seat trial judges rear and room from the same seat takes

- **Date:** 2026-09-22
- **Status:** Accepted
- **Extends:** [ADR-0325](0325-rear-program-compares-measured-symptoms-and-previews-by-superposition.md)
  and [ADR-0278](0278-measurement-purpose-is-independent-of-position.md)

## Context

The goal is sound at the listening position. The rear program had validated
superposition at the mark, but no shared figures described the seat goals.
A prediction at the mark cannot establish the response at the listener.
Separate rear and room rounds would spend more placements and compare takes
made under different conditions.

## Decision

The seat trial is a trial of the rear program at listening seats. Its
`rear/seat` row carries `co_purposes: ["room"]`, so the banker runs both views
on the same takes. Each candidate keeps its set identity: `packet["rear"]`
compares candidates, `packet["room"]` has a document per set, and
`packet["sets"]` joins each set to its candidate.

`seat_figures.py` owns position figures and their reductions. Figures disclose
evidence with named parameters, source bands, counts and missing-data reasons;
software never ranks candidates. Shape needs a level figure beside it.
Reference-trend ripple includes a broad re-tilt; own-trend ripple describes
roughness. At a seat, late energy describes modal decay, not a direct-sound
share. A declared front-wall band supports a wall-hole reading; an undeclared
band does not identify a dip's cause. The [playbook](../tuning-playbook.md#seat)
owns the commands and field-reading guide.

The `rear/pair_mark` pair take stays the superposition model. A seat trial
tests the chosen predictions; it does not turn seat curves into a new pair
model or prove a polar pattern.

## Consequences

- Hand rear trials run at `seat_express`; the arm keeps `rear_express`.
  `rear/seat` and `rear/pair_mark` are distinct banked identities.
- The room fit follows the chosen candidate's set through `--set`, and its
  composition uses that candidate as the base. Trial the composed document
  before apply. Banking either view does not adopt a tune.
- The three-seat `spread_rms_db` estimate is disclosed as noisy, with
  `median.n_positions`; `seat_cube` and `seat_cloud` provide broader samples.
  Seat trials do not supply a repeat floor. Cross-seat spread is not repeat
  spread, and `room-grade` across candidate sets is not a candidate comparison.
- Plain boxes use `room/seat` without a rear model or rear variants.

## Overrides considered

- **Retain an impulse in the bank:** rejected for this change. The banked
  `pose_curve` grid is log-spaced; its inverse FFT cannot recover a physical
  impulse. Keep the figures computed from capture-time impulse evidence rather
  than treating the retained frequency grid as an impulse store.
- **Classify SBIR by seat shift:** rejected. SBIR is speaker-boundary
  interference. A front-wall notch can move only about 1% for a 0.30 m seat
  shift in the design's geometry, too little to identify its cause reliably.
  Use declared geometry to name the band; do not classify the dip from motion.
- **Add `by_candidate`:** rejected. The packet already joins by set; another
  candidate-keyed room structure would duplicate that identity.
