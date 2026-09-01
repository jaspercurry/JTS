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


def refused(reason: str, detail: str, *, exit_code: int) -> int:
    """Print the refusal on both streams and hand back ``exit_code``."""

    print(
        json.dumps(
            {"status": "refused", "reason": reason, "detail": detail},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"refused ({reason}): {detail}", file=sys.stderr)
    return exit_code
