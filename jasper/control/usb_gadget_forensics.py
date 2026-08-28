# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persistent intent and fixed request mailbox for USB gadget forensics."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..web._common import write_env_file

ENABLED_FILE = "/var/lib/jasper/usb_gadget_forensics.env"
RUNTIME_DIR = "/run/jasper-usb-gadget-forensics"

# The sampler's single-threaded loop (deploy/usbsink/jasper-usbgadget-snapshot
# run_forensics) does not rewrite status.json while a repair blocks it: the
# gadget restart is bounded at that script's REPAIR_RESTART_TIMEOUT_SEC (60s),
# and a rebuild that lands runs jasper_usbgadget_refresh_consumers
# (deploy/usbsink/jasper-usbgadget-compose.sh), which chains two more
# try-restarts at that same 60s bound each -- 60 + 2*60 = 180s worst case with
# no status write in between. Nothing on disk flags "repair in flight" for
# that whole span (the request.<action> marker this module also reads below
# is deleted the instant the loop starts handling it, before the blocking
# call), so the freshness window can't be narrowed by keying off a repair
# marker -- it has to be widened outright, trading away fast genuine-
# staleness detection for never false-alarming "not running" mid-repair.
REPAIR_WORST_CASE_SEC = 180.0


def snapshot() -> dict:
    """Return bounded operator state; never expose captured artifact contents."""
    enabled = Path(ENABLED_FILE).exists()
    status: dict = {}
    try:
        loaded = json.loads((Path(RUNTIME_DIR) / "status.json").read_text())
        if isinstance(loaded, dict):
            status = loaded
    except (OSError, json.JSONDecodeError):
        pass
    last_sample = status.get("last_sample_at")
    interval = status.get("sample_interval_sec")
    margin = float(interval) if isinstance(interval, (int, float)) else 30.0
    freshness = max(REPAIR_WORST_CASE_SEC + margin, margin * 3)
    fresh = isinstance(last_sample, (int, float)) and time.time() - last_sample < freshness
    pending = next(
        (action for action in ("repair", "capture")
         if (Path(RUNTIME_DIR) / f"request.{action}").exists()),
        None,
    )
    return {
        "enabled": enabled,
        "running": bool(enabled and status.get("running") and fresh),
        "pending_action": pending,
        **{key: status.get(key) for key in (
            "ram_cap_bytes", "sample_interval_sec", "sample_count", "last_sample_at",
            "last_action", "last_action_at", "latest_artifact",
        )},
    }


def set_enabled(enabled: bool) -> dict:
    """Persist intent; the systemd path unit owns sampler start/recovery."""
    path = Path(ENABLED_FILE)
    if enabled:
        write_env_file(str(path), {"JASPER_USB_GADGET_FORENSICS": "1"}, mode=0o640)
    else:
        path.unlink(missing_ok=True)
    return snapshot()


def request(action: str) -> dict:
    """Queue one fixed root-owned action without granting control root access."""
    if action not in {"capture", "repair"}:
        raise ValueError("action must be capture or repair")
    state = snapshot()
    if not state["running"]:
        raise ValueError("USB forensics sampler is not running")
    request_path = Path(RUNTIME_DIR) / f"request.{action}"
    fd = os.open(
        request_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o660,
    )
    os.close(fd)
    return snapshot()
