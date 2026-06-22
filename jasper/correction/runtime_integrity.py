# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime-integrity evidence for correction measurement bundles.

The acoustic pipeline can produce a curve from a WAV even when the Pi
was under load, memory was tight, or the browser uploaded a truncated
capture. Capture quality checks inspect the audio signal itself; this
module records the surrounding system evidence so a bundle can explain
whether the measurement environment looked trustworthy.
"""
from __future__ import annotations

import os
import json
import socket
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1

Severity = Literal["warn", "fail"]

LOAD_PER_CORE_WARN = 1.50
MEM_AVAILABLE_WARN_MB = 96
MEM_AVAILABLE_FAIL_MB = 32
CAPTURE_EXTRA_SECONDS_WARN = 30.0
CAPTURE_EXTRA_RATIO_WARN = 3.0
FANIN_CONTROL_SOCKET = "/run/jasper-fanin/control.sock"
FANIN_STATUS_TIMEOUT_SEC = 0.15


@dataclass(frozen=True)
class RuntimeIssue:
    code: str
    severity: Severity
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.details:
            out["details"] = self.details
        return out


def _read_loadavg_1m() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError, ValueError):
        return None


def _read_meminfo_mb() -> dict[str, int] | None:
    path = Path("/proc/meminfo")
    if not path.exists():
        return None
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
    except OSError:
        return None
    total_kb = values.get("MemTotal")
    available_kb = values.get("MemAvailable")
    if total_kb is None or available_kb is None:
        return None
    return {
        "total_mb": total_kb // 1024,
        "available_mb": available_kb // 1024,
        "used_mb": (total_kb - available_kb) // 1024,
    }


def _read_fanin_status(
    socket_path: str = FANIN_CONTROL_SOCKET,
    timeout_sec: float = FANIN_STATUS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _fanin_summary(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    output = status.get("output") if isinstance(status.get("output"), dict) else {}
    inputs = status.get("inputs") if isinstance(status.get("inputs"), list) else []
    input_summaries = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        input_summaries.append({
            "label": item.get("label"),
            "frames_read": item.get("frames_read"),
            "xrun_count": item.get("xrun_count"),
        })
    return {
        "selected_input": status.get("selected_input"),
        "selection_mode": status.get("selection_mode"),
        "input_buffer_frames": status.get("input_buffer_frames"),
        "output": {
            "frames_written": output.get("frames_written"),
            "xrun_count": output.get("xrun_count"),
        },
        "inputs": input_summaries,
    }


def _round_float(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _system_snapshot(
    label: str,
    *,
    capture_kind: str | None,
    position_index: int | None,
    camilla_status: dict[str, Any] | None,
) -> dict[str, Any]:
    cpu_count = os.cpu_count() or 1
    load_1m = _read_loadavg_1m()
    mem = _read_meminfo_mb() or {}
    fanin = _fanin_summary(_read_fanin_status())
    snapshot: dict[str, Any] = {
        "label": label,
        "timestamp": time.time(),
        "monotonic_s": time.monotonic(),
        "capture_kind": capture_kind,
        "position_index": position_index,
        "cpu_count": cpu_count,
        "load_1m": _round_float(load_1m, 2),
        "load_per_core": (
            _round_float(load_1m / max(1, cpu_count), 3)
            if load_1m is not None else None
        ),
    }
    if mem:
        snapshot["memory"] = mem
    if fanin:
        snapshot["fanin"] = fanin
    if isinstance(camilla_status, dict):
        snapshot["camilla"] = camilla_status
    return snapshot


def _issues_for_snapshot(snapshot: dict[str, Any]) -> list[RuntimeIssue]:
    issues: list[RuntimeIssue] = []
    load_per_core = snapshot.get("load_per_core")
    if (
        isinstance(load_per_core, (int, float))
        and load_per_core >= LOAD_PER_CORE_WARN
    ):
        issues.append(RuntimeIssue(
            code="system_load_high",
            severity="warn",
            message=(
                "system load was high during measurement; remeasure if the "
                "curve looks surprising"
            ),
            details={
                "label": snapshot.get("label"),
                "load_per_core": round(float(load_per_core), 3),
                "threshold": LOAD_PER_CORE_WARN,
            },
        ))
    memory = snapshot.get("memory")
    if isinstance(memory, dict):
        available = memory.get("available_mb")
        if isinstance(available, int):
            if available < MEM_AVAILABLE_FAIL_MB:
                issues.append(RuntimeIssue(
                    code="memory_available_critical",
                    severity="fail",
                    message="available memory was critically low during measurement",
                    details={
                        "label": snapshot.get("label"),
                        "available_mb": available,
                        "threshold_mb": MEM_AVAILABLE_FAIL_MB,
                    },
                ))
            elif available < MEM_AVAILABLE_WARN_MB:
                issues.append(RuntimeIssue(
                    code="memory_available_low",
                    severity="warn",
                    message="available memory was low during measurement",
                    details={
                        "label": snapshot.get("label"),
                        "available_mb": available,
                        "threshold_mb": MEM_AVAILABLE_WARN_MB,
                    },
                ))
    return issues


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fanin_xrun_total(snapshot: dict[str, Any]) -> int | None:
    fanin = snapshot.get("fanin")
    if not isinstance(fanin, dict):
        return None
    total = 0
    seen = False
    output = fanin.get("output")
    if isinstance(output, dict):
        value = _as_int(output.get("xrun_count"))
        if value is not None:
            total += value
            seen = True
    inputs = fanin.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            value = _as_int(item.get("xrun_count"))
            if value is not None:
                total += value
                seen = True
    return total if seen else None


def _camilla_clipped_samples(snapshot: dict[str, Any]) -> int | None:
    camilla = snapshot.get("camilla")
    if not isinstance(camilla, dict):
        return None
    return _as_int(camilla.get("clipped_samples"))


def _delta_issues(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[RuntimeIssue]:
    if previous is None:
        return []
    issues: list[RuntimeIssue] = []
    prev_xruns = _fanin_xrun_total(previous)
    cur_xruns = _fanin_xrun_total(current)
    if prev_xruns is not None and cur_xruns is not None and cur_xruns > prev_xruns:
        issues.append(RuntimeIssue(
            code="fanin_xruns_increased",
            severity="warn",
            message="fan-in xrun count increased during measurement",
            details={
                "previous": prev_xruns,
                "current": cur_xruns,
                "delta": cur_xruns - prev_xruns,
                "label": current.get("label"),
            },
        ))
    prev_clipped = _camilla_clipped_samples(previous)
    cur_clipped = _camilla_clipped_samples(current)
    if (
        prev_clipped is not None
        and cur_clipped is not None
        and cur_clipped > prev_clipped
    ):
        issues.append(RuntimeIssue(
            code="camilla_clipping_increased",
            severity="warn",
            message="CamillaDSP clipped samples increased during measurement",
            details={
                "previous": prev_clipped,
                "current": cur_clipped,
                "delta": cur_clipped - prev_clipped,
                "label": current.get("label"),
            },
        ))
    return issues


def _read_wav_shape(path: Path) -> dict[str, int] | None:
    try:
        with wave.open(str(path), "rb") as wav:
            return {
                "sample_rate": int(wav.getframerate()),
                "channels": int(wav.getnchannels()),
                "sample_width_bytes": int(wav.getsampwidth()),
                "frames": int(wav.getnframes()),
            }
    except (wave.Error, OSError, EOFError):
        return None


class RuntimeIntegrityReport:
    """Mutable per-session collector.

    Kept intentionally small and stdlib-only: the correction service
    already imports NumPy/SciPy for DSP, but runtime evidence should not
    add another always-loaded dependency on the 1 GB Pi.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.snapshots: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self._issue_keys: set[tuple[Any, ...]] = set()

    def _add_issue(
        self,
        issue: RuntimeIssue,
        *,
        capture_kind: str | None,
        position_index: int | None,
    ) -> bool:
        payload = issue.to_dict()
        payload["capture_kind"] = capture_kind
        payload["position_index"] = position_index
        key = (
            payload.get("code"),
            payload.get("severity"),
            capture_kind,
            position_index,
            str(payload.get("details") or {}),
        )
        if key in self._issue_keys:
            return False
        self._issue_keys.add(key)
        self.issues.append(payload)
        self.updated_at = time.time()
        return True

    def record_snapshot(
        self,
        label: str,
        *,
        capture_kind: str | None = None,
        position_index: int | None = None,
        camilla_status: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        previous = self.snapshots[-1] if self.snapshots else None
        snapshot = _system_snapshot(
            label,
            capture_kind=capture_kind,
            position_index=position_index,
            camilla_status=camilla_status,
        )
        self.snapshots.append(snapshot)
        self.updated_at = snapshot["timestamp"]
        added: list[dict[str, Any]] = []
        for issue in _issues_for_snapshot(snapshot) + _delta_issues(
            previous,
            snapshot,
        ):
            if self._add_issue(
                issue,
                capture_kind=capture_kind,
                position_index=position_index,
            ):
                added.append(self.issues[-1])
        return added

    def record_capture(
        self,
        path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
        artifact_path: str | None = None,
        expected_sample_rate: int,
        expected_sweep_samples: int,
        expected_sweep_duration_s: float,
    ) -> list[dict[str, Any]]:
        path = Path(path)
        stat_size = path.stat().st_size if path.exists() else None
        shape = _read_wav_shape(path)
        capture: dict[str, Any] = {
            "capture_kind": capture_kind,
            "position_index": position_index,
            "artifact_path": artifact_path or path.name,
            "byte_size": stat_size,
            "expected_sample_rate": int(expected_sample_rate),
            "expected_sweep_samples": int(expected_sweep_samples),
            "expected_sweep_duration_s": round(float(expected_sweep_duration_s), 3),
        }
        issues: list[RuntimeIssue] = []
        if shape is None:
            issues.append(RuntimeIssue(
                code="capture_wav_unreadable",
                severity="fail",
                message="uploaded capture could not be read as a WAV file",
            ))
        else:
            frames = shape["frames"]
            sample_rate = shape["sample_rate"]
            duration_s = frames / sample_rate if sample_rate > 0 else 0.0
            capture.update({
                **shape,
                "duration_s": round(float(duration_s), 3),
                "sample_delta_vs_sweep": int(frames - expected_sweep_samples),
            })
            if sample_rate != expected_sample_rate:
                issues.append(RuntimeIssue(
                    code="runtime_sample_rate_mismatch",
                    severity="fail",
                    message="uploaded capture sample rate differs from the sweep rate",
                    details={
                        "sample_rate": sample_rate,
                        "expected_sample_rate": expected_sample_rate,
                    },
                ))
            if shape["channels"] != 1:
                issues.append(RuntimeIssue(
                    code="runtime_capture_not_mono",
                    severity="warn",
                    message="uploaded capture is not mono; analysis will downmix it",
                    details={"channels": shape["channels"]},
                ))
            if frames < expected_sweep_samples:
                issues.append(RuntimeIssue(
                    code="runtime_capture_too_short",
                    severity="fail",
                    message="uploaded capture is shorter than the played sweep",
                    details={
                        "captured_samples": frames,
                        "expected_sweep_samples": expected_sweep_samples,
                    },
                ))
            extra_s = duration_s - expected_sweep_duration_s
            if (
                extra_s > CAPTURE_EXTRA_SECONDS_WARN
                or duration_s > expected_sweep_duration_s * CAPTURE_EXTRA_RATIO_WARN
            ):
                issues.append(RuntimeIssue(
                    code="runtime_capture_much_longer_than_sweep",
                    severity="warn",
                    message=(
                        "uploaded capture is much longer than the sweep; "
                        "extra room noise may affect analysis"
                    ),
                    details={
                        "duration_s": round(float(duration_s), 3),
                        "expected_sweep_duration_s": round(
                            float(expected_sweep_duration_s), 3,
                        ),
                    },
                ))

        capture["issues"] = [issue.to_dict() for issue in issues]
        self.captures.append(capture)
        self.updated_at = time.time()
        added: list[dict[str, Any]] = []
        for issue in issues:
            if self._add_issue(
                issue,
                capture_kind=capture_kind,
                position_index=position_index,
            ):
                added.append(self.issues[-1])
        return added

    def summary(self) -> dict[str, Any]:
        severity = "ok"
        if any(issue.get("severity") == "fail" for issue in self.issues):
            severity = "fail"
        elif any(issue.get("severity") == "warn" for issue in self.issues):
            severity = "warn"
        latest = self.snapshots[-1] if self.snapshots else None
        return {
            "artifact_path": "runtime_integrity.json",
            "schema_version": SCHEMA_VERSION,
            "level": severity,
            "issue_count": len(self.issues),
            "warning_count": sum(
                1 for issue in self.issues if issue.get("severity") == "warn"
            ),
            "failure_count": sum(
                1 for issue in self.issues if issue.get("severity") == "fail"
            ),
            "capture_count": len(self.captures),
            "snapshot_count": len(self.snapshots),
            "latest_snapshot": latest,
            # Copy, not the live list: summary() feeds MeasurementSession.snapshot(),
            # serialized on the /status handler thread while _add_issue may append
            # from the loop thread. Point-in-time, like the session's own
            # capture_quality / noise_reports copies.
            "issues": list(self.issues),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "summary": self.summary(),
            "captures": self.captures,
            "snapshots": self.snapshots,
            "issues": self.issues,
            "thresholds": {
                "load_per_core_warn": LOAD_PER_CORE_WARN,
                "mem_available_warn_mb": MEM_AVAILABLE_WARN_MB,
                "mem_available_fail_mb": MEM_AVAILABLE_FAIL_MB,
                "capture_extra_seconds_warn": CAPTURE_EXTRA_SECONDS_WARN,
                "capture_extra_ratio_warn": CAPTURE_EXTRA_RATIO_WARN,
            },
        }
