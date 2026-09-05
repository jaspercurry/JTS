# Survey lane brief — speaker-tuning toolbox, fresh-eyes survey (issue #4029)

You are one read-only survey lane. The architect (a separate session) will judge
your report; you do not plan, you do not propose PR lists, and you do not edit.

## Hard rules
- READ-ONLY. Do not modify any file under /home/user/JTS. Do not create branches,
  worktrees, commits, or pushes. Do not touch git state beyond `git log`/`git show`/`git grep`.
- Read /home/user/JTS/AGENTS.md first (180 lines; it is the law here).
- HEAD is origin/main = f4ff89731 (the checkout is at it). Cite every claim as
  `path:line` at that HEAD. Never cite a file:line you did not open.
- Verdict vocabulary for any claim you check: TRUE / FALSE / PARTIAL /
  CHANGED-SINCE (name the commit) / COULD-NOT-DETERMINE (say what would decide it).
- The prior architect's record is exported under
  /tmp/claude-0/-home-user-JTS/7f9f2b6a-cb68-5f85-b9ea-ee768db85aa0/scratchpad/record/
  (LANDING.md, TARGET.md, WAVE-LOG.md, AUDIT-FRESH-EYES.md, HANDOFF-NEXT.md,
  PLAN-FRESH-EYES.md). Treat its claims as hypotheses to check, never as facts.
  The tree wins.
- Python: system `python3` has no numpy. A venv is being built at
  `<scratchpad>/venv`; it is ready when `<scratchpad>/venv/READY` exists
  (poll every 30 s for up to 6 minutes if you need it). Use
  `<scratchpad>/venv/bin/python` (`-m pytest -q -p no:cacheprovider` on a
  narrow target only; never the full suite; one pytest process at a time).
  If the venv never comes up, do your lane read-only and say so.
- Scratch files go under `<scratchpad>/work/<your-lane>/` (mkdir -p). Never under the repo.
- Budget your reading: open whole files only when the question needs it; use
  `grep -n`, `sed -n 'a,bp'`, `awk` to read excerpts.

## Report
Write the FULL report to `<scratchpad>/reports/<your-lane>.md` (markdown, tables
where the data is tabular, file:line everywhere, no prose about your process).
Return to the caller a summary of at most 250 words: the three to six findings
that most change the picture, each with one file:line, and one line naming
anything you could not determine.

## What "wanting" means (for the vision lanes)
The vision (issue #4029 §1): an LLM drives the box over SSH, asks a human to
place a microphone, measures, separates room from speaker, proposes a config,
stages it, re-measures. Three principles: (1) the LLM should never be wanting —
every capability is a CLI subcommand with argparse inputs, a named JSON
artifact beside the round, a generated menu row, a pointer from the
methodology; if the LLM would write a script, that is a toolbox defect;
(2) do not be prescriptive about the LLM's reasoning — refuse only on
integrity and the non-negotiables, disclose everything else; (3) manage the
LLM's context — a tool's answer is a small summary plus a path; depth on
demand; `inventory` names what is missing. "Wanting" = any place the LLM
would have to write a script, read a wall of curves, guess, or talk out-of-band.
