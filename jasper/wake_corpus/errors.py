# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Refusals the wake-corpus recorder raises to its HTTP adapter."""
from __future__ import annotations


class StateError(RuntimeError):
    """Raised when an operation isn't valid in the current state
    (e.g. starting a recording while one is in progress)."""


# Single user-facing refusal copy, shared by the backend's
# MicMutedError and the wizard's pre-side-effect fast path so the
# household sees one consistent message wherever the gate fires.
MIC_MUTED_MESSAGE = (
    "mic is muted — the wake-corpus recorder will not capture audio "
    "while the household mic mute is on. Unmute from the /system/ "
    "dashboard, then retry."
)


class LifecycleBusyError(StateError):
    pass


class NoRecordingError(StateError):
    pass


class MicMutedError(StateError):
    """Raised when the household mic-mute privacy switch is on.

    Mic mute is a privacy promise (see jasper/mic_mute_persistence.py)
    and is normally enforced inside jasper-voice — but the corpus
    recorder records the bridge's UDP legs directly while jasper-voice
    is stopped, so it must honor the persisted flag itself. Subclasses
    StateError so the wizard's existing error plumbing surfaces the
    message as an HTTP error without new handler branches."""
