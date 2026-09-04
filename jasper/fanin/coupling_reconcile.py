# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered convergence of the fan-in -> CamillaDSP ring coupling.

:mod:`jasper.fanin_coupling` owns the *vocabulary* (the flag, the ring device
names, the emit kwargs); this module owns the *convergence* across the three
audio daemons.

ONE TRANSPORT (ADR-0100). fan-in writes Ring A (program.ring) that CamillaDSP
captures via ``jts_ring_capture``; CamillaDSP writes its post-DSP program to
Ring B (content.ring) via ``jts_ring_playback`` that jasper-outputd reads — or,
on a roleful box whose active endpoint is armed, to the ACTIVE ring
(active-content.ring) via ``jts_ring_active_playback``. Both ends are ONE
coherent state: ``JASPER_FANIN_CAMILLA_COUPLING=shm_ring`` (fanin.env) AND
``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the post-DSP ring's path/slots
(outputd.env). ``_outputd_actions`` is the single writer of that pair.

NO FALLBACK. A pass writes the ring state and, when something moved, converges
the daemons in the order :func:`_converge_ring` documents. A step that fails
reports ``ok=False`` and the box PARKS visibly through
:mod:`jasper.control.transport_park`; recovery from a bad deploy is
``git revert`` + redeploy (ADR-0100).

SINGLE WRITER of the topology keys it owns: ``JASPER_FANIN_CAMILLA_COUPLING``
in ``/var/lib/jasper/fanin.env`` and the Ring B bridge keys in
``/var/lib/jasper/outputd.env``. The order-preserving single-key helpers
(:mod:`jasper.env_file`) leave neighboring operator/reconciler lines intact.

NOT a per-tick hot path. A pass whose env and ring geometry are already coherent
re-confirms CamillaDSP only and bounces nothing — a real convergence restarts the
SHARED fan-in daemon (a brief all-source glitch), so it is change-gated.
"""

from __future__ import annotations

import fcntl
import logging
from collections.abc import Callable
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import RuntimeEnvAction
from jasper.output_topology_runtime import GROUPING_RECONCILE_UNIT
from jasper.env_file import read_value, remove, upsert
from jasper.fanin.coupling_auto import (
    combo_is_armed,
    read_usb_gadget_available,
    usb_combo_actions,
    usbsink_effectively_enabled,
)
from jasper.fanin.latency_mode import (
    normalize_mode as normalize_usb_latency_mode,
    read_requested_mode as read_usb_latency_mode,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    OUTPUTD_RING_PATH_ENV_VAR,
    OUTPUTD_RING_SLOTS_ENV_VAR,
    resolve_outputd_ring_path,
    resolve_outputd_ring_slots,
)
from jasper.log_event import log_event

from jasper.fanin.ring_health import (
    _anchor_is_all_muted,
    _EnvSnapshot,
    _read_snapshot,
    _staged_anchor_identity,
    FANIN_ENV_PATH,
    OUTPUTD_ENV_PATH,
    read_loaded_camilla_graph,
    resolve_effective_fanin_ring_slots,
    resolve_effective_fanin_wire_format,
    ring_assets_ready,
    ring_edge_width_ready,
    ring_endpoint_anchor_converged,
    ring_topology_ready,
    ring_wire_caps_ready,
)

# Nothing below reads these; they are re-exported because callers BIND them on
# this module and would not see a ring_health-side patch: jasper/fanin/converge
# and jasper/fanin_coupling resolve them here through lazy imports, and the ring
# tests monkeypatch them here. Moving one is a behaviour change, not a rename.
from jasper.fanin.ring_health import (
    graph_at_active_ring_endpoint as graph_at_active_ring_endpoint,
    JASPER_ENV_PATH as JASPER_ENV_PATH,
    load_topology_for_wire as load_topology_for_wire,
    read_persisted_coupling as read_persisted_coupling,
)

logger = logging.getLogger(__name__)

FANIN_UNIT = "jasper-fanin.service"
OUTPUTD_UNIT = "jasper-outputd.service"
CAMILLA_UNIT = "jasper-camilla.service"
# NOT part of the ordered audio-graph bounce. jasper-voice is restarted by this
# module for exactly ONE reason: a coupling flip changed the box's resolved
# ASSISTANT wire width, which voice resolves once at start. See
# :func:`_try_restart_voice`.
VOICE_UNIT = "jasper-voice.service"
# Root oneshot that re-detects output hardware and re-emits the route floor
# actions into outputd.env. It is the single writer of
# ``JASPER_OUTPUTD_CONTENT_FORMAT``, which is why the spine below starts it
# before restarting outputd — see :func:`_converge_ring`.
AUDIO_HARDWARE_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"

# Legacy env key of a deleted coupling (the Camilla -> outputd File playback
# pipe). Nothing writes it; retained ONLY so ``_outputd_actions`` can UNSET a
# stale value off a migrating box's outputd.env. A one-way migration sweep
# target, not vocabulary.
_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV = "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE"

# Cross-invocation serialization of the reconcile ENTRY verbs.
# NOT under /run/jasper — that is jasper-voice's RuntimeDirectory, reaped on
# every voice stop; a reaped+recreated lock file would hand a second holder a
# fresh inode and defeat the exclusion exactly during deploys. Top-level /run is
# root-only tmpfs and every entry path runs as root (both oneshot units,
# install.sh, the sudo CLI). See :func:`_acquire_entry_lock`.
ENTRY_LOCK_PATH = "/run/jasper-fanin-coupling.lock"
ENTRY_LOCK_TIMEOUT_SECONDS = 10.0
ENTRY_LOCK_POLL_SECONDS = 0.2

# A daemon op (fan-in restart or camilla reconcile) returns (ok, detail).
DaemonOp = Callable[[], tuple[bool, str]]
# A ring preflight gate returns the same (ok, detail).
RingGate = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class CouplingResult:
    """Outcome of one ring-coupling convergence pass.

    ``ok`` is True only when the env write AND every daemon op the pass needed
    succeeded (or there was nothing to do). ``changed`` is True when the
    persisted env actually moved. ``detail`` carries the failure's reason and is
    the PARK's text: nothing is rolled back, so ``ok=False`` means a box that is
    not playing.
    """

    ok: bool
    changed: bool
    restarted_fanin: bool = False
    restarted_outputd: bool = False
    reconciled_camilla: bool = False
    detail: str = ""


# A DELIBERATE restart must not spend a daemon's CRASH-recovery start budget
# (ADR-0103): every restart this reconciler issues is a config-apply, but
# systemd counts it in the same StartLimitBurst window, and jasper-fanin /
# jasper-outputd escalate an exhausted window straight to
# StartLimitAction=reboot. ``systemctl reset-failed`` clears the failed latch AND
# the start rate counter, so a config-apply restart starts from a clean budget.
# Genuine crash loops still escalate: a daemon's own Restart= path never reaches
# this code.
# The units below are exactly the long-running daemons this module bounces; the
# oneshot owners it starts have no crash budget to protect
# (jasper-fanin-coupling-auto pins StartLimitIntervalSec=0) and are
# START_ONLY_UNITS in the broker, which would deny ``reset-failed`` anyway.
_START_BUDGET_VERBS = frozenset({"start", "restart", "try-restart"})
_CRASH_BUDGET_UNITS = frozenset({FANIN_UNIT, OUTPUTD_UNIT, CAMILLA_UNIT})
# Mirrors restart_broker._RESET_TIMEOUT_SEC (which owns the bound
# reset_then_manage actually applies); only the ceiling arithmetic below reads
# it, so it is spelled here rather than paid for as an import.
_RESET_FAILED_TIMEOUT_SEC = 5.0


def _restart_unit(
    unit: str, *, verb: str = "restart", reason: str, timeout: float,
    no_block: bool = False,
) -> tuple[bool, str]:
    """Drive a systemd unit through the broker with a closed verb. (ok, detail).

    ``verb`` is one of the broker's fixed vocabulary (``restart`` / ``stop`` /
    ``start`` / ...); ``no_block`` defaults False so the call returns only after
    systemd reports the transition complete — for a ``Type=notify`` unit like
    jasper-fanin that means ``READY=1`` (its ring/pipe writer re-attached), the
    "wait for fan-in up" step the camilla coordination below relies on. Pass True
    for a kick whose completion the caller does not wait on.

    A start-consuming verb on a crash-budget daemon goes through
    :func:`restart_broker.reset_then_manage` (see the block comment above).
    Guarded lazy import: a missing/broken control package degrades to a reported
    failure, never an exception out of the reconcile that would defeat the
    fail-safe ladder.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"
    drive = (
        restart_broker.reset_then_manage
        if verb in _START_BUDGET_VERBS and unit in _CRASH_BUDGET_UNITS
        else restart_broker.manage_units
    )
    resp = drive(
        unit,
        verb=verb,
        reason=reason,
        no_block=no_block,
        timeout=timeout,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def _restart_fanin(reason: str) -> tuple[bool, str]:
    """Restart jasper-fanin through the broker. (ok, detail).

    RESIDUAL, stated: fan-in's legal restart is its manager-default 90 s start
    plus a 5 s stop, so this 8 s bound is legally short and a genuinely slow
    restart is misreported. Measured restarts are 0.4-1.0 s. A truncation here
    can leave fan-in activating, which is why
    :data:`_CAMILLA_START_TIMEOUT_SEC` budgets it as a dependency.
    """
    return _restart_unit(FANIN_UNIT, reason=reason, timeout=8.0)


def _try_restart_voice(reason: str) -> tuple[bool, str]:
    """``try-restart`` jasper-voice through the broker. (ok, detail).

    ``try-restart``, not ``restart``, and the difference is load-bearing: a
    stopped jasper-voice must STAY stopped. A no-mic box parks the unit through
    its ``ConditionPathExists=!/var/lib/jasper/voice-input-absent`` gate, and an
    operator can stop it deliberately; a coupling flip is not permission to
    start either one. ``try-restart`` is a no-op on an inactive unit.

    Not in ``_CRASH_BUDGET_UNITS``: this fires only on an actual width
    TRANSITION — at most once per coupling flip — so it cannot walk the
    start-limit window the way the per-transaction fan-in bounces could.
    """
    return _restart_unit(VOICE_UNIT, verb="try-restart", reason=reason, timeout=8.0)


def _assistant_width_token(env_path: str | Path) -> str:
    """The box's resolved ASSISTANT wire width, from the persisted files.

    Read through :func:`jasper.fanin_coupling.assistant_wire_is_wide` — the same
    rule ``jasper-fanin``'s ``Config::program_wire_is_wide`` calls and
    ``jasper-voice`` resolves at start — so this observes the transition voice
    would observe.

    BOTH halves come from ``env_path`` when it declares them, and only then fall
    back to the standard ``jasper.env`` -> ``fanin.env`` chain (where the format
    key may legitimately live in the base file). With an explicit path, reading
    the coupling from the caller's file and the format from a module constant
    would make the predicate only accidentally coherent.
    """
    from jasper.fanin_coupling import (
        COUPLING_ENV_VAR,
        RING_WIRE_FORMAT,
        RING_WIRE_FORMAT_ENV_VAR,
        RING_WIRE_FORMAT_WIDE,
        assistant_wire_is_wide,
        read_declared_ring_wire_format,
        resolve_ring_wire_format,
    )

    try:
        try:
            text = Path(env_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            # NO FILE is a declaration of nothing on both halves — the
            # ``EnvironmentFile=-`` state `persisted_coupling_feeds_ring` reads
            # as the ring, with the format falling back to the chain below. A
            # file that EXISTS but cannot be read is different and is NOT caught
            # here: it falls to the narrow `except` below.
            text = ""
        raw_format = read_value(text, RING_WIRE_FORMAT_ENV_VAR)
        wire_format = (
            resolve_ring_wire_format(raw_format)
            if raw_format is not None
            else read_declared_ring_wire_format()
        )
        wide = assistant_wire_is_wide(
            wire_format=wire_format,
            # The RAW token, not `read_persisted_coupling`'s resolved answer:
            # the transport half turns on the REFUSED value, which "the ring or
            # nothing" cannot spell. `or ""` keeps this half AUTHORITATIVE —
            # `assistant_wire_is_wide` reads `None` as "not supplied" and would
            # fall back to the default fanin.env, discarding the caller's
            # env_path. A file naming no transport is the ring (ADR-0100).
            coupling=read_value(text, COUPLING_ENV_VAR) or "",
        )
    except (OSError, ValueError):
        # An unreadable/typo'd declaration is fan-in's fault to report (it parks
        # at exit 78). Resolving narrow here matches what jasper-voice resolves
        # in the same situation (`jasper.audio_io.tts_wire_is_wide` catches the
        # same two and returns False), so the comparison stays honest.
        return RING_WIRE_FORMAT
    return RING_WIRE_FORMAT_WIDE if wide else RING_WIRE_FORMAT


# How long a blocking START of jasper-camilla may take.
#
# jasper-camilla.service is Type=simple, but it declares Requires= AND After=
# jasper-audio-hardware-reconcile.service, a Type=oneshot whose RemainAfterExit
# is unset. That reconciler is therefore inactive between runs and RE-RUNS IN
# FULL on every camilla start, with PID 1 holding camilla's start job until the
# oneshot reports terminal. On a Pi Zero 2 W a camilla restart measures ~30 s,
# of which the re-queued reconciler is ~26 s; on a Pi 5, ~4 s.
#
# ALL THREE PULLED DEPENDENCIES ARE TERMS, not just the oneshot: fan-in and
# outputd can be inactive-or-activating when this start runs — the
# ``camilla_pause_failed`` branch of :func:`_restart_fanin_coordinated` starts
# camilla with no fan-in restart having run, the ordinary resume runs even when
# ``do_restart()`` returned not-ok, and :func:`reconcile_auto` runs the spine's
# own 8 s-bounded restarts first. So the dependency term is the CRITICAL PATH
# through what camilla pulls, not the largest of the three:
# jasper-audio-hardware-reconcile declares ``Before=jasper-outputd.service``, so
# those two run in series while fan-in runs alongside them.
#
#     hw-reconcile 50 -> outputd 95   = 145   (serialised by that Before=)
#     fan-in 95                       =  95   (unordered w.r.t. both)
#     critical path                   = 145
#
# Declared ceilings set the value. Pinned to the shipped units — including that
# ordering edge — by tests/test_fanin_coupling_reconcile.py, so adding an edge
# that lengthens the path fails rather than silently under-bounding this call.
_CAMILLA_REQUEUED_RECONCILE_START_SEC = 50.0  # its declared TimeoutStartSec
# jasper-fanin and jasper-outputd each declare no TimeoutStartSec= override, so
# each takes the manager default, plus its RestartSec when in restart backoff.
_SYSTEMD_DEFAULT_TIMEOUT_START_SEC = 90.0
_NOTIFY_DEP_RESTART_BACKOFF_SEC = 5.0
_CAMILLA_NOTIFY_DEP_START_SEC = (
    _SYSTEMD_DEFAULT_TIMEOUT_START_SEC + _NOTIFY_DEP_RESTART_BACKOFF_SEC
)
_CAMILLA_DEPENDENCY_CRITICAL_PATH_SEC = max(
    # hw-reconcile -> outputd, serialised by that Before= edge
    _CAMILLA_REQUEUED_RECONCILE_START_SEC + _CAMILLA_NOTIFY_DEP_START_SEC,
    _CAMILLA_NOTIFY_DEP_START_SEC,  # fan-in, in parallel with both
)
# The manager's DefaultTimeoutStartSec: jasper-camilla.service declares no
# TimeoutStartSec= override, so this is the ceiling PID 1 applies to its start.
# A mirror of a MANAGER default cannot be pinned to a unit file, so it drifts
# silently if DefaultTimeoutStartSec is ever changed — ledgered, not guarded.
_CAMILLA_OWN_START_SEC = _SYSTEMD_DEFAULT_TIMEOUT_START_SEC
_DAEMON_OP_CLIENT_MARGIN_SEC = 1.0
_CAMILLA_START_TIMEOUT_SEC = (
    _CAMILLA_DEPENDENCY_CRITICAL_PATH_SEC
    + _CAMILLA_OWN_START_SEC
    + _DAEMON_OP_CLIENT_MARGIN_SEC
)


def _stop_camilla(reason: str) -> tuple[bool, str]:
    """Stop jasper-camilla through the broker. (ok, detail).

    Used to pause CamillaDSP with a clean SIGTERM BEFORE a coordinated fan-in
    restart so it exits cleanly instead of hitting the RLIMIT_RTTIME SIGKILL its
    ring-ioplug capture reader triggers when fan-in's writer detaches (see
    :func:`_restart_fanin_coordinated`). ``jasper-camilla.service`` is already a
    broker ``MANAGED_UNITS`` member (polkit-granted for ``manage-units``, which
    covers stop/start) — no new grant is needed.

    The 8 s bound is deliberately NOT the start bound's derivation: a stop does
    not pull ``Requires=``, so the critical-path dependency term that dominates
    :data:`_CAMILLA_START_TIMEOUT_SEC` cannot apply. What is left is camilla's
    own stop, measured at 23-30 ms.

    RESIDUAL, stated: camilla's legal stop ceiling is the manager default (90 s),
    so a stop that genuinely took longer than 8 s is misreported here.
    """
    return _restart_unit(CAMILLA_UNIT, verb="stop", reason=reason, timeout=8.0)


def _start_camilla(reason: str) -> tuple[bool, str]:
    """Start jasper-camilla through the broker after fan-in is back up. (ok, detail).

    Mirrors the fan-in -> camilla order ``jasper-camilla-recover`` uses: fan-in's
    ring/pipe writer must be re-attached before CamillaDSP re-opens its capture,
    so this runs AFTER the ``Type=notify`` fan-in restart has returned.

    Bounded by :data:`_CAMILLA_START_TIMEOUT_SEC`, which carries the re-queued
    hardware-reconciler oneshot camilla ``Requires=``; see that constant.
    """
    return _restart_unit(
        CAMILLA_UNIT, verb="start", reason=reason,
        timeout=_CAMILLA_START_TIMEOUT_SEC,
    )


def _restart_outputd(reason: str) -> tuple[bool, str]:
    """Restart jasper-outputd through the broker. (ok, detail).

    Same residual as :func:`_restart_fanin`: legally 95 s, bounded at 8 s
    against measured 0.4-1.0 s restarts, and budgeted as a camilla start
    dependency for the same reason.
    """
    return _restart_unit(OUTPUTD_UNIT, reason=reason, timeout=8.0)


# How long a blocking start of the audio-hardware reconciler may take. The two
# callers have different stakes, so they pass different bounds.
#
# The ENDPOINT-CONVERGENCE kick (``jasper.fanin.converge``) keeps 15 s — the
# same bound the topology save/reset/repin wizard surfaces use
# (``jasper.output_topology_runtime.trigger_reconcile``) — because a timeout
# there costs only a delayed marker re-derivation, which the next
# udev/boot/deploy event converges anyway.
#
# The CONTENT-FORMAT converge gets 60 s, roughly four times a full reconciler
# pass on a Pi Zero 2 W, because a timeout there refuses the whole convergence
# (see :func:`_converge_ring`). The budget it spends is the caller unit's — the
# ``deploy/systemd/jasper-fanin-coupling-auto.service`` ceiling, which is
# :data:`COUPLING_AUTO_TIMEOUT_START_SEC` below and carries this converge as an
# enumerated term.
_HARDWARE_RECONCILE_TIMEOUT_SEC = 15.0
_KICK_ACCEPT_TIMEOUT_SEC = 5.0  # `--no-block` returns in ms; bounds the accept.
_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC = 60.0


# --- The outer ceiling one `--auto` pass needs -------------------------------
#
# ``jasper-fanin-coupling-auto.service`` is the Type=oneshot that runs
# :func:`reconcile_auto`. Its TimeoutStartSec has to outlast the pass's own
# blocking work, and two multipliers apply to EVERY daemon op here:
#
#   * a start-consuming verb on a crash-budget unit is preceded by a blocking
#     best-effort ``reset-failed`` (see :func:`_restart_unit`), and
#   * ``restart_broker.manage_units`` waits ``timeout + 5 s`` on the socket, then
#     — as root, which this unit is — retries the SAME call through
#     ``_direct_systemctl`` when the socket raises ``BrokerUnavailable`` (a
#     socket timeout is converted to exactly that). So one op can legally cost
#     twice its timeout plus the socket margin.
#
# THE CEILING IS SIZED FOR A LIVE BROKER, deliberately: the doubling fires only
# on ``BrokerUnavailable``, an independently loud already-degraded mode, and
# stretching the ceiling to cover it would hide every real wedge for that
# length. The broker-dead figure is computed once, disclosed as
# :data:`COUPLING_AUTO_BROKER_DEAD_WORST_SEC`, and never used in the arithmetic.
_BROKER_SOCKET_MARGIN_SEC = 5.0  # restart_broker._CLIENT_SOCKET_MARGIN_SEC


def _daemon_op_ceiling_sec(
    timeout: float, *, reset_failed: bool, broker_dead: bool = False
) -> float:
    """Worst legal wall time for one :func:`_restart_unit` call at ``timeout``.

    ``broker_dead`` adds the root direct-systemctl retry each broker call makes
    after ``BrokerUnavailable``. That is the disclosed residual, never an input
    to the shipped ceiling.
    """
    attempts = 2 if broker_dead else 1
    preamble = (
        attempts * _RESET_FAILED_TIMEOUT_SEC + _BROKER_SOCKET_MARGIN_SEC
        if reset_failed
        else 0.0
    )
    return preamble + attempts * timeout + _BROKER_SOCKET_MARGIN_SEC


# Entry-lock wait (10 s), convergence gate/graph/applied-record reads (4 s), and
# the anchor-branch re-emit (25 s: staged-anchor lock 15 s + camilladsp --check
# 10 s) — the three in-process figures jasper-fanin-coupling-auto.service's own
# tally carries, which no broker multiplier touches.
_COUPLING_AUTO_NON_DAEMON_WORK_SEC = 39.0


def _coupling_auto_pass_ceiling_sec(*, broker_dead: bool) -> float:
    """One ``--auto`` pass, enumerated along its worst reachable path.

    The coupling half and the USB-combo half are ADDITIVE, in that order:
    :func:`reconcile_auto` delegates the convergence to ``reconcile_coupling``
    and only THEN runs the coordinated restart, so fan-in is restarted TWICE
    across the worst path — once by the convergence spine, once by the combo
    coordination.
    """

    def op(timeout: float, reset_failed: bool) -> float:
        return _daemon_op_ceiling_sec(
            timeout, reset_failed=reset_failed, broker_dead=broker_dead
        )

    spine = (
        op(_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC, False)
        + op(8.0, True)  # outputd restart
        + op(8.0, True)  # fan-in restart
    )
    combo = (
        op(8.0, False)  # camilla stop: not a start verb, so no reset preamble
        + op(8.0, True)  # fan-in restart
        + op(_CAMILLA_START_TIMEOUT_SEC, True)  # the camilla resume
    )
    return (
        _COUPLING_AUTO_NON_DAEMON_WORK_SEC
        + op(_HARDWARE_RECONCILE_TIMEOUT_SEC, False)  # endpoint-convergence kick
        + spine
        # An assistant-width TRANSITION inside the pass try-restarts voice. Not
        # a crash-budget unit, so no reset preamble.
        + op(8.0, False)
        + combo
        + op(_KICK_ACCEPT_TIMEOUT_SEC, False)  # grouping re-bake kick
    )


COUPLING_AUTO_ENUMERATED_WORST_SEC = _coupling_auto_pass_ceiling_sec(broker_dead=False)
# DISCLOSED RESIDUAL, deliberately not an input above: under a dead broker AND
# maximally slow hardware, a pass can reach this instead and will be killed at
# the ceiling mid-heal. It retries on the next trigger, and the fail-closed
# usbsink rollback can recur in that window.
COUPLING_AUTO_BROKER_DEAD_WORST_SEC = _coupling_auto_pass_ceiling_sec(broker_dead=True)
# THE UNQUANTIFIED TERM, stated rather than hidden: a pass also runs
# :func:`_reconcile_camilla`, and that calls
# ``asyncio.run(reconcile_current_dsp())`` with NO timeout of its own. No finite
# ceiling covers an unbounded term, so this headroom is a courtesy; bounding that
# call is tracked on ledger issue #2802.
_COUPLING_AUTO_CEILING_HEADROOM_SEC = 270.0
COUPLING_AUTO_TIMEOUT_START_SEC = (
    COUPLING_AUTO_ENUMERATED_WORST_SEC + _COUPLING_AUTO_CEILING_HEADROOM_SEC
)


def _start_audio_hardware_reconcile(
    reason: str, *, timeout: float = _HARDWARE_RECONCILE_TIMEOUT_SEC
) -> tuple[bool, str]:
    """Start the audio-hardware reconciler oneshot through the broker. (ok, detail).

    Blocking, so the caller returns with the env actions actually re-emitted, not
    just requested. ``start`` of this unit is broker-permitted for non-root
    clients (``START_ONLY_UNITS``) and falls back to direct systemctl for a
    broker-less root shell — the same reach every other daemon op here has.

    ``timeout`` is per-caller because the two callers have different stakes; see
    :data:`_HARDWARE_RECONCILE_TIMEOUT_SEC`.
    """
    return _restart_unit(
        AUDIO_HARDWARE_RECONCILE_UNIT, verb="start", reason=reason, timeout=timeout
    )


def _reconcile_camilla(
    *,
    reason: str,
    force: bool = True,
) -> tuple[bool, str]:
    """Re-emit + load the CamillaDSP ring config. (ok, detail).

    A pass whose env moved forces a full reconcile because the graph is the
    change.  A no-op pass passes ``force=False`` so unchanged source reconciles
    take the runtime's YAML-equality fast path while still repairing a genuinely
    drifted loaded config.  reconcile_current_dsp validates with ``camilladsp
    --check`` before loading and fail-closes on an invalid config, so a failure
    here leaves the previously-loaded config running.

    ONE ``skipped`` IS ACCEPTED, on direct proof only. A mid-commission roleful
    box boots from the all-muted staged startup anchor, which the carrier
    refuses to host EQ on (:data:`CARRIER_TRANSIENT_ACTIVE_REFUSAL`).
    :func:`ring_endpoint_anchor_converged` proves from the artifacts on disk
    that the graph IS that anchor and IS already at the ring endpoint at the
    box's wire; only then is the step converged, with its own detail so the
    outcome is never confused with a re-emit. Every other ``skipped`` — a
    different refusal code, a commissioning load, an anchor that is not coherent
    — fails.

    NO acceptance here survives a payload that came over the STATEFILE
    (``transport=statefile``). That payload's ``current_config_path`` is
    the durable pointer rather than the daemon's answer, and this rung's contract
    is "re-emit AND LOAD" — with CamillaDSP down nothing was loaded. One guard
    ahead of all three branches, so the rule cannot be true of one acceptance and
    forgotten by the next.
    """
    import asyncio

    from jasper.sound.runtime import reconcile_current_dsp

    try:
        payload = asyncio.run(reconcile_current_dsp(force=force))
    except Exception as e:  # noqa: BLE001 - report, never raise out of the reconcile
        return False, f"camilla reconcile raised: {e}"
    status = payload.get("status")
    # ``transport`` names which reader answered "which graph is loaded"; ahead of
    # every acceptance because a commissioned box and a flat box both answer
    # ``reconciled``, so guarding only the anchor branch would let those two
    # report success about a dead daemon.
    if payload.get("transport") == "statefile":
        return False, f"camilla down: reconcile converged over the statefile ({status})"
    if status in ("reconciled", "unchanged"):
        return True, str(status)
    # A "skipped" reconcile means the ring config was NOT loaded, so it fails —
    # with the one proven exception below.
    refusal = str(payload.get("reason") or "")
    if status == "skipped" and refusal == CARRIER_TRANSIENT_ACTIVE_REFUSAL:
        # Keyed on the ONE refusal this acceptance is about, not on "skipped"
        # generally: a refusal code added later is a shape nobody proved
        # convergent. ``current_config_path`` is the DAEMON's own answer
        # (``cam.get_config_file_path`` over CamillaDSP's websocket), not the
        # statefile's — the statefile has several other writers and can name a
        # graph the running daemon does not hold, which would let one moved
        # mid-arm report ``converged_anchor`` while CamillaDSP still writes
        # the lane the ring replaced — the box goes silent.
        converged, anchor_detail = ring_endpoint_anchor_converged(
            loaded_config_path=payload.get("current_config_path")
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result=(
                "camilla_converged_anchor"
                if converged
                else "camilla_anchor_not_converged"
            ),
            reason=reason,
            refusal=refusal,
            detail=anchor_detail,
            level=logging.INFO if converged else logging.WARNING,
        )
        if converged:
            return True, CAMILLA_ANCHOR_CONVERGED_DETAIL
        return False, f"{refusal}: {anchor_detail}"
    return False, str(payload.get("reason") or status or "unknown")


@dataclass(frozen=True)
class _CoordinatedFaninRestart:
    """Outcome of a CamillaDSP-coordinated fan-in restart.

    ``fanin_restarted`` is whether fan-in actually restarted;
    ``camilla_stopped`` / ``camilla_started`` record the pause/resume outcomes.
    ``ok`` is True only when every step the chosen path needed succeeded.
    """

    ok: bool
    fanin_restarted: bool
    camilla_stopped: bool
    camilla_started: bool
    detail: str = ""


def _restart_fanin_coordinated(
    do_restart: DaemonOp,
    do_stop_camilla: DaemonOp,
    do_start_camilla: DaemonOp,
    *,
    reason: str,
    phase: str,
) -> _CoordinatedFaninRestart:
    """Restart fan-in without collaterally SIGKILLing CamillaDSP.

    THE HAZARD this guards: while the fan-in-written ``shm_ring`` coupling is
    live, CamillaDSP captures the transport via the ``jts_ring_capture`` ioplug,
    and a bare fan-in *process* restart detaches the ring WRITER. An unpaced
    capture reader busy-spins on that, and camilladsp (``SCHED_FIFO``,
    ``LimitRTTIME=200000`` us in ``jasper-camilla.service``) takes the kernel's
    ``RLIMIT_RTTIME`` hard SIGKILL ~213 ms later -> ``Restart=always``
    start-limit -> ``OnFailure=jasper-camilla-recover`` -> a core-graph bounce.

    So this pauses CamillaDSP with a clean SIGTERM FIRST, restarts fan-in, waits
    for it to come back (the ``Type=notify`` blocking broker restart returns only
    after fan-in re-attaches its ring writer + ``sd_notify`` READY=1), then
    resumes CamillaDSP — the fan-in -> camilla order
    ``deploy/bin/jasper-camilla-recover`` uses.

    FAILURE HONESTY: if CamillaDSP cannot be STOPPED it may still be running on
    the ring, so we do NOT restart fan-in and instead ensure camilla is running
    (a ``start`` is a no-op if it never stopped) and abort, ``ok=False``. If the
    fan-in restart fails AFTER camilla was stopped, we STILL start camilla back
    — never leave the DSP stopped forever. Either way
    ``OnFailure=jasper-camilla-recover`` stays the backstop for a resume that
    also fails; nothing here disables it.

    (Stopping camilla is safe for jasper-outputd even though camilla is outputd's
    Ring B writer: outputd's reader is DAC-clocked -- an absent writer yields paced
    silence, not a busy-spin -- so only the camilla side needs coordination.)

    SCOPE: DELIBERATE Python-side fan-in restarts only — today just
    :func:`reconcile_auto`'s USB-combo restart. RTTIME safety no longer rests on
    this coordination: the ring-ioplug capture reader now paces itself
    (``c/jts-ring-ioplug/``), so an UNCOORDINATED fan-in death (crash, OOM-kill,
    an external ``systemctl restart jasper-fanin``) degrades to <=2 s of paced
    silence while camilla blocks on the reader's timerfd. The coordination is
    kept for the gap-free UX.
    """
    stop_ok, stop_detail = do_stop_camilla()
    if not stop_ok:
        # Camilla could not be paused -> it may still be on the ring, so do NOT
        # restart fan-in. Ensure camilla is running and abort.
        start_ok, start_detail = do_start_camilla()
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="camilla_pause_failed",
            reason=reason,
            phase=phase,
            detail=stop_detail or None,
            camilla_started=start_ok,
            level=logging.WARNING,
        )
        return _CoordinatedFaninRestart(
            ok=False,
            fanin_restarted=False,
            camilla_stopped=False,
            camilla_started=start_ok,
            detail=(
                f"camilla pause failed ({stop_detail}); aborted fan-in restart to "
                "avoid an RTTIME-SIGKILL of a running CamillaDSP"
                + ("" if start_ok else f"; camilla start-back failed ({start_detail})")
            ),
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="camilla_paused_for_fanin_restart",
        reason=reason,
        phase=phase,
    )
    fan_ok, fan_detail = do_restart()
    # ALWAYS resume camilla, even if the fan-in restart failed — never leave the
    # DSP stopped forever (OnFailure/recover backstops a failed resume).
    start_ok, start_detail = do_start_camilla()
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="camilla_resumed_after_fanin_restart"
        if start_ok
        else "camilla_resume_failed",
        reason=reason,
        phase=phase,
        fanin_restarted=fan_ok,
        detail=start_detail or None,
        level=logging.INFO if start_ok else logging.WARNING,
    )
    detail = "; ".join(
        d
        for d in (
            "" if fan_ok else f"fan-in restart failed ({fan_detail})",
            "" if start_ok else f"camilla resume failed ({start_detail})",
        )
        if d
    )
    return _CoordinatedFaninRestart(
        ok=fan_ok and start_ok,
        fanin_restarted=fan_ok,
        camilla_stopped=True,
        camilla_started=start_ok,
        detail=detail,
    )


def reconcile_coupling(
    *,
    reason: str,
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    reconcile_camilla: "DaemonOp | None" = None,
    kick_hardware_reconcile: "DaemonOp | None" = None,
    restart_voice: "DaemonOp | None" = None,
) -> CouplingResult:
    """Converge the box onto the ring, then bound the two transients it can open.

    The convergence itself is :func:`_converge_ring`; this wrapper adds the two
    things that must happen AROUND it rather than inside.

    THE BONDED LEADER'S BAKED CAPTURE. A bonded ACTIVE leader's camilla#1 carries
    the coupling's capture device, baked at BOND time; nothing else re-derives it
    when this pass moves the env, and the two units are unordered — so a pass
    that moved the coupling kicks the grouping re-bake.

    THE ASSISTANT WIRE. The box's assistant IPC width is ``wire_format ==
    S32_LE`` AND a coupling fan-in does not refuse
    (:func:`jasper.fanin_coupling.assistant_wire_is_wide`), and ``jasper-voice``
    resolves it ONCE at start — it is not restarted by the ordered audio-graph
    bounce. So a pass that moves the coupling can leave voice speaking the old
    width into a fan-in that now expects the other one. That is converted
    losslessly and logged (``event=fanin.tts_wire_width_mismatch``), but without
    this it is a STANDING disagreement rather than a transient. Comparing the
    resolved width across the pass and issuing one ``try-restart`` makes the
    window the length of a convergence.

    Both reads are file-fresh and go through the same rule voice uses. The
    restart is best-effort: a failure is logged and never changes the pass's
    verdict, because the coupling IS converged either way.
    """
    # REMOVE once no box carries a refused coupling token: that is the only
    # `before` state whose width this pass can move.
    before = _assistant_width_token(env_path)
    result = _converge_ring(
        reason=reason,
        env_path=env_path,
        outputd_env_path=outputd_env_path,
        apply=apply,
        restart_fanin=restart_fanin,
        restart_outputd=restart_outputd,
        reconcile_camilla=reconcile_camilla,
        kick_hardware_reconcile=kick_hardware_reconcile,
    )
    if not apply:
        # Staging/migration writes the env but runs no daemon ops; restarting
        # voice here would be the one daemon op an apply=False pass performed.
        return result
    if result.changed and result.ok:
        # FIRE-AND-FORGET: the re-bake outruns any wait this side could justify
        # (TimeoutStartSec=6346) and killing the client would not cancel the
        # queued job. `ok` is "systemd ACCEPTED the job" — logged because a
        # drifted unit name would otherwise make this a SILENT no-op.
        kicked, kick_detail = _restart_unit(
            GROUPING_RECONCILE_UNIT, verb="start", reason=reason,
            no_block=True, timeout=_KICK_ACCEPT_TIMEOUT_SEC,
        )
        log_event(
            logger, "fanin.coupling_reconcile", unit=GROUPING_RECONCILE_UNIT,
            result="grouping_rebake_kicked" if kicked else "grouping_rebake_kick_failed",
            reason=reason, detail=kick_detail or None,
            level=logging.INFO if kicked else logging.WARNING,
        )
    after = _assistant_width_token(env_path)
    if after == before:
        return result
    do_restart_voice = restart_voice or (lambda: _try_restart_voice(reason=reason))
    ok, detail = do_restart_voice()
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result=(
            "assistant_width_voice_restarted"
            if ok
            else "assistant_width_voice_restart_failed"
        ),
        reason=reason,
        assistant_width_before=before,
        assistant_width_after=after,
        detail=detail or None,
        level=logging.INFO if ok else logging.WARNING,
    )
    return result


def _converge_ring(
    *,
    reason: str,
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    reconcile_camilla: "DaemonOp | None" = None,
    kick_hardware_reconcile: "DaemonOp | None" = None,
) -> CouplingResult:
    """Write the ring state, heal its geometry, and converge the daemons in order.

    The write comes first (single source of truth for the daemons' next start),
    then the two GEOMETRY heals — a shear-prone stale ``JASPER_FANIN_RING_SLOTS``
    and a geometry-mismatched on-disk ring file — then, only if something
    actually moved, the ordered spine:

    1. ``jasper-audio-hardware-reconcile``, the single writer of
       ``JASPER_OUTPUTD_CONTENT_FORMAT``, so outputd is restarted against the
       ring's own wire rather than a stale one. This step FAILS the pass: without
       it outputd comes up asking for a width CamillaDSP's ioplug does not
       attach, which is a hard ``attach_fatal``.
    2. jasper-outputd — the post-DSP ring's READER — first,
    3. jasper-fanin — the Ring A WRITER — second,
    4. CamillaDSP last, because it attaches to both.

    A pass whose env and geometry are already coherent skips the spine entirely
    and re-confirms CamillaDSP only, so a ``/sources/`` toggle does not bounce
    the shared fan-in daemon.

    NOTHING IS ROLLED BACK: a failing step returns ``ok=False`` with the reason
    and the box parks under its own name. ``apply=False`` writes the env only
    (staging/migration). The daemon ops are injectable for tests and default to
    the real broker + reconcile_current_dsp.
    """
    do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
    do_restart_outputd = restart_outputd or (lambda: _restart_outputd(reason=reason))
    do_converge_content_format = kick_hardware_reconcile or (
        lambda: _start_audio_hardware_reconcile(
            reason=reason, timeout=_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC
        )
    )

    def do_reconcile(*, force: bool) -> tuple[bool, str]:
        if reconcile_camilla is not None:
            return reconcile_camilla()
        return _reconcile_camilla(reason=reason, force=force)

    fanin_snapshot = _read_snapshot(env_path)
    outputd_snapshot = _read_snapshot(outputd_env_path)

    fanin_new_text, fanin_changed = _apply_action(
        fanin_snapshot.text,
        RuntimeEnvAction("set", COUPLING_ENV_VAR, COUPLING_SHM_RING),
    )
    outputd_new_text, outputd_changed = _apply_actions(
        outputd_snapshot.text, _outputd_actions(outputd_snapshot.text)
    )
    # Did this pass CONVERGE the ring-path/marker pair? Compared as RESOLVED
    # values so first-writing an absent key (which resolves to the same default)
    # is not mistaken for a heal. The pair is crossed for a bounded window by
    # construction (its two halves have two writers), so logging the heal is
    # what keeps that window observable.
    ring_path_before = resolve_outputd_ring_path(
        read_value(outputd_snapshot.text, OUTPUTD_RING_PATH_ENV_VAR)
    )
    ring_path_converged = (
        _outputd_ring_path_for(outputd_snapshot.text) != ring_path_before
    )
    changed = fanin_changed or outputd_changed

    # A write failure aborts BEFORE any daemon op so we never bounce a daemon
    # into a value the file doesn't carry.
    if changed:
        try:
            if fanin_changed:
                _write_env_text(fanin_snapshot.path, fanin_new_text)
            if outputd_changed:
                _write_env_text(outputd_snapshot.path, outputd_new_text)
        except OSError as e:
            _restore_snapshot(fanin_snapshot)
            _restore_snapshot(outputd_snapshot)
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="write_failed",
                reason=reason,
                error=e,
                level=logging.ERROR,
            )
            return CouplingResult(ok=False, changed=False, detail=str(e))

    if ring_path_converged:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="ring_path_converged",
            reason=reason,
            was=ring_path_before,
            now=_outputd_ring_path_for(outputd_new_text),
        )

    _sync_process_env_for_emit(outputd_new_text)

    if not apply:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="written",
            changed=changed,
            reason=reason,
        )
        return CouplingResult(ok=True, changed=changed)

    # GEOMETRY HEALS, every pass — not only when the coupling-flip WRITE moves.
    # A box already on the ring with a stale slot count or a stale on-disk ring
    # must still be healed. Both are write-on-change, so a coherent box pays a
    # few small reads and still takes the no-bounce path below.
    fanin_snapshot, slots_healed = _migrate_stale_fanin_ring_slots(
        fanin_snapshot, reason
    )
    files_cleared = _delete_stale_ring_files(reason, fanin_snapshot.text)

    if not (changed or slots_healed or files_cleared):
        # Already coherent: re-confirm camilla only (self-heal a drifted loaded
        # config) — no fan-in bounce on a no-op tick.
        ok, detail = do_reconcile(force=False)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="confirmed" if ok else "confirm_failed",
            reason=reason,
            detail=detail or None,
            level=logging.INFO if ok else logging.ERROR,
        )
        return CouplingResult(
            ok=ok,
            changed=False,
            reconciled_camilla=ok,
            detail="" if ok else detail,
        )

    kick_ok, kick_detail = do_converge_content_format()
    if not kick_ok:
        detail = (
            "could not converge JASPER_OUTPUTD_CONTENT_FORMAT to the ring wire "
            f"({kick_detail}); restarting jasper-outputd on a stale width would "
            "fail CamillaDSP's ring attach — run `sudo systemctl start "
            "jasper-audio-hardware-reconcile`, then re-run this reconcile"
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="ring_content_format_converge_failed",
            reason=reason,
            detail=detail,
            level=logging.ERROR,
        )
        return CouplingResult(ok=False, changed=changed, detail=detail)
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="ring_content_format_converged",
        reason=reason,
        detail=kick_detail or None,
    )

    # THE ORDERED SPINE. Each step is a precondition of the next — outputd must
    # be reading the post-DSP ring before fan-in re-creates Ring A, and both ends
    # must be up before CamillaDSP attaches to them — so a failure short-circuits
    # rather than bouncing the rest of the graph into a half-converged one.
    out_ok, out_detail = do_restart_outputd()
    fan_ok, fan_detail = (
        do_restart() if out_ok else (False, "skipped: outputd did not come up")
    )
    cam_ok, cam_detail = (
        do_reconcile(force=True)
        if fan_ok
        else (False, "skipped: fan-in did not come up")
    )
    ok = out_ok and fan_ok and cam_ok
    detail = "; ".join(
        d
        for d in (
            "" if out_ok else f"outputd restart failed ({out_detail})",
            "" if fan_ok else f"fan-in restart failed ({fan_detail})",
            "" if cam_ok else f"camilla reconcile failed ({cam_detail})",
        )
        if d
    )
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="ring_converged" if ok else "ring_converge_failed",
        reason=reason,
        detail=(detail or cam_detail) or None,
        level=logging.INFO if ok else logging.ERROR,
    )
    return CouplingResult(
        ok=ok,
        changed=changed,
        restarted_fanin=fan_ok,
        restarted_outputd=out_ok,
        reconciled_camilla=cam_ok,
        # The camilla step has two success shapes, and the operator's stdout line
        # prints ``detail`` only when it is non-empty. An ORDINARY re-emit stays
        # silent there; the anchor-converged acceptance says so, because only one
        # of the two wrote anything.
        detail=(
            detail
            or (
                CAMILLA_ANCHOR_CONVERGED_DETAIL
                if cam_detail == CAMILLA_ANCHOR_CONVERGED_DETAIL
                else ""
            )
        ),
    )


@dataclass(frozen=True)
class AutoResult:
    """Outcome of one unattended (``--auto``) pass.

    ``combo_armed`` is whether the USB combo resolved on, ``usb_combo_changed``
    whether the fan-in combo keys moved, ``coupling_result`` the delegated
    :class:`CouplingResult`, and ``restarted_fanin_for_combo`` True when a
    combo-only change forced an extra fan-in restart. ``ok`` reflects the
    delegated convergence plus that restart.
    """

    ok: bool
    gadget_present: bool
    usb_combo_changed: bool
    reason: str
    combo_armed: bool = False
    usb_intent_enabled: bool = False
    usb_latency_mode: str = "low"
    coupling_result: "CouplingResult | None" = None
    restarted_fanin_for_combo: bool = False
    detail: str = ""


def reconcile_auto(
    *,
    reason: str = "auto",
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    gadget_present: bool | None = None,
    usb_intent_enabled: bool | None = None,
    usb_latency_mode: str | None = None,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    stop_camilla: "DaemonOp | None" = None,
    start_camilla: "DaemonOp | None" = None,
    reconcile_camilla: "DaemonOp | None" = None,
    kick_hardware_reconcile: "DaemonOp | None" = None,
) -> AutoResult:
    """The unattended pass: USB combo from household intent, then the ring.

    Runs on deploy (install.sh) and boot (the reconciler's ``--auto`` CLI). Two
    independent halves, in this order:

    1. Resolve the USB combo from gadget presence AND the household's USB-audio
       intent plus current local-source role permission, and write the fan-in
       feature keys and preset floor into fanin.env (explicit values, never
       unset, defeating jasper.env precedence). Idempotent — a second pass with
       the same inputs writes nothing.
    2. Delegate the ring convergence to :func:`reconcile_coupling`. The ring is
       the only transport (ADR-0100), and a box it cannot serve parks under its
       own name (:mod:`jasper.control.transport_park`) rather than resolving a
       second route. A combo-only change that took the no-bounce path issues one
       extra CamillaDSP-coordinated fan-in restart
       (:func:`_restart_fanin_coordinated`), so it cannot RTTIME-SIGKILL camilla.

    If canonical USB intent is malformed or unreadable, the pass narrows itself
    to the safety action: resolve effective USB intent False, run the same
    explicit-off combo write + ordered fan-in restart, emit
    ``result=auto_usb_intent_fail_closed``, and return ``ok=False``. Derived or
    unit state must never authorize capture when canonical intent cannot be
    proved valid.

    Every ``DaemonOp`` argument plus ``gadget_present`` / ``usb_intent_enabled``
    is injectable for tests; ``gadget_present=None`` reads the resolved USB
    hardware capability and ``usb_intent_enabled=None`` reads canonical source
    intent plus current local-source role permission.
    """
    fanin_snapshot = _read_snapshot(env_path)
    gadget = (
        read_usb_gadget_available() if gadget_present is None else gadget_present
    )
    usb_intent_failure = ""
    if usb_intent_enabled is None:
        try:
            usb_intent = usbsink_effectively_enabled()
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            # A previously armed fan-in process retains its DIRECT lane until
            # this owner writes + applies the explicit-off combo plan. Treat the
            # unreadable preference as effective False, complete the ordinary
            # ordered write below, and only then return failure.
            usb_intent = False
            usb_intent_failure = f"USB source intent invalid or unreadable: {exc}"[:500]
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_intent_invalid",
                reason=reason,
                usb_intent_enabled=False,
                detail=usb_intent_failure,
                level=logging.ERROR,
            )
    else:
        usb_intent = usb_intent_enabled
    usb_latency_failure = ""
    try:
        latency_mode = (
            read_usb_latency_mode()
            if usb_latency_mode is None
            else normalize_usb_latency_mode(usb_latency_mode)
        )
    except (OSError, UnicodeError, ValueError) as exc:
        latency_mode = "high"
        usb_latency_failure = (
            f"USB latency preference invalid or unreadable: {exc}"[:500]
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_latency_invalid",
            reason=reason,
            usb_latency_mode=latency_mode,
            detail=usb_latency_failure,
            level=logging.ERROR,
        )

    combo_armed = not usb_intent_failure and combo_is_armed(
        gadget_present=gadget, usb_intent_enabled=usb_intent
    )
    combo_actions = usb_combo_actions(armed=combo_armed, latency_mode=latency_mode)

    # Step 1 — fan-in combo keys (reconciler = single writer). Write only on change.
    fanin_after_combo, combo_changed = _apply_actions(
        fanin_snapshot.text, combo_actions
    )
    if combo_changed:
        try:
            _write_env_text(fanin_snapshot.path, fanin_after_combo)
        except OSError as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_combo_write_failed",
                reason=reason,
                gadget_present=gadget,
                error=e,
                level=logging.ERROR,
            )
            return AutoResult(
                ok=False,
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                combo_armed=combo_armed,
                usb_latency_mode=latency_mode,
                usb_combo_changed=False,
                reason="USB combo write failed",
                detail="; ".join(part for part in (usb_intent_failure, str(e)) if part),
            )
        # Keep the live env coherent for the ring convergence's own re-read.
        for a in combo_actions:
            if a.action == "set":
                os.environ[a.key] = a.value
            else:
                os.environ.pop(a.key, None)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_combo_written",
            reason=reason,
            gadget_present=gadget,
            usb_intent_enabled=usb_intent,
            combo_armed=combo_armed,
            usb_latency_mode=latency_mode,
            keys=",".join(a.key for a in combo_actions),
        )

    # Step 2 — the ring. The reconciler re-reads fanin.env fresh (it snapshots
    # inside), so the combo keys just written persist untouched.
    coupling_result = reconcile_coupling(
        reason=reason,
        env_path=env_path,
        outputd_env_path=outputd_env_path,
        apply=apply,
        restart_fanin=restart_fanin,
        restart_outputd=restart_outputd,
        reconcile_camilla=reconcile_camilla,
        kick_hardware_reconcile=kick_hardware_reconcile,
    )

    # If the fan-in combo changed but the ring convergence did NOT restart fan-in
    # (a combo-only change on an already-coherent box takes the no-bounce path),
    # the new combo is not live until fan-in restarts. Issue one —
    # CamillaDSP-coordinated so it cannot RTTIME-SIGKILL camilla off the ring.
    restarted_for_combo = False
    if apply and combo_changed and not coupling_result.restarted_fanin:
        do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
        do_stop_camilla = stop_camilla or (lambda: _stop_camilla(reason=reason))
        do_start_camilla = start_camilla or (lambda: _start_camilla(reason=reason))
        coord = _restart_fanin_coordinated(
            do_restart,
            do_stop_camilla,
            do_start_camilla,
            reason=reason,
            phase="auto_usb_combo",
        )
        restarted_for_combo = coord.fanin_restarted
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_combo_fanin_restarted"
            if coord.ok
            else "auto_usb_combo_fanin_restart_failed",
            reason=reason,
            detail=coord.detail or None,
            level=logging.INFO if coord.ok else logging.WARNING,
        )
        if not coord.ok:
            return AutoResult(
                ok=False,
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                combo_armed=combo_armed,
                usb_latency_mode=latency_mode,
                usb_combo_changed=combo_changed,
                reason="USB combo fan-in restart failed",
                coupling_result=coupling_result,
                restarted_fanin_for_combo=restarted_for_combo,
                detail="; ".join(
                    part
                    for part in (
                        usb_intent_failure,
                        usb_latency_failure,
                        coord.detail,
                    )
                    if part
                ),
            )

    ok = coupling_result.ok and not usb_intent_failure and not usb_latency_failure
    detail = "; ".join(
        part
        for part in (
            usb_intent_failure,
            usb_latency_failure,
            coupling_result.detail,
        )
        if part
    )
    if usb_intent_failure:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_intent_fail_closed",
            reason=reason,
            usb_intent_enabled=False,
            combo_armed=combo_armed,
            usb_combo_changed=combo_changed,
            restarted_fanin=(coupling_result.restarted_fanin or restarted_for_combo),
            detail=detail,
            level=logging.ERROR,
        )
    return AutoResult(
        ok=ok,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        combo_armed=combo_armed,
        usb_latency_mode=latency_mode,
        usb_combo_changed=combo_changed,
        reason=(
            "USB source intent invalid — combo failed closed"
            if usb_intent_failure
            else "USB combo resolved from canonical source intent"
        ),
        coupling_result=coupling_result,
        restarted_fanin_for_combo=restarted_for_combo,
        detail=detail,
    )


# The carrier's refusal reason for a roleful box's TRANSIENT boot graph — the
# startup anchor and the commissioning load (``jasper.sound.graph_carrier``'s
# ``_ActiveGraphCarrier.reemit`` raises ``CarrierCannotHostEq`` with exactly this
# code, as a bare literal). ``tests/test_ring_anchor_arm_acceptance.py`` pins the
# two spellings against the real exception, so a rename fails loudly instead of
# silently turning the acceptance below into dead code.
CARRIER_TRANSIENT_ACTIVE_REFUSAL = "eq_on_active_not_wired"

# The camilla step's detail when it converged on an anchor rather than by
# re-emitting. Distinct from "reconciled"/"unchanged" (a graph that was written)
# and from the refusal reason, so the journal AND the operator's stdout line say
# which of the three happened.
CAMILLA_ANCHOR_CONVERGED_DETAIL = "converged_anchor"


def ring_topology_ready_strict() -> tuple[bool, str]:
    """``ring_topology_ready`` fail-CLOSED on an unreadable topology.

    See the ``strict_unreadable`` note on :func:`ring_topology_ready`: a
    topology this pass cannot read cannot prove anything about the graph it
    would move.
    """
    return ring_topology_ready(strict_unreadable=True)


def ring_roleful_unattended_ready() -> tuple[bool, str]:
    """May an unattended pass MOVE a ROLEFUL box's graph? Fail-closed, two arms.

    It refuses by DEFAULT and admits exactly two proven graph shapes. Both arms
    are about the GRAPH's provenance — never about the box's topology SHAPE,
    which :func:`ring_topology_ready` owns one gate later:

    1. **A hardware-fingerprint-matched applied baseline** —
       :func:`~jasper.active_speaker.baseline_profile.applied_baseline_hardware_match`,
       the same predicate the emitter fails closed on. What a converging pass
       then moves is the graph a human already approved for THIS hardware, with
       driver values byte-preserved. A real DAC swap fails the fingerprint and
       lands in the default refusal.
    2. **The all-muted staged anchor** — the loaded graph IS this box's published
       anchor (:func:`_staged_anchor_identity`) AND every output it declares ends
       in a wired terminal mute (:func:`_anchor_is_all_muted`). It emits silence,
       so it cannot be a hearing event on any hardware.

    SCOPE, held deliberately narrow: arm 1 is the fingerprint compare ONLY.
    Applied-record DIVERGENCE (``applied_profile_displacement``) is another
    gate's question and is NOT asked here.

    Everything else refuses, fail-CLOSED: an unreadable topology, no applied
    record and no anchor, a stale fingerprint, an anchor that is not terminally
    muted. The refusal names the runnable arm so a refused box has a way out.

    A CORRUPT applied record is caught HERE rather than left to the caller.
    ``load_applied_baseline_profile_state`` returns ``None`` for the shapes its
    own loader catches, but a non-UTF-8 byte — the SD-card / power-cut
    truncation — raises ``UnicodeDecodeError`` straight past it, and the
    caller's ``except`` would turn that into the one refusal in this gate
    carrying no remediation. The SHARED loader is deliberately not widened; it
    has other callers whose contracts are theirs.
    """
    from jasper.active_speaker.baseline_profile import (
        applied_baseline_hardware_match,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
        contract = classify_output_contract(topology)
    except (OutputTopologyError, OSError, ValueError) as exc:
        return False, (
            f"topology unreadable ({exc}); an unattended pass cannot prove this "
            "box is not roleful, so it leaves the graph alone (fail-closed)"
        )
    if not contract.requires_roleful_graph:
        return True, "topology is not roleful"

    # Arm 1 — an applied baseline that still matches this hardware.
    stale_detail = ""
    try:
        applied = load_applied_baseline_profile_state()
    except (OSError, ValueError) as exc:
        applied = None
        stale_detail = (
            f"the applied active-speaker record could not be read "
            f"({type(exc).__name__})"
        )
    if applied is not None:
        _snapshot, hardware_issues = applied_baseline_hardware_match(
            topology, applied_profile=applied
        )
        if not hardware_issues:
            return True, (
                "roleful, and this box's applied active-speaker profile still "
                "matches the hardware (topology identity and fingerprint both "
                "current), so a converging pass moves the graph a human already "
                "approved for these drivers"
            )
        stale_detail = "; ".join(
            str(issue.get("code", "")) for issue in hardware_issues
        )

    # Arm 2 — the all-muted staged anchor.
    graph = read_loaded_camilla_graph()
    anchor_detail = graph.note or ""
    if not graph.note:
        is_anchor, identity_problem = _staged_anchor_identity(graph)
        if is_anchor:
            muted, mute_problem = _anchor_is_all_muted(graph)
            if muted:
                return True, (
                    "roleful, but the loaded graph IS this box's published "
                    "all-muted startup anchor, which emits silence on any "
                    "hardware"
                )
            anchor_detail = mute_problem
        else:
            anchor_detail = identity_problem

    return False, (
        "roleful topology, and neither proven arm holds: the applied baseline "
        f"({stale_detail or 'no applied active-speaker profile on this box'}) "
        f"and the all-muted staged anchor ({anchor_detail}). Leaving the graph "
        "where it is; re-apply the speaker profile at /correction/, then run "
        "`jasper-fanin-coupling-reconcile shm_ring`."
    )


# THE ring preflights, in ONE order.
#
# ORDER IS A DIAGNOSTIC DECISION, not cost. Each gate sits ahead of the gates
# whose answers would be MEANINGLESS or MISLEADING without it: topology
# eligibility first, because a box that resolves no ring width makes the wire
# question ill-posed (``resolve_ring_wire`` falls back to the shipped stereo
# declaration there, so a wire mismatch would name the wrong defect on a roleful
# box); asset presence before the two gates that READ those assets; capability
# before width, because a plugin that cannot parse the wire's fields is a
# blunter refusal than any per-end disagreement.
_SHARED_RING_PREFLIGHTS: tuple[tuple[str, RingGate], ...] = (
    ("ring_topology", ring_topology_ready_strict),
    ("ring_assets", ring_assets_ready),
    ("ring_wire_caps", ring_wire_caps_ready),
    ("ring_edge_width", ring_edge_width_ready),
)


def default_ring_gates() -> tuple[tuple[str, RingGate], ...]:
    """The ring preflights an unattended graph move must pass, in one order.

    Its one caller is :func:`jasper.fanin.converge.converge_active_endpoint`,
    which runs this set before re-emitting a roleful box's graph onto the ACTIVE
    ring — a graph move is a hearing event, so it is proved first. These are NOT
    a transport decision: the ring is the only transport (ADR-0100) and a box
    that fails them parks under its own name rather than resolving a route.

    The order is :data:`_SHARED_RING_PREFLIGHTS`' (documented there); this
    prepends ``ring_roleful_unattended``, the coarser question, because its
    refusal is the one an operator of a crossover box needs to read.
    """
    return (
        ("ring_roleful_unattended", ring_roleful_unattended_ready),
    ) + _SHARED_RING_PREFLIGHTS


def _migrate_stale_fanin_ring_slots(
    fanin_snapshot: _EnvSnapshot, reason: str
) -> tuple[_EnvSnapshot, bool]:
    """Override a stale, shear-prone ``JASPER_FANIN_RING_SLOTS`` into fanin.env.

    ``JASPER_FANIN_RING_SLOTS`` is an operator-tunable env (documented range
    2..16), so this does NOT blindly remove a non-default — a value that MATCHES
    the conf.d ``jts_ring_capture`` ``n_slots`` is a coherent operator override and
    stays. It writes the key into the later-loaded reconciler file ONLY when the
    shipped conf.d pins the current product default but an earlier env layer or
    fanin.env carries an env-only mismatch (any mismatched env-only value is
    incoherent without a matching conf.d). Writing the coherent value rather than
    deleting the key is deliberate: deleting from fanin.env can expose a stale
    value in ``/etc/jasper/jasper.env`` on the next systemd start.

    Returns the current snapshot and whether the key actually moved — the
    caller converges the daemons on a heal, because fan-in creates Ring A with
    this value and CamillaDSP's ioplug attaches expecting the conf.d's.

    Fail-safe: an unreadable conf.d (indeterminate expected geometry), an
    absent/default env value, a non-default custom conf.d mismatch, or an invalid
    value is a no-op — fan-in's own attach error is the backstop. A write failure
    logs and reports no heal.

    IT DOES NOT CONVERGE A BOX SHEARED ON AN AXIS IT DOES NOT OWN. This writes
    ONE axis — the Ring-A slot count. If the box also disagrees about the WIRE
    (fan-in's declared format vs the conf.d's), converging the slots would make
    the geometry look repaired while the ring still cannot attach, so the wire is
    read first and a shear there DECLINES the write. Declining costs nothing: the
    slots value is still writable on the next pass, once the wire agrees.

    IMPORTANT: this runs AFTER the coupling write, so the passed
    ``fanin_snapshot`` is the PRE-write snapshot; the file is re-read fresh here
    and the override written into the CURRENT content — writing the stale
    snapshot back would clobber the just-written coupling line.
    """
    from jasper.fanin_coupling import (
        DEFAULT_FANIN_RING_SLOTS,
        RING_SLOTS_ENV_VAR,
    )
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        ring_conf_channels,
        ring_conf_format,
    )
    from jasper.ring_assets import ring_conf_n_slots

    # Re-read fresh: the coupling flip was already written to this file above.
    current = _read_snapshot(fanin_snapshot.path)

    # The axes this function does NOT own, read before it writes the one it does.
    conf_format = ring_conf_format(RING_A_CONF_PCM)
    conf_channels = ring_conf_channels(RING_A_CONF_PCM)
    fanin_format, fanin_format_source = resolve_effective_fanin_wire_format(
        current.text
    )
    from jasper.fanin_coupling import RING_A_CHANNELS

    wire_shear = ""
    if conf_format is not None and conf_format != fanin_format:
        wire_shear = (
            f"fan-in declares wire format {fanin_format} (from "
            f"{fanin_format_source}) but conf.d pcm.{RING_A_CONF_PCM} declares "
            f"{conf_format}"
        )
    elif conf_channels is not None and conf_channels != RING_A_CHANNELS:
        wire_shear = (
            f"conf.d pcm.{RING_A_CONF_PCM} declares {conf_channels} channels but "
            f"fan-in's mixer is fixed at {RING_A_CHANNELS}"
        )
    if wire_shear:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_slots_override_declined",
            reason=reason,
            key=RING_SLOTS_ENV_VAR,
            detail=(
                f"{wire_shear} — converging the slot count alone would report a "
                "geometry this box does not have"
            ),
            level=logging.WARNING,
        )
        return current, False

    conf_a = ring_conf_n_slots(RING_A_CONF_PCM)
    if conf_a is None:
        return current, False  # indeterminate conf.d → nothing provable to heal.
    resolution = resolve_effective_fanin_ring_slots(current.text)
    if resolution.raw is None or (
        resolution.raw.strip() == "" and resolution.source == "default"
    ):
        return current, False  # nothing persisted → default already coherent.
    if resolution.value is None:
        return current, False  # invalid → fan-in refuses with a crisp reason.
    if resolution.value == conf_a:
        return current, False  # coherent operator override → keep it.
    if conf_a != DEFAULT_FANIN_RING_SLOTS:
        return current, False  # custom conf.d mismatch → fan-in must fail loud.

    new_text, changed = _apply_action(
        current.text, RuntimeEnvAction("set", RING_SLOTS_ENV_VAR, str(conf_a))
    )
    if not changed:
        return current, False
    try:
        _write_env_text(current.path, new_text)
    except OSError as e:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_slots_override_failed",
            reason=reason,
            key=RING_SLOTS_ENV_VAR,
            value=resolution.raw,
            source=resolution.source,
            error=e,
            level=logging.WARNING,
        )
        return current, False
    os.environ[RING_SLOTS_ENV_VAR] = str(conf_a)
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="stale_ring_slots_overridden",
        reason=reason,
        key=RING_SLOTS_ENV_VAR,
        stale_value=resolution.raw,
        stale_source=resolution.source,
        conf_n_slots=conf_a,
    )
    return _EnvSnapshot(current.path, new_text, True), True


def _delete_stale_ring_files(reason: str, fanin_text: str = "") -> bool:
    """Delete on-disk ring files whose geometry != the expected one. Did any go?

    A ring file left over from a PRIOR geometry is a create-or-ATTACH ``open()``
    error for the writer: ``RingWriter::create_or_attach`` validates the existing
    header's geometry against the requested one and bails on a mismatch. The
    files live on tmpfs (``/dev/shm``) — pure transport state, recreated by the
    writer on its next start, NOT user data — so deleting a geometry-mismatched
    file is safe. The caller converges the daemons when this returns True,
    because a deleted file is only re-created by the writer coming back.

    Only deletes a file whose header is VALID (carries the ``JRIN`` magic) AND
    whose geometry differs from what fan-in / the conf.d will create, on ANY of
    the four attach-compared axes: ``n_slots``, ``period_frames`` (the ring slot
    IS one outputd period), ``sample_format`` and ``channels``. The comparison
    is :func:`jasper.ring_assets.ring_header_matches_conf`, shared with the
    doctor so the two cannot mean different things by "coherent".

    THE FORMAT AXIS IS WHAT MAKES THE WIRE ROLLBACK LEVER REPEATABLE. Forcing the
    wire narrow again (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) leaves the WIDE
    ring file on disk, which the writer rejects at attach as a config-class
    fault; clearing a format-mismatched file here is what saves the operator an
    ``rm`` by hand.

    A magic-less / absent / correct-geometry file is left untouched (the writer
    reclaims a magic-less file itself; a correct file is reused). Best-effort: a
    delete failure is logged, never raised — the writer's own attach error is the
    backstop.

    ``fanin_text`` is the (post-migration) fanin.env text — used ONLY as the
    fallback expected Ring-A slot count when the conf.d is unreadable.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, resolve_ring_slots
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        RING_A_PROGRAM_FILE,
        RING_ACTIVE_CONF_PCM,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONF_PCM,
        RING_B_CONTENT_FILE,
        ring_conf_n_slots,
        ring_header_matches_conf,
    )

    # Expected Ring-A slot count: the conf.d is the attach authority for what the
    # ioplug expects; fall back to fan-in's resolved env if the conf.d is
    # unreadable. Compare on-disk against the value the ioplug attaches with.
    try:
        fanin_slots = resolve_ring_slots(read_value(fanin_text, RING_SLOTS_ENV_VAR))
    except ValueError:
        fanin_slots = None
    expected_a = ring_conf_n_slots(RING_A_CONF_PCM)
    if expected_a is None:
        expected_a = fanin_slots

    deleted = False
    for path, pcm_name, expected_slots in (
        (RING_A_PROGRAM_FILE, RING_A_CONF_PCM, expected_a),
        (RING_B_CONTENT_FILE, RING_B_CONF_PCM, None),
        # The ACTIVE ring is judged against ITS OWN conf.d block, which is the
        # one whose CHANNELS legitimately differ per box. Unlike Ring B it can go
        # stale without anything else changing: a re-commission from a 2-way to a
        # 3-way moves only this file's width.
        (RING_ACTIVE_CONTENT_FILE, RING_ACTIVE_CONF_PCM, None),
    ):
        verdict = ring_header_matches_conf(
            path, pcm_name, expected_n_slots=expected_slots
        )
        if not verdict.present or verdict.ok:
            continue  # nothing to judge, or coherent on every axis.
        try:
            os.unlink(path)
        except OSError as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="stale_ring_unlink_failed",
                reason=reason,
                path=path,
                axis=verdict.axis,
                detail=verdict.detail,
                error=e,
                level=logging.WARNING,
            )
            continue
        deleted = True
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_deleted",
            reason=reason,
            path=path,
            axis=verdict.axis,
            detail=verdict.detail,
        )
    return deleted


def _restore_snapshot(snapshot: _EnvSnapshot) -> None:
    """Restore the env file to its pre-write contents. Best-effort."""
    try:
        if snapshot.existed:
            atomic_write_text(snapshot.path, snapshot.text)
        elif snapshot.path.exists():
            snapshot.path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_env_text(path: Path, text: str) -> None:
    if text:
        atomic_write_text(path, text)
    elif path.exists():
        path.unlink(missing_ok=True)


def _apply_action(text: str, action: RuntimeEnvAction) -> tuple[str, bool]:
    if action.action == "set":
        return upsert(text, action.key, action.value)
    return remove(text, action.key)


def _outputd_ring_path_for(outputd_text: str) -> str:
    """The ring file outputd must read, derived from the endpoint marker.

    ONE writer for the path half of outputd's ring-path/marker biconditional.
    Armed -> the ACTIVE ring's file; unarmed -> the operator's custom Ring B
    path if they set one, else the canonical Ring B default.

    The marker is read from the outputd.env TEXT the caller is reconciling
    rather than from the file on disk, so the path written and the marker
    written come from one snapshot; a second file read could straddle a
    concurrent hardware reconcile and emit a crossed pair.

    The asymmetry is deliberate: an operator's custom path is honoured on the
    STEREO ring and ignored on the ACTIVE one. There is exactly one legal
    active-ring file — outputd's allowlist compares against that named constant
    — so "preserving" a custom value there could only produce the crossed pair
    the allowlist refuses.

    TOTAL INTO THE LEGAL SET, in BOTH directions. The armed branch discards
    whatever the key held; the unarmed branch must equally refuse to CARRY
    FORWARD the active ring's own file, because the allowlist is a biconditional
    — that file may be read only by an armed endpoint, exactly as an armed
    endpoint may read only that file. Preserving it on the unarmed side makes the
    disarm direction STICKY (a box whose marker cleared while the coupling stayed
    ``shm_ring`` keeps pointing outputd at a ring whose only writer stood down,
    and every later pass preserves it again). Falling back to the stereo default
    is what makes every crossed pair one pass from healed, whichever half moved.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        DEFAULT_OUTPUTD_RING_PATH,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        ring_active_endpoint_armed,
    )

    armed = ring_active_endpoint_armed(
        {
            OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: read_value(
                outputd_text, OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR
            )
            or ""
        }
    )
    if armed:
        return DEFAULT_OUTPUTD_ACTIVE_RING_PATH
    carried = resolve_outputd_ring_path(
        read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR)
    )
    if carried == DEFAULT_OUTPUTD_ACTIVE_RING_PATH:
        return DEFAULT_OUTPUTD_RING_PATH
    return carried


def _outputd_actions(outputd_text: str) -> tuple[RuntimeEnvAction, ...]:
    """The COMPLETE set of reconciler-owned outputd.env actions for the ring.

    Sets ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the post-DSP ring's
    path/slots — content.ring, or active-content.ring on an armed roleful box.
    The two rings move together: fan-in's Ring A capture (fanin.env) and
    outputd's post-DSP ring bridge (here) are ONE coupling, and a split leaves
    one end reading or writing a ring nobody serves.

    It also UNSETS the legacy ``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` key — a
    one-way migration sweep so a box that once armed it converges clean on its
    next reconcile.

    **The ring PATH converges from the endpoint MARKER, it is not preserved.**
    outputd enforces a biconditional between the two — the active ring file may
    be read only by an armed active endpoint, and an armed active endpoint may
    read only that file. The marker is the FACT (written by
    ``jasper-audio-hardware-reconcile`` from the accepted active-lane decision)
    and the path is its PROJECTION, derived here by
    :func:`_outputd_ring_path_for`. A preserve-else-stereo default would write
    the full-range Ring B path onto an armed box, and outputd would refuse the
    pair at startup.

    THIS RUNS ON EVERY PASS, before the transition-vs-confirm split, so it is
    also the pair's RECOVERY: whichever half moved last, one pass converges the
    other. The two halves have different writers and cannot move in one write —
    the marker's writer runs first (``jasper-audio-hardware-reconcile``,
    ``Before=jasper-outputd``) and kicks ``jasper-fanin-coupling-auto.service``,
    which runs this. Between those two the pair is legitimately crossed;
    :func:`jasper.transport_coherence.transport_coherence_report` reports that
    window as the first-arm waypoint (a note) rather than a contradiction,
    because a refusal there could only fire while the projection is stale and
    would block the writer that converges it.
    """
    return (
        RuntimeEnvAction(
            "set", OUTPUTD_CONTENT_BRIDGE_ENV_VAR, OUTPUTD_CONTENT_BRIDGE_SHM_RING
        ),
        RuntimeEnvAction(
            "set",
            OUTPUTD_RING_PATH_ENV_VAR,
            _outputd_ring_path_for(outputd_text),
        ),
        RuntimeEnvAction(
            "set",
            OUTPUTD_RING_SLOTS_ENV_VAR,
            str(
                resolve_outputd_ring_slots(
                    read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR)
                )
            ),
        ),
        RuntimeEnvAction("unset", _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV),
    )


def _apply_actions(
    text: str, actions: tuple[RuntimeEnvAction, ...]
) -> tuple[str, bool]:
    """Fold a sequence of env actions onto ``text``; changed = any moved the file."""
    changed = False
    for action in actions:
        text, moved = _apply_action(text, action)
        changed = changed or moved
    return text, changed


def _sync_process_env_for_emit(outputd_text: str) -> None:
    """Make the in-process Camilla re-emit see the env we just persisted.

    Mirrors :func:`_outputd_actions`: the in-process env must carry the SAME
    content-source keys the files now carry so the immediate camilla re-emit names
    the right devices for any reader. The ring PATH comes from
    :func:`_outputd_ring_path_for`, the same single derivation the persisted
    write uses, so the in-process env can never carry a different ring than the
    file just written.
    """
    os.environ[COUPLING_ENV_VAR] = COUPLING_SHM_RING
    os.environ[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] = OUTPUTD_CONTENT_BRIDGE_SHM_RING
    os.environ[OUTPUTD_RING_PATH_ENV_VAR] = _outputd_ring_path_for(outputd_text)
    os.environ[OUTPUTD_RING_SLOTS_ENV_VAR] = str(
        resolve_outputd_ring_slots(
            read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR)
        )
    )
    os.environ.pop(_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV, None)


@dataclass(frozen=True)
class EntryLock:
    """Outcome of the entry-verb lock acquisition.

    ``outcome`` is ``acquired`` (``fh`` holds the advisory flock — the caller
    keeps it open for the WHOLE pass and closes it after), ``contended``
    (another reconcile pass held the lock past the bounded wait — the caller
    must abort loudly before touching env or daemons), or ``unavailable`` (the
    lock file could not be opened — fail-open: proceed unserialized rather than
    brick the reconcile; already logged at WARNING inside the helper).
    ``detail`` carries the holder pid / open error for the log line.
    """

    outcome: str
    fh: "IO[str] | None" = None
    detail: str = ""


def _acquire_entry_lock(
    path: str | Path = ENTRY_LOCK_PATH,
    *,
    timeout_seconds: float = ENTRY_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = ENTRY_LOCK_POLL_SECONDS,
) -> EntryLock:
    """Serialize the reconcile entry verbs behind one advisory flock.

    ``jasper-fanin-coupling-auto.service``, install.sh, and an operator CLI can
    invoke the same transition concurrently. One flock held for the whole pass
    keeps their ordered CamillaDSP/fan-in/outputd transitions atomic. systemd
    serializes starts of the same unit; this lock covers unit-vs-CLI and direct
    CLI-vs-CLI pairs.

    Bounded wait, never open-ended: contention past ``timeout_seconds`` returns
    ``contended`` and the caller aborts through
    :func:`_handle_entry_lock_contention` before any env write or daemon op (no
    partial state to unwind). The wait absorbs the common fast confirm-path
    ``--auto`` holder; a genuinely long transition in flight SHOULD abort rather
    than stack.

    Fail-open on an unopenable lock file (missing /run on a dev host, a
    non-root probe): a broken lock path must not brick reconciles — proceed
    unserialized at WARNING. The holder stamps its pid into the file so the
    contention log can name it.
    """
    p = Path(path)
    try:
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fh: IO[str] = os.fdopen(fd, "r+", encoding="utf-8")
        except Exception:  # noqa: BLE001 - never leak the fd on a fdopen failure
            os.close(fd)
            raise
    except OSError as e:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="entry_lock_unavailable",
            lock_path=str(p),
            error=e,
            level=logging.WARNING,
        )
        return EntryLock(outcome="unavailable", detail=str(e))
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                try:
                    fh.seek(0)
                    holder = fh.read(64).strip()
                except OSError:
                    holder = ""
                fh.close()
                return EntryLock(
                    outcome="contended",
                    detail=f"held by pid {holder or 'unknown'}",
                )
            time.sleep(poll_seconds)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass  # pid stamp is diagnostic only — never fail an acquired lock on it
    return EntryLock(outcome="acquired", fh=fh)


def main(argv: "list[str] | None" = None) -> int:
    """CLI: ``jasper-fanin-coupling-reconcile shm_ring`` (an operator asking for
    the ring convergence now) or ``--auto`` (the unattended boot/deploy pass,
    which also converges the USB combo from household intent).

    Every verb runs under the shared entry flock (:func:`_acquire_entry_lock`)
    so two passes can never interleave their ordered daemon convergences.
    """
    import argparse

    # This CLI is the jasper-fanin-coupling-auto systemd entrypoint, so its
    # journal is where INFO-level transition evidence lands. Without a configured
    # handler the root logger falls back to Python's lastResort handler (WARNING+)
    # and silently drops normal confirmations.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # `reconcile_current_dsp` swaps the live graph from this process, so its
    # swap duck needs a canonical target to release to.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    parser = argparse.ArgumentParser(
        prog="jasper-fanin-coupling-reconcile",
        description="Converge the fan-in -> CamillaDSP ring coupling in order.",
    )
    parser.add_argument(
        "coupling",
        nargs="?",
        choices=[COUPLING_SHM_RING],
        help=(
            "converge the box onto the ring now: Ring A plus the post-DSP SHM "
            "ring (Ring B, or the ACTIVE ring on an armed roleful box). "
            "Mutually exclusive with --auto."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "the unattended boot/deploy pass: converge the USB combo from "
            "household source intent, then converge the ring."
        ),
    )
    parser.add_argument("--reason", default="cli")
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="write the env only; skip the daemon transition (staging).",
    )
    args = parser.parse_args(argv)
    _modes = [args.auto, args.coupling is not None]
    if sum(bool(m) for m in _modes) > 1:
        parser.error("--auto and an explicit coupling are mutually exclusive")
    if not any(_modes):
        parser.error("give an explicit coupling or --auto")

    # Serialize the WHOLE pass against the sibling entry verbs — see
    # _acquire_entry_lock. On contention past the bounded wait, do NOT touch env
    # or daemons.
    lock = _acquire_entry_lock(
        ENTRY_LOCK_PATH,
        timeout_seconds=ENTRY_LOCK_TIMEOUT_SECONDS,
        poll_seconds=ENTRY_LOCK_POLL_SECONDS,
    )
    if lock.outcome == "contended":
        return _handle_entry_lock_contention(args, detail=lock.detail)
    try:
        return _run_entry_verb(args)
    finally:
        if lock.fh is not None:
            lock.fh.close()


def _handle_entry_lock_contention(args, *, detail: str = "") -> int:
    """Abort an apply verb that could not acquire the coupling entry lock."""
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="entry_lock_contended",
        reason=args.reason,
        lock_path=ENTRY_LOCK_PATH,
        timeout_seconds=ENTRY_LOCK_TIMEOUT_SECONDS,
        detail=detail or None,
        level=logging.ERROR,
    )
    print(
        "fan-in coupling reconcile: another reconcile pass holds "
        f"{ENTRY_LOCK_PATH} ({detail or 'unknown holder'}); "
        f"aborted after {ENTRY_LOCK_TIMEOUT_SECONDS:g}s without touching env "
        "or daemons.",
        file=sys.stderr,
    )
    return 1


def _run_entry_verb(args) -> int:
    """Body after validation and entry-lock acquisition."""
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE reconciling, so the camilla reconcile this triggers emits with
    # the persisted JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} etc. — not their
    # defaults. Without this, arming a coupling from a bare CLI/install shell
    # silently RESETS a tuned chunksize back to 1024. setdefault semantics keep
    # an explicit shell override winning. Mirrors jasper.cli.sound.
    from jasper.env_load import load_env_files

    load_env_files()

    # Converge the ACTIVE endpoint before the ring convergence reads it.
    # Inside the entry flock, so it can never interleave with another pass.
    # UNATTENDED PATH ONLY, and skipped under --no-apply, which promises
    # env-only staging. A refusal is NOT an abort: it leaves the box as it found
    # it and the box parks under its own name if nothing carries its program.
    if args.auto and not args.no_apply:
        from jasper.fanin.converge import converge_active_endpoint

        # Guarded because this step runs BEFORE the rest of the pass: a
        # convergence that cannot even decide must cost the box its convergence,
        # never its reconcile. The catch is NARROW and derived — the reachable
        # raise is a corrupt fanin.env (``_read_snapshot`` catches ``OSError``
        # only, so a non-UTF-8 byte arrives as ``UnicodeDecodeError``, a
        # ``ValueError``) plus ``OSError`` from its file reads. Anything outside
        # that pair is a bug and must not be swallowed here.
        try:
            converge_active_endpoint(reason=args.reason)
        except (OSError, ValueError) as exc:
            log_event(
                logger,
                "fanin.converge",
                result="converge_raised",
                reason=args.reason,
                detail=f"{type(exc).__name__}: {exc}",
                level=logging.ERROR,
            )

    if args.auto:
        auto = reconcile_auto(reason=args.reason, apply=not args.no_apply)
        print(
            f"coupling auto: gadget={auto.gadget_present} "
            f"usb_intent={auto.usb_intent_enabled} "
            f"combo_armed={auto.combo_armed} "
            f"usb_combo_changed={auto.usb_combo_changed} ok={auto.ok}"
            + (
                f" fanin_restarted_for_combo={auto.restarted_fanin_for_combo}"
                if auto.usb_combo_changed
                else ""
            )
            + (f" reason={auto.reason}" if auto.reason else "")
            + (f" detail={auto.detail}" if auto.detail else "")
        )
        return 0 if auto.ok else 1

    result = reconcile_coupling(
        reason=args.reason,
        apply=not args.no_apply,
    )
    print(
        f"coupling reconcile: ok={result.ok} changed={result.changed} "
        f"outputd={result.restarted_outputd} fanin={result.restarted_fanin} "
        f"camilla={result.reconciled_camilla}"
        + (f" detail={result.detail}" if result.detail else "")
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
