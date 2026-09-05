# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime reconciler for optional accessory-backed mic sources.

Accessory profiles declare what extra pipeline they can add. This module owns
the runtime decision: if BlueZ says an adapter-backed remote profile is paired,
publish the matching ``JASPER_MANUAL_MIC_SOURCES`` entry; otherwise publish
none. That keeps rare hardware from imposing resident cost on every speaker.

The published env file IS the instruction to the adapter: adapters run as tasks
inside the always-on ``jasper-input`` process (ADR-0225), which reads the file
at startup and runs exactly the adapters it names. So this module's only
systemd action for the mic half is to ``try-restart`` that host when the
published set changes — never enable, disable or stop it, which would take the
HID button bridge (volume, push-to-talk) down with it.

Where wake detection runs, it owns the *accessory* fact alone: the voice-input
gate marker is the AND of "no local mic" and "no accessory mic", so moving this
half hands the decision to that marker's single writer, ``jasper-aec-reconcile``
(see ``refresh_voice_input``). Where the assistant is granted WITHOUT wake
detection no such owner is installed and the remote is the only microphone, so
this reconciler runs ``jasper-voice`` itself — see ``voice_follows_accessory_mic``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.install_profile import (
    install_profile_allows_local_sources,
    install_profile_allows_voice_brain,
    install_profile_supports_wake_detection,
    read_install_profile,
)
from jasper.local_sources.markers import local_sources_allowed
from jasper.log_event import log_event
from jasper.music_sources import Source
from jasper.source_intent import source_intent_enabled

from ._dbus import variant_value
from .mic_env import (
    DEFAULT_ACCESSORY_MIC_ENV_FILE,
    render_manual_mic_env,
)
from .registry import KNOWN_PROFILES, RemoteProfile, lookup_by_name

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
VOICE_UNIT = "jasper-voice.service"
# Where wake detection runs, jasper-aec-reconcile owns the voice-input gate
# marker and the voice start/park decision; we hand our half back to it rather
# than deciding here — see refresh_voice_input. Where it is not installed, see
# voice_follows_accessory_mic.
VOICE_INPUT_GATE_UNIT = "jasper-aec-reconcile.service"
SYSTEMCTL_TIMEOUT_SEC = 10.0
BLUEZ_DISCOVERY_TIMEOUT_SEC = 5.0
# Per adapter host, and only when the published set changed: one try-restart,
# then one show probe of the result.
_ADAPTER_SYSTEMCTL_CALLS = 2
_ADAPTER_TIMEOUT_BUDGET_SEC = (
    _ADAPTER_SYSTEMCTL_CALLS * SYSTEMCTL_TIMEOUT_SEC
)
# One show probe, then exactly one verb — for the handoff (show the gate owner,
# then start it or try-restart voice) and for direct ownership (show voice, then
# start/restart/stop it) alike. See refresh_voice_input, converge_voice_unit.
_VOICE_REFRESH_SYSTEMCTL_CALLS = 2
_VOICE_REFRESH_TIMEOUT_BUDGET_SEC = (
    _VOICE_REFRESH_SYSTEMCTL_CALLS * SYSTEMCTL_TIMEOUT_SEC
)
_OWNER_OPERATION_TIMEOUT_BUDGET_SEC = (
    BLUEZ_DISCOVERY_TIMEOUT_SEC
    + _ADAPTER_TIMEOUT_BUDGET_SEC
    + _VOICE_REFRESH_TIMEOUT_BUDGET_SEC
)


# Request protocol for jasper-accessory-reconcile.path. This module is the
# single source of truth for it: the path unit watches this file, and every
# writer goes through the two functions below.
DEFAULT_RECONCILE_REQUEST_FILE = "/var/lib/jasper/accessory-reconcile.request"
_CLAIMED_SUFFIX = ".claimed"
# 0664, not 0600: the reconciler runs as root with an empty
# CapabilityBoundingSet, so it has no DAC override and reads this like any
# other group/other member.
_REQUEST_FILE_MODE = 0o664


def request_reconcile(reason: str, *, path: str | None = None) -> None:
    """Ask for one fresh accessory pass. Any member of group ``jasper`` may call
    this; no systemd privilege is involved.

    Published atomically so the path unit never sees a half-written request, and
    so a request racing a pass either lands before the claim (this pass carries
    it) or after (the path unit re-triggers). Raises ``OSError`` if the request
    could not be published — a silent failure here is a lost refresh.
    """
    atomic_write_text(
        path or DEFAULT_RECONCILE_REQUEST_FILE, reason, mode=_REQUEST_FILE_MODE,
    )


def claim_reconcile_request(path: str | None = None) -> str | None:
    """Consume a pending request at the START of a pass; return its reason.

    The rename is the claim, and it must happen before the pass reads any
    BlueZ state: a request written after it survives under the original name
    and re-triggers the path unit, so no requester is ever answered by a pass
    that predates it. Reading and then unlinking would instead destroy that
    racing request. A failed claim other than "no request" propagates: the
    request survives either way, so failing loudly leaves the oneshot in
    `failed` and stops the watcher at systemd's path trigger limit, which the
    doctor's `check_service_runtime_state` reports. Swallowing it would
    instead re-run full BlueZ passes silently forever.
    """
    target = path or DEFAULT_RECONCILE_REQUEST_FILE
    claimed = target + _CLAIMED_SUFFIX
    try:
        os.replace(target, claimed)
    except FileNotFoundError:
        return None
    try:
        # Best-effort: the claim, not the label, is what makes the pass fresh.
        reason = Path(claimed).read_text(encoding="utf-8").strip()
    except OSError:
        reason = ""
    finally:
        with contextlib.suppress(OSError):
            os.unlink(claimed)
    return reason or None


class AccessoryReconcileError(RuntimeError):
    """Accessory state could not converge authoritatively."""


class BluetoothSourceIntentError(AccessoryReconcileError):
    """Bluetooth intent was unreadable, so accessory services were parked."""


class AdapterHostRefreshError(AccessoryReconcileError):
    """An adapter host did not accept the refresh that applies the new plan."""


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
    active_profiles: tuple[str, ...]


def _is_truthy(value) -> bool:
    return bool(variant_value(value))


def adapter_mic_profiles() -> tuple[RemoteProfile, ...]:
    """Profiles that can publish a manual mic source through an adapter."""

    return tuple(
        profile for profile in KNOWN_PROFILES
        if profile.mic.status == "adapter"
        and profile.mic.capture_profile_id
        and profile.mic.device
        and profile.mic.adapter_host_service
    )


def adapter_mic_hosts() -> tuple[str, ...]:
    """Every process this reconciler asks to re-read the published sources."""

    return tuple(sorted({
        str(profile.mic.adapter_host_service)
        for profile in adapter_mic_profiles()
    }))


def _device_name(props: Mapping[str, object]) -> str:
    for key in ("Alias", "Name"):
        value = variant_value(props.get(key))
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
        host = profile.mic.adapter_host_service
        if not source_id or not device or not host:
            continue
        sources[source_id] = device
        active_profiles.add(profile.id)
    return AccessoryMicPlan(
        sources=dict(sorted(sources.items())),
        active_profiles=tuple(sorted(active_profiles)),
    )


def write_manual_mic_env(
    sources: Mapping[str, str],
    *,
    path: str = DEFAULT_ACCESSORY_MIC_ENV_FILE,
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


def _result_detail(result: subprocess.CompletedProcess) -> str:
    return str(
        getattr(result, "stderr", "")
        or getattr(result, "stdout", "")
        or f"rc={result.returncode}"
    ).strip()


def _show_properties(result: subprocess.CompletedProcess) -> dict[str, str]:
    """Parse ``systemctl show`` ``Key=value`` output, values lowercased."""

    properties: dict[str, str] = {}
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip().lower()
    return properties


def _unit_command_failure(
    unit: str,
    command: Sequence[str],
    *,
    systemctl: Systemctl,
) -> str | None:
    """Run one systemctl verb against ``unit``; the failure line, or None."""

    try:
        result = _invoke_systemctl(command, systemctl=systemctl)
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as exc:
        return f"{unit}: systemctl {' '.join(command)} raised {exc}"
    if result.returncode != 0:
        return (
            f"{unit}: systemctl {' '.join(command)} failed: "
            f"{_result_detail(result)}"
        )
    return None


def refresh_adapter_hosts(
    hosts: Sequence[str],
    *,
    restart: bool,
    require_active: bool,
    systemctl: Systemctl = _systemctl,
) -> tuple[str, ...]:
    """Converge each adapter host against the sources just published.

    ``restart`` is the *apply* step and runs only when the published set
    changed, because a restart is what makes the host re-read the file.
    ``try-restart`` only, never enable/disable/stop: the host is the always-on
    HID bridge, whose unit state the installer owns, and stopping it for an
    unpaired accessory would take volume and push-to-talk with it.

    ``require_active`` is the *observe* step and runs on every pass, changed
    set or not. ``try-restart`` succeeds against a host that is stopped or
    ``failed``, and a host past its start limit stays that way across every
    later no-op pass — while voice, ``/state`` and the doctor all keep
    reporting the accessory microphone the published file names. So while a
    source IS published, a host that is not running is a failure every time it
    is observed, not only on the pass that published it. The restart is
    blocking, not ``--no-block``, so the probe observes the restart it asked
    for.
    """

    failures: list[str] = []
    for host in hosts:
        if restart:
            failure = _unit_command_failure(
                host, ("try-restart", host), systemctl=systemctl,
            )
            if failure is not None:
                failures.append(failure)
                continue
        if require_active and not _unit_active(host, systemctl=systemctl):
            failures.append(f"{host}: expected is-active=active")
    return tuple(failures)


def _gate_owner_state(*, systemctl: Systemctl) -> str:
    """``enabled`` / ``parked`` / ``absent`` / ``masked`` for the gate owner.

    ``enabled`` is the only one that may be started. The distinctions are
    load-bearing rather than cosmetic: the first three all exist in the field,
    and each not-enabled state sends an operator somewhere different.

    * **enabled** — full speaker profile; ``install_systemd_units`` installs and
      enables the unit.
    * **absent** — ``LoadState=not-found``. ``install_streambox_systemd_units``
      installs neither reconciler (both ``install`` lines live in
      ``install_systemd_units``). ``not-found`` observed on jts4 2026-08-07.
    * **masked** — ``LoadState=masked``. Same action as ``absent`` (never
      start), separate state because the remediation is not: masking is a
      deliberate operator act, and telling them "not installed" sends them to
      re-run the installer instead of to ``systemctl unmask``.
    * **parked** — installed but ``UnitFileState`` is not enabled, which a
      full speaker converted to a streambox leaves behind. ``systemctl start``
      on such a unit *succeeds*, so never start one: its
      ``clear_voice_input_absent`` runs ``systemctl enable
      jasper-voice.service``, and a box whose voice follows the accessory mic
      owns that unit with start/stop only (ADR-0217).

    Reads ``LoadState``/``UnitFileState`` rather than branching on an exit code,
    matching ``jasper/source_intent.py``. Any unexpected failure answers
    ``absent``, which is the conservative direction: we do not start anything.
    """
    try:
        result = systemctl(
            ("show", VOICE_INPUT_GATE_UNIT,
             "--property=LoadState", "--property=UnitFileState"),
        )
    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        return "absent"
    if result.returncode != 0:
        return "absent"
    properties = _show_properties(result)
    load_state = properties.get("LoadState")
    if load_state == "masked":
        return "masked"
    if load_state != "loaded":
        return "absent"
    if properties.get("UnitFileState") in ("enabled", "enabled-runtime"):
        return "enabled"
    return "parked"


def refresh_voice_input(*, systemctl: Systemctl = _systemctl) -> str:
    """Converge jasper-voice after the accessory half of the voice-input gate moved.

    Restarting voice is not enough on its own. ``jasper-voice.service`` carries
    ``ConditionPathExists=!/var/lib/jasper/voice-input-absent``, and on a box
    with no local microphone that marker is present — so the unit is *gated
    off*, a restart can never converge it, and a freshly-paired remote is never
    read (issue #2205).

    **The gate owner is enabled** → start it. It is the marker's single writer:
    it re-derives "is there usable voice input?" across BOTH halves and takes
    the matching action (clear the marker and start voice, or write it and
    park). Deciding here would give this reconciler a second job and put two
    writers on one fact. ``--no-block`` because the owner may restart outputd
    and the AEC bridge, and this oneshot is ordered
    ``Before=jasper-voice.service`` — it must queue the work, not wait on it.

    **Otherwise** (absent or deliberately parked — see ``_gate_owner_state``) →
    ``try-restart`` jasper-voice. That restarts it **iff it is already running**,
    so a live push-to-talk session picks up the new source, and a box whose
    profile parked the voice brain is never started by us. On such a box nothing
    writes the marker, so voice is never gated off for a missing local mic and
    there is nothing for a gate owner to re-derive.

    ``try-restart``'s no-op property was measured, not assumed: on jts4
    (2026-08-07) it returned rc=0 and left ``ExecMainStartTimestamp`` unchanged
    against both a loaded-``inactive`` unit and a deliberately-``failed`` one,
    while a plain ``start`` on the same unit did re-run it.

    Bounded at two ``systemctl`` calls on every path — the budget
    ``_VOICE_REFRESH_SYSTEMCTL_CALLS`` claims and
    ``tests/test_systemd_hardening.py`` holds against the unit's
    ``TimeoutStartSec``.

    Returns the action taken, for the reconcile log line.
    """
    owner = _gate_owner_state(systemctl=systemctl)
    if owner == "enabled":
        started = _invoke_systemctl(
            ("--no-block", "start", VOICE_INPUT_GATE_UNIT),
            systemctl=systemctl,
        )
        return "gate_reconcile" if started.returncode == 0 else "none"
    log_event(
        logger,
        "accessory_mic.gate_owner_unavailable",
        unit=VOICE_INPUT_GATE_UNIT,
        state=owner,
    )
    # try-restart, not restart: never start a unit that is not already running.
    restarted = _invoke_systemctl(
        ("--no-block", "try-restart", VOICE_UNIT),
        systemctl=systemctl,
    )
    return "voice_try_restart" if restarted.returncode == 0 else "none"


def voice_follows_accessory_mic() -> bool:
    """Whether this box's voice brain lives and dies with the accessory mic.

    True exactly when the install profile grants the assistant WITHOUT wake
    detection. Nothing installs ``jasper-aec-reconcile`` there, so no gate
    owner exists to hand the decision back to and the paired remote is the only
    microphone: this reconciler owns ``jasper-voice.service`` outright. See
    docs/adr/0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md

    An unreadable install role answers False, which keeps the gate-owner
    handoff — the conservative direction, since that path never starts a
    stopped voice daemon.
    """

    try:
        profile = read_install_profile()
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "accessory_mic.role_probe_failed",
            error=str(exc),
            level=logging.WARNING,
        )
        return False
    return (
        install_profile_allows_voice_brain(profile)
        and not install_profile_supports_wake_detection(profile)
    )


def _unit_active(unit: str, *, systemctl: Systemctl) -> bool:
    try:
        result = systemctl(("show", unit, "--property=ActiveState"))
    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return _show_properties(result).get("ActiveState") == "active"


def converge_voice_unit(
    *,
    wanted: bool,
    env_changed: bool,
    systemctl: Systemctl = _systemctl,
) -> tuple[str, tuple[str, ...]]:
    """Run jasper-voice for exactly as long as an accessory mic is published.

    Start and stop only, never enable or disable: unit enablement belongs to
    the installer, and re-arming ``WantedBy`` here would make a box that boots
    with no remote paired start a voice daemon with no microphone.

    ``restart`` only when published sources changed under a live daemon — the
    one case a plain ``start`` cannot converge, because the daemon reads its
    sources from the env file at startup. Bounded at
    ``_VOICE_REFRESH_SYSTEMCTL_CALLS``.

    Returns the action taken plus any systemctl failures, which join the
    adapter's in the caller's failure list.
    """

    action: str
    command: tuple[str, ...]
    if not wanted:
        action, command = "stop", ("stop", VOICE_UNIT)
    elif env_changed and _unit_active(VOICE_UNIT, systemctl=systemctl):
        action, command = "restart", ("--no-block", "restart", VOICE_UNIT)
    else:
        action, command = "start", ("--no-block", "start", VOICE_UNIT)

    failure = _unit_command_failure(VOICE_UNIT, command, systemctl=systemctl)
    if failure is not None:
        return "none", (failure,)
    return action, ()


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
    env_file: str = DEFAULT_ACCESSORY_MIC_ENV_FILE,
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
        plan = AccessoryMicPlan(sources={}, active_profiles=())

    # Publish first in both directions: the file is what the host reads, so the
    # refresh below is what applies it, and a refresh before the write would
    # apply the previous plan. This gives up the old teardown ordering, which
    # stopped the producer before withdrawing its source — unreachable now that
    # the file is the only handle on the adapter. A write that fails therefore
    # leaves the previous plan running — which is an authoritative failure, not
    # the fail-soft I/O class main() exits 0 on: a failed unlink under
    # Bluetooth Off leaves the adapter streaming behind a green oneshot.
    try:
        env_changed = write_manual_mic_env(plan.sources, path=env_file)
    except OSError as exc:
        raise AccessoryReconcileError(
            f"could not publish accessory mic sources: {exc}"
        ) from exc
    hosts = adapter_mic_hosts()
    adapter_failures = refresh_adapter_hosts(
        hosts,
        restart=env_changed,
        require_active=bool(plan.sources),
        systemctl=systemctl,
    )
    if voice_follows_accessory_mic():
        voice, voice_failures = converge_voice_unit(
            wanted=bool(plan.sources),
            env_changed=env_changed,
            systemctl=systemctl,
        )
        adapter_failures += voice_failures
    else:
        voice = refresh_voice_input(systemctl=systemctl) if env_changed else "none"
    if intent_error is not None:
        log_event(
            logger,
            "accessory_mic.intent_invalid",
            reason=reason,
            action="parked",
            env_changed=int(env_changed),
            voice=voice,
            err=str(intent_error),
            level=logging.ERROR,
        )
    if adapter_failures:
        log_event(
            logger,
            "accessory_mic.host_refresh_failed",
            reason=reason,
            failures=" | ".join(adapter_failures),
            env_changed=int(env_changed),
            voice=voice,
            level=logging.ERROR,
        )
    if intent_error is not None:
        if adapter_failures:
            raise BluetoothSourceIntentError(
                f"{intent_error}; adapter host refresh failed: "
                + " | ".join(adapter_failures)
            )
        raise intent_error
    if adapter_failures:
        raise AdapterHostRefreshError(" | ".join(adapter_failures))
    log_event(
        logger,
        "accessory_mic.reconciled",
        reason=reason,
        bluetooth_intent="enabled" if bluetooth_enabled else "disabled",
        role_allowed=int(role_allowed),
        profiles=",".join(plan.active_profiles) or "(none)",
        sources=",".join(plan.sources) or "(none)",
        hosts=",".join(hosts) or "(none)",
        host_restarted=int(env_changed),
        env_changed=int(env_changed),
        voice=voice,
    )
    return plan


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=DEFAULT_ACCESSORY_MIC_ENV_FILE)
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--reason-file", default=DEFAULT_RECONCILE_REQUEST_FILE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from dbus_next.errors import DBusError  # type: ignore

    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # A claimed request names its own requester; --reason is the fallback for
    # the direct boot/install starts, which carry no request file.
    reason = claim_reconcile_request(args.reason_file) or args.reason
    try:
        asyncio.run(reconcile_once(env_file=args.env_file, reason=reason))
        return 0
    except AccessoryReconcileError as exc:
        log_event(
            logger,
            "accessory_mic.reconcile_failed",
            reason=reason,
            err=str(exc),
            level=logging.ERROR,
        )
        return 1
    except (DBusError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log_event(
            logger,
            "accessory_mic.reconcile_failed",
            reason=reason,
            err=str(exc),
            level=logging.WARNING,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
