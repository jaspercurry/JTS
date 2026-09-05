# Lane: walk-methodology

HEAD = `f4ff89731`. Read-only. Every `file:line` below was opened at that HEAD.
Persona: the driving LLM at an SSH prompt on the box, human beside the speaker,
with `docs/tuning-methodology.md` + the runbook's generated tool menu
(`docs/tuning-operator-runbook.md:419-503`) + `docs/measurement-loop-doctrine.md:17-75`
as its only map.

---

## Headline

The methodology is 872 lines of *decision rule* with almost no *verb*. Counted at HEAD:

| Binary | times named in tuning-methodology.md |
|---|---|
| `jasper-round-views` | 16 |
| `jasper-declare-geometry` | 4 |
| `jasper-angle-capture` | 3 |
| `jasper-crossover-prescriber` | 1 (`status`, once) |
| `jasper-seat-level` / `jasper-null` / `jasper-audition` | 1 each |
| **`jasper-round`** (open/wait/apply/bank) | **0** |
| **`jasper-measure`** | **0** |
| **`jasper-basic-profile`** | **0** |
| **`scripts/run-crossover-round.py`** | **0** |
| **`scripts/bank-crossover-round.sh`** | **0** |
| `jasper-doctor` | 0 |

Verified: `grep -n "jasper-round" docs/tuning-methodology.md | grep -v jasper-round-views` returns
nothing. The doc's only references to those tools are two unnamed noun phrases —
"the round runner" (`docs/tuning-methodology.md:441`) and "the round CLI"
(`docs/tuning-methodology.md:647`) — and they are two *different* tools, one of which
is misidentified (see D-1).

So a driver that reads the methodology end to end, as its own header instructs
(`docs/tuning-methodology.md:17`), never learns the name of the verb that **measures**,
the verb that **applies**, or the verb that **banks**. Those three are in
doctrine §1 (`docs/measurement-loop-doctrine.md:31,44`) and the generated menu row
(`docs/tuning-operator-runbook.md:433`) — but doctrine §1's five-step loop
(`measure → analyze → recommend → loop → save`) names **no apply verb at all**:
lines 26-44 list `jasper-round open`/`wait`, `prescriber packet`, `prescriber propose`,
`round-views frozen`, `jasper-round bank`. `jasper-round apply` appears nowhere in the loop.

Counterweight: the generated menu is **current** — `PYTHONPATH=. venv/bin/python
scripts/generate-tuning-tool-menu.py --check` exits 0 at HEAD. Every row in it resolves
to a real subparser. The gap is the methodology's pointers, not the menu.

---

## A. Step table

Column key. **Verb**: exact binary + subcommand the methodology names, or NONE.
**At HEAD**: module + `file:line` of the subparser. **Reads back**: what lands on
the LLM's terminal. **WANTING**: script / guess / wall-of-curves / out-of-band.
`⛔` = gap. `✅` = by-design NONE, doc says why.

### §0 DECLARE

| Step | Verb the doc names | Exists at HEAD | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §0 declare rig geometry (`:64-72`) | `jasper-declare-geometry set --speaker-height-in --mic-height-in --distance-in` | yes — `jasper/cli/declare_geometry.py:81` | `/var/lib/jasper/measurement_geometry.json` (`declare_geometry.py` default) | text, ~4 lines + derived | none |
| §0 read it back (`:75-79`) | `jasper-declare-geometry show` | yes — `declare_geometry.py:92` | — | ~8 lines text + provenance line (`declare_geometry.py:145-154`); exit 2 for both "nothing declared" and "unreadable" (`:140`, `:143`) | none — the doc's exit-2 statement is exactly right |
| §0 read the six declared quantities (`:52-62`: sensitivity, protection floor, radiating diameter, driver class, excitation plan, mic tier) | **NONE** — the table points at *Python modules* (`profile.py`, `design_draft.py`, `_common.py`, `excitation_safety_plan.py`) | n/a | — | — | ⛔ **No verb prints the declared set.** `jasper-crossover-prescriber status`'s `declared` block is roles + passbands only (`jasper/cli/crossover_prescriber.py:653-676`). To read `sensitivity_db` / `radiating_diameter_mm` / driver class the LLM must `cat /var/lib/jasper/active_speaker_design_draft.json` — a path (`jasper/active_speaker/design_draft.py:50`) that appears in **none of the three docs**. |
| §0 c-t-c spacing (`:97-102`) | NONE | — | — | — | ✅ by design, doc says `driver_spacing_m` is pinned `0.0` and tells you to name your source |
| §0 waveguide coverage (`:103-106`) | NONE | — | — | — | ✅ by design (`horn_coverage_deg` retired) |
| §0 σ_repeat availability (`:107-113`) | NONE (read `accuracy_budget.components…` in the packet) | packet block exists | `packet.json` | — | ✅ disclosed field, but requires opening the packet by hand |

### §1 PROVE THE PLUMBING

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §1a seat level (`:132-147`) | `jasper-seat-level` (+ `--target-db-spl` / `--tolerance-db`) | yes — `jasper/cli/seat_level.py:613` (flat parser, no subcommands) | banks the session measurement reference | 1 human line, or `--json` (`seat_level.py:682`) | ⛔ the doc never says the tool **requires** `--calibration-file` *or* `--mic-serial` (`seat_level.py:666,670`); omit both ⇒ exit 2 usage error (`seat_level.py:642-643`). Findable from `--help`, not from the doc. |
| §1a precondition: capture control at 100% (`:149-152`) | **NONE** | — | — | — | ⛔ out-of-band: `amixer -c <card>` is only in the tool's own `description` (`seat_level.py:621`), not in either doc's prose as a command. Human must set a mixer control the toolbox never reads back. |
| §1b polarity by reverse-null (`:158-162`) | **NONE — no command in the whole section** | the capability exists: `jasper-null --polarity both` "plays the in-phase/inverted pair at delay 0 — the polarity proof" (`jasper/cli/null_door.py:817-822`), and `jasper-angle-capture … --polarity inverted --inverted-role` (`jasper/cli/angle_capture.py:718-730`) | `<bundle>/null_runs/*.json` | — | ⛔ **gap.** §1b names refusal constants (`POLARITY_KEEP`/`INVERT`) and a module, no verb. The LLM must find `--polarity both` by reading `jasper-null --help`. Two different tools can do it and the doc arbitrates neither. |
| §1c bank the repeat floor (`:167-172`) | `jasper-round-views repeat-floor <N rounds> --install` (`--out repeat-floor.json`) | yes — `jasper/cli/round_views/repeat.py:118`; `--install` `:125`, `--out` `:133` | `/var/lib/jasper/active_speaker_repeat_floor.json` (`jasper/active_speaker/repeat_floor.py:41`) | one summary line + path | ⛔ **no verb produces its input.** It wants "two or more TOUCHED-NOTHING fixed-pose repeat round directories" (`repeat.py:122-124`); nothing in any doc says how to *make* N repeat rounds on the box. Also: `repeat-floor` is **absent from `ARTIFACT_BY_VIEW`** (`jasper/cli/round_views/_common.py`), so `inventory` never reports whether a round has one. |
| §1c which σ you used (`:171-174`) | NONE (read `compute_sigma_curve` vs `positions.cross_seat_sigma…`) | — | — | — | ✅ pointed at the runbook's "Reading σ honestly" (`docs/tuning-operator-runbook.md:1204`) |

### §2 RAW DRIVERS

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §2 position with an arm (`:196`) | `jasper-angle-capture serve` | yes — `jasper/cli/angle_capture.py:946` | walk trail | loop verdict | none |
| §2 position with a human (`:197-200`) | `jasper-angle-capture stage --program baseline --size express\|full` | yes — `angle_capture.py:930`; `--program` `:640`, `--size` `:659` | spool file + `handoff_url` in the receipt (`angle_capture.py:344`) | `N stops, moved by human …` + one line per stop (`angle_capture.py:381-401`); 5 stops express, 13 full (`jasper/active_speaker/measurement_programs.py:84-108`) | ⛔ the doc never writes `--mover human` (it is the argparse default, `angle_capture.py:709`); a driver copying §2 verbatim gets human by luck, not by statement |
| §2 poll for takes (`:200-201`) | `jasper-crossover-prescriber status` … under `banked` | yes — `jasper/cli/crossover_prescriber.py:1279`; the field is `banked.walk.n_takes` (`crossover_prescriber.py:717-727`) | — | JSON with `--json` (`:1284`) | minor: the doc says "under `banked`", the field is `banked.walk` |
| §2 store geometry (`:201-203`) | `jasper-declare-geometry set` | yes | `declared-geometry.json` frozen beside the bundle | — | none |
| **§2 open the measurement session** | **NONE — the section jumps from `stage` straight to polling** | `jasper-round open --tier <tier>` (`jasper/cli/round.py:353`) then `jasper-round wait` (`:388`) | live bundle | `wait` prints `session_id` + `candidate_fingerprint` (`round.py:192-204`) | ⛔ **the largest single gap.** Nothing in §2 (or anywhere in the methodology) says a session must be opened, or names the verb that opens it. Only the runbook happy path (`docs/tuning-operator-runbook.md:145-149`) and doctrine §1 (`docs/measurement-loop-doctrine.md:31`) carry it. |
| §2 Way-1 collapse (`:210-218`) | NONE | refusal names are real (`alignment_no_crossover_region`, …) | — | — | ✅ by design |

### §3 THE CROSSOVER CORNER

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §3 declare & pin a corner (`:222-224`) | **NONE in §3** | the door is `--topology-prescription PATH` on `jasper-round open` (`jasper/cli/round.py:375-385`) or the laptop runner | session-open judgement | refusal reason | ⛔ §3 is four sub-sections about *which corner* with no statement of *how to pin one*. The LLM must find the door in the runbook menu (`docs/tuning-operator-runbook.md:456`) or the happy path (`:92-96`). |
| §3a protection floor (`:228-235`) | NONE | refusal codes real; `beaming_ceiling_hz` is a disclosed topology-door field (`jasper/active_speaker/crossover_v2/topology_prescription.py:127`) | — | — | ⛔ no verb prints `2 × protection_highpass_floor_hz` or the declared floor; hand arithmetic off a JSON the docs never locate |
| §3b beaming onset (`:243-252`) | NONE — points at `branch_chain.beaming_onset_hz` | `jasper/active_speaker/branch_chain.py:292`, `BEAMING_KA = 2.0` at `:289` | — | — | ⛔ **no verb reports the measured horizontal patterns §3b tells you to prefer.** `jasper-round-views directivity` (`jasper/cli/round_views/seats.py:162`) is exactly that instrument and §3b never names it. |
| §3c vertical lobing arithmetic (`:256-273`) | NONE | — | — | — | ⛔ pure hand arithmetic over a `d` the system does not carry (§0 says `driver_spacing_m` is pinned 0.0). The LLM computes `θ_null` in its head from operator prose. |
| §3d vertical polar decides (`:277-288`) | **NONE** | elevation poses exist in `baseline/full` (±10/±20) and `baseline/express` (±10) — `measurement_programs.py:94-97,106-107` | — | — | ⛔ no verb reports "vertical polar data"; the doc's own escape ("no vertical data ⇒ say so in the receipt") is honest but leaves the decision unanswerable by any tool |

### §4 TIME ALIGNMENT

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §4.1 read `phase_composition` (`:334-336`) | `jasper-round-views delay-landscape` echoes it | yes — `jasper/cli/round_views/delay.py:219` | `delay_landscape.json` (`_common.py` `ARTIFACT_BY_VIEW`) | summary + path | none |
| §4.2 propose from banked evidence (`:341-344`, `:423-425`) | `jasper-round-views delay-landscape <bundle> --fc-hz N` | yes — `delay.py:219`, `_add_landscape_arguments` | `delay_landscape.json`; **prints ready-to-run `jasper-angle-capture stage --delayed-role R --delay-us N` lines** (`jasper/cli/round_views/delay.py:20-21`) | small; plays nothing | none — this is the toolbox at its best (a verb that hands you the next command) |
| §4.3 dispose acoustically (`:346-354`, `:408-410`) | `jasper-null --polarity keep --delays <grid>` | yes — `jasper/cli/null_door.py:806-822`; `--delays` is comma-separated µs, `--delays=-200,…` form required for a leading `-` (`:810-816`) | `<bundle>/null_runs/<row>.json` | one stderr line per coordinate (`null_door.py:614`) **plus the full row list as JSON on stdout** (`:710`) — ~20 fields × N coordinates | ⚠ context: the only measured verb that dumps every row to stdout instead of summary+path (contrast `sweeps.py:99-104`) |
| §4 grade the confirm (`:426-428`) | `jasper-round-views delay-confirm <bundle> --fc-hz N` | yes — `delay.py:229` | `delay_confirmation.json` | summary + path | none |
| §4 apply the winner (`:440-446`) | "`--alignment-prescription` on the round runner (a session-open key, not a prescriber verb)" | real: `jasper-round open --alignment-prescription PATH` (`jasper/cli/round.py:375-385`) **and** `scripts/run-crossover-round.py --alignment-prescription` | — | door refusal vocabulary | ⛔ "the round runner" is ambiguous — the phrase covers two different surfaces (on-box CLI vs laptop script) and the methodology names neither. The prescription JSON's schema is nowhere in the three docs; the LLM authors it from field names in prose (`delay_us`, `basis_delay_us`, `basis_artifacts`, `basis_note`, `polarity`). |
| §4 re-verify (`:455`) | NONE | — | — | — | ⛔ see §2 — no session-open verb named |

### §5 LEVEL MATCH

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §5 run the measured level-match rounds (`:467-472`) | **"There is no separate verb to run"** (`:477-478`) | the writer is the apply seam — `_bank_applied_base_trim` (`jasper/active_speaker/baseline_profile.py:3331`), reached through `apply_baseline_profile` | banked base trim (`jasper/active_speaker/driver_base_trim.py:238`) | — | ✅ **by design and the doc says so**, pointing to `testing-tooling.md`. But: the *apply* that banks it is `jasper-basic-profile apply` (`jasper/cli/basic_profile.py:419`, docstring `:14`) or the v2 apply — **`jasper-basic-profile` is named 0 times in the methodology**, so the failure-catalog row that says "apply a measured level match first (§5) — the apply banks it" (`:845`) is unactionable from this doc alone. |
| §5 pin a carried trim (`:483-486`) | NONE — `pinned_trim_db` on the driver prescription | door field, real (`docs/tuning-operator-runbook.md:458`) | — | — | ✅ field-level, doc says which surface |

### §6 LINEARIZE PER DRIVER

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §6 discriminator 1, min-phase/gate cascade (`:501-505`, `:516`) | `jasper-round-views classify-features <bundle-dir>` (`--dumps` optional) | yes — `jasper/cli/round_views/classify_features.py:166`; `--dumps` `:174` | `feature_classification.json` (in the artifact dir) | summary + path | none — the doc's "projects the ring out of the bundle itself" is confirmed (`classify_features.py:176-178`) |
| §6 discriminator 2, position invariance (`:507-510`) | NONE (`identify_interference_nulls` is a function) | — | rides in the packet | — | ✅ read off the packet |
| §6 read the gate geometry before the row (`:534-552`) | NONE | packet position rows | `packet.json` | — | ⛔ the reading is hand arithmetic (`≈ 1000/gate_reflection_delay_ms`) over rows the LLM must pull out of a large JSON |
| §6 gate rungs / gate sensitivity (`:553-577`) | `jasper-round-views gate-sweep` | yes — `jasper/cli/round_views/sweeps.py:118`; `--at-hz` `:124`, `--rungs-ms` `:123` | `gate_sweep.json` | one summary line per band on stderr + path (`sweeps.py:99-104`) | none |
| §6 spec verdict + window invariance (`:621-637`) | `jasper-round-views spec-sweep <round-dir> [--rungs-ms …]` | yes — `sweeps.py:109` | `<round-dir>/spec_gate_sensitivity.json` (`_common.py ARTIFACT_BY_VIEW`) | per-band summary line (`sweeps.py:76-81`) | none |
| §6 realization probe history (`:639-644`) | NONE (`classify_delta_probe`, `model_error_store`) | — | in the packet | — | ✅ |
| §6 controllability ledger (`:645-648`) | **"published on `/state` and printed by the round CLI"** | **both halves wrong** — see D-1 | — | — | ⛔ **the LLM will `curl :8780/state` and find nothing.** |
| §6 correct inside the trusted band (`:657-671`) | NONE (`gate_disclosure.evaluation_band_hz`) | — | packet | — | ✅ |

### §6a rungs

| Rung | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| Rung 1 — two floors off the spec report (`:687-692`) | NONE (read `entanglement_floor_source` then `_hz`) | fields real (`evidence_packet.py:138`) | spec report | — | ✅ field read |
| Rung 2 — `jasper-round-views gate-sweep <round_dir> --at-hz <max_deviation_hz> --out <path>` (`:694-704`) | yes — `sweeps.py:118` | `gate_sweep.json` | summary + path | ⚠ **contradicts §6.** `:636` says "`gate-sweep --at-hz` is now only for a bin the verdict did *not* flag"; `:697` says "**Always** pass `--at-hz` the failing band's own `max_deviation_hz`". Same doc, opposite instructions. |
| Rung 3 sizing — `jasper-round-views close-reference --distance --driver-diameter-in D --fc-hz FC` (`:709-711`) | yes — `jasper/cli/round_views/close_reference.py:164`; `--distance` `:168`, `--fc-hz` `:186`, `--driver-diameter-in` via `add_unit_pair` (`:191` → `jasper/cli/_unit_pair.py:31`) | — | mic distance | ⛔ **the doc itself says it: "the close-reference program row is #3498's amendment item 1 and is **not built**, so today you declare the distance yourself"** (`:711-712`). Declared NONE, honest. |
| Rung 3 compare — `close-reference --far-round A --close-round B --close-m M` (`:712-713`) | yes — `close_reference.py:173,174,180` | `close_reference.json` | verdicts per window/band | ⛔ the human must take a close capture with **no verb that captures it** — `jasper-measure` exists (`jasper/cli/measure.py:1010`) and is never named by either doc for this |
| Rung 4 elevation poses (`:718-724`) | `baseline/full` ±10/±20, `baseline/express` ±10 — staging walks them | **TRUE at HEAD** — `measurement_programs.py:94-97` (full), `:106-107` (express) | — | — | ⛔ "Until a round banks a pose with a non-zero `vertical_deg` the deciding experiment is **owed**" — an explicitly unmet experiment, no verb reports whether it has been met |

### §7 SUMMED VERIFY

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §7 four verdicts + the adoption row (`:731-735`) | **"read that table in code"** | `decide_adoption` / `ADOPTION_ROWS` (`jasper/active_speaker/crossover_v2/contracts.py:1031`, `crossover_v2/verification.py:1403`) | round receipt | — | ⛔ **explicit instruction to read source.** Justified ("dated prose copies exist") but it is a toolbox defect by the vision's principle 1: no verb prints the selected adoption row. Runbook says the same at `:725`. |
| §7 split level vs shape (`:742-755`) | NONE (`level_deviation_db` / `max_ripple_db`) | fields real | spec report | — | ✅ field read |
| §7 pooled listening-window lens (`:769-774`) | **"walk `both_at` stops"** | `both_at` is a **Python function** (`jasper/active_speaker/angle_capture.py:392`), not a CLI token. The CLI spelling is `jasper-angle-capture --angles 0,7,-7 --regime both` (`jasper/cli/angle_capture.py:696-706`) | — | — | ⛔ **three spellings, no bridge.** (a) `--regime` is `--angles`-only: "A program plays per_driver at every pose" (`angle_capture.py:704`); (b) `--angles` is documented as the *escape hatch* an LLM driver should **not** use — "LLM drivers stage a named program with `--program`" (`angle_capture.py:653`); (c) the word `regime` appears **zero** times in the runbook and zero in the methodology. So §7's pooled lens is only reachable via a flag the CLI tells the LLM not to use and neither doc documents. |

### §8-§10, failure catalog, honesty rules

| Step | Verb | Exists | Artifact | Reads back | WANTING |
|---|---|---|---|---|---|
| §8 declare the tilt (`:780-782`) | `jasper-round-views frozen` echoes `declared_tilt_db_per_octave` | yes — `jasper/cli/round_views/grades.py:134` | `frozen_reference.json` | grade summary | none |
| §8 audition by ear (`:793-796`) | `jasper-audition start --layer baseline\|full` | yes — `jasper/cli/audition.py:246`, `--layer` `:250-259` | runtime only | layer + expiry | none |
| §9 LF verification (`:803-804`) | **NONE — "needs near-field or mic-in-box methods — hands"** | — | — | — | ✅ declared out of scope, doc says why |
| §10 pre-register (`:820-822`) | `jasper-round-views frozen` (reports `expected_delta_db` beside the move) | yes — `grades.py:134` | `frozen_reference.json` | summary | ⛔ the *writing* of `expected_delta_db` is a driver-door field, no verb; authored by hand into the prescription JSON |
| §10 iterate / bank | **NONE** | `jasper-round bank <session-dir>` (`jasper/cli/round.py:416`) | `/var/lib/jasper/active_speaker/campaigns/<round-id>/` (`jasper/active_speaker/round_bank.py:61`) | banked dir on stdout | ⛔ never named in the methodology |
| Failure catalog row "A null that will not deepen" (`:845`) | "apply a measured level match first (§5) — the apply banks it" | the apply is `jasper-basic-profile apply` (`jasper/cli/basic_profile.py:419`) or the v2 apply | — | — | ⛔ names no verb; `jasper-basic-profile` is 0-mentioned in this doc |
| Failure catalog row "Measuring-noise chasing" (`:847`) | "re-measure the repeat floor" | `jasper-round-views repeat-floor` | — | — | ⛔ names no verb here (it is in §1c, 680 lines earlier) |
| Honesty rule 1 (`:851-853`) "every filter that went in unvouched" | NONE (`prescription.unvouched_filters`) | field real | — | — | ✅ field read |
| Honesty rule 5 (`:863`) "Keep and rollback cite a measured delta" | NONE | — | — | — | ✅ |

**Tally.** 41 rows. By-design NONEs where the doc says why: **12**. Real gaps (`⛔`): **21**.
Two internal contradictions (`⚠`) at `:636` vs `:697` and one stdout-volume note on `jasper-null`.

---

## B. Docs hygiene at HEAD

### B-1. Dated sentences — **26 lines confirmed** (the prior architect's count is TRUE)

Method: `grep -nE '20[0-9]{2}-[0-9]{2}-[0-9]{2}'`. 6 + 6 + 14 = 26 lines
(28 date *occurrences*: `tuning-methodology.md:22` carries the date twice — path and link text —
and `measurement-loop-doctrine.md:104-105` carries three dates across one sentence).

**docs/tuning-methodology.md (6)**
| Line | Kind |
|---|---|
| 22 | research-bank path `research/2026-08-31-tuning-methodology-deep-research/` — a dated *directory name*, unavoidable; the link resolves |
| 41 | "(owner ruling 2026-08-31)" — provenance |
| 138 | "owner ruling 2026-08-19" — provenance |
| 303 | "the 2026-08-31 flat campaign is the banked case" — history |
| 377 | "(jts3, 2026-08-31, as an example of the reading…)" — worked-example provenance, self-labelled |
| 578 | "**Ladder numbers banked before 2026-09-02 are in a different frame.**" — a live compatibility rule, load-bearing |

**docs/tuning-operator-runbook.md (6)**
| Line | Kind |
|---|---|
| 216 | "The 6-pose `lateral` walk **was retired** from stage 1 on 2026-08-22" — changelog |
| 379 | "the 2026-08-15 …" — history |
| 547 | "**it stopped refusing on 2026-08-23**" — changelog |
| 1105 | dead `captures/…` pointer (B-3) |
| 1363 | "measured 2026-08-06 on jts3" — measurement provenance |
| 1375 | "Last verified: 2026-08-26 (merge only — no claim re-derived against code)" — **stale by 10 days at HEAD** |

**docs/measurement-loop-doctrine.md (14)**
| Line | Kind |
|---|---|
| 3 | "ratified by the owner 2026-08-21" — status header |
| 48 | "**Owner ruling, 2026-09-01.**" — provenance |
| 104-105 | "Owner-ratified 2026-08-14 …, extended 2026-08-16, and re-affirmed 2026-08-22" — 3 dates, changelog |
| 159 | "refinement, 2026-08-22 (#2862)" — provenance |
| 178 | "(refusal census, 2026-08-25 …)" — provenance |
| 285 | "closed (2026-08-22)" — changelog |
| 287 | "closed (2026-08-22)" — changelog |
| 297 | "(2026-08-23)" — changelog |
| 305 | "(refusal census, 2026-08-25)" — provenance |
| 316 | "2026-08-03 ruling on #2087" — provenance |
| 372 | "Migration (2026-08-23, #2865): section 3 arrived from …" — pure migration changelog |
| 374 | "the owner's 2026-08-22 re-affirmation" — provenance |
| 378 | "Last verified: 2026-08-26 — …" — **stale by 10 days at HEAD** |

### B-2. "used to" / "previously" / "was retired" / history sentences

| file:line | Text |
|---|---|
| `docs/tuning-methodology.md:103` | "`horn_coverage_deg` **was retired** (`LEGACY_DROPPED_DRIVER_FIELDS`)" |
| `docs/tuning-methodology.md:570` | "a **smaller quantity** than the raw swing this bullet **used to** bound at ~1-2 dB" — narrates its own edit history |
| `docs/tuning-methodology.md:579` | "the ladder **moved onto** the engine's window family then" |
| `docs/tuning-methodology.md:636` | "`gate-sweep --at-hz` **is now** only for a bin the verdict did *not* flag" |
| `docs/tuning-methodology.md:320` | "**What changed is** how the coordinate is chosen." — changelog with no referent |
| `docs/tuning-operator-runbook.md:77-79` | "`crossover_v2/{search,objective,candidate_space}.py`, `fc_sweep`'s sweep half and `active_speaker/fc_selector.py` **were cancelled work** … and **the Wave-2 deletion PRs removed them** (tickets 2.2-2.4)" — changelog naming a **file deleted at HEAD** (see B-3) |
| `docs/tuning-operator-runbook.md:216-223` | the retirement paragraph, incl. "**what is gone is** the stage-1 arming and the adjudicating close" |
| `docs/tuning-operator-runbook.md:536` | "**replaced by it**" |
| `docs/tuning-operator-runbook.md:547` | "**it stopped refusing** on 2026-08-23" |
| `docs/measurement-loop-doctrine.md:275` | "this section **used to carry** a nine-row table of refusals that sat outside the list" |
| `docs/measurement-loop-doctrine.md:372-376` | the whole Migration trailer |

No `PR #` references found in any of the three files. Issue references (`#NNNN`) are why-pointers and are allowed by `AGENTS.md`.

### B-3. Pointers to a path or verb that does not exist at HEAD

Every markdown link in all three files resolves (checked all `](…)` targets against the tree). Backticked paths:

| file:line | Pointer | Verdict |
|---|---|---|
| `docs/tuning-operator-runbook.md:1105` | `captures/recommission-day2-2026-09-01/gate-sweep-validation/README.md` | **DEAD at HEAD.** `captures/` does not exist in the tree and is **gitignored** (`.gitignore:39`). On a fresh box the "banked worked example" the doc says to "read … rather than re-deriving it" is unreachable. **The prior architect's 1 dead `captures/…` pointer: TRUE.** |
| `docs/tuning-operator-runbook.md:77` | `active_speaker/fc_selector.py` | **File does not exist at HEAD** (`find` returns nothing; `git grep fc_selector` hits only docs, `journey.py:104`'s string constant `LATERAL_CONSUMER_FC_SELECTOR`, and tests). Deliberate — the sentence is *about* its deletion — but it is a dead path in a doc an LLM greps. |
| `docs/tuning-operator-runbook.md:454` | `captures/<campaign>/<label>/` | a template, not a claim; the directory is gitignored |

Every other backticked `*.py` basename in the three docs resolves to a real file (checked 33 of them by `find`). No dead verb: every binary and subcommand named in all three docs exists at HEAD — the generated menu passes `--check` (exit 0), and I opened each subparser (`declare_geometry.py:81,92`; `seat_level.py:613`; `angle_capture.py:923,930,937,946`; `measure.py:1010`; `crossover_prescriber.py:1279,1289,1318,1334`; `round.py:353,388,402,416`; `null_door.py:756`; `audition.py:246,263,267`; `basic_profile.py:412,419`; and all 18 `round_views` subcommands in `jasper/cli/round_views/{grades,seats,repeat,sweeps,delay,frequency,distortion,cloud_binding,forward_model,classify_features,close_reference,inventory}.py`).

**Paths the docs need but never give** (an LLM cannot type the command without reading code):

| Needed for | Real path | Named in the 3 docs? |
|---|---|---|
| `jasper-round bank <session-dir>` | `/var/lib/jasper/active_speaker/sessions/<session-id>/` — `jasper/active_speaker/bundles.py:71,392` | **no** |
| `prescriber stage --state <flow-state>` | `/var/lib/jasper/active_speaker_crossover_v2_state.json` — `jasper/active_speaker/crossover_v2/durable_state.py:61` | **no** (placeholder only, `runbook:130,138`) |
| reading §0's declared quantities | `/var/lib/jasper/active_speaker_design_draft.json` — `jasper/active_speaker/design_draft.py:50` | **no** |

### B-4. "not built" / "not yet" / "planned" / "owed"

| file:line | Text |
|---|---|
| `docs/tuning-methodology.md:127` | "sit in the **parked** LF program" |
| `docs/tuning-methodology.md:711-712` | "the close-reference program row is #3498's amendment item 1 and **is not built**, so today you declare the distance yourself" |
| `docs/tuning-methodology.md:723` | "the deciding experiment **is owed**: report the axis as unsampled" |
| `docs/tuning-operator-runbook.md:74` | "**ranking them is not built**" |
| `docs/tuning-operator-runbook.md:336,362` | ADR-0188 "…-relay-**parked**" (twice) |
| `docs/tuning-operator-runbook.md:480-485` | "Multiple DSP *configs* per position has a door but **no wiring** … **the open part is sequencing** … That is **a design to write**, not a refusal to remove" |
| `docs/tuning-operator-runbook.md:488` | "**Still ahead:** versioning these pose lists." |
| `docs/tuning-operator-runbook.md:491-492` | "ticket 3.7 **turns them into code**" — the measurement constants live only in `tuning-master-plan.md` prose |
| `docs/tuning-operator-runbook.md:649` | "**parked**." |
| `docs/tuning-operator-runbook.md:781-782` | "and clipping are **deliberately not built**" |
| `docs/tuning-operator-runbook.md:1353-1359` | three declared v2 non-goals (3-way, sub/main alignment, Fc re-derivation) |
| `docs/tuning-operator-runbook.md:1360` | "Commissioning's headroom on a literal 1 GB Pi **is unmeasured**" |
| `docs/measurement-loop-doctrine.md:63-75` | "**Known departure, open at writing time.** … the code **does not yet meet it** everywhere … Closing it is measurement-path work and **has no issue of its own yet**." |

No `TODO` token in any of the three files.

---

## C. The shortest real round — fresh box, human mover

Every command below is what an owner would actually type at an SSH prompt on the speaker.
**Source** column: `DOC` = derivable from the three docs alone; **`CODE`** = I had to open a
Python file to know what to type. Every `CODE` row is a finding.

| # | Command | Precondition | Artifact left | Source |
|---|---|---|---|---|
| 1 | `sudo -n /opt/jasper/.venv/bin/jasper-declare-geometry set --speaker-height-in 33 --mic-height-in 33 --distance-in 39` | human measured 3 lengths | `/var/lib/jasper/measurement_geometry.json` | **DOC** — verbatim at `tuning-methodology.md:70-72` |
| 2 | `sudo -n /opt/jasper/.venv/bin/jasper-declare-geometry show` | step 1 | — (exit 2 ⇒ nothing declared *or* unreadable) | **DOC** `:75-79` |
| 3 | `amixer -c <card> set <capture-control> 100%` | wired UMIK at the seat | — | **CODE/out-of-band** — the card and control names come from nowhere; the requirement appears only inside `jasper-seat-level`'s `description` (`jasper/cli/seat_level.py:619-622`). ⚠ *docs alone would not tell you what to type.* |
| 4 | `sudo -n /opt/jasper/.venv/bin/jasper-seat-level --calibration-file /var/lib/jasper/mic-cal/<serial>.txt` | step 3; calibrated mic | banked session measurement reference | **CODE** — §1a names only the bare binary (`tuning-methodology.md:133`). `--calibration-file`/`--mic-serial` is *required* (`seat_level.py:642-643`) and appears in neither doc. ⚠ |
| 5 | `sudo -n /opt/jasper/.venv/bin/jasper-angle-capture plan --program baseline --size express --mover human` | drivers declared | nothing (resolve + print) | **DOC** for `plan --program --size` (`runbook:466-477`, `methodology:197-198`); **CODE** for `--mover human` (`angle_capture.py:707-717`) — it is the default, so omitting it works, but no doc says so ⚠ |
| 6 | `sudo -n /opt/jasper/.venv/bin/jasper-angle-capture stage --program baseline --size express` | step 5 read | spool file; prints `price.mic_moves`, 5 stops, and `handoff_url` | **DOC** `methodology:197-200` |
| 7 | **`/opt/jasper/.venv/bin/jasper-round open --tier express`** | steps 4+6 | live session bundle under `/var/lib/jasper/active_speaker/sessions/<id>/` | **DOC via the runbook only** (`runbook:145`) — ⚠ **the methodology never mentions this step at all.** This is "which verb runs the walk on the box": *none does* — `jasper-round open` posts the session and the human walks it on the HTTPS handoff URL. `jasper-round`'s own help says "it does not stage an angle walk or run the arm" (`jasper/cli/round.py:323-326`). |
| 8 | *(human)* open `https://<speaker>/sound/crossover/` on a phone, tap through 5 poses | trusted private CA installed (`runbook:169-175`) | takes | **DOC**, but **out-of-band by construction** — the pacing surface is a browser, not a verb. `jasper-crossover-prescriber status --json` (`banked.walk.n_takes`, `crossover_prescriber.py:717-727`) is the only read-back. ⚠ |
| 9 | `/opt/jasper/.venv/bin/jasper-round wait --timeout-s 1200` | step 7 | prints `session_id` + `candidate_fingerprint` (`round.py:192-204`) | **DOC** `runbook:145` |
| 10 | `/opt/jasper/.venv/bin/jasper-round bank /var/lib/jasper/active_speaker/sessions/<session-id>` | step 9 finished | `/var/lib/jasper/active_speaker/campaigns/<round-id>/` | **CODE** ⚠⚠ — `wait` prints the `session_id` but **not the directory**, and the sessions root (`jasper/active_speaker/bundles.py:71`) appears in **none of the three docs**. The LLM must `ls /var/lib/jasper/…` blind or read `bundles.py`. |
| 11 | `/opt/jasper/.venv/bin/jasper-round-views inventory <round-dir>` | step 10 | `inventory.json`; stderr names the subcommand for each missing artifact (`jasper/cli/round_views/inventory.py:71-78`) | **DOC** `methodology:47-48` — the one verb that answers "what have I not run yet" |
| 12 | `/opt/jasper/.venv/bin/jasper-crossover-prescriber packet <round-dir>` | step 10 | `<round-dir>/packet.json` + summary only | **DOC** `runbook:114-119`, `doctrine:34` |
| 13 | views worth running: `jasper-round-views entry <round-dir>` · `spec-sweep <round-dir>` · `classify-features <round-dir>` · `frequency <round-dir>` · `directivity <round-dir>` · (2-way) `delay-landscape <round-dir> --fc-hz <FC>` | step 10 | `entry_state_grade.json`, `spec_gate_sensitivity.json`, `feature_classification.json`, `frequency_view.json`, `directivity.json`, `delay_landscape.json` | **DOC** for `spec-sweep`/`classify-features`/`delay-landscape` (`methodology:631,516,423`); **CODE** for `entry` / `directivity` / `frequency` — they exist only as words in the menu row's prose blob (`runbook:434`), and §3b/§7's "measured horizontal patterns" never names `directivity` ⚠ |
| 14 | author `prescription.json` by hand, then `jasper-crossover-prescriber propose --packet <round-dir>/packet.json --prescription -` | step 12 | validated dry run | **DOC** `runbook:134-136` — but **the prescription schema is in no doc.** Field names are scattered through methodology prose (`:442-446`, `:485`, `:820`) ⚠ |
| 15 | `jasper-crossover-prescriber stage --packet <round-dir>/packet.json --state /var/lib/jasper/active_speaker_crossover_v2_state.json --prescription -` | step 14 accepted | `/var/lib/jasper/active_speaker_crossover_v2_prescription.json` | **CODE** ⚠ — the `--state` path is only in `jasper/active_speaker/crossover_v2/durable_state.py:61`; the docs write `<flow-state>` (`runbook:138`) |
| 16 | `jasper-round apply --expected-fingerprint <fp from step 9>` | steps 9+15 | applied graph; apply seam banks the base trim (`baseline_profile.py:3331`) | **DOC** `runbook:158-160` — ⚠ **the methodology never names an apply verb, and doctrine §1's loop has no apply step** (`doctrine:26-44`) |
| 17 | re-measure: repeat steps 6-10 (`--tier express`, `--stage post_apply` on the open) | step 16 | second round | **CODE for `--stage post_apply`** — `jasper/cli/round.py:366-374`; the docs say "a verify round" (`runbook:161`) without the flag ⚠ |
| 18 | `jasper-round-views frozen <baseline-round> <new-round>` | two banked rounds | `frozen_reference.json`; echoes `expected_delta_db` and `declared_tilt_db_per_octave` | **DOC** `methodology:781,822`, `doctrine:41-42` |

**Score: 18 steps; 7 marked ⚠ where the docs alone would not tell you what to type**
(3, 4, 5-partial, 10, 13-partial, 15, 17), plus 2 more where the doc that *does* tell you
is not the methodology (7, 16). Three of those (3, 10, 15) are hard blockers — the command
cannot be completed without reading source or guessing a filesystem path.

---

## D. Two docs, one fact — different wording or values

**D-1. The controllability ledger's location — a direct contradiction with a functional consequence.**

| Source | Statement |
|---|---|
| `docs/tuning-methodology.md:645-648` | "read the controllability ledger (`jasper.active_speaker.controllability_ledger`, **published on `/state`** and **printed by the round CLI**)" |
| `docs/tuning-operator-runbook.md:463` | "`GET :8780/state` … **no round section** — round evidence is file-based" |
| Code | `jasper/web/correction_crossover_v2_status.py:192` puts it in the **crossover-v2 wizard's** status block, reached at `GET /correction/crossover/status` (`jasper/active_speaker/wizard_client.py:29` `STATUS_PATH`) on the correction wizard, **not** `:8780/state`. It is printed by `scripts/run-crossover-round.py:830` (`summarise_controllability`) — the **laptop** script, which the runbook lists under "Other surfaces … not CLIs" (`runbook:445-452`). `git grep controllability -- jasper/cli` returns nothing: **`jasper-round` does not print it.** |

Verdict: **methodology:646 is FALSE on both halves.** An LLM on the box following §6 curls
`:8780/state`, finds no ledger, and has no second place to look.

**D-2. `verify.gate` field names — the methodology elides a rename the runbook states.**

| Source | Statement |
|---|---|
| `docs/tuning-methodology.md:542-543` | "The packet's position rows carry `gate_reflection_delay_ms` and `gate_moved_rms_db` per capture, and **`verify.gate` carries the same pair** for the verify." |
| `docs/tuning-operator-runbook.md:921-922` | "…`gate_moved_rms_db` and `gate_reflection_delay_ms`; **`verify.gate` carries the same pair as `moved_rms_db` / `reflection_delay_ms`**." |
| Code | `jasper/active_speaker/crossover_v2/evidence_packet.py:1461-1472` — `_POSITION_GATE_NUMBER_FIELDS` = `{gate_moved_rms_db, gate_reflection_delay_ms}`; `_VERIFY_GATE_NUMBER_FIELDS` = `{moved_rms_db, reflection_delay_ms}`, "the `gate_` prefix is dropped because the block is already the gate". |

Verdict: runbook TRUE, methodology **misleading** — "the same pair" reads as the same keys.
A driver written against §6 looks up `verify.gate.gate_moved_rms_db` and gets nothing.

**D-3. `jasper-declare-geometry` exit 2 — same fact, one doc states half of it.**

| Source | Statement |
|---|---|
| `docs/tuning-methodology.md:77-79` | "it exits 2 **both for 'nothing declared' and for 'could not read it'**, so the sentence on stderr is what separates them" |
| `docs/tuning-operator-runbook.md:637-639` | "keeps `2` = `EXIT_NOT_FOUND` (**`show` before anything was declared**) — it is the one entry in `OWN_EXIT_VOCABULARY`" |
| Code | `jasper/cli/declare_geometry.py:140` (unreadable) and `:143` (nothing declared) both `return EXIT_NOT_FOUND` (= 2, `:31`). `OWN_EXIT_VOCABULARY = {"jasper.cli.declare_geometry"}` at `jasper/cli/_refusal.py:29-31`. |

Verdict: methodology complete, **runbook PARTIAL** — its parenthetical names only one of the two causes.

**D-4. The pooled-window walk: `both_at` vs `--regime both` vs nothing.**

| Source | Spelling |
|---|---|
| `docs/tuning-methodology.md:772` | "walk **`both_at`** stops (per-driver AND summed at each angle, one mic move)" |
| `docs/tuning-operator-runbook.md` | the word `regime` and the word `both_at` **do not appear** (`grep -n regime` → 0 hits); the menu's program paragraph (`:465-477`) documents `--program`/`--size`/`--angles` only |
| Code | `both_at` is a Python helper (`jasper/active_speaker/angle_capture.py:392`); the CLI spelling is `--regime both` (`jasper/cli/angle_capture.py:696-706`), `--angles`-only, and `--angles` is labelled the operator escape hatch that "LLM drivers" should not use (`angle_capture.py:653`) |

Verdict: three names for one capability, and the runbook — the tool manual — documents none of them.

**D-5. `--at-hz` — the methodology contradicts itself (one doc, both statements load-bearing).**

`docs/tuning-methodology.md:636` "`jasper-round-views gate-sweep --at-hz` **is now only** for a bin the verdict did *not* flag" vs `docs/tuning-methodology.md:697` "**Always** pass `--at-hz` the failing band's own `max_deviation_hz`". Both describe §6a rung 2's normal case.

**D-6. Hostname — the runbook contradicts itself.**

`docs/tuning-operator-runbook.md:164-165` "URLs are hostname-derived. Speakers are `jts1.local`, `jts3.local`, … — **never a hard-coded `jts.local`**" vs `docs/tuning-operator-runbook.md:192` "`http://jts.local/correction/` → the crossover step".

**D-7. "the round runner" and "the round CLI" — one phrase, two tools.**

`docs/tuning-methodology.md:441` "`--alignment-prescription` on **the round runner**" = both `scripts/run-crossover-round.py` and `jasper-round open --alignment-prescription` (`jasper/cli/round.py:375-385`; runbook `:94-96` names both). `docs/tuning-methodology.md:647` "printed by **the round CLI**" = `scripts/run-crossover-round.py:830`, which is not a CLI at all by the runbook's own taxonomy (`:445-452`). The methodology uses two unnamed noun phrases for three distinct surfaces.

**D-8. Staleness trailers disagree with HEAD.**

`docs/tuning-operator-runbook.md:1375` and `docs/measurement-loop-doctrine.md:378` both say "Last verified: 2026-08-26"; `docs/tuning-methodology.md` carries no such trailer at all — three sibling docs, two conventions.

---

## Could not determine

Whether the pooled listening-window lens (§7, `docs/tuning-methodology.md:769-774`) is meant to be
reachable from a **named program** — no program row in `jasper/active_speaker/measurement_programs.py:121-128`
plays a `both` regime, and `angle_capture.py:704` says "A program plays per_driver at every pose",
so today it is `--angles`-only. Whether that is an intentional restriction or an unwired
capability would be settled by issue #3498 / the tournament program's design notes, which I did not open.
