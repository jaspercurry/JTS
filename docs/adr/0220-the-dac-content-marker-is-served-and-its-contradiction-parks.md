# ADR-0220: The dac-content marker is served, and its contradiction parks

Date: 2026-09-02
Status: Accepted
Refs: #3656, #3118

## Context

`grouped_dac_content_lane` (ADR-0178) parked every bonded member because the
round-trip lane had no producer. It has one now: `jasper-grouping-reconcile`
points a dumb member's snapclient at `jts_ring_dac_content` and arms
`JASPER_OUTPUTD_DAC_CONTENT_LANE`, and outputd reads that ring as the box's
sole content source.

## Decision

**The MARKER half of `grouped_dac_content_lane` is SERVED and no longer parks**,
superseding that row of ADR-0178. Only the legacy raw-PCM FIFO spelling still
parks: it needs a content bridge no writer emits.

**The return ring takes the box's ring slot** — `RING_SLOT_FRAMES` (128), like
every other ring here (#3659). One slot is one outputd period.

**The arming decision is one function.** `member_lane_decision` answers it for
the env writer, the reconciler's bond refusal and the doctor, over four
conditions: an active-member config, not an ACTIVE endpoint, a topology that
permits a flat final-output graph, and an outputd period the ring's slot can
carry. Consequence accepted: the floorless HiFiBerry DAC8x Studio runs the
packaged 1024, so it cannot bond as a dumb member; it refuses under its own
reason token and stays solo rather than parking outputd.

**The marker beside a declared content bridge parks under its own name,**
`dac_content_marker_beside_bridge`, mirroring outputd's own refusal (`EX_CONFIG`
made permanent by `RestartPreventExitStatus=78` — silent, every unit green).
Reachable rather than theoretical: `jasper-fanin-coupling-auto` writes
`JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring` into `outputd.env` on every pass and
the unit loads the grouping layer after it. So the armed branch writes that key
BLANK (outputd's `env_optional` reads blank as undeclared) and every unarmed
branch omits it (without the marker outputd reads it with `env_str`, whose
blank it parks on).

## Consequences

Multi-room is servable for a passive single-DAC member on a floored DAC. The
control plane mirrors outputd's acceptance in one predicate pair
(`dac_content_ring_served` / `dac_content_marker_contradicted`), so no surface
reports a shape the daemon would refuse. The FIFO spelling, its park trigger and
outputd's FIFO reader retire together after a bonded pair plays on metal.
