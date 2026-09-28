# ADR-0385: The program charge is the emitted graph's peak, with one margin

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes:
  - [ADR-0324](0324-cardioid-headroom-is-the-stages-evaluated-peak.md)'s Decision, which charged the rear
    stage's peak on its own beside the room boost. Its evaluator, grid and all-pass Q bound stand.
  - [ADR-0121](0121-preference-boosts-boost-room-boosts-are-compensated.md)'s room-boost rule on the active
    path (the sum of positive room gains). The passive stereo prefix keeps it for now.
  - [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md) §5's
    rise formula, where `charge` was the room layer's positive-boost total.
  - [ADR-0345](0345-a-timing-reading-that-is-not-comparable-never-asks-for-a-reset.md) §1's sentence "The
    rear headroom that is no longer charged moves into the trims, so muting the rear raises no level."
  - [ADR-0367](0367-a-drivers-declared-band-bounds-a-boost-and-discloses-a-cut.md)'s consequence that
    `boost_headroom_by_role` refuses `driver_composed_boost_exceeded` in the door. Composition judges the
    charge now (#6007).

## Context

The emitter charged the pre-split attenuation as a sum of separate peaks:
- the positive room gains;
- the worst linearization branch plus 1.0 dB;
- the rear stage's peak;
- the output trim.

Those layers sit in series on each output, so the sum over-charged, by up to 10.6 dB on the #5909
stress case. A one-way trim was never credited, and the room and rear terms paid no margin
([#5909](https://github.com/jaspercurry/JTS/issues/5909)). The owner (2026-09-27): "if there is a
negative and a positive at different parts of the chain, they net out to optimize headroom where it
makes sense." `program_headroom.program_peak` is already the verifier's one number (#5938), and every
judge reads the charge off the compiled graph (#6007).

## Decision

1. **The charge.** It is the emitted graph's `program_peak`, taken with the graph's own
   `active_baseline_headroom` held at 0 dB, plus one `HEADROOM_MARGIN_DB` (1.0 dB) when that peak is
   over `PEAK_EPS_DB` (1e-3 dB). The household's output trim is added after that and is never netted.
   The one function is `program_headroom.charge_db`.
   - The emitter emits its graph at 0 dB, measures it, then emits it again at the charge. Only that
     gain's value differs between the two emits.
   - Past `MAX_PROGRAM_HEADROOM_DB` (40 dB) it refuses `program_headroom_exhausted`, as before.
   - A graph with no modelled transfer refuses `program_headroom_unreadable`.
2. **What nets and what does not.** It nets every series stage and mixer sum ahead of each output:
   room cuts and boosts, blend, the rear stage, crossovers, protection and bass-management high-passes,
   linearization and trims. It does not net preference EQ (ADR-0121), dynamic bass (ADR-0359), the
   limiters or the fader.
3. **One ε.** `PEAK_EPS_DB` decides what rides uncharged and is also the verifier's slack on a
   charged peak. No emitted graph can fall between the two.
4. **Timing.** The timing take folds its candidate graph's whole charge into its trims. Its own graph
   then charges only what still peaks, so it never plays louder than its candidate.
5. **The room-off rise.** It is what the applied room layer adds to the charge, less that layer's
   lowest in-band response, floored at 0. "What it adds" is `candidate_parts.room_layer_charge_db`:
   the charge with the layer less the charge without it.

## Consequences

**What changes.**
- Only the `active_baseline_headroom` value changes in an emitted graph.
- On the #5909 corpus, 34 of 498 graphs play louder at the same fader, by up to 3.92 dB.
- 19 play 0.69-1.0 dB quieter: charges from the room or rear stage alone, which now pay the margin.
- A one-way +4 dB boost charges 5.0 / 3.0 / 0.0 dB at trims of 0 / −2 / −6 dB. ADR-0374's unconverged
  one-way default is gone.

**What does not move.**
- The level ceiling. On the grid, every charged output peaks one margin under unity, where a flat tune
  already plays.
- `volume_limit` 0.0, the `set_volume_db` clamp, the −1 dBFS limiters, the verifier's inequality, the
  SPL stop and the declared driver caps.
- Overshoot above about 22 kHz and transients stay with the limiters (ADR-0324).

**Rollout.**
- A graph already emitted keeps its old charge until something re-emits it. The operator re-emits and
  reloads with nothing playing, so the step does not land un-ducked at the next `/sound` edit.
- Then the operator re-levels the seat anchor: an anchor banked before this under-predicts SPL by the
  charge drop.

**Disclosed, not changed.**
- The judges compile without the declaration's protection sections, so their charge can be above the
  applied graph's. That errs on the safe side.
- A tune whose charge nets to exactly 0 dB becomes groupable: it no longer trips
  `grouping_applied_prefix_unsupported`.
- The emit costs about 3× on a laptop. A charge cache follows only if one `/sound` live-draft move
  measures over about 250 ms (ADR-0226).

**Rejected.**
- The additive split.
- A margin per layer.
- Charging the door's way everywhere, which keeps the one-way over-charge.
- Netting the output trim.
- A continuous peak search. The grid residue is under 0.2 dB below 22 kHz, inside the margin.
