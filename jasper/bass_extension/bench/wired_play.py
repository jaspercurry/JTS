# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bench's :class:`~jasper.bass_extension.bench.executor.PlayAndCapture`.

Shaped exactly like ``jasper-null``'s ``_play_and_capture``
(:mod:`jasper.cli.null_door`): the engine's own play path — ``play_program``
over the three seams ``crossover_v2.composition.bind_program_playback_seams``
binds — wrapped by the wired capture kernel (a
:class:`~jasper.audio_measurement.wired_capture.WiredRecorder` armed before any
audio, ``mint_wired_answer`` after). There is no second recorder, no second
admission and no second play seam.

The binder's ``graph_yaml`` is the ACTIVATION READ-BACK the runner already banks
(``ActivationReadback.active_config_raw``), never the submitted graph text: a
candidate rung patches ``clip_limit`` onto the running graph after installing it
(:func:`~jasper.bass_extension.bench.activation.temporary_bass_activation`), so
the submitted text is not what runs. ``confirm_graph_is_live`` normalizes and
fingerprints rather than comparing bytes, so the read-back is exactly what that
parameter wants — and taking the proof per play, inside the writer lock, is the
only proof that covers rung *k+1*: ``measurement_window`` does not pause
CamillaDSP, and the lock is released between rungs.

The executor generates and pads every stimulus (R6) and hands this module one
content-addressed artifact; nothing here generates or modifies stimulus bytes.
What this module adds is the bench's own framing of that path: the two fader
collaborators the runner injects (:class:`ClaimFloorControl`,
:class:`BenchWindow`), the R6a / R10 reads taken at both ends of the play, and
the capture analysis (:mod:`~jasper.bass_extension.bench.analysis`) that turns
one recording into the frozen signal / protection / transparency evidence.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, cast

import numpy as np

from jasper.active_speaker.crossover_v2.composition import (
    bind_program_playback_seams,
)
from jasper.active_speaker.crossover_v2.volume_claim import MeasurementVolumeClaim
from jasper.active_speaker.excitation_safety_plan import (
    ExcitationSafetyPlanError,
    effective_sweep_duration_limit_s,
    resolve_driver_excitation_ceilings,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_playback import (
    ProgramPlaybackError,
    ProgramPlaybackRefused,
    play_program,
)
from jasper.active_speaker.restore_wait import give_back, resilient_restore
from jasper.active_speaker.session_volume_plan import (
    SessionVolumeOpenResult,
    SessionVolumePlan,
    SessionVolumePlanError,
    VolumeDoor,
)
from jasper.active_speaker.volume_latch import (
    EMERGENCY_MEASUREMENT_VOLUME_DB,
    READBACK_TOLERANCE_DB,
    GetMainVolumeDb,
    MeasurementFaderDrift,
    fader_matches,
    read_fader_db,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.frame_ledger import capture_faults
from jasper.audio_measurement.playback import SweepPlaybackError, WavSourceError
from jasper.audio_measurement.program import (
    KIND_SWEEP,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_SAMPLE_RATE_HZ,
    ExcitationProgram,
    ProgramSegment,
    finalize_program,
    segment_emitted_band_hz,
    silence_segment,
)
from jasper.audio_measurement.wired_capture import (
    WIRED_POST_ROLL_S,
    WIRED_PRE_PLAY_ALLOWANCE_S,
    WiredCaptureError,
    WiredMicDevice,
    decode_wav_to_mono,
    make_wired_recorder,
    mint_wired_answer,
)
from jasper.bass_extension.targets import MarginPolicy
from jasper.camilla import CamillaUnavailable
from jasper.log_event import log_event
from jasper.measurement_window import measurement_window
from jasper.output_topology import OutputTopology
from jasper.volume_owner import VolumeClaimHandle, VolumeOwner

from .analysis import (
    CaptureUnanalyzable,
    MeasurementPolicy,
    SustainCaptureAnalysis,
    SweepCaptureAnalysis,
    analyze_sustain_capture,
    analyze_sweep_capture,
    assess_transparency,
    sample_peak_dbfs,
    sweep_pre_roll_s,
)
from .executor import PlayedStimulus, SweepOrSustain
from .manifest import StimulusRequest
from .runner import (
    BenchRefused,
    FloorControl,
    ReferenceSweepCapture,
    Stop,
    TargetPlan,
)
from .sink import BundleSink
from .stimulus import PaddedStimulus

logger = logging.getLogger(__name__)

BENCH_LOCK_SOURCE = "bass_extension_bench"

#: Noise-floor window recorded before a sustain hold (a hold has no harmonic
#: images to clear, so it needs a floor window and nothing more).
SUSTAIN_PRE_ROLL_S = 1.0

#: Added to the program's own duration for ``verified_program_aplay``'s
#: timeout, which bounds the aplay subprocess: the device open and the drain
#: happen inside it. Scaled off the program rather than fixed, because a bass
#: hold runs minutes — one ceiling would either kill a long hold or wait out a
#: wedged short one.
PLAYBACK_TIMEOUT_MARGIN_S = 15.0

REFUSE_COMMANDED_VOLUME = "bench_commanded_volume_mismatch"
REFUSE_OWNER_ROLE = "bench_owner_role_unresolved"
REFUSE_BAND = "bench_stimulus_band_unprotected"
REFUSE_DRIVER_CAP = "bench_driver_cap_refused"
REFUSE_HOLD_DURATION = "bench_hold_exceeds_declared_duration"
REFUSE_ADMISSION = "bench_admission_refused"
REFUSE_PLAYBACK = "bench_playback_failed"
REFUSE_CONTROLLER = "bench_controller_unavailable"
REFUSE_FADER = "bench_fader_not_at_level"
REFUSE_NOT_AT_FLOOR = "bench_not_at_floor"
REFUSE_RECORDER = "bench_recorder_never_rolled"
REFUSE_CAPTURE = "bench_capture_failed"
REFUSE_VOLUME_NOT_OPEN = "bench_volume_not_open"
REFUSE_STIMULUS = "bench_stimulus_unreadable"
REFUSE_UNANALYZABLE = "bench_capture_unanalyzable"

#: :func:`bass_owner_role`'s answer when the owner is the preset's local
#: subwoofer. No :data:`~jasper.active_speaker.profile.DRIVER_ROLES_BY_WAY`
#: role is spelled this way, so it cannot collide with a declared driver.
SUBWOOFER_ROLE = "subwoofer"

#: How far below the lowest corner above the owner a stimulus band must stop.
#: A Linkwitz-Riley pair is only 6 dB down AT the corner; one octave below it
#: the LR4 high-pass on the driver above is ~24 dB further down, which is the
#: margin :func:`assert_stimulus_band_protected`'s reasoning assumes.
CORNER_MARGIN_OCTAVES = 1.0

_LEAD_IN_SEGMENT_ID = "bench_lead_in"
_LEAD_OUT_SEGMENT_ID = "bench_lead_out"
_INT16_FULL_SCALE = 32767.0


class ClaimFloorControl:
    """:class:`~jasper.bass_extension.bench.runner.FloorControl` over the
    process :class:`~jasper.volume_owner.VolumeOwner`.

    The floor is a transient duck UNDER the session's standing measurement
    claim, not a second level claim, so the owner keeps arbitrating every write
    and the claim's own level is what the duck releases back onto.
    """

    def __init__(
        self,
        owner: VolumeOwner,
        *,
        read_fader: GetMainVolumeDb,
        level_db: float,
    ) -> None:
        self._owner = owner
        self._read_fader = read_fader
        self._level_db = float(level_db)
        self._handle: VolumeClaimHandle | None = None

    async def to_floor(self) -> None:
        if self._handle is None:
            self._handle = await self._owner.acquire_duck(
                max(0.0, self._level_db - EMERGENCY_MEASUREMENT_VOLUME_DB)
            )

    async def assert_at_floor(self) -> None:
        observed = await read_fader_db(self._read_fader)
        if (
            observed is None
            or observed > EMERGENCY_MEASUREMENT_VOLUME_DB + READBACK_TOLERANCE_DB
        ):
            raise BenchRefused(
                REFUSE_NOT_AT_FLOOR,
                f"the fader reads {'nothing' if observed is None else f'{observed:.2f} dB'}, "
                f"not the {EMERGENCY_MEASUREMENT_VOLUME_DB:.2f} dB safe floor",
            )

    async def raise_to_level(self) -> None:
        handle = self._handle
        if handle is not None:
            # Cleared only after the release lands: a failed release that had
            # already dropped the handle would leave the duck installed with
            # nothing left to lift it.
            await self._owner.release(handle)
            self._handle = None


class BenchWindow:
    """``BenchDeps.open_window``: mux isolation + voice pause around the
    session volume plan, opened at the campaign's one measurement level.
    """

    def __init__(
        self,
        *,
        plan: SessionVolumePlan,
        claim: MeasurementVolumeClaim,
        door: VolumeDoor,
        floor: ClaimFloorControl,
        level_db: float,
        window: Callable[..., AbstractAsyncContextManager[Any]] = measurement_window,
    ) -> None:
        self._plan = plan
        self._claim = claim
        self._door = door
        self._floor = floor
        self._level_db = float(level_db)
        self._window = window

    def __call__(self) -> AbstractAsyncContextManager[None]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[None]:
        async with self._window():
            # Same reason `crossover_v2.door` calls it here.
            await self._plan.enforce_ceiling(self._door)
            body_error: BaseException | None = None
            try:
                try:
                    opened = await self._plan.open(self._level_db, self._door)
                except SessionVolumePlanError as exc:
                    raise BenchRefused(REFUSE_VOLUME_NOT_OPEN, str(exc)) from exc
                if opened is not SessionVolumeOpenResult.OPENED:
                    raise BenchRefused(
                        REFUSE_VOLUME_NOT_OPEN,
                        f"the measurement volume did not confirm at "
                        f"{self._level_db:.2f} dB ({opened.value}); the speaker "
                        "was put back",
                    )
                yield None
            except BaseException as raised:  # noqa: BLE001 - CancelledError is the point
                body_error = raised
                raise
            finally:
                await resilient_restore(
                    give_back(
                        (
                            self._floor.raise_to_level,
                            self._claim.release,
                            partial(
                                self._plan.close,
                                self._door,
                                reason="bass_extension_bench_closed",
                            ),
                        ),
                        body_error=body_error,
                    )
                )


@dataclass(frozen=True)
class AdmissionContext:
    """What the engine's re-admission reads.

    ``resolve_conductor_context`` supplies every field; the bench never derives
    one of them itself. Narrowed to those six rather than taking its
    :class:`~jasper.active_speaker.crossover_v2.conductor_context.V2ConductorContext`
    whole, so what re-admission reads is the whole of what this seam can reach.
    """

    topology: OutputTopology
    safety_profile: Mapping[str, Any]
    role_targets: Mapping[str, str]
    session_volume_db: float
    declared_sensitivities: Mapping[str, float]
    preset: ActiveSpeakerPreset


def bass_owner_role(
    preset: ActiveSpeakerPreset, owner_channels: Sequence[int]
) -> str:
    """The declared driver role the bass-extension owner channels belong to.

    Exactly one role, on both branches: an owner set that merely CONTAINS the
    declared subwoofer index would be answered "subwoofer" while a woofer sat
    in it too, and every cap below would then be judged for the wrong driver.
    """

    owner = frozenset(int(channel) for channel in owner_channels)
    local_sub = preset.local_subwoofer
    if local_sub is not None and owner == {int(local_sub.physical_output_index)}:
        return SUBWOOFER_ROLE
    indexes_by_role: dict[str, set[int]] = {}
    for output in preset.channel_map.outputs:
        indexes_by_role.setdefault(output.driver_role, set()).add(int(output.index))
    matched = [role for role, indexes in indexes_by_role.items() if indexes == owner]
    if len(matched) != 1:
        raise BenchRefused(
            REFUSE_OWNER_ROLE,
            f"owner channels {sorted(owner)} match {len(matched)} declared "
            "driver roles, not exactly one",
        )
    return matched[0]


def assert_stimulus_band_protected(
    preset: ActiveSpeakerPreset, *, owner_role: str, band: tuple[float, float]
) -> None:
    """Refuse a stimulus the owner's crossover does not keep to itself.

    The DECLARATION-side check, over ``preset.crossover_regions``: it names a
    stimulus that is wrong by design, in one refusal, before the graph is even
    read. It does NOT stand in for the per-driver caps —
    :func:`assert_driver_caps_evaluated` judges those — so
    what is left here is the shape of the band against the owner's own
    crossover:

    * no driver may sit BELOW the owner (a region whose upper driver is the
      owner, or a local subwoofer under a non-sub owner) — that driver takes
      the stimulus through its low-pass at the owner's level;
    * the band's top must stop :data:`CORNER_MARGIN_OCTAVES` below the lowest
      corner above the owner, since a Linkwitz-Riley pair is only 6 dB down AT
      the corner.

    An owner with nothing above it (a single-driver main) has no upper bound.
    """

    regions = preset.crossover_regions
    local_sub = preset.local_subwoofer
    below = [
        region.lower_driver for region in regions if region.upper_driver == owner_role
    ]
    if local_sub is not None and owner_role != SUBWOOFER_ROLE:
        below.append(SUBWOOFER_ROLE)
    if below:
        raise BenchRefused(
            REFUSE_BAND,
            f"{', '.join(sorted(below))} sits below the {owner_role} the "
            f"stimulus is admitted for and takes the {band[0]:g}-{band[1]:g} Hz "
            "band through its own low-pass",
        )
    if owner_role == SUBWOOFER_ROLE:
        if local_sub is None:
            raise BenchRefused(
                REFUSE_BAND, "the owner is a subwoofer the preset does not declare"
            )
        corners = [float(local_sub.crossover_fc_hz)]
    else:
        corners = [
            float(region.fc_hz)
            for region in regions
            if region.lower_driver == owner_role
        ]
    if not corners:
        return
    ceiling_hz = min(corners) / (2.0**CORNER_MARGIN_OCTAVES)
    if float(band[1]) > ceiling_hz:
        raise BenchRefused(
            REFUSE_BAND,
            f"the {band[0]:g}-{band[1]:g} Hz stimulus runs past {ceiling_hz:g} Hz, "
            f"{CORNER_MARGIN_OCTAVES:g} octave below the {owner_role}'s "
            f"{min(corners):g} Hz corner, so the driver above it is driven "
            "rather than protected",
        )


def assert_driver_caps_evaluated(
    admission: AdmissionContext,
    *,
    program: ExcitationProgram,
    session_volume_db: float,
) -> None:
    """Judge EVERY declared driver against the artifact, not just the owner.

    The played artifact is a 2-channel mix that reaches every driver through the
    installed graph, but ``readmit_program_from_wav`` is the ISOLATED-driver
    gate: it resolves a cap only for the roles the program's own channels
    declare, which here is the bass owner alone. So every role in
    ``role_targets`` is resolved through ``resolve_driver_excitation_ceilings``
    — the engine's one owner of a driver's permitted band and cap — and judged:

    * a driver whose permitted band OVERLAPS the stimulus band is being driven,
      so its own cap binds on the artifact's effective peak;
    * a driver entirely outside it must DECLARE the protection filter that puts
      it there (a high-pass at or above its permitted floor, a low-pass at or
      below its ceiling). Out of band with nothing declared is an unaccounted
      driver, not a protected one.

    The second clause is where this differs from
    ``readmit_summed_program_from_wav``, which applies ``peak <= cap`` to every
    role unconditionally — right for ITS case, a summed sweep in every driver's
    band, but a bass stimulus sits decades inside a tweeter's stopband and that
    rule would refuse every bench pass at the quietest driver's cap. Nothing
    here credits a declared filter with a dB figure: that is the "separately
    reviewed protection model" ``limiter-evidence-protocol.md``'s claim
    boundary puts outside this campaign.
    """

    bands = [segment_emitted_band_hz(seg) for seg in program.stimulus_segments()]
    low_hz, high_hz = min(low for low, _ in bands), max(high for _, high in bands)
    peak_dbfs = max(
        float(seg.gain_db) for seg in program.stimulus_segments()
    ) + float(session_volume_db)
    for role, fingerprint in admission.role_targets.items():
        try:
            permitted, cap_dbfs = resolve_driver_excitation_ceilings(
                admission.safety_profile,
                str(fingerprint),
                program_admission=True,
                declared_sensitivities=admission.declared_sensitivities,
            )
        except ExcitationSafetyPlanError as exc:
            raise BenchRefused(REFUSE_DRIVER_CAP, f"{role}: {exc}") from exc
        where = (
            f"the {low_hz:g}-{high_hz:g} Hz stimulus and the {role}'s "
            f"{permitted.lower_hz:g}-{permitted.upper_hz:g} Hz permitted band"
        )
        if low_hz <= permitted.upper_hz and high_hz >= permitted.lower_hz:
            if peak_dbfs > cap_dbfs:
                raise BenchRefused(
                    REFUSE_DRIVER_CAP,
                    f"{where} overlap, and {peak_dbfs:.3f} dBFS effective is "
                    f"over its {cap_dbfs:.3f} dBFS cap",
                )
            continue
        wanted, edge_hz = (
            ("highpass", permitted.lower_hz)
            if high_hz < permitted.lower_hz
            else ("lowpass", permitted.upper_hz)
        )
        if not any(
            declared["kind"] == wanted
            and (
                float(declared["cutoff_hz"]) >= edge_hz
                if wanted == "highpass"
                else float(declared["cutoff_hz"]) <= edge_hz
            )
            for declared in _declared_protection_filters(
                admission.safety_profile, str(fingerprint)
            )
        ):
            raise BenchRefused(
                REFUSE_DRIVER_CAP,
                f"{where} are disjoint, and it declares no {wanted} that puts "
                "the stimulus there",
            )


def _declared_protection_filters(
    safety_profile: Mapping[str, Any], fingerprint: str
) -> Sequence[Mapping[str, Any]]:
    for target in safety_profile["targets"]:
        if target["target_fingerprint"] == fingerprint:
            return target["required_protection_filters"]
    return ()


def assert_body_within_declared_duration(
    admission: AdmissionContext, *, role: str, body_frames: int
) -> None:
    """Refuse a rendered body longer than the role's declared duration ceiling.

    ``prepare_driver_excitation_plan`` judges every stimulus segment against
    ``effective_sweep_duration_limit_s``, and #2921 makes that the ONE number
    anything COMPOSING a stimulus must fit. Comparing the realized body here
    moves the refusal into the fail-closed vocabulary and in front of the
    fader — the same refusal otherwise arrives after ``raise_to_level``, with
    the graph mutated and the recorder armed — and makes it visible at
    ``--dry-run``. It catches both a hold longer than any declared sweep and
    the phase-closing round-up a synchronized sweep adds to a requested length.
    """

    fingerprint = admission.role_targets.get(role)
    if not fingerprint:
        raise BenchRefused(
            REFUSE_OWNER_ROLE,
            f"no driver-safety target is mapped for the {role} bass owner",
        )
    try:
        limit_s = effective_sweep_duration_limit_s(
            admission.safety_profile, str(fingerprint)
        )
    except ExcitationSafetyPlanError as exc:
        raise BenchRefused(REFUSE_HOLD_DURATION, f"{role}: {exc}") from exc
    realized_s = int(body_frames) / float(PROGRAM_SAMPLE_RATE_HZ)
    if realized_s > limit_s:
        raise BenchRefused(
            REFUSE_HOLD_DURATION,
            f"the rendered body runs {realized_s:.4f} s but the {role}'s "
            f"declared duration ceiling is {limit_s:.4f} s",
        )


@dataclass(frozen=True)
class StimulusWav:
    """The padded artifact's PCM, plus where its body sits."""

    pcm: np.ndarray
    body_start: int
    body_end: int

    @property
    def channels(self) -> int:
        return int(self.pcm.shape[1])

    @property
    def frames(self) -> int:
        return int(self.pcm.shape[0])

    def body(self, channel: int) -> np.ndarray:
        column = self.pcm[self.body_start : self.body_end, channel]
        return column.astype(np.float64) / _INT16_FULL_SCALE

    def peak_dbfs(self, channel: int) -> float:
        column = self.pcm[self.body_start : self.body_end, channel]
        extreme = max(int(column.max()), -int(column.min()))
        return sample_peak_dbfs(np.array([extreme / _INT16_FULL_SCALE]))


def read_stimulus_wav(padded: PaddedStimulus) -> StimulusWav:
    """Decode the executor's padded artifact and locate its body.

    R6's padding is embedded in the bytes and the executor counted it, so the
    body is exactly the span between the declared lead-in and lead-out.
    """

    with wave.open(io.BytesIO(padded.wav_bytes), "rb") as source:
        width = source.getsampwidth()
        rate = int(source.getframerate())
        channels = int(source.getnchannels())
        frames = int(source.getnframes())
        raw = source.readframes(frames)
    declared_rate = int(padded.sample_rate_hz)
    # `finalize_program` stamps PROGRAM_SAMPLE_RATE_HZ and the admission gate
    # reads int16 PCM, so anything else cannot be described as a program at all.
    # The executor generates at the LIVE graph's rate and the offline renders
    # read the artifact's DECLARED one, so the refusal names both: a live graph
    # off 48 kHz is what one here means.
    if width != 2 or rate != PROGRAM_SAMPLE_RATE_HZ:
        raise BenchRefused(
            REFUSE_STIMULUS,
            f"the padded stimulus is {width * 8}-bit at {rate} Hz "
            f"(declared {declared_rate} Hz), not 16-bit at "
            f"{PROGRAM_SAMPLE_RATE_HZ} Hz",
        )
    lead_in, body, lead_out = (
        int(padded.lead_in_frames),
        int(padded.body_frames),
        int(padded.lead_out_frames),
    )
    if body <= 0 or lead_in + body + lead_out != frames:
        raise BenchRefused(
            REFUSE_STIMULUS,
            f"the padded stimulus declares {lead_in}+{body}+{lead_out} frames "
            f"but carries {frames}",
        )
    return StimulusWav(
        pcm=np.frombuffer(raw, dtype="<i2").reshape(-1, channels),
        body_start=lead_in,
        body_end=lead_in + body,
    )


def stimulus_segment_id(role: str, channel: int) -> str:
    return f"bench_{role}_{channel}"


def describe_stimulus_program(
    wav: StimulusWav,
    *,
    role: str,
    band: tuple[float, float],
    peak_dbfs: float,
    commanded_main_volume_db: float,
) -> ExcitationProgram:
    """Describe an already-rendered stimulus WAV as an excitation program.

    ``KIND_SWEEP`` is the segment vocabulary's stimulus kind and admission reads
    only band / peak / duration off a segment, so the band-limited-noise hold is
    described the same way the sweep is. ``peak_dbfs`` is the peak the request
    AUTHORIZED, so admission compares the artifact's fresh true peak against
    what was asked for rather than against the artifact itself; per
    :class:`~jasper.audio_measurement.program.ProgramSegment`'s vocabulary the
    segment's ``effective_peak_dbfs`` is that digital peak plus the commanded
    fader level.
    """

    segments: list[ProgramSegment] = []
    if wav.body_start > 0:
        segments.append(silence_segment(_LEAD_IN_SEGMENT_ID, 0, wav.body_start))
    for channel in range(wav.channels):
        segments.append(
            ProgramSegment(
                segment_id=stimulus_segment_id(role, channel),
                kind=KIND_SWEEP, role=role, channel=channel,
                start_sample=wav.body_start,
                n_samples=wav.body_end - wav.body_start,
                f1_hz=float(band[0]), f2_hz=float(band[1]),
                gain_db=peak_dbfs,
                effective_peak_dbfs=peak_dbfs + float(commanded_main_volume_db),
            )
        )
    if wav.body_end < wav.frames:
        segments.append(
            silence_segment(
                _LEAD_OUT_SEGMENT_ID, wav.body_end, wav.frames - wav.body_end
            )
        )
    return finalize_program(PROGRAM_PHASE_MEASURE, wav.channels, segments, wav.frames)


@dataclass(frozen=True)
class _Prepared:
    """Everything derived from the artifact before any hardware is touched."""

    wav: StimulusWav
    artifact: ArtifactIdentity
    program: ExcitationProgram
    segment: ProgramSegment
    tag: str
    band: tuple[float, float]
    peak_dbfs: float
    commanded_db: float
    #: Recorded silence in front of the play, and the floor window every
    #: analysis reads: the sweep needs its harmonic pre-guard, a hold needs a
    #: noise-floor window and nothing more.
    pre_guard_s: float

    @property
    def max_onset_s(self) -> float:
        """The latest the body's first sample can land in the capture: the
        recorded pre-guard, the artifact's own lead-in, and the capture
        budget's allowance for everything ``play_program`` does first."""

        lead_in_s = self.wav.body_start / float(PROGRAM_SAMPLE_RATE_HZ)
        return self.pre_guard_s + lead_in_s + WIRED_PRE_PLAY_ALLOWANCE_S


class WiredPlayAndCapture:
    """Admit, play and near-field capture one already-padded stimulus."""

    def __init__(
        self,
        *,
        sink: BundleSink,
        controller: Any,
        mic: WiredMicDevice,
        plan: SessionVolumePlan,
        floor: FloorControl,
        admission: AdmissionContext,
        margin: MarginPolicy,
        policy: MeasurementPolicy,
        config_dir: str | Path,
        read_mux_status: Callable[[], Awaitable[Mapping[str, Any]]],
        read_fanin_status: Callable[[], Awaitable[Mapping[str, Any]]],
        recorder_factory: Callable[[int, float], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sink = sink
        self._controller = controller
        self._mic = mic
        self._plan = plan
        self._floor = floor
        self._admission = admission
        self._margin = margin
        self._policy = policy
        self._config_dir = config_dir
        self._read_mux_status = read_mux_status
        self._read_fanin_status = read_fanin_status
        self._recorder_factory = recorder_factory or self._wired_recorder
        self._sleep = sleep

    def _wired_recorder(self, rate: int, budget_s: float) -> Any:
        return make_wired_recorder(
            self._mic, sample_rate_hz=rate, max_capture_s=budget_s
        )

    def _prepare(
        self, *, target: TargetPlan, role: SweepOrSustain,
        request: StimulusRequest, stimulus: PaddedStimulus,
        artifact: ArtifactIdentity, tag: str,
    ) -> _Prepared:
        """Everything the play is judged on, before the fader leaves the floor.

        Every refusal this raises lands in the fail-closed vocabulary with the
        speaker still at the safe floor, the graph unmutated and no recorder
        open — and every one of them fires at ``--dry-run`` too.
        """

        wav = read_stimulus_wav(stimulus)
        role_name = bass_owner_role(self._admission.preset, target.owner_channels)
        band = (
            float(request.requested_stimulus_band_hz[0]),
            float(request.requested_stimulus_band_hz[1]),
        )
        assert_stimulus_band_protected(
            self._admission.preset, owner_role=role_name, band=band
        )
        assert_body_within_declared_duration(
            self._admission,
            role=role_name,
            body_frames=wav.body_end - wav.body_start,
        )
        commanded = float(request.requested_commanded_main_volume_db)
        program = describe_stimulus_program(
            wav,
            role=role_name,
            band=band,
            peak_dbfs=float(request.requested_stimulus_effective_peak_dbfs),
            commanded_main_volume_db=commanded,
        )
        assert_driver_caps_evaluated(
            self._admission, program=program, session_volume_db=commanded
        )
        segment = program.segment(stimulus_segment_id(role_name, 0))
        return _Prepared(
            wav=wav,
            artifact=artifact,
            program=program,
            segment=segment,
            tag=tag,
            band=band,
            peak_dbfs=wav.peak_dbfs(0),
            commanded_db=commanded,
            pre_guard_s=(
                sweep_pre_roll_s(segment)
                if role == "sweep_transparency"
                else SUSTAIN_PRE_ROLL_S
            ),
        )

    async def _read(self, what: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """One controller / daemon-status read, refused rather than raised.

        ``CamillaUnavailable`` is a plain ``Exception``, so it is named here
        beside ``OSError`` instead of riding a broader base class.
        """

        try:
            return await call()
        except (CamillaUnavailable, OSError) as exc:
            raise BenchRefused(REFUSE_CONTROLLER, f"{what}: {exc}") from exc

    async def _poll_peaks(
        self, request: StimulusRequest
    ) -> list[tuple[float, ...]]:
        """R10(c): exactly ``cross_check_read_count`` reads at the manifest's
        recorded interval."""

        samples: list[tuple[float, ...]] = []
        for _ in range(int(request.cross_check_read_count)):
            await self._sleep(float(request.cross_check_poll_interval_s))
            reading = await self._read(
                "get_playback_peak_all", self._controller.get_playback_peak_all
            )
            samples.append(
                () if reading is None else tuple(float(value) for value in reading)
            )
        return samples

    async def _read_fader_db(self) -> float | None:
        """The reader ``hold_measurement_volume`` proves the level through.

        ``volume_latch.FADER_IO_ERRORS`` cannot name ``CamillaUnavailable``
        (that leaf may not import :mod:`jasper.camilla`), so it is translated
        here — the same translation ``web.correction_crossover_v2``'s own
        session-volume reader makes — and the unreadable fader lands on the
        fader refusal instead of escaping as a traceback.
        """

        try:
            return await self._controller.get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    async def _hold_fader(self, role: str) -> None:
        try:
            await self._plan.hold_measurement_volume(
                self._read_fader_db, context=f"bench:{role}"
            )
        except MeasurementFaderDrift as exc:
            raise BenchRefused(REFUSE_FADER, str(exc)) from exc

    async def _play_and_record(
        self,
        prepared: _Prepared,
        *,
        request: StimulusRequest,
        role: SweepOrSustain,
        graph_yaml: str,
    ) -> tuple[Any, Any, list[tuple[float, ...]]]:
        wav = prepared.wav
        rate = PROGRAM_SAMPLE_RATE_HZ
        pre_guard_s = prepared.pre_guard_s
        program_s = wav.frames / float(rate)
        budget_s = (
            program_s + pre_guard_s + WIRED_PRE_PLAY_ALLOWANCE_S + WIRED_POST_ROLL_S
        )
        poll: asyncio.Task[list[tuple[float, ...]]] | None = None

        async def _before_play(_program: Any, _artifact: Any, _phase: str) -> None:
            """The binder's hook: inside the writer lock, after the live-graph
            proof, immediately in front of aplay.

            The fader is proven HERE and not before the play, because the level
            every driver cap was judged against has to hold where the audio is
            emitted — a household "louder" still moves the main volume
            mid-session, and the pre-guard sleep, the re-admission over the
            hold's PCM and the sha verify all sit in between. R10(c)'s polls
            start in the same place, for the same reason.
            """

            nonlocal poll
            await self._hold_fader(role)
            poll = asyncio.create_task(self._poll_peaks(request))

        try:
            # Bound BEFORE the recorder is armed: the binder refuses an empty
            # graph, and that refusal must not strand a live ALSA device.
            seams = bind_program_playback_seams(
                self._controller,
                bundle_dir=str(self._sink.bundle_dir),
                artifact=prepared.artifact,
                config_dir=str(self._config_dir),
                program=prepared.program,
                wav_path=str(
                    self._sink.bundle_dir / prepared.artifact.relative_path
                ),
                topology=self._admission.topology,
                safety_profile=self._admission.safety_profile,
                role_targets=self._admission.role_targets,
                session_volume_db=prepared.commanded_db,
                declared_sensitivities=self._admission.declared_sensitivities,
                timeout_s=program_s + PLAYBACK_TIMEOUT_MARGIN_S,
                graph_yaml=graph_yaml,
                before_play=_before_play,
                lock_source=BENCH_LOCK_SOURCE,
            )
        except ValueError as exc:
            raise BenchRefused(REFUSE_PLAYBACK, str(exc)) from exc

        recorder = self._recorder_factory(rate, budget_s)
        try:
            await asyncio.to_thread(recorder.start)
        except (WiredCaptureError, OSError, ValueError) as exc:
            raise BenchRefused(REFUSE_RECORDER, str(exc)) from exc

        finished = False
        try:
            await self._sleep(pre_guard_s)
            try:
                result = await play_program(
                    prepared.program, session_volume_plan=self._plan, **seams
                )
            except ProgramPlaybackRefused as exc:
                raise BenchRefused(REFUSE_ADMISSION, str(exc)) from exc
            except (
                ProgramPlaybackError,
                SweepPlaybackError,
                WavSourceError,
                SessionVolumePlanError,
                CamillaUnavailable,
                OSError,
            ) as exc:
                raise BenchRefused(REFUSE_PLAYBACK, str(exc)) from exc
            live_peaks = [] if poll is None else await poll
            try:
                recording = await asyncio.to_thread(
                    recorder.finish, tail_s=WIRED_POST_ROLL_S
                )
            except (WiredCaptureError, OSError, ValueError) as exc:
                raise BenchRefused(REFUSE_CAPTURE, str(exc)) from exc
            finished = True
        finally:
            # Any escape must release the live ALSA device.
            if not finished:
                if poll is not None and not poll.cancel() and not poll.cancelled():
                    # `cancel()` False means "already done", which includes
                    # "already cancelled" — and `.exception()` on a cancelled
                    # task would raise over the exception in flight.
                    poll.exception()
                await asyncio.to_thread(recorder.abort)
        return recording, result, live_peaks

    def _analyze(
        self, prepared: _Prepared, *, role: SweepOrSustain, capture: np.ndarray
    ) -> SweepCaptureAnalysis | SustainCaptureAnalysis:
        if role == "sweep_transparency":
            return analyze_sweep_capture(
                capture=capture,
                program=prepared.program,
                segment_id=prepared.segment.segment_id,
                stimulus_body=prepared.wav.body(0),
                band=prepared.band,
                margin=self._margin,
                policy=self._policy,
                max_onset_s=prepared.max_onset_s,
            )
        return analyze_sustain_capture(
            capture=capture,
            sample_rate_hz=PROGRAM_SAMPLE_RATE_HZ,
            stimulus_body=prepared.wav.body(0),
            band=prepared.band,
            margin=self._margin,
            policy=self._policy,
            max_onset_s=prepared.max_onset_s,
        )

    def _bank_capture(
        self, recording: Any, *, target_id: str, tag: str
    ) -> tuple[ArtifactIdentity, dict[str, Any], np.ndarray]:
        """Mint, bank and decode one take, so the largest buffers this call
        holds are released before the analysis allocates its own."""

        answer = mint_wired_answer(recording, device=self._mic)
        capture_id = self._sink.write_bytes(
            f"{target_id}/{tag}-capture.wav",
            answer.wav,
            kind="jts_bass_extension_bench_acoustic_capture",
        )
        report = dict(answer.capture_integrity or {})
        self._sink.write_json(
            f"{target_id}/{tag}-capture-integrity.json",
            {"device": dict(answer.device or {}), "capture_integrity": report},
            kind="jts_bass_extension_bench_capture_integrity",
        )
        capture, capture_rate = decode_wav_to_mono(answer.wav)
        if int(capture_rate) != PROGRAM_SAMPLE_RATE_HZ:
            # Every analysis window below is measured at the program rate.
            raise BenchRefused(
                REFUSE_CAPTURE,
                f"the take decodes at {int(capture_rate)} Hz, not the "
                f"{PROGRAM_SAMPLE_RATE_HZ} Hz every analysis window assumes",
            )
        return capture_id, report, capture

    def _transparency(
        self,
        prepared: _Prepared,
        *,
        target_id: str,
        candidate: SweepCaptureAnalysis,
        reference: ReferenceSweepCapture,
    ) -> tuple[ArtifactIdentity, str]:
        path = (
            self._sink.bundle_dir / reference.reference_signal_analysis.relative_path
        )
        try:
            banked = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A missing or truncated reference artifact ends the target through
            # the refused arm with this capture banked, not as a traceback.
            raise CaptureUnanalyzable(
                f"the banked reference sweep analysis could not be read: {exc}"
            ) from exc
        freqs = np.asarray(candidate.freqs_hz, dtype=np.float64)
        reference_freqs = np.asarray(banked["freqs_hz"], dtype=np.float64)
        if reference_freqs.shape != freqs.shape or not np.allclose(
            reference_freqs, freqs
        ):
            raise CaptureUnanalyzable(
                "the banked reference sweep was read on a different frequency "
                "grid than this candidate"
            )
        verdict, rms_db, max_db = assess_transparency(
            freqs=freqs,
            candidate_response_db=np.asarray(
                candidate.fundamental_db, dtype=np.float64
            ),
            reference_response_db=np.asarray(
                banked["fundamental_db"], dtype=np.float64
            ),
            band=prepared.band,
            max_tracking_rms_db=self._policy.max_tracking_rms_db,
        )
        identity = self._sink.write_json(
            f"{target_id}/{prepared.tag}-transparency.json",
            {
                "verdict": verdict,
                "rms_db": rms_db,
                "max_db": max_db,
                "max_tracking_rms_db": float(self._policy.max_tracking_rms_db),
                "reference_capture": (
                    reference.reference_acoustic_capture.relative_path
                ),
            },
            kind="jts_bass_extension_bench_transparency_analysis",
        )
        return identity, verdict

    async def play(
        self,
        *,
        target: TargetPlan,
        role: SweepOrSustain,
        request: StimulusRequest,
        stop: Stop,
        stimulus: PaddedStimulus,
        artifact: ArtifactIdentity,
        tag: str,
        graph_yaml: str,
        reference: ReferenceSweepCapture | None = None,
    ) -> PlayedStimulus:
        """``graph_yaml`` is this pass's activation read-back
        (``ActivationReadback.active_config_raw``, banked by the runner): what
        ``confirm_graph_is_live`` re-proves inside the writer lock on every
        play."""

        stop.check()
        commanded = float(request.requested_commanded_main_volume_db)
        log_event(
            logger,
            "bass_extension.bench.play",
            action="start",
            role=role,
            target_id=target.target_id,
            commanded_main_volume_db=f"{commanded:.2f}",
        )
        if not fader_matches(commanded, self._admission.session_volume_db):
            raise BenchRefused(
                REFUSE_COMMANDED_VOLUME,
                f"the manifest commands {commanded:.2f} dB but the session was "
                f"admitted at {float(self._admission.session_volume_db):.2f} dB",
            )
        try:
            self._plan.assert_ready()
        except SessionVolumePlanError as exc:
            raise BenchRefused(REFUSE_VOLUME_NOT_OPEN, str(exc)) from exc
        # Read as the OBSERVED side so a plan holding no level refuses instead
        # of raising: `fader_matches` normalizes an unreadable reading to False.
        if not fader_matches(self._plan.measurement_volume_db, commanded):
            raise BenchRefused(
                REFUSE_COMMANDED_VOLUME,
                f"the manifest commands {commanded:.2f} dB but the session "
                f"volume plan is open at {self._plan.measurement_volume_db} dB",
            )
        prepared = self._prepare(
            target=target,
            role=role,
            request=request,
            stimulus=stimulus,
            artifact=artifact,
            tag=tag,
        )

        mux_start = await self._read("mux_status", self._read_mux_status)
        fanin_start = await self._read("fanin_status", self._read_fanin_status)
        clipped_before = int(
            await self._read("clipped_samples", self._controller.get_clipped_samples)
        )

        await self._floor.raise_to_level()
        fader_before_db, fader_before_muted = await self._read(
            "volume_and_mute", self._controller.get_volume_and_mute
        )

        recording, result, live_peaks = await self._play_and_record(
            prepared, request=request, role=role, graph_yaml=graph_yaml
        )

        fader_after_db, fader_after_muted = await self._read(
            "volume_and_mute", self._controller.get_volume_and_mute
        )
        clipped_after = int(
            await self._read("clipped_samples", self._controller.get_clipped_samples)
        )
        mux_end = await self._read("mux_status", self._read_mux_status)
        fanin_end = await self._read("fanin_status", self._read_fanin_status)

        target_id = target.target_id
        capture_id, report, capture = self._bank_capture(
            recording, target_id=target_id, tag=prepared.tag
        )
        # Tens of MB on a long hold, and the analysis below allocates its own.
        del recording
        admission_id = self._sink.write_json(
            f"{target_id}/{prepared.tag}-admission.json",
            result.admission.to_dict(),
            kind="jts_bass_extension_bench_admission",
        )
        faults = capture_faults(report)
        intact = not faults

        transparency_id: ArtifactIdentity | None = None
        transparency_verdict: str | None = None
        try:
            analysis = self._analyze(prepared, role=role, capture=capture)
            # A lossy capture's SNR and harmonic reads are not evidence, so the
            # take is demoted BEFORE it is banked: the row and the artifact
            # carry one verdict, not two that can disagree.
            quality_verdict = analysis.quality_verdict if intact else "fail"
            protection_verdict = analysis.protection_verdict if intact else "fail"
            signal_payload = dict(analysis.signal_dict())
            signal_payload["verdict"] = quality_verdict
            signal_payload["capture_intact"] = intact
            signal_payload["capture_faults"] = faults
            signal_id = self._sink.write_json(
                f"{target_id}/{prepared.tag}-signal.json",
                signal_payload,
                kind="jts_bass_extension_bench_signal_analysis",
            )
            protection_payload = dict(analysis.protection_dict())
            protection_payload["verdict"] = protection_verdict
            protection_id = self._sink.write_json(
                f"{target_id}/{prepared.tag}-protection.json",
                protection_payload,
                kind="jts_bass_extension_bench_protection_analysis",
            )
            if reference is not None and role == "sweep_transparency":
                transparency_id, transparency_verdict = self._transparency(
                    prepared,
                    target_id=target_id,
                    candidate=cast(SweepCaptureAnalysis, analysis),
                    reference=reference,
                )
                # Same demotion as the other two: a lossy capture's paired
                # comparison is not evidence either.
                if not intact:
                    transparency_verdict = "fail"
        except CaptureUnanalyzable as exc:
            # A capture the kernels cannot window (onset too close to the
            # head, shorter than the stimulus) ends the target through the
            # refused arm with its capture banked, never as a traceback.
            raise BenchRefused(REFUSE_UNANALYZABLE, str(exc)) from exc

        log_event(
            logger,
            "bass_extension.bench.play",
            result="played",
            role=role,
            target_id=target_id,
            snr_db=f"{analysis.snr_db:.2f}",
            quality_verdict=quality_verdict,
            protection_verdict=protection_verdict,
        )

        # The admitted cooldown is HONOURED here, not merely recorded on the
        # row: the next role's play starts as soon as this call returns.
        await self._sleep(float(request.requested_cooldown_s))
        return PlayedStimulus(
            admission=admission_id,
            acoustic_capture=capture_id,
            signal_analysis=signal_id,
            protection_analysis=protection_id,
            quality_verdict=quality_verdict,
            protection_verdict=protection_verdict,
            transparency_analysis=transparency_id,
            transparency_verdict=transparency_verdict,
            mux_status_start=mux_start,
            mux_status_end=mux_end,
            fanin_status_start=fanin_start,
            fanin_status_end=fanin_end,
            fader_before_db=float(fader_before_db),
            fader_before_muted=bool(fader_before_muted),
            fader_after_db=float(fader_after_db),
            fader_after_muted=bool(fader_after_muted),
            clipped_samples_before=clipped_before,
            clipped_samples_after=clipped_after,
            live_peak_all_samples=tuple(tuple(sample) for sample in live_peaks),
            stimulus_effective_peak_dbfs=prepared.peak_dbfs + commanded,
            commanded_main_volume_db=commanded,
            target_boost_db=float(target.boost_headroom_db),
            hold_duration_s=(
                (prepared.wav.body_end - prepared.wav.body_start)
                / PROGRAM_SAMPLE_RATE_HZ
            ),
            required_cooldown_s=float(request.requested_cooldown_s),
            repeat_count=int(request.requested_repeat_count),
        )
