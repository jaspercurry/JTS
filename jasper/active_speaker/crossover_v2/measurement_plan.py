# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One plan an LLM authors and the executor runs: poses factored from takes.

A plan is ``poses x takes`` -- the mic visits each :class:`~.measurement_programs
.ProgramPose` once and plays every take (one :class:`~.measure_spec.MeasureSpec`
per candidate) there, under one held session. Factoring the two out, instead of
the flattened per-stop shape :mod:`.angle_capture` and :mod:`.measure_spec` each
already have, is what lets :meth:`MeasurementPlan.cost` price a round before any
mic moves. This module only builds and prices the document; nothing here stages
a walk or opens a session.

A take never carries its own ``positions`` -- the executor assigns the pose --
which is the one shape rule :func:`MeasurementPlan.validate` exists to hold.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import json_fingerprint

from ..angle_capture import MOVER_ARM, MOVER_HUMAN, MOVERS
from ..measurement_programs import (
    MeasurementProgram,
    ProgramPose,
    REGIME_BRANCHES,
    REGIME_SUMMED,
    program,
)
from .admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from .contracts import MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE
from .measure_spec import MeasureSpec

__all__ = [
    "PLAN_KIND",
    "PLAN_SCHEMA_VERSION",
    "LEVEL_HOLD_REFERENCE",
    "LEVEL_ACQUIRE_AT_ANCHOR",
    "LEVEL_SERIES",
    "LEVEL_MODES",
    "PLAN_REFUSALS",
    # Re-exported so a caller (the CLI, tests) has one import surface for the
    # plan model instead of also reaching into .angle_capture.
    "MOVER_ARM",
    "MOVER_HUMAN",
    "MOVERS",
    "LevelPolicy",
    "PlanCost",
    "MeasurementPlan",
    "PlanRefusal",
    "plan_for_program",
    "take_regime",
]

PLAN_KIND = "jts_measurement_plan"
PLAN_SCHEMA_VERSION = 1

#: How the executor holds the household's main-volume level across a plan's
#: takes. ``hold_reference``: touch nothing. ``acquire_at_anchor``: measure the
#: live level once, at the first pose, before comparing candidates against it.
#: ``series``: step through ``main_volume_series_db`` -- a session concept the
#: executor owns, never a per-take stimulus (that is :attr:`.measure_spec
#: .MeasureSpec.level_ladder_dbfs`, a different axis).
LEVEL_HOLD_REFERENCE = "hold_reference"
LEVEL_ACQUIRE_AT_ANCHOR = "acquire_at_anchor"
LEVEL_SERIES = "series"
LEVEL_MODES = (LEVEL_HOLD_REFERENCE, LEVEL_ACQUIRE_AT_ANCHOR, LEVEL_SERIES)

#: The closed vocabulary :func:`MeasurementPlan.validate` raises. Adding a
#: refusal means adding it here and nowhere else.
PLAN_REFUSALS = (
    "no_poses",
    "no_takes",
    "take_carries_positions",
    "unknown_mover",
    "unknown_level_mode",
    "series_requires_rungs",
    "ceiling_not_positive",
    "extra_attempts_negative",
    "unknown_candidate_scope",
    "plan_schema_unsupported",
)


class PlanRefusal(ValueError):
    """One closed-vocabulary reason a plan is not admissible. See :data:`PLAN_REFUSALS`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class LevelPolicy:
    """How the executor holds or steps the main-volume level for one plan."""

    mode: str = LEVEL_HOLD_REFERENCE
    main_volume_series_db: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "main_volume_series_db": list(self.main_volume_series_db),
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> LevelPolicy:
        return cls(
            mode=str(mapping.get("mode", LEVEL_HOLD_REFERENCE)),
            main_volume_series_db=tuple(
                float(v) for v in mapping.get("main_volume_series_db", ())
            ),
        )


@dataclass(frozen=True)
class PlanCost:
    """A plan's price, before any mic moves.

    ``stimulus_s`` sums, over every pose and every take there, that take's own
    stimulus duration -- its :attr:`~.measure_spec.MeasureSpec.sweep_s`, times
    its ladder rung count when :attr:`~.measure_spec.MeasureSpec
    .level_ladder_dbfs` is stated. A take with no stated ``sweep_s`` prices as
    ``0.0`` and turns ``stimulus_known`` false, rather than making the whole
    preview a guess.
    """

    poses: int
    takes_per_pose: int
    takes: int
    mic_moves: int
    max_attempts: int
    stimulus_s: float
    stimulus_known: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "poses": self.poses,
            "takes_per_pose": self.takes_per_pose,
            "takes": self.takes,
            "mic_moves": self.mic_moves,
            "max_attempts": self.max_attempts,
            "stimulus_s": self.stimulus_s,
            "stimulus_known": self.stimulus_known,
        }


def _take_stimulus_s(take: MeasureSpec) -> tuple[float, bool]:
    """One take's stimulus duration, or ``0.0`` with ``known=False``."""
    if take.sweep_s is None:
        return 0.0, False
    rungs = len(take.level_ladder_dbfs) if take.level_ladder_dbfs else 1
    return take.sweep_s * rungs, True


def _pose_to_dict(pose: ProgramPose) -> dict[str, Any]:
    body = asdict(pose)
    body["seat_offset_m"] = (
        list(pose.seat_offset_m) if pose.seat_offset_m is not None else None
    )
    return body


def _pose_from_mapping(mapping: Mapping[str, Any]) -> ProgramPose:
    return ProgramPose(**dict(mapping))


#: MeasureSpec fields the dataclass declares as tuples; JSON (and a plain
#: ``dict`` built by hand) carries them as lists, and ``MeasureSpec`` itself
#: does not renormalize -- unlike :class:`ProgramPose`, whose own
#: ``__post_init__`` already retuples ``seat_offset_m``.
_TAKE_TUPLE_FIELDS = ("positions", "pose_prompts", "level_ladder_dbfs", "sweep_band_hz")


def _take_to_dict(take: MeasureSpec) -> dict[str, Any]:
    body = asdict(take)
    for field_name in _TAKE_TUPLE_FIELDS:
        body[field_name] = list(body[field_name])
    return body


def _take_from_mapping(mapping: Mapping[str, Any]) -> MeasureSpec:
    kwargs = dict(mapping)
    for field_name in _TAKE_TUPLE_FIELDS:
        if field_name in kwargs:
            kwargs[field_name] = tuple(kwargs[field_name])
    return MeasureSpec(**kwargs)


@dataclass(frozen=True)
class MeasurementPlan:
    """poses x takes, one held session, priced before any mic moves.

    ``poses`` and ``takes`` are ORDER, not a set: nothing here reorders either,
    and the executor visits them in the order stated.
    """

    purpose: str
    program_id: str
    size: str
    mover: str
    poses: tuple[ProgramPose, ...]
    takes: tuple[MeasureSpec, ...]
    level: LevelPolicy
    ceiling_db_spl: float | None
    extra_attempts_per_pose: int = MAX_EXTRA_ATTEMPTS_PER_POSITION

    def _body(self) -> dict[str, Any]:
        return {
            "kind": PLAN_KIND,
            "schema_version": PLAN_SCHEMA_VERSION,
            "purpose": self.purpose,
            "program_id": self.program_id,
            "size": self.size,
            "mover": self.mover,
            "poses": [_pose_to_dict(pose) for pose in self.poses],
            "takes": [_take_to_dict(take) for take in self.takes],
            "level": self.level.to_dict(),
            "ceiling_db_spl": self.ceiling_db_spl,
            "extra_attempts_per_pose": self.extra_attempts_per_pose,
        }

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self._body())

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        body["fingerprint"] = self.fingerprint
        return body

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> MeasurementPlan:
        if not isinstance(mapping, Mapping) or (
            mapping.get("kind") != PLAN_KIND
            or mapping.get("schema_version") != PLAN_SCHEMA_VERSION
        ):
            raise PlanRefusal(
                "plan_schema_unsupported", "measurement plan schema/kind is unsupported"
            )
        return cls(
            purpose=str(mapping["purpose"]),
            program_id=str(mapping["program_id"]),
            size=str(mapping["size"]),
            mover=str(mapping["mover"]),
            poses=tuple(_pose_from_mapping(pose) for pose in mapping["poses"]),
            takes=tuple(_take_from_mapping(take) for take in mapping["takes"]),
            level=LevelPolicy.from_mapping(mapping.get("level") or {}),
            ceiling_db_spl=mapping.get("ceiling_db_spl"),
            extra_attempts_per_pose=int(
                mapping.get("extra_attempts_per_pose", MAX_EXTRA_ATTEMPTS_PER_POSITION)
            ),
        )

    def validate(self) -> None:
        """Raise :class:`PlanRefusal` for the first :data:`PLAN_REFUSALS` code that applies."""
        if not self.poses:
            raise PlanRefusal("no_poses", "a plan needs at least one pose")
        if not self.takes:
            raise PlanRefusal("no_takes", "a plan needs at least one take")
        for take in self.takes:
            if take.positions:
                raise PlanRefusal(
                    "take_carries_positions",
                    "a take names no positions; the executor assigns the pose",
                )
        if self.mover not in MOVERS:
            raise PlanRefusal("unknown_mover", f"mover must be one of {MOVERS}, got {self.mover!r}")
        if self.level.mode not in LEVEL_MODES:
            raise PlanRefusal(
                "unknown_level_mode",
                f"level mode must be one of {LEVEL_MODES}, got {self.level.mode!r}",
            )
        if self.level.mode == LEVEL_SERIES and not self.level.main_volume_series_db:
            raise PlanRefusal(
                "series_requires_rungs", "level mode series needs at least one rung"
            )
        if self.ceiling_db_spl is not None and (
            not math.isfinite(self.ceiling_db_spl) or self.ceiling_db_spl <= 0
        ):
            raise PlanRefusal("ceiling_not_positive", "ceiling_db_spl must be finite and positive")
        if self.extra_attempts_per_pose < 0:
            raise PlanRefusal(
                "extra_attempts_negative", "extra_attempts_per_pose must not be negative"
            )

    def cost(self) -> PlanCost:
        poses = len(self.poses)
        takes_per_pose = len(self.takes)
        durations = [_take_stimulus_s(take) for take in self.takes]
        return PlanCost(
            poses=poses,
            takes_per_pose=takes_per_pose,
            takes=poses * takes_per_pose,
            mic_moves=poses,
            max_attempts=poses * takes_per_pose + poses * self.extra_attempts_per_pose,
            stimulus_s=poses * sum(duration for duration, _known in durations),
            stimulus_known=all(known for _duration, known in durations),
        )


def take_regime(program: MeasurementProgram, candidates: Sequence[str]) -> str:
    """The driver-engagement regime a set of named candidates plays."""
    # see angle_capture.request_for_program's identical rule; a later
    # convergence PR folds the two into one.
    return REGIME_SUMMED if candidates and program.regime != REGIME_BRANCHES else program.regime


def plan_for_program(
    program_id: str,
    size: str | None,
    *,
    candidates: Sequence[str],
    candidate_scopes: Mapping[str, str],
    purpose: str | None = None,
    mover: str = MOVER_HUMAN,
    sweep_band_hz: tuple[float, float] | None = None,
    sweep_s: float | None = None,
    level: LevelPolicy = LevelPolicy(),
    ceiling_db_spl: float | None = None,
) -> MeasurementPlan:
    """One plan for ``program_id``/``size``: one take per candidate, no positions.

    ``"base"`` always measures the ``"base"`` graph scope; every other
    candidate measures the scope ``candidate_scopes`` names for it (see
    :func:`~.measured_crossover_candidate.candidate_trial_scope`) and is
    refused (:data:`PLAN_REFUSALS`'s ``unknown_candidate_scope``) when the
    mapping does not name one. ``ceiling_db_spl`` rides every take's own
    ``spl_ceiling_db_spl``: a batch must play one SPL ceiling
    (``jasper.cli.measure``'s ``REFUSE_SPL_CEILINGS_MIXED``), so a plan's
    takes are built to already agree.
    """
    resolved = program(program_id, size)
    resolved_purpose = purpose if purpose is not None else resolved.purpose
    band = tuple(sweep_band_hz) if sweep_band_hz else ()

    takes: list[MeasureSpec] = []
    for candidate in candidates:
        if candidate == "base":
            graph_scope, candidate_id, kind = "base", "", MEASURE_KIND_BASELINE
        else:
            try:
                graph_scope = candidate_scopes[candidate]
            except KeyError:
                raise PlanRefusal(
                    "unknown_candidate_scope", f"no scope given for candidate {candidate!r}"
                ) from None
            candidate_id, kind = candidate, MEASURE_KIND_CANDIDATE
        takes.append(
            MeasureSpec(
                kind=kind,
                graph_scope=graph_scope,
                candidate_id=candidate_id,
                sweep_band_hz=band,
                sweep_s=sweep_s,
                spl_ceiling_db_spl=ceiling_db_spl,
            )
        )

    return MeasurementPlan(
        purpose=resolved_purpose,
        program_id=resolved.program_id,
        size=resolved.size,
        mover=mover,
        poses=resolved.poses,
        takes=tuple(takes),
        level=level,
        ceiling_db_spl=ceiling_db_spl,
    )
