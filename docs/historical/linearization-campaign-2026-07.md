# Linearization campaign, 2026-07 — the archived decision record

> **Status: historical.** One record for the five plan files that described
> the 2026-07 linearization campaign: `flat-linearization-plan.md` (the
> measurement basis, spec and closed loop), `flat-linearization-productization-plan.md`
> (the PR ladder that built the instrument),
> `flat-linearization-flow-simplification-plan.md` (Express and the
> one-instruction screen grammar), `crossover-linearization-80-20-plan.md`
> (the R14–R20 revision) and `linearization-integrity-plan.md` (the fix ladder
> for the 10 dB-dark profile). All five are collapsed here.
>
> **What this file is for.** Production code cites these rulings — the spec
> table, the six fundamentals, the non-goals, the choreography defaults, the
> boost ruling, the cross-era disclosure — as the provenance of constants it
> already carries inline. Each cited ruling is preserved below under the name
> the citing comment uses. Read this for *why the number is that number*.
>
> **What it is not.** It is not current state and it is not a roadmap. Shipped
> operational truth is
> [`tuning-operator-runbook.md`](../tuning-operator-runbook.md); the layer
> architecture is
> [`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md);
> the program's planning authority is
> [`tuning-master-plan.md`](../tuning-master-plan.md). Where a ruling below has
> since been amended, the amendment is stated inline rather than the ruling
> being rewritten. Frozen: no further edits.

## Where each source's live authority went

| Source plan | What it owned | Who owns it now |
|---|---|---|
| `flat-linearization-plan.md` | measurement basis, the flat spec, the six fundamentals, the non-goals | `flat_spec.SPEC_BANDS` and `spatial_combine`/`interference_nulls`, each carrying its derivation inline; this record holds the provenance |
| `flat-linearization-productization-plan.md` | the PR-1…PR-8 build ladder and its declared choreography defaults | the constants it sized — `MAX_CAPTURE_PLAN_ATTEMPTS`, `DEFAULT_SESSIONS_MAX_BYTES`, the PR-4 band derivation — each stating its own arithmetic |
| `flat-linearization-flow-simplification-plan.md` | Express's claim boundary and the one-instruction screen grammar | `EXPRESS_CLOUD_VERIFY_POSITIONS` and the capture page; its §2.2/§2.6 contracts were already superseded by [`two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md) |
| `linearization-integrity-plan.md` | the PR-L1…PR-L5 fix ladder for the 10 dB-dark profile | all five items landed; `delta_probe`, `linearization_fit` and `crossover_envelope_v2` carry the live rules, and [ADR-0003](../adr/0003-prediction-gate-frame.md) carries the prediction-gate frame |

---

## The spec — what "flat" means here

The observable is the **spatially-averaged gated direct sound**: N gated
sweeps captured at mic positions spread over a small cloud around the
listening axis at ~1 m, each reflection-gated as today (~7 ms in the JTS3
room), combined as a **power average** (CTA-2034 Listening-Window-inspired;
honestly named a capture cloud, not certified LW angles). Pass/fail is
evaluated at 1/3-oct smoothing (1/6-oct retained for diagnostics),
**relative to the power mean over 250 Hz–8 kHz** computed at the same
smoothing over non-excluded bins — the reference deliberately comes from the
tight-tolerance bands so a top-octave deficit cannot re-center the target —
excluding interference-flagged bins from both the reference and the deviation
metric:

| Band | Tolerance |
|---|---|
| ~250 Hz – 2 kHz | ±1.5 dB |
| 2 – 8 kHz | ±2.0 dB |
| 8 – 16 kHz | ±2.5 dB |
| > 16 kHz | best-effort, disclosed, never specced |

The table is **S0-CONTINGENT**: revise only with S0/S3 data attached, per the
plan's own rule that *the spec serves the measurement, not the reverse*. It is
implemented as `flat_spec.SPEC_BANDS`, whose comment carries the same
constraint and the band-membership rule.

**The lower edges are nominal.** 250 Hz is a room-agnostic constant; a gated
capture is trustworthy only above `2.5/T` for its own reflection-free window
`T` (~357 Hz at the JTS3 room's 7 ms). `evaluate_flat_spec` raises every band's
lower edge — and the reference band's — to `max(f_lo, 2.5/T)` and publishes the
edge it graded from as each band's `graded_lo_hz` (#2551, #2599). The 250 Hz
edge is the seam with the room-correction layer and is owned by
`audio_measurement.room_boundary` so the spec's floor and the room ceiling's
clamp floor cannot drift apart (#1787).

**The 8–16 kHz tolerance is a unit, not just a pass mark.**
`interference_nulls.DEFAULT_MIN_NULL_DEPTH_DB` is set at that band's 2.5 dB
because excluding a band costs the correction real bandwidth permanently, so
the floor sits at the scale on which the spec judges a deviation to matter at
all.

## The six fundamentals

1. **Spatial multi-capture is THE measurement.** N≈8–12 gated sweeps at
   guided positions (≥10 cm spread for HF null decorrelation; ≥~30 cm spread
   to support the LF edge), per-capture quality gates (SNR, and the existing
   repeat/drift machinery within each position), combined by power average.
   Single-point measurement is demoted to a diagnostic. Discrete prompted
   positions first (lab UMIK-2 flow); Trueplay-style continuous moving capture
   is a later UX layer on the same combiner seam.
2. **Interference honesty screen.** Per-capture cepstral echo detection stamps
   τ/r diagnostics; across positions, bands where power-mean and median
   disagree by >2 dB are flagged interference-dominated. Flagged bins are
   excluded from correction **and** pass/fail, and reported. **Detection only —
   no echo removal in production.** (S0 corrected this: see § e.1 below. The
   screen is necessary but not sufficient, which is why a second, orthogonal
   instrument exists.)
3. **Minimum-phase, cut-biased correction only** (existing house rule:
   cut-domain + anchored give-back). The fit engine consumes the combined curve
   + exclusion mask; only features that survive spatial averaging get corrected.
4. **The spec above is the definition of done** for the speaker layer's "top of
   the table" contract in the layer doc.
5. **Closed loop at target SPL.** measure(cloud) → fit → apply →
   re-measure(cloud) → residual trim; converge at <~1 dB RMS 300 Hz–8 kHz
   (8–16 kHz reported against its own tolerance); any pass that increases
   residual error rolls back on the existing apply/undo rails.
6. **Role-count-blind.** Spec + loop operate on the summed system curve;
   per-driver machinery (linearization fit, alignment, protection) sits
   beneath, unchanged in ownership.

Fundamental 1 is the provenance of three live constants:
`DEFAULT_CLOUD_MEASURE_POSITIONS` (below), the guided-cloud placement policy id
`driver_reference_axis_v1` — deliberately its own id because the cloud asks the
household for the *opposite* whole-session promise from the stationary policy —
and the position-group choreography `capture_plan` builds.

## Non-goals and guardrails

- No cepstral or parametric echo *removal* in production (detection only).
- No max-hold estimator; no complex averaging of hand-moved captures.
- **No EQ of interference-flagged bins, ever; they are reported instead.**
- No absorber pads, tripods, or treatment steps in any user flow.
- No change to CamillaDSP safety ceilings (`devices.volume_limit` 0.0,
  positive-gain clamps) or driver-protection floors.
- Room correction's scope is untouched; no layer eats another's job.

The third bullet is what `linearization_envelope`'s null mask encodes, as the
only thing an envelope can say: zero permission. The fit corrects the response
*around* an identified null and never fills it.

## S0 executed — the 2026-07-25 session

The studio session that ran the instrument for the first time and produced the
campaign's central finding. Its full narrative — the attribution correction,
the two-mechanism verdict, the prediction scorecard, the honest top-octave
statement — was the plan's, and the parts production code cites are these.

### § e.1 — the screen is necessary but not sufficient

The power-vs-median screen flags bins where positions *disagree*, so it is
structurally blind to a null that every position sees. On S0 the screen
excluded **0 of 5462 bins** in 8–16 kHz — the power-vs-median gap there
measured **+1.27 dB** against its own >2 dB trigger — while a **source-fixed
comb sat inside that band cutting 5–7 dB nulls**. It correctly excluded the
position-dependent 1.8 kHz lobing dip; a position-*invariant* defect cannot
diverge across positions in the first place.

That is why `interference_nulls` exists as a second, orthogonal instrument:
it asks whether a dip is a *rung of a null ladder* attributable to a *measured
arrival*, and it is deliberately independent of the screen — consumers run
both, plus `geometry.locked`, and take the union. Position-invariance says
"this is real"; it does not say "this is correctable".

The corollary the same section corrected: "a defect every position sees carries
more weight" is false for a position-fixed defect, because the screen carries
no signal about it either way.

### § e.2 — the 8–16 kHz tolerance is not achievable by EQ

The ±2.5 dB tolerance cannot be met against a **5.4–7.0 dB source-fixed comb**
sitting inside that band, so the spec needs a documented carve-out for
identified interference nulls, pending the owner's decision on a source fix
(horn redesign). Per standard diffraction reasoning, more lip roll-back should
reduce the echo's amplitude r **without** materially shifting τ, because delay
tracks mouth radius while amplitude tracks how abruptly the rim's curvature
changes — design guidance only, never confirmed acoustically.

### Why the cloud carries 9 positions, not 8

Adjudication 3a, 2026-07-26. Fundamental 1's floor is **N≈8–12 gated sweeps**,
and a cloud of N positions yields `N − 1` summed CURVES, not N — the design-axis
anchor is a per-driver MEASURE capture and contributes only a modelled
`predicted_sum`. The first draft shipped 8 positions ⇒ 7 curves, meeting the
floor in positions but not in the thing the floor is about. 9 positions ⇒ 8
curves. S0's own stability work is why the choice stopped at 9 rather than
climbing toward 12: more positions is better, and the binding ceiling is
wall-clock, not statistics.

## The productization work order — the rulings that sized constants

### PR-1 — `identify_interference_nulls`, the orthogonal null-ID gate

The second instrument § e.1 above calls for, built as a pure module (numpy
only, no I/O, no policy). Its method promotes the S0 forensics: a candidate τ
set from the cloud's per-position `EchoDiagnostic`s (**a candidate needs ≥2
corroborating positions; single-estimate candidates are reported
`insufficient_evidence`, never identified**); a predicted ladder
`f_n = (n + ½)/τ`; and null matching designed around a *known measured
discrepancy* — on the S0 corpus the ladder τ implied by the measured null
frequencies (293–308 µs across groups) sits **4–9 % below** the directly
measured arrival τ (median 321.5 µs), because a real rim wave is not an ideal
single-delay reflector, so the matcher fits the best single-τ ladder to the
minima with τ as a free parameter rather than assuming the arrival's.

**Acceptance**, pre-registered before the module was written and still what its
tests assert: on the S0 main-leg cloud it identifies the 8–16 kHz nulls
(8.4/11.5/15 kHz family) as `position_invariant` with a single fitted
`τ_ladder` whose arrival corroboration passes the calibrated band (the 4–9 %
gap admitted, calibration committed with its regime stated) and r agreement
≤ 0.05; it **refuses** attribution of the 1.8 kHz dip by depth ceiling; and on
the ground-plane set it does not fabricate an identification from the
125–146 µs proud-capsule arrival without its own ladder support. Synthetically:
a constructed two-path IR gives exact identification, a no-echo synthetic gives
an empty registry, and ladder-without-arrival or arrival-without-ladder gives
`insufficient_evidence`.

### PR-2 item 4 — disclosure over restriction

The echo detector's reportable-τ resolution floor is surfaced rather than
designed around. For the shipped defaults (120 µs lower edge, 5–19 kHz) that
floor is **~191.4 µs** — the number that makes S0's ground-plane 125–146 µs
proud-capsule arrivals *structurally* unreportable rather than merely
unreported. The plan's ruling: publish the floor on every record instead of
raising the default lower edge, because a consumer needs "what was invisible to
this window" most when the window found nothing.

### PR-3b — the position-group choreography, and its declared maxima

The choreography's **declared** defaults — design inputs, not measurements —
are what size the relay's attempt cap. Worst-case ENTRY count at the section's
documented maxima: CHECK 1 + MEASURE 1 + `(N−1)` cloud-measure at max N=12 ⇒ 11,
plus M=6 cloud-verify and 2 geometry-retry positions = **21 entries**. Since
`max_attempts` doubles as the retake budget, the cap covers entries plus
retakes: 21 + 11 = **32**, ~52 % retake headroom.

**The named corner-cut that sizes retention:** full per-position WAVs are kept
rather than derived summaries, *because the S0 forensics that produced this
program's central finding were only possible from raw WAVs*. Disk is cheap; the
honesty is not. That is why `bundles.DEFAULT_SESSIONS_MAX_BYTES` went 256 MiB →
1 GiB.

### PR-4 — contract-derived analysis bands

> "The echo/detector band and PR-2's `signal_band_hz` derive from the declared
> contract: the summed system's swept band (`RoleBand.band` as composed) for the
> passband; the tweeter's `usable_frequency_range_hz` / `measurement_band_hz`
> for the upper echo band — replacing `DEFAULT_ECHO_BAND_HZ`'s flat constant at
> the call site."

Implemented in `crossover_v2/spatial.py`'s PR-4 section, together with the
single result-assembly function #1742 item 4 asks for.

### PR-7 — visualization on jts.local

The before/after overlay: the combined cloud spec curve pre-apply
(CLOUD-MEASURE) against post-apply (CLOUD-VERIFY), 1/3-oct, with excluded
intervals visually distinguished and the spec-band tolerance corridor drawn,
plus plain-language anomaly callouts. Decimated data ships in the envelope
payload. This is the section `deploy/assets/correction/js/crossover/cloud.js`
cites for what the hardware product smoke should eyeball — chiefly that the
chart self-heals on the first-unhide path.

## Express, and the one-instruction screen grammar

Express's shape is **N=5, M=1** against Full's N=9, M=6. Its premise is a
comb-free source; §3's first-run Full recommendation exists for the case where
that premise does not hold.

### §1.3 — degraded-claims table (what Express claims, what it stops claiming)

| Surface | Full (N=9, M=6) | Express (N=5, M=1) |
|---|---|---|
| Correction fit evidence | 8-position power-mean cloud | 4-position power-mean cloud; the envelope's σ/√N position-stability term computes from Express's own spread (§1.1 — **its limit is unmeasured until the JTS3 smoke**) — **automatic** |
| HF-null decorrelation | 8 dispersed positions | 4 positions decorrelate HF nulls less well on a speaker that still combs; the registry/carve-out machinery still refuses honestly, with less evidence |
| Outlier exclusion screen | power-vs-median over 8 | over 4 (weaker; a single bad take is harder to identify) — **automatic**, disclosed |
| Echo/geometry adjudication | n = 8, `thin_evidence` cliff at 2-of-≥4 | n = 4, same cliff semantics — **automatic** |
| Null registry / carve-outs | full corroboration budget | fewer corroborations → more `insufficient_evidence` refusals — **automatic** (the detector refuses rather than guesses) |
| Pre-apply spec verdict | cloud spec gauges on the measure cloud | same gauges on the 4-position cloud, tier-qualified |
| **Post-apply spec verdict** | cloud-verify group (5 positions) re-measures the applied result across the cloud | **ABSENT.** Express verifies tracking at the mark only (±1.5 dB, `VERIFY_TOLERANCE_DB` unchanged). No cross-position post-apply claim — disclosed on the done screen, in the wizard, and in `/state` |
| Before/after chart | both phases | "before" cloud + VERIFY tracking; the "after" cloud panel is absent and the callouts say why |
| Wall clock | ~10–15 min (page displays 11) | expected ~3–5 min real; page displays 5 |

The disclosure rule: **never claim wider than measured.** Express copy says what
it verified ("tuned and confirmed at the mark") and names the upgrade path
("run a Full measurement for the result checked at several spots around the
mark"). "The verified-everywhere result" was rejected as overclaiming past what
a Full measurement actually re-checks — a handful of prompted spots around the
mark, never every point in the room. Nothing is silently weakened: every row is
either the existing machinery scaling itself down, or an absence that is stated.

That table is why `EXPRESS_CLOUD_VERIFY_POSITIONS` is 1.

### §§2.1–2.6 — the screen grammar

One instruction per step; confirm-then-tone wherever a move happened; a plan
announcement before any sweep; retry and recovery screens using the same
grammar; courtesy-tone pacing; and per-measurement Retake / Next / Stop
control. §§2.2 and 2.6 were **superseded** by
[`two-stage-commission-flow-plan.md`](two-stage-commission-flow-plan.md) —
the VERIFY begin-first/confirm-after-apply contract and the group-close
"one extra tap" contract are that document's. The rest is what
`tests/js/capture_plan_loop_test.mjs` pins.

## The 80/20 revision — the R14–R20 slice

The revision that preserved the gated/spatial honesty research but gave the
anchor, lateral samples, candidate solver and later Room cloud separate jobs.
The campaign's own dated narrative is
[`crossover-measurement-v2-campaign-record.md`](crossover-measurement-v2-campaign-record.md)'s.
Three things are **only** here.

### One owner per fact

The table this plan opened with, because a campaign that spans five documents
needs one. It is the ancestor of the owner table at the top of this file.

| Fact | Owner |
|---|---|
| Shipped phase/state behavior and operator recovery | [`tuning-operator-runbook.md`](../tuning-operator-runbook.md) |
| Speaker/room/bass/preference layer boundaries | [`active-speaker-tuning-layers-design.md`](../active-speaker-tuning-layers-design.md) |
| Why spatially gated measurements, exclusions, and power averages exist | this record |
| This revision's measurement contracts, rounds, and issue disposition | this record |
| Individual defect, evidence, and acceptance tests | the owning GitHub issue |

### R16's unrecorded ratification — the finding, kept because it is a finding

R16 shipped at **649 gross production lines across 4 files** against a ceiling
of 350. **The overage's ratification is not in the record.** R20 checked:
#2157's body cites `#1894#issuecomment-5203437285`, which **404s and is absent
from #1894's comments**, and its prose (≤600 / ≤640) contradicts its own table
(≤650 / ≤790) — under the prose pair the round busts both. Per the campaign's
own §9 item 10, *an unrecorded ratification did not happen*; recording or
re-ratifying one is a conductor/owner act R20 did not take. It remains untaken.

### R17's three structural discoveries

Each was verified in code before it was acted on, and each reshaped the round's
ceiling (400 → 650 → 800 → 1000; it landed at 1,365 cumulative production).

1. **Evidence lifetime.** The raw capture is alive only inside
   `consume_capture`; what survives past it are derived `DriverResponse`s that
   the conditioning policy refuses to un-compose. So every candidate is
   evaluated at MEASURE-consume, **evaluate-and-release** — each candidate's
   analysis and fit are freed before the next starts — and only the small
   per-candidate records cross the walk to be adjudicated at its close.
2. **The preset-mismatch gate.** A selected Fc cannot reach applied DSP:
   `baseline_profile` refuses any candidate whose preset differs from the saved
   crossover, a deliberate one-writer-per-fact defence. Making an alternative
   applicable is a nine-site change across six modules — its own round. So the
   selector produced a **RECOMMENDATION**: the household is told the measured
   number, declares it in `/sound`, and the next session's configured Fc *is*
   that number, applied through the untouched golden path. Byte-equivalence
   becomes structural rather than asserted, and keep-configured is a
   first-class honest verdict rather than silence. **This is the shape the
   selector's deletion did not change** — a corner is declared and executed.
3. **The phone's deadline** (superseded 2026-08-22). `waitForCaptureResult`
   allowed `max(30 000, spec.duration_ms)` — **41,885 ms** on the live stage-1
   spec — and its expiry was a TERMINAL `sweepFailed`, not a retry. The
   evaluation budget was derived from that deadline and spent a conservative
   fraction of it, because the anchor's own ~7 s analysis had already come out
   of the window before the sweep started. A loaded Pi scored fewer candidates
   than it proposed; that was disclosed as k-of-N and was never a session
   failure. Ticket 2.3 deleted the Pi-minted per-capture `result_wait_s` this
   budget derived from along with the rest of the sweep; the capture page's own
   90 s `CAPTURE_RESULT_WAIT_BUDGET_MS` floor governs every round instead.

One ruling from the revision is still live.

### §4.2 — the "Boost ruling" (owner, 2026-08-05)

> The R15 driver-only path permits boost when post-apply `VERIFY` runs;
> `allow_boost` must no longer additionally require a pre-apply cloud. What that
> cloud supplied is absent until a later gated round: the cloud-derived
> boost-excluded bands, and the envelope's spatial-exclusion and
> position-stability terms. The accepted risk: a boost can land on a
> position-specific artifact that an at-mark verification cannot detect. The
> standing rails all remain — envelope depth limits, the realized-cascade
> stopband-gain guard, headroom accounting, post-apply `VERIFY`, and retained
> Undo. Prerequisite: the composition must be splice-free (in-band evidence
> unpolluted by the out-of-band seam) before this path is trusted on hardware.

**Implemented scope.** `allow_boost` is `post_apply_verifies AND (a cloud
verdict reached the envelope OR the session's capture plan contains no
pre-apply cloud phase)`. The second disjunct is the R15 driver-only path, where
the cloud is absent BY DESIGN. The first keeps the clause the retired condition
existed for: a session that PLANNED a cloud and LOST it stays cut-only, because
there the missing exclusions are a failure rather than a plan.
`boost_excluded_bands_hz` is `()` on the driver-only path — there is no spatial
evidence to compose, which is the accepted risk named above.

Second, intended consequence: boost enlarges the achievable set, so a candidate
the improvement gate previously refused as `correction_not_an_improvement` can
clear it on the same evidence. (That gate has since stopped refusing altogether
— it banks `LEDGER_NOT_AN_IMPROVEMENT` and the round proceeds; see
[`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md), deviation (c).)

## The integrity ladder — the fix for the 10 dB-dark profile

Five items, all landed: PR-L1 input integrity (the mic-calibration sign
convention), PR-L2 model integrity (shelf realization — `slope: 6` is not
CamillaDSP's Butterworth), PR-L3 the trim-frame defect, PR-L4 the missing
accountability assertions, PR-L5 the doctrine amendment and the delta probe.

### PR-L3 — the trim-frame defect, and the invariance that survived it

The defect was located and two rival candidates were **refuted**:
`_aligned_branch_tf` and `_driver_response` return byte-identical transfer
functions, and **the CHECK gain-plan skew is fully divided out by the
deconvolution — which also makes the trim-vs-fit frame comparison
invariant to the drive gain.** The real mechanism: `overlap_band_hz` clamps the shared band's
lower edge UP to the tweeter's sweep floor — Fc on this speaker — leaving
`[Fc, 2Fc]`, entirely inside the woofer's crossover skirt: **+10.59 dB of
closed-form bias on an ideal LR4 pair with two equal-sensitivity drivers**,
10.9 dB (07-27) / 13.1 dB measured.

The invariance sentence is the one
`tests/test_audio_measurement_program_analysis.py` cites: every MEASURE output
is a per-unit-drive transfer function, which is what lets CHECK's gain plan
drive the woofer and tweeter at different digital levels without biasing the
measurement.

### PR-L4 item 2 — spec-grade the prediction

Item 2's gate grades the RAW pre-fit and the LINEARIZED predicted sums through
**one evaluator — a same-instrument before/after**. An earlier revision compared
the model against the measured in-room cloud; adversarial review showed that
made the verdict a function of the ROOM (holding the correction constant, better
rooms refused harder), **and the threshold fell from 1.5 dB to 0.5 dB with the
frame change**, because the comparison no longer has to absorb a cross-frame
gap. That frame is the subject of [ADR-0003](../adr/0003-prediction-gate-frame.md).

Item 4 implements **surface, not restore**: a missing post-apply grade says
nothing about the correction — the commonest way to reach it is a household
closing the phone after the apply, and Express omits the post-apply position
group by design — so an auto-restore keyed on it would revert every Express
session ever run.

Named tolerances, derivations pinned by tests:
`REALIZED_LEVEL_MATCH_TOLERANCE_DB = 3.0`,
`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5`,
`MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB = 6.0`.

The sanction this item created is what `crossover_v2/accountability.py`'s item-2
assertion implements: on 2026-07-27 the flatness instrument failed all three
bands and auto-apply fired unconditionally two seconds later.

### PR-L5 — the delta probe

Every applied correction change is verified as a **realized-vs-commanded
per-frequency map** (one sweep before/after) and classified into one of four
verdicts — **matched** (keep), **model-error** (rollback + flag; permanently
catches the PR-L2 shelf-Q class), **level-dependent shortfall** (a driver
compression diagnostic), and **spatially costly** (cross-position spread
widened — interference; the service verdict routes placement-vs-speaker via the
tau ladder). Rollback is automatic on the three non-matched classes. It exists
because on 2026-07-27 a linearization shipped whose emitted shelves were
realized at Q 0.476 while every gate in the fit engine evaluated them at
Q 0.707.

Named tolerances: `DELTA_PROBE_TOLERANCE_LOW_DB = 1.5` (below the 1.70 dB the
shelf-Q defect peaked at, and the same bar `VERIFY_TOLERANCE_DB` already sets)
and `DELTA_PROBE_TOLERANCE_HIGH_DB = 2.5` (above the 2.0 dB repeat spread the
fit's own agreement gate accepts at those frequencies, so HF noise cannot
fabricate a verdict).

### Cross-era disclosure

> A candidate persisted BEFORE this amendment carries a `headroom_cost_db`
> stamped under the sum-of-positives rule, so it discloses (on the 2026-07-28
> JTS3 profile) ~22.5 dB where re-emitting the same candidate now charges ~5.
> The stamp is not re-derived on load, deliberately: it is a record of what
> that graph was emitted with, and a recommission replaces it. The realistic
> population is one profile.

That paragraph names the #1808 boundary only. A THIRD era opened on 2026-08-22
when #2758 widened the evaluation grid, and it can be wrong in the opposite
direction — an older stamp reading SMALLER than today's charge. The full era
table and the migration behaviour are
[`crossover-measurement-v2-campaign-record.md`](crossover-measurement-v2-campaign-record.md)'s,
"Reading one from before the 2026-08-22 grid widening".

This is why `crossover_envelope_v2` carries the charge as a compound rather than
a bare float: a renderer handed a lone `headroom_cost_db` cannot know which era
it is holding.

### The #2523 amendment — cuts are bounded too

> This item used to continue "Cuts are unbounded: out-of-band leakage still
> reaches the summed response and removing it spends no headroom." The first
> clause is retired; the justification is not.

Cuts are bounded at a LOOSER band — the radiating band widened by
`branch_target.STOPBAND_GAIN_MARGIN_OCTAVES` (half an octave), which is what the
whole solve now runs over. A shoulder cut is still kept. What changed is the far
end: R10a (#1817) gave the fit an IDEAL crossover as its target, and a real
branch does not follow one into its own deep stopband — breakup, cabinet leakage
and the capture's noise floor sit tens of dB above it — so the objective was
being fed demand no cut-only cascade can realize, and the greedy search spent
filter slots on it. Measured on the reconstructed fixture: all eight slots
between 9.7 and 11.8 kHz on a branch declared to radiate to 1282.3 Hz. The
give-back the unbounded solve was buying with out-of-band content alone —
0.0136 dB against 2.1668 dB on the same fixture — and the 2026-08-19 band fix
that renamed the quantity `core_band_giveback_db` are owned by
`linearization_fit.py`, which states both inline.

---

Archived 2026-08-26 (wave 7e). Superseded plans are not kept in sync with the
code; read the symbol a claim names before relying on it.
