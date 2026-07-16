# Wave 5 — runtime scheduler (Codex prompt)

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Waves 2–3 merged AND the Wave-0
memo (this prompt assumes it confirmed **R1**: live `PatchConfig`
micro-steps are clean; if it chose R2, STOP — revised prompt needed).

## Mission

The volume-linked runtime: as canonical `listening_level` moves, the
scheduler selects among the accepted profile's frozen targets and
transitions the live CamillaDSP filters click-free — retreat is
prompt, re-extend is lazy, and every failure converges toward the
natural target. Plus the live observability (`/state` fields, doctor
drift/heartbeat checks).

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §8.2–8.4 and §10 (read
   carefully — hysteresis, rate limits, micro-steps, limiter
   coupling, and the failure ladder are specified, not designable).
2. `jasper/volume_coordinator.py` — fully: `_dispatch`, the observer
   hooks (`note_voice_session` style), `maybe_reconcile_camilla` (the
   1 Hz reconciler pattern you extend), and the docstring's
   cross-daemon story.
3. `jasper/volume_curve.py`, `jasper/volume_persistence.py` — the
   level model and `speaker_volume.json` reads.
4. `jasper/camilla.py` — `patch_config`, `best_effort` semantics,
   timeout/retry behavior.
5. `jasper/multiroom/runtime_balance.py` — the one existing
   `PatchConfig` production caller (your patch-shape exemplar).
6. Find the 1 Hz `VolumeObserver._tick` (grep `VolumeObserver`) and
   read its gating (duck-active probe, measurement gate) — your
   reconciler must respect the same gates.

## Preflight facts

- Waves 2–3 APIs exist (`evaluate_bass_extension_profile`,
  `bass_extension_state_summary`; emitted graphs carry `bass_ext_lt`
  / `bass_ext_subsonic` names).
- `CamillaController.patch_config` exists (async);
  `jasper/multiroom/runtime_balance.py` is its one production call
  site — `camilla_patch_for_trim` builds the patch dict, and the
  module awaits `camilla.patch_config(..., best_effort=True)`.
- `VolumeCoordinator._dispatch` exists; identify where a synchronous
  post-write hook can be registered without changing dispatch
  semantics (if no clean seam exists, STOP and propose one in the
  report — do not monkey-patch or subclass).
- Confirm from the Wave-0 memo: patched params survive `set_volume_db`
  but reset on config reload (encode whatever the memo measured).

## File allowlist

Create:
- `jasper/bass_extension/scheduler.py` — pure (~150 lines)
- `jasper/bass_extension/runtime.py` — transition executor +
  reconciler glue (~250 lines)
- `tests/test_bass_extension_scheduler.py`
- `tests/test_bass_extension_runtime.py`

Modify (small, seam-only):
- `jasper/volume_coordinator.py` — one optional hook seam: after a
  successful level write, call a registered
  `bass_extension_notify(level, direction)` synchronously
  (retreat-first ordering: when the write RAISES the level across an
  anchor, the hook fires BEFORE the camilla volume write; otherwise
  after). Default no-op when unregistered.
- The process that owns the long-lived coordinator + 1 Hz observer
  (jasper-voice's daemon wiring) — register the runtime there.
- `jasper/control/state_aggregate.py` — extend the `bass_extension`
  section with live fields (`current_target`, `scheduler_alive`,
  `last_transition_at`).
- `jasper/cli/doctor/audio.py` — extend `check_bass_extension_profile`
  or add `check_bass_extension_runtime` (one CheckResult): accepted
  profile + live params ∉ frozen family → WARN drift; scheduler
  heartbeat stale → WARN.
- Existing tests for the files above (extend).

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
```

Constants as literals with names (`REEXTEND_HYSTERESIS_LEVELS = 4`,
`REEXTEND_DWELL_SEC = 2.0`, `DEEPEN_MIN_INTERVAL_SEC = 5.0`) in
`scheduler.py`. No env overrides.

## Frozen behavior (`runtime.py`)

- `ensure_bass_target(level)` — idempotent: evaluate profile
  (Wave 2), run `select_target`, and if a change is needed, execute
  the transition. Safe to call from both hook and reconciler in any
  process; concurrent duplicate patches of identical values are
  acceptable by design.
- Transition execution (R1): interpolate `(freq_target, q_target)`
  (and ported/PR member filter params) from current to next member in
  N steps such that no step changes predicted response by more than
  1 dB anywhere (compute N from Wave 1 responses; typically 4–8),
  spread over 0.5–1.0 s total, one
  `patch_config(best_effort=True)` per step patching `bass_ext_lt`,
  `bass_ext_subsonic`, AND the bass-owner limiter threshold
  (`limiter_threshold_dbfs` of the destination member) — limiter
  moves in the FIRST step when retreating (conservative-first) and
  the LAST step when deepening.
- Any patch failure mid-transition: stop stepping, hold, let the
  reconciler converge. Never retry-loop.
- Reconciler (piggybacked on the existing 1 Hz observer tick,
  respecting its existing duck/measurement gates): read
  `speaker_volume.json` + best-effort live params; if live params ∉
  frozen family or unreadable, or profile stale/missing → step toward
  NATURAL (this also heals the reload-reset case from the Wave-0
  memo). Writes a heartbeat timestamp into the runtime's in-process
  state exposed via `/state`.

## Tests (pinned coverage)

- Scheduler property tests: never selects a target whose anchor <
  level; retreat is single-call immediate; re-extend requires dwell +
  interval (drive `now_monotonic` explicitly — no sleeps); transient
  dip below anchor never deepens; missing/stale/bypassed → natural.
- Transition math: step count honors the ≤1 dB rule for the worked
  sealed family; limiter ordering (first-step on retreat, last-step
  on deepen) pinned.
- Runtime with mocked controller: patch failure mid-transition holds
  then reconciler converges; reconciler steps toward natural on
  unreadable params; duplicate concurrent `ensure_bass_target` calls
  produce identical final patches (no oscillation).
- Coordinator hook: retreat-before-louder ordering (hook fires before
  the volume write on a rising cross) — test at the seam with a fake
  runtime recording call order; no-op when unregistered (existing
  coordinator tests must pass unchanged).
- Doctor: drift WARN, heartbeat WARN, silent when no profile.

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

## Acceptance commands

```
.venv/bin/pytest tests/test_bass_extension_scheduler.py \
  tests/test_bass_extension_runtime.py -q
.venv/bin/pytest tests/test_volume_coordinator*.py -q
scripts/test-fast
```
