# ADR-0353: The cabinet model is an optional laptop-side aid

- **Date:** 2026-09-23
- **Status:** Accepted
- **Context:** On 2026-09-23 jts3's two woofers were measured in the near field with the UMIK-2
  (the rear pair program, staged at close and behind poses, analysed offline because the pair
  check refuses near-field takes). The takes were multiplied by a Boundary Lab solve of the
  cabinet from the CAD repo. The integral reproduced the solver's probes to 3e-4 and the measured
  15 → 30 mm level step within 0.15 dB. The model then predicted the live tune's front/back and
  seat response and fitted the rear stage now on trial on jts3
  ([scripts/cabinet-model/README.md](../../scripts/cabinet-model/README.md) has the numbers).
  The toolbox is microphone-only ([measurement-loop doctrine](../measurement-loop-doctrine.md)),
  and Boundary Lab, the enclosure model and the solve live in the CAD repo on a Mac.
- **Decision:** The cabinet model lives in `scripts/cabinet-model/` as laptop-side scripts,
  pointed to from the tuning runbook and the testing-tools index. It is optional: no speaker
  code, program, page or check depends on it, and the install gains nothing. It reaches the
  speaker only as an ordinary prescription document, through `jasper-crossover-prescriber
  judge`/`compose` and `jasper-round apply`, the same gates as a hand-written document. Its
  capture stages the rear pair program until #5684 ships a near-field program.
- **Consequences:** Given Boundary Lab and a solved case of the cabinet, an agent can predict the
  woofer pair without a room and at the seat, and fit the rear stage for the seat; without them
  the toolbox works as before. The scripts call the repo's own evaluators
  (`camilla_filter_response`, `rear_stage_response`, `expected_boost_db`, `fit-rear-branches.py`),
  so they follow those owners, and they stop on a graph stage they do not model. Proof is
  reproducing the 2026-09-23 run; tests pin that the scripts load, that Boundary Lab's phasors
  are read into the repo's phase convention, and where the wall notch falls. Rejected: a
  speaker-side BEM (1 GB of RAM, and the solve belongs with the CAD), and a new tuning program
  (the web near-field flow is #5684).
