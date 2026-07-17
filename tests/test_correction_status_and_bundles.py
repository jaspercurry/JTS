# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Current-correction visibility + per-session debug bundles.

Two features land in this PR. Both are exercised here:

  A) `parse_current_correction` keeps the backwards-compatible
     "JTS room correction or None" behavior, while
     `describe_current_config` gives UI/doctor surfaces the fuller
     truth about flat outputd baseline, preference, correction, active
     speaker baselines, measurement baselines, or custom CamillaDSP
     configs. /start loads a topology-preserving measurement baseline
     first so every measurement reflects the raw room rather than the
     existing correction.
  B) Each MeasurementSession writes a self-contained bundle at
     /var/lib/jasper/correction/sessions/<session_id>/ containing
     info.json (session params + state), result.json (chart curves +
     verify), per-position capture WAVs, optional verify.wav, and
     a copy of the applied CamillaDSP YAML. `scp`'able for debugging.
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.correction import bundles, evidence, status as correction_status
from jasper.correction.session import (
    MeasurementSession,
    SessionState,
    describe_current_config,
    parse_current_correction,
)
from jasper.camilla_config_contract import PeqFilter
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GraphSafety,
)
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile
from jasper.web import correction_setup
from ._web_test_helpers import json_post_with_csrf
from .correction_bundle_fixtures import write_golden_correction_bundle
from .correction_session_fixtures import (
    make_measurement_session as _make_session,
)


@pytest.fixture(autouse=True)
def _stable_no_bass_graph_authority(monkeypatch):
    async def classify(_cam):
        return GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={
                "bass_extension_profile_summary": {
                    "authority_valid": True,
                    "runtime_block_required": False,
                }
            },
        )

    monkeypatch.setattr(
        correction_setup,
        "_classify_live_bass_extension_graph",
        classify,
    )


# ---------- parse_current_correction ---------------------------------------


def test_current_correction_presentation_owns_copy_and_reset_authority():
    applied = correction_status.current_correction_presentation({
        "kind": "correction",
        "current_correction": {
            "applied_at_epoch": 1_700_000_000,
            "peq_count": 1,
        },
    })
    assert applied == {
        "tone": "applied",
        "message_template": (
            "Room correction on — 1 adjustment applied {applied_at}"
        ),
        "applied_at_epoch": 1_700_000_000,
        "reset_allowed": True,
    }

    custom = correction_status.current_correction_presentation({
        "kind": "custom",
        "message": "Advanced configuration is active.",
        "current_correction": None,
    })
    assert custom == {
        "tone": "custom",
        "message_template": "Advanced configuration is active.",
        "applied_at_epoch": None,
        "reset_allowed": True,
    }


def test_parse_current_correction_base_config_returns_none(tmp_path: Path):
    """The base outputd config is "no correction applied" —
    the UI shows the flat banner without a Reset button."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    # Anywhere that doesn't match the /var/lib/camilladsp/configs/
    # correction_* shape is treated as "no correction" by definition.
    assert parse_current_correction(
        "/etc/camilladsp/outputd-cutover.yml", config_dir=cfg_dir,
    ) is None
    assert parse_current_correction(None, config_dir=cfg_dir) is None
    assert parse_current_correction("", config_dir=cfg_dir) is None


def test_parse_current_correction_extracts_id_timestamp_peq_count(
    tmp_path: Path,
):
    """A correction file's filename encodes session_id + epoch, and
    we count `peq_N:` keys in the YAML to surface the filter count
    without needing a YAML parser dependency."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "correction_abc123_1700000000.yml"
    yaml_path.write_text(
        "filters:\n"
        "  flat:\n"
        "    type: Gain\n"
        "  peq_1:\n"
        "    type: Biquad\n"
        "  peq_2:\n"
        "    type: Biquad\n"
        "  peq_3:\n"
        "    type: Biquad\n"
    )
    cc = parse_current_correction(str(yaml_path), config_dir=cfg_dir)
    assert cc is not None
    assert cc["path"] == str(yaml_path)
    assert cc["session_id"] == "abc123"
    assert cc["applied_at_epoch"] == 1700000000
    assert cc["peq_count"] == 3


def test_parse_current_correction_counts_room_peqs_in_sound_config(tmp_path: Path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()

    for filename in ("sound_current.yml", "sound_audition.yml"):
        yaml_path = cfg_dir / filename
        yaml_path.write_text(
            "filters:\n"
            "  flat:\n"
            "    type: Gain\n"
            "  room_peq_1:\n"
            "    type: Biquad\n"
        )

        cc = parse_current_correction(str(yaml_path), config_dir=cfg_dir)

        assert cc is not None
        assert cc["session_id"] == "sound"
        assert cc["peq_count"] == 1


def test_parse_current_correction_ignores_sound_config_without_room_peqs(
    tmp_path: Path,
):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "sound_current.yml"
    yaml_path.write_text("filters:\n  sound_simple_bass:\n    type: Biquad\n")

    assert parse_current_correction(str(yaml_path), config_dir=cfg_dir) is None


def test_describe_current_config_active_content_beats_sound_filename(
    tmp_path: Path,
):
    from tests.test_active_speaker_runtime_contract import _active_baseline_yaml

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "sound_current.yml"
    yaml_path.write_text(_active_baseline_yaml("mono", 2), encoding="utf-8")

    descriptor = describe_current_config(str(yaml_path), config_dir=cfg_dir)

    assert descriptor["kind"] == "active_speaker"
    assert descriptor["managed"] is True
    assert descriptor["current_correction"] is None
    assert "Active-speaker DSP" in descriptor["message"]


def test_parse_current_correction_detects_room_peqs_in_active_content(
    tmp_path: Path,
):
    from jasper.camilla_config_contract import PeqFilter
    from tests.test_active_speaker_runtime_contract import _active_baseline_yaml

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "correction_abc123_1700000000.yml"
    yaml_path.write_text(
        _active_baseline_yaml(
            "mono",
            2,
            room_peqs=(PeqFilter(freq=80.0, q=4.0, gain=-3.0),),
        ),
        encoding="utf-8",
    )

    cc = parse_current_correction(str(yaml_path), config_dir=cfg_dir)

    assert cc is not None
    assert cc["session_id"] == "abc123"
    assert cc["peq_count"] == 1


def test_describe_current_config_measurement_baseline_is_managed(
    tmp_path: Path,
):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    measurement = cfg_dir / "correction_measurement_abc123_1700000000.yml"
    measurement.write_text("filters:\n  flat:\n    type: Gain\n", encoding="utf-8")

    descriptor = describe_current_config(str(measurement), config_dir=cfg_dir)

    assert descriptor["kind"] == "measurement_baseline"
    assert descriptor["managed"] is True
    assert descriptor["current_correction"] is None


def test_parse_current_correction_unknown_filename_returns_none(
    tmp_path: Path,
):
    """A YAML the user hand-edited (or a future filename scheme we
    don't recognise) shouldn't surface as a JTS-managed correction.
    The richer descriptor should carry that truth for UI surfaces."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "hand_edited.yml").write_text("filters: {}\n")
    assert parse_current_correction(
        str(cfg_dir / "hand_edited.yml"), config_dir=cfg_dir,
    ) is None
    # And a correction-shaped name in the WRONG directory shouldn't
    # match either — we only trust paths inside config_dir.
    rogue = tmp_path / "correction_xx_1700000000.yml"
    rogue.write_text("filters: {}\n")
    assert parse_current_correction(str(rogue), config_dir=cfg_dir) is None


def test_describe_current_config_flags_custom_config(tmp_path: Path):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    custom = cfg_dir / "hand_edited.yml"
    custom.write_text("filters: {}\n")

    descriptor = describe_current_config(str(custom), config_dir=cfg_dir)

    assert descriptor["kind"] == "custom"
    assert descriptor["managed"] is False
    assert descriptor["current_correction"] is None
    assert "cannot safely preserve" in descriptor["message"]


def test_describe_current_config_distinguishes_sound_preference(
    tmp_path: Path,
):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    sound = cfg_dir / "sound_current.yml"
    sound.write_text("filters:\n  sound_simple_bass:\n    type: Biquad\n")

    descriptor = describe_current_config(str(sound), config_dir=cfg_dir)

    assert descriptor["kind"] == "sound_preference"
    assert descriptor["managed"] is True
    assert descriptor["current_correction"] is None


def test_describe_current_config_does_not_overclaim_missing_sound_config(
    tmp_path: Path,
):
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    missing = cfg_dir / "sound_current.yml"

    descriptor = describe_current_config(str(missing), config_dir=cfg_dir)

    assert descriptor["kind"] == "unknown"
    assert descriptor["managed"] is False
    assert descriptor["current_correction"] is None


# ---------- Per-session bundle artifacts -----------------------------------


# ---------- Status / bundle payload serialization --------------------------


def test_status_serializers_pin_snapshot_info_and_result_shapes(
    tmp_path: Path,
):
    """The extracted serializer owns payload shape; the session/artifact
    wrappers should keep exposing the same status and bundle dictionaries."""
    from jasper.correction.session import CurveJSON, PEQJSON
    from jasper.audio_measurement.sweep import SweepMeta

    sess = _make_session(
        tmp_path,
        input_device={
            "label": "USB measurement mic",
            "device_id_hash": "abc123",
            "sample_rate": 48000,
        },
        repeat_main_position=True,
    )
    sess.state = SessionState.READY
    sess.error = "kept for serializer test"
    sess.noise_floor_db = -58.0
    sess.capture_quality = [{"capture_kind": "measurement", "position_index": 0}]
    sess.noise_reports = [{"capture_kind": "noise", "position_index": 0}]
    sess.repeat_quality = {"capture_kind": "repeat", "level": "ok"}
    sess.repeatability_report = {"available": True, "level": "high"}
    sess.verify_quality = {"capture_kind": "verify", "level": "ok"}
    sess.confidence_report = {"level": "medium", "score": 72}
    sess.acoustic_quality = {"summary": {"level": "ok", "snr_level": "high"}}
    sess.position_analysis = {"artifact_path": "position_analysis.json"}
    sess.current_correction_at_start = {"kind": "correction"}
    sess.sweep_meta = SweepMeta(
        f1=20.0,
        f2=20000.0,
        L=0.5,
        duration_s=1.0,
        n_samples=48000,
        sample_rate=48000,
        amplitude_dbfs=-12.0,
    )
    sess.peqs = [PEQJSON(freq_hz=80.0, q=4.0, gain_db=-3.0)]
    sess.design_report = {"correction_strategy": {"strategy_id": "balanced"}}
    sess.config_path = tmp_path / "configs" / "correction_abc_1700000000.yml"
    sess.verify_metrics = {"max_abs_db": 1.25}
    sess.measured_curve = CurveJSON([20.0, 80.0], [1.0, 6.0])
    sess.target_curve = CurveJSON([20.0, 80.0], [0.0, 0.0])
    sess.predicted_curve = CurveJSON([20.0, 80.0], [0.5, 1.0])
    sess.verify_curve = CurveJSON([20.0, 80.0], [0.25, 1.2])
    sess.repeat_curve = CurveJSON([20.0, 80.0], [1.1, 5.8])

    snapshot = correction_status.session_snapshot(sess)
    assert sess.snapshot() == snapshot
    assert set(snapshot) == {
        "session_id",
        "state",
        "started_at",
        "updated_at",
        "error",
        "total_positions",
        "current_position",
        "repeat_main_position",
        "target_choice",
        "target_profile",
        "strategy_choice",
        "correction_strategy",
        "input_device",
        "mic_calibration",
        "browser_audio_report",
        "capture_quality",
        "noise_reports",
        "repeat_quality",
        "repeatability_report",
        "verify_quality",
        "confidence_report",
        "acoustic_quality",
        "runtime_integrity",
        "position_analysis",
        "sweep",
        "peqs",
        "design_report",
        "config_path",
        "measurement_config_path",
        "pre_measurement_config_path",
        "verify_metrics",
        "verify_before_after",
        "acceptance",
        "auto_revert_outcome",
        "autolevel",
            "capture_transport",
            "local_capture_setup_bound",
            "level_match",
    }
    assert snapshot["sweep"] == sess.sweep_meta.to_dict()
    assert snapshot["peqs"] == [{"freq_hz": 80.0, "q": 4.0, "gain_db": -3.0}]
    assert snapshot["config_path"] == str(sess.config_path)

    info = correction_status.info_json_payload(sess)
    assert set(info) == {
        "bundle_schema_version",
        "session_id",
        "state",
        "started_at",
        "updated_at",
        "error",
        "total_positions",
        "current_position",
        "repeat_main_position",
        "target_choice",
        "target_profile",
        "strategy_choice",
        "correction_strategy",
        "noise_floor_db",
        "input_device",
        "mic_calibration",
        "browser_audio_report",
        "capture_quality",
        "noise_reports",
        "repeat_quality",
        "repeatability_report",
        "verify_quality",
        "confidence_report",
        "acoustic_quality",
        "runtime_integrity",
        "position_analysis",
        "current_correction_at_start",
        "autolevel",
        "capture_transport",
        "level_match",
        "sweep_meta",
        "peqs",
        "design_report",
        "config_path",
        "measurement_config_path",
        "pre_measurement_config_path",
        "verify_metrics",
        "verify_before_after",
        "acceptance",
        "auto_revert_outcome",
        "config",
    }
    assert info["bundle_schema_version"] == bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    assert info["sweep_meta"] == snapshot["sweep"]
    assert info["current_correction_at_start"] == {"kind": "correction"}
    assert info["config"]["sample_rate"] == 48000

    result = correction_status.result_json_payload(sess)
    assert set(result) == {
        "bundle_schema_version",
        "session_id",
        "input_device",
        "mic_calibration",
        "browser_audio_report",
        "measured",
        "target",
        "predicted",
        "position1",
        "verify",
        "verify_metrics",
        "verify_before_after",
        "acceptance",
        "auto_revert_outcome",
        "capture_quality",
        "noise_reports",
        "repeat",
        "repeat_quality",
        "repeatability_report",
        "verify_quality",
        "confidence_report",
        "acoustic_quality",
        "runtime_integrity",
        "position_analysis",
        "peqs",
        "design_report",
    }
    assert result["measured"] == {
        "freqs_hz": [20.0, 80.0],
        "magnitude_db": [1.0, 6.0],
    }
    assert result["repeat"] == {
        "freqs_hz": [20.0, 80.0],
        "magnitude_db": [1.1, 5.8],
    }
    assert result["verify_metrics"] == {"max_abs_db": 1.25}


def test_bundle_info_json_written_on_state_transition(tmp_path: Path):
    """info.json appears at the bundle root once the session
    transitions out of IDLE. The first PREPARING transition is the
    earliest it should land."""
    sess = _make_session(
        tmp_path,
        input_device={
            "label": "USB measurement mic",
            "device_id_hash": "abc123",
        },
    )
    # Trigger a state transition (uses the internal helper directly —
    # the public flow does this via prepare_and_play_sweep which
    # we test elsewhere).
    import asyncio
    asyncio.run(sess._set_state(SessionState.PREPARING))

    info_path = sess.bundle_dir / "info.json"
    assert info_path.exists()
    data = json.loads(info_path.read_text())
    assert data["session_id"] == sess.session_id
    assert data["bundle_schema_version"] == bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    assert data["state"] == "preparing"
    assert data["target_choice"] == "flat"
    assert data["strategy_choice"] == "balanced"
    assert data["correction_strategy"]["strategy_id"] == "balanced"
    assert data["target_profile"]["target_id"] == "flat"
    assert data["input_device"]["label"] == "USB measurement mic"
    assert "config" in data
    assert data["config"]["sample_rate"] == 48000
    manifest = bundles.read_artifact_manifest(sess.bundle_dir)
    assert any(
        artifact["path"] == "info.json"
        and artifact["schema_version"] == bundles.CURRENT_BUNDLE_SCHEMA_VERSION
        for artifact in manifest["artifacts"]
    )


def test_bundle_disabled_via_env_var(tmp_path: Path, monkeypatch):
    """Opt-out path: JASPER_CORRECTION_SAVE_BUNDLES=0 disables the
    bundle directory entirely. Captures fall back to the flat
    capture_dir, info.json never writes."""
    monkeypatch.setenv("JASPER_CORRECTION_SAVE_BUNDLES", "0")
    sess = _make_session(tmp_path)
    import asyncio
    asyncio.run(sess._set_state(SessionState.PREPARING))
    assert not sess.bundle_dir.exists()
    # Capture path falls through to the flat dir.
    path = sess.capture_path_for_position(0)
    assert sess.cfg.capture_dir in path.parents


def test_capture_path_for_position_uses_per_session_dir(tmp_path: Path):
    """Per-position WAVs land at sessions/<id>/captures/p<N>.wav, not
    the legacy flat captures/ dir. Verifies the path itself and that
    writing a body lands the file there."""
    sess = _make_session(tmp_path)
    p0 = sess.capture_path_for_position(0)
    assert p0 == sess.bundle_dir / "captures" / "p0.wav"
    p1 = sess.capture_path_for_position(1)
    assert p1 == sess.bundle_dir / "captures" / "p1.wav"
    # Parent dir is created lazily by _ensure_bundle_dir.
    assert p0.parent.exists()
    p0.parent.mkdir(parents=True, exist_ok=True)
    p0.write_bytes(b"riff stub")
    assert p0.read_bytes() == b"riff stub"
    # And verify capture lands at the bundle root.
    assert sess.verify_capture_path() == sess.bundle_dir / "verify.wav"


@pytest.mark.asyncio
async def test_apply_copies_yaml_into_bundle(tmp_path: Path, monkeypatch):
    """apply() writes the correction YAML to /var/lib/camilladsp/configs
    and copies it into the bundle as applied.yml — so the bundle is
    self-contained even if the user later deletes the configs file."""
    sess = _make_session(tmp_path)
    # Drive the session straight to READY without going through the
    # full capture flow; apply() only needs peqs + READY state.
    from jasper.correction.session import PEQJSON
    sess.state = SessionState.READY
    sess.peqs = [
        PEQJSON(freq_hz=80.0, q=4.0, gain_db=-3.0),
        PEQJSON(freq_hz=160.0, q=4.0, gain_db=-2.0),
    ]
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing_sound.json"))

    calls: list[str] = []

    async def fake_set_config(path: str) -> bool:
        calls.append(path)
        return True

    await sess.apply(fake_set_config)
    assert sess.state == SessionState.APPLIED
    assert sess.config_path is not None
    assert sess.config_path.exists()
    # applied.yml is a COPY (not symlink) of config_path.
    bundle_yaml = sess.bundle_dir / "applied.yml"
    assert bundle_yaml.exists()
    assert not bundle_yaml.is_symlink()
    assert bundle_yaml.read_text() == sess.config_path.read_text()
    manifest = bundles.read_artifact_manifest(sess.bundle_dir)
    assert any(
        artifact["path"] == "applied.yml"
        and artifact["kind"] == "camilladsp_config"
        for artifact in manifest["artifacts"]
    )


@pytest.mark.asyncio
async def test_correction_apply_preserves_saved_sound_profile(
    tmp_path: Path,
    monkeypatch,
):
    sess = _make_session(tmp_path)
    sess.state = SessionState.READY
    from jasper.correction.session import PEQJSON
    sess.peqs = [PEQJSON(freq_hz=80.0, q=4.0, gain_db=-3.0)]
    profile_path = tmp_path / "sound_profile.json"
    save_profile(
        SoundProfile(curve_id="harman", simple_eq=SimpleEq(treble_db=1.5)),
        profile_path,
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile_path))

    async def fake_set_config(path: str) -> bool:
        return True

    await sess.apply(fake_set_config)

    assert sess.config_path is not None
    yaml = sess.config_path.read_text()
    assert "room_peq_1:" in yaml
    assert "sound_curve_harman_bass:" in yaml
    assert "sound_simple_treble:" in yaml


@pytest.mark.asyncio
async def test_correction_apply_replaces_existing_room_peqs(
    tmp_path: Path,
    monkeypatch,
):
    sess = _make_session(tmp_path)
    sess.state = SessionState.READY
    from jasper.correction.session import PEQJSON
    sess.peqs = [PEQJSON(freq_hz=80.0, q=4.0, gain_db=-3.0)]
    sess.cfg.config_dir.mkdir()
    current = sess.cfg.config_dir / "correction_old_1700000000.yml"
    current.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=45.0, q=3.0, gain=-6.0)],
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    loaded = {"path": str(current)}

    async def fake_set_config(path: str) -> bool:
        loaded["path"] = path
        return True

    async def fake_get_config() -> str:
        return loaded["path"]

    await sess.apply(fake_set_config, camilla_get_config=fake_get_config)

    assert sess.config_path is not None
    yaml = sess.config_path.read_text(encoding="utf-8")
    assert "freq: 80.0000" in yaml
    assert "freq: 45.0000" not in yaml


@pytest.mark.asyncio
async def test_correction_apply_runs_authority_guard_inside_dsp_lock(
    tmp_path: Path,
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from jasper import dsp_apply
    from jasper.correction.session import PEQJSON

    sess = _make_session(tmp_path)
    sess.state = SessionState.READY
    sess.peqs = [PEQJSON(freq_hz=80.0, q=4.0, gain_db=-3.0)]
    sess.cfg.config_dir.mkdir()
    current = sess.cfg.config_dir / "sound_current.yml"
    current.write_text(
        emit_sound_config(SoundProfile(enabled=False)),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    lock_held = False
    guard_observations = []

    @asynccontextmanager
    async def observed_lock(*_args, **_kwargs):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    async def prepare_guard():
        guard_observations.append(lock_held)

    loaded = {"path": str(current)}

    async def fake_set_config(path: str) -> bool:
        loaded["path"] = path
        return True

    async def fake_get_config() -> str:
        return loaded["path"]

    monkeypatch.setattr(dsp_apply, "_maybe_dsp_apply_lock", observed_lock)

    await sess.apply(
        fake_set_config,
        camilla_get_config=fake_get_config,
        prepare_guard=prepare_guard,
    )

    assert guard_observations == [True]
    assert lock_held is False


@pytest.mark.asyncio
async def test_reset_no_room_config_preserves_preference_and_strips_room(
    tmp_path: Path,
    monkeypatch,
):
    from jasper.web import correction_setup

    sess = _make_session(tmp_path)
    sess.cfg.config_dir.mkdir()
    current = sess.cfg.config_dir / "correction_old_1700000000.yml"
    current.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=45.0, q=3.0, gain=-6.0)],
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "sound_profile.json"
    save_profile(
        SoundProfile(curve_id="harman", simple_eq=SimpleEq(treble_db=1.5)),
        profile_path,
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile_path))
    safety_checks: list[str] = []
    monkeypatch.setattr(
        "jasper.correction.runtime_safety.assert_correction_graph_safe",
        lambda text, **_kwargs: safety_checks.append(text),
    )
    fake_cam = _FakeCamilla(current_path=str(current))

    out_path = await correction_setup._write_no_room_correction_config(sess, fake_cam)

    yaml = out_path.read_text(encoding="utf-8")
    assert len(safety_checks) == 2
    assert safety_checks[-1] == yaml
    assert out_path.name.startswith(f"sound_reset_{sess.session_id}_")
    assert out_path.name.endswith(".yml")
    assert current.read_text(encoding="utf-8") != yaml
    assert "room_peq_1:" not in yaml
    assert "sound_curve_harman_bass:" in yaml
    assert "sound_simple_treble:" in yaml


@pytest.mark.asyncio
async def test_design_writes_result_json(tmp_path: Path, monkeypatch):
    """After spatial average + PEQ design, result.json captures the
    measured / target / predicted curves so a copied-off bundle is
    re-renderable without re-running the deconvolution."""
    import numpy as np
    from jasper.audio_measurement import sweep
    from jasper.correction import runtime_integrity

    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)

    sess = _make_session(tmp_path)
    sess.input_device = {
        "label": "USB measurement mic",
        "device_id_hash": "abc123",
        "sample_rate": 48000,
        "channel_count": 1,
        "echo_cancellation": False,
        "noise_suppression": False,
        "auto_gain_control": False,
    }
    from jasper.correction import browser_audio
    sess.browser_audio_report = browser_audio.assess_browser_audio_path(
        input_device=sess.input_device,
        expected_sample_rate=sess.cfg.sample_rate,
        has_mic_calibration=sess.mic_calibration is not None,
    ).to_dict()
    sess.total_positions = 1

    captured_paths: list[str] = []

    async def fake_play_sweep(path, **kwargs):
        captured_paths.append(path)

    await sess.prepare_and_play_sweep(fake_play_sweep)
    assert sess.state == SessionState.AWAITING_CAPTURE

    sweep_signal, sr = sweep.read_wav_mono(sess.sweep_wav_path)
    # Trivial "no room" capture — sweep convolved with a delta.
    captured = sweep_signal.astype(np.float32)
    cap_path = sess.capture_path_for_position(0)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_sweep_wav(cap_path, captured, sr)

    await sess.on_capture_uploaded(cap_path)
    assert sess.state == SessionState.READY

    result_path = sess.bundle_dir / "result.json"
    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["session_id"] == sess.session_id
    assert result["bundle_schema_version"] == (
        bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    )
    assert result["input_device"]["device_id_hash"] == "abc123"
    assert result["browser_audio_report"]["level"] == "warn"
    assert (
        result["confidence_report"]["browser_audio_report"]
        == result["browser_audio_report"]
    )
    assert result["runtime_integrity"]["level"] == "ok"
    assert result["acoustic_quality"]["level"] in {"ok", "warn"}
    assert result["acoustic_quality"]["snr_level"] in {
        "high",
        "medium",
        "low",
        "unavailable",
    }
    acoustic_path = sess.bundle_dir / "acoustic_quality.json"
    assert acoustic_path.exists()
    acoustic = json.loads(acoustic_path.read_text())
    assert acoustic["session_id"] == sess.session_id
    assert acoustic["artifact_schema_version"] == 1
    assert result["confidence_report"]["runtime_integrity"]["level"] == "ok"
    assert result["measured"] is not None
    assert "freqs_hz" in result["measured"]
    assert "magnitude_db" in result["measured"]
    assert result["target"] is not None
    assert result["predicted"] is not None
    assert result["design_report"]["correction_strategy"]["strategy_id"] == (
        "balanced"
    )
    assert result["design_report"]["target_profile"]["target_id"] == "flat"
    assert result["confidence_report"]["level"] in {"medium", "low"}
    assert result["confidence_report"]["strategy_gates"]["safe"]["allowed"] is True
    assert (
        result["design_report"]["confidence_report"]
        == result["confidence_report"]
    )
    assert result["position_analysis"]["artifact_path"] == "position_analysis.json"
    position_analysis_path = sess.bundle_dir / "position_analysis.json"
    assert position_analysis_path.exists()
    position_analysis = json.loads(position_analysis_path.read_text())
    assert position_analysis["session_id"] == sess.session_id
    assert position_analysis["artifact_schema_version"] == 1
    assert len(position_analysis["positions"]) == 1
    assert len(position_analysis["positions"][0]["magnitude_db"]) == len(
        position_analysis["freqs_hz"],
    )
    assert "std_db" in position_analysis["variance"]
    assert "range_db" in position_analysis["variance"]
    chart = result["position_analysis"]["chart"]
    assert set(chart) >= {"freqs_hz", "min_db", "max_db", "std_db", "range_db"}
    assert len(chart["freqs_hz"]) == len(position_analysis["freqs_hz"])
    assert len(chart["min_db"]) == len(chart["freqs_hz"])
    assert "bands" in position_analysis
    assert any(
        band["band_id"] == "correction_band"
        for band in position_analysis["bands"]
    )
    assert "feature_flags" in position_analysis
    assert result["position_analysis"]["bands"] == position_analysis["bands"]
    assert result["design_report"]["position_report"]["artifact_path"] == (
        "position_analysis.json"
    )
    manifest = bundles.read_artifact_manifest(sess.bundle_dir)
    manifest_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    runtime_artifact = next(
        artifact for artifact in manifest["artifacts"]
        if artifact["path"] == "runtime_integrity.json"
    )
    assert {
        "info.json",
        "captures/p0.wav",
        "runtime_integrity.json",
        "position_analysis.json",
        "result.json",
    }.issubset(manifest_paths)
    assert "captures/p0.wav" in runtime_artifact["dependencies"]
    assert not any(
        issue.severity == "fail"
        for issue in bundles.validate_bundle(sess.bundle_dir)
    )


# ---------- /start measurement baseline + /sessions endpoint ---------------


class _FakeCamilla:
    """Records calls to set_config_file_path so /start ordering is assertable."""
    def __init__(self, current_path: str, *, reset_ok: bool = True) -> None:
        self.current_path = current_path
        self.reset_ok = reset_ok
        self.set_calls: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False):
        return self.current_path

    async def get_active_config_raw(self, *, best_effort: bool = False):
        return Path(self.current_path).read_text(encoding="utf-8")

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        self.set_calls.append(path)
        if not self.reset_ok:
            return False
        self.current_path = path
        return True


def _stub_replace_to_tmp(correction_setup, tmp_path: Path, captured: dict):
    from jasper.correction.session import SessionConfig

    real_replace = correction_setup._replace_session

    def stub_replace(
        *,
        total_positions: int,
        target_choice: str,
        strategy_choice: str | None = None,
        mic_calibration=None,
        input_device=None,
        repeat_main_position: bool = False,
    ):
        sess = real_replace(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        sess.cfg = SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=tmp_path / "sessions",
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
            duration_s=1.0,
        )
        sess.cfg.base_config_path.write_text("# stub\n")
        sess.cfg.config_dir.mkdir(parents=True, exist_ok=True)
        # Recompute bundle_dir using the new cfg.
        sess.bundle_dir = sess.cfg.sessions_dir / sess.session_id
        captured["sess"] = sess
        return sess

    return stub_replace


class _DummyJsonHandler:
    def __init__(self, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode()
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


_READY_ROOM_CORRECTION_SETUP = {
    "active": False,
    "room_correction_allowed": True,
    "acoustic_commissioning": {
        "decision_schema_version": 1,
        "authority": "passive_not_required",
        "allowed": True,
        "status": "not_required",
    },
}


def test_room_readiness_producer_binds_fresh_camilla_active_raw(monkeypatch):
    from jasper.active_speaker import setup_status
    from jasper.web import correction_setup

    captured = {}

    class FakeCamilla:
        async def get_active_config_raw(self, *, best_effort=False):
            assert best_effort is False
            return "pipeline: [{type: Mixer, name: split}]\n"

    def fake_status(**kwargs):
        captured.update(kwargs)
        return _READY_ROOM_CORRECTION_SETUP

    monkeypatch.setattr(correction_setup, "_camilla", lambda: FakeCamilla())
    monkeypatch.setattr(
        correction_setup,
        "_run_async",
        lambda awaitable, *, timeout: asyncio.run(awaitable),
    )
    monkeypatch.setattr(setup_status, "read_active_speaker_setup_status", fake_status)

    result = correction_setup._room_correction_readiness()

    assert result is _READY_ROOM_CORRECTION_SETUP
    assert captured == {
        "active_config_text": "pipeline: [{type: Mixer, name: split}]\n",
    }


def test_room_readiness_accepts_consistent_passive_authority(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is True
    assert readiness.blocker is None


def test_room_readiness_rejects_unversioned_active_snapshot_authority(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "allowed": True,
                "status": "ready",
                "reason": None,
                "detail": "historical B2b evidence says ready",
                "setup_href": "/correction/crossover/",
            },
        },
    )
    monkeypatch.setattr(
        correction_setup,
        "_reserve_start_slot",
        lambda: pytest.fail("legacy Active authority must reject before reserve"),
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is False
    assert readiness.reason == "speaker_readiness_malformed"
    assert readiness.blocker == {
        "code": "speaker_readiness_unavailable",
        "text": "Speaker setup could not be checked. Try again.",
        "retryable": True,
        "recovery_action": {
            "label": "Check again",
            "href": "/correction/room/",
        },
    }
    assert "historical B2b" not in str(readiness.blocker)

    with pytest.raises(correction_setup.RoomRequestFailure) as exc_info:
        correction_setup._handle_start(_DummyJsonHandler())
    assert exc_info.value.status == HTTPStatus.SERVICE_UNAVAILABLE


def test_room_readiness_accepts_versioned_manual_active_authority(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": "manual_applied_profile",
                "layer_a_identity": "layer-a-manual",
                "allowed": True,
                "status": "ready",
                "setup_href": "/correction/crossover/",
            },
        },
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is True
    assert readiness.blocker is None
    assert readiness.authority_binding == (
        True,
        "manual_applied_profile",
        "layer-a-manual",
    )


def test_room_readiness_consumes_active_owned_grouped_scope(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "layer_a_identity": None,
                "allowed": False,
                "status": "incomplete",
                "reason": "active_grouped_room_correction_not_supported",
                "detail": "Active owns this unsupported scope decision.",
                "setup_href": "/rooms/",
            },
        },
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is False
    assert readiness.reason == "active_grouped_room_correction_not_supported"
    assert readiness.blocker["code"] == "speaker_setup_incomplete"
    assert readiness.blocker["recovery_action"] == {
        "label": "Open speaker setup",
        "href": "/rooms/",
    }
    assert "unsupported scope" not in str(readiness.blocker)


def test_room_readiness_accepts_only_explicit_automatic_receipt_authority(
    monkeypatch,
):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": "automatic_commissioning_receipt",
                "layer_a_identity": "layer-a-automatic",
                "allowed": True,
                "status": "ready",
                "setup_href": "/correction/crossover/",
            },
        },
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is True
    assert readiness.blocker is None


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"room_correction_allowed": True},
        {
            "room_correction_allowed": "yes",
            "acoustic_commissioning": {
                "allowed": True,
                "status": "ready",
            },
        },
        {
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "allowed": False,
                "status": "incomplete",
            },
        },
        {
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "allowed": True,
                "status": "incomplete",
            },
        },
    ],
)
def test_room_readiness_malformed_shapes_fail_closed_with_retry(
    monkeypatch,
    raw,
):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: raw,
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is False
    assert readiness.blocker["code"] == "speaker_readiness_unavailable"
    assert readiness.blocker["recovery_action"] == {
        "label": "Check again",
        "href": "/correction/room/",
    }


def test_room_readiness_unknown_authority_is_retryable_unavailable(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_setup_status",
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "required": True,
                "allowed": False,
                "status": "unknown",
                "reason": "output_topology_unreadable",
                "detail": "raw topology diagnostic",
                "setup_href": "/correction/crossover/",
            },
        },
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is False
    assert readiness.blocker["code"] == "speaker_readiness_unavailable"
    assert readiness.blocker["retryable"] is True
    assert readiness.blocker["recovery_action"] == {
        "label": "Open speaker setup",
        "href": "/correction/crossover/",
    }
    assert "raw topology diagnostic" not in str(readiness.blocker)

    with pytest.raises(correction_setup.RoomRequestFailure) as exc_info:
        correction_setup._handle_start(_DummyJsonHandler())
    assert exc_info.value.status == HTTPStatus.SERVICE_UNAVAILABLE


def test_room_readiness_read_failure_has_bounded_retry(monkeypatch):
    from jasper.web import correction_setup

    def unreadable():
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(correction_setup, "_room_correction_readiness", unreadable)

    readiness = correction_setup._room_readiness()

    assert readiness.blocker["code"] == "speaker_readiness_unavailable"
    assert readiness.blocker["recovery_action"] == {
        "label": "Check again",
        "href": "/correction/room/",
    }
    assert "secret filesystem detail" not in str(readiness.blocker)


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/setup",
        "//example.com/setup",
        "/correction/\\evil",
        "/correction/crossover/\nnext",
    ],
)
def test_room_readiness_rejects_unsafe_owner_recovery_links(monkeypatch, href):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "allowed": False,
                "status": "incomplete",
                "reason": "active_speaker_setup_not_ready",
                "detail": "raw Active detail",
                "setup_href": href,
            },
        },
    )

    readiness = correction_setup._room_readiness()

    assert readiness.allowed is False
    assert readiness.blocker["code"] == "speaker_setup_incomplete"
    assert readiness.blocker["recovery_action"] == {
        "label": "Check again",
        "href": "/correction/room/",
    }
    assert "raw Active detail" not in str(readiness.blocker)


def test_start_handler_loads_measurement_baseline_before_sweep(
    tmp_path: Path, monkeypatch,
):
    """Pin the load-bearing behavior: /start loads a generated baseline
    with room/preference filters stripped BEFORE it kicks off measurement.
    Without this, a sweep run on top of an existing correction would design
    new filters from the already-corrected curve and compound distortion.
    """
    from jasper.web import correction_setup
    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    authority_checks = []

    async def authority_current(_cam, expected):
        authority_checks.append(expected)

    monkeypatch.setattr(
        correction_setup,
        "_assert_room_authority_current",
        authority_current,
    )
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setattr(correction_setup, "_session", None)
    prior_path = tmp_path / "configs" / "correction_xyz_1700.yml"
    prior_path.parent.mkdir()
    prior_path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=45.0, q=3.0, gain=-6.0)],
        ),
        encoding="utf-8",
    )
    fake_cam = _FakeCamilla(current_path=str(prior_path))
    monkeypatch.setattr(correction_setup, "_camilla", lambda: fake_cam)

    # Hold the sweep entirely — we just want to observe the reset
    # call ordering. The first-sweep task fires-and-forgets onto the
    # background loop, so the reset visible in `set_calls` after
    # /start returns is the synchronous one.
    async def fake_play_sweep(path, **kwargs):
        return None
    monkeypatch.setattr(
        "jasper.correction.playback.play_sweep", fake_play_sweep,
    )
    # And the coordinator window — we don't want systemctl calls in
    # the test, just a no-op context manager.
    import contextlib

    @contextlib.asynccontextmanager
    async def noop_window():
        yield

    monkeypatch.setattr(
        "jasper.correction.coordinator.measurement_window", noop_window,
    )

    # Point the new session at tmp_path so we don't write to /var.
    captured: dict = {}
    monkeypatch.setattr(
        correction_setup,
        "_replace_session",
        _stub_replace_to_tmp(correction_setup, tmp_path, captured),
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = json_post_with_csrf(
            f"http://127.0.0.1:{port}",
            "/start",
            {},
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    sess = captured["sess"]
    # The /start handler should have called set_config_file_path exactly once
    # before kicking off the sweep, with the generated measurement baseline.
    assert len(fake_cam.set_calls) == 1
    assert Path(fake_cam.set_calls[0]).name.startswith("correction_measurement_")
    assert Path(fake_cam.set_calls[0]).exists()
    generated = Path(fake_cam.set_calls[0]).read_text(encoding="utf-8")
    assert "room_peq_1" not in generated
    assert "sound_curve_" not in generated
    # The descriptor is derived from immutable graph content rather than the
    # mutable predecessor filename.
    prior = body["current_correction_at_start"]
    assert prior is not None
    assert prior["kind"] == "sound_with_correction"
    assert prior["current_correction"]["session_id"] == "sound"
    assert body["strategy_choice"] == "balanced"
    assert body["correction_strategy"]["strategy_id"] == "balanced"
    assert sess.total_positions == 6
    assert sess.target_choice == "flat"
    assert sess.repeat_main_position is True
    assert body["measurement_config_path"] == fake_cam.set_calls[0]
    assert sess.pre_measurement_config_path == Path(
        tmp_path / "configs" / "correction_xyz_1700.yml"
    )
    assert sess.pre_measurement_restore_path is not None
    assert sess.pre_measurement_restore_path != sess.pre_measurement_config_path
    assert sess.pre_measurement_restore_path.exists()
    # Local browser permission/device selection is human-paced. /start must
    # suspend the automatic upload watchdog until setup is bound; otherwise a
    # household can time out while still responding to the permission prompt.
    assert sess._state_guard._capture_timeout_task is None
    assert authority_checks == [(False, "passive_not_required", None)]


@pytest.mark.asyncio
async def test_measurement_baseline_snapshots_locked_prior_config(
    tmp_path: Path,
    monkeypatch,
):
    """The bundle descriptor must name the graph replaced under the DSP lock.

    If another JTS writer swaps CamillaDSP after the first best-effort read but
    before the apply transaction prepares the candidate, the measurement graph
    is derived from the locked anchor. The saved prior descriptor should match
    that same anchor, not the stale pre-lock path.
    """
    from jasper.web import correction_setup

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setattr(
        "jasper.correction.runtime_safety.assert_correction_graph_safe",
        lambda text, **_kwargs: None,
    )
    async def authority_current(_cam, _expected):
        return None

    monkeypatch.setattr(
        correction_setup,
        "_assert_room_authority_current",
        authority_current,
    )
    sess = _make_session(tmp_path)
    sess.cfg.config_dir.mkdir()
    old_path = sess.cfg.config_dir / "correction_old_1700000000.yml"
    old_path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=45.0, q=3.0, gain=-6.0)],
        ),
        encoding="utf-8",
    )
    new_path = sess.cfg.config_dir / "correction_new_1700000001.yml"
    new_path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        ),
        encoding="utf-8",
    )

    class SwappingCamilla:
        def __init__(self) -> None:
            self.current_path = str(old_path)
            self.get_calls = 0
            self.set_calls: list[str] = []

        async def get_config_file_path(self, *, best_effort: bool = False):
            self.get_calls += 1
            if self.get_calls == 1:
                self.current_path = str(new_path)
                return str(new_path)
            return self.current_path

        async def set_config_file_path(
            self, path: str, *, best_effort: bool = False,
        ) -> bool:
            self.set_calls.append(path)
            self.current_path = path
            return True

        async def get_active_config_raw(self, *, best_effort: bool = False):
            return Path(self.current_path).read_text(encoding="utf-8")

    payload = await correction_setup._load_measurement_baseline(
        sess,
        SwappingCamilla(),
        expected_authority_binding=(False, "passive_not_required", None),
    )

    assert payload["prior_config_path"] == str(new_path)
    assert sess.pre_measurement_config_path == new_path
    assert sess.pre_measurement_restore_path is not None
    assert sess.pre_measurement_restore_path != new_path
    assert payload["restore_config_path"] == str(
        sess.pre_measurement_restore_path
    )
    assert payload["current_correction_at_start"]["current_correction"][
        "session_id"
    ] == "sound"


@pytest.mark.asyncio
async def test_measurement_baseline_rejects_layer_a_change_inside_prepare(
    tmp_path: Path,
    monkeypatch,
):
    """The graph admitted before reservation must still be current in prepare."""
    from jasper.dsp_apply import DspApplyError
    from jasper.web import correction_setup

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    sess = _make_session(tmp_path)
    sess.cfg.config_dir.mkdir()
    current = sess.cfg.config_dir / "sound_current.yml"
    current.write_text(
        emit_sound_config(SoundProfile(enabled=False)),
        encoding="utf-8",
    )
    fake_cam = _FakeCamilla(current_path=str(current))

    async def changed_authority(_cam):
        return {
            "active": True,
            "room_correction_allowed": True,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": "manual_applied_profile",
                "layer_a_identity": "layer-a-after-reservation",
                "allowed": True,
                "status": "ready",
                "setup_href": "/correction/crossover/",
            },
        }

    monkeypatch.setattr(
        correction_setup,
        "_read_room_correction_readiness",
        changed_authority,
    )

    with pytest.raises(DspApplyError) as exc_info:
        await correction_setup._load_measurement_baseline(
            sess,
            fake_cam,
            expected_authority_binding=(
                True,
                "manual_applied_profile",
                "layer-a-before-reservation",
            ),
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "authority changed" in str(exc_info.value.__cause__)
    assert fake_cam.set_calls == []


@pytest.mark.asyncio
async def test_measurement_baseline_hosts_program_bake_pipe(
    tmp_path: Path,
    monkeypatch,
):
    """Retain the carrier seam for future distributed Active authority.

    The v1 Active eligibility projection explicitly scopes grouped active out;
    once Active can bind both Camilla daemons, Room's lower-level carrier must
    still host the leader program bake when it resolves to the Snapcast pipe.
    """
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.web import correction_setup
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps(_active_topology("stereo", "active_2_way").to_dict()),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setattr(
        "jasper.multiroom.member_config.member_camilla_kwargs",
        lambda: {
            "enable_rate_adjust": False,
            "channel_split": None,
            "playback_pipe_path": "/run/jasper-snapserver/snapfifo",
        },
    )
    async def authority_current(_cam, _expected):
        return None

    monkeypatch.setattr(
        correction_setup,
        "_assert_room_authority_current",
        authority_current,
    )
    sess = _make_session(tmp_path)
    sess.cfg.config_dir.mkdir()
    current = sess.cfg.config_dir / "sound_current.yml"
    current.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        ),
        encoding="utf-8",
    )
    fake_cam = _FakeCamilla(current_path=str(current))

    payload = await correction_setup._load_measurement_baseline(
        sess,
        fake_cam,
        expected_authority_binding=(True, "manual_applied_profile", "layer-a"),
    )

    assert len(fake_cam.set_calls) == 1
    measurement_path = Path(fake_cam.set_calls[0])
    assert measurement_path.name.startswith("correction_measurement_")
    generated = measurement_path.read_text(encoding="utf-8")
    assert (
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_program_bake_config"
    ) in generated
    assert "/run/jasper-snapserver/snapfifo" in generated
    assert "enable_rate_adjust: false" in generated
    assert "room_peq_" not in generated
    assert "sound_curve_" not in generated
    assert payload["measurement_config_path"] == str(measurement_path)
    assert payload["prior_config_path"] == str(current)
    assert sess.pre_measurement_config_path == current
    assert sess.pre_measurement_restore_path is not None
    assert sess.pre_measurement_restore_path != current


def test_start_handler_aborts_if_measurement_baseline_load_fails(
    tmp_path: Path, monkeypatch,
):
    """If CamillaDSP cannot switch to the measurement baseline, /start must
    fail before playing a sweep."""
    from jasper.web import correction_setup
    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    async def authority_current(_cam, _expected):
        return None

    monkeypatch.setattr(
        correction_setup,
        "_assert_room_authority_current",
        authority_current,
    )
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setattr(correction_setup, "_session", None)

    prior_path = tmp_path / "configs" / "correction_xyz_1700.yml"
    prior_path.parent.mkdir()
    prior_path.write_text(
        emit_sound_config(SoundProfile(enabled=False)),
        encoding="utf-8",
    )
    fake_cam = _FakeCamilla(current_path=str(prior_path), reset_ok=False)
    monkeypatch.setattr(correction_setup, "_camilla", lambda: fake_cam)
    captured: dict = {}
    monkeypatch.setattr(
        correction_setup,
        "_replace_session",
        _stub_replace_to_tmp(correction_setup, tmp_path, captured),
    )
    monkeypatch.setattr(
        correction_setup,
        "_run_async",
        lambda coro, timeout=10.0: asyncio.run(coro),
    )

    scheduled = {"value": False}

    def fake_schedule(*args, **kwargs):
        scheduled["value"] = True
        raise AssertionError("sweep should not be scheduled")

    monkeypatch.setattr(
        correction_setup.asyncio,
        "run_coroutine_threadsafe",
        fake_schedule,
    )

    with pytest.raises(RuntimeError, match="CamillaDSP reload failed"):
        correction_setup._handle_start(_DummyJsonHandler())

    assert fake_cam.set_calls
    assert Path(fake_cam.set_calls[0]).name.startswith("correction_measurement_")
    assert scheduled["value"] is False
    assert correction_setup._start_in_progress is False


def test_start_handler_rejects_active_measurement(monkeypatch):
    """Server-side guard for handcrafted double-start requests.

    The browser disables the Run button while measuring, but the
    backend also needs to refuse a second /start while a sweep/capture
    lifecycle is already active.
    """
    from jasper.web import correction_setup
    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )

    class ActiveSession:
        state = SessionState.SWEEPING

    monkeypatch.setattr(correction_setup, "_session", ActiveSession())

    with pytest.raises(RuntimeError, match="measurement already in progress"):
        correction_setup._handle_start(_DummyJsonHandler())


def test_start_handler_rejects_failed_browser_audio_before_sweep(
    tmp_path: Path,
    monkeypatch,
):
    """A handcrafted /start cannot bypass the browser's disabled Run
    button when getUserMedia reports a capture path that is unsafe for
    measurement."""
    from jasper.web import correction_setup
    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )

    monkeypatch.setattr(correction_setup, "_session", None)
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(
        correction_setup,
        "_camilla",
        lambda: pytest.fail("CamillaDSP should not be touched"),
    )
    captured: dict = {}
    monkeypatch.setattr(
        correction_setup,
        "_replace_session",
        _stub_replace_to_tmp(correction_setup, tmp_path, captured),
    )

    with pytest.raises(ValueError, match="not safe for measurement"):
        correction_setup._handle_start(_DummyJsonHandler({
            "input_device": {
                "label": "iPhone microphone",
                "sample_rate": 44100,
                "channel_count": 1,
                "echo_cancellation": True,
                "noise_suppression": False,
                "auto_gain_control": False,
            },
        }))

    # Validation happens before replacing the prior session or touching DSP.
    assert captured == {}
    assert correction_setup._start_in_progress is False


@pytest.mark.parametrize("strategy_choice", ["assertive", "unknown"])
def test_start_rejects_non_household_strategy_before_dsp(
    monkeypatch,
    strategy_choice,
):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(
        correction_setup,
        "_camilla",
        lambda: pytest.fail("CamillaDSP should not be touched"),
    )

    with pytest.raises(ValueError, match="authorized household strategy"):
        correction_setup._handle_start(
            _DummyJsonHandler({"strategy_choice": strategy_choice})
        )

    assert correction_setup._start_in_progress is False


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"total_positions": 5}, "supported count"),
        ({"total_positions": False}, "supported count"),
        ({"total_positions": 6.5}, "supported count"),
        ({"total_positions": "6"}, "supported count"),
        ({"target_choice": "future"}, "registered Room target"),
        ({"repeat_main_position": False}, "automatic trust check"),
        ({"capture_transport": "future"}, "relay or local"),
    ],
)
def test_start_rejects_values_outside_the_disclosed_run_contract_before_dsp(
    monkeypatch,
    body,
    message,
):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(
        correction_setup,
        "_camilla",
        lambda: pytest.fail("CamillaDSP should not be touched"),
    )

    with pytest.raises(ValueError, match=message):
        correction_setup._handle_start(_DummyJsonHandler(body))

    assert correction_setup._start_in_progress is False


def test_start_defaults_to_configured_relay_before_session_admission(
    monkeypatch,
):
    from jasper.capture_relay import correction_adapter
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(correction_adapter, "relay_enabled", lambda: True)

    async def restore_level_match_volume(_setter):
        return True

    monkeypatch.setattr(
        correction_setup,
        "_get_or_create_session",
        lambda: SimpleNamespace(
            restore_level_match_volume=restore_level_match_volume,
        ),
    )
    captured = {}

    def replace_session(**kwargs):
        captured.update(kwargs)
        sess = SimpleNamespace(
            browser_audio_report={
                "failed": True,
                "refusal_reasons": ["test stop after admission"],
            },
        )
        captured["session"] = sess
        return sess

    monkeypatch.setattr(correction_setup, "_replace_session", replace_session)
    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())

    with pytest.raises(ValueError, match="not safe for measurement"):
        correction_setup._handle_start(_DummyJsonHandler())

    assert captured["total_positions"] == 6
    assert captured["target_choice"] == "flat"
    assert captured["strategy_choice"] == "balanced"
    assert captured["repeat_main_position"] is True
    assert captured["session"].capture_transport == "relay"
    assert correction_setup._start_in_progress is False


def test_start_rejects_explicit_relay_when_it_is_not_configured(monkeypatch):
    from jasper.capture_relay import correction_adapter
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(correction_adapter, "relay_enabled", lambda: False)
    monkeypatch.setattr(
        correction_setup,
        "_camilla",
        lambda: pytest.fail("CamillaDSP should not be touched"),
    )

    with pytest.raises(ValueError, match="phone capture is not configured"):
        correction_setup._handle_start(
            _DummyJsonHandler({"capture_transport": "relay"})
        )

    assert correction_setup._start_in_progress is False


def test_start_handler_rejects_reserved_start_before_state_transition(monkeypatch):
    """Close the narrow race before a new session leaves IDLE.

    A second handcrafted /start must be rejected even before the first
    background sweep has transitioned the fresh session into PREPARING.
    """
    from jasper.web import correction_setup
    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: _READY_ROOM_CORRECTION_SETUP,
    )

    monkeypatch.setattr(correction_setup, "_session", None)
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)

    assert correction_setup._reserve_start_slot() is None
    try:
        with pytest.raises(RuntimeError, match="measurement already in progress"):
            correction_setup._handle_start(_DummyJsonHandler())
    finally:
        correction_setup._clear_start_slot()


def test_start_handler_rejects_uncommissioned_active_speaker_before_reservation(
    monkeypatch,
):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_room_correction_readiness",
        lambda: {
            "active": True,
            "room_correction_allowed": False,
            "acoustic_commissioning": {
                "decision_schema_version": 1,
                "authority": None,
                "allowed": False,
                "status": "incomplete",
                "reason": "active_summed_acoustic_evidence_incomplete",
                "detail": (
                    "Finish the acoustic combined-crossover check before room "
                    "correction."
                ),
                "setup_href": "/correction/crossover/",
            },
        },
    )
    monkeypatch.setattr(
        correction_setup,
        "_reserve_start_slot",
        lambda: pytest.fail("readiness must reject before session reservation"),
    )
    monkeypatch.setattr(
        correction_setup,
        "_replace_session",
        lambda **_kwargs: pytest.fail("readiness must reject before session creation"),
    )

    with pytest.raises(correction_setup.RoomRequestFailure) as exc_info:
        correction_setup._handle_start(_DummyJsonHandler())

    assert exc_info.value.status == HTTPStatus.CONFLICT
    assert exc_info.value.failure == {
        "code": "speaker_setup_incomplete",
        "text": "Finish speaker setup first.",
        "retryable": False,
        "recovery_action": {
            "label": "Open speaker setup",
            "href": "/correction/crossover/",
        },
    }
    assert "combined-crossover" not in str(exc_info.value.failure)


def test_sessions_endpoint_lists_bundles(tmp_path: Path, monkeypatch):
    """GET /sessions returns recent info.json entries sorted newest-
    first. Bundles missing an info.json (in-progress writes) are
    skipped silently so a partial state doesn't 500 the endpoint."""
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    # Two complete bundles + one broken one.
    for sid, started in [("aaa", 1000), ("bbb", 2000)]:
        d = sessions_dir / sid
        d.mkdir()
        (d / "info.json").write_text(json.dumps({
            "session_id": sid,
            "state": "applied",
            "started_at": started,
            "target_choice": "flat",
            "peqs": [],
        }))
        (d / "result.json").write_text("{}")
    (sessions_dir / "broken").mkdir()
    (sessions_dir / "broken" / "info.json").write_text("not json")
    (sessions_dir / "no_info").mkdir()

    # Point the session module at tmp_path so /sessions reads from
    # the test dir.
    fake_sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=sessions_dir,
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake_sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/sessions", timeout=5,
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    sessions = body["sessions"]
    assert len(sessions) == 2  # broken / no_info excluded
    # Sorted by started_at desc — bbb (2000) before aaa (1000).
    assert sessions[0]["session_id"] == "bbb"
    assert sessions[1]["session_id"] == "aaa"
    # Decorations added by the handler.
    assert sessions[0]["has_result"] is True
    assert sessions[0]["has_applied_yml"] is False
    assert sessions[0]["has_verify_wav"] is False
    assert sessions[0]["bundle_size_bytes"] > 0
    assert sessions[0]["private_raw_audio_count"] == 0
    assert sessions[0]["bundle_dir"] == str(sessions_dir / "bbb")


def test_session_delete_endpoint_removes_historical_bundle(
    tmp_path: Path,
    monkeypatch,
):
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig

    sessions_dir = tmp_path / "sessions"
    write_golden_correction_bundle(sessions_dir, "old-session")
    fake_sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=sessions_dir,
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake_sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = json_post_with_csrf(
            base,
            "/session/delete",
            {"id": "old-session"},
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    assert body == {"deleted": True, "session_id": "old-session"}
    assert not (sessions_dir / "old-session").exists()


def test_session_delete_endpoint_refuses_current_ready_bundle(
    tmp_path: Path,
    monkeypatch,
):
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig, SessionState

    sessions_dir = tmp_path / "sessions"
    fake_sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=sessions_dir,
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    fake_sess.state = SessionState.READY
    bundle = write_golden_correction_bundle(sessions_dir, fake_sess.session_id)
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake_sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = json_post_with_csrf(
            base,
            "/session/delete",
            {"id": fake_sess.session_id},
            expect_status=409,
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    assert body["error"] == (
        "cannot delete the measurement bundle for an active session"
    )
    assert bundle.exists()


def test_session_report_endpoint_returns_evidence_packet(
    tmp_path: Path,
    monkeypatch,
):
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig

    sessions_dir = tmp_path / "sessions"
    write_golden_correction_bundle(sessions_dir, "bbb")
    fake_sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=sessions_dir,
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake_sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/session-report?id=bbb",
            timeout=5,
        )
        body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()

    assert body["session_id"] == "bbb"
    assert body["evidence"]["session_id"] == "bbb"
    assert body["evidence"]["agent_readiness"]["allowed_review"] is True
    versions = body["artifact_versions"]
    assert versions["bundle_schema_version"] == bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    assert versions["artifact_manifest_schema_version"] == (
        bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
    )
    assert versions["result_json_schema_version"] == (
        bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    )
    assert versions["runtime_integrity_schema_version"] == 1
    assert versions["acoustic_quality_schema_version"] == 1


def test_session_report_payload_builder_returns_evidence_versions(
    tmp_path: Path,
):
    from jasper.web import correction_report

    sessions_dir = tmp_path / "sessions"
    write_golden_correction_bundle(sessions_dir, "bbb")

    payload = correction_report.build_session_report_payload(
        sessions_dir=sessions_dir,
        session_id="bbb",
    )

    assert payload["session_id"] == "bbb"
    assert payload["evidence"]["artifact_schema_version"] == evidence.SCHEMA_VERSION
    assert payload["artifact_versions"]["expected_evidence_packet_schema_version"] == (
        evidence.SCHEMA_VERSION
    )
    assert payload["artifact_versions"]["evidence_packet_schema_version"] == (
        evidence.SCHEMA_VERSION
    )


def test_session_report_endpoint_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch,
):
    """The browser report route accepts a session id, not a path.

    Keep this as an HTTP-level regression so future route refactors
    preserve the client-visible 400 instead of accidentally probing
    outside the sessions directory.
    """
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    fake_sess = MeasurementSession(
        SessionConfig(
            sweep_dir=tmp_path / "sweeps",
            capture_dir=tmp_path / "captures",
            sessions_dir=sessions_dir,
            config_dir=tmp_path / "configs",
            base_config_path=tmp_path / "v1.yml",
        ),
    )
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake_sess,
    )

    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/session-report?id=..%2Fsecret",
                timeout=5,
            )
        assert exc.value.code == 400
        body = json.loads(exc.value.read())
    finally:
        server.shutdown()
        server.server_close()

    assert body["error"] == "invalid session id"


def test_render_page_includes_current_correction_banner():
    """Pin the banner element + reset-from-banner button in the
    rendered page so a future stylesheet refactor doesn't drop them.
    """
    from pathlib import Path

    from jasper.web import correction_setup
    body = correction_setup._render_page("jts.local").decode()
    # Banner markup + the auto-reset hint stay in the page; the render/refresh
    # logic moved into the relocated static ES module when /correction/ adopted
    # the canonical design system (chrome-only restyle).
    assert 'id="current-correction"' in body
    assert 'id="current-correction-label"' in body
    assert 'id="current-correction-reset"' in body
    # The hint near the Run measurement button explains the bypass behavior so
    # users aren't surprised by sweeps ignoring correction/preference layers.
    assert "JTS temporarily pauses your current sound settings" in body
    module_js = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "assets" / "correction" / "js" / "main.js"
    ).read_text()
    assert "renderCurrentCorrection" in module_js
    assert "refreshCurrentCorrection" in module_js
