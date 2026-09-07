# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pair state machine.

One generic flow regardless of device class:

  pair → trust → connect → handler

Async generator yielding StatusEvent dicts; the web layer streams them
over SSE. Per-class behaviour is dispatched through `handlers.pick()`.

Designed to be run inside one long-lived `BluetoothEngine` instance
on the daemon. Each `pair(mac)` call is independent. Pairing
authorization is owned by the JTS no-code default agent, which is not
always running — a low-memory deploy parks bt-agent.service for the
length of its build.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import AsyncIterator, TypeVar

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

from jasper.log_event import log_event

from .handlers import pick
from .models import (
    BluetoothActionResult,
    BluetoothDevice,
    adapter_not_ready_result,
)
from .scan import DeviceObserver

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEFAULT_ADAPTER = "hci0"
SCAN_DBUS_TIMEOUT_SEC = 5.0
CONNECT_TIMEOUT_S = 30.0
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
ACCESSORY_RECONCILE_ERRORS = (OSError,)


async def _default_accessory_reconcile(reason: str) -> object:
    """Request one accessory pass from its root owner.

    This engine runs inside jasper-web, which holds no systemd privilege: it
    publishes a request file that jasper-accessory-reconcile.path acts on. One
    small atomic write, so it stays inline on the request's own task.
    """
    from jasper.accessories.reconcile import request_reconcile

    request_reconcile(reason)
    return None


_T = TypeVar("_T")


async def _await_with_timeout(
    awaitable: Awaitable[_T],
    timeout_s: float,
    error_name: str,
    message: str,
) -> _T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise DBusError(error_name, message) from e


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


@contextlib.asynccontextmanager
async def _bondable_for_pair(adapter: str):
    """Raise the adapter's bondable flag for one pairing request.

    BlueZ reads `Pairable` when it builds the SMP pairing request, and with
    it low the request carries "No bonding": the pair succeeds, no long-term
    key is stored, and the bond dies on the next disconnect. Restored
    afterwards so a pair does not leave the speaker admitting inbound
    pairings outside its own window.

    Best-effort on both edges. A speaker that cannot raise the flag should
    still attempt the pair -- unbonded is how it behaved before this existed,
    and `untrust_unbonded` keeps that from stranding the device.
    """
    from .adapter import set_pairable, state as adapter_state

    # Tri-state on purpose. `False` and "could not read" are different
    # answers: treating an unreadable adapter as "was off" makes the restore
    # lower Pairable under a pairing window the user opened, leaving
    # Discoverable=yes with Pairable=no -- which the floor watch never heals,
    # because it only closes a window that is still pairable.
    was_pairable: bool | None = None
    try:
        was_pairable = bool((await adapter_state(adapter)).get("pairable"))
        await set_pairable(True, adapter)
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            "bluetooth.bondable_raise_failed",
            err=repr(exc),
            level=logging.WARNING,
        )
    try:
        yield
    finally:
        if was_pairable is False:
            try:
                await set_pairable(False, adapter)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    "bluetooth.bondable_restore_failed",
                    err=repr(exc),
                    level=logging.WARNING,
                )


class BluetoothEngine:
    """Owns the bus connection + observer. Singleton on the
    daemon. Pair streams progress events; connect, disconnect, and forget
    return one structured result."""

    def __init__(
        self,
        adapter: str = DEFAULT_ADAPTER,
        *,
        accessory_reconcile: AccessoryReconciler | None = None,
    ) -> None:
        self._adapter = adapter
        self._bus: MessageBus | None = None
        self._observer = DeviceObserver()
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
          {"stage": "pairing"}
          {"stage": "paired"}
          {"stage": "trusting"}
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

        dev_intro = await bus.introspect(BLUEZ_BUS, dev.path)
        dev_obj = bus.get_proxy_object(BLUEZ_BUS, dev.path, dev_intro)
        dev_iface = dev_obj.get_interface("org.bluez.Device1")
        dev_props = dev_obj.get_interface(
            "org.freedesktop.DBus.Properties",
        )

        yield {"stage": "pairing"}
        try:
            async with _bondable_for_pair(self._adapter):
                await _await_with_timeout(
                    dev_iface.call_pair(),
                    timeout_s,
                    "org.bluez.Error.AuthenticationTimeout",
                    f"pair timed out after {int(timeout_s)} s",
                )
        except asyncio.CancelledError:
            yield {"stage": "error", "message": "pair operation was cancelled"}
            return
        except Exception as err:  # noqa: BLE001
            yield {"stage": "error", "message": _classify_dbus_error(err)[0]}
            return

        yield {"stage": "paired", "address": dev.address}

        # Only a bonded device may be trusted — why:
        # `NoCodeAgent._trust_device`. Here the bond is guaranteed by
        # position: Pair() has returned successfully. Set before the
        # connect/handler stages so trust survives the user closing the
        # browser tab mid-flow. Doesn't grant Connect — separate call below.
        yield {"stage": "trusting"}
        try:
            await dev_props.call_set(
                "org.bluez.Device1",
                "Trusted",
                Variant("b", True),
            )
        except DBusError as e:
            logger.warning("Trust set failed (continuing): %s", e)

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
        reconciled = False
        async for evt in handler.post_pair(dev):
            if "error" in evt:
                yield {**evt, "handler": handler.id}
                return
            if evt.get("stage") == "ready" and not reconciled:
                yield {
                    "stage": "wiring",
                    "detail": "Requested an accessory profile refresh.",
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

    async def connect(self, mac: str) -> BluetoothActionResult:
        """Reconnect a paired device."""
        try:
            await self._recover_bus_if_required()
        except SCAN_OPERATION_ERRORS as error:
            return BluetoothActionResult(
                False,
                f"Bluetooth controller recovery failed: {error}",
            )
        bus = self._bus
        dev = self._observer.get_by_mac(mac)
        if dev is None or bus is None:
            return BluetoothActionResult(False, "device not found")
        try:
            intro = await bus.introspect(BLUEZ_BUS, dev.path)
            iface = bus.get_proxy_object(
                BLUEZ_BUS,
                dev.path,
                intro,
            ).get_interface("org.bluez.Device1")
            await _await_with_timeout(
                iface.call_connect(),
                CONNECT_TIMEOUT_S,
                "org.bluez.Error.Failed",
                "connect-timeout",
            )
            if not await self._reconcile_accessories("bluetooth-connect"):
                return BluetoothActionResult(
                    True,
                    "connected; optional accessory refresh will retry at boot",
                )
            return BluetoothActionResult(True, "connected")
        except DBusError as e:
            return _device_action_error(e)

    async def disconnect(self, mac: str) -> BluetoothActionResult:
        try:
            await self._recover_bus_if_required()
        except SCAN_OPERATION_ERRORS as error:
            return BluetoothActionResult(
                False,
                f"Bluetooth controller recovery failed: {error}",
            )
        bus = self._bus
        dev = self._observer.get_by_mac(mac)
        if dev is None or bus is None:
            return BluetoothActionResult(False, "device not found")
        try:
            intro = await bus.introspect(BLUEZ_BUS, dev.path)
            iface = bus.get_proxy_object(
                BLUEZ_BUS,
                dev.path,
                intro,
            ).get_interface("org.bluez.Device1")
            await iface.call_disconnect()
            return BluetoothActionResult(True, "disconnected")
        except DBusError as e:
            return _device_action_error(e)

    async def forget(self, mac: str) -> BluetoothActionResult:
        """Remove a known device from bluez.

        This clears pair/link-key state for paired devices and also removes
        stale BLE cache records for devices that are connected/trusted but no
        longer paired.
        """
        from .adapter import remove_device

        try:
            await remove_device(mac, self._adapter)
            result = BluetoothActionResult(True, "removed")
        except DBusError as error:
            result = _device_action_error(error)
        log_event(
            logger,
            "bluetooth.device_forget",
            address=mac,
            ok=result.ok,
            message=result.message,
            level=logging.INFO if result.ok else logging.WARNING,
        )
        if result.ok and not await self._reconcile_accessories("bluetooth-forget"):
            return BluetoothActionResult(
                True,
                f"{result.message}; optional accessory refresh will retry at boot",
            )
        return result

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


# BlueZ >= 5.63 reports Device1.Connect failures as org.bluez.Error.Failed
# with the message set to a reason token rather than a distinct error name.
_NOT_ANSWERING = (
    "The device didn't answer. Make sure its Bluetooth is on and it's nearby."
)
_REFUSED = (
    "The device refused. If you removed JTS on the device, Forget it here "
    "and pair again."
)
_BUSY = "Bluetooth is busy. Try again in a moment."
_CONNECT_FAILURE_REASONS: dict[str, str] = {
    **dict.fromkeys(
        (
            "br-connection-page-timeout",
            "le-connection-abort-by-local",
            "connect-timeout",
            "br-connection-timeout",
            "le-connection-timeout",
        ),
        _NOT_ANSWERING,
    ),
    **dict.fromkeys(
        (
            "br-connection-refused",
            "br-connection-key-missing",
            "le-connection-refused",
            "br-connection-aborted-by-remote",
            "le-connection-abort-by-remote",
        ),
        _REFUSED,
    ),
    "br-connection-profile-unavailable": (
        "No usable profile between this speaker and the device. For a "
        "phone, check Bluetooth is on in Sources."
    ),
    "br-connection-busy": _BUSY,
    "br-connection-canceled": "Connection was cancelled.",
}

# Pre-5.63 pair/connect failures, still reported as their own error names.
_NAME_ERROR_REASONS: dict[str, str] = {
    "AuthenticationTimeout": (
        "Pairing took too long. Make sure the device is in range "
        "and in pair mode, then try again."
    ),
    "AuthenticationCanceled": "Pairing was cancelled.",
    "AuthenticationRejected": "Pairing was rejected by the device.",
    "AuthenticationFailed": "Pairing failed. The link key didn't match.",
    "ConnectionAttemptFailed": (
        "Could not connect. Try moving the device closer and retrying."
    ),
    "AlreadyExists": "This device is already paired.",
    "InProgress": _BUSY,
}


def _classify_dbus_error(err: BaseException) -> tuple[str, str | None]:
    """Map a DBusError to (user-facing message, BlueZ reason code)."""
    if not isinstance(err, DBusError):
        return str(err), None
    name = err.type or ""
    msg = str(err)
    if msg in _CONNECT_FAILURE_REASONS:
        return _CONNECT_FAILURE_REASONS[msg], msg
    short_name = name.rsplit(".", 1)[-1]
    if "AuthenticationTimeout" in name or "AuthenticationTimeout" in msg:
        short_name = "AuthenticationTimeout"
    if short_name in _NAME_ERROR_REASONS:
        return _NAME_ERROR_REASONS[short_name], short_name
    return msg or "Unknown bluetooth error.", None


def _device_action_error(err: DBusError) -> BluetoothActionResult:
    if err.type == "org.bluez.Error.NotReady" or str(err).endswith("adapter-not-powered"):
        return adapter_not_ready_result()
    message, code = _classify_dbus_error(err)
    return BluetoothActionResult(False, message, code)


__all__ = ["BluetoothEngine"]
