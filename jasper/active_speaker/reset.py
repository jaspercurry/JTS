# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reset helpers for active-speaker setup evidence.

These helpers clear durable wizard/evidence state after the saved output
topology is reset to unconfigured. They intentionally do not delete generated
CamillaDSP YAML files: those are inert without the state/evidence JSON and can
be useful for forensics, while a loaded runtime graph is reconciled separately.

One artifact is not a lone file — ``staged_config`` is the METADATA half of the
staged startup-anchor pair — so its unlink alone runs under that pair's
cross-process lock (#2518/#2590). See :func:`_staged_anchor_unlink_guard`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

from .commission_ramp import ramp_state_path
from .crossover_preview import crossover_preview_path
from .design_draft import DEFAULT_DESIGN_DRAFT_PATH, DESIGN_DRAFT_PATH_ENV
from .measurement import measurement_state_path
from .path_safety import path_safety_evidence_path
from .staging import (
    StagedAnchorLockContended,
    staged_anchor_lock,
    staged_config_path,
    staged_metadata_path,
)
from .state_paths import (
    baseline_profile_state_path,
    commission_load_state_path,
    startup_load_state_path,
)

logger = logging.getLogger(__name__)

ACTIVE_SPEAKER_SETUP_RESET_KIND = "jts_active_speaker_setup_reset"

# The one artifact below that is HALF OF A PAIR: `staged_config` is the staged
# startup anchor's METADATA half, whose GRAPH half stays on disk. #2518 gave
# that pair a cross-process lock, and a reset racing a stage without it can lose
# the reset or leave the pair torn. See :func:`_staged_anchor_unlink_guard`.
_STAGED_ANCHOR_ARTIFACT_ID = "staged_config"
_STAGED_ANCHOR_LOCK_SOURCE = "active_speaker_reset"


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


# Pure MEASUREMENT-JOURNEY evidence, safe to clear so a household can restart
# the guided capture flow. Deliberately EXCLUDED, unlike the nuclear
# ``active_speaker_setup_state_paths()`` above:
#
# * ``design_draft`` — the driver research and topology intent; losing it forces
#   re-researching drivers, not just re-measuring them.
# * ``baseline_profile`` — the SOLO applied Layer-A anchor and the sole durable
#   record of the corrections the speaker is playing, read as the applied SSOT
#   by ``jasper.sound.graph_carrier`` and ``jasper-doctor``. It does NOT protect
#   a bonded speaker: the multiroom builders rebuild the driver-domain graph
#   from ``design_draft`` plus the CLEARED ``crossover_preview`` +
#   ``measurements``, so after a scoped reset a grouped speaker fails SAFE to
#   solo-active and needs re-measurement before it can re-group.
# * ``startup_load`` — the load/rollback bookkeeping for the protected candidate
#   CamillaDSP is CURRENTLY running. Its ``previous_config_path`` is the only
#   recorded path back to the config that was playing before, so deleting it
#   while ``loaded=True`` strands the rollback pointer.
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


@contextmanager
def _staged_anchor_unlink_guard(artifact_id: str) -> Iterator[None]:
    """Hold the staged startup anchor's pair lock — for ONE unlink only.

    Scoped to the ``staged_config`` artifact and nothing else: holding it across
    the whole clear loop would let one contending stage block eight unrelated
    deletions.

    :func:`~jasper.active_speaker.staging.staged_anchor_lock` keyed on the GRAPH
    half's path (``staged_config_path()``) — the same key both writers pass,
    which is what makes this the same lock rather than a second one. Its
    contract is inherited unchanged: bounded wait, then
    :class:`StagedAnchorLockContended`, and fail-OPEN at WARNING on a lock file
    that cannot be opened. The caller turns a contention into this artifact's
    ``errors`` entry, the existing ``status="partial"`` refusal.
    """

    if artifact_id != _STAGED_ANCHOR_ARTIFACT_ID:
        yield
        return
    with staged_anchor_lock(
        staged_config_path(), source=_STAGED_ANCHOR_LOCK_SOURCE
    ):
        yield


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
            with _staged_anchor_unlink_guard(artifact_id):
                path.unlink()
        # `StagedAnchorLockContended` is a RuntimeError, so the `OSError` arm
        # below would not catch it. A contended pair is reported as this
        # artifact's error and the loop continues.
        except StagedAnchorLockContended as exc:
            errors.append({
                "id": artifact_id,
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            })
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

    The scoped sibling of :func:`clear_active_speaker_setup_state`: restarts the
    guided capture sequence without losing driver research or disturbing the
    applied/loaded graph. :data:`_MEASUREMENT_JOURNEY_ARTIFACT_IDS` says exactly
    what is cleared and why the rest is kept.

    Callers own the surrounding choreography: stop any in-flight
    capture/level-match session and invalidate the comparison-set/level-lock lease
    BEFORE calling this, so no capture is orphaned mid-flight.
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
