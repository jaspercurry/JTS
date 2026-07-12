# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical structured-log emitter for JTS `event=` lines.

Across the codebase, operational events are logged as hand-written
f-strings of the shape ``event=<domain>.<action> k=v k=v``. That
convention is grep-friendly and human-readable, but each call site
re-implements the rendering, and none of them escape field values —
so a value that contains a space, ``=``, or a quote (an SSID, a USB
device label, a free-text reason) silently corrupts the key=val
parse for any tool that reads the journal as logfmt.

This module is the one place that renders that line. It keeps the
exact same on-the-wire shape for clean values (so existing greps and
parsers are unaffected), adds proper logfmt quoting plus one-line control
character escaping for values that need it, and offers an opt-in JSON sink
(``JASPER_LOG_JSON=1``) for machine consumers that would rather parse
one object per line than logfmt.

Stdlib-only, tiny, and built on the ``logging`` module the codebase
already uses — ``log_event(logger, "domain.action", key=value)``
emits through the caller's own logger so handler levels, the flight
recorder, and journald routing all keep working unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

__all__ = ["log_event", "render_logfmt", "render_json", "json_mode_enabled"]


# Printable characters that force a value to be quoted in logfmt. A bare token
# (no ASCII space/control, no `=`, no quote, no backslash) is emitted as-is so
# the common case stays byte-identical to the old hand-written lines. C0/DEL
# controls and Unicode line separators are handled by _unsafe_logfmt_char.
# A backslash forces quoting too: a bare `C:\x` is not safely
# round-trippable by a logfmt parser, and backslashes are rare in JTS
# field values (log paths are POSIX), so the churn is negligible.
_NEEDS_QUOTING = (" ", "\t", "\n", "\r", "=", '"', "\\")


def _unsafe_logfmt_char(ch: str) -> bool:
    codepoint = ord(ch)
    return (
        codepoint < 0x20
        or codepoint == 0x7F
        or ch in {"\u0085", "\u2028", "\u2029"}
    )


def _escape_logfmt_text(text: str) -> str:
    escaped: list[str] = []
    for ch in text:
        codepoint = ord(ch)
        if ch == "\\":
            escaped.append("\\\\")
        elif ch == '"':
            escaped.append('\\"')
        elif ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif ch == "\t":
            escaped.append("\\t")
        elif _unsafe_logfmt_char(ch):
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(ch)
    return "".join(escaped)


def json_mode_enabled(env: dict[str, str] | None = None) -> bool:
    """True when the JSON log sink is requested via JASPER_LOG_JSON.

    Read per call (one dict lookup) rather than cached at import so a
    test — or an operator flipping the env for one daemon — gets the
    live value without import-order surprises. Mirrors the lazy read
    in ``jasper.flight_recorder``. Accepts the literal truthy set the
    rest of the codebase uses (``Config._env_bool``).
    """
    source = os.environ if env is None else env
    raw = source.get("JASPER_LOG_JSON")
    if not raw or not raw.strip():
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _render_value(value: Any) -> str:
    """Render one field value to its logfmt token.

    Predictable scalars: ``None`` → ``null``; ``bool`` → ``true`` /
    ``false`` (checked before int, since ``bool`` is an ``int``
    subclass); ``float`` via ``repr`` so ``1.0`` stays ``1.0`` and
    doesn't collapse to ``1``. Everything else is stringified, then
    quoted+escaped only if it is empty or contains an ASCII space, ``=``,
    a quote, a backslash, an ASCII control, or a Unicode line separator.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = repr(value)
    else:
        text = str(value)
    if text == "" or any(
        ch in _NEEDS_QUOTING or _unsafe_logfmt_char(ch) for ch in text
    ):
        return f'"{_escape_logfmt_text(text)}"'
    return text


def render_logfmt(name: str, fields: dict[str, Any]) -> str:
    """Render ``event=<name> k=v ...`` in logfmt, fields in call order.

    ``name`` is emitted unescaped as the ``event=`` value — names are
    the ``domain.action`` vocabulary, never untrusted, so quoting them
    would just churn every existing grep.
    """
    parts = [f"event={name}"]
    for key, value in fields.items():
        parts.append(f"{key}={_render_value(value)}")
    return " ".join(parts)


def render_json(name: str, fields: dict[str, Any]) -> str:
    """Render one JSON object: ``{"event": name, ...fields}``.

    Non-JSON-native values (e.g. an exception) fall back to ``str``
    so a non-JSON-serializable object does not raise. ``event`` is
    always the first key.
    """
    payload: dict[str, Any] = {"event": name}
    payload.update(fields)
    return json.dumps(payload, default=str)


def log_event(
    logger: logging.Logger,
    name: str,
    /,
    *,
    level: int = logging.INFO,
    exc_info: Any = False,
    fields: dict[str, Any] | None = None,
    **kwfields: Any,
) -> None:
    """Emit one canonical structured event line through ``logger``.

    ``name`` is the ``domain.action`` event name (e.g.
    ``"knob.action"``); keyword fields become ``k=v`` pairs in the
    order given. Renders logfmt by default, or a JSON object when
    ``JASPER_LOG_JSON`` is truthy. The line is fully rendered before
    it reaches ``logger`` (no lazy ``%`` args), so values containing
    ``%`` are safe.

    ``exc_info`` is passed straight to ``logger.log`` — pass ``True``
    from an ``except`` block to attach the current traceback, exactly
    as ``logger.exception("event=...")`` did before migration. It
    defaults to ``False`` so the common (non-exception) path is
    byte-identical to a plain ``logger.info``/``warning`` call.

    ``fields`` is an explicit ordered mapping merged *before* the
    keyword fields. Use it for a field whose name can't be a keyword
    argument: one that collides with a reserved parameter — chiefly
    ``level`` (the volume level is a field literally named ``level``)
    or ``exc_info`` — or one that isn't a valid Python identifier.
    Order is preserved (``fields`` first, then ``**kwfields``), so a
    collision-free event can keep using plain keywords and only the
    rare colliding one reaches for ``fields=``.

    ``logger`` and ``name`` are positional-only so an event can carry
    fields literally named ``logger`` or ``name`` without colliding.
    """
    merged: dict[str, Any] = {**(fields or {}), **kwfields}
    if json_mode_enabled():
        message = render_json(name, merged)
    else:
        message = render_logfmt(name, merged)
    # Only thread exc_info when asked, so the common (non-exception)
    # path is exactly `logger.log(level, message)` — same LogRecord
    # (exc_info=None) as the plain call this replaces.
    if exc_info:
        logger.log(level, message, exc_info=exc_info)
    else:
        logger.log(level, message)
