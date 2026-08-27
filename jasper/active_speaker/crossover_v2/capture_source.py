# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-source seam: what any capture provider owes the conductor.

Decision 13 (#2662, ruled 2026-08-17): the commissioning flow supports more
than one capture source, each first-class. The conductor asks for *a capture of
program X at position Y*, and a provider answers with WAV plus metadata. A
source's own choreography — for the phone relay: sessions, encrypted upload,
deferred begins, host events, purge — is that provider's private internals,
never the conductor's or the web host's.

This module is the seam's VOCABULARY, deliberately logic-free (the same
register as :mod:`.refusal_copy` and :mod:`.contracts`): the provider identities
and the answer contract. The two halves of the conversation live where they
already are:

* **The ask.** The plan walk hands a provider three conductor-owned hooks —
  ``authorize_begin(index, attempt, entry)`` (admission, and the position
  gate ahead of it), ``on_armed(state)`` (the host plays program X), and
  ``consume_capture(index, attempt, answer)`` (the answer lands) — plus the
  stop/completion predicates. The arity asymmetry is real and not an
  oversight: ``authorize_begin`` reads the entry because it gates on the
  prompted spot, ``consume_capture`` never read its own, so that parameter
  went. ``run_capture_plan`` appends the entry only to a hook that declares
  room for one, so both arities are served by one walk. The shipped carrier
  of that hook set is ``jasper.capture_relay.session.run_capture_plan``,
  driven by the relay provider (``jasper.web.correction_crossover_v2_relay``);
  a wired provider implements the same conversation against local capture
  instead of a phone.
* **The answer.** :class:`CaptureAnswer` below. The relay's
  ``jasper.capture_relay.session.CaptureResult`` already satisfies it — the
  relay provider passes through what the phone sent — and a wired provider
  mints its own conformant answer (pinned by
  ``tests/test_crossover_v2_capture_source.py``).

Two ownership rules the seam holds, so the next provider cannot move them by
accident:

* **The provider mints the session identity.** The durable v2 state, the
  evidence publishers, and the phase artifacts are all keyed by the session id
  the provider's open returned (today: the relay session id). A second source
  supplies its own id on the same key; the persisted key vocabulary does not
  change per source. One scoping clause: for ATTRIBUTION the **bundle**
  session id stays canonical — the bundle is the retention unit — and the
  provider's id rides as an alias
  (``jasper.web.correction_crossover_v2.v2_session_identity``, alias key
  ``ALIAS_RELAY_SESSION_ID``). A new provider aliases its own id the same
  way; it does not mint a new canonical identity.
* **The host owns the persisted-code mapping.** What crosses the seam when a
  session dies is the flow's own reason vocabulary
  (``REASON_RELAY_TIMEOUT``, ``REASON_USER_STOPPED``, … —
  :mod:`.refusal_copy` and the flow's ``REASON_REGISTRY``), never a transport's
  exception types: only the relay provider knows ``CaptureTimeout`` from
  ``RelayError``, and it translates them before the host persists. The
  persisted codes themselves are frozen — hydration and incident replay
  depend on them.

Mic and calibration identity resolve PER SOURCE, at an injection point that
already exists: ``bind_production_analyze(resolve_calibration=...)`` in
``jasper.web.correction_crossover_v2``. The relay's resolver reads the
phone-reported ``setup``/``device`` pair; a wired source injects a resolver
for its own microphone. The answer carries the references; the session's
resolver turns them into a calibration record.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
)

#: The shipped provider: the phone-relay capture flow. A session's source is a
#: session-level fact — one provider hosts the whole walk — so the identity
#: lives here as a provider constant, not as a per-answer field.
SOURCE_RELAY = "relay"

#: The Pi-attached measurement-mic provider (#2662 W2b,
#: :mod:`jasper.web.correction_crossover_v2_wired`): the Pi plays AND records
#: on one host, so the relay's cross-device choreography (sessions, encrypted
#: upload, deferred begins) does not exist for it at all. Selected per session
#: by ``resolve_v2_capture_source`` in that module — presence of a
#: measurement-class capture card, with a ``JASPER_CAPTURE_SOURCE`` override.
SOURCE_WIRED = "wired"

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

    Attribute contract, structural on purpose — the relay's ``CaptureResult``
    satisfies it as it stands, and a wired answer is any object carrying the
    same four facts. Every field but ``wav`` is ``None`` when the source
    cannot state it; ``None`` means *not reported*, never a clean default.
    """

    #: The captured audio, complete WAV bytes (the analyzer decodes format and
    #: rate from the container itself).
    wav: bytes

    #: The microphone identity as the source observed it (for the relay: the
    #: phone-reported ``{"label", "device_id", ...}``). The session's
    #: calibration resolver checks it against the calibration choice so a
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
    #: reconciles against the frames the host actually decoded. The relay
    #: passes the phone's report through unparsed; a wired source counts its
    #: own capture chain into the same keys.
    capture_integrity: Mapping[str, Any] | None
