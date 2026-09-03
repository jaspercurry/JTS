# Recon 07 — the tuning documentation

Tree: `/home/user/JTS` @ `c032503` (branch `claude/busy-goodall-mz0gvv`, 2026-09-03).
All commands below are reproducible from the repo root. Git history is **shallow**
(`.git/shallow`, 623 commits) — "did the plan land" is answered by grepping HEAD, not
by `git log`.

## 0. The corpus, measured

```
wc -l docs/*.md docs/*/*.md docs/adr/*.md
```

| Tier | Files | Lines |
|---|---:|---:|
| Canonical three (doctrine / methodology / runbook) | 3 | **2,661** |
| Plans + cutover scaffolding | 6 | **6,034** |
| Design docs | 6 | **6,761** |
| Work orders (gating / room regime / two-stage) | 3 | **1,914** |
| Bass extension (HANDOFF + waves/) | 15 | **8,254** |
| `correction-ux-wave3/` | 8 | **2,362** |
| Deep-research + e2e log (top level) | 3 | **584** |
| `docs/research/**` | 54 | **10,781** |
| `docs/historical/**` (tuning subset) | 11 | **14,308** |
| Audit trio | 3 | **1,712** |
| **Total tuning doc surface** | **112** | **≈55,371** |

ADRs: 141 files, 9,569 lines. **No duplicate numbers, no gaps** (`0001–0019`,
`0100–0221`; the 20–99 band is a deliberate two-series split, verified by
`ls docs/adr | sed 's/^0*//;s/-.*//' | sort -n`). `#3745` already fixed the one
duplicate (0220→0221). There is **no ADR index file** — `docs/README.md` links the
directory only.

---

## 1. Per-doc verdicts

Legend: **hit rate** = fraction of backticked symbols / flags / env vars / paths in
the doc that resolve at HEAD, via `python3 /tmp/chk2.py <doc>` (git-grep over
`jasper scripts deploy rust c experiments tests pyproject.toml`, plus path-existence
for module names). Raw misses were then hand-triaged; the rate below is after
triage.

### 1a. The three canonical docs — all KEEP

| Doc | Lines | Status header | Owns | Hit rate | Verdict |
|---|---:|---|---|---:|---|
| `measurement-loop-doctrine.md` | 368 | "canonical doctrine, ratified 2026-08-21" | What may I try, what stops me, who decides | **51/55 = 92.7%** | **KEEP** (one deletion, §2.1) |
| `tuning-methodology.md` | 859 | "Written to you, the driving LLM"; speaker-agnostic | Order of operations + decision rule per step | **139/139 = 100%** | **KEEP** |
| `tuning-operator-runbook.md` | 1434 | "Operational map (current truth), not a script" | Which verb, flag, receipt field, log line | **494/499 = 99.0%** | **KEEP** (three fixes, §2.2–2.4) |

**They do not overlap each other.** Measured with a 6-gram shingle intersection over
sentences >60 chars, code spans and links stripped:

| Pair | Shared 6-grams | % of smaller doc |
|---|---:|---:|
| doctrine ↔ methodology | 13 | 0.54% |
| doctrine ↔ runbook | 9 | 0.37% |
| methodology ↔ runbook | 64 | 1.03% |

The shared n-grams are the deliberate one-line quotes each doc's own header table
says it is quoting ("a refusal that is not one of the five…", "**the LLM recommends;
the measurement decides**"). **The three-doc split is the healthiest thing in this
corpus — leave the boundary alone.**

The runbook's tool menu is **machine-generated** from each CLI's argparse metadata
(`scripts/generate-tuning-tool-menu.py`, markers at `tuning-operator-runbook.md:468`
/ `:487`) and CI-pinned by `tests/test_tuning_tool_menu_generator.py`. That is the
right pattern and explains the 99% hit rate. **The hand-written tables next to it are
where the rot is** (§2.2–2.4).

### 1b. Plans and cutover scaffolding

| Doc | Lines | Header claim | HEAD says | Verdict | Δ |
|---|---:|---|---|---|---:|
| `tuning-master-plan.md` | 788 | "adopted plan, execution in progress"; Waves 1–3, 6 open | Wave-3 modules (`candidate_space.py`, `fc_selector.py`, `objective.py`, `XoverCandidate`, `STAGE1_INCLUDES_LATERAL`) absent — correct for unbuilt work. 123/133 = 92.5%, all misses are future work | **KEEP** — the one planning authority | 0 |
| `REFACTOR-TUNING-2026-08.md` | 1868 | "FINAL — all gates settled 2026-08-25" | 232/261 = 88.9%. Names `RawCaptureTransport`, `SummedCaptureProducer`, `commissioning_capture_producer.py` (deleted by ADR-0197), `derive_position_curves.py`, `scripts/right-size-report.sh`, and 5 test files that don't exist | **DELETE** after moving §1's refusal census (§2.1) | −1,868 |
| `REFACTOR-CUTOVER-2026-08.md` | 1693 | "Chunk 2 of the tuning refactor" | 371/393 = 94.4%. Its **§2 "The analyze registry — VERIFIED-COMPLETE"** plans a subsystem ADR-0198 deleted: `AnalyzeOutcome`, `RecommendOutcome`, `Recommender`, `PriorBank`, `FakeRecommender`, `crossover_v2/prior_bank.py:52` — all `<<NONE>>` at HEAD. `session.py` has exactly `open`/`close`/`measure` (`grep -n 'async def' session.py`) | **DELETE** | −1,693 |
| `cutover-briefs-acceptance.md` | 822 | "**THIS DOCUMENT DIES WHEN ACCEPTANCE PASSES**"; own banner: "superseded by ADR-0192 — read that first" | ADR-0192 (2026-08-29) retired row 9's 0.37 dB bar outright. 131/153 = 85.6%. Cites `docs/REFACTOR-2026-08.md:279-284` — **file does not exist** | **DELETE** — it says so itself | −822 |
| `cutover-map-flow.md` | 328 | "STATUS: derived at HEAD `c253c3cf1`, file = **9,228 lines**. Every line number below is that tree" | `wc -l jasper/active_speaker/crossover_v2_flow.py` = **7,839**. 623 commits since c253c3cf1. Every band boundary in the table is off by up to 1,389 lines | **DELETE** | −328 |
| `cutover-map-web.md` | 535 | "…without re-reading 8,088 lines"; "TOTAL 47 / **8,088** / 100%" | `wc -l jasper/web/correction_crossover_v2.py` = **7,831** | **DELETE** | −535 |

Verification for the two maps:
```
git show c253c3cf1:jasper/active_speaker/crossover_v2_flow.py | wc -l   # 9228
git show c253c3cf1:jasper/web/correction_crossover_v2.py     | wc -l   # 8088
wc -l jasper/active_speaker/crossover_v2_flow.py jasper/web/correction_crossover_v2.py  # 7839 7831
git rev-list --count c253c3cf1..HEAD                                    # 623
```
Both maps say in their own banner "this map dies when tier 7 completes" and "re-derive
before you cut". Tier 7 has not completed — but a map whose every coordinate is wrong
is worse than no map, and re-deriving it is cheaper than reading it. The `#3724`
seam-3 PR in flight deletes `capture_source.py` and the per-source forks these maps
index, which will widen the gap again.

### 1c. Design docs

| Doc | Lines | Header | HEAD | Verdict | Δ |
|---|---:|---|---|---|---:|
| `active-crossover-information-design.md` | 2407 | "design of record"; "Wave 1 implementation boundary (2026-07-13): **contract-only**" | **162/209 = 77.5% — worst in the corpus.** 25 `reconstruction_*` refusal codes, `jts_active_speaker_reconstruction_geometry`, `sealed_single_radiator_v1`, `LevelSolveRefused`, `mark_level_run_*` — none exist (`git grep -c reconstruction -- jasper` hits only unrelated uses in `driver_safety.py`/`distortion.py`). §"Low-frequency reconstruction contract" (`:1147–1200`) is a contract that never shipped | **KEEP the shipped half, DELETE the unshipped contract sections** (~600 lines) | −600 |
| `room-correction-information-design.md` | 661 | "design of record" | 18/19 = 94.7% (only `jasper/correction/preferences.json` missing) | **KEEP** | 0 |
| `active-speaker-tuning-layers-design.md` | 1942 | "adopted direction, owner-approved 2026-07-23"; "execution handoff for the implementing session" | 185/199 = 93.0%. Names `fc_selector.py`, `e0_capture.py`, `XoverCandidate`, `lateral_adjudicates()`, `--reset-first`, `branch_operator_by_role`, `driver_plants` — absent. The one live fact (the layering rule) is already in `measurement-loop-doctrine.md:39` §1a | **FOLD** the tolerance table into methodology §6, **ARCHIVE** the rest | −1,800 |
| `crossover-v2-engine-design.md` | 553 | "**Status: historical design record.** ADR-0198 removed the… methods and seams described by parts of this file" | 162/169 = 95.9% but self-declared superseded; `REFACTOR-CUTOVER` still cites it as "ground truth this plan builds on" (`:8`) | **ARCHIVE** to `historical/` | −553 |
| `crossover-measurement-productization-design.md` | 871 | "Shipped (2026-07-19)… read this doc for the decision archaeology" | Self-declared archaeology; current truth already delegated to the runbook | **ARCHIVE** | −871 |
| `correction-journey-design.md` | 327 | "design record, **not yet implemented** (2026-07-19)… every file/function named here was verified to exist at `748f8d7a8`" | **37/45 = 82.2%. Every deliverable it specifies is absent**: `jasper/web/correction_journey.py`, `build_correction_journey`, `read_journey_inputs`, `JourneyInputs`, `available_actions`, `deploy/assets/correction/js/shared/journey-strip.js`, `tests/test_correction_journey.py` | **DELETE** — 6 weeks unimplemented, superseded in practice by the two-stage flow | −327 |

### 1d. Work orders — all landed or abandoned

| Doc | Lines | Header | HEAD | Verdict | Δ |
|---|---:|---|---|---|---:|
| `gating-v2-plan.md` | 469 | "adopted work order (2026-07-27)", issue #1790 | **75/83 = 90.4%; the deliverable vocabulary never landed**: `gate_anomaly`, `gate_anomaly_retained`, `invariant_family`, `suspect_near_search_start`, `geometry_inconsistent`, `insufficient_spatial_support`, `MF_REL`, `measured_candidate.py`'s `near_validity_floor` — none in `jasper/audio_measurement/gate_disclosure.py` at HEAD | **ARCHIVE** (abandoned; if the D1/D2 rulings still bind, they belong in an ADR) | −469 |
| `room-correction-regime-plan.md` | 471 | "adopted work order (2026-07-27)", issue #1791 | 48/49 = 98.0% — **landed** | **ARCHIVE** to `historical/` | −471 |
| `two-stage-commission-flow-plan.md` | 974 | "adopted work order (2026-07-28)", issue #1806 | 122/127 = 96.1% — **landed** (misses are two deleted JS tests and `prepare_v2_verify`/`retain_position`, casualties of the relay deletion) | **ARCHIVE** to `historical/` | −974 |

A landed work order is a decision record. If any of its rulings still bind, the
ruling goes to `docs/adr/` (AGENTS.md: "decisions go to `docs/adr/`"); the work
order itself is git history.

### 1e. Bass extension — the ADR-0199 violation

| Doc | Lines | Verdict |
|---|---:|---|
| `HANDOFF-bass-extension-plan.md` | 1931 | **OWNER CALL** — see §3 |
| `bass-extension-waves/*.md` (14 files) | 6,323 | **DELETE** |

`docs/bass-extension-waves/README.md:1` — "one self-contained implementation prompt
per wave… start a fresh Codex session per wave". These are **execution prompts for a
program ADR-0018 froze**. ADR-0018 §3 says the wire-up spec "lives in #1738 and in
`docs/tuning-master-plan.md` ticket 4.4" — it does **not** name the wave kit as part
of the park. 6,323 lines of delegation prompts for work nobody is authorized to do.

### 1f. Research and history

| Doc / dir | Lines | Verdict |
|---|---:|---|
| `crossover-design-guide-deep-research-2026-08-19.md` | 250 | **MOVE** to `docs/research/2026-08-19-crossover-design-guide/` — it is tagged `Status: research` and `doc-map.toml`'s `research` class already says "no synthesized design doc cites it yet" |
| `crossover-measurement-deep-research-2026-07-18.md` | 145 | **MOVE** to `docs/research/2026-07-18-.../` (same reason) |
| `crossover-room-e2e-validation-log.md` | 189 | **ARCHIVE** — "session artifact (living log)", 2026-07-17, screenshots live outside the repo |
| `docs/research/**` | 10,781 | **KEEP** — verbatim primary sources, correctly quarantined, 10 dated subdirs each with a README |
| `docs/historical/**` (tuning subset) | 14,308 | **KEEP as files**, but fix the code citations — see §2.6 |

### 1g. Audit trio

| Doc | Lines | Verdict |
|---|---:|---|
| `DEEP-AUDIT-2026-08-25.md` | 207 | **KEEP** — frozen snapshot, ADR-0199 explicitly exempts dated `DEEP-AUDIT-*` from truing, and §4.5 is a live, still-unspent handoff to this exact effort ("the zero-feature-loss cuts here are prose ~30K and test docstrings ~25K") |
| `REVIEW-deep-audit-ledger.md` | 969 | **DELETE** — `open: 0`, `in-progress: 0`, `deferred: 0`, `fixed: 639` across 680 `DA-` rows. Fully discharged; git history is the archive |
| `audit-pending-followups.md` | 536 | **out of tuning scope** — `grep -ci 'crossover\|active_speaker\|correction\|measurement'` = **0**. Leave to another agent |
| `correction-ux-wave3/*.md` | 2,362 | **DELETE** — Codex delegation kit whose plan of record is `campaign/VALIDATION-FIX-PLAN.md`, which the README itself says is "**not part of this git repository**". Prompts point at `jasper/web/correction_entry_flow.py`, `load_crossover_declaration`, `clear_crossover_declaration` — all absent (72–84% hit rates) |

---

## 2. Findings that need a fix regardless of what gets deleted

### 2.1 The doctrine's closed-deviation map names four dead symbols

`docs/measurement-loop-doctrine.md:265–284`. The section says the nine-row deviation
table "is deleted because a positively complete list is what it was compensating
for", then keeps a nine-row **map** of it. Four entries name constants that do not
exist anywhere in the tree:

```
git grep -n 'BOOST_VERTICALLY_BLIND\|FC_REJECT_BEAMING\|REASON_CORRECTION_NOT_AN_IMPROVEMENT\|REASON_DRIVER_LEVELS_DISAGREE' -- jasper deploy scripts   # no output
```

- `:268` **(a)** `BOOST_VERTICALLY_BLIND` — closed (#2805)
- `:269` **(b)** `FC_REJECT_BEAMING` — closed (#2853)
- `:270` **(c)** `REASON_CORRECTION_NOT_AN_IMPROVEMENT` — closed (2026-08-22)
- `:283` **(i)** `REASON_DRIVER_LEVELS_DISAGREE` — closed (#2937)

This is exactly the "history, dates, PR numbers" AGENTS.md bans. The map exists so a
stale *comment* citing `deviation (c)` resolves — but `tuning-operator-runbook.md:1336`
still cites "doctrine deviations (c)/(i)" too, so the map is propping up two other
docs' history rather than the code's. **Delete `:262–284` (23 lines) and the two
citations.** Low risk. Also delete the doctrine's dependency on
`REFACTOR-TUNING-2026-08.md §1` for the "~112 enforcement points / ~100 integrity
refusals" census (`:159–167`, `:288–292`) — there is no census script
(`ls scripts | grep -i census` → nothing), so those numbers are hand-counted, dated
2026-08-25, and cited from a doc slated for deletion. Either state them uncounted
("families, not sites") or land a script.

### 2.2 The runbook's door table is wrong in 4 of 5 rows

`docs/tuning-operator-runbook.md:557–565` — "Five prescription doors, one refusal
vocabulary each, **counted at HEAD**":

| Door | Runbook says | HEAD | Source |
|---|---:|---:|---|
| alignment | 9 | **10** | `crossover_v2/alignment_prescription.py:207` |
| topology | 9 | **10** | `crossover_v2/topology_prescription.py` |
| blend | 17 | **15** | `crossover_v2/blend_prescription.py` |
| driver | 17 | **15** | `crossover_v2/driver_prescription.py` |
| spool | 4 | 4 ✓ | `crossover_v2/prescription_spool.py` |

Reproduce: `ast.parse` each module, find `*_REFUSAL_REASONS`, count `frozenset({…})`
members (script in this session's scratch). The very next paragraphs (`:567–579`) are
a dated changelog — "dropped six on 2026-08-23", "dropped two more on 2026-08-29",
"lost `outside_declared_search_band` when #2870…" — narrating the drift that produced
the wrong numbers. **Fix:** extend `scripts/generate-tuning-tool-menu.py` (or a
sibling) to render this table from the five frozensets, delete the changelog
paragraphs, keep only the two rulings that still bind (`boost_route_unavailable` R8;
`driver_trim_pin_malformed`'s shape). ~−25 lines, medium value, low risk; the
existing `tests/test_tuning_tool_menu_generator.py` pattern is the proof.

### 2.3 The runbook names five refuse codes under the wrong prefix

`docs/tuning-operator-runbook.md:1145–1149` (the gate-sweep section):

> `gate_sweep_no_captures` and `gate_sweep_no_programs` … `gate_sweep_program_hash_unmatched`,
> `gate_sweep_radiated_band_missing`, `gate_sweep_capture_unreadable`

HEAD emits these from the shared discovery module, prefixed `round_`:

```
jasper/active_speaker/crossover_v2/round_captures.py:38  REFUSE_NO_CAPTURES        = "round_no_captures"
:39  REFUSE_NO_PROGRAMS = "round_no_programs"   :40  REFUSE_PROGRAM_UNMATCHED = "round_program_hash_unmatched"
:41  REFUSE_RADIATED_BAND_MISSING = "round_radiated_band_missing"   :42  REFUSE_CAPTURE_UNREADABLE = "round_capture_unreadable"
```

`jasper/cli/gate_sweep.py:38` imports `RoundCapturesRefused` from that module. Only
`gate_sweep_reference_band_empty` (`gate_sweep.py:221`) and `gate_sweep_single_pose`
(`:222`) are genuinely gate-sweep-owned. **The same file gets it right 100 lines
later** — `:1256` correctly says `round_radiated_band_missing`,
`round_capture_unreadable`. An LLM branching on the documented slug would never match.
Fix: 5 words. Risk: low. Proof: `git grep -F '"round_no_captures"'`.

### 2.4 Five shipped tuning CLIs are invisible in the tool menu

The generated menu (`:468–487`) has 18 rows. These `[project.scripts]` entries in
tuning scope are absent from it:

| Entry point | Module | Note |
|---|---|---|
| `jasper-project-ring` | `jasper/cli/project_ring.py` | **has an exit-code row at `:692`** and a mention at `:702` — reachable by exit code, unreachable by menu |
| `jasper-active-speaker` | `jasper/cli/active_speaker.py` | |
| `jasper-active-speaker-attempts-replay` | `jasper/cli/active_speaker_attempts_replay.py` | |
| `jasper-active-speaker-emit-bench` | `jasper/cli/active_speaker_emit_bench.py` | |
| `jasper-bass-extension-bench` | `jasper/cli/bass_extension_bench.py` | parked program — arguably correct to omit |
| `jasper-correction-bundle` | `jasper/cli/correction_bundle.py` | |
| *(none)* | `jasper/cli/measurement_mic.py` | **no entry point at all** — imported by `seat_level.py`, so not dead, but not a CLI either. Hand to the CLI-scope agent |

`TUNING_TOOL_MODULES` in `scripts/generate-tuning-tool-menu.py` is a hand-maintained
roster, so a new CLI does not appear until someone adds it. The docstring's stated
exclusion rule ("no `build_parser()` this script can safely import") does not cover
these. **The BRIEF's question "anything the LLM driver cannot reach from the runbook's
tool menu" answers here: six tools.** Either add them or add a test that
`[project.scripts]` ∩ tuning-scope ⊆ roster ∪ documented-exclusions.

### 2.5 Four docs cite a file that does not exist

```
grep -rn 'REFACTOR-2026-08' docs/ | grep -v 'REFACTOR-TUNING\|REFACTOR-CUTOVER'
```
- `docs/cutover-briefs-acceptance.md:465` — cites `docs/REFACTOR-2026-08.md:279-284` **by line number**
- `docs/install-hardware-tier-and-staleness.md:449`
- `docs/adr/0177-…:72` — correctly exempt (ADRs are append-only)
- `docs/doc-map.toml:762` — a live routing comment

`ls docs/REFACTOR-2026-08.md` → No such file. `docs-linkcheck.py` did not catch it
because it only checks *changed* Markdown files ("no changed Markdown files to
check"), and `docs-impact.py --validate-only` only validates the `docs = [...]`
arrays, not prose citations.

### 2.6 Code cites `docs/historical/` for live constraints

`docs/README.md:60` — "Historical files preserve evidence and provenance. **They do
not describe the current repository or deployed speaker.**" Yet:

```
grep -rn 'linearization-campaign-2026-07' jasper/ | wc -l    # 16 files
grep -rln 'attribution-stage-plan'        jasper/ | wc -l    #  9 files
```

`jasper/active_speaker/flat_spec.py` (×3), `crossover_v2/contracts.py` (×2),
`crossover_v2/capture_plan.py` (×2), `audio_measurement/interference_nulls.py` (×2),
`linearization_fit.py`, `delta_probe.py`, `capture_geometry.py`, `spatial_combine.py`
and others point at `historical/linearization-campaign-2026-07.md` as the source of a
tolerance or a frame. `docs/active-speaker-tuning-layers-design.md:11` calls it
"(adopted)". `docs/two-stage-commission-flow-plan.md:11` "supersedes §2.2 and §2.6 of"
it. **A file in the archive cannot be adopted, superseded, or a constraint source.**
The constants it owns belong in code (a named constant with a units comment) or in
ADR-0194, which already partially supersedes it. Same for `attribution-stage-plan.md`
(cited by 6 files under `jasper/attribution/`).

### 2.7 `HANDOFF-bass-extension-plan.md` survives a "resurrect condition: none" ADR

ADR-0199 (2026-08-30, Accepted): "**All 56 `docs/HANDOFF-*.md` files** and the one
copy renamed into `docs/historical/` are removed in the same PR… **Resurrect
condition: none.** … no process should treat resurrecting one as a live document as a
valid resolution to a future gap."

```
git log --oneline --all -- 'docs/HANDOFF-*.md'
  7266e6ab  ruling 13: the HANDOFF corpus dies (#3345)
  74bef069  docs: restore bass-extension plan of record (#3351)   # +1932 lines, 2026-08-30, owner
```

The file was deleted by ruling 13 and restored the same day, by the owner, in a
2-line commit with no rationale and **no superseding ADR**. `docs/README.md:47`
retroactively justifies it ("remains the parked plan and authorization source under
ADR-0018") — but ADR-0018 (2026-08-25) predates ADR-0199 and rules on
`jasper/bass_extension/` **the code**, never the doc; its §3 points the wire-up spec
at #1738 and `tuning-master-plan.md` ticket 4.4 instead.

**This is the one item in my area I will not decide.** Three consistent options:
1. Write an ADR amending 0199 with a named exception, and **rename the file out of
   the `HANDOFF-` namespace** (e.g. `docs/bass-extension-plan.md`) so no future sweep
   re-litigates it. Cheapest, keeps the 1,931 lines.
2. Delete it (ADR-0199 as written), keeping ADR-0018 + #1738 + ticket 4.4 as the
   authorization chain ADR-0018 §3 already names. −1,931.
3. Move to `docs/historical/` — explicitly rejected by ADR-0199's last paragraph.

Either way the 14 `bass-extension-waves/` prompts (6,323 lines) go: they are Codex
execution contracts, not authorization, and ADR-0018 does not cover them.

### 2.8 `doc-map.toml` routes one code change to 19 docs

`docs/doc-map.toml:696–800`, subsystem `room-correction-and-calibration`:
`code = [jasper/active_speaker/**, jasper/attribution/**, jasper/bass_extension/**,
jasper/correction/**, jasper/audio_measurement/**, jasper/calibration_agent/**,
jasper/web/correction_*.py, …]` → `docs = [19 entries]`. Touching *any* file in the
tuning stack routes a maintainer to 19 documents totalling ~14,000 lines. The map's
own preamble says "Keep entries coarse; false positives are okay" — but 19 is not a
routing hint, it is a reading list nobody reads. After §4's cuts this becomes 5.

**Note:** `scripts/docs-impact.py --validate-only` passes (`31 subsystem mappings
valid; 9 classified docs valid`) — every listed doc exists. It validates existence,
never currency, which is why 22%-stale docs sail through.

---

## 3. Doc restatement — how many places state one fact

Method per fact: a distinctive phrasing grep across `docs/*.md`, `docs/*/*.md`,
`docs/adr/*.md`, and `jasper/`. Counted as *files*, not occurrences.

| Fact | Live docs | Archived docs | ADRs | Code files | Verdict |
|---|---:|---:|---:|---:|---|
| **The five hard-stop clamps** (closed list) | 4 (doctrine, methodology, runbook, master-plan) + `REFACTOR-TUNING §1` | 3 | 2 (0002, 0101) | 23 (CLAMP/hard-stop vocabulary) | **Acceptable** — doctrine states it once at `:156`; the other three point at it. The code's 23 hits are the taxonomy vocabulary, not restatements. `REFACTOR-TUNING §1` holding the census is the one real dependency (§2.1) |
| **The layering rule** ("plays through layer N and below, never preference EQ above") | **2** (doctrine `:39` §1a, methodology) | 0 | 0 | **1** (`crossover_v2/tuning_scope.py`) | **Exemplary.** This is the target shape for every other fact |
| **Round cap of 3** (`ROUND_SERIES_CAP = 3`, extendable to 4 per #2602) | 3 (doctrine `:110`, runbook `:794`, layers-design `:641`) | 3 | 0 | 8 + 6 test files | **Acceptable** — the constant lives once (`crossover_v2/round_evidence.py:226`); `crossover_envelope_v2.py:2135` even warns against "spelling the number into copy" |
| **`devices.volume_limit = 0.0`** | **9** live docs | 8 | 4 | 20+ (`camilla_yaml.py` alone ×41) | **Over-stated.** It is a non-negotiable (AGENTS.md #1), so redundancy is cheap insurance — but 9 live docs is 6 too many. After §4's cuts: 4 |
| **The measure-again discriminator** | 4 (doctrine, master-plan, REFACTOR-TUNING, REFACTOR-CUTOVER) | 2 | **8** ADRs cite ADR-0002 | 1 (`refusal_copy.py`) | **Healthy** — 8 ADRs *citing* 0002 is the append-only store working as designed, not restatement |

**Conclusion:** the corpus's restatement problem is **not** in the canonical three or
the ADRs — it is concentrated in the plan/cutover/design tier, which restates the
doctrine to justify its own scope. Deleting that tier (§4) fixes the restatement
count without touching a single canonical sentence.

---

## 4. Proposed end state for the tuning docs

**Target: the canonical three + the master plan + two product contracts + ADRs +
`research/` + `historical/`.** Nine live files instead of 27.

### Keep (7 files, 6,724 → ~6,100 lines after trims)

| File | Lines | Change |
|---|---:|---|
| `measurement-loop-doctrine.md` | 368 | −23 (§2.1: the deviation map) |
| `tuning-methodology.md` | 859 | +~30 (absorbs the layers-design tolerance table) |
| `tuning-operator-runbook.md` | 1434 | −25 (§2.2 door table → generated; §2.3 prefix fix; §2.4 six CLIs added) |
| `tuning-master-plan.md` | 788 | 0 — the one planning authority |
| `active-crossover-information-design.md` | 2407 | −600 (§1c: the never-shipped reconstruction contract) |
| `room-correction-information-design.md` | 661 | 0 |
| `DEEP-AUDIT-2026-08-25.md` | 207 | 0 — frozen, and §4.5 is live input to this effort |

Plus `docs/adr/**` (141 files — ~40 concern tuning), `docs/research/**` (+2 moved in),
`docs/historical/**` (+6 moved in).

### Delete (34 files, −15,696 lines)

`REFACTOR-TUNING-2026-08.md` (1868), `REFACTOR-CUTOVER-2026-08.md` (1693),
`cutover-briefs-acceptance.md` (822), `cutover-map-flow.md` (328),
`cutover-map-web.md` (535), `correction-journey-design.md` (327),
`gating-v2-plan.md` (469), `REVIEW-deep-audit-ledger.md` (969),
`correction-ux-wave3/**` (2362, 8 files), `bass-extension-waves/**` (6323, 14 files).

### Archive to `docs/historical/` (6 files, −4,000 live lines)

`crossover-v2-engine-design.md` (553), `crossover-measurement-productization-design.md`
(871), `crossover-room-e2e-validation-log.md` (189),
`two-stage-commission-flow-plan.md` (974), `room-correction-regime-plan.md` (471),
`active-speaker-tuning-layers-design.md` (1942, after folding ~30 lines out).

### Move to `docs/research/` (2 files, 395 lines)

The two deep-research reports — they are already tagged `Status: research` and
classified as such in `doc-map.toml`.

### Owner call

`HANDOFF-bass-extension-plan.md` (1931) — §2.7.

### Net

**−15,696 deleted outright; −4,000 more out of the live tier.** The live tuning doc
surface drops from **27 files / ~26,000 lines** to **7 files / ~6,100 lines**, a
**77% reduction**, with no live operational fact lost — every deletion above is
either self-declared spent, provably unimplemented, or provably landed.

---

## 5. Ranked top moves

| # | Move | Δ lines | Risk | Proof it is safe |
|---:|---|---:|---|---|
| 1 | Fix the runbook's five `gate_sweep_*` refuse codes to `round_*` (`:1145–1149`) | ~0 | **low** | `round_captures.py:38-42`; the same file already says `round_*` at `:1256` |
| 2 | Delete the two cutover maps — every line number stale by 1,389 / 257 | −863 | **low** | `git show c253c3cf1:… \| wc -l` vs `wc -l`; both banners say "re-derive before you cut" |
| 3 | Delete `cutover-briefs-acceptance.md` | −822 | **low** | Its own banner: "THIS DOCUMENT DIES WHEN ACCEPTANCE PASSES", "superseded by ADR-0192" |
| 4 | Delete `REVIEW-deep-audit-ledger.md` | −969 | **low** | `open: 0 / in-progress: 0 / deferred: 0` |
| 5 | Delete `correction-ux-wave3/**` | −2,362 | **low** | Plan of record is outside the repo by its own README; deliverables absent at HEAD |
| 6 | Generate the runbook's door table; delete its dated changelog (`:557–579`) | −25 | **low** | 4/5 rows already wrong; mirror `tests/test_tuning_tool_menu_generator.py` |
| 7 | Delete `correction-journey-design.md` + `gating-v2-plan.md` | −796 | **low** | Zero deliverables at HEAD for either |
| 8 | Delete `bass-extension-waves/**` | −6,323 | **low-med** | ADR-0018 parks the *code* and names #1738 + ticket 4.4 as the spec; the wave prompts are execution contracts for unauthorized work. Confirm with owner alongside §2.7 |
| 9 | Delete `REFACTOR-CUTOVER-2026-08.md` (its §2 plans what ADR-0198 deleted) | −1,693 | **medium** | `session.py` has only `open`/`close`/`measure`; `Recommender`/`PriorBank`/`AnalyzeOutcome` absent. Read §6.1–6.3's three rulings first — promote any that still bind to ADRs |
| 10 | Delete `REFACTOR-TUNING-2026-08.md` **after** relocating §1's refusal census | −1,868 | **medium** | Doctrine `:159` and `:288` depend on its counts. Land a census script or restate the numbers as families, then cut |
| 11 | Trim `active-crossover-information-design.md`'s reconstruction contract (`:1147–1200` +) | −600 | **medium** | 25 refusal codes with zero implementation; check no in-flight PR is building them |
| 12 | Fold the layers-design tolerance table into methodology §6; archive the rest | −1,800 | **medium** | 93% hit rate but its named modules (`fc_selector`, `e0_capture`, `XoverCandidate`) never shipped |
| 13 | Repoint 25 code citations off `docs/historical/**` onto constants + ADR-0194 | ~0 | **medium** | `docs/README.md:60` says historical does not describe the current repo |
| 14 | Fix the 3 non-ADR citations of the non-existent `docs/REFACTOR-2026-08.md`; make `docs-linkcheck.py` run tree-wide, not changed-files-only | ~−5 | **low** | `ls docs/REFACTOR-2026-08.md` → absent |
| 15 | Shrink `doc-map.toml`'s `room-correction-and-calibration` docs list 19 → 5 | −60 | **low** | Falls out of moves 2–12; `docs-impact.py --validate-only` proves it |
| 16 | **Owner:** ADR + rename, or delete, `HANDOFF-bass-extension-plan.md` | 0 or −1,931 | — | ADR-0199 "resurrect condition: none" vs `74bef069` |

## 6. Uncertainty

- **Shallow git history** (623 commits) means I could not check whether a plan's
  symbols *once* existed and were renamed vs never built. I resolved every miss by
  reading HEAD, which is the AGENTS.md-sanctioned method, but "abandoned" vs
  "renamed" is inference in a few rows (gating-v2 especially).
- **`REFACTOR-CUTOVER §6.1–6.3`** contain three owner rulings (`NO HOME 1/2/3`) that
  I did not fully read. Move 9 must promote any still-binding ruling to an ADR first.
  Same caution for `REFACTOR-TUNING §4`'s twelve rulings S1–S12 — ADR-0198 already
  supersedes S1 in part (`0198:76`), which is evidence the others may be live.
- I did **not** verify whether in-flight PRs #3724/#3719 (relay deletion, ADR-0220)
  already delete some of these docs. #3719 is "docs: relay leaves documentation" —
  check for conflict before touching `two-stage-commission-flow-plan.md` and
  `active-crossover-information-design.md`'s `mark_level_run_phone_*` section.
- The hit-rate method under-reports: it cannot catch a doc whose *prose* is wrong
  while its symbols are right (the door table, §2.2, was found by hand). Treat 99%
  on the runbook as "its symbols resolve", not "it is accurate".
