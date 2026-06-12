"""HTTP control surface for external clients (dial, future wall switches,
home automation). Bound to LAN so an ESP32 dial on the household network
can drive volume / transport / session.

Stack: stdlib http.server (ThreadingHTTPServer), pycamilladsp client,
VolumeCoordinator (source-aware dispatch).

The route table is in `_make_handler` below — `do_GET` and `do_POST`
own the dispatch in one place rather than mirroring the list here
(that mirror went stale several times). Highlights:

- Volume + transport + session-bypass: dial-driven actions.
- /state: cross-daemon JSON snapshot — voice / audio / renderers /
  satellites; consumable from the /voice web UI, jasper-doctor, or
  `curl`.
- /cue/play: proxy to voice_daemon's UDS so a cue plays through
  the daemon's already-correctly-gained TtsPlayout.
- /dial/status: focused dial heartbeat (subset of /state.satellites.dial,
  kept because jasper-doctor calls it directly).

Volume dispatch: requests build a fresh VolumeCoordinator per call
(matches the per-request _dispatch_transport pattern). The coordinator
reads the canonical listening_level from /var/lib/jasper/speaker_volume.json,
applies the change, dispatches to the active source (or CamillaDSP
when idle), persists. This daemon doesn't run inbound observers —
that's voice_daemon's job. Both daemons converge through persistence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
import logging
import math
import os
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable, Optional

from ..http_security import management_read_allowed, mutating_request_allowed
from ..audio_quality import (
    DEFAULT_CONVERTER as _default_audio_converter,
    apply_requested_converter as _apply_audio_quality,
    converter_options as _audio_converter_options,
    normalize_converter as _normalize_audio_converter,
    read_active_converter as _read_active_audio_converter,
    read_state as _read_audio_quality_state,
)
from . import (
    bootloop_guard_state,
    debug_control,
    grouping_supervisor,
    mpris,
    shairport_supervisor,
    system_supervisor,
    wifi_guardian_state,
)
from .. import identity_state
from ..multiroom.config import GROUPING_ENV_FILE, validate_grouping
from ..multiroom.state import grouping_response, read_grouping_state
from ..music_sources import MUSIC_SOURCE_SPECS
from ..transit.state import read_state as read_transit_state
from ..volume_diagnostics import (
    build_volume_policy_snapshot,
    read_diagnostics as _read_volume_diagnostics,
)
from ..audio_profile_state import (
    AecIntent,
    MicProbe,
    build_audio_profile_status,
    infer_audio_input_profile,
    normalize_audio_input_profile,
    parse_env_bool as _parse_audio_profile_bool,
    profile_env_updates,
    resolve_audio_input_intent,
    runtime_env_from_mapping,
)
from ..audio_validation import (
    current_artifact_filter_kwargs as _audio_validation_filter_kwargs,
)
from ..audio_validation import latest_artifact_summary as _audio_validation_summary
from ..env_load import subprocess_env_with_fresh_files
from ..atomic_io import locked_update_env_file
from ..wake_models import WAKE_MODEL_FILE

logger = logging.getLogger(__name__)
dial_log = logging.getLogger("jasper.dial")
SOURCE_SELECT_IDS = {spec.id.value for spec in MUSIC_SOURCE_SPECS}
SOURCE_AVAILABILITY_TTL_SEC = 10.0
_source_availability_cache: tuple[float, dict[str, Any]] | None = None
_source_availability_lock = threading.Lock()
AUDIO_QUALITY_RENDERER_UNITS = [
    "shairport-sync.service",
    "librespot.service",
    "bluealsa-aplay.service",
    "jasper-usbsink.service",
]
OUTPUTD_BASE_CAMILLA_CONFIG = "/etc/camilladsp/outputd-cutover.yml"


def _safe_audio_quality_state() -> dict[str, Any]:
    try:
        return _read_audio_quality_state()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio quality state read failed")
        converter = _default_audio_converter
        options = _audio_converter_options()
        meta = next(
            option for option in options if option["converter"] == converter
        )
        try:
            active = _read_active_audio_converter()
        except Exception:  # noqa: BLE001
            active = None
        return {
            "converter": converter,
            "active_converter": active,
            "label": meta["label"],
            "summary": meta["summary"],
            "options": options,
            "error": str(e),
        }


def _same_config_path(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(str(left)) == os.path.realpath(str(right))


def _sound_apply_target(last_apply: Any) -> str | None:
    if not isinstance(last_apply, dict):
        return None
    for key in ("active_config_path", "candidate_config_path"):
        value = last_apply.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _sound_runtime_status(
    sound_profile: dict[str, Any],
    active_config_path: str | None,
) -> dict[str, Any]:
    """Describe whether the desired sound profile is actually loaded.

    ``sound_profile["enabled"]`` is the persisted preference. The
    runtime truth is CamillaDSP's active config path, which can differ
    after rollback, install repair, or a manual Camilla reload. Keep the
    distinction explicit so status surfaces do not imply EQ is active
    when the daemon is running the flat outputd base config.
    """

    last_apply_path = _sound_apply_target(sound_profile.get("last_dsp_apply"))
    try:
        filter_count = int(sound_profile.get("filter_count") or 0)
    except (TypeError, ValueError):
        filter_count = 0
    desired_has_filters = bool(sound_profile.get("enabled")) and filter_count > 0
    runtime = {
        "active_config_path": active_config_path,
        "last_apply_config_path": last_apply_path,
        "matches_last_apply": None,
        "state": "unknown",
        "active": None,
        "warning": None,
    }
    if not active_config_path:
        return runtime

    if last_apply_path:
        runtime["matches_last_apply"] = _same_config_path(
            active_config_path,
            last_apply_path,
        )

    if _same_config_path(active_config_path, OUTPUTD_BASE_CAMILLA_CONFIG):
        runtime["state"] = "base"
        runtime["active"] = not desired_has_filters
    elif runtime["matches_last_apply"] is True:
        runtime["state"] = "applied"
        runtime["active"] = True
    elif last_apply_path:
        runtime["state"] = "mismatch"
        runtime["active"] = False
    else:
        runtime["state"] = "custom"
        runtime["active"] = None

    if desired_has_filters and runtime["active"] is not True:
        runtime["warning"] = (
            "Desired sound profile is not the active CamillaDSP config."
        )
    return runtime


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


CONTROL_MAX_POST_BYTES = _env_int("JASPER_CONTROL_MAX_POST_BYTES", 4096)


# Most-recent dial heartbeat. Updated by the UDP log listener every
# time a datagram arrives; read by GET /dial/status. Kept module-level
# so jasper-doctor can ask "is a dial actually talking to us?" without
# parsing the journal. Lock isn't needed — Python dict assignment is
# atomic and a stale read is harmless for a heartbeat.
#
# Persisted to disk so `last_seen_ip` survives a jasper-control
# restart. Without this, every restart (typically a deploy) leaves
# the in-memory dict empty until the next user-initiated dlog —
# encoder turn or button press — which makes /state.satellites.dial.online
# briefly inaccurate for any external consumer. The file is tiny
# (~150 B) and writes happen at dlog rate (a few per second under
# heavy dial use), well within SD-card tolerance.
DIAL_HEARTBEAT_PATH = os.environ.get(
    "JASPER_DIAL_HEARTBEAT_PATH",
    "/var/lib/jasper/dial_heartbeat.json",
)
MUX_CONTROL_SOCKET_PATH = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET",
    "/run/jasper-mux/control.sock",
)


def _load_dial_heartbeat() -> dict[str, Any]:
    """Read the persisted heartbeat dict. Returns the empty default
    on any error (missing file, malformed JSON, wrong types) — a
    corrupted persisted file should never block the daemon from
    starting. Field-level type checks prevent a stale or
    hand-edited file from injecting odd values into /state.
    """
    default: dict[str, Any] = {
        "last_seen_at": None,
        "last_seen_ip": None,
        "last_message": None,
    }
    try:
        with open(DIAL_HEARTBEAT_PATH) as f:
            blob = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    if not isinstance(blob, dict):
        return default
    ts = blob.get("last_seen_at")
    ip = blob.get("last_seen_ip")
    msg = blob.get("last_message")
    return {
        "last_seen_at": ts if isinstance(ts, (int, float)) else None,
        "last_seen_ip": ip if isinstance(ip, str) else None,
        "last_message": msg if isinstance(msg, str) else None,
    }


def _persist_dial_heartbeat(snapshot: dict[str, Any]) -> None:
    """Atomically write the heartbeat snapshot. Fail-soft — a write
    error logs at WARN but never raises into the UDP listener's
    receive loop. tempfile+rename guarantees readers never see a
    half-written file."""
    try:
        directory = os.path.dirname(DIAL_HEARTBEAT_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = DIAL_HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, DIAL_HEARTBEAT_PATH)
    except OSError as e:
        dial_log.warning(
            "dial heartbeat persistence: write to %s failed: %s",
            DIAL_HEARTBEAT_PATH, e,
        )


_dial_heartbeat: dict[str, Any] = _load_dial_heartbeat()


# Same range jasper.tools.audio uses for the voice-driven volume tools.
# Percent↔dB is the normal attenuation curve. VolumeCoordinator layers
# Camilla main_mute on top at 0% so content/music 0% is a real mute.
VOLUME_MIN_DB = -50.0
VOLUME_MAX_DB = 0.0


def _clamp_db(db: float) -> float:
    return max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, float(db)))


def _db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return max(0, min(100, round((float(db) - VOLUME_MIN_DB) / span * 100.0)))


def _percent_to_db(percent: int) -> float:
    p = max(0, min(100, int(percent)))
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return VOLUME_MIN_DB + (span * p / 100.0)


def _delta_db_to_delta_percent(delta_db: float) -> int:
    """Convert a legacy-scale dB delta to a listening-level percent
    delta. The dial firmware sends fixed deltas like ±2.5 dB per
    encoder tick; we map those onto the 0-100 percent scale using
    the same 50 dB span the camilla-only path used. ±5 dB == ±10pp."""
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return round(float(delta_db) / span * 100.0)


# ---------- peering daemon supervisor ----------

# The peering daemon runs an asyncio event loop; jasper-control is
# stdlib threaded HTTP. Bridge by spawning a single background daemon
# thread that owns the asyncio loop for peering. When peering is OFF
# (the default), the thread is not even created — zero cost on a
# single-Pi household.
_peering_thread: threading.Thread | None = None


def _run_peering_loop() -> None:
    """Background thread target: own an asyncio loop, run the
    PeeringDaemon until the process exits."""
    # Lazy imports — keep jasper-control's import cost light when
    # peering is OFF and these modules never load.
    from ..peering import load_config
    from ..peering.daemon import PeeringDaemon

    cfg = load_config()
    if not cfg.enabled:
        logger.info(
            "event=peering.thread.exit mode=%s — daemon will not start",
            cfg.mode.value,
        )
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = PeeringDaemon(cfg)
    try:
        loop.run_until_complete(daemon.start())
        loop.run_forever()
    except Exception:  # noqa: BLE001
        logger.exception("peering daemon thread crashed")
    finally:
        try:
            loop.run_until_complete(daemon.stop())
        except Exception:  # noqa: BLE001
            logger.exception("peering daemon stop failed")
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def start_peering_daemon_if_enabled() -> None:
    """Start the peering daemon in a background thread iff peering
    is enabled in /var/lib/jasper/peering.env. Idempotent — repeated
    calls are no-ops once the thread exists.

    The check is done in the worker thread (not here) so that even
    when peering is OFF, we don't pay the cost of importing zeroconf.
    """
    global _peering_thread
    if _peering_thread is not None:
        return
    _peering_thread = threading.Thread(
        target=_run_peering_loop,
        name="peering-daemon",
        daemon=True,
    )
    _peering_thread.start()


_AEC_MODE_FILE = "/var/lib/jasper/aec_mode.env"
_WAKE_MODEL_FILE = WAKE_MODEL_FILE
_JASPER_ENV_FILE = "/etc/jasper/jasper.env"

# Default leg policy — must match deploy/install.sh's reconcile_aec_state
# and deploy/bin/jasper-aec-reconcile's ensure_mode_file. Raw is on
# by default (~5 MB / negligible CPU, gives OR-fusion wake-rate
# recovery), DTLN is off by default (~75 MB / ~25% one core, opt-in),
# chip-AEC is off by default (hardware-conditional, mutually exclusive
# with raw/DTLN — the chip-AEC promotion).
_LEG_DEFAULT_RAW = True
_LEG_DEFAULT_DTLN = False
_LEG_DEFAULT_CHIP_AEC = False
_PROFILE_DEFAULT = "custom"

# Operator-facing wake-leg toggle name -> jasper.wake_legs token(s). Values
# are tuples because one operator toggle can arm more than one leg: the
# "chip_aec" toggle (JASPER_WAKE_LEG_CHIP_AEC) arms BOTH fixed-beam legs
# (chip_aec_150 + chip_aec_210), with the reconciler fanning the single
# boolean out to JASPER_MIC_DEVICE_CHIP_AEC_150/_210. The chip-direct /
# AEC-OFF leg is exposed to operators (the /wake/ card, /aec/leg, the
# JASPER_WAKE_LEG_RAW env var, the bash reconciler) as "raw", but its frozen
# wire token is "off". Do NOT confuse "raw" with the "raw0" corpus-only leg
# (chip channel 2, no toggle). This map is the single place those mappings
# are spelled out; leg-toggle validation goes through its keys. See
# docs/HANDOFF-mic-fusion-architecture.md.
_TOGGLE_TO_TOKEN = {
    "raw": ("off",),
    "dtln": ("dtln",),
    "chip_aec": ("chip_aec_150", "chip_aec_210"),
}


def _parse_env_bool(raw: str, default: bool) -> bool:
    """Same normalization the bash reconciler does — accept yes/no/etc."""
    return _parse_audio_profile_bool(raw, default)


def _read_aec_state() -> dict:
    """Full /var/lib/jasper/aec_mode.env state — mode + both leg
    booleans. Missing keys fall back to the documented defaults so a
    partial file from a pre-leg-toggle deploy still parses sanely.

    The reconciler's ensure_mode_file appends any missing keys on its
    next run, so this fallback is a one-pass deal — but it must be
    correct for the GET that races that first reconcile."""
    state = {
        "mode": "auto",
        "leg_raw": _LEG_DEFAULT_RAW,
        "leg_dtln": _LEG_DEFAULT_DTLN,
        "leg_chip_aec": _LEG_DEFAULT_CHIP_AEC,
        "profile": "",
    }
    file_found = False
    try:
        with open(_AEC_MODE_FILE) as f:
            file_found = True
            for line in f:
                line = line.strip()
                if line.startswith("JASPER_AEC_MODE="):
                    val = line.split("=", 1)[1].strip().strip("'\"") or "auto"
                    state["mode"] = val
                elif line.startswith("JASPER_WAKE_LEG_RAW="):
                    state["leg_raw"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_RAW,
                    )
                elif line.startswith("JASPER_WAKE_LEG_DTLN="):
                    state["leg_dtln"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_DTLN,
                    )
                elif line.startswith("JASPER_WAKE_LEG_CHIP_AEC="):
                    state["leg_chip_aec"] = _parse_env_bool(
                        line.split("=", 1)[1], _LEG_DEFAULT_CHIP_AEC,
                    )
                elif line.startswith("JASPER_AUDIO_INPUT_PROFILE="):
                    state["profile"] = normalize_audio_input_profile(
                        line.split("=", 1)[1],
                        default=_PROFILE_DEFAULT,
                    )
    except OSError:
        pass
    if not state["profile"]:
        if file_found:
            state["profile"] = infer_audio_input_profile(
                AecIntent(
                    mode=state["mode"],
                    raw_enabled=bool(state["leg_raw"]),
                    dtln_enabled=bool(state["leg_dtln"]),
                    chip_aec_enabled=bool(state["leg_chip_aec"]),
                ),
            )
        else:
            state["profile"] = "auto"
    return state


def _read_aec_mode() -> str:
    """Compatibility shim — returns just the mode string."""
    return _read_aec_state()["mode"]


def _write_aec_mode(mode: str) -> None:
    """Atomic write of the AEC mode key, preserving leg keys."""
    if mode not in ("auto", "disabled"):
        raise ValueError(f"invalid mode: {mode!r}")
    _atomic_rewrite_env(
        _AEC_MODE_FILE,
        {
            "JASPER_AEC_MODE": mode,
            "JASPER_AUDIO_INPUT_PROFILE": "custom",
        },
    )


def _write_aec_leg(leg: str, enabled: bool) -> None:
    """Atomic write of one wake-leg boolean, preserving every other key
    in aec_mode.env (mode, the other leg).

    Caller is responsible for kicking the reconciler — this just
    persists the user's intent. Restart blast-radius lives in the
    reconciler since it has the actual mode + presence context."""
    if leg not in _TOGGLE_TO_TOKEN:
        raise ValueError(f"invalid leg: {leg!r}")
    key = f"JASPER_WAKE_LEG_{leg.upper()}"
    _atomic_rewrite_env(
        _AEC_MODE_FILE,
        {
            key: "1" if enabled else "0",
            "JASPER_AUDIO_INPUT_PROFILE": "custom",
        },
    )


def _write_audio_input_profile(profile: str) -> None:
    """Write a canonical audio input profile plus rollback-safe leg keys."""

    normalized = normalize_audio_input_profile(profile, default="")
    if not normalized or normalized == "custom":
        raise ValueError(f"invalid profile: {profile!r}")
    _atomic_rewrite_env(_AEC_MODE_FILE, profile_env_updates(normalized))


def _atomic_rewrite_env(path: str, updates: dict) -> None:
    """Read-modify-write of a systemd env file. Updates the given keys,
    preserves all others. Atomic via tempfile + rename. Used by
    _write_aec_mode and _write_aec_leg so concurrent toggles can't
    leave the file half-written."""
    from ..web._common import read_env_file, write_env_file
    state = read_env_file(path)
    state.update(updates)
    write_env_file(path, state, mode=0o644)


def _read_wake_threshold() -> float:
    """Read JASPER_WAKE_THRESHOLD from /var/lib/jasper/wake_model.env
    (the /wake/ wizard's home) with the daemon's compiled-in default
    (0.3) as fallback. Same precedence the daemon uses on startup."""
    try:
        from ..web._common import read_env_file
        val = read_env_file(_WAKE_MODEL_FILE).get("JASPER_WAKE_THRESHOLD", "")
    except OSError:
        val = ""
    if not val:
        val = os.environ.get("JASPER_WAKE_THRESHOLD", "")
    try:
        # Mirror the daemon's compiled-in default (jasper/config.py:469,
        # `wake_threshold=_env_float("JASPER_WAKE_THRESHOLD", 0.3)`, also
        # shipped in .env.example) so the slider + /state show what's
        # actually live. A higher fallback here would make a Save at the
        # displayed value silently raise the real threshold.
        return float(val) if val else 0.3
    except ValueError:
        return 0.3


def _write_wake_threshold(value: float) -> None:
    """Atomic write of JASPER_WAKE_THRESHOLD into wake_model.env,
    preserving JASPER_WAKE_MODEL. Both keys are wizard-managed by the
    /wake/ page (model picker writes JASPER_WAKE_MODEL via the form
    save; sensitivity slider posts to /wake/sensitivity which lands
    here)."""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"threshold out of range: {value}")
    locked_update_env_file(
        _WAKE_MODEL_FILE,
        {"JASPER_WAKE_THRESHOLD": f"{value:.2f}"},
        mode=0o644,
    )


def _aec_bridge_active() -> bool:
    """True if jasper-aec-bridge.service is currently active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "jasper-aec-bridge.service"],
            capture_output=True, text=True, timeout=2.0,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _kick_aec_reconciler() -> None:
    """Apply a persisted AEC-mode/leg change through the reconciler.

    Use `restart`, not `start`: the reconciler is a Type=oneshot unit.
    A rapid toggle can write new intent while the previous reconcile is
    still active; `systemctl start` would be a no-op in that state and
    leave runtime env one click behind the UI.
    """
    subprocess.Popen(
        ["systemctl", "restart", "--no-block",
         "jasper-aec-reconcile.service"],
    )


# Forwarded pair-volume requests carry this header; its presence stops a
# second hop (see _maybe_forward_volume_to_leader's loop breaker).
_PAIR_FORWARD_HEADER = "X-JTS-Pair-Forwarded"

# Seam for tests: the forward's ONE network call. Patching the stdlib
# urllib.request.urlopen would also intercept the test driver's own HTTP
# client (and anything else in-process); this alias scopes the double to
# the pair forward.
_pair_urlopen = urllib.request.urlopen


def _pair_follower_leader_addr() -> str | None:
    """The leader's handle when THIS speaker is an active bonded follower,
    else None. One tiny env-file read per call (multiroom.config.load_config
    — never the runtime derive with its systemctl/RPC probes: this gates
    every /volume request)."""
    from ..multiroom.config import load_config as _load_grouping

    cfg = _load_grouping()
    if (
        cfg.enabled
        and cfg.error is None
        and cfg.role == "follower"
        and cfg.leader_addr
    ):
        return cfg.leader_addr
    return None


def _kick_grouping_reconciler() -> None:
    """Apply a persisted grouping change through jasper-grouping-reconcile.

    Mirror of _kick_aec_reconciler: `restart` (not `start`) the Type=oneshot
    reconciler so a change written while a previous reconcile is still active
    is not a no-op. The reconciler is the single writer of the snapcast unit
    state + the outputd tap; this just nudges it to re-read grouping.env.
    """
    subprocess.Popen(
        ["systemctl", "restart", "--no-block",
         "jasper-grouping-reconcile.service"],
    )


def _write_grouping(
    *, enabled: bool, role: str, channel: str, bond_id: str, leader_addr: str,
) -> None:
    """Persist a grouping role into the wizard-owned grouping.env.

    Read-modify-write (via _atomic_rewrite_env) so operator-tuned
    JASPER_GROUPING_BUFFER_MS / _CODEC survive a role change. This is the
    single control-plane WRITER of grouping.env; jasper-grouping-reconcile is
    the single READER->action. The endpoint that calls this is the same
    no-auth LAN surface as the dial's /volume — so one speaker can configure
    another by POSTing to its :PORT/grouping/set (the bond-forming flow).
    """
    _atomic_rewrite_env(GROUPING_ENV_FILE, {
        "JASPER_GROUPING": "on" if enabled else "off",
        "JASPER_GROUPING_ROLE": role,
        "JASPER_GROUPING_CHANNEL": channel,
        "JASPER_GROUPING_BOND_ID": bond_id,
        "JASPER_GROUPING_LEADER_ADDR": leader_addr,
    })


def _fresh_jasper_env() -> dict[str, str]:
    """Fresh view of /etc/jasper/jasper.env.

    jasper-control is long-lived while the AEC reconciler mutates this
    file when mic mode changes, so `os.environ` can be stale. Status
    surfaces should prefer the file and fall back to process env only for
    keys absent from the file.
    """
    from ..env_load import parse_env_file
    return parse_env_file(os.environ.get("JASPER_ENV_FILE", _JASPER_ENV_FILE))


def _read_wake_word_status() -> dict[str, Any]:
    """Wake model label for the /wake/ status card."""
    from .. import wake_models
    from ..web._common import read_env_file
    try:
        state = read_env_file(_WAKE_MODEL_FILE)
    except OSError:
        state = {}
    model = (state.get("JASPER_WAKE_MODEL") or "").strip()
    if not model:
        model = os.environ.get("JASPER_WAKE_MODEL", "").strip() or "hey_jarvis"
    entry = wake_models.by_model(model)
    return {
        "model": model,
        "label": entry.label if entry else model,
        "pronunciation": entry.pronunciation if entry else "",
        "custom": entry is None,
    }


def _audio_profile_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    """Read-only mic/profile status for the /wake/ page.

    This is intentionally descriptive and side-effect-free: it reads the
    reconciler-owned env file plus the XVF profile's firmware helpers,
    then classifies intent vs observed runtime. It does not probe audio
    streams or open devices on the hot polling path.
    """
    env = _fresh_jasper_env()
    runtime = runtime_env_from_mapping(env, process_env=os.environ)
    try:
        from ..mics import xvf3800
        xvf_present = xvf3800.is_present()
        capture_channels = xvf3800.capture_channels()
        recommended_channels = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
        display_name = xvf3800.DISPLAY_NAME
        probe_error = None
    except Exception:  # noqa: BLE001
        xvf_present = False
        capture_channels = None
        recommended_channels = 6
        display_name = "Seeed ReSpeaker XVF3800 (USB UA)"
        probe_error = "firmware probe failed"

    return build_audio_profile_status(
        AecIntent(
            mode=state["mode"],
            raw_enabled=bool(state["leg_raw"]),
            dtln_enabled=bool(state["leg_dtln"]),
            chip_aec_enabled=bool(state["leg_chip_aec"]),
            profile_selection=str(state.get("profile") or ""),
        ),
        runtime,
        MicProbe(
            xvf_present=xvf_present,
            capture_channels=capture_channels,
            recommended_channels=recommended_channels,
            display_name=display_name,
            probe_error=probe_error,
        ),
        bridge_active=bridge_active,
        chip_available=chip_available,
    )


def _chip_aec_available() -> bool:
    """True when the XVF3800 exposes the chip-AEC beam firmware shape."""
    try:
        from ..mics import xvf3800
        return xvf3800.is_recommended_firmware()
    except Exception:  # noqa: BLE001
        return False


def _mic_status(
    state: dict[str, Any],
    *,
    bridge_active: bool,
    chip_available: bool,
) -> dict[str, Any]:
    """Compatibility wrapper for callers that only need mic status."""
    return _audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )["microphone"]


def _aec_full_status() -> dict:
    """JSON shape returned by GET /aec — the single source of truth
    for the /wake/ page's detection card. Includes both the configured
    state (from aec_mode.env) and the observed bridge service state.

    Per-leg observed state isn't returned separately today. A
    configured leg is implicitly "active" when (a) AEC mode is auto,
    (b) the bridge is active, and (c) the leg is configured on. DTLN
    load failures surface via jasper-doctor's check_aec_bridge_dtln_engine,
    which the /system Diagnostics disclosure runs on demand.

    The chip-AEC leg also carries an `available` flag: the XVF3800 chip
    beams only exist on the 6-channel firmware, so the /wake/ toggle stays
    disabled (with explanatory copy) when the chip isn't on that variant."""
    state = _read_aec_state()
    bridge_active = _aec_bridge_active()
    # The chip-AEC beams require the 6-channel XVF firmware.
    # is_recommended_firmware() reads /proc/asound and returns False when the
    # card is absent or on the 2-ch variant; wrap defensively so a probe
    # failure can never 500 a status GET the /wake/ page polls every 3 s.
    chip_available = _chip_aec_available()
    effective = resolve_audio_input_intent(
        AecIntent(
            mode=state["mode"],
            raw_enabled=bool(state["leg_raw"]),
            dtln_enabled=bool(state["leg_dtln"]),
            chip_aec_enabled=bool(state["leg_chip_aec"]),
            profile_selection=str(state.get("profile") or ""),
        ),
        chip_available=chip_available,
    )
    profile_status = _audio_profile_status(
        state,
        bridge_active=bridge_active,
        chip_available=chip_available,
    )
    requested_profile = profile_status["audio_profile"].get("requested")
    validation_filters = _audio_validation_filter_kwargs(
        requested_profile=requested_profile,
        system_env=_fresh_jasper_env(),
    )
    return {
        "mode": effective.mode,
        "profile": state["profile"],
        "raw_intent": {
            "mode": state["mode"],
            "leg_raw": state["leg_raw"],
            "leg_dtln": state["leg_dtln"],
            "leg_chip_aec": state["leg_chip_aec"],
        },
        "bridge_active": bridge_active,
        "legs": {
            "raw": {"configured": effective.raw_enabled},
            "dtln": {"configured": effective.dtln_enabled},
            "chip_aec": {
                "configured": effective.chip_aec_enabled,
                "available": chip_available,
            },
        },
        "threshold": _read_wake_threshold(),
        "wake_word": _read_wake_word_status(),
        "audio_profile": profile_status["audio_profile"],
        "microphone": profile_status["microphone"],
        "validation": _audio_validation_summary(**validation_filters),
    }


def _build_spotify_router_or_none():
    """Build a multi-account Spotify router for dial-driven volume.
    Returns None if SPOTIFY_CLIENT_ID isn't set or no accounts have
    been authorized — _set_spotify in the coordinator treats None as
    "skip Spotify dispatch", logging a no-op."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        return None
    try:
        from ..accounts import Registry, maybe_migrate_legacy
        from ..spotify_router import Router, build_clients
        registry = Registry.load(os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper/spotify/accounts.json",
        ))
        maybe_migrate_legacy(
            registry,
            os.environ.get(
                "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache",
            ),
            default_name="default",
        )
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        # build_clients returns BuildResult. The control daemon doesn't
        # surface revoked-vs-needs-oauth status to the user, so we use
        # the clients dict only — but still pass statuses through to the
        # Router so /state can introspect them if a future endpoint adds
        # a Spotify health probe.
        default_redirect_uri = (
            f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}"
        )
        result = build_clients(
            registry,
            client_id=client_id,
            redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI") or default_redirect_uri,
        )
        if not result.clients:
            return None
        return Router(
            clients=result.clients,
            default_name=registry.default_name,
            statuses=result.statuses,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("control daemon spotify router build failed: %s", e)
        return None


async def _with_coordinator(
    op: Callable[[Any], Any],
    *,
    camilla_host: str,
    camilla_port: int,
    duck_active_probe: Optional[Callable[[], Awaitable[Optional[bool]]]] = None,
) -> Any:
    """Build a VolumeCoordinator for one operation, run `op(coord)`,
    dispose. Mirrors `_dispatch_transport`'s per-request pattern — each
    HTTP request creates and tears down its own async resources, so we
    don't have to manage a long-lived asyncio loop in this stdlib HTTP
    server.

    `op` is an async callable taking the live coordinator and
    returning the per-request result (dict or scalar).

    `duck_active_probe` is forwarded into the coordinator. When set
    (callers that write camilla via the dial/web path), the
    coordinator defers its camilla write iff the probe returns True.
    See `_make_duck_active_probe` for the wire details and
    docs/HANDOFF-volume.md "Cross-daemon defer signal" for the why."""
    from ..camilla import CamillaController
    from ..renderer import RendererClient
    from ..speaker_name import runtime_name as _speaker_runtime_name
    from ..volume_coordinator import VolumeCoordinator
    from ..volume_persistence import VolumePersistence

    camilla = CamillaController(host=camilla_host, port=camilla_port)
    persistence = VolumePersistence(
        os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        ),
    )
    backend = RendererClient(
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
        ),
    )
    # Build a Spotify router per-request so dial volume can dispatch
    # to Spotify via Web API (librespot 0.8.0 has no local HTTP).
    # Best-effort: if env vars aren't set or no accounts authorized,
    # router is None and Spotify dispatch becomes a no-op.
    spotify_router = _build_spotify_router_or_none()
    coord = VolumeCoordinator(
        camilla=camilla,
        persistence=persistence,
        backend=backend,
        spotify_router=spotify_router,
        spotify_device_name=_speaker_runtime_name(),
        duck_active_probe=duck_active_probe,
    )
    coord.load_persisted_level()
    try:
        return await op(coord)
    finally:
        try:
            await coord.aclose()
        except Exception as e:  # noqa: BLE001
            logger.debug("coordinator aclose warning: %s", e)
        # RendererClient has no aclose — it's a stateless probe wrapper.
        # CamillaController has no aclose — sync websocket reconnects
        # on next use. GC handles cleanup of the cached client.


def _make_duck_active_probe(
    voice_socket_path: str,
) -> Callable[[], Awaitable[Optional[bool]]]:
    """Build the cross-daemon duck-active probe consumed by
    VolumeCoordinator._set_camilla in the per-request coordinators here.

    The probe asks jasper-voice over UDS whether the Ducker is
    currently holding camilla below the canonical listening_level
    target. True → defer the dial's camilla write (Ducker.restore
    will land it on session end). False → write camilla normally.
    None → unknown (UDS unreachable / voice wedged / response
    malformed); the coordinator treats this as fail-open and writes
    camilla — the dial must never silently stop working because of
    an inter-daemon problem.

    Tight 1 s timeout: STATUS is a synchronous attribute read in
    voice_daemon (no I/O). If it doesn't return in 1 s the daemon
    is wedged and we'd rather fail-open than block dial input. See
    docs/HANDOFF-volume.md "Cross-daemon defer signal"."""
    async def probe() -> Optional[bool]:
        try:
            response = await _voice_socket_command(
                voice_socket_path, "STATUS", timeout=1.0,
            )
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
        ):
            return None
        duck_active = response.get("duck_active")
        if isinstance(duck_active, bool):
            return duck_active
        # Older jasper-voice without the field, or unexpected type —
        # fail-open. Same effect as voice unreachable.
        return None
    return probe


async def _voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and return
    the parsed JSON response. Used by /session/start, /session/end,
    and /cue/play. The default 5s timeout covers session-state
    commands; cue playback takes longer (~6s for a 5s cue plus
    duck/restore plus drain) and bumps timeout explicitly."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("voice_daemon returned no response")
    return json.loads(line.decode("utf-8"))


async def _mux_socket_command(
    cmd: str,
    *,
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one ASCII command to jasper-mux's local control socket.

    The web frontend should not talk to fan-in directly: mux owns the
    manual-vs-auto source policy and uses fan-in only as the low-level
    audio gate.
    """
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("jasper-mux returned no response")
    payload = json.loads(line.decode("utf-8"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-mux returned non-object JSON")
    return payload


async def _local_status_json(
    socket_path: str,
    *,
    timeout: float = 2.0,
    max_bytes: int = 8192,
) -> dict | None:
    """Best-effort one-shot STATUS probe for local daemon UDS sockets."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=timeout,
        )
    except (FileNotFoundError, ConnectionRefusedError,
            asyncio.TimeoutError, OSError):
        return None
    try:
        writer.write(b"STATUS\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(max_bytes), timeout=timeout)
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        writer.close()
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, AssertionError):
            pass
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _outputd_status() -> dict | None:
    """Probe jasper-outputd's STATUS endpoint.

    Missing socket is fail-soft here so /state remains available while
    jasper-doctor owns the actionable cutover failure.
    """
    return await _local_status_json("/run/jasper-outputd/control.sock")


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add on/off wizard availability to mux source status.

    Mux knows audio policy; `/sources/` knows whether each renderer is
    enabled/available. The landing selector needs both, but keeping the
    merge here avoids teaching mux about systemd/DBus source toggles.
    """
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return payload
    global _source_availability_cache
    now = time.monotonic()
    with _source_availability_lock:
        cached = _source_availability_cache
        if cached is not None and now - cached[0] < SOURCE_AVAILABILITY_TTL_SEC:
            wizard_state = cached[1]
        else:
            try:
                from ..web.sources_setup import _gather_state as _sources_state
                wizard_state = _sources_state()
            except Exception as e:  # noqa: BLE001
                logger.debug("source availability read failed: %s", e)
                return payload
            _source_availability_cache = (now, wizard_state)
    for spec in MUSIC_SOURCE_SPECS:
        wizard_key = spec.wizard_key
        mux_key = spec.id.value
        state = wizard_state.get(wizard_key)
        if not isinstance(state, dict):
            continue
        slot = sources.setdefault(mux_key, {})
        if isinstance(slot, dict):
            slot["available"] = bool(state.get("available", True))
            slot["enabled"] = bool(state.get("enabled", False))
    return payload


async def _probe_dial_reachable(ip: str, *, timeout: float = 0.5) -> bool:
    """Fast TCP probe for dial liveness. The dial firmware doesn't run
    a server on any TCP port, so any connect attempt resolves to:

    - ConnectionRefusedError (RST from a live host): online
    - asyncio.TimeoutError / OSError: unreachable

    Port 80 is arbitrary — closed-port RST behaviour is identical on
    any port. Replaces the prior activity-based `online` check, which
    flagged an idle-but-healthy dial offline because the dial only
    emits UDP dlogs on encoder/button events. The probe takes
    ~3-10 ms on a dial running the WiFi-sleep-disabled firmware (see
    firmware/dial/src/main.cpp `WiFi.setSleep(false)`); the 500 ms
    cap is the worst-case envelope for a still-sleeping dial."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, 80),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except ConnectionRefusedError:
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _get_state(
    *,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
) -> dict[str, Any]:
    """Aggregate state across daemons for GET /state. Each section
    fails soft — voice unreachable / camilla restarting / dial never
    connected → that section reports null instead of erroring out
    the whole response. Slow probes fan out in parallel so the call
    completes in ~200 ms typical."""
    from datetime import datetime, timezone

    from .. import librespot_state
    from ..camilla import CamillaController
    from ..output_hardware import load_state as _load_output_hardware_state
    from ..speaker_name import read_state as _read_speaker_name_state
    from ..voice.provider_state import read_active_provider_state

    # Provider + model: re-read the wizard-owned SSOT file fresh on every
    # call. jasper-control is NOT restarted on a provider switch (only
    # jasper-voice is), so reading os.environ here pins the value to
    # whatever it was at this daemon's start and shows a stale provider
    # after every switch — the /system/ bug this fixes. Same fresh-read
    # rationale as the home_assistant block in /system/snapshot below.
    # ("", None) when unconfigured; never a guessed default.
    active_provider = read_active_provider_state()

    listening_level: int | None = None
    persisted_main_volume_db: float | None = None
    try:
        path = os.environ.get(
            "JASPER_VOLUME_STATE_PATH",
            "/var/lib/jasper/speaker_volume.json",
        )
        with open(path) as f:
            blob = json.load(f)
        raw_level = blob.get("listening_level")
        if isinstance(raw_level, (int, float)) and 0 <= raw_level <= 100:
            listening_level = int(raw_level)
        raw_db = blob.get("main_volume_db")
        if isinstance(raw_db, (int, float)) and math.isfinite(float(raw_db)):
            persisted_main_volume_db = round(float(raw_db), 2)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    sound_profile: dict[str, Any] | None
    try:
        from ..dsp_apply import last_dsp_apply_state
        from ..sound.profile import (
            build_sound_filters,
            estimate_headroom_db,
            load_profile,
        )
        from ..sound.settings import load_sound_settings, output_trim_db

        profile = load_profile()
        sound_settings = load_sound_settings()
        sound_profile = {
            "enabled": profile.enabled,
            "curve_id": profile.curve_id,
            "simple_eq": profile.simple_eq.to_dict(),
            "parametric_band_count": len(profile.parametric_bands),
            "filter_count": len(build_sound_filters(profile)),
            "headroom_db": estimate_headroom_db(profile),
            # Global output settings + the effective trim they apply, so the
            # dashboard can explain why a profile sounds quieter/level-matched.
            "match_loudness": sound_settings.match_loudness,
            "headroom_trim_db": sound_settings.headroom_trim_db,
            "output_trim_db": output_trim_db(profile, sound_settings),
            "updated_at": profile.updated_at or None,
            "last_dsp_apply": last_dsp_apply_state(),
        }
    except Exception:  # noqa: BLE001
        logger.exception("sound profile state probe failed")
        sound_profile = None

    # Slow probes — fan out in parallel.
    def _round_db(value: float | None) -> float | None:
        if value is None:
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, 2)

    def _round_pair(
        pair: tuple[float, float] | None,
    ) -> list[float | None] | None:
        if pair is None:
            return None
        return [_round_db(pair[0]), _round_db(pair[1])]

    async def _camilla_status() -> dict[str, Any]:
        status: dict[str, Any] = {
            "main_volume_db": None,
            "playback_rms_dbfs": None,
            "playback_peak_dbfs": None,
            "clipped_samples": None,
            "active_config_path": None,
        }

        async def _no_config_path() -> None:
            return None

        try:
            cam = CamillaController(host=camilla_host, port=camilla_port)
            config_path_probe = (
                cam.get_config_file_path(best_effort=True)
                if hasattr(cam, "get_config_file_path")
                else _no_config_path()
            )
            vol, rms, peak, clipped, active_config_path = await asyncio.gather(
                cam.get_volume_db(best_effort=True),
                cam.get_playback_rms(best_effort=True),
                cam.get_playback_peak(best_effort=True),
                cam.get_clipped_samples(best_effort=True),
                config_path_probe,
            )
            status["main_volume_db"] = _round_db(vol)
            status["playback_rms_dbfs"] = _round_pair(rms)
            status["playback_peak_dbfs"] = _round_pair(peak)
            status["clipped_samples"] = clipped
            status["active_config_path"] = active_config_path
            return status
        except Exception:  # noqa: BLE001
            return status

    async def _airplay_playing() -> bool | None:
        # Shared probe owns the subprocess hygiene (kill-on-timeout so a
        # DBus stall can't leak one busctl per /state poll; spawn OSError
        # → None instead of 500ing the whole fail-soft aggregate).
        return await mpris.shairport_playing(timeout=2.0)

    async def _voice_status() -> dict | None:
        try:
            return await _voice_socket_command(
                voice_socket_path, "STATUS", timeout=2.0,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError, RuntimeError):
            return None

    async def _ha_status() -> dict:
        """Probe the configured HA instance for /state. Fails soft —
        unconfigured returns {configured: false}; unreachable returns
        {connected: false, error: ...}. Reads /var/lib/jasper/home_assistant.env
        directly (not os.environ) so wizard saves are reflected
        immediately rather than waiting for jasper-control to restart —
        the wizard only restarts jasper-voice. See
        `jasper.home_assistant.probe_status_from_env`."""
        from .. import home_assistant
        return await home_assistant.probe_status_from_env()

    # Snapshot dial heartbeat early so the parallel reachability probe
    # has a stable IP target even if the UDP listener mutates the dict
    # mid-call. last_seen_ip is None until the dial has dlogged at
    # least once — without an IP we can't probe, so online stays false.
    dial_snapshot = dict(_dial_heartbeat)
    dial_ip = dial_snapshot.get("last_seen_ip")

    async def _dial_online() -> bool:
        if not dial_ip:
            return False
        return await _probe_dial_reachable(dial_ip)

    async def _fanin_status() -> dict | None:
        """Probe the jasper-fanin daemon's UDS STATUS endpoint.

        Returns None when:
          - the daemon isn't running yet or is unhealthy
          - the socket doesn't exist (daemon not yet bound)
          - the probe times out (work loop wedged, ALSA blocked)
          - the response isn't valid JSON

        Fan-in is mandatory for renderer audio, but /state is fail-soft
        like _voice_status. jasper-doctor owns the actionable failure.
        See docs/HANDOFF-fan-in-daemon.md for the daemon design.
        """
        return await _local_status_json("/run/jasper-fanin/control.sock")

    async def _mux_status() -> dict | None:
        try:
            return await _mux_socket_command("STATUS", timeout=1.0)
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    async def _aec_status() -> dict | None:
        """Additive mirror of GET /aec for one-shot /state consumers."""
        try:
            return await asyncio.to_thread(_aec_full_status)
        except Exception:  # noqa: BLE001
            logger.exception("AEC/profile state probe failed")
            return None

    (
        camilla_st,
        airplay,
        voice_st,
        ha_status,
        dial_online,
        fanin_st,
        outputd_st,
        mux_st,
        aec_status,
    ) = await asyncio.gather(
        _camilla_status(),
        _airplay_playing(),
        _voice_status(),
        _ha_status(),
        _dial_online(),
        _fanin_status(),
        _outputd_status(),
        _mux_status(),
        _aec_status(),
    )

    spotify_blob = librespot_state.read(
        os.environ.get("JASPER_LIBRESPOT_STATE", librespot_state.DEFAULT_PATH),
    )
    if sound_profile is not None:
        runtime = _sound_runtime_status(
            sound_profile,
            camilla_st.get("active_config_path"),
        )
        sound_profile["runtime"] = runtime
        # Keep these top-level aliases for lightweight consumers that
        # only need the running truth and do not want to parse the nested
        # runtime object.
        sound_profile["runtime_state"] = runtime["state"]
        sound_profile["runtime_active"] = runtime["active"]
        sound_profile["active_config_path"] = runtime["active_config_path"]
    speaker_name_state = _read_speaker_name_state()
    spotify = {
        "playing": bool(spotify_blob.get("playing", False)),
        "track_id": spotify_blob.get("track_id"),
        "uri": spotify_blob.get("uri"),
        "session_active": bool(spotify_blob.get("session_active", False)),
    }

    # USB sink — fourth renderer. Reads the state file the daemon
    # publishes. Section reports None when the feature is disabled
    # (no state file) so consumers can distinguish "off" from
    # "on but idle".
    usbsink_state: dict | None = None
    try:
        with open(
            os.environ.get(
                "JASPER_USBSINK_STATE_PATH",
                "/run/jasper-usbsink/state.json",
            ),
        ) as f:
            usbsink_blob = json.load(f)
        usbsink_state = {
            "playing": bool(usbsink_blob.get("playing", False)),
            "preempted": bool(usbsink_blob.get("preempted", False)),
            "host_connected": bool(
                usbsink_blob.get("host_connected", False),
            ),
            "rms_dbfs": usbsink_blob.get("rms_dbfs"),
            "updated_at": usbsink_blob.get("updated_at"),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    voice_session = bool(voice_st) and voice_st.get("state") == "SESSION"
    # Active-source picks. Mux owns the effective audible source in
    # both manual and auto mode. Fall back to raw renderer probes only
    # when mux is unavailable or has no selected winner yet.
    mux_effective_source = None
    if isinstance(mux_st, dict):
        raw_selected = mux_st.get("selected_source")
        if isinstance(raw_selected, str):
            mux_effective_source = raw_selected
        else:
            raw_winner = mux_st.get("winner")
            if isinstance(raw_winner, str):
                mux_effective_source = raw_winner

    if voice_session:
        active_source: str = "voice"
    elif mux_effective_source:
        active_source = mux_effective_source
    elif spotify["playing"]:
        active_source = "spotify"
    elif airplay:
        active_source = "airplay"
    elif usbsink_state is not None and usbsink_state.get("playing"):
        active_source = "usbsink"
    else:
        active_source = "idle"

    volume_policy = build_volume_policy_snapshot(
        active_source=active_source,
        listening_level=listening_level,
        main_volume_db=camilla_st["main_volume_db"],
        persisted_main_volume_db=persisted_main_volume_db,
        mux_status=mux_st,
        diagnostics=_read_volume_diagnostics(),
    )

    # Build the dial section from the snapshot taken before the gather
    # so age_seconds is consistent with whatever IP the probe targeted.
    # `online` reflects real TCP reachability (see _probe_dial_reachable),
    # not UDP-dlog freshness — an idle dial is now correctly online
    # rather than mislabelled offline after 30 s of no encoder activity.
    dial = dial_snapshot
    if dial.get("last_seen_at") is not None:
        dial["age_seconds"] = round(time.time() - dial["last_seen_at"], 1)
    else:
        dial["age_seconds"] = None
    dial["online"] = dial_online

    # Multiroom grouping. Re-reads /var/lib/jasper/grouping.env fresh
    # (never os.environ — jasper-control isn't restarted on a wizard
    # save). read_grouping_state is itself total, but guard the section
    # so any future read change can't take the whole /state down: a
    # broken read leaves grouping null and the rest of /state intact.
    # enabled=False means grouping is off (solo); enabled=True with a
    # non-null error is the fail-LOUD "configured but broken" state.
    try:
        grouping_state: dict | None = read_grouping_state()
    except Exception:  # noqa: BLE001
        logger.exception("grouping state read failed")
        grouping_state = None

    # Transit city packs. Re-reads /var/lib/jasper/transit.env fresh (never
    # os.environ — jasper-control isn't restarted on a /transit/ save, only
    # jasper-voice is). read_transit_state is itself total, but guard the
    # section so a future read change can't take the whole /state down: a
    # broken read leaves transit null and the rest of /state intact.
    try:
        transit_state: dict | None = read_transit_state()
    except Exception:  # noqa: BLE001
        logger.exception("transit state read failed")
        transit_state = None
    try:
        output_hardware = _load_output_hardware_state()
        output_hardware_state = (
            output_hardware.to_dict()
            if output_hardware is not None
            else None
        )
    except Exception:  # noqa: BLE001
        logger.exception("output hardware state read failed")
        output_hardware_state = None

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "voice": {
            "provider": active_provider.provider,
            "model": active_provider.model,
            "provider_status": active_provider.status,
            "provider_error": active_provider.detail or None,
            "session_active": voice_session,
            "spend_allowed": (voice_st or {}).get("spend_allowed"),
            "connection_paused": (voice_st or {}).get("connection_paused"),
            "mic_muted": (voice_st or {}).get("mic_muted"),
            "music_dbfs": (voice_st or {}).get("music_dbfs"),
            # Runtime-armed wake-leg tokens from jasper-voice's
            # session_status. jasper-doctor's check_wake_legs cross-checks
            # this against the configured intent in aec_mode.env to surface
            # a startup leg-skip; the /state aggregator curates voice
            # fields explicitly, so a new session_status field must be
            # pulled through here too.
            "wake_legs": (voice_st or {}).get("wake_legs"),
            "reachable": voice_st is not None,
        },
        "audio": {
            "main_volume_db": camilla_st["main_volume_db"],
            "listening_level_percent": listening_level,
            "volume_policy": volume_policy,
            "playback_rms_dbfs": camilla_st["playback_rms_dbfs"],
            "playback_peak_dbfs": camilla_st["playback_peak_dbfs"],
            "clipped_samples": camilla_st["clipped_samples"],
            "camilla_active_config_path": camilla_st["active_config_path"],
            "sound": sound_profile,
            "output_hardware": output_hardware_state,
        },
        "renderers": {
            "spotify": spotify,
            "airplay": (
                None if airplay is None else {"playing": airplay}
            ),
            # null when the feature is disabled (no state file). The
            # /system dashboard and any other consumer can show
            # "off" vs "idle" based on this.
            "usbsink": usbsink_state,
        },
        "speaker_name": {
            "name": speaker_name_state.name,
            "source": speaker_name_state.source,
        },
        "active_source": active_source,
        # Fan-in daemon. null only when the daemon/socket is unavailable.
        # When running, the UDS STATUS endpoint emits a JSON snapshot
        # with per-input frame counts, output xrun counts, and watchdog
        # metrics — surfaced verbatim here. See
        # docs/HANDOFF-fan-in-daemon.md.
        "fanin": fanin_st,
        # Final-output owner on current main. null when the daemon/socket
        # is unavailable; jasper-doctor owns the actionable failure.
        "outputd": outputd_st,
        # Additive mirror of GET /aec so one-shot /state consumers can see
        # requested intent vs observed mic/profile runtime truth without a
        # second control-plane request. null only when the probe itself fails.
        "aec": aec_status,
        "source_selection": mux_st,
        "satellites": {
            "dial": dial,
        },
        "resilience": {
            "shairport": shairport_supervisor.snapshot(),
            # Bonded-member runtime liveness: dac_content starvation
            # watch (kicks the grouping reconciler, rate-limited) +
            # continuous snapcast binding read-repair on the leader.
            # Off via JASPER_GROUPING_SUPERVISOR=disabled.
            "grouping_supervisor": grouping_supervisor.snapshot(),
            # T5.2 — userspace-liveness supervisor. Probes sshd / our
            # own HTTP / /proc/loadavg every 30 s; clean-reboots after
            # 3 consecutive failures (rate-limited 1/24h). Off via
            # JASPER_SYSTEM_SUPERVISOR=disabled.
            "system_supervisor": system_supervisor.snapshot(),
            # WiFi profile guardian: self-heal of the NM keyfile after
            # dirty shutdown. Synthesised from the on-disk stash + the
            # most recent `event=wifi_guardian.*` journal line — there's
            # no resident daemon to ask (the guardian is Type=oneshot).
            # Fail-soft inside the snapshot itself; never raises.
            "wifi_guardian": wifi_guardian_state.snapshot(),
            # Boot-loop guard (cross-boot circuit breaker for the T5.1
            # StartLimitAction=reboot ladder). Fresh marker read per
            # call; {"ran": false} when the oneshot hasn't run this
            # boot. tripped=true means reboot escalation is disarmed
            # for this boot via runtime drop-ins — fix the failing
            # daemon, then reboot to re-arm.
            "bootloop_guard": bootloop_guard_state.snapshot(),
            # Effective mDNS identity (jasper-identity-reconcile, boot
            # + 5-min timer). status=collision means Avahi renamed us —
            # another device owns our hostname; the management
            # allowlist self-heals from the same file, but the
            # household should pick a unique name. Fresh file read per
            # call (reconciler-owned, this daemon is never restarted on
            # identity changes); {"status": "absent"} pre-first-run.
            "identity": identity_state.snapshot(),
        },
        "home_assistant": ha_status,
        # Multiroom grouping (off by default). null only if the fresh
        # read itself errored; otherwise a JSON-able snapshot of the
        # wizard-owned grouping.env (enabled / role / channel / bond_id /
        # leader_addr / buffer_ms / codec / error). See
        # jasper/multiroom/state.py + docs/HANDOFF-multiroom.md.
        "grouping": grouping_state,
        # Transit city packs (which cities' transit is enabled). null only
        # if the fresh read itself errored; otherwise {packs: [{id, label,
        # enabled}]} read fresh from the wizard-owned transit.env. Mirrors
        # the daemon's enabled_pack_ids on both absent (all) and
        # present-empty (none). See jasper/transit/state.py.
        "transit": transit_state,
        # Runtime debug-logging toggle (the /system Debug card): which
        # subsystems are at DEBUG + the shared auto-expiry countdown.
        "debug": debug_control.snapshot(),
    }


async def _dispatch_transport(action: str) -> dict:
    """Build renderer + Spotify-router clients in the current event
    loop, dispatch a transport action, then close. We rebuild per
    request because httpx's AsyncClient is loop-bound: a persistent
    instance would be tied to the first request's loop and error on
    every subsequent one. The cost is small (~50 ms) and dial/remote
    presses are rare.

    `action` must be one of "toggle", "next", "previous" — the
    dispatcher's documented vocabulary."""
    # Import inside the function so jasper-control doesn't import the
    # full voice-daemon dependency tree at startup.
    from ..accounts import Registry, maybe_migrate_legacy
    from ..renderer import RendererClient
    from ..spotify_router import Router, build_clients
    from ..tools.transport import make_transport_dispatcher

    renderer = RendererClient(
        librespot_state_path=os.environ.get(
            "JASPER_LIBRESPOT_STATE", "/run/librespot/state.json",
        ),
    )
    router: Router | None = None
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if client_id:
        accounts_path = os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper/spotify/accounts.json",
        )
        legacy_cache = os.environ.get(
            "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache",
        )
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        default_redirect_uri = (
            f"https://jaspercurry.github.io/spotify-oauth-callback/?host={hostname}"
        )
        redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI") or default_redirect_uri
        accounts = Registry.load(accounts_path)
        maybe_migrate_legacy(accounts, legacy_cache, default_name="default")
        # build_clients returns BuildResult (clients + statuses). Dial
        # press is a one-shot operation that doesn't need lazy rebuild,
        # so we don't wire a rebuild_fn here.
        result = build_clients(
            accounts,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
        if result.clients:
            router = Router(
                clients=result.clients,
                default_name=accounts.default_name,
                statuses=result.statuses,
            )

    dispatch = make_transport_dispatcher(renderer, router)
    return await dispatch(action)


def _make_handler(
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str,
    sampler: Any = None,
    airplay_health_sampler: Any = None,
) -> type[BaseHTTPRequestHandler]:

    # One probe instance per handler — it's stateless (just closes
    # over voice_socket_path), so all volume ops share it. Read-only
    # `_get_op` doesn't need it (`get_listening_level` doesn't touch
    # camilla), but passing None there keeps the construction uniform.
    duck_active_probe = _make_duck_active_probe(voice_socket_path)

    async def _set_op(percent: int):
        async def _op(coord):
            return await coord.set_listening_level(percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _observe_op(source_name: str, percent: int) -> int:
        """Route a source-observed volume change (e.g. host slider on
        the USB gadget) through the coordinator's echo-prevented
        observe path. Unknown source names fall back to the
        authoritative set path so a future client that posts a fresh
        source name doesn't silently no-op.

        Returns the level the coordinator ended up at — equal to
        `percent` on normal observe (no echo) or the prior value if
        the observation was treated as an echo of our own write."""
        # Lazy import to avoid pulling the full volume_coordinator
        # graph into the import path of server.py's module load.
        from ..volume_coordinator import Source
        try:
            source_enum = Source(source_name)
        except ValueError:
            # Unknown source — treat as authoritative.
            return await _set_op(percent)

        async def _op(coord):
            await coord.observe_source_volume(source_enum, percent)
            # The coordinator's level either took our value or stayed
            # put (echo-suppressed). Return whatever's now canonical
            # for the client to render.
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _adjust_op(delta_percent: int):
        async def _op(coord):
            return await coord.adjust_listening_level(delta_percent)
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _get_op():
        async def _op(coord):
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
        )

    async def _mute_set_op(want_muted: bool):
        async def _op(coord):
            # Explicit set, idempotent: mute-when-muted stays muted,
            # unmute-when-unmuted returns the current level untouched.
            # Voice has distinct mute/unmute INTENTS, so it needs this
            # rather than the toggle (a toggle would invert a stale
            # intent — "mute" while already muted must not unmute).
            if want_muted:
                if not coord.is_muted():
                    await coord.mute()
                return 0
            if coord.is_muted():
                return await coord.unmute()
            return coord.get_listening_level()
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    async def _mute_toggle_op():
        async def _op(coord):
            # If currently muted, unmute and return restored level.
            # Otherwise mute and return 0 (the new actual level).
            if coord.is_muted():
                return await coord.unmute()
            await coord.mute()
            return 0
        return await _with_coordinator(
            _op,
            camilla_host=camilla_host, camilla_port=camilla_port,
            duck_active_probe=duck_active_probe,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > CONTROL_MAX_POST_BYTES:
                raise ValueError("invalid body length")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}

        def _guard_management_read(self) -> bool:
            if self.path == "/healthz":
                ok, reason = management_read_allowed({
                    "Host": self.headers.get("Host") or "",
                })
            else:
                ok, reason = management_read_allowed(self.headers)
            if ok:
                return True
            logger.warning(
                "event=http.reject reason=%s host=%r sec_fetch_site=%r path=%s client=%s",
                reason, self.headers.get("Host"),
                self.headers.get("Sec-Fetch-Site"), self.path,
                self.address_string(),
            )
            self._send_json({"error": reason}, status=403)
            return False

        def _guard_mutating_request(self) -> bool:
            ok, reason = mutating_request_allowed(self.headers)
            if not ok:
                logger.warning(
                    "event=http.reject reason=%s host=%r origin=%r path=%s client=%s",
                    reason, self.headers.get("Host"), self.headers.get("Origin"),
                    self.path, self.address_string(),
                )
                self._send_json({"error": reason}, status=403)
                return False
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._send_json({"error": "invalid_content_length"}, status=400)
                return False
            if length < 0:
                self._send_json({"error": "invalid_content_length"}, status=400)
                return False
            if length > CONTROL_MAX_POST_BYTES:
                logger.warning(
                    "event=http.reject reason=body_too_large bytes=%d limit=%d path=%s client=%s",
                    length, CONTROL_MAX_POST_BYTES, self.path, self.address_string(),
                )
                self._send_json(
                    {
                        "error": "request_body_too_large",
                        "max_bytes": CONTROL_MAX_POST_BYTES,
                    },
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return False
            return True

        def _volume_payload(self, percent: int) -> dict[str, Any]:
            # `db` is computed for back-compat with the dial firmware
            # which reads `percent` but logs `db`. The legacy 50 dB
            # scale is still the lingua franca for clients that haven't
            # been updated.
            return {"db": round(_percent_to_db(percent), 3), "percent": percent}

        def _maybe_forward_volume_to_leader(self) -> bool:
            """Bonded-follower volume proxy. Returns True when the request
            was handled (forwarded or rejected) and the caller must stop.

            While this speaker is an ACTIVE bonded follower, its local
            volume knobs are INERT — bonded content bypasses the local
            CamillaDSP entirely (the leader's one Camilla bakes the
            program; HANDOFF-multiroom.md §2). Without this, the landing
            page slider, a paired dial, and curl all "work" silently
            with no audible effect — the worst UX shape. So the four
            /volume endpoints forward verbatim to the leader's control
            API and relay its answer: every member's volume surface
            controls the PAIR volume, whichever speaker's page you have
            open. Solo and leader requests never enter this path; the
            grouping read is one tiny env-file parse (load_config), NOT
            the heavy runtime derive — this sits on every volume call.
            """
            leader = _pair_follower_leader_addr()
            if leader is None:
                return False
            # Loop breaker: a forwarded request never re-forwards. Two
            # speakers misconfigured as each other's follower would
            # otherwise ping-pong until a timeout stack built up.
            if self.headers.get(_PAIR_FORWARD_HEADER):
                # Drain any request body before responding so the
                # connection state stays sane if keep-alive is ever
                # enabled (HTTP/1.0 today, so this is pure hygiene).
                try:
                    stale = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    stale = 0
                if self.command == "POST" and stale > 0:
                    self.rfile.read(stale)
                self._send_json(
                    {"error": "pair forward loop (both speakers are "
                              "followers?)", "pair_leader": leader},
                    status=502,
                )
                return True
            body: bytes | None = None
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length > 0 else b"{}"
            url = "http://{}:{}{}".format(
                leader, self.server.server_address[1], self.path,
            )
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    _PAIR_FORWARD_HEADER: "1",
                },
                method=self.command,
            )
            try:
                with _pair_urlopen(req, timeout=2.5) as resp:
                    payload = json.loads(resp.read().decode())
            except Exception as e:  # noqa: BLE001 — relay any failure as 502
                logger.warning(
                    "event=volume.pair_forward_failed leader=%s path=%s "
                    "error=%s", leader, self.path, e,
                )
                self._send_json(
                    {"error": f"pair leader unreachable: {e}",
                     "pair_leader": leader},
                    status=502,
                )
                return True
            if isinstance(payload, dict):
                # Additive marker so UIs can label the slider "pair
                # volume"; dial firmware reads only db/percent.
                payload.setdefault("pair_leader", leader)
            self._send_json(payload)
            return True

        # --- routes ---
        #
        # do_GET / do_POST own the dispatch via the _GET_ROUTES /
        # _POST_ROUTES tables (path -> handler-method name) defined at the
        # bottom of this class. Each table entry's handler holds the exact
        # body the inlined `if self.path == ...` branch had — moved into a
        # named method, logic unchanged.
        #
        # SECURITY ORDERING IS LOAD-BEARING: the management-read /
        # mutating-request guard runs FIRST (before the table lookup), and
        # the unknown-path 404 happens LAST (table miss -> send_error). So
        # an unknown path under a hostile Host/Origin is still rejected by
        # the guard (403/400/413) BEFORE it can 404 — the inverse of the
        # web-wizard "route-check before guard" convention, preserved here
        # on purpose. Do not reorder lookup ahead of the guard.

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard_management_read():
                return
            handler_name = self._GET_ROUTES.get(self.path)
            if handler_name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            getattr(self, handler_name)()

        def _get_healthz(self) -> None:
            self._send_json({"ok": True})

        def _get_volume(self) -> None:
            if self._maybe_forward_volume_to_leader():
                return
            try:
                percent = asyncio.run(_get_op())
            except Exception as e:  # noqa: BLE001
                logger.exception("get volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(self._volume_payload(percent))

        def _get_mic(self) -> None:
            # Read mic mute state from the voice daemon's STATUS
            # response. If the daemon isn't reachable, surface that
            # explicitly so the UI can grey out the toggle instead
            # of pretending we know the state.
            try:
                st = asyncio.run(_voice_socket_command(
                    voice_socket_path, "STATUS", timeout=2.0,
                ))
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                self._send_json(
                    {"error": f"voice_daemon unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("mic STATUS failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json({"muted": bool(st.get("mic_muted", False))})

        def _get_source_state(self) -> None:
            # Source selection state from jasper-mux. This is
            # separate from the /sources/ wizard (on/off toggles):
            # selecting a source does not enable or disable any
            # renderer, it only chooses which active lane the
            # speaker should pass through.
            try:
                result = asyncio.run(_mux_socket_command("STATUS"))
            except (
                FileNotFoundError,
                ConnectionRefusedError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                self._send_json(
                    {"error": f"jasper-mux unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("source STATUS failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(_augment_source_payload(result))

        def _get_aec(self) -> None:
            # Software AEC bridge state + per-leg config + wake
            # threshold. Mode and leg booleans are the persisted
            # request (what the operator asked for, via the /wake/
            # page or aec_mode.env directly); bridge_active is the
            # observed truth from systemd. They diverge briefly
            # during a reconciler-driven transition (~10-15 s).
            # Threshold is read from wake_model.env — the same
            # file the /wake/ form save writes the model into, so
            # both controls stay in sync without sharing code.
            #
            # DTLN load failures don't surface in this payload —
            # /system's Diagnostics disclosure runs jasper-doctor
            # which has check_aec_bridge_dtln_engine for the
            # silent-failure case.
            self._send_json(_aec_full_status())

        def _get_debug(self) -> None:
            # Runtime debug-logging state for the /system Debug card:
            # per-subsystem on/off + the shared auto-expiry countdown.
            self._send_json(debug_control.snapshot())

        def _get_state(self) -> None:
            # Cross-daemon snapshot — voice / audio / renderers /
            # satellites. Polled by the /voice web UI for live
            # status, used by jasper-doctor for one-shot health,
            # and consumable from `curl jts.local:8780/state | jq`
            # for ad-hoc debugging. ~200 ms typical (mostly the
            # parallel busctl + camilla WS probes).
            try:
                state = asyncio.run(_get_state(
                    camilla_host=camilla_host,
                    camilla_port=camilla_port,
                    voice_socket_path=voice_socket_path,
                ))
            except Exception as e:  # noqa: BLE001
                logger.exception("/state aggregation failed")
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json(state)

        def _get_grouping(self) -> None:
            # Multiroom grouping block, nested under "grouping" so a
            # fail-soft read returns {"grouping": null} unambiguously.
            # Read SERVER-SIDE by another speaker's /rooms /unbond
            # fan-out (rooms_setup._get_member_grouping) to discover which
            # siblings share a bond_id before dissolving it — NOT by the
            # browser (the page reads self.grouping from /rooms.json).
            # NO CSRF: a read on the same no-auth LAN surface as /state
            # and /healthz. Fail-soft like /state's grouping section —
            # a broken read returns 200 with null rather than 500.
            try:
                grouping = read_grouping_state()
            except Exception:  # noqa: BLE001
                logger.exception("grouping state read failed")
                grouping = None
            # grouping_response is the ONE home for the envelope shape; the
            # /rooms /unbond consumer parses it via the paired
            # parse_grouping_response (jasper/multiroom/state.py), so the
            # two daemons can't drift (the C4 regression).
            self._send_json(grouping_response(grouping))

        def _get_dial_status(self) -> None:
            # Heartbeat snapshot — used by jasper-doctor's
            # "is the dial actually talking to us?" check.
            snap = dict(_dial_heartbeat)
            if snap["last_seen_at"] is not None:
                snap["age_seconds"] = round(
                    time.time() - snap["last_seen_at"], 1,
                )
            else:
                snap["age_seconds"] = None
            self._send_json(snap)

        def _get_system_snapshot(self) -> None:
            # Snapshot for the /system dashboard. Current values +
            # 60-min ring buffers for the sparklines + build info +
            # home_assistant connection status.
            # Sampler may be None in tests / direct CLI invocation;
            # surface an empty history rather than 500.
            from .system_metrics import read_build_info
            from .. import home_assistant as _ha_mod
            from ..speaker_name import read_state as _read_speaker_name_state
            from ..voice.provider_state import read_active_provider

            # HA probe is async + slow-ish (~50-200 ms typical against
            # a healthy local HA, fails fast on unreachable). Run it
            # via asyncio.run so the rest of /system/snapshot stays
            # synchronous like the existing handler.
            try:
                # Same env-file-direct read as /state.home_assistant
                # above — wizard saves must reflect immediately in the
                # dashboard without restarting jasper-control.
                ha_status = asyncio.run(_ha_mod.probe_status_from_env())
            except Exception:  # noqa: BLE001
                # Fail-soft per the existing aggregator convention —
                # never break /system/snapshot because HA is wedged.
                ha_status = {
                    "configured": False, "connected": False, "url": "",
                    "instance_name": None, "version": None,
                    "error": "probe failed",
                }

            try:
                airplay_health = (
                    airplay_health_sampler.snapshot()
                    if airplay_health_sampler is not None else None
                )
            except Exception:  # noqa: BLE001
                logger.exception("airplay health snapshot failed")
                airplay_health = {
                    "status": "unknown",
                    "reason": "AirPlay health sampler failed",
                }

            try:
                outputd_status = asyncio.run(_outputd_status())
            except Exception:  # noqa: BLE001
                logger.exception("outputd status snapshot failed")
                outputd_status = None

            payload: dict[str, Any] = {
                "build": read_build_info(),
                "metrics": (
                    sampler.snapshot() if sampler is not None else None
                ),
                "airplay_health": airplay_health,
                "outputd": outputd_status,
                "audio_quality": _safe_audio_quality_state(),
                "voice_provider": read_active_provider(),
                "speaker_name": _read_speaker_name_state().__dict__,
                "home_assistant": ha_status,
            }
            self._send_json(payload)

        def _get_system_diagnostics(self) -> None:
            # Run jasper-doctor --json and proxy its output. ~3-5 s
            # on a Pi 5; the dashboard surfaces a spinner during
            # the call. Single-flight semantics not enforced here
            # (the dashboard disables the button while in flight).
            try:
                proc = subprocess.run(
                    ["/opt/jasper/.venv/bin/jasper-doctor", "--json"],
                    capture_output=True, text=True, timeout=30,
                    env=subprocess_env_with_fresh_files(),
                )
            except (subprocess.SubprocessError, FileNotFoundError) as e:
                self._send_json(
                    {"error": f"jasper-doctor invocation failed: {e}"},
                    status=502,
                )
                return
            # jasper-doctor exits 1 when any check failed; that's
            # a normal "report has failures" outcome, not an HTTP
            # error. Parse stdout regardless.
            try:
                body = json.loads(proc.stdout)
            except json.JSONDecodeError:
                self._send_json(
                    {"error": "doctor output not JSON",
                     "stdout": proc.stdout[:500],
                     "stderr": proc.stderr[:500]},
                    status=502,
                )
                return
            self._send_json(body)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard_mutating_request():
                return
            handler_name = self._POST_ROUTES.get(self.path)
            if handler_name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            getattr(self, handler_name)()

        def _post_volume_adjust(self) -> None:
            if self._maybe_forward_volume_to_leader():
                return
            body = self._read_json()
            # Support both legacy delta_db (dial firmware compat,
            # interpreted on the 50 dB camilla scale) and the
            # cleaner delta_percent for newer clients.
            if "delta_percent" in body:
                try:
                    delta_pct = int(body["delta_percent"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "delta_percent must be an integer"},
                        status=400,
                    )
                    return
            elif "delta_db" in body:
                try:
                    delta_pct = _delta_db_to_delta_percent(
                        float(body["delta_db"]),
                    )
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "delta_db must be a number"},
                        status=400,
                    )
                    return
            else:
                self._send_json(
                    {"error": "missing delta_db or delta_percent"},
                    status=400,
                )
                return
            try:
                new_pct = asyncio.run(_adjust_op(delta_pct))
            except Exception as e:  # noqa: BLE001
                logger.exception("adjust volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=volume.adjust delta_pct=%d new_pct=%d client=%s",
                delta_pct, new_pct, self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))

        def _post_volume_set(self) -> None:
            if self._maybe_forward_volume_to_leader():
                return
            body = self._read_json()
            # Support both legacy `db` (dial / older clients) and
            # the cleaner `percent`. Percent is the canonical unit
            # for listening_level.
            if "percent" in body:
                try:
                    target_pct = int(body["percent"])
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "percent must be an integer"}, status=400,
                    )
                    return
            elif "db" in body:
                try:
                    target_pct = _db_to_percent(float(body["db"]))
                except (TypeError, ValueError):
                    self._send_json(
                        {"error": "db must be a number"}, status=400,
                    )
                    return
            else:
                self._send_json(
                    {"error": "missing db or percent"}, status=400,
                )
                return
            # Optional `source` field marks the caller as an
            # observed source-side change (e.g. host moved its
            # volume slider on the USB gadget). Route through
            # observe_source_volume so the coordinator's echo
            # window and source-active gate apply. Without
            # `source`, the caller is treated as authoritative
            # (dial twist, voice "louder", etc.).
            source_name = body.get("source")
            try:
                if source_name:
                    new_pct = asyncio.run(
                        _observe_op(str(source_name), target_pct),
                    )
                else:
                    new_pct = asyncio.run(_set_op(target_pct))
            except Exception as e:  # noqa: BLE001
                logger.exception("set volume failed")
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=volume.set new_pct=%d source=%s client=%s",
                new_pct, source_name or "authoritative",
                self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))

        def _post_grouping_set(self) -> None:
            # Set this speaker's grouping role. Same no-auth LAN surface
            # as /volume (the dial) — so the bond-forming UI on speaker A
            # configures speaker B by POSTing here on B's port. The
            # reconciler (kicked below) is the single applier of the
            # snapcast units + the outputd tap.
            body = self._read_json()
            enabled = bool(body.get("enabled"))
            role = str(body.get("role", "")).strip()
            channel = str(body.get("channel", "")).strip()
            bond_id = str(body.get("bond_id", "")).strip()
            leader_addr = str(body.get("leader_addr", "")).strip()
            # Validate an ENABLED request up front via the SHARED
            # validate_grouping (same rule the config loader applies on
            # read) so we never persist a fail-loud config. A disabled
            # request needs no fields.
            if enabled:
                err = validate_grouping(
                    role=role, channel=channel,
                    bond_id=bond_id, leader_addr=leader_addr,
                )
                if err:
                    self._send_json({"error": err}, status=400)
                    return
            try:
                _write_grouping(
                    enabled=enabled, role=role, channel=channel,
                    bond_id=bond_id, leader_addr=leader_addr,
                )
                _kick_grouping_reconciler()
            except Exception as e:  # noqa: BLE001
                logger.exception("grouping set failed")
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=grouping.set enabled=%s role=%s channel=%s "
                "bond=%s client=%s",
                enabled, role or "(none)", channel or "(none)",
                bond_id or "(none)", self.address_string(),
            )
            self._send_json({
                "ok": True, "enabled": enabled, "role": role,
                "channel": channel, "bond_id": bond_id,
                "leader_addr": leader_addr,
            })
            return

        def _post_volume_mute(self) -> None:
            if self._maybe_forward_volume_to_leader():
                return
            # Default is TOGGLE: muted → unmute (restore pre-mute
            # level), unmuted → mute. Used by HID accessory clicks
            # (jasper-input) and other one-shot toggle callers. An
            # optional explicit {"muted": true|false} body sets the
            # state idempotently — the shape voice's distinct
            # mute/unmute intents need (additive; absent = toggle).
            body = self._read_json()
            explicit = body.get("muted")
            if explicit is not None and not isinstance(explicit, bool):
                self._send_json(
                    {"error": "muted must be a boolean"}, status=400,
                )
                return
            try:
                if explicit is None:
                    new_pct = asyncio.run(_mute_toggle_op())
                else:
                    new_pct = asyncio.run(_mute_set_op(explicit))
            except Exception as e:  # noqa: BLE001
                logger.exception("mute failed")
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=volume.mute new_pct=%d explicit=%s client=%s",
                new_pct, explicit, self.address_string(),
            )
            self._send_json(self._volume_payload(new_pct))
            return

        def _post_transport(self) -> None:
            action = self.path.rsplit("/", 1)[1]  # toggle | next | previous
            try:
                result = asyncio.run(_dispatch_transport(action))
            except Exception as e:  # noqa: BLE001
                logger.exception("transport %s failed", action)
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=transport.dispatch action=%s client=%s",
                action, self.address_string(),
            )
            if "error" in result:
                self._send_json(result, status=502)
                return
            self._send_json(result)
            return

        def _post_source_select(self) -> None:
            # POST /source/select body: {"source": "airplay"} or
            # {"source": "auto"}. The mux validates policy and
            # forwards the low-level lane choice to fan-in.
            body = self._read_json()
            source = str(body.get("source") or "").strip().lower()
            if source == "auto":
                cmd = "AUTO"
            elif source in SOURCE_SELECT_IDS:
                cmd = f"SELECT {source}"
            else:
                choices = ", ".join(sorted(SOURCE_SELECT_IDS))
                self._send_json(
                    {
                        "error": (
                            f"source must be {choices}, or auto"
                        ),
                    },
                    status=400,
                )
                return
            try:
                result = asyncio.run(
                    _mux_socket_command(cmd, timeout=6.0),
                )
            except (
                FileNotFoundError,
                ConnectionRefusedError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                self._send_json(
                    {"error": f"jasper-mux unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("source select failed")
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=source.select source=%s client=%s",
                source, self.address_string(),
            )
            self._send_json(_augment_source_payload(result))
            return

        def _post_session(self) -> None:
            cmd = "START" if self.path.endswith("start") else "END"
            try:
                result = asyncio.run(
                    _voice_socket_command(voice_socket_path, cmd),
                )
            except FileNotFoundError:
                self._send_json(
                    {"error": "voice_daemon not running (socket not found)"},
                    status=503,
                )
                return
            except (OSError, asyncio.TimeoutError) as e:
                self._send_json(
                    {"error": f"voice_daemon unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("session %s failed", cmd)
                self._send_json({"error": str(e)}, status=502)
                return
            # Result codes from voice_daemon's manual_session_*:
            #   OK / BUSY / CAP / PAUSED / MUTED / MEASURING /
            #   NO_SESSION / ALREADY_ENDED / ERROR
            # Map non-OK outcomes to non-2xx so the dial's HTTP
            # error path can show the right LED color.
            http_status = 200
            if result.get("result") not in ("OK", None):
                if result.get("result") in ("CAP", "PAUSED", "MUTED", "MEASURING"):
                    http_status = 503
                elif result.get("result") in ("BUSY", "NO_SESSION", "ALREADY_ENDED"):
                    http_status = 409
                else:
                    http_status = 502
            self._send_json(result, status=http_status)
            return

        def _post_cue_play(self) -> None:
            # POST /cue/play  body: {"slug": "<cue_slug>"}
            # Routes the request through voice_daemon's control
            # socket so the cue plays through the daemon's
            # already-correctly-gained TtsPlayout. A separate
            # standalone client (e.g., `jasper-cues play <slug>`)
            # would have to recreate the daemon's volume math
            # to match levels, and got it wrong (~20 dB too
            # loud). Centralising here keeps levels consistent.
            body = self._read_json()
            slug = (body.get("slug") or "").strip()
            if not slug:
                self._send_json(
                    {"error": "missing 'slug' in body"}, status=400,
                )
                return
            try:
                # Cues run ~5-6s of audio plus duck/restore plus
                # drain. 30s gives generous headroom even for the
                # longest reasonable cue.
                result = asyncio.run(_voice_socket_command(
                    voice_socket_path, f"CUE_PLAY {slug}",
                    timeout=30.0,
                ))
            except FileNotFoundError:
                self._send_json(
                    {"error": "voice_daemon not running"}, status=503,
                )
                return
            except (OSError, asyncio.TimeoutError) as e:
                self._send_json(
                    {"error": f"voice_daemon unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("cue play failed")
                self._send_json({"error": str(e)}, status=502)
                return
            http_status = 200
            if result.get("result") == "missing_slug":
                http_status = 400
            elif result.get("result") == "unknown_slug":
                http_status = 404
            elif result.get("result") == "cues_not_configured":
                http_status = 503
            elif result.get("result") != "ok":
                http_status = 502
            self._send_json(result, status=http_status)
            return

        def _post_mic_mute(self) -> None:
            # POST /mic/mute  body: {"muted": bool}
            # Idempotent set. Forwards MUTE or UNMUTE to the voice
            # daemon's control socket, which drops mic frames at
            # the wake-loop gate (mute) or resumes (unmute) and
            # plays a short click on either edge for feedback.
            body = self._read_json()
            if "muted" not in body:
                self._send_json(
                    {"error": "missing 'muted' in body"}, status=400,
                )
                return
            cmd = "MUTE" if bool(body["muted"]) else "UNMUTE"
            try:
                result = asyncio.run(_voice_socket_command(
                    voice_socket_path, cmd, timeout=3.0,
                ))
            except FileNotFoundError:
                self._send_json(
                    {"error": "voice_daemon not running"}, status=503,
                )
                return
            except (OSError, asyncio.TimeoutError) as e:
                self._send_json(
                    {"error": f"voice_daemon unreachable: {e}"},
                    status=503,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("mic %s failed", cmd)
                self._send_json({"error": str(e)}, status=502)
                return
            logger.info(
                "event=mic.set muted=%s client=%s",
                bool(body["muted"]), self.address_string(),
            )
            # Read back the truth from the daemon. STATUS is cheap
            # and the daemon's flag is authoritative.
            try:
                st = asyncio.run(_voice_socket_command(
                    voice_socket_path, "STATUS", timeout=2.0,
                ))
                muted_now = bool(st.get("mic_muted", False))
            except Exception:  # noqa: BLE001
                # If readback fails, trust the set and move on.
                muted_now = bool(body["muted"])
            self._send_json({"muted": muted_now, "result": result.get("result")})
            return

        def _post_aec_toggle(self) -> None:
            # Flip JASPER_AEC_MODE between auto and disabled, then
            # kick the reconciler. The reconciler stops/starts
            # jasper-aec-bridge.service and restarts jasper-voice
            # with the new JASPER_MIC_DEVICE (udp:9876 vs chip
            # direct). Called by the /wake/ page's AEC layer toggle
            # (after a current-state read for idempotent set-state
            # semantics). Non-blocking — the wizard polls /aec to
            # see when the transition lands (~10-15 s). The kick
            # uses systemctl restart so rapid toggles cannot be
            # swallowed while the oneshot reconciler is already active.
            #
            # Risk model: LAN-local + browser-origin guard, same
            # as /system/restart/*. This is still not auth; it is
            # the small boundary that blocks cross-site browser
            # POSTs and DNS-rebinding Host headers while keeping
            # curl, local proxies, and accessories working.
            current = _read_aec_mode()
            new_mode = "disabled" if current == "auto" else "auto"
            try:
                _write_aec_mode(new_mode)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            logger.info(
                "event=aec.toggle from=%s to=%s client=%s",
                current, new_mode, self.address_string(),
            )
            self._send_json({
                "mode": new_mode,
                "bridge_active": _aec_bridge_active(),
            })
            return

        def _post_aec_leg(self) -> None:
            # Toggle one of the additive wake-detection legs
            # (raw chip-direct, DTLN neural, or chip-AEC beams). The
            # reconciler maps the boolean back to the underlying env vars
            # the bridge + voice each read at startup, then
            # restarts whichever daemons need to pick up the
            # change. Per-leg sub-toggles are only meaningful
            # when JASPER_AEC_MODE=auto and the bridge is up;
            # the reconciler clears the underlying vars when
            # AEC is disabled so a stale leg config doesn't
            # leave voice listening on a port nobody talks to.
            #
            # Risk model: LAN-local + browser-origin guard, same
            # as /aec/toggle.
            try:
                body = self._read_json()
            except (ValueError, OSError) as e:
                self._send_json(
                    {"error": f"invalid request body: {e}"}, status=400,
                )
                return
            leg = body.get("leg")
            enabled_val = body.get("enabled")
            if leg not in _TOGGLE_TO_TOKEN:
                self._send_json(
                    {"error": "leg must be one of: "
                              + ", ".join(sorted(_TOGGLE_TO_TOKEN))},
                    status=400,
                )
                return
            if not isinstance(enabled_val, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"}, status=400,
                )
                return
            try:
                _write_aec_leg(leg, enabled_val)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            logger.info(
                "event=aec.leg leg=%s enabled=%s client=%s",
                leg, enabled_val, self.address_string(),
            )
            self._send_json(_aec_full_status())
            return

        def _post_aec_profile(self) -> None:
            # Set the canonical mic/AEC input profile. This is the
            # preferred surface for households and onboarding: it writes
            # one high-level choice plus rollback-safe legacy leg keys.
            # The older /aec/toggle and /aec/leg routes remain as custom
            # expert controls and stamp JASPER_AUDIO_INPUT_PROFILE=custom.
            try:
                body = self._read_json()
            except (ValueError, OSError) as e:
                self._send_json(
                    {"error": f"invalid request body: {e}"}, status=400,
                )
                return
            profile = body.get("profile")
            if not isinstance(profile, str):
                self._send_json(
                    {"error": "profile must be a string"}, status=400,
                )
                return
            try:
                _write_audio_input_profile(profile)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write aec_mode.env failed: {e}"},
                    status=400 if isinstance(e, ValueError) else 502,
                )
                return
            try:
                _kick_aec_reconciler()
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"reconciler restart failed: {e}"},
                    status=502,
                )
                return
            logger.info(
                "event=aec.profile profile=%s client=%s",
                normalize_audio_input_profile(profile, default=""),
                self.address_string(),
            )
            self._send_json(_aec_full_status())
            return

        def _post_aec_threshold(self) -> None:
            # Sensitivity slider on the /wake/ page. Writes
            # JASPER_WAKE_THRESHOLD into wake_model.env (same
            # file the /wake/ form save writes the model into)
            # and restarts jasper-voice — the openWakeWord
            # detector reads the threshold at startup, so a hot
            # config change without a restart wouldn't take
            # effect on the next wake.
            #
            # AEC-mode and leg toggles share the reconciler
            # which already restarts voice; threshold-only
            # changes bypass the reconciler since they don't
            # need the bridge to restart. Non-blocking — the
            # slider's "Applying…" state is just UX.
            try:
                body = self._read_json()
            except (ValueError, OSError) as e:
                self._send_json(
                    {"error": f"invalid request body: {e}"}, status=400,
                )
                return
            try:
                threshold = float(body.get("threshold"))
            except (TypeError, ValueError):
                self._send_json(
                    {"error": "threshold must be a number"}, status=400,
                )
                return
            if not 0.0 <= threshold <= 1.0:
                self._send_json(
                    {"error": "threshold must be between 0 and 1"},
                    status=400,
                )
                return
            try:
                _write_wake_threshold(threshold)
            except (OSError, ValueError) as e:
                self._send_json(
                    {"error": f"write wake_model.env failed: {e}"},
                    status=502,
                )
                return
            try:
                subprocess.Popen(
                    ["systemctl", "restart", "--no-block",
                     "jasper-voice.service"],
                )
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"voice restart failed: {e}"},
                    status=502,
                )
                return
            logger.info(
                "event=wake.threshold value=%.2f client=%s",
                threshold, self.address_string(),
            )
            self._send_json({"threshold": threshold})
            return

        def _post_debug(self) -> None:
            # /system Debug card: raise one subsystem to DEBUG
            # logging. Additive-only + auto-expiring (jasper/
            # debug_mode.py). Daemon subsystems restart to apply;
            # control applies in-process. Non-blocking — the card's
            # "Applying…" state is just UX.
            try:
                body = self._read_json()
            except (ValueError, OSError) as e:
                self._send_json(
                    {"error": f"invalid request body: {e}"}, status=400,
                )
                return
            subsystem = str(body.get("subsystem") or "")
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"}, status=400,
                )
                return
            try:
                debug_control.set_debug(subsystem, enabled)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"debug toggle failed: {e}"}, status=502,
                )
                return
            logger.info(
                "event=debug.toggle subsystem=%s enabled=%s client=%s",
                subsystem, enabled, self.address_string(),
            )
            self._send_json(debug_control.snapshot())
            return

        def _post_system_audio_quality(self) -> None:
            try:
                body = self._read_json()
            except (ValueError, OSError) as e:
                self._send_json(
                    {"error": f"invalid request body: {e}"}, status=400,
                )
                return
            if not isinstance(body, dict):
                self._send_json(
                    {"error": "invalid request body: expected JSON object"},
                    status=400,
                )
                return
            raw_converter = body.get("converter")
            if not isinstance(raw_converter, str) or not raw_converter.strip():
                self._send_json(
                    {"error": "converter is required"},
                    status=400,
                )
                return
            try:
                converter = _normalize_audio_converter(raw_converter)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=400)
                return
            try:
                state = _apply_audio_quality(converter)
            except (OSError, subprocess.SubprocessError) as e:
                logger.exception("audio quality apply failed")
                self._send_json(
                    {"error": f"audio quality apply failed: {e}"},
                    status=502,
                )
                return
            try:
                # Refresh active renderers without resurrecting sources the
                # household explicitly disabled in /sources/.
                subprocess.Popen(
                    [
                        "systemctl",
                        "try-restart",
                        *AUDIO_QUALITY_RENDERER_UNITS,
                    ],
                )
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"renderer restart failed: {e}"},
                    status=502,
                )
                return
            logger.info(
                "event=audio_quality.set converter=%s client=%s",
                converter, self.address_string(),
            )
            self._send_json({
                "ok": True,
                "action": "audio-quality",
                "try_restart_units": AUDIO_QUALITY_RENDERER_UNITS,
                "audio_quality": state,
            })
            return

        def _post_system_action(self) -> None:
            # Action endpoints for the /system dashboard. All
            # shell out to systemctl; jasper-control already runs
            # as root so no sudo needed. Returns immediately —
            # the restart is async on systemd's side and the
            # dashboard polls /system/snapshot to know when
            # things are back up.
            #
            # Risk model: LAN-local + browser-origin guard
            # (consistent with the wizards). Anyone already on the
            # trusted WiFi can trigger these; the dashboard's
            # confirm dialogs are UX, not security.
            if self.path == "/system/restart/voice":
                units = ["jasper-voice.service"]
                action = "restart-voice"
            elif self.path == "/system/restart/audio":
                units = [
                    "jasper-camilla.service",
                    "librespot.service",
                    "shairport-sync.service",
                    "bluealsa-aplay.service",
                ]
                action = "restart-audio"
            elif self.path == "/system/reboot":
                units = []  # systemctl reboot — no units
                action = "reboot"
            else:
                # poweroff is reboot's terminal sibling: the speaker
                # stays off until someone physically re-plugs power.
                # The "graceful" part matters more than usual here —
                # this endpoint exists *specifically* to give the
                # household a non-power-yank way to shut down before
                # hardware changes, after 2026-05-23's dirty-shutdown
                # incident wiped the NetworkManager keyfile.
                units = []  # systemctl poweroff — no units
                action = "poweroff"
            # Audit BEFORE the action: reboot/poweroff take the system down, so
            # a log-after might never flush. This is the line that
            # distinguishes a dashboard-triggered restart/reboot/poweroff from a
            # watchdog or crash reset when debugging "the speaker restarted on
            # its own" (see AGENTS.md). No secrets — action + units + requester.
            logger.info(
                "event=system.action action=%s units=%s client=%s",
                action, ",".join(units) or "-", self.address_string(),
            )
            try:
                if action == "reboot":
                    subprocess.Popen(["systemctl", "reboot"])
                elif action == "poweroff":
                    subprocess.Popen(["systemctl", "poweroff"])
                else:
                    # Use start-after-stop semantics. Don't block
                    # on the systemctl call (jasper-aec-bridge +
                    # jasper-voice both take up to 90s to stop
                    # cleanly under the SIGTERM timeout).
                    subprocess.Popen(["systemctl", "restart", *units])
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json(
                    {"error": f"systemctl invocation failed: {e}"},
                    status=502,
                )
                return
            self._send_json({
                "ok": True,
                "action": action,
                "units": units,
            })
            return

        # --- route tables (path -> handler-method name) ---
        # Keyed by exact path. Method dispatch (do_GET vs do_POST)
        # disambiguates the two '/debug' handlers; tuple routes map
        # each member path to the one method that re-discriminates
        # self.path internally (transport action, system action).
        # The string keys keep the route literals greppable for the
        # client/server contract test (tests/test_control_client.py).
        _GET_ROUTES = {
            "/healthz": "_get_healthz",
            "/volume": "_get_volume",
            "/mic": "_get_mic",
            "/source/state": "_get_source_state",
            "/aec": "_get_aec",
            "/debug": "_get_debug",
            "/state": "_get_state",
            "/grouping": "_get_grouping",
            "/dial/status": "_get_dial_status",
            "/system/snapshot": "_get_system_snapshot",
            "/system/diagnostics": "_get_system_diagnostics",
        }
        _POST_ROUTES = {
            "/volume/adjust": "_post_volume_adjust",
            "/volume/set": "_post_volume_set",
            "/grouping/set": "_post_grouping_set",
            "/volume/mute": "_post_volume_mute",
            "/transport/toggle": "_post_transport",
            "/transport/next": "_post_transport",
            "/transport/previous": "_post_transport",
            "/source/select": "_post_source_select",
            "/session/start": "_post_session",
            "/session/end": "_post_session",
            "/cue/play": "_post_cue_play",
            "/mic/mute": "_post_mic_mute",
            "/aec/toggle": "_post_aec_toggle",
            "/aec/leg": "_post_aec_leg",
            "/aec/profile": "_post_aec_profile",
            "/aec/threshold": "_post_aec_threshold",
            "/debug": "_post_debug",
            "/system/audio-quality": "_post_system_audio_quality",
            "/system/restart/voice": "_post_system_action",
            "/system/restart/audio": "_post_system_action",
            "/system/reboot": "_post_system_action",
            "/system/poweroff": "_post_system_action",
        }

    return Handler


class ControlHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose accept loop drives the systemd watchdog.

    `service_actions()` runs on every `serve_forever()` poll iteration
    (~0.5 s cadence) **in the accept-loop thread itself**, so bumping the
    heartbeat here ties `WATCHDOG=1` to the loop actually spinning: if
    the accept loop wedges (blocked selector, interpreter deadlock), the
    bumps stop, `jasper.watchdog.Heartbeat`'s progress sentinel goes
    stale, pats stop, and systemd's `WatchdogSec=` revives us with a
    fresh process. Request handlers run on worker threads and
    intentionally don't gate the heartbeat — a slow probe must not look
    like a dead daemon. Same Tier 1 mechanism as jasper-voice
    (Type=notify + sentinel-guarded `WATCHDOG=1`).

    `heartbeat` stays None in tests/dev so the server runs standalone.
    """

    heartbeat: Any = None

    def service_actions(self) -> None:
        super().service_actions()
        hb = self.heartbeat
        if hb is not None:
            hb.bump()


def build_server(
    host: str,
    port: int,
    camilla_host: str,
    camilla_port: int,
    voice_socket_path: str = "/run/jasper/voice.sock",
    sampler: Any = None,
    airplay_health_sampler: Any = None,
) -> ControlHTTPServer:
    return ControlHTTPServer(
        (host, port),
        _make_handler(
            camilla_host,
            camilla_port,
            voice_socket_path,
            sampler,
            airplay_health_sampler,
        ),
    )


def run_dial_log_listener(host: str, port: int) -> threading.Thread:
    """Listen for one-line UDP datagrams from the dial and re-emit them
    via the Python logger (so `journalctl -u jasper-control` shows them
    interleaved with the HTTP-side log). Fire-and-forget on the dial
    side — UDP loss is acceptable for diagnostic output, and the dial
    isn't blocked on a TCP handshake when the Pi is unreachable.

    The listener runs in a daemon thread so it doesn't block server
    shutdown."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)

    def _loop() -> None:
        logger.info("dial-log UDP listener bound to %s:%d", host, port)
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("dial-log socket error: %s", e)
                return
            try:
                msg = data.decode("utf-8", errors="replace").rstrip()
            except Exception:  # noqa: BLE001
                msg = repr(data)
            # Tag with sender IP so multi-dial setups don't get confused.
            dial_log.info("[%s] %s", addr[0], msg)
            # Heartbeat for jasper-doctor's "is the dial talking?" check.
            _dial_heartbeat["last_seen_at"] = time.time()
            _dial_heartbeat["last_seen_ip"] = addr[0]
            _dial_heartbeat["last_message"] = msg
            # Persist so the next jasper-control restart inherits the
            # last-known IP instead of starting empty (which would leave
            # /state.satellites.dial.online as false until the next dlog).
            _persist_dial_heartbeat(dict(_dial_heartbeat))

    t = threading.Thread(target=_loop, name="dial-log-listener", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-control",
        description="HTTP control surface for the JTS speaker (dial, automation, etc.)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_CONTROL_HOST", "0.0.0.0"),
        help="bind host (default 0.0.0.0 — LAN-reachable)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CONTROL_PORT", "8780")),
    )
    parser.add_argument(
        "--camilla-host",
        default=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--camilla-port", type=int,
        default=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )
    parser.add_argument(
        "--dial-log-host",
        default=os.environ.get("JASPER_DIAL_LOG_HOST", "0.0.0.0"),
        help="bind host for the dial UDP log listener",
    )
    parser.add_argument(
        "--dial-log-port", type=int,
        default=int(os.environ.get("JASPER_DIAL_LOG_PORT", "5514")),
        help="UDP port for dial log datagrams (default 5514)",
    )
    parser.add_argument(
        "--voice-socket",
        default=os.environ.get(
            "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
        ),
        help="path to voice_daemon's control UDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Log flight recorder + runtime debug toggle (/system Debug card).
    # install() holds the jasper logger at DEBUG for the in-RAM ring,
    # keeps the journal at INFO, applies the debug toggle, and wires
    # SIGUSR1 -> dump. See jasper/flight_recorder.py.
    from .. import flight_recorder
    flight_recorder.install("control")

    # System metrics sampler — 5 s ring buffer for the /system dashboard.
    # Daemon thread, exits with the process.
    from .system_metrics import SystemSampler
    sampler = SystemSampler()
    sampler.start()
    # AirPlay health sampler — cheap fan-in counters at 5 s, slower
    # journal/MPRIS/Camilla probes for the /system AirPlay card.
    from .airplay_health import AirPlayHealthSampler
    airplay_health_sampler = AirPlayHealthSampler(
        camilla_host=args.camilla_host,
        camilla_port=args.camilla_port,
    )
    airplay_health_sampler.start()

    server = build_server(
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.voice_socket,
        sampler=sampler,
        airplay_health_sampler=airplay_health_sampler,
    )
    run_dial_log_listener(args.dial_log_host, args.dial_log_port)
    # Multi-device peering daemon. No-op (no thread, no asyncio loop,
    # no zeroconf import) when /var/lib/jasper/peering.env has
    # JASPER_PEERING=off — the default. The user enables it via the
    # /peers/ web wizard (added in a follow-up PR), which writes the
    # env file and restarts jasper-control to pick up the new mode.
    start_peering_daemon_if_enabled()
    # Tier 3 resilience: protocol-level liveness probe for shairport-sync
    # so a wedged AP2 control plane recovers without manual intervention.
    # docs/HANDOFF-resilience.md (Tier 3). Off via
    # JASPER_SHAIRPORT_SUPERVISOR=disabled in /etc/jasper/jasper.env.
    shairport_supervisor.start_supervisor()
    # T5.2 — userspace-liveness supervisor closing the gap exposed
    # by the 2026-05-23 incident (PID 1 alive enough to pat the
    # kernel watchdog but sshd / userspace effectively dead). Probes
    # sshd banner + our own HTTP /healthz + /proc/loadavg; clean
    # `systemctl reboot` after 3 consecutive failures, rate-limited
    # to 1 reboot per 24 hours. docs/HANDOFF-tier5-watchdog-liveness.md.
    # Off via JASPER_SYSTEM_SUPERVISOR=disabled.
    system_supervisor.start_supervisor()
    # Bonded-member runtime liveness: closes the gap between grouping
    # reconciles — sustained dac_content starvation kicks the
    # reconciler (rate-limited), and the leader's snapcast group→stream
    # bindings are read-repaired every poll (the 2026-06-11 silent-bond
    # class). Costs one grouping.env read per 30 s when solo. Off via
    # JASPER_GROUPING_SUPERVISOR=disabled.
    grouping_supervisor.start_supervisor()
    # Runtime debug toggle: clear an expired session left on disk, or
    # re-arm the auto-quiet timer if a debug session is still active
    # across this control restart. See jasper/control/debug_control.py.
    debug_control.reconcile_on_startup()
    logger.info(
        "jasper-control listening on http://%s:%d "
        "(camilla=%s:%d, dial-log=%s:%d/udp, voice=%s)",
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.dial_log_host, args.dial_log_port,
        args.voice_socket,
    )
    # Tier 1 — systemd watchdog (Type=notify + WatchdogSec in the unit).
    # READY=1 goes out here; serve_forever()'s poll loop bumps the
    # progress sentinel via ControlHTTPServer.service_actions, so a
    # wedged accept loop stops the WATCHDOG=1 pats and systemd restarts
    # us. No-ops outside systemd (NOTIFY_SOCKET unset). Same Heartbeat
    # helper jasper-voice uses (jasper/watchdog.py).
    from ..watchdog import Heartbeat
    heartbeat = Heartbeat()
    server.heartbeat = heartbeat
    heartbeat.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        heartbeat.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
