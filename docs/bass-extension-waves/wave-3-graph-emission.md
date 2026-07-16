# Wave 3 — graph emission + contract + apply (Codex prompt)

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

Teach the active-speaker graph to carry the bass-extension filter
pair at the **natural target's parameters** (at-rest invariant),
extend the safety contract so such graphs re-prove as approved
runtime, add the emit gate, and wire profile accept/bypass through
the existing full-graph apply transaction. After this wave the
speaker sounds *identical* — the emitted filters are exact
pass-throughs — but the graph is structurally ready for Wave 5.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §8.1, §8.4–8.6 (read
   carefully), §10.4.
2. `jasper/camilla_emit.py` — fully. Your new emitters must be
   indistinguishable in style from `emit_peaking_biquad` /
   `emit_linkwitz_riley`.
3. `jasper/active_speaker/camilla_yaml.py` — the baseline emitter
   path: `_emit_baseline_pipeline`, the per-role filter-chain
   assembly, `_assert_tweeter_outputs_protected` (your emit gate
   mirrors it), and where `BASELINE_LIMITER_CLIP_LIMIT_DB` is used.
4. `jasper/active_speaker/graph_safety.py` — the view/predicate
   pattern (`view_from_emitted_text`, the existing guard predicates).
5. `jasper/active_speaker/runtime_contract.py` — how
   `classify_camilla_graph` re-proves a baseline graph
   (`_active_graph_evidence` or its current equivalent), and what
   flips a graph to `unsafe`.
6. `jasper/active_speaker/baseline_profile.py` — the
   `recompose_applied_baseline_yaml` path and
   `apply_baseline_profile` transaction (this is your apply seam).
7. `tests/test_active_speaker_runtime_contract.py` — the red-team
   test style you must extend.

## Preflight facts

- Wave 2's `jasper.bass_extension.profile` API exists as specified.
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
  block on the bass-owner chain(s); emit-gate extension call.
  Insertion point: after the crossover biquads, before
  `driver_delay`. Filter names: `bass_ext_lt`, `bass_ext_subsonic`
  (shared definitions referenced from both stereo woofer channels, or
  the sub channel for local-sub owners). Emitted params = the
  profile's **natural** member + its subsonic; LT emitted with
  `freq_act == freq_target, q_act == q_target` (exact pass-through).
- `jasper/active_speaker/graph_safety.py` — one new predicate module-
  level function `bass_extension_block_valid(view, profile_summary)`
  returning typed evidence (mirror the existing predicate shapes).
- `jasper/active_speaker/runtime_contract.py` — extend baseline
  evidence: a graph carrying exactly the named pair on exactly the
  bass-owner channels is `approved_active_runtime` **iff** an
  accepted+current profile exists and the params match its natural
  member (within float tolerance 1e-6); a graph carrying `bass_ext_*`
  filters with no such profile, or with off-family params, is
  `unsafe`. A graph with no bass_ext filters remains valid regardless
  of profile (bypass = plain baseline).
- `jasper/active_speaker/baseline_profile.py` — thread the accepted
  profile into recompose/emission (load via Wave 2's evaluate; the
  candidate fingerprint payload must include the bass-extension
  profile_id so a profile change invalidates the candidate cache).
- `jasper/bass_extension/__init__.py` — `apply_bass_extension()` /
  `bypass_bass_extension()` seams that flip profile `status` and
  trigger the existing `apply_baseline_profile()` transaction (no new
  transaction machinery — reuse, including its rollback). Note:
  `apply_baseline_profile` is **async with many required inputs and
  callbacks** (topology, design draft, preview, measurements,
  load_config, validate, …) — mirror how its existing caller
  assembles them; budget for ~80 lines of assembly, not a one-liner.
- Tests: `tests/test_camilla_emit.py`,
  `tests/test_active_speaker_emit_gate.py` (the active-speaker
  emission/emit-gate tests — NOT `tests/test_sound_camilla_yaml*.py`,
  which pin the flat/stereo emitter you must not touch),
  `tests/test_active_speaker_runtime_contract.py`,
  `tests/test_active_speaker_graph_safety.py`,
  `tests/test_bass_extension_profile.py` (apply/bypass seams),
  extend only.

**Same-PR rule (non-negotiable):** the `camilla_yaml.py` emission
change and the `runtime_contract.py` classification change land in
ONE PR. Shipping either alone produces graphs the re-proof rejects
(fail-closed lockout) or a contract with no emitter to prove.

## Invariants your tests must red-team

- Emitted graph with an accepted sealed profile: classification is
  `approved_active_runtime`; the LT params equal the natural member;
  boost of the emitted member is 0.0.
- Strip the subsonic filter from the emitted text → emit gate raises;
  hand-edit the emitted text's LT to a non-natural member →
  `classify_camilla_graph` → `unsafe`.
- No profile + hand-injected `bass_ext_lt` → `unsafe`.
- Bypassed/stale/missing profile → emitter omits the block entirely;
  classification of that graph unchanged from today (pin with an
  existing-fixture equality test).
- Ported profile: subsonic present on every bass-owner channel with
  corner inside the adapter window; sealed profile: subsonic present
  unless the profile records the expert removal.
- Limiter: still present downstream on every bass-owner channel with
  the natural member's threshold; `devices.volume_limit ≤ 0` survives.
- Apply/bypass round-trip through `apply_baseline_profile` with a
  mocked controller: failure injected at readback → rollback path
  taken (reuse the existing transaction tests' mocking approach).

## Anti-overengineering fences

Do NOT: add A/B branches, faders, mixers, or any R2 structure (that
door only opens if a revised prompt says so); emit any non-natural
member (runtime target changes are Wave 5's `PatchConfig` job, never
emission's); add new transaction/rollback machinery; introduce a
"filter block" abstraction into `camilla_emit.py` (two plain emit
functions, same shape as the neighbors); touch the flat/stereo
emitter (`jasper/sound/camilla_yaml.py`), multiroom emission, or the
statefile writers; add doctor checks (Wave 5); modify
`reconstruction_capability.py`, `driver_safety.py`, or any
commissioning module. If `runtime_contract.py` has drifted so far
that the evidence seam you need doesn't resemble the one described,
STOP and report — do not restructure the contract to fit.

## Acceptance commands

```
.venv/bin/pytest tests/test_camilla_emit.py \
  tests/test_active_speaker_runtime_contract.py \
  tests/test_active_speaker_graph_safety.py -q
.venv/bin/pytest tests/ -q -k "emit_gate or camilla or bass_extension"
scripts/test-fast
```

Plus: paste into the PR description the emitted bass-owner chain
snippet (YAML) for a sealed accepted profile, and confirm byte-for-
byte identical emission for the no-profile case vs. pre-change main.
