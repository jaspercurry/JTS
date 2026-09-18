# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-line driver pad (L-pad / series-resistor attenuator) modeling.

Pure computation only: no I/O, no product policy, shared field validation. A
pad is always operator-declared — never AI-researched.

Formula (verified against JTS3's tweeter pad, 2026-07-23), where a series-only
pad degenerates to the bare driver impedance::

    R_par = Z * shunt / (Z + shunt)          (or Z when there is no shunt)
    attenuation_db = 20 * log10(R_par / (series + R_par))
    effective_impedance_ohm = series + R_par

A ``direct_db`` pad skips the topology: the operator enters the attenuation
directly, and its effective impedance is left unset rather than guessed.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.json_fields import CodedFieldError
from ._common import DriverFields

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


class DriverPadError(CodedFieldError):
    """Raised when a declared driver pad is malformed or under-specified."""


_fields = DriverFields(DriverPadError)


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

    Idempotence contract: this function's own output is a legal input to
    itself, so a persisted record re-normalises without stripping derived keys.
    For ``l_pad`` / ``series_resistor``, ``attenuation_db`` and
    ``effective_impedance_ohm`` are OUTPUTS: present on input, they are ignored
    and recomputed, never rejected and never taken on faith. For ``direct_db``,
    ``attenuation_db`` is genuinely an INPUT and stays required, while
    ``effective_impedance_ohm`` is ignored and dropped. A resistor field on a
    ``direct_db`` pad IS rejected — a kind mismatch, not a derived-field echo.

    Raises :class:`DriverPadError` when a required field is missing, an
    irrelevant field is set for the chosen kind, or (``l_pad`` /
    ``series_resistor``) the record declares no ``nominal_impedance_ohm``: the
    formula never assumes a default impedance.
    """

    if raw is None or raw == "":
        return None
    raw = _fields.mapping(raw, field_name)
    unknown = sorted(str(key) for key in raw if key not in _PAD_FIELDS)
    if unknown:
        raise DriverPadError(f"{field_name} has unknown fields: {', '.join(unknown)}", code="unknown_pad_fields")
    kind = raw.get("kind")
    if kind is None:
        raise DriverPadError(f"{field_name}.kind is required", code="field_required")
    if kind not in PAD_KINDS:
        raise DriverPadError(f"{field_name}.kind must be one of {PAD_KINDS}", code="field_unsupported")
    if kind == "none":
        return None

    series = _fields._positive_float(raw.get("series_ohm"), f"{field_name}.series_ohm")
    shunt = _fields._positive_float(raw.get("shunt_ohm"), f"{field_name}.shunt_ohm")

    if kind == "direct_db":
        if series is not None or shunt is not None:
            raise DriverPadError(
                f"{field_name} must not declare resistor values for kind=direct_db", code="pad_field_not_applicable"
            )
        direct_db = _fields._finite_float(
            raw.get("attenuation_db"), f"{field_name}.attenuation_db"
        )
        if direct_db is None:
            raise DriverPadError(
                f"{field_name}.attenuation_db is required for kind=direct_db", code="field_required"
            )
        if direct_db > 0:
            raise DriverPadError(f"{field_name}.attenuation_db must be <= 0", code="pad_attenuation_positive")
        # No meaning for a bare dB figure: accepted so a saved record's own
        # derived-output echo round-trips, but never stored.
        return {"kind": kind, "attenuation_db": direct_db}

    # attenuation_db and effective_impedance_ohm are OUTPUTS here, computed
    # below from the resistor values: either key present on input is ignored
    # and recomputed, never validated, so the value is never read.
    if series is None:
        raise DriverPadError(f"{field_name}.series_ohm is required for kind={kind}", code="field_required")
    if kind == "series_resistor" and shunt is not None:
        raise DriverPadError(f"{field_name}.shunt_ohm is only valid for kind=l_pad", code="pad_field_not_applicable")
    if kind == "l_pad" and shunt is None:
        raise DriverPadError(f"{field_name}.shunt_ohm is required for kind=l_pad", code="field_required")
    if nominal_impedance_ohm is None:
        raise DriverPadError(
            f"{field_name.rsplit('.', 1)[0]}.nominal_impedance_ohm is required", code="field_required"
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
