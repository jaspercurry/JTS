# ADR-0346: Analysis views never write a round's evidence

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** Candidates bind to a round's `packet_fingerprint`: a composed
  candidate records it and a prescription echoes it. Two optional views,
  `classify-features` and `distortion`, wrote their outputs into the round's
  own evidence directory, and the packet read those files, so the
  fingerprint — and every candidate's binding — changed with which optional
  views someone happened to run, and `jasper-crossover-prescriber status`
  suggested running one as if it were a measurement step (the 2026-09-22
  toolbox review, finding R1-11). In Room EQ Wizard a view is a pure function
  of the measurements and its parameters; it never changes the measurement.
- **Decision:**
  1. A view writes only beside the round, where `default_out` places every
     round view's artifact, never inside the round's evidence set.
  2. The packet cites those two views' outputs, read where they were written
     (`round_inputs.view_path`), in a `derived_views` block that
     `packet_fingerprint` skips. Readers that need a view's result (the
     driver gate's feature classification, `status`) read it from
     `derived_views`.
  3. Every existing round keeps its fingerprint byte-for-byte. A round that
     filed view outputs inside its evidence before this decision still has
     them read into the same fingerprinted blocks; `derived_views` falls back
     to those files when nothing beside the round replaced them and says
     `legacy_view_files_in_evidence: true`, and candidates bound to such a
     round keep resolving.
  4. `status` offers no view run as a next command; `inventory` lists the
     views a round lacks.
- **Consequences:** Running `classify-features` or `distortion` on a banked
  round leaves its evidence and its fingerprint unchanged, so an agent can
  analyse freely without orphaning a candidate. Known debt: the evidence
  blocks `feature_classification` and `harmonics` now carry only those legacy
  files, and dropping them moves every round's fingerprint once, so they go
  when no such round matters; until then a new round's `not_evaluated` still
  lists `per_bin_minimum_phase_class` beside a derived classification. Known
  exception, left for its own decision: the `room` view rewrites the
  `room.json` the bank wrote, and the room contract digest inside the
  fingerprint reads it, so re-running `room` still moves the fingerprint.
  Rejected: recomputing old rounds' fingerprints under the new rule (every
  candidate and prescription bound to them would stop matching), and
  computing the classification and H2/H3 reading at bank time as evidence
  (both are parameterised views, and re-deconvolving every capture would put
  heavy analysis on the Pi at bank time).
