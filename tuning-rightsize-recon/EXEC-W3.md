# Execution brief — tuning right-sizing, WAVE 3 (read fully before touching anything)

Waves 1 and 2 are on main. Start every branch from CURRENT origin/main.
Branch prefix for this wave: `claude/tuning-rightsize/w3-<slug>`.

Repo: /home/user/JTS. Read AGENTS.md first (it is short and it is the law here).
Program context if you need it (read-only, never merge that branch):
`git show origin/claude/tuning-rightsize/recon-reports:tuning-rightsize-recon/PLAN.md`.

## Environment facts (this container)
- `python3` has NO pytest module. Use `/root/.local/bin/pytest`. Find ruff with
  `which ruff || ls /root/.local/bin`.
- Many agents share this container. Run ONE pytest process at a time from your
  worktree, only the touched modules' tests, `-q -p no:cacheprovider`. Never the
  full suite. Never `scripts/test-fast`'s changed-file lane. CI is the authority.
- Temp files: the scratchpad is shared and files get clobbered. Use `mktemp -d`.
- Helper scripts (read-only, do not edit):
  - `/tmp/claude-0/-home-user-JTS/9b111738-25b0-5aac-b32a-13ff1633a457/scratchpad/w3/astsame.py <worktree>`
    — proves every changed .py file is code-identical to origin/main with
    docstrings stripped and comments ignored. Prose PRs MUST paste its output.
  - `/tmp/claude-0/-home-user-JTS/9b111738-25b0-5aac-b32a-13ff1633a457/scratchpad/w3/metrics.py <list.txt>`
    — prose census (JSON: file,total,docstring,comment,prose_pct). Note it
    reads paths relative to /home/user/JTS; for your worktree, copy it to your
    temp dir and change `REPO` at the top.

## Branch and PR mechanics
- From /home/user/JTS:
  `git fetch origin main && git worktree add "$(mktemp -d)/wt" -b claude/tuning-rightsize/w3-<slug> origin/main`
  Work there. Never commit to any other branch. Never push to main.
  When done (after push): `git worktree remove <path>` from /home/user/JTS.
- One concern per PR. If your task lists several concerns, make one branch/PR
  each, sequentially, each from a fresh origin/main.
- Commit with EXPLICIT PATHS (`git add <files>`), never `git add -A`.
- Before pushing: `git fetch origin` and check `git log --oneline origin/main -30`
  for anything touching your files; rebase onto origin/main if so.
- Review before push: run the `/code-review` skill (medium) on your diff and fix
  what is real; for code-changing PRs run `/simplify` FIRST, then `/code-review`.
  Record both in the PR body (findings → fixed / wontfix with reason). If the
  skill fan-out is unavailable inside your agent, say "single-pass" in the body.
- `ruff check <touched files>` must be clean. If mypy is cheap for your files,
  run `mypy <files>`.
- `git push -u origin <branch>` (retry with backoff on network failure). Then
  open the PR with the GitHub MCP tool (load via ToolSearch
  `select:mcp__github__create_pull_request`; owner jaspercurry, repo JTS, base
  main). If the tool is unavailable after one retry, push and REPORT THE BRANCH
  in your final message; do not retry forever.
- Commit message: imperative title ≤ 70 chars; body states the line delta and
  what was kept. End EVERY commit message with exactly:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015N7EavRVFCHwXkKTPLEJNF
  ```
- PR body sections: **Summary** · **Line delta** (table: file, before, after)
  · **What was kept and why** (prose PRs) / **What was deleted and the verdict**
  (deletion PRs) · **Review record** (/simplify, /code-review findings and
  outcomes) · **Validation** (pytest counts, ruff/mypy, astsame output for prose
  PRs). End with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  https://claude.ai/code/session_015N7EavRVFCHwXkKTPLEJNF
  ```
- No model identifiers anywhere in pushed content (commit bodies, PR bodies,
  code) other than the mandated trailer above.
- Do NOT merge. Do NOT create ADRs. Do NOT add config knobs, guards, scripts,
  or new docs files. Do NOT touch files outside your assignment.

## Files you must not touch (refreshed 2026-09-03 23:00Z)
- Open PR #3934 owns `experiments/usb-turntable/jts_turntable.py`.
- Open PR #3929 owns `jasper/web/balance_flow.py`.
- In-wave ownership (other agents are editing these right now):
  - TOOLBOX lane: `docs/measurement-loop-doctrine.md` §1, `docs/tuning-methodology.md`,
    `docs/tuning-operator-runbook.md` (generated menu), `jasper/cli/round_views.py`,
    `jasper/active_speaker/crossover_v2/round_views.py`,
    `jasper/active_speaker/driver_protection.py`, `scripts/generate-tuning-tool-menu.py`.
  - HYGIENE lane: `jasper/active_speaker/__init__.py`, `jasper/active_speaker/startup_load.py`,
    `jasper/active_speaker/attempts_loop.py`, `tests/test_active_speaker_attempts_loop.py`,
    `tests/test_audio_measurement_gating.py`, `tests/test_lazy_imports.py`, and PR #3869's
    files (`jasper/web/correction_crossover_flow.py`, `jasper/active_speaker/setup_status.py`,
    `jasper/active_speaker/measurement.py`, `jasper/active_speaker/web_commissioning.py`).
  - PROSE lanes P1/P2/P3: the `jasper/active_speaker/crossover_v2/` files listed in each
    lane's task. No prose lane touches `round_views.py`, `evidence_packet.py`,
    `feature_classifier.py`, `contracts.py`, or anything outside the package.
  - RELOCATION lane 2.1: `jasper/correction/coordinator.py`, `jasper/correction/playback.py`,
    `jasper/correction/envelope.py`, their new homes, their importers, and their tests.
- OWNER RULINGS (ADR-0229): `docs/HANDOFF-bass-extension-plan.md` and
  `docs/bass-extension-waves/**` STAY. Never delete, move, or rename them.
- Do not edit `jasper/active_speaker/__init__.py` (lazy façade) except the HYGIENE lane.

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
holds it — ADR-0227/0228 hold most of them; if NO ADR or doc holds a
still-binding ruling, keep ONE line stating the constraint and citing the
issue, and list it in the PR body under "rulings with no ADR"), text
addressed to a reviewer or future agent, defences of hypotheticals,
comparisons with other code, restated docstrings of the thing being called.
Normalize blank lines (PEP 8: 2 between top-level defs, 1 inside; no runs of 3+).

THE COSTLY FAILURE MODE (wave 2 paid for it seven times): a constant's comment
carries a NUMERIC COUPLING to another constant, a tolerance ceiling, a floor
that must not be lifted, or "four bounds" while the code lists four. When you
shorten a sentence next to a number, re-read the code it describes and keep
the number's constraint. Compression has also turned true counterfactuals
("forgetting X costs no error") into false claims about current code. Do not
rely on /code-review to catch these — an Opus reviewer will diff every
constant's comment against main before your PR is accepted, and a dropped
coupling sends the PR back.

Before deleting a docstring, check nothing pins it:
`grep -n "<module>" tests/*.py | grep -E "__doc__|getsource|read_text|ast\.parse"`.
If a test pins prose, leave that block untouched and name it in the PR body.
Work file by file, by reading. No scripts that strip comments. Target: the
constraint content, whatever percentage that lands at (calibration-table files
legitimately stay at 25–35 %). Report before/after prose lines per file.

## Deletion rules (for any PR that deletes a def, script, CLI, or experiment)
OWNER RULE: a caller grep is necessary but not sufficient. An LLM has driven
this codebase's tuning by hand, so scripts, CLIs, experiments and analysis
helpers may have been used ad hoc with no code caller. Any deletion of a
script, CLI entry point, experiment, or an analysis/metric function needs a
written verdict in the PR body — SPENT (result is banked and the method is
covered; name where), SUPERSEDED (name the tool at HEAD that covers it), or
PROMOTE (do not delete; open an issue proposing the proper tool) — with the
evidence (docstring corpus names, docs/ADR citations, git history). Plain
helpers with a same-file successor need only the grep.
A symbol is dead only if a repo-wide grep (`git grep -n "<name>"` across
jasper/ scripts/ deploy/ experiments/ pyproject.toml tests/ docs/) shows no
production reference — count registries, entry points, systemd/udev,
importlib strings, getattr strings, and `__all__`. Delete the symbol, its
imports, its `__all__` entry, and the tests that exist ONLY for it. Paste the
grep in the PR body. If in doubt, leave it and say so.

## Toolbox shape (for any PR that adds or moves a capability)
Every capability ends up as: a CLI subcommand with argparse-documented inputs;
a named JSON artifact written beside the round (pattern: `jasper-round-views
co-metrics` / `directivity` / `cloud-binding`); a row in the runbook's tool
menu, regenerated with `PYTHONPATH=. python3 scripts/generate-tuning-tool-menu.py`
(never hand-edit the table); and a pointer from `docs/tuning-methodology.md`
or the doctrine at the step where an LLM would reach for it. JSON evidence
bundles under `/var/lib/jasper/active_speaker/sessions/`; no database.

## Report back (≤ 250 words)
Branch(es), PR URL(s) or branch names if the PR tool failed, line delta per
PR, before/after prose per file (prose PRs), anything held back and why, any
ruling-with-no-ADR you found, anything that surprised you or contradicted
this brief (the tree wins; say what you found).

## Relocation rule added 2026-09-04 (after a near-miss in #3952)
Any move of a module across package depth MUST, before push, resolve every
relative import in the moved file — module-scope AND deferred (function-body)
— with `importlib.util.resolve_name` + `find_spec` against the new package,
and add or extend an AST-walk pin so the suite catches it (tests stub the
callers; they will not). A missed `..` in a deferred import is invisible to
ruff, mypy and the suite, and fatal on hardware.
