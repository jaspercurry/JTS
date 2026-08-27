# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ``Recommender`` seam filled: banked ids in, evidence and spool out.

:data:`~jasper.active_speaker.crossover_v2.session_seams.Recommender` is
``Callable[[Sequence[str]], Awaitable[Mapping[str, Any]]]`` — its whole input
is the ids a session banked. ``docs/REFACTOR-CUTOVER-2026-08.md`` §3 settles
what that can honestly reach: the **read-only** half of the prescriber, because
that half IS a function of the bank. ``propose`` and ``stage`` are not — they
consume a model's answer document the engine never holds — so a seam of this
signature cannot reach them, and this adapter does not pretend to.

**Composed, never re-extracted.** ``session_seams`` carries the standing order
in as many words, and it is kept literally: the two doors called here are
:func:`~.crossover_prescriber.status_document` and
:func:`~jasper.active_speaker.crossover_v2.evidence_packet.build_crossover_evidence_packet`,
both already shipped and already returning values. What this module adds is the
arity — one bundle, one call, one mapping — and nothing else.

**Why it lives beside the prescriber rather than in the engine package.**
``status_document`` is defined in :mod:`.crossover_prescriber`, and the standing
order forbids moving it. That fixes the layer: a
``crossover_v2`` module importing this one would put the engine's domain package
on a front end, the one direction the strangler destination exists to refuse
(``tests/test_crossover_v2_verification`` pins it for the modules already
there). Here the imports run downward only — a sibling in this package, and the
packet builder in the domain below it — which is the direction
``crossover_prescriber`` itself already takes. The engine reaches this the way
it reaches every side effect: injected at construction, never imported.

**Reading the spool without consuming it.** ``status_document`` composes its
staged section from ``staged_prescription_pending()``, which answers *"is a
prescription waiting"* and leaves it waiting. That is the whole reason the
recommendation can be asked twice: a ``take`` here would hand the first caller
the prescription and tell the second there is none.

**The binding is NOT here.** Nothing in this module constructs an
``EngineSeams``; the wave that wires the session to a front end owns that site.
This ships the callable and its pins.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
    round_artifact_dir,
)
from .crossover_prescriber import status_document

__all__ = ["BankedRoundRecommender"]


def _relay_of(record_id: str) -> str:
    """The round a banked id names, read positionally as its second segment.

    The store files every artifact at ``<root>/<relay>/…`` and
    :func:`~jasper.active_speaker.crossover_v2.evidence_packet.round_artifact_dir`
    resolves ``<root>/<relay>`` inside the bundle and reports ``<relay>`` as
    that directory's name — so an id's second segment and that name are the
    same string, which is the whole id-to-directory mapping.

    Read by position rather than against a spelling of ``<root>``: the root
    literal already has three authors in this tree, and a fourth here would be
    the one that drifts. ``""`` for an id shaped like nothing this store mints.
    """
    parts = record_id.split("/")
    return parts[1] if len(parts) > 2 and parts[0] and parts[1] else ""


class BankedRoundRecommender:
    """One bundle's evidence and spool state, as the seam's return value.

    Construct with the bundle the session banks into — in production the
    directory
    :attr:`~jasper.active_speaker.commissioning_evidence_store.CommissioningEvidenceStore.bundle_dir`
    reports for the store handed to the same session, so the ids and this
    directory are two views of one round by construction.

    ``state_path`` is the crossover-v2 flow state, banked separately from the
    bundle; the other two optional paths are the packet builder's own and are
    passed through unchanged. Whether a state was supplied is a fact
    ``status_document`` reports on, so it is carried rather than inferred.
    """

    def __init__(
        self,
        bundle_dir: Path,
        *,
        state_path: Path | None = None,
        driver_draft_path: Path | None = None,
        dump_ring_dir: Path | None = None,
    ) -> None:
        self._bundle_dir = Path(bundle_dir)
        self._state_path = state_path
        self._driver_draft_path = driver_draft_path
        self._dump_ring_dir = dump_ring_dir

    async def __call__(self, record_ids: Sequence[str]) -> Mapping[str, Any]:
        """What should happen next, over exactly the records named.

        The whole composition runs in one worker thread: both doors are
        blocking file I/O — the packet walks a bundle, the spool reads a file
        on the speaker — and the seam's caller is the measurement walk, which
        runs ON the correction loop.
        """
        return await asyncio.to_thread(self._recommend, tuple(record_ids))

    # --------------------------------------------------------------- internals

    def _recommend(self, record_ids: tuple[str, ...]) -> Mapping[str, Any]:
        packet, packet_error = self._packet(record_ids)
        return {
            "packet": packet,
            **status_document(
                packet, packet_error, state_supplied=self._state_path is not None,
            ),
        }

    def _packet(
        self, record_ids: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, str]:
        """The evidence for this round, or the reason there is none.

        Fail-soft in ``status_document``'s own shape and with its exact
        exception set: an unreadable bundle becomes every evidence section's
        reason while the spool is still reported truthfully, because a
        prescription waiting is a fact about this speaker whichever directory
        was named.
        """
        mismatch = self._round_mismatch(record_ids)
        if mismatch:
            return None, mismatch
        try:
            return build_crossover_evidence_packet(
                self._bundle_dir,
                state_path=self._state_path,
                driver_draft_path=self._driver_draft_path,
                dump_ring_dir=self._dump_ring_dir,
            ), ""
        except (CrossoverEvidencePacketError, OSError) as exc:
            return None, str(exc)

    def _round_mismatch(self, record_ids: tuple[str, ...]) -> str:
        """Why these ids are not this bundle's round, or ``""`` when they are.

        Fails closed rather than grading a proposal against the wrong round —
        the refusal ``round_artifact_dir`` already makes for a bundle carrying
        two of them, applied one step earlier to the ids themselves. Ids naming
        no round at all (``persist``'s state ids never reach here, but an empty
        walk does) assert nothing, so they refuse nothing.
        """
        named = {relay for relay in map(_relay_of, record_ids) if relay}
        if not named:
            return ""
        if len(named) > 1:
            return (
                "banked ids name more than one round "
                f"({', '.join(sorted(named))})"
            )
        resolved, why = round_artifact_dir(self._bundle_dir)
        if resolved is None:
            return why
        if resolved.name not in named:
            return (
                f"banked ids name round {named.pop()!r}, but the bundle "
                f"carries {resolved.name!r}"
            )
        return ""
