# Tuning master plan — declared-design executor, linearization tournaments, LLM operator

Ratified 2026-08-21 (owner + architecture session). This is the decision
record and work plan for the measurement/tuning program: the crossover is a
**declared design that JTS executes**, driver linearization is a **measured
tournament** driven by an LLM operator over SSH, and room correction stays
**pinned** on a shared substrate. It supersedes the *search* framing of stage
P2 in
[active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md);
the amendment ships in Wave 2 together with the deletions that make it true,
so prose never leads code. The operator-facing companion,
`docs/llm-operator-runbook.md`, is Wave 1 work.

Authority model and method are **not restated here**: the loop's rules live in
[measurement-loop-doctrine.md](measurement-loop-doctrine.md) (propose/dispose,
the closed hard-stop list, the nanny test) and the execution method lives in
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
   chosen by a measured search. Exploration of corner variations happens as
   offline *simulated evaluation* over banked solo captures, at zero capture
   cost.
3. **Predictions propose, measurements dispose.** Arithmetic predictions
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
5. **One durable apply door; measurement-time activation is ephemeral.**
   `handle_v2_apply` remains the only path that durably applies a measured
   crossover. Activating a candidate for measurement goes through
   `program_playback.play_program` — in-window, proved by read-back,
   self-restoring on every exit path.
6. **Safety is the closed hard-stop list; everything else informs.** Refusals
   exist only for named component-damage mechanisms (the doctrine's list).
   All other checks are informational flags. The five known deviations are
   scheduled for burn-down (Wave 2).
7. **Handoff URLs derive from the speaker's hostname.** Tools that hand the
   human to the web UI print URLs built from `JASPER_HOSTNAME` /
   `Config.hostname` (speakers are `jts1.local`, `jts3.local`, … — never a
   hard-coded `jts.local`).
8. **Operator prose is quarantined.** The evidence packet's
   `household_prose_excluded` invariant stands: nothing a human typed appears
   in the evidence document. The one consolidated notes field travels as a
   separate operator-notes artifact, labeled as operator-typed text to be
   treated as information, not instructions.
9. **Naming: "arm" means the physical rig** (`jasper-arm-walk`), nothing
   else. DSP variants under test are **candidates**; a tournament round runs
   a **candidate cycle** at each pose.

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
| Evidence | `evidence_packet.build_crossover_evidence_packet` | a **computed view** over the bank, schema-versioned; consumers rebuild it, nothing reads a stale packet file. The classifier artifact carries the full lab rows; the 7-key `FeatureVerdict` stays the gate-facing subset |
| Prescription doors + spool | `driver_prescription` / `blend_prescription` / `alignment_prescription` / `topology_prescription` + `prescription_spool` | all four envelopes versioned; refusal vocabularies converge to the hard-stop list; spool stays single-slot last-wins (logged) |
| Activation / apply | `program_playback.play_program` / `handle_v2_apply` | invariant 5 |
| Verification | `verification.py` + `flat_spec` + `delta_probe` | one grading currency; `fc_selector`'s parallel grader retires with the hunt; predicted-vs-measured is first-class |

## Decision register (final, post-review)

The direction passed an independent adversarial review on 2026-08-21; the
dispositions below incorporate its blockers and should-fixes.

| # | Ruling | Disposition notes |
|---|---|---|
| R1 | Delete the hunt: `crossover_v2/{search,objective,candidate_space}.py`, `fc_sweep`'s sweep half, `fc_selector` + its live recommendation surface. Keep `forward_model` (simulated evaluation). | `XoverCandidate` folds into `forward_model.py` (it imports it at module scope). `resolve_fc_search_band` and `recornered_preset` stay — live importers. Deletion PRs name the #2811/#2815 write-off explicitly. |
| R2 | Filter vocabulary: entry-time validation now; **Butterworth support is funded work in this plan** (owner override 2026-08-21), scoped to its true blast radius. | Blast radius named in Wave 4.1: `branch_chain.CrossoverSection` gains a family tag and `crossover_response_db/_complex` become family-aware (the headroom ledger bottoms out there); `graph_safety.output_highpass_protected` stops matching the literal `"LinkwitzRileyHighpass"`; `profile.SUPPORTED_CROSSOVER_TYPES`/`SUPPORTED_LR_ORDERS`; `staging._normalise_filter_type`/`_slope_to_lr_order`; `crossover_declaration` comparison. |
| R3 | One consolidated free-text notes field (driver notes fold in; guided placeholder bullets). Quantitative fields must compile or gate safety; fields with neither sink are deleted. | Prose is quarantined per invariant 8. The research intake's validated JSON output (safety facts, prefills) is data, not prose — it persists in the draft and rides in the packet's declarations. |
| R4 | LLM operator v1 = cloud agent over SSH. No embedded-advisor UX. Room's embedded tuning-LLM gets no new investment until Loop C re-enters. | The commissioning copy-paste research flow stays as-is. |
| R5 | Division of labor per invariants 3–4. | Doctrine deviation (c), `REASON_CORRECTION_NOT_AN_IMPROVEMENT` — a predicted-vs-predicted veto — is named in the Wave 2 burn-down. |
| R6 | Program menu + bounded LLM selection; provider orthogonality. | No identity-schema change needed now (see substrate table). |
| R7 | Tournament mechanics: harden `play_program` (adopting `temporary_bass_activation`'s prove-at-quiet-floor and shielded restore), compose-without-apply, expected-delta banking, candidate cycle per pose, N-candidate comparator. | **Same-corner contract:** candidates in a round hold the declared corner (Fc, family, slope, topology) fixed and may vary linearization EQ, trim, delay, polarity. This preserves the excitation-safety premise (permitted bands are derived from the graph's crossover high-passes) and matches `candidate_bank`'s republish gate (#2816). A future corner-varying candidate type must re-derive the permitted band per candidate. |
| R8 | Boost caps widen to the engine cap (12 dB/filter). | This **overturns the 2026-08-18 ruling recorded in `driver_prescription`** ("a prescription has no such prediction") on its own terms: the pre-registered expected delta supplies exactly that closed-loop prediction. The derived `MAX_SPL_SPEND_BOUND_DB` is re-proved and named in the widening PR, not inherited. Binding protections unchanged: envelope per-bin depth (zero in measured nulls), realized-peak headroom charging, limiters, excitation safety plan, adoption failing closed on boosted-with-indeterminate-benefit. Blend's `BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a headroom term; a summed capture cannot attribute a deficit to a driver). |
| R9 | Retention per the substrate table. | The genuinely new work is the banked-solo complex-TF loader (re-deconvolution of banked WAV + stimulus, the chain `round_views.verify_pose_curve` already walks in magnitude form) feeding `forward_model`, plus cache policy + budgets. |
| R10 | Evidence enrichment (Wave 1) + the classifier/envelope join in one per-feature record; classifier stays decoupled from the fit engine. | The packet's own `not_evaluated` rows (angle, `first_reflection_ms`, harmonics) are the spec. |
| R11 | Sequencing = artifact-dependency refusals, not a workflow engine. Runbook + one orientation verb. | The orientation verb reads the **same builders the doors read** — never a second tree-walker. |
| R12 | Legibility pass ("solo engineer" test): dead code out, enumerations honest, one artifact-kind vocabulary. | Kind namespacing applies to new writes with a tolerant reader — no bundle migration. |
| R13 | Room correction pinned; substrate stays flow-agnostic. | Nothing in `jasper/correction/` structurally resists this beyond vocabulary. |

## Measurement program constants (owner research, 2026-08-21)

From the owner's sourced research memo (Toole/CTA-2034, Keele 1974,
Struck & Temme 1994, D'Appolito, Bücklein 1981, Toole & Olive 1988, REW and
VituixCAD practice). Constants land in code as the named program definitions;
⚠ items have in-house settling experiments (below).

- **`baseline` program:** 13 poses at 1 m — horizontal 0°, ±10°, ±20°, ±30°,
  ±40°; vertical 0°, ±10°, ±20° — N=4 repeat sweeps per pose (no mic move),
  plus one nearfield capture per woofer (and port, when vented) at N=8–16.
  Single drive level ≈ 86 dB SPL at 1 m. Vertical poses activate when
  elevation capture ships; until then the program is the horizontal set +
  on-axis + nearfield.
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
- **Boost probe:** +6 dB is the standard probe (+3 dB when headroom is
  tight). A dip is correctable when measured gain-back ≥ ~0.8× commanded AND
  excess group delay is flat there; non-correctable when ≤ ~0.5× or excess-GD
  is non-flat. Policy: cut peaks freely where excess-GD is flat; probe every
  candidate dip before filling; never fill a failing dip (peaks are far more
  audible than dips — Bücklein).
- **Level dependence:** single-level default; escalate to a second level
  (≥10 dB separation, e.g. 96 dB) only on anomaly — `delta_probe`'s
  `level_dependent_shortfall` verdict is the trigger. Thermal compression
  needs sustained stimulus (tens of seconds) and stays out of v1.
- **Stopping rule:** stop when the aggregate error metric (NBD or
  target-weighted RMS over the trusted band) stops improving by more than
  ~2× its own aggregate sampling standard error for **2 consecutive
  rounds**, AND the residual sits within ±1–1.5 dB at 1/3–1/6-octave
  smoothing. No per-bin significance chasing (multiple-comparison
  inflation).
- **Nearfield splice v1 (Wave 4.2):** mic within 0.055×D of the dust cap;
  valid up to ≈4311/D_inches, bounded by the enclosure limit c/(3·M)
  (Struck & Temme); port scaled by √(Sp/Sd) and complex-summed (organ-pipe
  spikes excluded); baffle step applied analytically from the declared
  baffle width (f₃ ≈ 115/W, gradual 6 dB shelf); splice in the overlap band
  below the gate floor and above box tuning. Adds one quantitative
  commissioning field: baffle width (plus port dimensions when vented).
  ⚠ splice error budget ≈ ±1–3 dB (synthesized figure — E3).

## Work breakdown

Sizes: S ≤ ~1 day, M ≤ ~3 days, L = a week-class effort. Wave 1 tickets are
independent of each other and of Wave 2; Waves 2 and 3 can run as parallel
tracks after Wave 1 starts. Every ticket ships its tests ("pin promises with
tests") and its doc-map scan.

**Wave 0 — in flight elsewhere (hands off):** #2804 (N captures per pose +
bank-derived pose index), #2807 (walk session-end fix), #2816 (candidate
republish seam), #2822 (dead islands F3+F4), #2823 (rig ring/marker fix).
Everything below assumes they land as-is.

**Wave 1 — evidence & honesty (all S):**
1.1 Classifier artifact carries the full lab rows (~26 columns per feature);
    `FeatureVerdict` stays the 7-key gate view.
1.2 Packet reads the `positions/` sidecars and dump-ring SNR — signed angles
    and per-capture SNR appear; the corresponding `not_evaluated` rows close.
1.3 Per-bin σ across seats from the member curves already in the packet.
1.4 H2/H3 harmonic rows derived from banked captures (`deconv` harmonic
    machinery; the packet's own `not_evaluated` row says they are computable).
1.5 `gate_moved_rms_db` and `first_reflection_ms` become numeric fields
    (today one is prose inside `gate_disclosure`, the other flagged-absent).
1.6 Notes consolidation + quarantined operator-notes artifact (invariant 8);
    driver notes fold in; guided placeholder bullets; `horn_coverage_deg`
    moves into the declarations context or is deleted with it.
1.7 Entry-time crossover-vocabulary validation: the wizard offers exactly
    what compiles and refuses the rest at entry (kills the silent
    `crossover_preview_filter_unsupported` late block).
1.8 Orientation verb — `jasper-crossover-prescriber status`: prints declared
    / banked / staged / applied state and possible next actions, reading the
    same builders the doors read; hostname-derived URLs at every human
    handoff point.
1.9 `docs/llm-operator-runbook.md`: the tool menu (as inventoried), the
    happy path, division-of-labor rules, the untrusted-notes rule, the
    program menu. A map, not a script.

**Wave 2 — executor ratification (deletion-led):**
2.1 Layers-doc P2 amendment + pointer to this plan (lands with/after 2.2–2.4
    so prose states what is).
2.2 Delete `search.py`, `objective.py`, `candidate_space.py`; fold
    `XoverCandidate` into `forward_model.py`; PR body records the
    #2811/#2815 write-off as a decision. (M)
2.3 Delete `fc_sweep`'s sweep half; retire `STAGE1_INCLUDES_LATERAL` and
    `journey.lateral_adjudicates`; cancels the planned #2717 flag flip;
    closes doctrine deviation (b) `FC_REJECT_BEAMING` for free. (M)
2.4 Retire `fc_selector` and the web `SELECTION_*` surface. The
    declared-corner path is already the shipped path on every session;
    `fc_selection` becomes a versioned-absent field and readers of
    historical rounds that carry it degrade gracefully. (M)
2.5 Version + kind the `alignment_prescription` / `topology_prescription`
    envelopes and `crossover_declaration`. (S)
2.6 Dead-field removal: `DriverSpec.{fs_hz, rated_power_w,
    expected_nearfield_response}` (schema-only, never populated). (S)
2.7 Nanny burn-down: the doctrine's five recorded deviations (a)–(e),
    explicitly including (c) `REASON_CORRECTION_NOT_AN_IMPROVEMENT`. (S each)
2.8 Legibility: `audio_measurement/__init__` enumerates all modules;
    artifact-kind namespacing for new writes + tolerant reader. (S)

**Wave 3 — the tournament (M–L overall):**
3.1 Harden `play_program`: prove-at-quiet-floor before mutation and shielded
    restore on cancel (adopted from `temporary_bass_activation`). It is the
    single activation door. (S)
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
    `position_cycle` (#2804); N=4 repeats default. (M)
3.5 N-candidate comparator: a new small comparator over N independent
    `delta_probe` gradings (not a widening of `classify_delta_probe`'s
    two-graph signature), surfaced in the packet. (M)
3.6 Boost-cap widening PR per R8 (named ruling overturn + re-proved spend
    bound). (S)
3.7 Program menu as data (`baseline`/`tournament`/`verify`/`spot`), guided
    human flow speaking pose identities, LLM program request via the
    generalized `angle_capture` staging document. Elevation capture — the
    `(azimuth, elevation)` tuple and the string-and-protractor prompts — is
    the follow-on that unlocks the vertical poses and the tilt check. (L)
3.8 Retention: complex-TF loader over banked solos feeding `forward_model`;
    cache policy + budgets; eviction never crosses an active round. (M)

**Wave 4 — funded features & coherence:**
4.1 Butterworth support, scoped per R2's blast radius, one contained PR
    series with tests. (M–L)
4.2 Nearfield splice v1 per the constants above; adds the baffle-width
    field. (M)
4.3 Policy owners in the kernel: smoothing widths, arrival window, σ
    definitions. (M)
4.4 Bass bench migrates onto the hardened `play_program` (dedup; bass
    otherwise stays parked). (S)
4.5 Simulated evaluation wired as a verify field: `forward_model` over the
    banked-solo loader — predicted-vs-measured sum for declared-design
    verification. (M)

**Wave 5 — room correction re-entry** (pinned until Waves 1–3 are done):
prescribe path converges onto the doors; program model extends to the
listening-position regime; moving-mic decision made then.

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
  thresholds from measured reality.
- **E1 — lobe-tilt resolution:** inject +0.1/+0.2/+0.5 ms delay errors on a
  known-good alignment; vertical polar at 5° steps; adopt the coarsest step
  that shows a monotonic tilt at 0.2 ms.
- **E3 — splice error budget:** same speaker via nearfield splice vs
  ground-plane outdoors; RMS difference over 100–500 Hz is the real budget.

## Provenance

Inputs: the 2026-08-21 architecture session (boundary map and four recon
reports over `af9db97d`); an independent adversarial review of the direction
(2026-08-21, dispositions folded into the register above); the owner's
sourced measurement-program research memo (2026-08-21); owner rulings
recorded per-decision. The plan document itself passes the standing
adversarial gate before it is treated as ratified.

Last verified: 2026-08-21
