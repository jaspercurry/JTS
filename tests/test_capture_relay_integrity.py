# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language-stable relay control integrity primitives."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from jasper.capture_relay.integrity import (
    CaptureIntegrityError,
    authenticated_phone_event,
    capture_spec_mac,
    verify_authenticated_phone_event,
    verify_capture_spec_mac,
)
from jasper.capture_relay.session import PhoneEventVerifier

KEY = bytes(range(32))
SESSION = "cap_integrity_test"
SPEC = '{"kind":"crossover_sweep","capture_protocol_version":3}'


def test_spec_mac_matches_public_page_vector_and_rejects_tamper():
    # Keep this vector byte-identical to `tests/js/capture_transport_integrity_test.mjs`
    # — it is the cross-language pin, so both sides move together or not at all.
    tag = capture_spec_mac(KEY, SESSION, SPEC)
    assert tag == "SnRu5CNZlynsM-Wjk4sfI_6Q2dAVrJdwMHOwZR1Ybqo"
    verify_capture_spec_mac(KEY, SESSION, SPEC, tag)
    with pytest.raises(CaptureIntegrityError):
        verify_capture_spec_mac(KEY, SESSION, SPEC + " ", tag)


def test_authenticated_event_binds_exact_payload_sequence_and_session():
    envelope = authenticated_phone_event(
        KEY,
        SESSION,
        {"armed": True, "capture_page": {"capture_protocol_version": 3}},
        sequence=1,
    )
    payload, sequence = verify_authenticated_phone_event(KEY, SESSION, envelope)
    assert payload["armed"] is True
    assert sequence == 1

    tampered = {"authenticated_event": dict(envelope["authenticated_event"])}
    tampered["authenticated_event"]["payload"] = tampered[
        "authenticated_event"
    ]["payload"].replace("true", "false")
    with pytest.raises(CaptureIntegrityError):
        verify_authenticated_phone_event(KEY, SESSION, tampered)
    with pytest.raises(CaptureIntegrityError):
        verify_authenticated_phone_event(KEY, "cap_other_session", envelope)


def test_phone_event_verifier_ignores_authenticated_stale_sequence_only(caplog):
    """A delayed relay slot is stale only after its MAC/session binding passes.

    The accepted sequence stays monotonic, an identical current-slot replay is
    idempotent, and every conflicting/auth-invalid shape remains fail closed.
    """
    session = SimpleNamespace(
        content_key=KEY,
        session_id=SESSION,
        spec=SimpleNamespace(kind="crossover_v2:verify"),
    )
    verifier = PhoneEventVerifier(session)
    current = {"armed": True, "begin_capture": {"index": 1, "attempt": 1}}
    accepted = authenticated_phone_event(KEY, SESSION, current, sequence=2)

    with caplog.at_level(logging.INFO):
        assert verifier.verify(accepted) == current
        assert verifier.verify(accepted) == current
        assert verifier.verify(
            authenticated_phone_event(
                KEY, SESSION, {"begin_capture": {"index": 1, "attempt": 1}},
                sequence=1,
            )
        ) is None
        # A persistent stale relay slot must not emit once per poll.
        assert verifier.verify(
            authenticated_phone_event(
                KEY, SESSION, {"begin_capture": {"index": 1, "attempt": 1}},
                sequence=1,
            )
        ) is None

    assert verifier.sequence == 2
    assert sum(
        "event=capture_relay.capture_event_sequence_accepted" in record.message
        for record in caplog.records
    ) == 1
    stale = [
        record.message for record in caplog.records
        if "event=capture_relay.capture_event_stale_ignored" in record.message
    ]
    assert len(stale) == 1
    assert "accepted_sequence=2" in stale[0]
    assert "stale_sequence=1" in stale[0]

    with pytest.raises(CaptureIntegrityError):
        verifier.verify(authenticated_phone_event(
            KEY, SESSION, {"armed": False}, sequence=2,
        ))
    with pytest.raises(CaptureIntegrityError):
        verifier.verify(authenticated_phone_event(
            KEY, "cap_other_session", {"armed": False}, sequence=3,
        ))
