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

> **UX surface superseded (2026-07-27), instrument unaffected.** This
> work order's PR-6/PR-7 shipped the wizard/capture-page UX described
> below as of that PR ladder: a single 16-capture instrument, per-group
> "Spot i of n" screen copy, and no tier choice. The **flow-simplification
> work order**
> ([`flat-linearization-flow-simplification-plan.md`](flat-linearization-flow-simplification-plan.md))
> subsequently replaced that UX surface — the one-instruction-per-step
> screen grammar, an Express tier alongside Full, and the wizard's tier
> chooser — without touching the honesty machinery this doc productized
> (`combine_positions`, the null identification method, `detect_echo`,
> the spec-flatness gauge). The tier capture counts moved again after
> flow-simplification shipped, with the two-stage commission split
> (#2400 — re-verified 2026-08-13); current counts are in
> [`HANDOFF-crossover-measurement-v2.md`](HANDOFF-crossover-measurement-v2.md)
> "What it is", not restated here. Read that plan for the current UX;
> read this doc for the instrument it still accurately describes.

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
  out-of-repo action: PR-3a's relay-Worker deploy (release ordering in
  that PR; *no capture-page deploy — the live page has no plan-length
  cap, see PR-3b's annotation*). Audible playback happens only in
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

*(Mechanism deviation, recorded: the pre-registered gate —
"the session's negotiated protocol version" — is structurally
incapable of catching the skew it was written to catch, and PR-3a
shipped a different one. `capture_protocol_version` is negotiated
between the **Pi and the capture page**: the page advertises
`supported_capture_protocol_versions` and the Pi validates it from the
phone's identity event. The **Worker never participates** — it treats
`capture_spec` as an opaque string it never parses (its own load-bearing
invariant #1), so it neither reads nor reports a protocol version. A
fresh page plus a stale Worker therefore passes protocol negotiation
and still rejects blob index ≥ 8 mid-session, which is exactly the
failure the contract forbids. Shipped instead: a Worker capability
surface, `GET /capabilities` →
`{schema_version, max_capture_plan_attempts}`, whose **absence is the
version signal** — a Worker predating it 404s, and the Pi reads that as
the frozen pre-capacity ceiling of 8. It is probed **before**
`POST /sessions`, so a stale Worker never receives the oversized spec
at all, and only plans above the legacy ceiling probe, so the shipped
3-entry and 1-entry flows keep byte-identical wire bytes. The
fail-closed property the contract asked for is preserved and
strengthened; only the mechanism changed.)*

*(Follow-on, 2026-07-27: capture protocols 1 and 2 were deleted, so the
"negotiated protocol version" this annotation critiques is now a
single-value equality check — the pre-registered gate is not merely
mis-aimed at the Worker, it can no longer vary at all. The shipped
`GET /capabilities` gate is untouched and remains the real mechanism;
`LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS` stays, because it describes the
deployed Worker's blob-index ceiling, an axis independent of the page
protocol.)*

**Acceptance:** spec-layer tests for >8-entry plans and the skew
refusal; Worker-side test for the widened index space; the existing
3-entry and 1-entry (re-verify) flows byte-identical.

**Review scope line:** "the relay capture-plan capacity change on
this branch (capture_relay/spec.py, relay/src/worker.js, version
gating, and tests)."

---

### PR-3b — Conductor position-group choreography (S1b, the instrument's front half)
**Tier: Opus. Size: ~800–1200 diff lines across conductor/relay/web/phone copy + tests. Needs PR-3a merged AND its Worker deployed.**

*(Pre-registered as "Worker/page deployed"; **Worker only** as shipped.
PR-3a verified that the deployed capture page needs no redeploy for
capacity: `capture-page/js/main.js` reads
`maxAttempts: Math.max(1, Number(plan.max_attempts) || 1)` and renders
`Measurement ${index} of ${target}` generically, with no plan-length
cap anywhere in the page, so the live page already handles a 21-entry
plan. No page file changed in PR-3a and `version.json` is untouched.
A page deploy re-enters the picture only if PR-3b itself changes page
code — which its own design contract says it does not need to.)*

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
- **Prompt copy:** the S0 kit's hand-width language was the validated
  reference (`captures/flat-linearization-20260725/s0-kit/s0_capture.py`,
  the `_prompt_position` table: "One hand-width LEFT of the mark", …).
  **Superseded 2026-07-28/29** by two owner field rulings (issues #1805
  and #1806, executed as the two-stage work order's D7): product copy is
  numeric — inches with the metric value beside it — and every prompt is
  an ABSOLUTE pose measured from the mark rather than a delta on the
  previous one. Spread guidance still encodes the same physics, and now
  encodes it as DATA rather than as prose: each row carries an
  `offset_cm` and `wide` is computed from it (≥10 cm spread for HF
  decorrelation, at least two wide ≥30 cm offsets for the LF edge — the
  plan's "≥30 cm LF spread is load-bearing" side-finding).
- **Defaults:** N = 9 cloud-measure positions (min 6 to proceed,
  max 12), M = 6 cloud-verify (`DEFAULT_CLOUD_MEASURE_POSITIONS` /
  `DEFAULT_CLOUD_VERIFY_POSITIONS`, `crossover_v2_flow.py` — N counts the
  MEASURE anchor plus 8 prompted positions, which is why the default is 9
  and not 8). The "Position groups" section that used to be cited here now
  sits inside `HANDOFF-crossover-measurement-v2.md`'s tagged-historical
  appendix — a shipped stage 1 no longer walks a pre-apply cloud at all. For
  the walk that ships, read that doc's live-spine "The capture flow" and
  "What it is". One geometry-locked retry loop: when
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

*(Two mechanism deviations, recorded. **(a) The group is FIXED-LENGTH; there
is no operator early-end.** The relay's plan runner completes a set at
exactly `capture_target` accepted captures — `index == accepted_count + 1`
and the ordinary terminations are target-met or attempts-exhausted
(`_poll_capture_plan`). Since #2097 a host verdict can also set
`terminal=true` when the final allowed position attempt proves the set cannot
reach its required floor; that ends the whole set honestly but does not make a
short group operator-selectable. A variable-length group is still not expressible
without changing that shared runner AND giving the phone an affordance to
signal "I'm done", which this PR's own design contract rules out ("no new
phone-side prompt mechanism"). N and M are therefore chosen
at plan-build time and validated against `MIN`/`MAX`; the min/max remain
meaningful as the range a caller may configure. **(b) The geometry retry is a
bounded RETAKE of the group's last position, not two appended positions** —
the same protocol reason: a rejected capture is the one lever that keeps a
plan alive at the same index. A "and replacing is better physics" claim was
made here in round 1 and is WITHDRAWN: the reviewer computed the power-mean
counterexample, where appending a wide position to a clustered cloud fills a
−15 dB null further than replacing does (−6.1 dB vs −7.7 dB) and lowers
`clustered_fraction` more besides. Replacing is what the protocol permits,
not what the estimator prefers. Both deviations are bounded by
`GEOMETRY_RETRY_POSITIONS` and disclosed. The pre-registered relay capacity
sizing is unaffected: the shipped plan is 16 entries / 23 attempts, worst case
19 / 26, against the relay's 32.)*

*(**A page change WAS needed after all** — the pre-registered contract's "no
new phone-side *prompt* mechanism is needed" was true of the prompts and
false of the retry path, and round-1 review falsified it: the deployed page
extracted only `accepted`/`error` from `capture_result`, so the geometry
retake's own guidance never reached the operator and every retake happened at
the same spot. `capture-page/js/main.js` now forwards `reason`/`banner`/
`prompt`/`code`, the bounded-retry `terminal` outcome, and an unresolved
position's diagnosis, and renders the server-supplied guidance fail-soft in
both directions. Final-position `unresolved` also rides
`capture_set_complete` so last-write-wins completion cannot erase it. Source +
tests ship in PR-3b/#2097. The public page and Pi rollout remain separate:
follow the page-first fixture and rollback order owned by
`capture-page/README.md`; repository merge alone is not that deployment.)*

*(**N raised 8 → 9** (adjudication 3a, round-1 review) so the delivered curve
rests on 8 summed sweeps — fundamental 1's floor is a count of CURVES, and a
group's anchor contributes none. Entries 15 → 16, attempts 22 → 23, ceiling
3240 → 3360 s under the 3600 s cap.)*

*(One design contract question the section left open, answered here: the
**verify-only re-arm plan stays 1 entry** and byte-identical on the wire. It
re-runs the single-position tracking verdict, and §5.6's session-binding rule
means a new session's captures could never join the original cloud anyway —
so a cloud there would be a second, unrelated cloud, not a resumption.)*

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
  program's new surfaces, `driver_class` informs display copy only
  (`horn_coverage_deg`, named here alongside it, was deleted by #2872).
  Be aware the *existing* fit stack already
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
  *(PR-6b, 2026-07-27: shipped in two steps on one branch. The report-payload
  half went first. The `candidate.json` half and the fit wiring below were
  BLOCKED on a session-ordering fact this work order did not account for —
  `_fit_linearization` ran at capture index 2 while the pre-apply cloud group
  closes at index 10 — and are now unblocked by the owner-approved timing move
  that follows. Both halves are shipped; full accounting in the PR-6b
  paragraphs at the foot of this doc.)*
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
  *(As shipped: **Worker-first, Pi second** — no page deploy, and the
  gate is the Worker's own `GET /capabilities` document rather than the
  protocol version, which the Worker never sees. Full rationale in the
  PR-3a design-contract annotation above; the fail-closed guarantee is
  unchanged.)*
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
the τ-ladder-vs-arrival 4–9 % gap PR-1's matcher must admit). The
executing session updates per-PR status here as merges land.*

*Ladder status: **PR-2 merged as #1749**; **PR-1 merged as #1751**;
**PR-6a merged as #1753** (the owner-approved fit-side fast-track
described below); **PR-3b merged as #1755**; **PR-4 merged as #1756**
(closing issue #1742); **PR-5 merged as #1757**; **PR-6b merged as #1760**
— both halves: the carve-out disclosure (own paragraph below) AND the
blocker it hit, resolved by the owner's timing-move decision (the fit now
runs at CLOUD_MEASURE group close instead of MEASURE accept), not left
reported-and-blocked; **PR-7 merged as #1761** (before/after cloud
visualization + anomaly callouts — see its own paragraph below for the
two review-confirmed deviations from its section's literal wording).
**PR-8 (docs closeout) — this PR.** Kit parity fixes executed
laptop-side, not part of this repo diff (`captures/` is gitignored):
`grade_prediction_6` flipped to the FAIL "S0 executed" § c documents,
with a third refusal shape (`earlier_dominant_arrival`, shipped by PR-2
after § c was written) added to the same energy-discriminator gate as
`clean_no_echo`/an edge refusal; a leg-B-only session directory now
borrows its leg-A energy reference from a sibling session via a new
`--leg-a-session` flag; `_confident_taus`/`effective_floor_us` now
import the shipped `usable_echo_estimates`/
`EchoDiagnostic.effective_floor_us` rather than re-deriving them. Both
SCORECARDs re-run and verified; kit README updated in place. **PR-3a**
raises the relay capture-plan cap 8 → 32
and ships a **mechanism deviation** from its pre-registered design
contract: the gate is the Worker's own `GET /capabilities` document
(absence = pre-capacity relay), not the negotiated protocol version,
which the Worker structurally never sees — annotated in place in the
PR-3a section above. It needs a **Worker deploy only**; the live
capture page has no plan-length cap.*

*Owner-approved split (2026-07-26): PR-6's fit-side half (envelope
terms + convergence guard) fast-tracked as **PR-6a** ahead of W2 to
unblock the corpus-derived profile; registry persistence, carve-out
disclosure surfaces, and the spec-table/open-question-8 annotations
remain **PR-6b** in ladder order. The PR-6 section body below is
unchanged and still describes the whole of PR-6. (PR-6b shipped all of it —
the carve-out disclosure and both annotations first, then, after an
owner-approved timing move cleared a session-ordering blocker, the fit wiring
and the registry-into-the-candidate. See the PR-6b paragraphs below.)*

*PR-2 review (2026-07-25) — three rounds. R1: 1 blocker — the
`earlier_dominant_arrival` refusal had no dominance criterion, so the
band-limited envelope's own ringing qualified as an "arrival" at raised
windows (21 of 660 echo-free readings flipped into a false refusal, on
the committed `_CALIBRATION_WRONG_READING_WINDOWS` ladder — the earlier
"22" was a mixed-ladder figure R2 itself retracted);
calibrated to `EARLIER_ARRIVAL_DOMINANCE_DB = -10 dB` against a measured
14.58 dB gap between genuine early arrivals and ringing. R2: 1 blocker,
4 should-fixes — stale prose still denied the very threshold R1 had just
added, and the calibration sweep behind that threshold was uncommitted
(evidence asserted without a pinned artifact). R3: mechanical closures
only.*

*PR-1 review (2026-07-25) — one round: 0 blockers, 3 should-fixes, 5
nits. The `MIN_LADDER_RUNGS` counterfactual the section's own acceptance
criteria named could not be reproduced as stated and was re-derived as
the actual failure mode: a single-rule mutation that removes only the
1.8 kHz dip from the candidate set. A stale sibling docstring was caught
at architect closure.*

*PR-6a review (2026-07-26) — one round: 0 blockers, 4 should-fixes, 3
nits. All six design adjudications from the PR-6a section upheld:
σ/√N pooling, post-smooth exclusion ordering, any-overlap
rasterization, the honest zero-gain restatement, the convergence-guard
derivation, and the doc paragraph describing it.*

*PR-3b (2026-07-26): the position-group choreography lands, and the
**shipped main-session plan becomes the 16-entry cloud** — the intended
product change (fundamental 1: the cloud IS the measurement), so the
3-entry golden wire-byte pin was updated by its own documented procedure
while the 1-entry re-verify pin stayed byte-identical. Two mechanism
deviations (fixed-length groups, retake-based geometry retry) are
annotated in the PR-3b section above. The combine call is minimal
(geometry verdict only) behind `combine_cloud_positions` /
`cloud_geometry_verdict`, which PR-4 extends as a consumer rather than
replaces; the gated-IR assembly those functions perform is
corpus-validated against the S0 reference construction rather than
asserted.*

*Round-1 review (2026-07-26) — 3 blockers, all phone-facing, all fixed on
the same branch: the consent surface still promised a stationary mic (now
parameterized by plan shape, with its own `summed_guided_cloud_v1` policy
id; the stationary copy and policy stay reachable and byte-identical for
the 1-entry re-verify); the geometry-retry instruction never reached the
phone (page extraction + render fixed, see the page annotation above); and
a retake's evidence sidecar collided with the take it replaced, leaving the
surviving record describing the wrong capture (artifacts are now qualified
by attempt, the prompt recorded is the one actually shown, and the
retention seam is tested through the REAL write-once store). Behavioural
follow-ons: geometry asks consume the same pooled position extras as every
retry — two conductor asks leave one household retry, with no discount or
separate quality budget — and a corrupt `session_phases` list can no longer
read as "done". Also corrected in the
accounting below: deviation 5's "structurally identical" list omitted the
MEASURE "this spot is the mark" addition and the relocation of the END
screen's `done_title`/`done_body` from the VERIFY entry to the last
cloud-verify entry — both behavioural-adjacent copy/shape changes on
paths the deviation claimed were untouched.*

*PR-4 (2026-07-26): the live-flow wiring lands — combine → identify_
interference_nulls → evaluate_flat_spec, assembled by one new function,
`assemble_cloud_group_result` (issue #1742 item 4's single consumer of
mask ∪ geometry.locked ∪ null registry), called once per closed group from
`_close_cloud_group`. Contract-derived bands: `signal_band_hz` is the union
of both roles' `RoleBand.band` (`_composed_swept_band_hz`, new — no prior
function composed across roles); `echo_band_hz` is the tweeter's confirmed
`measurement_band_hz` (new `excitation_safety_plan.resolve_driver_
measurement_band_hz`, since `resolve_driver_excitation_ceilings` validates
that field internally but returns the excitation ceiling, a different
quantity), clamped inside the passband and disclosed (not overridden) via a
WARNING log when its lower edge falls below `ECHO_BAND_HF_REGIME_FLOOR_HZ`
(4000 Hz — the last comfortable row of `BAND_BELOW_PASSBAND_MARGIN_DB`'s
own pinned six-band table, `test_band_deficit_separation_depends_on_the_
analysis_band`, N-3's own note that PR-4 would derive this from the
tweeter's declared range). **The "disclosed (not overridden)" half of that
sentence was SUPERSEDED on 2026-07-27 — the band is now clamped up to the
floor and the clamp disclosed; see the issue #1763 annotation below.**

**VERIFY-anchor join: proposed, then REJECTED (2026-07-26, architect
reversal).** The round-1 draft of this PR joined VERIFY's own summed capture
into the cloud-verify combine (M positions yielding M curves instead of
M-1), reasoning fundamental 1's floor is a count of curves. Reversed on
review: the post-apply cloud would then contain the on-axis design
point — the exact axis the fit optimizes — at 1/6 weight while the
pre-apply cloud contains none, an undisclosed asymmetry in precisely the
two curves PR-5 re-bases and PR-7 charts as before/after. The evidence
gain (+1 curve of 6) does not outweigh biasing the comparison surface, and
M is deliberately sized BELOW the "more is better" floor for wall-clock
reasons (see `DEFAULT_CLOUD_VERIFY_POSITIONS`'s own "smaller on purpose"
comment) — fundamental 1 does not actually require this. The post-apply
cloud combines the M-1 prompted curves exactly as PR-3b shipped; the
anchor stays tracking-only. A future SYMMETRIC design — adding an anchor
summed sweep to MEASURE too, so both clouds carry the on-axis point at the
same weight — is a legitimate follow-up if ever wanted, but asymmetric
inclusion (post-apply only) is not.

Persisted to
`crossover_v2/<session>/cloud_measure.json` / `cloud_verify.json` (**a
mechanism deviation from the work order's literal singular `cloud.json`**:
the evidence store is write-once and the two groups close at genuinely
different times in one session, so a single shared path would collide on
the second write — `bind_cloud_publisher`'s own docstring). Surfaced at
`/state` (compact per-band pass/fail + excluded-interval count + geometry,
`crossover_v2_status_block`'s 9th key), the envelope (same compact
projection plus the geometry guidance copy), and a new flat `jasper-doctor`
check (`check_crossover_v2_cloud_pipeline`). Closes issue #1742 (items 2-3
landed in PR-2, item 1 pre-program in #1746, item 4 here).*

*Round-1 review (2026-07-26) — 1 blocker, 4 should-fixes, 6 nits, all fixed
on the same branch; the VERIFY-anchor join was reversed (see above). B1
(blocker): the verify re-arm's conductor has no group phase in its own
session, so the durable `cloud` block's session-id-gated carry-forward
(mirroring `candidate`/`evidence`) blanked a real prior cloud verdict on
the first "Try again" tap — fixed with an unconditional carry-forward when
the conductor's own session has no group phase, mirroring
`pre_apply_profile`'s existing unconditional pattern instead. S3: the
pipeline's "second combine, deterministically identical" design was
measured at seconds-per-combine (3-6 s across runs/hosts on the S0
ten-position corpus) and reversed to a single combine per group close. S4:
`assemble_cloud_group_result`'s "any exception is caught" docstring
overclaim corrected to name the actual caught family and state the residual
honestly; an outer wrap at the `_close_cloud_group` call site makes a
NAMED-family pipeline exception unable to cost the group its accept — the
residual (a KeyError, or anything outside that family) still propagates by
design. S5: two more "PR-4 renders it" overclaims (this doc and the module)
corrected to "PR-4 carries it; PR-7 renders it".*

*Round-2 review (2026-07-27) — 1 blocker, 2 should-fixes, 5 nits, all fixed
on the same branch. BLOCKER: `check_crossover_v2_cloud_pipeline` warned on
ANY closed group's spec failure, including `cloud_measure` — the PRE-APPLY,
uncorrected baseline that exists in order to be out of spec — so a
perfectly corrected speaker warned forever. Fixed to gate the warn on
`cloud_verify`'s verdict only; `cloud_measure`'s verdict still appears in
the detail text. SF-1: `_compact_cloud_status` defaulted
`excluded_interval_count` to `0` and `geometry_guidance` to `""` when the
pipeline never became available — `0` reads as a fabricated "no
interference found" rather than "unknown". Fixed: `None` when unavailable
(doctor prints `n/a`); `geometry_guidance` is now computed directly from
the geometry verdict, so a locked group's guidance survives an unrelated
downstream pipeline failure instead of disappearing with it. SF-2: the B1
carry-forward escaped `observe_restore`'s enumerated clears — an Undo left
`evidence.cloud_artifacts` behind for the next verify-only re-arm to
resurrect. Fixed by clearing `evidence` wholesale in `observe_restore`
(matching `reset_v2_journey_state`'s existing precedent of always nulling
it), not a surgical per-key delete. Nits: the outer-wrap comment's
"structurally true" claim narrowed to the named family it actually catches,
with a pinned test for the KeyError residual; the "5.6-6.2 s" figure
(measured 3.14 s on a re-run of the same corpus) restated as a 3-6 s regime
at all four sites, and the retry arithmetic corrected (`GEOMETRY_RETRY_POSITIONS
= 2` allows 3 close attempts, so the pre-fix worst case was 6 combines, not
"4x"); a stale "reads ONE field: geometry" bullet in the HANDOFF doc
brought in line with its own later correction; the `no_positions` /
`combine_failed` reason-string divergence between `cloud_geometry_verdict`
and `_geometry_verdict_from_combined` documented rather than silently left
inconsistent; and the B1 fix's inverse (a session WITH a group phase must
overwrite, not inherit, a stale prior cloud) pinned by test.*

***Behaviour change (2026-07-27, issue #1763): disclose-don't-override was
falsified by the first real cloud session and replaced with
clamp-with-disclosure.*** *The PR-4 design above — warn when the derived echo
band's lower edge falls below `ECHO_BAND_HF_REGIME_FLOOR_HZ`, then run the
detector on the declared band anyway — was reviewed and reasoned (never
silently trust an uncalibrated regime, never silently hide a real declared
value). Hardware settled it: on JTS3's first cloud session (2026-07-27,
`cap_4NUGqx3yIzSuv4ta2ozfKw`) the cdhorn tweeter's **correctly** declared
`measurement_band_hz` [2000, 18000] produced a (2000, 18000) analysis band,
fired the designed WARNING (`event=correction.crossover_v2_cloud_echo_band_
below_hf_regime derived_lo_hz=2000.0 floor_hz=4000.0`), and proceeded — so
that session's τ/r/registry outputs carry an uncalibrated-regime asterisk on
the one measurement that mattered (the new-horn r=0.175 result). The
derivation had been conflating two quantities: the driver's declared
operating/measurement WINDOW (excitation + SNR scoring, `measurement_band_hz`'s
own job) and the echo/null ANALYSIS band (a detector-calibration concern).
Disclosure alone does not keep a session inside a calibrated regime.*

*As shipped: `_derive_cloud_echo_band_hz` **clamps** the lower edge up to the
floor, keeps the contract-derived upper edge, and discloses the clamp both
ways — `event=correction.crossover_v2_cloud_echo_band_clamped_to_hf_regime`
carrying `derived_lo_hz`/`clamped_lo_hz`/`floor_hz`, and a JSON
`echo_band_provenance` block (`source`, `hf_regime_clamped`, `derived_lo_hz`,
`floor_hz`) riding the pipeline payload beside the applied `echo_band_hz`, so
a reader of the durable state or the bundle can tell a contract-derived band
from a clamped one **without** the journal. The band and its provenance travel
as one value (`_CloudEchoBand`), so they cannot be published apart. Degenerate
case, decided from the detector's own constants rather than picked: if raising
the edge would leave less than `GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 /
DEFAULT_ECHO_SEARCH_US[1]` = 3.0 × 1e6 / 800 µs = **3750 Hz** of width — the
width at which `assess_geometry`'s resolution floor reaches the TOP of the
searched window, so no delay the detector may look for could ever be
clustered — the band falls back to `DEFAULT_ECHO_BAND_HZ` under its own
`clamp_degenerate_default` reason. That bound dominates the detector's other
width constraint (`MIN_ECHO_BAND_BINS`, 16 bins of a 4096-point FFT = 175.8 Hz
at 48 kHz) by 21×, so one rule covers both. **The clamp costs no cross-session
comparability**: (4000, 18000) is 14 kHz wide, so its cepstral resolution is
1e6/14000 = 71.4 µs — identical to S0's (5000, 19000), also 14 kHz — and a
clamped session's τ ladder stays directly comparable to S0's. The pure modules
were not touched (this is a call-site derivation change); PR-2's
`signal_band_hz` passband stays the composed swept band, and the
passband-contains-analysis-band containment contract still holds with the
clamped band.*

*PR-5 (2026-07-27): the spec-curve SSOT lands. `combine_positions`' spec
curve, evaluated once by `evaluate_flat_spec` against the merged honesty mask,
reduced once by the new pure `flat_spec.spec_flatness_gauge`, and published as
`assemble_cloud_group_result`'s `flatness` key — which `/state`, the envelope,
the doctor detail, and the wizard's expert disclosure all **copy**, so the
frame-consistency contract (`tests/test_flat_spec_ssot.py`) is byte-identity by
construction rather than by two code paths agreeing. Interpretation call (B)
honoured: `_analyze_verify`'s measured-vs-`predicted_sum` comparator is
untouched and now carries the comment stating why (both sides of that
comparison share one design-axis geometry, which is the whole basis of the
claim; feeding it a spatially-averaged curve would read cloud variation as a
tracking error in the one gate that gates). No gating verdict changed —
`max_db_notch_excluded`, `verify_inconclusive`'s gate-comparability rule, and
the `overlap_band_hz` tracking window are all as they were.*

*Three mechanism deviations from the PR-5 section's literal wording, all
recorded rather than silently taken. **(a) `_flatness_tracking` was RETIRED,
not re-based in place.** The section says "evaluated on the combined spec curve
… replacing its capture-grid/band-mean framing", and the replacement cannot
live where the function lived: `_analyze_verify` runs per capture, the anchor
VERIFY capture is consumed BEFORE the cloud-verify group closes, and
`program_analysis` (an `audio_measurement` module) cannot import
`active_speaker.flat_spec` without inverting the layering. So the function,
`FLATNESS_VERIFY_HI_HZ` (superseded by `flat_spec.BEST_EFFORT_ABOVE_HZ`),
`FLATNESS_VERIFY_TOLERANCE_DB` (the never-bench-derived 3.0, superseded by the
spec table's per-band tolerances), `ProgramAnalysis.flatness_tracking`, its
`PhaseVerdict` relay, the conductor's `flatness_evidence` stash, and the
`verify.flatness` state key are all gone; the claim is made once per group on
the cloud. Keeping a per-capture flatness number under any name would have
preserved the exact second construction the PR exists to remove. **(b) The
candidate/envelope gauges had no private curve re-derivation to re-base.**
`_candidate_octave_summary` and `_linearization_octave_rows` are pure
projections of `LinearizationFit.observe_octave_summary`, and a summed cloud
curve has no per-role decomposition to re-derive them from. What was wrong was
the FRAME, so PR-5 applied the treatment the section's own next bullet
prescribes for the fit ladder: relabeled, in the docstrings and in the rendered
line ("`<role>` fit residual vs target (design-axis capture, not the spatial
measurement)" — the old text led with "measured"). `_flatness_details_lines`
IS re-based, onto the shared gauge. **(c) The gauge quotes spec-band BIN
counts, not an interval count.** An interval count spans the whole axis
including frequencies no spec band grades, so "N regions excluded from
grading" would have over-reported the moment the validity clamp removed a
sub-250 Hz region — caught by the contract test's own byte-identity assertion.*

*Measured regime for the validity-floor clamp, stated because it is not free:
`cloud_validity_floor_hz` takes the group's WORST (highest) gate floor, mirroring
`_measure_validity_floor_hz`'s existing "worse of the two branches" rule, and
those bins leave the spec evaluation (deviations AND reference — a
non-measurement must not re-centre the target either). On the S0 main leg, as
re-derived 2026-08-02 (#2045), **all ten** positions gate to 142.857 Hz, below
the 250 Hz spec edge, so the group's own floor makes the clamp a **no-op**.
(Until PR #1991 `cloud_04` reported a measured reflection at **1777.8 Hz** and
set the group floor; that reading was the detector firing early — the #1790
instance the prominence vote rejects.) Clamping at that floor supplied
explicitly still costs, all pinned by `tests/test_flat_spec_ssot.py`:
**987 bins** leave the
250 Hz–2 kHz band (7678 → 6691 graded); the reference re-centres
**−27.2386 → −28.3062 dB**; the **headline `max_db` moves −8.9389 → −7.8713 dB,
i.e. +1.0676 dB in the FLATTERING direction** — exactly the reference shift,
because the worst bin (15999.7 Hz) survives the clamp and its deviation tracks
the reference one-for-one, so the first number the ledger line prints moves
*further* than the RMS does; the pooled RMS moves 3.8031 → 3.1740 dB; and the
250 Hz–2 kHz **band verdict FLIPS**, +4.2458 dB (fail) → −1.2146 dB (pass),
since `passed` is `abs(max) ≤ tolerance` (overall stays False only because the
other two bands fail on their own). The **direction is response-shape dependent
and measured on this corpus only** — here the removed region sat above the
surviving reference, so dropping it flattered what was left; a speaker with a
quiet sub-floor region moves the other way. None of it is the speaker
improving: it is the same speaker on fewer bins, which is exactly why
`n_bins`/`n_excluded` ride on the gauge. The clamp is deliberately kept OUT of
`merged_excluded_bands_hz` (and so out of `/state`'s
`excluded_interval_count`), which stays the honesty instruments' own "how much
interference did we find" count; the clamp is disclosed separately as
`validity_floor_hz`, carried through `_compact_cloud_status` to `/state`, the
envelope, and the doctor so a live surface can separate a combed room from a
collapsed gate. A group with no usable floor clamps nothing and reports `None` —
withholding the whole gauge over an unverified lower edge would throw away the
2–16 kHz evidence.*

*Deferred alternative, recorded (review SF-3): the honest third option is
**per-position, per-bin validity masking inside `combine_positions`** — mask
each position's contribution below that position's OWN floor and combine the
survivors, so nine good captures keep contributing at 500 Hz instead of one bad
one costing the whole band for the group. It is strictly better than a
group-wide clamp and is deferred only because it is a `spatial_combine`
signature and estimator change (the power mean would need per-bin weights), not
a wiring one — out of PR-5's scope. **Revisit trigger:** a real session where
one collapsed gate meaningfully shrinks the graded band. The S0 `cloud_04` case
above already IS that evidence, so this is queued on measured grounds rather
than speculation; it is a scope call, not a doubt about whether it is worth
doing.*

*PR-6b (2026-07-27): owner decision 1's disclosure half lands, and the two
plan-doc annotations it owed are recorded. `carve_outs_by_band` re-reads the
SAME null registry and the SAME `evaluate_flat_spec` report
`assemble_cloud_group_result` already holds — per spec band, which ranges left
that band's grading, each row tagged with the instrument that carved it
(`identified_null` / `position_screen`), the registry's rows carrying
τ/r/rung/classification as the exclusion reason of record. It is a third
reading of one evaluation, never a second one: the bins are gone from `spec`
before it runs and no verdict can move. The rows ride the pipeline result into
`cloud_measure.json` / `cloud_verify.json`, through `_compact_cloud_status` onto
`/state` and the envelope (the one surface PR-7 will render from), and — the
part a household sees TODAY, without waiting for PR-7 — into the envelope's
`<details>` expert disclosure, where the excluded-bin COUNT that PR-5 shipped
now has the ranges and their τ/r beside it. The copy has ONE owner
(`crossover_v2_flow`, beside `_geometry_guidance_copy`) in two registers, a
plain-language `disclosure` headline and an `expert` line carrying τ/r, so a
chart callout and the expert disclosure cannot say different things about the
same range; a copy-discipline test pins the `position_invariant` wording to the
pre-registered travels-with-the-speaker-OR-fixed-path phrasing and keeps every
hardware noun out. The gate-validity clamp stays deliberately OUT of the
carve-out rows, disclosed separately as `validity_floor_hz` exactly as PR-5 left
it. Docs: the spec table's 8–16 kHz annotation and open question 8 both record
owner decision 1 as their resolution (annotated, never rewritten; the ±2.5 dB
number is untouched — the carve-out is disclosed, not re-specified).*

*PR-6b, the blocker it hit, and the owner decision that cleared it
(2026-07-27). The section's null-registry bullet asks for the registry to ride
`candidate.json` "whenever a fit consumed cloud-derived exclusions", and PR-6a's
own commit message names PR-6b as the PR that makes its two optional
`compose_envelope` arguments live. **Neither was reachable as the flow stood.**
`build_v2_cloud_index_phase_map`'s running order is CHECK 1, MEASURE 2,
CLOUD_MEASURE 3–10, VERIFY 11, CLOUD_VERIFY 12–16, and `_fit_linearization` was
called from `_build_candidate` ← `_measure_verdict` ← index 2 only — so the
pre-apply cloud group closed at index 10, **eight captures AFTER the fit that
was supposed to consume it**, and `candidate.json` was published (and auto-apply
fired) before any registry existed. A `_measure_verdict` re-arm re-runs index 2,
never a later index; a verify-only re-arm has no MEASURE at all; and §5.6's
session-binding rule forbids carrying a prior session's cloud into a new one.
The implementer refused to wire the arguments into a branch no production
ordering could enter, and reported instead — the finding was independently
re-traced and taken to the owner.*

> **Ordering note (post-R15).** The capture ordering above is the pre-R15 one.
> R15 ([#2106](https://github.com/jaspercurry/JTS/issues/2106)) removes the
> pre-apply cloud, so stage 1 runs CHECK 1, MEASURE 2 and stops; see
> [`crossover-linearization-80-20-plan.md`](crossover-linearization-80-20-plan.md).
> The narrative above is unchanged as the record of why the fit moved.

***Owner decision (2026-07-27): move the fit.*** *The fit, the candidate build,
and the auto-apply trigger relocate from MEASURE's accept to the CLOUD_MEASURE
group close — the work order's own pre-registered phase order. The root cause is
recorded as a **criteria conflict inside this document**: the pre-registered
architecture puts the fit downstream of the combined cloud
("fit engine: envelope terms from mask + spread"), while PR-3b's acceptance line
said "no behavior change to CHECK/MEASURE/apply/restore paths" — and the second
was read as binding when the two met. **Resolved in favour of the
pre-registered shape.** The 2026-07-20 auto-apply ruling's INTENT is preserved
in full: apply is still automatic, still needs no human tap, still fires off the
host's existing `consume()` seam — only the trigger point moves.*

*As shipped: `_measure_verdict` keeps **every** gate it owned (locate, glitch,
sweep-schedule, clip, linearity, alignment status, the alignment-confidence
trust floor, Fix-3 plausibility, and G1's predicted-ripple check — which since
the 2026-08-03 ruling on #2087 discloses rather than refuses, but still runs
here and still reads the analysis) because
every one of them reads the ANALYSIS, not the candidate — a session doomed at
sweep two still fails at sweep two rather than after a nine-position walk. The
one candidate-coupled failure that used to surface there, an analysis with no
`candidate` at all, is hoisted to the same capture as an identical raise, so its
observable behaviour (`internal_error`, at MEASURE) is unchanged.
`_close_cloud_group` then calls `_publish_measure_candidate` — the single
build/publish path,
shared with the pre-cloud shape — OUTSIDE the fail-soft wrap that protects the
diagnostic pipeline, because the candidate is the session's product and a
failure there is a real failure. **A session with no CLOUD_MEASURE phase (the
pre-cloud 3-entry shape the conductor still defaults to, and the 1-entry
re-verify path) is byte-identical to before:** the rule is "the fit runs at the
last capture before the apply", and for those shapes that capture is MEASURE.
Wire bytes, screens, plan entries, and the golden pins are all untouched — only
conductor-internal timing moved.*

*The wiring that was blocked now runs: `_cloud_fit_evidence` turns the closed
group's pipeline result into the merged honesty intervals plus the cloud's
`band_spread`/`n_positions`, and `compose_envelope` receives all three. It is
**all-or-nothing on the pipeline's availability**, deliberately: a failed
pipeline still leaves the power-vs-median screen's own intervals, and handing
the fit those alone is exactly the mask-alone read issue #1742 item 4 forbids —
the screen structurally cannot see a position-invariant null (0 of 5462 bins in
8–16 kHz on the S0 corpus), so a screen-only mask would exclude the interference
the cloud CAN see while silently correcting the interference it cannot. No
cloud verdict means no cloud terms, logged as
`event=correction.crossover_v2_fit_without_cloud` rather than degrading
silently. The registry rides the candidate as the new optional
`MeasuredCrossoverCandidate.exclusion_evidence` field — the excluded intervals,
the band spread, N, and the τ/r registry, enough to re-derive both envelope
terms from `candidate.json` alone — following `linearization`'s own
omit-when-empty / fingerprinted-when-present / era-tolerant conventions
exactly. Telemetry: the `linearization` field left
`correction.crossover_v2_measure_diag` (which is now emitted eight captures
before the fit and could only ever report `""`) for a new
`correction.crossover_v2_candidate_built` event, the same treatment PR-5 gave
the per-capture `flatness_*` fields when their subject moved to the cloud.*

*Round-1 review of the two PR-6b commits (2026-07-27) — 1 blocker, 1
should-fix, 7 nits, all fixed on the same branch; the timing move itself was
walked against six race/ordering scenarios and came back clean.
**BLOCKER: era tolerance.** `to_dict()` always writes `exclusion_evidence`, but
`from_mapping`'s reopen comparison only `setdefault`ed the two OLDER optional
fields — so every pre-PR-6b `candidate.json` straddling a deploy refused as
`candidate_tampered` on the LIVE apply route, telling a household their
correction had been altered when the file was merely older. Three docstrings
promised the tolerance the code did not deliver, and it is a verbatim
reintroduction of the P1 the candidate test module names at its own `:418`.
Fixed with the missing `setdefault`, the third era test mirroring the existing
two, an end-to-end apply test through the real `apply_baseline_profile` path,
and — because three hand-written era tests will not keep pace with a growing
field set — a structural guard that drops EACH optional key in turn, so a
fourth field added without its `setdefault` fails even if nobody writes its era
test. All four verified to fail with the fix reverted.
**SHOULD-FIX: retention.** The retained MEASURE analysis was stored on both
arms and never released. It is not small — one two-occurrence `DriverResponse`
measured 33.6 MB of ndarray payload on the S0 corpus grid — so it is now stored
only on the arm that defers, and released once the fit consumes it. Releasing
changes what a re-delivered close would do, which is safe because the relay
admits a begin only at `(accepted_count + 1, attempts_used + 1)` and dedupes
processed pairs; a geometry retake returns rejected long before the build. Both
facts are documented at the site.
**Nits, all seven:** the "accept is already decided" wrap comment now scopes
itself to the pipeline and names the honest journalled-but-unaccepted state a
candidate-build raise leaves (with a test); `exclusion_evidence`'s measured
`candidate.json` cost is disclosed (5,294 bytes on S0 — registry 3,307,
band_spread 1,596, intervals 287) as the `/state` projection's was; two
stale-spine design records annotated (`correction-journey-design.md`,
`bass-commissioning-ux.md` — the latter's own staleness correction had itself
gone stale); the "~40 s analyses" figure replaced with a measured 2.7-2.8 s
combine and 0.02-0.04 s pipeline; the standalone "byte-identical" phrase scoped
(the pre-cloud arm's candidate DOES gain an always-empty, non-fingerprinted
key); and the carve-out clamp-exclusion claim pinned structurally.
**One of the nits found a defect in this session's own test.** The severing
test claimed "the correction itself differs"; measured, the emitted biquads and
trims are IDENTICAL wired and severed — what the cloud changes on that fixture
is the fit's permitted band, its residual accounting, and which term the 8 kHz
octave reports as binding. The test now asserts `fit_band_hz` (a real fit
change, and the assertion that would fail if the wiring degraded to
reporting-only), asserts the filter equality explicitly, and says so. The
sibling positive test's no-filter-inside-a-null assertion is likewise annotated
as a standing invariant rather than that test's proof, because it holds in the
severed case too on this fixture.*

*Labels that were aspirational became true with no edit — "the pre-apply cloud
is the UNCORRECTED baseline", the doctor's premise, PR-5's before/after framing,
and PR-4's VERIFY-anchor-join reversal all now describe what the code does. The
one that had stated the old timing explicitly, this module's own flow diagram
(`CHECK → MEASURE → candidate → the pre-apply position group`), is corrected.
**And a real defect closed on the way:** before the move, auto-apply fired the
instant MEASURE was accepted while positions 3–10 were still being walked, so
the "pre-apply cloud" was being captured through a speaker that was already, or
about to be, corrected — the asymmetry PR-4's VERIFY-anchor-join reversal
explicitly refused to introduce, present all along on the other side. The move
removes it: the pre-apply cloud now closes BEFORE the apply starts, and VERIFY's
`on_apply` hold becomes a real wait rather than a formality. **That hold's
budget was checked, not assumed.** `REVIEW_HOLD_BUDGET_S` is 30 s and its own
comment sizes it on "the auto-apply TRANSACTION's own latency (a CamillaDSP
set-config + confirm round trip, typically well under a few seconds)" — which
is exactly the wait it now covers. Between the 2026-07-20 ruling and the cloud's
arrival the apply already sat one capture before VERIFY and that budget shipped;
what the cloud briefly did was insert nine captures of slack in front of it, so
the hold stopped mattering. The move restores the arrangement the budget was
derived for rather than newly stressing it.*

*PR-7 (2026-07-27): the before/after visualization + anomaly callouts land.
Rebased onto PR-6b/#1760 mid-implementation (it merged while this branch was
in progress) to consume the real `carve_outs_by_band` schema rather than a
reference-worktree approximation — verified byte-for-byte identical once
merged, zero rendering-code changes needed. Two deviations from the
section's literal wording, both raised as judgment calls in the
implementer's own report and independently confirmed by review rather than
silently taken.

**(a) `json_island` is not used.** The section's "decimated data ships in
the envelope payload via `json_island`" is self-contradictory on inspection:
`json_island` is the mechanism for embedding untrusted JSON inside a
server-rendered `<script>` element at PAGE LOAD (guarding the HTML-breakout
risk of an inline island); this page's architecture — established before
this PR, unchanged by it — ships cloud/curve/carve-out data through the
POLLED `GET /correction/crossover/envelope` JSON endpoint (the same pattern
the room page's own chart already uses for its curve data), which carries
no inline-embedding step for `json_island` to guard. A negative-guard test
pins this non-vacuously (asserting zero `type="application/json"`
occurrences across the page's module graph, not merely that the helper was
never called).

**(b) `analysis.before_after_delta`/`before_after_fill_segments` are not
reused.** The section's "reuse … where they fit" is conditional, and they
do not: that machinery grades ONE fixed-band EXTERNAL target curve (the
room correction's modal PEQ target) against measured/predicted, while the
flat spec has no external target at all — each phase's own `reference_db`
is self-referential (a power mean of its OWN curve), spans three
differently-toleranced bands, and the corridor it licenses is
`0 ± tolerance_db` in the deviation frame, not a value external to the
curve being graded. The chart draws the corridor directly from disclosed
`reference_db`/`tolerance_db` instead — confirmed by a full arithmetic
inventory to derive no new spec-facing number (every screen quantity is
either copied verbatim or the exact subtraction `evaluate_flat_spec`
itself already documents).

Also confirmed on review: the corridor's derivation-free arithmetic (full
inventory); VERIFY-only sourcing of the corridor/callouts/hatching (MEASURE
exists to be out of spec and never earns one); and keeping the shared
`h()`/`escapeHtml` helpers out of the two new ES modules (every string
lands via `textContent`, verified line-by-line as XSS-safe by construction
either way; avoiding `h()` kept `cloud.js`'s only import a same-directory
relative one, simplifying its Node harness). The provenance marker
(`_cloud_summary` stamps each closed group with its producing session id;
`_compact_cloud_status` compares it to the caller's current session and
renders a plain caption only for the genuinely-stale case) was the
review's own pick for the strongest part of the PR.

**Round 2 (2026-07-27): 2 blockers, 5 should-fixes, 4 nits, all fixed on the
same branch.** The reviewer hand-executed the chart's own domain arithmetic
against the real S0 main-leg cloud to find both blockers. **(B-1)** both
curves were plotted in absolute dB against ONE shared reference (VERIFY's);
linearization's cut-only invariant means VERIFY's reference is always at or
below MEASURE's, so the "Before" curve was displaced by a level change the
spec never grades, under a corridor labeled "Spec tolerance" that was not
testing what it claimed to. Fixed by plotting each curve relative to its
OWN `reference_db` (already on the wire for every phase — no new server
data), corridor at `0 ± tolerance_db`, the same window for both curves now
that each is normalized to its own reference. **(B-2)** the y-domain was
computed over the FULL curve (measured 0.71–23,953 Hz on the real S0
curve), collapsing the corridor to 6.9% of plot height; restricting to the
displayed 20–20,000 Hz range alone was still not enough — the worst
DISPLAYED point (19,969 Hz) sits inside `flat_spec.BEST_EFFORT_ABOVE_HZ`'s
own "best-effort, disclosed, never specced" region, a driver's natural
top-octave rolloff, not a defect. The domain is now bounded to the spec's
own GRADED frequency range (derived from `specBands`, e.g. 250–16,000 Hz —
never a hardcoded constant), measured at 20.96% corridor height on the same
corpus (up from 6.9%); the wider displayed range is still drawn at full
resolution and canvas-clipped where it exceeds that bound, so the ungraded
rolloff is shown, just does not set the scale.

Should-fixes: the chart feed's byte cost is now measured and stated —
41,161 bytes for both phases' curves at the persisted 512-point resolution
(82% of an otherwise-typical envelope poll, repeated every ~1.5 s) — and
two docstrings' "the doctor/`/state` never pays for this" claims corrected
(the KEY split from the compact block does not shrink that response, since
both keys ride the same returned dict; it only spares a `cloud`-only reader
from parsing curve-shaped data). A new, feed-specific 256-point
re-decimation ceiling (distinct from `crossover_v2_flow`'s own 512-point
persisted-artifact ceiling) measured 20,653 bytes on the same corpus, under
1 px/point on the chart's own ~640 px canvas. The legend now renders
progressively — a measure-only window (verify not yet closed) shows only
the "Before correction" swatch plus a plain, hardware-blind caption that
the after-correction curve is still coming, rather than three swatches for
series that are not on the canvas. The resize redraw is now debounced
150 ms, mirroring the room page's own `scheduleChartRedraw` exactly. Nits:
`_cloud_summary`'s new `session_id` stamp is guarded the same way its
sibling `pipeline` key already was (a conductor test double need not carry
every attribute a real one does); stale forward-looking comments referring
to carve-outs as landing "once PR-6b lands" were resolved by the same
rewrite that fixed B-1 (PR-6b having since merged); and test/comment prose
claiming on-device verification had already happened was corrected to
name it as owed to the HW product smoke (CI cannot see pixels).*

*Last verified: 2026-07-27*
