# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a CamillaDSP graph's ``chunksize`` / ``target_level`` / ``queuelimit``.

Sits above :mod:`jasper.camilla_config_contract`, which owns the vocabulary and
stays a leaf. Resolution is not vocabulary: it reads the process environment and
the transport the graph's governing device belongs to. Emitters take the
constants from the contract and the resolver from here.
"""

from __future__ import annotations

import logging
import os

from jasper.camilla_config_contract import (
    DEFAULT_CHUNKSIZE,
    DEFAULT_QUEUELIMIT,
    DEFAULT_TARGET_LEVEL,
)
from jasper.env_load import bounded_env_int
from jasper.fanin_coupling import RING_CAMILLA_GEOMETRY, RING_PCM_DEVICES
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

_OPERATOR_KNOBS = ("JASPER_CAMILLA_CHUNKSIZE", "JASPER_CAMILLA_TARGET_LEVEL")
# CamillaDSP's own bound on chunksize; target_level shares it as a sanity cap.
_KNOB_MAX = 1 << 20


def resolve_camilla_latency_for_devices(
    *,
    capture_device: str,
    playback_device: str | None,
    chunksize: int | None = None,
    target_level: int | None = None,
    queuelimit: int | None = None,
) -> tuple[int, int, int]:
    """The ``(chunksize, target_level, queuelimit)`` a graph between these needs.

    A caller value passed here is returned untouched; only what was left
    ``None`` is resolved.

    ONE OWNER PER TRANSPORT. Since ADR-0100 a graph with a ring end has its
    buffering decided by THE RING, not by the box: the ring's capacity is a
    compile-time constant of the fan-in writer and the ioplug, identical on
    every box and unrelated to which DAC is fitted. A chunk larger than that
    cannot be negotiated at all — CamillaDSP exits ("Trying to set avail_min to
    N, must be smaller than or equal to device buffer size of 256") and systemd
    restart-loops it, which is silent deafness (AGENTS.md #6) — and the other
    two fields are certified WITH the chunk, never mixed across owners. So a
    ring end takes :data:`~jasper.fanin_coupling.RING_CAMILLA_GEOMETRY` whole,
    the same values the two end-to-end ring graphs pass explicitly. Everything
    else takes the operator env or the shipped defaults.

    ``playback_device=None`` is a CLOCKLESS sink (a ``File`` — the bonded
    leader's snapserver FIFO, the parked graph's ``/dev/null``): it declares no
    ALSA buffer, so a ring capture is the only ALSA end and governs. A non-ring
    ALSA playback device governs even when capture is Ring A, because that
    sink's own hardware buffer is what the process must feed.
    """

    governing_device = capture_device if playback_device is None else playback_device
    if governing_device in RING_PCM_DEVICES:
        for key in _OPERATOR_KNOBS:
            if os.environ.get(key, "").strip():
                log_event(
                    logger, "camilla_latency.operator_knob_ignored",
                    level=logging.WARNING, key=key, device=governing_device,
                )
        return (
            RING_CAMILLA_GEOMETRY["chunksize"] if chunksize is None else chunksize,
            (
                RING_CAMILLA_GEOMETRY["target_level"]
                if target_level is None
                else target_level
            ),
            RING_CAMILLA_GEOMETRY["queuelimit"] if queuelimit is None else queuelimit,
        )
    return (
        bounded_env_int("JASPER_CAMILLA_CHUNKSIZE", DEFAULT_CHUNKSIZE, lo=1, hi=_KNOB_MAX)
        if chunksize is None
        else chunksize,
        bounded_env_int("JASPER_CAMILLA_TARGET_LEVEL", DEFAULT_TARGET_LEVEL, lo=1, hi=_KNOB_MAX)
        if target_level is None
        else target_level,
        DEFAULT_QUEUELIMIT if queuelimit is None else queuelimit,
    )
