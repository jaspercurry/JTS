# Execution brief — tuning toolbox, WAVE 6 (= PLAN-FRESH-EYES wave 3) (read fully before touching anything)

Repo: /home/user/JTS. Read AGENTS.md first (short; it is the law here). Waves 1–3
of the right-sizing program are on main. The plan you are executing is
`tuning-rightsize-recon/PLAN-FRESH-EYES.md` and the shape it builds toward is
`TARGET.md`, both on branch `claude/tuning-rightsize/recon-reports` (read with
`git show origin/claude/tuning-rightsize/recon-reports:tuning-rightsize-recon/<file>`;
never merge that branch). The audit behind them is `AUDIT-FRESH-EYES.md` there.

## The bar (owner, 2026-09-04)
Clean, elegant, modular code; clear separation of concerns; single source of
truth; no god files; no bloat, no extra complexity, no new machinery; not too
much prose. Leave every file you touch smaller than you found it unless the
feature genuinely grew. Comments only for non-derivable constraints or a
one-line why-pointer. No new `JASPER_*` knob, no new guard, no new doc file,
no ADR unless your row says so.

## Verify the premise first, stop if false
Before changing anything, re-verify your row's premise against the tree at
your fresh `origin/main` (cite file:line in your report). If the premise is
false or already handled, STOP, push nothing, and report why. Two stopped
lanes last wave were both right.

## Environment (this container)
- `python3` has no numpy. Use the shared venv for anything that imports
  jasper: `/tmp/claude-0/-home-user-JTS/0e671b43-61c0-5c6e-96ba-be80a44d80c1/scratchpad/venv/bin/python`
  (`-m pytest`). `ruff` is the venv one (`<venv>/bin/ruff`, pinned ≥ 0.16.1; the /root/.local/bin one mis-reads `nonlocal` bindings); `mypy` is at `/root/.local/bin/`.
- Several agents share this box. Run ONE pytest process at a time, only the
  touched modules' tests, `-q -p no:cacheprovider`. Never the full suite.
  CI is the authority. Compare a suspicious local failure against a pristine
  `origin/main` worktree before believing it.
- Temp files: `mktemp -d`. Never write under the repo except your worktree.

## Branch and PR mechanics
- From /home/user/JTS: `git fetch origin main && git worktree add "$(mktemp -d)/wt" -b claude/tuning-rightsize/w6-<slug> origin/main`.
  Work there. Commit with EXPLICIT PATHS (`git add <files>`), never `-A`.
- One concern per PR. Never commit to any other branch; never push to main.
- Before pushing: `git fetch origin main` and rebase onto it (main moves under
  a wave); re-run your touched tests after the rebase.
- Review before push: for code PRs run the `/simplify` skill FIRST, then
  `/code-review` (medium) on your diff from inside the worktree; fix what is
  real; record both in the PR body (findings → fixed / wontfix with reason).
  If the skill is unavailable inside your agent, say "single-pass" in the body.
- `ruff check <touched files>` clean; `mypy <files>` if cheap.
- `git push -u origin <branch>` (retry with backoff on network failure). Open
  the PR with the GitHub MCP tool (load via ToolSearch
  `select:mcp__github__create_pull_request`; owner jaspercurry, repo JTS, base
  main). If the tool fails after one retry, report the branch name.
- Commit message: imperative title ≤ 70 chars; body states what changed and
  the line delta. End EVERY commit message with exactly:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01R8RQ2FPdsucCApERfUqwH3
  ```
- PR body sections: **Summary** · **Premise verified at** (SHA + file:line) ·
  **Line delta** (table: file, before, after) · **Proof** (the row's proof,
  shown) · **Review record** (/simplify, /code-review findings and outcomes) ·
  **Validation** (pytest counts for touched modules, ruff/mypy; astsame output
  for relocations). End with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  https://claude.ai/code/session_01R8RQ2FPdsucCApERfUqwH3
  ```
- No model identifiers anywhere in pushed content other than the trailer.
- Do NOT merge. Do NOT touch files outside your row. Remove your worktree
  when done (`git -C /home/user/JTS worktree remove <path>`), never with
  unpushed work.

## Relocation rule (after a near-miss in #3952)
Any move of a def or module across package depth MUST, before push, resolve
every relative import in the moved code — module-scope AND deferred
(function-body) — with `importlib.util.resolve_name` + `find_spec` against
the new package, and add or extend an AST-walk pin so the suite catches it
(tests stub the callers; they will not). Prove the moved bodies are
AST-identical to the merge-base (strip docstrings, ignore comments) and paste
the proof. Delete the old definition in the same PR; no re-export shims.

## Deletion rule
A caller grep is necessary but not sufficient. Every deleted def, script or
CLI needs a verdict in the PR body — SPENT (banked and covered; where),
SUPERSEDED (the tool at HEAD that covers it) or PROMOTE (do not delete; say
what it should become) — with the repo-wide grep pasted
(`git grep -n "<name>" -- jasper scripts deploy experiments pyproject.toml tests docs`).

## Toolbox shape (any PR that adds or moves a capability)
A CLI subcommand with argparse-documented inputs; one named JSON artifact
beside the round (`ARTIFACT_BY_VIEW` names round-views artifacts); a menu row
regenerated with `PYTHONPATH=. <venv>/bin/python scripts/generate-tuning-tool-menu.py`
(never hand-edit the table; `--check` must pass); a pointer from
`docs/tuning-methodology.md` or the doctrine at the step where an LLM reaches
for it. Exit codes and the refusal shape come from `jasper/cli/_refusal.py`.
Stdout is a small summary, never a curve array.

## Files other lanes own right now (do not touch)
Open PR #3934 owns the turntable autostop unit under deploy/. Your row names
your files; anything else belongs to another lane this wave.

## Report back (≤ 200 words)
Branch, PR URL (or branch if the tool failed), premise verification
(file:line), line delta, proof shown, review record, anything held back and
why, anything that contradicted this brief (the tree wins; say what you found).


## Wave-3 rows: god files and boundary pins (audit §5)
Landing is BATCHED: your PR is reviewed individually but merged through an
integration branch with its siblings under one CI run. So: open the PR as
usual, rebase onto fresh `origin/main` before pushing, and expect no
individual merge. Relocation rule applies in full (AST identity, deferred
imports resolved via `importlib.util.resolve_name` + `find_spec`, AST-walk
pin, no shims, old definition deleted in the same PR). A moved block keeps
its tests; only import paths change. No new prose beyond a one-line module
docstring stating what the new module owns.
- Files other lanes own this wave (do not touch): `jasper/cli/round_views*`,
  `jasper/cli/{classify_features,project_ring,close_reference,delay_sweep,arm_walk,angle_capture}.py`
  (wave 2); `jasper/active_speaker/startup_load.py` (3.1), `staging.py` (3.4),
  `driver_safety.py` (3.5), `tests/test_correction_boundary_ssot.py` (3.8),
  `tests/test_crossover_v2_conductor.py` (3.9) — each belongs to its own row only.
