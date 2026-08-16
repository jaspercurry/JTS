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
# Every PCM the ring conf.d defines, with the tool that probes its direction and
# the ring file it names. The THIRD entry is the ACTIVE ring — a roleful box's
# post-crossover per-driver hop. It is listed for the same reason the other two
# are: the conf.d defines it on every box, so an inert box's open-probe must
# prove it resolves, and a missing entry would leave one shipped PCM unprobed.
#
# The PCM NAMES and the ring FILENAMES are taken from ``jasper.ring_assets``,
# which already owns both facts for the renderer, the reconciler and the stale-
# file guard. Only the probe TOOL is local — it is a doctor concern (which of
# arecord/aplay opens this direction) and belongs to nobody else. Spelling the
# names again here would have made the doctor a second place to update when a
# ring is added; the assertion below is what keeps the derived order honest.
_JTS_RING_PCMS = (
    (ring_assets.RING_A_CONF_PCM, "arecord", os.path.basename(ring_assets.RING_A_PROGRAM_FILE)),
    (ring_assets.RING_B_CONF_PCM, "aplay", os.path.basename(ring_assets.RING_B_CONTENT_FILE)),
    (
        ring_assets.RING_ACTIVE_CONF_PCM,
        "aplay",
        os.path.basename(ring_assets.RING_ACTIVE_CONTENT_FILE),
    ),
)
# One home for "which blocks exist", proven rather than assumed: the doctor's
# table must cover exactly the conf.d's blocks, in the same order.
assert tuple(name for name, _tool, _ring in _JTS_RING_PCMS) == ring_assets.RING_CONF_PCMS

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

#: The lane roster on a box with NO renderer lane armed — the shipped fleet
#: shape, and the one this check compared against unconditionally before U3.
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

    A renderer-ingress lane (U3 / P6) reports its RING PATH as its `pcm`,
    because that is where its audio actually comes from — so on an armed box
    the roster legitimately differs from the shipped aloop one, and comparing
    against a hardcoded list would diagnose a correctly-armed box as drifted.

    The armed set is read from the lane map (`jasper.renderer_lanes`), which is
    the same file fan-in itself reads, so this check's expectation and the
    daemon's behaviour come from ONE source. That is also what keeps the drift
    check meaningful: a lane whose STATUS `pcm` disagrees with what the map
    says is a real fault, and still fails.
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


# The assistant-loudness gain floor, and the only fixed bound the shared Rust
# engine applies. Its owner is MIN_TTS_GAIN_DB in
# rust/jasper-tts-protocol/src/loudness.rs; sanitize_tts_gain_db() floors there
# and maps a malformed value there too. tests/test_audio_safety_pins.py reads
# the Rust literal and fails if this copy drifts.
#
# There is deliberately NO fixed positive ceiling: the fixed -6 dB ceiling was
# removed in "Remove fixed TTS gain ceiling" (2026-07-01) because it pinned
# quiet, peaky voices below music. A pre-DSP (fan-in) decision legitimately goes
# positive — it pre-compensates for CamillaDSP's downstream attenuation. The
# ceiling that IS enforced is dynamic and per-decision: the peak-aware cap
# (max_peak_dbfs - source_peak_dbfs), which STATUS publishes next to the gain it
# limited. See docs/audio-paths.md "Hearing safety is peak-aware".
_ASSISTANT_GAIN_FLOOR_DB = -60.0
# Under today's publish paths the comparison below is EXACT: both round
# monotonically to 0.1 dB, min/max commute with any monotone map, and the floor
# is fixed under that rounding — so the value recomputed from the published
# inputs equals the published final_gain_db, with no residue. Fuzzing both paths
# (fan-in's pack-x10-then-format and outputd's format, under both tie rules)
# put the worst disagreement at 0.000000 dB over 400k samples each. This
# tolerance is therefore a deliberate cushion against a future publish path that
# rounds differently, NOT a bound derived from the current arithmetic; a real
# clamp regression is dB-scale and clears it by an order of magnitude.
_ASSISTANT_GAIN_ROUNDING_DB = 0.15


def _assistant_gain_fault(loudness: dict[str, object]) -> str | None:
    """Return a one-clause WARN reason when ``final_gain_db`` breaks the shared
    loudness contract, else None.

    The contract is one line of Rust (``AssistantLoudness::decide_gain``):

        final = max(MIN_TTS_GAIN_DB, min(requested_gain, peak_cap_gain))

    Both daemons publish all three numbers from the SAME decision under one
    seqlock, so the doctor asserts the relation rather than a magic range —
    that keeps the peak cap, not a literal, as the single owner of "how loud may
    the assistant get". A daemon too old to publish the two inputs is held to
    the floor alone.
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

    This catches the exact split-brain failure that silences a
    `loopback`-coupled box: renderers and jasper-fanin are running in
    fan-in mode, but /etc/asound.conf still points `pcm.jasper_capture`
    at the old substream 0 instead of the summed fan-in output on
    substream 7 — so CamillaDSP captures a private renderer lane, or
    fails to open one at all, instead of the summed program.

    It is not an AEC check. Since U4/P7-1 the AEC bridge's reference is
    jasper-outputd's UDP speaker monitor, and since U4/P7-2 so is
    jasper-aec-tune's; neither opens this tap. Every ALSA-graph
    assertion below is file-level drift detection against
    `deploy/alsa/asoundrc.jasper`, which is why they hold on a
    ring-coupled box too — there the shipped definitions survive with
    no writer and no reader. (The pair-5 definitions are already gone:
    P9-C deleted them. What survives is the pair-0..4/6/7 set, which
    P9-E reduces to the grouping pair.) The one assertion
    that is not about the graph is the trailing `audio_topology.env`
    probe, which warns about a leftover from the retired dmix/fanin
    switcher.
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
    # lane's idle fallback — see _FANIN_EXPECTED_ALOOP_INPUTS above — but nothing writes it.
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
            "pcm.jasper_capture missing — CamillaDSP has no capture "
            "device on a loopback-coupled box.",
        )
    if 'pcm "hw:Loopback,1,7"' not in capture:
        detail = (
            "pcm.jasper_capture must dsnoop hw:Loopback,1,7 "
            "(jasper-fanin's summed output)."
        )
        if 'pcm "hw:Loopback,1,0"' in capture:
            detail += (
                " It currently points at substream 0, which is now a "
                "private fan-in input lane and can make the tap's "
                "readers fail with EBUSY."
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

    # `pcm.jasper_ref` has had NO reader since U4/P7-3 retired the last one
    # (the AEC bridge's ALSA fallback went first, in P7-1). The definition is
    # deliberately retained — `deploy/alsa/asoundrc.jasper` still ships it, and
    # P9-E is what reduces the remaining aloop PCMs to the grouping pair — so
    # what these two branches report is deployed-asoundrc drift from the
    # shipped file, not a broken consumer.
    ref = _asound_pcm_block(active, "jasper_ref")
    if ref is None:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref missing — deploy/alsa/asoundrc.jasper still "
            "ships this plug wrapper, so the deployed asoundrc has "
            "drifted from it. Re-run install.sh.",
        )
    if 'slave.pcm "jasper_capture"' not in ref:
        return CheckResult(
            label,
            "fail",
            "pcm.jasper_ref must plug-wrap pcm.jasper_capture; the "
            "deployed block wraps something else.",
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
    # intent so a coupling that failed to restart onto the box is caught.
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
    # output.pcm is a CONFIG echo (state.rs pushes self.output_pcm), not proof a
    # PCM was opened. On a loopback-coupled box it is the real output device. On
    # shm_ring it is vestigial: U4/P7-4 dropped the lane-7 aloop mirror, so fan-in
    # opens no ALSA playback PCM at all there — the value is still reported
    # because the Coupling::Loopback arm still needs it, and checking it keeps the
    # loopback box honest. If a later phase stops configuring output_pcm on a ring
    # box, this equality is what will fail first; scope it per-coupling then.
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
    expected_inputs = _fanin_expected_inputs()
    if actual_inputs != expected_inputs:
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS inputs drifted. Expected "
            f"{expected_inputs!r}; got {actual_inputs!r}. "
            "Check /var/lib/jasper/fanin.env and "
            "/var/lib/jasper/renderer_lanes.env.",
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
    gain_fault = _assistant_gain_fault(loudness)
    if gain_fault is not None:
        return CheckResult(
            "jasper-fanin service",
            "warn",
            f"active with pre-DSP TTS enabled but "
            f"tts.assistant_loudness.{gain_fault}.",
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


def _loaded_playback_device(config_path: Path) -> str | None:
    """The ``devices.playback.device`` of a CamillaDSP config, or None."""
    return _loaded_device_field(config_path, "playback", "device")


def _expected_playback_format(
    playback_type: str | None, playback_device: str | None
) -> tuple[str, str]:
    """``(expected_format, constant_name)`` for a loaded config's playback lane.

    Three lanes, three owners of the width — see
    :func:`check_camilla_playback_format` for why each is what it is.

    The first two predicates are DISJOINT in every reachable config, so their
    order is belt only, not load-bearing: a ``File`` sink names its target with
    ``filename`` and carries no ``device`` key at all, so ``playback_device`` is
    ``None`` on every pipe/parked graph. Do not read the sequence here as a
    precedence rule that something depends on — a mutation swapping the two
    kills no test, precisely because no config can satisfy both.
    """
    from jasper.camilla_config_contract import (
        DEFAULT_PIPE_SINK_FORMAT,
        DEFAULT_PLAYBACK_FORMAT,
    )
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_ring_wire,
    )

    if playback_type == "File":
        return DEFAULT_PIPE_SINK_FORMAT, "DEFAULT_PIPE_SINK_FORMAT"
    # MEMBERSHIP over every ring device, not one `==`. Both rings carry the
    # resolved ring WIRE format — that axis is one per box, shared by all three
    # ring ends — so a device-specific answer would be wrong, while omitting the
    # active ring would fall through to DEFAULT_PLAYBACK_FORMAT and red-line a
    # perfectly healthy armed box whenever the two differ. Since the ring wire's
    # resolver defaults wide they no longer differ on an undeclared box; an
    # operator's narrow pin is what separates them, and this branch is what
    # keeps such a box green.
    if playback_device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        # Named for the RESOLVER, not for a constant: the ring's width is a
        # per-box resolution, and a detail line citing a constant would send a
        # reader to a value that is only one of the answers it can give.
        return resolve_ring_wire().sample_format, "resolve_ring_wire"
    return DEFAULT_PLAYBACK_FORMAT, "DEFAULT_PLAYBACK_FORMAT"


@doctor_check(order=51.75, group="audio")
def check_camilla_playback_format() -> CheckResult:
    """The loaded CamillaDSP config's declared playback format must match its
    LANE's expected format (wide-output-path program, D-list survey finding
    1): today NOTHING read the emitted playback format back off a live
    config, so a half-flip — an emitter regenerating a config against one
    value of the expected constant while the running process (or an
    un-reconciled stale file) reflects another — was silent rather than a red
    doctor line.

    LANE-AWARE, and there are THREE lanes, not two — the width has three
    separate owners and this check must ask the right one
    (:func:`_expected_playback_format`):

    - a ``File`` sink (the bonded-leader pipe, or the active-speaker parked
      graph's ``/dev/null``) expects ``DEFAULT_PIPE_SINK_FORMAT`` — pinned
      narrow by the snapserver wire contract (D4) regardless of the general
      program lane;
    - the SHM ring's playback device (``RING_PLAYBACK_DEVICE``) expects the ring
      wire ``resolve_ring_wire`` resolves — an ARMED RING IS ``type: Alsa``, so
      the File split alone does not cover it. Its width comes from the coupling's
      own kwargs (``capture_kwargs_for_coupling``), which read that same
      resolver, which is exactly why a ring-coupled box keeps a coherent lane on
      a box whose general default is wide (the PR-6 ring ruling). The ring
      LAYOUT accepts S16LE and S32LE, so this check is what catches a ring
      config that drifted to the other one — the attach would not;
    - every other sink — the ALSA loopback lane, every real active-speaker DAC
      graph — expects ``DEFAULT_PLAYBACK_FORMAT``.

    Miss either split and this check red-lines a HEALTHY box with a remediation
    that regenerates the identical config: without the File split, every
    pipe-sink leader and parked box; without the ring split, every armed-ring box
    (including the certified-latency USB box), whose canary criterion is
    literally "doctor green". The pipe/File sink stays pinned narrow
    (``DEFAULT_PIPE_SINK_FORMAT`` ``S16_LE``) regardless of the ring wire flip,
    so it still diverges in force from the loopback lane's
    ``DEFAULT_PLAYBACK_FORMAT`` (``S32_LE``). The ring split no longer buys a
    THIRD distinct value on an undeclared box — the ring wire's resolver
    defaults wide too now, so an unpinned ring box's expected format equals
    the loopback lane's — but the split stays load-bearing because the ring's
    expected value is a per-box RESOLUTION (``resolve_ring_wire``), not a
    constant: an operator's narrow rollback pin
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) is exactly the box this check
    must still catch, and reading it off ``DEFAULT_PLAYBACK_FORMAT`` instead
    would miss that box entirely.

    Keyed on the LOADED CONFIG's own ``device``/``type``, not on the persisted
    coupling: this check's one job is "does the config on disk match what its own
    lane should be", so introducing a second source (``read_persisted_coupling``)
    would let a box mid-arm — coupling flipped, config not yet swapped — read as
    healthy or as broken depending on which source won, when the honest answer is
    determined by the config in front of it. It also keeps the check a pure read
    of one file, matching every sibling here.
    See ``captures/PLAN-wide-output-path-2026-08-07.md`` PR-1, PR-6, D4, D5.
    """
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
    playback_device = _loaded_playback_device(path)
    expected_format, expected_name = _expected_playback_format(
        playback_type, playback_device
    )
    if loaded_format == expected_format:
        return CheckResult(
            label,
            "ok",
            f"playback format={loaded_format} "
            f"(type={playback_type}, device={playback_device}, "
            f"expected {expected_name})",
        )
    return CheckResult(
        label,
        "fail",
        f"loaded CamillaDSP playback format={loaded_format!r} for playback "
        f"type={playback_type!r} device={playback_device!r}, expected "
        f"{expected_format!r} "
        f"({expected_name}) — a half-flipped box: {path} was generated "
        f"against a different {expected_name} than the one currently in "
        "force. Regenerate the config (sudo /opt/jasper/.venv/bin/jasper-sound "
        f"reconcile-current-dsp) or investigate why {expected_name} and the "
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


def _requires_roleful_graph() -> bool:
    """Does the saved topology need a per-driver (crossover) graph?

    NOT a ``@doctor_check`` — a plain helper, and it must stay above the next
    decorated function rather than between a decorator and its target.

    Fail-soft to False: this only ever SOFTENS a message or adds an eligibility
    sentence, never gates anything, so an unreadable topology should leave the
    generic wording in place rather than assert a class it cannot prove. Every
    caller that acts on rolefulness reads it from the fail-CLOSED loaders
    instead.
    """
    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        return bool(
            classify_output_contract(load_output_topology_strict())
            .requires_roleful_graph
        )
    except (OutputTopologyError, OSError, ValueError):
        return False


@doctor_check(order=51.7, group="audio")
def check_fanin_coupling() -> CheckResult:
    """The transport intent must match the loaded CamillaDSP graph.

    A mismatch is a half-applied arm/disarm: the dangerous one is
    intent=loopback but a ``RawFile`` config is loaded — CamillaDSP then reads a
    pipe no writer feeds and crash-loops on its statefile config (the jts5
    2026-06-27 failure mode). Under ``shm_ring`` (P2) both ends are ALSA ioplug
    devices — capture ``jts_ring_capture`` (Ring A) + the post-DSP playback ring
    (``jts_ring_playback``, or ``jts_ring_active_playback`` once the active
    endpoint is armed) — AND ``JASPER_OUTPUTD_CONTENT_BRIDGE`` must be
    ``shm_ring``: this check catches the PARTIAL flip (one end ring, the other
    ALSA/direct) that strands a ring. The fix is to re-run the ordered
    reconciler: ``jasper-fanin-coupling-reconcile <intent>``.
    """
    from jasper.fanin.coupling_reconcile import read_persisted_coupling
    from jasper.fanin_coupling import (
        COUPLING_SHM_RING,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_outputd_content_bridge,
        ring_active_endpoint_armed,
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
        # Ring A capture + the post-DSP ring playback are BOTH ALSA ioplug
        # devices — the loaded graph must name jts_ring_capture AND the ring this
        # box's endpoint marker selects, or the coherent env pair above landed
        # but the loaded config is stale/half-ring.
        #
        # WHAT PUTS A BOX HERE. A CamillaDSP restart re-seeds the graph the
        # statefile points at, so a STALE ARTIFACT (one emitted before the
        # endpoint moved) reappears on the next restart — that is the
        # finding-5 revert shape, and its repair is a baseline re-emit, not a
        # re-arm. The MARKER is not moved by a camilla restart at all: its only
        # writer is jasper-audio-hardware-reconcile, which re-derives it from the
        # loaded graph on boot, on udev, and on every deploy — so those are the
        # events that can de-arm a box whose artifact drifted, and a camilla
        # restart alone is not one of them.
        #
        # WHICH post-DSP ring is EXACTLY ONE answer, taken from the reconciler's
        # marker — not "either is fine". Accepting both would read green through
        # the crossing this rung exists to prevent: a roleful box's graph pointed
        # at the full-range stereo ring, or a stereo box's at the active ring.
        armed = ring_active_endpoint_armed()
        expected_playback = (
            RING_ACTIVE_PLAYBACK_DEVICE if armed else RING_PLAYBACK_DEVICE
        )
        # A ROLEFUL box with a CLEARED marker has no honest stereo expectation:
        # jts_ring_playback is a FORBIDDEN token for every active emitter, so
        # such a box's graph can never name it, and printing "(expected
        # jts_ring_playback)" would send an operator to a device the emitters
        # refuse to write. The honest statement is that this box is mid-arm —
        # the artifact moved to the ring but the marker has not been re-derived
        # (or the reverse) — and the remedy is the ladder, not a re-arm.
        roleful = _requires_roleful_graph()
        capture_device = _loaded_device_field(config_path, "capture", "device")
        playback_device = _loaded_device_field(config_path, "playback", "device")
        ring_mismatches: list[str] = []
        if capture != "Alsa" or capture_device != RING_CAPTURE_DEVICE:
            ring_mismatches.append(
                f"capture={capture}/{capture_device or '(missing)'} "
                f"(expected Alsa/{RING_CAPTURE_DEVICE})"
            )
        if playback_device != expected_playback:
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
            return CheckResult(
                label,
                "ok",
                f"{coupling} (capture={RING_CAPTURE_DEVICE}, "
                f"playback={expected_playback}, bridge={outputd_bridge})",
            )
        # Severity stays WARN. Under the arm ladder this is a mid-procedure
        # TRANSIENT — the graph moves first, the marker is re-derived second, the
        # coupling third — so a box observed between two of those steps is
        # exactly this state and is not broken. What the detail owes the operator
        # is the command that finishes it.
        if roleful:
            recovery = (
                "the ACTIVE-ring ladder, in order: sudo /opt/jasper/.venv/bin/"
                "jasper-active-speaker baseline-reemit --endpoint ring && sudo "
                "systemctl start jasper-audio-hardware-reconcile && sudo "
                "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
                "(to roll back, the same three with --endpoint aloop and "
                "`loopback`)"
            )
        else:
            recovery = (
                "run: sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile "
                "shm_ring"
            )
        cause = (
            "a stale baseline artifact re-seeded on a camilla restart is the "
            "usual cause (finding-5 revert)"
        )
        # A ROLEFUL box that is still ARMED here cannot be told apart, by this
        # check alone, from the CANONICAL rollback-ladder step-2 refusal
        # (owner-ruled 2026-08-12, #2332): jasper-audio-hardware-reconcile
        # refuses the aloop-rollback candidate with a ring-plan endpoint
        # mismatch and exits 78 without touching the marker, so an armed box
        # mid-rollback lands in exactly this warn. That refusal is expected,
        # not a second symptom to chase — the way out is step 3
        # (jasper-fanin-coupling-reconcile loopback), not re-running step 2, and
        # a later hardware reconcile clears the stale marker once step 3 lands.
        if roleful and armed:
            cause += (
                ", or an EXPECTED rollback-ladder step-2 refusal (#2332) if "
                "jasper-audio-hardware-reconcile already exited 78 here after "
                "baseline-reemit --endpoint aloop — normal on an armed box: skip "
                "straight to jasper-fanin-coupling-reconcile loopback (step 3) "
                "rather than re-running step 2"
            )
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but loaded graph is not the ring config: "
            f"{'; '.join(ring_mismatches)}; {cause}; {recovery}",
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
        if dev
        in (RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)
    ]
    if stale_ring_devices:
        # WHICH command clears this depends on the topology, and on a roleful box
        # the coupling reconciler alone cannot. `jasper-fanin-coupling-reconcile
        # loopback` routes through `reconcile_current_dsp`, which refuses to host
        # a roleful box's transient startup/commissioning graph
        # (`eq_on_active_not_wired` -> status `skipped`) and — because the
        # coupling is already non-ring — reports SUCCESS without moving the
        # graph. The operator then re-runs a command that converges nothing while
        # the warn persists. The graph has to be moved by step 1 of the ladder,
        # exactly as the shm_ring branch above already spells out for its own
        # direction.
        if _requires_roleful_graph():
            recovery = (
                "this box is ROLEFUL, so the coupling reconciler cannot move its "
                "graph (it declines to host a transient active graph and reports "
                "success without converging). Run the ROLLBACK ladder, in order: "
                "sudo /opt/jasper/.venv/bin/jasper-active-speaker baseline-reemit "
                "--endpoint aloop && sudo systemctl start "
                "jasper-audio-hardware-reconcile && sudo /opt/jasper/.venv/bin/"
                "jasper-fanin-coupling-reconcile loopback"
            )
        else:
            recovery = (
                "Run: sudo /opt/jasper/.venv/bin/"
                "jasper-fanin-coupling-reconcile loopback"
            )
        return CheckResult(
            label,
            "warn",
            f"intent={coupling} but the loaded graph still names ring ioplug "
            f"device(s): {'; '.join(stale_ring_devices)}; a disarm's camilla step "
            "likely failed — CamillaDSP captures a writer-dead ring (silence) while "
            f"the env reads clean. {recovery}",
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


def _jts_ring_probe_wire(pcm: str) -> tuple[int, str] | None:
    """``(channels, format)`` the open-probe must ask this PCM for, or ``None``
    when this conf.d block's wire is indeterminate (unreadable file, missing
    block, or a torn declaration — nothing safe to ask ALSA for).

    From the conf.d block itself — :func:`~jasper.ring_assets.ring_conf_channels`
    / :func:`~jasper.ring_assets.ring_conf_format` — NOT from
    :func:`~jasper.fanin_coupling.resolve_ring_wire`. The ioplug advertises
    EXACTLY what THIS conf.d, as it stands on disk, declares as its hw_params
    constraint, so the probe's question is "what does the file say" — a
    DIFFERENT question from "what SHOULD this box declare", which is what the
    resolver answers. Those two answers are independently gated: conf
    rendering (``render_ring_conf_wire``, driven by a detected DAC's declared
    ``LatencyFloor``) and ring coupling (the ``shm_ring`` arm) are separate
    gates, so a box can carry a per-box-rendered Ring B conf.d while still
    sitting coupling-inert (or the reverse) — probing the resolver's answer on
    such a box would ask ALSA for the wrong width. An absent
    ``format``/``channels`` key means the ioplug default
    (:data:`~jasper.ring_assets.RING_CONF_DEFAULT_FORMAT` /
    :data:`~jasper.ring_assets.RING_CONF_DEFAULT_CHANNELS`), which both parsers
    already encode, so this answers a never-rendered file correctly too — no
    separate "unrendered" branch needed. That path now carries only
    ``channels``: the shipped conf.d SPELLS ``format S32_LE`` in every block, so
    the format axis is read from the file rather than inferred. The ring PCMs
    can legitimately differ on channels, hence the per-PCM lookup.
    """
    channels = ring_assets.ring_conf_channels(pcm, _JTS_RING_CONF_D)
    sample_format = ring_assets.ring_conf_format(pcm, _JTS_RING_CONF_D)
    if channels is None or sample_format is None:
        return None
    return channels, sample_format


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
    wire = _jts_ring_probe_wire(pcm)
    if wire is None:
        return False, (
            f"ring conf.d ({_JTS_RING_CONF_D}) has no readable pcm.{pcm} "
            "format/channels to probe with — indeterminate wire (unreadable "
            "or torn); redeploy to reinstall it"
        )
    channels, sample_format = wire
    ring_path = _jts_ring_path_for(pcm)
    pre_existed = ring_path is not None and os.path.exists(ring_path)
    # arecord -> /dev/null (discard captured silence); aplay -> /dev/zero
    # (feed silence in). 48 kHz / 1 s; the width comes from what THIS
    # conf.d's own pcm.<name> block declares (an absent key is the ioplug's
    # own default) — see _jts_ring_probe_wire.
    sink = "/dev/null" if tool == "arecord" else "/dev/zero"
    try:
        proc = _run(
            [tool, "-D", pcm, "-c", str(channels), "-r", "48000",
             "-f", sample_format, "-d", "1", sink],
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
    ioplug actually dlopens.

    Three assets: the compiled ioplug .so, the conf.d PCM definitions
    (jasper.ring_assets.RING_CONF_PCMS), and the /dev/shm/jts-ring directory.
    On a box whose coupling is ARMED they are load-bearing — CamillaDSP's
    capture and post-DSP playback resolve through them — so what a missing or
    broken asset COSTS is what this check's severity tracks.

    Statuses:
      ok    — .so + conf.d + shm dir present, and (on an unarmed box) every PCM
              open-probes cleanly.
              CAVEAT: presence and the open-probe both pass on a STALE .so left
              by a failed rebuild (the 2026-07-02 class) — it is structurally
              valid, so it dlopens and registers. `check_ring_ioplug_provenance`
              is the check that separates the two, by comparing the installer's
              record against the plugin on disk. See ring-platform.sh.
      warn  — an asset is MISSING while the ring is NOT armed. Loopback carries
              audio in that state, so the box is degraded rather than broken and
              the next deploy rebuilds.
      fail  — an asset is missing while shm_ring IS armed (the armed graph
              cannot resolve its ring devices), or the .so is installed but a
              PCM fails to open — a bad registration / arch mismatch, e.g. the
              -DPIC class. The EBUSY exception is called out below.

    Probe leaves no residue: it opens each device for 1 s against an absent
    ring, exercising only the writer-dead / no-reader silence path (feeds or
    discards silence). Because the ioplug open path is create-or-attach, the
    probe would create the ring file; _jts_ring_pcm_resolves snapshots and
    unlinks only what it created, so an unarmed box still carries no ring file
    after a doctor run.

    Armed-state aware: when a coupling is ARMED (the persisted
    JASPER_FANIN_CAMILLA_COUPLING is shm_ring) the ring has a live reader/writer,
    so the ioplug's SPSC guard EBUSYs the open-probe — which is NOT a defect. In
    that state this check does NOT open-probe (the live ring must not be
    disturbed); it verifies asset PRESENCE only and defers the "is the armed ring
    coherent + alive" verdict to `check_fanin_coupling` (intent vs the loaded
    config) and `check_ring_geometry_coherence` (env vs conf.d vs the on-disk
    header). The open-probe path below runs only on an UNARMED box, where an
    EBUSY genuinely would indicate a stray lab arm or a stuck ring.
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
        # + liveness verdict belongs to `check_fanin_coupling` and
        # `check_ring_geometry_coherence`.
        return CheckResult(
            label,
            "ok",
            "ioplug + conf.d + /dev/shm/jts-ring present; shm_ring ARMED "
            "(open-probe skipped — live ring; see the 'fanin coupling' and "
            "'ring geometry' checks)",
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
        # ring is simply in use. A ring armed through the PRODUCT path never
        # reaches here (the armed branch above returns before the probe), so an
        # EBUSY at this point means a reader/writer the persisted coupling does
        # not account for — a stray lab arm, or a stuck ring.
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


def _resolved_ring_wire():
    """The ring wire an arm would render into the conf.d, or ``None``.

    The same two calls the arm's own capability gate makes
    (:func:`jasper.fanin.coupling_reconcile.ring_wire_caps_ready`), so the doctor
    weighs a provenance record against the wire the RECONCILER will use rather
    than deriving a second answer to the same question.

    ``None`` when the box declares a wire neither language recognizes: that
    refusal belongs to ``resolve_wire_for_gate``, which already names it in the
    arm's own log, and restating it as a provenance verdict would give one
    failure two reasons.
    """
    try:
        from ...fanin.coupling_reconcile import (
            load_topology_for_wire,
            resolve_wire_for_gate,
        )

        wire, _problem = resolve_wire_for_gate(load_topology_for_wire())
    except (ImportError, OSError):
        return None
    return wire


@doctor_check(order=51.85, group="audio")
def check_ring_ioplug_provenance() -> CheckResult:
    """Is the INSTALLED ioplug the one the installer built, and what can it parse?

    THE GAP THIS CLOSES. ``check_ring_platform_assets`` reports presence, and its
    open-probe passes on a structurally-valid plugin — so a STALE ``.so`` (the
    ioplug build degrades to a WARN, leaving the previous one installed beside
    freshly-installed Rust daemons) read as ``ok`` and the only signal was a WARN
    in a deploy transcript that had long since scrolled away. The installer now
    records the sha and the conf.d fields of the plugin it installed, and revokes
    that record on every path where it did NOT produce the installed file, so
    this check can say "stale" where it used to say nothing.

    THE VERDICT IS WEIGHED BY THE BOX'S OWN WIRE, because that is what decides
    whether an unvouched plugin costs anything:

    * a wire that renders no conf.d field beyond the ioplug's own defaults opens
      on ANY installed plugin, so "cannot vouch" is a ``warn`` — real, but
      nothing is refused on such a box;
    * a wire that declares a non-default sample FORMAT is refused at the arm by
      ``ring_wire_caps_ready``, which is a ``fail``: the box drops to loopback,
      and a roleful box parks its content lane. Reporting that as a warning
      would bury the one verdict that predicts a disarm.

    WHAT THE PREDICATE COVERS, so this does not over-promise:
    ``ring_wire_capabilities`` reads the sample format, the Ring A / Ring B
    channel counts, and — since the ring-wire default flip — the ACTIVE block's
    own ``channels`` axis, so a roleful box whose post-crossover width forces
    that key is weighed here too. What stays outside it is the FILE: the
    predicate answers "which keys does this wire force onto the conf.d", not
    "which keys does the conf.d on disk declare", and the shipped conf.d now
    spells ``format`` explicitly. So a box an operator has pinned narrow
    resolves an empty capability set while its rendered conf.d still carries a
    ``format`` line — see ``ring_wire_capabilities``' own note, and #2597.

    The escalation asks :func:`jasper.ring_assets.ring_ioplug_wire_supported` —
    the gate's own predicate, remediation text included — rather than restating
    its rule, and that call SHORT-CIRCUITS to ok before reading the record or
    hashing the plugin when the wire needs nothing. So on every box carrying a
    wire that declares no extra field this branch is inert and the verdicts
    below are exactly what they were.

    Skips when the ``.so`` is absent: that is ``check_ring_platform_assets``'s
    missing-asset verdict, and one absent file should produce one reason.
    """
    label = "ring ioplug provenance"
    so_path = ring_assets.ring_ioplug_so_path(plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR)
    if not os.path.exists(so_path):
        return CheckResult(
            label, "ok", f"skipped — {so_path} absent (see 'ring platform')"
        )
    wire = _resolved_ring_wire()
    if wire is not None:
        # A capability-needing wire hashes the .so here and again in the
        # record-compare below. Left as two reads rather than threading a
        # digest between the layers: the plugin is tens of KB, and the gate
        # owning its own comparison end-to-end is what makes the verdict
        # quotable rather than reassembled.
        support = ring_assets.ring_ioplug_wire_supported(
            wire, plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR
        )
        if not support.ok:
            return CheckResult(
                label,
                "fail",
                "the ring arm will be REFUSED on this box: "
                f"{support.detail}. Cost until then: loopback on a flat box, a "
                "parked content lane on a roleful one. Command: bash "
                "scripts/deploy-to-pi.sh",
            )
    record = ring_assets.read_ring_ioplug_provenance()
    if not record.recorded:
        return CheckResult(
            label,
            "warn",
            f"{so_path} is installed but UNVOUCHED: no usable record at "
            f"{ring_assets.RING_IOPLUG_PROVENANCE}. Either this box has not been "
            "redeployed since the installer began recording, or a deploy revoked "
            "the record because its ioplug build failed — check the deploy "
            "transcript for a jts_ring ioplug build WARN. Redeploy (bash "
            "scripts/deploy-to-pi.sh) to rebuild and record.",
        )
    installed = ring_assets.ring_ioplug_so_sha256(
        plugin_dir=_JTS_RING_ALSA_PLUGIN_DIR
    )
    if installed is None:
        return CheckResult(
            label, "warn", f"{so_path} could not be read to compare against the record"
        )
    if installed != record.sha256:
        return CheckResult(
            label,
            "warn",
            f"STALE ioplug: {so_path} hashes {installed[:12]}… but the installer "
            f"recorded {record.sha256[:12]}…, so the plugin on disk is NOT the one "
            "the last successful install produced. The ioplug build degrades to a "
            "WARN and leaves the previous .so in place — redeploy and check the "
            "transcript for a jts_ring ioplug build failure.",
        )
    caps = ", ".join(sorted(record.caps)) or "none"
    return CheckResult(
        label,
        "ok",
        f"{so_path} matches the installer's record (sha {record.sha256[:12]}…); "
        f"conf.d fields it can parse: [{caps}]",
    )


# How long to wait before CONFIRMING a suspected two-writer observation.
#
# MUST exceed the C ioplug's writer-lock acquisition budget
# (``JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS``, 500 ms): ``acquire_writer_lock``
# opens the lock file FIRST and only then spins on ``flock`` until that budget
# expires, so for up to that long a perfectly healthy box legitimately has TWO
# processes holding an fd on one ``.writer.lock`` — the live incumbent and a
# transient contender that is about to be refused. A single-sample count would
# report that ordinary race as the two-live-writers defect. Pinned against the
# header by ``tests/test_ring_slot_ceiling_pin.py``.
_WRITER_LOCK_CONFIRM_DELAY_SEC = 0.75
# The procfs root the writer-lock sweep reads. A module constant, resolved at
# CALL time by everything below, so a test can repoint it at a synthetic tree.
_PROC_ROOT = "/proc"


def _ring_writer_lock_holders(
    *,
    proc_root: str | None = None,
    shm_dir: str | None = None,
) -> tuple[dict[str, dict[int, bool]], int]:
    """Which live pids hold an fd on a ring ``.writer.lock``, by lock path.

    Returns ``({lock_path: {pid: target_was_unlinked}}, unreadable_pid_count)``.

    WHY fds AND NOT ``/proc/*/maps``. The obvious enumeration — scan maps for
    the RING file — cannot work: the Rust reader mmaps the very same ring file
    ``PROT_READ|PROT_WRITE`` (``rust/jasper-ring/src/lib.rs`` ``mmap_fd``), so
    on every armed box the ring has two mappers by design and ">1 mapper" is the
    healthy state. The WRITER LOCK is the discriminator, because only the C
    ioplug's writer ever opens it: Rust takes the ``.open.lock`` transaction lock
    and never this one.

    WHY the pathname is the grouping key. It is the shape of the residual
    :func:`check_ring_writer_lock_exclusivity` documents: an orphaned incumbent
    and the fresh file that replaced it share one PATHNAME while holding two
    different inodes, so grouping by pathname puts both halves in one bucket.
    ``/proc``'s ``" (deleted)"`` suffix on the incumbent's fd names which half
    is the orphan.
    """
    root = ring_assets.RING_SHM_DIR if shm_dir is None else shm_dir
    procfs = _PROC_ROOT if proc_root is None else proc_root
    prefix = root.rstrip("/") + "/"
    holders: dict[str, dict[int, bool]] = {}
    unreadable = 0
    try:
        entries = os.listdir(procfs)
    except OSError:
        return holders, unreadable
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        fd_dir = os.path.join(procfs, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            # Non-root doctor runs see only their own processes. Counted so the
            # verdict can say it was partially blind instead of claiming clean.
            unreadable += 1
            continue
        except OSError:
            # ENOENT — the pid exited between the two listdirs. Not a blind
            # spot: a process that is gone holds nothing.
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            unlinked = target.endswith(" (deleted)")
            if unlinked:
                target = target[: -len(" (deleted)")]
            if not target.startswith(prefix):
                continue
            if not target.endswith(ring_assets.RING_WRITER_LOCK_SUFFIX):
                continue
            per_path = holders.setdefault(target, {})
            # One pid with several fds on one lock is still ONE writer; an
            # unlinked target anywhere in that pid's fds marks it orphaned.
            per_path[pid] = per_path.get(pid, False) or unlinked
    return holders, unreadable


@doctor_check(order=51.86, group="audio")
def check_ring_writer_lock_exclusivity() -> CheckResult:
    """Do two live writers hold one ring's writer lock?

    THE RESIDUAL THIS CLOSES. ``c/jts-ring-ioplug/jts_ring_shm.c`` records it
    verbatim: an ``flock``'s identity is the PATHNAME, not the inode, so
    UNLINKING ``<ring>.writer.lock`` while a writer holds it voids exclusivity
    SILENTLY — "two live writers proceed with no log line between them.
    Measured." It is reachable today because ``scripts/ring-proto/disarm.sh``
    does ``rm -rf`` over the tmpfs directory. The C comment names its own fix, a
    doctor check that notices two live writer pids on one ring; this is it. It
    lands here, and not as a follow-up, because the grouping reconciler's
    active-leader arm now DEPENDS on that lock as a safety signal (the
    active-content release barrier, ``jasper/multiroom/reconcile.py``).

    THE HEADER CANNOT ANSWER THIS. ``writer_pid`` is a SINGLE header slot
    (offset 56, ``_Static_assert``-pinned), so a second writer's attach simply
    overwrites it: the shared file can only ever name one writer and is
    structurally blind to the exact condition being detected. This reads the
    KERNEL's view — who holds an fd on the lock — which is the only view the
    pathname-vs-inode residual cannot fool.

    Statuses:
      ok    — no lock path has more than one live holder (the normal state,
              including "no writer anywhere" on an unarmed box).
      warn  — a holder's lock file has been UNLINKED out from under it (one
              writer, but exclusivity is already void: the next opener will
              create a fresh inode and will NOT be excluded), or ``/proc`` was
              only partially readable so the sweep was blind to some pids.
      fail  — two or more live pids hold one ring's writer lock, confirmed on a
              second sample ``_WRITER_LOCK_CONFIRM_DELAY_SEC`` later so an
              ordinary create-or-attach race cannot masquerade as the defect.
    """
    label = "ring writer-lock exclusivity"
    if not os.path.isdir(_PROC_ROOT):
        return CheckResult(label, "ok", f"skipped — no {_PROC_ROOT} on this host")

    holders, unreadable = _ring_writer_lock_holders()
    suspects = {path: pids for path, pids in holders.items() if len(pids) > 1}
    if suspects:
        time.sleep(_WRITER_LOCK_CONFIRM_DELAY_SEC)
        confirmed_holders, confirm_unreadable = _ring_writer_lock_holders()
        unreadable = max(unreadable, confirm_unreadable)
        confirmed: dict[str, dict[int, bool]] = {}
        for path, pids in suspects.items():
            still = confirmed_holders.get(path, {})
            # Only pids present in BOTH samples count: a contender that gave up
            # inside the C budget is gone by now, a real second writer is not.
            both = {
                pid: still.get(pid, False) or held
                for pid, held in pids.items()
                if pid in still
            }
            if len(both) > 1:
                confirmed[path] = both
        if confirmed:
            parts = []
            for path, pids in sorted(confirmed.items()):
                who = ", ".join(
                    f"pid {pid}{' (lock file unlinked)' if unlinked else ''}"
                    for pid, unlinked in sorted(pids.items())
                )
                parts.append(f"{path}: {who}")
            return CheckResult(
                label,
                "fail",
                "TWO LIVE WRITERS on one ring — exclusivity is void and the "
                "ring's SPSC contract is broken: "
                + "; ".join(parts)
                + ". Most likely the lock file was unlinked while a writer held "
                "it (scripts/ring-proto/disarm.sh rm -rf's the tmpfs dir). Stop "
                "the extra writer, then re-arm the lane.",
            )

    orphaned = [
        (path, pid)
        for path, pids in holders.items()
        for pid, unlinked in pids.items()
        if unlinked
    ]
    if orphaned:
        detail = "; ".join(f"{path}: pid {pid}" for path, pid in sorted(orphaned))
        return CheckResult(
            label,
            "warn",
            "a ring writer holds a lock file that has been UNLINKED, so "
            f"exclusivity is already void for the next opener — {detail}. "
            "Re-arm the lane to recreate the lock before a second writer "
            "attaches.",
        )
    if unreadable:
        return CheckResult(
            label,
            "warn",
            f"could not read /proc/<pid>/fd for {unreadable} process(es), so "
            "this sweep was partially blind — run jasper-doctor as root for a "
            "complete answer.",
        )
    held = sum(len(pids) for pids in holders.values())
    return CheckResult(
        label,
        "ok",
        f"{len(holders)} ring writer lock(s) held by {held} process(es); "
        "no ring has more than one live writer",
    )


@doctor_check(order=51.56, group="audio")
def check_ring_reader_stall() -> CheckResult:
    """A ring being WRITTEN but not READ, judged from the SHARED HEADER.

    THE INDEPENDENT OBSERVER, and that is the whole point. Its sibling
    ``check_fanin_ring_stall`` (order 51.55) asks fan-in's STATUS socket — the
    WRITER reporting on itself, which works for Ring A because fan-in owns that
    ring's writer in Rust and keeps the counters. The ACTIVE ring has no such
    witness: its writer is CamillaDSP through the **C ioplug**, whose
    ``published_slots`` / ``drop_no_reader`` / ``full_waits`` are process-local
    ``jts_ring_writer_t`` fields printed at close, not shared-header fields, so
    no reader can see them; and its reader is outputd, which during exactly this
    fault is blocked in ``writei`` and is not even sampling occupancy. The doctor
    is blocked in neither end, so it reads the header directly.

    THE CONJUNCTION, and why it is this one: ``writer_heartbeat_ns`` FRESH while
    ``reader_heartbeat_ns`` is STALE. Not ``read_seq``-flat — the writer advances
    ``read_seq`` on the absent reader's behalf at demotion, so a ``read_seq``
    clause goes false exactly when the drops begin. See
    :class:`jasper.ring_assets.RingStallVerdict` for the full derivation.

    Judges all THREE rings, because the fault is a property of a ring file rather
    than of a coupling, and reports per-ring so an operator knows which daemon to
    look at. Absent / idle rings are silent: ``present=False`` covers the unarmed
    fleet, where none of these files exists at all.

    Returns:
      - ok when no ring is stalled (including every unarmed box, where there is
        nothing to judge)
      - warn naming each stalled ring, its two heartbeat ages, and the reader
        daemon to check. WARN not FAIL: the ring self-recovers the instant the
        reader resumes, and the household's remedy is the same either way.
    """
    from jasper.ring_assets import (
        RING_A_PROGRAM_FILE,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONTENT_FILE,
        ring_stall_verdict,
    )

    name = "ring reader stall"
    rings = (
        ("Ring A (fan-in -> CamillaDSP)", RING_A_PROGRAM_FILE),
        ("Ring B (CamillaDSP -> outputd)", RING_B_CONTENT_FILE),
        ("ACTIVE ring (CamillaDSP -> outputd)", RING_ACTIVE_CONTENT_FILE),
    )
    stalled: list[str] = []
    judged: list[str] = []
    for label, path in rings:
        verdict = ring_stall_verdict(path)
        if not verdict.present:
            continue
        judged.append(label)
        if verdict.stalled:
            stalled.append(f"{label}: {verdict.detail}")
    if stalled:
        return CheckResult(name, "warn", "; ".join(stalled))
    if not judged:
        return CheckResult(
            name,
            "ok",
            "no ring is being written (no armed ring on this box, or all idle)",
        )
    return CheckResult(
        name, "ok", f"reader keeping up on {len(judged)} live ring(s): {', '.join(judged)}"
    )


@doctor_check(order=51.9, group="audio")
def check_ring_geometry_coherence() -> CheckResult:
    """Verify the Ring-A geometry agrees across env, conf.d, and on-disk (defect A).

    The ring geometry must match or CamillaDSP's ioplug attach fails hard
    (hw_params EINVAL + ``attach_fatal reason=ring header does not match expected
    geometry`` → crash-loop → start-limit-hit). ``n_slots`` is checked on THREE
    axes, and the on-disk header is then compared on EVERY axis the attach
    compares:

      1. fan-in's resolved ``JASPER_FANIN_RING_SLOTS`` (jasper.env -> fanin.env
         systemd env chain, default 2)
      2. the conf.d ``jts_ring_capture`` ``n_slots`` (the ioplug attach authority)
      3. the on-disk ``program.ring`` header vs that conf.d block, on ``n_slots``,
         ``period_frames`` (the ring slot IS one outputd period, so a stale period
         fails the attach even with matching slots — Nit-7, 2026-07-05),
         ``sample_format`` and ``channels``

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
    # Axes 3-6: every axis the ioplug attach compares — n_slots, period_frames,
    # sample_format and channels — through the SAME comparator the coupling
    # reconciler's stale-file guard and CONFIRM self-heal use, so the doctor
    # cannot call a file coherent that the reconciler is about to delete (or
    # vice versa). The wire axes used to be REPORTED here and compared nowhere,
    # which is precisely how a ring that shears on format or channel count
    # passed every Python guard and was first noticed as a hard attach failure.
    verdict = ring_assets.ring_header_matches_conf(
        ring_assets.RING_A_PROGRAM_FILE,
        ring_assets.RING_A_CONF_PCM,
        conf_d=_JTS_RING_CONF_D,
    )
    if not verdict.ok:
        return CheckResult(
            label, "fail",
            f"{verdict.detail}. A stale ring file from a prior {verdict.axis} "
            "geometry blocks the ioplug attach. Run: sudo "
            "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
            "(it deletes a geometry-mismatched ring file before re-arming).",
        )
    return CheckResult(
        label, "ok",
        f"Ring A geometry coherent across env + conf.d + on-disk header "
        f"(n_slots={header.n_slots}, period_frames={header.period_frames}, "
        f"wire {header.sample_format_name}/{header.channels}ch); rate "
        f"{header.rate} Hz",
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

    An ``ok`` that leaves a box on loopback SAYS WHY (issue #2294). Both
    no-floor and non-matching-floor are ok — the conf.d is right either way —
    but they also bear on ring eligibility, so reporting only "nothing to
    render" left the household's actual question ("why is this box on
    loopback?") answered nowhere.

    WHAT THOSE BRANCHES MAY AND MAY NOT CLAIM. They read the DECLARED floor,
    which is not the same fact as outputd's RESOLVED period — the two diverge
    through the documented operator seam, ``JASPER_OUTPUTD_PERIOD_FRAMES`` in
    ``/etc/jasper/jasper.env``, which outranks the reconciler's floor-derived
    value. So a floorless box CAN ring, and one did: jts4 (InnoMaker HiFi AMP
    Pro, no declared floor at the time) armed shm_ring on 2026-08-14 with the
    period and DAC buffer hand-set in that file. These branches therefore say
    what is not RENDERED and name both routes to a ring; they must not say the
    ring is unavailable, which is a claim about the resolved period they do not
    read. ``ring geometry`` and the coupling reconciler's own preflight own that
    axis.

    THE FLOOR IS NOT THE ONLY REASON, and R7b changed what this check can say
    about the other one. A ROLEFUL (active-crossover) box is never armed by the
    unattended default pass however good its floor is: its ring is the ACTIVE
    ring, and that is explicit-arm-only. So on a roleful box a green period line
    means "the conf.d is ready", NOT "this box will ring" — and saying only the
    former is how an operator concludes the arm is broken when nothing is. Every
    OK branch below therefore carries the rolefulness sentence when it applies,
    read from the saved topology (:func:`_requires_roleful_graph`). The two WARN
    branches deliberately do not: each is a concrete render failure with one
    remedy, and appending "…and even then it would not ring on its own" would
    bury the action the operator has to take.

    (This supersedes R7a's scope note, which said a roleful topology "has no
    ring width at all" and that the reason was one this check could not see.
    Both were true when it was written and neither is now:
    ``active_ring_channels_for_topology`` answers a roleful width, and the check
    reads the topology. jts3 is the box where the two reasons swapped — before
    the DAC8X declared a floor its reason was the floor; after it, the reason is
    the topology.)

    The product boundary: Ring A's slot size is fan-in's COMPILE-TIME
    ``RING_SLOT_FRAMES`` (``rust/jasper-fanin/src/config.rs``, no env
    override), so only a floor that EQUALS it is renderable. A DAC declaring
    any other floor never gets a rendered conf.d, loopback continues, and the
    floor's outputd period/buffer geometry still applies through
    ``outputd.env``. That is a known limit with an owner (issue #2147), so it
    reports ok, not warn.

    Scope: this compares the conf.d against the DECLARED floor, which is what
    the renderer uses. An operator ``JASPER_OUTPUTD_PERIOD_FRAMES`` override
    moves outputd's EFFECTIVE period away from the floor; that divergence is
    the coupling reconciler's preflight to catch (and ``ring geometry`` for the
    on-disk axis), not this check's.
    """
    label = "ring conf floor"
    # Local import: audio_runtime_plan is heavy and the doctor's other ring
    # checks already reach for it lazily.
    from ...audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES

    dac_id = _active_audio_dac_id()
    floor = latency_floor_for(dac_id)
    # The eligibility half this check cannot read off the conf.d. Appended to
    # every OK branch, so an operator never has to already know that the floor is
    # only one of the two gates. The WARN branches leave it off on purpose — see
    # the docstring.
    # Says "roleful box", NOT "commissioned box": step 1 accepts either roleful
    # boot graph — an applied baseline or the all-muted startup anchor a
    # mid-commission box boots from — so an operator on the fleet-typical
    # composite must not read this and conclude the arm is out of reach until
    # commissioning finishes. It is not.
    roleful_note = (
        " This box is ROLEFUL (active crossover), so even a rendered conf.d "
        "does not make it ring on its own: the ACTIVE ring is armed only by an "
        "explicit `jasper-active-speaker baseline-reemit --endpoint ring` "
        "followed by jasper-audio-hardware-reconcile and "
        "`jasper-fanin-coupling-reconcile shm_ring`, never by the unattended "
        "default pass. That first step works on a mid-commission box too — it "
        "re-stages the all-muted startup anchor when no applied baseline is "
        "saved yet."
        if _requires_roleful_graph()
        else ""
    )
    if floor is None:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares no latency floor — {_JTS_RING_CONF_D} keeps its "
            "shipped default (no declared floor, nothing to render). Absent a "
            f"floor outputd resolves its packaged default period "
            f"{DEFAULT_OUTPUTD_PERIOD_FRAMES}, which is not the fixed ring slot "
            f"{RING_SLOT_FRAMES}, so shm_ring needs one of two things here: "
            f"JASPER_OUTPUTD_PERIOD_FRAMES={RING_SLOT_FRAMES} (plus a matching "
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES) in /etc/jasper/jasper.env, which "
            f"outranks this default; or a declared {RING_SLOT_FRAMES}-frame "
            "floor on this DAC, which is the codified form of the same values. "
            "Until either, loopback coupling is correct. (Issue #2147 would make "
            "the ring slot floor-derived and retire the question.)" + roleful_note,
        )
    if floor.outputd_period_frames != RING_SLOT_FRAMES:
        return CheckResult(
            label,
            "ok",
            f"{dac_id} declares outputd period "
            f"{floor.outputd_period_frames} != the fixed ring transport slot "
            f"{RING_SLOT_FRAMES}, so its conf.d is deliberately not rendered "
            "until issue #2147 makes the ring slot floor-derived. shm_ring "
            f"needs outputd's RESOLVED period to be {RING_SLOT_FRAMES}, which "
            "on this DAC only an operator JASPER_OUTPUTD_PERIOD_FRAMES in "
            "/etc/jasper/jasper.env can produce; otherwise loopback coupling "
            "continues and still receives the floor's outputd period/buffer "
            "geometry." + roleful_note,
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
        f"period_frames={conf_period} matches {dac_id}'s declared latency floor"
        + roleful_note,
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


def _outputd_content_bridge_detail(data: dict[str, object]) -> str:
    """Report outputd's resolved content source (``direct`` or ``shm_ring``).

    A plain mode readout. The rate-matched bridge that once published
    fill/ppm/lock/anomaly counters under this key was deleted; the SHM ring's
    own health has its own ``shm_ring`` block, so there is nothing left to
    raise a warning about here.
    """
    bridge = data.get("content_bridge")
    if not isinstance(bridge, dict):
        return "content_bridge=missing"
    mode = bridge.get("mode")
    if not isinstance(mode, str) or not mode:
        return "content_bridge=missing"
    return f"content_bridge={mode}"


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
    gain_fault = _assistant_gain_fault(loudness)
    if gain_fault is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but assistant_loudness.{gain_fault}.",
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
    outputd_env: dict[str, str] | None = None,
) -> str | CheckResult:
    """Validate ALSA or SHM-ring buffer geometry and return ring detail.

    ``outputd_env`` is the reconciled ``outputd.env`` the caller already read.
    It decides WHICH ring's width the observed channels are compared against —
    Ring B's or the ACTIVE ring's — via the endpoint marker. Passing the env
    rather than re-reading it keeps one read per check; ``None`` falls back to a
    file-fresh read so a focused test can call this helper directly.
    """
    ring_detail = ""
    if ring_mode:
        # Ring B (SHM ping-pong content ring): outputd reads the post-DSP program
        # from an n-slot SHM ring, NOT an ALSA capture PCM (neither outputd sink
        # opens a content PCM under shm_ring). content.buffer_frames is therefore
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
        # THE WIRE, compared and not merely printed. outputd's shm_ring block
        # publishes the format/channels it read back off the header it ATTACHED
        # to; the resolver answers what this box's wire is supposed to be. Those
        # are two independent sources, so a disagreement is a real shear — the
        # reader consuming a geometry nobody declared — rather than a constant
        # echoing itself. Only checked once ATTACHED: before that outputd
        # reports its own declaration, which proves nothing about a ring that
        # does not exist yet.
        if ring_attached and isinstance(shm_ring_block, dict):
            from jasper.fanin.coupling_reconcile import load_topology_for_wire
            from jasper.fanin_coupling import (
                resolve_ring_wire,
                ring_active_endpoint_armed,
            )

            # TOPOLOGY-THREADED, like every reconciler gate that compares this
            # wire (``ring_edge_width_ready`` / ``ring_wire_caps_ready`` both
            # pass ``load_topology_for_wire()``). The channel counts are the
            # PER-TOPOLOGY axes in the wire, so resolving with ``None`` here
            # would answer the shipped stereo declaration and report a FAIL
            # against a box whose post-DSP ring legitimately carries a different
            # width — the doctor contradicting the reconciler that armed it.
            # Threading the topology is also why the two agree on a box that
            # cannot read its topology at all: the helper fails soft to ``None``
            # and both sides then ask the same shipped-geometry question.
            wire = resolve_ring_wire(load_topology_for_wire())
            # WHICH ring outputd attached decides which width it is held to.
            # An armed ACTIVE endpoint reads the post-crossover per-driver ring,
            # whose width is ``ring_active_channels``; comparing it against Ring
            # B's stereo width is the same D5 red-line shape as an unrecognized
            # endpoint — a healthy armed box failing forever on a number nobody
            # claimed. An armed endpoint whose active width does not resolve
            # (``None``) has no honest expectation to compare, so the channels
            # axis is skipped rather than compared against a fallback.
            active_endpoint = ring_active_endpoint_armed(outputd_env)
            expected_channels = (
                wire.ring_active_channels if active_endpoint else wire.ring_b_channels
            )
            ring_label = "the active ring" if active_endpoint else "Ring B"
            observed_format = shm_ring_block.get("format")
            observed_channels = shm_ring_block.get("channels")
            if observed_format is not None and observed_format != wire.sample_format:
                return CheckResult(
                    "jasper-outputd",
                    "fail",
                    f"shm_ring.format={observed_format!r} but this box's ring wire "
                    f"resolves to {wire.sample_format} — outputd attached to "
                    f"{ring_label} geometry nobody declared. Run: sudo /opt/jasper/"
                    ".venv/bin/jasper-fanin-coupling-reconcile shm_ring (it clears "
                    "a wire-mismatched ring file before re-arming).",
                )
            if (
                observed_channels is not None
                and expected_channels is not None
                and observed_channels != expected_channels
            ):
                return CheckResult(
                    "jasper-outputd",
                    "fail",
                    f"shm_ring.channels={observed_channels!r} but this box's "
                    f"{ring_label} resolves to {expected_channels} — outputd "
                    f"attached to {ring_label} geometry nobody declared. Run: sudo "
                    "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
                    "(it clears a wire-mismatched ring file before re-arming).",
                )
        ring_detail = (
            f", shm_ring_slots={ring_slots}, shm_ring_slot_frames={ring_slot_frames}"
            f", shm_ring_capacity_frames={ring_capacity}"
            f", shm_ring_occupancy={ring_occupancy}"
            f", shm_ring_attached={ring_attached}"
            f", shm_ring_wire="
            f"{shm_ring_block.get('format') if isinstance(shm_ring_block, dict) else None}"
            f"/{shm_ring_block.get('channels') if isinstance(shm_ring_block, dict) else None}ch"
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
        transport_coherence_report,
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
        transport_report = transport_coherence_report(
            coupling=coupling,
            outputd_env=live_outputd_env,
            camilla_devices=endpoint_evidence.devices,
        )
        if transport_report.errors:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "; ".join(transport_report.errors) + _transport_route_remedy(),
            )
        # A note is coherent but NOT steady, and today's only note — the
        # ACTIVE-ring arm waypoint — is a box that goes silent at its next
        # CamillaDSP load. Warn rather than pass, precisely because it is not
        # audible yet: the statefile repoint is durable, so the box can sit in
        # the waypoint indefinitely and come back silent from a reboot that
        # nothing else here would flag — fan-in and outputd both keep looping
        # over the missing stage. Warn rather than fail because it is a
        # documented rung of an operator ladder with two named exits, not a
        # broken box; the note carries both.
        if transport_report.notes:
            transport_evidence_warning = "; ".join(transport_report.notes)
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
        outputd_env=outputd_env,
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
    bridge_detail = _outputd_content_bridge_detail(data)
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


@doctor_check(order=52.65, group="audio")
def check_renderer_ring_lanes() -> CheckResult:
    """Every ARMED renderer-ingress lane is attached, fed, and coherent (U3/P6).

    An unarmed box — the shipped fleet state — reports ``ok``: no lane armed
    means nothing to judge, and the fleet default is a healthy state, not a
    finding.

    On an armed box it answers three questions, in the order an operator needs
    them, because they have different remedies:

    1. **Is the lane ATTACHED?** A detached lane renders silence. The
       ``detach_reason`` token names which remedy: ``geometry`` is a conf.d /
       fan-in shear, ``refused`` is almost always the renderer user's
       ``jts-ring`` membership or a missing ``UMask=0007``, ``unavailable`` is a
       ring that has not been created yet.
    2. **Is anything WRITING it?** ``writer_alive`` false on an attached lane is
       the ordinary "renderer is not playing" state and NOT a fault, so it never
       fails on its own — but combined with (3) it separates two very different
       situations.
    3. **Has it EVER been written?** This is what
       ``startup_empty_reads`` vs ``empty_reads`` discriminates, and it is the
       reason both are published separately. A lane whose empty reads are ALL
       startup ones has never received a single slot since fan-in attached — the
       renderer has never successfully opened its ring, which looks identical to
       "paused" on every other signal. A lane with steady-state ``empty_reads``
       has been fed and is now idle, which is just a paused renderer.

    Never FAILS on a paused renderer; WARNs on a lane that is detached or has
    never been fed, with the remediation on the line.
    """
    from jasper import renderer_lanes as rl

    label_name = "renderer ring lanes"
    armed = rl.read_armed_labels()
    if not armed:
        # "ok", NOT a novel "skip" status: the doctor vocabulary is
        # ok|warn|fail (CheckResult.status), and render()'s else-branch
        # turns anything unknown into a red ✗ + exit 1 — which briefly made
        # every unarmed box (the fleet default) report failure. Unarmed-is-
        # healthy follows the established unconfigured-is-ok convention
        # (check_chip_reference above, Spotify auth, capture relay, Google
        # integrations). Pinned by tests/test_doctor_audio_runtime.py's
        # unarmed-lane tests, including one through render()'s exit path.
        return CheckResult(label_name, "ok", "no renderer lane armed (fleet default)")

    try:
        status = _read_status_socket(_FANIN_STATUS_SOCKET)
    except (OSError, ValueError) as e:
        return CheckResult(
            label_name,
            "warn",
            f"{len(armed)} lane(s) armed ({', '.join(armed)}) but fan-in STATUS is "
            f"unreadable ({type(e).__name__}) — cannot confirm they are attached",
        )
    inputs = status.get("inputs")
    if not isinstance(inputs, list):
        return CheckResult(
            label_name, "warn", "fan-in STATUS carries no inputs[] to judge"
        )
    by_label = {
        inp.get("label"): inp for inp in inputs if isinstance(inp, dict)
    }

    problems: list[str] = []
    healthy: list[str] = []
    for lane_label in armed:
        entry = by_label.get(lane_label)
        if entry is None:
            problems.append(
                f"{lane_label}: armed but fan-in reports no such lane — restart "
                "jasper-fanin to pick up the lane map"
            )
            continue
        if entry.get("source") != rl_source_ring():
            problems.append(
                f"{lane_label}: armed but fan-in reports source="
                f"{entry.get('source')!r} — jasper-fanin has not restarted since "
                "the lane map changed"
            )
            continue
        ring = entry.get("ring")
        if not isinstance(ring, dict):
            problems.append(f"{lane_label}: source=ring but no ring{{}} block")
            continue
        if not ring.get("attached"):
            reason = ring.get("detach_reason", "unknown")
            problems.append(
                f"{lane_label}: DETACHED (reason={reason}, retries="
                f"{ring.get('retries')}) — {_ring_detach_remedy(str(reason))}"
            )
            continue
        # Attached. Has it ever actually been fed? All-startup empty reads with
        # no filled slot means the renderer has never opened its ring.
        steady = ring.get("empty_reads") or 0
        startup = ring.get("startup_empty_reads") or 0
        frames = entry.get("frames_read") or 0
        if not frames and startup:
            if _ring_lane_is_on_demand(lane_label):
                # An on-demand lane (correction — ephemeral aplay writers,
                # U3/P6c) is fed only while a measurement is playing, so
                # armed-attached-never-fed is its RESTING state between
                # measurements — the same "not a fault" class as a paused
                # renderer, not the daemon-renderer wiring failure the WARN
                # below diagnoses. Warning here would put a standing false
                # warning on every armed box.
                #
                # STATED RESIDUAL: this carve-out cannot tell resting from
                # broken-at-open. A writer that can NEVER open its ring
                # (missing jts-ring membership, geometry shear) produces the
                # SAME armed-attached-never-fed signature, and this branch
                # reports it healthy. Accepted because correction playback
                # is operator-initiated: the first real measurement fails at
                # the point of use (aplay open error -> PlaybackError ->
                # _raise_legacy_error -> the wizard surfaces it), which is
                # louder and better-attributed than a standing doctor WARN
                # could be — but the detail string below must carry the
                # hint so a doctor reading never implies the writer path
                # was PROVEN.
                healthy.append(
                    f"{lane_label}(attached, on-demand, no measurement "
                    "played yet — a writer that cannot open looks identical "
                    "here; run a measurement to confirm)"
                )
                continue
            problems.append(
                f"{lane_label}: attached but NEVER FED (startup_empty_reads="
                f"{startup}, frames_read=0) — the renderer has not opened its "
                f"ring. Check that {_ring_lane_unit(lane_label)} restarted after "
                "the arm, and that its ALSA device resolves"
            )
            continue
        healthy.append(
            f"{lane_label}(writer_alive={ring.get('writer_alive')}, "
            f"occupancy={ring.get('occupancy')}, empty_reads={steady}, "
            f"epoch_resets={ring.get('epoch_resets')})"
        )

    if problems:
        return CheckResult(label_name, "warn", "; ".join(problems))
    return CheckResult(label_name, "ok", "; ".join(healthy))


def rl_source_ring() -> str:
    """The STATUS ``source`` token a ring lane publishes.

    Read from ``jasper.fanin.status`` rather than spelled here, so the doctor
    and the Rust serializer cannot disagree about the token.
    """
    from jasper.fanin.status import FANIN_INPUT_SOURCE_RING

    return FANIN_INPUT_SOURCE_RING


def _ring_lane_is_on_demand(label: str) -> bool:
    """Whether this lane's writers are ephemeral spawns (no renderer unit).

    Unitless lanes (correction, U3/P6c) are fed only while a measurement
    plays; the never-fed WARN's daemon-renderer wiring diagnosis does not
    apply to them.

    STATED RESIDUAL of routing a lane through the on-demand branch: the
    doctor then cannot distinguish "resting between measurements" from "the
    writer can NEVER open its ring" (missing group membership, geometry
    shear) — both are armed-attached-never-fed. Accepted for
    operator-initiated lanes: the first real measurement fails loudly at
    the point of use (the wizard surfaces the playback error), and the
    healthy detail carries a run-a-measurement-to-confirm hint so the
    doctor reading never claims the writer path was proven.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label(label)
    return lane is not None and lane.unit is None


def _ring_lane_unit(label: str) -> str:
    """The restartable thing a lane's remedy strings should name.

    Only unit-ful lanes reach the never-fed remedy (on-demand lanes branch
    off before it), but the fallbacks stay total so a remedy string can
    never interpolate ``None``.
    """
    from jasper import renderer_lanes as rl

    lane = rl.lane_by_label(label)
    if lane is None:
        return "the renderer"
    if lane.unit is None:
        return f"{lane.renderer} (spawn-time writers; nothing to restart)"
    return lane.unit


def _ring_detach_remedy(reason: str) -> str:
    """The remediation for each detach reason. One line each, actionable."""
    if reason == "geometry":
        from jasper import renderer_lanes as rl

        return (
            f"the on-disk ring header disagrees with {rl.RENDERER_LANES_CONF_D}; "
            "re-run `jasper-audio-config renderer-lanes --disarm <lane> --arm "
            "<lane>` (which clears a stale ring) or revert the fan-in geometry "
            "override"
        )
    if reason == "refused":
        from jasper import renderer_lanes as rl

        return (
            f"the ring could not be mapped — usually the renderer user missing "
            f"from group {rl.RING_GROUP!r}, or its unit missing UMask=0007 "
            f"(which leaves a new ring 0640, group-unwritable). Redeploy and "
            "restart the renderer"
        )
    if reason == "orphaned":
        return (
            "the ring at this path was REPLACED while fan-in held it open (an "
            "arm/disarm, or a geometry change, clears and recreates it). The "
            "lane re-latches onto the live file on its own within ~2 s, so a "
            "single sighting is self-healing; persistent means something is "
            "recreating the ring in a loop"
        )
    return (
        "the ring file does not exist yet — normal briefly at boot; persistent "
        "means the renderer has never opened its device"
    )


@doctor_check(order=52.66, group="audio")
def check_outputd_content_lane_park() -> CheckResult:
    """jasper-outputd is not parked on a content-lane open failure.

    ``deploy/bin/jasper-outputd-failure-reconcile`` parks the unit
    out-of-band after ``CONTENT_LANE_PARK_AFTER`` consecutive content-lane
    open failures, so the unit cannot ride ``Restart=on-failure`` into
    ``StartLimitAction=reboot``. That park is TERMINAL for the boot —
    nothing re-arms outputd on its own — and until this check existed the
    record it writes had NO reader: a parked speaker was silent, and so was
    every operator surface.

    Severity is ``fail``, not ``warn``, and the reason is the definition the
    rest of the doctor uses: a park means the speaker emits NOTHING and no
    automatic path recovers it. That is operator-action class. A ``warn``
    would put "your speaker is silent until you intervene" in the same bucket
    as advisory drift.

    The record's own ``action=`` and ``re_arm=`` text is surfaced verbatim
    rather than restated here — the writer picks a LANE-SPECIFIC remedy
    (active vs passive), and a second copy of that prose in Python is a
    guaranteed drift site.
    """
    label = "outputd content lane"

    from ...control import content_lane_state

    state = content_lane_state.snapshot()
    status = state.get("status")

    if status == "absent":
        return CheckResult(
            label, "ok", "no content-lane failure record this boot"
        )

    if status == "unreadable":
        return CheckResult(
            label,
            "warn",
            f"content-lane record at {state.get('path')} exists but could not "
            f"be read ({state.get('error')}) — a park cannot be ruled out. "
            "The record is written root:jasper 0660 by jasper-outputd's "
            "ExecStopPost; check that the reader is in group jasper.",
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"content-lane record at {state.get('path')} is present but its "
            "count/timestamp do not parse (a truncated write) — a park cannot "
            "be ruled out from it. Check jasper-outputd's recent starts "
            "directly; the record is cleared on the next clean stop.",
        )

    if state.get("parked"):
        lane = state.get("lane") or "unknown"
        count = state.get("count")
        parts = [
            f"PARKED — jasper-outputd stopped after {count} consecutive "
            f"{lane}-lane open failures",
        ]
        parked_utc = state.get("parked_utc")
        if parked_utc:
            parts.append(f"at {parked_utc}")
        action = state.get("action")
        if action:
            parts.append(f"ACTION: {action}")
        re_arm = state.get("re_arm")
        if re_arm:
            parts.append(f"RE-ARM: {re_arm}")
        return CheckResult(label, "fail", ". ".join(parts))

    count = state.get("count")
    if state.get("streak_live"):
        return CheckResult(
            label,
            "warn",
            f"content-lane open is failing: {count} consecutive failure(s) "
            "recorded within the streak window, below the park threshold. "
            "Usually a transient wait for CamillaDSP to open its half of the "
            "pair at boot; persistent means the lane will park.",
        )
    return CheckResult(
        label,
        "ok",
        f"content lane recovered — a stale record of {count} failure(s) "
        f"remains ({state.get('age_sec')}s old, outside the streak window); "
        "it is cleared on the next clean stop",
    )
