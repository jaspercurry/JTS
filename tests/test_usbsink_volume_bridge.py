# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.usbsink.volume_bridge.VolumeBridge.

Hardware-free: discovery's amixer subprocess calls are mocked at the
boundary (`subprocess.run`) and the Linux-only `alsaaudio` binding is
injected as a fake module. Tests exercise the discovery → mixer event →
post state machine and the corner cases around mute, range, and missing
controls.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from jasper.usbsink.volume_bridge import (
    MIXER_ELEMENT_NAME,
    POST_RETRY_BACKOFF_FACTOR,
    POST_RETRY_CEILING_SEC,
    POST_RETRY_INTERVAL_SEC,
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
# Fake `alsaaudio` binding. The real one is Linux-only and needs a live
# UAC2 gadget card; the shapes below are the ones jts3 reports (see
# test_raw_step_index_matches_pyalsaaudio_normalized_percent).
# ----------------------------------------------------------------------


class _FakeMixer:
    """One simple-mixer element backed by a real pipe, so the bridge's
    `loop.add_reader` on `polldescriptors()` is exercised for real.

    `playback` models the merged "PCM" element's playback half, present
    whenever the USB mic export is on. It sits above the 0..50 capture span
    (IDX_MAX) so a getvolume() call that forgets `pcmtype` is caught rather
    than coincidentally matching the capture value (#4209).
    """

    def __init__(self, raw: int = 41, *, playback: int | None = 80) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.raw = raw
        self.playback = playback
        self.rec = [1]
        self.fail: BaseException | None = None
        self.handled = 0
        self.closed = False

    def polldescriptors(self):
        return [(self._read_fd, 41)]

    def handleevents(self) -> int:
        self.handled += 1
        os.read(self._read_fd, 4096)
        if self.fail is not None:
            raise self.fail
        return 1

    def getvolume(self, pcmtype=None, units=None):
        if self.fail is not None:
            raise self.fail
        # Mirrors pyalsaaudio: unqualified getvolume() resolves to the
        # PLAYBACK half whenever the element has one.
        if pcmtype is None and self.playback is not None:
            return [self.playback]
        return [self.raw]

    def getrec(self):
        return list(self.rec)

    def close(self) -> None:
        self.closed = True
        os.close(self._read_fd)
        os.close(self._write_fd)

    # --- test-side driver -------------------------------------------------
    def emit(self, raw: int | None = None) -> None:
        """Make the control FD readable, as a host slider move would."""
        if raw is not None:
            self.raw = raw
        os.write(self._write_fd, b"x")


def _fake_alsaaudio(*mixers: _FakeMixer) -> ModuleType:
    module = ModuleType("alsaaudio")
    module.VOLUME_UNITS_RAW = 1  # type: ignore[attr-defined]
    # Matches real pyalsaaudio 0.11 (SND_PCM_STREAM_{PLAYBACK,CAPTURE}).
    module.PCM_PLAYBACK = 0  # type: ignore[attr-defined]
    module.PCM_CAPTURE = 1  # type: ignore[attr-defined]
    module.cards = lambda: ["UMIK2", "UAC2Gadget"]  # type: ignore[attr-defined]
    module.card_indexes = lambda: [0, 4]  # type: ignore[attr-defined]
    pending = list(mixers)

    def _mixer(control, cardindex):
        assert control == MIXER_ELEMENT_NAME
        assert cardindex == 4
        return pending.pop(0)

    module.Mixer = _mixer  # type: ignore[attr-defined]
    return module


def _ready_bridge(*mixers: _FakeMixer, **kwargs) -> VolumeBridge:
    """A bridge past discovery, wired to fake mixers."""
    bridge = VolumeBridge(
        card_name="UAC2Gadget",
        alsaaudio_module=_fake_alsaaudio(*mixers),
        **kwargs,
    )
    bridge._vol_numid = 3
    bridge._switch_numid = 2
    _set_range(bridge)
    return bridge


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


async def test_run_retries_discovery_after_transient_mixer_miss(monkeypatch):
    mixer = _FakeMixer()
    bridge = _ready_bridge(mixer, discovery_retry_interval_sec=0.01)
    calls = 0

    def discover() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VolumeBridgeUnavailable("not enumerated yet")

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        return True

    monkeypatch.setattr(bridge, "_post", _accept)
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


def test_missing_alsa_binding_degrades_to_discovery_retry():
    """A tier whose venv lacks the Linux-only binding must idle on the
    discovery retry loop, not crash-loop the unit under Restart=on-failure."""
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch(
        "jasper.usbsink.volume_bridge._load_alsaaudio",
        side_effect=ImportError("alsaaudio"),
    ):
        with pytest.raises(VolumeBridgeUnavailable):
            bridge._open_mixer()


async def test_mixer_reset_backs_off_before_rediscovery(monkeypatch):
    """A card that enumerates but whose reads always fail must not spin
    discovery: the reset pays the discovery retry interval, like any other
    failure. Without it each turn costs two `amixer` subprocesses and two log
    lines, as fast as the CPU allows."""
    broken, healthy = _FakeMixer(), _FakeMixer()
    broken.fail = OSError(5, "EIO")
    bridge = _ready_bridge(broken, healthy, discovery_retry_interval_sec=7.0)
    monkeypatch.setattr(bridge, "_discover", lambda: None)

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    task = asyncio.create_task(bridge.run())
    try:
        for _ in range(200):
            if bridge._mixer is healthy:
                break
            await real_sleep(0)
        assert bridge._mixer is healthy
        # The ONLY sleep between the broken mixer and its replacement.
        assert delays == [7.0]
        assert broken.closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_reopens_mixer_after_it_breaks(monkeypatch):
    """The gadget function re-enumerating under the process invalidates the
    control FDs. The bridge closes them and falls back into discovery instead
    of exiting the unit."""
    broken = _FakeMixer()
    healthy = _FakeMixer()
    bridge = _ready_bridge(broken, healthy, discovery_retry_interval_sec=0.01)
    discovers = 0

    def discover() -> None:
        nonlocal discovers
        discovers += 1

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        return True

    monkeypatch.setattr(bridge, "_post", _accept)
    with patch.object(bridge, "_discover", side_effect=discover):
        task = asyncio.create_task(bridge.run())
        try:
            for _ in range(20):
                if bridge._mixer is broken:
                    break
                await asyncio.sleep(0.01)
            broken.fail = OSError(9, "Bad file descriptor")
            broken.emit()
            for _ in range(50):
                if discovers >= 2:
                    break
                await asyncio.sleep(0.01)
            assert discovers >= 2
            assert broken.closed
            assert bridge._mixer is healthy
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def test_discover_raises_on_amixer_timeout():
    bridge = VolumeBridge(card_name="UAC2Gadget")
    with patch("subprocess.run") as run_mock:
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd="amixer", timeout=3.0,
        )
        with pytest.raises(VolumeBridgeUnavailable, match="amixer controls failed"):
            bridge._discover()


# ----------------------------------------------------------------------
# _raw_to_pct() — step-index -> visible macOS slider percent (#1698)
#
# HARDWARE-REAL inputs: the kernel reports a 0-based STEP INDEX (0..50),
# not dB. On the live Mac/UAC2 path, the normalized step fraction is the
# square root of the visible slider fraction, so _raw_to_pct squares it.
# ----------------------------------------------------------------------


def test_raw_to_pct_square_curve_endpoints():
    """Endpoints are preserved: the min step index (0) maps to 0%, the
    max step index (50) maps to 100%."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert bridge._raw_to_pct(IDX_MIN) == 0
    assert bridge._raw_to_pct(IDX_MAX) == 100


@pytest.mark.parametrize(
    ("step_index", "expected_pct"),
    [
        (18, 13),  # observed Mac 13%: (18/50)^2 = 12.96%
        (25, 25),  # observed Mac 25%: (25/50)^2 = 25%
        (35, 49),  # one quantized side of Mac 50%
        (36, 52),  # the other quantized side of Mac 50%
        (40, 64),  # observed screenshot: Mac 64%, formerly JTS 31%
        (43, 74),  # expected quantized Mac 75%
    ],
)
def test_raw_to_pct_inverts_observed_macos_step_transfer(
    step_index: int,
    expected_pct: int,
):
    """The square curve reproduces live Mac slider points and the expected
    one-step quantization around 50% and 75%."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert bridge._raw_to_pct(step_index) == expected_pct


def test_raw_to_pct_does_not_treat_physical_db_as_slider_percent():
    """The DB_MINMAX TLV proves the advertised physical scale, but physical
    amplitude is not the visible Mac slider. Step 40 is -10 dB: amplitude
    normalization produced the broken 31%, while the step inverse is 64%."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert bridge._raw_to_pct(40) == 64


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
#   - volume_bridge inverts macOS's observed square-root step transfer.
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
# Mixer events -> POST. The bridge reads only when the control FDs say the
# host moved something; nothing runs in between.
# ----------------------------------------------------------------------


async def _until(predicate, *, turns: int = 200) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _settle(turns: int = 200) -> None:
    """Give the loop plenty of turns without advancing wall time, so a
    would-be poller or an immediate retry would have shown itself."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_mixer_event_posts_once_and_never_subprocesses(monkeypatch):
    """One host slider move produces exactly one POST, and an idle bridge
    spawns no processes at all — the 4 Hz `amixer cget` pair (two forks per
    250 ms, ~700k forks/day on jts3) is gone."""
    mixer = _FakeMixer(raw=25)  # step 25/50 -> 25%
    bridge = _ready_bridge(mixer)
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)
    monkeypatch.setattr(bridge, "_discover", lambda: None)

    with patch("subprocess.run") as run_mock:
        task = asyncio.create_task(bridge.run())
        try:
            await _until(lambda: posted == [25])
            mixer.emit(40)  # step 40/50 -> 64%
            await _until(lambda: posted == [25, 64])
            await _settle()
            assert posted == [25, 64]
            assert mixer.handled == 1
            assert run_mock.call_count == 0
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_repeated_identical_observation_is_deduplicated(monkeypatch):
    """A mixer event that does not change the value POSTs nothing."""
    mixer = _FakeMixer(raw=IDX_MAX)
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    await bridge._observe()
    await bridge._observe()
    await bridge._observe()
    assert posted == [100]


@pytest.mark.parametrize("raw", [0, 18, 25, 43])
async def test_observe_reads_capture_volume_not_merged_playback_default(
    monkeypatch, raw,
):
    """The merged "PCM" simple element gets a playback half whenever the USB
    mic export is on, and pyalsaaudio's getvolume() defaults to PLAYBACK when
    the call omits `pcmtype` (#4209). The fake's playback value (80) sits
    above the whole capture span (0..50), so reading playback here would
    always clamp to 100% regardless of the capture step index — _observe()
    must publish the capture-derived percent instead."""
    mixer = _FakeMixer(raw=raw)
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    await bridge._observe()
    assert posted == [bridge._raw_to_pct(raw)]


async def test_mute_overrides_to_zero(monkeypatch):
    """When the capture switch reports muted, the POSTed percent is 0
    regardless of the volume control's value."""
    mixer = _FakeMixer(raw=20)
    mixer.rec = [0]
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    await bridge._observe()
    assert posted == [0]


async def test_observe_skips_when_mixer_reports_no_values(monkeypatch):
    """An empty read is skipped entirely — don't POST a stale value."""
    mixer = _FakeMixer()
    mixer.getvolume = lambda pcmtype=None, units=None: []  # type: ignore[method-assign]
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    await bridge._observe()
    assert posted == []


# ----------------------------------------------------------------------
# Declined observations. jasper-control declines while USB is not the active
# source (volume_coordinator's source gate) and while a measurement holds the
# fader. A host slider MOVE is re-presented on a capped backoff; the startup
# snapshot is not (measured on an idle jts3: 708 declined POSTs/hour that had
# nothing to publish).
# ----------------------------------------------------------------------


def test_ceiling_stays_within_handoff_latency_budget():
    # Bounds the USB source-handoff volume-mismatch window (a declined move
    # pinned at the ceiling, then an unattributed jump once the host starts
    # playback — see the prose above the constants in volume_bridge.py) under
    # the ~10 s household-perception budget that window must stay under.
    assert POST_RETRY_CEILING_SEC <= 10.0


async def test_declined_startup_snapshot_is_not_retried(monkeypatch):
    """A restarted bridge re-reads the mixer, but that snapshot predates any
    proof of a host action. Declined, it is dropped rather than re-presented
    forever; the next real move is retried normally."""
    mixer = _FakeMixer(raw=41)  # step 41/50 -> 67%
    bridge = _ready_bridge(mixer)
    posted: list[tuple[int, bool]] = []

    async def _decline(pct: int, *, initial: bool = False) -> bool:
        posted.append((pct, initial))
        return False

    monkeypatch.setattr(bridge, "_post", _decline)
    monkeypatch.setattr(bridge, "_discover", lambda: None)

    task = asyncio.create_task(bridge.run())
    try:
        await _until(lambda: posted == [(67, True)])
        await _settle()
        assert posted == [(67, True)]
        assert bridge._retry_task is None

        mixer.emit(25)  # a real host move -> 25%
        await _until(lambda: len(posted) == 2)
        assert posted[1] == (25, False)
        assert bridge._retry_task is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_declined_host_move_retry_backoff_is_bounded(monkeypatch):
    """A declined host move is re-presented until accepted, on a capped
    exponential backoff: the true slider position lands within one
    ceiling-length window of the source going active, without an unbounded
    HTTP + mux IPC rate while it waits."""
    bridge = _ready_bridge(_FakeMixer())
    bridge._last_published_pct = 25  # a prior accepted value
    bridge._initial_observed_pct = 25  # so 64% is a MOVE, not the snapshot

    attempts = 0

    async def _decline_then_accept(pct: int, *, initial: bool = False) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts >= 8

    monkeypatch.setattr(bridge, "_post", _decline_then_accept)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await bridge._publish(64)
    assert bridge._retry_task is not None
    await bridge._retry_task

    expected: list[float] = []
    delay = POST_RETRY_INTERVAL_SEC
    for _ in range(len(delays)):
        expected.append(delay)
        delay = min(delay * POST_RETRY_BACKOFF_FACTOR, POST_RETRY_CEILING_SEC)
    assert delays == expected
    assert max(delays) == POST_RETRY_CEILING_SEC
    assert bridge._last_published_pct == 64


async def test_new_move_replaces_the_value_being_retried(monkeypatch):
    """A fresh slider move must not inherit the backoff of a now-stale value:
    it is attempted immediately and the old retry is dropped."""
    bridge = _ready_bridge(_FakeMixer())
    bridge._last_published_pct = 25
    bridge._initial_observed_pct = 25
    posted: list[int] = []

    async def _decline(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return False

    monkeypatch.setattr(bridge, "_post", _decline)

    await bridge._publish(64)
    stale = bridge._retry_task
    assert stale is not None
    await bridge._publish(36)

    assert posted == [64, 36]
    assert stale.cancelled()
    assert bridge._retry_task is not stale


async def test_marks_only_unchanged_startup_snapshot_as_initial(monkeypatch):
    """A bridge restart labels discovery state, but a later host move is
    intent — and only intent may clear a mute latched by another surface.
    A move BACK to the startup value is still intent: once the host has been
    seen moving the slider, nothing it does is discovery any more, and the
    value must be retried rather than silently dropped."""
    mixer = _FakeMixer(raw=40)
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    posted: list[tuple[int, bool]] = []

    async def _decline(pct: int, *, initial: bool = False) -> bool:
        posted.append((pct, initial))
        return False

    monkeypatch.setattr(bridge, "_post", _decline)

    await bridge._observe()
    assert bridge._retry_task is None  # snapshot: one attempt, no retry
    mixer.raw = 25
    await bridge._observe()
    mixer.raw = 40  # back to the startup value
    await bridge._observe()

    assert posted == [(64, True), (25, False), (64, False)]
    assert bridge._retry_task is not None


async def test_unchanged_mixer_event_does_not_disturb_a_pending_retry(
    monkeypatch,
):
    """A wake that carries no percent change must not cancel the in-flight
    retry and restart its backoff at the base interval."""
    mixer = _FakeMixer(raw=40)
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    bridge._last_observed_pct = 25
    bridge._host_moved = True
    posted: list[int] = []

    async def _decline(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return False

    monkeypatch.setattr(bridge, "_post", _decline)

    await bridge._observe()  # 25 -> 64, declined, retry armed
    armed = bridge._retry_task
    assert armed is not None
    await bridge._observe()  # same value: nothing to say
    assert posted == [64]
    assert bridge._retry_task is armed


async def test_observe_skips_capture_switch_when_the_card_has_none(monkeypatch):
    """`_discover` only requires the volume control. On a gadget descriptor
    without `c_mute_present` the simple-mixer element has no capture switch
    and `getrec()` raises, so it must not be called."""
    mixer = _FakeMixer(raw=40)

    def _no_switch():
        raise AssertionError("getrec() called on a card with no capture switch")

    mixer.getrec = _no_switch  # type: ignore[method-assign]
    bridge = _ready_bridge(mixer)
    bridge._switch_numid = None
    bridge._mixer = mixer
    posted: list[int] = []

    async def _accept(pct: int, *, initial: bool = False) -> bool:
        posted.append(pct)
        return True

    monkeypatch.setattr(bridge, "_post", _accept)

    await bridge._observe()
    assert posted == [64]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"percent": 31, "observation_applied": False}, False),
        ({"percent": 64, "observation_applied": True}, True),
        ({"percent": 64}, True),  # rolling upgrade: old control response
        ("not-a-mapping", None),  # 2xx that proves nothing
    ],
)
async def test_post_requires_application_acknowledgement(payload, expected):
    """A 2xx response only counts as delivered when jasper-control confirms
    that the source-active gate accepted the observation."""

    class _Response:
        ok = True
        status = 200

        def json(self):
            return payload

    class _Control:
        async def set_volume(
            self,
            pct,
            *,
            source,
            observation_initial=None,
        ):
            assert pct == 64
            assert source == "usbsink"
            assert observation_initial is False
            return _Response()

    bridge = VolumeBridge()
    bridge._control = _Control()

    assert await bridge._post(64) is expected


async def test_unanswered_startup_snapshot_is_retried(monkeypatch):
    """A DECLINED snapshot is dropped, but an UNANSWERED one is not: after a
    reboot the bridge's first POST can time out while jasper-control is still
    starting (observed on jts3), and the snapshot is then the only thing that
    will sync the host slider until the user next touches it."""
    mixer = _FakeMixer(raw=41)
    bridge = _ready_bridge(mixer)
    bridge._mixer = mixer
    attempts: list[bool] = []

    async def _no_answer(pct: int, *, initial: bool = False) -> bool | None:
        attempts.append(initial)
        return None

    monkeypatch.setattr(bridge, "_post", _no_answer)
    await bridge._observe()
    assert attempts == [True]
    assert bridge._retry_task is not None
    await bridge._cancel_retry_and_wait()


def test_raw_step_index_matches_pyalsaaudio_normalized_percent():
    """The bridge reads `getvolume(units=VOLUME_UNITS_RAW)` — the same 0-based
    step index `amixer cget` printed — so the volume curve is unchanged by the
    move off subprocesses. jts3 hardware, step 41 of 0..50: pyalsaaudio's
    DEFAULT (linear, normalized) percent view reports 82, which is NOT a
    listening level; the bridge's own curve maps that slider position to 67."""
    bridge = VolumeBridge()
    _set_range(bridge)
    assert round(41 * 100 / (IDX_MAX - IDX_MIN)) == 82
    assert bridge._raw_to_pct(41) == 67
