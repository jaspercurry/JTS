# ADR-0307: Only a measured excess-boost finding acts

- **Date:** 2026-09-12
- **Status:** Accepted

## Context

The `jasper/active_speaker/crossover_v2/coordinator.py` module docstring states
that it banks round advice and restores playback only for a measured
excess-boost finding. The `crossover_v2/verification.py` module docstring states
that capture validity, realization, benefit, and specification are independent
verdicts, with absent evidence reported rather than failed.

These contracts are implemented by
`jasper/active_speaker/crossover_v2/coordinator.py:run_round`, which banks the
evaluation, `coordinator.py:round_verdict`, which acts only on that protection
result, and `coordinator.py:_stop_excess_boost`, which records the finding and
calls the graph-restore seam.

## Decision

Only a measured excess-boost finding acts: it stops the round and attempts to
restore the prior graph through `_stop_excess_boost`. Every other adverse
verification or adoption verdict is advice to the LLM or operator. It is
banked and disclosed, but does not restore, park, or otherwise change playback.

## Consequences

Quality regressions and incomplete evidence remain visible without becoming a
second apply gate. A measured boost beyond the declared bound remains the one
automatic action because it is an output-path protection finding, not a quality
opinion.
