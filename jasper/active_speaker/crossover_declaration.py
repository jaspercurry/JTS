"""The crossover geometry Sound DECLARES, and the change one candidate asks for.

``/sound``'s design draft spells a crossover as ``frequency_hz`` /
``filter_type`` / ``slope_db_per_octave``; a measured candidate's
:class:`~jasper.active_speaker.profile.CrossoverRegion` spells the same fact as
``fc_hz`` / ``target_type`` / ``order``. This is the one place the two are
compared, and the one place their difference becomes an instruction. The change
is derived from the candidate, never from an advisory selection record, so the
declaration and the emitted graph cannot disagree. Nothing here writes:
``sound_setup`` owns the single durable writer in both directions.

**Why the comparison has to exist at all.**
``baseline_profile``'s ``measured_candidate_preset_mismatch`` guard is a whole-
dataclass ``!=``: the candidate's ``source_preset`` must equal the preset
recompiled *now* from the saved declaration. So a candidate measured at a
crossover the declaration does not carry can only be applied by making the
declaration carry it FIRST. That is what :class:`CrossoverDeclarationChange`
describes, and what
``jasper.web.sound_setup.apply_measured_crossover_geometry`` writes.

**Why it is derived from the candidate rather than read off a selection
record.** The candidate is the artifact that will be applied; a persisted
recommendation is a claim *about* it. Deriving the change from the candidate
means the declaration and the emitted graph cannot disagree by construction,
and it leaves exactly one answer to "what crossover is this apply asking for".
An advisory record — the retired corner selector's recommendation was one, and
whatever supersedes it will be another — stays what it is: evidence a screen
may render, and not a gate it was never able to keep honest.

**Nothing here writes.** This module derives and refuses; ``sound_setup`` owns
the single durable writer, in both directions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .driver_protection import (
    format_protection_hz,
    protection_highpass_floor_satisfied,
)
from .test_signal_plan import (
    declared_protection_floor_hz,
    strictest_crossover_highpass_hz,
)

__all__ = [
    "CROSSOVER_BELOW_DECLARED_FLOOR",
    "CROSSOVER_DECLARATION_CHANGE_KIND",
    "CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION",
    "CrossoverBelowDeclaredFloor",
    "CrossoverDeclarationChange",
    "CrossoverGeometry",
    "assert_crossover_honours_declared_floor",
    "change_from_record",
    "change_to_record",
    "declaration_change_for_candidate",
    "declared_crossover_geometry",
    "matching_declared_candidate_index",
    "preset_crossover_geometry",
]

#: Frequencies this close are the same declared corner. Named once so the
#: derivation, the writer's compare-and-swap and the tests share one number.
FC_SAME_HZ_TOL = 0.05

#: Slopes this close are the same declared slope. Tighter than the frequency
#: tolerance on purpose: ``staging._slope_to_lr_order`` compiles exactly 12 /
#: 24 / 48 dB/octave and refuses anything 0.01 away, so a looser tolerance here
#: could call two slopes the same that compile to different orders.
SLOPE_SAME_DB_TOL = 0.005

#: The role whose declared protection floor bounds a crossover corner, spelled
#: as its three sibling gates spell it (``camilla_yaml``'s emit gate,
#: ``path_safety``'s load gate, ``test_signal_plan``'s protective clamp).
_PROTECTED_ROLE = "tweeter"

#: ``reason=`` slug of the apply path's below-floor refusal. Machine-readable
#: half of a hearing-safety refusal: the operator sentence may be reworded,
#: this may not.
CROSSOVER_BELOW_DECLARED_FLOOR = "crossover_below_declared_protection_floor"

#: The document version :func:`change_to_record` answers. A record naming a
#: version this build does not speak reads as no change to reverse — see
#: :func:`change_from_record`.
CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION = 1

#: The ``kind`` discriminator: this record is the Undo inverse of a
#: Sound-declaration write, not a prescription.
CROSSOVER_DECLARATION_CHANGE_KIND = "jts_crossover_declaration_change"


class CrossoverBelowDeclaredFloor(ValueError):
    """A crossover corner sits below the protected driver's declared floor.

    Carries :attr:`reason` (the stable slug) beside the household sentence.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = CROSSOVER_BELOW_DECLARED_FLOOR


@dataclass(frozen=True)
class CrossoverGeometry:
    """One crossover, in the declaration's own vocabulary.

    ``filter_type`` and ``slope_db_per_octave`` are the *declared* spellings
    (``"Linkwitz-Riley"``, ``24.0``), never the preset's compiled ones
    (``"LinkwitzRiley"``, ``order=4``): this value is written back into
    ``/sound``.
    """

    fc_hz: float
    filter_type: str
    slope_db_per_octave: float

    def same_filter_as(self, other: "CrossoverGeometry") -> bool:
        """Whether both name the same filter, asked of the module that compiles
        it — a household's ``"LR"`` is not a crossover change."""

        from .staging import same_declared_filter_type

        return same_declared_filter_type(self.filter_type, other.filter_type)

    def matches(self, other: "CrossoverGeometry") -> bool:
        """Whether both describe the same declared crossover."""

        return (
            math.isclose(self.fc_hz, other.fc_hz, abs_tol=FC_SAME_HZ_TOL)
            and self.same_filter_as(other)
            and math.isclose(
                self.slope_db_per_octave,
                other.slope_db_per_octave,
                abs_tol=SLOPE_SAME_DB_TOL,
            )
        )


@dataclass(frozen=True)
class CrossoverDeclarationChange:
    """What one candidate asks ``/sound`` to declare, and what it declares now.

    ``configured`` is read from the live draft and is what the writer's
    compare-and-swap checks; ``selected`` is read off the candidate's preset.
    Undo is this value with the two swapped.
    """

    between_roles: tuple[str, str]
    configured: CrossoverGeometry
    selected: CrossoverGeometry

    @property
    def changes_frequency(self) -> bool:
        return not math.isclose(
            self.configured.fc_hz, self.selected.fc_hz, abs_tol=FC_SAME_HZ_TOL
        )

    @property
    def changes_slope(self) -> bool:
        return not self.configured.same_filter_as(self.selected) or not math.isclose(
            self.configured.slope_db_per_octave,
            self.selected.slope_db_per_octave,
            abs_tol=SLOPE_SAME_DB_TOL,
        )


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def preset_crossover_geometry(
    preset: Any,
) -> tuple[tuple[str, str], CrossoverGeometry] | None:
    """The single crossover a preset declares, in the declaration's vocabulary.

    ``None`` for anything that is not exactly one region compiled from exactly
    one declared filter: no regions, two or more (a three-way declares two
    entries and this seam writes one), or a region no declared spelling
    compiles to. Each is a real refusal — the alternative is guessing.
    """

    from .staging import declaration_filter_type, declaration_slope_db_per_octave

    regions = tuple(getattr(preset, "crossover_regions", None) or ())
    if len(regions) != 1:
        return None
    region = regions[0]
    fc_hz = _finite_positive(getattr(region, "fc_hz", None))
    filter_type = declaration_filter_type(getattr(region, "target_type", None))
    slope = declaration_slope_db_per_octave(getattr(region, "order", None))
    lower = str(getattr(region, "lower_driver", "") or "")
    upper = str(getattr(region, "upper_driver", "") or "")
    if fc_hz is None or filter_type is None or slope is None or not lower or not upper:
        return None
    return (lower, upper), CrossoverGeometry(
        fc_hz=fc_hz, filter_type=filter_type, slope_db_per_octave=slope
    )


def matching_declared_candidate_index(
    candidates: Sequence[Any], between_roles: tuple[str, str]
) -> int | None:
    """Index of the ONE declared crossover entry for ``between_roles``.

    ``None`` when it is missing or ambiguous. One matcher, shared with the
    writer in ``sound_setup``, so the entry read is the entry written.
    """

    matches = [
        index
        for index, item in enumerate(candidates)
        if isinstance(item, Mapping)
        and tuple(item.get("between_roles") or ()) == between_roles
    ]
    return matches[0] if len(matches) == 1 else None


def declared_crossover_geometry(
    design_draft: Any, between_roles: tuple[str, str]
) -> CrossoverGeometry | None:
    """What ``/sound`` declares for ``between_roles`` right now, or ``None``.

    ``None`` when the draft has no manual settings, no unambiguous entry for
    these roles, or an entry omitting any of the three fields. ``design_draft``
    drops empty fields on save, so an entry with no declared slope must be left
    alone rather than completed with a number nobody declared.
    """

    manual = design_draft.get("manual_settings") if isinstance(design_draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return None
    raw = manual.get("crossover_candidates")
    candidates = list(raw) if isinstance(raw, list) else []
    index = matching_declared_candidate_index(candidates, between_roles)
    if index is None:
        return None
    entry = candidates[index]
    fc_hz = _finite_positive(entry.get("frequency_hz"))
    slope = _finite_positive(entry.get("slope_db_per_octave"))
    filter_type = entry.get("filter_type")
    if fc_hz is None or slope is None or not isinstance(filter_type, str) or not filter_type:
        return None
    return CrossoverGeometry(
        fc_hz=fc_hz, filter_type=filter_type, slope_db_per_octave=slope
    )


def declaration_change_for_candidate(
    *, source_preset: Any, design_draft: Any
) -> CrossoverDeclarationChange | None:
    """The declaration change this candidate needs, or ``None`` for none.

    ``None`` means this apply writes nothing to ``/sound``: the candidate was
    measured at exactly the declared crossover, or the two cannot be reconciled
    at all (:func:`preset_crossover_geometry` and
    :func:`declared_crossover_geometry` each say why).
    """

    resolved = preset_crossover_geometry(source_preset)
    if resolved is None:
        return None
    between_roles, selected = resolved
    configured = declared_crossover_geometry(design_draft, between_roles)
    if configured is None or configured.matches(selected):
        return None
    return CrossoverDeclarationChange(
        between_roles=between_roles, configured=configured, selected=selected
    )


def change_to_record(change: CrossoverDeclarationChange) -> dict[str, Any]:
    """One change as the plain JSON the review state persists.

    ``applied_*`` is what the accept put into ``/sound``; ``previous_*`` is what
    it displaced. Undo reads this back and runs the write with the two swapped.
    """

    return {
        "artifact_schema_version": CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION,
        "kind": CROSSOVER_DECLARATION_CHANGE_KIND,
        "between_roles": [change.between_roles[0], change.between_roles[1]],
        "applied_hz": float(change.selected.fc_hz),
        "previous_hz": float(change.configured.fc_hz),
        "applied_filter_type": change.selected.filter_type,
        "previous_filter_type": change.configured.filter_type,
        "applied_slope_db_per_octave": float(change.selected.slope_db_per_octave),
        "previous_slope_db_per_octave": float(change.configured.slope_db_per_octave),
    }


def change_from_record(record: Any) -> CrossoverDeclarationChange | None:
    """:func:`change_to_record` run backwards, or ``None`` for a bad record.

    Every field is required; a record missing any reads as ``None`` rather than
    as a partial change completed with a guess.

    One exception: a record naming NEITHER ``kind`` nor
    ``artifact_schema_version`` is the pre-envelope shape a deployed speaker may
    still hold, and reads as this module's own kind at version 1. A record
    naming EITHER, with the other missing or wrong, tried to speak the envelope
    and got it wrong, so it reads as ``None``.
    """

    if not isinstance(record, Mapping):
        return None
    pre_envelope = (
        "kind" not in record and "artifact_schema_version" not in record
    )
    if not pre_envelope and (
        record.get("kind") != CROSSOVER_DECLARATION_CHANGE_KIND
        or record.get("artifact_schema_version")
        != CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION
    ):
        return None
    roles = record.get("between_roles")
    applied_hz = _finite_positive(record.get("applied_hz"))
    previous_hz = _finite_positive(record.get("previous_hz"))
    applied_type = record.get("applied_filter_type")
    previous_type = record.get("previous_filter_type")
    applied_slope = _finite_positive(record.get("applied_slope_db_per_octave"))
    previous_slope = _finite_positive(record.get("previous_slope_db_per_octave"))
    if (
        not isinstance(roles, Sequence)
        or isinstance(roles, (str, bytes))
        or len(roles) != 2
        or not all(isinstance(role, str) and role for role in roles)
        or applied_hz is None
        or previous_hz is None
        or applied_slope is None
        or previous_slope is None
        or not isinstance(applied_type, str)
        or not applied_type
        or not isinstance(previous_type, str)
        or not previous_type
    ):
        return None
    return CrossoverDeclarationChange(
        between_roles=(str(roles[0]), str(roles[1])),
        configured=CrossoverGeometry(
            fc_hz=previous_hz,
            filter_type=previous_type,
            slope_db_per_octave=previous_slope,
        ),
        selected=CrossoverGeometry(
            fc_hz=applied_hz,
            filter_type=applied_type,
            slope_db_per_octave=applied_slope,
        ),
    )


def assert_crossover_honours_declared_floor(preset: Any) -> None:
    """Refuse a preset whose crossover is below the protected driver's floor.

    The apply path's BOUNDARY re-check, on the same condition the emit gate and
    the startup-load gate refuse, through the shared rule
    ``driver_protection.protection_highpass_floor_satisfied``: *at* the floor is
    legal, below it is refused, an undeclared floor is honoured unchanged, and a
    declared floor with no readable corner is refused.

    It runs here, not only at emit, because the apply transaction saves the
    Sound declaration BEFORE it emits the graph, so an emit-time refusal leaves
    ``/sound`` declaring a crossover the speaker cannot be made to play.
    """

    floor_hz = declared_protection_floor_hz(preset, _PROTECTED_ROLE)
    crossover_hz = strictest_crossover_highpass_hz(preset, _PROTECTED_ROLE)
    if protection_highpass_floor_satisfied(
        highpass_hz=crossover_hz, floor_hz=floor_hz
    ):
        return
    # Unreachable with floor_hz None: the shared predicate honours an absent
    # floor, so a refusal here always has a real declared number to name.
    assert floor_hz is not None
    floor = format_protection_hz(floor_hz)
    if crossover_hz is None:
        detail = (
            "this speaker declares no crossover corner that high-passes the "
            f"{_PROTECTED_ROLE}, so it cannot be proven to honour that "
            f"driver's own declared protective high-pass floor of {floor}"
        )
    else:
        detail = (
            f"it crosses at {format_protection_hz(crossover_hz)}, below the "
            f"{_PROTECTED_ROLE}'s own declared protective high-pass floor of "
            f"{floor}; raise the crossover to at least {floor} (or correct "
            "that driver's declared required_protection_filters)"
        )
    raise CrossoverBelowDeclaredFloor(
        "refusing to apply this crossover: " + detail
    )
