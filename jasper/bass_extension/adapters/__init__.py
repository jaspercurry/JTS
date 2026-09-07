"""Enclosure-specific bass-extension adapters."""

from __future__ import annotations

from typing import cast

from .base import EnclosureAdapter
from .passive_radiator import PASSIVE_RADIATOR_ADAPTER, PassiveRadiatorPlantFit
from .ported import PORTED_ADAPTER, PortedPlantFit
from .sealed import SEALED_ADAPTER, SealedPlantFit

ADAPTERS: dict[str, EnclosureAdapter] = {
    "sealed_v1": cast(EnclosureAdapter, SEALED_ADAPTER),
    "ported_v1": cast(EnclosureAdapter, PORTED_ADAPTER),
    "passive_radiator_v1": cast(EnclosureAdapter, PASSIVE_RADIATOR_ADAPTER),
}


def adapter_for_enclosure(enclosure_kind: str) -> EnclosureAdapter | None:
    adapter_id = {
        "sealed": "sealed_v1",
        "vented": "ported_v1",
        "passive_radiator": "passive_radiator_v1",
    }.get(enclosure_kind)
    return ADAPTERS.get(adapter_id) if adapter_id is not None else None


__all__ = [
    "ADAPTERS",
    "adapter_for_enclosure",
    "PassiveRadiatorPlantFit",
    "PortedPlantFit",
    "SealedPlantFit",
]
