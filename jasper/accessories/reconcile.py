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
from jasper.install_profile import (
    install_profile_allows_local_sources,
    read_install_profile,
)
from jasper.local_sources.guard import local_sources_allowed
from jasper.log_event import log_event
from jasper.music_sources import Source
from jasper.source_intent import source_intent_enabled

from .registry import KNOWN_PROFILES, RemoteProfile, lookup_by_name

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
DEFAULT_ENV_FILE = "/var/lib/jasper/accessory-mics.env"
VOICE_UNIT = "jasper-voice.service"
SYSTEMCTL_TIMEOUT_SEC = 10.0
BLUEZ_DISCOVERY_TIMEOUT_SEC = 5.0
_ADAPTER_SYSTEMCTL_CALLS = 3
_ADAPTER_TIMEOUT_BUDGET_SEC = (
    _ADAPTER_SYSTEMCTL_CALLS * SYSTEMCTL_TIMEOUT_SEC
)
_VOICE_REFRESH_SYSTEMCTL_CALLS = 2
_VOICE_REFRESH_TIMEOUT_BUDGET_SEC = (
    _VOICE_REFRESH_SYSTEMCTL_CALLS * SYSTEMCTL_TIMEOUT_SEC
)
_OWNER_OPERATION_TIMEOUT_BUDGET_SEC = (
    BLUEZ_DISCOVERY_TIMEOUT_SEC
    + _ADAPTER_TIMEOUT_BUDGET_SEC
    + _VOICE_REFRESH_TIMEOUT_BUDGET_SEC
)


class AccessoryReconcileError(RuntimeError):
    """Accessory state could not converge authoritatively."""


class BluetoothSourceIntentError(AccessoryReconcileError):
    """Bluetooth intent was unreadable, so accessory services were parked."""


class AdapterServiceTeardownError(AccessoryReconcileError):
    """One or more optional adapter services did not reach parked state."""


class AdapterServiceActivationError(AccessoryReconcileError):
    """One or more requested adapter services did not become active."""


def _local_sources_allowed() -> bool:
    """Mirror the source coordinator's install-role + grouping permission."""

    try:
        if not install_profile_allows_local_sources(read_install_profile()):
            return False
        return local_sources_allowed()[0]
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "accessory_mic.role_probe_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        return False


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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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


def _apply_adapter_service(
    service: str,
    *,
    active: bool,
    systemctl: Systemctl,
    restart_active: bool,
) -> tuple[str, ...]:
    failures: list[str] = []
    commands: tuple[tuple[str, ...], ...]
    if active:
        verb = "restart" if restart_active else "start"
        commands = (("enable", service), (verb, service))
        expected_enabled = "enabled"
        expected_active = "active"
    else:
        commands = (
            ("disable", "--now", service),
            ("reset-failed", service),
        )
        expected_enabled = "disabled"
        expected_active = "inactive"

    for command in commands:
        # ``reset-failed`` is cleanup, not the terminal-state contract.
        # systemd returns nonzero when an already-clean inactive unit has no
        # failed state to reset ("Unit ... not loaded"), even when its unit
        # file is loaded and the requested disabled/inactive state is exact.
        # Keep this best-effort and let the authoritative show probe below
        # catch a genuinely failed or active adapter.
        if command[0] == "reset-failed":
            try:
                systemctl(command)
            except (
                OSError,
                RuntimeError,
                TimeoutError,
                subprocess.SubprocessError,
            ):
                pass
            continue
        try:
            result = _invoke_systemctl(command, systemctl=systemctl)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            subprocess.SubprocessError,
        ) as exc:
            failures.append(
                f"{service}: systemctl {' '.join(command)} raised {exc}"
            )
            continue
        if result.returncode != 0:
            detail = str(
                getattr(result, "stderr", "")
                or getattr(result, "stdout", "")
                or f"rc={result.returncode}"
            ).strip()
            failures.append(
                f"{service}: systemctl {' '.join(command)} failed: {detail}"
            )

    show_command = (
        "show",
        service,
        "--property=UnitFileState",
        "--property=ActiveState",
    )
    try:
        result = systemctl(show_command)
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as exc:
        failures.append(f"{service}: systemctl show raised {exc}")
        return tuple(failures)
    if result.returncode != 0:
        detail = str(
            getattr(result, "stderr", "")
            or getattr(result, "stdout", "")
            or f"rc={result.returncode}"
        ).strip()
        failures.append(f"{service}: systemctl show failed: {detail}")
        return tuple(failures)

    properties: dict[str, str] = {}
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip().lower()
    for label, property_name, expected in (
        ("is-enabled", "UnitFileState", expected_enabled),
        ("is-active", "ActiveState", expected_active),
    ):
        observed = properties.get(property_name, "")
        if observed != expected:
            failures.append(
                f"{service}: expected {label}={expected}, "
                f"observed {observed or 'missing state'}"
            )
    return tuple(failures)


def apply_adapter_services(
    active_services: Sequence[str],
    *,
    systemctl: Systemctl = _systemctl,
    restart_active: bool = True,
) -> tuple[str, ...]:
    """Converge every adapter and return ordered per-adapter failures.

    Each declared adapter retains its own exact commands, logs, and terminal
    probes. The registry currently contains one adapter, so a direct ordered
    loop is the smallest reliable owner. If a second real adapter ships, its
    measured service behavior should drive any timeout/concurrency redesign.
    """

    services = adapter_mic_services()
    if not services:
        return ()

    active = set(active_services)

    def converge(service: str) -> tuple[str, ...]:
        return _apply_adapter_service(
            service,
            active=service in active,
            systemctl=systemctl,
            restart_active=restart_active,
        )

    results = tuple(converge(service) for service in services)
    return tuple(failure for result in results for failure in result)


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

    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()
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
    intent_error: BluetoothSourceIntentError | None = None
    try:
        bluetooth_enabled = source_intent_enabled(Source.BLUETOOTH)
    except RuntimeError as exc:
        # The intent file is non-root-writable input. An explicit malformed
        # value must never fall back to Bluetooth's shipped-on default. Park
        # every optional adapter, clear its voice source, then fail the oneshot
        # so the operator sees the invalid source-of-truth state.
        bluetooth_enabled = False
        intent_error = BluetoothSourceIntentError(
            f"cannot read Bluetooth source intent: {exc}"
        )

    role_allowed = _local_sources_allowed() if bluetooth_enabled else False
    effective_enabled = bluetooth_enabled and role_allowed
    if effective_enabled:
        try:
            managed = await asyncio.wait_for(
                bluez_managed_objects(),
                timeout=BLUEZ_DISCOVERY_TIMEOUT_SEC,
            )
        except TimeoutError as exc:
            log_event(
                logger,
                "accessory_mic.bluez_discovery_failed",
                reason=reason,
                error="timeout",
                timeout_sec=BLUEZ_DISCOVERY_TIMEOUT_SEC,
                level=logging.ERROR,
            )
            raise AccessoryReconcileError(
                "BlueZ accessory discovery timed out after "
                f"{BLUEZ_DISCOVERY_TIMEOUT_SEC:g}s"
            ) from exc
        plan = plan_from_bluez_objects(managed)
    else:
        # Bluetooth Off and role parking are authoritative even when BlueZ still
        # has paired records. Do not query D-Bus: BlueZ may be powered down or
        # deliberately retained only as shared control-plane infrastructure.
        plan = AccessoryMicPlan(
            sources={},
            adapter_services=(),
            active_profiles=(),
        )

    if plan.adapter_services:
        # Publish the source before its producer starts.
        env_changed = write_manual_mic_env(plan.sources, path=env_file)
        adapter_failures = apply_adapter_services(
            plan.adapter_services,
            systemctl=systemctl,
            restart_active=env_changed or reason == "install",
        )
    else:
        # Fail closed in the opposite order: stop every producer before its
        # voice source disappears. This also guarantees malformed intent parks
        # the adapter even if cleaning up the env file subsequently fails.
        adapter_failures = apply_adapter_services((), systemctl=systemctl)
        env_changed = write_manual_mic_env({}, path=env_file)
    voice_restarted = (
        restart_voice_if_active(systemctl=systemctl) if env_changed else False
    )
    if intent_error is not None:
        log_event(
            logger,
            "accessory_mic.intent_invalid",
            reason=reason,
            action="parked",
            env_changed=int(env_changed),
            voice_restarted=int(voice_restarted),
            err=str(intent_error),
            level=logging.ERROR,
        )
    if adapter_failures:
        log_event(
            logger,
            (
                "accessory_mic.activation_failed"
                if plan.adapter_services
                else "accessory_mic.teardown_failed"
            ),
            reason=reason,
            failures=" | ".join(adapter_failures),
            env_changed=int(env_changed),
            voice_restarted=int(voice_restarted),
            level=logging.ERROR,
        )
    if intent_error is not None:
        if adapter_failures:
            raise BluetoothSourceIntentError(
                f"{intent_error}; adapter teardown failed: "
                + " | ".join(adapter_failures)
            )
        raise intent_error
    if adapter_failures:
        error_type = (
            AdapterServiceActivationError
            if plan.adapter_services
            else AdapterServiceTeardownError
        )
        raise error_type(" | ".join(adapter_failures))
    log_event(
        logger,
        "accessory_mic.reconciled",
        reason=reason,
        bluetooth_intent="enabled" if bluetooth_enabled else "disabled",
        role_allowed=int(role_allowed),
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
    except AccessoryReconcileError as exc:
        log_event(
            logger,
            "accessory_mic.reconcile_failed",
            reason=args.reason,
            err=str(exc),
            level=logging.ERROR,
        )
        return 1
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
