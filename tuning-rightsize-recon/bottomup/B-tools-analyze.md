# B — ANALYZE / RECOMMEND / SAVE tools (bottom-up sizing)

Scope: the 9 read/prescribe tools on the LLM menu, plus 6 off-menu binaries and
4 laptop scripts. **Current: 43,059 lines. First-principles clean: ~8,200.
Delta ~34,900 (81%).**

Method: for each tool I counted the CLI file plus the engine modules it is the
*primary* consumer of (a module counted once, under one tool). Excluded as
shared substrate someone else sizes: `audio_measurement/` DSP (`deconv`,
`analysis`, `calibration`, `distortion`, `interference_nulls`), `flat_spec*`,
`attribution/`, `contracts.py`, `journey.py`, and the record/bank contract.
Prose measured with an AST+tokenize counter (docstring lines + comment lines
over non-blank lines); script at `scratchpad/prose.py`.

---

## 1. On-menu tools

| Tool | What it does | Now | Clean | Top reason for the gap |
|---|---|---:|---:|---|
| `jasper-round-views` (9 views) | 9 grading/comparison views over one banked round | **3,293**<br><sub>cli 681 · cv2/round_views 1810 · round_inputs 200 · repeat_floor 195 · frequency_view 173+234</sub> | **850** | 49% prose in the engine; 12 dataclasses each with a hand-written `to_dict`; `repeat`/`repeat-floor` are one computation published twice |
| `jasper-crossover-prescriber` | packet → propose → stage → status | **12,632**<br><sub>cli 1628 · evidence_packet 3753 · 4 doors 5911 · prescription_spool 953 · door 387</sub> | **1,840** | the four-door duplication (below) + a 600-line hand-written English "what can I do next" narrator (`crossover_prescriber.py:1065-1222`) doing the LLM's job |
| `jasper-classify-features` | min-phase/gate mechanism discriminator | **3,501**<br><sub>cli 300 · feature_classifier 2599 · feature_classification 602</sub> | **1,150** | real DSP, but a 215-line per-round self-validation harness (`feature_classifier.py:1373-1586`) that is a test living in production; `feature_classification.py` is 61.7% prose |
| `jasper-gate-sweep` | window-ladder room-vs-speaker sweep | **1,746**<br><sub>cli 223 · gate_sweep 1140 · round_captures 383</sub> | **540** | `round_captures.py` is a *second* banked-round reader beside `round_views.load_banked_round`; the CLI is ~15 real lines wrapped in 208 of prose/argparse |
| `jasper-read-distortion` | H2/H3 vs fundamental from banked MEASURE captures | **1,617**<br><sub>cli 353 · harmonic_evidence 1264</sub> | **490** | the DSP is already in `audio_measurement/distortion.py`; this module is program-rebuild + scoping + report shaping at 44.9% prose |
| `jasper-delay-sweep propose` | inter-driver delay from banked curves | **964**<br><sub>cli 286 · delay_landscape 603 · as/delay_sweep 75</sub> | **340** | third round reader (`position_cycle.read_pose_curve_pair`); 36% prose |
| `jasper-close-reference` | correct a close capture to far distance | **953**<br><sub>cli 280 · cv2/close_reference 673</sub> | **420** | closest to right-sized of the set; gap is prose + CLI re-roll only |
| `jasper-forward-model` | predict summed response from per-driver solos | **732**<br><sub>cli 327 · cv2/forward_model 405</sub> | **270** | engine is 48.9% prose for ~180 lines of numpy; `verify-delta` lives in `round_views.py:1043` not here |
| `jasper-project-ring` | re-project a bank into the legacy capture-ring layout | **552**<br><sub>cli 167 · ring_projection 385</sub> | **0** | pure compatibility shim — see delta #3 |
| **subtotal** | | **25,990** | **5,900** | |

Prose in that 25,990: **6,654 docstring + 3,413 comment lines = 43.2% of
non-blank**. For comparison the older `jasper/calibration_agent/` code in the
same repo runs 0.8–13.6%. The bloat is specific to the crossover-v2 tuning
layer, not to the project.

### Is 9 + 7 separate read tools the right shape?

**No — but the fix is smaller than it looks.** These *are* views over one
banked round; the problem is that there are **five different readers** of that
round today:

| Reader | Type | Consumers |
|---|---|---|
| `round_views.load_banked_round` → `BankedRound` | `crossover_v2/round_views.py:304` | round-views, forward-model |
| `round_captures.discover_captures` → `PoseCapture` | `round_captures.py:172` | gate-sweep, close-reference |
| `feature_classifier.load_round_captures` → `RoundCapture` | `feature_classifier.py:602` | classify-features |
| `harmonic_evidence._bind_measure_captures` | `harmonic_evidence.py:617` | read-distortion |
| `position_cycle.read_pose_curve_pair` | `position_cycle.py:320` | delay-sweep |

Plus `ring_projection.py` (385 lines) whose only job is to make readers 3 and 4
accept a modern bank. The owner's doctrine ("analyze defaults to everything the
bank supports; depth is pulled one verb per question") is satisfied by **one
`jasper-round-analyze` binary with ~16 verbs over one `BankedRound`**:

- one reader (`BankedRound` incl. curves, captures, poses, programs): **~250**
- one CLI harness (subcommand registry, shared failure record, `--out`
  defaulting, JSON writer): **~150** (a good ~130 of this already exists in
  `cli/_refusal.py` + `cli/_report.py` and is simply not consumed by everyone)
- 16 verbs × ~40 lines of arg-binding + summary line: **~640**
- the analysis engines behind them: **~4,860**

= **~5,900**, versus 25,990. Note this is *fewer* files, not more abstraction:
9 CLI files collapse to 1, five readers to one, and `docs/tuning-operator-runbook.md`'s
entire "Exit codes" section (lines 672–728) — which exists only to reconcile
per-tool re-rolls — collapses to two sentences.

---

## 2. Off-menu units — verdict for each

| Unit | Now | Verdict | Clean | Why |
|---|---:|---|---:|---|
| `jasper-active-speaker-attempts-replay`<br><sub>cli 1079</sub> | 1,079 | **ghost** | 0 | A one-off 2026-07 study replay hard-wired to two banks that are not in the repo (`captures/repeat-floor-20260731/`, `captures/r11-loop-proof-corpus/` — `active_speaker_attempts_replay.py:82`), including a `render_readme()` (`:833`). Its assertions already exist as `tests/test_active_speaker_attempts_replay.py`. Engine `attempts_loop.py` (923) is live product and stays. |
| `jasper-active-speaker-emit-bench` + `active_speaker/bench/`<br><sub>cli 285 · bench 2266</sub> | 2,551 | **operator/bench tool** — never an LLM tool (must run on the speaker; resolves the CamillaDSP binary from the running unit) | 600 | It is a regression harness for the emitter. `bench/compare.py` is 55.6% prose; `bench/__init__.py` is 63 lines of which 57 are docstring. |
| `jasper-bass-extension-bench` + `bass_extension/bench/`<br><sub>cli 194 · bench 5683</sub> | 5,877 | **operator/bench tool, and out of the tuning house** — `bass_extension/` is its own program; only `bass_extension/profile.py` reaches the tuning path (`baseline_profile.py:27`) | 1,200 | 15 modules for one supervised bench run (`executor.py` 1201, `render.py` 822, `derivation.py` 732). A limiter-evidence bench is a manifest + a runner + a bundle writer. |
| `jasper-correction-bundle`<br><sub>cli 273 · bundle_tools 521 · fir_runtime 301</sub> | 1,095 | **operator tool** — room-correction domain, not crossover tuning | 400 | Lowest-prose unit in the set (14.9%); gap is scope, not bloat. Should not appear on the tuning menu. |
| `jasper-calibration-agent` + client stack<br><sub>cli 523 · response 964 · model_client 503 · actions 450 · tools 340 · proposal_sim 283 · prompt 82 · sound_actions 93</sub> | 3,238 | **ghost for this area — owner decision** | 0 | This is an **in-product LLM** (its own API client, prompt, and 964-line response contract) advising on *room* correction, wired to `web/correction_tuning.py:298` and `web/correction_setup.py:5608`. It is the architecture the "SSH in and let Claude drive discrete tools" model replaces. Not named once in the runbook, the methodology, or the doctrine. `advisor_context.py` (483) + `correction_advisor.py` (702) are the evidence assembly and belong to the room wizard either way. |
| `scripts/run-crossover-round.py` | 1,452 | **collapse** | 60 | The runbook itself says it is "the same three wizard verbs" as `jasper-round` (`tuning-operator-runbook.md:469`) over "the same transport and the same apply gate". Under the owner's model the agent is already on the box. Keep an ssh convenience wrapper. |
| `scripts/render-metric-views.py` | 459 | **ghost** | 0 | Superseded by `jasper-round-views per-seat` / `frequency`; its own docstring concedes the duplicated half already moved into product code. Its two surviving columns (`gap`, `ratio`) are a `--lab` flag. |
| `scripts/harmonic-distortion-replay.py` | 701 | **ghost (verbatim duplicate)** | 0 | See delta #2. |
| `scripts/severed-twin-replay.py` | 617 | **ghost** | 0 | A "corpus-replay rung of the validation ladder" — a passed proof. AGENTS.md: a passed validation proof stays valid; it does not need a permanent 617-line runner. Belongs in `tests/` or git history. |
| **subtotal** | **17,069** | | **2,260** | |

---

## 3. Bottom-up total and breakdown

| | Lines |
|---|---:|
| **Current (this area)** | **43,059** |
| **Clean 80/20** | **~8,200** |
| **Removable** | **~34,900** |

Where the 34,900 goes:

| Bucket | Lines | Notes |
|---|---:|---|
| Prose over the AGENTS.md bar | ~11,500 | 14,151 prose lines today across the area (34.4% of non-blank; 43.2% in the on-menu engines). A clean impl at ~15% keeps ~1,200. |
| Duplication | ~9,000 | four doors (delta #1), two harmonic readers (delta #2), five banked-round readers, three laptop-script twins of shipped tools |
| Dead / one-off study code / superseded shims | ~5,500 | attempts-replay, severed-twin, render-metric-views, project-ring, the `--dumps` ring layout |
| Over-abstraction | ~4,900 | 9 CLIs re-rolling argparse + exit codes + reason slugs + `--out` defaulting; 12 dataclasses × hand-written `to_dict`; `prescription_spool` + `door` ceremony; the 600-line `status` narrator |
| Speculative-or-parked | ~4,000 | bench harnesses beyond an 80/20 regression check (bass-extension bench, emit bench), the in-product LLM client stack |

Confidence: the on-menu 25,990 → 5,900 I hold at ±20%. The off-menu 17,069 →
2,260 carries the calibration-agent judgment call (3,238 lines) and the
bass-extension bench scope call (4,677 lines) — both are owner decisions, not
mechanical findings. Excluding both, the area still lands at ~16,000 clean-able.

---

## 4. The three biggest single deltas

### #1 — The four prescription doors: 7,251 → ~740 (−6,500)

Four modules implement one concept ("read a JSON prescription document, check
its bounds, refuse by name") four times. Verbatim-or-near duplicate helpers:

| Helper | alignment | topology | blend | driver |
|---|---|---|---|---|
| `_finite_number` | `:352` | `:427` | `:938` | `:1055` |
| `_parse_prescription` | `:374` | `:521` | `:1266` | `:1811` |
| `_optional_number` | `:494` | `:507` | — | — |
| `_read_artifacts` | `:660` | `:474` | — | — |
| `read_*_prescription` | `:508` | `:632` | `:1332` | `:1880` |
| `*_from_mapping` | `:615` | `:792` | `:1548` | `:2169` |
| `*Refused(ValueError)` | `:246` | `:302` | `:502` | — |
| `*_response_format()` | `:693` | `:903` | `:657` | `:2253` |

The `*_response_format()` functions are the worst of it: four **hand-written
English JSON "schemas"** (e.g. `alignment_prescription.py:693-751`,
`topology_prescription.py:903-964`) that are prose describing a shape the
dataclass beside them already declares, published into the evidence packet at
`evidence_packet.py:3529-3534`. A generated schema off the door dataclass +
its refusal frozenset replaces all four. Prose in these four files runs
51.5–60.0%.

Clean shape: one `door.py` with a generic reader/validator/emitter (~200) +
four schema-and-bound declarations (~90 each). `prescription_spool.py` (953,
**64.6% prose**, 240 comment lines) is a stage/take/consume file — ~180.

### #2 — `scripts/harmonic-distortion-replay.py` is `harmonic_evidence.py` (−701)

Line-for-line, function-for-function:

| Script | Engine |
|---|---|
| `sign_convention` `:161` | `_sign_convention` `harmonic_evidence.py:766` (identical body) |
| `rebuild_program` `:182` | `rebuild_measure_program` `:378` |
| `bind_captures` `:253` | `_bind_measure_captures` `:617` |
| `fidelity_failures` `:281` | `_fidelity_failures` `:817` |
| `glitch_disclosure` `:306` | `_glitch_disclosure` `:844` |
| `read_capture` `:323` | `_read_one_capture` `:865` |
| `load_mono` `:138` | `_read_mono` `:266` |
| `report` `:408` | `_role_block` `:978` + `read_round_harmonics` `:1091` |

`jasper-read-distortion` is the shipped answer. AGENTS.md: "Two implementations
of one concern in reach: converge them or open an issue — never add a third."

### #3 — `jasper-project-ring` exists only to feed a layout nothing writes (−552, and it unblocks more)

`ring_projection.py:1-21` states the whole case against itself: the ring
producer "died with the retention seam (#3250)", and "**nothing was actually
lost**" — every wired round banks the WAV plus a record carrying every field
both readers consume. So this module re-projects data that is already on disk
into a legacy layout so that `feature_classifier.load_round_captures`
(`:602`) and `harmonic_evidence` can find it via `RING_SIDECAR_GLOB`.

The 80/20 move is to teach the one banked-round reader to open a bundle
directly (~40 lines), then delete `ring_projection.py` (385), `cli/project_ring.py`
(167), the `--dumps` flag on two CLIs, and the paragraph in
`docs/tuning-methodology.md:511-513` that makes the LLM run an extra tool
before it can classify anything. It also removes the only tool the methodology
*requires* that never made it onto the generated menu
(`scripts/generate-tuning-tool-menu.py:54` `TUNING_TOOL_MODULES`).

---

## 5. Smaller findings worth carrying

- **The shared exit-code contract is not shared.** `cli/_refusal.py:26` sets
  `EXIT_REFUSED = 1`; `cli/crossover_prescriber.py:136-139` inverts it
  (`EXIT_EVIDENCE_UNREADABLE = 1`, `EXIT_REFUSED = 2`). Three tools
  (`read_distortion`, `project_ring`, `classify_features`) still print an older
  `{"ok": false, …}` shape through `fail_with_payload` (`_refusal.py:64`), whose
  own docstring calls converging it "a follow-on". The cost is
  `docs/tuning-operator-runbook.md:672-728` — a 57-line doc section, plus
  "Three traps worth knowing before you branch on a number".
- **`refusal_copy.py` (1,578 lines, 62.5% prose, 618 comment lines)** — its
  module docstring (`:1-60`) is pure ruling history: "*Phase 5c-ii moved it
  anyway… **That argument did not survive.***" It is a 43-code registry of
  household sentences; ~200 lines of data. (Counted under the flow, not this
  area, but it is the same disease.)
- **41 reason-slug constants and 55 error classes** across `crossover_v2/` +
  `cli/` for what `_refusal.py` already models as three stages.
- **`crossover_prescriber status`'s next-actions narrator** (`:1065-1222`,
  158 lines of hand-composed English, plus `_status_sections`/`_banked_section`/
  `_declared_section`/`_applied_section` feeding it, ~600 total) writes prose
  for a reader that is an LLM. The packet already carries `not_evaluated`
  (`evidence_packet.py:3509`) with the same absence reasons as structured data.
