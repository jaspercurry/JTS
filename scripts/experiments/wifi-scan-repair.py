#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-only Pi 5 Wi-Fi scan repair experiments.

This script keeps the disruptive experiments out of the household
`/wifi/` page. The non-disruptive critical-protocol-stop primitive now
lives in `jasper.wifi_scan_repair`, because `/wifi/scan` uses the same
bounded implementation for production self-heal.

Typical Pi-side usage after deploy:

  sudo python3 /home/pi/jts/scripts/experiments/wifi-scan-repair.py probe
  sudo python3 /home/pi/jts/scripts/experiments/wifi-scan-repair.py crit-stop
  sudo python3 /home/pi/jts/scripts/experiments/wifi-scan-repair.py bounce \\
      --i-have-physical-access
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jasper import wifi_scan_repair


DEFAULT_IFACE = wifi_scan_repair.DEFAULT_IFACE
DEFAULT_ROLLBACK_DELAY = 75
ROLLBACK_UNIT = "jasper-wifi-bounce-rollback"


@dataclass(frozen=True)
class Completed:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _run(argv: list[str], *, timeout: float = 20) -> Completed:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        return Completed(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except FileNotFoundError as e:
        return Completed(
            argv=list(argv),
            returncode=127,
            stdout="",
            stderr=str(e),
        )
    except subprocess.TimeoutExpired as e:
        stdout = (
            e.stdout.decode("utf-8", "replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        stderr = (
            e.stderr.decode("utf-8", "replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        return Completed(
            argv=list(argv),
            returncode=124,
            stdout=stdout,
            stderr=stderr or f"timed out after {timeout}s",
        )


def parse_nmcli_wifi_list(stdout: str) -> list[dict[str, Any]]:
    networks: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        fields = raw.split(":")
        if len(fields) < 5:
            continue
        in_use, ssid, signal, channel, security = fields[:5]
        if not ssid:
            continue
        try:
            signal_i = int(signal)
        except ValueError:
            signal_i = None
        networks.append({
            "ssid": ssid,
            "inUse": in_use == "*",
            "signal": signal_i,
            "channel": channel,
            "security": security or "--",
        })
    return networks


def recent_kernel_suppression() -> bool | None:
    proc = _run(
        ["journalctl", "-k", "-b", "-n", "120", "--no-pager"],
        timeout=3,
    )
    if proc.returncode != 0:
        return None
    return wifi_scan_repair.text_mentions_scan_suppression(
        proc.stdout,
        proc.stderr,
    )


def scan_probe(iface: str, *, rescan: bool = True) -> dict[str, Any]:
    rescan_proc: Completed | None = None
    if rescan:
        rescan_proc = _run(
            ["nmcli", "device", "wifi", "rescan", "ifname", iface],
            timeout=20,
        )
        time.sleep(1.5)

    list_proc = _run(
        [
            "nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,CHAN,SECURITY",
            "device", "wifi", "list", "ifname", iface,
        ],
        timeout=20,
    )
    networks = (
        parse_nmcli_wifi_list(list_proc.stdout)
        if list_proc.returncode == 0 else []
    )
    only_current = len(networks) == 1 and networks[0].get("inUse") is True
    log_suppressed = recent_kernel_suppression()
    command_suppressed = wifi_scan_repair.text_mentions_scan_suppression(
        rescan_proc.stdout if rescan_proc else "",
        rescan_proc.stderr if rescan_proc else "",
        list_proc.stdout,
        list_proc.stderr,
    )
    suppressed = command_suppressed or (
        log_suppressed is True
        and (only_current or list_proc.returncode != 0)
    )
    return {
        "iface": iface,
        "suppressed": suppressed,
        "onlyCurrentNetwork": only_current,
        "recentSuppressionLog": log_suppressed,
        "rescanReturncode": rescan_proc.returncode if rescan_proc else None,
        "listReturncode": list_proc.returncode,
        "networks": networks,
        "errors": {
            "rescan": (rescan_proc.stderr.strip() if rescan_proc else ""),
            "list": list_proc.stderr.strip(),
        },
    }


def active_wifi_profile(iface: str) -> str | None:
    proc = _run(
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
        timeout=5,
    )
    if proc.returncode != 0:
        return None
    for raw in proc.stdout.splitlines():
        fields = raw.split(":")
        if (
            len(fields) >= 3
            and fields[2] == iface
            and fields[1] in ("802-11-wireless", "wifi")
        ):
            return fields[0]
    return None


def ethernet_connected() -> bool:
    proc = _run(["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"], timeout=5)
    if proc.returncode != 0:
        return False
    for raw in proc.stdout.splitlines():
        fields = raw.split(":")
        if len(fields) >= 2 and fields[0] == "ethernet" and fields[1] == "connected":
            return True
    return False


def rollback_timer_command(profile: str, delay_s: int, unit: str = ROLLBACK_UNIT) -> list[str]:
    return [
        "systemd-run",
        f"--unit={unit}",
        f"--on-active={delay_s}",
        "--collect",
        "/usr/bin/nmcli",
        "--wait", "20",
        "connection", "up", profile,
    ]


def cancel_rollback_timer(unit: str = ROLLBACK_UNIT) -> None:
    _run(["systemctl", "stop", f"{unit}.timer", f"{unit}.service"], timeout=5)
    _run(["systemctl", "reset-failed", f"{unit}.timer", f"{unit}.service"], timeout=5)


def run_bounce(args: argparse.Namespace) -> dict[str, Any]:
    iface = args.iface
    profile = active_wifi_profile(iface)
    if not profile:
        raise RuntimeError(f"no active Wi-Fi profile found on {iface}")

    has_ethernet = ethernet_connected()
    if not has_ethernet and not args.i_have_physical_access:
        raise RuntimeError(
            "no Ethernet fallback detected; rerun with "
            "--i-have-physical-access if the Pi is physically recoverable",
        )

    before = scan_probe(iface)
    timer_cmd = rollback_timer_command(profile, args.rollback_delay)
    if args.dry_run:
        return {
            "experiment": "bounce",
            "dryRun": True,
            "iface": iface,
            "profile": profile,
            "hasEthernet": has_ethernet,
            "rollbackTimerCommand": timer_cmd,
            "before": before,
        }

    timer = _run(timer_cmd, timeout=10)
    if timer.returncode != 0:
        raise RuntimeError(
            "failed to schedule rollback timer before Wi-Fi bounce: "
            f"{timer.stderr or timer.stdout}",
        )

    down = _run(["nmcli", "--wait", "10", "connection", "down", profile], timeout=20)
    time.sleep(args.down_delay)
    up = _run(["nmcli", "--wait", "30", "connection", "up", profile], timeout=45)
    after = scan_probe(iface)
    if up.returncode == 0:
        cancel_rollback_timer()
    return {
        "experiment": "bounce",
        "dryRun": False,
        "iface": iface,
        "profile": profile,
        "hasEthernet": has_ethernet,
        "rollbackDelay": args.rollback_delay,
        "rollbackTimerScheduled": timer.returncode == 0,
        "downReturncode": down.returncode,
        "upReturncode": up.returncode,
        "before": before,
        "after": after,
        "errors": {
            "timer": timer.stderr.strip(),
            "down": down.stderr.strip(),
            "up": up.stderr.strip(),
        },
    }


def require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError(errno.EPERM, f"{action} requires root/CAP_NET_ADMIN")


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_probe(args: argparse.Namespace) -> int:
    print_json({"experiment": "probe", "probe": scan_probe(args.iface, rescan=not args.no_rescan)})
    return 0


def cmd_crit_stop(args: argparse.Namespace) -> int:
    require_root("crit-stop")
    before = scan_probe(args.iface)
    try:
        repair = wifi_scan_repair.send_crit_proto_stop(
            args.iface,
            dry_run=args.dry_run,
        )
        error = None
    except Exception as e:  # noqa: BLE001
        repair = {"iface": args.iface, "ack": False}
        error = repr(e)
    time.sleep(args.after_delay)
    after = scan_probe(args.iface)
    print_json({
        "experiment": "crit-stop",
        "before": before,
        "repair": repair,
        "after": after,
        "error": error,
    })
    return 0 if error is None else 1


def cmd_bounce(args: argparse.Namespace) -> int:
    require_root("bounce")
    print_json(run_bounce(args))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operator-only Wi-Fi scan repair experiments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--iface", default=DEFAULT_IFACE)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="read-only scan/suppression probe")
    p_probe.add_argument("--no-rescan", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_crit = sub.add_parser("crit-stop", help="send NL80211_CMD_CRIT_PROTOCOL_STOP")
    p_crit.add_argument("--dry-run", action="store_true")
    p_crit.add_argument("--after-delay", type=float, default=1.5)
    p_crit.set_defaults(func=cmd_crit_stop)

    p_bounce = sub.add_parser("bounce", help="down/up the active Wi-Fi profile")
    p_bounce.add_argument("--i-have-physical-access", action="store_true")
    p_bounce.add_argument("--rollback-delay", type=int, default=DEFAULT_ROLLBACK_DELAY)
    p_bounce.add_argument("--down-delay", type=float, default=2.0)
    p_bounce.add_argument("--dry-run", action="store_true")
    p_bounce.set_defaults(func=cmd_bounce)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as e:  # noqa: BLE001
        print_json({"ok": False, "error": str(e), "type": type(e).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
