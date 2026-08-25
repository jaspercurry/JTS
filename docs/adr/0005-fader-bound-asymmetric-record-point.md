# ADR-0005: A fader position tracked across a fallible write is a LOWER BOUND, recorded on an asymmetric point

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The seat-level ramp walks the fader up toward a target band, and its teardown
and its refusals both have to answer "where is the fader right now?" A fader
write either lands or raises, so after attempting `old -> L` the true position
is unknown — it is one of `{old, L}`. Recording the intended level at a fixed
point (always before the write, or always after) is wrong on one of the two
directions, and the wrong one commands the room UP.

The reasoning lives in one closure's docstring —
`jasper/active_speaker/seat_level_ramp.py:2084-2122` — with the two facts it
separates declared just above it at `2071-2082`.
`tests/test_active_speaker_seat_level.py:3145-3154` names it while building the
fixture that exercises it, but does not carry the argument. Nothing in `docs/`
states it at all.

`fader_db` is one of five overlapping level notions the tuning refactor
collapses into a single declared level owned by one volume owner
(`docs/REFACTOR-TUNING-2026-08.md` §3 wave 5a), so the variable this reasoning
is attached to is scheduled to disappear. The reasoning is not. This ADR
extracts it before the code moves (§0 rule 1, §6 R7).

## Decision

**Two facts, not one.** The level the walk believes it is measuring at (its
INTENT) and where the fader actually is are different quantities, and a fade
leg moves the second without moving the first
(`seat_level_ramp.py:2071-2082`, issue #2929). Anything asking "what is the
room hearing right now" reads the second.

**The second fact is a LOWER BOUND, and the record point is asymmetric.**
Quoted from `seat_level_ramp.py:2087-2101`:

> **The asymmetric record point is the whole design, and it is load-bearing.**
> A write either lands or raises, so after attempting ``old -> L`` the true
> position is one of ``{old, L}``:
>
> * DOWNWARD (``L < old``): both candidates are ``>= L``, so recording ``L``
>   BEFORE the write keeps the bound valid even when the write raises.
> * UPWARD (``L > old``): both candidates are ``>= old``, so the bound must
>   stay ``old`` until the write is known to have landed — recorded AFTER.
>
> Record-after-always is unsafe on the downward-raise path (the bound stays
> high, and a teardown computed from it commands the room UP past a fader that
> did move). Record-before-always is unsafe on the upward-raise path, for the
> mirror reason. Hence the branch — if this is ever simplified, that is the
> reasoning that has to survive.

**The residual is disclosed, not hidden, and it is in the safe direction.**
From `seat_level_ramp.py:2103-2108`:

> On a FAILED downward write the bound sits below the true fader, so the
> teardown fades from too low and ``cancel_tone`` cuts a stimulus that is still
> higher than the teardown thinks: the #2929 shape, degraded but never upward.
> That is the safe direction, and it is the price of a bound that can never be
> too high.

A bound that can never be too high is the property being bought. The cost is a
teardown that can start too low. That trade is the decision.

**One setter, not a second variable.** From `seat_level_ramp.py:2110-2114`:

> One setter and not a second variable, deliberately: two values that must be
> kept in sync is the defect this closure exists to close. Every write made
> while ``fader_db`` still has a reader goes through here

The one write that legitimately bypasses the setter is the teardown's own fade,
and only because it runs after the result it would inform is already built —
"the only thing tracking it could change is a value nothing goes on to read."
A bypass needs that argument, not an exemption.

## Consequences

- Any engine component that tracks a fallible actuator's position across
  writes owes the same three answers: which direction is unsafe to guess, which
  record point is safe in each direction, and which residual it accepts. The
  branch is not an optimization to fold away.
- The unified volume owner inherits the invariant, not the variable: whatever
  it calls the level it believes it commanded, the quantity a teardown or a
  refusal reads must be a bound that cannot be too high.
- Deliberately given up: symmetry. A reviewer meeting the branch will read it
  as an inconsistency worth "restoring symmetry" on, which is exactly why the
  reasoning is written down somewhere the code cannot take with it.
- Rejected: tracking a second variable for "where the fader really is". Two
  values that must be kept in sync is the defect the single setter exists to
  close.
