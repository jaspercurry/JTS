# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Faithful in-memory relay used by capture-session and adapter tests."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayResponse
from jasper.capture_relay.integrity import authenticated_phone_event

CAPTURE_PAGE = {
    "schema_version": 1,
    "capture_protocol_version": 3,
    "supported_capture_protocol_versions": [3],
    "capture_page_build": "20260727.2",
}


class FakeRelayBackend:
    """In-memory transport mirroring the Worker's Pi-facing endpoints."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.content_keys: dict[str, bytes] = {}

    def bind_phone(self, session: Any) -> None:
        """Hand the fake phone the E2E key it would read from the tap link."""

        self.content_keys[session.session_id] = session.content_key

    def _authenticate(
        self,
        sid: str,
        event: dict[str, Any],
        *,
        auth_session: Any = None,
        content_key: bytes | None = None,
        sequence: int = 1,
    ) -> dict[str, Any]:
        if auth_session is not None:
            content_key = auth_session.content_key
            sid = auth_session.session_id
        key = content_key or self.content_keys.get(sid)
        if key is None:
            raise AssertionError(
                f"no content key bound for {sid} — call backend.bind_phone(session); "
                "every phone event must be authenticated"
            )
        return authenticated_phone_event(key, sid, event, sequence=sequence)

    def __call__(self, method: str, url: str, headers: dict, body: bytes):
        path = urllib.parse.urlsplit(url).path
        parts = [part for part in path.split("/") if part]
        auth = headers.get("Authorization", "") or ""
        token = auth.removeprefix("Bearer ")

        def json_response(status: int, obj: dict[str, Any]) -> RelayResponse:
            return RelayResponse(status, {}, json.dumps(obj).encode())

        if parts == ["sessions"] and method == "POST":
            registration = json.loads(body)
            self.sessions[registration["session_id"]] = {
                "capture_spec": registration["capture_spec"],
                "upload_token": registration.get("upload_token"),
                "pull_token": registration["pull_token"],
                "max_upload_bytes": registration.get("max_upload_bytes"),
                "state": "pending",
                "event": None,
                "host_event": None,
                "integrity": None,
                "blob": None,
            }
            return json_response(
                201,
                {"session_id": registration["session_id"], "state": "pending"},
            )

        if len(parts) >= 2 and parts[0] == "sessions":
            sid = parts[1]
            subpath = parts[2] if len(parts) > 2 else ""
            session = self.sessions.get(sid)
            if session is None:
                return json_response(404, {"error": "not_found"})
            if subpath in ("status", "blob", "") and token != session["pull_token"]:
                return json_response(401, {"error": "unauthorized"})
            if subpath == "status" and method == "GET":
                return json_response(
                    200,
                    {
                        "state": session["state"],
                        "size": len(session["blob"] or b""),
                        "integrity": session["integrity"],
                        "event": session["event"],
                        "host_event": session["host_event"],
                        "expires_at": 0,
                    },
                )
            if subpath == "host-event" and method == "POST":
                if token != session["pull_token"]:
                    return json_response(401, {"error": "unauthorized"})
                session["host_event"] = json.loads(body)
                return json_response(200, {"ok": True})
            if subpath == "blob" and method == "GET":
                if session["state"] != "ready":
                    return json_response(409, {"error": "not_ready"})
                return RelayResponse(
                    200,
                    {
                        "x-plaintext-length": str(
                            session["integrity"]["plaintext_len"]
                        ),
                        "x-plaintext-sha256": session["integrity"]["sha256"],
                    },
                    session["blob"],
                )
            if subpath == "" and method == "DELETE":
                del self.sessions[sid]
                return RelayResponse(204, {}, b"")
        return json_response(404, {"error": "not_found"})

    def phone_arm(
        self,
        sid: str,
        device: Any = None,
        *,
        noise_floor: Any = None,
        setup: Any = None,
        acknowledgement: Any = None,
        capture_page: dict[str, Any] | None = None,
        auth_session: Any = None,
        content_key: bytes | None = None,
        sequence: int = 1,
    ) -> None:
        event = {"armed": True, "capture_page": dict(capture_page or CAPTURE_PAGE)}
        if device is not None:
            event["device"] = device
        if noise_floor is not None:
            event["noise_floor"] = noise_floor
        if setup is not None:
            event["setup"] = setup
        if acknowledgement is not None:
            event["acknowledgement"] = acknowledgement
        self.sessions[sid]["event"] = self._authenticate(
            sid,
            event,
            auth_session=auth_session,
            content_key=content_key,
            sequence=sequence,
        )

    def phone_setup_validate(
        self, sid: str, setup: Any, *, token: str = "setup-token", sequence: int = 1
    ) -> None:
        self.sessions[sid]["event"] = self._authenticate(
            sid,
            {
                "setup_validate": True,
                "setup_token": token,
                "setup": setup,
                "capture_page": dict(CAPTURE_PAGE),
            },
            sequence=sequence,
        )

    def phone_abort(
        self, sid: str, reason: str = "backgrounded", *, sequence: int = 1
    ) -> None:
        self.sessions[sid]["event"] = self._authenticate(
            sid, {"aborted": True, "abort_reason": reason}, sequence=sequence
        )

    def phone_upload(self, sid: str, content_key: bytes, wav: bytes) -> None:
        iv = os.urandom(crypto.IV_BYTES)
        blob = iv + AESGCM(content_key).encrypt(iv, wav, None)
        session = self.sessions[sid]
        session["blob"] = blob
        session["integrity"] = {
            "plaintext_len": len(wav),
            "sha256": hashlib.sha256(wav).hexdigest(),
        }
        session["state"] = "ready"

    def phone_upload_corrupt(
        self, sid: str, content_key: bytes, wav: bytes
    ) -> None:
        self.phone_upload(sid, content_key, wav)
        self.sessions[sid]["integrity"]["sha256"] = "0" * 64
