# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE room-correction PEQ set, prescribed from outside this process.

This door owns the room class: the per-side filter sets, the round's own
spatial median as the evidence they are measured against, and the order the
gates run in. Every limit it applies — the per-frequency cut floor, the boost
cap, the taper below the ceiling, and the evidence a boost must show — is
:mod:`jasper.audio_measurement.room_limits`', never a second opinion here
about the physics.

Shape and posture are :mod:`.blend_prescription`'s, and what the two doors
share is imported from it rather than restated: the prohibited-key walk, the
intake readers, the filter record, the composed grid, the refusal values whose
meaning is identical, and the exception class.

`See ADR-0256` rules 1-2 and `docs/room-correction-regime-plan.md` D5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

import numpy as np

from jasper.active_speaker._common import require_sha256_hex
from jasper.active_speaker.branch_chain import chain_response
from jasper.audio_measurement.room_boundary import (
    CEILING_SOURCES,
    ROOM_FLOOR_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    ROOM_MEDIAN_WINDOW,
)
from jasper.audio_measurement.room_limits import (
    ROOM_MAX_FILTER_BOOST_DB,
    ROOM_MAX_FILTERS_PER_SIDE,
    ROOM_MAX_TOTAL_BOOST_DB,
    ROOM_PEQ_Q_MAX,
    ROOM_PEQ_Q_MIN,
    BoostAdmission,
    admit_boost,
    boost_cap_db,
    cut_floor_db,
)
from jasper.camilla_config_contract import PeqFilter, total_positive_boost_db
from jasper.json_fields import finite_float

from .blend_prescription import (
    COMPOSED_BOOST_EXCEEDED,
    FILTER_BOOST_TOO_HIGH,
    FILTER_COUNT_EXCEEDED,
    FILTER_MALFORMED,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    PRESCRIPTION_PROHIBITED_FIELD,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PROHIBITED_PRESCRIPTION_KEYS,
    RATIONALE_MAX_CHARS,
    BlendPrescriptionRefused,
    composed_grid,
    find_prohibited_keys,
    # Renamed only to stay distinct from this module's own identifiers: the
    # VALUES are that door's, which is what makes one vocabulary cover both.
    BLEND_PRESCRIPTION_MALFORMED as PRESCRIPTION_MALFORMED,
    BLEND_PRESCRIPTION_PROVENANCE_MISSING as PRESCRIPTION_PROVENANCE_MISSING,
    # Shared with the blend door rather than re-typed: this door raises the
    # same exception class and refuses under both readers' own values.
    _FILTER_FIELDS,
    _prescriber,
    _rationale,
    _refuse,
)

__all__ = [
    "BOOST_NOT_ADMITTED",
    "FILTER_CUT_TOO_DEEP",
    "LAYOUT_UNAVAILABLE",
    "ROOM_COMPOSED_TOLERANCE_DB",
    "ROOM_MEDIAN_FIELD",
    "ROOM_MEDIAN_MISMATCH",
    "ROOM_MEDIAN_UNAVAILABLE",
    "ROOM_PRESCRIPTION_KIND",
    "ROOM_PRESCRIPTION_REFUSAL_REASONS",
    "ROOM_PRESCRIPTION_SCHEMA_VERSION",
    "SIDE_MALFORMED",
    "TAPER_VIOLATED",
    "RoomMedian",
    "RoomPrescription",
    "RoomPrescriptionRefused",
    "read_room_median",
    "read_room_prescription",
    "room_prescription_response_format",
    "room_prescription_to_candidate_fields",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: A proposal naming a version this build does not speak is refused, never
#: best-effort parsed.
ROOM_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, and what the CLI switches its evidence on.
ROOM_PRESCRIPTION_KIND = "jts_room_prescription"

#: The median field a proposal must echo back.
ROOM_MEDIAN_FIELD = "room_median_sha256"

#: Slack, dB, on the COMPOSED cascade's per-frequency allowance. A Q >= 1 bell
#: still leaves a skirt at the knee where the taper has closed the allowance to
#: zero, so an exact bound would refuse every filter placed near the ceiling
#: for arithmetic that spends no audible level.
ROOM_COMPOSED_TOLERANCE_DB = 0.5


# --------------------------------------------------------------------------- #
# refusal vocabulary — closed, by slug, never by prose
# --------------------------------------------------------------------------- #

#: The echoed digest names a different median than the one supplied.
ROOM_MEDIAN_MISMATCH = "room_median_mismatch"
#: No median artifact, or one this door cannot read into limits.
ROOM_MEDIAN_UNAVAILABLE = "room_median_unavailable"
#: No readable applied profile, so nothing can say which sides this speaker
#: declares -- the median's sibling: evidence the door must have to judge at all.
LAYOUT_UNAVAILABLE = "layout_unavailable"
#: A cut past the depth this bin's cross-position spread supports.
FILTER_CUT_TOO_DEEP = "filter_cut_too_deep"
#: A boost the spatial evidence does not admit; the evidence carries the
#: :class:`~jasper.audio_measurement.room_limits.BoostAdmission` finding.
BOOST_NOT_ADMITTED = "boost_not_admitted"
#: The composed cascade sits outside the tapered allowance somewhere.
TAPER_VIOLATED = "taper_violated"
#: ``sides`` is not a mapping of side name to filter list.
SIDE_MALFORMED = "side_malformed"

ROOM_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    PRESCRIPTION_MALFORMED,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_PROHIBITED_FIELD,
    FILTER_MALFORMED,
    FILTER_COUNT_EXCEEDED,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    FILTER_BOOST_TOO_HIGH,
    COMPOSED_BOOST_EXCEEDED,
    ROOM_MEDIAN_MISMATCH,
    ROOM_MEDIAN_UNAVAILABLE,
    LAYOUT_UNAVAILABLE,
    FILTER_CUT_TOO_DEEP,
    BOOST_NOT_ADMITTED,
    TAPER_VIOLATED,
    SIDE_MALFORMED,
})

#: The candidate field an accepted room prescription lands in.
ROOM_CANDIDATE_FIELD = "room_correction"

#: One refusal class for both doors, so the CLI's one handler and every
#: ``except`` on the seam keep working.
RoomPrescriptionRefused = BlendPrescriptionRefused

#: Top-level fields a proposal may carry. Anything else is refused rather than
#: ignored: a misspelled ``sides`` that silently dropped the prescription would
#: leave the gate accepting an empty one.
_PRESCRIPTION_FIELDS = frozenset({
    "artifact_schema_version",
    "kind",
    ROOM_MEDIAN_FIELD,
    "prescriber",
    "sides",
    "rationale",
})


# --------------------------------------------------------------------------- #
# the evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class RoomMedian:
    """One round's spatial median, reduced to what the bounds are computed from.

    ``deviations_db`` is positions x bins, each row a position's deviation FROM
    ``median_db`` — so a position's own level at a bin is the sum of the two.
    ``ceiling_hz`` arrives from the median document and is never derived here.
    """

    freqs_hz: np.ndarray
    median_db: np.ndarray
    spread_db: np.ndarray
    deviations_db: np.ndarray
    n_positions: int
    ceiling_hz: float
    ceiling_source: str
    #: The level ``median_db`` was read against: the producer's median curve's
    #: own median over the band, dB.
    level_reference_db: float = 0.0

    @property
    def band_hz(self) -> tuple[float, float]:
        """The band a prescription against this median may place filters in."""
        return (ROOM_FLOOR_HZ, self.ceiling_hz)


def _unavailable(detail: str, **evidence: Any) -> NoReturn:
    _refuse(ROOM_MEDIAN_UNAVAILABLE, detail, **evidence)


def _median_array(raw: Any, field: str, *, length: int | None = None) -> np.ndarray:
    """One finite 1-D array off the median document, or a refusal."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        _unavailable(f"{field} must be a list of numbers")
    values = [finite_float(value) for value in raw]
    if any(value is None for value in values):
        _unavailable(f"{field} must be finite numbers")
    if length is not None and len(values) != length:
        _unavailable(
            f"{field} carries {len(values)} value(s) against a "
            f"{length}-bin frequency grid"
        )
    return np.asarray(values, dtype=np.float64)


def read_room_median(raw: Mapping[str, Any]) -> RoomMedian:
    """Lane B's ``room_median.json`` as a value, or a refusal naming the fault.

    Strict on everything a bound is computed from — one finite array per bin, a
    strictly increasing grid that spans the whole prescribable band, one
    deviation row per declared position — because a median read loosely would
    make the limits a property of the artifact's mistakes. Every fault is
    ``room_median_unavailable``: a median that cannot be read is evidence this
    door does not have, whatever the reason.
    """
    if not isinstance(raw, Mapping):
        _unavailable(f"a room median must be a mapping, got {type(raw).__name__}")
    freqs = _median_array(raw.get("freqs_hz"), "freqs_hz")
    if freqs.size < 2 or not np.all(np.diff(freqs) > 0.0) or freqs[0] <= 0.0:
        _unavailable("freqs_hz must be a strictly increasing positive grid")
    bins = int(freqs.size)
    median_db = _median_array(raw.get("median_db"), "median_db", length=bins)
    spread_db = _median_array(raw.get("spread_db"), "spread_db", length=bins)
    if np.any(spread_db < 0.0):
        _unavailable("spread_db is a population sigma and cannot be negative")

    ceiling = finite_float(raw.get("ceiling_hz"))
    if ceiling is None or not ROOM_BOUNDARY_MIN_HZ <= ceiling <= ROOM_BOUNDARY_MAX_HZ:
        _unavailable(
            "ceiling_hz must sit within "
            f"{ROOM_BOUNDARY_MIN_HZ:g}-{ROOM_BOUNDARY_MAX_HZ:g} Hz"
        )
    if freqs[0] < ROOM_FLOOR_HZ or freqs[-1] > ceiling:
        _unavailable(
            f"the median's grid ({freqs[0]:.1f}-{freqs[-1]:.1f} Hz) reaches "
            f"outside the room band {ROOM_FLOOR_HZ:g}-{ceiling:.1f} Hz"
        )
    source = raw.get("ceiling_source")
    if source not in CEILING_SOURCES:
        _unavailable(f"ceiling_source must be one of {sorted(CEILING_SOURCES)}")
    window = raw.get("window")
    if window != ROOM_MEDIAN_WINDOW:
        _unavailable(
            f"a room median must be read {ROOM_MEDIAN_WINDOW}, got {window!r}",
            window=window,
        )

    positions = raw.get("positions")
    if isinstance(positions, (str, bytes)) or not isinstance(positions, Sequence):
        _unavailable("positions must be a list of position records")
    declared = raw.get("n_positions")
    if not isinstance(declared, int) or isinstance(declared, bool):
        _unavailable("n_positions must be an integer")
    if declared != len(positions):
        _unavailable(
            f"n_positions is {declared} against {len(positions)} position record(s)"
        )
    rows: list[np.ndarray] = []
    for index, entry in enumerate(positions):
        if not isinstance(entry, Mapping):
            _unavailable(f"position {index} must be an object")
        rows.append(
            _median_array(
                entry.get("deviation_db"),
                f"position {index} deviation_db",
                length=bins,
            )
        )
    # The producer writes the median at measurement level; a room correction
    # moves shape, never level, so the trend is read against its own robust
    # level over the band and that reference is disclosed.
    level_db = float(np.median(median_db))
    return RoomMedian(
        freqs_hz=freqs,
        median_db=median_db - level_db,
        spread_db=spread_db,
        level_reference_db=level_db,
        deviations_db=(
            np.vstack(rows) if rows else np.zeros((0, bins), dtype=np.float64)
        ),
        n_positions=len(rows),
        ceiling_hz=float(ceiling),
        ceiling_source=str(source),
    )


# --------------------------------------------------------------------------- #
# the prescription
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoomPrescription:
    """A validated room correction and the evidence that admits its boosts.

    ``sides`` is a TOTAL per side, not a delta — the whole modal-band set the
    next round applies.
    """

    #: Side name -> the prescribed biquads, in emission order, reduced to the
    #: record the candidate field and the emitter both speak.
    sides: Mapping[str, tuple[dict[str, Any], ...]]
    #: ``"cut"`` when every gain is non-positive, ``"boost"`` when any is
    #: positive. The receipt's attribution key, spelled as the blend class
    #: spells it.
    prescription_class: str
    #: The median document this answered, content-addressed.
    room_median_sha256: str
    prescriber_model: str
    prescriber_operator: str
    #: The band's upper edge and where it came from, echoed from the median.
    ceiling_hz: float
    ceiling_source: str
    #: The round the median belongs to, for the candidate's basis.
    round_id: str
    #: One finding per boosting filter, in emission order. Empty on a
    #: cut-class prescription.
    admissions: tuple[BoostAdmission, ...] = ()
    #: The largest per-side sum of positive gains, dB, and the level cost that
    #: spend charges. They are equal by construction: a boost is paid for by
    #: turning everything else down.
    boost_db_total: float = 0.0
    level_cost_db: float = 0.0
    #: The prescriber's own words. NEVER parsed for behaviour.
    rationale: str = ""
    rationale_dropped_chars: int | None = None

    @property
    def filters(self) -> list[dict[str, Any]]:
        """Every filter, flat, each naming its side — the printer's view."""
        return [
            {"side": side, **entry}
            for side, entries in self.sides.items()
            for entry in entries
        ]

    @property
    def band_hz(self) -> tuple[float, float]:
        return (ROOM_FLOOR_HZ, self.ceiling_hz)

    @property
    def admitted_boosts_hz(self) -> list[float]:
        return [finding.freq_hz for finding in self.admissions]

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was prescribed, and what admits it."""
        return {
            "artifact_schema_version": ROOM_PRESCRIPTION_SCHEMA_VERSION,
            "kind": ROOM_PRESCRIPTION_KIND,
            "prescription_class": self.prescription_class,
            "sides": {
                side: [dict(entry) for entry in entries]
                for side, entries in self.sides.items()
            },
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            "ceiling_hz": self.ceiling_hz,
            "ceiling_source": self.ceiling_source,
            "round_id": self.round_id,
            ROOM_MEDIAN_FIELD: self.room_median_sha256,
            "prescriber": {
                "model": self.prescriber_model,
                "operator": self.prescriber_operator,
            },
            "admissions": [finding.to_dict() for finding in self.admissions],
            "boost_db_total": self.boost_db_total,
            "level_cost_db": self.level_cost_db,
            "rationale": self.rationale,
            "rationale_dropped_chars": self.rationale_dropped_chars,
        }


def room_prescription_response_format() -> dict[str, Any]:
    """The contract a room prescriber must satisfy, as data.

    One owner for the instructions and the gate, so the two cannot describe
    different shapes. A PURE CONSTANT: nothing measured or household-authored
    reaches it.
    """
    return {
        "artifact_schema_version": ROOM_PRESCRIPTION_SCHEMA_VERSION,
        "kind": "jts_room_prescription_contract",
        "required_top_level": {
            "artifact_schema_version": ROOM_PRESCRIPTION_SCHEMA_VERSION,
            "kind": ROOM_PRESCRIPTION_KIND,
            ROOM_MEDIAN_FIELD: (
                "copy the sha256 of the room median document you were given; "
                "a prescription naming a different median is refused"
            ),
            "prescriber": {
                "model": "the model that authored this",
                "operator": "the person who ran it",
            },
            "sides": (
                "one entry per side this speaker declares, keyed by exactly "
                "those side names (a mono layout declares one side, named "
                f"`mono`), each 0 to {ROOM_MAX_FILTERS_PER_SIDE} objects of "
                "{freq: <Hz>, q: <number>, gain: <dB>}"
            ),
        },
        "optional_top_level": {
            "rationale": (
                f"free text; the first {RATIONALE_MAX_CHARS} characters are "
                "banked and the excess is dropped with its count disclosed. "
                "It is NEVER parsed for behaviour: no argument made here can "
                "widen a bound below"
            ),
        },
        "filters_are_a_total": (
            "prescribe the WHOLE room set the next round should apply, not a "
            "delta against the incumbent"
        ),
        "bounds": {
            "band_hz": (
                f"{ROOM_FLOOR_HZ:g} Hz to the median's own ceiling_hz, above "
                "which the direct-sound stage owns the band"
            ),
            "q_range": [ROOM_PEQ_Q_MIN, ROOM_PEQ_Q_MAX],
            "max_filters_per_side": ROOM_MAX_FILTERS_PER_SIDE,
            "max_filter_boost_db": ROOM_MAX_FILTER_BOOST_DB,
            "max_total_boost_db": ROOM_MAX_TOTAL_BOOST_DB,
            "cut_depth_is_per_frequency": (
                "a cut may not go below the depth that bin's cross-position "
                "spread supports; both that floor and the boost cap are "
                "scaled to zero over the third of an octave below the "
                "ceiling, and the COMPOSED cascade is checked against them"
            ),
            "composed_tolerance_db": ROOM_COMPOSED_TOLERANCE_DB,
        },
        "boost_admission": (
            "a positive gain must be a dip the seats agree on: at least "
            "three positions, present at 70% of them, deep enough to matter "
            "and shallow enough to be a mode rather than an interference "
            "null, and wide enough for a bell. An un-admitted boost is "
            "refused with the finding attached"
        ),
        "refusal_reasons": sorted(ROOM_PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(PROHIBITED_PRESCRIPTION_KEYS),
        "execution_boundary": {
            "model_may_propose": True,
            "model_may_execute": False,
            "model_may_grade_itself": False,
            "jts_validates_and_measures": True,
        },
    }


# --------------------------------------------------------------------------- #
# the request gate
# --------------------------------------------------------------------------- #


def _number(value: Any, *, reason: str, field: str) -> float:
    """One numeric field, strictly — no coercion, no bools, no strings."""
    number = finite_float(value)
    if number is None:
        _refuse(reason, f"{field} must be a finite number")
    return float(number)


def _parse_filter(side: str, position: int, entry: Any) -> dict[str, Any]:
    """One filter's SHAPE, and none of its bounds."""
    where = f"side {side!r} filter {position}"
    if not isinstance(entry, Mapping):
        _refuse(FILTER_MALFORMED, f"{where} must be an object")
    unknown = sorted(set(entry) - _FILTER_FIELDS)
    if unknown:
        _refuse(
            FILTER_MALFORMED, f"{where} carries unknown field(s): {', '.join(unknown)}"
        )
    if entry.get("biquad_type", "Peaking") != "Peaking":
        _refuse(
            FILTER_MALFORMED,
            f"{where} must be a Peaking biquad, got {entry.get('biquad_type')!r}",
        )
    freq = _number(entry.get("freq"), reason=FILTER_MALFORMED, field=f"{where} freq")
    if freq <= 0.0:
        _refuse(FILTER_MALFORMED, f"{where} freq must be positive")
    return {
        "freq": freq,
        "q": _number(entry.get("q"), reason=FILTER_MALFORMED, field=f"{where} q"),
        "gain": _number(
            entry.get("gain"), reason=FILTER_MALFORMED, field=f"{where} gain"
        ),
    }


def _parse_sides(raw: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    """The per-side filter lists' shape; the door checks the NAMES against the
    layout's own sides once the shape is known."""
    if not isinstance(raw, Mapping) or not raw:
        _refuse(
            SIDE_MALFORMED,
            "a room prescription must state a non-empty sides object keyed by "
            "side name",
        )
    sides: dict[str, tuple[dict[str, Any], ...]] = {}
    for name, entries in raw.items():
        side = " ".join(str(name).split())
        if not side:
            _refuse(SIDE_MALFORMED, "a side name must be non-blank")
        if side in sides:
            _refuse(
                SIDE_MALFORMED,
                f"side {side!r} is named more than once",
                side=side,
            )
        if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
            _refuse(SIDE_MALFORMED, f"side {side!r} must carry a list of filters")
        sides[side] = tuple(
            _parse_filter(side, position, entry)
            for position, entry in enumerate(entries)
        )
    return sides


def _parse_prescription(
    raw: Mapping[str, Any],
) -> tuple[dict[str, tuple[dict[str, Any], ...]], str, str, str, str, int]:
    """Shape, identity and provenance — and none of the bounds."""
    if not isinstance(raw, Mapping):
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    # BEFORE the unknown-field check: every prohibited key is also an unknown
    # one, so checking shape first would report a prescriber reaching for
    # `volume_db` as a typo.
    prohibited = sorted(set(find_prohibited_keys(raw)))
    if prohibited:
        _refuse(
            PRESCRIPTION_PROHIBITED_FIELD,
            f"a prescription may not name {', '.join(prohibited)}: it supplies "
            "numbers into a fixed shape, never configuration, coefficients, or "
            "a per-role value",
            prohibited=prohibited,
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    if raw.get("kind") != ROOM_PRESCRIPTION_KIND:
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"a prescription must name kind={ROOM_PRESCRIPTION_KIND!r}, got "
            f"{raw.get('kind')!r}",
        )
    version = raw.get("artifact_schema_version")
    if version != ROOM_PRESCRIPTION_SCHEMA_VERSION:
        _refuse(
            PRESCRIPTION_SCHEMA_UNSUPPORTED,
            "this build speaks room prescription schema "
            f"{ROOM_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
            supported=ROOM_PRESCRIPTION_SCHEMA_VERSION,
        )
    try:
        echoed = require_sha256_hex(
            raw.get(ROOM_MEDIAN_FIELD), ROOM_MEDIAN_FIELD, ValueError
        )
    except ValueError as exc:
        _refuse(PRESCRIPTION_PROVENANCE_MISSING, str(exc))
    model, operator = _prescriber(raw.get("prescriber"))
    rationale, dropped = _rationale(raw.get("rationale"))
    return _parse_sides(raw.get("sides")), echoed, model, operator, rationale, dropped


def _checked_median(
    median: RoomMedian | None, supplied_sha256: Any, echoed: str, round_id: str
) -> RoomMedian:
    """The evidence this proposal is measured against, or why there is none.

    Content-addressed like the blend door's packet fingerprint: a proposal
    answering a median nobody supplied is refused rather than graded against
    evidence it never saw.
    """
    if median is None:
        _unavailable("no room median was supplied to check this prescription against")
    if not isinstance(supplied_sha256, str) or not supplied_sha256:
        _unavailable("the supplied room median carries no digest to compare against")
    if not round_id.strip():
        _unavailable("no round names the supplied room median")
    if echoed != supplied_sha256:
        _refuse(
            ROOM_MEDIAN_MISMATCH,
            f"this prescription answers a different room median "
            f"({echoed[:12]}...) than the one supplied "
            f"({supplied_sha256[:12]}...)",
            prescription_answers=echoed,
            median_is=supplied_sha256,
        )
    return median


def _check_bounds(
    sides: Mapping[str, tuple[dict[str, Any], ...]],
    median: RoomMedian,
    floor_db: np.ndarray,
) -> str:
    """Every per-filter bound, and the class the gains add up to.

    The two depth bounds are per-FREQUENCY and come from the median: the cut
    floor is what this bin's spread supports, the boost cap is D5's ceiling,
    and both are already tapered toward the ceiling.
    """
    lo, hi = median.band_hz
    boosts = 0
    for side, entries in sides.items():
        for position, entry in enumerate(entries):
            where = f"side {side!r} filter {position}"
            freq, q, gain = entry["freq"], entry["q"], entry["gain"]
            if not lo <= freq <= hi:
                _refuse(
                    FILTER_OUTSIDE_REGION,
                    f"{where} at {freq:.1f} Hz is outside the room band "
                    f"{lo:.1f}-{hi:.1f} Hz",
                    freq_hz=freq,
                    band_hz=[lo, hi],
                )
            if not ROOM_PEQ_Q_MIN <= q <= ROOM_PEQ_Q_MAX:
                _refuse(
                    FILTER_Q_OUT_OF_RANGE,
                    f"{where} Q {q:g} is outside {ROOM_PEQ_Q_MIN:g}-"
                    f"{ROOM_PEQ_Q_MAX:g}",
                    q=q,
                    q_range=[ROOM_PEQ_Q_MIN, ROOM_PEQ_Q_MAX],
                )
            # BEFORE the underflow probe below, which OVERFLOWS above
            # ~+12330 dB: a gain past the absolute cap can never be legal, so
            # it refuses by slug rather than raising out of the arithmetic.
            if gain > ROOM_MAX_FILTER_BOOST_DB:
                _refuse(
                    FILTER_BOOST_TOO_HIGH,
                    f"{where} boosts {gain:.2f} dB, past the "
                    f"{ROOM_MAX_FILTER_BOOST_DB:g} dB a room filter may spend",
                    gain_db=gain,
                    max_boost_db=ROOM_MAX_FILTER_BOOST_DB,
                    freq_hz=freq,
                )
            # 10**(gain/40) is exactly 0.0 below ~-12960 dB, and the biquad
            # evaluator divides by it.
            if 10.0 ** (gain / 40.0) == 0.0:
                _refuse(
                    FILTER_MALFORMED,
                    f"{where} gain {gain:g} dB underflows 64-bit arithmetic "
                    "and cannot be evaluated or emitted",
                )
            if gain > 0.0:
                cap = float(boost_cap_db(freq, median.ceiling_hz))
                if gain > cap:
                    _refuse(
                        FILTER_BOOST_TOO_HIGH,
                        f"{where} boosts {gain:.2f} dB, past the {cap:.2f} dB "
                        f"allowed at {freq:.1f} Hz",
                        gain_db=gain,
                        max_boost_db=cap,
                        freq_hz=freq,
                    )
                boosts += 1
                continue
            allowed = float(np.interp(freq, median.freqs_hz, floor_db))
            if gain < allowed:
                _refuse(
                    FILTER_CUT_TOO_DEEP,
                    f"{where} cuts {gain:.2f} dB, past the {allowed:.2f} dB "
                    f"the spread at {freq:.1f} Hz supports",
                    gain_db=gain,
                    cut_floor_db=allowed,
                    freq_hz=freq,
                )
    return "boost" if boosts else "cut"


def _check_boosts(
    sides: Mapping[str, tuple[dict[str, Any], ...]], median: RoomMedian
) -> tuple[BoostAdmission, ...]:
    """The spatial bar, per boosting filter. **It refuses on the first miss.**

    Unlike the summed blend class's positional finding, this one is a GATE:
    feeding an interference null spends headroom on a cancellation that
    swallows it (`See docs/room-correction-regime-plan.md` D5).
    """
    findings: list[BoostAdmission] = []
    for entries in sides.values():
        for entry in entries:
            if entry["gain"] <= 0.0:
                continue
            finding = admit_boost(
                entry["freq"],
                freqs_hz=median.freqs_hz,
                median_db=median.median_db,
                deviations_db=median.deviations_db,
                n_positions=median.n_positions,
            )
            if not finding.admitted:
                _refuse(
                    BOOST_NOT_ADMITTED,
                    f"the cloud does not admit a boost at {entry['freq']:.1f} "
                    f"Hz: {finding.reason}",
                    admission=finding.to_dict(),
                )
            findings.append(finding)
    return tuple(findings)


def _check_composed(
    sides: Mapping[str, tuple[dict[str, Any], ...]],
    median: RoomMedian,
    floor_db: np.ndarray,
) -> float:
    """Per side: the slot count, the boost spend, and the EVALUATED cascade.

    Through ``chain_response``, the ONE biquad evaluator, so this gate and the
    emitter's headroom charge cannot disagree about what CamillaDSP realizes.
    Two filters whose skirts overlap deliver more than either alone — and take
    the cascade past a per-filter bound both filters cleared. Returns the
    largest per-side boost spend, which is what the level costs.
    """
    grid = composed_grid(median.band_hz, median.freqs_hz)
    grid_floor_db = np.interp(grid, median.freqs_hz, floor_db)
    cap_db = boost_cap_db(grid, median.ceiling_hz)
    spend = 0.0
    for side, entries in sides.items():
        if len(entries) > ROOM_MAX_FILTERS_PER_SIDE:
            _refuse(
                FILTER_COUNT_EXCEEDED,
                f"side {side!r} carries {len(entries)} filters, past the "
                f"{ROOM_MAX_FILTERS_PER_SIDE} a side may hold",
                n_filters=len(entries),
                max_filters=ROOM_MAX_FILTERS_PER_SIDE,
            )
        boost = total_positive_boost_db(PeqFilter(**entry) for entry in entries)
        if boost > ROOM_MAX_TOTAL_BOOST_DB:
            _refuse(
                COMPOSED_BOOST_EXCEEDED,
                f"side {side!r} spends {boost:.2f} dB of boost, past the "
                f"{ROOM_MAX_TOTAL_BOOST_DB:g} dB ceiling",
                composed_boost_db=boost,
                max_composed_boost_db=ROOM_MAX_TOTAL_BOOST_DB,
            )
        spend = max(spend, boost)
        if not entries:
            continue
        composed = 20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(entries, grid))), 1e-12)
        )
        over = composed - cap_db
        under = grid_floor_db - composed
        worst = int(np.argmax(np.maximum(over, under)))
        if max(over[worst], under[worst]) > ROOM_COMPOSED_TOLERANCE_DB:
            _refuse(
                TAPER_VIOLATED,
                f"side {side!r} composes to {composed[worst]:+.2f} dB at "
                f"{grid[worst]:.1f} Hz, outside the "
                f"{grid_floor_db[worst]:+.2f}..{cap_db[worst]:+.2f} dB allowed "
                "there",
                freq_hz=float(grid[worst]),
                composed_db=float(composed[worst]),
                cut_floor_db=float(grid_floor_db[worst]),
                boost_cap_db=float(cap_db[worst]),
                tolerance_db=ROOM_COMPOSED_TOLERANCE_DB,
            )
    return float(spend)


def read_room_prescription(
    raw: Mapping[str, Any] | None,
    *,
    room_median: RoomMedian | None,
    room_median_sha256: str | None,
    round_id: str,
    sides: Sequence[str],
) -> RoomPrescription | None:
    """THE request gate. One point, and the one place every bound is applied.

    ``None`` when there is no prescription. Otherwise a validated
    :class:`RoomPrescription`, or :class:`RoomPrescriptionRefused` naming which
    gate said no. ``sides`` is the layout's own declared side names, which the
    document's keys must be exactly.

    Order is deliberate — shape, identity, provenance, per-filter bounds, the
    spatial bar for each boost, then the composed cascade — because each stage
    sends a prescriber somewhere different, and a proposal learns its filters
    are unplaceable before it learns the cascade they compose to is. The bounds
    are INCLUSIVE, so a round's legality does not turn on float noise.
    """
    if raw is None:
        return None
    prescribed, echoed, model, operator, rationale, dropped = _parse_prescription(raw)
    if set(prescribed) != set(sides):
        _refuse(
            SIDE_MALFORMED,
            f"this speaker declares {sorted(sides)}, so a prescription must "
            f"key its sides by exactly those names, not {sorted(prescribed)}",
            expected_sides=sorted(sides),
        )
    median = _checked_median(room_median, room_median_sha256, echoed, round_id)
    # Built once: the per-filter bound and the composed one read the same
    # per-bin floor, on the same grid the median declared it on.
    floor_db = cut_floor_db(median.spread_db, median.freqs_hz, median.ceiling_hz)
    prescription_class = _check_bounds(prescribed, median, floor_db)
    admissions = _check_boosts(prescribed, median)
    boost_db_total = _check_composed(prescribed, median, floor_db)
    return RoomPrescription(
        sides=prescribed,
        prescription_class=prescription_class,
        room_median_sha256=echoed,
        prescriber_model=model,
        prescriber_operator=operator,
        ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source,
        round_id=round_id.strip(),
        admissions=admissions,
        boost_db_total=boost_db_total,
        # One number, two names: the boost is paid for by turning the whole
        # graph down, so what it spends IS what the level costs.
        level_cost_db=boost_db_total,
        rationale=rationale,
        rationale_dropped_chars=dropped,
    )


def room_prescription_to_candidate_fields(
    prescription: RoomPrescription | None,
) -> dict[str, Any]:
    """The candidate fields a validated prescription contributes.

    The value must enter at CANDIDATE-BUILD time:
    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)``, so a
    prescription applied after construction is either invisible to the
    fingerprint or refused as ``candidate_tampered``. ``{}`` for ``None``, so a
    caller can splat it unconditionally.
    """
    if prescription is None:
        return {}
    return {
        ROOM_CANDIDATE_FIELD: {
            "sides": {
                side: [dict(entry) for entry in entries]
                for side, entries in prescription.sides.items()
            },
            "ceiling_hz": prescription.ceiling_hz,
            "ceiling_source": prescription.ceiling_source,
            "basis": {
                "round_id": prescription.round_id,
                ROOM_MEDIAN_FIELD: prescription.room_median_sha256,
                "admitted_boosts_hz": prescription.admitted_boosts_hz,
            },
            "boost_db_total": prescription.boost_db_total,
            "level_cost_db": prescription.level_cost_db,
        }
    }
