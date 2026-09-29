# ADR-0385: The program charge is the emitted graph's peak, with one margin

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes:
  - [ADR-0324](0324-cardioid-headroom-is-the-stages-evaluated-peak.md)'s charge decision. Its
    evaluator, grid and all-pass Q bound stand.
  - [ADR-0121](0121-preference-boosts-boost-room-boosts-are-compensated.md)'s positive-sum room rule, on
    the active path only.
  - [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md) §5's
    rise formula.
  - [ADR-0345](0345-a-timing-reading-that-is-not-comparable-never-asks-for-a-reset.md) §1's rule that the
    rear headroom no longer charged moves into the trims.
  - [ADR-0367](0367-a-drivers-declared-band-bounds-a-boost-and-discloses-a-cut.md)'s consequence that
    `boost_headroom_by_role` refuses in the door. Composition judges the charge now (#6007).
- **Context:** The emitter added separate peaks ahead of the split:
  - the positive room gains;
  - the worst linearization branch plus 1.0 dB;
  - the rear stage's peak;
  - the output trim.

  Those layers run in series on each output, so the sum over-charged, by up to 10.6 dB on the #5909
  stress case. A one-way trim was never credited, and the room and rear terms paid no margin
  ([#5909](https://github.com/jaspercurry/JTS/issues/5909)). The owner asked for a positive and a
  negative at different parts of the chain to net out. `program_headroom.program_peak` is already the
  verifier's one number (#5938).
- **Decision:**
  1. **The charge** (`program_headroom.charge_db`) is the emitted graph's `program_peak` with its own
     `active_baseline_headroom` held at 0 dB, plus one `HEADROOM_MARGIN_DB` (1.0 dB) when that peak is
     over `PEAK_EPS_DB` (1e-3 dB), plus the output trim, which is never netted.
     - The emitter emits at 0 dB, measures, then emits again at the charge.
     - Above 40 dB it refuses `program_headroom_exhausted`, as before.
     - A graph with no modelled transfer refuses `program_headroom_unreadable`.
  2. **What nets.** Every series stage and mixer sum ahead of an output: room, blend, the rear stage,
     crossovers, protection and bass-management high-passes, linearization and trims. Preference EQ
     (ADR-0121), dynamic bass (ADR-0359), the limiters and the fader do not net.
  3. **One ε.** `graph_types.PEAK_EPS_DB` is both the uncharged threshold and the verifier's slack.
  4. **Timing.** The timing take folds its candidate graph's whole charge into its trims, so it never
     plays louder than its candidate.
  5. **The room-off rise.** It is what the applied room layer adds to the played graph's charge
     (`measurement_emit.room_layer_charge_db`), less the layer's lowest in-band response, floored at 0.
- **Consequences:**
  - Only the `active_baseline_headroom` value moves in emitted graphs.
    - On the #5909 corpus, 34 of 498 graphs play louder at the same fader, by up to 3.92 dB.
    - 19 play 0.69-1.0 dB quieter: a room-only or rear-only charge now pays the margin.
    - A one-way +4 dB boost charges 5.0 / 3.0 / 0.0 dB at trims of 0 / −2 / −6 dB, which settles
      ADR-0374's unconverged one-way default.
  - The ceiling does not move. On the grid, every charged output peaks one margin under unity, where a
    flat tune already plays. These stand unchanged: `volume_limit` 0.0, the `set_volume_db` clamp, the
    −1 dBFS limiters, the verifier's inequality, the SPL stop and the declared driver caps. Overshoot
    above about 22 kHz and transients stay with the limiters.
  - A graph keeps its old charge until it is re-emitted.
    - The operator re-emits and reloads with nothing playing, so the step does not land un-ducked at a
      `/sound` edit.
    - Then the operator re-levels the seat anchor. An anchor banked before this under-predicts SPL by
      the charge drop.
  - Disclosed, not changed:
    - The judges compile without the declaration's protection sections, so their charge can exceed the
      applied graph's. That is the safe side.
    - A tune whose charge nets to exactly 0 dB becomes groupable (`grouping_applied_prefix_unsupported`
      no longer applies).
    - The emit costs about 3× on a laptop. A cache follows only if one `/sound` live-draft move measures
      over about 250 ms (ADR-0226).
  - Rejected:
    - The additive split.
    - A margin per layer.
    - Keeping the one-way over-charge.
    - Netting the output trim.
    - A continuous peak search. The grid residue is under 0.2 dB below 22 kHz, inside the margin.
