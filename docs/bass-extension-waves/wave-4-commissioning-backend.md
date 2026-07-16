# Wave 4 — commissioning backend (Codex prompt)

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Waves 1–3 merged, AND the
operator has confirmed the crossover program's measurement machinery
has had its on-device burn-in (ask if unstated — this wave builds
directly on it).

## Mission

The commissioning flow: a ladder state machine that characterizes the
bass owner from nearfield sweeps, fits the plant, proposes a family,
verifies the deepest target with a stepped-level ladder plus a
sustain stress test, derives anchors, and writes the accepted
profile. Backend + HTTP only — the browser UI is Wave 6, and this
wave's JSON contracts are what Wave 6 builds against.

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
  file's diff to dispatch lines — its split is a separately planned
  project.
- `deploy/systemd/jasper-web.socket` ONLY IF the flow genuinely needs
  a new port (it should not — it rides the existing correction
  server; if you believe otherwise, stop and report).

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
compression, THD, capture clip, repeat spread > 2 dB, SNR
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
data. Accept writes the profile via Wave 2's
`save_bass_extension_profile` then calls Wave 3's
`apply_bass_extension()`.

## HTTP contract (frozen — Wave 6 builds against this)

All POST bodies/responses JSON; all routes mounted under the existing
correction server; guard exactly as sibling backends do (route
allowlist → `guard_mutating_request()` → `read_json_object`).

- `GET  /bassext/state` → the full session snapshot (server-driven:
  includes `available_actions: [...]` so the UI renders state, not
  logic) + profile summary + preconditions (refusals when not
  commissionable).
- `POST /bassext/session/start` `{margin}` → `{session_id}` or 409
  with refusals.
- `POST /bassext/capture/start` `{role}` → relay session payload
  (tap link etc., mirroring the crossover capture start response).
- `POST /bassext/fit` `{}` → fit result or refusal.
- `POST /bassext/propose` `{margin?}` → family + anchor preview.
- `POST /bassext/verify/start` `{}` → begins verify_deepest.
- `POST /bassext/ladder/start` `{}` / `POST /bassext/ladder/abort`.
- `POST /bassext/sustain/start` `{}`.
- `POST /bassext/accept` `{}` → writes profile, applies, returns
  evaluation.
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
  the JSON keys — Wave 6 depends on them).
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
