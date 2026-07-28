# Room-correction regime — work order (issue #1791)

> **Status: adopted work order (2026-07-28).** Synthesized from the
> owner-run competitive deep-research result (verbatim:
> [`docs/research/2026-07-27-acoustics-round-2/02-room-correction-competitive.md`](research/2026-07-27-acoustics-round-2/02-room-correction-competitive.md);
> prompt and laptop-side evidence in
> `captures/room-correction-research-20260727/`) against the code as
> verified on 2026-07-28. Anchors
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
gated response — JTS's structural advantage; no shipping competitor
owns the anechoic curve — and **nothing above 1 kHz**. FIR/phase is
rejected on quantified grounds. Evidence-gated LF boosts are unlocked,
but on room-instrument evidence, not the HF null registry (see D5's
delta).

## Current state (verified against code 2026-07-28 — the review bar)

- The "350 Hz cap" is per-strategy, not global:
  `CORRECTION_STRATEGIES` in
  [`jasper/correction/strategy.py`](../jasper/correction/strategy.py)
  ships `safe` 25–250 / `balanced` 20–350 (default) / `assertive`
  20–500 (`cuts_only=False`, `max_total_boost_db=3.0`, not offered on
  the household surface per `HOUSEHOLD_CORRECTION_STRATEGY_IDS`).
- **Ten** 350-literal sites, not #1787's seven: the issue's seven plus
  `acoustic_quality.SNR_BANDS_HZ`, `acoustic_quality`'s
  `min(350.0, peq_f_high)` re-clamp (would silently cap any raised
  ceiling), and the shared metric
  `audio_measurement/analysis.deviation_metrics(f_high=350.0)` that
  acceptance/verify/envelope all flow through. `session.py`'s
  `SessionConfig.peq_f_high` is an independent copy not passed to
  `design_correction` — a latent divergence pair.
- Vocabulary: peaking bells only (`correction/peq.py`, greedy
  peak-fit); shelves exist only in the taste layer. The round-trip
  reader `extract_room_peqs_from_config_text` regex-matches peaking
  blocks only — **any new room-layer filter type would be silently
  dropped on every recompose** (taste-EQ edit, bass-extension
  recompose, multi-room bake).
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
- Cross-layer reach: `jasper/correction/` reads **neither**
  `validity_floor_hz`, the gated spec curve, nor the null registry
  (zero imports; the persisted registry lives in the active-speaker
  commissioning evidence bundle with no reader). The null registry's
  derivation band is the gated instrument's HF echo band (~4–18 kHz)
  — it classifies speaker-fixed interference, **not LF room nulls**.
- Schroeder: `calibration_agent/tools.compute_schroeder` is a dead
  stub (always `available: False`); no RT60 estimator exists. The IR
  is already persisted per capture, so an offline Schroeder-integral
  estimator needs no new capture.
- Layer stacking: room PEQs ride the stereo program bus pre-split on
  both graph paths, upstream of the taste layer, crossover, driver
  linearization, bass extension, limiters; `volume_limit ≤ 0` is
  load-enforced.

## Adopted decisions

**D1 — Bandwidth: two-tier ceiling replacing the hard wall. ADOPT
(research DECISION 1).** Tier A (full-resolution modal correction,
existing vocabulary and strategy character) runs 20 Hz → **f_t**, a
per-room transition = Schroeder estimate clamped to **[250, 500] Hz**,
falling back to **350** when estimation is uncertain (and always until
RC2 ships the estimator). Tier B (residual trend only, D2) runs f_t →
**1,000 Hz hard stop**. Above 1 kHz the room layer does nothing —
recorded as doctrine. Implementation constraint: **one boundary SSOT**
consumed by every one of the ten literal sites (strategies compose
their character with the room-derived boundary; `safe` keeps its
tighter `min(250, f_t)` cap); `SessionConfig.peq_f_high` and the
`acoustic_quality` re-clamp are collapsed into the SSOT, not left as
copies.

**D2 — Attribution: Tier B corrects only what the room added. ADOPT
(research DECISION 2), via a new applied-speaker-evidence seam.** In
Tier B the correction target is the residual: spatially-averaged
in-room curve minus the applied profile's **gated** curve, level-
normalized over a shared trusted band (pinned in the RC4 design).
Double-correction becomes impossible by construction — the room layer
never re-flattens voicing the gated layer set. Code reality: the gated
curve, validity floor, and null registry are unreachable from
`correction/` today, so RC1 builds **one read-only seam** — an
applied-speaker-evidence reader exposing `{gated spec curve, validity
floor, null registry, config fingerprint/era}` from the applied
commissioning evidence. Staleness rule: evidence must
fingerprint-match the applied baseline profile; missing or stale
evidence (passive speakers, pre-commissioning rooms, recommission in
progress) **disables Tier B with disclosure** — Tier A continues
unchanged. Tier B vocabulary: **broad peaking bells only, Q ≤ 1.0,
±3 dB per term, ±4 dB net across the band** (architect delta from the
research's shelf/tilt vocabulary: the recompose round-trip reader is
peaking-only, and bells within these caps express the same broad
trends — shelf/tilt terms are deferred behind a reader+round-trip-test
extension, recorded as a follow-up, not silently added). Phone-mic
tier: Tier B requires a calibrated mic in v1 (phone mics are least
reliable exactly in 500 Hz–1 kHz); phone sessions get Tier A only,
with the limitation disclosed. Revisit when phone-mic calibration
evidence exists.

**D3 — The shared band: gated-layer primacy with a derived handoff.
ADOPT (research DECISION 3); lands #1787.** Below the applied
profile's validity floor (read from the evidence seam; the
support-derived floor once gating v2 PR-G1 lands — same payload key),
the room layer + bass extension own everything. From the floor to f_t,
the gated layer owns absolute tonal balance; the room layer cuts modal
peaks and corrects broad trend against the residual basis where
available. The 250 Hz `flat_spec.SPEC_BANDS` lower edge vs the room
ceiling is pinned by a contract test through the same SSOT so the two
layers can never silently disagree about the seam again. When the
evidence seam is absent, the handoff falls back to the current
constants — with the fallback disclosed, never silent.

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
present with agreeing depth in ≥ the position-presence fraction of
cloud positions (position-dependent SBIR/interference dips average
out and are refused), (b) **shape-plausible as modal** — bounded Q and
depth (deep narrow nulls refused with the existing
`nulls_not_boosted` grammar), and (c) within headroom. Cap **+6 dB
per admitted dip**; total positive boost cap rises from +3.0 to
**+6.0 dB** with the emit-time headroom trim carrying the cost and the
cost disclosed ("this correction costs N dB of maximum level" —
mirroring the L5 doctrine on the speaker layer). `design_peq` gains
the per-bin `max_boost_db` array mirroring `max_cut_db`. Closed-loop
acceptance + auto-revert applies unchanged. **This amends the
design-of-record's +3.0 dB ceiling — an owner-visible doctrine change
carried in RC5's spec amendment, not slipped in.** Where the HF null
registry IS authoritative (the gated instrument's band), it continues
to bound the *speaker* layer's fit; nothing here touches that.

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
ruling, single-constant revisable) sits inside that plateau. No
change; the justification is now recorded. The 1/3-position quick
modes stay. An MMM mode for UMIK-2 users is noted as a possible
future, not scheduled.

## PR ladder (RC1–RC5)

Standard per-PR gate: implementation → independent Opus adversarial
review to 0 blockers / 0 should-fixes → CI green → merge. Serial local
lanes (`-p no:randomly`).

- **RC1 — seam SSOT + evidence reader (lands #1787).** One
  boundary-SSOT module; all ten literal sites routed through it with
  behavior byte-identical (boundary still 350); the
  applied-speaker-evidence read seam ({gated curve, validity floor,
  null registry, fingerprint}) with staleness rule and
  missing-evidence disclosure; contract tests pinning the seam and the
  250-vs-boundary relationship. Pure mechanics — no acoustic behavior
  change.
- **RC2 — RT60/Schroeder estimator + per-room f_t.** Offline
  Schroeder-integral RT60 from the already-persisted IRs; f_t =
  clamp(estimate, 250, 500) with `uncertain → 350` and the estimate +
  confidence disclosed on the session surface; replayed against
  archived room bundles with results recorded in the PR body. No
  consumer flips yet.
- **RC3 — Tier A ceiling goes per-room.** The boundary SSOT resolves
  to f_t; strategies compose; acceptance/confidence/evidence/SNR bands
  follow via RC1's routing; verify semantics unchanged. Prediction 2
  (per-room f_t lowers post-correction spatial variance in rooms whose
  estimate is far from 350) is graded on available bundles and
  recorded.
- **RC4 — Tier B residual trend correction.** Residual basis from the
  evidence seam (level normalization pinned + tested); broad-bell
  vocabulary with the D2 caps; calibrated-mic gating; headroom
  disclosure; acceptance extended over the Tier B band with the
  existing revert ladder. Research prediction 1's gated-curve
  half of the rollback trigger is adapted honestly: per-apply verify
  is the in-room acceptance loop; the gated-curve non-degradation
  check runs at the next gated VERIFY/recommission (cross-instrument
  re-measure is not a per-apply cost) — this delta from the research
  text is deliberate and disclosed here.
- **RC5 — evidence-gated LF boosts.** Spatial-persistence admission +
  per-bin boost array + the +6.0 total cap + cost disclosure +
  design-of-record amendment (the owner-visible doctrine change) +
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
- **The ten sites, not seven.** RC1's routing must cover
  `acoustic_quality`'s re-clamp and `analysis.deviation_metrics` —
  the two sites that would silently defeat a raised ceiling.
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
