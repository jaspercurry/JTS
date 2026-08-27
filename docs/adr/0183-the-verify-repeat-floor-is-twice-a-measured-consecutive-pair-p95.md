# ADR-0183: The VERIFY repeat floor is twice a measured consecutive-pair p95

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`VERIFY_REPEAT_FLOOR_DB` (0.2) is
[#1873](https://github.com/jaspercurry/JTS/issues/1873)'s repeatability
discriminator: how close two consecutive graded VERIFY attempts have to land
before the mismatch between them is called DETERMINISTIC rather than transient.
It looks like a chosen tolerance. It is a measured number, and the measurement
that produced it — plus the reason this file hardcodes a value another module
computes — lives only in the comment above the constant
(`jasper/active_speaker/crossover_v2_flow.py:785-847`). The refactor
(`docs/REFACTOR-CUTOVER-2026-08.md` §6.1) moves that block, so the derivation
is extracted first. No code moves here; the comment shrinks to `See ADR-0183`
plus its maintainer trap in the PR that moves its row (§6.1's 7305–7924).

## Decision

**The derivation, quoted verbatim from `crossover_v2_flow.py:789-797`:**

> MEASURED, not chosen. Panel course-correction P0 (`captures/
> repeat-floor-20260731/README.md`) repeated the shipped stage-2 VERIFY
> instrument back-to-back with the microphone bolted in place, through the real
> deconvolution / gating / smoothing / grading path, and reported the repeat
> floor of THIS EXACT metric — ``max_db_notch_excluded`` over 1000-4000 Hz — as
> 0.052 dB median / 0.085 dB p95 between consecutive measurements. Its own table
> then states the honest per-attempt claim threshold for that metric as
> **0.2 dB, twice the consecutive-pair p95**. That number is what is used here;
> this module derives nothing.

**The second spelling, and why it is one rather than a second threshold.**
Verbatim from `crossover_v2_flow.py:799-809`:

> **The rule already has an owner, and this is its second spelling — so it says
> so.** :data:`~jasper.active_speaker.attempts_loop.CLAIM_FLOOR_P95_MULTIPLE`
> (2.0) owns "an honest per-attempt claim floor is twice the observed
> consecutive-pair p95", and its own comment records that the p95 over the 13
> accepted pairs is 0.08508 dB — so the rule computes 0.17016 dB, and the
> README's 0.2 is that same rule at conservative display rounding, not a second
> threshold. The kernel there COMPUTES the floor from a banked repeat study;
> this constant HARDCODES the rounded value instead, because a live VERIFY
> sitting has no such bank to read — it holds two attempts of its own and
> nothing else, and importing a kernel that needs a study it cannot supply would
> buy a dependency rather than a number.

**One rule, two spellings, and the duplication is deliberate and bounded.**
`CLAIM_FLOOR_P95_MULTIPLE` is the rule; `VERIFY_REPEAT_FLOOR_DB` is that rule
evaluated once, offline, and frozen — because the site that needs it has no
study to evaluate it against. The removal condition is written into the
comment and is the one that governs: *"If that kernel ever gains a VERIFY-time
source for its floor, this constant is what should go — replaced by the
computed value, not hand-edited toward it."*

**The comparison is consecutive, never against a fixed baseline** — the
measurement's own finding 1: against a fixed early baseline the floor walks
with drift (+0.0046 dB/repeat, r = +0.81, ~0.07 dB over 15 repeats); against
the predecessor it is flat (−0.0021 dB/repeat). So the stored value is
refreshed on every graded attempt. That is deliberately the opposite of G3's
frozen baseline
([ADR-0182](0182-the-verify-pilot-transfer-ceiling-rests-on-one-clean-session.md)):
G3 asks whether the recording chain has moved SINCE the sitting began; this
asks whether the speaker gives the same answer TWICE. Different questions,
different baselines.

**It is a fixed-mic floor, and that is the safe direction.** The study's
microphone was never moved and never unplugged. The mic-replacement arm —
remove, replace, re-aim — is unmeasured and is the dominant term in the panel's
3.2 dB cross-session bound. A household can nudge a phone between in-session
attempts, so the true same-sitting floor is at or above 0.2 dB, and using the
tightest measured value makes this discriminator HARDER to trigger: it
under-claims determinism, costing one retry that was not needed, where
over-claiming would remove a retry that could have helped.

**What stays a comment, not an ADR.** The maintainer trap at
`crossover_v2_flow.py:822-827` — that "tightening" 0.2 toward 0.17016 moves
*away* from safe, because a bigger floor is a wider agreement window — is a
non-derivable trap about the direction the code reads, and it belongs beside
the number. This ADR records the derivation; the trap rides the constant.

## Consequences

- The number is auditable rather than traditional. Changing it means
  re-measuring the repeat floor of `max_db_notch_excluded` over 1000–4000 Hz
  through the shipped path, not arguing about tolerances.
- The 17.5 % gap between 0.2 and the kernel's 0.17016 is understood and
  accepted in the direction it actually cuts: 0.2 declares determinism slightly
  more readily than the bench rule would, and the fixed-mic caveat is what pays
  for that.
- Two spellings of one rule remain in the tree, with a written removal
  condition. A third is not allowed; a reader who needs the rule cites
  `CLAIM_FLOOR_P95_MULTIPLE`, and a reader who needs the frozen value cites
  this ADR.
- Deliberately given up: computing the floor at VERIFY time. It would be more
  honest per sitting and it is not available — a live sitting holds two
  attempts and no study — so the dependency would buy nothing the frozen number
  does not already give.
