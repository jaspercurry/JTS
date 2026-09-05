# Shared brief for JTS quality-review subagents

Checkout: /home/user/JTS at SHA 2d571e6b8 (branch claude/codebase-quality-review-do78rn == origin/main at start).
READ-ONLY: never edit/create/delete files inside /home/user/JTS. Never git checkout/stash/reset/commit.
Write your report ONLY to the scratchpad path named in your task. Scratch scripts also go in the scratchpad dir.

Context: JTS is a Raspberry Pi smart speaker (Python ~426k LOC in jasper/, Rust ~59k in rust/, C in c/,
bash+systemd+nginx in deploy/, laptop tools in scripts/, ~900 pytest files in tests/). One owner, developed by AI
agents at very high velocity (PR numbers > 4100, 157 ADRs in docs/adr/). Read /home/user/JTS/AGENTS.md first —
it is the house standard you audit against (smaller files, no duplicate implementations, delete dead code,
no unrequested JASPER_* knobs, tests pin behavior not prose, comments only for non-derivable constraints,
guards need a removal condition, single-writer env files, push-don't-pull on constrained hardware).

The owner's goal: simpler, better organized, clear boundaries and mandates per system, single source of truth,
DRY, no duplicate systems, no orphaned code, easy to follow and debug, resilient, observable.

Rules:
- Evidence before judgment. Every finding cites file:line or file:function you actually opened. Grep-only claims
  must say so. Docs/comments/PR titles are claims to verify, not facts.
- Before calling anything dead/orphaned, check callers in: jasper/ rust/ c/ deploy/ scripts/ tests/ .github/ docs/;
  pyproject.toml [project.scripts]; systemd Exec*= lines in deploy/systemd/; deploy/install.sh and deploy/lib/install/;
  deploy/bin/; udev rules; nginx confs (deploy/nginx*); importlib / getattr / __import__ / string dispatch;
  registries (jasper/cli/doctor, jasper/tools, jasper/cues/registry.py, jasper/web); JS modules under deploy/assets.
- Reject astronaut engineering as loudly as missing structure. Prefer "delete" and "merge into X" over "add a layer".
- Work already in flight (do not re-report as new; you may note "in flight via PR #NNNN"): issue #4030 rightsizing
  program (prose diet, doctor split, test doubles dedupe, wake_corpus split, TtsPlayout collapse, control/handlers
  seam); issue #4027 / ADR-0235 attached-hardware one-owner-per-fact; issue #4031 web UI cleanup + independent
  subwoofer deletion (ADR-0236); issue #3915 voice reconnect supervisor; tuning-rightsize waves w6..w9
  (active_speaker/audio_measurement CLIs "stdout is the answer", RecordStore).

Output: markdown, terse, tables where they fit, findings ranked by impact, no narration of process. Each finding:
severity (Blocker / Should-fix / Nit / Earns-its-keep), file refs, one-line evidence, one-line cleanest fix.
End with a "Coverage" section: what you opened, what you skipped and why.
