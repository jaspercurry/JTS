# Wave 3 — graph emission + contract + apply (Codex prompt)

> **Revision 3 (2026-07-17).** Static graph groundwork remains
> narrowed to `sealed_v1`; ported/passive-radiator profiles remain
> valid retained commissioning artifacts. This revision also freezes
> immutable recomposition, bass-independent baseline identity, and
> symmetric profile+DSP rollback. Wave 5 is not yet authorized to arm
> the graph; see its revision 3 safety gate. Findings and rationale are
> in the changelog.

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Waves 1–2 merged AND the Wave-0
decision memo exists.

> ⚠ **This prompt assumes the Wave-0 memo chose R1** (single live
> filter pair, `PatchConfig` micro-steps). If the memo chose R2
> (parallel A/B branches with Aux faders), STOP — this prompt must be
> revised first; do not adapt it yourself.
>
> ⚠ **Hot files.** `camilla_yaml.py`, `runtime_contract.py`, and
> `graph_safety.py` are actively churned by the crossover program.
> Extra process rules below (edit-plan-first, same-PR contract rule,
> rebase before every push) are mandatory, not advisory.

## Mission

Teach the active-speaker graph to carry the sealed bass-extension
filter pair at the **natural target's parameters** (at-rest
invariant), extend the safety contract so such graphs re-prove as
approved runtime, add the emit gate, and wire sealed profile
accept/bypass through a local two-authority transaction built from the
existing writer-lock, validation, DSP readback, and rollback primitives.
After this wave the speaker sounds *identical* — the emitted sealed
filters are exact pass-throughs — but the graph is structurally ready
for a later, safety-complete Wave 5 revision. This wave does not
authorize a deep target or consume `limiter_threshold_dbfs`; the
existing downstream baseline limiter remains byte-for-byte and
parameter-for-parameter unchanged.

This is an explicit **sealed-only first graph slice**. Accepted,
current `ported_v1` and `passive_radiator_v1` profiles remain persisted
and visible, but they emit the byte-identical ordinary baseline and do
not arm a runtime block. Wave 1 intentionally gives those families
`qp=None`, no LT, and member `filters` tuples whose count/type can
change. Wave 3 must not synthesize identity filters or Q values to
hide that structural difference.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §8.1, §8.4–8.6 (read
   carefully), §10.4.
2. `docs/bass-extension-waves/wave-1-numerics.md` `TargetSpec` plus
   the ported/PR family sections; then
   `jasper/bass_extension/adapters/{base,ported,passive_radiator}.py`.
   Verify the merged no-LT/`qp=None` truth and the changing member
   filter shapes; do not normalize them.
3. `jasper/camilla_emit.py` — fully. Your new emitters must be
   indistinguishable in style from `emit_peaking_biquad` /
   `emit_linkwitz_riley`.
4. `jasper/active_speaker/camilla_yaml.py` — the baseline emitter
   path: `_emit_baseline_pipeline`, the per-role filter-chain
   assembly, `_assert_tweeter_outputs_protected` (your emit gate
   mirrors it), and where `BASELINE_LIMITER_CLIP_LIMIT_DB` is used.
5. `jasper/active_speaker/graph_safety.py` — the view/predicate
   pattern (`view_from_emitted_text`, the existing guard predicates).
6. `jasper/active_speaker/runtime_contract.py` — how
   `classify_camilla_graph` re-proves a baseline graph
   (`_active_graph_evidence` or its current equivalent), and what
   flips a graph to `unsafe`.
7. `jasper/active_speaker/baseline_profile.py` — the
   `recompose_applied_baseline_yaml` path and
   `apply_baseline_profile` transaction primitives. The former is your
   immutable carrier; the latter is **not** your apply entry point.
8. `jasper/sound/graph_carrier.py` — the active-baseline recompose
   path and how current preference/room/trim overlays are preserved.
9. `tests/test_active_speaker_runtime_contract.py` — the red-team
   test style you must extend.

## Preflight facts

- Wave 2's `jasper.bass_extension.profile` API exists as specified.
- Wave 1's sealed natural target has finite `qp`, while every
  ported/PR target has `qp is None`, no `LinkwitzTransform`, and at
  least one ordinary generated family changes filter count/type
  between members. If this is no longer true, STOP and report rather
  than broadening this slice.
- `emit_linkwitz_riley` and `emit_peaking_biquad` exist in
  `jasper/camilla_emit.py`.
- `_assert_tweeter_outputs_protected` (or a successor emit gate) is
  called before every emitted active YAML is returned.
- The baseline per-role chain today orders
  `[bass_management_hp?] → crossover → delay → gain → limiter`
  (verify against the current `_emit_baseline_pipeline`; if the chain
  shape has changed, STOP and report — your insertion point spec
  below depends on it).
- `classify_camilla_graph` fails closed on unknown filters in a
  baseline graph (verify by reading the evidence path; if it silently
  ignores unknown filters instead, report that too — it changes the
  contract work).

## Edit-plan-first (mandatory)

Before writing code: post (as a draft-PR description) a short edit
plan — for each allowlisted file, the functions you will touch and
the one-line reason. Wait for no one; proceed after posting — the
point is a reviewable record of intent that the reviewer diffs
against what actually changed.

## File allowlist

Modify:
- `jasper/camilla_emit.py` — `emit_linkwitz_transform_biquad(name,
  freq_act, q_act, freq_target, q_target)` and
  `emit_butterworth_highpass(name, freq, order)` (+ shared bounds
  constants), ~60 lines.
- `jasper/active_speaker/camilla_yaml.py` — optional bass-extension
  block on the bass-owner chain(s) for accepted+current **sealed_v1
  only**; emit-gate extension call.
  Insertion point: after the crossover biquads, before
  `driver_delay`. Filter names: `bass_ext_lt`, `bass_ext_subsonic`
  (shared definitions referenced from both stereo woofer channels, or
  the sub channel for local-sub owners). Emitted params = the sealed
  profile's **natural** member + its subsonic; LT emitted with
  `freq_act == freq_target, q_act == q_target` (exact pass-through).
  Ported/PR profiles omit the entire block and preserve byte-identical
  ordinary-baseline emission.
- `jasper/active_speaker/graph_safety.py` — one new predicate module-
  level function `bass_extension_block_valid(view, profile_summary)`
  returning typed evidence (mirror the existing predicate shapes).
- `jasper/active_speaker/runtime_contract.py` — extend baseline
  evidence according to the complete state table below. The proof
  takes the evaluated bass profile as separate evidence; it does not
  infer permission from filter names or mutate Layer-A identity.
- `jasper/active_speaker/baseline_profile.py` — thread the accepted
  sealed profile into **`recompose_applied_baseline_yaml`**, the
  immutable production carrier. Default production recomposition
  evaluates the separately persisted bass profile against the supplied
  applied snapshot; the bass apply transaction may pass its desired
  in-memory profile explicitly so it never has to publish profile state
  before DSP readback succeeds. Mutable design drafts, crossover
  previews, and measurement stores remain outside this path.
  `baseline_candidate_fingerprint` stays a strictly bass-independent
  Layer-A identity: do not add `profile_id`, status, or bass filters to
  its payload. Deferred adapters leave emitted YAML unchanged.
- `jasper/sound/graph_carrier.py` — active-baseline seam only: expose
  the minimal host helper needed for bass apply/bypass to reemit with
  the currently persisted preference profile, room PEQs, and output
  trim while passing the desired in-memory bass profile. Do not change
  passive, unknown, multiroom, or flat/stereo emission. If the current
  overlays cannot be reproduced from their existing canonical inputs,
  STOP; never parse-and-splice the loaded YAML and never silently reset
  a program layer.
- `jasper/bass_extension/__init__.py` — `apply_bass_extension()` /
  `bypass_bass_extension()` seams that recompose from the immutable
  applied baseline snapshot and reuse the existing DSP writer lock,
  validation, load/readback, and exact-graph rollback primitives.
  **Do not call `apply_baseline_profile()`**: that method consumes
  mutable candidate inputs and could promote unrelated staged speaker
  work. Define the one shared graph-scope constant here as
  `BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})`.
  For ported/PR, accepting retains `status="accepted"` but does not
  call the graph transaction; bypass remains a profile-state change
  only.

  Sealed apply/bypass is one local compensating transaction over two
  authorities, not a new generic transaction framework:

  1. Under the existing DSP writer lock, snapshot the exact predecessor
     bass-profile bytes (or absence) and exact loaded predecessor graph.
  2. Build the desired profile in memory, recompose from the immutable
     applied baseline plus the currently persisted program-layer
     overlays, and validate/re-proof that candidate against the desired
     profile. Staged design/preview/measurement edits are never inputs.
  3. Load and read back the candidate graph using the existing DSP
     transaction primitives. The persisted bass profile is still the
     predecessor at this point.
  4. Only after DSP readback succeeds, atomically save the desired
     profile, then perform a final graph+persisted-profile re-proof.
  5. Any failure after either authority changes restores **both** the
     exact predecessor graph and exact predecessor profile bytes (or
     removes the file if it was previously absent), then proves the
     restored pair. Apply and bypass use the same ordering.

  A sealed profile with any target's `subsonic is None` is outside this
  first slice: refuse before graph or profile mutation. Current Wave 1
  adapters always generate the protection; do not add a removal state,
  identity substitute, or new refusal vocabulary here.
- `jasper/bass_extension/profile.py` — small observability-only
  extension to `bass_extension_state_summary`: add `adapter_id`,
  `runtime_eligible`, and `runtime_deferred_reason`. The last is
  `null` for sealed and the exact stable value
  `"fixed_graph_not_defined"` for ported/PR. Derive this from the
  shared runtime-scope constant; do not change profile schema/status.
  `runtime_eligible` is adapter-level graph support only; it does not
  imply that a profile is accepted, current, or live-armed. Wave 5's
  `runtime_armed` owns that live-state distinction.
- Tests: `tests/test_camilla_emit.py`,
  `tests/test_active_speaker_emit_gate.py` (the active-speaker
  emission/emit-gate tests — NOT `tests/test_sound_camilla_yaml*.py`,
  which pin the flat/stereo emitter you must not touch),
  `tests/test_active_speaker_runtime_contract.py`,
  `tests/test_active_speaker_graph_safety.py`,
  `tests/test_bass_extension_profile.py` (apply/bypass seams),
  `tests/test_sound_graph_carrier.py` (program-overlay preservation),
  extend only.

**Same-PR rule (non-negotiable):** the `camilla_yaml.py` emission
change and the `runtime_contract.py` classification change land in
ONE PR. Shipping either alone produces graphs the re-proof rejects
(fail-closed lockout) or a contract with no emitter to prove.

## Profile state × graph proof table (complete)

| Evaluated profile state | Only approved bass-extension filter set |
|---|---|
| accepted + current `sealed_v1`, every target has subsonic protection | exactly `bass_ext_lt` + `bass_ext_subsonic` on exactly the bass-owner channels; natural LT/subsonic params; existing downstream baseline limiter unchanged |
| accepted + current `sealed_v1`, any target has `subsonic is None` | none; apply is refused before mutation and any observed graph/profile pair is `unsafe` |
| accepted + current ported/PR | no `bass_ext_*` definitions or references (ordinary baseline; runtime deferred) |
| bypassed, stale, malformed, or missing profile | no `bass_ext_*` definitions or references |

Every combination not listed as approved is `unsafe`, including an
accepted/current sealed profile whose expected pair disappeared, a
partial pair, wrong channels, non-natural at-rest params, or any
injected block for a deferred/bypassed/stale/missing profile. There is
no "no filters regardless of profile" escape hatch.

## Invariants your tests must red-team

- Emitted graph with an accepted sealed profile: classification is
  `approved_active_runtime`; the LT params equal the natural member;
  boost of the emitted member is 0.0; the pre-existing baseline
  limiter name/type/params are unchanged.
- Strip the subsonic filter from the emitted text → emit gate raises;
  hand-edit the emitted text's LT to a non-natural member →
  `classify_camilla_graph` → `unsafe`.
- No profile + hand-injected `bass_ext_lt` → `unsafe`.
- Bypassed/stale/missing profile → emitter omits the block entirely;
  classification of that graph unchanged from today (pin with an
  existing-fixture equality test).
- Accepted ported and PR profiles: profile remains `accepted`, state
  reports `runtime_eligible=false` and
  `runtime_deferred_reason="fixed_graph_not_defined"`, emission is
  byte-identical to the no-profile baseline, and no graph transaction
  is called. Hand-injecting any `bass_ext_*` filter into either graph
  classifies `unsafe`.
- Sealed profile with any missing subsonic: apply refuses before any
  graph/profile mutation; no expert-removal form is accepted in this
  slice.
- Limiter: the existing baseline limiter remains present downstream on
  every bass-owner channel with `BASELINE_LIMITER_CLIP_LIMIT_DB`; no
  target threshold is required or emitted; `devices.volume_limit ≤ 0`
  survives.
- Sealed apply and bypass: inject failure at DSP readback, profile
  publish, and final re-proof; every case restores the exact predecessor
  graph **and** predecessor profile bytes/absence. Pin that an unrelated
  staged design/crossover/measurement edit is not promoted and current
  program-layer overlays are preserved.
- Identity: save → accept/apply → reload/evaluate remains current and
  `baseline_candidate_fingerprint(applied_snapshot)` is unchanged.

## Anti-overengineering fences

Do NOT: add A/B branches, faders, mixers, or any R2 structure (that
door only opens if a revised prompt says so); emit any non-natural
member (runtime target changes are Wave 5's `PatchConfig` job, never
emission's); add identity LTs/Q values, placeholder shaping slots,
filter bypasses, or add/remove-at-runtime machinery for ported/PR;
change Wave 1 adapters or `TargetSpec`; add new transaction/rollback
framework (the local two-authority compensation above is required);
call mutable-candidate `apply_baseline_profile`; introduce a
"filter block" abstraction into `camilla_emit.py` (two plain emit
functions, same shape as the neighbors); touch the flat/stereo
emitter (`jasper/sound/camilla_yaml.py`), passive/unknown/multiroom
graph carriers, multiroom emission, or the
statefile writers; add doctor checks (Wave 5); modify
`reconstruction_capability.py`, `driver_safety.py`, or any
commissioning module. If `runtime_contract.py` has drifted so far
that the evidence seam you need doesn't resemble the one described,
STOP and report — do not restructure the contract to fit.

## Acceptance commands

```
.venv/bin/pytest tests/test_camilla_emit.py \
  tests/test_active_speaker_runtime_contract.py \
  tests/test_active_speaker_graph_safety.py \
  tests/test_bass_extension_profile.py \
  tests/test_sound_graph_carrier.py -q
.venv/bin/pytest tests/ -q -k "emit_gate or camilla or bass_extension"
scripts/test-fast
```

Plus: paste into the PR description the emitted bass-owner chain
snippet (YAML) for a sealed accepted profile, and confirm byte-for-
byte identical emission for the no-profile, ported-profile, and
PR-profile cases vs. pre-change main.

## Changelog

- **Rev 3 (2026-07-17)** — adversarial review found that revision 2
  self-invalidated `baseline_fingerprint` by adding `profile_id`,
  pointed bass apply at mutable commissioning inputs, left profile and
  DSP rollback asymmetric, permitted a missing required subsonic, and
  consumed an unproduced target limiter threshold. Rationale: keep the
  underlying Layer-A fingerprint bass-independent; recompose only from
  the immutable applied snapshot; commit profile state after DSP
  readback with exact two-authority compensation; require the Wave 1
  subsonic; and leave the existing baseline limiter untouched. The
  rejected alternative was inventing a protective-threshold derivation
  or pretending the one-artifact baseline apply transaction covered a
  second persisted authority.

- **Rev 2 (2026-07-17)** — resolves the Wave 3 stop report in draft
  PR #1558. Finding: Wave 1 intentionally forbids LT in ported/PR
  families, assigns `qp=None`, and emits member tuples whose
  count/type changes (including a high-pass that has no exact
  pass-through parameterization), while this prompt required one
  natural-Q LT pair for every adapter. Rationale: retain Wave 1 as
  truth and ship the smallest re-proofable R1 graph: sealed-only,
  natural-at-rest, with explicit profile-state observability for the
  deferred adapters. Rejected alternative: adapter-shaped fixed slots
  or structural patching; Wave 0 proved coefficient micro-steps on an
  existing named LT, not filter add/remove or bypass machinery, and
  that alternative would materially expand Waves 3/5 and require a
  separate audio-safety proof.
