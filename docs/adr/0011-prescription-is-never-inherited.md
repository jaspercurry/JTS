# ADR-0011: A prescription is one round's explicit instruction — never inherited, and validated against its own round's corner

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

A round may carry a prescription: an explicit instruction naming a topology
corner and a delay for this round only. Session open validates it. Two rulings
govern that validation, and both live in one comment block —
`jasper/web/correction_crossover_v2.py:7528-7564` — with no doc home.
[#2662](https://github.com/jaspercurry/JTS/issues/2662) is the issue the prose
cites.

The LLM front end is the prescription producer the refactor is building
toward, so these bind the engine's session open rather than the wizard.

## Decision

**Rule 1 — a prescription is never inherited.** Quoted from
`correction_crossover_v2.py:7539-7542`:

> Never inherited from the lapsed session's durable state the way ``tier``
> above deliberately is: a prescription is one round's explicit instruction,
> and a "measure again" that silently re-ran a candidate would put that
> candidate's name on a round nobody asked for.

The contrast with `tier` is the useful half: some durable state *should* carry
forward across a lapse, and some must not. The discriminator is whether the
value is a standing property of the setup or an instruction someone issued
once.

**Rule 2 — the topology pin is read first, and the order is load-bearing.**
From `correction_crossover_v2.py:7544-7549`:

> The TOPOLOGY pin is read FIRST, and the order is load-bearing rather than
> alphabetical: it decides the corner this round runs at, and the delay gate
> below is a half-period AT that corner. Read the other way round, a 4000 Hz
> round's delay would be bounded by the incumbent 1648.7 Hz lobe (303 us)
> instead of its own (125 us) — a gate that passes candidates the round it is
> gating cannot support.

**A gate must be evaluated in the frame the round will actually run in.** This
is ADR-0003's frame rule in its admission form: bounds derived from the
incumbent state do not gate a round that changes that state.

**Rule 3 — validate at the untrusted-input boundary, and after the prior
questions.** From `correction_crossover_v2.py:7528-7537`:

> It is the untrusted-input boundary, so a malformed or out-of-lobe
> prescription is refused before any evidence store, relay registration, or
> capture — an operator walking a delay sweep learns at the tap, not after a
> ten-minute measurement. It is the first point holding the crossover corner
> the bound is a half-period of. And it sits AFTER the two speaker-level gates
> above rather than before them: whether this speaker can be measured at all is
> a prior question to whether this request's prescription is good, and
> answering them in the other order would hand a household a prescription error
> for a speaker whose protection cannot be used either way.

Refusal ordering follows question dependency: a prior question answered first,
so the message names the real blocker.

**Every bound is a declaration, asked of the module that owns it**, and the
ka/beaming onset rides along as disclosure only —
[#1675](https://github.com/jaspercurry/JTS/issues/1675) made it guidance, and
no admissibility bound anywhere reads it.

**Rule 4 — derive a decision's inputs only for the rounds that take that
decision.** From `correction_crossover_v2.py:7558-7564`:

> The declarations are gathered ONLY for a request that carries a pin, and that
> branch is deliberate rather than an optimisation. Several of this context's
> fields are read nowhere else on this path; deriving them unconditionally would
> make an ORDINARY round's session-open depend on declarations it is not using
> … so a context shaped for a decision this round does not take could fail a
> round that never asked for one.

## Consequences

- The engine's session open must distinguish standing setup state (carries
  forward) from a round's instruction (does not). Presets are parameters, so
  the same test applies to them: a preset chosen for one round is not inherited
  by the next unless someone chose it again.
- Any admission bound derived from the current state must be re-derived against
  the state the round will run in. Reading the incumbent is the defect.
- Refusal messages name the first-failing prior question, so an operator fixes
  the real blocker rather than the first one alphabetically.
- Deliberately given up: convenience re-runs. "Measure again" after a lapse
  starts an ordinary round; re-running a prescription means issuing it again.
