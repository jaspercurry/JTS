# Brief: lane D — bass extension as a candidate kind, protected (wave 3, rows 3.1–3.5)

You are running the **bass** lane of the JTS seat-matched tuning program
(`seat-tuning-program/PLAN.md` on branch `claude/loudspeaker-tuning-architecture-iephfa`;
tracking issue #4502). You bind the limiter bench so it can run for real, add
a fit view over the seat cube, add a Layer-2 scheduled candidate kind, and make
the protection ladder a code-owned program. Four code PRs plus docs, one per
row, each from a fresh `origin/main` branch `claude/seat-w3-<row>-<slug>`. The
supervised limiter campaign (row 4.1) and the runtime scheduler (4.2) are NOT
yours; you leave the bench ready to run.

## 0. Read first

`AGENTS.md`; the plan §1, §2, §7; ADR-0257 and, once merged, ADR-0259 and
ADR-0260 (if not on main yet, proceed on the plan's §2 wording: no nearfield
rung; fit on the seat-cube median); `docs/HANDOFF-bass-extension-plan.md`
§§ on the family and margins (lines ~60–145) and the R1 patch mechanism
(~1006–1048); `docs/bass-extension-waves/limiter-evidence-protocol.md` and
`limiter-tap-realization.md` in full (frozen contracts you must not weaken);
`jasper/cli/null_door.py` `_play_and_capture` and `_run` (your play-path
template); `jasper/active_speaker/crossover_v2/blend_prescription.py` (door
template). Every `file:line` below was verified at main `72bc347` (2026-09-08,
after PR #4510); re-verify at your HEAD and stop a row whose premise is false.

## 1. Facts (verified at main `72bc347`)

**The bench and its two unbound collaborators.** `PlayAndCapture` Protocol
(`jasper/bass_extension/bench/executor.py:148-181`): `async def play(*, target:
TargetPlan, role, request: StimulusRequest, stop: Stop, stimulus_path: Path,
reference: ReferenceSweepCapture | None = None) -> PlayedStimulus`. `TargetPlan`
(`bench/runner.py:85-94`): `target_id, target_fingerprint, graph_raw_text,
limiter_name, owner_channels, profile_summary, baseline_clip_limit_dbfs`.
`BenchRoleExecutor(target=...)` (`executor.py:358`) is never instantiated
in-tree. Rung sequencing `run_discovery → finish_discovery →
run_reference_sweep → run_candidate → finish_candidate` (`:897,935,1013,1044,1130`),
each through `_prepare_and_play` (`:418`, calls `play_and_capture.play` at
`:455`). Stimuli (`:192-219`): `sweep_transparency` = synchronized swept sine
(`audio_measurement.sweep`); `sustain_stress` / `digital_transfer_probe` =
band-limited noise (`ensure_bandlimited_noise_wav`). `--live` refuses at
`jasper/cli/bass_extension_bench.py:181-189` naming both collaborators; issue
#1738 at `:148`. `runner.py` (639) orchestrates activation only; `stimulus.py`
(285) pads lead-in/out; `live_proof.py` (250) is pure predicates;
`derivation.py` (732) renders pre/post-limiter tap configs offline.

**The engine play path to mirror.** `jasper-null`'s `_play_and_capture`
(`jasper/cli/null_door.py:351-413`): `bind_program_playback_seams` →
`make_wired_recorder` → `recorder.start()` → `play_program` →
`recorder.finish(tail_s=WIRED_POST_ROLL_S)` (or `abort()` on failure) →
`mint_wired_answer`; `_run` calls `require_wired_mic()` **outside** the
`measurement_door` (`:697`) and plays inside it (`:732`). `play_program(program,
*, session_volume_plan, readmit, play_wav, writer_lock)`
(`jasper/active_speaker/program_playback.py:81-124`) enforces
`session_volume_plan.assert_ready()`, a fresh `readmit()` (raises
`ProgramPlaybackRefused`), then `writer_lock()`. `ProgramPlaybackTransaction.run(*,
spec, position_deg, prompt, level_db, stimulus_dbfs) -> PlaybackOutcome`
(`crossover_v2/program_transaction.py:169`). Admission:
`readmit_program_from_wav`, `readmit_summed_program_from_wav`
(`jasper/active_speaker/program_admission.py:609,666`); declared caps:
`declared_level_ceiling_dbfs`, `resolve_driver_excitation_ceilings`,
`prepare_driver_excitation_plan` (`excitation_safety_plan.py:493,535,737`).
Wired kernel (leaf): `WiredRecorder` (`jasper/audio_measurement/wired_capture.py:309`;
`start` :407, `finish(*, tail_s)` :429, `abort` :450), `require_wired_mic`
(`:240`), `WiredCaptureAnswer` (`:603`), `setup_from_hint` (`:620`),
`mint_wired_answer` (`:653`), `make_wired_recorder` (`:698`).

**Plant models and the family.** `EnclosureAdapter.fit_plant(captures:
Mapping[CaptureRole, MagnitudeCurve], cabinet: CabinetInfo) -> PlantFit |
FitRefusal` (`jasper/bass_extension/adapters/base.py` ~`:94`); fits:
`SealedPlantFit` (`sealed.py:37`: `f0_hz, q0, fit_rms_db`), `PortedPlantFit`
(`ported.py:30`: adds `fb_hz, knee_hz, knee_slope, natural_curve`),
`PassiveRadiatorPlantFit` (`passive_radiator.py:27`: adds `notch_hz`). The
per-corner family is each adapter's `generate_family` (`sealed.py:174`) →
`tuple[TargetSpec, ...]`; **`targets.py` owns `MarginPolicy`/`MARGINS`
(`:16-69`: conservative/normal/aggressive; `boost_cap_db`, `rung_step_db`,
subsonic corner ratio/order), not the corners.** `linkwitz_transform_params`
(`alignment.py:28-44`) returns the CamillaDSP dict `{type, freq_act, q_act,
freq_target, q_target}`.

**Record, emission, safety.** `BassExtensionProfile` (`profile.py:193-408`:
`bass_owner, enclosure, targets, anchors, clean_ceiling, sustain_test, status`);
`/state` reporter `bass_extension_state_summary` (`:525-632`).
`_bass_extension_emission` (`jasper/active_speaker/camilla_yaml.py:497-538`)
requires `adapter_id == "sealed_v1"`, `status == "accepted"`, owner channels
matching the preset; `_emit_bass_extension_definitions` (`:563`),
`_bass_extension_chain_names` (`:584`); placement in the per-driver chain after
crossover and linearization, before delay/gain/limiter (`:987-1013`, sub
`:1017-1025`); `_assert_bass_extension_safe` (`:604-660`) re-proves the strict
`LT → subsonic → limiter` order; `classify_bass_extension_graph`
(`runtime_contract.py:3919`); `bass_management_corner_hz`
(`jasper/output_topology.py:1077-1094`). `produce_limiter_thresholds`
(`limiter_evidence.py:1194`) "deliberately has no production caller".

**Distortion and level.** `HarmonicReading` (`jasper/audio_measurement/distortion.py:101-166`;
`floor_limited()`, `images_clean`, `clearance_s` disclose per-order
contamination); `read_segment_distortion` (`:609`); view
`jasper/cli/round_views/distortion.py:34`. `SeatLevelTarget`
(`seat_level_reference.py:102`); `_proven_level` (`crossover_v2/session.py:535`)
returns `None` unless the fader matches. Runtime (later wave): `set_volume_db`
(`jasper/camilla.py:666`), `patch_config` (`:964`, wraps CamillaDSP's
`PatchConfig` query); plan R1 = stepped `PatchConfig` on the live filter.

**Boundaries.** `jasper/bass_extension` already imports `jasper.active_speaker`
at module level (`bench/activation.py:37,41`, `derivation.py:102,106`,
`excitation.py:25`, `executor.py:64`); `camilla_yaml.py` imports
`bass_extension` only under `TYPE_CHECKING` (`:100-101`), so there is no
runtime cycle and the bench may use the engine's play path directly.
`PACKAGE_BOUNDARIES` (`tests/test_correction_boundary_ssot.py:292-313`) does
**not** list `bass_extension` at all.

**Tests.** Retire lane (A) deletes `tests/test_bass_extension_ladder.py` (703),
`_plan_status.py` (61), `_runtime_gate_ssot.py` (196) and the apply-pathway
functions inside `_profile.py` (1,560, mixed). Yours stay: adapters (426),
alignment (76), targets (117), the 14 `bench_*` files, `limiter_evidence`
(826), `limiter_protocol` (168), `state` (155), `refusal_vocab` (100).

**Input contract from lane B.** `room_median.json` beside a seat round:
`{"freqs_hz", "median_db", "spread_db", "n_positions", "positions":
[{"id", "deviation_db"}], "ceiling_hz", "ceiling_source", "window": "ungated"}`.
Until lane B lands, hand-build fixtures of that shape.

## 2. Rows

### 3.1 — Bind the bench (branch `claude/seat-w3-3-1-bench-binding`) — **NN**

1. New `jasper/bass_extension/bench/wired_play.py` implementing
   `PlayAndCapture.play` exactly as `null_door._play_and_capture` does it:
   `require_wired_mic()` once, outside the door; per rung `make_wired_recorder`
   → `start` → `play_program` through the engine's admission (`readmit_*` from
   the rendered stimulus WAV; declared caps from `excitation_safety_plan`;
   `session_volume_plan` proven via the seat-level reference) → `finish` /
   `abort` → `mint_wired_answer`; return the `PlayedStimulus` the executor
   expects, with the answer's `capture_integrity` and `capture_device` carried
   into the bench evidence. No second recorder, no second admission.
2. Bind `TargetPlan`: in `jasper/cli/bass_extension_bench.py`, build one plan
   per target from the family (`generate_family` for the declared adapter),
   with `graph_raw_text` rendered by the existing emitter path for that target
   and `limiter_name` from `driver_baseline_limiter_name` / the sub's; replace
   the `--live` `SystemExit` with the real binding. Keep the offline (`--dry`)
   path byte-identical.
3. Add `jasper/bass_extension` to `PACKAGE_BOUNDARIES`: may import
   `audio_measurement` and `active_speaker`; may not import `correction`, `web`,
   `cli`. (A T-row: one table entry, one test run.)

Proof: fake-Camilla fixture runs one discovery + one candidate rung end to end
and banks evidence with integrity fields; `--dry` output unchanged (pin);
`ruff`, `mypy` on touched modules, `scripts/test-fast`. Gate: `/code-review`
high **and** `/adversarial-review` (excitation path). The owner's campaign
(row 4.1) is the hardware proof; merge waits for the adversarial review, not
for hardware.

### 3.2 — `bass-fit` view (branch `claude/seat-w3-3-2-bass-fit`)

`jasper-round-views bass-fit <round-dir>` → `bass_fit.json`. Inputs:
`room_median.json` (lane B's contract), the declared cabinet block (adapter id;
declared f0/Q where present), `MarginPolicy`. Steps: smooth the median to 1/3
octave below `ceiling_hz`; wrap it as a `MagnitudeCurve` under a new
`CaptureRole` (e.g. `SEAT_MEDIAN`) and call the adapter's `fit_plant` for the
**effective in-situ** corner and Q (room gain included — that is the point,
per Bank); if the fit refuses on RMS, fall back to the declared f0/Q and
disclose it; `generate_family` for the rung corners (the deepest rung is the
30 Hz-class target); per rung compute `linkwitz_transform_params` from the
effective plant to the rung target, the subsonic high-pass per `MarginPolicy`,
the boost in dB below the corner, its headroom cost, and the excursion margin
from the declared plant model. Publish per rung; answer on stdout with the
effective corner, fit RMS or fallback, the rung count, and the deepest rung's
boost and cost. Register in `ARTIFACT_BY_VIEW` and `_VIEW_RUN`. Proof:
fixture median → family JSON; refusal path disclosed. Gate: `/code-review`.

### 3.3 — The Layer-2 scheduled candidate kind (branch `claude/seat-w3-3-3-bass-candidate`)

1. `bass_extension` field on `MeasuredCrossoverCandidate` (add to
   `_OPTIONAL_FIELD_TYPES`; validate in `__post_init__`; fingerprinted):
   `{ "owner": {"role", "channels"}, "adapter_id", "effective_plant": {f0_hz, q0,
   source}, "rungs": [ {"rung_id", "max_level_db", "lt": {...camilla dict...},
   "subsonic": {...}, "boost_db", "level_cost_db", "protection": {"ladder_evidence"
   | null, "limiter_evidence" | null}} ], "basis": {"round_id", "bass_fit_sha256"} }`.
   Keyed by listening level: each rung names the maximum level at which it
   may play.
2. Door `jasper/active_speaker/crossover_v2/bass_prescription.py`, modeled on
   the blend door: the LLM chooses the margin policy and which rungs to adopt
   from `bass_fit.json`; the door validates against the family and refuses
   anything the fit did not offer. **Admission rule (hard stop):** a rung with
   boost > 0 is admissible only with both protection evidences attached
   (3.4's ladder evidence for its level; the limiter evidence bundle once 4.1
   exists). Until then only the natural rung (boost 0) is admissible — the
   "natural at rest" emission wave 3 already ships.
3. Emission: `_bass_extension_emission` consumes the candidate's field instead
   of a `BassExtensionProfile`; the `sealed_v1`-only check becomes a check on
   the adapter's emission support (sealed today; ported/PR refuse with a code
   until their emission exists); `_assert_bass_extension_safe` unchanged; the
   `/state.bass_extension` summary reads the applied candidate. `profile.py`
   is absorbed: delete what the candidate field now owns, with verdicts, in the
   same PR.
4. `jasper-crossover-prescriber` gains the `kind` branch.

Proof: door refusals parametrized (rung not in family, boost without evidence,
owner mismatch); fixture candidate emits through a fake Camilla and
`_assert_bass_extension_safe` passes; `/state` summary reads back. Gate:
`/code-review` high; the emitter change is DSP on the output path →
`/adversarial-review`.

### 3.4 — Protection ladder as a program (branch `claude/seat-w3-3-4-protection-ladder`) — **NN**

A code-owned measurement program `bass/ladder`: at the seat (head centre pose,
`kind=seat`), for a named rung, stepped-level summed sweeps from the seat-level
reference upward in `rung_step_db` steps to the rung's `max_level_db`, then the
sustain test (band-limited noise, duration per the plan), each step captured
through the wired kernel with the rung's graph active (scope `room_candidate`
from lane C or a `bass_candidate` sibling — coordinate; same compose function).
Per step bank `read_segment_distortion` per order with its contamination
disclosure; the step **fails** when a clean order's distortion rises by more
than the ladder's threshold over the previous step or the capture is not
intact; a failing step marks the rung inadmissible at and above that level,
banked as `ladder_evidence` the door (3.3) requires. Thresholds are constants
with a pointer to the plan's §8 and ADR-0260, not knobs. The excitation caps
and the commissioning SPL stop apply unchanged; the human starts each level.
Proof: fixture ladder with a synthetic distortion rise fails at the right step;
refusal codes; the door admits a rung only with passing evidence. Gate:
`/code-review` high and `/adversarial-review` (hardware damage tier); the first
real ladder is owner-present hardware time, scheduled by the plan's §6.

### 3.5 — Docs (branch `claude/seat-w3-3-5-bass-docs`)

Replace the methodology "Bass" stub with one section (the family, the seat
median fit, the ladder, what is admissible when); runbook rows for `bass-fit`,
the bass prescription kind, `bass/ladder`; regenerate the menu. Update
`docs/bass-extension-waves/README.md`'s status table for waves 3–5 as rebased
here. Gate: sanity look.

## 3. Rules that bind this lane

- Standing rules in the kickoff snippet and plan §7.
- Do not weaken the limiter-evidence protocol or the tap realization rules;
  do not delete `ladder.py`, the `__init__` apply pathway or their tests (lane A
  does, with verdicts); do not touch lane B's views or lane C's room door
  beyond the shared `kind` dispatch in the prescriber.
- Hard stops stay code-owned: excitation caps, the ladder's fail rule, the
  admission rule in the door. The LLM chooses corners and margins; it never
  chooses how loud a rung may play.
- No nearfield rung; a close-mic capture remains an optional diagnostic via
  `round-views close-reference`.

## 4. Report back

Per PR: link, line delta, what was deleted with verdicts (3.3), the
admission-rule refusal codes, the ladder's fail-rule constants, validation
sentinels including the adversarial-review record, "stale, not fixed here",
and any premise in §1 found false at HEAD with what you did instead.
