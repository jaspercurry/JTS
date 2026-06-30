# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side relay HTTP client (phone-mic relay step 4).

Outbound HTTPS only — the Pi never accepts inbound connections, so this works
behind home NAT (the Pi already has internet for voice providers). The client
speaks the relay contract from `relay/src/worker.js` §7 with the **pull_token**
(the Pi's half of the privilege split; the upload_token stays on the phone) plus
the open registration call. The HTTP transport is injectable so the whole client
is testable without a network or a live Worker.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_USER_AGENT = "JTS capture-relay/1"


@dataclass(frozen=True)
class RelayResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


# (method, url, headers, body) -> RelayResponse
Transport = Callable[[str, str, Mapping[str, str], "bytes | None"], RelayResponse]


class RelayError(RuntimeError):
    """A relay request returned a non-2xx status."""

    def __init__(self, message: str, status: int, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> RelayResponse:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, **dict(headers)}
    req = urllib.request.Request(
        url, data=body, method=method, headers=request_headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return RelayResponse(
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as exc:
        # Surface the status uniformly rather than raising — the client methods
        # classify it.
        return RelayResponse(
            exc.code,
            {k.lower(): v for k, v in (exc.headers or {}).items()},
            exc.read() or b"",
        )


class RelayClient:
    """Talks to the relay for one Pi. `base_url` is the relay origin."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # Outbound-HTTPS-only: enforce the https scheme that _urllib_transport's
        # S310 audit-suppression assumes, so an operator misconfiguration can't
        # send tokens over http:// or follow a file:// base. Skipped when a custom
        # transport is injected (tests use https://relay.test through a fake).
        if transport is None and not base_url.startswith("https://"):
            raise ValueError(f"relay base_url must be https://, got {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport: Transport = transport or (
            lambda m, u, h, b: _urllib_transport(m, u, h, b, timeout=timeout)
        )

    def _session_url(self, session_id: str, suffix: str = "") -> str:
        return f"{self.base_url}/sessions/{session_id}{suffix}"

    def _json(self, resp: RelayResponse) -> dict[str, Any]:
        if resp.body:
            return json.loads(resp.body.decode("utf-8"))
        return {}

    def _require_ok(self, resp: RelayResponse, what: str) -> None:
        if not (200 <= resp.status < 300):
            detail = ""
            try:
                # Expected failures: a non-JSON body (ValueError) or a JSON value
                # that isn't an object (AttributeError on .get).
                detail = (self._json(resp) or {}).get("error", "")
            except (ValueError, AttributeError, UnicodeDecodeError):
                detail = resp.body[:200].decode("utf-8", "replace")
            raise RelayError(f"{what} failed: {resp.status} {detail}", resp.status, resp.body)

    # -- registration (open; the Pi mints its own tokens) --

    def register(
        self,
        *,
        session_id: str,
        capture_spec_json: str,
        upload_token: str,
        pull_token: str,
        ttl_s: int,
        max_upload_bytes: int,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "session_id": session_id,
                "capture_spec": capture_spec_json,  # opaque string to the relay
                "upload_token": upload_token,
                "pull_token": pull_token,
                "ttl_s": ttl_s,
                "max_upload_bytes": max_upload_bytes,
            }
        ).encode("utf-8")
        resp = self._transport(
            "POST",
            f"{self.base_url}/sessions",
            {"Content-Type": "application/json"},
            body,
        )
        self._require_ok(resp, "register")
        return self._json(resp)

    # -- pull side (pull_token) --

    def status(self, session_id: str, pull_token: str) -> dict[str, Any]:
        resp = self._transport(
            "GET",
            self._session_url(session_id, "/status"),
            {"Authorization": f"Bearer {pull_token}"},
            None,
        )
        self._require_ok(resp, "status")
        return self._json(resp)

    def pull_blob(
        self, session_id: str, pull_token: str
    ) -> tuple[bytes, dict[str, Any]]:
        """Return (ciphertext blob, integrity) where integrity is the phone's
        plaintext length + SHA-256 (relayed via headers)."""
        resp = self._transport(
            "GET",
            self._session_url(session_id, "/blob"),
            {"Authorization": f"Bearer {pull_token}"},
            None,
        )
        self._require_ok(resp, "pull_blob")
        plen = resp.headers.get("x-plaintext-length", "")
        integrity = {
            "plaintext_len": int(plen) if plen.isdigit() else None,
            "sha256": resp.headers.get("x-plaintext-sha256", ""),
        }
        return resp.body, integrity

    def delete(self, session_id: str, pull_token: str) -> None:
        resp = self._transport(
            "DELETE",
            self._session_url(session_id),
            {"Authorization": f"Bearer {pull_token}"},
            None,
        )
        # 204 expected; a 404 (already gone / TTL-expired) is fine for a purge.
        if resp.status not in (204, 404):
            self._require_ok(resp, "delete")
