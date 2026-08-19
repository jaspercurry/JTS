# Next-session prompt — crossover-optimization stack: finish landing, then the hardware sweeps

**Written 2026-08-19 (late), by the conductor session that built the stack.**
You are a fresh session with no context. This file + the pointers in it are
everything. Read it fully before any action.

## 0. The standing method (binding — the handoff rule requires restating it)

- **Conductor rule:** the session-driving model conducts ONLY — plans,
  diagnoses from evidence, dispatches subagents (explicit models: Opus for
  complex/judgment work, Sonnet for mechanical), reviews results, records
  decisions. The conductor never implements product code.
- **Adversarial gate rule:** EVERY PR (code or docs, any size) passes an
  independent adversarial review in a separate agent to **0 blockers / 0
  should-fixes** before merge. Fix rounds go to the original implementer;
  deltas to the same reviewer. **The conductor posts the disposition as a PR
  comment WHEN THE REVIEW RETURNS** (an unrecorded review did not happen).
  Hearing-safety / CamillaDSP-graph / level-math changes escalate to a
  three-lens panel (correctness, hearing-safety, resilience) — #2733 is the
  worked example; a purely protective refusal-only gate may run a single
  hearing-safety-aware lens with the ruling recorded (#2736 precedent).
- **Owner's values:** saturation (context: delegate to keep the window lean;
  system: bounded CPU/mem/IO), single source of truth, separation of
  concerns, 80/20 right-sized simplicity (COAH bar in AGENTS.md), elastic /
  modular / observable / resilient / performant. **Supersede-and-delete**
  (2026-08-19 ruling): new code replaces old → the old dies in the same PR
  or a NAMED follow-up; plans carry a Deletions section. **Flatness is a
  profile, not a scalar** (2026-08-19 ruling): per-chunk deviation +
  worst-chunk max-norm + tilt + RMS; acceptance = target improves AND no
  chunk regresses; never let an average hide structure. **Attribution rule:**
  state the artifact a number came from, or mark it inference.
- **Merge discipline:** this repo's branch protection does NOT block merges
  on pending checks. `gh pr checks <n> --watch --fail-fast` must exit 0 in
  an explicit conditional BEFORE `gh pr merge` (never chain grep→merge
  unconditionally — the #2733 near-miss). Verify `state=MERGED`. Never
  delete a branch that is another PR's base (stacked rule). Capped-file
  diffs recount on the MERGE TREE (AGENTS item 12).

## 1. The vision (ratified, on main)

The **linearization pipeline** — `docs/active-speaker-tuning-layers-design.md`,
section "The linearization pipeline — seed → crossover science → EQ
(ratified 2026-08-19)" (merged as #2729; status labels tree-verified):

- **P1 seed** (EXISTS): driver declarations → safe starting crossover.
- **P2 crossover science** (substrate EXISTS, search lands tonight): per-driver
  complex captures → offline forward model + search over polarity/delay/
  Fc/slopes → played confirmation rounds. BOTH-AND with EQ, in that order.
- **P3 EQ the residue** (partially EXISTS): full trusted band (357 Hz–16 kHz),
  classify-first (defect vs interference), per-driver placement, frozen-
  reference grading, cuts free / boosts parked on the owner's headroom
  decision.

The prescriber is **LLM-driven**: any competent LLM reads the evidence
packet + knowledge, proposes candidates; deterministic validators own
bounds, safety, statistics, rollback. The knowledge architecture (owner-
ratified): a **universal pack** (`docs/crossover-agent/`, to be built —
abstracted lessons only, works for any speaker) + a **per-speaker
graveyard** (jts3's instance = `PACK-DRAFT-crossover-agent-graveyard-2026-08-19.md`
on branch `claude/night-driver` — hypotheses measurement already executed;
READ IT before proposing anything acoustic).

## 2. What landed today (all 0/0-gated, dispositions on each PR)

#2726 prescription wiring · #2728 prescriber-CLI logging (A10) · #2729
pipeline design doc · #2730 cuts-only Q ceiling 8.0 · #2731 silent-slip
guard (floor 4→2 samples) · #2732 per-driver captures at any angle (both
mover modes) · #2733 giveback band fix (three-lens panel; the brightness
root cause) · #2736 Fc apply-time protection floor gate · #2735 candidate
space + forward model (parity 3.55e-15 dB vs shipped physics on banked
arms) · plus #2737 (another session: mark-VERIFY badge cap). Main is green
(portaudio apt hangs recur — issue #2727 tally; remedy: `gh run rerun
--failed`, verify the step-level cause first).

## 3. In flight at handoff (finish these first)

- **#2739 objective + search** (stacked on merged #2735; builder was
  rebasing onto main + retargeting base; then needs its GATE — brief the
  reviewer with: the flatness-profile ruling, the pooling-cancellation
  finding (see §4), the armrun rung pinning ρ=−1.000 with ranking authority
  `bracket_only` stamped in every prediction doc, chunk width imported from
  the spec fraction, and the floor-completeness contract from #2735's delta
  ("the search must emit candidates complete w.r.t. floor-declared roles").
  After merge: DELETE the retained #2735 branch
  (`claude/crossover-candidate-forward-model`).
- **#2741 capture-trigger CLI** (`jasper-angle-capture`): in fix round —
  stat-first spool cap + the #2728-style subprocess logging pin + two nits.
  Reviewer's worktrees kept at /private/tmp/jts-gate-2741*. Then delta →
  merge.
- **#2740 per-driver full-band cuts-only prescription route**: gate was
  mid-review (fresh Opus). Then fix round(s) → merge. Note its deliberate
  boundary: round CONSUMPTION of the driver class is fail-closed unwired
  pending the compose-vs-replace ruling (architect's to make, owner
  informed).
- **apply-path PR** (branch `claude/fc-apply-path`, existed unpushed-PR at
  handoff; builder was closing out): re-points the dormant
  `fc_selection` route + slope on the same writer + Undo coverage + binds
  the #2736 floor gate. Hearing-safety-aware gate when it opens.
- **THE BOUNDARY: the walk-take PR is NOT started** (owner's explicit
  order: land everything up to it, stop there). Its founding documents:
  #2741's blocker framing (verified accurate by its gate) + the P2 doc
  section. The problem: per-driver pose captures are reachable only under
  PHASE_LATERAL whose last index runs `_close_lateral_walk` →
  `_adjudicate_fc()` (barred by #2711); both exits edit the two AT-CAP
  files (crossover_v2_flow.py 13,055; web/correction_crossover_v2.py
  8,804). The ruling direction already recorded (#2732): per-driver capture
  for the forward model is a DIFFERENT CONSUMER than the barred statistic —
  suppress the close for a non-stage-1 walk; never silently un-pause the
  statistic.
- **#2738** (done-screen badge precedence): ANOTHER agent's task, pending
  the OWNER's (a)/(b)/(c) ruling on the issue. Verified brief at
  `BRIEF-issue-2738-fix-agent.md` (checkout root). Not yours unless asked.
- Two chips in separate sessions: .gitignore venv-symlink; constraints-test
  skip-without-venv (kills the 2-failure lane artifact every agent re-diagnoses).

## 4. Findings a fresh prescriber MUST hold (full detail: the graveyard file)

- **Pooling cancellation is REAL in the shipped pooled view**:
  `jasper/audio_measurement/spatial_combine.py::combine_positions` power-
  averages seat CURVES before deviation (±3 dB opposite seats → +0.96 dB).
  The objective (#2739) grades per-seat, worst-seat-per-chunk; shipped
  grading intentionally untouched — flagged. Never grade flatness on a
  seat-averaged curve.
- **The model has NO ranking authority for delay** (ρ = −1.000 vs measured,
  banked armrun) until rung-4 calibration passes ≥ +0.6. Stamped in every
  prediction document.
- **jts3 tweeter pad/declarations are CORRECT to 0.05 dB — DO NOT TOUCH THE
  PAD.** The A1 "anomaly" was a code defect (giveback band mismatch), fixed
  in #2733. The live config is ~+2.5 dB tweeter-hot (owner heard "a little
  bright") — the RE-LEVEL after deploy fixes it.
- **A2:** turntable MOTION obeys the command (+ = right facing the speaker,
  owner-eyeballed); the offset READBACK lies (negated). Never consume
  readback sign.
- Timing: chain stable ≤7.33 µs after alignment; capture starts scatter
  ±14–26 ms; USB silently drops ~0.5% packets — slip guard (#2731) rejects
  ≥2 samples; ~1-sample slips (20.8 µs = the whole 2 kHz budget) still
  pass; closing that needs in-program pilot signal (named follow-up).
- **c-t-c = 8.5 in = 215.9 mm** (owner-declared): arms the inert
  `MeasurementGeometry.driver_spacing_m` parallax correction, vertical
  synthesis (label MODELED), spacing priors (1.2λ ≈ 1.9 kHz; external
  sources disagree at 2.5 kHz — a prior conflict measurement resolves).
- In-window features (5/5 seats, natural Q): −1.56@1037 (6.6), +0.81@1406
  (5.1), +0.67@2057 (3.9). Out-of-window bank: +0.83@4149 · −1.46@4582 ·
  +1.13@5396 · −0.70@6245 · −2.00@8530 · +1.01@9509 — now addressable via
  #2740's route once merged+consumed. ≥3.6 kHz size-split class = beaming,
  barred. All nine MINIMUM-PHASE (controls-verified).
- Fc-move hypothesis for this horn (modeled, external): 1.6→2.5–2.8 kHz,
  claimed nonmonotonicity at 2.0; woofer breakup = unmeasured ceiling. A
  SEARCH CANDIDATE, not a ruling; #2736's floor + breakup check gate it.
- Vertical measurement is BLOCKING for delay-lever ranking claims (lobe
  re-aiming, mark-vs-pool ρ=−0.66). c-t-c synthesis is modeled mitigation
  only. Cheap real path: speaker on its side (owner's hands).

## 5. The hardware session (when the owner gives a studio window)

Speaker: jts3 (SSH pi@192.168.1.92; .env.local in the session worktree at
`.claude/worktrees/speaker-linearization-measurement-74513c` — PI_HOST
192.168.1.92, JASPER_HOSTNAME jts3.local). Box at handoff: baseline config
`389bd7a55148`, build `4b7e76db4`, silent, arm parked 0.0°. THE SPEAKER WAS
PHYSICALLY MOVED (lowered) by the owner — it must be RAISED BACK first, and
the first measurement is a BASELINE RE-VERIFICATION (position-specific
constants: the banked baseline family, the frozen-reference anchor 0.8589,
§10.7 decision constants — all die if the position changed; re-bank if
outside noise).

Sequence: (1) deploy current main (`bash scripts/deploy-to-pi.sh` from the
session worktree; verify build.txt SHA+ok; expect the #2736 fleet check —
compare live tweeter corner vs declared floor BEFORE deploy so a
refuse-on-save surprises no one); (2) owner raises speaker; (3) baseline
re-verify; (4) **RE-LEVEL** (stage-1 leveling with #2733 live — expect the
anchor near −10.19, realized-level gate clearing with margin; the owner
listens: "a little bright" should be gone — perceptual acceptance); (5)
fresh baseline bank (n≥2); (6) **round 19** — narrow-Q EQ verdict (width-
matched cuts at the RE-DERIVED feature magnitudes from a fresh packet —
the re-level changes the features; prediction banked in the deciding
frame's units with re-derived σ constants; keep/rollback per pre-registered
rule); (7) per-driver five-angle captures via `jasper-angle-capture`
(needs the walk-take PR merged); (8) offline search (#2739) → shortlist →
at most a few confirmation rounds (delay/polarity first — the only
calibrated-adjacent axes; Fc after its apply path + calibration arm), each
with the REVERSE-NULL check (invert one branch, measure the on-axis null —
adjudicates alignment, including the modeled 175–292 µs vs measured
+24.06 µs tension). Rounds ≈ 25 min; rollback is proven; end every session
parked + silent.

## 6. Environment traps (all bit us; memory files have detail)

Worktrees have no .venv (main venv + PYTHONPATH pinned; VIRTUAL_ENV for
the uv-based constraints tests until the chips land). Bash cwd ≠ Write cwd
possible — absolute paths + sentinel-verify. Lanes UNPIPED; trust only the
final `==> test-merge: N passed` line. Freeze-and-recheck (`git write-tree`
before/after) around final lanes; re-freeze if a delegate edited. Bash tool
is zsh: no word-split (`${=VAR}`), `mapfile` absent (silently empty),
capture exit codes before pipes. Never `pkill -f` shared patterns; never
unscoped `pgrep -f` waits — scope to your own worktree path. Shared
scratchpad: unique filenames. PYTHONDONTWRITEBYTECODE=1 + purge
__pycache__ for mutations; prove the edit landed AND the test ran (no-op
control). Agents stopping to "wait" for untracked background lanes stall
forever — nudge with SendMessage. Spawn isolation is unreliable: agents
launched "fresh" sometimes land in the shared session worktree — verify
`git rev-parse --show-toplevel` FIRST; the session worktree holds the
owner's untracked METHODOLOGY/BRIEF/PACK files — never delete them.

## 7. Artifact index

- Branch `claude/night-driver` (pushed): PLAN-crossover-forward-model (the
  P2 execution spec + H5 addendum), RESEARCH-BRIEF-speaker-linearization
  (+errata), RESEARCH-BRIEF-self-referencing-timing, RESEARCH-crossover-
  design-guide (owner's, with adoption header), METHODOLOGY-overview,
  PACK-DRAFT graveyard. Copies of most at the main-checkout root.
- `captures/wired-night-2026-08-19/` (laptop, gitignored): run-log.md
  (~3,100 lines, §§8–10 = the campaign's evidence), charts/ 01–06 +
  README, analysis/, tools/ (incl. the acceptance-tested
  frozen_reference.py and the classify_* suite).
- `captures/xover-armrun-2026-08-18/`: the banked per-driver arm data
  (model dev fixture; 5 applied arms + a250 pre-apply-only).
- Memory: `~/.claude/projects/-Users-jaspercurry-Code-JTS/memory/` — the
  index is load-bearing; the wired-night + supersede-and-delete + env-trap
  entries especially.

## 8. Immediate next actions for you, in order

1. `gh pr list` + read each open PR's disposition comments — finish the
   in-flight reviews/fix-rounds per §3, merge each on 0/0 + live-green.
2. Delete the #2735 base branch after #2739 lands.
3. Build the walk-take PR (per §3's boundary paragraph) — ONLY if the owner
   has cleared crossing the boundary; at handoff the standing order was
   "land everything UP TO it".
4. Then §5 when the owner opens the studio.
5. Queued next-wave builds (owner-approved, not started): round-type
   registry (the always-run protocol as code); knowledge-pack
   productization (docs/crossover-agent/ + per-speaker memory mechanism +
   packet pack-version stamp); the compose-vs-replace ruling for #2740's
   consumption; the boost-route headroom design brief (owner decision).

Land PRs before tokens run out. Post dispositions when reviews return.
Leave every artifact where the next session can find it. The measurement
is the referee; the ear is the acceptance check; nothing merges un-gated.
