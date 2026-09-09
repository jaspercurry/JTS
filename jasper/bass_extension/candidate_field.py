# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bass family a candidate carries, and which of its targets may play.

Layer-2's candidate field: one enclosure adapter's target family, ordered
deepest first and keyed by listening level (ADR-0259 section 1), sized under
one headroom budget (ADR-0257 section 3) against a plant fitted in situ
(ADR-0260 section 3).

This module owns the HARD STOP, and it is not the prescriber's to move: a
target spending boost headroom is REFUSED without both protection evidences —
the ladder evidence for its level and the limiter evidence bundle. Until those
artifacts exist, the natural target (boost 0) is the only member a family may
carry, which is the "natural at rest" emission that already ships.

The prescription door composes the field; this is the independent second
check at the persistence boundary, on
``jasper.active_speaker.crossover_v2.room_prescription``'s rule that the
composing door and the persisted value are checked by different code.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Sequence

from jasper.active_speaker._common import require_sha256_hex
from jasper.bass_extension import BASS_EXTENSION_RUNTIME_ADAPTER_IDS
from jasper.bass_extension.refusals import BassExtensionRefusal
from jasper.json_fields import finite_float

__all__ = [
    "BASS_EXTENSION_CANDIDATE_FIELD",
    "EVIDENCE_PASS",
    "NATURAL_TARGET_ID",
    "NO_BASS_EXTENSION_PROFILE_SUMMARY",
    "BassCandidateFieldError",
    "applied_bass_extension_field",
    "bass_extension_summary",
    "graph_summary",
    "validate_bass_extension_field",
]

#: The candidate field an accepted bass prescription lands in.
BASS_EXTENSION_CANDIDATE_FIELD = "bass_extension"

#: The family member that spends no boost: the plant's own corner.
NATURAL_TARGET_ID = "natural"

#: The verdict an evidence document must carry to protect anything.
EVIDENCE_PASS = "pass"

#: Graph authority for a speaker carrying no family: the emitted graph must
#: hold no bass stage at all.
NO_BASS_EXTENSION_PROFILE_SUMMARY: Mapping[str, Any] = MappingProxyType({
    "authority_valid": True,
    "runtime_block_required": False,
})

#: The plant a seat-cube fit can describe. Wider than any real cabinet corner:
#: this is the "not a typo, not a unit error" bound, not a design bound.
_PLANT_F0_MIN_HZ, _PLANT_F0_MAX_HZ = 15.0, 200.0
_PLANT_Q_MIN, _PLANT_Q_MAX = 0.3, 1.5

#: Float round-trip noise between the family generator and this reader; never
#: enough to bridge a different transform.
_FILTER_REL_TOL = 1e-6

_FIELD_KEYS = frozenset({
    "owner", "adapter_id", "margin_policy_name", "effective_plant", "rungs",
    "basis",
})
_OWNER_KEYS = frozenset({"role", "channels"})
_PLANT_KEYS = frozenset({"f0_hz", "q0", "source"})
_RUNG_KEYS = frozenset({"target", "max_level_db", "lt_boost_db", "protection"})
_TARGET_KEYS = frozenset({
    "target_id", "fp_hz", "qp", "filters", "boost_headroom_db", "subsonic",
    "limiter_threshold_dbfs",
})
_SUBSONIC_KEYS = frozenset({"type", "freq", "order"})
_PROTECTION_KEYS = frozenset({"ladder", "limiter"})
_BASIS_KEYS = frozenset({"round_id", "bass_fit_sha256"})


class BassCandidateFieldError(ValueError):
    """One bass_extension field this module would not accept, and why."""

    def __init__(self, reason: BassExtensionRefusal, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _refuse(reason: BassExtensionRefusal, detail: str) -> NoReturn:
    raise BassCandidateFieldError(reason, detail)


def _exact_keys(
    raw: Any,
    keys: frozenset[str],
    *,
    reason: BassExtensionRefusal,
    name: str,
) -> Mapping[str, Any]:
    """One object whose key set is exactly ``keys`` — no extras, none missing."""
    if not isinstance(raw, Mapping) or set(raw) != keys:
        _refuse(reason, f"{name} keys must be exactly {sorted(keys)}")
    return raw


def _sha256(value: Any, *, reason: BassExtensionRefusal, name: str) -> str:
    try:
        return require_sha256_hex(value, name, ValueError)
    except ValueError as exc:
        _refuse(reason, str(exc))


def _number(value: Any, *, reason: BassExtensionRefusal, name: str) -> float:
    """One numeric field, strictly — no coercion, no bools, no strings."""
    number = finite_float(value)
    if number is None:
        _refuse(reason, f"{name} must be a finite number")
    return number


def _text(value: Any, *, reason: BassExtensionRefusal, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _refuse(reason, f"{name} must be a non-empty string")
    return value


def _owner(raw: Any) -> dict[str, Any]:
    invalid = BassExtensionRefusal.OWNER_INVALID
    owner = _exact_keys(raw, _OWNER_KEYS, reason=invalid, name="owner")
    channels = owner["channels"]
    if (
        not isinstance(channels, Sequence)
        or isinstance(channels, (str, bytes))
        or not channels
    ):
        _refuse(invalid, "owner.channels must be a non-empty list")
    resolved: list[int] = []
    for channel in channels:
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
            _refuse(invalid, "owner.channels must be non-negative integers")
        resolved.append(channel)
    if len(set(resolved)) != len(resolved):
        _refuse(invalid, "owner.channels must name each channel once")
    return {
        "role": _text(owner["role"], reason=invalid, name="owner.role"),
        "channels": resolved,
    }


def _effective_plant(raw: Any) -> dict[str, Any]:
    invalid = BassExtensionRefusal.PLANT_INVALID
    plant = _exact_keys(raw, _PLANT_KEYS, reason=invalid, name="effective_plant")
    f0_hz = _number(plant["f0_hz"], reason=invalid, name="effective_plant.f0_hz")
    q0 = _number(plant["q0"], reason=invalid, name="effective_plant.q0")
    if not _PLANT_F0_MIN_HZ <= f0_hz <= _PLANT_F0_MAX_HZ:
        _refuse(
            invalid,
            f"effective_plant.f0_hz must be within {_PLANT_F0_MIN_HZ:g}.."
            f"{_PLANT_F0_MAX_HZ:g} Hz",
        )
    if not _PLANT_Q_MIN <= q0 <= _PLANT_Q_MAX:
        _refuse(
            invalid,
            f"effective_plant.q0 must be within {_PLANT_Q_MIN:g}..{_PLANT_Q_MAX:g}",
        )
    return {
        "f0_hz": f0_hz,
        "q0": q0,
        "source": _text(
            plant["source"], reason=invalid, name="effective_plant.source"
        ),
    }


def _filter(raw: Any, *, where: str) -> dict[str, Any]:
    """One filter's shape: a named type, and finite numbers under every other key."""
    invalid = BassExtensionRefusal.TARGET_INVALID
    if not isinstance(raw, Mapping) or "type" not in raw:
        _refuse(invalid, f"{where} must be an object naming its type")
    entry: dict[str, Any] = {
        "type": _text(raw["type"], reason=invalid, name=f"{where} type")
    }
    for key, value in raw.items():
        if key == "type":
            continue
        if not isinstance(key, str):
            _refuse(invalid, f"{where} field names must be strings")
        entry[key] = _number(value, reason=invalid, name=f"{where} {key}")
    return entry


def _subsonic(raw: Any, *, where: str) -> dict[str, Any] | None:
    invalid = BassExtensionRefusal.TARGET_INVALID
    if raw is None:
        return None
    subsonic = _exact_keys(
        raw, _SUBSONIC_KEYS, reason=invalid, name=f"{where} subsonic"
    )
    freq = _number(subsonic["freq"], reason=invalid, name=f"{where} subsonic freq")
    order = subsonic["order"]
    if freq <= 0.0 or isinstance(order, bool) or not isinstance(order, int) or order < 1:
        _refuse(invalid, f"{where} subsonic must state a positive freq and order")
    return {
        "type": _text(
            subsonic["type"], reason=invalid, name=f"{where} subsonic type"
        ),
        "freq": freq,
        "order": order,
    }


def _target(raw: Any, *, where: str) -> dict[str, Any]:
    invalid = BassExtensionRefusal.TARGET_INVALID
    target = _exact_keys(raw, _TARGET_KEYS, reason=invalid, name=f"{where} target")
    fp_hz = _number(target["fp_hz"], reason=invalid, name=f"{where} fp_hz")
    if fp_hz <= 0.0:
        _refuse(invalid, f"{where} fp_hz must be positive")
    qp = None
    if target["qp"] is not None:
        qp = _number(target["qp"], reason=invalid, name=f"{where} qp")
        if qp <= 0.0:
            _refuse(invalid, f"{where} qp must be positive")
    boost = _number(target["boost_headroom_db"], reason=invalid, name=f"{where} boost")
    if boost < 0.0:
        _refuse(invalid, f"{where} boost_headroom_db must not be negative")
    filters_raw = target["filters"]
    if not isinstance(filters_raw, Sequence) or isinstance(filters_raw, (str, bytes)):
        _refuse(invalid, f"{where} filters must be a list")
    threshold = target["limiter_threshold_dbfs"]
    return {
        "target_id": _text(
            target["target_id"], reason=invalid, name=f"{where} target_id"
        ),
        "fp_hz": fp_hz,
        "qp": qp,
        "filters": [
            _filter(entry, where=f"{where} filter {position}")
            for position, entry in enumerate(filters_raw)
        ],
        "boost_headroom_db": boost,
        "subsonic": _subsonic(target["subsonic"], where=where),
        "limiter_threshold_dbfs": (
            None
            if threshold is None
            else _number(threshold, reason=invalid, name=f"{where} limiter_threshold")
        ),
    }


def _evidence(raw: Any, *, where: str, levelled: bool) -> dict[str, Any] | None:
    """One protection evidence, or ``None`` where none is attached.

    Extra keys are KEPT: the evidence is another door's artifact and this
    reader checks only what admission turns on.
    """
    invalid = BassExtensionRefusal.PROTECTION_INVALID
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _refuse(invalid, f"{where} must be an object")
    _sha256(raw.get("sha256"), reason=invalid, name=f"{where}.sha256")
    if raw.get("verdict") != EVIDENCE_PASS:
        _refuse(invalid, f"{where}.verdict must be {EVIDENCE_PASS!r}")
    entry = {key: value for key, value in raw.items() if isinstance(key, str)}
    if levelled:
        level = _number(
            raw.get("max_level_db"), reason=invalid, name=f"{where}.max_level_db"
        )
        if level > 0.0:
            _refuse(invalid, f"{where}.max_level_db must not be positive")
        entry["max_level_db"] = level
    return entry


def _protection(raw: Any, *, where: str, boost_headroom_db: float) -> dict[str, Any]:
    """THE hard stop, at the persistence boundary.

    A target spending boost headroom is refused outright without both
    evidences — the ladder that measured its level and the limiter bundle that
    bounds it. The prescriber chooses which targets to adopt and never how
    loud one may play.
    """
    invalid = BassExtensionRefusal.PROTECTION_INVALID
    protection = _exact_keys(
        raw, _PROTECTION_KEYS, reason=invalid, name=f"{where} protection"
    )
    resolved = {
        "ladder": _evidence(
            protection["ladder"], where=f"{where} ladder", levelled=True
        ),
        "limiter": _evidence(
            protection["limiter"], where=f"{where} limiter", levelled=False
        ),
    }
    if boost_headroom_db > 0.0:
        missing = [name for name, value in resolved.items() if value is None]
        if missing:
            _refuse(
                invalid,
                f"{where} spends {boost_headroom_db} dB of boost, and a boosted "
                f"target may only be carried with protection measured for it: "
                f"{', '.join(missing)} missing",
            )
    return resolved


def _sealed_filters(target: Mapping[str, Any], plant: Mapping[str, Any], where: str) -> None:
    """A non-natural target IS its Linkwitz transform, recomputed here."""
    # lazy: alignment carries the numpy response models, and this module sits on
    # the emitter's and the runtime proof's import paths.
    from jasper.bass_extension.alignment import linkwitz_transform_params

    invalid = BassExtensionRefusal.TARGET_INVALID
    filters = target["filters"]
    if len(filters) != 1 or filters[0]["type"] != "LinkwitzTransform":
        _refuse(invalid, f"{where} must carry exactly one LinkwitzTransform filter")
    if target["fp_hz"] >= plant["f0_hz"]:
        _refuse(invalid, f"{where} fp_hz must sit below the plant's f0_hz")
    try:
        expected = linkwitz_transform_params(
            plant["f0_hz"], plant["q0"], target["fp_hz"], target["qp"]
        )
    except (TypeError, ValueError) as exc:
        _refuse(invalid, f"{where} names a transform outside the alignment domain: {exc}")
    if set(filters[0]) != set(expected):
        _refuse(invalid, f"{where} transform fields must be exactly {sorted(expected)}")
    for key, value in expected.items():
        actual = filters[0][key]
        if isinstance(value, str):
            if actual != value:
                _refuse(invalid, f"{where} transform {key} must be {value!r}")
        elif not math.isclose(float(actual), float(value), rel_tol=_FILTER_REL_TOL):
            _refuse(
                invalid,
                f"{where} transform {key} is {actual!r}, and this plant and "
                f"corner compose {value!r}",
            )


def _rungs(raw: Any, *, plant: Mapping[str, Any]) -> list[dict[str, Any]]:
    invalid = BassExtensionRefusal.TARGET_INVALID
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        _refuse(invalid, "rungs must be a non-empty list")
    rungs: list[dict[str, Any]] = []
    for position, raw_rung in enumerate(raw):
        where = f"rung {position}"
        entry = _exact_keys(raw_rung, _RUNG_KEYS, reason=invalid, name=where)
        target = _target(entry["target"], where=where)
        lt_boost = _number(entry["lt_boost_db"], reason=invalid, name=f"{where} lt_boost_db")
        if lt_boost < 0.0:
            _refuse(invalid, f"{where} lt_boost_db must not be negative")
        max_level_db = _number(
            entry["max_level_db"],
            reason=BassExtensionRefusal.TARGET_LEVEL_INVALID,
            name=f"{where} max_level_db",
        )
        if max_level_db > 0.0:
            _refuse(
                BassExtensionRefusal.TARGET_LEVEL_INVALID,
                f"{where} max_level_db must not be positive",
            )
        rungs.append({
            "target": target,
            "max_level_db": max_level_db,
            "lt_boost_db": lt_boost,
            "protection": _protection(
                entry["protection"],
                where=where,
                boost_headroom_db=target["boost_headroom_db"],
            ),
        })
    _check_order(rungs)
    _check_levels(rungs)
    for position, rung in enumerate(rungs[:-1]):
        _sealed_filters(rung["target"], plant, f"rung {position}")
    return rungs


def _check_order(rungs: Sequence[Mapping[str, Any]]) -> None:
    """The family's own order: deepest first, natural last."""
    unordered = BassExtensionRefusal.TARGETS_UNORDERED
    corners = [float(rung["target"]["fp_hz"]) for rung in rungs]
    if any(later <= earlier for earlier, later in zip(corners, corners[1:])):
        _refuse(unordered, "rungs must run deepest first, by strictly rising fp_hz")
    natural = rungs[-1]["target"]
    if natural["target_id"] != NATURAL_TARGET_ID:
        _refuse(unordered, f"the last rung must be the {NATURAL_TARGET_ID!r} target")
    if (
        natural["filters"]
        or natural["boost_headroom_db"] != 0.0
        or rungs[-1]["lt_boost_db"] != 0.0
    ):
        _refuse(
            BassExtensionRefusal.TARGET_INVALID,
            f"the {NATURAL_TARGET_ID!r} target is the plant's own corner: no "
            "filters, and no boost to spend",
        )
    if rungs[-1]["max_level_db"] != 0.0:
        _refuse(
            BassExtensionRefusal.TARGET_LEVEL_INVALID,
            f"the {NATURAL_TARGET_ID!r} target costs no level",
        )


def _check_levels(rungs: Sequence[Mapping[str, Any]]) -> None:
    """Level is CODE's, and a rung below 0 dB must show the ladder that measured it."""
    invalid = BassExtensionRefusal.TARGET_LEVEL_INVALID
    levels = [float(rung["max_level_db"]) for rung in rungs]
    if any(later < earlier for earlier, later in zip(levels, levels[1:])):
        _refuse(invalid, "max_level_db must not fall as the family runs toward natural")
    for position, rung in enumerate(rungs):
        if rung["max_level_db"] == 0.0:
            continue
        evidence = rung["protection"]["ladder"]
        if evidence is None:
            _refuse(
                invalid,
                f"rung {position} claims a level bound with no ladder evidence to "
                "have measured it",
            )
        if float(evidence["max_level_db"]) != rung["max_level_db"]:
            _refuse(
                invalid,
                f"rung {position} states a level its own ladder evidence does not",
            )


def validate_bass_extension_field(value: Mapping[str, Any]) -> dict[str, Any]:
    """The bass family ``value`` claims, refused whole if it breaks a bound.

    Returns a plain-JSON copy: every number a ``float`` or ``int``, every
    unknown key already refused, so a caller can persist the result without a
    second walk.
    """
    # lazy: targets reaches scipy through adapters, and this module sits on
    # MeasuredCrossoverCandidate's own import path.
    from jasper.bass_extension.targets import MARGINS

    field = _exact_keys(
        value, _FIELD_KEYS, reason=BassExtensionRefusal.FIELD_MALFORMED,
        name="bass_extension",
    )
    adapter_id = field["adapter_id"]
    # The RUNTIME's set, not the fitter's: a family this speaker cannot emit
    # must not reach the applied profile at all.
    if (
        not isinstance(adapter_id, str)
        or adapter_id not in BASS_EXTENSION_RUNTIME_ADAPTER_IDS
    ):
        _refuse(
            BassExtensionRefusal.ADAPTER_UNKNOWN,
            f"adapter_id must be one of {sorted(BASS_EXTENSION_RUNTIME_ADAPTER_IDS)}",
        )
    margin = field["margin_policy_name"]
    if not isinstance(margin, str) or margin not in MARGINS:
        _refuse(
            BassExtensionRefusal.FIELD_MALFORMED,
            f"margin_policy_name must be one of {sorted(MARGINS)}",
        )
    plant = _effective_plant(field["effective_plant"])
    basis = _exact_keys(
        field["basis"], _BASIS_KEYS, reason=BassExtensionRefusal.BASIS_INVALID,
        name="basis",
    )
    return {
        "owner": _owner(field["owner"]),
        "adapter_id": adapter_id,
        "margin_policy_name": margin,
        "effective_plant": plant,
        "rungs": _rungs(field["rungs"], plant=plant),
        "basis": {
            "round_id": _text(
                basis["round_id"],
                reason=BassExtensionRefusal.BASIS_INVALID,
                name="basis.round_id",
            ),
            "bass_fit_sha256": _sha256(
                basis["bass_fit_sha256"],
                reason=BassExtensionRefusal.BASIS_INVALID,
                name="basis.bass_fit_sha256",
            ),
        },
    }


def bass_extension_summary(field: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What ``/state`` and the bass tab publish about the applied family.

    ``None`` for a speaker carrying no family, and for one whose field this
    module would refuse: a summary of a record that could not be applied would
    read as a commissioned speaker.
    """
    if not field:
        return None
    try:
        validated = validate_bass_extension_field(field)
    except BassCandidateFieldError:
        return None
    return {
        "commissioned": True,
        "adapter_id": validated["adapter_id"],
        "margin_policy_name": validated["margin_policy_name"],
        "owner": validated["owner"],
        "effective_plant": validated["effective_plant"],
        "targets": [
            {
                "target_id": rung["target"]["target_id"],
                "fp_hz": rung["target"]["fp_hz"],
                "boost_headroom_db": rung["target"]["boost_headroom_db"],
                "max_level_db": rung["max_level_db"],
            }
            for rung in validated["rungs"]
        ],
        "basis": validated["basis"],
    }


def applied_bass_extension_field(
    applied_profile: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The bass family one baseline profile carries, or ``None``.

    THE accessor for the applied candidate's ``bass_extension``: the emitter
    and every production runtime proof read the family here and nowhere else.
    """
    snapshot = (
        applied_profile.get("recomposition_snapshot")
        if isinstance(applied_profile, Mapping)
        else None
    )
    field = (
        snapshot.get(BASS_EXTENSION_CANDIDATE_FIELD)
        if isinstance(snapshot, Mapping)
        else None
    )
    return field if isinstance(field, Mapping) and field else None


def graph_summary(field: Mapping[str, Any] | None) -> dict[str, Any]:
    """The authority evidence one emitted graph is proved against.

    A field this module would refuse authorizes no stage at all, exactly as an
    absent family does: the graph proof then requires the complete absence of
    the bass block rather than trusting an unreadable family.
    """
    if not field:
        return dict(NO_BASS_EXTENSION_PROFILE_SUMMARY)
    try:
        validated = validate_bass_extension_field(field)
    except BassCandidateFieldError:
        return dict(NO_BASS_EXTENSION_PROFILE_SUMMARY)
    natural = validated["rungs"][-1]["target"]
    return {
        "authority_valid": True,
        "runtime_block_required": True,
        "bass_owner_channels": list(validated["owner"]["channels"]),
        "natural": {
            "fp_hz": natural["fp_hz"],
            "qp": natural["qp"],
            "boost_headroom_db": natural["boost_headroom_db"],
            "subsonic": (
                dict(natural["subsonic"])
                if natural["subsonic"] is not None
                else None
            ),
        },
    }
