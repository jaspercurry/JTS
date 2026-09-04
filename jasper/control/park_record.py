# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read half of a park record.

``jasper-camilla-recover`` (ADR-0175) parks a daemon out-of-band on a ``/run``
record, and :mod:`jasper.control.camilla_recover_state` reads it for
jasper-doctor and ``/state.resilience``. This module owns the
open/absent/unreadable/parse preamble, including its posture: a record that
cannot be read is reported distinctly from one that is not there, because a
permissions regression must never read as a healthy speaker. Each reader
keeps only what is specific to its own record's fields.
"""
from __future__ import annotations

from typing import Any

from ..env_load import parse_env_text


def read(path: str) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Fail-soft read of a park record at ``path``.

    Returns ``(terminal, fields)``. A non-None ``terminal`` is the complete
    snapshot the caller must return — the ``absent`` or ``unreadable`` verdict,
    which both records spell the same way. Otherwise ``fields`` is the parsed
    record (empty when the text is malformed; that is the caller's to
    classify).

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
        fields = parse_env_text(text)
    except Exception:  # noqa: BLE001 - a malformed record must not raise here
        fields = {}
    return None, fields
