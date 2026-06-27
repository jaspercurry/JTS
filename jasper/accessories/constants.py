# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared constants for optional accessory adapters."""

WIIM_REMOTE_2_MIC_UDP_PORT = 9892
WIIM_REMOTE_2_MIC_DEVICE = f"udp:{WIIM_REMOTE_2_MIC_UDP_PORT}"

# Single source of truth for the WiiM Remote 2 Bluetooth advertised-name
# pattern.  Both the GATT voice-characteristic adapter (wiim_remote_mic.py)
# and the registry (registry.py) must use this constant so they can never
# silently drift apart.  Drift failure mode: reconciler activates the adapter
# (registry match) but the adapter's stale local regex fails to find the voice
# characteristic → silent wiim_remote_mic.not_ready retry loop, no audio.
WIIM_REMOTE_2_NAME_RE = r"(?i)\bwiim remote 2\b"
