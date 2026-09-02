# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Start verdicts for local music sources, published as marker files.

One source's verdict is household intent plus current grouping role (plus, for
USB, the physical data-role capability); the shared verdict is role only, for
infrastructure with no single source intent.  The source coordinator
(:mod:`jasper.source_intent`) is the single writer; every gated unit consumes
one marker as ``ConditionPathExists=``, so an absent marker — or an absent
directory before the first pass — blocks the start.  See ADR-0221.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..log_event import log_event
from ..music_sources import Source
from ..multiroom.config import load_config
from ..multiroom.effective_role import (
    effective_local_sources_park_reason,
    read_effective_role_status,
)
from ..output_hardware import current_usb_data_role
from ..source_intent import source_intent_enabled
from .registry import local_source_lifecycles

logger = logging.getLogger(__name__)

MARKER_DIR = "/run/jasper-source-intent/allowed"
# Not a source id: the role-only verdict carried by shared infrastructure that
# has no single source intent (jasper-mux). Cannot collide with a Source value.
SHARED_LABEL = "shared"


def marker_path(label: str) -> str:
    return f"{MARKER_DIR}/{label}"


def _set_marker(label: str, allowed: bool) -> None:
    path = Path(marker_path(label))
    if allowed:
        path.touch(mode=0o644)
    else:
        path.unlink(missing_ok=True)


def local_sources_allowed() -> tuple[bool, str | None]:
    """Return whether this speaker may run/advertise local sources.

    Unexpected config read/parse failures normally fail open. An existing
    reconciler-owned deny remains authoritative, though: losing the requested
    config must not reopen sources midway through a role transition. Missing or
    untrusted status still fails open so a solo speaker is not bricked because
    this check could not read state.
    """
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "local_sources.role_read_failed",
            error=e,
            level=logging.WARNING,
        )
        prior_status = read_effective_role_status()
        if prior_status.get("local_sources_allowed") is False:
            return False, str(
                prior_status.get("blocked_reason")
                or "role_transition_in_progress"
            )
        return True, None
    reason = effective_local_sources_park_reason(cfg)
    return reason is None, reason


def local_source_allowed(source: Source) -> tuple[bool, str | None]:
    """Return whether one declared source may start right now.

    Role-read failures retain the existing availability-biased behavior for a
    solo speaker.  Intent is different: it is the household's canonical Off
    switch, so unreadable or malformed intent must fail closed at the start
    boundary instead of falling back to a shipped default.
    """

    allowed, reason = local_sources_allowed()
    if not allowed:
        return False, reason
    try:
        enabled = source_intent_enabled(source)
    except RuntimeError as exc:
        log_event(
            logger,
            "local_sources.intent_read_failed",
            source=source.value,
            error=exc,
            level=logging.WARNING,
        )
        return False, "source_intent_invalid"
    if not enabled:
        return False, "source_intent_disabled"
    if source == Source.USBSINK:
        try:
            usb_role = current_usb_data_role()
        except (OSError, RuntimeError, ValueError) as exc:
            log_event(
                logger,
                "local_sources.usb_role_failed",
                error=exc,
                level=logging.WARNING,
            )
            return False, "usb_role_unavailable"
        if not usb_role.gadget_available:
            return False, usb_role.reason
    return True, None


def publish_allowed_markers() -> dict[str, tuple[bool, str | None]]:
    """Mirror every start verdict into ``MARKER_DIR`` and return the verdicts.

    Call before the coordinator's apply loop: a marker states intent and role,
    never whether an apply later succeeded (ADR-0191).
    """

    Path(MARKER_DIR).mkdir(mode=0o755, parents=True, exist_ok=True)
    verdicts: dict[str, tuple[bool, str | None]] = {
        SHARED_LABEL: local_sources_allowed(),
    }
    for lifecycle in local_source_lifecycles():
        verdicts[lifecycle.source.value] = local_source_allowed(lifecycle.source)
    for label, (allowed, reason) in verdicts.items():
        _set_marker(label, allowed)
        log_event(
            logger,
            "local_sources.marker_published",
            label=label,
            allowed=allowed,
            reason=reason,
            level=logging.INFO,
        )
    return verdicts
