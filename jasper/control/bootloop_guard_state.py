# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only state snapshot for `/state.resilience.bootloop_guard`.

The boot-loop guard (`deploy/bin/jasper-bootloop-guard`) is a
`Type=oneshot` unit that runs once at boot — no resident daemon to ask
for state. It writes a marker JSON to `/run/jasper-bootloop-guard/
state.json` on every run (`tripped: false` on a healthy boot,
`tripped: true` after it has written the runtime drop-ins that disarm
StartLimitAction=reboot). This module reads that marker fresh on every
call so `/state` reflects the truth of the current boot, including a
guard that never ran (fresh install, unit failed).

Shape mirrors `wifi_guardian_state.snapshot()`: always returns a dict,
never raises, with a top-level `ran` discriminator.
"""
from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_MARKER_PATH = "/run/jasper-bootloop-guard/state.json"


def _marker_path() -> str:
    return os.environ.get("JASPER_BOOTLOOP_MARKER_FILE", DEFAULT_MARKER_PATH)


def snapshot() -> dict[str, Any]:
    """Fail-soft marker read. Fields when the guard ran this boot:
    ``ran`` / ``tripped`` / ``reload_ok`` / ``boots_in_window`` /
    ``threshold`` / ``window_sec`` / ``checked_at`` / ``units`` (drop-in
    targets when tripped). A missing or corrupt marker resolves to
    ``{"ran": False}`` — the guard fails open, and so does its
    observability."""
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"ran": False}
    if not isinstance(raw, dict):
        return {"ran": False}
    return {
        "ran": True,
        "tripped": bool(raw.get("tripped")),
        "reload_ok": raw.get("reload_ok"),
        "boots_in_window": raw.get("boots_in_window"),
        "threshold": raw.get("threshold"),
        "window_sec": raw.get("window_sec"),
        "checked_at": raw.get("checked_at"),
        "units": raw.get("units"),
    }
