"""Tests for jasper.home_assistant.probe_status and the doctor check.

probe_status is the one-shot reachability + version helper consumed by
jasper-control's /state aggregator, the /system/ dashboard card, and
jasper-doctor's check_home_assistant. Same httpx.MockTransport pattern
as test_home_assistant.py.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import jasper.home_assistant as ha_mod
from jasper.home_assistant import HAClient, probe_status


# ---- probe_status (async) ---------------------------------------------------

def _mock_client(handler):
    """Return an httpx.AsyncClient backed by MockTransport with the
    auth header set, mirroring HAClient._client() output. Returned
    client is passed via monkeypatch into a captured HAClient instance."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        transport=transport,
        headers={"Authorization": "Bearer test"},
    )


@pytest.mark.asyncio
async def test_probe_status_returns_unconfigured_when_url_missing():
    result = await probe_status("", "any-token")
    assert result == {
        "configured": False, "connected": False, "url": "",
        "instance_name": None, "version": None, "error": None,
    }


@pytest.mark.asyncio
async def test_probe_status_returns_unconfigured_when_token_missing():
    result = await probe_status("http://homeassistant.local:8123", "")
    assert result["configured"] is False
    assert result["connected"] is False


@pytest.mark.asyncio
async def test_probe_status_connected_returns_instance_metadata(monkeypatch):
    """Healthy HA: GET /api/ returns API running, GET /api/config returns
    location_name + version."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if path == "/api/config":
            return httpx.Response(200, json={
                "location_name": "Brooklyn House",
                "version": "2026.5.1",
            })
        return httpx.Response(404)

    # Patch HAClient._client to return the mocked client.
    orig_client = HAClient._client

    async def fake_client(self):
        if self._http is None:
            self._http = _mock_client(handler)
        return self._http
    monkeypatch.setattr(HAClient, "_client", fake_client)

    result = await probe_status("http://homeassistant.local:8123", "test")

    assert result["configured"] is True
    assert result["connected"] is True
    assert result["url"] == "http://homeassistant.local:8123"
    assert result["instance_name"] == "Brooklyn House"
    assert result["version"] == "2026.5.1"
    assert result["error"] is None
    # Both endpoints exercised in order.
    assert requested == ["/api/", "/api/config"]


@pytest.mark.asyncio
async def test_probe_status_unreachable_returns_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    async def fake_client(self):
        if self._http is None:
            self._http = _mock_client(handler)
        return self._http
    monkeypatch.setattr(HAClient, "_client", fake_client)

    result = await probe_status("http://homeassistant.local:8123", "test")

    assert result["configured"] is True
    assert result["connected"] is False
    assert result["instance_name"] is None
    assert result["error"] and "Couldn't reach" in result["error"]


@pytest.mark.asyncio
async def test_probe_status_401_marks_not_connected(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="Unauthorized")

    async def fake_client(self):
        if self._http is None:
            self._http = _mock_client(handler)
        return self._http
    monkeypatch.setattr(HAClient, "_client", fake_client)

    result = await probe_status("http://homeassistant.local:8123", "bad")
    assert result["configured"] is True
    assert result["connected"] is False
    # Token error is surfaced through the generic "couldn't reach" copy —
    # probe_status doesn't differentiate auth from network at the UI
    # layer (that's the wizard's job). The error string just needs to
    # exist.
    assert result["error"]


@pytest.mark.asyncio
async def test_probe_status_falls_back_when_config_endpoint_fails(monkeypatch):
    """GET /api/ OK but /api/config returns 500 — still report connected,
    just without the enriched name/version."""
    def handler(request):
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(500)

    async def fake_client(self):
        if self._http is None:
            self._http = _mock_client(handler)
        return self._http
    monkeypatch.setattr(HAClient, "_client", fake_client)

    result = await probe_status("http://homeassistant.local:8123", "test")
    assert result["connected"] is True
    # Defaults when /api/config didn't give us anything.
    assert result["instance_name"] == "Home Assistant"
    assert result["version"] is None


# ---- doctor.check_home_assistant -------------------------------------------

def test_check_home_assistant_skip_when_not_enabled():
    from jasper.cli.doctor import check_home_assistant

    class _Cfg:
        ha_enabled = False
        ha_url = ""
        ha_token = ""
        ha_agent_id = ""
        hostname = "jts.local"

    result = check_home_assistant(_Cfg())
    assert result.status == "ok"
    assert "not configured" in result.detail
    assert "/homeassistant" in result.detail  # actionable hint


def test_check_home_assistant_ok_when_probe_succeeds(monkeypatch):
    from jasper.cli import doctor

    async def fake_probe(url, token):
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Brooklyn House", "version": "2026.5.1",
            "error": None,
        }
    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)

    class _Cfg:
        ha_enabled = True
        ha_url = "http://homeassistant.local:8123"
        ha_token = "test"
        ha_agent_id = ""
        hostname = "jts.local"

    result = doctor.check_home_assistant(_Cfg())
    assert result.status == "ok"
    assert "Brooklyn House" in result.detail
    assert "2026.5.1" in result.detail


def test_check_home_assistant_fail_when_unreachable(monkeypatch):
    from jasper.cli import doctor

    async def fake_probe(url, token):
        return {
            "configured": True, "connected": False, "url": url,
            "instance_name": None, "version": None,
            "error": "Couldn't reach Home Assistant — check the URL and token.",
        }
    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)

    class _Cfg:
        ha_enabled = True
        ha_url = "http://homeassistant.local:8123"
        ha_token = "test"
        ha_agent_id = ""
        hostname = "jts.local"

    result = doctor.check_home_assistant(_Cfg())
    assert result.status == "fail"
    assert "unreachable" in result.detail.lower()
    assert "homeassistant.local:8123" in result.detail
    # Actionable hint pointing at the wizard
    assert "/homeassistant" in result.detail


def test_check_home_assistant_fail_when_probe_raises(monkeypatch):
    from jasper.cli import doctor

    async def fake_probe(url, token):
        raise RuntimeError("network stack exploded")
    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)

    class _Cfg:
        ha_enabled = True
        ha_url = "http://homeassistant.local:8123"
        ha_token = "test"
        ha_agent_id = ""
        hostname = "jts.local"

    result = doctor.check_home_assistant(_Cfg())
    assert result.status == "fail"
    assert "raised" in result.detail.lower() or "error" in result.detail.lower()
