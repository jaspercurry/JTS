# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The three lifetime seams a tuning session opens once — volume owner, session
graph, capture record — plus the play transaction, as Protocol contracts.

Vocabulary only: no provider lives here. Deliberately not
``@runtime_checkable`` (see :class:`~.playback_transaction.PlaybackTransaction`);
method bodies raise :class:`NotImplementedError` so a partial explicit subclass
fails loudly. See ADR-0179 for why every seam verb is ``async``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .playback_transaction import PlaybackTransaction

__all__ = [
    "EngineSeams",
    "RecordStore",
    "SessionGraph",
    "VolumeClaim",
]


class SessionGraph(Protocol):
    """One measurement graph, installed once and patched per candidate.

    The install must be role-routed, crossover-free and per-driver protected
    (tweeter high-pass plus soft-clip limiter on the tweeter output channels)
    at once, and must pass the emitter's ``_assert_program_graph_proven`` once
    before the first stimulus. Every ``dataclasses.fields(ActiveEmitDevices)``
    field must be derived from ``active_emit_devices(...)`` and forwarded.
    """

    async def install(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        """Install the graph and return its fingerprint.

        Flips, delays and trims are install-time, not patch-time: the
        fingerprint must name the graph the stimulus actually played through.
        The fingerprint is provenance, never a gate — a host that cannot name
        the graph returns ``""``. May raise; the session then treats nothing as
        installed and still calls :meth:`restore`.
        """
        raise NotImplementedError

    async def patch(self, changes: Mapping[str, Any]) -> None:
        """Change what one candidate needs, without re-installing."""
        raise NotImplementedError

    async def restore(self) -> None:
        """Put back whatever the install displaced.

        Idempotent, a no-op when nothing is installed, and safe after an
        :meth:`install` that raised or a first :meth:`restore` that raised.
        """
        raise NotImplementedError


class VolumeClaim(Protocol):
    """The session's one hold on the fader — the session-measurement claim.

    One declared level for the whole session; a level ladder moves the
    stimulus, never this claim. ``devices.volume_limit`` stays ``0.0`` and
    ``CamillaController.set_volume_db`` clamps positive writes — this seam is
    not an exception to that.
    """

    async def acquire(self, level_db: float) -> None:
        """Take the session-measurement claim at the declared level.

        May raise after registering the claim internally; :meth:`release` is
        called regardless.
        """
        raise NotImplementedError

    async def prove(self) -> float | None:
        """The fader reading, but only when it agrees with the declared level.

        Returns the reading when it is within
        :data:`~jasper.active_speaker.volume_latch.READBACK_TOLERANCE_DB` of the
        acquired level (via
        :func:`~jasper.active_speaker.volume_latch.fader_matches`), else
        ``None`` — unreadable and preempted both read as ``None``, which
        refuses to bank the capture. Called once per stimulus, not once per
        spec: a claim can be preempted between two positions of one walk.
        """
        raise NotImplementedError

    async def release(self) -> None:
        """Give the fader back.

        Idempotent, a no-op when nothing is held, and safe after an
        :meth:`acquire` that raised or a first :meth:`release` that raised.
        """
        raise NotImplementedError


class RecordStore(Protocol):
    """Where this session's evidence lands — one shape, one writer.

    Every record kind folds onto :meth:`bank`, discriminated by the record's
    own ``kind``. Write-only since ADR-0198: reading a bank back belongs to the
    doors-and-banks tools. An integrity refusal still banks.
    """

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Write one record; return the id that finds it again."""
        raise NotImplementedError


@dataclass(frozen=True)
class EngineSeams:
    """Everything a :class:`~.session.TuningSession` needs from outside itself.

    Engine-internal: only :class:`~.session.TuningSession` calls through these
    fields. A front end reaching ``session.seams.records.bank(...)`` would bank
    a record the session never counts in
    :attr:`~.session.TuningSession.banked_record_ids`.
    """

    graph: SessionGraph
    volume: VolumeClaim
    records: RecordStore
    play: PlaybackTransaction
