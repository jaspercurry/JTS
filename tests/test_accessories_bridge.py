# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tap counter state-machine coverage for the HID accessory bridge.

We exercise `_TapCounter` directly with a fake async poster so the test
runs in milliseconds (real-time `asyncio.sleep` calls aside, which we
trim by giving the action a small window_ms). No real network, no httpx
— the bridge now posts to jasper-control via the typed control client, and
the unit under test only depends on the poster callable's contract:
`async post(method, path, body) -> ControlResponse`.

The shape of `_TapCounter` matters: the bridge daemon depends on it
to translate every VK-01 click into the right transport action, and
the timing semantics (defer-on-single, immediate-on-triple) are the
whole point of the gesture.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from typing import List, Optional

import pytest

from jasper.accessories.bridge import (
    COALESCE_WINDOW_SEC, _Coalescer, _post_once, _read_device, _TapCounter,
)
from jasper.accessories.registry import Device, HoldAction, KeyAction, TapAction
from jasper.control.client import ControlError, ControlResponse


# Window short enough that tests finish quickly but long enough that
# the asyncio scheduler can resolve event ordering deterministically.
WINDOW_MS = 30
WINDOW_SEC = WINDOW_MS / 1000.0


def _recording_poster(calls: List[str]):
    """A poster that records every request's path into `calls`."""

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        calls.append(path)
        return ControlResponse(200, b"")

    return post


def _make_counter(post) -> tuple[_TapCounter, List[str]]:
    """Return a counter wired to `post`."""
    action = TapAction(
        on_single=KeyAction("POST", "/transport/toggle", {}),
        on_double=KeyAction("POST", "/transport/next", {}),
        on_triple=KeyAction("POST", "/transport/previous", {}),
        window_ms=WINDOW_MS,
    )
    tc = _TapCounter(
        post=post,
        action=action,
        device_name="fake",
        key_name="KEY_FAKE",
    )
    return tc, []


@pytest.mark.asyncio
async def test_single_tap_commits_toggle_after_window():
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    tc.hit()
    # Before window expires: nothing has fired.
    await asyncio.sleep(WINDOW_SEC / 2)
    assert calls == []
    # After window: toggle.
    await asyncio.sleep(WINDOW_SEC)
    assert calls == ["/transport/toggle"]


@pytest.mark.asyncio
async def test_double_tap_commits_next_after_window():
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    # Still no fire — the second tap reset the timer, not committed yet.
    assert calls == []
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/next"]


@pytest.mark.asyncio
async def test_triple_tap_commits_previous_immediately():
    """The third tap shouldn't wait another window — we know it's a
    triple at the moment it arrives because there's no quadruple-tap
    semantic. This is the whole point of "fire on count=3"."""
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    # One event loop tick for the immediate task to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Previous should fire immediately, not after the window.
    assert calls == ["/transport/previous"]
    # And no spurious additional fire when the window would have expired.
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/previous"]


@pytest.mark.asyncio
async def test_slow_double_tap_fires_two_toggles():
    """If the user double-taps but spaces them past the window, each
    tap commits independently as a play/pause toggle. Acceptable —
    the alternative is making the window arbitrarily long."""
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/toggle", "/transport/toggle"]


@pytest.mark.asyncio
async def test_sequential_independent_sequences():
    """After a sequence commits, a fresh tap starts a new sequence
    from count=1. The counter must reset fully between gestures."""
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    # Double-tap.
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    # Single tap.
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/next", "/transport/toggle"]


@pytest.mark.asyncio
async def test_quadruple_fires_previous_then_starts_new_sequence():
    """Four rapid taps: the first three fire previous immediately
    (count=3 is unambiguous), the fourth starts a new sequence at
    count=1 and commits toggle after the window."""
    calls: List[str] = []
    tc, _ = _make_counter(_recording_poster(calls))
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    # Yield so the immediate-fire task runs.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["/transport/previous"]
    # Fourth tap, tightly after.
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/previous", "/transport/toggle"]


@pytest.mark.asyncio
async def test_unmapped_tap_count_is_silent():
    """If a TapAction has no on_double, double-tap should silently
    no-op rather than fall through to single or triple."""
    calls: List[str] = []
    action = TapAction(
        on_single=KeyAction("POST", "/foo", {}),
        on_double=None,
        on_triple=KeyAction("POST", "/baz", {}),
        window_ms=WINDOW_MS,
    )
    tc = _TapCounter(_recording_poster(calls), action, "fake", "KEY_FAKE")
    tc.hit()
    await asyncio.sleep(WINDOW_SEC / 4)
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == []  # no on_double mapping → silent drop


@pytest.mark.asyncio
async def test_http_error_does_not_break_subsequent_taps():
    """If one dispatch fails (jasper-control down → ControlError), the
    counter must keep working for later taps. We've been bitten by "one
    failure poisons the supervisor" in async code before."""
    calls: List[str] = []
    state = {"fail_next": True}

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        calls.append(path)
        if state["fail_next"]:
            state["fail_next"] = False
            # jasper-control is down: the control client raises ControlError
            # when the localhost connect is refused. The bridge catches it
            # via `except ControlError`. Must not crash the reader task.
            raise ControlError("simulated: jasper-control down")
        return ControlResponse(200, b"")

    action = TapAction(
        on_single=KeyAction("POST", "/transport/toggle", {}),
        window_ms=WINDOW_MS,
    )
    tc = _TapCounter(post, action, "fake", "KEY_FAKE")
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    # First fired and was caught — call recorded, no exception.
    assert calls == ["/transport/toggle"]
    # Second works.
    tc.hit()
    await asyncio.sleep(WINDOW_SEC * 2)
    assert calls == ["/transport/toggle", "/transport/toggle"]


@pytest.mark.asyncio
async def test_post_once_failure_emits_canonical_event(caplog):
    """Pin the migrated knob.action.failed emit: the canonical
    log_event helper must render the device/key/err fields in logfmt,
    quoting the free-text error (spaces + colon force quoting)."""

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        raise ControlError("simulated: jasper-control down")

    action = KeyAction("POST", "/mic/mute", {})
    with caplog.at_level(logging.WARNING, logger="jasper.accessories.bridge"):
        await _post_once(post, action, "fake", "KEY_MUTE")
    assert caplog.records[-1].getMessage() == (
        'event=knob.action.failed device=fake key=KEY_MUTE '
        'err="simulated: jasper-control down"'
    )


# A device label is an untrusted, host-provided string (the kernel's
# device name, which a BT accessory can advertise with spaces). The
# `+`-signed coalesced delta is the most plausible byte-diff site if a
# future refactor changed the render. Both get pinned below.
_SPACED_DEVICE = "Anti cater VK-01"


@pytest.mark.asyncio
async def test_coalescer_adjust_emits_canonical_event(caplog):
    """Pin the migrated knob.adjust emit (success path in _Coalescer):
    the untrusted device label is quoted because it contains spaces,
    and the signed delta renders unquoted as a bare `+N` token."""
    calls: List[str] = []

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        calls.append(path)
        return ControlResponse(200, b"")

    action = KeyAction(
        "POST", "/volume/adjust", {"delta_percent": 2}, coalesce=True,
    )
    cz = _Coalescer(post, action, _SPACED_DEVICE)
    with caplog.at_level(logging.INFO, logger="jasper.accessories.bridge"):
        cz.hit()
        cz.hit()
        # Wait past the coalesce window so the deferred flush fires.
        await asyncio.sleep(COALESCE_WINDOW_SEC * 2)
    assert calls == ["/volume/adjust"]
    assert caplog.records[-1].getMessage() == (
        'event=knob.adjust device="Anti cater VK-01" delta=+4 status=200'
    )


@pytest.mark.asyncio
async def test_tap_failure_emits_canonical_event(caplog):
    """Pin the migrated knob.tap.failed emit: the untrusted device
    label and free-text error are both quoted (spaces force quoting),
    proving the dispatch-error path no longer corrupts logfmt."""

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        raise ControlError("simulated: jasper-control down")

    action = TapAction(
        on_single=KeyAction("POST", "/transport/toggle", {}),
        window_ms=WINDOW_MS,
    )
    tc = _TapCounter(post, action, _SPACED_DEVICE, "KEY_FAKE")
    with caplog.at_level(logging.WARNING, logger="jasper.accessories.bridge"):
        tc.hit()
        await asyncio.sleep(WINDOW_SEC * 2)
    assert caplog.records[-1].getMessage() == (
        'event=knob.tap.failed device="Anti cater VK-01" key=KEY_FAKE '
        'count=1 path=/transport/toggle '
        'err="simulated: jasper-control down"'
    )


def _install_fake_evdev(monkeypatch, *, input_device) -> None:
    """Inject a minimal fake `evdev` module so _read_device imports
    cleanly on dev hosts that lack the Linux-only package.

    `input_device` is the callable bound to `evdev.InputDevice`; the
    rest of the surface (`ecodes`) is stubbed only as far as
    _read_device touches it.
    """
    fake = types.ModuleType("evdev")
    fake.InputDevice = input_device
    ecodes = types.SimpleNamespace(EV_KEY=1, keys={})
    fake.ecodes = ecodes
    monkeypatch.setitem(sys.modules, "evdev", fake)


@pytest.mark.asyncio
async def test_read_device_hold_action_posts_on_press_and_release(monkeypatch):
    calls: List[tuple[str, str, Optional[dict]]] = []

    class _Event:
        def __init__(self, value: int):
            self.type = 1
            self.code = 217
            self.value = value

    class _FakeDev:
        def __init__(self, path):
            self.info = types.SimpleNamespace(
                bustype=5, vendor=0x2717, product=0x32B9,
            )
            self.name = "WiiM Remote 2"

        async def async_read_loop(self):
            # Press, autorepeat, release. HoldAction should ignore repeat.
            for value in (1, 2, 0):
                yield _Event(value)
                await asyncio.sleep(0)

        def close(self):
            pass

    _install_fake_evdev(monkeypatch, input_device=_FakeDev)

    device = Device(
        name="WiiM Remote 2",
        vendor_id=0x2717,
        product_id=0x32B9,
        keymap={
            217: HoldAction(
                on_press=KeyAction("POST", "/session/start", {}),
                on_release=KeyAction("POST", "/session/end", {}),
            )
        },
    )

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        calls.append((method, path, body))
        return ControlResponse(200, b"")

    await _read_device("/dev/input/event9", device, post)
    await asyncio.sleep(0)
    assert calls == [
        ("POST", "/session/start", None),
        ("POST", "/session/end", None),
    ]


@pytest.mark.asyncio
async def test_read_device_open_failure_emits_canonical_event(caplog, monkeypatch):
    """Pin the migrated knob.open.failed emit through the real
    _read_device path: when InputDevice() raises OSError, the device
    label and error are escaped (both carry spaces)."""

    def _raises(path):
        raise OSError("simulated: no such device")

    _install_fake_evdev(monkeypatch, input_device=_raises)

    device = Device(
        name="Anti cater VK-01",
        vendor_id=0x514C,
        product_id=0x8850,
        keymap={},
    )

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        return ControlResponse(200, b"")

    with caplog.at_level(logging.WARNING, logger="jasper.accessories.bridge"):
        await _read_device("/dev/input/event9", device, post)
    assert caplog.records[-1].getMessage() == (
        'event=knob.open.failed device="Anti cater VK-01" '
        'path=/dev/input/event9 err="simulated: no such device"'
    )


@pytest.mark.asyncio
async def test_read_device_close_emits_canonical_event(caplog, monkeypatch):
    """Pin the migrated knob.close emit through the real _read_device
    path: when the read loop raises OSError (unplug / BT out of range),
    the device label and free-text reason are escaped (both carry
    spaces)."""

    class _FakeDev:
        def __init__(self, path):
            self.info = types.SimpleNamespace(
                bustype=3, vendor=0x514C, product=0x8850,
            )
            self.name = "Anti cater VK-01"

        async def async_read_loop(self):
            raise OSError("simulated: device disconnected")
            yield  # pragma: no cover - makes this an async generator

        def close(self):
            pass

    _install_fake_evdev(monkeypatch, input_device=_FakeDev)

    device = Device(
        name="Anti cater VK-01",
        vendor_id=0x514C,
        product_id=0x8850,
        keymap={},
    )

    async def post(method: str, path: str, body: Optional[dict]) -> ControlResponse:
        return ControlResponse(200, b"")

    with caplog.at_level(logging.INFO, logger="jasper.accessories.bridge"):
        await _read_device("/dev/input/event9", device, post)
    # The last line is knob.close; knob.open is emitted first (INFO).
    assert caplog.records[-1].getMessage() == (
        'event=knob.close device="Anti cater VK-01" '
        'reason="simulated: device disconnected"'
    )
