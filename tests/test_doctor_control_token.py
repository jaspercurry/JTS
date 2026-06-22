# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's control-token posture check.

check_control_token reports whether the gate has a readable token — a posture
line, ok either way — and must NEVER echo the token value.
"""
from __future__ import annotations

from jasper.cli.doctor import web as doctor_web
from jasper.control import control_token


def test_disabled_posture_is_ok_and_secret_free(monkeypatch, tmp_path):
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(tmp_path / "absent"))
    r = doctor_web.check_control_token()
    assert r.status == "ok"
    assert "disabled" in r.detail.lower()
    assert "SECURITY.md" in r.detail


def test_enabled_posture_is_ok_and_secret_free(monkeypatch, tmp_path):
    path = tmp_path / "control_token"
    secret = "super-secret-token-value-xyz"
    path.write_text(secret + "\n")
    monkeypatch.setattr(control_token, "TOKEN_FILE", str(path))
    r = doctor_web.check_control_token()
    assert r.status == "ok"
    assert "ENABLED" in r.detail
    assert "X-JTS-Token" in r.detail
    # The secret must never appear in the doctor output.
    assert secret not in r.detail
    assert secret not in r.name
