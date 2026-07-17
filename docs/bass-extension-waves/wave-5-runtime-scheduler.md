# Wave 5 — runtime scheduler (Codex prompt)

> **Revision 4 (2026-07-17) — implementation blocked.** The eventual
> R1 scheduler remains sealed-only, but no Wave 5 implementation is
> authorized by this prompt. `TargetSpec.limiter_threshold_dbfs` has
> no frozen commissioning producer, and Wave 4 revision 3 now blocks
> behind a focused measured-derivation prerequisite rather than
> inventing one. This revision also freezes scheduler behavior around
> Wave 3's durable natural-at-rest commit/recovery boundary. Ported/PR
> profiles remain retained and observable. Findings and rationale are
> in the changelog.

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Waves 2–3 merged AND the Wave-0
memo (this prompt assumes it confirmed **R1**: live `PatchConfig`
micro-steps are clean; if it chose R2, STOP — revised prompt needed).

> ⚠ **Mandatory stop.** Do not create or modify any Wave 5 file from
> this revision. Wave 0 proved parameter micro-steps on an existing LT;
> it did not define the target-coupled protection threshold that bounds
> program peaks at each measured target. The existing `None` values are
> not permission to use the baseline −1 dB limiter, guess a formula, or
> omit target protection. The sections below preserve the requirements
> the replacement prompt must carry; they are not an implementation
> authorization.

## Intended mission after the safety gate is resolved

The sealed-only volume-linked runtime: as canonical
`listening_level` moves, the scheduler selects among an accepted,
current `sealed_v1` profile's frozen targets and transitions the live
CamillaDSP filters click-free — retreat is prompt, re-extend is lazy,
and every failure converges toward the natural target. Plus the live
observability (`/state` fields, doctor drift/heartbeat checks).

Accepted ported/PR profiles remain commissioned data but are not
runtime-eligible: the scheduler reports the deferral, stays at
`natural`, and sends no patch. This wave must not invent fixed slots,
identity parameters, or structural patches for their changing filter
tuples.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §8.2–8.4 and §10 (read
   carefully — hysteresis, rate limits, micro-steps, limiter
   coupling, and the failure ladder are specified, not designable).
2. `docs/bass-extension-waves/wave-3-graph-emission.md` revision 4,
   then Wave 1's `TargetSpec` and ported/PR family sections. Read the
   fixed-graph scope and deferral contract carefully.
3. `jasper/volume_coordinator.py` — fully: `_dispatch`, the observer
   hooks (`note_voice_session` style), `maybe_reconcile_camilla` (the
   1 Hz reconciler pattern you extend), and the docstring's
   cross-daemon story.
4. `jasper/volume_curve.py`, `jasper/volume_persistence.py` — the
   level model and `speaker_volume.json` reads.
5. `jasper/camilla.py` — `patch_config`, `best_effort` semantics,
   timeout/retry behavior.
6. `jasper/multiroom/runtime_balance.py` — the one existing
   `PatchConfig` production caller (your patch-shape exemplar).
7. Find the 1 Hz `VolumeObserver._tick` (grep `VolumeObserver`) and
   read its gating (duck-active probe, measurement gate) — your
   reconciler must respect the same gates.

## Blocking preflight

- Waves 2–3 APIs exist (`evaluate_bass_extension_profile`,
  `bass_extension_state_summary`; accepted+current sealed graphs
  carry `bass_ext_lt` / `bass_ext_subsonic`; ported/PR graphs carry no
  `bass_ext_*` filters and state reports them runtime-ineligible).
- `CamillaController.patch_config` exists (async);
  `jasper/multiroom/runtime_balance.py` is its one production call
  site — `camilla_patch_for_trim` builds the patch dict, and the
  module awaits `camilla.patch_config(..., best_effort=True)`.
- `VolumeCoordinator._dispatch` exists; identify the smallest awaited
  gate that receives the pre-mutation previous level and runs before
  every louder Camilla/Spotify/Bluetooth actuator (if no clean seam
  exists, STOP and propose one in the report — do not monkey-patch or
  subclass).
- Confirm from the Wave-0 memo: patched params survive `set_volume_db`
  but reset on config reload (encode whatever the memo measured).
- Confirm Wave 3 defines
  `BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})`.
  If it implemented any ported/PR runtime block or structural slot
  scheme, STOP and report.
- Confirm Wave 3's one commit owner, source-explicit graph-evidence
  resolver, natural predecessor normalization, and correction-process
  recovery owner exist exactly as revision 4 specifies. A pending
  apply intent must be visible in static state and must authorize only
  its two exact natural graph fingerprints. If recovery is implicit in
  GET, runs in a new process/task, or may leave a deep graph, STOP.
- Confirm a **merged, dated replacement for Wave 4 revision 3** defines a
  deterministic evidence → `limiter_threshold_dbfs` derivation for
  every sealed target, its units/stage in the Camilla graph, refusal on
  missing evidence, and hardware-free tests; confirm Wave 4 implements
  it and accepted sealed profiles carry finite thresholds. No such
  contract exists as of this revision: Wave 4 revision 3 explicitly
  found that its existing evidence is insufficient and blocks behind
  the focused measured-derivation prerequisite. This check therefore
  fails and Wave 5 must stop. Do not design the derivation in Wave 5.

## Future file allowlist (inactive until a replacement prompt)

Create:
- `jasper/bass_extension/scheduler.py` — pure (~150 lines)
- `jasper/bass_extension/runtime.py` — transition executor +
  reconciler glue (~250 lines)
- `tests/test_bass_extension_scheduler.py`
- `tests/test_bass_extension_runtime.py`

Modify (small, seam-only):
- `jasper/volume_coordinator.py` — one optional **awaited** gate seam,
  passed both `previous_level` and `target_level`. For every louder
  canonical change, await `bass_extension_notify(previous_level,
  target_level)` before the first actuator that can raise audible level
  — Camilla master, Spotify, or Bluetooth. If retreat cannot be
  confirmed, do not execute the louder carrier write; retain the prior
  canonical level and surface the failure through the runtime event and
  state. For same/quieter changes, perform the carrier write first and
  then await best-effort convergence; failure leaves the already-safer
  level in place. Default no-op when unregistered. Direction is derived
  before `_level` is overwritten, not reconstructed in `_dispatch`.
- `jasper/voice/daemon_main.py` — register one runtime with the
  long-lived coordinator and existing 1 Hz observer.
- `jasper/control/volume_ops.py` — register the same runtime contract
  on each per-request coordinator; no background task or second
  heartbeat owner. This seam is required so web/dial writes get the
  same pre-actuator safety ordering as voice writes.
- `jasper/voice_daemon.py` — add the runtime snapshot as the exact
  `bass_extension_runtime` object in the existing `STATUS` response.
- `jasper/control/state_aggregate.py` — extend the `bass_extension`
  section with live fields (`current_target`, `scheduler_alive`,
  `last_transition_at`, `runtime_armed`). Preserve Wave 3's
  `adapter_id`, `runtime_eligible`, and `runtime_deferred_reason`.
  Treat `runtime_eligible` as adapter-level graph support;
  `runtime_armed` is true only for an accepted, current eligible
  profile whose runtime is live and confirmed.
  Preserve Wave 3's `apply_recovery_required` unchanged. While it is
  true, sealed state is `runtime_armed=false` and
  `current_target="natural"` only when the canonical resolver proves
  one of the intent's exact natural graphs; malformed/unproved pending
  state reports `current_target=null`. This transaction gate takes
  precedence over accepted/current profile state.
  For accepted ported/PR profiles: `current_target="natural"`,
  `runtime_armed=false`, and
  `runtime_deferred_reason="fixed_graph_not_defined"`.
  Merge live fields only from `voice_st["bass_extension_runtime"]`;
  jasper-voice is the sole authoritative heartbeat/current-target
  owner. If voice STATUS is unreachable or the object is missing, an
  accepted/current sealed profile reports `scheduler_alive=false`,
  `runtime_armed=false`, `current_target=null`, and
  `last_transition_at=null` (unknown is never rendered as natural).
  Deferred ported/PR remains its static healthy natural/no-arm state
  and does not acquire a heartbeat warning.
- `jasper/cli/doctor/audio.py` — extend `check_bass_extension_profile`
  or add `check_bass_extension_runtime` (one CheckResult): accepted
  sealed profile + live params ∉ frozen family → WARN drift; sealed
  scheduler heartbeat stale → WARN; accepted ported/PR → OK with the
  explicit runtime-deferred detail.
- Existing tests for the files above (extend).
- `tests/test_control_server.py` — extend the existing per-request
  coordinator coverage for control registration and ordering.

## Frozen behavior (`scheduler.py`, pure)

```python
@dataclass(frozen=True)
class SchedulerState:
    current_target_id: str
    below_since_monotonic: float | None   # when level first went <= anchor-4
    last_deepen_monotonic: float | None

def select_target(profile, listening_level: int, state: SchedulerState,
                  now_monotonic: float) -> TargetDecision:
    # TargetDecision(next_target_id, reason, new_state)
    # RETREAT: level > anchor(current) -> immediately pick the
    #   shallowest-boost target whose anchor >= level (or natural).
    # RE-EXTEND: only when level <= anchor(candidate) - 4 continuously
    #   for >= 2.0 s AND >= 5.0 s since last deepen; deepen ONE step
    #   per decision (never jump multiple targets deeper).
    # Missing/stale/bypassed profile -> natural, always.
    # Any adapter outside the shared runtime-scope constant ->
    # natural with reason="adapter_deferred", always; do not start
    # dwell or deepen timers.
```

Constants as literals with names (`REEXTEND_HYSTERESIS_LEVELS = 4`,
`REEXTEND_DWELL_SEC = 2.0`, `DEEPEN_MIN_INTERVAL_SEC = 5.0`) in
`scheduler.py`. No env overrides.

## Frozen behavior (`runtime.py`)

- `ensure_bass_target(level)` — idempotent: evaluate profile
  (Wave 2), run `select_target`, and if a change is needed, execute
  the transition. Safe to call from both hook and reconciler in any
  process; concurrent duplicate patches of identical values are
  acceptable by design. For ported/PR it records the deferred state
  and returns without reading or patching CamillaDSP. Its awaited gate
  result distinguishes `confirmed` from `failed`; a louder caller may
  continue only on `confirmed`.
- Before reading or patching live bass params, re-evaluate Wave 3's
  static state. If `apply_recovery_required=true`, do not call
  `PatchConfig`, do not start/deepen timers, and publish
  `runtime_armed=false`. Return `confirmed` to a volume gate only when
  the canonical resolver proves the live graph is one of the intent's
  exact natural fingerprints; otherwise return `failed`. Recovery is
  never a Wave 5 action.
- The existing correction measurement gate applies to **both** the
  1 Hz reconciler and hook-driven transitions. While it is held, no
  Wave 5 process patches bass filters. This lets Wave 3 normalize,
  commit, or recover the graph without a racing scheduler. On gate
  release, the next ordinary tick re-evaluates the now-persisted
  profile; no new cross-process lock, pause API, task, or handshake is
  introduced.
- Transition execution (R1, sealed only): interpolate
  `(freq_target, q_target)` from current to next member in N steps
  such that no step changes predicted response by more than 1 dB
  anywhere (compute N from Wave 1 responses; typically 4–8), spread
  over 0.5–1.0 s total, one
  `patch_config(best_effort=True)` per step patching `bass_ext_lt`,
  `bass_ext_subsonic`, AND the bass-owner limiter threshold
  (`limiter_threshold_dbfs` of the destination member). Protection
  ordering is safety-asymmetric: **deepening installs the more
  conservative destination limiter first, before adding any boost;
  retreat removes boost first and relaxes the limiter only in the
  final step**. A missing/non-finite threshold is a hard no-arm
  condition, never a fallback to −1 dB.
- Any patch failure mid-transition: stop stepping, hold, let the
  reconciler converge. Never retry-loop. If this was a pre-louder
  retreat, return `failed` so the coordinator does not actuate the
  louder carrier and restores the previous canonical level.
- Reconciler (piggybacked on the existing 1 Hz observer tick,
  respecting its existing duck/measurement gates): read
  `speaker_volume.json` + best-effort live params for eligible sealed
  profiles; if live params ∉ frozen family or unreadable, or profile
  stale/missing → step toward NATURAL (this also heals the
  reload-reset case from the Wave-0 memo). Accepted ported/PR profiles
  are a healthy no-op: do not read missing bass filters as drift and
  do not patch. Writes a heartbeat timestamp into jasper-voice's
  in-process state, exported only through `STATUS` and then curated by
  jasper-control into `/state`.

Wave 3's commit owner intentionally reloads the predecessor's persisted
natural graph inside `measurement_window()` before it records an
intent. Therefore cancellation, process death, and power loss can
leave only the intent's predecessor or desired **natural** graph.
Wave 5's pending-intent no-arm rule preserves that state until the
existing correction process rolls back; the scheduler never guesses
which profile side won and never completes the commit forward.

R1 is a parameter-only mechanism over a graph whose named filter
definitions and pipeline references never change. No transition may
add, remove, rename, or change the type of a filter.

## Tests (pinned coverage)

- Scheduler property tests: never selects a target whose anchor <
  level; retreat is single-call immediate; re-extend requires dwell +
  interval (drive `now_monotonic` explicitly — no sleeps); transient
  dip below anchor never deepens; missing/stale/bypassed → natural;
  accepted ported/PR → natural with `adapter_deferred` and no timers.
- Transition math: step count honors the ≤1 dB rule for the worked
  sealed family; limiter ordering (first-step on deepen, last-step on
  retreat) pinned.
- Runtime with mocked controller: patch failure mid-transition holds
  then reconciler converges; reconciler steps toward natural on
  unreadable params; duplicate concurrent `ensure_bass_target` calls
  produce identical final patches (no oscillation); accepted
  ported/PR calls never read or patch CamillaDSP. A pending apply
  intent never patches or advances timers, returns `confirmed` only
  for an exactly proved natural graph, and resumes ordinary selection
  only after the intent clears. A held correction measurement gate
  suppresses both tick- and hook-driven patches.
- Coordinator hook: retreat-before-louder ordering (hook fires before
  the first audible-level actuator on every rising change) — test at
  the seam with a fake awaited runtime recording call order for
  Camilla-master, Spotify, and Bluetooth; failure blocks the louder
  actuator and restores the prior canonical level; no-op when
  unregistered (existing coordinator tests must pass unchanged).
- State: accepted ported/PR preserves the commissioned profile fields,
  reports `current_target="natural"`, `runtime_armed=false`, and the
  exact deferred reason; sealed reports `runtime_armed=true` only
  when the accepted/current block is present, no apply intent exists,
  every target threshold is finite, and the runtime owns it. Pending
  exact-natural state is not armed; malformed pending state is unknown.
- STATUS/state: jasper-voice publishes the exact live object;
  jasper-control pulls it through; unavailable STATUS produces the
  sealed unknown/not-armed semantics above, never a fabricated
  heartbeat or natural target. Per-request control runtimes do not
  claim heartbeat ownership.
- Doctor: sealed drift WARN, sealed heartbeat WARN, silent when no
  profile; accepted ported/PR is OK with explicit "runtime deferred"
  detail and never produces missing-filter or stale-heartbeat WARNs.

## Anti-overengineering fences

Do NOT: create a daemon, thread, or standalone asyncio task (the
runtime lives inside existing processes' existing ticks/hooks); add
cross-process locks (idempotent convergence IS the design); persist
scheduler state to disk (it reconstructs from level + live params);
implement R2/faders; implement signal-aware scheduling (explicit
non-goal — the seam is `select_target` and that's where it would go
LATER); add config/env knobs; smooth/ramp anything beyond the
specified micro-steps (CamillaDSP's own volume ramp is not yours to
touch); modify `runtime_balance.py`, mux, ducker, or AEC code. If
the coordinator hook seam requires more than ~20 lines in
`volume_coordinator.py`, your seam is wrong — stop and report.
Do not add ported/PR identity filters, named shaping slots, filter
bypasses, or structural `PatchConfig` updates; that is a separately
budgeted graph-design/proof problem, not this wave.
Do not read or repair Wave 3's intent, publish a profile, reload a full
graph, or add a scheduler-specific transaction/quiesce protocol. The
runtime only observes the canonical state/measurement gates, stays
natural and unarmed while recovery is pending, and resumes after the
existing correction owner clears the intent.

## Acceptance commands

These commands belong to the replacement prompt. They are not
authorization to implement this revision; the blocking preflight must
first be resolved by a merged commissioning/protection contract.

```
.venv/bin/pytest tests/test_bass_extension_scheduler.py \
  tests/test_bass_extension_runtime.py -q
.venv/bin/pytest tests/test_volume_coordinator*.py -q
.venv/bin/pytest tests/test_control_server.py -q
scripts/test-fast
```

## Changelog

- **Rev 4 (2026-07-17)** — the resumed cross-wave review found that a
  durable Wave 3 commit also needs a scheduler-side exclusion rule:
  otherwise a restarted voice/control runtime could deepen or race a
  graph while the profile+DSP intent remains unresolved. Rationale:
  require Wave 3 to record only exact natural predecessor/desired
  graphs, treat intent presence as a hard no-arm/no-patch state, and
  reuse the existing correction measurement gate to exclude both tick-
  and hook-driven patches during commit/recovery. Wave 4 revision 3
  separately establishes that its current measurements cannot derive
  the limiter threshold and blocks behind a focused measured
  prerequisite. Rejected alternatives were scheduler-owned recovery,
  a new cross-process lock/handshake, forward-completing an ambiguous
  commit, arming from a pending profile, or inventing a threshold.

- **Rev 3 (2026-07-17)** — adversarial review found no producer for
  `limiter_threshold_dbfs`, reversed limiter transition ordering, an
  unawaitable Camilla-only volume hook that missed Spotify/Bluetooth
  actuators and jasper-control wiring, and process-local telemetry with
  no `/state` transport. Rationale: stop implementation rather than
  invent a protection threshold; require a later Wave 4 producer and
  replacement Wave 5 prompt. The future contract is also corrected to
  install conservative protection before boost, use an awaited
  previous→target gate for every carrier, wire the existing control
  coordinator seam, and carry live state through voice `STATUS`.
  Rejected alternatives: silently using −1 dB/`None`, computing a new
  threshold inside the scheduler, allowing a louder write after failed
  retreat, or treating process-local state as cross-process truth.

- **Rev 2 (2026-07-17)** — follows Wave 3 revision 2 after draft PR
  #1558 exposed the frozen-contract conflict. Finding: ported/PR
  families have no LT/Q and change filter count/type between members,
  so the prior instruction to interpolate their member filters through
  the sealed named pair could not preserve fixed graph structure.
  Rationale: apply Wave 0's measured R1 coefficient-micro-step result
  only to the existing sealed LT+subsonic pair; keep ported/PR
  profiles retained, natural, typed in scheduler decisions, and
  observable in `/state`/doctor. Rejected alternative: speculative
  fixed slots or filter add/remove patches, which have no Wave 0 audio
  proof and would exceed the Wave 3/5 budgets.
