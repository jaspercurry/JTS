# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared MeasurementSession construction for correction tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jasper.correction.session import MeasurementSession, SessionConfig


def make_measurement_session(
    tmp_path: Path,
    **session_kwargs: Any,
) -> MeasurementSession:
    """Build a fast single-position session under ``tmp_path``.

    Most state-machine unit tests exercise one transition and predate the
    household six-position/repeat product defaults. Keep those mechanics
    explicit here; tests of the real defaults instantiate ``MeasurementSession``
    directly with this temporary config.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        sessions_dir=tmp_path / "sessions",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        calibration_dir=tmp_path / "calibrations",
        duration_s=1.0,
    )
    cfg.base_config_path.write_text(
        "# stub base v1.yml for tests\n",
        encoding="utf-8",
    )
    session_kwargs.setdefault("total_positions", 1)
    session_kwargs.setdefault("repeat_main_position", False)
    return MeasurementSession(cfg, **session_kwargs)
