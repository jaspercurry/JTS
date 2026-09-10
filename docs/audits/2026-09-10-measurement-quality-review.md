# Shared leveling and measurement-quality audit

Audited SHA: `5fc22ba456bf480b6d875d1828f0f864d29f42c6`.
Re-verification date: 2026-09-10.
Tracking issue: [#4868](https://github.com/jaspercurry/JTS/issues/4868).

This is a scoped source and software-behavior audit of the shared measurement path, not a whole-repository or live acoustic audit. It was performed independently of prior conversations. The supplied Mac worktree was unavailable; a clean worktree was created from freshly fetched GitHub `main`. JTS3's hostname did not resolve, so no live declaration, logs, capture, playback, timing, calibration or deployment was verified.

## Findings at the audited SHA

| Finding | Evidence and consequence | Ledger |
|---|---|---|
| Current acoustic level is not acquired by the shared measurement group | `session_volume_plan.measurement_reference_volume_db` selects a saved reference or −20 dB. `MeasureSpec` has stimulus rungs but no automatic/quiet/series intent and purpose. `TuningSession._proven_level` proves Main readback, not current acoustic level. The standalone leveler owns a separate measurement hold/restoration lifecycle. | [#4863](https://github.com/jaspercurry/JTS/issues/4863) |
| Quality and recovery depend on caller | `cli/measure._measure` gives `CapturedRecordStore` loudness-only enrichment. `web/correction_crossover_v2` attaches `conductor.consume_capture`; UI flow and `capture_dispatch` assess/rearm captures. `TuningSession._record` describes completed capture but does not establish shared band-specific usefulness. | [#4864](https://github.com/jaspercurry/JTS/issues/4864) |
| Absolute SPL and continuous-probe admission lack verified context | `cli/seat_level` leaves the calibration gain prerequisite to a manual amixer check. `WiredLevelMeter` stamps `agc_frozen=True` from the ALSA path. `unsegmented_stimulus_ceiling_db` bounds digital peaks while disclosing, rather than enforcing, declared driver level caps for this probe. | [#4865](https://github.com/jaspercurry/JTS/issues/4865) |
| Matched-comparison context omits loudness compensation | `cli/measure` writes `loudness_volume_db`; `measurement_context.capture_basis` and `CAPTURE_FIELDS` omit it. A difference in volume-dependent DSP compensation can escape the shared comparison test. | [#4866](https://github.com/jaspercurry/JTS/issues/4866) |
| Corrupt samples can be reported as merely quiet | Public `quality.assess_capture` with `[0.1, NaN, 0.1]` returns `failed=False`, peak/RMS −120 dBFS and low-level warnings. `dbfs` maps the nonfinite statistics to its finite display floor. This is an integrity failure, not evidence to raise playback. | [#4867](https://github.com/jaspercurry/JTS/issues/4867) |

These findings concern false or missing measurement claims, not a demand to stop safe experiments merely because their results are uncertain. A captured WAV can contain useful midband evidence and unusable bass evidence simultaneously. A declared quiet or nonlinear experiment is not inherently a failure.

## Mechanisms that already exist

`seat_level_ramp._walk_to_the_band` starts a continuous tone and uses measured target error with bounded upward corrections. Its start is −50 dB; positive bites are 15% of the start-to-ceiling span, and the same pass can make a full correction when the remaining gap fits. Each settled reading uses adjacent 0.5-second windows; two qualifying readings are required before banking. It includes a measured silent floor, live recorder/SPL stops, observability checks, bounded time/readings, cancellation and restoration. It is not a stop/play/ramp cycle for every step.

The ramp permits a separate silent remeasurement after a reading contradicts the initial ambient floor. Source inspection found that CLI `cancel_requested` remains true after tone cancellation, so the subsequent player binding also requests termination. This is a caller-specific restart risk within #4863, distinct from the core ramp's tested fake-player restart behavior; no live reproduction is claimed.

`SessionVolumePlan` and the existing volume door already own apply/hold/prove/restore and durable recovery. `measurement_door` brackets a group; `TuningSession` can keep its volume claim across multiple specs and positions, proving it before each stimulus. Therefore repeated household restoration between rounds is not required by the engine itself. Integration should respect these existing boundaries rather than introduce a second writer.

`snr_policy` already supplies same-domain spectral integration, per-band coverage and decision-specific SNR verdicts. `quality_model` owns thresholds. Program CHECK/response analysis includes ambient/pilot evidence and rejects unavailable frequency support. `capture_dispatch`, `refusal_copy` and admission already own recovery classification and budgets. The architectural gap is shared coordination and consistent invocation, not an absence of every underlying mechanism.

`capture_geometry` includes Main, stimulus and commissioning gain in level locks. `measurement_context` distinguishes intended candidate-graph changes from incompatible capture conditions. These are reusable comparison responsibilities. Source identity alone is insufficient if separately applied gain or DSP compensation differs.

## Coverage and exclusions

| Area inspected | Files or owners read |
|---|---|
| Rules and entry contract | `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, measurement-loop doctrine, tuning operator runbook, ADR-0284 |
| Level and volume | `seat_level_ramp.py`, `seat_level_reference.py`, `session_volume_plan.py`, `volume_latch.py`, `cli/seat_level.py` |
| Shared measurement path | `crossover_v2/measure_spec.py`, `door.py`, `session.py`, `session_seams.py`, `composition.py`, `wired_stimulus.py`, `cli/measure.py` |
| Recording analysis | `audio_measurement/quality.py`, `quality_model.py`, `snr_policy.py`, program CHECK/response analysis, `wired_level_meter.py`, `calibration.py`, noise generation in `audio_measurement/playback.py` |
| Comparison and retries | `capture_geometry.py`, `crossover_v2/measurement_context.py`, `capture_dispatch.py`, `refusal_copy.py`, admission and the wired UI/conductor integration |
| Behavioral evidence | Existing seat-level, CLI, session-volume and band-SNR tests; direct public-quality reproduction; deterministic old/new leveler harness |

Reads followed relevant functions and callers; this report does not claim every line of every large UI module was audited. Unrelated voice/rendering implementations, amplifier electronics, driver mechanical state and all live Pi state were excluded. No hardware model was inferred from a fixture, README example or previous conversation.

The focused pre-change suite passed 296 tests in 12.05 seconds. Deterministic fixtures reproduced the existing controller's differing costs for linear and slowly settling responses. Virtual time is not JTS3 time, and no measured before/after acoustic speedup exists in this audit. Research, synthetic results, receipt semantics and the resulting limited implementation decision are recorded in [ADR-0289](../adr/0289-leveling-performance-requires-observed-evidence.md). Raw diagnostic trails are kept out of the repository under ADR-0284; the tracking issue owns subsequent evidence and work.
