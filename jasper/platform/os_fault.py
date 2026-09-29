# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Walk a wrapped exception's chain, and find the OS fault under it."""
from __future__ import annotations

from collections.abc import Iterator


def exception_chain(
    exc: BaseException, *, context: bool = False,
) -> Iterator[BaseException]:
    """``exc`` and what it was raised from, nearest first, each link once.

    Follows ``__cause__``. ``context=True`` also follows an implicit
    ``__context__``, for an SDK that re-raises inside the ``except`` that
    caught the original; otherwise it would surface an unrelated earlier
    error from an enclosing ``except``."""
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        yield link
        link = link.__cause__ or (link.__context__ if context else None)


def root_os_error(exc: BaseException) -> OSError | None:
    """The deepest ``OSError`` down the ``__cause__`` chain, if any.

    Wrappers nest: an SDK re-raises ``from`` a socket error, an evidence
    store raises through a bundle reader that wrapped an artifact reader."""
    found: OSError | None = None
    for link in exception_chain(exc):
        if isinstance(link, OSError):
            found = link
    return found
