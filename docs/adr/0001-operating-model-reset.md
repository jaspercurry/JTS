# ADR-0001: Operating-model reset — hobbyist proportionality replaces production doctrine

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The 2026-08-25 whole-codebase deep audit
([docs/DEEP-AUDIT-2026-08-25.md](../DEEP-AUDIT-2026-08-25.md), SHA `9fcda9e`)
graded the repo **B− engineering quality / C− proportionality**: 1.42M tracked
lines for a solo-owner hobbyist speaker, a test suite larger than the product
(617K vs ~490K lines), a 0.50 comment-to-code ratio inside `jasper/`, 65
HANDOFF docs (73K lines), and a 3,534-line AGENTS.md whose unconditional rules
("every promise gets a test", "codify everything", adversarial review to zero
findings on every PR) compounded — through stateless agents with no cost signal
and no deletion mandate — into process-as-code artifacts (e.g.
`test_lint_contracts.py`: 2,159 lines, 88% comments, a ratchet narrated upward
instead of enforced).

A follow-up deep-research pass (2026-08-26, owner-run) supplied external
evidence: repository context files can *reduce* agent task success while
raising cost ~20% (Gloaguen et al., arXiv:2602.11988); instruction-following
collapses as rules stack (Sonnet follow-rate 0.96 → 0.45 from 1 to 20 stacked
instructions, arXiv:2608.02639; IFScale, arXiv:2507.11538); **incorrect**
comments actively harm LLM code reasoning (−23% avg, CodeCrash, NeurIPS 2025;
Macke & Doyle, NAACL 2024) while missing comments barely matter; mutation
score, not test count, signals suite quality (Google, ICSE 2021); and defenses
themselves are a failure source (Cook, *How Complex Systems Fail*; the Knight
Capital stale-guard incident). Practitioner convergence: instruction files
under ~200 lines, memory in append-only decision records + git history, policy
enforced mechanically rather than in prose.

The owner's 2026-08-25 right-sizing directive (PRs #2940/#2941) was the
interim patch; layering an override on 3,500 contradicting lines is itself an
instruction-conflict hazard, so the full replacement follows.

## Decision

1. **AGENTS.md becomes a ~200-line navigational charter**: identity
   (hobbyist, solo, pre-production), a closed non-negotiables list (hearing
   clamps, brick hazards, secrets, deploy integrity, paid tests), concrete
   proportionality defaults, a map, build/deploy commands, tiered review
   policy. Nothing in it restates what another file owns. The old doctrine
   text lives in git history only.
2. **Memory architecture:** decisions and their "why" live in `docs/adr/`
   (append-only, immutable, superseded-not-edited) and in commit messages.
   Inline code prose is not a memory store. HANDOFF docs are legacy deep
   references: kept, trimmed over time, not grown, no new ones without an
   owner request.
3. **Comment policy:** constraints and why-pointers only; no narration,
   history, dates, or PR numbers in code; unverifiable comments are deleted
   rather than preserved (wrong comments mislead agents; missing ones don't).
4. **Test policy:** one altitude per externally observable behavior; no
   assertions on source text or log/error prose; property/parametrized tests
   over example clusters; heavy testing (and, later, mutation-score gating)
   reserved for the non-negotiable paths.
5. **Guard triage:** hypotheticals get nothing; real incidents get
   fix-forward + observability; permanent machinery requires a
   non-negotiable tie or a recurrence, and every guard carries a removal
   condition or expiry.
6. **Review tiering:** built-in `/code-review` (medium) as the default with
   owner triage and no zero-findings bar; the whittled `/adversarial-review`
   only for diffs touching the non-negotiables; `/simplify` as an occasional
   cleanup pass. The standing multi-agent conductor/gate ceremony is retired
   for ordinary work.
7. **Deletion mandate:** agents leave touched files smaller unless the
   feature grew, and delete verified-dead code in scope.

## Consequences

- Every future session inherits proportionality as concrete defaults instead
  of mood language; token and wall-clock cost per change drops (no mandatory
  gate rounds, no disposition ceremony, no doc-impact ritual).
- Superseded: the "standing multi-agent method" (conductor rule, mandatory
  0/0 adversarial gate, handoff-restatement rule), the "documentation
  paradigm" as a per-PR obligation (touched-subsystem scans, atlas
  completeness, freshness footers as CI), universal "pin promises with
  tests" / "codify, don't memorise" rules, and the COAH bar as a per-change
  gate. The underlying *values* (observability, resilience where earned,
  single source of truth) survive as the charter's defaults and
  non-negotiables.
- Deliberately given up: some defect-catch rate on non-critical paths
  (accepted — deploys are cheap, blast radius outside the clamps is small);
  the guarantee that every documented behavior has a pinning test.
- Known risks, accepted with mitigations: ADR-beats-living-docs for agent
  consumption is extrapolation, not a measured result (mitigation: keep
  HANDOFFs until the ADR set proves out); comment deletion could remove a
  load-bearing "why" (mitigation: restore as an ADR link, not inline prose);
  ratchets can still be narrated upward (mitigation: budget increases require
  an ADR).
- The deletion refactor itself (audit §6 waves) proceeds under these rules;
  the tuning-stack half is owned by the tuning program's own plan.
