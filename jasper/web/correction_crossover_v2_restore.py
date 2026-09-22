# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Identify the applied tuning graph."""

from __future__ import annotations

from typing import Any, Mapping

from jasper.active_speaker import baseline_profile


def config_graph_fingerprint(profile: Mapping[str, Any] | None) -> str:
    return str(((profile or {}).get("config") or {}).get("sha256") or "")[:16]


def current_graph_fingerprint() -> str:
    profile = baseline_profile.load_applied_baseline_profile_state()
    if profile is None or baseline_profile.applied_profile_displacement(profile) == baseline_profile.APPLIED_PROFILE_DISPLACED:
        return ""
    return config_graph_fingerprint(profile)
