# D — the shared measurement substrate, sized bottom-up

HEAD `c032503`. Read-only. Every count below is reproducible from the commands
cited or from `scratchpad/bottomup/{prose,fnscan}.py` (written this session).

## 0. What the unit actually is

| slice | files | total lines | code lines | prose | blank |
|---|---|---|---|---|---|
| `jasper/audio_measurement/` | 38 | 31,943 | 15,972 | 12,608 (39.5 %) | 3,363 |
| `active_speaker/` measurement side (23 files) | 23 | 13,222 | 7,948 | 3,491 | 1,506 |
| `correction/{acoustic_quality,peq,target}` | 3 | 1,023 | 596 | 316 | 111 |
| **unit total** | **64** | **46,188** | **24,394** | **16,828 (36.4 %)** | 4,966 |

"code lines" = non-blank, non-docstring, non-comment.

**The prior "~23k of honest DSP in the tree" does not survive contact.** Take
every file in the unit that contains any DSP — `deconv analysis gating
gate_disclosure distortion alignment olive_metrics spatial_combine
interference_nulls snr_policy quality sweep room_boundary comparison_bands
quality_model measurement_geometry null_walk delay_graph frame_fit
timeline_slip program_analysis program flat_spec flat_spec_views
frequency_view acoustic_quality peq target`. That set is **25,587 total lines**
— which is where "23k" came from — but only **11,155 code lines**, and an AST
pass (`fnscan.py`) finds only **6,504 code lines across the whole unit that sit
inside a function which references `np`/`numpy`/`scipy` at all** (5,457 in
`audio_measurement`, 1,047 in the rest). Even that overstates it: those
functions include their record-building and refusal branches. So the honest
math is ~6.5k code lines, not 23k — a 3.5× overcount at the file level and
~2.3× at the code-line level. **Math is not why this unit is 46k lines.**

## 1. Substrate function by function

Current lines = whole modules the function is the primary owner of.
First-principles = a clean numpy implementation *including* its record types,
its refusal/disclosure vocabulary, and docstrings at the AGENTS.md bar
(~12–15 % prose). Assumptions: production-grade for the CLAMP row; one
orchestrator, not three; `dataclasses.asdict` + one canonicalizer instead of
81 hand-written serializers; deletions from §3.

| # | substrate function | implemented today by | now | clean | top reason for the gap |
|---|---|---|---|---|---|
| 1 | **Stimulus generation** — sync ESS (Novak), tone, band-limited noise, speech, multi-segment program schedule + deterministic PCM render | `sweep.py` 350 · `program.py` 2,159 · `test_signal_plan.py` 523 · `topology_tone.py` 244 · `speech_stimulus.py` 232 · `measurement_programs.py` 160 · `tone_plan.py` 42 · sine/noise halves of `am/playback.py` (~350) and `as/playback.py` (~200) | 4,260 | **900** | four independent sine-WAV generators; four program composers that share 70 % of their body |
| 2 | **Playback transaction** — emit an admitted WAV on the fan-in lane, bounded, cleaned up, with a measurement window that pauses music | `am/playback.py` 1,611 · `admitted_playback.py` 654 · `correction_lane.py` 369 · `as/playback.py` 1,245 · `playback_route.py` 395 · `safe_playback.py` 577 · `program_playback.py` 179 · `correction/playback.py` 154 · `correction/coordinator.py` 987 | 6,171 | **750** | three parallel playback stacks (async/WAV-fd, sync/aplay-backends, correction tone) for one act |
| 3 | **Excitation admission (CLAMP)** — closed band∩level ledger, two-boundary fingerprinted artifact, per-segment fan-out | `excitation_admission.py` 820 · `excitation_artifacts.py` 988 · `excitation_safety_plan.py` 1,001 · `program_admission.py` 723 | 3,532 | **900** | the clamp is ~250 lines of arithmetic wrapped in 3,300 lines of evidence ceremony |
| 4 | **Session volume / level** — snapshot, set fixed measurement volume, readback-confirm, durable latch, restore once | `ramp.py` 1,752 · `session_volume_plan.py` 1,291 · `volume_latch.py` 317 · `wired_level_meter.py` 230 (+ adjacent, not mine: `seat_level_ramp` 2,245 · `crossover_level_run` 1,038 · `level_match` 1,082 · `autolevel` 463) | 3,590 | **350** | two superseded generations kept side by side (§4, delta #2) |
| 5 | **Wired USB-mic capture** — registry-anchored device resolve, ALSA record, frame accounting, channel select, zero-run scan, WAV encode, position/provenance | `wired_capture.py` 674 · `mic_identity.py` 206 · `capture_geometry.py` 706 · `capture_provenance.py` 409 · `capture_entry_anchor.py` 143 · `frame_ledger.py` 312 · `frame_fit.py` 361 · `timeline_slip.py` 285 | 3,096 | **800** | three capture-integrity reporters (`wired_capture.py:647`, `frame_ledger.py:288`, `program_analysis.py:2581`); `frame_ledger`/`frame_fit`/`timeline_slip` are 65–70 % prose about a phone chain being deleted |
| 6 | **Calibration** — mic cal parse (3 formats), sign convention, vendor fetch, resample onto grid, SPL | `calibration.py` 1,223 | 1,223 | **350** | one parser per vendor quirk + 233 docstring lines |
| 7 | **Deconvolution → IR** — regularized FFT inverse, length cap, arrival window, harmonic IR extraction | `deconv.py` 403 | 403 | **300** | **essentially none — this module is right-sized** |
| 8 | **Gating + trusted-floor disclosure** — analytic envelope, first-reflection detect w/ prominence, window build, floor arithmetic, `GateDisclosure` + one sentence | `gating.py` 1,128 · `gate_disclosure.py` 585 · `room_boundary.py` 148 | 1,861 | **600** | 55 % prose in `gating`; `room_boundary` is 86 % prose over one dead function |
| 9 | **Smoothing / resample / deviation** — fractional-octave smoothing (already the SSOT), log resample, spatial dB average, deviation metrics, band levels | `analysis.py` 798 | 798 | **450** | mild; one dead 86-line function (§3) |
| 10 | **Distortion separation** — harmonic windows off the sync sweep, phantom floor, per-order magnitude, THD, clearance | `distortion.py` 828 | 828 | **450** | 37 % prose; `HarmonicReading` carries 8 derived properties |
| 11 | **SNR policy** — band levels vs ambient, per-band verdicts by decision class, fallback | `snr_policy.py` 840 | 840 | **350** | 44 % prose; two dead functions (147 lines) |
| 12 | **Alignment / delay** — xcorr, GCC-PHAT (band-limited, parabolic peak), fractional shift, confidence gate, delay-binding proof, null-walk candidate search | `alignment.py` 453 · `delay_graph.py` 734 · `null_walk.py` 860 | 2,047 | **620** | 58 % of `delay_graph` is dead; `null_walk` is 860 lines of "pure decision content" whose runner was deleted (`null_walk.py:5-17`) |
| 13 | **Spatial combine (cloud)** — power-mean onto a shared grid, mean-vs-median interference screen, per-capture echo tau, geometry verdict | `spatial_combine.py` 2,228 · `measurement_geometry.py` 248 | 2,476 | **550** | 56 % prose; 769 code lines for ~200 lines of array work + a refusal record |
| 14 | **Interference-null detection** — which dips are uncorrectable comb nulls | `interference_nulls.py` 1,900 | 1,900 | **450** | 57 % prose (541 doc + 538 comment) over 646 code lines |
| 15 | **FR record: provenance + fingerprint + bank** — one response record, canonical JSON sha256, bundle manifest, archive/list, view projection | `as/measurement.py` 1,792 · `evidence_identity.py` 530 · `bundles.py` 443 · `measurement_document.py` 277 · `measurement_archive.py` 204 · `frequency_view.py` 173 · `measurement_emit.py` 132 | 3,551 | **620** | `as/measurement.py` is a 1,792-line hand-rolled JSON state store with 55 top-level defs; 7 fingerprint re-rolls beside the SSOT |
| 16 | **Flat-spec grader + metrics** — per-band deviation vs tolerance with exclusion mask, power-mean reference, pass/fail; NBD/SM; derived views | `flat_spec.py` 1,288 · `flat_spec_views.py` 1,169 · `olive_metrics.py` 387 · `comparison_bands.py` 183 · `quality_model.py` 202 | 3,229 | **700** | 49 %/48 % prose; 15 dataclasses; `directivity_table` + its 3 record types (~380) have one caller, a `scripts/` renderer |
| 17 | **Capture quality** — clip, DC, dropout, level window | `quality.py` 282 | 282 | **200** | near-none |
| 18 | **Program analysis (phase dispatch)** — `(program, capture) → analysis` for CHECK/MEASURE/VERIFY | `program_analysis.py` 6,572 | 6,572 | **2,070** | see delta #1 |
| 19 | **Room-correction shared** — capture→quality→response, biquad PEQ design, target curve | `acoustic_quality.py` 633 · `peq.py` 308 · `target.py` 82 | 1,023 | **500** | a second `analyze_capture` beside `quality.assess_capture`; ungated per-position loop duplicates §13's grid handling |
| 20 | **Shared helpers + verdict tokens** — `dbfs`, analysis window, canonical JSON, sha256, ~12 verdict spellings | *none — scattered* | 0 | **150** | the absence is why rows 1–19 each re-roll them |
| | **TOTAL** | | **46,188** | **≈12,000** | |

≈12,000 total lines ⇒ ≈10,200 code lines, of which ~5,000 is DSP. That is
consistent with the 6,504 numpy-touching lines measured today: **the math
survives nearly intact and everything around it shrinks ~4×.**

## 2. Bottom-up total and the gap

**46,188 → ≈12,000. Gap ≈ 34,200 lines.** Attributed by primary cause (each
bucket carries its own prose share; the "prose" row is only narration sitting
on code that survives, so nothing is double-counted):

| bucket | lines | what it is |
|---|---|---|
| **legit** | **12,000** | the clean substrate above |
| **prose over the bar** | **5,500** | 16,828 prose lines today vs ~1,800 clean. Constant essays (324 `#:` blocks + `program_analysis.py:1-612`, 540 prose lines for ~20 constants), lab-notebook docstrings (`frame_ledger` 66 %, `correction_lane` 80 %, `room_boundary` 86 %, `quality_model` 77 %, `mic_identity` 70 %, `timeline_slip` 70 %, `frame_fit` 68 %), and `__init__.py` (132 of 136 lines are a hand-maintained `ls` with a CI gate) |
| **duplication** | **9,500** | 3 playback stacks · 4 sine generators (`am/playback.py:1193`, `as/playback.py:629`, `program.py:899`, `web/sound_setup.py:1251`) · 6 volume controllers · 3 capture-integrity reporters · 2 `analyze_capture`s · 7 fingerprint re-rolls · 2 verbatim `dbfs` copies · 10 inlined `np.hanning` sites · 2 deliberately-duplicated locate constants pinned equal by a test (`program_analysis.py:168-173`) |
| **over-abstraction** | **13,800** | 124 frozen dataclasses = 4,213 code lines, of which 81 hand-written `to_dict`/`from_dict` = 1,174; evidence ceremony (two-boundary admission artifacts, `evidence_identity`, `bundles`, `capture_provenance`, `capture_entry_anchor`); `program_analysis`'s 736-line dispatch band and 438-line `_build_candidate`; `as/measurement.py`'s 1,792-line JSON store |
| **dead halves** | **1,900** | `delay_graph` dead half 425 · `snr_policy.sweep_excitation_bands`/`cap_null_depth_db` 147 · `level_solver.py` 48 (self-declared) · `analysis.before_after_fill_segments` 86 (only a docstring reference survives, `correction/envelope.py:39`) · `flat_spec_views.directivity_table` + 3 record types ~380 (only caller `scripts/render-metric-views.py`) · `program.courtesy_beep_to_stimulus_gap_s` 38 · `room_boundary.room_boundary_hz` 21 · `excitation_artifacts` historical pair 24 · `excitation.py` 13 · plus the prose riding with them |
| **speculative / parked** | **3,488** | `safe_playback.py` 577 — "*Safety-session substrate for **future** active-speaker test tones… deliberately does not generate audio*" (`:5-10`), yet live and load-bearing, so it is a stale contract not dead code · `null_walk.py` 860 (decision content whose runner was deleted) · `ramp.py`'s phone-relay half · the `#:` PROVISIONAL constant apparatus |

Tests are outside this number: `tests/test_audio_measurement*.py` alone is
**24,077 lines across 25 files**, plus ~8,700 for the `active_speaker`
measurement side — a 1.4:1 test-to-code ratio over a substrate whose clean
form is 12k.

## 3. The three biggest single deltas

### 1. `program_analysis.py` — 6,572 → ~2,070 (−4,500)

It is one file at five altitudes. Measured band by band (total / doc / comment
/ blank / **code**):

| band | section | lines | code |
|---|---|---|---|
| 1–612 | imports + constants + their essays | 612 | **160** (397 comment lines) |
| 613–842 | crest factor + more constants | 230 | 37 |
| 843–1,632 | 16 dataclasses | 790 | 233 |
| 1,635–2,728 | signal helpers, locate/anchor, drift, frame integrity | 1,094 | 591 |
| 2,729–4,099 | per-driver response, alignment, candidate | 1,371 | **630** ← the real DSP |
| 4,100–5,027 | CHECK helpers (pilot/ambient/channel-map/gain solve) | 928 | 412 |
| 5,028–6,232 | phase dispatch + `_build_candidate` (438) | 1,205 | **736** ← orchestration |
| 6,233–6,572 | `analysis_diagnostic_summary` for `web/correction_crossover_v2` | 340 | 194 ← disclosure text |

**Analysis ≈ 1,500 code lines. Orchestration 736. Disclosure text 194. Record
types 233. Constant essays 197 code lines carrying 540 prose lines.**
`program_analysis.py:150-230` spends **80 lines to declare four constants**,
including a measured-population justification of the number 50, a
"PROVISIONAL" note naming the fields a future field study would count, and an
explicit statement that `SWEEP_LOCATE_CONFIDENCE_FLOOR` is *"duplicated rather
than imported"* from `crossover_v2.capture_dispatch` and pinned equal by
`tests/test_measurement_integrity_floor_contracts.py`. That is the file's
whole pathology in one screen: real judgment, narrated at 20:1, then
duplicated on purpose and defended by a test.

### 2. The volume/level layer — 3,590 in my unit (8,418 with neighbours) → ~350 (−3,240)

Six live modules for "set the measurement volume safely and put it back", and
two of them say in their own docstrings that they superseded the others:

- `session_volume_plan.py:5-9` — *"replaces the per-step ramp/lock machinery
  with ONE fixed measurement volume held for the whole session"* — 1,291 lines.
- `ramp.py:7` — *"This is the P2 generalization of `jasper.correction.autolevel`"*
  — 1,752 lines. `jasper/correction/autolevel.py` still exists at 463 lines and
  is still imported by `correction/session.py:78`.
- Plus `volume_latch.py` 317, `wired_level_meter.py` 230, and outside my unit
  `seat_level_ramp.py` 2,245, `crossover_level_run.py` 1,038,
  `level_match.py` 1,082.

Neither generation was retired. The wired path knows the level it played, so
the settle-based search that `ramp.py` exists for is only needed on the
phone-relay path that PR #3724's stack is removing. First principles: a durable
latch written before the first mutation, set-and-confirm through an independent
readback, restore once — **~350 lines, one module.**

### 3. The playback + admission stack — 9,703 → ~1,650 (−8,050)

Three orchestrations of one act, over one clamp:

- async/WAV-fd: `am/playback.py` 1,611 (7 `wave.open`/`struct.pack` sites,
  `ensure_sine_wav:1136`, `ensure_bandlimited_noise_wav:1289`, `TonePlayer:1418`)
  + `admitted_playback.py` 654 + `correction_lane.py` 369.
- sync/aplay-backends: `as/playback.py` 1,245 (three backends, one of which
  *"never emits audio"*, `_tone_sample:612`, `_write_multichannel_wav:633`,
  artifact pruning) + `playback_route.py` 395 + `program_playback.py` 179.
- correction tone: `correction/playback.py` 154 + `coordinator.py` 987, whose
  `measurement_window()` is a system-wide facility mis-homed in a feature package.

Underneath, the CLAMP itself is 3,532 lines across `excitation_admission`,
`excitation_artifacts`, `excitation_safety_plan` and `program_admission` — a
band∩band intersection and a level ceiling, wrapped in two-boundary
fingerprinted artifacts with their own canonical-JSON and sha256 re-rolls
(`excitation_artifacts.py:233,239`; `excitation_admission.py:84,97`) beside
`evidence_identity.json_fingerprint:78`. The clamp must stay production-grade;
production-grade for this contract is ~900 lines, not 3,532.

## 4. Uncertainty

- The clean estimates are per-function judgments, not a written prototype. The
  DSP rows (7, 9, 10, 12 core, 13, 14) I hold to ±15 %; rows 15, 18 and the
  admission row are ±30 % because how much fingerprinted evidence the owner
  wants is a policy choice, not a derivation.
- `fnscan.py` attributes a whole function to "numpy" if it references `np`
  once, so 6,504 is an **upper** bound on honest math, not a lower one.
- I did not read PR #3724's branch. If the phone-relay capture path is gone,
  `ramp.py`, `frame_fit.py`, `timeline_slip.py` and much of `frame_ledger.py`
  lose their reason to exist and my row-4/row-5 clean numbers are generous.
- Dead-half claims in §2 reuse the prior recon's AST reachability pass plus my
  own greps; each still needs an exact-symbol grep before deletion.
