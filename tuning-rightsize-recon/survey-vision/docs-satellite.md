# docs-satellite — tuning-master-plan.md and active-speaker-tuning-layers-design.md

HEAD: f4ff89731979abfb0f27a4afb3f220d22d21e78c (origin/main, confirmed ancestor). Read-only.

## 1. Section map + classification

Classes: CONSTANT (LLM needs it to drive a round) / DESIGN-RATIONALE (why, dev-facing)
/ HISTORY (dated, changelog, superseded-plan prose) / STALE (verb/file/path/value that
doesn't hold at HEAD) / DUPLICATE (restates doctrine/methodology/runbook — both lines cited).
Most sections carry several classes at once (this corpus mixes ratified rules with the
session-log evidence for them in the same paragraph); the dominant class is listed first.

### docs/tuning-master-plan.md (791 lines)

| Header | Lines | Count | Class |
|---|---|---|---|
| Title/intro | 1-31 | 31 | HISTORY (status banner: "adopted 2026-08-21", "Wave 6 ... adopted 2026-08-31") |
| Product invariants | 32-104 | 73 | DESIGN-RATIONALE + HISTORY (9 numbered invariants, several amended in place: "#2871", "2026-08-22") |
| The three loops, one substrate | 105-142 | 38 | DESIGN-RATIONALE (owner table of substrate contracts; a few CONSTANT pointers e.g. `MARK_DISTANCE_M`) |
| Decision register | 143-165 | 23 | HISTORY (R1-R14 ruling log, every row dated/PR-numbered) |
| **Measurement program constants** | 166-251 | 86 | **CONSTANT** (see Q2 — the one section built to be read every round) |
| Considered and deliberately not built (v1) | 252-288 | 37 | DESIGN-RATIONALE (negative-space record, not needed to drive) |
| Supersessions and standing docs | 289-352 | 64 | HISTORY (which older doc superseded which, all dated) |
| Work breakdown | 353-736 | 384 | HISTORY (Wave 0-6 ticket list, sizes, PR/issue numbers — pure planning backlog) |
| The operator session, end to end | 737-750 | 14 | DUPLICATE of `docs/tuning-methodology.md`'s own step-by-step (methodology:1-22 header table + its body) and `docs/tuning-operator-runbook.md`'s happy path — same loop restated a third way |
| Calibration experiments (optional, lab-rig) | 751-771 | 21 | CONSTANT (E2's `MEASURED_BENEFIT_MARGIN_DB`/`ITERATION_PLATEAU_DB`, both verified in code — see Q2) + HISTORY (dated first-run result) |
| Provenance | 772-791 | 20 | HISTORY (session/PR provenance, "last verified" footer) |

**384 of 791 lines (49%) are the Work breakdown alone** — a ticket backlog (Wave 0-6,
sizes S/M/L, issue numbers) with no content an LLM driving a round consumes.

### docs/active-speaker-tuning-layers-design.md (1942 lines)

| Header | Lines | Count | Class |
|---|---|---|---|
| Title/status header | 1-62 | 62 | HISTORY (8 dated amendment banners stacked on the title, 2026-07-23 through 2026-08-29) |
| Why this exists (one paragraph of history) | 63-78 | 16 | HISTORY (named in its own header) |
| The five layers | 79-116 | 38 | DESIGN-RATIONALE (the one section worth an LLM's context — names layer ownership) |
| Decisions already made (do not re-litigate) | 117-310 | 194 | HISTORY (15 dated owner rulings, #1665-#2872, each with its own evidentiary paragraph) |
| Layer 1a concretely — UX and data flow | 311-444 | 134 | DESIGN-RATIONALE + CONSTANT (correction-envelope formula, cold-start priors — see below) + **STALE** (line 410, see finding) |
| CD-horn compensation — the top-octave HF stage (#1668) | 445-630 | 186 | HISTORY (a session's dated forensic log: "+8.13 dB", "run-6", "2026-07-24 JTS3 runs") with some still-live constants embedded |
| The region-based adjustment contract (2026-08-17) | 631-707 | 77 | HISTORY (one paragraph of history is literally its own subheading) + DESIGN-RATIONALE |
| The prescriber seam (open decision) | 708-752 | 45 | HISTORY ("Update (2026-08-19): ... did not resolve the way it was written to") |
| Measurement Program v2 — the capture schedule | 753-764 | 12 | HISTORY (banner: "ratified ... NOT built") |
| The schema | 765-780 | 16 | DESIGN-RATIONALE, superseded — see STALE finding below (position set) |
| Two stimulus regimes, and the session's one-timers | 781-819 | 39 | CONSTANT-shaped but **unbuilt** (D/S regimes, SPL anchor) — not what shipped (see Q2/finding) |
| The position set, and the unit | 820-845 | 26 | **STALE** — "0°, ±7°, ±22°" superseded by the shipped 13-pose baseline (finding below) |
| The schedule — position-major | 846-918 | 73 | HISTORY (PR #2715/#2717 budget arithmetic) — describes an unbuilt schedule |
| Constraints carried forward | 919-957 | 39 | HISTORY + one still-live CONSTANT (`MEASURE_REPEAT_COUNT = 3`, verified) |
| Deferred axis — elevation (v2+) | 958-975 | 18 | DESIGN-RATIONALE (explicitly "not built and not scheduled") |
| Sequencing, and what this reverses or supersedes | 976-1008 | 33 | HISTORY |
| Linearization pipeline intro/ruling | 1009-1021 | 13 | HISTORY (ratified 2026-08-19 banner) |
| The ruling | 1022-1044 | 23 | DESIGN-RATIONALE (P1/P2/P3 order — the one durable idea in this doc) |
| How the stages map onto the five layers | 1045-1059 | 15 | DESIGN-RATIONALE |
| Stage P1 — seed from driver knowledge | 1060-1125 | 66 | DESIGN-RATIONALE, STATUS:EXISTS, verified current |
| Stage P2 — crossover tuning by measurement | 1126-1410 | 285 | HISTORY (dated STATUS narrative, gap-by-gap, "measured on jts3 on 2026-08-19") |
| Stage P3 — EQ the minimum-phase residue | 1411-1616 | 206 | HISTORY + **STALE** (line 1554, see finding) |
| Provenance, and what this section supersedes | 1617-1634 | 18 | HISTORY |
| Session operating model | 1635-1650 | 16 | DUPLICATE of AGENTS.md's own "standing multi-agent method" pointer (tuning-master-plan.md:28 already deflects to AGENTS.md for this) |
| Composition & code seams (verified present) | 1651-1677 | 27 | DESIGN-RATIONALE, spot-checked current |
| Speaker-class applicability (#1671) | 1678-1704 | 27 | HISTORY/DESIGN-RATIONALE, 2-way only shipped |
| Microphone doctrine (#1672) | 1705-1725 | 21 | DESIGN-RATIONALE, open arbitration |
| Execution plan for the implementing session | 1726-1759 | 34 | HISTORY (phase-by-phase PR log, #1666-#1672) |
| Issue ledger (all open threads, one place) | 1760-1942 | 183 | HISTORY (a per-pass changelog of what got "trued up" and when — meta-history about the doc itself) |

**The last 183 lines are a changelog of the document's own revision history** (which
pass corrected which paragraph), not tuning content at all.

## 2. "Measurement program constants" (master-plan:166-251) — every constant vs. code

| Constant | Doc value (line) | Code | Verdict |
|---|---|---|---|
| `baseline` pose count/layout | 13 poses: horiz 0°,±10°,±20°,±30°,±40°; vert 0°,±10°,±20° (172-177) | `jasper/active_speaker/measurement_programs.py:84-96` `_BASELINE_FULL_POSES` — exact match, 13 distinct bearings | TRUE |
| Anchor repeats | ×4 at 0°, ×1 elsewhere (174-176) | `measurement_programs.py:35` `ANCHOR_REPEATS = 4` | TRUE |
| Seat-level anchor | `DEFAULT_TARGET_DB_SPL` = 77.5 dB SPL (183-185) | `jasper/active_speaker/seat_level_reference.py:55` `= 77.5` | TRUE |
| Commissioning ceiling | 85 dB on every shipped preset; schema cap 45-85 (186-190) | `jasper/active_speaker/profile.py:551,597` `max_commissioning_level_db_spl: float = 85.0`, `45 <= ... <= 85`; both preset JSONs declare 85 | TRUE |
| Distance rule default | default 1 m (207-208) | `jasper/active_speaker/crossover_v2/spatial.py:1393` `MARK_DISTANCE_M = 1.0` | TRUE |
| Distance rule conditionals (3× baffle farfield; C-t-C≥1λ→5° tighten; gate<3ms→0.5m; σ>0.5dB warn) | 207-211 | No coded threshold found (`git grep` across `jasper/active_speaker`, `jasper/audio_measurement` for "3 ms"/"tighten"/"0.5 m"/baffle-farfield logic returns nothing matching this rule) | **COULD-NOT-DETERMINE / likely LLM-executed judgment, not a coded gate** — the only distance limits in code are rig-safety bounds (`measurement_geometry.py:29-31`, MIN/MAX_DISTANCE_M = 0.15/3.0), unrelated to this rule |
| Escalation step | anchor +10 dB "where the ceiling permits" (194-201) | `SafetyEnvelope.escalation_step_db` exists (`profile.py:552`, default 5.0, cap ≤10 at line 603) but **has zero callers anywhere in `jasper/`** outside `profile.py` and the two preset JSONs (which declare 5 and 3, not 10) | **FALSE / dead field** — the doc's escalation mechanism is not wired to any code path under this name; the similarly-named field is unused and disagrees in value (5/3 vs the doc's 10) |
| Boost probe | +6 dB standard, +3 dB tight headroom; correctable ≥~0.8× commanded, non-correctable ≤~0.5× (222-227) | No `BOOST_PROBE`/`+6`/`+3` constant found in `jasper/active_speaker` or `jasper/audio_measurement`. `delta_probe.py:161` has an unrelated `DELTA_PROBE_SHORTFALL_GAIN_CEILING = 0.85` (different threshold, different purpose — realization-shape shortfall, not dip-fill correctability) | **COULD-NOT-DETERMINE / not coded as a named constant** — reads as an LLM-executed test procedure, not a gate |
| Stopping rule | ~2× own aggregate sampling SE, 2 consecutive rounds, ±1-1.5 dB @ 1/3-1/6 octave (233-238) | `NBD_BAND_OCTAVES = 0.5` exists (`olive_metrics.py:38`) but no "×2 SE" or "2 consecutive rounds" threshold found anywhere in `jasper/active_speaker` or `jasper/audio_measurement` | **COULD-NOT-DETERMINE / not coded** — this is a judgment call the doc hands to the LLM operator, not an automated stop |
| Nearfield splice v1 constants (0.055×D mic distance, ≈4311/D_inches validity, c/(3M) enclosure limit, f₃≈115/W baffle step, ±1-3 dB budget) | 239-251 | Zero hits for any of these figures/formulas in `jasper/`. `measurement_programs.py:19` explicitly states **"No nearfield poses — that regime is not wired"** | **TRUE-BUT-UNBUILT** — this is Wave-4 ticket 4.2 ("Nearfield splice v1 per the constants above", master-plan:552-553), correctly disclosed by the doc itself as future work, not a stale/broken claim |
| `MEASURED_BENEFIT_MARGIN_DB` / `ITERATION_PLATEAU_DB` (Calibration experiments, 751-771, not inside the constants section itself but cross-referenced from it) | 0.5 / 0.25 (line 758) | `jasper/active_speaker/crossover_v2/round_evidence.py:88,97` — exact match | TRUE |

**Net for Q2:** 6 of the 10 items check out exactly against code (pose layout, repeats,
SPL anchor, ceiling, distance default, the two calibration constants). 3 items — escalation
step, boost probe, stopping rule — have **no corresponding coded constant at all**; the doc
frames them as arithmetic the LLM operator applies by hand each round, which is internally
consistent with the doc's own "LLM judges" framing (master-plan:56, invariant 4) but means
an LLM cannot `grep` its way to these numbers — it has to re-derive them from this one prose
section every time. The nearfield-splice bullet is honestly-disclosed unbuilt work, not a
contradiction.

## 3. Lines an LLM actually needs to drive one round

**From tuning-master-plan.md: ~86 lines** — only "Measurement program constants"
(166-251). Nothing else in the file's 791 lines is needed mid-round: Product invariants,
Decision register, Supersessions and the 384-line Work breakdown are read once (if ever)
by whoever is *building* the toolbox, not by the LLM driving a measurement session. The
"operator session end to end" summary (737-750, 14 lines) duplicates tuning-methodology.md
and tuning-operator-runbook.md and adds nothing a driving session needs beyond what those
two already say.

**From active-speaker-tuning-layers-design.md: 0 lines are load-bearing for driving a
round.** Every operationally-live number this doc contains is a code constant with its
own home: cold-start mic-trust tiers (`linearization_envelope._MIC_TRUST_TABLE_HZ`),
boost caps (`linearization_fit.PER_FILTER_BOOST_CAP_DB`), the correction-envelope formula
(`linearization_envelope.compose_envelope`'s own docstring). The doc's job — per its own
title, "(design)" — is answering *why* the five-layer split and the P1→P2→P3 ordering
exist, which a driving LLM does not need mid-session; the runbook (tuning-operator-runbook.md)
and methodology own the *what to do next*. "The five layers" (79-116, 38 lines) is the
closest thing to a quick-reference table (layer → job → instrument → re-run trigger) and
is the one section a *new* driving session might benefit from skimming once, but
tuning-methodology.md and the runbook do not currently point at it for that purpose —
methodology's header table (tuning-methodology.md:13-14) sends the LLM to this whole
1942-line file only for "why the correction layers are shaped this way," which is a
question a mid-round LLM is not asking.

**Total: ~86 of 2,733 combined lines (3.1%) are round-time-load-bearing**, against the
3,627 lines (872+791+1942, methodology+master-plan+layers-design combined, before adding
the 1,375-line runbook and 380-line doctrine the record already counted) the LLM is told
these two files might make it open.

## 4. Cross-references (`git grep -n "tuning-master-plan\|tuning-layers-design"`)

Ran against `docs jasper scripts AGENTS.md README.md` (repo root `README.md` has zero
hits — it does not mention either doc; the only `README.md` hit is `docs/README.md`).

- **docs/README.md:39** — one-line doc-index entry for the layers doc ("Tuning layers"). Live.
- **docs/doc-map.toml:730,734** — both docs registered. Live (structural registry).
- **docs/tuning-methodology.md:13-14, tuning-operator-runbook.md:14,217,490,746,1245** —
  the pointers this survey was asked to check. All live.
- **docs/measurement-loop-doctrine.md:166,243** — cites master-plan twice. Live.
- **docs/adr/0018, 0227, 0229, 0231** — four ADRs cite one or both docs as the record of a
  ruling. Live (ADRs are append-only by policy, so these never go stale by definition).
- **docs/historical/*.md (9 files)** — audio-commissioning-roadmap, attribution-stage-plan,
  crossover-measurement-v2-campaign-record, crossover-v2-engine-design,
  flat-campaign-2026-08-31, linearization-campaign-2026-07, llm-native-tuning-workbench-plan,
  tuning-bench-design, tuning-bench-execution-plan — all cite one or both as their
  superseding authority. Expected; these are the docs the master plan says it superseded.
- **docs/research/2026-07-23-driver-linearization/{README,02-engineering-spec}.md** and
  **docs/research/2026-08-31-tuning-methodology-deep-research/{00,01,03,04,05}.md** — cite
  the master plan back (see Q5).
- **jasper/ (8 hits)**: `blend_correction.py:6`, `evidence_packet.py:1815`, `fc_sweep.py:10`,
  `feature_classification.py:7`, `driver_protection.py:35,319`,
  `linearization_envelope.py:12`, `linearization_fit.py:18`, `measurement_programs.py:11`,
  `web/correction_crossover_v2.py:1534` — all "why"-pointer comments citing a specific
  decision/section in one of the two docs. All the cited code objects exist at HEAD (spot
  checked: `PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE`, `_MIC_TRUST_TABLE_HZ`,
  `PER_FILTER_BOOST_CAP_DB`, `measurement_programs.py`'s own docstring pointer, all present).
- **scripts/_lib.sh:49, scripts/generate-tuning-tool-menu.py:11** — comment pointers, live.

**No dead reference found.** Every file that names either doc still exists and the doc
still exists at the path named. This is a healthy reference graph — the volume of hits is
citation/provenance density (the doctrine of "why-pointers" from AGENTS.md working as
intended), not link rot.

## 5. docs/research/

- **Referenced from the three main docs?** Only two of three, and narrowly:
  - `tuning-methodology.md:22` points at exactly one file:
    `research/2026-08-31-tuning-methodology-deep-research/00-adjudications.md`, framed as
    "sourced provenance for the thresholds" — a citation, not something the header table
    tells the LLM to consult while driving.
  - `tuning-master-plan.md:668,674` (Wave 6 ticket 6.9) names the same subdirectory as
    where four owner research assignments landed.
  - `tuning-operator-runbook.md` and `measurement-loop-doctrine.md`: **zero** references
    to `docs/research/` (checked directly, no hits).
  - `active-speaker-tuning-layers-design.md:156` separately points at a *different*
    subdirectory, `research/2026-07-23-driver-linearization/`, as the source of its own
    "Sources" decision (#7).
- **Total size:** 1.6 MB across 56 files in 10 dated subdirectories
  (2026-05-25 through 2026-08-31).
- **File the walk actually needs:** none. The two docs' own framing is "sourced
  provenance" / "results land under" — citation trail for where a ratified number came
  from, not operational content. The specifically-cited deep-research folder itself is
  144 KB / 683 lines across 5 files (00, 01, 03, 04, 05 — **02 is missing from the
  sequence**; master-plan:674 names 4 assignments and the doc says the 2nd, "reference
  axis and position artifacts," "resolved into 6.13's pooled-window co-metric rather than
  a moved microphone" instead of shipping as its own numbered file — consistent, not a
  gap, but worth knowing before searching for a "02-*" file that was never written).

## 6. Dated/history sentence count (same criterion: dates, "PR #", "used to", "previously", "as of", "was", "superseded")

**tuning-master-plan.md: 54 matching lines** (of 791, ~7%). First 30:

3, 6, 9, 13, 44, 64, 79, 133, 145, 151, 152, 158, 159, 162, 164, 166, 183, 185, 188, 292,
297, 299, 300, 301, 314, 318, 332, 333, 334, 335

**active-speaker-tuning-layers-design.md: 173 matching lines** (of 1942, ~9%). First 30:

3, 5, 11, 17, 22, 24, 28, 32, 35, 37, 44, 47, 54, 74, 156, 163, 166, 181, 184, 199, 209,
230, 234, 236, 243, 252, 254, 256, 269, 280

Both counts undercount true history density: the grep criterion catches explicit dates and
a handful of keywords, but large stretches of prose (e.g. the entire CD-horn compensation
and Stage P2/P3 sections) are dated-session narrative that reads as history without
tripping "was"/"previously" on every sentence — e.g. "Reproducible: +8.13 dB hotter..."
(layers-design:611) is unambiguously a dated hardware-run log entry with no date token or
trigger word on that exact line, immediately preceded (line ~608, "What this replaced
(2026-08-19)") by one that does trip it. The 173-count for the layers-design doc is a
floor, not a ceiling, on how much of its 1942 lines are campaign history rather than
standing design.

## Findings that most change the picture

1. **Three of the master-plan's own "Measurement program constants" have no coded
   counterpart at all** — escalation step (+10 dB), boost probe (+6/+3 dB, 0.8×/0.5×
   gain-back), and the stopping rule (2× SE, 2 consecutive rounds) exist nowhere as named
   constants in `jasper/` (master-plan:194-201, 222-227, 233-238). The one same-named field,
   `SafetyEnvelope.escalation_step_db` (`profile.py:552`), has zero callers and disagrees in
   value (5.0/preset values of 5 and 3, vs. the doc's 10). An LLM cannot look these up in
   code; it must re-derive them from this one prose section every round.

2. **active-speaker-tuning-layers-design.md:1554 claims PR #2730 "merged" and widened
   `BLEND_FILTER_Q` into a "sign-split pair"** (wider ceiling for cuts, old value kept for
   boosts) — but the same document says "(open)" for the identical PR 57 lines earlier
   (line 1497), and at HEAD `blend_correction.py:95` still defines `BLEND_FILTER_Q = 2.0`
   as a single scalar with no split. Line 1554 is **STALE/FALSE at HEAD**.

3. **The layers-design doc's own ratified "position set" (0°, ±7°, ±22°, lines 820-845)
   was superseded by a different pose layout that is what actually shipped.**
   `measurement_programs.py`'s `_BASELINE_FULL_POSES` (lines 84-96) — which the module's own
   docstring says comes from `tuning-master-plan.md`'s constants section — uses 0°, ±10°,
   ±20°, ±30°, ±40° horizontal and 0°, ±10°, ±20° vertical (13 poses), not the layers-design
   doc's 5-pose "one center plus symmetric pairs" structure. The later master plan won; the
   earlier doc's specific numbers were never reconciled in place (it does hedge "the numbers
   are tunable," but the *structure* — pair count — also changed, which the hedge doesn't cover).

4. **384 of the master plan's 791 lines (49%) are a dated ticket backlog** (Work breakdown,
   lines 353-736, Waves 0-6) with zero content an LLM needs while driving a round — same
   character as the runbook/methodology bulk the prior record already flagged, just not
   counted because this file wasn't in the original three-doc tally.

5. **The layers-design doc's last 183 lines (Issue ledger, 1760-1942) are a changelog of
   the document's own revision history** ("The pass that... trued up...") — meta-documentation
   about which edit fixed which stale paragraph, not tuning content.

6. **Of the 3,627 combined lines across `tuning-methodology.md` + these two satellite docs,
   only ~86 lines (2.4%) are things an LLM driving one round would actually consult** — the
   master plan's "Measurement program constants" section alone. The 1942-line layers-design
   doc contributes 0 round-time lines; everything live in it (mic-trust tiers, boost caps,
   the envelope formula) already has a named home in code that a doc citation
   (`linearization_fit.py:18`, `linearization_envelope.py:12`) points back to.

**Could not determine:** whether the "distance rule" conditionals (C-t-C≥1λ→5° tighten,
gate<3ms→0.5m, σ>0.5dB warn) and the boost-probe/stopping-rule numbers are executed by the
LLM as manual arithmetic (consistent with the doc's "LLM judges, code computes" framing) or
are simply unimplemented gaps — deciding this needs the actual operator-runbook procedure
text for these steps (out of this lane's two-file scope) or the owner's intent.
