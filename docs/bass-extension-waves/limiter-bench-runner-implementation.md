# Bass Extension — limiter bench-runner implementation (fresh-session prompt)

> **This is the runner-implementation prompt** that the accepted
> [`limiter-bench-runner-protocol.md`](limiter-bench-runner-protocol.md)
> amendment defers to. Execute it in a **fresh context window**. **Understand the
> codebase first, then build.** The runner plays real audio at stress levels and
> temporarily mutates the live CamillaDSP graph — it is the single most
> hardware-safety-critical piece in the Bass Extension program. Every rule here
> is a hard constraint, not a suggestion.

Read [`README.md`](README.md) (the binding charter) first, then this prompt
completely.

## The two gates (non-negotiable)

Building the runner is authorized — gate 1 (the amendment is merged) is done. But:

- **Gate 2 — independent safety review before any tone plays.** The finished
  runner must pass an independent adversarial safety review at **zero Blockers /
  zero Should-fixes** BEFORE it is ever run on hardware, and **Jasper runs the
  actual bench session under supervision.** Merging the runner code does not
  authorize an unattended or unreviewed campaign.
- **No automated hardware test — by design.** The real evidence is Jasper's
  supervised on-device pass. Your deliverable is the runner + **hardware-free**
  tests + the passing safety review, *staged* for that session. Do not write a
  test that opens a device, socket, real CamillaDSP, subprocess, or real
  coordinator.

## Understand-first protocol (before writing any code)

1. `git fetch origin main`, branch from `origin/main`, record the base SHA.
2. Read the binding contracts — **reference them, do not restate or reinvent
   them** (single source of truth):
   - [`limiter-evidence-protocol.md`](limiter-evidence-protocol.md) — **FROZEN.**
     The exact campaign (stimulus roles, discovery + candidate passes, abort
     rules), the replayable bundle schema, and the pure producer contract. You
     implement this campaign; you do not redesign it or edit this file.
   - [`limiter-bench-runner-protocol.md`](limiter-bench-runner-protocol.md) — the
     runner's contract: responsibilities, the fail-closed
     temporary-graph-activation safety contract, the reuse list, the fences, and
     what stays blocked. Implement it exactly.
3. Read the reuse machinery (the wave-4 prompt's "Required reading" is the deeper
   index — [`wave-4-commissioning-backend.md`](wave-4-commissioning-backend.md)):
   `measurement_window()` (`jasper/correction/coordinator.py`); the two-boundary
   admission chain (`jasper/audio_measurement/excitation_admission.py` +
   `admitted_playback.py`); `MeasurementRamp` / `safe_playback`
   (`jasper/audio_measurement/ramp.py`); the capture relay + `BUILDERS` registry
   and `build_crossover_sweep_spec` (`jasper/capture_relay/spec.py`); the located
   playback module (`jasper/audio_measurement/playback.py`); `bundles.py` +
   `evidence_identity.py` (`record_artifact`, `write_json_artifact`,
   `ArtifactIdentity`, `json_fingerprint`); the `camilla_yaml` limiter-name and
   `_assert_bass_extension_safe` proofs (`jasper/active_speaker/camilla_yaml.py`);
   `patch_config` (`jasper/camilla.py`, the Wave-0 micro-stepped graph mutation);
   and `driver_safety.py` (`hard_excitation_band_hz`, `level_duration_limits`).
4. Read the closest **prior-art shape**: the operator-driven bench experiment
   [`CHIP-AEC-EXPERIMENT.md`](../CHIP-AEC-EXPERIMENT.md) +
   `jasper/chip_aec_experiment.py` + `scripts/chip-aec-*.sh`. The bench runner is
   the same *kind* of thing — an operator-run, bench-only campaign that produces
   evidence — mirror that shape, not a household-facing wizard.
5. **Verify the preflight facts below.** If any has drifted, STOP and report —
   do not improvise around it.
6. **Propose before you build.** In the draft PR description, write a one-page
   design: the module breakdown you will create (within the allowlist), the exact
   reuse seams, and how the discovery/candidate passes map onto
   `measurement_window` + admission + `patch_config` + the taps. Get it
   sanity-checked. Only then implement.

## Preflight facts (verify; STOP if drifted)

- Producer `jasper.bass_extension.limiter_evidence.produce_limiter_thresholds`,
  `LimiterThresholdSet`, `LimiterEvidenceRefusal` exist and are frozen. You emit
  a bundle it can consume; you never modify it.
- `measurement_window` in `jasper/correction/coordinator.py`; `admit_excitation`
  / `ExcitationRequest` / `ExcitationLimits` / `ProtectionEvidence` in
  `jasper/audio_measurement/excitation_admission.py`.
- `record_artifact` / `write_json_artifact` in
  `jasper/audio_measurement/bundles.py`; `ArtifactIdentity` / `json_fingerprint`
  in `evidence_identity.py`.
- `patch_config` on `jasper/camilla.py`'s controller; the bass limiter proofs
  (`_assert_bass_extension_safe`, `driver_baseline_limiter_name`,
  `sub_baseline_limiter_name`) in `jasper/active_speaker/camilla_yaml.py`.
- `hard_excitation_band_hz`, `level_duration_limits` in
  `jasper/active_speaker/driver_safety.py`.
- Operator CLI entries register in `pyproject.toml` `[project.scripts]` as
  `jasper-<name> = "jasper.cli.<mod>:main"` (mirror `jasper-aec-tune`).

## What to build

One bench runner that executes the frozen `limiter-evidence-protocol.md` campaign
end to end and writes the replayable bundle, driven by an operator at the bench.
Its responsibilities are the amendment's four: author the `campaign_manifest`
from operator-authorized inputs (never a default); execute the discovery +
candidate passes exactly as the frozen protocol specifies; own the fail-closed
temporary graph activation; record the receipts + PCM/capture artifacts and emit
the bundle in the exact frozen schema. During a run it **does not** call
`produce_limiter_thresholds`, `apply_bass_extension`, or any profile writer.

**Reuse — do not rebuild.** The runner is an *orchestrator*. Playback rides the
existing admission chain + ramp + located playback module; capture rides the
existing relay (+ one new `build_bass_nearfield_spec` builder mirroring
`build_crossover_sweep_spec`, server-derived geometry); graph mutation is the
existing `patch_config`; graph proof is `_assert_bass_extension_safe`; bundle IO
is `bundles.py` / `evidence_identity.py`. If you find yourself re-implementing
measurement gating, admission, graph proof, or evidence identity — stop, it
exists.

## File allowlist

Create:
- The runner module(s) under `jasper/bass_extension/` — a `bench_runner.py`, or a
  small `bench/` subpackage if the manifest / tap / receipt / bundle-emission
  concerns are genuinely clearer split. Your call after reading; pick the
  **smallest clear shape**, not the most factored.
- One operator CLI entry `jasper/cli/<name>.py` with a `main()`.
- Hardware-free tests under `tests/`.

Modify (additive only):
- `jasper/capture_relay/spec.py` — `build_bass_nearfield_spec(...)` + registry
  entry (mirror `build_crossover_sweep_spec`; `driver_capture_geometry="near_field"`,
  server-derived, never operator-supplied).
- `pyproject.toml` — one `[project.scripts]` line for the CLI entry. This is
  operator invocation, not a daemon/route/service.

Reuse WITHOUT modifying: the admission chain, ramp / `safe_playback`,
`measurement_window`, the capture relay client, `bundles.py`,
`evidence_identity.py`, the `camilla_yaml` limiter/proof helpers, `patch_config`,
`driver_safety.py`, and `limiter_evidence.py`. Changing the behaviour of any of
those, or adding a systemd unit / HTTP route / background daemon / env knob, is a
**stop-and-report**.

## The safety contract (implement the amendment's exactly)

The temporary graph activation is the highest-risk code in this program. Its
**exact mechanism is fixed** in
[`limiter-bench-runner-activation.md`](limiter-bench-runner-activation.md) — read
it and implement that addendum (mutate the *running* config only via
`set_active_config_raw` / `patch_config`; never write the on-disk file; restore
fail-closed via `reload`; prove by read-back with `view_from_camilla_dict` +
`bass_extension_block_valid` + `filter_param_matches`; build the reusable
activation helper it specifies). The prose here is the intent that addendum makes
precise:
Implement `limiter-bench-runner-protocol.md`'s "Temporary graph activation"
section exactly: enter `measurement_window()`; snapshot the **exact** predecessor
graph + profile; fade to the safe floor via the existing ramp / `safe_playback`;
apply the proposed graph (and, in the candidate pass, the candidate `clip_limit`)
with `patch_config` — an operator bench mutation, **explicitly not
`apply_bass_extension`, persisting no profile**; read the active graph back and
**prove** target identity, ordered owner chain, configured `clip_limit`, and
owner channels via `_assert_bass_extension_safe` **before unmuting**; after the
pass, fade to floor, restore the exact predecessor, re-prove it, and record a
restoration receipt; **abort immediately** on operator Stop or any protocol abort
condition, preserving partial artifacts. Use the existing per-driver limiter
as-is: no compressor, no second limiter, no new threshold knob, no invented
hardware-safety number (every number comes from the manifest, the admission
receipts, or the measurement outputs). The producer, if referenced by the
round-trip test, is imported **function-locally** and is never on a production
path.

## The 80/20 quality bar (staff-grade — not minimal-hacky, not gold-plated)

- **Separation of concerns.** The runner *composes*; it owns only campaign
  orchestration + manifest/receipt/bundle shaping + the tap. It never
  reimplements measurement gating, admission, graph proof, or evidence identity —
  those stay owned where they already live.
- **Single source of truth.** The campaign is the frozen protocol; the safety
  contract is the amendment; the bundle schema is the protocol + the producer's
  types. Reference them; do not fork or restate them.
- **Not hacky.** No shortcut around `measurement_window`, admission, the ramp, or
  the read-back proof; no "temporary" bypass; fail closed everywhere.
- **Not over-engineered.** One runner for one frozen campaign. No generic
  "measurement-orchestration framework," no plugin system, no speculative config
  knob, no parallelism, no SSE/websockets/queues, no handling for states the
  protocol cannot reach. If you catch yourself adding flexibility the protocol
  doesn't name, delete it.
- **Own the boundary, not the symptom.** The runner owns the
  temporary-graph-activation lifecycle cleanly; it does not scatter graph/limiter
  special cases through the reused modules.
- **No scope creep.** Nothing beyond executing this one campaign and emitting
  this one bundle.

## What stays blocked (unchanged by this work)

Production wiring of `produce_limiter_thresholds` (still gated on Jasper's
accepted bundle + a later Wave 4 revision naming its `evidence_fingerprint`); any
`apply_bass_extension` / `bypass_bass_extension` /
`recover_pending_bass_extension_apply` caller; profile persistence, runtime
eligibility, the scheduler, and the `review → accepted` commit; the frozen
protocol. The runner writes a bundle and nothing else.

## Tests (hardware-free only)

Per the amendment: manifest authoring from operator inputs (incl. refusal on a
missing input); the receipt + bundle **shape** matches the frozen schema
(schema-check the keys); abort + predecessor-restore logic with a **mocked**
measurement window, admission, playback, and camilla graph; and a **round-trip**
where a synthetic runner-emitted bundle is accepted by `produce_limiter_thresholds`
(proving you emit what it reads). Deterministic, no network, no sleeps, no real
device. Mirror the exemplar test style
(`tests/test_bass_extension_limiter_evidence.py`, `tests/test_capture_relay_*.py`).
The real on-device validation is Jasper's supervised bench session — not an
automated test.

## Deliverable, review, and hand-off

1. The runner + CLI + `build_bass_nearfield_spec` + hardware-free tests, in one
   reviewable PR. `scripts/test-fast` and the acceptance tests green; ruff clean.
2. **Gate 2:** run the independent adversarial safety review (the charter's
   review gate, held to the audio-safety bar) to **zero Blockers / zero
   Should-fixes**. Paste the verdict into the PR.
3. **Do not run it on hardware.** Hand the reviewed, staged runner to Jasper with
   a short operator runbook (how to invoke it, the Stop/abort control, and what a
   successful bundle looks like) for the supervised on-device session. When the
   runner lands, update the plan's **Wave status** table (the single source of
   truth) — do not restate status elsewhere.
