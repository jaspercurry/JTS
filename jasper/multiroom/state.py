"""Fresh-read state reader for multiroom grouping — the ONE place that
resolves "what is the grouping config right now" for *display/aggregation*
consumers, chiefly ``jasper-control``'s ``/state`` aggregator.

Mirrors :mod:`jasper.voice.provider_state`: it deliberately re-reads the
SSOT file (``/var/lib/jasper/grouping.env``) on **every call** so a wizard
save is reflected immediately, **without restarting the long-lived
jasper-control daemon**. It must therefore NEVER read ``os.environ`` —
long-lived daemons load the env file once at process start, so
``os.environ`` would be frozen at the value from boot. That is the
stale-dashboard bug this fresh-read shape exists to prevent.

Total + fail-soft: a missing, unreadable, or malformed file resolves to
the all-off config (``enabled=False``) rather than raising. A
configured-but-invalid bond keeps ``enabled=True`` and carries a specific
``error`` string — that is the fail-LOUD signal the doctor/dashboard
surfaces. All of that logic already lives in
:func:`jasper.multiroom.config.load_config`; this module is the thin,
JSON-able projection of it.
"""
from __future__ import annotations

from typing import Any

from .config import GROUPING_ENV_FILE, load_config


def read_grouping_state(path: str = GROUPING_ENV_FILE) -> dict[str, Any]:
    """Read the grouping config fresh from the SSOT file and return a
    JSON-able snapshot dict for ``/state`` and the dashboard.

    Re-reads ``path`` on every call (the fresh-read contract — never
    ``os.environ``). Total: never raises. A missing / unreadable /
    malformed file resolves to the disabled snapshot
    (``enabled=False``, ``error=None``); an enabled-but-invalid file
    keeps ``enabled=True`` with a populated ``error`` (fail-LOUD).

    Keys: ``enabled``, ``role``, ``channel``, ``bond_id``,
    ``leader_addr``, ``buffer_ms``, ``codec``, ``error`` — the
    GroupingConfig fields, in declaration order.
    """
    cfg = load_config(path)
    return {
        "enabled": cfg.enabled,
        "role": cfg.role,
        "channel": cfg.channel,
        "bond_id": cfg.bond_id,
        "leader_addr": cfg.leader_addr,
        "buffer_ms": cfg.buffer_ms,
        "codec": cfg.codec,
        "error": cfg.error,
    }
