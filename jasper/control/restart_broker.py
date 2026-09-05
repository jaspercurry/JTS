# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Privileged restart broker — the mediated systemctl door for non-root daemons.

The JTS service daemons (jasper-web's wizards, jasper-mux's librespot
recovery, the wiim-remote adapter, and the rest) run as dedicated non-root
service users, so they cannot ``systemctl`` anything themselves. jasper-control
hosts this local UNIX-socket broker and performs a tightly-scoped unit action
on their behalf.

Why this is safe to centralise:

- **Peer-credential auth (SO_PEERCRED).** The connecting process's uid is read
  from the kernel — unforgeable, no token to steal. Only the known JTS service
  users (and root) may call. The socket also lives at 0660 under
  jasper-control's ``RuntimeDirectory``, so filesystem perms are a second gate.
- **Closed verb vocabulary.** The broker NEVER runs an arbitrary ``systemctl`` verb or
  argument — :data:`ALLOWED_VERBS` maps each verb to a fixed argv prefix. A compromised
  client cannot smuggle ``systemctl ... ; rm -rf`` or a ``--property=ExecStart=``
  injection. (Note: the ``enable`` / ``enable-now`` / ``disable-now`` verbs map to
  ``org.freedesktop.systemd1.manage-unit-files``, which the polkit rule deliberately
  does NOT grant the non-root ``jasper-control`` — it can't be unit-scoped and
  ``restart`` consults it, so a grant would re-open restart-of-any-unit. Those verbs
  therefore fail-soft for a non-root broker; nothing routes through them today — voice's
  boot-enable is owned by the root ``jasper-aec-reconcile``. The verbs stay in the
  vocabulary for a future root client / Phase-4 grant.)
- **Unit allowlist.** Every requested unit must be in :data:`MANAGED_UNITS` —
  the single source of truth for "units the privileged surface may generally
  touch" — or, for graph-transition root helpers, in
  :data:`START_ONLY_UNITS` with verb ``start``. The Phase-3 *user-drop* PR
  derives the polkit rule (which grants the ``jasper-control`` user
  ``manage-units`` for exactly these units) from the same constants, so the
  broker authz and the polkit grant can never drift. The broker, not polkit,
  enforces start-only semantics for the helper set.
- **Audit.** Every request and every denial emits a stable ``event=`` log line
  with the peer uid/pid, verb, units, and reason. Nothing here is a secret, so
  the lines are safe to journal.

NOT brokered, by design — this is the door for non-root *clients*, not the
tree's only ``systemctl``. Direct calls elsewhere are the design, not drift:

- **Read-only probes** (``is-active`` / ``is-enabled`` / ``show``): systemd
  lets any user run them, so callers query directly. Only state-changing verbs
  need a broker.
- **jasper-control's own supervisors and endpoints**: they already run as the
  uid the broker would act as, so they call ``systemctl`` directly and polkit
  mediates the same grant — see ``deploy/systemd/jasper-control.service``.
- **Root oneshots and CLIs** (``deploy/bin/*``, the ``jasper-*-reconcile``
  units, ``jasper.cli.*`` under sudo): they hold the privilege themselves and
  are outside the client set. This is also where the un-brokerable verbs live —
  ``jasper.source_intent`` runs ``enable``/``disable`` directly because the
  non-root broker deliberately cannot (see the closed-vocabulary note above).
  Such helpers may still call :func:`manage_units` for the allowlist and audit
  trail; the root fallback below keeps them working when the broker is down.

**Root fallback.** :func:`manage_units` tries the broker first and, only if the
broker is unreachable **and** the caller is root (``os.geteuid() == 0``), falls
back to a direct ``systemctl`` — logged LOUDLY (``event=restart_broker.
fallback_direct``) so a silently-broken broker path stays visible. A non-root
client has no fallback: ``geteuid() != 0``, so the broker is its only path.

Wire format mirrors the other JTS control sockets (voice/mux/peering): a single
newline-delimited JSON request, a single newline-delimited JSON response.
"""
from __future__ import annotations

import json
import logging
import os
import pwd
import socket
import struct
import subprocess
import threading
from socketserver import StreamRequestHandler, ThreadingUnixStreamServer
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)
_SELF_UNIT = "jasper-control.service"

# The broker socket. jasper-control declares RuntimeDirectory=jasper-control,
# so /run/jasper-control exists owned by the unit's user. Overridable for
# tests / headless runs.
DEFAULT_SOCKET_PATH = os.environ.get(
    "JASPER_RESTART_BROKER_SOCKET", "/run/jasper-control/restart.sock",
)

# SINGLE SOURCE OF TRUTH for the units the BROKER will act on — not a census of
# every unit the daemons touch (see the not-brokered list in the module
# docstring). The polkit rule grants the jasper-control user manage-units for
# exactly this set, so the two never drift. Keep entries fully-qualified
# (".service"); the broker normalizes bare names before checking.
MANAGED_UNITS = frozenset({
    # Tier-A daemons (debug-restart, /speaker rename, /rooms, /voice, /spotify)
    "jasper-voice.service",
    "jasper-control.service",
    "jasper-web.service",
    "jasper-mux.service",
    "jasper-input.service",
    # Audio chain + reconcilers (control endpoints / supervisors /
    # wake-corpus bridge-output enable flow)
    "jasper-aec-bridge.service",
    "jasper-aec-init.service",
    "jasper-aec-reconcile.service",
    "jasper-grouping-reconcile.service",
    "jasper-grouping-reconcile-trailing.service",
    "jasper-camilla.service",
    "jasper-outputd.service",
    # jasper.fanin.coupling_reconcile restarts fan-in to apply a coupling or
    # USB-combo flip. Caught on jts 2026-06-27 (then via the since-deleted
    # adaptive output-buffer arm): the restart was rejected ("not in allowlist")
    # because fan-in had never been broker-restarted before — the unit tests
    # mocked the broker so they never hit this. Keep in lockstep with the
    # polkit grant.
    "jasper-fanin.service",
    # Root oneshot that captures `jasper-doctor --json` at full fidelity for the
    # /system/diagnostics card — the non-root jasper-control `systemctl start`s
    # it via its polkit manage-units grant.
    "jasper-doctor-json.service",
    # AirPlay / Spotify / USB renderers (/sources, /airplay, mux, correction)
    "shairport-sync.service",
    "nqptp.service",
    "librespot.service",
    "jasper-usbsink.service",
    # Durable, naturally-debounced USB microphone descriptor/producer apply.
    # jasper-control restarts it after persisting intent; the root oneshot owns
    # the delayed recompose so an in-process timer cannot be lost on exit.
    "jasper-usbmic-apply.service",
    # The relay itself (distinct from -apply above). /speaker rename's
    # post-gadget-rebuild refresh try-restarts this directly, the same
    # jasper-fanin incident repeating one unit over: never brokered before,
    # so it was missing here even though jasper-web (non-root) needs it.
    "jasper-usbmic.service",
    # jasper-usbgadget owns the hardware-gated composite ConfigFS gadget
    # (default-on USB network + wizard-toggled USB audio). The root coordinator restarts
    # it to recompose the audio function after source/role changes; /speaker
    # restarts it so the name-patch reruns. Replaces the deleted
    # jasper-usbsink-init.service.
    "jasper-usbgadget.service",
    # Bluetooth stack (/speaker rename restarts the whole BT chain)
    "bluetooth.service",
    "bluealsa.service",
    "bluealsa-aplay.service",
    "bt-agent.service",
})

# Fixed root helpers that non-root clients may only *start*. These remain out
# of MANAGED_UNITS so Tier-B reconcilers are not generally brokerable; this is
# just enough for graph transitions to request a bounded reconciliation pass.
START_ONLY_UNITS = frozenset({
    "jasper-audio-hardware-reconcile.service",
    # Root oneshot that resolves the fan-in coupling + USB low-latency combo
    # (jasper.fanin.coupling_auto). Normally runs at boot/deploy, but the
    # /sources/ USB-audio toggle (jasper-web, non-root) starts it right after
    # an enable/disable so the combo arms/disarms immediately instead of only
    # at the next reboot. Start-only: jasper-web may kick a reconcile pass, not
    # stop/restart the reconciler (mirrors jasper-wifi-scan-repair).
    "jasper-fanin-coupling-auto.service",
    # Root oneshot that persists /sources enable/disable intent
    # (jasper.source_intent). enable/disable is manage-unit-files, which the
    # non-root broker deliberately cannot run (can't be unit-scoped; systemctl
    # restart consults it → would re-open restart-of-any-unit). So the /sources/
    # wizard (jasper-web, non-root) records intent in source_intent.env and
    # starts this helper to apply it. Start-only: jasper-web may kick a
    # persistence pass, not stop/restart the helper (mirrors
    # jasper-wifi-scan-repair). The helper enforces its own source-unit allowlist
    # internally; the broker/polkit only allow STARTING it.
    "jasper-source-intent-reconcile.service",
    "jasper-wifi-scan-repair.service",
    # Root oneshot that reserves BLE connection-event length on the live WiiM
    # Remote 2 link (jasper.cli.wiim_remote_ce). BlueZ hardcodes CE length to
    # 0, which starves the remote's mic to ~24 % of realtime on a Pi Zero 2 W.
    # The reservation lives on the connection, so the non-root adapter task
    # (inside jasper-input) must re-request it on every reconnect. Start-only:
    # the adapter may kick one reservation pass, not stop/restart the helper
    # (mirrors jasper-wifi-scan-repair). The helper
    # takes NO argument identifying a connection — it enumerates live LE links
    # and cross-checks them against BlueZ itself, so the caller cannot aim it
    # at an unrelated Bluetooth device.
    "jasper-wiim-remote-ce.service",
    "jasper-xvf-firmware-update.service",
    "jasper-enhanced-aec-install.service",
    # Root oneshot running the audible chip-AEC re-measurement
    # (jasper.cli.aec_commission). The non-root jasper-control starts it
    # directly (POST /aec/commission) under the polkit manage-units grant this
    # set derives; start-only for broker clients like every maintenance
    # oneshot here.
    "jasper-aec-commission.service",
})

POLKIT_MANAGE_UNITS = MANAGED_UNITS | START_ONLY_UNITS

# Service users permitted to call the broker (root is always permitted — the
# still-root clients, and operator debugging). Resolved fresh per request so a
# user created by a deploy works without restarting the broker.
BROKER_CLIENT_USERS = (
    "jasper-control",
    "jasper-web",
    "jasper-mux",
    "jasper-voice",
    "jasper-input",
)

# Closed verb vocabulary: verb -> (systemctl argv tail before units,
# whether --no-block is meaningful). The broker only ever runs one of these.
_VERB_ARGV: dict[str, tuple[list[str], bool]] = {
    "restart": (["restart"], True),
    "try-restart": (["try-restart"], True),
    "start": (["start"], True),
    "stop": (["stop"], True),
    "enable": (["enable"], False),
    "enable-now": (["enable", "--now"], True),
    "disable-now": (["disable", "--now"], True),
    "reset-failed": (["reset-failed"], False),
}
ALLOWED_VERBS = frozenset(_VERB_ARGV)

# Per-request systemctl exec timeout: the client passes how long it's willing
# to wait, the broker runs systemctl with that bound (clamped), and the client
# then waits slightly LONGER on the socket — so the broker always returns a
# verdict before the client gives up (no racing-deadlines truncation of a
# legitimate blocking restart). --no-block calls return in ms regardless.
_DEFAULT_EXEC_TIMEOUT_SEC = 30.0
# Ordinary broker actions retain the original hard ceiling.  The sole extended
# shape is a blocking start of exactly the source-intent coordinator: its finite
# 2693-second systemd bound covers all four sources, bounded owner barriers,
# failed-unit resets, and fail-closed cleanup; its caller allows 2703 seconds for
# PID 1 to return. That pair is jasper.source_intent's
# RECONCILE_SYSTEMD_TIMEOUT_SECONDS / RECONCILE_BROKER_TIMEOUT_SECONDS, mirrored
# here rather than imported so this root boundary keeps its lean import surface;
# tests/test_source_intent_systemd.py fails if this ceiling drops below the
# coordinator's broker bound, which is the direction that truncates a pass.
# Derive that exception from the already-normalized, validated request on the
# server; a client-supplied number alone never grants a longer broker thread.
_EXEC_TIMEOUT_CEILING_SEC = 120.0
# `reset-failed` is a bookkeeping call against PID 1 with no unit transition to
# wait on, so :func:`reset_then_manage` bounds it independently of whatever the
# action it precedes is allowed to take.
_RESET_TIMEOUT_SEC = 5.0
_SOURCE_INTENT_RECONCILE_UNIT = "jasper-source-intent-reconcile.service"
_SOURCE_INTENT_EXEC_TIMEOUT_CEILING_SEC = 2703.0
_CAMILLA_UNIT = "jasper-camilla.service"
# jasper-camilla.service Requires= (and is After=) a Type=oneshot hardware
# reconciler whose RemainAfterExit is unset, so every camilla START re-queues
# that oneshot in full. Measured on jts4 (Pi Zero 2 W, 2026-08-21): the
# reconciler took 25.5-26.0 s inside camilla restarts of 30.307 / 28.675 /
# 28.723 s. Its caller derives its bound from the reconciler's declared 50 s
# ceiling plus camilla's own 90 s plus a margin; clamping that back to the
# ordinary 120 s here would re-create exactly the false timeout the derived
# bound exists to remove. Mirrors
# jasper.fanin.coupling_reconcile._CAMILLA_START_TIMEOUT_SEC and is pinned to it
# by tests/test_restart_broker.py.
_CAMILLA_START_EXEC_TIMEOUT_CEILING_SEC = 236.0
# Blocking single-unit requests whose target can legally outrun the ordinary
# ceiling, keyed per (unit, verb) so the 120 s backstop still governs every other
# request — including every other verb against these same units.
_EXTENDED_EXEC_TIMEOUT_CEILING_SEC: dict[tuple[str, str], float] = {
    (_SOURCE_INTENT_RECONCILE_UNIT, "start"): _SOURCE_INTENT_EXEC_TIMEOUT_CEILING_SEC,
    (_CAMILLA_UNIT, "start"): _CAMILLA_START_EXEC_TIMEOUT_CEILING_SEC,
}
_CLIENT_SOCKET_MARGIN_SEC = 5.0    # client waits this much past the exec bound
_MAX_REQUEST_BYTES = 4096


def _clamp_exec_timeout(
    raw: Any,
    *,
    verb: str,
    units: list[str],
    no_block: bool,
) -> float:
    ceiling = _EXEC_TIMEOUT_CEILING_SEC
    if not no_block and len(units) == 1:
        # An exemption is per (unit, verb) and only for a blocking single-unit
        # request; a batch or a --no-block kick keeps the ordinary ceiling.
        ceiling = _EXTENDED_EXEC_TIMEOUT_CEILING_SEC.get((units[0], verb), ceiling)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EXEC_TIMEOUT_SEC
    return min(max(value, 1.0), ceiling)


class BrokerUnavailable(RuntimeError):
    """The restart broker socket could not be reached or did not answer."""


def _normalize_unit(unit: str) -> str:
    """Append the default ``.service`` suffix when a bare unit name is given,
    matching systemctl's own behaviour. ``jasper-voice`` -> ``jasper-voice.service``;
    a name that already carries a unit suffix (``.service``/``.socket``/...) is
    returned unchanged."""
    name = unit.strip()
    if "." not in name.rsplit("/", 1)[-1]:
        return name + ".service"
    return name


def _build_argv(verb: str, units: list[str], *, no_block: bool) -> list[str]:
    """Build the fixed systemctl argv for a validated verb + units. Raises
    KeyError for an unknown verb (callers validate first)."""
    tail, supports_no_block = _VERB_ARGV[verb]
    argv = ["systemctl", *tail]
    if no_block and supports_no_block:
        argv.append("--no-block")
    argv.extend(units)
    return argv


def _run_systemctl_request(
    verb: str,
    units: list[str],
    *,
    no_block: bool,
    exec_timeout: float,
) -> tuple[int | None, str, bool]:
    """Execute a validated request and return ``(rc, stderr, self_deferred)``.
    ``rc=None`` means the control self-restart was queued but cannot be
    confirmed without risking the broker dying before it replies.

    Restarting jasper-control from inside jasper-control is special: a single
    ``systemctl restart voice control mux`` can kill the broker before systemd
    has queued the later units. Queue non-self units first, then fire the
    control restart as a detached no-block command so the broker can answer.
    """
    if verb == "restart" and _SELF_UNIT in units:
        non_self_units = [u for u in units if u != _SELF_UNIT]
        if non_self_units:
            first = subprocess.run(
                _build_argv(verb, non_self_units, no_block=no_block),
                check=False,
                timeout=exec_timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if first.returncode != 0:
                return first.returncode, (first.stderr or "").strip(), False

        subprocess.Popen(
            _build_argv(verb, [_SELF_UNIT], no_block=True),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return None, "", True

    proc = subprocess.run(
        _build_argv(verb, units, no_block=no_block),
        check=False,
        timeout=exec_timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, (proc.stderr or "").strip(), False


def _unit_allowed_for_verb(unit: str, verb: str) -> bool:
    return unit in MANAGED_UNITS or (verb == "start" and unit in START_ONLY_UNITS)


def _allowed_uids() -> set[int]:
    """The set of uids permitted to drive the broker — root plus whichever JTS
    service users currently exist on the host."""
    uids = {0}
    for name in BROKER_CLIENT_USERS:
        try:
            uids.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            continue
    return uids


# SO_PEERCRED is Linux-only (the broker runs on the Pi). Resolve it once;
# on a platform without it (a macOS dev box) peer-cred auth can't work, so the
# handler fail-closes rather than crashing the worker thread.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", None)


def _peer_cred(conn: socket.socket) -> tuple[int, int, int]:
    """Return ``(pid, uid, gid)`` of the connected peer via SO_PEERCRED."""
    if _SO_PEERCRED is None:
        raise OSError("SO_PEERCRED is unsupported on this platform")
    raw = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


# ---------------------------------------------------------------- server side


class _BrokerHandler(StreamRequestHandler):
    timeout = 5.0  # socket read timeout; a stalled client can't pin a thread

    def handle(self) -> None:  # noqa: D401
        try:
            pid, uid, _gid = _peer_cred(self.connection)
        except OSError as exc:
            self._reply({"ok": False, "error": f"peercred unavailable: {exc}"})
            return

        if uid not in _allowed_uids():
            log_event(
                logger, "restart_broker.denied", reason="peer_uid",
                peer_uid=uid, peer_pid=pid, level=logging.WARNING,
            )
            self._reply({"ok": False, "error": "unauthorized peer"})
            return

        try:
            raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        except OSError as exc:
            self._reply({"ok": False, "error": f"read failed: {exc}"})
            return
        if len(raw) > _MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "request too large"})
            return
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply({"ok": False, "error": "invalid json request"})
            return
        if not isinstance(req, dict):
            self._reply({"ok": False, "error": "request must be a JSON object"})
            return

        verb = req.get("verb")
        if verb not in ALLOWED_VERBS:
            log_event(
                logger, "restart_broker.denied", reason="verb",
                verb=repr(verb), peer_uid=uid, peer_pid=pid,
                level=logging.WARNING,
            )
            self._reply({"ok": False, "error": f"unknown verb {verb!r}"})
            return

        raw_units = req.get("units")
        if (
            not isinstance(raw_units, list)
            or not raw_units
            or not all(isinstance(u, str) for u in raw_units)
        ):
            self._reply(
                {"ok": False, "error": "units must be a non-empty list of strings"},
            )
            return
        units = [_normalize_unit(u) for u in raw_units]
        bad = [u for u in units if not _unit_allowed_for_verb(u, verb)]
        if bad:
            log_event(
                logger, "restart_broker.denied", reason="unit",
                verb=verb, units=",".join(bad), peer_uid=uid, peer_pid=pid,
                level=logging.WARNING,
            )
            self._reply(
                {"ok": False, "error": f"unit(s) not in allowlist: {','.join(bad)}"},
            )
            return

        reason = str(req.get("reason") or "")
        no_block = bool(req.get("no_block", True))
        exec_timeout = _clamp_exec_timeout(
            req.get("exec_timeout"),
            verb=verb,
            units=units,
            no_block=no_block,
        )
        log_event(
            logger, "restart_broker.request", verb=verb,
            units=",".join(units), reason=reason or "-",
            peer_uid=uid, peer_pid=pid, no_block=no_block,
            exec_timeout=exec_timeout,
        )
        try:
            rc, err, self_deferred = _run_systemctl_request(
                verb, units, no_block=no_block, exec_timeout=exec_timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log_event(
                logger, "restart_broker.exec_failed", verb=verb,
                units=",".join(units), error=str(exc), level=logging.WARNING,
            )
            self._reply({"ok": False, "error": f"systemctl invocation failed: {exc}"})
            return
        if self_deferred:
            log_event(
                logger, "restart_broker.self_restart_deferred",
                verb=verb, units=_SELF_UNIT, result="queued_unconfirmed",
            )
        queued_unconfirmed = self_deferred and rc is None
        if rc is not None and rc != 0:
            log_event(
                logger, "restart_broker.exec_nonzero", verb=verb,
                units=",".join(units), rc=rc, detail=err[:200],
                level=logging.WARNING,
            )
        self._reply({
            "ok": queued_unconfirmed or rc == 0,
            "action": verb,
            "units": units,
            "rc": rc,
            "stderr": err[:500] if rc is not None and rc != 0 else "",
            "self_deferred": self_deferred,
            "confirmed": not queued_unconfirmed,
            "status": "queued_unconfirmed" if queued_unconfirmed else "confirmed",
        })

    def _reply(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            pass  # client hung up; nothing actionable


class _BrokerServer(ThreadingUnixStreamServer):
    # One thread per connection (restarts are rare); threads die with the
    # daemon process, so they need not block jasper-control shutdown.
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.exception("restart broker handler crashed")


def start_broker(socket_path: str | None = None) -> _BrokerServer | None:
    """Bind the broker UDS (0660) and serve it on a daemon thread.

    Returns the server (so callers can ``shutdown()`` it) or ``None`` if
    binding failed. Binding failure is non-fatal and logged: jasper-control's
    other surfaces (volume, /state, supervisors) must keep running even if the
    broker can't come up — the wizards degrade to their existing fail-soft
    "restart didn't happen, logged" behaviour.

    ``socket_path`` defaults to :data:`DEFAULT_SOCKET_PATH` resolved at CALL
    time. Spelling it as a ``None`` sentinel rather than a default argument
    value is deliberate: a default is bound once at def time, so rebinding the
    module attribute would not reach it.
    """
    if socket_path is None:
        socket_path = DEFAULT_SOCKET_PATH
    parent = os.path.dirname(socket_path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = _BrokerServer(socket_path, _BrokerHandler)
        os.chmod(socket_path, 0o660)
    except OSError as exc:
        log_event(
            logger, "restart_broker.bind_failed", path=socket_path,
            error=str(exc), level=logging.WARNING,
        )
        return None
    threading.Thread(
        target=server.serve_forever, name="restart-broker", daemon=True,
    ).start()
    log_event(logger, "restart_broker.listening", path=socket_path)
    return server


# ---------------------------------------------------------------- client side


def request_restart(
    *units: str,
    verb: str = "restart",
    reason: str = "",
    no_block: bool = True,
    timeout: float = 5.0,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Ask the broker to run one closed-vocabulary systemctl action.

    Returns the broker's response dict (always carries ``ok``). Raises
    :class:`BrokerUnavailable` if the socket can't be reached or the broker
    doesn't answer with a parseable line — callers that want best-effort
    behaviour should use :func:`manage_units` instead.

    ``socket_path`` defaults to :data:`DEFAULT_SOCKET_PATH` resolved at CALL
    time. Spelling it as a ``None`` sentinel rather than a default argument
    value is deliberate: a default is bound once at def time, so rebinding the
    module attribute would not reach it — which silently sent every
    :func:`manage_units` call to the import-time path regardless.
    """
    if socket_path is None:
        socket_path = DEFAULT_SOCKET_PATH
    payload = json.dumps({
        "verb": verb,
        "units": [_normalize_unit(u) for u in units],
        "reason": reason,
        "no_block": no_block,
        # The broker bounds its systemctl call to this; we wait a little
        # longer on the socket (below) so its verdict always reaches us.
        "exec_timeout": timeout,
    })
    buf = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout + _CLIENT_SOCKET_MARGIN_SEC)
            sock.connect(socket_path)
            # The broker answers its two pre-auth rejections from the peer
            # credentials alone, so it can reply and close before this send
            # completes — and its reply is already queued on our side when
            # that happens. Raising here without looking would turn
            # "unauthorized peer" into "broken pipe" and leave the journal's
            # event=restart_broker.denied line as the only evidence of why
            # (measured: 6% of denials under 8-wide load on a Pi 5, 0% idle).
            # So remember the send failure and go read the answer first.
            #
            # Exactly the two "peer already closed on us" errors, not
            # OSError. A peer that has fully closed makes the read below
            # return at once, whereas a send that TIMED OUT would buy a
            # second full socket deadline on top of the first — settimeout is
            # per-operation — and the source-intent budget has no room for
            # it: 2703 s + a 5 s margin, doubled, is 5416 s against nginx's
            # derived proxy_read_timeout of 5600s.
            #
            # The honest cost of narrowing: a send failing ENOBUFS under
            # memory pressure could in principle have an answer waiting and
            # will not salvage it. That degrades to the pre-existing symptom
            # (a transport error instead of the denial), never to a new
            # failure, and it is not worth re-opening the timeout hole for.
            send_error: OSError | None = None
            try:
                sock.sendall((payload + "\n").encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError) as exc:
                send_error = exc
            try:
                while b"\n" not in buf and len(buf) < 8192:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                if send_error is None:
                    raise  # an ordinary read failure, reported as always
                buf = b""  # nothing legible arrived; the send error is the story
            if send_error is not None and not buf:
                raise send_error
    except (OSError, socket.timeout) as exc:
        raise BrokerUnavailable(str(exc)) from exc
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        raise BrokerUnavailable("empty broker response")
    try:
        resp = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerUnavailable(f"invalid broker response: {exc}") from exc
    if not isinstance(resp, dict):
        raise BrokerUnavailable("broker response was not an object")
    return resp


def _direct_systemctl(
    verb: str, units: list[str], *, no_block: bool, timeout: float,
) -> dict[str, Any]:
    """Run the action directly (root fallback). Mirrors the broker's result
    shape so callers can't tell which path executed."""
    if verb not in ALLOWED_VERBS:
        return {"ok": False, "error": f"unknown verb {verb!r}"}
    argv = _build_argv(verb, [_normalize_unit(u) for u in units], no_block=no_block)
    try:
        proc = subprocess.run(
            argv, check=False, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "action": verb,
        "units": [_normalize_unit(u) for u in units],
        "rc": proc.returncode,
        "stderr": (proc.stderr or "").strip()[:500] if proc.returncode != 0 else "",
    }


def manage_units(
    *units: str,
    verb: str = "restart",
    reason: str = "",
    no_block: bool = True,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Route a privileged unit action through the broker, best-effort.

    Never raises — returns a result dict with ``ok``. When the broker is
    unreachable AND this process is still root, falls back to a direct
    ``systemctl`` (logged loudly). Once the caller is a non-root service user
    the fallback cannot fire, so the broker is the only path.
    """
    if not units:
        return {"ok": True, "action": verb, "units": []}
    label = ",".join(units)
    try:
        resp = request_restart(
            *units, verb=verb, reason=reason, no_block=no_block, timeout=timeout,
        )
    except BrokerUnavailable as exc:
        if os.geteuid() == 0:
            log_event(
                logger, "restart_broker.fallback_direct", verb=verb,
                units=label, error=str(exc), level=logging.WARNING,
            )
            normalized_units = [_normalize_unit(unit) for unit in units]
            direct_timeout = _clamp_exec_timeout(
                timeout,
                verb=verb,
                units=normalized_units,
                no_block=no_block,
            )
            return _direct_systemctl(
                verb,
                normalized_units,
                no_block=no_block,
                timeout=direct_timeout,
            )
        log_event(
            logger, "restart_broker.unavailable", verb=verb, units=label,
            error=str(exc), level=logging.ERROR,
        )
        return {"ok": False, "error": f"restart broker unavailable: {exc}"}
    if not resp.get("ok"):
        log_event(
            logger, "restart_broker.client_error", verb=verb, units=label,
            error=str(resp.get("error") or f"rc={resp.get('rc')}"),
            level=logging.WARNING,
        )
    return resp


def reset_then_manage(
    *units: str,
    verb: str = "restart",
    reason: str = "",
    no_block: bool = True,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Clear the units' systemd failure/start-rate state, then run ``verb``.

    A deliberate operator or reconciler action gets a fresh, bounded retry
    budget this way, so a burst of them cannot walk the target into its
    ``StartLimitAction`` (#2175). The reset is best-effort in the strong
    sense: ``reset-failed`` is denied for START_ONLY units and routinely exits
    nonzero against an already-GC'd oneshot (#3237), so it is logged and
    discarded. Only the ACTION's result is returned — a failed reset must
    never be reported as, or turn into, a failed action.
    """
    reset = manage_units(
        *units,
        verb="reset-failed",
        reason=reason,
        no_block=False,
        timeout=_RESET_TIMEOUT_SEC,
    )
    if not reset.get("ok"):
        log_event(
            logger, "restart_broker.reset_failed_skipped", verb=verb,
            units=",".join(units), reason=reason,
            error=str(reset.get("error") or f"rc={reset.get('rc')}"),
            level=logging.WARNING,
        )
    return manage_units(
        *units, verb=verb, reason=reason, no_block=no_block, timeout=timeout,
    )
