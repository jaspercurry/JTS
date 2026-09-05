# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice input (microphone) presence marker — shared reader.

`jasper-aec-reconcile` is the single *writer* of a persistent negative
marker that means "the reconciler positively determined there is no
usable voice input, so jasper-voice is intentionally parked."

"Usable voice input" is an OR over two independently-owned facts, and the
marker is the AND of their absences:

- a **local** microphone, resolved by `jasper-aec-reconcile` itself;
- an **accessory** microphone, resolved by `jasper-accessory-reconcile` and
  published as `JASPER_MANUAL_MIC_SOURCES` (`jasper.accessories.mic_env`).

The AEC reconciler consults the accessory owner's *published file* — never
BlueZ — before it writes the marker, so a box with no local mic but a paired
mic-bearing remote is no longer gated shut (issue #2205). It remains the single
writer; when the accessory half moves, `jasper-accessory-reconcile` starts it
(when that unit is enabled on this profile) rather than writing here itself.
`ConditionPathExists` cannot express an AND, which is exactly why the AND is
computed before the single write instead of split across two markers.

**This is a start gate, not a runtime guarantee, and it cannot say WHICH half
answered.** Its presence means "neither"; its absence does not distinguish "a
local mic is present" from "no local mic, but a remote is paired" — and the
daemon needs exactly that distinction to decide whether to plan a wake leg at
all. So the AEC reconciler publishes its own half separately as
``JASPER_LOCAL_MIC_PRESENT`` (``1`` / ``0`` / ``unknown``), which
``Config.local_mic_present`` reads and ``_configured_wake_legs`` acts on. Do
not try to recover that fact from this marker, and nothing derived from this
marker may phrase itself as "voice is running on the accessory" — that is a
runtime claim and this file only knows about starting.

Several read-only surfaces need to consult it:

- `jasper-voice.service` gates `ExecStart` on it via
  ``ConditionPathExists=!<marker>`` (evaluated by PID 1, not this
  module) so the daemon never start-crash-loops on a missing mic.
- `jasper-doctor` reports the parked state as *expected idle* rather
  than *broken*.
- `/state` exposes ``voice.parked_no_mic`` so the dashboard can tell
  ``reachable:false`` "no mic, idle" from "crashed".

Negative polarity + persistent storage are deliberate. The marker is the
*absence* signal so the default (no file) fails **open** — voice runs
unless the reconciler has explicitly said there is no input — and it lives
in ``/var/lib/jasper`` (persistent, not ``/run``) so a no-input box is
gated from the very first instant of boot, before any reconcile runs.
That cold-boot property is preserved by the accessory OR: a box with *no*
input at all still carries the marker from its last reconcile and is gated at
instant zero, while a box whose accessory satisfies the gate correctly boots
with it open. A pairing change made while the box was off converges on the
first accessory reconcile of the next boot, without a reboot.

The path is duplicated as a literal in the systemd unit
(``ConditionPathExists=``) and the bash reconciler; the agreement is
pinned by ``tests/test_voice_input_gate.py``. ``JASPER_VOICE_INPUT_ABSENT_MARKER``
overrides it (tests, nonstandard layouts) and must match the override
the reconciler reads.
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


def voice_parked_no_mic() -> bool:
    """True when the AEC reconciler has marked the speaker as having no
    usable voice input — neither a local microphone nor a paired accessory
    one — so jasper-voice is intentionally parked.

    Fail-safe to False: an unreadable/erroring stat must never *invent*
    a no-mic state — the gate's whole point is that only a positive
    reconciler verdict withholds voice.
    """
    try:
        return os.path.exists(voice_input_absent_marker_path())
    except OSError:
        return False


def voice_park_is_transient() -> bool:
    """True when the current park is a transient round trip — e.g. the
    chip-AEC validation bounce (``jasper-aec-reconcile``'s
    ``activate_managed_chip_aec``, ADR-0238) — rather than a real absence of
    voice input. Meaningless unless ``voice_parked_no_mic()`` is also true.

    Fail-safe to False: a missing marker, an unreadable one, or one with no
    ``transient=1`` line all read as "not transient", so a real absence can
    never be misread as transient and lose its shutdown cue.
    """
    try:
        body = Path(voice_input_absent_marker_path()).read_text()
    except OSError:
        return False
    return any(line.strip() == "transient=1" for line in body.splitlines())
