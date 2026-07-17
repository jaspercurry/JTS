# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded whole-system soak sampler.

This command is intentionally an operator diagnostic, not a daemon. Run it
through ``scripts/pi-run-diagnostic.sh`` for Pi-side use so systemd bounds the
diagnostic process separately from product daemons.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jasper.control.system_metrics import (
    EXTRA_SERVICE_GROUPS,
    JASPER_SERVICE_GROUPS,
    SystemSampler,
)

DEFAULT_OUTPUT_DIR = "/var/lib/jasper/diagnostics/system-soak"
DEFAULT_DURATION_SEC = 10 * 60
DEFAULT_INTERVAL_SEC = 30
MAX_DURATION_SEC = 2 * 60 * 60
SCHEMA_VERSION = 1

STATUS_SOCKETS = {
    "outputd": "/run/jasper-outputd/control.sock",
    "fanin": "/run/jasper-fanin/control.sock",
    "mux": "/run/jasper-mux/control.sock",
    "voice": "/run/jasper/voice.sock",
}

# Resident daemons that matter to whole-system soak evidence but are not part
# of the management dashboard's curated service inventory. The adjacent USB
# gadget, USB input, and volume-bridge units already come from
# JASPER_SERVICE_GROUPS; the transient jasper-usbmic-apply oneshot is not a
# resource-soak target.
SOAK_EXTRA_UNITS = {
    "jasper-usbmic.service",
    "jasper-usbnet-dhcp.service",
}


def _utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _artifact_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_duration(raw: str) -> int:
    text = str(raw).strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration cannot be empty")
    unit = text[-1]
    number = text[:-1] if unit in {"s", "m", "h"} else text
    try:
        value = float(number)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid duration: {raw!r}") from e
    scale = {"s": 1, "m": 60, "h": 3600}.get(unit, 1)
    seconds = int(value * scale)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def _tracked_units() -> list[str]:
    return sorted(
        set(JASPER_SERVICE_GROUPS)
        | set(EXTRA_SERVICE_GROUPS)
        | SOAK_EXTRA_UNITS
    )


def _control_group_dir(control_group: str) -> Path | None:
    if not control_group:
        return None
    rel = control_group.lstrip("/")
    if not rel:
        return None
    return Path("/sys/fs/cgroup") / rel


def _read_key_value_file(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def _read_pressure_file(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        kind = parts[0]
        vals: dict[str, float] = {}
        for item in parts[1:]:
            key, sep, value = item.partition("=")
            if not sep:
                continue
            try:
                vals[key] = float(value)
            except ValueError:
                continue
        if vals:
            out[kind] = vals
    return out


def _read_cgroup_extra(control_group: str) -> dict[str, Any]:
    base = _control_group_dir(control_group)
    if base is None:
        return {}
    out: dict[str, Any] = {
        "cpu_stat": _read_key_value_file(base / "cpu.stat"),
        "memory_events": _read_key_value_file(base / "memory.events"),
    }
    pressure = {
        name: parsed for name in ("cpu", "memory", "io")
        if (parsed := _read_pressure_file(base / f"{name}.pressure"))
    }
    if pressure:
        out["pressure"] = pressure
    try:
        io_text = (base / "io.stat").read_text().splitlines()
    except OSError:
        io_text = []
    if io_text:
        out["io_stat"] = io_text
    return {key: value for key, value in out.items() if value}


def _pids_for_cgroup(control_group: str) -> list[int]:
    base = _control_group_dir(control_group)
    if base is None:
        return []
    try:
        text = (base / "cgroup.procs").read_text()
    except OSError:
        return []
    out: list[int] = []
    for raw in text.split():
        try:
            out.append(int(raw))
        except ValueError:
            continue
    return out


def _read_smaps_rollup(pid: int) -> dict[str, int] | None:
    path = Path("/proc") / str(pid) / "smaps_rollup"
    try:
        text = path.read_text()
    except OSError:
        return None
    keys = {
        "Rss": "rss_kb",
        "Pss": "pss_kb",
        "Private_Clean": "private_clean_kb",
        "Private_Dirty": "private_dirty_kb",
        "Shared_Clean": "shared_clean_kb",
        "Shared_Dirty": "shared_dirty_kb",
    }
    out: dict[str, int] = {}
    for raw in text.splitlines():
        name, sep, rest = raw.partition(":")
        if not sep or name not in keys:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            out[keys[name]] = int(parts[0])
        except ValueError:
            continue
    return out or None


def _pss_rollup(control_group: str) -> dict[str, Any] | None:
    pids = _pids_for_cgroup(control_group)
    if not pids:
        return None
    totals = {
        "rss_kb": 0,
        "pss_kb": 0,
        "private_clean_kb": 0,
        "private_dirty_kb": 0,
        "shared_clean_kb": 0,
        "shared_dirty_kb": 0,
    }
    readable = 0
    for pid in pids:
        data = _read_smaps_rollup(pid)
        if data is None:
            continue
        readable += 1
        for key in totals:
            totals[key] += data.get(key, 0)
    if readable == 0:
        return {"pid_count": len(pids), "readable_pids": 0}
    return {"pid_count": len(pids), "readable_pids": readable, **totals}


def _status_socket(path: str, timeout: float = 1.0, max_bytes: int = 65536) -> dict[str, Any] | None:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while sum(len(c) for c in chunks) < max_bytes:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _sample_units(
    *,
    units: list[str],
    include_pss: bool,
    previous_cpu: dict[str, tuple[int, float]],
    now_mono: float,
) -> list[dict[str, Any]]:
    states = SystemSampler._read_service_states(units)
    rows: list[dict[str, Any]] = []
    for unit in units:
        state = states.get(unit, {"unit": unit})
        row: dict[str, Any] = {
            "unit": unit,
            "group": (
                JASPER_SERVICE_GROUPS.get(unit)
                or EXTRA_SERVICE_GROUPS.get(unit)
                or "Service"
            ),
            "load_state": state.get("load_state"),
            "active_state": state.get("active_state"),
            "sub_state": state.get("sub_state"),
            "result": state.get("result"),
            "n_restarts": state.get("n_restarts"),
            "main_pid": state.get("main_pid"),
            "tasks_current": state.get("tasks_current"),
            "memory_current_bytes": state.get("memory_current_bytes"),
            "control_group": state.get("control_group") or "",
        }
        cpu_nsec = state.get("cpu_usage_nsec")
        row["cpu_usage_nsec"] = cpu_nsec
        if isinstance(cpu_nsec, int):
            prev = previous_cpu.get(unit)
            if prev is not None:
                prev_nsec, prev_mono = prev
                wall = now_mono - prev_mono
                if wall > 0 and cpu_nsec >= prev_nsec:
                    row["cpu_pct"] = round(
                        (cpu_nsec - prev_nsec) / (wall * 1e9) * 100.0,
                        2,
                    )
            previous_cpu[unit] = (cpu_nsec, now_mono)
        cgroup = row["control_group"]
        if cgroup:
            extra = _read_cgroup_extra(cgroup)
            if extra:
                row["cgroup"] = extra
            if include_pss:
                pss = _pss_rollup(cgroup)
                if pss is not None:
                    row["pss"] = pss
        rows.append(row)
    return rows


def _sample_status_sockets() -> dict[str, Any]:
    return {
        name: {"path": path, "status": _status_socket(path)}
        for name, path in STATUS_SOCKETS.items()
    }


def _summarize_journal(since: str, until: str, units: list[str]) -> dict[str, Any]:
    cmd = [
        "journalctl", "--since", since, "--until", until, "-o", "json",
        "--output-fields=__REALTIME_TIMESTAMP,_SYSTEMD_UNIT,PRIORITY,MESSAGE",
        "--no-pager",
    ]
    for unit in units:
        cmd.extend(["-u", unit])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return {"available": False, "error": str(e)}
    if proc.returncode not in (0, 1):
        return {
            "available": False,
            "returncode": proc.returncode,
            "error": proc.stderr[:300],
        }
    by_unit: dict[str, dict[str, Any]] = {}
    total = 0
    for raw in proc.stdout.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        unit = entry.get("_SYSTEMD_UNIT") or "unknown"
        priority = str(entry.get("PRIORITY") or "unknown")
        message = entry.get("MESSAGE", "")
        if isinstance(message, str):
            message_bytes = len(message.encode("utf-8", errors="replace"))
        else:
            message_bytes = len(
                json.dumps(message, sort_keys=True).encode("utf-8"),
            )
        bucket = by_unit.setdefault(
            unit,
            {"entries": 0, "message_bytes": 0, "priorities": {}},
        )
        bucket["entries"] += 1
        bucket["message_bytes"] += message_bytes
        bucket["priorities"][priority] = bucket["priorities"].get(priority, 0) + 1
        total += 1
    return {"available": True, "entries": total, "by_unit": by_unit}


def run_soak(
    *,
    duration_sec: int,
    interval_sec: int,
    include_pss: bool,
    include_journal: bool,
    output_dir: Path,
    profile: str,
) -> Path:
    if duration_sec > MAX_DURATION_SEC:
        raise ValueError(f"duration exceeds {MAX_DURATION_SEC}s safety cap")
    output_dir.mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    started_mono = time.monotonic()
    started_iso = _utc_iso(started_wall)
    units = _tracked_units()
    previous_cpu: dict[str, tuple[int, float]] = {}
    samples: list[dict[str, Any]] = []

    next_sample = started_mono
    end_at = started_mono + duration_sec
    while True:
        now_mono = time.monotonic()
        now_wall = time.time()
        samples.append({
            "ts": _utc_iso(now_wall),
            "elapsed_sec": round(now_mono - started_mono, 3),
            "units": _sample_units(
                units=units,
                include_pss=include_pss,
                previous_cpu=previous_cpu,
                now_mono=now_mono,
            ),
            "status": _sample_status_sockets(),
        })
        if now_mono >= end_at:
            break
        next_sample += interval_sec
        time.sleep(max(0.0, min(next_sample, end_at) - time.monotonic()))

    ended_wall = time.time()
    ended_iso = _utc_iso(ended_wall)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "started_at": started_iso,
        "ended_at": ended_iso,
        "duration_sec": round(ended_wall - started_wall, 3),
        "interval_sec": interval_sec,
        "include_pss": include_pss,
        "include_journal": include_journal,
        "units": units,
        "samples": samples,
    }
    if include_journal:
        artifact["journal"] = _summarize_journal(started_iso, ended_iso, units)

    path = output_dir / f"{_artifact_stamp()}__{profile}__system_soak.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a bounded JTS system resource soak artifact.",
    )
    parser.add_argument(
        "--duration", type=parse_duration, default=DEFAULT_DURATION_SEC,
        help="soak duration, e.g. 10m, 30m, 1h (max 2h)",
    )
    parser.add_argument(
        "--interval", type=parse_duration, default=DEFAULT_INTERVAL_SEC,
        help="sample interval, e.g. 30s or 1m",
    )
    parser.add_argument(
        "--profile", default="idle",
        choices=("idle", "realistic", "lab"),
        help="operator label recorded in the artifact",
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help=f"artifact directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--include-pss", action="store_true",
        help="read /proc/<pid>/smaps_rollup for better memory attribution",
    )
    parser.add_argument(
        "--no-journal", action="store_true",
        help="skip journal count/byte summary",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval < 5:
        parser.error("--interval must be at least 5s")
    if args.interval > args.duration:
        parser.error("--interval must be <= --duration")
    try:
        path = run_soak(
            duration_sec=args.duration,
            interval_sec=args.interval,
            include_pss=args.include_pss,
            include_journal=not args.no_journal,
            output_dir=Path(args.output_dir),
            profile=args.profile,
        )
    except Exception as e:  # noqa: BLE001
        print(f"jasper-system-soak failed: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
