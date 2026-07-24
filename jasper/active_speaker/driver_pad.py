# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-line driver pad (L-pad / series-resistor attenuator) modeling.

Pure computation only, mirroring level_trim.py's shape: no I/O, no product
policy, no cross-module imports. A "pad" is an operator-declared resistor
network (or a purchased fixed attenuator) sitting between the amplifier and
one driver -- a physical fact the operator knows because they wired it, never
something AI-researched (see driver_safety.build_driver_research_prompt's
docstring: the prompt deliberately omits pad).

Formula (verified against JTS3's tweeter pad, 2026-07-23): for a two-resistor
L-pad, the shunt resistor sits in parallel with the driver's own nominal
impedance; the series resistor and that parallel combination form a voltage
divider the amplifier sees as one effective load. A series-only pad is the
same formula with no shunt (the parallel combination degenerates to the bare
driver impedance).

    R_par = Z * shunt / (Z + shunt)          (or Z when there is no shunt)
    attenuation_db = 20 * log10(R_par / (series + R_par))
    effective_impedance_ohm = series + R_par

A ``direct_db`` pad skips this topology entirely: the operator already knows
the attenuation (a datasheet figure for a purchased attenuator, or a bench
measurement) and enters it directly. Its effective impedance is not derivable
from a bare dB figure, so it is left unset rather than guessed.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

# Closed vocabulary for the "kind" of in-line pad a driver can declare.
# "none" and an absent pad field are equivalent (both mean "no attenuation");
# normalise_pad returns None for either so there is exactly one no-pad shape.
PAD_KINDS: tuple[str, ...] = ("none", "series_resistor", "l_pad", "direct_db")

_PAD_FIELDS = {
    "kind",
    "series_ohm",
    "shunt_ohm",
    "attenuation_db",
    "effective_impedance_ohm",
}


class DriverPadError(ValueError):
    """Raised when a declared driver pad is malformed or under-specified."""


def _positive_float(raw: Any, field_name: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise DriverPadError(f"{field_name} must be numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DriverPadError(f"{field_name} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise DriverPadError(f"{field_name} must be > 0")
    return value


def _finite_float(raw: Any, field_name: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise DriverPadError(f"{field_name} must be numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DriverPadError(f"{field_name} must be numeric") from exc
    if not math.isfinite(value):
        raise DriverPadError(f"{field_name} must be finite")
    return value


def normalise_pad(
    raw: Any,
    *,
    nominal_impedance_ohm: float | None,
    field_name: str,
) -> dict[str, Any] | None:
    """Validate one declared in-line driver pad and derive its attenuation.

    ``raw`` is the operator-entered pad record: ``kind`` plus whichever of
    ``series_ohm`` / ``shunt_ohm`` (``l_pad`` / ``series_resistor``) or
    ``attenuation_db`` (``direct_db``) that kind needs. Returns ``None`` for
    an absent pad or an explicit ``kind: "none"``.

    Idempotence contract: ``normalise_pad(normalise_pad(x, ...), ...) ==
    normalise_pad(x, ...)`` for any ``x`` that normalises successfully. This
    function's own output is a legal input to itself -- callers that persist
    the return value and later re-normalise it (e.g. rebuilding a design
    draft from a saved record) must not have to strip derived keys first.
    Concretely: for ``l_pad`` / ``series_resistor``, ``attenuation_db`` and
    ``effective_impedance_ohm`` are OUTPUTS -- if either is present on input
    it is ignored and silently dropped (recomputed from the resistor values
    with no consistency check against the incoming value), never rejected.
    An operator still cannot invent an attenuation for a resistor pad -- it
    is always recomputed, never taken on faith -- so the anti-confusion
    intent survives; only the "reject the field outright" enforcement of it
    is gone. For ``direct_db``, ``attenuation_db`` is the one derived-looking
    key that is genuinely an INPUT for that kind and stays required;
    ``effective_impedance_ohm`` is still ignored-and-dropped (a bare dB
    figure has no impedance model to store one), not rejected. A resistor
    field (``series_ohm`` / ``shunt_ohm``) declared on a ``direct_db`` pad
    remains rejected -- that is a genuine kind mismatch, not a derived-field
    echo.

    Raises :class:`DriverPadError` when a required field is missing, an
    irrelevant field is set for the chosen kind, or (``l_pad`` /
    ``series_resistor``) the record has no declared ``nominal_impedance_ohm``
    -- the formula never assumes a default impedance.
    """

    if raw is None or raw == "":
        return None
    if not isinstance(raw, Mapping):
        raise DriverPadError(f"{field_name} must be an object")
    unknown = sorted(str(key) for key in raw if key not in _PAD_FIELDS)
    if unknown:
        raise DriverPadError(f"{field_name} has unknown fields: {', '.join(unknown)}")
    kind = raw.get("kind")
    if kind not in PAD_KINDS:
        raise DriverPadError(f"{field_name}.kind must be one of {PAD_KINDS}")
    if kind == "none":
        return None

    series = _positive_float(raw.get("series_ohm"), f"{field_name}.series_ohm")
    shunt = _positive_float(raw.get("shunt_ohm"), f"{field_name}.shunt_ohm")

    if kind == "direct_db":
        if series is not None or shunt is not None:
            raise DriverPadError(
                f"{field_name} must not declare resistor values for kind=direct_db"
            )
        direct_db = _finite_float(
            raw.get("attenuation_db"), f"{field_name}.attenuation_db"
        )
        if direct_db is None:
            raise DriverPadError(
                f"{field_name}.attenuation_db is required for kind=direct_db"
            )
        if direct_db > 0:
            raise DriverPadError(f"{field_name}.attenuation_db must be <= 0")
        # effective_impedance_ohm has no meaning for a bare dB figure. It is
        # accepted (see _PAD_FIELDS) so a saved record's own derived-output
        # echo round-trips, but never stored -- direct_db has no impedance
        # model to store one in.
        return {"kind": kind, "attenuation_db": direct_db}

    # l_pad / series_resistor: attenuation_db and effective_impedance_ohm are
    # OUTPUTS, computed below from the resistor values. Either key may be
    # present on input -- typically a verbatim echo of this function's own
    # last output -- and is ignored and recomputed rather than validated, so
    # a saved pad record re-normalises cleanly (see the idempotence contract
    # in the docstring above). Deliberately not even type-checked here: the
    # value is never read, so there's nothing to validate.
    if series is None:
        raise DriverPadError(f"{field_name}.series_ohm is required for kind={kind}")
    if kind == "series_resistor" and shunt is not None:
        raise DriverPadError(f"{field_name}.shunt_ohm is only valid for kind=l_pad")
    if kind == "l_pad" and shunt is None:
        raise DriverPadError(f"{field_name}.shunt_ohm is required for kind=l_pad")
    if nominal_impedance_ohm is None:
        raise DriverPadError(
            f"{field_name} requires nominal_impedance_ohm on the same record"
        )

    impedance = float(nominal_impedance_ohm)
    r_par = impedance * shunt / (impedance + shunt) if shunt is not None else impedance
    attenuation_db = 20.0 * math.log10(r_par / (series + r_par))
    effective_impedance_ohm = series + r_par
    out: dict[str, Any] = {
        "kind": kind,
        "series_ohm": series,
        "attenuation_db": round(attenuation_db, 1),
        "effective_impedance_ohm": round(effective_impedance_ohm, 1),
    }
    if shunt is not None:
        out["shunt_ohm"] = shunt
    return out


def effective_sensitivity_db(
    naked_db: float | None, pad: Mapping[str, Any] | None
) -> float | None:
    """Fold a declared pad's attenuation into a driver's naked sensitivity.

    Returns ``None`` when ``naked_db`` itself is ``None`` -- an undeclared
    sensitivity stays undeclared; a pad never invents one. A missing or
    malformed ``pad`` (no ``attenuation_db``) leaves ``naked_db`` unchanged.
    """

    if naked_db is None:
        return None
    if not isinstance(pad, Mapping):
        return float(naked_db)
    attenuation = pad.get("attenuation_db")
    if not isinstance(attenuation, (int, float)) or isinstance(attenuation, bool):
        return float(naked_db)
    return float(naked_db) + float(attenuation)
