# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's tool-catalog check.

check_tool_catalog has three branches: skip-if-no-provider (ok), configured
but catalog absent (warn), and configured + present (ok with counts). It must
never crash — it's a diagnostic over fail-soft reads.
"""
from __future__ import annotations

from jasper import tool_catalog_view
from jasper.cli.doctor import web as doctor_web
from jasper.voice import provider_state


def test_skips_when_no_provider(monkeypatch):
    monkeypatch.setattr(provider_state, "read_active_provider", lambda: "")
    r = doctor_web.check_tool_catalog()
    assert r.status == "ok"
    assert "not configured" in r.detail.lower()


def test_warns_when_catalog_absent(monkeypatch):
    monkeypatch.setattr(provider_state, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tool_catalog_view, "summary", lambda: {
        "catalog_present": False, "count": 0,
        "disabled": [], "disabled_count": 0, "pending": False,
    })
    r = doctor_web.check_tool_catalog()
    assert r.status == "warn"
    assert "not written" in r.detail.lower()


def test_ok_with_counts_when_present(monkeypatch):
    monkeypatch.setattr(provider_state, "read_active_provider", lambda: "gemini")
    monkeypatch.setattr(tool_catalog_view, "summary", lambda: {
        "catalog_present": True, "count": 28,
        "disabled": ["get_weather"], "disabled_count": 1, "pending": True,
    })
    r = doctor_web.check_tool_catalog()
    assert r.status == "ok"
    assert "28 tools" in r.detail
    assert "1 disabled" in r.detail
    assert "restart pending" in r.detail
