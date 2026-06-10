from __future__ import annotations

import subprocess
from pathlib import Path

from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    DspApplyState,
    ValidationStatus,
    apply_dsp_config,
    dsp_apply_lock_path,
    dsp_write_epoch,
    dsp_write_epoch_from_state,
    last_dsp_apply_state,
    record_dsp_apply_state,
    validate_camilla_config,
)


def _fake_camilladsp(tmp_path: Path, *, exit_code: int = 0) -> Path:
    script = tmp_path / "camilladsp"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$JASPER_ARGV_CAPTURE\"\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


def test_validate_camilla_config_uses_check_flag_with_positional_config(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    argv_capture = tmp_path / "argv.txt"
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))
    monkeypatch.setenv("JASPER_ARGV_CAPTURE", str(argv_capture))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.VALID
    assert argv_capture.read_text().splitlines() == ["--check", str(cfg)]


def test_dsp_write_epoch_tracks_latest_apply_state(tmp_path: Path):
    state_path = tmp_path / "dsp_apply_state.json"

    assert dsp_write_epoch_from_state(None) == "none"
    assert dsp_write_epoch(state_path=state_path) == "none"

    record_dsp_apply_state(
        DspApplyState(
            schema_version=1,
            op_id="op-123",
            source="test",
            phase="done",
            result="success",
            started_at="2026-05-28T00:00:00Z",
            finished_at="2026-05-28T00:00:01Z",
            prior_config_path=None,
            candidate_config_path="/tmp/test.yml",
        ),
        state_path=state_path,
    )

    assert dsp_write_epoch(state_path=state_path) == "op-123"
    assert dsp_apply_lock_path(tmp_path) == tmp_path / ".dsp_apply.lock"


def test_validate_camilla_config_classifies_invalid_config(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    binary = _fake_camilladsp(tmp_path, exit_code=101)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))
    monkeypatch.setenv("JASPER_ARGV_CAPTURE", str(tmp_path / "argv.txt"))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply


def test_validate_camilla_config_classifies_usage_error_as_runner_error(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    script = tmp_path / "camilladsp"
    script.write_text(
        "#!/bin/sh\n"
        "printf 'Usage: camilladsp [OPTIONS] [CONFIGFILE]\\n' >&2\n"
        "exit 2\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(script))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.RUNNER_ERROR
    assert not result.ok_to_apply


def test_validate_camilla_config_timeout_output_is_json_safe(
    tmp_path: Path,
    monkeypatch,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", "/tmp/camilladsp")

    def fake_run(*args, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(
            cmd=["camilladsp"],
            timeout=10,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr("jasper.dsp_apply.subprocess.run", fake_run)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.TIMEOUT
    assert result.stdout_tail == "partial stdout"
    assert result.stderr_tail == "partial stderr"
    assert isinstance(result.to_dict()["stdout_tail"], str)


async def test_apply_dsp_config_validation_failure_does_not_load_or_persist(
    tmp_path: Path,
):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    loaded: list[str] = []
    persisted = False

    async def load(path: str) -> bool:
        loaded.append(path)
        return True

    def persist() -> None:
        nonlocal persisted
        persisted = True

    def validate(path: str | Path) -> CamillaConfigValidationResult:
        return CamillaConfigValidationResult(
            status=ValidationStatus.INVALID_CONFIG,
            path=str(path),
            returncode=101,
        )

    try:
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            prior_config_path="/etc/camilladsp/v1.yml",
            persist=persist,
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=validate,
        )
    except DspApplyError as e:
        assert e.state.result == "invalid_config"
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected validation failure")

    assert loaded == []
    assert not persisted
    assert last_dsp_apply_state(
        state_path=tmp_path / "dsp_apply_state.json",
    )["result"] == "invalid_config"


async def test_apply_dsp_config_rolls_back_when_reload_fails(tmp_path: Path):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\n")
    calls: list[str] = []

    async def load(path: str) -> bool:
        calls.append(path)
        if path == str(cfg):
            raise RuntimeError("reload failed")
        return True

    try:
        await apply_dsp_config(
            source="sound",
            candidate_path=cfg,
            load_config=load,
            prior_config_path="/etc/camilladsp/v1.yml",
            state_path=tmp_path / "dsp_apply_state.json",
            lock_path=tmp_path / "dsp_apply.lock",
            validate=lambda path: CamillaConfigValidationResult(
                status=ValidationStatus.VALID,
                path=str(path),
            ),
        )
    except DspApplyError as e:
        assert e.state.result == "load_failed_rolled_back"
        assert e.state.rollback_succeeded is True
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected reload failure")

    assert calls == [str(cfg), "/etc/camilladsp/v1.yml"]


# ---------------------------------------------------------------------------
# Audit C6 — devices.volume_limit safety ceiling at the validate gate.
# CamillaDSP's own --check accepts a positive limit and defaults the main
# fader's maximum to +50 dB when the key is omitted; the JTS apply path
# must reject both shapes before anything touches live audio.
# ---------------------------------------------------------------------------


def test_validate_rejects_positive_volume_limit(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 3.0\n")
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply
    assert "0 dB" in (result.error or "")


def test_validate_rejects_missing_volume_limit(tmp_path: Path, monkeypatch):
    """Omitted key means CamillaDSP defaults the fader ceiling to +50 dB —
    a loud-output hazard, rejected like a positive limit."""
    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  samplerate: 48000\n")
    binary = _fake_camilladsp(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(binary))

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply
    assert "volume_limit" in (result.error or "")


def test_validate_limit_check_applies_without_camilladsp_binary(
    tmp_path: Path, monkeypatch,
):
    """Dev machines without CamillaDSP skip the CLI preflight (MISSING is
    ok_to_apply) but must still get the pure-Python safety rejection."""
    import jasper.dsp_apply as dsp_apply

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 6.0\n")
    monkeypatch.setattr(dsp_apply, "_camilladsp_binary", lambda: None)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.INVALID_CONFIG
    assert not result.ok_to_apply


def test_validate_accepts_zero_volume_limit_without_binary(
    tmp_path: Path, monkeypatch,
):
    import jasper.dsp_apply as dsp_apply

    cfg = tmp_path / "candidate.yml"
    cfg.write_text("---\ndevices:\n  volume_limit: 0.0\n")
    monkeypatch.setattr(dsp_apply, "_camilladsp_binary", lambda: None)

    result = validate_camilla_config(cfg)

    assert result.status == ValidationStatus.MISSING
    assert result.ok_to_apply
