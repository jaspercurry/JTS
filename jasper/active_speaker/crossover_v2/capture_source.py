# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-source seam: what a capture provider owes the conductor.

Decision 13 (#2662, ruled 2026-08-17): the conductor asks for *a capture of
program X at position Y*, and a provider answers with WAV plus metadata. How a
source arranges that recording is its own private internals, never the
conductor's or the web host's.

This module is the seam's VOCABULARY, deliberately logic-free (the same
register as :mod:`.refusal_copy` and :mod:`.contracts`): the source identity,
the answer contract, and the exceptions the conversation is carried on. The
halves of the conversation live here or where they already are:

* **The ask.** The plan walk hands a provider three conductor-owned hooks —
  ``authorize_begin(index, attempt, entry)`` (admission, and the position
  gate ahead of it), ``on_armed(state)`` (the host plays program X), and
  ``consume_capture(index, attempt, answer)`` (the answer lands) — plus the
  stop/completion predicates. The arity asymmetry is real and not an
  oversight: ``authorize_begin`` reads the entry because it gates on the
  prompted spot, ``consume_capture`` never read its own, so that parameter
  went. ``run_capture_plan`` appends the entry only to a hook that declares
  room for one, so both arities are served by one walk. The shipped carrier
  of that hook set is
  ``jasper.web.correction_crossover_v2_wired.build_v2_wired_run_and_consume``.
* **The two ways to say no.** :class:`CaptureBeginRefused` (terminal) and
  :class:`CaptureBeginDeferred` (a soft hold) below. They are the conductor's
  answers to ``authorize_begin``, so they are seam vocabulary and not a
  provider's: the provider raises and translates them.
* **The two ways a capture ends badly.** :class:`CaptureFailed` (the capture
  could not be produced or trusted) and :class:`CaptureStopped` (the host
  stopped it). Capture vocabulary for the same reason the pair above is: the
  provider raises them and the host maps them onto its own reason codes.
* **The answer.** :class:`CaptureAnswer` below, minted by the provider and
  pinned by ``tests/test_crossover_v2_capture_source.py``.

Two ownership rules the seam holds, so a second provider cannot move them by
accident:

* **The provider mints the session identity.** The durable v2 state, the
  evidence publishers, and the phase artifacts are all keyed by the session id
  the provider's open returned. One scoping clause: for ATTRIBUTION the
  **bundle** session id stays canonical — the bundle is the retention unit —
  and the provider's id rides as an alias
  (``jasper.web.correction_crossover_v2.v2_session_identity``, alias key
  ``ALIAS_RELAY_SESSION_ID``).
* **The host owns the persisted-code mapping.** What crosses the seam when a
  session dies is the flow's own reason vocabulary (``REASON_USER_STOPPED``,
  … — :mod:`.refusal_copy` and the flow's ``REASON_REGISTRY``), never a
  provider's exception types: the provider translates its own faults before
  the host persists. The persisted codes themselves are frozen — hydration
  and incident replay depend on them.

Mic and calibration identity resolve at an injection point that already
exists: ``bind_production_analyze(resolve_calibration=...)`` in
``jasper.web.correction_crossover_v2``. The answer carries the references; the
session's resolver turns them into a calibration record.
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

    Admission stays conductor-owned (SPEC W2.3): the host's injected
    ``authorize_begin`` raises this to refuse — most importantly when
    ``repeat_admission`` refuses the attempt budget. ``code`` is the stable
    machine reason; ``user_message`` is the operator-facing copy
    (refusal-naming pattern, #1534). Both ride the provider's refusal channel
    so the operator can see why nothing started."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


class CaptureBeginDeferred(RuntimeError):
    """A NON-terminal soft-hold on a ``begin_capture``.

    Distinct from :class:`CaptureBeginRefused` (terminal — ends the whole
    plan): the host's injected ``authorize_begin`` raises this when the Pi
    is not YET ready to admit this capture but the plan should stay alive —
    e.g. a heterogeneous plan (crossover-measurement-productization-design.md
    §5.7) parked between MEASURE and VERIFY awaiting the household's Apply
    tap. The provider holds the capture in the SAME ``awaiting_begin`` phase,
    so the identical ``begin_capture {index, attempt}`` may be retried — the
    attempt budget is not spent and the session does not end. ``code`` is the
    stable machine reason; ``user_message`` is the operator-facing copy
    (mirrors ``CaptureBeginRefused``). Because a provider retries throughout a
    hold, the runner dedupes on the (index, code) transition: one INFO log per
    state change, DEBUG for identical repeats."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


class CaptureFailed(RuntimeError):
    """A capture could not be produced or trusted (see ``__cause__``)."""


class CaptureStopped(RuntimeError):
    """The host explicitly stopped this capture."""


#: The per-take integrity counters a provider's answer carries, in the exact
#: wire spelling ``reconcile_capture_frames`` reads. DERIVED from the owner
#: (:mod:`jasper.audio_measurement.frame_ledger` names each key and what it
#: counts), never respelled: this tuple exists so a provider building a report
#: can iterate the contract instead of quoting strings.
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
    setup: Mapping[str, Any] | None

    #: The source's own per-take account of the recording, carrying the
    #: :data:`INTEGRITY_COUNTER_KEYS` counters that
    #: ``jasper.audio_measurement.frame_ledger.reconcile_capture_frames``
    #: reconciles against the frames the host actually decoded: the wired
    #: source counts its own capture chain into these keys.
    capture_integrity: Mapping[str, Any] | None
