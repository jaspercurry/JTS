# Tuning master plan — declared-design executor, linearization tournaments, LLM operator

> **Status: adopted plan.** This document is the decision record and planning
> authority for the measurement/tuning program: the crossover is a
> **declared design that JTS executes**, driver linearization is a **measured
> tournament** driven by an LLM operator over SSH, and room correction stays
> **pinned** on a shared substrate. The prior plans it supersedes are listed
> in "Supersessions and standing docs" below.

Authority model and method are **not restated here**: the loop's rules live in
[measurement-loop-doctrine.md](measurement-loop-doctrine.md) (the authority
model, the guiding principle — least-bad measured, honed in bites — the closed
hard-stop list, the nanny test) and the execution method lives in AGENTS.md.
Round-time constants an LLM driving a session needs are in "Measurement
program constants" below; [tuning-methodology.md](tuning-methodology.md) is
what states the order of operations and the decision rule at each step.

## Product invariants

1. **Commissioning ends in a usable speaker.** Declare drivers → run the
   research intake → confirm sound → done: the seed baseline (declared
   crossover + datasheet trims + safety rails) is a complete product state.
   Linearization and room correction are optional enhancement loops that
   always start from that seed. No tuning flow may become a prerequisite for
   using the speaker.
2. **Executor, not hunter.** Crossover corners are declared by the operator
   (with the research intake as a prefill), compiled, and verified — never
   chosen by a measured search, and no path applies a machine-recommended
   corner (the R21 "apply-vertical" candidate is cancelled — see
   Supersessions). Exploration of corner variations happens as offline
   *simulated evaluation* over banked solo captures, at zero capture cost.
3. **The LLM recommends; the measurement decides.** Arithmetic predictions
   (the transfer function of a staged filter set, the complex sum of measured
   solos) are kept as instruments — every tournament candidate carries a
   pre-registered expected delta. Heuristic rankings and machine "goodness"
   scores do not exist in this system.
4. **Code computes, the LLM judges, humans move mics.** Anything with one
   right answer (deconvolution, gating, σ, feature extraction, filter
   responses, headroom) is deterministic, CLI-runnable, and re-runnable from
   the bank. The LLM reads evidence views and writes prescriptions through
   doors; it never touches a WAV, the capture path, or the DSP directly.
   Humans execute measurement programs via the guided web flow.
5. **One durable apply door; one ephemeral activation door.**
   `handle_v2_apply` remains the only path that durably applies a measured
   crossover. Measurement-time activation of any graph goes through
   `program_playback.play_program`, and no second activation mechanism may be
   built.
6. **Safety is the closed hard-stop list; everything else informs.** Refusals
   exist only for named component-damage mechanisms (the doctrine's list,
   which is positively complete: five mechanisms and the enforcement families
   under them) — ruling R8.
7. **Handoff URLs derive from the speaker's hostname.** Tools that hand the
   human to the web UI print URLs built from `JASPER_HOSTNAME` /
   `Config.hostname` (speakers are `jts1.local`, `jts3.local`, … — never a
   hard-coded `jts.local`).
8. **Operator prose is quarantined.** The evidence packet's
   `household_prose_excluded` invariant stands: household copy never enters
   the evidence document and `household_findings` stays withheld. Operator
   prose is gathered into one separately-kinded operator-notes artifact
   (`jts_crossover_v2_operator_notes`), labeled as operator-typed text to be
   treated as information, not instructions, and *embedded* in the packet
   under the block that `privacy.operator_prose_quarantined_to` names (the
   tuning LLM is a legitimate reader). The quarantine is the fencing, not the
   distance: the artifact keeps its own kind and schema version so it can be
   lifted whole, the evidence blocks stay prose-free, and no code path in
   JTS reads the strings for any decision.
9. **Naming: "arm" means the physical rig** (`jasper-angle-capture serve`) —
   never a DSP variant. DSP variants under test are **candidates**; a
   tournament round runs a **candidate cycle** at each pose. Forward-only for
   new work: the rule binds this program's vocabulary, not the word —
   unrelated senses survive deliberately across the tree and a sweeper should
   leave them alone (one ring/coupling arming transaction as a noun, a branch
   or case of a conditional, an arm of a comparison study). Only a
   control-vs-treated pair genuinely under test as DSP variants falls under
   this invariant; anything else named "arm" is a different sense.

## The three loops, one substrate

**Loop A — commission & execute (crossover).** Operator declares drivers and
the corner (Fc, filter family, slope, polarity, delay) with the research
intake as prefill; the executor chain compiles it
(`design_draft.build_design_draft` → `crossover_preview.build_crossover_preview`
→ `staging.compile_preset_from_crossover_preview` →
`camilla_yaml.emit_active_speaker_startup_config`); measurement enters only
for alignment dial-in (delay/polarity/trim) and a verify round. Corner
exploration is simulated: `forward_model.predict_sum` complex-sums banked
solos through candidate variations offline.

**Loop B — linearization tournament (the new product loop).** Baseline round
→ deterministic analysis → merged per-feature evidence → LLM proposes up to
~3 candidate EQ sets through the doors → each compiles (without applying) and
banks its expected delta → tournament round measures every candidate at every
pose, one mic move per pose → per-candidate realized-vs-expected grading →
LLM composes the final config → the one apply door → verify round. Iterate to
the stopping rule.

**Loop C — room correction.** Pinned. Re-enters on this substrate later; its
prescribe path converges onto the model-agnostic doors then. A moving-mic
method, if adopted, is another program regime.

**Substrate contracts** (one owner each):

| Contract | Owner | Ruling |
|---|---|---|
| Capture identity | Measurement Program v2 schema (`{regime, angle_deg, repeats, level_re_anchor}`) | elevation ships later as an `(azimuth, elevation)` tuple, not a plane enum. Candidate-under-measurement and capture provider are **provenance**, not identity — the graph fingerprint is already recorded per capture |
| Measurement programs | named, versioned pose lists as data (`baseline`, `tournament`, `verify`, `spot`) | LLM selects a program + bounded params via a staged request (generalizing the `angle_capture` request spool); it never invents free-form geometry. Arm and guided human are interchangeable providers behind `capture_source` |
| Retention | the bank | mic WAV + stimulus are **evidence**, both flows (room's `sweep_meta` regenerates its stimulus deterministically — equivalent). IR and complex TF are **derivable caches**, prunable. Ring eviction never crosses an active round's boundary; budgets stated per store |
| Analysis kernel | `jasper/audio_measurement/` | owns the math and the policies (smoothing widths, arrival window, σ definitions — one owner per policy). Imports neither flow |
| Evidence | `evidence_packet.build_crossover_evidence_packet` | a **computed view** over the bank, schema-versioned; consumers rebuild it, nothing reads a stale packet file. The classification block carries both the 7-key `FeatureVerdict` gate-facing view and the classifier artifact's full `lab_rows`, side by side (`_classification_block`'s returned dict carries both `"verdicts"` and `"lab_rows"` keys). Every published uncertainty field labels itself **random or systematic** — the two must never pool. Where the evidence cannot separate them, the field says so under `unseparated` and names what would, never being filed as a kind it does not have |
| Mechanism vocabulary | one taxonomy, joined in the per-feature record (not yet built) | `jasper/attribution`'s mechanism ids + fix classes are meant to become the registered vocabulary of the per-feature evidence record, joined with the classifier's classifications and the envelope's depth reasons. Detectors beyond the two shipped discriminators (min-phase/gate ladder, position-invariance) are **not built** — mechanism inference over the record is the LLM's job |
| Prescription doors + spool | `driver_prescription` / `blend_prescription` / `alignment_prescription` / `topology_prescription` + `prescription_spool` | all four envelopes versioned; refusal vocabularies converge to the hard-stop list; spool stays single-slot last-wins (logged) |
| Activation / apply | `program_playback.play_program` / `handle_v2_apply` | invariant 5 |
| Verification | `verification.py` + `flat_spec` + `delta_probe` | one grading currency; predicted-vs-measured is first-class |

## Decision register

Owner rulings, cited by number elsewhere in this codebase (`ruling R8`,
`house ruling R11`, …) — the R-numbers are stable identifiers; do not
renumber.

| # | Ruling | Disposition notes |
|---|---|---|
| R1 | Delete the hunt: `crossover_v2/{search,objective,candidate_space}.py`, `fc_sweep`'s sweep half, `fc_selector` + its live recommendation surface. Keep `forward_model` (simulated evaluation). | All deleted; `resolve_fc_search_band` (a carve-out of the same hunt) is deleted too. `forward_model.py` survives as offline simulated evaluation with no ranking/optimization search over it. |
| R2 | Filter vocabulary: entry-time validation now; **Butterworth support is funded work in this plan**, scoped to its true blast radius. | Blast radius (indicative, not exhaustive — verify against the tree before scoping the work): `branch_chain.CrossoverSection` gains a family tag and `crossover_response_db` / `crossover_response_complex` become family-aware (the headroom ledger bottoms out there); `camilla_emit.emit_linkwitz_riley` (the emitter itself); `branch_peak._MODELLED_COMBO_TYPES` (closed allowlist on the headroom path); `graph_safety.output_highpass_protected` plus its sibling predicates (`protection_requirement_present`, `tweeter_guard_present`, `sub_guard_present`, `sub_audible_guard_present`, `mains_highpass_present`) that match the `LinkwitzRiley*` literals; `runtime_contract`'s four literal sites and its `SUPPORTED_LR_ORDERS` reads; `profile.SUPPORTED_CROSSOVER_TYPES`/`SUPPORTED_LR_ORDERS`; `staging._normalise_filter_type`/`_slope_to_lr_order`; `crossover_declaration` comparison. Not yet built. |
| R3 | One consolidated free-text notes field (driver notes fold in; guided placeholder bullets). Quantitative fields must compile or gate safety; fields with neither sink are deleted. | Prose is quarantined per invariant 8. The research intake's validated JSON output (safety facts, prefills) is data, not prose — it persists in the draft and rides in the packet's declarations. |
| R4 | LLM operator v1 = cloud agent over SSH. No embedded-advisor UX. Room's embedded tuning-LLM gets no new investment until Loop C re-enters. | The commissioning copy-paste research flow stays as-is. |
| R5 | Division of labor per invariants 3–4. | A predicted-vs-predicted veto (correction that measurement predicts is not an improvement) is not yet built. |
| R6 | Program menu + bounded LLM selection; provider orthogonality. | No identity-schema change needed (see substrate table). |
| R7 | Tournament mechanics: harden `play_program` (prove-at-quiet-floor, read-back proof, shielded restore), compose-without-apply, expected-delta banking, candidate cycle per pose, N-candidate comparator. | **Same-corner contract:** candidates in a round hold the declared corner (Fc, family, slope, topology) fixed and may vary linearization EQ, trim, delay, polarity. This preserves the excitation-safety premise (permitted bands are derived from the graph's crossover high-passes) and matches `candidate_bank`'s republish gate. A future corner-varying candidate type must re-derive the permitted band per candidate. `play_program`'s prove-at-quiet-floor/read-back/shielded-restore hardening is not yet built. |
| R8 | Boost caps widen: per-filter **12 dB** and composed **12 dB**, with `MAX_SPL_SPEND_BOUND_DB` re-proved at **13 dB** (composed cap + `branch_chain.HEADROOM_MARGIN_DB` = 1.0). | This overturns the prior `driver_prescription` ruling ("a prescription has no such prediction") on its own terms: the pre-registered expected delta supplies exactly that closed-loop prediction. The composed number is policy — the fit engine has no composed cap to inherit (`PER_FILTER_BOOST_CAP_DB` is a realization bound; total boost is deliberately unbounded there). The binding protections are unchanged: envelope per-bin depth (zero in measured nulls), realized-peak headroom charging, limiters, excitation safety plan, and the live adoption guard — a boosted intervention fails closed when the evidence is untrusted (`ADOPTION_UNPROVEN_BOOST`). Blend's `BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a headroom term; a summed capture cannot attribute a deficit to a driver). |
| R9 | Retention per the substrate table. | The banked-solo complex-TF loader feeding `forward_model` (`load_branch_pair`), plus cache policy + budgets, was the genuinely new work — it EXISTS. A lateral pose banks its own magnitude AND phase (`spatial.pose_curve_record`), so the loader reads the bank instead of re-deriving from WAV + stimulus; `round_views.verify_pose_curve` reads the round's banked VERIFY curve directly. |
| R10 | Evidence enrichment + the classifier/envelope/attribution join in one per-feature record; the classifier stays decoupled from the fit engine. | The packet's own `not_evaluated` rows (angle, `first_reflection_ms`, harmonics) are the spec. The join adopts one mechanism vocabulary (substrate table); not yet built. |
| R11 | Sequencing = artifact-dependency refusals, not a workflow engine. Runbook + one orientation verb. | The orientation verb reads the **same builders the doors read** — never a second tree-walker. |
| R12 | Legibility pass ("solo engineer" test): dead code out, enumerations honest, one artifact-kind vocabulary, one vocabulary per question. | Kind namespacing applies to new writes with a tolerant reader — no bundle migration. |
| R13 | Room correction pinned; the substrate stays flow-agnostic. | Nothing in `jasper/correction/` structurally resists this beyond vocabulary. |
| R14 | **Toolbox shape:** capture is two front doors — turntable measure (available only when the rig is attached, disclosed at runtime) and manual measure (guided walk) — onto ONE capture engine and record model that does not care who moved the mic. Analysis stays independent verbs: the always-relevant facts ride the banked round (lab rows, packet), and depth is pulled verb-per-question — never an aggregate "analyze everything" surface. | The record model already distinguishes candidate takes by applied-graph fingerprint, so measuring multiple candidates per pose is representable today; the candidate-cycle orchestration is R7's, sequenced BEHIND the forward model's acceptance runs (predict first, physically measure the winner). The methodology's contract carries the read-pattern ("depth is pulled, never dumped"); [ADR-0204](adr/0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md) owns the tiered surface this ratifies. |

## Measurement program constants

From the owner's sourced research memo (Toole/CTA-2034, Keele 1974,
Struck & Temme 1994, D'Appolito, Bücklein 1981, Toole & Olive 1988, REW and
VituixCAD practice), reconciled against JTS's own declared ceilings and
ratified rulings. Some of these constants are coded, named below where they
are; the rest are documented heuristics the LLM operator applies by hand
each round, per the "code computes, the LLM judges" division of labor
(invariant 4) — marked **[heuristic]** below where no coded gate exists.
⚠ items have in-house settling experiments (below).

- **`baseline` program:** 13 poses at 1 m — horizontal 0°, ±10°, ±20°, ±30°,
  ±40°; vertical 0°, ±10°, ±20° — plus one nearfield capture per woofer (and
  port, when vented). Repeats: ×4 at the 0° anchor pose, ×1 at every other
  pose, ×8–16 for the nearfield captures
  (`_BASELINE_FULL_POSES`, `ANCHOR_REPEATS` in `measurement_programs.py`).
  **The vertical poses are live**, and `jasper-angle-capture stage --program
  baseline` stages them.
- **Drive level is anchor-relative, never an absolute program constant.**
  The baseline level is the ratified measurement anchor — 75–80 dB SPL at
  the microphone, seeded from the seat-level reference
  (`DEFAULT_TARGET_DB_SPL` = 77.5 dB SPL) — expressed as `level_re_anchor =
  0`. The research memo's 86 dB industry convention is noted and **rejected
  as an absolute**: it exceeds the declared `max_commissioning_level_db_spl`
  ceiling (85 on every shipped preset, which is also the schema's top —
  the schema validates both `initial_sweep_level_db_spl` and
  `max_commissioning_level_db_spl` into 45–85 dB SPL), which sits on the
  doctrine's closed hard-stop list. Absolute SPL anchoring uses the mic's
  parsed sensitivity (`calibration.parse_calibration_sensitivity`).
- **Escalation [heuristic]** (the second level, on anomaly): anchor + 10 dB
  where the preset's declared ceiling permits, otherwise the largest
  separation the ceiling allows, with the shrunken separation disclosed in
  the evidence. Both shipped presets declare 85 — the schema's own top — so
  for them raising the ceiling is already spent as a lever, and a shrunken
  separation is disclosed rather than bought with a higher stop; a
  household preset declaring less than 85 keeps that lever within the same
  45–85 range. `SafetyEnvelope.escalation_step_db` is a *different*,
  unrelated schema field (a preview-preset construction knob) — do not read
  it as this rule's implementation. `delta_probe`'s commanded-amount
  regression remains the primary compression detector and needs no second
  level, which is why escalation is on-anomaly rather than default.
- **Distance rule [heuristic]:** measure at the largest distance where the
  gate still clears the driver-summing farfield (≈3× the largest baffle
  dimension); default 1 m, fall back to 0.5 m only when the room forces it,
  flagging the summing-nearfield phase caveat. Conditionals: C-t-C ≥ 1λ at
  the crossover → tighten vertical steps to 5°; gate < 3 ms at 1 m → move to
  0.5 m; midband repeat σ > 0.5 dB → warn (fix mic mounting) before trusting
  any delta — a disclosed warning, never a refusal.
- **`verify` program:** the reverse-null polarity check (flip the tweeter:
  correct alignment yields a deep on-axis null at Fc) and, once elevation
  ships, the ±10°/±20° vertical tilt check — both run as back-to-back
  candidates at one pose, which is exactly the candidate-cycle primitive.
  The commissioning flow's existing `normal`/`reverse`/`delay_null`
  evidence path (`commissioning_evidence`) is the shipped reverse-null
  implementation this program reuses rather than re-inventing.
  `null_walk` is the bounded delay stepping's decision content — spec,
  schedule, scoring, selection — and `active_speaker.delay_sweep` bounds
  one walk and states the depth bars it is read against.
  `jasper-round-views delay-landscape` is the operator door onto the computed
  optimum. A delay is found by compute-then-confirm
  (`crossover_v2.delay_landscape`), which proposes the coordinate from
  banked complex transfers and confirms it with three staged takes; the
  flip and the swept delay both ride the protected-neutral MEASURE program
  via `measure_spec.inverted_roles_for` and `measurement_delays_us`.
- **Boost probe [heuristic]:** +6 dB is the standard probe (+3 dB when
  headroom is tight). A dip is correctable when measured gain-back ≥ ~0.8×
  commanded AND excess group delay is flat there; non-correctable when
  ≤ ~0.5× or excess-GD is non-flat. Policy: cut peaks freely where excess-GD
  is flat; probe every candidate dip before filling; never fill a failing
  dip (peaks are far more audible than dips — Bücklein).
- **Level dependence:** single-level default; escalate per the escalation
  bullet only on anomaly — `delta_probe`'s `level_dependent_shortfall`
  verdict is the trigger. The in-program two-level pilot pair
  (`program._append_leading_pilot_pair`) already exists as the low-cost
  in-capture level probe. Thermal compression needs sustained stimulus
  (tens of seconds) and stays out of v1.
- **Stopping rule [heuristic]:** stop when the aggregate error metric (NBD
  or target-weighted RMS over the trusted band) stops improving by more than
  ~2× its own aggregate sampling standard error — computed over the
  **random** uncertainty terms only — for **2 consecutive rounds**, AND the
  residual sits within ±1–1.5 dB at 1/3–1/6-octave smoothing. No per-bin
  significance chasing (multiple-comparison inflation).
- **Nearfield splice v1 (not yet built):** mic within 0.055×D of the dust
  cap; valid up to ≈4311/D_inches, bounded by the enclosure limit c/(3·M)
  (Struck & Temme); port scaled by √(Sp/Sd) and complex-summed (organ-pipe
  spikes excluded); baffle step applied analytically from the declared
  baffle width (f₃ ≈ 115/W, gradual 6 dB shelf); splice in the overlap band
  below the gate floor and above box tuning. Adds one quantitative
  commissioning field: baffle width (plus port dimensions when vented).
  ⚠ splice error budget ≈ ±1–3 dB (synthesized figure — E3).

## Considered and deliberately not built (v1)

Recorded so absence reads as a decision, not an oversight. Each may be
revisited when evidence demands it.

- **Alternative excitations** (MLS, stepped sine, multitone, periodic
  noise): the synchronized ESS plus the in-program pilot pair cover the
  loop's needs; distortion separates in time under ESS, which the others
  cannot do.
- **Frequency-dependent windowing**: genuinely disputed in the field; the
  gating SSOT stays a fixed, versioned window.
- **Window-shape variation as an artifact probe**: the classifier's control
  suite (C0–C3 injected controls plus the C4 pair scale) is the instrument
  self-test; a shape-variation probe adds little over it.
- **Ground-plane and two-distance regimes; air absorption modeling.**
- **Calibration phase application and incidence-aware math**: the cal
  file's phase column is read past and never kept — `CalibrationCurve`
  carries magnitude only, because `apply_calibration_curve` is a real-dB
  correction with no complex path to feed. This ledger entry is the record;
  orientation routes the vendor fetch only.
- **Bespoke mechanism detectors** (port resonance, cone breakup, room mode,
  panel resonance, rattle/impulsive defects, amplifier-clipping
  heuristics): the per-feature evidence record carries the discriminating
  fields (harmonics by order, level dependence, position variance, Q, gate
  behavior); mechanism inference over those fields is the LLM's half of the
  division of labor, guided by the runbook's mechanism-signature table.
  The two shipped discriminators (min-phase/gate-invariance,
  position-invariance) stay code.
- **σ_placement as a distinct statistic**: run calibration experiment E2
  first; add the re-placement statistic only if E2 shows placement error
  matters at our σ levels.
- **Thermal compression measurement**: requires sustained stimulus;
  `delta_probe`'s shortfall verdict is the revisit trigger.
- **Household-facing "uncorrectable" copy**: the classifier verdict is in
  the record; the LLM narrates it. No new web copy surface.
- **Embedded advisor UX** (per R4).

## Supersessions and standing docs

This plan is the planning authority for the measurement/tuning program; the
docs below carry a historical-tag or banner pointing back here.

**Superseded:**

- `docs/historical/audio-commissioning-roadmap.md` — the previous
  program-wide roadmap for the identical scope. Replaced whole; its Ethos
  section ("least bad is still the overall guiding principle") moved into
  the guiding-principle section of
  [measurement-loop-doctrine.md](measurement-loop-doctrine.md#3-the-guiding-principle--least-bad-measured-honed-in-bites)
  instead, since the doctrine is where the loop's rules live.
- The cancelled CURRENT-POSITION `next_mission` — the **R21
  "apply-vertical" candidate (a path that applies a machine-RECOMMENDED
  Fc) is cancelled** by invariant 2. The `R21` token elsewhere in the tree
  (accept-receipt provenance in `crossover_v2/contracts.py` and the
  crossover-measurement-v2 ledger) names shipped vocabulary from a prior,
  unrelated ruling series and is unaffected by this cancellation.
- `docs/historical/llm-native-tuning-workbench-plan.md` — its
  planning-authority claim and its "experiment workspace is the one new
  mutation owner" claim are superseded: `play_program` is the single
  activation door (invariant 5), and the SSH-operator + runbook model
  (R4/R11) replaces the workbench direction. Its deferred embedded-agent
  appendix stays deferred.
- `docs/historical/attribution-stage-plan.md` — its open work orders are
  absorbed, not pursued independently: the reverse-null probe → the
  `verify` program; harmonic extraction and position-variance → evidence
  enrichment (R10); the serial dial-in loop → Loop B's tournament
  mechanics; the mechanism registry + fix-class vocabulary → the
  per-feature record join (substrate table). The attribution package's
  shipped findings/promotion code is substrate for that join, not a
  parallel system.
- The five-file linearization family (`flat-linearization-*`, the 80/20
  revision, the integrity ladder) is archived into
  [`historical/linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md):
  foundational work fully absorbed into Loop B.

**Standing, compatible (no action):** `gating-v2-plan.md` (feeds the
kernel), `HANDOFF-bass-extension-plan.md` (parked; converges with bass
extension), `room-correction-regime-plan.md` (parked under the Loop C pin),
and the information-design docs. `tuning-operator-runbook.md` stays the
operational canon. Three landed records moved to
[`historical/`](historical/): the productization design, the two-stage
commission flow plan, and
[`crossover-v2-engine-design.md`](historical/crossover-v2-engine-design.md),
which carries the file map — `fc_selector.py` and `fc_sweep`'s sweep half
are gone, so the file map carries no rows for them to amend.

## Calibration experiments (optional, lab-rig)

- **E2 — σ(f) table (do first, cheap):** 16 back-to-back gated sweeps, fixed
  mic; per-bin σ across the band; sets the trusted-band and stopping
  thresholds from measured reality. Two additional customers: the
  round-evidence constants `MEASURED_BENEFIT_MARGIN_DB` (0.5) and
  `ITERATION_PLATEAU_DB` (0.25), both self-described assumptions awaiting
  exactly this study, reconcile against E2's numbers. **Has run** on at
  least one rig, in-band σ_repeat 0.03–0.17 dB at a fixed pose — well under
  both constants above. Its durable home is the banked repeat floor:
  `jasper-round-views repeat-floor <repeat rounds> --out PATH` writes the
  record; place that file on the speaker at
  `/var/lib/jasper/active_speaker_repeat_floor.json`, from which
  `bank-crossover-round.sh` pulls it beside every later round as
  `repeat-floor.json`. The packet's `in_capture_repeat_floor` reads it and
  derives the stopping plateau and benefit margin from it. Honestly
  unavailable on a rig that has banked no repeats.
- **E1 — lobe-tilt resolution:** inject +0.1/+0.2/+0.5 ms delay errors on a
  known-good alignment; vertical polar at 5° steps; adopt the coarsest step
  that shows a monotonic tilt at 0.2 ms.
- **E3 — splice error budget:** same speaker via nearfield splice vs
  ground-plane outdoors; RMS difference over 100–500 Hz is the real budget.
