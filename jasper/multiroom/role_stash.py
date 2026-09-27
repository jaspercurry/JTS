# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The CamillaDSP role-swap stash ladder, owned once and bound per grouping
arm (``leader_config``, ``follower_config``) to that arm's own persistent
stash path, so the arms never fight over one file. Wraps ``_stash``'s
read/write/clear + camilla-factory mechanics; each arm keeps its own
module-level names (``_camilla``, ``read_stash``, ``_write_stash``,
``_clear_stash``) bound to its own :class:`RoleStash` instance — a test seam
(each name is monkeypatched per-module in the arm's own tests) and the
reconcile idiom of re-reading a possibly test-redirected module constant at
call time (an explicit ``path`` always wins over the bound default, so it
never goes through a delegation's def-time default)."""
from __future__ import annotations

from dataclasses import dataclass

from . import _stash


@dataclass(frozen=True)
class RoleStash:
    """One arm's stash ladder, defaulting to ``path``. Every method takes an
    optional ``path`` override so a caller can still target another file
    (a generic roundtrip test, or a sibling arm reusing this arm's ladder
    with its own constant — see ``active_leader_config``)."""

    path: str

    def camilla(self):
        """The default `camilla_factory`: camilla#1
        (jasper.camilla.primary_controller)."""
        return _stash.camilla()

    def read_stash(self, path: str | None = None) -> str | None:
        """The stashed prior config path, or None (no stash / unreadable)."""
        return _stash.read_stash(self.path if path is None else path)

    def write_stash(self, value: str, path: str | None = None) -> None:
        _stash.write_stash(value, self.path if path is None else path)

    def clear_stash(self, path: str | None = None) -> None:
        _stash.clear_stash(self.path if path is None else path)
