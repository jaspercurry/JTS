# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Short-lived Home Assistant status probe for jasper-control.

The parent control daemon uses this module as a subprocess target so the
HA/httpx probe stack can exit after each refresh instead of staying resident
after the first `/system/snapshot` poll.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .. import home_assistant


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env_file = args[0] if args else home_assistant.HA_ENV_FILE
    status = asyncio.run(
        home_assistant.probe_status_from_env(
            env_file_path=env_file,
            force=True,
        ),
    )
    print(json.dumps(status, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
