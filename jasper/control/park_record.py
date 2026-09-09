# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read half of a park record.

``jasper-camilla-recover`` (ADR-0175) and
``jasper-outputd-failure-reconcile`` each park a daemon out-of-band on a
``/run`` record; :func:`snapshot`, driven by one :class:`ParkRecordSpec` per
record, owns the open/absent/unreadable/parse preamble and its posture: a
record that cannot be read is reported distinctly from one that is not
there, because a permissions regression must never read as a healthy
speaker. The one park timestamp any record carries is always named
``parked_at`` (epoch seconds) on the wire, however the record spells it.
Extra branching a bare record can't answer (unit-state cross-checks) is the
caller's, built on top of this shape. :func:`read_json` is the same posture
for ``jasper-bootloop-guard``'s JSON marker, which has no park timestamp and
no reader beyond its own module.
"""
from __future__ import annotations

import json
import os
import time
from calendar import timegm
from dataclasses import dataclass
from typing import Any, Literal

from ..env_file import parse_env_mapping

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class ParkRecordSpec:
    """One park record: its path, the fields it carries verbatim, and which
    of them (if any) is the park timestamp and how it's spelled."""

    default_path: str
    path_env_var: str
    fields: tuple[str, ...] = ()
    timestamp_field: str | None = None
    timestamp_format: Literal["epoch", "iso"] | None = None
    #: A field whose absence makes an otherwise-parsed record
    #: "unintelligible" — reachable only through a partial write that still
    #: renames.
    required_field: str | None = None


def read(path: str) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Fail-soft read of a park record at ``path``.

    Returns ``(terminal, fields)``. A non-None ``terminal`` is the complete
    snapshot the caller must return — the ``absent`` or ``unreadable``
    verdict. Otherwise ``fields`` is the parsed record (empty when the text
    is malformed; that is the caller's to classify).

    Never raises.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"status": "absent", "parked": False}, {}
    except OSError as exc:
        return {
            "status": "unreadable",
            "parked": False,
            "path": path,
            "error": str(exc),
        }, {}

    try:
        fields = parse_env_mapping(text)
    except Exception:  # noqa: BLE001 - a malformed record must not raise here
        fields = {}
    return None, fields


def read_json(path: str) -> dict[str, Any] | None:
    """Fail-soft JSON marker read: ``None`` for absent, unreadable, or
    malformed content — the caller does not need to tell those apart."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parked_at(raw: Any, fmt: str | None) -> int | None:
    if raw is None or fmt is None:
        return None
    if fmt == "iso":
        try:
            return timegm(time.strptime(str(raw), _ISO_FORMAT))
        except ValueError:
            return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def snapshot(spec: ParkRecordSpec, path: str | None = None) -> dict[str, Any]:
    """Fail-soft read of the park record ``spec`` describes.

    Shared shape: ``status`` in ``{"absent", "unreadable", "unintelligible",
    "present"}``, ``parked`` (``True`` only for ``"present"``), ``path``, the
    spec's own ``fields`` verbatim, and ``parked_at`` (epoch seconds) when
    the spec names a timestamp field. Never raises.
    """
    target = path if path is not None else os.environ.get(
        spec.path_env_var, spec.default_path
    )
    terminal, fields = read(target)
    if terminal is not None:
        terminal.setdefault("path", target)
        return terminal

    if spec.required_field and not fields.get(spec.required_field):
        return {"status": "unintelligible", "parked": False, "path": target}

    out: dict[str, Any] = {"status": "present", "parked": True, "path": target}
    for name in spec.fields:
        out[name] = fields.get(name)
    if spec.timestamp_field:
        out["parked_at"] = _parked_at(
            fields.get(spec.timestamp_field), spec.timestamp_format
        )
    return out
