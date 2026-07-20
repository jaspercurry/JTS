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


def test_live_run_fails_closed_without_the_on_device_executor(tmp_path: Path) -> None:
    path = _write(tmp_path, _inputs("deep"))
    with pytest.raises(SystemExit) as excinfo:
        bass_extension_bench.main([str(path)])
    assert "on-device" in str(excinfo.value)
