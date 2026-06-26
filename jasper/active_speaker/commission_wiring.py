# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared operator-surface wiring for per-driver commissioning + the Stage-5 ramp.

The ``jasper-active-speaker`` CLI and the ``/sound/`` commission card drive the
same guarded machinery (``startup_load.load_driver_commissioning_config`` /
``commission_ramp.ramp_audible_step``). This module owns the small glue both
need so neither hand-rolls it: the INLINE CamillaController seams, the
saved-crossover-preview resolution, and fresh path-safety evidence. Living here
in the ``active_speaker`` layer — not in ``cli/`` or ``web/`` — is what lets a
third operator surface (a voice tool, a dial/satellite action) land
declaration-only instead of copying the wiring a third time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

PathLoader = Callable[[str], Awaitable[bool]]
RunningConfigReader = Callable[[], Awaitable[str | None]]
ConfigPathReader = Callable[[], Awaitable[str | None]]


def commission_load_config(cam: Any) -> PathLoader:
    """The INLINE loader seam: read the candidate file and apply it as the running
    graph WITHOUT repointing the persisted statefile.

    ``CamillaController.set_active_config_raw`` is CamillaDSP's ``SetConfig``; it
    leaves ``config_file_path`` (the outputd statefile boot anchor) untouched,
    which is what makes crash-recovery-MUTED *structural* — a reboot still comes
    up on the all-muted staged boot config. Loading via ``set_config_file_path``
    would repoint the statefile and break that invariant.
    """

    async def _load(path: str) -> bool:
        text = Path(path).read_text(encoding="utf-8")
        return await cam.set_active_config_raw(text, best_effort=False)

    return _load


def commission_seams(
    cam: Any,
) -> tuple[PathLoader, RunningConfigReader, ConfigPathReader]:
    """The three inline-transport seams a guarded commission load/ramp needs:
    ``(load_config, read_running_config, get_current_config_path)``."""
    return (
        commission_load_config(cam),
        lambda: cam.get_active_config_raw(best_effort=False),
        lambda: cam.get_config_file_path(best_effort=False),
    )


async def read_current_config_path(cam: Any) -> tuple[str | None, str | None]:
    """Read the persisted config path, fail-soft: ``(path, error_type_name)``.

    The path binds the path-safety evidence + the active-graph-is-staged
    precondition; a transient read failure becomes blocked evidence, never a
    crash.
    """
    try:
        return (await cam.get_config_file_path(best_effort=False)), None
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def resolve_commission_inputs(preset: Any = None) -> tuple[Any, dict[str, Any] | None]:
    """Resolve ``(preset, crossover_preview)`` so the per-driver commissioning
    config matches what protected staging emitted.

    Staging compiles from the saved crossover preview, so the per-driver load
    must use the SAME source or its mask/crossover would not match the active
    all-muted graph. Default: the saved preview when it is ready for staging, else
    the bundled-preset fallback (exactly what staging does with no ready preview).
    An explicit ``preset`` object overrides (bench / preset-fallback work).
    """
    if preset is not None:
        return preset, None
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    if preview.get("status") == "ready_for_protected_staging":
        return None, preview
    return None, None


def write_commission_path_safety(
    topology: Any,
    staged: dict[str, Any],
    current_config_path: str | None,
    current_config_error: str | None,
    *,
    require_physical_identity: bool = True,
) -> str:
    """Build + persist fresh no-audio startup-load path-safety evidence bound to
    the current config; return its path.

    The per-driver commissioning preflight reuses ``build_startup_load_preflight``,
    which binds to this evidence to prove the speaker is ready for an active load
    and the all-muted staged config is a valid rollback anchor.
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
