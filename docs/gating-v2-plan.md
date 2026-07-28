# Gating v2 — work order (issue #1790)

> **Status: adopted work order (2026-07-28).** Synthesized from the
> owner-run deep-research result (verbatim:
> [`docs/research/2026-07-27-acoustics-round-2/01-gating-v2.md`](research/2026-07-27-acoustics-round-2/01-gating-v2.md);
> prompt and laptop-side evidence in `captures/gate-research-20260727/`)
> against the code as verified on 2026-07-28. Anchors
> [issue #1790](https://github.com/jaspercurry/JTS/issues/1790).
> Composes with the room-correction regime work order (issue #1791):
> the group validity floor this plan redefines is the layer boundary
> that plan consumes.

## Why (the incident, in one paragraph)

In a 10-position cloud session, nine captures gated at the 7 ms search
ceiling (floor 142.9 Hz) and one (`cloud_04`) "found" a reflection at
~0.56 ms — 3 samples past the search-start offset — collapsing its gate
to 27 samples (floor 1777.8 Hz). The group floor is
`max()` across positions (`cloud_validity_floor_hz`,
[`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)),
so one capture's false detection removed 143–1778 Hz from spec grading,
re-centred the reference −27.267 → −28.317 dB, moved the headline
`max_db` +1.05 dB in the flattering direction, and flipped the
250 Hz–2 kHz band verdict fail→pass. The measure path kept the
collapsed capture; nothing retook, excluded, or even annotated it.
`assemble_cloud_group_result`'s docstring records this forensically and
queues per-position masking as the named deferred fix — this plan is
that fix, plus the detector and policy work around it.

## Current state (verified against code 2026-07-28 — the review bar)

- Detector: `detect_first_reflection` in
  [`jasper/audio_measurement/gating.py`](../jasper/audio_measurement/gating.py)
  — moving-RMS envelope (0.20 ms kernel) threshold-with-hysteresis:
  envelope must drop below `peak − 12 dB` (`REFLECTION_THRESHOLD_DB`)
  then rise back above it, searched in `[direct+0.5 ms, direct+7 ms]`
  (`SEARCH_T_MIN_MS`/`SEARCH_T_MAX_MS`). This is the STA/LTA-class
  energy picker whose spike false-trigger mode is the incident.
- Window: rectangular + half-Hann tail, `TAPER_FRACTION = 0.25`,
  `WINDOW_KIND = "half_hann_tail"`, `GATING_SCHEMA_VERSION = 1`.
- Floor: `f_valid_floor_hz(window_s) = 1/T`; provenance vocabulary
  `FLOOR_MEASURED` / `FLOOR_SEARCH_BOUND` / `NEAR_FIELD_EXEMPT` exists,
  but **no consumer reads it for trust** — a 27-sample
  `FLOOR_MEASURED` is as authoritative as a 7 ms one. There is no
  minimum-gate guard anywhere; `SEARCH_T_MIN_MS` is a search offset,
  not a validity assertion.
- Advisory band: `NEAR_FLOOR_RATIO = 1.25` marks
  `[floor, 1.25·floor)` "near_validity_floor" (reduced confidence,
  non-excluding).
- Group aggregation: `cloud_validity_floor_hz` = `max(usable floors)`;
  single call site in `_close_cloud_group`; the floor clamps the spec
  mask in `assemble_cloud_group_result` (deliberately NOT
  `merged_excluded_bands_hz` — gate artifacts must not masquerade as
  room combing; keep that separation).
- Verify comparability: `_verify_verdict` refuses (`inconclusive`,
  `REASON_VERIFY_INCONCLUSIVE`) when the VERIFY gate is shorter than
  the MEASURE gate by **any** margin (strict inequality + 1e-6 ms
  epsilon). Cloud groups are deliberately exempt (a cloud position's
  gate legitimately varies with the mic's nearest boundary — "that is
  the measurement, not a defect").
- Repeats: `MEASURE_REPEAT_COUNT = 3` exists **only at the anchor
  MEASURE position** (`build_measure_program`). Every cloud position is
  a single mono summed sweep (`build_verify_program`, no repeat
  parameter). Within-position repeatability data does not exist at
  cloud positions, and the WAV size cap
  (`CROSSOVER_CAPTURE_MAX_WAV_BYTES`, ~1.2 MB headroom) binds adding
  it.
- Retake machinery (U-ladder, shipped): `parse_begin_capture` admits a
  bounded retake of the last accepted slot; host-side precedent is the
  geometry-locked retake (`GEOMETRY_RETRY_POSITIONS = 2`, prompt rungs
  in `CLOUD_GEOMETRY_RETRY_PROMPTS`, budget beside
  `CLOUD_RETAKE_ALLOWANCE`), driven by returning a rejecting
  `PhaseVerdict` from the group consume path. A gate-anomaly retake
  rides the same lever; no relay/spec change needed.
- τ machinery (shipped, compose don't duplicate):
  `spatial_combine.detect_echo` (cepstrum + band-limited envelope,
  refusal-not-clamp window contract), `usable_echo_estimates`,
  `assess_geometry` (τ-cluster GEOMETRY_LOCKED/DISPERSED/UNKNOWN), and
  `interference_nulls.identify_interference_nulls` (single-τ ladder
  admission, position-invariant/-dependent classification). Error
  discipline both share and gating v2 must match: malformed *config*
  raises `ValueError`; malformed *data* returns a named refusal.
- Two representations of one quantity: `_measure_gate` (min of driver
  windows) and `_measure_validity_floor_hz` (max of driver floors) are
  a reciprocal pair computed independently.

## Adopted decisions

Each is tagged with its delta from the research recommendation where
code reality forced one.

**D1 — Group aggregation: per-position per-bin masking + MIN_POS.
ADOPT (research Q3, and the codebase's own queued fix).**
Inside `spatial_combine.combine_positions`, mask each position's
contribution below that position's own `1/Tᵢ` before power-averaging;
a bin is graded only where at least
`MIN_POS = max(3, ceil(M/2))` positions contribute (M = positions
entering the combine after existing screens). Below that, the bin is
disclosed `insufficient_spatial_support` — not silently averaged, not
silently dropped. The group's `validity_floor_hz` payload key is kept
for every existing consumer and becomes the **support-derived floor**:
the lowest frequency at which per-bin support ≥ MIN_POS. Per-bin
support (`m(f)`) and each masked position's identity+floor ship in the
payload as disclosure. This preserves the `max()` docstring's guarantee
*by construction, per-bin* — every graded bin is inside every
**contributing** capture's validity — while removing the single-point
poisoning. The spec-mask vs `merged_excluded_bands_hz` separation is
unchanged. `GATING_SCHEMA_VERSION`-adjacent: the combined-curve
semantics change, so the combine result carries a
`combine_masking: "per_position_v2"` provenance marker; cross-era
comparisons refuse via INV-4 (D5).

**D2 — Measure-path anomaly → action policy. ADOPT (research Q4), with
the retake as the repeatability instrument (architect delta).**
The research's classifier needs N-repeat evidence that cloud positions
do not have; adding repeats is bound by the WAV cap. Resolution: the
**bounded auto-retake doubles as the repeat**. Policy:

- *Anomaly rule*: a position is anomalous when its realized gate
  `Tᵢ < 0.5 · T_med` (running median over already-accepted usable
  gates) **or** `f_valid,i > 2 · f_valid,med`. Warm-up: no anomaly
  verdicts until ≥3 accepted positions carry usable gates; positions
  accepted during warm-up are re-judged at group close.
- *Cheap suspicion guard* (map finding): a `FLOOR_MEASURED` detection
  within `T_MIN_PROXIMITY_SAMPLES` (default 8) of the search start is
  annotated `suspect_near_search_start` regardless of the anomaly
  rule — provenance for the classifier and the disclosure surface.
- *On anomaly at accept time*: trigger one bounded retake (new reason
  code beside `REASON_CLOUD_GEOMETRY_LOCKED`, counter beside
  `_geometry_retries_used`, prompt rung beside
  `CLOUD_GEOMETRY_RETRY_PROMPTS`, budget inside the existing
  `CLOUD_RETAKE_ALLOWANCE`): "One spot picked up a nearby surface.
  Let's redo that one — hold the phone a little farther from walls,
  shelves, and the speaker edge, then tap Retry."
- *Classify with the retake pair + cloud τ context*:
  - retake's gate ceilings or its τ disagrees (> `REPEAT_TOL` =
    max(1 sample, 5% of τ)) → **ARTIFACT**: reject the detection,
    re-gate the surviving capture at the ceiling, keep it, log only
    (no user nag).
  - retake reproduces τ AND the cloud's τ machinery says the delay is
    position-invariant (echo-estimate cluster / null-registry family
    agreement) → **SOURCE_FIXED**: do not gate on it; keep at ceiling;
    log only. (Sub-0.5 ms source arrivals like the horn rim are
    already outside the search span; this class covers ≥0.5 ms ones.)
  - retake reproduces a moved-or-similar τ that does not cluster
    across positions → **REAL_SURFACE**: keep the shorter gate only if
    the retake also collapsed (the surface is really there); after
    `MAX_RETAKE = 2` still-anomalous → **exclude-with-disclosure**:
    "We left one measurement out because it kept picking up something
    too close to the phone. Your result is based on the other N."
- *At group close*: the full policy re-runs over all positions
  (catches warm-up-window anomalies). Retroactively-anomalous
  positions that can no longer be retaken are excluded-with-disclosure
  when MIN_POS still holds; otherwise the close is refused with the
  existing group-close refusal grammar ("add a retake" via the
  final-position confirm gate). **Keep-and-poison is retired.**
- Every action lands an `event=` log; refusals and exclusions surface
  in the session payload and the chart disclosure (#1783 composes).

**D3 — Detector v2: AIC + matched filter with agreement gating. ADOPT
offline-first (research Q1), production-enable only on corpus green.**
New picker beside `detect_first_reflection` in `gating.py`: Stage 1
normalized cross-correlation of the post-direct IR against the direct
arrival's own template (`SRC_SIG_MS = 0.6`); Stage 2 Maeda-AIC onset on
the search span; Stage 3 admit a detection only where an MF local
maximum ≥ `MF_REL = 0.5` falls within `GUARD_MS = 0.15` of an AIC
break; no candidate → ceiling (`FLOOR_SEARCH_BOUND`), preserving the
9-capture behavior. Composes with — does not duplicate —
`detect_echo` (which owns HF comb evidence in its own band and stays
detection-only); the shared refusal discipline applies. Pre-registered
demotion rule: if corpus grading shows any regression on the desk-edge
ceiling set (prediction 8), detector v2 ships as a **veto** on the
current picker's early picks, not as the primary. The v1 picker and
constants stay in place either way until the corpus verdict is
recorded in the PR.

**D4 — Graded validity band. ADOPT (research Q5), superseding
`NEAR_FLOOR_RATIO`.** Hard floor stays `f_valid = 1/T` (k=1, continuity
with every recorded floor). Between `1/T` and `2/T`, derived
quantities carry a graded uncertainty ±2 dB at `1/T` tapering
linearly-in-log-f to ±0.5 dB at `2/T`; above `2/T` full confidence.
`flat_spec` gains a `marginal` band-verdict state: a band whose
worst deviation is within the graded uncertainty of its tolerance is
neither pass nor fail and is disclosed as such (never silently rounded
to pass). The `NEAR_FLOOR_RATIO = 1.25` advisory band is retired in
the same PR — one validity-uncertainty concept, not two.

**D5 — Session invariants. ADOPT INV-2/3/4; REJECT the research's
INV-1 ratio (architect delta).** The existing verify refusal is
*strict* (any shorter verify gate refuses); the research's
`COMPARE_RATIO = 0.7` would loosen it. Strictness is the conservative
direction at the anchor (same position ⇒ same geometry ⇒ a shorter
gate is evidence of changed placement), so it stays. Adopted:
- **INV-2** — when differencing two curves, grade only above
  `max(floor_a, floor_b)`; below, disclosed non-comparable.
- **INV-3** — cloud coherence: per-position gates spanning more than
  2× the cloud median flag the cloud `geometry_inconsistent` with a
  "your measurements varied a lot — consider remeasuring" nudge
  (advisory, not a refusal — cloud gate variance is legitimate).
- **INV-4** — window-shape invariance: comparisons and cross-session
  overlays assert identical `WINDOW_KIND`, `TAPER_FRACTION`, and
  combine-masking provenance; mismatch refuses with a named reason.

**D6 — Two-band gate. DEFER (research Q6).** Only if D1–D4 leave a
demonstrated LF-accuracy gap. Its acceptance test is pre-registered
(prediction 7: two-band matches single-gate above the splice within
±0.25 dB on the corpus) and its trigger is recorded here; no code in
this ladder.

**D7 — Continuous frequency-dependent windowing in the verdict path.
REJECT (research Q6).** Parameter-dependent smoothing is a hidden knob
that reshapes a graded curve; not compatible with the honesty
discipline. Recorded as closed; revisit requires new evidence, not
re-argument.

**D8 — Reciprocal-pair SSOT. ADOPT (map finding).** Collapse
`_measure_gate` (min window) / `_measure_validity_floor_hz` (max
floor) into one derivation with two views, pinned by a contract test.

## PR ladder

Standard per-PR gate: Opus implementation → independent Opus
adversarial review to 0 blockers / 0 should-fixes → CI green → merge.
Serial local lanes (`-p no:randomly`); corpus tests must report
PASSED (not SKIPPED) under both
`JTS_FLAT_LIN_CORPUS=…/captures/flat-linearization-20260725/cdhorn-live-session`
and `JTS_FLAT_LIN_S0=…/captures/flat-linearization-20260725`.

- **PR-G1 — masked combine (D1) + INV-2 + D8.** `spatial_combine`
  estimator + signature change (per-bin weights), support-derived
  floor, disclosure payload, `assemble_cloud_group_result` wiring,
  chart-feed support field. Corpus replay grades predictions 2, 3, 4
  (session floor returns ~143 Hz; <0.1 dB change above 200 Hz;
  <1 dB masked-vs-naive delta near the LF edge) — results recorded in
  the PR body. Re-pins `tests/test_flat_spec_ssot.py` incident figures
  by the documented procedure (the flattering +1.05 dB shift must
  disappear).
- **PR-G2 — anomaly policy (D2) + INV-3 + suspicion guard.** Conductor
  policy, retake wiring, classification with retake-pair + τ context,
  exclusion disclosure, user copy, `event=` logs. Corpus: replay the
  incident session — `cloud_04` must be flagged anomalous and (absent
  a live retake) excluded-with-disclosure with the floor recovered;
  the desk-edge sessions must produce zero anomaly flags.
- **PR-G3 — detector v2 (D3), offline + corpus-gated.** Pure
  `gating.py` addition + grading harness; predictions 1, 5, 8 graded
  and recorded; production enable (or veto demotion) decided by the
  recorded verdict, in the same PR only if green.
- **PR-G4 — graded band (D4) + INV-4.** `f_valid` uncertainty model,
  `flat_spec` marginal vocabulary, `NEAR_FLOOR_RATIO` retirement,
  envelope/chart disclosure (composes #1783's floor-disclosure work),
  window-shape assertion.
- **PR-G5 — two-band gate. DEFERRED** behind D6's trigger.

Sequencing: G1 first (highest leverage, incident-closing); G2 after G1
(policy consumes support-derived semantics); G3 and G4 independent
after G1, parallelizable.

## Traps (the review hunts these)

- **Measured-narrow-stated-wide.** Every corpus claim in a PR body
  states the geometry set it was graded on. The 26-capture corpus
  spans desk / desk-edge / ground-plane; predictions name their set.
- **The masking must not weaken the spec-mask separation.** The floor
  clamp rides the spec evaluation mask only; `excluded_interval_count`
  stays the honesty instruments' own count.
- **Unknown ≠ zero.** A `None` floor still clamps nothing and
  discloses as unknown; masking must not convert unknown floors into
  zero-support bins silently.
- **Express tier arithmetic.** Express M=5 → MIN_POS=3; cloud-verify
  M=6 → MIN_POS=3; full M=9 → MIN_POS=5. The formula must be derived
  from the group's actual position count after screens, never a
  literal.
- **Retake budget is shared.** Gate-anomaly retakes spend
  `CLOUD_RETAKE_ALLOWANCE`; the policy must degrade to
  exclude-with-disclosure when the budget is spent, never stall the
  plan.
- **No new echo detector.** Detector v2 picks gates; `detect_echo`
  owns echo evidence. One τ admission rule (`usable_echo_estimates`)
  keeps classification and the null registry from drifting.
- **WAV cap.** No repeat_count changes to cloud sweeps in this ladder.

## Acceptance

The ladder is done when: the incident session replays to a ~143 Hz
graded floor with the anomaly disclosed; the desk-edge sessions replay
unchanged; every new refusal/exclusion path has an `event=` log, an
honest copy string, and a pinning test; and the room-correction plan
(#1791) can consume the support-derived floor as its layer boundary
without reading any gating internals.

Last verified: 2026-07-28
