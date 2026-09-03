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
* the stimulus — generated from the drivers' own declarations
  (:func:`default_stimulus_wav`), or an operator-named WAV, played on the
  correction lane

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

Usage::

    jasper-seat-level --mic-serial 810-8494

Exit 0 only on a converged, banked reference; 1 on any refusal, with the
``REFUSE_*`` reason on stderr and in the ``event=`` line. A refusal's line also
carries the window the stop abandoned — how many samples it saw, their
min/median/max dB SPL, and the sample that tripped with its offset from the
volume step — so a stop can be told apart from a level that rose and stayed
without reading the journal. ``--verbose`` adds the whole per-sample series,
one DEBUG line per window.
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
from pathlib import Path
from typing import Any, NamedTuple

from jasper.log_event import log_event
from jasper.active_speaker.seat_level_ramp import (
    REFUSE_INTERRUPTED,
    interrupted_restore_outcome,
    SeatLevelRampError,
    SeatLevelResult,
    run_seat_level_ramp,
)
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TARGET_DB_SPL,
    DEFAULT_TOLERANCE_DB,
    SeatLevelTarget,
    SeatLevelTargetError,
    StimulusProvenance,
)
from jasper.active_speaker.commission_wiring import CommissionPresetResolutionError
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.session_volume_plan import (
    SessionVolumePlanError,
    unsegmented_stimulus_ceiling_db,
)
from jasper.audio_measurement.calibration import (
    MIC_CALIBRATION_UNAVAILABLE_DETAIL,
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
    resolve_mic_sensitivity,
)

from ._logging import CLI_LOG_FORMAT

logger = logging.getLogger(__name__)

REFUSE_MIC_ABSENT = "measurement_mic_absent"
REFUSE_TARGET_REJECTED = "seat_spl_target_rejected"
# The slug is unchanged on purpose: the ceiling no longer BINDS on the driver
# caps, but resolving them is still what can fail here (the same call resolves
# each driver's permitted band), and it is a stable operator-facing string.
REFUSE_CEILING_UNDERIVABLE = "driver_cap_ceiling_underivable"
REFUSE_STIMULUS_MISSING = "stimulus_wav_missing"

def _refused(
    reason: str, detail: str, *, restored: bool | None = None
) -> tuple[SeatLevelResult, str]:
    return (
        SeatLevelResult(status="refused", reason=reason, restored=restored),
        detail,
    )


def _ambient_phrase(ramp: dict[str, Any]) -> str:
    """Disclose a room floor the pass had to measure twice.

    The rise gate reads ``observed - floor``, so which window supplied the floor
    changes which readings the pass trusted. An operator reading a terminal is
    not reading ``--json``, and a silently replaced floor is exactly the kind of
    correction that must be stated rather than applied invisibly.
    """
    if not ramp.get("ambient_remeasured"):
        return ""
    # Leading ". " and not " ": this is APPENDED to a detail that does not end
    # in a period (the converged line is "reference X dB measured Y dB SPL"), so
    # the phrase has to supply its own sentence break or the two run together.
    return (
        f". A climb reading landed below the {ramp['ambient_db_spl']:.1f} dB SPL "
        "ambient window, which cannot happen while the speaker is playing, so "
        "the tone was stopped and the room re-measured in silence: "
        f"{ramp['ambient_remeasured_db_spl']:.1f} dB SPL, which is the floor "
        "every rise above was measured against."
    )


def _restore_phrase(restored: bool | None) -> str:
    """Say what is known about the fader, and never more than that."""
    if restored is True:
        return "The household volume was restored."
    if restored is False:
        return (
            "The household volume was NOT restored — the speaker is parked at a "
            "measurement level; the volume-recovery screen can drain it."
        )
    return "Whether the household volume was restored could not be observed."


def stimulus_provenance(
    path: Path, *, band_hz: tuple[float, float] | None = None
) -> StimulusProvenance:
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
    full_scale = (
        float(np.iinfo(np.asarray(data).dtype).max)
        if np.issubdtype(np.asarray(data).dtype, np.integer)
        else 1.0
    )
    peak = float(np.abs(samples).max()) / full_scale if samples.size else 0.0
    if not (peak > 0.0) or not math.isfinite(peak):
        raise ValueError(
            f"{path} carries no signal; a silent stimulus cannot bound a volume"
        )
    rms = float(np.sqrt(np.mean((samples / full_scale) ** 2)))
    return StimulusProvenance(
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        peak_dbfs=20.0 * math.log10(peak),
        rms_dbfs=20.0 * math.log10(rms),
        band_hz=band_hz,
    )


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
    safety_profile = draft.get("driver_safety_profile")
    if not isinstance(safety_profile, dict):
        raise SessionVolumePlanError(
            "the design draft carries no driver_safety_profile; commission the "
            "drivers before leveling"
        )
    return _Declarations(topology, draft, safety_profile)


def default_stimulus_wav(
    declarations: _Declarations,
) -> tuple[Path, tuple[float, float]]:
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
    from jasper.active_speaker.commissioning_admission import (
        ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS,
    )
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_measurement_band_hz,
    )
    from jasper.active_speaker.measurement import active_driver_targets
    from jasper.active_speaker.speech_stimulus import DEFAULT_CACHE_DIR
    from jasper.active_speaker.test_signal_plan import (
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        MIN_DRIVER_TEST_FREQUENCY_HZ,
    )
    from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav
    from jasper.audio_measurement.program import PROGRAM_SAMPLE_RATE_HZ

    bands = [
        resolve_driver_measurement_band_hz(
            declarations.safety_profile, str(target["target_fingerprint"])
        )
        for target in active_driver_targets(declarations.topology)
    ]
    if not bands:
        raise SessionVolumePlanError(
            "this topology declares no active driver targets, so no stimulus "
            "band can be derived; name one with --stimulus-wav"
        )
    f_lo = max(MIN_DRIVER_TEST_FREQUENCY_HZ, min(lo for lo, _hi in bands))
    f_hi = min(
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        PROGRAM_SAMPLE_RATE_HZ / 2.0 - 1.0,
        max(hi for _lo, hi in bands),
    )
    band = (float(f_lo), float(f_hi))
    return (
        ensure_bandlimited_noise_wav(
            f_lo_hz=band[0],
            f_hi_hz=band[1],
            duration_s=MAX_STIMULUS_SAMPLES / PROGRAM_SAMPLE_RATE_HZ,
            dbfs=ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS,
            sample_rate=PROGRAM_SAMPLE_RATE_HZ,
            cache_dir=DEFAULT_CACHE_DIR,
        ),
        band,
    )


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


def _derive_bounds(
    stimulus: Path, levels: StimulusProvenance, declarations: _Declarations
) -> tuple[float, float]:
    """``(volume ceiling for THIS stimulus, commissioning SPL ceiling)``.

    ``levels`` is measured once by the caller rather than read again here (the
    ramp needs the same numbers for its refusal, and two reads of one file are
    two answers), and ``declarations`` is loaded once by the caller for the
    same reason.
    """
    from jasper.active_speaker.commission_wiring import commissioning_spl_ceiling_db
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
    )
    from jasper.active_speaker.measurement import active_driver_targets

    topology, draft, safety_profile = declarations
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
        stimulus_peak_dbfs=levels.peak_dbfs,
        declared_sensitivities=declared_effective_driver_sensitivities(draft),
        branch_peaks_dbfs=_applied_branch_peaks(stimulus, targets),
    )
    return ceiling_db, commissioning_spl_ceiling_db(topology)


class _OperatorStopped(Exception):
    """SIGINT arrived while the pass was running, and the pass has torn down.

    Carries the pass's MEASURED restore outcome (``None`` when the pass never
    got far enough to have one), so the refusal this becomes can state the
    volume rather than assume it.
    """

    def __init__(self, restored: bool | None) -> None:
        super().__init__("stopped by the operator")
        self.restored = restored


async def _stoppable(pass_coro: Any) -> SeatLevelResult:
    """Run the leveling pass with SIGINT wired to its own cancellation.

    Stopping must be possible at ANY moment, and it must stop the stimulus and
    give the household its volume back — which is the pass's own teardown, not
    a second one here. So SIGINT cancels the task and the pass's shielded
    ``run_teardown`` does the work; this only turns the cancellation into an
    honest refusal. Without the handler the only stop is Python's default
    KeyboardInterrupt, which unwinds through the same ``finally`` blocks but
    gives the operator no named outcome — and on a loop that is not the main
    thread's, no handler can be installed at all, so the default is left in
    place rather than pretended at.
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
    except asyncio.CancelledError as exc:
        if stopped:
            raise _OperatorStopped(interrupted_restore_outcome(exc)) from None
        raise
    except KeyboardInterrupt as exc:
        # Reached only when no handler could be installed (a loop that is not
        # the main thread's): the interpreter raises inside the running
        # coroutine, so the pass's teardown has already run and stamped it.
        raise _OperatorStopped(interrupted_restore_outcome(exc)) from None
    finally:
        if handled:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGINT)


async def _run(args: argparse.Namespace) -> tuple[SeatLevelResult, str]:
    from jasper.audio_measurement.correction_lane import exec_correction_play
    from jasper.audio_measurement.wired_capture import (
        WiredCaptureError,
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

    sensitivity = resolve_mic_sensitivity(
        calibration_file=args.calibration_file,
        mic_serial=args.mic_serial,
        mic_provider=args.mic_provider,
        mic_model=args.mic_model,
    )
    if sensitivity is None:
        return _refused(
            REFUSE_MIC_CALIBRATION_UNAVAILABLE, MIC_CALIBRATION_UNAVAILABLE_DETAIL
        )

    mic = resolve_wired_mic()
    if mic is None:
        return _refused(
            REFUSE_MIC_ABSENT,
            "no measurement-class capture card is present; plug the mic in",
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
        result = await _stoppable(
            run_seat_level_ramp(
                target=target,
                sensitivity=sensitivity,
                max_main_volume_db=ceiling_db,
                spl_ceiling_db_spl=spl_ceiling,
                get_main_volume_db=cam.get_volume_db,
                set_main_volume_db=cam.set_volume_db,
                play_continuous_tone=_play,
                cancel_tone=_cancel,
                next_samples=_samples,
                # Disclosure, not a bound: WHICH signal is being played and
                # what it measures, so `spl_target_unreachable` can show its
                # own arithmetic instead of reading as a nanny, and so the
                # banked reference names the stimulus half of its definition.
                stimulus=provenance,
            )
        )
    except _OperatorStopped as stop:
        return _refused(
            REFUSE_INTERRUPTED,
            "stopped by the operator; the stimulus was cut and nothing was "
            f"banked. {_restore_phrase(stop.restored)}",
            restored=stop.restored,
        )
    except SeatLevelRampError as exc:
        # The refusal code is the first token of the message (the window/ceiling
        # validators format it that way) so the operator sees the same
        # vocabulary a refusal terminal produces.
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
    # The refusal's own window summary already rides ``result.detail`` (the ramp
    # writes it there so one sentence serves every reader). The re-measured
    # floor is a ramp fact rather than a refusal fact, so it is appended here
    # and reaches a converged run's line too -- which is the run that most needs
    # it, since the second silent window is what its readings were judged
    # against.
    return result, detail + _ambient_phrase(result.ramp)


#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "measured"


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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - a reference is already banked for this session and you are\n"
            "    not deliberately re-leveling\n"
            "  - the mic capture control is not confirmed at 100% (see the\n"
            "    PRECONDITION above) -- level first, then re-run this\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-seat-level \\\n"
            "      --calibration-file /var/lib/jasper/mic-cal/umik2-7003219.txt\n"
            "\n"
            "EXIT CODES\n"
            "  0  converged and banked; the human line and --json both carry\n"
            "     the reference dB SPL reached\n"
            "  1  refused -- \"refused (<reason>): <detail>\" on stderr names\n"
            "     why (interrupted, or the ramp's own refusal vocabulary);\n"
            "     --json emits the same reason/detail as structured fields\n"
            "  2  usage error (argparse) -- most commonly neither\n"
            "     --calibration-file nor --mic-serial was passed"
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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also log every settle window's per-sample dB SPL series (one "
        "DEBUG line per window) — the evidence that separates a one-sample "
        "excursion from a level that rose and stayed",
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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=CLI_LOG_FORMAT,
    )
    if not args.calibration_file and not args.mic_serial:
        build_parser().error("pass --calibration-file or --mic-serial")
    try:
        result, detail = asyncio.run(_run(args))
    except KeyboardInterrupt as exc:
        # The last-resort path: the interrupt escaped ``_stoppable`` entirely,
        # so the pass may never have opened the latch. Report only what the
        # exception actually carries -- claiming a restore here is the
        # dishonesty this field exists to prevent.
        restored = interrupted_restore_outcome(exc)
        result = SeatLevelResult(
            status="refused", reason=REFUSE_INTERRUPTED, restored=restored
        )
        detail = (
            "stopped by the operator; the stimulus was cut and nothing was "
            f"banked. {_restore_phrase(restored)}"
        )
    if args.json:
        print(json.dumps({**result.to_dict(), "detail": detail}, indent=2, sort_keys=True))
    elif result.converged:
        print(f"converged: {detail}")
    else:
        print(f"refused ({result.reason}): {detail}", file=sys.stderr)
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
