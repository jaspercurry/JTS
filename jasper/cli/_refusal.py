# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared refusal output for the read-only measurement CLIs.

A refusal is an output, not an error: the machine-readable record goes to
stdout, one sentence goes to stderr, and the caller's own exit code is
returned so each tool keeps its own contract.
"""
from __future__ import annotations

import json
import sys


def refused(reason: str, detail: str, *, exit_code: int, status: str = "refused") -> int:
    """Print the outcome on both streams and hand back ``exit_code``.

    ``status`` names the KIND on both streams. A round that could not be read,
    or a view that could not be written, is not the instrument declining, and
    stamping ``refused`` on it sends a reader after a refusal reason no tool
    ever named.
    """

    print(
        json.dumps(
            {"status": status, "reason": reason, "detail": detail},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"{status} ({reason}): {detail}", file=sys.stderr)
    return exit_code
