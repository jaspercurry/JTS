# Wave 3 — graph emission + contract + apply (Codex prompt)

> **Revision 10 (2026-07-17; exact-head contract repair).** Static graph
> groundwork remains
> narrowed to `sealed_v1`; ported/passive-radiator profiles remain
> valid retained commissioning artifacts. This revision also freezes
> an explicit graph-classification boundary, one predecessor-aware commit
> owner, and a durable profile+DSP recovery contract owned by the
> existing correction process. Wave 5 is not yet authorized to arm
> the graph; see its revision 9 safety gate. Revision 10 pins live
> selected-file provenance, one correction evidence handoff, whole-graph
> carrier re-proof, and repeated-cancellation rollback drain. Findings
> and rationale are in the changelog.

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
approved runtime, add the emit gate, and wire profile
accept/bypass/replacement through a local two-authority transaction built from the
existing writer-lock, validation, DSP readback, and rollback primitives.
After this wave no extension boost is active: the natural LT is exact
identity, while the mandatory protective subsonic high-pass may change
only the intended sub-band response. The graph is structurally ready
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
4. `jasper/active_speaker/camilla_yaml.py` — the active baseline
   emitter paths: `_emit_baseline_pipeline` and
   `_emit_driver_domain_pipeline`, the shared per-role filter-chain
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
9. `jasper/correction/session.py` `MeasurementSession.apply`, then
   `jasper/correction/runtime_safety.py` and the owning apply seam in
   `jasper/web/correction_setup.py` — follow the existing writer-lock
   guard and graph-evidence flow end to end.
10. `jasper/active_speaker/commissioning_apply.py`
   `_shielded_restore_locked` — the established repeated-cancellation
   drain pattern; copy its semantics, not a new rollback abstraction.
11. `tests/test_active_speaker_runtime_contract.py` — the red-team
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
- The solo and driver-domain baseline per-role chains today order
  `[bass_management_hp?] → crossover → delay → gain → limiter`
  (verify against the current `_emit_baseline_pipeline` and
  `_emit_driver_domain_pipeline`; if either chain shape has changed,
  STOP and report — your insertion point spec below depends on it).
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
  block on the bass-owner chain(s) in both the solo baseline and the
  existing active-speaker driver-domain graph for accepted+current
  **sealed_v1 only**; emit-gate extension call. This is the same local
  Layer-A protection across a transport-role change, not generic
  multiroom graph work.
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
  Freeze one canonical graph-classification boundary with sync and
  async entry points shaped to the existing hosts:

  ```python
  classify_bass_extension_graph(
      topology, *,
      evidence_source: Literal[
          "persisted_boot", "persisted_candidate", "desired"
      ],
      statefile_path: Path | None = None,
      candidate_kind: Literal[
          "explicit", "applied_baseline", "staged_all_muted"
      ] | None = None,
      candidate_path: Path | None = None,
      graph_text: str | None = None,
      applied_baseline_path: Path | None = None,
      applied_baseline_state: Mapping[str, Any] | None = None,
      profile_path: Path | None = None,
      intent_path: Path | None = None,
      staged_metadata_path: Path | None = None,
      desired_profile: BassExtensionProfile | None | object =
          _BASS_PROFILE_EVIDENCE_OMITTED,
  ) -> GraphSafety

  async classify_active_bass_extension_graph(
      topology, *,
      statefile_path: Path,
      read_active_graph_text: Callable[[], Awaitable[str | None]],
      applied_baseline_path: Path,
      profile_path: Path,
      intent_path: Path,
      staged_metadata_path: Path,
  ) -> GraphSafety
  ```

  These are two transport-shaped entry points to one boundary, not two
  policies. Both use one private, disk-I/O-free snapshot evaluator in
  `runtime_contract.py` for staged-guard parsing, bass-profile
  evaluation, pending-intent rules, and the final low-level verifier
  call. Do not duplicate those decisions between the entry points or
  introduce a snapshot/wrapper class. The synchronous entry point is
  the only one available to boot, candidate, and desired callers. The
  asynchronous entry point is the only live-active entry and always
  awaits the supplied callback inside its sandwich. Callers pass
  `lambda: camilla.get_active_config_raw(best_effort=False)`; an exception,
  `None`, non-string, or unparseable response fails closed.

  Exactly one synchronous source is legal. All persisted paths through
  the two entry points require explicit applied-baseline/profile/intent
  and staged-metadata paths. `persisted_boot` additionally requires
  `statefile_path`; the
  resolver itself parses that statefile's one `config_path` and reads
  the selected graph. It classifies the stable boot-selected file.
  `classify_active_bass_extension_graph` also parses the selector and,
  inside the same snapshot, awaits live readback and requires its
  normalized fingerprint to equal the stable boot-selected file. It
  never treats path equality alone as active-graph proof. CamillaDSP's
  `active_raw` is the executable semantic/readback witness only: compare
  its normalized parsed-YAML fingerprint with the stable selected-file
  bytes, but do **not** send `active_raw` to the canonical classifier.
  CamillaDSP strips comments, including the bounded `# Source:`
  provenance used to distinguish a legal saved baseline. After the
  complete paired snapshot and semantic equality succeed, classify the
  stable selected-file text so its original provenance remains available
  to the existing structural classifier. This does not trust a stale
  file: both selector targets, both selected-file reads, every paired
  evidence file, and live semantic equality must all agree inside the
  same bounded sandwich. Never synthesize provenance from a path or
  filename, copy a selected-file comment into mismatched live text, or
  accept a selected file whose bounded provenance/structure fails the
  existing classifier. A read/decode/parse failure, non-string live
  response, normalized live/selected mismatch, selector/file change, or
  other sandwich instability exhausts the one whole-sandwich retry and
  returns the existing fail-closed
  `bass_extension_active_snapshot_unstable` result. Stable selected text
  whose provenance or structure is invalid reaches the canonical
  classifier and preserves that classifier's specific unsafe issue code;
  it is not relabeled as snapshot instability.

  `persisted_candidate` exists only for the current
  `safe_graph_for_current_topology` startup/fallback decision, whose
  existing policy must compare persisted graphs that are not yet the
  statefile selection: an explicit `current_config_path` override, the
  applied-baseline candidate, the loopback/ring flat fallbacks, and the
  staged all-muted candidate. It forbids `statefile_path`. The fixed
  `candidate_kind` owns path provenance: `explicit` requires the exact
  current override or flat/ring path already supplied to
  `safe_graph_for_current_topology`; `applied_baseline` forbids
  `candidate_path` and derives `config.path` from the stable paired
  applied-baseline bytes; `staged_all_muted` forbids `candidate_path`
  and derives the locator from the stable paired staged metadata. The
  resolver, not the caller, reads graph bytes. It passes the immutable
  parsed staged mapping to the low-level verifier so topology identity,
  locator, and software-guard evidence remain mandatory.
  `applied_baseline.config.path` is therefore a rebuildable
  fallback-candidate locator only, never proof of the current or active
  graph. No other production caller or candidate class may use this
  source, and this revision does not move fallback selection policy into
  the bass resolver.

  The boot/active boundary performs
  `applied-baseline₁ → intent₁ → profile₁ → staged-metadata₁ → selector₁
  → selected-file₁ → [await active graph in the async entry only]
  → selected-file₂ → selector₂
  → staged-metadata₂ → profile₂ → intent₂ → applied-baseline₂`. Both
  reads of every authority file and selected graph must match byte-for-
  byte; the two parsed selector targets must
  match (unrelated Camilla statefile volume/mute changes do not change
  graph authority). The candidate boundary performs
  `applied-baseline₁ → intent₁ → profile₁ → staged-metadata₁
  → candidate-file₁ → candidate-file₂ → staged-metadata₂ → profile₂
  → intent₂ → applied-baseline₂`; both candidate reads and paired staged
  metadata match byte-for-byte. Applied-baseline- and staged-derived
  locators must also be identical in their paired authority snapshots.
  A paired missing staged-metadata file is stable empty evidence: it
  cannot authorize a guarded/all-muted graph, though graph classes that
  never depend on staged evidence retain their existing proof.
  One bounded retry of the **whole** selected or
  candidate sandwich is allowed, then instability fails closed.
  `desired` forbids all paths
  and callbacks, requires in-memory `graph_text`, applied-baseline
  state, and **explicit** profile evidence, and performs no disk read.
  Define one private module sentinel
  `_BASS_PROFILE_EVIDENCE_OMITTED = object()` solely to distinguish an
  omitted keyword from explicit `desired_profile=None`; this is not a
  snapshot/wrapper type. For `desired`, accept only a
  `BassExtensionProfile` or explicit `None`: a profile serializes to the
  existing evaluated snapshot input, while explicit `None` supplies the
  existing no-runtime-profile summary and proves the ordinary no-block
  graph. Omission and every other object are
  `bass_extension_source_invalid`. Every persisted source requires the
  sentinel default and rejects even explicit `None`, because persisted
  absence must come from the paired profile-path read. This preserves
  the rule that omitted low-level bass evidence is never interpreted as
  no profile while allowing the transaction to prove a missing,
  bypassed, stale, or otherwise non-accepted natural predecessor without
  inventing profile bytes. The boundary owns all profile evaluation;
  callers never parse profile/intent/
  statefile/staged-metadata bytes or choose bass policy themselves.
  `Mapping[str, Any]` and the existing `GraphSafety` are the concrete
  merged types;
  do not introduce an `AppliedBaselineProfileState`,
  `GraphClassification`, or another wrapper type for this seam.

  The lower-level `classify_camilla_graph` remains a pure verifier over
  the immutable graph text and returned bass evidence. It must not
  read graph/profile/intent files, invoke the persisted boundary
  implicitly, or interpret omitted evidence as "no profile." Production
  host boundaries call the matching canonical entry point; direct use of
  the low-level verifier is limited to already-frozen in-memory test or
  composition inputs. A baseline-shaped graph whose bass evidence is
  omitted is unsafe; graph classes that cannot carry the optional
  baseline block (flat/program-pipe and guarded/all-muted commissioning
  graphs) retain their existing proof.

  For baseline-shaped graphs, a valid pending intent supplies the exact predecessor and desired
  normalized-graph fingerprints plus the exact predecessor and desired
  profile bytes. The persisted resolver may authorize only those two
  already-proved natural graphs while recovery is pending, selecting
  the matching profile evaluation by graph fingerprint. A malformed
  intent, profile bytes other than the recorded predecessor/desired
  bytes, or any third graph fingerprint is unsafe. This is narrow
  restart availability, not forward completion: the transaction owner
  still rolls back to the predecessor. Non-baseline flat/program-pipe
  and guarded/all-muted classes retain their existing independent
  proof, but never become runtime-armed or complete the transaction.

  Audit every production `classify_camilla_graph` call with `rg`.
  The source mapping is frozen, not caller-selectable bass policy:
  startup/fallback uses `persisted_boot` for its statefile-selected
  graph and `persisted_candidate` for only the unselected candidates
  enumerated above; doctor uses `persisted_boot`; live correction,
  commissioning, and multiroom decisions use the async active entry;
  only a staged pre-publication transaction graph uses `desired`. Each path
  calls the canonical boundary at its host edge and threads its
  immutable result into nested decisions. Add the
  necessary seam-only caller edits to
  `jasper/cli/active_speaker.py`,
  `jasper/correction/runtime_safety.py`,
  `jasper/web/correction_setup.py`,
  `jasper/active_speaker/commissioning_{runtime,capture_producer,apply}.py`,
  `jasper/cli/doctor/audio.py`, and
  `jasper/multiroom/{active_leader_config,follower_config}.py`; do not
  add caller-specific profile policy. The live correction host passes
  `lambda: cam.get_active_config_raw(best_effort=False)` into
  `classify_active_bass_extension_graph`; it does not await or parse the
  live graph first. Generated pre-publication YAML continues to use the
  synchronous `desired` proof path. Production startup/doctor hosts
  thread CLI `--staged-metadata` or the existing
  `staged_metadata_path()` default into the resolver; they no longer
  call `load_staged_startup_config()` before classification. Retain
  in-memory staged mappings only for already-frozen direct low-level
  tests, never as persisted host authority.
- `jasper/active_speaker/baseline_profile.py` — thread the accepted
  sealed profile into **`recompose_applied_baseline_yaml`**, the
  immutable production carrier, and through its existing
  `driver_domain=True` builder/emitter seam. Default production
  recomposition
  evaluates the separately persisted bass profile against the supplied
  applied snapshot; the bass apply transaction may pass its desired
  in-memory profile explicitly so it never has to publish profile state
  before DSP readback succeeds. Mutable design drafts, crossover
  previews, and measurement stores remain outside this path.
  `baseline_candidate_fingerprint` stays a strictly bass-independent
  Layer-A identity: do not add `profile_id`, status, or bass filters to
  its payload. The applied baseline JSON remains the immutable
  recomposition/provenance anchor; its `config.sha256` describes the
  compiled Layer-A candidate and is not a locator or checksum for the
  current composite graph. The canonical resolver must use the explicit
  outputd statefile path above, never the applied artifact's `config`
  fields, to locate live durable graph authority. Reapplying a baseline
  continues to rebuild its generated cache before validation. Deferred
  adapters leave emitted YAML unchanged.
- `jasper/sound/graph_carrier.py` — active-baseline seam only: expose
  the minimal host helper needed for bass apply/bypass to reemit with
  the currently persisted preference profile, room PEQs, and output
  trim while passing the desired in-memory bass profile. Do not change
  passive, unknown, generic multiroom carrier, or flat/stereo emission;
  the shared active-speaker driver-domain seam above is explicitly in
  scope. If the current
  overlays cannot be reproduced from their existing canonical inputs,
  STOP; never parse-and-splice the loaded YAML and never silently reset
  a program layer. `recompose_active_baseline_for_bass_extension()` must
  independently re-prove the **whole** returned YAML before returning it:
  call the canonical synchronous boundary with
  `evidence_source="desired"`, the supplied topology, immutable applied
  baseline mapping, explicit in-memory profile evidence (including
  `None` for a proved no-runtime-profile predecessor), and recomposed
  graph text.
  This is the existing full runtime contract, not a direct call to only
  `bass_extension_block_valid`. If the proof is not allowed, return no
  YAML and raise `CarrierCannotHostEq` with the stable reason code
  `bass_extension_recompose_unavailable`, preserving the helper's
  existing refusal vocabulary; the caller must not validate, write,
  load, or publish the refused candidate. Pin the independent boundary
  with fault injection that makes the lower recomposer return a
  syntactically valid active graph missing a required woofer low-pass:
  the carrier helper itself must refuse it even when its bass pair is
  otherwise valid. Also pin successful whole-graph proof for explicit
  `None` (missing-profile predecessor) and for at least one bypassed or
  stale no-block predecessor; neither may bypass this carrier proof.
- `jasper/correction/session.py` — seam-only signature change in
  `MeasurementSession.apply`: the existing keyword-only guard becomes
  `prepare_guard: Callable[[], Awaitable[Mapping[str, Any]]] | None =
  None`.
  On the topology-aware `camilla_get_config` path, invoke it under the
  existing DSP-writer lock, require the returned value to be a
  `Mapping`, retain it as `bass_profile_summary`, and pass that exact
  immutable mapping to
  `assert_correction_graph_safe(result.yaml,
  bass_profile_summary=bass_profile_summary)`. Missing/non-mapping
  evidence refuses before the candidate can be loaded. Preserve the
  setter-only flat compatibility path, which continues to use
  `assert_flat_apply_safe` and needs no bass evidence.

  `jasper/web/correction_setup.py` remains the policy host.
  `_assert_room_authority_current(...) -> Mapping[str, Any]` must return
  the `bass_extension_profile_summary` from the canonical
  `classify_active_bass_extension_graph` result already obtained during
  that same writer-lock guard invocation, after the existing Room
  authority binding succeeds. It may thread that `GraphSafety` through
  its private readiness helpers, but must not parse profile/intent files,
  reconstruct bass policy, substitute the no-profile summary, or perform
  a second canonical live proof merely to obtain the mapping. The
  session consumes evidence; it does not own bass policy. Existing
  passive/no-profile graphs still arrive as an explicit host-proved
  summary, not as omitted evidence.
- `jasper/bass_extension/__init__.py` — `apply_bass_extension()` /
  `bypass_bass_extension()` seams that recompose from the immutable
  applied baseline snapshot and reuse the existing DSP writer lock,
  validation, load/readback, and exact-graph rollback primitives.
  **Do not call `apply_baseline_profile()`**: that method consumes
  mutable candidate inputs and could promote unrelated staged speaker
  work. Define the one shared graph-scope constant here as
  `BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})`.
  Every adapter goes through this predecessor-aware entry point.
  Desired ported/PR and bypassed states recompose the ordinary
  no-`bass_ext_*` baseline. If their predecessor is sealed, the owner
  must load and prove that no-block graph before publishing the
  deferred/bypassed profile; otherwise the old sealed pair would be
  stranded under an authority state that forbids it. When predecessor
  and desired graphs already have the same proved no-block fingerprint,
  the owner may skip the redundant DSP load, but profile publication
  remains inside the same durable commit boundary. Commissioning
  callers hand the desired profile to this entry point and never save
  it first.

  Refuse apply, replacement, and bypass **before any profile or graph
  mutation** while the speaker is in a bonded program-bake or
  driver-domain role. Leave the profile and every Camilla graph
  byte-for-byte unchanged. Bond entry may compile and re-prove the
  already-accepted/current profile's natural pair on the existing local
  driver-domain graph, but this one-path transaction must not invent a
  two-Camilla commit or overwrite a driver-domain carrier with a solo
  recompose. Pin this refusal at the owner boundary and in both
  leader/follower tests; no new persisted profile status is required.

  `apply_bass_extension(desired_profile)` is the **only production
  commit owner** for both authorities. Wave 4 may construct a profile
  and provide audio isolation, but it never publishes profile bytes or
  applies a bass graph itself. `bypass_bass_extension()` constructs the
  desired bypassed profile and delegates to the same owner.
  Wave 3 may land these seams and their tests, but must add **no
  production caller, route, or startup invocation**: mutation remains
  unreachable until the revised Wave 4 implementation lands the
  correction-process isolation/recovery host.

  Apply/replacement/bypass is one local durable transaction over two
  authorities, not a new generic transaction framework. Keep the
  intent helpers private to this module and persist one immutable pending
  record at
  `/var/lib/jasper/bass_extension_apply_intent.json` (test-path
  override only, same atomic-write/mode/owner handling as the profile).
  The record contains a kind/schema, operation id, exact predecessor
  and desired profile bytes (or an explicit predecessor-absent marker),
  the natural predecessor `ExactDspStateIdentity`, the predecessor
  config path plus its exact predecessor and desired file
  bytes/mode/fingerprints and normalized graph fingerprints, and the
  unchanged durable boot-selector target read from the existing
  `/var/lib/camilladsp/outputd-statefile.yml`. Atomically write and
  directory-fsync it before the first authority mutation. Do not add
  phases or a second journal: record existence is the complete rollback
  instruction.
  A surviving intent always means "roll back," never "finish forward":

  1. The sole production host (Wave 4's existing
     `jasper-correction-web` process) opens the existing
     `measurement_window()` before calling this owner. Under the DSP
     writer lock, first recover any older intent. Then reload and
     freshly prove the currently persisted predecessor's canonical
     **natural-at-rest** graph. This pre-intent normalization changes no
     profile or graph semantics and deliberately discards any ephemeral
     Wave 5 target; if interrupted, reload semantics already converge in
     the safe direction. Refuse if natural state cannot be proved.
  2. Snapshot that normalized exact predecessor graph, its readable
     config-path bytes/mode, predecessor profile bytes/absence, and the
     existing outputd statefile's `config_path`. The live path and
     durable selector must resolve to the same file. Refuse before
     mutation if they differ, if the predecessor file is outside the
     correction service's existing writable paths, or if it cannot be
     read, fingerprinted, and durably restored; do not widen
     permissions.
     Before an intent can exist, atomically rewrite those **same**
     predecessor bytes/mode with `durable=True`, fsync the parent, and
     re-read/re-prove them. This is a durability reassertion, not a new
     graph; interruption leaves old-or-identical safe bytes.
     Build the desired profile in memory, recompose from the immutable
     applied baseline plus the currently persisted program-layer
     overlays, and validate/re-proof that candidate against the desired
     profile. Staged design/preview/measurement edits are never inputs.
     Ask the carrier for YAML in memory (`out_path=None`). Write an
     operation-unique **validation scratch** under the existing Camilla
     config directory, run the existing syntax preflight, read and
     fingerprint the exact desired bytes, then unlink the scratch. It
     is never loaded or written into the outputd statefile; a crash may
     leave only an unreferenced non-authoritative orphan. Durably publish
     the one intent containing both byte sets only after both natural
     graphs have been proved; intent-write failure refuses before either
     authority changes.
  3. Atomically replace the **same already-selected config path** with
     the desired bytes using
     `atomic_write_text(..., durable=True, mode=<recorded mode>,
     group_from_parent=True)`, fsync its parent, and read back the exact
     fingerprint. Do not call `set_config_file_path` and do not edit the
     outputd statefile: its pre-existing `config_path` remains the
     durable restart selector throughout. Invoke the existing guarded
     `CamillaController.reload()` and read back the active path, active
     graph, graph-file bytes, and statefile `config_path`. All must match
     the intent. The persisted bass profile is still the predecessor at
     this point.
  4. Only after DSP readback succeeds, save the desired profile with
     file-content and parent-directory fsync, then perform a final
     graph+persisted-profile re-proof through the canonical sandwich.
  5. Any failure after either authority changes first durably restores
     the predecessor config-path bytes/mode and parent directory,
     invokes guarded `reload()` on that unchanged path, proves boot
     selector + active path + active graph + on-disk bytes, and
     restores the exact predecessor profile bytes (or durably removes
     the profile if it was previously absent). It then proves the whole
     restored authority. Apply,
     deferred-adapter replacement, and bypass use the same ordering.
     Clear and directory-fsync the intent only after the desired
     profile + active graph/path + durable graph-file bytes final proof;
     if a crash leaves an intent after a successful proof, recovery
     still conservatively restores the predecessor.

  Cancellation after intent publication drains one shielded exact
  rollback task before propagating cancellation, with the established
  `commissioning_apply._shielded_restore_locked` semantics: create the
  rollback task once; while it is not done, repeatedly await
  `asyncio.shield(task)` and remember every caught `CancelledError`;
  then call `task.result()` only after completion. Never await the still-
  pending rollback task unshielded after the first cancellation, because
  a second cancellation could cancel exact restoration. If restoration
  succeeds, re-propagate cancellation only after graph, profile, and
  intent state are exact and durable. If restoration fails, retain the
  intent and surface the existing recovery-required failure; cancellation
  must not hide a rollback failure. A process kill or power loss is
  handled from the durable record.

  `recover_pending_bass_extension_apply()` is idempotent, runs under
  the same writer lock while the host holds `measurement_window()`,
  durably restores the recorded predecessor config-path bytes/mode,
  invokes guarded `reload()` without changing the outputd statefile,
  restores profile bytes, freshly proves the boot selector + path +
  active graph + graph-file bytes + profile through the
  canonical resolver, and only then clears the intent. Applying an
  in-memory `active_raw` without restoring and fsyncing the file is not
  recovery. It runs before every new apply/bypass. Wave 4's
  existing socket-activated correction process is the named lifecycle
  and permission owner: on every process claim, before
  `_systemd.notify_ready()`, it synchronously attempts recovery; every
  bass POST repeats the guard before mutation. No GET handler performs
  recovery and there is no recovery endpoint, daemon, background task,
  or new permission. If recovery cannot obtain the measurement window
  or prove both predecessors, retain the intent, expose
  `apply_recovery_required=true`, and reject state-advancing bass POSTs while leaving
  unrelated correction GET/routes available. Wave 4's red Stop is the
  sole safety exception: it may abort/retire session state while
  leaving the intent pending, but cannot start forward work or clear
  recovery evidence.

  The global admission must sit below **every** production Camilla graph
  mutation, not only the public `dsp_writer_lock()` wrapper. Extend the
  existing private `_dsp_apply_lock` path with one task-local reentrancy
  record (held lock path + recovery permission), and expose a small
  `camilla_graph_mutation(source=...)` async context from
  `jasper.dsp_apply`. Freeze one production lock path,
  `/var/lib/camilladsp/configs/.dsp_apply.lock`, derived from the
  already-shipped Camilla config directory; per-test path injection is
  allowed, but there is no env/config override or per-candidate
  production lock. `apply_dsp_config`, `dsp_writer_lock`, and direct
  controller mutations all resolve to that same path. When the current
  task already holds the writer
  lock, it reuses that ownership; otherwise it acquires the same
  canonical lock. Immediately after acquisition and before mutation it
  checks the canonical bass intent. Ordinary ownership refuses while
  the intent exists; only the Wave 3 recovery owner may set
  `allow_pending_bass_extension_recovery=True`. A source label is not
  permission.

  `CamillaController.set_config_file_path`,
  `set_active_config_raw`, `patch_config`, and `reload` must each wrap their
  complete mutation in that context. This is the actual lowest shared
  production seam: ordinary `apply_dsp_config()` already enters the
  private lock path and therefore carries the task-local record, while
  direct startup, capture-entry, audition, correction, sound, and
  multiroom calls acquire it automatically. No call-site allowlist can
  bypass it. The race is pinned both ways: a mutation that acquired the
  lock first finishes before intent publication; an intent published
  under the lock causes every later ordinary file load, active-raw
  swap, patch, and reload to refuse. This reuses one existing fcntl lock; it is
  not a daemon, service, second lock, or generic transaction framework.
  Read-only Camilla calls, volume/mute commands, and non-DSP correction
  operations remain available.

  `jasper-correction-web` already runs as root (the unit has no
  `User=`), has `ReadWritePaths=/var/lib/jasper /var/lib/camilladsp`,
  and owns the existing process-claim hook in
  `_claim_crossover_state_owners`; no service or permission change is
  authorized. On power-up the intent may remain until the next socket
  activation, but the canonical startup classifier accepts only its
  two recorded natural graphs, and Wave 5 revision 9 treats intent
  presence as no-arm. Thus music remains available and natural while
  convergence is pending; the next correction-process claim repairs
  it before serving a mutating bass action.

  A sealed profile with any target's `subsonic is None` is outside this
  first slice: refuse before graph or profile mutation. Current Wave 1
  adapters always generate the protection; do not add a removal state,
  identity substitute, or new refusal vocabulary here.
- `jasper/dsp_apply.py` — put pending-intent admission on the private
  lock path used by `apply_dsp_config`, add the task-local reentrancy
  record, canonical production path, and `camilla_graph_mutation`
  context described above; ordinary callers retain the default refusal
  and Wave 3 is the only explicit recovery permission owner.
- `jasper/camilla.py` — wrap the four graph-mutating controller
  methods (`set_config_file_path`, `set_active_config_raw`,
  `patch_config`, `reload`) in `camilla_graph_mutation`; do not change their wire
  operations, best-effort policy, or volume/mute methods.
- `jasper/bass_extension/profile.py` — use
  `atomic_write_text(..., durable=True)` for desired publication and
  exact predecessor restoration, and directory-fsync predecessor
  removal. Also extend `bass_extension_state_summary` with `adapter_id`,
  `runtime_eligible`, `runtime_deferred_reason`, and
  `apply_recovery_required`. The deferred reason is `null` for sealed
  and the exact stable value
  `"fixed_graph_not_defined"` for ported/PR. Derive this from the
  shared runtime-scope constant; do not change profile schema/status.
  `apply_recovery_required` is true exactly while the durable intent
  exists; it is transaction observability, not profile validity.
  `runtime_eligible` is adapter-level graph support only; it does not
  imply that a profile is accepted, current, or live-armed. Wave 5's
  `runtime_armed` owns that live-state distinction.
- Tests: `tests/test_camilla_emit.py`,
  `tests/test_active_speaker_emit_gate.py` (the active-speaker
  emission/emit-gate tests — NOT `tests/test_sound_camilla_yaml*.py`,
  which pin the flat/stereo emitter you must not touch),
  `tests/test_active_speaker_runtime_contract.py`,
  `tests/test_active_speaker_cli.py` (startup/fallback candidate-source
  wiring and refusal of candidate-source use outside the fixed host),
  `tests/test_active_speaker_graph_safety.py`,
  `tests/test_bass_extension_profile.py` (durable saver and
  apply/bypass/deferred-replacement and durable graph-file seams),
  `tests/test_dsp_apply.py` (private-path admission, task-local
  reentrancy, two-sided race ordering, and sole recovery permission),
  `tests/test_camilla_controller.py` (all four graph mutations enter
  the admission context),
  `tests/test_sound_graph_carrier.py` (program-overlay preservation),
  plus seam-only signature/fixture updates in
  `tests/test_active_speaker_baseline_profile.py`,
  `tests/test_active_speaker_commissioning_apply.py`,
  `tests/test_active_speaker_commissioning_runtime.py`,
  `tests/test_active_speaker_graph_evidence.py`,
  `tests/test_active_speaker_local_subwoofer.py`,
  `tests/test_correction_session.py` (seam-only pins for the explicit
  writer-lock evidence handoff),
  `tests/test_correction_setup.py`,
  `tests/test_correction_status_and_bundles.py`,
  `tests/test_multiroom_active_leader_config.py`, and
  `tests/test_multiroom_follower_config.py`; extend only. The
  runtime-contract tests must exercise the canonical
  persisted source through startup/fallback, doctor, correction, and
  representative commissioning/multiroom call paths, plus the explicit
  desired source before profile publish.

  The multiroom tests also pin that bond entry emits and re-proves the
  accepted/current sealed profile's natural pair on the local
  driver-domain chain, while apply/replacement/bypass in either bonded
  role refuses before changing the profile or either Camilla graph.

  `tests/test_active_speaker_runtime_contract.py` must additionally pin
  that comment-free but semantically equal `active_raw` proves the stable
  selected file while canonical classification still sees its bounded
  `# Source:` provenance. A mismatched live graph, changed selector, or
  changed/unparseable selected file exhausts the retry and pins
  `bass_extension_active_snapshot_unstable`. For structurally parseable,
  semantically equal selected text with missing/malformed provenance,
  pin that the async result preserves the same specific issue code as
  the synchronous persisted classifier for that stable snapshot and is
  not relabeled as snapshot instability. The same module pins the
  sentinel boundary: explicit `None` is legal only for a no-block
  `desired` graph; omitted desired evidence, any other object, and
  explicit `None` on either persisted source are
  `bass_extension_source_invalid`. `tests/test_correction_setup.py` and
  `tests/test_correction_session.py` must jointly prove that the web host
  returns the exact canonical summary through `prepare_guard`, the
  session forwards that same object to `assert_correction_graph_safe`,
  and missing/non-mapping evidence prevents load. Do not make
  `MeasurementSession` read bass authority itself.

  `tests/test_sound_graph_carrier.py` must include the missing-woofer-
  low-pass fault injection described above and assert the stable
  `bass_extension_recompose_unavailable` refusal before the candidate
  escapes. It must also prove the same whole-graph boundary accepts
  explicit-`None` missing-profile and bypassed/stale no-block
  predecessors; omission remains `bass_extension_source_invalid`.
  `tests/test_bass_extension_profile.py` must cancel an in-progress
  rollback at least twice, prove the single restore task is never
  cancelled, and observe outer `CancelledError` only after exact graph
  bytes/mode, profile bytes/absence, and intent removal are durable.

  Add a static ownership assertion that
  `allow_pending_bass_extension_recovery=True` appears only in the
  Wave 3 commit/recovery owner; ordinary callers may not opt out.

**Same-PR rule (non-negotiable):** the `camilla_yaml.py` emission
change and the `runtime_contract.py` classification change land in
ONE PR. Shipping either alone produces graphs the re-proof rejects
(fail-closed lockout) or a contract with no emitter to prove.

## Profile state × graph proof table (complete)

| Evaluated profile state | Only approved bass-extension filter set |
|---|---|
| accepted + current `sealed_v1`, every target has subsonic protection | exactly `bass_ext_lt` + `bass_ext_subsonic` on exactly the bass-owner channels of every baseline-shaped solo or active driver-domain graph; natural LT/subsonic params; existing downstream baseline limiter unchanged |
| accepted + current `sealed_v1`, any target has `subsonic is None` | none; apply is refused before mutation and any observed graph/profile pair is `unsafe` |
| accepted + current ported/PR | no `bass_ext_*` definitions or references (ordinary baseline; runtime deferred) |
| bypassed, stale, malformed, or missing profile | no `bass_ext_*` definitions or references |
| valid pending apply intent | only the intent's exact predecessor or desired natural graph, matched by normalized fingerprint to its recorded profile evaluation; recovery required, never runtime-armed |

Every combination not listed as approved is `unsafe`, including an
accepted/current sealed profile whose expected pair disappeared, a
partial pair, wrong channels, non-natural at-rest params, or any
injected block for a deferred/bypassed/stale/missing profile. There is
no "no filters regardless of profile" escape hatch; the pending row is
an exact two-fingerprint crash-recovery proof, not a state wildcard.

## Invariants your tests must red-team

- Emitted solo and active driver-domain graphs with an accepted sealed
  profile: classification is
  `approved_active_runtime`; the LT params equal the natural member;
  boost of the emitted member is 0.0; the protective subsonic remains
  active at its commissioned natural parameter; the pre-existing
  baseline limiter name/type/params are unchanged.
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
  byte-identical to the no-profile baseline. From an already no-block
  predecessor, no redundant graph load occurs. From a sealed
  predecessor, the transaction removes and proves the exact sealed
  pair before publishing the deferred profile. Hand-injecting any
  `bass_ext_*` filter into either graph classifies `unsafe`.
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
- Durable recovery: cancel or simulate process death after intent
  publication, after graph readback, after profile publication, and
  after final proof but before intent removal; reopen state as a fresh
  process and prove idempotent exact predecessor restoration. Delete,
  truncate, and replace the selected config file at every crash point;
  recovery must durably restore the predecessor bytes/mode/path without
  changing the boot selector, reload it, and prove selector + active
  graph + path + file bytes + profile before clearing the intent. After
  a successful commit and simulated power cycle, the unchanged
  selector, durable desired bytes, and profile must classify together.
  A failed recovery retains the intent and reports
  `apply_recovery_required=true`; no new forward operation may start.
- Evidence ownership: the canonical persisted boundary is exercised by
  existing startup/fallback, doctor, correction, commissioning, and
  multiroom classifier callers; its full graph/evidence sandwich
  accepts only stable or exact pending evidence. Tests mutate selected
  graph bytes, every startup candidate's bytes and locator, staged
  metadata bytes/locator/guard fields, selector target, applied-baseline,
  profile, and intent inputs at every sandwich
  seam and require whole-snapshot
  retry/fail-closed behavior; the async active entry also refuses a live
  graph that is absent, malformed, or differs from the selected file,
  and tests prove the await happens between the paired disk reads. Pin the fixed caller-source
  mapping and that `persisted_candidate` plus its three
  `candidate_kind` values are used only inside
  `safe_graph_for_current_topology` for its existing current override,
  applied baseline, flat/ring, and staged all-muted candidates. The
  CLI no longer parses applied-baseline `config.path` or staged metadata
  before calling the resolver; doctor and other production hosts do not
  pre-read staged authority either. The staged desired graph can pass only
  `evidence_source="desired"`. The
  low-level classifier performs no disk I/O, and omitted evidence
  never means a missing profile.
- Live provenance and correction handoff: a legal selected solo
  baseline with bounded `# Source:` provenance and comment-free,
  semantically equal Camilla `active_raw` is approved; classification
  consumes the selected text only after the full live-equality sandwich.
  Every mismatch remains unsafe. Room apply receives the exact summary
  from that host proof through its writer-lock guard and refuses omitted,
  stale, reconstructed, or non-mapping evidence before load.
- Carrier independence: fault-inject a recomposed graph that retains a
  valid sealed bass pair but loses another required Layer-A protection;
  `recompose_active_baseline_for_bass_extension` independently invokes
  the whole-graph desired boundary and refuses before returning YAML.
- Commit/recovery ordering: Wave 4 never invokes the profile saver or
  graph loader directly; it enters `measurement_window()` and calls
  the one Wave 3 owner. Cancellation is shielded. A fresh
  `jasper-correction-web` process claims recovery before ready; GET
  remains read-only; failed isolation/proof keeps the intent and blocks
  state-advancing bass POSTs (apart from the never-409 safety Stop).
  With a pending intent, direct controller file loads, active-raw
  swaps, patches, reloads, and ordinary `apply_dsp_config` calls are refused;
  Wave 5 never patches/deepens. Tests race intent publication against
  a direct controller mutation in both acquisition orders. Desired
  graph/profile publication, predecessor graph/profile restoration or
  removal, and intent clearing are power-loss durable.
  Repeated cancellation during rollback cannot cancel its one restore
  task: shield/drain continues through every cancellation and propagates
  cancellation only after exact durable restoration; rollback failure
  instead retains the intent and remains the surfaced failure.
- Identity: save → accept/apply → reload/evaluate remains current and
  `baseline_candidate_fingerprint(applied_snapshot)` is unchanged.

## Anti-overengineering fences

Do NOT: add A/B branches, faders, mixers, or any R2 structure (that
door only opens if a revised prompt says so); emit any non-natural
member (runtime target changes are Wave 5's `PatchConfig` job, never
emission's); add identity LTs/Q values, placeholder shaping slots,
filter bypasses, or add/remove-at-runtime machinery for ported/PR;
change Wave 1 adapters or `TargetSpec`; add new transaction/rollback
framework (the one immutable local intent and recovery above are required);
call mutable-candidate `apply_baseline_profile`; introduce a
"filter block" abstraction into `camilla_emit.py` (two plain emit
functions, same shape as the neighbors); touch the flat/stereo emitter
(`jasper/sound/camilla_yaml.py`), passive/unknown/generic multiroom
graph carriers, multiroom state writers, multiroom emission outside the
existing active-speaker driver-domain seam, or the statefile writers;
add a new doctor result (the existing classifier
caller receives evidence; Wave 5 owns runtime checks); modify
`reconstruction_capability.py`, `driver_safety.py`, or any
commissioning behavior beyond the allowlisted evidence seams. If
`runtime_contract.py` has drifted so far
that the evidence seam you need doesn't resemble the one described,
STOP and report — do not restructure the contract to fit.

## Acceptance commands

```
.venv/bin/pytest tests/test_camilla_emit.py \
  tests/test_active_speaker_runtime_contract.py \
  tests/test_active_speaker_graph_safety.py \
  tests/test_bass_extension_profile.py \
  tests/test_dsp_apply.py \
  tests/test_camilla_controller.py \
  tests/test_sound_graph_carrier.py -q
.venv/bin/pytest tests/ -q -k "emit_gate or camilla or bass_extension"
.venv/bin/pytest tests/test_correction_runtime_safety.py \
  tests/test_correction_session.py \
  tests/test_correction_setup.py \
  tests/test_correction_status_and_bundles.py \
  tests/test_active_speaker_cli.py \
  tests/test_active_speaker_commissioning_runtime.py \
  tests/test_multiroom_active_leader_config.py \
  tests/test_multiroom_follower_config.py tests/test_doctor.py -q
scripts/test-fast
```

Plus: paste into the PR description the emitted bass-owner chain
snippet (YAML) for a sealed accepted profile, and confirm byte-for-
byte identical emission for the no-profile, ported-profile, and
PR-profile cases vs. pre-change main.

## Changelog

- **Rev 10 (2026-07-17)** — exact-head review of draft PR #1574 at
  `70eaa558329200c5cb6892b183a843ca526c3b46` (comment posted
  2026-07-17T23:10:37Z) found two blockers and two should-fix gaps in
  revision 9. First, the live boundary proved comment-free Camilla
  `active_raw` equal to the selected file and then classified the
  comment-free payload, losing the bounded baseline provenance required
  by Room, commissioning, multiroom, apply, and recovery consumers.
  Second, `MeasurementSession.apply` re-proved recomposed correction
  YAML without the host's sealed-profile summary, and no clean fix fit
  the revision-9 absolute allowlist. The remaining gaps were a Wave 3
  carrier helper that trusted the lower recomposer without independently
  running the whole-graph contract, and a rollback that awaited its task
  unshielded after the first cancellation. Rationale: use `active_raw`
  only for semantic/readback equality and classify the stable selected
  bytes whose paired snapshot retains provenance; let the existing
  writer-lock guard return the canonical summary to a seam-only
  `MeasurementSession.apply` signature; require the carrier to invoke
  the existing synchronous `desired` boundary; and copy the established
  repeated-shield drain loop. The absolute allowlist grows only by
  `jasper/correction/session.py` and
  `tests/test_correction_session.py`; all other owning files and tests
  were already listed. The first fresh full-document review of revision
  10 then found that the frozen `desired_profile=None` default could not
  distinguish omitted evidence from a legitimate missing/bypassed/stale
  no-block predecessor, and that the live clause conflated sandwich
  instability with stable structural-provenance refusal. The minimal
  follow-up uses one private omission sentinel, requires explicit `None`
  only for desired no-profile proof, and preserves the stable selected
  classifier's own issue while reserving
  `bass_extension_active_snapshot_unstable` for failed snapshots.
  Rejected alternatives were synthesizing source
  provenance, trusting an unmatched/stale selected file, parsing bass
  authority inside the session, treating omitted evidence as no profile,
  duplicating only the bass predicate in the carrier, or adding a new
  transaction/cancellation abstraction.

- **Rev 9 (2026-07-17)** — the final independent cross-path audit
  found that an accepted/current sealed profile requires the natural
  pair in every baseline-shaped graph, while the prompt named only the
  solo `_emit_baseline_pipeline` and broadly forbade multiroom
  emission. Existing follower/leader bonding emits and re-proves an
  active-speaker driver-domain Layer-A graph through
  `_emit_driver_domain_pipeline`; omitting the pair there would either
  drop commissioned subsonic protection during a transport-role change
  or fail classification. Profile mutation while bonded was also
  undefined: an active leader's local pair lives on Camilla #2 while
  the one-path transaction targets Camilla #1, and a follower's
  selected graph is driver-domain rather than the solo recompose
  carrier. Rationale: emit and re-prove the same natural pair on the
  existing local driver-domain chain at bond entry, but refuse
  apply/replacement/bypass before mutation in either bonded role.
  Generic multiroom carriers, remote bass-owner shaping, state writers,
  and a two-Camilla transaction remain out of scope. Rejected
  alternatives were a sealed no-pair classifier exemption, refusing
  ordinary bonding, overwriting a driver-domain graph with a solo
  carrier, or broad multiroom graph surgery.

- **Final seam amendment (2026-07-17)** — the final independent caller
  audit found that `jasper/correction/runtime_safety.py` alone could not
  move live correction's already-awaited Camilla read inside the async
  authority sandwich: the actual host is
  `jasper/web/correction_setup.py`, and its two owning test modules were
  outside the absolute allowlist. Rationale: allow only that host and
  `tests/test_correction_{setup,status_and_bundles}.py` as seam edits,
  pass its best-effort-false read as the canonical awaitable callback,
  and retain generated pre-publication YAML on synchronous `desired`
  proof. Rejected alternatives were keeping the live pre-read, moving
  graph policy into the web host, broadening another correction route,
  or adding a service/task/state/transport abstraction.

- **Rev 8 (2026-07-17)** — the follow-up final gate found two concrete
  implementation mismatches in revision 7. The staged all-muted graph
  requires staged locator/topology/guard metadata that was outside the
  candidate sandwich, and the live callback was typed synchronously
  even though the existing Camilla read is awaited and nullable.
  Rationale: make staged metadata a paired authority for every persisted
  proof, derive the staged locator inside that snapshot, and give the
  one canonical resolver boundary sync and async entry points that share
  a single pure evaluator. The async entry awaits best-effort-false live
  readback inside the sandwich and fails closed on no result. Rejected
  alternatives were trusting caller-parsed staged metadata, dropping
  the staged software guard, pre-reading live YAML into a synchronous
  closure, synchronously driving a coroutine on an active event loop,
  or converting all synchronous startup/doctor callers to async.

- **Rev 7 (2026-07-17)** — the fresh final gate found that revision 6
  could classify only the outputd-statefile selection, while the
  existing startup/fallback decision must also prove an unselected
  applied baseline, loopback/ring flat fallback, staged all-muted
  fallback, and an explicit current override. The CLI also parsed the
  applied-baseline locator outside the promised authority snapshot.
  Rationale: add one host-restricted `persisted_candidate` source that
  reads each existing candidate twice inside the same applied-baseline/
  intent/profile sandwich; derive the applied candidate locator inside
  that boundary; and pin the complete caller/source map in CLI and
  runtime-contract tests. Fallback selection policy remains in
  `safe_graph_for_current_topology`, and `config.path` remains only a
  rebuildable candidate locator. Rejected alternatives were treating
  every candidate as the statefile-selected graph, trusting a caller-
  read YAML string, making `config.path` current-graph authority, or
  adding a new startup selector abstraction.

- **Rev 6 (2026-07-17)** — the second independent gate found that the
  proposed public writer-lock guard did not cover `apply_dsp_config`'s
  private lock path or direct Camilla graph mutations, and that fsyncing
  intent/profile state did not make the referenced DSP graph files
  power-loss durable. Rationale: put admission on the existing private
  lock path, carry its ownership task-locally for reentrant controller
  calls, and make every production file load, active-raw swap, patch,
  and reload enter that same lock/intent boundary. The transaction now
  writes desired bytes durably into the already-selected config path,
  records exact predecessor and desired file identities plus the
  unchanged outputd boot selector, and durably restores and proves
  graph-file authority before clearing intent. Concrete merged
  classifier types and the complete guard/caller tests replace
  speculative types and incomplete commands. Rejected alternatives
  were caller-by-caller guard wiring, a second lock/service, a new
  authoritative candidate path/statefile transition, live-raw-only
  rollback, and treating an atomic-but-unfsynced graph file as durable
  authority.

- **Rev 5 (2026-07-17)** — the fresh independent review found three
  remaining authority gaps. A sealed→ported/PR replacement could leave
  the old sealed pair live; graph text was read outside the
  intent/profile snapshot; and ordinary profile publication was atomic
  but not fsync-durable. Rationale: make every adapter use the same
  predecessor-aware owner, remove/prove a predecessor sealed block
  before deferred publication, classify graph plus evidence through
  one full sandwich, and make desired/restored profile state
  power-loss durable. The review-driven audit also adds a central
  pending-intent refusal to the existing DSP writer lock so another
  full-graph writer cannot overtake recovery. Rejected alternatives
  were refusing useful sealed→deferred replacement, caller-by-caller
  evidence reads, best-effort persistence, and a new recovery daemon or
  lock service.

- **Rev 4 (2026-07-17)** — independent adversarial review found three
  remaining cross-authority gaps: Wave 4 still saved the profile before
  DSP apply, exception-only compensation could strand profile and DSP
  across cancellation/restart, and existing classifier callers had no
  frozen owner for separate profile evidence. It also found that only
  the natural LT, not the required subsonic high-pass, is exact
  pass-through. The resumed cross-wave review then found that an
  implicit disk-reading classifier still left caller policy and
  crash-time evidence ambiguous, and that first-mutation recovery had
  no process/audio-isolation owner. Rationale: make the low-level
  classifier pure; require one explicit, source-tagged evidence
  resolver across every host; make Wave 3's function the sole
  profile+graph commit owner; normalize both intent candidates to
  natural; and assign synchronous, measurement-isolated recovery to
  the already-root, already-authorized correction process. Wave 4
  passes desired state in memory, and the mission promises no extension
  boost rather than byte-identical response. Rejected alternatives
  were a generic transaction framework, hidden or caller-specific
  profile reads, an HTTP recovery action, a new daemon or boot service,
  and treating a live protective high-pass as identity.

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
