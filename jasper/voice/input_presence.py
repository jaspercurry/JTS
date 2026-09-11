# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice input (microphone) presence marker — shared reader.

`jasper-aec-reconcile` is the single *writer* of a persistent negative
marker meaning "no usable voice input; jasper-voice is intentionally
parked." Usable voice input is an OR of two independently-owned facts,
and the marker is the AND of their absences:

- a **local** microphone, resolved by `jasper-aec-reconcile` itself;
- an **accessory** microphone, published as `JASPER_MANUAL_MIC_SOURCES`
  (`jasper.accessories.mic_env`).

The reconciler reads the accessory owner's *published file*, never BlueZ
(issue #2205). `ConditionPathExists` cannot express an AND, which is why
this is one marker computed before a single write, not two.

**This is a start gate, not a runtime guarantee, and it cannot say WHICH
half answered.** Its absence does not distinguish "local mic present" from
"no local mic, but a remote is paired" — for that, the reconciler publishes
``JASPER_LOCAL_MIC_PRESENT`` (``1`` / ``0`` / ``unknown``) separately, read
by ``Config.local_mic_present``. Do not recover that fact from this
marker, and nothing derived from it may claim voice *is running* on the
accessory — this file only knows about starting.

Consumed read-only by `jasper-voice.service` (``ConditionPathExists=!
<marker>``), `jasper-doctor` (reports the parked state as expected idle),
and `/state` (``voice.parked_no_mic``).

Negative polarity fails **open**: voice runs unless the reconciler
positively said otherwise. It lives in ``/var/lib/jasper`` (persistent,
not ``/run``) so a no-input box is gated from boot's first instant.
"""
from __future__ import annotations

import os
from pathlib import Path

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


def voice_input_absent_marker_lines() -> list[str]:
    """Marker body lines; ``[]`` when it is missing or unreadable.

    The single read of the file. What the body *means* — the ``reason=`` code
    vocabulary, its ``detail=`` prose, and which codes are transient parks —
    belongs to ``jasper.mic_presence``, which cannot be imported from here
    (it imports this module).
    """
    try:
        return Path(voice_input_absent_marker_path()).read_text().splitlines()
    except OSError:
        return []


def voice_parked_no_mic() -> bool:
    """True when the AEC reconciler marked the speaker as having no usable
    voice input, so jasper-voice is intentionally parked.

    Fail-safe to False: an unreadable/erroring stat must never *invent*
    a no-mic state — the gate's whole point is that only a positive
    reconciler verdict withholds voice.
    """
    try:
        return os.path.exists(voice_input_absent_marker_path())
    except OSError:
        return False
