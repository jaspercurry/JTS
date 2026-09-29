# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Banked round data and curve readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.crossover_v2 import position_cycle
from jasper.active_speaker.crossover_v2.evidence_packet import (
    CrossoverEvidencePacketError,
    round_evidence,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundInputs,
    RoundViewsError,
    round_inputs,
)
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.audio_measurement.program_analysis import DriverResponse


@dataclass(frozen=True)
class BankedRound:
    """One round — banked or still live on the box — read once, ready for every
    view below."""

    round_dir: Path
    inputs: RoundInputs
    packet: Mapping[str, Any] = field(repr=False)

    @property
    def session_dir(self) -> Path:
        """The commissioning bundle this round's evidence was read from."""
        return self.inputs.session_dir


def load_banked_round(round_dir: Path) -> BankedRound:
    """Read one round — banked tree or LIVE session bundle — into a
    :class:`BankedRound`, its packet read through :func:`~.evidence_packet.round_evidence`.

    Which of the two it is, and so where its inputs come from, is
    :func:`~.round_inputs.round_inputs`' answer; they ride on
    :attr:`BankedRound.inputs`. Raises :class:`RoundViewsError` when the
    directory is neither shape, when a banked tree holds more than one session,
    when a packet built on read finds no crossover-v2 bundle, or when a banked
    tree stored no packet evidence; it keeps the packet reader's ``code``. It
    does NOT judge what the round banked — what a view needs, the view says
    (#3478, #3482).
    """
    round_dir = Path(round_dir)
    inputs = round_inputs(round_dir)
    try:
        packet = round_evidence(inputs)
    except CrossoverEvidencePacketError as exc:
        raise RoundViewsError(f"{round_dir}: {exc}", code=getattr(exc, "code", None)) from exc
    return BankedRound(round_dir=round_dir, inputs=inputs, packet=packet)


def response_from_banked_curve(curve: Mapping[str, Any]) -> tuple[DriverResponse, tuple[float, float]]:
    """One banked MEASURE curve as ``(DriverResponse, driven_band_hz)``.

    Both halves come back through the shipped inverse of
    :func:`~.spatial.pose_curve_record`, so "the banked curve" — its transfer
    function AND the band it was driven over — means here what it means to the
    delay landscape and the forward model. ``band_hz`` is optional on a banked
    curve and that parser already falls back to the grid extent, so it is never
    read off the mapping directly.

    A curve, or one of its repeats, without ``validity_floor_hz`` or
    ``repeat_curves`` refuses :data:`TAKE_CURVES_NOT_BANKED` by that field
    (#2902). A present ``None`` floor is "no floor was resolved", which
    ``compose_envelope`` handles itself. A curve it cannot read raises
    :class:`RoundViewsError`.
    """
    role = str(curve.get("role") or "")
    parsed = position_cycle.parse_curve_complex(curve)
    if parsed is None:
        raise RoundViewsError(f"the banked {role} curve is unreadable")
    for field_name in ("validity_floor_hz", "repeat_curves"):
        if field_name not in curve:
            raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {"field": field_name, "role": role})
    floor = curve["validity_floor_hz"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float, type(None))):
        raise RoundViewsError(f"the banked {role} curve is unreadable")
    freqs, tf, band = parsed
    return DriverResponse(
        role=role,
        freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.abs(tf)),
        complex_tf=tf,
        gating={"f_trusted_hz": trusted} if (trusted := curve.get("trusted_floor_hz")) is not None else {},
        snr=None,
        validity_floor_hz=None if floor is None else float(floor),
        repeat_responses=tuple(response_from_banked_curve(repeat)[0] for repeat in curve["repeat_curves"] or ()),
    ), band
