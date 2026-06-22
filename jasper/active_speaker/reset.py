# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reset helpers for active-speaker setup evidence.

These helpers clear durable wizard/evidence state after the saved output
topology is reset to passive. They intentionally do not delete generated
CamillaDSP YAML files: those are inert without the state/evidence JSON and can
be useful for forensics, while a loaded runtime graph is reconciled separately.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

from .baseline_profile import baseline_profile_state_path
from .commission_ramp import ramp_state_path
from .crossover_preview import crossover_preview_path
from .design_draft import DEFAULT_DESIGN_DRAFT_PATH, DESIGN_DRAFT_PATH_ENV
from .measurement import measurement_state_path
from .path_safety import path_safety_evidence_path
from .staging import staged_metadata_path
from .startup_load import commission_load_state_path, startup_load_state_path

logger = logging.getLogger(__name__)

ACTIVE_SPEAKER_SETUP_RESET_KIND = "jts_active_speaker_setup_reset"


def _design_draft_state_path(path: str | Path | None = None) -> Path:
    import os

    return Path(path or os.environ.get(DESIGN_DRAFT_PATH_ENV) or DEFAULT_DESIGN_DRAFT_PATH)


def active_speaker_setup_state_paths() -> dict[str, Path]:
    """Return reset-owned active-speaker setup/evidence JSON paths."""

    return {
        "design_draft": _design_draft_state_path(),
        "crossover_preview": crossover_preview_path(),
        "staged_config": staged_metadata_path(),
        "path_safety": path_safety_evidence_path(),
        "startup_load": startup_load_state_path(),
        "commission_load": commission_load_state_path(),
        "commission_ramp": ramp_state_path(),
        "measurements": measurement_state_path(),
        "baseline_profile": baseline_profile_state_path(),
    }


def clear_active_speaker_setup_state() -> dict[str, Any]:
    """Remove stale active-speaker setup/evidence state after topology reset."""

    cleared: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for artifact_id, path in active_speaker_setup_state_paths().items():
        try:
            path.unlink()
        except FileNotFoundError:
            missing.append({"id": artifact_id, "path": str(path)})
        except OSError as exc:
            errors.append({
                "id": artifact_id,
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            })
        else:
            cleared.append({"id": artifact_id, "path": str(path)})

    status = "partial" if errors else "cleared"
    log_event(
        logger,
        "active_speaker.setup_reset",
        status=status,
        cleared=len(cleared),
        missing=len(missing),
        errors=len(errors),
        level=logging.WARNING if errors else logging.INFO,
    )
    return {
        "artifact_schema_version": 1,
        "kind": ACTIVE_SPEAKER_SETUP_RESET_KIND,
        "status": status,
        "cleared": cleared,
        "missing": missing,
        "errors": errors,
    }


__all__ = [
    "ACTIVE_SPEAKER_SETUP_RESET_KIND",
    "active_speaker_setup_state_paths",
    "clear_active_speaker_setup_state",
]
