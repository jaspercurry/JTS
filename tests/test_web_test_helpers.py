# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the shared socketless web-wizard handler."""

from email.message import Message
from io import BytesIO

from ._web_test_helpers import FakeHandler


def test_fake_handler_preserves_request_and_response_surface():
    handler = FakeHandler("/save", body=b"answer=42", cookies="jts_csrf=token")

    assert handler.path == "/save"
    assert isinstance(handler.headers, Message)
    assert handler.headers.get_all("Content-Length") == ["9"]
    assert handler.headers.get_all("Content-Type") == [
        "application/x-www-form-urlencoded"
    ]
    assert handler.headers.get_all("Cookie") == ["jts_csrf=token"]
    assert isinstance(handler.rfile, BytesIO)
    assert handler.rfile.read() == b"answer=42"
    assert isinstance(handler.wfile, BytesIO)
    assert handler.wfile.getvalue() == b""
    assert handler.status is None
    assert handler.client_address == ("127.0.0.1", 0)
    assert handler.address_string() == "127.0.0.1"

    handler.send_response(201)
    handler.send_response_only(202)
    handler.send_header("Set-Cookie", "first=1")
    handler.send_header("X-Test", "middle")
    handler.send_header("set-cookie", "second=2")
    handler.end_headers()
    handler.log_message("ignored %s", "message")

    assert handler.status == 202
    assert handler.sent_headers == [
        ("Set-Cookie", "first=1"),
        ("X-Test", "middle"),
        ("set-cookie", "second=2"),
    ]
    assert handler.header_values("SET-cookie") == ["first=1", "second=2"]

    handler.send_error(403, "forbidden")
    assert handler.status == 403


def test_fake_handler_body_none_omits_form_headers():
    handler = FakeHandler("/rooms.json", body=None)

    assert handler.headers.get("Content-Length") is None
    assert handler.headers.get("Content-Type") is None
    assert handler.rfile.read() == b""
