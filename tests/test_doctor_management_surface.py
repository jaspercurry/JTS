"""Unit tests for jasper-doctor's management-surface probe.

check_management_surface exercises the browser path (loopback nginx with
`Host: <JASPER_HOSTNAME>` → system wizard → jasper-control's
management-host guard). The network is mocked; the on-Pi smoke test is
jasper-doctor itself plus the deploy-time probe in
scripts/deploy-to-pi.sh.
"""
from __future__ import annotations

import io
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from jasper.cli.doctor import web as doctor_web


def _install_nginx_site(monkeypatch, tmp_path):
    site = tmp_path / "jasper.conf"
    site.write_text("# nginx site\n")
    monkeypatch.setattr(doctor_web, "NGINX_SITE", site)


@contextmanager
def _urlopen_returns(status: int, body: bytes):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self, n=-1):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()) as m:
        yield m


def test_skips_when_nginx_site_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_web, "NGINX_SITE", tmp_path / "absent.conf")
    r = doctor_web.check_management_surface()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_ok_on_200(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts3.local")
    with _urlopen_returns(200, b"{}") as m:
        r = doctor_web.check_management_surface()
    assert r.status == "ok"
    assert "jts3.local" in r.detail
    # The probe must carry the speaker hostname as the Host header —
    # that is the whole point of the check.
    req = m.call_args[0][0]
    assert req.get_header("Host") == "jts3.local"


def test_403_fails_with_guard_hint(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_web.MANAGEMENT_PROBE_URL, 403, "Forbidden", None,
        io.BytesIO(b'{"error": "host_not_allowed"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "host_not_allowed" in r.detail
    assert "event=http.reject" in r.detail


def test_502_fails_naming_control(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.HTTPError(
        doctor_web.MANAGEMENT_PROBE_URL, 502, "Bad Gateway", None,
        io.BytesIO(b'{"error": "jasper-control unreachable: ..."}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "jasper-control" in r.detail


def test_connection_refused_fails_naming_nginx(monkeypatch, tmp_path):
    _install_nginx_site(monkeypatch, tmp_path)
    err = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with patch("urllib.request.urlopen", side_effect=err):
        r = doctor_web.check_management_surface()
    assert r.status == "fail"
    assert "nginx" in r.detail
