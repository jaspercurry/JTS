# Execution brief — tuning toolbox, WAVE 5 (= PLAN-FRESH-EYES wave 2) (read fully before touching anything)

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
- From /home/user/JTS: `git fetch origin main && git worktree add "$(mktemp -d)/wt" -b claude/tuning-rightsize/w5-<slug> origin/main`.
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

## Wave-2 rows: one binary per verb family (relocations, tag R)
Each row retires ONE console script into a verb of `jasper-round-views`
(or `jasper-round` / `jasper-angle-capture`), deleting the entry point and
the module in the same PR. Rules on top of the relocation rule:
- The absorbing CLI module stays THIN: one `add_parser` block plus one
  `_cmd_<view>` that parses, calls the engine, files the artifact through
  the module's existing writer, prints the small summary. Logic that lived
  in the retired CLI module beyond that (formatting helpers, computation,
  refusal mapping) moves DOWN into the engine module that owns the
  computation (`jasper/active_speaker/crossover_v2/<topic>.py`), never
  sideways into `round_views.py`. If `jasper/cli/round_views.py` would pass
  1,000 lines after your row, STOP and report: the orchestrator decides on
  a package split first.
- `ARTIFACT_BY_VIEW` gains the view's row (the retired tool's artifact name,
  unchanged) and `inventory` therefore names it; `--out` keeps the same
  meaning. Exit codes and the refusal record come from `_refusal.py`
  (`stage`/`StageFailed` for the write).
- Delete: the retired module, its `pyproject.toml` entry point, its
  `TUNING_TOOL_MODULES` roster line in `scripts/generate-tuning-tool-menu.py`,
  and regenerate the menu (`--check` must pass). Deletion verdict in the PR
  body: SUPERSEDED by `<binary> <verb>`, with the repo-wide grep pasted.
- Tests: move the retired CLI's behaviour pins onto the new verb (same
  altitude, same count or fewer); delete pins whose subject was the old
  `main`/parser only. Engine tests untouched.
- Docs: `docs/tuning-methodology.md`, `docs/tuning-operator-runbook.md`,
  `docs/measurement-loop-doctrine.md` and `docs/testing-tooling.md` mention
  the retired name; replace each mention with the new verb IN THE SAME PR
  (a one-token substitution per mention, no rewording); a local-links or
  docs test failing on the old name is yours, not row 2.11's.
- `scripts/run-crossover-round.py` is a laptop transport; if it spells the
  retired binary, repoint it (same one-token rule).
- Concurrency: sibling lanes add rows to `ARTIFACT_BY_VIEW`, subparsers to
  `build_parser`, and lines to `pyproject.toml` at the same time. Expect an
  additive rebase conflict in those three places and in the generated menu:
  keep both sides for the first three, then REGENERATE the menu (never
  hand-merge it), re-run your tests, and push. Rebase again if the push is
  rejected.
- Files other lanes own this wave: everything under another row's binary.
  Do not touch `jasper/cli/delay_sweep.py`, `classify_features.py`,
  `project_ring.py`, `close_reference.py` unless your row names them.
