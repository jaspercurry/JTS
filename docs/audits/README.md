# Audit archive

A report in this directory is a frozen snapshot: findings pinned to the
audited SHA and the re-verification date, never edited to reflect later
work. The ledger of open findings lives in GitHub issues labelled `audit`
plus one `audit-<date>` label per run, tracked on that run's tracking
issue. Evidence trails (per-agent reports, tarballs, diffs) go on the
tracking issue, or a release asset pinned to the audited SHA (ADR-0284) —
never committed to this directory.

A report may carry at most one dated landing note as its final appendix,
written once when the audit's lane closes and never refreshed. It is not a
status field: it names PRs and measured figures at one date, and points at
the tracking issue for anything current.

| date | audited SHA | scope | report | tracking issue | evidence | status |
|---|---|---|---|---|---|---|
| 2026-08-25 | `9fcda9ee5` | whole-repo deep audit, ~79 subagents | [2026-08-25-deep-audit.md](2026-08-25-deep-audit.md) | — | — | superseded |
| 2026-09-05 | `2d571e6b8` | whole-repo quality review, ~65 subagents | [2026-09-05-codebase-quality-review.md](2026-09-05-codebase-quality-review.md) | #4786–#4812 (15 individual rows + 12 theme umbrellas) | `docs/codebase-quality-review-2026-09-05/`, deleted in a follow-up PR | superseded |
| 2026-09-05 (voice) | `8777cff19` | voice loop (wake→turn→TTS) audit | ledger deleted in #4784 | #4777–#4783 (its open rows) | not committed | superseded |
| 2026-09-09 | `53a883808` | whole-repo deep audit, 114 agents | [2026-09-09-deep-audit.md](2026-09-09-deep-audit.md) | #4775 | release tag [`audit-evidence-2026-09-09`](https://github.com/jaspercurry/JTS/releases/tag/audit-evidence-2026-09-09) | current baseline |

## How to run the next one

1. Run [docs/DEEP-AUDIT-PLAYBOOK.md](../DEEP-AUDIT-PLAYBOOK.md) (`/deep-audit`).
2. Land one report file in this directory, named `<date>-deep-audit.md` or
   `<date>-<scope>-review.md`.
3. File one GitHub issue per surviving finding, labelled `audit` and
   `audit-<date>`. Do not add a status column to the report itself.
4. Attach the run's evidence tarball to the tracking issue, not to git.
5. Add a row to the table above and retire the prior baseline's status to
   `superseded`.
