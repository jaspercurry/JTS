# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install-profile helpers shared by deploy/runtime code.

Small Pi installs have two product axes:

* install profile — full speaker or streambox local-renderer
* output topology — full-range or future active-crossover

There are exactly TWO install profiles: ``full`` and ``streambox``. The
former third tier (``endpoint`` / ``satellite``) is GONE as an install
tier — "endpoint behavior" is now purely the multiroom *follower*
grouping role at runtime (a full/streambox box bonded as a follower
parks its brain and sources; see jasper.multiroom.reconcile). The legacy
tokens are still ACCEPTED here and mapped to ``streambox`` so a field box
with a persisted ``endpoint``/``satellite`` marker auto-migrates on its
next deploy instead of stranding.

Keep this module deliberately tiny and stdlib-only so it can be imported
by lightweight surfaces such as jasper-control, jasper-doctor, and the
multi-room reconciler without pulling in the full speaker stack.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

from .log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_PROFILE = "full"
FULL_INSTALL_PROFILE = "full"
STREAMBOX_INSTALL_PROFILE = "streambox"
INSTALL_PROFILE_FILE = Path("/var/lib/jasper/install_profile")
VALID_INSTALL_PROFILES = frozenset({
    FULL_INSTALL_PROFILE,
    STREAMBOX_INSTALL_PROFILE,
})

# Legacy install-tier tokens kept ONLY for backwards compatibility: a
# persisted marker or env value from before the third tier was removed
# maps to streambox so the box auto-migrates rather than failing closed.
_LEGACY_STREAMBOX_ALIASES = frozenset({"endpoint", "satellite"})


def normalize_install_profile(value: str | None) -> str:
    """Normalize an install-profile token.

    Empty/unset means the historical full-speaker profile. The legacy
    ``endpoint``/``satellite`` tokens map to ``streambox`` (never raise on
    them — that auto-migrates field boxes). Any other invalid value raises
    ``ValueError`` so callers can fail closed.
    """
    raw = (value or "").strip()
    if raw == "":
        return DEFAULT_INSTALL_PROFILE
    if raw in _LEGACY_STREAMBOX_ALIASES:
        return STREAMBOX_INSTALL_PROFILE
    if raw in VALID_INSTALL_PROFILES:
        return raw
    raise ValueError(
        f"invalid install profile {raw!r}; expected full or streambox"
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
    compatibility with every pre-streambox install.

    A persisted/env value carrying a legacy ``endpoint``/``satellite``
    token resolves to ``streambox`` and emits a single greppable
    ``event=install_profile.migrate`` log line so the auto-migration is
    observable.
    """
    marker = Path(path)
    try:
        value = marker.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError):
        value = None
    except OSError:
        value = None

    if value:
        return _normalize_with_migration_log(value, source="marker")

    source = os.environ if env is None else env
    return _normalize_with_migration_log(
        source.get("JASPER_INSTALL_PROFILE"), source="env",
    )


def _normalize_with_migration_log(value: str | None, *, source: str) -> str:
    raw = (value or "").strip()
    normalized = normalize_install_profile(raw)
    if raw in _LEGACY_STREAMBOX_ALIASES:
        log_event(
            logger,
            "install_profile.migrate",
            previous=raw,
            profile=normalized,
            source=source,
        )
    return normalized


def install_role_for_profile(profile: str | None) -> str:
    """Return the product role for an install-profile marker.

    Role == profile now: ``full`` or ``streambox`` (legacy tokens
    normalized to ``streambox``).
    """
    return normalize_install_profile(profile)


def is_streambox_install_profile(profile: str | None) -> bool:
    return install_role_for_profile(profile) == STREAMBOX_INSTALL_PROFILE


def install_profile_runs_local_audio_graph(profile: str | None) -> bool:
    """Whether this profile owns the local renderer -> DSP -> DAC graph.

    True for both ``full`` and ``streambox`` — the only two profiles.
    """
    return install_role_for_profile(profile) in {
        FULL_INSTALL_PROFILE,
        STREAMBOX_INSTALL_PROFILE,
    }


def install_profile_allows_local_sources(profile: str | None) -> bool:
    """Whether this install role may advertise/run local music sources."""
    return install_profile_runs_local_audio_graph(profile)


def install_profile_allows_content_dsp(profile: str | None) -> bool:
    """Whether local EQ/room-correction DSP belongs on this box."""
    return install_profile_runs_local_audio_graph(profile)


def install_profile_allows_voice_brain(profile: str | None) -> bool:
    """Whether voice, wake, mic/AEC, and assistant integrations run locally."""
    return install_role_for_profile(profile) == FULL_INSTALL_PROFILE


def system_capabilities_for_profile(profile: str | None) -> dict[str, object]:
    """The management-UI capability map for an install profile.

    Single source of truth, shared by two consumers: jasper-control's
    /system snapshot (runtime) AND install.sh, which bakes the result into
    the static landing page so its capability-gated sections are correct at
    first paint with no network round-trip. Kept here (stdlib-only) so the
    installer can compute it without importing the full control stack.

    Values are derived purely from the profile, so the baked page and the
    live snapshot always agree for the same marker.
    """
    role = install_role_for_profile(profile)
    full = role == FULL_INSTALL_PROFILE
    local_dsp = install_profile_allows_content_dsp(profile)
    local_sources = install_profile_allows_local_sources(profile)
    voice_brain = install_profile_allows_voice_brain(profile)
    return {
        # `install_profile` echoes the token this is CALLED with; the boolean
        # caps below — what the page gates on — derive from the normalized
        # role. In production both callers (the /system snapshot and the
        # install.sh bake) pass read_install_profile(), which already
        # normalizes endpoint/satellite -> streambox, so this field reads
        # full|streambox and baked vs live always agree. A raw legacy token
        # only appears if the function is called directly with one.
        "install_profile": profile,
        "role": role,
        "local_sources": local_sources,
        "content_dsp": local_dsp,
        "voice_brain": voice_brain,
        "network_settings": True,
        "speaker_settings": True,
        "pair_management": True,
        "developer_tools": full,
        "audio_quality": local_dsp,
        "restart_voice": voice_brain,
        "restart_audio": local_dsp,
        "reboot": True,
        "poweroff": True,
        "diagnostics": True,
    }
