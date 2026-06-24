# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .config import GROUPING_ENV_FILE, GroupingConfig, load_config
from .reconcile import (
    FOLLOWER_STATUS_FILE,
    SNAP_STREAM_ID,
    desired_snapfifo_path,
    plan,
)

# How long to wait on the `systemctl is-active` probe before giving up
# and reporting "unknown". Bounded so a wedged systemd can't stall the
# /state aggregator.
_PROBE_TIMEOUT_SEC = 5


def read_active_follower_status(path: str = FOLLOWER_STATUS_FILE) -> dict[str, Any]:
    """Fresh-read the reconciler's active-follower endpoint status (Slice 3).

    Returns ``{active_follower: bool, blocked_reason: str}`` or ``{}`` when the
    file is missing / unreadable / malformed. Total + fail-soft: never raises,
    never reads ``os.environ`` (jasper-control is not restarted on a bond, so a
    cached env would go stale — the fresh-read contract this module exists for)."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        "active_follower": bool(raw.get("active_follower")),
        "active_leader": bool(raw.get("active_leader")),
        "blocked_reason": str(raw.get("blocked_reason") or ""),
    }


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


def _dac_content_signal(local_outputd_status: Any) -> dict[str, Any]:
    """Local outputd dac_content signal for the pair-lock verdict. PURE.

    ``serving_fifo=true`` is useful: it says snapclient is feeding bytes
    into outputd's bonded-member lane. It is deliberately NOT a clock-lock
    signal; Snapcast's rate loop may still be unproven from JTS's point of
    view. Keep that distinction explicit in the JSON so dashboards and
    doctor never collapse "bytes flow" into "sample lock proven."
    """
    signal: dict[str, Any] = {
        "source": "outputd.dac_content",
        "available": False,
        "enabled": None,
        "serving_fifo": None,
        "bytes_flowing": None,
        "meaning": "serving_fifo means bytes are flowing; it is not a clock-lock signal",
    }
    if not isinstance(local_outputd_status, dict):
        signal["detail"] = "outputd STATUS unavailable"
        return signal
    dac = local_outputd_status.get("dac_content")
    if not isinstance(dac, dict):
        signal["detail"] = "outputd STATUS has no dac_content block"
        return signal
    enabled = dac.get("enabled") is True
    serving = dac.get("serving_fifo") is True
    signal.update({
        "available": True,
        "enabled": enabled,
        "serving_fifo": serving,
        "bytes_flowing": enabled and serving,
        "detail": (
            "snapclient is feeding the local outputd FIFO"
            if enabled and serving
            else "local outputd FIFO is not serving bonded content"
        ),
    })
    return signal


def _stream_client_signal(
    stream_clients: Any,
    *,
    self_name: str = "",
    want_stream: str = "",
) -> dict[str, Any]:
    """Snapcast RPC signal used by the pair-lock verdict. PURE."""
    signal: dict[str, Any] = {
        "source": "snapcast.Server.GetStatus",
        "available": False,
        "reachable": None,
        "connected": 0,
        "audible": 0,
        "wrong_stream": 0,
        "muted_or_zero": 0,
        "own_client_connected": None,
        "clients": [],
        "meaning": (
            "Snapcast RPC exposes connection, stream binding, configured "
            "latency, stream status, and software volume; it does not "
            "expose buffer fill, drift, or clock lock"
        ),
    }
    if stream_clients is None:
        signal["detail"] = "snapcast registry not probed on this role"
        return signal
    if stream_clients == "unreachable":
        signal.update({
            "available": True,
            "reachable": False,
            "detail": "snapserver RPC unreachable",
        })
        return signal
    if not isinstance(stream_clients, list):
        signal["detail"] = "snapcast registry probe returned an unexpected shape"
        return signal

    clients: list[dict[str, Any]] = []
    connected = audible = wrong_stream = muted_or_zero = 0
    own_client_connected: bool | None = False if self_name else None
    for row in stream_clients:
        if not isinstance(row, dict):
            continue
        is_connected = row.get("connected") is True
        on_stream = not want_stream or row.get("stream_id") == want_stream
        is_muted = (
            row.get("muted") is True
            or row.get("group_muted") is True
            or row.get("volume_percent", 100) == 0
        )
        if is_connected:
            connected += 1
        if is_connected and not on_stream:
            wrong_stream += 1
        if is_connected and is_muted:
            muted_or_zero += 1
        if is_connected and on_stream and not is_muted:
            audible += 1
        if self_name and row.get("name") == self_name:
            own_client_connected = is_connected
        clients.append({
            "name": row.get("name") or "",
            "connected": is_connected,
            "stream_id": row.get("stream_id") or "",
            "stream_status": row.get("stream_status") or "",
            "muted": row.get("muted") is True,
            "group_muted": row.get("group_muted") is True,
            "volume_percent": row.get("volume_percent"),
            "latency_ms": row.get("latency_ms"),
        })

    signal.update({
        "available": True,
        "reachable": True,
        "connected": connected,
        "audible": audible,
        "wrong_stream": wrong_stream,
        "muted_or_zero": muted_or_zero,
        "own_client_connected": own_client_connected,
        "clients": clients,
        "detail": f"{audible}/{connected} connected snapclient(s) audible on {want_stream or 'the selected stream'}",
    })
    return signal


def _follower_clock_lock_signal() -> dict[str, Any]:
    """Follower clock-lock truth as exposed today. PURE.

    We intentionally return ``locked=None`` instead of guessing. Snapcast
    owns the sync engine, but its documented JSON-RPC control API does not
    publish the follower's buffer fill, drift, or time-lock state. A future
    snapclient-facing probe can replace this one signal without changing
    the surrounding verdict shape.
    """
    return {
        "source": "snapcast.Server.GetStatus",
        "status": "unobservable",
        "locked": None,
        "detail": (
            "Snapcast RPC does not expose follower buffer fill, drift, "
            "or time-lock; connected/latency/volume are not clock lock"
        ),
    }


def _derive_pair_lock(
    cfg: GroupingConfig,
    *,
    runtime_health: str,
    runtime_detail: str,
    stream_clients: Any = None,
    self_name: str = "",
    want_stream: str = "",
    local_outputd_status: Any = None,
) -> dict[str, Any]:
    """Composite "pair locked + healthy" verdict. PURE and total."""
    if not cfg.enabled:
        return {
            "applicable": False,
            "status": "off",
            "locked_and_healthy": False,
            "detail": "grouping off (solo)",
            "signals": {},
        }
    if cfg.error is not None:
        return {
            "applicable": False,
            "status": "invalid",
            "locked_and_healthy": False,
            "detail": cfg.error,
            "signals": {},
        }

    fifo = _dac_content_signal(local_outputd_status)
    stream = _stream_client_signal(
        stream_clients, self_name=self_name, want_stream=want_stream,
    )
    clock = _follower_clock_lock_signal()
    signals = {
        "snapcast_clients": stream,
        "local_fifo": fifo,
        "follower_clock_lock": clock,
    }

    if runtime_health != "ok":
        return {
            "applicable": True,
            "status": "degraded",
            "locked_and_healthy": False,
            "detail": runtime_detail,
            "signals": signals,
        }
    if fifo.get("available") and fifo.get("bytes_flowing") is False:
        return {
            "applicable": True,
            "status": "degraded",
            "locked_and_healthy": False,
            "detail": "local bonded output lane is not serving FIFO bytes",
            "signals": signals,
        }
    if stream.get("reachable") is False:
        return {
            "applicable": True,
            "status": "degraded",
            "locked_and_healthy": False,
            "detail": "snapserver RPC unreachable; pair health cannot be verified",
            "signals": signals,
        }
    if (
        stream.get("reachable") is True
        and (
            stream.get("wrong_stream", 0) > 0
            or stream.get("muted_or_zero", 0) > 0
            or stream.get("own_client_connected") is False
        )
    ):
        return {
            "applicable": True,
            "status": "degraded",
            "locked_and_healthy": False,
            "detail": "snapcast clients are connected but not all audible on the JTS stream",
            "signals": signals,
        }
    return {
        "applicable": True,
        "status": "unknown",
        "locked_and_healthy": False,
        "detail": (
            "unit/binding/byte-flow health is clear enough, but follower "
            "clock lock is unobservable from Snapcast RPC"
        ),
        "signals": signals,
    }


def _runtime_with_pair_lock(
    runtime: dict[str, Any],
    cfg: GroupingConfig,
    *,
    stream_clients: Any = None,
    self_name: str = "",
    want_stream: str = "",
    local_outputd_status: Any = None,
) -> dict[str, Any]:
    runtime = dict(runtime)
    runtime["pair_lock"] = _derive_pair_lock(
        cfg,
        runtime_health=str(runtime.get("health") or ""),
        runtime_detail=str(runtime.get("detail") or ""),
        stream_clients=stream_clients,
        self_name=self_name,
        want_stream=want_stream,
        local_outputd_status=local_outputd_status,
    )
    return runtime


def derive_grouping_runtime(
    cfg: GroupingConfig,
    unit_states: dict[str, str],
    *,
    leader_tap_path: str = "",
    stream_clients: Any = None,
    self_name: str = "",
    want_stream: str = "",
    local_outputd_status: Any = None,
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

    ``stream_clients`` (LEADER only — the 2026-06-11 silent-bond lesson:
    every unit green, the pipe wired, and yet the bond mute because
    snapcast's PERSISTED group→stream binding pointed at a stale stream
    and the leader's client played zeros): the snapserver client-binding
    rows from :func:`jasper.multiroom.snapcast_rpc.read_stream_clients`.
    ``None`` = not probed (solo / follower / tests of other facets — no
    verdict change); the literal string ``"unreachable"`` = snapserver
    RPC down → degraded; a list → three silence classes are checked:
    a CONNECTED client bound to a stream other than ``want_stream``, a
    CONNECTED client muted or at volume 0 (snapclient's software mixer
    scales samples — zeros flow, everything stays green), and
    ``self_name``'s own client absent/disconnected (the leader must hear
    itself). INJECTED, never read here — stays pure.

    ``health``:
      - ``off``      — grouping disabled (solo).
      - ``invalid``  — enabled but ``cfg.error`` (fail-LOUD; the bond is
                       misconfigured and the reconciler refuses to start
                       it). ``detail`` is the error.
      - ``degraded`` — a unit that SHOULD be running is not ``active``,
                       OR (leader only) the snap units are up but the active
                       CamillaDSP config is not writing the pipe — snapserver reads a green
                       ``active`` while the FIFO is empty and followers get
                       silence. Both are §7 visible-failure cases: a
                       follower whose snapclient is ``failed``/``inactive``
                       (leader unreachable), a leader whose snapserver is
                       down, or a leader whose stream source is dry.
      - ``ok``       — every unit the plan wants up is ``active`` (and, for
                       a leader, active CamillaDSP writes the pipe).

    Always reports per-unit ``{expected, actual}`` so a dashboard can
    show exactly which leg is down.
    """
    if not cfg.enabled:
        return _runtime_with_pair_lock(
            {"health": "off", "detail": "grouping off (solo)", "units": {}},
            cfg,
        )
    if cfg.error is not None:
        return _runtime_with_pair_lock(
            {"health": "invalid", "detail": cfg.error, "units": {}},
            cfg,
        )

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
        return _runtime_with_pair_lock(
            {"health": "degraded", "detail": detail, "units": units},
            cfg,
            stream_clients=stream_clients,
            self_name=self_name,
            want_stream=want_stream,
            local_outputd_status=local_outputd_status,
        )

    # Snap units are up. For a leader, the stream source must ALSO be live:
    # if the role needs a producer (``desired_snapfifo_path``) but nothing
    # is feeding the FIFO (``leader_tap_path`` empty), snapserver streams an
    # empty FIFO and followers get silence while every unit reads "active".
    # Surface that as degraded rather than a green-looking-but-dry bond.
    if cfg.role == "leader" and desired_snapfifo_path(cfg) and not leader_tap_path:
        return _runtime_with_pair_lock(
            {
                "health": "degraded",
                "detail": (
                    "leader's active CamillaDSP config does not write the "
                    "snapserver pipe — the stream is silent; the reconciler's "
                    "bond apply did not land (check "
                    "jasper-grouping-reconcile's journal)"
                ),
                "units": units,
            },
            cfg,
            stream_clients=stream_clients,
            self_name=self_name,
            want_stream=want_stream,
            local_outputd_status=local_outputd_status,
        )

    # Stream-binding + client-audibility truth (leader only; see the
    # stream_clients docstring). Unit states + the pipe config cannot
    # see these — the 2026-06-11 silent-bond class.
    if cfg.role == "leader" and stream_clients is not None:
        if stream_clients == "unreachable":
            return _runtime_with_pair_lock(
                {
                    "health": "degraded",
                    "detail": (
                        "snapserver RPC unreachable — client stream bindings "
                        "cannot be verified (run jasper-grouping-reconcile, "
                        "check jasper-snapserver)"
                    ),
                    "units": units,
                },
                cfg,
                stream_clients=stream_clients,
                self_name=self_name,
                want_stream=want_stream,
                local_outputd_status=local_outputd_status,
            )
        for row in stream_clients:
            if row.get("connected") and want_stream and row.get("stream_id") != want_stream:
                return _runtime_with_pair_lock(
                    {
                        "health": "degraded",
                        "detail": (
                            f"client {row.get('name') or '?'} is bound to stream "
                            f"{row.get('stream_id') or '(none)'} (want {want_stream}) "
                            "— it hears silence; run jasper-grouping-reconcile"
                        ),
                        "units": units,
                    },
                    cfg,
                    stream_clients=stream_clients,
                    self_name=self_name,
                    want_stream=want_stream,
                    local_outputd_status=local_outputd_status,
                )
            if row.get("connected") and (
                row.get("muted")
                or row.get("group_muted")
                or row.get("volume_percent", 100) == 0
            ):
                return _runtime_with_pair_lock(
                    {
                        "health": "degraded",
                        "detail": (
                            f"client {row.get('name') or '?'} is muted or at "
                            "volume 0 in snapcast — its software mixer plays "
                            "zeros; unmute via the snapcast registry"
                        ),
                        "units": units,
                    },
                    cfg,
                    stream_clients=stream_clients,
                    self_name=self_name,
                    want_stream=want_stream,
                    local_outputd_status=local_outputd_status,
                )
        if self_name and not any(
            row.get("name") == self_name and row.get("connected")
            for row in stream_clients
        ):
            return _runtime_with_pair_lock(
                {
                    "health": "degraded",
                    "detail": (
                        f"leader's own snapclient ({self_name}) is not connected "
                        "to snapserver — the leader cannot hear its own bond"
                    ),
                    "units": units,
                },
                cfg,
                stream_clients=stream_clients,
                self_name=self_name,
                want_stream=want_stream,
                local_outputd_status=local_outputd_status,
            )

    detail = (
        f"leader streaming (bond {cfg.bond_id})"
        if cfg.role == "leader"
        else f"follower connected to {cfg.leader_addr} (bond {cfg.bond_id})"
    )
    return _runtime_with_pair_lock(
        {"health": "ok", "detail": detail, "units": units},
        cfg,
        stream_clients=stream_clients,
        self_name=self_name,
        want_stream=want_stream,
        local_outputd_status=local_outputd_status,
    )


def _self_client_name() -> str:
    """This speaker's snapcast client name (snapclient reports the bare
    hostname). Total: any failure resolves to "" (the own-client check
    is then skipped rather than wrong)."""
    try:
        import socket

        return socket.gethostname().strip()
    except OSError:
        return ""


def read_grouping_state(
    path: str = GROUPING_ENV_FILE,
    *,
    unit_state_reader: Callable[[list[str]], dict[str, str]] | None = None,
    tap_path_reader: Callable[[], str] | None = None,
    stream_clients_reader: Callable[[], Any] | None = None,
    local_outputd_reader: Callable[[], Any] | None = None,
    endpoint_status_reader: Callable[[], dict[str, Any]] | None = None,
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
    subwoofer_present = (
        cfg.subwoofer_present
        or cfg.channel == "sub"
        or any(m.channel == "sub" for m in cfg.roster)
    )
    snapshot: dict[str, Any] = {
        "enabled": cfg.enabled,
        "role": cfg.role,
        "channel": cfg.channel,
        "bond_id": cfg.bond_id,
        "leader_addr": cfg.leader_addr,
        "buffer_ms": cfg.buffer_ms,
        "codec": cfg.codec,
        "trim_db": cfg.trim_db,
        "mains_highpass_enabled": cfg.mains_highpass_enabled,
        "subwoofer_present": subwoofer_present,
        "peer_addr": cfg.peer_addr,
        "peer_name": cfg.peer_name,
        # The bond roster (leader only): every follower the leader recorded
        # at bond time, so the UI + _unbond can disable ALL members (not just
        # the L/R sibling). [] on followers, solo, and legacy bonds.
        "roster": [
            {"addr": m.addr, "name": m.name, "channel": m.channel}
            for m in cfg.roster
        ],
        "error": cfg.error,
    }
    # Receiver-side wireless-sub crossover corner. Surfaced for the sub (its
    # outputd LR4 low-pass) and for mains in a bond that has a sub (their
    # outputd LR4 high-pass + /rooms toggle fan-out need the same SSOT).
    # Read fresh from grouping.env, never os.environ.
    if subwoofer_present:
        snapshot["crossover_hz"] = cfg.crossover_hz
    if cfg.enabled:
        states: dict[str, str] = {}
        tap = ""
        stream_clients: Any = None
        local_outputd_status: Any = None
        if cfg.error is None:
            reader = unit_state_reader or read_unit_active_states
            states = reader([it.unit for it in plan(cfg).intents])
            if local_outputd_reader is not None:
                try:
                    local_outputd_status = local_outputd_reader()
                except (OSError, RuntimeError, TypeError, ValueError):
                    local_outputd_status = None
            # Leader producer feed (Increment 5): the ACTIVE CamillaDSP
            # config scanned for the pipe sink — daemon-adjacent truth,
            # never an env-intent mirror. Only consulted for a leader.
            if cfg.role == "leader":
                if tap_path_reader is None:
                    from .leader_config import active_leader_pipe_path
                    tap_path_reader = active_leader_pipe_path
                tap = tap_path_reader()
                # Client-binding truth (the 2026-06-11 silent-bond
                # lesson): probe snapserver's registry fresh; RPC
                # failure maps to the explicit "unreachable" verdict
                # (never silently skipped — an unverifiable bond is a
                # degraded bond).
                if stream_clients_reader is None:
                    # The CACHED probe: /state is polled (dashboard ~7 s,
                    # any LAN client) and a hung snapserver costs the RPC
                    # timeout per probe — the TTL cache bounds that to one
                    # real probe per window. The doctor deliberately uses
                    # the FRESH variant (operator-run, this-instant truth).
                    from .snapcast_rpc import read_stream_clients_cached
                    stream_clients_reader = read_stream_clients_cached
                stream_clients = stream_clients_reader()
                if stream_clients is None:
                    stream_clients = "unreachable"
        snapshot["runtime"] = derive_grouping_runtime(
            cfg, states, leader_tap_path=tap,
            stream_clients=stream_clients,
            self_name=_self_client_name(),
            want_stream=SNAP_STREAM_ID,
            local_outputd_status=local_outputd_status,
        )

        # Active-endpoint surface (distributed-active Slice 3 follower / Slice 5
        # active leader): an ``endpoint`` block when this box runs its local
        # Layer-A crossover on the bonded stream — as a FOLLOWER, or as the active
        # LEADER (camilla#2, while camilla#1 bakes the wire) — OR when an
        # active-endpoint bond was REFUSED and the box fell back to solo active
        # (the fail-closed reason — the household-facing "why didn't it join the
        # group" signal; the audible cue through Layer A is the open Q2 spike
        # item). Gated on cfg.enabled so a solo speaker's snapshot stays
        # byte-for-byte unchanged (no status-file read, no extra key). Read fresh,
        # never os.environ.
        endpoint = (endpoint_status_reader or read_active_follower_status)()
        endpoint_follower = endpoint.get("active_follower")
        endpoint_leader = endpoint.get("active_leader")
        if endpoint_follower or endpoint_leader or endpoint.get("blocked_reason"):
            snapshot["endpoint"] = {
                # Both follower + active leader run a local Layer-A crossover;
                # ``role`` discriminates. "blocked" = bond refused (fail-closed).
                "mode": (
                    "active_crossover"
                    if endpoint_follower or endpoint_leader
                    else "blocked"
                ),
                "role": (
                    "leader" if endpoint_leader
                    else "follower" if endpoint_follower
                    else ""
                ),
                "blocked_reason": endpoint.get("blocked_reason", ""),
            }

        # Snapcast provisioning progress (the grouping opt-in install): surfaced
        # so the /rooms wizard can show "Installing Snapcast…" while the
        # reconciler apt-installs the binaries on first enable. Read fresh from
        # the reconciler-written status file (never os.environ). Gated on
        # cfg.enabled so a solo snapshot stays byte-identical, and only present
        # once the reconciler has written a status (installing/installed/failed).
        from .provision import read_provision_status

        provision = read_provision_status()
        if provision.get("state"):
            snapshot["provision"] = provision
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
