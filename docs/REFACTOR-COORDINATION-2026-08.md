# Coordination map — the audit's fix plan vs the tuning refactor

Written 2026-08-25 by the tuning-stack conductor, for the owner to hand to the
audit-executing agent. Sources: `docs/DEEP-AUDIT-2026-08-25.md` (branch
`claude/codebase-complexity-audit-plynn4`, frozen at `9fcda9ee5`) and the
tuning-stack inventory (`00-synthesis.md` + addendum in this directory).

## The boundary principle

The audit already drew it: its census zone = the tuning refactor's zone —
`jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/correction/`,
the crossover_v2/correction web+CLI surfaces, and their tests. **Everything
inside that zone belongs to the tuning refactor. Everything outside belongs to
the audit's waves.** The audit's own §6 owner-decision 2 hands the in-zone
work (the ~55K prose/test discipline, the `commissioning_capture_producer`
orphan + its doc drift) to the tuning agent — accepted; it is folded into the
refactor plan.

## Audit waves, cleared or flagged

| Audit item | Verdict | Rule |
|---|---|---|
| **Wave 0** (BRINGUP rewrite, nginx return-1, root wizard hardening, wake-corpus guard) | **CLEAR** — zero overlap | run any time |
| Wave 0: `apply_dsp_config(acquire_lock=...)` phantom-knob deletion (13 signatures) | **LOW RISK** — shared DSP-apply infrastructure adjacent to the zone, but the refactor does not change those signatures | run any time; note it in the PR so the refactor rebases cleanly |
| **Wave 1** (dead code: bass_extension, orbs.js, channel_split, wizard mains, TtsPlayout, bass_alignment, experiments/aec3) | **CLEAR** — all outside the zone | run any time |
| **Wave 2**: `rust/jasper-fanin` host-compliance/prime deletion | **SEQUENCE + RE-VERIFY** — the refactor's transport design rests on a measured fact (the correction ring lane passes stereo bit-exactly; evidence file `08-…` here). The compliance machinery is USB-session-scoped and *should* be orthogonal, but any `mixer.rs`/`lane_resampler.rs` edit invalidates the measurement until re-run | land it whenever, but the 5-case stereo tap (scripted in evidence 08) MUST be re-run on the box after it lands and before the refactor's transport wave executes |
| Wave 2: CI classifier, env-migrations, nginx twins, doctor never-fail, wake-events cap, wake_training move | **CLEAR** | run any time |
| **Wave 3**: `test_lint_contracts.py` strip (~1,700) | **DEFER TO THE REFACTOR** — 7 of its 8 ceilings are the refactor zone's files; the inventory proposes deleting `MAX_LINES_BY_PATH` outright (fragment 07). Two hands in one 2,159-line file guarantees conflicts | single owner: the tuning refactor. The audit agent does not touch this file |
| Wave 3: 962 redundant asyncio markers (repo-wide mechanical) | **ORDER IT FIRST or SCOPE IT OUT** — trivially touches tuning tests the refactor will rewrite/delete | either land the sweep BEFORE the refactor's first wave (rebase-cheap), or exclude the census-zone test files and the refactor handles its own |
| Wave 3: doctor tests, CSRF altitude, tests/js loaders, literal-welded wiring tests | **CLEAR** (outside zone) | run any time |
| **Wave 4**: AGENTS.md restructure (~1,050) | **CLEAR with one invariant** — the "Right-sizing directive" section (merged 2026-08-25, #2940/#2941) is new, owner-ratified, and load-bearing; it must survive the restructure verbatim. Tuning-section pointers get re-trued at the refactor's END, not before | audit agent takes AGENTS.md/README; leaves `docs/measurement-loop-doctrine.md`, the tuning HANDOFF/plan family, and `docs/testing-tooling.md` to the refactor |
| **Wave 5**: duck lease (~200 lines) | **HELD — superseded by measurement** — the lease's main client (the graph-swap duck) was measured protecting silence on every swapping stimulus in both baseline rounds; the refactor expects to DELETE it (one no-pop check pending). Build no lease until that decision lands; then re-evaluate whether the remaining ducks (cue/TTS) earn one under the right-sizing directive | do not build |
| Wave 5: systemctl-through-broker, sound_setup 502 helper, transit `git mv`, `jasper/measurement/` fold, `test_control_server` split, atomic writers | **CLEAR** — all outside; the `jasper/measurement/` fold is confirmed non-colliding (inventory fragment 05 verified it belongs to the `/balance/` wizard, not the tuning stack) | run any time |
| Owner-decision 5 (process weight) | **ALREADY RESOLVED** — the right-sizing directive in AGENTS.md is the ruling | nothing to do |

## The four single-owner files/areas

1. `tests/test_lint_contracts.py` — tuning refactor.
2. `rust/jasper-fanin/**` — audit agent may edit, but the stereo-tap re-verify
   gates the refactor's transport wave afterward.
3. The duck machinery (`GRAPH_SWAP_DUCK_DB`, the swap duck, any lease) —
   tuning refactor; audit agent builds nothing there.
4. Tuning-slice docs (doctrine, tuning HANDOFF/plan family, testing-tooling) —
   tuning refactor. AGENTS.md/README — audit agent, directive preserved.

## Practical protocol

Small PRs, land fast, rebase often — both programs. The audit's waves 0–1 are
pure green light and worth landing before the refactor starts (they shrink the
tree the refactor rebases over). Anything not in the table that later looks
shared: the boundary principle decides, and one line in the other program's
next PR body is enough notice.
