# ADR-0344: Every round-view answer carries one envelope

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** An agent tunes the speaker by reading `jasper-round-views`
  answers side by side, the way a person reads traces in Room EQ Wizard. REW
  shows each trace's window, smoothing and band; our answers did not. Only
  `directivity` stated its parameters (the review also found them in
  `per-seat --include`, since deleted), most view artifacts had no schema,
  and two success artifacts used a top-level `status` key, which ADR-0237
  reserves for failure documents. The agent could not tell whether two
  answers were comparable or whether an answer's shape had changed (the
  2026-09-22 toolbox review, finding R1-10).
- **Decision:**
  1. Every `jasper-round-views` answer carries `view`, `schema`, `subject` and
     `parameters`, built in one place, `jasper/cli/round_views/_common.answer`.
     No view hand-builds these keys.
  2. `subject` names what the view read: the round's catalog id, the set id,
     the take ids and the candidate id, each only when it applies. A view
     that compares or joins rounds lists one subject per round under
     `rounds`, in argument order, even when given one.
  3. `parameters` holds only the analysis parameters the view used
     (smoothing, window, band, reference level, calibration id, and the like),
     taken from the argument or constant the code used, or the engine's own
     record of it, never from a restated literal. A parameter whose input was
     not given is null; a view that graded nothing states none.
  4. `schema` is `jts_<name>/<version>`. The view's artifact and its answer
     carry the same string: `ARTIFACT_BY_VIEW` rows hold it and every writer
     of a view artifact stamps it. `ANSWER_SCHEMAS` holds it for the three
     answers no row names (`speaker-fit` and `close-reference --distance`
     write no artifact; `repeat --set` writes one no row lists). Rows for
     artifacts no view writes (the run manifest, the position cycle) name
     none. Removing or renaming a key, or changing what a value means, bumps
     the version; adding a key does not.
  5. A `jasper-round-views` success answer or artifact never has a top-level
     `status` key. The two that had one say `outcome`.
  6. A view addresses one take with one flag, `--take <take-id>` (or a
     side-named pair such as `--far-take` / `--close-take`), not by path,
     phase or position.
- **Consequences:** An agent can compare two answers by their `parameters`
  and notice a shape change by its `schema` before it misreads a key. Each
  new view must declare its parameters, and a reader keyed on a retired flag
  or `status` key breaks loudly instead of drifting. Known debt:
  `jasper-round list|show` rows, `run_manifest.json` and the bookkeeping's
  per-view records in `provenance.json` still carry a `status` key (none is a
  round-view answer or artifact), and the gate-sweep and distortion artifacts
  keep an old integer version beside `schema`. Rejected: a per-view envelope
  (the drift this replaces) and a `view/1` spelling (the six existing
  `jts_*/1` strings have readers; one convention wins).
