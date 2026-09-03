# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small shared reader and field parser for versioned JSON artifacts.

Artifact modules keep ownership of their schemas and error classes. This leaf
only centralizes getting an untyped JSON mapping off disk and the repeated
scalar/container rules used while turning one into those domain models.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


def read_json_mapping(path: Path) -> dict[str, Any] | None:
    """One JSON object off disk, or ``None`` — never raises. ``None``
    covers every way the artifact can fail to be a mapping: missing,
    unreadable, not UTF-8, unparsable, or a non-object top level."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def finite_float(value: Any) -> float | None:
    """One real number out of untyped JSON, or ``None`` — never a coercion.

    ``bool`` is an ``int`` and a numeric string is something ``float`` accepts,
    so both are rejected; an arbitrary-precision ``int`` is legal JSON and
    raises ``OverflowError`` rather than returning ``inf``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class JsonFields:
    """Parse common JSON field shapes using a domain-owned error type."""

    error_type: type[Exception]
    length_limit_separator: str = ""

    def mapping(self, raw: Any, field_name: str) -> Mapping[str, Any]:
        if not isinstance(raw, Mapping):
            raise self.error_type(f"{field_name} must be an object")
        return raw

    def sequence(self, raw: Any, field_name: str) -> list[Any]:
        if not isinstance(raw, list):
            raise self.error_type(f"{field_name} must be a list")
        return raw

    def require_id(self, value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error_type(f"{field_name} is required")
        result = value.strip()
        if not _SAFE_ID_RE.match(result):
            raise self.error_type(
                f"{field_name} must be <=80 chars and contain only safe id chars"
            )
        return result

    def optional_id(
        self,
        value: Any,
        field_name: str = "optional id",
    ) -> str | None:
        if value is None or value == "":
            return None
        return self.require_id(value, field_name)

    def text(
        self,
        value: Any,
        field_name: str,
        *,
        default: str | None = None,
        max_length: int = 120,
    ) -> str:
        if value is None and default is not None:
            return default
        if not isinstance(value, str) or not value.strip():
            raise self.error_type(f"{field_name} is required")
        result = " ".join(value.split())
        if len(result) > max_length:
            raise self.error_type(
                f"{field_name} must be <={self.length_limit_separator}"
                f"{max_length} chars"
            )
        return result

    def optional_text(
        self,
        value: Any,
        field_name: str,
        *,
        max_length: int = 240,
        allow_blank: bool = False,
        type_error_message: str | None = None,
        length_field_name: str | None = None,
    ) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise self.error_type(type_error_message or f"{field_name} is required")
        result = " ".join(value.split())
        if not result and not allow_blank:
            raise self.error_type(f"{field_name} is required")
        if len(result) > max_length:
            length_name = length_field_name or field_name
            raise self.error_type(
                f"{length_name} must be <={self.length_limit_separator}"
                f"{max_length} chars"
            )
        return result

    def integer(self, value: Any, field_name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise self.error_type(f"{field_name} must be an integer") from exc

    def optional_integer(self, value: Any, field_name: str) -> int | None:
        if value is None or value == "":
            return None
        return self.integer(value, field_name)

    @staticmethod
    def boolean(value: Any, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def strict_boolean(self, value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        raise self.error_type(f"{field_name} must be boolean")

    def enum(
        self,
        value: Any,
        field_name: str,
        supported: Collection[str],
    ) -> str:
        if not isinstance(value, str):
            raise self.error_type(f"{field_name} must be a string")
        token = value.strip()
        if token not in supported:
            raise self.error_type(f"{field_name} is unsupported: {token}")
        return token

    def number(
        self,
        value: Any,
        field_name: str,
        *,
        default: float = 0.0,
    ) -> float:
        if value is None or value == "":
            return default
        return self.finite_number(value, field_name)

    def finite_number(self, value: Any, field_name: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise self.error_type(f"{field_name} must be numeric") from exc
        if not math.isfinite(result):
            raise self.error_type(f"{field_name} must be finite")
        return result

    def optional_number(self, value: Any, field_name: str) -> float | None:
        if value is None or value == "":
            return None
        return self.finite_number(value, field_name)
