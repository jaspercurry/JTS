"""evdev → jasper-control HTTP bridge for HID accessories.

Watches /dev/input/event* for any device matching `registry.KNOWN_DEVICES`
(by USB VID/PID). For each match, opens an async evdev reader and
translates key events into HTTP calls against jasper-control on
localhost. Volume-rotation bursts are coalesced into one POST per
~80 ms window so a fast spin doesn't hammer the daemon.

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

import httpx
import pyudev

from .registry import KNOWN_DEVICES, Device, KeyAction, lookup

logger = logging.getLogger(__name__)


# jasper-control on the same Pi. Stays localhost: the bridge is the
# only host-side caller; the LAN-facing dial / satellites talk to
# the same daemon over the LAN.
DEFAULT_CONTROL_URL = "http://127.0.0.1:8780"

# Coalesce window for rotation events. At 20 Hz detents (the VK-01's
# fast-spin rate), this collapses ~4 events into one HTTP call.
COALESCE_WINDOW_SEC = 0.08


class _Coalescer:
    """Per-keycode accumulator: sums `delta_percent` over a short
    window, fires one HTTP POST when the window quiets.

    A new event resets the flush timer — the POST goes out after
    COALESCE_WINDOW_SEC of idle, so a continuous fast spin emits
    one POST every ~80 ms with the summed delta in between."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        control_url: str,
        action: KeyAction,
        device_name: str,
    ) -> None:
        self._client = client
        self._url = control_url + action.path
        self._per_hit_delta = int(action.body.get("delta_percent", 0))
        self._device_name = device_name
        self._pending = 0
        self._flush: asyncio.Task | None = None

    def hit(self) -> None:
        self._pending += self._per_hit_delta
        if self._flush is not None and not self._flush.done():
            self._flush.cancel()
        self._flush = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(COALESCE_WINDOW_SEC)
        except asyncio.CancelledError:
            return
        delta = self._pending
        self._pending = 0
        try:
            r = await self._client.post(
                self._url, json={"delta_percent": delta}, timeout=2.0,
            )
            logger.info(
                "event=knob.adjust device=%s delta=%+d status=%d",
                self._device_name, delta, r.status_code,
            )
        except httpx.HTTPError as e:
            logger.warning(
                "event=knob.adjust.failed device=%s delta=%+d err=%s",
                self._device_name, delta, e,
            )


async def _post_once(
    client: httpx.AsyncClient,
    control_url: str,
    action: KeyAction,
    device_name: str,
    key_name: str,
) -> None:
    """Fire-once HTTP call for a non-coalescing key (mute, etc.)."""
    try:
        r = await client.request(
            action.method,
            control_url + action.path,
            json=action.body or None,
            timeout=2.0,
        )
        logger.info(
            "event=knob.action device=%s key=%s path=%s status=%d",
            device_name, key_name, action.path, r.status_code,
        )
    except httpx.HTTPError as e:
        logger.warning(
            "event=knob.action.failed device=%s key=%s err=%s",
            device_name, key_name, e,
        )


def _key_name(code: int) -> str:
    """Best-effort human keycode name for logging."""
    from evdev import ecodes  # type: ignore

    name = ecodes.keys.get(code, code)
    if isinstance(name, list):  # multiple aliases — pick the first
        name = name[0]
    return str(name)


async def _read_device(
    device_path: str,
    device: Device,
    client: httpx.AsyncClient,
    control_url: str,
) -> None:
    """Translate key events from one matched device into HTTP calls.
    Exits cleanly on unplug (OSError) or cancellation."""
    from evdev import InputDevice, ecodes  # type: ignore

    try:
        dev = InputDevice(device_path)
    except OSError as e:
        logger.warning(
            "event=knob.open.failed device=%s path=%s err=%s",
            device.name, device_path, e,
        )
        return

    logger.info(
        "event=knob.open device=%s path=%s vid=%04x pid=%04x",
        device.name, device_path, device.vendor_id, device.product_id,
    )

    coalescers: dict[int, _Coalescer] = {}
    tasks: set[asyncio.Task] = set()  # retain non-coalescing dispatch tasks

    try:
        async for ev in dev.async_read_loop():
            if ev.type != ecodes.EV_KEY:
                continue
            if ev.value != 1:  # press only; ignore release + autorepeat
                continue
            action = device.keymap.get(ev.code)
            if action is None:
                continue
            if action.coalesce:
                cz = coalescers.get(ev.code)
                if cz is None:
                    cz = _Coalescer(
                        client, control_url, action, device.name,
                    )
                    coalescers[ev.code] = cz
                cz.hit()
            else:
                t = asyncio.create_task(_post_once(
                    client, control_url, action, device.name,
                    _key_name(ev.code),
                ))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
    except OSError as e:
        # Device unplugged / BT out of range — reader exits, supervisor
        # rediscovers on the next "add" udev event.
        logger.info(
            "event=knob.close device=%s reason=%s", device.name, e,
        )
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


async def _supervise(control_url: str) -> None:
    """Discover known HID accessories at startup, then watch udev for
    hot-plug. One reader task per attached device; tasks exit on
    unplug and are recreated on replug."""
    from evdev import InputDevice, list_devices  # type: ignore

    ctx = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by("input")

    active: dict[str, asyncio.Task] = {}

    def _maybe_start(client: httpx.AsyncClient, path: str) -> None:
        try:
            dev = InputDevice(path)
        except OSError:
            return
        vid, pid = dev.info.vendor, dev.info.product
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass
        entry = lookup(vid, pid)
        if entry is None:
            return
        existing = active.get(path)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            _read_device(path, entry, client, control_url),
        )
        active[path] = task

    async with httpx.AsyncClient() as client:
        for path in list_devices():
            _maybe_start(client, path)

        if not active:
            logger.info(
                "event=knob.bridge.idle (no known accessories attached; "
                "waiting for hot-plug; known=%s)",
                ", ".join(d.name for d in KNOWN_DEVICES),
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
                    _maybe_start(client, node)
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
