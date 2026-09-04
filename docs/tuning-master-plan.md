# Tuning master plan — declared-design executor, linearization tournaments, LLM operator

> **Status: adopted plan (owner-ratified direction, 2026-08-21), execution in progress.**
> This document is the planning authority for the measurement/tuning program;
> the prior plans it supersedes are listed in "Supersessions and standing
> docs" below. Wave 0 landed 2026-08-21 (all five PRs merged); Waves 1–3 are
> underway (tracking: epics #2824/#2825/#2826 carry per-ticket state — not
> enumerated here; one planning authority, one tracker). Wave 6 (toolbox
> hardening after the flat campaign) was adopted 2026-08-31. The 2026-08-21
> adversarial gate review of this document returned 1 blocker and 7
> should-fixes, all folded into this revision (see Provenance).

Adopted 2026-08-21 (owner + architecture session). This is the decision
record and work plan for the measurement/tuning program: the crossover is a
**declared design that JTS executes**, driver linearization is a **measured
tournament** driven by an LLM operator over SSH, and room correction stays
**pinned** on a shared substrate. It supersedes the *search* framing of stage
P2 in
[active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md);
the amendment ships in Wave 2 together with the deletions that make it true,
so prose never leads code. The operator-facing companion,
`docs/tuning-operator-runbook.md`, is Wave 1 work.

Authority model and method are **not restated here**: the loop's rules live in
[measurement-loop-doctrine.md](measurement-loop-doctrine.md) (the authority
model, the guiding principle — least-bad measured, honed in bites — the closed
hard-stop list, the nanny test) and the execution method lives in
AGENTS.md ("The standing multi-agent method": conductor, adversarial gate to
0 blockers / 0 should-fixes, the owner's values). Every ticket below is
executed under both.

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
   `program_playback.play_program` — today it loads and restores in a
   `finally`; ticket 3.1 hardens it with prove-at-quiet-floor,
   read-back proof, and a shielded restore, and no second activation
   mechanism may be built (the workbench plan's "experiment workspace"
   mutation-owner claim is superseded — see Supersessions).
6. **Safety is the closed hard-stop list; everything else informs.** Refusals
   exist only for named component-damage mechanisms (the doctrine's list,
   which is positively complete: five mechanisms and the enforcement families
   under them). The burn-down converged per tickets 2.3/2.7 and ruling R8.
7. **Handoff URLs derive from the speaker's hostname.** Tools that hand the
   human to the web UI print URLs built from `JASPER_HOSTNAME` /
   `Config.hostname` (speakers are `jts1.local`, `jts3.local`, … — never a
   hard-coded `jts.local`).
8. **Operator prose is quarantined.** The evidence packet's
   `household_prose_excluded` invariant stands: household copy never enters
   the evidence document and `household_findings` stays withheld. Operator
   prose is gathered into one separately-kinded operator-notes artifact
   (`jts_crossover_v2_operator_notes`), labeled as operator-typed text to be
   treated as information, not instructions. **Amended by owner ruling
   2026-08-22 (#2871):** the artifact is *embedded* in the packet, under the
   single block `privacy.operator_prose_quarantined_to` names, rather than
   travelling beside it — the tuning LLM is a legitimate reader and had none,
   and prose with no reader is a write-only store. The quarantine is the
   fencing, not the distance: the artifact keeps its own kind and schema
   version so it can be lifted whole, the evidence blocks stay prose-free, and
   no code path in JTS reads the strings for any decision.
9. **Naming: "arm" means the physical rig** (`jasper-angle-capture serve`)
   — never a
   DSP variant. DSP variants under test are **candidates**; a tournament
   round runs a **candidate cycle** at each pose. Forward-only for new work;
   ticket 2.8's subject sweep trued up the DSP-variant prose it covered
   (`alignment_prescription` is now at zero). The rule binds this program's
   vocabulary, not the word: unrelated senses survive deliberately across
   the tree and a sweeper should leave them alone — one ring/coupling
   arming transaction as a noun (`coupling_reconcile`'s "the arm's outputd
   restart" and "an arm or a disarm"; the `test_ring_anchor_arm_acceptance`
   and `test_composite_ring_arm_enabling` modules), a branch or case of a
   conditional (`outputd`'s "both arms", `program_analysis`'s `None` and
   low-SNR arms), and an arm of a comparison study. The offline A/B emit bench
   is the one **covered** exception, not an unrelated sense: #2878 records
   that its "control-vs-treated legs genuinely ARE DSP variants under test —
   so the invariant does cover them", non-violating only because this
   invariant is forward-only for identifiers, and #2878 owns the rename of
   its welded `arm` field, `arm=` log key, and `"derived both arms:"` CLI
   string.

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
| Capture identity | Measurement Program v2 schema (`{regime, angle_deg, repeats, level_re_anchor}`, ratified 2026-08-18) | unchanged; elevation ships later as the ratified `(azimuth, elevation)` tuple, not a plane enum. Candidate-under-measurement and capture provider are **provenance**, not identity — the graph fingerprint is already recorded per capture (#2793) |
| Measurement programs | named, versioned pose lists as data (`baseline`, `tournament`, `verify`, `spot`) | LLM selects a program + bounded params via a staged request (generalizing the `angle_capture` request spool); it never invents free-form geometry. Arm and guided human are interchangeable providers behind `capture_source` |
| Retention | the bank | mic WAV + stimulus are **evidence**, both flows (room's `sweep_meta` regenerates its stimulus deterministically — equivalent). IR and complex TF are **derivable caches**, prunable. Ring eviction never crosses an active round's boundary; budgets stated per store |
| Analysis kernel | `jasper/audio_measurement/` | owns the math and the policies (smoothing widths, arrival window, σ definitions — one owner per policy). Imports neither flow |
| Evidence | `evidence_packet.build_crossover_evidence_packet` | a **computed view** over the bank, schema-versioned; consumers rebuild it, nothing reads a stale packet file. The classifier artifact already carries the full lab rows; the packet's classification block widens to match (ticket 1.1), while the 7-key `FeatureVerdict` stays the gate-facing subset. Every published uncertainty field labels itself **random or systematic** — the two must never pool. Where the evidence cannot separate them, the field says so under `unseparated` and names what would (ticket 1.3's cross-seat σ contains both and needs E2), never being filed as a kind it does not have |
| Mechanism vocabulary | one taxonomy, joined in the per-feature record (ticket 1.10) | `jasper/attribution`'s mechanism ids + fix classes become the registered vocabulary of the per-feature evidence record, joined with the classifier's classifications and the envelope's depth reasons. Detectors beyond the two shipped discriminators (min-phase/gate ladder, position-invariance) are **not built** — mechanism inference over the record is the LLM's job (see the skip ledger) |
| Prescription doors + spool | `driver_prescription` / `blend_prescription` / `alignment_prescription` / `topology_prescription` + `prescription_spool` | all four envelopes versioned; refusal vocabularies converge to the hard-stop list; spool stays single-slot last-wins (logged) |
| Activation / apply | `program_playback.play_program` / `handle_v2_apply` | invariant 5 |
| Verification | `verification.py` + `flat_spec` + `delta_probe` | one grading currency; `fc_selector`'s parallel grader retires with the hunt; predicted-vs-measured is first-class |

## Decision register (final, post-review)

The direction passed an independent adversarial review on 2026-08-21, and the
document itself passed the standing adversarial gate the same day; the
dispositions below incorporate both.

| # | Ruling | Disposition notes |
|---|---|---|
| R1 | Delete the hunt: `crossover_v2/{search,objective,candidate_space}.py`, `fc_sweep`'s sweep half, `fc_selector` + its live recommendation surface. Keep `forward_model` (simulated evaluation). | `XoverCandidate` folds into `forward_model.py` (it imports it at module scope). `recornered_preset` stays — live importers. `resolve_fc_search_band` was carved out here for the same reason and has since been DELETED: the owner ruled on 2026-08-22 (#2870) that the crossover search band it intersected is itself a vestige of this hunt, so the carve-out lapsed with the field. Deletion PRs name the #2811/#2815 write-off explicitly, and 2.4 names the 80-20 campaign's completed R17 deliverable it retires plus the cancelled R21 candidate. |
| R2 | Filter vocabulary: entry-time validation now; **Butterworth support is funded work in this plan** (owner override 2026-08-21), scoped to its true blast radius. | Blast radius (indicative, verified 2026-08-21 — the ticket's first task is the exhaustive literal sweep): `branch_chain.CrossoverSection` gains a family tag and `crossover_response_db` / `crossover_response_complex` become family-aware (the headroom ledger bottoms out there); `camilla_emit.emit_linkwitz_riley` (the emitter itself); `branch_peak._MODELLED_COMBO_TYPES` (closed allowlist on the headroom path); `graph_safety.output_highpass_protected` plus its sibling predicates (`protection_requirement_present`, `tweeter_guard_present`, `sub_guard_present`, `sub_audible_guard_present`, `mains_highpass_present`) that match the `LinkwitzRiley*` literals; `runtime_contract`'s four literal sites and its `SUPPORTED_LR_ORDERS` reads; `profile.SUPPORTED_CROSSOVER_TYPES`/`SUPPORTED_LR_ORDERS`; `staging._normalise_filter_type`/`_slope_to_lr_order`; `crossover_declaration` comparison. |
| R3 | One consolidated free-text notes field (driver notes fold in; guided placeholder bullets). Quantitative fields must compile or gate safety; fields with neither sink are deleted. | Prose is quarantined per invariant 8. The research intake's validated JSON output (safety facts, prefills) is data, not prose — it persists in the draft and rides in the packet's declarations. |
| R4 | LLM operator v1 = cloud agent over SSH. No embedded-advisor UX. Room's embedded tuning-LLM gets no new investment until Loop C re-enters. | The commissioning copy-paste research flow stays as-is. The workbench plan's embedded-agent direction is deferred with it (see Supersessions). |
| R5 | Division of labor per invariants 3–4. | Doctrine deviation (c), `REASON_CORRECTION_NOT_AN_IMPROVEMENT` — a predicted-vs-predicted veto — is named in the Wave 2 burn-down. |
| R6 | Program menu + bounded LLM selection; provider orthogonality. | No identity-schema change needed now (see substrate table). |
| R7 | Tournament mechanics: harden `play_program` (adopting `temporary_bass_activation`'s prove-at-quiet-floor and shielded restore), compose-without-apply, expected-delta banking, candidate cycle per pose, N-candidate comparator. | **Same-corner contract:** candidates in a round hold the declared corner (Fc, family, slope, topology) fixed and may vary linearization EQ, trim, delay, polarity. This preserves the excitation-safety premise (permitted bands are derived from the graph's crossover high-passes) and matches `candidate_bank`'s republish gate (#2816). A future corner-varying candidate type must re-derive the permitted band per candidate. |
| R8 | Boost caps widen: per-filter **12 dB** and composed **12 dB**, with `MAX_SPL_SPEND_BOUND_DB` re-proved at **13 dB** (composed cap + `branch_chain.HEADROOM_MARGIN_DB` = 1.0). | This **overturns the 2026-08-18 ruling recorded in `driver_prescription`** ("a prescription has no such prediction") on its own terms: the pre-registered expected delta supplies exactly that closed-loop prediction. The composed number is policy, owner-ratified here — the fit engine has no composed cap to inherit (`PER_FILTER_BOOST_CAP_DB` is a realization bound; total boost is deliberately unbounded there). Worst-case max-SPL spend per document moves 5 → 13 dB; the realized-peak headroom charge compensates with pre-split attenuation, so the binding protections are unchanged: envelope per-bin depth (zero in measured nulls), realized-peak headroom charging, limiters, excitation safety plan, and the live adoption guard — a boosted intervention fails closed when the **evidence is untrusted** (`ADOPTION_UNPROVEN_BOOST`, scoped to `ADOPTION_ROW_RESTORE_UNTRUSTED` by #2537). Blend's `BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a headroom term; a summed capture cannot attribute a deficit to a driver). |
| R9 | Retention per the substrate table. | The genuinely new work was the banked-solo complex-TF loader feeding `forward_model`, plus cache policy + budgets. Wave 4a removed the re-deconvolution half of it: a lateral pose now banks its own magnitude AND phase (`spatial.pose_curve_record`), so the loader reads the bank instead of re-deriving from WAV + stimulus. `round_views.verify_pose_curve` no longer walks that chain in any form — it reads the round's banked VERIFY curve. |
| R10 | Evidence enrichment (Wave 1) + the classifier/envelope/attribution join in one per-feature record; the classifier stays decoupled from the fit engine. | The packet's own `not_evaluated` rows (angle, `first_reflection_ms`, harmonics) are the spec. The join adopts one mechanism vocabulary (substrate table). |
| R11 | Sequencing = artifact-dependency refusals, not a workflow engine. Runbook + one orientation verb. | The orientation verb reads the **same builders the doors read** — never a second tree-walker. |
| R12 | Legibility pass ("solo engineer" test): dead code out, enumerations honest, one artifact-kind vocabulary, one vocabulary per question. | Kind namespacing applies to new writes with a tolerant reader — no bundle migration. The parallel-vocabulary findings of 2026-08-21 land as tickets 2.9–2.13. |
| R13 | Room correction pinned; the substrate stays flow-agnostic. | Nothing in `jasper/correction/` structurally resists this beyond vocabulary. |
| R14 | **Toolbox shape ratified (owner, 2026-08-31).** Capture is two front doors — turntable measure (available only when the rig is attached, disclosed at runtime) and manual measure (guided walk; web flow is the V2 half of 6.12) — onto ONE capture engine and record model that does not care who moved the mic. Analysis stays independent verbs: the always-relevant facts ride the banked round (lab rows, packet), and depth is pulled verb-per-question — never an aggregate "analyze everything" surface. | The record model already distinguishes candidate takes by applied-graph fingerprint, so measuring multiple candidates per pose is representable today; the candidate-cycle orchestration is R7's existing line, sequenced BEHIND the forward model's acceptance runs (predict first, physically measure the winner). The methodology's contract carries the read-pattern ("depth is pulled, never dumped"); ADR-0204 owns the tiered surface this ratifies. |

## Measurement program constants (owner research, 2026-08-21)

From the owner's sourced research memo (Toole/CTA-2034, Keele 1974,
Struck & Temme 1994, D'Appolito, Bücklein 1981, Toole & Olive 1988, REW and
VituixCAD practice), reconciled against JTS's own declared ceilings and
ratified rulings. Constants land in code as the named program definitions;
⚠ items have in-house settling experiments (below).

- **`baseline` program:** 13 poses at 1 m — horizontal 0°, ±10°, ±20°, ±30°,
  ±40°; vertical 0°, ±10°, ±20° — plus one nearfield capture per woofer (and
  port, when vented). Repeats follow the ratified position-major structure:
  ×4 at the 0° anchor pose, ×1 at every other pose (the research count
  applied to Program v2's ratified center-plus-pairs shape), ×8–16 for the
  nearfield captures. The vertical poses are live, and
  `jasper-angle-capture stage --program baseline` stages them.
- **Drive level is anchor-relative, never an absolute program constant.**
  The baseline level is the ratified measurement anchor — 75–80 dB SPL at
  the microphone (Program v2, ratified 2026-08-18), seeded from the
  seat-level reference (`DEFAULT_TARGET_DB_SPL` = 77.5 dB SPL, owner ruling
  2026-08-19) — expressed as `level_re_anchor = 0`. The research memo's
  86 dB industry convention is noted and **rejected as an absolute**: it
  exceeds the declared `max_commissioning_level_db_spl` ceiling (85 on
  every shipped preset since the 2026-08-23 owner ruling, which is also the
  schema cap), which sits on the doctrine's closed hard-stop list. Absolute
  SPL anchoring uses the mic's parsed sensitivity
  (`calibration.parse_calibration_sensitivity`).
- **Escalation** (the second level, on anomaly): anchor + 10 dB where the
  preset's declared ceiling permits, otherwise the largest separation the
  ceiling allows, with the shrunken separation disclosed in the evidence.
  Both **shipped** presets declare 85 dB SPL, which is also the schema cap, so
  for them raising the declared ceiling is spent as a lever: a separation the
  ceiling cannot fit is disclosed shrunken rather than bought with a higher
  stop. A household preset declaring less than 85 keeps the lever, within the
  same 45–85 schema. `delta_probe`'s commanded-amount regression remains the
  primary compression detector and needs no second level, which is why
  escalation is on-anomaly rather than default.
- **Distance rule:** measure at the largest distance where the gate still
  clears the driver-summing farfield (≈3× the largest baffle dimension);
  default 1 m, fall back to 0.5 m only when the room forces it, flagging the
  summing-nearfield phase caveat. Conditionals: C-t-C ≥ 1λ at the crossover →
  tighten vertical steps to 5°; gate < 3 ms at 1 m → move to 0.5 m; midband
  repeat σ > 0.5 dB → warn (fix mic mounting) before trusting any delta —
  a disclosed warning, never a refusal.
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
  `jasper-delay-sweep propose` is the operator door onto the computed
  optimum. A delay is found by compute-then-confirm
  (`crossover_v2.delay_landscape`), which proposes the coordinate from
  banked complex transfers and confirms it with three staged takes; the
  flip and the swept delay both ride the protected-neutral MEASURE program
  via `measure_spec.inverted_roles_for` and `measurement_delays_us`.
- **Boost probe:** +6 dB is the standard probe (+3 dB when headroom is
  tight). A dip is correctable when measured gain-back ≥ ~0.8× commanded AND
  excess group delay is flat there; non-correctable when ≤ ~0.5× or excess-GD
  is non-flat. Policy: cut peaks freely where excess-GD is flat; probe every
  candidate dip before filling; never fill a failing dip (peaks are far more
  audible than dips — Bücklein).
- **Level dependence:** single-level default; escalate per the escalation
  bullet only on anomaly — `delta_probe`'s `level_dependent_shortfall`
  verdict is the trigger. The in-program two-level pilot pair
  (`program._append_leading_pilot_pair`) already exists as the low-cost
  in-capture level probe. Thermal compression needs sustained stimulus
  (tens of seconds) and stays out of v1.
- **Stopping rule:** stop when the aggregate error metric (NBD or
  target-weighted RMS over the trusted band) stops improving by more than
  ~2× its own aggregate sampling standard error — computed over the
  **random** uncertainty terms only — for **2 consecutive rounds**, AND the
  residual sits within ±1–1.5 dB at 1/3–1/6-octave smoothing. No per-bin
  significance chasing (multiple-comparison inflation).
- **Nearfield splice v1 (Wave 4.2):** mic within 0.055×D of the dust cap;
  valid up to ≈4311/D_inches, bounded by the enclosure limit c/(3·M)
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

This plan is the planning authority for the measurement/tuning program.
Ticket 2.14 applied the banners/tags below on 2026-08-22; this section stays
the ruling those banners point back to.

**Superseded (historical-tag or banner + pointer here):**

- `docs/historical/audio-commissioning-roadmap.md` (owner-ratified 2026-08-14) — the
  previous program-wide roadmap for the identical scope. Replaced whole. Its
  Ethos section was the one carve-out — "least bad is still the overall
  guiding principle", owner re-affirmed 2026-08-22 — and #2865 moved all five
  of its rulings on 2026-08-23 into the guiding-principle section of
  [measurement-loop-doctrine.md](measurement-loop-doctrine.md#3-the-guiding-principle--least-bad-measured-honed-in-bites),
  not into this plan: the doctrine is where the loop's rules live (see the
  orientation note above), and the three production sites that cite the
  ruling now cite it there.
- The cancelled CURRENT-POSITION `next_mission` — the **R21
  "apply-vertical" candidate (a path that applies
  a machine-RECOMMENDED Fc) is cancelled** by invariant 2. This plan owns
  campaign ordering ("what supersedes what"). The `R21` token elsewhere in
  the tree (accept-receipt provenance in
  `crossover_v2/contracts.py` and the crossover-measurement-v2 ledger)
  names shipped vocabulary, not established as the same round and
  unaffected by this cancellation. The 80-20 plan's landed R14–R20 record
  remains historical truth and is **not** superseded (it stands in the
  compatible list below).
- `docs/historical/llm-native-tuning-workbench-plan.md` — its planning-authority claim
  and its §5.5 "experiment workspace is the one new mutation owner" are
  superseded: `play_program` is the single activation door (invariant 5),
  and the SSH-operator + runbook model (R4/R11) replaces the workbench
  direction. Its deferred embedded-agent appendix stays deferred.
- `docs/historical/attribution-stage-plan.md` — open work orders are absorbed, not
  pursued independently: P1 (reverse-null probe) → the `verify` program;
  P6/M6 (harmonic extraction) → ticket 1.4; P2 (position variance) →
  tickets 1.2/1.3; WO-7's serial dial-in loop → Loop B's tournament
  mechanics; WO-8 (room line) → Wave 5; the mechanism registry + fix-class
  vocabulary → the per-feature record join (ticket 1.10). The attribution
  package's shipped findings/promotion code is substrate for that join, not
  a parallel system.

**Archived, not standing.** The five-file linearization family — the
`flat-linearization-*` trio, the 80/20 revision and the integrity ladder —
was listed here as “standing, compatible (no action)”. That framing was wrong
by the time it was written: the trio was foundational and fully absorbed into
Loop B, the 80/20 plan's R14–R20 record was already historical truth, and the
integrity ladder's five items had all landed. Nothing was standing to be
compatible with. All five collapsed into
[`historical/linearization-campaign-2026-07.md`](historical/linearization-campaign-2026-07.md)
in wave 7e; ticket 2.4 still names the R17 deliverable it retires.

**Standing, compatible (no action):** `gating-v2-plan.md`
(feeds the kernel), `HANDOFF-bass-extension-plan.md` (parked; converges at
4.4),
`room-correction-regime-plan.md` (parked under the Loop C pin), and the
information-design docs. `tuning-operator-runbook.md` stays the
operational canon. Three landed records moved to
[`historical/`](historical/): the productization design, the two-stage
commission flow plan, and
[`crossover-v2-engine-design.md`](historical/crossover-v2-engine-design.md),
which carries the file map. The
Wave-2 deletion PRs landed: `fc_selector.py` is gone and `fc_sweep`'s sweep half
with it, so the file map carries no rows for them to amend.

## Work breakdown

Sizes: S ≤ ~1 day, M ≤ ~3 days, L = a week-class effort. Wave 1 tickets are
independent of each other and of Wave 2; Waves 2 and 3 can run as parallel
tracks after Wave 1 starts. Every ticket ships its tests ("pin promises with
tests") and its doc-map scan.

**Wave 0 — in flight elsewhere (hands off):** #2804 (N captures per pose +
bank-derived pose index), #2807 (walk session-end fix), #2816 (candidate
republish seam), #2822 (dead islands F3+F4), #2823 (rig ring/marker fix).
Everything below assumes they land as-is.

**Wave 1 — evidence & honesty (all S unless noted).** Enrichment rule for
1.1–1.5: every published uncertainty field labels itself random or
systematic; the two never pool. Where a measurement can only yield a pooled
figure it is published as `unseparated` with the separation named as pending —
1.3's cross-seat σ is the shipped case — never as one of the two kinds.
1.1 The evidence packet's classification block widens to carry the
    classifier artifact's full lab rows (the artifact already has all ~26
    columns; `evidence_packet._classification_block` currently publishes
    only the 7-key `FeatureVerdict` view, which stays the gate-facing
    subset).
1.2 Packet reads the `positions/` sidecars and dump-ring SNR — signed angles
    and per-capture SNR appear; the corresponding `not_evaluated` rows close.
1.3 Per-bin σ across seats from the member curves already in the packet.
1.4 H2/H3 harmonic rows derived from banked captures (`deconv` harmonic
    machinery; the packet's own `not_evaluated` row says they are computable).
1.5 `gate_moved_rms_db`, `first_reflection_ms`, and the reflector path
    distance (τ × c from the interference-null ladder) become numeric fields
    (today the first is prose inside `gate_disclosure`, the others
    flagged-absent or buried).
1.6 Notes consolidation + quarantined operator-notes artifact (invariant 8);
    driver notes fold in; guided placeholder bullets. The `horn_coverage_deg`
    fork this ticket carried is settled: #2872 deleted the field, so a
    waveguide's rated coverage rides the driver notes 1.6 already folds in.
1.7 Entry-time crossover-vocabulary validation: the wizard offers exactly
    what compiles and refuses the rest at entry (kills the silent
    `crossover_preview_filter_unsupported` late block).
1.8 Orientation verb — `jasper-crossover-prescriber status`: prints declared
    / banked / staged / applied state and possible next actions, reading the
    same builders the doors read; hostname-derived URLs at every human
    handoff point.
1.9 `docs/tuning-operator-runbook.md`: the tool menu (as inventoried), the
    happy path, division-of-labor rules, the untrusted-notes rule, the
    program menu, the mechanism-signature table (the LLM's Block-D
    interpretation guide), and the σ-interpretation caveat (σ_position is
    only as meaningful as E2's measured σ_repeat). A map, not a script.
1.10 The per-feature record join (M): classifier verdicts + envelope depth
    reasons + attribution mechanism vocabulary in one record per feature,
    published through the packet. One mechanism taxonomy (substrate table);
    the fit engine stays decoupled. Binding on the record shape: intervention
    identity stays distinct from candidate identity, so selection can reach a
    measured-good intervention inside a losing candidate (owner refinement
    2026-08-22, #2862; the principle itself lives in
    [the doctrine](measurement-loop-doctrine.md#3-the-guiding-principle--least-bad-measured-honed-in-bites)).

**Wave 2 — executor ratification (deletion-led).** Standing hygiene for
every deletion PR in this wave: run `bash scripts/tense-grep.sh --all`
against a pre-cut baseline plus a subject sweep for the deleted thing's own
vocabulary; update `docs/doc-map.toml` routing; update
`crossover_v2/__init__`'s module docstring; amend
`historical/crossover-v2-engine-design.md`'s file-map rows.
2.1 Layers-doc P2 amendment + pointer to this plan (lands with/after 2.2–2.4
    so prose states what is); also trues up the Measurement Program v2
    section's ×3 anchor-repeat count to the adopted ×4.
2.2 Delete `search.py`, `objective.py`, `candidate_space.py`; fold
    `XoverCandidate` into `forward_model.py`; PR body records the
    #2811/#2815 write-off as a decision. (M)
2.3 Delete `fc_sweep`'s sweep half; retire `STAGE1_INCLUDES_LATERAL` and
    `journey.lateral_adjudicates`; cancels the planned #2717 flag flip;
    closes doctrine deviation (b) `FC_REJECT_BEAMING` for free. (M)
2.4 Retire `fc_selector` and the web `SELECTION_*` surface. The
    declared-corner path is already the shipped path on every session;
    `fc_selection` becomes a versioned-absent field and readers of
    historical rounds that carry it degrade gracefully. PR body names what
    it retires: the 80-20 campaign's completed R17 deliverable and the
    cancelled R21 candidate. (M)
2.5 Version + kind the `alignment_prescription` / `topology_prescription`
    envelopes and `crossover_declaration`; while in those files, prefix the
    colliding constant names (the two unprefixed
    `PRESCRIPTION_REFUSAL_REASONS`/`PRESCRIPTION_MALFORMED`/
    `PRESCRIPTION_PROVENANCE_MISSING` sets, and the two
    `SPOOL_REFUSAL_REASONS` sets in `angle_capture_spool` /
    `prescription_spool`). (S)
2.6 Dead-field removal: `DriverSpec.{fs_hz, rated_power_w,
    expected_nearfield_response}` — schema-complete, populated in both
    shipped presets and two test fixtures, **never read** by any consumer.
    Removal includes the preset JSONs and fixtures; the Epique preset's
    compression-driver bring-up note ("must be measured with the final
    horn…") is relocated to the driver-safety notes by decision, not
    dropped. (S)
2.7 Nanny burn-down, rescoped: **DONE.** Deviations (a)-(d) and (f)-(i) all
    closed; (e) is **retained** by ruling R8. The doctrine no longer carries a
    deviation table — its §4 clamp list is positively complete and carries a
    letter→slug map for old citations. (S each)
2.8 Legibility: `audio_measurement/__init__` and `crossover_v2/__init__`
    enumerate all their modules (the latter omits 12 today); artifact-kind
    namespacing for new writes + tolerant reader; the invariant-9 "arm"
    subject sweep over existing prose. (S)
2.9 Rename the stimulus-vs-journey phase collision: `program.py`'s
    `PHASE_CHECK/MEASURE/VERIFY` and `journey.py`'s carry identical names
    AND identical string values for different concepts; one family gains a
    distinguishing prefix. (S)
2.10 One severity vocabulary for capture usability: the thresholds are
    already unified in `quality_model`; unify the *words* across the six
    verdict sets (`quality.Severity`, `runtime_integrity.Severity`,
    `browser_audio`'s two scales, `acoustic_quality`'s summary,
    `snr_policy`'s rank); classifier confidence `med` → `medium`. The
    `correction/envelope.py` `surface_quality_gated`-rename clause this item
    used to carry is moot: ruling S8 deleted `gate_on_acoustic_quality`
    outright rather than renaming its copy key — a warned verify-capture SNR
    is now a plain `verify_quality_warned` nudge, and no verdict-downgrade
    copy exists to rename. (S–M)
2.11 `setup_status.py`'s private statefile reader (duplicate env-var name,
    literal path, and regex parse) folds onto
    `active_speaker/environment.py`. Its scope was that one duplicate;
    the premise that the other readers of `JASPER_CAMILLA_STATEFILE` kept
    documented reasons did not hold. #2848 folds all four: the three
    parser duplicates (`cli/doctor/correction.py`,
    `audio_runtime_plan.py`, `multiroom/leader_config.py`) and
    `fanin/converge.py`'s override lookup, which resolved the statefile
    PATH by hand though it never parsed `config_path`. Converge keeps
    its written reason for passing `--statefile` at all. (S)
2.12 Rename the two `vocabulary.py` modules (attribution's taxonomy vs
    crossover_v2's household refusal copy) so the names say which is
    which. (S)
2.13 Remove `web_measurement._stored_ambient_report`'s write-only
    `pending_signal_boundary`/`controlled_pre_sweep` fields or wire the
    analyzer they awaited. (S)
2.14 Docs true-up (the Supersessions section): historical tags/banners on
    `audio-commissioning-roadmap.md`, `llm-native-tuning-workbench-plan.md`,
    and `attribution-stage-plan.md`; the next_mission cancellation and
    ordering-authority transfer note; doctrine deviation-table rows
    (a)/(b); a one-line unapplied-phase note in `calibration.py`. (S–M)

**Wave 3 — the tournament (M–L overall):**
3.1 Harden `play_program`: prove-at-quiet-floor before mutation, read-back
    proof of the loaded graph, and a shielded restore on cancel (all three
    adopted from `temporary_bass_activation`). It is the single activation
    door. (S)
3.2 Compose-candidate-config-from-staged-prescription without applying —
    reusing `measured_crossover_candidate.compile_candidate_config` /
    `prove_candidate_config`; provable off-Pi. (M)
3.3 Expected-delta entry point: staged prescription → banked predicted
    per-frequency delta curve (building blocks:
    `commanded.graph_predicted_sum`/`commanded_delta` +
    `linearization_fit.complex_correction_response`). (M)
3.4 Round-runner candidate cycle: `--candidates <fingerprints>` from the
    candidate bank; same-corner contract enforced at stage time; per-capture
    candidate identity via the provenance graph fingerprint (#2793) +
    `position_cycle` (#2804). (M)
3.5 N-candidate comparator: a new small comparator over N independent
    `delta_probe` gradings (not a widening of `classify_delta_probe`'s
    realized-vs-commanded signature), surfaced in the packet. (M)
3.6 Boost-cap widening PR per R8: per-filter and composed caps to 12 dB,
    `MAX_SPL_SPEND_BOUND_DB` re-proved at 13 dB with its span clause, the
    named ruling overturn in the PR body. (S)
3.7 Program menu as data (`baseline`/`tournament`/`verify`/`spot`), guided
    human flow speaking pose identities, LLM program request via the
    generalized `angle_capture` staging document. Pose naming ruling lands
    here: `pose_id` is the canonical per-pose key; the
    `position_id`/`position_index` split unifies crossover-side (the flow
    site publishing a `pose_id` under the `position_id` key is fixed), and
    room-side at Wave 5. Program levels resolve absolute SPL through the
    seat-level reference + mic sensitivity. Elevation capture — the
    `(azimuth, elevation)` tuple and the string-and-protractor prompts — is
    the follow-on that unlocks the vertical poses and the tilt check. (L)
    **Superseded for execution** by the conductor's six-issue decomposition
    (recorded on epic #2826, 2026-08-22). Each issue's body and its binding
    "Conductor amendments" section are the current scope; the rows below say
    only which issue covers what. Completion rule, in #2885's words: "Along
    with the five siblings, this ticket is what ticks 3.7."
    #2879 — "Human release source for crossover rounds: unweld (degrees,
    tap), gate hand-walked wired sessions, add the wired retake verb";
    amended to "lands FIRST of the five".
    #2880 — "Pose programs as data with an elevation axis: (azimuth,
    elevation) pose identity, per-mover reach, plan constants become code
    (3.7)"; amended to depend on #2879, and to pull 3.7's stated elevation
    follow-on into core — "a deliberate widening, not a reading of the plan
    as written".
    #2881 — "Guided round mode on /correction/crossover/: render the minted
    release action, per-take verdict and retake; promote the walk shell to
    shared/".
    #2882 — "The box becomes the whole instrument: local bank, on-box
    campaigns, live-bundle views, open/verify/apply CLI, installed runbook".
    #2883 — "Tuning handoff card: box-generated pointer prompt after
    commissioning (driver-research pattern)".
    #2885 — "3.7 remainder: pose_id canonical key (+ the position_id publish
    bug), the LLM program-request door, and absolute-SPL level resolution";
    the clauses the first five left unowned.
3.8 Retention: complex-TF loader over banked solos feeding `forward_model`;
    cache policy + budgets; eviction never crosses an active round.
    Cross-capture time anchoring is part of this loader's spec — the wired
    path has per-chunk monotonic-clock anchoring, the browser relay has
    counters only, and `timeline_slip` is a within-capture reject rule, not
    a cross-capture anchor. (M)

**Wave 4 — funded features & coherence:**
4.1 Butterworth support, scoped per R2's blast radius, one contained PR
    series with tests. (M–L)
4.2 Nearfield splice v1 per the constants above; adds the baffle-width
    field. (M)
4.3 Policy owners in the kernel: smoothing widths, arrival window, σ
    definitions. (M)
4.4 Bass bench migrates onto the hardened `play_program` (dedup). Bass
    otherwise stays parked — and is recorded as such: the entire apply
    pathway (`apply_bass_extension`/`bypass_bass_extension`/
    `recover_pending_bass_extension_apply`) has zero production callers by
    design (issues #2202/#1738), and `bench/excitation.py` is a
    zero-importer module to wire or delete when bass resumes. Its three
    pass/fail spellings unify then too. (S)
4.5 Simulated evaluation wired as a verify field: `forward_model` over the
    banked-solo loader — predicted-vs-measured sum for declared-design
    verification. (M)

**Wave 5 — room correction re-entry** (pinned until Waves 1–3 are done):
prescribe path converges onto the doors; program model extends to the
listening-position regime; `position_index` unifies with the pose ruling;
moving-mic decision made then.

**Wave 6 — toolbox hardening after the flat campaign (adopted 2026-08-31;
sequenced now, not behind Wave 5's pin).** Evidence:
[`historical/flat-campaign-2026-08-31.md`](historical/flat-campaign-2026-08-31.md)
and the owner-commissioned independent methodology review of it, reconciled
against the tree on 2026-08-31 — roughly half the review's "missing" list
turned out to be shipped, which is why these tickets are this small.
Standing rule for the wave, owner-restated 2026-08-31: **tools compute
facts; the LLM judges** (invariants 3–4) — no ticket ships a score, a
recommendation, or a veto; new discriminating evidence lands as fields.
Elevation stays sequenced behind this wave by the same ruling: the
campaign's delay findings raise the vertical tilt check's value, and that
work is already planned (#2880's `(azimuth, elevation)` axis, the `verify`
program's tilt check, experiment E1) — deliberately not promoted into this
wave. Impedance measurement is dead, not parked:
[ADR-0200](adr/0200-the-measurement-toolbox-is-microphone-only.md).
Owner ruling, same day
([ADR-0203](adr/0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md)):
the incumbent tune retires and the program's next campaign recommissions
structure-first — the measured delay committed before any response fit.
This wave's instruments are what that campaign runs with; 6.12/6.14 verify
the committed structure rather than adjudicate it. The wave's acceptance
run: the banked flat-campaign rounds re-read through 6.1–6.3 (+6.10–6.11),
validating the new fields on known data and mapping the incumbent's
features to mechanisms, so the recommissioning campaign opens with its
priors measured.
6.1 Gate-ladder exposure: `jasper-classify-features` grows a gates-list
    flag wired to `classify_round(gates_ms=...)`, and the artifact's
    per-rung rows (center/depth per gate, per feature) are verified
    readable — rungs above `SEARCH_T_MAX_MS` deliberately re-admit
    reflections; convergence vs fan-out across the ladder is the read.
    The shipped 3/5/7 default and the gate-invariance verdict semantics
    are untouched. (S)
6.2 Off-axis persistence as a field: per detected feature, the classifier
    artifact gains per-pose depth/center rows read from the round's banked
    lateral pose curves (`spatial.pose_curve_record`'s Wave-4a
    magnitude+phase bank — never raw lateral WAVs;
    `LATERAL_CAPTURE_SHAPE`'s refusal to pool laterals into detection
    stands). A discriminating field under the skip-ledger's rule, not a
    mechanism detector. (M)
6.3 Decay as a field: per-feature narrow-band decay from already-banked
    IRs — band-limited analytic envelope, time-to-−20 dB at the feature
    center and at flanking bands — promoting the campaign's out-of-repo
    898 Hz ridge method into the classifier's field set, with known-answer
    controls in the shipped control-suite style. (M)
6.4 Toolbox legibility, redesigned on research 05
    ([ADR-0204](adr/0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md)):
    the operator surface becomes three loading tiers. Tier 0 —
    `jasper-crossover-prescriber status` (the runbook's own "Orient"
    step) prints the reading order and the menu pointer, the cold-start
    entry an SSH-only agent lands on. Tier 2 — the three operator docs
    (methodology, runbook, doctrine) install onto the box (the #2882
    direction, taken for all three) and load on demand, never pasted
    into resident context. Tier 3 — each tuning CLI's `--help` is the
    tool's contract of record: purpose, when NOT to use, parameters, one
    worked example, exit codes, and error messages that name the
    corrective action (the field's best-measured lever — SWE-agent ACI,
    research 05). The runbook's per-tool menu section becomes a
    generated index derived from the CLIs' own metadata, pinned by a
    hardware-free regeneration test (the counted-in-one-place pattern,
    ADR-0181); the hand-maintained table and its drift surface are
    deleted. No per-tool `.md` documents; no Skill packaging for the
    tuning flow. (M)
6.5 Accuracy beside precision: one packet summary block juxtaposing the
    round's measured repeat floor (random) against the standing systematic
    bounds (mic-cal tier, position sensitivity, gate leakage), building on
    the Wave-1 random/systematic labeling rule — so a 0.04 dB repeat floor
    can never again read as accuracy. (S)
6.6 Trim-drift visibility: the packet carries the re-solved trim pair's
    recent per-round history from banked receipts, so a monotonic walk
    (the campaign's −1.4 → −2.1 → −3.1 runaway signature) is readable
    evidence. No verdict — the pin fields are the shipped remedy. (S)
6.7 `forward_model` lands now: 3.8 + 4.5 promoted into this wave, scope
    unchanged, plus one acceptance case each from the campaign bank —
    postdict the r8 regression (measured delay under held EQ → the ~−3 dB
    crossover-region dip) and track the C5→final measured delta within
    the model's stated tolerance, before any prediction triages a
    candidate. (M)
6.8 Prescription-door bounds audit: run the doctrine's nanny test over
    every refusal bound in the four door envelopes (blend's count/Q/depth
    caps, driver-envelope terms beyond the hard-stop-derived ones,
    alignment/topology bounds). 2.7 burned down the doctrine's deviation
    table; this sweep covers the door bounds specifically, post-campaign.
    Output is a proposal table — each bound names its damage/hearing
    mechanism (stays), re-tags as integrity under the ADR-0002 test
    (stays), or is proposed for demotion to disclosure; demotions land
    only after owner ruling, as their own PRs. (M)
    **Ruled and landed 2026-08-31 (ADR-0207):** owner ruled the 8-item
    menu — cut depth/composed/Q bounds and the min-Q floor retired
    outright in both doors, blend rationale demoted to
    truncate-and-disclose, boost caps and region bar stay; a fully
    demoted bound's machinery is deleted, never left as a vestige.
6.9 Research intake: four owner research assignments are out as of
    2026-08-31 (correction granularity/audibility; reference axis and
    position artifacts; gating/windowing; structure/alignment and
    automation prior art); results land under `docs/research/` (the
    2026-07-24 bass-extension pattern); each re-adjudication they force
    is its own ADR — named candidates: the FDW not-built entry, the flat
    ±dB `SPEC_BANDS` tolerances (audibility/Q weighting), the 0°
    reference axis for the horn class. Until a result lands, the standing
    ledger entries hold. (S) **Landed same day:** files 00–04 under
    `docs/research/2026-08-31-tuning-methodology-deep-research/`;
    ADR-0201/0202/0203 carry the forced re-adjudications; the
    reference-axis question resolved into 6.13's pooled-window co-metric
    rather than a moved microphone.
6.10 FDW rungs as diagnostic evidence: 5- and 15-cycle
    frequency-dependent-window variants computed offline from banked IRs,
    published as per-feature facts beside the fixed-gate ladder's rungs
    (6.1's pattern). Correction targets and grades stay fixed-gate
    ([ADR-0201](adr/0201-fdw-stays-out-of-the-correction-path-funded-as-diagnostic-evidence.md));
    the diagnostic reading rule (a dip that fills under FDW but holds
    under the fixed gate is reflection-caused) is guide content, not
    code. (S)
6.11 Derived structure/honesty fields: (a) phase-overlay Δφ(f) between
    the two branches' banked complex curves across the crossover octave,
    with the implied summation dB (`20·log10(2·cos(Δφ/2))` — the 60°
    corridor is convention, the 120° additive boundary is McCarthy);
    (b) per-feature cycles-in-gate; (c) an EGD-window receipt — prove the
    excess-group-delay read takes the longest clean window rather than
    the reflection gate (research 03's pitfall), and fix it if not. (S)
6.12 Sideways-cabinet vertical family: the cabinet on its side turns the
    turntable into a vertical-plane instrument; a standard walk reads
    coarse lobe tilt for the committed alignment. Captures label the
    orientation honestly (provenance, not a new identity axis — #2880's
    elevation work stays where it is). Verification for the
    recommissioning campaign per ADR-0203. (S, protocol + provenance
    label) **Deferred to V2 by owner ruling 2026-08-31** alongside the
    guided web flow's elevation prompts (#2880/#2881 family): the
    recommissioning campaign runs with the vertical axis
    disclosed-unsampled per the methodology's §3d rule, and the driving
    LLM's job is to surface the options (sideways protocol now, guided
    elevation later) to the owner, never to silently skip the axis.
6.13 Audibility-weighted co-metrics: NBD and SM (Olive 2004, 1/20-octave
    smoothing via the shared smoother) on the banked on-axis curve AND
    the pooled horizontal window (0/±7/±22° average, named
    `pooled_window_horizontal` — deliberately not "listening window":
    CTA-2034's includes vertical poses this rig does not capture).
    Co-reported beside the band grade; `SPEC_BANDS` stays the acceptance
    metric
    ([ADR-0202](adr/0202-audibility-weighted-co-metrics-beside-the-band-grade.md)). (M)
6.14 Refit expected delta (gated on 6.7): `forward_model` renders the
    structure-first refit's predicted endpoint — per-driver linearization
    held (delay-independent), summed-region corrections re-derived
    against the delayed sum — so the recommissioning campaign opens with
    a pre-registered expectation per invariant 3. A predictor, not a
    decision gate (ADR-0203 already decided). (M)
6.15 The operator guide (conductor-authored — owner request 2026-08-31):
    `tuning-methodology.md` becomes THE method SSOT for the driving LLM —
    sequence (structure → crossover → response), traps, the adjudicated
    reading thresholds with their evidence labels, topology lanes,
    tool-selection pointers. The runbook narrows to pure tool reference
    (menu, contracts, exit codes, debugging); its methodology prose moves
    into the guide, never duplicated. The doctrine is untouched as the
    authority. One front door: the guide states the reading order. (M)
6.16 σ_repeat gets a durable home, now that E2 has run (2026-08-31, jts3,
    in-band 0.03–0.17 dB at a fixed pose): **DONE.**
    `jasper.active_speaker.repeat_floor` is the single-writer record;
    `jasper-round-views repeat-floor` writes it from N touched-nothing
    repeat rounds through the existing `repeatability_spread` view, and
    6.5's `in_capture_repeat_floor` reports it — with `thresholds.source`
    saying whether the plateau/margin are that derivation or the two
    codified assumptions. Still honestly unavailable on a rig that banked
    no repeats. (S)

## The operator session, end to end

SSH in (cloud agent, laptop, or on-Pi) → `jasper-crossover-prescriber
status` → read the evidence packet + operator notes (quarantined) → run or
re-run deterministic views (`jasper-classify-features`, `jasper-round-views`)
→ propose up to ~3 candidate prescriptions; each passes a door, compiles,
banks its expected delta → hand the human the tournament program URL
(hostname-derived); they move the mic pose-to-pose while candidates cycle →
read the comparative grading → compose the final prescription → the apply
door → hand the human the verify program → check the stopping rule → done, or
iterate. Every step is a refusal-guarded artifact dependency; there is no
workflow engine to fight.

## Calibration experiments (optional, lab-rig)

- **E2 — σ(f) table (do first, cheap):** 16 back-to-back gated sweeps, fixed
  mic; per-bin σ across the band; sets the trusted-band and stopping
  thresholds from measured reality. Two additional customers: the
  round-evidence constants `MEASURED_BENEFIT_MARGIN_DB` (0.5) and
  `ITERATION_PLATEAU_DB` (0.25), both self-described assumptions awaiting
  exactly this study, reconcile against E2's numbers. **First run: 2026-08-31,
  jts3 — in-band σ_repeat 0.03–0.17 dB at a fixed pose.** Its durable home is
  the banked repeat floor. `jasper-round-views repeat-floor <repeat rounds>
  --out PATH` writes the record; place that file on the speaker at
  `/var/lib/jasper/active_speaker_repeat_floor.json`, from which
  `bank-crossover-round.sh` pulls it beside every later round as
  `repeat-floor.json`. The packet's `in_capture_repeat_floor` reads it and
  derives the stopping plateau and benefit margin from it.
- **E1 — lobe-tilt resolution:** inject +0.1/+0.2/+0.5 ms delay errors on a
  known-good alignment; vertical polar at 5° steps; adopt the coarsest step
  that shows a monotonic tilt at 0.2 ms.
- **E3 — splice error budget:** same speaker via nearfield splice vs
  ground-plane outdoors; RMS difference over 100–500 Hz is the real budget.

## Provenance

Inputs: the 2026-08-21 architecture session (boundary map and four recon
reports over `af9db97d`); an independent adversarial review of the direction
(2026-08-21); the owner's sourced measurement-program research memo
(2026-08-21), reconciled against the declared ceilings per the gate's
blocker; the first-principles measurement reference cross-checked block by
block against the tree; the parallel-vocabulary, straggler, and
plan-document-reconciliation sweeps (2026-08-21); owner rulings recorded
per-decision. The 2026-08-21 adversarial gate review of this document
(1 blocker, 7 should-fixes) is fully folded into this revision; the delta
re-review verdict is recorded in the session/PR disposition.

Wave 6 inputs (2026-08-31): the frozen flat-campaign record; an
owner-commissioned independent methodology review of it; three
tree-reconciliation recon reports over `0bcdd95d2`; owner rulings in chat —
facts-not-judgments restated, impedance ruled out (ADR-0200), elevation
kept sequenced behind the wave.

Last verified: 2026-08-21 (Wave 6 additions verified 2026-08-31)
