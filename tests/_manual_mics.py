# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared fake push-to-talk remote every WakeLoop/PushToTalk test wants.

Every push-to-talk test builds the same `ManualMicRuntime` for the one
remote source the fixtures care about — the source id and device path
never vary, only whether the test needs a mic double that actually behaves
(`_IdleMic()` and friends) rather than a bare sentinel.
"""
from __future__ import annotations

from jasper.voice.push_to_talk import ManualMicRuntime


def remote_mic(mic: object | None = None) -> ManualMicRuntime:
    """A `ManualMicRuntime` for the shared fake `wiim_remote_2` source.

    `mic` defaults to a bare `object()` sentinel — the double tests use
    when nothing reads frames from it; pass a real mic double when the
    test needs frame behavior.
    """
    return ManualMicRuntime(
        "wiim_remote_2", object() if mic is None else mic, "udp:9892",
    )
