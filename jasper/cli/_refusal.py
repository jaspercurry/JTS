# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning CLIs' shared source reader, exit-code rule, its output, and the
``--help`` they render from their catalog rows (ADR-0393).

A failure is an output, not an error, and there are three of them: the
instrument REFUSED a round it could read, the input was UNREADABLE, or the
result was UNWRITABLE. The machine-readable record goes to stdout, one
sentence goes to stderr, and the exit code says which of the three it was,
because that is what tells an operator where to go. The refusal record's own
fields and each tool's ``--help`` are the reference.

Every tool in the runbook's tool menu takes its codes from here; a tool whose
own failures are finer-grained than three says so in its ``reason`` slug,
never by numbering them itself. :data:`OWN_EXIT_VOCABULARY` names who does not.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, TypeVar

from ._report import render_report

if TYPE_CHECKING:  # type only: the catalog loads NumPy, and jasper-round list|show stay light (ADR-0393)
    from jasper.active_speaker.round_view_artifacts import CatalogRow

_T = TypeVar("_T")

#: The one tool-menu module that keeps its own numbering: a human-only sudo
#: ``set``/``show`` config door.
OWN_EXIT_VOCABULARY = frozenset({
    "jasper.cli.declare_geometry",
})

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNREADABLE = 2
EXIT_WRITE_FAILED = 3

#: What each code tells the caller; a tool's ``--help`` lists them from here.
EXIT_MEANINGS = {
    EXIT_OK: "the answer",
    EXIT_REFUSED: "it read its input and cannot grade it; the reason names why",
    EXIT_UNREADABLE: "it cannot read its input, malformed input included",
    EXIT_WRITE_FAILED: "it did the work, but cannot write its artifact",
}

#: The word each failing code publishes as ``status``: callers name the CODE
#: and this picks the word, so the two can never disagree.
STATUS_BY_CODE = {
    EXIT_REFUSED: "refused",
    EXIT_UNREADABLE: "unreadable",
    EXIT_WRITE_FAILED: "unwritable",
}


def exit_codes_help(codes: Sequence[int] = tuple(EXIT_MEANINGS)) -> str:
    """The ``EXIT CODES`` block of a ``--help``, each failure under the ``status`` its record carries."""
    failing = [code for code in codes if code in STATUS_BY_CODE]
    return "\n".join((
        "EXIT CODES",
        *(f"  {code}  {STATUS_BY_CODE[code] + ': ' if code in failing else ''}{EXIT_MEANINGS[code]}"
          for code in codes),
        *(['  a failure prints "<status> (<reason>): <detail>" on stderr and the\n'
           '  same record as JSON on stdout'] if failing else []),
    ))


#: argparse wraps its own help at 78 columns on an 80-column terminal; a verb's rendered text matches it.
_HELP_WIDTH = 78


def help_from_rows(
    parser: argparse.ArgumentParser, rows: Mapping[str, CatalogRow], *,
    codes: Sequence[int] = tuple(EXIT_MEANINGS), note: str = "",
) -> None:
    """A verb's ``--help`` from its catalog rows (ADR-0393): each mode's question and
    when not to use it, its example, and the exit ``codes`` it returns.

    ``note`` says what the shared words leave out for this verb.
    """
    modes = []
    for command, row in rows.items():
        mode = " ".join(command.split()[2:])
        modes.append(textwrap.fill(f"{mode}: {row.question}" if mode else row.question, _HELP_WIDTH) + "\n"
                     + textwrap.fill(f"Not for {row.avoid}.", _HELP_WIDTH, initial_indent="  ", subsequent_indent="  "))
    examples = [f"  {' '.join((command, *row.argv))}" for command, row in rows.items()]
    exits = [exit_codes_help(codes)]
    if note:
        exits.append(textwrap.fill(note, _HELP_WIDTH, initial_indent="  ", subsequent_indent="  "))
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = "\n\n".join(modes)
    parser.epilog = "\n\n".join(("\n".join(("EXAMPLES" if len(examples) > 1 else "EXAMPLE", *examples)), "\n".join(exits)))


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


def answered(document: Mapping[str, Any], line: str = "", *, sort_keys: bool = True) -> int:
    """A verb's answer on stdout and, when given, its one human line on
    stderr (ADR-0237). A success document never carries ``status``."""

    print(render_report(dict(document), sort_keys=sort_keys))
    if line:
        print(line, file=sys.stderr)
    return EXIT_OK


def envelope(
    view: str, *, schema: str | None, subject: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any], out: Path | None = None, **fields: Any,
) -> dict[str, Any]:
    """A success answer under the envelope every one shares: the view and its
    answer version, what it read (one subject, or a list of them as ``rounds``
    for a view that compares rounds), the parameters it used, and the
    artifact it wrote, when it wrote one."""
    # See ADR-0387
    document = {
        "view": view, "schema": schema, "parameters": dict(parameters),
        "subject": dict(subject) if isinstance(subject, Mapping) else {"rounds": [dict(one) for one in subject]},
        **fields,
    }
    if out is not None:
        document.update(out=str(out), bytes=out.stat().st_size)
    return document


def answer(
    view: str, *, schema: str | None, subject: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any], out: Path | None = None, line: str, **fields: Any,
) -> int:
    """Print :func:`envelope`'s answer and its one human line (ADR-0237)."""
    return answered(envelope(view, schema=schema, subject=subject, parameters=parameters, out=out, **fields), line)


def refused(
    reason: str, detail: Any, *, exit_code: int, status: str = "refused",
    code: str | None = None, next_action: Mapping[str, Any] | None = None, line: str | None = None,
) -> int:
    """Print the outcome on both streams and hand back ``exit_code``.

    ``detail`` is a sentence or the fields the failure carried -- everything the
    tool would otherwise have published as top-level keys goes here, so one
    reader parses every refusal. ``line``, when given, is the stderr sentence
    in place of ``detail``'s own.
    """

    sentence = (
        line if line is not None
        else detail if isinstance(detail, str)
        else json.dumps(detail, sort_keys=True, default=str)
    )
    record = {"status": status, "reason": reason, "detail": detail}
    if code is not None:
        record["code"] = code
    if next_action is not None:
        record["next_action"] = dict(next_action)
    print(render_report(record))
    print(f"{status} ({reason}): {sentence}", file=sys.stderr)
    return exit_code


def failed(
    exit_code: int, reason: str, detail: Any, *,
    code: str | None = None, next_action: Mapping[str, Any] | None = None, line: str | None = None,
) -> int:
    """One failing stage, published under the word its code owns."""

    return refused(
        reason, detail, exit_code=exit_code, status=STATUS_BY_CODE[exit_code],
        code=code, next_action=next_action, line=line,
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
