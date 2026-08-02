# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared persistence vocabulary for AirPlay drift-correction mode."""
from __future__ import annotations

from collections.abc import Mapping

MODE_ENV_FILE = "/var/lib/jasper/airplay_mode.env"
ENV_VAR = "JASPER_AIRPLAY_FREE_RUNNING"
MODE_FREE_RUNNING = "free-running"
MODE_SYNCED = "synced"

_TRUE_VALUES = frozenset({"yes", "true", "1"})


def mode_from_env(env: Mapping[str, str]) -> str:
    """Resolve persisted env values, defaulting unknown input to synced."""

    value = (env.get(ENV_VAR) or "").strip().lower()
    return MODE_FREE_RUNNING if value in _TRUE_VALUES else MODE_SYNCED
