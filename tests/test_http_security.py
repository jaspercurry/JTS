# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from email.message import Message

import pytest

from jasper import http_security
from jasper.usb_network import derive_plan


USB_PLAN = derive_plan("10000000abcdef01")


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


def test_allowed_management_host_accepts_usb_gadget_network_address():
    """The derived USB management address and legacy migration address are
    inside the RFC 1918 /8 the private-IP allowlist already covers, pinned so a
    future tightening of the RFC1918 ranges can't silently lock out the
    USB fallback path. Both bare and Host-header-with-port forms must
    pass, and a link-local (no-DHCP-yet) address must too, contrasted
    against a public IP that must still be rejected."""
    assert http_security.is_allowed_management_host(USB_PLAN.device_address)
    assert http_security.is_allowed_management_host(
        f"{USB_PLAN.device_address}:8780"
    )
    assert http_security.is_allowed_management_host("10.12.194.1")
    # 169.254.x.x: IPv4 link-local, reachable before/without the usbnet
    # DHCP lease landing (or if dnsmasq is down) — must stay allowed.
    assert http_security.is_allowed_management_host("169.254.12.34")
    # Contrast case: a public IP in the neighboring /8 must still reject.
    assert not http_security.is_allowed_management_host("11.12.194.1")


def test_management_read_rejects_lowercase_bad_host_header():
    ok, reason = http_security.management_read_allowed({"host": "evil.example"})
    assert (ok, reason) == (False, "host_not_allowed")


def test_management_read_rejects_cross_site_fetch_metadata():
    ok, reason = http_security.management_read_allowed({
        "Host": "192.168.1.23:8780",
        "Sec-Fetch-Site": "cross-site",
    })
    assert (ok, reason) == (False, "cross_site_request")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(
            _headers(Host="192.168.1.23:8780"), (True, "ok"),
            id="allows_missing_origin_for_non_browser_clients",
        ),
        pytest.param(
            _headers(Host="jts.local:8780", Origin="http://jts.local"), (True, "ok"),
            id="allows_same_host_browser_origin",
        ),
        pytest.param(
            _headers(Host="127.0.0.1:8780", Origin="http://localhost:8780"), (True, "ok"),
            id="allows_loopback_name_ip_pair",
        ),
        pytest.param(
            _headers(Host="192.168.1.23:8780", Origin="https://evil.example"),
            (False, "origin_not_allowed"),
            id="rejects_cross_site_origin",
        ),
        pytest.param(
            _headers(Host="jts.local:8780", Origin="null"),
            (False, "origin_not_allowed"),
            id="rejects_null_origin",
        ),
        pytest.param(
            _headers(Host="evil.example:8780", Origin="http://evil.example:8780"),
            (False, "host_not_allowed"),
            id="rejects_dns_rebinding_host_even_if_origin_matches",
        ),
        # A plain dict with lowercase keys, not a Message — the allowlist
        # must case-fold headers itself, not rely on Message's lookup.
        pytest.param(
            {"host": "evil.example:8780", "origin": "http://evil.example:8780"},
            (False, "host_not_allowed"),
            id="rejects_lowercase_bad_host_header",
        ),
        pytest.param(
            {"Host": "192.168.1.23:8780", "Sec-Fetch-Site": "cross-site"},
            (False, "cross_site_request"),
            id="rejects_cross_site_fetch_metadata_without_origin",
        ),
        pytest.param(
            _headers(Host="192.168.1.23:8780", Origin="http://jts.local"),
            (False, "origin_host_mismatch"),
            id="rejects_origin_host_mismatch_between_local_aliases",
        ),
    ],
)
def test_mutating_request(headers, expected):
    assert http_security.mutating_request_allowed(headers) == expected


def test_management_read_accepts_usb_gadget_and_link_local_host_headers():
    """Guard-layer contrast case for the USB management network fallback
    URL: a plan-derived gadget address, its with-port
    Host form, and a pre-DHCP link-local address must all pass
    management_read_allowed, while a public IP one /8 over must still
    be rejected as host_not_allowed."""
    for host in (
        USB_PLAN.device_address,
        f"{USB_PLAN.device_address}:8780",
        "169.254.12.34",
    ):
        ok, reason = http_security.management_read_allowed({"Host": host})
        assert (ok, reason) == (True, "ok"), host
    ok, reason = http_security.management_read_allowed({"Host": "11.12.194.1"})
    assert (ok, reason) == (False, "host_not_allowed")


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
