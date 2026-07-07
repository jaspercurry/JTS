# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Observability for the phone-mic capture relay: config snapshot, health probe,
the jasper-doctor check, and the /state ↔ health lockstep.

The relay transport is a per-measurement library (no resident daemon), so the
operator surfaces report configuration + reachability. `/state` reads the env
directly (to keep jasper-control off the numpy/scipy deps); the doctor imports
the helper to probe. These tests pin that the two reads agree and that the doctor
check skips cleanly until configured.
"""
from __future__ import annotations

import pytest

from jasper.capture_relay import health


def test_relay_config_unconfigured(monkeypatch):
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN", raising=False)
    assert health.relay_config_from_env() == {
        "configured": False,
        "relay_base": None,
        "registration_secret_configured": False,
    }
    assert health.relay_base_from_env() is None
    assert health.relay_registration_token_from_env() is None


def test_relay_config_configured_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech/")
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN", "  pi-secret  ")
    assert health.relay_config_from_env() == {
        "configured": True,
        "relay_base": "https://relay.jasper.tech",
        "registration_secret_configured": True,
    }
    assert health.relay_registration_token_from_env() == "pi-secret"


@pytest.mark.parametrize(
    "value",
    ["disabled", "off", "0", "none", " DISABLED/ "],
)
def test_relay_config_explicit_disable_sentinel(monkeypatch, value):
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", value)
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN", raising=False)
    assert health.relay_config_from_env() == {
        "configured": False,
        "relay_base": None,
        "registration_secret_configured": False,
    }
    assert health.relay_base_from_env() is None


def test_probe_rejects_non_https():
    ok, detail = health.probe_relay_health("http://relay.jasper.tech")
    assert ok is False
    assert "https" in detail


def test_probe_reachable(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def read(self, _n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, *, timeout):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(health.urllib.request, "urlopen", _urlopen)
    ok, detail = health.probe_relay_health("https://relay.jasper.tech")
    assert ok is True
    assert "reachable" in detail
    assert seen["headers"]["user-agent"] == health.RELAY_USER_AGENT
    assert seen["headers"]["accept"] == "application/json"
    assert seen["timeout"] == 2.0


def test_probe_unreachable(monkeypatch):
    def _boom(*a, **k):
        raise health.urllib.error.URLError("no route")

    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    ok, detail = health.probe_relay_health("https://relay.jasper.tech")
    assert ok is False
    assert "unreachable" in detail


# --- /state ↔ health lockstep (the drift guard the comments promise) ----------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "https://relay.jasper.tech",
        "https://relay.jasper.tech/",
        "",
        *sorted(health.DISABLED_RELAY_BASE_VALUES),
    ],
)
def test_state_snapshot_matches_health(monkeypatch, value):
    from jasper.control.state_aggregate import _capture_relay_config

    if value is None:
        monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    else:
        monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", value)
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN", "secret")
    assert _capture_relay_config() == health.relay_config_from_env()


# --- doctor check -------------------------------------------------------------


def test_doctor_skips_when_unconfigured(monkeypatch):
    from jasper.cli.doctor.correction import check_capture_relay

    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    result = check_capture_relay()
    assert result.status == "ok"
    assert "not configured" in result.detail


def test_doctor_ok_when_reachable(monkeypatch):
    from jasper.cli.doctor.correction import check_capture_relay

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    monkeypatch.setattr(health, "probe_relay_health", lambda *_a, **_k: (True, "reachable (ok)"))
    result = check_capture_relay()
    assert result.status == "ok"
    assert "relay.jasper.tech" in result.detail


def test_doctor_warns_when_unreachable(monkeypatch):
    from jasper.cli.doctor.correction import check_capture_relay

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    monkeypatch.setattr(
        health, "probe_relay_health", lambda *_a, **_k: (False, "unreachable: timeout")
    )
    result = check_capture_relay()
    assert result.status == "warn"
    # Existing corrections are unaffected — say so.
    assert "existing applied corrections are unaffected" in result.detail
