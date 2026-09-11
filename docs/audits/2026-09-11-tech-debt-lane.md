# 2026-09-11 tech-debt paydown lane

| field | value |
|---|---|
| audited SHA (re-verification) | `c364bce19` |
| landing SHA | `<FINAL_SHA>` |
| date | 2026-09-11 |
| scope | 165 open issues (3 prior audits) re-verified at HEAD, then executed across 5 waves |
| tracking issue | `<TRACKER_ISSUE>` |
| plan page | https://claude.ai/code/artifact/90c96223-06f0-4216-a696-cf55952b9384 |
| evidence | `<EVIDENCE_RELEASE>` |

## 1. Headline

This lane took the three audits already on the books at 2026-09-10 — the
2026-09-09 whole-repo deep audit (`docs/audits/2026-09-09-deep-audit.md`,
tracking #4775), the 2026-09-05 register (issues #4786-#4812, twelve of
them umbrellas holding 146 findings), and the small 2026-09-10 measurement
audit (#4863-#4868, Codex bass lane) — and did two things: re-verified
every one of the 165 issues still open across those three audits directly
against source at `c364bce19` (never against the audits' own prose), then
executed a five-wave plan against what was still real. Five investigators
produced the evidence (`audit-status.md`, `issues-A/B/C-status.md`,
`health-snapshot.md`, all file:line or commit citations, never memory); a
conductor turned that into a plan (`plan.md`) and ran Wave 0 (close what's
already done) and Wave 1-2 (mechanical PRs) through worktree/branch/PR
builders, each reviewed with `/simplify` + `/code-review`, non-negotiable-
tier diffs also through `/adversarial-review`.

**Outcome, `c364bce19` → this lane's work:**

- **19 PRs merged**: #4832, #4345, #4895 (Wave 0); #4900, #4902, #4905,
  #4912, #4914, #4915, #4916 (Wave 1); #4901, #4908, #4911, #4924, #4926,
  #4929, #4933 (Wave 2); #4931 (secrets); #4935 (closes #4930, landed after
  this lane's own tracker synthesis was written — see §5).
- **23 issues closed**: 21 in Wave 0 with a posted evidence line each, plus
  #4927 (via #4931) and #4930 (via its own fix). One additional issue,
  #4803, was closed **by accident** (a commit-message trap, not real
  completion) and needs reopening — see §10.
- **~36 register-finding rows landed** across the twelve #4801-#4812
  umbrellas (§4's umbrella tables), on top of the ~30 already-done rows the
  investigation itself found pre-lane.
- **6 new issues filed**: #4893 (hardware-evening checklist), #4894
  (#4324 residuals), #4920, #4927 (closed by #4931), #4928 (closed by
  #4926's fixture), #4930 (closed late — see above). Two issues were
  **reopened** after the same commit-message trap closed them wrongly:
  #4717, #4749.
- **2 ADRs**: ADR-0288 (the v1 commissioning lane is deleted, #4832),
  ADR-0293 (the no-provider park is owned by the voice daemon, #4905,
  supersedes ADR-0165 on that axis).

The live ledger for everything below is tracking issue `<TRACKER_ISSUE>`
(token — the conductor fills this in on posting); the five-wave plan with
the full owner decision sheet is on the plan page above; evidence
(investigator reports, PR diffs, review transcripts) is at `<EVIDENCE_RELEASE>`
(token — release asset, per ADR-0284, or the tracking issue if smaller).
This file is frozen at `c364bce19`; nothing below is updated after landing
— read the tracking issue for current state.

---

## 2. Method

### 2.1 Investigation (five investigators, one day, HEAD `c364bce19`)

Every investigator worked from the same rule: **evidence is a file:line or
a commit hash, never memory, never the audit's own prose.** Concretely:

- **`audit-status.md`** re-verified every action item in the 2026-09-09
  report (58 main-report rows, plus 11 PR-body follow-ups and 11
  owner-decision leftovers) directly against `c364bce19` — DONE / OPEN /
  PARTIAL / CANT-TELL, each with the grep or `git log -S` that produced the
  verdict.
- **`issues-A-status.md`, `issues-B-status.md`, `issues-C-status.md`** — the
  full 165-issue tracker dump, split into three slices (A: the 34
  `audit`-labelled issues, including the twelve #4801-#4812 umbrellas and
  the six #4863-#4868 measurement-audit rows; B: 63 numbered-≥4000,
  non-`audit`-labelled issues, mostly per-lane handoff/residual trackers;
  C: 68 numbered-<4000 issues, mostly tuning/voice/hardware). Each issue's
  comment thread was read for an owner ruling before the code was
  re-checked; a ruling recorded in an issue's own **body** text (not a
  comment) was noted but not treated as a literal ruling per the task's own
  rule — several issues below carry that distinction explicitly.
- **`health-snapshot.md`** — ten measured sections (size, largest files,
  test altitude, prose ratio, knobs, lazy imports, TODO markers, docs,
  duplicate helpers, voice-cleanup-program state), each with the exact
  Python/`awk`/`git grep` command next to the number, and a comparison
  against the 2026-09-09 baseline's own published figures where a command
  survived. Where the 09-09 playbook didn't publish an exact regex (most of
  the altitude counts), this snapshot states its own operational definition
  and flags it as a proxy, not a re-run.

The conductor turned these into `plan.md` (five waves + a 26-item owner
decision sheet, each item with a recommendation) and ran it.

### 2.2 Execution

- **One worktree, one branch, one PR per batch.** Wave 1's batches (1a-1g)
  and Wave 2's slices (2a1-2a5, 2b, 2c1) each touch a disjoint file set —
  no two batches share a file, so builders run in parallel with nothing to
  reconcile.
- **Every builder starts from the same brief** (`wave1-builder-template.md`,
  reused verbatim for Wave 2 with the file list and cap swapped) — verbatim
  below, since the next session should reuse it rather than re-derive it:

```
# Wave 1 builder brief — shared template (read fully before your batch section)

You are a builder in the JTS tech-debt lane. Owner ruling (2026-09-10): delete unused code, merge to main, make the codebase simpler and more elegant, obvious wins first; every PR gets simplify + code review; step back on each item to make sure the architecture is right before building.

## Hard rules
- Work ONLY in the worktree you are given. One live agent per worktree. Never `cd` elsewhere. Do NOT spawn sub-agents. Do NOT run any `gh` command (push with `git push`, report the SHA; the conductor opens/merges the PR).
- The worktree has no venv: use `/Users/jaspercurry/Code/JTS/.venv/bin/python -m pytest …` and `/Users/jaspercurry/Code/JTS/.venv/bin/ruff` / `mypy` from inside the worktree.
- Commit early, push a WIP commit before any long test run. Run `scripts/test-fast` in the FOREGROUND with a 600 s timeout before the final push; trust only the final `==> <lane>: N passed` line.
- Respect AGENTS.md: leave every file you touch smaller than you found it unless the feature grew; delete dead code you find in scope after verifying no caller (registries, pyproject entry points, systemd ExecStart, deploy/bin, udev, CI, importlib/getattr strings); comments only for non-derivable constraints and why-pointers; no narration, no history, no dates/PR numbers in code; hoist function-local imports unless a trailing `# lazy` names the reason; tests pin behaviour (types, codes, structured fields), never log/error prose; no new JASPER_* knobs.
- Non-negotiables are untouchable: volume_limit 0.0 + set_volume_db clamp; never SAVE_CONFIGURATION on the XVF3800; secrets only in their compartment files; deploy only via scripts/deploy-to-pi.sh; renderer ALSA probe as the unit's real User; a new path that prevents wake response plays a cue; never run tests/voice_eval; main protected.

## Step back first (mandatory, write it into the PR body under "Design")
Before editing, for the batch as a whole and for any item that is more than a mechanical delete: (1) which module OWNS this concern, and is the fix landing behind that boundary; (2) is there a simpler, more general change to the underlying mechanism than the one the issue proposes (prefer it, say why); (3) does this add machinery — if so, name its removal condition; (4) does any part touch a non-negotiable tier (cues, install.sh, secrets, DSP output path) — flag it so the conductor routes an adversarial review. Ten lines, plain language. If the step-back says the issue's proposed fix is the wrong shape, build the right shape and say so; if it says the item should not be built at all, skip it and say why.

## Flow
1. `git fetch origin && git rebase origin/main` (your branch starts at origin/main).
2. Build the batch, one concern per commit, commit messages in the repo's style (subject: area: what; body: why in 1–3 sentences; end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`).
3. Run the `simplify` skill on your own diff (Skill tool, name `simplify`) and apply what it finds.
4. `scripts/test-fast` (foreground, 600 s timeout). Fix reds. Push.
5. Write the PR body to `<scratchpad>/pr-<batch>-body.md`: title line, "Design" (the step-back), "What changed" (one bullet per item with `Closes #N` or `Refs #N`), "Verified" (the test-fast sentinel line, ruff/mypy), and end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
6. Final report: branch, head SHA, files touched, `Closes` list, anything skipped and why, anything that needs the adversarial tier.

## Evidence
Every item's current state was verified at main c364bce19 on 2026-09-10 and is written up with file:line in the status files in the scratchpad (issues-A-status.md, issues-B-status.md, issues-C-status.md, audit-status.md). Read your items' sections there first; re-verify at HEAD before editing (main moves ~300 commits/day).
```

  **One thing this template got wrong, corrected mid-lane:** step 3 tells
  the builder to invoke the `simplify` Skill tool directly. Every Wave 2
  slice's PR body instead says "applied the four `/simplify` lenses myself
  instead of invoking the skill, per this task's instructions" — the
  conductor's Wave 2 briefs dropped the Skill-tool call in favor of the
  builder applying the four lenses (reuse, simplification, efficiency,
  altitude) inline. This matches the standing process rule (§10): a builder
  invoking a skill risks the same concurrent-agent-limit trip a sub-agent
  spawn does. **Reusing this template for a future wave: strike step 3's
  "Skill tool" instruction and replace it with "apply the four lenses
  yourself" before handing it out.**

- **Non-negotiable-tier diffs got `/adversarial-review` on top of
  `/simplify` + `/code-review`.** Four batches were routed there: #4905
  (voice park/cue), #4912 (install-lib comment touch, precautionary),
  #4916 (install-lib script generalization), #4931 (secrets). All four
  found real issues before merge — detailed in §5, since the owner
  explicitly wants them treated as the lane's lessons, not just its
  changelog.
- **Wave 0's closes and the umbrella-row comments were conductor-run, REST
  API only** (`wave0-closes-ledger.md`, `wave-wrapup-ledger.md`) — one
  Sonnet agent posting an evidence comment then closing, or posting an
  evidence comment on an umbrella issue without closing it (umbrellas never
  auto-close; disposition of every remaining row stays in the issue, per
  ADR-0284). 54 API calls in Wave 0, 24 more in the wrap-up pass, both
  under the ~40-call budget with a `sleep 4` between each.
- **One CI waiter, conductor-run, never a builder.** `scratchpad/ci_wait.sh`
  (22 lines, verbatim below) polls every PR in `PRS.txt` every ~4s per PR
  with a 150s outer sleep, and appends one `CI #<pr> <sha9> GREEN` or
  `RED[<failing-names>]` line to `CI_SEEN.txt` the first time a SHA's
  check-runs all complete — so a PR gets pushed to twice (once RED, once
  GREEN) shows up as two distinct lines, which is exactly the trail that
  makes #4916's and #4926's fix-and-reverify cycles (§5) reconstructable
  after the fact.

```bash
#!/bin/bash
# One slow CI waiter. PRS.txt lists PR numbers (one per line). Emits one line per (pr, head sha)
# when every check-run on that sha has completed: "CI #<pr> <sha9> GREEN" or "CI #<pr> <sha9> RED[names]".
S="$(dirname "$0")"; touch "$S/PRS.txt" "$S/CI_SEEN.txt"
while true; do
  for pr in $(grep -E '^[0-9]+$' "$S/PRS.txt"); do
    sha=$(gh api "repos/jaspercurry/JTS/pulls/$pr" --jq '.head.sha' 2>/dev/null) || { sleep 4; continue; }
    [ -z "$sha" ] && continue
    grep -q "^$pr $sha " "$S/CI_SEEN.txt" && continue
    sleep 4
    runs=$(gh api "repos/jaspercurry/JTS/commits/$sha/check-runs?per_page=100" --jq '[.check_runs[] | {name, status, conclusion}]' 2>/dev/null) || { sleep 4; continue; }
    total=$(jq length <<<"$runs" 2>/dev/null || echo 0); [ "$total" -eq 0 ] && { sleep 4; continue; }
    pending=$(jq '[.[] | select(.status!="completed")] | length' <<<"$runs")
    [ "$pending" -gt 0 ] && { sleep 4; continue; }
    failed=$(jq -r '[.[] | select(.conclusion!="success" and .conclusion!="skipped" and .conclusion!="neutral") | .name] | join(",")' <<<"$runs")
    if [ -z "$failed" ]; then v=GREEN; else v="RED[$failed]"; fi
    echo "CI #$pr ${sha:0:9} $v"
    echo "$pr $sha $v" >> "$S/CI_SEEN.txt"
    sleep 4
  done
  sleep 150
done
```

---

## 3. Investigation findings

**State of the three audits at `c364bce19`.**

| audit | state |
|---|---|
| 2026-09-09 deep audit (114 agents, `docs/audits/2026-09-09-deep-audit.md`) | Essentially landed: all 8 Blockers and all 9 delete-now items 100% DONE, each fixed within a day of the audit. Still owed: one unmerged deletion (#4832, finished this lane), the test-suite altitude debt (below, untouched), ~15 small named gaps (aec-reconcile `--changed`, the invalid-provider silent voice park — both closed this lane via #4905 — plus avahi's polkit gap, still open). |
| 2026-09-05 register (#4786-#4812, 146 umbrella findings) | About a quarter self-resolved by unrelated work in the five days since filing; about half of its "small" rows were false positives or misreadings of deliberate architecture when re-verified at HEAD — e.g. #4786 (delete `jasper-web-streambox.service`) turned out to be the streambox's own alternate source file, not a duplicate unit, and deleting it would have broken the streambox install profile. **Rule for the future, stated in the tracker: re-verify every register row at HEAD before building it — never trust the register's own prose.** |
| 2026-09-10 measurement audit (#4863-#4868, Codex bass lane, PRs #4870/#4872) | Small (5 findings + 1 tracking issue). 2 of 5 findings (#4866, #4867) are fixed **inside** open PR #4870, which never merged during this lane — so they could not be closed here even though the code exists. #4863 partially addressed by the same PR (evidence-disclosure only). #4864, #4865 untouched. |

**"Altitude, not volume" — the test-debt numbers, re-measured (§health-snapshot.md §3), holding almost exactly where the 09-09 audit left them:**

| metric | 09-09 audit | this lane's re-measure (`c364bce19`) | delta |
|---|---:|---:|---:|
| private-attribute assert pins | 1,784 sites / 228 files | 1,866 sites / 235 files | +4.6% / +3% — untouched |
| log-text pins (combined definition) | 783 sites / 132 files | 445 sites / 135 files | -43% sites, same file count — 10 caplog-migration PRs already landed before this lane; `caplog.text` alone: 645/86 → 179/58 |
| `pytest.raises(..., match=<prose>)` | 653 sites / 156 files | 583 sites (prose-only bucket) | -11%, still blocked on a typed exception-code field |
| hand-rolled Fake\*/Stub\* duplicate names | not measured by 09-09 | 10 duplicated names / 92 classes | converged to 2 by PR #4901 (Wave 2) |

The 09-09 audit's own verdict — "the 'pinned to bullshit' hypothesis does not
survive the count; the real debt is altitude, not volume" — holds again at
this pin: both independently-derived regexes land within a few percent of
the audit's own numbers, and under 1% of test LOC is genuinely removable.

**False-positive rate of register rows (the investigation's own headline
finding):** re-verifying every one of the twelve umbrellas' 146 rows at
HEAD, rather than trusting the register's text, found: #4803 (dead-code,
16 rows) 6 fully fixed + 4 more partial by unrelated commits (62%); #4804
(deploy-integrity, 14 rows) 6 fixed (43%); #4807 (observability, 8 rows) 4
fixed/mostly-fixed (50%); #4809 (resilience, 14 rows) 3 fixed + 2 stale
(36%); #4805 (duplicate-primitives, 30 rows) only 4 fixed/stale but 12 more
simply not independently reconfirmed. Several issue-level "closes" in
Batch C (issues-C-status.md) were the same shape: #2102, #1969, #1955,
#2428, #2170, #2705 all read as fully resolved by work unrelated to the
issue itself once checked at HEAD, not left open by neglect.

**Size hotspots.** `tests/` is 502k non-blank LOC against `jasper/`'s 348k
(1.44:1) — a large but, per the snapshot, not abnormal ratio for this
house's heavy-parametrization, hardware-fidelity style. 8 of the top 10
largest Python files are `active_speaker`/`crossover_v2` product code or
its own tests (`correction_crossover_v2.py` 5,531 LOC is the largest
non-test product file; `runtime_contract.py` 4,494; `baseline_profile.py`
3,939). Duplication's real candidates are the small numeric/hash/path
helpers reimplemented per module — `_finite`/`_finite_float`/
`_finite_number` (27 definitions across 3 near-identical names),
`_sha256`, `_fingerprint`, `_positive_int`, `_state_path` — not the
top-5-by-count names (`build_parser`, `add_parser`, `make_server`, …),
which are a deliberate per-module interface convention, not copy-paste
debt. Ghost knobs: 313 `JASPER_*` names read by code but undocumented in
`deploy/`/`docs/`; 80 of those are AEC-family, already covered by the
owner's 09-10 "wake legs → one lab knob" ruling; ~130 are genuinely fresh
(`ACTIVE_SPEAKER`, `OUTPUTD`, `GROUPING`, `OPENAI`, `VOLUME`, …). 1,438
function-local imports lack the `# lazy` tag AGENTS.md asks for (upper
bound — some already carry a prose justification under a different
spelling); two web-wizard files (`correction_crossover_v2.py`,
`sound_active_speaker.py`) hold 16% of the total. TODO/FIXME/XXX/HACK
markers: a naive grep says 39, but 30 of those are `mktemp ... .XXXXXX`
noise; the corrected count is 13, of which only 2 are real markers in
product code (the rest are the audit playbook's own prose naming the four
words while explaining its method).

**Voice-lane ownership.** Another session owns the voice stack this week
(ADR-0292, the voice-cleanup lane) — Wave 3 and issues #4777-#4781, #4711
belong there, not here, and this lane's investigators deliberately did not
build against `jasper/voice/` files that lane is actively touching. What
they measured anyway, since it bears on scope decisions: `WakeLoop`
(`jasper/voice_daemon.py:494-3276` at the pinned SHA) is 2,783 lines / 91
methods — 85% of a 3,285-line file; `daemon_main.run` is 595 lines (45% of
a 1,319-line file); `OutageTracker` (`jasper/voice/_supervisor.py`) is
wired as a template-method funnel every provider is meant to report into,
but `OpenAILiveConnection` (the kept Realtime/Live adapter) overrides
`start()` directly instead of the hook, so `last_failure_detail()` and
`wake_cue()` can never reflect a real Live-path connection failure — a
genuine behavioral gap the voice-cleanup lane's own program already names
as its first item. (`jasper/research/` — including the code these numbers
were measured against — was deleted by PR #4878, merged 34 minutes
**after** this pin; a reader working from a later checkout will see
smaller numbers for the same reason, not new work.)

---

## 4. Triage — all 165 issues

Built from `issues-A-status.md` (34 issues, the `audit`-labelled slice —
including the twelve #4801-#4812 umbrellas and the six #4863-#4868
measurement-audit issues), `issues-B-status.md` (63 issues, numbered
≥4000, no `audit` label — mostly per-lane handoff/residual trackers),
`issues-C-status.md` (68 issues, numbered <4000 — mostly tuning/voice/
hardware), and `lane-tracker-issue.md` for what this lane actually landed.
Action codes: **CLOSE** (already true at HEAD, close with evidence),
**S/M/L** (buildable, sized), **OWNER** (needs a one-line ruling first),
**HW** (needs hardware or the owner in the room). "This lane" reports what
changed 2026-09-10→11; where it says "still open" nothing in this lane
touched that issue — see §3 for why (voice lane, Codex bass lane, or
simply not reached this round).

### 4.1 Batch A — `audit`-labelled (34 issues)

| # | title | action | status at `c364bce19` | this lane |
|---|---|---|---|---|
| #4801 | boundaries-cycles umbrella, 15 findings | L | 13/15 open; R-095 stale (code deleted), R-089 fixed pre-lane | wave0 evidence comment only; 0 rows landed |
| #4802 | config-knobs umbrella, 8 findings | S/M | R-215 fixed pre-lane; R-204 needs owner; 7 open | 3 rows landed (PR #4902: R-214, R-217, R-218) |
| #4803 | dead-code umbrella, 16 findings | S | 6 fixed + 4 partial pre-lane; rest open | 5 rows landed (PR #4900/#4902); **issue auto-closed by a commit-message trap — real rows remain open, needs reopening** |
| #4804 | deploy-integrity umbrella, 14 findings | S/M | 6 fixed pre-lane; R-243 needs owner (L) | 1 row landed (PR #4902: R-255) |
| #4805 | duplicate-primitives umbrella, 30 findings | L | 4 fixed/stale pre-lane, 12 not reconfirmed, rest open | 0 rows landed |
| #4806 | god-files umbrella, 17 findings | L | 2 fixed pre-lane (one a full rewrite); ~8,000 LOC still open | 0 rows landed |
| #4807 | observability umbrella, 8 findings | S | 4 fixed/mostly-fixed pre-lane, 4 open | 0 rows landed |
| #4808 | prose umbrella, 3 findings | S/OWNER | R-234 self-contradicting, needs owner; R-235/237 open | 0 rows landed |
| #4809 | resilience umbrella, 14 findings + R-014 (NN-1) | S/OWNER | R-015 fixed pre-lane; R-014 hearing-tier owner Q open; ~9 open | 0 rows landed |
| #4810 | single-writer umbrella, 12 findings | S/M | R-159/160 fixed pre-lane; R-163 live 10s timeout drift found | 2 rows landed (PR #4902: R-165, R-168) |
| #4811 | tests-pinning-internals umbrella, 5 findings | L | R-232 exact match confirmed; rest partial/open | 0 rows landed |
| #4812 | tuning-zone-structure umbrella, 4 findings | L/S | R-099 in-flight elsewhere; R-100/104 trivial; R-103 needs a destination decision | 1 row landed (PR #4900: R-100) |
| #4775 | 2026-09-09 deep-audit tracking issue | tracking | current, indexes #4786-4812 | landing note posted; stays open as index |
| #4777 | delete dead WakeFuser path | S | open, confirmed live-but-inert | still open (voice lane) |
| #4778 | relocate submit_recorded_audio + 2 stale comments | S | 2/3 fixed pre-lane; 1 open (peering_client docstring) | still open |
| #4779 | converge voice UDS clients + duplicate GainRamp | M | open, unchanged | still open (voice lane) |
| #4780 | finish WakeLoop wake-frame inlay + table builders | S | builder-table half done pre-lane; inlay open | still open (voice lane) |
| #4781 | Wave 5: supervisor test file + LiveConnection trim | M | open, unchanged | still open (voice lane) |
| #4782 | Wave 3 latency work (B1-B6) | OWNER | gated on Gate 0.2, no owner answer yet | still open |
| #4783 | Wave 6 hardware-gated tuning | HW | all 4 rows open, same gate as #4782 | still open |
| #4786 | delete jasper-web-streambox.service | S | **premise wrong** — alternate streambox source file, not a duplicate | skipped in PR #4902 (false premise); issue needs a closing comment, not yet closed |
| #4787 | mixer.rs info!/warn! + String alloc on SCHED_FIFO thread | M | open; sibling `sync_channel` pattern exists locally to copy | still open |
| #4788 | owner ruling: delete v1 lane (commissioning_apply.py) | CLOSE | already executed (commit 1b49a9052) | **CLOSED, Wave 0** |
| #4790 | wake dropped after research-confirm cancel timeout | CLOSE | already fixed pre-lane, moot after #4878 | **CLOSED, Wave 0** |
| #4791 | HID reader tasks die permanently (bridge.py) | S | open at investigation time | fixed pre-lane, found while building PR #4902 (commit 97f4e1c1f); issue not yet closed |
| #4792 | two rival measured-crossover-candidate models | CLOSE | already executed | **CLOSED, Wave 0** |
| #4800 | promote 2 security doctor rows to fail | OWNER | bulk done pre-lane (#4821); 1 open Q (core=True) | still open |
| #4814 | first-boot park cues are silent (NN-6) | M/OWNER | open, zero WAVs committed | still open |
| #4863 | current-level verification must belong to shared group | L | partially addressed by open, unmerged PR #4870 | still open (Codex lane's own PR) |
| #4864 | CLI/UI need one quality/recovery path | L | not addressed by either open PR | still open |
| #4865 | absolute SPL/probe admission lack verified hardware context | HW/OWNER | not resolved; NN-2-adjacent | still open |
| #4866 | cloud basis omits loudness compensation | CLOSE (cond.) | fixed inside open, unmerged PR #4870 | still open — **not closeable; #4870 itself never merged this lane** |
| #4867 | nonfinite recordings classified as quiet | CLOSE (cond.) | fixed inside open, unmerged PR #4870 | still open — same as #4866 |
| #4868 | shared leveling/measurement-quality tracking issue | tracking | 2/5 children done via #4870; 3 open | still open, unchanged |

### 4.2 Batch B — numbered ≥4000, no `audit` label (63 issues)

| # | title | action | status at `c364bce19` | this lane |
|---|---|---|---|---|
| #4027 | attached-hardware hardening survey (ADR-0235) | CLOSE | ADR-0235 fully realized; 2 rows HW-only | **CLOSED, Wave 0** |
| #4124 | jts4 CamillaDSP short-read WARN storm | HW | code path unchanged; needs a jts4 journal capture | still open |
| #4131 | installer build policy: 2 orphaned follow-ups | OWNER/HW | prebuilt-artifacts row done; threshold Q open | still open |
| #4139 | idle-efficiency review 2026-09-05 | OWNER | 12/14 open; 2 done pre-lane | still open |
| #4201 | quality lane P7: tests → A | S | 8 open (source-text pins, dup mypy run); 11 done, 7 stale | still open |
| #4202 | quality lane P8: docs/prose → A | S | 9 open incl. `sound/profile.py`'s false "clip-safe" claim | 1 item fixed (PR #4902: the profile.py comment); rest open |
| #4279 | secrets lane P1 handoff | OWNER | 10 open (item 8 PSK-on-argv is the owner Q); 13 done pre-lane | still open |
| #4324 | P12 handoff: attached-hardware residuals | CLOSE | 12 open code residuals, 2 done, 2 stale | **CLOSED, Wave 0** (residuals not yet re-filed) |
| #4327 | P4 observability lane remaining queue | L | 9 open, 3 done, 1 stale | still open |
| #4362 | XVF command-guard coverage gap | OWNER | unchanged, wontfix-eligible | still open |
| #4385 | P9 voice-loop lane handoff | HW | 10 open, 5 done; voice-lane territory | still open (voice lane) |
| #4387 | P6 right-sizing lane handoff | OWNER | 1 open (ObsMode enum), 6 done, 2 stale-corrected | still open |
| #4405 | ADR number ledger | no action | still live, correct, only mechanism | unchanged, no fix needed |
| #4416 | P3 resilience handoff | M | 6 open (R13/14/17/20), 22 done | still open |
| #4421 | P2 round-three deploy-integrity queue | M | 3 open (row 10 prose trim), 4 done | still open |
| #4427 | cleanup-programme handoff 2 | CLOSE | superseded by its own follow-through (spun off #4703-4738) | **CLOSED, Wave 0** |
| #4437 | P5 handoff: structure/SSOT/god-file remainder | L | 15 open, 4 done | still open |
| #4502 | seat-matched tuning: room+bass as toolbox layers | OWNER | superseded by shipped native dynamic-bass (ADR-0286/0287) | still open, confirmation Q posted |
| #4533 | voice UX: conversational follow-up window | OWNER | not started; 4 design Qs block it | still open (voice lane) |
| #4534 | voice UX: earcons instead of spoken "Done." | OWNER | #4532's safe half done; earcon half needs paid voice-eval | still open (voice lane) |
| #4635 | UX-audit leftovers | S/OWNER | 6 rows already split into own issues; 2 done; 4 open/stale-premise | still open |
| #4651 | Rust/C source-text pins outside 2 known files | L | 1 moot, 4 partial, 9 untouched | still open |
| #4669 | health/resilience lane close-out (33 PRs) | HW | ~20 HW proofs owed, 6 parked, ~16 follow-ups (4 spot-checked open) | still open |
| #4703 | migrate_wifi_guardian awk NAME:TYPE split | M | open, unchanged | still open |
| #4704 | widen_control_secret_env_modes needs a removal condition | S | open, unchanged | **CLOSED, PR #4912** |
| #4705 | jasper-usbmic.service enables unconditionally | CLOSE | already fixed pre-lane | **CLOSED, Wave 0** |
| #4706 | python-runtime.sh bare array expansion, bash 3.2 | M | open, unchanged | still open |
| #4707 | delete first-party-runtime.sh's 581-line seam | L | owner already said go; untouched | still open |
| #4708 | pi-run-diagnostic.sh may mis-quote a remote arg | S | open, confirmed | **CLOSED, PR #4912** |
| #4709 | jasper-camilla.service still Requires= its reconcile dep | CLOSE | already fixed pre-lane | **CLOSED, Wave 0** |
| #4710 | bound the RT→fdatasync xrun channel; add xrun_drops | M | open at filing | investigated (PR #4916): **premise stale, no such channel exists** — recommend closing, not yet closed |
| #4711 | bound the provider-PCM asyncio.Queue in voice/_base.py | S | open, unchanged | still open — handed to the voice-cleanup lane |
| #4712 | resilience park writers never record unparked_at | S | outputd half done pre-lane; camilla half open | **CLOSED, PR #4916** (camilla half + generalized `jasper-unpark`) |
| #4713 | no udev rule re-triggers reconcile on BT adapter add | CLOSE | already fixed pre-lane | **CLOSED, Wave 0** |
| #4714 | add one RESTART_POLICY table over reboot-ladder units | CLOSE | already fixed pre-lane | **CLOSED, Wave 0** |
| #4715 | add test_timeout_contracts.py (AST walk) | CLOSE | already fixed pre-lane | **CLOSED, Wave 0** |
| #4716 | doctor reader tri-states: 5 readers stuck two-state | S/M | 4/5 already tri-stated pre-lane | investigated (PR #4916): **re-scoped to zero rows** (5th must not soften NN-1) — recommend closing, not yet closed |
| #4717 | HomeAssistantStatusCache carries no sampled_at | S | open, unchanged | `sampled_at` landed as groundwork (PR #4916, no consumer yet, Closes downgraded to Refs); **issue reopened after an accidental auto-close** |
| #4718 | audio_health.py god file + prose-asserting tests | L | unchanged, 3,095 lines / 239 prose asserts | still open — duplicate of #4816 item 18, converge |
| #4719 | ratchet the layers-contract exhaustive_ignores list | M | open, unchanged | still open |
| #4720 | converge the two env-bool parsers | M | open, unchanged | still open |
| #4721 | platform/uds.py vs status_socket.py cap disagreement | S | open, 256 KiB vs 1 MiB | **CLOSED, PR #4916** (converged UP to 1 MiB — design reversed after review) |
| #4722 | converge scattered path-literal declarers | M | not re-verified, 8 groups | still open |
| #4723 | cut a volume_floor leaf, break `# lazy: cycle` import | S | open, unchanged | **CLOSED, PR #4902** |
| #4724 | google_setup.py comment references a moved helper | S | open, confirmed | **CLOSED, PR #4915** |
| #4725 | 3 atomic-write sites hand-roll tempfile+rename | M | open, unchanged | still open |
| #4726 | model_downloads.py inline registry imports (leaf inversion) | M | open, confirmed | still open |
| #4727 | audio_health.py hardcodes fanin's health-broken literal | S | open, unchanged | **CLOSED, PR #4916** |
| #4728 | 4 web wizards not on dispatch_get/post seam | S/M | open, confirmed | **CLOSED, PR #4915** |
| #4729 | sound-profile main.js still one 5,642-line file | M | open, unchanged | still open |
| #4730 | grouping snapshot has no left/right delay_ms | M | open, unchanged | still open |
| #4731 | sound_setup.py carries 77 dead active-speaker route refs | M | open, exact count confirmed | still open |
| #4732 | sound-profile main.js has 27 raw fetch() calls | S | open, confirmed | **CLOSED, PR #4915** |
| #4733 | correction_crossover_v2.py still 6,112-line god file | L | open, grew slightly | still open |
| #4734 | 3 wake-leg-default tables unconverged | S | open, all 3 confirmed | **CLOSED, PR #4912** |
| #4735 | Apple dongle USB id hand-copied across 5 sites | S | open, all 5 confirmed | **CLOSED, PR #4912** |
| #4736 | jasper-usbgadget-compose.sh a 2nd env-file dialect | M | not a true duplicate — deliberate safety narrowing | still open |
| #4737 | knob bridge: no event=knob.opened log; unbounded queue | M | open, both confirmed | still open |
| #4738 | is jts_turntable.py's print() secret-adjacent? | CLOSE | checked — answer is no | **CLOSED, Wave 0** |
| #4749 | resource deep-dive ranked plan (8 items) | S/OWNER/HW | top 2 done pre-lane; item 3 open | item 3 landed (PR #4915); **issue reopened after an accidental auto-close** |
| #4815 | hardware verification: 33-PR audio-lane program | HW | all silent rows PASS; 13-item listening checklist owed | still open |
| #4816 | audio-lane cleanup: 18 deferred refactors (batch B) | L | 4 spot-checked open, 14 carried from source | still open — Wave 0 pointer comment only |
| #4860 | grouping_supervisor starvation-watch predicate diverges | M | open, filed same-day as HEAD | still open |

### 4.3 Batch C — numbered <4000 (68 issues)

| # | title | action | status at `c364bce19` | this lane |
|---|---|---|---|---|
| #2906 | retrim up to ceiling (give-back on the receipt) | S | DONE except the receipt field | still open (S-sized remainder untouched) |
| #2653 | level datum: frame-coherence + anchor lifecycle | CLOSE | both halves resolved pre-lane | **CLOSED, Wave 0** |
| #2082 | attempts-loop wiring: 6 bounded follow-ups | CLOSE | all 6 disposed pre-lane | **CLOSED, Wave 0** |
| #2133 | driver-research flow hygiene (7-item checklist) | S | 3/7 resolved, 1 unverified, 3 open | still open |
| #2567 | round_evidence masks at validity, not trusted, floor | M | open, premise confirmed | still open |
| #2800 | channel-map validation misses realistic rolloff swap | M | open, distinct from closed sibling #2801 | still open |
| #2013 | linearization claim FRAME keeps a pre-seam Lorentzian term | M | fully open, code says so itself | still open |
| #1990 | frame discipline: follow-up sites + notch-bin limit | S | disclosure done; items A/B deliberately parked | still open (parked by design) |
| #1922 | check-phase per-driver level-sanity + attribution | OWNER | P1 + operator half of P2 done; household-copy Q open | still open |
| #1869 | crossover v2 alignment evidence gaps (3) | M | gap 1 done pre-lane; gaps 2/3 open | still open |
| #3895 | Gemini echoed-vs-real close classification | OWNER | bug confirmed, pinned as intentional-but-wrong | still open |
| #1843 | wake-word "say it again to dismiss" | OWNER | unimplemented design proposal | still open (voice lane) |
| #3346 | remote PTT lifecycle (armed/ready split) | M | gap confirmed real, unimplemented | still open — NN-6-adjacent for streambox tier |
| #3698 | right-size the largest test files | L | 6/9 files done pre-lane by other PRs | advanced this lane: **PR #4929** assessed `test_ring_active_endpoint.py` (not bloat; small fold only); 2 files remain |
| #3667 | chip-AEC beam tuning: 3 unmeasured plug-in values | HW | all 3 gaps open, unchanged | still open |
| #3271 | aec-commission timing rejected twice on jts.local | HW | code fix already shipped pre-lane; re-run owed | still open |
| #3270 | AEC class-manifest experiment | HW | unchanged, hardware-blocked | still open |
| #3464 | USB gadget reopen churn resets the lane resampler | M | sidebar done pre-lane; core bug open | still open |
| #3456 | 12.5 Hz phase-modulation ladder | HW | untouched since filing | still open |
| #3444 | faint TTS clicking on jts3 | HW | fix landed pre-lane (759d90f0c); listening confirm owed | still open |
| #2353 | needle-drop pop at playback start | HW | owner-parked explicitly | still open |
| #2327 | jts3 chip-AEC SYS_DELAY re-derivation | HW | capture done 2026-09-10; re-derivation pending | still open |
| #3639 | source-toggle-under-pressure camilla-recover kill | S | core fix + 3/5 follow-ups done pre-lane | **CLOSED, PR #4905** (`--no-reload` item) |
| #3038 | short-lived volume claims need durable state | M | 2 implementation attempts reverted; attempt 3 unstarted | still open |
| #2982 | composite dual-DAC ring-transport story | HW | design shipped/live; no composite box exists | still open |
| #2489 | ring-coupled composite DAC spins clockless to SIGKILL | HW | 2/3 defects fixed pre-lane (ADR-0262); defect 2 open | still open |
| #2463 | doctor honest-observability follow-ups | M | item 1 retired pre-lane; item 2 open | still open |
| #2408 | lab Pi 5s hard-lost power, no self-recovery | HW | detection half shipped pre-lane; rest unaddressed | still open |
| #2257 | packed-24 composite child write path | HW | owner-parked, unchanged | still open |
| #3503 | pose-first sub-500 Hz feature attribution | HW | tooling shipped pre-lane; owner walk owed | still open |
| #3498 | guided human-measurement program | HW | code-complete pre-lane (WP1-5); hardware accept owed | still open |
| #3497 | SUMMED_SWEEP_PHASES measures production graph incl. preference EQ | OWNER | departure unresolved, owner Q posed | still open |
| #3665 | gating tightening-pass follow-ups (10 items) | S | 4 done/moot pre-lane, 1 wontfix, rest small | still open |
| #2301 | gating portability beyond the 7 ms ceiling | OWNER | disclosure machinery shipped; portability goal now a stale placeholder | still open |
| #2103 | sub-0.5 ms gate arrivals mislabeled DUT-internal | OWNER | unfixed exactly as filed | still open |
| #2102 | make the 7 ms reflection-gate cap portable | CLOSE | all 5 portable-contract clauses shipped pre-lane | **CLOSED, Wave 0** |
| #1969 | gating contract, 2026-07-31 research memo | CLOSE | all 3 follow-up questions dispositioned pre-lane | **CLOSED, Wave 0** |
| #1967 | null registry band-clamped 4-18 kHz | OWNER | grading/disclosure half fixed pre-lane; boost-permission half open | still open |
| #1955 | L/R room-correction decorrelation hazard | CLOSE | hazard has no live code path | **CLOSED, Wave 0** |
| #1868 | VERIFY grades measured-vs-model; a real null passes | OWNER | remedy (a) shipped pre-lane; adoption-gate Q open 4 weeks | still open |
| #1988 | constant-ε deconvolution HF tilt | HW | mechanism real but refuted as "the" cause (~19% max) | still open |
| #2902 | legacy-config tolerance deletion campaign | L | owner-ruled, not started | still open |
| #2847 | boosted graph stopping headroom proof should re-emit | OWNER | unfixed, confirmed structurally | still open |
| #2802 | timeout-model residuals (6+2 items) | S | 2 done pre-lane, 2 small open, rest recorded-not-fix | still open |
| #2767 | lab probe rigs silently measure silence on ring-armed boxes | M | mechanism unfixed; doc-coverage gap fixed pre-lane | still open |
| #2765 | declared driver sensitivities unvalidated | M | fully unfixed, confirmed live | still open — driver-protection-adjacent |
| #2757 | prescribed-on-unfitted rounds | OWNER | fully open, code says so | still open |
| #2747 | done-screen cap excludes ABSENT/UNMEASURABLE cells | OWNER | confirmed still open | still open |
| #2705 | start-over round-cap semantics | CLOSE | resolved + shipped + tested pre-lane (PR #3354) | **CLOSED, Wave 0** |
| #2683 | `_pilot_observations` publishes hard linearity_ok from snr=+inf | M | confirmed still fully present | still open — hearing-adjacent |
| #2612 | auto-level closed-loop capture calibration | CLOSE | owner-deferred twice, trigger not fired | **CLOSED, Wave 0** |
| #2575 | hifiberry_dac8x_studio latency floor/edge format | HW | code prep done pre-lane; ADR-0232 Phase-1 hw session owed | still open |
| #2574 | convergence is registered-DACs-only | M | unchanged, no work started | still open |
| #2555 | offline re-gating replay tool | M | groundwork cleared pre-lane; tool itself unbuilt | still open |
| #2431 | evaluate_benefit can't check analyzer version/calibration | OWNER | unchanged, identical to issue text | still open |
| #2428 | near_validity_floor hard-refuses in 2 admission consumers | CLOSE | both consumers deleted (v1-lane deletion side effect) | **CLOSED, Wave 0** |
| #2401 | first-ever apply anchor (option b, deferred) | L | unbuilt, unchanged, deprioritized not blocked | still open |
| #2269 | hardware-verification ledger (32 rows, A-H) | HW | all 32 rows open; 2 citations stale (F.2, G.1) | still open |
| #2479 | commission a remove/replace/re-aim repeat study | OWNER | 1 of 2 preconditions fired (#2687 shipped pre-lane) | still open |
| #2913 | delete HF sensitivity-derivation machinery (~200 LOC) | HW | precondition unmet (jts3 tweeter still -65.0) | still open |
| #2188 | passive-mains+sub profile rung + copy fix | M | both items unchanged | still open |
| #2170 | 3-way commissioning support (manual/automatic) | CLOSE | automatic half moot (v1-lane deletion); manual folds into #1703 | **CLOSED, Wave 0** |
| #2169 | listen-confirm ramp for passive full-range drivers | M | gap unchanged; owner-gated hearing-safety design | still open |
| #1822 | enclosure_kind fingerprint-load-bearing, no consumer | OWNER | lockout fixed pre-lane (#2809); consumer target deleted | still open |
| #1783 | cloud chart paints below validity floor, no marker | S | still broken; fix pattern proven elsewhere in the codebase | still open |
| #1706 | unify manual/automated commissioning flows | L | unimplemented design proposal | still open |
| #1703 | three-way support for crossover-v2 flow | L | 2-driver ceiling still hard-enforced | still open |
| #1652 | CHECK-SNR quality gate + noise-attributed VERIFY | L | acceptance-path half shipped pre-lane; 2 pieces unbuilt | still open |

### 4.4 Umbrella row tables, #4801-#4812 (146 findings)

Format: `row | status at c364bce19 | landed by`. "pre-lane" = already fixed
by unrelated work before this lane started (found during re-verification,
not built here). "—" = still open, nothing landed this round.

**#4801 — boundaries-cycles (15 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-097 | OPEN (drifted: 112 modules now, was 116) | — |
| R-074 | OPEN, unchanged | — |
| R-082 | OPEN, unchanged | — |
| R-086 | OPEN (barrel, 124 matches) | — |
| R-091 | OPEN, unchanged | — |
| R-075 | OPEN, unchanged | — |
| R-083 | OPEN, unchanged | — |
| R-084 | OPEN | — |
| R-095 | STALE — code deleted (8b15fa615) | pre-lane |
| R-080 | OPEN, unchanged | — |
| R-092 | OPEN, unchanged | — |
| R-081 | OPEN (module renamed, privates still reached) | — |
| R-077 | OPEN (LOC estimate stale: 899, not 1,698) | — |
| R-090 | OPEN, unchanged | — |
| R-089 | FIXED (env_file.py split) | pre-lane |

**#4802 — config-knobs (8 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-218 | OPEN, half-fixed (retirement row exists; doctor WARN un-expired) | PR #4902 |
| R-204 | can't fully re-verify (audit's own ledger csv not in tree) | — (OWNER Q) |
| R-214 | OPEN, unchanged | PR #4902 |
| R-215 | FIXED (VALID_COUPLINGS gone) | pre-lane |
| R-211 | OPEN, unchanged | — (overlaps open PR #4870) |
| R-213 | OPEN (doc half fixed; architecture problem stands) | — |
| R-216 | OPEN, unchanged | — |
| R-217 | OPEN, unchanged | PR #4902 |

**#4803 — dead-code (16 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-185 | OPEN, shrunk (duplicate of #4777) | — |
| R-200 | OPEN (1 of ~5 items done) | PR #4902 (2 of the 4 named aliases) |
| R-188 | OPEN, unchanged | PR #4902 |
| R-198 | MIXED — 1 sub-claim now stale (target gained real callers) | — |
| R-199 | not evaluated (informational, defers to tuning zone) | — |
| R-182 | mostly FIXED (3/4 items gone) | PR #4900 (last item) |
| R-201 | mostly FIXED (DLL fields gone); ObsMode leftover | — |
| R-175 | partially FIXED (dead backend gone pre-lane); 11-line remainder | — |
| R-197 | FIXED (commissioning_host.py deleted, v1-lane); 2 sub-items open | PR #4902 (literal converged) |
| R-190 | mostly FIXED; ROOM/DRIVER collapse open | — |
| R-191 | OPEN, unchanged | PR #4902 |
| R-196 | FIXED | pre-lane |
| R-192 | OPEN, unchanged | — |
| R-178 | FIXED | pre-lane |
| R-173 | mostly FIXED (9/10 wizard `main()`s gone) | pre-lane |
| R-174 | FIXED | pre-lane |

**#4804 — deploy-integrity (14 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-251 | OPEN (register's own claim unverified) | — |
| R-241 | FIXED | pre-lane |
| R-242 | OPEN, unchanged | — |
| R-243 | OPEN, unchanged (needs owner: keep or delete 148-LOC recovery path) | — |
| R-244 | FIXED | pre-lane |
| R-245 | FIXED | pre-lane |
| R-246 | FIXED | pre-lane |
| R-247 | OPEN, unchanged | — |
| R-249 | OPEN, appears unchanged | — |
| R-248 | OPEN, unchanged (hardcodes ALSA card 4) | — |
| R-240 | FIXED | pre-lane |
| R-252 | OPEN, unchanged (mypy `check_untyped_defs`) | — |
| R-253 | OPEN — ruled intentional, not a gap | — |
| R-255 | OPEN, unchanged | PR #4902 |

**#4805 — duplicate-primitives (30 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-155 | OPEN, unchanged | — |
| R-105 | not independently reconfirmed | — |
| R-136 | OPEN, unchanged | — |
| R-106 | OPEN, unchanged | — |
| R-107 | OPEN (canonical fn exists, unused; repoint left) | — |
| R-108 | not independently reconfirmed | — |
| R-110 | looks FIXED | pre-lane |
| R-111 | OPEN, unchanged | — |
| R-119 | not independently reconfirmed | — |
| R-140 | partially FIXED | — |
| R-121 | OPEN, unchanged | — |
| R-123 | OPEN, unchanged | — |
| R-124 | OPEN, unchanged | — |
| R-125 | OPEN, unchanged | — |
| R-127 | STALE — target files deleted (v1-lane) | pre-lane |
| R-120 | OPEN, unchanged | — |
| R-129 | not independently reconfirmed | — |
| R-130 | not independently reconfirmed | — |
| R-118 | mostly FIXED | pre-lane |
| R-133 | OPEN, unchanged | — |
| R-134 | OPEN, unchanged | — |
| R-135 | not independently reconfirmed | — |
| R-132 | not independently reconfirmed | — |
| R-139 | OPEN, unchanged | — |
| R-149 | OPEN, unchanged (clean evidence) | — |
| R-150 | OPEN, likely unchanged | — |
| R-116 | OPEN, likely unchanged | — |
| R-143 | OPEN, weak evidence | — |
| R-144 | OPEN, unchanged | — |
| R-156 | FIXED | pre-lane |

**#4806 — god-files (17 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-073 | FIXED | pre-lane |
| R-071 | OPEN, unchanged (1,697 LOC) | — |
| R-065 | partially progressed | — |
| R-048 | OPEN, some shrinkage (4,903 LOC) | — |
| R-057 | OPEN, unchanged (4,207 LOC) | — |
| R-058 | OPEN, slightly worse | — |
| R-059 | not independently reconfirmed | — |
| R-062 | FIXED — file rewritten to 37 lines | pre-lane |
| R-070 | OPEN, unchanged (2,090 LOC) | — |
| R-060 | OPEN, unchanged | — |
| R-061 | OPEN, unchanged (linter-suppressed) | — |
| R-067 | OPEN, unchanged | — |
| R-072 | OPEN, unchanged | — |
| R-056 | OPEN, unchanged (864-line `main()`) | — |
| R-066 | OPEN, some shrinkage | — |
| R-064 | OPEN, file grew | — |
| R-063 | OPEN, likely unchanged | — |

**#4807 — observability (8 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-259 | OPEN, unchanged | — |
| R-044 | FIXED | pre-lane |
| R-039 | OPEN, unchanged | — |
| R-043 | looks FIXED | pre-lane |
| R-257 | partially FIXED | — |
| R-260 | mostly FIXED (1,425→754 lines) | pre-lane |
| R-261 | OPEN, unchanged (touches a pinned Rust test) | — |
| R-263 | partially reconfirmed | — |

**#4808 — prose (3 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-234 | OPEN — register contradicts itself, needs owner | — |
| R-235 | OPEN (spot-checked 1/13, confirmed) | — |
| R-237 | OPEN, unchanged | — |

**#4809 — resilience (14 rows, plus R-014's own deep dive)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-033 | partially fixed | — |
| R-020 | STALE — target package gone | pre-lane |
| R-021 | OPEN, unchanged | — |
| R-035 | OPEN, unchanged | — |
| R-015 | FIXED | pre-lane |
| R-014 | confirmed true (NN-1); safety holds by construction, not enforcement | — (OWNER Q, adversarial review required either way) |
| R-026 | OPEN, unchanged | — |
| R-019 | STALE — file/package gone | pre-lane |
| R-028 | not independently reconfirmed | — |
| R-030 | not independently reconfirmed | — |
| R-023 | not independently reconfirmed | — |
| R-025 | target code not found (likely moved/removed) | — |
| R-027 | not independently reconfirmed | — |
| R-031 | OPEN, unchanged | — |

**#4810 — single-writer (12 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-161 | OPEN, unchanged | — |
| R-163 | OPEN — fresh drift caught (10 s timeout mismatch, live) | — |
| R-162 | OPEN, unchanged | — |
| R-168 | OPEN, unchanged | PR #4902 |
| R-167 | inconclusive (symbol may have moved) | — |
| R-157 | OPEN, unchanged (`jasper/paths.py` doesn't exist) | — |
| R-169 | OPEN, unchanged | — |
| R-172 | OPEN, unchanged | — |
| R-159/160 | FIXED | pre-lane |
| R-158 | OPEN, unchanged | — |
| R-170 | OPEN at write time, about to become moot | now moot post-#4832 (Wave 0) |
| R-165 | partially fixed | PR #4902 |

**#4811 — tests-pinning-internals (5 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-219/222/224 | partially reduced | — |
| R-226 | mixed signal, ongoing not stalled | — |
| R-227 | not conclusively re-verified | — |
| R-231 | mixed (1 sub-item moot, 1 confirmed open) | — |
| R-232 | OPEN, exact match confirmed (22 real sleeps) | — |

**#4812 — tuning-zone-structure (4 rows)**

| row | status at `c364bce19` | landed by |
|---|---|---|
| R-099 | OPEN, confirmed (in-flight elsewhere, the W5-b cutover) | — |
| R-100 | OPEN, confirmed exact duplicate | PR #4900 |
| R-103 | partially stale (destination package gone) | — (OWNER/M) |
| R-104 | OPEN, confirmed | — |

---

## 5. What landed, by wave

### Wave 0 — land what was already in flight

**#4832** (v1 commissioning lane, part 3 of 3) deleted `commissioning_run.py`,
`commissioning_evidence.py`, `commissioning_lifecycle.py` whole (~5,900
lines of product code, ~3,260 lines of their tests) — the last caller of
each died when part 2 removed the apply/verify/receipt stack, so the run
store and its transition journal had nothing left to serve.
`commissioning_evidence_store.py` shrank 1,418→617, keeping only the
artifact half eight `crossover_v2`/`correction_crossover_v2` modules still
reach. Ships ADR-0288 (supersedes ADR-0196 and ADR-0197). Review (a
documentation/comment-only pass) found six stale citations, all fixed in
the same PR: ADR-0197's own Status line now points at ADR-0288; the total-
bytes ceiling comment now explains it's a carried-over v1 figure that
doesn't bind v2's KB-scale writes; `retirements.sh` gained the matching
four-row entry for the state files install stopped provisioning. Finishes
the 21,667-line v1-lane deletion ruled on #4788/#4792.

**#4345** (dependabot ruff bump) merged green, no review needed.

**#4895** (supersedes #4879) — dependabot's python-runtime group bump
(`protobuf`, `google-auth`, `google-api-python-client`) was red because
`deploy/constraints-pi.pins` — a Pi-generated pip overlay dependabot's uv
ecosystem can't see — still pinned the old versions, tripping the #1275
cross-ecosystem drift guard. This PR cherry-picked dependabot's commit and
added a second regenerating the Pi pins with `scripts/align-pi-constraint-
pins.py`, co-resolving all three packages to `uv.lock`'s versions.

**Wave 0 closes/comments** — one conductor-run Sonnet agent, REST API
only: 21 issues closed with an evidence comment each (§4 marks each
`**CLOSED, Wave 0**`), 8 umbrella issues got a first evidence comment
(without closing — umbrellas never auto-close), two new tracking issues
filed (#4893 hardware-evening checklist, #4894 the #4324 residuals).

### Wave 1 — small mechanical PRs

**#4900 (1b, dead code + tiny convergences, Sonnet).** Deleted
`VOLUME_MIN_DB` (test-only reader), converged `POSITION_AXES` onto
`contracts.py`'s definition (deleting the parity test that only proved the
duplication, not prevented it), deleted `avahi_service.reload_avahi()` and
the `reload`/`reload_avahi` boolean it threaded through 4 functions across
3 modules, and renamed the shared `low_memory_*` install park record to a
neutral name since two independent triggers (a low-RAM build park and the
F-S2-1 core-graph-restart park) both write into it — the rename cascaded
into the `_build_sandbox_log` event tags too once `/simplify` pointed out
that leaving those `low_memory_*`-prefixed while the record itself was
renamed undermined the rename's own point. Two items were deliberately
**not** built: `start_active_comparison_set` (now a fixture builder for 6
test files, not truly dead) and `_extract_wake_corpus.py`'s safe-to-remove
fork (protects a symlinked corpus's lexical parent, a real behavior the
shared helper lacks) — both need an owner call, not a mechanical delete.

**#4902 (1g, tests/docs grab-bag + audit S rows, Sonnet).** Extracted
`jasper/volume_floor.py` (a true leaf, zero jasper-internal imports) so
`volume_curve.py` could hoist its `sound.settings` import out of a
`# lazy: cycle` marker and into the import-linter's ranked bottom layer —
closing #4723, the one real import cycle this lane found. Fixed the
`sound/profile.py` comment that claimed EQ boosts auto-attenuate to
"clip-safe" (they don't; the trim is opt-in, default 0 dB) — a factually
wrong, hearing-adjacent comment AGENTS.md's own rule says is worse than no
comment at all. Deleted 3 more confirmed-dead pieces, converged
`fanin_coupling`'s misleadingly-named 1-line delegate and
`topology_tone`'s byte-identical duplicate of `_common.bounded_int`,
removed CI's redundant standalone mypy step (already run inside
`scripts/test-merge` moments later), and converged `config.py`'s 7
weather/transit env-key literals onto `location_state.py`'s named
constants (the module whose own docstring already claims that ownership).
Its own design section reports the batch's real finding: **roughly half
of the S-rows it was handed turned out to be already fixed or based on a
misreading of deliberate architecture** — a named test-injection seam read
as a dead write-only attribute, two intentionally-separate profiles that
happen to share values today read as duplicates, a documented
single-variant Rust enum read as dead, a raising validator assumed
interchangeable with a silent best-effort coercer.

**#4905 (1a, cues + reconcilers, Opus, adversarial — DETAILED).**
`jasper-aec-reconcile`'s invalid-provider branch disabled `jasper-voice`
with `systemctl disable --now`, so the daemon whose own `_announce_park_at_
boot` plays the NN-6 cue never ran — a shell script re-derived a predicate
the daemon (`Config.from_env()` → `VoiceProviderNotConfigured` → exit 78,
already in both `SuccessExitStatus` and `RestartPreventExitStatus`) had
owned correctly since one day before the reconciler's branch even landed.
The step-back's chosen shape was **delete the action, not teach the shell
to cue** — routing a cue through the daemon being parked is circular, and
the cheaper alternative (`jasper-cues play` from the reconcile pass) would
still be silent on a never-configured box for #4814's reason, strictly
more machinery for the same audible outcome. ADR-0293 records this: the
daemon owns the no-provider park end to end, superseding ADR-0165 on that
axis. Also closed #3639's `--no-reload` item across the ~9 remaining
enable/disable sites in `source_intent.py`, and found 2 more sites in
`audio_hardware/reconcile.py` sharing the same gap.
**The adversarial review found the original test pin was a denylist, not
a contract**: `test_reconcile_leaves_an_unusable_provider_to_the_daemon`
only asserted `disable --now`/the restart command were *absent* — a `stop
jasper-voice.service` slipped into the same branch would have passed every
test unnoticed. Replaced with a mutation-proven allowlist (the exact
command set the pass must issue, confirmed to fail when a `stop` is
injected and pass again when reverted). Explicitly **not built**: an
`ExecCondition=`-based `--changed` short-circuit for this reconciler,
because (1) its inputs include live BlueZ pairing state, not just file
content, and ADR-0226 forbids starting an interpreter inside an
`ExecCondition` to check it; (2) half its activations are recovery paths
(`OnFailure=`, a dead-dongle recover unit) with no file delta to hash — a
condition that can't see its own trigger would silently skip exactly the
passes meant to recover a deaf box, the NN-6 failure shape itself; (3) the
costly half (bouncing voice/the AEC stack) is already short-circuited by
#2703's own fingerprints.

**#4912 (1f, deploy/install + attached-hardware hygiene, Sonnet — DETAILED).**
Fixed `pi-run-diagnostic.sh`'s double-quoting bug (a remote command was
quoted once, then re-quoted as a single token crossing `ssh` a second
time) by forwarding `"$@"` straight to `systemd-run`'s exec target instead
of re-wrapping it — reproduced live against `pi@192.168.1.92` first, with
a 22-case script covering pipes, `&&`/`||`, command substitution, embedded
newlines, and quoting edge cases, confirmed the pre-fix and post-fix
transcripts were byte-identical except for the one genuinely-invalid input
(an unpaired quote, which fails the same way at a real terminal). Added
the missing removal-condition comment above
`widen_control_secret_env_modes` (#4704), converged the three independent
copies of the wake-leg default table onto one shared
`audio_profile_state.WAKE_LEG_DEFAULTS` (#4734), and converged the Apple
dongle USB id literal, discovering a 6th, previously-unlisted site
(`99-jasper-audio-hardware-reconcile.rules`, which needs the unpadded
`5ac/110a` spelling because the kernel's `PRODUCT` uevent drops the
leading zero on `remove` — confirmed by that file's own comment and a
matching test guard) while auditing (#4735). **Review caught a real
secrets-adjacent regression**: the quoting fix deleted the one
`/usr/bin/bash -lc ` wrapper layer that `fetch-pi-logs.sh`'s sudo-audit
redaction keys on by literal string match — with it gone, that redaction
would have silently stopped matching. Fixed by restoring exactly one
wrapper layer (the redundant *second* pass stayed deleted) and adding a
cross-file pin that runs `pi-run-diagnostic.sh` for real through
`fetch-pi-logs.sh`'s actual sed pattern for three argv shapes, asserting
the command text is gone and the redaction placeholder survives.

**#4914 (1c, control-server reach-throughs, Opus).** Moved all 31 genuine
`_server.` reach-throughs (a regex-counted 33 included 2 false positives on
an unrelated `restart_broker_server` local variable) out of `server.py`
into the 5 handler modules that actually own the concern they touch — AEC-
commission locks to `aec_endpoints.py`, USB-latency tracking to
`system.py`, grouping/volume gates to their own handlers — following the
ownership rule `measurement.py` already modeled (a route mixin's
concern-specific state lives in the sibling module that owns that
concern, imported directly). One site didn't fit: `_get_state`, protected
by an existing test as an intentional host seam several tests monkeypatch
at runtime — it gained one real accessor method (`_collect_state`) on the
shared contract instead of moving. `server.py` shrank 1327→1119 lines. The
full test run caught one real bug before push: a hand-rolled `_Probe`
route-mixin subclass in `test_wire_contracts.py` hadn't been updated to
stub the new `_install_profile` contract accessor.

**#4915 (1d, web wizard convergence + shared JS, Sonnet).** Converged
`google`/`transit`/`voice`/`weather_setup.py`'s hand-rolled
`_GET_ROUTES.get(route_path(...))` dispatch onto the `dispatch_get`/
`dispatch_post` seam every other wizard already uses (#4728); moved
Spotify's bounce/manual mode picker inside its `<form>` so native
submission replaces a hidden-input JS mirror (#4635); put wifi's
join-by-name fields behind a `<details>` disclosure to match saved
networks (#4635); converted all 27 raw `fetch()` calls in sound-profile's
`main.js` onto the shared `getJSON`/`postJSON` helpers (#4732); and merged
landing's private 5s safety-mute poll with `settings-status.js`'s 20s
sublabel poll into one shared poll with an `onSnapshot` hook, each surface
still rendering at its own cadence (#4749 item 3). `/simplify` folded
three duplicated 409-refusal blocks into one `handleTopologyConflict()`
handler and decoupled the merged poll's render cadence from its fetch
cadence (landing's mute check was redrawing the rarely-changing sublabels
5x as often as needed).

**#4916 (1e, resilience small hardening, Opus, adversarial — DETAILED).**
Converged `audio_health.py`'s hardcoded `"broken"` literal onto
`fanin/status.py`'s owned `DIRECT_HEALTH_BROKEN` constant (#4727); merged
two independent STATUS-read byte caps (`platform/uds.py`'s 256 KiB,
`control/audio_health.py`'s 1 MiB) into one `status_socket.py`-owned
`STATUS_MAX_BYTES`, and — after review — **converged it UP to 1 MiB, not
down**, since the cap exists to bound a wedged/hostile local daemon, not to
save memory, and nothing in the original design argued the memory was
needed back (#4721). Gave every Home Assistant `/state` card a
`sampled_at` wall-clock stamp as groundwork (no consumer wired yet —
downgraded from `Closes` to `Refs #4717` once review traced that the one
doctor check touching HA re-probes fresh and never reads the cache).
Generalized the outputd sibling's park-retirement script into one shared
`deploy/bin/jasper-unpark <record-path> <event-name>`, called by both
`jasper-camilla.service` and `jasper-outputd.service`, rather than writing
a second copy for camilla — the step-back explicitly rejected mirroring
the sibling as "the second implementation of one concern" (#4712).
**The adversarial review found a real deploy-integrity blocker, not a
style note**: the new script's install-table row sat *after* the two units
that name it in their `ExecStartPost=`, so a partial install failure would
leave a unit pointing at a script never staged — silently, since the line
is `-`-prefixed; the retired `jasper-outputd-unpark` copy was being
removed *before* the loop that installs its replacement, so a failed
replacement install would leave neither script on disk; and the harness
change written to prove the ordering fix actually deleted a real
`/usr/local/sbin` file on a live Pi host during the first test pass
(caught because `rm` wasn't in `_RECORDER_SHIMS`'s exclusion list, so the
test's own stub-everything loop silently no-op'd `rm`, which then let a
*different*, unrelated shim's internal bookkeeping call through
unguarded). All three fixed and independently re-verified: the new
script's row now lands before both units; the retired copy's removal
gates on the install loop's own success; the test harness shims `rm`
everywhere it shims `install`/`systemctl`, with a new assertion that fails
the test on any escape outside the temp root. A third finding
(`JASPER_OUTPUTD_RECONCILE_PARK_STATE` had regressed to a literal path
when the shared script replaced the old env-var-reading one) was fixed by
giving the unit one `Environment=` line both its `ExecStartPost=`/
`ExecStopPost=` substitute from. Also closed #3639's remaining follow-ups
that turned out to be already done pre-lane (found while auditing, not
built): #4710's premise (no such RT→fdatasync channel exists — the tap
event pattern it wanted already ships) and #4716's (4 of 5 readers already
tri-stated; the 5th must not soften without weakening NN-1) — both
recommended for closing, neither closed here (no `gh` access from a
builder). Follow-up #4930 filed (camilla's park reader has no
stale-record branch, fixed separately — see below).

### Wave 2 — test-suite altitude

**#4901 (2b, converge Fake\*/Stub\* doubles, Sonnet, −101 net lines).**
Audited all 10 duplicated hand-rolled test-double names (of 92 classes /
62 distinct names total) against the rule "does a wider fake pass for the
wrong reason": `FakeCamilla` (6 defs) and `FakeCam` converged cleanly onto
real `CamillaController` behavior; `FakeClock` mostly turned out to be a
**false positive** — 4 of its 5 "duplicates" fake different call-site
contracts (a bare `Callable[[], float]`, a nanosecond counter, sync vs.
async `sleep()`) that don't swap into each other's site without breaking
it, so 4 were renamed to their own contract instead of merged.
`FakeSeams`/`FakeVolume` were explicitly **not touched**: `tests/
engine_twin.py`'s own module docstring says it is deliberate ADR-0228
parallel infrastructure, migrating consumer-by-consumer until it "dies
with its last importer" — converging it now would blur that signal. A
broken per-file grep during the `FakeCam` merge missed two importers; the
full test run caught the resulting failure (8 tests reading `0.0` instead
of `-14.0` for a default that no longer matched) before push.

**#4908 (2a1, Sonnet, −73 net lines).** First caplog→structured-fields
slice: `test_crossover_v2_planner_wiring.py` (18 sites),
`test_bluetooth_engine.py` (16), `test_multiroom_reconcile.py` (14) — all
onto the pre-existing `tests/_log_events.py` parser (`event_fields`/
`event_records`/`parse_event`, already proven across 10 earlier merged
PRs). Flagged, not fixed: one negative assertion pins the absence of an
event name (`multiroom.reconcile.ring_armed_bond_blocked`) that no code
path can emit any more — a "test's subject moved" question, not a caplog
question, left for whoever knows the grouping-cutover history.

**#4911 (2a2, Sonnet, −14 net lines).** Second slice, 8 files, 48 real
conversions; deleted a hand-rolled `_events()` helper in
`test_active_speaker_commissioning_capture.py` that duplicated
`event_records()` and converged its 11 call sites onto the shared one.
Found and documented the first "left for a product change" cluster: 6
sites across `test_web_wifi_setup.py`, `test_platform_systemd.py`,
`test_tools_dispatch.py`, `test_tools_home_assistant.py` back onto plain
`logger.*` calls with no `event=` structure at all — nothing for the
shared parser to stand in for until those `jasper/` call sites migrate to
`log_event()`.

**#4926 (2a3, Sonnet — DETAILED, the most consequential slice).**
Converted the last 4 plain-`logger`/hand-rolled-`event=` call sites in
`jasper/web/wifi_setup.py`, `jasper/platform/systemd.py`, and `jasper/
tools/{__init__,home_assistant}.py` onto canonical `log_event()` calls —
which also **fixed a real bug**: the hand-rolled `ha.*` f-strings put an
unquoted, multi-word `action=` value last, so it truncated at the first
space under the structured parser; `log_event` quotes it. CI's first run
on this PR went **RED**: `scripts/test-fast` failed 4 tests, all the same
parametrized secrets test, and tracing it found a real, narrower bug in
`RedactingFilter`: `_SECRET_WORD_RE` re-matches an *already*-redacted
`password <redacted>` value (the literal token is 8+ chars, satisfying the
rule's own bare-word branch), and because `log_event` now quotes that
value's trailing `"` when it needs quoting, the rule's greedy match
swallowed the closing quote — corrupting the journal line's shape (no
secret exposed; the secret was already gone before this ever ran). Root
cause traced further: `test_audio_hardware_reconcile.py`'s own tests call
`configure_logging()` in-process with no isolation, so `RedactingFilter`
stays glued onto pytest's shared capture handler for the rest of the
session — a per-file fix (`bare_root_logger()`) protected only the one
file that had it; four sibling reconcile-suite files reproduced the same
pollution. Fixed with **one general mechanism, not five per-file ones**:
a 16-line autouse `conftest.py` fixture, `_isolate_root_logger_filters`,
that snapshots and restores every root handler's filter list around each
test — this is the general fix behind issue #4928. Filed, not fixed here:
`_SECRET_WORD_RE`'s underlying re-redaction bug is `jasper/
secret_redaction.py`, non-negotiable tier — closed separately by #4931
(below). Review added three more fixes: a quote-aware parser for the
doctor's idle-exit-hold regex (the pre-existing `holds: ([^)]+)` pattern
was replaced by a narrower `\S+` that would have silently under-captured a
quoted, space-containing value), a real emitter-driven fixture for the
doctor's test lines instead of hand-typed strings, and a missing pin for
the DEBUG-only `ha.consequential_direct` event.

**#4924 (2a4, Sonnet, −27 net lines).** Third slice, 11 files, ~50
conversions; caught a real event-name collision while converting
`test_active_speaker_program_config.py` — two different call sites in
`camilla_yaml.py` both emit under the name `active_speaker.emit_gate` (one
of them, the `active_speaker.program_emit_gate` path, is a **different**
event name than the sibling slice 2a4 itself touches in a different test
file) — reusing the wrong constant would have silently asserted against
zero records. Documented two more product-change-blocked sites (an
unquoted, multi-word `holder=`/`reason=` value truncating the same way
2a3's `ha.*` bug did, in `commission_load.py` and `staging.py`).

**#4933 (2a5, Sonnet, −27 net lines).** Fourth and final slice, 12 files,
~44 real conversions out of 48 raw-metric sites; read 17 tied-rank
candidate files before choosing, explicitly logging the zero-yield ones
(`test_correction_sweep_deconv.py` — no `event=` field at the source call
site at all) and the above-quota ones (`test_crossover_v2_stage_bridge.py`
— real yield higher than the raw metric showed, flagged as a good next
target rather than squeezed in over budget).

**#4929 (2c1, right-size `test_ring_active_endpoint.py`).** Assessed, not
gutted: unlike the five earlier "Right-size tests/…" siblings, this file
(4,104 lines, 82 tests) is **not** a stale accumulation — its own
docstrings document several past consolidation rounds already, and nearly
every test pins a distinct, individually mutation-proven incident with
forensic detail ("so a future reader does not 'fix' it," in the file's own
words). Folded one genuine duplicate pair into a 2-case parametrize and
converged 8 identical "nothing moved" assertion blocks onto two shared
helpers — a real but modest −26 lines, deliberately not forced further. A
contracts/eligibility-vs-arm-sequence split was considered and rejected:
both halves share the same local topology builders, and the file's own
header explains why those stay local.

### Secrets

**#4931 (closes #4927, adversarial — DETAILED).** Fixed the bug 2a3's
CI run surfaced: `_SECRET_WORD_RE`'s space-separated branch got a
placeholder guard mirroring `_AUTHORIZATION_RE`'s existing one — narrowly
scoped to the space branch only, since a first attempt at a whole-rule
guard broke 3 real tests by blocking the colon branch's legitimate job of
sweeping up `_KEY_VALUE_RE`'s partial redaction on a multi-word colon
value. **The adversarial review found a real regression in the first
cut**: the guard shipped as a bare *prefix* check (`(?!<redacted>)`),
which blocks a match whenever the remaining text merely *starts with* the
placeholder — so a real secret glued directly onto it with no separator
(`password <redacted>hunter2xyz`) also starts with `<redacted>` and the
guard blocked the match, **leaving the secret unredacted**. Confirmed this
was strictly worse than shipping no guard at all (on `origin/main`, the
same input redacts correctly). Fixed with a token-terminated lookahead —
`(?!<redacted>(?![^\s'"]))` — that only blocks when the placeholder is
followed by a real token boundary (end of string, whitespace, or a quote),
so a glued-on secret still matches and redacts. Discrimination proved by
hand: reverting to the naive guard and re-running failed exactly the 3
glued-secret cases and nothing else. Ran the full 88-row `CASES` table
from `test_secret_redaction.py` through `origin/main`'s pre-fix module and
this branch's fixed one side by side — byte-identical on every row, since
the guard can only affect an input that already contains the literal
`<redacted>`, and none of the 88 rows do. Flagged two things explicitly
**not** fixed here, both out of scope for a PR scoped to one rule:
`_AUTHORIZATION_RE` carries the identical prefix-shaped hole (confirmed
reproducible, filed as a follow-up), and a pre-existing, unrelated
quote-eating bug where a real secret abutting a closing quote loses that
quote to the greedy value class (narrowing the class was tried and broke
the shell-escaped-quote case it needs to keep matching).

### Late addition

**#4935** (branch `claude/debt-4930-camilla-stale-park`, closes #4930)
landed after `lane-tracker-issue.md`'s own synthesis was written — that
tracker still lists #4930 as an open follow-up, and this PR's own body was
not among the files the synthesis drew from. Camilla's park record read as
a hard `fail`/`REASON_CAMILLA_GRAPH_PARKED` whenever present, indistinguishable
from a genuine standing park, even while `jasper-camilla` was actively
running — a real doctor-verdict precision gap, not a cosmetic one. Fixed
with one `unit_active()` guard at the `check_camilla_recover_park` call
site, `warn`/`REASON_CAMILLA_PARK_RECORD_STALE` on the present-record
+ unit-active cell, mirroring outputd's sibling `REASON_..._PARK_RECORD_STALE`
pattern exactly (the step-back explicitly rejected mirroring the sibling's
full `snapshot(unit_state=...)` signature as unneeded plumbing for a
reader with no other need for it). Investigated and left alone,
per "don't defend hypotheticals": adding an "is camilla active" guard to
`jasper-camilla-recover` itself, since that script only ever runs from
`OnFailure=` after camilla's restart burst is exhausted — camilla is never
`active` at that moment by construction, so the guard would be dead code.
Review converged the new call site onto the doctor package's own
`evidence.unit_active(name)` idiom (every sibling check in the package
uses it) instead of the lower-level `service_units.unit_active(...)` the
first cut had reached for. Confirmed merged into `origin/main` at
`b9436321d` — 9 commits past this report's own `c364bce19` pin, none of
them touching `docs/audits/` — via direct inspection of the merge commit
and its own "Closes #4930." trailer, not from the assigned source set.

---

## 6. Decisions and why

The six decisions `lane-tracker-issue.md` records, verbatim reasoning:

1. **Deletions and consolidations are the priority ("obvious wins
   first"), each as a single-concern PR with simplify + code review;
   NN-tier diffs get an adversarial pass.** This caught two real blockers
   ordinary review missed: #4916's install-row ordering (§5) and #4912's
   redaction-marker regression (§5).
2. **The no-provider voice park belongs to the daemon (ADR-0293), not the
   reconciler**: one owner of the concern, one cue path — a shell script
   re-deriving a predicate the daemon already owned correctly was the root
   cause, not a missing feature.
3. **The STATUS socket ceiling converges up (1 MiB), never down**: the
   ceiling exists so a growing diagnostic surface can never blind a
   reader — it is a safety bound against a wedged/hostile local daemon,
   not a size estimate to shrink.
4. **Umbrella register rows are re-verified per row at HEAD before
   building** — half were stale (§3).
5. **Fixes to test fakes make them reject what the real object rejects**;
   a wider fake passes for the wrong reason (PR #4901's own rule, applied
   to the FakeCam/FakeCamilla merge decisions).
6. **One general mechanism over per-file patches**: the root-logger
   isolation fixture (one autouse `conftest.py` entry, not five
   `bare_root_logger()` wraps), one `jasper-unpark` script (not a second
   copy of the outputd sibling), one shared dispatch helper for the web
   wizards.

**Per-PR judgment calls — things deliberately NOT built, and why** (see
§5 for full context on each):

- **The `--changed` short-circuit for `jasper-aec-reconcile`** (PR #4905):
  its inputs include live BlueZ state an `ExecCondition=` cannot read
  without starting an interpreter (forbidden, ADR-0226), and half its
  activations are recovery paths with no file delta to hash at all — a
  condition that can't see its trigger would silently skip the very
  passes meant to recover a deaf box.
- **`start_active_comparison_set`** (PR #4900): the audit's "zero
  non-test callers" reading is now stale — 6 test files use it as a real
  fixture builder, not a hand-rolled one; deleting it would trade dead
  code for hand-rolled test doubles, the opposite of the house rule.
- **`_extract_wake_corpus.py`'s safe-to-remove fork** (PR #4900): protects
  a symlinked corpus's lexical parent directory, a real behavior the
  shared `_wake_pipeline_common` helper doesn't have; converging would
  either silently drop that protection or leave a wrapper that still
  carries bespoke logic — not a real simplification either way.
- **The Python→bash mirror for the wake-leg table** (PR #4912): considered
  shelling the bash side out to Python for full unification (the file's
  own docstring elsewhere describes exactly this pattern for other
  vocabulary), rejected because `ensure_mode_file` runs on the hot
  boot/hotplug path and AGENTS.md/ADR-0226 forbid short-lived Python
  there; named the tradeoff explicitly rather than picking silently.
- **A generation step for the USB id across udev/bash/Python** (PR #4912):
  a 6th, previously-unlisted encoding of the same id was found while
  auditing (the unpadded `5ac/110a` form a kernel uevent requires), which
  means any single-template generator would need to emit two different
  textual encodings — more moving parts than a hardware-id literal that
  only changes alongside a whole new `DacProfile` justifies.
- **The recover-script guard on `jasper-camilla-recover`** (fix for
  #4930): investigated adding an "is camilla active" guard to the
  recovery script itself; by construction the script only ever runs from
  `OnFailure=` after camilla's restart burst is exhausted, so camilla is
  never `active` at that moment — the guard would be dead code, not a
  fix. The one reachable staleness source (an external start beating the
  enqueued stop) was already accepted as "the recoverable half" by a
  prior ADR; this PR turns its consequence into a correctly-labeled
  `warn` instead of a misleading `fail`, and leaves the rest alone per
  "don't defend hypotheticals."
- **The STATUS cap direction reversed mid-lane** (PR #4916): the original
  design converged the cap *down* to 256 KiB; adversarial review reversed
  it to 1 MiB once the reasoning was examined — see decision 3 above.
- **`publish_raw_artifact`** (batch 1b): the planned deletion of this
  zero-production-caller function in
  `commissioning_evidence_store.py` was explicitly deferred until **after**
  #4832 merged (Wave 0), since #4832 was still deleting the module around
  it — sequencing, not a design decision against the delete.

---

## 7. Outstanding work, by wave

### Wave 1h — tuning-stack small finishes (S, gated on the Codex bass lane, #4870/#4872, still open)

Verbatim from `wave1-batches.md`, unbuilt:

> #2906 give-back on the round receipt; #2802 items 1+7+8; #3665 items
> 5/6/7; #1783 cloud chart floor shading (reuse `prepare_frequency_curve`);
> #2133 items 1/2/7; #1868 delete the dead `REASON_VERIFY_CROSSOVER_REGION`
> vocabulary.

### Wave 2c/2d/2e — test-suite altitude, not started

| item | size | gate |
|---|---|---|
| `test_audio_health.py` right-size, converge with #4718/#4816-item-18's split | L | none — buildable now |
| `test_voice_daemon_measurement_inflight.py` right-size | L | voice-cleanup lane (ADR-0292) owns the file |
| Private-attribute pin pilot (1,866 sites, 235 files, untouched since the audit) | M design + L sweep | needs an Opus-designed public `_status_payload()`/`/state` twin before a Sonnet sweep can start |
| Typed error-code pilot for `pytest.raises(..., match=<prose>)` (583 sites) | M product change + L sweep | needs one `code` field per exception class first; pilot on the 3 most-raised classes |

**The remaining caplog tail** (product-change sites named across the 2a
slices' PR bodies, not fixable test-side): `deconv.py:62`'s plain
`logger.warning` (3 pins, `test_correction_sweep_deconv.py`);
`commission_load.py`'s hand-rolled `holder=`/`reason=` multi-word value
(1 pin, `test_active_speaker_commission_load.py`); `staging.py`'s
matching `holder=` value (1 pin, `test_active_speaker_staging.py`);
`test_web_wifi_setup.py`/`test_platform_systemd.py`/`test_tools_
dispatch.py`/`test_tools_home_assistant.py`'s plain-`logger` call sites
(11 sites, 2a2's fixer). Each needs its `jasper/` call site migrated to
`log_event()` first — a small, independent product change per site, not a
design question.

### Wave 3 — voice-stack cleanup program

Owned entirely by the voice-cleanup lane (ADR-0292), not this one. Issues
#4777-#4781, #4711 belong there. State as measured this lane (§3):
`WakeLoop` 2,783 lines/91 methods, `daemon_main.run` 595 lines,
`OutageTracker` never fires for the kept `OpenAILiveConnection` adapter.

### Wave 4 — right-size the tuning stack (after the Codex bass lane lands or pauses)

| item | size | notes |
|---|---|---|
| God-file lane (#4806, 17 findings, ~8,000 LOC) | L, one file per PR | `correction_crossover_v2.py` (5,531→continue the split its siblings started), `runtime_contract.py` (4,494), `crossover_v2_flow.py` (4,248), `baseline_profile.py` (3,939), `sound_active_speaker.py` (3,515), `camilla_yaml.py` (3,457), sound-profile `main.js` (5,609/5,642) |
| Duplicate primitives (#4805, 30 findings) | S/M per name | `_finite`/`_finite_float`/`_finite_number` (27 definitions), `_sha256`, `_fingerprint`, `_positive_int`, `_state_path` |
| Boundaries and cycles (#4801, 15), tuning-zone structure (#4812, 4), tests-pinning-internals (#4811, 5 — overlaps 2d) | L each | see §4.4's umbrella tables for the row-by-row state |
| Function-local imports without `# lazy` | L sweep | 1,438 sites (upper bound), 248 files; two web-wizard files hold 16% |
| Ghost knobs | M | ~130 genuinely fresh non-AEC ghosts once the 80 already-ruled AEC-family ones are excluded |

**Highest collision risk with live sessions — schedule explicitly**, per
`plan.md`'s own warning.

---

## 8. Owner decision sheet

All 26 items from `plan.md`'s Wave 5, with its recommendation. The six
that matter most (★) first.

| # | item | recommendation |
|---|---|---|
| ★1 | #4809/R-014 (NN-1, hearing): `camilla.py`'s live graph-write methods have no `volume_limit` boundary check; safety holds only because all 10 call sites happen to route through a validated emitter | add the boundary check inside those two methods (~20 lines, adversarial review) so NN-1 holds by enforcement, not luck |
| ★2 | #4279 item 8 (secrets, NN-3): WiFi PSK visible on `nmcli`'s argv during the connect window | rework to pass via stdin or a 0600 temp file; do not accept |
| ★3 | #4816 item 8: `multiroom/reconcile.py` restarts `jasper-outputd` by shelling `systemctl` directly, bypassing the crash-budget restart broker | route through the broker (M, adversarial tier) — safety-adjacent, not really a decision |
| ★4 | #4814 (NN-6): first-boot box has no cue WAV before a provider is configured — silent park | ship one committed fallback WAV |
| ★5 | #4800: promote the two doctor security rows to `core=True` (deploy gate)? | yes — a misconfigured box should fail install loudly, the ruling's own consequence |
| ★6 | multiroom spike harness (1,176 lines), `ROUTE_BITPERFECT_DECLARED` (45 lines), `test_rust_runtime_panic_freedom.py` (532 lines) | delete the spike + its doc section; delete `ROUTE_BITPERFECT_DECLARED` including the `.env.example` enum value; keep the Rust panic test as a documented exception in its own docstring |
| 7 | #4502 nod-to-close (native dynamic bass already shipped, ADR-0286/0287) | close |
| 8 | #2847 auto re-emit a headroom-regressed boot graph from baseline? | yes |
| 9 | #1868 gate VERIFY adoption on a spec failure? | no — disclose only |
| 10 | #3895 Gemini close ambiguity (echoed vs. real close) | silent-retry default |
| 11 | #1843 wake-word "say it again to dismiss" | not now |
| 12 | #2757 (prescribed-on-unfitted rounds) | see plan page for the 3-way recommendation |
| 13 | #2747 (done-screen cap coverage) | see plan page |
| 14 | #2301 (gating portability beyond 7 ms) | close in favor of shipped disclosure |
| 15 | #2103 (relabel DUT-internal) | S, relabel |
| 16 | #1967 (boost permission below 4 kHz) | see plan page |
| 17 | #2431 (analyzer-version/calibration enforcement) | re-scope to the five enforced properties |
| 18 | #2479 (commission the repeat study) | see plan page |
| 19 | #1822 (enclosure_kind dead field) | close |
| 20 | #1922 (naming the driver in a presence-gate refusal) | fine — a direct-observation gate is the exception to the no-hardware-noun rule |
| 21 | #3497 (SUMMED_SWEEP measures production graph incl. preference EQ) | keep current, re-document |
| 22 | #4782 | see plan page |
| 23 | #4808/R-234 | see plan page |
| 24 | #4131 | see plan page |
| 25 | #4139 | see plan page |
| 26 | #4362, #4387, #4533, #4534, plus the two "confirmed true, is it a problem?" items (jasper-system-web restarted by the low-RAM unpark: accept; grouping_supervisor's single-predicate reliance: accept) | see plan page for each |

Full text and reasoning for every item: the plan page,
https://claude.ai/code/artifact/90c96223-06f0-4216-a696-cf55952b9384.

---

## 9. Hardware evening

Tracked on **#4893** (filed Wave 0). Full list, from `plan.md`:

#4783, #4865, #4124, #4385, #4669, #4815 (audio-lane L rows), #3456,
#3444 (confirm by ear — the seam-click fix already landed), #2353, #2327,
#2982, #2489 defect 2, #3667, #3271 (re-run and read `competitor_lag`),
#3270, #2575 (ADR-0232 phase 1 on jts3), #2269 (32 rows; section D likely
already satisfied by ordinary operation; F.2/G.1 citations are stale),
#2408, #2257, #3503, #3498 (one acceptance walk), #2913 (blocked on jts3's
stored −65.0 seed), #1988, #2612 (close, parked twice — no hardware
session actually needed, listed here only so it isn't lost).

---

## 10. Process facts and traps for the next session

- **Agent cap 3.** 5 tripped the session's concurrent-agent limit at
  ~06:40 on 2026-09-11, after ~4.5h — the whole session went dark
  mid-work. Never let a builder run the `/simplify` or `/code-review`
  *skills*, or spawn sub-agents; run the lenses inline and review from the
  conductor.
- **The builder template's own step 3 was wrong, and got silently
  corrected mid-lane.** `wave1-builder-template.md` (§2, verbatim above)
  tells a builder to invoke the `simplify` Skill tool directly on its own
  diff. Every Wave 2 PR body instead says it applied the four lenses
  manually "per this task's instructions" — the conductor's briefs for
  Wave 2 dropped the Skill-tool call, matching the agent-cap lesson above
  (a skill invocation can behave like a sub-agent spawn for this purpose).
  **Strike step 3's Skill-tool instruction before reusing this template.**
- **Builders park on backgrounded test runs.** Brief every builder to run
  `scripts/test-fast` in the foreground with a 600s timeout. If one goes
  silent with commits already made, stop it and spawn a finisher on the
  same worktree rather than a fresh one — a fresh worktree loses the
  paper trail (§2c1's `test_ring_active_endpoint.py` PR and §5's #4916
  both had a finisher pick up after a rebase).
- **Commit messages with "Closes #N" auto-close on merge even when the PR
  body says `Refs`.** Bit this lane three times: #4803 (still open, not
  yet reopened — flagged in §4.1 and §1), #4749 and #4717 (both caught and
  reopened). GitHub reads the commit trailer, not the PR body's own
  `Refs`/`Closes` section — grep every commit message in a PR for `Closes
  #` before pushing, not just the PR body's stated intent.
- **Install-table ordering rule**, discovered by #4916's adversarial
  review: a script's install row must land **before** the units that name
  it in an `ExecStartPost=`/`ExecStart=`; a retirement (`rm -f` of an old
  copy) must land **after** the install loop that stages its replacement,
  gated on that loop's own success; never a bare absolute path in the
  install lib (route it through a named directory variable, e.g.
  `LOCAL_SBIN_DIR` beside the existing `SYSTEMD_DIR`).
- **The shared `.venv` lacks `openai.types.live`** — `tests/
  test_openai_live_session.py` fails to collect locally in every agent
  worktree (they carry no venv of their own and point at
  `/Users/jaspercurry/Code/JTS/.venv`). CI is the truth for the full lane;
  `uv sync --extra full --extra streambox` in the main checkout fixes it
  locally.
- **`gh` quota is one token for all sessions.** Agents never call `gh`
  directly — builders push with `git push` and report the SHA; the
  conductor owns the PR-open/merge/close/comment API budget through one
  slow waiter (§2's `ci_wait.sh`) and one REST-only closing agent (Wave
  0). This lane's Wave 0 close pass used 54 calls, the wrap-up pass 24 —
  both under a ~40-call-per-agent budget with a `sleep 4` between calls.
- **Worktree cross-write guard.** Every builder worked in its own
  worktree/branch with a hard "never `cd` elsewhere" rule; a conductor
  action that needs to touch a different worktree uses the Bash tool with
  an absolute path (or the `EnterWorktree` tool where available), never a
  shared working directory two agents could collide in.
- **The root-logger isolation trap is general, not per-file.** Any test
  that calls a product `main()` in-process without `bare_root_logger()`-
  style isolation is exposed to `configure_logging()`'s filter mutation
  persisting on pytest's shared capture handler for the rest of the
  session (§5, #4926). The fix — one autouse `conftest.py` fixture,
  `_isolate_root_logger_filters` — is now in place, but the class of bug
  (a test calling a real `main()` with no isolation) was only found because
  a *different* PR's exact-match assertion made the pollution visible;
  a substring-based pin would have stayed silently passing.
- **`_SECRET_WORD_RE`'s prefix-guard hole has a documented, unfixed
  twin.** `_AUTHORIZATION_RE` carries the identical
  `(?!(?:[A-Za-z]+[ \t]+)?<redacted>)`-shaped prefix guard #4931 found and
  fixed on `_SECRET_WORD_RE` — confirmed independently reproducible
  (`redact_secrets("Authorization: Bearer <redacted>hunter2xyzSECRET")`
  returns the secret unredacted). Filed as a follow-up, not yet built.

---

## 11. How to resume in one page

1. `git fetch origin && git merge-base --is-ancestor origin/main HEAD` (or
   rebase if behind) — this lane's own worktree convention, per AGENTS.md.
2. Read `<TRACKER_ISSUE>` first — it is the live ledger; this file is
   frozen at `c364bce19` and will not reflect anything closed after
   landing.
3. Read the plan page (https://claude.ai/code/artifact/90c96223-06f0-4216-a696-cf55952b9384)
   for the full five-wave plan and the 26-item decision sheet's complete
   reasoning (§8 above is the terse version).
4. `origin/main` had already advanced 9 commits past this report's own
   `c364bce19` pin by the time it was frozen — #4935 (closes #4930, §5's
   "Late addition") among them, none touching `docs/audits/`. Start any
   new work from current `origin/main`, not from `c364bce19`.
5. Reopen #4803 if it is still closed (§4.1, §10) — the umbrella issue's
   real rows (R-192, R-190 ROOM/DRIVER, R-201 ObsMode, R-175's remainder,
   R-198's re-verify, R-200's other two aliases, R-197's remaining
   sub-item) never landed; only the auto-close did.
6. Post closing comments on #4786 and #4791 (§4.1) — both were found
   resolved (false premise; already fixed pre-lane) but neither issue was
   actually closed, since builders have no `gh` access.
7. Check whether the Codex bass lane (#4870/#4872) merged or paused — it
   gates Wave 1h and Wave 4, and #4863/#4866/#4867 (§4.1) can only close
   once #4870 itself lands.
8. Pick the next wave by what's actually buildable now: Wave 2c
   (`test_audio_health.py` right-size, no gate) and the owner decision
   sheet (§8, one sitting, mostly one-word answers) are the two
   ungated blocks of work remaining.
9. If resuming Wave 1/2-style batches: reuse `wave1-builder-template.md`'s
   brief (§2, verbatim) with the step-3 Skill-tool fix applied (§10) and a
   fresh evidence file re-verified at the new HEAD — main moves
   ~300 commits/day; nothing in `issues-A/B/C-status.md` should be trusted
   past this file's own pin without a fresh grep.
10. Before merging anything: `scripts/test-fast` foreground with a 600s
    timeout, non-negotiable-tier diffs (cues, install.sh, secrets, DSP
    output path, volume clamps) through `/adversarial-review` on top of
    `/simplify` + `/code-review` — every one of the four adversarial
    passes this lane ran (§5) found a real bug ordinary review missed.
