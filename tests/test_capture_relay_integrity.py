# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language-stable relay control integrity primitives."""

from __future__ import annotations

import pytest

from jasper.capture_relay.integrity import (
    CaptureIntegrityError,
    authenticated_phone_event,
    capture_spec_mac,
    verify_authenticated_phone_event,
    verify_capture_spec_mac,
)

KEY = bytes(range(32))
SESSION = "cap_integrity_test"
SPEC = '{"kind":"crossover_sweep","capture_protocol_version":2}'


def test_spec_mac_matches_public_page_vector_and_rejects_tamper():
    tag = capture_spec_mac(KEY, SESSION, SPEC)
    assert tag == "bGlwzjxko5SkN3PLp8ZP6vdPuj2SXGQYMWaLZ3yGFe0"
    verify_capture_spec_mac(KEY, SESSION, SPEC, tag)
    with pytest.raises(CaptureIntegrityError, match="integrity check failed"):
        verify_capture_spec_mac(KEY, SESSION, SPEC + " ", tag)


def test_authenticated_event_binds_exact_payload_sequence_and_session():
    envelope = authenticated_phone_event(
        KEY,
        SESSION,
        {"armed": True, "capture_page": {"capture_protocol_version": 2}},
        sequence=1,
    )
    payload, sequence = verify_authenticated_phone_event(KEY, SESSION, envelope)
    assert payload["armed"] is True
    assert sequence == 1

    tampered = {"authenticated_event": dict(envelope["authenticated_event"])}
    tampered["authenticated_event"]["payload"] = tampered[
        "authenticated_event"
    ]["payload"].replace("true", "false")
    with pytest.raises(CaptureIntegrityError, match="integrity check failed"):
        verify_authenticated_phone_event(KEY, SESSION, tampered)
    with pytest.raises(CaptureIntegrityError, match="integrity check failed"):
        verify_authenticated_phone_event(KEY, "cap_other_session", envelope)
