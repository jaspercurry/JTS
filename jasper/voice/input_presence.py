# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice input (microphone) presence marker — shared reader.

`jasper-aec-reconcile` is the single *writer* of a persistent negative
marker that means "the reconciler positively determined there is no
usable microphone, so jasper-voice is intentionally parked." Several
read-only surfaces need to consult it:

- `jasper-voice.service` gates `ExecStart` on it via
  ``ConditionPathExists=!<marker>`` (evaluated by PID 1, not this
  module) so the daemon never start-crash-loops on a missing mic.
- `jasper-doctor` reports the parked state as *expected idle* rather
  than *broken*.
- `/state` exposes ``voice.parked_no_mic`` so the dashboard can tell
  ``reachable:false`` "no mic, idle" from "crashed".

Negative polarity + persistent storage are deliberate; see
``docs/HANDOFF-hotplug-resilience.md`` "Layer 1". The marker is the
*absence* signal so the default (no file) fails **open** — voice runs
unless the reconciler has explicitly said there is no mic — and it lives
in ``/var/lib/jasper`` (persistent, not ``/run``) so a no-mic box is
gated from the very first instant of boot, before any reconcile runs.

The path is duplicated as a literal in the systemd unit
(``ConditionPathExists=``) and the bash reconciler; the agreement is
pinned by ``tests/test_voice_input_gate.py``. ``JASPER_VOICE_INPUT_ABSENT_MARKER``
overrides it (tests, nonstandard layouts) and must match the override
the reconciler reads.
"""
from __future__ import annotations

import os

# Keep in lockstep with deploy/bin/jasper-aec-reconcile's
# VOICE_INPUT_ABSENT_MARKER default and jasper-voice.service's
# ConditionPathExists path. tests/test_voice_input_gate.py asserts all
# three agree.
DEFAULT_VOICE_INPUT_ABSENT_MARKER = "/var/lib/jasper/voice-input-absent"


def voice_input_absent_marker_path() -> str:
    """Resolved marker path (env override wins, for tests/odd layouts)."""
    return os.environ.get(
        "JASPER_VOICE_INPUT_ABSENT_MARKER",
        DEFAULT_VOICE_INPUT_ABSENT_MARKER,
    )


def voice_parked_no_mic() -> bool:
    """True when the AEC reconciler has marked the speaker as having no
    usable microphone (so jasper-voice is intentionally parked).

    Fail-safe to False: an unreadable/erroring stat must never *invent*
    a no-mic state — the gate's whole point is that only a positive
    reconciler verdict withholds voice.
    """
    try:
        return os.path.exists(voice_input_absent_marker_path())
    except OSError:
        return False
