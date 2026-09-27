# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared field helpers for versioned JSON artifacts, and the one owner of their
identity rules: canonical JSON bytes, the strict and lenient fingerprints, the
strict freeze, SHA-256 hex checks and text/file hashing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from calendar import timegm
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

#: Read size for :func:`sha256_file` — bounded for the 415 MB Pi Zero 2 W.
_HASH_CHUNK_BYTES = 1 << 16

#: The one Zulu-stamp literal in the tree, shared by utc_now_iso/parse_utc_iso.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


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


def require_finite(
    value: Any, *, field: str, error: Callable[[str], Exception] = ValueError, positive: bool = False,
) -> float:
    """:func:`finite_float`, or ``error`` naming ``field``; ``positive`` also
    refuses zero and below."""
    number = finite_float(value)
    if number is None:
        raise error(f"{field} must be a finite number")
    if positive and number <= 0.0:
        raise error(f"{field} must be positive")
    return number


def as_float(value: Any) -> float | None:
    """``value`` coerced by ``float()``, or ``None`` when it refuses it — the
    lenient sibling of :func:`finite_float`: numeric strings and ``bool``
    convert, and NaN and infinities pass through."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def utc_now_iso() -> str:
    """The wall-clock stamp artifacts carry, e.g. ``2026-09-07T12:34:56Z``."""
    return time.strftime(_ISO_FORMAT, time.gmtime())


def parse_utc_iso(text: str) -> int | None:
    """Inverse of :func:`utc_now_iso`, or ``None`` when ``text`` isn't one."""
    try:
        return timegm(time.strptime(str(text), _ISO_FORMAT))
    except (TypeError, ValueError):
        return None


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


def sha256_text(text: str) -> str:
    """SHA-256 hex of ``text``'s UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_sha256_hex(
    value: Any, *, field: str, error: Callable[[str], Exception] = ValueError,
) -> str:
    """``value`` when it is a lowercase SHA-256 hex digest, else ``error``
    naming ``field`` — never a repair: padding and uppercase are refused."""
    if isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None:
        return value
    raise error(f"{field} must be a lowercase SHA-256 fingerprint")


def freeze_json(
    value: Any, *, field: str, error: Callable[[str], Exception] = ValueError, path: str = "$",
) -> Any:
    """A fresh copy of ``value`` in JSON's exact data model — ``None``,
    ``bool``, ``int``, ``str``, finite ``float``, ``list`` and ``str``-keyed
    mappings (copied to ``dict``) — else ``error`` naming ``field`` and the
    ``path`` of the first refusal. Stricter than :func:`canonical_json_bytes`
    on purpose: that writes a tuple as a list and ``{1: x}`` as ``{"1": x}``,
    and an exact identity must not let two inputs share one encoding."""
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise error(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, nested in value.items():
            if type(key) is not str:
                raise error(f"{field} contains a non-string key at {path}")
            frozen[key] = freeze_json(nested, field=field, error=error, path=f"{path}.{key}")
        return frozen
    if type(value) is list:
        return [
            freeze_json(nested, field=field, error=error, path=f"{path}[{index}]")
            for index, nested in enumerate(value)
        ]
    raise error(f"{field} contains a non-JSON value at {path}")


def canonical_json_bytes(value: Any) -> bytes:
    """The strict canonical JSON encoding identities hash and persist: sorted
    keys, no whitespace, ASCII only, finite numbers only (``ValueError`` on
    NaN or infinity). Any change re-fingerprints every persisted record.
    :func:`lenient_json_fingerprint` is a different rule with its own
    persisted fingerprints; do not fold the two together."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def json_fingerprint(mapping: Mapping[str, Any]) -> str:
    """SHA-256 hex of one mapping's :func:`canonical_json_bytes`."""
    return hashlib.sha256(canonical_json_bytes(mapping)).hexdigest()


def lenient_json_fingerprint(value: Any) -> str:
    """SHA-256 hex of ``value`` under the older, lenient encoding: sorted keys
    and no whitespace, but ``default=str`` stringifies any non-JSON value and
    NaN or infinity passes through, so a ``Path`` hashes like its string and a
    tuple like a list. Topology, baseline, measurement and wake-corpus
    fingerprints on disk were minted with it, so its bytes must never change;
    a new identity uses :func:`json_fingerprint`."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CodedFieldError(ValueError):
    """A field refusal whose ``code`` a caller can branch on."""

    code: str  # Present only for an explicit code or a subclass default.

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class JsonFields:
    """Parse common JSON field shapes using a domain-owned error type."""

    error_type: type[CodedFieldError]
    length_limit_separator: str = ""

    def mapping(self, raw: Any, field_name: str) -> Mapping[str, Any]:
        if not isinstance(raw, Mapping):
            raise self.error_type(f"{field_name} must be an object", code="field_not_object")
        return raw

    def sequence(self, raw: Any, field_name: str) -> list[Any]:
        if not isinstance(raw, list):
            raise self.error_type(f"{field_name} must be a list", code="field_not_list")
        return raw

    def require_id(self, value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error_type(f"{field_name} is required", code="field_required")
        result = value.strip()
        if not _SAFE_ID_RE.match(result):
            raise self.error_type(
                f"{field_name} must be <=80 chars and contain only safe id chars", code="field_invalid_id"
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
            raise self.error_type(f"{field_name} is required", code="field_required")
        result = " ".join(value.split())
        if len(result) > max_length:
            raise self.error_type(
                f"{field_name} must be <={self.length_limit_separator}"
                f"{max_length} chars", code="field_too_long"
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
            raise self.error_type(type_error_message or f"{field_name} is required", code="field_not_string")
        result = " ".join(value.split())
        if not result and not allow_blank:
            raise self.error_type(f"{field_name} is required", code="field_required")
        if len(result) > max_length:
            length_name = length_field_name or field_name
            raise self.error_type(
                f"{length_name} must be <={self.length_limit_separator}"
                f"{max_length} chars", code="field_too_long"
            )
        return result

    def integer(self, value: Any, field_name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise self.error_type(f"{field_name} must be an integer", code="field_not_integer") from exc

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
        raise self.error_type(f"{field_name} must be boolean", code="field_not_boolean")

    def enum(
        self,
        value: Any,
        field_name: str,
        supported: Collection[str],
    ) -> str:
        if not isinstance(value, str):
            raise self.error_type(f"{field_name} must be a string", code="field_not_string")
        token = value.strip()
        if token not in supported:
            raise self.error_type(f"{field_name} is unsupported: {token}", code="field_unsupported")
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
            raise self.error_type(f"{field_name} must be numeric", code="field_not_numeric") from exc
        if not math.isfinite(result):
            raise self.error_type(f"{field_name} must be finite", code="field_not_finite")
        return result

    def optional_number(self, value: Any, field_name: str) -> float | None:
        if value is None or value == "":
            return None
        return self.finite_number(value, field_name)


def as_mapping(value: Any) -> Mapping[str, Any]:
    """``value`` when it is an object, else an empty one — so a chain of
    ``.get()`` hops over an absent branch stays a lookup, not a crash."""
    return value if isinstance(value, Mapping) else {}
