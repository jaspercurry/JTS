"""Install-tier helpers shared by deploy/runtime code.

The Raspberry Pi Zero 2 W endpoint is an install profile, not a second
runtime role. Keep this module deliberately tiny and stdlib-only so it
can be imported by endpoint-safe surfaces such as jasper-control,
jasper-doctor, and the multi-room reconciler without pulling in the
full speaker stack.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


DEFAULT_INSTALL_PROFILE = "full"
ENDPOINT_INSTALL_PROFILE = "endpoint"
FULL_INSTALL_PROFILE = "full"
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
    if raw in VALID_INSTALL_PROFILES:
        return raw
    raise ValueError(
        f"invalid install profile {raw!r}; expected full or endpoint"
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
