# ADR-0298: Tiers and stages retire; the program registry is the only pose vocabulary

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

The tuning surface described one experiment through overlapping tier, stage,
program, and layout terms. Tier arithmetic also restated pose counts already
owned by the program registry. A post-apply stage named chronology instead of
the measurement question.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.1–2.2 and evidence comments 2, 4, and 11.


## Decision

Tiers and stages retire from requests, CLI arguments, durable state, status,
and evidence. A plan selects a program and either names poses directly or
selects a layout from the program registry. The registry is the only owner of
named layouts, pose geometry, default sizes, and program purpose.

Measure and trial are plan shapes of the same `run` verb. An empty candidate
list measures the program's named baseline. A non-empty candidate list trials
those candidates. Measuring the applied result after an apply is another run,
not a `post_apply` stage.

ADR-0277 remains the authority for saved layout identities and the seat-cloud
default. ADR-0278 remains the authority that purpose, stimulus, screen copy,
geometry, and mover reach are independent. Mover reach is checked per resolved
pose and does not create a mover-specific layout vocabulary.

Existing durable records that contain `tier` are migrated by their state
owner: the field is read only to preserve the old record's meaning, then is
omitted from the new shape. No default tier is inferred and no stored round is
relabelled. This is the migration case contrasted in ADR-0011.

## Consequences

Operators state the question once. Pose counts cannot drift between tier
tables and the registry. The loss of shorthand is deliberate; layouts provide
the useful shorthand without creating a second vocabulary.

Old tier and stage flags refuse as unsupported rather than mapping loosely to
new plans. Historical evidence keeps its recorded identity.

## Supersedes / Amends

This ADR does not supersede ADR-0277 or ADR-0278; it makes them the sole pose
authority. It records how ADR-0011 governs removal of the durable `tier` fact.
