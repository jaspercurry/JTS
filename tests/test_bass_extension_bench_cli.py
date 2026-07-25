# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI preflight: dry-run authors + validates, a gap refuses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from jasper.bass_extension.bench.manifest import STIMULUS_ROLES
from jasper.cli import bass_extension_bench


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request() -> dict[str, Any]:
    return {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -30.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 12.0,
        "requested_cooldown_s": 4.0,
        "requested_repeat_count": 2,
        "stimulus_generator_identity": "gen-v1",
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 536_870_912,
        "render_rlimit_cpu_s": 60,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.25,
        "cross_check_read_count": 40,
        "cross_check_tolerance_db": 1.5,
    }


def _inputs(*target_ids: str) -> dict[str, Any]:
    return {
        "driver_safety_fingerprint": _sha("ds"),
        "margin_policy_name": "conservative",
        "margin_policy_fingerprint": _sha("mp"),
        "requests": {tid: {role: _request() for role in STIMULUS_ROLES} for tid in target_ids},
    }


def _write(tmp_path: Path, inputs: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    return path


def test_dry_run_authors_and_prints_the_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _inputs("deep", "natural"))
    rc = bass_extension_bench.main([str(path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "margin=conservative" in out
    assert "deep, natural" in out
    assert "jts_bass_extension_limiter_evidence" in out
    assert "[-120.0, 0.0] dBFS" in out


def test_missing_input_refuses_with_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inputs = _inputs("deep")
    del inputs["requests"]["deep"]["sweep_transparency"]["requested_hold_duration_s"]
    path = _write(tmp_path, inputs)
    rc = bass_extension_bench.main([str(path), "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "requests.deep.sweep_transparency.requested_hold_duration_s" in err


def test_unknown_margin_is_rejected(tmp_path: Path) -> None:
    inputs = _inputs("deep")
    inputs["margin_policy_name"] = "reckless"
    path = _write(tmp_path, inputs)
    with pytest.raises(SystemExit):
        bass_extension_bench.main([str(path), "--dry-run"])


def test_no_flags_defaults_to_the_safe_dry_run_posture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry-run is the DEFAULT posture: omitting both --dry-run and --live
    must never attempt live execution."""

    path = _write(tmp_path, _inputs("deep"))
    rc = bass_extension_bench.main([str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run: no device opened" in out


def test_dry_run_wins_even_when_live_is_also_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, _inputs("deep"))
    rc = bass_extension_bench.main([str(path), "--dry-run", "--live"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run: no device opened" in out


def test_live_run_fails_closed_without_the_on_device_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.bass_extension.bench.render import BinaryIdentity

    fake_binary = BinaryIdentity(
        path="/opt/camilladsp/camilladsp",
        version_output="CamillaDSP 4.1.3",
        sha256="0" * 64,
        camilladsp_build_id="camilladsp-v4.1.3-000000000000",
    )
    monkeypatch.setattr(
        bass_extension_bench, "resolve_render_binary", lambda: fake_binary
    )
    path = _write(tmp_path, _inputs("deep"))
    with pytest.raises(SystemExit) as excinfo:
        bass_extension_bench.main([str(path), "--live"])
    assert "on-device" in str(excinfo.value)
    assert "TargetPlan" in str(excinfo.value)
    assert "PlayAndCapture" in str(excinfo.value)


def test_live_run_refuses_when_the_render_binary_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jasper.bass_extension.bench.render import RenderError

    def _raise() -> None:
        raise RenderError("no jasper-camilla.service on this host")

    monkeypatch.setattr(bass_extension_bench, "resolve_render_binary", _raise)
    path = _write(tmp_path, _inputs("deep"))
    rc = bass_extension_bench.main([str(path), "--live"])
    assert rc == 2
