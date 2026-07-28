# Gating v2 — work order (issue #1790)

> **Status: adopted work order (2026-07-27).** Synthesized from the
> owner-run deep-research result (verbatim:
> [`docs/research/2026-07-27-acoustics-round-2/01-gating-v2.md`](research/2026-07-27-acoustics-round-2/01-gating-v2.md);
> prompt and laptop-side evidence in `captures/gate-research-20260727/`)
> against the code as verified on 2026-07-27. Anchors
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

## Current state (verified against code 2026-07-27 — the review bar)

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
- Near-floor band: `NEAR_FLOOR_RATIO = 1.25` marks
  `[floor, 1.25·floor)` "near_validity_floor". Locally documented as
  advisory, but **downstream it is a hard admission predicate**:
  `measured_candidate.py` requires `near_validity_floor: False` in
  the summed-acoustic admission set and refuses
  `isolated_overlap_unsafe` on `.get(...) is not False`, and
  `commissioning_isolated_producer.py` uses the same predicate —
  deleting the field would make every isolated overlap refuse
  (fail-closed brick of the commissioning isolated path). D4 must
  not retire it.
- Group aggregation: `cloud_validity_floor_hz` = `max(usable floors)`;
  single call site in `_run_cloud_pipeline` (invoked from
  `_close_cloud_group`); the floor clamps the spec mask in
  `assemble_cloud_group_result` (deliberately NOT
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
  parameter), so within-position repeatability data does not exist at
  cloud positions. Adding repeats is **byte-feasible** — the verify
  program's worst shape leaves ~3.5 MiB under
  `CROSSOVER_CAPTURE_MAX_WAV_BYTES` (the oft-quoted ~1.2 MB headroom
  belongs to the MEASURE program) — the binding constraint is session
  wall-clock and operator patience, which the conductor's own
  position-count adjudications name as the real limit.
- Retake machinery (U-ladder, shipped): `parse_begin_capture` admits a
  bounded retake of the last accepted slot; host-side precedent is the
  geometry-locked retake (`GEOMETRY_RETRY_POSITIONS = 2`, prompt rungs
  in `CLOUD_GEOMETRY_RETRY_PROMPTS`, budget beside
  `CLOUD_RETAKE_ALLOWANCE`), driven by returning a rejecting
  `PhaseVerdict` from the group consume path. A gate-anomaly retake
  rides the same lever; no relay/spec change needed.
- τ machinery (shipped, compose don't duplicate):
  `spatial_combine.detect_echo` (cepstrum + band-limited envelope,
  refusal-not-clamp window contract), `usable_echo_estimates` (the one
  τ admission rule), `assess_geometry` (τ-cluster
  GEOMETRY_LOCKED/DISPERSED/UNKNOWN), and
  `interference_nulls.identify_interference_nulls` (single-τ ladder
  admission; its classification vocabulary is *deliberately about the
  evidence, never about hardware* — position-invariance within one
  session cannot separate a speaker-borne arrival from a stable room
  path, and the module says so). Error discipline is **not** uniform
  and the plan does not pretend it is: `detect_echo` never raises
  (named refusals for both bad config and bad data);
  `combine_positions` / `identify_interference_nulls` are pure kernels
  that raise `ValueError` on malformed input; `gating.py` itself is a
  third shape (`detect_first_reflection` never raises — returns
  `floor_source=None`; `apply_gate_fragment` raises). Detector v2
  (D3) follows `gating.py`'s own detector shape: never raise on data,
  return the `floor_source` vocabulary; raise `ValueError` only on
  malformed config.
- Two representations of one quantity: `_measure_gate` (min of driver
  windows) and `_measure_validity_floor_hz` (max of driver floors) are
  a reciprocal pair computed independently.

## Adopted decisions

Each is tagged with its delta from the research recommendation where
code reality forced one.

**D1 — Group aggregation: per-position per-bin masking + MIN_POS.
ADOPT (research Q3, and the codebase's own queued fix).**
Inside `spatial_combine.combine_positions`, mask each capture's
contribution below that capture's own `1/Tᵢ` before power-averaging;
a bin is graded only where at least
`MIN_POS = max(3, ceil(C/2))` captures contribute, where **C = curves
entering the combine after existing screens** — curves, not prompted
positions: the cloud carries N − 1 summed curves for N positions (see
traps for the worked table). Below MIN_POS, the bin is disclosed
`insufficient_spatial_support` — not silently averaged, not silently
dropped. The group's `validity_floor_hz` payload key is kept
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

**D2 — Measure-path anomaly → action policy. ADOPT (research Q4),
simplified against D1 (architect delta — twice revised).** The
research's classifier needs N-repeat evidence that cloud positions do
not have. Repeats are byte-feasible but cost session time at *every*
position; a retake costs time only on anomaly (rare). The first draft
of this plan treated the retake as a repeatability instrument; the
review killed that honestly — a retake deliberately **moves the
phone**, so it samples the position axis, not the repeat axis, and
placement jitter (~50 µs+, the research's own SRCFIX_TOL rationale)
exceeds any within-position τ tolerance. The re-derived policy leans
on D1 instead: **once per-bin masking is in, a collapsed gate cannot
poison the group**, so the policy's job is only (1) give the user one
chance to fix placement, (2) disclose. No τ-agreement fork, no
artifact-vs-real action split, no speaker-vs-room inference:

- *Anomaly rule*: a position is anomalous when its realized gate
  `Tᵢ < 0.5 · T_med` (running median over already-accepted usable
  gates) **or** `f_valid,i > 2 · f_valid,med`. Warm-up: no anomaly
  verdicts until ≥3 accepted positions carry usable gates; positions
  accepted during warm-up are re-judged at group close.
- *Cheap suspicion guard* (map finding): a `FLOOR_MEASURED` detection
  within one envelope-kernel span (`ENVELOPE_SMOOTH_MS`, i.e. the
  detector's own resolution — self-justifying and rate-independent)
  of the search start is annotated `suspect_near_search_start`
  regardless of the anomaly rule. The incident's 3-sample detection
  is caught by this alone.
- *Invariant-family short-circuit*: if the anomalous detection's τ
  joins a geometry-locked cloud-wide arrival family (through the one
  τ admission rule — `usable_echo_estimates` clustered by
  `assess_geometry`; that single instrument is the authority, no
  second one), the collapse is not a placement problem: skip the
  retake prompt, do not gate on the arrival, and let the shipped
  echo-detection → null-identification → carve-out chain own its
  spectral consequence (that chain deliberately cannot say
  speaker-vs-stable-room-path either, and does not need to — its
  disclosure surfaces carry the finding, not a log line).
- *On anomaly at accept time*: trigger one bounded retake (new reason
  code beside `REASON_CLOUD_GEOMETRY_LOCKED`, counter beside
  `_geometry_retries_used`, prompt rung beside
  `CLOUD_GEOMETRY_RETRY_PROMPTS`, budget inside the existing
  `CLOUD_RETAKE_ALLOWANCE`): "One spot picked up a nearby surface.
  Let's redo that one — hold the phone a little farther from walls,
  shelves, and the speaker edge, then tap Retry."
  - *Retake gate healthy* → it **replaces** the original (the shipped
    `_retain_cloud_position` replacement semantics — no pair storage
    needed for the decision). A compact anomaly-evidence note (the
    replaced take's τ and gate scalars) is persisted for the session
    record so the event is disclosed, not just logged.
  - *Retake also anomalous* (or budget exhausted) → **retain with its
    own floor under D1 masking + disclose** (`gate_anomaly_retained`).
    Masking makes retention safe and keeps the position's valid HF
    content; excluding it would discard good data. The user-facing
    disclosure: "One spot kept picking up something close to the
    phone; frequencies it couldn't see cleanly came from the other
    N − 1 spots."
- *Persisted provenance*: a retained-anomalous capture's gating block
  carries a `gate_anomaly` annotation so the durable record
  distinguishes it from an honest ceiling (`FLOOR_SEARCH_BOUND`) and
  from a clean measured floor — the review's
  indistinguishability finding.
- *At group close*: the policy re-runs over all positions (catches
  warm-up-window anomalies); retroactively-anomalous positions are
  retained-with-mask + disclosed. The close **never stalls**: MIN_POS
  governs which bins are gradeable, and a degraded support floor
  rises honestly and visibly. The final-position confirm-gate retake
  remains the user's option, never a forced loop.
  **Keep-and-poison is retired; keep-with-mask-and-disclose replaces
  it.**
- *INV-3 ordering*: the cloud-coherence nudge evaluates the
  post-policy retained set, so a household whose outlier was already
  replaced or disclosed is not nagged twice.
- Every action lands an `event=` log; disclosures surface in the
  session payload and the chart disclosure (#1783 composes).

**D3 — Detector v2: AIC + matched filter with agreement gating. ADOPT
offline-first, conditional on demonstrated need (research Q1,
demoted by the review's balance check).** After D1 + D2, a false pick
is harmless to the group (masked) and disclosed (policy); the residual
risk D3 addresses is **retake friction** — false detections cause
needless retake prompts — plus the masked position's own lost LF
bins. D3's warrant is therefore measured, not assumed: the corpus
grading (predictions 1, 5, 8) *is* the gate, and the observed retake
rate after G2 ships informs whether v2 becomes primary at all. The
design: new picker beside `detect_first_reflection` in `gating.py`
following the same detector contract (never raise on data; return the
`floor_source` vocabulary): Stage 1 normalized cross-correlation of
the post-direct IR against the direct arrival's own template
(`SRC_SIG_MS = 0.6`); Stage 2 Maeda-AIC onset on the search span;
Stage 3 admit a detection only where an MF local maximum ≥
`MF_REL = 0.5` falls within `GUARD_MS = 0.15` of an AIC break; no
candidate → ceiling (`FLOOR_SEARCH_BOUND`), preserving the 9-capture
behavior. Composes with — does not duplicate — `detect_echo` (which
owns HF comb evidence in its own band and stays detection-only).
Pre-registered demotion rule: any regression on the desk-edge ceiling
set (prediction 8) → v2 ships as a **veto** on the v1 picker's early
picks, not as the primary. Provenance: the gating fragment gains a
`detector` field (`"v1"`/`"v2"`) so two eras never become
indistinguishable in a bundle; INV-4 asserts it for comparisons, and
`GATING_SCHEMA_VERSION` bumps if field semantics change.

**D4 — Graded validity band. ADOPT (research Q5), as
disclosure-only.** Hard floor stays `f_valid = 1/T` (k=1, continuity
with every recorded floor). Between `1/T` and `2/T`, derived
quantities carry a graded uncertainty ±2 dB at `1/T` tapering
linearly-in-log-f to ±0.5 dB at `2/T`; above `2/T` full confidence.
`flat_spec` gains a **`marginal` disclosure flag, not a third verdict
value**: `BandResult.passed` stays `bool | None` with its existing
meanings (`None` = unevaluable) and is still computed at nominal
tolerance, so `overall_passed` composition is untouched and
prediction 6's "changes zero pass verdicts" holds *by construction*;
a band whose worst deviation lies within the graded uncertainty of
its tolerance additionally carries `marginal: True`, rendered loudly
on the spec/chart surfaces (passed-with-asterisk, failed-with-
asterisk — never silently rounded either way).
`NEAR_FLOOR_RATIO` is **not retired** (first draft said retire; the
review mapped the blast radius): `near_validity_floor` is a hard
admission predicate in `measured_candidate` and
`commissioning_isolated_producer` (`.get(...) is not False` — field
deletion refuses every isolated overlap). The two concepts coexist:
`near_validity_floor` stays the admission gate at 1.25; the graded
band is grading disclosure at 2.0. Unifying them is recorded as a
follow-up with those three consumer sites named — not done here.

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
Serial local lanes (run pytest without xdist parallelism — no `-n`;
`pytest-randomly` is not installed, so no de-randomization flag is
needed); corpus tests must report PASSED (not SKIPPED) under both
`JTS_FLAT_LIN_CORPUS=…/captures/flat-linearization-20260725/cdhorn-live-session`
and `JTS_FLAT_LIN_S0=…/captures/flat-linearization-20260725` (note
`requires_s0` fixtures also need `s0-analysis/loopback/`, and
`requires_s0_curves` needs `s0-session-deskedge/` plus the UMIK-2 cal
file — `JTS_FLAT_LIN_S0` alone is necessary, not sufficient).

- **PR-G1 — masked combine (D1) + INV-2 + D8.** `spatial_combine`
  estimator + signature change: `PositionCapture` gains the
  per-position validity floor (today it carries none — the input
  contract is part of the change), per-bin weights, support-derived
  floor, disclosure payload, `assemble_cloud_group_result` wiring,
  chart-feed support field. **Owns the consumer-contract rewrite** in
  `correction_crossover_v2.py`'s compact block: the key's documented
  purpose ("separate 'the room combed this speaker' from 'one
  capture's gate collapsed'") is answered post-D1 by the per-position
  disclosure list, not the group scalar — the docstring and payload
  contract move together. Corpus replay grades predictions 2, 3, 4 —
  results recorded in the PR body. Re-pins
  `tests/test_flat_spec_ssot.py` incident figures **and adds the
  documented re-pin procedure block that file currently lacks**
  (modeled on the conductor golden-pin discipline: what the figures
  mean, what must never change them, how to re-derive).
- **PR-G2 — anomaly policy (D2) + INV-3 + suspicion guard.** Conductor
  policy, retake wiring, invariant-family short-circuit, retained-
  anomalous provenance annotation + evidence note, disclosure copy,
  `event=` logs. Corpus (pinned to the **v1 detector** explicitly —
  G3 changing the detector later moves this assertion to prediction-1
  grading, it does not silently break it): replay the incident
  session — `cloud_04` must be flagged anomalous and (absent a live
  retake) retained-with-mask + disclosed, with the graded floor
  recovered; the S0 desk-edge sessions must produce zero anomaly
  flags.
- **PR-G3 — detector v2 (D3), offline + corpus-gated + conditional.**
  Pure `gating.py` addition + grading harness + `detector` provenance
  field; predictions 1, 5, 8 graded and recorded; production enable
  (or veto demotion) decided by the recorded verdict, in the same PR
  only if green.
- **PR-G4 — graded band (D4) + INV-4.** `f_valid` uncertainty model,
  `flat_spec` marginal disclosure flag (verdict composition
  untouched), envelope/chart disclosure (composes #1783's
  floor-disclosure work), window-shape + masking-provenance
  assertion. **Grades prediction 6** on the S0 ground-plane set
  (zero verdict flips; some near-floor bins gain the marginal flag)
  — recorded in the PR body.
- **PR-G5 — two-band gate. DEFERRED** behind D6's trigger.

Sequencing: G1 first (masking is the incident-closing rung); G2 after
G1 (the retention policy leans on masking); G3 and G4 independent
after G1, parallelizable — with G2's corpus assertion pinned to v1 as
above.

## Traps (the review hunts these)

- **Measured-narrow-stated-wide.** Every corpus claim in a PR body
  states the geometry set it was graded on, **using the code's own
  set names** (`S0_MAIN` / `S0_DESK_EDGE` / `S0_GROUND_PLANE` in
  `tests/_flat_lin_corpus.py` — 20/6/6 WAVs across 16 positions in
  the durable S0 corpus) plus the separate pre-S0 cdhorn corpus under
  `JTS_FLAT_LIN_CORPUS`. There is no "26-capture corpus"; the first
  draft's number conflated legs.
- **MIN_POS counts curves, not positions.** The cloud carries
  **N − 1 summed curves** for N prompted positions (the conductor's
  own 2026-07-26 adjudication — the first draft of *that* constant
  made this exact error, and the first draft of *this plan* repeated
  it). `MIN_POS = max(3, ceil(C/2))` where C = curves entering the
  combine after screens: full cloud-measure 9 pos → 8 curves →
  MIN_POS 4; cloud-verify 6 → 5 → 3; express cloud-measure 5 → 4 → 3;
  express cloud-verify is 1 position → **0 curves → no combine at
  all** (the degenerate case is named, not discovered). Derived from
  the group's actual curve count, never a literal.
- **The masking must not weaken the spec-mask separation.** The floor
  clamp rides the spec evaluation mask only; `excluded_interval_count`
  stays the honesty instruments' own count.
- **Unknown ≠ zero.** A `None` floor still clamps nothing and
  discloses as unknown; masking must not convert unknown floors into
  zero-support bins silently.
- **Retake budget is shared.** Gate-anomaly retakes spend
  `CLOUD_RETAKE_ALLOWANCE`; when the budget is spent the policy
  degrades to retain-with-mask + disclose — the plan never stalls,
  and the group close never demands a retake it cannot admit.
- **No new echo detector.** Detector v2 picks gates; `detect_echo`
  owns echo evidence. One τ admission rule (`usable_echo_estimates`)
  keeps the invariant-family short-circuit and the null registry from
  drifting.
- **No repeat_count changes to cloud sweeps in this ladder** — on
  session-duration grounds (the byte cap has headroom; the operator's
  patience does not).

## Acceptance

The ladder is done when: the incident session replays to a ~143 Hz
graded floor with the anomaly disclosed; the S0 desk-edge sessions
replay unchanged; every new refusal/disclosure path has an `event=`
log, an honest copy string, and a pinning test; and the
room-correction plan (#1791) can consume the support-derived floor as
its layer boundary without reading any gating internals.

Last verified: 2026-07-27
