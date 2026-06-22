# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom speaker grouping ("bonds") for JTS.

When a household wants two or more JTS speakers to play the same audio
in sync — a stereo pair across a room, a leader plus a sub, whole-home
mono — this package owns the coordination. A bond has one *leader*
(runs a snapserver, the timing master) and one or more *followers*
(run snapclients pointed at the leader); each speaker plays a single
assigned *channel* (stereo / left / right / sub / mono) of the bond's
stream.

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

Module layout:

  config.py     pure  — GroupingConfig dataclass + env-file loader
  reconcile.py  mixed — pure plan()/argv builders + a thin systemctl main()
  state.py      pure  — fresh-read JSON-able snapshot for /state
"""
from __future__ import annotations

from .config import GroupingConfig, is_enabled, load_config

__all__ = [
    "GroupingConfig",
    "is_enabled",
    "load_config",
]
