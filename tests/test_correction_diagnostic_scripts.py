# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the laptop-side correction diagnostic pair."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType


REPO = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    path = REPO / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tone_detection_uses_correction_lane_not_household_playback() -> None:
    capture = _load_script("capture-correction-diagnostic.py")

    assert capture._tone_is_active({"playback_peak_dbfs": [-3.0]}) is False
    assert capture._tone_is_active({"correction_input_rms_dbfs": -35.0}) is True


def test_state_only_bundle_handles_timeline_error_rows(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir(mode=0o700)
    (bundle / "manifest.json").write_text(
        json.dumps({"state_only": True}) + "\n", encoding="utf-8"
    )
    (bundle / "speaker_timeline.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"utc": "now", "error": "temporary fetch failure"}),
                json.dumps(
                    {
                        "t_epoch_s": 1.0,
                        "correction_input_rms_dbfs": -32.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "analyze-correction-diagnostic.py"),
            str(bundle),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    output = bundle / "analysis.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["state_only"] is True
    assert payload["stimulus"]["detected"] is True
    assert payload["speaker_timeline"] == {
        "sample_count": 1,
        "error_count": 1,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_nonfinite_diagnostic_values_serialize_as_null() -> None:
    analyze = _load_script("analyze-correction-diagnostic.py")

    assert analyze.finite_json({"thd": float("nan")}) == {"thd": None}


def test_analyzer_labels_tone_metrics_with_actual_frequency() -> None:
    source = (
        REPO / "scripts" / "analyze-correction-diagnostic.py"
    ).read_text(encoding="utf-8")

    assert '"median_tone_rms_dbfs"' in source
    assert '"max_tone_rms_dbfs"' in source
    assert '"tone_frequency_hz": expected_tone_hz' in source
    assert "median_1khz_rms_dbfs" not in source
    assert "max_1khz_rms_dbfs" not in source
    assert source.count("usable_pairs =") == 1


def test_remote_gain_archive_shell_quotes_active_config_path() -> None:
    capture = _load_script("capture-correction-diagnostic.py")
    hostile_path = "/tmp/active config; touch /tmp/escaped"

    command = capture._remote_gain_archive_command([hostile_path])

    assert command == (
        "sudo tar -czf - --ignore-failed-read -- "
        "'/tmp/active config; touch /tmp/escaped'"
    )
