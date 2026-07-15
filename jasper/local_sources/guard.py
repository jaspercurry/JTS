# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Systemd start gate for local music sources.

Systemd units call this via ``ExecCondition``.  The generic form protects
shared infrastructure from follower-role parking; ``--source <id>`` also
checks that source's current household intent.  The latter is the final,
fail-closed start boundary: stale systemd enablement, boot ordering, and a
maintenance restore cannot resurrect a source the household turned Off.
"""
from __future__ import annotations

import argparse
import logging

from ..log_event import log_event
from ..music_sources import Source
from ..multiroom.config import load_config
from ..multiroom.effective_role import (
    effective_local_sources_park_reason,
    read_effective_role_status,
)
from ..source_intent import source_intent_enabled
from .registry import local_source_lifecycles

logger = logging.getLogger(__name__)


def local_sources_allowed() -> tuple[bool, str | None]:
    """Return whether this speaker may run/advertise local sources.

    Unexpected config read/parse failures normally fail open. An existing
    reconciler-owned deny remains authoritative, though: losing the requested
    config must not reopen sources midway through a role transition. Missing or
    untrusted status still fails open so a solo speaker is not bricked because
    this tiny guard could not read state.
    """
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "local_sources.guard_read_failed",
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
            "local_sources.guard_intent_failed",
            source=source.value,
            error=exc,
            level=logging.WARNING,
        )
        return False, "source_intent_invalid"
    if not enabled:
        return False, "source_intent_disabled"
    return True, None


def _source_choices() -> tuple[str, ...]:
    """Fixed CLI vocabulary derived from the lifecycle registry."""

    return tuple(lifecycle.source.value for lifecycle in local_source_lifecycles())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jasper-local-source-allowed")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--source", choices=_source_choices())
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    source = Source(args.source) if args.source is not None else None
    if source is None:
        allowed, reason = local_sources_allowed()
    else:
        allowed, reason = local_source_allowed(source)
    if allowed:
        return 0
    log_event(
        logger,
        "local_sources.guard_parked",
        source=source.value if source is not None else "(shared)",
        reason=reason,
        level=logging.INFO,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
