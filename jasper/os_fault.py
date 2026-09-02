# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Find the OS fault under a wrapped exception."""
from __future__ import annotations


def root_os_error(exc: BaseException) -> OSError | None:
    """The deepest ``OSError`` down the ``__cause__`` chain, if any.

    Wrappers nest: an SDK re-raises ``from`` a socket error, an evidence
    store raises through a bundle reader that wrapped an artifact reader.
    Only explicit ``__cause__`` links are followed — ``__context__`` would
    surface an unrelated earlier error from an enclosing ``except``."""
    seen: set[int] = set()
    found: OSError | None = None
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, OSError):
            found = cursor
        cursor = cursor.__cause__
    return found
