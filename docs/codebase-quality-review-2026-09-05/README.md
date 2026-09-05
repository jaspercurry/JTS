# Evidence for the 2026-09-05 codebase quality review

Frozen record behind [`../CODEBASE-QUALITY-REVIEW-2026-09-05.md`](../CODEBASE-QUALITY-REVIEW-2026-09-05.md).
Audited tree `2d571e6b8`. Nothing here is current operating truth; disposition of each finding lives in
the steward issues.

- `register.csv` — one row per distinct finding after dedup: id, severity as reported and as normalized,
  theme, area, `file:line` refs, evidence, fix, LOC estimate, source reports, in-flight PR/issue, owning
  territory, and whether a skeptic re-read it (`verify`).
- `reports/BRIEF*.md` — the briefs every agent was given.
- `reports/research-best-practices.md` — the structure and quality-measurement reference and the
  12-row grading rubric.
- `reports/p0-*.md` — Phase 0 cartography: inventory and complexity, orphans, config ledger,
  duplicate systems, test-suite health, docs drift.
- `reports/p1-T*.md` — Phase 1 tiled reads; the tile file lists are summarized in each report's
  Coverage section.
- `reports/p2-L*.md` — Phase 2 lenses: boundaries and import architecture, resilience, observability,
  secrets and config ownership, Pi performance.
- `reports/p2-S*.md` — Phase 2 scenarios: privileged actions, the audio chain, deploy, wake to
  response, volume paths.
- `reports/p3-*.md` — Phase 3 adversarial verification: the blocker claims, the deletion claims, the
  seam findings. These verdicts override the tile grades.
- `reports/p4-critic.md` — the completeness critic's answer to "what did no one open".
