# ADR-0012: The design axis is a MEMBER of the post-apply walk, not just the anchor in front of it

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The post-apply position group combined four off-axis curves. An on-axis capture
was taken at the mark, but it feeds the tracking verdict and never joins the
group — so the round banked no on-axis position record at all. The 2026-08
new-horn campaign had to improvise an extra MEASURE round just to obtain one
on-axis summed response of the graph it had applied.

The owner ruled on 2026-08-24. The ruling and its incident live in one section
comment, `jasper/active_speaker/crossover_v2_flow.py:831-866`. Docs record the
*outcome* — "anchor plus the four side poses" — at
`docs/HANDOFF-crossover-measurement-v2.md:2208`,
`docs/two-stage-commission-flow-plan.md:589`, and
`docs/testing-tooling.md:2256`; none records the incident or the argument.

This ADR extracts it before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7).

## Decision

**The on-axis pose is a member of the group, and an anchor does not stand in
for one.** Quoted from `crossover_v2_flow.py:833-840`:

> **The design axis is a MEMBER of this walk, not just the anchor in front of
> it** (owner ruling, 2026-08-24). VERIFY's anchor is measured at the mark, but
> its summed capture is consumed by the TRACKING verdict and never joins the
> group … so before this table the post-apply group combined four off-axis
> curves and banked no on-axis position record at all. The 2026-08 new-horn
> campaign paid for that directly: it had to improvise an extra minimal MEASURE
> round just to get one on-axis summed response of the graph it had applied.

**A capture consumed by one verdict is not evidence for another.** From
`crossover_v2_flow.py:842-845`:

> an anchor's evidence answers a different question, so a design-axis sample in
> the SIDES' own fidelity class is a curve the group can actually put beside
> them.

**What it cost, stated rather than hidden.** The fifth pose — 12 cm above,
the journey's only above/below-mark-height sample — was given up for it:
*"That ruling spent this capture on shortening the session; this one spends the
same capture on the one place the household sits."* No claim reads the vertical
axis on its own, and the design axis is where every chart's reference is drawn.

**Vertical-free by construction.** The table is derived from the same shared
side offsets the lateral walk uses, *"and vertical-free BY CONSTRUCTION — which
is what lets ``remote_cloud_verify_positions`` stop clamping and lets an
external positioner walk the whole thing."* A pose set that stays in one plane
is what an arm can walk unclamped.

**An exemption is argued, not inherited.** The at-mark row bypasses the minimum
offset check for a mechanical reason (a 0 cm move cannot clear it) but *"NOT
for the same purpose, so the exemption is argued rather than inherited."* The
minimum-offset floor guarantees a prompted move decorrelates HF nulls, and it
is *"a property of the GROUP, not of each row"*.

## Consequences

- Every round banks an on-axis position record in the same fidelity class as
  its off-axis members. A campaign asking "what does the applied graph measure
  on axis?" reads it instead of improvising a round.
- The engine's preset vocabulary inherits the vertical-free property as the
  thing that makes a pose set arm-walkable. A preset that reintroduces a
  vertical pose costs the arm front end its unclamped walk.
- Group-level guarantees (offset floors, spread) are properties of the group.
  Checking them per row and exempting the odd row one at a time is how the
  guarantee quietly stops holding.
- Deliberately given up: the only above/below-mark-height sample in the
  journey, and with it any future claim that reads the vertical axis on its
  own.
