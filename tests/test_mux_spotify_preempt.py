"""Tests for the two-tier Spotify-preempt escalation in jasper.mux.

Tier 1 (existing): Spotify Web API `PUT /me/player/pause` via spotipy.
Tier 2 (added 2026-05-22): `systemctl restart librespot.service` if
Tier 1 fails. Tier 2 still matters after the fan-in cutover: an
un-pauseable librespot owns its private fan-in lane, stays alive, and
is summed with the new winner until it releases that lane.
The user's contract ("we cannot have both played at the same time")
requires us to force a release.

Off-switch: JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled reverts to
"Web API only, mix-on-failure" behaviour (pre-2026-05-22 contract).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.mux import Mux, Source


@pytest.fixture
def mux(tmp_path):
    return Mux(librespot_state_path=str(tmp_path / "librespot.state.json"))


def _mock_systemctl_router(returncode: int = 0, stderr: bytes = b""):
    """asyncio.create_subprocess_exec replacement that recognises a
    `systemctl restart librespot.service` invocation and returns
    a configurable result. Other invocations get a default succeed."""
    captured: dict[str, list] = {"calls": []}

    async def fake(*args, **kwargs):
        captured["calls"].append(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        proc.returncode = returncode
        return proc

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
    """The happy path. systemctl must not be invoked."""
    _stub_web_api_result(mux, ok=True)
    fake_exec, captured = _mock_systemctl_router()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await mux._pause(Source.SPOTIFY)
    assert captured["calls"] == [], (
        f"systemctl should not run on Web API success; saw: {captured['calls']}"
    )


# ----------------------------------------------------------------------
# Web API fails, escalation enabled — Tier 2 fires
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_web_api_fails_escalates_to_restart(mux):
    """Tier 1 returns False → Tier 2 must invoke
    `systemctl restart librespot.service`."""
    _stub_web_api_result(mux, ok=False)
    fake_exec, captured = _mock_systemctl_router(returncode=0)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await mux._pause(Source.SPOTIFY)
    assert len(captured["calls"]) == 1
    call_args = captured["calls"][0]
    assert "systemctl" in call_args[0]
    assert "restart" in call_args
    assert "librespot.service" in call_args


# ----------------------------------------------------------------------
# Off-switch disables escalation
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_off_switch_disables_escalation(mux, monkeypatch):
    """JASPER_MUX_SPOTIFY_PREEMPT_RESTART=disabled reverts to Tier-1-only.
    With Web API failed and escalation off, systemctl must not fire."""
    monkeypatch.setenv("JASPER_MUX_SPOTIFY_PREEMPT_RESTART", "disabled")
    _stub_web_api_result(mux, ok=False)
    fake_exec, captured = _mock_systemctl_router()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await mux._pause(Source.SPOTIFY)
    assert captured["calls"] == []


# ----------------------------------------------------------------------
# systemctl itself failing doesn't raise — log and continue
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_spotify_restart_systemctl_nonzero_does_not_raise(mux):
    """systemctl exit != 0 is logged but not raised; the mux tick must
    continue. (If systemctl is somehow unavailable, retrying every tick
    would just create log noise.)"""
    _stub_web_api_result(mux, ok=False)
    fake_exec, _ = _mock_systemctl_router(
        returncode=1, stderr=b"Unit librespot.service not loaded.",
    )
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        # Should complete without raising.
        await mux._pause(Source.SPOTIFY)


@pytest.mark.asyncio
async def test_pause_spotify_restart_systemctl_missing_does_not_raise(mux):
    """systemctl missing on PATH (unlikely on Trixie but possible in a
    container) must fail soft, not crash the mux loop."""
    _stub_web_api_result(mux, ok=False)
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("systemctl"),
    ):
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
