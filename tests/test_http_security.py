from __future__ import annotations

from email.message import Message

from jasper import http_security


def _headers(**values: str) -> Message:
    msg = Message()
    for key, value in values.items():
        msg[key.replace("_", "-")] = value
    return msg


def test_normalize_host_strips_ports_and_brackets():
    assert http_security.normalize_host("jts.local:8780") == "jts.local"
    assert http_security.normalize_host("[::1]:8780") == "::1"
    assert http_security.normalize_host("speaker.local.") == "speaker.local"


def test_allowed_management_host_accepts_private_ip_and_configured_name(monkeypatch):
    monkeypatch.setenv("JASPER_HOSTNAME", "speaker.local")
    assert http_security.is_allowed_management_host("192.168.1.23:8780")
    assert http_security.is_allowed_management_host("10.1.2.3")
    assert http_security.is_allowed_management_host("localhost:8780")
    assert http_security.is_allowed_management_host("speaker.local:8780")
    assert http_security.is_allowed_management_host("speaker:8780")


def test_allowed_management_host_accepts_explicit_alias(monkeypatch):
    monkeypatch.setenv("JASPER_MANAGEMENT_ALLOWED_HOSTS", "musicbox.lan")
    assert http_security.is_allowed_management_host("musicbox.lan:8780")


def test_allowed_management_host_rejects_public_hostname():
    assert not http_security.is_allowed_management_host("evil.example:8780")


def test_management_read_rejects_lowercase_bad_host_header():
    ok, reason = http_security.management_read_allowed({"host": "evil.example"})
    assert (ok, reason) == (False, "host_not_allowed")


def test_management_read_rejects_cross_site_fetch_metadata():
    ok, reason = http_security.management_read_allowed({
        "Host": "192.168.1.23:8780",
        "Sec-Fetch-Site": "cross-site",
    })
    assert (ok, reason) == (False, "cross_site_request")


def test_mutating_request_allows_missing_origin_for_non_browser_clients():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="192.168.1.23:8780"),
    )
    assert (ok, reason) == (True, "ok")


def test_mutating_request_allows_same_host_browser_origin():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="jts.local:8780", Origin="http://jts.local"),
    )
    assert (ok, reason) == (True, "ok")


def test_mutating_request_allows_loopback_name_ip_pair():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="127.0.0.1:8780", Origin="http://localhost:8780"),
    )
    assert (ok, reason) == (True, "ok")


def test_mutating_request_rejects_cross_site_origin():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="192.168.1.23:8780", Origin="https://evil.example"),
    )
    assert (ok, reason) == (False, "origin_not_allowed")


def test_mutating_request_rejects_null_origin():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="jts.local:8780", Origin="null"),
    )
    assert (ok, reason) == (False, "origin_not_allowed")


def test_mutating_request_rejects_dns_rebinding_host_even_if_origin_matches():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="evil.example:8780", Origin="http://evil.example:8780"),
    )
    assert (ok, reason) == (False, "host_not_allowed")


def test_mutating_request_rejects_lowercase_bad_host_header():
    ok, reason = http_security.mutating_request_allowed({
        "host": "evil.example:8780",
        "origin": "http://evil.example:8780",
    })
    assert (ok, reason) == (False, "host_not_allowed")


def test_mutating_request_rejects_cross_site_fetch_metadata_without_origin():
    ok, reason = http_security.mutating_request_allowed({
        "Host": "192.168.1.23:8780",
        "Sec-Fetch-Site": "cross-site",
    })
    assert (ok, reason) == (False, "cross_site_request")


def test_mutating_request_rejects_origin_host_mismatch_between_local_aliases():
    ok, reason = http_security.mutating_request_allowed(
        _headers(Host="192.168.1.23:8780", Origin="http://jts.local"),
    )
    assert (ok, reason) == (False, "origin_host_mismatch")
