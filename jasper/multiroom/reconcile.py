# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom grouping reconciler — pure plan + thin systemctl entrypoint.

Single writer of the snapcast unit state. Reads the wizard-owned GroupingConfig
(``jasper.multiroom.config``) and decides which units run; an enabled-but-INVALID
config runs neither (never bring up a broken bond).

After its role/data-plane work lands it hands the role to the canonical source
coordinator. Grouping never starts or stops source resources itself.

``plan`` and the argv builders are PURE and total. jasper-grouping-reconcile.service
is Type=oneshot — there is no resident process here.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .. import atomic_io
from .. import tts_routing as _tts_routing
from ..fanin_coupling import (
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    RING_ACTIVE_PLAYBACK_DEVICE,
)
from ..log_event import log_event
from ..ring_assets import RING_ACTIVE_CONTENT_FILE, ring_writer_lock_path
from ..source_intent import (
    RECONCILE_SYSTEMD_TIMEOUT_SECONDS as SOURCE_RECONCILE_SYSTEMD_TIMEOUT_SECONDS,
)
from ..source_intent import RECONCILE_UNIT as SOURCE_INTENT_RECONCILE_UNIT
from . import config
from .config import SNAP_STREAM_ID, GroupingConfig
from .dac_content_ring import (
    DAC_CONTENT_LANE_ENV,
    DAC_CONTENT_RING_PCM,
    DAC_CONTENT_RING_PERIOD_FRAMES,
    dac_content_ring_servable,
)
from .effective_role import (
    FOLLOWER_STATUS_FILE,
    grouping_request_fingerprint,
    normalise_boot_id,
    read_current_boot_id,
    read_effective_role_status,
)
from .grouping_ring import GROUPING_RING_PCM
from .tts_route import VOICE_PARK_ENV, expected_grouping_tts_route

logger = logging.getLogger(__name__)

OUTPUTD_TTS_SOCKET = _tts_routing.OUTPUTD_TTS_SOCKET
OUTPUTD_TTS_SOCKET_ENV = _tts_routing.OUTPUTD_TTS_SOCKET_ENV
VOICE_TTS_SOCKET_ENV = _tts_routing.VOICE_TTS_SOCKET_ENV
TTS_MIX_STAGE_ENV = _tts_routing.TTS_MIX_STAGE_ENV
TTS_MIX_STAGE_POST_DSP = _tts_routing.TTS_MIX_STAGE_POST_DSP


# ---------- Unit names (single source of truth) ----------

SNAPSERVER_UNIT = "jasper-snapserver.service"
SNAPCLIENT_UNIT = "jasper-snapclient.service"
# The AirPlay receiver. A FOLLOWER parks it; a LEADER keeps it running and gets
# its backend latency offset re-derived on bond/unbond (a bonded leader folds in
# the Snapcast round-trip buffer — see airplay_grouping_env).
SHAIRPORT_UNIT = "shairport-sync.service"
# Short manager requests (probes, reset-failed) return promptly. Blocking
# starts/restarts may wait for a normal service job, but must remain finite when
# this module is run directly during install or repair, outside the grouping
# oneshot's outer systemd timeout.
_SYSTEMCTL_CONTROL_TIMEOUT_SEC = 5.0
_SYSTEMCTL_BLOCKING_TIMEOUT_SEC = 60.0
# A role handoff never RESTARTS the source owner: it may be between ordered USB
# fan-in/gadget steps. A blocking ``systemctl start`` against a running
# activation is only a barrier (it joins and waits), bounded just beyond the
# target unit's own TimeoutStartSec.
_SOURCE_RECONCILE_START_TIMEOUT_SEC = SOURCE_RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 5.0
_MAX_SOURCE_RECONCILE_STARTS = 2  # drain prior pass, then run fresh role pass
# Conservative *sequential* ceilings, not typical latency: a steady-state pass
# normally performs no blocking work.
_MAX_PLAN_UNIT_INTENTS = 2  # snapserver + snapclient; sources have one owner
_SNAPCAST_PROVISION_BUDGET_SEC = 420.0  # apt update 120 + install 300
_MAX_POST_PLAN_BLOCKING_ACTIONS = 6
# _plan_changes_units probes each plan intent's unit (one ActiveState read) BEFORE
# _apply runs it.
_UNIT_CHANGE_PROBE_CALLS = _MAX_PLAN_UNIT_INTENTS
_BASE_RECONCILE_BUDGET_SEC = (
    _MAX_PLAN_UNIT_INTENTS * _SYSTEMCTL_BLOCKING_TIMEOUT_SEC
    + _SNAPCAST_PROVISION_BUDGET_SEC
    + _MAX_POST_PLAN_BLOCKING_ACTIONS * _SYSTEMCTL_BLOCKING_TIMEOUT_SEC
    + _UNIT_CHANGE_PROBE_CALLS * _SYSTEMCTL_CONTROL_TIMEOUT_SEC
)
_OWNER_CONTROL_CALLS_PER_HANDOFF = 2  # reset-failed + ActiveState probe
_RECONCILE_TIMEOUT_MARGIN_SEC = 30.0
_RECONCILE_SYSTEMD_TIMEOUT_SEC = (
    _BASE_RECONCILE_BUDGET_SEC
    + _MAX_SOURCE_RECONCILE_STARTS * _SOURCE_RECONCILE_START_TIMEOUT_SEC
    + _OWNER_CONTROL_CALLS_PER_HANDOFF * _SYSTEMCTL_CONTROL_TIMEOUT_SEC
    + _RECONCILE_TIMEOUT_MARGIN_SEC
)

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

# ---------- the leader's music producer ----------
#
# The leader's CamillaDSP feeds the snapserver pipe (post-correction,
# post-master_gain — the stream inherits the volume + safety ceiling), applied by
# this reconciler via jasper.multiroom.leader_config. Producer liveness for
# runtime health reads the ACTIVE CamillaDSP config (camilla's own statefile
# names it, and the doctor's `leader pipe` check scans it), never a Python mirror
# of env intent.

# ---------- the member round-trip content lane ----------
#
# The dumb member's round-trip rides the dac-content SHM ring: snapclient writes
# DAC_CONTENT_RING_PCM through its `alsa` player and outputd reads that ring as
# its sole content source. The transport's identity — PCM name, ring file, wire,
# slot geometry — is owned by jasper.multiroom.dac_content_ring. Still never
# snd-aloop (snapclient's snd_pcm_delay would lie, inv-2) and never the raw DAC,
# which outputd owns.

# Reconciler-owned PERSISTENT env file the jasper-outputd unit layers after
# jasper.env (EnvironmentFile=-). Persistent (NOT /run) so a bonded speaker boots
# with the lane already configured — no extra outputd restart at boot. Both
# derived keys are written as empty strings when this speaker is not an active
# member, so a stale file can never leave the lane half-configured.
OUTPUTD_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-outputd.env"
OUTPUTD_DAC_CONTENT_FIFO_ENV = "JASPER_OUTPUTD_DAC_CONTENT_FIFO"
OUTPUTD_DAC_CONTENT_CHANNEL_ENV = "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL"
OUTPUTD_DAC_CONTENT_TRIM_ENV = "JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB"
OUTPUTD_UNIT = "jasper-outputd.service"
CAMILLA_UNIT = "jasper-camilla.service"

# Voice-side grouping route: a reconciler-owned PERSISTENT env file layered LAST
# in jasper-voice.service. The TTS route matrix decides whether this file points
# voice at outputd, parks voice/AEC, or OMITS the socket so voice falls back to
# fan-in. Omission (not present-but-empty) is required: an empty value is read as
# a real, invalid path.
VOICE_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-voice.env"
VOICE_UNIT = "jasper-voice.service"

# Reconciler-owned PERSISTENT env file the shairport-sync unit's ExecStartPre
# (jasper-apply-airplay-mode) layers when deriving the AirPlay backend latency
# offset. Holds the bonded-leader-only Snapcast round-trip delay; EMPTY (no keys)
# for solo/follower so the offset stays byte-identical to the solo value.
# Persistent (NOT /run) so a bonded leader boots with the bonded offset already
# derived. mode 0644, no secret.
AIRPLAY_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-airplay.env"
AIRPLAY_BONDED_EXTRA_DELAY_ENV = "JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC"

# jasper-aec-reconcile is the SINGLE owner of jasper-voice + jasper-aec-bridge
# unit state. Role changes therefore KICK it rather than touching those units
# here: it reads the derived park flag below and restarts-or-parks voice per
# role + provider + mic, one writer total.
AEC_RECONCILE_UNIT = "jasper-aec-reconcile.service"
AUDIO_HARDWARE_RECONCILE = "/usr/local/sbin/jasper-audio-hardware-reconcile"

# camilla#2 — the endpoint-crossover CamillaDSP instance (:1235), armed ONLY on
# an ACTIVE LEADER. Reconciler-gated: `enable --now` on bond (after the statefile
# is re-seeded with the re-proven driver-domain graph) and `disable --now` on
# unbond. It carries NO StartLimitAction=reboot, so a failed arm fails closed to
# silence through the crossover — never reboots the household speaker (unlike the
# always-on camilla#1).
CROSSOVER_UNIT = "jasper-camilla-crossover.service"

# The exclusive active-content PCM camilla#1 owns in solo-active mode and
# camilla#2 owns after the active-leader handoff. It is the ACTIVE ring
# (`jts_ring_active_playback` -> `/dev/shm/jts-ring/active-content.ring`), and
# the release signal is the ring's own writer lock: the C ioplug's writer holds
# an exclusive `flock` on `<ring>.writer.lock` for the life of its mapping, so a
# NON-BLOCKING exclusive `flock` that SUCCEEDS proves no writer owns the ring.
# The kernel drops an `flock` on process exit INCLUDING SIGKILL, so there is no
# frozen-state window, and it is the SAME primitive camilla#2 contends on when it
# attaches.
#
# ORDERING CONSEQUENCE: a box whose ring platform never armed has no
# `active-content.ring.writer.lock` at all, so this probe answers `unknown` and
# the arm fails closed to solo-active with the lock path in the log line. Arming
# without proof is the EBUSY reboot loop this exists to prevent.
ACTIVE_CONTENT_WRITER_LOCK_PATH = ring_writer_lock_path(RING_ACTIVE_CONTENT_FILE)
ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC = 0.8
ACTIVE_CONTENT_RELEASE_POLL_SEC = 0.05


@dataclass(frozen=True)
class _PcmHandleProbeResult:
    """One bounded active-content PCM release probe result."""

    state: str  # "released" | "busy" | "unknown"
    reason: str
    detail: str = ""
    lock_path: str = ACTIVE_CONTENT_WRITER_LOCK_PATH
    attempts: int = 0
    timeout_sec: float = 0.0

    @property
    def released(self) -> bool:
        return self.state == "released"

    @property
    def busy(self) -> bool:
        return self.state == "busy"

    @property
    def unknown(self) -> bool:
        return self.state == "unknown"


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

    THE FIFO KEY IS CLEARED, NEVER SET. Arming both round-trip transports at once
    is outputd's most fundamental refusal — two content sources on one DAC — and
    this writer must never emit a combination the validator rejects across all
    env LAYERS.

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


#: Why a bonded member is not on the dac-content return ring. Stable tokens:
#: they reach ``/state`` and the doctor through the follower STATUS file.
LANE_REFUSED_ACTIVE_ENDPOINT = "active_endpoint"
LANE_REFUSED_FLAT_OUTPUT_DENIED = "flat_output_not_allowed"
LANE_REFUSED_PERIOD = "dac_content_ring_period_mismatch"


@dataclass(frozen=True)
class LaneDecision:
    """Whether this box arms the dac-content return lane, and why not."""

    armed: bool
    #: One of the ``LANE_REFUSED_*`` tokens, or ``""`` when armed.
    reason: str = ""


def member_lane_decision(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
    outputd_period_frames: int | None = None,
) -> LaneDecision:
    """THE arming rule for the dumb-member round-trip lane. PURE.

    Four conditions, spelled once and consumed by everything that needs the
    answer — the env writer, the reconciler's bond refusal, and the doctor's
    channel-pick check:

    - an ``is_active_member``-shaped config (enabled, no error);
    - not an ACTIVE endpoint: CamillaDSP owns that box's channel-pick and split
      (Layer A), so outputd runs its normal active sink and no lane;
    - a saved topology that permits a flat final-output graph, from the
      canonical output runtime contract;
    - an outputd period the ring's slot can carry
      (:func:`~jasper.multiroom.dac_content_ring.dac_content_ring_servable`).

    A disabled or invalid config is not refused — it is not a member at all —
    so it returns the same unarmed decision with no reason token.
    """
    if not (cfg.enabled and cfg.error is None):
        return LaneDecision(armed=False)
    if active_endpoint:
        return LaneDecision(armed=False, reason=LANE_REFUSED_ACTIVE_ENDPOINT)
    if not flat_output_allowed:
        return LaneDecision(armed=False, reason=LANE_REFUSED_FLAT_OUTPUT_DENIED)
    if not dac_content_ring_servable(outputd_period_frames):
        return LaneDecision(armed=False, reason=LANE_REFUSED_PERIOD)
    return LaneDecision(armed=True)


def _assemble_args(
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


def outputd_grouping_env(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
    flat_output_allowed: bool = False,
    outputd_period_frames: int | None = None,
) -> dict[str, str]:
    """The outputd round-trip lane env derived from a GroupingConfig. PURE.

    Whether the lane arms is :func:`member_lane_decision`'s answer, never a
    second rule; what the lane IS is :mod:`jasper.multiroom.dac_content_ring`'s
    module docstring.

    Every non-arming shape gets EMPTY strings rather than absent keys — outputd
    reads empty as unset (``env_optional``) and as disarmed (``env_bool``), so a
    stale file can never half-configure the lane.

    ``active_endpoint`` (the ACTIVE follower, plus the active leader's own
    drivers) DISABLES the ``dac_content`` ChannelPick on this box: CamillaDSP
    owns both the channel-pick and the ``2->N`` split (Layer A), so outputd just
    runs its normal active sink fed by camilla.

    THE ARMED BRANCH WRITES A BLANK ``JASPER_OUTPUTD_CONTENT_BRIDGE``, and every
    other branch OMITS the key. outputd refuses the marker beside a DECLARED
    bridge of any value (``rust/jasper-outputd/src/config.rs``), and its
    ``env_optional`` read counts blank as undeclared — so blank is what
    overrides the ``shm_ring`` that ``jasper-fanin-coupling-auto`` writes into
    the FIRST env layer on every pass. Omitting the key there would leave that
    value standing and park the daemon at EX_CONFIG under
    ``RestartPreventExitStatus=78``. The unarmed branches must NOT write blank:
    without the marker outputd reads this key with ``env_str``, whose blank is a
    value it parks on, so an unarmed box has to inherit layer 1 verbatim.

    THE FIFO KEY IS CLEARED, NEVER SET. Arming both round-trip transports at
    once is outputd's most fundamental refusal — two content sources on one DAC
    — and this writer must never emit a combination the validator rejects across
    all env LAYERS.

    Active-mode TTS stays upstream of the crossover in fan-in: the outputd TTS
    mixer is stereo-only and post-crossover, and on an active lane a 2-way
    speaker is also "2 channels", so arming that socket would send full-range
    assistant audio to the tweeter. Active endpoints therefore clear the outputd
    TTS socket along with the lane.
    """
    route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)

    if cfg.enabled and cfg.error is None:
        if not member_lane_decision(
            cfg,
            active_endpoint=active_endpoint,
            flat_output_allowed=flat_output_allowed,
            outputd_period_frames=outputd_period_frames,
        ).armed:
            return {
                DAC_CONTENT_LANE_ENV: "",
                OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
                OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
                OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
                # Empty = unset to outputd's env_f32 (default 0.0).
                OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
            }  # no CONTENT_BRIDGE key: layer 1's value must stand
        return {
            # The BARE marker outputd's env_bool reads (never a path — outputd
            # derives the ring file from its own DEFAULT_DAC_CONTENT_RING_PATH,
            # so the two ends have no second spelling to disagree on).
            DAC_CONTENT_LANE_ENV: "1",
            # BLANK, not absent: this layer loads AFTER outputd.env, where
            # jasper-fanin-coupling-auto writes shm_ring on every pass.
            OUTPUTD_CONTENT_BRIDGE_ENV_VAR: "",
            OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
            OUTPUTD_DAC_CONTENT_CHANNEL_ENV: cfg.channel or "stereo",
            OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
            # Pair-balance trim (validated <= 0 by load_config; outputd
            # re-validates fail-closed). Always written while bonded so
            # a cleared trim converges back to 0.0.
            OUTPUTD_DAC_CONTENT_TRIM_ENV: f"{cfg.trim_db:.1f}",
        }
    return {
        DAC_CONTENT_LANE_ENV: "",
        OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
        OUTPUTD_TTS_SOCKET_ENV: "",
        # Empty = unset to outputd's env_f32 (default 0.0).
        OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
    }


def voice_grouping_env(
    cfg: GroupingConfig,
    *,
    active_endpoint: bool = False,
) -> dict[str, str]:
    """jasper-voice's grouping-derived env. PURE.

    The route matrix owns the policy. Passive members point voice's TTS
    playout socket at outputd so each member's OWN replies mix at its OWN final
    output; inv-3 keeps the leader's TTS out of the SHARED stream. Active
    endpoints fail closed to fan-in or park, with outputd TTS unarmed. Solo also
    returns an EMPTY dict — the key is omitted, never present-but-empty (a
    set-empty value would be read as a real, invalid socket path).
    """
    route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)
    if cfg.enabled and cfg.error is None:
        env = (
            {}
            if route.voice_env_socket is None
            else {
                VOICE_TTS_SOCKET_ENV: route.voice_env_socket,
                TTS_MIX_STAGE_ENV: TTS_MIX_STAGE_POST_DSP,
            }
        )
        if route.voice_parked:
            # Parked routes stop voice (and the AEC stack) through the flag
            # jasper-aec-reconcile gates on; the route matrix owns any socket
            # override separately.
            env[VOICE_PARK_ENV] = "1"
        return env
    return {}


def airplay_grouping_env(cfg: GroupingConfig) -> dict[str, str]:
    """shairport's bonded-leader AirPlay latency-offset delta. PURE.

    Only an ACTIVE bonded LEADER both receives AirPlay AND plays its own channel
    through the Snapcast round-trip, so only a leader's shairport must fold the
    Snapcast playout buffer into its backend latency offset to keep the leader's
    OWN output landing on the AirPlay anchor (lip-sync). Everyone else — solo,
    follower (shairport parked), invalid — gets an EMPTY dict, which clears the
    file to the byte-identical solo offset.

    The value is the Snapcast buffer in SECONDS — the dominant new delay the
    bonded leader's own output gains over solo, and deliberately a first-order
    estimate: the solo offset's Ring A / CamillaDSP / Ring B / outputd terms
    still apply in the bonded path, and the residual (CamillaDSP pipe-sink fill,
    the member content FIFO) is second-order and acoustically calibrated
    alongside snapclient --latency. jasper-apply-airplay-mode ADDS this to the
    solo-derived offset.
    """
    if config.is_active_leader(cfg):
        return {AIRPLAY_BONDED_EXTRA_DELAY_ENV: f"{cfg.buffer_ms / 1000:.6f}"}
    return {}


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


# ============================================================
# I/O entrypoint. Everything above is pure; everything below does real
# systemctl calls. Keep that boundary crisp.
# ============================================================


def _output_topology_state() -> tuple[bool | None, bool]:
    """Return ACTIVE classification and permission for direct flat output.

    ``None`` preserves load/parse uncertainty for hardware-sensitive callers;
    they must not guess passive because that could bypass crossover protection.
    Both answers come from the same topology read.
    """
    try:
        from jasper.active_speaker.playback_route import (
            active_playback_route_capability,
        )
        from jasper.active_speaker.runtime_contract import (
            classify_output_contract,
            topology_allows_flat_dac_graph,
        )
        from jasper.output_topology import (
            OutputTopologyError,
            load_output_topology_strict,
        )

        topology = load_output_topology_strict()
        active = active_playback_route_capability(topology).active_group_count > 0
        flat_allowed = topology_allows_flat_dac_graph(
            classify_output_contract(topology)
        )
        return active, flat_allowed
    except ImportError:
        return None, False  # ORDER IS LOAD-BEARING: binds OutputTopologyError.
    except OutputTopologyError as e:
        log_event(
            logger,
            "multiroom.reconcile.active_speaker_probe_failed",
            error=e,
            level=logging.WARNING,
        )
        return None, False


def is_active_speaker_box() -> bool:
    """True when this speaker's saved output topology declares active 2-/3-way
    main groups. Splits the ACTIVE-follower path (CamillaDSP runs Layer A in the
    bonded path) from the DUMB-follower path (outputd ChannelPick).

    TOTAL + fail-soft: any load/parse failure resolves to ``False`` (treat as
    passive → the safe dumb-follower path). Commissioning READINESS is NOT
    checked here — a box that declares active groups but is not yet commissioned
    still takes the active path, where the follower apply fail-closes rather than
    silently degrading to a full-range dumb follower. Boolean consumers fail-soft
    unknown to ``False``; the reconciler reads :func:`_output_topology_state`
    directly and blocks graph transitions on unknown."""
    return _output_topology_state()[0] is True


def box_outputd_period_frames() -> int | None:
    """The outputd period THIS box will LOAD, or ``None`` if unresolved.

    :func:`jasper.audio_runtime_plan.outputd_period_frames_as_loaded`, never the
    plan's policy resolver: the slot gate has to match the value outputd's own
    ``env_u32`` reads off its three EnvironmentFile= layers, and the two differ
    exactly where guessing is fatal (a DAC floor of 128 with a stale 1024 still
    in ``outputd.env``; an operator ``jasper.env`` value the reconciler has not
    applied).

    Fail-soft to ``None``, which
    :func:`~jasper.multiroom.dac_content_ring.dac_content_ring_servable` reads
    as "do not arm": a wrong guess parks outputd and the speaker goes silent.

    Lazy import for the reason the rest of this module's
    ``jasper.audio_runtime_plan`` uses are lazy: that module is imported at
    module level by :mod:`jasper.multiroom.active_leader_config`.
    """
    try:
        from jasper.audio_runtime_plan import outputd_period_frames_as_loaded

        return outputd_period_frames_as_loaded()
    except Exception as e:  # noqa: BLE001 - an unresolved period must not raise
        log_event(
            logger,
            "multiroom.reconcile.outputd_period_unresolved",
            error=e,
            level=logging.WARNING,
        )
        return None


def _systemctl_unit_state(query: str, unit: str) -> bool | None:
    """Tri-state truth for one ``systemctl is-*`` query.

    A missing systemctl binary returns ``None`` silently; other spawn failures
    return ``None`` with one warning. Completed commands are classified by their
    explicit state TEXT, not return code alone, so a manager/D-Bus error cannot
    masquerade as disabled or inactive.
    """
    try:
        proc = subprocess.run(
            ["systemctl", query, unit],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.reconcile.unit_state_probe_failed",
            unit=unit,
            query=query,
            error=e,
            level=logging.WARNING,
        )
        return None

    state = (proc.stdout or "").strip().lower()
    true_states = {
        "is-enabled": {"enabled", "enabled-runtime"},
        "is-active": {"active"},
    }
    false_states = {
        "is-enabled": {
            "alias",
            "static",
            "indirect",
            "disabled",
            "generated",
            "transient",
            "linked",
            "linked-runtime",
            "masked",
            "masked-runtime",
            "not-found",
        },
        "is-active": {"inactive", "failed"},
    }
    if state in true_states.get(query, set()):
        return True
    if state in false_states.get(query, set()):
        return False
    log_event(
        logger,
        "multiroom.reconcile.unit_state_probe_failed",
        unit=unit,
        query=query,
        rc=proc.returncode,
        state=state or "(none)",
        stderr=(proc.stderr or "").strip(),
        level=logging.WARNING,
    )
    return None


def _unit_is_active(unit: str) -> bool:
    """``systemctl is-active`` truth. Only explicit ``active`` is true.

    Inactive, failed, absent, transitional, or unknown reads as not-active — the
    safe direction for the active-leader bake gate: a bake against a reader-less
    or missing snapserver pipe must NOT proceed, because it cannot release the
    DAC and arming camilla#2 would then fight camilla#1 for it."""
    return _systemctl_unit_state("is-active", unit) is True


def _probe_active_content_pcm_once(
    *,
    lock_path: str | None = None,
) -> _PcmHandleProbeResult:
    """Try to take the ACTIVE ring's writer lock once, non-blocking.

    The C ioplug's writer holds ``flock(LOCK_EX)`` on ``<ring>.writer.lock`` for
    the life of its mapping (``acquire_writer_lock``,
    ``c/jts-ring-ioplug/jts_ring_shm.c``), so:

      - the lock is FREE      -> ``released`` — no writer owns the ACTIVE ring.
      - ``EWOULDBLOCK``       -> ``busy``     — a live writer still owns it.
      - anything else         -> ``unknown``  — the caller fails closed.

    Three properties this probe must keep:

    1. **Never ``O_CREAT``.** A wrong-mode creation by an out-of-unit first
       creator locks the renderer out permanently (the sticky directory bit stops
       it deleting the file), which is why the ioplug ``fchmod``-heals the mode.
       An absent lock file means no writer has ever attached this ring ->
       ``unknown``.
    2. **Release immediately.** The probe is a BARRIER, not a lock handoff: it
       drops the lock before returning so it can never be the thing camilla#2
       contends with.
    3. **The TOCTOU is accepted.** camilla#1 could reattach between a successful
       probe and camilla#2's own attach; the authority is camilla#2's attach,
       which takes this same lock and gets ``-EBUSY`` if it lost the race.
       Fail-closed either way, so no handoff protocol is needed.

    ``O_RDONLY`` is deliberate: ``flock`` needs no write access, and the smaller
    request is the one more likely to succeed against a lock file created by a
    peer under a different uid.

    ``lock_path=None`` resolves :data:`ACTIVE_CONTENT_WRITER_LOCK_PATH` at CALL
    time, never as a bound default (the rule :mod:`jasper.ring_assets` states on
    ``ring_ioplug_so_path``): a def-time binding would make a caller that
    repoints the module constant silently probe the original path while every log
    line still names the constant.
    """
    lock_path = ACTIVE_CONTENT_WRITER_LOCK_PATH if lock_path is None else lock_path
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_absent",
            detail=str(e),
            lock_path=lock_path,
        )
    except PermissionError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_unopenable",
            detail=str(e),
            lock_path=lock_path,
        )
    except OSError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_open_error",
            detail=str(e),
            lock_path=lock_path,
        )

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            # EWOULDBLOCK/EAGAIN — a live writer holds the ring. The ONE busy
            # answer; every other failure is unknown, because "could not ask"
            # must not be reported as "someone is holding it".
            return _PcmHandleProbeResult(
                "busy",
                "writer_lock_held",
                detail=str(e),
                lock_path=lock_path,
            )
        except OSError as e:
            return _PcmHandleProbeResult(
                "unknown",
                "writer_lock_probe_error",
                detail=str(e),
                lock_path=lock_path,
            )
        # Barrier, not handoff: drop it before the caller acts on the answer.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return _PcmHandleProbeResult(
            "released",
            "writer_lock_free",
            lock_path=lock_path,
        )
    finally:
        os.close(fd)


def _wait_for_active_content_pcm_release(
    *,
    timeout_sec: float = ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC,
    interval_sec: float = ACTIVE_CONTENT_RELEASE_POLL_SEC,
    lock_path: str | None = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> _PcmHandleProbeResult:
    """Poll until camilla#1 has positively released the active-content PCM.

    Returns `busy` while a live writer still holds the ring's writer lock (and on
    timeout), and `unknown` when the lock cannot be asked at all — absent,
    unopenable, or an unexpected errno. The caller arms camilla#2 ONLY on a
    positive `released`; both `busy` and `unknown` fail closed to solo-active.

    ``lock_path=None`` resolves the module constant at CALL time, for the same
    reason the single-shot probe does.
    """
    lock_path = ACTIVE_CONTENT_WRITER_LOCK_PATH if lock_path is None else lock_path
    deadline = monotonic() + max(timeout_sec, 0.0)
    attempts = 0
    last = _PcmHandleProbeResult(
        "busy",
        "not_probed",
        lock_path=lock_path,
        timeout_sec=timeout_sec,
    )
    while True:
        attempts += 1
        last = _probe_active_content_pcm_once(lock_path=lock_path)
        if not last.busy:
            return replace(last, attempts=attempts, timeout_sec=timeout_sec)
        now = monotonic()
        if now >= deadline:
            detail = last.detail
            detail = f"{last.reason}: {detail}" if detail else last.reason
            return _PcmHandleProbeResult(
                "busy",
                "timeout",
                detail=detail,
                lock_path=lock_path,
                attempts=attempts,
                timeout_sec=timeout_sec,
            )
        sleep(min(interval_sec, max(deadline - now, 0.0)))


def _unit_absent_stderr(stderr: str) -> bool:
    """True when a systemctl failure means THE UNIT DOES NOT EXIST.

    A streambox box never installs some full-speaker units (e.g. the
    voice/AEC stack), so stop/park intents against absent units must be
    clean no-ops."""
    lowered = (stderr or "").lower()
    return "not loaded" in lowered or "not found" in lowered


def _unit_active(unit: str) -> bool | None:
    """Return whether `unit`'s live ``ActiveState`` counts as active.

    ``None`` on a probe failure or an unrecognized state; callers treat that
    as unproven and take the safe branch.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveState", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    state = (proc.stdout or "").strip().lower()
    if proc.returncode == 0 and state in {
        "active",
        "activating",
        "reloading",
        "deactivating",
    }:
        return True
    if proc.returncode == 0 and state in {"inactive", "failed"}:
        return False
    return None


def _plan_changes_units(intents: tuple[UnitIntent, ...]) -> bool:
    """Whether applying `intents` would flip any unit's live ``ActiveState``.

    Probed BEFORE the plan runs, so an already-active unit getting `start` (or
    an already-inactive unit getting `stop`) does not count as a change. A
    probe failure counts as a change — the caller uses this to decide whether
    the post-role source barrier can be skipped, and an unproven state must
    not license skipping it.
    """
    for it in intents:
        state = _unit_active(it.unit)
        if state is None or state != (it.desired == "start"):
            return True
    return False


def _apply(plan_: ReconcilePlan) -> int:
    """Apply a plan via systemctl. Returns a process exit code.

    A failure on one intent is logged and surfaced in the exit code but does not
    abort the rest of the plan — a half-applied bond is worse than a best-effort
    one. Units that do not exist on this install tier are clean no-ops.
    """
    rc = 0
    for it in plan_.intents:
        verb = it.desired
        try:
            subprocess.run(
                ["systemctl", verb, it.unit],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
            )
            log_event(
                logger,
                "multiroom.reconcile.unit",
                unit=it.unit,
                desired=it.desired,
                reason=it.reason,
            )
        except FileNotFoundError:
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                error="systemctl_not_found",
                level=logging.ERROR,
            )
            rc = 1
        except subprocess.CalledProcessError as e:
            if _unit_absent_stderr(e.stderr):
                log_event(
                    logger,
                    "multiroom.reconcile.unit",
                    unit=it.unit,
                    desired=it.desired,
                    result="skipped_unit_absent",
                    reason=it.reason,
                )
                continue
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                rc=e.returncode,
                stderr=(e.stderr or "").strip(),
                level=logging.ERROR,
            )
            rc = 1
        except (OSError, subprocess.SubprocessError) as e:
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                error=e,
                stderr=(getattr(e, "stderr", "") or "").strip(),
                level=logging.ERROR,
            )
            rc = 1
    return rc


def _write_derived_env(
    keys: dict[str, str],
    *,
    path: str = OUTPUTD_GROUPING_ENV_FILE,
    consumer: str,
) -> tuple[bool, bool]:
    """Write a reconciler-owned derived environment file iff it changed.

    Returns ``(changed, ok)``. Compare-before-write keeps the common no-change
    reconcile from restarting its consumer; the caller refreshes the consumer
    only on ``changed and ok``, because ``EnvironmentFile=`` is read at unit
    start and a content change without a restart would silently not apply.
    Fail-soft; carries no secrets (mode 0644)."""
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        old = Path(path).read_text()
    except OSError:
        old = None
    if old == body:
        return (False, True)
    if old is None and body == "":
        # Nothing existed and nothing needs clearing: a fresh solo speaker's
        # first reconcile must not count as a change, which would spuriously
        # restart the consuming unit (~15 s for jasper-voice) on first boot.
        return (False, True)
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            f"multiroom.reconcile.{consumer}_env_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return (True, False)
    return (True, True)


def _reset_failed_unit(unit: str) -> None:
    """Reset failed state before a DELIBERATE reconciler start or restart.

    The reconciler's restarts are control-plane CONFIG-APPLIES, not crash
    recovery. A rapid burst of /grouping/set updates legitimately re-derives the
    lane env many times in seconds, and each apply spends a slot of the target
    unit's StartLimitBurst; once that burst is exhausted inside
    StartLimitIntervalSec, systemd escalates to StartLimitAction=reboot (outputd
    / voice) or Camilla's recovery budget, turning deliberate churn into recovery
    escalation. reset-failed clears any prior failed / start-limit parking so a
    config-apply restart never consumes the crash-recovery budget. Genuine crash
    loops still escalate: a daemon's own Restart= path does NOT call this.

    Fail-soft and BEST-EFFORT: a reset-failed failure must never block the
    start/restart it precedes."""
    try:
        subprocess.run(
            ["systemctl", "reset-failed", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.reconcile.reset_failed_error",
            unit=unit,
            error=e,
            level=logging.WARNING,
        )


def _restart_unit(
    unit: str,
    *,
    no_block: bool = False,
    active_only: bool = False,
) -> bool:
    """Restart a unit so it re-reads its grouping env. Fail-soft (the caller
    reflects a failure in the exit code; the doctor's drift checks surface a lane
    left unwired).

    reset-failed FIRST (see :func:`_reset_failed_unit`) so a config-apply restart
    does not inherit the target's accumulated crash-reboot budget.

    `no_block` is for cross-owner kicks whose target owns its own downstream
    startup graph (grouping -> AEC -> voice). Ordered, same-owner restarts stay
    blocking so the reconciler still fails loudly when an apply step it owns does
    not land.
    """
    _reset_failed_unit(unit)
    cmd = ["systemctl"]
    if no_block:
        cmd.append("--no-block")
    verb = "try-restart" if active_only else "restart"
    cmd.extend((verb, unit))
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=(
                _SYSTEMCTL_CONTROL_TIMEOUT_SEC
                if no_block
                else _SYSTEMCTL_BLOCKING_TIMEOUT_SEC
            ),
        )
    except (OSError, subprocess.SubprocessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.unit_restart_failed",
            unit=unit,
            error=e,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.unit_restarted",
        unit=unit,
        reason="grouping_env_changed",
        no_block=no_block,
        active_only=active_only,
    )
    return True


def _source_reconciler_activation_busy() -> bool | None:
    """Return whether the source owner has an activation that can absorb a start.

    ``systemctl is-active`` does not distinguish every oneshot state, so read
    ``ActiveState`` directly via :func:`_unit_active`. Unknown / probe failure
    returns ``None``; the caller handles it in the safe direction.
    """

    state = _unit_active(SOURCE_INTENT_RECONCILE_UNIT)
    if state is None:
        log_event(
            logger,
            "multiroom.reconcile.owner_state_probe_failed",
            unit=SOURCE_INTENT_RECONCILE_UNIT,
            level=logging.WARNING,
        )
    return state


def _converge_sources_after_role(*, grouping_active: bool, units_changed: bool) -> bool:
    """Run a fresh source pass after grouping's role plan and await it.

    A bare no-block start can join an activation that read the PREVIOUS role. So:
    probe after the role apply; if an activation is busy (or its state cannot be
    trusted), synchronously join it as a bounded barrier, then start a new pass.
    If it is inactive, any activation racing the final start began after the
    probe and therefore already sees the new role. ``start`` throughout — never
    ``restart`` — so an ordered source transition is not interrupted. The final
    call is blocking: grouping reports success only after source park/restore
    reaches its terminal result.

    ``source-intent-reconcile.service`` in turn ``Wants=``/``After=``
    ``audio-hardware-reconcile.service`` (a ~30 s pass on a Pi Zero 2 W), so this
    barrier is skipped when grouping is off/solo AND the role plan touched no
    unit — nothing changed for source-intent to react to.
    """
    if not grouping_active and not units_changed:
        log_event(
            logger,
            "multiroom.sources_barrier_skipped",
            reason="no_role_change",
        )
        return True

    unit = SOURCE_INTENT_RECONCILE_UNIT
    _reset_failed_unit(unit)
    busy = _source_reconciler_activation_busy()
    if busy is not False:
        barrier_cmd = ["systemctl", "start", unit]
        try:
            subprocess.run(
                barrier_cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=_SOURCE_RECONCILE_START_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            log_event(
                logger,
                "multiroom.reconcile.owner_barrier_failed",
                unit=unit,
                error=exc,
                stderr=stderr.strip(),
                level=logging.ERROR,
            )
            return False
        log_event(
            logger,
            "multiroom.reconcile.owner_prior_activation_drained",
            unit=unit,
            state_was_unknown=busy is None,
        )

    cmd = ["systemctl", "start", unit]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_SOURCE_RECONCILE_START_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.source_converge_failed",
            unit=unit,
            error=exc,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.source_converged",
        unit=unit,
        reason="grouping_role_applied",
        waited_for_prior_activation=busy is not False,
    )
    return True


def _ensure_unit_active(unit: str, *, reason: str) -> bool:
    """Start a required unit after clearing a stale start-limit state.

    Active-leader self-healing can intentionally stop camilla#2 to release the
    active-content lane. If camilla#1 previously hit StartLimit while camilla#2
    held that lane, a plain ``systemctl start`` remains parked until
    ``reset-failed`` runs.
    """
    if _unit_is_active(unit):
        return True
    _reset_failed_unit(unit)
    try:
        subprocess.run(
            ["systemctl", "start", unit],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            error="systemctl_not_found",
            level=logging.ERROR,
        )
        return False
    except subprocess.CalledProcessError as e:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            rc=e.returncode,
            stderr=(e.stderr or "").strip(),
            level=logging.ERROR,
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            error=e,
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.unit_started",
        unit=unit,
        reason=reason,
    )
    return True


def _run_audio_hardware_reconcile(*, reason: str) -> bool:
    """Run the audio-hardware reconciler after an active-leader graph change.

    That reconciler is the single writer of /var/lib/jasper/outputd.env. Outputd
    must switch from the passive stereo lane to the active-content lane BEFORE
    camilla#2 is armed, or camilla#2 can fight an existing opener for the
    exclusive active-content playback PCM.
    """
    try:
        subprocess.run(
            [AUDIO_HARDWARE_RECONCILE, "--reason", reason],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.audio_hardware_failed",
            reason=reason,
            error=e,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.audio_hardware",
        reason=reason,
        result="reconciled",
    )
    return True


def _systemctl_crossover_unit(*verb: str, action: str) -> bool:
    """Run ``systemctl <verb...>`` against camilla#2 for the active-leader
    arm/teardown. Fail-soft (the doctor's active-leader crossover-unit check
    surfaces a unit left un-armed). camilla#2 carries NO
    StartLimitAction=reboot, so a failed arm fails closed to silence."""
    try:
        subprocess.run(
            ["systemctl", *verb, CROSSOVER_UNIT],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.crossover_unit_failed",
            unit=CROSSOVER_UNIT,
            action=action,
            error=e,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.crossover_unit",
        unit=CROSSOVER_UNIT,
        action=action,
    )
    return True


def _arm_crossover_unit() -> bool:
    """``systemctl enable --now`` camilla#2 for an active leader. Idempotent.

    The crossover statefile MUST be re-seeded with the re-proven driver-domain
    graph BEFORE this (the caller orders it) so a cold start never loads a flat
    statefile — full-range to a tweeter."""
    return _systemctl_crossover_unit("enable", "--now", action="armed")


def _disable_crossover_unit() -> bool:
    """``systemctl disable --now`` camilla#2 on unbond. Idempotent (disabling a
    not-armed unit is a no-op)."""
    return _systemctl_crossover_unit("disable", "--now", action="disabled")


def _write_follower_status(
    *,
    active_follower: bool,
    blocked_reason: str,
    active_leader: bool = False,
    requested_cfg: GroupingConfig | None = None,
    local_sources_allowed: bool | None = None,
    path: str = FOLLOWER_STATUS_FILE,
    boot_id_reader: Callable[[], str] | None = None,
) -> bool:
    """Publish the effective-role authorization fact and UI status.

    Rewritten every reconcile so the surface is fresh truth; read by
    jasper.multiroom.state rather than os.environ, because jasper-control is not
    restarted on a bond. I/O failures are contained and returned as ``False``:
    safety-sensitive callers must abort before granting local sources, while
    status-only refresh paths may continue with the previous fail-safe fact.

    ``active_follower`` = this box runs its local Layer-A crossover on the bonded
    stream as a FOLLOWER; ``active_leader`` = it runs that crossover (camilla#2)
    as the bond LEADER and also bakes the wire on camilla#1; ``blocked_reason``
    (non-empty) = an active-endpoint transition was REFUSED and either fell back
    to solo active or preserved the existing graph because ownership could not be
    changed safely (invariant 5 fail-closed).

    Every payload carries the current Linux boot ID. An attempted grant without a
    valid boot ID is rewritten as a deny and returns ``False``: a persistent
    grant must never survive into a later boot as fresh truth.
    """
    read_boot_id = boot_id_reader or read_current_boot_id
    try:
        boot_id = normalise_boot_id(read_boot_id())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        boot_id = ""
        log_event(
            logger,
            "multiroom.reconcile.boot_id_failed",
            error=exc,
            level=logging.ERROR,
        )
    grant_fresh = not (local_sources_allowed is True and not boot_id)
    if not grant_fresh:
        # A persistent grant without a current boot identity could be trusted
        # after reboot: publish an explicit deny and fail the caller's grant so
        # grouping retries rather than reporting a source-unpark transition that
        # never became authoritative.
        local_sources_allowed = False
        log_event(
            logger,
            "multiroom.reconcile.source_grant_blocked",
            reason="boot_id_unavailable",
            level=logging.ERROR,
        )
    payload: dict[str, object] = {
        "active_follower": active_follower,
        "active_leader": active_leader,
        "blocked_reason": blocked_reason,
        "boot_id": boot_id,
    }
    if requested_cfg is not None:
        payload["requested_fingerprint"] = grouping_request_fingerprint(
            requested_cfg,
        )
    if local_sources_allowed is not None:
        payload["local_sources_allowed"] = local_sources_allowed
    body = json.dumps(payload, sort_keys=True) + "\n"
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.follower_status_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return False
    return grant_fresh


def _restart_outputd() -> bool:
    return _restart_unit(OUTPUTD_UNIT)


def _write_args_file(keys: dict[str, str], *, path: str = ARGS_FILE) -> bool:
    """Atomically write the derived snapcast args to ``path``. Fail-soft.

    One ``KEY=value`` line per key, order preserved. Returns True on success,
    False on any failure; NEVER raises — a lost args write must not crash the
    reconcile path. Carries no secrets, so mode 0644 (matches grouping.env).
    """
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.args_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """systemd ExecStart entrypoint for jasper-grouping-reconcile.service.

    Loads the wizard-owned config fresh, computes the pure plan, persists the
    derived env files, and applies the plan via systemctl. Returns a process exit
    code.

    `--reason` is a free-text trigger source echoed into the structured log for
    correlation. Unknown args are ignored so a future caller adding a flag cannot
    crash the reconcile path.

    ORDER (load-bearing):

      1. Derived files (snapcast args + outputd lane env) + the member FIFO —
         before any unit work, so everything a started unit reads is fresh
         (``EnvironmentFile=`` is read at unit start).
      2. CamillaDSP solo RESTORE when this speaker is not an active leader —
         BEFORE units stop, so the pipe's writer leaves before its reader.
      3. outputd restart, only when the lane env CHANGED.
      4. The unit plan (stops before starts).
      5. CamillaDSP bonded APPLY when this speaker is an active leader — LAST,
         after snapserver started, so the pipe's reader exists before
         CamillaDSP's File sink opens it for write (a FIFO write-open blocks
         until a reader exists).

    Camilla apply/restore failures are caught and logged
    (event=multiroom.reconcile.camilla_failed): the reconcile still manages
    units, the doctor's `leader pipe` / runtime-health surfaces carry the
    unapplied state, and the exit code flips so the oneshot unit shows failed.
    """
    parser = argparse.ArgumentParser(prog="jasper.multiroom.reconcile")
    parser.add_argument("--reason", default="manual")
    args, _unknown = parser.parse_known_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Step 5 below swaps the live CamillaDSP graph, so its swap duck needs a
    # canonical target to release to.
    from jasper.volume_coordinator import install_env_canonical_target_provider

    install_env_canonical_target_provider()

    cfg = config.load_config()
    requested_cfg = cfg
    prior_role_status = read_effective_role_status(FOLLOWER_STATUS_FILE)
    transitioning_from_parked_role = (
        not config.local_sources_parked(requested_cfg)
        and prior_role_status.get("local_sources_allowed") is False
    )
    decision = plan(cfg)
    active = cfg.enabled and cfg.error is None
    active_leader = active and cfg.role == "leader"
    # An ACTIVE (multi-driver) follower relocates Layer A onto its own CamillaDSP
    # in the bonded path; a DUMB (single-DAC) follower uses outputd's dac_content
    # ChannelPick. The saved topology decides which path this reconcile takes.
    active_box_state, flat_output_allowed = _output_topology_state()
    box_is_active = active_box_state is True
    active_follower = active and cfg.role == "follower" and box_is_active
    # An ACTIVE leader is brains + endpoint: camilla#1 bakes the program domain
    # to the wire AND camilla#2 runs this box's own Layer-A crossover on the
    # round-tripped stream. A PASSIVE leader keeps the single-camilla pipe bake.
    active_speaker_leader = active_leader and box_is_active
    passive_leader = active_leader and not box_is_active
    # Both active endpoints (the follower AND the active leader's own drivers)
    # capture the grouping ring and run a camilla-owned channel-pick + split, so
    # they SHARE the snapclient-writes-the-ring + outputd-dac_content-disabled
    # wiring.
    active_endpoint = active_follower or active_speaker_leader
    # ONE lane decision per pass, read by the bond refusal below and handed to
    # the env writer, so the refusal and what gets written cannot disagree.
    outputd_period_frames = box_outputd_period_frames()
    lane_decision = member_lane_decision(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=outputd_period_frames,
    )
    log_event(
        logger,
        "multiroom.reconcile.start",
        reason=args.reason,
        enabled=cfg.enabled,
        role=cfg.role or "(none)",
        error=cfg.error or "(none)",
        active_box=("unknown" if active_box_state is None else box_is_active),
        active_follower=active_follower,
        active_leader=active_speaker_leader,
        summary=repr(decision.summary),
    )
    rc = 0
    endpoint_block_reason = ""
    active_leader_arm_blocked = False
    refused_follower_fallback = False

    def fall_back_to_solo() -> None:
        """Reset every derived bond role after a fail-safe refusal."""
        nonlocal cfg, decision, active, active_leader, active_follower
        nonlocal active_speaker_leader, passive_leader, active_endpoint
        nonlocal refused_follower_fallback, rc

        cfg = replace(cfg, enabled=False)
        refused_follower_fallback = config.local_sources_parked(requested_cfg)
        decision = plan(cfg)
        active = False
        active_leader = False
        active_follower = False
        active_speaker_leader = False
        passive_leader = False
        active_endpoint = False
        rc = 1

    if active_box_state is None:
        endpoint_block_reason = "active_speaker_topology_unknown"
        log_event(
            logger,
            "multiroom.reconcile.active_restore_blocked",
            reason=endpoint_block_reason,
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        _write_follower_status(
            active_follower=False,
            active_leader=False,
            blocked_reason=endpoint_block_reason,
            requested_cfg=requested_cfg,
            local_sources_allowed=(
                not config.local_sources_parked(cfg) and not transitioning_from_parked_role
            ),
            path=FOLLOWER_STATUS_FILE,
        )
        cleared, env_ok = _write_derived_env(
            outputd_grouping_env(cfg, flat_output_allowed=False),
            path=OUTPUTD_GROUPING_ENV_FILE,
            consumer="outputd",
        )
        if cleared and env_ok:
            _restart_outputd()
        return 1

    # A member whose outputd period cannot carry the return ring has no
    # round-trip transport, and arming one anyway makes outputd bail EX_CONFIG
    # under `RestartPreventExitStatus=78` — a parked daemon and a SILENT
    # speaker. Fail-SAFE to solo: the box keeps playing its own content, the
    # request stays in the wizard config, and a DAC change lets the next
    # reconcile bond. Placed BEFORE snapcast provision / any bond wiring. Only
    # this reason refuses the bond — the other two unarmed shapes (an ACTIVE
    # endpoint, a topology that forbids a flat graph) are legitimate members.
    if active and lane_decision.reason == LANE_REFUSED_PERIOD:
        endpoint_block_reason = LANE_REFUSED_PERIOD
        log_event(
            logger,
            "multiroom.reconcile.dac_content_ring_period_mismatch",
            reason=args.reason,
            outputd_period_frames=(
                "(unresolved)" if outputd_period_frames is None
                else outputd_period_frames
            ),
            ring_period_frames=DAC_CONTENT_RING_PERIOD_FRAMES,
            detail=(
                "this box's outputd period is not the dac-content return ring's "
                "slot, so outputd would refuse the pair at startup and park. "
                "Staying solo."
            ),
            level=logging.WARNING,
        )
        fall_back_to_solo()

    # Grouping prerequisite: install.sh ships the snapcast units but never the
    # binaries — that is the grouping opt-in's job (jasper.multiroom.provision).
    # Runs BEFORE the active-endpoint gate so the active-leader precheck's
    # snapcast check sees a fresh install. TOTAL + fail-soft: a failed install is
    # surfaced via /state.grouping.provision + the doctor and flips rc, but never
    # raises — the snap units simply fail to start, the box stays solo-safe, and
    # the next reconcile retries.
    if active:
        from .provision import ensure_snapcast_installed

        prov = ensure_snapcast_installed()
        if prov["state"] == "failed":
            log_event(
                logger,
                "multiroom.reconcile.snapcast_provision_failed",
                detail=prov["detail"] or "(none)",
                level=logging.ERROR,
            )
            rc = 1
        elif prov["state"] == "installed":
            log_event(
                logger,
                "multiroom.reconcile.snapcast_provisioned",
                result="installed",
            )

    # Active-ENDPOINT readiness GATE (fail-safe to SOLO). Build + re-prove the
    # driver-domain graph BEFORE tearing down the solo path — for a follower its
    # one CamillaDSP, for an active leader BOTH camilla#2's driver-domain graph
    # AND camilla#1's program bake. If it cannot be made safe (bad channel, not
    # commissioned, graph fails re-proof), do NOT bond: fall back to solo active
    # so the box keeps playing its own content instead of half-parking silent.
    # This is invariant 5's "refuses to bond" — the unsafe graph never reaches
    # the DACs. The actual CamillaDSP applies happen later, after snapcast is up.
    if active_endpoint:
        try:
            if active_speaker_leader:
                from .active_leader_config import precheck_active_leader_sync

                precheck_active_leader_sync(cfg)
            else:
                from .follower_config import precheck_active_follower_sync

                precheck_active_follower_sync(cfg)
        except RuntimeError as e:
            endpoint_block_reason = getattr(
                e,
                "reason",
                "active_endpoint_precheck_error",
            )
            # Distinct event per role; both literals stay greppable.
            blocked_event = (
                "multiroom.reconcile.active_leader_blocked"
                if active_speaker_leader
                else "multiroom.reconcile.active_follower_blocked"
            )
            log_event(
                logger,
                blocked_event,
                reason=endpoint_block_reason,
                error=e,
                level=logging.ERROR,
            )
            # Fail-safe to solo for the rest of this reconcile: treat exactly
            # like an invalid bond. Reset EVERY role flag — including
            # active_leader, which gates the step-6 stream-binding pin — so a
            # refused bond never partially behaves like a leader/endpoint.
            fall_back_to_solo()

    # A solo-active box needs positive ownership proof BEFORE any role-derived
    # file or unit mutation. Enabled intent alone is insufficient: a partial
    # `disable --now` can leave camilla#2 active. If either probe is unknown,
    # even applying the ordinary solo unit plan could remove SNAPFIFO's reader
    # and indirectly restart camilla#1 onto a DAC camilla#2 may still own.
    prior_crossover_owned = False

    def block_active_restore(reason: str) -> int:
        log_event(
            logger,
            "multiroom.reconcile.active_restore_blocked",
            reason=reason,
            unit=CROSSOVER_UNIT,
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        _write_follower_status(
            active_follower=False,
            active_leader=False,
            blocked_reason=reason,
            requested_cfg=requested_cfg,
            local_sources_allowed=(
                not config.local_sources_parked(cfg) and not transitioning_from_parked_role
            ),
            path=FOLLOWER_STATUS_FILE,
        )
        return 1

    if box_is_active and not active_leader and not active_follower:
        prior_crossover_enabled = _systemctl_unit_state(
            "is-enabled",
            CROSSOVER_UNIT,
        )
        prior_crossover_active = _systemctl_unit_state(
            "is-active",
            CROSSOVER_UNIT,
        )
        if prior_crossover_enabled is None or prior_crossover_active is None:
            return block_active_restore("crossover_ownership_state_unknown")
        prior_crossover_owned = prior_crossover_enabled or prior_crossover_active
        if prior_crossover_owned:
            if not _disable_crossover_unit():
                return block_active_restore("crossover_teardown_failed")
            crossover_active_after = _systemctl_unit_state(
                "is-active",
                CROSSOVER_UNIT,
            )
            if crossover_active_after is not False:
                return block_active_restore(
                    "crossover_inactive_state_unproven",
                )

    # Endpoint status for /state + the dashboard: active-follower /
    # active-leader mode, or the fail-closed block reason if the bond was refused
    # and this reconcile fell back to solo active.
    status_block_reason = endpoint_block_reason
    if transitioning_from_parked_role and not status_block_reason:
        status_block_reason = "role_transition_in_progress"
    role_status_ok = _write_follower_status(
        active_follower=active_follower,
        active_leader=active_speaker_leader,
        blocked_reason=status_block_reason,
        requested_cfg=requested_cfg,
        local_sources_allowed=(
            not config.local_sources_parked(cfg)
            and not refused_follower_fallback
            and not transitioning_from_parked_role
        ),
        path=FOLLOWER_STATUS_FILE,
    )
    if not role_status_ok:
        log_event(
            logger,
            "multiroom.reconcile.effective_role_publish_failed",
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        return 1

    # 1. Derived files — before any unit work.
    derived = _assemble_args(cfg, active_endpoint=active_endpoint)
    wrote = _write_args_file(derived)
    set_keys = [k for k, v in derived.items() if v]
    log_event(
        logger,
        "multiroom.reconcile.args",
        path=ARGS_FILE,
        ok=wrote,
        set=",".join(set_keys) or "(none)",
    )

    # Paths passed explicitly (module globals read at CALL time); a def-time
    # default would pin the production path.
    outputd_env = outputd_grouping_env(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=outputd_period_frames,
    )
    env_changed, env_ok = _write_derived_env(
        outputd_env,
        path=OUTPUTD_GROUPING_ENV_FILE,
        consumer="outputd",
    )
    log_event(
        logger,
        "multiroom.reconcile.outputd_env",
        path=OUTPUTD_GROUPING_ENV_FILE,
        changed=env_changed,
        ok=env_ok,
        lane=outputd_env[DAC_CONTENT_LANE_ENV] or "(cleared)",
        channel=outputd_env[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] or "(cleared)",
    )
    if not env_ok:
        rc = 1
    # NO ring file to create on either bonded path: both the grouping ring and
    # the dac-content ring are ioplug rings whose C writer creates the file at
    # open, and the install leaves them alone on purpose
    # (deploy/lib/install/ring-platform.sh).

    # 2. CamillaDSP solo RESTORE — unwind a prior bond before units tear down.
    #    A box that will APPLY a bonded config below skips restore. An ACTIVE box
    #    restores its ACTIVE baseline, Layer A intact — NEVER a passive graph,
    #    which would be full-range to a tweeter.
    solo_restore_ok = True
    if active_leader or active_follower:
        pass
    elif box_is_active and prior_crossover_owned:
        # Unbond of an ACTIVE LEADER: camilla#2 (the crossover unit) is enabled
        # or active only after an active leader armed it. The pre-mutation gate
        # above has already disabled it and positively proved it inactive, so
        # camilla#1 can now reclaim the DAC via the leader stash.
        try:
            from .active_leader_config import restore_active_leader_solo_sync

            restored = restore_active_leader_solo_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_leader_solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_leader_restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1
    elif box_is_active:
        try:
            from .follower_config import restore_active_follower_solo_sync

            restored = restore_active_follower_solo_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1
    else:
        try:
            from .leader_config import restore_solo_config_sync

            restored = restore_solo_config_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1

    # 3. outputd picks up the lane env only at unit start. For an active leader,
    # defer that restart until camilla#1's program-bake graph is live and
    # camilla#2's statefile is seeded with the re-proven endpoint graph: the
    # audio-hardware reconciler needs that graph pair as evidence to switch
    # outputd to the active-content lane before camilla#2 is armed. Restarting
    # here would read the grouping TTS env but still use the solo baseline,
    # re-opening the passive lane camilla#2 needs.
    defer_outputd_restart = active_speaker_leader
    outputd_restart_ok = True
    if env_changed and env_ok and not defer_outputd_restart:
        outputd_restart_ok = _restart_outputd()
        if not outputd_restart_ok:
            rc = 1

    # 3b. Voice's grouping-derived env (TTS socket flip + park flag): written +
    # kick-on-change only — a voice restart costs ~10-15 s and must happen only
    # on a real bond/unbond, never on the routine no-change reconcile. The kick
    # goes to jasper-aec-reconcile, NOT jasper-voice directly: that script is the
    # single owner of the voice/bridge units and decides restart-vs-park from
    # this flag plus its own provider + mic gates.
    voice_env = voice_grouping_env(cfg, active_endpoint=active_endpoint)
    voice_changed, voice_ok = _write_derived_env(
        voice_env,
        path=VOICE_GROUPING_ENV_FILE,
        consumer="voice",
    )
    log_event(
        logger,
        "multiroom.reconcile.voice_env",
        path=VOICE_GROUPING_ENV_FILE,
        changed=voice_changed,
        ok=voice_ok,
        socket=voice_env.get(VOICE_TTS_SOCKET_ENV, "(solo: fanin default)"),
        park=voice_env.get(VOICE_PARK_ENV, "0"),
    )
    if not voice_ok:
        rc = 1
    voice_refresh_ok = True
    if (
        voice_changed
        and voice_ok
        and not (
            voice_refresh_ok := _restart_unit(
                AEC_RECONCILE_UNIT,
                no_block=True,
            )
        )
    ):
        rc = 1

    # 3c. shairport's bonded-leader AirPlay offset delta: written +
    # restart-on-change. The re-derivation itself happens in shairport's
    # ExecStartPre (jasper-apply-airplay-mode reads this file), so the restart in
    # step 4b is what applies it.
    airplay_env = airplay_grouping_env(cfg)
    airplay_changed, airplay_ok = _write_derived_env(
        airplay_env,
        path=AIRPLAY_GROUPING_ENV_FILE,
        consumer="airplay",
    )
    log_event(
        logger,
        "multiroom.reconcile.airplay_env",
        path=AIRPLAY_GROUPING_ENV_FILE,
        changed=airplay_changed,
        ok=airplay_ok,
        extra_delay_sec=airplay_env.get(AIRPLAY_BONDED_EXTRA_DELAY_ENV, "(solo)"),
    )
    if not airplay_ok:
        rc = 1

    # 4. The unit plan (stops before starts). Probed before it runs — see
    # _plan_changes_units — so the post-role source barrier below knows
    # whether this pass actually moved a unit.
    units_changed = _plan_changes_units(decision.intents)
    apply_rc = _apply(decision)
    rc = max(rc, apply_rc)

    # 4b. Re-derive shairport's backend latency offset on a bond/unbond that
    # changed it: a restart runs the ExecStartPre that reads
    # grouping-airplay.env. Skip a bonded FOLLOWER — the plan PARKED its
    # shairport and restarting would un-park it; a follower receives no AirPlay
    # anyway. One restart, only on a real offset change.
    is_bonded_follower = config.local_sources_parked(cfg)
    airplay_refresh_ok = True
    if airplay_changed and airplay_ok and not is_bonded_follower:
        # AirPlay may be household-Off. A plain restart ignores unit enablement
        # and would resurrect it after a leader bond/unbond; refresh only a
        # receiver that is already active.
        airplay_refresh_ok = _restart_unit(SHAIRPORT_UNIT, active_only=True)
        if not airplay_refresh_ok:
            rc = 1

    # 5. Bonded apply LAST (snapserver is up → the pipe has its reader; snapclient
    #    is up → the grouping ring has its writer).
    if passive_leader:
        try:
            from .leader_config import apply_bonded_leader_config_sync

            applied = apply_bonded_leader_config_sync(cfg)
            log_event(
                logger,
                "multiroom.reconcile.camilla",
                result="bonded",
                path=applied,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="bonded_apply",
                error=e,
                level=logging.ERROR,
            )
            rc = 1
    elif active_speaker_leader:
        # WIRE-UP GUARD — the single top-of-path precondition. The two-instance
        # setup is viable ONLY if the wire is up: camilla#1's bake writes a
        # File/FIFO sink that needs snapserver as its reader, and ONLY a
        # successful bake moves camilla#1 off the DAC so camilla#2 can take it.
        # If snapserver did not start, bail here and STAY SOLO-ACTIVE (camilla#1
        # keeps the DAC on its safe solo baseline, camilla#2 un-armed) —
        # otherwise the two instances fight for the DAC and camilla#1 exhausts
        # its recovery budget.
        if not _unit_is_active(SNAPSERVER_UNIT):
            log_event(
                logger,
                "multiroom.reconcile.active_leader_blocked",
                reason="snapserver_not_active",
                detail=(
                    "active-leader wire is down; staying solo-active "
                    "(camilla#1 keeps the DAC, camilla#2 un-armed)"
                ),
                level=logging.ERROR,
            )
            if not _disable_crossover_unit():
                rc = 1
            rc = 1
        else:
            # Wire is up. camilla#1 bakes to the now-readable pipe; THEN the
            # camilla#2 statefile is RE-SEEDED with the re-proven driver-domain
            # graph before audio-hardware reconcile sizes outputd's active lane.
            # Only if that bake and outputd env handoff succeed, and camilla#1 has
            # provably released the DAC, is camilla#2 armed onto it — the
            # never-flat guarantee. camilla#2 is disabled before the bake and
            # later started from that statefile, so trim-only rewrites are picked
            # up by process start rather than by an idempotent systemd no-op.
            bake_ok = False
            if not _disable_crossover_unit():
                rc = 1
            elif not _ensure_unit_active(CAMILLA_UNIT, reason="active-leader-bake"):
                rc = 1
            else:
                active_leader_action = "active_leader_bake_apply"
                try:
                    from .active_leader_config import (
                        apply_active_leader_bake_sync,
                        seed_crossover_statefile,
                    )

                    applied = apply_active_leader_bake_sync()
                    log_event(
                        logger,
                        "multiroom.reconcile.camilla",
                        result="active_leader_bake",
                        path=applied,
                    )
                    bake_ok = True
                    active_leader_action = "active_leader_crossover_seed"
                    seed_crossover_statefile()
                    if not _run_audio_hardware_reconcile(
                        reason="grouping-active-leader-bake",
                    ):
                        bake_ok = False
                        rc = 1
                except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
                    log_event(
                        logger,
                        "multiroom.reconcile.camilla_failed",
                        action=active_leader_action,
                        error=e,
                        level=logging.ERROR,
                    )
                    rc = 1
            # Arm camilla#2 ONLY when the bake provably moved camilla#1 off the
            # active-content PCM, outputd re-converged to the active lane, and
            # the exclusive handle positively released. A successful CamillaDSP
            # config reload is not enough: the transport can lag the actual
            # close, and arming into that window races EBUSY against camilla#1's
            # recovery-budget unit.
            if bake_ok:
                if _unit_is_active(CROSSOVER_UNIT):
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                        lock_path=ACTIVE_CONTENT_WRITER_LOCK_PATH,
                        result="already_armed",
                        reason="crossover_unit_active",
                    )
                else:
                    probe = _wait_for_active_content_pcm_release()
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                        lock_path=probe.lock_path,
                        result=probe.state,
                        reason=probe.reason,
                        detail=probe.detail or "(none)",
                        attempts=probe.attempts,
                        timeout_sec=probe.timeout_sec,
                        level=logging.WARNING if probe.unknown else logging.INFO,
                    )
                    if not probe.released:
                        # `busy` and `unknown` both fail closed to solo-active:
                        # arming without positive proof is the EBUSY reboot loop
                        # this barrier exists to prevent. See
                        # ACTIVE_CONTENT_WRITER_LOCK_PATH for which boxes reach
                        # `unknown` and why blocking is the honest answer there.
                        endpoint_block_reason = (
                            "active_content_pcm_busy"
                            if probe.busy
                            else "active_content_pcm_unverified"
                        )
                        active_leader_arm_blocked = True
                        log_event(
                            logger,
                            "multiroom.reconcile.active_leader_blocked",
                            reason=endpoint_block_reason,
                            detail=(
                                "active-content playback PCM not positively "
                                f"released after camilla#1 bake (state="
                                f"{probe.state}, reason={probe.reason}); "
                                "restoring solo-active and leaving camilla#2 "
                                "un-armed"
                            ),
                            pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                            lock_path=probe.lock_path,
                            probe_reason=probe.reason,
                            probe_detail=probe.detail or "(none)",
                            attempts=probe.attempts,
                            timeout_sec=probe.timeout_sec,
                            level=logging.ERROR,
                        )
                        try:
                            from jasper.camilla import CamillaUnavailable
                            from jasper.dsp_apply import DspApplyError

                            from .active_leader_config import (
                                restore_active_leader_solo_sync,
                            )

                            restored = restore_active_leader_solo_sync()
                            if restored:
                                log_event(
                                    logger,
                                    "multiroom.reconcile.camilla",
                                    result=(
                                        "active_leader_solo_restored_after_pcm_busy"
                                    ),
                                    path=restored,
                                )
                        except (
                            CamillaUnavailable,
                            DspApplyError,
                            OSError,
                            RuntimeError,
                            TimeoutError,
                            ValueError,
                        ) as e:
                            log_event(
                                logger,
                                "multiroom.reconcile.camilla_failed",
                                action="active_leader_pcm_busy_restore",
                                error=e,
                                level=logging.ERROR,
                            )
                        _write_follower_status(
                            active_follower=False,
                            active_leader=False,
                            blocked_reason=endpoint_block_reason,
                            requested_cfg=requested_cfg,
                            local_sources_allowed=(
                                not config.local_sources_parked(cfg)
                                and not transitioning_from_parked_role
                            ),
                            path=FOLLOWER_STATUS_FILE,
                        )
                        rc = 1
                    elif not _arm_crossover_unit():
                        rc = 1
            else:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_leader_crossover_arm_skipped",
                    reason="crossover_not_ready",
                )
                rc = 1

    if active_leader and not active_leader_arm_blocked:
        # 6. The stream-binding pin (ANY leader hosts the stream; runs after the
        # camilla apply so snapserver has had its longest warm-up): re-bind every
        # PERSISTED snapcast group to our stream, because a stale server.json
        # binding silently mutes the whole bond behind green health. The ensure
        # retries internally; an unreachable snapserver flips the exit code (a
        # bond whose bindings cannot be verified is a degraded bond).
        from .snapcast_rpc import ensure_groups_on_stream

        report = ensure_groups_on_stream(SNAP_STREAM_ID)
        log_event(
            logger,
            "multiroom.reconcile.stream_binding",
            reachable=report["reachable"],
            groups=report["groups"],
            fixed=report["fixed"],
            failed=report["failed"],
            want=SNAP_STREAM_ID,
        )
        if not report["reachable"] or report["failed"]:
            rc = 1

    # 5b. Active FOLLOWER CamillaDSP swap LAST (snapclient is up → the grouping
    #     ring has its writer, so CamillaDSP locks immediately). The graph was
    #     built + re-proven by the readiness gate above, so no capture content
    #     (stream / silence / garbage) can produce a full-range driver feed. A
    #     swap failure here keeps CamillaDSP on its prior safe solo-active graph;
    #     the next reconcile retries.
    if active_follower:
        try:
            from .follower_config import apply_prebuilt_follower_config_sync

            applied = apply_prebuilt_follower_config_sync()
            log_event(
                logger,
                "multiroom.reconcile.camilla",
                result="active_follower",
                path=applied,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_follower_apply",
                error=e,
                level=logging.ERROR,
            )
            rc = 1

    # A requested follower may have been refused and safely resolved to solo.
    # Publish that effective permission only AFTER every load-bearing solo
    # transition has succeeded: until this point the earlier status explicitly
    # denies local sources, so a concurrent systemd start cannot enter while the
    # old follower graph is still live. Any failed restore/file/unit step leaves
    # the fail-safe deny in place for the next reconcile to repair.
    source_grant_pending = refused_follower_fallback or transitioning_from_parked_role
    if source_grant_pending:
        transition_landed = all(
            (
                wrote,
                env_ok,
                solo_restore_ok,
                outputd_restart_ok,
                voice_ok,
                voice_refresh_ok,
                airplay_ok,
                airplay_refresh_ok,
                apply_rc == 0,
            )
        )
        # A refused follower intentionally returns nonzero for the rejected bond
        # even after its safe solo fallback landed. An ordinary
        # follower->solo/leader transition has no such expected error: every
        # later role-specific step must also have succeeded before sources can be
        # granted.
        if transitioning_from_parked_role and not refused_follower_fallback:
            transition_landed = transition_landed and rc == 0
        if transition_landed:
            grant_published = _write_follower_status(
                active_follower=False,
                active_leader=(
                    active_speaker_leader if transitioning_from_parked_role else False
                ),
                blocked_reason=(
                    endpoint_block_reason if refused_follower_fallback else ""
                ),
                requested_cfg=requested_cfg,
                local_sources_allowed=True,
                path=FOLLOWER_STATUS_FILE,
            )
            if not grant_published:
                rc = 1
                log_event(
                    logger,
                    (
                        "multiroom.reconcile.fallback_source_grant_failed"
                        if refused_follower_fallback
                        else "multiroom.reconcile.role_transition_grant_failed"
                    ),
                    reason=endpoint_block_reason,
                    action="sources_remain_parked",
                    level=logging.ERROR,
                )
        else:
            log_event(
                logger,
                (
                    "multiroom.reconcile.fallback_sources_parked"
                    if refused_follower_fallback
                    else "multiroom.reconcile.role_transition_sources_parked"
                ),
                reason=endpoint_block_reason,
                action="retry_reconcile",
                level=logging.ERROR,
            )

    # 7. Hand the completed role to the one source owner. It reads grouping
    # permission fresh and performs follower park or solo/leader restore for all
    # sources, including USB's arm -> advertise -> start sequence.
    if not _converge_sources_after_role(
        grouping_active=active,
        units_changed=units_changed,
    ):
        rc = 1

    log_event(logger, "multiroom.reconcile.done", rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
