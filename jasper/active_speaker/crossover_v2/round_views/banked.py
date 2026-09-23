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
    build_crossover_evidence_packet,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundInputs,
    RoundViewsError,
    round_inputs,
)
from jasper.active_speaker.flat_spec import FlatSpecReport
from jasper.audio_measurement.program_analysis import DriverResponse


@dataclass(frozen=True)
class BankedRound:
    """One round — banked or still live on the box — read once, ready for every
    view below.

    ``report`` carries the round's own grading frame, and is ABSENT on a round
    that banked no cloud group: that is a round SHAPE rather than a defect
    (#3478), since only the verify stage banks one.
    """

    round_dir: Path
    inputs: RoundInputs
    report: FlatSpecReport | None
    packet: Mapping[str, Any] = field(repr=False)

    @property
    def session_dir(self) -> Path:
        """The commissioning bundle this round's evidence was read from."""
        return self.inputs.session_dir


def load_banked_round(round_dir: Path) -> BankedRound:
    """Read one round — banked tree or LIVE session bundle — into a
    :class:`BankedRound`.

    Which of the two it is, and so where the flow state, design draft and
    applied profile come from, is :func:`~.round_inputs.round_inputs`' answer;
    it rides on :attr:`BankedRound.inputs` so the views read the same files this
    packet was built from. Raises :class:`RoundViewsError` when the directory is
    neither shape, when a banked tree holds more than one session, or when the
    bundle carries no readable evidence packet. It does NOT judge what the round
    banked — what a view needs, the view says (#3478, #3482).
    """
    round_dir = Path(round_dir)
    inputs = round_inputs(round_dir)
    try:
        packet = build_crossover_evidence_packet(
            inputs.session_dir,
            state_path=inputs.state_path,
            driver_draft_path=inputs.design_draft_path,
            applied_profile_path=inputs.applied_profile_path,
            repeat_floor_path=inputs.repeat_floor_path,
            declared_geometry_path=inputs.declared_geometry_path,
            statefile_path=inputs.statefile_path,
        )
    except CrossoverEvidencePacketError as exc:
        raise RoundViewsError(f"{round_dir}: {exc}") from exc

    spec_block = packet.get("spec") or {}
    report = FlatSpecReport.from_dict(spec_block) if spec_block.get("bands") else None
    return BankedRound(round_dir=round_dir, inputs=inputs, report=report, packet=packet)


def response_from_banked_curve(curve: Mapping[str, Any]):
    """One banked MEASURE curve as ``(DriverResponse, driven_band_hz)``, or
    ``None`` when the take predates the two inputs the fit needs.

    Both halves come back through the shipped inverse of
    :func:`~.spatial.pose_curve_record`, so "the banked curve" — its transfer
    function AND the band it was driven over — means here what it means to the
    delay landscape and the forward model. ``band_hz`` is optional on a banked
    curve and that parser already falls back to the grid extent, so it is never
    read off the mapping directly.

    The two fit inputs are decided on the KEY, not the value: an absent
    ``validity_floor_hz`` is a take from before the field rode here, while a
    present ``None`` is "no floor was resolved" — which ``compose_envelope``
    handles itself, and which re-running the round would not change.
    """
    parsed = position_cycle.parse_curve_complex(curve)
    if parsed is None:
        return None
    if "validity_floor_hz" not in curve or "repeat_curves" not in curve:
        return None
    floor = curve["validity_floor_hz"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float, type(None))):
        return None
    repeats = []
    for occurrence in curve["repeat_curves"] or ():
        repeat = response_from_banked_curve(occurrence)
        if repeat is None:
            return None
        repeats.append(repeat[0])
    freqs, tf, band = parsed
    return DriverResponse(
        role=str(curve.get("role") or ""),
        freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.abs(tf)),
        complex_tf=tf,
        gating={"f_trusted_hz": trusted} if (trusted := curve.get("trusted_floor_hz")) is not None else {},
        snr=None,
        validity_floor_hz=None if floor is None else float(floor),
        repeat_responses=tuple(repeats),
    ), band
