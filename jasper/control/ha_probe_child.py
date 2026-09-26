# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Child-process Home Assistant status probe for jasper-control.

jasper-control only needs the small JSON status card for /system/snapshot.
Running the HA probe in this module keeps the parent process from retaining
the Home Assistant/httpx import graph after the dashboard asks for status.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .. import home_assistant


def main() -> int:
    rc = 0
    try:
        status = asyncio.run(home_assistant.probe_status_from_env())
    except Exception:  # noqa: BLE001 - child must never crash the dashboard
        status = home_assistant.failed_status("probe failed")
        rc = 1

    json.dump(status, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return rc


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
