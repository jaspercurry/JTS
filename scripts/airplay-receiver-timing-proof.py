#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Receiver-side timing proof for the AirPlay/output pipeline.

This is intentionally receiver-side only: it does not try to observe the
sender's display scanout. It measures the local audio pipeline by capturing:

  * ``plug:jasper_capture``: the shared summed-program tap feeding CamillaDSP.
  * outputd's final electrical speaker-reference UDP stream on loopback.

The script never binds the UDP port, so it can run while the AEC bridge owns
127.0.0.1:9891. It sniffs loopback with a temporary root AF_PACKET socket,
captures the shared ALSA tap with arecord, copies both raw files back, and
correlates the waveforms locally.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import wave
from pathlib import Path
from typing import Any


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2
DEFAULT_REF_PORT = 9891
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
FANIN_SOCKET = "/run/jasper-fanin/control.sock"


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
from pathlib import Path


SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
FANIN_SOCKET = "/run/jasper-fanin/control.sock"


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


def parse_env_file(path: str) -> dict[str, str]:
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
        value = value.strip().strip("'").strip('"')
        out[key.strip()] = value
    return out


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


def udp_payload_if_reference(packet: bytes, port: int) -> bytes | None:
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


def capture_pre_tap(args: dict, workdir: Path, start: threading.Event, meta: dict) -> None:
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
    meta["pre_first_monotonic_ns"] = None
    meta["pre_last_monotonic_ns"] = None
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
                now = time.monotonic_ns()
                if meta["pre_first_monotonic_ns"] is None:
                    meta["pre_first_monotonic_ns"] = now
                meta["pre_last_monotonic_ns"] = now
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


def capture_reference_udp(args: dict, workdir: Path, start: threading.Event, meta: dict) -> None:
    out_path = workdir / "outputd_reference_udp.s16le"
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
    sock.bind(("lo", 0))
    sock.settimeout(0.1)
    meta["ref_packets"] = 0
    meta["ref_bytes"] = 0
    meta["ref_first_monotonic_ns"] = None
    meta["ref_last_monotonic_ns"] = None
    start.wait()
    deadline = time.monotonic() + float(args["duration"])
    with out_path.open("wb") as fh:
        while time.monotonic() < deadline:
            try:
                packet, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            payload = udp_payload_if_reference(packet, int(args["ref_port"]))
            if not payload:
                continue
            now = time.monotonic_ns()
            if meta["ref_first_monotonic_ns"] is None:
                meta["ref_first_monotonic_ns"] = now
            meta["ref_last_monotonic_ns"] = now
            meta["ref_packets"] += 1
            meta["ref_bytes"] += len(payload)
            fh.write(payload)
    sock.close()


def main() -> int:
    args = json.loads(sys.argv[1])
    stamp = args["stamp"]
    workdir = Path("/tmp") / f"jasper-airplay-receiver-proof-{stamp}"
    workdir.mkdir(mode=0o755, parents=True, exist_ok=True)

    meta: dict = {
        "host": socket.gethostname(),
        "workdir": str(workdir),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "bytes_per_frame": BYTES_PER_FRAME,
        "args": args,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outputd_before": uds_status(OUTPUTD_SOCKET),
        "fanin_before": uds_status(FANIN_SOCKET),
        "outputd_env": parse_env_file("/var/lib/jasper/outputd.env"),
        "fanin_env": parse_env_file("/var/lib/jasper/fanin.env"),
        "jasper_env": parse_env_file("/etc/jasper/jasper.env"),
        "shairport_latency_offset_seconds": shairport_offset(),
    }

    start = threading.Event()
    errors: list[str] = []

    def guarded(label: str, fn) -> None:
        try:
            fn(args, workdir, start, meta)
        except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

    pre_thread = threading.Thread(target=guarded, args=("pre", capture_pre_tap))
    ref_thread = threading.Thread(target=guarded, args=("ref", capture_reference_udp))
    pre_thread.start()
    ref_thread.start()
    time.sleep(0.25)
    meta["capture_start_monotonic_ns"] = time.monotonic_ns()
    start.set()
    pre_thread.join(float(args["duration"]) + 5.0)
    ref_thread.join(float(args["duration"]) + 5.0)

    if pre_thread.is_alive():
        errors.append("pre: capture thread did not finish")
    if ref_thread.is_alive():
        errors.append("ref: capture thread did not finish")

    meta["outputd_after"] = uds_status(OUTPUTD_SOCKET)
    meta["fanin_after"] = uds_status(FANIN_SOCKET)
    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["errors"] = errors
    (workdir / "meta.remote.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for path in workdir.iterdir():
        path.chmod(0o644)
    workdir.chmod(0o755)
    print(json.dumps({"workdir": str(workdir), "errors": errors}, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


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


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def ssh_target(user: str, host: str) -> str:
    return f"{user}@{host}"


def fetch_remote_dir(target: str, remote_dir: str, local_dir: Path) -> None:
    parent = local_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    if local_dir.exists():
        shutil.rmtree(local_dir)
    cmd = ["scp", "-q", "-r", f"{target}:{remote_dir}", str(parent)]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "scp failed\n"
            f"cmd: {' '.join(shlex.quote(p) for p in cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    fetched = parent / Path(remote_dir).name
    if fetched != local_dir:
        fetched.rename(local_dir)


def cleanup_remote(target: str, remote_dir: str) -> None:
    quoted = shlex.quote(remote_dir)
    run(["ssh", target, f"sudo rm -rf -- {quoted}"], check=False)


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


def get_path(dct: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = dct
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def xrun_delta(before: dict[str, Any], after: dict[str, Any], path: list[str]) -> int | None:
    a = get_path(before, path)
    b = get_path(after, path)
    if isinstance(a, int) and isinstance(b, int):
        return b - a
    return None


def audio_metrics(samples: Any, np: Any) -> dict[str, float]:
    if samples.size == 0:
        return {"frames": 0, "rms": 0.0, "peak": 0.0, "dbfs": -120.0}
    rms = float(np.sqrt(np.mean(samples * samples)))
    peak = float(np.max(np.abs(samples)))
    dbfs = 20.0 * float(np.log10(max(rms, 1e-9) / 32768.0))
    return {"frames": int(samples.size), "rms": rms, "peak": peak, "dbfs": dbfs}


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
    start_delta_s: float,
    min_latency_ms: float,
    max_latency_ms: float,
) -> dict[str, Any]:
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

    lag_min = int(round((min_latency_ms / 1000.0 - start_delta_s) * SAMPLE_RATE))
    lag_max = int(round((max_latency_ms / 1000.0 - start_delta_s) * SAMPLE_RATE))
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
    latency_ms = (start_delta_s + best_lag / SAMPLE_RATE) * 1000.0

    if normalized >= 0.25 and ambiguity_ratio >= 1.15 and peak_to_median >= 8.0:
        confidence = "high"
    elif normalized >= 0.12 and ambiguity_ratio >= 1.05 and peak_to_median >= 4.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "lag_samples_ref_minus_pre": best_lag,
        "start_delta_ms_ref_minus_pre": start_delta_s * 1000.0,
        "measured_latency_ms": latency_ms,
        "normalized_peak": normalized,
        "ambiguity_ratio": ambiguity_ratio,
        "peak_to_median": peak_to_median,
        "confidence": confidence,
        "search_latency_ms": [min_latency_ms, max_latency_ms],
    }


def build_model(meta: dict[str, Any]) -> dict[str, Any]:
    outputd_env = meta.get("outputd_env") if isinstance(meta.get("outputd_env"), dict) else {}
    fanin_env = meta.get("fanin_env") if isinstance(meta.get("fanin_env"), dict) else {}
    jasper_env = meta.get("jasper_env") if isinstance(meta.get("jasper_env"), dict) else {}
    outputd_after = meta.get("outputd_after") if isinstance(meta.get("outputd_after"), dict) else {}

    chunk = int_env(outputd_env, jasper_env, key="JASPER_CAMILLA_CHUNKSIZE", default=1024)
    target = int_env(outputd_env, jasper_env, key="JASPER_CAMILLA_TARGET_LEVEL", default=2048)
    fanin_output = int_env(fanin_env, jasper_env, key="JASPER_FANIN_OUTPUT_BUFFER_FRAMES", default=1024)
    outputd_dac_buffer = int_env(outputd_env, jasper_env, key="JASPER_OUTPUTD_DAC_BUFFER_FRAMES", default=3072)
    outputd_period = int_env(outputd_env, jasper_env, key="JASPER_OUTPUTD_PERIOD_FRAMES", default=1024)
    camilla_extra = max(target - chunk, 0)
    live_dac_delay = get_path(outputd_after, ["dac", "snd_pcm_delay_frames"], None)
    if not isinstance(live_dac_delay, int):
        live_dac_delay = outputd_dac_buffer
    configured_full = fanin_output + camilla_extra + outputd_dac_buffer
    live_full = fanin_output + camilla_extra + live_dac_delay
    return {
        "camilla_chunk_frames": chunk,
        "camilla_target_level_frames": target,
        "camilla_extra_frames": camilla_extra,
        "fanin_output_buffer_frames": fanin_output,
        "outputd_period_frames": outputd_period,
        "outputd_configured_dac_buffer_frames": outputd_dac_buffer,
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

    pre = load_s16le_mono(local_dir / "pre_jasper_capture.s16le", np)
    ref = load_s16le_mono(local_dir / "outputd_reference_udp.s16le", np)
    write_wav(local_dir / "pre_jasper_capture.wav", pre, np)
    write_wav(local_dir / "outputd_reference_udp.wav", ref, np)

    pre_first = meta.get("pre_first_monotonic_ns")
    ref_first = meta.get("ref_first_monotonic_ns")
    if not isinstance(pre_first, int) or not isinstance(ref_first, int):
        raise ValueError("remote capture did not record first-sample timestamps")
    start_delta_s = (ref_first - pre_first) / 1_000_000_000.0

    lag = estimate_lag(
        pre,
        ref,
        np=np,
        signal=signal,
        start_delta_s=start_delta_s,
        min_latency_ms=args.min_latency_ms,
        max_latency_ms=args.max_latency_ms,
    )
    model = build_model(meta)
    outputd_before = meta.get("outputd_before") if isinstance(meta.get("outputd_before"), dict) else {}
    outputd_after = meta.get("outputd_after") if isinstance(meta.get("outputd_after"), dict) else {}
    fanin_before = meta.get("fanin_before") if isinstance(meta.get("fanin_before"), dict) else {}
    fanin_after = meta.get("fanin_after") if isinstance(meta.get("fanin_after"), dict) else {}

    offset_abs = (
        abs(model["shairport_offset_ms"])
        if isinstance(model.get("shairport_offset_ms"), float)
        else None
    )

    report = {
        "artifact_dir": str(local_dir),
        "pre_tap": "plug:jasper_capture (fan-in summed output / Camilla input)",
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
            "outputd_content_xruns": xrun_delta(
                outputd_before, outputd_after, ["content", "xrun_count"]
            ),
            "outputd_dac_xruns": xrun_delta(outputd_before, outputd_after, ["dac", "xrun_count"]),
            "outputd_content_empty_periods": xrun_delta(
                outputd_before, outputd_after, ["content", "empty_periods"]
            ),
            "fanin_output_xruns": xrun_delta(fanin_before, fanin_after, ["output", "xrun_count"]),
        },
        "capture_metrics": {
            "pre": audio_metrics(pre, np),
            "reference": audio_metrics(ref, np),
            "ref_packets": meta.get("ref_packets"),
            "pre_bytes": meta.get("pre_bytes"),
            "ref_bytes": meta.get("ref_bytes"),
        },
        "remote_meta": meta,
    }
    (local_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
        "fan-in/Camilla input tap to outputd's final electrical reference. "
        "It does not prove the sender's video display time."
    )


def print_aggregate(reports: list[dict[str, Any]]) -> None:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture and correlate the local AirPlay receiver path without "
            "binding outputd's reference UDP port."
        )
    )
    parser.add_argument("--host", default=os.environ.get("PI_HOST", "jts2.local"))
    parser.add_argument("--user", default=os.environ.get("PI_USER", "pi"))
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pause-s", type=float, default=1.0)
    parser.add_argument("--pre-device", default="plug:jasper_capture")
    parser.add_argument("--ref-port", type=int, default=DEFAULT_REF_PORT)
    parser.add_argument("--min-latency-ms", type=float, default=-20.0)
    parser.add_argument("--max-latency-ms", type=float, default=250.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("captures") / "airplay-receiver-timing",
    )
    parser.add_argument("--keep-remote", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration < 2.0:
        raise SystemExit("--duration must be at least 2 seconds")
    if args.runs < 1:
        raise SystemExit("--runs must be at least 1")
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
        local_dir = (args.out_dir / f"{args.host}-{stamp}").resolve()
        config = {
            "duration": args.duration,
            "pre_device": args.pre_device,
            "ref_port": args.ref_port,
            "stamp": stamp,
        }
        remote_cmd = [
            "ssh",
            target,
            f"sudo python3 - {shlex.quote(json.dumps(config, separators=(',', ':')))}",
        ]
        result = run(remote_cmd, input_text=REMOTE_CAPTURE, check=False)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if not result.stdout.strip():
            raise RuntimeError("remote capture produced no JSON output")
        try:
            remote_result = json.loads(result.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"could not parse remote capture output:\n{result.stdout}") from exc
        remote_dir = str(remote_result["workdir"])
        try:
            if result.returncode != 0:
                raise RuntimeError(f"remote capture failed: {remote_result.get('errors')}")
            fetch_remote_dir(target, remote_dir, local_dir)
            report = analyze_capture(local_dir, args)
            reports.append(report)
            print_summary(report)
        finally:
            if args.keep_remote:
                print(f"Kept remote artifacts on {target}:{remote_dir}")
            else:
                cleanup_remote(target, remote_dir)
        if run_index + 1 < args.runs and args.pause_s > 0:
            time.sleep(args.pause_s)
    print_aggregate(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
