"""Bounded repair for Pi 5 brcmfmac Wi-Fi scan suppression.

The Pi 5's brcmfmac firmware can leave BRCMF_SCAN_STATUS_SUPPRESS set
after a DHCP or coexistence event. When that happens, scan requests
return EAGAIN and NetworkManager usually shows only the current SSID.

The least disruptive repair we found in live testing is to send
NL80211_CMD_CRIT_PROTOCOL_STOP. That nudges the kernel/driver path
that normally clears scan suppression after a critical protocol window
without intentionally disconnecting Wi-Fi. This module keeps that
single primitive isolated, observable, and rate-limited.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_IFACE = "wlan0"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/wifi_scan_repair.json")
NL80211_HEADER = Path("/usr/include/linux/nl80211.h")

# A successful repair is cheap, but scans can happen on page load and by
# repeated user taps. Keep the automatic repair bounded.
DEFAULT_ATTEMPT_COOLDOWN_S = 60.0
DEFAULT_FAILURE_COOLDOWN_S = 300.0


# Netlink / generic-netlink constants. These are stable uapi values.
NETLINK_GENERIC = 16
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
CTRL_ATTR_VERSION = 3
NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04
NLMSG_ERROR = 0x02
NETLINK_TIMEOUT_S = 5.0
_REPAIR_LOCK = threading.Lock()


@dataclass(frozen=True)
class RepairResult:
    """Outcome of one automatic repair decision."""

    iface: str
    attempted: bool
    reason: str
    ack: bool | None = None
    driver: str | None = None
    cooldown_remaining: float | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in asdict(self).items() if v is not None}
        cooldown_remaining = payload.pop("cooldown_remaining", None)
        if cooldown_remaining is not None:
            payload["cooldownRemaining"] = cooldown_remaining
        return payload


def text_mentions_scan_suppression(*chunks: str | None) -> bool:
    text = "\n".join(chunk or "" for chunk in chunks).lower()
    return any(
        marker in text
        for marker in (
            "brcmf_cfg80211_scan: scanning suppressed",
            "scanning suppressed",
            "resource temporarily unavailable",
            "status (4)",
            "(-11)",
            "eagain",
        )
    )


def iface_driver_name(iface: str) -> str | None:
    """Return the kernel driver name for an interface, if visible."""
    try:
        return Path(f"/sys/class/net/{iface}/device/driver").resolve().name
    except OSError:
        return None


def maybe_repair_scan_suppression(
    iface: str = DEFAULT_IFACE,
    *,
    state_path: Path | str = DEFAULT_STATE_PATH,
    now: float | None = None,
    attempt_cooldown_s: float = DEFAULT_ATTEMPT_COOLDOWN_S,
    failure_cooldown_s: float = DEFAULT_FAILURE_COOLDOWN_S,
    require_driver: str | None = "brcmfmac",
) -> RepairResult:
    """Try the non-disruptive scan-suppression repair if rate limits allow.

    Callers should invoke this only after scan diagnostics already point
    at driver suppression. The helper is deliberately conservative: it
    does not infer suppression itself, and it skips non-brcmfmac radios
    by default so USB Wi-Fi adapters are left alone.
    """
    with _REPAIR_LOCK:
        return _maybe_repair_scan_suppression_locked(
            iface,
            state_path=state_path,
            now=now,
            attempt_cooldown_s=attempt_cooldown_s,
            failure_cooldown_s=failure_cooldown_s,
            require_driver=require_driver,
        )


def _maybe_repair_scan_suppression_locked(
    iface: str,
    *,
    state_path: Path | str,
    now: float | None,
    attempt_cooldown_s: float,
    failure_cooldown_s: float,
    require_driver: str | None,
) -> RepairResult:
    now = time.time() if now is None else now
    path = Path(state_path)
    state = _read_state(path)

    next_allowed = _float_or_none(state.get("nextAllowedAt"))
    if next_allowed is not None and now < next_allowed:
        remaining = max(0.0, next_allowed - now)
        result = RepairResult(
            iface=iface,
            attempted=False,
            reason="cooldown",
            cooldown_remaining=round(remaining, 3),
        )
        log_event(
            logger,
            "wifi_scan_repair.skip",
            iface=iface,
            reason="cooldown",
            remaining=round(remaining, 3),
        )
        return result

    driver = iface_driver_name(iface)
    if require_driver and driver != require_driver:
        reason = "driver_unknown" if driver is None else "driver_not_brcmfmac"
        result = RepairResult(
            iface=iface,
            attempted=False,
            reason=reason,
            driver=driver,
        )
        log_event(
            logger,
            "wifi_scan_repair.skip",
            iface=iface,
            reason=reason,
            driver=driver,
        )
        return result

    try:
        detail = send_crit_proto_stop(iface)
        ack = bool(detail.get("ack"))
        cooldown = attempt_cooldown_s if ack else failure_cooldown_s
        _write_state(path, {
            "lastAttemptAt": now,
            "lastAck": ack,
            "nextAllowedAt": now + cooldown,
            "lastReason": "attempted",
            "iface": iface,
        })
        result = RepairResult(
            iface=iface,
            attempted=True,
            reason="attempted",
            ack=ack,
            driver=driver,
            detail=detail,
        )
        log_event(
            logger,
            "wifi_scan_repair.attempt",
            iface=iface,
            driver=driver,
            ack=ack,
        )
        return result
    except Exception as e:  # noqa: BLE001
        _write_state(path, {
            "lastAttemptAt": now,
            "lastAck": False,
            "nextAllowedAt": now + failure_cooldown_s,
            "lastReason": "error",
            "iface": iface,
            "error": repr(e),
        })
        result = RepairResult(
            iface=iface,
            attempted=True,
            reason="error",
            ack=False,
            driver=driver,
            error=repr(e),
        )
        log_event(
            logger,
            "wifi_scan_repair.attempt_failed",
            level=logging.WARNING,
            iface=iface,
            driver=driver,
            err=repr(e),
        )
        return result


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log_event(
            logger,
            "wifi_scan_repair.state_write_failed",
            level=logging.WARNING,
            path=path,
            err=repr(e),
        )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strip_c_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def parse_enum_constant(header_text: str, enum_name: str, constant: str) -> int:
    clean = _strip_c_comments(header_text)
    m = re.search(
        rf"enum\s+{re.escape(enum_name)}\s*\{{(?P<body>.*?)\}};",
        clean,
        re.S,
    )
    if not m:
        raise KeyError(f"enum {enum_name!r} not found")
    value = -1
    values: dict[str, int] = {}
    for item in m.group("body").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, expr = [part.strip() for part in item.split("=", 1)]
            if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", expr):
                value = int(expr, 0)
            else:
                value = _eval_enum_expr(expr, values)
        else:
            name = item.split()[0]
            value += 1
        values[name] = value
        if name == constant:
            return value
    raise KeyError(f"{constant!r} not found in enum {enum_name!r}")


def _eval_enum_expr(expr: str, values: dict[str, int]) -> int:
    safe = expr
    for name, value in values.items():
        safe = re.sub(rf"\b{re.escape(name)}\b", str(value), safe)
    if not re.fullmatch(r"[0-9xXa-fA-F\s+\-()]+", safe):
        raise ValueError(f"unsupported enum expression: {expr!r}")
    return int(eval(safe, {"__builtins__": {}}, {}))  # noqa: S307


def load_nl80211_constants(path: Path = NL80211_HEADER) -> dict[str, int]:
    header = path.read_text(encoding="utf-8")
    return {
        "NL80211_CMD_CRIT_PROTOCOL_STOP": parse_enum_constant(
            header,
            "nl80211_commands",
            "NL80211_CMD_CRIT_PROTOCOL_STOP",
        ),
        "NL80211_ATTR_IFINDEX": parse_enum_constant(
            header,
            "nl80211_attrs",
            "NL80211_ATTR_IFINDEX",
        ),
        "NL80211_ATTR_WDEV": parse_enum_constant(
            header,
            "nl80211_attrs",
            "NL80211_ATTR_WDEV",
        ),
    }


def _align4(n: int) -> int:
    return (n + 3) & ~3


def nlattr(attr_type: int, payload: bytes) -> bytes:
    raw = struct.pack("HH", 4 + len(payload), attr_type) + payload
    return raw + (b"\0" * (_align4(len(raw)) - len(raw)))


def parse_attrs(data: bytes) -> dict[int, list[bytes]]:
    attrs: dict[int, list[bytes]] = {}
    pos = 0
    while pos + 4 <= len(data):
        length, attr_type = struct.unpack_from("HH", data, pos)
        if length < 4 or pos + length > len(data):
            break
        attrs.setdefault(attr_type, []).append(data[pos + 4:pos + length])
        pos += _align4(length)
    return attrs


def netlink_message(
    nlmsg_type: int,
    flags: int,
    seq: int,
    genl_cmd: int,
    version: int,
    attrs: bytes,
) -> bytes:
    genl = struct.pack("BBH", genl_cmd, version, 0)
    length = 16 + len(genl) + len(attrs)
    return struct.pack("IHHII", length, nlmsg_type, flags, seq, 0) + genl + attrs


def iter_netlink_messages(data: bytes):
    pos = 0
    while pos + 16 <= len(data):
        length, nlmsg_type, flags, seq, pid = struct.unpack_from(
            "IHHII",
            data,
            pos,
        )
        if length < 16 or pos + length > len(data):
            break
        yield nlmsg_type, flags, seq, pid, data[pos + 16:pos + length]
        pos += _align4(length)


def get_genl_family(sock: socket.socket, family_name: str) -> tuple[int, int]:
    seq = 1
    attrs = nlattr(CTRL_ATTR_FAMILY_NAME, family_name.encode() + b"\0")
    msg = netlink_message(
        GENL_ID_CTRL,
        NLM_F_REQUEST,
        seq,
        CTRL_CMD_GETFAMILY,
        1,
        attrs,
    )
    sock.send(msg)
    data = sock.recv(65535)
    for nlmsg_type, _flags, _seq, _pid, payload in iter_netlink_messages(data):
        if nlmsg_type == NLMSG_ERROR:
            _raise_netlink_error(payload, "get generic-netlink family")
        if len(payload) < 4:
            continue
        _cmd, _version, _reserved = struct.unpack_from("BBH", payload, 0)
        attrs = parse_attrs(payload[4:])
        family_id = struct.unpack_from("H", attrs[CTRL_ATTR_FAMILY_ID][0], 0)[0]
        family_version = attrs.get(CTRL_ATTR_VERSION, [b"\0"])[0][0]
        return family_id, family_version
    raise RuntimeError(f"no generic-netlink response for {family_name!r}")


def _raise_netlink_error(payload: bytes, context: str) -> None:
    if len(payload) < 4:
        raise RuntimeError(f"netlink error without errno while trying to {context}")
    error = struct.unpack_from("i", payload, 0)[0]
    if error == 0:
        return
    err_no = -error
    raise OSError(err_no, f"{context}: {os.strerror(err_no)}")


def wdev_for_iface(iface: str) -> int | None:
    try:
        proc = subprocess.run(
            ["iw", "dev", iface, "info"],
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"^\s*wdev\s+(0x[0-9a-fA-F]+|\d+)\s*$", proc.stdout, re.M)
    return int(m.group(1), 0) if m else None


def send_crit_proto_stop(iface: str, *, dry_run: bool = False) -> dict[str, Any]:
    constants = load_nl80211_constants()
    ifindex = socket.if_nametoindex(iface)
    wdev = wdev_for_iface(iface)
    attrs = nlattr(constants["NL80211_ATTR_IFINDEX"], struct.pack("I", ifindex))
    if wdev is not None:
        attrs += nlattr(constants["NL80211_ATTR_WDEV"], struct.pack("Q", wdev))
    result = {
        "iface": iface,
        "ifindex": ifindex,
        "wdev": wdev,
        "command": "NL80211_CMD_CRIT_PROTOCOL_STOP",
        "dryRun": dry_run,
    }
    if dry_run:
        return result

    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC) as sock:
        sock.settimeout(NETLINK_TIMEOUT_S)
        sock.bind((0, 0))
        family_id, family_version = get_genl_family(sock, "nl80211")
        msg = netlink_message(
            family_id,
            NLM_F_REQUEST | NLM_F_ACK,
            2,
            constants["NL80211_CMD_CRIT_PROTOCOL_STOP"],
            family_version,
            attrs,
        )
        sock.send(msg)
        while True:
            data = sock.recv(65535)
            for nlmsg_type, _flags, _seq, _pid, payload in iter_netlink_messages(data):
                if nlmsg_type == NLMSG_ERROR:
                    _raise_netlink_error(
                        payload,
                        "send NL80211_CMD_CRIT_PROTOCOL_STOP",
                    )
                    result["ack"] = True
                    return result
