# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-to-session repeatability of pooled grades."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from jasper.active_speaker import flat_spec
from jasper.active_speaker.crossover_v2.evidence_packet import _mapping
from jasper.active_speaker.flat_spec_views import role_split_flatness
from jasper.active_speaker.repeat_floor import SHIPPED_POOL_METRIC, sample_spread

from .banked import DEFAULT_PRIMARY_ROLE, BankedRound


@dataclass(frozen=True)
class RepeatabilityMetric:
    """One metric's spread across the compared rounds, plus each round's own
    value keyed by the round label the caller supplied.

    ``degrees`` is the BEARING each round banked for this row, empty on a pooled
    role metric. It exists because a position id stopped naming the same bearing
    across the 2026-08-24 geometry ruling, which put the design axis at the front
    of the post-apply pose set: ``cloud_verify_02`` was −7° before it and 0°
    after. A spread taken across that boundary is the difference between two
    different seats.

    It DISCLOSES rather than refuses: this is an interpretation question, and
    comparing a pre-ruling round to a post-ruling one is legitimate.
    :meth:`bearings_agree` names the answer; what to do with ``False`` is the
    reader's call.
    """

    name: str
    values: dict[str, float]
    #: ``{round label: bearing}``, only for rows that HAVE one. A label absent
    #: from this map recorded no bearing for the row, which is why
    #: :meth:`bearings_agree` answers ``None`` rather than ``True`` below two
    #: known bearings: "nothing disagreed" and "nothing was comparable" differ.
    degrees: dict[str, float] = field(default_factory=dict)

    def bearings_agree(self) -> bool | None:
        """Whether every round that recorded a bearing recorded the SAME one.

        ``None`` means unknowable here — fewer than two rounds recorded one, so
        there is no comparison to make. Never ``True`` by default.
        """
        known = list(self.degrees.values())
        if len(known) < 2:
            return None
        return len(set(known)) == 1

    def spread(self) -> dict[str, float] | None:
        return sample_spread(list(self.values.values()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "values": self.values,
            "spread": self.spread(),
            "degrees": self.degrees,
            "bearings_agree": self.bearings_agree(),
        }


@dataclass(frozen=True)
class RepeatabilityResult:
    """The stop-criterion table: per-round pooled figures, their spread
    (the measured repeat noise), and per-position stability."""

    round_labels: tuple[str, ...]
    metrics: tuple[RepeatabilityMetric, ...]
    per_position: tuple[RepeatabilityMetric, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_labels": list(self.round_labels),
            "metrics": [m.to_dict() for m in self.metrics],
            "per_position": [m.to_dict() for m in self.per_position],
        }


def repeatability_spread(
    rounds: Sequence[tuple[str, BankedRound]], *, primary_role: str = DEFAULT_PRIMARY_ROLE
) -> RepeatabilityResult:
    """Session-to-session spread of the pooled honest figures, plus each round's
    own value.

    ``rounds`` is ``(label, banked_round)`` pairs; the label is whatever the
    caller wants printed. Every round is graded SHIPPED (no frozen substitution)
    through the PUBLIC
    :func:`~jasper.active_speaker.flat_spec_views.role_split_flatness`, which
    reports BOTH poolings per role — ``rms_db``, the shipped per-bin weighting,
    and ``log_rms_db``, the per-octave re-weighting — and this view carries both
    through under those names. ``primary_role`` is a seam requirement of that
    signature, not a repeatability policy: the split is immediately recombined.
    """
    role_pooled: dict[str, dict[str, float]] = {}
    log_role_pooled: dict[str, dict[str, float]] = {}
    linear_pooled: dict[str, float] = {}
    position_values: dict[str, dict[str, float]] = {}
    position_degrees: dict[str, dict[str, float]] = {}
    for label, banked in rounds:
        split = role_split_flatness(
            banked.graded_report, banked.graded_positions, primary_role=primary_role,
        )
        roles = ([split.primary] if split.primary is not None else []) + list(split.others)
        for role_flatness in roles:
            if role_flatness.rms_db is not None:
                role_pooled.setdefault(role_flatness.role, {})[label] = role_flatness.rms_db
            if role_flatness.log_rms_db is not None:
                log_role_pooled.setdefault(role_flatness.role, {})[label] = role_flatness.log_rms_db
            for position_flatness in role_flatness.positions:
                if position_flatness.rms_db is not None:
                    position_values.setdefault(position_flatness.position_id, {})[
                        label
                    ] = position_flatness.rms_db
                    # The seat's banked bearing rides beside its number, so a
                    # comparison spanning the geometry ruling is VISIBLE. Only
                    # recorded bearings are stored, which is what makes
                    # ``bearings_agree()`` answer None rather than invent it.
                    if position_flatness.degrees is not None:
                        position_degrees.setdefault(position_flatness.position_id, {})[
                            label
                        ] = float(position_flatness.degrees)
        # The SHIPPED linear-pooled figure — spec_convergence_residual's own
        # number, lifted from the report rather than recomputed — so a caller can
        # see whether the number the tournament actually reads repeats too.
        residual = flat_spec.spec_convergence_residual(banked.graded_report)
        if residual.evaluable and residual.rms_db is not None:
            linear_pooled[label] = float(residual.rms_db)

    labels = tuple(label for label, _banked in rounds)
    metrics = [RepeatabilityMetric(SHIPPED_POOL_METRIC, dict(linear_pooled))]
    for role in sorted(role_pooled):
        metrics.append(RepeatabilityMetric(f"{role}_linear_pooled_db", dict(role_pooled[role])))
    for role in sorted(log_role_pooled):
        metrics.append(RepeatabilityMetric(f"{role}_log_pooled_db", dict(log_role_pooled[role])))
    per_position_metrics = [
        RepeatabilityMetric(seat, dict(values), dict(position_degrees.get(seat, {})))
        for seat, values in sorted(position_values.items())
    ]
    return RepeatabilityResult(
        round_labels=labels, metrics=tuple(metrics), per_position=tuple(per_position_metrics)
    )


def repeat_floor_provenance(label: str, banked: BankedRound) -> dict[str, Any]:
    """One record row naming what produced a repeat — the packet fields a
    floor cites. Basename only: the record leaves this laptop and a local
    path is nobody's provenance."""
    session = _mapping(banked.packet.get("session"))
    identity = _mapping(banked.packet.get("identity"))
    return {
        "label": Path(label).name,
        "bundle_session_id": session.get("bundle_session_id"),
        "graph_fingerprint": identity.get("graph_fingerprint"),
        "mic_calibration_id": _mapping(identity.get("mic")).get("calibration_id"),
        "started_at": session.get("started_at"),
    }
