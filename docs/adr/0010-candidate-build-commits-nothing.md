# ADR-0010: A candidate build commits nothing — and the accountability gate lives outside the builder

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Fitting a candidate takes seconds, so the session runs it speculatively before
the household has confirmed anything. That is only safe if a speculative build
can be dropped and leave the session exactly as it was. Separately, the
accountability measurements — the realized inter-driver level and the
spec-graded prediction (linearization-integrity PR-L4 items 1 and 2) — have a
placement question of their own: inside the fit, or outside it.

Both rulings live in two adjacent docstrings in
`jasper/active_speaker/crossover_v2_flow.py:8329-8352` and `8375-8393`, and
nowhere else.

This ADR extracts them before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). The refactor makes
per-candidate work cheap by design — one session graph, `patch_config` per
candidate — so "what does building a candidate commit?" becomes the question
the whole inner loop rests on.

## Decision

**"Commits nothing" has an enumerated meaning.** Quoted from
`crossover_v2_flow.py:8375-8380`:

> **What "commits nothing" has to mean for that to be safe.** Three things make
> a candidate REAL, and none of them happen here: it is not written to
> ``self._candidate`` (the fire-once guard), the ``publish_candidate`` seam does
> not fire (no evidence is written), and the retained MEASURE analysis is not
> released. So a build that a retake moots can simply be dropped, leaving the
> session exactly as it was.

Three named commitments, not a vibe. A speculative stage is safe exactly when
it takes none of them, and a new commitment added to the builder silently ends
the property.

**The accountability gate runs at build time — it is part of producing a
candidate, not part of proposing one** — and it *"refuses nothing"*; what it
produces is the level-frame record, banked against the candidate it is about.

**But it may not live inside the fit.** From `crossover_v2_flow.py:8340-8344`:

> They live here and not inside :meth:`_build_candidate` on purpose: that
> method's SF2 arm catches a fit-engine failure and degrades to the trims-only
> path, which is the right answer for a BUG in the fit and the wrong answer for
> an accountability verdict — a verdict banked about a candidate nobody built
> describes a graph that was never proposed.

The general rule: **a stage that degrades on failure may not host a stage that
must not.** A fallback arm is correct for a fit bug and wrong for a verdict,
and putting them in one method makes the fallback swallow the verdict's subject.

**Neither accountability check refuses.** From `crossover_v2_flow.py:8332-8335`:

> **NEITHER refuses.** Item 2 stopped with the nanny burn-down (#2854,
> deviation (c)) and item 1 with the realized-level demotion (deviation (i));
> both now GRADE, bank what they measured, and let the round proceed.

They still run after the build and before the candidate is published, *"because
that is where the numbers they grade exist"* — placement follows the data, not
the call graph's convenience.

## Consequences

- Speculative fitting stays safe as long as the three commitments stay outside
  the builder. Any future state a builder writes has to be checked against that
  list, and the list is the thing to keep — not the specific field names.
- The engine's `analyze`/`recommend` stages may run ahead of the household and
  be discarded. That is what makes "many candidates per position" affordable,
  which is the whole point of rule 2's loop ordering.
- Accountability verdicts stay outside any stage with a degrade arm. A verdict
  banked about a candidate nobody built is worse than no verdict.
- Deliberately given up: a single method that both fits and grades. The split
  is real structure, and it is the price of a fallback that cannot lie.
