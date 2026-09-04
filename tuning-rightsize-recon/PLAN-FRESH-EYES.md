# Plan — fresh eyes (2026-09-04)

Ordered by what closes the LLM's walk soonest. Every row is one PR, one
concern, its own branch `claude/tuning-rightsize/w<N>-<slug>` from a fresh
`origin/main`, following `EXEC-W3.md` with the prefix bumped. Tags: **R**
relocation (behaviour-identical, AST-pinned where a body moves), **W** rewrite
(small, contract-first), **D** delete (SPENT / SUPERSEDED verdict in the PR
body), **P** prose or doc, **T** test. **NN** rows touch `volume_limit`,
`set_volume_db`, the commissioning SPL stop, the excitation ledger or
`program_admission`: gate is `/adversarial-review` plus an owner hardware pass,
and their merge is held until the owner confirms the pass. Models: Opus for
code, relocations and the constants review; Sonnet for prose, docs, tests and
prose review. Every code PR: `/simplify` then `/code-review` (medium) in the
lane, `/code-review` again at the orchestrator. Every prose PR: AST identity
against the merge-base plus an Opus constants review.

Acceptance bar on every row (owner, 2026-09-04): clean, modular, one owner per
concern, single source of truth, no god files, no new machinery, no added prose
beyond a constraint or a pointer.

## Wave 1 — close the stalls (audit §1–§4)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 1.1 | `jasper-round open --alignment-prescription/--topology-prescription` (file or `-`) → `open_session` body; runbook step 6 names it | W | test pins both keys in the POST body; menu regenerates | code-review |
| 1.2 | `jasper-delay-sweep confirm <bundle>` calls `confirmation_verdict` over `null_runs/`, writes `delay_confirmation.json`; `propose --out` defaults beside the round; methodology §4 "not wired" sentence deleted | W | `confirmation_verdict` gains a production caller; artifact appears in a fixture round | code-review |
| 1.3 | `LinearizationState` carries `committed_side` and `anchor_drift_db`; `trim_strategy_for_outcome` reads them | W | the "fitted" case no longer maps to `COMMITTED_PAIR_UNRECORDED`; one behaviour pin | code-review (high) |
| 1.4 | `round-views repeat-floor --install` calls `write_repeat_floor` at the on-box path; methodology §1c stops asking for a manual copy | W | file appears under a temp state dir; doc sentence | code-review |
| 1.5 | take record stamps `phase_composition` from `analysis.configured_path_composed`; `delay-sweep propose` echoes it; methodology §4 "recorded nowhere" deleted | W | take-record field pinned | code-review |
| 1.6 | one exit vocabulary: `null_door`, `measure`, `round_bank`, `basic_profile`, `crossover_prescriber` (1/2 un-inverted) on `_refusal.py`; `jasper-round` reports transport loss as `unreadable` + `reason=transport`; runbook table regenerated | W | one parametrized test: every tuning CLI's exit set ⊆ {0,1,2,3} and status/code agree | code-review |
| 1.7 | `resolve_conductor_context`, `status_payload`'s shared half and `WiredStimulusCapture` → `crossover_v2/conductor_context.py`; `cli/measure.read_box_declaration` consumes it; `PACKAGE_BOUNDARIES` gains `jasper/cli → jasper.web` | R | boundary row green; zero cli→web imports; AST identity of moved bodies | two-pass code-review (deferred-import rule) |
| 1.8 | topology floor/ceiling looked up by role, not `pair[1]`/`pair[0]` | W | test reorders the role tuple and the door still refuses the same fc | **adversarial-review** (declared-bands clamp) |
| 1.9 | `prescriber packet` defaults `--out` beside the round, prints the `status` sections and the path; no curve array on stdout | W | stdout pinned curve-free; artifact appears | code-review |
| 1.10 | delete `priors.candidate_priors` (SPENT, own docstring), `correction_crossover_backend.begin_commissioning_run` (zero callers), `attempts_loop.DECISIONS` (owner-approved); fold `attempt_grading.py` into `verification.py` | D | grep in PR body; tests for deleted symbols removed | code-review |

## Wave 2 — one binary per verb family (audit §2, target table)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 2.1 | `jasper-round-bank` → `jasper-round bank`; entry point deleted; menu regenerated | R | menu row; runbook step 6 | code-review |
| 2.2 | `jasper-arm-walk` → `jasper-angle-capture serve --mover turntable`; entry point deleted (after #3934 lands) | R | `test_measurement_mover_agnostic` green; menu row | code-review |
| 2.3 | `jasper-gate-sweep` → `round-views gate-sweep` (`ARTIFACT_BY_VIEW` row) | R | inventory names it; menu row | code-review |
| 2.4 | `jasper-classify-features` + `jasper-project-ring` → `round-views classify-features` with the ring projected by default | R | same | code-review |
| 2.5 | `jasper-read-distortion` → `round-views distortion` | R | same | code-review |
| 2.6 | `jasper-close-reference compare` → `round-views close-reference`; `distance` stays a flag | R | same | code-review |
| 2.7 | `jasper-forward-model` → `round-views forward-model` | R | same | code-review |
| 2.8 | `jasper-delay-sweep` → `round-views delay-landscape` and `delay-confirm` (after 1.2) | R | same | code-review |
| 2.9 | `proposal.json` gains optional `expected_delta` and `declared_tilt_db` (owner-approved expectation slot); `round-views frozen` reports them beside the measured delta | W | field pins; methodology §8 names the field | code-review |
| 2.10 | ADR-0231: the four rulings with no ADR (Decision 13/#2662; "refusals HOLD the incumbent"; MS-4; MS-14) plus the one-line D11 note | P | ADR review | sanity look |
| 2.11 | methodology and runbook pointers for the retired binaries; §1c "two spreads" → "two of the spreads here"; runbook:39-42 → doctrine §2 pointer | P | astsame vs merge-base; constants review | Opus constants review |

## Wave 3 — god files and boundary pins (audit §5)

| Row | Concern | Tag | Proof | Gate |
|---|---|---|---|---|
| 3.1 | `startup_load.py` → split at the two lifecycles: `commission_load.py` takes 1173–2333 | R | AST identity; deferred imports resolved | two-pass code-review |
| 3.2 | `web_commissioning.py` mux socket client (573–914) → its own module (verify no existing client first; stop if one exists) | R | AST identity | two-pass code-review |
| 3.3 | `cli/active_speaker._reemit_staged_startup_anchor` (510–779) → `startup_load` | R | AST identity; deploy callers unchanged | two-pass code-review |
| 3.4 | `staging.py` declaration vocabulary (342–531) → `declaration_vocabulary.py`; four importers repointed | R | AST identity | code-review |
| 3.5 | `driver_safety.py` prompt block (1402–1706) → `driver_safety_prompt.py` | R | AST identity | code-review |
| 3.6 | `program_analysis.py` → package along its eight banners, `__init__` re-exports | R | import-closure test green; AST identity | two-pass code-review |
| 3.7 | `camilla_yaml.py`: six inline clamps → one `_assert_volume_limit` raising `ActiveSpeakerConfigError` | R | every emitter still refuses `volume_limit_db > 0`; heavy tests untouched | **NN: adversarial-review + hardware pass** |
| 3.8 | boundary pins: `jasper/active_speaker → jasper.web/jasper.cli` forbidden; `jasper/correction → jasper.active_speaker` allowed only from `runtime_safety.py` | T | rows green at HEAD | sanity look |
| 3.9 | `test_crossover_v2_conductor.py` split by banner into `test_crossover_v2_conductor_<topic>.py` | T | collected count unchanged; ratchet still green | sanity look |
| 3.10 | web-twin step 10: `CrossoverLevelLease` → `session_volume_plan` / `volume_claim` | R | byte-identical volume plan over the fixture sessions | **NN: adversarial-review + hardware pass** |

## Held (owner decision, or after wave 3)

- `jasper-null` → engine module + `measure --kind null` (**NN**: excitation
  ledger, `program_admission`). Rewrite; only when a wave has hardware time.
- `run-crossover-round.py` mapping its twelve codes onto the shared four with
  the sub-tool's name in the trail. Rewrite of a laptop script; low harm.
- `bass_extension_bench --live` stub (#1738): wire or delete; owner's call.

## Dropped from PLAN.md, and why

3.1 (73 exception classes → one): the LLM meets refusals as codes and one
stdout shape, both already owned by `_refusal.py`; class count is invisible to
the walk. 3.2 door kit, 3.3 record kit, 3.7 stacks, 4.1–4.3 walk-state and
session-open, all of Phase 5: rewrites that close no stall and touch
safety-adjacent paths for size alone. 3.8: room correction, the other product.
3.9: the fixture half landed; only the conductor split remains (row 3.9 above).
Web-twin steps 2–9: the skeptics showed `prepare_v2_session` and
`handle_v2_apply` parse requests and delegate; the three named engine
destinations were never created and are not owed. 3.4 helpers and 2.6/2.7
regroup: only inside a PR already touching the file. 4.4 survives as row 3.10.
