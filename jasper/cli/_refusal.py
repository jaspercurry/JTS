# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning CLIs' shared exit-code rule and its output.

A failure is an output, not an error, and there are three of them: the
instrument REFUSED a round it could read, the input was UNREADABLE, or the
result was UNWRITABLE. The machine-readable record goes to stdout, one
sentence goes to stderr, and the exit code says which of the three it was,
because that is what tells an operator where to go. The record's shape is
stated once, in docs/tuning-operator-runbook.md's "Exit codes".

Every tool with a ``build_parser()`` in the runbook's tool menu takes its
codes from here; a tool whose own failures are finer-grained than three says
so in its ``reason`` slug, never by numbering them itself. Two doors are
deliberately outside: ``jasper-declare-geometry`` is a ``set``/``show`` config
door run under sudo by the person holding the tape measure, so it prints human
text and keeps its own ``EXIT_NOT_FOUND``; and ``jasper-arm-walk`` is a
long-running mover service whose stall codes are its own
(``jasper/active_speaker/arm_walk.py``'s ``EXIT_NAMES``).
"""
from __future__ import annotations

import json
import sys
from typing import Any

EXIT_OK = 0
#: The instrument declined a round it could read.
EXIT_REFUSED = 1
#: The round, bundle or source could not be read at all.
EXIT_UNREADABLE = 2
#: The work was done and the result could not be filed.
EXIT_WRITE_FAILED = 3

#: The word each failing code publishes as ``status``: callers name the CODE
#: and this picks the word, so the two can never disagree.
STATUS_BY_CODE = {
    EXIT_REFUSED: "refused",
    EXIT_UNREADABLE: "unreadable",
    EXIT_WRITE_FAILED: "unwritable",
}


def refused(reason: str, detail: str, *, exit_code: int, status: str = "refused") -> int:
    """Print the outcome on both streams and hand back ``exit_code``."""

    print(
        json.dumps(
            {"status": status, "reason": reason, "detail": detail},
            indent=2,
            sort_keys=True,
        )
    )
    print(f"{status} ({reason}): {detail}", file=sys.stderr)
    return exit_code


def failed(exit_code: int, reason: str, detail: str) -> int:
    """One failing stage, published under the word its code owns."""

    return refused(
        reason, detail, exit_code=exit_code, status=STATUS_BY_CODE[exit_code]
    )


def fail_with_payload(
    message: str, payload: dict[str, Any], *, as_json: bool, code: int
) -> int:
    """The ``--json``-gated variant: a sentence always, the record on request.

    Three tools publish an ``{"ok": false, ...}`` record only when the caller
    asked for one. Converging that contract with :func:`failed`'s is a
    follow-on; this is the one implementation of the contract they have.
    """

    print(message, file=sys.stderr)
    if as_json:
        json.dump(payload, sys.stdout, indent=1)
        sys.stdout.write("\n")
    return code
