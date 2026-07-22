# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the two-tier Spotify-preempt escalation in jasper.mux.

Tier 1 (existing): Spotify Web API `PUT /me/player/pause` via spotipy.
Tier 2 (added 2026-05-22): `systemctl try-restart librespot.service` if
Tier 1 fails and librespot is still active. Tier 2 still matters after the
fan-in cutover: an un-pauseable librespot owns its private fan-in lane, stays
alive, and is summed with the new winner until it releases that lane.
The user's contract ("we cannot have both played at the same time")
requires us to force a release.

Off-switch: JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled reverts to
"Web API only, mix-on-failure" behaviour (pre-2026-05-22 contract).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jasper.mux import Mux, Source


@pytest.fixture
def mux(tmp_path):
    return Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))


def _mock_broker(*, ok: bool = True, rc: int = 0, error: str | None = None):
    """Replacement for jasper-control's restart broker client
    (restart_broker.manage_units, which jasper-mux now calls via
    asyncio.to_thread instead of shelling out to systemctl). Records every
    call and returns a configurable result dict. manage_units never raises —
    a failed restart surfaces as {"ok": False}, exactly like production."""
    captured: dict[str, list] = {"calls": []}

    def fake(*units, **kwargs):
        captured["calls"].append((units, kwargs))
        resp: dict = {"ok": ok, "action": kwargs.get("verb"), "units": list(units)}
        if not ok:
            if error is not None:
                resp["error"] = error
            else:
                resp["rc"] = rc
        return resp

    return fake, captured


def _stub_web_api_result(mux: Mux, ok: bool):
    """Force `_spotify_pause_via_web_api` to return a fixed value
    without standing up the multi-account router machinery."""
    mux._spotify_pause_via_web_api = AsyncMock(return_value=ok)


# ----------------------------------------------------------------------
# Web API succeeds — Tier 2 should NOT fire
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_web_api_succeeds_no_restart(mux):
    """The happy path. The restart broker must not be invoked."""
    _stub_web_api_result(mux, ok=True)
    fake, captured = _mock_broker()
    with patch("jasper.control.restart_broker.manage_units", side_effect=fake):
        await mux._pause(Source.SPOTIFY)
    assert captured["calls"] == [], (
        f"broker should not run on Web API success; saw: {captured['calls']}"
    )


# ----------------------------------------------------------------------
# Web API fails, escalation enabled — Tier 2 fires
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_web_api_fails_escalates_to_active_only_restart(mux):
    """Tier 1 returns False → Tier 2 may restart only active librespot."""
    _stub_web_api_result(mux, ok=False)
    fake, captured = _mock_broker(ok=True)
    with patch("jasper.control.restart_broker.manage_units", side_effect=fake):
        await mux._pause(Source.SPOTIFY)
    assert len(captured["calls"]) == 1
    units, kwargs = captured["calls"][0]
    assert units == ("librespot.service",)
    assert kwargs["verb"] == "try-restart"


@pytest.mark.asyncio
async def test_spotify_recovery_cannot_resurrect_concurrently_stopped_source(mux):
    """A source Off/park landing before the final mutation must remain Off."""
    unit_active = False  # source coordinator won the race before broker call
    calls = []

    def fake_broker(*units, **kwargs):
        nonlocal unit_active
        calls.append((units, kwargs))
        if kwargs["verb"] == "restart":
            unit_active = True
        # systemctl try-restart is a successful no-op while inactive.
        return {"ok": True}

    with patch(
        "jasper.control.restart_broker.manage_units",
        side_effect=fake_broker,
    ):
        assert await mux._spotify_force_restart_librespot() is True

    assert calls[0][1]["verb"] == "try-restart"
    assert unit_active is False


# ----------------------------------------------------------------------
# Off-switch disables escalation
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_off_switch_disables_escalation(mux, monkeypatch):
    """JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled reverts to Tier-1-only.
    With Web API failed and escalation off, the broker must not be asked."""
    monkeypatch.setenv("JASPER_MUX_SPOTIFY_PREEMPT_RESTART", "disabled")
    _stub_web_api_result(mux, ok=False)
    fake, captured = _mock_broker()
    with patch("jasper.control.restart_broker.manage_units", side_effect=fake):
        await mux._pause(Source.SPOTIFY)
    assert captured["calls"] == []


# ----------------------------------------------------------------------
# A failed try-restart doesn't raise — log and continue
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_restart_nonzero_does_not_raise(mux):
    """A non-zero try-restart result is logged but not raised; the mux tick must
    continue. (Retrying every tick would just create log noise.)"""
    _stub_web_api_result(mux, ok=False)
    fake, _ = _mock_broker(ok=False, rc=1)
    with patch("jasper.control.restart_broker.manage_units", side_effect=fake):
        # Should complete without raising.
        await mux._pause(Source.SPOTIFY)


@pytest.mark.asyncio
async def test_pause_spotify_restart_broker_unavailable_does_not_raise(mux):
    """If the broker is unreachable (and mux is non-root so there's no
    fallback), manage_units returns {"ok": False}; the mux loop must fail
    soft rather than crash."""
    _stub_web_api_result(mux, ok=False)
    fake, _ = _mock_broker(ok=False, error="restart broker unavailable: [Errno 2]")
    with patch("jasper.control.restart_broker.manage_units", side_effect=fake):
        await mux._pause(Source.SPOTIFY)


# ----------------------------------------------------------------------
# Web API: two-pass behaviour — prefers is_active, falls through
# ----------------------------------------------------------------------

class _FakeSpClient:
    """Minimal spotipy stand-in: .sp.devices() and .sp.pause_playback()."""
    def __init__(self, name: str, devices: list[dict], raises_on_pause=False):
        self.account = SimpleNamespace(name=name)
        self.sp = SimpleNamespace(
            devices=lambda: {"devices": devices},
            pause_playback=lambda device_id: self._do_pause(device_id),
        )
        self._raises_on_pause = raises_on_pause
        self.pause_calls: list[str] = []

    def _do_pause(self, device_id):
        self.pause_calls.append(device_id)
        if self._raises_on_pause:
            raise RuntimeError("simulated Web API pause failure")


def _attach_fake_router(mux: Mux, clients: list[_FakeSpClient]):
    """Bypass `_ensure_spotify_router`'s multi-account-machinery
    setup by directly seeding the router cache."""
    mux._spotify_router_built = True
    mux._spotify_router = SimpleNamespace(
        clients={c.account.name: c for c in clients},
    )


@pytest.mark.asyncio
async def test_web_api_prefers_active_device_when_available(mux, monkeypatch):
    """The two-pass logic should hit an is_active device on pass 1 and
    never fall through to pass 2 (which would also try inactive devs)."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    active = _FakeSpClient("primary", [
        {"name": "JTS", "id": "jts-active", "is_active": True},
    ])
    _attach_fake_router(mux, [active])
    assert await mux._spotify_pause_via_web_api() is True
    assert active.pause_calls == ["jts-active"]


@pytest.mark.asyncio
async def test_web_api_falls_through_to_inactive_device(mux, monkeypatch):
    """If no account has JTS as is_active, the second pass should still
    try inactive JTS devices. Real-world rationale: Spotify's is_active
    flag lags behind player state by multiple seconds; librespot can be
    audibly playing while Web API still says is_active=False."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    inactive = _FakeSpClient("primary", [
        {"name": "JTS", "id": "jts-inactive", "is_active": False},
    ])
    _attach_fake_router(mux, [inactive])
    assert await mux._spotify_pause_via_web_api() is True
    assert inactive.pause_calls == ["jts-inactive"]


@pytest.mark.asyncio
async def test_web_api_no_jts_device_at_all_returns_false(mux, monkeypatch):
    """If no account has any device named JTS, the Web API path fails
    cleanly (returns False); caller then escalates to systemctl."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    other = _FakeSpClient("primary", [
        {"name": "Phone", "id": "phone-1", "is_active": True},
    ])
    _attach_fake_router(mux, [other])
    assert await mux._spotify_pause_via_web_api() is False
    assert other.pause_calls == []


@pytest.mark.asyncio
async def test_web_api_pause_exception_on_one_account_tries_next(
    mux, monkeypatch,
):
    """If one account's pause_playback raises, the loop should continue
    to the next account rather than aborting the whole preempt."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    bad = _FakeSpClient("primary", [
        {"name": "JTS", "id": "jts-1", "is_active": True},
    ], raises_on_pause=True)
    good = _FakeSpClient("secondary", [
        {"name": "JTS", "id": "jts-2", "is_active": True},
    ])
    _attach_fake_router(mux, [bad, good])
    assert await mux._spotify_pause_via_web_api() is True
    assert bad.pause_calls == ["jts-1"]
    assert good.pause_calls == ["jts-2"]


# ----------------------------------------------------------------------
# A hung Spotify API socket must NOT suspend the mux tick. spotipy has
# no requests_timeout by default; the two to_thread calls in
# _spotify_pause_via_web_api are wrapped in asyncio.wait_for so a stuck
# account is bounded and the loop continues to the next account.
# ----------------------------------------------------------------------

class _HangingSpClient:
    """spotipy stand-in whose .sp call blocks until released. Models a
    hung Spotify API socket (no server response). The blocking happens
    inside a worker thread via asyncio.to_thread, exactly as the real
    spotipy HTTP call would."""

    def __init__(self, name, devices, *, hang_on):
        import threading
        self.account = SimpleNamespace(name=name)
        self._release = threading.Event()
        self.pause_calls: list[str] = []

        def _devices():
            if hang_on == "devices":
                self._release.wait(timeout=10.0)
            return {"devices": devices}

        def _pause(device_id):
            self.pause_calls.append(device_id)
            if hang_on == "pause":
                self._release.wait(timeout=10.0)

        self.sp = SimpleNamespace(devices=_devices, pause_playback=_pause)

    def release(self):
        self._release.set()


@pytest.fixture
def _clamp_wait_for(monkeypatch):
    """Clamp the wait_for timeout *as seen by jasper.mux* to a tiny value
    so a hung call trips the timeout in milliseconds instead of the
    production 5 s, keeping the test fast while still exercising the real
    wrap. Returns the unclamped wait_for so the test's own outer guard
    isn't shortened."""
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout):
        return await real_wait_for(aw, timeout=0.1)

    import jasper.mux as _mux
    monkeypatch.setattr(_mux.asyncio, "wait_for", fast_wait_for)
    return real_wait_for


@pytest.mark.asyncio
async def test_web_api_hung_devices_does_not_hang_tick(
    mux, monkeypatch, _clamp_wait_for,
):
    """A hung sp.devices() call must time out and be skipped — the pause
    helper returns without suspending the mux tick. A second, healthy
    account still gets paused, proving the loop continues."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    hung = _HangingSpClient("primary", [
        {"name": "JTS", "id": "jts-hung", "is_active": True},
    ], hang_on="devices")
    good = _FakeSpClient("secondary", [
        {"name": "JTS", "id": "jts-good", "is_active": True},
    ])
    _attach_fake_router(mux, [hung, good])
    real_wait_for = _clamp_wait_for
    try:
        result = await real_wait_for(
            mux._spotify_pause_via_web_api(), timeout=5.0,
        )
    finally:
        hung.release()
    assert result is True
    assert good.pause_calls == ["jts-good"]


@pytest.mark.asyncio
async def test_web_api_hung_pause_does_not_hang_tick(
    mux, monkeypatch, _clamp_wait_for,
):
    """A hung sp.pause_playback() call must time out and be skipped —
    the loop continues to the next account rather than freezing the
    mux tick indefinitely on one stuck account."""
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS")
    hung = _HangingSpClient("primary", [
        {"name": "JTS", "id": "jts-hung", "is_active": True},
    ], hang_on="pause")
    good = _FakeSpClient("secondary", [
        {"name": "JTS", "id": "jts-good", "is_active": True},
    ])
    _attach_fake_router(mux, [hung, good])
    real_wait_for = _clamp_wait_for
    try:
        result = await real_wait_for(
            mux._spotify_pause_via_web_api(), timeout=5.0,
        )
    finally:
        hung.release()
    assert result is True
    assert hung.pause_calls == ["jts-hung"]
    assert good.pause_calls == ["jts-good"]
