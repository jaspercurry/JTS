# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the USB "playing" RMS gate.

A host is treated as "playing" only when its audio level is above a shared
threshold — so a Mac streaming digital silence (a muted Zoom, an idle tab) does
NOT seize the speaker. The gate is Python-only now: `jasper.source_state.
USBSINK_PLAYING_RMS_DBFS` is applied to fan-in's DIRECT-capture lane's reported
`rms_dbfs` (the live USB path), gating mux's combo-liveness check
(`usbsink_direct_audible` / `step_combo_liveness`) and the `/state` renderer
level.

The retired Rust bridge no longer exists, so there is no second implementation
to drift from. `jasper.mux` imports `USBSINK_PLAYING_RMS_DBFS` from
`jasper.source_state` rather than declaring its own value, so the remaining
single-source-of-truth risk is Python-internal: a future edit could give
`jasper.mux` a local literal instead of importing the shared constant. This
test pins that instead of a cross-language pair. See AGENTS.md.
"""
from __future__ import annotations

from jasper import mux, source_state


def test_usbsink_playing_rms_dbfs_is_a_single_shared_definition():
    """`jasper.mux` must import the threshold, not re-declare its own copy.

    Identity (`is`), not just equality — a future edit that gives mux.py a
    local literal instead of importing `source_state.USBSINK_PLAYING_RMS_DBFS`
    would silently fork the gate even if the value still matched today."""
    assert mux.USBSINK_PLAYING_RMS_DBFS is source_state.USBSINK_PLAYING_RMS_DBFS


def test_usbsink_playing_rms_dbfs_value():
    """Pin the actual threshold so a change to it is deliberate, not accidental."""
    assert source_state.USBSINK_PLAYING_RMS_DBFS == -60.0
