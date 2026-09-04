# Handoff prompt — JTS tuning right-size: fresh eyes on landing the program

Paste everything below this line into a fresh Claude Code session (Fable) on
the JTS repo. It is written for that agent, not for the owner.

---

You are a **fresh pair of eyes** on the **tuning right-size program**
(tracking issue [#3769](https://github.com/jaspercurry/JTS/issues/3769)) on
`jaspercurry/JTS`, a Raspberry Pi smart speaker whose owner tunes a two-way
active speaker with a turntable-driven measurement toolbox that an LLM drives
over SSH. Six waves have landed. Another orchestrator session is still
landing the tail of the current plan (§5 below); you do not touch its work.
Your job is to look at the whole program at HEAD, say honestly whether it is
on the right path, and write the plan that lands it. Read this whole prompt,
then AGENTS.md, then the evidence branch, before touching anything.

## 0. How you work (non-negotiable for this session)

- **You are the architect, conductor and judge. You do not implement.**
  Every investigation, read, measurement, draft and implementation is
  delegated: **Sonnet** for prose, docs, tests, tiled reads and prose review;
  **Opus** for code, relocations, adversarial and constants review. **Never
  spawn a Fable subagent** — Fable is expensive; one Fable (you) orchestrates
  and judges, nothing else. If you catch yourself reading a 2,000-line file
  or writing a diff, stop and delegate it.
- Every code PR runs **`/simplify` then `/code-review`** (medium) before push,
  in the lane that wrote it, with both recorded in the PR body; you review
  again before merge. Non-negotiable rows (the closed list in AGENTS.md) also
  get `/adversarial-review` and an owner hardware pass. There is no
  zero-findings rule; every finding is fixed or an explicit wontfix.
- **What "good" means here:** modular, clear separation of concerns, one
  source of truth per fact, 80/20 high quality (the least machinery that
  works, shipped and verified on hardware), observable (`event=` logs,
  `/state`, doctor lines, named refusal reasons and exit codes) and resilient
  (fail closed on the non-negotiables, disclose and continue everywhere
  else). Not bloated: comments only for non-derivable constraints and
  why-pointers; no narration, no history, no prose addressed to a reviewer.
  Deletions and consolidations are small changes regardless of line count.
- The owner directs; you decide within that. Ask only when readings would
  produce materially different work. Never deploy from an agent
  (`scripts/deploy-to-pi.sh` is the owner's). Never merge red `main`.
- Commit trailer and PR footer: use the session's own attribution as given in
  your system reminders; GitHub comments end with the Claude Code footer.

## 1. Where the record lives (read in this order)

1. `AGENTS.md` at HEAD — operating rules. The non-negotiables are a closed
   list; everything else is a default. Its Docs rule bans a handoff-doc tier
   in the repo, which is why this prompt lives on an evidence branch.
2. Evidence branch `claude/tuning-rightsize/recon-reports` (**never merge
   it**), directory `tuning-rightsize-recon/`:
   - `AUDIT-FRESH-EYES.md` — the deep audit that reset the program (what was
     wrong, with evidence).
   - `TARGET.md` — the destination: **ten binaries, one verb family each**,
     the conventions every tool obeys, the round directory as memory, the
     boundaries as an import rule, what is out of scope by design.
   - `PLAN-FRESH-EYES.md` — the three-wave plan that executed the target
     (waves 1–3), its Held list and its Dropped-and-why list.
   - `WAVE-LOG.md` — append-only log of every wave, decision and lesson.
   - `EXEC-W5.md`, `EXEC-W6.md` — the sub-agent briefs (branch/PR mechanics,
     the prose bar, deletion rules, relocation proof); reuse their shape.
   - `astsame.py`, `astmove.py` — the AST proofs (prose-only diff; moved
     bodies identical) used to accept relocations.
   - `HANDOFF-PROMPT.md`, `HANDOFF-W4.md` — earlier handoffs, kept as
     backlog evidence and mechanics lessons, not as the plan.
3. ADRs 0227–0231 in `docs/adr/` — the owner rulings the program surfaced.
4. Issue #3769's comments — the owner-facing status per wave, newest last.
5. `docs/measurement-loop-doctrine.md` (the authority model and the loop),
   `docs/tuning-methodology.md` (the steps an LLM walks),
   `docs/tuning-operator-runbook.md` (the generated tool menu — never
   hand-edit the table; `scripts/generate-tuning-tool-menu.py --check`).

## 2. The vision, in one paragraph

An LLM tunes the speaker the way a scientist works: it recommends an
experiment, the toolbox measures it, and the measurement decides. Every
capability is a CLI subcommand with argparse-documented inputs, a named JSON
artifact beside the round (`ARTIFACT_BY_VIEW` in
`jasper/cli/round_views/_common.py` is the one table), a generated menu row,
and a pointer from the methodology at the step where the LLM reaches for it.
One exit vocabulary (`jasper/cli/_refusal.py`: 0 ok / 1 refused / 2
unreadable / 3 unwritable, with a named `reason`). Truth layer
(`jasper/audio_measurement`) < engine (`jasper/active_speaker`,
`crossover_v2/`) < surfaces (`jasper/cli`, `jasper/web`), pinned by
`tests/test_runtime_import_closure.py`. Evidence is JSON bundles under
`/var/lib/jasper/active_speaker/sessions/`; no database until a
cross-session question asks for one. Room correction is a separate product.

## 3. What has landed (broad strokes; do not redo)

- **Waves 1–3 (PRs #3837, #3914, and twelve wave-3 PRs):** −74k lines. The
  spent doc tier deleted, zero-caller code deleted with SPENT / SUPERSEDED /
  PROMOTE verdicts, three prose passes to the AGENTS.md bar, source-text pins
  converted to behaviour pins, runtime severed from the tuning engine
  (`state_paths.py` leaf, import-closure pins), `jasper-round-views` given
  the one artifact table and an `inventory` view, PEQ and playback kernels
  moved to the shared measurement layer.
- **Fresh-eyes reset:** the deep audit, TARGET.md and PLAN-FRESH-EYES.md
  replaced the old 362-line PLAN.md. Its waves executed as WAVE-LOG waves
  4–6:
  - **Plan wave 1 (close the stalls, 13 PRs):** one exit vocabulary with
    `stage`/`StageFailed` hoisted into `_refusal.py`; the topology door read
    by role; ADR-0231.
  - **Plan wave 2 (one binary per verb family, 12 PRs):** `jasper-round
    bank`; `jasper-arm-walk`, `jasper-delay-sweep`, `jasper-gate-sweep`,
    `jasper-classify-features`, `jasper-project-ring`,
    `jasper-close-reference`, `jasper-forward-model`, `jasper-read-distortion`
    retired into `jasper-round-views <view>` and `jasper-angle-capture
    serve`; `round_views.py` split into a package (`--help` byte-identical);
    `OWN_EXIT_VOCABULARY` down to the one human-only door.
  - **Plan wave 3 (god files and boundary pins, 8 PRs):** `startup_load.py`
    split at its two lifecycles (`commission_load.py`); `program_analysis.py`
    (4,977 lines) a package of ten; the declaration vocabulary and the
    driver-research prompt out of `staging.py` / `driver_safety.py`; the
    conductor test split into eight topic files; the re-emit anchor block
    into the engine; boundary pins for the active_speaker/correction edges.
- **Mechanics that worked:** file-disjoint lanes in parallel, each in its
  own worktree from fresh `origin/main`; lanes stop on a false premise and
  say so (four did, correctly); relocations accepted only on AST identity of
  moved bodies plus resolved deferred imports, no shims; batches of
  approved PRs landed through ONE integration branch and one CI run (the
  pytest lane is ~30 min and the sequential-rebase tax was the bottleneck).

## 4. What is open, and why

**Held by plan (owner decision or hardware time):**
- `jasper-null` → engine module + `jasper-measure --kind null` (NN:
  excitation ledger, `program_admission`). A rewrite; needs hardware time.
- `scripts/run-crossover-round.py` still maps twelve exit codes onto the
  shared four with the sub-tool's name in the trail. Laptop script; low harm.
- `bass_extension_bench --live` stub (#1738): wire or delete; owner's call.
- `docs/HANDOFF-bass-extension-plan.md` and `docs/bass-extension-waves/**`
  stay (ADR-0229). Do not touch.

**Waiting on the owner:**
- #3991 (NN row 3.7): six inline `volume_limit` clamps → one helper on the
  existing `ensure_volume_limit_db`. Adversarially clean (six emitters ×
  fourteen value classes byte-identical). Merges only after the owner deploys
  its branch and confirms `volume_limit: 0.0` in the emitted config.
- #3934 (owner's PR) needs a trivial rebase: wave 2 retargeted one line of
  its test.

**Follow-ups recorded, not yet rowed** (verify each at HEAD; the tree wins):
- 65 modules import from the `program_analysis` facade; repoint them and
  shrink the 312-line `__init__`.
- Three more copies of the mux socket-path literal (`mux.py`, `renderer.py`,
  `cli/system_soak.py`) → `uds.MUX_CONTROL_SOCKET_PATH`.
- `sound_setup` calls the sync mux helpers from async defs under
  `asyncio.run`; sync→async convergence needs its own row with a
  thread-bridge decision. `FaninGateContext` lease vs `measurement_window`:
  five semantic differences documented in #3992's body.
- `startup_load.reemit_staged_startup_anchor(args: argparse.Namespace, …)`
  prints operator text from the engine; should return a report and let the
  CLI print.
- The v1 level-match half of `correction_crossover_backend.py`
  (`run_level_match`, `configure_targets`,
  `driver_sweep_locked_main_volume_db`) has zero production callers; a
  deletion row with verdicts, scoped so heavy tests stay untouched.
- By-name refusal arms in `round_views/delay.py`, `classify_features.py`,
  `distortion.py` should consume `_common.refused_by_name` (one spelling).
- Three private path-or-stdin JSON readers; `CommissioningRunStore.
  replace_current` is test-only; `cli→web` held items
  (`WiredStimulusCapture`, `crossover_v2_status_block`, `GRADE_*`).
- CI: three spawn-bound test failures in one day under runner fd/process
  exhaustion late in the suite (`tests/test_wifi_guardian_script.py`
  EMFILE/EAGAIN retries; airplay hook and arm-walk park tests time out).
  A leak diagnosis is in flight (§5); the fix is a real PR once the leaking
  fixtures are named.
- `TARGET.md` cites the wrong pin for the audio_measurement boundary claim
  (the fold fixes it).

## 5. What the other orchestrator is landing right now (hands off)

Session `session_01R8RQ2FPdsucCApERfUqwH3` owns, until it reports done on
#3769: the wave-2 fold into `WAVE-LOG.md` on the evidence branch; the
fd-leak diagnosis and its fix PR; #3991's merge after the owner's pass. Do
not open PRs on those concerns, do not push to `claude/tuning-rightsize/*`
branches, do not edit `WAVE-LOG.md`. Everything in §4's follow-up list is
free for you to plan; coordinate by posting on #3769 before dispatching a
lane that touches a file named in an open PR.

## 6. Your deliverable

Act 1 — **verify the lay of the land at HEAD**, delegated: tile the reads
(Sonnet), verify each claim above and each TARGET.md row against the tree
(Opus for code claims), and write `tuning-rightsize-recon/LANDING.md` on the
evidence branch: a table of TARGET.md's ten binaries and conventions with a
TRUE / PARTIAL / FALSE verdict and the evidence line for each; the same for
the boundary rule and the round-directory-as-memory rule; what the last
three waves got right and wrong, with the two or three things you would have
done differently; and where the program actually is against the vision in
§2 — including whether the plan's Dropped list still holds.

Act 2 — **the landing plan**: what remains between HEAD and "done", as rows
with a tag (D delete / P prose / R relocate / W rewrite / NN), a proof, a
gate and a size, ordered by value; a **definition of done** for the program
the owner can sign; and an explicit list of what you recommend NOT doing
(with why). Say plainly if the right answer is "stop here". Post a short
summary on #3769 and wait for the owner's nod before executing anything.

Act 3 — only after the owner approves: execute in file-disjoint lanes with
the mechanics in §3, batched landings through one integration branch, one
status per wave on #3769, each wave folded into `WAVE-LOG.md`.

Constraints that carry: `tests/voice_eval/` opens paid sessions — never loop
it, state the cost first. Keep PRs single-concern and under ~400 changed
lines unless the change is a deletion. `git fetch origin` and rebase before
every push; main moves under you (another session merges unrelated web and
control PRs hourly). Run `scripts/test-fast` before pushing; trust only the
final `==> <lane>: N passed` sentinel. The shared laptop-side venv is not
yours; create your own under your scratchpad.
