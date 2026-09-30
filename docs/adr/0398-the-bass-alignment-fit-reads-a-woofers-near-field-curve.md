# ADR-0398: The bass alignment fit reads a woofer's near-field curve

- **Date:** 2026-09-30
- **Status:** Accepted. Amends
  [ADR-0360](0360-near-field-driver-takes-are-reference-evidence-one-driver-per-pose.md)'s
  consequence "Near-field evidence stays out of every fit until the splice lands".
- **Context:** A `linkwitz_transform` starts from the box's measured alignment, `source_hz` and
  `source_q` (ADR-0359), and no tool measured it. The laptop cabinet model fitted a 2nd-order
  high-pass to each woofer's near-field curve (ADR-0353), off the menu.
  [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB9 names both inputs of an on-speaker
  view: the `nearfield` view's per-woofer curve, or a bass take's banked curve.
- **Decision:**
  1. `jasper-round-views bass-alignment <round>` fits each woofer's near-field raw curve at its
     nearest placement, and answers `source_hz`, `source_q` and `residual_db`: the in-box
     alignment the Linkwitz transform starts from. `--take` fits one bass take's banked curve as
     played. The fit is the cabinet model's, moved into the package.
  2. ADR-0360 §2 stands: no reader of a tuning purpose admits a near-field take. The view is
     called by name, and its answer is advisory; a prescription document states the values.
- **Consequences:**
  - An agent reads the transform's source from the toolbox; `judge` still checks the document
    that states it.
  - Linearization still waits for the [#5695](https://github.com/jaspercurry/JTS/issues/5695)
    splice.
