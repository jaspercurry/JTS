# ADR-0017: Position retention keeps the RAW capture, never a derived summary — and the fail-soft boundary sits at the caller

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Each position in a measurement group writes one WAV plus one metadata sidecar.
Two design questions sit on that seam: what gets kept, and where a write
failure is allowed to be soft.

Both answers live in one function docstring,
`jasper/web/correction_crossover_v2.py:5061-5086`, and nowhere else. The
incident cited is the S0 campaign.

This ADR extracts them before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). The refactor's ONE
CAPTURE RECORD is a wave-4 rewrite of exactly this material, and §1 names the
curve as the record block that does not exist today for a lateral pose —
*"and it is the whole gap."* The retention rule is the part of today's design
the new record must carry forward rather than replace.

## Decision

**Keep the raw capture, because the question has not been asked yet.** Quoted
from `correction_crossover_v2.py:5063-5067`:

> The forensic record the position-group choreography owes: the S0 work that
> produced this program's central finding (source-fixed vs room-fixed comb
> attribution) was only possible because every position's RAW capture survived,
> so a household cloud keeps the same thing rather than a derived summary that
> cannot answer a question nobody has asked yet.

A derived summary is scoped to the analysis that produced it. The S0 finding
came from re-analysing raw captures for a question nobody had when they were
taken, and that is the case retention exists for.

**The sidecar is the only durable record of WHERE.** It *"carries the prompt the
operator was given, which is the only durable record of WHERE a curve was
measured"*, and its `position_id` is what lets a flagged or outlying position be
named back to the household. Place is banked with the capture or it is lost.

**Placement follows the shipped scheme, not a private layout.** A position's WAV
lands beside the flow's other summed captures rather than in a layout only this
seam understands.

**The fail-soft boundary lives at the caller, not at the writer.** From
`correction_crossover_v2.py:5079-5086`:

> This function does NOT swallow failures. The evidence store is deliberately
> strict — ``publish_json_artifact`` raises ``CommissioningEvidenceStoreError``
> (a ``RuntimeError``) rather than silently dropping an artifact, and a WAV
> write raises ``OSError`` — and the fail-soft boundary lives one level up, at
> the conductor's ``_retain_cloud_position`` call site, which logs and continues
> so a full disk cannot turn an acoustically-good position into a retake.
> Keeping the boundary there rather than here means the strictness the store was
> built for is preserved for every OTHER caller.

**The general rule: soften at the call site that knows the cost, never in the
shared writer.** A writer that swallows its own failures has softened them for
every caller, including the ones that needed the raise. The caller here knows
one specific thing the writer cannot — that a full disk should not cost a
household a good capture.

## Consequences

- The engine's capture record banks the raw capture and its place, and any
  derived curve is in addition to them, never instead of them. That is what
  keeps a future campaign able to re-analyse a corpus for a question nobody has
  yet.
- Strict shared writers, soft call sites. When a wave moves a writer, its
  callers' fail-soft decisions move with it — and a writer that acquires a
  `try/except` has silently changed policy for callers it cannot see.
- Deliberately given up: storage. Raw captures for every position of every
  round are the cost of the S0-class finding being possible at all.
