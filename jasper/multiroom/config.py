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

import logging
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

# Snapcast stream codec. "flac" is the lossless default (good drift
# tolerance, modest CPU); "pcm" is uncompressed (lowest CPU, highest
# bandwidth); "opus" is lossy (lowest bandwidth). A value outside this
# set on an ENABLED config is fail-LOUD (sets `error`), never silently
# corrected — an unknown codec would make snapserver refuse to start.
ALLOWED_CODECS = ("pcm", "flac", "opus")
DEFAULT_CODEC = "flac"


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


def _parse_buffer_ms(raw: str) -> int:
    """Parse the playout buffer; non-int falls back to the default,
    then clamp into [BUFFER_MS_LO, BUFFER_MS_HI]. Never an error.
    """
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_BUFFER_MS
    return max(BUFFER_MS_LO, min(BUFFER_MS_HI, val))


def validate_grouping(
    *,
    role: str,
    channel: str,
    bond_id: str,
    leader_addr: str,
    codec: str = DEFAULT_CODEC,
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

    error = validate_grouping(
        role=role,
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        codec=codec,
    )

    return GroupingConfig(
        enabled=True,
        role=role,
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        buffer_ms=buffer_ms,
        codec=codec,
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


def disables_local_rate_adjust(cfg: GroupingConfig) -> bool:
    """inv-5 (docs/HANDOFF-multiroom.md §2): an ACTIVE bond member must run its
    LOCAL CamillaDSP with ``enable_rate_adjust: false``.

    snapclient's sample-stuffing is the single rate-tracker for the whole
    synced chain; a second rate-adjuster in the member's local CamillaDSP
    fights it and oscillates (the documented ``rate_adjust`` + ``AsyncSinc``
    trap). True => the member's generated config must set rate_adjust off.
    Solo / off / invalid => False (normal local rate_adjust, unchanged).

    This is the predicate the CamillaDSP config generators consult and that
    ``jasper-doctor`` checks the ACTIVE config against. Currently identical to
    :func:`is_active_member`; kept as its own named intent so the inv-5 rule
    has one home and reads clearly at every call site."""
    return is_active_member(cfg)
