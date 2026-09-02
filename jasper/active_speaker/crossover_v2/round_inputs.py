# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where one round's evidence inputs are, for the two shapes a round comes in.

A round's evidence packet is built from five things: the commissioning bundle,
and four files that live OUTSIDE it — the crossover-v2 flow state, the design
draft, the applied baseline profile, and the banked repeat floor. Where those
five are depends on
whether the round is still on the speaker or has been banked:

* **live**, on the box: the bundle IS the session directory under
  ``/var/lib/jasper/active_speaker/sessions/<id>``, and the other four are the
  on-Pi SSOT files their owning modules declare — read here from those owners,
  never re-spelled. The flow state is the one of the three that is a RECORD OF
  ONE SESSION rather than of the speaker, and a dozen session directories are
  retained against one state file, so it is handed over only to the session it
  names (:func:`_live_state_path`).
* **banked**, by ``scripts/bank-crossover-round.sh``: the bundle is copied to
  ``<round-dir>/bundle/<session>/`` and the same four are frozen beside it
  under fixed names.

Two readers wanted the same answer and had a shape each —
:func:`~.round_views.load_banked_round` took only a banked tree,
``jasper-crossover-prescriber`` only a live directory (#3498, #2882) — so the
shape is decided HERE, once, and both consume it. :attr:`RoundInputs.banked`
travels with the paths because it is the one thing a reader cannot re-derive
from them: a banked round's four siblings are frozen copies of what the
speaker was wearing, a live round's are the speaker's current files and move
under the reader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jasper.active_speaker.baseline_profile import (
    DEFAULT_STATE_PATH as APPLIED_PROFILE_DEFAULT_PATH,
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
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as REPEAT_FLOOR_DEFAULT_PATH,
)

__all__ = [
    "APPLIED_PROFILE_DEFAULT_PATH",
    "APPLIED_PROFILE_FILENAME",
    "DESIGN_DRAFT_FILENAME",
    "DRIVERS_DEFAULT_PATH",
    "REPEAT_FLOOR_DEFAULT_PATH",
    "REPEAT_FLOOR_FILENAME",
    "RoundInputs",
    "RoundViewsError",
    "STATE_DEFAULT_PATH",
    "STATE_FILENAME",
    "STATE_NOT_THIS_SESSION",
    "STATE_SESSION_UNKNOWN",
    "round_inputs",
]

#: The four names ``bank-crossover-round.sh`` writes beside the copied bundle.
STATE_FILENAME = "state.json"
DESIGN_DRAFT_FILENAME = "design-draft.json"
APPLIED_PROFILE_FILENAME = "applied-profile.json"
REPEAT_FLOOR_FILENAME = "repeat-floor.json"

#: Why a LIVE session resolves to no flow state. Codes rather than prose: they
#: are what a reader decides on, and they reach an operator through the
#: ``verify_pose.reason`` field the views already publish.
STATE_NOT_THIS_SESSION = "state_not_this_session"
STATE_SESSION_UNKNOWN = "state_session_unknown"


class RoundViewsError(CrossoverEvidencePacketError):
    """A round directory could not be read into a comparable view.

    A subclass rather than a sibling so the prescriber's one "the evidence
    could not be read" arm keeps catching every shape of that failure: a
    directory this resolver refuses is, in that tool's own vocabulary, not a
    crossover-v2 session bundle.
    """


@dataclass(frozen=True)
class RoundInputs:
    """The five paths one round's evidence packet is built from.

    The four optional paths are ``None`` for a banked round that did not bank
    that sibling, and :attr:`state_path` is also ``None`` for a live round
    whose flow state belongs to a different session
    (:func:`_live_state_path`) — absence the packet builder already reports
    inside the document rather than raising on. :attr:`state_reason` is the
    code for that second case and empty otherwise; a reader that only wants
    the paths can ignore it.
    """

    session_dir: Path
    state_path: Path | None
    design_draft_path: Path | None
    applied_profile_path: Path | None
    repeat_floor_path: Path | None
    banked: bool
    state_reason: str = ""


def _live_state_path(session_dir: Path) -> tuple[Path | None, str]:
    """The speaker's flow state, but only when it is THIS session's.

    There is ONE flow state on a speaker and up to twelve retained session
    directories, so a live bundle that is not the current round would
    otherwise be handed another round's verify curve, verdicts and ordinal —
    numbers that are wrong rather than missing. The two ids are compared in
    the one namespace they share: the state's own ``session_id`` is a RELAY
    id, and the bundle files its round artifacts under that same id
    (``evidence/v1/artifacts/crossover_v2/<relay-session-id>/``, which is what
    :func:`~.evidence_packet.round_artifact_dir` returns).

    A state file that is absent or unreadable is NOT refused here: nothing
    claims it belongs to another round, and the packet builder already reports
    an unreadable input inside the document it builds.
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
        return None, STATE_NOT_THIS_SESSION
    return STATE_DEFAULT_PATH, ""


def _sibling(round_dir: Path, name: str) -> Path | None:
    path = round_dir / name
    return path if path.is_file() else None


def round_inputs(path: Path) -> RoundInputs:
    """Resolve ``path`` — a banked round tree or a live session bundle.

    Raises :class:`RoundViewsError` when it is neither, naming both shapes:
    the two failures send an operator to different places, and a message that
    named only one would read as "bank this first" to somebody who had pointed
    at a session directory one level off.
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
            banked=False,
            state_reason=state_reason,
        )
    raise RoundViewsError(
        f"{path}: neither a banked round (no bundle/ directory) nor a live "
        f"session bundle (no info.json)"
    )
