# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom speaker grouping ("bonds") for JTS.

When a household wants two or more JTS speakers to play the same audio
in sync — a stereo pair across a room, whole-home mono — this package
owns the coordination. A bond has one *leader* (runs a snapserver, the
timing master) and one or more *followers* (run snapclients pointed at
the leader); each speaker plays a single assigned *channel*
(stereo / left / right / mono) of the bond's stream.

**Off by default.** A solo speaker pays nothing: with grouping `off`,
no snapserver or snapclient runs, no channel split happens, no socket
opens. The user explicitly opts in (later phase: a web wizard writes
`/var/lib/jasper/grouping.env` — ABSENT means off, exactly like
`/var/lib/jasper/peering.env`).

Off-by-default plumbing has landed (config + the reconciler decision
layer + a /state reader); the BondedSet / channel-split / volume system
and the live snapcast lifecycle arrive in later phases. The pure layers
(config, plan, argv builders, state) do no I/O beyond reading the SSOT
file; only the reconciler's thin `main()` entrypoint touches systemd
(start/stop units) — and even that does not run until a household opts
in, so a solo speaker spawns no subprocess and opens no socket.

Fail-safe vs fail-loud (mirrors peering + the project rule):
  - Missing / unreadable / malformed file => grouping OFF, no error.
    A broken file must never silently leave grouping ON.
  - Explicitly ON but internally inconsistent => stays ON with a
    specific `error` string the doctor surfaces ("configured but
    broken" is a state the operator must see).

Public surface — re-exported from this package:

  - GroupingConfig            — frozen resolved-config dataclass
  - load_config / is_enabled  — pure loader over /var/lib/jasper/grouping.env

This docstring describes the package's original 3-module shape
(config.py/reconcile.py/state.py); it has since grown to 17 modules
(leader/follower/member config, channel splitting, cascade timing,
airplay latency, sync measurement, snapcast RPC, TTS routing, and
more) across multiple shipped phases.
"""
from __future__ import annotations

from typing import Any

from . import config
from .config import GroupingConfig

# Convention for every module in this package: resolve config *callables*
# through the `config` module at call time (``config.load_config(...)``),
# never via ``from .config import load_config``. A from-import binds the
# value at import time, so a test monkeypatching
# ``jasper.multiroom.config.load_config`` neither reaches the captured
# binding nor undoes it at teardown (#1270, #1678). Constants and types
# (``GROUPING_ENV_FILE``, ``GroupingConfig``) are immutable — import those
# directly.
__all__ = [
    "GroupingConfig",
    "is_enabled",
    "load_config",
]

_CONFIG_CALLABLES = frozenset({"is_enabled", "load_config"})


def __getattr__(name: str) -> Any:
    if name in _CONFIG_CALLABLES:
        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
