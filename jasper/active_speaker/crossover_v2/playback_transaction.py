# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The play transaction contract: ready → admit → lock → play → restore, for
one stimulus. :mod:`.program_transaction` is the implementation (a named
boundary inside ``measure``, not a fifth verb — See ADR-0228 #1). ``run``
returns the capture's path because a host that plays and records on one box
holds the recording INSIDE this transaction; a remote microphone answers
through the capture-source seam instead. ``ready`` carries the mic-position
precondition, so nothing below this seam branches on who moved the mic (See
ADR-0228 #8). Stage vocabulary is engine-internal, never reaching a front end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .measure_spec import MeasureSpec

__all__ = [
    "PLAYBACK_STAGES",
    "STAGE_ADMIT",
    "STAGE_LOCK",
    "STAGE_PLAY",
    "STAGE_READY",
    "STAGE_RESTORE",
    "PlaybackOutcome",
    "PlaybackTransaction",
]

#: The mover-agnostic mic-position precondition is satisfied here and nowhere
#: else. See ADR-0228 #8.
STAGE_READY = "ready"
#: A stimulus enters pre-DSP, never the post-crossover active ring (ADR-0231 §3).
STAGE_ADMIT = "admit"
#: The declared level is held for this stimulus. Proving the fader agrees is the
#: session's volume-claim slot's job, taken before this transaction is called
#: (MS-14), and an unproven level still reaches this stage.
STAGE_LOCK = "lock"
#: The stimulus plays.
STAGE_PLAY = "play"
#: Whatever this transaction changed is put back.
STAGE_RESTORE = "restore"

#: In order.
PLAYBACK_STAGES = (
    STAGE_READY,
    STAGE_ADMIT,
    STAGE_LOCK,
    STAGE_PLAY,
    STAGE_RESTORE,
)

#: Stage → its position in the run.
_STAGE_RANK = {stage: rank for rank, stage in enumerate(PLAYBACK_STAGES)}
_PLAY_RANK = _STAGE_RANK[STAGE_PLAY]

#: A reason code in the spelling ``REASON_REGISTRY`` and :mod:`.refusal_copy`
#: use: a lower-case slug. Checked by shape, not by membership — a mechanical
#: failure the household vocabulary has no copy for must still be reportable.
_INCIDENT_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class PlaybackOutcome:
    """What one stimulus did, said in facts a record can carry.

    ``stage_reached`` is the last stage this transaction COMPLETED, in
    :data:`PLAYBACK_STAGES` order — a run that admitted, failed to play and
    restored correctly reports :data:`STAGE_ADMIT`, not :data:`STAGE_RESTORE`.
    ``incident`` is a reason code, ``""`` for none, never a sentence. No level
    is carried: the fader reading is the session's volume claim's to report.
    ``wav_path`` is carried because it has no second copy — the bundle-relative
    path is not derivable from the take id
    (``bundles.capture_artifact_relpath`` appends a ``uuid4`` hex). An empty
    ``wav_path`` on a stimulus that played must name an incident.
    """

    stage_reached: str
    incident: str = ""
    wav_path: str = ""

    def __post_init__(self) -> None:
        if self.stage_reached not in _STAGE_RANK:
            raise ValueError(
                f"a playback stage must be one of {PLAYBACK_STAGES}, "
                f"got {self.stage_reached!r}"
            )
        if self.incident and not _INCIDENT_CODE.match(self.incident):
            raise ValueError(
                "a playback incident is a reason code, not a sentence, got "
                f"{self.incident!r}"
            )

    @property
    def played(self) -> bool:
        """Did the stimulus actually play? True exactly when ``play`` completed."""
        return _STAGE_RANK[self.stage_reached] >= _PLAY_RANK


class PlaybackTransaction(Protocol):
    """Ready → admit → lock → play → restore, for ONE stimulus.

    Structural, and deliberately not ``@runtime_checkable``: ``isinstance``
    against a Protocol compares method NAMES only, so it would buy confidence it
    cannot deliver. The body raises :class:`NotImplementedError` so a partial
    explicit subclass fails loudly.

    ``spec`` says what to play; a body composes it THROUGH :mod:`.programs`,
    which owns the only level clamp on the summed sweep. ``position_deg`` is the
    bearing in :class:`~.spatial.PositionGeometry`'s frame, ``None`` where the
    pose commands none; ``prompt`` is what the mover was told, ``""`` where none
    was issued. ``level_db`` is the session's one declared fader level;
    ``stimulus_dbfs`` is this rung's stimulus level, ``None`` for the program's
    own — a ladder moves the stimulus and never the claim (See ADR-0228 #6).

    Never raises for a measurement problem: an exception here would strand the
    session and lose the restore. An exception remains correct for a programming
    error.
    """

    async def run(
        self,
        *,
        spec: "MeasureSpec",
        position_deg: int | None,
        prompt: str,
        level_db: float,
        stimulus_dbfs: float | None,
    ) -> PlaybackOutcome:
        raise NotImplementedError
