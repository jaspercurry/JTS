# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning CLIs' shared source reader, exit-code rule, and its output.

A failure is an output, not an error, and there are three of them: the
instrument REFUSED a round it could read, the input was UNREADABLE, or the
result was UNWRITABLE. The machine-readable record goes to stdout, one
sentence goes to stderr, and the exit code says which of the three it was,
because that is what tells an operator where to go. The record's shape is
stated once, in docs/tuning-operator-runbook.md's "Exit codes".

Every tool in the runbook's tool menu takes its codes from here; a tool whose
own failures are finer-grained than three says so in its ``reason`` slug,
never by numbering them itself. :data:`OWN_EXIT_VOCABULARY` names who does not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from ._report import render_report

_T = TypeVar("_T")

#: The one tool-menu module that keeps its own numbering: a human-only sudo
#: ``set``/``show`` config door.
OWN_EXIT_VOCABULARY = frozenset({
    "jasper.cli.declare_geometry",
})

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


def read_source_bytes(path: str) -> bytes:
    """The document named by ``path``, or stdin when ``path`` is ``-``."""

    return sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()


def read_json_source(path: str) -> Any:
    """The same source parsed as JSON. Unreadable and unparsable arrive as one
    ``ValueError`` naming the source, because they are the one outcome
    :data:`EXIT_UNREADABLE` publishes."""

    try:
        return json.loads(read_source_bytes(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def answered(document: Mapping[str, Any], line: str = "") -> int:
    """A verb's answer on stdout and, when given, its one human line on
    stderr (ADR-0235). A success document never carries ``status``."""

    print(render_report(dict(document)))
    if line:
        print(line, file=sys.stderr)
    return EXIT_OK


def refused(reason: str, detail: Any, *, exit_code: int, status: str = "refused") -> int:
    """Print the outcome on both streams and hand back ``exit_code``.

    ``detail`` is a sentence or the fields the failure carried -- everything the
    tool would otherwise have published as top-level keys goes here, so one
    reader parses every refusal.
    """

    sentence = (
        detail if isinstance(detail, str)
        else json.dumps(detail, sort_keys=True, default=str)
    )
    print(render_report({"status": status, "reason": reason, "detail": detail}))
    print(f"{status} ({reason}): {sentence}", file=sys.stderr)
    return exit_code


def failed(exit_code: int, reason: str, detail: Any) -> int:
    """One failing stage, published under the word its code owns."""

    return refused(
        reason, detail, exit_code=exit_code, status=STATUS_BY_CODE[exit_code]
    )


class StageFailed(Exception):
    """A failure a stage claimed, carrying that stage's exit code."""

    def __init__(self, code: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.code = code


def stage(
    code: int,
    errors: tuple[type[Exception], ...],
    fn: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run one stage; what it raises from ``errors`` gets that stage's code."""

    try:
        return fn(*args, **kwargs)
    except errors as exc:
        raise StageFailed(code, exc) from exc
