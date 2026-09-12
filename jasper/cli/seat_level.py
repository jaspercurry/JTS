# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Level once at the session mark, bank the gain, and restore household playback.

**Why the stimulus is derived, not designed here.** A settled-window SPL read
needs a CONTINUOUS signal, and a session's own programs are silence-separated
bursts and sweeps — so "point it at the program you will measure with" named a
class of file that structurally cannot work, and every operator substituted an
ad-hoc WAV nothing could later identify. The default is now synthesized from
declarations that already exist (:func:`default_stimulus_wav` states which);
``--stimulus-wav`` remains the override. Either way its true peak is read from
the bytes, each driver's branch peak is rendered from those same bytes through
the graph that is actually applied, and the ceiling is solved so no branch
reaches full scale at any commanded volume. When that render cannot be exact —
no applied graph, a filter type the renderer does not model, a stimulus past
the render bound — the ceiling falls back to bounding every branch by the
full-band peak, which is the conservative answer this verb shipped with.

**The declared per-driver level caps do not hold this volume down** — a
published one included. A per-driver level limit binds that driver, at
admission and in a composed program's segment gain; it cannot be enforced on a
single signal that carries no per-driver gain, so the ceiling here is digital
headroom and the caps are named beside it on
``event=active_speaker.unsegmented_ceiling_bound`` — what each driver receives
at this ceiling, and how far past its declared figure that lands (owner ruling,
2026-08-23). What still stops the climb: full scale, the graph's limiters, and —
live, on measured samples — the profile's ``max_commissioning_level_db_spl``.

**Precondition an operator must check.** The mic's ``Sens Factor`` is quoted at
its maximum capture volume. Confirm ``amixer -c <card>`` shows the capture
control at 100% before trusting any absolute SPL this prints.

"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, NamedTuple

from jasper.active_speaker.auto_level import SETTLE_TIMEOUT_S, START_FADER_DB, LevelResult, level_to
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL, DEFAULT_TOLERANCE_DB, SeatLevelTarget, SeatLevelTargetError,
    StimulusProvenance, seat_level_reference_state_path, write_seat_level_reference,
)
from jasper.active_speaker.commission_wiring import CommissionPresetResolutionError
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH, FaderVolumeDoor, SessionVolumePlan, SessionVolumePlanError, SessionVolumeOpenResult,
    SessionVolumeRestoreResult, live_measurement_session, unsegmented_stimulus_ceiling_db,
)
from jasper.active_speaker.restore_wait import await_restore_task_resilient, resilient_restore
from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL, REFUSE_MIC_CALIBRATION_UNAVAILABLE, resolve_mic_sensitivity,
)
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.wired_capture import WiredCaptureError, WiredSplMonitor
from jasper.audio_measurement.ramp import MAX_STEP_DB
from jasper.env_load import bounded_env_float
from jasper.log_event import log_event
from jasper.measurement_window import MeasurementWindowError, measurement_window
from ._logging import CLI_LOG_FORMAT
from ._refusal import EXIT_OK, EXIT_REFUSED, failed

logger = logging.getLogger(__name__)
REFUSE_MIC_ABSENT = "measurement_mic_absent"
REFUSE_TARGET_REJECTED = "seat_spl_target_rejected"
# The slug is unchanged on purpose: the ceiling no longer BINDS on the driver
# caps, but resolving them is still what can fail here (the same call resolves
# each driver's permitted band), and it is a stable operator-facing string.
REFUSE_CEILING_UNDERIVABLE = "driver_cap_ceiling_underivable"
REFUSE_STIMULUS_MISSING = "stimulus_wav_missing"
REFUSE_INTERRUPTED = "seat_level_interrupted"
#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "measured"


def _refused(reason: str, detail: str, *, restored: bool | None = None) -> tuple[dict[str, Any], str]:
    log_event(
        logger,
        "active_speaker.seat_level_result",
        status="refused",
        reason=reason,
        gain_db=None,
        leveled_db_spl=None,
        ambient_db_spl=None,
        readings=None,
    )
    return {"status": "refused", "reason": reason, "restored": restored}, detail


def stimulus_provenance(path: Path, *, band_hz: tuple[float, float] | None=None) -> StimulusProvenance:
    """Which stimulus WAV this is, and what it measures — from ONE read.

    The identity and both levels come out of the same bytes, because a second
    read is a second answer the day the path is a symlink somebody swapped —
    and telling those two files apart is the whole reason the sha is recorded.

    The PEAK bounds the volume ceiling (``unsegmented_stimulus_ceiling_db``:
    full scale less the peak). The RMS is what the seat actually hears at a
    given volume. Their difference is the crest factor, and it is the number
    that decides whether a target is reachable at all. Crest is a property of
    the program (band, length and draw), so it is MEASURED here rather than
    assumed; to read the size of the effect, one 20 s 150-8000 Hz noise draw
    measured ~14 dB of crest, so peak-normalized to -20 dBFS it sits at
    ~-34 dBFS RMS and reaches the seat 19 dB quieter than the same draw
    peak-normalized to -1 dBFS — at a fader that cannot go above 0 dB, that is
    19 dB of target simply out of reach.

    The peak is the max over the whole interleaved array — deliberately NOT a
    downmix. ``sweep.read_wav_mono`` averages channels, which halves the peak
    of a program whose stimulus sits on one channel while the other is silent,
    and an under-reported peak would RAISE the derived volume ceiling. This
    reads the worst case instead, which is the only direction that is safe.

    The RMS is over the same whole array and is therefore a DIGITAL level, not
    an acoustic one: on a program whose stimulus sits on one channel it counts
    the silent channel too, which understates what one driver receives. It
    bounds nothing — it is disclosure — so the conservative direction does not
    apply and the honest one (what the file as a whole measures) does.

    ``band_hz`` is the band a GENERATED default was synthesized over, passed in
    rather than estimated from the samples: it is a declaration, and a measured
    approximation of it would be a second, disagreeing answer.

    A peak of zero raises: a silent file would derive an absurdly high ceiling.
    """
    import hashlib
    import io
    import numpy as np
    from scipy.io import wavfile
    raw = path.read_bytes()
    _rate, data = wavfile.read(io.BytesIO(raw))
    samples = np.asarray(data).astype(np.float64)
    full_scale = float(np.iinfo(np.asarray(data).dtype).max) if np.issubdtype(np.asarray(data).dtype, np.integer) else 1.0
    peak = float(np.abs(samples).max()) / full_scale if samples.size else 0.0
    if not peak > 0.0 or not math.isfinite(peak):
        raise ValueError(f'{path} carries no signal; a silent stimulus cannot bound a volume')
    rms = float(np.sqrt(np.mean((samples / full_scale) ** 2)))
    return StimulusProvenance(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), peak_dbfs=20.0 * math.log10(peak), rms_dbfs=20.0 * math.log10(rms), band_hz=band_hz)

class _Declarations(NamedTuple):
    """One load of the topology and design draft, shared by the whole pass."""
    topology: Any
    draft: dict[str, Any]
    safety_profile: dict[str, Any]

def _load_declarations(args: argparse.Namespace) -> _Declarations:
    """Load ONCE what both derivations below read.

    The default-stimulus band and the volume ceiling both come off the same
    topology and design draft; loading per consumer would do the draft's
    derived-field stamping twice and give the missing-profile refusal two
    homes to drift between.
    """
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.output_topology import load_output_topology_strict
    topology = load_output_topology_strict(args.topology)
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get('driver_safety_profile')
    if not isinstance(safety_profile, dict):
        raise SessionVolumePlanError('the design draft carries no driver_safety_profile; commission the drivers before leveling')
    return _Declarations(topology, draft, safety_profile)

def default_stimulus_wav(declarations: _Declarations) -> tuple[Path, tuple[float, float]]:
    """Synthesize the default stimulus, and say which band it covers.

    Every parameter is read off a declaration this box already carries, so
    nothing here is a property of one rig, one room, or one operator's home
    directory:

    * the BAND is the hull of the drivers' declared ``measurement_band_hz``,
      clamped to the global driver-test limits and to Nyquist. The hull and not
      the intersection: this is ONE unsegmented signal that the applied
      crossover splits, so it has to cover every driver's declared window, and
      a two-way's two windows can fail to overlap at all;
    * the LEVEL is the level driver-capture excitation already runs at, so a
      reference banked against the default sits at the same digital level as
      the programs the session goes on to measure with;
    * the DURATION is the branch-peak render bound — the longest stimulus whose
      per-branch peak solve stays EXACT. Past it the ceiling silently falls
      back to the conservative full-band bound.

    It is cached under the installer-registered stimulus directory
    (``speech_stimulus.DEFAULT_CACHE_DIR``, created by ``deploy/install.sh``),
    so the file an operator is asked about is discoverable rather than an
    unbanked path in somebody's home directory.
    """
    from jasper.active_speaker.branch_peak import MAX_STIMULUS_SAMPLES
    from jasper.active_speaker.commissioning_admission import ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS
    from jasper.active_speaker.excitation_safety_plan import resolve_driver_measurement_band_hz
    from jasper.active_speaker.measurement import active_driver_targets
    from jasper.active_speaker.speech_stimulus import DEFAULT_CACHE_DIR
    from jasper.active_speaker.test_signal_plan import MAX_DRIVER_TEST_FREQUENCY_HZ, MIN_DRIVER_TEST_FREQUENCY_HZ
    from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav
    from jasper.audio_measurement.program import PROGRAM_SAMPLE_RATE_HZ
    bands = [resolve_driver_measurement_band_hz(declarations.safety_profile, str(target['target_fingerprint'])) for target in active_driver_targets(declarations.topology)]
    if not bands:
        raise SessionVolumePlanError('this topology declares no active driver targets, so no stimulus band can be derived; name one with --stimulus-wav')
    f_lo = max(MIN_DRIVER_TEST_FREQUENCY_HZ, min((lo for lo, _hi in bands)))
    f_hi = min(MAX_DRIVER_TEST_FREQUENCY_HZ, PROGRAM_SAMPLE_RATE_HZ / 2.0 - 1.0, max((hi for _lo, hi in bands)))
    band = (float(f_lo), float(f_hi))
    return (ensure_bandlimited_noise_wav(f_lo_hz=band[0], f_hi_hz=band[1], duration_s=MAX_STIMULUS_SAMPLES / PROGRAM_SAMPLE_RATE_HZ, dbfs=ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS, sample_rate=PROGRAM_SAMPLE_RATE_HZ, cache_dir=DEFAULT_CACHE_DIR), band)

def _applied_branch_peaks(stimulus: Path, targets: list[dict[str, Any]]) -> dict[str, float] | None:
    """Each driver's branch true peak for THIS stimulus through the LIVE graph.

    ``None`` whenever the render cannot be exact, which the ceiling derivation
    turns back into the conservative full-band bound — so every failure here
    makes the speaker quieter, never louder. The reason is logged rather than
    swallowed: a silent fallback looks identical to a genuinely tight graph and
    sends an operator hunting the wrong number.

    The applied graph is read through
    :func:`jasper.active_speaker.environment.read_camilla_statefile_config_path`,
    the same public statefile reader every other surface uses, so this adds no
    second answer to "which config is live".
    """
    import yaml
    from jasper.active_speaker.branch_peak import BranchPeakError, branch_peaks_for_targets
    from jasper.active_speaker.environment import read_camilla_statefile_config_path
    try:
        config_path = read_camilla_statefile_config_path()
        if not config_path:
            raise BranchPeakError('no CamillaDSP statefile names an applied config')
        config = yaml.safe_load(Path(config_path).read_text(encoding='utf-8'))
        peaks = branch_peaks_for_targets(config, stimulus, targets)
    except (BranchPeakError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        log_event(logger, 'active_speaker.seat_level_branch_peaks_unavailable', detail=str(exc))
        return None
    log_event(logger, 'active_speaker.seat_level_branch_peaks', peaks=' '.join((f'{key}={value:.2f}' for key, value in sorted(peaks.items()))))
    return peaks

def _derive_bounds(stimulus: Path, levels: StimulusProvenance, declarations: _Declarations) -> tuple[float, float]:
    """``(volume ceiling for THIS stimulus, commissioning SPL ceiling)``.

    ``levels`` is measured once by the caller rather than read again here (the
    ramp needs the same numbers for its refusal, and two reads of one file are
    two answers), and ``declarations`` is loaded once by the caller for the
    same reason.
    """
    from jasper.active_speaker.commission_wiring import commissioning_spl_ceiling_db
    from jasper.active_speaker.design_draft import declared_effective_driver_sensitivities
    from jasper.active_speaker.measurement import active_driver_targets
    topology, draft, safety_profile = declarations
    targets = active_driver_targets(topology)
    fingerprints = [str(target['target_fingerprint']) for target in targets]
    # The PAD-FOLDED sensitivities, not the naked datasheet ones. An L-pad'd
    # tweeter's acoustic output is quieter than its bare rating by exactly the
    # pad, and the derived HF ceiling is a sensitivity DELTA against the woofer
    # — so reading the naked figure protects the driver as if it were the pad's
    # worth more sensitive than it physically is. This is the reader
    # ``declared_driver_sensitivities``' own docstring names for
    # excitation-ceiling derivation and session-volume planning (#1665), and the
    # one the crossover-v2 flow already passes.
    ceiling_db = unsegmented_stimulus_ceiling_db(safety_profile, fingerprints, stimulus_peak_dbfs=levels.peak_dbfs, declared_sensitivities=declared_effective_driver_sensitivities(draft), branch_peaks_dbfs=_applied_branch_peaks(stimulus, targets))
    return (ceiling_db, commissioning_spl_ceiling_db(topology))

class _OperatorStopped(Exception):
    """SIGINT arrived while the pass was running, and the pass has torn down."""
    pass


async def _stoppable(pass_coro: Any) -> LevelResult:
    """Run the leveling pass with SIGINT wired to its own cancellation.

    Stopping must be possible at ANY moment, and it must stop the stimulus and
    give the household its volume back — which is the pass's own teardown, not
    a second one here.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(pass_coro)
    stopped = False

    def _stop() -> None:
        nonlocal stopped
        stopped = True
        task.cancel()
    handled = True
    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
    except (NotImplementedError, RuntimeError, ValueError):
        handled = False
    try:
        return await task
    except asyncio.CancelledError:
        if stopped:
            raise _OperatorStopped() from None
        raise
    except KeyboardInterrupt:
        # Reached only when no handler could be installed (a loop that is not
        # the main thread's): the interpreter raises inside the running
        # coroutine, so the pass's teardown has already run and stamped it.
        raise _OperatorStopped() from None
    finally:
        if handled:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGINT)

async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    from jasper.audio_measurement.correction_lane import exec_correction_play
    from jasper.audio_measurement.wired_capture import (
        resolve_wired_mic,
    )
    from jasper.audio_measurement.wired_level_meter import WiredLevelMeter
    from jasper.camilla import primary_controller

    if args.stimulus_wav is not None and not Path(args.stimulus_wav).is_file():
        return _refused(
            REFUSE_STIMULUS_MISSING,
            f"no such stimulus WAV: {args.stimulus_wav}. Omit --stimulus-wav "
            "and the verb generates its own from the drivers' declared "
            "measurement bands",
        )

    mic = resolve_wired_mic()
    if mic is None:
        return _refused(
            REFUSE_MIC_ABSENT,
            "no measurement-class capture card is present; plug the mic in",
        )
    sensitivity = (
        resolve_mic_sensitivity(
            calibration_file=args.calibration_file, mic_serial=args.mic_serial,
            mic_provider=args.mic_provider, mic_model=args.mic_model,
        ) if args.calibration_file or args.mic_serial
        else resolved_household_sensitivity(mic)
    )
    if sensitivity is None:
        return _refused(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL
        )

    try:
        declarations = _load_declarations(args)
        stimulus, band_hz = (
            (Path(args.stimulus_wav), None)
            if args.stimulus_wav is not None
            else default_stimulus_wav(declarations)
        )
        provenance = stimulus_provenance(stimulus, band_hz=band_hz)
        ceiling_db, spl_ceiling = _derive_bounds(stimulus, provenance, declarations)
    except (
        SessionVolumePlanError,
        CommissionPresetResolutionError,
        ActiveSpeakerConfigError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        return _refused(REFUSE_CEILING_UNDERIVABLE, str(exc))

    target = SeatLevelTarget(
        target_db_spl=args.target_db_spl, tolerance_db=args.tolerance_db
    )
    try:
        target.validate(ceiling_db_spl=spl_ceiling)
    except SeatLevelTargetError as exc:
        return _refused(REFUSE_TARGET_REJECTED, str(exc))

    cam = primary_controller()
    monitor = WiredSplMonitor(sensitivity, spl_ceiling, 0)
    meter = WiredLevelMeter(mic.pcm, channels=args.mic_channels, spl_monitor=monitor)
    player: Any = None

    # #2938: cancellation must survive scheduling and process creation.
    async def _play() -> None:
        async def spawn() -> None:
            nonlocal player
            player = await exec_correction_play(
                stimulus, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        await await_restore_task_resilient(asyncio.create_task(spawn()))

    async def _cancel() -> None:
        nonlocal player
        if player is not None and player.returncode is None:
            player.terminate()
            try:
                await asyncio.wait_for(player.wait(), timeout=2.0)
            except TimeoutError:
                player.kill()
                await player.wait()
        player = None

    async def _samples() -> list[Any]:
        if monitor.error is not None:
            raise monitor.error
        if player is not None and player.returncode is not None:
            if player.returncode != 0:
                raise WiredCaptureError("Leveling stimulus failed")
            await _play()
        return meter.drain()

    busy = live_measurement_session(action="leveling the seat SPL")
    if busy is not None:
        return _refused("measurement_session_already_live", busy)
    plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    door = FaderVolumeDoor(cam.set_volume_db, cam.get_volume_db)
    restored: bool | None = None
    result: LevelResult | None = None

    async def _restore() -> None:
        nonlocal restored
        outcome = await plan.close(door, reason="seat_level_complete")
        restored = outcome in (SessionVolumeRestoreResult.EXACT_RESTORED,
                               SessionVolumeRestoreResult.ALREADY_RESOLVED)

    async def _pass() -> LevelResult:
        async with measurement_window(gate_owner="seat-level"):
            try:
                async with asyncio.timeout(SETTLE_TIMEOUT_S):
                    current = await cam.get_volume_db()
                if current is None or not math.isfinite(current):
                    return LevelResult("refused", "volume_latch_unconfirmed")
                start = min(current, START_FADER_DB, ceiling_db, 0.0)
                settle_s = bounded_env_float("JASPER_SEAT_LEVEL_SETTLE_TIMEOUT_S", SETTLE_TIMEOUT_S, lo=2.0, hi=30.0)
                watchdog_s = (math.ceil((min(ceiling_db, 0.0) - start) / MAX_STEP_DB) + 7) * settle_s
                plan.set_wall_clock_ceiling_s(watchdog_s + 60.0)
                opened = await plan.open(start, door)
                if opened is not SessionVolumeOpenResult.OPENED:
                    return LevelResult("refused", "volume_latch_unconfirmed")
                async with asyncio.timeout(watchdog_s):
                    await asyncio.to_thread(meter.start)
                    return await level_to(
                        target.target_db_spl, tolerance_db=target.tolerance_db,
                        stop_db_spl=spl_ceiling, max_main_volume_db=ceiling_db, sensitivity=sensitivity,
                        get_main_volume_db=cam.get_volume_db, set_main_volume_db=cam.set_volume_db,
                        play=_play, stop_playback=_cancel, next_samples=_samples,
                    )
            finally:
                try:
                    await resilient_restore(_cancel())
                finally:
                    try:
                        await resilient_restore(_restore())
                    finally:
                        await asyncio.to_thread(meter.stop)

    try:
        result = await _stoppable(_pass())
    except TimeoutError:
        return _refused("seat_level_watchdog_expired", "Leveling timed out", restored=restored)
    except _OperatorStopped:
        return _refused(REFUSE_INTERRUPTED, "Stopped by the operator", restored=restored)
    except (WiredCaptureError, MeasurementWindowError, SessionVolumePlanError, OSError, ValueError) as exc:
        return _refused(getattr(exc, "code", "ramp_error"), str(exc), restored=restored)
    log_event(logger, "active_speaker.seat_level_result", status=result.status, reason=result.reason,
              gain_db=result.gain_db, leveled_db_spl=result.leveled_db_spl,
              ambient_db_spl=result.ambient_db_spl, readings=len(result.readings))
    payload = {**asdict(result), "restored": restored,
               "reference_volume_db": result.gain_db, "measured_db_spl": result.leveled_db_spl}
    if result.status == "converged":
        assert result.gain_db is not None and result.leveled_db_spl is not None
        write_seat_level_reference(reference_volume_db=result.gain_db, measured_db_spl=result.leveled_db_spl,
            target=target, sensitivity=sensitivity.to_dict(), max_main_volume_db=ceiling_db, stimulus=provenance)
        detail = f"reference {result.gain_db:.2f} dB measured {result.leveled_db_spl:.1f} dB SPL"
    else:
        reason_spec = REASON_REGISTRY.get(str(result.reason))
        detail = reason_spec.message if reason_spec is not None else str(result.reason)
    return payload, detail


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-seat-level",
        description=(
            "Ramp the measurement volume until a calibrated mic at the seat "
            "reads the target dB SPL and bank it as the crossover session's "
            "measurement reference — PRECONDITION: `amixer -c <card>` shows "
            "the mic's capture control at 100%, where its Sens Factor is "
            "quoted, or every absolute SPL is wrong by the shortfall."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - a reference is already banked for this session and you are\n"
            "    not deliberately re-leveling\n"
            "  - the mic capture control is not confirmed at 100% (see the\n"
            "    PRECONDITION above) -- level first, then re-run this\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-seat-level\n"
            "\n"
            "EXIT CODES\n"
            "  0  converged and banked; stdout carries the reference dB\n"
            "     SPL reached and where it was banked\n"
            "  1  refused -- {status, reason, detail} on stdout under the\n"
            "     reason (interrupted, or the ramp's own refusal\n"
            "     vocabulary). Readings are included in the result.\n"
            "  2  usage error (argparse)"
        ),
    )
    parser.add_argument(
        "--stimulus-wav",
        default=None,
        help="override the generated default with a CONTINUOUS, band-limited "
        "WAV under the branch-peak render bound; omit it and one is "
        "synthesized from the drivers' declared measurement bands",
    )
    parser.add_argument(
        "--target-db-spl",
        type=float,
        default=DEFAULT_TARGET_DB_SPL,
        help=f"seat SPL to converge on (default {DEFAULT_TARGET_DB_SPL:g})",
    )
    parser.add_argument(
        "--tolerance-db",
        type=float,
        default=DEFAULT_TOLERANCE_DB,
        help=f"half-width of the accepted band (default {DEFAULT_TOLERANCE_DB:g})",
    )
    parser.add_argument(
        "--calibration-file",
        help="vendor calibration .txt with 'Sens Factor'; defaults to the household mic",
    )
    parser.add_argument(
        "--mic-serial",
        help="look the stored calibration up by microphone serial instead",
    )
    parser.add_argument("--mic-provider", default="minidsp")
    parser.add_argument("--mic-model", default="minidsp_umik2")
    parser.add_argument(
        "--mic-channels",
        type=int,
        default=1,
        help="capture channel count the mic enumerates (default 1)",
    )
    parser.add_argument("--topology", default=None)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Without this the whole disclosure receipt is computed and discarded: the
    # root logger sits at WARNING, so ``event=active_speaker.unsegmented_ceiling_bound``
    # -- the ONE production reader of the declared caps this ceiling drives past
    # -- reaches no handler. ``basicConfig`` at INFO in ``main`` is what the
    # sibling ``event=``-emitting CLIs do (``crossover_prescriber``,
    # ``arm_walk``, ``sound``, ...), reusing the shared FORMAT so the one place
    # that shape is written down stays the only one. In ``main`` rather than at
    # import, because a module that configures the root logger on import
    # imposes its choice on every importer, the test suite included.
    #
    # ``--verbose`` raises that floor to DEBUG rather than reaching for
    # ``_logging.configure_verbose_logging``, whose no-flag floor is WARNING --
    # the level that would discard the receipt above.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=CLI_LOG_FORMAT)
    try:
        result, detail = asyncio.run(_run(args))
    except KeyboardInterrupt:
        # The last-resort path: the interrupt escaped ``_stoppable`` entirely,
        # so the pass may never have opened the latch. Report only what the
        # exception actually carries -- claiming a restore here is the
        # dishonesty this field exists to prevent.
        result, detail = _refused(REFUSE_INTERRUPTED, "Stopped by the operator")
    if result["status"] != "converged":
        reason = result.pop("reason")
        result.pop("status")
        return failed(EXIT_REFUSED, reason, {**result, "detail": detail})
    print(f"converged: {detail}", file=sys.stderr)
    print(json.dumps({**result, "detail": detail, "out": str(seat_level_reference_state_path())}, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
