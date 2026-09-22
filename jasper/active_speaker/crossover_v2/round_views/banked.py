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
    _read_candidate,
    build_crossover_evidence_packet,
    round_artifact_dir,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundInputs,
    RoundViewsError,
    round_inputs,
)
from jasper.active_speaker.flat_spec import FlatSpecReport
from jasper.active_speaker.flat_spec_views import PositionCurve
from jasper.audio_measurement.program_analysis import DriverResponse

#: Mirrors ``.spatial.POSITION_ROLE_ONAX`` as a local literal rather than
#: importing that large, orchestration-heavy module for one string. This package
#: never owns the constant; it takes it as a caller-supplied value.
DEFAULT_PRIMARY_ROLE = "onax"


@dataclass(frozen=True)
class BankedRound:
    """One round — banked or still live on the box — read once, ready for every
    view below.

    ``report`` carries the round's own grading frame; ``positions`` is built from
    the evidence packet's ``positions`` block, and nothing here re-parses
    ``cloud_verify.json``. Both are ABSENT on a round that banked no cloud group,
    and that is a round SHAPE rather than a defect (#3478): only the verify stage
    banks one. The two accessors below are where a view that NEEDS them says so.
    """

    round_dir: Path
    inputs: RoundInputs
    positions: tuple[PositionCurve, ...]
    curve_grid_hz: np.ndarray
    report: FlatSpecReport | None
    packet: Mapping[str, Any] = field(repr=False)

    @property
    def session_dir(self) -> Path:
        """The commissioning bundle this round's evidence was read from."""
        return self.inputs.session_dir

    @property
    def graded_report(self) -> FlatSpecReport:
        """The round's own graded spec, or :class:`RoundViewsError`."""
        if self.report is None:
            raise RoundViewsError(
                f"{self.round_dir}: evidence packet carries no graded spec"
            )
        return self.report

    @property
    def graded_positions(self) -> tuple[PositionCurve, ...]:
        """The round's cloud seats, or :class:`RoundViewsError`.

        TWO refusals, because two different things are missing: a round that
        banked no cloud group is the measure-stage SHAPE above, while a block
        that says ``available`` with nothing left after the ``magnitude_db``
        filter is a TRUNCATED packet from a round that did walk one.
        """
        if self.positions:
            return self.positions
        block = self.packet.get("positions") or {}
        if block.get("available"):
            raise RoundViewsError(
                f"{self.round_dir}: every position row is missing its magnitude_db"
            )
        raise RoundViewsError(
            f"{self.round_dir}: evidence packet carries no position evidence"
        )


def _row_degrees(row: Mapping[str, Any]) -> float | None:
    """One packet position row's banked bearing, or ``None`` for "not recorded".

    ``None`` covers every way a row can lack one and they are deliberately not
    told apart HERE; the packet's own ``angle_deg`` block publishes the
    distinction. ``bool`` is rejected before ``int`` because it subclasses it,
    so a hand-edited ``true`` would otherwise publish as a 1° bearing.
    """
    value = row.get("position_deg")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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

    positions_block = packet.get("positions") or {}
    spec_block = packet.get("spec") or {}
    grid_block = positions_block.get("curve_grid") or {}
    grid = np.asarray(grid_block.get("freqs_hz") or [], dtype=float)
    smoothing = int(grid_block.get("smoothing_fraction") or 0)
    positions = tuple(
        PositionCurve(
            position_id=str(row.get("position_id") or ""),
            role=str(row.get("role") or ""),
            freqs_hz=grid,
            magnitude_db=np.asarray(row.get("magnitude_db") or [], dtype=float),
            smoothing_fraction=smoothing,
            # The seat's OWN banked bearing, read rather than defaulted. Absent
            # stays ``None`` — "not recorded", never zero. ``bool`` is excluded
            # because it subclasses ``int``.
            degrees=_row_degrees(row),
            take_id=str(row.get("take_id") or ""),
        )
        for row in positions_block.get("positions") or []
        if row.get("magnitude_db")
    )
    report = FlatSpecReport.from_dict(spec_block) if spec_block.get("bands") else None
    return BankedRound(
        round_dir=round_dir,
        inputs=inputs,
        positions=positions,
        curve_grid_hz=grid,
        report=report,
        packet=packet,
    )


def _round_candidate(banked: BankedRound) -> dict[str, Any]:
    """The round's own ``candidate.json``, or ``{}``."""
    artifact_dir, _why = round_artifact_dir(banked.session_dir)
    return {} if artifact_dir is None else _read_candidate(artifact_dir)


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
