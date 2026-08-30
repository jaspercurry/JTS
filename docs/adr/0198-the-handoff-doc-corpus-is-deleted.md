# ADR-0198: The HANDOFF doc corpus is deleted

- **Date:** 2026-08-30
- **Status:** Accepted

## Context

`docs/HANDOFF-*.md` had grown to 56 files (~30,700 lines), plus one copy
(`HANDOFF-correction-revision-plan.md`, 1,899 lines) already renamed into
`docs/historical/` rather than deleted. Deep-audit findings recorded in
`docs/DEEP-AUDIT-2026-08-25.md` named this corpus, and AGENTS.md's own prose
restating it, as the single largest cuttable mass in the repo. Every one of
these docs described a subsystem's state at the moment it was last hand-edited
— never re-verified against the code it claimed to describe — and agents kept
citing them as if they were current. AGENTS.md already tightened this once
("do not create or grow HANDOFF docs unless the owner asks"); the corpus kept
existing anyway.

## Decision

**The corpus is deleted, not archived.** All 56 `docs/HANDOFF-*.md` files and
the one copy renamed into `docs/historical/` are removed in the same PR that
records this ADR. Every referrer — AGENTS.md's Map, README's doc index, other
docs' links, code comments, docstrings, log/CLI/HTML strings, and
`docs/doc-map.toml`'s routing entries — is trued in the same change: either
the citation is dropped (no replacement doc exists) or, where a subsystem's
entire mapped doc set was HANDOFF-only, it now routes to `AGENTS.md`.

What agents must remember now lives in the hierarchy AGENTS.md's Memory
section already states: AGENTS.md itself (operational), `docs/adr/`
(decisions and their why), and git history plus PR descriptions (what changed
and when). A HANDOFF tier no longer exists in that list. A fact that isn't
covered by those three sources gets re-derived at HEAD by reading the code —
never trusted from a doc that may have drifted since it was written.

**Resurrect condition: none.** Git history is the archive. Any specific
HANDOFF doc's last text is still reachable by path in history; that is
sufficient, and no process should treat resurrecting one as a live document
as a valid resolution to a future gap.

Two exclusions, both deliberate: `docs/adr/**` stays untouched (append-only —
an old ADR's citation to a since-deleted HANDOFF doc is itself a historical
fact, not a live pointer), and archival/point-in-time material (`docs/
historical/**`, `docs/research/**`, dated `REVIEW-*`/`DEEP-AUDIT-*` snapshots,
and directories another program has explicitly frozen) is left as-written for
the same reason — rewriting a snapshot's citations after the fact is
revisionism, not truing.

## Consequences

Subsystem detail that used to have a dedicated write-up no longer does. A
reader who wants "how does AEC commissioning work" now reads the code,
`docs/doc-map.toml`'s routing hints, and the relevant ADRs, rather than a
prose document that claimed to already have the answer. That is the intended
trade: a doc that is sometimes stale is worse than no doc, because it is
trusted the same either way.

`docs/doc-map.toml` is smaller and every subsystem's `docs` list is either
still doc-specific or falls back to `AGENTS.md`; `scripts/docs-impact.py
--validate-only` enforces that every listed doc exists, so this cannot rot
silently the way the deleted corpus did.

Rejected: archiving instead of deleting (moving the corpus into
`docs/historical/` the way one copy already was). That is exactly the move
this ADR closes off — it was tried once, the file kept getting cited as
current anyway, and "archived" docs still show up in greps and still tempt a
future agent to trust them over the code.
