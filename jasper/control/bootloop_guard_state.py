# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only state snapshot for jasper-doctor's boot-loop guard checks.

The boot-loop guard (`deploy/bin/jasper-bootloop-guard`) is a
`Type=oneshot` unit that runs once at boot — no resident daemon to ask
for state. It writes a marker JSON to `/run/jasper-bootloop-guard/
state.json` on every run (`tripped: false` on a healthy boot,
`tripped: true` after it has written the runtime drop-ins that disarm
StartLimitAction=reboot). This module reads that marker fresh on every
call so the doctor row reflects the truth of the current boot, including
a guard that never ran (fresh install, unit failed). The marker's
open/parse fail-soft posture is :func:`jasper.control.park_record.read_json`.

Always returns a dict, never raises, with a top-level `ran`
discriminator.
"""
from __future__ import annotations

import os
from typing import Any

from . import park_record

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
    raw = park_record.read_json(_marker_path())
    if raw is None:
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
