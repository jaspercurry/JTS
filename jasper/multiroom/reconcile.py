# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom grouping reconciler — pure plan + thin systemctl entrypoint.

The reconciler is the single writer of the snapcast unit state and the
applier of role-derived local-source parking. It reads the wizard-owned
GroupingConfig (see jasper.multiroom.config) and decides which units should
be running:

  - solo / grouping OFF        => neither snapserver nor snapclient runs.
  - grouping ON but INVALID    => neither runs (fail-safe: never bring up a
                                  broken bond; the doctor surfaces the error).
  - ON, valid, role=leader     => snapserver + snapclient (the leader hosts
                                  the stream AND plays its own channel).
  - ON, valid, role=follower   => snapclient only (consumes the leader's
                                  stream) and local source resource groups
                                  are parked via jasper.local_sources.

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

import argparse
import json
import logging
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .. import atomic_io
from .. import tts_routing as _tts_routing
from ..log_event import log_event
from ..local_sources import local_source_park_units, local_source_restore_units
from .config import (
    GroupingConfig,
    is_active_leader,
    load_config,
    local_sources_parked,
)
from .tts_route import VOICE_PARK_ENV, expected_grouping_tts_route

logger = logging.getLogger(__name__)

OUTPUTD_TTS_SOCKET = _tts_routing.OUTPUTD_TTS_SOCKET
OUTPUTD_TTS_SOCKET_ENV = _tts_routing.OUTPUTD_TTS_SOCKET_ENV
VOICE_TTS_SOCKET_ENV = _tts_routing.VOICE_TTS_SOCKET_ENV


# ---------- Unit names (single source of truth) ----------

SNAPSERVER_UNIT = "jasper-snapserver.service"
SNAPCLIENT_UNIT = "jasper-snapclient.service"
# shairport-sync is the AirPlay receiver. A FOLLOWER parks it (below); a
# LEADER keeps it running and gets its backend latency offset re-derived
# on bond/unbond (a bonded leader folds in the Snapcast round-trip buffer
# — see airplay_grouping_env + main()).
SHAIRPORT_UNIT = "shairport-sync.service"

# ---------- Snapcast wiring constants ----------

# The FIFO the fan-in chain writes the mixed stereo program into and
# snapserver reads from as its pipe source. Lives in snapserver's OWN
# per-unit runtime dir (/run/jasper-snapserver/, RuntimeDirectory=
# jasper-snapserver). A unit's RuntimeDirectory is reaped when it stops;
# sharing runtime directories would let snapserver stopping destroy another
# daemon's sockets. tmpfs-backed, recreated each boot.
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

# ---------- the ACTIVE follower round-trip loopback (distributed-active Slice 3) ----------
#
# An ACTIVE (multi-driver) follower cannot use the dumb-follower
# `dac_content` FIFO path — its CamillaDSP must run Layer A (the crossover)
# in the bonded audio path. So instead of the FIFO, snapclient writes a
# private snd-aloop substream and the follower's CamillaDSP captures the
# paired side, rate-tracking it bit-perfectly (enable_rate_adjust, no
# resampler — the proven S0-sync seam). This is DELIBERATELY snd-aloop here
# (not the inv-2 FIFO): the active follower needs the loopback's clock for
# CamillaDSP's `rate_adjust` to track, and the fixed CamillaDSP pipeline
# latency is nulled by snapclient `--latency` (HANDOFF-distributed-active.md
# "Clock domain"). An active follower runs TWO loopback hops that must NOT
# collide: (1) snapclient -> camilla [this grouping round-trip], and (2) camilla
# -> outputd's active-content lane = substream 5 (`outputd_active_content_*`).
# So the round-trip must use a DIFFERENT pair from 5. snd_aloop caps
# `pcm_substreams` at 8 (pairs 0-7 — a 9th is silently clamped, verified on
# jts3), so a dedicated extra pair is impossible without a second card (which
# reintroduces the removed-LoopbackAEC wedge risk). The round-trip therefore
# rides pair 6 — the PASSIVE stereo content lane (`outputd_content_*`). That is
# safe to share by a hard hardware-mode invariant: an active follower's outputd
# is ALWAYS Composite (active topology -> Composite sink -> it reads the
# active-content lane on pair 5) and NEVER opens the passive content lane on
# pair 6; a passive box that DOES use pair 6 is never an active follower. The
# full allocation: 0-4 renderers, 5 active-content, 6 passive-content / active-
# follower round-trip, 7 fan-in. No reboot needed (pair 6 always exists). Both
# sides are env-overridable for on-device tuning.
#
#   snapclient (writer)  --player alsa --soundcard <PLAYBACK>  -> hw:Loopback,0,6
#   CamillaDSP (reader)  capture device              <CAPTURE>  <- hw:Loopback,1,6
GROUPING_LOOPBACK_PLAYBACK = os.environ.get(
    "JASPER_GROUPING_LOOPBACK_PLAYBACK", "hw:Loopback,0,6",
)
GROUPING_LOOPBACK_CAPTURE = os.environ.get(
    "JASPER_GROUPING_LOOPBACK_CAPTURE", "hw:Loopback,1,6",
)
# snapclient decodes to the snapserver-pinned 48 kHz / S16 / stereo, so the
# follower's CamillaDSP captures the loopback as S16_LE (the S0-sync bench
# format) — raw hw:, no plug/resampler, so the bit-perfect rate-track holds.
GROUPING_LOOPBACK_CAPTURE_FORMAT = "S16_LE"

# The active-follower endpoint STATUS file — the reconciler's fresh truth for
# /state + the dashboard (read by jasper.multiroom.state, never os.environ,
# because jasper-control is not restarted on a bond). Persistent (a bond
# survives reboots). Rewritten on every reconcile so a stale file can never
# claim an active-follower mode that is no longer current. mode 0644, no secret.
FOLLOWER_STATUS_FILE = "/var/lib/jasper/grouping-follower-status.json"
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
OUTPUTD_DAC_CONTENT_TRIM_ENV = "JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB"
# Receiver-side wireless-sub low-pass corner (Hz). Emitted ONLY when this
# member's channel is "sub" — outputd's "sub" ChannelPick reads it to build
# its LR4 low-pass. ABSENT for every other channel (a non-sub member must
# never carry it), so outputd defaults to its safe 80 Hz only if a sub
# somehow lacks it. The single writer is outputd_grouping_env.
OUTPUTD_DAC_CONTENT_SUB_HZ_ENV = "JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ"
# Receiver-side wireless-sub bass-management high-pass corner (Hz). Emitted
# for non-sub MAIN members only when this bond is known to contain a sub and
# the per-bond toggle is on. Empty everywhere else so stale env can never leave
# a main bass-light without a sub.
OUTPUTD_DAC_CONTENT_HP_HZ_ENV = "JASPER_OUTPUTD_DAC_CONTENT_HP_HZ"
OUTPUTD_UNIT = "jasper-outputd.service"
CAMILLA_UNIT = "jasper-camilla.service"

# Voice-side grouping route: a reconciler-owned PERSISTENT env file layered LAST
# in jasper-voice.service. The TTS route matrix decides whether this file points
# voice at outputd, parks voice/AEC, or omits the socket so voice falls back to
# fan-in. Omission is intentional: present-but-empty is read as a real, invalid
# path. Same never-empty lesson as the CONTENT_BRIDGE pin.
VOICE_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-voice.env"
VOICE_UNIT = "jasper-voice.service"

# Reconciler-owned PERSISTENT env file the shairport-sync unit's
# ExecStartPre (jasper-apply-airplay-mode) layers when deriving the AirPlay
# backend latency offset. Holds the bonded-leader-only Snapcast round-trip
# delay; EMPTY (no keys) for solo/follower so the offset stays
# byte-identical to the solo value (and an empty body avoids a spurious
# shairport restart on a fresh solo speaker — see _write_outputd_env).
# Persistent (NOT /run) so a bonded leader boots with the bonded offset
# already derived. mode 0644, no secret.
AIRPLAY_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-airplay.env"
AIRPLAY_BONDED_EXTRA_DELAY_ENV = "JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC"

# jasper-aec-reconcile is the SINGLE owner of jasper-voice +
# jasper-aec-bridge unit state (it already parks voice when the provider
# is unconfigured). Role changes therefore KICK it rather than touching
# those units here — it reads the derived park flag below and
# restarts-or-parks voice per role + provider + mic, one writer total.
AEC_RECONCILE_UNIT = "jasper-aec-reconcile.service"
AUDIO_HARDWARE_RECONCILE = "/usr/local/sbin/jasper-audio-hardware-reconcile"

# camilla#2 — the endpoint-crossover CamillaDSP instance (:1235), armed ONLY on
# an ACTIVE LEADER (HANDOFF-distributed-active.md "Stage B — the ratified
# active-leader realization"). Reconciler-gated: `enable --now` on bond (after
# the statefile is re-seeded with the re-proven driver-domain graph) and
# `disable --now` on unbond. INERT/dormant infrastructure otherwise (PR #930).
# It carries NO StartLimitAction=reboot, so a failed arm fails closed to silence
# through the crossover — never reboots the household speaker (unlike the
# always-on camilla#1).
CROSSOVER_UNIT = "jasper-camilla-crossover.service"

# The exclusive active-content PCM camilla#1 owns in solo-active mode and
# camilla#2 owns after the active-leader handoff. `outputd_active_content_*`
# resolves to raw snd-aloop pair 5 (see deploy/alsa/asoundrc.jasper), so the
# per-substream `/proc/asound` status is the only cheap positive release signal:
# the shared `/dev/snd/pcmC*D0p` node covers every playback substream and would
# be a false "busy" while renderers hold 0..4 or fan-in holds 7.
ACTIVE_CONTENT_PLAYBACK_PCM = "hw:Loopback,0,5"
ACTIVE_CONTENT_PLAYBACK_STATUS_PATH = "/proc/asound/Loopback/pcm0p/sub5/status"
ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC = 0.8
ACTIVE_CONTENT_RELEASE_POLL_SEC = 0.05


@dataclass(frozen=True)
class _PcmHandleProbeResult:
    """One bounded active-content PCM release probe result."""

    state: str  # "released" | "busy" | "unknown"
    reason: str
    detail: str = ""
    status_path: str = ACTIVE_CONTENT_PLAYBACK_STATUS_PATH
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

    `desired` is one of {"start", "stop", "restore"}; `reason` is a
    short human-readable explanation for the log line. "restore" means
    start ONLY if the unit is systemd-enabled — the shape that puts a
    parked source resource back exactly per the /sources/ wizard's intent
    (a wizard-disabled source must stay off after an unbond).
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
    restore_renderers = tuple(
        UnitIntent(u, "restore", "not a bonded follower — sources per wizard")
        for u in local_source_restore_units()
    )

    if not cfg.enabled:
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "stop", "grouping off"),
                UnitIntent(SNAPCLIENT_UNIT, "stop", "grouping off"),
            ) + restore_renderers,
            summary="grouping off (solo)",
        )

    if cfg.error is not None:
        # Fail-safe to SOLO behavior: a broken bond must not keep the
        # household's sources parked on top of not playing.
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "stop", "config invalid"),
                UnitIntent(SNAPCLIENT_UNIT, "stop", "config invalid"),
            ) + restore_renderers,
            summary=(
                f"grouping enabled but INVALID: {cfg.error} — not starting"
            ),
        )

    if cfg.role == "leader":
        return ReconcilePlan(
            intents=(
                UnitIntent(SNAPSERVER_UNIT, "start", "leader hosts stream"),
                UnitIntent(SNAPCLIENT_UNIT, "start", "leader plays its channel"),
            ) + restore_renderers,
            summary=f"grouping leader (bond {cfg.bond_id}, channel {cfg.channel})",
        )

    # role-policy says local sources are parked (today: valid bonded
    # follower). The dumb-follower profile stops whole source resource
    # groups, including advertise-side units such as the USB gadget init.
    parked = tuple(
        UnitIntent(u, "stop", "parked (bonded follower)")
        for u in local_source_park_units()
    )
    return ReconcilePlan(
        intents=(
            UnitIntent(SNAPSERVER_UNIT, "stop", "follower runs no server"),
        ) + parked + (
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

    PURE: a deterministic function of `cfg`. snapserver reads the mixed
    program from the SNAPFIFO pipe source and streams it with the
    configured codec and the group/network playout buffer (cfg.buffer_ms,
    passed as the global ``--stream.buffer``).
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
    )
    # buffer_ms is the GLOBAL `--stream.buffer` flag (snapcast's end-to-end
    # capture->playout latency), NOT a `pipe://?...&buffer_ms=` source-URL
    # query param. snapcast's pipe-source parser reads only name/mode/
    # sampleformat/codec/chunk_ms and SILENTLY IGNORES an unknown query
    # key, so a `&buffer_ms=` value is inert — the bond would run
    # snapcast's 1000 ms default regardless. Do NOT move this back into the
    # source URL (that was the latent bug: a configured 400 ms bond
    # actually buffered 1000 ms).
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
    player_fifo: str | None = None,
    player_alsa_device: str | None = None,
) -> list[str]:
    """Build the snapclient command line from a GroupingConfig.

    PURE: a deterministic function of `cfg` (+ the optional `player_fifo`).
    The host is the loopback when this speaker is the leader (it runs its own
    server), otherwise the leader's address. The ``--latency`` value is
    the fixed client PCM/output-path latency compensation, not the group
    stream buffer.

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

    ``player_alsa_device`` (distributed-active Slice 3 — the ACTIVE follower):
    when set, snapclient writes to that ALSA device via its ``alsa`` player
    (``--soundcard <dev> --player alsa``). An active follower's CamillaDSP runs
    Layer A in the bonded path, so it captures the paired side of this snd-aloop
    loopback and rate-tracks it (the ``snd_pcm_delay`` trap is avoided not by
    dodging snd-aloop but by CamillaDSP owning the clock + ``--latency`` nulling
    the fixed pipeline latency — HANDOFF-distributed-active.md "Clock domain").
    Mutually exclusive with ``player_fifo`` (active vs dumb follower); the bench
    proved ``--soundcard hw:Loopback,0,S --player alsa``.
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
        str(cfg.client_latency_ms),
    ]
    if player_alsa_device:
        argv += ["--soundcard", player_alsa_device, "--player", "alsa"]
    elif player_fifo:
        argv += ["--player", f"file:filename={player_fifo}"]
    return argv


def _assemble_args(
    cfg: GroupingConfig, *, active_endpoint: bool = False,
) -> dict[str, str]:
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
    if active_endpoint:
        # ACTIVE follower (Slice 3): snapclient writes the round-trip snd-aloop
        # loopback; this box's CamillaDSP captures the paired side and runs
        # Layer A (the crossover) IN the bonded path. The dac_content FIFO lane
        # is NOT used — camilla owns the channel-pick + split.
        client = _join_args(
            snapclient_argv(cfg, player_alsa_device=GROUPING_LOOPBACK_PLAYBACK)
        )
    else:
        # DUMB member: snapclient writes the round-trip FIFO (the `file`
        # player) — never an ALSA sink, which would fight outputd for the DAC
        # (the observed `Device or resource busy` failure of the pre-Increment-5
        # bond). outputd reads the FIFO via its dac_content lane (Increment 3)
        # and picks this member's channel there.
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


def outputd_grouping_env(
    cfg: GroupingConfig, *, active_endpoint: bool = False,
) -> dict[str, str]:
    """The outputd round-trip lane env derived from a GroupingConfig. PURE.

    A DUMB ACTIVE member (enabled + valid, either role, single-DAC) plays the
    round-tripped stream: outputd reads ``MEMBER_CONTENT_FIFO`` and
    picks this speaker's channel (Increment 3's ``ChannelPick``; the
    channel-split vocabulary). Everyone else gets EMPTY strings — which
    outputd's ``env_optional`` reads as unset, i.e. the byte-identical
    solo loop — so a stale file can never half-configure the lane
    (mirrors ``_assemble_args``'s disable-clears-stale idiom).

    ``active_endpoint`` (distributed-active Slice 3 — the ACTIVE follower, plus
    the active leader's own drivers): DISABLES the ``dac_content`` ChannelPick
    on this box. CamillaDSP owns BOTH the channel-pick and the ``2->N`` split
    (Layer A), so outputd just runs its normal active sink fed by camilla — no
    FIFO, no ChannelPick. This is the real capability that replaces the
    ``dac_content_lane_rejects_non_single_alsa_sink`` fail-closed: the active
    sink is now a legitimate bonded member (via CamillaDSP, not the dac_content
    lane).

    Active-mode TTS deliberately stays upstream of the crossover in fan-in. The
    outputd TTS mixer is stereo-only and post-crossover; on an active lane, a
    2-way speaker is also "2 channels", so arming that socket would send
    full-range assistant audio to the tweeter. Active endpoints therefore clear
    the outputd TTS socket along with the dac_content lane.

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
    route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)

    if cfg.enabled and cfg.error is None:
        if active_endpoint:
            return {
                OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
                OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
                OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
                OUTPUTD_DAC_CONTENT_HP_HZ_ENV: "",
                # Empty = unset to outputd's env_f32 (default 0.0).
                OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
            }
        sub_present = (
            cfg.subwoofer_present
            or cfg.channel == "sub"
            or any(m.channel == "sub" for m in cfg.roster)
        )
        main_highpass_hz = (
            str(cfg.crossover_hz)
            if (
                cfg.mains_highpass_enabled
                and sub_present
                and cfg.channel != "sub"
            )
            else ""
        )
        env = {
            OUTPUTD_DAC_CONTENT_FIFO_ENV: MEMBER_CONTENT_FIFO,
            OUTPUTD_DAC_CONTENT_CHANNEL_ENV: cfg.channel or "stereo",
            OUTPUTD_CONTENT_BRIDGE_ENV: "direct",
            OUTPUTD_TTS_SOCKET_ENV: route.outputd_tts_socket,
            OUTPUTD_DAC_CONTENT_HP_HZ_ENV: main_highpass_hz,
            # Pair-balance trim (validated <= 0 by load_config; outputd
            # re-validates fail-closed). Always written while bonded so
            # a cleared trim converges back to 0.0.
            OUTPUTD_DAC_CONTENT_TRIM_ENV: f"{cfg.trim_db:.1f}",
        }
        # Receiver-side wireless-sub corner: emitted ONLY for channel="sub"
        # (the LR4 low-pass corner outputd's "sub" pick applies). Absent for
        # every other channel — a non-sub member must never carry it.
        if cfg.channel == "sub":
            env[OUTPUTD_DAC_CONTENT_SUB_HZ_ENV] = str(cfg.crossover_hz)
            # A sub plays only low-passed bass and NEVER voice. outputd mixes
            # TTS/cues AFTER the ChannelPick low-pass, so an armed TTS lane on a
            # sub would emit FULL-RANGE speech to the subwoofer. A sub is always
            # a follower, whose voice is parked today (nothing feeds the socket),
            # but clear it so that hazard cannot exist by construction — same
            # disable-clears-stale idiom as the off path below (empty = unset to
            # outputd, so no TTS server is constructed on a sub).
            env[OUTPUTD_TTS_SOCKET_ENV] = route.outputd_tts_socket
        return env
    return {
        OUTPUTD_DAC_CONTENT_FIFO_ENV: "",
        OUTPUTD_DAC_CONTENT_CHANNEL_ENV: "",
        OUTPUTD_TTS_SOCKET_ENV: "",
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV: "",
        # Empty = unset to outputd's env_f32 (default 0.0) — the same
        # disable-clears-stale idiom as the lane keys above.
        OUTPUTD_DAC_CONTENT_TRIM_ENV: "",
    }


def voice_grouping_env(
    cfg: GroupingConfig, *, active_endpoint: bool = False,
) -> dict[str, str]:
    """jasper-voice's grouping-derived env. PURE.

    The route matrix owns the policy. Passive non-sub members point voice's TTS
    playout socket at outputd so each member's OWN replies mix at its OWN final
    output; inv-3 keeps the leader's TTS out of the SHARED stream. Active
    endpoints and sub routes fail closed to fan-in or park, with outputd TTS
    unarmed. Solo also returns an EMPTY dict — the key is omitted, never
    present-but-empty (a set-empty value would be read as a real, invalid socket
    path).
    """
    route = expected_grouping_tts_route(cfg, active_endpoint=active_endpoint)
    if cfg.enabled and cfg.error is None:
        env = (
            {}
            if route.voice_env_socket is None
            else {VOICE_TTS_SOCKET_ENV: route.voice_env_socket}
        )
        if route.voice_parked:
            # Parked routes stop voice (and the AEC stack) through the
            # validated cross-language contract jasper-aec-reconcile gates on.
            # The route matrix still owns any socket override separately, so
            # active endpoints and sub followers can fail closed without
            # duplicating those safety rules here.
            env[VOICE_PARK_ENV] = "1"
        return env
    return {}


def airplay_grouping_env(cfg: GroupingConfig) -> dict[str, str]:
    """shairport's bonded-leader AirPlay latency-offset delta. PURE.

    Only an ACTIVE bonded LEADER both receives AirPlay AND plays its own
    channel through the Snapcast round-trip ("a follower of itself"), so
    only a leader's shairport must fold the Snapcast playout buffer into
    its backend latency offset to keep the leader's OWN output landing on
    the AirPlay anchor (lip-sync). Everyone else — solo, follower
    (shairport parked), invalid — gets an EMPTY dict, which clears the file
    to the byte-identical solo offset (the disable-clears-stale idiom). An
    empty body (no keys) also avoids a spurious shairport restart on a
    fresh solo speaker's first reconcile (see _write_outputd_env's
    old-is-None-and-empty guard).

    The value is the Snapcast buffer in seconds — the DOMINANT new delay
    the bonded leader's own output gains over solo. It is deliberately a
    first-order estimate: the solo offset's fan-in / CamillaDSP / outputd
    terms still apply in the bonded path, and the residual (CamillaDSP
    pipe-sink fill, the member content FIFO) is second-order and
    acoustically calibrated alongside snapclient --latency.
    jasper-apply-airplay-mode ADDS this to the solo-derived offset.
    """
    if is_active_leader(cfg):
        return {AIRPLAY_BONDED_EXTRA_DELAY_ENV: f"{cfg.buffer_ms / 1000:.6f}"}
    return {}


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

def is_active_speaker_box() -> bool:
    """True when this speaker is a commissioned ACTIVE (multi-driver) speaker —
    its saved output topology declares active 2-/3-way main groups. This is the
    branch signal that splits the ACTIVE-follower path (CamillaDSP runs Layer A
    in the bonded path) from the DUMB-follower path (outputd ChannelPick).

    TOTAL + fail-soft: any load/parse failure resolves to ``False`` (treat as
    passive → the safe dumb-follower path). Commissioning READINESS is NOT
    checked here — a box that declares active groups but is not yet commissioned
    still takes the active path, where the follower apply fail-closes (no ready
    baseline → refuse to bond) rather than silently degrading to a full-range
    dumb follower."""
    try:
        from jasper.active_speaker.playback_route import (
            active_playback_route_capability,
        )
        from jasper.output_topology import load_output_topology

        topology = load_output_topology()
        return active_playback_route_capability(topology).active_group_count > 0
    except Exception:  # noqa: BLE001 — fail-soft to the passive path
        return False


def _unit_is_enabled(unit: str) -> bool:
    """`systemctl is-enabled --quiet` truth for the restore intent.

    Anything other than rc=0 (disabled, static, masked, NOT-FOUND,
    systemctl missing) reads as not-enabled — restore then skips, which
    is the safe direction on every shape: a wizard-disabled source
    stays off, and a never-installed unit is silently not started.
    """
    try:
        return subprocess.run(
            ["systemctl", "is-enabled", "--quiet", unit],
            capture_output=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


def _unit_is_active(unit: str) -> bool:
    """`systemctl is-active --quiet` truth. Anything other than rc=0 (inactive,
    failed, NOT-FOUND, systemctl missing) reads as not-active — the safe
    direction for the active-leader bake gate: a bake against a reader-less /
    missing snapserver pipe must NOT proceed (it cannot release the DAC, so
    arming camilla#2 would fight camilla#1 for it — the 2026-06-23 recovery
    loop)."""
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            capture_output=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


def _probe_active_content_pcm_once(
    *,
    status_path: str = ACTIVE_CONTENT_PLAYBACK_STATUS_PATH,
    run=subprocess.run,
    probe_timeout_sec: float = 0.5,
) -> _PcmHandleProbeResult:
    """Read snd-aloop's per-substream status once.

    `/proc/asound/Loopback/pcm0p/sub5/status` reads exactly `closed` when
    `hw:Loopback,0,5` has no opener. Any open status (RUNNING, PREPARED, etc.)
    means the exclusive PCM is still held. A missing probe tool is the one
    fail-soft case: older/minimal installs should not crash the reconciler just
    because this positive barrier cannot run.
    """
    try:
        proc = run(
            ["cat", status_path],
            capture_output=True,
            text=True,
            timeout=probe_timeout_sec,
        )
    except FileNotFoundError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "probe_tool_missing",
            detail=str(e),
            status_path=status_path,
        )
    except subprocess.TimeoutExpired as e:
        return _PcmHandleProbeResult(
            "busy",
            "probe_timeout",
            detail=str(e),
            status_path=status_path,
        )
    except OSError as e:
        return _PcmHandleProbeResult(
            "busy",
            "probe_error",
            detail=str(e),
            status_path=status_path,
        )

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"rc={proc.returncode}"
        return _PcmHandleProbeResult(
            "busy",
            "status_unavailable",
            detail=detail,
            status_path=status_path,
        )

    status = (proc.stdout or "").strip()
    if status.lower() == "closed":
        return _PcmHandleProbeResult(
            "released",
            "status_closed",
            detail=status,
            status_path=status_path,
        )
    if not status:
        return _PcmHandleProbeResult(
            "busy",
            "status_empty",
            status_path=status_path,
        )
    first_line = status.splitlines()[0].strip()
    return _PcmHandleProbeResult(
        "busy",
        "status_open",
        detail=first_line,
        status_path=status_path,
    )


def _wait_for_active_content_pcm_release(
    *,
    timeout_sec: float = ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC,
    interval_sec: float = ACTIVE_CONTENT_RELEASE_POLL_SEC,
    status_path: str = ACTIVE_CONTENT_PLAYBACK_STATUS_PATH,
    run=subprocess.run,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> _PcmHandleProbeResult:
    """Poll until camilla#1 has positively released the active-content PCM.

    Returns `busy` on timeout/still-open and `unknown` only for the fail-soft
    case (probe tool missing). The caller arms camilla#2 ONLY on a positive
    `released`; both `busy` and `unknown` fail closed to solo-active (it logs
    `unknown` at WARNING since arming without proof risks the EBUSY reboot
    loop). `unknown` is theoretical-only — `cat` is universal on a real Pi.
    """
    deadline = monotonic() + max(timeout_sec, 0.0)
    attempts = 0
    last = _PcmHandleProbeResult(
        "busy",
        "not_probed",
        status_path=status_path,
        timeout_sec=timeout_sec,
    )
    while True:
        attempts += 1
        last = _probe_active_content_pcm_once(
            status_path=status_path,
            run=run,
        )
        if not last.busy:
            return replace(last, attempts=attempts, timeout_sec=timeout_sec)
        now = monotonic()
        if now >= deadline:
            detail = last.detail
            if last.reason != "status_open":
                detail = f"{last.reason}: {detail}" if detail else last.reason
            return _PcmHandleProbeResult(
                "busy",
                "timeout",
                detail=detail,
                status_path=status_path,
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


def _apply(plan_: ReconcilePlan) -> int:
    """Apply a plan via systemctl. Returns a process exit code.

    Intent kinds: `start` / `stop` map to systemctl verbs; `restore`
    is start-only-if-enabled (the un-park shape — /sources/ keeps
    enable/disable as the household's intent, so a parked source resource
    comes back exactly per the wizard). A failure on one intent is
    logged and surfaced in the exit code but does not abort the rest of
    the plan — a half-applied bond is worse than a best-effort one.
    Units that do not exist on this install tier are clean no-ops.
    """
    rc = 0
    for it in plan_.intents:
        verb = it.desired
        if verb == "restore":
            if not _unit_is_enabled(it.unit):
                log_event(
                    logger,
                    "multiroom.reconcile.unit",
                    unit=it.unit,
                    desired="restore",
                    result="skipped_not_enabled",
                    reason=it.reason,
                )
                continue
            verb = "start"
        try:
            subprocess.run(
                ["systemctl", verb, it.unit],
                check=True,
                capture_output=True,
                text=True,
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
    if old is None and body == "":
        # Nothing existed and nothing needs clearing — a fresh solo
        # speaker's first reconcile must not count as a change (it
        # would spuriously restart the consuming unit, e.g. a ~15 s
        # jasper-voice restart on first boot for an empty file).
        return (False, True)
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.outputd_env_failed",
            path=path,
            error=e,
            level=logging.WARNING,
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
            log_event(
                logger,
                "multiroom.reconcile.fifo_replaced",
                path=path,
                detail="non-FIFO squatter removed",
                level=logging.WARNING,
            )
            os.unlink(path)
            st = None
        if st is None:
            os.mkfifo(path, 0o600)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.fifo_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return False
    return True


def _reset_failed_unit(unit: str) -> None:
    """`systemctl reset-failed <unit>` before a DELIBERATE reconciler restart.

    The reconciler's restarts are control-plane CONFIG-APPLIES, not crash
    recovery. A rapid burst of /grouping/set updates — e.g. an active-crossover
    calibration / trim / delay sweep on the leader re-fanned to a follower —
    legitimately re-derives the lane env many times in seconds, and each apply
    spends a slot of the target unit's StartLimitBurst. Once that burst is
    exhausted inside StartLimitIntervalSec, systemd escalates to
    StartLimitAction=reboot for direct reboot-budget units (outputd / voice) or
    Camilla's recovery budget, turning deliberate churn into recovery
    escalation — the 2026-06-24 jts.local follower reboot (six /grouping/set
    POSTs from the leader in 44 s tripped outputd's start-limit). reset-failed
    clears any prior failed / start-limit parking so a config-apply restart
    never consumes the crash-recovery budget.
    Genuine crash loops still escalate: the daemon's own Restart= path does NOT
    call this, so only reconciler-initiated (deliberate) restarts are exempted.

    Fail-soft and BEST-EFFORT: a reset-failed failure must never block the
    restart it precedes. Mirrors grouping_supervisor.kick_reconciler and
    shairport_supervisor.restart_shairport, which reset-failed the same way."""
    try:
        subprocess.run(
            ["systemctl", "reset-failed", unit],
            check=False, capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError) as e:
        log_event(
            logger,
            "multiroom.reconcile.reset_failed_error",
            unit=unit,
            error=e,
            level=logging.WARNING,
        )


def _restart_unit(unit: str) -> bool:
    """Restart a unit so it re-reads its grouping env. Fail-soft (a
    failure is logged + reflected in the exit code by the caller; the
    doctor's drift checks surface a lane left unwired).

    reset-failed FIRST (see :func:`_reset_failed_unit`) so a config-apply
    restart never spends the target's crash-reboot budget — this is the single
    guard that turns a rapid grouping-config burst into harmless restarts
    instead of a Pi reboot."""
    _reset_failed_unit(unit)
    try:
        subprocess.run(
            ["systemctl", "restart", unit],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
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
    )
    return True


def _ensure_unit_active(unit: str, *, reason: str) -> bool:
    """Start a required unit after clearing a stale start-limit state.

    Active-leader self-healing can intentionally stop camilla#2 to release the
    active-content lane. If camilla#1 previously hit StartLimit while camilla#2
    held that lane, a plain ``systemctl start`` remains parked until
    ``reset-failed`` runs. Keep this helper narrow and explicit so the common
    restore/restart paths retain their existing semantics.
    """
    if _unit_is_active(unit):
        return True
    try:
        subprocess.run(
            ["systemctl", "reset-failed", unit],
            check=False, capture_output=True, text=True,
        )
        subprocess.run(
            ["systemctl", "start", unit],
            check=True, capture_output=True, text=True,
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
    log_event(
        logger,
        "multiroom.reconcile.unit_started",
        unit=unit,
        reason=reason,
    )
    return True


def _run_audio_hardware_reconcile(*, reason: str) -> bool:
    """Run the audio-hardware reconciler after an active-leader graph change.

    That reconciler is the single writer of /var/lib/jasper/outputd.env. The
    active-leader bake changes camilla#1 from the solo active baseline to the
    program-bake pipe, and the freshly seeded camilla#2 statefile names the
    endpoint graph. Outputd must then switch from the passive stereo lane to the
    active-content lane BEFORE camilla#2 is armed, or camilla#2 can fight an
    existing opener for the exclusive active-content playback PCM.
    """
    try:
        subprocess.run(
            [AUDIO_HARDWARE_RECONCILE, "--reason", reason],
            check=True,
            capture_output=True,
            text=True,
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
    """Run ``systemctl <verb...> jasper-camilla-crossover.service`` for the
    active-leader camilla#2 arm/teardown. Fail-soft (logged + reflected in the
    exit code; the doctor's active-leader crossover-unit check surfaces a unit
    left un-armed). camilla#2 carries NO StartLimitAction=reboot, so a failed
    arm fails closed to silence — never reboots the speaker."""
    try:
        subprocess.run(
            ["systemctl", *verb, CROSSOVER_UNIT],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
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
    """``systemctl enable --now`` camilla#2 (the endpoint-crossover instance) for
    an active leader. Idempotent (enabling/starting an already-armed unit is a
    no-op). The crossover statefile MUST be re-seeded with the re-proven
    driver-domain graph BEFORE this (the caller orders it) so a cold start never
    loads a flat statefile (full-range to a tweeter)."""
    return _systemctl_crossover_unit("enable", "--now", action="armed")


def _disable_crossover_unit() -> bool:
    """``systemctl disable --now`` camilla#2 on unbond. Idempotent (disabling a
    not-armed unit is a no-op)."""
    return _systemctl_crossover_unit("disable", "--now", action="disabled")


def _write_follower_status(
    *, active_follower: bool, blocked_reason: str,
    active_leader: bool = False,
    path: str = FOLLOWER_STATUS_FILE,
) -> None:
    """Write the active-endpoint status for /state + the dashboard.

    Fail-soft (a lost status write must not crash the reconcile path; /state
    falls back to "no endpoint block"). Rewritten every reconcile so the
    surface is always fresh truth. ``active_follower`` = this box runs its local
    Layer-A crossover on the bonded stream as a FOLLOWER; ``active_leader`` = it
    runs that crossover (camilla#2) as the bond LEADER (and also bakes the wire
    on camilla#1); ``blocked_reason`` (non-empty) = an active-endpoint bond was
    REFUSED and the box fell back to solo active (invariant 5 fail-closed)."""
    body = json.dumps(
        {
            "active_follower": active_follower,
            "active_leader": active_leader,
            "blocked_reason": blocked_reason,
        },
        sort_keys=True,
    ) + "\n"
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


def _restart_outputd() -> bool:
    return _restart_unit(OUTPUTD_UNIT)


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

    SCOPE (Increment 5): the FULL bonded dataplane. Beyond the
    snapcast args this also (a) writes the outputd round-trip lane env
    (FIFO + channel pick + the PR-2 TTS socket) and restarts outputd
    only on change, (b) creates the member content FIFO, (c) flips
    voice's TTS socket to outputd while bonded (grouping-voice.env,
    restart-on-change; the doctor's `TTS lane` check guards the two
    files' agreement), and (d) drives the CamillaDSP config swap
    through jasper.multiroom.leader_config — the bonded pipe config on
    an active leader, the solo restore otherwise, and (e) re-derives
    shairport's AirPlay backend latency offset for a bonded leader
    (grouping-airplay.env + restart-on-change) so the leader's own
    output lands on the AirPlay anchor.

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
    # An ACTIVE (multi-driver) follower relocates Layer A onto its own
    # CamillaDSP in the bonded path (distributed-active Slice 3); a DUMB
    # (single-DAC) follower uses outputd's dac_content ChannelPick. The box's
    # saved topology decides which path this reconcile takes.
    box_is_active = is_active_speaker_box()
    active_follower = active and cfg.role == "follower" and box_is_active
    # An ACTIVE leader is brains + an endpoint: camilla#1 bakes the program
    # domain to the wire AND camilla#2 runs this box's own Layer-A crossover on
    # the round-tripped stream (two CamillaDSP; HANDOFF-distributed-active.md
    # "Stage B — the ratified active-leader realization"). A PASSIVE leader keeps
    # the single-camilla pipe bake (jasper.multiroom.leader_config).
    # ``active_leader`` (valid leader, either kind) is unchanged; these split it.
    active_speaker_leader = active_leader and box_is_active
    passive_leader = active_leader and not box_is_active
    # Both active endpoints (the follower AND the active leader's own drivers)
    # capture the round-trip loopback and run a camilla-owned channel-pick +
    # split, so they SHARE the snapclient-writes-loopback + outputd-dac_content-
    # disabled wiring (the active leader ALSO bakes the wire + hosts the stream).
    active_endpoint = active_follower or active_speaker_leader
    log_event(
        logger,
        "multiroom.reconcile.start",
        reason=args.reason,
        enabled=cfg.enabled,
        role=cfg.role or "(none)",
        error=cfg.error or "(none)",
        active_box=box_is_active,
        active_follower=active_follower,
        active_leader=active_speaker_leader,
        summary=repr(decision.summary),
    )
    rc = 0
    endpoint_block_reason = ""
    active_leader_arm_blocked = False

    # Grouping prerequisite: ensure the snapcast binaries are installed — the
    # "grouping opt-in's job" install.sh ships the units for but never installs
    # (jasper.multiroom.provision). Runs BEFORE the active-endpoint gate so the
    # active-leader precheck's snapcast check sees a fresh install. Gated on
    # `active` (a valid enabled bond); a present install is a fast no-op. TOTAL +
    # fail-soft: a failed install is logged + surfaced via
    # /state.grouping.provision (the /rooms wizard shows "Installing Snapcast…")
    # + the doctor, and flips rc so the oneshot shows failed — but never raises,
    # and the snap units simply fail to start (the box stays solo-safe, never
    # wedged; the next reconcile retries).
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
                logger, "multiroom.reconcile.snapcast_provisioned", result="installed",
            )

    # Active-ENDPOINT readiness GATE (fail-safe to SOLO). Build + re-prove the
    # driver-domain graph BEFORE tearing down the solo path — for a follower its
    # one CamillaDSP, for an active leader BOTH camilla#2's driver-domain graph
    # AND camilla#1's program bake. If it can't be made safe (bad channel / not
    # commissioned / graph fails re-proof), do NOT bond: fall back to solo active
    # so the box keeps playing its own content (self-recovery, AGENTS.md
    # resilience) instead of half-parking silent. This is invariant 5's "refuses
    # to bond" — the unsafe graph never reaches the DACs. The actual CamillaDSP
    # applies happen later, after snapcast is up.
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
                e, "reason", "active_endpoint_precheck_error",
            )
            # Distinct event per role (the follower name is documented in
            # HANDOFF-distributed-active.md); both literals stay greppable.
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
            # like an invalid bond (plan stops snapcast + restores renderers).
            # Reset EVERY role flag — including active_leader, which gates the
            # step-6 stream-binding pin — so a refused bond never partially
            # behaves like a leader/endpoint.
            cfg = replace(cfg, enabled=False)
            decision = plan(cfg)
            active = False
            active_leader = False
            active_follower = False
            active_speaker_leader = False
            passive_leader = False
            active_endpoint = False
            rc = 1

    # Endpoint status for /state + the dashboard (fresh truth every reconcile):
    # active-follower / active-leader mode, or the fail-closed block reason if
    # the bond was refused and we fell back to solo active.
    _write_follower_status(
        active_follower=active_follower,
        active_leader=active_speaker_leader,
        blocked_reason=endpoint_block_reason,
        path=FOLLOWER_STATUS_FILE,
    )

    # 1. Derived files + FIFO — before any unit work.
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

    # Paths passed explicitly (module globals read at CALL time) so the
    # test harness can redirect them; a def-time default would pin the
    # production path.
    outputd_env = outputd_grouping_env(cfg, active_endpoint=active_endpoint)
    env_changed, env_ok = _write_outputd_env(
        outputd_env, path=OUTPUTD_GROUPING_ENV_FILE,
    )
    log_event(
        logger,
        "multiroom.reconcile.outputd_env",
        path=OUTPUTD_GROUPING_ENV_FILE,
        changed=env_changed,
        ok=env_ok,
        fifo=outputd_env[OUTPUTD_DAC_CONTENT_FIFO_ENV] or "(cleared)",
        channel=outputd_env[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] or "(cleared)",
    )
    if not env_ok:
        rc = 1
    # The member content FIFO feeds the DUMB follower's dac_content lane. An
    # active ENDPOINT (follower or active leader) uses the snd-aloop round-trip
    # loopback instead (a fixed snd-aloop subdevice — always present, no mkfifo
    # equivalent), so skip it.
    if active and not active_endpoint and not _ensure_member_fifo(
        path=MEMBER_CONTENT_FIFO
    ):
        rc = 1

    # 2. CamillaDSP solo RESTORE — unwind a prior bond before units tear down.
    #    A box that will APPLY a bonded config below (active leader/follower)
    #    skips restore. An ACTIVE box restores its ACTIVE baseline (Layer A
    #    intact — NEVER a passive graph, which would be full-range to a tweeter);
    #    a passive box uses the leader-stash restore. All branches are a no-op on
    #    the common solo reconcile.
    if active_leader or active_follower:
        pass
    elif box_is_active and _unit_is_enabled(CROSSOVER_UNIT):
        # Unbond of an ACTIVE LEADER: camilla#2 (the crossover unit) is enabled
        # ONLY after an active leader armed it, so its enable state is the
        # discriminator "this box WAS an active leader" — tear camilla#2 down +
        # restore camilla#1 via the leader stash (the untouched active-FOLLOWER
        # path below stays byte-identical). disable is idempotent.
        if not _disable_crossover_unit():
            rc = 1
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
            rc = 1

    # 3. outputd picks up the lane env only at unit start. For an active leader,
    # defer that restart until after camilla#1's program-bake graph is live and
    # camilla#2's statefile has been seeded with the re-proven endpoint graph.
    # The audio-hardware reconciler needs that graph pair as evidence to switch
    # outputd from the passive stereo lane to the active-content lane before
    # camilla#2 is armed. Restarting here would read the grouping TTS env but
    # still use the solo baseline, re-opening the passive lane that camilla#2
    # needs.
    defer_outputd_restart = active_speaker_leader
    if env_changed and env_ok and not defer_outputd_restart and not _restart_outputd():
        rc = 1

    # 3b. Voice's grouping-derived env (PR-2 TTS socket flip + the PR-B
    # park flag): written + kick-on-change only — a voice restart costs
    # ~10-15 s and must happen only on a real bond/unbond, never on the
    # routine no-change reconcile. The kick goes to jasper-aec-reconcile,
    # NOT jasper-voice directly: that script is the single owner of the
    # voice/bridge units and decides restart-vs-park from the flag plus
    # its own provider + mic gates (writer/validator coherence — two
    # writers of one unit's state was the jts3 boot-loop class).
    voice_env = voice_grouping_env(cfg, active_endpoint=active_endpoint)
    voice_changed, voice_ok = _write_outputd_env(
        voice_env, path=VOICE_GROUPING_ENV_FILE,
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
    if voice_changed and voice_ok and not _restart_unit(AEC_RECONCILE_UNIT):
        rc = 1

    # 3c. shairport's bonded-leader AirPlay offset delta
    # (grouping-airplay.env): written + restart-on-change. A bonded leader
    # folds the Snapcast round-trip buffer into its backend latency offset
    # so its OWN output lands on the AirPlay anchor (lip-sync); solo and
    # follower clear it to the byte-identical solo offset. The re-derivation
    # itself happens in shairport's ExecStartPre (jasper-apply-airplay-mode
    # reads this file), so the restart in step 4b is what applies it.
    airplay_env = airplay_grouping_env(cfg)
    airplay_changed, airplay_ok = _write_outputd_env(
        airplay_env, path=AIRPLAY_GROUPING_ENV_FILE,
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

    # 4. The unit plan (stops before starts).
    rc = max(rc, _apply(decision))

    # 4b. Re-derive shairport's backend latency offset on a bond/unbond
    # that changed it. shairport's ExecStartPre runs
    # jasper-apply-airplay-mode, which reads grouping-airplay.env, so a
    # restart re-derives the offset (bonded leader: solo terms + Snapcast
    # buffer; unbonded: the unchanged solo value). Skip a bonded FOLLOWER —
    # the plan PARKED its shairport (stopped) and restarting would un-park
    # it; a follower receives no AirPlay anyway. A leader keeps shairport
    # running (the plan does not touch it); a solo/unbonded speaker only
    # reaches here on the bonded->solo transition (airplay_changed). One
    # restart, only on a real offset change — never on the steady-state
    # solo reconcile.
    is_bonded_follower = local_sources_parked(cfg)
    if airplay_changed and airplay_ok and not is_bonded_follower:
        if not _restart_unit(SHAIRPORT_UNIT):
            rc = 1

    # 5. Bonded apply LAST (snapserver is up → the pipe has its reader; snapclient
    #    is up → the round-trip loopback has its writer).
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
        # Active leader = two CamillaDSP: camilla#1 bakes the program domain to the
        # wire, camilla#2 runs this box's own Layer-A crossover on the round-trip.
        #
        # WIRE-UP GUARD (2026-06-23 JTS5 incident — the single top-of-path
        # precondition). The two-instance setup is viable ONLY if the wire is up.
        # camilla#1's bake writes a File/FIFO sink that needs snapserver as its
        # reader, and ONLY a successful bake moves camilla#1 off the DAC so
        # camilla#2 can take it. If snapserver did not start (no Snapcast
        # installed, or a failed start in step 4 — the precheck snapcast gate
        # catches a missing binary, this catches a failed start), bail here and
        # STAY SOLO-ACTIVE: camilla#1 keeps the DAC on its safe solo baseline,
        # camilla#2 stays un-armed. Otherwise the two instances fight for the DAC
        # and camilla#1 exhausts its recovery budget.
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
            # provably released the DAC, is camilla#2 armed onto it. This is the
            # never-flat guarantee: the crossover guard repairs a dead pipe, not a
            # flat statefile. camilla#2 is disabled before the bake and later
            # started from that statefile, so trim-only rewrites are picked up by
            # process start rather than relying on an idempotent systemd no-op.
            # camilla#2 keeps enable_rate_adjust ON — the validated
            # active-follower seam, no outputd-summer yet
            # (HANDOFF-distributed-active.md "Sequencing" 1).
            bake_ok = False
            if not _disable_crossover_unit():
                rc = 1
            elif not _ensure_unit_active(
                CAMILLA_UNIT, reason="active-leader-bake"
            ):
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
            # Arm camilla#2 (systemctl enable --now) ONLY when the bake provably
            # moved camilla#1 off the active-content PCM, outputd re-converged
            # to the active lane, and the exclusive handle positively released.
            # A successful CamillaDSP config reload is not enough: snd-aloop can
            # lag the actual close, and arming camilla#2 into that window races
            # EBUSY against camilla#1's recovery-budget unit.
            if bake_ok:
                if _unit_is_active(CROSSOVER_UNIT):
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=ACTIVE_CONTENT_PLAYBACK_PCM,
                        status_path=ACTIVE_CONTENT_PLAYBACK_STATUS_PATH,
                        result="already_armed",
                        reason="crossover_unit_active",
                    )
                else:
                    probe = _wait_for_active_content_pcm_release()
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=ACTIVE_CONTENT_PLAYBACK_PCM,
                        status_path=probe.status_path,
                        result=probe.state,
                        reason=probe.reason,
                        detail=probe.detail or "(none)",
                        attempts=probe.attempts,
                        timeout_sec=probe.timeout_sec,
                        level=logging.WARNING if probe.unknown else logging.INFO,
                    )
                    if not probe.released:
                        # Arm camilla#2 ONLY on a POSITIVE release proof. `busy`
                        # (still-open/timeout) and `unknown` (probe tool missing)
                        # both fail closed to solo-active — arming without proof
                        # is the exact jts3 EBUSY reboot-loop this barrier exists
                        # to prevent, and `cat` is universal on a real Pi so the
                        # `unknown` branch is a theoretical-only safety net, not a
                        # path we ever want to arm through.
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
                            pcm=ACTIVE_CONTENT_PLAYBACK_PCM,
                            status_path=probe.status_path,
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
                                        "active_leader_solo_restored_after_"
                                        "pcm_busy"
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
        # PERSISTED snapcast group to our stream. A stale server.json binding (the
        # distro-snapserver era's "default") silently mutes the whole bond behind
        # green health — the 2026-06-11 bring-up incident. The ensure retries
        # internally; an unreachable snapserver flips the exit code (a bond whose
        # bindings cannot be verified is a degraded bond) and the runtime health
        # shows it.
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

    # 5b. Active FOLLOWER CamillaDSP swap LAST (snapclient is up → the round-trip
    #     loopback has its writer, so CamillaDSP locks immediately). The graph
    #     was already built + re-proven by the readiness gate above, so this is
    #     just the glitch-free swap. The graph is the re-proven driver-domain
    #     baseline, so no capture content (stream / silence / garbage) can ever
    #     produce a full-range driver feed. A swap failure here keeps CamillaDSP
    #     on its prior safe solo-active graph; the next reconcile retries.
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

    log_event(logger, "multiroom.reconcile.done", rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
