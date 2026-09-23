# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A round's position in the household's series, read from durable state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jasper.output_topology import topology_config_fingerprint
from jasper.output_topology_store import load_output_topology


@dataclass(frozen=True)
class SeriesPosition:
    """Where the next round sits in the household's series."""

    #: 1-based position of the round about to be graded.
    ordinal: int
    ordinal_epoch: int = 0

    @classmethod
    def first(cls, *, ordinal_epoch: int = 0) -> "SeriesPosition":
        return cls(ordinal=1, ordinal_epoch=ordinal_epoch)


ROUND_ORDINAL_EPOCH_STATE_KEY = "round_ordinal_epoch"


def round_ordinal_epoch_from_state(raw: Any) -> int:
    """The ordinal sequence's epoch, or ``0`` for "no reset recorded".

    ``0`` for every unreadable shape: claiming a reset that did not happen is
    the direction that fabricates. ``bool`` is rejected before ``int`` because
    it subclasses it — a hand-edited ``true`` must not publish as epoch 1.
    """

    if not isinstance(raw, Mapping):
        return 0
    epoch = raw.get(ROUND_ORDINAL_EPOCH_STATE_KEY)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return 0
    return epoch


def series_position_from_state(raw: Any) -> SeriesPosition:
    """Resolve the next round's series position from durable journey state.

    Every unreadable shape resolves to the FIRST round.
    """

    epoch = round_ordinal_epoch_from_state(raw)
    receipt = raw.get("round_receipt") if isinstance(raw, Mapping) else None
    if not isinstance(receipt, Mapping):
        return SeriesPosition.first(ordinal_epoch=epoch)
    previous_ordinal = receipt.get("round_ordinal")
    if not isinstance(previous_ordinal, int) or isinstance(previous_ordinal, bool):
        return SeriesPosition.first(ordinal_epoch=epoch)
    if previous_ordinal < 1:
        return SeriesPosition.first(ordinal_epoch=epoch)
    # #2704: a topology/driver change invalidates the series. A receipt from
    # before this fingerprint existed carries no claim either way, so only a
    # STORED-AND-DIFFERENT fingerprint resets the series; an absent one is read
    # as "unknown", not as a mismatch.
    stored_fingerprint = receipt.get("topology_fingerprint")
    if (
        isinstance(stored_fingerprint, str) and stored_fingerprint
        and stored_fingerprint != topology_config_fingerprint(load_output_topology())
    ):
        return SeriesPosition.first(ordinal_epoch=epoch)
    return SeriesPosition(ordinal=previous_ordinal + 1, ordinal_epoch=epoch)
