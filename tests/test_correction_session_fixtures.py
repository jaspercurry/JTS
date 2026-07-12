# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the shared correction-session test builder."""

from __future__ import annotations

from pathlib import Path

from .correction_session_fixtures import make_measurement_session


def test_session_builder_isolated_paths_lazy_dirs_and_kwargs(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "correction"
    input_device = {
        "label": "Test measurement mic",
        "device_id_hash": "test-mic",
        "sample_rate": 48_000,
    }

    session = make_measurement_session(
        root,
        total_positions=3,
        target_choice="warm",
        input_device=input_device,
        repeat_main_position=True,
    )

    assert session.cfg.sweep_dir == root / "sweeps"
    assert session.cfg.capture_dir == root / "captures"
    assert session.cfg.sessions_dir == root / "sessions"
    assert session.cfg.config_dir == root / "configs"
    assert session.cfg.base_config_path == root / "v1.yml"
    assert session.cfg.calibration_dir == root / "calibrations"
    assert session.cfg.duration_s == 1.0
    assert session.cfg.base_config_path.read_text(encoding="utf-8") == (
        "# stub base v1.yml for tests\n"
    )
    assert not session.cfg.config_dir.exists()
    assert not session.cfg.sweep_dir.exists()
    assert not session.cfg.capture_dir.exists()
    assert not session.cfg.sessions_dir.exists()
    assert not session.cfg.calibration_dir.exists()
    assert session.bundle_dir == root / "sessions" / session.session_id
    assert session.total_positions == 3
    assert session.target_choice == "warm"
    assert session.input_device == input_device
    assert session.repeat_main_position is True
