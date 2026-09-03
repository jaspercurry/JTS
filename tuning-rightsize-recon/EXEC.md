# Execution brief — tuning right-sizing, Phase 1 (read fully before touching anything)

Repo: /home/user/JTS (you are in your own git worktree of it). Read AGENTS.md
first. Plan context: scratchpad/PLAN.md §3 (phases) and §7 (bottom-up check);
recon evidence in scratchpad/recon/ and scratchpad/bottomup/.

## Branch and PR mechanics
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

## Files you must not touch (in-flight PR #3724 and its stacked follow-ups)
jasper/active_speaker/angle_capture.py · jasper/active_speaker/crossover_v2/capture_source.py ·
jasper/audio_measurement/wired_capture.py · jasper/correction/envelope.py ·
jasper/web/correction_crossover_v2.py · jasper/web/correction_crossover_v2_wired.py ·
jasper/web/correction_crossover_v2_relay.py · jasper/web/correction_room_flow.py ·
jasper/web/correction_setup.py · jasper/capture_relay/** · deploy/assets/shared/js/qr.js ·
and their test files (tests/test_correction_crossover_v2_*.py, tests/test_correction_setup.py,
tests/test_correction_envelope.py, tests/test_angle_capture_take.py,
tests/test_crossover_v2_stage_bridge.py, tests/test_crossover_v2_remote_tier.py,
tests/test_crossover_v2_profile_not_confirmed.py, tests/test_measurement_vocabulary.py,
tests/crossover_v2_fixtures.py). The docs agent may edit docs/tuning-operator-runbook.md
but must keep its hunks away from the JASPER_CAPTURE_SOURCE / capture-source text #3724 removes.

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
Work file by file, by reading. No scripts that strip comments. Target ≈ 10-12 %
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
