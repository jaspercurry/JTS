# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom grouping — the pure plan, unit names, and argv/derived-args
builders. A leaf: stdlib plus sibling PURE multiroom modules only, no
subprocess/systemctl/camilla/dsp_apply, so a caller that only needs
``plan()``, an argv builder, or ``SNAPFIFO``/``ARGS_FILE`` does not pay
``jasper.multiroom.reconcile``'s heavier import cost (ADR-0226; a
Type=oneshot on a Pi Zero 2 W).

Every name below is PURE and total: no I/O, no subprocess, no clock.
``jasper.multiroom.reconcile`` re-imports what it still uses so existing
importers keep working unchanged; SNAPFIFO's own external readers (doctor
checks, active_speaker) import it from here directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SNAP_STREAM_ID, GroupingConfig
from .dac_content_ring import DAC_CONTENT_RING_PCM
from .grouping_ring import GROUPING_RING_PCM

# ---------- Unit names plan() decides ----------


SNAPSERVER_UNIT = "jasper-snapserver.service"
SNAPCLIENT_UNIT = "jasper-snapclient.service"


# ---------- Snapcast wiring constants ----------

# The FIFO the fan-in chain writes the mixed stereo program into and snapserver
# reads as its pipe source. Lives in snapserver's OWN per-unit runtime dir
# (RuntimeDirectory=jasper-snapserver): a unit's RuntimeDirectory is reaped when
# it stops, so a shared one would let snapserver stopping destroy another
# daemon's sockets. tmpfs-backed, recreated each boot.
SNAPFIFO = "/run/jasper-snapserver/snapfifo"

# Reconciler-owned runtime env file holding the DERIVED snapcast args (the argv
# after argv[0], space-joined). The snapserver/snapclient units pick it up
# through their only generated `EnvironmentFile=`; the root services never read
# management-writable grouping.env directly.
#
# Deliberately NOT a unit RuntimeDirectory: that is reaped the moment its unit
# stops, which would erase args a sibling unit (or a restart) still needs.
# tmpfs-backed (/run), so it is recreated on every boot reconcile before the
# units start.
ARGS_DIR = "/run/jasper-grouping"
ARGS_FILE = ARGS_DIR + "/snapcast-args.env"

# The two derived keys the units read (one line per key, empty-string to clear).
_SERVER_ARGS_KEY = "JASPER_SNAPSERVER_ARGS"
_CLIENT_ARGS_KEY = "JASPER_SNAPCLIENT_ARGS"


# ---------- The leader's music-producer predicate ----------


def desired_snapfifo_path(cfg: GroupingConfig) -> str:
    """The FIFO path the leader's MUSIC PRODUCER must feed, or "" when this role
    needs no producer. PURE.

    Only a VALID LEADER hosts the synchronised stream. Drives the runtime-health
    derive: a leader whose active CamillaDSP config does not write the pipe is
    degraded.
    """
    if cfg.enabled and cfg.error is None and cfg.role == "leader":
        return SNAPFIFO
    return ""


# ---------- Plan types ----------


@dataclass(frozen=True)
class UnitIntent:
    """A desired terminal state for one systemd unit.

    `desired` is one of {"start", "stop"}; `reason` is a short human-readable
    explanation for the log line. Source lifecycle verbs deliberately do not
    exist here — ``jasper.source_intent`` owns them.
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
        # Fail-safe to SOLO behavior: a broken bond must not keep the
        # household's sources parked on top of not playing.
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "stop", "config invalid"),
                UnitIntent(SNAPCLIENT_UNIT, "stop", "config invalid"),
            ),
            summary=(f"grouping enabled but INVALID: {cfg.error} — not starting"),
        )

    if cfg.role == "leader":
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "start", "leader hosts stream"),
                UnitIntent(SNAPCLIENT_UNIT, "start", "leader plays its channel"),
            ),
            summary=f"grouping leader (bond {cfg.bond_id}, channel {cfg.channel})",
        )

    return ReconcilePlan(
        intents=(
            UnitIntent(SNAPSERVER_UNIT, "stop", "follower runs no server"),
            UnitIntent(SNAPCLIENT_UNIT, "start", "follower consumes stream"),
        ),
        summary=(
            f"grouping follower (bond {cfg.bond_id}, channel {cfg.channel}, "
            f"leader {cfg.leader_addr}, sources parked)"
        ),
    )


# ---------- Pure argv builders ----------


def snapserver_argv(cfg: GroupingConfig) -> list[str]:
    """Build the snapserver command line from a GroupingConfig.

    PURE: a deterministic function of `cfg`. cfg.buffer_ms is the group/network
    playout buffer, passed as the GLOBAL ``--stream.buffer``.
    """
    # sampleformat is PINNED, not left to snapserver's default: the whole chain
    # is 48 kHz / S16 / stereo — CamillaDSP's File sink writes it and outputd's
    # dac_content reader assumes it. mode=create is pinned because snapserver
    # owning FIFO creation is load-bearing: it opens the read end first, so
    # CamillaDSP's write-open cannot block.
    source = (
        f"pipe://{SNAPFIFO}?name={SNAP_STREAM_ID}"
        f"&mode=create"
        f"&sampleformat=48000:16:2"
        f"&codec={cfg.codec}"
    )
    # buffer_ms is the GLOBAL `--stream.buffer` flag (snapcast's end-to-end
    # capture->playout latency), NOT a `pipe://?...&buffer_ms=` source-URL query
    # param. snapcast's pipe-source parser reads only name/mode/sampleformat/
    # codec/chunk_ms and SILENTLY IGNORES an unknown query key, so a
    # `&buffer_ms=` there is inert and the bond runs snapcast's 1000 ms default.
    return [
        "snapserver",
        "--stream.source",
        source,
        "--stream.buffer",
        str(cfg.buffer_ms),
    ]


def snapclient_argv(
    cfg: GroupingConfig,
    *,
    player_alsa_device: str | None = None,
) -> list[str]:
    """Build the snapclient command line from a GroupingConfig.

    PURE: a deterministic function of `cfg` (+ the optional
    ``player_alsa_device``). The host is the loopback when this speaker is the
    leader (it runs its own server), otherwise the leader's address.

    Channel selection (which of L/R/mono this client plays) is a CamillaDSP
    or outputd concern and is intentionally NOT decided here.

    ``active_endpoint`` (the ACTIVE follower, plus the active leader's own
    drivers) DISABLES the ``dac_content`` ChannelPick on this box: CamillaDSP
    owns both the channel-pick and the ``2->N`` split (Layer A), so outputd just
    runs its normal active sink fed by camilla.

    THE ARMED BRANCH WRITES A BLANK ``JASPER_OUTPUTD_CONTENT_BRIDGE``, and every
    other branch OMITS the key. outputd refuses the marker beside a DECLARED
    bridge of any value, and its ``env_optional`` read counts blank as
    undeclared — so blank is what overrides the ``shm_ring`` that
    ``jasper-fanin-coupling-auto`` writes into the FIRST env layer on every pass.
    Omitting the key there leaves that value standing and parks the daemon at
    EX_CONFIG under ``RestartPreventExitStatus=78``. The unarmed branches must
    NOT write blank: without the marker outputd reads this key with ``env_str``,
    whose blank is a value it parks on, so they inherit layer 1 verbatim.

    Active-mode TTS stays upstream of the crossover in fan-in. The outputd TTS
    mixer is stereo-only and post-crossover; on an active lane a 2-way speaker is
    also "2 channels", so arming that socket would send full-range assistant
    audio to the tweeter. Active endpoints therefore clear the outputd TTS socket
    along with the dac_content lane.
    """
    # cfg.leader_addr is passed VERBATIM to snapclient --host. The bond wizard
    # mints it as a STABLE mDNS .local handle (the leader's JASPER_HOSTNAME), not
    # a raw DHCP IP, so a follower survives the leader changing IP: snapclient
    # re-resolves the name via mDNS at connect/reconnect time. A literal IPv4 is
    # also accepted — see config.GroupingConfig.leader_addr.
    host = "127.0.0.1" if cfg.role == "leader" else cfg.leader_addr
    argv = [
        "snapclient",
        "--host",
        host,
        "--latency",
        str(cfg.client_latency_ms),
    ]
    if player_alsa_device:
        argv += ["--soundcard", player_alsa_device, "--player", "alsa"]
    return argv


def assemble_args(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
) -> dict[str, str]:
    """Derive the {key: value} the units read, from a GroupingConfig.

    PURE: a deterministic function of `cfg`. Returns the two derived keys
    (``JASPER_SNAPSERVER_ARGS`` / ``JASPER_SNAPCLIENT_ARGS``) whose values are
    the argv AFTER argv[0] (the binary name, already in the unit's ExecStart),
    space-joined. Both keys are ALWAYS present; a key is the EMPTY STRING when
    its unit should not carry derived args (a follower runs no server; a
    disabled or invalid config clears both). The units do not start in those
    states, but clearing the derived args means a started unit can never pick up
    STALE values.

    Word-splitting safety: snapcast args must stay space-free, asserted in
    ``_join_args`` — the units' unquoted ``$JASPER_SNAP*_ARGS`` would mangle a
    space-containing arg.
    """
    if not cfg.enabled or cfg.error is not None:
        return {_SERVER_ARGS_KEY: "", _CLIENT_ARGS_KEY: ""}

    # The units invoke `/usr/bin/snap* $ARGS`, so persist only argv[1:].
    server = "" if cfg.role != "leader" else _join_args(snapserver_argv(cfg))
    # ONE snapclient shape, two rings, told apart by which end READS them: an
    # ACTIVE endpoint's own CamillaDSP captures GROUPING_RING_PCM to run Layer A
    # in the bonded path, while a DUMB member's outputd reads
    # DAC_CONTENT_RING_PCM as its sole content source. A member the lane
    # decision refuses never reaches here bonded — `main` falls back to solo,
    # which returns above on `cfg.enabled`.
    player = GROUPING_RING_PCM if active_endpoint else DAC_CONTENT_RING_PCM
    client = _join_args(snapclient_argv(cfg, player_alsa_device=player))
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
