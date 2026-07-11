# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integrity for opaque relay control data.

The relay can see and store a capture spec and phone control events, but it
does not know the capture content key carried in the phone-link fragment.  We
derive a separate HMAC key from that content key and authenticate the exact spec
bytes plus every protocol-v2 phone event before either endpoint interprets
them.  The relay remains a byte transport; no relay-side parsing or secret is
added.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

INTEGRITY_SCHEMA_VERSION = 1
AUTHENTICATED_EVENT_KEY = "authenticated_event"
_KEY_DERIVATION_LABEL = b"jts-capture-transport-integrity-key-v1"
_MESSAGE_DOMAIN = b"jts-capture-transport-integrity-v1\x00"
_SPEC_KIND = "capture-spec"
_EVENT_KIND = "phone-event"
_MAX_AUTHENTICATED_EVENT_BYTES = 1024 * 1024


class CaptureIntegrityError(ValueError):
    """Relay-carried data failed its end-to-end integrity contract."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CaptureIntegrityError("capture integrity MAC is missing")
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise CaptureIntegrityError("capture integrity MAC is malformed") from exc


def derive_integrity_key(content_key: bytes) -> bytes:
    """Derive an HMAC-only key; never reuse the AES-GCM key directly."""

    if not isinstance(content_key, bytes) or len(content_key) != 32:
        raise CaptureIntegrityError("content key must be exactly 32 bytes")
    return hmac.new(content_key, _KEY_DERIVATION_LABEL, hashlib.sha256).digest()


def _framed_message(kind: str, session_id: str, payload: bytes) -> bytes:
    kind_raw = str(kind).encode("utf-8")
    session_raw = str(session_id).encode("utf-8")
    if not kind_raw or not session_raw:
        raise CaptureIntegrityError("capture integrity message is missing identity")
    return b"".join((
        _MESSAGE_DOMAIN,
        len(kind_raw).to_bytes(2, "big"),
        kind_raw,
        len(session_raw).to_bytes(2, "big"),
        session_raw,
        len(payload).to_bytes(8, "big"),
        payload,
    ))


def _mac(content_key: bytes, kind: str, session_id: str, payload: bytes) -> str:
    key = derive_integrity_key(content_key)
    return _b64url(
        hmac.new(
            key,
            _framed_message(kind, session_id, payload),
            hashlib.sha256,
        ).digest()
    )


def capture_spec_mac(
    content_key: bytes,
    session_id: str,
    capture_spec_json: str,
) -> str:
    """MAC the exact opaque spec string registered with the relay."""

    if not isinstance(capture_spec_json, str) or not capture_spec_json:
        raise CaptureIntegrityError("capture spec must be a non-empty string")
    return _mac(
        content_key,
        _SPEC_KIND,
        session_id,
        capture_spec_json.encode("utf-8"),
    )


def verify_capture_spec_mac(
    content_key: bytes,
    session_id: str,
    capture_spec_json: str,
    observed_mac: str,
) -> None:
    expected = capture_spec_mac(content_key, session_id, capture_spec_json)
    try:
        observed = _b64url_decode(observed_mac)
        expected_raw = _b64url_decode(expected)
    except CaptureIntegrityError:
        raise
    if not hmac.compare_digest(observed, expected_raw):
        raise CaptureIntegrityError("capture spec integrity check failed")


def authenticated_phone_event(
    content_key: bytes,
    session_id: str,
    event: Mapping[str, Any],
    *,
    sequence: int,
) -> dict[str, Any]:
    """Build the relay-opaque signed envelope used by protocol-v2 phones."""

    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise CaptureIntegrityError("phone event sequence must be a positive integer")
    payload_json = json.dumps(
        dict(event),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload = payload_json.encode("utf-8")
    if len(payload) > _MAX_AUTHENTICATED_EVENT_BYTES:
        raise CaptureIntegrityError("authenticated phone event is too large")
    return {
        AUTHENTICATED_EVENT_KEY: {
            "schema_version": INTEGRITY_SCHEMA_VERSION,
            "sequence": sequence,
            "payload": payload_json,
            "mac": _mac(
                content_key,
                f"{_EVENT_KIND}:{sequence}",
                session_id,
                payload,
            ),
        }
    }


def verify_authenticated_phone_event(
    content_key: bytes,
    session_id: str,
    relay_event: Any,
) -> tuple[dict[str, Any], int]:
    """Verify and parse a phone event without trusting relay-visible fields."""

    if not isinstance(relay_event, Mapping):
        raise CaptureIntegrityError("authenticated phone event is missing")
    envelope = relay_event.get(AUTHENTICATED_EVENT_KEY)
    if not isinstance(envelope, Mapping):
        raise CaptureIntegrityError("authenticated phone event is missing")
    if set(envelope) != {"schema_version", "sequence", "payload", "mac"}:
        raise CaptureIntegrityError("authenticated phone event shape is invalid")
    sequence = envelope.get("sequence")
    payload_json = envelope.get("payload")
    observed_mac = envelope.get("mac")
    if (
        envelope.get("schema_version") != INTEGRITY_SCHEMA_VERSION
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(payload_json, str)
        or not isinstance(observed_mac, str)
    ):
        raise CaptureIntegrityError("authenticated phone event shape is invalid")
    payload = payload_json.encode("utf-8")
    if len(payload) > _MAX_AUTHENTICATED_EVENT_BYTES:
        raise CaptureIntegrityError("authenticated phone event is too large")
    expected = _mac(
        content_key,
        f"{_EVENT_KIND}:{sequence}",
        session_id,
        payload,
    )
    if not hmac.compare_digest(_b64url_decode(observed_mac), _b64url_decode(expected)):
        raise CaptureIntegrityError("authenticated phone event integrity check failed")
    try:
        decoded = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise CaptureIntegrityError("authenticated phone event payload is invalid") from exc
    if not isinstance(decoded, dict):
        raise CaptureIntegrityError("authenticated phone event payload must be an object")
    return decoded, sequence

