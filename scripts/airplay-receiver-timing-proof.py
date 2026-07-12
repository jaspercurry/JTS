#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Receiver-side timing proof for the AirPlay/output pipeline.

This is intentionally receiver-side only: it does not try to observe the
sender's display scanout. It measures the local audio pipeline by capturing:

  * ``plug:jasper_capture``: the legacy loopback lane-7 summed-program tap.
  * outputd's final electrical speaker-reference UDP stream on loopback.

The script never binds the UDP port, so it can run while the AEC bridge owns
127.0.0.1:9891. It sniffs loopback with a temporary root AF_PACKET socket,
captures the ALSA diagnostic tap with arecord, copies both raw files back, and
correlates the waveforms locally. It refuses ``shm_ring`` because lane 7 is a
lossy diagnostic mirror in that topology, not the CamillaDSP transport.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import textwrap
import time
import wave
from pathlib import Path
from typing import Any


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2
DEFAULT_REF_PORT = 9891
DEFAULT_PRE_DEVICE = "plug:jasper_capture"
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
FANIN_SOCKET = "/run/jasper-fanin/control.sock"
REMOTE_DIR_PREFIX = "/tmp/jasper-airplay-receiver-proof-"
REMOTE_DIR_RE = re.compile(
    r"^/tmp/jasper-airplay-receiver-proof-"
    r"(?P<stamp>\d{8}T\d{6}Z(?:-run[1-9]\d*)?)-"
    r"(?P<token>[A-Za-z0-9_-]{20,64})$"
)
MAX_DURATION_SEC = 60.0
MAX_RUNS = 10
MAX_PAUSE_SEC = 300.0
MIN_LATENCY_MS = -1000.0
MAX_LATENCY_MS = 5000.0
MAX_REMOTE_DIR_BYTES = 32 * 1024 * 1024
MAX_TAR_MEMBERS = 16
CONTROL_STATE_URL = "http://127.0.0.1:8780/state"
REMOTE_COMMAND_GRACE_SEC = 20.0
TRANSFER_TIMEOUT_SEC = 60.0
CLEANUP_TIMEOUT_SEC = 15.0
MIN_CAPTURE_COVERAGE_RATIO = 0.95


REMOTE_CAPTURE = r"""
from __future__ import annotations

import json
import os
import select
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
FANIN_SOCKET = "/run/jasper-fanin/control.sock"
CONTROL_STATE_URL = "http://127.0.0.1:8780/state"
MODEL_ENV_KEYS = frozenset({
    "JASPER_CAMILLA_CHUNKSIZE",
    "JASPER_CAMILLA_TARGET_LEVEL",
    "JASPER_FANIN_OUTPUT_BUFFER_FRAMES",
    "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
    "JASPER_OUTPUTD_PERIOD_FRAMES",
})
PACKET_OUTGOING = getattr(socket, "PACKET_OUTGOING", 4)


def uds_status(path: str) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(path)
        sock.sendall(b"STATUS\n")
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()


def parse_env_file(path: str, allowed_keys: frozenset[str]) -> dict[str, str]:
    # Read only explicitly public numeric model inputs. The base env has
    # historically carried credentials; never serialize it unfiltered into
    # an artifact that leaves the Pi.
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed_keys:
            continue
        value = value.strip().strip("'").strip('"')
        out[key] = value
    return out


def active_source() -> str:
    try:
        with urllib.request.urlopen(CONTROL_STATE_URL, timeout=2.0) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"could not read jasper-control active source: {type(exc).__name__}: {exc}"
        ) from exc
    source = payload.get("active_source") if isinstance(payload, dict) else None
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError("jasper-control /state did not report active_source")
    return source.strip().lower()


def outputd_capture_errors(status: dict, ref_port: int, phase: str) -> list[str]:
    if not isinstance(status, dict):
        return [f"outputd {phase} STATUS is not a JSON object"]
    errors = []
    content = status.get("content")
    content_source = content.get("source") if isinstance(content, dict) else None
    if content_source == "shm_ring":
        errors.append(
            f"outputd {phase} uses shm_ring; plug:jasper_capture is only a lossy "
            "lane-7 diagnostic mirror in that topology, so this proof has no "
            "measured pre-output transport model"
        )
    elif content_source != "alsa":
        errors.append(
            f"outputd {phase} content source is {content_source!r}, expected 'alsa'"
        )

    expected_target = f"127.0.0.1:{ref_port}"
    if status.get("udp_target") != expected_target:
        errors.append(
            f"outputd {phase} udp_target is {status.get('udp_target')!r}, "
            f"expected {expected_target!r}"
        )
    if status.get("udp_active") is not True:
        errors.append(
            f"outputd {phase} udp_active is {status.get('udp_active')!r}, expected true"
        )
    return errors


def shairport_offset(path: str = "/etc/shairport-sync.conf") -> float | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r"audio_backend_latency_offset_in_seconds\s*=\s*([-+]?\d+(?:\.\d+)?)",
        text,
    )
    return float(match.group(1)) if match else None


def udp_payload_if_reference(packet: bytes, port: int, packet_type: int) -> bytes | None:
    # Linux presents loopback traffic to AF_PACKET as both PACKET_OUTGOING and
    # PACKET_HOST. Accept the sender-side copy only or every outputd period is
    # duplicated in the captured waveform.
    if packet_type != PACKET_OUTGOING:
        return None
    if len(packet) < 42:
        return None
    eth_type = struct.unpack("!H", packet[12:14])[0]
    if eth_type != 0x0800:
        return None
    ipoff = 14
    ihl = (packet[ipoff] & 0x0F) * 4
    if ihl < 20 or len(packet) < ipoff + ihl + 8:
        return None
    if packet[ipoff + 9] != 17:
        return None
    fragment_bits = struct.unpack("!H", packet[ipoff + 6 : ipoff + 8])[0]
    if fragment_bits & 0x3FFF:
        return None
    if packet[ipoff + 16 : ipoff + 20] != socket.inet_aton("127.0.0.1"):
        return None
    udpoff = ipoff + ihl
    src_port, dst_port, udp_len, _checksum = struct.unpack(
        "!HHHH", packet[udpoff : udpoff + 8]
    )
    if dst_port != port:
        return None
    payload_end = udpoff + udp_len
    if payload_end > len(packet):
        return None
    return packet[udpoff + 8 : payload_end]


def capture_pre_tap(
    args: dict,
    workdir: Path,
    ready: threading.Event,
    start: threading.Event,
    meta: dict,
) -> None:
    out_path = workdir / "pre_jasper_capture.s16le"
    cmd = [
        "arecord",
        "-q",
        "-D",
        args["pre_device"],
        "-f",
        "S16_LE",
        "-c",
        str(CHANNELS),
        "-r",
        str(SAMPLE_RATE),
        "-t",
        "raw",
        "-",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    meta["pre_command"] = cmd
    meta["pre_pid"] = proc.pid
    meta["pre_bytes"] = 0
    ready.set()
    start.wait()
    deadline = time.monotonic() + float(args["duration"])
    try:
        with out_path.open("wb") as fh:
            fd = proc.stdout.fileno()
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                meta["pre_bytes"] += len(chunk)
                fh.write(chunk)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            _stdout, stderr = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _stdout, stderr = proc.communicate(timeout=1.0)
        meta["pre_returncode"] = proc.returncode
        meta["pre_stderr"] = stderr.decode("utf-8", errors="replace")[-2000:]


def capture_reference_udp(
    args: dict,
    workdir: Path,
    ready: threading.Event,
    start: threading.Event,
    meta: dict,
) -> None:
    out_path = workdir / "outputd_reference_udp.s16le"
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
    sock.bind(("lo", 0))
    sock.settimeout(0.1)
    meta["ref_packets"] = 0
    meta["ref_bytes"] = 0
    ready.set()
    start.wait()
    deadline = time.monotonic() + float(args["duration"])
    with out_path.open("wb") as fh:
        while time.monotonic() < deadline:
            try:
                packet, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            packet_type = addr[2] if isinstance(addr, tuple) and len(addr) >= 3 else -1
            payload = udp_payload_if_reference(
                packet,
                int(args["ref_port"]),
                packet_type,
            )
            if not payload:
                continue
            meta["ref_packets"] += 1
            meta["ref_bytes"] += len(payload)
            fh.write(payload)
    sock.close()


def main() -> int:
    args = json.loads(sys.argv[1])
    stamp = args["stamp"]
    run_token = args["run_token"]
    if not isinstance(run_token, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{20,64}", run_token
    ):
        raise ValueError("invalid run token")
    workdir = Path("/tmp") / f"jasper-airplay-receiver-proof-{stamp}-{run_token}"
    os.umask(0o077)
    workdir.mkdir(mode=0o700, parents=True, exist_ok=False)

    errors: list[str] = []
    try:
        active_source_before = active_source()
    except RuntimeError as exc:
        active_source_before = "unknown"
        errors.append(str(exc))
    if active_source_before != "airplay":
        errors.append(
            f"active source before capture is {active_source_before!r}, expected 'airplay'"
        )

    outputd_before = uds_status(OUTPUTD_SOCKET)
    errors.extend(outputd_capture_errors(outputd_before, int(args["ref_port"]), "before capture"))

    meta: dict = {
        "host": socket.gethostname(),
        "workdir": str(workdir),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "bytes_per_frame": BYTES_PER_FRAME,
        "args": args,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outputd_before": outputd_before,
        "fanin_before": uds_status(FANIN_SOCKET),
        "outputd_env": parse_env_file("/var/lib/jasper/outputd.env", MODEL_ENV_KEYS),
        "fanin_env": parse_env_file("/var/lib/jasper/fanin.env", MODEL_ENV_KEYS),
        "jasper_env": parse_env_file("/etc/jasper/jasper.env", MODEL_ENV_KEYS),
        "shairport_latency_offset_seconds": shairport_offset(),
        "active_source_before": active_source_before,
    }

    if errors:
        meta["errors"] = errors
        meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (workdir / "meta.remote.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "workdir": str(workdir),
            "run_token": run_token,
            "errors": errors,
        }, sort_keys=True))
        return 1

    start = threading.Event()
    pre_ready = threading.Event()
    ref_ready = threading.Event()

    def guarded(label: str, fn, ready: threading.Event) -> None:
        try:
            fn(args, workdir, ready, start, meta)
        except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

    pre_thread = threading.Thread(target=guarded, args=("pre", capture_pre_tap, pre_ready))
    ref_thread = threading.Thread(target=guarded, args=("ref", capture_reference_udp, ref_ready))
    pre_thread.start()
    ref_thread.start()
    if not pre_ready.wait(5.0):
        errors.append("pre: capture endpoint did not become ready")
    if not ref_ready.wait(5.0):
        errors.append("ref: capture endpoint did not become ready")
    meta["capture_start_monotonic_ns"] = time.monotonic_ns()
    start.set()
    pre_thread.join(float(args["duration"]) + 5.0)
    ref_thread.join(float(args["duration"]) + 5.0)

    if pre_thread.is_alive():
        errors.append("pre: capture thread did not finish")
    if ref_thread.is_alive():
        errors.append("ref: capture thread did not finish")

    meta["outputd_after"] = uds_status(OUTPUTD_SOCKET)
    errors.extend(
        outputd_capture_errors(
            meta["outputd_after"],
            int(args["ref_port"]),
            "after capture",
        )
    )
    meta["fanin_after"] = uds_status(FANIN_SOCKET)
    try:
        meta["active_source_after"] = active_source()
    except RuntimeError as exc:
        meta["active_source_after"] = "unknown"
        errors.append(str(exc))
    if meta["active_source_after"] != "airplay":
        errors.append(
            "active source changed during capture: "
            f"{active_source_before!r} -> {meta['active_source_after']!r}"
        )
    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["errors"] = errors
    (workdir / "meta.remote.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for path in workdir.iterdir():
        path.chmod(0o600)
    workdir.chmod(0o700)
    print(json.dumps({
        "workdir": str(workdir),
        "run_token": run_token,
        "errors": errors,
    }, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def new_run_token() -> str:
    return secrets.token_urlsafe(18)


def require_numpy() -> tuple[Any, Any]:
    try:
        import numpy as np
        from scipy import signal
    except (ImportError, OSError) as exc:
        raise SystemExit(
            "This proof needs numpy + scipy for waveform correlation. "
            "Run it with a Python environment that has them, e.g. "
            "`python -m pip install numpy scipy` in a local virtualenv, "
            "then run this script with that Python.\n"
            f"Import error: {type(exc).__name__}: {exc}"
        ) from exc
    return np, signal


def run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}"


def validate_remote_dir(
    remote_dir: str,
    *,
    expected_stamp: str | None = None,
    expected_token: str | None = None,
) -> Path:
    match = REMOTE_DIR_RE.fullmatch(remote_dir)
    if match is None:
        raise ValueError(f"refusing unsafe remote artifact path: {remote_dir!r}")
    if expected_stamp is not None and match.group("stamp") != expected_stamp:
        raise ValueError(
            "remote artifact stamp mismatch: "
            f"expected {expected_stamp!r}, got {match.group('stamp')!r}"
        )
    if expected_token is not None and match.group("token") != expected_token:
        raise ValueError(
            "remote artifact token mismatch: "
            f"expected {expected_token!r}, got {match.group('token')!r}"
        )
    return Path(remote_dir)


def confirm_remote_ownership(
    remote_result: Any,
    *,
    expected_remote_dir: str,
    expected_stamp: str,
    expected_token: str,
) -> str:
    if not isinstance(remote_result, dict):
        raise RuntimeError("remote capture result is not a JSON object")
    returned_token = remote_result.get("run_token")
    if returned_token != expected_token:
        raise RuntimeError(
            "remote capture did not prove run ownership: "
            f"expected token {expected_token!r}, got {returned_token!r}"
        )
    returned_remote_dir = str(remote_result.get("workdir", ""))
    validate_remote_dir(
        returned_remote_dir,
        expected_stamp=expected_stamp,
        expected_token=expected_token,
    )
    if returned_remote_dir != expected_remote_dir:
        raise RuntimeError(
            "remote capture returned an unexpected artifact directory: "
            f"{returned_remote_dir!r}"
        )
    return returned_remote_dir


def _remote_dir_size(target: str, remote: Path) -> int:
    cmd = [
        "ssh",
        target,
        shlex.join(["sudo", "du", "-sb", "--", str(remote)]),
    ]
    result = run(cmd, check=True, timeout=CLEANUP_TIMEOUT_SEC)
    try:
        size = int(result.stdout.split(maxsplit=1)[0])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"could not parse remote artifact size: {result.stdout!r}") from exc
    if size < 0 or size > MAX_REMOTE_DIR_BYTES:
        raise RuntimeError(
            f"remote artifact directory is {size} bytes; limit is {MAX_REMOTE_DIR_BYTES}"
        )
    return size


def _safe_extract_tar(data: bytes, destination: Path, *, expected_root: str) -> Path:
    destination = destination.resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    dest_root = destination / expected_root
    if dest_root.exists():
        raise RuntimeError(f"refusing to replace existing artifact path: {dest_root}")

    total_size = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_TAR_MEMBERS:
            raise RuntimeError(
                f"unexpected archive member count: {len(members)} "
                f"(limit {MAX_TAR_MEMBERS})"
            )
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or not path.parts or path.parts[0] != expected_root:
                raise RuntimeError(f"refusing unsafe tar member {member.name!r}")
            target = (destination / path).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise RuntimeError(f"refusing unsafe tar member {member.name!r}")
            if not (member.isdir() or member.isreg()):
                raise RuntimeError(f"refusing non-file tar member {member.name!r}")
            if member.size < 0:
                raise RuntimeError(f"refusing negative-size tar member {member.name!r}")
            total_size += member.size
            if total_size > MAX_REMOTE_DIR_BYTES:
                raise RuntimeError(
                    f"archive expands beyond {MAX_REMOTE_DIR_BYTES} byte limit"
                )

        for member in members:
            path = Path(member.name)
            target = destination / path
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read archive member {member.name!r}")
            with source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(0o600)
    return dest_root


def fetch_remote_dir(
    target: str,
    remote_dir: str,
    local_parent: Path,
    *,
    expected_stamp: str,
    expected_token: str,
) -> Path:
    remote = validate_remote_dir(
        remote_dir,
        expected_stamp=expected_stamp,
        expected_token=expected_token,
    )
    _remote_dir_size(target, remote)
    cmd = [
        "ssh",
        target,
        shlex.join(["sudo", "tar", "-C", str(remote.parent), "-czf", "-", remote.name]),
    ]
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        timeout=TRANSFER_TIMEOUT_SEC,
    )
    if len(result.stdout) > MAX_REMOTE_DIR_BYTES:
        raise RuntimeError(
            f"compressed artifact transfer exceeds {MAX_REMOTE_DIR_BYTES} byte limit"
        )
    return _safe_extract_tar(result.stdout, local_parent, expected_root=remote.name)


def cleanup_remote(
    target: str,
    remote_dir: str,
    *,
    expected_stamp: str,
    expected_token: str,
) -> bool:
    remote = validate_remote_dir(
        remote_dir,
        expected_stamp=expected_stamp,
        expected_token=expected_token,
    )
    result = run(
        ["ssh", target, shlex.join(["sudo", "rm", "-rf", "--", str(remote)])],
        check=False,
        timeout=CLEANUP_TIMEOUT_SEC,
    )
    return result.returncode == 0


def load_s16le_mono(path: Path, np: Any) -> Any:
    raw = path.read_bytes()
    usable = len(raw) - (len(raw) % BYTES_PER_FRAME)
    if usable <= 0:
        raise ValueError(f"{path} contains no complete stereo S16_LE frames")
    arr = np.frombuffer(raw[:usable], dtype="<i2").reshape(-1, CHANNELS)
    return arr.astype(np.float64).mean(axis=1)


def write_wav(path: Path, mono: Any, np: Any) -> None:
    clipped = np.clip(mono, -32768, 32767).astype("<i2")
    stereo = np.column_stack([clipped, clipped]).reshape(-1).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(stereo.tobytes())
    path.chmod(0o600)


def int_env(*dicts: dict[str, Any], key: str, default: int = 0) -> int:
    for dct in dicts:
        raw = dct.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(str(raw))
        except ValueError:
            continue
    return default


def int_env_with_source(
    *sources: tuple[str, dict[str, Any]],
    key: str,
    default: int,
) -> tuple[int, str]:
    for label, dct in sources:
        raw = dct.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(str(raw)), f"{label} {key}"
        except ValueError:
            continue
    return default, f"built-in default for {key}"


def get_path(dct: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = dct
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def status_positive_int(status: dict[str, Any], path: list[str]) -> int | None:
    value = get_path(status, path)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def counter_evidence(
    before: dict[str, Any],
    after: dict[str, Any],
    path: list[str],
) -> dict[str, Any]:
    before_value = get_path(before, path)
    after_value = get_path(after, path)
    if (
        not isinstance(before_value, int)
        or isinstance(before_value, bool)
        or not isinstance(after_value, int)
        or isinstance(after_value, bool)
    ):
        return {
            "before": before_value,
            "after": after_value,
            "delta": None,
            "status": "unknown",
        }
    if after_value < before_value:
        return {
            "before": before_value,
            "after": after_value,
            "delta": None,
            "status": "reset",
        }
    delta = after_value - before_value
    return {
        "before": before_value,
        "after": after_value,
        "delta": delta,
        "status": "unchanged" if delta == 0 else "increased",
    }


def require_healthy_counters(
    outputd_before: dict[str, Any],
    outputd_after: dict[str, Any],
    fanin_before: dict[str, Any],
    fanin_after: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    evidence = {
        "outputd_content_xruns": counter_evidence(
            outputd_before, outputd_after, ["content", "xrun_count"]
        ),
        "outputd_dac_xruns": counter_evidence(
            outputd_before, outputd_after, ["dac", "xrun_count"]
        ),
        "outputd_content_empty_periods": counter_evidence(
            outputd_before, outputd_after, ["content", "empty_periods"]
        ),
        "fanin_output_xruns": counter_evidence(
            fanin_before, fanin_after, ["output", "xrun_count"]
        ),
    }
    for name, item in evidence.items():
        if item["status"] in {"unknown", "reset"}:
            raise ValueError(
                f"capture health counter {name} is {item['status']} "
                f"(before={item['before']!r}, after={item['after']!r})"
            )
    increased_xruns = [
        name
        for name in (
            "outputd_content_xruns",
            "outputd_dac_xruns",
            "fanin_output_xruns",
        )
        if evidence[name]["delta"] > 0
    ]
    if increased_xruns:
        details = ", ".join(
            f"{name}=+{evidence[name]['delta']}" for name in increased_xruns
        )
        raise ValueError(f"capture crossed an audio xrun: {details}")
    return evidence


def audio_metrics(samples: Any, np: Any) -> dict[str, float]:
    if samples.size == 0:
        return {"frames": 0, "rms": 0.0, "peak": 0.0, "dbfs": -120.0}
    rms = float(np.sqrt(np.mean(samples * samples)))
    peak = float(np.max(np.abs(samples)))
    dbfs = 20.0 * float(np.log10(max(rms, 1e-9) / 32768.0))
    return {"frames": int(samples.size), "rms": rms, "peak": peak, "dbfs": dbfs}


def require_capture_coverage(
    pre: Any,
    ref: Any,
    *,
    duration: float,
) -> dict[str, Any]:
    requested_frames = int(round(duration * SAMPLE_RATE))
    minimum_frames = int(math.ceil(requested_frames * MIN_CAPTURE_COVERAGE_RATIO))
    pre_frames = int(pre.size)
    ref_frames = int(ref.size)
    evidence = {
        "requested_frames": requested_frames,
        "minimum_frames": minimum_frames,
        "minimum_ratio": MIN_CAPTURE_COVERAGE_RATIO,
        "pre_frames": pre_frames,
        "reference_frames": ref_frames,
        "pre_ratio": pre_frames / requested_frames,
        "reference_ratio": ref_frames / requested_frames,
    }
    short = [
        name
        for name, frames in (("pre", pre_frames), ("reference", ref_frames))
        if frames < minimum_frames
    ]
    if short:
        details = ", ".join(
            f"{name}={evidence[f'{name}_frames']}/{requested_frames} frames"
            for name in short
        )
        raise ValueError(
            "capture is incomplete; each endpoint must contain at least "
            f"{MIN_CAPTURE_COVERAGE_RATIO:.0%} of the requested duration ({details})"
        )
    return evidence


def overlap_for_lag(ref: Any, pre: Any, lag: int) -> tuple[Any, Any]:
    if lag >= 0:
        n = min(pre.size, ref.size - lag)
        if n <= 0:
            return ref[:0], pre[:0]
        return ref[lag : lag + n], pre[:n]
    n = min(pre.size + lag, ref.size)
    if n <= 0:
        return ref[:0], pre[:0]
    return ref[:n], pre[-lag : -lag + n]


def estimate_lag(
    pre: Any,
    ref: Any,
    *,
    np: Any,
    signal: Any,
    min_latency_ms: float,
    max_latency_ms: float,
) -> dict[str, Any]:
    """Estimate asynchronous digital pre-tap to reference latency.

    This intentionally differs from ``aec-probe-timing.py::estimate_lag``.
    That acoustic ref-to-mic probe searches positive mic lag in aligned 16 kHz
    captures. Here the two 48 kHz capture workers start independently, so the
    proof differentiates program audio, searches a two-sided file-relative
    latency window, and rejects ambiguous repeated peaks. Userspace read times
    are deliberately not treated as sample timestamps: both arecord and the
    AF_PACKET socket can deliver buffered audio on their first read.
    Keeping those semantics explicit is clearer than a switch-heavy shared
    helper whose modes would each have one caller.
    """
    pre_d = np.diff(pre)
    ref_d = np.diff(ref)
    pre_d = pre_d - np.mean(pre_d)
    ref_d = ref_d - np.mean(ref_d)
    pre_std = float(np.std(pre_d))
    ref_std = float(np.std(ref_d))
    if pre_std <= 0.0 or ref_std <= 0.0:
        raise ValueError("one capture is flat; play audio with transients and rerun")
    pre_z = pre_d / pre_std
    ref_z = ref_d / ref_std

    lag_min = int(round(min_latency_ms / 1000.0 * SAMPLE_RATE))
    lag_max = int(round(max_latency_ms / 1000.0 * SAMPLE_RATE))
    if lag_min > lag_max:
        lag_min, lag_max = lag_max, lag_min

    corr = signal.correlate(ref_z, pre_z, mode="full", method="fft")
    lags = signal.correlation_lags(ref_z.size, pre_z.size, mode="full")
    mask = (lags >= lag_min) & (lags <= lag_max)
    if not bool(np.any(mask)):
        raise ValueError("search window does not overlap the captured files")
    window = corr[mask]
    window_lags = lags[mask]
    abs_window = np.abs(window)
    best_i = int(np.argmax(abs_window))
    best_lag = int(window_lags[best_i])

    ref_seg, pre_seg = overlap_for_lag(ref_z, pre_z, best_lag)
    denom = float(np.sqrt(np.sum(ref_seg * ref_seg) * np.sum(pre_seg * pre_seg)))
    normalized = float(abs_window[best_i] / denom) if denom > 0.0 else 0.0

    exclude = int(round(0.003 * SAMPLE_RATE))
    second_mask = np.abs(window_lags - best_lag) > exclude
    second_peak = float(np.max(abs_window[second_mask])) if bool(np.any(second_mask)) else 0.0
    ambiguity_ratio = float(abs_window[best_i] / max(second_peak, 1.0))
    median = float(np.median(abs_window))
    peak_to_median = float(abs_window[best_i] / max(median, 1.0))
    latency_ms = best_lag / SAMPLE_RATE * 1000.0

    if normalized >= 0.25 and ambiguity_ratio >= 1.15 and peak_to_median >= 8.0:
        confidence = "high"
    elif normalized >= 0.12 and ambiguity_ratio >= 1.05 and peak_to_median >= 4.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "lag_samples_ref_minus_pre": best_lag,
        "measured_latency_ms": latency_ms,
        "normalized_peak": normalized,
        "ambiguity_ratio": ambiguity_ratio,
        "peak_to_median": peak_to_median,
        "confidence": confidence,
        "search_latency_ms": [min_latency_ms, max_latency_ms],
    }


def require_accepted_correlation(lag: dict[str, Any]) -> None:
    if lag.get("confidence") == "low":
        raise ValueError(
            "low-confidence correlation; play transient-rich audio and rerun "
            f"(normalized={float(lag.get('normalized_peak', 0.0)):.3f}, "
            f"ambiguity={float(lag.get('ambiguity_ratio', 0.0)):.2f}, "
            f"peak/median={float(lag.get('peak_to_median', 0.0)):.1f})"
        )


def validate_airplay_source_identity(meta: dict[str, Any]) -> None:
    before = meta.get("active_source_before")
    after = meta.get("active_source_after")
    if before != "airplay" or after != "airplay":
        raise ValueError(
            "capture is not a stable AirPlay proof: "
            f"active_source before={before!r}, after={after!r}"
        )


def build_model(meta: dict[str, Any]) -> dict[str, Any]:
    outputd_env = dict_value(meta.get("outputd_env"))
    fanin_env = dict_value(meta.get("fanin_env"))
    jasper_env = dict_value(meta.get("jasper_env"))
    outputd_after = dict_value(meta.get("outputd_after"))
    fanin_after = dict_value(meta.get("fanin_after"))

    chunk, chunk_source = int_env_with_source(
        ("outputd env", outputd_env),
        ("jasper env", jasper_env),
        key="JASPER_CAMILLA_CHUNKSIZE",
        default=1024,
    )
    target, target_source = int_env_with_source(
        ("outputd env", outputd_env),
        ("jasper env", jasper_env),
        key="JASPER_CAMILLA_TARGET_LEVEL",
        default=2048,
    )
    fanin_status_buffer = status_positive_int(fanin_after, ["output", "buffer_frames"])
    outputd_status_buffer = status_positive_int(outputd_after, ["dac", "buffer_frames"])
    outputd_status_period = status_positive_int(outputd_after, ["dac", "period_frames"])
    fanin_output = fanin_status_buffer or int_env(
        fanin_env,
        jasper_env,
        key="JASPER_FANIN_OUTPUT_BUFFER_FRAMES",
        default=1024,
    )
    outputd_dac_buffer = outputd_status_buffer or int_env(
        outputd_env,
        jasper_env,
        key="JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
        default=3072,
    )
    outputd_period = outputd_status_period or int_env(
        outputd_env,
        jasper_env,
        key="JASPER_OUTPUTD_PERIOD_FRAMES",
        default=1024,
    )
    camilla_extra = max(target - chunk, 0)
    live_dac_delay = get_path(outputd_after, ["dac", "snd_pcm_delay_frames"], None)
    if not isinstance(live_dac_delay, int):
        live_dac_delay = outputd_dac_buffer
    configured_full = fanin_output + camilla_extra + outputd_dac_buffer
    live_full = fanin_output + camilla_extra + live_dac_delay
    return {
        "camilla_chunk_frames": chunk,
        "camilla_chunk_source": f"inferred from {chunk_source}",
        "camilla_target_level_frames": target,
        "camilla_target_level_source": f"inferred from {target_source}",
        "camilla_extra_frames": camilla_extra,
        "camilla_values_kind": "inferred_from_allowlisted_env_or_builtin_defaults",
        "fanin_output_buffer_frames": fanin_output,
        "fanin_output_buffer_source": (
            "fanin STATUS output.buffer_frames"
            if fanin_status_buffer is not None
            else "allowlisted env or built-in default"
        ),
        "outputd_period_frames": outputd_period,
        "outputd_period_source": (
            "outputd STATUS dac.period_frames"
            if outputd_status_period is not None
            else "allowlisted env or built-in default"
        ),
        "outputd_configured_dac_buffer_frames": outputd_dac_buffer,
        "outputd_dac_buffer_source": (
            "outputd STATUS dac.buffer_frames"
            if outputd_status_buffer is not None
            else "allowlisted env or built-in default"
        ),
        "outputd_live_dac_delay_frames": live_dac_delay,
        "configured_full_hidden_frames": configured_full,
        "configured_full_hidden_ms": configured_full / SAMPLE_RATE * 1000.0,
        "live_full_hidden_frames": live_full,
        "live_full_hidden_ms": live_full / SAMPLE_RATE * 1000.0,
        "shairport_offset_seconds": meta.get("shairport_latency_offset_seconds"),
        "shairport_offset_ms": (
            float(meta["shairport_latency_offset_seconds"]) * 1000.0
            if isinstance(meta.get("shairport_latency_offset_seconds"), (int, float))
            else None
        ),
    }


def analyze_capture(local_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    np, signal = require_numpy()
    meta = json.loads((local_dir / "meta.remote.json").read_text(encoding="utf-8"))
    if meta.get("errors"):
        raise RuntimeError(f"remote capture errors: {meta['errors']}")
    validate_airplay_source_identity(meta)

    pre = load_s16le_mono(local_dir / "pre_jasper_capture.s16le", np)
    ref = load_s16le_mono(local_dir / "outputd_reference_udp.s16le", np)
    coverage = require_capture_coverage(pre, ref, duration=args.duration)
    write_wav(local_dir / "pre_jasper_capture.wav", pre, np)
    write_wav(local_dir / "outputd_reference_udp.wav", ref, np)

    lag = estimate_lag(
        pre,
        ref,
        np=np,
        signal=signal,
        min_latency_ms=args.min_latency_ms,
        max_latency_ms=args.max_latency_ms,
    )
    require_accepted_correlation(lag)
    model = build_model(meta)
    outputd_before = dict_value(meta.get("outputd_before"))
    outputd_after = dict_value(meta.get("outputd_after"))
    fanin_before = dict_value(meta.get("fanin_before"))
    fanin_after = dict_value(meta.get("fanin_after"))
    health_evidence = require_healthy_counters(
        outputd_before,
        outputd_after,
        fanin_before,
        fanin_after,
    )

    offset_abs = (
        abs(model["shairport_offset_ms"])
        if isinstance(model.get("shairport_offset_ms"), float)
        else None
    )

    report = {
        "artifact_dir": str(local_dir),
        "pre_tap": (
            f"{args.pre_device} (legacy loopback lane-7 summed-program diagnostic tap)"
        ),
        "reference_tap": "outputd final electrical UDP reference sniffed on loopback",
        "lag": lag,
        "model": model,
        "derived": {
            "measured_receiver_hidden_delay_ms": lag["measured_latency_ms"],
            "measurement_minus_configured_model_ms": (
                lag["measured_latency_ms"] - model["configured_full_hidden_ms"]
            ),
            "measurement_minus_live_model_ms": (
                lag["measured_latency_ms"] - model["live_full_hidden_ms"]
            ),
            "configured_shairport_offset_abs_ms": offset_abs,
            "measurement_minus_configured_offset_abs_ms": (
                lag["measured_latency_ms"] - offset_abs
                if offset_abs is not None
                else None
            ),
        },
        "health_deltas": {
            name: item["delta"] for name, item in health_evidence.items()
        },
        "health_counter_evidence": health_evidence,
        "capture_metrics": {
            "pre": audio_metrics(pre, np),
            "reference": audio_metrics(ref, np),
            "ref_packets": meta.get("ref_packets"),
            "pre_bytes": meta.get("pre_bytes"),
            "ref_bytes": meta.get("ref_bytes"),
            "coverage": coverage,
        },
        "remote_meta": meta,
    }
    (local_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (local_dir / "report.json").chmod(0o600)
    return report


def print_summary(report: dict[str, Any]) -> None:
    lag = report["lag"]
    model = report["model"]
    derived = report["derived"]
    health = report["health_deltas"]
    metrics = report["capture_metrics"]
    print()
    print("================ Receiver timing proof ================")
    print(f"Artifacts: {report['artifact_dir']}")
    print(f"Measured receiver-side tap-to-output delay: {lag['measured_latency_ms']:.2f} ms")
    print(
        "Configured full hidden-delay model: "
        f"{model['configured_full_hidden_ms']:.2f} ms "
        f"({model['configured_full_hidden_frames']} frames)"
    )
    print(
        "Live full hidden-delay model: "
        f"{model['live_full_hidden_ms']:.2f} ms "
        f"({model['live_full_hidden_frames']} frames)"
    )
    if model.get("shairport_offset_ms") is not None:
        print(f"shairport backend offset: {model['shairport_offset_ms']:.3f} ms")
        print(
            "Measured minus |offset|: "
            f"{derived['measurement_minus_configured_offset_abs_ms']:.2f} ms"
        )
    print(
        "Measured minus live model: "
        f"{derived['measurement_minus_live_model_ms']:.2f} ms"
    )
    print(
        "Correlation: "
        f"{lag['confidence']} confidence, normalized={lag['normalized_peak']:.3f}, "
        f"ambiguity={lag['ambiguity_ratio']:.2f}, peak/median={lag['peak_to_median']:.1f}"
    )
    print(
        "Capture level: "
        f"pre {metrics['pre']['dbfs']:.1f} dBFS, "
        f"reference {metrics['reference']['dbfs']:.1f} dBFS, "
        f"ref packets {metrics['ref_packets']}"
    )
    print(
        "Health deltas: "
        + ", ".join(f"{key}={value}" for key, value in health.items())
    )
    print()
    print(
        "Interpretation: this proves the receiver audio path from the shared "
        "loopback lane-7 diagnostic tap to outputd's final electrical reference. "
        "It does not prove the sender's video display time."
    )


def print_aggregate(reports: list[dict[str, Any]]) -> None:
    if any(get_path(report, ["lag", "confidence"]) == "low" for report in reports):
        raise ValueError("refusing to aggregate a low-confidence correlation")
    if len(reports) <= 1:
        return
    np, _signal = require_numpy()
    measured = np.array(
        [r["derived"]["measured_receiver_hidden_delay_ms"] for r in reports],
        dtype=np.float64,
    )
    live_delta = np.array(
        [r["derived"]["measurement_minus_live_model_ms"] for r in reports],
        dtype=np.float64,
    )
    offset_delta = np.array(
        [
            r["derived"]["measurement_minus_configured_offset_abs_ms"]
            for r in reports
            if r["derived"]["measurement_minus_configured_offset_abs_ms"] is not None
        ],
        dtype=np.float64,
    )
    print()
    print("================ Aggregate ================")
    print(f"Runs: {len(reports)}")
    print(
        "Measured receiver-side tap-to-output delay: "
        f"median {float(np.median(measured)):.2f} ms, "
        f"range {float(np.min(measured)):.2f}..{float(np.max(measured)):.2f} ms"
    )
    print(
        "Measured minus live model: "
        f"median {float(np.median(live_delta)):.2f} ms, "
        f"range {float(np.min(live_delta)):.2f}..{float(np.max(live_delta)):.2f} ms"
    )
    if offset_delta.size:
        print(
            "Measured minus |shairport offset|: "
            f"median {float(np.median(offset_delta)):.2f} ms, "
            f"range {float(np.min(offset_delta)):.2f}..{float(np.max(offset_delta)):.2f} ms"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture and correlate the local AirPlay receiver path without "
            "binding outputd's reference UDP port."
        )
    )
    parser.add_argument("--host", default=os.environ.get("PI_HOST", "jts2.local"))
    parser.add_argument("--user", default=os.environ.get("PI_USER", "pi"))
    parser.add_argument(
        "--duration",
        type=float,
        default=15.0,
        help=f"seconds per private capture (2..{MAX_DURATION_SEC:g}; default: 15)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help=f"capture repetitions (1..{MAX_RUNS}; default: 1)",
    )
    parser.add_argument(
        "--pause-s",
        type=float,
        default=1.0,
        help=f"pause between runs (0..{MAX_PAUSE_SEC:g}; default: 1)",
    )
    parser.add_argument("--pre-device", default=DEFAULT_PRE_DEVICE)
    parser.add_argument("--ref-port", type=int, default=DEFAULT_REF_PORT)
    parser.add_argument("--min-latency-ms", type=float, default=-20.0)
    parser.add_argument("--max-latency-ms", type=float, default=250.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("captures") / "airplay-receiver-timing",
    )
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="retain the private root-owned /tmp capture on the Pi",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not math.isfinite(args.duration) or not 2.0 <= args.duration <= MAX_DURATION_SEC:
        raise SystemExit(
            f"--duration must be between 2 and {MAX_DURATION_SEC:g} seconds"
        )
    if not 1 <= args.runs <= MAX_RUNS:
        raise SystemExit(f"--runs must be between 1 and {MAX_RUNS}")
    if not math.isfinite(args.pause_s) or not 0 <= args.pause_s <= MAX_PAUSE_SEC:
        raise SystemExit(f"--pause-s must be between 0 and {MAX_PAUSE_SEC:g} seconds")
    if not 1 <= args.ref_port <= 65535:
        raise SystemExit("--ref-port must be between 1 and 65535")
    if args.ref_port != DEFAULT_REF_PORT:
        raise SystemExit(
            f"--ref-port must be {DEFAULT_REF_PORT}; the proof validates outputd's "
            "production electrical-reference target"
        )
    if args.pre_device != DEFAULT_PRE_DEVICE:
        raise SystemExit(
            f"--pre-device must be {DEFAULT_PRE_DEVICE}; the proof is valid only "
            "for the production lane-7 diagnostic tap"
        )
    if (
        not math.isfinite(args.min_latency_ms)
        or not math.isfinite(args.max_latency_ms)
        or not MIN_LATENCY_MS <= args.min_latency_ms < args.max_latency_ms <= MAX_LATENCY_MS
    ):
        raise SystemExit(
            "latency search must satisfy "
            f"{MIN_LATENCY_MS:g} <= --min-latency-ms < --max-latency-ms "
            f"<= {MAX_LATENCY_MS:g}"
        )
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", args.user):
        raise SystemExit("--user must be a plain remote account name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.host):
        raise SystemExit("--host must be a hostname or IPv4 address")


def main() -> int:
    args = parse_args()
    validate_args(args)
    target = ssh_target(args.user, args.host)

    print(
        textwrap.dedent(
            f"""
            Capturing receiver-side timing proof from {target}: {args.runs} run(s), {args.duration:.1f}s each.
            Play AirPlay audio with clear transients now; click/chirp content gives the cleanest proof.
            This is read-only for the audio services: no deploy, no restart, no UDP bind.
            """
        ).strip()
    )
    reports: list[dict[str, Any]] = []
    for run_index in range(args.runs):
        stamp = utc_stamp()
        if args.runs > 1:
            stamp = f"{stamp}-run{run_index + 1}"
            print()
            print(f"--- run {run_index + 1}/{args.runs} ---")
        run_token = new_run_token()
        config = {
            "duration": args.duration,
            "pre_device": args.pre_device,
            "ref_port": args.ref_port,
            "stamp": stamp,
            "run_token": run_token,
        }
        expected_remote_dir = f"{REMOTE_DIR_PREFIX}{stamp}-{run_token}"
        remote_cmd = [
            "ssh",
            target,
            f"sudo python3 - {shlex.quote(json.dumps(config, separators=(',', ':')))}",
        ]
        owned_remote_dir: str | None = None
        try:
            result = run(
                remote_cmd,
                input_text=REMOTE_CAPTURE,
                check=False,
                timeout=args.duration + REMOTE_COMMAND_GRACE_SEC,
            )
            if result.stderr:
                sys.stderr.write(result.stderr)
            if not result.stdout.strip():
                raise RuntimeError("remote capture produced no JSON output")
            try:
                remote_result = json.loads(result.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"could not parse remote capture output:\n{result.stdout}"
                ) from exc
            returned_remote_dir = confirm_remote_ownership(
                remote_result,
                expected_remote_dir=expected_remote_dir,
                expected_stamp=stamp,
                expected_token=run_token,
            )
            owned_remote_dir = returned_remote_dir
            if result.returncode != 0:
                raise RuntimeError(f"remote capture failed: {remote_result.get('errors')}")
            local_dir = fetch_remote_dir(
                target,
                owned_remote_dir,
                args.out_dir.resolve(),
                expected_stamp=stamp,
                expected_token=run_token,
            )
            report = analyze_capture(local_dir, args)
            reports.append(report)
            print_summary(report)
        finally:
            if args.keep_remote and owned_remote_dir is not None:
                print(f"Remote artifacts retained at {target}:{owned_remote_dir}")
            elif not args.keep_remote and owned_remote_dir is not None:
                try:
                    cleaned = cleanup_remote(
                        target,
                        owned_remote_dir,
                        expected_stamp=stamp,
                        expected_token=run_token,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    cleaned = False
                    cleanup_detail = f": {type(exc).__name__}: {exc}"
                else:
                    cleanup_detail = ""
                if not cleaned:
                    print(
                        "WARNING: could not remove remote artifacts at "
                        f"{target}:{owned_remote_dir}{cleanup_detail}",
                        file=sys.stderr,
                    )
            elif owned_remote_dir is None:
                print(
                    "WARNING: remote ownership was not confirmed; no cleanup was "
                    f"attempted for {target}:{expected_remote_dir}",
                    file=sys.stderr,
                )
        if run_index + 1 < args.runs and args.pause_s > 0:
            time.sleep(args.pause_s)
    print_aggregate(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
