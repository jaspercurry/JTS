# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where one round's evidence inputs are, for the two shapes a round comes in.

A round's packet is built from the commissioning bundle plus five siblings: the
flow state, the design draft, the applied baseline profile, the repeat floor
and the declared rig geometry. **live**: the bundle IS the session directory
and the five are the on-Pi SSOT paths their owning modules declare. **banked**:
the bundle is copied to ``<round-dir>/bundle/<session>/`` and the five are
frozen beside it under the filenames below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jasper.active_speaker.state_paths import (
    DEFAULT_BASELINE_PROFILE_STATE_PATH as APPLIED_PROFILE_DEFAULT_PATH,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    DEFAULT_V2_STATE_PATH as STATE_DEFAULT_PATH,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    round_artifact_dir,
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
    "round_inputs",
]

#: The five names ``bank-crossover-round.sh`` writes beside the copied bundle.
STATE_FILENAME = "state.json"
DESIGN_DRAFT_FILENAME = "design-draft.json"
APPLIED_PROFILE_FILENAME = "applied-profile.json"
REPEAT_FLOOR_FILENAME = "repeat-floor.json"
DECLARED_GEOMETRY_FILENAME = "declared-geometry.json"

#: The household's declared rig geometry; single writer
#: ``jasper-declare-geometry set``.
DECLARED_GEOMETRY_DEFAULT_PATH = Path(_DECLARED_GEOMETRY_DEFAULT_PATH)

#: Why a LIVE session resolves to no flow state. One slug for every such case;
#: it reaches an operator through the published ``verify_pose.reason`` field.
STATE_SESSION_UNKNOWN = "state_session_unknown"


class RoundViewsError(CrossoverEvidencePacketError):
    """A round directory could not be read into a comparable view.

    A subclass of :class:`CrossoverEvidencePacketError` so the prescriber's one
    "the evidence could not be read" arm keeps catching every shape of it.
    """


@dataclass(frozen=True)
class RoundInputs:
    """The six paths one round's evidence packet is built from.

    A path is ``None`` where a banked round did not bank that sibling, and
    :attr:`state_path` is also ``None`` for a live round whose flow state
    belongs to another session — :attr:`state_reason` carries the code for that
    second case and is empty otherwise.
    """

    session_dir: Path
    state_path: Path | None
    design_draft_path: Path | None
    applied_profile_path: Path | None
    repeat_floor_path: Path | None
    declared_geometry_path: Path | None
    banked: bool
    state_reason: str = ""


def _live_state_path(session_dir: Path) -> tuple[Path | None, str]:
    """The speaker's flow state, but only when it is THIS session's.

    One flow state exists per speaker against up to twelve retained session
    directories. The two ids are compared in the one namespace they share: the
    state's ``session_id`` is a CAPTURE id, and the bundle files its round
    artifacts under that same id (:func:`~.evidence_packet.round_artifact_dir`).
    An absent or unreadable state file is not refused here — nothing claims it
    belongs to another round.
    """
    round_dir, _reason = round_artifact_dir(session_dir)
    if round_dir is None:
        return None, STATE_SESSION_UNKNOWN
    try:
        state = json.loads(STATE_DEFAULT_PATH.read_text())
    except (OSError, UnicodeDecodeError, ValueError):
        return STATE_DEFAULT_PATH, ""
    if not isinstance(state, Mapping):
        return STATE_DEFAULT_PATH, ""
    session_id = state.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None, STATE_SESSION_UNKNOWN
    if session_id != round_dir.name:
        return None, STATE_SESSION_UNKNOWN
    return STATE_DEFAULT_PATH, ""


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
        return RoundInputs(
            session_dir=children[0],
            state_path=_sibling(path, STATE_FILENAME),
            design_draft_path=_sibling(path, DESIGN_DRAFT_FILENAME),
            applied_profile_path=_sibling(path, APPLIED_PROFILE_FILENAME),
            repeat_floor_path=_sibling(path, REPEAT_FLOOR_FILENAME),
            declared_geometry_path=_sibling(path, DECLARED_GEOMETRY_FILENAME),
            banked=True,
        )
    if (path / "info.json").is_file():
        state_path, state_reason = _live_state_path(path)
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
