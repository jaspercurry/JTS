# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared MeasurementSession construction for correction tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

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


def seed_prior_sound_config(sess: MeasurementSession) -> Path:
    """Write a JTS-generated ``sound_current.yml`` for ``sess`` to report.

    ``apply()`` always resolves the CamillaDSP-loaded config through the
    topology-aware graph carrier, which classifies it by path + content
    (see ``carrier_for_loaded_config``). Tests that drive ``apply()``
    through a fake setter need a real, classifiable prior config on disk
    for their ``camilla_get_config`` stub to report.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    sess.cfg.config_dir.mkdir(parents=True, exist_ok=True)
    prior = sess.cfg.config_dir / "sound_current.yml"
    prior.write_text(
        emit_sound_config(SoundProfile(enabled=False)),
        encoding="utf-8",
    )
    return prior


def stateful_camilla_stub(
    initial_path: Path,
) -> tuple[Callable[[], Awaitable[str]], Callable[[str], None]]:
    """A ``(camilla_get_config, note_loaded)`` pair simulating CamillaDSP's
    own loaded-config bookkeeping.

    ``apply()``'s post-load confirm phase re-reads ``camilla_get_config``
    after the candidate is loaded and requires it to report that exact
    path — a getter fixed at ``initial_path`` fails confirm on every real
    ``apply()`` call. Call ``note_loaded(path)`` from the test's
    ``camilla_set_config`` stub so the getter reflects the swap.
    """
    state = {"path": str(initial_path)}

    async def get_config() -> str:
        return state["path"]

    def note_loaded(path: str) -> None:
        state["path"] = str(path)

    return get_config, note_loaded


async def default_bass_profile_summary() -> Mapping[str, Any]:
    """Stub ``apply(prepare_guard=...)`` — a passing bass-authority check."""
    return {"authority_valid": True, "runtime_block_required": False}
