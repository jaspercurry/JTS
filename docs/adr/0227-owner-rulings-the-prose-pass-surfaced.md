# ADR-0227: Owner rulings the tuning prose pass surfaced with no ADR home

- **Date:** 2026-09-03
- **Status:** Accepted

## Context

The phase-1 prose pass over the tuning half (#3799, #3807, #3808, #3809,
#3813, #3814, #3815) removed roughly 20,000 lines of comment and docstring.
Each of those PRs turned up owner rulings written down **only** as a code
comment. The prose bar keeps such a ruling as one constraint line citing its
issue, which leaves the rule living in whichever file happens to enforce it:
move the file, split it, or delete the block and the rule is gone.

This ADR is the register: per ruling, the constraint it imposes, the issue or
date it was given, and the site that enforces it. Every site below was grepped
at its PR's branch head on 2026-09-03 and still exists. The argument stays in
the issue; this file records that the ruling binds, and where.

## Decision

**These rulings stand, and this file is their home until one earns its own
ADR** (which then supersedes the row).

### 1. Declared values are the only refusing authority — [#2874](https://github.com/jaspercurry/JTS/issues/2874), 2026-08-22

Only a manufacturer's **published** declared condition may refuse. A code
figure or class table may prefill, disclose and serve as fallback; it may
never refuse a declaration. Sites: `crossover_v2/topology_prescription.py` —
`TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT` (never the commissioning figure
derived from a published one) and `recommended_slope_db_per_octave` (disclosed,
never enforced); `driver_safety.py`'s low-limit author split (a research reply
outside the plausibility band is refused at intake, an operator-typed value
warns and saves). `docs/measurement-loop-doctrine.md` §§2, 5 hold the adjacent
half — a refusal names a damage mechanism — and not this one.

### 2. A correction that cannot clip costs the speaker nothing — [#1808](https://github.com/jaspercurry/JTS/issues/1808), 2026-07-28

A branch whose evaluated chain peak never exceeds unity is charged `0.0`; only
above unity does the peak plus `HEADROOM_MARGIN_DB` become program-domain
attenuation. Site: `branch_chain.headroom_charge_db`.

### 3. Settle at the rejection, not at the next begin — [#2086](https://github.com/jaspercurry/JTS/issues/2086) item 3

A refused attempt resolves its slot in place, unless the verdict already ended
the set on its own finding (`payload["terminal"] is True`). A household is
never shown a retry screen whose button leads to a pre-play refusal. Site:
`crossover_v2_flow.consume_capture` and the spent-slot reader below it.
ADR-0002 cites #2086 as the incident behind the measure-again discriminator;
it does not hold item 3.

### 4. The prediction threshold has two values, chosen by class — PR-B, 2026-08-20

The fitted class keeps `PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB` (0.5 dB); a
prescribed graph requires non-worsening (0.0), because a narrow high-Q filter
predicts only 0.077–0.152 dB of pooled improvement when it is exactly right.
**Neither bar stops anything** — the choice decides which ledger value is
banked. Site: `crossover_v2_flow._assert_accountable`.

### 5. Every prompted distance carries numeric units — [#1805](https://github.com/jaspercurry/JTS/issues/1805), 2026-07-28

Inches and/or metres on every prompt, never body-part units; centimetres
beside them because every prompted move is between 0.1 m and 0.6 m. Site:
`crossover_v2/capture_plan.format_position_distance` and the prompt tables.
`docs/two-stage-commission-flow-plan.md` records this field ruling superseding
the 2026-07-25 studio ruling; ADR-0013 holds the sibling pose rule (#1806),
not this one.

### 6. The frame is removed for the rollback question, not the capture question — [#2521](https://github.com/jaspercurry/JTS/issues/2521)

`delta_probe.classify_delta_probe` re-asks the **rollback** question with the
fitted frame removed, because refusing a correction on evidence that cannot be
told apart from the microphone is the worse error.
`program_analysis._analyze_verify` still gates on its raw number: it refuses a
capture, not a correction. A call site keeps its reported grade byte-identical
and adds the frame-removed one beside it. Site: `frame_fit.py`'s docstring.

### 7. The newest measurement wins — ruling S20

Guided captures newer than a banked base-trim record's `measured_at` answer
instead of it. That is `STATUS_SUPERSEDED`, deliberately not a refusal: the
re-measure a refusal would demand has already happened. `measured_at` is
evidence time, never write time. Sites: `driver_base_trim.py`
(`STATUS_SUPERSEDED` and the `measured_at` note) and `baseline_profile.py`.

### 8. An applied profile names the evidence it levelled by — ruling S16 (d)

`trim_source` is carried into the profile's `level_match` ledger so a receipt
reading the banked trim still names the measurement behind it. Site:
`driver_base_trim.py`'s applied-status record.

### 9. Driver protection is exactly two invariants, one owner each — 2026-07-19

Wrong-frequency-range (the declared hard band plus the proven protective
high-pass) and too-loud (**one** derived ceiling, not stacked hedges) — which
is why no absolute dBFS hedge sits above the derivation. Named residual:
declared sensitivities carry no plausibility validation
([#2765](https://github.com/jaspercurry/JTS/issues/2765)). Site:
`driver_protection.py`'s HF measurement-ceiling derivation.
`docs/crossover-measurement-productization-design.md` §W6.5 defines the model
and is being archived to `docs/historical/` (#3794);
`docs/active-speaker-tuning-layers-design.md` only records that it stands.

### 10. If it was in the safe overall envelope, it is safe to test — 2026-08-23

The tweeter crossover high-pass **corner refuses** against
`graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ` — a crossover below it puts
the excursion hazard band on a compression driver, a named damage mechanism.
The **slope only discloses** against
`PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE`, a code figure no datasheet
contains; the manufacturer's published condition is enforced at the pin. Site:
`camilla_yaml._assert_tweeter_crossover_hp_satisfies_floor`.

### 11. The staged-startup hold is an ephemeral `/run` marker

While the marker is present, `safe_graph_for_current_topology` preserves the
staged startup anchor instead of restoring the approved baseline. It lives
under `/run` on purpose: a normal boot starts empty, so a commissioned box
always comes back to audio. One TAKE, three RELEASEs. Site:
`startup_hold.py`'s module docstring; no doc holds this.

### 12. `RecordStore` is THE durable-write seam — 2026-08-26 (standing, **unexecuted**)

The five `V2FlowSeams` publishers fold into `RecordStore`, discriminated by
`kind`; fail-soft belongs in a named caller-side wrapper, never as a store
feature or a seam flag; and `EngineSeams` does not grow a seam for it.

Ruled in `docs/REFACTOR-CUTOVER-2026-08.md` §6.2 and **not carried out**: at
HEAD `crossover_v2_flow.py` still declares `publish_candidate` and
`jasper/web/correction_crossover_v2.py` still binds `bind_evidence_publishers`,
`bind_round_receipt`, `bind_findings_publisher` and `bind_cloud_publisher`.
ADR-0198 deferred collapsing the seam for a different reason.

Two of the ruling's own anchors have since moved, so the constraint above is
stated at HEAD rather than quoted: `_hand_to_retention`, the wrapper it named
as the shape for fail-soft, exists only in docs (the current durable write is
`V2FlowSeams.bank_take`, whose production binding is itself the fail-soft one —
`crossover_v2_flow.py:1080-1086`), and `EngineSeams` now carries **four**
fields (`session_seams.py`: `graph`, `volume`, `records`, `play`), not the
five the ruling counted. The cardinality invariant is "no new seam", whatever
the count.

## Already recorded — do not re-extract

- **The design axis is a member of the post-apply walk** (2026-08-24) →
  **ADR-0012**, incident and all. #3809 reported it as homeless; it is not.
  `DEFAULT_CLOUD_VERIFY_POSITIONS = 6` is that ruling's arithmetic, asserted
  at import against `1 + len(CLOUD_VERIFY_POSE_PROMPTS)`.
- **Poses are absolute; the actor is the microphone** (#1806) → **ADR-0013**.
- **R8's boost ceilings and the bounds demoted beside them** → **ADR-0207**.
- **#1675's ka/beaming figure is guidance, not a gate** → **ADR-0011**.
- **A refusal names a damage mechanism; unproven facts disclose** →
  `docs/measurement-loop-doctrine.md` §§2, 5.

## Consequences

- Deleting, splitting or moving any file named above no longer takes the rule
  with it, so the prose pass can keep cutting.
- The inline one-liners stay — one line each, at the site that enforces the
  constraint. A later pass may swap an issue citation for `See ADR-0227.`; it
  may not drop the line.
- Twelve decisions in one file is against this directory's
  one-decision-per-file rule, taken deliberately: each row is a sentence plus a
  site, and twelve one-paragraph ADRs would cost more to read than the rulings
  are worth. A row that grows an argument earns its own ADR, which supersedes
  the row.
- Entry 12 is the only row that does not describe HEAD. It is marked unexecuted
  rather than left out, because the doc that ruled it is being retired.
