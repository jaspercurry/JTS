# Flat linearization — productization work order (W1–W5 + HW)

> **This is the execution work order** for productizing the
> measurement-honesty machinery that S0 proved. Strategy, spec,
> evidence, and adjudications live in
> [`flat-linearization-plan.md`](flat-linearization-plan.md) — this doc
> never restates them, it maps them onto PRs. Read that doc's
> **"S0 executed — results and the attribution correction
> (2026-07-25)"** section first; it is ground truth for everything
> here. Owner decisions of record for this work order were taken
> 2026-07-25 (see "Owner decisions" below). Written by the Fable
> architect session for execution by Opus/Sonnet implementer sessions.

## Mission

Take the S0-proven honesty machinery — the spatial cloud
(`combine_positions`), the interference-null identification method
(τ-ladder + arrival corroboration + implied-r agreement + the
null-depth ceiling test), and the echo detector (`detect_echo`) — and
make it product: wired into the live v2 conductor flow, working for
any hardware, correcting what physics allows, refusing what it
doesn't, and showing the user what it found.

Hold every design against the product vision:

- **Hardware-abstracted.** Any speaker with a mic: horns, waveguide
  domes, bare domes, full-range. No horn-specific, JTS3-specific, or
  geometry-specific logic in shipped code. Generalize by **contract**
  (declared driver metadata that already exists + measured evidence),
  never by device taxonomy.
- **Anomaly detection is algorithmic, not LLM.** Pure numpy code
  end-to-end. Combs from any source (horn rims, waveguide edges, desk
  bounces, enclosure diffraction, room paths) get detected, classified
  where evidence allows, excluded from correction where uncorrectable,
  and disclosed — without degrading linearization of the rest of the
  band. Where the evidence cannot classify (e.g. source-fixed vs
  room-fixed from a single session), the output says so instead of
  guessing.
- **Visualization is a first-class deliverable.** jts.local shows the
  initial measurement, the post-correction measurement, and
  plain-language anomaly callouts. τ/r vocabulary lives in an expert
  disclosure, not the headline.
- **80/20.** Hardened against anomaly classes we have evidence for; no
  astronaut engineering for hypothetical hardware. Judgment-call
  corner-cuts are named in this doc, not decided silently.

## Owner decisions of record (2026-07-25)

1. **8–16 kHz spec: carve-out + envelope.** Identified interference
   nulls (τ/r recorded) are excluded from spec evaluation AND from
   correction; the ±2.5 dB tolerance applies to the surviving
   envelope; the report discloses "EQ cannot fill these" with the
   numbers. The spec table's number is not changed. Horn redesign
   proceeds in parallel as the owner's hardware project. (Resolves the
   plan doc's open question 8 mechanics for the product; the plan
   doc's spec-table annotation is updated by PR-6.)
2. **Hardware sessions: runbooks now, drive later.** This work order
   ships pre-registered runbooks (RB-1 per-driver discriminator, RB-2
   flush-capsule ground plane); a later owner-present session executes
   them. Product wiring (W1–W5) does not gate on either runbook.
3. **RB-2 (flush-capsule ground-plane redo) is in scope** as an
   optional leg after RB-1, same visit if time and appetite allow.
4. **Review protocol:** every PR gets an independent Opus 5
   adversarial review (the canonical repo review prompt, scope line
   adapted), rerun until 0 blockers and 0 should-fixes.
   Commit-before-review. This plan doc itself received one Opus round
   before its PR opened.

## Architecture — how the honest instrument composes

```
phone taps + prompted mic moves        (PR-3a/3b: capacity + choreography)
        │
        ▼
per-position gated summed sweeps ──► per-capture QC (existing diagnostics)
        │
        ▼
combine_positions()                       (shipped; wired by PR-4)
  ├── power-mean spec curve + 1/6-oct diagnostics   ── THE spec curve SSOT
  ├── power-vs-median exclusion screen  (partially-aligned interference)
  ├── per-position detect_echo + assess_geometry    (PR-2 hardening)
  └── BandSpread cross-position stability
        │
        ▼
identify_interference_nulls()             (PR-1: the new orthogonal gate)
  τ-ladder + arrival corroboration + implied-r agreement + depth ceiling
  → null registry {f, n, τ, r_time, r_freq, classification, evidence}
        │
        ▼
merged honesty mask = screen ∪ identified nulls      (consumed TOGETHER
        │                                             with geometry.locked
        ▼                                             — issue #1742 item 4)
evaluate_flat_spec(spec curve, mask)      (shipped; wired by PR-4/PR-6)
fit engine: envelope terms from mask + spread        (PR-6)
report: before/after + plain-language callouts       (PR-7)
```

Three complementary honesty instruments, each with a named blind spot:

| Instrument | Catches | Blind to |
|---|---|---|
| power-vs-median screen | partially-aligned interference (some positions nulled, others not) | fully-aligned nulls (every position sees the same null) |
| `geometry.locked` | a cloud whose echo estimates cluster (τ shift < tolerance) — "spreading the mic further" advice | anything without a credible echo estimate |
| null-ID gate (PR-1) | position-invariant interference nulls attributable to a measured arrival | dips with no matching arrival/ladder (left alone — honest) |

Consumers must consume all three together (the wiring contract,
issue #1742 item 4). The mask alone is a hole.

### Two architect interpretation calls (owner-visible, decided here)

**(A) "The fit engine consumes the combined curve" (plan fundamental 3)
is implemented as combined-cloud-derived constraints on the existing
per-driver fit, not as a replacement of the fit's input curve.**
Mechanism: a new `ReasonCode` term in
`jasper/active_speaker/linearization_envelope.py::compose_envelope`
zeroes `allowed_depth_db` on honesty-masked bins (exclusion), plus a
position-stability term that shrinks allowed correction depth where
cross-position spread is high (from `CombinedResponse`'s `BandSpread`).
Rationale: features that survive spatial averaging are, by
construction, present at every position — including the anchor — so
suppressing everything else (the stability term) leaves the fit
correcting only surviving features, which is the fundamental's intent.
The anchor's *rendition* of a surviving feature may still differ in
magnitude from the cloud's: the parent plan is explicit that the cloud
grades the crossover region "the way CTA-2034's listening window does
(slightly gentler than a single on-axis point — by design)," so an
anchor-vs-cloud offset on surviving features is expected, bounded by
the cloud's own `BandSpread` diagnostics, and not a defect. This call
preserves the per-driver safety machinery (repeat sigma, mic-tier
eligibility, cut-only invariant, anchored give-back) untouched. The
literal alternative — role-sliced combined-summed-curve as fit input —
is recorded as a deeper follow-up with a **measured** revisit trigger:
S3 closed-loop residuals (graded on the cloud) stalling above the
convergence threshold with residual concentrated on surviving
(non-excluded, position-stable) features beyond what `BandSpread`
predicts. An expected listening-window offset is not the trigger.

**(B) The S2 "one shared curve construction" is scoped to the
spec-facing curves.** `combine_positions`' output (power-mean curve,
1/3-oct spec + 1/6-oct diagnostic smoothings, exclusion intervals)
becomes the single owner of "what does the speaker measure as" —
consumed by the spec gauges, the observe ledger's spec-facing summary,
the report, and VERIFY's flatness gauge. VERIFY's *tracking* comparator
(`_analyze_verify`'s measured-vs-`predicted_sum` on the capture grid)
stays a distinct construction **on purpose**: it answers "did apply do
what the model predicted," not "is the speaker flat," and collapsing
the two conflates different questions. The MEASURE-vs-VERIFY
frame-discrepancy class is killed by the spec-facing SSOT; the
tracking comparator is documented as intentionally separate (PR-5).
The `_ladder_smooth` parity duplicate between `linearization_fit` and
`linearization_envelope` is internal fit machinery on the envelope
grid — out of scope, already pinned by a lockstep test.

## Execution protocol (for the executing session)

- **Read first:** this doc; `flat-linearization-plan.md` "S0 executed"
  + "The six fundamentals" + "Adjudicated" + "Non-goals"; memory
  `MEMORY.md` (esp. `reference_adversarial_review_prompt`,
  `project_flat_linearization_program`); the file map in
  `HANDOFF-crossover-measurement-v2.md` before any W2/W3/W4 PR.
- **Per PR:** branch from freshly-fetched `origin/main`
  (`git merge-base --is-ancestor origin/main HEAD` must pass before
  first edit); implement; `scripts/test-fast` locally; corpus tests
  run with the corpus env vars — the existing suites use
  `JTS_FLAT_LIN_CORPUS=/Users/jaspercurry/Code/JTS/captures/flat-linearization-20260725/cdhorn-live-session`
  (the **pre-S0** seven-run corpus), and the NEW acceptance fixtures
  in PR-1/PR-2 gate on a second root,
  `JTS_FLAT_LIN_S0=/Users/jaspercurry/Code/JTS/captures/flat-linearization-20260725`
  (with `s0-session-main/`, `s0-session-groundplane/`, and
  `s0-analysis/loopback/` resolved beneath it). Both gates skip
  cleanly in CI — which is exactly why a corpus-acceptance PR is not
  done until the implementer has run the suite locally and
  **confirmed the corpus tests report PASSED, not SKIPPED**
  (`pytest -rs`): a wrong-but-existing path skips silently; commit;
  **independent Opus 5 adversarial
  review** with the canonical prompt (memory
  `reference_adversarial_review_prompt`, verbatim, scope line adapted
  to the branch), rerun to 0 blockers / 0 should-fixes; run the docs
  checks the prompt lists; push; open PR; `gh pr merge --auto` then
  **verify actual state with `gh pr view`** (the exit code lies;
  squash merges also break `--contains` ancestry checks — verify via
  PR state, not ancestry).
- **Implementer tiers:** Opus 5 for DSP/estimator/conductor cores,
  Sonnet 5 for plumbing/tests/web/evidence. Never low-effort tiers.
- **Subagent worktrees read the wrong checkout unless pinned** — pass
  absolute paths explicitly in every subagent prompt.
- **Do not touch the bass lane** (`jasper/bass_extension/*`,
  `correction_bass_flow`, bench executor).
- **No paid voice-eval, no audible playback in W1–W5.** Everything
  through PR-8 is hardware-free (pytest + corpus replay), with one
  out-of-repo action: PR-3a's relay-Worker + capture-page deploy
  (release ordering in that PR). Audible playback happens only in
  the HW session, owner pinged first.
- **The re-litigation firewall:** pulse/TDS time-selection, two-path
  inversion, cepstral echo *removal*, max-hold estimator, the
  tweeter's `inverted: true`, and prediction-5's original consequent
  are all adjudicated in the plan doc. Do not reopen them in design
  discussions or review responses.
- **The recurring defect class is measured-narrow-stated-wide prose**
  (spatial_combine's 4-round review history; two S0 grader bugs).
  Every claim in a docstring/comment must state the exact regime it
  was measured in. Reviewers: hunt this specifically.

## The PR ladder

Workstream → PR map (the W vocabulary used throughout this doc):
**W1** = PR-2 + PR-1 (pure cores), **W2** = PR-3a + PR-3b + PR-4
(instrument into the live flow), **W3** = PR-5 + PR-6 (correction
doctrine), **W4** = PR-7 (visualization), **W5** = PR-8 + the
laptop-side kit parity. **HW** = the runbooks + product smoke.

Dependencies: **PR-2 → PR-1** (both edit `spatial_combine.py` and
PR-1 builds on PR-2's results — sequential, PR-2 first, never
parallel). PR-3a is independent of both; PR-3b needs PR-3a (deployed);
PR-4 needs PR-1 + PR-3b; PR-5 needs 4; PR-6 needs 5; PR-7 needs 6
(skeleton may start after 4); PR-8 rolls up at the end. Sequential
within `jasper/web/correction_*` (PR-4/5/6/7 touch big fast-moving
files — rebase before push, never stack long-lived branches).

---

### PR-1 — `identify_interference_nulls`: the orthogonal null-ID gate
**Tier: Opus. Size: ~500–800 module lines + ~700 test lines. Lands
after PR-2 (see dependencies).**

New pure module `jasper/audio_measurement/interference_nulls.py`
(name avoids collision with `null_walk.py`, which is the
delay-alignment search primitive — unrelated). Same purity contract as
`spatial_combine`: numpy only, no I/O/logging/policy, zero production
callers until PR-4.

**Method (promotes the S0 forensics; reference:
`captures/flat-linearization-20260725/s0-analysis/REPORT.md` § the
null-consistency analysis, and plan doc "S0 executed" § b/§ e.1):**

1. **Candidate τ set** from the cloud's per-position
   `EchoDiagnostic`s (confident, non-refused estimates; cluster
   consistent with `assess_geometry`'s vocabulary). A candidate needs
   ≥2 corroborating positions; single-estimate candidates are
   reported `insufficient_evidence`, never identified.
2. **Ladder prediction:** null frequencies `f_n = (n + ½)/τ` for n
   covering the analysis band.
3. **Null matching — designed around a known measured discrepancy.**
   Locate measured local minima of the combined 1/6-oct diagnostic
   curve (depth measured against the local envelope, not absolute
   level). On the S0 corpus, the ladder τ implied by the measured
   null frequencies (293–308 µs across groups) sits **4–9 % below**
   the directly measured arrival τ (median 321.5 µs) — the REPORT's
   own hedge is "consistent … at this resolution," and a real rim
   wave is not an ideal single-delay reflector. Therefore the
   matcher fits the **best single-τ ladder to the minima with τ as a
   free parameter** (require ≥2 matched rungs — one dip is not a
   ladder); the rung-match tolerance is calibrated on the corpus and
   stated with its regime. A smoothing-bandwidth window alone
   (±~6 % at 1/6-oct) does **not** admit the measured gap and would
   fail the very corpus this gate is pre-registered against.
4. **Arrival corroboration + implied-r agreement:** corroborate the
   fitted ladder against the cloud's arrival estimates — τ_ladder
   within a corpus-calibrated band of the arrival τ that admits the
   measured 4–9 % gap (stated with its regime) — then, from
   matched-null depths, solve the frequency-domain r (max null depth
   for reflection ratio r is `20·log10((1+r)/(1−r))`) and compare
   against the time-domain r from the arrival envelope
   (`EchoDiagnostic.strength_db`-derived). S0 measured r agreement
   0.031 (0.373 time-domain vs 0.342 frequency-domain); the shipped
   agreement gate is likewise corpus-calibrated and regime-stated.
5. **Depth-ceiling acquittal:** a dip **deeper** than the candidate
   arrival's physical ceiling (+ stated margin) **cannot** be that
   echo and is refused attribution — this is exactly how the 10.71 dB
   1.8 kHz dip was acquitted of the r≈0.37 arrival (ceiling 6.81 dB).
   An acquitted dip is left alone (no exclusion from this gate; the
   power-vs-median screen may still catch it independently).
6. **Classification vocabulary** (evidence-honest, hardware-blind):
   - `position_invariant` — ladder + arrival corroborated across the
     cloud (the S0 comb shape). Callout copy may say "origin travels
     with the speaker **or** a path that did not change during the
     session; moving the speaker and re-measuring distinguishes them"
     — a single session cannot separate speaker-fixed from
     static-room-path, and the output must not claim it can. (S0
     separated them only by moving the speaker.)
   - `position_dependent` — matched at some positions, absent/moved
     at others (needs per-position curves; see input contract).
   - `insufficient_evidence` — everything else, with the reason.
7. **Output:** `InterferenceNullReport` — tuple of `IdentifiedNull`
   records `{f_lo_hz, f_hi_hz, n, tau_us, r_time, r_freq,
   agreement, depth_db, classification, evidence: {...}}` + merged
   exclusion intervals (reuse `merged_true_intervals` — one owner) +
   per-candidate refusal reasons. The registry is the exclusion
   *reason of record* consumed by PR-6.

**Input contract:** `CombinedResponse` + the per-position smoothed
curves. PR-1 **owns** extending `CombinedResponse` to retain
per-position 1/6-oct curves (one smoothing owner — note
`CombinedResponse.stacked` is deliberately *unsmoothed* per-position
magnitude, so "just use stacked" would force the new module to
re-smooth, duplicating the construction: the SSOT failure mode).
*(Pre-registered here as `stacked`; shipped as the field
`per_position_db` — `stacked` was a local inside `combine_positions`,
never a field, so PR-1 added both it and `per_position_diag_db`. The
SSOT argument is unchanged and in fact stronger: with no field at all, a
consumer would have had to rebuild the canonical-grid and decimation
chain as well as the smoothing.)*
PR-2 does not touch this extension. Band to search derives from the
**caller-supplied** analysis band, not a horn constant.

**Acceptance (corpus + synthetic, all hardware-free):**
- Corpus (env-gated): on the S0 main-leg cloud, identifies the
  8–16 kHz nulls (8.4/11.5/15 kHz family) as `position_invariant`
  with a single fitted τ_ladder whose arrival corroboration passes
  the calibrated band of method step 4 (the measured 4–9 %
  ladder-vs-arrival gap admitted; calibration committed with its
  regime stated) and r agreement ≤ 0.05; **refuses**
  attribution of the 1.8 kHz dip by depth ceiling; on the
  ground-plane set, does not fabricate an identification from the
  125–146 µs proud-capsule arrival without its own ladder support.
- Synthetic: constructed two-path IR + curve → exact identification;
  no-echo synthetic → empty registry; ladder-without-arrival and
  arrival-without-ladder → `insufficient_evidence`; a dip 3 dB deeper
  than the ceiling → acquitted.
- Property: never identifies more total excluded bandwidth than a
  stated cap fraction of the analysis band (a runaway-exclusion
  guard; calibrate on corpus, state the number).

**Review scope line:** "the interference-null identification gate
added on this branch (jasper/audio_measurement/interference_nulls.py
and its tests)."

---

### PR-2 — `detect_echo` hardening + geometry policy (issue #1742 items 2–3 + S0 findings)
**Tier: Opus. Size: ~250–450 diff lines in `spatial_combine.py` +
tests. Lands before PR-1 (same file, sequential).**

Four changes, all inside the shipped detector/geometry layer:

1. **`band_below_passband` signal-presence refusal.** New optional
   parameter (e.g. `signal_band_hz: tuple | None`) — the *caller*
   supplies the declared passband (pure module stays
   product-blind; the wiring layer derives it from the driver
   contract in PR-4). When the analysis band's energy sits far below
   the declared passband's (the loopback found a confident τ=323 µs
   at confidence 0.275 on a woofer-branch signal **49.7 dB** below
   its own passband — pure stopband residue), refuse with the new
   reason string. Fixture: the loopback woofer-branch WAV
   (corpus-gated) + a synthetic stopband-residue case. Threshold
   calibrated with stated margin against both.
2. **The new-earlier-arrival corroboration fix.** S0's ground-plane
   captures: a *new dominant earlier* arrival (125–146 µs) pulled the
   envelope corroboration candidate away and the detector collapsed
   to an uninformative confidence-0 result via a corroboration
   technicality (`corroboration = 1.0` with `refusal=""` — the module
   deliberately keeps "ran, found nothing credible" distinct from a
   refusal; this fix must preserve that distinction). The detector
   must consider
   in-window arrival candidates for corroboration rather than only
   the global envelope peak — or refuse with an explicit
   `earlier_dominant_arrival` reason naming the interloper's τ.
   Either way the gp fixtures must yield an *informative* result
   (found-with-corroboration or named-refusal), never a silent
   confidence collapse. Corpus fixtures: gp_01–03.
3. **Thin-evidence geometry lock (#1742 item 2).** `GeometryLock`
   gains an explicit low-evidence qualifier (e.g.
   `thin_evidence: bool` when confident-estimate count is at
   `GEOMETRY_MIN_CONFIDENT` with n_positions ≥ 2× that) rather than
   scaling the threshold. Consumers (PR-4's UX) phrase the verdict
   softer when thin. Estimates were honest in the observed case —
   the fix is disclosure, not rejection.
4. **Default-window effective floor (#1742 item 3).** Do **not**
   raise the default lower edge — disclosure over restriction. (The
   gp 125–146 µs arrivals were caught by the S0 report's offline
   envelope scan, not by `detect_echo`, whose default-window
   reporting floor is already ~191–210 µs — which is exactly why the
   floor must be *surfaced*, not pushed higher.) Instead surface the
   window's effective
   reporting floor (`effective_floor_us`) on `EchoDiagnostic` so
   consumers/UI can say "arrivals below ~X µs are invisible to this
   window" honestly. Documented at point of use.

Update `tests/test_spatial_combine.py` contract layers; keep the
rahmonic-screen calibration tests untouched (they are env-guarded and
adjudicated). Every new claim states its measured regime.

**Review scope line:** "the detect_echo/geometry hardening on this
branch (spatial_combine.py diff and its tests)."

---

### PR-3a — Relay capture-plan capacity (the protocol prerequisite)
**Tier: Opus. Size: ~200–400 diff lines across Pi + Worker + tests,
plus a Worker/page deploy.**

The relay protocol hard-caps a capture plan at **8 entries** — and
the choreography needs 15–21. `MAX_CAPTURE_PLAN_ATTEMPTS = 8` in
`jasper/capture_relay/spec.py` bounds `capture_target ≤ max_attempts
≤ 8` with entry indexes exactly `0..capture_target-1`, kept in
lockstep with `relay/src/worker.js`'s own
`MAX_CAPTURE_PLAN_ATTEMPTS = 8`, which governs the **deployed
Worker's blob-index space** (`bad_capture_index` beyond it). And
`max_attempts` doubles as the **retry** budget, so capacity must
cover entries *plus* retakes (target ~32). Without this PR, plan
fundamental 1 (N≈8–12 positions) is structurally unreachable through
the relay — the cap, not the phone UI, is the real constraint (the
per-entry prompt mechanism itself needs nothing new; see PR-3b).
Splitting the cloud across relay sessions is not an escape: one plan
per session, and PR-3b's resume rule keeps cloud evidence
session-bound.

**Design contract:** raise the cap in lockstep on both sides. The Pi
gates emitted plan size on the session's **negotiated protocol
version**, so an un-updated Worker/page never receives a >8-entry
plan — version skew fails closed (plan refused with a clear error),
never mid-session. Deploy order per the capture-page README's
release-ordering rule: Worker + page first (accepting larger plans is
backwards-compatible), Pi second (emitting them).

**Acceptance:** spec-layer tests for >8-entry plans and the skew
refusal; Worker-side test for the widened index space; the existing
3-entry and 1-entry (re-verify) flows byte-identical.

**Review scope line:** "the relay capture-plan capacity change on
this branch (capture_relay/spec.py, relay/src/worker.js, version
gating, and tests)."

---

### PR-3b — Conductor position-group choreography (S1b, the instrument's front half)
**Tier: Opus. Size: ~800–1200 diff lines across conductor/relay/web/phone copy + tests. Needs PR-3a merged AND its Worker/page deployed.**

The v2 conductor
(`jasper/active_speaker/crossover_v2_flow.py::CrossoverV2Conductor`)
gains prompted multi-position capture groups. **No new phone-side
*prompt* mechanism is needed** (capacity is PR-3a's job): position
prompts ride `CapturePlanEntry.screen` with
`auto_advance: AUTO_ADVANCE_TAP` — the phone already renders
per-entry screens and gates on the operator's tap
(`capture-page/js/main.js`). The plan builders
(`build_v2_capture_plan` / `build_v2_verify_capture_plan`) and the
parameterized `index_phase_map` (already used by `prepare_v2_verify`'s
`{1: PHASE_VERIFY}`) are the seams.

**Design contract:**

- **Phase shape:** CHECK (tap, design axis — unchanged) → MEASURE
  (countdown, design axis — unchanged: alignment/level/polarity and
  per-driver linearization content stay single-position by plan
  doctrine, "alignment is never cloud-averaged") → **CLOUD-MEASURE
  group**: N−1 additional summed sweeps at prompted positions (each
  `tap`) → fit → auto-apply (existing) → **CLOUD-VERIFY group**: M
  summed sweeps, first entry `on_apply`, rest `tap`.
- **Prompt copy:** the S0 kit's hand-width language is the validated
  reference (`captures/flat-linearization-20260725/s0-kit/s0_capture.py`,
  the `_prompt_position` table: "One hand-width LEFT of the mark",
  "one hand-width HIGHER than tweeter height", …). Product copy is
  plain-language, deliberately casual, never numeric-precision
  ("~10 cm" allowed as a parenthetical). Spread guidance encodes the
  plan's physics: ≥10 cm spread for HF decorrelation, at least two
  wide (~30 cm+) offsets for the LF edge (the plan's "≥30 cm LF
  spread is load-bearing" side-finding).
- **Defaults:** N = 8 cloud-measure positions (min 6 to proceed,
  max 12), M = 6 cloud-verify. One geometry-locked retry loop: when
  the group-end combine reports `geometry.locked` (and not
  `thin_evidence`-soft), the conductor prompts up to 2 extra
  wider-spread positions, once. Bounded, then proceed with the
  verdict disclosed. Operator may end the group early at ≥ min.
- **Per-position work is light; the heavy pass runs once per group.**
  Each cloud capture gets the existing per-capture QC (SNR/glitch/
  drift diagnostics already in the analyze seam) but **not** a ~40 s
  full analysis; the combine + screens + null gate run at group end.
  This keeps session wall-clock sane and matches the pure-function
  contract (`combine_positions` is a pure function of the retained
  per-position results).
- **Session budget:** the `SessionVolumePlan` walked-away ceiling
  (`DEFAULT_WALL_CLOCK_CEILING_S = 1800`) scales with plan length
  (bounded per-entry increment, hard max stated in code); the
  restore ladder ("exact" then "emergency" −60 dBFS) and the
  restore-once latch semantics are unchanged. A user who walks away
  mid-cloud still can never leave the speaker pinned at measurement
  volume.
- **Artifacts:** one evidence bundle per session (existing model);
  position captures land under the existing
  `capture_artifact_relpath(kind, group, role)` scheme with `group`
  as the position-group carrier (`cloud_01`…) + a position-metadata
  JSON (prompt text, index, timestamps, QC verdict).
  `PositionCapture.position_id` (spatial_combine) is fed from this.
  **Retention re-size:** cloud sessions are ~N× bigger; raise
  `DEFAULT_SESSIONS_MAX_BYTES` (256 MiB → 1 GiB) so two cloud
  sessions cannot evict all history; bundle-count cap stays 12.
  (Named corner-cut: full per-position WAVs are retained — the S0
  forensics lived on raw WAVs; disk is cheap, honesty is not.)
- **Resume semantics:** unchanged session-bound rule — a new relay
  session invalidates capture evidence (mic position unverifiable
  across sessions). A cloud group interrupted mid-way resumes only
  within the same relay session, else restarts.

**Acceptance:** conductor state-walk tests (fake seams) for the group
lifecycle incl. early-end, geometry-retry, budget scaling, resume/
invalidate; relay plan tests for N-entry tap plans
(`tests/test_capture_relay_plan.py` harness); endpoint tests through
the real `run_capture_plan` with a scripted phone driver — including
one full 15+-entry plan with a mid-cloud retake, exercising PR-3a's
capacity end-to-end. No behavior change to CHECK/MEASURE/apply/
restore paths (pinned by existing suites).

**Review scope line:** "the position-group choreography on this
branch (crossover_v2_flow.py, capture_relay, plan builders, phone
copy, and tests)."

---

### PR-4 — Live analysis wiring: combine + screens + null gate + spec in the flow
**Tier: Sonnet (wiring; the cores are shipped). Size: ~600–900 diff lines + tests.**

At cloud-group end, the conductor runs the honest-instrument pipeline
and persists/serves its results:

- Build `PositionCapture`s from retained per-position results (grids,
  magnitudes, IRs, `position_id`) → `combine_positions` →
  `identify_interference_nulls` → `evaluate_flat_spec` (spec curve,
  merged mask). The echo/detector band and the passband for PR-2's
  `signal_band_hz` derive from the **declared contract**: the summed
  system's swept band (`RoleBand.band` as composed), the tweeter's
  `usable_frequency_range_hz` / `measurement_band_hz` for the upper
  echo band — replacing `DEFAULT_ECHO_BAND_HZ`'s flat constant with a
  contract-derived value at the call site (the pure module keeps its
  parameter). The honesty instruments (detector, combiner, null gate,
  this wiring) add **no** class- or device-keyed branches; within this
  program's new surfaces, `driver_class` / `horn_coverage_deg` inform
  display copy only. Be aware the *existing* fit stack already
  dispatches on **declared** `driver_class` — `class_prior_limit`'s
  `_CLASS_PRIOR_FULL_TO_HZ` table inside `compose_envelope`, and
  `HF_CONTINUATION_POLICY` in `linearization_fit` — which is
  declared-contract behavior (the owner declared the class), not
  forbidden device taxonomy, and it stays. PR-6 must design with it
  explicitly (see PR-6).
- **The wiring contract (issue #1742 item 4), enforced in one place:**
  a single result-assembly function consumes exclusion mask AND
  `geometry.locked` AND the null registry together; no consumer reads
  the mask alone. `geometry.locked` → user-facing "spread the mic
  further" guidance (soft when `thin_evidence`).
- Persist to the bundle (`crossover_v2/<session>/cloud.json`:
  combined curves decimated for JSON, mask intervals, registry,
  geometry verdict, spec report) and to the durable v2 state for the
  status/envelope payloads.
- Surface: `/state` block (compact: spec verdict per band, excluded
  intervals count, geometry verdict) and a `jasper-doctor` check
  (flat, one `CheckResult`: last session's spec verdict + registry
  size + "not yet run" state).
- Close issue #1742 with this PR (item 1 landed pre-program in
  #1746; items 2–3 land in PR-2; item 4 lands here).

**Acceptance:** endpoint-level test walking a scripted N-position
session to a persisted cloud.json + status payload; corpus-replay
test feeding S0 curves through the exact wiring assembly (mask ∪
registry ∪ geometry consumed together — a test that deletes one input
must fail); /state + doctor contract tests; conventions suites stay
green.

**Review scope line:** "the live-flow wiring of the spatial cloud,
honesty screens, and spec evaluator on this branch."

---

### PR-5 — The spec-curve SSOT (S2's shared construction)
**Tier: Opus. Size: ~400–700 diff lines + tests.**

Per interpretation call (B): `combine_positions`' output becomes the
single spec-facing curve construction. Re-base onto it:

- `program_analysis._flatness_tracking` (the flatness gauge) —
  evaluated on the combined spec curve over the spec band with
  exclusion awareness, replacing its capture-grid/band-mean framing;
  `verify_inconclusive` semantics and the gate-window comparability
  rule (VERIFY gate ≥ MEASURE gate; validity floor clamps) are
  preserved and now also clamp the spec band's lower edge.
- The candidate/envelope gauges
  (`_candidate_octave_summary`, `_linearization_octave_rows`,
  `_flatness_details_lines`) — read the shared curve + `FlatSpecReport`
  instead of private re-derivations.
- The observe ledger's spec-facing summary — sourced from the same
  construction (the fit's internal envelope-grid ladder summary stays
  as fit diagnostics, relabeled so no surface presents it as "the
  measurement").
- **VERIFY tracking stays distinct** (measured-vs-predicted on the
  capture grid) with a comment stating why, per call (B). Baseline
  honesty, so nobody "fixes" the wrong thing: `_flatness_tracking`
  is *already* full-band (`validity_floor_hz →
  FLATNESS_VERIFY_HI_HZ = 16 kHz`) and **report-only**; the ~2·Fc
  band belongs to the integration *tracking* verdict
  (`overlap_band_hz`), which is what gates. PR-5's flatness change
  is therefore the framing swap above (capture-grid/band-mean → the
  shared exclusion-aware spec construction) plus first-class
  reporting of the spec verdict. The integration tracking verdict
  keeps gating apply/verify acceptance **unchanged**; the spec
  verdict becomes the instrument S3's loop will consume — S2's
  promise delivered without silently changing what gates today.

**Acceptance:** a frame-consistency contract test — the value shown
by the gauge, the ledger, the spec report, and VERIFY's flatness
block for the same session are byte-identical numbers from one
construction (kills the MEASURE-vs-VERIFY ledger-discrepancy class);
existing verify/tracking suites unchanged.

**Review scope line:** "the shared spec-curve construction on this
branch (program_analysis flatness re-base, gauge/ledger re-derivation,
and tests)."

---

### PR-6 — Correction doctrine: exclusions into the fit + the null registry + the carve-out
**Tier: Opus. Size: ~600–900 diff lines + tests.**

Per interpretation call (A):

- **Envelope terms.** New `ReasonCode`s in `compose_envelope`:
  `spatial_exclusion` (zero `allowed_depth_db` on honesty-masked bins
  — screen ∪ identified nulls, mapped onto each role's envelope grid
  across the role's swept band) and `position_stability` (allowed
  depth shrinks where `BandSpread` reports high cross-position
  spread; calibrate the shrink curve on the S0 corpus and state the
  regime). The fit thereby corrects the **envelope around identified
  nulls and never fills them** — the cut-only, non-positive-gain
  invariants are untouched and re-validated downstream as today
  (emission re-proof stays). **Design WITH the existing class
  prior:** `compose_envelope` takes `np.min` across terms, and
  `class_prior_limit` (keyed by declared `driver_class`) already
  bounds HF allowed depth — for an **undeclared** class (`unknown`,
  `full_to` 6 kHz) allowed depth reaches 0 dB by ~12 kHz *before*
  any new term exists. The new terms compose through the same
  `np.min` (they can only narrow allowed depth, never widen it), so
  every 8–16 kHz behavior statement in this PR is
  **class-regime-dependent** and must be stated as such.
- **The null registry is the exclusion reason of record.** Persist
  `InterferenceNullReport` with the candidate
  (`candidate.json` + bundle), thread τ/r/classification through to
  the report payloads. A registry entry is *why* a band was excluded;
  the UI and any future session can read it.
- **Carve-out mechanics (owner decision 1):** `evaluate_flat_spec`
  already excludes masked bins from reference + deviation; the
  report's per-band verdict discloses carved-out intervals with
  plain-language reason + τ/r in the expert layer. The 8–16 kHz
  table number stays ±2.5. Update the plan doc's spec-table
  annotation (the "in progress, not resolved" note) **and** its open
  question 8 — both record this decision; leave neither dangling —
  in this PR, same-change docs rule.
- **Convergence guard for S3:** the residual metric the closed loop
  will use (RMS over non-excluded spec-band bins of the combined
  curve) lands here as a pure function next to `evaluate_flat_spec`,
  so S3's loop policy has its instrument ready.

**Acceptance:** corpus-replay: with S0 cloud + registry **and the
JTS3 declared classes** (tweeter `compression_horn`), the fit's
proposed correction spends **zero** gain inside identified-null
intervals and corrects the surrounding envelope where the class
prior allows; a second case with `driver_class` unset pins the
`unknown`-class regime (correction above the class-prior taper stays
refused by the *existing* prior, the new terms only narrowing);
predicted-sum ripple does not regress vs the no-mask fit outside
excluded bands;
cut-only invariant suites unchanged; spec-report carve-out disclosure
contract test; plan-doc annotation updated in the same PR.

**Review scope line:** "the exclusion-aware fit envelope, null
registry persistence, and spec carve-out on this branch."

---

### PR-7 — Visualization on jts.local
**Tier: Sonnet. Size: ~600–900 lines (ES module + payload + tests).**

The crossover wizard currently renders **no charts** (text
measurement-rows only — its lone runtime canvas is the QR code via
`/assets/shared/js/qr.js`; the only *chart* canvas in the correction
surface is the legacy room page). Add:

- **Before/after overlay:** the combined cloud spec curve pre-apply
  (CLOUD-MEASURE) vs post-apply (CLOUD-VERIFY), 1/3-oct, with
  excluded intervals visually distinguished (hatched/dimmed) and the
  spec-band tolerance corridor drawn. Decimated data ships in the
  envelope payload via `json_island` (kit precedent:
  `_decimate_for_json`); reuse `analysis.before_after_delta` /
  `before_after_fill_segments` where they fit.
- **Anomaly callouts, plain language first:** e.g. "Interference
  nulls at 8.4, 11.5, and 15 kHz — a delayed copy of the sound
  arrives 0.32 ms late. EQ cannot fill these, so they are excluded
  from correction and grading. They stayed at the same frequencies at
  every mic position — consistent with a reflection that travels with
  the speaker or a fixed path in the room; moving the speaker and
  re-measuring would tell those apart." τ/r/n and per-position
  numbers live in a `<details>` expert disclosure. Copy comes from
  classification vocabulary only — no hardware guesses ("horn rim"
  never appears in shipped copy; that is JTS3 knowledge, not measured
  general truth).
- **Geometry verdict UI:** `geometry.locked` renders the
  spread-the-mic guidance; `thin_evidence` softens it.
- **Conventions:** `canonical_page` page already; new ES module(s)
  under `deploy/assets/correction/js/crossover/` (relative imports),
  `escapeHtml`/`h()` from shared modules, no inline JS, no native
  dialogs; `install.sh` web-assets manifest picks up new files;
  conventions + design-system tests must pass. Chart is a `<canvas>`
  drawn by module code (room-page precedent), responsive per the
  wizard rules, light/dark aware via CSS tokens.

**Acceptance:** payload contract tests (decimation, escaping, island
shape); rendered-HTML tests for callout copy incl. the
cannot-classify phrasing; conventions/design suites green; an
on-device browser pass is listed in the HW session checklist (charts
verified against a real session before the program is declared done —
CI cannot see pixels).

**Review scope line:** "the before/after visualization and anomaly
callouts on this branch (payload, ES modules, copy, tests)."

---

### PR-8 — Docs + parity closeout
**Tier: Sonnet. Size: ~200–400 lines docs diff.**

- `HANDOFF-crossover-measurement-v2.md`: position groups, cloud
  phases, new payloads, budget scaling, retention change — per the
  touched-subsystem rule (each earlier PR already scanned; this PR
  sweeps for drift across the set).
- `flat-linearization-plan.md`: mark S1b/S2 stages landed with PR
  numbers (annotation style, never rewriting history);
  this doc gets its status updated per stage as PRs merge (the
  executing session keeps it truthful).
- README doc atlas + `docs/doc-map.toml` entries verified for the new
  module/doc surface.
- **Kit parity (laptop-side, NOT a PR — `captures/` is gitignored):**
  fix `grade_prediction_6` (energy discriminator ordered before the
  `clean_no_echo` branch, or `clean_no_echo` also requires collapsed
  energy) and let a leg-B-only session dir load a leg-A reference
  from a sibling session; re-run both SCORECARDs; prediction 6 must
  flip to the FAIL the plan doc § c documents. Where the kit
  duplicates product logic that W1 shipped, prefer importing the
  product module (the kit already `pin_jts_root`s the repo).

**Review scope line:** "the docs/parity closeout on this branch."

---

## Hardware runbooks (owner-present; pre-registered before first sound)

Shared rules: JTS3 (`PI_HOST=jts3.local`, passwordless sudo, lab box
— safe to test freely); UMIK-2 with its by-serial cal; **macOS mic
check first** (2 s sox capture; flat −96.3 dBFS = permission not
granted); archive the evidence-bundle dir before multi-capture work
(12-bundle eviction); the Pi's verify analysis takes ~40 s — never
poll with short deadlines; **AskUserQuestion ping before any audible
playback**. Predictions get concrete numbers committed to the session
directory **before** the first sweep; the kit's scorecard pattern
(pre-registered grader, graded verdicts, no post-hoc "held") is the
template.

### RB-1 — Per-driver lobing discriminator (~30 min)

**Question:** is the 1.8 kHz dip crossover vertical lobing
(summation-only), a woofer-intrinsic feature, or mic-geometry
interference? A summation null **cannot exist in either driver
alone** — that asymmetry is the discriminator. It must also reconcile
the pre-S0 forensics note that an old woofer-ALONE capture showed
−9 dB @ 1712 Hz (each hypothesis must state what it predicts for
that observation).

**Protocol:** desk geometry, tweeter height (where the dip measured
10.7 dB), ≥3 mic positions from the s0-session-main prompt table so
positions are comparable. Per position: per-driver MEASURE sweeps
(`sweep_w`, `sweep_t`) **and** a summed VERIFY sweep at the identical
placement.

**Capture path (small pre-work item for the executing session):** the
S0 kit's Pi path is the VERIFY-only re-verify endpoint; per-driver
sweeps need a MEASURE-composition program played **without** the
apply step (MEASURE auto-applies in the product flow). Extend the kit
with a measure-only runner through the existing program-composition +
admitted-playback seams — constraints: no DSP graph mutation, driver
ceilings and declared bands honored via the existing admission path,
no apply reachable. Review-gated like everything else; it is a lab
tool, so it lives in the kit, not the product.

**Pre-registered prediction template (numbers finalized at session):**
- P-A (lobing): summed dip ≥6 dB in 1.7–1.9 kHz at tweeter-height
  positions; neither driver alone shows >3 dB feature within ±1/6 oct
  of the summed dip frequency at the same position.
- P-B (woofer-intrinsic): woofer-alone reproduces the dip within
  ±1/12 oct and within 3 dB of the summed depth, tracking the summed
  dip position-to-position.
- P-C (mic-geometry interference): the dip appears in woofer-alone
  AND summed, frequency shifting with position consistently with a
  single τ ladder (and the null-ID gate, run offline on these
  captures, attributes it).
- Decision rule: exactly one of P-A/P-B/P-C's signature holds →
  classify; mixed → report the numbers, no forced verdict.
  Consequence: P-A confirms exclusion-not-correction + motivates
  vertical-geometry guidance copy (generic, not JTS3-specific); P-B
  makes it correctable content (envelope permitting); P-C validates
  the gate on a second real mechanism.

### RB-2 — Flush-capsule ground-plane redo (optional leg, same visit)

**Question:** a comb-free top-octave reference (currently only
bounded 2–8 dB; "tonight produced no comb-free top-octave
measurement").

**Protocol fix over S0 leg B:** the capsule must be **at** the
boundary, not proud of it — S0's "mic lying on the floor"
manufactured its own dominant 125–146 µs arrival, **4.3–5.0 cm of
path** (consistent with the capsule sitting centimeters proud of the
boundary; r up to 0.93), making the floor the *worst* HF reference
of the night. Mount so the capsule center is ≤~8 mm off the hard surface
(flush board/plate extending the floor plane; no soft materials —
the no-props UX ruling applies to product UX, and this is a lab
protocol, but keep it to rigid surfaces anyway). Speaker on the hard
floor tilted so the design axis aims at the capsule, ~1 m. **≥5
positions**, N=2 sweeps each.

**Pre-registered predictions (template):**
- G-1: no arrival in ~100–160 µs above −15 dB re direct on ≥4/5
  positions (the proud-capsule signature is gone).
- G-2: the ~320 µs arrival is **still present** (source-fixed
  confirmation; its absence would reopen attribution).
- G-3: position-to-position agreement ±1 dB, 300 Hz–8 kHz, on ≥4/5
  pairs (the S0 gp_03-style outlier rule: one outlier is reported,
  not averaged in).
- G-4: with identified nulls excluded, the top-octave residual reads
  inside the 2–8 dB bound — the first honest top-octave number.
- Success → ground plane becomes the documented **lab/advanced
  one-time top-octave reference protocol** (the cloud remains the
  product flow; nothing in W1–W5 gates on this).

### Product smoke (same visit, after PR-3b + PR-4 + PR-7 merge + deploy)

One full cloud session on JTS3 driven from a phone: choreography
prompts read naturally, budget holds, cloud.json lands, the
before/after chart renders with the comb called out in plain
language, doctor check green. This is the hardware validation for
PR-3b/PR-4/PR-7 that CI structurally cannot provide.

## Risks and named corner-cuts

- **Session length vs the walked-away guarantee** is a real tension;
  PR-3b's budget scaling is the mitigation, and N defaults (8/6) are
  chosen for wall-clock, not statistical perfection — S0's stability
  data (6-of-10 subsets) says more positions is better; the
  geometry-retry loop is the honest escape hatch, and N is a
  constant, not a promise.
- **Retention bump (256 MiB → 1 GiB)** trades SD space for forensic
  honesty; named above.
- **The fit stays anchored on the design-axis per-driver curves**
  (interpretation call A) — the recorded trigger for revisiting is
  S3 closed-loop evidence, nothing else.
- **VERIFY tracking not unified** (interpretation call B) — scoped on
  purpose; the frame-discrepancy class dies at the spec-facing SSOT.
- **Big-file churn:** `correction_setup.py` (7.4 k lines) and
  `correction_crossover_v2.py` (3 k) move fast on `main`; PR-3b
  through PR-7 are sequential, rebased before push, never long-lived.
- **PR-3a is a cross-deployment protocol change** (Pi + the deployed
  relay Worker + capture page) — the one place this program touches
  infrastructure outside the Pi deploy path. Capacity lands
  Worker/page-first, Pi second, gated on the negotiated protocol
  version; the un-updated-Worker skew case fails closed (plan
  refused with a clear error), never mid-session.
- **`driver_spacing_m` is inert (0.0) today** — vertical-lobing
  *prediction* from declared geometry is possible future work and
  deliberately **not** in this program (80/20; RB-1 measures instead
  of modeling).

## Not in this program

S3's closed-loop iteration policy (convergence/rollback loop itself),
S4 role-count generalization (#1703), the near-field 400–1500 Hz
instrument, horn redesign (owner's parallel hardware project, see
`captures/flat-linearization-20260725/RESEARCH-PROMPT-horn-redesign.md`),
and anything on the adjudicated do-not-relitigate list.

---

*Status: authored 2026-07-25 (Fable architect session); one
independent Opus adversarial review round applied — 3 blockers,
5 should-fixes, 8 nits, all fixed (notably: the PR-3a relay-capacity
split — the protocol caps plans at 8 entries; the existing
`driver_class` envelope-prior interaction PR-6 must design with; and
the τ-ladder-vs-arrival 4–9 % gap PR-1's matcher must admit). PR
ladder not yet started. The executing session updates per-PR status
here as merges land.*

*Last verified: 2026-07-25*
