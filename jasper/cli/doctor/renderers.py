# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — renderers domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional
from ...config import Config
from ...log_event import log_event

_LANE_LOG = logging.getLogger(__name__)
from ...mux_mode_persistence import DEFAULT_PATH as _MUX_MODE_DEFAULT_PATH
from ...music_sources import MUSIC_SOURCES, Source
from ...source_intent import (
    read_bluetooth_rfkill_state,
    source_intent_enabled,
)
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    REASON_SOURCE_INTENT_INVALID,
    REASON_SYSTEMCTL_UNAVAILABLE,
    CheckResult,
    _exception_detail,
    _parked_follower_result,
    _parse_systemd_environment,
    _run,
)

# Closed vocabulary for this module's `CheckResult.reason` (AGENTS.md: tests
# pin status + reason, never `detail` prose). Named by the fact a consumer
# would branch on; two branches meaning the same thing share one code —
# REASON_SOURCE_OFF is set from ONE shared helper and covers every renderer
# check that calls it, and REASON_SPOTIFY_NOT_CONFIGURED covers both Spotify
# checks below. The bonded-follower park code lives in `_shared.py`.
REASON_SOURCE_OFF = "source_off"
REASON_SOURCE_OFF_DRIFT = "source_off_drift"
REASON_BLUETOOTH_RADIO_UNVERIFIABLE = "bluetooth_radio_unverifiable"
REASON_BLUETOOTH_RADIO_NOT_READY = "bluetooth_radio_not_ready"

REASON_LIBRESPOT_BINARY_MISSING = "librespot_binary_missing"
REASON_LIBRESPOT_NOT_ACTIVE = "librespot_not_active"

REASON_SHAIRPORT_BINARY_MISSING = "shairport_binary_missing"
REASON_SHAIRPORT_NOT_AP2 = "shairport_not_ap2"
REASON_SHAIRPORT_NOT_ACTIVE = "shairport_not_active"

REASON_NQPTP_NOT_ACTIVE = "nqptp_not_active"

REASON_MUX_NOT_ACTIVE = "mux_not_active"

REASON_BLUEALSA_NOT_ACTIVE = "bluealsa_not_active"

REASON_BT_PAIRING_SYSTEMCTL_SHOW_FAILED = "bt_pairing_systemctl_show_failed"
REASON_BT_PAIRING_AGENT_NOT_RUNNING = "bt_pairing_agent_not_running"
REASON_BT_PAIRING_WRONG_AGENT = "bt_pairing_wrong_agent"
REASON_BT_PAIRING_BLUETOOTHCTL_UNAVAILABLE = "bt_pairing_bluetoothctl_unavailable"
REASON_BT_PAIRING_ADAPTER_STATE_UNKNOWN = "bt_pairing_adapter_state_unknown"
REASON_BT_PAIRING_PAIRABLE_WITHOUT_DISCOVERABLE = (
    "bt_pairing_pairable_without_discoverable"
)
REASON_BT_PAIRING_WINDOW_OPEN = "bt_pairing_window_open"

REASON_SPOTIFY_NOT_CONFIGURED = "spotify_not_configured"
REASON_SPOTIFY_CACHE_MISSING = "spotify_cache_missing"
REASON_SPOTIFY_DEVICE_NAME_EMPTY = "spotify_device_name_empty"
REASON_SPOTIFY_CLIENT_BUILD_FAILED = "spotify_client_build_failed"
REASON_SPOTIFY_NO_TOKENS = "spotify_no_tokens"
REASON_SPOTIFY_DEVICE_VISIBLE_TO_SOME = "spotify_device_visible_to_some"
REASON_SPOTIFY_DEVICE_NOT_VISIBLE = "spotify_device_not_visible"

REASON_SHAIRPORT_CONF_MISSING = "shairport_conf_missing"
REASON_SHAIRPORT_CONF_UNREADABLE = "shairport_conf_unreadable"
REASON_SHAIRPORT_NO_OUTPUT_DEVICE = "shairport_no_output_device"
REASON_SHAIRPORT_LANE_REGISTRY_MISSING = "shairport_lane_registry_missing"
REASON_SHAIRPORT_RING_DISARMED_STALE = "shairport_ring_disarmed_stale"
REASON_SHAIRPORT_ALOOP_ARMED_STALE = "shairport_aloop_armed_stale"
REASON_SHAIRPORT_LEGACY_DMIX = "shairport_legacy_dmix"
REASON_SHAIRPORT_LEGACY_PLUGHW = "shairport_legacy_plughw"
REASON_SHAIRPORT_RAW_HW_LOOPBACK = "shairport_raw_hw_loopback"
REASON_SHAIRPORT_DEVICE_UNRECOGNIZED = "shairport_device_unrecognized"
REASON_SHAIRPORT_RING_ARMED_OK = "shairport_ring_armed_ok"
REASON_SHAIRPORT_ALOOP_UNARMED_OK = "shairport_aloop_unarmed_ok"

REASON_RENDERER_DEVICE_UNRESOLVABLE = "renderer_device_unresolvable"
REASON_RENDERER_NONE_CONFIGURED = "renderer_none_configured"

REASON_MUX_MODE_UNREADABLE = "mux_mode_unreadable"
REASON_MUX_MODE_CORRUPT = "mux_mode_corrupt"
REASON_MUX_MODE_UNKNOWN_SOURCE = "mux_mode_unknown_source"
REASON_MUX_MODE_PINNED = "mux_mode_pinned"

# ----------------------------------------------------------------------
# Per-renderer health: each daemon's own surface (HTTP / DBus / system).
# ----------------------------------------------------------------------


def _bluetoothctl_show() -> subprocess.CompletedProcess | Exception:
    """``bluetoothctl show``, or the exception it raised. Never raises itself
    — the memoized read behind :func:`_cached_bluetoothctl_show` must be
    computable exactly once and handed to every caller, so a caller that
    needs to react to (or re-raise) a particular exception type gets it back
    as a value instead of losing it to a swallowed first call."""
    try:
        return _run(["bluetoothctl", "show"])
    except (OSError, subprocess.SubprocessError) as exc:
        return exc


def _cached_bluetoothctl_show() -> subprocess.CompletedProcess | Exception:
    """``bluetoothctl show`` is asked up to three times per run (household-Off
    drift, desired-On radio proof, pairing-policy's own adapter gate); one
    process serves all three."""
    return evidence.get("bluetoothctl_show", _bluetoothctl_show)


def _intentional_source_off(
    source: Source,
    label: str,
    *,
    units: tuple[str, ...],
    check_bluetooth_radio: bool = False,
) -> CheckResult | None:
    """Return a healthy Off only after proving derived runtime is also Off."""
    try:
        enabled = source_intent_enabled(source)
    except RuntimeError as exc:
        return CheckResult(
            label,
            "fail",
            f"source intent is invalid or unreadable: {exc}",
            reason=REASON_SOURCE_INTENT_INVALID,
        )
    if not enabled:
        drift: list[str] = []
        for unit in units:
            if evidence.unit_active(unit):
                drift.append(f"{unit} is still active")
        if check_bluetooth_radio:
            try:
                rfkill = read_bluetooth_rfkill_state()
            except RuntimeError as exc:
                return CheckResult(
                    label,
                    "fail",
                    f"Bluetooth is intentionally off but RF-kill state "
                    f"cannot be verified: {exc}",
                    reason=REASON_BLUETOOTH_RADIO_UNVERIFIABLE,
                )
            if rfkill.present and not rfkill.fully_soft_blocked:
                drift.append("Bluetooth radio is not RF-killed")
            powered = _cached_bluetoothctl_show()
            if isinstance(powered, Exception):
                raise powered
            if any(
                line.strip().lower() == "powered: yes"
                for line in powered.stdout.splitlines()
            ):
                drift.append("BlueZ still reports Powered: yes")
        if drift:
            return CheckResult(
                label,
                "fail",
                "source intent is off but derived state drifted: "
                + "; ".join(drift),
                reason=REASON_SOURCE_OFF_DRIFT,
            )
        return CheckResult(
            label,
            "ok",
            "intentionally off in Music sources (/sources/)",
            reason=REASON_SOURCE_OFF,
        )
    return None


def _desired_bluetooth_radio_failure(label: str) -> CheckResult | None:
    """Return a failure unless desired-On reached RF-kill and BlueZ."""
    try:
        rfkill = read_bluetooth_rfkill_state()
    except RuntimeError as exc:
        return CheckResult(
            label,
            "fail",
            f"source intent is on but RF-kill state cannot be verified: {exc}",
            reason=REASON_BLUETOOTH_RADIO_UNVERIFIABLE,
        )

    drift: list[str] = []
    if not rfkill.present:
        drift.append("Bluetooth RF-kill radio is not present")
    if rfkill.soft_blocked:
        drift.append("Bluetooth radio is soft blocked")
    if rfkill.hard_blocked:
        drift.append("Bluetooth radio is hard blocked")
    powered_or_exc = _cached_bluetoothctl_show()
    if isinstance(powered_or_exc, Exception):
        drift.append(f"BlueZ Powered state cannot be read: {powered_or_exc}")
    else:
        powered = powered_or_exc
        powered_value: str | None = None
        for raw in powered.stdout.splitlines():
            key, separator, value = raw.strip().partition(":")
            if separator and key == "Powered":
                powered_value = value.strip().lower()
                break
        if powered.returncode != 0 or powered_value is None:
            drift.append("BlueZ Powered state is unavailable")
        elif powered_value != "yes":
            drift.append(f"BlueZ reports Powered: {powered_value}")

    if not drift:
        return None
    return CheckResult(
        label,
        "fail",
        "source intent is on but the Bluetooth radio is not ready: "
        + "; ".join(drift),
        reason=REASON_BLUETOOTH_RADIO_NOT_READY,
    )


@doctor_check(label="librespot.service", needs_cfg=True)
def check_librespot_running(cfg: Config) -> CheckResult:
    """Verify librespot is installed and the systemd unit is active.

    librespot 0.8.0 (rust) replaced go-librespot in the debian-stack
    on 2026-05-07 specifically for the configurable volume curve
    (--volume-ctrl log over 60 dB range). It has no local control
    HTTP, so health is checked via systemd state + binary version."""
    parked = _parked_follower_result("librespot.service")
    if parked is not None:
        return parked
    intentional_off = _intentional_source_off(
        Source.SPOTIFY,
        "librespot.service",
        units=("librespot.service",),
    )
    if intentional_off is not None:
        return intentional_off
    bin_path = "/usr/bin/librespot"
    if not os.path.isfile(bin_path):
        return CheckResult(
            "librespot binary", "fail",
            f"{bin_path} not present. Install: "
            "apt install raspotify (provides librespot via .deb)",
            reason=REASON_LIBRESPOT_BINARY_MISSING,
        )
    state = (evidence.unit_state("librespot.service") or {}).get(
        "active_state",
    ) or "unknown"
    if state != "active":
        return CheckResult(
            "librespot.service", "fail",
            f"systemctl is-active = '{state}'. Check: "
            "systemctl status librespot",
            reason=REASON_LIBRESPOT_NOT_ACTIVE,
        )
    # Best-effort version line (librespot prints to stderr at startup)
    return CheckResult(
        "librespot.service", "ok",
        f"{bin_path} active (state file: {cfg.librespot_state_path})",
    )

@doctor_check()
def check_shairport_sync_ap2() -> CheckResult:
    """Verify shairport-sync is installed with AirPlay 2 support
    AND the systemd unit is active. The Debian Trixie apt package
    is AP1-only; the migration's source-build emits a binary whose
    `-V` output contains 'AirPlay2'."""
    parked = _parked_follower_result("shairport-sync AP2")
    if parked is not None:
        return parked
    intentional_off = _intentional_source_off(
        Source.AIRPLAY,
        "shairport-sync AP2",
        units=("shairport-sync.service", "nqptp.service"),
    )
    if intentional_off is not None:
        return intentional_off
    if shutil.which("shairport-sync") is None:
        return CheckResult(
            "shairport-sync AP2", "fail",
            "binary not found. Source-build per deploy/debian-stack/README.md",
            reason=REASON_SHAIRPORT_BINARY_MISSING,
        )
    p = _run(["shairport-sync", "-V"])
    out = (p.stdout + p.stderr).strip().split("\n")[0]
    if "AirPlay2" not in out:
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary lacks --with-airplay-2 (got: {out!r}). "
            f"Apt's package is AP1-only; rebuild from source.",
            reason=REASON_SHAIRPORT_NOT_AP2,
        )
    state = (evidence.unit_state("shairport-sync.service") or {}).get(
        "active_state",
    ) or "unknown"
    if state != "active":
        return CheckResult(
            "shairport-sync AP2", "fail",
            f"binary OK but systemd state={state}. "
            f"Check: journalctl -u shairport-sync",
            reason=REASON_SHAIRPORT_NOT_ACTIVE,
        )
    return CheckResult("shairport-sync AP2", "ok", out)

@doctor_check()
def check_nqptp_running() -> CheckResult:
    """nqptp is required for AirPlay 2 timing. Without it,
    shairport-sync's AP2 path silently fails to handshake."""
    parked = _parked_follower_result("nqptp.service")
    if parked is not None:
        return parked
    intentional_off = _intentional_source_off(
        Source.AIRPLAY,
        "nqptp.service",
        units=("shairport-sync.service", "nqptp.service"),
    )
    if intentional_off is not None:
        return intentional_off
    state = (evidence.unit_state("nqptp.service") or {}).get(
        "active_state",
    ) or "unknown"
    if state == "active":
        return CheckResult("nqptp", "ok", "active (UDP 319/320)")
    return CheckResult(
        "nqptp", "fail",
        f"state={state}. shairport-sync AP2 will not handshake "
        f"without nqptp running.",
        reason=REASON_NQPTP_NOT_ACTIVE,
    )

@doctor_check(core=True)
def check_jasper_mux() -> CheckResult:
    """jasper-mux arbitrates which renderer plays when. Without it,
    source selection and guarded handoff stop working; if fan-in has
    restarted into its safe NONE state, music may stay silent."""
    parked = _parked_follower_result("jasper-mux")
    if parked is not None:
        return parked
    unit_state = evidence.unit_state("jasper-mux.service")
    if unit_state is None:
        return CheckResult(
            "jasper-mux",
            "skipped",
            "systemctl unavailable — skipped (not Linux?)",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    state = unit_state.get("active_state") or "unknown"
    if state == "active":
        return CheckResult(
            "jasper-mux", "ok",
            "active (source selection + latest-source-wins)",
        )
    return CheckResult(
        "jasper-mux", "fail",
        f"state={state}. Source selection and guarded handoff are "
        f"unavailable; fan-in may remain silent until mux is restarted.",
        reason=REASON_MUX_NOT_ACTIVE,
    )

@doctor_check()
def check_bluealsa() -> CheckResult:
    """bluealsa daemon registers the A2DP profile with bluez;
    bluealsa-aplay forwards incoming A2DP audio to ALSA. Both
    must be active for "phone-as-Bluetooth-source → speaker"
    to work end-to-end."""
    parked = _parked_follower_result("bluealsa")
    if parked is not None:
        return parked
    intentional_off = _intentional_source_off(
        Source.BLUETOOTH,
        "bluealsa",
        units=("bluealsa.service", "bluealsa-aplay.service", "bt-agent.service"),
        check_bluetooth_radio=True,
    )
    if intentional_off is not None:
        return intentional_off
    radio_failure = _desired_bluetooth_radio_failure("bluealsa")
    if radio_failure is not None:
        return radio_failure
    s1 = (evidence.unit_state("bluealsa.service") or {}).get(
        "active_state",
    ) or "unknown"
    s2 = (evidence.unit_state("bluealsa-aplay.service") or {}).get(
        "active_state",
    ) or "unknown"
    if s1 == "active" and s2 == "active":
        return CheckResult("bluealsa", "ok", "daemon + aplay active")
    return CheckResult(
        "bluealsa", "fail",
        f"bluealsa={s1}, bluealsa-aplay={s2}. "
        f"Check: journalctl -u bluealsa",
        reason=REASON_BLUEALSA_NOT_ACTIVE,
    )

@doctor_check()
def check_bluetooth_pairing_policy() -> CheckResult:
    """Verify the JTS no-code pairing agent is installed and idle-closed."""
    parked = _parked_follower_result("Bluetooth pairing policy")
    if parked is not None:
        return parked
    intentional_off = _intentional_source_off(
        Source.BLUETOOTH,
        "Bluetooth pairing policy",
        units=("bt-agent.service",),
    )
    if intentional_off is not None:
        return intentional_off
    expected_exec = "/opt/jasper/.venv/bin/jasper-bluetooth-agent"
    unit_state = evidence.unit_state("bt-agent.service")
    if unit_state is None:
        return CheckResult(
            "Bluetooth pairing policy",
            "skipped",
            "systemctl unavailable — skipped",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    active = unit_state.get("active_state") or ""
    sub = unit_state.get("sub_state") or ""
    if active != "active" or sub != "running":
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            f"bt-agent.service state={active}/{sub}; no-code default agent not running",
            reason=REASON_BT_PAIRING_AGENT_NOT_RUNNING,
        )
    # ExecStart isn't in the batched roster read's property set — one small
    # dedicated call for the one property this check needs it for.
    try:
        exec_proc = _run(["systemctl", "show", "bt-agent.service", "-p", "ExecStart"])
    except FileNotFoundError:
        return CheckResult(
            "Bluetooth pairing policy",
            "skipped",
            "systemctl unavailable — skipped",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    if exec_proc.returncode != 0:
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            "systemctl show bt-agent.service failed",
            reason=REASON_BT_PAIRING_SYSTEMCTL_SHOW_FAILED,
        )
    exec_start = ""
    for line in exec_proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key == "ExecStart":
            exec_start = value
            break
    if expected_exec not in exec_start:
        return CheckResult(
            "Bluetooth pairing policy",
            "fail",
            f"bt-agent.service ExecStart is not the JTS no-code agent: {exec_start}",
            reason=REASON_BT_PAIRING_WRONG_AGENT,
        )

    bt_or_exc = _cached_bluetoothctl_show()
    if isinstance(bt_or_exc, FileNotFoundError):
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but bluetoothctl unavailable — adapter gate not checked",
            reason=REASON_BT_PAIRING_BLUETOOTHCTL_UNAVAILABLE,
        )
    if isinstance(bt_or_exc, Exception):
        raise bt_or_exc
    bt = bt_or_exc
    if bt.returncode != 0:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but bluetoothctl show failed — adapter gate not checked",
            reason=REASON_BT_PAIRING_ADAPTER_STATE_UNKNOWN,
        )

    values: dict[str, str] = {}
    for line in bt.stdout.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            values[key] = value.strip().split(" ", 1)[0].lower()
    discoverable = values.get("Discoverable")
    pairable = values.get("Pairable")
    if discoverable is None or pairable is None:
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but adapter Discoverable/Pairable state was not reported",
            reason=REASON_BT_PAIRING_ADAPTER_STATE_UNKNOWN,
        )
    if pairable == "yes" and discoverable != "yes":
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            "agent OK, but Pairable=yes outside an open pairing window; "
            "the runtime floor should close Pairable shortly",
            reason=REASON_BT_PAIRING_PAIRABLE_WITHOUT_DISCOVERABLE,
        )
    if discoverable == "yes" or pairable == "yes":
        return CheckResult(
            "Bluetooth pairing policy",
            "warn",
            f"agent OK, pairing window open (Discoverable={discoverable}, Pairable={pairable})",
            reason=REASON_BT_PAIRING_WINDOW_OPEN,
        )
    return CheckResult(
        "Bluetooth pairing policy",
        "ok",
        "JTS no-code agent active; pairing window closed",
    )

@doctor_check(label="Spotify auth", needs_cfg=True)
def check_spotify_cache(cfg: Config) -> CheckResult:
    """Verify Spotify is authenticated. Prefers the multi-account
    registry (per-household-member accounts, the modern path) over the
    legacy single-account cache. Reports OK if either has a usable
    refresh token. The earlier "cache missing" warning was a false
    positive on installs using only the multi-account setup."""
    if not cfg.spotify_enabled:
        return CheckResult(
            "Spotify auth", "skipped", "not configured",
            reason=REASON_SPOTIFY_NOT_CONFIGURED,
        )
    # Modern path: per-account registry at spotify_accounts_path.
    try:
        from ...accounts import Registry
        registry = Registry.load(cfg.spotify_accounts_path)
    except Exception:  # noqa: BLE001
        registry = None
    if registry is not None and registry.accounts:
        authed = []
        for acct in registry.accounts:
            try:
                if Path(acct.cache_path).exists():
                    authed.append(acct.name)
            except (OSError, AttributeError):
                pass
        if authed:
            return CheckResult(
                "Spotify auth", "ok",
                f"{len(authed)} account(s) cached: {', '.join(authed)}",
            )
        return CheckResult(
            "Spotify auth", "ok",
            f"{len(registry.accounts)} account(s) registered but no token "
            f"caches found under {Path(cfg.spotify_accounts_path).parent}/"
            f"caches/. Visit {cfg.spotify_setup_url} to re-link.",
            reason=REASON_SPOTIFY_CACHE_MISSING,
        )
    # Fall back to legacy single-account cache for installs that
    # haven't migrated to the multi-account registry.
    p = Path(cfg.spotify_cache_path)
    if not p.exists():
        return CheckResult(
            "Spotify auth", "ok",
            f"no accounts registered ({cfg.spotify_accounts_path}) and "
            f"no legacy cache at {p}. Visit {cfg.spotify_setup_url} to "
            f"link an account.",
            reason=REASON_SPOTIFY_CACHE_MISSING,
        )
    return CheckResult("Spotify auth", "ok", f"legacy cache at {p}")

@doctor_check(label="Spotify Connect device", needs_cfg=True)
def check_spotify_connect_device(cfg: Config) -> CheckResult:
    """Verify the on-Pi librespot endpoint is visible to at least one
    configured Spotify account, with a broadcast name matching the
    /speaker/ display name (substring match).

    This is the cold-start playback path: when no AirPlay is active,
    `spotify_play` falls through to `resolve_target` → librespot.
    `_find_librespot_id` does a case-insensitive substring match of
    the configured pattern against `sp.devices()[].name`. If the
    pattern doesn't match what librespot is broadcasting, every
    cold-start `play X` returns 'no spotify target device available'
    — a silent severe failure this check catches."""
    parked = _parked_follower_result("Spotify Connect device")
    if parked is not None:
        return parked
    label = "Spotify Connect device"
    intentional_off = _intentional_source_off(
        Source.SPOTIFY,
        label,
        units=("librespot.service",),
    )
    if intentional_off is not None:
        return intentional_off
    if not cfg.spotify_enabled:
        return CheckResult(
            label, "skipped", "not configured",
            reason=REASON_SPOTIFY_NOT_CONFIGURED,
        )

    pattern = cfg.spotify_device_name.strip().lower()
    if not pattern:
        return CheckResult(
            label, "fail",
            "speaker name is empty. Visit http://jts.local/speaker/ "
            "and set a display name (default 'JTS').",
            reason=REASON_SPOTIFY_DEVICE_NAME_EMPTY,
        )

    # Build clients and probe each account's sp.devices() for a match.
    try:
        from ...accounts import Registry
        from ...spotify_router import build_clients
        accounts = Registry.load(cfg.spotify_accounts_path)
        result = build_clients(
            accounts,
            client_id=cfg.spotify_client_id,
            redirect_uri=cfg.spotify_redirect_uri,
        )
        clients = result.clients
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            label, "warn",
            f"could not build Spotify clients: {_exception_detail(e)}. "
            f"This usually means no accounts have OAuth tokens — visit "
            f"{cfg.spotify_setup_url} to link an account.",
            reason=REASON_SPOTIFY_CLIENT_BUILD_FAILED,
        )
    if not clients:
        return CheckResult(
            label, "ok",
            f"no accounts have OAuth tokens (visit {cfg.spotify_setup_url}). "
            f"Once linked, this check will verify librespot visibility.",
            reason=REASON_SPOTIFY_NO_TOKENS,
        )

    matched_accounts: list[str] = []
    missed_accounts: list[str] = []
    seen_names_overall: set[str] = set()
    for account_name, ac in clients.items():
        try:
            devices = ac.sp.devices()
        except Exception as e:  # noqa: BLE001
            missed_accounts.append(
                f"{account_name} (devices fetch failed: {_exception_detail(e)})"
            )
            continue
        names = [(d.get("name") or "") for d in devices.get("devices", [])]
        seen_names_overall.update(names)
        if any(pattern in n.lower() for n in names):
            matched_accounts.append(account_name)
        else:
            missed_accounts.append(account_name)

    if matched_accounts and not missed_accounts:
        return CheckResult(
            label, "ok",
            f"{cfg.spotify_device_name!r} visible to all "
            f"{len(matched_accounts)} account(s): {', '.join(matched_accounts)}",
        )
    if matched_accounts and missed_accounts:
        return CheckResult(
            label, "warn",
            f"{cfg.spotify_device_name!r} visible to {matched_accounts} "
            f"but NOT {missed_accounts}. Cold-start `play X` will work "
            f"only for the matched account(s). Try opening Spotify on the "
            f"missing account and casting to the device once to register it.",
            reason=REASON_SPOTIFY_DEVICE_VISIBLE_TO_SOME,
        )
    return CheckResult(
        label, "fail",
        f"no account sees a device matching "
        f"{cfg.spotify_device_name!r}. Devices currently visible to the "
        f"linked accounts: {sorted(seen_names_overall)}. "
        f"Fix: open Spotify on a phone/desktop logged into the linked "
        f"account, click the cast/devices icon, select the speaker "
        f"once to make it discoverable; or verify librespot is running "
        f"(`systemctl status librespot`) and broadcasting "
        f"(`avahi-browse -tr _spotify-connect._tcp`).",
        reason=REASON_SPOTIFY_DEVICE_NOT_VISIBLE,
    )

@doctor_check()
def check_shairport_sync_loopback_plughw() -> CheckResult:
    """Verify the deployed shairport-sync.conf uses a multi-writer-safe
    renderer device that MATCHES the lane map's intent.

    Canonical is transport-dependent since U3/P6d: `shairport_substream`
    (AirPlay's private snd-aloop fan-in lane — the unarmed/fleet default)
    or `shairport_ring_lane` (the SHM ring, when the airplay renderer lane
    is armed). Both names come from the `airplay` row in
    `jasper.renderer_lanes.RENDERER_LANES` — never respelled here — and the
    armed set from the same lane map `jasper-apply-airplay-mode` reads, so
    this check and the conf renderer cannot disagree about intent.

    The conf is a DERIVED artifact re-rendered at every unit start, so a
    conf that disagrees with the armed set means exactly one thing: the
    unit has not restarted since the map changed. That is the half-flip
    window the arm CLI's restart_required instruction exists to close, and
    naming it here makes that instruction verifiable after the fact.

    A stale `jasper_renderer_in` value means shairport is still pointed at
    the retired renderer-side dmix path. Legacy `plughw:Loopback,0,0` and
    raw `hw:Loopback,0,0` are both stale now; the raw form is additionally
    broken because it bypasses ALSA's plug layer. All three legacy
    remediations name the device the lane map resolves for THIS box —
    the ring PCM when the lane is armed, the aloop lane when it is not —
    rather than a hardcoded `shairport_substream`, which would send an
    armed box's operator to the wrong target.

    Check runs against the DEPLOYED file (not the repo) so it catches
    both kinds of drift: branch not yet merged, and manual on-Pi edits."""
    from jasper.renderer_lanes import device_for, lane_by_label, read_armed_labels

    label = "shairport-sync.conf: output_device"
    p = Path("/etc/shairport-sync.conf")
    if not p.exists():
        return CheckResult(
            label, "warn",
            f"{p} missing — shairport-sync may not be installed.",
            reason=REASON_SHAIRPORT_CONF_MISSING,
        )
    try:
        text = p.read_text()
    except OSError as e:
        return CheckResult(
            label, "warn", f"can't read {p}: {e}",
            reason=REASON_SHAIRPORT_CONF_UNREADABLE,
        )
    # Look for an active (non-comment) output_device line. Comments in
    # shairport-sync.conf use //; libconfig syntax. We tolerate the
    # value being quoted or unquoted, single or double quotes.
    active_lines = [
        ln.strip() for ln in text.splitlines()
        if ln.strip().startswith("output_device")
    ]
    if not active_lines:
        return CheckResult(
            label, "warn",
            "no `output_device` directive found in alsa block; relying "
            "on shairport-sync's default (probably wrong).",
            reason=REASON_SHAIRPORT_NO_OUTPUT_DEVICE,
        )
    line = active_lines[0]
    lane = lane_by_label("airplay")
    if lane is None:  # registry regression — a doctor check never raises
        return CheckResult(
            label, "warn",
            "no `airplay` row in jasper.renderer_lanes.RENDERER_LANES — "
            "cannot judge the rendered device against the lane map",
            reason=REASON_SHAIRPORT_LANE_REGISTRY_MISSING,
        )
    armed = lane.label in read_armed_labels()
    if lane.ring_device in line:
        if armed:
            return CheckResult(
                label, "ok",
                f"{lane.ring_device} (renderer ring lane, armed)",
                reason=REASON_SHAIRPORT_RING_ARMED_OK,
            )
        return CheckResult(
            label, "warn",
            f"conf renders {lane.ring_device} but the {lane.label} lane is "
            "NOT armed — shairport-sync has not restarted since the disarm, "
            "and is writing a ring jasper-fanin no longer reads (silent "
            f"AirPlay). Restart {lane.unit} (its ExecStartPre re-renders "
            "the conf from the lane map).",
            reason=REASON_SHAIRPORT_RING_DISARMED_STALE,
        )
    if lane.aloop_device in line:
        if armed:
            return CheckResult(
                label, "warn",
                f"the {lane.label} lane is ARMED but the conf still renders "
                f"{lane.aloop_device} — shairport-sync has not restarted "
                "since the arm, so it is writing the aloop lane while "
                "jasper-fanin reads the ring (silent AirPlay). Restart "
                f"{lane.unit} (its ExecStartPre re-renders the conf from "
                "the lane map).",
                reason=REASON_SHAIRPORT_ALOOP_ARMED_STALE,
            )
        return CheckResult(
            label, "ok",
            f"{lane.aloop_device} (fan-in private AirPlay lane)",
            reason=REASON_SHAIRPORT_ALOOP_UNARMED_OK,
        )
    # One spelling of the armed→device rule, shared with the arm CLI and the
    # rendered map (jasper.renderer_lanes.device_for) rather than restated.
    expected_device = device_for(lane, armed)
    if "jasper_renderer_in" in line:
        return CheckResult(
            label, "fail",
            "jasper_renderer_in — stale retired dmix path. Re-run "
            f"deploy/install.sh so shairport renders to {expected_device}.",
            reason=REASON_SHAIRPORT_LEGACY_DMIX,
        )
    if 'plughw:Loopback' in line:
        return CheckResult(
            label, "ok",
            "plughw:Loopback,0,0 — stale pre-fan-in wiring. Redeploy "
            f"to render {expected_device}.",
            reason=REASON_SHAIRPORT_LEGACY_PLUGHW,
        )
    if '"hw:Loopback' in line or "'hw:Loopback" in line:
        return CheckResult(
            label, "fail",
            "output_device uses raw `hw:Loopback,0,0` — AirPlay sessions "
            "will be silently rejected because Loopback is locked at "
            "48 kHz and shairport requests 44.1 kHz. Symptom: iPhone / "
            "Mac sees the speaker in the picker but can't establish a session. "
            f"Fix: redeploy via `bash scripts/deploy-to-pi.sh` (this box "
            f"renders {expected_device}). Source of truth: "
            "deploy/shairport-sync.conf.template.",
            reason=REASON_SHAIRPORT_RAW_HW_LOOPBACK,
        )
    return CheckResult(
        label, "warn",
        f"output_device value not recognized: {line!r}",
        reason=REASON_SHAIRPORT_DEVICE_UNRECOGNIZED,
    )

# Renderer registry: (label_suffix, runtime_user, parse_function).
# parse_function returns the configured device name, or None if not
# discoverable. Centralising the registry here keeps the probe loop
# below uniform across renderers; adding a fourth renderer is one
# entry.
def _read_first_line_matching(path: Path, predicate) -> Optional[str]:
    """Scan a config file for the first line where `predicate(line)`
    returns truthy. Returns the line stripped, or None."""
    try:
        for ln in path.read_text().splitlines():
            if predicate(ln):
                return ln.strip()
    except OSError:
        return None
    return None

def _renderer_device_shairport() -> Optional[str]:
    """shairport-sync: parse /etc/shairport-sync.conf for output_device.
    Format: `output_device = "shairport_substream";` (libconfig syntax)."""
    ln = _read_first_line_matching(
        Path("/etc/shairport-sync.conf"),
        lambda line: (
            line.lstrip().startswith("output_device")
            and "=" in line
            and not line.lstrip().startswith("//")
        ),
    )
    if not ln:
        return None
    # output_device = "DEVNAME"; — pull the quoted string.
    m = re.search(r'"([^"]+)"', ln) or re.search(r"'([^']+)'", ln)
    return m.group(1) if m else None

def _renderer_device_librespot() -> Optional[str]:
    """librespot: parse the ExecStart= line(s) in librespot.service for
    --device. systemd allows ExecStart to span multiple lines via
    backslash continuation."""
    p = Path("/etc/systemd/system/librespot.service")
    try:
        text = p.read_text()
    except OSError:
        return None
    # Collapse line continuations so we can scan the full ExecStart.
    flat = text.replace("\\\n", " ")
    for ln in flat.splitlines():
        s = ln.strip()
        if not s.startswith("ExecStart=") or "--device" not in s:
            continue
        # --device <DEVNAME>  (may be quoted)
        m = re.search(r"--device\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))", s)
        if m:
            return m.group(1) or m.group(2) or m.group(3)
    return None

def _renderer_device_bluealsa() -> Optional[str]:
    """bluealsa-aplay: parse the drop-in ExecStart= for --pcm=DEVNAME."""
    # The drop-in is mode-0644 readable; doctor runs as root anyway.
    for path in (
        Path("/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf"),
        Path("/etc/systemd/system/bluealsa-aplay.service.d/override.conf"),
    ):
        try:
            text = path.read_text()
        except OSError:
            continue
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("ExecStart=") and "--pcm=" in s:
                m = re.search(r"--pcm=(\S+)", s)
                if m:
                    return m.group(1)
    return None

def _systemd_unit_user(unit: str) -> tuple[Optional[str], str]:
    """`(User=, LoadState=)` for `unit`. Empty User on a LOADED unit is root.

    LoadState rides along because it is the ONLY thing separating "runs as
    root" from "does not exist": `systemctl show` exits 0 with an empty User=
    for an absent unit, so a renamed renderer would otherwise degrade into a
    root probe that passes — asserting "as the unit's real User=" with no unit.
    """
    state = evidence.unit_state(unit)
    load_state = (state or {}).get("load_state") or "unknown"
    users = evidence.unit_property("User", (unit,))
    user = (users[0] or None) if users else None
    return user, load_state

def _renderer_lane_device_overrides() -> dict[str, str]:
    """Renderer `--device` values the lane map declares (U3 / P6).

    The lane map is the SSOT for which PCM each migrated renderer writes, so
    resolving from it is exact by construction rather than reconstructed from a
    systemd surface. This is the pattern-3 shape the rest of the ring platform
    uses: the reconciler/CLI is the single writer of the resolved value, and
    every reader — the daemon, the doctor — reads that same file.

    Returns `{}` when NO map exists, which is the shipped fleet state. That
    emptiness is load-bearing: an absent map has no opinion about any device, so
    it must not assert the aloop default over a genuine operator override that
    `/proc/<MainPID>/environ` can see. (`fanin_env_expectations` deliberately
    names every lane's device including the unarmed ones — that is right for a
    drift check against a map that exists, and wrong as a claim when none does.)
    """
    try:
        from jasper import renderer_lanes as rl
    except ImportError:  # pragma: no cover - the package is always present
        return {}
    if not os.path.exists(rl.RENDERER_LANES_ENV):
        return {}
    try:
        return rl.fanin_env_expectations()
    except (OSError, ValueError):
        # Fail-SOFT, as this function's contract says: a torn or malformed map
        # must degrade to "no opinion" and let the next tier answer, never raise
        # into a doctor check. ValueError belongs here alongside OSError because
        # the map is parsed, not just read.
        return {}


def _unit_runtime_environ(unit: str) -> dict[str, str]:
    """The FULLY RESOLVED environment the running unit was exec'd with.

    Read from `/proc/<MainPID>/environ`, which carries the complete
    `EnvironmentFile=` chain. `systemctl show -p Environment` does NOT: see
    :func:`_resolve_systemd_env_vars` for why that matters here.

    Returns `{}` when the unit has no MainPID (parked/stopped/failed) or the
    environ cannot be read — the caller then falls back, and a genuinely
    unresolvable `${VAR}` reaches aplay and fails loudly, which is correct.
    """
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    pid = r.stdout.strip()
    if r.returncode != 0 or not pid.isdigit() or pid == "0":
        return {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        name, _, value = entry.partition(b"=")
        out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _resolve_systemd_env_vars(device: str, unit: str) -> str:
    """Expand `${VAR}` references in a device string to what the renderer
    actually writes.

    Renderer units use a `${VAR}` device so a per-box ring flip is one env write
    rather than a unit edit (U3 / P6). systemd expands the reference at daemon
    start; the doctor reading the unit file sees the literal `${VAR}`, and
    passing that to aplay would fail with "Unknown PCM ${VAR}" — a false
    positive.

    **`systemctl show -p Environment` CANNOT answer this.** That property
    returns the unit's `Environment=` directives ONLY — it does not include
    `EnvironmentFile=` layers, which is exactly where every JTS runtime
    override lives: an armed box's real `PERIOD_FRAMES` / `ACTIVE_LANE`
    values can be invisible to it entirely, which is why this reads
    `/proc/<MainPID>/environ` instead. Trusting the old surface here would
    have made the doctor probe an ARMED box's *aloop* device — reporting a
    lane healthy while the live ring lane went unprobed.

    Three sources, most authoritative first:

    1. **The lane map** (`jasper.renderer_lanes`) — the SSOT that WROTE the
       override. Exact by construction, and readable whether or not the unit is
       running.
    2. **`/proc/<MainPID>/environ`** — the running daemon's real environment,
       the arm.sh precedent. Catches an operator override the lane map does not
       know about, and disagreement with (1) is itself worth surfacing.
    3. **`systemctl show -p Environment`** — kept LAST, and only for what it can
       actually answer: a genuine in-unit `Environment=` directive on a unit
       that is not running.

    Returns the original string unchanged when it contains no `${VAR}` or when
    nothing can resolve it (best-effort — the caller's aplay probe then fails
    loudly with a clear error, which is the right behavior).
    """
    if "${" not in device:
        return device

    env_map: dict[str, str] = {}
    # Least authoritative first, so the better source overwrites it.
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "--value"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            env_map.update(_parse_systemd_environment(r.stdout))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    observed = _unit_runtime_environ(unit)
    env_map.update(observed)
    declared = _renderer_lane_device_overrides()
    # The map WINS (it is what the next restart will apply), but a disagreement
    # with what the daemon is actually running is the single most diagnostic
    # fact available here — it means the unit has not restarted since the lane
    # was armed or disarmed, which is exactly the half-flip window. Naming it
    # costs one line; swallowing it leaves an operator comparing a healthy-
    # looking probe against a lane that is silent.
    for name, value in declared.items():
        was = observed.get(name)
        if was is not None and was != value:
            # The unit has not restarted since the lane map changed; the
            # probe follows the map.
            log_event(
                _LANE_LOG,
                "renderer_lane.device_disagreement",
                level=logging.WARNING,
                unit=unit,
                key=name,
                lane_map=value,
                running=was,
            )
    env_map.update(declared)

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return env_map.get(name, match.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, device)

#: The probe is bounded by its OWN work, not by outlasting a timer: `aplay -s`
#: writes `_PROBE_FRAMES` frames (100 ms at 48 kHz, per channel) then exits 0
#: after open → prepare → write → drain — ~0.13 s (ring) / ~0.16 s (aloop) on a
#: Pi. `_PROBE_TIMEOUT_SEC` is a backstop only: a kill (124) is a FAILURE, since
#: a probe that never finished proved nothing. It **MUST exceed TWICE
#: `JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS`** (500 ms, `jts_ring_shm.h`) — both of a
#: contended open's lock waits ADD inside `snd_pcm_prepare`, and an open that
#: wins at the end of them must not be killed into a false failure (jts3: a
#: singly contended open returns EBUSY at 0.55 s). Pinned, with the full
#: rationale, by `test_renderer_ring_lanes.py`.
_PROBE_FRAMES = "4800"
_PROBE_TIMEOUT_SEC = "2.0"


class ProbeOutcome(Enum):
    """What a probe proved: the PCM opened; or it opened as far as a
    single-writer lock a live writer holds (BUSY — resolution IS proven,
    WHOSE writer is judged next); or it never resolved (the PR #223 class)."""

    OPENED = "opened"
    BUSY = "busy"
    FAILED = "failed"


#: The ring ioplug's own refusal, out of `jts_ring_prepare`
#: (`c/jts-ring-ioplug/pcm_jts_ring.c`); BOTH halves must sit on ONE line, as a
#: slave open further down the chain reports its own EBUSY — spelled
#: `failed (-16)`, never `rc=-16` — which `rc=-16` alone would swallow.
#: `_ALOOP_BUSY_MARKER` is aplay's refusal for an snd-aloop substream that
#: already has a writer, anchored on its prefix likewise. Both captured on jts3.
_RING_BUSY_MARKERS = ("jts_ring: writer_open(", "rc=-16")
_ALOOP_BUSY_MARKER = "audio open error: Device or resource busy"


def _busy_marker_line(stderr: str) -> Optional[str]:
    """The stderr line proving the PCM opened and hit a single-writer lock.
    EVERY line is scanned — `_classify_probe` owns why a slice cannot (#3515)."""
    for line in stderr.splitlines():
        if all(m in line for m in _RING_BUSY_MARKERS) or _ALOOP_BUSY_MARKER in line:
            return line.strip()
    return None


def _classify_probe(returncode: int, stderr: str) -> tuple[ProbeOutcome, str]:
    """The ONE place a probe's (returncode, stderr) becomes a verdict.

    A busy marker decides BEFORE the return code, and every line is searched:
    the SNDERR fires the instant the lock wait expires, aplay then dumps ~15
    hw_params lines, and the outer `timeout` can land anywhere in them — so a
    contended probe can carry the marker AND be killed at 124 (#3515). It
    cannot carry one and exit 0, both markers being fatal where they print.

    Otherwise ONLY a clean exit is success: aplay bounds itself at
    `_PROBE_FRAMES`, so rc 0 means open, prepare, write and drain all
    completed. Everything else failed, 124 included: a killed probe finished
    nothing and proves nothing.
    """
    marker = _busy_marker_line(stderr)
    detail = (marker or stderr.strip())[:200]
    if marker:
        return ProbeOutcome.BUSY, detail
    if returncode == 0:
        return ProbeOutcome.OPENED, detail
    if returncode == 124:
        detail = f"killed at {_PROBE_TIMEOUT_SEC}s with the burst unfinished"
    return ProbeOutcome.FAILED, detail or f"exit={returncode}"


def _probe_open_as_user(
    device: str, user: Optional[str],
) -> tuple[ProbeOutcome, str]:
    """Open `device` for a short silence burst AS `user`, and classify it.

    Why aplay + /dev/zero: it exercises the same code path the renderer uses
    (alsalib's snd_pcm_open through the user-space plugin chain) while writing
    only silence — additive into any mix, so safe while music is playing. The
    burst is `_PROBE_FRAMES`, the kill guard `_PROBE_TIMEOUT_SEC`; read both
    notes before changing either.
    """
    # `env LC_ALL=C` rides INSIDE the command because sudo resets the
    # environment: `_ALOOP_BUSY_MARKER` is snd_strerror text, so translatable.
    cmd = [
        "env", "LC_ALL=C",
        "timeout", _PROBE_TIMEOUT_SEC,
        "aplay", "-q",
        "-s", _PROBE_FRAMES,
        "-D", device,
        "-c", "2", "-r", "48000", "-f", "S16_LE",
        "/dev/zero",
    ]
    if user:
        cmd = ["sudo", "-n", "-u", user, *cmd]
    try:
        r = _run(cmd, timeout=float(_PROBE_TIMEOUT_SEC) + 2.0)
    # OSError, not just FileNotFoundError: a fork hitting ENOMEM on a 1 GB Pi
    # must fail this renderer, not crash the check.
    except (OSError, subprocess.TimeoutExpired) as e:
        return ProbeOutcome.FAILED, f"probe subprocess failed: {e}"
    return _classify_probe(r.returncode, r.stderr or "")

# The playback (write-side) renderer lanes that own an snd-aloop substream. USB is
# NOT here: jasper-fanin DIRECT-captures hw:UAC2Gadget rather than reading an aloop
# write lane (the usbsink_substream=3 solo bridge was removed 2026-07-10).
_FANIN_PRIVATE_RENDERER_DEVICES = {
    "librespot_substream": 0,
    "shairport_substream": 1,
    "bluealsa_substream": 2,
}

def _ring_renderer_devices() -> dict[str, str]:
    """Ring-lane PCM name -> the fan-in lane LABEL whose ring it carries.

    DERIVED from `jasper.renderer_lanes.RENDERER_LANES`, not hand-listed. Like
    the aloop lanes above these are single-writer, but the exclusivity is
    enforced by the ioplug's writer guard rather than by snd-aloop, and the
    owner pid lives in the ring HEADER rather than in /proc/asound.

    Deriving it is load-bearing: a lane hand-listed here could be forgotten
    when one is added to the registry, and that lane's busy probe would then
    fail with "not a known fan-in ring lane" — a red doctor on a healthy box.
    """
    from jasper.renderer_lanes import RENDERER_LANES

    return {lane.ring_device: lane.label for lane in RENDERER_LANES}


def _cgroup_owner_is_unit(pid: object, unit: str) -> tuple[bool, str]:
    """Does `pid` run inside the SYSTEM manager's `unit`? Fail-closed: an
    empty `unit` would make the membership test match any cgroup at all, and
    an unreadable /proc entry proves nothing.

    Rejecting `/user.slice/` is what stops a same-named unit under a per-user
    manager (`/user.slice/user-N.slice/user@N.service/app.slice/<unit>`). Do
    NOT tighten that to a `/system.slice/` prefix: JTS renderers declare
    `Slice=jts-audio.slice`, so a real owner reads
    `/jts.slice/jts-audio.slice/<unit>` and would be rejected.
    """
    if not unit:
        return False, "no unit to attribute the writer to"
    path = Path(f"/proc/{pid}/cgroup")
    try:
        cgroup = path.read_text()
    except OSError as e:
        return False, f"could not read {path}: {e}"
    if f"/{unit}" in cgroup and "/user.slice/" not in cgroup:
        return True, ""
    return False, f"cgroup={cgroup.strip()!r}"


def _ring_lane_busy_owner_matches(device: str, unit: str) -> tuple[bool, str]:
    """Return whether an EBUSY renderer RING lane is owned by `unit`.

    The ring's exact analogue of the aloop `owner_pid` check below: the caller
    has already established that the probe opened the PCM (a BUSY verdict), so
    this only has to separate "the renderer legitimately owns its ring" from
    "some stray process is writing frames into the mix".
    """
    label = _ring_renderer_devices().get(device)
    if label is None:
        return False, "not a known fan-in ring lane"
    from jasper.renderer_lanes import ring_writer_pid

    pid = ring_writer_pid(label)
    if pid is None:
        return False, f"ring for lane {label} names no writer"
    owned, why = _cgroup_owner_is_unit(pid, unit)
    if owned:
        return True, f"busy/owned pid={pid} (ring writer)"
    return False, f"busy but ring writer pid={pid} {why}"


def _fanin_lane_busy_owner_matches(device: str, unit: str) -> tuple[bool, str]:
    """Return whether an EBUSY private fan-in lane is owned by `unit`.

    A BUSY verdict proves the PCM resolved, not that the expected renderer
    owns the lane; `/proc/asound` publishes the aloop `owner_pid`, and ring
    lanes take the sibling path above for the same fact.

    RETIREMENT. This aloop branch survives the audio-graph consolidation
    (#2285) because a renderer whose lane is NOT armed for ring ingress still
    writes its snd-aloop substream, and `/proc/asound` is then the only place
    its owner pid exists. It retires with the snd-aloop renderer lanes
    themselves, taking `_FANIN_PRIVATE_RENDERER_DEVICES` with it. Fleet arming
    state is not that trigger: an all-armed fleet has not deleted the aloop
    lanes from the code, and an un-armed box would still open one.
    """
    if device in _ring_renderer_devices():
        return _ring_lane_busy_owner_matches(device, unit)
    substream = _FANIN_PRIVATE_RENDERER_DEVICES.get(device)
    if substream is None:
        return False, "not a known fan-in private lane"
    text = evidence.loopback_substreams().get(substream)
    if text is None:
        return False, f"could not read Loopback substream {substream} status"
    m = re.search(r"owner_pid\s*:\s*(\d+)", text)
    if not m:
        return False, f"Loopback substream {substream} status has no owner_pid"
    pid = m.group(1)
    owned, why = _cgroup_owner_is_unit(pid, unit)
    if owned:
        return True, f"busy/owned pid={pid}"
    return False, f"busy but owner pid={pid} {why}"

@doctor_check(exclusive_group="audio-probe", core=True)
def check_renderer_device_resolvable() -> CheckResult:
    """Verify each music renderer can actually open the ALSA device it is
    configured to write to, AS its runtime systemd User=.

    The original bug this catches: renderer users could not read the
    asoundrc that defined the named ALSA PCMs, so snd_pcm_open() returned
    "Unknown PCM" despite config strings looking right.

    Fan-in caveat: renderer lanes are intentionally private single-writer
    lanes, so probing one whose renderer is active is refused with EBUSY —
    which PROVES the PCM resolved and opened. `_classify_probe` calls that
    BUSY, and only then does the lane's published owner pid decide whether the
    expected unit holds it. A busy verdict is never a substitute for the
    probe: a probe that failed for any other reason stays a failure however
    healthy the lane looks.

    Returns:
      ok    — all configured renderers can open their device as their user
      fail  — any renderer can't open its device (this is the bug class)
      warn  — a renderer's device or user wasn't discoverable (likely not
              installed; informational)
    """
    label = "renderer ALSA device resolvable"
    renderers = [
        ("shairport-sync", "shairport-sync.service",
         _renderer_device_shairport),
        ("librespot",      "librespot.service",
         _renderer_device_librespot),
        ("bluealsa-aplay", "bluealsa-aplay.service",
         _renderer_device_bluealsa),
    ]
    failures: list[str] = []
    incomplete: list[str] = []
    successes: list[str] = []
    for name, unit, parse_dev in renderers:
        device = parse_dev()
        if device is None:
            incomplete.append(f"{name}: config not found (not installed?)")
            continue
        # If the parsed device contains a ${VAR} reference, ask systemd
        # what value it would substitute at ExecStart time. Otherwise
        # the aplay probe below will fail with "Unknown PCM ${VAR}" —
        # a false positive, since the running daemon has resolved it.
        resolved_device = _resolve_systemd_env_vars(device, unit)
        user, load_state = _systemd_unit_user(unit)
        if load_state != "loaded":
            failures.append(f"{name}: {unit} is {load_state}, not loaded")
            continue
        outcome, detail = _probe_open_as_user(resolved_device, user)
        who = user or "root"
        # Show both the literal-parsed and resolved values when they
        # differ, so the operator can spot a misconfigured env file
        # without re-reading the unit themselves.
        display = (
            f"{resolved_device}"
            if resolved_device == device
            else f"{resolved_device} (from {device})"
        )
        if outcome is ProbeOutcome.OPENED:
            successes.append(f"{name}({who})→{display}")
        elif outcome is ProbeOutcome.BUSY:
            owned, owner_detail = _fanin_lane_busy_owner_matches(
                resolved_device, unit,
            )
            if owned:
                successes.append(f"{name}({who})→{display} {owner_detail}")
            else:
                failures.append(f"{name}({who})→{display}: {owner_detail}")
        else:
            failures.append(f"{name}({who})→{display}: {detail}")
    if failures:
        return CheckResult(
            label, "fail",
            "; ".join(failures) + ". This is the bug class PR #223 "
            "addressed — verify /etc/asound.conf exists and is mode "
            "0644 so non-root renderer users can resolve user-space "
            "ALSA PCM names. EBUSY is expected only for active fan-in "
            "private lanes; Unknown PCM is always a real failure.",
            reason=REASON_RENDERER_DEVICE_UNRESOLVABLE,
        )
    if not successes:
        # All renderers were unknown — probably a stripped image.
        return CheckResult(
            label, "warn",
            "; ".join(incomplete) if incomplete
            else "no renderers configured",
            reason=REASON_RENDERER_NONE_CONFIGURED,
        )
    detail = "; ".join(successes)
    if incomplete:
        detail += " (skipped: " + "; ".join(incomplete) + ")"
    return CheckResult(label, "ok", detail)


def _classify_mux_mode(path: Path) -> CheckResult:
    """Classify the persisted jasper-mux source-selection mode at `path`.

    Split from the check so tests can point it at a tmp file. Granular
    on purpose — the runtime reader (`mux_mode_persistence.
    read_manual_source`) deliberately collapses missing/corrupt/unknown
    to None (fail-open to auto), which is right for the daemon but means
    a household's lost pin is silent. The doctor line tells the operator
    WHICH state the file is in."""
    name = "mux mode state"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Normal: auto mode never persisted a pin, or fresh install.
        return CheckResult(name, "ok", "auto (no source pin persisted)")
    except OSError as e:
        return CheckResult(
            name, "warn",
            f"unreadable ({e.__class__.__name__}) — mux falls back to "
            f"auto. Check permissions on {path}",
            reason=REASON_MUX_MODE_UNREADABLE,
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return CheckResult(
            name, "warn",
            f"corrupt — mux falls back to auto (a manual source pin, if "
            f"one was set, is lost). Delete to clear: {path}",
            reason=REASON_MUX_MODE_CORRUPT,
        )
    if not isinstance(data, dict) or data.get("mode") != "manual":
        return CheckResult(name, "ok", "auto (latest-source-wins)")
    label = data.get("selected_source")
    try:
        source = Source(label)
    except (TypeError, ValueError):
        source = None
    if source is None or source not in MUSIC_SOURCES:
        return CheckResult(
            name, "warn",
            f"manual pin to unknown source {label!r} — ignored, mux runs "
            f"auto. Re-pin via the landing page or delete {path}",
            reason=REASON_MUX_MODE_UNKNOWN_SOURCE,
        )
    return CheckResult(
        name, "ok", f"manual pin: {source.value}", reason=REASON_MUX_MODE_PINNED
    )


@doctor_check()
def check_mux_mode_state() -> CheckResult:
    """Surface the persisted source-selection mode (auto vs manual pin).

    A corrupt file or a pin to a source that no longer exists is
    fail-open at runtime (mux silently runs auto), so this line is the
    only place an operator learns the household's pin was dropped."""
    path = Path(
        os.environ.get("JASPER_MUX_MODE_STATE_PATH", _MUX_MODE_DEFAULT_PATH),
    )
    return _classify_mux_mode(path)
