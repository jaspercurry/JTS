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

Two layers:

  - **Declared config** (``read_grouping_state``'s base keys) — what the
    wizard wrote: enabled/role/channel/bond/error. Pure projection of
    :func:`jasper.multiroom.config.load_config`.
  - **Runtime health** (the ``runtime`` block, present only when grouping
    is ENABLED) — whether the snapcast units the reconciler's plan says
    should be running ACTUALLY are. This is the §7 "make it visible, not
    invisible" surface: a bond that is configured-valid but whose
    follower can't reach its leader (snapclient ``failed``) shows
    ``health: degraded`` here, not a green-looking config with silent
    breakage underneath. On a solo speaker (grouping off) there is no
    ``runtime`` block and no ``systemctl`` probe — zero added cost.

Pure/IO split mirrors the reconciler: :func:`derive_grouping_runtime` is
a PURE, total function of (config, unit-states) and is unit-tested with
synthetic inputs; :func:`read_unit_active_states` is the thin
``systemctl`` edge (injectable, validated on hardware).

Total + fail-soft throughout: a missing/unreadable/malformed file
resolves to the all-off snapshot; a failed ``systemctl`` probe resolves
every unit to ``"unknown"`` rather than raising. Nothing here can crash
the ``/state`` aggregator.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable

from .config import GROUPING_ENV_FILE, GroupingConfig, load_config
from .reconcile import plan

# How long to wait on the `systemctl is-active` probe before giving up
# and reporting "unknown". Bounded so a wedged systemd can't stall the
# /state aggregator.
_PROBE_TIMEOUT_SEC = 5


def read_unit_active_states(units: list[str]) -> dict[str, str]:
    """Thin I/O: ``systemctl is-active <units…>`` → ``{unit: state}``.

    One subprocess for ALL units (``is-active`` prints one state line per
    argument, in order). Fail-soft: any error — no systemd, timeout,
    line-count mismatch — resolves every unit to ``"unknown"`` rather
    than raising. NOT unit-tested (it shells out); the pure
    :func:`derive_grouping_runtime` consumes its output via an injected
    reader in tests.

    ``systemctl is-active`` exits non-zero when any unit is not active —
    that is the normal "follower can't reach leader" case, so we read
    stdout and ignore the exit code.
    """
    if not units:
        return {}
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", *units],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
        lines = proc.stdout.splitlines()
        if len(lines) == len(units):
            return {u: (lines[i].strip() or "unknown") for i, u in enumerate(units)}
    except (OSError, subprocess.SubprocessError):
        pass
    return {u: "unknown" for u in units}


def derive_grouping_runtime(
    cfg: GroupingConfig,
    unit_states: dict[str, str],
) -> dict[str, Any]:
    """Combine the declared config with actual unit states into a runtime
    health verdict. PURE and total — no I/O, no clock.

    ``unit_states`` maps a unit name to its ``systemctl is-active`` word
    (``active`` / ``inactive`` / ``failed`` / ``activating`` /
    ``unknown``). "What should be running" is taken from
    :func:`jasper.multiroom.reconcile.plan`, so this never re-derives the
    leader/follower→units mapping — there is one definition of it.

    ``health``:
      - ``off``      — grouping disabled (solo).
      - ``invalid``  — enabled but ``cfg.error`` (fail-LOUD; the bond is
                       misconfigured and the reconciler refuses to start
                       it). ``detail`` is the error.
      - ``degraded`` — a unit that SHOULD be running is not ``active``.
                       The §7 visible-failure case: a follower whose
                       snapclient is ``failed``/``inactive`` (leader
                       unreachable), or a leader whose snapserver is down.
      - ``ok``       — every unit the plan wants up is ``active``.

    Always reports per-unit ``{expected, actual}`` so a dashboard can
    show exactly which leg is down.
    """
    if not cfg.enabled:
        return {"health": "off", "detail": "grouping off (solo)", "units": {}}
    if cfg.error is not None:
        return {"health": "invalid", "detail": cfg.error, "units": {}}

    expected = {it.unit: it.desired for it in plan(cfg).intents}
    units: dict[str, dict[str, str]] = {}
    down: list[str] = []
    for unit, desired in expected.items():
        actual = unit_states.get(unit, "unknown")
        units[unit] = {"expected": desired, "actual": actual}
        if desired == "start" and actual != "active":
            down.append(unit)

    if down:
        if cfg.role == "follower":
            sc = unit_states.get("jasper-snapclient.service", "unknown")
            detail = (
                f"follower not connected — snapclient {sc}; "
                f"leader {cfg.leader_addr} unreachable?"
            )
        else:
            detail = "leader degraded — " + ", ".join(
                f"{u}={unit_states.get(u, 'unknown')}" for u in down
            )
        return {"health": "degraded", "detail": detail, "units": units}

    detail = (
        f"leader streaming (bond {cfg.bond_id})"
        if cfg.role == "leader"
        else f"follower connected to {cfg.leader_addr} (bond {cfg.bond_id})"
    )
    return {"health": "ok", "detail": detail, "units": units}


def read_grouping_state(
    path: str = GROUPING_ENV_FILE,
    *,
    unit_state_reader: Callable[[list[str]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Read the grouping config fresh from the SSOT file and return a
    JSON-able snapshot dict for ``/state`` and the dashboard.

    Re-reads ``path`` on every call (the fresh-read contract — never
    ``os.environ``). Total: never raises. A missing / unreadable /
    malformed file resolves to the disabled snapshot
    (``enabled=False``, ``error=None``); an enabled-but-invalid file
    keeps ``enabled=True`` with a populated ``error`` (fail-LOUD).

    Base keys: ``enabled``, ``role``, ``channel``, ``bond_id``,
    ``leader_addr``, ``buffer_ms``, ``codec``, ``error`` — the
    GroupingConfig fields, in declaration order.

    When grouping is ENABLED, a ``runtime`` block is added with the live
    snap-unit health (see :func:`derive_grouping_runtime`). On a solo
    speaker (grouping off) there is NO ``runtime`` key and NO
    ``systemctl`` probe — the snapshot is byte-for-byte what it was
    before this surface existed, preserving the zero-cost-when-N=1
    contract. The units are only probed for a valid bond (an INVALID
    bond's plan starts nothing, so there is nothing to probe).

    ``unit_state_reader`` is injectable for tests; production uses
    :func:`read_unit_active_states`.
    """
    cfg = load_config(path)
    snapshot: dict[str, Any] = {
        "enabled": cfg.enabled,
        "role": cfg.role,
        "channel": cfg.channel,
        "bond_id": cfg.bond_id,
        "leader_addr": cfg.leader_addr,
        "buffer_ms": cfg.buffer_ms,
        "codec": cfg.codec,
        "error": cfg.error,
    }
    if cfg.enabled:
        states: dict[str, str] = {}
        if cfg.error is None:
            reader = unit_state_reader or read_unit_active_states
            states = reader([it.unit for it in plan(cfg).intents])
        snapshot["runtime"] = derive_grouping_runtime(cfg, states)
    return snapshot
