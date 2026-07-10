# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Order-preserving systemd ``EnvironmentFile`` upsert helpers.

The single home for "rewrite one ``KEY=value`` line in a wizard/reconciler-owned
``/var/lib/jasper/*.env`` file without disturbing the operator's other lines."
Several reconcilers own exactly one key in a shared multi-reader env file and
must upsert it order-preservingly: a co-reader's **comments and blank lines
survive verbatim** and assignment order is preserved. Assignment lines are
canonicalized to ``KEY=value`` on any rewrite (key-side spacing in a
hand-written ``KEY = value`` is normalized) — harmless because every writer here
emits clean ``KEY=value`` and that is the systemd ``EnvironmentFile`` form. The
consumers are :mod:`jasper.fanin.coupling_reconcile` and
:mod:`jasper.fanin.buffer_reconcile` — both predated this module and carried
equivalent private ``_parse_env`` / ``_render_*`` copies until they were
migrated onto this helper (behavior-preserving: for the single-assignment
files those reconcilers produce the rendering is identical).

Scope is deliberately small: parse, read one key, upsert one key, remove one
key. It is NOT a general env-file framework (no interpolation, no multi-line
values, no `export ` handling) because systemd ``EnvironmentFile`` lines are
plain ``KEY=value`` — matching the format the daemons actually read. The callers
own their key name, their value validation, their atomic write, and their
restart/rollback; this module only owns the text transform.
"""

from __future__ import annotations

# A parsed line is either a real assignment ``(key, value)`` or a line we
# preserve verbatim — comment / blank / malformed — carried as ``(raw, None)``.
ParsedLine = "tuple[str, str | None]"


def parse_env_lines(text: str) -> "list[tuple[str, str | None]]":
    """Parse env-file text into ordered ``(key, value)`` / ``(raw_line, None)``.

    Assignments become ``(key, value)`` with the key stripped; everything else
    (comments, blanks, lines with no ``=``) is carried verbatim as
    ``(raw_line, None)`` so a rewrite preserves the operator's file exactly.
    """
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append((raw, None))
            continue
        key, _, value = stripped.partition("=")
        out.append((key.strip(), value))
    return out


def _render(lines: "list[tuple[str, str | None]]") -> str:
    return "\n".join(k if v is None else f"{k}={v}" for k, v in lines)


def read_value(text: str, key: str) -> str | None:
    """The (last) value assigned to ``key`` in ``text``, surrounding quotes and
    whitespace stripped, or ``None`` if the key is absent.

    Last-wins mirrors systemd's own ``EnvironmentFile`` semantics (a later line
    overrides an earlier one), so a reader sees what the daemon would.
    """
    found: str | None = None
    for k, v in parse_env_lines(text):
        if v is not None and k == key:
            found = v.strip().strip("'\"")
    return found


def upsert(text: str, key: str, value: str) -> tuple[str, bool]:
    """Set ``key=value``, replacing the first assignment in place or appending.

    Returns ``(new_text, changed)``; ``changed`` is False iff the key's existing
    value already RESOLVES to ``value`` (quotes/whitespace aside), so the caller
    can skip a redundant write + restart. ``new_text`` is only authoritative when
    ``changed`` is True; the result always ends in a single trailing newline.
    """
    lines = parse_env_lines(text)
    new_lines: list[tuple[str, str | None]] = []
    found = False
    changed = False
    for k, v in lines:
        if v is not None and k == key:
            if found:
                # Drop duplicate later assignments — the first becomes canonical.
                changed = True
                continue
            found = True
            if v.strip().strip("'\"") == value:
                # Already the desired value -> changed=False, so the caller skips
                # the write/restart (and discards this text). We keep the parsed
                # value side as-written (quotes preserved); key-side spacing is
                # canonicalized to KEY=value, which only surfaces if some OTHER
                # key forces a rewrite — acceptable per the module docstring.
                new_lines.append((k, v))
            else:
                changed = True
                new_lines.append((key, value))
        else:
            new_lines.append((k, v))
    if not found:
        new_lines.append((key, value))
        changed = True
    return _render(new_lines) + "\n", changed


def remove(text: str, key: str) -> tuple[str, bool]:
    """Strip every assignment of ``key`` from ``text``, preserving other lines.

    Returns ``(new_text, changed)``. When the result is empty the caller should
    unlink the file rather than leave a 0-byte file, so the unit's own
    ``Environment=`` default (if any) becomes the single source of truth again.
    The non-empty result ends in a single trailing newline; the empty result is
    the empty string.
    """
    lines = parse_env_lines(text)
    new_lines: list[tuple[str, str | None]] = []
    changed = False
    for k, v in lines:
        if v is not None and k == key:
            changed = True
            continue
        new_lines.append((k, v))
    body = _render(new_lines)
    return (body + "\n" if body else ""), changed
