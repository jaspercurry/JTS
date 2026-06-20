# Handoff: shared audio measurement & calibration core

> **Status: living architecture & product plan, created 2026-06-19.**
> This doc owns the cross-cutting plan for turning JTS's audio
> measurement/DSP/calibration work into ONE shared core that three
> consumers build on — **room correction**, **active-crossover
> calibration**, and **pair/leader-follower balance & sync** — plus the
> layered calibration *product* (L0/L1/L2) and a regression-safe refactor
> roadmap. It is the **output/measurement-side sibling** of
> [HANDOFF-audio-capability-platform.md](HANDOFF-audio-capability-platform.md)
> (which owns the *input* side: mic/AEC/DAC hardware capability). Backing
> safety/DSP contracts stay canonical in
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md),
> [HANDOFF-correction.md](HANDOFF-correction.md), and
> [HANDOFF-volume.md](HANDOFF-volume.md). Research backing:
> [docs/research/2026-06-19-active-crossover-calibration/](research/2026-06-19-active-crossover-calibration/README.md).

---

## TL;DR

The audio subsystem is the heart of JTS and should become **one clean,
resilient measurement/calibration core** that distinct consumers ride as
thin adapters — not three parallel stacks. The good news from the
2026-06-19 audit: **this is mostly consolidation + wiring, not a
ground-up build.** The room-correction measurement pipeline is
production-grade and *already* reused by the others (active-speaker's
`driver_acoustics.py` imports `jasper.correction.{sweep,deconv,analysis,quality}`;
`balance_flow.py`/`sync_flow.py` import `correction/coordinator.py`'s
`measurement_window`). The work is to (1) formalize that shared core,
(2) kill the duplicated graph-safety parsing, (3) close the
already-built-but-unwired active-speaker measurement loop, and (4) ship a
layered calibration product anyone can use.

The product is three tiers:

- **L0 — the crossover is actually applied, fail-closed.** Foundational.
  On the JTS3 lab Pi today it is **not** (the live CamillaDSP graph is a
  flat passthrough — see "Current state"). This is the real cause of the
  "shrill / horn far too powerful" symptom, and the first thing to fix.
- **L1 — phone-mic woofer↔tweeter level matching.** No special hardware.
  Relative level is a ratio measurement; an uncalibrated phone mic is good
  enough (±3–6 dB) with guardrails. One fixed trim, measured once.
- **L2 — calibrated-mic FR / phase / null-depth.** Optional, for users
  with a measurement mic (the maintainer's Dayton USB-C). Reuses the
  existing `correction/calibration.py` upload path. This is active-speaker
  commissioning **Stage 6 (sweep+measure)** and **Stage 7 (freeze)**.

---

## Current state (verified against the worktree & `origin/main`, 2026-06-19)

### What exists and is production-grade
- **Room-correction measurement kernel** (`jasper/correction/`): `sweep.py`
  (Novak ESS), `deconv.py` (FFT/Tikhonov IR), `analysis.py` (octave
  smoothing, log resample, band normalize), `quality.py`/`acoustic_quality.py`
  (SNR/clipping gates), `confidence.py`, `calibration.py` (Dayton/miniDSP/UMIK
  lookup + upload), `coordinator.py` (`measurement_window`: pauses renderers
  + voice, serializes), `session.py` (`MeasurementSession` state machine),
  `bundles.py` (schema-versioned durable evidence). Shipped, tested.
- **Shared browser-mic capture**: `deploy/assets/shared/js/measurement-audio.js`
  (mono 48 kHz, AGC/EC/NS hard-coded off) + `correction/browser_audio.py`.
- **Active-speaker subsystem** (`jasper/active_speaker/`, 32 files on
  `origin/main`): commissioning stages 1–5 (muted load → per-driver unmute
  at a floor → audible gain ramp → audible-evidence confirmation), driver
  research/preset, `camilla_yaml.py` per-driver Gain/Crossover emit,
  `driver_protection.py`, `safe_playback.py`, runtime contract & staging.

### What is already shared (the core is partly emergent)
- `active_speaker/driver_acoustics.py` **imports** `correction.sweep`,
  `correction.deconv`, `correction.analysis`, `correction.quality`
  (lines ~196, ~232–233) — it reuses the room-correction DSP verbatim.
- `web/balance_flow.py` + `web/sync_flow.py` **import** `measurement_window`
  and gate on `_reserve_start_slot` mutual exclusion.
- `commissioning_capture.py` accepts a calibration flag and routes to the
  same analysis. Formalizing a "core" mostly *names* a dependency that's
  already there — that's why the refactor is low-risk.

### The gaps (worktree-confirmed)
- **Active-speaker measurement loop is built but UNWIRED.**
  `driver_acoustics.py` (`analyze_driver_capture`, `analyze_summed_crossover`)
  and `commissioning_capture.py` (`record_driver_acoustic_capture`) are
  fully coded and unit-tested, but referenced **only** by themselves,
  `__init__.py`, and tests — **no production caller** (`commissioning.py`,
  `measurement.py`, a web endpoint, or any daemon) invokes them. The
  acoustic-verdict field stays `None` in production records.
- **`DriverSpec.sensitivity_db` is stored but never read to set gain.**
  Per-driver gain comes only from a caller-supplied corrections dict,
  default 0 dB. There is no sensitivity-delta → trim path. (This is why a
  ~25 dB woofer/tweeter sensitivity gap is not auto-compensated.)
- **Duplicated graph-safety parsing.** The same CamillaDSP-graph
  invariants (per-output commission mute at −120 dB + wired; tweeter
  outputs wrapped by protective HP + limiter; fail-closed on parse error)
  are re-implemented across `runtime_contract.py` (`_commission_mutes`,
  `_pipeline_contains`, `_filter_params`, …) and `staging.py`
  (`_parse_generated_filters`, `_pipeline_contains_chain`,
  `_running_filter_matches`, plus three functions —
  `_all_commission_mutes_engaged`, `_software_guard_evidence`,
  `driver_commission_audible_evidence` — that each re-parse), with a live
  read-back variant too. ≈4 parallel paths. (Matches the prior staff
  review's P1.)
- **Active-speaker commissioning does not use `measurement_window`** — it
  drives CamillaDSP load/unload directly, so it isn't serialized against
  room correction / balance / sync.
- **Confidence/quality thresholds are hard-coded per domain** (room:
  `acoustic_quality.py`; driver: `driver_acoustics.py`
  `SILENT_PEAK_DBFS=-45`, `NULL_THRESHOLD_DB=6`, …) with no shared,
  parameterized model.
- **Evidence durability is inconsistent**: correction has schema-versioned
  per-session bundles; active-speaker uses one global JSON state file;
  balance/sync don't persist bundles (lost on restart).
- **JTS3 lab Pi (2026-06-19): the crossover is not live.** Output HW is a
  HiFiBerry DAC8x (8 outputs); the live CamillaDSP graph (`v1.yml` and the
  outputd `outputd-cutover.yml`) is a **flat identity passthrough** — no
  crossover, no per-driver trim. With a B&C DE250-8 compression driver
  (~108.5 dB) ~**25 dB hotter** than the Epique E150HE-44 woofer
  (~83.3 dB), full-range equal-level audio = shrill/horn-dominant, and a
  tweeter-safety risk. This is the L0 failure made concrete.

> **Provenance note.** A design workflow's adversarial verifier inspected
> the *main checkout* (`/Users/jaspercurry/Code/JTS`), which was parked on
> a sibling session's branch lacking `jasper/active_speaker/`, and wrongly
> concluded the subsystem was "unbuilt." All "what exists" claims here
> were re-verified against this worktree and `origin/main` (32 files).
> Future automated audits: pin paths to the working tree / `origin/main`,
> not whatever branch the shared main checkout happens to be on.

---

## Two settled questions (full reasoning in the research snapshot)

1. **"Is it just level matching?" — Half.** Broadband level fixes
   "shouty/shrill" (tweeter too hot). "Nasal/honky" (~300 Hz–2 kHz) is a
   midrange/baffle-step or crossover-region problem that a trim won't fix.
   An LLM-designed crossover from datasheets won't have modeled the baffle.
2. **"Calibrated mic vs iPhone?" — Uncalibrated phone is fine for level
   matching, not for phase/FR.** Relative level is a ratio at one mic
   position; mic + room cancel in the crossover overlap band (±3–6 dB).
   Guardrails: AGC/EC/NS off (already enforced), fixed position, compare in
   the overlap band, average several captures. Calibrated mic required for
   FR/phase/null-depth (uncalibrated phase error ±20–40° at Fc).

## Multi-volume verdict (settled)

Woofer↔tweeter level matching is **ONE fixed trim, level-INDEPENDENT** in
the drivers' linear region — measured once at a 75–85 dB reference. **Do
not build per-volume level curves.** Perceived tonal change with volume is
**loudness compensation** (ISO 226 / Fletcher-Munson) — a *separate,
optional* feature (the Audyssey MultEQ-vs-Dynamic-EQ split), absent today
and out of scope for commissioning. Keep them orthogonal.

---

## Target architecture

**Pattern: functional core + imperative shell + adapter ports, reached via
strangler-fig extraction.** (Deliberately *not* a grand `MeasurementCore`
Protocol — the consumers differ enough — room: modal 20–350 Hz; crossover:
full-range per-driver; balance: level+time — that premature abstraction
would overfit. Honor "don't abstract before the second real instance"; we
have the second instance, so a thin shared kernel is justified.)

```
        ┌──────────────── Measurement Orchestrator (shell) ───────────────┐
        │  MeasurementSession lifecycle + measurement_window() + slot lock │
        │  pluggable MeasurementReporter callback per consumer             │
        └───────┬───────────────────┬──────────────────────┬──────────────┘
   CorrectionAdapter        CrossoverAdapter           BalanceAdapter
     (shipped)              (wire Stage 6/7)           (level + sync)
        └───────────────────────┼──────────────────────────┘
              ┌─────────────── Measurement Kernel (pure) ───────────────┐
              │ sweep · deconv · analysis · quality · calibration ·      │
              │ evidence(bundles) · QualityModel(params per consumer)    │
              └───────────────────────────┬──────────────────────────────┘
                            GraphValidator (single, fail-closed)
                                          │
                                  camilla_yaml emit  →  CamillaDSP / outputd
```

**Core OWNS** (move/extract, mostly from `jasper/correction/`):
- Signal gen (`sweep`), deconvolution (`deconv`), FR analysis (`analysis`).
- A **parameterized `QualityModel`** (room_response vs driver_presence vs
  level_ramp thresholds) instead of hard-coded per-module constants.
- Mic calibration lookup/upload (`calibration.py`) as a `CalibratedMicProvider`.
- Durable, schema-versioned **evidence bundles** (extend to tag
  `consumer_id` / `measurement_type` / `kernel_version`).
- **`measurement_window` + a single mutual-exclusion slot registry** that
  *all* consumers (including active-speaker commissioning) register with.
- **One graph-safety module** (`jasper/active_speaker/graph_safety.py` —
  kept in `active_speaker` for now since it's the only consumer; promote to
  a top-level shared module when balance/sync need it. NB `jasper/camilla.py`
  already exists, so `jasper/camilla/` as a package would collide).
  **Design: normalize-then-predicate, NOT "one parser."** A 2026-06-19 read
  of the code found the ≈4 paths parse YAML *three legitimately different
  ways*, by design: (a) `staging.py` hand-rolls a **line/text parser** over
  the JTS-emitted config — this doubles as an *emitter-format-drift guard*;
  (b) `staging.py`'s live check uses `yaml.safe_load` because CamillaDSP
  re-serializes the running graph in its own dialect (block lists, `channel:`
  scalar sugar, reordered keys) the text parser can't read (see
  staging.py:780–788); (c) `runtime_contract.py` uses `yaml.safe_load` for
  candidate-graph classification. Forcing one parser would change what's
  accepted/rejected and weaken the drift guard. So the module owns: one
  normalized `GraphView` (`filters: {name→{type,parameters}}`,
  `pipeline_steps: [{channels:set, names:[]}]`); three thin **adapters**
  (`view_from_emitted_text`, `view_from_camilla_dict`, `view_from_yaml_text`)
  that preserve each source's parsing semantics; and the shared **predicates**
  (`output_hard_muted_and_wired`, `output_unmuted_and_wired`,
  `tweeter_guard_present`, `startup_headroom_ok`, …), fail-closed. The ≈4
  callers keep their parser choice but call the shared predicates — killing
  the duplicated *logic* without changing behavior. Wire the predicates at
  the `camilla_yaml` emit gate too, so an unsafe graph can't reach disk.

**Consumer-specific (stays in adapters):** room target curves +
multi-position averaging + PEQ design; active-speaker role assignment,
per-driver sweep routing, crossover/trim, stage-gate ladder; balance/sync
leader ownership, per-speaker trim / Delay + Snapcast latency.

**Naming:** core module `jasper/audio_measurement/` (or
`jasper/audio_core/`); safety `jasper/camilla/graph_safety.py`. Decide in
the decision points below.

---

## Layered product spec

| Tier | Audience | What it does | Reuses | New |
|---|---|---|---|---|
| **L0** | everyone (implicit) | Designed crossover + protective HP **applied, fail-closed**; flat-graph-with-tweeter-role is illegal | `GraphValidator`, outputd graph | wire the validator at the emit gate; make commission cut-over actually apply |
| **L1** | anyone, phone only | Per-driver level match: play band-limited tone/sweep per driver through the production graph, capture phone mic, compute overlap-band dB delta → fixed trim, propose + confirm + apply; `measurement_mode=magnitude_only` so it can never authorize a phase/delay decision | sweep/deconv/analysis/quality, `measurement_window`, browser-mic | trim algorithm; Stage-6 endpoint+UI; sensitivity-fallback when skipped |
| **L1.5** | optional | Loudness compensation (ISO 226) as a *separate* volume-dependent EQ layer | — | separate feature, default off; **not** part of commissioning |
| **L2** | enthusiasts w/ calibrated mic | FR + phase + null-depth; per-driver EQ; crossover blend/polarity/delay | `calibration.py` upload, full deconv pipeline, `phase_aware` mode | null-depth capture; delay/polarity search gated on `phase_aware` |

**Fail-closed default:** if L1 capture is low-SNR or aborts, fall back to
datasheet sensitivity (or a conservative tweeter trim) and mark the config
**provisional** in `/state` + UI; never emit a graph that sends full-level
signal to a compression driver.

---

## Refactor roadmap (strangler-fig, regression-safe)

Each phase keeps the **room-correction test suite green as the regression
gate**; no big-bang. "Extract/move" ≠ "net-new".

| Phase | Scope | Size | Net-new? | Done when |
|---|---|---|---|---|
| **0. Spike** | ~150-line CLI: route a band-limited sweep to one driver through the production graph → capture via existing pipeline → print proposed trim | ~1 day | net-new (throwaway) | a real "tweeter +25 dB" number from JTS3 hardware |
| **1. GraphValidator** | Extract one `graph_safety.GraphValidator`; call it at the `camilla_yaml` emit gate; replace the ≈4 parsers; add `test_graph_validator_rejects_flat_with_tweeter_role` | M | extract + 1 net-new gate | parsers deduped, all old safety tests pass, flat-with-tweeter is rejected (fixes JTS3 L0) |
| **2. Kernel extraction** | Move pure `sweep/deconv/analysis/quality` into `jasper/audio_measurement/`; wrap with characterization tests (pass unchanged); add parameterized `QualityModel` | M | extract | correction + active-speaker import the kernel; behavior identical |
| **3. Close Stage 6** | Wire `commissioning_capture` into a production caller + `/sound/` UI card; read `DriverSpec.sensitivity_db` → propose per-driver trim; register commissioning into `measurement_window`; `measurement_mode` enum | L | net-new wiring | L0+L1 ship: a user level-matches a 2-way and hears it; trim persists + re-freezes |
| **4. Balance/sync as 3rd consumer** | Reuse the kernel + bundles for pair level-match (and Delay/Snapcast for sync); persist durable bundles | M | net-new adapter | leader-measured pair balance rides the core with no forked DSP |

**Progress (2026-06-19):** Phase 1 slice 1 landed (additive, no caller
changes): `jasper/active_speaker/graph_safety.py` — the leaf module
(normalized `GraphView` + two adapters `view_from_emitted_text` /
`view_from_camilla_dict` + shared fail-closed predicates `filter_param_matches`
/ `pipeline_contains_chain` / `output_hard_muted_and_wired` /
`output_unmuted_and_wired`) with `tests/test_active_speaker_graph_safety.py`.
The candidate/unknown-graph adapter (`view_from_yaml_text`) and a tweeter-guard
predicate are intentionally NOT pre-built — they land in slice 2b, driven by
`runtime_contract`'s real needs (its `<=`-clip / order≥2 / soft_clip policy and
its two parse-error issue codes), per "don't abstract before the second real
instance."

Phase 1 slice 2a landed: `staging.py`'s `_all_commission_mutes_engaged`,
`_software_guard_evidence`, `driver_commission_audible_evidence`, and
`running_commission_evidence` now call the shared predicates; the duplicated
emitted-text + running-graph parser/predicate cluster (~150 lines:
`_parse_generated_filters`/`_parse_generated_pipeline_filters`/
`_filter_param_matches`/`_pipeline_contains_chain`/`_float_matches`/the
`_parse_scalar`/`_parse_inline_*`/`_top_level_sections` text helpers/the
`_running_*` helpers) is deleted. Behavior-preserving.

An adversarial staff review (2026-06-19) then tightened slice 1: removed the
speculative `tweeter_guard_present` / `view_from_yaml_text` / orphaned helpers
(deferred to 2b), wired the staging mask loops to `output_hard_muted_and_wired`
/ `output_unmuted_and_wired` so every predicate has a real caller, and
documented + tested the intentional bool-channel / None-name parse hardening
(uniform across both adapters; the protective direction). Ruff clean;
active-speaker suite green (390 passed — the −3 vs 393 is the retired
speculative-predicate unit tests). **Owed before PR:** land on a branch cut
fresh from `origin/main` — this worktree branch carries unrelated prior commits
(`staging.py` +197 vs `origin/main`), so the slice must be recreated there to
PR cleanly.

Phase 1 slice 2b landed: `runtime_contract.py`'s `_active_graph_evidence` now
builds the shared `GraphView` via the new `view_from_yaml_text` adapter (a
list-only `yaml.safe_load` reader — no scalar `channel: N` sugar, mirroring the
deleted `_pipeline_contains`) and proves its invariants through the shared
predicates (`pipeline_contains_chain`, `filter_param_matches`, and a new
`tweeter_guard_present` carrying runtime_contract's LOOSE policy: any positive
Fc, order ≥ 2, soft_clip, clip ≤ ceiling — separate from staging's exact-match
guard, which is untouched). The duplicated local cluster
(`_safe_load_yaml`/`_pipeline_contains`/`_commission_mutes`/
`_commission_mute_gain_ok`) is deleted; the commission-mute scan keeps its
runtime_contract-specific `as_out{N}_commission_mute` name pattern but reads
`GraphView.filters`. Behavior-preserving: the granular issue codes and the two
distinct parse-error codes (`camilla_yaml_unparseable` vs
`camilla_yaml_not_object`) are preserved — the latter via a local parse, since
`view_from_yaml_text` collapses both to `parsed_ok=False`. Ruff clean; full
suite green (6539 passed). The baseline-path filter accessors stay on
`graph_evidence` (the names+scalar-accessor module that overlaps `graph_safety`;
their reconcile is its own follow-up).

Phase 1 slice 3 landed (the L0 program-graph gate): a flat full-range program
graph can no longer go live (emitted *or* loaded) to the DAC while the saved
topology assigns a protected tweeter role. The shared judgement is the topology
predicate `runtime_contract.flat_program_graph_blocked_reason()` — the program
lane is structurally a 2-channel passthrough, so the only question is whether the
topology has a tweeter to protect; fail-closed on a corrupt/unreadable topology.
The refuse POLICY lives at each caller's boundary, **never** on the shared
`emit_sound_config` leaf: the `/sound` graph-carrier (`_StereoHostCarrier`) reads
it at construction so `can_host_eq` is `False` (the durable pre-check refuses
early, no spurious `prepare_failed`) and re-asserts in `reemit`, so BOTH the
live-draft SetConfig path and the durable write refuse with the existing typed
`CarrierCannotHostEq("flat_graph_protected_tweeter", …)` → honest blocked-200;
room correction's direct emit gates via
`correction.runtime_safety.assert_flat_apply_safe` (the sweep entry already
blocks measuring on a roleful topology — this is the measure-then-reassign
backstop); the multiroom solo-restore emit stays deliberately lenient
(un-bonding must always succeed). No-op for full-range / mono / subwoofer /
unconfigured topologies. (An earlier cut wired the gate inside
`emit_sound_config` itself with an inline `graph_safety`-predicate check —
[#871](https://github.com/jaspercurry/JTS/pull/871); a staff review found the
leaf placement missed the live-draft SetConfig path, raised a
non-`CarrierCannotHostEq` type the `/sound` route couldn't map to an honest
blocked-200, and broke the multiroom never-refuse invariant — so the gate moved
to the caller boundaries, reusing `CarrierCannotHostEq`.) Contract doc updated:
[HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md). On-Pi (jts3) status
(2026-06-20): the refusal LOGIC is validated on jts3's real topology
(`active_mono_2way`, tweeter @ DAC output 2), running the merged code on-device
(non-destructively, via a temp tree — not deployed): the verdict blocks a flat
program graph, the stereo-host carrier refuses the live-draft path
(`can_host_eq=False` + `CarrierCannotHostEq("flat_graph_protected_tweeter")`),
correction apply refuses, multiroom solo-restore stays lenient, and the live
active baseline still resolves to the active carrier (unaffected). STILL OWED:
the full DEPLOYED HTTP end-to-end (a real `/sound` request returning
blocked-200), which requires jts3 to actually be in the flat-graph state — not
induced on a wired compression tweeter, since that is the hazard the gate
prevents; confirm opportunistically when jts3 is transiently flat under the
tweeter topology (e.g. right after a fresh topology assignment, before the
active graph is staged), and that un-bonding still succeeds.

**Next slice (Phase 2 — kernel extraction):** move pure `sweep`/`deconv`/
`analysis`/`quality` into `jasper/audio_measurement/` behind characterization
tests; add the parameterized `QualityModel`. `runtime_contract` remains the
proven graph-safety re-use pattern.
NB: a worktree may have no `.venv`; run tests as
`PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/python -m pytest …`
so `import jasper` resolves to the worktree, not the main checkout.

**Smallest valuable first step:** Phase 1 (GraphValidator) — it both kills
the P1 duplication *and* fixes the JTS3 L0 hole (a flat graph can no longer
go live when a tweeter role is assigned). Phase 0 spike can run in parallel
to de-risk Phase 3.

---

## Decision points (need maintainer input)

1. **Sequence: foundation-first vs feature-first.** Recommend
   **foundation-first** — Phase 1 (GraphValidator/L0) then Phase 3 (L1),
   because L0 is a live safety/correctness hole on JTS3. (Alternative: ship
   L1 first for momentum; riskier given the flat-graph state.)
2. **Refactor aggressiveness.** Recommend the **incremental strangler-fig**
   (extract kernel, leave adapters in place) over a sweeping reorg —
   matches "don't over-abstract," keeps the regression suite meaningful.
3. **Module placement/naming.** `jasper/audio_measurement/` (core) +
   `jasper/camilla/graph_safety.py` (validator). Confirm or adjust.
4. **L1 launch scope.** Recommend **uncalibrated-only** at L1 launch with an
   honest "±3–6 dB, gross balance" disclaimer; L2 calibrated path follows.

---

## Risks & what to verify on hardware

- **Kernel extraction must preserve load-bearing contracts** (deconv
  regularization constant + peak window; `analysis` return dtypes;
  `measurement_window` pause/restore protocol; `camilla_yaml` emit shape;
  `percent_to_db` mapping; the 0 dB `volume_limit` ceiling). Pin with
  characterization tests *before* moving code.
- **iPhone/Android AGC** actually honoring `autoGainControl:false` — capture
  a constant tone, confirm RMS flat ±2 dB on ≥2 iOS + 2 Android devices.
- **Protective HP** not skewing the tweeter passband vs the deployed config.
- **Null-depth repeatability** on JTS3's DAC8x (≥5 captures, variance <2 dB)
  before trusting any `phase_aware` delay step.
- **DAC8x clock coherence** for the chip-AEC reference path (separate, but
  shares the hardware).

---

Last verified: 2026-06-20
