# Phase 0 cartography — docs, debt markers, doc/code drift

Checkout: `/home/user/JTS` @ `2d571e6b8`. All findings verified against files opened this session unless marked grep-only.

## 1. Docs inventory

`docs/` = 295 `.md` files / 77,625 lines (157 ADRs, 11,149 lines; 138 non-ADR, ~66.5K lines). Root `.md` = 12 files.

| Zone | Files | Lines | In `docs/README.md`? | In `doc-map.toml`? |
|---|---|---|---|---|
| `docs/*.md` (top-level, 33 files) | 33 | 23,075 | mapped or classified for all 33 (CI-verified, see below) | same |
| `docs/adr/*.md` | 157 | 11,149 | linked as a directory | not routed (by design — ADRs aren't code-change targets) |
| `docs/research/**` | ~35 | — | linked as a directory; `document_classes.research` covers 1 file explicitly | 5 subdir READMEs individually routed from room-correction subsystem |
| `docs/historical/**` | 19 | ~19K | linked as a directory; explicitly excluded by `test_historical_docs_are_never_canonical_routes` | excluded by the same test |
| `docs/bass-extension-waves/**` | 16 | ~5.6K | **not linked anywhere in `docs/README.md`** | **not in `doc-map.toml`** |
| `docs/ux-audit-2026-09-03/**` | 9 | ~3.7K | reachable only via `docs/UX-AUDIT-2026-09-03.md`'s own link | **not in `doc-map.toml`** |
| `docs/examples/` | 1 (`.py`, not `.md`) | — | linked from 2 docs | n/a |

**The "no-orphan-doc" invariant is narrower than it looks.** `tests/test_docs_impact.py::test_root_and_top_level_docs_are_intentionally_mapped` globs only `ROOT.glob("*.md")` and `(ROOT/"docs").glob("*.md")` — **non-recursive**. `docs/historical/` gets its own dedicated test excluding it by name (`test_historical_docs_are_never_canonical_routes`); `docs/research/` is exempted by a `doc-map.toml` header comment. **`docs/bass-extension-waves/` (16 files) and `docs/ux-audit-2026-09-03/` (9 files) have no test and no comment excluding them — they are invisible to the orphan sweep by accident of directory depth, not by declared policy.** Today both happen to be reachable by hand-written links (bass-extension-waves ← `HANDOFF-bass-extension-plan.md` ← `docs/README.md`; ux-audit subdir ← `UX-AUDIT-2026-09-03.md`), so nothing is *currently* dangling, but nothing would catch it if one went stale. — Should-fix: extend the orphan-sweep glob to `docs/**/*.md` (excluding `historical/` and `research/` as today) or add explicit `document_classes` entries for the two waves/audit directories, matching the historical-docs precedent (`tests/test_docs_impact.py:52-90`).

**Dangling map entries:** zero. Every path in `doc-map.toml`'s `docs =` lists and `document_classes` resolves to a real file (verified programmatically against `git ls-files`, and independently by `scripts/docs-impact.py --validate-only`'s own logic, which this session re-ran by hand).

**Orphan verdict:** 0 true orphans under the CI-enforced definition; 25 files (waves + ux-audit subdir) orphaned under a *recursive* definition, all currently reachable by hand.

## 2. Dead references

Scanned every markdown link and backtick path/command in `docs/**`, `README.md`, `AGENTS.md`, `BRINGUP.md`, `QUICKSTART.md`, `CONTRIBUTING.md`, `PLAN.md`, `CHANGELOG.md`, `.claude/**` (7,515 backtick/link candidates; most are code identifiers, not paths — narrowed to 260 real path-shaped candidates in non-historical/non-research/non-adr docs, then hand-verified). `README.md`/`AGENTS.md`/`BRINGUP.md`/`QUICKSTART.md`/`CONTRIBUTING.md`/`CHANGELOG.md` themselves: **zero broken links found** — the top-level agent-facing docs are clean.

| Doc:line | Reference | Status | Note |
|---|---|---|---|
| `docs/adr/0146:73` | `../HANDOFF-resilience.md` | dead | HANDOFF corpus deleted (ADR-0199); ADR is append-only, can't be fixed in place |
| `docs/adr/0115:48` | `../HANDOFF-barge-in.md` | dead | same |
| `docs/adr/0169:62` | `../HANDOFF-aec.md` | dead | same |
| `docs/adr/0018:16` | `docs/REFACTOR-TUNING-2026-08.md` | dead | "chunk 1" of the tuning refactor, retired into ADR-0228; file deleted, ADR-0018's citation wasn't |
| `docs/AEC-DIAG-06-xvf-format-level-profile.md:21-24` | `HANDOFF-xvf3800.md`, `HANDOFF-aec.md`, `CHIP-AEC-EXPERIMENT.md`, `HANDOFF-speaker-output-reference.md` | all 4 dead | this doc is **not** append-only and **not** in `docs/historical/` — it's a current `aec-and-mic` doc-map target; fixable |
| `docs/RESEARCH-pipewire-low-latency.md:179` | `docs/HANDOFF-audio-latency-foundation.md` | dead | tagged `document_classes.research`, i.e. still a "current reference"; fixable |
| `docs/DEEP-AUDIT-2026-08-25.md:136,148` | `HANDOFF-audio-measurement-core.md`, `HANDOFF-audio-graph-consolidation.md` | dead | self-declared "frozen snapshot," lower priority, but its own §4.6 "refuted — do NOT cut" list leans on the latter as if it's a live doc |
| `docs/bass-extension-waves/limiter-bench-runner-implementation.md:70` | `docs/CHIP-AEC-EXPERIMENT.md` | dead but **self-annotated** `"(deleted; see git history...)"` | correct model for how to cite a dead doc — contrast with AEC-DIAG-06 above |
| `docs/testing-tooling.md:82` | `tests/test_dependency_groups.py::test_hang_backstop_is_configured_and_uses_the_signal_method` | wrong file | function is real, now lives in `tests/test_build_and_ci_contracts.py:238` — moved, doc not updated |
| `docs/testing-tooling.md:116-117` | `tests/test_relay_worker_js.py`, `tests/test_capture_page_js.py` | don't exist | the described pattern (`shutil.which("node")` Node-harness tests) is real and used by 14 *other* files (`test_dialog_helper.py`, `test_landing_page_html.py`, `test_crossover_wizard_js.py`, …) — the two cited example filenames are stale |
| `docs/adr/0018:41-44` | `tests/test_bass_extension_plan_status.py::test_readme_does_not_claim_bass_extension_has_no_code` | wrong name | real sibling test is `test_bass_extension_unshipped_surfaces_do_not_exist` — content matches, name doesn't |
| `docs/multiroom-pairing-reliability-plan.md` (15 `::function` refs, e.g. `:489,595,600,688,691,724,...`) | assorted `file.py::function_name` | stale | doc is explicitly self-labeled "rescued plan, not yet executed" and `doc-map.toml` already excludes it from routing pending re-verification — consistent with its own disclaimer, not a new finding |

**Verified false positives worth naming** (so a future sweep doesn't re-flag them): `jasper/web/nav.py` (4 refs) is explicitly aspirational ("once it exists" / ledger row B.1, not yet built); `docs/dumb-endpoint-bringup.md`'s `deploy/endpoint/` describes a **rejected** proposal by name; `jasper/transit/active_transit` is a function, not a path; `docs/REFACTOR-CUTOVER-2026-08.md`'s ~30 dead citations (`program_analysis.py:6054`, `scripts/severed-twin-replay.py`, `tests/test_crossover_v2_conductor.py`, `scripts/right-size-report.sh`, …) all point at files deleted when the plan's own "god file dissolves" was executed — see §3, this is one symptom of a fully-landed plan, not 30 separate bugs.

## 3. Plans that look like the forbidden HANDOFF tier

Every plan-shaped doc found (`*plan*.md`, `*-waves/`, `REFACTOR-CUTOVER-*`, `UX-AUDIT-*`, `DEEP-AUDIT-*`, `audit-pending-followups.md`, `PROPOSAL-*`, `AEC-DIAG-*`) opens with an explicit `Status:` line — that convention is real and mostly honored. `PLAN.md` itself names none of them; per `docs/README.md`'s own rule ("other files named plan/proposal/research/review/audit are inputs or records... unless a current document says otherwise"), their liveness rests entirely on `doc-map.toml` routing + their own status line, which is a legitimate but fragile substitute for `PLAN.md` mentioning them.

| Doc | Self-declared status | Verified against code | Verdict |
|---|---|---|---|
| `docs/REFACTOR-CUTOVER-2026-08.md` (1,694 lines) | "STATUS — all eight sections VERIFIED-COMPLETE... NOTHING WAITS ON THE OWNER" (as of 2026-08-26) | `jasper/audio_measurement/program_analysis.py` (the "god file" this entire doc plans to dissolve) **is gone**; `crossover_v2_flow.py` shrank 13,459 → 4,636 lines between the 2026-08-25 audit and now; 3 of 4 companion scripts it cites are deleted | **Landed. Delete or fold into an ADR**, exactly like its sibling "chunk 1" already was (ADR-0228). Its own top banner promises this treatment ("Chunk 2... built beside the god files... the god files dissolve") but never received the chunk-1 retirement step. |
| `docs/ux-audit-2026-09-03/PROMPT-subwoofer-deletion.md` (201 lines) | work order | `UX-AUDIT-2026-09-03.md:338` ledger: `[x] P.2` — all 8 PRs merged; `jasper/bass_management.py` confirmed deleted; ADR-0236 records the same rulings and PR numbers | **Fully landed, duplicates ADR-0236. Delete.** |
| `docs/ux-audit-2026-09-03/PROMPT-balance-sync.md` | work order, two parts | Ledger: `[x] P.1` (`/balance/` deleted — confirmed, `deploy/assets/balance/` and its nginx block are gone) / `[ ] C.S4` (fold `/sync/` into pair page — confirmed still open, `/sync/` still stands alone) | **Partially landed. Keep until C.S4 lands**; Part 1's now-historical inventory (30+ line-pinned citations to now-deleted `balance_*` files) is dead weight inside a still-open file — trim Part 1 to a one-line "done, see ADR/PR" note. |
| `docs/UX-AUDIT-2026-09-03.md` | "a work queue with an execution ledger, not doctrine... delete this file and its directory when the ledger is done" | Ledger has real open rows (B.0a/B.0b/B.1/B.2, C.S4) | **Keep.** Correctly self-describes the AGENTS.md-compliant shape (delete-when-done work queue, not a permanent handoff). Not a violation. |
| `docs/audit-pending-followups.md` (530 lines) | tracks a "May 2026" audit's deferred items | Footer: **"Last verified: 2026-07-11"** — 56 days stale against today (2026-09-05) in a repo doing >4,100 PRs | **Should-fix.** Still routed from `doc-map.toml` (voice-runtime, security-oss subsystems) as current; re-verify or mark sections resolved. At least 3 of its "not yet built" env vars (`JASPER_OPENAI_IDLE_TIMEOUT_SEC` etc.) are still genuinely absent from code, so the content isn't wrong, just unattested for 8 weeks. |
| `docs/DEEP-AUDIT-2026-08-25.md` (207 lines) + `DEEP-AUDIT-PLAYBOOK.md` | self-declared "frozen snapshot... disposition belongs in a separate ledger, not in edits to this file" | Cross-checked 6 of its "verified deletions" (orbs.js, `channel_split.py`, `bass_alignment.py`+test refuted then re-confirmed dead by PR #2970, wizard per-unit services, `program_analysis.py`, wake-events 1 GiB→128 MiB cap) — **all already executed** | **Earns-its-keep as a frozen record**, but note for the owner: 11 days old and already substantially actioned — a good sign the process works, and a reason this Phase-0 report should feed a similar "ledger," not become a second untracked snapshot. |
| `docs/gating-v2-plan.md`, `room-correction-regime-plan.md`, `tuning-master-plan.md`, `research-tool-plan.md`, `tool-platform-plan.md`, `conversation-history-plan.md`, `PROPOSAL-dac-profile-registry.md` | all: "adopted work order" / "living plan" / "adopted plan, execution in progress" | Not independently re-derived line-by-line (budget) | **Keep** — self-status is internally consistent and each is routed from `doc-map.toml`; no evidence of being forbidden-tier. |
| `docs/multiroom-pairing-reliability-plan.md` | "rescued plan, not yet executed" | `doc-map.toml` already parks it out of routing pending re-verification (tracked by #1852); 15+ of its `file::function` citations are stale | **Keep, already correctly quarantined.** Not a new problem — the file's own header and the map's comment already say exactly this. |

## 4. ADR health

**157 ADRs, 11,149 lines, no index.** `docs/adr/` has 157 numbered files + `TEMPLATE.md` and nothing else — no `README.md`/index, no topic tags, no script anywhere references `docs/adr/` for validation or discovery. `docs/README.md` links the directory as a whole. **Discoverability verdict: yes, this is a real gap** — a reader must open files or grep titles; there is no map from "I'm touching X" to "which of 157 ADRs govern it" other than `doc-map.toml`'s coarse per-subsystem `docs =` lists (which mostly point at living-plan docs, not ADRs).

**Dates cluster hard: this is a ~12-day corpus, and half of it is one day.**

| Date | ADRs | | Date | ADRs |
|---|---|---|---|---|
| 2026-08-25 | 23 | | 2026-08-31 | 9 |
| **2026-08-26** | **79 (50.3%)** | | 2026-09-01 | 1 |
| 2026-08-27 | 4 | | 2026-09-02 | 8 |
| 2026-08-28 | 4 | | 2026-09-03 | 8 |
| 2026-08-29 | 4 | | 2026-09-04 | 3 |
| 2026-08-30 | 4 | | 2026-09-05 | 2 |

**Supersession:** 10 ADRs contain a `Supersedes` line (ADR-0236→0126; ADR-0234→0232 Phase 1 step 3; ADR-0169→0108; ADR-0101→... ; others). No orphaned "supersedes a file that doesn't exist" found in the sample.

**Rulings batches — the one-decision-per-file rule is violated by exactly 3 files, but hard:**

| ADR | Decisions bundled | Evidence |
|---|---|---|
| ADR-0227 "owner rulings the prose pass surfaced" | 12 (`### 1.`–`### 12.`) | each numbered decision cites its own issue/PR |
| ADR-0228 "rulings carried out of refactor-tuning on its retirement" | 12 | same shape |
| ADR-0231 "four rulings that lived only in code comments" | 5 (title undercounts) | |

29 decisions in 3 files (of 157) — i.e., the true decision count is ~183, and 15% of it is invisible to anyone who greps ADR *filenames* for a topic (ADR-0227's decision #9 on "driver protection... two invariants" isn't findable by a title search — you have to already know to open ADR-0227).

**Spot-checked 8 named ADRs against current code:**

| ADR | Claim checked | Verdict |
|---|---|---|
| 0018 (bass extension parked) | zero production callers of `apply_bass_extension` et al.; unshipped surfaces absent; enforcing test exists | **Accurate**, except the test-name citation bug above and the dead `REFACTOR-TUNING-2026-08.md` link |
| 0101 (proven-once, disclose-on-change) | foundational; paraphrased verbatim in AGENTS.md; cited as still-governing by ADR-0169 (dated 3 weeks later) | **Accurate by cross-citation** (not independently re-derived from the commissioning code paths — budget) |
| 0198 (unwired engine verb half deleted) | `TuningSession` keeps only `open`/`measure`/`close`; `analyze`/`recommend`/`save` gone | **Accurate** — confirmed via `jasper/active_speaker/crossover_v2/session.py` |
| 0226 (constrained-hardware doctrine) | push-don't-pull, no spawns in hot/restart paths | Consistent with AGENTS.md's own paraphrase; not independently re-audited unit-by-unit |
| 0233 (one reader per fact) | rule 5: `jasper-deploy-health` "retires once a registry-selected `--core` subset of the doctor" ships | **Not yet executed** — `deploy/bin/jasper-deploy-health` (900 lines) still stands alone; no `--core` flag exists in `jasper/cli/doctor/`. ADR is 2 days old — not a contradiction yet, but the exact commitment to track. |
| 0234 (detected hardware used automatically) | `hat_products` registry field drives `reconcile_boot_config` | **Accurate** — both exist and match |
| 0235 (attached-hardware one-owner-per-fact) | not independently verified this pass | — |
| 0236 (independent subwoofers deleted) | `jasper/bass_management.py`, the `/rooms/` add-subwoofer flow, `JASPER_GROUPING_CROSSOVER_HZ`/`_MAINS_HIGHPASS`/`_SUBWOOFER_PRESENT`, outputd's `Lr4LowPass`/`Lr4HighPass` all gone | **Fully accurate** — every deletion it claims is verified gone from the tree |

**Duplicated decisions:** none found in the sample beyond the tracked supersessions above; not exhaustively checked across all 157 (would need semantic clustering, out of budget — flagged in Coverage).

## 5. Debt markers in code

**Traditional markers are essentially absent.** `grep -rE '#\s*(TODO|FIXME|XXX|HACK)\b'` across `jasper/ rust/ c/ deploy/ scripts/`: **zero hits.** Every naive `TODO|FIXME|XXX|HACK|DEPRECATED` match (19 total) is a false positive — `mktemp ....XXXXXX` templates, or the literal word "TODO" inside prose describing a *different* project's convention. **Earns-its-keep**: the "fix forward, no parking-lot comments" discipline holds completely at the marker-word level.

**"for now"/"temporary"/"legacy"/"compat"/"backward"**: 371-off combined; not narration debt — nearly every sampled instance carries an explicit removal condition, consistent with AGENTS.md's "every new guard ships with a removal condition" rule:

| File:line | Shim | Removal condition | Has it happened? |
|---|---|---|---|
| `jasper/fanin/coupling_reconcile.py:786` | `before = _assistant_width_token(...)` | "REMOVE once no box carries a refused coupling token" | No — condition can't be checked from the repo (device-state dependent); correctly left in |
| `jasper/cli/doctor/_shared.py:375` | snd-aloop blind-spot doc | "Delete once no renderer lane can use snd-aloop any more (#2285)" | No — issue open |
| `jasper/cues/registry.py:42` | `fallback` field on `CueDef` | "Remove once cues are baked by a local TTS that needs no provider" | No |
| `jasper/install_profile.py:72` | `_LEGACY_STREAMBOX_ALIASES = {"endpoint","satellite"}` | migrates old install-tier tokens → `streambox` | **This IS the brief's named endpoint/satellite→streambox migration.** Shim added 2026-09-03 (2 days old) — too new to remove, no fleet-migration evidence available from the repo |
| `deploy/lib/install/env-migrations.sh` | 4 one-shot `migrate_*`/`remove_retired_*` functions (down from DEEP-AUDIT's claimed ~11) | "once the owner confirms every live Pi has deployed since" | **Partially already executed** — file was cut from ~11 relocation loops to 4 as of 2026-09-03, matching the audit's own recommendation |
| `jasper/fanin_coupling.py:36-37` | `_VALID_COUPLINGS = VALID_COUPLINGS` | none stated | **Trivially removable**: the module's own 2 internal call sites (`:675,691`) still use the private `_VALID_COUPLINGS` name instead of the public `VALID_COUPLINGS` it was aliased *from* — no external caller uses the private name. 2-line fix: point the internal call sites at the public name, delete the alias line. |

Grep-only (not individually re-verified): `jasper/peering/config.py:238` `_default_room` alias (has one internal caller, not a pure external shim); `jasper/web/tools_setup.py:434` old `/tool/<name>` link fallback (not checked whether any live UI still emits that link shape); `jasper/config.py:652-654` `JASPER_TTS_DEVICE` self-labeled "legacy... retained for pre-outputd archaeology" (a good candidate for deletion, not verified against callers this pass).

## 6. Comment prose audit

Tokenizer-based (Python `tokenize`+`ast`) count over all 788 tracked `jasper/*.py` files (424,462 lines total — matches the brief's "~426k LOC" figure).

| Package | Total lines | Code lines | Prose lines (comment+docstring) | Ratio |
|---|---|---|---|---|
| `active_speaker/` | 140,932 | 94,220 | 32,587 | 0.35 (largest **absolute** prose mass in the repo) |
| (jasper root `*.py`) | 58,273 | 35,960 | 15,857 | 0.44 |
| `web/` | 45,183 | 29,964 | 10,619 | 0.35 |
| `cli/` | 46,194 | 32,188 | 9,188 | 0.29 |
| `audio_measurement/` | 25,427 | 16,677 | 5,917 | 0.36 |
| `voice/` | 9,269 | 4,941 | 3,369 | 0.68 |
| `tools/` | 5,599 | 2,727 | 2,160 | 0.79 |
| `fanin/` | 4,719 | 2,356 | 1,822 | 0.77 |
| `transit/` | 1,665 | 747 | 675 | 0.90 |
| `aec_engines/` | 369 | 152 | 155 | 1.02 (more prose than code, but tiny — 369 lines total) |

**Top files by comment:code ratio** (min 40 lines; full 200-row table in scratch `comment_file_ratios.tsv`), headline examples spot-checked for whether the prose is load-bearing WHY-doc or narration:

`jasper/voice/input_presence.py` (ratio 25.0, 3 code lines / 75 prose lines) — read in full: the 55-line module docstring explains a genuinely non-derivable cross-daemon contract (fail-open polarity, OR-of-two-facts gating, why `ConditionPathExists` can't express an AND). **This is the good kind of high ratio** — cite it as the house standard, not a violation, despite the extreme number. Other top-25 files (`_oom_adj.py`, `multiroom/dac_content_ring.py`, `multiroom/grouping_ring.py`, `wake_conditions.py`, `multiroom/__init__.py`, `openwakeword_guard.py`) were not each individually read this pass — flagged for spot-check, not confirmed narration.

**Narration signal (dates/PR-numbers/reviewer-address):** raw counts are large — 1,301 `#NNN`-shaped PR-number mentions and 268 `20XX-` dates across `jasper/*.py` (623 and 133 respectively in comments alone, excluding docstrings) — **but this overcounts real debt.** `docs/DEEP-AUDIT-2026-08-25.md` already adjudicated this exact question ("prose-density#4 refuted: dated citations are the owner's documented house style... most dense prose is genuine WHY-constraint documentation") and this session's own sampling agrees: `#1738`, `#2087`-style citations next to a ruling are evidence pointers, not archaeology.

The **higher-precision signal** is "previously"/"used to" phrasing, which reads as true changelog-in-comments regardless of citation style. Only ~35 instances total, concentrated in 9 files:

| File | Count | Example (verbatim) |
|---|---|---|
| `jasper/web/correction_crossover_v2.py` | 5 | — |
| `jasper/active_speaker/crossover_envelope_v2.py` | 5 | `# CLOUD_MEASURE, no VERIFY — used to fall straight through to PHASE_DONE`; `A zero-length pair used to yield {...}` |
| `jasper/web/correction_setup.py` | 4 | `# A second copy of the browser-audio refusal used to sit here, re-reading...`; `#1854). It used to reach a SECOND lifetime — the auto-apply worker thread` |
| `jasper/active_speaker/baseline_profile.py` | 4 | — |
| `jasper/voice_daemon.py` | 3 | — |
| `jasper/active_speaker/playback.py` | 3 | — |
| `jasper/config.py` | 1 (self-labeled) | `# JASPER_TTS_DEVICE: legacy PortAudio device name retained for pre-outputd archaeology` |

This is a small, bounded, mechanical cleanup (~35 lines), not a systemic problem. Issues **#4106/#4113** (named in the brief as in-flight) were not independently located in open-PR form from this checkout — noted for the owner to confirm they cover this exact set before re-litigating.

## 7. Module docstring drift (40 largest modules, spot-checked 39)

Extracted the module docstring's first line + top-level `def`/`class` names for the 39 largest `jasper/*.py` files (4,177–7,165 lines each; full list in scratch). **Zero contradictions found** — every docstring's claimed one-line responsibility matched its defs (e.g. `jasper/active_speaker/crossover_v2/verification.py`: "Four verification verdicts, four adoption axes" → `Verdict`, `evaluate_capture_validity`, `evaluate_realization`, `evaluate_benefit`, `evaluate_region_benefit`, `evaluate_spec` — matches exactly).

**Two of the largest, most load-bearing files have *no* module docstring at all:**
- `jasper/voice_daemon.py` (5,476 lines — AGENTS.md's own Map entry: "the wake→LLM loop")
- `jasper/audio_io.py` (1,916 lines)

Both start directly with the SPDX header and `from __future__ import annotations` — no docstring statement precedes the first import, so `ast.get_docstring` returns nothing (verified, not a parser artifact). Every other file in the top-40 sample has one. **Should-fix**: these are two of the highest-traffic files in the repo and the two with nothing to orient a reader.

Not independently re-derived: file #40 by size and any file below the top-39 sampled.

## 8. Top-level repo files

All 7 last git-touched 2026-09-03 (2 days before this audit). Spot-checked ≥2 claims per file (5 on the larger/denser ones), all against live code:

| File | Claims checked | Result |
|---|---|---|
| `CHANGELOG.md` | ESP32 dial/satellite retirement has matching cleanup code (`retire_esp32_accessory_python_packages` in `deploy/lib/install/python-runtime.sh`); Gemini/OpenAI/Grok session files exist; `jasper-fanin`/`jasper-outputd`/`jasper-mux` real; `volume_limit` hard-clamped in `camilla_config_contract.py:438-457`; `scripts/check-provenance.py` exists | **5/5 accurate** |
| `PRIVACY.md` | `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES` default 128 MiB (`jasper/wake_events.py:82`); `JASPER_USAGE_DB`, `JASPER_CONVERSATION_CAPTURE`, `mic_mute.env` all real | **5/5 accurate** — and the 128 MiB figure confirms `DEEP-AUDIT-2026-08-25.md`'s "drop from 1 GiB to ~128 MiB" recommendation was actually implemented |
| `SECURITY.md` | `jasper-control` binds `0.0.0.0:8780` by default (`jasper/control/server.py:2184`); nginx proxies `127.0.0.1:8780`; `management_read_allowed`/`mutating_request_allowed` exist in `http_security.py` | **3/3 accurate** |
| `PLAN.md` (41 lines) | Cites ADR-0198 and the tuning-operator-runbook as current | **Accurate** (ADR-0198 independently confirmed in §4) |
| `QUICKSTART.md` | `.claude/commands/onboard-pi.md` exists; `JASPER_INSTALL_PROFILE` real; streambox = Pi Zero 2 W (matches ADR-0226's named target) | **3/3 accurate** |
| `BRINGUP.md` (1,222 lines) | `/etc/jasper/jasper.env` still the real operator-editable file (referenced in `jasper/config.py`, `deploy/install.sh`) | **1/1 spot-check accurate**; not read end-to-end (budget) |
| `CODE_OF_CONDUCT.md` | Contributor Covenant boilerplate; contact `jaspercurry@gmail.com` (differs from `SECURITY.md`'s `jc@jasper.tech`, plausibly intentional — conduct vs. security are different inboxes) | No contradiction found |

**Verdict: the top-level docs are in good shape.** No stale claims found in any spot-check.

## 9. Ranked top-15 simplifications

| # | Finding | Sev | LOC removable | Fix |
|---|---|---|---|---|
| 1 | `docs/REFACTOR-CUTOVER-2026-08.md` — VERIFIED-COMPLETE, its god file deleted, never retired like sibling chunk 1 | Should-fix | 1,694 (doc) | Fold surviving rulings into an ADR (as ADR-0228 did for chunk 1), delete the file |
| 2 | `docs/ux-audit-2026-09-03/PROMPT-subwoofer-deletion.md` — fully landed `[x] P.2`, duplicates ADR-0236 | Should-fix | 201 (doc) | Delete |
| 3 | ADR-0227/0228/0231 bundle 29 decisions into 3 files, violating one-decision-per-file | Should-fix | 0 removable (append-only) | Not retroactively fixable; adopt a hard per-PR ADR-count check going forward |
| 4 | 157 ADRs, zero index | Should-fix | 0 removable | Add `docs/adr/README.md` — generated table of `{number, title, date, supersedes}`, cheap to keep current with a script |
| 5 | 6 dead `HANDOFF-*.md`/`CHIP-AEC-EXPERIMENT.md` links in currently-maintained (non-append-only, non-historical) docs: `AEC-DIAG-06` (×4), `RESEARCH-pipewire-low-latency.md`, `DEEP-AUDIT-2026-08-25.md` (×2, lower priority) | Should-fix | ~7 lines | Point at whatever doc now carries the fact, or annotate `(deleted; see git history)` as `limiter-bench-runner-implementation.md` already correctly does |
| 6 | `docs/testing-tooling.md` cites a moved test function and two nonexistent example test filenames | Nit | 3 lines | Update citations |
| 7 | `docs/adr/0018` cites a renamed test function | Nit | 1 line (unfixable — append-only) | Note the rename in a follow-up ADR if it matters, else accept |
| 8 | `jasper/voice_daemon.py` and `jasper/audio_io.py` — top-40-largest files, zero module docstring | Should-fix | +~10-20 (net addition, not removal) | Add a one-paragraph responsibility docstring to both, matching the other 38 sampled files' convention |
| 9 | ~35 "used to"/"previously" narration comments concentrated in 9 files (`correction_crossover_v2.py`, `crossover_envelope_v2.py`, `correction_setup.py`, `baseline_profile.py`, `voice_daemon.py`, `playback.py`, `config.py`) | Nit | ~35 | Strip changelog-in-comments; the fact being narrated is already in git history |
| 10 | `jasper/fanin_coupling.py` `_VALID_COUPLINGS` alias with zero external callers | Nit | 2 | Point its 2 internal call sites at `VALID_COUPLINGS`, delete the alias |
| 11 | `docs/audit-pending-followups.md` self-declared "Last verified: 2026-07-11" — 56 days stale | Should-fix | 0 (verify, don't delete) | Re-derive at HEAD or mark resolved sections; still routed from `doc-map.toml` as current |
| 12 | `docs/ux-audit-2026-09-03/PROMPT-balance-sync.md` Part 1 (fully landed) kept alongside still-open Part 2 | Nit | ~30 (Part 1's now-dead file/line inventory) | Collapse Part 1 to "done, see PR history"; keep Part 2 |
| 13 | `docs/bass-extension-waves/` and `docs/ux-audit-2026-09-03/` invisible to the CI orphan-sweep test (recursive glob gap) | Should-fix | 0 (test hardening, not doc deletion) | Widen `test_root_and_top_level_docs_are_intentionally_mapped`'s glob to recurse, or add explicit `document_classes` entries like `historical/` has |
| 14 | `jasper/config.py:652-654` self-labeled "legacy... kept for pre-outputd archaeology" comment on `JASPER_TTS_DEVICE` | Nit | ~3 | Delete comment once confirmed zero real callers of the sounddevice transport (not verified this pass) |
| 15 | `deploy/lib/install/env-migrations.sh`'s 4 remaining one-shot migrations (`migrate_voice_keys_split`, `migrate_google_routes_key`, `migrate_wifi_guardian`, `remove_retired_audio_topology_state`) | Earns-its-keep (not a finding — listed to close the loop) | 0 | All dated 2026-09-03 (2 days old); too new to remove; already reduced from DEEP-AUDIT's ~11 |

## Coverage

**Opened and read in full or near-full:** `AGENTS.md`, `docs/README.md`, `docs/doc-map.toml`, `docs/UX-AUDIT-2026-09-03.md`, `docs/ux-audit-2026-09-03/PROMPT-subwoofer-deletion.md` + `PROMPT-balance-sync.md`, `docs/REFACTOR-CUTOVER-2026-08.md` (head + targeted sections), `docs/audit-pending-followups.md` (head/tail), `docs/DEEP-AUDIT-2026-08-25.md` (full), `docs/adr/{0018,0101,0146,0115,0169,0198,0226,0227,0228,0231,0233,0234,0236}` (full), `docs/AEC-DIAG-06-xvf-format-level-profile.md` (head), `docs/RESEARCH-pipewire-low-latency.md` (excerpt), `CHANGELOG.md`, `PRIVACY.md` (full), `PLAN.md` (full), `SECURITY.md` (excerpt), `QUICKSTART.md`/`BRINGUP.md`/`CODE_OF_CONDUCT.md` (excerpts), `scripts/docs-impact.py` (full), `tests/test_docs_impact.py` (relevant tests).

**Scripted/automated across the full tree:** docs inventory (LOC + git-log last-touch + README/doc-map membership) for all 295 docs files; markdown-link and backtick-path extraction across `docs/**` + top-level docs + `.claude/**` (7,515 raw candidates, narrowed and hand-verified as described in §2); `TODO`/`FIXME`/`XXX`/`HACK`/`DEPRECATED` grep across `jasper/ rust/ c/ deploy/ scripts/` (exhaustive, zero real hits); `for now`/`temporary`/`legacy`/`compat`/`backward` grep (exhaustive counts, ~15 instances individually read); comment/docstring-vs-code line counts via Python `tokenize`+`ast` over all 788 tracked `jasper/*.py` files; narration-pattern (date/PR/reviewer/previously/used-to) counts over the same set; 39-of-40 largest-module docstring-vs-defs comparison.

**Not done / explicitly out of budget:**
- Rust (`rust/**/*.rs`) and C (`c/**/*.c`, `*.h`) comment-ratio analysis (§6 numbers are Python-only; debt-marker grep in §5 did cover rust/c, with zero hits).
- Exhaustive re-derivation of all 157 ADRs against code — 8 named ADRs spot-checked (per the brief's list) plus 0146/0115/0169/0227/0228/0231 opened for the rulings-batch and dead-link findings; the remaining ~145 were not opened.
- Semantic duplicate-decision clustering across all 157 ADRs (would need pairwise topic comparison — only the already-tracked supersessions were confirmed; no independent duplicate-finding pass was run).
- `docs/gating-v2-plan.md`, `room-correction-regime-plan.md`, `tuning-master-plan.md`, `research-tool-plan.md`, `tool-platform-plan.md`, `conversation-history-plan.md`, `PROPOSAL-dac-profile-registry.md` — status-line read only, not re-derived line-by-line against current code (7 files, ~3,200 lines total).
- `docs/adr/0235` — listed in the brief's spot-check set but not opened this pass (time budget; 0018/0101/0198/0226/0233/0234/0236 plus the 3 rulings-batch ADRs and 3 dead-link ADRs were prioritized instead).
- Full end-to-end read of `BRINGUP.md` (1,222 lines) and `QUICKSTART.md` (516 lines) — spot-checked only.
- `docs/examples/tool_pack_starter.py` currency against `docs/tool-platform-plan.md`'s example — not checked.
- Top-25-by-ratio file list beyond the 2 read in full (`voice/input_presence.py` read; the rest of the top-25 in `comment_file_ratios.tsv` were not individually opened to confirm "good WHY-doc" vs. "narration" — flagged as unverified in §6, not asserted either way.
- Issues #4106/#4113 (named in the brief as in-flight prose work) — not looked up on GitHub; this session has no network access to confirm scope overlap with §6's findings.
