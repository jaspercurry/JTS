# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read `jasper.log_event` lines back as the fields a journal consumer parses.

`caplog.text` is the wrong surface for an event pin. `render_logfmt` quotes any
value holding a space (`reason=`, `detail=`, `action=`), so a whitespace split
truncates it; and a substring over the concatenated text is satisfied by fields
spread across several records, which is not the one-record property such a pin
is written to hold. This parser honours the quoting and the escapes
`jasper.log_event` emits, and `event_fields` folds "exactly one record carries
this event" into every call site.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

__all__ = [
    "event_field_maps",
    "event_fields",
    "event_records",
    "parse_event",
    "stderr_event",
    "stderr_events",
]

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\" or index + 1 >= len(text):
            out.append(text[index])
            index += 1
            continue
        marker = text[index + 1]
        if marker == "u":
            out.append(chr(int(text[index + 2 : index + 6], 16)))
            index += 6
        else:
            out.append(_ESCAPES.get(marker, marker))
            index += 2
    return "".join(out)


def _tokens(message: str) -> Iterator[str]:
    """Split a logfmt line on unquoted spaces only."""
    token: list[str] = []
    quoted = False
    escaped = False
    for char in message:
        if escaped:
            token.append(char)
            escaped = False
        elif quoted and char == "\\":
            token.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            token.append(char)
        elif char == " " and not quoted:
            if token:
                yield "".join(token)
                token = []
        else:
            token.append(char)
    if token:
        yield "".join(token)


def parse_event(message: str) -> tuple[str, dict[str, str]] | None:
    """``(event name, fields)`` for one rendered event line, else ``None``."""
    fields: dict[str, str] = {}
    name: str | None = None
    for token in _tokens(message):
        key, sep, raw = token.partition("=")
        if not sep:
            continue
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = _unescape(raw[1:-1])
        if name is None:
            if key != "event":
                return None
            name = raw
            continue
        fields[key] = raw
    return None if name is None else (name, fields)


def stderr_events(stderr: str, name: str) -> list[dict[str, str]]:
    """Field maps of every ``event=<name>`` line in a captured stderr stream."""
    return [
        parsed[1]
        for parsed in (parse_event(line) for line in stderr.splitlines())
        if parsed is not None and parsed[0] == name
    ]


def stderr_event(stderr: str, name: str) -> dict[str, str]:
    """The ONE ``event=<name>`` line's fields."""
    matched = stderr_events(stderr, name)
    assert len(matched) == 1, matched
    return matched[0]


def event_records(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    """Every captured record whose event name is exactly ``event``.

    Exact, so `chip_aec_init` never matches `chip_aec_init.ordering_probe`.
    """
    matched: list[logging.LogRecord] = []
    for record in caplog.records:
        parsed = parse_event(record.getMessage())
        if parsed is not None and parsed[0] == event:
            matched.append(record)
    return matched


def event_fields(
    caplog: pytest.LogCaptureFixture, event: str
) -> dict[str, str]:
    """The ONE record named ``event``, as its ``k=v`` field map."""
    records = event_records(caplog, event)
    assert len(records) == 1, [record.getMessage() for record in records]
    parsed = parse_event(records[0].getMessage())
    assert parsed is not None
    return parsed[1]


def event_field_maps(
    caplog: pytest.LogCaptureFixture, event: str, **where: str
) -> list[dict[str, str]]:
    """Field maps of every record named ``event``, in emission order.

    ``where`` narrows to the records carrying those exact field values, so a
    per-source pin can name its record without counting the others; unpacking
    the result (``(fields,) = ...``) is how a call site says "exactly one".
    """
    maps: list[dict[str, str]] = []
    for record in event_records(caplog, event):
        parsed = parse_event(record.getMessage())
        assert parsed is not None
        if all(parsed[1].get(key) == value for key, value in where.items()):
            maps.append(parsed[1])
    return maps
