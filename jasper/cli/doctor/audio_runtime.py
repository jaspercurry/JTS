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
from ...env_load import merged_env_files
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

_OUTPUTD_EXPECTED_DAC_PCM = "outputd_dac"

_OUTPUTD_EXPECTED_DUAL_DAC_PCM = "dual_apple_usb_c_dac_4ch"

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
    """outputd's env as its own unit layers it, read fresh.

    BOTH FILES, in ``jasper-outputd.service``'s own ``EnvironmentFile=`` order:
    ``outputd.env`` then ``grouping-outputd.env``, so a bonded box's grouping
    pins win here exactly as they win for the daemon. Reading only the first
    layer reported a bonded box on an env it is not running. (The grouping layer
    no longer pins a content bridge — ADR-0100 left the round-trip lane no route
    to pin — but it still pins the lane's own keys, so the order still matters.)

    The merge is :func:`jasper.control.transport_park._outputd_env`'s, consumed
    rather than restated, so the doctor and ``/state`` cannot disagree about
    what outputd's env says.
    """
    from ...control.transport_park import _outputd_env

    override = os.environ.get("JASPER_OUTPUTD_ENV_FILE")
    if override:
        # The test/operator seam for the FIRST layer only; the grouping layer
        # keeps its own path so an override cannot hide a bonded pin.
        from ...multiroom.reconcile import OUTPUTD_GROUPING_ENV_FILE

        return merged_env_files((override, OUTPUTD_GROUPING_ENV_FILE))
    return _outputd_env()


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

    The RENDERER side of the snd-aloop graph, which the ring did not
    replace: each renderer writes its own private lane and jasper-fanin
    reads all of them. A lane whose slave or wire has drifted from
    `deploy/alsa/asoundrc.jasper` costs that renderer its audio, so this
    is file-level drift detection against the shipped template.

    It does NOT check CamillaDSP's capture. Under ADR-0100 the ring is
    the one fan-in → CamillaDSP transport, and what carries it is
    reported elsewhere: `check_ring_platform_assets` for whether the
    ioplug, the conf.d drop-in and /dev/shm/jts-ring EXIST, and
    `check_ring_geometry_coherence` for whether the `jts_ring_capture`
    block's geometry agrees with fan-in's env and the on-disk header. A
    third partial reader of that same drop-in here would be a second
    answer, not a stronger one. It is not an AEC check either: the
    bridge's reference is jasper-outputd's UDP speaker monitor and it
    opens no ALSA tap.

    The one assertion that is not about the graph is the trailing
    `audio_topology.env` probe, which warns about a leftover from the
    retired dmix/fanin switcher.
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

    stale_state = Path("/var/lib/jasper/audio_topology.env")
    if stale_state.exists():
        return CheckResult(
            label,
            "warn",
            f"fan-in asoundrc is correct, but stale {stale_state} still "
            f"exists from the retired dmix/fanin switcher. Re-run "
            f"deploy/install.sh to archive/remove it.",
        )

    return CheckResult(label, "ok", "renderer/test lanes 0..4")

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
    # The ring is the only transport a running fan-in can be on (ADR-0100): the
    # daemon refuses any other declaration at config parse and parks at exit 78,
    # so a LIVE STATUS can only come from a ring box. Expected is therefore a
    # constant, NOT a mapping from the persisted file — deriving it from
    # /var/lib/jasper/fanin.env would FAIL a healthy box whose key has not been
    # written yet (coupling-auto runs After=jasper-fanin.service).
    actual_transport = output.get("transport")
    if actual_transport != "shm_ring":
        return CheckResult(
            "jasper-fanin service",
            "fail",
            f"active but STATUS output.transport={actual_transport!r}; "
            "expected 'shm_ring' — the SHM ring is fan-in's only transport "
            "toward CamillaDSP. Check journalctl -u jasper-fanin for the "
            "transport it actually opened.",
        )
    ring = output.get("ring")
    if not isinstance(ring, dict):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS is missing output.ring metrics — "
            "fan-in is not actually writing Ring A. Check "
            "journalctl -u jasper-fanin for event=fanin.ring.opened.",
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
    if not isinstance(input_buffer_frames, int):
        return CheckResult(
            "jasper-fanin service",
            "fail",
            "active but STATUS missing integer input_buffer_frames",
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

    Reachability is owned by the 'jasper-fanin service' check — an unreadable
    STATUS, or one carrying no ring block, reports OK here rather than
    double-failing a daemon that check already fails.

    Returns:
      - ok when the ring is draining normally, when STATUS carries no ring
        block, or when STATUS is unreachable
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
        # A running fan-in always publishes a ring block (ADR-0100), so this is
        # a STATUS-shape guard, not a topology branch: with no counters to read
        # there is nothing to assess. The missing block itself is FAILed by the
        # 'jasper-fanin service' check — one fact, one owner.
        return CheckResult(
            name,
            "ok",
            "not assessed — STATUS carries no output.ring block; the "
            "'jasper-fanin service' check owns that failure",
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


def _graph_feeds_the_bond(config_path: Path) -> bool:
    """Is this graph's post-DSP endpoint the bond rather than a local ring?

    A bonded LEADER's camilla#1 plays into the Snapcast pipe
    (:data:`jasper.multiroom.reconcile.SNAPFIFO`, imported rather than
    respelled) and never touches a ring device. Any OTHER ``File`` sink is not
    this endpoint — a stale local pipe is a real fault and must keep failing the
    playback axis, which is why the filename is compared and not just the type.
    """
    from ...multiroom.reconcile import SNAPFIFO

    return (
        _loaded_playback_type(config_path) == "File"
        and _loaded_playback_filename(config_path) == SNAPFIFO
    )


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

    THIS CHECK FAILS OPEN ON A CONFIG IT CANNOT READ, and that bound has to be
    stated because the check is cited elsewhere as the detector for a suppressed
    DSP reconcile (PR #2601). An unreadable / absent statefile, an unresolvable
    ``config_path``, or a config with no ``devices.playback.format`` field all
    return ``ok`` here — deliberately, because "I could not read it" is not
    evidence of a mismatch, and a second reason for one absent file would bury
    the check that names the fix. So this catches a config that is present and
    WRONG, never a config that is missing.

    The unreadable half is owned by ``check_correction_current_config``
    (``jasper/cli/doctor/correction.py``), which reads the statefile through the
    SAME :func:`_active_camilla_config_path` helper and is the one that speaks:
    ``warn`` when ``config_path`` cannot be read out of the statefile, ``fail``
    when the statefile points at a config that does not exist. So the pair
    covers both states between them, and neither restates the other's verdict.
    See ``captures/PLAN-wide-output-path-2026-08-07.md`` PR-1, PR-6, D4, D5.
    """
    label = "camilla playback format"
    _, config_path = _active_camilla_config_path()
    if config_path is None:
        # FAIL-OPEN, disclosed in the docstring above: no readable config is not
        # a mismatch, and a sibling check owns the absent-statefile verdict.
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
        f"fanin={plan.setting('JASPER_FANIN_INPUT_BUFFER_FRAMES').value}"
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
        ROUTE_LATENCY_RERUN_ACTION,
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
        f"artifact_hash={summary.get('route_config_hash')}, "
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
    if status == "warn" and summary.get("recommendation") != ROUTE_LATENCY_RERUN_ACTION:
        # A p99-promotion recommendation is about evidence that is already
        # valid; the re-run remedy below would misdirect it.
        return CheckResult("route latency evidence", "warn", detail)
    return CheckResult(
        "route latency evidence",
        "warn" if status == "warn" else "fail",
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
    from jasper.fanin.ring_health import FANIN_ENV_PATH
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
            "transport — the ring is the only one. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile --auto to converge the box and clean "
            "the file.",
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
    """The loaded CamillaDSP graph must name this box's ring devices.

    Since ADR-0100 the ring is the only transport, so the capture axis has ONE
    expectation and no second one to select between: ``jts_ring_capture``
    (Ring A). The playback axis has two legal endpoints — the post-DSP ring this
    box's endpoint marker names (``jts_ring_playback``, or
    ``jts_ring_active_playback`` once the active endpoint is armed), or the
    Snapcast pipe a bonded LEADER feeds instead of any local ring. A graph
    naming anything else is a stale artifact — CamillaDSP reads or writes a
    device nobody is at the far end of. The fix is to re-run the ordered
    reconciler: ``jasper-fanin-coupling-reconcile shm_ring``.

    KEYED ON THE LOADED GRAPH, never on ``JASPER_FANIN_CAMILLA_COUPLING``. A
    running fan-in is on the ring whatever that file says — the daemon refuses
    every other value at parse and parks — so a healthy box whose key has not
    been written yet (coupling-auto runs ``After=jasper-fanin.service``) read as
    a half-applied transition while the expectation came from the file. The
    file's own legacy-token question belongs to
    :func:`check_fanin_coupling_value`, and whether outputd consumes what this
    graph writes belongs to :func:`check_ring_split_transport`.
    """
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
        RING_PLAYBACK_DEVICE,
        ring_active_endpoint_armed,
    )
    from jasper.multiroom.reconcile import SNAPFIFO

    label = "fan-in coupling"
    # _active_camilla_config_path returns (statefile, active_config_path|None);
    # the active path is what CamillaDSP actually loaded. Fall back to the JTS
    # sound config when the statefile names nothing.
    _, active_path = _active_camilla_config_path()
    config_path = Path(active_path) if active_path else Path(
        "/var/lib/camilladsp/configs/sound_current.yml"
    )
    capture = _loaded_capture_type(config_path)
    if capture is None:
        # No JTS config loaded yet (fresh box / non-JTS graph) — nothing to
        # compare the expectation against.
        return CheckResult(label, "ok", "no loaded capture to compare")

    # WHAT PUTS A BOX HERE. A CamillaDSP restart re-seeds the graph the
    # statefile points at, so a STALE ARTIFACT (one emitted before the endpoint
    # moved) reappears on the next restart — that is the finding-5 revert shape,
    # and its repair is a baseline re-emit, not a re-arm. The MARKER is not moved
    # by a camilla restart at all: its only writer is
    # jasper-audio-hardware-reconcile, which re-derives it from the loaded graph
    # on boot, on udev, and on every deploy — so those are the events that can
    # de-arm a box whose artifact drifted, and a camilla restart alone is not one
    # of them.
    #
    # WHICH post-DSP ring is EXACTLY ONE answer, taken from the reconciler's
    # marker — not "either is fine". Accepting both would read green through the
    # crossing this rung exists to prevent: a roleful box's graph pointed at the
    # full-range stereo ring, or a stereo box's at the active ring.
    armed = ring_active_endpoint_armed()
    expected_playback = RING_ACTIVE_PLAYBACK_DEVICE if armed else RING_PLAYBACK_DEVICE
    # A ROLEFUL box with a CLEARED marker has no honest stereo expectation:
    # jts_ring_playback is a FORBIDDEN token for every active emitter, so such a
    # box's graph can never name it, and printing "(expected jts_ring_playback)"
    # would send an operator to a device the emitters refuse to write. The honest
    # statement is that this box is mid-arm — the artifact moved to the ring but
    # the marker has not been re-derived (or the reverse) — and the remedy is the
    # ladder, not a re-arm.
    roleful = _requires_roleful_graph()
    capture_device = _loaded_device_field(config_path, "capture", "device")
    playback_device = _loaded_device_field(config_path, "playback", "device")
    ring_mismatches: list[str] = []
    if capture != "Alsa" or capture_device != RING_CAPTURE_DEVICE:
        ring_mismatches.append(
            f"capture={capture}/{capture_device or '(missing)'} "
            f"(expected Alsa/{RING_CAPTURE_DEVICE})"
        )
    # A box has TWO legal post-DSP endpoints, and only one of them is a ring: a
    # bonded LEADER's camilla#1 writes the Snapcast pipe and reaches no ring
    # device at all (jasper.multiroom.reconcile owns that lane). The playback
    # axis is this check's business only on the ring endpoint; comparing anyway
    # read `(missing)` against a ring name and warned a box feeding the group.
    feeds_the_bond = _graph_feeds_the_bond(config_path)
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
    # Severity stays WARN. Under the arm ladder this is a mid-procedure
    # TRANSIENT — the graph moves first, the marker is re-derived second — so a
    # box observed between those steps is exactly this state and is not broken.
    # What the detail owes the operator is the command that finishes it.
    if roleful:
        # The first two steps are the SAME ladder the transport-park check
        # records, composed from its constant rather than respelled, so the
        # two surfaces cannot prescribe different things to the same
        # operator while both live. Only the third step is this check's own.
        from ...control.transport_park import ACTIVE_ENDPOINT_REMEDY

        recovery = (
            f"the ACTIVE-ring ladder, in order: {ACTIVE_ENDPOINT_REMEDY} && "
            "sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring"
        )
    else:
        recovery = (
            "run: sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile "
            "shm_ring"
        )
    return CheckResult(
        label,
        "warn",
        "the loaded graph is not this box's ring config: "
        f"{'; '.join(ring_mismatches)}; a stale baseline artifact re-seeded on a "
        f"camilla restart is the usual cause (finding-5 revert); {recovery}",
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


def _jts_ring_probeable_pcms() -> list[tuple[str, str]]:
    """The ``(pcm, tool)`` pairs an open-probe may safely touch right now.

    EMPTY unless ``jasper-fanin`` is inactive: fan-in is Ring A's only writer,
    and a box whose fan-in is down is not carrying audio through any ring, so
    nothing can be disturbed by opening one. While fan-in runs, a probe would
    contend with a live transport for no diagnostic gain — the ioplug is
    demonstrably registered, because the graph is using it.

    Within that, only PCMs whose ring FILE is absent: an existing file may still
    be attached (CamillaDSP writes Ring B, outputd reads it), and an absent one
    cannot be. That is a GATE, not a guarantee — the ``exists`` check races a
    daemon restarting underneath it — but a probe that loses that race still
    unlinks only what it created, and the ioplug's SPSC guard is what refuses it.
    """
    if _run(["systemctl", "is-active", "jasper-fanin.service"]).stdout.strip() == (
        "active"
    ):
        return []
    probeable = []
    for pcm, tool, _ring_basename in _JTS_RING_PCMS:
        ring_path = _jts_ring_path_for(pcm)
        if ring_path and not os.path.exists(ring_path):
            probeable.append((pcm, tool))
    return probeable


def _jts_ring_pcm_resolves(pcm: str, tool: str) -> tuple[bool, str]:
    """Open-probe one inert jts_ring PCM. Success means ALSA resolved the
    conf.d name AND dlopen()ed the ioplug .so AND the writer-dead/no-reader
    silence path terminated. A 1-second probe against an absent ring is
    safe: the ioplug free-runs (playback) or emits timer-paced silence
    (capture) rather than blocking (the lab resolvability-step contract).

    THE ONLY DETECTION for the -DPIC / arch-mismatch class (a structurally
    invalid .so that presence checks pass and ALSA cannot dlopen). Reached only
    through :func:`_jts_ring_probeable_pcms`, which is what keeps it off a ring
    the daemons are using.

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
    # 4 s, not 6: the caller probes up to three PCMs in one row, and a doctor row
    # is cut off at 15 s — at 6 s each, three hung probes overrun it and the
    # operator loses the stderr this probe exists to surface. 4 s still leaves
    # 3 s of slack over a 1 s capture/playback.
    try:
        proc = _run(
            [tool, "-D", pcm, "-c", str(channels), "-r", "48000",
             "-f", sample_format, "-d", "1", sink],
            timeout=4.0,
        )
    except subprocess.TimeoutExpired:
        return False, "open probe hung (>4 s) — ioplug no-reader/no-writer path may be broken"
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


def _grouped_dac_content_lane_parked() -> bool:
    """Is this box the bonded shape whose post-DSP hop is not a ring at all?

    ``jasper.control.transport_park`` is the one place that answers which park a
    box is in, so this asks it rather than re-deriving "is it bonded" from the
    grouping config — a second derivation would drift from the surface the
    household and ``/state`` read.

    Fail-soft to False: an unreadable topology must not silence a real split.
    The park check's own ``unavailable`` branch is where that read failure is
    reported.
    """
    from ...control import transport_park

    try:
        state = transport_park.snapshot()
    except Exception:  # noqa: BLE001 - a park read must never crash a sibling
        return False
    return any(
        park.get("park_class") == transport_park.PARK_GROUPED_DAC_CONTENT_LANE
        for park in (state.get("parks") or [])
    )


@doctor_check(order=51.72, group="audio")
def check_ring_split_transport() -> CheckResult:
    """The two ends of the post-DSP hop must agree about the ring.

    One predicate, both directions, and either one is SILENCE:

    * the loaded CamillaDSP graph writes a post-DSP ring while
      ``JASPER_OUTPUTD_CONTENT_BRIDGE`` is not ``shm_ring`` — outputd reads its
      ALSA content lane and nobody consumes the ring;
    * the bridge is ``shm_ring`` while the loaded graph writes somewhere else —
      outputd waits on a ring CamillaDSP is not filling.

    Both leave the speaker silent with every daemon healthy and every other
    check green, which is why this is a ``fail``.

    KEYED ON THE BRIDGE, not on ``JASPER_FANIN_CAMILLA_COUPLING``. Under
    ADR-0100 that file selects nothing — a running fan-in is on the ring
    whatever it says — so it cannot imply what outputd reads. The bridge is what
    decides that (``rust/jasper-outputd/src/config.rs``), and it is observable
    from outputd's own env, so this check reports the state the operator can act
    on rather than an intent the daemons ignore.

    IT IS ALSO THE DETECTION SURFACE FOR AN INTERRUPTED CONVERGENCE (#2285 P7).
    ``jasper.fanin.converge`` moves the graph to the ring and the pass flips the
    bridge seconds later; a process killed between those leaves exactly this
    split. It self-heals at the next boot, deploy or DAC hotplug — the
    convergence step is idempotent and re-enters from current state — and until
    then this check is what names it, which is why it carries the remedy.

    WHY TWO TERMS AND NOT THREE — do not "helpfully" add the third back.
    The original design specified a third conjunct, ``writer_alive:false``.
    It is not observable in this check's own target state, and conditioning on
    it would make the check silently never fire:

    * ``writer_alive`` is a READER-REPORTED metric — ``jts_ring_shm.h:99``
      states it is "what a reader REPORTS". With outputd off the ring NOTHING
      reads it, so there is no reader to report it.
    * outputd publishes it only inside its ``shm_ring`` block, and
      ``shm_ring_path`` is ``Some`` **iff** ``JASPER_OUTPUTD_CONTENT_BRIDGE=
      shm_ring`` (``rust/jasper-outputd/src/state.rs:220-223``). Off the ring
      that block is ``{"enabled": false}`` with no further fields, so the key is
      absent exactly when this check would need it.

    A BOX WITH NEITHER END ON THE RING IS NOT THIS CHECK'S FAULT. That pair is
    coherent; whether the shape is one the single transport can serve at all is
    :func:`check_ring_transport_park`'s question, and whether the graph is the
    one this box should be running is :func:`check_fanin_coupling`'s.

    NOR IS THE GROUPED PARK. A bonded box whose dac_content lane is armed runs
    ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct`` by design (its grouping env pins
    it, and ``jasper.cli.doctor.grouping`` requires that pin while bonded) while
    its graph still loads the stereo ring — a pair this predicate reads as a
    split on a box that is playing. It is a park with a name and an issue, and
    the arm this check would prescribe is one
    ``coupling_supported_for_route`` refuses for exactly that box, so the park
    is carved out rather than double-reported with a dead remedy.

    KNOWN TRANSIENT. The arm ladder moves the graph first and the bridge later,
    so a doctor run taken INSIDE an active arm ladder can FAIL here for the
    seconds between those rungs. That is the ladder working, not a fault. The
    arm's own terminal doctor pass is the authoritative read.
    """
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        output_endpoint_evidence_from_statefiles,
    )
    from jasper.fanin_coupling import (
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        outputd_bridge_is_ring,
    )

    label = "ring split transport"
    if _grouped_dac_content_lane_parked():
        # A bonded box's grouping env pins the DIRECT bridge and its graph feeds
        # the dac_content lane, so the pair below reads as a split while the box
        # is playing. That shape has an owner — `check_ring_transport_park`
        # names it `grouped_dac_content_lane` with its tracked issue — and this
        # check must not prescribe an arm the coupling support matrix refuses
        # for exactly that box.
        return CheckResult(
            label, "ok", "grouped dac_content lane; see the transport-park check"
        )
    outputd_env = _outputd_reconciled_env()
    # `(unset, = the ring)` rather than a bare `(unset)`: an undeclared bridge IS
    # the ring (config.rs), so a reader who saw only "unset" beside a ring graph
    # would think the pair disagreed when it agrees.
    bridge = (
        str(outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip()
        or "(unset, = the ring)"
    )
    outputd_on_ring = outputd_bridge_is_ring(
        outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
    )

    # BOTH STATEFILES, first-recognized-endpoint-wins — the same reader the
    # `check_outputd_service` transport note used before #2285 folded that note
    # into this check. Reading only the primary was the gap that fold left: an
    # active leader keeps a program-bake graph in the primary statefile and its
    # real output endpoint in camilla#2's, so a box whose primary names no
    # registered endpoint (a bake, a parked graph, a statefile pointing at a
    # deleted config) while camilla#2 names the ACTIVE ring would have gone
    # quiet from BOTH surfaces. Measured, not assumed: two such shapes were
    # constructed on disk and are pinned in
    # tests/test_doctor_audio_runtime.py.
    #
    # The primary path still comes from `_active_camilla_config_path()` so an
    # operator's `JASPER_CAMILLA_STATEFILE` override keeps working; the reader
    # falls through to camilla#2 by itself when the primary is unreadable, which
    # is why the old hardcoded `sound_current.yml` rescue is gone rather than
    # kept beside it.
    statefile, _ = _active_camilla_config_path()
    evidence = output_endpoint_evidence_from_statefiles(
        statefile, DEFAULT_CAMILLA2_STATEFILE_PATH
    )
    playback_device = (evidence.devices or {}).get("playback_device")
    graph_on_ring = playback_device in (
        RING_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
    if graph_on_ring == outputd_on_ring:
        return CheckResult(
            label,
            "ok",
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge}, loaded graph playback="
            f"{playback_device or '(none)'}",
        )
    if graph_on_ring:
        stranded = (
            f"the loaded graph writes {playback_device} but "
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge}, which names no "
            "transport outputd can serve — it parks instead of reading, and "
            "nothing consumes the ring"
        )
    else:
        stranded = (
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge} but the loaded graph "
            f"writes {playback_device or '(none)'}, so outputd waits on a ring "
            "CamillaDSP is not filling"
        )
    return CheckResult(
        label,
        "fail",
        f"SPLIT TRANSPORT: {stranded} — this speaker is SILENT while every "
        "daemon looks healthy. Converge the pair: sudo /opt/jasper/.venv/bin/"
        "jasper-fanin-coupling-reconcile shm_ring. (Transient and expected if "
        "an arm ladder is running right now; the ladder's own final doctor pass "
        "is the authoritative read.)",
    )


@doctor_check(order=51.73, group="audio")
def check_active_ring_path_projection() -> CheckResult:
    """A ring PATH that lags its endpoint marker is SILENT — outputd refuses it.

    The complement of :func:`check_ring_split_transport`, and the two partition
    the bridge: that one owns every state where the graph and the bridge
    disagree about the ring, and this one starts where they agree ON the ring.
    Between them the ACTIVE-ring arm ladder has no unowned rung.

    The state this catches: with outputd on the ring bridge, its ring PATH
    disagrees with its endpoint MARKER. outputd enforces a biconditional — the
    active ring file may be read only by an armed active endpoint, and an armed
    active endpoint may read only that file — so it bails at startup on the
    crossed pair, with ``RestartPreventExitStatus=78`` parking the unit rather
    than looping. The speaker is silent, and the daemon that would have explained
    it is not running.

    WHY IT CANNOT LIVE IN ``check_outputd_service``. That check reports the same
    contradiction from ``transport_coherence_report``, but only after
    ``_outputd_service_state_failure()`` and the STATUS read — and at this exact
    state outputd is NOT active, so it returns the systemd failure first and the
    transport finding is never reached. A check whose whole target state makes
    the daemon refuse to start cannot depend on that daemon's live surface, so
    this one reads persisted evidence only — outputd's own env, which is both
    the gate (the content bridge) and the subject (the ring path and the
    endpoint marker the path projects).

    BOTH DIRECTIONS, one question. The path is a projection of the marker with a
    single derivation (``_outputd_ring_path_for``), so "is the projection
    current?" is one predicate covering the arm lag (marker armed, path still
    Ring B) and the disarm lag (marker cleared, path still the active ring).

    KNOWN TRANSIENT, like its sibling. The marker's writer runs first and the
    path's writer follows, so a doctor run taken between those two rungs FAILs
    here for the seconds between them. That is the ladder working. The remedy is
    the same pass the ladder's own next step runs.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_ENV_PATH
    from jasper.fanin.coupling_reconcile import _outputd_ring_path_for
    from jasper.fanin_coupling import (
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_RING_PATH_ENV_VAR,
        outputd_bridge_is_ring,
        resolve_outputd_ring_path,
    )
    from jasper.env_file import read_value

    label = "active ring path projection"
    # THE BRIDGE, not the persisted coupling: outputd's ring-path allowlist runs
    # iff outputd is on the ring bridge, so that key is what makes this one live
    # or inert. A box off the ring bridge is the sibling check's subject.
    #
    # LAYERED, because a bonded box's grouping env pins this key and the unit
    # reads that file last — the gate has to see what outputd sees.
    declared = str(
        _outputd_reconciled_env().get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or ""
    ).strip()
    if not outputd_bridge_is_ring(declared):
        # A box that named the retired route reads no ring, so the allowlist has
        # nothing to project. It is not healthy — it parks — and the park is the
        # transport-park check's subject, not this one's.
        return CheckResult(
            label,
            "ok",
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={declared}; no ring path to read",
        )
    # The SUBJECT stays outputd.env's own text: the marker and the ring path are
    # single-writer keys of that file (the grouping layer pins neither), and
    # `_outputd_ring_path_for` is contracted on one snapshot of the file being
    # reconciled — a merged view would fork that derivation.
    try:
        outputd_env = Path(DEFAULT_OUTPUTD_ENV_PATH).read_text(encoding="utf-8")
    except OSError:
        outputd_env = ""
    carried = resolve_outputd_ring_path(
        read_value(outputd_env, OUTPUTD_RING_PATH_ENV_VAR)
    )
    derived = _outputd_ring_path_for(outputd_env)
    if carried == derived:
        return CheckResult(label, "ok", f"{OUTPUTD_RING_PATH_ENV_VAR}={carried}")
    return CheckResult(
        label,
        "fail",
        f"RING PATH LAGS ITS MARKER: {OUTPUTD_RING_PATH_ENV_VAR}={carried} but "
        f"this box's endpoint marker derives {derived}. outputd may read the "
        "active ring file only from an armed active endpoint and vice versa, so "
        "it refuses the pair at startup (exit 78, no restart) — this speaker is "
        "SILENT. Converge the pair: sudo /opt/jasper/.venv/bin/"
        "jasper-fanin-coupling-reconcile shm_ring. (Transient and expected if an "
        "arm ladder is running right now; the ladder's own final doctor pass is "
        "the authoritative read.)",
    )


@doctor_check(order=51.8, group="audio", exclusive_group="audio-probe")
def check_ring_platform_assets() -> CheckResult:
    """Verify the jts_ring transport platform assets are present.

    Three assets: the compiled ioplug .so, the conf.d PCM definitions
    (jasper.ring_assets.RING_CONF_PCMS), and the /dev/shm/jts-ring directory.
    They are ALWAYS load-bearing since ADR-0100 — the ring is the only fan-in →
    CamillaDSP transport, so CamillaDSP's capture and post-DSP playback resolve
    through them on every box.

    Statuses:
      ok    — .so + conf.d + shm dir present.
              CAVEAT: presence passes on a STALE .so left by a failed rebuild
              (the 2026-07-02 class) — it is structurally valid, so it dlopens
              and registers. `check_ring_ioplug_provenance` is the check that
              separates the two, by comparing the installer's record against the
              plugin on disk. See ring-platform.sh.
      fail  — an asset is missing: the graph cannot resolve its ring devices,
              and there is no second transport to fall back to. Or a present
              ioplug ALSA cannot actually load (the -DPIC / arch-mismatch
              class), which only the open-probe can tell apart from a healthy
              one.

    THE OPEN-PROBE RUNS ONLY WHERE IT CANNOT DISTURB ANYTHING —
    :func:`_jts_ring_probeable_pcms` decides, on fan-in's systemd state plus
    each ring file's absence, and answers nothing on a box carrying audio. That
    gate replaced a persisted-token one (``JASPER_FANIN_CAMILLA_COUPLING``),
    which probed a LIVE ring on any box whose key had not been written yet
    (coupling-auto runs ``After=jasper-fanin.service``).

    The "is the ring coherent + alive" verdict is not here: it belongs to
    `check_fanin_coupling` (the loaded graph) and `check_ring_geometry_coherence`
    (env vs conf.d vs the on-disk header).
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

    if missing:
        # ONE VERDICT, because there is no inert phase left to be degraded into.
        # This used to split on a persisted ``ring_armed`` token: a missing asset
        # FAILED an armed box and merely WARNED an unarmed one, on the reasoning
        # that "loopback still carries audio". ADR-0100 retired that route, so the
        # unarmed branch claimed a transport the box does not have — a speaker
        # emitting nothing, reported as degraded-but-playing. The verdict no
        # longer turns on a persisted token at all: the ring is the only way this
        # box makes sound, and an asset it needs is absent.
        return CheckResult(
            label,
            "fail",
            "a ring-platform asset is missing: "
            + "; ".join(missing)
            + " — the ring is this box's only transport, so its graph cannot "
            "resolve the ring devices and nothing carries audio; redeploy "
            "(bash scripts/deploy-to-pi.sh) to rebuild them.",
        )

    present_detail = "ioplug + conf.d + /dev/shm/jts-ring present"
    probeable = _jts_ring_probeable_pcms()
    if not probeable:
        return CheckResult(
            label,
            "ok",
            f"{present_detail} (open-probe skipped — fan-in is running or a ring "
            "file is in use; see the 'fan-in coupling' and 'ring geometry' checks)",
        )
    failures = []
    for pcm, tool in probeable:
        ok, detail = _jts_ring_pcm_resolves(pcm, tool)
        if not ok:
            failures.append(f"{pcm}: {detail}")
    if failures:
        return CheckResult(
            label,
            "fail",
            "the ring ioplug is installed but ALSA cannot open through it: "
            + "; ".join(failures)
            + " — a structurally invalid plugin (the -DPIC / arch-mismatch "
            "class) passes a presence check and still cannot carry audio; "
            "redeploy (bash scripts/deploy-to-pi.sh) to rebuild it.",
        )
    return CheckResult(
        label,
        "ok",
        f"{present_detail}; open-probe resolved "
        + ", ".join(pcm for pcm, _tool in probeable),
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
        from ...fanin.ring_health import (
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

    * a wire that renders no conf.d field beyond the ioplug's own defaults needs
      nothing from any installed plugin, so "cannot vouch" is a ``warn`` — real,
      but nothing is refused on such a box;
    * a wire that declares a non-default sample FORMAT is refused at the arm by
      ``ring_wire_caps_ready``, which is a ``fail``: the gate fails closed and
      leaves the box exactly as it was found — a roleful box parks its content
      lane (ADR-0178) — never a fallback to a second transport (ADR-0100).
      Reporting that as a warning would bury the one verdict that predicts a
      stalled arm.

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
    Measured." It stays reachable because nothing stops an operator (or a
    cleanup script) from ``rm -rf``-ing the tmpfs directory out from under a
    live writer. The C comment names its own fix, a
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
                "it — anything that rm -rf's /dev/shm/jts-ring voids "
                "exclusivity silently. Stop the extra writer, then re-arm the "
                "lane.",
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

    The GROUPING ring is judged for the same reason and rides the same shape: its
    writer is snapclient through that same C ioplug, so it has no witness either,
    and its reader is the bonded endpoint's CamillaDSP. It is SILENT until
    something creates the file — ``present=False`` — so listing it here costs one
    stat on every box and self-arms on the box where the ring exists.

    Judges every ring in the tuple below, because the fault is a property of a
    ring file rather than of a coupling, and reports per-ring so an operator
    knows which daemon to look at. Absent / idle rings are silent:
    ``present=False`` covers the unarmed fleet, where none of these files exists
    at all.

    Returns:
      - ok when no ring is stalled (including every unarmed box, where there is
        nothing to judge)
      - warn naming each stalled ring, its two heartbeat ages, and the reader
        daemon to check. WARN not FAIL: the ring self-recovers the instant the
        reader resumes, and the household's remedy is the same either way.
    """
    from jasper.multiroom.grouping_ring import GROUPING_RING_FILE
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
        # Path imported from the grouping transport's own identity module, not
        # respelled here — one owner for the name and the file.
        ("GROUPING ring (snapclient -> CamillaDSP)", GROUPING_RING_FILE),
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

    UNCONDITIONAL since ADR-0100. Ring A is never inert: it is the only fan-in →
    CamillaDSP transport, so the graph opens it on every box. This used to skip
    unless the persisted coupling read ``shm_ring``, which silently stopped
    assessing a crash-loop-class defect on any box whose key had not been written
    yet. A mismatch is ``fail`` (the graph cannot run); an indeterminate conf.d /
    env is ``warn`` (redeploy to reinstall).
    """
    label = "ring geometry"
    try:
        from jasper.fanin.ring_health import (
            FANIN_ENV_PATH,
            resolve_effective_fanin_ring_slots,
        )
        from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    except ImportError as e:  # pragma: no cover - always importable in prod
        return CheckResult(label, "warn", f"ring modules unavailable: {e}")

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
            f"{resolution.error}. fan-in will refuse to create Ring A, which is "
            "this box's only transport. Clear the stale value.",
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
            "CamillaDSP's ioplug attach fails (hw_params EINVAL) "
            "and crash-loops. Run: sudo /opt/jasper/.venv/bin/"
            "jasper-fanin-coupling-reconcile shm_ring (it self-heals a stale env), "
            "or match the two values.",
        )

    # Axis 3: the on-disk ring header (what the writer actually created).
    header = ring_assets.read_ring_header(ring_assets.RING_A_PROGRAM_FILE)
    if not header.valid:
        # No coherent on-disk ring yet: fan-in may be between restarts, or the
        # ring was cleared. The env/conf.d agree, so the next writer create
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
    about the other one. A ROLEFUL (active-crossover) box does not ring on the
    strength of its floor however good it is: its ring is the ACTIVE ring, which
    the unattended pass arms only for a box already carrying a proven graph
    (``ring_roleful_unattended_ready``'s two arms — a hardware-fingerprint-matched
    applied baseline, or the all-muted anchor) and refuses otherwise. So on a
    roleful box a green period line
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
        "does not make it ring on its own. The unattended default pass now "
        "does the whole thing by itself for a box carrying a proven graph — a "
        "hardware-fingerprint-matched applied baseline, or the all-muted "
        "startup anchor: it re-emits the graph at the ring endpoint, "
        "re-derives the hardware markers, and arms. So this box converges at "
        "the next boot, deploy or DAC hotplug with no command at all. A box "
        "carrying NEITHER graph still refuses, and its explicit ladder is "
        "`jasper-active-speaker baseline-reemit --endpoint ring`, then "
        "jasper-audio-hardware-reconcile, then "
        "`jasper-fanin-coupling-reconcile shm_ring` — that first step works on "
        "a mid-commission box, re-staging the all-muted startup anchor when no "
        "applied baseline is saved yet."
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
            from jasper.fanin.ring_health import load_topology_for_wire
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
        "jasper-fanin-coupling-reconcile --auto only if the coupling check also "
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
    expected_dac_pcm: str,
) -> tuple[str, str, str, bool] | CheckResult:
    """Validate outputd's live topology, endpoint coherence, PCMs, and references.

    OUTPUTD'S OWN ENV IS THE EXPECTATION, not ``JASPER_FANIN_CAMILLA_COUPLING``.
    Under ADR-0100 the fan-in file selects nothing — the daemon serves the ring
    whatever it says — so it cannot predict what outputd opened. The content
    bridge does, and ``outputd_env`` here is the daemon's own env read through
    the unit's ``EnvironmentFile=`` layering (:func:`_outputd_reconciled_env`),
    so a bonded box's grouping pin is part of the expectation exactly as it is
    part of what outputd started with. That makes this comparison "is the
    running daemon on the env it was last given?" — a restart it missed after a
    reconcile. Whether that env is the RIGHT one for this box is
    :func:`check_ring_split_transport`'s question.
    """
    from jasper.fanin_coupling import (
        COUPLING_SHM_RING,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        outputd_bridge_is_ring,
    )
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
        output_endpoint_evidence_from_statefiles,
        transport_coherence_report,
        transport_topology_for_coupling,
    )

    bridge = (
        str(outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip()
        or "(unset, = the ring)"
    )
    outputd_on_ring = outputd_bridge_is_ring(
        outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
    )
    # ``None`` is the planner's own spelling for "not the ring" — it resolves to
    # the non-ring shape without this module naming a retired token.
    coupling = COUPLING_SHM_RING if outputd_on_ring else None
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
            f"{expected_content_source!r} for {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}="
            f"{bridge!r} in outputd's own env — the running daemon is on an older "
            "env than the file. Run jasper-fanin-coupling-reconcile --auto to "
            "restart outputd onto it.",
        )
    live_outputd_env = dict(outputd_env)
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
        # NOTES ARE DELIBERATELY NOT ELEVATED HERE — do not re-add the WARN.
        # Every note has an OWNING check that FAILs on the same state and
        # carries the runnable remedy, so elevating the same fact to a WARN
        # under this check's name would make one problem read as two. The two
        # notes and their owners, which between them cover both rungs of the
        # ACTIVE-ring arm ladder and partition the content bridge:
        #
        #   graph rung (the graph and the bridge disagreeing about the ring)
        #     -> :func:`check_ring_split_transport`
        #   endpoint rung (the ring PATH lagging its endpoint marker on the
        #     ring bridge) -> :func:`check_active_ring_path_projection`
        #
        # Both owners read PERSISTED evidence rather than outputd's live STATUS,
        # which is what lets them fire at all: at the endpoint rung outputd has
        # refused to start, so this function returns its systemd failure long
        # before reaching here. Errors above still FAIL here, because those are
        # outputd's own coherence, not a ladder rung.
    local_pipe_detail = f"content_source={actual_content_source}"
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
        outputd_on_ring,
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


@doctor_check(order=52.67, group="audio")
def check_camilla_recover_park() -> CheckResult:
    """The core DSP graph is not parked by jasper-camilla-recover.

    ``deploy/bin/jasper-camilla-recover`` parks the graph — CamillaDSP
    stopped out-of-band, every core-graph client already stopped — when its
    one bounded recovery pass cannot bring it back (ADR-0175). That park is
    TERMINAL for the boot by design: the handler stops re-entering so the box
    holds a stable floor instead of stopping all eleven units again every
    cooldown.

    Severity is ``fail`` for the same reason the outputd park's is: a park
    means the speaker emits NOTHING and no automatic path recovers it.

    The record's own ``action=``/``re_arm=`` text is surfaced verbatim rather
    than restated here — a second copy of that prose in Python is a
    guaranteed drift site.
    """
    label = "camilla recovery park"

    from ...control import camilla_recover_state

    state = camilla_recover_state.snapshot()
    status = state.get("status")

    if status == "absent":
        return CheckResult(
            label, "ok", "no core-graph recovery park this boot"
        )

    if status == "unreadable":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} exists but could "
            f"not be read ({state.get('error')}) — a park cannot be ruled "
            "out. Check journalctl -u jasper-camilla-recover.",
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} is present but "
            "carries no reason (a truncated write) — a park cannot be ruled "
            "out from it. Check journalctl -u jasper-camilla-recover.",
        )

    parts = [
        f"PARKED — the core DSP graph was stopped after a failed recovery "
        f"({state.get('reason')})",
    ]
    parked_utc = state.get("parked_utc")
    if parked_utc:
        parts.append(f"at {parked_utc}")
    for field, prefix in (
        ("detail", ""),
        ("action", "ACTION: "),
        ("re_arm", "RE-ARM: "),
    ):
        value = state.get(field)
        if value:
            parts.append(f"{prefix}{value}")
    return CheckResult(label, "fail", ". ".join(parts))


@doctor_check(order=52.68, group="audio")
def check_ring_transport_park() -> CheckResult:
    """No topology this box declares is one the ring cannot serve (ADR-0178).

    ADR-0100 makes ``shm_ring`` the only central transport; ADR-0178 names the
    four shapes it cannot carry and the tracked issue each waits on. Severity
    follows whether the loopback route still exists:

    * ring-only and parked — the speaker emits NOTHING and no automatic path
      recovers it, which is the ``fail`` definition the rest of the doctor
      uses for a park;
    * loopback still legal — ``warn``. The box plays today; the disclosure is
      the inventory of what the transport deletion will park, named issue by
      issue so the operator can clear them before it lands, not after.

    The classification, the issue numbers and the remedy text all come from
    ``jasper.control.transport_park`` — the same reader
    ``/state.resilience.transport_park`` and the household audio card use, so
    the three surfaces cannot name different issues for the same box.
    """
    label = "ring transport parks"

    from ...control import transport_park

    state = transport_park.snapshot()
    status = state.get("status")

    if status == "unavailable":
        return CheckResult(
            label,
            "warn",
            "the saved output topology or outputd's env could not be read "
            f"({state.get('error')}) — a transport park cannot be ruled out.",
        )

    if status == "ok":
        # The honest claim, not "the ring can serve this box": a box with no
        # ring geometry that no class names lands in `unclassified` below, and
        # this sentence must not have already told the operator it was fine.
        return CheckResult(
            label, "ok", "this box is in none of the four named transport parks"
        )

    if status == "unclassified":
        return CheckResult(
            label,
            "warn",
            "this box's declared topology resolves no ring geometry of either "
            "kind, and none of the four named parks describes it — so it is "
            "neither servable by the single transport nor tracked by an issue "
            "yet. Report the saved layout (/sound/setup/) so it can be named.",
        )

    parks = state.get("parks") or []
    named = []
    for park in parks:
        part = f"{park.get('park_class')} ({park.get('detail')})"
        issue = park.get("issue")
        if issue:
            part = f"{part}. TRACKED: {issue}"
        remedy = park.get("remedy")
        if remedy:
            part = f"{part}. REMEDY: {remedy}"
        named.append(part)

    if status == "parked":
        return CheckResult(
            label,
            "fail",
            "PARKED — no ring serves this box, so it emits nothing: "
            + "; ".join(named),
        )
    return CheckResult(
        label,
        "warn",
        "this box plays on the loopback route today, but the ring cannot "
        "serve it — it parks when the loopback route is deleted: "
        + "; ".join(named),
    )


# ---------------------------------------------------------------------------
# The aloop-remnant guard (audio-graph consolidation #2285, P9-C).
#
# WHY THIS EXISTS. P9-C moved every solo box's content lane onto SHM rings and
# deleted snd-aloop pair 5's PCM definitions, leaving the bounded remnant this
# check measures: the pairs `_derive_registered_pairs` reads out of their owning
# constants below. (The bonded grouping ingress used to ride pair 6 raw; it is
# on `jasper.multiroom.grouping_ring`'s SHM ring now and declares no aloop pair
# at all.) A bounded remnant is only bounded if
# something measures it, and the design names the failure mode directly
# (risk 5.1): "the remnant becomes permanent by silence." This check is the
# measurement, and it names in its own text who owns what is left, so an
# operator who reads it learns where the remnant is scheduled to die.
#
# #2508 retired the GROUPING half — the bonded ingress that used to ride pair 6
# raw. ADR-0100 then retired the rest: outputd's passive content lane (pair 6)
# and fan-in's summed music output (pair 7) both moved to SHM rings, and
# deploy/alsa/asoundrc.jasper declares neither. What survives is fan-in's five
# CAPTURE lanes and nothing else.
#
# WHAT IT ASSERTS. Every OPEN aloop substream has a REGISTERED purpose, where
# the registered set is DERIVED — never restated — from the one place that
# still owns a pair allocation:
#
#   pairs 0-4  `_FANIN_EXPECTED_ALOOP_INPUTS`   (above, this module)
#
# Deriving rather than tabulating is what makes retirement MECHANICAL: a pair
# stops being registered the moment its owning constant stops naming it, with
# no edit here and no phase label to remember. A hand-maintained table would
# be a fourth restatement of the allocation (after the modprobe conf prose,
# asoundrc.jasper, and the constant above) and would silently keep
# permitting a lane after its owner dropped it — risk 5.1's "permanent by
# silence" transplanted onto the very instrument built to measure it.
#
# The input roster is read in its ALOOP form deliberately, not through
# `_fanin_expected_inputs()`: a ring-armed renderer lane still RESERVES its
# aloop pair (nothing else may take it), so the registered set must not flap
# with arming state.
#
# Pairs 5, 6 and 7 are absent because no owner names any of them: P9-C deleted
# pair 5's PCM definitions and ADR-0100 deleted pairs 6 and 7's. So an open
# pair in that range is a box that resurrected a deleted lane (a rolled-back
# binary, a stale asoundrc, a hand-started process) and is exactly the
# regression those two changes can produce. That is the FAIL. Reserved rather
# than reclaimed, per deploy/modprobe.d/snd-aloop.conf: pcm_substreams stays 8
# so no surviving pair renumbers.
#
# NOT ASSERTABLE, AND NOW PERMANENTLY SO — the first half of design :497. That
# bullet opened "snd-aloop present ⟹ grouping-capable box" (restated at :441 as
# "the module loads only on a grouping-capable box"). It was carried forward on
# the premise that the remnant would become grouping-only. It did the opposite:
# grouping LEFT pair 6 for the SHM ring, and every pair the set still registers
# belongs to fan-in's capture side on every box. `check_loopback`
# (jasper/cli/doctor/audio.py) asserts the module must be present fleet-wide,
# which is now simply the end state rather than a temporary disagreement. The
# clause is recorded as retired, not carried.
#
# WHY NOT A LITERAL SET. The registered pairs stay DERIVED even now that only
# one owner is left, because a literal would keep permitting a lane after its
# owner dropped it. Live evidence for why the surviving five must not be
# blanket-failed, 2026-08-14: jts4 holds `/proc/asound/Loopback/pcm1c/sub3` in
# `state: RUNNING`, owner cgroup `jasper-fanin.service` — the usbsink lane's
# idle-read fallback that deploy/modprobe.d/snd-aloop.conf documents ("fan-in
# still OPENS hw:Loopback,1,3 as the usbsink lane's idle read fallback when USB
# Audio Input is off"). jts3 and jts.local held nothing. A guard that fails on
# a documented, healthy holder is a false positive, and a doctor that cries
# wolf is worse than no doctor.
#
# BOUNDED. At most 4 PCM directories x `_ALOOP_SUBSTREAMS` status reads (32
# small procfs files), plus one `comm`/`cgroup` read per offender, capped at
# `_ALOOP_OFFENDER_DETAIL_CAP` offenders in the message.
#
# FAIL-SOFT. An unreadable /proc is `warn`, never an exception and never a
# FAIL: the guard must not convert "I could not look" into "something is
# wrong".
#
# SERIALIZED. `exclusive_group="audio-probe"` shares a lane with
# `check_renderer_device_resolvable` (renderers.py), which opens real PCMs with
# `aplay` as its probe. Today the false positive is UNREACHABLE: every PCM that
# probe opens maps to a pair this check still registers (0/1/2 renderer lanes,
# 4 correction), so an observed temporary open reads as `ok` either way. The
# serialization is kept because an unregistered pair briefly held open by a
# co-tenant probe would read as an offender, and its cost is near zero.
# ---------------------------------------------------------------------------

#: procfs ALSA root. Overridable for tests, matching the established
#: `JASPER_ASOUND_ROOT` hook already used by deploy/bin/jasper-camilla-recover
#: and jasper/cli/xvf_profile.py, so this check tests the same way the rest of
#: the tree already does.
_ALOOP_PROC_ROOT_ENV = "JASPER_ASOUND_ROOT"
_ALOOP_PROC_ROOT_DEFAULT = "/proc/asound"

#: The snd-aloop card id, pinned by deploy/modprobe.d/snd-aloop.conf
#: (`id=Loopback`).
_ALOOP_CARD_ID = "Loopback"

#: snd-aloop exposes two PCM devices, each with a playback and a capture side.
#: Verified on jts3 (2026-08-14): /proc/asound/Loopback contains exactly
#: pcm0c, pcm0p, pcm1c, pcm1p, each with sub0..sub7.
_ALOOP_PCM_DIRS = ("pcm0p", "pcm0c", "pcm1p", "pcm1c")

#: `pcm_substreams=8` in deploy/modprobe.d/snd-aloop.conf — pairs 0..7. Pinned
#: against that file by tests/test_doctor_audio_runtime.py so a future
#: reduction cannot leave this walker scanning a range the module no longer
#: has, and cannot leave the owning constants naming a pair that no longer
#: exists.
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
    """``{pair: provenance}`` derived from the owning facts.

    The registered set is never written down here — it is read out of the
    constant that already owns the allocation (see the header comment). A pair
    leaves the set exactly when its owner stops naming it, which is how pairs 6
    and 7 left when ADR-0100 moved both onto SHM rings.

    Returns None if ANY source entry is unparseable. That is deliberately
    all-or-nothing: a partial derivation would silently shrink the registered
    set, and a shrunk set turns legitimate holders into doctor FAILs. Better to
    say "I could not derive this" than to red-doctor a healthy speaker.
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

    Returns ``""`` when nothing is readable. Naming the offender is the point
    of the FAIL, but a procfs race (the owner exits between the status read
    and the comm read) must never turn into a crash or a different verdict —
    every step here degrades to LESS DETAIL, never to a changed status.
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
            # Keep only the leaf unit — the full cgroup path is long and the
            # unit name is the actionable half.
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

    Audio-graph consolidation #2285, P9-C. See the header comment above for
    the full rationale; the operator-facing summary:

    - snd-aloop absent            -> ok (no remnant on this box)
    - registered set underivable  -> warn (never a shrunken set)
    - /proc unreadable            -> warn (fail-soft; never a FAIL)
    - every open pair registered  -> ok, reporting the remnant's current size
    - an UNREGISTERED pair open   -> fail, naming the offender

    The registered set is DERIVED from the constant that owns the pair
    allocation, so it shrinks on its own as pairs retire. The unregistered
    cases are pairs 5, 6 and 7: P9-C deleted pair 5's PCM definitions and
    ADR-0100 deleted pairs 6 and 7's, so anything holding one of them has
    resurrected a deleted lane.
    """
    label = "aloop remnant"

    card_dir = _aloop_proc_root() / _ALOOP_CARD_ID
    if not card_dir.is_dir():
        return CheckResult(
            label,
            "ok",
            f"snd-aloop not loaded ({card_dir} absent) — no aloop remnant "
            "on this box",
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
                # A substream this kernel does not expose is not evidence of
                # anything — absence is a narrower module, not a fault.
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
            "lanes (pairs 0-4) are registered: pair 5's PCM definitions were "
            "DELETED by #2285 P9-C, and pairs 6 and 7's by ADR-0100 when the "
            "content lane and the summed music output both moved to SHM rings. "
            "A holder there means a rolled-back binary or a stale "
            "/etc/asound.conf resurrected a deleted lane. Identify the process "
            "above, stop it, and re-run `bash scripts/deploy-to-pi.sh` to "
            "restore the shipped ALSA config.",
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
