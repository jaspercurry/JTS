"""The crossover geometry Sound DECLARES, and the change one candidate asks for.

Two speakers, one fact. ``/sound``'s design draft declares a crossover as
``manual_settings.crossover_candidates[]`` — ``frequency_hz``,
``filter_type``, ``slope_db_per_octave``. A measured candidate carries the
:class:`~jasper.active_speaker.profile.ActiveSpeakerPreset` it was *measured
against*, whose :class:`~jasper.active_speaker.profile.CrossoverRegion` spells
the same fact as ``fc_hz`` / ``target_type`` / ``order``. This module is the
one place those two spellings are compared, and the one place the difference
between them becomes an instruction.

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

#: Frequencies this close are the same declared corner. The tolerance the apply
#: path and the declaration writer already used, named once so the derivation,
#: the writer's compare-and-swap, and the tests cannot pick three numbers.
FC_SAME_HZ_TOL = 0.05

#: Slopes this close are the same declared slope. Tighter than the frequency
#: tolerance on purpose: ``staging._slope_to_lr_order`` compiles exactly 12 /
#: 24 / 48 dB/octave and refuses anything 0.01 away, so a looser tolerance here
#: could call two slopes the same that compile to different orders.
SLOPE_SAME_DB_TOL = 0.005

#: The role whose declared protection floor bounds a crossover corner. Spelled
#: the same way its three sibling gates spell it (``camilla_yaml``'s emit gate,
#: ``path_safety``'s load gate, ``test_signal_plan``'s protective clamp) —
#: there is no shared role constant in this package to point at instead.
_PROTECTED_ROLE = "tweeter"

#: ``reason=`` slug of the apply path's below-floor refusal. Machine-readable
#: half of a hearing-safety refusal: the operator sentence may be reworded,
#: this may not.
CROSSOVER_BELOW_DECLARED_FLOOR = "crossover_below_declared_protection_floor"

#: The document version :func:`change_to_record` answers, mirroring
#: :data:`~.crossover_v2.driver_prescription.DRIVER_PRESCRIPTION_SCHEMA_VERSION`'s
#: envelope. A record naming a version this build does not speak reads as no
#: change to reverse, on this module's own tolerant-read rule — see
#: :func:`change_from_record`.
CROSSOVER_DECLARATION_CHANGE_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, mirroring
#: :data:`~.crossover_v2.driver_prescription.DRIVER_PRESCRIPTION_KIND`: this
#: record is the Undo inverse of a Sound-declaration write, not a prescription,
#: and its own string says so.
CROSSOVER_DECLARATION_CHANGE_KIND = "jts_crossover_declaration_change"


class CrossoverBelowDeclaredFloor(ValueError):
    """A crossover corner sits below the protected driver's declared floor.

    Carries :attr:`reason` (the stable slug) beside the household sentence, so
    a caller can log the machine-readable fact and render the prose without
    parsing one out of the other.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = CROSSOVER_BELOW_DECLARED_FLOOR


@dataclass(frozen=True)
class CrossoverGeometry:
    """One crossover, in the declaration's own vocabulary.

    ``filter_type`` and ``slope_db_per_octave`` are the *declared* spellings
    (``"Linkwitz-Riley"``, ``24.0``), never the preset's compiled ones
    (``"LinkwitzRiley"``, ``order=4``): this value is what gets written back
    into ``/sound``, so it speaks the language of the file it lands in.
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
    compare-and-swap checks; ``selected`` is read off the candidate's own
    preset. Undo is this value with the two swapped — the same write run
    backwards, which is why they are one type and not two.
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

    ``None`` — meaning "this preset cannot be reconciled against a declaration
    entry" — for anything that is not exactly one region compiled from exactly
    one declared filter: no regions, two or more regions (a three-way's
    declaration has two entries and this seam writes one), or a region whose
    ``target_type`` / ``order`` no declared spelling compiles to. Every one of
    those is a real refusal, not a defect: the alternative is guessing what
    Sound should say.
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

    ``None`` when it is missing or ambiguous. One matcher, shared by the
    derivation here and the writer in ``sound_setup``, so a declaration the
    derivation read cannot be a different entry from the one the write lands
    on.
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
    these roles, or an entry that omits any of the three fields. That last case
    is not an error to repair here: ``design_draft`` drops empty fields on
    save, so an entry with no declared slope is one this seam must leave alone
    rather than complete with a number nobody declared.
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

    ``None`` is the ordinary answer and the safe one: it means this apply
    writes nothing to ``/sound`` — either because the candidate was measured at
    exactly the declared crossover (every shipped session today), or because
    the two cannot be reconciled at all (:func:`preset_crossover_geometry` and
    :func:`declared_crossover_geometry` each say why). A caller that gets
    ``None`` must behave exactly as it did before this seam existed.
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
    it displaced. Undo reads this back and runs the write with the two swapped,
    which is why the round trip lives here beside the type rather than in the
    handler that happens to store it.
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

    Every field is required, and a record missing any of them reads as ``None``
    — "there is no declaration write to reverse" — rather than as a partial
    change completed with a guess, and inventing a slope to write into
    ``/sound`` would be a number no accept ever chose. That strictness DOES
    have an older shape to be tolerant of, though: the accept branch that
    writes this record's shape shipped in the same PR as this module (#2743),
    so a deployed speaker can already hold a record from before ``kind``/
    ``artifact_schema_version`` existed — see the envelope paragraph below
    for how that shape is read.

    ``kind`` and ``artifact_schema_version`` are checked on the same terms as
    every other field, with ONE exception: a record naming NEITHER is exactly
    the pre-envelope shape #2743's own shipped builds wrote, and such records
    are carried unconditionally across a deploy for as long as the applied
    graph is (``correction_crossover_v2.observe_apply_success`` /
    ``persist_conductor_state``) — so refusing it outright would strand a
    reader against a record an older build legitimately wrote. That one shape
    reads as this module's own kind and version 1 rather than raising. A
    record naming EITHER field, even if the other is missing or wrong, is NOT
    that legacy shape — it tried to speak the envelope and got it wrong — and
    reads as ``None`` under both postures, exactly like the seven fields
    beside it.
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

    The apply path's BOUNDARY re-check, and deliberately the same condition the
    L0 emit gate (``camilla_yaml._assert_tweeter_crossover_honours_declared_
    floor``) and the startup-load gate (``path_safety._tweeter_protection_
    floor_verdict``) refuse. All three read the two numbers from their owners
    (``test_signal_plan``) and compare them with the shared rule
    (``driver_protection.protection_highpass_floor_satisfied``), so no layer
    can drift on the boundary case: *at* the floor is legal, below it is
    refused, an undeclared floor is honoured unchanged, and a declared floor
    with no readable corner is refused rather than waved through.

    **Why a third layer, when the emit gate already refuses.** Ordering. The
    apply transaction saves the Sound declaration BEFORE it composes and emits
    the graph, because ``baseline_profile``'s staleness guard demands the
    declaration already carry the candidate's crossover. An emit-time refusal
    therefore arrives one durable write too late: the graph is correctly
    refused, but ``/sound`` is left declaring a crossover the speaker is not
    playing and cannot be made to play. Running the same rule here — before the
    write — means a refused apply changes nothing at all. A gate that only runs
    downstream of a commit is a gate the commit has already escaped.
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
