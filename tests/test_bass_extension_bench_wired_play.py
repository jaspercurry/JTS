# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``WiredPlayAndCapture`` — the bench's one hardware seam, wired to fakes.

The volume rig is real throughout (a real ``VolumeOwner`` over a fake fader, a
real ``SessionVolumePlan`` on a tmp state file, a real
``MeasurementVolumeClaim``/``OwnerVolumeDoor``), and so are the admission, the
program description, the capture kernel (a real ``WiredRecorder`` over a
scripted PCM) and every analysis. Only the ALSA device, the aplay transport and
the CamillaDSP controller are doubles.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from jasper.active_speaker.crossover_v2.volume_claim import (
    MeasurementVolumeClaim,
    OwnerVolumeDoor,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_admission import (
    ProgramAdmissionRefusal,
    readmit_program_from_wav,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlan
from jasper.active_speaker.volume_latch import EMERGENCY_MEASUREMENT_VOLUME_DB
from jasper.audio_measurement.frame_ledger import REPORT_KEY_RENDER_GAPS
from jasper.audio_measurement.wired_capture import WiredMicDevice, WiredRecorder
from jasper.bass_extension.bench import (
    activation,
    derivation,
    executor,
    render,
    stimulus as stimulus_mod,
    wired_play,
)
from jasper.bass_extension.bench.analysis import MeasurementPolicy
from jasper.bass_extension.bench.context import (
    DETECTOR_REFERENCE,
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
    limiter_domain_fingerprint,
)
from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    StimulusRequest,
    author_campaign_manifest,
)
from jasper.bass_extension.bench.runner import (
    BenchDeps,
    BenchRefused,
    ReferenceSweepCapture,
    Stop,
    run_campaign,
)
from jasper.bass_extension.bench.sink import BundleSink
from jasper.bass_extension.targets import MARGINS
from jasper.volume_owner import VolumeOwner
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_program_admission import _profile_and_targets
from jasper.camilla import CamillaUnavailable
from tests.test_bass_extension_bench_executor import (
    OWNER_CHANNELS,
    TARGET_ID,
    FakeController,
    _fanin_status,
    _live_yaml,
    _mux_status,
    _round_trip_render_config,
    _sha,
    _target_plan,
)
from tests.wired_capture_fixtures import FakePcm

RATE = 48_000
BAND = (100.0, 400.0)
HOLD_S = 1.0
LEVEL_DB = -35.0
HOUSEHOLD_DB = -20.0
POLICY = MeasurementPolicy(min_snr_db=25.0, max_tracking_rms_db=1.0)
MARGIN = MARGINS["conservative"]
MIC = WiredMicDevice(
    card_id="UMIK2", card_index=1, usb_id="2752:0072",
    model_key="umik2", model_label="UMIK-2",
)


@pytest.fixture(autouse=True)
def _stub_post_limiter_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph proofs have their own tests; these focus on the play path."""

    for module in (derivation, activation):
        monkeypatch.setattr(
            module,
            "_prove_active_graph",
            lambda config, proof: proof.expected_clip_limit_dbfs,
        )


def _request(**overrides: Any) -> StimulusRequest:
    """A short, narrow-band request: the executor suite's own differs in band,
    hold and poll count, all of which this suite's captures are sized by."""

    fields: dict[str, Any] = {
        "requested_stimulus_band_hz": BAND,
        "requested_stimulus_effective_peak_dbfs": -20.0,
        "requested_commanded_main_volume_db": LEVEL_DB,
        "requested_hold_duration_s": HOLD_S,
        "requested_cooldown_s": 0.25,
        "requested_repeat_count": 1,
        "stimulus_generator_identity": "bench-noise-v1",
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 1 << 29,
        "render_rlimit_cpu_s": 30,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.01,
        "cross_check_read_count": 5,
        "cross_check_tolerance_db": 3.0,
    }
    fields.update(overrides)
    return StimulusRequest(**fields)


def _preset(
    *,
    woofer_indexes=OWNER_CHANNELS,
    sub_index: int | None = None,
    crossover_fc_hz: float | None = None,
):
    raw = _two_way_preset("stereo")
    tweeter_indexes = [i for i in range(4) if i not in tuple(woofer_indexes)]
    woofers, tweeters = list(woofer_indexes), list(tweeter_indexes)
    for output in raw["channel_map"]["outputs"]:
        pool = woofers if output["driver_role"] == "woofer" else tweeters
        output["index"] = pool.pop(0)
    if crossover_fc_hz is not None:
        raw["crossover_regions"][0]["fc_hz"] = crossover_fc_hz
    if sub_index is not None:
        raw["local_subwoofer"] = {
            "physical_output_index": sub_index,
            "label": "Subwoofer",
            "crossover_fc_hz": 80.0,
        }
    return ActiveSpeakerPreset.from_mapping(raw)


def _admission_context(
    *,
    session_volume_db: float = LEVEL_DB,
    preset_kwargs: dict[str, Any] | None = None,
    **profile_overrides: Any,
):
    topology, profile, targets = _profile_and_targets(
        woofer_floor=20.0, **profile_overrides
    )
    return wired_play.AdmissionContext(
        topology=topology,
        safety_profile=profile,
        role_targets=targets,
        session_volume_db=session_volume_db,
        declared_sensitivities={},
        preset=_preset(**(preset_kwargs or {})),
    )


# --------------------------------------------------------------------------- #
# The fader rig: real owner / claim / plan / door over a fake fader
# --------------------------------------------------------------------------- #


class FakeFader:
    def __init__(self, level_db: float = HOUSEHOLD_DB) -> None:
        self.level_db = float(level_db)
        self.muted = False

    async def get(self) -> float:
        return self.level_db

    async def set(self, value: float) -> bool:
        self.level_db = float(value)
        return True


@asynccontextmanager
async def _noop_window(*, gate_owner: str):
    yield None


def _volume_rig(tmp_path: Path, *, level_db: float = LEVEL_DB):
    fader = FakeFader()
    owner = VolumeOwner(set_fader_db=fader.set, get_fader_db=fader.get)
    claim = MeasurementVolumeClaim(owner)
    door = OwnerVolumeDoor(owner, read_fader=fader.get, claim=claim)
    plan = SessionVolumePlan(state_path=tmp_path / "session_volume.json")
    floor = wired_play.ClaimFloorControl(
        owner, read_fader=fader.get, level_db=level_db
    )
    window = wired_play.BenchWindow(
        plan=plan,
        claim=claim,
        door=door,
        floor=floor,
        level_db=level_db,
        window=_noop_window,
    )
    return SimpleNamespace(
        fader=fader, claim=claim, plan=plan, floor=floor, window=window
    )


class FakeCamilla(FakeController):
    """The activation seam's controller, plus the R4(a)/R10 read surfaces."""

    def __init__(self, fader: FakeFader, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fader = fader
        self.clipped_samples = 0
        self.playback_peaks: list[float] | None = [-99.0, -99.0, -20.0, -20.0]
        self.peak_reads = 0
        #: Read surfaces that answer with ``CamillaUnavailable`` instead.
        self.unavailable: set[str] = set()

    def _guard(self, surface: str) -> None:
        if surface in self.unavailable:
            raise CamillaUnavailable(surface)

    async def get_volume_db(self, *, best_effort: bool = False) -> float:
        self._guard("get_volume_db")
        return self._fader.level_db

    async def get_volume_and_mute(self) -> tuple[float, bool]:
        return self._fader.level_db, self._fader.muted

    async def get_clipped_samples(self) -> int:
        self._guard("get_clipped_samples")
        return self.clipped_samples

    async def get_playback_peak_all(self) -> list[float] | None:
        self._guard("get_playback_peak_all")
        self.peak_reads += 1
        return None if self.playback_peaks is None else list(self.playback_peaks)


# --------------------------------------------------------------------------- #
# The capture rig: a real WiredRecorder over a script the seams write into
# --------------------------------------------------------------------------- #

#: The fake speaker's own low corner, INSIDE the played band so the sustain
#: reading has a knee to find, and the second-order term a real driver adds: a
#: perfectly linear plant leaves every harmonic point on the measurement floor,
#: which proves nothing about protection.
_CORNER_HZ = 150.0
_SECOND_ORDER = 0.02


class _SharedScriptPcm(FakePcm):
    """:class:`FakePcm` over a script the play seams append to mid-capture.

    Every frame it hands back carries a floor: a real mic records noise, and an
    unbroken run of exact digital zeros is the dropout signature the capture
    integrity report grades.
    """

    def __init__(self, script: list) -> None:
        super().__init__(())
        self._script = script
        self._rng = np.random.default_rng(101)

    def read(self):
        frames, payload = super().read()
        if frames <= 0:
            return frames, payload
        samples = np.frombuffer(payload, dtype="<i4").reshape(-1, 2).copy()
        floor = self._rng.integers(1, 4, samples.shape[0], dtype=np.int32)
        samples[:, 0] = np.where(samples[:, 0] == 0, floor, samples[:, 0])
        return frames, samples.tobytes()


class CaptureRig:
    """``sleep`` writes silence into the capture; ``play_wav`` writes the played
    stimulus through the fake speaker below — so the recorded pre-roll is
    exactly the time the seam under test waited before playing.

    ``silence_scale`` shortens what a wait actually records, which is how a
    capture whose onset lands too early for the harmonic windows is staged.
    """

    def __init__(self, sink: BundleSink, controller: "FakeCamilla") -> None:
        self._sink = sink
        self._controller = controller
        self.script: list = []
        self.pcm: _SharedScriptPcm | None = None
        self.played: list[Any] = []
        self.silence_scale = 1.0
        self.polls_at_play_start: int | None = None

    def recorder_factory(self, rate: int, budget_s: float) -> WiredRecorder:
        self.script = []

        def _pcm() -> _SharedScriptPcm:
            self.pcm = _SharedScriptPcm(self.script)
            return self.pcm

        return WiredRecorder(
            "fake:wired",
            sample_rate_hz=rate,
            channels=2,
            max_capture_s=budget_s,
            pcm_factory=_pcm,
        )

    async def sleep(self, seconds: float) -> None:
        frames = int(round(float(seconds) * self.silence_scale * RATE))
        if frames > 0:
            self.script.append((frames, [(0, 0)] * frames))
        await asyncio.sleep(0)

    async def play_wav(self, artifact: Any, timeout_s: float) -> Any:
        self.polls_at_play_start = self._controller.peak_reads
        self.played.append(artifact)
        path = self._sink.bundle_dir / artifact.relative_path
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            pcm = np.frombuffer(
                source.readframes(source.getnframes()), dtype="<i2"
            ).reshape(-1, channels)
        emitted = self._speaker(pcm[:, 0].astype(np.float64) / 32767.0)
        scaled = np.clip(emitted, -1.0, 1.0) * float(np.iinfo(np.int32).max)
        values = [(int(value), 0) for value in scaled]
        self.script.append((len(values), values))
        return SimpleNamespace(emission="completed")

    def _speaker(self, samples: np.ndarray) -> np.ndarray:
        shaped = sosfilt(
            butter(2, _CORNER_HZ / (RATE / 2.0), btype="highpass", output="sos"),
            samples,
        )
        return shaped + _SECOND_ORDER * shaped**2


def _padding_minima(lead_in: int = 4_800, lead_out: int = 4_800):
    return stimulus_mod.PaddingMinima(
        lead_in_samples=lead_in,
        lead_out_samples=lead_out,
        lead_in_time_constant_component_samples=lead_in,
        lead_in_conv_component_samples=0,
        lead_out_delay_component_samples=lead_out,
        lead_out_conv_component_samples=0,
    )


def _padded(sink: BundleSink, *, role: str, request: StimulusRequest):
    """The executor's own R6 pipeline: generate, duplicate to stereo, pad, bank."""

    target_dir = sink.bundle_dir / TARGET_ID
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_path = executor._generate_stimulus_wav(
        role=role, request=request, sample_rate_hz=RATE, target_dir=target_dir
    )
    padded = stimulus_mod.pad_stimulus_wav(raw_path, minima=_padding_minima())
    identity = sink.write_bytes(
        f"{TARGET_ID}/{role}-padded.wav",
        padded.wav_bytes,
        kind="jts_bass_extension_bench_padded_stimulus",
    )
    return padded, identity


def _seam(tmp_path: Path, *, admission=None, config_dir: Path | None = None):
    sink = BundleSink(tmp_path / "bundle", bundle_id="play")
    volume = _volume_rig(tmp_path)
    controller = FakeCamilla(volume.fader)
    rig = CaptureRig(sink, controller)
    seam = wired_play.WiredPlayAndCapture(
        sink=sink,
        controller=controller,
        mic=MIC,
        plan=volume.plan,
        floor=volume.floor,
        admission=admission if admission is not None else _admission_context(),
        margin=MARGIN,
        policy=POLICY,
        config_dir=str(config_dir or tmp_path),
        read_mux_status=_read_mux,
        read_fanin_status=_read_fanin,
        play_wav=rig.play_wav,
        recorder_factory=rig.recorder_factory,
        sleep=rig.sleep,
    )
    return SimpleNamespace(
        sink=sink, volume=volume, controller=controller, rig=rig, seam=seam
    )


async def _play(
    pieces,
    *,
    role: str = "sweep_transparency",
    request: StimulusRequest | None = None,
    rendered: StimulusRequest | None = None,
    reference: ReferenceSweepCapture | None = None,
):
    """Play one stimulus through the seam. ``rendered`` generates the BYTES from
    a different request than the one the play is authorized against."""

    request = request or _request()
    padded, identity = _padded(pieces.sink, role=role, request=rendered or request)
    async with pieces.volume.window():
        return await pieces.seam.play(
            target=_target_plan(),
            role=role,
            request=request,
            stop=Stop(),
            stimulus=padded,
            artifact=identity,
            tag=role,
            reference=reference,
        )


async def _read_mux() -> dict:
    return _mux_status()


async def _read_fanin() -> dict:
    return _fanin_status()


def _read_json(sink: BundleSink, relative_path: str) -> dict:
    return json.loads((sink.bundle_dir / relative_path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# ClaimFloorControl
# --------------------------------------------------------------------------- #


async def test_floor_control_ducks_under_the_claim_and_gives_it_back(
    tmp_path: Path,
) -> None:
    volume = _volume_rig(tmp_path)
    await volume.claim.acquire(LEVEL_DB)
    assert volume.fader.level_db == pytest.approx(LEVEL_DB)

    await volume.floor.to_floor()
    await volume.floor.to_floor()  # idempotent: one duck, not two
    assert volume.fader.level_db <= EMERGENCY_MEASUREMENT_VOLUME_DB
    await volume.floor.assert_at_floor()

    await volume.floor.raise_to_level()
    assert volume.fader.level_db == pytest.approx(LEVEL_DB)


async def test_floor_control_refuses_when_the_fader_is_above_the_floor(
    tmp_path: Path,
) -> None:
    volume = _volume_rig(tmp_path)
    await volume.claim.acquire(LEVEL_DB)

    with pytest.raises(BenchRefused) as raised:
        await volume.floor.assert_at_floor()
    assert raised.value.reason == wired_play.REFUSE_NOT_AT_FLOOR


# --------------------------------------------------------------------------- #
# BenchWindow
# --------------------------------------------------------------------------- #


async def test_bench_window_opens_at_the_level_and_gives_the_speaker_back(
    tmp_path: Path,
) -> None:
    volume = _volume_rig(tmp_path)

    async with volume.window():
        assert volume.fader.level_db == pytest.approx(LEVEL_DB)
        assert volume.plan.measurement_volume_db == pytest.approx(LEVEL_DB)
    assert volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)

    # A second open after a clean close works.
    async with volume.window():
        assert volume.fader.level_db == pytest.approx(LEVEL_DB)
    assert volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)


async def test_bench_window_gives_back_when_the_body_raises(tmp_path: Path) -> None:
    volume = _volume_rig(tmp_path)

    with pytest.raises(RuntimeError):
        async with volume.window():
            await volume.floor.to_floor()
            raise RuntimeError("body failed")

    assert volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)
    assert volume.plan.measurement_volume_db is None


# --------------------------------------------------------------------------- #
# bass_owner_role / read_stimulus_wav / describe_stimulus_program
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("preset_kwargs", "owner_channels", "expected"),
    [
        ({"woofer_indexes": (2, 3)}, (2, 3), "woofer"),
        ({"woofer_indexes": (0, 2)}, (0, 2), "woofer"),
        ({"woofer_indexes": (0, 2), "sub_index": 4}, (4,), "subwoofer"),
    ],
)
def test_bass_owner_role_resolves_the_declared_role(
    preset_kwargs: dict, owner_channels: tuple[int, ...], expected: str
) -> None:
    assert wired_play.bass_owner_role(_preset(**preset_kwargs), owner_channels) == expected


def test_bass_owner_role_refuses_channels_that_match_no_single_role() -> None:
    with pytest.raises(BenchRefused) as raised:
        wired_play.bass_owner_role(_preset(woofer_indexes=(2, 3)), (1, 2))
    assert raised.value.reason == wired_play.REFUSE_OWNER_ROLE


def test_described_program_is_admitted_from_the_padded_wav(tmp_path: Path) -> None:
    sink = BundleSink(tmp_path / "bundle", bundle_id="describe")
    request = _request()
    padded, identity = _padded(sink, role="sweep_transparency", request=request)
    wav = wired_play.read_stimulus_wav(padded)
    program = wired_play.describe_stimulus_program(
        wav,
        role="woofer",
        band=BAND,
        peak_dbfs=request.requested_stimulus_effective_peak_dbfs,
        commanded_main_volume_db=LEVEL_DB,
    )

    # ProgramSegment's vocabulary: the digital peak the request authorized,
    # plus the fader the play commands.
    segment = next(iter(program.stimulus_segments()))
    assert segment.gain_db == pytest.approx(
        request.requested_stimulus_effective_peak_dbfs
    )
    assert segment.effective_peak_dbfs == pytest.approx(
        request.requested_stimulus_effective_peak_dbfs + LEVEL_DB
    )

    assert wav.body_start > 0 and wav.body_end < wav.frames
    assert program.channels == 2
    assert {segment.role for segment in program.stimulus_segments()} == {"woofer"}

    context = _admission_context()
    admission = readmit_program_from_wav(
        program,
        sink.bundle_dir / identity.relative_path,
        topology=context.topology,
        safety_profile=context.safety_profile,
        role_targets=context.role_targets,
        session_volume_db=LEVEL_DB,
        declared_sensitivities=context.declared_sensitivities,
    )
    assert admission.allowed, admission.refusals


def _wav_bytes(*, sample_width: int = 2, sample_rate: int = RATE, frames: int = 128):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(sample_width)
        out.setframerate(sample_rate)
        out.writeframes(b"\x01" * (2 * sample_width * frames))
    return buffer.getvalue()


def _padded_double(
    *, sample_width: int = 2, sample_rate: int = RATE,
    frames: int = 128, body_frames: int = 128,
) -> stimulus_mod.PaddedStimulus:
    return stimulus_mod.PaddedStimulus(
        wav_bytes=_wav_bytes(
            sample_width=sample_width, sample_rate=sample_rate, frames=frames
        ),
        lead_in_frames=0,
        lead_out_frames=0,
        body_frames=body_frames,
        sample_rate_hz=sample_rate,
        channels=2,
        sample_width_bytes=sample_width,
    )


@pytest.mark.parametrize(
    ("sample_width", "sample_rate"), [(4, RATE), (2, 44_100)]
)
def test_read_stimulus_wav_refuses_a_foreign_container(
    sample_width: int, sample_rate: int
) -> None:
    with pytest.raises(BenchRefused) as raised:
        wired_play.read_stimulus_wav(
            _padded_double(sample_width=sample_width, sample_rate=sample_rate)
        )
    assert raised.value.reason == wired_play.REFUSE_STIMULUS


@pytest.mark.parametrize("body_frames", [0, 64])
def test_read_stimulus_wav_refuses_padding_that_misses_the_bytes(
    body_frames: int,
) -> None:
    with pytest.raises(BenchRefused) as raised:
        wired_play.read_stimulus_wav(_padded_double(body_frames=body_frames))
    assert raised.value.reason == wired_play.REFUSE_STIMULUS


# --------------------------------------------------------------------------- #
# play()
# --------------------------------------------------------------------------- #


async def _play_one(tmp_path: Path, *, role: str = "sweep_transparency"):
    pieces = _seam(tmp_path)
    played = await _play(pieces, role=role)
    return SimpleNamespace(
        played=played, sink=pieces.sink, rig=pieces.rig,
        path=pieces.sink.bundle_dir / f"{TARGET_ID}/{role}-padded.wav",
    )


async def test_play_returns_the_full_played_stimulus_evidence(tmp_path: Path) -> None:
    outcome = await _play_one(tmp_path)
    played, sink, rig = outcome.played, outcome.sink, outcome.rig

    # B3: the artifact handed to the transport is byte-identical to the file
    # the executor padded — the seam never regenerates stimulus bytes.
    assert len(rig.played) == 1
    handed = sink.bundle_dir / rig.played[0].relative_path
    assert handed.read_bytes() == outcome.path.read_bytes()
    assert rig.played[0].byte_size == outcome.path.stat().st_size

    # R10(c): exactly the manifest's recorded poll count, and every one of
    # them taken across the AUDIO — none had been read when the transport was
    # handed the artifact, all of them by the time the play returned.
    assert rig.polls_at_play_start == 0
    assert len(played.live_peak_all_samples) == _request().cross_check_read_count

    # R4(a): the fader bracket sits on the one commanded measurement level.
    assert played.fader_before_db == pytest.approx(LEVEL_DB)
    assert played.fader_after_db == pytest.approx(LEVEL_DB)
    assert played.commanded_main_volume_db == pytest.approx(LEVEL_DB)
    assert played.fader_before_muted is False

    assert played.quality_verdict == "pass"
    assert played.protection_verdict == "pass"
    assert played.transparency_verdict is None
    assert played.repeat_count == 1
    assert played.hold_duration_s > 0.0

    for identity in (
        played.admission,
        played.acoustic_capture,
        played.signal_analysis,
        played.protection_analysis,
    ):
        assert (sink.bundle_dir / identity.relative_path).is_file()

    integrity = _read_json(sink, f"{TARGET_ID}/sweep_transparency-capture-integrity.json")
    assert integrity["device"]["card"] == MIC.card_id
    assert integrity["capture_integrity"][REPORT_KEY_RENDER_GAPS] == 0
    signal = _read_json(sink, f"{TARGET_ID}/sweep_transparency-signal.json")
    assert signal["capture_intact"] is True
    assert signal["capture_faults"] == []
    assert signal["verdict"] == "pass"


async def test_play_grades_a_sustain_hold_through_the_sustain_analysis(
    tmp_path: Path,
) -> None:
    outcome = await _play_one(tmp_path, role="sustain_stress")

    protection = _read_json(
        outcome.sink, f"{TARGET_ID}/sustain_stress-protection.json"
    )
    assert "sag_db" in protection and "start_corner_hz" in protection
    assert outcome.played.protection_verdict == "pass"
    assert outcome.played.quality_verdict == "pass"


async def test_play_scores_transparency_against_the_banked_reference(
    tmp_path: Path,
) -> None:
    reference_outcome = await _play_one(tmp_path / "reference")
    reference = ReferenceSweepCapture(
        reference_stimulus=reference_outcome.played.admission,
        reference_admission=reference_outcome.played.admission,
        reference_acoustic_capture=reference_outcome.played.acoustic_capture,
        reference_signal_analysis=reference_outcome.played.signal_analysis,
    )
    # The candidate sink must hold the reference's own signal analysis at the
    # relative path the identity names — the runner keeps both phases in one
    # bundle, and the candidate reads that curve rather than re-deriving it.
    pieces = _seam(tmp_path / "candidate")
    banked = reference.reference_signal_analysis.relative_path
    pieces.sink.write_bytes(
        banked,
        (reference_outcome.sink.bundle_dir / banked).read_bytes(),
        kind="jts_bass_extension_bench_signal_analysis",
    )

    played = await _play(pieces, reference=reference)

    assert played.transparency_verdict == "pass"
    assert played.transparency_analysis is not None
    transparency = _read_json(
        pieces.sink, f"{TARGET_ID}/sweep_transparency-transparency.json"
    )
    assert transparency["max_tracking_rms_db"] == POLICY.max_tracking_rms_db
    assert transparency["rms_db"] <= POLICY.max_tracking_rms_db


async def test_play_refuses_a_commanded_volume_the_session_never_admitted(
    tmp_path: Path,
) -> None:
    pieces = _seam(tmp_path, admission=_admission_context(session_volume_db=-10.0))

    with pytest.raises(BenchRefused) as raised:
        await _play(pieces)
    assert raised.value.reason == wired_play.REFUSE_COMMANDED_VOLUME
    # Refused before any read: nothing was polled and no capture exists.
    assert pieces.rig.played == []
    assert not (
        pieces.sink.bundle_dir / TARGET_ID / "sweep_transparency-capture.wav"
    ).exists()


async def test_play_surfaces_a_refused_admission_and_aborts_the_recorder(
    tmp_path: Path,
) -> None:
    # A woofer cap far under the stimulus peak: the fresh re-admission inside
    # play_program refuses before any audio.
    pieces = _seam(tmp_path, admission=_admission_context(woofer_peak=-90.0))

    with pytest.raises(BenchRefused) as raised:
        await _play(pieces)

    assert raised.value.reason == wired_play.REFUSE_ADMISSION
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP.value in raised.value.detail
    assert pieces.rig.pcm is not None and pieces.rig.pcm.closed is True
    assert not (
        pieces.sink.bundle_dir / TARGET_ID / "sweep_transparency-capture.wav"
    ).exists()


async def test_play_refuses_bytes_rendered_under_the_authorized_peak(
    tmp_path: Path,
) -> None:
    """The described program declares the peak the request AUTHORIZED, so an
    artifact rendered quieter than that fails the fresh admission."""

    pieces = _seam(tmp_path)

    with pytest.raises(BenchRefused) as raised:
        await _play(
            pieces, rendered=_request(requested_stimulus_effective_peak_dbfs=-23.0)
        )

    assert raised.value.reason == wired_play.REFUSE_ADMISSION
    assert ProgramAdmissionRefusal.MANIFEST_PEAK_MISMATCH.value in raised.value.detail


@pytest.mark.parametrize(
    ("preset_kwargs", "refused"),
    [
        ({}, False),
        # The stimulus runs past the woofer/tweeter corner: above it the
        # tweeter is being driven, not protected, and no cap was evaluated
        # for it.
        ({"crossover_fc_hz": 300.0}, True),
        # At the corner itself the pair above is only 6 dB down.
        ({"crossover_fc_hz": 400.0}, True),
        # A local subwoofer sits BELOW the woofer the stimulus is admitted
        # for and takes the whole band through its own low-pass.
        ({"sub_index": 4}, True),
    ],
)
async def test_play_refuses_a_band_the_owners_crossover_does_not_contain(
    tmp_path: Path, preset_kwargs: dict, refused: bool
) -> None:
    pieces = _seam(
        tmp_path, admission=_admission_context(preset_kwargs=preset_kwargs)
    )

    if not refused:
        assert (await _play(pieces)).quality_verdict == "pass"
        return
    with pytest.raises(BenchRefused) as raised:
        await _play(pieces)
    assert raised.value.reason == wired_play.REFUSE_BAND
    # Refused before any audio: nothing was played and no take was banked.
    assert pieces.rig.played == []
    assert not (
        pieces.sink.bundle_dir / TARGET_ID / "sweep_transparency-capture.wav"
    ).exists()


@pytest.mark.parametrize(
    "surface", ["get_clipped_samples", "get_playback_peak_all"]
)
async def test_play_refuses_when_a_controller_read_is_unavailable(
    tmp_path: Path, surface: str
) -> None:
    pieces = _seam(tmp_path)
    pieces.controller.unavailable.add(surface)

    with pytest.raises(BenchRefused) as raised:
        await _play(pieces)

    assert raised.value.reason == wired_play.REFUSE_CONTROLLER
    # The live ALSA device is never left open, and the window put the
    # household level back on the way out.
    assert pieces.rig.pcm is None or pieces.rig.pcm.closed is True
    assert not (
        pieces.sink.bundle_dir / TARGET_ID / "sweep_transparency-capture.wav"
    ).exists()
    assert pieces.volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)


async def test_play_refuses_a_capture_whose_onset_is_inside_the_pre_guard(
    tmp_path: Path,
) -> None:
    pieces = _seam(tmp_path)
    pieces.rig.silence_scale = 0.02

    with pytest.raises(BenchRefused) as raised:
        await _play(pieces)

    assert raised.value.reason == wired_play.REFUSE_UNANALYZABLE
    # The take is banked even though it could not be read.
    assert (
        pieces.sink.bundle_dir / TARGET_ID / "sweep_transparency-capture.wav"
    ).is_file()


async def test_play_records_an_unreadable_peak_poll_as_an_empty_snapshot(
    tmp_path: Path,
) -> None:
    pieces = _seam(tmp_path)
    pieces.controller.playback_peaks = None

    played = await _play(pieces)

    assert list(played.live_peak_all_samples) == [()] * _request().cross_check_read_count


# --------------------------------------------------------------------------- #
# The real executor + the real runner, over this seam
# --------------------------------------------------------------------------- #


def _campaign_pieces(tmp_path: Path, *, admission=None):
    # `_round_trip_render_config`'s step sits at frame 125_000, so the
    # campaign's stimulus body must be long enough to span it — the R10
    # permissive bound is computed from the rendered body's own envelope.
    request = _request(requested_hold_duration_s=4.0, cross_check_poll_interval_s=0.1)
    pieces = _seam(tmp_path, admission=admission, config_dir=tmp_path)
    config_file = tmp_path / "active.yml"
    config_file.write_text(_live_yaml(), encoding="utf-8")
    pieces.controller.config_path = config_file
    bench = executor.BenchRoleExecutor(
        target=_target_plan(),
        requests={role: request for role in STIMULUS_ROLES},
        play_and_capture=pieces.seam,
        binary=render.BinaryIdentity(
            path="/opt/camilladsp/camilladsp",
            version_output="CamillaDSP 4.1.3",
            sha256=_sha("fake-binary"),
            camilladsp_build_id="camilladsp-v4.1.3-fake",
        ),
        margin=MARGIN,
        renders_outstanding=64,
    )
    deps = BenchDeps(
        open_window=pieces.volume.window,
        controller=pieces.controller,
        floor=pieces.volume.floor,
        executor=bench,
        stop=Stop(),
    )
    manifest = author_campaign_manifest(
        {
            "driver_safety_fingerprint": _sha("ds"),
            "margin_policy_name": "conservative",
            "margin_policy_fingerprint": _sha("mp"),
            "requests": {
                TARGET_ID: {role: request.to_dict() for role in STIMULUS_ROLES}
            },
        },
        target_ids=(TARGET_ID,),
    )
    context = {
        "target_family_fingerprint": _sha("family"),
        "target_order": [
            {"target_id": TARGET_ID, "target_fingerprint": _sha(f"target:{TARGET_ID}")}
        ],
        "driver_safety_fingerprint": _sha("ds"),
        "margin_policy_fingerprint": _sha("mp"),
        "transparency_policy_fingerprint": _sha("tp"),
        "natural_graph_fingerprint": _sha("natural-graph"),
        "baseline_limiter_clip_limit_dbfs": -1.0,
        "limiter_domain_min_dbfs": LIMITER_DOMAIN_MIN_DBFS,
        "limiter_domain_max_dbfs": LIMITER_DOMAIN_MAX_DBFS,
        "limiter_domain_fingerprint": limiter_domain_fingerprint(),
        "camilladsp_build_id": "build",
        "owner_channels": list(OWNER_CHANNELS),
        "sample_rate_hz": RATE,
        "limiter_name": _target_plan().limiter_name,
        "limiter_type": "Limiter",
        "soft_clip": True,
        "tap_implementation_id": "tap",
        "detector_reference": DETECTOR_REFERENCE,
    }
    retained = {
        name: executor.ArtifactIdentity(
            bundle_kind="jts_bass_extension_limiter_bench",
            bundle_id="fixture",
            relative_path=f"retained-{name}.json",
            sha256=_sha(name),
            byte_size=len(name),
        )
        for name in (
            "sweep", "sustain", "commanded_level", "stimulus_peak", "boost",
            "digital_clamp",
        )
    }
    return SimpleNamespace(
        sink=pieces.sink, deps=deps, manifest=manifest, context=context,
        retained=retained, volume=pieces.volume,
    )


async def test_campaign_over_the_wired_seam_emits_resolvable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``BenchRoleExecutor`` and the real ``run_campaign``, with this
    module's ``WiredPlayAndCapture`` as the only measurement collaborator."""

    monkeypatch.setattr(render, "render_config", _round_trip_render_config)
    pieces = _campaign_pieces(tmp_path)

    emitted = await run_campaign(
        pieces.deps,
        manifest=pieces.manifest,
        measured_context=pieces.context,
        targets=[_target_plan()],
        retained_facts=pieces.retained,
        sink=pieces.sink,
    )

    result = emitted["targets"][0]["result"]
    assert result["disposition"] == "evaluated", result
    candidates = result["candidates_least_to_most_permissive"]
    assert [candidate["disposition"] for candidate in candidates][-1] == "accepted"

    # Every capture / admission identity the bundle carries resolves to a file
    # under the sink whose sha256 matches what the row claims.
    checked = 0
    for row in _identities(emitted, {"acoustic_capture", "admission"}):
        path = pieces.sink.bundle_dir / row["relative_path"]
        assert path.is_file(), row
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        checked += 1
    assert checked >= 4

    # R6a(i): the gate the bench holds is the owner live_proof proves.
    assert _mux_status()["test_owner"] == wired_play.BENCH_GATE_OWNER
    assert pieces.volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)


def _identities(node: Any, keys: set[str]):
    """Every ArtifactIdentity-shaped mapping the bundle carries under ``keys``."""

    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, dict) and "sha256" in value:
                yield value
            else:
                yield from _identities(value, keys)
    elif isinstance(node, list):
        for item in node:
            yield from _identities(item, keys)


async def test_campaign_reports_a_bench_refusal_as_the_targets_refused_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render, "render_config", _round_trip_render_config)
    pieces = _campaign_pieces(
        tmp_path, admission=_admission_context(session_volume_db=-10.0)
    )

    emitted = await run_campaign(
        pieces.deps,
        manifest=pieces.manifest,
        measured_context=pieces.context,
        targets=[_target_plan()],
        retained_facts=pieces.retained,
        sink=pieces.sink,
    )

    result = emitted["targets"][0]["result"]
    assert result["disposition"] == "refused"
    stop = _read_json(pieces.sink, f"{TARGET_ID}/stop.json")
    assert stop["reason"] == wired_play.REFUSE_COMMANDED_VOLUME
    assert pieces.volume.fader.level_db == pytest.approx(HOUSEHOLD_DB)
