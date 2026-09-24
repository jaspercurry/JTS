# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The stable cross-store session identity (``docs/historical/attribution-stage-plan.md`` §6)."""

from __future__ import annotations

import pytest

from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    SESSION_IDENTITY_KEY,
    SESSION_IDENTITY_SCHEME,
    SessionIdentity,
    SessionIdentityError,
    read_session_identity,
    stamp_session_identity,
)

_SESSION = SessionIdentity(
    session_id="7f54494228cc",
    aliases={ALIAS_CAPTURE_SESSION_ID: "cap_Ktm3xQ2p"},
)


def test_the_identity_round_trips_and_survives_a_flat_carrier() -> None:
    assert SessionIdentity.from_mapping(_SESSION.to_dict()) == _SESSION
    assert _SESSION.token == "jts-session-1:7f54494228cc"
    # A token carries the identity, not its decoration.
    assert SessionIdentity.from_token(_SESSION.token).session_id == _SESSION.session_id


def test_one_key_name_carries_the_identity_through_every_store() -> None:
    """§6's requirement is "one identifier that survives every hop". The
    mechanism is one canonical key: a reader that finds
    ``jts_session_identity`` in a bundle artifact, a capture-ring sidecar, or
    a laptop archive manifest resolves the session without knowing which
    store it came from."""

    hops = [
        {"kind": "jts_crossover_v2_cloud_evidence", "phase": "cloud_measure"},
        {"phase": "measure", "wav_sha256": "b" * 64},
    ]
    for payload in hops:
        stamp_session_identity(payload, _SESSION)
        assert SESSION_IDENTITY_KEY in payload
        assert read_session_identity(payload) == _SESSION


def test_an_identity_without_a_session_id_is_refused_by_name() -> None:
    """``from_mapping`` refused unknown fields but never REQUIRED the one
    field that matters, so a mapping missing it fell through to the charset
    check and reported a malformed identifier rather than a missing one."""

    for raw in ({}, {"scheme": SESSION_IDENTITY_SCHEME}, {"session_id": None},
                {"session_id": 12}):
        with pytest.raises(SessionIdentityError):
            SessionIdentity.from_mapping(raw)


def test_an_unstamped_payload_reads_as_legacy_not_as_an_error() -> None:
    """Every artifact written before this module existed carries no identity
    key — and that corpus is exactly what WO-0 had to read. Absence is
    history; malformation is a writer bug."""

    assert read_session_identity({"phase": "measure"}) is None
    assert read_session_identity(None) is None
    with pytest.raises(SessionIdentityError):
        read_session_identity({SESSION_IDENTITY_KEY: {"session_id": "x", "junk": 1}})


def test_a_token_cannot_be_confused_with_a_content_hash() -> None:
    """§6: "Content hashing stays the *verifier*; it must stop being the
    *index*." The scheme prefix is what keeps an identity self-identifying
    wherever it appears, so a bare hex digest can never be mistaken for one."""

    with pytest.raises(SessionIdentityError):
        SessionIdentity.from_token("a" * 64)
    with pytest.raises(SessionIdentityError):
        SessionIdentity(session_id="7f54494228cc", scheme="sha256")
