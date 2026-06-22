# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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


def test_management_read_rejects_unspecified_address_host():
    """0.0.0.0 is a bind address, never a legitimate browser Host. The
    fix for the 2026-06-11 /system/ 403 lives in the control *client*
    (jasper.control.client._connect_host maps unspecified → loopback
    before connecting), NOT here: the guard keeps rejecting so a
    poisoned client surfaces as a loud 403 instead of silently passing."""
    ok, reason = http_security.management_read_allowed({"Host": "0.0.0.0:8780"})
    assert (ok, reason) == (False, "host_not_allowed")


def test_avahi_suffix_rename_of_local_hostname_is_allowed(monkeypatch):
    """RFC 6762 collision rename: when another device claims our
    hostname, Avahi silently renames us to <name>-2.local — the only
    name the speaker is still reachable as. Rejecting it would lock
    the household out of the management UI with no self-heal."""
    monkeypatch.setattr(http_security.socket, "gethostname", lambda: "jts")
    for host in ("jts-2.local", "jts-2", "jts-3.local", "jts-12.local:8780"):
        ok, reason = http_security.management_read_allowed({"Host": host})
        assert (ok, reason) == (True, "ok"), host


def test_avahi_suffix_only_matches_our_own_hostname(monkeypatch):
    """The suffix family is scoped to THIS machine's hostname with a
    purely numeric suffix — a foreign base or non-numeric tail stays a
    rebinding-shaped reject."""
    monkeypatch.setattr(http_security.socket, "gethostname", lambda: "jts")
    for host in (
        "other-2.local",        # someone else's name family
        "jts-evil.local",       # non-numeric suffix
        "jts-2.evil.example",   # public-DNS shape
        "jts-.local",           # empty suffix
        "jts2.local",           # sibling speaker, no hyphen — not ours
    ):
        ok, reason = http_security.management_read_allowed({"Host": host})
        assert (ok, reason) == (False, "host_not_allowed"), host


def test_identity_file_names_extend_the_allowlist(monkeypatch, tmp_path):
    """Names the identity reconciler observed (identity.env) are
    accepted — covers shapes the static rules can't derive, e.g. a
    stale-but-still-advertised configured name after an operator
    rename."""
    identity = tmp_path / "identity.env"
    identity.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=kitchen\n"
        "JASPER_IDENTITY_AVAHI_HOSTNAME=kitchen-2.local\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts-kitchen.local\n"
    )
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(identity))
    monkeypatch.setattr(http_security.socket, "gethostname", lambda: "unrelated")
    for host in ("kitchen.local", "kitchen-2.local", "jts-kitchen.local"):
        ok, reason = http_security.management_read_allowed({"Host": host})
        assert (ok, reason) == (True, "ok"), host
    # Still not a free-for-all.
    ok, reason = http_security.management_read_allowed({"Host": "evil.example"})
    assert (ok, reason) == (False, "host_not_allowed")


def test_missing_identity_file_changes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "absent.env"))
    ok, reason = http_security.management_read_allowed({"Host": "evil.example"})
    assert (ok, reason) == (False, "host_not_allowed")
