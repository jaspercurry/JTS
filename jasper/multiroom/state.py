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
from .reconcile import (
    desired_snapfifo_path,
    plan,
)

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
    *,
    leader_tap_path: str = "",
) -> dict[str, Any]:
    """Combine the declared config with actual unit states into a runtime
    health verdict. PURE and total — no I/O, no clock.

    ``unit_states`` maps a unit name to its ``systemctl is-active`` word
    (``active`` / ``inactive`` / ``failed`` / ``activating`` /
    ``unknown``). "What should be running" is taken from
    :func:`jasper.multiroom.reconcile.plan`, so this never re-derives the
    leader/follower→units mapping — there is one definition of it.

    ``leader_tap_path`` is the LEADER's live music-producer feed path ("" =
    "no producer is feeding the snapfifo"). It is INJECTED, never read
    here, so this function stays pure. Production injects
    :func:`jasper.multiroom.leader_config.active_leader_pipe_path` — the
    ACTIVE CamillaDSP config scanned for the pipe sink (Increment 5;
    daemon-adjacent truth, never an env-intent mirror) — so a leader
    whose active config does not write the pipe honestly derives
    ``degraded``. Only consulted for a valid leader; a follower / solo /
    invalid config has no producer concept and the argument is ignored.

    ``health``:
      - ``off``      — grouping disabled (solo).
      - ``invalid``  — enabled but ``cfg.error`` (fail-LOUD; the bond is
                       misconfigured and the reconciler refuses to start
                       it). ``detail`` is the error.
      - ``degraded`` — a unit that SHOULD be running is not ``active``,
                       OR (leader only) the snap units are up but outputd
                       is not tapping the stream — snapserver reads a green
                       ``active`` while the FIFO is empty and followers get
                       silence. Both are §7 visible-failure cases: a
                       follower whose snapclient is ``failed``/``inactive``
                       (leader unreachable), a leader whose snapserver is
                       down, or a leader whose stream source is dry.
      - ``ok``       — every unit the plan wants up is ``active`` (and, for
                       a leader, outputd is tapping the stream).

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

    # Snap units are up. For a leader, the stream source must ALSO be live:
    # if the role needs a producer (``desired_snapfifo_path``) but nothing
    # is feeding the FIFO (``leader_tap_path`` empty), snapserver streams an
    # empty FIFO and followers get silence while every unit reads "active".
    # Surface that as degraded rather than a green-looking-but-dry bond.
    if cfg.role == "leader" and desired_snapfifo_path(cfg) and not leader_tap_path:
        return {
            "health": "degraded",
            "detail": (
                "leader's active CamillaDSP config does not write the "
                "snapserver pipe — the stream is silent; the reconciler's "
                "bond apply did not land (check "
                "jasper-grouping-reconcile's journal)"
            ),
            "units": units,
        }

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
    tap_path_reader: Callable[[], str] | None = None,
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

    For a VALID LEADER, ``leader_tap_path`` is injected into the pure
    derive. Production reads it from
    :func:`jasper.multiroom.leader_config.active_leader_pipe_path` —
    the ACTIVE CamillaDSP config scanned for the pipe sink (Increment 5;
    daemon-adjacent truth: camilla's own statefile names the config) —
    never an env-intent mirror (the removed ``SNAPFIFO_PRODUCER_WIRED``
    lesson). A leader whose active config does not write the pipe
    honestly shows ``degraded`` instead of a healthy-looking-but-silent
    bond.

    ``unit_state_reader`` and ``tap_path_reader`` are injectable for tests;
    production uses :func:`read_unit_active_states` and
    :func:`active_leader_pipe_path`.
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
        tap = ""
        if cfg.error is None:
            reader = unit_state_reader or read_unit_active_states
            states = reader([it.unit for it in plan(cfg).intents])
            # Leader producer feed (Increment 5): the ACTIVE CamillaDSP
            # config scanned for the pipe sink — daemon-adjacent truth,
            # never an env-intent mirror. Only consulted for a leader.
            if cfg.role == "leader":
                if tap_path_reader is None:
                    from .leader_config import active_leader_pipe_path
                    tap_path_reader = active_leader_pipe_path
                tap = tap_path_reader()
        snapshot["runtime"] = derive_grouping_runtime(
            cfg, states, leader_tap_path=tap
        )
    return snapshot


# ----------------------------------------------------------------------
# GET /grouping wire contract — ONE home for the envelope shape.
#
# jasper-control's GET /grouping handler (the PRODUCER) and the /rooms
# /unbond fan-out (the CONSUMER, jasper.web.rooms_setup._get_member_grouping)
# are in different daemons and exchange JSON over HTTP. The C4 regression
# (2026-06-09) was exactly this contract drifting: the producer nested the
# snapshot under a "grouping" key while the consumer read bond_id at the top
# level, so /unbond matched no real peer. Both sides now go through the two
# functions below, which share GROUPING_RESPONSE_KEY — so the envelope shape
# is defined in exactly one place and the two daemons cannot drift by
# construction. A round-trip test locks parse(build(x)) == x.
# ----------------------------------------------------------------------

# The single key the GET /grouping body nests its snapshot under. Nested
# (rather than a bare dict) so a fail-soft read returns {"grouping": null}
# unambiguously — None means "read failed / unknown", distinct from a real
# disabled snapshot.
GROUPING_RESPONSE_KEY = "grouping"


def grouping_response(grouping: dict | None) -> dict:
    """Build the GET /grouping wire body from a snapshot (or None on a
    fail-soft read). The PRODUCER side of the contract — used by
    jasper-control's handler. Inverse of :func:`parse_grouping_response`."""
    return {GROUPING_RESPONSE_KEY: grouping}


def parse_grouping_response(body: object) -> dict | None:
    """Extract the inner grouping snapshot from a GET /grouping body, or None
    when it's absent / null / not a dict (treat as "unknown", so it can never
    spuriously match a bond_id). The CONSUMER side of the contract — used by
    the /rooms /unbond discovery. Inverse of :func:`grouping_response`; total,
    never raises."""
    if not isinstance(body, dict):
        return None
    inner = body.get(GROUPING_RESPONSE_KEY)
    return inner if isinstance(inner, dict) else None
