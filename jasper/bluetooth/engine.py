# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair state machine.

One generic flow regardless of device class:

  trust → pair → connect → handler

Async generator yielding StatusEvent dicts; the web layer streams them
over SSE. Per-class behaviour is dispatched through `handlers.pick()`.

Designed to be run inside one long-lived `BluetoothEngine` instance
on the daemon. Each `pair(mac)` call is independent. Pairing
authorization is owned by the always-on JTS no-code default agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

from jasper.log_event import log_event

from .handlers import REGISTRY, pick
from .models import BluetoothDevice
from .roles import RoleStore
from .scan import DeviceObserver

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEFAULT_ADAPTER = "hci0"
SCAN_DBUS_TIMEOUT_SEC = 5.0
SCAN_OPERATION_ERRORS = (
    AttributeError,
    DBusError,
    EOFError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
AccessoryReconciler = Callable[[str], Awaitable[object]]
ACCESSORY_RECONCILE_ERRORS = (
    DBusError,
    OSError,
    RuntimeError,
    subprocess.SubprocessError,
)


async def _default_accessory_reconcile(reason: str) -> object:
    from jasper.accessories.reconcile import reconcile_once

    return await reconcile_once(reason=reason)


def _stop_discovery_already_idle(err: DBusError) -> bool:
    """Return whether BlueZ proved StopDiscovery is already satisfied."""

    if err.type == "org.bluez.Error.NotReady":
        return True
    if err.type != "org.bluez.Error.Failed":
        return False
    detail = str(err).casefold()
    return any(
        message in detail
        for message in (
            "no discovery started",
            "discovery not started",
            "not discovering",
        )
    )


class BluetoothEngine:
    """Owns the bus connection + observer. Singleton on the
    daemon; exposes pair / connect / disconnect / forget as async
    generators yielding status events."""

    def __init__(
        self,
        adapter: str = DEFAULT_ADAPTER,
        *,
        accessory_reconcile: AccessoryReconciler | None = None,
    ) -> None:
        self._adapter = adapter
        self._bus: MessageBus | None = None
        self._observer = DeviceObserver()
        self._roles = RoleStore()
        self._accessory_reconcile = accessory_reconcile or _default_accessory_reconcile
        self._closing = False
        # Set only when this engine deliberately disconnects its discovery
        # owner bus to prove an ambiguous scan terminal. The next operation
        # that needs the shared engine bus may establish one bounded
        # replacement; an engine that simply has not started yet remains a
        # no-op for backwards compatibility.
        self._bus_recovery_required = False
        # Pair/connect requests can arrive together after a failed scan cleanup.
        # Only one request may replace the shared long-lived bus.
        self._bus_recovery_lock = asyncio.Lock()
        # Active scan auto-stop task. bluez auto-stops discovery when
        # the initiating bus client disconnects, so the engine owns
        # discovery on its long-lived bus. A short-lived adapter helper
        # would lose the scan the moment its ephemeral bus closed.
        self._scan_task: asyncio.Task | None = None
        # BlueZ discovery is adapter-global, while scan requests and their
        # expiry tasks are concurrent. Keep StartDiscovery + deadline refresh,
        # natural expiry, and manual stop as one serialized state transition.
        self._scan_lock = asyncio.Lock()

    @property
    def observer(self) -> DeviceObserver:
        return self._observer

    @property
    def roles(self) -> RoleStore:
        return self._roles

    async def start(self) -> None:
        self._closing = False
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus_recovery_required = False
        await self._observer.start()

    async def stop(self) -> None:
        # Publish shutdown intent before waiting for an in-flight StartDiscovery
        # transition. Its post-call check must not install a fresh timer while
        # stop() is queued behind it on the same lock.
        self._closing = True
        scan_task: asyncio.Task | None = None
        async with self._scan_lock:
            scan_task = self._scan_task
            self._scan_task = None
            if scan_task is not None and not scan_task.done():
                scan_task.cancel()
            bus = self._bus
            self._bus = None
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.disconnect()
            self._bus_recovery_required = False
        await self._await_cancelled_scan_task(scan_task)
        await self._observer.stop()

    # ------------- discovery (scan) -----------------

    async def start_discovery(self, *, duration_s: float = 30.0) -> None:
        """Start bluez discovery on our long-lived bus and auto-stop
        after `duration_s`. Idempotent — if a scan is already running
        the existing auto-stop deadline is replaced by a fresh one.

        Discovery MUST run on the engine's bus, not a fresh ephemeral
        connection: bluez tracks discovery per-client and auto-stops
        when the originating bus disconnects, so a short-lived bus
        would cancel the scan within a millisecond of starting it."""
        prior_task: asyncio.Task | None = None
        async with self._scan_lock:
            await self._recover_bus_if_required()
            bus = self._bus
            if self._closing or bus is None:
                return
            try:
                await self._call_bluez_start_discovery()
            except DBusError as e:
                if e.type != "org.bluez.Error.InProgress":
                    raise

            if self._closing or self._bus is not bus:
                return

            # Replace the auto-stop task only after BlueZ accepted this start.
            # If StartDiscovery fails, the prior scan keeps its real deadline.
            prior_task = self._scan_task
            if prior_task is not None and not prior_task.done():
                prior_task.cancel()
            self._scan_task = asyncio.create_task(
                self._auto_stop_scan(duration_s),
            )
        await self._await_cancelled_scan_task(prior_task)

    async def _auto_stop_scan(self, duration_s: float) -> None:
        this_task = asyncio.current_task()
        try:
            try:
                await asyncio.sleep(duration_s)
            except asyncio.CancelledError:
                return
            async with self._scan_lock:
                # A refreshed or manually-stopped scan replaced/cleared us
                # while we waited for the lock. It owns the adapter now.
                if self._scan_task is not this_task:
                    return
                # IMPORTANT: don't call stop_discovery() here — that would
                # cancel `self._scan_task`, and `self._scan_task` IS us.
                # Drive the BlueZ call directly instead.
                try:
                    await self._call_bluez_stop_discovery()
                except SCAN_OPERATION_ERRORS as e:
                    log_event(
                        logger,
                        "bluetooth.scan_auto_stop_failed",
                        error_type=type(e).__name__,
                        error=str(e),
                        level=logging.WARNING,
                    )
                    # This expiry was the final deadline. If StopDiscovery is
                    # ambiguous, disconnect the initiating client: BlueZ's
                    # per-client ownership rule then proves discovery ended.
                    self._release_scan_owner_bus(
                        self._bus,
                        reason="auto-stop-failed",
                    )
        finally:
            # Never let an older task erase a refreshed deadline.
            if self._scan_task is this_task:
                self._scan_task = None

    async def stop_discovery(self) -> None:
        """External entry point. Cancels the auto-stop task (we're
        stopping early) and tells bluez to stop discovery."""
        scan_task: asyncio.Task | None = None
        async with self._scan_lock:
            scan_task = self._scan_task
            try:
                await self._call_bluez_stop_discovery()
            except SCAN_OPERATION_ERRORS:
                # Keep a live expiry armed when manual stop fails. If no live
                # deadline exists, release the owner bus now so discovery
                # cannot remain active indefinitely.
                if scan_task is None or scan_task.done():
                    self._release_scan_owner_bus(
                        self._bus,
                        reason="manual-stop-failed-without-deadline",
                    )
                raise
            if self._scan_task is scan_task:
                self._scan_task = None
            if scan_task is not None and not scan_task.done():
                scan_task.cancel()
        await self._await_cancelled_scan_task(scan_task)

    async def _await_cancelled_scan_task(
        self,
        task: asyncio.Task | None,
    ) -> None:
        """Drain an owned canceled timer without holding ``_scan_lock``."""

        if task is None or task is asyncio.current_task():
            return
        await asyncio.gather(task, return_exceptions=True)

    def _release_scan_owner_bus(
        self,
        bus: MessageBus | None,
        *,
        reason: str,
    ) -> None:
        """Fail closed by releasing BlueZ's per-client discovery owner."""

        if bus is None or self._bus is not bus:
            return
        self._bus = None
        self._bus_recovery_required = True
        with contextlib.suppress(Exception):
            disconnect = getattr(bus, "disconnect", None)
            if callable(disconnect):
                disconnect()
        log_event(
            logger,
            "bluetooth.scan_owner_bus_released",
            reason=reason,
            level=logging.WARNING,
        )

    async def _recover_bus_if_required(self) -> None:
        """Bound and serialize recovery after fail-closed bus release."""

        if self._bus is not None or not self._bus_recovery_required:
            return
        async with self._bus_recovery_lock:
            if self._bus is not None or not self._bus_recovery_required:
                return
            try:
                bus = await asyncio.wait_for(
                    MessageBus(bus_type=BusType.SYSTEM).connect(),
                    timeout=SCAN_DBUS_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as error:
                timeout_failure = asyncio.TimeoutError(
                    f"BlueZ bus recovery timed out after {SCAN_DBUS_TIMEOUT_SEC:g}s"
                )
                log_event(
                    logger,
                    "bluetooth.bus_recovery_failed",
                    error_type=type(timeout_failure).__name__,
                    error=str(timeout_failure),
                    level=logging.WARNING,
                )
                raise timeout_failure from error
            except SCAN_OPERATION_ERRORS as error:
                recovery_failure = RuntimeError(f"BlueZ bus recovery failed: {error}")
                log_event(
                    logger,
                    "bluetooth.bus_recovery_failed",
                    error_type=type(error).__name__,
                    error=str(error),
                    level=logging.WARNING,
                )
                raise recovery_failure from error
            if self._closing:
                with contextlib.suppress(Exception):
                    bus.disconnect()
                return
            self._bus = bus
            self._bus_recovery_required = False
            log_event(logger, "bluetooth.bus_recovered")

    async def _call_bluez_start_discovery(self) -> None:
        """Bound the complete BlueZ StartDiscovery exchange."""

        bus = self._bus
        if bus is None:
            return
        path = f"/org/bluez/{self._adapter}"

        try:
            intro = await asyncio.wait_for(
                bus.introspect(BLUEZ_BUS, path),
                timeout=SCAN_DBUS_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError(
                f"BlueZ adapter introspection timed out after "
                f"{SCAN_DBUS_TIMEOUT_SEC:g}s"
            ) from error
        adapter = bus.get_proxy_object(
            BLUEZ_BUS,
            path,
            intro,
        ).get_interface("org.bluez.Adapter1")
        try:
            await asyncio.wait_for(
                adapter.call_start_discovery(),
                timeout=SCAN_DBUS_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as start_error:
            # The method may have reached BlueZ before its reply was lost. A
            # bounded StopDiscovery proves the adapter idle; if that cleanup
            # also fails, releasing the owner bus is BlueZ's final session-level
            # guarantee that discovery cannot continue without an auto-stop.
            try:
                await self._call_bluez_stop_discovery()
            except SCAN_OPERATION_ERRORS as cleanup_error:
                log_event(
                    logger,
                    "bluetooth.scan_start_cleanup_failed",
                    error_type=type(cleanup_error).__name__,
                    error=str(cleanup_error),
                    level=logging.WARNING,
                )
                self._release_scan_owner_bus(
                    bus,
                    reason="start-timeout-cleanup-failed",
                )
            raise asyncio.TimeoutError(
                f"BlueZ StartDiscovery timed out after {SCAN_DBUS_TIMEOUT_SEC:g}s"
            ) from start_error

    async def _call_bluez_stop_discovery(self) -> None:
        """The actual bluez StopDiscovery call. Pulled out of
        stop_discovery() so the auto-stop task can use it without
        cancelling itself mid-await."""
        bus = self._bus
        if bus is None:
            return
        path = f"/org/bluez/{self._adapter}"

        async def _stop() -> None:
            intro = await bus.introspect(BLUEZ_BUS, path)
            adapter = bus.get_proxy_object(
                BLUEZ_BUS,
                path,
                intro,
            ).get_interface("org.bluez.Adapter1")
            await adapter.call_stop_discovery()

        try:
            await asyncio.wait_for(_stop(), timeout=SCAN_DBUS_TIMEOUT_SEC)
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError(
                f"BlueZ StopDiscovery timed out after {SCAN_DBUS_TIMEOUT_SEC:g}s"
            ) from error
        except DBusError as e:
            if _stop_discovery_already_idle(e):
                logger.debug("stop_discovery already idle: %s", e)
                return
            raise

    async def pair(
        self,
        mac: str,
        *,
        timeout_s: float = 60.0,
    ) -> AsyncIterator[dict]:
        """Pair the device at `mac`. Yields status events for SSE.

        Events:
          {"stage": "starting"}
          {"stage": "trusting"}
          {"stage": "pairing"}
          {"stage": "paired"}
          {"stage": "connecting"}
          {"stage": "wiring", "detail": ...}    (handler-specific)
          {"stage": "ready", "detail": ...}     (terminal — success)
          {"stage": "error", "message": ...}    (terminal — failure)
        """
        try:
            await self._recover_bus_if_required()
        except SCAN_OPERATION_ERRORS as error:
            yield {
                "stage": "error",
                "message": f"Bluetooth controller recovery failed: {error}",
            }
            return
        bus = self._bus
        if bus is None:
            yield {"stage": "error", "message": "bluetooth engine not started"}
            return

        # Find the device. The observer cache is updated continuously,
        # so a recently-scanned MAC will be there.
        dev = self._observer.get_by_mac(mac)
        if dev is None:
            yield {
                "stage": "error",
                "message": f"device {mac} not found — make sure it's "
                "advertising (was it in pair mode?)",
            }
            return

        yield {"stage": "starting", "name": dev.name, "address": dev.address}

        # Trust early so a successful pair stays trusted even if the user closes
        # the browser tab before the connect/handler stages finish. Doesn't
        # grant Connect — that's a separate call below.
        yield {"stage": "trusting"}
        dev_intro = await bus.introspect(BLUEZ_BUS, dev.path)
        dev_obj = bus.get_proxy_object(BLUEZ_BUS, dev.path, dev_intro)
        dev_iface = dev_obj.get_interface("org.bluez.Device1")
        dev_props = dev_obj.get_interface(
            "org.freedesktop.DBus.Properties",
        )
        try:
            await dev_props.call_set(
                "org.bluez.Device1",
                "Trusted",
                Variant("b", True),
            )
        except DBusError as e:
            logger.warning("Trust set failed (continuing): %s", e)

        yield {"stage": "pairing"}
        try:
            await self._call_pair_with_timeout(dev_iface, timeout_s)
        except asyncio.CancelledError:
            yield {"stage": "error", "message": "pair operation was cancelled"}
            return
        except Exception as err:  # noqa: BLE001
            yield {"stage": "error", "message": _format_dbus_error(err)}
            return

        yield {"stage": "paired", "address": dev.address}

        # Re-fetch device props post-pair so connection / uuid lists
        # reflect post-pairing state.
        dev = await self._refresh_device(dev.path) or dev

        # Connect for device classes that support it.
        yield {"stage": "connecting"}
        try:
            await dev_iface.call_connect()
        except DBusError as e:
            # Some devices (BLE-only sensors, GATT peripherals)
            # may not support a generic Connect — record the
            # error but continue to the handler. The handler can
            # decide whether to retry or accept partial setup.
            logger.info("connect failed (continuing): %s", e)

        # Per-class post-pair routing.
        dev = await self._refresh_device(dev.path) or dev
        handler = pick(dev)
        self._roles.set(dev.address, handler.id)
        reconciled = False
        async for evt in handler.post_pair(dev):
            if "error" in evt:
                yield {**evt, "handler": handler.id}
                return
            if evt.get("stage") == "ready" and not reconciled:
                yield {
                    "stage": "wiring",
                    "detail": "Refreshing optional accessory profiles.",
                    "handler": handler.id,
                }
                reconciled = True
                if not await self._reconcile_accessories("bluetooth-pair"):
                    yield {
                        "stage": "wiring",
                        "detail": (
                            "Paired. Optional accessory features will retry "
                            "at boot if they are not active yet."
                        ),
                        "handler": handler.id,
                    }
            yield {**evt, "handler": handler.id}
        if not reconciled:
            await self._reconcile_accessories("bluetooth-pair")

    async def connect(self, mac: str) -> tuple[bool, str]:
        """Reconnect a paired device. Returns (ok, message)."""
        try:
            await self._recover_bus_if_required()
        except SCAN_OPERATION_ERRORS as error:
            return False, f"Bluetooth controller recovery failed: {error}"
        bus = self._bus
        dev = self._observer.get_by_mac(mac)
        if dev is None or bus is None:
            return False, "device not found"
        try:
            intro = await bus.introspect(BLUEZ_BUS, dev.path)
            iface = bus.get_proxy_object(
                BLUEZ_BUS,
                dev.path,
                intro,
            ).get_interface("org.bluez.Device1")
            await iface.call_connect()
            if not await self._reconcile_accessories("bluetooth-connect"):
                return True, "connected; optional accessory refresh will retry at boot"
            return True, "connected"
        except DBusError as e:
            return False, _format_dbus_error(e)

    async def disconnect(self, mac: str) -> tuple[bool, str]:
        try:
            await self._recover_bus_if_required()
        except SCAN_OPERATION_ERRORS as error:
            return False, f"Bluetooth controller recovery failed: {error}"
        bus = self._bus
        dev = self._observer.get_by_mac(mac)
        if dev is None or bus is None:
            return False, "device not found"
        try:
            intro = await bus.introspect(BLUEZ_BUS, dev.path)
            iface = bus.get_proxy_object(
                BLUEZ_BUS,
                dev.path,
                intro,
            ).get_interface("org.bluez.Device1")
            await iface.call_disconnect()
            return True, "disconnected"
        except DBusError as e:
            return False, _format_dbus_error(e)

    async def forget(self, mac: str) -> tuple[bool, str]:
        """Remove a known device from bluez.

        This clears pair/link-key state for paired devices and also removes
        stale BLE cache records for devices that are connected/trusted but no
        longer paired.
        """
        from .adapter import remove_device

        ok, msg = await remove_device(mac, self._adapter)
        log_event(
            logger,
            "bluetooth.device_forget",
            address=mac,
            ok=ok,
            message=msg,
            level=logging.INFO if ok else logging.WARNING,
        )
        if ok:
            self._roles.remove(mac)
            if not await self._reconcile_accessories("bluetooth-forget"):
                return True, f"{msg}; optional accessory refresh will retry at boot"
        return ok, msg

    # ---------- internals ----------

    async def _reconcile_accessories(self, reason: str) -> bool:
        try:
            await self._accessory_reconcile(reason)
            return True
        except ACCESSORY_RECONCILE_ERRORS as exc:
            log_event(
                logger,
                "bluetooth.accessory_reconcile_failed",
                reason=reason,
                err=str(exc),
                level=logging.WARNING,
            )
            return False

    async def _call_pair_with_timeout(self, dev_iface, timeout_s: float):
        try:
            await asyncio.wait_for(dev_iface.call_pair(), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise DBusError(
                "org.bluez.Error.AuthenticationTimeout",
                f"pair timed out after {int(timeout_s)} s",
            ) from e

    async def _refresh_device(self, path: str) -> BluetoothDevice | None:
        """Re-read a device's properties from bluez after state-
        changing calls (Pair, Connect). Used to keep the handler
        view in sync with reality."""
        if self._bus is None:
            return None
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, path)
            props = self._bus.get_proxy_object(
                BLUEZ_BUS,
                path,
                intro,
            ).get_interface("org.freedesktop.DBus.Properties")
            all_props = await props.call_get_all("org.bluez.Device1")
            return BluetoothDevice.from_props(path, all_props)
        except DBusError:
            return None


def _format_dbus_error(err: BaseException) -> str:
    """Turn a DBusError into a user-friendly message. Maps known
    bluez error names to the iPhone-equivalent copy."""
    if not isinstance(err, DBusError):
        return str(err)
    name = err.type or ""
    msg = str(err)
    if "AuthenticationTimeout" in name or "AuthenticationTimeout" in msg:
        return (
            "Pairing took too long. Make sure the device is in range "
            "and in pair mode, then try again."
        )
    if "AuthenticationCanceled" in name:
        return "Pairing was cancelled."
    if "AuthenticationRejected" in name:
        return "Pairing was rejected by the device."
    if "AuthenticationFailed" in name:
        return "Pairing failed. The link key didn't match."
    if "ConnectionAttemptFailed" in name:
        return "Could not connect. Try moving the device closer and retrying."
    if "AlreadyExists" in name:
        return "This device is already paired."
    if "InProgress" in name:
        return "Bluetooth is busy. Try again in a moment."
    return msg or "Unknown bluetooth error."


__all__ = ["BluetoothEngine", "REGISTRY"]
