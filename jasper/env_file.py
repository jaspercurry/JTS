# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The single home for systemd ``EnvironmentFile`` mechanics.

Two halves, one quoting rule (:func:`_unquoted`, systemd's own):

* **One key at a time**, order-preservingly, for the reconcilers that own a
  single key in a file several units read: a co-reader's **comments and blank
  lines survive verbatim** and assignment order is preserved. Assignment lines
  are canonicalized to ``KEY=value`` on any rewrite (key-side spacing in a
  hand-written ``KEY = value`` is normalized) — harmless because every writer
  here emits clean ``KEY=value``, which is the ``EnvironmentFile`` form.
* **The whole file**, for the wizards whose unit of work is the file: read it
  into a mapping, publish a mapping as its complete contents, delete it.

Scope is deliberately small: no interpolation, no multi-line values, no
``export`` handling, because ``EnvironmentFile`` lines are plain ``KEY=value``
— the format the daemons actually read. Callers own their key names, their
value validation, and their restart/rollback; a caller whose writers race owns
picking :func:`jasper.atomic_io.locked_update_env_file` over
:func:`write_env_file`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# A parsed line is either a real assignment ``(key, value)`` or a line we
# preserve verbatim -- comment / blank / malformed -- carried as ``(raw, None)``.
ParsedLine = tuple[str, str | None]


def parse_env_lines(text: str) -> list[ParsedLine]:
    """Parse env-file text into ordered ``(key, value)`` / ``(raw_line, None)``.

    Assignments become ``(key, value)`` with the key stripped; everything else
    (comments, blanks, lines with no ``=``) is carried verbatim as
    ``(raw_line, None)`` so a rewrite preserves the operator's file exactly.
    """
    out: list[ParsedLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append((raw, None))
            continue
        key, _, value = stripped.partition("=")
        out.append((key.strip(), value))
    return out


def _unquoted(value: str) -> str:
    """Surrounding whitespace stripped, then ONE matching quote pair — the
    resolution systemd itself applies to an ``EnvironmentFile`` value."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _render(lines: list[ParsedLine]) -> str:
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
            found = _unquoted(v)
    return found


def parse_env_mapping(text: str) -> dict[str, str]:
    """Every assignment in ``text`` as ``{key: value}``, unquoted like
    :func:`read_value` and resolved last-wins, as systemd does."""
    return {k: _unquoted(v) for k, v in parse_env_lines(text) if v is not None}


def upsert(text: str, key: str, value: str) -> tuple[str, bool]:
    """Set ``key=value``, replacing the first assignment in place or appending.

    Returns ``(new_text, changed)``; ``changed`` is False iff the key's existing
    value already RESOLVES to ``value`` (quotes/whitespace aside), so the caller
    can skip a redundant write + restart. ``new_text`` is only authoritative when
    ``changed`` is True; the result always ends in a single trailing newline.
    """
    lines = parse_env_lines(text)
    new_lines: list[ParsedLine] = []
    found = False
    changed = False
    for k, v in lines:
        if v is not None and k == key:
            if found:
                # Drop duplicate later assignments — the first becomes canonical.
                changed = True
                continue
            found = True
            if _unquoted(v) == value:
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
    new_lines: list[ParsedLine] = []
    changed = False
    for k, v in lines:
        if v is not None and k == key:
            changed = True
            continue
        new_lines.append((k, v))
    body = _render(new_lines)
    return (body + "\n" if body else ""), changed


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """The assignments in the file at ``path``, or ``{}`` when it has none.

    Fail-soft: a missing file resolves silently to ``{}``, so a reader outside
    a secret compartment's group gets an empty mapping rather than a
    traceback. An existing-but-unreadable file is a provisioning fault, so it
    is logged before resolving the same way.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as e:
        logger.warning("could not read %s: %s", path, e)
        return {}
    return parse_env_mapping(text)


def write_env_file(
    path: str | os.PathLike[str],
    values: Mapping[str, str],
    *,
    mode: int = 0o600,
) -> None:
    """Atomically publish ``values`` as the file's COMPLETE contents.

    A whole-file replace, so a reader never sees a torn file — but two writers
    that each read, change one key, and publish do lose each other's key. Use
    :func:`jasper.atomic_io.locked_update_env_file` where writers race (the
    threaded wizard server's own ``/save`` handlers do).

    ``mode`` defaults to 0600 because these files carry API keys and OAuth
    secrets; pass a group-readable mode for the ones a non-root daemon has to
    read off disk. Raises ``ValueError`` for a value carrying a newline, which
    systemd would read as a second assignment.
    """
    # lazy: import cost — env_file is a leaf every parse-only reader imports,
    # and this pulls in tempfile/fcntl for the writers alone (ADR-0226).
    from jasper.atomic_io import atomic_write_text, format_env_text

    atomic_write_text(path, format_env_text(values), mode=mode)


def delete_env_file(path: str | os.PathLike[str]) -> None:
    """Best-effort unlink; an already-absent file is success."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not delete %s: %s", path, e)
