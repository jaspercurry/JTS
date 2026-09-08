# Session kickoff snippet (copy, fill the two placeholders, paste into a fresh session)

The orchestrating session fills `<LANE>` and `<BRIEF>` per lane and hands the
owner one copy per session to spawn. Everything below the line is the snippet.

---

You are running the **<LANE>** lane of the JTS seat-matched tuning program.

- Tracking issue: https://github.com/jaspercurry/JTS/issues/4502
- Plan of record (fetch the branch, never merge it):
  https://github.com/jaspercurry/JTS/blob/claude/loudspeaker-tuning-architecture-iephfa/seat-tuning-program/PLAN.md
- Your brief:
  https://github.com/jaspercurry/JTS/blob/claude/loudspeaker-tuning-architecture-iephfa/seat-tuning-program/briefs/<BRIEF>.md

**Start.** `git fetch origin main claude/loudspeaker-tuning-architecture-iephfa`.
Read `AGENTS.md` at HEAD, then your brief, then the plan's §2 (principles) and
§7 (protocol), then the ADRs your brief names. Verify every `file:line` in the
brief at your HEAD before acting; if a premise is false, stop that row and say
so in your report rather than forcing it.

**How to work.**

- You are the orchestrator of this lane. Delegate where it makes sense: Sonnet
  agents for citation verification, reading, prose sweeps and test scaffolding;
  Opus agents for implementation and relocations. Keep design decisions, the
  non-negotiable-tier review and the final judgment yourself.
- One concern per PR, from a fresh `origin/main` branch named in the brief.
  Before every push: `scripts/test-fast` (trust only the final
  `==> <lane>: N passed` sentinel), then `/simplify`, then `/code-review`
  medium; fix what is real. Rows marked NN also run `/adversarial-review` and
  hold the merge until the owner confirms a hardware pass.
- Deletions carry a SUPERSEDED, SPENT or PROMOTE verdict with the grep that
  proves it, in the PR body. Shared pieces move before anything is deleted.
  Never two copies of one thing alive across a merge.
- What we are building toward: simple, elegant, modular; 80/20; one owner per
  concern; single source of truth; clear boundaries (the package-boundary tests
  are the contract); observability where a fact matters (a log event, `/state`,
  doctor); reliability over cleverness. Leave every file you touch smaller than
  you found it unless the feature genuinely grew. Do not blow up the codebase:
  add rows at the engine's extension points (programs, views, candidate kinds,
  emitter stages), never a new framework, daemon, database, knob or doc tier.
- Fetch again before pushing; `git push -u`; confirm the remote ref advanced.
  No model identifiers in commits or PRs. PR body: line delta, verdicts,
  validation sentinels, and a "stale, not fixed here" list.
- Report back in the shape your brief's last section asks for; the
  orchestrating session reviews the merged diff against the row before the
  next brief.
