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

import asyncio
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jasper.usbsink.volume_bridge import (
    VolumeBridge,
    VolumeBridgeUnavailable,
)

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.asyncio
async def test_run_retries_discovery_after_transient_mixer_miss():
    bridge = VolumeBridge(
        card_name="UAC2Gadget",
        poll_interval_sec=60.0,
        discovery_retry_interval_sec=0.01,
    )
    calls = 0

    def discover() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VolumeBridgeUnavailable("not enumerated yet")

    with patch.object(bridge, "_discover", side_effect=discover):
        task = asyncio.create_task(bridge.run())
        try:
            for _ in range(20):
                if calls >= 2:
                    break
                await asyncio.sleep(0.01)
            assert calls >= 2
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

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
    """Two distinct fail-safe layers, pinned here: a totally unparseable
    amixer read (no ``values=`` at all) fails OPEN (returns False/unmuted)
    because the regex match fails entirely — that's this test's assertion.
    The fail-safe-TO-MUTED layer is different: it only kicks in once
    ``values=`` IS parsed but parses to something other than 'on'."""
    bridge = VolumeBridge()
    with patch("subprocess.run") as run_mock:
        run_mock.return_value = _make_completed_process("garbage")
        assert bridge._read_switch_value(2) is False  # missing values=


# ----------------------------------------------------------------------
# _raw_to_pct() — amplitude-domain range mapping (issue #1698)
#
# The gadget advertises -50..0 dB (centi-dB units -5000..0), so the raw
# read is dB*100. The curve converts that dB to a linear amplitude and
# normalizes THAT (10**(dB/20)) rather than normalizing linearly in dB —
# because macOS maps its slider POSITION perceptually onto the dB range,
# so a linear-in-dB inverse over-reads the low half of the slider.
# ----------------------------------------------------------------------


def _old_linear_in_db_pct(raw: int, vol_min: int, vol_max: int) -> int:
    """The pre-#1698 mapping, recomputed here so the direction assertions
    below are non-vacuous — they compare the new amplitude curve against
    the exact linear-in-dB math it replaced."""
    span = vol_max - vol_min
    return max(0, min(100, round((raw - vol_min) / span * 100.0)))


def test_raw_to_pct_amplitude_endpoints():
    """(b) Endpoints are preserved by the amplitude normalization: the
    raw value at vol_max maps to 100, at vol_min maps to 0."""
    bridge = VolumeBridge()
    bridge._vol_min = -5000  # -50 dB (centi-dB)
    bridge._vol_max = 0      # 0 dB
    assert bridge._raw_to_pct(0) == 100
    assert bridge._raw_to_pct(-5000) == 0


def test_raw_to_pct_mid_db_reads_lower_than_old_linear_in_db():
    """(c) A mid-range dB read lands LOW in the amplitude domain, well
    below the old linear-in-dB result for the same dB. Over -50..0 dB a
    -35 dB read (raw -3500) is ~1-2% amplitude, where the old curve read
    ~30% — dB is logarithmic, so -35 dB is acoustically quiet and must
    map low. This is the whole point of the fix; the range narrowing (to
    -50..0) is what then lets a real mid-slider land near mid on-Mac."""
    bridge = VolumeBridge()
    bridge._vol_min = -5000
    bridge._vol_max = 0
    raw = -3500  # -35 dB

    new_pct = bridge._raw_to_pct(raw)
    old_pct = _old_linear_in_db_pct(raw, bridge._vol_min, bridge._vol_max)

    assert old_pct == 30  # pin the old behavior so this stays non-vacuous
    assert new_pct < old_pct
    assert new_pct <= 5  # amplitude domain: 10**(-35/20) ≈ 1.8%


def test_raw_to_pct_range_midpoint_is_not_fifty():
    """The dB midpoint of the range no longer maps to 50% (it did under
    the old linear-in-dB curve). At -25 dB (the midpoint of -50..0) the
    amplitude read is ~5.6%, far below the old linear 50%."""
    bridge = VolumeBridge()
    bridge._vol_min = -5000
    bridge._vol_max = 0
    mid_raw = -2500  # -25 dB, the dB midpoint of the range
    assert _old_linear_in_db_pct(mid_raw, -5000, 0) == 50
    assert bridge._raw_to_pct(mid_raw) < 20


def test_raw_to_pct_clamps_out_of_range_values():
    """Defensive: if amixer reports a value outside the declared range
    (unusual but possible during a transient), clamp to [0, 100]."""
    bridge = VolumeBridge()
    bridge._vol_min = -5000
    bridge._vol_max = 0
    assert bridge._raw_to_pct(-6000) == 0   # below -50 dB
    assert bridge._raw_to_pct(500) == 100   # above 0 dB


def test_raw_to_pct_degenerate_range_returns_50():
    """If somehow max == min, return 50 — sane default avoids
    division-by-zero crash and keeps the daemon running."""
    bridge = VolumeBridge()
    bridge._vol_min = 100
    bridge._vol_max = 100
    assert bridge._raw_to_pct(100) == 50


# ----------------------------------------------------------------------
# Advertised UAC2 capture-volume range — static-writer contract (a).
#
# The two ends of the volume-curve contract live in different languages:
#   - deploy/usbsink/jasper-usbgadget-up advertises the -50..0 dB range
#     to the host (so macOS tapers its slider over it);
#   - volume_bridge._raw_to_pct amplitude-normalizes the observed dB.
# They can't share code, so this pins the advertised centi-dB values the
# way tests/test_wifi_profile_hardening_contract.py pins the NM hardening
# set. If the range changes in the script, update _CENTI_DB_PER_UNIT's
# assumptions + this contract together.
# ----------------------------------------------------------------------

USBGADGET_UP = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-up"

# centi-dB (1/100 dB): -5000 = -50 dB floor, 0 = 0 dB ceiling, 100 = 1 dB step.
REQUIRED_VOLUME_ATTRS = [
    ("c_volume_min", "-5000"),
    ("c_volume_max", "0"),
    ("c_volume_res", "100"),
]


def _script_body_no_comments(path) -> str:
    """Script text with comment lines stripped + whitespace collapsed, so
    prose that names the attrs can't false-pass the value assertions."""
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return re.sub(r"\s+", " ", "\n".join(lines))


def test_usbgadget_advertises_narrow_capture_volume_range():
    body = _script_body_no_comments(USBGADGET_UP)
    for attr, value in REQUIRED_VOLUME_ATTRS:
        needle = f"functions/uac2.usb0/{attr} {value}"
        assert needle in body, (
            f"jasper-usbgadget-up must advertise `{needle}` "
            f"(the -50..0 dB range that pairs with _raw_to_pct)"
        )


def test_usbgadget_volume_range_writes_are_best_effort():
    """The range writes must go through write_if_present (guarded on the
    attr existing) so an older kernel that lacks c_volume_min/max/res does
    not fail gadget bring-up — the kernel default range applies instead."""
    body = _script_body_no_comments(USBGADGET_UP)
    for attr, _value in REQUIRED_VOLUME_ATTRS:
        assert f"write_if_present functions/uac2.usb0/{attr}" in body, (
            f"{attr} must be written best-effort via write_if_present"
        )


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
    bridge._vol_min = -5000
    bridge._vol_max = 0

    # Patch the subprocess-backed reads to return non-zero vol + muted.
    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: -500)
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
    bridge._vol_min = -5000
    bridge._vol_max = 0

    # A steady 0 dB read (raw 0) maps to 100% under the amplitude curve.
    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: 0)
    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)

    await bridge._tick()
    await bridge._tick()
    await bridge._tick()
    assert posted == [100]  # only first tick posted; subsequent are dedup'd


@pytest.mark.asyncio
async def test_tick_skips_when_raw_read_fails(monkeypatch):
    """A transient parse failure on the volume read (e.g. amixer
    returned garbled output) means the tick is skipped entirely —
    don't POST a stale value, don't crash the loop."""
    bridge = VolumeBridge()
    bridge._vol_numid = 1
    bridge._switch_numid = None
    bridge._vol_min = -5000
    bridge._vol_max = 0

    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: None)
    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)
    await bridge._tick()
    assert posted == []
