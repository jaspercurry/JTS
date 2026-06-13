"""Install-role helpers shared by deploy/runtime code.

Small Pi installs have two product axes:

* install role — full speaker, satellite, or future streambox
* output topology — full-range or future active-crossover

The deployed compatibility marker for the built satellite role is still
``endpoint``. Keep this module deliberately tiny and stdlib-only so it
can be imported by endpoint-safe surfaces such as jasper-control,
jasper-doctor, and the multi-room reconciler without pulling in the full
speaker stack.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


DEFAULT_INSTALL_PROFILE = "full"
ENDPOINT_INSTALL_PROFILE = "endpoint"
FULL_INSTALL_PROFILE = "full"
SATELLITE_INSTALL_ROLE = "satellite"
INSTALL_PROFILE_FILE = Path("/var/lib/jasper/install_profile")
VALID_INSTALL_PROFILES = frozenset({
    FULL_INSTALL_PROFILE,
    ENDPOINT_INSTALL_PROFILE,
})


def normalize_install_profile(value: str | None) -> str:
    """Normalize an install-profile token.

    Empty/unset means the historical full-speaker profile. Invalid values
    raise ``ValueError`` so callers can fail closed.
    """
    raw = (value or "").strip()
    if raw == "":
        return DEFAULT_INSTALL_PROFILE
    if raw == SATELLITE_INSTALL_ROLE:
        return ENDPOINT_INSTALL_PROFILE
    if raw in VALID_INSTALL_PROFILES:
        return raw
    raise ValueError(
        f"invalid install profile {raw!r}; expected full, endpoint, or satellite"
    )


def read_install_profile(
    *,
    path: str | os.PathLike[str] = INSTALL_PROFILE_FILE,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read the active install profile.

    The persisted marker is authoritative once present. ``JASPER_INSTALL_PROFILE``
    is a fallback for tests and early install-time processes before the marker
    exists; absent marker + absent env returns ``"full"`` for backwards
    compatibility with every pre-endpoint install.
    """
    marker = Path(path)
    try:
        value = marker.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError):
        value = None
    except OSError:
        value = None

    if value:
        return normalize_install_profile(value)

    source = os.environ if env is None else env
    return normalize_install_profile(source.get("JASPER_INSTALL_PROFILE"))


def is_endpoint_install(
    *,
    path: str | os.PathLike[str] = INSTALL_PROFILE_FILE,
    env: Mapping[str, str] | None = None,
) -> bool:
    return read_install_profile(path=path, env=env) == ENDPOINT_INSTALL_PROFILE


def install_role_for_profile(profile: str | None) -> str:
    """Return the product role for a persisted install-profile marker."""
    normalized = normalize_install_profile(profile)
    if normalized == ENDPOINT_INSTALL_PROFILE:
        return SATELLITE_INSTALL_ROLE
    return normalized


def is_satellite_install_profile(profile: str | None) -> bool:
    return install_role_for_profile(profile) == SATELLITE_INSTALL_ROLE


def install_profile_allows_local_sources(profile: str | None) -> bool:
    """Whether this install role may advertise/run local music sources."""
    role = install_role_for_profile(profile)
    return role == FULL_INSTALL_PROFILE
