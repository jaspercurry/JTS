# ADR-NNNN: Short decision title

- **Date:** YYYY-MM-DD
- **Status:** Accepted | Superseded by ADR-NNNN

For a single ruling, three flat bullets:

- **Context:** What situation forced a decision? Two to six sentences.
  Link evidence (audit findings, incidents, measurements) — don't restate it.
- **Decision:** What we decided, stated as the rule/shape that now holds.
- **Consequences:** What this makes easier, what it makes harder, what it
  deliberately gives up. Include rejected alternatives worth remembering.

For a multi-part decision, `## Context` / `## Decision` / `## Consequences`
headed sections instead, each free to run multi-paragraph or numbered.

Rules for this directory: one decision per file; files are immutable — a
change of mind is a new ADR that names what it supersedes; number files
sequentially (`NNNN-slug.md`). Code points here with `# See ADR-NNNN`.
