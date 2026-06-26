# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""evdev → jasper-control HTTP bridge for HID remote profiles.

Watches /dev/input/event* for any device matching
`registry.KNOWN_PROFILES` (by USB VID/PID or Bluetooth name fallback).
For each match, opens an async reader and translates key events into
HTTP calls against jasper-control on localhost. Volume bursts are
coalesced into at most one POST per ~80 ms window so a fast spin or
held remote button doesn't hammer the daemon while still moving
promptly during the gesture.

Hot-plug: a pyudev monitor catches "add" events on /dev/input/* and
opens a reader for matched devices. "remove" is handled passively —
the reader's `async_read_loop` raises OSError when the device
disappears and the task exits cleanly.

The dial (ESP32, WiFi) posts to jasper-control directly; this bridge
is the analogous translation for HID devices that surface as kernel
input nodes instead.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from jasper.control.client import AsyncControlClient, ControlError, ControlResponse
from jasper.log_event import log_event

# pyudev is Linux-only (Pi runtime). Imported lazily inside _supervise
# so the rest of the module (registry types, _TapCounter, _Coalescer)
# stays importable on dev hosts that don't have it — used by the
# hardware-free pytest suite. Same lazy-import idiom as
# jasper/control/server.py's _dispatch_transport.

from .registry import (
    KNOWN_PROFILES,
    HoldAction,
    KeyAction,
    RemoteProfile,
    TapAction,
    lookup,
    lookup_by_name,
)

logger = logging.getLogger(__name__)


# jasper-control on the same Pi. Stays localhost: the bridge is the
# only host-side caller; the LAN-facing dial / satellites talk to
# the same daemon over the LAN.
DEFAULT_CONTROL_URL = "http://127.0.0.1:8780"

# Coalesce window for rotation events. At 20 Hz detents (the VK-01's
# fast-spin rate), this collapses ~4 events into one HTTP call.
COALESCE_WINDOW_SEC = 0.08
HOLD_REPEAT_INITIAL_DELAY_SEC = 0.30
HOLD_REPEAT_INTERVAL_SEC = 0.16
HOLD_START_RETRY_SEC = 0.20


# Async poster signature: (method, path, body-dict-or-None) -> ControlResponse.
Poster = Callable[[str, str, Optional[dict]], Awaitable[ControlResponse]]


class _Coalescer:
    """Per-keycode accumulator: sums `delta_percent` over a short
    window, fires one HTTP POST per window while hits continue.

    The first event starts the timer; later events add to the pending
    delta without pushing the timer out. If new hits arrive while the
    HTTP POST is in flight, this task keeps ownership and flushes the
    next batch after another window."""

    def __init__(
        self,
        post: Poster,
        action: KeyAction,
        device_name: str,
        profile_id: str | None = None,
    ) -> None:
        self._post = post
        self._path = action.path
        self._per_hit_delta = int(action.body.get("delta_percent", 0))
        self._device_name = device_name
        self._profile_id = profile_id
        self._pending = 0
        self._flush: asyncio.Task | None = None

    def hit(self) -> None:
        self._pending += self._per_hit_delta
        if self._flush is None or self._flush.done():
            self._flush = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(COALESCE_WINDOW_SEC)
            except asyncio.CancelledError:
                return
            delta = self._pending
            self._pending = 0
            if delta == 0:
                return
            try:
                resp = await self._post(
                    "POST", self._path, {"delta_percent": delta},
                )
                fields = {
                    "device": self._device_name,
                    "delta": f"{delta:+d}",
                    "status": resp.status,
                }
                if self._profile_id:
                    fields["profile"] = self._profile_id
                log_event(logger, "knob.adjust", fields=fields)
            except ControlError as e:
                fields = {
                    "device": self._device_name,
                    "delta": f"{delta:+d}",
                    "err": str(e),
                }
                if self._profile_id:
                    fields["profile"] = self._profile_id
                log_event(
                    logger, "knob.adjust.failed",
                    level=logging.WARNING, fields=fields,
                )
            if self._pending == 0:
                return


async def _post_once(
    post: Poster,
    action: KeyAction,
    device_name: str,
    key_name: str,
    profile_id: str | None = None,
    *,
    emit_log: bool = True,
) -> ControlResponse | None:
    """Fire-once HTTP call for a non-coalescing key (mute, etc.)."""
    try:
        resp = await post(
            action.method,
            action.path,
            action.body or None,
        )
        if emit_log:
            _log_key_action(
                "knob.action",
                action,
                device_name,
                key_name,
                profile_id,
                status=resp.status,
            )
        return resp
    except ControlError as e:
        if emit_log:
            _log_key_action(
                "knob.action.failed",
                action,
                device_name,
                key_name,
                profile_id,
                err=str(e),
                level=logging.WARNING,
            )
        return None


def _log_key_action(
    event: str,
    action: KeyAction,
    device_name: str,
    key_name: str,
    profile_id: str | None,
    *,
    status: int | None = None,
    err: str | None = None,
    level: int = logging.INFO,
) -> None:
    fields = {
        "device": device_name,
        "key": key_name,
        "path": action.path,
    }
    if status is not None:
        fields["status"] = status
    if err is not None:
        fields["err"] = err
    if profile_id:
        fields["profile"] = profile_id
    log_event(logger, event, level=level, fields=fields)


def _is_retryable_hold_start(action: KeyAction, resp: ControlResponse | None) -> bool:
    return (
        action.method == "POST"
        and action.path == "/session/start"
        and resp is not None
        and resp.status == 409
    )


class _HoldController:
    """Stateful press/release dispatcher for HoldAction.

    Physical push-to-talk buttons have two useful properties that a bare
    "POST on press, POST on release" mapping cannot express:

      * Press can arrive while the previous voice turn is still closing; keep
        retrying START while the key remains held instead of forcing the user
        to tap once and hold again.
      * Release should only send END if START actually succeeded. Otherwise a
        quick press during a busy turn becomes a harmless no-op, not a stray
        409-generating END.
    """

    def __init__(
        self,
        post: Poster,
        action: HoldAction,
        device_name: str,
        key_name: str,
        profile_id: str | None = None,
    ) -> None:
        self._post = post
        self._action = action
        self._device_name = device_name
        self._key_name = key_name
        self._profile_id = profile_id
        self._pressed = False
        self._started = False
        self._released = asyncio.Event()
        self._start_task: asyncio.Task | None = None

    async def press(self) -> None:
        if self._pressed:
            return
        self._pressed = True
        self._started = False
        self._released.clear()
        resp = await _post_once(
            self._post,
            self._action.on_press,
            self._device_name,
            self._key_name,
            self._profile_id,
        )
        if resp is not None and resp.ok:
            self._started = True
            return
        if not _is_retryable_hold_start(self._action.on_press, resp):
            return
        _log_key_action(
            "knob.hold.retry",
            self._action.on_press,
            self._device_name,
            self._key_name,
            self._profile_id,
            status=resp.status,
        )
        self._start_task = asyncio.create_task(self._retry_until_ready())

    async def release(self) -> None:
        if not self._pressed and not self._started:
            return
        self._pressed = False
        self._released.set()
        if self._start_task is not None:
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
            self._start_task = None
        if self._started:
            await _post_once(
                self._post,
                self._action.on_release,
                self._device_name,
                self._key_name,
                self._profile_id,
            )
            self._started = False

    async def _retry_until_ready(self) -> None:
        while self._pressed:
            try:
                await asyncio.wait_for(
                    self._released.wait(),
                    timeout=HOLD_START_RETRY_SEC,
                )
            except asyncio.TimeoutError:
                pass
            else:
                return
            resp = await _post_once(
                self._post,
                self._action.on_press,
                self._device_name,
                self._key_name,
                self._profile_id,
                emit_log=False,
            )
            if resp is not None and resp.ok:
                _log_key_action(
                    "knob.action",
                    self._action.on_press,
                    self._device_name,
                    self._key_name,
                    self._profile_id,
                    status=resp.status,
                )
                self._started = True
                return
            if not _is_retryable_hold_start(self._action.on_press, resp):
                return


class _RepeatController:
    """Host-side hold repeat for coalesced volume-style actions."""

    def __init__(self, coalescer: _Coalescer) -> None:
        self._coalescer = coalescer
        self._pressed = False
        self._task: asyncio.Task | None = None

    @property
    def pressed(self) -> bool:
        return self._pressed

    def press(self) -> None:
        self._coalescer.hit()
        if self._pressed:
            return
        self._pressed = True
        self._task = asyncio.create_task(self._repeat_loop())

    async def release(self) -> None:
        self._pressed = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _repeat_loop(self) -> None:
        try:
            await asyncio.sleep(HOLD_REPEAT_INITIAL_DELAY_SEC)
            while self._pressed:
                self._coalescer.hit()
                await asyncio.sleep(HOLD_REPEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            return


class _TapCounter:
    """Per-keycode tap-count state machine: counts consecutive presses,
    fires the matching HTTP call (single/double/triple) after the
    quiescence window — or immediately on the third tap, since
    quadruple-tap has no semantic and waiting another window just
    adds perceived latency to "previous".

    Concurrency notes (handled, but worth knowing if you change this):
      - hit() can run while a prior fire's HTTP is in flight; we snapshot
        the count into a local before the HTTP, so a late hit() during
        dispatch can't corrupt it.
      - The timer is cancelled-and-replaced on each hit() that arrives
        during its sleep phase. If a hit() lands in the narrow window
        after sleep but before the snapshot, the cancel is a no-op and
        the in-flight fire proceeds with the count it observed; the
        late hit() starts a fresh sequence on its own next timer.
    """

    def __init__(
        self,
        post: Poster,
        action: TapAction,
        device_name: str,
        key_name: str,
        profile_id: str | None = None,
    ) -> None:
        self._post = post
        self._action = action
        self._device_name = device_name
        self._key_name = key_name
        self._profile_id = profile_id
        self._window_sec = action.window_ms / 1000.0
        self._count = 0
        self._timer: asyncio.Task | None = None
        # Retain in-flight dispatch tasks so they aren't garbage-
        # collected mid-await (asyncio drops weakly-held tasks).
        self._dispatches: set[asyncio.Task] = set()

    def hit(self) -> None:
        self._count += 1
        # Cancel any pending deferred-fire timer; it'll be replaced.
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        # Three taps is the longest gesture we recognise — fire
        # immediately rather than waiting another window for a
        # quadruple that has no meaning.
        if self._count >= 3:
            count = self._count
            self._count = 0
            self._track(asyncio.create_task(self._dispatch(count)))
            return
        # Otherwise, defer — there might be more taps coming.
        self._timer = asyncio.create_task(self._fire_after_delay())

    def _track(self, task: asyncio.Task) -> None:
        self._dispatches.add(task)
        task.add_done_callback(self._dispatches.discard)

    async def _fire_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._window_sec)
        except asyncio.CancelledError:
            return
        # Snapshot count BEFORE the await in _dispatch so a late hit()
        # arriving during HTTP can't mutate what we're firing.
        count = self._count
        self._count = 0
        await self._dispatch(count)

    async def _dispatch(self, count: int) -> None:
        if count == 1:
            target = self._action.on_single
        elif count == 2:
            target = self._action.on_double
        else:  # count >= 3
            target = self._action.on_triple
        if target is None:
            # Tap-count has no mapping — silently drop with a log so
            # the operator can confirm taps are registering but the
            # gesture isn't defined for this device.
            fields = {
                "device": self._device_name,
                "key": self._key_name,
                "count": count,
            }
            if self._profile_id:
                fields["profile"] = self._profile_id
            log_event(logger, "knob.tap.unmapped", fields=fields)
            return
        try:
            resp = await self._post(
                target.method,
                target.path,
                target.body or None,
            )
            fields = {
                "device": self._device_name,
                "key": self._key_name,
                "count": count,
                "path": target.path,
                "status": resp.status,
            }
            if self._profile_id:
                fields["profile"] = self._profile_id
            log_event(logger, "knob.tap", fields=fields)
        except ControlError as e:
            fields = {
                "device": self._device_name,
                "key": self._key_name,
                "count": count,
                "path": target.path,
                "err": str(e),
            }
            if self._profile_id:
                fields["profile"] = self._profile_id
            log_event(
                logger, "knob.tap.failed",
                level=logging.WARNING, fields=fields,
            )


def _key_name(code: int) -> str:
    """Best-effort human keycode name for logging."""
    from evdev import ecodes  # type: ignore

    name = ecodes.keys.get(code, code)
    if isinstance(name, (list, tuple)):  # multiple aliases — pick a stable key name
        key_names = [n for n in name if isinstance(n, str) and n.startswith("KEY_")]
        name = key_names[0] if key_names else name[0]
    return str(name)


async def _read_device(
    device_path: str,
    device: RemoteProfile,
    post: Poster,
) -> None:
    """Translate key events from one matched device into HTTP calls.
    Exits cleanly on unplug (OSError) or cancellation."""
    from evdev import InputDevice, ecodes  # type: ignore

    try:
        dev = InputDevice(device_path)
    except OSError as e:
        log_event(
            logger,
            "knob.open.failed",
            level=logging.WARNING,
            device=device.name,
            profile=device.id,
            path=device_path,
            err=str(e),
        )
        return

    # Log the runtime identity (bus + actual kernel-reported vid/pid)
    # rather than the registry's canonical USB IDs — otherwise a BT-
    # paired accessory shows up in the journal as its USB IDs, which
    # is confusing when troubleshooting "is this plugged in over USB
    # or BT?". bustype: 3=USB, 5=BLUETOOTH.
    transport = {3: "usb", 5: "bt"}.get(dev.info.bustype, f"bus={dev.info.bustype:#x}")
    log_event(
        logger,
        "knob.open",
        device=device.name,
        profile=device.id,
        path=device_path,
        transport=transport,
        vid=f"{dev.info.vendor:04x}",
        pid=f"{dev.info.product:04x}",
    )

    coalescers: dict[int, _Coalescer] = {}
    repeaters: dict[int, _RepeatController] = {}
    tap_counters: dict[int, _TapCounter] = {}
    hold_controllers: dict[int, _HoldController] = {}
    tasks: set[asyncio.Task] = set()  # retain non-coalescing dispatch tasks

    try:
        async for ev in dev.async_read_loop():
            if ev.type != ecodes.EV_KEY:
                continue
            action = device.keymap.get(ev.code)
            if action is None:
                continue
            key_name = _key_name(ev.code)
            if isinstance(action, HoldAction):
                if ev.value == 1:
                    controller = hold_controllers.get(ev.code)
                    if controller is None:
                        controller = _HoldController(
                            post, action, device.name, key_name, device.id,
                        )
                        hold_controllers[ev.code] = controller
                    await controller.press()
                elif ev.value == 0:
                    controller = hold_controllers.get(ev.code)
                    if controller is not None:
                        await controller.release()
                else:
                    continue
                continue
            if isinstance(action, TapAction):
                if ev.value != 1:  # taps are press-only; ignore release + autorepeat
                    continue
                tc = tap_counters.get(ev.code)
                if tc is None:
                    tc = _TapCounter(
                        post, action, device.name, key_name, device.id,
                    )
                    tap_counters[ev.code] = tc
                tc.hit()
            elif action.coalesce:
                if ev.value not in (0, 1, 2):  # press/repeat/release only
                    continue
                cz = coalescers.get(ev.code)
                if cz is None:
                    cz = _Coalescer(post, action, device.name, device.id)
                    coalescers[ev.code] = cz
                repeater = repeaters.get(ev.code)
                if repeater is None:
                    repeater = _RepeatController(cz)
                    repeaters[ev.code] = repeater
                if ev.value == 1:
                    repeater.press()
                elif ev.value == 0:
                    await repeater.release()
                elif not repeater.pressed:
                    # Defensive fallback for devices that emit EV_KEY
                    # repeat without a visible press edge.
                    cz.hit()
            else:
                if ev.value != 1:  # press only; ignore release + autorepeat
                    continue
                t = asyncio.create_task(_post_once(
                    post, action, device.name, key_name, device.id,
                ))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
    except OSError as e:
        # Device unplugged / BT out of range — reader exits, supervisor
        # rediscovers on the next "add" udev event.
        log_event(
            logger,
            "knob.close",
            device=device.name,
            profile=device.id,
            reason=str(e),
        )
    finally:
        for repeater in list(repeaters.values()):
            await repeater.release()
        for controller in list(hold_controllers.values()):
            await controller.release()
        repeaters.clear()
        hold_controllers.clear()
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


async def _supervise(control_url: str) -> None:
    """Discover known HID accessories at startup, then watch udev for
    hot-plug. One reader task per attached device; tasks exit on
    unplug and are recreated on replug."""
    import pyudev  # Linux-only — lazy-imported so the module loads on dev hosts.
    from evdev import InputDevice, list_devices  # type: ignore

    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by("input")

    active: dict[str, asyncio.Task] = {}
    post = AsyncControlClient(control_url).request

    def _maybe_start(path: str) -> None:
        try:
            dev = InputDevice(path)
        except OSError:
            return
        vid, pid = dev.info.vendor, dev.info.product
        name = dev.name or ""
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass
        # USB VID/PID is the strict match; BT-HID falls back to
        # name match because the same physical device often advertises
        # different USB IDs over BLE (e.g. VK-01 reuses Apple Magic
        # Mouse IDs 05AC:022C when paired over BT).
        entry = lookup(vid, pid) or lookup_by_name(name)
        if entry is None:
            return
        existing = active.get(path)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(_read_device(path, entry, post))
        active[path] = task

    for path in list_devices():
        _maybe_start(path)

    if not active:
        log_event(
            logger,
            "knob.bridge.idle",
            note="no known accessories attached; waiting for hot-plug",
            known=", ".join(d.name for d in KNOWN_PROFILES),
        )

    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()

    def _udev_cb(action: str, dev: pyudev.Device) -> None:
        node = dev.device_node
        if node and node.startswith("/dev/input/event"):
            loop.call_soon_threadsafe(events.put_nowait, (action, node))

    observer = pyudev.MonitorObserver(monitor, _udev_cb)
    observer.start()

    try:
        while True:
            # Reap completed reader tasks.
            for p in list(active.keys()):
                if active[p].done():
                    del active[p]
            action, node = await events.get()
            if action == "add":
                # udev fires before the kernel finishes wiring up
                # /dev/input/event* sometimes — a short sleep
                # avoids racing the device-open.
                await asyncio.sleep(0.1)
                _maybe_start(node)
    finally:
        observer.stop()
        for task in active.values():
            task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Translate HID accessory key events (volume knobs, etc.) "
            "into HTTP calls against jasper-control."
        ),
    )
    parser.add_argument(
        "--control-url", default=DEFAULT_CONTROL_URL,
        help=f"jasper-control base URL (default {DEFAULT_CONTROL_URL})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (default INFO).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(_supervise(args.control_url))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
