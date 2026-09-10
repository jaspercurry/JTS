# ADR-0284: Audits are frozen reports; issues are the ledger

- **Date:** 2026-09-10
- **Status:** Accepted

## Context

Three audit generations have accumulated in `docs/` with no shared shape.
`docs/DEEP-AUDIT-2026-08-25.md` (207 lines) is a flat snapshot.
`docs/CODEBASE-QUALITY-REVIEW-2026-09-05.md` (685 lines) plus its evidence
directory `docs/codebase-quality-review-2026-09-05/` (79 files, 16,911 lines,
including a live `register.csv`) tracks open/closed status inside the
snapshot itself, so the directory keeps getting re-opened and re-edited after
the audit that produced it is long past. `docs/VOICE-AUDIT-2026-09-05.md` plus
`docs/voice-audit-2026-09-05/` (1,810 lines total) does the same. Between
them, 148 distinct issue references are scattered across prose rather than
tracked as issues. The 2026-09-09 deep audit (54 agents, per MEMORY.md) never
landed a `docs/` file at all — its only record lived in a session scratchpad.
No two of these four runs are discoverable, supersedable, or closeable the
same way.

## Decision

1. **One frozen report per audit run**, at `docs/audits/YYYY-MM-DD-<scope>.md`,
   naming the SHA it audited. It is written once and never edited afterward —
   a later audit that revisits the same ground is a new file, not a patch to
   this one. It records what was true at that SHA; it carries no plan and no
   live status field.
2. **The live ledger is GitHub issues.** Each finding an audit wants tracked
   becomes an issue labelled `audit` and `audit-<date>` (owner-approved
   findings additionally get `owner-decision`). A finding fixed in a PR closes
   its issue the normal way. Nothing in `docs/` tracks open/closed state.
3. **Evidence trails are not committed.** Tile reports, prompts, and agent
   transcripts are attached as a tarball to the audit's tracking issue.
4. **`docs/audits/README.md` is the index**: one row per run — date, SHA,
   scope, report link, tracking issue, status.
5. **The method stays in `docs/DEEP-AUDIT-PLAYBOOK.md`**, unchanged by this
   ADR — it governs how an audit is run, not how its output is kept.

## Consequences

- `docs/codebase-quality-review-2026-09-05/` and `docs/VOICE-AUDIT-2026-09-05.md`
  plus its evidence directory are deleted once their still-open rows are filed
  as `audit`-labelled issues; that filing is separate follow-up work, not part
  of this ADR. `docs/DEEP-AUDIT-2026-08-25.md` already reads as a frozen
  snapshot under this shape and can stay or move under `docs/audits/` as a
  mechanical rename.
- The next audit writes exactly one file under `docs/audits/`, opens its
  findings as issues, and does not create a second snapshot directory with its
  own status tracking. A finding still open when the report is written stays
  open in the issue, not in a re-edit of the report.
- **Non-goal:** this is not a handoff tier. [ADR-0199](0199-the-handoff-doc-corpus-is-deleted.md)
  stands — no subsystem fact is trusted from an audit report instead of HEAD.
  A frozen report records what was observed at a SHA; it is read the way any
  other dated snapshot is read (`docs/historical/`, `DEEP-AUDIT-2026-08-25.md`
  today), never as a live plan the way [ADR-0229](0229-the-bass-extension-plan-is-exempt-from-the-handoff-deletion.md)'s
  exempted bass-extension plan is.
- Rejected: keeping status inside the snapshot file (the 09-05 pattern) —
  it is what made two directories need re-opening after the fact. Rejected:
  committing evidence trails — 17K+ lines of per-tile transcript is exactly
  the mass ADR-0199 already ruled against keeping in the tree.
