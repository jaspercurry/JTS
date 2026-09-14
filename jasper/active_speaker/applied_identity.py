# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Names of the candidate and compiled config in an applied record."""

from typing import Any, Mapping


def applied_identity(applied_state: Mapping[str, Any] | None) -> dict[str, str | None] | None:
    if applied_state is None:
        return None
    config = applied_state.get("config") or {}
    return {
        "candidate": (applied_state.get("source") or {}).get("measured_candidate_fingerprint"),
        "record": (config.get("sha256") or "")[:12] or None,
        "config_path": config.get("path"),
        "applied_at": applied_state.get("applied_at"),
    }
