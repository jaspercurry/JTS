# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Publish a HAT ID EEPROM node the way devicetree does, for tests."""

from __future__ import annotations

from pathlib import Path


def write_hat_eeprom(
    hat_dir: Path,
    *,
    vendor: str | None = "HiFiBerry",
    product: str | None = None,
    uuid: str | None = "be3b8164-dd7b-48fc-ab27-79dd7c641980",
) -> Path:
    """Write the properties as NUL-terminated strings, as the firmware does.

    A ``None`` field is left unpublished, which is how a partial node reaches
    the reader.
    """

    hat_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("vendor", vendor), ("product", product), ("uuid", uuid)):
        if value is not None:
            (hat_dir / name).write_bytes(value.encode("utf-8") + b"\x00")
    return hat_dir
