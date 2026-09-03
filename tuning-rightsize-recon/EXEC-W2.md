# Execution brief — tuning right-sizing, WAVE 2 (read fully before touching anything)

Wave 1 is on main (PR #3837). Start every branch from CURRENT origin/main.
Branch prefix for this wave: `claude/tuning-rightsize/w2-<slug>`.

Repo: /home/user/JTS (you are in your own git worktree of it). Read AGENTS.md
first. Plan context: git show origin/claude/tuning-rightsize/recon-reports:tuning-rightsize-recon/PLAN.md (§4 target shape, §9 state).

## Branch and PR mechanics
- Temp files: the scratchpad is SHARED by every agent and files get clobbered.
  Put logs, commit messages and scratch scripts in `mktemp -d` (or your own
  worktree's untracked dir), never at the scratchpad root.
- Load: the container runs many agents at once. Do not run test-fast's
  changed-file lane; run `ruff check` on touched files and
  `python3 -m pytest <touched modules' tests> -q -p no:cacheprovider`; state in
  the PR that CI is the authority for the full lane.
- `git fetch origin main` then `git checkout -b claude/tuning-rightsize/w2-<slug> origin/main`
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

## Files you must not touch (in-flight PRs, refreshed 2026-09-03 16:10Z)
- PR #3851 (delete the phone-mic capture relay package) touches:
  jasper/active_speaker/crossover_v2/capture_plan.py · crossover_v2/sweep_spec.py ·
  jasper/active_speaker/crossover_v2_flow.py · jasper/bass_extension/bench/executor.py ·
  jasper/capture_protocol.py · jasper/capture_relay/** · jasper/capture_relay_config.py ·
  jasper/cli/bass_extension_bench.py · jasper/cli/doctor/correction.py ·
  jasper/control/state_aggregate.py · jasper/cues/registry.py ·
  jasper/web/correction_crossover_v2.py · jasper/web/correction_crossover_v2_relay.py ·
  jasper/web/correction_crossover_v2_republish.py · pyproject.toml · and these tests:
  tests/crossover_v2_fixtures.py test_active_speaker_repeat_reservation_sinks.py
  test_audio_measurement_program.py test_correction_boundary_ssot.py
  test_correction_crossover_v2_*.py test_correction_setup.py test_crossover_v2_admission_wiring.py
  test_crossover_v2_capture_source.py test_crossover_v2_cleanup_drain.py test_crossover_v2_conductor.py
  test_crossover_v2_honest_capture_copy.py test_crossover_v2_lateral_evidence.py
  test_crossover_v2_profile_not_confirmed.py test_crossover_v2_remote_tier.py
  test_crossover_v2_stage_bridge.py test_crossover_v2_verify_grading.py test_install_profile_tiers.py
  test_lint_contracts.py test_measurement_vocabulary.py test_web_correction_setup.py
  Do NOT edit any of those.
- PR #3836 (branch claude/tuning-rightsize/1-2a-as-dead) is HELD for an owner ruling; do not
  delete or rename anything in jasper/active_speaker/commissioning_runtime.py,
  jasper/active_speaker/staging.py, jasper/web/web_commissioning.py beyond prose edits.
- OWNER RULING: docs/HANDOFF-bass-extension-plan.md and docs/bass-extension-waves/** STAY.
  Never delete, move, or rename them.
- Do not edit jasper/active_speaker/__init__.py (lazy façade) except to drop an entry whose
  target you deleted.
- Before pushing, `git fetch origin` and check `git log --oneline origin/main -30` for anything
  that touched your files; rebase onto origin/main if so.

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
OWNER RULE (2026-09-03): a caller grep is necessary but not sufficient. An LLM has
driven this codebase's tuning by hand, so scripts, CLIs, experiments and analysis
helpers may have been used ad hoc with no code caller. Any deletion of a script,
CLI entry point, experiment, or an analysis/metric function needs a written verdict
in the PR body — SPENT (result is banked and the method is covered; name where),
SUPERSEDED (name the tool at HEAD that covers it), or PROMOTE (do not delete; open
an issue proposing the proper tool) — with the evidence (docstring corpus names,
docs/ADR citations, git history). Plain helpers with a same-file successor need only
the grep.
A symbol is dead only if a repo-wide grep (`git grep -n "<name>"` across
jasper/ scripts/ deploy/ experiments/ pyproject.toml tests/ docs/) shows no
production reference — count registries, entry points, systemd/udev, importlib
strings, getattr strings, and `__all__`. Delete the symbol, its imports, its
`__all__` entry, and the tests that exist ONLY for it. Paste the grep in the
PR body. If in doubt, leave it and say so.

## Report back (≤ 200 words)
Branch(es), PR URL(s), line delta per PR, anything held back and why, any
ruling-with-no-ADR you found, anything that surprised you.

## Wave-2 specifics
- Second-pass prose files landed at 30-50% prose after wave 1. The bar is unchanged
  (≈10-12%): most remaining docstrings are still narration. Prove code identity with
  `python3 /tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/astsame.py <file>...` style: strip docstrings/comments from both
  versions with ast and compare dumps — do it yourself with a 10-line script in your
  temp dir and paste the result in the PR body ("AST identical: N files").
- Count prose with `python3 /tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/census/metrics.py <list.txt>`
  (JSON list with file,total,docstring,comment,prose_pct).
- Do not run more than ONE pytest process at a time from your worktree; never the
  full suite. Test only the touched modules' tests.
- Your worktree: `git worktree add <mktemp -d>/wt -b claude/tuning-rightsize/w2-<slug> origin/main`
  from /home/user/JTS, work there, and `git worktree remove` it when done (after push).
  Commit with EXPLICIT PATHS (`git add <files>`), never `git add -A`.
