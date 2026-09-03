# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read side of the accessory supervisor's status file, for ``/state``.

Kept apart from :mod:`jasper.accessories.supervisor` so jasper-control reads
the file without importing the bridge runtime.
"""
from __future__ import annotations

import json
import os
from typing import Any

# jasper-input's RuntimeDirectory (deploy/systemd/jasper-input.service).
# systemd reaps it on stop, so a file that exists describes the process
# running now; no staleness stamp is needed.
STATUS_PATH = "/run/jasper-input/status.json"


def snapshot(status_path: str | os.PathLike = STATUS_PATH) -> dict[str, Any]:
    """``bridges`` maps each bridge to ``restarts`` (failures since the process
    started) and ``last_error`` (the exception class name, set only while the
    bridge waits out its backoff). Never raises: a missing or unreadable file
    reads as ``published: False``."""
    try:
        with open(status_path, encoding="utf-8") as f:
            return {"published": True, "bridges": json.load(f)["bridges"]}
    except (OSError, KeyError, TypeError, ValueError):
        return {"published": False, "bridges": {}}
