# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Retired-device names the transport/reconcile suites assert against."""

from __future__ import annotations

# The retired snd-aloop pair (ADR-0100, ADR-0262). No product module names
# either half any more; they live here as the graph an unreconciled box can
# still present.
RETIRED_ALOOP_CAPTURE_DEVICE = "plug:jasper_capture"
RETIRED_ALOOP_PLAYBACK_DEVICE = "outputd_content_playback"
