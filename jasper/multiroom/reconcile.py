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
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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

# ---------- the leader's music producer (Increment 5: CamillaDSP) ----------
#
# The leader's CamillaDSP feeds the snapserver pipe (post-correction,
# post-master_gain — the stream inherits the volume + safety ceiling),
# applied by this reconciler via jasper.multiroom.leader_config (the
# bonded emit + glitch-free config swap, reusing the wizards' shared
# apply engine). The earlier outputd-as-producer machinery was removed
# 2026-06-11 — see HANDOFF-multiroom.md §2 "Canonical signal flow" +
# "Stranded by this design". Producer liveness for runtime health reads
# the ACTIVE CamillaDSP config (the daemon-adjacent truth: camilla's own
# statefile names it, and the doctor's `leader pipe` check scans it) —
# never a Python mirror of env intent, the lesson the removed
# SNAPFIFO_PRODUCER_WIRED flag existed to patch.

# ---------- the member round-trip content lane (Increment 5) ----------
#
# Raw-PCM FIFO snapclient writes the buffered round-trip into
# (`--player file:filename=...`, option string verified on snapclient
# 0.31.0), read by outputd's `dac_content` lane (Increment 3) — never
# snd-aloop, so snapclient's snd_pcm_delay can't lie (inv-2). Lives in
# the reconciler-owned ARGS_DIR (tmpfs; the reconciler mkfifos it before
# starting snapclient on every reconcile/boot).
MEMBER_CONTENT_FIFO = ARGS_DIR + "/member-content.fifo"
# Reconciler-owned PERSISTENT env file the jasper-outputd unit layers
# after jasper.env (EnvironmentFile=-). Persistent (NOT /run) so a
# bonded speaker boots with the lane already configured — no extra
# outputd restart at boot; mirrors the aec_mode.env pattern. The two
# derived keys mirror Increment 3's config contract; both are written
# as empty strings when this speaker is not an active member, so a
# stale file can never leave the lane half-configured.
OUTPUTD_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-outputd.env"
OUTPUTD_DAC_CONTENT_FIFO_ENV = "JASPER_OUTPUTD_DAC_CONTENT_FIFO"
OUTPUTD_DAC_CONTENT_CHANNEL_ENV = "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL"
# Pinned to "direct" while bonded (writer/validator coherence — see
# outputd_grouping_env): the lane fail-closes on any other bridge mode,
# and this file is the last env layer, so the pin wins over lab retunes.
OUTPUTD_CONTENT_BRIDGE_ENV = "JASPER_OUTPUTD_CONTENT_BRIDGE"
OUTPUTD_UNIT = "jasper-outputd.service"

# The snapserver stream id — ONE definition: the argv builder names the
# pipe source with it, the reconciler's binding pin re-binds persisted
# groups to it, and the leader's runtime health checks clients against
# it. snapcast PERSISTS group->stream assignments in server.json, so a
# stale binding (e.g. the distro-snapserver era's "default") silently
# mutes a bond behind green health — the 2026-06-11 bring-up incident.
SNAP_STREAM_ID = "jts"
# (The former LEADER_CONTENT_LANE_GATE staging env was retired when this
# lane went live: the reconciler's role wiring IS the gate now — the
# lane activates exactly when a valid bond is configured, and the
# off/solo path writes empty env = byte-identical outputd behavior.)


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
    # sampleformat is PINNED (codify, don't rely on snapserver defaults):
    # the whole chain is 48 kHz / S16 / stereo — CamillaDSP's File sink
    # writes it, and outputd's dac_content reader assumes it. mode=create
    # is snapcast's default for a pipe source but is pinned for the same
    # reason: snapserver owning FIFO creation is load-bearing (it opens
    # the read end first, so CamillaDSP's write-open cannot block).
    source = (
        f"pipe://{SNAPFIFO}?name={SNAP_STREAM_ID}"
        f"&mode=create"
        f"&sampleformat=48000:16:2"
        f"&codec={cfg.codec}"
        f"&buffer_ms={cfg.buffer_ms}"
    )
    return [
        "snapserver",
        "--stream.source",
        source,
    ]


def snapclient_argv(
    cfg: GroupingConfig, *, player_fifo: str | None = None,
) -> list[str]:
    """Build the snapclient command line from a GroupingConfig.

    PURE: a deterministic function of `cfg` (+ the optional `player_fifo`).
    The host is the loopback when this speaker is the leader (it runs its own
    server), otherwise the leader's address. The playout latency tracks
    cfg.buffer_ms.

    Channel selection (which of L/R/sub this client plays) is a later
    CamillaDSP concern and is intentionally NOT decided here.

    ``player_fifo`` (inv-2 leader content lane — STAGED, see HANDOFF §2 "inv-2
    realization"): when set, snapclient writes raw PCM to that FIFO via its
    ``file`` player instead of a default ALSA sink, so the buffered round-trip
    feeds outputd's ``dac_content`` lane (Increment 3) rather than snd-aloop
    (which would trip the ``snd_pcm_delay``-lies trap — inv-2) — and rather
    than fighting outputd for the raw DAC (the observed ``Device or resource
    busy`` failure of the pre-Increment-5 bond). ``None`` leaves the command
    BYTE-FOR-BYTE unchanged. The ``file:filename=`` option string was verified
    against snapclient 0.31.0 on jts3 (``--player file:?``).
    """
    # cfg.leader_addr is passed VERBATIM to snapclient --host. The bond
    # wizard now mints it as a STABLE mDNS .local handle (the leader's
    # JASPER_HOSTNAME, e.g. "jts3.local"), not a raw DHCP IP, so a follower
    # survives the leader changing IP: snapclient re-resolves the name via
    # mDNS at connect/reconnect time. A literal IPv4 is still accepted (and
    # works) — see config.GroupingConfig.leader_addr — but the .local handle
    # is what the wizard writes, so no reconcile change was needed for it.
    host = "127.0.0.1" if cfg.role == "leader" else cfg.leader_addr
    argv = [
        "snapclient",
        "--host",
        host,
        "--latency",
        str(cfg.buffer_ms),
    ]
    if player_fifo:
        argv += ["--player", f"file:filename={player_fifo}"]
    return argv


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
    # Every active member's snapclient writes the round-trip FIFO (the
    # `file` player) — never an ALSA sink, which would fight outputd for
    # the DAC (the observed `Device or resource busy` failure of the
    # pre-Increment-5 bond). outputd reads the FIFO via its dac_content
    # lane (Increment 3) and picks this member's channel there.
    server = "" if cfg.role != "leader" else _join_args(snapserver_argv(cfg))
    client = _join_args(snapclient_argv(cfg, player_fifo=MEMBER_CONTENT_FIFO))
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


def outputd_grouping_env(cfg: GroupingConfig) -> dict[str, str]:
    """The outputd round-trip lane env derived from a GroupingConfig. PURE.

    An ACTIVE member (enabled + valid, either role) plays the
    round-tripped stream: outputd reads ``MEMBER_CONTENT_FIFO`` and
    picks this speaker's channel (Increment 3's ``ChannelPick``; the
    channel-split vocabulary). Everyone else gets EMPTY strings — which
    outputd's ``env_optional`` reads as unset, i.e. the byte-identical
    solo loop — so a stale file can never half-configure the lane
    (mirrors ``_assemble_args``'s disable-clears-stale idiom).

    WRITER/VALIDATOR COHERENCE (the jts3 2026-06-11 boot-loop incident):
    outputd FAIL-CLOSES on ``DAC_CONTENT_FIFO`` + ``CONTENT_BRIDGE=
    rate_match`` — and systemd composes outputd's env from LAYERS, so a
    lab retune in ``/var/lib/jasper/outputd.env`` (the rate_match soak)
    plus this file's FIFO crashed outputd into StartLimitAction=reboot
    (contained by the T5.1 boot-loop guard). The writer must never emit
    a combination the validator rejects ACROSS ALL LAYERS, so while
    bonded this file — deliberately the LAST EnvironmentFile= layer —
    also pins ``CONTENT_BRIDGE=direct``, the lane's hard requirement.
    Solo OMITS the key entirely (never an empty value: outputd's
    ``env_str`` treats a SET-but-empty bridge mode as invalid and bails),
    so a solo speaker falls back to the underlying layers and the lab's
    rate_match soak resumes. Bonding and the soak coexist; neither can
    crash outputd.
    """
    if cfg.enabled and cfg.error is None:
        return {
            OUTPUTD_DAC_CONTENT_FIFO_ENV: MEMBER_CONTENT_FIFO,
            OUTPUTD_DAC_CONTENT_CHANNEL_ENV: cfg.channel or "stereo",
            OUTPUTD_CONTENT_BRIDGE_ENV: "direct",
        }
    return {
        OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
    }


def desired_snapfifo_path(cfg: GroupingConfig) -> str:
    """The FIFO path the leader's MUSIC PRODUCER must feed, or "" when this
    role needs no producer. PURE.

    Only a VALID LEADER hosts the synchronised stream, so only a leader
    needs a producer feeding the snapserver FIFO. A follower *consumes*
    the stream; a solo / off / invalid config does not stream at all. The
    path is the reconciler's canonical ``SNAPFIFO`` (in snapserver's
    RuntimeDirectory). The producer is the leader's CamillaDSP (Increment
    5 — applied by this reconciler via jasper.multiroom.leader_config);
    this predicate drives the runtime-health derive ("a leader whose
    active config does not write the pipe is degraded").
    """
    if cfg.enabled and cfg.error is None and cfg.role == "leader":
        return SNAPFIFO
    return ""


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


def _write_outputd_env(
    keys: dict[str, str], *, path: str = OUTPUTD_GROUPING_ENV_FILE,
) -> tuple[bool, bool]:
    """Write the outputd round-trip lane env iff it changed.

    Returns ``(changed, ok)``. Compare-before-write keeps the common
    no-change reconcile from restarting outputd (the caller restarts the
    unit only on ``changed and ok`` — EnvironmentFile= is read at unit
    start, so a content change without a restart would silently not
    apply). Fail-soft like ``_write_args_file``; carries no secrets
    (mode 0644)."""
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        old = Path(path).read_text()
    except OSError:
        old = None
    if old == body:
        return (False, True)
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        logger.warning(
            "event=multiroom.reconcile.outputd_env_failed path=%s error=%s",
            path, e,
        )
        return (True, False)
    return (True, True)


def _ensure_member_fifo(*, path: str = MEMBER_CONTENT_FIFO) -> bool:
    """Make sure the member round-trip FIFO exists at ``path``. Fail-soft.

    tmpfs-backed (ARGS_DIR), so it must be recreated each boot — the
    reconciler runs at boot and before starting snapclient, which writes
    it via the `file` player. A non-FIFO squatter (a stray regular file)
    is replaced: snapclient's file player would happily write a growing
    regular file (a disk-filling silent failure), and outputd's
    dac_content open would still succeed, masking it."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        st = None
        try:
            st = os.stat(path)
        except FileNotFoundError:
            pass
        if st is not None and not stat.S_ISFIFO(st.st_mode):
            logger.warning(
                "event=multiroom.reconcile.fifo_replaced path=%s "
                "detail=non-FIFO squatter removed", path,
            )
            os.unlink(path)
            st = None
        if st is None:
            os.mkfifo(path, 0o600)
    except OSError as e:
        logger.warning(
            "event=multiroom.reconcile.fifo_failed path=%s error=%s", path, e,
        )
        return False
    return True


def _restart_outputd() -> bool:
    """Restart jasper-outputd so it re-reads the grouping env. Fail-soft
    (a failure is logged + reflected in the exit code by the caller; the
    doctor's channel-pick drift check surfaces a lane left unwired)."""
    try:
        subprocess.run(
            ["systemctl", "restart", OUTPUTD_UNIT],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        logger.error(
            "event=multiroom.reconcile.outputd_restart_failed error=%s "
            "stderr=%s", e, stderr.strip(),
        )
        return False
    logger.info(
        "event=multiroom.reconcile.outputd_restarted reason=grouping_env_changed",
    )
    return True


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

    SCOPE (Increment 5): the FULL bonded music dataplane. Beyond the
    snapcast args this also (a) writes the outputd round-trip lane env
    (FIFO + channel pick) and restarts outputd only on change, (b)
    creates the member content FIFO, and (c) drives the CamillaDSP
    config swap through jasper.multiroom.leader_config — the bonded
    pipe config on an active leader, the solo restore otherwise. What
    does NOT flow through here is per-member TTS (the PR-2 outputd TTS
    mixer — until then TTS rides the synced stream, surfaced by the
    doctor's standing `TTS interim` warn).

    `--reason` is a free-text trigger source (systemd / wizard / manual)
    echoed into the structured log for correlation, mirroring
    jasper-aec-reconcile. Unknown args are ignored so a future caller
    adding a flag can't crash the reconcile path.

    ORDER (load-bearing — see HANDOFF-multiroom.md §2):

      1. Derived files (snapcast args + outputd lane env) + the member
         FIFO — before any unit work, so everything a started unit
         reads is fresh.
      2. CamillaDSP solo RESTORE when this speaker is not an active
         leader (a no-op on the common solo reconcile) — BEFORE units
         stop, so the pipe's writer leaves before its reader.
      3. outputd restart, only when the lane env CHANGED.
      4. The unit plan (stops before starts, as always).
      5. CamillaDSP bonded APPLY when this speaker is an active leader —
         LAST, after snapserver started, so the pipe's reader exists
         before CamillaDSP's File sink opens it for write (a FIFO
         write-open blocks until a reader exists).

    Camilla apply/restore failures are caught and logged
    (event=multiroom.reconcile.camilla_failed) — the reconcile still
    manages units, and the doctor's `leader pipe` / runtime-health
    surfaces carry the unapplied state. They flip the exit code, so the
    oneshot unit shows failed.
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
    active = cfg.enabled and cfg.error is None
    active_leader = active and cfg.role == "leader"
    logger.info(
        "event=multiroom.reconcile.start reason=%s enabled=%s role=%s error=%s summary=%r",
        args.reason, cfg.enabled, cfg.role or "(none)", cfg.error or "(none)", decision.summary,
    )
    rc = 0

    # 1. Derived files + FIFO — before any unit work.
    derived = _assemble_args(cfg)
    wrote = _write_args_file(derived)
    set_keys = [k for k, v in derived.items() if v]
    logger.info(
        "event=multiroom.reconcile.args path=%s ok=%s set=%s",
        ARGS_FILE, wrote, ",".join(set_keys) or "(none)",
    )
    # Paths passed explicitly (module globals read at CALL time) so the
    # test harness can redirect them; a def-time default would pin the
    # production path.
    outputd_env = outputd_grouping_env(cfg)
    env_changed, env_ok = _write_outputd_env(
        outputd_env, path=OUTPUTD_GROUPING_ENV_FILE,
    )
    logger.info(
        "event=multiroom.reconcile.outputd_env path=%s changed=%s ok=%s "
        "fifo=%s channel=%s",
        OUTPUTD_GROUPING_ENV_FILE, env_changed, env_ok,
        outputd_env[OUTPUTD_DAC_CONTENT_FIFO_ENV] or "(cleared)",
        outputd_env[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] or "(cleared)",
    )
    if not env_ok:
        rc = 1
    if active and not _ensure_member_fifo(path=MEMBER_CONTENT_FIFO):
        rc = 1

    # 2. Solo restore when not an active leader (no-op when already solo).
    if not active_leader:
        try:
            from .leader_config import restore_solo_config_sync

            restored = restore_solo_config_sync()
            if restored:
                logger.info(
                    "event=multiroom.reconcile.camilla result=solo_restored path=%s",
                    restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            logger.error(
                "event=multiroom.reconcile.camilla_failed action=restore error=%s", e,
            )
            rc = 1

    # 3. outputd picks up the lane env only at unit start.
    if env_changed and env_ok and not _restart_outputd():
        rc = 1

    # 4. The unit plan (stops before starts).
    rc = max(rc, _apply(decision))

    # 5. Bonded apply LAST (snapserver is up → the pipe has its reader).
    if active_leader:
        try:
            from .leader_config import apply_bonded_leader_config_sync

            applied = apply_bonded_leader_config_sync(cfg)
            logger.info(
                "event=multiroom.reconcile.camilla result=bonded path=%s", applied,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            logger.error(
                "event=multiroom.reconcile.camilla_failed action=bonded_apply error=%s", e,
            )
            rc = 1

        # 6. The stream-binding pin (after camilla apply so snapserver
        # has had its longest warm-up): re-bind every PERSISTED snapcast
        # group to our stream. A stale server.json binding (the distro-
        # snapserver era's "default") silently mutes the whole bond
        # behind green health — the 2026-06-11 bring-up incident. The
        # ensure retries internally; an unreachable snapserver flips the
        # exit code (a bond whose bindings cannot be verified is a
        # degraded bond) and the runtime health shows it.
        from .snapcast_rpc import ensure_groups_on_stream

        report = ensure_groups_on_stream(SNAP_STREAM_ID)
        logger.info(
            "event=multiroom.reconcile.stream_binding reachable=%s groups=%d "
            "fixed=%d failed=%d want=%s",
            report["reachable"], report["groups"], report["fixed"],
            report["failed"], SNAP_STREAM_ID,
        )
        if not report["reachable"] or report["failed"]:
            rc = 1

    logger.info("event=multiroom.reconcile.done rc=%d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
