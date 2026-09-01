# ADR-0207: Tier-1 prescription bounds demote — a cut is the prescriber's to spend

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

Ticket 6.8's read-only audit classified all 94 numeric bounds in the four
prescription doors against the doctrine's closed hard-stop list and against
what actually re-raises downstream (the emitter, the shipped strict
readers). Five bounds were hard refusals guarding no hearing or hardware
mechanism, with no downstream re-check — quality calibrations wearing a
gate's clothes, and exactly the bounds the flat campaign's σ-analysis
fought. The owner ruled on the audit's eight-item menu: demote items 1–5,
leave 6–8, and **when a bound is demoted fully, delete all the machinery
that powered it**.

## Decision

1. **Retired outright** (constant, check, refusal slug, contract field —
   all deleted):
   - the driver door's per-filter (12 dB) and composed (18 dB) cut
     ceilings;
   - the blend door's per-filter (3 dB) and composed (4 dB) cut ceilings
     — the deterministic solver's own copies in `blend_correction` stay,
     bounding the algorithm only;
   - the blend door's cut-Q ceiling (8.0), converging on the driver door's
     precedent (both doors' own docstrings made the identical "a narrower
     cut only shrinks its footprint" argument);
   - the min-Q floor (0.5, both doors). What remains on a filter's Q is
     the evaluable range `jasper.sound.profile` bounds a filter's
     realization to (see Consequences) — not the emitter's bare
     buildability predicate.
2. **Blend rationale length demotes to truncate-and-disclose**, mirroring
   the driver door's 2026-08-29 demotion of the identical field: the first
   1,200 characters are banked, the dropped count rides the receipt as
   `rationale_dropped_chars`, nothing refuses.
3. **Left standing, by the same ruling:** the driver composed-boost cap
   (R8's own SPL-budget policy, ten days old), the blend
   filter-outside-region bar (genuine separation of concerns), and every
   Tier-3 bound lockstep-duplicated at the emitter (demoting a door's copy
   returns zero freedom and swaps a legible refusal for a build-time
   crash).
4. **No disclosure residue is invented for the retired cut bounds.** The
   staged prescription already banks every filter verbatim — depth, Q and
   composition are on the record — and a disclosure comparing them against
   a retired policy number would be commentary, not a fact. The net is the
   round's own measured verify with auto-restore.

## Consequences

- A prescribed cut's DEPTH is bounded by the declared band and the
  per-role filter slots, and nothing else. Its Q is a different story: the
  adversarial review that followed this ruling measured f64 cancellation
  in the Peaking cascade realizing +6.99 dB from an admitted -3.0 dB,
  Q-8e14 cut, and an exact unity pole radius by Q 1e16 — not "approaches 1
  from below and never reaches it", the claim this ADR shipped with. Both
  doors now bound a filter's Q to the EVALUABLE range `[1e-4, 1e6]`
  (`jasper.sound.profile.EVALUABLE_Q_MIN`/`EVALUABLE_Q_MAX`) — an
  INSTRUMENT-fidelity bound owned by the evaluator, not a policy ceiling:
  a cut still spends no headroom and is free up to it. A gain whose
  amplitude underflows f64 (`10**(gain/40) == 0.0`, below ~-12960 dB)
  refuses as malformed rather than reaching the evaluator at all.
- The envelope asymmetry the audit surfaced (F1) is now stated to the
  driving LLM in the methodology: the mic-tier/repeatability/class-prior
  envelope bounds the deterministic fitter only — prescriptions were never
  subject to it, and the measurement grades them instead.
- The emitter comment that called the per-filter boost re-check "a
  hardware-bound safety invariant" is relabeled to what the number's own
  derivation says it is: realization fidelity (F4). Real SPL safety is the
  headroom accounting.
- The audit's F5 (two declared-band derivations at the web boundary) was
  traced and closed without a check: the corner-admissibility edges read
  the declared HARD excitation band under the proven-HP rules, while the
  filter-aiming passbands read `measurement_band_hz` narrowed by declared
  protection corners — different questions from different declared fields,
  each owned by its own module. An equality test would pin a false
  invariant.
- The 2026-08-19 cut-Q evidence record (measured natural Q 6.6/5.1/3.9;
  28–43 % on-target efficiency of Q-2.0 filters) stays in git history and
  that ruling's ADR trail; it justified widening 2.0 → 8.0 and does not
  bound the further step to unbounded, which rests on the same physics
  argument the driver door already shipped.
