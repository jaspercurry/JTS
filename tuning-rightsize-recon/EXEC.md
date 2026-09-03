# Execution brief — tuning right-sizing, Phase 1 (read fully before touching anything)

Repo: /home/user/JTS (you are in your own git worktree of it). Read AGENTS.md
first. Plan context: scratchpad/PLAN.md §3 (phases) and §7 (bottom-up check);
recon evidence in scratchpad/recon/ and scratchpad/bottomup/.

## Branch and PR mechanics
- Temp files: the scratchpad is SHARED by every agent and files get clobbered.
  Put logs, commit messages and scratch scripts in `mktemp -d` (or your own
  worktree's untracked dir), never at the scratchpad root.
- Load: the container runs many agents at once. Do not run test-fast's
  changed-file lane; run `ruff check` on touched files and
  `python3 -m pytest <touched modules' tests> -q -p no:cacheprovider`; state in
  the PR that CI is the authority for the full lane.
- `git fetch origin main` then `git checkout -b claude/tuning-rightsize/<slug> origin/main`
  (slug given in your task). Never commit to any other branch. Never push to main.
- One concern per PR. If your task lists two concerns, make two branches/PRs.
- Before pushing: `bash scripts/test-fast` from the worktree root (it selects
  tests from the diff against origin/main; trust only the final
  `==> <lane>: N passed` sentinel). For prose-only PRs ALSO run
  `python3 -m pytest tests/<the test files for every module you touched> -q`.
  `ruff check <touched files>` must be clean. If mypy is cheap for your files,
  run `mypy <files>`.
- Review before push: run the `/code-review` skill at medium on your diff and fix
  what is real; for code-changing PRs also run `/simplify` first. Record both in
  the PR body (findings → fixed / wontfix with reason).
- `git push -u origin <branch>` (retry with backoff on network failure). Then
  open the PR with the GitHub MCP tool `mcp__github__create_pull_request`
  (owner jaspercurry, repo jts, base main). If the tool is unavailable, push
  and report the branch; do not retry forever.
- Commit message: imperative title ≤ 70 chars; body states the line delta
  and what was kept. End every commit message with exactly:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_014c7gAw7jA2r1pvBDE5wXkN
  ```
- PR body sections: **Summary** · **Line delta** (a table: file, before, after)
  · **What was kept and why** (prose PRs) / **What was deleted and the grep
  that proves no caller** (deletion PRs) · **Review record** (/simplify,
  /code-review findings and outcomes) · **Validation** (the test-fast sentinel
  line, pytest counts, ruff/mypy). End with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  https://claude.ai/code/session_014c7gAw7jA2r1pvBDE5wXkN
  ```
- Do NOT merge. Do NOT create ADRs. Do NOT add config knobs, guards, or
  scripts. Do NOT touch files outside your assignment.

## Files you must not touch (in-flight PRs)
- #3724 has MERGED (main bcf56117+). Its stacked follow-ups (the relay routes PR
  and the deploy PR) have not landed, so still do not touch:
  jasper/web/correction_crossover_v2_relay.py · jasper/capture_relay/** ·
  deploy/assets/shared/js/qr.js · deploy/assets/correction/js/main.js ·
  jasper/web/correction_setup.py · jasper/web/correction_crossover_v2.py ·
  jasper/web/correction_crossover_v2_wired.py · jasper/web/correction_room_flow.py
  and their tests (tests/test_correction_setup.py, tests/test_correction_crossover_v2_*.py).
- #3766 (branch claude/active-speaker-lazy-init) rewrites
  jasper/active_speaker/__init__.py (PEP 562 lazy names) and touches
  jasper/cli/active_speaker.py, jasper/cli/seat_level.py, jasper/multiroom/*,
  tests/test_active_speaker_package.py, tests/test_lazy_imports.py. Do not edit
  jasper/active_speaker/__init__.py; keep hunks in the other files away from its.
- #3768 rewrites jasper/cli/aec_bridge.py (not tuning scope; do not touch).
- Before pushing, `git fetch origin` and check `git log --oneline origin/main -20`
  for anything that touched your files; rebase onto origin/main if so.

## The prose bar (for prose PRs) — AGENTS.md, applied fully
KEEP a comment or docstring line only if it is one of:
1. a non-derivable constraint: a unit, a range, a timing, a hardware quirk, a
   protocol fact, a numeric value's provenance ("measured on jts3 2026-08-14,
   see #2925" is fine as ONE line — the number's provenance is a constraint);
2. a one-line why-pointer: `See ADR-0198.` / `#3489: …one clause…` /
   `docs/measurement-loop-doctrine.md §4`;
3. a module docstring of ≤ 8 lines saying what the module owns and its one or
   two non-obvious rules;
4. a function docstring of 1 line (+ constraint lines per rule 1);
5. tooling comments: `# noqa`, `# type: ignore`, `# pragma`, SPDX headers,
   `#:` attribute docs reduced to the constraint;
6. `--help` / argparse text and any string literal — those are copy, not
   comments: NEVER edit string literals, code, imports, decorators, or `__all__`.
DELETE everything else: narration of what the code does, history ("used to",
"previously", "superseded", "before #NNNN"), dates and PR numbers as
narrative, owner-ruling quotations (replace with a pointer to the ADR that
holds it; if NO ADR or doc holds a still-binding ruling, keep ONE line stating
the constraint and citing the issue, and list it in the PR body under
"rulings with no ADR"), text addressed to a reviewer or future agent,
defences of hypotheticals, comparisons with other code, restated docstrings
of the thing being called. Normalize blank lines (PEP 8: 2 between top-level
defs, 1 inside; no runs of 3+).
Before deleting a docstring, check nothing pins it:
`grep -n "<module>" tests/*.py | grep -E "__doc__|getsource|read_text|ast\.parse"`.
If a test pins prose, leave that block untouched and name it in the PR body.
Work file by file, by reading. No scripts that strip comments. When you
SHORTEN a sentence, re-read the code it describes: compression has turned
correct counterfactuals ("forgetting X costs no error") into false claims about
current code (X was keyword-only with no default). /code-review catches some;
do not rely on it. If the container's test-fast changed-file lane is killed by
memory pressure (exit 144), run the touched modules' tests directly and say so. Target ≈ 10-12 %
prose for the file when done; report before/after prose lines per file
(count docstring+comment lines with tokenize/ast; recon/census/metrics.py works).

## Deletion rules (for dead-code PRs)
A symbol is dead only if a repo-wide grep (`git grep -n "<name>"` across
jasper/ scripts/ deploy/ experiments/ pyproject.toml tests/ docs/) shows no
production reference — count registries, entry points, systemd/udev, importlib
strings, getattr strings, and `__all__`. Delete the symbol, its imports, its
`__all__` entry, and the tests that exist ONLY for it. Paste the grep in the
PR body. If in doubt, leave it and say so.

## Report back (≤ 200 words)
Branch(es), PR URL(s), line delta per PR, anything held back and why, any
ruling-with-no-ADR you found, anything that surprised you.
