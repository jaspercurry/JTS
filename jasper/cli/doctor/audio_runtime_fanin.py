# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the jasper-fanin mixer and its ALSA wiring.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime_camilla`` -> ``_fanin`` -> ``_outputd`` -> ``_ring``, so this
module may import only from ``audio_runtime_camilla``.

Closed vocabulary for this module's `CheckResult.reason`: one snake_case
constant per distinct decision branch of the checks below, its value unique
across the doctor and prefixed by the check that emits it. `detail` stays the
human sentence (free to reword); `reason` is what tests and self-healing
consumers pin instead (ADR-0233 rule 3).

A branch that formed NO verdict — subsystem not installed, not applicable to
this box, or the evidence source unreachable so nothing was observed — is
`skipped` with a reason, never `ok`. An `ok` reason means an actual verdict a
consumer would branch on (a feature the box turned off, a floor that is
deliberately not renderable).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ...audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from ...camilla_config_contract import devices_playback_is_pipe
from ...fanin_coupling import read_declared_ring_wire_format
from ...route_latency.status_socket import FANIN_STALE_MS, FANIN_STATUS_SOCKET
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _service_state_failure
from .audio_runtime_camilla import _loaded_device_fields


REASON_FANIN_BINARY_MISSING = "fanin_binary_missing"
REASON_FANIN_BINARY_NOT_EXECUTABLE = "fanin_binary_not_executable"

REASON_ASOUND_CONF_MISSING = "asound_conf_missing"
REASON_ASOUND_CONF_UNREADABLE = "asound_conf_unreadable"
REASON_ASOUND_LEGACY_RENDERER_BLOCK = "asound_legacy_renderer_block"
REASON_ASOUND_LANE_MISSING = "asound_lane_missing"
REASON_ASOUND_LANE_WRONG_SLAVE = "asound_lane_wrong_slave"
REASON_ASOUND_LANE_WIDTH_SHEAR = "asound_lane_width_shear"
REASON_ASOUND_STALE_TOPOLOGY_STATE = "asound_stale_topology_state"
REASON_ASOUND_RING_WIRE_UNRESOLVED = "asound_ring_wire_unresolved"

REASON_FANIN_UNIT_MISSING = "fanin_unit_missing"
REASON_FANIN_UNIT_NOT_ENABLED = "fanin_unit_not_enabled"
REASON_FANIN_INACTIVE = "fanin_inactive"
REASON_FANIN_STATUS_UNREACHABLE = "fanin_status_unreachable"
REASON_FANIN_STATUS_MALFORMED = "fanin_status_malformed"
REASON_FANIN_STATUS_MISSING_OUTPUT = "fanin_status_missing_output"
REASON_FANIN_STATUS_MISSING_RING = "fanin_status_missing_ring"
REASON_FANIN_STATUS_MISSING_INPUTS = "fanin_status_missing_inputs"
REASON_FANIN_STATUS_MISSING_WATCHDOG = "fanin_status_missing_watchdog"
REASON_FANIN_STATUS_MISSING_INPUT_BUFFER = "fanin_status_missing_input_buffer"
REASON_FANIN_TRANSPORT_NOT_RING = "fanin_transport_not_ring"
REASON_FANIN_INPUTS_DRIFTED = "fanin_inputs_drifted"
REASON_FANIN_PROGRESS_STALE = "fanin_progress_stale"
REASON_FANIN_INPUT_BUFFER_UNDERSIZED = "fanin_input_buffer_undersized"
REASON_FANIN_LOUDNESS_TELEMETRY_MISSING = "fanin_loudness_telemetry_missing"

# One loudness contract, two publishers, one code each: the row's check name
# and its reason must not need reading together to tell them apart.
REASON_FANIN_ASSISTANT_GAIN_NOT_NUMERIC = "fanin_assistant_gain_not_numeric"
REASON_FANIN_ASSISTANT_GAIN_OFF_CONTRACT = "fanin_assistant_gain_off_contract"

REASON_FANIN_TTS_NOT_ENABLED = "fanin_tts_not_enabled"

# The secondary fan-in checks stand down when the STATUS socket is unreadable;
# the mandatory `jasper-fanin service` check owns that failure. One code per
# check, so a consumer can tell which row stood down.
REASON_FANIN_TTS_STATUS_NOT_PROBED = "fanin_tts_status_not_probed"
REASON_FANIN_TTS_LANE_DISABLED = "fanin_tts_lane_disabled"
REASON_FANIN_TTS_PROTOCOL_ERRORS = "fanin_tts_protocol_errors"
REASON_FANIN_TTS_AUDIO_DROPPED = "fanin_tts_audio_dropped"

REASON_FANIN_RING_STALL_STATUS_NOT_PROBED = "fanin_ring_stall_status_not_probed"
REASON_FANIN_RING_STALL_BLOCK_ABSENT = "fanin_ring_stall_block_absent"
REASON_FANIN_RING_STALL_ACTIVE = "fanin_ring_stall_active"

REASON_HOST_CLOCK_STATUS_NOT_PROBED = "host_clock_status_not_probed"
REASON_HOST_CLOCK_TELEMETRY_MISSING = "host_clock_telemetry_missing"
REASON_HOST_CLOCK_DISABLED = "host_clock_disabled"
REASON_HOST_CLOCK_ACTUATOR_TELEMETRY_MISSING = "host_clock_actuator_telemetry_missing"
REASON_HOST_CLOCK_PROBE_TELEMETRY_MISSING = "host_clock_probe_telemetry_missing"
REASON_HOST_CLOCK_ACTUATOR_UNAVAILABLE = "host_clock_actuator_unavailable"
REASON_HOST_CLOCK_L2_FALLBACK = "host_clock_l2_fallback"
REASON_HOST_CLOCK_PROBING = "host_clock_probing"

REASON_COUPLING_FILE_ABSENT = "coupling_file_absent"
REASON_COUPLING_DEVICES_UNPARSED = "coupling_devices_unparsed"
REASON_COUPLING_TOKEN_UNKNOWN = "coupling_token_unknown"
REASON_COUPLING_NO_LOADED_CAPTURE = "coupling_no_loaded_capture"
REASON_COUPLING_GRAPH_NOT_RING = "coupling_graph_not_ring"
REASON_COUPLING_ACTIVE_LADDER_PENDING = "coupling_active_ladder_pending"

REASON_ALOOP_NOT_LOADED = "aloop_not_loaded"
REASON_ALOOP_REGISTERED_SET_UNDERIVABLE = "aloop_registered_set_underivable"
REASON_ALOOP_PROC_UNREADABLE = "aloop_proc_unreadable"
REASON_ALOOP_UNREGISTERED_SUBSTREAM_OPEN = "aloop_unregistered_substream_open"


@doctor_check(order=49, group="audio")
def check_fanin_binary_installed() -> CheckResult:
    """The jasper-fanin Rust daemon ships as an installed binary at
    /opt/jasper/bin/jasper-fanin.
    """
    path = Path("/opt/jasper/bin/jasper-fanin")
    if not path.exists():
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} missing. Re-run install.sh; check cargo build "
            f"output for compilation errors.",
            reason=REASON_FANIN_BINARY_MISSING,
        )
    if not os.access(path, os.X_OK):
        return CheckResult(
            "jasper-fanin binary",
            "fail",
            f"{path} present but not executable. Run: "
            f"sudo chmod +x {path}",
            reason=REASON_FANIN_BINARY_NOT_EXECUTABLE,
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

    Not a general ALSA parser: a drift detector for our own generated file,
    where each top-level block ends at the next `pcm.`/`ctl.` definition.
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

#: The lane roster on a box with NO renderer lane armed (the shipped fleet shape).
_FANIN_EXPECTED_ALOOP_INPUTS = [
    ("spotify", "hw:Loopback,1,0"),
    ("airplay", "hw:Loopback,1,1"),
    ("bluealsa", "hw:Loopback,1,2"),
    ("usbsink", "hw:Loopback,1,3"),
    ("correction", "hw:Loopback,1,4"),
]


def _fanin_expected_inputs(
    lanes_env: str | None = None,
) -> list[tuple[str, str]]:
    """The `(label, pcm)` roster fan-in's STATUS should report on THIS box.

    An armed renderer-ingress lane reports its RING PATH as its `pcm`, so the
    armed set is read from the lane map (`jasper.renderer_lanes`) fan-in itself
    reads rather than compared against a hardcoded list.
    """
    from jasper import renderer_lanes as rl

    armed = (
        rl.read_armed_labels()
        if lanes_env is None
        else rl.read_armed_labels(lanes_env)
    )
    return [
        (label, rl.expected_fanin_lane_pcm(label, pcm, armed))
        for label, pcm in _FANIN_EXPECTED_ALOOP_INPUTS
    ]


# The assistant-loudness gain floor, and the only fixed bound the shared Rust
# engine applies. Its owner is MIN_TTS_GAIN_DB in
# rust/jasper-tts-protocol/src/loudness.rs; sanitize_tts_gain_db() floors there
# and maps a malformed value there too. tests/test_audio_safety_pins.py reads
# the Rust literal and fails if this copy drifts.
#
# There is deliberately NO fixed positive ceiling: a pre-DSP (fan-in) decision
# legitimately goes positive, pre-compensating for CamillaDSP's downstream
# attenuation. The enforced ceiling is dynamic and per-decision — the peak-aware
# cap (max_peak_dbfs - source_peak_dbfs), published next to the gain it limited.
# See docs/audio-paths.md "Hearing safety is peak-aware".
_ASSISTANT_GAIN_FLOOR_DB = -60.0
# Under today's publish paths the comparison below is EXACT (both sides round
# monotonically to 0.1 dB). This tolerance is a cushion against a future publish
# path that rounds differently, NOT a bound derived from the current arithmetic;
# a real clamp regression is dB-scale and clears it by an order of magnitude.
_ASSISTANT_GAIN_ROUNDING_DB = 0.15


def _assistant_gain_fault(loudness: dict[str, object]) -> str | None:
    """Return a one-clause WARN reason when ``final_gain_db`` breaks the shared
    loudness contract, else None.

    The contract is one line of Rust (``AssistantLoudness::decide_gain``):

        final = max(MIN_TTS_GAIN_DB, min(requested_gain, peak_cap_gain))

    A daemon too old to publish the two inputs is held to the floor alone.
    """

    def _f(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    final = _f(loudness.get("final_gain_db"))
    if final is None:
        return None
    if final < _ASSISTANT_GAIN_FLOOR_DB - _ASSISTANT_GAIN_ROUNDING_DB:
        return (
            f"final_gain_db={final} is below the {_ASSISTANT_GAIN_FLOOR_DB} dB "
            "gain floor"
        )
    requested = _f(loudness.get("requested_gain_db"))
    peak_cap = _f(loudness.get("peak_cap_gain_db"))
    if requested is None or peak_cap is None:
        return None
    expected = max(_ASSISTANT_GAIN_FLOOR_DB, min(requested, peak_cap))
    if abs(final - expected) > _ASSISTANT_GAIN_ROUNDING_DB:
        return (
            f"final_gain_db={final} but the decision it came from asked for "
            f"requested_gain_db={requested} under peak_cap_gain_db={peak_cap}, "
            f"so the contract value is {expected}"
        )
    return None


@doctor_check(order=50, group="audio")
def check_fanin_asound_wiring() -> CheckResult:
    """Verify the deployed ALSA graph is the fan-in graph.

    Scope is the RENDERER side of the snd-aloop graph only — drift detection
    against `deploy/alsa/asoundrc.jasper`. The ring (ADR-0100) is covered by
    `check_ring_platform_assets` and `check_ring_geometry_coherence`.
    """
    label = "fan-in ALSA wiring"
    path = Path("/etc/asound.conf")
    if not path.exists():
        return CheckResult(
            label,
            "fail",
            f"{path} missing — re-run install.sh",
            reason=REASON_ASOUND_CONF_MISSING,
        )
    try:
        text = path.read_text()
    except OSError as e:
        return CheckResult(
            label,
            "fail",
            f"can't read {path}: {e}",
            reason=REASON_ASOUND_CONF_UNREADABLE,
        )

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
            reason=REASON_ASOUND_LEGACY_RENDERER_BLOCK,
        )

    # No usbsink_substream write alias: USB audio is DIRECT-captured by
    # jasper-fanin from hw:UAC2Gadget. fan-in still READS the pair-3 capture side
    # as the usbsink lane's idle fallback, but nothing writes it.
    expected_aliases = {
        "librespot_substream": "hw:Loopback,0,0",
        "shairport_substream": "hw:Loopback,0,1",
        "bluealsa_substream": "hw:Loopback,0,2",
        CORRECTION_SUBSTREAM: "hw:Loopback,0,4",
    }
    # snd-aloop pins both halves of a cable to one format, and the reader half is
    # jasper-fanin, which opens every capture side at the box's one resolved
    # wire. So the expected lane width is that wire.
    try:
        wire = read_declared_ring_wire_format()
    except ValueError as e:
        return CheckResult(label, "fail", str(e), reason=REASON_ASOUND_RING_WIRE_UNRESOLVED)
    missing: list[str] = []
    wrong: list[str] = []
    sheared: list[str] = []
    for alias, slave in expected_aliases.items():
        block = _asound_pcm_block(active, alias)
        if block is None:
            missing.append(alias)
        elif (
            f'pcm "{slave}"' not in block
            or "rate 48000" not in block
            or "channels 2" not in block
        ):
            wrong.append(f"{alias}≠{slave}")
        elif f"format {wire}" not in block:
            # Reported apart from a wrong slave: the lane is wired correctly and
            # only its WIDTH disagrees, which has its own remedy.
            sheared.append(alias)
    if missing or wrong or sheared:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if wrong:
            parts.append("wrong slave " + ", ".join(wrong))
        if sheared:
            parts.append(
                f"lane width ≠ {wire} (this box's resolved wire): "
                + ", ".join(sheared)
            )
        # One row can carry several classes; the reason names the worst, so a
        # box whose only defect is a width shear says so.
        drift_reason = (
            REASON_ASOUND_LANE_MISSING if missing
            else REASON_ASOUND_LANE_WRONG_SLAVE if wrong
            else REASON_ASOUND_LANE_WIDTH_SHEAR
        )
        return CheckResult(
            label,
            "fail",
            "; ".join(parts) + f". Every slave must be 48000/2/{wire} over its "
            "own substream. Re-run deploy/install.sh to restore the fan-in "
            "asoundrc.",
            reason=drift_reason,
        )

    stale_state = Path("/var/lib/jasper/audio_topology.env")
    if stale_state.exists():
        return CheckResult(
            label,
            "warn",
            f"fan-in asoundrc is correct, but stale {stale_state} still "
            f"exists from the retired dmix/fanin switcher. Re-run "
            f"deploy/install.sh to archive/remove it.",
            reason=REASON_ASOUND_STALE_TOPOLOGY_STATE,
        )

    return CheckResult(label, "ok", "renderer/test lanes 0..4")

@doctor_check(order=51, group="audio")
def check_fanin_service() -> CheckResult:
    """The jasper-fanin systemd unit is required for renderer audio.

    Returns:
      - ok ("active, responding") when enabled and the UDS endpoint
        replies to STATUS with a fresh progress sentinel.
      - fail when disabled/inactive, when STATUS cannot be read, or
        when the live STATUS schema drifts from the production graph.
      - warn when enabled+active but the work loop is stale.
    """
    service_failure = _service_state_failure(
        "jasper-fanin service",
        "jasper-fanin.service",
        missing=REASON_FANIN_UNIT_MISSING,
        not_enabled=REASON_FANIN_UNIT_NOT_ENABLED,
        inactive=REASON_FANIN_INACTIVE,
    )
    if service_failure is not None:
        return service_failure

    status = evidence.fanin_status()
    if status.unreachable:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS probe at {FANIN_STATUS_SOCKET} failed: {status.error}. "
            f"Fan-in is mandatory; without STATUS doctor cannot verify "
            f"the live graph, buffers, or watchdog progress. "
            f"check: journalctl -u jasper-fanin | tail",
            reason=REASON_FANIN_STATUS_UNREACHABLE,
        )
    data = status.payload
    if data is None:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but UDS STATUS is unusable: {status.error}",
            reason=REASON_FANIN_STATUS_MALFORMED,
        )

    output = data.get("output", {})
    if not isinstance(output, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing output{}",
            reason=REASON_FANIN_STATUS_MISSING_OUTPUT,
        )
    # The ring is the only transport a running fan-in can be on (ADR-0100), so
    # the expectation is a constant, NOT a mapping from the persisted file:
    # deriving it from /var/lib/jasper/fanin.env would FAIL a healthy box whose
    # key has not been written yet (coupling-auto runs
    # After=jasper-fanin.service).
    actual_transport = output.get("transport")
    if actual_transport != "shm_ring":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.transport={actual_transport!r}; "
            "expected 'shm_ring' — the SHM ring is fan-in's only transport "
            "toward CamillaDSP. Check journalctl -u jasper-fanin for the "
            "transport it actually opened.",
            reason=REASON_FANIN_TRANSPORT_NOT_RING,
        )
    ring = output.get("ring")
    if not isinstance(ring, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS is missing output.ring metrics — "
            "fan-in is not actually writing Ring A. Check "
            "journalctl -u jasper-fanin for event=fanin.ring.opened.",
            reason=REASON_FANIN_STATUS_MISSING_RING,
        )

    inputs = data.get("inputs")
    if not isinstance(inputs, list):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing inputs[]",
            reason=REASON_FANIN_STATUS_MISSING_INPUTS,
        )
    actual_inputs = [
        (inp.get("label"), inp.get("pcm"))
        for inp in inputs
        if isinstance(inp, dict)
    ]
    expected_inputs = _fanin_expected_inputs()
    if actual_inputs != expected_inputs:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS inputs drifted. Expected "
            f"{expected_inputs!r}; got {actual_inputs!r}. "
            "Check /var/lib/jasper/fanin.env and "
            "/var/lib/jasper/renderer_lanes.env.",
            reason=REASON_FANIN_INPUTS_DRIFTED,
        )

    progress_age = data.get("watchdog", {}).get(
        "last_progress_age_ms", -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS response missing watchdog state",
            reason=REASON_FANIN_STATUS_MISSING_WATCHDOG,
        )
    if progress_age > FANIN_STALE_MS:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            f"(work loop may be wedged; watchdog should fire soon)",
            reason=REASON_FANIN_PROGRESS_STALE,
        )
    frames = output.get("frames_written", 0)
    xruns = output.get("xrun_count", 0)
    input_buffer_frames = data.get("input_buffer_frames")
    if not isinstance(input_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer input_buffer_frames",
            reason=REASON_FANIN_STATUS_MISSING_INPUT_BUFFER,
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
            reason=REASON_FANIN_INPUT_BUFFER_UNDERSIZED,
        )
    tts = data.get("tts", {})
    if not isinstance(tts, dict) or not bool(tts.get("enabled", False)):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but pre-DSP TTS socket is not enabled. Current "
            "production topology requires TTS/cues to enter jasper-fanin "
            "before CamillaDSP.",
            reason=REASON_FANIN_TTS_NOT_ENABLED,
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
            reason=REASON_FANIN_LOUDNESS_TELEMETRY_MISSING,
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
            reason=REASON_FANIN_ASSISTANT_GAIN_NOT_NUMERIC,
        )
    gain_fault = _assistant_gain_fault(loudness)
    if gain_fault is not None:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active with pre-DSP TTS enabled but "
            f"tts.assistant_loudness.{gain_fault}.",
            reason=REASON_FANIN_ASSISTANT_GAIN_OFF_CONTRACT,
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
            reason=REASON_HOST_CLOCK_TELEMETRY_MISSING,
        )
    if host_clock.get("enabled") is not True:
        return CheckResult(
            label,
            "ok",
            "disabled (no host-clock health claim)",
            reason=REASON_HOST_CLOCK_DISABLED,
        )

    ladder = str(host_clock.get("ladder") or "unknown")
    reason_value = host_clock.get("fallback_reason")
    reason = str(reason_value) if reason_value is not None else "none"
    actuator = host_clock.get("actuator")
    probe = host_clock.get("probe")
    if not isinstance(actuator, dict):
        return CheckResult(
            label,
            "warn",
            "enabled but actuator telemetry is missing",
            reason=REASON_HOST_CLOCK_ACTUATOR_TELEMETRY_MISSING,
        )
    if not isinstance(probe, dict):
        return CheckResult(
            label,
            "warn",
            "enabled but probe telemetry is missing",
            reason=REASON_HOST_CLOCK_PROBE_TELEMETRY_MISSING,
        )

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
            reason=REASON_HOST_CLOCK_ACTUATOR_UNAVAILABLE,
        )

    if ladder == "l2_fallback":
        return CheckResult(
            label,
            "warn",
            f"persistent L2 fallback: fallback_reason={reason}, "
            f"capture_generation={capture_generation}, control_generation={control_generation}; "
            f"{counters}. Stop/start creates a new session; a gadget generation "
            "change self-heals automatically.",
            reason=REASON_HOST_CLOCK_L2_FALLBACK,
        )

    phase = probe.get("phase")
    attempt = probe.get("attempt")
    max_attempts = probe.get("max_attempts")
    if ladder == "probing":
        # Await-lock, baseline, step and the single retry wait are bounded
        # acquisition states, not permanent failures.
        return CheckResult(
            label,
            "ok",
            f"recovering: phase={phase}, attempt={attempt}/{max_attempts}, "
            f"generations={capture_generation}/{control_generation}; {counters}",
            reason=REASON_HOST_CLOCK_PROBING,
        )

    return CheckResult(
        label,
        "ok",
        f"ladder={ladder}, generations={capture_generation}/{control_generation}, "
        f"probe_final={probe.get('final_result')}, retries={probe.get('retries')}; {counters}",
    )


@doctor_check(order=51.52, group="audio")
def check_fanin_host_clock() -> CheckResult:
    """Report persistent USB host-clock recovery/fallback with exact cause."""
    status = evidence.fanin_status()
    if status.payload is None:
        # Reachability belongs to the preceding mandatory fan-in service check.
        return CheckResult(
            "USB host clock",
            "skipped",
            f"not probed ({type(status.error).__name__}); "
            "see jasper-fanin service check",
            reason=REASON_HOST_CLOCK_STATUS_NOT_PROBED,
        )
    return _host_clock_health_from_status(status.payload)

@doctor_check(order=51.5, group="audio")
def check_fanin_tts_drops() -> CheckResult:
    """Report fan-in TTS protocol errors and pending-budget drops.

    fan-in's TTS lane drops whole audio commands that arrive while its bounded
    pending queue is full (it cannot block the socket reader without stalling
    barge-in FLUSH behind queued audio). The Python writer paces itself to stay
    under that budget (`_OUTPUTD_PACE_AHEAD_SEC` in jasper/audio_io.py), so a
    nonzero drop counter means assistant/cue audio audibly skipped.

    Returns:
      - ok when counters are zero, or the TTS lane is disabled in this
        topology.
      - skipped when STATUS is unreachable (reachability is owned by
        'jasper-fanin service').
      - warn when protocol errors or dropped audio > 0 since fan-in start.
    """
    name = "fan-in TTS delivery"
    status = evidence.fanin_status()
    data = status.payload
    if data is None:
        return CheckResult(
            name,
            "skipped",
            f"not probed ({type(status.error).__name__}); fan-in reachability is "
            "covered by the 'jasper-fanin service' check",
            reason=REASON_FANIN_TTS_STATUS_NOT_PROBED,
        )

    tts = data.get("tts")
    if not isinstance(tts, dict) or not tts.get("enabled"):
        return CheckResult(
            name,
            "ok",
            "TTS lane disabled in this topology",
            reason=REASON_FANIN_TTS_LANE_DISABLED,
        )

    dropped_frames = int(tts.get("dropped_audio_frames") or 0)
    dropped_commands = int(tts.get("dropped_commands") or 0)
    protocol_errors = int(tts.get("protocol_errors") or 0)
    if protocol_errors:
        return CheckResult(
            name,
            "warn",
            f"{protocol_errors} TTS socket protocol error(s) since fan-in "
            "start — assistant and cue audio may be mute. Check "
            "`journalctl -u jasper-fanin | grep tts_socket.protocol_error` "
            "for a voice/fan-in wire mismatch or malformed client.",
            reason=REASON_FANIN_TTS_PROTOCOL_ERRORS,
        )
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
        reason=REASON_FANIN_TTS_AUDIO_DROPPED,
    )


@doctor_check(order=51.55, group="audio")
def check_fanin_ring_stall() -> CheckResult:
    """A live fan-in→CamillaDSP ring stall (issue #1524).

    The ring is full AND CamillaDSP is not draining it (heartbeat-live but
    ``read_seq`` frozen, or the reader absent > 1 s). The writer self-recovers by
    DEMOTING the stuck reader to free-run so fan-in stays real time, but content
    drops. ``stall_active`` is true for exactly as long as the reader stays
    stuck, so it is the live/sustained signal.

    Reachability is owned by the 'jasper-fanin service' check.

    Returns:
      - ok when the ring is draining normally
      - skipped when STATUS is unreachable or carries no ring block
      - warn when a stall episode is CURRENTLY active, surfacing the stuck-reader
        vs no-reader drop split and the last stall duration.
    """
    name = "fan-in ring stall"
    status = evidence.fanin_status()
    data = status.payload
    if data is None:
        return CheckResult(
            name,
            "skipped",
            f"not probed ({type(status.error).__name__}); fan-in reachability is "
            "covered by the 'jasper-fanin service' check",
            reason=REASON_FANIN_RING_STALL_STATUS_NOT_PROBED,
        )

    output = data.get("output")
    ring = output.get("ring") if isinstance(output, dict) else None
    if not isinstance(ring, dict):
        # A running fan-in always publishes a ring block (ADR-0100); the missing
        # block itself is FAILed by the 'jasper-fanin service' check.
        return CheckResult(
            name,
            "skipped",
            "STATUS carries no output.ring block; the 'jasper-fanin service' "
            "check owns that failure",
            reason=REASON_FANIN_RING_STALL_BLOCK_ABSENT,
        )

    stuck = int(ring.get("stuck_reader_drops") or 0)
    no_reader = int(ring.get("drop_no_reader") or 0)
    last_ms = int(ring.get("last_stall_ms") or 0)
    clockless = int(ring.get("clockless_paces") or 0)
    counts = (
        f"stuck_reader_drops={stuck}, drop_no_reader={no_reader}, "
        f"last_stall_ms={last_ms}, clockless_paces={clockless}"
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
            reason=REASON_FANIN_RING_STALL_ACTIVE,
        )
    return CheckResult(name, "ok", f"no active stall ({counts})")


@doctor_check(order=51.68, group="audio")
def check_fanin_coupling_value() -> CheckResult:
    """The persisted fan-in coupling must be a RECOGNIZED token.

    jasper-fanin REFUSES an unrecognized value at start (exit 78) and the
    ``--auto`` reconciler converges it; this surfaces the stale value until that
    pass runs. An ABSENT key is not that state: fan-in serves the ring for it
    (ADR-0100), so a box the reconciler has not written yet is ``ok``.
    """
    from jasper.fanin.ring_health import FANIN_ENV_PATH, persisted_coupling_feeds_ring
    from jasper.fanin_coupling import COUPLING_ENV_VAR, COUPLING_SHM_RING
    from jasper.env_file import read_value

    label = "fan-in coupling value"
    try:
        text = Path(FANIN_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        return CheckResult(
            label, "ok", f"no fanin.env — fan-in serves {COUPLING_SHM_RING}",
            reason=REASON_COUPLING_FILE_ABSENT,
        )
    # The raw token is read for the MESSAGE only; the verdict is the shared
    # predicate's, so this surface cannot drift from what fan-in serves.
    raw = read_value(text, COUPLING_ENV_VAR)
    if not persisted_coupling_feeds_ring(text=text):
        return CheckResult(
            label,
            "warn",
            f"{COUPLING_ENV_VAR}={raw!r} in {FANIN_ENV_PATH} names a removed/unknown "
            "transport — the ring is the only one. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile --auto to converge the box and clean "
            "the file.",
            reason=REASON_COUPLING_TOKEN_UNKNOWN,
        )
    return CheckResult(
        label,
        "ok",
        f"{COUPLING_ENV_VAR}={raw or f'(unset → {COUPLING_SHM_RING})'}",
    )


def _requires_roleful_graph() -> bool:
    """Does the saved topology need a per-driver (crossover) graph?

    NOT a ``@doctor_check`` — a plain helper, and it must stay above the next
    decorated function rather than between a decorator and its target.

    Fail-soft to False: it only ever softens a message, never gates anything.
    Every caller that ACTS on rolefulness reads the fail-CLOSED loaders instead.
    """
    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        # The STRICT loader, not `evidence.output_topology()`: this one raises
        # on a torn/absent file instead of fail-softing, which is what the
        # `except` below classifies. Memoized so it stays one read per run.
        topology = evidence.get("output_topology_strict", load_output_topology_strict)
        return bool(classify_output_contract(topology).requires_roleful_graph)
    except (OutputTopologyError, OSError, ValueError):
        return False


@doctor_check(order=51.7, group="audio")
def check_fanin_coupling() -> CheckResult:
    """The loaded CamillaDSP graph must name this box's ring devices.

    Since ADR-0100 the capture axis has ONE expectation, ``jts_ring_capture``
    (Ring A). The playback axis has two legal endpoints: the post-DSP ring this
    box's endpoint marker names (``jts_ring_playback``, or
    ``jts_ring_active_playback`` once the active endpoint is armed), or the
    Snapcast pipe a bonded LEADER feeds instead of any local ring.

    KEYED ON THE LOADED GRAPH, never on ``JASPER_FANIN_CAMILLA_COUPLING``: a
    running fan-in is on the ring whatever that file says, and a healthy box's
    key may not be written yet (coupling-auto runs
    ``After=jasper-fanin.service``). The file's own legacy-token question
    belongs to :func:`check_fanin_coupling_value`, and whether outputd consumes
    what this graph writes to :func:`check_ring_split_transport`.
    """
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
        RING_PLAYBACK_DEVICE,
        ring_active_endpoint_armed,
    )
    from jasper.multiroom.reconcile import SNAPFIFO

    label = "fan-in coupling"
    active_path = evidence.camilla_config_path()
    config_path = Path(active_path or "/var/lib/camilladsp/configs/sound_current.yml")
    devices = _loaded_device_fields(config_path)
    if not devices and config_path.exists():
        # A config IS loaded and its devices block did not parse (an absent or
        # duplicated top-level `devices:` key, or a file this process cannot
        # read). Not "no config yet": every coupling axis below is unjudged.
        return CheckResult(
            label,
            "warn",
            f"loaded config {config_path} yields no devices block, so the "
            "capture and playback axes cannot be judged. Check the file's "
            "top-level devices: key, then run: sudo /opt/jasper/.venv/bin/"
            "jasper-sound reconcile-current-dsp",
            reason=REASON_COUPLING_DEVICES_UNPARSED,
        )
    capture = devices.get("capture_type")
    if capture is None:
        # No JTS config loaded yet (fresh box / non-JTS graph).
        return CheckResult(
            label,
            "skipped",
            "no loaded capture to compare",
            reason=REASON_COUPLING_NO_LOADED_CAPTURE,
        )

    # WHICH post-DSP ring is EXACTLY ONE answer, taken from the reconciler's
    # marker — not "either is fine". Accepting both would read green through the
    # crossing this rung exists to prevent: a roleful box's graph pointed at the
    # full-range stereo ring, or a stereo box's at the active ring.
    armed = ring_active_endpoint_armed()
    expected_playback = RING_ACTIVE_PLAYBACK_DEVICE if armed else RING_PLAYBACK_DEVICE
    # A ROLEFUL box with a CLEARED marker has no honest stereo expectation:
    # jts_ring_playback is a FORBIDDEN token for every active emitter, so naming
    # it as expected would send an operator to a device the emitters refuse to
    # write. Such a box is mid-arm, and the remedy is the ladder, not a re-arm.
    roleful = _requires_roleful_graph()
    capture_device = devices.get("capture_device")
    playback_device = devices.get("playback_device")
    ring_mismatches: list[str] = []
    if capture != "Alsa" or capture_device != RING_CAPTURE_DEVICE:
        ring_mismatches.append(
            f"capture={capture}/{capture_device or '(missing)'} "
            f"(expected Alsa/{RING_CAPTURE_DEVICE})"
        )
    # A bonded LEADER's camilla#1 writes the Snapcast pipe and reaches no ring
    # device at all, so the playback axis is this check's business only on the
    # ring endpoint.
    feeds_the_bond = devices_playback_is_pipe(devices, SNAPFIFO)
    if not feeds_the_bond and playback_device != expected_playback:
        if roleful and not armed:
            ring_mismatches.append(
                f"playback_device={playback_device or '(missing)'} "
                f"(this box is roleful and its endpoint marker is CLEAR, so "
                f"no ring is expected here at all — {RING_PLAYBACK_DEVICE} "
                "carries a full-range stereo program an active graph may "
                "never target)"
            )
        else:
            ring_mismatches.append(
                f"playback_device={playback_device or '(missing)'} "
                f"(expected {expected_playback})"
            )
    if not ring_mismatches:
        endpoint = SNAPFIFO if feeds_the_bond else expected_playback
        return CheckResult(
            label,
            "ok",
            f"capture={RING_CAPTURE_DEVICE}, playback={endpoint}",
        )
    # Severity stays WARN: under the arm ladder the graph moves first and the
    # marker is re-derived second, so a box observed between those steps is
    # exactly this state and is not broken.
    if roleful:
        # The first two steps are the SAME ladder the transport-park check
        # records, composed from its constant rather than respelled.
        from ...control.transport_park import ACTIVE_ENDPOINT_REMEDY

        coupling_reason = REASON_COUPLING_ACTIVE_LADDER_PENDING
        recovery = (
            f"the ACTIVE-ring ladder, in order: {ACTIVE_ENDPOINT_REMEDY} && "
            "sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring"
        )
    else:
        coupling_reason = REASON_COUPLING_GRAPH_NOT_RING
        recovery = (
            "run: sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile "
            "shm_ring"
        )
    return CheckResult(
        label,
        "warn",
        "the loaded graph is not this box's ring config: "
        f"{'; '.join(ring_mismatches)}; a stale baseline artifact re-seeded on a "
        f"camilla restart is the usual cause; {recovery}",
        reason=coupling_reason,
    )


# ---------------------------------------------------------------------------
# The aloop-remnant guard (ADR-0100).
#
# WHAT IT ASSERTS. Every OPEN aloop substream has a REGISTERED purpose, where
# the registered set is DERIVED — never restated — from the one place that
# still owns a pair allocation:
#
#   pairs 0-4  `_FANIN_EXPECTED_ALOOP_INPUTS`   (above, this module)
#
# Deriving rather than tabulating makes retirement MECHANICAL: a pair stops
# being registered the moment its owning constant stops naming it. The roster is
# read in its ALOOP form, not through `_fanin_expected_inputs()`, because a
# ring-armed renderer lane still RESERVES its aloop pair, so the registered set
# must not flap with arming state.
#
# Pairs 5, 6 and 7 are absent because no owner names them: their PCM
# definitions are gone, so an open pair in that range has resurrected a
# deleted lane. That is the FAIL. They stay reserved
# rather than reclaimed, per deploy/modprobe.d/snd-aloop.conf: pcm_substreams
# stays 8 so no surviving pair renumbers.
#
# BOUNDED. At most 4 PCM directories x `_ALOOP_SUBSTREAMS` status reads (32
# small procfs files), plus one `comm`/`cgroup` read per offender, capped at
# `_ALOOP_OFFENDER_DETAIL_CAP` offenders in the message.
#
# FAIL-SOFT. An unreadable /proc is `warn`, never an exception and never a FAIL.
#
# SERIALIZED. `exclusive_group="audio-probe"` shares a lane with
# `check_renderer_device_resolvable` (renderers.py), which opens real PCMs with
# `aplay`, so an unregistered pair briefly held open by that probe would read as
# an offender.
# ---------------------------------------------------------------------------

#: procfs ALSA root. Overridable for tests through the same
#: `JASPER_ASOUND_ROOT` hook deploy/bin/jasper-camilla-recover and
#: jasper/cli/xvf_profile.py use.
_ALOOP_PROC_ROOT_ENV = "JASPER_ASOUND_ROOT"
_ALOOP_PROC_ROOT_DEFAULT = "/proc/asound"

#: The snd-aloop card id, pinned by deploy/modprobe.d/snd-aloop.conf
#: (`id=Loopback`).
_ALOOP_CARD_ID = "Loopback"

#: snd-aloop exposes two PCM devices, each with a playback and a capture side.
_ALOOP_PCM_DIRS = ("pcm0p", "pcm0c", "pcm1p", "pcm1c")

#: `pcm_substreams=8` in deploy/modprobe.d/snd-aloop.conf — pairs 0..7. Pinned
#: against that file by tests/test_doctor_audio_runtime_fanin.py.
_ALOOP_SUBSTREAMS = 8

#: Cap on how many offenders are spelled out in the FAIL detail, so a
#: pathological box cannot produce an unbounded doctor line.
_ALOOP_OFFENDER_DETAIL_CAP = 4


def _aloop_proc_root() -> Path:
    return Path(
        os.environ.get(_ALOOP_PROC_ROOT_ENV, _ALOOP_PROC_ROOT_DEFAULT)
    )


def _pair_from_loopback_pcm(pcm: str) -> int | None:
    """Substream index from an ``hw:Loopback,<device>,<sub>`` name.

    Returns None for anything that is not an snd-aloop hw triple — a ring path,
    a plug wrapper, a renamed card. Callers treat None as "cannot derive" and
    degrade to `warn`; they must never treat it as "this pair is not
    registered", because that would SHRINK the registered set and turn a
    healthy box red.
    """
    m = re.fullmatch(r"hw:([^,]+),\d+,(\d+)", pcm.strip())
    if not m or m.group(1) != _ALOOP_CARD_ID:
        return None
    return int(m.group(2))


def _derive_registered_pairs() -> dict[int, str] | None:
    """``{pair: provenance}`` derived from the owning facts (see header comment).

    Returns None if ANY source entry is unparseable — all-or-nothing, because a
    partial derivation would shrink the registered set and turn legitimate
    holders into doctor FAILs.
    """
    registered: dict[int, str] = {}

    for label, pcm in _FANIN_EXPECTED_ALOOP_INPUTS:
        pair = _pair_from_loopback_pcm(pcm)
        if pair is None:
            return None
        registered[pair] = f"fan-in input lane {label!r}"

    return registered


def _aloop_substream_owner(status_text: str) -> str:
    """Best-effort ``pid=N comm=… cgroup=…`` for an open substream.

    Returns ``""`` when nothing is readable: a procfs race (the owner exits
    between the status read and the comm read) degrades to LESS DETAIL, never to
    a changed status.
    """
    m = re.search(r"owner_pid\s*:\s*(\d+)", status_text)
    if not m:
        return ""
    pid = m.group(1)
    parts = [f"pid={pid}"]
    for name, path in (
        ("comm", Path(f"/proc/{pid}/comm")),
        ("cgroup", Path(f"/proc/{pid}/cgroup")),
    ):
        try:
            value = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not value:
            continue
        if name == "cgroup":
            # Keep only the leaf unit; the full cgroup path is long.
            value = value.splitlines()[-1].rsplit("/", 1)[-1]
        parts.append(f"{name}={value}")
    return " ".join(parts)


@doctor_check(
    order=75.96,
    group="audio",
    exclusive_group="audio-probe",
)
def check_aloop_registered_substreams() -> CheckResult:
    """snd-aloop is loaded only for the bounded aloop remnant this check
    measures — nothing else holds it.

    See the header comment above for the rationale; the statuses:

    - snd-aloop absent            -> skipped (no remnant on this box)
    - registered set underivable  -> warn (never a shrunken set)
    - /proc unreadable            -> warn (fail-soft; never a FAIL)
    - every open pair registered  -> ok, reporting the remnant's current size
    - an UNREGISTERED pair open   -> fail, naming the offender
    """
    label = "aloop remnant"

    card_dir = _aloop_proc_root() / _ALOOP_CARD_ID
    if not card_dir.is_dir():
        return CheckResult(
            label,
            "skipped",
            f"snd-aloop not loaded ({card_dir} absent) — no aloop remnant "
            "on this box",
            reason=REASON_ALOOP_NOT_LOADED,
        )

    derived = _derive_registered_pairs()
    if derived is None:
        return CheckResult(
            label,
            "warn",
            "could not derive the registered substream set from its owning "
            "constant (_FANIN_EXPECTED_ALOOP_INPUTS in this module) — each "
            "entry must be an 'hw:Loopback,<device>,<sub>' triple. The "
            "remnant's scope cannot be verified.",
            reason=REASON_ALOOP_REGISTERED_SET_UNDERIVABLE,
        )
    registered_pairs = derived

    offenders: list[str] = []
    registered_open: set[int] = set()
    unreadable = 0
    scanned = 0

    for pcm_dir in _ALOOP_PCM_DIRS:
        for pair in range(_ALOOP_SUBSTREAMS):
            status_path = card_dir / pcm_dir / f"sub{pair}" / "status"
            try:
                text = status_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except FileNotFoundError:
                # A substream this kernel does not expose is a narrower module,
                # not a fault.
                continue
            except OSError:
                unreadable += 1
                continue
            scanned += 1
            stripped = text.strip()
            first = stripped.splitlines()[0].strip() if stripped else ""
            if not first or first == "closed":
                continue
            if pair in registered_pairs:
                registered_open.add(pair)
                continue
            owner = _aloop_substream_owner(text)
            offenders.append(
                f"{pcm_dir}/sub{pair} ({first})"
                + (f" {owner}" if owner else " owner unreadable")
            )

    if scanned == 0:
        return CheckResult(
            label,
            "warn",
            f"snd-aloop card present at {card_dir} but no substream status "
            f"was readable ({unreadable} read error(s)) — the remnant's scope "
            "could not be verified",
            reason=REASON_ALOOP_PROC_UNREADABLE,
        )

    if offenders:
        shown = offenders[:_ALOOP_OFFENDER_DETAIL_CAP]
        more = len(offenders) - len(shown)
        suffix = f" (+{more} more)" if more > 0 else ""
        return CheckResult(
            label,
            "fail",
            "snd-aloop substream(s) open with no registered purpose in this "
            f"phase: {'; '.join(shown)}{suffix}. Only fan-in's five capture "
            "lanes (pairs 0-4) are registered; pairs 5, 6 and 7 have no PCM "
            "definitions left to open (ADR-0100 moved the content lane and the "
            "summed music output to SHM rings). "
            "A holder there means a rolled-back binary or a stale "
            "/etc/asound.conf resurrected a deleted lane. Identify the process "
            "above, stop it, and re-run `bash scripts/deploy-to-pi.sh` to "
            "restore the shipped ALSA config.",
            reason=REASON_ALOOP_UNREGISTERED_SUBSTREAM_OPEN,
        )

    open_pairs = sorted(registered_open)
    detail = (
        f"scoped: {len(registered_pairs)} of {_ALOOP_SUBSTREAMS} pairs "
        f"still registered (fan-in's capture lanes); no aloop remnant on the "
        "program path"
    )
    if open_pairs:
        detail += (
            "; open pairs held by registered owners: "
            f"{open_pairs}"
        )
    else:
        detail += "; no pair currently open"
    if unreadable:
        detail += f"; {unreadable} substream status file(s) unreadable"
    return CheckResult(label, "ok", detail)
