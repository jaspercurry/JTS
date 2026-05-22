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


# ---- Auto-reset cache between tests ----------------------------------------

@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """probe_status caches results process-globally for 15s by default.
    Without an auto-reset, a test that mocks 'connected' would poison
    every subsequent test sharing the same (url, token) key. The reset
    is cheap (clears two module-level vars)."""
    ha_mod._reset_cache_for_tests()
    yield
    ha_mod._reset_cache_for_tests()


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


# ---- Caching --------------------------------------------------------------
#
# These tests stub _probe_uncached so the cache layer is exercised in
# isolation from the HTTP path. Each call to the stub increments a
# counter we use to assert hit vs miss.

@pytest.mark.asyncio
async def test_cache_hit_skips_uncached_probe(monkeypatch):
    """Second call within TTL with the same (url, token) reuses the
    cached result without re-invoking _probe_uncached."""
    calls = {"n": 0}

    async def fake_uncached(url, token, *, verify_ssl=True):
        calls["n"] += 1
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "2026.5.1", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    r1 = await probe_status("http://ha.local:8123", "tok")
    r2 = await probe_status("http://ha.local:8123", "tok")

    assert calls["n"] == 1
    assert r1 == r2


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    """After PROBE_CACHE_TTL_SEC elapses, the next call re-probes."""
    calls = {"n": 0}

    async def fake_uncached(url, token, *, verify_ssl=True):
        calls["n"] += 1
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "2026.5.1", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    # Drive the cache's monotonic clock with a controllable clock.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(ha_mod.time, "monotonic", lambda: fake_now["t"])

    await probe_status("http://ha.local:8123", "tok")
    assert calls["n"] == 1
    # Advance just past the TTL
    fake_now["t"] += ha_mod.PROBE_CACHE_TTL_SEC + 0.1
    await probe_status("http://ha.local:8123", "tok")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_cache_keyed_on_url_and_token(monkeypatch):
    """Changing url OR token invalidates the cache — the next probe
    runs fresh."""
    calls = {"n": 0}

    async def fake_uncached(url, token, *, verify_ssl=True):
        calls["n"] += 1
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "2026.5.1", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    await probe_status("http://ha-a.local:8123", "tok1")  # 1
    await probe_status("http://ha-a.local:8123", "tok1")  # cache hit
    await probe_status("http://ha-b.local:8123", "tok1")  # 2 (different url)
    await probe_status("http://ha-b.local:8123", "tok2")  # 3 (different token)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_force_bypasses_cache(monkeypatch):
    """jasper-doctor passes force=True so its output reflects ground
    truth at invocation time, not whatever was last cached."""
    calls = {"n": 0}

    async def fake_uncached(url, token, *, verify_ssl=True):
        calls["n"] += 1
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "2026.5.1", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    await probe_status("http://ha.local:8123", "tok")
    await probe_status("http://ha.local:8123", "tok", force=True)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_force_does_not_poison_cache(monkeypatch):
    """A force=True call doesn't write to the cache — subsequent
    non-forced calls still see the pre-force cached value (if any)
    or run fresh (if no prior cache). Either way, the force result
    doesn't displace what a regular caller expects."""
    cached_value = {
        "configured": True, "connected": True, "url": "x",
        "instance_name": "Old", "version": "1.0", "error": None,
    }
    fresh_value = {
        "configured": True, "connected": True, "url": "x",
        "instance_name": "New", "version": "2.0", "error": None,
    }
    state = {"return": cached_value, "calls": 0}

    async def fake_uncached(url, token, *, verify_ssl=True):
        state["calls"] += 1
        return state["return"]
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    # Prime the cache with `cached_value`.
    r1 = await probe_status("http://ha.local:8123", "tok")
    assert r1["instance_name"] == "Old"
    # Force a call — sees the new state.
    state["return"] = fresh_value
    r2 = await probe_status("http://ha.local:8123", "tok", force=True)
    assert r2["instance_name"] == "New"
    # Regular call right after: should still see the cached value (the
    # force=True call didn't displace it).
    state["return"] = {"sentinel": "would-be-third-call"}
    r3 = await probe_status("http://ha.local:8123", "tok")
    assert r3["instance_name"] == "Old"
    # Three uncached calls total: prime + force + (would-be-third never ran)
    assert state["calls"] == 2


# ---- State-transition logging ---------------------------------------------

@pytest.mark.asyncio
async def test_logs_reachable_on_first_connected_probe(monkeypatch, caplog):
    """First probe that returns connected=true emits event=ha.reachable."""
    async def fake_uncached(url, token, *, verify_ssl=True):
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "2026.5.1", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    import logging
    with caplog.at_level(logging.INFO, logger="jasper.home_assistant"):
        await probe_status("http://ha.local:8123", "tok")

    assert any("event=ha.reachable" in r.message for r in caplog.records)
    assert any("Home" in r.message and "2026.5.1" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_logs_unreachable_on_transition(monkeypatch, caplog):
    """connected: true → false transition emits event=ha.unreachable."""
    state = {"connected": True}

    async def fake_uncached(url, token, *, verify_ssl=True):
        if state["connected"]:
            return {
                "configured": True, "connected": True, "url": url,
                "instance_name": "Home", "version": "1.0", "error": None,
            }
        return {
            "configured": True, "connected": False, "url": url,
            "instance_name": None, "version": None,
            "error": "Couldn't reach Home Assistant",
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    import logging
    # Prime as reachable.
    await probe_status("http://ha.local:8123", "tok")
    # Force the next call to bypass cache + flip state.
    state["connected"] = False
    with caplog.at_level(logging.WARNING, logger="jasper.home_assistant"):
        await probe_status("http://ha.local:8123", "tok", force=True)

    assert any("event=ha.unreachable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_log_when_state_unchanged(monkeypatch, caplog):
    """Two consecutive probes both returning connected=true don't emit
    a second event=ha.reachable. We log on transitions, not per call."""
    async def fake_uncached(url, token, *, verify_ssl=True):
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Home", "version": "1.0", "error": None,
        }
    monkeypatch.setattr(ha_mod, "_probe_uncached", fake_uncached)

    import logging
    # First call logs (initial state).
    await probe_status("http://ha.local:8123", "tok")
    caplog.clear()
    # Second forced call (cache bypass) — same state, no new log.
    with caplog.at_level(logging.INFO, logger="jasper.home_assistant"):
        await probe_status("http://ha.local:8123", "tok", force=True)

    assert not any("event=ha.reachable" in r.message for r in caplog.records)


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
    assert "/ha" in result.detail  # actionable hint


def test_check_home_assistant_ok_when_probe_succeeds(monkeypatch):
    from jasper.cli import doctor

    async def fake_probe(url, token, *, force=False, verify_ssl=True):
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

    async def fake_probe(url, token, *, force=False, verify_ssl=True):
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
    assert "/ha" in result.detail


def test_check_home_assistant_fail_when_probe_raises(monkeypatch):
    from jasper.cli import doctor

    async def fake_probe(url, token, *, force=False, verify_ssl=True):
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
