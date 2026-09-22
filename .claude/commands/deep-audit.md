---
description: |
  Run the heavyweight, whole-codebase pre-launch audit — comb the entire
  repo close to line by line with many sub-agents to find dead code, stale
  docs, drift, duplication, unjustified complexity, and the unknown unknowns
  (orphans, dead flags, abandoned corners). The capital-T-truth audit, NOT a
  per-diff review. Use when the user says any variant of: "deep audit", "final
  audit", "audit the whole codebase", "make sure everything earns its keep",
  "comb the codebase", "find dead/stale code", "I'm done shipping, let's do
  the big audit". For reviewing a single branch/PR use /code-review ultra
  instead — this combs the whole tree.
---

# Deep Audit — whole-codebase comb for the capital-T truth

Execute the **Deep Audit Playbook**:
[`docs/DEEP-AUDIT-PLAYBOOK.md`](../../docs/DEEP-AUDIT-PLAYBOOK.md). **Read that
file first** — it is the canonical method (five phases, the per-file rubric,
the discovery query catalog, the workflow skeleton). This command is the
trigger; the playbook is the substance. Do not improvise a shallower version.

Inputs: the checkout's absolute path, audited SHA, scope, and token budget /
agent scale. Use the playbook's kickoff prompt and workflow skeleton.
