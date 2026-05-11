"""DBus bluez agent — handles pairing prompts.

Capability is `DisplayYesNo`, which is the right choice for a Pi with
a web UI (no physical keyboard or display on the Pi itself, but the
user has a browser they can click in). Per the BT Core Spec SSP table:

  Local DisplayYesNo × Remote DisplayYesNo  → Numeric Comparison
        (phone, computer: "do these 6 digits match? [Yes/No]")
  Local DisplayYesNo × Remote KeyboardOnly  → Passkey Entry
        (BT keyboards: we show 6 digits, user types on remote)
  Local DisplayYesNo × Remote DisplayOnly   → Passkey Entry
        (some headsets: read code off remote, type it on Pi)
  Local DisplayYesNo × Remote NoInputNoOutput → Just Works
        (headphones, speakers, knobs, BLE: auto-accept)
  Legacy (pre-SSP) device                   → Legacy PIN

The agent surfaces interactive prompts to the engine via asyncio
Futures keyed by device D-Bus path. The engine registers a
PromptHandle before calling `Pair()`; bluez calls back here (e.g.,
RequestConfirmation); we resolve the handle's `future` with the
prompt details so the web layer can render the right UI; we then
await the user's response and return it to bluez.

Pre-2.1 PIN-entry and "we read code off remote display" cases use
the same PromptHandle pattern; the web UI shows a text/number input
instead of Yes/No buttons.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore
from dbus_next.service import ServiceInterface, method  # type: ignore

logger = logging.getLogger(__name__)


# Bluez raises this name for "user said no / agent rejected".
REJECTED_DBUS_NAME = "org.bluez.Error.Rejected"


@dataclass
class PromptHandle:
    """A pending interactive prompt from bluez. The engine awaits
    `future`; the web layer (via the engine) calls `.respond()`
    when the user clicks something."""

    device_path: str
    kind: str = "pending"   # "confirm" | "passkey" | "pincode" | "display_passkey"
    passkey: int | None = None
    future: asyncio.Future = field(default_factory=asyncio.Future)

    def respond(self, *, accept: bool, value: str | int | None = None) -> None:
        if self.future.done():
            return
        if not accept:
            self.future.set_exception(_BluezReject("user declined"))
            return
        self.future.set_result(value)


class _BluezReject(Exception):
    """Internal exception that dbus-next maps to org.bluez.Error.Rejected
    when raised inside an agent method body (via _DBUS_ERROR_NAME attr)."""
    _DBUS_ERROR_NAME = REJECTED_DBUS_NAME


class Agent(ServiceInterface):
    """The bluez agent. Multi-call: one agent serves many concurrent
    pair attempts. Engine instances register PromptHandles keyed by
    device path; the agent dispatches callbacks back to them."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")
        self._prompts: dict[str, PromptHandle] = {}

    def register_prompt(self, handle: PromptHandle) -> None:
        self._prompts[handle.device_path] = handle

    def clear_prompt(self, device_path: str) -> None:
        self._prompts.pop(device_path, None)

    async def _await_response(
        self, kind: str, device_path: str, passkey: int | None = None,
    ):
        """Resolve the engine's PromptHandle with this prompt's kind/
        passkey, then await the future for the user's response.
        Raises Rejected (mapped to org.bluez.Error.Rejected) if no
        handle is registered — bluez treats that as a pair rejection."""
        handle = self._prompts.get(device_path)
        if handle is None:
            logger.warning(
                "agent: %s(%s) but no prompt handle registered; rejecting",
                kind, device_path,
            )
            raise _BluezReject("no pending prompt for device")
        handle.kind = kind
        handle.passkey = passkey
        try:
            return await handle.future
        finally:
            self.clear_prompt(device_path)

    # ---------- DBus agent methods ----------
    # dbus-next reads param annotations as DBus signature fragments
    # ("o" = object path, "s" = string, "u" = uint32, "q" = uint16).
    # Methods returning a value to bluez carry a return annotation;
    # void-returning methods omit `-> None` (the decorator rejects
    # `None` as a non-string annotation).
    #
    # Methods can be async — dbus-next awaits them. That lets us use
    # asyncio Futures inside without re-entering the event loop.

    @method()
    def Release(self):  # noqa: N802 (DBus methods are CamelCase)
        pass

    @method()
    async def RequestPinCode(self, device: "o") -> "s":  # type: ignore  # noqa: N802
        value = await self._await_response("pincode", device)
        return str(value or "")

    @method()
    def DisplayPinCode(  # noqa: N802
        self, device: "o", pincode: "s",  # type: ignore
    ):
        logger.info(
            "agent: display PIN %s for %s — but we have no display; "
            "user must read this from elsewhere",
            pincode, device,
        )

    @method()
    async def RequestPasskey(self, device: "o") -> "u":  # type: ignore  # noqa: N802
        value = await self._await_response("passkey", device)
        return int(value or 0)

    @method()
    def DisplayPasskey(  # noqa: N802
        self, device: "o", passkey: "u", entered: "q",  # type: ignore
    ):
        # The remote needs the user to type a code; we show it.
        # bluez calls this repeatedly with `entered` ticking up as
        # the remote types — only surface on the first call.
        if entered == 0:
            asyncio.create_task(self._surface_display_passkey(device, passkey))

    async def _surface_display_passkey(self, device: str, passkey: int) -> None:
        handle = self._prompts.get(device)
        if handle is None:
            return
        handle.kind = "display_passkey"
        handle.passkey = int(passkey)
        # Don't resolve future — the remote types the digits and
        # the pair completes on its own. The web UI just displays
        # the digits with a "type these on your device" hint.

    @method()
    async def RequestConfirmation(  # noqa: N802
        self, device: "o", passkey: "u",  # type: ignore
    ):
        # SSP Numeric Comparison — the iPhone-style "do these match?".
        await self._await_response("confirm", device, passkey=int(passkey))

    @method()
    def RequestAuthorization(self, device: "o"):  # type: ignore  # noqa: N802
        # Just Works case. We always accept — the user already
        # initiated the pair by clicking the device in the list.
        logger.info("agent: auto-authorizing %s", device)

    @method()
    def AuthorizeService(  # noqa: N802
        self, device: "o", uuid: "s",  # type: ignore
    ):
        logger.info("agent: auto-authorizing service %s on %s", uuid, device)

    @method()
    def Cancel(self):  # noqa: N802
        logger.info("agent: Cancel()")


async def register_agent(
    bus: MessageBus,
    agent: Agent,
    *,
    agent_path: str = "/com/jasper/bt/agent",
    capability: str = "DisplayYesNo",
) -> None:
    """Export the agent on `bus` and register as the default bluez agent.
    Safe to call multiple times — re-export / re-register are swallowed."""
    try:
        bus.export(agent_path, agent)
    except Exception:  # noqa: BLE001
        pass
    intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object(
        "org.bluez", "/org/bluez", intro,
    ).get_interface("org.bluez.AgentManager1")
    try:
        await mgr.call_register_agent(agent_path, capability)
    except DBusError as e:
        if "already exists" not in str(e).lower():
            raise
    try:
        await mgr.call_request_default_agent(agent_path)
    except DBusError:
        # Another agent already holds the default; that's OK.
        pass


async def unregister_agent(
    bus: MessageBus,
    *,
    agent_path: str = "/com/jasper/bt/agent",
) -> None:
    try:
        intro = await bus.introspect("org.bluez", "/org/bluez")
        mgr = bus.get_proxy_object(
            "org.bluez", "/org/bluez", intro,
        ).get_interface("org.bluez.AgentManager1")
        await mgr.call_unregister_agent(agent_path)
    except DBusError:
        pass
    try:
        bus.unexport(agent_path)
    except Exception:  # noqa: BLE001
        pass
