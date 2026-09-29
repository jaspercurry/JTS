# ADR-0396: One reporter of a rear section's charge

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial)
  [ADR-0329](0329-cardioid-on-off-is-an-audition-layer.md)'s level-match term
  `delta(f) = change_db(f) + relative_charge`, and
  [ADR-0325](0325-rear-program-compares-measured-symptoms-and-previews-by-superposition.md) §2's
  "headroom change" among a summed round's figures. Carries the owner's answer B for
  [#5909](https://github.com/jaspercurry/JTS/issues/5909) H4 item 1
  ([#5925](https://github.com/jaspercurry/JTS/issues/5925), comment 5887122300).
- **Context:** Since [ADR-0385](0385-the-program-charge-is-the-emitted-graphs-peak-with-one-margin.md)
  the program charge is one function of the whole emitted graph. The rear views still reported the
  rear stage's own additive peak (`rear_branch_sum_headroom_db`), the rule before ADR-0385:
  - the summed rear view: `headroom_charge_db` and `headroom_change_db` on each candidate;
  - the rear preview: `stage.headroom_charge_db`, and `stage.relative_charge`, which it subtracted
    from every `change_db`.

  That number is not the program charge; on the #5925 fixture it was off by about 1.4 dB. A program
  charge in a view needs one full compile per variant, and a choice of the graph that a rear section
  plays in. Nothing that plays read the views' numbers: the audition trim added `relative_charge`
  back.
- **Decision:**
  1. The rear views report no charge. The summed view's `headroom_charge_db` and
     `headroom_change_db` go, and so do the preview's `stage.headroom_charge_db` and
     `stage.relative_charge`.
  2. The preview's `curve.change_db`, `trough_fill_db` and `bands[].change_db` are the plain change
     against the document with its rear muted, at one headroom, as the Cardioid On|Off switch plays
     it. The audition's broadband delta is the power mean of that `change_db`, with nothing added
     back.
  3. `jasper-crossover-prescriber judge --preview` is the one reporter of a rear section's charge. It
     composes the document on its `base`, as `judge` does, and answers `program_charge_db` from
     ADR-0385's one function (`candidate_parts.program_charge_db`). A `--vary` row carries it too. A
     base or a document that does not compose refuses the preview, as a driver or blend preview
     does.
- **Consequences:**
  - The audition trim is unchanged but for rounding. The old path rounded the change and the charge
    to 0.001 dB each before it added them; the new path rounds the change once. The broadband delta
    moves by less than 0.001 dB, so a trim moves by at most one 0.01 dB step, and only when the
    delta sits at a rounding edge. On the four fixture sections measured for #5909 the trim is
    byte-identical.
  - A rear preview costs about 2× what it did: 0.22 s on a laptop, of which the composition is
    0.12 s. The audition's preview and the views compose nothing.
  - A rear preview now needs a base that composes; before, it read no base. Its acoustic figures do
    not depend on the base.
  - A trial plays each candidate at its own charge. To read a preview as a trial, subtract the rise
    of `program_charge_db` over the rear-muted copy's.
  - The shapes move: `jts_rear_view/3`, `jts_prescription_preview/2` and
    `jts_prescription_preview_grid/2`. A rear round banked before this keeps the packet its bank
    stored ([ADR-0371](0371-a-rounds-evidence-packet-is-built-once-when-it-is-banked.md)).
  - `branch_chain.rear_branch_sum_headroom_db` stays as the rear stage's own peak for the laptop
    design scripts. It is not a charge.
  - No emitted graph changes.
  - Rejected:
    - The views report the program-charge change on the round's parent graph (option A). It costs
      one compile per variant, 4–5× the summed view, and a view that reads a base graph.
    - Keep the additive sum under a new name (option C). It is not the program charge.
    - A preview that answers its figures with `program_charge_db` as a gap when the base does not
      compose. It adds a second shape for one number.
