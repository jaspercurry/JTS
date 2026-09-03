# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Write a librespot state fixture in the --onevent hook's file contract.

One writer, so the next format change breaks here instead of in every
consumer's tests. Tests that pin the reader against deliberately malformed
text write it literally rather than going through this.
"""
from __future__ import annotations

from pathlib import Path


def write_librespot_state(path: str | Path, **keys: object) -> Path:
    """Write ``keys`` as the hook's ``KEY=value`` lines; returns ``path``.

    Keys are named in the reader's lowercase vocabulary (``playing``,
    ``volume``, ``uri``); bools become the ``1``/``0`` the hook emits.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(
        f"{key.upper()}={1 if value is True else 0 if value is False else value}\n"
        for key, value in keys.items()
    ))
    return p
