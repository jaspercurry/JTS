# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lab override artifact for audio runtime settings.

Runtime defaults and hardware floors live in code/profile data. Temporary lab
tuning belongs here, with a reason and optional expiry, rather than in
``/etc/jasper/jasper.env`` where it looks permanent and can shadow reconciler
state months later.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text


SCHEMA_VERSION = 1
KIND = "jts_audio_runtime_overrides"
DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH = "/var/lib/jasper/audio_runtime_overrides.json"
AUDIO_RUNTIME_OVERRIDES_PATH_ENV = "JASPER_AUDIO_RUNTIME_OVERRIDES_PATH"


def runtime_overrides_path(env: Mapping[str, str] | None = None) -> str:
    """Resolve the override artifact path from env, falling back to production."""

    values = os.environ if env is None else env
    raw = str(values.get(AUDIO_RUNTIME_OVERRIDES_PATH_ENV, "")).strip()
    return raw or DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH


@dataclass(frozen=True)
class RuntimeOverrideEntry:
    key: str
    value: str
    reason: str
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {
            "value": self.value,
            "reason": self.reason,
        }
        if self.created_at:
            out["created_at"] = self.created_at
        if self.expires_at:
            out["expires_at"] = self.expires_at
        return out


@dataclass(frozen=True)
class RuntimeOverrides:
    entries: tuple[RuntimeOverrideEntry, ...] = ()
    warnings: tuple[str, ...] = ()

    def values(self) -> dict[str, str]:
        return {entry.key: entry.value for entry in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "overrides": {
                entry.key: entry.to_dict()
                for entry in sorted(self.entries, key=lambda item: item.key)
            },
            "warnings": list(self.warnings),
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _format_dt(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_dt(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _load_raw(path: str | Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ()
    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        return {}, (f"audio runtime overrides unreadable: {type(e).__name__}: {e}",)
    if not isinstance(data, dict):
        return {}, ("audio runtime overrides ignored: root must be an object",)
    if data.get("kind") not in (None, KIND):
        return {}, (
            f"audio runtime overrides ignored: unsupported kind {data.get('kind')!r}",
        )
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        return {}, (
            "audio runtime overrides ignored: unsupported schema_version "
            f"{data.get('schema_version')!r}",
        )
    return data, ()


def load_runtime_overrides(
    path: str | Path = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    *,
    allowed_keys: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
) -> RuntimeOverrides:
    raw, base_warnings = _load_raw(path)
    warnings = list(base_warnings)
    overrides = raw.get("overrides", {})
    if not isinstance(overrides, dict):
        return RuntimeOverrides(
            warnings=tuple(warnings + ["audio runtime overrides ignored: overrides must be an object"])
        )
    current = _now() if now is None else now.astimezone(UTC)
    entries: list[RuntimeOverrideEntry] = []
    for key, item in overrides.items():
        key_text = str(key).strip()
        if allowed_keys is not None and key_text not in allowed_keys:
            warnings.append(f"audio runtime override ignored: unsupported key {key_text!r}")
            continue
        if not isinstance(item, dict):
            warnings.append(f"audio runtime override ignored: {key_text} must be an object")
            continue
        value = str(item.get("value", "")).strip()
        reason = str(item.get("reason", "")).strip()
        created_at = str(item.get("created_at", "")).strip()
        expires_at = str(item.get("expires_at", "")).strip()
        if not value:
            warnings.append(f"audio runtime override ignored: {key_text} has empty value")
            continue
        if not reason:
            warnings.append(f"audio runtime override ignored: {key_text} needs a reason")
            continue
        if expires_at:
            expires = _parse_dt(expires_at)
            if expires is None:
                warnings.append(
                    f"audio runtime override ignored: {key_text} has invalid expires_at"
                )
                continue
            if expires <= current:
                warnings.append(f"audio runtime override expired: {key_text}")
                continue
        entries.append(
            RuntimeOverrideEntry(
                key=key_text,
                value=value,
                reason=reason,
                created_at=created_at,
                expires_at=expires_at,
            )
        )
    return RuntimeOverrides(entries=tuple(entries), warnings=tuple(warnings))


def write_runtime_overrides(
    overrides: RuntimeOverrides,
    path: str | Path = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
) -> None:
    data = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "overrides": {
            entry.key: entry.to_dict()
            for entry in sorted(overrides.entries, key=lambda item: item.key)
        },
    }
    atomic_write_text(
        path,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        mode=0o644,
    )


def set_runtime_override(
    *,
    key: str,
    value: str,
    reason: str,
    path: str | Path = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    ttl_seconds: int | None = None,
    expires_at: str = "",
    allowed_keys: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
) -> RuntimeOverrides:
    key = key.strip()
    value = value.strip()
    reason = reason.strip()
    if allowed_keys is not None and key not in allowed_keys:
        raise ValueError(f"unsupported override key: {key}")
    if not value:
        raise ValueError("override value is required")
    if not reason:
        raise ValueError("override reason is required")
    current = _now() if now is None else now.astimezone(UTC)
    expiry = expires_at.strip()
    if ttl_seconds is not None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        expiry = _format_dt(current + timedelta(seconds=ttl_seconds))
    elif expiry and _parse_dt(expiry) is None:
        raise ValueError("expires_at must be ISO-8601")
    existing = load_runtime_overrides(path, allowed_keys=allowed_keys, now=current)
    by_key = {entry.key: entry for entry in existing.entries}
    by_key[key] = RuntimeOverrideEntry(
        key=key,
        value=value,
        reason=reason,
        created_at=_format_dt(current),
        expires_at=expiry,
    )
    updated = RuntimeOverrides(entries=tuple(by_key.values()))
    write_runtime_overrides(updated, path)
    return updated


def clear_runtime_override(
    key: str,
    path: str | Path = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    *,
    allowed_keys: set[str] | frozenset[str] | None = None,
) -> RuntimeOverrides:
    key = key.strip()
    if allowed_keys is not None and key not in allowed_keys:
        raise ValueError(f"unsupported override key: {key}")
    existing = load_runtime_overrides(path, allowed_keys=allowed_keys)
    updated = RuntimeOverrides(
        entries=tuple(entry for entry in existing.entries if entry.key != key)
    )
    write_runtime_overrides(updated, path)
    return updated
