"""Tests for jasper.renderer — the RendererBackend protocol +
DebianBackend implementation + make_backend factory.

Mocks at the I/O boundary: httpx.AsyncClient for go-librespot, and
asyncio.create_subprocess_exec for busctl / bluealsa-cli. MPD calls
are mocked per-test since the protocol requires `_mpd_call` to work
even when MPD is offline (caller `get_currentsong` falls back to
empty dict).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jasper.renderer import (
    DebianBackend,
    RendererBackend,
    _parse_mpris_metadata,
    make_backend,
)


# ----------------------------------------------------------------------
# make_backend factory
# ----------------------------------------------------------------------

def test_make_backend_debian_returns_debian_backend():
    b = make_backend(
        moode_base_url="http://127.0.0.1",
        mpd_host="127.0.0.1",
        mpd_port=6600,
        go_librespot_url="http://127.0.0.1:3678",
        backend_name="debian",
    )
    assert isinstance(b, DebianBackend)


def test_make_backend_moode_returns_moode_client():
    from jasper.moode import MoodeClient
    b = make_backend(
        moode_base_url="http://127.0.0.1",
        mpd_host="127.0.0.1",
        mpd_port=6600,
        backend_name="moode",
    )
    assert isinstance(b, MoodeClient)


def test_make_backend_reads_env_when_no_explicit_name(monkeypatch):
    monkeypatch.setenv("JASPER_RENDERER_BACKEND", "debian")
    b = make_backend(
        moode_base_url="http://127.0.0.1",
        mpd_host="127.0.0.1",
        mpd_port=6600,
    )
    assert isinstance(b, DebianBackend)


def test_make_backend_unknown_name_falls_back_to_moode(monkeypatch, caplog):
    from jasper.moode import MoodeClient
    monkeypatch.delenv("JASPER_RENDERER_BACKEND", raising=False)
    b = make_backend(
        moode_base_url="http://127.0.0.1",
        mpd_host="127.0.0.1",
        mpd_port=6600,
        backend_name="not-a-real-backend",
    )
    assert isinstance(b, MoodeClient)


def test_protocol_runtime_check():
    """Both backends should satisfy isinstance against the protocol."""
    debian = DebianBackend(
        go_librespot_url="http://127.0.0.1:3678",
        mpd_host="127.0.0.1",
        mpd_port=6600,
    )
    assert isinstance(debian, RendererBackend)
    from jasper.moode import MoodeClient
    moode = MoodeClient(
        base_url="http://127.0.0.1", mpd_host="127.0.0.1", mpd_port=6600,
    )
    assert isinstance(moode, RendererBackend)


# ----------------------------------------------------------------------
# DebianBackend.active_renderers — mocks each underlying source
# ----------------------------------------------------------------------

@pytest.fixture
def backend():
    return DebianBackend(
        go_librespot_url="http://127.0.0.1:3678",
        mpd_host="127.0.0.1",
        mpd_port=6600,
    )


def _mock_subprocess(stdout: bytes = b"", returncode: int = 0):
    """Build an asyncio.create_subprocess_exec replacement that returns
    a mock proc with .communicate() / .wait() pre-canned."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    async def fake(*args, **kwargs):
        return proc
    return fake


@pytest.mark.asyncio
async def test_active_renderers_all_inactive(backend):
    # go-librespot says stopped, busctl returns nothing for AirPlay,
    # bluealsa-cli has no PCM.
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={"stopped": True, "paused": False, "track": None},
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await backend.active_renderers()
    assert result == {
        "aplactive": False,
        "btactive": False,
        "spotactive": False,
        "slactive": False,
        "rbactive": False,
    }


@pytest.mark.asyncio
async def test_active_renderers_spotify_playing(backend):
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={
            "stopped": False, "paused": False,
            "track": {"name": "X", "artist_names": ["Y"], "album_name": "Z"},
        },
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await backend.active_renderers()
    assert result["spotactive"] is True
    assert result["aplactive"] is False
    assert result["btactive"] is False


@pytest.mark.asyncio
async def test_active_renderers_bluetooth_playing(backend):
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={"stopped": True, "paused": False, "track": None},
    )
    fake_pcm = b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source\n"
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=fake_pcm),
    ):
        result = await backend.active_renderers()
    assert result["btactive"] is True
    assert result["spotactive"] is False


@pytest.mark.asyncio
async def test_active_renderers_resilient_to_go_librespot_down(backend):
    """If go-librespot HTTP is unreachable, _spot_active() returns
    False rather than raising — same fail-soft contract MoodeClient
    follows for SQLite."""
    import httpx
    backend._http.get = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        result = await backend.active_renderers()
    assert result["spotactive"] is False


# ----------------------------------------------------------------------
# DebianBackend.get_currentsong — cascade by active source
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_currentsong_spotify_extracts_track_metadata(backend):
    spot_response = {
        "stopped": False,
        "paused": False,
        "track": {
            "name": "Stay Lucky",
            "artist_names": ["Bachi"],
            "album_name": "It Was The Best",
            "uri": "spotify:track:6IiSsjuKiOIbOCSv10SqPn",
        },
    }
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(return_value=spot_response)
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await backend.get_currentsong()
    assert song["title"] == "Stay Lucky"
    assert song["artist"] == "Bachi"
    assert song["album"] == "It Was The Best"


@pytest.mark.asyncio
async def test_currentsong_falls_through_to_mpd_when_no_source(backend):
    """When no Spotify, AirPlay, or BT is active, currentsong falls
    through to MPD. If MPD is also down, returns empty dict."""
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={"stopped": True, "paused": False, "track": None},
    )
    backend._mpd_call = AsyncMock(side_effect=ConnectionRefusedError())
    with patch(
        "asyncio.create_subprocess_exec",
        new=_mock_subprocess(stdout=b""),
    ):
        song = await backend.get_currentsong()
    assert song == {}


# ----------------------------------------------------------------------
# disable_renderer — per-source actions
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disable_renderer_spotify_calls_pause_endpoint(backend):
    backend._http.post = AsyncMock()
    await backend.disable_renderer("spotify")
    backend._http.post.assert_awaited_once_with("http://127.0.0.1:3678/player/pause")


@pytest.mark.asyncio
async def test_disable_renderer_airplay_calls_mpris_pause(backend):
    """Verify disable_renderer('airplay') invokes busctl with the
    Pause method on shairport-sync's MPRIS interface. We capture
    the args by wrapping create_subprocess_exec rather than
    replacing it with `new=`."""
    captured_args: list[tuple] = []
    fake = _mock_subprocess(returncode=0)

    async def capturing(*args, **kwargs):
        captured_args.append(args)
        return await fake(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", side_effect=capturing):
        await backend.disable_renderer("airplay")

    assert captured_args, "create_subprocess_exec was not called"
    args = captured_args[0]
    assert "busctl" in args[0]
    assert "Pause" in args
    assert "org.mpris.MediaPlayer2.ShairportSync" in args


@pytest.mark.asyncio
async def test_disable_renderer_bluetooth_is_noop(backend, caplog):
    """No clean pause API for bluez-alsa A2DP sink. Method returns
    cleanly; spotify_routing handles fallback."""
    await backend.disable_renderer("bluetooth")
    # No exception; method logs and returns.


# ----------------------------------------------------------------------
# MPRIS metadata parser — tested with the actual busctl output we
# captured from shairport-sync during the migration on jts.local
# (see conversation 2026-05-06).
# ----------------------------------------------------------------------

def test_parse_mpris_metadata_real_shairport_output():
    sample = (
        'v a{sv} 5 "mpris:trackid" o "/org/gnome/ShairportSync/2BDA81CACBA82DDD" '
        '"xesam:title" s "PROSTITUTE" '
        '"xesam:album" s "PROSTITUTE" '
        '"xesam:artist" as 1 "Labrinth" '
        '"mpris:length" x 164610000'
    )
    parsed = _parse_mpris_metadata(sample)
    assert parsed["xesam:title"] == "PROSTITUTE"
    assert parsed["xesam:album"] == "PROSTITUTE"
    assert parsed["xesam:artist"] == ["Labrinth"]


def test_parse_mpris_metadata_multiple_artists():
    sample = '"xesam:artist" as 2 "Daft Punk" "Pharrell Williams"'
    parsed = _parse_mpris_metadata(sample)
    assert parsed["xesam:artist"] == ["Daft Punk", "Pharrell Williams"]


def test_parse_mpris_metadata_empty_input():
    assert _parse_mpris_metadata("") == {}
    assert _parse_mpris_metadata("v s \"random\"") == {}


# ----------------------------------------------------------------------
# Edge cases — make sure failure modes don't crash the cascade
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_renderers_when_busctl_missing(backend):
    """If busctl can't be found (FileNotFoundError), _airplay_playing
    should return False rather than propagating. Same contract as the
    other probes."""
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={"stopped": True, "paused": False, "track": None},
    )
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("busctl not found"),
    ):
        result = await backend.active_renderers()
    # All probes return False on FileNotFoundError; nothing crashes.
    assert result["aplactive"] is False
    assert result["btactive"] is False


@pytest.mark.asyncio
async def test_currentsong_airplay_returns_metadata(backend):
    """When AirPlay is the active source and shairport-sync's MPRIS
    has metadata, currentsong should populate title/album/artist
    from the parsed busctl output."""
    # spotactive=False, aplactive=True
    backend._http.get = AsyncMock()
    backend._http.get.return_value.raise_for_status = MagicMock()
    backend._http.get.return_value.json = MagicMock(
        return_value={"stopped": True, "paused": False, "track": None},
    )

    sample_mpris = (
        'v a{sv} 4 "mpris:trackid" o "/foo" '
        '"xesam:title" s "Bohemian Rhapsody" '
        '"xesam:album" s "A Night at the Opera" '
        '"xesam:artist" as 1 "Queen"'
    )

    async def fake_subproc(*args, **kwargs):
        # First call: bluealsa-cli list-pcms (BT not active)
        # Second call: busctl Get PlaybackStatus (returns "Playing")
        # Third call: busctl Get Metadata (returns the sample)
        proc = MagicMock()
        proc.returncode = 0
        if "bluealsa-cli" in args:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        elif "PlaybackStatus" in args:
            proc.communicate = AsyncMock(return_value=(b'v s "Playing"\n', b""))
        elif "Metadata" in args:
            proc.communicate = AsyncMock(
                return_value=(sample_mpris.encode(), b""),
            )
        else:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subproc):
        song = await backend.get_currentsong()

    assert song["title"] == "Bohemian Rhapsody"
    assert song["album"] == "A Night at the Opera"
    assert song["artist"] == "Queen"
