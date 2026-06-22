# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""BlueZ pairing agent for JTS.

JTS is a headless speaker: it has no trusted local display or keyboard, and
the product pairing UX is deliberately "turn on pairing mode, then pair from
your phone." The only supported agent capability is therefore
NoInputNoOutput. BlueZ maps that to Secure Simple Pairing "Just Works" for
ordinary phones and audio devices, with no PIN/passkey/code prompt.

Interactive pairing requests are rejected on purpose. If a device requires a
PIN, passkey, or numeric comparison, it is outside the supported JTS flow.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import NoReturn

from dbus_next import Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore
from dbus_next.service import ServiceInterface, method  # type: ignore

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

REJECTED_DBUS_NAME = "org.bluez.Error.Rejected"
DEFAULT_AGENT_PATH = "/com/jasper/bt/no_code_agent"


def _reject(message: str) -> NoReturn:
    raise DBusError(REJECTED_DBUS_NAME, message)


class NoCodeAgent(ServiceInterface):
    """A single-purpose BlueZ Agent1 implementation.

    Accepts no-code authorization requests and rejects every pairing method
    that would require a human to compare or type a code.
    """

    def __init__(
        self,
        bus: MessageBus | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> None:
        super().__init__("org.bluez.Agent1")
        self._bus = bus
        self._on_release = on_release

    @method()
    def Release(self):  # noqa: N802
        log_event(logger, "bluetooth_agent.release")
        if self._on_release is not None:
            self._on_release()

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore  # noqa: N802
        log_event(
            logger,
            "bluetooth_agent.reject",
            device=device,
            reason="pin_required",
            level=logging.WARNING,
        )
        _reject("JTS does not support PIN-code pairing")

    @method()
    def DisplayPinCode(  # noqa: N802
        self, device: "o", pincode: "s",  # type: ignore
    ):
        log_event(
            logger,
            "bluetooth_agent.reject",
            device=device,
            reason="display_pin_required",
            level=logging.WARNING,
        )
        _reject("JTS does not display PIN codes")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore  # noqa: N802
        log_event(
            logger,
            "bluetooth_agent.reject",
            device=device,
            reason="passkey_required",
            level=logging.WARNING,
        )
        _reject("JTS does not support passkey pairing")

    @method()
    def DisplayPasskey(  # noqa: N802
        self, device: "o", passkey: "u", entered: "q",  # type: ignore
    ):
        log_event(
            logger,
            "bluetooth_agent.reject",
            device=device,
            reason="display_passkey_required",
            level=logging.WARNING,
        )
        _reject("JTS does not display passkeys")

    @method()
    def RequestConfirmation(  # noqa: N802
        self, device: "o", passkey: "u",  # type: ignore
    ):
        log_event(
            logger,
            "bluetooth_agent.reject",
            device=device,
            reason="confirmation_required",
            level=logging.WARNING,
        )
        _reject("JTS does not support numeric comparison pairing")

    @method()
    async def RequestAuthorization(self, device: "o"):  # type: ignore  # noqa: N802
        log_event(
            logger,
            "bluetooth_agent.authorize_pairing",
            device=device,
        )
        await self._trust_device(device)

    @method()
    async def AuthorizeService(  # noqa: N802
        self, device: "o", uuid: "s",  # type: ignore
    ):
        log_event(
            logger,
            "bluetooth_agent.authorize_service",
            device=device,
            uuid=uuid,
        )
        await self._trust_device(device)

    @method()
    def Cancel(self):  # noqa: N802
        log_event(logger, "bluetooth_agent.cancel")

    async def _trust_device(self, device: str) -> None:
        if self._bus is None:
            return
        try:
            intro = await self._bus.introspect("org.bluez", device)
            props = self._bus.get_proxy_object(
                "org.bluez", device, intro,
            ).get_interface("org.freedesktop.DBus.Properties")
            await props.call_set(
                "org.bluez.Device1", "Trusted", Variant("b", True),
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                "bluetooth_agent.trust_failed",
                device=device,
                err=exc,
                level=logging.WARNING,
            )
        else:
            log_event(logger, "bluetooth_agent.trusted", device=device)


async def register_agent(
    bus: MessageBus,
    agent: ServiceInterface,
    *,
    agent_path: str = DEFAULT_AGENT_PATH,
    capability: str = "NoInputNoOutput",
) -> None:
    """Export `agent` and request default-agent ownership."""
    try:
        bus.export(agent_path, agent)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).lower()
        already_exported = "already" in detail and (
            "export" in detail or agent_path.lower() in detail
        )
        if not already_exported:
            log_event(
                logger,
                "bluetooth_agent.export_failed",
                path=agent_path,
                err=exc,
                level=logging.WARNING,
            )
            raise
        log_event(
            logger,
            "bluetooth_agent.export_exists",
            path=agent_path,
            level=logging.DEBUG,
        )
    intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object(
        "org.bluez", "/org/bluez", intro,
    ).get_interface("org.bluez.AgentManager1")
    try:
        await mgr.call_register_agent(agent_path, capability)
    except DBusError as e:
        if "already exists" not in str(e).lower():
            raise
    await mgr.call_request_default_agent(agent_path)


async def unregister_agent(
    bus: MessageBus,
    *,
    agent_path: str = DEFAULT_AGENT_PATH,
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


__all__ = [
    "DEFAULT_AGENT_PATH",
    "NoCodeAgent",
    "REJECTED_DBUS_NAME",
    "register_agent",
    "unregister_agent",
]
