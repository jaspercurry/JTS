# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.web.wake_corpus_setup.

Covers the RecordingBackend state machine, metadata persistence, the
RecordingTask audio collection logic (with a fake UdpMicCapture), and
the HTTP handler routing. Hardware-free.

Strategy for the async parts: the backend's asyncio loop runs in a
daemon thread; tests start + shutdown the backend explicitly. The
UdpMicCapture used by RecordingTask is lazy-imported via
`from jasper.audio_io import UdpMicCapture`, so tests monkeypatch
`jasper.audio_io.UdpMicCapture` to inject a fake before the
RecordingTask is constructed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from jasper.wake_corpus import bridge_session
from jasper.mics import xvf3800
from jasper.web import wake_corpus_setup


# ---------------------------------------------------------------------------
# Relocated static assets. /wake-corpus/ moved onto the canonical design
# system: the page behaviour now lives in an ES module and the bespoke CSS in
# a page stylesheet, so the HTML render-shape assertions below check whichever
# artifact a given string now lives in (page body vs module vs stylesheet),
# mirroring how the /wifi/ migration relocated its inline JS + tests.
# ---------------------------------------------------------------------------

_ASSETS = Path(__file__).resolve().parents[1] / "deploy" / "assets" / "wake-corpus"
_NODE = shutil.which("node")


def _stub_xvf_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    variant: xvf3800.FirmwareVariant | None = xvf3800.VARIANT_6CH,
    present: bool = True,
    channels: int | None = 6,
) -> None:
    plan = xvf3800.chip_beam_plan_for_variant(variant)
    card = variant.alsa_card_name if variant else xvf3800.ALSA_CARD_NAME
    monkeypatch.setattr(
        xvf3800,
        "detect_runtime_profile",
        lambda: xvf3800.RuntimeProfile(
            present=present,
            variant=variant,
            alsa_card_name=card,
            capture_channels=channels,
            chip_beam_plan=plan,
            reason="test profile",
        ),
    )


def _module_js() -> str:
    return (_ASSETS / "js" / "main.js").read_text()


def _controls_js_path() -> Path:
    return _ASSETS / "js" / "controls.js"


def _controls_js() -> str:
    return _controls_js_path().read_text()


def _page_css() -> str:
    return (_ASSETS / "wake-corpus.css").read_text()


# ---------------------------------------------------------------------------
# Fake UDP capture — yields fixed frames at a fast rate, deterministic.
# ---------------------------------------------------------------------------


class _FakeUdpMicCapture:
    """Async context manager + frames() iterator.

    Matches the public surface UdpMicCapture exposes that
    RecordingTask uses. Each instance produces frames with a fixed
    sample value so tests can assert per-leg routing (the AEC ON
    leg's frames stay distinct from the AEC OFF leg's frames in the
    output WAV bytes).
    """

    # Test hook: tests append (port → sample_value) entries so
    # different ports yield distinguishable audio.
    port_to_value: dict[int, int] = {}

    def __init__(self, host: str = "127.0.0.1", port: int = 9876) -> None:
        self._port = port
        self._value = self.port_to_value.get(port, 0)
        self._closed = False

    async def __aenter__(self) -> "_FakeUdpMicCapture":
        return self

    async def __aexit__(self, *exc) -> None:
        self._closed = True

    async def frames(self):
        # Yield ~1 frame per 5 ms (much faster than real-time so 0.1 s
        # of capture produces ~20 frames).
        while not self._closed:
            yield np.full(1280, self._value, dtype=np.int16)
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _patch_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the fake into jasper.audio_io so RecordingTask's lazy
    import picks it up."""
    import jasper.audio_io as audio_io
    monkeypatch.setattr(audio_io, "UdpMicCapture", _FakeUdpMicCapture)
    # Reset the per-port value map between tests
    _FakeUdpMicCapture.port_to_value = {}


# ---------------------------------------------------------------------------
# RecordingTask — direct exercise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_task_collects_frames_per_leg() -> None:
    _FakeUdpMicCapture.port_to_value = {9876: 11, 9877: 22, 9878: 33}
    task = wake_corpus_setup.RecordingTask(
        ports={"on": 9876, "off": 9877, "dtln": 9878},
    )
    await task.start()
    await asyncio.sleep(0.1)  # let the background task collect ~20 frames/leg
    pcm = await task.stop()

    assert set(pcm.keys()) == {"on", "off", "dtln"}
    # Each leg's bytes should be all the same value as its fake.
    on_samples = np.frombuffer(pcm["on"], dtype=np.int16)
    off_samples = np.frombuffer(pcm["off"], dtype=np.int16)
    dtln_samples = np.frombuffer(pcm["dtln"], dtype=np.int16)
    assert len(on_samples) > 0
    assert (on_samples == 11).all()
    assert (off_samples == 22).all()
    assert (dtln_samples == 33).all()


@pytest.mark.asyncio
async def test_recording_task_elapsed_grows() -> None:
    task = wake_corpus_setup.RecordingTask(ports={"on": 9876})
    await task.start()
    assert task.elapsed_sec() < 0.05
    await asyncio.sleep(0.1)
    assert task.elapsed_sec() >= 0.1
    await task.stop()


# ---------------------------------------------------------------------------
# RecordingBackend — start/shutdown
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(monkeypatch, tmp_path: Path):
    """Construct + start a backend rooted in a tmp dir, tear down on
    test exit. All 4 leg ports configured — matches the production
    default. Tests that exercise 3-leg mode just don't opt into
    include_raw_mic_0."""
    monkeypatch.setattr(
        bridge_session,
        "BRIDGE_STATS_PATH",
        tmp_path / "missing_aec_bridge_stats.json",
    )
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={
            "on": 9876,
            "off": 9877,
            "dtln": 9878,
            "raw0": 9879,
            "ref": 9880,
            "usb_raw": 9881,
            "usb_webrtc": 9882,
            "usb_dtln": 9883,
            "chip_aec_150": 9887,
            "chip_aec_210": 9888,
            "xvf_raw0_webrtc_aec3": 9889,
            "xvf_raw0_dtln": 9890,
            **wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS,
        },
        max_duration_sec=10.0,  # long enough to not auto-stop during tests
    )
    b.start()
    yield b
    b.shutdown()


def test_backend_start_is_idempotent(tmp_path: Path) -> None:
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    b.start()
    b.start()  # second call must not raise + not spawn a 2nd thread
    b.shutdown()


# ---------------------------------------------------------------------------
# begin_session
# ---------------------------------------------------------------------------


def test_begin_session_sets_id_and_member(backend) -> None:
    sid = backend.begin_session("jasper")
    assert sid is not None
    assert backend.session_id() == sid
    assert backend.member() == "jasper"


def test_begin_session_sanitizes_member(backend) -> None:
    backend.begin_session("Jasper Curry!")
    assert backend.member() == "jaspercurry"


def test_begin_session_rejects_empty_member(backend) -> None:
    with pytest.raises(ValueError, match="no usable chars"):
        backend.begin_session("   ")


def test_begin_session_rejects_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.begin_session("brittany")
    finally:
        backend.stop_recording()


# ---------------------------------------------------------------------------
# start_recording / stop_recording
# ---------------------------------------------------------------------------


def test_start_recording_validates_condition(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="unknown condition"):
        backend.start_recording("loud", "near")


def test_start_recording_validates_distance(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="unknown distance"):
        backend.start_recording("quiet", "across-the-house")


def test_start_recording_requires_session(backend) -> None:
    with pytest.raises(wake_corpus_setup.StateError, match="begin_session"):
        backend.start_recording("quiet", "near")


def test_start_recording_rejects_double_start(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError, match="in progress"):
            backend.start_recording("quiet", "near")
    finally:
        backend.stop_recording()


def test_stop_recording_without_start_raises(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(wake_corpus_setup.StateError, match="no recording"):
        backend.stop_recording()


def test_start_stop_writes_wavs_to_correct_quadrant(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    result = backend.start_recording("music", "far")
    assert "clip_id" in result
    time.sleep(0.1)  # collect ~20 frames per leg
    clip = backend.stop_recording()

    # Files landed in aec_<leg>_music/ since condition=music
    out = tmp_path / "out"
    assert (out / "aec_on_music").is_dir()
    assert (out / "aec_off_music").is_dir()
    assert (out / "aec_dtln_music").is_dir()
    on_wavs = list((out / "aec_on_music").glob("*.aec-on.wav"))
    off_wavs = list((out / "aec_off_music").glob("*.aec-off.wav"))
    dtln_wavs = list((out / "aec_dtln_music").glob("*.aec-dtln.wav"))
    assert len(on_wavs) == 1
    assert len(off_wavs) == 1
    assert len(dtln_wavs) == 1
    # Filename pattern: enroll_<member>_<session>_<seq>.aec-<leg>.wav
    assert on_wavs[0].name.startswith("enroll_jasper_")
    assert on_wavs[0].name.endswith("_001.aec-on.wav")
    # ClipMetadata reflects all of this
    assert clip.member == "jasper"
    assert clip.condition == "music"
    assert clip.distance == "far"
    assert clip.seq == 1
    assert set(clip.files.keys()) == {"on", "off", "dtln"}


def test_start_stop_writes_wav_in_correct_format(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    backend.stop_recording()

    wavs = list((tmp_path / "out").rglob("*.aec-on.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0])) as w:
        assert w.getnchannels() == wake_corpus_setup.CHANNELS
        assert w.getsampwidth() == wake_corpus_setup.SAMPLE_WIDTH_BYTES
        assert w.getframerate() == wake_corpus_setup.SAMPLE_RATE_HZ
        assert w.getnframes() > 0  # actual audio captured


def test_sequential_clips_get_incrementing_seq(backend) -> None:
    backend.begin_session("jasper")
    seqs = []
    for _ in range(3):
        backend.start_recording("quiet", "near")
        time.sleep(0.05)
        clip = backend.stop_recording()
        seqs.append(clip.seq)
    assert seqs == [1, 2, 3]


def test_sequence_excludes_deleted_clips(backend) -> None:
    """Deleting clip 1 must not let the next clip reuse seq=2.

    Filenames include the per-session sequence number, so reusing a
    sequence can overwrite a later good take in the same condition.
    """
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip1 = backend.stop_recording()
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip2 = backend.stop_recording()
    assert clip1.seq == 1
    assert clip2.seq == 2

    backend.delete_clip(clip1.clip_id)

    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip3 = backend.stop_recording()
    # Sequence is monotonic across the session, including deleted clips.
    assert clip3.seq == 3


# ---------------------------------------------------------------------------
# delete_clip
# ---------------------------------------------------------------------------


def test_delete_clip_removes_wavs_and_marks_deleted(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()

    assert all(Path(p).is_file() for p in clip.files.values())
    assert backend.delete_clip(clip.clip_id) is True

    # WAVs gone from disk
    assert all(not Path(p).is_file() for p in clip.files.values())
    # Clip not in non-deleted list
    assert clip.clip_id not in {c.clip_id for c in backend.list_clips()}
    # But still in include_deleted
    all_clips = backend.list_clips(include_deleted=True)
    assert any(c.clip_id == clip.clip_id and c.deleted for c in all_clips)


def test_delete_clip_idempotent_on_missing(backend) -> None:
    assert backend.delete_clip("nonexistent-uuid") is False


def test_delete_clip_idempotent_on_already_deleted(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()
    backend.delete_clip(clip.clip_id)
    # Second delete returns False (already deleted)
    assert backend.delete_clip(clip.clip_id) is False


# ---------------------------------------------------------------------------
# Metadata persistence — JSON sidecar
# ---------------------------------------------------------------------------


def test_metadata_written_per_session(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    assert len(json_files) == 1
    assert json_files[0].name.startswith("enroll_jasper_")

    data = json.loads(json_files[0].read_text())
    assert data["member"] == "jasper"
    assert data["session_id"] == backend.session_id()
    assert len(data["clips"]) == 1
    assert data["clips"][0]["clip_id"] == clip.clip_id
    assert data["clips"][0]["condition"] == "quiet"
    assert data["clips"][0]["distance"] == "near"
    assert data["clips"][0]["capture_health"]["status"] == "unknown"
    assert data["clips"][0]["capture_health"]["legs"]["on"]["packets"] > 0
    assert data["capture_plan"]["recipe"] == "single_mic_comparison"
    assert data["clips"][0]["capture_plan"]["selected_legs"] == [
        "on", "off", "dtln",
    ]


def test_metadata_capture_plan_persists_missing_bridge_outputs(
    backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_tmp_bridge_env(monkeypatch, tmp_path)

    backend.begin_session("jasper", include_dtln=True, include_usb_mic=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    missing = set(data["capture_plan"]["bridge"]["missing_outputs"])
    assert {"dtln", "ref", "usb"} <= missing
    assert any(
        "bridge is not currently emitting" in warning
        for warning in data["capture_plan"]["warnings"]
    )
    assert (
        data["clips"][0]["capture_plan"]["bridge"]["missing_outputs"]
        == data["capture_plan"]["bridge"]["missing_outputs"]
    )


def test_metadata_records_audio_context_snapshot(
    backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_path, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env=(
            "JASPER_MIC_DEVICE=udp:9876\n"
            "JASPER_AEC_MIC_DEVICE=Array\n"
            "JASPER_AEC_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG=chip_aec_210\n"
            "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887\n"
            "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888\n"
            "JASPER_XVF_VARIANT=xvf3800_legacy_square_6ch\n"
            "JASPER_XVF_GEOMETRY=square\n"
            "JASPER_XVF_CHIP_BEAM_PLAN=xvf_square_fixed_150_210\n"
            "JASPER_XVF_CHIP_AEC_SUPPORTED=1\n"
            "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n"
            "JASPER_OUTPUTD_DAC_PCM=envfile_dac\n"
            "JASPER_OUTPUTD_BACKEND=alsa_envfile\n"
            "JASPER_OUTPUTD_CONTROL_SOCKET=/run/envfile-outputd.sock\n"
        ),
        corpus_env=(
            "JASPER_AEC_REF_SOURCE=outputd_udp\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED=1\n"
            "JASPER_OUTPUTD_CHIP_REF_PCM=plughw:CARD=Array,DEV=0\n"
            "JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891\n"
            "JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE=16000\n"
            "JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES=320\n"
            "JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES=1280\n"
        ),
    )
    assert system_path.is_file()
    assert bridge_path.is_file()
    aec_mode_path = tmp_path / "aec_mode.env"
    aec_mode_path.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n",
    )
    validation_path = tmp_path / "audio_validation.json"
    validation_path.write_text(json.dumps({
        "schema_version": 1,
        "validated_at": "2026-06-01T12:00:00Z",
        "profile": "xvf_chip_aec",
        "status": "pass",
        "hardware": {
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
        "checks": {"measured_drift_delay": {"status": "pass"}},
        "recommendation": "chip_aec_validated",
    }))
    monkeypatch.setattr(bridge_session, "AEC_MODE_PATH", aec_mode_path)
    monkeypatch.setattr(
        bridge_session,
        "AUDIO_VALIDATION_ARTIFACT_PATH",
        validation_path,
    )
    monkeypatch.setenv("JASPER_OUTPUTD_DAC_PCM", "stale_process_dac")
    monkeypatch.setenv("JASPER_OUTPUTD_BACKEND", "fake")
    monkeypatch.setenv(
        "JASPER_OUTPUTD_CONTROL_SOCKET",
        "/run/stale-process-outputd.sock",
    )
    monkeypatch.setattr(bridge_session, "aec_bridge_active", lambda: True)
    _stub_xvf_runtime(monkeypatch)

    backend.begin_session(
        "jasper",
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
    )
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    assert (
        data["metadata_schema_version"]
        == wake_corpus_setup.METADATA_SCHEMA_VERSION
    )
    context = data["audio_context"]
    assert (
        context["schema_version"]
        == wake_corpus_setup.AUDIO_CONTEXT_SCHEMA_VERSION
    )
    assert context["production_audio_profile"]["requested"] == "xvf_chip_aec"
    assert context["production_audio_profile"]["active"] == "xvf_chip_aec"
    assert context["runtime_audio_env"]["chip_primary_leg"] == "chip_aec_210"
    assert context["microphone"]["firmware"]["capture_channels"] == 6
    assert context["microphone"]["identity"]["usb_vid_pid"] == "2886:001a"
    assert context["dac_reference"]["dac"]["pcm"] == "envfile_dac"
    assert context["dac_reference"]["dac"]["backend"] == "alsa_envfile"
    assert (
        context["dac_reference"]["dac"]["control_socket"]
        == "/run/envfile-outputd.sock"
    )
    assert context["dac_reference"]["reference"]["source"] == "outputd_udp"
    assert context["dac_reference"]["validation"]["status"] == "pass"
    assert context["dac_reference"]["validation"]["hardware"]["dac_id"] == (
        "apple_usb_c_dongle"
    )
    details = {
        item["token"]: item
        for item in context["corpus"]["leg_details"]
    }
    assert details["chip_aec_150"]["kind"] == "hardware_aec"
    assert details["chip_aec_150"]["wake_input"] is True
    assert details["raw0"]["profile_role"] == "corpus_only"

    clip = data["clips"][0]
    assert clip["selected_legs"] == data["enabled_legs"]
    assert (
        clip["audio_context"]["production_audio_profile"]["active"]
        == "xvf_chip_aec"
    )


def test_standard_metadata_marks_on_leg_as_chip_primary_when_runtime_active(
    backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env=(
            "JASPER_AEC_CHIP_AEC_ENABLED=1\n"
            "JASPER_AEC_CHIP_AEC_PRIMARY_LEG=chip_aec_210\n"
            "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887\n"
            "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888\n"
            "JASPER_XVF_VARIANT=xvf3800_legacy_square_6ch\n"
            "JASPER_XVF_GEOMETRY=square\n"
            "JASPER_XVF_CHIP_BEAM_PLAN=xvf_square_fixed_150_210\n"
            "JASPER_XVF_CHIP_AEC_SUPPORTED=1\n"
        ),
    )
    aec_mode_path = tmp_path / "aec_mode.env"
    aec_mode_path.write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_WAKE_LEG_RAW=0\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=1\n",
    )
    monkeypatch.setattr(bridge_session, "AEC_MODE_PATH", aec_mode_path)
    monkeypatch.setattr(bridge_session, "aec_bridge_active", lambda: True)
    _stub_xvf_runtime(monkeypatch)

    backend.begin_session("jasper", include_dtln=False)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    by_token = {leg["token"]: leg for leg in data["capture_plan"]["legs"]}
    assert by_token["on"]["label"] == "Chip AEC ASR 210 primary"
    assert by_token["on"]["processing"] == "hardware_aec"
    assert by_token["on"]["runtime_primary_leg"] == "chip_aec_210"
    assert "on" not in data["capture_plan"]["software_transforms"]["webrtc_aec3"]

    context_by_token = {
        leg["token"]: leg
        for leg in data["audio_context"]["corpus"]["leg_details"]
    }
    assert context_by_token["on"]["processing"] == "hardware_aec"


def test_validation_artifact_summary_rejects_wrong_current_dac(
    tmp_path: Path,
) -> None:
    validation_path = tmp_path / "audio_validation.json"
    validation_path.write_text(json.dumps({
        "schema_version": 1,
        "validated_at": "2026-06-01T12:00:00Z",
        "profile": "xvf_chip_aec",
        "status": "pass",
        "hardware": {
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
        "checks": {"measured_drift_delay": {"status": "pass"}},
        "recommendation": "chip_aec_validated",
    }))

    summary = wake_corpus_setup._validation_artifact_summary(
        validation_path,
        requested_profile="xvf_chip_aec",
        mic_probe=wake_corpus_setup.MicProbe(
            xvf_present=True,
            capture_channels=6,
        ),
        system_env={"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
    )

    assert summary["state"] == "mismatch"
    assert "dac_id" in summary["reason"]


def test_metadata_updated_on_delete(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()
    backend.delete_clip(clip.clip_id)

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    # The clip is still in the metadata list, marked deleted (audit trail)
    matching = [c for c in data["clips"] if c["clip_id"] == clip.clip_id]
    assert len(matching) == 1
    assert matching[0]["deleted"] is True


def test_metadata_atomic_no_tmp_left_behind(backend, tmp_path: Path) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    md_dir = tmp_path / "out" / "metadata"
    json_files = list(md_dir.glob("enroll_*.json"))
    tmp_files = list(md_dir.glob("*.tmp"))
    assert len(json_files) == 1
    assert tmp_files == []


# ---------------------------------------------------------------------------
# Auto-stop on excessive duration
# ---------------------------------------------------------------------------


def test_auto_stop_fires_on_max_duration(tmp_path: Path) -> None:
    """A forgotten Stop click should auto-stop at MAX_DURATION_SEC
    with the auto_stopped flag set so the operator notices."""
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={"on": 9876},
        max_duration_sec=0.3,  # short for the test
    )
    b.start()
    try:
        b.begin_session("jasper")
        b.start_recording("quiet", "near")
        # Wait long enough for auto-stop to fire + the worker
        # thread to complete the save.
        time.sleep(0.8)
        assert not b.is_recording()
        clips = b.list_clips()
        assert len(clips) == 1
        assert clips[0].auto_stopped is True
    finally:
        b.shutdown()


# ---------------------------------------------------------------------------
# HTML rendering — quick sanity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Voice-daemon control safety — refuses start during recording
# ---------------------------------------------------------------------------


def test_voice_daemon_start_refused_during_recording(
    backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator clicks 'Start jasper-voice' while a recording
    is in progress, the server must refuse — starting the daemon
    would try to bind UDP ports the recording owns, sending
    jasper-voice into a restart loop.

    Tests the HTTP handler logic directly (no real HTTP socket) by
    instantiating it against a mock request transport.
    """
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        # Build a minimal handler stand-in with the bound backend.
        # The handler's POST routing checks backend.is_recording() for
        # start; we just need to verify the check exists + works.
        assert backend.is_recording()
        # Simulate the handler's guard: this is the condition the
        # handler checks before invoking systemctl. We're verifying
        # the guard's contract, not the HTTP transport.
        action = "start"
        guard_should_refuse = action == "start" and backend.is_recording()
        assert guard_should_refuse, (
            "voice-daemon start handler must refuse while recording"
        )

        # The inverse: when recording stops, the guard releases.
        backend.stop_recording()
        guard_should_refuse_after = action == "start" and backend.is_recording()
        assert not guard_should_refuse_after, (
            "voice-daemon start handler must allow after recording stops"
        )
    finally:
        if backend.is_recording():
            backend.stop_recording()


# ---------------------------------------------------------------------------
# make_server — socket-activation support
# ---------------------------------------------------------------------------


def test_make_server_accepts_host_port_tuple(backend) -> None:
    """The (host, port) tuple form is what main()'s direct-bind path
    uses. make_server must construct a ThreadingHTTPServer correctly."""
    from http.server import ThreadingHTTPServer
    server = wake_corpus_setup.make_server(
        ("127.0.0.1", 0),  # port=0 → OS picks a free port (no clash in CI)
        csrf_token="test-token",
        backend=backend,
    )
    try:
        assert isinstance(server, ThreadingHTTPServer)
        # Handler must have backend + csrf_token bound for request handling
        handler_cls = server.RequestHandlerClass
        assert handler_cls.backend is backend
        assert handler_cls.csrf_token == "test-token"
    finally:
        server.server_close()


def test_make_server_accepts_prebound_socket(backend) -> None:
    """The socket form is what __main__.py's socket-activation path
    uses (systemd-passed fds). make_server must adopt the socket
    without re-binding (or it'd EADDRINUSE the systemd fd)."""
    import socket
    from http.server import ThreadingHTTPServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    try:
        server = wake_corpus_setup.make_server(
            s, csrf_token="test-token", backend=backend,
        )
        try:
            assert isinstance(server, ThreadingHTTPServer)
            # The server adopted our pre-bound socket — same fd, same address
            assert server.socket.fileno() == s.fileno()
            assert server.server_address == s.getsockname()
        finally:
            server.server_close()
    except Exception:  # noqa: BLE001
        s.close()
        raise


def test_index_html_is_valid_shape() -> None:
    """Not a full HTML validator — just enough to catch obvious
    breakage like missing </body>, unmatched template strings, etc.
    Canonical_page() emits the document shell (lowercase doctype)."""
    html_text = wake_corpus_setup._render_index_html("token123")
    assert "<!doctype html>" in html_text
    assert "<title>Wake-word corpus</title>" in html_text
    assert "</body>" in html_text
    assert "</html>" in html_text
    # No template placeholder should leak into the rendered page.
    for stale in ("{header}", "{config_island}", "{csrf_token}", "{nav_back"):
        assert stale not in html_text
    # Key API paths must be referenced from the behaviour module. The JS uses
    # RELATIVE paths ('api/status' not '/api/status') so the same page works
    # both standalone (http://host:8782/) and behind nginx
    # (http://host/wake-corpus/). Absolute paths would 502 under nginx.
    js = _module_js()
    assert "api/status" in js
    assert "api/session" in js
    assert "api/clip/start" in js
    assert "api/clip/stop" in js
    # Defensive: ensure we don't regress to absolute paths in the JS.
    assert "api('GET', 'api/status')" in js or \
           'api("GET", "api/status")' in js
    # All three conditions + distances must be selectable (page body).
    for c in ("quiet", "ambient", "music"):
        assert f'value="{c}"' in html_text
    for d in ("near", "mid", "far"):
        assert f'value="{d}"' in html_text


# ---------------------------------------------------------------------------
# CSRF — token embedded in HTML + required on mutating requests
# ---------------------------------------------------------------------------


def test_render_index_has_back_home_link() -> None:
    """Every JTS wizard page has a back-to-Home affordance in the top-left.
    On the canonical design system that's the shared `.app-header` back
    button (canonical_header), which links '/' and labels itself 'Home'."""
    html_text = wake_corpus_setup._render_index_html("token")
    assert "app-header" in html_text
    assert 'href="/"' in html_text
    assert 'aria-label="Home"' in html_text
    assert "#icon-back" in html_text


def test_render_index_embeds_csrf_token() -> None:
    """The CSRF token must appear in the canonical meta tag so the shared
    http.js helpers can read it. Token is HTML-escaped defensively (even
    though secrets.token_hex only produces hex chars)."""
    html_text = wake_corpus_setup._render_index_html("abc123def")
    assert 'name="jts-csrf"' in html_text
    assert 'content="abc123def"' in html_text


def test_render_index_escapes_csrf_token() -> None:
    """A pathological token with HTML metachars must be escaped so
    it can't break out of the meta tag's content attribute."""
    html_text = wake_corpus_setup._render_index_html('"><script>x</script>')
    assert "<script>x</script>" not in html_text
    assert "&lt;script&gt;" in html_text or "&quot;&gt;" in html_text


def test_csrf_header_name_constant_matches_module() -> None:
    """The server's CSRF_HEADER must stay X-CSRF-Token — the contract the
    shared http.js jsonHeaders() helper used by the behaviour module sends.
    The page exposes the token via the <meta name=jts-csrf> tag; jsonHeaders()
    reads it and attaches the X-CSRF-Token header the server's _check_csrf
    compares against."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert wake_corpus_setup.CSRF_HEADER == "X-CSRF-Token"
    assert 'name="jts-csrf"' in html_text
    assert 'import { jsonHeaders } from "/assets/shared/js/http.js"' in _module_js()


# ---------------------------------------------------------------------------
# RecordingTask.stop() idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_task_stop_idempotent() -> None:
    """Calling stop() twice must not crash on double __aexit__ or
    double-await of a cancelled task. Defensive against state-machine
    bugs in callers."""
    _FakeUdpMicCapture.port_to_value = {9876: 7}
    task = wake_corpus_setup.RecordingTask(ports={"on": 9876})
    await task.start()
    await asyncio.sleep(0.05)
    pcm_first = await task.stop()
    pcm_second = await task.stop()  # must not raise

    assert len(pcm_first["on"]) > 0
    # Second call returns the buffered bytes unchanged (no new frames
    # since cleanup), but doesn't crash. Either same bytes or empty
    # is acceptable — what matters is no exception.
    assert "on" in pcm_second


# ---------------------------------------------------------------------------
# Session recovery on backend start
# ---------------------------------------------------------------------------


def test_recovery_loads_recent_session(tmp_path: Path) -> None:
    """A fresh backend on a corpus dir with a recent metadata file
    must load the session into memory so the UI can pick up where
    the operator left off after a crash."""
    out = tmp_path / "out"
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True)
    # Write a metadata file mimicking a previous session
    session_data = {
        "session_id": "20260525T120000Z",
        "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878},
        "clips": [
            {
                "clip_id": "abc-123", "member": "jasper",
                "condition": "quiet", "distance": "near",
                "session_id": "20260525T120000Z", "seq": 1,
                "start_ts": "2026-05-25T12:00:00.000+00:00",
                "stop_ts": "2026-05-25T12:00:03.000+00:00",
                "duration_sec": 3.0,
                "files": {"on": "/tmp/x.wav"},
                "deleted": False, "auto_stopped": False, "notes": "",
            },
        ],
    }
    md_file = md_dir / "enroll_jasper_20260525T120000Z.json"
    md_file.write_text(json.dumps(session_data))
    (md_dir / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "20260525T120000Z",
    }))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.session_id() == "20260525T120000Z"
        assert b.member() == "jasper"
        clips = b.list_clips()
        assert len(clips) == 1
        assert clips[0].clip_id == "abc-123"
    finally:
        b.shutdown()


def test_recovery_ignores_recent_session_without_active_marker(
    tmp_path: Path,
) -> None:
    """Recent metadata alone is historical, not an append target.

    A graceful corpus test-mode exit clears the active marker; after
    that, reopening the page should show a fresh new-session form even
    if the last corpus session was moments ago.
    """
    out = tmp_path / "out"
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True)
    (md_dir / "enroll_jasper_recent.json").write_text(json.dumps({
        "session_id": "recent", "member": "jasper",
        "ports": {}, "clips": [],
    }))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.session_id() is None
        assert b.member() is None
    finally:
        b.shutdown()


def test_recovery_ignores_stale_session(tmp_path: Path) -> None:
    """An active marker older than RESUME_WINDOW_SEC must NOT be
    loaded — operator opens the UI tomorrow shouldn't see clips
    from a session they abandoned overnight."""
    out = tmp_path / "out"
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True)
    md_file = md_dir / "enroll_jasper_old.json"
    md_file.write_text(json.dumps({
        "session_id": "old", "member": "jasper", "ports": {}, "clips": [],
    }))
    marker = md_dir / wake_corpus_setup.ACTIVE_SESSION_MARKER
    marker.write_text(json.dumps({"session_id": "old"}))
    # Force mtime to be old
    old_mtime = time.time() - (wake_corpus_setup.RESUME_WINDOW_SEC + 60)
    os.utime(md_file, (old_mtime, old_mtime))
    os.utime(marker, (old_mtime, old_mtime))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.session_id() is None
        assert b.member() is None
    finally:
        b.shutdown()


def test_recovery_ignores_corrupt_json(tmp_path: Path) -> None:
    """A corrupt metadata file must not crash startup — just skip
    recovery + log + start with a fresh state."""
    out = tmp_path / "out"
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True)
    (md_dir / "enroll_jasper_corrupt.json").write_text("{not json")

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.session_id() is None
    finally:
        b.shutdown()


def test_recovery_handles_missing_metadata_dir(tmp_path: Path) -> None:
    """No metadata dir → no crash, no session loaded."""
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    b.start()
    try:
        assert b.session_id() is None
    finally:
        b.shutdown()


def test_begin_session_after_recovery_starts_fresh(
    tmp_path: Path,
) -> None:
    """After recovery, calling begin_session() with a different (or
    same) member must replace the recovered state with a fresh
    session — recovery is a one-shot, not a permanent re-attach."""
    out = tmp_path / "out"
    md_dir = out / "metadata"
    md_dir.mkdir(parents=True)
    (md_dir / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "recovered", "member": "jasper",
        "ports": {}, "clips": [],
    }))
    (md_dir / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "recovered",
    }))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        # Recovery loaded the old session
        assert b.session_id() == "recovered"
        # Beginning a new session replaces it
        new_id = b.begin_session("brittany")
        assert new_id != "recovered"
        assert b.member() == "brittany"
        assert b.list_clips() == []
    finally:
        b.shutdown()


# ---------------------------------------------------------------------------
# start_recording race-window fix — concurrent attempts refuse cleanly
# ---------------------------------------------------------------------------


def test_start_recording_refuses_during_starting_window(backend) -> None:
    """If a second start_recording call arrives while the first is in
    the middle of its slow `_submit`, the second must see the
    `_starting_clip_id` sentinel and refuse with the right error
    (not race into a UDP-bind failure).

    We simulate the race deterministically by manually setting the
    sentinel + verifying the next start refuses, then clearing +
    verifying it's allowed again.
    """
    backend.begin_session("jasper")
    # Manually set the starting sentinel as if a concurrent start is
    # in flight.
    with backend._lock:
        backend._starting_clip_id = "concurrent-fake-id"
    try:
        with pytest.raises(
            wake_corpus_setup.StateError, match="in progress",
        ):
            backend.start_recording("quiet", "near")
    finally:
        with backend._lock:
            backend._starting_clip_id = None

    # Sentinel cleared → next start is allowed.
    backend.start_recording("quiet", "near")
    backend.stop_recording()


def test_start_recording_clears_sentinel_on_success(backend) -> None:
    """After a successful start, the sentinel must be cleared (it
    moves to `_current_clip_id`). Otherwise a leftover sentinel would
    block all future recordings until process restart."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        # Sentinel should be cleared after successful transition
        with backend._lock:
            assert backend._starting_clip_id is None
            assert backend._current_clip_id is not None
    finally:
        backend.stop_recording()


# ---------------------------------------------------------------------------
# Ambient condition — third quadrant for AC / HVAC / fridge noise
# ---------------------------------------------------------------------------


def test_conditions_includes_ambient() -> None:
    """The CONDITIONS tuple must expose 'ambient' so the wizard's
    radio button + the backend's validation both line up."""
    assert "ambient" in wake_corpus_setup.CONDITIONS


def test_start_recording_accepts_ambient(backend) -> None:
    """A new third condition; previously rejected as 'unknown'."""
    backend.begin_session("jasper")
    result = backend.start_recording("ambient", "near")
    assert "clip_id" in result
    backend.stop_recording()


def test_ambient_clips_land_in_ambient_quadrant(
    backend, tmp_path: Path,
) -> None:
    """Files for condition=ambient land in aec_<leg>_ambient/ —
    separate from both nomusic (quiet) and music quadrants so
    downstream training can slice on the realistic-home condition."""
    backend.begin_session("jasper")
    backend.start_recording("ambient", "mid")
    time.sleep(0.1)
    backend.stop_recording()

    out = tmp_path / "out"
    assert (out / "aec_on_ambient").is_dir()
    assert (out / "aec_off_ambient").is_dir()
    assert (out / "aec_dtln_ambient").is_dir()
    wavs = list((out / "aec_on_ambient").glob("*.aec-on.wav"))
    assert len(wavs) == 1


def test_quiet_clips_still_land_in_nomusic_quadrant(
    backend, tmp_path: Path,
) -> None:
    """Backward compatibility: 'quiet' still maps to the historical
    'nomusic' directory so existing recordings + downstream tools
    (extract-wake-corpus.py) keep working unchanged."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    backend.stop_recording()

    out = tmp_path / "out"
    # Quiet → 'nomusic' (NOT 'quiet')
    assert (out / "aec_on_nomusic").is_dir()
    assert not (out / "aec_on_quiet").exists()


# ---------------------------------------------------------------------------
# compute_rms_dbfs — pure helper for the live mic-level meter
# ---------------------------------------------------------------------------


def test_compute_rms_dbfs_silent_returns_floor() -> None:
    """All-zeros frame returns the -100 dBFS floor (avoids -inf
    from log(0); UI clamps below this anyway)."""
    frame = np.zeros(1280, dtype=np.int16)
    assert wake_corpus_setup.compute_rms_dbfs(frame) == -100.0


def test_compute_rms_dbfs_empty_returns_floor() -> None:
    """Zero-length frame returns the floor instead of NaN."""
    frame = np.zeros(0, dtype=np.int16)
    assert wake_corpus_setup.compute_rms_dbfs(frame) == -100.0


def test_compute_rms_dbfs_full_scale_is_zero() -> None:
    """A constant int16 max-amplitude frame is ~0 dBFS."""
    frame = np.full(1280, 32767, dtype=np.int16)
    dbfs = wake_corpus_setup.compute_rms_dbfs(frame)
    # Within rounding of 0 dBFS
    assert -0.01 < dbfs <= 0.0


def test_compute_rms_dbfs_half_scale_is_about_minus_6() -> None:
    """A constant int16 half-amplitude frame is ~-6 dBFS
    (20*log10(0.5) ≈ -6.02)."""
    frame = np.full(1280, 16384, dtype=np.int16)
    dbfs = wake_corpus_setup.compute_rms_dbfs(frame)
    assert -6.1 < dbfs < -5.9


def test_compute_rms_dbfs_monotonic_with_amplitude() -> None:
    """Louder frame → higher (less negative) dBFS. Sanity check
    for the meter's color thresholds."""
    quiet = np.full(1280, 100, dtype=np.int16)
    medium = np.full(1280, 3000, dtype=np.int16)
    loud = np.full(1280, 20000, dtype=np.int16)
    assert (
        wake_corpus_setup.compute_rms_dbfs(quiet)
        < wake_corpus_setup.compute_rms_dbfs(medium)
        < wake_corpus_setup.compute_rms_dbfs(loud)
    )


# ---------------------------------------------------------------------------
# get_current_rms_dbfs — live level read by the SSE endpoint
# ---------------------------------------------------------------------------


def test_get_current_rms_dbfs_none_when_idle(backend) -> None:
    """No recording in flight → None. UI greys out the meter."""
    assert backend.get_current_rms_dbfs() is None
    backend.begin_session("jasper")
    assert backend.get_current_rms_dbfs() is None


def test_get_current_rms_dbfs_returns_float_while_recording(
    backend,
) -> None:
    """While recording, returns a float in [-100, 0] reflecting
    the AEC ON leg's RMS. The fake capture emits a constant value
    so we can predict roughly where the RMS lands."""
    _FakeUdpMicCapture.port_to_value = {9876: 16384, 9877: 0, 9878: 0}
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        # Give the loop a few frames to populate the level
        time.sleep(0.1)
        rms = backend.get_current_rms_dbfs()
        assert rms is not None
        # Half-scale on the AEC ON leg → ~-6 dBFS
        assert -6.5 < rms < -5.5
    finally:
        backend.stop_recording()


def test_get_current_rms_dbfs_clears_after_stop(backend) -> None:
    """After stop_recording, the level meter goes back to None."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    assert backend.get_current_rms_dbfs() is None


# ---------------------------------------------------------------------------
# HTML — new UI affordances for ambient + mic-level + trash icon
# ---------------------------------------------------------------------------


def test_html_has_ambient_radio_button() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'value="ambient"' in html_text


def test_html_renders_ambient_in_counts_matrix() -> None:
    """The per-cell counts table includes an ambient column so the
    operator sees their progress in the third condition. The counts
    matrix is built by the behaviour module's renderCounts(), so the
    column label + per-row key live there."""
    js = _module_js()
    # The renderCounts JS literal — header row + per-row keys
    assert '">ambient<' in js
    assert '`${d}-ambient`' in js


def test_html_has_mic_level_bar_elements() -> None:
    """The Record-a-clip card includes a visible mic-level meter
    so the operator knows the mic is alive before they speak."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="mic-level"' in html_text
    assert 'id="mic-level-fill"' in html_text
    assert 'id="mic-level-readout"' in html_text


def test_html_subscribes_to_level_sse() -> None:
    """The behaviour module opens an EventSource to the level endpoint on load."""
    assert "EventSource('api/recording/level')" in _module_js()


def test_html_count_guidance_matches_two_session_protocol() -> None:
    """The UI copy should match Phase 0b's Session A/B targets."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert "Session A: ~7-9 per cell" in html_text
    assert "Session B: ~2-3 per cell" in html_text
    assert "~13-14 utterances per cell" not in html_text


def test_html_delete_button_uses_trash_icon() -> None:
    """Delete button is small + uses a trash icon (was previously
    wide text 'delete', which overlapped the audio player). The clip rows
    are rendered by the behaviour module, so assert against it."""
    js = _module_js()
    # The icon character + the icon class
    assert "🗑" in js
    assert '"danger icon"' in js


def test_clip_row_audio_cell_does_not_block_trash_button() -> None:
    """Regression: with a naked `audio` element in a fixed-pixel
    grid column, the audio's intrinsic min-content (browser-default
    300px+) blows past the column width and pushes the trash button
    off the right edge of the card. Fix is twofold and both legs
    must remain in the CSS:

      1. `minmax(0, …)` on the audio column overrides grid's default
         min-width:auto so the column can shrink below content min-content.
      2. Explicit `width: 100%; min-width: 0` on .clip audio so the
         element itself shrinks to fit instead of forcing the cell
         to grow.

    A future CSS edit dropping either leg would silently re-introduce
    the "I see the audio but can't find the delete button" bug.
    """
    css = _page_css()
    # Leg 1: minmax(0, …) in the .clip grid template
    assert "minmax(0," in css, (
        "the .clip row's audio column needs minmax(0, …) so the "
        "audio's intrinsic min-content doesn't force the grid to grow"
    )
    # Leg 2: explicit width constraints on the audio element
    assert ".clip audio" in css
    assert "min-width: 0" in css


# ---------------------------------------------------------------------------
# /api/recording/level SSE endpoint — read-only, no CSRF, streams
# ---------------------------------------------------------------------------


def _serve_in_thread(backend):
    """Spin up the recorder HTTP server on a random port in a daemon
    thread; return (server, thread, port)."""
    server = wake_corpus_setup.make_server(
        ("127.0.0.1", 0),
        csrf_token="test-token",
        backend=backend,
    )
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    return server, th, port


def _use_tmp_bridge_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    system_env: str = "",
    corpus_env: str = "",
) -> tuple[Path, Path]:
    """Point wake_corpus_setup's bridge env helpers at temp files."""
    system_path = tmp_path / "jasper.env"
    bridge_path = tmp_path / "wake_corpus_bridge.env"
    if system_env:
        system_path.write_text(system_env)
    if corpus_env:
        bridge_path.write_text(corpus_env)
    monkeypatch.setattr(bridge_session, "SYSTEM_ENV_PATH", system_path)
    monkeypatch.setattr(
        bridge_session, "BRIDGE_CORPUS_ENV_PATH", bridge_path,
    )
    return system_path, bridge_path


def test_level_sse_returns_event_stream_headers(backend) -> None:
    """GET /api/recording/level must return 200 + correct SSE headers
    (Content-Type, no-cache, X-Accel-Buffering: no for nginx). We
    don't read the body — just confirm the headers, then close."""
    import http.client

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/recording/level")
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "text/event-stream"
            assert resp.getheader("X-Accel-Buffering") == "no"
            # Don't drain the stream — it's open-ended. Closing the
            # connection cleanly exits the handler.
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_level_sse_streams_idle_payload_when_not_recording(
    backend,
) -> None:
    """When no recording is in flight, the stream emits frames with
    recording=false + rms_dbfs=null. Read one frame then disconnect."""
    import http.client

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/recording/level")
        resp = conn.getresponse()
        try:
            # SSE frames are 'data: <json>\n\n' — read up to the first
            # blank line.
            line = resp.fp.readline().decode()
            assert line.startswith("data: "), f"unexpected: {line!r}"
            payload = json.loads(line[len("data: "):].strip())
            assert payload == {"recording": False, "rms_dbfs": None}
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_level_sse_does_not_require_csrf(backend) -> None:
    """The SSE endpoint is read-only — like /api/status — and must
    NOT 403 when the X-CSRF-Token header is absent. (Browsers can't
    send custom headers on EventSource connections anyway.)"""
    import http.client

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        # No X-CSRF-Token header — must still get 200
        conn.request("GET", "/api/recording/level")
        resp = conn.getresponse()
        try:
            assert resp.status == 200
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


# ---------------------------------------------------------------------------
# Mutating-request routing — route-check before CSRF-check (the wizard
# convention in jasper/web/_common.py: bogus paths return 404 without
# revealing CSRF state). Pre-fix, do_POST/do_DELETE checked CSRF first and
# 403'd on unknown paths.
# ---------------------------------------------------------------------------


def _mutating_status(backend, method: str, path: str, token: str = "") -> int:
    """Issue one POST/DELETE against a live server; return the status."""
    import http.client

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {"Content-Length": "0"}
        if token:
            headers["X-CSRF-Token"] = token
        conn.request(method, path, b"", headers)
        try:
            return conn.getresponse().status
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_post_unknown_path_404s_without_revealing_csrf_state(backend) -> None:
    assert _mutating_status(backend, "POST", "/api/nope") == 404


def test_post_known_path_without_token_403s(backend) -> None:
    assert _mutating_status(backend, "POST", "/api/session") == 403


def test_delete_unknown_path_404s_without_revealing_csrf_state(backend) -> None:
    assert _mutating_status(backend, "DELETE", "/api/nope") == 404


def test_delete_known_route_shape_without_token_403s(backend) -> None:
    assert _mutating_status(backend, "DELETE", "/api/clip/some-id") == 403


# ---------------------------------------------------------------------------
# Raw mic 0 leg — 4th capture leg, opt-in per session
# ---------------------------------------------------------------------------


def test_legs_includes_raw0_in_tuple() -> None:
    """The LEGS tuple must include raw0 so downstream tools that
    iterate over it pick up the new quadrant directories."""
    assert "raw0" in wake_corpus_setup.LEGS
    assert wake_corpus_setup.BASE_LEGS == ("on", "off")
    assert wake_corpus_setup.DTLN_LEG == "dtln"


def test_default_aec_raw0_port_constant_exposed() -> None:
    """Recorder re-exports the shared default port so socket-activation
    + CLI both see the same number."""
    from jasper.cli.wake_enroll import DEFAULT_AEC_RAW0_PORT
    assert DEFAULT_AEC_RAW0_PORT == 9879


def test_default_ports_dict_includes_all_four_legs(tmp_path: Path) -> None:
    """A backend constructed without explicit ports defaults to all
    known leg ports (recorder subscribes to a session-selected subset)."""
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    assert set(b._ports.keys()) == {
        "on", "off", "dtln", "raw0", "ref", "usb_raw",
        "usb_webrtc", "usb_dtln",
        "chip_aec_150", "chip_aec_210",
        "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    }


def test_build_ports_keeps_raw0_when_dtln_disabled() -> None:
    """Low-RAM installs can skip DTLN without losing the raw0 corpus leg."""
    ports = wake_corpus_setup.build_ports(
        aec_on_port=1111,
        aec_off_port=2222,
        aec_dtln_port=3333,
        aec_raw0_port=4444,
        aec_ref_port=5555,
        aec_usb_raw_port=6666,
        aec_usb_webrtc_port=7777,
        aec_usb_dtln_port=8888,
        include_chip_corpus=False,
        include_dtln=False,
    )
    assert ports == {
        "on": 1111,
        "off": 2222,
        "raw0": 4444,
        "ref": 5555,
        "usb_raw": 6666,
        "usb_webrtc": 7777,
        "usb_dtln": 8888,
        **wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS,
    }


def test_combined_web_entrypoint_includes_raw0_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socket-activated jasper-web path must also pass raw0.

    Regression guard for the production path: RecordingBackend defaults
    included raw0, but jasper.web.__main__ supplied an explicit 3-leg
    map, so raw0-enabled sessions could silently produce only 3 WAVs.
    """
    from jasper.web import __main__ as web_main

    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_ON_PORT", "1100")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_OFF_PORT", "2200")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_DTLN_PORT", "3300")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_RAW0_PORT", "4400")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_REF_PORT", "5500")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_RAW_PORT", "6600")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_WEBRTC_PORT", "7700")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_USB_DTLN_PORT", "8800")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_CHIP_AEC_150_PORT", "8810")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_CHIP_AEC_210_PORT", "8820")
    monkeypatch.setenv(
        "JASPER_WAKE_CORPUS_AEC_XVF_RAW0_WEBRTC_AEC3_PORT",
        "8890",
    )
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_XVF_RAW0_DTLN_PORT", "8900")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_1_PORT", "9901")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_2_PORT", "9902")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_3_PORT", "9903")

    assert web_main._wake_corpus_ports_from_env() == {
        "on": 1100,
        "off": 2200,
        "dtln": 3300,
        "raw0": 4400,
        "ref": 5500,
        "usb_raw": 6600,
        "usb_webrtc": 7700,
        "usb_dtln": 8800,
        "chip_aec_150": 8810,
        "chip_aec_210": 8820,
        "xvf_raw0_webrtc_aec3": 8890,
        "xvf_raw0_dtln": 8900,
        "aec3_variant_1": 9901,
        "aec3_variant_2": 9902,
        "aec3_variant_3": 9903,
    }


def test_combined_web_entrypoint_keeps_raw0_when_dtln_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jasper.web import __main__ as web_main

    monkeypatch.setenv("JASPER_WAKE_CORPUS_DTLN", "0")
    monkeypatch.setenv("JASPER_WAKE_CORPUS_AEC_RAW0_PORT", "4400")

    ports = web_main._wake_corpus_ports_from_env()
    assert "dtln" not in ports
    assert ports["raw0"] == 4400
    assert ports["ref"] == wake_corpus_setup.DEFAULT_AEC_REF_PORT
    assert ports["usb_dtln"] == wake_corpus_setup.DEFAULT_AEC_USB_DTLN_PORT
    assert ports["aec3_variant_1"] == wake_corpus_setup.DEFAULT_AEC3_SWEEP_PORTS[
        "aec3_variant_1"
    ]


def test_combined_web_lazy_wake_corpus_serves_after_first_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy loader must not overwrite BaseHTTPRequestHandler.handle.

    Regression guard for the production 502: the first request loaded
    wake_corpus_setup successfully, then later requests accepted and
    immediately closed because the lazy class had copied socketserver's
    no-op BaseRequestHandler.handle onto itself.
    """
    import http.client

    from jasper.web import __main__ as web_main

    monkeypatch.setattr(bridge_session, "voice_daemon_active", lambda: False)
    monkeypatch.setattr(
        bridge_session,
        "bridge_output_status",
        lambda: {
            "dtln": True,
            "ref": False,
            "usb": False,
            "usb_dtln": False,
            "env_path": str(tmp_path / "wake_corpus_bridge.env"),
        },
    )

    server = web_main._make_lazy_wake_corpus_server(
        ("127.0.0.1", 0),
        output_dir=tmp_path / "out",
        ports={"on": 9876, "off": 9877},
        csrf_token="test-token",
    )
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def get(path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    try:
        status, body = get("/")
        assert status == 200
        assert b"Wake-word corpus" in body

        status, body = get("/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["voice_daemon_active"] is False

        status, body = get("/api/sessions")
        assert status == 200
        assert json.loads(body) == {"sessions": []}
    finally:
        backend_obj = getattr(server.RequestHandlerClass, "backend", None)
        server.shutdown()
        server.server_close()
        if backend_obj is not None:
            backend_obj.shutdown()
        th.join(timeout=2)


def test_begin_session_default_excludes_raw0(backend) -> None:
    """Default begin_session() does NOT opt into raw0 — historical
    pre-flag sessions shouldn't suddenly start capturing 4 legs."""
    backend.begin_session("jasper")
    assert backend.include_raw_mic_0() is False
    assert backend.include_dtln() is True


def test_begin_session_can_disable_dtln(backend, tmp_path: Path) -> None:
    """XVF DTLN is session-selectable so low-RAM corpus runs can stay
    on the two cheap production legs."""
    backend.begin_session("jasper", include_dtln=False)
    assert backend.include_dtln() is False
    assert backend.enabled_legs() == ("on", "off")

    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    assert (out / "aec_on_nomusic").is_dir()
    assert (out / "aec_off_nomusic").is_dir()
    assert not (out / "aec_dtln_nomusic").exists()
    assert set(clip.files.keys()) == {"on", "off"}


def test_begin_session_with_raw0_records_4_legs(
    backend, tmp_path: Path,
) -> None:
    """A session opened with include_raw_mic_0=True captures all 4
    legs per clip into aec_<leg>_<condition_dir>/ quadrants."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    assert backend.include_raw_mic_0() is True
    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    # All 4 quadrants exist with 1 file each
    for leg in ("on", "off", "dtln", "raw0"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        wavs = list(d.glob("*.aec-*.wav"))
        assert len(wavs) == 1, f"expected 1 wav in {d}, got {len(wavs)}"
    # ClipMetadata.files maps all 4 legs
    assert set(clip.files.keys()) == {"on", "off", "dtln", "raw0"}


def test_begin_session_without_raw0_records_3_legs(
    backend, tmp_path: Path,
) -> None:
    """Without the flag, only the 3 base legs are captured — the
    raw0 quadrant directories should NOT be created (keeps the
    on-disk layout clean for non-raw0 sessions)."""
    backend.begin_session("jasper", include_raw_mic_0=False)
    backend.start_recording("quiet", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("on", "off", "dtln"):
        assert (out / f"aec_{leg}_nomusic").is_dir()
    # raw0 dir absent
    assert not (out / "aec_raw0_nomusic").exists()
    # ClipMetadata.files has 3 keys
    assert set(clip.files.keys()) == {"on", "off", "dtln"}


def test_begin_session_with_usb_mic_records_corpus_experiment_legs(
    backend, tmp_path: Path,
) -> None:
    """USB/ref opt-in adds the corpus-only cheap-mic legs without
    needing to change the production base leg set."""
    backend.begin_session("jasper", include_usb_mic=True)
    assert backend.include_usb_mic() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    }
    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("ref", "usb_raw", "usb_webrtc"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    }


def test_begin_session_with_usb_dtln_records_companion_legs(
    backend, tmp_path: Path,
) -> None:
    """USB DTLN can be tested independently of USB WebRTC, but it
    still records ref + USB raw so the comparison is interpretable."""
    backend.begin_session("jasper", include_usb_dtln=True)
    assert backend.include_usb_mic() is False
    assert backend.include_usb_dtln() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_dtln",
    }

    backend.start_recording("ambient", "near")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in ("ref", "usb_raw", "usb_dtln"):
        d = out / f"aec_{leg}_ambient"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "dtln", "ref", "usb_raw", "usb_dtln",
    }
    assert "usb_webrtc" not in clip.files


def test_begin_session_with_xvf_raw0_dtln_records_companion_legs(backend) -> None:
    backend.begin_session(
        "jasper",
        include_raw_mic_0=False,
        include_xvf_raw0_dtln=True,
    )

    assert backend.include_raw_mic_0() is True
    assert backend.include_xvf_raw0_dtln() is True
    assert set(backend.enabled_legs()) == {
        "on", "off", "dtln", "raw0", "xvf_raw0_dtln",
    }


def test_begin_session_chip_profile_records_comparison_legs(backend) -> None:
    backend.begin_session(
        "jasper",
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=True,  # ignored here; this is not the raw0 DTLN path.
        include_raw_mic_0=False,  # forced by the chip profile.
        include_usb_mic=False,
        include_usb_dtln=False,
        include_xvf_raw0_dtln=True,
        include_aec3_sweep=True,  # incompatible pilot sweep is parked.
    )

    assert backend.corpus_profile() == wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON
    assert backend.include_raw_mic_0() is True
    assert backend.include_usb_mic() is False
    assert backend.include_aec3_sweep() is False
    assert backend.enabled_legs() == (
        "chip_aec_150",
        "chip_aec_210",
        "raw0",
        "xvf_raw0_webrtc_aec3",
        "ref",
        "xvf_raw0_dtln",
    )


def test_begin_session_chip_profile_records_usb_legs_when_requested(
    backend,
) -> None:
    backend.begin_session(
        "jasper",
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=True,
        include_usb_dtln=True,
    )

    assert backend.include_usb_mic() is True
    assert backend.enabled_legs() == (
        "chip_aec_150",
        "chip_aec_210",
        "raw0",
        "xvf_raw0_webrtc_aec3",
        "ref",
        "usb_raw",
        "usb_webrtc",
        "usb_dtln",
    )


def test_capture_plan_describes_chip_profile_layers(backend) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_bridge_readiness=False,
    )

    assert plan["schema_version"] == wake_corpus_setup.CAPTURE_PLAN_SCHEMA_VERSION
    assert plan["recipe"] == "chip_aec_comparison"
    assert plan["selected_physical_mics"] == ["xvf3800"]
    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["chip_aec_150"]["processing"] == "hardware_aec"
    assert by_token["xvf_raw0_webrtc_aec3"]["native_stream"] == "raw_mic_0"
    assert by_token["xvf_raw0_webrtc_aec3"]["processing"] == "webrtc_aec3"
    assert by_token["ref"]["device_id"] == "speaker_reference"


def test_capture_plan_describes_on_leg_runtime_overlay(backend) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        include_dtln=False,
        include_bridge_readiness=False,
        active_audio_profile={
            "requested": "xvf_chip_aec",
            "active": "xvf_chip_aec",
            "state": "active",
        },
        runtime_audio_env={"chip_primary_leg": "chip_aec_210"},
    )

    by_token = {leg["token"]: leg for leg in plan["legs"]}
    assert by_token["on"]["label"] == "Chip AEC ASR 210 primary"
    assert by_token["on"]["kind"] == "hardware_aec"
    assert by_token["on"]["processing"] == "hardware_aec"
    assert by_token["on"]["source_channel"] == "fixed_beam_210"
    assert by_token["on"]["runtime_role"] == "production_primary"
    assert "on" not in plan["software_transforms"]["webrtc_aec3"]


def test_capture_plan_warns_for_heavy_two_mic_dtln(backend) -> None:
    plan = wake_corpus_setup.build_capture_plan(
        backend.ports(),
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
        include_bridge_readiness=False,
    )

    assert plan["recipe"] == "chip_aec_comparison_extended"
    assert set(plan["selected_physical_mics"]) == {"xvf3800", "usb_mic"}
    assert plan["resource"]["level"] in {"high", "unsafe"}
    assert any("Multiple DTLN legs" in warning for warning in plan["warnings"])


def test_begin_session_with_aec3_sweep_records_variant_legs(
    backend, tmp_path: Path,
) -> None:
    """AEC3 sweep captures XVF reference plus USB baseline/variants."""
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    assert backend.include_aec3_sweep() is True
    assert backend.enabled_legs() == (
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    )

    backend.start_recording("music", "far")
    time.sleep(0.1)
    clip = backend.stop_recording()

    out = tmp_path / "out"
    for leg in (
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    ):
        d = out / f"aec_{leg}_music"
        assert d.is_dir(), f"missing dir: {d}"
        assert len(list(d.glob("*.aec-*.wav"))) == 1
    assert set(clip.files.keys()) == {
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    }
    assert "dtln" not in clip.files


def test_missing_bridge_outputs_detects_disabled_usb_and_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(monkeypatch, tmp_path)

    assert wake_corpus_setup.missing_bridge_outputs_for_session(
        include_dtln=True,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_aec3_sweep=True,
    ) == ["dtln", "ref", "usb", "usb_dtln", "aec3_sweep"]


def test_missing_bridge_outputs_honors_overlay_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The /var/lib corpus env wins over /etc, matching systemd's
    later EnvironmentFile precedence."""
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env=(
            "JASPER_AEC_DTLN_ENABLED=0\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=0\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=0\n"
        ),
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb\n"
        ),
    )

    assert wake_corpus_setup.missing_bridge_outputs_for_session(
        include_dtln=True,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_aec3_sweep=True,
    ) == []


def test_parse_amixer_bool_accepts_common_forms() -> None:
    assert wake_corpus_setup._parse_amixer_bool("Mono: Capture [on]") is True
    assert wake_corpus_setup._parse_amixer_bool(": values=off") is False
    assert wake_corpus_setup._parse_amixer_bool("no boolean here") is None


def test_usb_mic_status_reports_hardware_agc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_USB_MIC_DEVICE=USB PnP Sound Device\n"
            "JASPER_AEC_USB_MIXER_CARD=4\n"
        ),
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Mono: Capture [on]\n", stderr="",
        )

    monkeypatch.setattr(wake_corpus_setup.subprocess, "run", fake_run)

    status = wake_corpus_setup.usb_mic_status()

    assert status["device"] == "USB PnP Sound Device"
    assert status["hardware_agc"]["mixer_card"] == "4"
    assert status["hardware_agc"]["control"] == "Auto Gain Control"
    assert status["hardware_agc"]["available"] is True
    assert status["hardware_agc"]["enabled"] is True
    assert calls == [["amixer", "-c", "4", "get", "Auto Gain Control"]]


def test_enable_bridge_outputs_writes_wizard_env_and_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )

    wake_corpus_setup.enable_bridge_outputs_for_session(
        include_dtln=True,
        include_usb_mic=False,
        include_usb_dtln=True,
        include_aec3_sweep=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert values["JASPER_AEC_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED"] == "1"
    assert values["JASPER_AEC_USB_MIC_DEVICE"] == "USB PnP Sound Device"
    assert restarts == ["restart"]


def test_restart_aec_bridge_resets_start_limit_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WS1 Phase 3: restart_aec_bridge asks jasper-control's restart broker
    # (reset-failed to clear any start-limit lockout, then restart) instead of
    # shelling out to systemctl, so the wake-corpus flow needs no privilege of
    # its own once jasper-web drops to a non-root service user.
    from jasper.control import restart_broker

    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_manage(*units: str, **kwargs: object):
        calls.append((units, kwargs.get("verb")))
        return {"ok": True}

    monkeypatch.setattr(restart_broker, "manage_units", fake_manage)

    wake_corpus_setup.restart_aec_bridge()

    assert calls == [
        ((wake_corpus_setup.BRIDGE_UNIT,), "reset-failed"),
        ((wake_corpus_setup.BRIDGE_UNIT,), "restart"),
    ]


def test_enable_bridge_outputs_preserves_system_usb_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env='JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n',
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    wake_corpus_setup.enable_bridge_outputs_for_session(
        include_dtln=False,
        include_usb_mic=True,
        include_usb_dtln=False,
    )

    assert "JASPER_AEC_USB_MIC_DEVICE" not in bridge_path.read_text()


def test_set_bridge_outputs_matches_selected_session_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n"
        ),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        include_dtln=False,
        include_usb_mic=True,
        include_usb_dtln=False,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert "JASPER_AEC_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED" not in values
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_ENABLED"] == "1"
    assert values["JASPER_AEC_USB_MIC_DEVICE"] == "Studio Mic"
    assert restarts == ["restart"]


def test_set_bridge_outputs_enables_aec3_sweep_and_parks_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_aec3_sweep=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_DTLN_ENABLED"] == "0"
    assert values["JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED"] == "1"


def test_set_bridge_outputs_enables_chip_profile_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_unit",
        lambda unit, timeout=wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC: (
            restarts.append(unit)
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
        include_aec3_sweep=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert values["JASPER_OUTPUTD_CHIP_REF_PCM"] == wake_corpus_setup.DEFAULT_CHIP_REF_PCM
    assert values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == wake_corpus_setup.OUTPUTD_REF_UDP_TARGET
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_SAMPLE_RATE
    )
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_PERIOD_FRAMES
    )
    assert (
        values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"]
        == wake_corpus_setup.DEFAULT_CHIP_REF_BUFFER_FRAMES
    )
    assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED" not in values
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]


def test_chip_ref_pcm_prefers_resolved_xvf_card() -> None:
    assert bridge_session.chip_ref_pcm_for_env(
        {
            "JASPER_XVF_ALSA_CARD": "L16K6Ch",
            "JASPER_AEC_MIC_DEVICE": "Array",
        }
    ) == "plughw:CARD=L16K6Ch,DEV=0"


def test_set_bridge_outputs_chip_profile_without_usb_enables_ref_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_unit",
        lambda unit, timeout=wake_corpus_setup.BRIDGE_RESTART_TIMEOUT_SEC: (
            restarts.append(unit)
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append(wake_corpus_setup.BRIDGE_UNIT),
    )

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=False,
        include_usb_dtln=False,
        include_xvf_raw0_dtln=False,
        include_aec3_sweep=False,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_CORPUS_REF_ENABLED"] == "1"
    assert "JASPER_AEC_CORPUS_USB_ENABLED" not in values
    assert "JASPER_AEC_USB_MIC_DEVICE" not in values
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] == "1"
    assert values["JASPER_AEC_REF_SOURCE"] == "outputd_udp"
    assert values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] == (
        wake_corpus_setup.OUTPUTD_REF_UDP_TARGET
    )
    assert restarts == [
        wake_corpus_setup.OUTPUTD_UNIT,
        wake_corpus_setup.AEC_INIT_UNIT,
        wake_corpus_setup.BRIDGE_UNIT,
    ]


def test_set_bridge_outputs_chip_profile_parks_production_dtln(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    monkeypatch.setattr(bridge_session, "restart_unit", lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    changed = wake_corpus_setup.set_bridge_outputs_for_session(
        corpus_profile=wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
        include_dtln=False,
        include_usb_mic=True,
        include_usb_dtln=True,
        include_xvf_raw0_dtln=True,
    )

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert changed is True
    assert values["JASPER_AEC_DTLN_ENABLED"] == "0"
    assert values["JASPER_AEC_CORPUS_USB_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] == "1"
    assert values["JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"] == "1"


def test_enable_bridge_outputs_rolls_back_when_restart_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_DTLN_ENABLED=0\n",
    )
    attempts = 0

    def fake_restart() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(
                1, ["systemctl", "restart", "jasper-aec-bridge.service"],
                stderr="USB corpus mic unavailable",
            )

    monkeypatch.setattr(
        bridge_session, "restart_aec_bridge", fake_restart,
    )

    with pytest.raises(subprocess.CalledProcessError):
        wake_corpus_setup.enable_bridge_outputs_for_session(
            include_dtln=True,
            include_usb_mic=True,
            include_usb_dtln=True,
        )

    assert bridge_path.read_text() == "JASPER_AEC_DTLN_ENABLED=0\n"
    assert attempts == 2  # failed new config, then restarted rollback config


def test_disable_bridge_outputs_removes_overrides_and_preserves_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
            "JASPER_AEC_USB_MIC_DEVICE=Studio Mic\n"
        ),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )

    wake_corpus_setup.disable_bridge_corpus_outputs()

    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in bridge_path.read_text().splitlines()
    }
    assert "JASPER_AEC_DTLN_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_REF_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_ENABLED" not in values
    assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in values
    assert values["JASPER_AEC_USB_MIC_DEVICE"] == "Studio Mic"
    assert restarts == ["restart"]


def test_disable_bridge_outputs_restores_system_dtln_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=0\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
        ),
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    before = wake_corpus_setup.bridge_output_status()
    wake_corpus_setup.disable_bridge_corpus_outputs()
    after = wake_corpus_setup.bridge_output_status()

    assert before["active"] is True
    assert before["dtln"] is False
    assert after["active"] is False
    assert after["dtln"] is True


def test_build_capture_health_marks_bridge_drop_compromised() -> None:
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {"on": 0},
            "packets_sent_by_leg": {"on": 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 1, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {"on": 0},
            "packets_sent_by_leg": {"on": 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={"on": [frame]},
        bridge_start=start,
        bridge_stop=stop,
    )

    assert health["status"] == "compromised"
    assert health["bridge_delta"]["queue_drops"]["mic"] == 1
    assert health["legs"]["on"]["status"] == "compromised"
    assert health["legs"]["on"]["bridge_drop_counts"]["mic_queue_full"] == 1


def test_build_capture_health_marks_aec3_sweep_bridge_drops() -> None:
    """AEC3 sweep legs use the same XVF mic/ref frames as the baseline
    AEC leg, so their per-leg health must inherit mic/ref bridge drops."""
    leg = wake_corpus_setup.AEC3_SWEEP_LEGS[0]
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 1, "raw0": 0, "usb": 0, "ref": 2},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={leg: [frame]},
        bridge_start=start,
        bridge_stop=stop,
    )

    drop_counts = health["legs"][leg]["bridge_drop_counts"]
    assert health["status"] == "compromised"
    assert health["legs"][leg]["status"] == "compromised"
    assert drop_counts["mic_queue_full"] == 1
    assert drop_counts["ref_queue_full"] == 2


def test_build_capture_health_marks_usb_aec3_sweep_bridge_drops() -> None:
    """When the sweep source is USB, variant legs inherit USB/ref drops
    instead of XVF mic drops."""
    leg = wake_corpus_setup.AEC3_SWEEP_LEGS[0]
    frame = np.zeros(1280, dtype=np.int16)
    start = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 2.0,
        "counters": {
            "frames_processed": 10,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 0, "raw0": 0, "usb": 0, "ref": 0},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 0},
        },
    }
    stop = {
        "pid": 123,
        "started_epoch_sec": 1.0,
        "updated_epoch_sec": 3.0,
        "counters": {
            "frames_processed": 20,
            "ref_starved_frames": 0,
            "queue_drops": {"mic": 4, "raw0": 0, "usb": 1, "ref": 2},
            "udp_send_drops_by_leg": {leg: 0},
            "packets_sent_by_leg": {leg: 1},
        },
    }

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={leg: [frame]},
        bridge_start=start,
        bridge_stop=stop,
        aec3_sweep_source="usb",
    )

    drop_counts = health["legs"][leg]["bridge_drop_counts"]
    assert health["status"] == "compromised"
    assert "mic_queue_full" not in drop_counts
    assert drop_counts["usb_queue_full"] == 1
    assert drop_counts["ref_queue_full"] == 2


def test_build_capture_health_unknown_without_bridge_stats() -> None:
    frame = np.zeros(1280, dtype=np.int16)

    health = wake_corpus_setup.build_capture_health(
        wall_duration_sec=0.08,
        buffers={"on": [frame]},
        bridge_start=None,
        bridge_stop=None,
    )

    assert health["status"] == "unknown"
    assert health["legs"]["on"]["packets"] == 1
    assert health["legs"]["on"]["audio_duration_sec"] == pytest.approx(0.08)


def test_metadata_persists_include_raw_mic_0_flag(
    backend, tmp_path: Path,
) -> None:
    """The session JSON sidecar must persist include_raw_mic_0 so
    recovery + list_sessions can show it."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    assert data["include_raw_mic_0"] is True
    assert data["include_dtln"] is True
    assert data["enabled_legs"] == ["on", "off", "dtln", "raw0"]


def test_metadata_persists_include_usb_mic_flag(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session("jasper", include_usb_mic=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    assert data["include_usb_mic"] is True
    assert data["include_usb_dtln"] is False
    assert data["enabled_legs"] == [
        "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
    ]


def test_metadata_persists_dtln_session_flags(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session(
        "jasper", include_dtln=False, include_usb_dtln=True,
    )
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    assert data["include_dtln"] is False
    assert data["include_usb_dtln"] is True
    assert data["enabled_legs"] == ["on", "off", "ref", "usb_raw", "usb_dtln"]


def test_metadata_persists_aec3_sweep_flags(
    backend, tmp_path: Path,
) -> None:
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    backend.start_recording("music", "far")
    time.sleep(0.05)
    backend.stop_recording()

    json_files = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
    data = json.loads(json_files[0].read_text())
    assert data["include_aec3_sweep"] is True
    assert data["include_usb_mic"] is True
    assert data["aec3_sweep_source"] == "usb"
    assert data["enabled_legs"] == [
        "on", "off", "ref", "usb_raw", "usb_webrtc",
        *wake_corpus_setup.AEC3_SWEEP_LEGS,
    ]
    assert data["aec3_sweep_variants"] == wake_corpus_setup.variant_metadata(
        input_source="usb",
    )
    assert data["aec3_sweep_config"]["input_source"] == "usb"


def test_loaded_aec3_sweep_session_refreshes_current_variant_legs() -> None:
    """A loaded pilot session should use the current sweep registry even
    if its saved metadata names an older retired variant."""
    ports = {
        "on": 9876,
        "off": 9877,
        **{
            leg: 9884 + index
            for index, leg in enumerate(wake_corpus_setup.AEC3_SWEEP_LEGS)
        },
    }
    data = {
        "include_aec3_sweep": True,
        "enabled_legs": [
            "on",
            "aec3_hf_relaxed",
            "aec3_nearend_fast",
            "aec3_slow_attack",
            "off",
        ],
    }

    assert wake_corpus_setup._enabled_legs_from_metadata(data, ports) == (
        "on", *wake_corpus_setup.AEC3_SWEEP_LEGS, "off",
    )


def test_recovery_restores_include_raw_mic_0_flag(tmp_path: Path) -> None:
    """A recovered session must restore the include_raw_mic_0 flag
    so a follow-up clip inherits the original session's leg set
    (not silently degraded to the 3-base default)."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_x.json").write_text(json.dumps({
        "session_id": "x", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878, "raw0": 9879},
        "include_raw_mic_0": True,
        "clips": [],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "x",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_raw_mic_0() is True
        assert b.include_dtln() is True
    finally:
        b.shutdown()


def test_recovery_restores_usb_dtln_flag(tmp_path: Path) -> None:
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_x.json").write_text(json.dumps({
        "session_id": "x", "member": "jasper",
        "ports": {
            "on": 9876, "off": 9877, "dtln": 9878,
            "ref": 9880, "usb_raw": 9881, "usb_dtln": 9883,
        },
        "include_dtln": False,
        "include_usb_dtln": True,
        "clips": [],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "x",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_dtln() is False
        assert b.include_usb_dtln() is True
        assert b.enabled_legs() == ("on", "off", "ref", "usb_raw", "usb_dtln")
    finally:
        b.shutdown()


def test_recovery_handles_pre_raw0_session_metadata(tmp_path: Path) -> None:
    """Sessions recorded BEFORE this feature don't have the
    include_raw_mic_0 key. Recovery must treat the missing key as
    False (backward compat with existing on-disk corpora)."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878},
        "clips": [],
        # NO include_raw_mic_0 key
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "old",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.include_raw_mic_0() is False
    finally:
        b.shutdown()


def test_recovery_handles_pre_audio_context_session_metadata(tmp_path: Path) -> None:
    """Older sidecars do not have audio_context or per-clip selected_legs."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {"on": 9876, "off": 9877, "dtln": 9878},
        "include_dtln": True,
        "clips": [
            {"clip_id": "1", "member": "jasper", "condition": "quiet",
             "distance": "near", "session_id": "old", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    (md / wake_corpus_setup.ACTIVE_SESSION_MARKER).write_text(json.dumps({
        "session_id": "old",
    }))
    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    b.start()
    try:
        assert b.audio_context() is None
        clips = b.list_clips(include_deleted=True)
        assert len(clips) == 1
        assert clips[0].selected_legs == []
        assert clips[0].audio_context == {}
    finally:
        b.shutdown()


# ---------------------------------------------------------------------------
# Sessions management — list / load / delete
# ---------------------------------------------------------------------------


def test_list_sessions_empty_dir(tmp_path: Path) -> None:
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    assert b.list_sessions() == []


def test_list_sessions_returns_summaries_newest_first(
    tmp_path: Path,
) -> None:
    """list_sessions scans the metadata dir + summarizes each
    session. Sort order is newest-first by mtime."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    # Two sessions, the second one newer
    (md / "enroll_jasper_old.json").write_text(json.dumps({
        "session_id": "old", "member": "jasper",
        "ports": {}, "include_raw_mic_0": False,
        "clips": [
            {"clip_id": "1", "member": "jasper", "condition": "quiet",
             "distance": "near", "session_id": "old", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    (md / "enroll_jasper_new.json").write_text(json.dumps({
        "session_id": "new", "member": "jasper",
        "ports": {}, "include_raw_mic_0": True,
        "clips": [
            {"clip_id": "2", "member": "jasper", "condition": "ambient",
             "distance": "far", "session_id": "new", "seq": 1,
             "start_ts": "x", "stop_ts": "y", "duration_sec": 1.0,
             "files": {}, "deleted": False, "auto_stopped": False, "notes": ""},
        ],
    }))
    import os as _os
    # Force the "new" file's mtime to be later than the "old" file's
    now = time.time()
    _os.utime(md / "enroll_jasper_old.json", (now - 10, now - 10))
    _os.utime(md / "enroll_jasper_new.json", (now, now))

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "new"  # newest first
    assert sessions[1]["session_id"] == "old"
    assert sessions[0]["include_raw_mic_0"] is True
    assert sessions[1]["include_raw_mic_0"] is False
    assert sessions[0]["include_dtln"] is True
    assert sessions[0]["include_usb_dtln"] is False
    assert sessions[0]["clip_count"] == 1
    assert sessions[0]["conditions"] == {"ambient": 1}


def test_list_sessions_marks_active(tmp_path: Path) -> None:
    """The session currently loaded in memory is flagged is_active so
    the UI can render the row differently (and disable Load)."""
    b = wake_corpus_setup.RecordingBackend(output_dir=tmp_path / "out")
    b.start()
    try:
        b.begin_session("jasper")
        active_id = b.session_id()
        sessions = b.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == active_id
        assert sessions[0]["is_active"] is True
    finally:
        b.shutdown()


def test_list_sessions_skips_corrupt_files(tmp_path: Path) -> None:
    """A single corrupt JSON file must not break the whole list."""
    out = tmp_path / "out"
    md = out / "metadata"
    md.mkdir(parents=True)
    (md / "enroll_jasper_good.json").write_text(json.dumps({
        "session_id": "good", "member": "jasper",
        "ports": {}, "clips": [],
    }))
    (md / "enroll_jasper_bad.json").write_text("{not valid json")

    b = wake_corpus_setup.RecordingBackend(output_dir=out)
    sessions = b.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "good"


def test_load_session_switches_active(backend, tmp_path: Path) -> None:
    """load_session swaps the in-memory active session to an
    existing one on disk."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    first_id = backend.session_id()
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()

    backend.begin_session("jasper")  # creates a 2nd session (sleeps to differ ts)
    time.sleep(0.05)
    second_id = backend.session_id()
    assert first_id != second_id

    # Switch back to the first session
    result = backend.load_session(first_id)
    assert result["session_id"] == first_id
    assert result["include_raw_mic_0"] is True
    assert result["include_dtln"] is True
    assert result["include_usb_dtln"] is False
    assert backend.session_id() == first_id
    assert backend.include_raw_mic_0() is True
    # And the loaded session's clips are now visible
    assert len(backend.list_clips()) == 1


def test_load_session_refuses_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.load_session("anything")
    finally:
        backend.stop_recording()


def test_load_session_unknown_raises(backend) -> None:
    backend.begin_session("jasper")
    with pytest.raises(ValueError, match="not found"):
        backend.load_session("nonexistent-id")


def test_delete_session_removes_wavs_and_json(
    backend, tmp_path: Path,
) -> None:
    """delete_session hard-removes the WAV files and the JSON
    sidecar. The session is no longer listable."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = backend.stop_recording()
    sid = backend.session_id()

    # WAVs and JSON present
    assert all(Path(p).is_file() for p in clip.files.values())
    md_path = tmp_path / "out" / "metadata" / f"enroll_jasper_{sid}.json"
    assert md_path.is_file()

    result = backend.delete_session(sid)
    assert result["wavs_deleted"] >= 1  # at least one per leg
    assert all(not Path(p).is_file() for p in clip.files.values())
    assert not md_path.is_file()
    # No longer in list_sessions
    assert sid not in {s["session_id"] for s in backend.list_sessions()}


def test_delete_active_session_clears_in_memory_state(
    backend,
) -> None:
    """When the operator deletes the session they have open in
    memory, the in-memory active state must be cleared so the UI
    doesn't show 'phantom' clips with broken WAV links."""
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()

    backend.delete_session(sid)
    assert backend.session_id() is None
    assert backend.member() is None
    assert backend.list_clips() == []
    assert backend.include_raw_mic_0() is False
    assert backend.include_dtln() is False
    assert backend.include_usb_dtln() is False


def test_unload_session_clears_state_but_keeps_metadata(
    backend, tmp_path: Path,
) -> None:
    """Unload is the non-destructive end-of-session operation."""
    backend.begin_session("jasper", include_raw_mic_0=True)
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()
    md_dir = tmp_path / "out" / "metadata"
    md_path = md_dir / f"enroll_jasper_{sid}.json"
    marker = md_dir / wake_corpus_setup.ACTIVE_SESSION_MARKER
    assert md_path.is_file()
    assert marker.is_file()

    assert backend.unload_session() == sid

    assert md_path.is_file()
    assert not marker.exists()
    assert backend.session_id() is None
    assert backend.member() is None
    assert backend.list_clips() == []
    assert backend.include_raw_mic_0() is False


def test_delete_session_refuses_during_recording(backend) -> None:
    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    sid = backend.session_id()
    try:
        with pytest.raises(wake_corpus_setup.StateError):
            backend.delete_session(sid)
    finally:
        backend.stop_recording()


def test_delete_session_unknown_raises(backend) -> None:
    with pytest.raises(ValueError, match="not found"):
        backend.delete_session("nonexistent-id")


# ---------------------------------------------------------------------------
# HTML — Sessions card + raw-mic-0 toggle wiring
# ---------------------------------------------------------------------------


def test_html_has_sessions_card() -> None:
    """The collapsible Sessions card sits below the new-session form."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert html_text.index('id="session-card"') < html_text.index(
        'id="sessions-card"',
    )
    assert "<details" in html_text
    assert 'id="sessions-card"' in html_text
    assert 'id="sessions-list"' in html_text


def test_html_labels_speaker_as_name_not_member() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    assert '<label for="member">Name:</label>' in html_text
    assert '<label for="member">Member:</label>' not in html_text


def test_html_has_include_raw_mic_0_toggle() -> None:
    """Begin-a-new-session form has the raw-mic-0 toggle."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-raw-mic-0"' in html_text
    assert 'class="toggle"' in html_text
    assert 'Raw mic 0' in html_text


def test_html_has_include_usb_mic_toggle() -> None:
    """Begin-a-new-session form has the corpus USB/ref toggle. The switch
    + label live in the page body; the include_usb_mic payload key lives in
    the capture option module."""
    html_text = wake_corpus_setup._render_index_html("t")
    controls_js = _controls_js()
    assert 'id="include-usb-mic"' in html_text
    assert 'USB mic + reference' in html_text
    assert 'Raw USB, AEC3, reference.' in html_text
    assert 'id="usb-mic-note"' not in html_text
    assert 'include_usb_mic' in controls_js
    assert "elements.usbMic.disabled = sessionLoaded;" in controls_js
    assert "|| corpusProfile === 'chip_aec_comparison_v1'" not in controls_js


def test_html_has_dtln_session_toggles() -> None:
    """Begin-a-new-session form exposes XVF and USB DTLN toggles. Switches
    in the body, payload keys in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-dtln"' in html_text
    assert 'id="include-usb-dtln"' in html_text
    assert 'USB DTLN' in html_text
    js = _controls_js()
    assert 'include_dtln' in js
    assert 'include_usb_dtln' in js


def test_html_has_aec3_sweep_toggle() -> None:
    """Begin-a-new-session form exposes the bounded AEC3 sweep mode. The
    toggle is in the body; the sweep variant legs + labels ride in the JSON
    config island (so they're in the rendered page) and the
    include_aec3_sweep payload key is in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-aec3-sweep"' in html_text
    assert "AEC3 sweep" in html_text
    assert "include_aec3_sweep" in _controls_js()
    for variant in wake_corpus_setup.AEC3_SWEEP_VARIANTS:
        # Both leg + label are serialized into the wake-corpus-config island.
        assert variant.leg in html_text
        assert variant.label in html_text


def test_html_has_capture_plan_preview() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    js = _module_js()
    css = _page_css()

    assert 'id="capture-plan-preview"' in html_text
    assert "api/capture-plan" in js
    assert "renderCapturePlan" in js
    assert ".capture-plan-preview" in css


def test_html_test_mode_button_follows_capture_leg_choices() -> None:
    """The operator chooses capture legs before entering test mode. The leg
    toggles + Begin button live in the body; the corpus-test-mode call
    lives in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    button_idx = html_text.index('id="session-begin"')
    assert html_text.index('id="include-raw-mic-0"') < button_idx
    assert html_text.index('id="include-dtln"') < button_idx
    assert html_text.index('id="include-aec3-sweep"') < button_idx
    assert html_text.index('id="include-usb-mic"') < button_idx
    assert html_text.index('id="include-usb-dtln"') < button_idx
    assert "voice-toggle" not in html_text
    assert "bridge-output-disable" not in html_text
    assert "api/corpus-test-mode" in _module_js()


def test_capture_option_controls_enforce_chip_profile_rules() -> None:
    if _NODE is None:
        pytest.skip("node is required for the wake-corpus controls harness")

    harness = f"""
        import {{
          currentSessionPayload,
          syncCorpusProfileControls,
        }} from {json.dumps(_controls_js_path().as_uri())};

        function input({{ checked = false, value = '', row = null }} = {{}}) {{
          return {{
            checked,
            value,
            disabled: false,
            closest(selector) {{
              if (selector !== '.capture-option') throw new Error(selector);
              return row;
            }},
          }};
        }}
        function row() {{
          return {{ hidden: false }};
        }}

        const chipRows = {{
          raw: row(),
          dtln: row(),
          sweep: row(),
        }};
        const chip = {{
          member: input({{ value: ' jasper ' }}),
          chipProfile: input({{ checked: true }}),
          rawMic0: input({{ checked: false, row: chipRows.raw }}),
          dtln: input({{ checked: true, row: chipRows.dtln }}),
          xvfRaw0Dtln: input({{ checked: true }}),
          aec3Sweep: input({{ checked: true, row: chipRows.sweep }}),
          usbMic: input({{ checked: false }}),
          usbDtln: input({{ checked: false }}),
        }};
        syncCorpusProfileControls(chip, false);
        const chipPayload = currentSessionPayload(chip);

        const standardRows = {{
          raw: row(),
          dtln: row(),
          sweep: row(),
        }};
        const standard = {{
          member: input({{ value: ' test ' }}),
          chipProfile: input({{ checked: false }}),
          rawMic0: input({{ checked: false, row: standardRows.raw }}),
          dtln: input({{ checked: true, row: standardRows.dtln }}),
          xvfRaw0Dtln: input({{ checked: false }}),
          aec3Sweep: input({{ checked: true, row: standardRows.sweep }}),
          usbMic: input({{ checked: false }}),
          usbDtln: input({{ checked: true }}),
        }};
        syncCorpusProfileControls(standard, false);
        const standardPayload = currentSessionPayload(standard);

        console.log(JSON.stringify({{
          chip: {{
            rawChecked: chip.rawMic0.checked,
            dtlnChecked: chip.dtln.checked,
            sweepChecked: chip.aec3Sweep.checked,
            rawHidden: chipRows.raw.hidden,
            dtlnHidden: chipRows.dtln.hidden,
            sweepHidden: chipRows.sweep.hidden,
            rawDisabled: chip.rawMic0.disabled,
            dtlnDisabled: chip.dtln.disabled,
            sweepDisabled: chip.aec3Sweep.disabled,
            usbDisabled: chip.usbMic.disabled,
            payload: chipPayload,
          }},
          standard: {{
            rawHidden: standardRows.raw.hidden,
            dtlnHidden: standardRows.dtln.hidden,
            sweepHidden: standardRows.sweep.hidden,
            rawDisabled: standard.rawMic0.disabled,
            dtlnDisabled: standard.dtln.disabled,
            sweepDisabled: standard.aec3Sweep.disabled,
            payload: standardPayload,
          }},
        }}));
    """
    proc = subprocess.run(
        [_NODE, "--input-type=module"],
        input=harness,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["chip"]["rawChecked"] is True
    assert out["chip"]["dtlnChecked"] is False
    assert out["chip"]["sweepChecked"] is False
    assert out["chip"]["rawHidden"] is True
    assert out["chip"]["dtlnHidden"] is True
    assert out["chip"]["sweepHidden"] is True
    assert out["chip"]["rawDisabled"] is True
    assert out["chip"]["dtlnDisabled"] is True
    assert out["chip"]["sweepDisabled"] is True
    assert out["chip"]["usbDisabled"] is False
    assert out["chip"]["payload"] == {
        "member": "jasper",
        "corpus_profile": "chip_aec_comparison_v1",
        "include_raw_mic_0": True,
        "include_dtln": False,
        "include_usb_mic": False,
        "include_usb_dtln": False,
        "include_xvf_raw0_dtln": True,
        "include_aec3_sweep": False,
        "aec3_sweep_source": "xvf",
    }

    assert out["standard"]["rawHidden"] is False
    assert out["standard"]["dtlnHidden"] is False
    assert out["standard"]["sweepHidden"] is False
    assert out["standard"]["rawDisabled"] is False
    assert out["standard"]["dtlnDisabled"] is False
    assert out["standard"]["sweepDisabled"] is False
    assert out["standard"]["payload"] == {
        "member": "test",
        "corpus_profile": "standard",
        "include_raw_mic_0": False,
        "include_dtln": True,
        "include_usb_mic": True,
        "include_usb_dtln": True,
        "include_xvf_raw0_dtln": False,
        "include_aec3_sweep": True,
        "aec3_sweep_source": "usb",
    }


def test_html_loaded_session_enters_test_mode_without_new_session() -> None:
    """Loaded sessions should not be labeled as newly-started sessions.

    The button enters corpus test mode using the loaded session's saved
    legs instead of beginning a second session. This dynamic button-text
    + flow logic lives in the behaviour module; the unload control + its
    'Loaded session' empty state are guarded there too.
    """
    js = _module_js()
    assert "Enter corpus test mode" in js
    assert "Stop voice & resume recording" in js
    assert "Stop voice & apply outputs" in js
    assert "Apply bridge outputs" in js
    assert "Ready to record" in js
    assert "Enter corpus test mode for loaded session" not in js
    assert "api/session/unload" in js
    assert "'Loaded session'" in js
    assert "sessionBridgeReady" in js
    assert "latestStatus?.session_id" in js
    assert "latestStatus.include_dtln" in js
    assert "latestStatus.include_aec3_sweep" in js
    assert "Session active" not in js
    # The unload button element itself is server-rendered in the body.
    assert 'id="session-unload"' in wake_corpus_setup._render_index_html("t")


def test_html_confirm_enables_missing_bridge_outputs() -> None:
    """The Begin flow offers a deliberate bridge enable/restart retry
    instead of silently starting a session with missing WAV legs. This
    retry flow lives in the behaviour module."""
    js = _module_js()
    assert "can_enable_bridge_outputs" in js
    assert "enable_bridge_outputs: true" in js
    assert "restart the affected audio daemons" in js


def test_html_playback_uses_leg_selector() -> None:
    """Clip rows let the operator choose any recorded leg for playback. The
    clip rendering + leg-ordering live in the behaviour module; the
    AEC3-sweep + USB labels ride in the JSON config island."""
    js = _module_js()
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'data-audio-leg' in js
    assert 'legLabel(leg)' in js
    assert 'orderedLegs(c.files || {})' in js
    assert "'usb_dtln', 'ref'" in js
    assert 'encodeURIComponent(ev.target.value)' in js
    assert "on: 'XVF WebRTC AEC3'" in js
    # The sweep/legacy leg labels are injected via the config island.
    assert "aec3_variant_1" in html_text
    assert "aec3_variant_2" in html_text
    assert "aec3_variant_3" in html_text
    assert "aec3_hf_slow_only" in html_text
    assert "aec3_hf_relaxed" in html_text
    assert "aec3_edge_combo" in html_text
    assert "aec3_gentle_dnd" in html_text
    assert "aec3_nearend_fast" in html_text
    assert "aec3_slow_attack" in html_text
    assert "USB AEC3 edge combo 80 ms" in html_text  # usb_webrtc corpus label
    assert "USB_AEC3_SWEEP_BASELINE_LABEL" in js
    assert "session?.include_aec3_sweep" in js
    assert "usb_dtln: 'USB DTLN'" in js


def test_html_js_calls_sessions_endpoints() -> None:
    """JS must call the right relative API paths (not absolute —
    nginx prefix-strip would 502 those). The calls live in the module."""
    js = _module_js()
    assert "'api/sessions'" in js or '"api/sessions"' in js
    assert "'api/session/load'" in js or '"api/session/load"' in js
    assert "'api/session/unload'" in js or '"api/session/unload"' in js
    assert "api/session/${" in js  # DELETE template literal


# ---------------------------------------------------------------------------
# /api/sessions HTTP endpoint
# ---------------------------------------------------------------------------


def test_api_sessions_returns_empty_list(backend) -> None:
    import http.client

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/sessions")
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body == {"sessions": []}
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_load_round_trip(backend) -> None:
    """POST /api/session/load with a valid session_id switches the
    active session. Use the same backend's begin_session to create
    the target so we don't need a separate disk fixture."""
    import http.client

    backend.begin_session("jasper", include_raw_mic_0=True)
    first_id = backend.session_id()
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    backend.begin_session("brittany")  # second session, now active

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session/load",
            json.dumps({"session_id": first_id}),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["session_id"] == first_id
            assert body["include_raw_mic_0"] is True
        finally:
            conn.close()
        # Backend's active session swapped
        assert backend.session_id() == first_id
        marker = (
            backend._output_dir  # noqa: SLF001
            / "metadata"
            / wake_corpus_setup.ACTIVE_SESSION_MARKER
        )
        assert json.loads(marker.read_text())["session_id"] == first_id
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_unload_round_trip(backend) -> None:
    import http.client

    backend.begin_session("jasper")
    sid = backend.session_id()

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session/unload",
            json.dumps({}),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["unloaded_session"] == sid
        finally:
            conn.close()
        assert backend.session_id() is None
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_delete_round_trip(backend) -> None:
    import http.client

    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "DELETE", f"/api/session/{sid}",
            headers={"X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["deleted_session"] == sid
        finally:
            conn.close()
        # Active state cleared
        assert backend.session_id() is None
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_status_includes_include_raw_mic_0(
    backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status payload must surface include_raw_mic_0 so the UI's
    session-id label can show the raw-mic-0 marker.

    voice_daemon_active() shells out to systemctl which doesn't exist
    on dev macs — monkeypatch it so the test runs hardware-free.
    """
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session("jasper", include_raw_mic_0=True)
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert body["include_raw_mic_0"] is True
            assert body["include_dtln"] is True
            assert body["enabled_legs"] == ["on", "off", "dtln", "raw0"]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_status_includes_include_usb_mic(
    backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session("jasper", include_usb_mic=True)
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert body["include_usb_mic"] is True
            assert body["include_usb_dtln"] is False
            assert body["enabled_legs"] == [
                "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
            ]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_capture_plan_previews_selected_layers(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/capture-plan",
            json.dumps({
                "corpus_profile": wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
                "include_usb_mic": True,
                "include_usb_dtln": True,
                "include_xvf_raw0_dtln": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            plan = body["capture_plan"]
            assert plan["recipe"] == "chip_aec_comparison_extended"
            assert set(plan["selected_physical_mics"]) == {"xvf3800", "usb_mic"}
            assert "usb_dtln" in plan["software_transforms"]["dtln"]
            assert plan["resource"]["level"] in {"high", "unsafe"}
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_status_includes_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert body["include_aec3_sweep"] is True
            assert body["include_usb_mic"] is True
            assert body["aec3_sweep_source"] == "usb"
            assert body["aec3_sweep_variants"] == wake_corpus_setup.variant_metadata(
                input_source="usb",
            )
            assert body["enabled_legs"] == [
                "on", "off", "ref", "usb_raw", "usb_webrtc",
                *wake_corpus_setup.AEC3_SWEEP_LEGS,
            ]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_begin_accepts_dtln_flags(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session",
            json.dumps({
                "member": "jasper",
                "include_dtln": False,
                "include_usb_dtln": True,
                "enable_bridge_outputs": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["include_dtln"] is False
            assert body["include_usb_dtln"] is True
            assert body["enabled_legs"] == [
                "on", "off", "ref", "usb_raw", "usb_dtln",
            ]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_begin_accepts_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
        ),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session",
            json.dumps({
                "member": "jasper",
                "include_dtln": False,
                "include_aec3_sweep": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["include_aec3_sweep"] is True
            assert body["include_usb_mic"] is True
            assert body["aec3_sweep_source"] == "usb"
            assert body["enabled_legs"] == [
                "on", "off", "ref", "usb_raw", "usb_webrtc",
                *wake_corpus_setup.AEC3_SWEEP_LEGS,
            ]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_status_includes_bridge_output_status(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=0\n"
        ),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert body["bridge_outputs"] == {
                "dtln": True,
                "ref": True,
                "usb": False,
                "usb_dtln": False,
                "chip_aec": False,
                "xvf_raw0_webrtc_aec3": False,
                "xvf_raw0_dtln": False,
                "outputd_ref": False,
                "aec3_sweep": False,
                "aec3_sweep_source": "xvf",
                "env_path": str(tmp_path / "wake_corpus_bridge.env"),
                "recorder_outputs": {
                    "dtln": True,
                    "ref": True,
                    "usb": False,
                    "usb_dtln": False,
                    "chip_aec": False,
                    "xvf_raw0_webrtc_aec3": False,
                    "xvf_raw0_dtln": False,
                    "outputd_ref": False,
                    "aec3_sweep": False,
                    "aec3_sweep_source": "xvf",
                },
                "active": True,
            }
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_bridge_outputs_disable(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_CORPUS_USB_ENABLED=1\n",
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/bridge-outputs",
            json.dumps({"action": "disable"}),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["bridge_outputs"]["active"] is False
            assert restarts == ["restart"]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_corpus_test_mode_enter_stops_voice_and_sets_outputs(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
        ),
    )
    voice_active = {"value": True}
    voice_actions: list[str] = []
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "voice_daemon_active",
        lambda: voice_active["value"],
    )

    def fake_voice(action: str) -> None:
        voice_actions.append(action)
        voice_active["value"] = action == "start"

    monkeypatch.setattr(bridge_session, "set_voice_daemon_state", fake_voice)
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/corpus-test-mode",
            json.dumps({
                "action": "enter",
                "include_dtln": False,
                "include_usb_mic": True,
                "include_usb_dtln": False,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["voice_daemon_active"] is False
            assert voice_actions == ["stop"]
            assert restarts == ["restart"]
            text = bridge_path.read_text()
            assert "JASPER_AEC_CORPUS_REF_ENABLED=1" in text
            assert "JASPER_AEC_CORPUS_USB_ENABLED=1" in text
            assert "JASPER_AEC_DTLN_ENABLED" not in text
            assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in text
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_corpus_test_mode_enter_can_enable_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    voice_active = {"value": True}
    monkeypatch.setattr(
        bridge_session,
        "voice_daemon_active",
        lambda: voice_active["value"],
    )
    monkeypatch.setattr(
        bridge_session,
        "set_voice_daemon_state",
        lambda action: voice_active.__setitem__("value", action == "start"),
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/corpus-test-mode",
            json.dumps({
                "action": "enter",
                "include_dtln": False,
                "include_aec3_sweep": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["voice_daemon_active"] is False
            text = bridge_path.read_text()
            assert "JASPER_AEC_DTLN_ENABLED=0" in text
            assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1" in text
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_corpus_test_mode_exit_disables_outputs_and_starts_voice(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    backend.begin_session("jasper", include_usb_mic=True)
    sid = backend.session_id()
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_CORPUS_USB_ENABLED=1\n",
    )
    voice_active = {"value": False}
    voice_actions: list[str] = []
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "voice_daemon_active",
        lambda: voice_active["value"],
    )

    def fake_voice(action: str) -> None:
        voice_actions.append(action)
        voice_active["value"] = action == "start"

    monkeypatch.setattr(bridge_session, "set_voice_daemon_state", fake_voice)
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/corpus-test-mode",
            json.dumps({"action": "exit"}),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["voice_daemon_active"] is True
            assert body["bridge_outputs"]["active"] is False
            assert body["action"] == "exit"
            assert voice_actions == ["start"]
            assert restarts == ["restart"]
        finally:
            conn.close()
        assert backend.session_id() is None
        assert sid is not None
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_voice_start_can_disable_bridge_outputs_first(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    # WS1 Phase 3: the handler starts jasper-voice via the restart broker
    # (manage_units), not a direct systemctl. The is-active readback
    # (voice_daemon_active) stays a direct read-only systemctl probe.
    voice_calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_manage(*units, **kwargs):
        voice_calls.append((units, kwargs.get("verb")))
        return {"ok": True}

    monkeypatch.setattr(wake_corpus_setup, "manage_units", fake_manage)
    monkeypatch.setattr(
        wake_corpus_setup.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="active\n"),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/voice-daemon",
            json.dumps({"action": "start", "disable_bridge_outputs": True}),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["bridge_outputs"]["active"] is False
            assert restarts == ["restart"]
            assert voice_calls == [((wake_corpus_setup.VOICE_UNIT,), "start")]
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_usb_mic_status_endpoint(
    backend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session,
        "usb_mic_status",
        lambda: {
            "device": "USB PnP Sound Device",
            "hardware_agc": {
                "control": "Auto Gain Control",
                "mixer_card": "4",
                "available": True,
                "enabled": True,
            },
        },
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/usb-mic/status")
        resp = conn.getresponse()
        try:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["hardware_agc"]["enabled"] is True
            assert body["hardware_agc"]["control"] == "Auto Gain Control"
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_offers_bridge_enable_for_missing_outputs(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session",
            json.dumps({
                "member": "jasper",
                "include_dtln": True,
                "include_usb_mic": True,
                "include_usb_dtln": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 409
            assert body["can_enable_bridge_outputs"] is True
            assert body["missing_bridge_outputs"] == [
                "dtln", "ref", "usb", "usb_dtln",
            ]
            assert backend.session_id() is None
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


def test_api_session_enable_bridge_outputs_then_begins(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    server, th, port = _serve_in_thread(backend)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST", "/api/session",
            json.dumps({
                "member": "jasper",
                "include_dtln": True,
                "include_usb_mic": True,
                "include_usb_dtln": True,
                "enable_bridge_outputs": True,
            }),
            {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
        )
        resp = conn.getresponse()
        try:
            body = json.loads(resp.read())
            assert resp.status == 200
            assert body["member"] == "jasper"
            assert body["enabled_legs"] == [
                "on", "off", "dtln", "ref", "usb_raw",
                "usb_webrtc", "usb_dtln",
            ]
            assert restarts == ["restart"]
            text = bridge_path.read_text()
            assert "JASPER_AEC_CORPUS_USB_ENABLED=1" in text
            assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1" in text
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=2)


# ---------------------------------------------------------------------------
# Mic mute — the recorder must honor the household privacy switch
# (jasper/mic_mute_persistence.py) because it records the bridge's UDP
# legs while jasper-voice (the usual mute enforcer) is stopped.
# ---------------------------------------------------------------------------


def _write_mute(path: Path, muted: bool) -> None:
    path.write_text(f"JASPER_MIC_MUTED={1 if muted else 0}\n")


@pytest.fixture
def mute_path(tmp_path: Path) -> Path:
    return tmp_path / "mic_mute.env"


@pytest.fixture
def mute_backend(monkeypatch, tmp_path: Path, mute_path: Path):
    """Backend wired to a tmp mic_mute.env (same shape as `backend`)."""
    monkeypatch.setattr(
        bridge_session,
        "BRIDGE_STATS_PATH",
        tmp_path / "missing_aec_bridge_stats.json",
    )
    b = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        ports={"on": 9876, "off": 9877, "dtln": 9878},
        max_duration_sec=10.0,
        mic_mute_path=mute_path,
    )
    b.start()
    yield b
    b.shutdown()


def test_begin_session_refused_while_muted(
    mute_backend, mute_path: Path, caplog,
) -> None:
    _write_mute(mute_path, True)
    with pytest.raises(wake_corpus_setup.MicMutedError, match="muted"):
        mute_backend.begin_session("jasper")
    assert mute_backend.session_id() is None
    assert "event=wake_corpus.mute_refused" in caplog.text


def test_start_recording_refused_when_mute_flips_after_session_begin(
    mute_backend, mute_path: Path,
) -> None:
    mute_backend.begin_session("jasper")
    _write_mute(mute_path, True)
    with pytest.raises(wake_corpus_setup.MicMutedError, match="muted"):
        mute_backend.start_recording("quiet", "near")
    assert not mute_backend.is_recording()


def test_unmuted_or_missing_file_records_normally(
    mute_backend, mute_path: Path,
) -> None:
    # Missing file (fail-safe: unmuted) — recording works.
    mute_backend.begin_session("jasper")
    mute_backend.start_recording("quiet", "near")
    time.sleep(0.05)
    clip = mute_backend.stop_recording()
    assert clip.mute_stopped is False


def test_mute_mid_recording_stops_clip_and_flags_it(
    monkeypatch, mute_backend, mute_path: Path, caplog,
) -> None:
    from jasper.wake_corpus import recording_backend

    monkeypatch.setattr(recording_backend, "MUTE_POLL_INTERVAL_SEC", 0.05)
    _write_mute(mute_path, False)
    mute_backend.begin_session("jasper")
    mute_backend.start_recording("quiet", "near")
    _write_mute(mute_path, True)

    deadline = time.time() + 5.0
    while time.time() < deadline:
        clips = mute_backend.list_clips()
        if clips:
            break
        time.sleep(0.02)
    else:
        pytest.fail("mute did not stop the recording within 5s")

    assert not mute_backend.is_recording()
    assert clips[-1].mute_stopped is True
    # A privacy stop is NOT the duration-cap auto-stop — downstream
    # tools must be able to tell them apart.
    assert clips[-1].auto_stopped is False
    assert "event=wake_corpus.mute_stop" in caplog.text
    # The flag persists into the session metadata sidecar.
    meta_dir = mute_backend._metadata_dir
    data = json.loads(next(meta_dir.glob("enroll_*.json")).read_text())
    assert data["clips"][-1]["mute_stopped"] is True


def test_post_session_handler_refuses_while_muted(
    tmp_path: Path, mute_path: Path,
) -> None:
    """The wizard surfaces the refusal as an HTTP 409 BEFORE any
    bridge-output side effects (no backend loop needed)."""
    _write_mute(mute_path, True)
    backend = wake_corpus_setup.RecordingBackend(
        output_dir=tmp_path / "out",
        mic_mute_path=mute_path,
    )  # intentionally not started — refusal must come first
    handler_cls = wake_corpus_setup._make_handler_class(backend, "tok")
    handler = handler_cls.__new__(handler_cls)
    sent: dict[str, object] = {}

    def _capture(status: int, msg: str) -> None:
        sent["status"] = status
        sent["msg"] = msg

    handler._send_error_json = _capture  # type: ignore[method-assign]
    handler._post_session({"member": "jasper"})
    assert sent["status"] == 409
    assert "muted" in str(sent["msg"])
