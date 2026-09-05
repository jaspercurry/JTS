# Survey — the tuning toolbox against the vision (2026-09-05, main f4ff89731)

The fresh-eyes survey issue #4029 asked for: the record and the tree tiled
across ten read-only lanes (Opus verifiers and vision surveys, Sonnet digests),
LANDING.md's verdicts re-checked at HEAD, the methodology walked as the driving
LLM, and the four **(vision)** items measured rather than reasoned about. The
lane reports are banked beside this file under `survey-vision/` and every
claim below cites one; verdicts and the plan are the architect's. Nothing
executes before the owner's nod.

Lane reports: `verify-landing.md`, `verify-memory-ssot.md`,
`walk-methodology.md`, `vision-human-mover.md`, `vision-config-ladder.md`,
`vision-attribution.md`, `vision-context.md` (39 measured invocations),
`maintainer-legibility.md`, `pins-ci-record.md`, `docs-satellite.md`.

## 1. Verdict

**The right-sizing program is done and was scored against the wrong yardstick.**
TARGET.md is a right-sizing target — binaries, exit codes, boundaries, a
generated menu — and against it the tree is true: LANDING §1 holds at HEAD
on every row (`verify-landing.md` §1–§3), one improved, none regressed. Against the vision in #4029 §1 —
an LLM at an SSH prompt drives a human through a round, separates room from
speaker, proposes, stages, re-measures — the toolbox is roughly **half built,
and the missing half is small.** Measured, not reasoned:

- **The LLM is locked out of the human-mover loop, by design.** A hand-walked
  round is driven from a phone at `/sound/crossover/`; the walk's live hold
  (`capture.position_pending`) has one on-box reader and it is the turntable
  loop; `jasper-round` has no verb to read the hold, release it, close the set
  or re-ask a spot; `angle-capture serve` accepts only `--mover turntable`. The
  LLM stages, opens, tells the human in chat to open a URL, and is blind for
  up to 46 minutes. `jasper-measure --prompt "what the mover was told"` is the
  toolbox recording that the conversation happens out-of-band.
  (`vision-human-mover.md` §7; `angle_capture.py:797-799`, `arm_walk.py:853-869`)
- **The methodology never names the verb that measures, applies or banks.**
  `jasper-round`, `jasper-measure`, `jasper-basic-profile`: zero mentions in
  872 lines; doctrine §1's loop has no apply step; three filesystem paths a
  round needs (sessions root, flow state, design draft) appear in no doc; the
  prescription schema is in no doc. Of the 18 commands in one real round, 7
  cannot be typed from the docs and 3 of those are blockers.
  (`walk-methodology.md` Headline, §C)
- **Every view's answer is on stderr.** 15 of 17 successful `round-views`
  runs wrote 0 bytes to stdout; refusals go to stdout; the prescriber splits
  two verbs each way. `$(jasper-round-views directivity $R)` is the empty
  string. Only one of 39 invocations states an artifact's size; `inventory`'s
  `produced_by` is a template in 17 of 17 rows; `status`'s next-actions are
  0 of 6 runnable. (`vision-context.md` §2, §5; the prescriber split is
  `verify-landing.md` §3)
- **`jasper/attribution/` (2,047 lines) is reachable from none of the ten
  binaries**, `inventory` does not know its three artifacts exist, the packet
  reads one finding phase of three, and nothing below 4 kHz can produce a
  finding — undisclosed. The classifier's floor (~450 Hz on the shipped gate)
  is disclosed only by refusing. (`vision-attribution.md` §1, §6)
- **The config ladder exists inside one round and nothing reads it back.**
  `angle-capture --candidates A,B,C` walks pose-major/candidate-minor and every
  take stamps `candidate_id`; `_TAKE_FIELDS` omits it, so the packet's pose
  rows cannot say which candidate a curve belongs to — ambiguous exactly at a
  held pose. Only alignment candidates are measurable; a DSP-graph ladder has a
  door (`republish`) and no CLI. (`vision-config-ladder.md` §5, §6)
- **The round directory forgets the trim decision entirely.** LANDING said
  `trim_strategy` is serialized; it is not — none of the three fields reaches
  any file (`verify-memory-ssot.md` §1). `position_cycle.json` is a dead loop
  (one laptop writer, zero readers).
- **Refusal shapes: eleven, not five**; seven tools gate a document behind
  `--json`, not four; fifteen bare stderr-only paths; the roster test invokes
  no CLI's `main`. LANDING §4's row 8.1 was right and undersized.
  (`verify-landing.md` §1, §2)

What is right and stays right: ten binaries, one exit vocabulary, boundaries
pinned (the last edge landed as wave 7's 7.3), menu `--check` green, evidence
as JSON under the round, room correction out of scope. The CLI modules
themselves are honest and small; `jasper/cli/_refusal.py` (109 lines) is the
best-designed thing in the toolbox.

**Should we walk a round on hardware before writing more code?** Yes for the
walk path and no for the map. Six facts the human-mover lane could not settle
statically (`vision-human-mover.md` §8) — whether the browser release actually
posts on a live wired round, whether an 11-stop walk fits the 46-minute
ceiling, whether four identical "leave it at 0°" screens read as a bug — decide
the shape of the loop verbs. But the docs-as-map, stream and inventory rows are
pure surface: they are wrong today by inspection and a hardware round cannot
make them righter. So: **Round A now (owner, browser, the §10 sequence) in
parallel with batch 1; batch 2 shaped by Round A's friction log; Round B
LLM-driven through the new verbs.**

## 2. LANDING re-verified at HEAD

`verify-landing.md` verdicts every row of LANDING §1. Substance holds; one
verdict flips upward (`audio_measurement ↛ cli` is now pinned at
`tests/test_correction_boundary_ssot.py:267`, commit 59e53e3cf); citations into
`round.py` (−7), `crossover_prescriber.py` (−4), `round_views/delay.py` (−13)
and `cli/doctor/correction.py` no longer resolve; `pyproject.toml:57/:59` never
did. Undercounts: refusal shapes (11 vs 5), `--json` gating (7 tools vs 4),
`round-views`' 736-character description. Overstatements: `trim_strategy`
serialized (false — nothing lands); `answer small: PARTIAL, coherent` (for an
LLM consumer it is the single most-felt defect). `position_cycle.json`,
`proposal.json`/`apply.json`, σ `kind`: as LANDING said, with one correction —
the LLM does read unlabelled σ, in `gate_sweep.json` and `candidate.json`
(`verify-memory-ssot.md` §4).

Wave 7 landed in full (#4021, per #3769's wave-7 status). Wave 8: only 8.11
(#4024). Zero open PRs touch
the tuning scope today (`pins-ci-record.md` §D). Five follow-ups in the record
have no row and no not-doing entry: the CI fd/`--durations` thread, a fourth
path-or-stdin reader (`cli/aec_sweep_config.py:33`), a source-text assertion
in `tests/test_ring_active_endpoint.py`, the `jasper-round wait` doc one-liner,
`CommissioningRunStore.replace_current` (`pins-ci-record.md` §C).

## 3. The vision, item by item

| Vision item (#4029 §1) | Measured status | Lane |
|---|---|---|
| The human is told where to put the mic | TRUE, on the phone only: "Turn the microphone to -20° (20° LEFT of the design axis). Keep it 1 m from the speaker and pointed at it." + a ready button. No terminal, no cue. Vertical poses in degrees only; the button flattens every vertical pose to 0. | human-mover §2, §3 |
| The LLM asks for a position, waits for ready, triggers, sees the take | FALSE. No read/release/complete/retake verb; `serve` is turntable-only. `WizardClient` already has `get_json`/`post_json` + CSRF; the endpoints exist. | human-mover §7 |
| A bad take is re-asked | TRUE in the browser (`retake`); `jasper-round wait` misreports a retriable rejection as `session_failed` (`wizard_client.py:322-327`). | human-mover §5 |
| Configs A/B/C at a held position | PARTIAL: `--candidates` walks it; nothing reads `candidate_id` back; only alignment candidates; `repeat` is a noise instrument and would lie if used as the comparator. | config-ladder |
| Room vs speaker attribution | PARTIAL: `gate-sweep` gives one machine verdict (`window_verdict`); `close-reference` answers per spec band; classifier floor ~450 Hz; `attribution` package unreachable; a ROOM verdict is reachable, a defensible SPEAKER verdict is not, and the asymmetry is stated nowhere. | attribution §4 |
| Every capability a subcommand with a menu row and a methodology pointer | TRUE for the menu; FALSE for the pointers (measure/apply/bank unnamed; `directivity`/`entry`/`frequency` exist only in the menu blob; `both_at`/`--regime both` documented nowhere). | walk §A, §D |
| Answer small, depth on demand | FALSE for the stream (answer on stderr); PARTIAL for size (`per_seat.json` 13 kB, 70% numeric, no scalar; `frequency_view.json` 32 kB; `delay-landscape` prints the grid). No `--summary` flag anywhere. | context §2, §5 |
| `inventory` names what is missing | PARTIAL: names 17 producers where 5 can run; commands not runnable; no sizes; the attribution artifacts and `position_cycle.json` are not in its table. | context §3, attribution G2 |
| Refuse only on integrity and the non-negotiables, disclose the rest | TRUE — no nanny found. `classification == room` unvouches, never blocks. | attribution G13 |
| Evidence is JSON beside the round; no DB | TRUE. | memory-ssot |
| Docs the LLM reads every session | 2,627 lines in three docs + 2,733 in two the methodology's owner table sends it to, of which ~86 are load-bearing at round time; 26 dated sentences; 2 dead pointers; one false claim (`layers-design.md:1554`); one self-contradiction (`methodology:636` vs `:697`); `methodology:646` false on both halves. | walk §B, §D; docs-satellite |

## 4. What the last architect got right, wrong, and what I would do differently

Right: stalls first; nine binaries into two with no shims; one exit vocabulary;
integration batching; stopping on false premises (five lanes, all correct);
the definition of done ending in a hardware round; the `/simplify` +
`/code-review` + orchestrator-review discipline, which the constants reviews
show paid for itself.

Wrong or underweighted:
1. **Scored against TARGET, not the vision.** "85% of the vision" was 85% of
   TARGET. The vision's centre — the LLM driving the human — was never traced;
   it is closed to the LLM by a documented design choice
   (`runbook:350`) that nobody re-examined against §1.
2. **"Answer small: PARTIAL, coherent."** For an SSH-driving LLM the stderr
   convention is the defect it feels on every command. This should have been
   wave 4's row, before any relocation.
3. **The docs were treated as a history-hygiene problem.** The real defect is
   that the methodology is 872 lines of decision rule with no verb for measure,
   apply or bank, and no path an LLM can type.
4. **"Not doing: helper convergence" hid a bug.** `evidence_packet.py:2338`'s
   `_finite_or_none` omits `isfinite`; a NaN gain reaches `json.dumps` as bare
   `NaN`. The file already imports `json_fields.finite_float`.
5. **Two claims about memory were wrong in the record's own favour**
   (`trim_strategy` serialized; five σ producers unlabelled but unread).

What I would do differently: measure the LLM's experience with executed
commands before writing a plan (this survey ran 39 of them); trace the human's
experience from the templates, not the docs; put "the LLM can feel it" as the
row-ordering rule, which the record stated as a lesson and then did not apply
to its own wave 8 (five of thirteen rows LLM-facing).

## 5. Maintainer legibility (the owner's first goal)

`maintainer-legibility.md` measured what an external maintainer meets:

- The ten CLIs are small and honest; the closure behind them is the product:
  `jasper-basic-profile` (460 lines, 4 imports) pulls 213 modules;
  `jasper-measure` pulls 402, including `jasper/web/correction_crossover_v2.py`
  (7,167 lines) through `correction_crossover_v2_wired → sound_setup` — a
  measurement CLI loading the web host. Row 8.2 (the capture kernel into the
  engine) cuts that edge; it is a legibility row as much as a duplication row.
- `runtime_contract.py` — 5,152 lines, 97 top-level defs, **zero** section
  markers, reached by 9 of 10 binaries and imported directly by none. Files
  with the most banners (`spatial.py` 19, `round_views.py` 22) are title-less
  rules that mark nothing; the largest files have none.
- `program_analysis/__init__.py`: 140 re-exports, 66 facade importers, 0
  submodule importers, 32 `_`-private names published across packages.
  `active_speaker/__init__.py`: a 207-entry `_LAZY_ATTRS` string dict no tool
  can follow. Both are go-to-definition traps.
- 120 comment/docstring hits of campaign shorthand (`Wave 5a`, `PR-T3`, `WO-1`,
  `panel lens C`) across 18 files — pointers into a record the maintainer does
  not have, and a class AGENTS.md already forbids.
- One concept, many words: round/bundle/session (both in one signature at
  `evidence_packet.py:491`); candidate/proposal/prescription/profile/draft/
  preset/plan (seven live nouns); door/gate/lease/latch/hold/admission/screen.
  Two modules named `round_views`; ten modules named `measurement*`.
- `jasper-audition` has no `main()` test; `jasper-angle-capture` pins its
  refusals on private `_cmd_*` functions.

Rows below take the cheap, high-yield legibility items (the campaign
shorthand, the capture edge, the `_finite_or_none` bug, the dead defs). The
structural ones (facade repoint, `runtime_contract` shape, the nouns) are in
§7 with a recommendation each.

## 6. Wave 9 — rows, proofs, gates, sizes

Ordering rule: what the LLM feels first. Tags: W wire, P prose, T test,
R relocate, D delete, NN non-negotiable tier. Sizes: tiny < 50 changed lines,
small < 150, medium < 400. Every code row: `/simplify` then `/code-review`
in the lane, orchestrator review before merge; file-disjoint lanes in
worktrees from fresh `origin/main`; premise re-verified first, stop if false.

### Batch 1 — the map and the streams (parallel with Round A)

| Row | Concern | Tag | Proof | Gate | Size |
|---|---|---|---|---|---|
| 9.1 | **The methodology is a map.** Name `jasper-round open/wait/bank/apply` at the steps that own them (§2, §7, §10) and add apply to doctrine §1's loop; `--stage post_apply` at §7; `--calibration-file` and the capture-gain precondition at §1a; the three paths (sessions root, flow state, design draft) once each; `directivity`/`entry`/`frequency` at §3b/§7; `--regime both` at §7 replacing `both_at`; fix D-1 (`:646` false), D-2 (`verify.gate` keys), D-5 (`--at-hz` self-contradiction), D-6 (`jts.local`), D-7 (the two noun phrases); pointer from §6a rung 2 to `classify-features` and its band. No restatement of what the runbook owns. | P | every ⚠ row of `walk-methodology.md` §C becomes DOC; menu `--check` 0; Opus constants review against main | sanity look | medium (prose) |
| 9.2 | **stdout is the answer.** One convention, two halves, recorded in one ADR: on success a tool prints one bounded JSON document on stdout (summary fields, `out` path, `bytes`; no numeric list longer than 16) and one human sentence on stderr; on failure the shared `{status, reason, detail}` on stdout regardless of `--json`, one sentence on stderr. Per-file lanes: (a) `round_views/` 17 views + `delay-landscape`'s grid off stdout; (b) `crossover_prescriber.py` — bare `error:` paths, `{"accepted": false}`, `propose` default `--out` beside the packet, `status`/`packet` already right; (c) `round.py` — receipts as the document, `blocked` to one code, `bank`'s `banked:` → `status`; (d) `angle_capture.py` (`{"ok": false}` retired), `basic_profile.py`, `audition.py`, `seat_level.py` ungated; (e) `measure.py`, `null_door.py` — `rows[]`/`record_ids[]` into the artifact, summary on stdout, the false "every exit speaks JSON" comment fixed. | W ×5 + ADR | 9.3 green per lane; existing tests; stdout of every success pinned array-free | code-review (medium); (c) high — apply path | small ×5 |
| 9.3 | **The pin.** `tests/test_cli_exit_vocabulary.py` grows one parametrized test that invokes each roster `main()` with a refusing input and asserts stdout is `{status, reason, detail}` with `status == STATUS_BY_CODE[code]`, and one that asserts every success document is array-free; `declare-geometry` exempt. Lands red-on-HEAD first. | T | red on HEAD for 9.2's files, green after | sanity look | small |
| 9.4 | **`inventory` and `status` are runnable.** `produced_by` with `<this-round>` substituted (12 of 17 rows become copy-pasteable), `bytes` per present artifact and a total, the bundle path resolved for the four `bundle_dir` views, `position_cycle.json` and the three `findings_*.json` in `ARTIFACT_BY_VIEW`; `status` next-actions emit the full command where the remedy is a CLI (six f-strings), reading order shows bytes/lines; `propose --help` lists the prescription fields from the dataclass. | W | fixture round: `inventory` rows runnable by `shlex`; `status` output pinned | code-review | small ×2 |
| 9.5 | **Memory lands.** The applied profile carries a `trim` block (`strategy`, `anchor_drift_db`, `committed_side`) as top-level siblings of the mirror, outside the fingerprinted snapshot; `jasper-round bank` writes `position_cycle.json` from the take records, outside the `rmtree` block, disclosed on failure; `candidate_id` joins `_TAKE_FIELDS`; `plan` prints the candidate per stop. | W ×3 | one behaviour pin each; banked fixture round carries all three | code-review | tiny ×3 |
| 9.6 | `_finite_or_none` → `json_fields.finite_float` (the NaN bug) | W | one pin: NaN gain serializes as null | sanity look | tiny |
| 9.7 | Both boundary tests into `scripts/test-fast`'s always-on list; `--durations=25` on the pytest lane so the fd thread has data | T | test-fast runs them on an unrelated change | sanity look | tiny |
| 9.8 | `--help` metavar `<round-dir>` across the ten; `round-views`' 736-character description to one sentence; four two-sentence descriptions to one | P | menu regenerates; `--help` diff is metavar/description only | sanity look | tiny |

### Batch 2 — the loop and the reads (after Round A's friction log)

| Row | Concern | Tag | Proof | Gate | Size |
|---|---|---|---|---|---|
| 9.9 | **The LLM drives the human.** Four verbs on `jasper-round`: `pending` (prints `capture.position_pending` verbatim), `position-ready --index N`, `complete`, `retake` — thin argparse over `WizardClient.get_json`/`post_json` and endpoints that already ship; `angle-capture show` over `peek_staged_angle_request` and the two stale epilog lines repointed; `measure.py`'s false "run-crossover-round calls this" epilog deleted. | W | seam tests over a fake wizard; Round B driven with them | code-review high (touches the live walk) + owner: Round B | small |
| 9.10 | **Walk transport truth.** `wait` returns `failed` only when the phase is terminal or the failure code is non-retriable; `pending_from_capture` returns `None` when `hand_released` is set; vertical poses carry the cm and `position_vertical_deg` through screen/pending/button. | W | one pin each | code-review | small |
| 9.11 | **Attribution reachable.** `round-views findings <round> [--phase]` over `storage.read_finding_set` for all three phases; the resolved `echo_band_hz` published beside the findings with the sentence that the band bounds the set; `classify-features` prints `classifiable_band_hz` in its summary and `inventory` shows it. | W | fixture with findings; `inventory` names them | code-review | small |
| 9.12 | **The packet reads all three finding phases** and carries `produced_by`/`present` so "ran and found nothing" ≠ "never ran". | W | fixture pin | code-review | tiny |
| 9.13 | **The ladder is readable.** `round-views candidates <round>`: takes grouped by `candidate_id` at each held pose, per-candidate curve summary and pairwise delta, `candidates.json` in the table; the runbook recipe names the three forms and that `repeat` is not the comparator. | W + P | fixture ladder round | code-review | small |
| 9.14 | `close-reference --at-hz` emits a narrow-band row beside the spec rows (the way `gate-sweep` already does); `gate-sweep` publishes an explicit `elevation: {sampled: false}` block; `pose_persistence` gains the σ across poses. | W | pins | code-review | small |

### Batch 3 — the kernel and the deletions (owner look)

| Row | Concern | Tag | Proof | Gate | Size |
|---|---|---|---|---|---|
| 9.15 | **One wired capture kernel in the engine** (LANDING 8.2): `WiredStimulusCapture`, `make_wired_recorder`, `mint_wired_answer`, the constants → `audio_measurement/wired_capture.py` or `crossover_v2/`; `null_door._play_and_capture` deleted, null runs bank `capture_integrity`; the `cli/measure.py` allowlist entry deleted, so `jasper-measure` stops importing `jasper.web`. | R+D | astmove identity; null pinned to bank integrity; allowlist test green with the entry gone; import closure of `jasper.cli.measure` excludes `jasper.web` | two-pass code-review; owner: one null run on the box | medium |
| 9.16 | **Dead defs with verdicts**: `CrossoverLevelLease.{run_level_match, configure_targets, driver_sweep_locked_main_volume_db}` (SUPERSEDED), `MeasurementSession.run_level_match` (0 callers — verify), `correction_crossover_backend.play_driver_capture_sweep` + `FaninGateContext` and its six threaded parameters (SPENT), `SafetyEnvelope.escalation_step_db` (0 callers, disagrees with the doc it mirrors). Two PRs. | D | repo-wide grep + verdict per symbol; no live volume write path changes (stop if one does) | code-review; owner sanity look (volume domain) | medium (deletion) |

### Close

| Row | Concern | Tag | Proof | Gate | Size |
|---|---|---|---|---|---|
| 9.17 | **Docs history pass**, five docs: 26 dated sentences and the "used to"/PR-number sentences in the three; the master plan's 384-line ticket backlog (lines 353-736) and the layers-design changelog tail (1760-1942) deleted; `layers-design.md:1554` (false at HEAD) and the superseded position set corrected or deleted; the dead `captures/…` and `fc_selector.py` pointers; "Last verified" trailers to one convention; the methodology's owner table sends the LLM to the master plan's constants section only. | P | astsame (prose-only); Opus constants review; every remaining pointer resolves | sanity look | medium (deletion) |
| 9.18 | **Campaign shorthand out of code comments** (120 hits, 18 files): delete or replace with the ADR/issue the sentence actually means. | P | astsame vs merge-base; Opus constants review | sanity look | small |
| 9.19 | **Record fold**: TARGET.md rewritten to HEAD truth (stream convention, field spellings, position_cycle on bank, trim block, `frozen` compares two measurements, the loop verbs); WAVE-LOG wave-9 fold; LANDING citations refreshed; #3769 status. | P | every TARGET sentence has a file:line | orchestrator | small |

### Hardware (owner)

- **Round A, now:** one hand-walked round on the box with the §10 sequence,
  browser-paced, timed; the friction log is batch 2's input. Confirm the
  #3991 debt at the same deploy: emitted CamillaDSP config reads
  `volume_limit: 0.0`.
- **Round B, after batch 2:** the same round driven from SSH through
  `jasper-round pending/position-ready/complete/retake`, the LLM telling the
  human nothing in chat that a verb could say.

Batching: batch 1 through one integration branch (lanes are file-disjoint:
docs | round_views | prescriber | round.py | angle/basic/audition/seat |
measure+null | test file | baseline_profile+round_bank+position_cycle |
evidence_packet | test-fast+CI | metavars); batch 2 through one; batch 3
through one; close as two PRs. ~24 PRs, 4 CI runs. **The 80/20 cut if only
one batch ships: batch 1 + Round A** — the LLM can then find every verb,
read every answer on stdout, and copy every command `inventory` prints.

## 7. Not doing, with why

- **`program_analysis` facade repoint** (66 importers): a real maintainer trap,
  but a 66-file mechanical diff conflicts with every lane in flight. Do it as
  one PR after this wave, when nothing else is open; not never.
- **`_LAZY_ATTRS`** (207 entries): lazy for cold-start reasons on the Pi; same
  treatment as the facade, after a measured `-X importtime` on hardware.
- **Splitting `runtime_contract.py` and the other large files**: single-owner
  files are not split for size (LANDING §6 holds). The cheapest legibility
  win — a table of contents — is prose AGENTS.md forbids. Name them in the
  map (§5) and move on.
- **Renaming the nouns** (round/bundle/session; the seven proposal words; the
  two `round_views` modules): churn across hundreds of files for one word
  each. Glossary in `maintainer-legibility.md` §5 instead.
- **`jasper-round republish`** for a DSP-graph ladder: the runbook says the
  open part is sequencing (hold the next capture until apply lands), and that
  is a design to write after Round B shows what the alignment ladder costs.
- **A per-stop audible cue for the human mover**: new audible product
  behaviour the owner has not asked for; the phone screen is the channel.
  Question, not a row (§8).
- **Close-reference program row** (§6a rung 3 "not built") and a second drive
  level for level dependence: measurement-path work with a real capture cost;
  after Round B.
- **A banked-candidates listing verb**: fold into `prescriber status`'s
  `banked` section later if Round B's ladder wants it.
- **Helper convergence** beyond the one bug: LANDING §6 holds.
- **`run-crossover-round.py` codes, the null fold, σ-kind labelling, the
  doctor's cli→web edges**: LANDING §6 holds, with one amendment — the σ the
  LLM reads in `gate_sweep.json` and `candidate.json` gets its `kind` when
  9.14 touches those writers, not as its own row.
- **The CI fd leak as a diagnosis row**: 9.7 buys `--durations` data; the
  fix is a PR when the leakers are named.

## 8. Owner questions

1. **Stream reversal.** 9.2 reverses a documented module choice (#2769:
   summary on stderr, stdout empty). Confirm "stdout is the answer" is the
   rule; the ADR records it.
2. **Round A first?** Batch 1 is safe to start now. Say if you would rather
   walk Round A before anything lands.
3. **The master plan's constants** with no code (escalation step, boost
   probe, stopping rule) — keep as documented heuristics the LLM applies
   (my recommendation, principle 2) and delete the "ticket 3.7 turns them into
   code" promise; or wire them.
4. **Per-stop cue** for the human mover: no for now unless Round A says
   otherwise.
5. **Facade repoint** as one mechanical PR after the wave: yes or no.

## 9. Definition of done

1. Every command in §10 is derivable from the methodology and runbook (the
   seven ⚠ rows are zero); menu `--check` green.
2. stdout is the answer on all ten binaries, pinned by invoking each `main`.
3. The LLM drives a hand-walked round from SSH (Round B), with the human told
   nothing in chat that a verb could say.
4. `inventory` prints copy-pasteable commands with sizes; `status` prints
   commands.
5. `round-views findings` and `candidates` exist; band floors are disclosed
   before a refusal.
6. The trim decision and `position_cycle.json` land from the on-box path,
   pinned; every take row carries `candidate_id`.
7. One wired capture kernel; `jasper.cli.measure`'s import closure excludes
   `jasper.web`.
8. Zero-production-caller code in the toolbox's domain deleted with verdicts.
9. Five docs carry no dated history, no dead pointer, no false sentence;
   `git grep -c "Wave [0-9]\|PR-[A-Z]\|WO-[0-9]\|panel lens" jasper` is zero.
10. Two hardware rounds walked; `volume_limit: 0.0` confirmed on the deploy.
11. TARGET matches HEAD; WAVE-LOG folded; #3769 points here.

## 10. The shortest real round (Round A, human mover)

From `walk-methodology.md` §C, every command an owner types at the speaker's
SSH prompt; ⚠ marks a step the docs alone would not have given (9.1 closes
them). `<V>` = `/opt/jasper/.venv/bin`.

```
sudo -n <V>/jasper-declare-geometry set --speaker-height-in 33 --mic-height-in 33 --distance-in 39
sudo -n <V>/jasper-declare-geometry show
amixer -c <card> set <capture-control> 100%                                  ⚠ (card/control from nowhere)
sudo -n <V>/jasper-seat-level --calibration-file /var/lib/jasper/mic-cal/<serial>.txt   ⚠ (required flag in no doc)
sudo -n <V>/jasper-angle-capture plan  --program baseline --size express --mover human
sudo -n <V>/jasper-angle-capture stage --program baseline --size express --mover human
<V>/jasper-round open --tier express                                         (methodology never names it)
   # human: open https://<speaker>/sound/crossover/ on a phone; tap through the poses
<V>/jasper-round wait --timeout-s 3000                                       (may misreport a retriable rejection)
<V>/jasper-round bank /var/lib/jasper/active_speaker/sessions/<session-id>   ⚠ (path in no doc; wait prints only the id)
<V>/jasper-round-views inventory <round-dir>
<V>/jasper-crossover-prescriber packet <round-dir>
<V>/jasper-round-views entry <round-dir>; spec-sweep; classify-features; frequency; directivity; delay-landscape --fc-hz <FC>
<V>/jasper-crossover-prescriber propose --packet <round-dir>/packet.json --prescription -   (schema in no doc)
<V>/jasper-crossover-prescriber stage --packet … --state /var/lib/jasper/active_speaker_crossover_v2_state.json --prescription -   ⚠
<V>/jasper-round apply --expected-fingerprint <fp>                          (methodology never names it)
   # re-measure: stage, open --stage post_apply, wait, bank                  ⚠ (flag in no doc)
<V>/jasper-round-views frozen <baseline-round> <new-round>
```

Record what you had to look up, what took longer than the doc implied, and
every screen that read wrong. That log is batch 2.
