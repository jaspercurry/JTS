# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Peering configuration — env-file loader.

Persisted at /var/lib/jasper/peering.env, mode 0644 (no secrets).
Written by the /rooms/ Speakers page, sourced into jasper-control's
environment via the systemd unit's `EnvironmentFile=` directive.

Same precedence rule as wake_model.env and voice_provider.env:
wizard-managed values override anything in /etc/jasper/jasper.env.

Two modes only:
  off  — default. Nothing peering-related runs: no Avahi advertise,
         no zeroconf browse, no multicast socket, no thread. Cost
         on a single-Pi household: zero.
  on   — Full peering. Advertise via Avahi, browse for siblings,
         arbitrate wake events. The user opts in via /rooms/.

We deliberately don't expose an `auto` mode where peering would
turn itself on if peers appear; one mode flip per setting, easy
to reason about, no surprises when a guest's JTS-shaped device
appears on the network.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

logger = logging.getLogger(__name__)


# ---------- Constants — wire/protocol level ----------

# RFC 2365 admin-local scope (239.192.0.0/14). Won't cross any
# configured admin boundary, scopes naturally to the LAN.
MULTICAST_GROUP = "239.192.0.1"
MULTICAST_PORT = 5354

# TTL=1 guarantees the packet dies at the first router hop even if
# a misconfigured network would otherwise forward it. Wake arbitration
# is a single-subnet concern by definition.
MULTICAST_TTL = 1

# Arbitration window — how long each peer waits after the first WAKE
# message before applying the ranking function. The window absorbs
# detection-time jitter across the fleet (typically 30-150 ms on Pi 5).
# 150 ms is the canonical default from the Sonos/Apple literature
# (see docs/satellites.md "Proposed approach for JTS"). Tunable per
# install via JASPER_PEER_ARB_WINDOW_MS but most users should leave
# it alone.
DEFAULT_ARB_WINDOW_MS = 150

# Safe clamp bounds for the arbitration window. A value outside this
# range is clamped (never rejected — fail-safe). MAX_ARB_WINDOW_MS is
# the ceiling the fail-open ARBITRATE RPC timeout must stay strictly
# above (see ARBITRATE_RPC_TIMEOUT_SEC in daemon.py): the timeout has
# to leave the state machine room to emit StartSession/StandDown even
# at the widest configured window, or a fail-open WIN could race the
# real decision at the window boundary.
MIN_ARB_WINDOW_MS = 50
MAX_ARB_WINDOW_MS = 500

# A locally-detected wake at or above this score breaks an in-flight
# foreign session (re-enters arbitration). Below this, the local peer
# stays suppressed. 0.85 is high enough that "real, deliberate wake at
# this device" clears it without TV/music false fires breaking
# sessions on every peer.
DEFAULT_BREAK_THRESHOLD = 0.85

# Heartbeats every 1 s; after 2 s without one, peers conclude the
# winner crashed and un-suppress. Mid-session re-arbitration won't
# happen on the next wake event — by then peers are already idle.
DEFAULT_HEARTBEAT_INTERVAL_SEC = 1.0
DEFAULT_HEARTBEAT_TIMEOUT_SEC = 2.0

# How often to multicast HELLO. Doubles as a multicast-health probe —
# if a peer's HELLO hasn't arrived in HELLO_INTERVAL * 3 but it's
# still in mDNS, mark its multicast path broken (future work: unicast
# fallback). Kept loose to minimize airtime cost.
HELLO_INTERVAL_SEC = 30.0


# ---------- File layout ----------

# Wizard-managed env file (matches the wake_model.env / voice_provider.env
# pattern). Lives in /var/lib/jasper so it survives daemon restarts and
# package upgrades.
PEERING_ENV_FILE = "/var/lib/jasper/peering.env"

# Stable per-install peer identifier. Generated once on first install
# (or first daemon start), persists across reboots. Never user-edited —
# treat as a machine-id equivalent. Lives in /var/lib/jasper so it's
# scoped to "this Pi" rather than "this user/process".
PEER_ID_FILE = "/var/lib/jasper/peer_id"

# UDS where jasper-control's peering daemon listens for voice→peering
# arbitration requests. jasper-control runs non-root and owns
# RuntimeDirectory=jasper-control, so the server socket must live under
# /run/jasper-control rather than the voice-owned /run/jasper directory.
PEERING_UDS_PATH = "/run/jasper-control/peering.sock"


class PeeringMode(str, Enum):
    OFF = "off"
    ON = "on"


@dataclass(frozen=True)
class PeeringConfig:
    """Resolved peering configuration.

    Construct via `load_config()` rather than directly so the env-file
    precedence and validation happen in one place.
    """

    mode: PeeringMode
    peer_id: str          # stable UUID, persists across reboots
    room: str             # human label, surfaced in /rooms/ UI and logs
    primary: bool         # small bias in the ranking function (~0.05)
    arb_window_ms: int    # arbitration collection window
    break_threshold: float  # local-wake confidence required to break a foreign session

    @property
    def enabled(self) -> bool:
        return self.mode is PeeringMode.ON


def _read_env_file(path: str) -> Mapping[str, str]:
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line).

    Mirrors jasper.web._common.read_env_file but avoids the import —
    this module must be importable in a fresh asyncio worker without
    dragging the web wizard's HTTP machinery in.
    """
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
    return out


def read_state(path: str = PEERING_ENV_FILE) -> dict[str, str]:
    """Read the peering EnvironmentFile as a plain dict.

    This is the small public helper for web/doctor surfaces that need to
    display or update the current peering setting without constructing a full
    :class:`PeeringConfig` (which may create a missing peer id). Missing or
    unreadable files resolve to ``{}``, matching ``load_config``'s fail-soft
    default-off posture.
    """
    return dict(_read_env_file(path))


def state_enabled(state: Mapping[str, str]) -> bool:
    """Return whether a peering state mapping resolves to ON.

    The persisted file wins, with process env as a fallback for older/manual
    deployments. This preserves the old web-helper behavior while keeping the
    ownership in the peering package instead of a deleted page module.
    """
    raw = (
        state.get("JASPER_PEERING", "")
        or os.environ.get("JASPER_PEERING", "")
    )
    return _parse_mode(raw) is PeeringMode.ON


def state_primary(state: Mapping[str, str]) -> bool:
    """Return whether the state mapping marks this speaker primary."""
    raw = (
        state.get("JASPER_PEER_PRIMARY", "")
        or os.environ.get("JASPER_PEER_PRIMARY", "")
    )
    return _parse_bool(raw)


def _ensure_peer_id(path: str = PEER_ID_FILE) -> str:
    """Read or generate the stable peer_id.

    Idempotent: subsequent calls return the same UUID. First call on a
    fresh install creates the file (mode 0644, owned by whoever is
    running — typically root via jasper-control's systemd unit).

    A peer_id is just a UUID4 string. We don't use the MAC address
    because the user might re-image the SD card on the same Pi and
    expect the fleet to forget the old "instance"; UUID lets the user
    `rm /var/lib/jasper/peer_id` to force a clean re-introduction.
    """
    try:
        existing = open(path).read().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as e:
        # Permission / FS error — log and fall through to generate a
        # transient one rather than crashing the daemon at startup.
        logger.warning("could not read %s (%s); using ephemeral peer_id", path, e)
        return str(uuid.uuid4())

    new_id = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(new_id + "\n")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("could not write %s (%s); peer_id is ephemeral", path, e)
    return new_id


_ROOM_FALLBACK_RE = re.compile(r"[^a-z0-9_-]+")


def default_room(hostname: str | None = None) -> str:
    """Pick a sensible default room label from the system hostname.

    The hostname is usually something like "jts" or "jts-bedroom"; we
    strip the leading "jts-" if present so "jts-bedroom" → "bedroom".
    A bare "jts" or a non-conforming hostname falls back to "default".
    Non-mDNS-safe chars (spaces, punctuation, etc.) collapse into a
    single dash, so "Living Room" → "living-room".

    Exposed publicly so management surfaces can produce the same
    fallback as `load_config()` without dragging the full peering
    package into the wizard's import path.
    """
    raw = (hostname if hostname is not None else socket.gethostname()).lower()
    if raw.startswith("jts-"):
        raw = raw[4:]
    cleaned = _ROOM_FALLBACK_RE.sub("-", raw).strip("-")
    if not cleaned or cleaned == "jts":
        return "default"
    return cleaned[:32]


# Backwards-compat alias for the historical underscore-prefixed name.
_default_room = default_room


def _parse_mode(raw: str) -> PeeringMode:
    val = raw.strip().lower()
    if val in ("on", "true", "1", "yes", "enabled"):
        return PeeringMode.ON
    # Everything else (including the empty string, "off", anything
    # malformed) means off. We don't fail hard on a typo here — a
    # broken file should never silently leave peering ON, only OFF.
    return PeeringMode.OFF


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(raw: str, *, default: int, lo: int, hi: int) -> int:
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return max(lo, min(hi, val))


def _parse_float(raw: str, *, default: float, lo: float, hi: float) -> float:
    try:
        val = float(raw.strip())
    except (ValueError, AttributeError):
        return default
    return max(lo, min(hi, val))


def load_config(
    *,
    env_file: str = PEERING_ENV_FILE,
    peer_id_file: str = PEER_ID_FILE,
    overrides: Mapping[str, str] | None = None,
) -> PeeringConfig:
    """Load PeeringConfig with the standard precedence ladder.

    Precedence (highest wins):
      1. `overrides` arg (test injection)
      2. process environment (systemd-merged /etc/jasper/jasper.env
         + EnvironmentFile=/var/lib/jasper/peering.env)
      3. env_file directly (if process env hasn't been refreshed —
         not the normal path; we double-read for the doctor's
         no-restart-required case)
      4. compiled defaults

    Malformed values fall through to defaults rather than crashing
    the daemon at startup. A broken JASPER_PEERING value silently
    resolves to `off` — fail-safe, never fail-on.
    """
    src = dict(_read_env_file(env_file))
    src.update({k: v for k, v in os.environ.items() if k.startswith("JASPER_PEER")})
    if overrides:
        src.update(overrides)

    mode = _parse_mode(src.get("JASPER_PEERING", "off"))
    room = src.get("JASPER_PEER_ROOM", "").strip() or _default_room()
    primary = _parse_bool(src.get("JASPER_PEER_PRIMARY", "0"))
    arb_window_ms = _parse_int(
        src.get("JASPER_PEER_ARB_WINDOW_MS", ""),
        default=DEFAULT_ARB_WINDOW_MS,
        lo=MIN_ARB_WINDOW_MS, hi=MAX_ARB_WINDOW_MS,
    )
    break_threshold = _parse_float(
        src.get("JASPER_PEER_BREAK_THRESHOLD", ""),
        default=DEFAULT_BREAK_THRESHOLD,
        lo=0.5, hi=0.99,
    )

    return PeeringConfig(
        mode=mode,
        peer_id=_ensure_peer_id(peer_id_file),
        room=room,
        primary=primary,
        arb_window_ms=arb_window_ms,
        break_threshold=break_threshold,
    )
