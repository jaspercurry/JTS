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
    host = "127.0.0.1" if cfg.role == "leader" else cfg.leader_addr
    return [
        "snapclient",
        "--host",
        host,
        "--latency",
        str(cfg.buffer_ms),
    ]


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


def main(argv: list[str] | None = None) -> int:
    """systemd ExecStart entrypoint for jasper-grouping-reconcile.service.

    Loads the wizard-owned config fresh, computes the pure plan, logs the
    decision, and applies it via systemctl. Returns a process exit code.

    SCOPE (P0/P1 boundary): this entrypoint only START/STOPs units. It
    does NOT yet assemble the snapserver/snapclient argv (the pure
    ``snapserver_argv`` / ``snapclient_argv`` builders below are spec'd
    and unit-tested but not yet wired) nor persist them as
    ``JASPER_SNAPSERVER_ARGS`` / ``JASPER_SNAPCLIENT_ARGS`` for the
    units to read. Arg application + the live snapcast lifecycle land in
    P1, validated on hardware (the codec/buffer come from the §8 spike).
    Off-by-default means nothing triggers this path yet.

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
    rc = _apply(decision)
    logger.info("event=multiroom.reconcile.done rc=%d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
