# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CamillaDSP test double for the transport/reconcile suites."""

from __future__ import annotations

# The retired snd-aloop pair (ADR-0100, ADR-0262). No product module names
# either half any more; they live here as the graph an unreconciled box can
# still present.
RETIRED_ALOOP_CAPTURE_DEVICE = "plug:jasper_capture"
RETIRED_ALOOP_PLAYBACK_DEVICE = "outputd_content_playback"


class FakeCamilla:
    """Reports one loaded config path and records what it was asked to load.

    Deliberately independent of the statefile the derivation reads: a stub
    that conflated the two could not express a PRE-arm/ring split (#2339).
    """

    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        self.loaded_path = path
        return True
