"""Pair state machine.

One generic flow regardless of device class:

  trust → pair → (maybe agent prompts → user responds) → connect → handler

Async generator yielding StatusEvent dicts; the web layer streams them
over SSE. Per-class behaviour is dispatched through `handlers.pick()`.

Designed to be run inside one long-lived `BluetoothEngine` instance
on the daemon (the engine holds the bus + agent registration). Each
`pair(mac)` call is independent — concurrent calls for different
devices share the same agent but get their own PromptHandle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

from .agent import Agent, PromptHandle, register_agent, unregister_agent
from .handlers import REGISTRY, pick
from .models import BluetoothDevice
from .roles import RoleStore
from .scan import DeviceObserver

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEFAULT_ADAPTER = "hci0"


class BluetoothEngine:
    """Owns the bus connection + agent + observer. Singleton on the
    daemon; exposes pair / connect / disconnect / forget as async
    generators yielding status events."""

    def __init__(self, adapter: str = DEFAULT_ADAPTER) -> None:
        self._adapter = adapter
        self._bus: MessageBus | None = None
        self._agent: Agent | None = None
        self._observer = DeviceObserver()
        self._roles = RoleStore()
        # One PromptHandle map across the engine (shared with the
        # agent). The engine creates handles per pair attempt; the
        # web layer resolves them via `respond_prompt()`.
        self._prompts: dict[str, PromptHandle] = {}
        # Active scan auto-stop task. bluez auto-stops discovery when
        # the initiating bus client disconnects, so the engine OWNS
        # discovery on its long-lived bus — `adapter.start_discovery()`
        # would lose the scan the moment its ephemeral bus closed.
        self._scan_task: asyncio.Task | None = None

    @property
    def observer(self) -> DeviceObserver:
        return self._observer

    @property
    def roles(self) -> RoleStore:
        return self._roles

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._agent = Agent()
        await register_agent(self._bus, self._agent)
        await self._observer.start()

    async def stop(self) -> None:
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
        if self._bus is not None:
            try:
                await unregister_agent(self._bus)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
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
        if self._bus is None:
            return
        path = f"/org/bluez/{self._adapter}"
        intro = await self._bus.introspect(BLUEZ_BUS, path)
        a = self._bus.get_proxy_object(
            BLUEZ_BUS, path, intro,
        ).get_interface("org.bluez.Adapter1")
        try:
            await a.call_start_discovery()
        except DBusError as e:
            if "in progress" not in str(e).lower():
                raise

        # Replace the auto-stop task. Cancel any prior task; bluez
        # discovery only runs once at a time, so a new "scan" click
        # extends the deadline rather than stacking.
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
        self._scan_task = asyncio.create_task(
            self._auto_stop_scan(duration_s),
        )

    async def _auto_stop_scan(self, duration_s: float) -> None:
        try:
            await asyncio.sleep(duration_s)
        except asyncio.CancelledError:
            return
        # IMPORTANT: don't call stop_discovery() here — that would
        # cancel `self._scan_task`, and `self._scan_task` IS us.
        # Self-cancellation propagates CancelledError into the next
        # await (the bluez introspect call), so the StopDiscovery
        # bluez call never lands and the radio keeps scanning.
        # Drive the bluez call directly instead.
        try:
            await self._call_bluez_stop_discovery()
        except Exception as e:  # noqa: BLE001
            logger.warning("scan auto-stop failed: %s", e)

    async def stop_discovery(self) -> None:
        """External entry point. Cancels the auto-stop task (we're
        stopping early) and tells bluez to stop discovery."""
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()
        await self._call_bluez_stop_discovery()

    async def _call_bluez_stop_discovery(self) -> None:
        """The actual bluez StopDiscovery call. Pulled out of
        stop_discovery() so the auto-stop task can use it without
        cancelling itself mid-await."""
        if self._bus is None:
            return
        path = f"/org/bluez/{self._adapter}"
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, path)
            a = self._bus.get_proxy_object(
                BLUEZ_BUS, path, intro,
            ).get_interface("org.bluez.Adapter1")
            await a.call_stop_discovery()
        except DBusError as e:
            # "Not Authorized" / "No Discovery started" are both fine
            # — caller doesn't need to care whether we were scanning.
            logger.debug("stop_discovery non-fatal: %s", e)

    async def pair(
        self, mac: str, *, timeout_s: float = 60.0,
    ) -> AsyncIterator[dict]:
        """Pair the device at `mac`. Yields status events for SSE.

        Events:
          {"stage": "starting"}
          {"stage": "trusting"}
          {"stage": "pairing"}
          {"stage": "confirm_passkey", "passkey": 123456}   (numeric comparison)
          {"stage": "request_passkey"}                       (passkey entry)
          {"stage": "request_pincode"}                       (legacy)
          {"stage": "display_passkey", "passkey": 123456}    (we show, remote types)
          {"stage": "paired"}
          {"stage": "connecting"}
          {"stage": "wiring", "detail": ...}    (handler-specific)
          {"stage": "ready", "detail": ...}     (terminal — success)
          {"stage": "error", "message": ...}    (terminal — failure)
        """
        if self._bus is None:
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

        # Register the prompt handle BEFORE Pair() — bluez can call
        # the agent synchronously inside Pair(), so the handle must
        # exist by then.
        handle = PromptHandle(device_path=dev.path)
        self._agent.register_prompt(handle)
        self._prompts[dev.address.upper()] = handle

        # Future that we forward to the web layer when bluez asks us
        # an interactive question.
        prompt_awaiter = asyncio.create_task(self._await_prompt(handle))

        try:
            # Trust early so a successful pair stays trusted even if
            # the user closes the browser tab before we re-set Trust
            # at the end. Doesn't grant Connect — that's a separate
            # call below.
            yield {"stage": "trusting"}
            dev_intro = await self._bus.introspect(BLUEZ_BUS, dev.path)
            dev_obj = self._bus.get_proxy_object(BLUEZ_BUS, dev.path, dev_intro)
            dev_iface = dev_obj.get_interface("org.bluez.Device1")
            dev_props = dev_obj.get_interface(
                "org.freedesktop.DBus.Properties",
            )
            try:
                await dev_props.call_set(
                    "org.bluez.Device1", "Trusted", Variant("b", True),
                )
            except DBusError as e:
                logger.warning(
                    "Trust set failed (continuing): %s", e,
                )

            yield {"stage": "pairing"}

            # Run Pair() and prompt-awaiter concurrently. The first to
            # surface meaningful data wins:
            #   - prompt_awaiter resolves → emit the prompt stage,
            #     keep awaiting Pair() (the agent's reply will let
            #     bluez continue and Pair() will then resolve).
            #   - Pair() resolves → pair finished, cancel prompt_awaiter.
            pair_task = asyncio.create_task(
                self._call_pair_with_timeout(dev_iface, timeout_s),
            )

            while not pair_task.done():
                done, _ = await asyncio.wait(
                    {pair_task, prompt_awaiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if prompt_awaiter in done and not pair_task.done():
                    # Surface the prompt to the SSE consumer.
                    prompt_event = prompt_awaiter.result()
                    if prompt_event is None:
                        # Prompt was already resolved (e.g., handle
                        # closed by Cancel). Re-arm waiter.
                        prompt_awaiter = asyncio.create_task(
                            self._await_prompt(handle),
                        )
                    else:
                        yield prompt_event
                        # Re-arm waiter for the next possible prompt
                        # (e.g., DisplayPasskey + later confirmation).
                        prompt_awaiter = asyncio.create_task(
                            self._await_prompt(
                                self._fresh_handle(dev.path),
                            ),
                        )
                if pair_task in done:
                    break

            try:
                err = pair_task.exception()
                if err is not None:
                    yield {"stage": "error",
                           "message": _format_dbus_error(err)}
                    return
            except asyncio.CancelledError:
                yield {"stage": "error",
                       "message": "pair operation was cancelled"}
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
            async for evt in handler.post_pair(dev):
                yield {**evt, "handler": handler.id}
                if "error" in evt:
                    return

        finally:
            self._agent.clear_prompt(dev.path)
            self._prompts.pop(dev.address.upper(), None)
            if not prompt_awaiter.done():
                prompt_awaiter.cancel()

    def respond_prompt(
        self, mac: str, *, accept: bool, value: str | int | None = None,
    ) -> bool:
        """Resolve a pending interactive prompt for `mac`. Returns
        True if a prompt existed, False otherwise (the web layer
        treats False as "no prompt was waiting — the user clicked
        too late or for a stale device")."""
        handle = self._prompts.get(mac.upper())
        if handle is None or handle.future.done():
            return False
        handle.respond(accept=accept, value=value)
        return True

    async def connect(self, mac: str) -> tuple[bool, str]:
        """Reconnect a paired device. Returns (ok, message)."""
        dev = self._observer.get_by_mac(mac)
        if dev is None or self._bus is None:
            return False, "device not found"
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, dev.path)
            iface = self._bus.get_proxy_object(
                BLUEZ_BUS, dev.path, intro,
            ).get_interface("org.bluez.Device1")
            await iface.call_connect()
            return True, "connected"
        except DBusError as e:
            return False, _format_dbus_error(e)

    async def disconnect(self, mac: str) -> tuple[bool, str]:
        dev = self._observer.get_by_mac(mac)
        if dev is None or self._bus is None:
            return False, "device not found"
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, dev.path)
            iface = self._bus.get_proxy_object(
                BLUEZ_BUS, dev.path, intro,
            ).get_interface("org.bluez.Device1")
            await iface.call_disconnect()
            return True, "disconnected"
        except DBusError as e:
            return False, _format_dbus_error(e)

    async def forget(self, mac: str) -> tuple[bool, str]:
        """Remove the device from bluez (clears pair, link key, etc.)
        and our role map."""
        from .adapter import remove_device

        ok, msg = await remove_device(mac, self._adapter)
        if ok:
            self._roles.remove(mac)
        return ok, msg

    # ---------- internals ----------

    async def _call_pair_with_timeout(self, dev_iface, timeout_s: float):
        try:
            await asyncio.wait_for(dev_iface.call_pair(), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise DBusError(
                "org.bluez.Error.AuthenticationTimeout",
                f"pair timed out after {int(timeout_s)} s",
            ) from e

    def _fresh_handle(self, device_path: str) -> PromptHandle:
        """Replace the agent's prompt handle for a new prompt — used
        when an earlier prompt (e.g. DisplayPasskey) has been surfaced
        and the next prompt (e.g. confirmation) may come."""
        if self._agent is None:
            return PromptHandle(device_path=device_path)
        handle = PromptHandle(device_path=device_path)
        self._agent.register_prompt(handle)
        # Update the engine-level lookup by MAC too.
        dev = self._observer.get(device_path)
        if dev is not None:
            self._prompts[dev.address.upper()] = handle
        return handle

    async def _await_prompt(self, handle: PromptHandle) -> dict | None:
        """Wait for `handle.kind` to become non-pending, then return
        an SSE event describing the prompt. The event is yielded to
        the caller, who awaits the user's response via
        `respond_prompt()` — which resolves `handle.future` and lets
        the agent unblock."""
        # Poll the handle's kind field — dbus-next agent callbacks
        # set it before awaiting future. A small sleep makes this
        # efficient; alternative is an asyncio.Event but the
        # complexity isn't worth it for prompt latency on a knob.
        # Wait up to the engine timeout (Pair() may resolve before
        # any prompt fires — Just Works case).
        for _ in range(600):
            await asyncio.sleep(0.1)
            if handle.kind != "pending":
                if handle.kind == "confirm":
                    return {
                        "stage": "confirm_passkey",
                        "passkey": handle.passkey,
                    }
                if handle.kind == "passkey":
                    return {"stage": "request_passkey"}
                if handle.kind == "pincode":
                    return {"stage": "request_pincode"}
                if handle.kind == "display_passkey":
                    return {
                        "stage": "display_passkey",
                        "passkey": handle.passkey,
                    }
            if handle.future.done():
                return None
        return None

    async def _refresh_device(self, path: str) -> BluetoothDevice | None:
        """Re-read a device's properties from bluez after state-
        changing calls (Pair, Connect). Used to keep the handler
        view in sync with reality."""
        if self._bus is None:
            return None
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, path)
            props = self._bus.get_proxy_object(
                BLUEZ_BUS, path, intro,
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
        return (
            "Could not connect. Try moving the device closer and "
            "retrying."
        )
    if "AlreadyExists" in name:
        return "This device is already paired."
    if "InProgress" in name:
        return "Bluetooth is busy. Try again in a moment."
    return msg or "Unknown bluetooth error."


__all__ = ["BluetoothEngine", "REGISTRY"]
