# Wave 4 — commissioning backend (Codex prompt)

> **Revision 3 (2026-07-17) — implementation blocked.** Accept hands
> the desired profile in memory to Wave 3's sole profile+DSP commit
> owner; it never persists first. The existing correction process owns
> synchronous recovery before readiness and mutating bass routes. The
> current ladder/sustain/digital evidence still cannot determine a
> Camilla-stage limiter threshold, so a focused measured-derivation
> prerequisite is mandatory before any Wave 4 code. Findings and
> rationale are in the changelog.

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Waves 1–3 merged, AND the
operator has confirmed the crossover program's measurement machinery
has had its on-device burn-in (ask if unstated — this wave builds
directly on it).

> ⚠ **Mandatory stop — limiter derivation prerequisite.** Do not
> create or modify any Wave 4 implementation file from this revision.
> The frozen ladder proves acoustic linearity at one admitted sweep
> peak and the sustain test proves one admitted noise waveform; the
> digital clamp proves arithmetic headroom for the alignment. None
> defines how those observations bound arbitrary program peaks at the
> downstream per-driver limiter's detector, nor the limiter's exact
> Camilla-stage dBFS reference. Subtracting `boost_headroom_db`, reusing
> `digital_margin_db`, copying the baseline −1 dB value, or assuming a
> crest factor would invent an audio-safety parameter.
>
> Before this prompt can authorize implementation, merge a dated,
> focused measurement/protection result and revise this prompt. That
> prerequisite must:
>
> 1. identify the exact existing limiter definition and detector point
>    in the emitted bass-owner chain, with units;
> 2. state whether the already-recorded commanded volume, admitted
>    stimulus peak, rung clean ceiling, sustain result, target boost,
>    and digital-clamp evidence are sufficient; if not, specify the
>    smallest additional measured stimulus/evidence needed;
> 3. freeze one deterministic evidence-to-threshold derivation for
>    every sealed target, including missing/invalid-evidence refusal
>    and conservative ordering across targets;
> 4. provide hardware-free test vectors derived from retained evidence
>    plus the on-device validation that justifies the mapping; and
> 5. revise Wave 4 to name the pure producer and require every accepted
>    sealed target to carry a finite `limiter_threshold_dbfs`.
>
> This is a focused prerequisite, not permission to add a compressor,
> signal-aware controller, new threshold knob, default, or formula in
> this prompt. Ported/PR profile retention remains in the intended Wave
> 4 flow, but this blocked prompt publishes no new profile of any kind.

## Intended mission after the prerequisite is resolved

The commissioning flow: a ladder state machine that characterizes the
bass owner from nearfield sweeps, fits the plant, proposes a family,
verifies the deepest target with a stepped-level ladder plus a
sustain stress test, derives anchors, and commits the accepted
profile through Wave 3's transaction. Backend + HTTP only — the
browser UI is Wave 6, and this wave's JSON contracts are what Wave 6
builds against.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §7 entire (read carefully —
   the state machine, thresholds, and sustain test are specified
   there and are not yours to redesign).
2. `jasper/web/correction_crossover_backend.py` — the host-adapter
   shape you mirror: how it claims runs, opens captures, calls
   services, returns server-driven JSON. Read carefully.
3. `jasper/active_speaker/crossover_level_run.py` — the single-slot
   durable run store pattern (you build the multi-rung variant).
4. `jasper/audio_measurement/ramp.py` — `MeasurementRamp` /
   `RampController` public surface only (how a level gets settled).
5. `jasper/audio_measurement/excitation_admission.py` +
   `excitation_artifacts.py` + `admitted_playback.py` — the
   two-boundary admission chain (read the module docstrings fully;
   you supply a limits derivation, you do not modify the chain).
6. `jasper/active_speaker/excitation_safety_plan.py` — the limits-
   derivation pattern you mirror for the bass owner.
7. `jasper/correction/coordinator.py` — `measurement_window()`.
8. `jasper/capture_relay/spec.py` — the builder registry
   (`BUILDERS`/`SHIPPED_KINDS`) and one existing builder
   (`build_crossover_sweep_spec`) as the template.
9. `jasper/active_speaker/repeat_admission.py` — repeat/median/spread
   admission you reuse for the characterize captures.
10. `jasper/audio_measurement/bundles.py` + `evidence_identity.py` —
    evidence persistence and `ArtifactIdentity`.

## Preflight facts

- Waves 1–3 APIs exist as their prompts specify (spot-check:
  `adapter_for_enclosure`, `interpolate_anchors`,
  `BassExtensionProfile.from_dict`, `apply_bass_extension`).
- `build_crossover_sweep_spec` exists in `jasper/capture_relay/spec.py`
  with a `BUILDERS` registry.
- `measurement_window` exists in `jasper/correction/coordinator.py`.
- `admit_excitation`, `ExcitationRequest`, `ExcitationLimits`,
  `ProtectionEvidence` exist in
  `jasper/audio_measurement/excitation_admission.py`.
- Identify the current WAV/tone playback mechanics module (grep for
  `play_sweep` / `TonePlayer` — it has moved between
  `jasper/correction/playback.py` and
  `jasper/audio_measurement/playback.py` historically). Record which,
  use it; do not duplicate playback code.
- The driver-safety profile exposes per-target
  `hard_excitation_band_hz` and `level_duration_limits` (verify key
  names in `driver_safety.py`).

## File allowlist

Create:
- `jasper/bass_extension/ladder.py` — pure state machine (~350 lines)
- `jasper/bass_extension/limits.py` — bass-owner excitation-limits +
  ProtectionEvidence derivation, mirroring `excitation_safety_plan.py`
  (~150 lines)
- `jasper/web/bassext_backend.py` — HTTP host adapter (~450 lines)
- `tests/test_bass_extension_ladder.py`
- `tests/test_bass_extension_limits.py`
- `tests/test_web_bassext_backend.py`

Modify (additive):
- `jasper/capture_relay/spec.py` — `build_bass_nearfield_spec(...)` +
  registry entry (one builder, mirror `build_crossover_sweep_spec`,
  including its parameter name `driver_capture_geometry="near_field"`
  — server-derived, never browser-supplied).
- `jasper/audio_measurement/playback.py` (or the located playback
  module) — `ensure_bandlimited_noise_wav(path, f_lo, f_hi,
  duration_s, amplitude_dbfs, sample_rate)` for the sustain hold
  (~60 lines; deterministic seeded noise, Butterworth band edges,
  5 ms fades — reuse the module's existing WAV-writing helpers).
- `jasper/web/correction_setup.py` — routing ONLY: there is no
  module-mount registry; backend helpers are imported and dispatched
  by this god-file's `_POST_ROUTES` allowlist plus the `do_POST` /
  `do_GET` if/elif ladders. Add the `/bassext/*` routes to
  `_POST_ROUTES` and one `/bassext/` prefix dispatch block in each
  ladder, mirroring the existing `/crossover/` and `/sync/` blocks,
  with every handler body living in `bassext_backend.py`. Keep this
  file's diff to dispatch lines plus one
  `claim_bass_extension_apply_owner` entry in the existing
  `_claim_crossover_state_owners()` lifecycle list. That claim runs
  before socket adoption and `_systemd.notify_ready()`; it is not an
  HTTP handler. The file's split is a separately planned project.
Do not modify a systemd unit or socket: the flow rides the existing
correction server and the recovery owner already has the required
paths and lifecycle hook. If current main no longer matches those
facts, stop and revise the contract rather than adding a host seam.

## The ladder state machine (`ladder.py`, pure)

States exactly as plan §7.2:
`idle → characterize → fit → propose → verify_deepest → ladder →
sustain_test → derive_anchors → review → accepted`, plus `aborted`
from any state. Implement as a frozen-dataclass session snapshot +
`transition(session, event) -> session` pure function with an
explicit allowed-transition table (mirror
`commissioning_lifecycle.py`'s style). The session snapshot carries:
margin name, adapter id, capture records (ArtifactIdentity + quality
verdict per repeat), plant fit, proposed family, rung records
(`rung_ordinal, commanded_main_volume_db, listening_level,
capture_id, band_levels, compression_db_by_band, thd_summary,
tracking_rms_db, tracking_max_db, verdict`), sustain result, anchor
set, refusals. All decisions (rung pass/fail, ceiling, anchor
derivation) are pure functions in this module calling Wave 1
numerics with the `MarginPolicy` thresholds — the web layer never
computes a verdict.

Stop-conditions per rung (from plan §7.5, via `MarginPolicy`):
compression, THD (evaluated on `thd_curve`'s unmasked/SNR-valid grid
points only — band-edge masking is expected and is not a failure;
Wave 0 finding), capture clip, repeat spread > 2 dB, SNR
insufficient, digital ceiling, mic-moved coherence check
(150–400 Hz band correlation on gain-normalized consecutive rungs —
implement in `ladder.py` using Wave 1 band helpers; threshold 0.98
correlation, provisional). First failure ends the ladder; ceiling =
previous rung.

## Persistence

Durable session slot `/var/lib/jasper/bass_extension_session.json`
(env override `JASPER_BASS_EXTENSION_SESSION_STATE`), fcntl-locked
single-current-session, `claim_owner()`-style restart retirement —
mirror `CrossoverLevelRunStore`'s shape, including `interrupted`
disposition. Raw WAVs + per-rung analysis JSON into a commissioning
bundle via `bundles.py` (`record_artifact`/`write_json_artifact`);
the session JSON stores `ArtifactIdentity` pointers, never inline
data.

The existing socket-activated **`jasper-correction-web` process is the
sole lifecycle and permission owner** for the Wave 3 apply intent. Its
systemd unit already runs as root (no `User=`), already grants
`ReadWritePaths=/var/lib/jasper /var/lib/camilladsp`, and already calls
`_claim_crossover_state_owners()` before ready. Do not edit the unit or
add permissions.

`bassext_backend.claim_bass_extension_apply_owner()` first checks for
an intent without mutation. When one exists, it synchronously enters
the existing `measurement_window()`, calls Wave 3's idempotent rollback
under its writer lock, and returns only after exact predecessor
profile+graph proof or a retained-intent failure. Register that claim
in `_claim_crossover_state_owners()` before
`_systemd.notify_ready()`. This is the existing process-claim pattern,
not a daemon, background task, timer, or HTTP recovery action. Every
bass POST repeats the same guarded recovery before its own mutation;
failed isolation/proof returns a stable 409 with
`apply_recovery_required=true`. Other correction routes remain
available.

`GET /bassext/state` never invokes recovery. It reads state and reports
Wave 3's `apply_recovery_required`; while true, `available_actions` is
empty and state-advancing bass routes are blocked unless their entry
guard first completes recovery. The red Stop remains the safety
exception: it may retire/abort the session and report recovery still
pending, but never clears the intent or returns 409. A socket-activating GET may wait for the
process-level claim that precedes all request dispatch, but the GET
handler itself is read-only and there is no state-changing GET route.

Accept constructs the complete desired `BassExtensionProfile` in
memory and, for sealed profiles, enters `measurement_window()` before
passing it to Wave 3's
`apply_bass_extension(desired_profile)`. That Wave 3 function is the
one commit owner. **Do not call `save_bass_extension_profile`, a graph
loader, or a second transaction helper first or directly.** Wave 3
normalizes the predecessor to its persisted natural graph, snapshots
that predecessor, proves the desired natural graph, durably records
both, loads/readbacks DSP, publishes the desired profile, proves the
persisted pair, and clears the intent in that order. Only after it
returns success may the backend transition the session from `review`
to `accepted`.

Cancellation of the backend task propagates only after Wave 3 drains
its shielded rollback. For ported/PR profiles, that same entry point
publishes the accepted profile without a graph transaction or audio-
isolation requirement and returns the stable runtime deferral. A
failed accept leaves the exact predecessor profile/graph pair and the
session in `review`.

This revision does **not** derive or publish
`limiter_threshold_dbfs`; the mandatory stop above applies before any
Wave 4 implementation. The replacement prompt must name the measured
pure producer, and accepted sealed profiles must then contain a finite
threshold for every target. Ported/PR remains profile-retention-only
and does not imply a runtime threshold contract.

## HTTP contract (frozen — Wave 6 builds against this)

All POST bodies/responses JSON; all routes mounted under the existing
correction server; guard exactly as sibling backends do (route
allowlist → `guard_mutating_request()` → `read_json_object`).

- `GET  /bassext/state` → the full session snapshot (server-driven:
  includes `available_actions: [...]` so the UI renders state, not
  logic) + profile summary + preconditions (refusals when not
  commissionable). This route never performs recovery or another
  mutation; while an intent exists it returns no available action and
  reports `apply_recovery_required=true`.
- `POST /bassext/session/start` `{margin}` → `{session_id}` or 409
  with refusals.
- `POST /bassext/capture/start` `{role}` → relay session payload
  (tap link etc., mirroring the crossover capture start response).
- `POST /bassext/fit` `{}` → fit result or refusal.
- `POST /bassext/propose` `{margin?}` → family + anchor preview.
- `POST /bassext/verify/start` `{}` → begins verify_deepest.
- `POST /bassext/ladder/start` `{}` / `POST /bassext/ladder/abort`.
- `POST /bassext/sustain/start` `{}`.
- `POST /bassext/accept` `{}` → builds the desired profile in memory,
  invokes Wave 3's transaction, and only on its success returns the
  committed evaluation. The handler never persists first.
- `POST /bassext/stop` `{}` → the red Stop: graceful fade-down, abort
  session, restore. Must work in every state; never 409s.

Long operations run as the backend's background task with progress in
`GET /state` polling — mirror how the crossover backend handles its
async capture lifecycle; do not invent SSE/websockets.

## Playback path per rung (composition, not new machinery)

`measurement_window()` → arm via `safe_playback` (floor-confirm on
first target only) → settle level with `MeasurementRamp` (reuse its
config shapes; `verify_deepest` runs at the lowest anchor level, each
ladder rung steps `main_volume` by `margin.rung_step_db`) → Wave-4
`limits.py` derivation → generation admission → sweep WAV
(`write_driver_sweep_wav` targeting the bass-owner channel) →
playback re-admission → play → relay capture pull → quality gate →
`ladder.py` analysis. The sustain hold is the same chain with the
noise WAV and an `ExcitationRequest` that declares its true duration
and the mandatory cooldown (plan §7.6 admission note): if
`level_duration_limits.max_sweep_duration_s` refuses the hold, that
is a **correct refusal** — surface it; do not split the hold into
sneaky segments.

## Tests (pinned coverage)

- Ladder transition table: every legal transition, every illegal one
  rejected; abort from each state; restart retirement (`interrupted`).
- Rung verdicts: synthetic rung series triggering each stop-condition
  exactly once (compression, THD, clip, spread, SNR, digital,
  mic-moved); ceiling = previous rung.
- Sustain: sag-fail lowers ceiling one rung; fc-shift-fail same;
  pass records evidence.
- Anchor derivation end-to-end: ladder + two spot points →
  `interpolate_anchors` wiring, evidence tags correct.
- limits.py: band intersection, peak/duration/repeat mins vs
  driver-safety profile; sustain request honesty (duration/cooldown).
- Backend: mocked camilla/relay/ramp end-to-end happy path to
  `accepted`; Stop in mid-ladder restores and marks aborted; second
  concurrent session 409s; malformed bodies rejected via the shared
  reader; every response shape matches this contract (schema-check
  the JSON keys — Wave 6 depends on them). Pin that accept passes the
  desired profile in memory, never calls
  `save_bass_extension_profile`, and does not enter `accepted` when
  Wave 3 returns a failure. Pin `measurement_window` → Wave 3 commit →
  session `accepted` ordering and shielded cancellation. Reopen with a
  pending Wave 3 intent and prove the process claim runs before ready,
  exact recovery happens under the measurement window, GET state is
  read-only with no actions, and every POST retries recovery before its
  own mutation. Failed recovery retains the intent and blocks forward
  work without blocking the red Stop or unrelated correction routes;
  Stop cannot clear the intent.
- Spec builder: registry round-trip, constraints mono/48k/EC-off
  (mirror the existing builder tests).

## Anti-overengineering fences

Do NOT: modify the admission chain, ramp, coordinator, relay client,
or any `commissioning_*` module; build a generic "measurement
orchestration framework" (this is one state machine + one backend);
add SSE/websockets/queues; add per-rung retry loops (a failed rung is
a result, not an error to retry); parallelize captures; write a
scheduler or touch volume-coordinator code (Wave 5); create UI
(Wave 6); add config knobs beyond the one session-path env override.
Do not derive, guess, or default `limiter_threshold_dbfs` in this
revision, and do not bypass Wave 3 by persisting an accepted profile
directly. Do not add a recovery route, state-changing GET, process,
thread, task, timer, systemd edit, or permission; recovery is a
synchronous claim/mutating-request guard in the existing process.
The deep mode (full per-target ladders) is the SAME code path with
more (target, level) pairs — if you find yourself writing a second
ladder implementation for it, stop.

## Acceptance commands

```
.venv/bin/pytest tests/test_bass_extension_ladder.py \
  tests/test_bass_extension_limits.py \
  tests/test_web_bassext_backend.py -q
.venv/bin/pytest tests/test_capture_relay_*.py -q
scripts/test-fast
```

## Changelog

- **Rev 3 (2026-07-17)** — the resumed cross-wave review found that
  first-mutation recovery was not durable across a power loss, had no
  audio-isolation or permission owner, and was exposed as an
  unnecessary HTTP action. It also confirmed that the current
  ladder/sustain/digital records observe one admitted sweep/noise
  program but do not define the downstream limiter detector's dBFS
  bound for arbitrary content. Rationale: assign synchronous claim-time
  and pre-POST recovery to the existing root correction process under
  its existing measurement window and permissions; keep GET read-only;
  retain Wave 3 as the one commit owner; and block all Wave 4
  implementation behind a focused measured limiter-derivation result.
  Rejected alternatives were a recovery daemon/task/route, profile-
  first publication, copying −1 dB, subtracting boost or digital
  margin, assuming program crest factor, and shipping commissioning
  with null sealed thresholds for later repair.

- **Rev 2 (2026-07-17)** — independent review found that the frozen
  accept path saved the profile before calling Wave 3, so Wave 3 would
  snapshot the new bytes as its predecessor and could not restore the
  prior authority after a DSP failure. Rationale: keep desired state in
  memory and make the Wave 3 entry point the sole commit owner; invoke
  its durable recovery before exposing backend state. The same review
  confirmed that no frozen evidence-to-limiter-threshold producer
  exists, so this wave records null as reserved and leaves Wave 5
  blocked. Rejected alternatives were profile-first save with
  best-effort compensation and inventing a protective threshold in the
  commissioning host adapter.
