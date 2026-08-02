# Adversarial review gate (mandatory for every wave PR)

Every wave PR must pass an **independent adversarial review** before
merge. Independent means a fresh context: a new Codex session (or a
separate review subagent) that did not write the code, given ONLY
this file, the canonical prompt it points to, and the branch — never
the implementing session reviewing its own reasoning.

**Loop:** implement → acceptance commands green → run this review in
a fresh context → implementer fixes **every Blocker and Should-fix
finding** (Nits at the implementer's judgment, noted in the PR) →
re-run the review → repeat until a clean pass (zero Blocker, zero
Should-fix) → merge. Paste the final review verdict into the PR as a
comment.

Scope note for the reviewing session: where the canonical prompt
(linked below) says "all the work you just did in this session," read:
**the wave branch's full diff against `origin/main`, plus any
uncommitted or untracked files** — the prompt's own instructions for
building the changed-file set handle this. The wave's prompt file
(`wave-N-*.md`) and the charter (`README.md` in this directory) are
part of the repo context the reviewer should read: a violation of
the wave's file allowlist or anti-overengineering fences is at least
a Should-fix.

## The review prompt

Use
[`.claude/commands/adversarial-review.md`](../../.claude/commands/adversarial-review.md)
verbatim — it is the canonical JTS review-gate prompt (the two
invariants, Method, the product-grade lens, severity taxonomy,
JTS-specific checklist, docs checks, and final response format). Do
not re-embed a copy here: a second copy going stale under the
canonical file is exactly the single-source-of-truth violation this
review process exists to catch (#1948, which retired the copy that
used to live in this section). Give the reviewing session that file
plus this one.
