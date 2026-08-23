# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for calibrated seat-SPL leveling.

Answers one question on real hardware: *what main volume makes this speaker
measure the operator's target dB SPL at the listening seat?* — and banks the
answer as the crossover session's measurement reference
(:mod:`jasper.active_speaker.seat_level_reference`), replacing the codified
-20 dB guess.

This module is wiring only. Every decision it makes belongs to someone else:

* the ramp, its guards, and the refusal codes — :mod:`jasper.active_speaker.seat_level_ramp`
* the volume ceiling — ``session_volume_plan.unsegmented_stimulus_ceiling_db``,
  the digital headroom THIS stimulus still has in each driver's own branch of
  the live graph
* those branch peaks — :mod:`jasper.active_speaker.branch_peak`, which renders
  the stimulus through the applied CamillaDSP graph
* the SPL ceiling — the profile's ``max_commissioning_level_db_spl``
* the absolute level reference — the mic's own calibration file
* the mic feed — :class:`jasper.audio_measurement.wired_level_meter.WiredLevelMeter`
* the stimulus — an operator-named WAV, played on the correction lane

**Why the stimulus is named, not designed here.** Choosing a safe excitation for
a summed, seat-position measurement is the excitation-admission subsystem's job,
not a CLI's. Point ``--stimulus-wav`` at the program this session will actually
measure with; its true peak is read from the bytes, each driver's branch peak is
rendered from those same bytes through the graph that is actually applied, and
the ceiling is solved so no branch reaches full scale at any commanded volume.
When that render cannot be exact — no applied graph, a filter type the renderer
does not model — the ceiling falls back to bounding every branch by the
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

Usage::

    jasper-seat-level --stimulus-wav /var/lib/jasper/.../check.wav \\
        --mic-serial 810-8494

Exit 0 only on a converged, banked reference; 1 on any refusal, with the
``REFUSE_*`` reason on stdout and in the ``event=`` line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from jasper.log_event import log_event
from jasper.active_speaker.seat_level_ramp import (
    SeatLevelRampError,
    SeatLevelResult,
    run_seat_level_ramp,
)
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL,
    DEFAULT_TOLERANCE_DB,
    SeatLevelTarget,
    SeatLevelTargetError,
)
from jasper.active_speaker import ActiveSpeakerConfigError
from jasper.active_speaker.commission_wiring import CommissionPresetResolutionError
from jasper.active_speaker.session_volume_plan import (
    SessionVolumePlanError,
    unsegmented_stimulus_ceiling_db,
)

from ._logging import CLI_LOG_FORMAT

logger = logging.getLogger(__name__)

REFUSE_MIC_CALIBRATION_UNAVAILABLE = "mic_calibration_unavailable"
REFUSE_MIC_ABSENT = "measurement_mic_absent"
REFUSE_TARGET_REJECTED = "seat_spl_target_rejected"
# The slug is unchanged on purpose: the ceiling no longer BINDS on the driver
# caps, but resolving them is still what can fail here (the same call resolves
# each driver's permitted band), and it is a stable operator-facing string.
REFUSE_CEILING_UNDERIVABLE = "driver_cap_ceiling_underivable"
REFUSE_STIMULUS_MISSING = "stimulus_wav_missing"


def _refused(reason: str, detail: str) -> tuple[SeatLevelResult, str]:
    return SeatLevelResult(status="refused", reason=reason), detail


def _resolve_sensitivity(args: argparse.Namespace) -> Any:
    """The mic's absolute reference, from an explicit file or the stored record.

    Returns ``None`` when no calibration can be read — the caller REFUSES; a
    guessed sensitivity would silently mis-scale every SPL decision.
    """
    from jasper.audio_measurement.calibration import (
        find_stored_calibration,
        parse_calibration_sensitivity,
    )

    path: Path | None = None
    if args.calibration_file:
        path = Path(args.calibration_file)
    elif args.mic_serial:
        record = find_stored_calibration(
            provider=args.mic_provider,
            model_key=args.mic_model,
            serial=args.mic_serial,
        )
        if record is not None:
            path = Path(record.raw_path)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_calibration_sensitivity(text)


def stimulus_peak_dbfs(path: Path) -> float:
    """True peak of a stimulus WAV, dBFS, across ALL channels.

    The max over the whole interleaved array — deliberately NOT a downmix.
    ``sweep.read_wav_mono`` averages channels, which halves the peak of a
    program whose stimulus sits on one channel while the other is silent, and
    an under-reported peak would RAISE the derived volume ceiling. This reads
    the worst case instead, which is the only direction that is safe.

    A peak of zero raises: a silent file would derive an absurdly high ceiling.
    """
    import numpy as np
    from scipy.io import wavfile

    _rate, data = wavfile.read(str(path))
    magnitudes = np.abs(np.asarray(data).astype(np.float64))
    peak = float(magnitudes.max()) if magnitudes.size else 0.0
    if np.issubdtype(np.asarray(data).dtype, np.integer):
        peak /= float(np.iinfo(np.asarray(data).dtype).max)
    if not (peak > 0.0) or not math.isfinite(peak):
        raise ValueError(
            f"{path} carries no signal; a silent stimulus cannot bound a volume"
        )
    return 20.0 * math.log10(peak)


def _applied_branch_peaks(
    stimulus: Path, targets: list[dict[str, Any]]
) -> dict[str, float] | None:
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

    from jasper.active_speaker.branch_peak import (
        BranchPeakError,
        branch_peaks_for_targets,
    )
    from jasper.active_speaker.environment import read_camilla_statefile_config_path

    try:
        config_path = read_camilla_statefile_config_path()
        if not config_path:
            raise BranchPeakError("no CamillaDSP statefile names an applied config")
        config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        peaks = branch_peaks_for_targets(config, stimulus, targets)
    except (BranchPeakError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        log_event(
            logger,
            "active_speaker.seat_level_branch_peaks_unavailable",
            detail=str(exc),
        )
        return None
    log_event(
        logger,
        "active_speaker.seat_level_branch_peaks",
        peaks=" ".join(f"{key}={value:.2f}" for key, value in sorted(peaks.items())),
    )
    return peaks


def _derive_bounds(args: argparse.Namespace, stimulus: Path) -> tuple[float, float]:
    """``(volume ceiling for THIS stimulus, commissioning SPL ceiling)``."""
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.measurement import active_driver_targets
    from jasper.output_topology import load_output_topology_strict

    topology = load_output_topology_strict(args.topology)
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    if not isinstance(safety_profile, dict):
        raise SessionVolumePlanError(
            "the design draft carries no driver_safety_profile; commission the "
            "drivers before leveling"
        )
    targets = active_driver_targets(topology)
    fingerprints = [str(target["target_fingerprint"]) for target in targets]
    # The PAD-FOLDED sensitivities, not the naked datasheet ones. An L-pad'd
    # tweeter's acoustic output is quieter than its bare rating by exactly the
    # pad, and the derived HF ceiling is a sensitivity DELTA against the woofer
    # — so reading the naked figure protects the driver as if it were the pad's
    # worth more sensitive than it physically is. This is the reader
    # ``declared_driver_sensitivities``' own docstring names for
    # excitation-ceiling derivation and session-volume planning (#1665), and the
    # one the /correction crossover-v2 flow already passes.
    ceiling_db = unsegmented_stimulus_ceiling_db(
        safety_profile,
        fingerprints,
        stimulus_peak_dbfs=stimulus_peak_dbfs(stimulus),
        declared_sensitivities=declared_effective_driver_sensitivities(draft),
        branch_peaks_dbfs=_applied_branch_peaks(stimulus, targets),
    )
    # ``resolve_capture_preset`` is the sibling every capture-analysis surface
    # uses: it resolves the preview-compiled preset and only then falls back to
    # the bundled one. ``resolve_commission_inputs()`` alone returns ``None``
    # for the preset on an ordinary box.
    preset = resolve_capture_preset(topology)
    spl_ceiling = float(preset.safety.max_commissioning_level_db_spl)
    return ceiling_db, spl_ceiling


async def _run(args: argparse.Namespace) -> tuple[SeatLevelResult, str]:
    from jasper.audio_measurement.correction_lane import exec_correction_play
    from jasper.audio_measurement.wired_capture import (
        WiredCaptureError,
        resolve_wired_mic,
    )
    from jasper.audio_measurement.wired_level_meter import WiredLevelMeter
    from jasper.camilla import primary_controller

    stimulus = Path(args.stimulus_wav)
    if not stimulus.is_file():
        return _refused(REFUSE_STIMULUS_MISSING, f"no such stimulus WAV: {stimulus}")

    sensitivity = _resolve_sensitivity(args)
    if sensitivity is None:
        return _refused(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE,
            "no parseable 'Sens Factor' calibration for this microphone — pass "
            "--calibration-file, or store the vendor file via the /correction "
            "wizard. Absolute SPL is never guessed.",
        )

    mic = resolve_wired_mic()
    if mic is None:
        return _refused(
            REFUSE_MIC_ABSENT,
            "no measurement-class capture card is present; plug the mic in",
        )

    try:
        ceiling_db, spl_ceiling = _derive_bounds(args, stimulus)
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
    meter = WiredLevelMeter(mic.pcm, channels=args.mic_channels)
    player: Any = None

    async def _play() -> None:
        nonlocal player
        player = await exec_correction_play(
            stimulus, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await player.wait()

    def _cancel() -> None:
        if player is not None and player.returncode is None:
            player.terminate()

    async def _samples() -> list[Any]:
        return meter.drain()

    try:
        meter.start()
    except WiredCaptureError as exc:
        return _refused(REFUSE_MIC_ABSENT, str(exc))
    try:
        result = await run_seat_level_ramp(
            target=target,
            sensitivity=sensitivity,
            max_main_volume_db=ceiling_db,
            spl_ceiling_db_spl=spl_ceiling,
            get_main_volume_db=cam.get_volume_db,
            set_main_volume_db=cam.set_volume_db,
            play_continuous_tone=_play,
            cancel_tone=_cancel,
            next_samples=_samples,
        )
    except SeatLevelRampError as exc:
        # The refusal code is the first token of the message (build_seat_level_
        # ramp_config formats it that way) so the operator sees the same
        # vocabulary a ramp terminal produces.
        return _refused(str(exc).split(":", 1)[0], str(exc))
    finally:
        _cancel()
        meter.stop()
    detail = (
        f"reference {result.reference_volume_db:.2f} dB measured "
        f"{result.measured_db_spl:.1f} dB SPL"
        if result.converged
        else (result.detail or "nothing was banked")
    )
    return result, detail


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-seat-level",
        description=(
            "Ramp the measurement volume until a calibrated mic at the seat "
            "reads the target dB SPL, then bank that volume as the crossover "
            "session's measurement reference. PRECONDITION: the mic's Sens "
            "Factor is quoted at MAXIMUM capture volume — confirm "
            "`amixer -c <card>` shows the capture control at 100%, or every "
            "absolute SPL below is wrong by the shortfall."
        ),
    )
    parser.add_argument(
        "--stimulus-wav",
        required=True,
        help="continuous stimulus to level against (the program this session "
        "will measure with)",
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
        help="explicit vendor calibration .txt carrying the 'Sens Factor' line",
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
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Without this the whole disclosure receipt is computed and discarded: the
    # root logger sits at WARNING, so ``event=active_speaker.unsegmented_ceiling_bound``
    # -- the ONE production reader of the declared caps this ceiling drives past
    # -- reaches no handler. ``basicConfig`` at INFO in ``main`` is what the
    # sibling ``event=``-emitting CLIs do (``crossover_prescriber``,
    # ``arm_walk``, ``sound``, ...), reusing the shared FORMAT so the one place
    # that shape is written down stays the only one. In ``main`` rather than at
    # import, because a module that configures the root logger on import
    # imposes its choice on every importer, the test suite included.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(argv)
    if not args.calibration_file and not args.mic_serial:
        build_parser().error("pass --calibration-file or --mic-serial")
    result, detail = asyncio.run(_run(args))
    if args.json:
        print(json.dumps({**result.to_dict(), "detail": detail}, indent=2, sort_keys=True))
    elif result.converged:
        print(f"converged: {detail}")
    else:
        print(f"refused ({result.reason}): {detail}", file=sys.stderr)
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
