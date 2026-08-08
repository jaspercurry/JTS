# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for fan-in, outputd, and their runtime coupling."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ... import ring_assets
from ...audio_hardware.dac import latency_floor_for
from ...audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from ...camilla_config_contract import read_camilla_device_field
from ...env_load import parse_env_file
from ...fanin_coupling import RING_SLOT_FRAMES
from ._registry import doctor_check
from ._shared import CheckResult, _active_audio_dac_id, _run
from .correction import _active_camilla_config_path

if TYPE_CHECKING:
    from ...audio_runtime_plan import AudioRuntimePlan

# Asset paths are shared with the coupling reconciler. Historical private names
# remain available for focused tests and downstream imports.
_JTS_RING_ALSA_PLUGIN_DIR = ring_assets.RING_ALSA_PLUGIN_DIR
_JTS_RING_IOPLUG_SO = ring_assets.RING_IOPLUG_SO
_JTS_RING_CONF_D = ring_assets.RING_CONF_D
_JTS_RING_SHM_DIR = ring_assets.RING_SHM_DIR
_JTS_RING_PCMS = (
    ("jts_ring_capture", "arecord", "program.ring"),
    ("jts_ring_playback", "aplay", "content.ring"),
)

@doctor_check(order=49, group="audio")
def check_fanin_binary_installed() -> CheckResult:
    """The jasper-fanin Rust daemon ships as an installed binary at
    /opt/jasper/bin/jasper-fanin. install.sh runs cargo build during
    deploy; this check verifies the build actually produced the
    binary. A missing binary means cargo build silently failed and
    renderer audio cannot run.

    See docs/HANDOFF-fan-in-daemon.md for the design.
    """
    path = Path("/opt/jasper/bin/jasper-fanin")
    if not path.exists():
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} missing. Re-run install.sh; check cargo build "
            f"output for compilation errors.",
        )
    if not os.access(path, os.X_OK):
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} present but not executable. Run: "
            f"sudo chmod +x {path}",
        )
    try:
        size_kb = path.stat().st_size // 1024
    except OSError:
        size_kb = 0
    return CheckResult(
        "jasper-fanin binary", "ok", f"{path} ({size_kb} KB)"
    )

def _asound_non_comment_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )

def _asound_pcm_block(text: str, name: str) -> str | None:
    """Return a top-level pcm.NAME block body from an asoundrc.

    The deployed ALSA snippets keep each top-level PCM block separated
    by the next `pcm.` or `ctl.` definition. We do not need a full ALSA
    parser here; this is a drift detector for our own generated file.
    """
    pattern = re.compile(rf"^pcm\.{re.escape(name)}\s*\{{", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    tail = text[match.start():]
    next_def = re.search(r"^(?:pcm|ctl)\.", tail[match.end() - match.start():], re.MULTILINE)
    if next_def:
        return tail[:match.end() - match.start() + next_def.start()]
    return tail

_FANIN_EXPECTED_INPUTS = [
    ("spotify", "hw:Loopback,1,0"),
    ("airplay", "hw:Loopback,1,1"),
    ("bluealsa", "hw:Loopback,1,2"),
    ("usbsink", "hw:Loopback,1,3"),
    ("correction", "hw:Loopback,1,4"),
]

_FANIN_EXPECTED_OUTPUT_PCM = "hw:Loopback,0,7"

_OUTPUTD_EXPECTED_CONTENT_PCM = "outputd_content_capture"

_OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM = "outputd_active_content_capture"

_OUTPUTD_EXPECTED_DAC_PCM = "outputd_dac"

_OUTPUTD_EXPECTED_DUAL_DAC_PCM = "dual_apple_usb_c_dac_4ch"

_OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"

_FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"

_OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"

_STATUS_RESPONSE_MAX_BYTES = 1_048_576


def _read_status_socket_bytes(socket_path: str, *, timeout: float) -> bytes:
    """Return the raw reply from a local JTS ``STATUS\n`` control socket.

    This helper owns only the shared socket lifecycle.  Callers deliberately
    retain their own retry, UTF-8/JSON parsing, and fail-versus-skip policy.
    """

    deadline = time.monotonic() + timeout

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        def set_remaining_timeout() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("STATUS response deadline exceeded")
            sock.settimeout(remaining)

        set_remaining_timeout()
        sock.connect(socket_path)
        set_remaining_timeout()
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        received = 0
        while True:
            set_remaining_timeout()
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += len(chunk)
            if received > _STATUS_RESPONSE_MAX_BYTES:
                raise OSError("STATUS response exceeds byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _read_status_socket(socket_path: str) -> dict[str, object]:
    payload = _read_status_socket_bytes(socket_path, timeout=1.0).decode("utf-8")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("STATUS response root is not an object")
    return parsed


def _route_live_state_issues_for_doctor(
    plan: AudioRuntimePlan,
    *,
    negotiated_buffer_frames: int | None = None,
) -> tuple[str, ...]:
    from jasper.audio_validation import route_live_state_issues

    identity = plan.route_latency_identity()
    if negotiated_buffer_frames is not None:
        direct = identity.get("fanin_direct_config")
        if isinstance(direct, dict):
            identity["fanin_direct_config"] = {
                **direct,
                "negotiated_buffer_frames": negotiated_buffer_frames,
            }
    issues: list[str] = []
    fanin_status: dict[str, object] | None = None

    try:
        fanin_status = _read_status_socket(_FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        issues.append(f"live_fanin_status_unreadable:{type(e).__name__}")

    return tuple(
        dict.fromkeys(
            (
                *issues,
                *route_live_state_issues(
                    identity,
                    fanin_status=fanin_status,
                    # A valid promotion artifact certifies the installed route,
                    # not a promise that an idle USB host keeps the activity-
                    # dependent resampler lock asserted forever. fan-in's
                    # direct.health is the canonical idle/capturing/broken
                    # classifier; the shared helper remains fail-closed unless
                    # that field says exactly "idle". The artifact writer does
                    # not opt in and therefore stays strict mid-stream.
                    allow_idle_direct_lane=True,
                ),
            )
        )
    )


def _outputd_reconciled_env() -> dict[str, str]:
    return parse_env_file(
        os.environ.get("JASPER_OUTPUTD_ENV_FILE") or _OUTPUTD_ENV_PATH
    )


def _outputd_active_channels_from_env(env: dict[str, str]) -> int | None:
    raw = str(env.get("JASPER_OUTPUTD_ACTIVE_CHANNELS") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 2 <= value <= 8 else None


# outputd STATUS reports, per content/dac section, an all-time xrun_count plus
# two rolling fields the daemon already computes: xrun_rate_per_hour (count /
# uptime-hours) and last_xrun_age_ms (ms since the most recent xrun, null when
# none). The doctor WARN keys on BOTH so it flags a *sustained, current*
# problem — a high rate alone could be a long-ago burst diluting slowly as
# uptime grows, and a recent single xrun alone is a normal transient. Only a
# rate that's still meaningfully high AND a recent xrun is worth a yellow line.
_OUTPUTD_XRUN_RATE_WARN_PER_HOUR = 6.0
_OUTPUTD_XRUN_RECENT_AGE_MS = 300_000  # 5 minutes


def _outputd_xrun_rate_warning(
    content: dict[str, object],
    dac: dict[str, object],
) -> str | None:
    """Return a one-clause WARN reason when either outputd lane shows a
    sustained xrun rate with a recent xrun, else None.

    Keyed on the daemon-computed ``xrun_rate_per_hour`` and ``last_xrun_age_ms``
    so a burst that has since cleared (recent-but-low-rate, or high-rate but
    stale) does NOT warn. Both sections are checked independently; the worst
    qualifying lane is reported.
    """

    def _f(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    worst: tuple[float, str] | None = None
    for label, section in (("content", content), ("dac", dac)):
        if not isinstance(section, dict):
            continue
        rate = _f(section.get("xrun_rate_per_hour"))
        age = _f(section.get("last_xrun_age_ms"))  # null → None → no recent xrun
        if rate is None or age is None:
            continue
        if rate >= _OUTPUTD_XRUN_RATE_WARN_PER_HOUR and age <= _OUTPUTD_XRUN_RECENT_AGE_MS:
            reason = (
                f"{label} xrun_rate_per_hour={rate:.1f} "
                f"(last_xrun_age_ms={int(age)})"
            )
            if worst is None or rate > worst[0]:
                worst = (rate, reason)
    return worst[1] if worst else None


@doctor_check(order=50, group="audio")
def check_fanin_asound_wiring() -> CheckResult:
    """Verify the deployed ALSA graph is the fan-in graph.

    This catches the exact split-brain failure that can break AEC:
    renderers and jasper-fanin are running in fan-in mode, but
    /etc/asound.conf still points `pcm.jasper_capture` at the old
    substream 0 instead of the summed fan-in output on substream 7.
    """
    label = "fan-in ALSA wiring"
    path = Path("/etc/asound.conf")
    if not path.exists():
        return CheckResult(label, "fail", f"{path} missing — re-run install.sh")
    try:
        text = path.read_text()
    except OSError as e:
        return CheckResult(label, "fail", f"can't read {path}: {e}")

    active = _asound_non_comment_text(text)
    legacy_blocks = [
        name for name in ("jasper_renderer_mix", "jasper_renderer_in")
        if re.search(rf"^pcm\.{name}\s*\{{", active, re.MULTILINE)
    ]
    if legacy_blocks:
        return CheckResult(
            label,
            "fail",
            f"{path} still defines legacy renderer dmix block(s): "
            f"{', '.join(legacy_blocks)}. Fan-in-only installs must "
            f"define private renderer lanes and no jasper_renderer_* "
            f"front end. Re-run deploy/install.sh.",
        )

    # No usbsink_substream write alias: USB audio is DIRECT-captured by jasper-fanin
    # from hw:UAC2Gadget (the aloop solo bridge that wrote hw:Loopback,0,3 was
    # removed 2026-07-10). fan-in still READS the pair-3 capture side as the usbsink
    # lane's idle fallback — see _FANIN_EXPECTED_INPUTS above — but nothing writes it.
    expected_aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
        CORRECTION_SUBSTREAM: "hw:Loopback,0,4",
    }
    missing: list[str] = []
    wrong: list[str] = []
    for alias, slave in expected_aliases.items():
        block = _asound_pcm_block(active, alias)
        if block is None:
            missing.append(alias)
        elif (
            f'pcm "{slave}"' not in block
            or "rate 48000" not in block
            or "channels 2" not in block
            or "format S16_LE" not in block
        ):
            wrong.append(f"{alias}≠{slave}")
    if missing or wrong:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if wrong:
            parts.append("wrong slave " + ", ".join(wrong))
        return CheckResult(
            label,
            "fail",
            "; ".join(parts) + ". Re-run deploy/install.sh to restore "
            "the fan-in asoundrc.",
        )

    capture = _asound_pcm_block(active, "jasper_capture")
    if capture is None:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_capture missing — CamillaDSP and AEC bridge "
            "have no shared reference tap.",
        )
    if 'pcm "hw:Loopback,1,7"' not in capture:
        detail = (
            "pcm.jasper_capture must dsnoop hw:Loopback,1,7 "
            "(jasper-fanin's summed output)."
        )
        if 'pcm "hw:Loopback,1,0"' in capture:
            detail += (
                " It currently points at substream 0, which is now a "
                "private fan-in input lane and can make jasper_ref fail "
                "with EBUSY."
            )
        return CheckResult(label, "fail", detail)
    for required in ("rate 48000", "channels 2", "format S16_LE"):
        if required not in capture:
            return CheckResult(
                label,
                "fail",
                "pcm.jasper_capture must pin the dsnoop slave to "
                f"48 kHz stereo S16_LE; missing {required!r}.",
            )

    ref = _asound_pcm_block(active, "jasper_ref")
    if ref is None:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref missing — AEC bridge opens jasper_ref, not "
            "jasper_capture directly.",
        )
    if 'slave.pcm "jasper_capture"' not in ref:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref must plug-wrap pcm.jasper_capture so the AEC "
            "bridge reads the summed fan-in reference.",
        )

    stale_state = Path("/var/lib/jasper/audio_topology.env")
    if stale_state.exists():
        return CheckResult(
            label,
            "warn",
            f"fan-in asoundrc is correct, but stale {stale_state} still "
            f"exists from the retired dmix/fanin switcher. Re-run "
            f"deploy/install.sh to archive/remove it.",
        )

    return CheckResult(
        label,
        "ok",
        "renderer/test lanes 0..4; jasper_capture/jasper_ref on summed substream 7",
    )

@doctor_check(order=51, group="audio")
def check_fanin_service() -> CheckResult:
    """The jasper-fanin systemd unit is required for renderer audio.

    Fan-in is the only supported renderer topology. If the daemon is
    disabled or inactive, AirPlay/Spotify/Bluetooth/USB-in may write to
    their private lanes, but nothing publishes the summed stream to
    CamillaDSP or the AEC bridge.

    Returns:
      - ok ("active, responding") when enabled and the UDS endpoint
        replies to STATUS with a fresh progress sentinel.
      - fail when disabled/inactive, when STATUS cannot be read, or
        when the live STATUS schema drifts from the production graph.
      - warn when enabled+active but the work loop is stale.
    """
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-fanin.service"]
    ).stdout.strip()
    active = _run(
        ["systemctl", "is-active", "jasper-fanin.service"]
    ).stdout.strip()

    if enabled in ("disabled", "static", "indirect"):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"state={enabled}. Fan-in is mandatory; run: "
            f"sudo systemctl enable --now jasper-fanin.service",
        )
    if enabled == "not-found":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "systemd unit not installed. Re-run install.sh.",
        )

    # Unit is enabled (or masked, alias, ...) — operator opted in.
    if active != "active":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"enabled but state={active}. "
            f"Check: journalctl -u jasper-fanin",
        )

    # Service is active. Probe the UDS endpoint to verify the work
    # loop is making progress (catches "process alive but wedged").
    socket_path = "/run/jasper-fanin/control.sock"
    last_error: OSError | None = None
    for attempt in range(2):
        try:
            payload = _read_status_socket_bytes(socket_path, timeout=2.0)
            break
        except OSError as e:
            last_error = e
            if attempt == 0:
                time.sleep(0.1)
    else:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS probe at {socket_path} failed: {last_error}. "
            f"Fan-in is mandatory; without STATUS doctor cannot verify "
            f"the live graph, buffers, or watchdog progress. "
            f"check: journalctl -u jasper-fanin | tail",
        )

    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS returned invalid JSON: {e}",
        )
    if not isinstance(data, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS root is {type(data).__name__}, expected object",
        )

    output = data.get("output", {})
    if not isinstance(output, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing output{}",
        )
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import COUPLING_SHM_RING

    coupling = read_persisted_coupling()
    # The fan-in STATUS echoes its live coupling transport (state.rs
    # push_kv_str("transport", ...)): "loopback" (default) or "shm_ring" (Ring A —
    # with a "ring" observability block). Expect the STATUS to match the persisted
    # intent so a coupling that failed to restart onto the box is caught. shm_ring
    # keeps a lossy aloop MIRROR on lane 7, so output.pcm still reports
    # hw:Loopback,0,7 (checked below, unchanged); only the transport string and the
    # ring block differ from loopback.
    expected_transport = {
        COUPLING_SHM_RING: "shm_ring",
    }.get(coupling, "loopback")
    actual_transport = output.get("transport")
    if actual_transport != expected_transport:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.transport={actual_transport!r}; "
            f"expected {expected_transport!r} for persisted coupling={coupling!r}. "
            "Run jasper-fanin-coupling-reconcile to restart fan-in onto the "
            "persisted topology.",
        )
    if coupling == COUPLING_SHM_RING:
        ring = output.get("ring")
        if not isinstance(ring, dict):
            return CheckResult(
                "jasper-fanin service",
                "fail",
                "active but shm_ring STATUS is missing output.ring metrics — "
                "fan-in is not actually writing Ring A. Run "
                "jasper-fanin-coupling-reconcile shm_ring.",
            )

    output_pcm = output.get("pcm")
    # Both loopback and shm_ring (via its lane-7 aloop mirror) report
    # hw:Loopback,0,7 as output.pcm.
    if output_pcm != _FANIN_EXPECTED_OUTPUT_PCM:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.pcm={output_pcm!r}; expected "
            f"{_FANIN_EXPECTED_OUTPUT_PCM}. Check /var/lib/jasper/fanin.env.",
        )
    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing inputs[]",
        )
    actual_inputs = [
        (inp.get("label"), inp.get("pcm"))
        for inp in inputs
        if isinstance(inp, dict)
    ]
    if actual_inputs != _FANIN_EXPECTED_INPUTS:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS inputs drifted. Expected "
            f"{_FANIN_EXPECTED_INPUTS!r}; got {actual_inputs!r}. "
            "Check /var/lib/jasper/fanin.env.",
        )

    progress_age = data.get("watchdog", {}).get(
        "last_progress_age_ms", -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing watchdog state",
        )
    if progress_age > 1000:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            f"(work loop may be wedged; watchdog should fire soon)",
        )
    frames = output.get("frames_written", 0)
    xruns = output.get("xrun_count", 0)
    input_buffer_frames = data.get("input_buffer_frames")
    output_buffer_frames = output.get("buffer_frames")
    if not isinstance(input_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer input_buffer_frames",
        )
    if not isinstance(output_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer output.buffer_frames",
        )
    input_xruns = []
    for inp in data.get("inputs", []):
        try:
            count = int(inp.get("xrun_count", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if count:
            input_xruns.append(f"{inp.get('label', '?')}={count}")
    if input_buffer_frames < 4096:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime input_buffer_frames={input_buffer_frames} is below "
            f"4096. AirPlay WiFi burst absorption was validated at 4096; "
            f"check /var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_INPUT_BUFFER_FRAMES.",
        )
    if output_buffer_frames < 1024:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime output_buffer_frames={output_buffer_frames} is below "
            f"1024. The 1024-frame fan-in output queue is the production "
            f"floor validated on the low-latency Camilla path; lower values "
            f"need fresh hardware validation before shipping. Check "
            f"/var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_OUTPUT_BUFFER_FRAMES.",
        )
    if output_buffer_frames > 3072:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active, but runtime output_buffer_frames={output_buffer_frames} exceeds "
            f"3072. Larger fan-in output queues add latency and need a "
            f"fresh AirPlay offset validation before shipping. "
            f"Check /var/lib/jasper/fanin.env and "
            f"JASPER_FANIN_OUTPUT_BUFFER_FRAMES.",
        )
    tts = data.get("tts", {})
    if not isinstance(tts, dict) or not bool(tts.get("enabled", False)):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but pre-DSP TTS socket is not enabled. Current "
            "production topology requires TTS/cues to enter jasper-fanin "
            "before CamillaDSP.",
        )

    tts_detail = "tts_enabled=true"
    loudness = tts.get("assistant_loudness")
    if not isinstance(loudness, dict):
        return CheckResult(
            "jasper-fanin service",
            "warn",
            "active with pre-DSP TTS enabled but STATUS is missing "
            "tts.assistant_loudness telemetry; deploy current jasper-fanin "
            "before evaluating TTS loudness.",
        )
    decision_seen = bool(loudness.get("decision_seen", False))
    calibrated = bool(loudness.get("calibrated", False))
    final_gain = loudness.get("final_gain_db")
    if decision_seen and not isinstance(final_gain, (int, float)):
        return CheckResult(
            "jasper-fanin service",
            "warn",
            "active with pre-DSP TTS enabled but "
            "tts.assistant_loudness.decision_seen=true without "
            "numeric final_gain_db.",
        )
    if isinstance(final_gain, (int, float)) and not -60.0 <= float(final_gain) <= 0.0:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active with pre-DSP TTS enabled but "
            f"tts.assistant_loudness.final_gain_db={final_gain!r}; "
            "expected clamped [-60, 0] dB.",
        )
    tts_detail = (
        f"tts_enabled=true, "
        f"tts_pending_frames={tts.get('pending_frames', 0)}, "
        f"assistant_loudness_decision={decision_seen}, "
        f"assistant_loudness_calibrated={calibrated}, "
        f"assistant_final_gain_db={final_gain}"
    )
    return CheckResult(
        "jasper-fanin service",
        "ok",
        f"active, frames_written={frames}, "
        f"transport={actual_transport}, "
        f"input_buffer_frames={input_buffer_frames}, "
        f"output_buffer_frames={output_buffer_frames}, "
        f"output xruns={xruns}, input xruns={','.join(input_xruns) or '0'}, "
        f"progress_age_ms={progress_age}, "
        f"{tts_detail}",
    )


def _host_clock_health_from_status(data: dict[str, object]) -> CheckResult:
    """Classify fan-in's additive host-clock state without touching hardware."""
    label = "USB host clock"
    host_clock = data.get("host_clock")
    if not isinstance(host_clock, dict):
        return CheckResult(
            label,
            "warn",
            "fan-in STATUS has no host_clock object; deploy current jasper-fanin",
        )
    if host_clock.get("enabled") is not True:
        return CheckResult(label, "ok", "disabled (no host-clock health claim)")

    ladder = str(host_clock.get("ladder") or "unknown")
    reason_value = host_clock.get("fallback_reason")
    reason = str(reason_value) if reason_value is not None else "none"
    actuator = host_clock.get("actuator")
    probe = host_clock.get("probe")
    if not isinstance(actuator, dict):
        return CheckResult(label, "warn", "enabled but actuator telemetry is missing")
    if not isinstance(probe, dict):
        return CheckResult(label, "warn", "enabled but probe telemetry is missing")

    ready = actuator.get("ready") is True
    capture_generation = actuator.get("capture_generation")
    control_generation = actuator.get("control_generation")
    generations_match = (
        isinstance(capture_generation, int)
        and capture_generation > 0
        and control_generation == capture_generation
    )
    counters = (
        f"refreshes={actuator.get('refreshes', '?')}, "
        f"open_failures={actuator.get('open_failures', '?')}, "
        f"write_failures={actuator.get('write_failures', '?')}"
    )

    if not ready or not generations_match:
        return CheckResult(
            label,
            "warn",
            f"actuator unavailable/mismatched: ladder={ladder}, "
            f"fallback_reason={reason}, capture_generation={capture_generation}, "
            f"control_generation={control_generation}, ready={ready}; {counters}. "
            "Audio remains on the direct resampler fallback; check "
            "`journalctl -u jasper-fanin | grep host_clock_control` and the UAC2 gadget.",
        )

    if ladder == "l2_fallback":
        return CheckResult(
            label,
            "warn",
            f"persistent L2 fallback: fallback_reason={reason}, "
            f"capture_generation={capture_generation}, control_generation={control_generation}; "
            f"{counters}. Stop/start creates a new session; a gadget generation "
            "change self-heals automatically.",
        )

    phase = probe.get("phase")
    attempt = probe.get("attempt")
    max_attempts = probe.get("max_attempts")
    if ladder == "probing":
        # Await-lock, baseline, step, and the single retry wait are expected,
        # bounded acquisition states—not permanent doctor failures.
        return CheckResult(
            label,
            "ok",
            f"recovering: phase={phase}, attempt={attempt}/{max_attempts}, "
            f"generations={capture_generation}/{control_generation}; {counters}",
        )

    return CheckResult(
        label,
        "ok",
        f"ladder={ladder}, generations={capture_generation}/{control_generation}, "
        f"probe_final={probe.get('final_result')}, retries={probe.get('retries')}; {counters}",
    )


@doctor_check(order=51.1, group="audio")
def check_camilla_service() -> CheckResult:
    """The jasper-camilla systemd unit must never stay stopped.

    Every source's audio runs through CamillaDSP, so a stopped unit is a
    silent speaker. The peer checks miss a CLEAN stop (#2163):
    `check_service_runtime_state` flags only `failed`, and
    `check_camilla_websocket` reports the same state as "can't reach
    127.0.0.1:1234", which reads as a websocket problem rather than a
    stopped unit. That state is reachable — `jasper-camilla-recover` parks
    the unit stopped after an exhausted start-limit burst, and
    `OnFailure=jasper-camilla-recover` does not fire on a clean exit.

    "Enabled but not active" is unambiguous here because CamillaDSP has no
    gate that makes `inactive` legitimate, unlike jasper-outputd (missing-DAC
    `ExecCondition`) or jasper-voice (`voice-input-absent` marker).

    Returns:
      - ok when enabled and active.
      - fail when the unit is missing, disabled, or enabled and not active.
    """
    label = "jasper-camilla service"
    unit = "jasper-camilla.service"
    enabled = _run(["systemctl", "is-enabled", unit]).stdout.strip()
    active = _run(["systemctl", "is-active", unit]).stdout.strip()

    if enabled == "not-found":
        return CheckResult(
            label, "fail", "systemd unit not installed. Re-run install.sh."
        )
    if enabled in ("disabled", "static", "indirect"):
        return CheckResult(
            label,
            "fail",
            f"state={enabled}. CamillaDSP is mandatory; run: "
            f"sudo systemctl enable --now {unit}",
        )
    if active != "active":
        return CheckResult(
            label,
            "fail",
            f"enabled but state={active}. Every source's audio runs through "
            f"CamillaDSP, so nothing will play until it starts. Check: "
            f"journalctl -u jasper-camilla -u jasper-camilla-recover",
        )
    return CheckResult(label, "ok", "enabled and active")


@doctor_check(order=51.52, group="audio")
def check_fanin_host_clock() -> CheckResult:
    """Report persistent USB host-clock recovery/fallback with exact cause."""
    try:
        data = _read_status_socket(_FANIN_STATUS_SOCKET)
    except (OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        # Reachability belongs to the preceding mandatory fan-in service check.
        return CheckResult(
            "USB host clock",
            "ok",
            f"not probed ({type(e).__name__}); see jasper-fanin service check",
        )
    return _host_clock_health_from_status(data)

@doctor_check(order=51.5, group="audio")
def check_fanin_tts_drops() -> CheckResult:
    """Dropped TTS audio at fan-in's pending budget means garbled replies.

    fan-in's TTS lane drops whole audio commands that arrive while its
    bounded pending queue is full (it cannot block the socket reader
    without stalling barge-in FLUSH behind queued audio). The Python
    writer paces itself to stay under that budget
    (`_OUTPUTD_PACE_AHEAD_SEC` in jasper/audio_io.py), so a nonzero drop
    counter means assistant/cue audio audibly skipped ("fast-forward"
    garble) since fan-in last started — either the writer-side pacing
    contract regressed or an unpaced client wrote to the TTS socket.
    The 2026-06-11 JTS3 incident surfaced exactly this signature.

    Returns:
      - ok when counters are zero, the TTS lane is disabled, or STATUS
        is unreachable (reachability is owned by 'jasper-fanin service').
      - warn when dropped audio commands/frames > 0 since fan-in start.
    """
    name = "fan-in TTS drops"
    socket_path = "/run/jasper-fanin/control.sock"
    try:
        payload = _read_status_socket_bytes(socket_path, timeout=2.0)
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError(
                f"STATUS response root is {type(data).__name__}, not object"
            )
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return CheckResult(
            name,
            "ok",
            f"not probed ({type(e).__name__}); fan-in reachability is "
            "covered by the 'jasper-fanin service' check",
        )

    tts = data.get("tts")
    if not isinstance(tts, dict) or not tts.get("enabled"):
        return CheckResult(name, "ok", "TTS lane disabled in this topology")

    dropped_frames = int(tts.get("dropped_audio_frames") or 0)
    dropped_commands = int(tts.get("dropped_commands") or 0)
    if dropped_frames == 0 and dropped_commands == 0:
        return CheckResult(
            name,
            "ok",
            f"none since fan-in start (pending_frames="
            f"{tts.get('pending_frames')}, budget_frames="
            f"{tts.get('budget_frames')})",
        )

    sample_rate = int(data.get("output", {}).get("sample_rate") or 48_000)
    dropped_sec = dropped_frames / float(sample_rate)
    return CheckResult(
        name,
        "warn",
        f"{dropped_commands} audio command(s) / ~{dropped_sec:.1f}s of "
        "TTS audio dropped at the pending budget since fan-in start — "
        "assistant replies were audibly garbled/fast-forwarded. Check "
        "`journalctl -u jasper-fanin | grep tts_command_dropped` and the "
        "voice daemon's `paced` turn accounting; an unpaced writer or a "
        "pacing regression is the usual cause.",
    )


@doctor_check(order=51.55, group="audio")
def check_fanin_ring_stall() -> CheckResult:
    """A live fan-in→CamillaDSP ring stall (issue #1524) means the SHM ring is
    full AND CamillaDSP is not draining it (heartbeat-live but ``read_seq``
    frozen, e.g. wedged in Prepared — or the reader has been absent > 1 s). The
    writer self-recovers by DEMOTING the stuck reader to free-run (so fan-in stays
    real time instead of running at ~1/9 speed and wedging the correction-lane
    aplay), but content is being dropped and the DSP is not consuming — the
    household hears no music through the ring. ``stall_active`` is true for exactly
    as long as the reader stays stuck, so it is the live/sustained signal;
    ``stuck_reader_drops`` climbs alongside it.

    Skip-if-loopback: only the ``shm_ring`` coupling has a ring; the default
    ``loopback`` coupling is timer-paced and structurally immune, so this reports
    OK there (no ring block in STATUS). Reachability is owned by the
    'jasper-fanin service' check — an unreadable STATUS reports OK here rather than
    double-failing a down daemon.

    Returns:
      - ok when loopback, when the ring is draining normally, or STATUS unreachable
      - warn when a stall episode is CURRENTLY active, surfacing the stuck-reader
        vs no-reader drop split and the last stall duration.
    """
    name = "fan-in ring stall"
    try:
        payload = _read_status_socket_bytes(_FANIN_STATUS_SOCKET, timeout=2.0)
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError(f"STATUS root is {type(data).__name__}, not object")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return CheckResult(
            name,
            "ok",
            f"not probed ({type(e).__name__}); fan-in reachability is "
            "covered by the 'jasper-fanin service' check",
        )

    output = data.get("output")
    ring = output.get("ring") if isinstance(output, dict) else None
    if not isinstance(ring, dict):
        # loopback (or any non-ring topology): no ring, nothing can stall.
        return CheckResult(
            name,
            "ok",
            "skipped — loopback coupling (no shm_ring ring; timer-paced, "
            "stall-immune)",
        )

    stuck = int(ring.get("stuck_reader_drops") or 0)
    no_reader = int(ring.get("drop_no_reader") or 0)
    last_ms = int(ring.get("last_stall_ms") or 0)
    counts = (
        f"stuck_reader_drops={stuck}, drop_no_reader={no_reader}, "
        f"last_stall_ms={last_ms}"
    )
    if bool(ring.get("stall_active")):
        return CheckResult(
            name,
            "warn",
            f"a ring stall is CURRENTLY active — CamillaDSP is not draining the "
            f"fan-in ring; fan-in demoted to free-run to stay real-time but ring "
            f"content is dropping ({counts}). Check `journalctl -u jasper-fanin "
            f"| grep event=fanin.ring.stall` and `systemctl status "
            f"jasper-camilla`.",
        )
    return CheckResult(name, "ok", f"no active stall ({counts})")


def _loaded_device_field(config_path: Path, block: str, field: str) -> str | None:
    """A field from ``devices.<block>`` in a CamillaDSP config, or None.

    Thin path-typed wrapper over the shared scan in
    ``jasper.camilla_config_contract.read_camilla_device_field`` (the SSOT it
    shares with the installer's deploy-ordering probe).
    """
    return read_camilla_device_field(config_path, block, field)


def _loaded_capture_type(config_path: Path) -> str | None:
    """The ``devices.capture.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "capture", "type")


def _loaded_playback_type(config_path: Path) -> str | None:
    """The ``devices.playback.type`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "type")


def _loaded_playback_filename(config_path: Path) -> str | None:
    """The ``devices.playback.filename`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "filename")


def _loaded_playback_format(config_path: Path) -> str | None:
    """The ``devices.playback.format`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "format")


@doctor_check(order=51.75, group="audio")
def check_camilla_playback_format() -> CheckResult:
    """The loaded CamillaDSP config's declared playback format must match its
    LANE's expected format (wide-output-path program, D-list survey finding
    1): today NOTHING read the emitted playback format back off a live
    config, so a half-flip — an emitter regenerating a config against one
    value of the expected constant while the running process (or an
    un-reconciled stale file) reflects another — was silent rather than a red
    doctor line.

    Lane-aware via the sibling ``_loaded_playback_type`` helper: a ``File``
    sink (the bonded-leader pipe, or the active-speaker parked graph's
    ``/dev/null``) expects ``DEFAULT_PIPE_SINK_FORMAT`` — pinned narrow
    (D4) regardless of the general program lane; every other sink (the ALSA
    loopback lane, the SHM ring, every real active-speaker DAC graph)
    expects ``DEFAULT_PLAYBACK_FORMAT``. Without this split, the check would
    red-line every healthy pipe-sink leader and parked box now that
    ``DEFAULT_PLAYBACK_FORMAT`` is wide (PR-6) — the exact lanes the
    wide-output-path program pins narrow — with a remediation string that
    tells the operator to regenerate a config that is already correct. The two
    constants now genuinely differ (ALSA lane ``S32_LE``, pipe/File sink
    ``S16_LE``), so the split is load-bearing rather than latent.
    See ``captures/PLAN-wide-output-path-2026-08-07.md`` PR-1 and PR-6.
    """
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )

    label = "camilla playback format"
    _, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(label, "ok", "no loaded config to compare")
    path = Path(config_path)
    loaded_format = _loaded_playback_format(path)
    if loaded_format is None:
        return CheckResult(
            label, "ok", f"{path} has no devices.playback.format field"
        )
    playback_type = _loaded_playback_type(path)
    expected_format = (
        DEFAULT_PIPE_SINK_FORMAT if playback_type == "File" else DEFAULT_PLAYBACK_FORMAT
    )
    expected_name = (
        "DEFAULT_PIPE_SINK_FORMAT" if playback_type == "File" else "DEFAULT_PLAYBACK_FORMAT"
    )
    if loaded_format == expected_format:
        return CheckResult(
            label, "ok", f"playback format={loaded_format} (type={playback_type})"
        )
    return CheckResult(
        label,
        "fail",
        f"loaded CamillaDSP playback format={loaded_format!r} for playback "
        f"type={playback_type!r}, expected {expected_format!r} "
        f"({expected_name}) — a half-flipped box: {path} was generated "
        f"against a different {expected_name} than the one currently in "
        "force. Regenerate the config (sudo /opt/jasper/.venv/bin/jasper-sound "
        "reconcile-current-dsp) or investigate why the constant and the "
        "loaded config disagree.",
    )


@doctor_check(order=51.6, group="audio")
def check_audio_runtime_plan() -> CheckResult:
    """Explainable SSOT check for audio latency/coupling knobs."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    summary = (
        f"profile={plan.profile_id}, route={plan.route_mode}, "
        f"route_profile={plan.route_profile.route_id}, "
        f"route_hash={plan.route_config_hash}, "
        f"coupling={plan.setting('JASPER_FANIN_CAMILLA_COUPLING').value}, "
        f"camilla={plan.setting('JASPER_CAMILLA_CHUNKSIZE').value}/"
        f"{plan.setting('JASPER_CAMILLA_TARGET_LEVEL').value}, "
        f"outputd={plan.setting('JASPER_OUTPUTD_PERIOD_FRAMES').value}/"
        f"{plan.setting('JASPER_OUTPUTD_DAC_BUFFER_FRAMES').value}, "
        f"content_buffer={plan.setting('JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES').value}, "
        f"fanin={plan.setting('JASPER_FANIN_INPUT_BUFFER_FRAMES').value}/"
        f"{plan.setting('JASPER_FANIN_OUTPUT_BUFFER_FRAMES').value}"
    )
    if plan.errors:
        return CheckResult(
            "audio runtime plan",
            "fail",
            summary + "; " + "; ".join(plan.errors),
        )
    if plan.warnings:
        return CheckResult(
            "audio runtime plan",
            "warn",
            summary + "; " + "; ".join(plan.warnings[:3]),
        )
    return CheckResult("audio runtime plan", "ok", summary)


@doctor_check(order=51.65, group="audio")
def check_route_latency_evidence() -> CheckResult:
    """Low-latency route claims require a fresh measured artifact."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system
    from jasper.audio_validation import (
        ROUTE_LATENCY_MIC_ID,
        ROUTE_LATENCY_P95_BUDGET_MS,
        ROUTE_LATENCY_PROFILE,
        ROUTE_LATENCY_STALE_AFTER,
        artifact_directory,
        assess_route_latency_artifact,
        load_latest_artifact,
    )

    plan = build_audio_runtime_plan_from_system()
    route = plan.route_profile
    if not route.low_latency_claim:
        return CheckResult(
            "route latency evidence",
            "ok",
            f"route_profile={route.route_id} has no low-latency claim",
        )
    if plan.errors:
        return CheckResult(
            "route latency evidence",
            "fail",
            f"route_profile={route.route_id}, route_hash={plan.route_config_hash}, "
            "runtime plan errors block latency certification: "
            + "; ".join(plan.errors),
        )

    dac_id = None if plan.profile_id == "unknown" else plan.profile_id
    result = load_latest_artifact(
        artifact_directory(),
        mic_id=ROUTE_LATENCY_MIC_ID,
        dac_id=dac_id,
        profile=ROUTE_LATENCY_PROFILE,
        max_age=ROUTE_LATENCY_STALE_AFTER,
    )
    summary = assess_route_latency_artifact(
        result,
        route_config_hash=plan.route_config_hash,
        expected_identity=plan.route_latency_identity(),
    )
    detail = (
        f"route_profile={route.route_id}, route_hash={plan.route_config_hash}, "
        f"artifact_status={summary.get('status')}, state={summary.get('state')}, "
        f"p95_ms={summary.get('p95_ms')}, p99_ms={summary.get('p99_ms')}, "
        f"samples={summary.get('sample_count')}, "
        f"duration_seconds={summary.get('duration_seconds')}, "
        f"certified={summary.get('certified_percentiles')}, "
        f"config_match={summary.get('config_match')}, "
        f"issues={summary.get('issues')}, "
        f"artifact={summary.get('artifact_path')}"
    )
    status = str(summary.get("status") or "fail")
    if status in {"pass", "warn"}:
        negotiated_buffer_frames: int | None = None
        if result.artifact is not None:
            artifact_identity = result.artifact.checks.get("identity")
            if isinstance(artifact_identity, dict):
                raw_buffer = artifact_identity.get(
                    "fanin_direct_negotiated_buffer_frames"
                )
                if isinstance(raw_buffer, (int, float, str)) and not isinstance(
                    raw_buffer, bool
                ):
                    try:
                        parsed_buffer = int(raw_buffer)
                    except (TypeError, ValueError):
                        parsed_buffer = 0
                    if parsed_buffer > 0:
                        negotiated_buffer_frames = parsed_buffer
        live_issues = _route_live_state_issues_for_doctor(
            plan,
            negotiated_buffer_frames=negotiated_buffer_frames,
        )
        if live_issues:
            status = "fail"
            detail += f", live_issues={list(live_issues)}"
    if status == "pass":
        return CheckResult("route latency evidence", "ok", detail)
    if status == "warn":
        return CheckResult("route latency evidence", "warn", detail)
    return CheckResult(
        "route latency evidence",
        "fail",
        detail + "; Run route-latency validation for usb_low_latency_48k; "
        "p95 must be "
        f"<={ROUTE_LATENCY_P95_BUDGET_MS:g} ms with >=200 impulses over >=5 minutes, "
        "and promotion p99 requires >=1000 impulses over >=30 minutes.",
    )


@doctor_check(order=51.68, group="audio")
def check_fanin_coupling_value() -> CheckResult:
    """The persisted fan-in coupling must be a RECOGNIZED token.

    A migrating box may carry ``JASPER_FANIN_CAMILLA_COUPLING=transport_pipe`` (the
    coupling REMOVED 2026-07-11) or a typo. ``resolve_coupling`` fails such a value
    safe to loopback at daemon start, and the ``--auto`` reconciler converges it
    loudly (``event=…result=removed_coupling_failsafe``); this surfaces the stale
    value until that pass runs so the operator knows the persisted file names a
    transport that no longer exists.
    """
    from jasper.fanin.coupling_reconcile import FANIN_ENV_PATH
    from jasper.fanin_coupling import COUPLING_ENV_VAR, coupling_value_removed
    from jasper.env_file import read_value

    label = "fan-in coupling value"
    try:
        text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        return CheckResult(label, "ok", "no fanin.env — coupling defaults to loopback")
    raw = read_value(text, COUPLING_ENV_VAR)
    if coupling_value_removed(raw):
        return CheckResult(
            label,
            "warn",
            f"{COUPLING_ENV_VAR}={raw!r} in {FANIN_ENV_PATH} names a removed/unknown "
            "transport (e.g. the deleted transport_pipe coupling); it fails safe to "
            "loopback. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback (or --auto) to converge the box "
            "and clean the file.",
        )
    return CheckResult(label, "ok", f"{COUPLING_ENV_VAR}={raw or '(unset → loopback)'}")


@doctor_check(order=51.7, group="audio")
def check_fanin_coupling() -> CheckResult:
    """The transport intent must match the loaded CamillaDSP graph.

    A mismatch is a half-applied arm/disarm: the dangerous one is
    intent=loopback but a ``RawFile`` config is loaded — CamillaDSP then reads a
    pipe no writer feeds and crash-loops on its statefile config (the jts5
    2026-06-27 failure mode). Under ``shm_ring`` (P2) both ends are ALSA ioplug
    devices — capture ``jts_ring_capture`` (Ring A) + playback
    ``jts_ring_playback`` (Ring B) — AND ``JASPER_OUTPUTD_CONTENT_BRIDGE`` must be
    ``shm_ring``: this check catches the PARTIAL flip (one end ring, the other
    ALSA/direct) that strands a ring. The fix is to re-run the ordered
    reconciler: ``jasper-fanin-coupling-reconcile <intent>``.
    """
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import (
        COUPLING_SHM_RING,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        RING_CAPTURE_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_outputd_content_bridge,
    )
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_ENV_PATH
    from jasper.env_file import read_value

    label = "fan-in coupling"
    coupling = read_persisted_coupling()
    # _active_camilla_config_path returns (statefile, active_config_path|None);
    # the active path is what CamillaDSP actually loaded. Fall back to the JTS
    # sound config when the statefile names nothing.
    _, active_path = _active_camilla_config_path()
    config_path = Path(active_path) if active_path else Path(
        "/var/lib/camilladsp/configs/sound_current.yml"
    )
    try:
        outputd_env = Path(DEFAULT_OUTPUTD_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        outputd_env = ""
    outputd_bridge = resolve_outputd_content_bridge(
        read_value(outputd_env, OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
    )
    # Ring B env coherence: shm_ring REQUIRES the outputd bridge to match, and any
    # NON-ring coupling must NOT carry a stale shm_ring bridge (a partial flip that
    # points outputd at a ring nobody writes).
    if coupling == COUPLING_SHM_RING and outputd_bridge != OUTPUTD_CONTENT_BRIDGE_SHM_RING:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={outputd_bridge} "
            f"in {DEFAULT_OUTPUTD_ENV_PATH} (expected shm_ring); PARTIAL flip — "
            "outputd reads snd-aloop while fan-in writes Ring A. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring",
        )
    if coupling != COUPLING_SHM_RING and outputd_bridge == OUTPUTD_CONTENT_BRIDGE_SHM_RING:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but stale {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring "
            f"remains in {DEFAULT_OUTPUTD_ENV_PATH}; outputd waits on Ring B that "
            "CamillaDSP no longer writes — run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback",
        )
    capture = _loaded_capture_type(config_path)
    if capture is None:
        # No JTS config loaded yet (fresh box / non-JTS graph) — nothing to
        # contradict the intent. Report the intent so the comb has a verdict.
        return CheckResult(label, "ok", f"intent={coupling}; no loaded capture to compare")

    if coupling == COUPLING_SHM_RING:
        # Ring A capture + Ring B playback are BOTH ALSA ioplug devices — the
        # loaded graph must name jts_ring_capture AND jts_ring_playback, or the
        # coherent env pair above landed but the loaded config is stale/half-ring
        # (the built-in-revert class: a camilla restart re-seeded loopback).
        capture_device = _loaded_device_field(config_path, "capture", "device")
        playback_device = _loaded_device_field(config_path, "playback", "device")
        ring_mismatches: list[str] = []
        if capture != "Alsa" or capture_device != RING_CAPTURE_DEVICE:
            ring_mismatches.append(
                f"capture={capture}/{capture_device or '(missing)'} "
                f"(expected Alsa/{RING_CAPTURE_DEVICE})"
            )
        if playback_device != RING_PLAYBACK_DEVICE:
            ring_mismatches.append(
                f"playback_device={playback_device or '(missing)'} "
                f"(expected {RING_PLAYBACK_DEVICE})"
            )
        if not ring_mismatches:
            return CheckResult(
                label,
                "ok",
                f"{coupling} (capture={RING_CAPTURE_DEVICE}, "
                f"playback={RING_PLAYBACK_DEVICE}, bridge={outputd_bridge})",
            )
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but loaded graph is not the ring config: "
            f"{'; '.join(ring_mismatches)}; a camilla restart may have re-seeded "
            "loopback (finding-5 revert) — run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring",
        )

    # Non-ring intent (loopback). The env
    # pair may be coherent (loopback/direct) yet the LOADED graph still name the
    # ring ioplug devices — a disarm whose camilla step failed leaves a stale ring
    # config that captures a writer-dead Ring A (zero-fill silence) while the env
    # reads clean. A type-only "capture==Alsa" check reads GREEN through that
    # permanent-silence state (the mirror of the shm_ring finding-5 branch above,
    # for the disarm direction). So inspect the device names too.
    capture_device = _loaded_device_field(config_path, "capture", "device")
    playback_device = _loaded_device_field(config_path, "playback", "device")
    stale_ring_devices = [
        f"{lane}={dev}"
        for lane, dev in (("capture", capture_device), ("playback", playback_device))
        if dev in (RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE)
    ]
    if stale_ring_devices:
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but the loaded graph still names ring ioplug "
            f"device(s): {'; '.join(stale_ring_devices)}; a disarm's camilla step "
            "likely failed — CamillaDSP captures a writer-dead ring (silence) while "
            "the env reads clean. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile loopback",
        )
    expected = "Alsa"
    if capture == expected:
        playback = _loaded_playback_type(config_path)
        playback_path = _loaded_playback_filename(config_path)
        if playback == "File" and playback_path != "/run/jasper-snapserver/snapfifo":
            return CheckResult(
                label,
                "warn",
                f"intent={coupling} but loaded playback is a non-Snapcast File "
                f"sink ({playback_path or '(missing)'}); a stale File sink left by "
                "the removed transport_pipe coupling — run: "
                "sudo /opt/jasper/.venv/bin/"
                "jasper-fanin-coupling-reconcile loopback",
            )
        return CheckResult(label, "ok", f"{coupling} (capture={capture})")
    return CheckResult(
        label,
        "warn",
        f"intent={coupling} but loaded capture={capture} (expected {expected}); "
        f"half-applied transition — run: "
        f"sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile {coupling}",
    )


def _jts_ring_path_for(pcm: str) -> str | None:
    """The SHM ring-file path a given inert PCM's open probe would create,
    or None if the PCM name is not one of ours. Derived from _JTS_RING_SHM_DIR
    (so tests can repoint the dir) + the basename registered in _JTS_RING_PCMS
    (which mirrors deploy/alsa/conf.d/60-jts-ring.conf's `path` values)."""
    for name, _tool, ring_basename in _JTS_RING_PCMS:
        if name == pcm:
            return os.path.join(_JTS_RING_SHM_DIR, ring_basename)
    return None


def _jts_ring_pcm_resolves(pcm: str, tool: str) -> tuple[bool, str]:
    """Open-probe one inert jts_ring PCM. Success means ALSA resolved the
    conf.d name AND dlopen()ed the ioplug .so AND the writer-dead/no-reader
    silence path terminated. A 1-second probe against an absent ring is
    safe: the ioplug free-runs (playback) or emits timer-paced silence
    (capture) rather than blocking (the lab resolvability-step contract).

    Leaves no residue: the ioplug open path is create-or-attach
    (O_RDWR|O_CREAT|O_EXCL), so probing an ABSENT ring CREATES the ring file.
    A doctor-created ring would (a) violate P1's inertness invariant ("no ring
    file exists until P2 arms") on every box after every deploy, and (b) poison
    P2's first arm, because a valid-magic ring carrying the conf.d PLACEHOLDER
    geometry is a fail-closed open error (only magic-less files are reclaimed).
    So we snapshot the ring path's existence before the probe and unlink ONLY a
    file the probe itself created. A live armed ring pre-exists (it is in the
    "existed before" set and is never unlinked; it also EBUSYs the probe via the
    SPSC guard), so this can never remove a ring in use.

    Returns (ok, detail). detail carries the tail of stderr on failure so
    a broken registration (e.g. the -DPIC "undefined symbol: snd_dlsym_start"
    class) is legible, not just "probe failed".
    """
    if not shutil.which(tool):
        return False, f"{tool} not found"
    ring_path = _jts_ring_path_for(pcm)
    pre_existed = ring_path is not None and os.path.exists(ring_path)
    # arecord -> /dev/null (discard captured silence); aplay -> /dev/zero
    # (feed silence in). Both 2ch/48k/S16_LE/1s, matching the lab probe.
    sink = "/dev/null" if tool == "arecord" else "/dev/zero"
    try:
        proc = _run(
            [tool, "-D", pcm, "-c", "2", "-r", "48000", "-f", "S16_LE",
             "-d", "1", sink],
            timeout=6.0,
        )
    except subprocess.TimeoutExpired:
        return False, "open probe hung (>6 s) — ioplug no-reader/no-writer path may be broken"
    finally:
        # Remove a ring file the probe created (it did not exist beforehand).
        # Best-effort: a failure to unlink must not turn a clean probe into a
        # doctor error, but the residue would still be visible next run.
        if ring_path and not pre_existed and os.path.exists(ring_path):
            try:
                os.unlink(ring_path)
            except OSError:
                pass
    if proc.returncode == 0:
        return True, "resolved"
    err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    if len(err) > 160:
        err = err[:157] + "..."
    return False, err or f"{tool} exit {proc.returncode}"


@doctor_check(order=51.8, group="audio", exclusive_group="audio-probe")
def check_ring_platform_assets() -> CheckResult:
    """Verify the jts_ring transport platform assets are present and the
    ioplug actually dlopens (audio-graph consolidation P1).

    Three assets ship INERT in P1: the compiled ioplug .so, the conf.d
    PCM definitions (pcm.jts_ring_capture / pcm.jts_ring_playback), and
    the /dev/shm/jts-ring directory. Nothing opens them yet — the default
    coupling is still loopback — but the platform must be correctly staged
    for P2 to arm it, and a broken .so (the -DPIC registration class) or a
    missing asset should surface here rather than at first arm.

    Statuses:
      ok    — .so + conf.d + shm dir present, both PCMs open-probe cleanly.
              CAVEAT: this cannot distinguish a freshly-built .so from a STALE
              one left by a failed rebuild (the 2026-07-02 class) — a stale but
              structurally-valid .so open-probes fine and reads ok here. The
              install transcript's build-failure WARN is the signal for that;
              ioplug-vs-Rust protocol-drift detection is P2's job (when the .so
              becomes load-bearing). See ring-platform.sh.
      warn  — an asset is MISSING (a first-ever build failed, or drift). P1 is
              inert and loopback remains the transport, so a missing .so is
              degraded, not broken — the next deploy rebuilds. (This flips to
              fail after P9, when the ioplug becomes load-bearing.)
      fail  — the .so is installed but a PCM fails to open. In the inert
              phase that is a real defect (bad registration / arch mismatch,
              e.g. the -DPIC class), which would break P2's arm. The EBUSY
              exception is called out below.

    Probe leaves no residue: it opens each device for 1 s against an absent
    ring, exercising only the writer-dead / no-reader silence path (feeds or
    discards silence). Because the ioplug open path is create-or-attach, the
    probe would create the ring file; _jts_ring_pcm_resolves snapshots and
    unlinks only what it created, so P1's "no ring file until P2 arms"
    invariant holds after every deploy.

    Armed-state aware (P2): when a coupling is ARMED (the persisted
    JASPER_FANIN_CAMILLA_COUPLING is shm_ring) the ring has a live reader/writer,
    so the ioplug's SPSC guard EBUSYs the open-probe — which is NOT a defect. In
    that state this check does NOT open-probe (the live ring must not be
    disturbed); it verifies asset PRESENCE only and defers the "is the armed ring
    coherent + alive" verdict to check_ring_coupling_coherent. The open-probe path
    below runs only in the INERT phase (loopback default), where an EBUSY genuinely
    would indicate a stray lab arm or a stuck ring.
    """
    label = "ring platform"
    # Pass the module-level constants (which tests monkeypatch, and which alias the
    # ring_assets SSOT) so the presence snapshot honors a repointed path.
    presence = ring_assets.ring_asset_presence(
        plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR,
        conf_d=_JTS_RING_CONF_D,
        shm_dir=_JTS_RING_SHM_DIR,
    )
    missing = list(presence.missing())

    # Is a ring coupling armed? Read the persisted intent (fail-safe to loopback).
    try:
        from jasper.fanin.coupling_reconcile import read_persisted_coupling
        from jasper.fanin_coupling import COUPLING_SHM_RING

        ring_armed = read_persisted_coupling() == COUPLING_SHM_RING
    except (ImportError, OSError):
        ring_armed = False

    if missing:
        if ring_armed:
            # ARMED but an asset is gone: the ring is load-bearing now, so this is
            # a genuine failure (the ioplug/conf.d must exist for the armed graph).
            return CheckResult(
                label,
                "fail",
                "shm_ring is ARMED but a ring-platform asset is missing: "
                + "; ".join(missing)
                + " — the armed graph cannot resolve its ring devices; "
                "disarm (jasper-fanin-coupling-reconcile loopback) or redeploy.",
            )
        # Inert phase: a missing asset means the ring platform is
        # unavailable, but loopback still carries audio — degraded, not
        # a hard failure. Redeploy to rebuild/replace.
        return CheckResult(
            label,
            "warn",
            "inert platform incomplete (loopback still active): "
            + "; ".join(missing)
            + " — redeploy (bash scripts/deploy-to-pi.sh) to rebuild",
        )

    if ring_armed:
        # Assets present AND armed: do NOT open-probe (the live ring EBUSYs the
        # SPSC guard — expected, not a defect). Report ok here; the coherence
        # + liveness verdict is check_ring_coupling_coherent's job.
        return CheckResult(
            label,
            "ok",
            "ioplug + conf.d + /dev/shm/jts-ring present; shm_ring ARMED "
            "(open-probe skipped — live ring; see 'ring coupling' check)",
        )

    # All assets present AND inert — the .so being installed means it MUST dlopen
    # and register; a failure to open is a genuine defect.
    probe_failures: list[str] = []
    for pcm, tool, _ring_basename in _JTS_RING_PCMS:
        ok, detail = _jts_ring_pcm_resolves(pcm, tool)
        if not ok:
            probe_failures.append(f"{pcm}: {detail}")
    if probe_failures:
        # EBUSY is NOT a registration defect: on a lab-armed box (the ring-proto
        # experiment live, or after P2 arms a coupling) the ring already has a
        # live foreign reader/writer, and the ioplug's SPSC guard refuses the
        # probe with -EBUSY ("Device or resource busy"). The .so is fine — the
        # ring is simply in use. (check_ring_platform_assets and its "probe is
        # safe anytime" docstring assume the inert phase; P2 must make this check
        # armed-state-aware so an armed ring reports ok/skip, not fail.)
        joined = "; ".join(probe_failures)
        if re.search(r"resource busy|EBUSY|Device or resource busy", joined, re.I):
            remediation = (
                "ring is in use (a live reader/writer already owns it — e.g. a "
                "lab arm) — not a registration defect; re-run with the ring "
                "disarmed / camilla stopped to probe the .so."
            )
        else:
            remediation = (
                "Rebuild the ioplug (check -DPIC / arch); the .so is present but "
                "ALSA can't use it."
            )
        return CheckResult(
            label,
            "fail",
            f"ioplug .so installed but PCM open failed: {joined}. {remediation}",
        )
    return CheckResult(
        label,
        "ok",
        f"ioplug + conf.d + /dev/shm/jts-ring staged (inert); "
        f"{', '.join(name for name, _tool, _ring in _JTS_RING_PCMS)} resolve",
    )


@doctor_check(order=51.9, group="audio")
def check_ring_geometry_coherence() -> CheckResult:
    """Verify the Ring-A geometry agrees across env, conf.d, and on-disk (defect A).

    The ring geometry must match or CamillaDSP's ioplug attach fails hard
    (hw_params EINVAL + ``attach_fatal reason=ring header does not match expected
    geometry`` → crash-loop → start-limit-hit). ``n_slots`` is checked on THREE
    axes; the on-disk header adds a fourth check on ``period_frames`` (the ring slot
    IS one outputd period, so a stale period fails the attach even with matching
    slots — Nit-7, 2026-07-05):

      1. fan-in's resolved ``JASPER_FANIN_RING_SLOTS`` (jasper.env -> fanin.env
         systemd env chain, default 2)
      2. the conf.d ``jts_ring_capture`` ``n_slots`` (the ioplug attach authority)
      3. the on-disk ``program.ring`` header ``n_slots`` (what the writer created)
      4. the on-disk header ``period_frames`` vs the conf.d ``period_frames``

    The 2026-07-06 default migration class is old 8-slot state making fan-in or
    the existing ring file present 8 slots while the conf.d pins 2. The coupling
    reconciler preflights + self-heals this at arm time (and on the CONFIRM path
    for an already-armed box); this check is the standing surface that catches
    drift on a live box.

    Skips cleanly when the coupling is NOT shm_ring (the ring is inert — the env /
    conf.d values are placeholders that nothing opens, so a "mismatch" is not a
    live defect). On an armed box a mismatch is ``fail`` (the graph cannot run);
    an indeterminate conf.d / env is ``warn`` (redeploy to reinstall).
    """
    label = "ring geometry"
    try:
        from jasper.fanin.coupling_reconcile import (
            FANIN_ENV_PATH,
            read_persisted_coupling,
            resolve_effective_fanin_ring_slots,
        )
        from jasper.fanin_coupling import (
            COUPLING_SHM_RING,
            RING_SLOTS_ENV_VAR,
        )
    except ImportError as e:  # pragma: no cover - always importable in prod
        return CheckResult(label, "warn", f"ring modules unavailable: {e}")

    if read_persisted_coupling(FANIN_ENV_PATH) != COUPLING_SHM_RING:
        return CheckResult(
            label, "ok",
            "skipped — shm_ring not armed (Ring A geometry is inert; nothing opens it)",
        )

    # Axis 1: fan-in's resolved env slot count (fail-loud on a bad value).
    try:
        fanin_text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        fanin_text = ""
    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.value is None:
        return CheckResult(
            label, "fail",
            f"effective {RING_SLOTS_ENV_VAR} from {resolution.source} is invalid: "
            f"{resolution.error}. shm_ring is armed — fan-in will refuse to create "
            "the ring. Clear the stale value.",
        )
    fanin_slots = resolution.value

    # Axis 2: the conf.d attach authority.
    conf_slots = ring_assets.ring_conf_n_slots(
        ring_assets.RING_A_CONF_PCM, _JTS_RING_CONF_D
    )
    if conf_slots is None:
        return CheckResult(
            label, "warn",
            f"conf.d ({_JTS_RING_CONF_D}) has no single n_slots for "
            f"pcm.{ring_assets.RING_A_CONF_PCM}; Ring A geometry is indeterminate — "
            "redeploy to reinstall the ring conf.d.",
        )
    if fanin_slots != conf_slots:
        return CheckResult(
            label, "fail",
            f"Ring A slot mismatch: JASPER_FANIN_RING_SLOTS resolves to {fanin_slots} "
            f"but conf.d pcm.{ring_assets.RING_A_CONF_PCM} pins n_slots={conf_slots}. "
            "shm_ring is armed — CamillaDSP's ioplug attach fails (hw_params EINVAL) "
            "and crash-loops. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it self-heals a stale env), "
            "or match the two values.",
        )

    # Axis 3: the on-disk ring header (what the writer actually created).
    header = ring_assets.read_ring_header(ring_assets.RING_A_PROGRAM_FILE)
    if not header.valid:
        # Armed but no coherent on-disk ring yet: fan-in may be between restarts,
        # or the ring was cleared. The env/conf.d agree, so the next writer create
        # is coherent — noteworthy, not a hard failure.
        return CheckResult(
            label, "warn",
            f"env + conf.d agree (n_slots={fanin_slots}) but {ring_assets.RING_A_PROGRAM_FILE} "
            "has no valid ring header yet (fan-in restarting / ring cleared). It "
            "will be created coherently on the next fan-in start.",
        )
    if header.n_slots != conf_slots:
        return CheckResult(
            label, "fail",
            f"on-disk Ring A ({ring_assets.RING_A_PROGRAM_FILE}) has n_slots="
            f"{header.n_slots} but env + conf.d expect {conf_slots}. A stale ring "
            "file from a prior geometry blocks the ioplug attach. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
            "(it deletes a geometry-mismatched ring file before re-arming).",
        )
    # period_frames is the SECOND on-disk geometry axis (the ring slot IS one
    # outputd period): a file with matching n_slots but a stale period_frames also
    # fails the ioplug attach — the exact confusing daemon-level error the
    # preflights exist to pre-empt. Compare against the conf.d's pinned period when
    # it is readable (indeterminate conf.d period → skip this axis, don't guess).
    conf_period = ring_assets.ring_conf_period_frames(_JTS_RING_CONF_D)
    if conf_period is not None and header.period_frames != conf_period:
        return CheckResult(
            label, "fail",
            f"on-disk Ring A ({ring_assets.RING_A_PROGRAM_FILE}) has period_frames="
            f"{header.period_frames} but conf.d expects {conf_period} (n_slots match "
            f"at {header.n_slots}). A stale ring file from a prior period geometry "
            "blocks the ioplug attach. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it deletes a geometry-"
            "mismatched ring file before re-arming).",
        )
    return CheckResult(
        label, "ok",
        f"Ring A geometry coherent across env + conf.d + on-disk header "
        f"(n_slots={header.n_slots}, period_frames={header.period_frames})",
    )


@doctor_check(order=51.95, group="audio")
def check_ring_conf_floor_render() -> CheckResult:
    """Verify the ring conf.d slot period matches the active DAC's declared floor.

    The ring slot IS one outputd DAC period, so a box whose DAC declares a
    :class:`~jasper.audio_hardware.dac.LatencyFloor` has its conf.d
    ``period_frames`` RENDERED from that floor by
    ``jasper-audio-hardware-reconcile``. This check is the standing surface that
    catches a box where that render did not land (an install that re-laid the
    shipped conf.d without a reconcile after it, a hand-edited conf.d, a DAC
    swapped while the reconciler was down).

    Both facts are read from their owners: the floor from the DAC registry
    (``latency_floor_for``), the period from the conf.d file itself.

    Statuses:
      ok    — no declared floor (the shipped default stands, by rule); a
              declared floor that is not ``RING_SLOT_FRAMES`` (a documented
              product boundary, not drift — see below); or the conf.d already
              declares the floor's period.
      warn  — a renderable floor the conf.d has NOT been rendered to, or an
              indeterminate conf.d period. Never fail: the conf.d is inert
              unless shm_ring is armed, and the coupling reconciler
              independently preflights this and fail-closes to loopback rather
              than arming a mismatched geometry.

    The product boundary: Ring A's slot size is fan-in's COMPILE-TIME
    ``RING_SLOT_FRAMES`` (``rust/jasper-fanin/src/config.rs``, no env
    override), so only a floor that EQUALS it is renderable. A DAC declaring
    any other floor never gets a rendered conf.d — shm_ring is simply
    unavailable on it, loopback continues, and the floor's outputd
    period/buffer geometry still applies through ``outputd.env``. That is a
    known limit with an owner (issue #2147), so it reports ok, not warn.

    Scope: this compares the conf.d against the DECLARED floor, which is what
    the renderer uses. An operator ``JASPER_OUTPUTD_PERIOD_FRAMES`` override
    moves outputd's EFFECTIVE period away from the floor; that divergence is
    the coupling reconciler's preflight to catch (and ``ring geometry`` for the
    on-disk axis), not this check's.
    """
    label = "ring conf floor"
    dac_id = _active_audio_dac_id()
    floor = latency_floor_for(dac_id)
    if floor is None:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares no latency floor — {_JTS_RING_CONF_D} keeps its "
            "shipped default (no declared floor, nothing to render)",
        )
    if floor.outputd_period_frames != RING_SLOT_FRAMES:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares outputd period "
            f"{floor.outputd_period_frames} != the fixed ring transport slot "
            f"{RING_SLOT_FRAMES}, so its conf.d is deliberately not rendered: "
            "shm_ring is unavailable on this DAC until issue #2147 makes the "
            "ring slot floor-derived. Loopback coupling continues and still "
            "receives the floor's outputd period/buffer geometry.",
        )
    conf_period = ring_assets.ring_conf_period_frames(_JTS_RING_CONF_D)
    if conf_period is None:
        return CheckResult(
            label,
            "warn",
            f"{dac_id} declares outputd_period_frames="
            f"{floor.outputd_period_frames} but {_JTS_RING_CONF_D} has no single "
            "period_frames (absent or torn); the ring slot geometry is "
            "indeterminate — redeploy (bash scripts/deploy-to-pi.sh) to "
            "reinstall it.",
        )
    if conf_period != floor.outputd_period_frames:
        return CheckResult(
            label,
            "warn",
            f"{_JTS_RING_CONF_D} pins period_frames={conf_period} but {dac_id} "
            f"declares a latency floor of {floor.outputd_period_frames}; the ring "
            "slot is one outputd DAC period, so shm_ring cannot arm against this "
            "conf.d. Run: sudo systemctl start "
            "jasper-audio-hardware-reconcile.service (it renders the conf.d from "
            "the declared floor).",
        )
    return CheckResult(
        label,
        "ok",
        f"period_frames={conf_period} matches {dac_id}'s declared latency floor",
    )


def _outputd_dual_apple_health(
    data: dict[str, object],
    *,
    sink_mode: object,
    active_single_alsa: bool,
    active_channels: int | None,
) -> tuple[str, str, str | None] | CheckResult:
    dual_detail = ""
    dual_warning: str | None = None
    active_detail = (
        f", active_channels={active_channels}"
        if active_single_alsa else ""
    )
    if sink_mode == "dual_apple":
        dual = data.get("dual_apple")
        if not isinstance(dual, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "STATUS missing dual_apple runtime health for dual sink",
            )
        dual_a_pcm = dual.get("dac_a_pcm")
        dual_b_pcm = dual.get("dac_b_pcm")
        if not isinstance(dual_a_pcm, str) or not dual_a_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_a_pcm is missing",
            )
        if not isinstance(dual_b_pcm, str) or not dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_b_pcm is missing",
            )
        if dual_a_pcm == dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple DAC A/B PCMs are identical",
            )
        dual_linked = bool(dual.get("linked", False))
        delay_delta = dual.get("delay_delta_frames")
        delay_error = dual.get("delay_delta_error_frames")
        max_delay = dual.get("max_delay_delta_frames")
        if (
            isinstance(delay_error, int)
            and isinstance(max_delay, int)
            and delay_error > max_delay
        ):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple delay delta exceeds runtime budget: "
                f"error={delay_error} max={max_delay}",
            )
        if not dual_linked:
            dual_warning = "dual Apple PCMs are not ALSA-linked"
        dual_detail = (
            f", dual_a_pcm={dual_a_pcm}, dual_b_pcm={dual_b_pcm}, "
            f"dual_linked={dual_linked}, "
            f"dual_delay_delta_frames={delay_delta}, "
            f"dual_delay_delta_error_frames={delay_error}, "
            f"dual_max_delay_delta_frames={max_delay}"
        )
    return active_detail, dual_detail, dual_warning


def _outputd_service_state_failure() -> CheckResult | None:
    """Return the actionable systemd failure, or ``None`` when active."""
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-outputd.service"]
    ).stdout.strip()
    if enabled == "not-found":
        return CheckResult(
            "jasper-outputd",
            "fail",
            "systemd unit is not installed. Re-run install.sh.",
        )
    if enabled not in {"enabled", "static"}:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"systemd unit is {enabled or 'unknown'}; expected enabled "
            "for the outputd mainline topology.",
        )
    active = _run(
        ["systemctl", "is-active", "jasper-outputd.service"]
    ).stdout.strip()
    if active != "active":
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"service state={active or 'unknown'}. "
            "Check: journalctl -u jasper-outputd",
        )
    return None


def _outputd_status_payload() -> dict[str, object] | CheckResult:
    """Load and validate the STATUS transport envelope."""
    try:
        payload = _read_status_socket_bytes(_OUTPUTD_STATUS_SOCKET, timeout=2.0)
    except OSError as exc:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS probe at {_OUTPUTD_STATUS_SOCKET} failed: {exc}. "
            "Without STATUS doctor cannot verify DAC ownership, buffers, "
            "xruns, or work-loop progress.",
        )
    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS returned invalid JSON: {exc}",
        )
    if not isinstance(data, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS root is {type(data).__name__}, expected object",
        )
    return data


def _outputd_content_bridge_health(
    data: dict[str, object],
) -> tuple[str, str | None]:
    """Summarize optional rate-match bridge telemetry and anomalies."""
    bridge = data.get("content_bridge")
    detail = "content_bridge=missing"
    warning: str | None = None
    if not isinstance(bridge, dict):
        return detail, warning
    if not bool(bridge.get("enabled", False)):
        return "content_bridge=direct", warning

    locked = bool(bridge.get("locked", False))
    fill = int(bridge.get("fill_frames", 0) or 0)
    target = int(bridge.get("target_fill_frames", 0) or 0)
    ratio = bridge.get("ratio_ppm")
    silence = int(bridge.get("silence_frames", 0) or 0)
    underrun = int(bridge.get("underrun_frames", 0) or 0)
    overrun = int(bridge.get("overrun_frames", 0) or 0)
    resync = int(bridge.get("resync_count", 0) or 0)
    reset = int(bridge.get("reset_count", 0) or 0)
    clamp = int(bridge.get("ratio_clamp_count", 0) or 0)
    detail = (
        f"content_bridge=rate_match, bridge_locked={locked}, "
        f"bridge_fill_frames={fill}, bridge_target_fill_frames={target}, "
        f"bridge_ratio_ppm={ratio}, bridge_silence_frames={silence}, "
        f"bridge_underrun_frames={underrun}, "
        f"bridge_overrun_frames={overrun}, bridge_resync_count={resync}, "
        f"bridge_reset_count={reset}, bridge_ratio_clamp_count={clamp}"
    )
    anomalies = []
    for name, value in (
        ("underrun_frames", underrun),
        ("overrun_frames", overrun),
        ("resync_count", resync),
        ("reset_count", reset),
        ("ratio_clamp_count", clamp),
    ):
        if value:
            anomalies.append(f"{name}={value}")
    if anomalies:
        warning = ", ".join(anomalies)
    return detail, warning


def _outputd_loudness_health(data: dict[str, object]) -> str | CheckResult:
    """Validate optional assistant-loudness telemetry and render its detail."""
    loudness = data.get("assistant_loudness")
    if not isinstance(loudness, dict):
        return "assistant_loudness=fan-in-owned"
    decision_seen = bool(loudness.get("decision_seen", False))
    calibrated = bool(loudness.get("calibrated", False))
    final_gain = loudness.get("final_gain_db")
    content_anchor = loudness.get("content_anchor_lufs")
    if decision_seen and not isinstance(final_gain, (int, float)):
        return CheckResult(
            "jasper-outputd",
            "warn",
            "active but assistant_loudness.decision_seen=true without "
            "numeric final_gain_db.",
        )
    if (
        isinstance(final_gain, (int, float))
        and not -60.0 <= float(final_gain) <= 0.0
    ):
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but assistant_loudness.final_gain_db={final_gain!r}; "
            "expected clamped [-60, 0] dB.",
        )
    return (
        f"assistant_loudness_decision={decision_seen}, "
        f"assistant_loudness_calibrated={calibrated}, "
        f"assistant_final_gain_db={final_gain}, "
        f"content_anchor_lufs={content_anchor}"
    )



def _outputd_buffer_health(
    data: dict[str, object],
    content: dict[str, object],
    *,
    ring_mode: bool,
    content_buffer: object,
    dac_buffer: object,
    period_frames: int,
) -> str | CheckResult:
    """Validate ALSA or SHM-ring buffer geometry and return ring detail."""
    ring_detail = ""
    if ring_mode:
        # Ring B (SHM ping-pong content ring): outputd reads the post-DSP program
        # from an n-slot SHM ring, NOT an ALSA capture PCM (AlsaBackend::new never
        # opens the content PCM under shm_ring). content.buffer_frames is therefore
        # a synthetic period-sized stand-in, so the generic ">= 2x period" ALSA
        # jitter-margin floor does not apply — a bounded n-slot queue is not an ALSA
        # buffer, and every shm_ring box would otherwise structurally fail that floor
        # (content.buffer_frames == period < 2 x period). Validate the TRUE ring
        # geometry from content.ring instead (the honesty contract outputd publishes
        # next to the synthetic; capacity_frames == n_slots x slot_frames).
        if not isinstance(content_buffer, int) or content_buffer < period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.buffer_frames={content_buffer!r}; expected >= "
                f"one period ({period_frames})",
            )
        ring = content.get("ring")
        if not isinstance(ring, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "content.source='shm_ring' but STATUS missing content.ring geometry "
                "contract (n_slots/slot_frames/capacity_frames). Redeploy outputd.",
            )
        ring_slots = ring.get("slots")
        ring_slot_frames = ring.get("slot_frames")
        ring_capacity = ring.get("capacity_frames")
        if not isinstance(ring_slots, int) or ring_slots < 2:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slots={ring_slots!r}; expected >= 2 "
                "(ping-pong minimum)",
            )
        if not isinstance(ring_slot_frames, int) or ring_slot_frames != period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slot_frames={ring_slot_frames!r}; expected "
                f"== dac.period_frames ({period_frames}) — the ring slot must match "
                "the DAC period.",
            )
        expected_capacity = ring_slots * ring_slot_frames
        if not isinstance(ring_capacity, int) or ring_capacity != expected_capacity:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.capacity_frames={ring_capacity!r}; expected "
                f"n_slots*slot_frames ({expected_capacity})",
            )
        # Runtime health (occupancy/attach) rides the top-level shm_ring block; a
        # transient empty ring is normal (idle), so surface it in the detail without
        # gating on it here.
        shm_ring_block = data.get("shm_ring")
        ring_occupancy = (
            shm_ring_block.get("occupancy")
            if isinstance(shm_ring_block, dict) else None
        )
        ring_attached = (
            bool(shm_ring_block.get("attached", False))
            if isinstance(shm_ring_block, dict) else None
        )
        ring_detail = (
            f", shm_ring_slots={ring_slots}, shm_ring_slot_frames={ring_slot_frames}"
            f", shm_ring_capacity_frames={ring_capacity}"
            f", shm_ring_occupancy={ring_occupancy}"
            f", shm_ring_attached={ring_attached}"
        )
    elif not isinstance(content_buffer, int) or content_buffer < period_frames * 2:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.buffer_frames={content_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
        )
    if not isinstance(dac_buffer, int) or dac_buffer < period_frames * 2:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.buffer_frames={dac_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
        )

    return ring_detail



def _transport_route_remedy() -> str:
    """Return the remedy that actually clears a post-DSP route disconnect.

    Honest branch: when the saved layout needs a roleful graph and the resolved
    DAC declares no active outputd lane, no reconcile can pair the lanes — the
    reconciler is *correctly* resolving passive for hardware that has no active
    lane, so recommending it sends an operator into a loop. Naming the
    reconciler stays right for the reconcilable case: an active-capable DAC
    whose generated env has drifted.
    """
    from jasper.active_speaker.playback_route import active_lane_capability_gap
    from jasper.output_topology import load_output_topology

    gap = active_lane_capability_gap(load_output_topology())
    if gap is not None:
        return (
            f". {gap.device_label} does not support the active speaker lane, so "
            "this cannot be reconciled: choose a passive speaker layout on this "
            "speaker's /sound/setup/ page (passive sends full-range audio to "
            "every output — only safe when the speaker has its own built-in "
            "passive crossover), or attach an active-capable DAC."
        )
    return (
        ". Run jasper-audio-hardware-reconcile to restore the paired "
        "CamillaDSP playback/outputd capture lane, then re-run "
        "jasper-fanin-coupling-reconcile only if the coupling check also "
        "reports Ring A/Ring B drift."
    )


def _outputd_transport_health(
    data: dict[str, object],
    content: dict[str, object],
    dac: dict[str, object],
    *,
    outputd_env: dict[str, str],
    sink_mode: object,
    active_channels: int | None,
    expected_content_pcm: str,
    expected_dac_pcm: str,
) -> tuple[str, str, str, bool] | CheckResult:
    """Validate coupling, endpoint coherence, PCMs, and reference ownership."""
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import COUPLING_SHM_RING
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
        output_endpoint_evidence_from_statefiles,
        transport_coherence_errors,
        transport_topology_for_coupling,
    )

    coupling = read_persisted_coupling()
    topology = transport_topology_for_coupling(
        coupling,
        outputd_env=outputd_env,
    )
    expected_content_source = topology.outputd_content_source
    actual_content_source = content.get("source")
    if actual_content_source != expected_content_source:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.source={actual_content_source!r}; expected "
            f"{expected_content_source!r} for persisted coupling={coupling!r}. "
            "Run jasper-fanin-coupling-reconcile to restart outputd onto the "
            "persisted topology.",
        )
    live_outputd_env = dict(outputd_env)
    live_outputd_env["JASPER_OUTPUTD_CONTENT_PCM"] = str(content.get("pcm") or "")
    endpoint_evidence = output_endpoint_evidence_from_statefiles(
        DEFAULT_CAMILLA_STATEFILE_PATH,
        DEFAULT_CAMILLA2_STATEFILE_PATH,
    )
    transport_evidence_warning = ""
    if (
        endpoint_evidence.devices is None
        or not endpoint_evidence.endpoint_recognized
    ):
        evidence_detail = "; ".join(endpoint_evidence.errors) or (
            "loaded graph does not target a registered output endpoint"
        )
        transport_evidence_warning = (
            "post-DSP transport coherence unknown: " + evidence_detail
        )
    else:
        transport_errors = transport_coherence_errors(
            coupling=coupling,
            outputd_env=live_outputd_env,
            camilla_devices=endpoint_evidence.devices,
        )
        if transport_errors:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "; ".join(transport_errors) + _transport_route_remedy(),
            )
    local_pipe_detail = f"content_source={actual_content_source}"
    if content.get("pcm") != expected_content_pcm:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.pcm={content.get('pcm')!r}; expected "
            f"{expected_content_pcm!r} for sink_mode={sink_mode!r}, "
            f"active_channels={active_channels!r}",
        )
    if dac.get("pcm") != expected_dac_pcm:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.pcm={dac.get('pcm')!r}; expected {expected_dac_pcm!r} "
            f"for sink_mode={sink_mode!r}, active_channels={active_channels!r}",
        )
    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing reference_outputs speaker-reference contract",
        )
    speaker_reference_source = reference_outputs.get("speaker_reference_source")
    if speaker_reference_source != "outputd_final_electrical":
        return CheckResult(
            "jasper-outputd",
            "fail",
            "reference_outputs.speaker_reference_source="
            f"{speaker_reference_source!r}; expected 'outputd_final_electrical'",
        )
    reference_detail = (
        "speaker_reference_source=outputd_final_electrical, "
        "speaker_reference_active="
        f"{bool(reference_outputs.get('speaker_reference_active', False))}, "
        "speaker_reference_channels="
        f"{reference_outputs.get('speaker_reference_channels')}, "
        f"speaker_reference_udp={reference_outputs.get('udp_target')!r}, "
        f"chip_ref_pcm={reference_outputs.get('chip_ref_pcm')!r}"
    )
    return (
        transport_evidence_warning,
        local_pipe_detail,
        reference_detail,
        coupling == COUPLING_SHM_RING,
    )



@doctor_check(order=52, group="audio")
def check_outputd_service() -> CheckResult:
    """Validate the outputd final-output-owner daemon.

    Current main expects outputd to own the physical DAC. Treat
    disabled/inactive outputd as a real audio-path failure and verify
    the STATUS socket, runtime backend, negotiated buffers, xrun
    counters, and progress sentinel.
    """
    service_failure = _outputd_service_state_failure()
    if service_failure is not None:
        return service_failure
    status_payload = _outputd_status_payload()
    if isinstance(status_payload, CheckResult):
        return status_payload
    data = status_payload

    if data.get("backend") != "alsa":
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but backend={data.get('backend')!r}; expected 'alsa'",
        )
    sink_mode = data.get("sink_mode") or "single_alsa"
    outputd_env = _outputd_reconciled_env()
    active_channels = _outputd_active_channels_from_env(outputd_env)
    active_single_alsa = sink_mode == "single_alsa" and active_channels is not None
    expected_content_pcm = (
        _OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM
        if sink_mode == "dual_apple" or active_single_alsa
        else _OUTPUTD_EXPECTED_CONTENT_PCM
    )
    expected_dac_pcm = (
        _OUTPUTD_EXPECTED_DUAL_DAC_PCM
        if sink_mode == "dual_apple"
        else _OUTPUTD_EXPECTED_DAC_PCM
    )
    content = data.get("content", {})
    dac = data.get("dac", {})
    if not isinstance(content, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing content{}",
        )
    if not isinstance(dac, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing dac{}",
        )
    transport_health = _outputd_transport_health(
        data,
        content,
        dac,
        outputd_env=outputd_env,
        sink_mode=sink_mode,
        active_channels=active_channels,
        expected_content_pcm=expected_content_pcm,
        expected_dac_pcm=expected_dac_pcm,
    )
    if isinstance(transport_health, CheckResult):
        return transport_health
    (
        transport_evidence_warning,
        local_pipe_detail,
        reference_detail,
        ring_mode,
    ) = transport_health
    dual_health = _outputd_dual_apple_health(
        data,
        sink_mode=sink_mode,
        active_single_alsa=active_single_alsa,
        active_channels=active_channels,
    )
    if isinstance(dual_health, CheckResult):
        return dual_health
    active_detail, dual_detail, dual_warning = dual_health
    sample_rate = dac.get("sample_rate")
    period_frames = dac.get("period_frames")
    content_buffer = content.get("buffer_frames")
    dac_buffer = dac.get("buffer_frames")
    if sample_rate != 48000:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.sample_rate={sample_rate!r}; expected 48000",
        )
    if not isinstance(period_frames, int) or period_frames <= 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing positive dac.period_frames",
        )
    buffer_health = _outputd_buffer_health(
        data,
        content,
        ring_mode=ring_mode,
        content_buffer=content_buffer,
        dac_buffer=dac_buffer,
        period_frames=period_frames,
    )
    if isinstance(buffer_health, CheckResult):
        return buffer_health
    ring_detail = buffer_health
    watchdog = data.get("watchdog")
    progress_age = (
        watchdog.get("last_progress_age_ms", -1)
        if isinstance(watchdog, dict)
        else -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS response missing watchdog.last_progress_age_ms",
        )
    content_xruns = int(content.get("xrun_count", 0) or 0)
    dac_xruns = int(dac.get("xrun_count", 0) or 0)
    xrun_warning = _outputd_xrun_rate_warning(content, dac)
    content_empty = int(content.get("empty_periods", 0) or 0)
    content_partial = int(content.get("partial_periods", 0) or 0)
    content_eagain = int(content.get("eagain_count", 0) or 0)
    frames = int(dac.get("frames_written", 0) or 0)
    bridge_detail, bridge_warning = _outputd_content_bridge_health(data)
    tts_raw = data.get("tts")
    tts = tts_raw if isinstance(tts_raw, dict) else {}
    tts_pending = int(tts.get("pending_frames", 0) or 0)
    tts_over_budget = bool(tts.get("over_budget", False))
    tts_over_budget_ms = int(tts.get("over_budget_ms", 0) or 0)
    tts_over_budget_streak_ms = int(
        tts.get("over_budget_streak_ms", 0) or 0
    )
    tts_max_pending = int(tts.get("max_pending_frames", 0) or 0)
    tts_dropped_commands = int(tts.get("dropped_commands", 0) or 0)
    tts_dropped_audio_frames = int(
        tts.get("dropped_audio_frames", 0) or 0
    )
    loudness_health = _outputd_loudness_health(data)
    if isinstance(loudness_health, CheckResult):
        return loudness_health
    loudness_detail = loudness_health
    if progress_age > 1000:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            "(work loop may be wedged; watchdog should fire soon)",
        )
    if dual_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {dual_warning}. {dual_detail.lstrip(', ')}",
        )
    if bridge_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            "active but rate-match content bridge reported anomalies: "
            f"{bridge_warning}. {bridge_detail}",
        )
    if tts_over_budget or tts_pending > 48000 * 2:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but tts.pending_frames={tts_pending} (>2s). "
            f"over_budget_streak_ms={tts_over_budget_streak_ms}. "
            "TTS producer may be outrunning outputd playback.",
        )
    if xrun_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {xrun_warning}. xruns={content_xruns}/{dac_xruns}. "
            "A sustained, recent xrun rate means audible dropouts — check "
            "CPU contention (jasper-camilla RT scheduling), DAC buffer sizing "
            "(JASPER_OUTPUTD_DAC_BUFFER_FRAMES), and "
            "`journalctl -u jasper-outputd | grep xrun`.",
        )
    status = "warn" if transport_evidence_warning else "ok"
    transport_detail = (
        f", {transport_evidence_warning}" if transport_evidence_warning else ""
    )
    return CheckResult(
        "jasper-outputd",
        status,
        f"active, backend=alsa, frames_written={frames}, "
        f"content_buffer_frames={content_buffer}, dac_buffer_frames={dac_buffer}, "
        f"xruns={content_xruns}/{dac_xruns}, "
        f"content_empty_periods={content_empty}, "
        f"content_partial_periods={content_partial}, "
        f"content_eagain_count={content_eagain}, "
        f"{local_pipe_detail}, "
        f"tts_pending_frames={tts_pending}, "
        f"tts_max_pending_frames={tts_max_pending}, "
        f"tts_over_budget_ms={tts_over_budget_ms}, "
        f"tts_dropped_commands={tts_dropped_commands}, "
        f"tts_dropped_audio_frames={tts_dropped_audio_frames}, "
        f"{bridge_detail}, "
        f"{reference_detail}, "
        f"{loudness_detail}, "
        f"progress_age_ms={progress_age}"
        f"{active_detail}"
        f"{dual_detail}"
        f"{ring_detail}"
        f"{transport_detail}",
    )

@doctor_check(order=52.6, group="audio")
def check_aec_clock_drift() -> CheckResult:
    """Surface the passive chip-AEC clock-drift estimate (Layer 0).

    Reads ``reference_outputs.aec_clock`` from outputd STATUS — the
    observe-only SRO (sample-rate-offset) estimator's verdict, ppm, and
    latency budget. This is purely diagnostic; no audio path depends on it.

      - skip (ok + "skipped — …") when outputd is disabled/inactive,
        STATUS is unreachable or invalid, the chip reference is not
        configured, or the aec_clock block is absent (pre-Layer-0 builds /
        no XVF reference). Mirrors the skip-if-not-configured idiom used by
        the other audio checks — a non-applicable probe is OK, not a fail.
      - warn only when sro_estimator_status == "untrusted" (the clock
        signal itself cannot be trusted right now).
      - ok otherwise: coherent, compensable (a real steady offset a future
        layer would compensate — the *expected* state on independent-clock
        DACs like the HiFiBerry), and observing (still measuring) are all
        healthy. Echoes the estimate and the latency budget.
    """
    label = "AEC clock drift"
    enabled = _run(
        ["systemctl", "is-enabled", "jasper-outputd.service"]
    ).stdout.strip()
    if enabled in {"not-found", "disabled", ""}:
        return CheckResult(label, "ok", "skipped — jasper-outputd not enabled")
    active = _run(
        ["systemctl", "is-active", "jasper-outputd.service"]
    ).stdout.strip()
    if active != "active":
        return CheckResult(label, "ok", "skipped — jasper-outputd not active")

    try:
        payload = _read_status_socket_bytes(_OUTPUTD_STATUS_SOCKET, timeout=2.0)
    except OSError as e:
        return CheckResult(label, "ok", f"skipped — STATUS unreachable: {e}")

    body = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return CheckResult(label, "ok", "skipped — STATUS returned invalid JSON")
    if not isinstance(data, dict):
        return CheckResult(
            label,
            "ok",
            f"skipped — STATUS root is {type(data).__name__}, expected object",
        )

    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(label, "ok", "skipped — STATUS missing reference_outputs")
    if reference_outputs.get("chip_ref_pcm") is None:
        return CheckResult(label, "ok", "skipped — chip reference not configured")
    chip_ref_writer = reference_outputs.get("chip_ref_writer")
    if isinstance(chip_ref_writer, dict) and not bool(
        chip_ref_writer.get("active", chip_ref_writer.get("enabled", False))
    ):
        writer_status = str(chip_ref_writer.get("status") or "unknown")
        recovery = (
            "outputd is retrying the optional AEC reference device"
            if writer_status in {"connecting", "degraded"}
            else "the reference worker stopped; correct the device/config and "
            "restart jasper-outputd"
        )
        return CheckResult(
            label,
            "warn",
            "chip reference is desired but unavailable; speaker playback remains "
            f"active and {recovery} "
            f"(status={writer_status}, "
            f"open_errors={chip_ref_writer.get('open_error_count')}, "
            f"retries={chip_ref_writer.get('retry_count')})",
        )
    aec_clock = reference_outputs.get("aec_clock")
    if not isinstance(aec_clock, dict):
        return CheckResult(
            label, "ok", "skipped — outputd build predates aec_clock observation"
        )

    verdict = aec_clock.get("verdict")
    status = aec_clock.get("sro_estimator_status")
    sro_ppm = aec_clock.get("chip_ref_sro_ppm")
    reason = aec_clock.get("verdict_reason")
    # Observe mode: the chip-ref writer was armed purely to MEASURE drift on
    # the software-AEC3 mic path (chip-AEC observe mode), not for production
    # chip-AEC. Surfaced so an operator can tell why the writer is running.
    observe = aec_clock.get("observe")
    latency = aec_clock.get("latency") or {}
    dac_ms = latency.get("dac_presentation_ms")
    playback_ms = latency.get("playback_queue_ms")
    chip_ref_ms = latency.get("chip_ref_queue_ms")
    detail = (
        f"verdict={verdict}, sro_estimator_status={status}, "
        f"observe={observe}, chip_ref_sro_ppm={sro_ppm}, "
        f"dac_presentation_ms={dac_ms}, playback_queue_ms={playback_ms}, "
        f"chip_ref_queue_ms={chip_ref_ms}"
    )
    # Warn only on a genuinely untrusted clock signal. "observing" (still
    # measuring at startup) and "compensable" (real drift a future layer
    # handles — expected on independent-clock DACs) are both healthy; warning
    # on them would cry wolf at boot and on exactly the DACs we target.
    if status == "untrusted":
        return CheckResult(
            label,
            "warn",
            f"chip-AEC clock drift cannot be trusted: {reason}. {detail}",
        )
    return CheckResult(label, "ok", detail)
