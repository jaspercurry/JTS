# ADR-0019: A declared-metadata gap on an optional surface never refuses the session — it degrades to the default

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Session open resolves a driver's declared facts from the confirmed safety
record. Some of those facts are **clamp inputs** — the excitation ceilings and
sweep-duration limits, whose absence is a refusal (`CrossoverV2Refused`, "the
`{role}`'s safe excitation limits could not be resolved"). One of them is not:
the tweeter's declared measurement band, an optional surface added by the
flat-linearization plan's PR-4 whose consumer already degrades to a module
default on `None`.

The ruling that keeps the second kind out of the first kind's refusal path
lives in one call-site comment,
`jasper/web/correction_crossover_v2.py:6290-6298`, and nowhere else.
`tests/test_correction_crossover_v2_conductor_context.py:436-442` states the
rule while explicitly deferring to that comment as the authority — *"see
``resolve_conductor_context``'s own comment at the call site"* — so deleting
the comment leaves the rule with no home.

This ADR extracts it before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7).

## Decision

**A declared-metadata gap on an optional surface must never turn into a refused
measurement session.** Quoted from `correction_crossover_v2.py:6294-6298`:

> it is still wrapped, because a declared-metadata gap on this NEW, optional
> surface must never turn into a refused measurement session — the conductor's
> own ``tweeter_measurement_band_hz`` ctor param degrades to the module default
> on ``None``.

**The discriminator is what the fact gates, not where it is stored.** Both
kinds of fact are read from the same confirmed safety record in the same loop.
The excitation ceiling refuses because it is a clamp input — nothing may play
until it resolves. The measurement band does not, because its consumer has a
default and nothing unsafe happens without it. Storage location, record type,
and resolver module are all shared and none of them is the test.

**A new optional surface is opt-in for its consumer, never a new refusal for
everybody.** The comment's own reasoning notes the call is *"not expected to
raise here"* — the same field was already validated upstream for this role —
*"it is still wrapped"* anyway. The wrap is not defending a known failure; it
is a standing guarantee that adding an optional declared field cannot make a
previously-working session stop opening.

**This is ruling S10's shape at session open.** S10 holds that outside the
§4 clamps, an unproven or stale fact discloses and never stops the work, and
that *refusing to CLAIM stays while refusing to WORK dies*. A missing optional
declaration is exactly the unproven fact S10 governs: the session opens, the
measurement runs, and the only thing lost is a claim nobody was making. Wave 7j
applies the same rule to the #2935 topology-staleness block.

## Consequences

- Adding a declared field to the safety record is a bounded change. It cannot
  refuse a session that has been opening fine, so the blast radius of new
  metadata is the consumer that asked for it.
- Every resolver call at session open owes an explicit answer to "is this a
  clamp input?" The two kinds sit adjacent in one loop, so the classification
  has to be visible at the call site rather than inferred from the record.
- The engine's session open inherits a two-class read: clamp inputs refuse,
  everything else degrades to its default. That split is the same one ADR-0002
  draws for refusals after the capture, applied before it.
- Deliberately given up: early detection of an incomplete driver record. A
  speaker missing an optional declaration measures normally and the consumer
  falls back — which is the intended outcome, not a silent failure, because the
  fallback is a documented module default rather than a guess.
