# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's CamillaGUI socket-bind check (#2319).

camillagui.socket used to bind 0.0.0.0:5005 — an unauthenticated,
root-backed listener reachable from any device on the LAN. The shipped
unit now binds 127.0.0.1:5005 (tests/test_camillagui_systemd.py pins the
unit file itself); this module pins the doctor check that reads the LIVE
kernel-level bind via `ss` so a botched restart that leaves the old
0.0.0.0 fd open is caught rather than masked. The `ss` calls are mocked;
there is no real socket here."""
from __future__ import annotations

import subprocess

from jasper.cli import doctor
from jasper.cli.doctor import web as doctor_web

from .doctor_test_support import _registered_check_names


def _ss_output(*lines: str) -> str:
    """Join fake `ss -H -ltn` rows. Real column shape:
    STATE  RECV-Q  SEND-Q  LOCAL-ADDR:PORT  PEER-ADDR:PORT"""
    return "\n".join(lines) + "\n" if lines else ""


def _fake_run(stdout: str, *, returncode: int = 0):
    def run(cmd, timeout=5.0):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr="",
        )

    return run


def test_ok_when_loopback_only(monkeypatch):
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output("LISTEN 0 128   127.0.0.1:5005   0.0.0.0:*")),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "ok"
    assert "loopback-only" in r.detail
    assert "127.0.0.1:5005" in r.detail


def test_warn_when_bound_to_all_interfaces(monkeypatch):
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output("LISTEN 0 128     0.0.0.0:5005   0.0.0.0:*")),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "0.0.0.0:5005" in r.detail
    assert "#2319" in r.detail
    assert "LAN-reachable" in r.detail


def test_warn_when_any_listener_is_non_loopback_even_alongside_a_loopback_one(
    monkeypatch,
):
    """Discriminating comparison: the check must flag non-loopback even when
    a loopback listener is ALSO present (e.g. a moment mid-restart) — a
    naive "check only the first row" implementation would pass this."""
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output(
            "LISTEN 0 128   127.0.0.1:5005   0.0.0.0:*",
            "LISTEN 0 128     0.0.0.0:5005   0.0.0.0:*",
        )),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "0.0.0.0:5005" in r.detail


def test_ok_when_not_listening(monkeypatch):
    """Covers both 'never installed' and 'administratively stopped' (e.g.
    the jts3 hardware runbooks' precautionary socket stop) — neither is a
    live exposure, so this is ok, not a warning."""
    monkeypatch.setattr(doctor_web, "_run", _fake_run(_ss_output()))
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "ok"
    assert "not currently listening" in r.detail


def test_ignores_other_ports(monkeypatch):
    """Discriminating comparison: a non-loopback listener on a DIFFERENT
    port (e.g. the backend's own 5006, or an unrelated service) must not
    trip the check — only :5005 is CamillaGUI's externally-reachable
    socket. A mutated port comparison (e.g. always-true) would warn here."""
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output(
            "LISTEN 0 128   127.0.0.1:5006   0.0.0.0:*",
            "LISTEN 0 128     0.0.0.0:8780   0.0.0.0:*",
        )),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "ok"
    assert "not currently listening" in r.detail


def test_ipv6_loopback_is_ok(monkeypatch):
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output("LISTEN 0 128        [::1]:5005      [::]:*")),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "ok"
    # Detail-content pinned, not just status: "not currently listening" is
    # ALSO status "ok" (test_ok_when_not_listening) but reached via a
    # completely different branch (empty addresses vs. a recognized
    # loopback address). Without this, a mutation that misparses "[::1]"
    # into "not found at all" would silently fall through to that other
    # ok branch and this test would still pass for the wrong reason.
    assert "loopback-only" in r.detail
    assert "[::1]:5005" in r.detail


def test_ipv6_wildcard_warns(monkeypatch):
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output("LISTEN 0 128         [::]:5005      [::]:*")),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "[::]:5005" in r.detail


def test_warn_when_ss_missing(monkeypatch):
    def raises(cmd, timeout=5.0):
        raise FileNotFoundError("ss not found")

    monkeypatch.setattr(doctor_web, "_run", raises)
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "can't verify" in r.detail


def test_warn_when_ss_not_executable(monkeypatch):
    """PermissionError (non-executable ss binary) and a fork failure under
    memory pressure (OSError, e.g. errno ENOMEM) are OSError but not
    FileNotFoundError — the probe must still degrade to warn, not crash
    the whole doctor run. `PermissionError` is a concrete stand-in for
    both: both are plain OSError subclasses the narrower
    (SubprocessError, FileNotFoundError) except clause would miss."""
    def raises(cmd, timeout=5.0):
        raise PermissionError("ss not executable")

    monkeypatch.setattr(doctor_web, "_run", raises)
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "can't verify" in r.detail


def test_warn_when_ss_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        doctor_web, "_run", _fake_run("", returncode=1),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "warn"
    assert "can't verify" in r.detail


def test_ignores_malformed_ss_lines(monkeypatch):
    """A short/odd line (fewer than 4 whitespace fields, or a non-LISTEN
    state) must not crash the parser."""
    monkeypatch.setattr(
        doctor_web, "_run",
        _fake_run(_ss_output(
            "",
            "garbage",
            "ESTAB 0 0   127.0.0.1:5005   127.0.0.1:9",
        )),
    )
    r = doctor_web.check_camillagui_loopback()
    assert r.status == "ok"
    assert "not currently listening" in r.detail


def test_check_registered_in_web_group():
    assert "check_camillagui_loopback" in _registered_check_names()
    by_name = {c.func.__name__: c for c in doctor.registered_checks()}
    assert by_name["check_camillagui_loopback"].group == "web"
