# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Restart saved settings through the broker and the voice availability gates."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from jasper.local_sources.markers import local_sources_allowed
from jasper.platform.service_units import JASPER_VOICE_SERVICE
from jasper.voice.provider_state import read_active_provider
from .restart_broker import manage_units

logger = logging.getLogger(__name__)


class RestartOutcome(Enum):
    RAN = "ran"
    SKIPPED = "skipped"
    REFUSED = "refused"


def restart_systemd_units(*units: str) -> RestartOutcome:
    # Type=notify restarts must not wait for voice model loading (8–12 s on a Pi).
    if not units:
        return RestartOutcome.SKIPPED
    resp = manage_units(*units, verb="restart", reason="wizard config change", no_block=True, timeout=5.0)
    return RestartOutcome.RAN if resp.get("ok") else RestartOutcome.REFUSED


def bonded_follower_active() -> bool:
    return not local_sources_allowed()[0]


@dataclass(frozen=True)
class RestartResult:
    outcome: RestartOutcome
    reason: str | None = None


def voice_restart_skip_reason() -> str | None:
    if not read_active_provider():
        return "provider_unset"
    return "bonded_follower" if bonded_follower_active() else None


def restart_voice_daemon() -> RestartResult:
    if reason := voice_restart_skip_reason():
        logger.info("not restarting jasper-voice: %s", reason)
        return RestartResult(RestartOutcome.SKIPPED, reason)
    # Boot enable/disable belongs to aec-reconcile; see deploy/polkit/49-jasper-control.rules.
    return RestartResult(restart_systemd_units(JASPER_VOICE_SERVICE))
