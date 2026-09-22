# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Optional installed-hardware facts and conditional estimates; no DSP authority."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from jasper.json_fields import CodedFieldError, JsonFields


INSTALLATION_FIELDS: dict[str, dict[str, Any]] = {
    "horn_model": {"label": "Horn or waveguide", "type": "text"},
    "coil_wiring": {"label": "Voice-coil wiring", "type": "text"},
    "amplifier_model": {"label": "Amplifier model", "type": "text"},
    "amplifier_gain_control": {"label": "Amplifier gain control", "type": "text"},
    "supply_voltage_v": {"label": "Power supply (V)", "type": "number"},
    "supply_current_a": {"label": "Power supply rating (A)", "type": "number"},
    "net_volume_l": {"label": "Internal air volume (litres)", "type": "number"},
    "passive_radiator_model": {"label": "Passive radiator model", "type": "text", "enclosure": "passive_radiator"},
    "passive_radiator_count": {"label": "Number of passive radiators", "type": "number", "integer": True, "enclosure": "passive_radiator"},
    "passive_radiator_added_mass_g": {"label": "Added weight per passive radiator (g)", "type": "number", "allow_zero": True, "enclosure": "passive_radiator"},
    "port_area_cm2": {"label": "Total port opening area (cm²)", "type": "number", "enclosure": "vented"},
    "port_length_cm": {"label": "Port length (cm)", "type": "number", "enclosure": "vented"},
}
_FIELDS = JsonFields(CodedFieldError)


def normalise_installation(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    raw = _FIELDS.mapping(raw, "installation")
    if set(raw) - INSTALLATION_FIELDS.keys():
        raise CodedFieldError("installation has unknown fields", code="unknown_installation_fields")
    result: dict[str, Any] = {}
    for key, spec in INSTALLATION_FIELDS.items():
        value = raw.get(key)
        if value is None or value == "":
            continue
        if spec["type"] == "text":
            result[key] = _FIELDS.text(value, key)
            continue
        if isinstance(value, bool):
            raise CodedFieldError(f"{key} must be numeric", code="field_not_numeric")
        value = _FIELDS.finite_number(value, key)
        if spec.get("allow_zero"):
            if value < 0:
                raise CodedFieldError(f"{key} must be >= 0", code="field_negative")
        elif value <= 0:
            raise CodedFieldError(f"{key} must be > 0", code="field_not_positive")
        if spec.get("integer") and not value.is_integer():
            raise CodedFieldError(f"{key} must be a whole number", code="field_not_integer")
        result[key] = value
    return result or None


def installation_evidence(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Read the saved declaration; never persist a second copy of estimates."""
    rows = []
    for driver in (draft.get("manual_settings") or {}).get("drivers", []):
        facts = normalise_installation(driver.get("installation"))
        if not facts:
            continue
        voltage = facts.get("supply_voltage_v")
        estimate = None
        if voltage is not None:
            # Ideal BTL sine: Vpeak <= supply, Vrms = Vpeak / sqrt(2).
            # https://www.ti.com/lit/ds/symlink/tpa3255.pdf (BTL output topology).
            rms = voltage / math.sqrt(2)
            estimate = {"ideal_btl_rms_voltage_ceiling_v": rms,
                "assumptions": "Bridged output, adequate supply current, no amplifier losses.",
                "scope": "Electrical upper bound only. Clean output and remaining headroom need amplifier gain and output-voltage calibration."}
        rows.append({"target_id": driver.get("target_id"), "role": driver["role"],
            "inputs": facts, "amplifier_estimate": estimate,
            "acoustic_limit": {"status": "not_estimated", "reason": "Cabinet and radiator inputs guide microphone trials; they do not establish excursion or clean-output limits."}})
    return {"source": "design_draft.manual_settings.drivers[].installation",
        "provenance": "operator_entered", "drivers": rows}


def installation_view(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {**draft, "installation": {"fields": INSTALLATION_FIELDS, **installation_evidence(draft)}}
