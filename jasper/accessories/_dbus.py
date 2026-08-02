# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small DBus value adapters shared by accessory integrations."""

from __future__ import annotations

from typing import Any


def variant_value(value: Any) -> Any:
    """Unwrap a dbus-next Variant while accepting already-plain values."""
    return getattr(value, "value", value)
