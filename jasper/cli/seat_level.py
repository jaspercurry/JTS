# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Level with the room/bass summed sweep and the watch's loudest window.

The jts3 noise/sweep mismatch is recorded in ADR-0308. Each sweep now reads
``max_window_db_spl``, the same statistic stamped on measurement takes.
The mic's ``Sens Factor`` is quoted at its maximum capture volume.
Confirm ``amixer -c <card>`` shows the capture control at 100% before trusting
any absolute SPL this prints.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jasper.active_speaker.auto_level import VOLUME_CONFIRM_TIMEOUT_S, START_FADER_DB, LevelResult, level_to
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.bundles import mark_state, open_bundle
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore
from jasper.active_speaker.commission_wiring import commissioning_spl_ceiling_db
from jasper.active_speaker.crossover_v2.conductor_context import conductor_status, resolve_conductor_context
from jasper.active_speaker.crossover_v2.door import bind_measurement_graph, set_measurement_loudness
from jasper.active_speaker.crossover_v2.programs import SessionExcitation
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
from jasper.active_speaker.seat_level_sweep import SweepLevelReader, watchdog_seconds
from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
from jasper.active_speaker.volume_latch import read_fader_db
from jasper.camilla import primary_controller
from jasper.output_topology import load_output_topology_strict
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL, DEFAULT_TOLERANCE_DB, SeatLevelTarget, SeatLevelTargetError,
    seat_level_reference_state_path, write_seat_level_reference,
)
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH, FaderVolumeDoor, SessionVolumePlan, SessionVolumeOpenResult,
    SessionVolumeRestoreResult, live_measurement_session,
)
from jasper.active_speaker.restore_wait import resilient_restore
from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL, REFUSE_MIC_CALIBRATION_UNAVAILABLE, resolve_mic_sensitivity,
)
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.ramp import HARD_CEILING_DBFS
from jasper.audio_measurement.wired_capture import WiredSplMonitor, resolve_wired_mic
from jasper.log_event import log_event
from jasper.measurement_window import measurement_window
from ._logging import CLI_LOG_FORMAT
from ._refusal import EXIT_OK, EXIT_REFUSED, failed

logger = logging.getLogger(__name__)
REFUSE_MIC_ABSENT = "measurement_mic_absent"
REFUSE_TARGET_REJECTED = "seat_spl_target_rejected"
REFUSE_CEILING_UNDERIVABLE = "driver_cap_ceiling_underivable"
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
    explicit_calibration = bool(args.calibration_file or args.mic_serial)
    sensitivity = resolve_mic_sensitivity(
        calibration_file=args.calibration_file, mic_serial=args.mic_serial,
        mic_provider=args.mic_provider, mic_model=args.mic_model,
    ) if explicit_calibration else None
    if explicit_calibration and sensitivity is None:
        return _refused(REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL)
    mic = resolve_wired_mic()
    if mic is None:
        return _refused(
            REFUSE_MIC_ABSENT,
            "no measurement-class capture card is present; plug the mic in",
        )
    if not explicit_calibration:
        sensitivity = resolved_household_sensitivity(mic)
    if sensitivity is None:
        return _refused(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL
        )

    try:
        context = resolve_conductor_context(
            conductor_status(), topology=load_output_topology_strict(args.topology),
            require_banked_level=False,
        )
        spl_ceiling = commissioning_spl_ceiling_db(context.topology, preset=context.preset)
        candidate = candidate_from_applied_profile(
            context.topology, load_applied_baseline_profile_state() or {},
        )
        profile = MeasurementGraphProfile(
            preset=context.preset, topology=context.topology, role_channels=context.role_channels,
            playback_device=context.playback_device,
            protection_sections_by_role=confirmed_protection_sections(context.safety_profile, context.role_targets),
        )
    except (OSError, RuntimeError, ValueError, LookupError) as exc:
        code = getattr(exc, "code", REFUSE_CEILING_UNDERIVABLE)
        if code == "composition_saved_tune_unavailable":
            code = "applied_baseline_snapshot_unavailable"
        return _refused(code, str(exc))
    # Driver caps bind each composed segment through back_off_gain and live re-admission.
    # The fader itself is bounded only by digital full scale.
    ceiling_db = HARD_CEILING_DBFS

    target = SeatLevelTarget(
        target_db_spl=args.target_db_spl, tolerance_db=args.tolerance_db
    )
    try:
        target.validate(ceiling_db_spl=spl_ceiling)
    except SeatLevelTargetError as exc:
        return _refused(REFUSE_TARGET_REJECTED, str(exc))

    cam = primary_controller()
    monitor = WiredSplMonitor(sensitivity, spl_ceiling, 0)
    busy = live_measurement_session(action="leveling the seat SPL")
    if busy is not None:
        return _refused("measurement_session_already_live", busy)
    plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    door = FaderVolumeDoor(cam.set_volume_db, cam.get_volume_db)
    restored: bool | None = None
    loudness_entry: float | None = None
    reader: SweepLevelReader | None = None
    bundle_dir: Path | None = None
    graph = bind_measurement_graph(
        profile, camilla_factory=lambda: cam, config_dir=DEFAULT_CAMILLA_CONFIG_DIR, candidate=candidate,
    )

    async def _restore() -> None:
        nonlocal restored
        try:
            if loudness_entry is not None:
                await set_measurement_loudness(cam, loudness_entry)
        finally:
            outcome = await plan.close(door, reason="seat_level_complete")
            restored = outcome in (SessionVolumeRestoreResult.EXACT_RESTORED,
                                   SessionVolumeRestoreResult.ALREADY_RESOLVED)

    async def _pass() -> LevelResult:
        nonlocal reader, bundle_dir, loudness_entry
        async with measurement_window(gate_owner="seat-level"):
            try:
                async with asyncio.timeout(VOLUME_CONFIRM_TIMEOUT_S):
                    current = await cam.get_volume_db()
                    loudness_entry = await read_fader_db(cam.get_loudness_volume_db)
                if current is None or not math.isfinite(current) or loudness_entry is None or not math.isfinite(loudness_entry):
                    return LevelResult("refused", "volume_latch_unconfirmed")
                start = min(current, START_FADER_DB, ceiling_db, 0.0)
                excitation = SessionExcitation(
                    roles=context.roles_bands, caps_dbfs=context.driver_caps_dbfs,
                    session_volume_db=start, fc_hz=context.fc_hz,
                    sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
                )
                program = excitation.verify_program(leading_pilots=False)
                watchdog_s = watchdog_seconds(start, ceiling_db, program.total_samples / program.sample_rate_hz)
                plan.set_wall_clock_ceiling_s(watchdog_s + 60.0)
                opened = await plan.open(start, door)
                if opened is not SessionVolumeOpenResult.OPENED:
                    return LevelResult("refused", "volume_latch_unconfirmed")
                async with asyncio.timeout(watchdog_s):
                    await graph.install()
                    info = open_bundle(context.topology, calibration_id="")
                    if info is None:
                        raise RuntimeError("Could not open a commissioning evidence bundle")
                    bundle_dir = Path(info["bundle_dir"])
                    store = CommissioningEvidenceStore.open(bundle_dir, expected_session_id=info["session_id"])
                    reader = SweepLevelReader(
                        excitation=excitation, candidate=candidate, graph=graph, cam=cam,
                        plan=plan, store=store, context=context, device=mic, monitor=monitor,
                        config_dir=str(DEFAULT_CAMILLA_CONFIG_DIR), bundle_id=info["session_id"],
                    )
                    return await level_to(
                        target.target_db_spl, tolerance_db=target.tolerance_db,
                        stop_db_spl=spl_ceiling, max_main_volume_db=ceiling_db, sensitivity=sensitivity,
                        get_main_volume_db=cam.get_volume_db, set_main_volume_db=cam.set_volume_db,
                        read_level=reader.read_level, read_ambient=reader.read_ambient,
                    )
            finally:
                try:
                    await resilient_restore(graph.restore())
                finally:
                    try:
                        await resilient_restore(_restore())
                    finally:
                        if bundle_dir is not None:
                            mark_state(bundle_dir, "closed")
                            for audio in bundle_dir.rglob("*.wav"):
                                audio.unlink(missing_ok=True)

    try:
        result = await _stoppable(_pass())
    except TimeoutError:
        return _refused("seat_level_watchdog_expired", "Leveling timed out", restored=restored)
    except _OperatorStopped:
        return _refused(REFUSE_INTERRUPTED, "Stopped by the operator", restored=restored)
    except (OSError, RuntimeError, ValueError) as exc:
        return _refused(getattr(exc, "code", getattr(exc, "reason", "ramp_error")), str(exc), restored=restored)
    log_event(logger, "active_speaker.seat_level_result", status=result.status, reason=result.reason,
              gain_db=result.gain_db, leveled_db_spl=result.leveled_db_spl,
              ambient_db_spl=result.ambient_db_spl, readings=len(result.readings))
    payload = {**asdict(result), "restored": restored,
               "reference_volume_db": result.gain_db, "measured_db_spl": result.leveled_db_spl}
    if result.status == "converged":
        assert result.gain_db is not None and result.leveled_db_spl is not None
        assert reader is not None and reader.provenance is not None
        write_seat_level_reference(reference_volume_db=result.gain_db, measured_db_spl=result.leveled_db_spl,
            target=target, sensitivity=sensitivity.to_dict(), max_main_volume_db=ceiling_db, stimulus=reader.provenance)
        detail = f"reference {result.gain_db:.2f} dB measured {result.leveled_db_spl:.1f} dB SPL"
    else:
        reason_spec = REASON_REGISTRY.get(str(result.reason))
        detail = reason_spec.message if reason_spec is not None else str(result.reason)
    return payload, detail


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-seat-level",
        description=(
            "Play the room/bass summed measurement sweep and adjust the fader "
            "until the calibrated mic's loudest window (max_window_db_spl) "
            "reads the target; bank the session gain. "
            "PRECONDITION: `amixer -c <card>` shows "
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
    parser.add_argument("--topology", default=None)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
