#!/usr/bin/env python3
"""Capture one synchronized UMIK/JTS correction diagnostic bundle.

This is an operator tool, not a second measurement controller.  It observes the
normal browser/relay level-match run without changing volume, starting audio, or
posting relay events.  Raw room audio is intentionally local-only and stored in
the gitignored ``captures/`` tree with restrictive permissions.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import queue
import shlex
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import sounddevice as sd
from scipy.io import wavfile


SAMPLE_RATE = 48_000
CHANNELS = 2
BLOCK_FRAMES = 9_600  # 200 ms: same analysis cadence as the browser meter.
CLIP_THRESHOLD = 0.999


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -120.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":"), sort_keys=True))
        handle.write("\n")


def _fetch_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {}


def _find_input_device(label: str) -> tuple[int, dict[str, Any]]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    needle = label.casefold()
    for index, info in enumerate(sd.query_devices()):
        item = dict(info)
        if int(item.get("max_input_channels", 0)) < CHANNELS:
            continue
        if needle in str(item.get("name", "")).casefold():
            candidates.append((index, item))
    if len(candidates) != 1:
        found = [f"{index}:{item.get('name')}" for index, item in candidates]
        raise RuntimeError(
            f"expected exactly one {label!r} input with {CHANNELS} channels; "
            f"found {found or 'none'}"
        )
    return candidates[0]


def _compact_speaker_sample(state: dict[str, Any]) -> dict[str, Any]:
    audio = (
        cast(dict[str, Any], state.get("audio"))
        if isinstance(state.get("audio"), dict)
        else {}
    )
    fanin = (
        cast(dict[str, Any], state.get("fanin"))
        if isinstance(state.get("fanin"), dict)
        else {}
    )
    outputd = (
        cast(dict[str, Any], state.get("outputd"))
        if isinstance(state.get("outputd"), dict)
        else {}
    )
    correction_input: dict[str, Any] = {}
    for item in fanin.get("inputs") or []:
        if isinstance(item, dict) and item.get("label") == "correction":
            correction_input = item
            break
    return {
        "t_epoch_s": time.time(),
        "t_monotonic_s": time.monotonic(),
        "utc": _utc_now(),
        "main_volume_db": audio.get("main_volume_db"),
        "playback_rms_dbfs": audio.get("playback_rms_dbfs"),
        "playback_peak_dbfs": audio.get("playback_peak_dbfs"),
        "camilla_clipped_samples": audio.get("clipped_samples"),
        "active_config_path": audio.get("camilla_active_config_path"),
        "correction_input_rms_dbfs": correction_input.get("rms_dbfs"),
        "correction_input_frames_read": correction_input.get("frames_read"),
        "fanin_selection_mode": fanin.get("selection_mode"),
        "fanin_selected_input": fanin.get("selected_input"),
        "outputd_clipped_samples": (
            outputd.get("mix", {}).get("clipped_samples")
            if isinstance(outputd.get("mix"), dict)
            else None
        ),
        "outputd_last_period_clipped_samples": (
            outputd.get("mix", {}).get("last_period_clipped_samples")
            if isinstance(outputd.get("mix"), dict)
            else None
        ),
    }


def _tone_is_active(sample: dict[str, Any]) -> bool:
    level = sample.get("correction_input_rms_dbfs")
    return (
        isinstance(level, (int, float))
        and math.isfinite(float(level))
        and level > -90.0
    )


def _ramp_state(status: dict[str, Any]) -> tuple[bool, str | None]:
    level = status.get("level_match")
    if not isinstance(level, dict):
        return False, None
    last = level.get("last")
    ramp = last.get("ramp") if isinstance(last, dict) else None
    state = ramp.get("state") if isinstance(ramp, dict) else None
    return bool(level.get("running")), str(state) if state else None


def _remote_gain_archive_command(paths: list[str]) -> str:
    """Build one shell-safe command for ssh's remote-shell boundary."""

    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    return f"sudo tar -czf - --ignore-failed-read -- {quoted_paths}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="UMIK-2")
    parser.add_argument("--speaker", default="http://jts3.local")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--tone-frequency-hz", type=float, default=1000.0)
    parser.add_argument("--safe-cap-volume-db", type=float, default=-4.0)
    parser.add_argument("--window-low-dbfs", type=float, default=-20.0)
    parser.add_argument("--pre-window-low-dbfs", type=float, default=-23.75)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--state-only",
        action="store_true",
        help="capture speaker evidence without opening a local audio input",
    )
    parser.add_argument(
        "--ssh-host",
        help="optional SSH target used to archive persisted gain/DSP files during playback",
    )
    args = parser.parse_args()

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    speaker_name = urllib.parse.urlsplit(args.speaker).hostname or "speaker"
    speaker_slug = (
        "".join(
            char if char.isalnum() or char in {"-", "_"} else "-"
            for char in speaker_name
        ).strip("-")
        or "speaker"
    )
    out_dir = args.output_dir or Path("captures") / f"{speaker_slug}-level-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    out_dir.chmod(0o700)
    blocks_path = out_dir / "umik_blocks.jsonl"
    speaker_path = out_dir / "speaker_timeline.jsonl"
    audio_path = out_dir / "umik_raw_2ch_float32.wav"

    if args.state_only:
        device_index, device_info = None, None
    else:
        device_index, device_info = _find_input_device(args.device)
    state_url = f"{args.speaker.rstrip('/')}:8780/state"
    crossover_url = f"{args.speaker.rstrip('/')}/correction/crossover/status"
    stop_event = threading.Event()
    audio_queue: queue.SimpleQueue[tuple[np.ndarray, dict[str, Any], str]] = (
        queue.SimpleQueue()
    )
    frames: list[np.ndarray] = []
    frame_cursor = 0
    sequence = 0
    callback_errors: list[str] = []

    def request_stop(*_: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    def audio_callback(
        indata: np.ndarray,
        frame_count: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        copied = np.array(indata, dtype=np.float32, copy=True)
        timing = {
            "input_buffer_adc_time": float(time_info.inputBufferAdcTime),
            "current_time": float(time_info.currentTime),
            "frame_count": int(frame_count),
        }
        audio_queue.put((copied, timing, str(status)))

    manifest = {
        "schema_version": 1,
        "kind": "jts_correction_level_diagnostic",
        "started_at": _utc_now(),
        "speaker": args.speaker,
        "sample_rate_hz": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_format": "float32",
        "block_frames": BLOCK_FRAMES,
        "clip_threshold": CLIP_THRESHOLD,
        "tone_frequency_hz": args.tone_frequency_hz,
        "safe_cap_volume_db": args.safe_cap_volume_db,
        "window_low_dbfs": args.window_low_dbfs,
        "pre_window_low_dbfs": args.pre_window_low_dbfs,
        "device": (
            None
            if device_info is None
            else {
                "index": device_index,
                "name": device_info.get("name"),
                "hostapi": device_info.get("hostapi"),
                "max_input_channels": device_info.get("max_input_channels"),
                "default_samplerate": device_info.get("default_samplerate"),
                "default_low_input_latency": device_info.get(
                    "default_low_input_latency"
                ),
                "default_high_input_latency": device_info.get(
                    "default_high_input_latency"
                ),
            }
        ),
        "state_only": args.state_only,
        "privacy": "raw room audio; local-only; delete after diagnosis",
    }
    _write_json(out_dir / "manifest.json", manifest)
    device_label = (
        "state-only" if device_info is None else f"{device_index}:{device_info['name']}"
    )
    print(f"ARMING output={out_dir} device={device_label}", flush=True)

    started = time.monotonic()
    next_poll = started
    saw_running = False
    saw_tone = False
    terminal_at: float | None = None
    initial_state: dict[str, Any] | None = None
    initial_crossover: dict[str, Any] | None = None
    active_state_saved = False
    archive_thread: threading.Thread | None = None
    archive_status: dict[str, Any] | None = None
    final_state: dict[str, Any] | None = None
    final_crossover: dict[str, Any] | None = None

    def archive_gain_state(active_config: str | None) -> None:
        nonlocal archive_status
        paths = [
            "/var/lib/jasper/speaker_volume.json",
            "/var/lib/jasper/sound_profile.json",
            "/var/lib/jasper/sound_settings.json",
            "/var/lib/jasper/dsp_apply_state.json",
            "/var/lib/camilladsp/statefile.yml",
            "/var/lib/camilladsp/statefile2.yml",
        ]
        if isinstance(active_config, str) and active_config.startswith("/"):
            paths.append(active_config)
        archive_path = out_dir / "gain_state_during_tone.tar.gz"
        try:
            with archive_path.open("wb") as archive:
                result = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=5",
                        args.ssh_host,
                        _remote_gain_archive_command(paths),
                    ],
                    stdout=archive,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=15.0,
                )
            archive_path.chmod(0o600)
            returncode: int | None = result.returncode
            stderr = result.stderr.decode("utf-8", errors="replace")
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stderr = str(exc)
            timed_out = True
        except OSError as exc:
            returncode = None
            stderr = str(exc)
            timed_out = False
        archive_status = {
            "returncode": returncode,
            "paths": paths,
            "stderr": stderr,
            "timed_out": timed_out,
        }
        _write_json(out_dir / "gain_state_archive_result.json", archive_status)

    try:
        stream = (
            contextlib.nullcontext()
            if args.state_only
            else sd.InputStream(
                device=device_index,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=BLOCK_FRAMES,
                callback=audio_callback,
            )
        )
        with stream:
            print(
                "READY speaker-state capture is active"
                if args.state_only
                else "READY raw UMIK recording is active",
                flush=True,
            )
            while (
                not stop_event.is_set() and time.monotonic() - started < args.duration
            ):
                while not audio_queue.empty():
                    block, timing, callback_status = audio_queue.get()
                    start_frame = frame_cursor
                    frame_cursor += len(block)
                    sequence += 1
                    frames.append(block)
                    abs_block = np.abs(block)
                    channel_rms = np.sqrt(np.mean(np.square(block), axis=0))
                    channel_peak = np.max(abs_block, axis=0)
                    row = {
                        "seq": sequence,
                        "utc": _utc_now(),
                        "t_epoch_s": time.time(),
                        "t_monotonic_s": time.monotonic(),
                        "frame_start": start_frame,
                        "frame_end": frame_cursor,
                        "rms_dbfs": [_dbfs(float(value)) for value in channel_rms],
                        "peak_dbfs": [_dbfs(float(value)) for value in channel_peak],
                        "clip_samples": [
                            int(np.count_nonzero(abs_block[:, index] >= CLIP_THRESHOLD))
                            for index in range(CHANNELS)
                        ],
                        "callback_status": callback_status,
                        **timing,
                    }
                    _append_jsonl(blocks_path, row)
                    if callback_status:
                        callback_errors.append(callback_status)

                now = time.monotonic()
                if now >= next_poll:
                    next_poll = now + 0.2
                    try:
                        state = _fetch_json(state_url)
                        compact = _compact_speaker_sample(state)
                        _append_jsonl(speaker_path, compact)
                        if initial_state is None:
                            initial_state = state
                            _write_json(out_dir / "speaker_state_before.json", state)
                        if _tone_is_active(compact):
                            saw_tone = True
                            if not active_state_saved:
                                active_state_saved = True
                                _write_json(
                                    out_dir / "speaker_state_during_tone.json", state
                                )
                                if args.ssh_host:
                                    active_config = compact.get("active_config_path")
                                    archive_thread = threading.Thread(
                                        target=archive_gain_state,
                                        args=(
                                            active_config
                                            if isinstance(active_config, str)
                                            else None,
                                        ),
                                        name="gain-state-archive",
                                        daemon=True,
                                    )
                                    archive_thread.start()
                                print(
                                    "TONE_DETECTED saved full speaker state", flush=True
                                )
                        final_state = state
                    except (OSError, ValueError, urllib.error.URLError) as exc:
                        _append_jsonl(
                            speaker_path,
                            {"utc": _utc_now(), "error": f"state fetch: {exc}"},
                        )
                    try:
                        crossover = _fetch_json(crossover_url)
                        if initial_crossover is None:
                            initial_crossover = crossover
                            _write_json(
                                out_dir / "crossover_status_before.json", crossover
                            )
                        running, ramp_state = _ramp_state(crossover)
                        saw_running = saw_running or running
                        final_crossover = crossover
                        if saw_running and not running and ramp_state:
                            if terminal_at is None:
                                terminal_at = time.monotonic()
                                print(f"RAMP_TERMINAL state={ramp_state}", flush=True)
                            elif time.monotonic() - terminal_at >= 3.0:
                                stop_event.set()
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
                time.sleep(0.01)
    finally:
        if archive_thread is not None:
            archive_thread.join(timeout=20.0)
        while not audio_queue.empty():
            block, timing, callback_status = audio_queue.get()
            frames.append(block)
        if frames:
            wavfile.write(audio_path, SAMPLE_RATE, np.concatenate(frames, axis=0))
            audio_path.chmod(0o600)
        if final_state is not None:
            _write_json(out_dir / "speaker_state_after.json", final_state)
        if final_crossover is not None:
            _write_json(out_dir / "crossover_status_after.json", final_crossover)
        manifest.update(
            {
                "finished_at": _utc_now(),
                "duration_s": time.monotonic() - started,
                "frame_count": sum(len(block) for block in frames),
                "saw_ramp_running": saw_running,
                "saw_tone": saw_tone,
                "callback_errors": callback_errors,
                "gain_state_archive": archive_status,
                "files": {
                    "audio": audio_path.name if frames else None,
                    "audio_blocks": blocks_path.name,
                    "speaker_timeline": speaker_path.name,
                },
            }
        )
        _write_json(out_dir / "manifest.json", manifest)
        for path in out_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
    print(
        f"SAVED output={out_dir} frames={manifest['frame_count']} "
        f"tone={saw_tone} callback_errors={len(callback_errors)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
