# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.reset import clear_active_speaker_setup_state


_STATE_ENVS = {
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
}


def _seed_state_paths(monkeypatch, tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for env_name, filename in _STATE_ENVS.items():
        path = tmp_path / filename
        path.write_text('{"stale": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
        paths.append(path)
    return paths


def test_clear_active_speaker_setup_state_removes_reset_owned_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = _seed_state_paths(monkeypatch, tmp_path)

    payload = clear_active_speaker_setup_state()

    assert payload["kind"] == "jts_active_speaker_setup_reset"
    assert payload["status"] == "cleared"
    assert len(payload["cleared"]) == len(paths)
    assert payload["errors"] == []
    assert all(not path.exists() for path in paths)

    second = clear_active_speaker_setup_state()

    assert second["status"] == "cleared"
    assert second["cleared"] == []
    assert len(second["missing"]) == len(paths)
    assert second["errors"] == []
