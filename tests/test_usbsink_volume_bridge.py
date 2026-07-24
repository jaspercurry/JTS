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
    USBSINK_VOLUME_DB_MAX,
    USBSINK_VOLUME_DB_MIN,
    USBSINK_VOLUME_STEP_DB,
    VolumeBridge,
    VolumeBridgeUnavailable,
)

ROOT = Path(__file__).resolve().parents[1]

# Hardware-real advertised range: the kernel reports the UAC2 volume control
# as a 0-based STEP INDEX (u_audio_volume_info: min=0, max=step-count) plus a
# DB_MINMAX TLV giving the physical dB endpoints. For our -50..0 dB / 1 dB-step
# advertised range that is 0..50 with a -50.00..0.00 dB TLV.
IDX_MIN = 0
IDX_MAX = 50
TLV_DB_MIN = -50.0
TLV_DB_MAX = 0.0


def _set_range(bridge: VolumeBridge) -> None:
    """Put a bridge into the hardware-real post-discovery state: a 0..50 step
    index range with a -50..0 dB physical scale (as if read from the TLV)."""
    bridge._vol_min = IDX_MIN
    bridge._vol_max = IDX_MAX
    bridge._db_min = TLV_DB_MIN
    bridge._db_max = TLV_DB_MAX
    bridge._db_source = "tlv"


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


def _cget_volume(
    raw: int = 25,
    idx_min: int = IDX_MIN,
    idx_max: int = IDX_MAX,
    *,
    tlv: bool = True,
    tlv_min_db: float = TLV_DB_MIN,
    tlv_max_db: float = TLV_DB_MAX,
) -> str:
    """Format an `amixer cget` for the PCM Capture Volume.

    Mirrors the real u_audio shape: min/max are STEP INDICES (min=0), the
    value is a step index, and (by default) a decoded DB_MINMAX TLV line
    carries the physical dB endpoints.
    """
    out = (
        f"numid=1,iface=MIXER,name='PCM Capture Volume'\n"
        f"  ; type=INTEGER,access=rw---R--,values=1,"
        f"min={idx_min},max={idx_max},step=1\n"
        f"  : values={raw}\n"
    )
    if tlv:
        out += f"  | dBminmax-min={tlv_min_db:.2f}dB,max={tlv_max_db:.2f}dB\n"
    return out


def _cget_volume_stereo(left: int, right: int) -> str:
    return (
        "numid=1,iface=MIXER,name='PCM Capture Volume'\n"
        "  ; type=INTEGER,access=rw---R--,values=2,min=0,max=50,step=1\n"
        f"  : values={left},{right}\n"
        "  | dBminmax-min=-50.00dB,max=0.00dB\n"
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
    """When amixer lists both controls, _discover() populates numids,
    parses the STEP-INDEX range (min=0), and recovers the physical dB
    endpoints from the DB_MINMAX TLV."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = [
            _make_completed_process(CONTROLS_OUTPUT),
            _make_completed_process(_cget_volume(idx_min=0, idx_max=50)),
        ]
        bridge._discover()
    assert bridge._vol_numid == 1
    assert bridge._switch_numid == 2
    # ALSA reports a 0-based step index, NOT dB (kernel u_audio_volume_info).
    assert bridge._vol_min == 0
    assert bridge._vol_max == 50
    # Physical dB recovered from the DB_MINMAX TLV line.
    assert bridge._db_min == -50.0
    assert bridge._db_max == 0.0
    assert bridge._db_source == "tlv"


def test_discover_reconstructs_db_when_tlv_absent():
    """No decoded TLV line (older amixer / stripped kernel) → fall back to
    the advertised-range constants rather than guessing from the step index."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = [
            _make_completed_process(CONTROLS_OUTPUT),
            _make_completed_process(
                _cget_volume(idx_min=0, idx_max=50, tlv=False),
            ),
        ]
        bridge._discover()
    assert bridge._vol_min == 0
    assert bridge._vol_max == 50
    assert bridge._db_min == USBSINK_VOLUME_DB_MIN
    assert bridge._db_max == USBSINK_VOLUME_DB_MAX
    assert bridge._db_source == "reconstructed"


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
# _raw_to_pct() — step-index -> physical dB -> amplitude-domain % (#1698)
#
# HARDWARE-REAL inputs: the kernel reports a 0-based STEP INDEX (0..50),
# not dB. _raw_to_pct recovers physical dB from the index (linear across the
# TLV/reconstructed dB endpoints) then amplitude-normalizes (10**(dB/20)),
# because macOS maps its slider POSITION perceptually onto the dB range so a
# linear inverse over-reads the low half of the slider.
# ----------------------------------------------------------------------


def _old_linear_in_index_pct(index: int, idx_min: int, idx_max: int) -> int:
    """The pre-#1698 mapping, recomputed here so the direction assertions
    below are non-vacuous. The old code normalized the raw ALSA value
    linearly across [min,max]; fed a 0-based step index that is exactly
    linear-in-step (== linear-in-dB), which is what this replaces."""
    span = idx_max - idx_min
    return max(0, min(100, round((index - idx_min) / span * 100.0)))


def test_raw_to_pct_amplitude_endpoints():
    """(b) Endpoints are preserved: the min step index (0) maps to 0%, the
    max step index (50) maps to 100%."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert bridge._raw_to_pct(IDX_MIN) == 0    # -50 dB -> amp_min -> 0%
    assert bridge._raw_to_pct(IDX_MAX) == 100  # 0 dB -> amp_max -> 100%


def test_raw_to_pct_mid_step_reads_lower_than_old_linear():
    """(c) The dB midpoint step lands LOW in the amplitude domain, well below
    the old linear result for the same step. Step 25 -> -25 dB -> ~5%
    amplitude, where the old linear-in-step curve read 50%. dB is logarithmic,
    so -25 dB is acoustically quiet and must map low. The range narrowing (to
    -50..0) is what then lets a real mid-slider land near mid on-Mac."""
    bridge = VolumeBridge()
    _set_range(bridge)
    step = 25  # dB midpoint of the 0..50 step range -> -25 dB

    new_pct = bridge._raw_to_pct(step)
    old_pct = _old_linear_in_index_pct(step, IDX_MIN, IDX_MAX)

    assert old_pct == 50  # pin the old behavior so this stays non-vacuous
    assert new_pct < old_pct
    assert new_pct <= 10  # amplitude domain: 10**(-25/20) ≈ 5.6%


def test_raw_to_pct_reads_step_index_as_db_not_centidb():
    """Guard against the reverted-units bug (adversarial review B2). The ALSA
    value is a 0-based STEP INDEX, not centi-dB; physical dB must be recovered
    (index -> dB across the -50..0 endpoints) BEFORE the amplitude step. If a
    step index were instead amplitude-normalized as if it were centi-dB
    (dB = raw*0.01, over a ~0.5 dB span with min index 0), the curve would
    collapse to ~linear and step 25 would read ~50%. Correct handling reads
    step 25 as -25 dB -> ~5%."""
    bridge = VolumeBridge()
    _set_range(bridge)

    # Reproduce the buggy centi-dB reading to prove the two differ sharply.
    def _buggy_centidb_pct(index: int) -> int:
        def amp(raw: int) -> float:
            return 10.0 ** ((raw * 0.01) / 20.0)
        lo, hi = amp(bridge._vol_min), amp(bridge._vol_max)
        return max(0, min(100, round((amp(index) - lo) / (hi - lo) * 100.0)))

    assert _buggy_centidb_pct(25) >= 40   # the bug reads a step index ~linear
    assert bridge._raw_to_pct(25) <= 10   # correct dB recovery reads it low


def test_raw_to_pct_clamps_out_of_range_values():
    """Defensive: if amixer reports a step outside the declared range
    (unusual but possible during a transient), clamp to [0, 100]."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert bridge._raw_to_pct(-10) == 0   # below step 0 (< -50 dB)
    assert bridge._raw_to_pct(60) == 100  # above step 50 (> 0 dB)


def test_raw_to_pct_degenerate_range_returns_50():
    """If somehow max == min, return 50 — sane default avoids
    division-by-zero crash and keeps the daemon running."""
    bridge = VolumeBridge()
    bridge._vol_min = 50
    bridge._vol_max = 50
    assert bridge._raw_to_pct(50) == 50


# ----------------------------------------------------------------------
# Advertised UAC2 capture-volume range — static-writer contract (a).
#
# The two ends of the volume-curve contract live in different languages:
#   - deploy/usbsink/jasper-usbgadget-up advertises the -50..0 dB range
#     to the host (so macOS tapers its slider over it), in configfs 1/256-dB
#     units;
#   - volume_bridge recovers physical dB + amplitude-normalizes.
# They can't share code, so this pins the advertised 1/256-dB literals the
# way tests/test_wifi_profile_hardening_contract.py pins the NM hardening set.
# The SINGLE source of truth is the USBSINK_VOLUME_DB_* Python constants; the
# bash literals are DERIVED here as round(const * 256) so the two ends cannot
# drift.
# ----------------------------------------------------------------------

USBGADGET_UP = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-up"

_UAC2_DB_UNITS_PER_DB = 256  # UAC2 volume unit is 1/256 dB (kernel u_audio)

# Derived from the Python single-source-of-truth constants, in 1/256 dB.
REQUIRED_VOLUME_ATTRS = [
    ("c_volume_min", str(round(USBSINK_VOLUME_DB_MIN * _UAC2_DB_UNITS_PER_DB))),
    ("c_volume_max", str(round(USBSINK_VOLUME_DB_MAX * _UAC2_DB_UNITS_PER_DB))),
    ("c_volume_res", str(round(USBSINK_VOLUME_STEP_DB * _UAC2_DB_UNITS_PER_DB))),
]


def test_advertised_range_constants_are_1_over_256_db():
    """Pin B1: configfs c_volume_* is 1/256 dB, so a -50..0 dB / 1 dB-step
    range is -12800/0/256. Also pin the kernel's bind-time validity rule
    (max-min) % res == 0 (12800 % 256 == 0 -> 50 steps)."""
    vmin = round(USBSINK_VOLUME_DB_MIN * _UAC2_DB_UNITS_PER_DB)
    vmax = round(USBSINK_VOLUME_DB_MAX * _UAC2_DB_UNITS_PER_DB)
    vres = round(USBSINK_VOLUME_STEP_DB * _UAC2_DB_UNITS_PER_DB)
    assert (vmin, vmax, vres) == (-12800, 0, 256)
    assert (vmax - vmin) % vres == 0


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
            f"(the -50..0 dB range, in 1/256 dB, that pairs with _raw_to_pct)"
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
    _set_range(bridge)

    # Patch the subprocess-backed reads to return non-zero vol + muted.
    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: 20)
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
    _set_range(bridge)

    # A steady max-step read (index 50 -> 0 dB) maps to 100%.
    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: IDX_MAX)
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
    _set_range(bridge)

    monkeypatch.setattr(bridge, "_read_int_value", lambda numid: None)
    posted = []

    async def _fake_post(pct: int) -> None:
        posted.append(pct)

    monkeypatch.setattr(bridge, "_post", _fake_post)
    await bridge._tick()
    assert posted == []
