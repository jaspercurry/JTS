# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom grouping configuration — env-file loader.

Persisted at /var/lib/jasper/grouping.env, mode 0644 (no secrets).
Written by the grouping web wizard (later phase), sourced into the
relevant systemd unit's environment via `EnvironmentFile=`.

Same precedence rule as peering.env / wake_model.env / voice_provider.env:
wizard-managed values override anything in /etc/jasper/jasper.env.

Two states only:
  off  — default. Nothing grouping-related runs: no snapserver, no
         snapclient, no channel split. Cost on a solo speaker: zero.
  on   — Grouping plumbing active. This speaker plays its assigned
         channel as part of a bond. The user opts in via the wizard.

This module is the OFF-by-default plumbing increment: the codified
env contract + the pure loader. The BondedSet / channel-split /
volume system is a later phase — nothing here opens a socket, spawns
a thread, or calls Snapcast.

Fail-safe vs fail-loud:
  - A missing / unreadable / malformed file resolves to grouping OFF
    with no error. A broken file must never silently leave grouping ON.
  - A file that is explicitly ON but internally inconsistent
    (no bond, bad channel/role, follower with no leader address)
    stays ON and carries a specific `error` string. That is the
    fail-LOUD path the doctor surfaces — "configured but broken" is
    a state the operator needs to see, not one we paper over.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)


# ---------- File layout ----------

# Wizard-managed env file (matches the peering.env / wake_model.env /
# voice_provider.env pattern). Lives in /var/lib/jasper so it survives
# daemon restarts and package upgrades. ABSENT => grouping off.
GROUPING_ENV_FILE = "/var/lib/jasper/grouping.env"


# ---------- Allowed value sets ----------

# What this speaker plays out of its bond. "stereo" is the solo /
# unsplit default; the rest are the channel-split assignments.
ALLOWED_CHANNELS = ("stereo", "left", "right", "sub", "mono")

# This speaker's role in its bond. Empty string = unset.
ALLOWED_ROLES = ("leader", "follower")

# snapclient playout buffer (ms). Higher = more drift headroom at the
# cost of latency. Clamped, never an error.
DEFAULT_BUFFER_MS = 400
BUFFER_MS_LO = 150
BUFFER_MS_HI = 1500

# Snapcast client/output-path latency (ms). Distinct from the stream
# buffer above: buffer_ms is the group/network playout budget; client
# latency compensates a fixed PCM/DAC/backend path offset for one
# snapclient.
DEFAULT_CLIENT_LATENCY_MS = 0
CLIENT_LATENCY_MS_LO = 0
CLIENT_LATENCY_MS_HI = 1500

# Leader-owned room/pair acoustic delay (ms), baked into the rendered
# stereo stream by CamillaDSP. Positive-only: delay the early side to
# meet the late side.
CHANNEL_DELAY_MS_LO = 0.0
CHANNEL_DELAY_MS_HI = 100.0

# Receiver-side wireless-sub low-pass corner (Hz). Only meaningful when
# channel=="sub": the follower mono-sums the full-range stereo program
# and applies an LR4 low-pass locally in outputd so a powered sub plays
# only the low end. When bass management is enabled, every non-sub main
# member applies the complementary LR4 high-pass at this same corner in
# its own local output path. A blank/non-numeric value falls back to the
# default (a "sub" must never play full-range); an out-of-range value on
# a sub is fail-LOUD. Bounds bracket sane home-sub corners.
DEFAULT_CROSSOVER_HZ = 80.0
CROSSOVER_HZ_LO = 40.0
CROSSOVER_HZ_HI = 200.0

# Wireless-sub bass management. Default ON: when a bond contains a sub, mains
# high-pass at the same crossover corner the sub low-passes. The toggle exists
# for people who deliberately want full-range mains + sub augmentation.
DEFAULT_MAINS_HIGHPASS_ENABLED = True

# Snapcast stream codec. "flac" is the lossless default (good drift
# tolerance, modest CPU); "pcm" is uncompressed (lowest CPU, highest
# bandwidth); "opus" is lossy (lowest bandwidth). A value outside this
# set on an ENABLED config is fail-LOUD (sets `error`), never silently
# corrected — an unknown codec would make snapserver refuse to start.
ALLOWED_CODECS = ("pcm", "flac", "opus")
DEFAULT_CODEC = "flac"

# Per-member pair-balance trim (dB). Attenuate-only: balancing trims the
# LOUDER speaker down — a boost would cost headroom and risk hearing
# safety (enforced again fail-closed in outputd). The -24 floor marks
# "misconfigured, not unbalanced" (industry trim ranges are ±10-15).
TRIM_DB_MIN = -24.0
TRIM_DB_MAX = 0.0

# A follower's leader_addr must be hostname/IPv4-shaped: it is consumed by
# THREE surfaces (snapclient argv, the control-API volume forward's URL
# build, the landing page's leader link) and a string with '/', '@', or
# whitespace would reshape a URL rather than name a host. This is the same
# hostname alphabet the browser local-web-host helper accepts before it
# further rejects raw IPs for user-facing links.
_LEADER_ADDR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")


@dataclass(frozen=True)
class BondMember:
    """One follower in a leader's bond roster: its LAN IPv4, directory
    display name, and channel. Recorded on the LEADER at bond time so
    :func:`jasper.web.rooms_setup._unbond` can disable EVERY member of an
    N-member bond (e.g. a 2.1 system: left + right + sub) instead of only
    the single L/R sibling — no orphaned sub."""

    addr: str
    name: str
    channel: str


@dataclass(frozen=True)
class GroupingConfig:
    """Resolved multiroom grouping configuration.

    Construct via `load_config()` rather than directly so the env-file
    parsing and validation happen in one place.

    `error` is the fail-LOUD signal: it is non-None only when grouping
    is enabled but the config is internally invalid. A disabled config
    (or a valid enabled one) always has `error=None`.
    """

    enabled: bool
    role: str            # "" | "leader" | "follower"
    channel: str         # one of ALLOWED_CHANNELS
    bond_id: str
    # A follower's leader address. May be a literal IPv4 OR (preferred, and
    # what the bond wizard now mints) a stable mDNS host like "jts3.local" —
    # both are accepted because snapclient resolves either, and the .local
    # handle survives the leader's DHCP IP churn (reconcile.snapclient_argv).
    leader_addr: str
    buffer_ms: int
    codec: str           # one of ALLOWED_CODECS
    error: str | None    # human-readable reason an ENABLED config is invalid; else None
    # Pair-balance trim (TRIM_DB_MIN..0; 0 = none). Defaulted so the
    # wide existing constructor surface (tests, fan-out payload builds)
    # stays source-compatible — load_config always sets it explicitly.
    trim_db: float = 0.0
    # Fixed snapclient PCM/DAC/backend latency compensation for this
    # member. This is not the group buffer budget.
    client_latency_ms: int = DEFAULT_CLIENT_LATENCY_MS
    # Leader-owned rendered-channel acoustic delays for this room/pair.
    # Followers persist but do not execute these; the leader's render
    # graph consumes them when generating the shared stereo stream.
    left_delay_ms: float = 0.0
    right_delay_ms: float = 0.0
    # Receiver-side wireless-sub low-pass corner (Hz). Only meaningful
    # when channel=="sub"; also used as the matched mains high-pass corner
    # when this bond has a sub and mains_highpass_enabled is true.
    # Defaulted so the wide existing constructor surface stays
    # source-compatible; load_config always sets it explicitly.
    crossover_hz: float = DEFAULT_CROSSOVER_HZ
    # Per-bond wireless-sub bass-management preference. Default ON; only
    # takes effect when a sub is actually present in the bond.
    mains_highpass_enabled: bool = DEFAULT_MAINS_HIGHPASS_ENABLED
    # Fan-out-derived bond-composition fact, persisted on every member so
    # a non-leader main can self-heal its local outputd env without needing
    # the leader-only roster. The leader also derives this from roster as
    # defence in depth.
    subwoofer_present: bool = False
    # The bond roster, LEADER only: who this leader's pair sibling IS,
    # recorded at bond-forming time. peer_addr is the follower's LAN
    # IPv4 (the cross-speaker control calls are IP-only by SSRF
    # design); peer_name is its directory display name, kept so the
    # peer can be re-found through discovery when DHCP moves its IP.
    # Empty on followers, solo speakers, and bonds formed before the
    # field existed (resolvers fall back to bond-id inference). Why a
    # roster: inferring membership from "who on the LAN claims my
    # bond_id" is ambiguous whenever a third device transiently claims
    # the bond (observed live 2026-06-12 — an endpoint-tier test Pi
    # made every pair operation fail with "found 2").
    peer_addr: str = ""
    peer_name: str = ""
    # LEADER-only: every follower (addr/name/channel) recorded at bond
    # time, so _unbond disables ALL members (not just the L/R sibling) —
    # no orphaned sub. Empty on followers, solo, and legacy bonds (the
    # discovery fallback covers those).
    roster: tuple[BondMember, ...] = ()


# The all-off, no-error config returned whenever the file is absent,
# unreadable, or grouping is not explicitly ON.
_DISABLED = GroupingConfig(
    enabled=False,
    role="",
    channel="stereo",
    bond_id="",
    leader_addr="",
    buffer_ms=DEFAULT_BUFFER_MS,
    codec=DEFAULT_CODEC,
    trim_db=0.0,
    client_latency_ms=DEFAULT_CLIENT_LATENCY_MS,
    left_delay_ms=0.0,
    right_delay_ms=0.0,
    crossover_hz=DEFAULT_CROSSOVER_HZ,
    mains_highpass_enabled=DEFAULT_MAINS_HIGHPASS_ENABLED,
    subwoofer_present=False,
    error=None,
)


def _read_env_file(path: str) -> Mapping[str, str]:
    """Parse a systemd-style EnvironmentFile (KEY=VALUE per line).

    Mirrors jasper.peering.config._read_env_file — kept local so this
    module stays importable without dragging any heavier package in.
    Total: a missing or unreadable file yields an empty mapping, never
    an exception.
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


def _parse_enabled(raw: str) -> bool:
    """JASPER_GROUPING is ON only on an exact (trimmed, case-insensitive)
    "on". Everything else — empty, "off", garbage — is OFF (fail-safe).
    A broken value must never silently leave grouping ON.
    """
    return raw.strip().lower() == "on"


def _parse_bool_env_default_true(raw: str, *, key: str) -> tuple[bool, str | None]:
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_MAINS_HIGHPASS_ENABLED, None
    if text in {"1", "true", "yes", "on"}:
        return True, None
    if text in {"0", "false", "no", "off"}:
        return False, None
    return DEFAULT_MAINS_HIGHPASS_ENABLED, (
        f"{key}={raw!r} must be one of on/off/true/false/1/0"
    )


def _parse_bool_env_default_false(raw: str, *, key: str) -> tuple[bool, str | None]:
    text = (raw or "").strip().lower()
    if not text:
        return False, None
    if text in {"1", "true", "yes", "on"}:
        return True, None
    if text in {"0", "false", "no", "off"}:
        return False, None
    return False, f"{key}={raw!r} must be one of on/off/true/false/1/0"


def _parse_buffer_ms(raw: str) -> int:
    """Parse the playout buffer; non-int falls back to the default,
    then clamp into [BUFFER_MS_LO, BUFFER_MS_HI]. Never an error.
    """
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_BUFFER_MS
    return max(BUFFER_MS_LO, min(BUFFER_MS_HI, val))


def _parse_client_latency_ms(raw: str) -> tuple[int, str | None]:
    if not raw.strip():
        return DEFAULT_CLIENT_LATENCY_MS, None
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_CLIENT_LATENCY_MS, (
            f"JASPER_GROUPING_CLIENT_LATENCY_MS={raw!r} is not an integer"
        )
    if not (CLIENT_LATENCY_MS_LO <= val <= CLIENT_LATENCY_MS_HI):
        return val, (
            f"JASPER_GROUPING_CLIENT_LATENCY_MS={val} must be between "
            f"{CLIENT_LATENCY_MS_LO} and {CLIENT_LATENCY_MS_HI}"
        )
    return val, None


def _parse_channel_delay_ms(raw: str, *, key: str) -> tuple[float, str | None]:
    if not raw.strip():
        return 0.0, None
    try:
        val = float(raw.strip())
    except (ValueError, AttributeError):
        return 0.0, f"{key}={raw!r} is not a number"
    if not (CHANNEL_DELAY_MS_LO <= val <= CHANNEL_DELAY_MS_HI):
        return val, (
            f"{key}={val} must be between {CHANNEL_DELAY_MS_LO} "
            f"and {CHANNEL_DELAY_MS_HI}"
        )
    return val, None


def _parse_crossover_hz(raw: str) -> float:
    """Parse the sub low-pass corner; a blank or non-numeric value falls
    back to DEFAULT_CROSSOVER_HZ (a "sub" must NEVER play full-range, so
    there is no bypass sentinel). Range enforcement is the shared rule in
    :func:`validate_grouping`, applied only when the config is enabled and
    the channel is "sub" — so a non-sub member carrying a stray value is
    not fail-LOUD over a knob it does not use.
    """
    if not raw.strip():
        return DEFAULT_CROSSOVER_HZ
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        logger.warning(
            "JASPER_GROUPING_CROSSOVER_HZ=%r is not a number; "
            "defaulting to %.1f Hz", raw, DEFAULT_CROSSOVER_HZ,
        )
        return DEFAULT_CROSSOVER_HZ


def _scrub_roster_field(value: str) -> str:
    """Replace the roster delimiters ("|" ",") and any control char (ord < 32)
    with a space so an untrusted value can never RESHAPE the serialization — a
    "|"/"," in the middle of a field would otherwise inject an extra member, and
    a stray delimiter would split (drop) the record. Caller strips + length-caps.
    A valid addr (IP/host) or channel (left/right/sub/...) never contains these,
    so scrubbing only ever neutralises a malformed/hostile value."""
    return "".join(" " if (c in "|," or ord(c) < 32) else c for c in value)


def format_roster(members) -> str:
    """Serialize an iterable of :class:`BondMember` into the env-file value
    for JASPER_GROUPING_ROSTER: ``addr|name|channel`` entries joined by ",".

    PUBLIC (mirrors :func:`validate_grouping`): the cross-package write contract
    used by jasper.control.server to build the env string — not a private detail.

    Members with an empty addr are skipped (a roster slot with no address is
    meaningless). ALL THREE fields are sanitized via :func:`_scrub_roster_field`
    — the "|"/"," delimiters and control chars become a space — then stripped and
    length-capped, so NO untrusted field (addr, an mDNS directory name, or a
    channel) can reshape the serialization (inject or drop a member). Sanitizing
    is defence-in-depth under :func:`validate_grouping`, which the writer runs
    whenever a roster is present. Inverse of :func:`_parse_roster`."""
    out: list[str] = []
    for m in members:
        addr = _scrub_roster_field(str(m.addr)).strip()[:64]
        if not addr:
            continue
        name = _scrub_roster_field(str(m.name)).strip()[:64]
        channel = _scrub_roster_field(str(m.channel)).strip()[:32]
        out.append(f"{addr}|{name}|{channel}")
    return ",".join(out)


# Back-compat alias: the serializer was introduced private; `format_roster` is
# the public spelling now that it is a cross-package contract.
_format_roster = format_roster


def _parse_roster(raw: str) -> tuple[BondMember, ...]:
    """Parse JASPER_GROUPING_ROSTER into a tuple of :class:`BondMember`.

    TOTAL — never raises. Splits on "," into entries, each entry on "|"
    into exactly ``addr|name|channel``; an entry without exactly three
    parts, or with an empty addr, is silently skipped. Each field is
    stripped. Inverse of :func:`_format_roster`. Range/IP/channel
    validation is the loud job of :func:`validate_grouping`, not this
    parser."""
    members: list[BondMember] = []
    for entry in (raw or "").split(","):
        parts = entry.split("|")
        if len(parts) != 3:
            continue
        addr = parts[0].strip()
        if not addr:
            continue
        members.append(
            BondMember(addr=addr, name=parts[1].strip(), channel=parts[2].strip())
        )
    return tuple(members)


def validate_grouping(
    *,
    role: str,
    channel: str,
    bond_id: str,
    leader_addr: str,
    codec: str = DEFAULT_CODEC,
    trim_db: float = 0.0,
    client_latency_ms: int = DEFAULT_CLIENT_LATENCY_MS,
    left_delay_ms: float = 0.0,
    right_delay_ms: float = 0.0,
    crossover_hz: float = DEFAULT_CROSSOVER_HZ,
    mains_highpass_enabled: bool = DEFAULT_MAINS_HIGHPASS_ENABLED,
    subwoofer_present: bool = False,
    peer_addr: str = "",
    peer_name: str = "",
    roster: tuple[BondMember, ...] = (),
) -> str | None:
    """The single grouping-validation rule for an ENABLED config.

    Returns a human-readable error string when the config is internally
    inconsistent (the fail-LOUD reason the doctor/dashboard surface), or
    None when valid. This is the ONE source of truth, used by
    :func:`load_config` (validating a file on read) AND the
    ``/grouping/set`` control endpoint (validating a write before it
    persists) — so the two can never drift. Checked in order: bond_id,
    channel, codec, role, follower-needs-leader_addr.

    CODEC ASYMMETRY (intentional, not a bug): the ``/grouping/set`` endpoint
    calls this WITHOUT a codec arg, so it validates against ``DEFAULT_CODEC``.
    That is correct because the bond wizard never sets the codec — codec is an
    operator-tuned knob (``JASPER_GROUPING_CODEC`` in grouping.env). The
    control endpoint's read-modify-write (``_write_grouping``) does NOT touch
    that key, so an operator's codec is PRESERVED across a wizard role change;
    and :func:`load_config` re-validates the actually-persisted codec on read,
    so a bad operator value still fails LOUD there. Validating the write path
    against the default is thus safe: the write can never INTRODUCE a bad codec
    (it never writes one), and the read path is the fail-loud backstop.
    """
    if not bond_id:
        return "JASPER_GROUPING_BOND_ID is empty (grouping is on)"
    if channel not in ALLOWED_CHANNELS:
        return (
            f"JASPER_GROUPING_CHANNEL={channel!r} is not one of "
            f"{', '.join(ALLOWED_CHANNELS)}"
        )
    if codec not in ALLOWED_CODECS:
        return (
            f"JASPER_GROUPING_CODEC={codec!r} is not one of "
            f"{', '.join(ALLOWED_CODECS)}"
        )
    if role not in ALLOWED_ROLES:
        return (
            f"JASPER_GROUPING_ROLE={role!r} is not one of "
            f"{', '.join(ALLOWED_ROLES)}"
        )
    if role == "follower" and not leader_addr:
        return "JASPER_GROUPING_LEADER_ADDR is empty for role=follower"
    if leader_addr and not _LEADER_ADDR_RE.match(leader_addr):
        return (
            f"JASPER_GROUPING_LEADER_ADDR={leader_addr!r} is not a "
            "hostname or IPv4 address"
        )
    if not (TRIM_DB_MIN <= trim_db <= TRIM_DB_MAX):
        return (
            f"JASPER_GROUPING_TRIM_DB={trim_db} must be between "
            f"{TRIM_DB_MIN} and {TRIM_DB_MAX} (attenuate-only pair trim)"
        )
    if not (CLIENT_LATENCY_MS_LO <= client_latency_ms <= CLIENT_LATENCY_MS_HI):
        return (
            f"JASPER_GROUPING_CLIENT_LATENCY_MS={client_latency_ms} must be "
            f"between {CLIENT_LATENCY_MS_LO} and {CLIENT_LATENCY_MS_HI}"
        )
    if not isinstance(mains_highpass_enabled, bool):
        return "JASPER_GROUPING_MAINS_HIGHPASS must be boolean"
    if not isinstance(subwoofer_present, bool):
        return "JASPER_GROUPING_SUBWOOFER_PRESENT must be boolean"
    for key, value in (
        ("JASPER_GROUPING_LEFT_DELAY_MS", left_delay_ms),
        ("JASPER_GROUPING_RIGHT_DELAY_MS", right_delay_ms),
    ):
        if not (CHANNEL_DELAY_MS_LO <= value <= CHANNEL_DELAY_MS_HI):
            return (
                f"{key}={value} must be between {CHANNEL_DELAY_MS_LO} "
                f"and {CHANNEL_DELAY_MS_HI}"
            )
    # The crossover corner is mandatory for a sub, and for a main when
    # bass management is armed for a bond that contains a sub. A non-sub
    # member in a plain stereo pair can still carry a stale corner without
    # failing loud because the reconciler clears the HP env unless a sub is
    # present.
    uses_crossover = (
        channel == "sub"
        or (subwoofer_present and mains_highpass_enabled and channel != "sub")
    )
    if uses_crossover and not (CROSSOVER_HZ_LO <= crossover_hz <= CROSSOVER_HZ_HI):
        return (
            f"JASPER_GROUPING_CROSSOVER_HZ={crossover_hz} must be between "
            f"{CROSSOVER_HZ_LO} and {CROSSOVER_HZ_HI} Hz"
        )
    if peer_addr:
        try:
            ip = ipaddress.ip_address(peer_addr)
        except ValueError:
            ip = None
        if ip is None or ip.version != 4 or not (
                ip.is_private or ip.is_loopback):
            return (
                f"JASPER_GROUPING_PEER_ADDR={peer_addr!r} is not a "
                "private/loopback IPv4 address"
            )
    if peer_name and (len(peer_name) > 64
                      or any(ord(c) < 32 for c in peer_name)):
        return (
            "JASPER_GROUPING_PEER_NAME must be printable and at most "
            "64 characters"
        )
    # Roster (leader only): delegate to validate_roster — the SAME rule the writer
    # runs whenever a roster is present (including a DISABLED request), so an
    # unvalidated member can never be persisted.
    return validate_roster(roster)


def validate_roster(roster: tuple[BondMember, ...]) -> str | None:
    """Validate a bond roster independently of the enabled-grouping fields.

    Every member's addr must be a private/loopback IPv4 (the SSRF-driven rule,
    same as peer_addr), its channel a known one, its name bounded printable text.
    Returns an error naming the bad field on the FIRST bad member, or None. An
    empty roster is valid.

    PUBLIC and standalone (NOT gated on enabled): jasper.control.server calls this
    whenever a roster key is present — including a `disabled` /grouping/set, whose
    full validate_grouping is skipped — so a roster member with an injected foreign
    addr or a malformed channel can never be persisted (it would otherwise become an
    _unbond disable target, or — if a delimiter split it — an orphaned member)."""
    for m in roster:
        try:
            ip = ipaddress.ip_address(m.addr)
        except ValueError:
            ip = None
        if ip is None or ip.version != 4 or not (ip.is_private or ip.is_loopback):
            return (
                f"JASPER_GROUPING_ROSTER member addr={m.addr!r} is not a "
                "private/loopback IPv4 address"
            )
        if m.channel not in ALLOWED_CHANNELS:
            return (
                f"JASPER_GROUPING_ROSTER member channel={m.channel!r} is not "
                f"one of {', '.join(ALLOWED_CHANNELS)}"
            )
        if len(m.name) > 64 or any(ord(c) < 32 for c in m.name):
            return (
                f"JASPER_GROUPING_ROSTER member name={m.name!r} must be "
                "printable and at most 64 characters"
            )
    return None


def load_config(path: str = GROUPING_ENV_FILE) -> GroupingConfig:
    """Load the GroupingConfig from the wizard-owned env file.

    Pure except for the single read of `path`. Total — never raises on
    a missing file or bad input.

    Resolution:
      - File absent / unreadable => the all-off config (error=None).
      - JASPER_GROUPING not exactly "on" => disabled, error=None
        (fail-safe to solo; a plain "off" is not an error).
      - Enabled but internally inconsistent => enabled stays True and
        `error` carries a specific reason (fail-LOUD; the doctor
        surfaces it). Invalid cases, checked in order:
          * bond_id empty
          * channel not in ALLOWED_CHANNELS
          * codec not in ALLOWED_CODECS
          * role not in ALLOWED_ROLES
          * role == "follower" and leader_addr empty
      - buffer_ms is always parsed + clamped, never an error.
    """
    src = _read_env_file(path)

    if not _parse_enabled(src.get("JASPER_GROUPING", "off")):
        return _DISABLED

    role = src.get("JASPER_GROUPING_ROLE", "").strip()
    channel = src.get("JASPER_GROUPING_CHANNEL", "").strip() or "stereo"
    bond_id = src.get("JASPER_GROUPING_BOND_ID", "").strip()
    leader_addr = src.get("JASPER_GROUPING_LEADER_ADDR", "").strip()
    buffer_ms = _parse_buffer_ms(src.get("JASPER_GROUPING_BUFFER_MS", ""))
    codec = src.get("JASPER_GROUPING_CODEC", "").strip() or DEFAULT_CODEC
    peer_addr = src.get("JASPER_GROUPING_PEER_ADDR", "").strip()
    peer_name = src.get("JASPER_GROUPING_PEER_NAME", "").strip()
    roster = _parse_roster(src.get("JASPER_GROUPING_ROSTER", ""))
    trim_raw = src.get("JASPER_GROUPING_TRIM_DB", "").strip()
    trim_parse_error: str | None = None
    trim_db = 0.0
    if trim_raw:
        try:
            trim_db = float(trim_raw)
        except ValueError:
            trim_parse_error = (
                f"JASPER_GROUPING_TRIM_DB={trim_raw!r} is not a number"
            )
    client_latency_ms, client_latency_parse_error = _parse_client_latency_ms(
        src.get("JASPER_GROUPING_CLIENT_LATENCY_MS", "")
    )
    left_delay_ms, left_delay_parse_error = _parse_channel_delay_ms(
        src.get("JASPER_GROUPING_LEFT_DELAY_MS", ""),
        key="JASPER_GROUPING_LEFT_DELAY_MS",
    )
    right_delay_ms, right_delay_parse_error = _parse_channel_delay_ms(
        src.get("JASPER_GROUPING_RIGHT_DELAY_MS", ""),
        key="JASPER_GROUPING_RIGHT_DELAY_MS",
    )
    crossover_hz = _parse_crossover_hz(
        src.get("JASPER_GROUPING_CROSSOVER_HZ", "")
    )
    mains_highpass_enabled, mains_highpass_parse_error = (
        _parse_bool_env_default_true(
            src.get("JASPER_GROUPING_MAINS_HIGHPASS", ""),
            key="JASPER_GROUPING_MAINS_HIGHPASS",
        )
    )
    subwoofer_present, subwoofer_present_parse_error = (
        _parse_bool_env_default_false(
            src.get("JASPER_GROUPING_SUBWOOFER_PRESENT", ""),
            key="JASPER_GROUPING_SUBWOOFER_PRESENT",
        )
    )

    error = (
        trim_parse_error
        or client_latency_parse_error
        or left_delay_parse_error
        or right_delay_parse_error
        or mains_highpass_parse_error
        or subwoofer_present_parse_error
        or validate_grouping(
        role=role,
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        codec=codec,
        trim_db=trim_db,
        client_latency_ms=client_latency_ms,
        left_delay_ms=left_delay_ms,
        right_delay_ms=right_delay_ms,
        crossover_hz=crossover_hz,
        mains_highpass_enabled=mains_highpass_enabled,
        subwoofer_present=subwoofer_present,
        peer_addr=peer_addr,
        peer_name=peer_name,
        roster=roster,
        )
    )

    return GroupingConfig(
        enabled=True,
        role=role,
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        buffer_ms=buffer_ms,
        codec=codec,
        trim_db=trim_db,
        client_latency_ms=client_latency_ms,
        left_delay_ms=left_delay_ms,
        right_delay_ms=right_delay_ms,
        crossover_hz=crossover_hz,
        mains_highpass_enabled=mains_highpass_enabled,
        subwoofer_present=subwoofer_present,
        peer_addr=peer_addr,
        peer_name=peer_name,
        roster=roster,
        error=error,
    )


def is_enabled(path: str = GROUPING_ENV_FILE) -> bool:
    """Cheap enabled-check. True only if the file parses to enabled=True.

    A configured-but-invalid bond is still `enabled` (the fail-LOUD
    state) — callers that need validity inspect `load_config().error`.
    """
    return load_config(path).enabled


def is_active_member(cfg: GroupingConfig) -> bool:
    """Is this speaker an ACTIVE member of a running bond — enabled AND valid?

    A bond's snapcast units only run when the config is enabled and has no
    `error` (the reconciler refuses to start a broken bond; see
    :func:`jasper.multiroom.reconcile.plan`). So "active member" = a speaker
    whose local audio is actually part of a synced stream. PURE.

    Distinct from :func:`is_enabled`, which is True even for a fail-LOUD
    invalid config (nothing is streaming there, so it is NOT an active member).
    """
    return cfg.enabled and cfg.error is None


def is_active_leader(cfg: GroupingConfig) -> bool:
    """Is this speaker the ACTIVE LEADER of a running bond — an active member
    whose role is leader? PURE; composed from :func:`is_active_member`,
    mirroring :func:`follower_leader_addr`.

    The ONE predicate behind every "this speaker plays its own channel
    through the Snapcast round-trip, so it must compensate that delay"
    decision. It is shared on purpose so the WRITER of the bonded-leader
    AirPlay offset (:func:`jasper.multiroom.reconcile.airplay_grouping_env`)
    and the OBSERVERS of that state (the ``/state`` snapshot + doctor check in
    :mod:`jasper.multiroom.airplay_latency` / the grouping doctor checks)
    can never disagree about whether the offset is armed — a divergence would
    make the observability lie about what the speaker is actually doing."""
    return is_active_member(cfg) and cfg.role == "leader"


def follower_leader_addr(cfg: GroupingConfig) -> str | None:
    """The leader's handle when ``cfg`` is an ACTIVE bonded FOLLOWER, else
    None. The one predicate behind every pair-forward gate (jasper-control's
    /volume* proxy, the voice tools' loopback reuse of it) — composed from
    :func:`is_active_member` so bond-validity semantics can grow in one
    place. PURE."""
    if is_active_member(cfg) and cfg.role == "follower" and cfg.leader_addr:
        return cfg.leader_addr
    return None
