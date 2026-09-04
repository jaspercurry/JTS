# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-source seam: what a capture provider owes the conductor.

Decision 13, #2662: the conductor asks for a capture of program X at
position Y and a provider answers with WAV plus metadata; how a source
produces that recording is its own private internals.

Logic-free vocabulary only, same register as :mod:`.refusal_copy` and
:mod:`.contracts`. The three conductor-owned hooks a provider is handed:
``authorize_begin(index, attempt, entry)``, ``on_armed(state)`` and
``consume_capture(index, attempt, answer)``, plus the stop/completion
predicates — shipped by
``jasper.web.correction_crossover_v2_wired.build_v2_wired_run_and_consume``.

Two ownership rules: the provider mints the session identity (the bundle id
is canonical for ATTRIBUTION; the provider's rides as the
``ALIAS_CAPTURE_SESSION_ID`` alias), and the host owns the persisted-code
mapping, frozen because hydration and incident replay depend on it.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
)


class CaptureBeginRefused(RuntimeError):
    """The conductor refused a provider's ``begin_capture`` request.

    Admission stays conductor-owned (SPEC W2.3). ``code`` is the stable
    machine reason; ``user_message`` is the operator-facing copy
    (refusal-naming pattern, #1534)."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


class CaptureBeginDeferred(RuntimeError):
    """A NON-terminal soft-hold on a ``begin_capture``.

    Distinct from :class:`CaptureBeginRefused`: the plan stays alive and the
    provider holds the capture in the SAME ``awaiting_begin`` phase, so the
    identical ``begin_capture {index, attempt}`` retries without spending the
    attempt budget. See crossover-measurement-productization-design.md §5.7.
    ``code``/``user_message`` mirror ``CaptureBeginRefused``. The runner
    dedupes retries on the (index, code) transition: one INFO log per state
    change, DEBUG for identical repeats."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


class CaptureFailed(RuntimeError):
    """A capture could not be produced or trusted (see ``__cause__``)."""


class CaptureStopped(RuntimeError):
    """The host explicitly stopped this capture."""


#: The per-take integrity counters a provider's answer carries, spelled
#: exactly as :mod:`jasper.audio_measurement.frame_ledger` defines them —
#: never respelled — so a provider can iterate the contract instead of
#: quoting strings.
INTEGRITY_COUNTER_KEYS = (
    REPORT_KEY_FRAMES,
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
)


@runtime_checkable
class CaptureAnswer(Protocol):
    """One capture, answered: the WAV plus the metadata the session consumes.

    Attribute contract, structural on purpose: an answer is any object
    carrying the same four facts. Every field but ``wav`` is ``None`` when the
    source cannot state it; ``None`` means *not reported*, never a clean
    default.
    """

    #: The captured audio, complete WAV bytes (the analyzer decodes format and
    #: rate from the container itself).
    wav: bytes

    #: The microphone identity as the source observed it
    #: (``{"label", "device_id", ...}``). The session's calibration
    #: resolver checks it against the calibration choice so a
    #: curve is never applied to a different microphone than the one that
    #: recorded.
    device: Mapping[str, Any] | None

    #: The mic/cal identity REFERENCE — which calibration the operator chose
    #: (mode, calibration id, serial). Resolved into a stored record by the
    #: session's injected resolver; the answer only carries the reference.
    #: Injected at
    #: ``jasper.web.correction_crossover_v2.bind_production_analyze(resolve_calibration=...)``.
    setup: Mapping[str, Any] | None

    #: The source's own per-take account of the recording, carrying the
    #: :data:`INTEGRITY_COUNTER_KEYS` counters that
    #: ``jasper.audio_measurement.frame_ledger.reconcile_capture_frames``
    #: reconciles against the frames the host actually decoded: the wired
    #: source counts its own capture chain into these keys.
    capture_integrity: Mapping[str, Any] | None
