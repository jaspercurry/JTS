"""Current-correction visibility + per-session debug bundles.

Two features land in this PR. Both are exercised here:

  A) `parse_current_correction` keeps the backwards-compatible
     "JTS room correction or None" behavior, while
     `describe_current_config` gives UI/doctor surfaces the fuller
     truth about flat, preference, correction, or custom CamillaDSP
     configs. /start auto-resets CamillaDSP to the base config first
     so every measurement reflects the raw room rather than the
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
from pathlib import Path

import pytest

from jasper.correction import bundles, evidence
from jasper.correction.session import (
    MeasurementSession,
    SessionConfig,
    SessionState,
    describe_current_config,
    parse_current_correction,
)
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile
from ._web_test_helpers import json_post_with_csrf


# ---------- parse_current_correction ---------------------------------------


def test_parse_current_correction_base_config_returns_none(tmp_path: Path):
    """The base /etc/camilladsp/v1.yml is "no correction applied" —
    the UI shows the flat banner without a Reset button."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    # Anywhere that doesn't match the /var/lib/camilladsp/configs/
    # correction_* shape is treated as "no correction" by definition.
    assert parse_current_correction(
        "/etc/camilladsp/v1.yml", config_dir=cfg_dir,
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


def _make_session(tmp_path: Path, **kwargs) -> MeasurementSession:
    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        sessions_dir=tmp_path / "sessions",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        duration_s=1.0,
    )
    cfg.base_config_path.write_text("# stub base v1.yml for tests\n")
    cfg.config_dir.mkdir(exist_ok=True)
    return MeasurementSession(cfg, **kwargs)


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
async def test_design_writes_result_json(tmp_path: Path):
    """After spatial average + PEQ design, result.json captures the
    measured / target / predicted curves so a copied-off bundle is
    re-renderable without re-running the deconvolution."""
    import numpy as np
    from jasper.correction import sweep

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


# ---------- /start auto-reset + /sessions endpoint -------------------------


class _FakeCamilla:
    """Records calls to set_config_file_path so we can assert the
    /start handler resets to base BEFORE the sweep kicks off."""
    def __init__(self, current_path: str, *, reset_ok: bool = True) -> None:
        self.current_path = current_path
        self.reset_ok = reset_ok
        self.set_calls: list[str] = []

    async def get_config_file_path(self, *, best_effort: bool = False):
        return self.current_path

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
    headers = {"Content-Length": "2"}

    def __init__(self) -> None:
        self.rfile = io.BytesIO(b"{}")


def test_start_handler_resets_to_base_before_sweep(
    tmp_path: Path, monkeypatch,
):
    """Pin the load-bearing behavior: /start calls
    CamillaController.set_config_file_path(base_config_path) BEFORE
    it kicks off the measurement window. Without this, a sweep run
    on top of an existing correction would design new filters from
    the already-corrected curve and produce compounding distortion.
    """
    from jasper.web import correction_setup
    monkeypatch.setattr(correction_setup, "_session", None)
    fake_cam = _FakeCamilla(
        current_path=str(tmp_path / "configs" / "correction_xyz_1700.yml"),
    )
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
    # The /start handler should have called set_config_file_path
    # exactly once before kicking off the sweep, with the base
    # config path.
    assert fake_cam.set_calls == [str(sess.cfg.base_config_path)]
    # And snapshot the prior correction descriptor in the response so
    # the UI can render "was: correction_xyz" if it wants.
    prior = body["current_correction_at_start"]
    assert prior is not None
    assert prior["kind"] == "correction"
    assert prior["current_correction"]["session_id"] == "xyz"
    assert body["strategy_choice"] == "balanced"
    assert body["correction_strategy"]["strategy_id"] == "balanced"


def test_start_handler_aborts_if_reset_to_base_fails(
    tmp_path: Path, monkeypatch,
):
    """If CamillaDSP cannot switch to the flat base config, /start must
    fail before playing a sweep. Measuring through an existing
    correction would compound filters and corrupt the result."""
    from jasper.web import correction_setup
    monkeypatch.setattr(correction_setup, "_session", None)

    fake_cam = _FakeCamilla(
        current_path=str(tmp_path / "configs" / "correction_xyz_1700.yml"),
        reset_ok=False,
    )
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

    with pytest.raises(RuntimeError, match="reset speaker to flat"):
        correction_setup._handle_start(_DummyJsonHandler())

    sess = captured["sess"]
    assert fake_cam.set_calls == [str(sess.cfg.base_config_path)]
    assert scheduled["value"] is False
    assert correction_setup._start_in_progress is False


def test_start_handler_rejects_active_measurement(monkeypatch):
    """Server-side guard for handcrafted double-start requests.

    The browser disables the Run button while measuring, but the
    backend also needs to refuse a second /start while a sweep/capture
    lifecycle is already active.
    """
    from jasper.web import correction_setup

    class ActiveSession:
        state = SessionState.SWEEPING

    monkeypatch.setattr(correction_setup, "_session", ActiveSession())

    with pytest.raises(RuntimeError, match="measurement already in progress"):
        correction_setup._handle_start(_DummyJsonHandler())


def test_start_handler_rejects_reserved_start_before_state_transition(monkeypatch):
    """Close the narrow race before a new session leaves IDLE.

    A second handcrafted /start must be rejected even before the first
    background sweep has transitioned the fresh session into PREPARING.
    """
    from jasper.web import correction_setup

    monkeypatch.setattr(correction_setup, "_session", None)
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)

    assert correction_setup._reserve_start_slot() is None
    try:
        with pytest.raises(RuntimeError, match="measurement already in progress"):
            correction_setup._handle_start(_DummyJsonHandler())
    finally:
        correction_setup._clear_start_slot()


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
    assert sessions[0]["bundle_dir"] == str(sessions_dir / "bbb")


def _write_report_bundle(sessions_dir: Path, session_id: str) -> Path:
    bundle = sessions_dir / session_id
    bundle.mkdir(parents=True)
    freqs = [50, 80, 160, 320, 500]
    info = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "state": "ready",
        "started_at": 2000,
        "current_position": 1,
        "total_positions": 1,
        "target_choice": "flat",
        "strategy_choice": "balanced",
        "capture_quality": [],
        "runtime_integrity": {"level": "ok", "issue_count": 0},
        "acoustic_quality": {
            "level": "ok",
            "snr_level": "high",
            "min_estimated_snr_db": 32.0,
        },
    }
    result = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "measured": {"freqs_hz": freqs, "magnitude_db": [0, 1, 2, 1, 0]},
        "target": {"freqs_hz": freqs, "magnitude_db": [0, 0, 0, 0, 0]},
        "confidence_report": {
            "level": "high",
            "score": 88,
            "strategy_gates": {
                "safe": {"allowed": True, "reasons": []},
                "balanced": {"allowed": True, "reasons": []},
                "assertive": {"allowed": False, "reasons": ["needs repeat"]},
            },
        },
    }
    runtime = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "artifact_schema_version": 1,
        "summary": {"level": "ok", "issue_count": 0},
        "issues": [],
    }
    acoustic = {
        "artifact_schema_version": 1,
        "summary": {
            "level": "ok",
            "snr_level": "high",
            "min_estimated_snr_db": 32.0,
        },
        "issues": [],
    }
    bundles.write_json_artifact(
        bundle,
        "info.json",
        info,
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="test",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        result,
        kind="analysis_result",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="test",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "runtime_integrity.json",
        runtime,
        kind="runtime_integrity",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="test",
        schema_version=1,
    )
    bundles.write_json_artifact(
        bundle,
        "acoustic_quality.json",
        acoustic,
        kind="acoustic_quality",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="test",
        schema_version=1,
    )
    return bundle


def test_session_report_endpoint_returns_evidence_packet(
    tmp_path: Path,
    monkeypatch,
):
    from jasper.web import correction_setup
    from jasper.correction.session import SessionConfig

    sessions_dir = tmp_path / "sessions"
    _write_report_bundle(sessions_dir, "bbb")
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
    _write_report_bundle(sessions_dir, "bbb")

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
    from jasper.web import correction_setup
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="current-correction"' in body
    assert 'id="current-correction-label"' in body
    assert 'id="current-correction-reset"' in body
    assert "renderCurrentCorrection" in body
    assert "refreshCurrentCorrection" in body
    # The hint near the Run measurement button explains the auto-
    # reset behavior so users aren't surprised by sweeps wiping
    # their correction.
    assert "Each measurement starts from flat" in body
