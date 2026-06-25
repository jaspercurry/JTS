# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Systemd start gate for role-parked local music sources.

The grouping role policy is the single source of truth for whether local
sources may run. Systemd units call this via ExecCondition so a bonded
follower cannot briefly advertise or start a local source during boot before
the reconciler gets a chance to stop it.
"""
from __future__ import annotations

import logging

from ..log_event import log_event
from ..multiroom.config import load_config, local_sources_park_reason

logger = logging.getLogger(__name__)


def local_sources_allowed() -> tuple[bool, str | None]:
    """Return whether this speaker may run/advertise local sources.

    Unexpected read/parse failures fail open. A malformed grouping env already
    makes the bond invalid and therefore does not park local sources; bricking a
    solo speaker's renderers because a tiny guard could not read state would be
    the more dangerous operational failure.
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
        return True, None
    reason = local_sources_park_reason(cfg)
    return reason is None, reason


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    allowed, reason = local_sources_allowed()
    if allowed:
        return 0
    log_event(
        logger,
        "local_sources.guard_parked",
        reason=reason,
        level=logging.INFO,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
