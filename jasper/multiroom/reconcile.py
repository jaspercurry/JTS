"""Multiroom grouping reconciler — pure plan + thin systemctl entrypoint.

The reconciler is the single writer of the snapcast unit state. It reads
the wizard-owned GroupingConfig (see jasper.multiroom.config) and decides
which units should be running:

  - solo / grouping OFF        => neither snapserver nor snapclient runs.
  - grouping ON but INVALID    => neither runs (fail-safe: never bring up a
                                  broken bond; the doctor surfaces the error).
  - ON, valid, role=leader     => snapserver + snapclient (the leader hosts
                                  the stream AND plays its own channel).
  - ON, valid, role=follower   => snapclient only (consumes the leader's
                                  stream).

Mirrors the jasper-aec-reconcile / jasper-wifi-guardian shape: the
decision is a PURE, total function (`plan`) that is unit-tested with
synthetic configs; the systemd-facing `main()` does all the I/O
(load config, run systemctl) and is validated on hardware, not in pytest.

The argv builders (`snapserver_argv`, `snapclient_argv`) are likewise
pure — they translate a GroupingConfig into a command line so the same
logic can be tested without spawning snapcast.

There is no resident process here: jasper-grouping-reconcile.service is
Type=oneshot. It runs, applies the plan, and exits.
"""
from __future__ import annotations

import logging
import argparse
import subprocess
import sys
from dataclasses import dataclass

from .. import atomic_io
from .config import GroupingConfig, load_config

logger = logging.getLogger(__name__)


# ---------- Unit names (single source of truth) ----------

SNAPSERVER_UNIT = "jasper-snapserver.service"
SNAPCLIENT_UNIT = "jasper-snapclient.service"


# ---------- Snapcast wiring constants ----------

# The FIFO the fan-in chain writes the mixed stereo program into and
# snapserver reads from as its pipe source. Lives in snapserver's OWN
# per-unit runtime dir (/run/jasper-snapserver/, RuntimeDirectory=
# jasper-snapserver) — NOT the shared /run/jasper, which jasper-voice
# owns for voice.sock + peering.sock. A unit's RuntimeDirectory is
# reaped when it stops; sharing /run/jasper would let snapserver
# stopping destroy the voice/peering sockets. tmpfs-backed, recreated
# each boot.
SNAPFIFO = "/run/jasper-snapserver/snapfifo"

# Reconciler-owned runtime env file holding the DERIVED snapcast args
# (the argv after argv[0], space-joined). The snapserver/snapclient
# units pick it up via a third `EnvironmentFile=` layered AFTER
# grouping.env, so the derived args override the bare wizard intent.
#
# Deliberately NOT a unit RuntimeDirectory: a unit's RuntimeDirectory is
# reaped the moment that unit stops, which would erase the args a sibling
# unit (or a restart) still needs. This dir is owned by the reconciler
# and persists for the boot. tmpfs-backed (/run), recreated each boot —
# the reconciler runs at boot and on every wizard change, so it is always
# rewritten before the units start.
ARGS_DIR = "/run/jasper-grouping"
ARGS_FILE = ARGS_DIR + "/snapcast-args.env"

# The two derived keys the units read. Mirrors the aec-reconcile
# derived-env contract (one line per key, empty-string to clear).
_SERVER_ARGS_KEY = "JASPER_SNAPSERVER_ARGS"
_CLIENT_ARGS_KEY = "JASPER_SNAPCLIENT_ARGS"

# ---------- outputd snapfifo tap (leader only) ----------

# The final-output owner. A grouping LEADER's outputd taps the post-clamp
# program into SNAPFIFO; we activate that tap by writing its env file and
# try-restarting outputd. This is the ONE non-grouping unit the reconciler
# touches (mirrors jasper-aec-reconcile restarting jasper-voice on a mic
# change) — and outputd has StartLimitAction=reboot, so touching it only on
# an actual change (never a steady-state reconcile) is load-bearing.
OUTPUTD_UNIT = "jasper-outputd.service"

# Reconciler-owned env file that the jasper-outputd unit layers in via an
# OPTIONAL EnvironmentFile=. Carries just the tap path. Absent / empty =>
# outputd does not tap (a solo speaker / every follower). Lives in the
# reconciler's /run dir (tmpfs, recreated each boot), like the snap args.
OUTPUTD_SNAPFIFO_ENV_FILE = ARGS_DIR + "/outputd-snapfifo.env"
_OUTPUTD_SNAPFIFO_KEY = "JASPER_OUTPUTD_SNAPFIFO_PATH"


# ---------- Plan types ----------

@dataclass(frozen=True)
class UnitIntent:
    """A desired terminal state for one systemd unit.

    `desired` is one of {"start", "stop"}; `reason` is a short
    human-readable explanation for the log line.
    """
    unit: str
    desired: str  # "start" | "stop"
    reason: str


@dataclass(frozen=True)
class ReconcilePlan:
    """The full set of unit intents plus a one-line summary.

    `intents` is ordered stops-before-starts so a role flip tears the
    old shape down before bringing the new one up.
    """
    intents: tuple[UnitIntent, ...]
    summary: str


# ---------- The pure decision function ----------

def plan(cfg: GroupingConfig) -> ReconcilePlan:
    """Decide the desired snapcast unit state from a GroupingConfig.

    PURE and total: no I/O, no subprocess, no clock. Same input always
    yields the same plan. Intents are ordered stops-before-starts.

    Cases:
      - disabled                  => stop both (solo).
      - enabled but cfg.error set => stop both (fail-safe: never run a
                                     broken bond).
      - enabled, valid, leader    => start snapserver + start snapclient.
      - enabled, valid, follower  => stop snapserver + start snapclient.
    """
    if not cfg.enabled:
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "stop", "grouping off"),
                UnitIntent(SNAPCLIENT_UNIT, "stop", "grouping off"),
            ),
            summary="grouping off (solo)",
        )

    if cfg.error is not None:
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "stop", "config invalid"),
                UnitIntent(SNAPCLIENT_UNIT, "stop", "config invalid"),
            ),
            summary=(
                f"grouping enabled but INVALID: {cfg.error} — not starting"
            ),
        )

    if cfg.role == "leader":
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "start", "leader hosts stream"),
                UnitIntent(SNAPCLIENT_UNIT, "start", "leader plays its channel"),
            ),
            summary=f"grouping leader (bond {cfg.bond_id}, channel {cfg.channel})",
        )

    # role == "follower" (validated: a valid enabled config is one of the
    # two ALLOWED_ROLES, and leader is handled above).
    return ReconcilePlan(
        intents=(
            UnitIntent(SNAPSERVER_UNIT, "stop", "follower runs no server"),
            UnitIntent(SNAPCLIENT_UNIT, "start", "follower consumes stream"),
        ),
        summary=(
            f"grouping follower (bond {cfg.bond_id}, channel {cfg.channel}, "
            f"leader {cfg.leader_addr})"
        ),
    )


# ---------- Pure argv builders ----------

def snapserver_argv(cfg: GroupingConfig) -> list[str]:
    """Build the snapserver command line from a GroupingConfig.

    PURE: a deterministic function of `cfg`. snapserver reads the mixed
    program from the SNAPFIFO pipe source and streams it with the
    configured codec and a buffer derived from cfg.buffer_ms.
    """
    source = (
        f"pipe://{SNAPFIFO}?name=jts"
        f"&codec={cfg.codec}"
        f"&buffer_ms={cfg.buffer_ms}"
    )
    return [
        "snapserver",
        "--stream.source",
        source,
    ]


def snapclient_argv(cfg: GroupingConfig) -> list[str]:
    """Build the snapclient command line from a GroupingConfig.

    PURE: a deterministic function of `cfg`. The host is the loopback
    when this speaker is the leader (it runs its own server), otherwise
    the leader's address. The playout latency tracks cfg.buffer_ms.

    Channel selection (which of L/R/sub this client plays) is a later
    CamillaDSP concern and is intentionally NOT decided here.
    """
    # cfg.leader_addr is passed VERBATIM to snapclient --host. The bond
    # wizard now mints it as a STABLE mDNS .local handle (the leader's
    # JASPER_HOSTNAME, e.g. "jts3.local"), not a raw DHCP IP, so a follower
    # survives the leader changing IP: snapclient re-resolves the name via
    # mDNS at connect/reconnect time. A literal IPv4 is still accepted (and
    # works) — see config.GroupingConfig.leader_addr — but the .local handle
    # is what the wizard writes, so no reconcile change was needed for it.
    host = "127.0.0.1" if cfg.role == "leader" else cfg.leader_addr
    return [
        "snapclient",
        "--host",
        host,
        "--latency",
        str(cfg.buffer_ms),
    ]


def _assemble_args(cfg: GroupingConfig) -> dict[str, str]:
    """Derive the {key: value} the units read, from a GroupingConfig.

    PURE: a deterministic function of `cfg`. Returns the two derived
    keys (``JASPER_SNAPSERVER_ARGS`` / ``JASPER_SNAPCLIENT_ARGS``) whose
    values are the argv AFTER argv[0] (the binary name, already in the
    unit's ExecStart), space-joined. Both keys are always present; a key
    is the EMPTY STRING when its unit should NOT carry derived args:

      - disabled / cfg.error  => both empty (the units won't start in
                                 these states, but clearing the derived
                                 args means a started unit can never pick
                                 up stale values — mirrors aec-reconcile's
                                 disable-clears-stale idiom).
      - enabled, valid leader => server + client set.
      - enabled, valid follower => server EMPTY, client set
        (a follower runs no server).

    Word-splitting safety: snapcast args are space-free (a pipe URL and
    host/latency). We assert that here — if a builder ever emits a
    space-containing arg, the units' unquoted ``$JASPER_SNAP*_ARGS``
    word-splitting would mangle it, and that is a separate quoting task.
    """
    if not cfg.enabled or cfg.error is not None:
        return {_SERVER_ARGS_KEY: "", _CLIENT_ARGS_KEY: ""}

    # argv[0] is the binary name (already in the unit's ExecStart); the
    # units invoke `/usr/bin/snap* $ARGS`, so persist only argv[1:].
    server = "" if cfg.role != "leader" else _join_args(snapserver_argv(cfg))
    client = _join_args(snapclient_argv(cfg))
    return {_SERVER_ARGS_KEY: server, _CLIENT_ARGS_KEY: client}


def _join_args(argv: list[str]) -> str:
    """Space-join argv[1:] (drop the binary name), asserting no element
    contains whitespace — the units word-split the unquoted env var."""
    tail = argv[1:]
    for a in tail:
        assert a == a.strip() and " " not in a and "\t" not in a, (
            f"snapcast arg {a!r} contains whitespace; unquoted "
            "$JASPER_SNAP*_ARGS word-splitting would mangle it"
        )
    return " ".join(tail)


def desired_snapfifo_path(cfg: GroupingConfig) -> str:
    """The FIFO path jasper-outputd should tap into, or "" when it must NOT
    tap. PURE.

    Only a VALID LEADER taps: it hosts the synchronised stream, so its
    outputd copies the post-clamp program into the snapserver FIFO. A
    follower *consumes* the stream (no tap); a solo / off / invalid config
    does not stream at all. The path is the reconciler's canonical
    ``SNAPFIFO`` (in snapserver's RuntimeDirectory).
    """
    if cfg.enabled and cfg.error is None and cfg.role == "leader":
        return SNAPFIFO
    return ""


def outputd_tap_action(desired: str, current: str) -> bool:
    """PURE: does outputd's tap env need (re)writing + a try-restart?

    True ONLY when the desired tap differs from what outputd currently has.
    The final-output owner is touched solely on an actual leader transition
    — never on a steady-state reconcile, which would blip audio and, on
    repeat, trip outputd's ``StartLimitAction=reboot``. This change-gate is
    the safety property; ``_reconcile_outputd_tap`` is a thin wrapper.
    """
    return desired != current


# ============================================================
# I/O entrypoint — NOT unit-tested (validated on hardware).
# Everything above is pure; everything below does real systemctl
# calls. Keep that boundary crisp.
# ============================================================

def _apply(plan_: ReconcilePlan) -> int:
    """Apply a plan via systemctl. Returns a process exit code.

    Each intent is `systemctl <start|stop> <unit>`. A failure on one
    intent is logged and surfaced in the exit code but does not abort
    the rest of the plan — a half-applied bond is worse than a
    best-effort one.
    """
    rc = 0
    for it in plan_.intents:
        try:
            subprocess.run(
                ["systemctl", it.desired, it.unit],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info(
                "event=multiroom.reconcile.unit unit=%s desired=%s reason=%s",
                it.unit, it.desired, it.reason,
            )
        except FileNotFoundError:
            logger.error(
                "event=multiroom.reconcile.unit_failed unit=%s desired=%s "
                "error=systemctl_not_found",
                it.unit, it.desired,
            )
            rc = 1
        except subprocess.CalledProcessError as e:
            logger.error(
                "event=multiroom.reconcile.unit_failed unit=%s desired=%s "
                "rc=%d stderr=%s",
                it.unit, it.desired, e.returncode,
                (e.stderr or "").strip(),
            )
            rc = 1
    return rc


def _write_args_file(keys: dict[str, str], *, path: str = ARGS_FILE) -> bool:
    """Atomically write the derived snapcast args to ``path``. Fail-soft.

    Delegates the atomic tempfile+rename mechanics to
    ``atomic_io.atomic_write_text`` (makedirs the parent, write a temp file
    in the SAME dir, ``chmod 0644`` BEFORE the rename so the published file
    never has a wider permission window, then ``os.replace``). One
    ``KEY=value`` line per key, order preserved.

    Returns True on success, False on any failure. NEVER raises — a lost
    args write must not crash the reconcile path (the plan still
    start/stops units; the units would fall back to their own defaults).
    The file carries no secrets, so mode 0644 (matches grouping.env).
    """
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        logger.warning(
            "event=multiroom.reconcile.args_failed path=%s error=%s",
            path, e,
        )
        return False
    return True


def _read_outputd_snapfifo_path(path: str = OUTPUTD_SNAPFIFO_ENV_FILE) -> str:
    """Read outputd's CURRENT tap path from its reconciler-owned env file.

    Total: a missing / unreadable / empty file (or one without the key)
    resolves to "" — "outputd is not tapping". Used by
    ``_reconcile_outputd_tap`` for change-detection so the final-output
    owner is touched only on an actual transition.
    """
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if line.startswith(_OUTPUTD_SNAPFIFO_KEY + "="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(
            "event=multiroom.reconcile.outputd_tap_read_failed path=%s error=%s",
            path, e,
        )
    return ""


def _write_outputd_snapfifo_env(
    desired: str, *, path: str = OUTPUTD_SNAPFIFO_ENV_FILE,
) -> bool:
    """Atomically write outputd's tap env file. Fail-soft (never raises).

    ``desired`` non-empty => one ``JASPER_OUTPUTD_SNAPFIFO_PATH=<path>``
    line. ``desired`` empty (not a leader) => an EMPTY file, so a restarted
    outputd has NO tap key set. Same atomic tempfile+rename + 0644 mode as
    ``_write_args_file`` (delegated to ``atomic_io.atomic_write_text``);
    carries no secrets.
    """
    body = f"{_OUTPUTD_SNAPFIFO_KEY}={desired}\n" if desired else ""
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        logger.warning(
            "event=multiroom.reconcile.outputd_tap_write_failed path=%s error=%s",
            path, e,
        )
        return False
    return True


def _try_restart(unit: str) -> int:
    """``systemctl try-restart <unit>`` — restart ONLY if already active.

    ``try-restart`` (not ``restart``) is deliberate: at boot the unit reads
    the freshly-written env on its own start, so we must NOT force-start it
    from here (which would also couple outputd's boot to this optional
    reconciler). Returns 0 on success, 1 on failure.
    """
    try:
        subprocess.run(
            ["systemctl", "try-restart", unit],
            check=True, capture_output=True, text=True,
        )
        return 0
    except FileNotFoundError:
        logger.error(
            "event=multiroom.reconcile.outputd_tap_restart_failed unit=%s "
            "error=systemctl_not_found", unit,
        )
        return 1
    except subprocess.CalledProcessError as e:
        logger.error(
            "event=multiroom.reconcile.outputd_tap_restart_failed unit=%s "
            "rc=%d stderr=%s", unit, e.returncode, (e.stderr or "").strip(),
        )
        return 1


def _reconcile_outputd_tap(cfg: GroupingConfig) -> int:
    """Reconcile jasper-outputd's snapfifo tap to the grouping role.

    Writes the tap env + try-restarts outputd ONLY on an actual change
    (``outputd_tap_action``). Separate from the snap-unit plan: outputd is
    always running, so it must not be touched on a steady-state reconcile.
    Returns 0 ok, 1 on a write/restart failure.
    """
    desired = desired_snapfifo_path(cfg)
    current = _read_outputd_snapfifo_path()
    if not outputd_tap_action(desired, current):
        return 0  # steady state — never touch the final-output owner
    wrote = _write_outputd_snapfifo_env(desired)
    logger.info(
        "event=multiroom.reconcile.outputd_tap from=%r to=%r wrote=%s",
        current, desired, wrote,
    )
    if not wrote:
        # The env write failed (already logged). Restarting outputd now
        # would blip the final-output owner for nothing — it would re-read
        # the same stale env. Surface the failure (rc=1) instead of acting.
        return 1
    return _try_restart(OUTPUTD_UNIT)


def main(argv: list[str] | None = None) -> int:
    """systemd ExecStart entrypoint for jasper-grouping-reconcile.service.

    Loads the wizard-owned config fresh, computes the pure plan, ASSEMBLES
    and PERSISTS the derived snapcast args, logs the decision, and applies
    the plan via systemctl. Returns a process exit code.

    Order matters: the args file is written BEFORE `_apply`, so a unit
    that `_apply` starts reads fresh args (its `EnvironmentFile=` is read
    at unit start). The args persistence mirrors jasper-aec-reconcile's
    derived-env pattern — assemble the concrete `JASPER_SNAPSERVER_ARGS`
    / `JASPER_SNAPCLIENT_ARGS` from the config (argv after the binary
    name, space-joined), atomically write them to a reconciler-owned
    runtime env file (``ARGS_FILE``) the units layer on top of
    grouping.env, and clear (empty-string, not delete the key) the args
    when a producer should not run — so a started unit can never pick up
    stale args.

    SCOPE: arg-application is now DONE. What is still pending is the
    snapfifo PRODUCER (the P1.3 jasper-outputd reference consumer that
    feeds the mixed program into ``SNAPFIFO``). Until that lands,
    snapserver reads an EMPTY fifo: this increment fully CONFIGURES a
    bond (a started snapserver reads the fifo with the right codec/buffer,
    a started snapclient targets the right host/latency) but does NOT yet
    make audio FLOW. Off-by-default means nothing starts these units until
    a bond is configured via the wizard AND the P1.3 producer ships.

    `--reason` is a free-text trigger source (systemd / wizard / manual)
    echoed into the structured log for correlation, mirroring
    jasper-aec-reconcile. Unknown args are ignored so a future caller
    adding a flag can't crash the reconcile path.
    """
    parser = argparse.ArgumentParser(prog="jasper.multiroom.reconcile")
    parser.add_argument("--reason", default="manual")
    args, _unknown = parser.parse_known_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    decision = plan(cfg)
    logger.info(
        "event=multiroom.reconcile.start reason=%s enabled=%s role=%s error=%s summary=%r",
        args.reason, cfg.enabled, cfg.role or "(none)", cfg.error or "(none)", decision.summary,
    )

    # Derive + persist the snapcast args BEFORE applying the plan, so any
    # unit `_apply` starts reads fresh args from its EnvironmentFile=. The
    # write is fail-soft: a failure here is logged and the reconcile still
    # start/stops units (the units fall back to their own defaults).
    derived = _assemble_args(cfg)
    wrote = _write_args_file(derived)
    set_keys = [k for k, v in derived.items() if v]
    logger.info(
        "event=multiroom.reconcile.args path=%s ok=%s set=%s",
        ARGS_FILE, wrote, ",".join(set_keys) or "(none)",
    )

    rc = _apply(decision)
    # Reconcile outputd's snapfifo tap to the role (a valid leader taps;
    # everyone else does not). Separate from the snap-unit plan above:
    # outputd is always running, so this touches it ONLY on an actual leader
    # transition (change-detected) — never a steady-state reconcile.
    rc |= _reconcile_outputd_tap(cfg)
    logger.info("event=multiroom.reconcile.done rc=%d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
