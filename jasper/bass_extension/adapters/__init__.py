"""Enclosure-specific models used by the offline bass-fit command."""

from .base import EnclosureAdapter
from .sealed import SEALED_ADAPTER


def adapter_for_enclosure(enclosure_kind: str) -> EnclosureAdapter | None:
    return SEALED_ADAPTER if enclosure_kind == "sealed" else None


__all__ = ["SEALED_ADAPTER", "adapter_for_enclosure"]
