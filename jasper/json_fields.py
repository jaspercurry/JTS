# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small shared field helpers for versioned JSON artifacts.

Artifact modules keep ownership of their schemas and error classes. This leaf
centralizes the rules they all repeat: the scalar/container checks used while
turning an untyped JSON mapping into a domain model, and the three stamps
those artifacts carry — a UTC timestamp, a file digest, a canonical-JSON
fingerprint. Loading and publishing an artifact stays ``atomic_io``'s job.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Collection, Mapping

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")

#: Read size for :func:`sha256_file` — bounded for the 415 MB Pi Zero 2 W.
_HASH_CHUNK_BYTES = 1 << 16


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


def utc_now_iso() -> str:
    """The wall-clock stamp artifacts carry, e.g. ``2026-09-07T12:34:56Z``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def age_seconds(epoch: float) -> float:
    """Seconds since ``epoch``, floored at zero so a backwards clock step
    cannot publish a negative age."""
    return max(0.0, round(time.time() - epoch, 1))


def sha256_file(path: str | os.PathLike[str]) -> str:
    """SHA-256 hex of a file's bytes, read in bounded chunks.

    Streamed rather than slurped: a downloaded model is tens of megabytes and
    the Pi Zero 2 W has 415 MB of RAM. Raises ``OSError`` like any other read —
    a caller that wants a sentinel instead catches it.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_fingerprint(mapping: Mapping[str, Any]) -> str:
    """SHA-256 hex of one mapping's canonical JSON: sorted keys, no spaces."""
    canonical = json.dumps(
        mapping,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
