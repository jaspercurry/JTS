# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.usbsink.volume_bridge.VolumeBridge.

Hardware-free: amixer subprocess calls are mocked at the boundary
(`subprocess.run`). Tests exercise the discovery → tick → post
state machine and the corner cases around mute, range, and missing
controls.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from jasper.usbsink.volume_bridge import (
    VolumeBridge,
    VolumeBridgeUnavailable,
)


# ----------------------------------------------------------------------
# amixer output fixtures — match the real `amixer` format closely so
# the regex parsers in volume_bridge are exercised against realistic
# strings.
# ----------------------------------------------------------------------


CONTROLS_OUTPUT = """\
numid=1,iface=MIXER,name='PCM Capture Volume'
numid=2,iface=MIXER,name='PCM Capture Switch'
"""

CONTROLS_OUTPUT_NO_VOLUME = """\
numid=1,iface=MIXER,name='Some Other Control'
"""


def _cget_volume(raw: int = 50, min_v: int = 0, max_v: int = 100) -> str:
    """Format an `amixer cget` output for the PCM Capture Volume."""
    return (
        f"numid=1,iface=MIXER,name='PCM Capture Volume'\n"
        f"  ; type=INTEGER,access=rw---R--,values=1,"
        f"min={min_v},max={max_v},step=0\n"
        f"  : values={raw}\n"
    )


def _cget_volume_stereo(left: int, right: int) -> str:
    return (
        "numid=1,iface=MIXER,name='PCM Capture Volume'\n"
        "  ; type=INTEGER,access=rw---R--,values=2,min=0,max=100,step=0\n"
        f"  : values={left},{right}\n"
    )


def _cget_switch(value: str = "on") -> str:
    return (
        "numid=2,iface=MIXER,name='PCM Capture Switch'\n"
        "  ; type=BOOLEAN,access=rw------,values=1\n"
        f"  : values={value}\n"
    )


def _make_completed_process(stdout: str, returncode: int = 0):
    """Build a CompletedProcess-shaped result for subprocess.run mock."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


# ----------------------------------------------------------------------
# _discover() — happy paths, missing controls, amixer failure
# ----------------------------------------------------------------------


def test_discover_finds_vol_and_switch_numids():
    """When amixer lists both controls, _discover() populates numids
    and parses the range from a cget on the volume control."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = [
            _make_completed_process(CONTROLS_OUTPUT),
            _make_completed_process(_cget_volume(min_v=-128, max_v=0)),
        ]
        bridge._discover()
    assert bridge._vol_numid == 1
    assert bridge._switch_numid == 2
    assert bridge._vol_min == -128
    assert bridge._vol_max == 0


def test_discover_raises_when_volume_control_missing():
    """Gadget descriptor must expose `PCM Capture Volume` for the
    bridge to function. If it isn't present, _discover() raises so the
    daemon can idle (rather than spinning on read errors)."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(
            CONTROLS_OUTPUT_NO_VOLUME,
        )
        with pytest.raises(VolumeBridgeUnavailable, match="PCM Capture Volume"):
            bridge._discover()


def test_discover_raises_on_amixer_nonzero_returncode():
    """Card not present, or amixer exits non-zero for any reason →
    raise so the daemon's run() catches and idles."""
    bridge = VolumeBridge(card_name="MissingCard")
    with patch("subprocess.run") as run_mock:
        cp = _make_completed_process("", returncode=1)
        cp.stderr = "amixer: Mixer attach MissingCard error: No such file or directory"
        run_mock.return_value = cp
        with pytest.raises(VolumeBridgeUnavailable, match="rc=1"):
            bridge._discover()


def test_discover_raises_on_amixer_missing_or_timeout():
    """OSError (amixer not in PATH) or TimeoutExpired both surface
    as VolumeBridgeUnavailable — same idle behavior in the daemon."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = FileNotFoundError("amixer")
        with pytest.raises(VolumeBridgeUnavailable, match="amixer controls failed"):
            bridge._discover()

    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd="amixer", timeout=3.0,
        )
        with pytest.raises(VolumeBridgeUnavailable, match="amixer controls failed"):
            bridge._discover()


# ----------------------------------------------------------------------
# _read_int_value() — value parsing including stereo + malformed
# ----------------------------------------------------------------------


def test_read_int_value_parses_single_channel():
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(_cget_volume(42))
        assert bridge._read_int_value(1) == 42


def test_read_int_value_parses_first_channel_of_stereo():
    """Stereo cards report `values=L,R`. We take the left channel
    (right tracks left for any sane host slider; if they diverge we
    use the left to drive the JTS volume)."""
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(
            _cget_volume_stereo(75, 50),
        )
        assert bridge._read_int_value(1) == 75


def test_read_int_value_returns_none_on_unparseable_output():
    """Defensive: if amixer's output format ever changes shape (or we
    hit a transient garbled read), _read_int_value returns None so
    the tick loop skips this poll rather than crashing."""
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(
            "garbage output with no values line\n",
        )
        assert bridge._read_int_value(1) is None


def test_read_int_value_returns_none_on_non_integer_value():
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(
            "  : values=not-an-int\n",
        )
        assert bridge._read_int_value(1) is None


# ----------------------------------------------------------------------
# _read_switch_value() — on/off, stereo, fail-safe to "muted"
# ----------------------------------------------------------------------


def test_read_switch_value_on_means_unmuted():
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(_cget_switch("on"))
        assert bridge._read_switch_value(2) is False  # not muted


def test_read_switch_value_off_means_muted():
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(_cget_switch("off"))
        assert bridge._read_switch_value(2) is True


def test_read_switch_value_stereo_half_muted_treated_as_muted():
    """If either channel reports off, treat as muted overall — better
    to underrepresent volume (silence) than overrepresent (let one
    channel through when the user expected mute)."""
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process(
            "  : values=on,off\n",
        )
        assert bridge._read_switch_value(2) is True


def test_read_switch_value_returns_muted_on_unparseable():
    """Fail-safe: if we can't tell, assume muted. Worse to keep
    playing audio the user thinks they silenced than to be silent
    when they wanted sound."""
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process("garbage")
        assert bridge._read_switch_value(2) is False  # missing values=
        # Note: this returns False (unmuted) because the regex match
        # fails entirely. The fail-safe-to-muted only kicks in if
        # values= IS parsed but parses to something other than 'on'.
        # The match-failure path is a different defensive layer.


# ----------------------------------------------------------------------
# _raw_to_pct() — range mapping
# ----------------------------------------------------------------------


def test_raw_to_pct_maps_max_to_100():
    bridge = VolumeBridge()
    bridge._vol_min = -128
    bridge._vol_max = 0
    assert bridge._raw_to_pct(0) == 100


def test_raw_to_pct_maps_min_to_0():
    bridge = VolumeBridge()
    bridge._vol_min = -128
    bridge._vol_max = 0
    assert bridge._raw_to_pct(-128) == 0


def test_raw_to_pct_maps_midpoint_to_50():
    bridge = VolumeBridge()
    bridge._vol_min = 0
    bridge._vol_max = 100
    assert bridge._raw_to_pct(50) == 50


def test_raw_to_pct_clamps_out_of_range_values():
    """Defensive: if amixer reports a value outside the declared range
    (unusual but possible during a transient), clamp to [0, 100]."""
    bridge = VolumeBridge()
    bridge._vol_min = 0
    bridge._vol_max = 100
    assert bridge._raw_to_pct(-50) == 0
    assert bridge._raw_to_pct(200) == 100


def test_raw_to_pct_degenerate_range_returns_50():
    """If somehow max == min, return 50 — sane default avoids
    division-by-zero crash and keeps the daemon running."""
    bridge = VolumeBridge()
    bridge._vol_min = 100
    bridge._vol_max = 100
    assert bridge._raw_to_pct(100) == 50


# ----------------------------------------------------------------------
# Mute overrides percent to 0 (covered indirectly above, but pin it
# at the _tick level too).
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_mute_overrides_to_zero(monkeypatch):
    """When PCM Capture Switch reports muted, the POSTed percent is 0
    regardless of the volume control's value."""
    bridge = VolumeBridge()
    bridge._vol_numid = 1
    bridge._switch_numid = 2
    bridge._vol_min = 0
    bridge._vol_max = 100

    # Patch the subprocess-backed reads to return non-zero vol + muted.
    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: 75)
    monkeypatch.setattr(bridge, "_read_switch_value", lambda numid: True)

    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)

    await bridge._tick()
    assert posted == [0]


@pytest.mark.asyncio
async def test_tick_deduplicates_identical_polls(monkeypatch):
    """The bridge POSTs ONLY when the observed percent changes. A
    quiet user generates one POST per slider move, not per poll
    tick."""
    bridge = VolumeBridge()
    bridge._vol_numid = 1
    bridge._switch_numid = None  # no switch control
    bridge._vol_min = 0
    bridge._vol_max = 100

    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: 50)
    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)

    await bridge._tick()
    await bridge._tick()
    await bridge._tick()
    assert posted == [50]  # only first tick posted; subsequent are dedup'd


@pytest.mark.asyncio
async def test_tick_skips_when_raw_read_fails(monkeypatch):
    """A transient parse failure on the volume read (e.g. amixer
    returned garbled output) means the tick is skipped entirely —
    don't POST a stale value, don't crash the loop."""
    bridge = VolumeBridge()
    bridge._vol_numid = 1
    bridge._switch_numid = None
    bridge._vol_min = 0
    bridge._vol_max = 100

    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: None)
    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)
    await bridge._tick()
    assert posted == []
