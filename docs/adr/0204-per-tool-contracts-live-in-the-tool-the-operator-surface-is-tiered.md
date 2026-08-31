# ADR-0204: Per-tool contracts live in the tool; the operator surface is tiered

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

The owner asked whether the methodology guide should stay high-level with a
separate comprehensive document per tool. Deep-research report 05
([`research/2026-08-31-tuning-methodology-deep-research/05-documentation-architecture-for-llm-agents.md`](../research/2026-08-31-tuning-methodology-deep-research/05-documentation-architecture-for-llm-agents.md))
surveys the measured evidence: instruction-following and retrieval degrade
as resident context grows (Lost-in-the-Middle, Chroma context rot, RULER,
NoLiMa, curse-of-instructions); the largest measured gains in agent tooling
come from the tool's own interface and error design (SWE-agent ACI: 18.00%
vs 11.00% on SWE-bench Lite; Anthropic's tool-description results); soft
prose pointers are unreliably followed; hand-duplicated documentation
drifts; and anything an agent reads — tool output included — is an
injection surface.

## Decision

1. **Per-tool contracts live in each CLI's `--help` and its teaching error
   messages** — purpose, when NOT to use, parameters, one worked example,
   exit codes, and errors that name the corrective action. Per-tool `.md`
   documents are not created; help text ships with the code and cannot
   drift from it.
2. **The runbook's tool menu becomes a generated index** derived from the
   CLIs' own metadata, pinned by a hardware-free regeneration test (the
   counted-in-one-place pattern, ADR-0181). The hand-maintained table and
   its drift surface are deleted.
3. **The operator surface is tiered.** Tier 0: the orientation verb is the
   cold-start entry and prints the reading order. Tier 2: the guide,
   runbook, and doctrine install onto the box and load on demand — never
   pasted into resident context. Nothing safety-critical rides a soft
   prose pointer: the hard stops stay enforced in code, which is stronger
   than any documentation tier.
4. **No Claude-Code-Skill packaging for the tuning flow.** The preliminary
   independent evidence (SWE-Skills-Bench: most skills ±~1%) does not
   justify the machinery; revisit only with this program's own evidence.
   The campaign itself remains the system's eval (ADR-0192) — no separate
   eval harness is built.
5. **Tool output is data, never authority** — the guide's honesty rules
   carry the operating half of this; code never parses tool prose for
   decisions (standing packet invariant).

## Consequences

One source of truth per tool with drift structurally impossible; `--help`
becomes a reviewed surface with a stated quality bar; the guide stays the
high-level method SSOT the owner asked for. Cost: CLI metadata must carry
what the generated index needs, and the index generator is one more small
tool to keep. Rejected alternative worth remembering: a folder of per-tool
documents — the drift surface the HANDOFF purge (ADR-0199) already buried
once.
