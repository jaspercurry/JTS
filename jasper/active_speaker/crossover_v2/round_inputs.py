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
  never re-spelled.
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

from dataclasses import dataclass
from pathlib import Path

from jasper.active_speaker.baseline_profile import (
    DEFAULT_STATE_PATH as APPLIED_PROFILE_DEFAULT_PATH,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
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
    "STATE_FILENAME",
    "round_inputs",
    "state_default_path",
]

#: The four names ``bank-crossover-round.sh`` writes beside the copied bundle.
STATE_FILENAME = "state.json"
DESIGN_DRAFT_FILENAME = "design-draft.json"
APPLIED_PROFILE_FILENAME = "applied-profile.json"
REPEAT_FLOOR_FILENAME = "repeat-floor.json"


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

    The four optional paths are ``None`` only for a banked round that did not
    bank that sibling — absence the packet builder already reports inside the
    document rather than raising on. A live round always names all four: the
    SSOT file either exists on the speaker or its absence is reported the same
    way.
    """

    session_dir: Path
    state_path: Path | None
    design_draft_path: Path | None
    applied_profile_path: Path | None
    repeat_floor_path: Path | None
    banked: bool


def state_default_path() -> Path:
    """The flow state's on-Pi path, from the module that owns the FILE.

    A function rather than a module constant beside the other two because the
    owner is the web host: it imports this package, so importing it back at
    module scope would invert that direction for one path. Deferred instead,
    the way every other caller outside ``jasper.web`` reaches it (see
    ``jasper/cli/doctor/correction.py``, ``jasper/cli/null_door.py``).
    """
    from jasper.web.correction_crossover_v2 import DEFAULT_V2_STATE_PATH

    return DEFAULT_V2_STATE_PATH


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
        return RoundInputs(
            session_dir=path,
            state_path=state_default_path(),
            design_draft_path=DRIVERS_DEFAULT_PATH,
            applied_profile_path=APPLIED_PROFILE_DEFAULT_PATH,
            repeat_floor_path=REPEAT_FLOOR_DEFAULT_PATH,
            banked=False,
        )
    raise RoundViewsError(
        f"{path}: neither a banked round (no bundle/ directory) nor a live "
        f"session bundle (no info.json)"
    )
