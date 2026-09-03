# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared operator-surface wiring for per-driver commissioning + the Stage-5 ramp.

Owns the glue the ``jasper-active-speaker`` CLI and the ``/sound/`` commission
card both need for ``startup_load.load_driver_commissioning_config`` /
``commission_ramp.ramp_audible_step``: the inline CamillaController seams, the
saved-crossover-preview resolution, and fresh path-safety evidence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable

from ._common import finite_float

PathLoader = Callable[[str], Awaitable[bool]]
RunningConfigReader = Callable[[], Awaitable[str | None]]
ConfigPathReader = Callable[[], Awaitable[str | None]]


class CommissionPresetResolutionError(ValueError):
    """A saved crossover preview could not produce a commissioning preset."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in issues
            if isinstance(issue, Mapping)
        ]
        super().__init__(
            "active speaker preset is not ready for capture analysis"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )


def commission_load_config(cam: Any) -> PathLoader:
    """The inline loader seam: apply a candidate file as the running graph.

    ``set_active_config_raw`` is CamillaDSP's ``SetConfig``: it leaves
    ``config_file_path`` (the outputd statefile boot anchor) untouched, so a
    reboot still comes up on the all-muted staged boot config. Never load via
    ``set_config_file_path`` here — that repoints the statefile.
    """

    async def _load(path: str) -> bool:
        text = Path(path).read_text(encoding="utf-8")
        return await cam.set_active_config_raw(text, best_effort=False)

    return _load


def commission_seams(
    cam: Any,
) -> tuple[PathLoader, RunningConfigReader, ConfigPathReader]:
    """``(load_config, read_running_config, get_current_config_path)``."""
    return (
        commission_load_config(cam),
        lambda: cam.get_active_config_raw(best_effort=False),
        lambda: cam.get_config_file_path(best_effort=False),
    )


async def read_current_config_path(cam: Any) -> tuple[str | None, str | None]:
    """Read the persisted config path, fail-soft: ``(path, error_type_name)``."""
    try:
        return (await cam.get_config_file_path(best_effort=False)), None
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def resolve_commission_inputs(preset: Any = None) -> tuple[Any, dict[str, Any] | None]:
    """Resolve ``(preset, crossover_preview)`` for a per-driver commissioning load.

    Staging compiles from the saved crossover preview, so the load must use the
    SAME source or its mask/crossover would not match the active all-muted graph.
    """
    if preset is not None:
        return preset, None
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    if preview.get("status") == "ready_for_protected_staging":
        return None, preview
    return None, None


def _is_passive_mains(topology: Any) -> bool:
    """Whether this topology's mains carry no inter-driver crossover."""
    from jasper.output_topology import OutputTopology, topology_is_passive_mains

    return isinstance(topology, OutputTopology) and topology_is_passive_mains(
        topology
    )


def _passive_mains_preset(topology: Any) -> Any:
    """The passive box's own preset, compiled from its saved topology.

    Never the bundled 2-way JSON, which names drivers this box does not have.
    """
    from jasper.active_speaker.staging import build_passive_mains_preset

    compiled, raw_issues, _gates = build_passive_mains_preset(topology)
    if compiled is not None:
        return compiled
    raise CommissionPresetResolutionError(
        [issue for issue in raw_issues if isinstance(issue, dict)]
    )


def resolve_capture_preset(topology: Any) -> Any:
    """Resolve the protected preset used by every capture-analysis surface.

    The passive arm answers before ``resolve_commission_inputs``: a passive box
    compiles no crossover preview for those reads to reach.
    """

    if _is_passive_mains(topology):
        return _passive_mains_preset(topology)
    preset, crossover_preview = resolve_commission_inputs()
    return resolve_commission_preset(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )


def commissioning_spl_ceiling_db(topology: Any) -> float:
    """The commissioning SPL hard stop this box declares, in dB SPL at the mic.

    The one reader of ``safety.max_commissioning_level_db_spl``: every surface
    bounding a level against the stop resolves it here. Raises ``ValueError``
    when no finite ceiling resolves.
    """

    preset = resolve_capture_preset(topology)
    ceiling = finite_float(preset.safety.max_commissioning_level_db_spl)
    if ceiling is None:
        raise ValueError("the preset declares no finite max_commissioning_level_db_spl")
    return ceiling


def resolve_commission_preset(
    topology: Any,
    *,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> Any:
    """Resolve explicit, passive-topology, preview-compiled, or fallback preset."""

    if preset is not None:
        return preset
    if _is_passive_mains(topology):
        return _passive_mains_preset(topology)
    if crossover_preview is not None:
        from jasper.active_speaker.staging import compile_preset_from_crossover_preview

        compiled, raw_issues, _gates = compile_preset_from_crossover_preview(
            topology,
            crossover_preview,
        )
        if compiled is not None:
            return compiled
        issues = [issue for issue in raw_issues if isinstance(issue, dict)]
        raise CommissionPresetResolutionError(issues)

    from jasper.active_speaker.tone_plan import load_active_speaker_preset

    return load_active_speaker_preset(
        os.environ.get("JASPER_ACTIVE_SPEAKER_PRESET") or None
    )


def write_commission_path_safety(
    topology: Any,
    staged: dict[str, Any],
    current_config_path: str | None,
    current_config_error: str | None,
    *,
    require_physical_identity: bool = True,
) -> str:
    """Persist fresh no-audio path-safety evidence for the current config.

    ``build_startup_load_preflight`` binds to this evidence to prove the
    all-muted staged config is a valid rollback anchor.
    """
    from jasper.active_speaker.calibration_level import load_calibration_level_state
    from jasper.active_speaker.path_safety import (
        build_startup_load_path_safety_evidence,
        write_path_safety_evidence,
    )

    evidence = build_startup_load_path_safety_evidence(
        topology,
        staged_config=staged,
        calibration_level=load_calibration_level_state(),
        current_config_path=current_config_path,
        current_config_error=current_config_error,
        require_physical_identity=require_physical_identity,
    )
    return str(write_path_safety_evidence(evidence))
