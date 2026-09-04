# Room-correction regime — work order (issue #1791)

> **Status: adopted work order (2026-07-27).** Synthesized from the
> owner-run competitive deep-research result (verbatim:
> [`docs/research/2026-07-27-acoustics-round-2/02-room-correction-competitive.md`](research/2026-07-27-acoustics-round-2/02-room-correction-competitive.md);
> prompt and laptop-side evidence in
> `captures/room-correction-research-20260727/`) against the code as
> verified on 2026-07-27. Anchors
> [issue #1791](https://github.com/jaspercurry/JTS/issues/1791) and
> lands [issue #1787](https://github.com/jaspercurry/JTS/issues/1787)'s
> seam SSOT. Composes with the gating v2 work order
> ([`docs/gating-v2-plan.md`](gating-v2-plan.md)): the group validity
> floor that plan redefines is the layer boundary this plan consumes.
> This plan owns the **acoustic regime** (bandwidth, vocabulary,
> attribution); the room-correction modernization plan's UX/structure
> phases (R1–R5) are a different axis and are untouched — this ladder
> is numbered **RC1–RC5** to avoid collision. The
> [`room-correction-information-design.md`](room-correction-information-design.md)
> spec remains design-of-record; RC-PRs that change its promises amend
> it in the same PR.

## The verdict in one paragraph

The research survey (Dirac/Audyssey/Trinnov/Lyngdorf/Anthem/Genelec/
Neumann/Trueplay/JBL/REW-practice) splits into a modal-only school and
a full-range school that hedges with broad low-Q terms and phase tools.
The current 350 Hz ceiling is right in spirit, wrong as a hard wall:
the defensible regime is a **per-room transition** for full-resolution
modal correction, a strictly-bounded **residual trend tier** above it
that corrects only what the room added on top of the speaker's known
gated response — and **nothing above 1 kHz**. The attribution
advantage, stated precisely (owner pushback 2026-07-28 corrected an
overclaim here): hardware owners (Sonos, Neumann, Genelec, HomePod)
do subtract their speaker's anechoic behavior, but from a
**factory, model-level golden-unit reference** baked in at design
time; hardware-agnostic correctors (Dirac, Audyssey) never measured
the speaker at all and must *estimate* its direct sound from the
same in-room capture (Dirac's frequency-dependent windowing is
exactly that estimate). JTS is the only one of the three shapes that
corrects against a **live, per-unit, gated measurement of this
specific speaker as currently commissioned** — necessarily so, since
JTS hardware is open/DIY (nobody has a factory curve for a cdhorn
build, a swapped horn, or aged drivers) — fingerprint-matched to the
applied DSP. FIR/phase is
rejected on quantified grounds. Evidence-gated LF boosts are unlocked,
but on room-instrument evidence, not the HF null registry (see D5's
delta).

## Current state (verified against code 2026-07-27 — the review bar)

- The "350 Hz cap" is per-strategy, not global:
  `CORRECTION_STRATEGIES` in
  [`jasper/correction/strategy.py`](../jasper/correction/strategy.py)
  ships `safe` 25–250 / `balanced` 20–350 (default) / `assertive`
  20–500 (`cuts_only=False`, `max_total_boost_db=3.0`, not offered on
  the household surface per `HOUSEHOLD_CORRECTION_STRATEGY_IDS`).
- **Ten** 350-literal sites, not #1787's seven — *and RC1's
  implementation found an eleventh: `correction/envelope.py`'s
  `_band_word`, which owns 250/500 thresholds on the household-facing
  headline sentence ("in the bass and lower mids"). It is routed too;
  the count below is the pre-implementation survey.* The issue's seven
  plus
  `acoustic_quality.SNR_BANDS_HZ`, `acoustic_quality`'s
  `min(350.0, peq_f_high)` re-clamp (would silently cap any raised
  ceiling), and the shared metric
  `audio_measurement/analysis.deviation_metrics(f_high=350.0)` that
  acceptance/verify/envelope all flow through. An eleventh shared-edge
  site exists and is deliberately **excluded from the routing**:
  `audio_measurement/snr_policy.CROSSOVER_SNR_BANDS_HZ` shares its
  first four rows with `acoustic_quality.SNR_BANDS_HZ` — pinned by
  `tests/test_audio_measurement_snr_policy.py`, which asserts prefix
  value-equality between those two literals and, separately, object
  identity between `correction_session.SNR_BANDS_HZ` and
  `acoustic_quality.SNR_BANDS_HZ` — and is consumed by the *gated*
  instrument. SNR band tables are capture-quality vocabulary, not
  correction bands, and must stay static so banded SNR remains
  comparable across sessions and instruments (see traps).
- **`SessionConfig.peq_f_high` is a shipped defect, not a latent one.**
  `SessionConfig` carries a frozen shadow copy of `balanced`'s
  parameters that is never overridden (`SessionConfig()` is the only
  constructor call); design correctly reads the selected strategy, but
  acceptance, the verify before/after delta, repeatability, confidence
  banding, and the *disclosed* `correction_band_hz` all read
  `cfg.peq_f_high` = 350. A household-selectable `safe` session
  therefore corrects 25–250 Hz while grading ACCEPT/REVERT over
  50–350, scoring confidence over 20–350, and disclosing
  `[20, 350]` — corrected-narrow-stated-wide, live on the household
  surface today. RC1 fixes this (the one named exception to its
  behavior-identical promise).
- Vocabulary: peaking bells only (`audio_measurement/peq.py`, greedy
  peak-fit); shelves exist only in the taste layer. The round-trip
  reader `extract_room_peqs_from_config_text` regex-matches peaking
  blocks only — a new room-layer filter type is **silently dropped**
  on taste-EQ recompose and the multi-room leader bake (both re-extract
  with no guard), and makes the bass-extension recompose **refuse
  loudly** (`CarrierCannotHostEq` on the overlay-projection mismatch —
  bass extension becomes unavailable). Supporting shelves is a data
  model + emitter + reader + projection change (`PeqFilter` has no
  `type` field), not a regex tweak.
- Boost machinery half-exists: `_enforce_total_boost_cap`,
  `_exclude_boosts_near_crossover` (±1/3 oct around the bass corner),
  emit-time headroom trims on both graph paths
  (`room_headroom` / `active_baseline_headroom`, canonical
  `total_positive_boost_db` in `camilla_config_contract.py`), and the
  design-of-record's +3.0 dB total-positive-boost hard ceiling.
  `design_peq`'s `max_cut_db` accepts a per-bin array; `max_boost_db`
  is scalar-only — the mirror change is the insertion point for
  evidence-gated boosts.
- Measurement input: 6-position cloud default (choices 1/3/6, owner
  ruling), power-mean average (`spatial_average_db`), 1/48-oct design
  smoothing, position-1 matched basis + repeat capture for
  acceptance/repeatability. Acceptance loop
  (`acceptance.evaluate_acceptance`, ACCEPT/SURFACE/REVERT ladder,
  auto-revert wired) is shipped and stays authoritative.
- Targets: `correction/target.py` (flat default; opt-in Harman-warmth
  profiles) and `sound/profile.py` `CURVE_PRESETS` are two independent
  tilt vocabularies with no shared contract.
- Cross-layer reach: `jasper/correction/` reads **none** of
  `validity_floor_hz`, the gated spec curve, or the null registry
  (zero imports of any of the three). The null registry itself is
  already candidate-borne and staleness-gated: it rides
  `MeasuredCrossoverCandidate.exclusion_evidence`, participates in the
  candidate fingerprint, and is re-read at every apply behind
  `baseline_profile`'s `expected_candidate_fingerprint` gate — but the
  **validity floor and gated spec curve are not**: they live only in
  the retention-prunable session bundle. The registry's derivation
  band is the gated instrument's HF echo band (≥4 kHz; module default
  5–19 kHz, JTS3 clamped 4–18 kHz) — it classifies speaker-fixed
  interference, **not LF room nulls**.
- Schroeder: `calibration_agent/tools.compute_schroeder` implements
  the real formula `f_s = 2000·√(T60/V)` but is dead in practice —
  its only caller passes no arguments, so it always returns
  `available: False`. No RT60 estimator exists, **and nothing
  measures or asks for room volume V**, which the formula requires.
  IRs are recomputed from persisted raw captures
  (`interop.impulse_response_from_capture`); raw WAVs are prunable
  private data, so an archived bundle may no longer support the
  computation.
- Layer stacking: room PEQs ride the stereo program bus pre-split on
  both graph paths, upstream of the taste layer, crossover, driver
  linearization, bass extension, limiters; `volume_limit ≤ 0` is
  load-enforced.

## Adopted decisions

**D1 — Bandwidth: two-tier ceiling replacing the hard wall. ADOPT
(research DECISION 1).** Tier A (full-resolution modal correction,
existing vocabulary and strategy character) runs 20 Hz → **f_t**, a
per-room transition clamped to **[250, 500] Hz**, falling back to
**350** whenever estimation is uncertain (and always until RC2 ships
the estimator). f_t derivation needs **two inputs**: T60 from a
Schroeder-*integral* (backward decay integration) over the persisted
IRs, and room volume V for the Schroeder-*frequency* formula
`f_s = 2000·√(T60/V)` — two different Schroeder things, named apart
deliberately. Nothing measures V and the plan does not pretend
otherwise: RC2 adds a **coarse optional room-size input** (three
buckets with stated V values, disclosed on the session surface; the
[250, 500] clamp bounds the damage of coarse V since f_s ∝ 1/√V).
Missing either input → f_t = 350, disclosed as the fallback. Tier B
(residual trend only, D2) runs f_t → **1,000 Hz hard stop**. Above
1 kHz the room layer does nothing — recorded as doctrine.
Implementation constraint: **one boundary SSOT** consumed by every one
of the ten literal sites (strategies compose their character with the
room-derived boundary; `safe` keeps its tighter `min(250, f_t)` cap);
`SessionConfig.peq_f_high` and the `acoustic_quality` re-clamp are
collapsed into the SSOT, not left as copies.

**D2 — Attribution: Tier B corrects only what the room added. ADOPT
(research DECISION 2), via a new applied-speaker-evidence seam.** In
Tier B the correction target is the residual: spatially-averaged
in-room curve minus the applied profile's **gated** curve, level-
normalized over a shared trusted band (pinned in the RC4 design).
Double-correction becomes impossible by construction — the room layer
never re-flattens voicing the gated layer set. Code reality: all three
evidence members are unreachable from `correction/` today, but they
have **two different homes**: the null registry is already
candidate-borne and fingerprint-gated (rides
`MeasuredCrossoverCandidate.exclusion_evidence`, "travels with the
correction it justifies", staleness already solved by
`expected_candidate_fingerprint`), while the validity floor and gated
spec curve live only in the retention-prunable session bundle. The
seam therefore has two parts, neither inventing a staleness rule:
(a) RC1 extends the candidate evidence payload so the validity floor
and gated spec curve become candidate-borne beside the registry
(era-tolerant: candidates predating the extension simply lack the
fields), and (b) RC1 adds the read-only accessor in `correction/`
that resolves the **applied** candidate through the existing
fingerprint gate. Missing, stale, or era-absent evidence (passive
speakers, pre-commissioning rooms, pre-extension candidates,
recommission in progress) **disables Tier B with disclosure** —
Tier A continues unchanged; Tier B never falls back to the raw
in-room curve. Tier B vocabulary: **broad peaking bells only, Q ∈ [0.4, 1.0],
±3 dB per term, ±4 dB net across the band** (architect delta from the
research's shelf/tilt vocabulary: the recompose round-trip reader is
peaking-only, and bells within these caps express the same broad
trends — shelf/tilt terms are deferred behind a reader+round-trip-test
extension, recorded as a follow-up, not silently added). Note the Q
floor: the household strategies clamp Q to 1.0–8.0 and the
design-of-record says so — Tier B terms need Q *below* that floor to
be broad, so the Tier B Q range is one of the named design-of-record
amendments (D5's enumeration). Phone-mic tier: Tier B requires a
calibrated mic in v1 (phone mics are least reliable exactly in
500 Hz–1 kHz); phone sessions get Tier A only, with the limitation
disclosed. **Tier B ships opt-in/experimental** — surfaced only on
the calibrated-mic path and promoted to a default only after a
hardware session grades prediction 1; the machinery-to-evidence ratio
does not yet justify default-on. Revisit when phone-mic calibration
evidence exists.

**D3 — The shared band: gated-layer primacy with a derived handoff.
ADOPT (research DECISION 3); lands #1787.** Below the applied
profile's validity floor (read from the evidence seam; the
support-derived floor once gating v2 PR-G1 lands — same payload key),
the room layer + bass extension own everything. From the floor to f_t,
the gated layer owns absolute tonal balance; the room layer cuts modal
peaks and corrects broad trend against the residual basis where
available. The seam relation is pinned concretely: the boundary SSOT
module co-owns the room ceiling derivation (its [250, 500] clamp
bounds) and the gated spec's lower edge side by side with the relation
documented in one place, and the contract test asserts (1) every
listed site resolves through the SSOT (a no-re-declared-literal drift
guard over the routed files) and (2) the clamp bounds and spec edge
are the SSOT's values — so moving either is a one-file, test-visible
change, never a scattered literal edit. (The spec edge itself stays
revisable — `flat_spec` marks 250-vs-300 as S0-contingent — but it
can only move *through* the SSOT.) When the evidence seam is absent,
the handoff falls back to the current constants — with the fallback
disclosed, never silent.

**D4 — Target ownership: room removes deviation; tilt is taste. ADOPT
(research DECISION 4 — confirmatory).** The design-of-record already
says it ("Room removes repeatable room deviation and Sound owns
subjective preference"); the room default target stays flat. Recorded
delta: the opt-in Harman-warmth room targets stay for continuity, but
the two independent tilt vocabularies (`correction/target.py` vs
`sound/profile.py` presets) are flagged for unification into one
shared vocabulary — a follow-up SSOT item, not a rip-out in this
ladder.

**D5 — LF boost policy: evidence-gated boosts on room-instrument
evidence. ADOPT (research DECISION 5), with a corrected evidence base
(architect delta).** The research assumed "the null registry" gates
LF boosts; the registry's provenance is the gated instrument's HF
echo band and it cannot classify LF room nulls. Boost admission in
Tier A is instead gated on evidence the room instrument itself
produces: a dip is boostable only when (a) **spatially persistent** —
present with agreeing depth in at least
`LF_BOOST_PRESENCE_FRACTION` of cloud positions, a **new
LF-calibrated constant owned by this policy** (not a borrowed
reference to `interference_nulls.POSITION_PRESENCE_FRACTION`, whose
0.70 was calibrated on 8–12-position HF echo rungs). On the room
cloud the fraction is coarsely quantised and the plan says what it
means: at N=6 the bar is ≥5 of 6; at N=3 it is 3 of 3; **below 3
positions boost admission is refused outright** — a 1-position
session must never spend maximum level boosting into what could be an
SBIR null. Position-dependent interference dips average out and are
refused; (b) **shape-plausible as modal** — bounded Q and depth (deep
narrow nulls refused with the existing `nulls_not_boosted` grammar);
and (c) within headroom. Cap **+6 dB per admitted dip**; total
positive boost cap rises from +3.0 to **+6.0 dB** with the emit-time
headroom trim carrying the cost and the cost disclosed ("this
correction costs N dB of maximum level" — mirroring the L5 doctrine
on the speaker layer). `design_peq` gains the per-bin `max_boost_db`
array mirroring `max_cut_db`. Closed-loop acceptance + auto-revert
applies unchanged. **Design-of-record amendments — enumerated, all
owner-visible, all carried in RC5's spec amendment:** (i) the
"cuts-only by default, consuming 0.0 dB of positive-boost budget"
household contract sentence — boosts arrive as a new evidence-gated
capability *on the household strategies*, orthogonal to strategy
selection (cuts-only remains the fit's default behavior; admitted
dips are the only exception); the `assertive` strategy and its R5
household-eligibility ruling are **untouched** — RC5 does not ship
assertive; (ii) the +3.0 dB total-positive-boost ceiling → +6.0 dB
with disclosed level cost — the design-of-record states +3.0 in
**two** places (the household-contract bullet and the assertive/R5
paragraph's "the +3.0 dB total ceiling still applies"), and the
amendment updates the *numeric ceiling in both* while leaving the R5
*ruling* itself (assertive stays excluded until R5 brings it inside
household bounds) intact — otherwise the spec would contradict
itself; (iii) the Q clamp — Tier B terms need Q ∈ [0.4, 1.0], below
the household 1.0–8.0 floor (D2). Where the HF
null registry IS authoritative (the gated instrument's band), it
continues to bound the *speaker* layer's fit; nothing here touches
that.

**D6 — Phase/FIR room correction: REJECT (research DECISION 6 —
confirmatory).** Already the design-of-record position; now recorded
with the quantified rationale (group-delay audibility ~1 ms
300 Hz–1 kHz; linear-phase FIR at LF-useful resolution ≈ 100 ms
latency vs smart-speaker duties; the room's non-min-phase errors are
single-point-correctable only; Anthem and Sonos independently
IIR/magnitude-only). The existing `correction_latency_eligibility`
gate stays. Closed; revisit requires new evidence.

**D7 — Spatial protocol: keep 6, don't chase 8 (architect delta from
research DECISION 7).** The research's own evidence puts diminishing
returns at ~5–8 positions; the shipped default of 6 (an explicit owner
ruling) sits inside that plateau. No change; the justification is now
recorded. Revising it later touches `DEFAULT_ROOM_POSITION_COUNT` +
`ROOM_POSITION_COUNT_CHOICES` and the wizard's rendered position copy
— two constants and a template token, not literally one constant. The
1/3-position quick modes stay (noting D5: below 3 positions, boost
admission is refused). An MMM mode for UMIK-2 users is noted as a
possible future, not scheduled.

## PR ladder (RC1–RC5)

Standard per-PR gate: implementation → independent Opus adversarial
review to 0 blockers / 0 should-fixes → CI green → merge. Serial local
lanes (`-p no:randomly`).

- **RC1 — seam SSOT + evidence reader (lands #1787).
  ✅ LANDED 2026-07-28.** One
  boundary-SSOT module; all ten literal sites routed through it with
  behavior identical (boundary still 350) — **with one named
  exception**: the `SessionConfig.peq_f_high` shadow-copy defect is
  fixed here, so a `safe` session's acceptance, confidence, and
  disclosed `correction_band_hz` follow the strategy's actual band;
  the safe-session before/after is documented in the PR body. The
  SNR band tables (`acoustic_quality.SNR_BANDS_HZ` ≡
  `snr_policy.CROSSOVER_SNR_BANDS_HZ`) are deliberately **not**
  routed — capture-quality vocabulary stays static and
  cross-instrument, and the existing identity pin keeps holding. The
  candidate evidence payload is extended (validity floor + gated spec
  curve become candidate-borne, era-tolerant) and the `correction/`
  read accessor resolves the applied candidate through the existing
  fingerprint gate; contract tests pin the SSOT
  drift guard and the clamp-bounds/spec-edge co-ownership (D3).

  *As-built notes (2026-07-28).* Shipped as
  [`jasper/audio_measurement/room_boundary.py`](../jasper/audio_measurement/room_boundary.py),
  the applied-candidate read now split between
  [`jasper/active_speaker/crossover_contract.py`](../jasper/active_speaker/crossover_contract.py)'s
  `legacy_manual_preservation_state` and
  [`jasper/active_speaker/candidate_bank.py`](../jasper/active_speaker/candidate_bank.py),
  and [`tests/test_correction_boundary_ssot.py`](../tests/test_correction_boundary_ssot.py). Four
  deltas from the text above, all deliberate:
  (i) the SSOT is homed in **`audio_measurement`, not `correction`**.
  The reason is *not* a strict layer order — `correction` and
  `active_speaker` import each other in both directions
  (`active_speaker.seat_level_ramp` -> `correction.coordinator`;
  `correction.runtime_safety` -> `active_speaker.runtime_contract`),
  so neither is below the other. The property that actually earns the
  home is narrower and verified: `audio_measurement` is **imported by
  both and imports neither**, so it is the one package every consumer
  — including `audio_measurement.analysis`, itself a routed site — can
  read the boundary from with no new cross-package edge. A contract
  test pins that invariant;
  (ii) the candidate extension needed **no change to
  `measured_crossover_candidate.py`** — the floor and gated curve
  ride *inside* `exclusion_evidence`, a free-form mapping already
  fingerprinted only when non-empty, so era tolerance came for free
  and no `optional`-set or tamper-check line had to move;
  (iii) this plan named `expected_candidate_fingerprint` as the gate,
  but that identifier is the **baseline** candidate's fingerprint, a
  different vocabulary from `MeasuredCrossoverCandidate.fingerprint`,
  and no existing helper rehydrates the applied measured candidate.
  The seam therefore reuses the identity the apply path already
  records — `source.measured_candidate_fingerprint` — which is the
  same staleness fact, read from the same place, without inventing a
  new rule;
  (iv) `SessionConfig.peq_f_low`/`peq_f_high` were **removed** rather
  than re-pointed, so the shadow copy cannot return. One further
  defect of the same class surfaced and was fixed in passing:
  `status.session_config_payload` also reported
  `cfg.correction_strategy` (always the default) rather than the
  household's actual pick, which would have contradicted the
  now-correct band on the same payload.

  *Carried into RC3 as an OPEN decision.* `safe` composes as
  `ROOM_BOUNDARY_MIN_HZ`, which is provably identical to D1's
  `min(250, f_t)` for every admissible f_t (the clamp floor is 250),
  so it needs no revisit. **`assertive` does not have that
  guarantee.** RC1 expresses it as `ROOM_BOUNDARY_MAX_HZ` to preserve
  its shipped 500 Hz, but D1 never adjudicated assertive under a
  per-room ceiling, and the two concepts differ: MAX bounds how far
  the *estimator* may be trusted, while assertive's band is a
  power-user policy choice. At f_t = 280 a static 500 would leave
  assertive correcting ~220 Hz of what D1 assigns to Tier B's
  residual-only regime. RC3 must decide assertive explicitly rather
  than inherit the equality. Also note the strategy table binds the
  SSOT's *static* module-level values at import time, so RC3's f_t
  requires editing that composition in `strategy.py` — RC1 confined
  the change to one place, it did not remove it.
- **RC2 — T60 estimator + per-room f_t.** Offline Schroeder-integral
  T60 recomputed from persisted raw captures
  (`impulse_response_from_capture`); a bundle whose raw audio was
  pruned refuses with a named reason and falls back to 350, disclosed.
  The coarse room-size input (D1) supplies V; f_t =
  clamp(2000·√(T60/V), 250, 500) with `either input missing → 350`
  and the estimate + inputs + confidence disclosed on the session
  surface; replayed against archived room bundles with results
  recorded in the PR body. No consumer flips yet.
- **RC3 — Tier A ceiling goes per-room.** The boundary SSOT resolves
  to f_t; strategies compose; acceptance/confidence/evidence/SNR bands
  follow via RC1's routing; verify semantics unchanged. Prediction 2
  (per-room f_t lowers post-correction spatial variance in rooms whose
  estimate is far from 350) is graded on available bundles and
  recorded.
- **RC4 — Tier B residual trend correction (opt-in/experimental).**
  Residual basis from the evidence seam (level normalization pinned +
  tested); broad-bell vocabulary with the D2 caps; calibrated-mic
  gating; headroom disclosure; **Tier-B-scoped acceptance
  thresholds** — the modal defaults (6.0 dB band regression trigger)
  cannot resolve a ±4 dB-authority tier, so RC4 names its own
  threshold pair with rationale, and the round-trip contract test
  covers both the silent-drop paths and the loud
  bass-extension-recompose refusal. Research prediction 1's
  gated-curve half of the rollback trigger is adapted honestly:
  per-apply verify is the in-room acceptance loop; the gated-curve
  non-degradation check runs at the next gated VERIFY/recommission
  (cross-instrument re-measure is not a per-apply cost) — this delta
  from the research text is deliberate and disclosed here. Promotion
  from opt-in to default happens only after a hardware session grades
  prediction 1.
- **RC5 — evidence-gated LF boosts.** The new
  `LF_BOOST_PRESENCE_FRACTION` admission (with the N≥3 refusal and
  quantisation table from D5) + per-bin boost array + the +6.0 total
  cap + cost disclosure + the **three enumerated design-of-record
  amendments** (D5 i–iii, the owner-visible doctrine changes) +
  prediction 4 grading (boosts into admitted dips realize ±1 dB on
  re-measure; refused-dip boosts would not — validated by simulation
  and the next hardware session).

Sequencing: RC1 → RC2 → RC3 are strictly ordered; RC4 and RC5 are
independent of each other after RC3. RC1 can land immediately (no
dependency on gating v2); RC4 consumes whatever floor semantics are
current — the seam reads the payload key, which gating v2 keeps
stable.

## Traps (the review hunts these)

- **Measured-narrow-stated-wide.** Tier B claims are graded only on
  the instruments and bundles named in each PR body; "improves the
  in-room average" without a named corpus is a defect.
- **The ten sites, not seven — and the one that must NOT move.**
  RC1's routing must cover `acoustic_quality`'s re-clamp and
  `analysis.deviation_metrics` — the two sites that would silently
  defeat a raised ceiling. The SNR band tables are the deliberate
  non-mover: routing them would either break the cross-instrument
  identity pin at RC1 or drag the gated instrument's capture-quality
  bands onto a per-room boundary at RC3, making banded SNR
  non-comparable across sessions. They stay static, and the trap is
  named so nobody "completes" the routing helpfully.
- **Round-trip survival.** Any Tier B filter must survive
  `extract_room_peqs_from_config_text` recompose round-trips; a
  round-trip contract test ships with RC4. Shelves stay out until the
  reader is extended.
- **Unknown ≠ zero.** Missing evidence seam data disables Tier B with
  disclosure; it must never fall back to correcting the raw in-room
  curve above f_t.
- **Boost cost is level, and it is disclosed.** The headroom trim
  means total boost trades maximum level 1:1; the +6.0 cap must show
  its cost in the session surface, mirroring L5's disclosure doctrine.
- **Taste-layer unity asymmetry.** Taste EQ boosts ±12 dB at unity
  with no headroom trim while room correction auto-trims — a
  pre-existing doctrine asymmetry, flagged here for a future decision,
  NOT silently "fixed" in this ladder.
- **No new correction above 1 kHz, ever, in this ladder.** The hard
  stop is doctrine; a PR that reaches above it is out of scope by
  definition.

## Acceptance

The ladder is done when: the seam SSOT makes the 250/350 disagreement
structurally impossible (contract-tested); a room with a measured
Schroeder estimate gets a disclosed per-room Tier A ceiling; Tier B
corrections are derived from the residual basis, capped, disclosed,
calibrated-mic-gated, and survive recompose round-trips; LF boosts
land only on spatially-persistent modal evidence with their level cost
disclosed; and no code path corrects the raw in-room curve above f_t.

Last verified: 2026-07-28
