# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The chip-AEC commissioning outcome record: one shape, one owner.

`jasper-aec-commission` writes the last run's verdict here; jasper-control
reads it into the `/aec` `commission` object. This module owns the path, the
persisted keys, and the public projection so the writer and the reader cannot
drift. It stays stdlib-only apart from `atomic_io` so the long-lived control
daemon can import it without pulling the commissioner's numpy stack.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.atomic_io import atomic_write_json

OUTCOME_PATH = Path("/var/lib/jasper/chip-aec-commission.json")
SCHEMA_VERSION = 1

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _text(value: Any) -> str:
    return str(value or "")


@dataclass(frozen=True, slots=True)
class CommissionOutcome:
    """The persisted record. Defaults are the "no run yet" reading."""

    state: str = ""
    detail: str = ""
    updated_at: str = ""
    # None when the record on disk declared no usable version: a later
    # read-modify-write must not stamp an unknown record as this schema.
    schema_version: int | None = SCHEMA_VERSION

    @classmethod
    def now(cls, *, state: str, detail: str) -> CommissionOutcome:
        return cls(
            state=state,
            detail=detail,
            updated_at=time.strftime(_TIMESTAMP_FORMAT, time.gmtime()),
        )

    def to_public(self, *, running: bool) -> dict[str, Any]:
        """The `/aec` `commission` object.

        `running` is live unit truth, not part of the record, so the caller
        supplies it.
        """
        return {"running": running, "state": self.state, "detail": self.detail}


def read(path: Path) -> CommissionOutcome | None:
    """The record at ``path``, or None when it is absent or unreadable."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    return CommissionOutcome(
        state=_text(data.get("state")),
        detail=_text(data.get("detail")),
        updated_at=_text(data.get("updated_at")),
        schema_version=version if isinstance(version, int) else None,
    )


def write(path: Path, outcome: CommissionOutcome) -> None:
    """Publish ``outcome`` atomically. Raises OSError; callers set policy."""
    atomic_write_json(
        path,
        {
            "schema_version": outcome.schema_version,
            "state": outcome.state,
            "detail": outcome.detail,
            "updated_at": outcome.updated_at,
        },
        mode=0o644,
    )
