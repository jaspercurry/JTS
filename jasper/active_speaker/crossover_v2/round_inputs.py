# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve one round's captures, matching state and separate bank-time context.

The capture owner's snapshot lives in the bundle. Legacy live or banked state
is usable only when its capture ID matches the round's artifact directory.
Design, applied profile, repeat floor and declared geometry retain their own
current/bank-time meanings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from jasper.json_fields import finite_float
from jasper.active_speaker import bundles
from jasper.active_speaker.candidate_bank import _candidate_roots
from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

from jasper.active_speaker.state_paths import (
    DEFAULT_BASELINE_PROFILE_STATE_PATH as APPLIED_PROFILE_DEFAULT_PATH,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    DEFAULT_V2_STATE_PATH as STATE_DEFAULT_PATH,
)
from jasper.active_speaker.design_draft import (
    DEFAULT_DESIGN_DRAFT_PATH as DRIVERS_DEFAULT_PATH,
)
from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH as _DECLARED_GEOMETRY_DEFAULT_PATH,
)
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as REPEAT_FLOOR_DEFAULT_PATH,
)

__all__ = [
    "APPLIED_PROFILE_DEFAULT_PATH",
    "APPLIED_PROFILE_FILENAME",
    "CAPTURE_STATE_FILENAME",
    "DECLARED_GEOMETRY_DEFAULT_PATH",
    "DECLARED_GEOMETRY_FILENAME",
    "DESIGN_DRAFT_FILENAME",
    "DRIVERS_DEFAULT_PATH",
    "REPEAT_FLOOR_DEFAULT_PATH",
    "REPEAT_FLOOR_FILENAME",
    "RoundInputs",
    "RoundViewsError",
    "STATE_DEFAULT_PATH",
    "STATE_FILENAME",
    "STATE_SESSION_UNKNOWN",
    "banked_round_of",
    "iter_round_sessions",
    "matching_state_path",
    "recent_round_sessions",
    "state_matches_capture",
    "round_inputs",
]

#: The five names ``bank-crossover-round.sh`` writes beside the copied bundle.
STATE_FILENAME = "state.json"
CAPTURE_STATE_FILENAME = "crossover-v2-state.json"
DESIGN_DRAFT_FILENAME = "design-draft.json"
APPLIED_PROFILE_FILENAME = "applied-profile.json"
REPEAT_FLOOR_FILENAME = "repeat-floor.json"
DECLARED_GEOMETRY_FILENAME = "declared-geometry.json"

#: The household's declared rig geometry; single writer
#: ``jasper-declare-geometry set``.
DECLARED_GEOMETRY_DEFAULT_PATH = Path(_DECLARED_GEOMETRY_DEFAULT_PATH)

STATE_SESSION_UNKNOWN = "state_session_unknown"


class CrossoverEvidencePacketError(ValueError):
    """The named directory is not a crossover-v2 session bundle."""


NO_ROUND_ARTIFACTS_REASON = "no crossover_v2 round artifacts under evidence/v1"


def round_artifact_dir(session_dir: Path) -> tuple[Path | None, str]:
    matches = sorted(
        path for path in session_dir.glob(f"{EVIDENCE_ROOT}/artifacts/crossover_v2/*")
        if path.is_dir()
    )
    if not matches:
        return None, NO_ROUND_ARTIFACTS_REASON
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        return None, f"bundle carries more than one round ({names})"
    return matches[0], ""


class RoundViewsError(CrossoverEvidencePacketError):
    """A round directory could not be read into a comparable view.

    A subclass of :class:`CrossoverEvidencePacketError` so the prescriber's one
    "the evidence could not be read" arm keeps catching every shape of it.
    """


@dataclass(frozen=True)
class RoundInputs:
    """Paths for a round; unavailable matching state carries a reason code."""

    session_dir: Path
    state_path: Path | None
    design_draft_path: Path | None
    applied_profile_path: Path | None
    repeat_floor_path: Path | None
    declared_geometry_path: Path | None
    banked: bool
    state_reason: str = ""


def state_matches_capture(state: object, capture_id: str) -> bool:
    return isinstance(state, Mapping) and state.get("session_id") == capture_id


def matching_state_path(
    session_dir: Path, fallback: Path | None,
) -> tuple[Path | None, str]:
    """Prefer the capture owner's snapshot; accept older state only by capture ID."""
    round_dir, _reason = round_artifact_dir(session_dir)
    if round_dir is None:
        return None, STATE_SESSION_UNKNOWN
    reason = ""
    for path in (session_dir / CAPTURE_STATE_FILENAME, fallback):
        if path is None or not path.is_file():
            continue
        try:
            state = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, ValueError):
            reason = STATE_SESSION_UNKNOWN
            continue
        if state_matches_capture(state, round_dir.name):
            return path, ""
        reason = STATE_SESSION_UNKNOWN
    return None, reason


def _sibling(round_dir: Path, name: str) -> Path | None:
    path = round_dir / name
    return path if path.is_file() else None


def round_inputs(path: Path) -> RoundInputs:
    """Resolve ``path`` — a banked round tree or a live session bundle.

    Raises :class:`RoundViewsError` when it is neither, naming both shapes.
    """
    path = Path(path)
    bundle_dir = path / "bundle"
    if bundle_dir.is_dir():
        children = sorted(child for child in bundle_dir.iterdir() if child.is_dir())
        if len(children) != 1:
            raise RoundViewsError(
                f"{bundle_dir}: expected exactly one session directory, "
                f"found {len(children)}"
            )
        state_path, state_reason = matching_state_path(children[0], _sibling(path, STATE_FILENAME))
        return RoundInputs(
            session_dir=children[0],
            state_path=state_path,
            design_draft_path=_sibling(path, DESIGN_DRAFT_FILENAME),
            applied_profile_path=_sibling(path, APPLIED_PROFILE_FILENAME),
            repeat_floor_path=_sibling(path, REPEAT_FLOOR_FILENAME),
            declared_geometry_path=_sibling(path, DECLARED_GEOMETRY_FILENAME),
            banked=True,
            state_reason=state_reason,
        )
    if (path / "info.json").is_file():
        state_path, state_reason = matching_state_path(path, STATE_DEFAULT_PATH)
        return RoundInputs(
            session_dir=path,
            state_path=state_path,
            design_draft_path=DRIVERS_DEFAULT_PATH,
            applied_profile_path=APPLIED_PROFILE_DEFAULT_PATH,
            repeat_floor_path=REPEAT_FLOOR_DEFAULT_PATH,
            declared_geometry_path=DECLARED_GEOMETRY_DEFAULT_PATH,
            banked=False,
            state_reason=state_reason,
        )
    raise RoundViewsError(
        f"{path}: neither a banked round (no bundle/ directory) nor a live "
        f"session bundle (no info.json)"
    )


def banked_round_of(session_dir: Path) -> Path | None:
    """The bank containing this bundle, if it resolves back to the same bundle."""
    candidate = session_dir.parent.parent
    try:
        inputs = round_inputs(candidate)
        return candidate if inputs.banked and inputs.session_dir == session_dir else None
    except RoundViewsError:
        return None


def iter_round_sessions(session_dir: Path) -> Iterator[Path]:
    """Search retained stores without a recent window or a materialized history."""
    from jasper.active_speaker.candidate_bank import _candidate_roots, _directories  # lazy: bank imports

    bank = banked_round_of(session_dir)
    root = bank.parent if bank else session_dir.parent
    for store in _candidate_roots(root):
        for directory in _directories(store):
            try:
                yield round_inputs(directory).session_dir
            except (OSError, CrossoverEvidencePacketError):
                continue


def recent_round_sessions(session_dir: Path | None = None, *, limit: int = 32) -> list[Path]:
    """Read a bounded window from adjacent live and campaign stores.

    Directory times select the window; capture times order the evidence.
    Duplicate bundle ids are read from the requested store first.
    No selected session uses the configured live and adjacent campaign stores.
    """
    bank = banked_round_of(session_dir) if session_dir is not None else None
    root = (bank.parent if bank else session_dir.parent) if session_dir else bundles.sessions_dir()
    sessions: dict[str, tuple[float, Path]] = {}
    for store in _candidate_roots(root):
        if not store.is_dir():
            continue
        directories = sorted(
            (path for path in store.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )[:max(0, limit)]
        for directory in directories:
            try:
                bundle = round_inputs(directory).session_dir
                info = json.loads((bundle / "info.json").read_text())
                if not isinstance(info, dict):
                    continue
            except (OSError, ValueError, CrossoverEvidencePacketError):
                continue
            sessions.setdefault(str(info.get("session_id") or bundle.name), (
                finite_float(info.get("started_at")) or 0.0, bundle,
            ))
    return [bundle for _started_at, bundle in sorted(sessions.values(), reverse=True)][:max(0, limit)]
