# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard for the USB audio-level threshold — now DISPLAY-ONLY.

History: this threshold used to gate mux's source arbitration ("a Mac streaming
digital silence must not seize the speaker"). The 2026-07-17 liveness rework
removed that gate: USB liveness is now purely frames-based (see
`jasper.mux.step_combo_liveness`). Removing the gate fixed dropped faint audio
and level-driven quiet-passage dropouts on browser video. Since 2026-07-22, the
frame-flow edge enters the same latest-start-wins policy as every other source;
pinning a source or disabling USB are the explicit opt-outs. fan-in publishes
that edge at 20 Hz and wakes mux directly, while the 1 Hz patrol is only a
lost-alert fallback.

`jasper.source_state.USBSINK_PLAYING_RMS_DBFS` survives only as the level shown
on the `/state` dashboard (via `usbsink_direct_audible`, read by
`jasper.control.state_aggregate`). This test pins that it is (a) still a single
shared definition and (b) NO LONGER referenced by the arbiter `jasper.mux`, so a
future edit can't silently re-gate arbitration on audio level. See AGENTS.md and
docs/HANDOFF-usbsink.md.
"""
from __future__ import annotations

from jasper import mux, source_state
from jasper.control import state_aggregate


def test_usbsink_playing_rms_dbfs_value():
    """Pin the display threshold so a change to it is deliberate, not accidental."""
    assert source_state.USBSINK_PLAYING_RMS_DBFS == -60.0


def test_state_aggregate_is_the_display_consumer():
    """The threshold's only remaining job is the /state level readout, via
    `usbsink_direct_audible`. Pin that the display module still imports it so the
    'display-only' rationale above stays true."""
    assert hasattr(state_aggregate, "usbsink_direct_audible")
    assert (
        state_aggregate.usbsink_direct_audible
        is source_state.usbsink_direct_audible
    )


def test_mux_no_longer_gates_arbitration_on_level():
    """The arbiter must not reference the audio-level threshold at all — the
    source-neutral policy operates on confirmed frame-flow transitions. A future
    edit that re-imports the gate into mux would resurrect the
    dropped-faint-audio / startup-lag / quiet-dropout class this fixed."""
    assert not hasattr(mux, "USBSINK_PLAYING_RMS_DBFS")
    assert not hasattr(mux, "usbsink_direct_rms_dbfs")
