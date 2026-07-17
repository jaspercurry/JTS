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


# Artifacts that are pure MEASUREMENT-JOURNEY evidence: driver captures, the
# summed-crossover validation, the compiled-but-not-loaded protected-startup
# candidate, its pre-load safety checklist, and the per-driver commissioning
# ramp/load bookkeeping. Clearing these lets a household restart the guided
# capture flow from a clean slate.
#
# Deliberately EXCLUDED from this subset, unlike the nuclear
# ``active_speaker_setup_state_paths()`` above:
#
# * ``design_draft`` — the driver research (model, sensitivity, protection
#   bands), any manually entered crossover settings, and the topology intent
#   the household spent time on. Losing it would force re-researching drivers
#   from scratch, not just re-measuring them.
# * ``baseline_profile`` — the SOLO applied Layer-A anchor: once a baseline
#   has been applied, this file's ``applied_recomposition_profile`` is the
#   sole durable record of the corrections (gain/delay/polarity) the SOLO
#   speaker is playing, read as the "what is currently applied" SSOT by
#   ``jasper.sound.graph_carrier`` and ``jasper-doctor`` (not just by this
#   flow). Clearing it would corrupt the solo applied graph's record.
#   Multiroom is a DIFFERENT story — keeping this file does NOT protect a
#   bonded speaker's group crossover, because the active-leader/follower
#   builders (``jasper/multiroom/active_leader_config.py`` /
#   ``follower_config.py``) never read it. They REBUILD the driver-domain
#   graph from ``design_draft`` (KEPT) plus ``crossover_preview`` +
#   ``measurements`` (both CLEARED) via
#   ``build_baseline_profile_candidate(driver_domain=True)``. So after a
#   scoped reset a bonded speaker's next re-prove sees empty measurements →
#   ``may_apply=False`` → ActiveLeaderError/ActiveFollowerError → the
#   grouping reconciler's readiness gate fails SAFE to solo-active (no mute,
#   no loud output; self-recovers when the household re-measures and
#   re-groups). The scoped reset therefore keeps the SOLO applied audio, but
#   a grouped speaker needs re-measurement before it can re-group — see the
#   grouping-aware "Start over" copy and docs/HANDOFF-correction.md
#   "Scoped crossover reset".
# * ``startup_load`` — the load/rollback bookkeeping for whichever protected
#   candidate CamillaDSP is CURRENTLY running. Hardware-verified on 2026-07-17
#   (JTS3): this file's ``previous_config_path`` is, at that moment, the ONLY
#   recorded path back to the config that was playing before the active-
#   speaker flow's muted candidate was loaded. Deleting it while
#   ``loaded=True`` would not stop any audio by itself, but it would strand
#   the rollback pointer, leaving no automated way back to the prior config.
#   A scoped "start over" must never risk that — see
#   docs/HANDOFF-correction.md "Scoped crossover reset".
_MEASUREMENT_JOURNEY_ARTIFACT_IDS = (
    "crossover_preview",
    "staged_config",
    "path_safety",
    "commission_load",
    "commission_ramp",
    "measurements",
)

ACTIVE_SPEAKER_MEASUREMENT_JOURNEY_RESET_KIND = (
    "jts_active_speaker_measurement_journey_reset"
)


def active_speaker_measurement_journey_paths() -> dict[str, Path]:
    """Return the scoped subset of ``active_speaker_setup_state_paths()``
    that is safe to clear without losing driver research or disturbing the
    currently-applied/currently-loaded audio graph."""

    all_paths = active_speaker_setup_state_paths()
    return {
        artifact_id: all_paths[artifact_id]
        for artifact_id in _MEASUREMENT_JOURNEY_ARTIFACT_IDS
    }


def _clear_paths(
    paths: dict[str, Path],
    *,
    kind: str,
    event: str,
) -> dict[str, Any]:
    cleared: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for artifact_id, path in paths.items():
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
        event,
        status=status,
        cleared=len(cleared),
        missing=len(missing),
        errors=len(errors),
        level=logging.WARNING if errors else logging.INFO,
    )
    return {
        "artifact_schema_version": 1,
        "kind": kind,
        "status": status,
        "cleared": cleared,
        "missing": missing,
        "errors": errors,
    }


def clear_active_speaker_setup_state() -> dict[str, Any]:
    """Remove stale active-speaker setup/evidence state after topology reset."""

    return _clear_paths(
        active_speaker_setup_state_paths(),
        kind=ACTIVE_SPEAKER_SETUP_RESET_KIND,
        event="active_speaker.setup_reset",
    )


def clear_active_speaker_measurement_journey() -> dict[str, Any]:
    """Clear ONLY the active-speaker measurement journey, in place.

    This is the scoped sibling of :func:`clear_active_speaker_setup_state`,
    for an in-flow "start over" that restarts the guided capture sequence
    without losing driver research or disturbing whatever audio graph is
    currently applied/loaded. See :data:`_MEASUREMENT_JOURNEY_ARTIFACT_IDS`
    for exactly what is cleared and why the rest is kept.

    Callers are responsible for the surrounding safety choreography this
    file does not own: stopping any in-flight relay/level-match session and
    invalidating the in-process comparison-set/level-lock lease BEFORE
    calling this (see ``jasper.web.correction_crossover_backend
    .reset_measurement_journey``), so no capture is silently orphaned mid-
    flight.
    """

    return _clear_paths(
        active_speaker_measurement_journey_paths(),
        kind=ACTIVE_SPEAKER_MEASUREMENT_JOURNEY_RESET_KIND,
        event="active_speaker.measurement_journey_reset",
    )


__all__ = [
    "ACTIVE_SPEAKER_MEASUREMENT_JOURNEY_RESET_KIND",
    "ACTIVE_SPEAKER_SETUP_RESET_KIND",
    "active_speaker_measurement_journey_paths",
    "active_speaker_setup_state_paths",
    "clear_active_speaker_measurement_journey",
    "clear_active_speaker_setup_state",
]
