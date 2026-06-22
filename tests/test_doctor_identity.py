# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's identity checks.

check_identity_coherence reads the reconciler-written identity.env via
jasper.identity_state; check_correction_cert_hostname compares the
/correction/ TLS cert SAN against the advertised name. Filesystem and
subprocess surfaces are mocked; on-Pi smoke testing is jasper-doctor
itself.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from jasper.cli.doctor import correction as doctor_correction
from jasper.cli.doctor import network as doctor_network


def _write_identity(tmp_path, monkeypatch, *, collision="0", drift="0",
                    checked_at=None, avahi="jts3.local"):
    if checked_at is None:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    f = tmp_path / "identity.env"
    f.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=jts3\n"
        f"JASPER_IDENTITY_AVAHI_HOSTNAME={avahi}\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts3.local\n"
        "JASPER_IDENTITY_AVAHI_AVAILABLE=1\n"
        f"JASPER_IDENTITY_COLLISION={collision}\n"
        f"JASPER_IDENTITY_DRIFT={drift}\n"
        f"JASPER_IDENTITY_CHECKED_AT={checked_at}\n"
    )
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(f))
    return f


# ----------------------------------------------------------------------
# check_identity_coherence
# ----------------------------------------------------------------------


def test_coherence_ok(monkeypatch, tmp_path):
    _write_identity(tmp_path, monkeypatch)
    r = doctor_network.check_identity_coherence()
    assert r.status == "ok"
    assert "avahi=jts3.local" in r.detail


def test_coherence_collision_warns_with_reachable_name(monkeypatch, tmp_path):
    _write_identity(
        tmp_path, monkeypatch, collision="1", drift="1", avahi="jts3-2.local",
    )
    r = doctor_network.check_identity_coherence()
    assert r.status == "warn"
    assert "jts3-2.local" in r.detail
    assert "rename-speaker" in r.detail


def test_coherence_drift_warns(monkeypatch, tmp_path):
    _write_identity(tmp_path, monkeypatch, drift="1")
    r = doctor_network.check_identity_coherence()
    assert r.status == "warn"
    assert "JASPER_HOSTNAME" in r.detail


def test_coherence_stale_snapshot_warns(monkeypatch, tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _write_identity(tmp_path, monkeypatch, checked_at=old)
    r = doctor_network.check_identity_coherence()
    assert r.status == "warn"
    assert "timer" in r.detail


def test_coherence_absent_skips_off_pi(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(
        doctor_network.os.path, "exists", lambda p: False,
    )
    r = doctor_network.check_identity_coherence()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_coherence_absent_warns_when_reconciler_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(
        doctor_network.os.path, "exists",
        lambda p: p == "/usr/local/sbin/jasper-identity-reconcile",
    )
    r = doctor_network.check_identity_coherence()
    assert r.status == "warn"
    assert "systemctl start jasper-identity-reconcile" in r.detail


# ----------------------------------------------------------------------
# check_correction_cert_hostname
# ----------------------------------------------------------------------


def _with_cert(monkeypatch, tmp_path, exists=True):
    cert = tmp_path / "jts.local.crt"
    if exists:
        cert.write_text("---")
    real_path = doctor_correction.Path

    def fake_path(p):
        if p == "/etc/nginx/ssl/jts.local.crt":
            return cert
        return real_path(p)

    monkeypatch.setattr(doctor_correction, "Path", fake_path)


def test_cert_check_skips_without_cert(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path, exists=False)
    r = doctor_correction.check_correction_cert_hostname()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_cert_san_covers_advertised_name(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path)
    _write_identity(tmp_path, monkeypatch)
    fake = SimpleNamespace(
        returncode=0,
        stdout="X509v3 Subject Alternative Name:\n"
               "    DNS:jts3.local, DNS:*.jts3.local, DNS:jts.local\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = doctor_correction.check_correction_cert_hostname()
    assert r.status == "ok"


def test_cert_san_missing_advertised_name_warns(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path)
    _write_identity(tmp_path, monkeypatch, avahi="jts3-2.local",
                    collision="1", drift="1")
    fake = SimpleNamespace(
        returncode=0,
        stdout="X509v3 Subject Alternative Name:\n"
               "    DNS:jts3.local, DNS:*.jts3.local\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = doctor_correction.check_correction_cert_hostname()
    assert r.status == "warn"
    assert "jts3-2.local" in r.detail
    assert "deploy" in r.detail
