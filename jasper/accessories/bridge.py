# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The jasper-input process: every accessory bridge in one interpreter.

Two bridges share this process (ADR-0225), each supervised as its own task so
a fault in one can never stop the other or the process:

* the HID bridge below — evdev → jasper-control HTTP calls, always running;
* the WiiM Remote 2 BLE mic adapter (``wiim_remote_mic``), run only while
  ``jasper-accessory-reconcile`` publishes that accessory's manual mic source.

The HID bridge watches /dev/input/event* for any device matching
`registry.KNOWN_PROFILES` (by USB VID/PID or Bluetooth name fallback).
For each match, opens an async reader and translates key events into
HTTP calls against jasper-control on localhost. Volume bursts are
coalesced into at most one POST per ~80 ms window so a fast spin or
held remote button doesn't hammer the daemon while still moving
promptly during the gesture.

Hot-plug: a pyudev monitor catches "add" events on /dev/input/* and
opens a reader for matched devices; "remove" cancels that device's
reader. Each reader runs under a per-device supervisor task that
re-arms it on a bounded backoff when it ends, and reports its state
in the status file so a dead reader is visible in ``/state``.

The bridge is the translation boundary for supported HID devices that
surface as kernel input nodes.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from typing import Any, Awaitable, Callable, Optional

from jasper.control.client import (
    CONTROL_PORT, AsyncControlClient, ControlError, ControlResponse,
)
from jasper.log_event import log_event

# pyudev is Linux-only (Pi runtime). Imported lazily inside the HID bridge
# so the rest of the module (registry types, _TapCounter, _Coalescer)
# stays importable on dev hosts that don't have it — used by the
# hardware-free pytest suite. Same lazy-import idiom as
# jasper/control/server.py's _dispatch_transport.

from .constants import WIIM_REMOTE_2_SOURCE_ID
from .mic_env import read_accessory_mic_sources
from .registry import (
    KNOWN_PROFILES,
    HoldAction,
    KeyAction,
    RemoteProfile,
    TapAction,
    lookup,
    lookup_by_name,
)
from .status import STATUS_PATH
from .supervisor import Bridge, Publish, supervise

logger = logging.getLogger(__name__)


def default_control_url() -> str:
    """jasper-control on the same Pi; the bridge is a host-side caller."""
    return f"http://127.0.0.1:{CONTROL_PORT}"


# Coalesce window for rotation events. At 20 Hz detents (the VK-01's
# fast-spin rate), this collapses ~4 events into one HTTP call.
COALESCE_WINDOW_SEC = 0.08
HOLD_REPEAT_INITIAL_DELAY_SEC = 0.30
HOLD_REPEAT_INTERVAL_SEC = 0.16
HOLD_START_RETRY_SEC = 0.20

# Re-arm delay for a reader that ended, doubling to the ceiling so a device
# that dies at every open costs one wakeup per 30 s on the Pi Zero 2 W's
# single core rather than a spin. Reset once a reader outlives the ceiling.
# Remove when the reader can prove its own liveness to the supervisor (a
# heartbeat) — until then this is the only re-arm.
READER_REARM_SEC = 1.0
READER_REARM_MAX_SEC = 30.0
# udev fires "add" before the kernel finishes wiring up /dev/input/event*;
# the node is retried on this ladder (0.1, 0.2, 0.4, 0.8, 1.6 s) before the
# bridge gives up on it and waits for the next hot-plug.
UDEV_SETTLE_SEC = 0.1
UDEV_SETTLE_ATTEMPTS = 5


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
    action: KeyAction | None,
    device_name: str,
    key_name: str,
    profile_id: str | None,
    *,
    status: int | None = None,
    err: str | None = None,
    count: int | None = None,
    level: int = logging.INFO,
) -> None:
    fields = {
        "device": device_name,
        "key": key_name,
    }
    if count is not None:
        fields["count"] = count
    if action is not None:
        fields["path"] = action.path
    if status is not None:
        fields["status"] = status
    if err is not None:
        fields["err"] = err
    if profile_id:
        fields["profile"] = profile_id
    log_event(logger, event, level=level, fields=fields)


def _is_retryable_hold_start(action: KeyAction, resp: ControlResponse | None) -> bool:
    if not (
        action.method == "POST"
        and action.path == "/session/start"
        and resp is not None
    ):
        return False
    if resp.status == 409:
        return True
    if resp.status == 503:
        # A press landing in the ~2s window after a jasper-voice restart,
        # before its control socket exists yet (jasper/control/uds.py's own
        # bounded connect retry already absorbs the common case within one
        # request; this is the fallback for a restart that outlasts it,
        # retried for as long as the key stays held. Each retry POST already
        # spent up to uds.py's own connect-retry budget before answering
        # 503, so the effective cadence here is that budget plus
        # HOLD_START_RETRY_SEC, not HOLD_START_RETRY_SEC alone). Keyed on
        # the structured reason, not the 409/503-shared error prose, so a
        # genuine refusal (CAP/PAUSED/MUTED/MEASURING) is left alone.
        try:
            body = resp.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and body.get("reason") == "voice_daemon_unreachable"
        )
    return False


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
            if not self._pressed:
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
            _log_key_action(
                "knob.tap.unmapped",
                None,
                self._device_name,
                self._key_name,
                self._profile_id,
                count=count,
            )
            return
        try:
            resp = await self._post(
                target.method,
                target.path,
                target.body or None,
            )
            _log_key_action(
                "knob.tap",
                target,
                self._device_name,
                self._key_name,
                self._profile_id,
                count=count,
                status=resp.status,
            )
        except ControlError as e:
            _log_key_action(
                "knob.tap.failed",
                target,
                self._device_name,
                self._key_name,
                self._profile_id,
                count=count,
                err=str(e),
                level=logging.WARNING,
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


class _ReaderHealth:
    """Per-device reader liveness, published inside the HID bridge's status
    entry (``readers``).

    The supervisor's own ``restarts``/``last_error`` describe the bridge
    coroutine, which stays healthy while every reader under it is dead —
    this is what tells the two apart in ``/state.accessory_bridges``.
    """

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self._publish: Publish = lambda: None

    def register(self, publish: Publish) -> dict[str, Any]:
        self._publish = publish
        return {"readers": self.devices}

    def opened(self, path: str, profile: str) -> None:
        entry = self.devices.get(path)
        if entry is None:
            self.devices[path] = {
                "profile": profile,
                "state": "open",
                "restarts": 0,
                "last_error": None,
            }
        else:
            entry.update(
                profile=profile,
                state="open",
                last_error=None,
                restarts=entry["restarts"] + 1,
            )
        self._publish()

    def ended(self, path: str, state: str, error: str | None = None) -> None:
        entry = self.devices.get(path)
        if entry is None:
            return
        entry.update(state=state, last_error=error)
        self._publish()


async def _run_hid_bridge(control_url: str, readers: _ReaderHealth) -> None:
    """Discover known HID accessories at startup, then watch udev for
    hot-plug. One supervisor task per attached device: it runs the reader,
    re-arms it after a bounded backoff when it ends, and is cancelled when
    udev removes the node."""
    import pyudev  # Linux-only — lazy-imported so the module loads on dev hosts.
    from evdev import InputDevice, list_devices  # type: ignore

    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by("input")

    active: dict[str, asyncio.Task] = {}
    post = AsyncControlClient(control_url).request

    async def _supervise_reader(path: str, entry: RemoteProfile) -> None:
        delay = READER_REARM_SEC
        loop = asyncio.get_running_loop()
        while True:
            readers.opened(path, entry.id)
            started = loop.time()
            try:
                await _read_device(path, entry, post)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a dead reader is a dead knob
                err: str | None = type(exc).__name__
                log_event(
                    logger,
                    "knob.reader_died",
                    level=logging.WARNING,
                    device=entry.name,
                    profile=entry.id,
                    path=path,
                    error=err,
                    err=str(exc),
                )
            else:
                err = None
            readers.ended(path, "died", err)
            if loop.time() - started >= READER_REARM_MAX_SEC:
                delay = READER_REARM_SEC
            await asyncio.sleep(delay)
            delay = min(delay * 2, READER_REARM_MAX_SEC)

    def _maybe_start(path: str) -> bool:
        """False only when the node could not be opened at all — a settle
        race worth retrying. A node that is not a known accessory is True:
        nothing to wait for."""
        try:
            dev = InputDevice(path)
        except OSError:
            return False
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
            return True
        existing = active.get(path)
        if existing is not None and not existing.done():
            return True
        active[path] = asyncio.create_task(_supervise_reader(path, entry))
        return True

    async def _start_when_settled(path: str) -> None:
        delay = UDEV_SETTLE_SEC
        for _ in range(UDEV_SETTLE_ATTEMPTS):
            await asyncio.sleep(delay)
            if _maybe_start(path):
                return
            delay *= 2
        log_event(
            logger,
            "knob.open.failed",
            level=logging.WARNING,
            path=path,
            err="node never became openable",
        )

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
            action, node = await events.get()
            if action == "add":
                await _start_when_settled(node)
            elif action == "remove":
                task = active.pop(node, None)
                if task is not None:
                    task.cancel()
                    readers.ended(node, "removed")
                    log_event(logger, "knob.removed", path=node)
    finally:
        observer.stop()
        for task in active.values():
            task.cancel()


async def _run_wiim_remote_mic() -> None:
    # Imported here, not at module scope: dbus-next and the ADPCM decoder cost
    # resident memory that a box with no WiiM Remote 2 paired must not pay.
    from .wiim_remote_mic import MicAdapterConfig, run

    await run(MicAdapterConfig())


# Manual mic source id (as published in accessory-mics.env) -> the adapter that
# produces it inside this process.
MIC_ADAPTERS: dict[str, Bridge] = {
    WIIM_REMOTE_2_SOURCE_ID: _run_wiim_remote_mic,
}


def _published_mic_adapters() -> dict[str, Bridge]:
    """The adapters jasper-accessory-reconcile currently publishes a source for.

    An unreadable or corrupt file degrades to "no accessory mic" rather than
    propagating: the HID bridge in this process carries volume and
    push-to-talk, and must start whatever the mic half says.
    """
    try:
        sources = read_accessory_mic_sources()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log_event(
            logger,
            "accessory.mic_sources_unreadable",
            level=logging.WARNING,
            err=f"{type(exc).__name__}: {exc}",
        )
        return {}
    return {
        source: MIC_ADAPTERS[source]
        for source in sources
        if source in MIC_ADAPTERS
    }


async def _run_bridges(control_url: str, status_path: str = STATUS_PATH) -> None:
    readers = _ReaderHealth()
    bridges: dict[str, Bridge] = {
        "hid": lambda: _run_hid_bridge(control_url, readers),
        **_published_mic_adapters(),
    }
    # Cancel on the signal rather than letting the default disposition kill the
    # interpreter: every pair/forget try-restarts this unit, and the bridges'
    # cleanup (GATT StopNotify, the udev observer thread, evdev fds) only runs
    # if asyncio gets to unwind them first. NotImplementedError: not every
    # loop implementation carries signal handlers.
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if current is not None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, current.cancel)
    log_event(logger, "accessory.bridges_started", bridges=",".join(bridges))
    try:
        await supervise(
            bridges, status_path=status_path, details={"hid": readers.register},
        )
    except asyncio.CancelledError:
        log_event(logger, "accessory.bridges_stopped")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the accessory bridges: HID key events into jasper-control "
            "HTTP calls, plus any published accessory mic adapter."
        ),
    )
    control_url = default_control_url()
    parser.add_argument(
        "--control-url", default=control_url,
        help=f"jasper-control base URL (default {control_url})",
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
        asyncio.run(_run_bridges(args.control_url))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
