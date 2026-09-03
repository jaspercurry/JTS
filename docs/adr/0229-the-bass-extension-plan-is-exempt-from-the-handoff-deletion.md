# ADR-0229: The bass-extension plan is exempt from the HANDOFF deletion

- **Date:** 2026-09-03
- **Status:** Accepted

## Context

[ADR-0199](0199-the-handoff-doc-corpus-is-deleted.md) deleted the
`docs/HANDOFF-*.md` corpus — 56 files describing subsystems as they were when
last hand-edited, never re-verified — and closed off archiving as a resolution.
`docs/HANDOFF-bass-extension-plan.md` and `docs/bass-extension-waves/**` (18
files, 8,239 lines) survived that deletion, and three places record their
standing: `docs/README.md` calls the plan *"the parked plan and authorization
source under ADR-0018"*, `docs/tuning-master-plan.md` lists it as *"standing,
compatible (no action)"*, and the tuning refactor's wave 7f fenced
`bass-extension-waves/` off the maintenance rules as a frozen primary source.

None of those is a ruling that the files stay, and the third is going: the wave
7f fence lives in `docs/REFACTOR-TUNING-2026-08.md`, which is retired in a
sibling change. What is left is two descriptive mentions against a named
deletion mandate that both ADR-0199 and AGENTS.md's Docs default state, over
files that still match the pattern ADR-0199 names. That reads to the next doc
sweep as a missed cut.

They are not that tier. They are the **active** plan for the bass-extension
program — the architecture, the wave breakdown, the frozen limiter-evidence
protocol revisions, and the hardware prerequisites a resumption inherits.
[ADR-0018](0018-bass-extension-stays-parked.md) parked the package by owner
ruling and named *"the wire-up spec, the frozen limiter protocol revision, and
the hardware prerequisites"* as what makes it worth more than its lines. The
first of those lives in #1738 and `tuning-master-plan.md` ticket 4.4; the other
two live here and nowhere else.

## Decision

**Owner ruling: `docs/HANDOFF-bass-extension-plan.md` and
`docs/bass-extension-waves/**` stay.** They are live plan work, not the retired
handoff tier, and they are kept as written — not renamed out of the `HANDOFF-`
prefix, not moved to `docs/historical/`, not trimmed.

**ADR-0199 is amended, not superseded, on this one point.** Everything else it
decided holds: the corpus is deleted and not archived, no HANDOFF tier exists in
the memory hierarchy, and no new handoff doc gets written. What changes is that
its scope has one enumerated exception, named here rather than left as an
absence.

A deletion mandate, an orphan sweep, a doc-count target, or an audit finding is
**not** sufficient authority to remove these files. Changing this needs a fresh
owner ruling, recorded as an ADR superseding this one — the same bar
[ADR-0018](0018-bass-extension-stays-parked.md) sets for the package itself.

AGENTS.md's Docs default carries a one-clause pointer here, because that bullet
is where the deletion mandate is stated. Its Memory section's HANDOFF sentence
is deliberately left alone: it says a subsystem fact is never trusted from a
handoff, which is true of these files too — they are a plan of record, not a
description of HEAD, and reading them for current behaviour is still wrong.

## Consequences

- 8,239 lines of planning prose stay in the tree for a program that is parked,
  which is the same cost ADR-0018 already accepted for the package's ~20K lines
  and for the same reason: the design context is what makes resumption cheaper
  than a rewrite, and git history does not carry it usefully.
- **Discovery is one-way.** ADR-0199 stays immutable and `Accepted` with no
  forward pointer, so a reader arriving there directly — a grep for ADR-0199, an
  audit citing it as deletion authority — sees the unqualified mandate. The two
  paths to this exception are AGENTS.md's Docs default and this file. That is
  accepted deliberately: editing ADR-0199 breaks the directory's immutability
  rule, and superseding it would reopen a corpus the owner closed for good.
- The rename that would end the confusion — dropping the `HANDOFF-` prefix — is
  deliberately **not** taken here. Renaming touches every referrer
  (`docs/doc-map.toml`, `docs/README.md`, `tuning-master-plan.md`, the wave
  docs' cross-links); it is a separate change, and the owner ruled the files
  stay as they are. This ADR is the cheaper fix for the actual risk, which is
  deletion, not the prefix.
- Rejected: superseding ADR-0199. Its decision is right and still governs the
  other 56 files; an exception is not a reversal.
