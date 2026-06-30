# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime reconciler for optional accessory-backed mic sources.

Accessory profiles declare what extra pipeline they can add. This module owns
the runtime decision: if BlueZ says an adapter-backed remote profile is paired,
publish the matching ``JASPER_MANUAL_MIC_SOURCES`` entry and run that adapter
unit; otherwise keep both voice and the adapter idle. That keeps rare hardware
from imposing resident cost on every speaker.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dbus_next.errors import DBusError  # type: ignore

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

from .registry import KNOWN_PROFILES, RemoteProfile, lookup_by_name

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
DEFAULT_ENV_FILE = "/var/lib/jasper/accessory-mics.env"
VOICE_UNIT = "jasper-voice.service"
SYSTEMCTL_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class AccessoryMicPlan:
    """Resolved accessory mic state from current BlueZ device records."""

    sources: Mapping[str, str]
    adapter_services: tuple[str, ...]
    active_profiles: tuple[str, ...]


def _unwrap(value):
    return getattr(value, "value", value)


def _is_truthy(value) -> bool:
    return bool(_unwrap(value))


def adapter_mic_profiles() -> tuple[RemoteProfile, ...]:
    """Profiles that can publish a manual mic source through an adapter."""

    return tuple(
        profile for profile in KNOWN_PROFILES
        if profile.mic.status == "adapter"
        and profile.mic.capture_profile_id
        and profile.mic.device
        and profile.mic.adapter_service
    )


def adapter_mic_services() -> tuple[str, ...]:
    """All adapter services managed by this reconciler."""

    return tuple(sorted({
        str(profile.mic.adapter_service)
        for profile in adapter_mic_profiles()
    }))


def _device_name(props: Mapping[str, object]) -> str:
    for key in ("Alias", "Name"):
        value = _unwrap(props.get(key))
        if value:
            return str(value)
    return ""


def plan_from_bluez_objects(
    managed: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> AccessoryMicPlan:
    """Return the active manual mic sources implied by BlueZ devices.

    A profile becomes active only once BlueZ has a paired record for a matching
    advertised name. Nearby scan results that merely look like a WiiM Remote 2
    do not start daemons or open voice mic sources.
    """

    sources: dict[str, str] = {}
    services: set[str] = set()
    active_profiles: set[str] = set()
    for ifaces in managed.values():
        props = ifaces.get(DEVICE_IFACE)
        if not props or not _is_truthy(props.get("Paired")):
            continue
        profile = lookup_by_name(_device_name(props))
        if profile is None or profile.mic.status != "adapter":
            continue
        source_id = profile.mic.capture_profile_id
        device = profile.mic.device
        service = profile.mic.adapter_service
        if not source_id or not device or not service:
            continue
        sources[source_id] = device
        services.add(service)
        active_profiles.add(profile.id)
    return AccessoryMicPlan(
        sources=dict(sorted(sources.items())),
        adapter_services=tuple(sorted(services)),
        active_profiles=tuple(sorted(active_profiles)),
    )


def render_manual_mic_env(sources: Mapping[str, str]) -> str:
    if not sources:
        return ""
    value = ",".join(
        f"{source}={device}" for source, device in sorted(sources.items())
    )
    return f"JASPER_MANUAL_MIC_SOURCES={value}\n"


def write_manual_mic_env(
    sources: Mapping[str, str],
    *,
    path: str = DEFAULT_ENV_FILE,
) -> bool:
    """Publish the voice env file. Returns True when on-disk state changed."""

    target = Path(path)
    body = render_manual_mic_env(sources)
    if not body:
        try:
            existing_body = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        if existing_body == "":
            return False
        target.unlink()
        return True
    current: str | None
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    if current == body:
        return False
    atomic_write_text(path, body, mode=0o644)
    return True


Systemctl = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _systemctl(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args],
        check=False,
        timeout=SYSTEMCTL_TIMEOUT_SEC,
    )


def _invoke_systemctl(
    args: Sequence[str],
    *,
    systemctl: Systemctl,
) -> subprocess.CompletedProcess:
    result = systemctl(args)
    if result.returncode != 0:
        log_event(
            logger,
            "accessory_mic.systemctl_failed",
            command="systemctl " + " ".join(args),
            returncode=result.returncode,
            level=logging.WARNING,
        )
    return result


def apply_adapter_services(
    active_services: Sequence[str],
    *,
    systemctl: Systemctl = _systemctl,
    restart_active: bool = True,
) -> None:
    active = set(active_services)
    for service in adapter_mic_services():
        if service in active:
            _invoke_systemctl(("enable", service), systemctl=systemctl)
            verb = "restart" if restart_active else "start"
            _invoke_systemctl(("--no-block", verb, service), systemctl=systemctl)
        else:
            _invoke_systemctl(
                ("--no-block", "disable", "--now", service),
                systemctl=systemctl,
            )
            _invoke_systemctl(("reset-failed", service), systemctl=systemctl)


def restart_voice_if_active(*, systemctl: Systemctl = _systemctl) -> bool:
    state = systemctl(("is-active", "--quiet", VOICE_UNIT))
    if state.returncode != 0:
        return False
    restarted = _invoke_systemctl(
        ("--no-block", "restart", VOICE_UNIT),
        systemctl=systemctl,
    )
    return restarted.returncode == 0


async def bluez_managed_objects() -> Mapping[str, Mapping[str, Mapping[str, object]]]:
    from dbus_next import BusType  # type: ignore
    from dbus_next.aio import MessageBus  # type: ignore

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect(BLUEZ_BUS, "/")
        om = bus.get_proxy_object(
            BLUEZ_BUS, "/", intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        return await om.call_get_managed_objects()
    finally:
        bus.disconnect()


async def reconcile_once(
    *,
    env_file: str = DEFAULT_ENV_FILE,
    systemctl: Systemctl = _systemctl,
    reason: str = "manual",
) -> AccessoryMicPlan:
    managed = await bluez_managed_objects()
    plan = plan_from_bluez_objects(managed)
    env_changed = write_manual_mic_env(plan.sources, path=env_file)
    restart_adapters = env_changed or reason == "install"
    apply_adapter_services(
        plan.adapter_services,
        systemctl=systemctl,
        restart_active=restart_adapters,
    )
    voice_restarted = restart_voice_if_active(systemctl=systemctl) if env_changed else False
    log_event(
        logger,
        "accessory_mic.reconciled",
        reason=reason,
        profiles=",".join(plan.active_profiles) or "(none)",
        sources=",".join(plan.sources) or "(none)",
        services=",".join(plan.adapter_services) or "(none)",
        env_changed=int(env_changed),
        voice_restarted=int(voice_restarted),
    )
    return plan


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--reason", default="manual")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(reconcile_once(env_file=args.env_file, reason=args.reason))
        return 0
    except (DBusError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log_event(
            logger,
            "accessory_mic.reconcile_failed",
            reason=args.reason,
            err=str(exc),
            level=logging.WARNING,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
