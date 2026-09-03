# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read the fitted Pi HAT's ID EEPROM as the firmware exposes it in devicetree.

The EEPROM is the only non-label discriminator for Studio-family HiFiBerry
silicon: rpi-6.18.y's ``hifiberry_studio.c`` names every card in that family
"Hifiberry Studio Soundcard", with no product token and no channel width, so
the ALSA label alone cannot separate an 8-channel Studio DAC8x from a
2-channel Studio Digi (#2258).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .text_property import read_text_property


DEFAULT_HAT_DIR = "/proc/device-tree/hat"


@dataclass(frozen=True)
class HatEeprom:
    """The vendor/product/uuid triple a fitted HAT declares."""

    vendor: str
    product: str
    uuid: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HatEeprom | None":
        vendor = raw.get("vendor")
        product = raw.get("product")
        uuid = raw.get("uuid")
        if not isinstance(vendor, str) or not isinstance(product, str):
            return None
        if not isinstance(uuid, str):
            return None
        return cls(vendor=vendor, product=product, uuid=uuid)

    def to_dict(self) -> dict[str, str]:
        return {"vendor": self.vendor, "product": self.product, "uuid": self.uuid}


def read_hat_eeprom(hat_dir: str | Path = DEFAULT_HAT_DIR) -> HatEeprom | None:
    """Return the fitted HAT's declared identity, or None when there is none.

    A board with no HAT and a kernel that publishes no ``hat`` node both answer
    None: the caller must treat "no EEPROM" as "no extra evidence", never as a
    distinct hardware claim. A node that publishes only some of the three
    fields is still an answer — the blanks simply match no profile's declared
    products, so a partial EEPROM routes nothing.
    """

    root = Path(hat_dir)
    vendor = read_text_property(root / "vendor")
    product = read_text_property(root / "product")
    uuid = read_text_property(root / "uuid")
    if not (vendor or product or uuid):
        return None
    return HatEeprom(vendor=vendor, product=product, uuid=uuid)
