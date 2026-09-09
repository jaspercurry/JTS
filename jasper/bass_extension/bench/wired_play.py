# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bench's :class:`~jasper.bass_extension.bench.executor.PlayAndCapture`.

Shaped exactly like ``jasper-null``'s ``_play_and_capture``
(:mod:`jasper.cli.null_door`): the engine's own play path — ``play_program``
over a FRESH re-admission of the rendered WAV, under the DSP writer lock, into
verified aplay — wrapped by the wired capture kernel (a
:class:`~jasper.audio_measurement.wired_capture.WiredRecorder` armed before any
audio, ``mint_wired_answer`` after). There is no second recorder and no second
admission: the pieces this bench needs already exist and are consumed here.

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

from jasper.active_speaker.crossover_v2.door import give_back
from jasper.active_speaker.crossover_v2.volume_claim import MeasurementVolumeClaim
from jasper.active_speaker.program_admission import readmit_program_from_wav
from jasper.active_speaker.program_playback import (
    ProgramPlaybackError,
    ProgramPlaybackRefused,
    play_program,
    verified_program_aplay,
)
from jasper.active_speaker.restore_wait import resilient_restore
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
from jasper.dsp_apply import dsp_writer_lock
from jasper.log_event import log_event
from jasper.measurement_window import MEASUREMENT_GATE_OWNER, measurement_window
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

#: ``live_proof.prove_isolation`` proves the WIZARD's gate owner, so the bench
#: holds the gate under that same identity. A bench-specific identity waits on
#: ``live_proof`` taking an owner parameter; declaring one here would make every
#: R6a(i) proof fail against a gate the bench genuinely held.
BENCH_GATE_OWNER = MEASUREMENT_GATE_OWNER

BENCH_LOCK_SOURCE = "bass_extension_bench"

#: Noise-floor window recorded before a sustain hold (a hold has no harmonic
#: images to clear, so it needs a floor window and nothing more).
SUSTAIN_PRE_ROLL_S = 1.0

PLAYBACK_TIMEOUT_MARGIN_S = 15.0

REFUSE_COMMANDED_VOLUME = "bench_commanded_volume_mismatch"
REFUSE_OWNER_ROLE = "bench_owner_role_unresolved"
REFUSE_BAND = "bench_stimulus_band_unprotected"
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

_LEAD_IN_SEGMENT_ID = "bench_lead_in"
_LEAD_OUT_SEGMENT_ID = "bench_lead_out"
_INT16_FULL_SCALE = 32767.0


def controller_fader_reader(controller: Any) -> GetMainVolumeDb:
    """The reader every bench collaborator proves the fader level through.

    ``volume_latch.FADER_IO_ERRORS`` cannot name ``CamillaUnavailable`` (that
    leaf may not import :mod:`jasper.camilla`), so it is translated here — the
    same translation ``web.correction_crossover_v2``'s own session-volume
    reader makes — and the unreadable fader lands on the fader refusal instead
    of escaping as a traceback. The floor control, the volume door and the play
    seam all read through this one function.
    """

    async def read() -> float | None:
        try:
            return await controller.get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return read


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
        async with self._window(gate_owner=BENCH_GATE_OWNER):
            # A leftover ACTIVE record past its own ceiling is a crashed run;
            # `plan.open` refuses over it unless it is drained first.
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
    one of them itself.
    """

    topology: Any
    safety_profile: Mapping[str, Any]
    role_targets: Mapping[str, str]
    session_volume_db: float
    declared_sensitivities: Mapping[str, float]
    preset: Any


def bass_owner_role(preset: Any, owner_channels: Sequence[int]) -> str:
    """The declared driver role the bass-extension owner channels belong to."""

    owner = frozenset(int(channel) for channel in owner_channels)
    local_sub = getattr(preset, "local_subwoofer", None)
    if local_sub is not None and int(local_sub.physical_output_index) in owner:
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
    preset: Any, *, owner_role: str, band: tuple[float, float]
) -> None:
    """Refuse a stimulus the owner's crossover does not keep to itself.

    The played artifact is a 2-channel mix that reaches EVERY driver through
    the installed graph, while admission evaluates only the bass owner's caps.
    So the band is admissible only where the owner's own crossover is what
    stands between the stimulus and every other driver:

    * no driver may sit BELOW the owner (a region whose upper driver is the
      owner, or a local subwoofer under a non-sub owner) — that driver takes
      the stimulus through its low-pass with no cap evaluated for it;
    * the band's top may not pass the lowest corner above the owner, since
      past it the driver above is being driven, not attenuated.

    An owner with nothing above it (a single-driver main) has no upper bound.
    """

    regions = preset.crossover_regions
    local_sub = getattr(preset, "local_subwoofer", None)
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
            "band through its own low-pass, against no evaluated cap",
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
    # At the corner itself a Linkwitz-Riley pair is only 6 dB down, so the
    # band must stop short of it, not at it.
    if corners and float(band[1]) >= min(corners):
        raise BenchRefused(
            REFUSE_BAND,
            f"the {band[0]:g}-{band[1]:g} Hz stimulus reaches the {owner_role}'s "
            f"{min(corners):g} Hz corner, so the driver above it is driven "
            "rather than protected",
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
    readmit: Callable[[], Awaitable[Any]]
    tag: str
    band: tuple[float, float]
    peak_dbfs: float


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
        play_wav: Callable[[ArtifactIdentity, float], Awaitable[Any]] | None = None,
        recorder_factory: Callable[[int, float], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sink = sink
        self._controller = controller
        self._read_fader_db = controller_fader_reader(controller)
        self._mic = mic
        self._plan = plan
        self._floor = floor
        self._admission = admission
        self._margin = margin
        self._policy = policy
        self._config_dir = config_dir
        self._read_mux_status = read_mux_status
        self._read_fanin_status = read_fanin_status
        self._play_wav = play_wav or self._verified_aplay
        self._recorder_factory = recorder_factory or self._wired_recorder
        self._sleep = sleep

    async def _verified_aplay(
        self, artifact: ArtifactIdentity, timeout_s: float
    ) -> Any:
        return await verified_program_aplay(
            self._sink.bundle_dir, artifact, timeout_s=timeout_s
        )

    def _wired_recorder(self, rate: int, budget_s: float) -> Any:
        return make_wired_recorder(
            self._mic, sample_rate_hz=rate, max_capture_s=budget_s
        )

    def _prepare(
        self, *, target: TargetPlan, request: StimulusRequest,
        stimulus: PaddedStimulus, artifact: ArtifactIdentity, tag: str,
    ) -> _Prepared:
        wav = read_stimulus_wav(stimulus)
        role_name = bass_owner_role(self._admission.preset, target.owner_channels)
        band = (
            float(request.requested_stimulus_band_hz[0]),
            float(request.requested_stimulus_band_hz[1]),
        )
        assert_stimulus_band_protected(
            self._admission.preset, owner_role=role_name, band=band
        )
        commanded = float(request.requested_commanded_main_volume_db)
        program = describe_stimulus_program(
            wav,
            role=role_name,
            band=band,
            peak_dbfs=float(request.requested_stimulus_effective_peak_dbfs),
            commanded_main_volume_db=commanded,
        )
        readmit = partial(
            asyncio.to_thread,
            partial(
                readmit_program_from_wav,
                program,
                self._sink.bundle_dir / artifact.relative_path,
                topology=self._admission.topology,
                safety_profile=self._admission.safety_profile,
                role_targets=self._admission.role_targets,
                session_volume_db=commanded,
                # MUST match what the session composed against: readmission
                # re-resolves every cap.
                declared_sensitivities=self._admission.declared_sensitivities,
            ),
        )
        return _Prepared(
            wav=wav,
            artifact=artifact,
            program=program,
            segment=program.segment(stimulus_segment_id(role_name, 0)),
            readmit=readmit,
            tag=tag,
            band=band,
            peak_dbfs=wav.peak_dbfs(0),
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

    async def _hold_fader(self, role: str) -> None:
        try:
            await self._plan.hold_measurement_volume(
                self._read_fader_db, context=f"bench:{role}"
            )
        except MeasurementFaderDrift as exc:
            raise BenchRefused(REFUSE_FADER, str(exc)) from exc

    async def _play_and_record(
        self, prepared: _Prepared, *, request: StimulusRequest, role: SweepOrSustain
    ) -> tuple[Any, Any, list[tuple[float, ...]]]:
        wav = prepared.wav
        rate = PROGRAM_SAMPLE_RATE_HZ
        pre_guard_s = (
            sweep_pre_roll_s(prepared.segment)
            if role == "sweep_transparency"
            else SUSTAIN_PRE_ROLL_S
        )
        program_s = wav.frames / float(rate)
        budget_s = (
            program_s + pre_guard_s + WIRED_PRE_PLAY_ALLOWANCE_S + WIRED_POST_ROLL_S
        )
        recorder = self._recorder_factory(rate, budget_s)
        try:
            # Armed BEFORE any audio: `start` blocks until the first real chunk
            # lands, so the pre-roll is a fact rather than a hope.
            await asyncio.to_thread(recorder.start)
        except (WiredCaptureError, OSError, ValueError) as exc:
            raise BenchRefused(REFUSE_RECORDER, str(exc)) from exc

        poll: asyncio.Task[list[tuple[float, ...]]] | None = None

        async def _play_wav_polled() -> Any:
            # R10(c)'s polls must sample the AUDIO. Started here, immediately
            # in front of the aplay awaitable, because everything play_program
            # does first — re-admission, the writer lock, the sha verify —
            # would otherwise consume the whole poll budget before a frame is
            # emitted.
            nonlocal poll
            poll = asyncio.create_task(self._poll_peaks(request))
            return await self._play_wav(
                prepared.artifact, program_s + PLAYBACK_TIMEOUT_MARGIN_S
            )

        finished = False
        try:
            await self._sleep(pre_guard_s)
            try:
                result = await play_program(
                    prepared.program,
                    session_volume_plan=self._plan,
                    readmit=prepared.readmit,
                    play_wav=_play_wav_polled,
                    writer_lock=partial(
                        dsp_writer_lock, self._config_dir, source=BENCH_LOCK_SOURCE
                    ),
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
                if poll is not None and not poll.cancel():
                    poll.exception()
                recorder.abort()
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
            )
        return analyze_sustain_capture(
            capture=capture,
            sample_rate_hz=PROGRAM_SAMPLE_RATE_HZ,
            stimulus_body=prepared.wav.body(0),
            band=prepared.band,
            margin=self._margin,
            policy=self._policy,
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
        capture, _ = decode_wav_to_mono(answer.wav)
        return capture_id, report, capture

    def _transparency(
        self,
        prepared: _Prepared,
        *,
        target_id: str,
        candidate: SweepCaptureAnalysis,
        reference: ReferenceSweepCapture,
    ) -> tuple[ArtifactIdentity, str]:
        banked = json.loads(
            (
                self._sink.bundle_dir
                / reference.reference_signal_analysis.relative_path
            ).read_text(encoding="utf-8")
        )
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
        reference: ReferenceSweepCapture | None = None,
    ) -> PlayedStimulus:
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
        await self._hold_fader(role)
        fader_before_db, fader_before_muted = await self._read(
            "volume_and_mute", self._controller.get_volume_and_mute
        )

        recording, result, live_peaks = await self._play_and_record(
            prepared, request=request, role=role
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
