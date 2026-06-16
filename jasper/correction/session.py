"""Measurement-session state machine.

Phase 2: multi-position MMM averaging + verify pass.

The session owns the measurement state machine. The web handler opens
a fresh measurement window for each sweep so renderers pause only while
the speaker is actively measuring. Browser-side calls arm capture,
upload pre-sweep room noise, trigger sweep playback, upload the captured
response, and eventually apply or verify the generated correction.

State transitions in the browser flow (with N = total_positions):

    IDLE → NEEDS_NOISE_CAPTURE → PREPARING → SWEEPING → AWAITING_CAPTURE
         → (on_capture_uploaded for position 0 ... N-2)
         → optional NEEDS_REPEAT_CAPTURE → AWAITING_REPEAT_CAPTURE
         → NEEDS_NEXT_POSITION
         → NEEDS_NOISE_CAPTURE → SWEEPING (position 1)
         → ...
         → AWAITING_CAPTURE (position N-1)
         → on_capture_uploaded for last position
         → ANALYZING (spatial avg + PEQ design)
         → READY → APPLIED
         → VERIFYING (separate fresh window) → AWAITING_VERIFY_CAPTURE
         → VERIFIED  (back to APPLIED with verify_curve populated)

Single-position is N=1: the same flow, just no NEEDS_NEXT_POSITION.

Phase 1 callers (the old `prepare_and_play_sweep` API) still work —
it's a thin wrapper that opens a fresh window for one position
and closes it when done.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from . import (
    acoustic_quality,
    analysis,
    browser_audio,
    calibration,
    confidence,
    deconv,
    quality,
    runtime_integrity,
    strategy,
    sweep,
)
from .artifacts import ANALYSIS_NORMALIZE_BAND_HZ, SessionArtifacts
from .calibration import CalibrationRecord
from .peq import PEQ
from ..log_event import log_event

logger = logging.getLogger(__name__)


_CORRECTION_FILENAME_RE = re.compile(
    r"^correction_(?P<id>[A-Za-z0-9]+)_(?P<ts>\d+)\.yml$"
)
_SOUND_FILENAME_RE = re.compile(r"^sound_(?:current|audition)\.yml$")
_PEQ_KEY_RE = re.compile(r"^\s+(?:peq|room_peq)_\d+:", re.MULTILINE)


def parse_current_correction(
    path: str | None,
    *,
    config_dir: Path = Path("/var/lib/camilladsp/configs"),
) -> dict[str, Any] | None:
    """Describe whatever correction (if any) the given CamillaDSP
    config path represents. Returns None for the base outputd
    config or any path we don't recognise as a correction emission.

    The filename shape is fixed by `MeasurementSession.apply`:
    ``correction_<session_id>_<unixtime>.yml`` under
    ``/var/lib/camilladsp/configs/``. Anything else returns None for
    backwards compatibility. Use `describe_current_config()` when the
    caller needs to distinguish the flat outputd baseline,
    JTS-managed sound configs, and custom CamillaDSP configs.
    """
    descriptor = describe_current_config(path, config_dir=config_dir)
    correction = descriptor.get("current_correction")
    return correction if isinstance(correction, dict) else None


def describe_current_config(
    path: str | None,
    *,
    config_dir: Path = Path("/var/lib/camilladsp/configs"),
    base_config_path: Path = Path("/etc/camilladsp/outputd-cutover.yml"),
) -> dict[str, Any]:
    """Describe the active CamillaDSP config without overclaiming.

    `parse_current_correction()` intentionally remains the backwards-
    compatible "is there a JTS room correction?" helper. This richer
    descriptor lets UI/doctor/agent surfaces distinguish the flat
    outputd baseline, JTS-generated sound/correction configs,
    and arbitrary CamillaGUI/custom configs that JTS should not
    silently preserve.
    """
    if not path:
        return {
            "kind": "unknown",
            "managed": False,
            "path": None,
            "label": "Unknown active config",
            "message": "CamillaDSP did not report an active config path.",
            "current_correction": None,
        }
    p = Path(path)
    if p == base_config_path:
        return {
            "kind": "base",
            "managed": True,
            "path": str(p),
            "label": "JTS flat baseline",
            "message": "No JTS room correction is applied.",
            "current_correction": None,
        }
    if p.parent != Path(config_dir):
        return {
            "kind": "custom",
            "managed": False,
            "path": str(p),
            "label": "Advanced DSP config",
            "message": (
                "CamillaDSP is running a config outside the JTS generated "
                "config directory. JTS cannot safely preserve it."
            ),
            "current_correction": None,
        }

    m = _CORRECTION_FILENAME_RE.match(p.name)
    if not m:
        if not _SOUND_FILENAME_RE.match(p.name):
            return {
                "kind": "custom",
                "managed": False,
                "path": str(p),
                "label": "Advanced DSP config",
                "message": (
                    "CamillaDSP is running a config that JTS did not "
                    "generate. JTS cannot safely preserve it."
                ),
                "current_correction": None,
            }
        try:
            text = p.read_text()
            peq_count = len(_PEQ_KEY_RE.findall(text))
            applied_at_epoch = int(p.stat().st_mtime)
        except OSError:
            return {
                "kind": "unknown",
                "managed": False,
                "path": str(p),
                "label": "Unreadable JTS sound config",
                "message": "JTS could not read the active sound config file.",
                "current_correction": None,
            }
        if peq_count == 0:
            return {
                "kind": "sound_preference",
                "managed": True,
                "path": str(p),
                "label": "JTS sound preference",
                "message": "Preference EQ is active; no room correction PEQs were found.",
                "current_correction": None,
            }
        correction = {
            "path": str(p),
            "session_id": "sound",
            "applied_at_epoch": applied_at_epoch,
            "peq_count": peq_count,
        }
        return {
            "kind": "sound_with_correction",
            "managed": True,
            "path": str(p),
            "label": "JTS sound preference with room correction",
            "message": "A JTS sound config is active and includes room correction PEQs.",
            "current_correction": correction,
        }
    try:
        ts = int(m.group("ts"))
    except ValueError:
        return {
            "kind": "custom",
            "managed": False,
            "path": str(p),
            "label": "Advanced DSP config",
            "message": "Correction-shaped config has an invalid timestamp.",
            "current_correction": None,
        }
    peq_count = 0
    try:
        text = p.read_text()
    except OSError:
        text = ""
    if text:
        peq_count = len(_PEQ_KEY_RE.findall(text))
    correction = {
        "path": str(p),
        "session_id": m.group("id"),
        "applied_at_epoch": ts,
        "peq_count": peq_count,
    }
    return {
        "kind": "correction",
        "managed": True,
        "path": str(p),
        "label": "JTS room correction",
        "message": "A JTS room correction config is active.",
        "current_correction": correction,
    }


def _bundles_enabled() -> bool:
    """Default ON; opt-out via JASPER_CORRECTION_SAVE_BUNDLES=0."""
    return os.environ.get("JASPER_CORRECTION_SAVE_BUNDLES", "1").strip() != "0"


DBFS_FLOOR = -120.0
SNR_BANDS_HZ: tuple[tuple[str, float, float], ...] = (
    ("sub_bass", 20.0, 80.0),
    ("bass", 80.0, 160.0),
    ("upper_bass", 160.0, 350.0),
    ("transition", 350.0, 1000.0),
)


def _dbfs(value: float) -> float:
    if value <= 0 or not np.isfinite(value):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(value))


def _band_levels_dbfs(samples: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
    if samples.ndim != 1 or sample_rate <= 0 or samples.size < 8:
        return []
    x = np.asarray(samples, dtype=np.float64)
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    out: list[dict[str, Any]] = []
    for band_id, low, high in SNR_BANDS_HZ:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            continue
        rms_like = math.sqrt(float(np.mean(power[mask]))) / max(1, x.size)
        out.append({
            "band_id": band_id,
            "band_hz": [low, high],
            "level_dbfs": round(_dbfs(rms_like), 2),
        })
    return out


class SessionState(Enum):
    IDLE = "idle"
    NEEDS_NOISE_CAPTURE = "needs_noise_capture"
    PREPARING = "preparing"
    SWEEPING = "sweeping"
    AWAITING_CAPTURE = "awaiting_capture"
    NEEDS_REPEAT_CAPTURE = "needs_repeat_capture"
    AWAITING_REPEAT_CAPTURE = "awaiting_repeat_capture"
    NEEDS_NEXT_POSITION = "needs_next_position"
    ANALYZING = "analyzing"
    READY = "ready"
    APPLIED = "applied"
    VERIFYING = "verifying"
    AWAITING_VERIFY_CAPTURE = "awaiting_verify_capture"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass
class CurveJSON:
    freqs_hz: list[float]
    magnitude_db: list[float]


@dataclass
class PEQJSON:
    freq_hz: float
    q: float
    gain_db: float

    @classmethod
    def from_peq(cls, p: PEQ) -> "PEQJSON":
        return cls(freq_hz=p.freq, q=p.q, gain_db=p.gain)


@dataclass
class SessionEvent:
    seq: int
    timestamp: float
    type: str
    payload: dict[str, Any]


class AutolevelStatus(Enum):
    """Auto-level sub-state. Orthogonal to the measurement state
    machine — autolevel can run from any "idle-ish" session state
    (IDLE / READY / APPLIED / VERIFIED / FAILED) without affecting
    the session's measurement flow.
    """
    IDLE = "idle"
    RAMPING = "ramping"
    LOCKED = "locked"
    MAXED_OUT = "maxed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class AutolevelData:
    """Tracks one auto-level run. Replaced (not mutated) when the
    user starts a new one.

    `original_main_volume_db` is the CamillaDSP `main_volume` value
    saved at the start of the run, so we can restore it after the
    measurement workflow completes (apply / reset). `current` is
    where the ramp is right now; `locked` is where it ended up
    when the user signalled lock (or where it was when the ramp
    completed without lock). `cap_db` is the dynamic end-of-ramp
    cap computed from `original + bump`; exposed so the client and
    tests can verify what cap the run is operating under.
    """
    status: AutolevelStatus = AutolevelStatus.IDLE
    current_main_volume_db: float = -50.0
    original_main_volume_db: float | None = None
    locked_main_volume_db: float | None = None
    cap_db: float | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return round(x, 2) if x is not None else None
        return {
            "status": self.status.value,
            "current_main_volume_db": r(self.current_main_volume_db),
            "original_main_volume_db": r(self.original_main_volume_db),
            "locked_main_volume_db": r(self.locked_main_volume_db),
            "cap_db": r(self.cap_db),
            "error": self.error,
        }


@dataclass
class SessionConfig:
    sweep_dir: Path = Path("/var/lib/jasper/correction/sweeps")
    capture_dir: Path = Path("/var/lib/jasper/correction/captures")
    sessions_dir: Path = Path("/var/lib/jasper/correction/sessions")
    config_dir: Path = Path("/var/lib/camilladsp/configs")
    base_config_path: Path = Path("/etc/camilladsp/outputd-cutover.yml")
    calibration_dir: Path = calibration.DEFAULT_CALIBRATION_DIR

    f1_hz: float = 20.0
    f2_hz: float = 20000.0
    duration_s: float = 10.0
    sample_rate: int = 48000
    amplitude_dbfs: float = -12.0

    peq_f_low: float = 20.0
    peq_f_high: float = 350.0
    peq_max_filters: int = 5
    peq_max_cut_db: float = -10.0
    peq_max_boost_db: float = 3.0
    peq_cuts_only: bool = True
    peq_flatness_target_db: float = 1.0
    correction_strategy: str = strategy.DEFAULT_CORRECTION_STRATEGY_ID


# After a sweep the session waits in an awaiting_*_capture state for the
# browser to upload the recording. If that upload never arrives (iOS screen
# lock, backgrounded tab, network blip), the session would wedge forever and
# block every future /start. This watchdog abandons such a stranded session so
# the wizard self-recovers. A sweep+upload normally completes in seconds; 120 s
# is generous headroom. Mirrors voice_daemon's measurement-window auto-clear.
AWAITING_CAPTURE_TIMEOUT_SEC = 120.0

# States the stranded-capture watchdog guards: every state where the session
# is parked waiting on the BROWSER to upload audio it records automatically,
# with no user action in the loop. If that upload never arrives — denied mic
# permission, a backgrounded iOS tab, a closed page — the session would wedge
# forever and block every future /start. needs_noise_capture is included
# because it is exactly that shape: an automatic pre-sweep noise recording,
# entered before any measurement window opens (so a wedge there never leaves
# the speaker muted — see begin_noise_capture / prepare_and_play_sweep). The
# user-paced needs_next_position / needs_repeat_capture states are deliberately
# NOT guarded: the user may legitimately take minutes to reposition the phone,
# and those states already carry their own Cancel affordance.
_CAPTURE_TIMEOUT_STATES = frozenset({
    SessionState.NEEDS_NOISE_CAPTURE,
    SessionState.AWAITING_CAPTURE,
    SessionState.AWAITING_REPEAT_CAPTURE,
    SessionState.AWAITING_VERIFY_CAPTURE,
})

# States reset() refuses, because a fire-and-forget sweep/analysis task is
# actively running and will set the next state AFTER reset() sets IDLE —
# leaving the session looking reset for an instant and then jumping back.
# These are exactly the states the wizard never offers Cancel / Reset from
# (see cancellableStates in correction/js/main.js), so rejecting them here
# breaks no UI affordance; it only fences a stale/buggy client off the race.
# Every settled, parked, or wedged state (idle / needs_* / awaiting_* / ready
# / applied / verified / failed) still resets, so the escape hatch keeps
# working when the user actually needs it.
_RESET_BUSY_STATES = frozenset({
    SessionState.PREPARING,
    SessionState.SWEEPING,
    SessionState.ANALYZING,
    SessionState.VERIFYING,
})


def compute_autolevel_cap(
    original_db: float, *, bump_db: float, floor_db: float, ceil_db: float
) -> float:
    """End-of-ramp cap for auto-level: +bump over the user's listening
    volume, clamped to [floor, ceil]. The floor raises a very quiet listener
    UP to a usable measurement level; the ceiling is the room safety limit.
    Pinned by tests so the maxed_out UI reads cap_db rather than hardcoding it.
    """
    return max(floor_db, min(original_db + bump_db, ceil_db))


class MeasurementSession:
    """Multi-position measurement session.

    The HTTP handler creates one MeasurementSession per `POST /start`
    call. It lives until the next `/start` (which replaces it) or
    until the daemon restarts. State + curves persist as long as
    the session does.
    """

    def __init__(
        self,
        cfg: SessionConfig | None = None,
        *,
        total_positions: int = 1,
        target_choice: str = "flat",
        strategy_choice: str | None = None,
        mic_calibration: CalibrationRecord | None = None,
        input_device: dict[str, Any] | None = None,
        repeat_main_position: bool = False,
    ) -> None:
        self.cfg = cfg or SessionConfig()
        self.session_id = uuid.uuid4().hex[:12]
        self.state = SessionState.IDLE
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.error: str | None = None

        self.total_positions = max(1, int(total_positions))
        self.current_position = 0
        self.target_choice = strategy.resolve_target_profile(
            target_choice,
        ).target_id
        self.strategy_choice = strategy.resolve_correction_strategy(
            strategy_choice or self.cfg.correction_strategy,
        ).strategy_id
        self.mic_calibration = mic_calibration
        self.input_device = input_device
        self.repeat_main_position = bool(repeat_main_position)
        self.browser_audio_report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=self.cfg.sample_rate,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        # Per-position smoothed magnitude responses (dB on log grid).
        # Spatial-averaged at end of multi-position flow.
        self.position_magnitudes: list[np.ndarray] = []
        self.position_freqs: np.ndarray | None = None  # log grid
        self.capture_quality: list[dict[str, Any]] = []
        self.noise_reports: list[dict[str, Any]] = []
        self.repeat_quality: dict[str, Any] | None = None
        self.repeat_curve: CurveJSON | None = None
        self.repeatability_report: dict[str, Any] | None = None
        self.verify_quality: dict[str, Any] | None = None
        self.confidence_report: dict[str, Any] | None = None
        self.acoustic_quality: dict[str, Any] | None = None
        self.runtime_integrity = runtime_integrity.RuntimeIntegrityReport(
            self.session_id,
        )
        self.position_analysis: dict[str, Any] | None = None

        # Output curves for the chart.
        self.measured_curve: CurveJSON | None = None
        self.target_curve: CurveJSON | None = None
        self.predicted_curve: CurveJSON | None = None
        self.verify_curve: CurveJSON | None = None
        self.verify_metrics: dict[str, float] | None = None
        self.design_report: dict[str, Any] | None = None

        self.peqs: list[PEQJSON] = []
        self.config_path: Path | None = None

        # Sweep cache.
        self.sweep_meta: sweep.SweepMeta | None = None
        self.sweep_wav_path: Path | None = None
        self.last_capture_path: Path | None = None

        # Auto-level sub-state — orthogonal to the measurement
        # state machine; runs against CamillaDSP main_volume +
        # iPhone-mic feedback.
        self.autolevel: AutolevelData = AutolevelData()
        self._autolevel_lock_event: asyncio.Event | None = None
        self._autolevel_cancel_event: asyncio.Event | None = None

        # Single-slot watchdog that abandons a session stranded waiting for a
        # capture upload (see AWAITING_CAPTURE_TIMEOUT_SEC). Armed/cancelled
        # centrally from _set_state; capture_timeout_sec is overridable in
        # tests (and disabled when <= 0).
        self._capture_timeout_task: asyncio.Task[None] | None = None
        self.capture_timeout_sec: float = AWAITING_CAPTURE_TIMEOUT_SEC

        # Optional client-reported room noise floor (the autolevel
        # preflight measures this in the browser before the tone
        # plays). Saved into info.json so debug bundles preserve the
        # context that drove the autolevel target band.
        self.noise_floor_db: float | None = None

        # Snapshot of the active CamillaDSP config at the moment
        # `/start` was hit, BEFORE the auto-reset to base config. Lets
        # the bundle reproduce what state the speaker was in when this
        # session began, including custom configs that JTS cannot
        # safely preserve.
        self.current_correction_at_start: dict[str, Any] | None = None

        # Per-session debug bundle. All artifacts (info.json,
        # result.json, per-position WAVs, verify.wav, applied.yml)
        # and mic_calibration.* land here. The directory is created
        # lazily on first write so tests that pass a SessionConfig
        # pointing at a tmp_path don't have to pre-mkdir.
        self.bundle_dir: Path = self.cfg.sessions_dir / self.session_id
        self.save_bundles: bool = _bundles_enabled()
        self.artifacts = SessionArtifacts(self)

        # Events retained for debugging / future progress streams. The
        # shipped browser UI currently polls GET /status.
        self._events: list[SessionEvent] = []
        self._event_seq = 0
        # Lazy-init the asyncio.Lock — Python 3.9 binds it to the
        # current loop at construction time and raises if none is
        # running. Construction happens from sync HTTP-handler
        # threads, so deferring until first async use keeps the
        # session safe to instantiate anywhere.
        self._lock_obj: asyncio.Lock | None = None

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self._event_seq += 1
        ev = SessionEvent(
            seq=self._event_seq,
            timestamp=time.time(),
            type=type_,
            payload=payload,
        )
        self._events.append(ev)
        self.updated_at = ev.timestamp

    def _cancel_capture_timeout(self) -> None:
        task = self._capture_timeout_task
        self._capture_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    def _arm_capture_timeout(self, state: SessionState) -> None:
        if self.capture_timeout_sec <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._capture_timeout_task = loop.create_task(
            self._capture_timeout_guard(state, self.capture_timeout_sec)
        )

    async def _capture_timeout_guard(
        self, expected_state: SessionState, timeout_sec: float,
    ) -> None:
        try:
            await asyncio.sleep(timeout_sec)
        except asyncio.CancelledError:
            return
        # Self-contained: this runs as a detached task, so any error here would
        # surface only as an "exception never retrieved" warning. Swallow +
        # log instead of letting that leak.
        try:
            async with self._lock:
                # Own the slot first so _fail()'s cancel doesn't cancel us.
                self._capture_timeout_task = None
                if self.state != expected_state:
                    return
                log_event(
                    logger,
                    "correction_capture_timeout",
                    session=self.session_id,
                    state=expected_state.value,
                    after_sec=f"{timeout_sec:.0f}",
                    level=logging.WARNING,
                )
                await self._fail(
                    "capture never arrived — tap Start to measure again"
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "capture-timeout guard failed (session=%s)", self.session_id,
            )

    async def _set_state(self, state: SessionState, **extra: Any) -> None:
        prev = self.state
        self.state = state
        # Re-arm the stranded-capture watchdog on every transition: cancel any
        # pending timer, then start a fresh one only when entering a state that
        # waits on an automatic browser upload. An upload (or any other
        # transition) cancels it for free.
        self._cancel_capture_timeout()
        if state in _CAPTURE_TIMEOUT_STATES:
            self._arm_capture_timeout(state)
        payload = {"state": state.value, "prev": prev.value, **extra}
        self._emit("state", payload)
        logger.info(
            "session %s: %s → %s %s",
            self.session_id, prev.value, state.value,
            extra if extra else "",
        )
        # Bundle artifacts are best-effort — never let a write failure
        # bring down a measurement state transition.
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", state.value,
            )

    # ------------------------------------------------------------------
    # Bundle artifacts. MeasurementSession keeps this public/internal surface
    # for callers, while SessionArtifacts owns the file-system work.
    # ------------------------------------------------------------------

    def _ensure_bundle_dir(self) -> Path | None:
        return self.artifacts.ensure_bundle_dir()

    def _bundle_relative_path(self, path: Path) -> str | None:
        return self.artifacts.bundle_relative_path(path)

    def _existing_bundle_dependencies(self, *paths: str) -> list[str]:
        return self.artifacts.existing_bundle_dependencies(*paths)

    def _write_capture_replay_artifacts(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
        ir: np.ndarray,
        raw_freqs_hz: np.ndarray,
        raw_magnitude_db: np.ndarray,
        smoothed_magnitude_db: np.ndarray,
        log_freqs_hz: np.ndarray,
        log_magnitude_db: np.ndarray,
        direct_arrival: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.artifacts.write_capture_replay_artifacts(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            ir=ir,
            raw_freqs_hz=raw_freqs_hz,
            raw_magnitude_db=raw_magnitude_db,
            smoothed_magnitude_db=smoothed_magnitude_db,
            log_freqs_hz=log_freqs_hz,
            log_magnitude_db=log_magnitude_db,
            direct_arrival=direct_arrival,
        )

    def _record_raw_capture_artifact(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None = None,
    ) -> None:
        self.artifacts.record_raw_capture_artifact(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
        )

    def _refresh_acoustic_quality(self) -> None:
        self.acoustic_quality = acoustic_quality.build_acoustic_quality_report(
            session_id=self.session_id,
            capture_quality=self.capture_quality,
            noise_reports=self.noise_reports,
            repeat_quality=self.repeat_quality,
            repeatability=self.repeatability_report,
            verify_quality=self.verify_quality,
        )

    def _write_acoustic_quality_json(self) -> None:
        self.artifacts.write_acoustic_quality_json()

    def _write_runtime_integrity_json(
        self,
        *,
        extra_dependencies: tuple[str, ...] = (),
    ) -> None:
        self.artifacts.write_runtime_integrity_json(
            extra_dependencies=extra_dependencies,
        )

    def _log_runtime_integrity_issues(
        self,
        issues: list[dict[str, Any]],
    ) -> None:
        for issue in issues:
            log_event(
                logger,
                "correction_runtime_integrity_issue",
                session=self.session_id,
                code=issue.get("code"),
                severity=issue.get("severity"),
                capture_kind=issue.get("capture_kind"),
                position_index=issue.get("position_index"),
                message=issue.get("message"),
                level=logging.WARNING,
            )

    async def _record_runtime_snapshot(
        self,
        label: str,
        *,
        capture_kind: str | None,
        position_index: int | None,
        runtime_probe_async: Callable[[], Awaitable[dict[str, Any] | None]] | None,
    ) -> None:
        camilla_status = None
        if runtime_probe_async is not None:
            try:
                camilla_status = await runtime_probe_async()
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "correction_runtime_probe_failed",
                    session=self.session_id,
                    label=label,
                    error=e,
                    level=logging.DEBUG,
                )
        issues = self.runtime_integrity.record_snapshot(
            label,
            capture_kind=capture_kind,
            position_index=position_index,
            camilla_status=camilla_status,
        )
        self._log_runtime_integrity_issues(issues)
        try:
            self._write_runtime_integrity_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle runtime_integrity.json write failed")

    def _record_runtime_capture(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
    ) -> None:
        if self.sweep_meta is None:
            return
        rel_path = self._bundle_relative_path(captured_wav_path)
        issues = self.runtime_integrity.record_capture(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            artifact_path=rel_path,
            expected_sample_rate=self.cfg.sample_rate,
            expected_sweep_samples=self.sweep_meta.n_samples,
            expected_sweep_duration_s=self.sweep_meta.duration_s,
        )
        self._log_runtime_integrity_issues(issues)
        try:
            self._write_runtime_integrity_json(
                extra_dependencies=(rel_path,) if rel_path else (),
            )
        except Exception:  # noqa: BLE001
            logger.exception("bundle runtime_integrity.json write failed")

    def capture_path_for_position(self, idx: int) -> Path:
        """Where a per-position WAV should be written. Falls back to
        cfg.capture_dir when bundles are disabled or the per-session
        dir can't be created — keeps the upload path working even
        when /var/lib/jasper is read-only or full."""
        return self.artifacts.capture_path_for_position(idx)

    def noise_capture_path_for_position(self, idx: int) -> Path:
        """Where the pre-sweep noise WAV for a position should land."""
        return self.artifacts.noise_capture_path_for_position(idx)

    def repeat_capture_path_for_position(
        self,
        idx: int = 0,
        *,
        repeat_index: int = 1,
    ) -> Path:
        """Where optional same-position repeat WAVs should land."""
        return self.artifacts.repeat_capture_path_for_position(
            idx,
            repeat_index=repeat_index,
        )

    def verify_capture_path(self) -> Path:
        """Where the post-Apply re-measurement WAV should land."""
        return self.artifacts.verify_capture_path()

    def _write_info_json(self) -> None:
        """Atomically rewrite info.json with the current session snapshot."""
        self.artifacts.write_info_json()

    def _write_result_json(self) -> None:
        """Snapshot the chart curves + verify after design / verify."""
        self.artifacts.write_result_json()

    def _write_mic_calibration_bundle(self, bundle: Path) -> None:
        self.artifacts.write_mic_calibration_bundle(bundle)

    def _write_position_analysis_json(self) -> None:
        """Persist replayable per-position curves and variance bands."""
        self.artifacts.write_position_analysis_json()

    def _copy_applied_yaml(self) -> None:
        """Copy the just-emitted correction YAML into the bundle."""
        self.artifacts.copy_applied_yaml()

    async def state_changed_from(
        self,
        from_states: SessionState | set[SessionState],
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        """Block until session state is no longer in `from_states`.

        Used by HTTP handlers that kick off background async tasks
        and want to return the *new* state to the client — without
        this wait, the client briefly sees stale pre-transition
        state and (in the case of pollState's needs_next_position /
        applied / verified branches) STOPS POLLING, missing all
        subsequent state changes. The bug surfaced as
        `cannot advance to next position from state awaiting_capture`
        when the user double-tapped Continue after the polling died
        silently.

        Returns True if state changed, False on timeout.
        """
        if isinstance(from_states, SessionState):
            from_states = {from_states}
        else:
            from_states = set(from_states)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if self.state not in from_states:
                return True
            await asyncio.sleep(0.02)
        return False

    async def _fail(self, message: str) -> None:
        self._cancel_capture_timeout()
        self.error = message
        self.state = SessionState.FAILED
        self._emit("error", {"message": message})
        logger.error("session %s failed: %s", self.session_id, message)
        try:
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception(
                "bundle info.json write failed (state=%s)", self.state.value,
            )

    def _ensure_sweep_cache(self) -> tuple[Path, sweep.SweepMeta]:
        """Generate or reuse the cached sweep WAV. Cached on disk
        because the sweep is deterministic per parameter tuple — no
        point regenerating each measurement (saves ~50 ms per
        position)."""
        self.cfg.sweep_dir.mkdir(parents=True, exist_ok=True)
        sweep_path = self.cfg.sweep_dir / (
            f"sweep_{int(self.cfg.f1_hz)}_{int(self.cfg.f2_hz)}_"
            f"{int(self.cfg.duration_s * 1000)}ms_"
            f"{self.cfg.sample_rate}Hz_"
            f"{int(abs(self.cfg.amplitude_dbfs) * 10)}dbm.wav"
        )
        signal, meta = sweep.synchronized_swept_sine(
            f1=self.cfg.f1_hz,
            f2=self.cfg.f2_hz,
            duration_approx_s=self.cfg.duration_s,
            sample_rate=self.cfg.sample_rate,
            amplitude_dbfs=self.cfg.amplitude_dbfs,
        )
        if not sweep_path.exists():
            sweep.write_sweep_wav(sweep_path, signal, self.cfg.sample_rate)
        self.sweep_wav_path = sweep_path
        self.sweep_meta = meta
        return sweep_path, meta

    def _noise_report_dict(
        self,
        noise_wav_path: Path,
        *,
        position_index: int,
    ) -> dict[str, Any]:
        samples, sample_rate = sweep.read_wav_mono(noise_wav_path)
        samples64 = samples.astype(np.float64)
        abs_samples = np.abs(samples64)
        rms = (
            float(np.sqrt(np.mean(samples64 ** 2)))
            if samples64.size
            else 0.0
        )
        peak = float(np.max(abs_samples)) if abs_samples.size else 0.0
        artifact_path: Path | str = noise_wav_path
        if self.bundle_dir is not None:
            try:
                artifact_path = noise_wav_path.relative_to(self.bundle_dir)
            except ValueError:
                pass
        return {
            "capture_kind": "noise",
            "position_index": position_index,
            "artifact_path": str(artifact_path),
            "sample_rate": int(sample_rate),
            "duration_s": round(
                float(samples64.size / sample_rate) if sample_rate > 0 else 0.0,
                3,
            ),
            "rms_dbfs": round(_dbfs(rms), 2),
            "peak_dbfs": round(_dbfs(peak), 2),
            "band_noise_dbfs": _band_levels_dbfs(samples64, sample_rate),
            "method": "pre_sweep_silence_wav",
        }

    def _noise_report_for_position(
        self,
        position_index: int | None,
    ) -> dict[str, Any] | None:
        if position_index is None:
            return None
        for report in reversed(self.noise_reports):
            if report.get("position_index") == position_index:
                return report
        return None

    def _capture_band_snr(
        self,
        captured_wav_path: Path,
        noise_report: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not noise_report:
            return []
        try:
            captured, sample_rate = sweep.read_wav_mono(captured_wav_path)
        except Exception:  # noqa: BLE001
            return []
        capture_levels = _band_levels_dbfs(captured.astype(np.float64), sample_rate)
        noise_by_band = {
            band.get("band_id"): band
            for band in noise_report.get("band_noise_dbfs") or []
            if isinstance(band, dict)
        }
        out: list[dict[str, Any]] = []
        for capture_band in capture_levels:
            band_id = capture_band.get("band_id")
            noise_band = noise_by_band.get(band_id)
            if not noise_band:
                continue
            capture_db = float(capture_band["level_dbfs"])
            noise_db = float(noise_band["level_dbfs"])
            out.append({
                "band_id": band_id,
                "band_hz": capture_band.get("band_hz"),
                "capture_level_dbfs": round(capture_db, 2),
                "noise_level_dbfs": round(noise_db, 2),
                "estimated_snr_db": round(capture_db - noise_db, 2),
                "method": "fft_band_power_difference",
            })
        return out

    def _direct_arrival_report(self, impulse_response: np.ndarray) -> dict[str, Any]:
        ir = np.asarray(impulse_response, dtype=np.float64)
        if ir.ndim != 1 or ir.size < 8:
            return {"available": False, "reason": "impulse response unavailable"}
        peak_index = int(np.argmax(np.abs(ir)))
        pre_end = max(0, peak_index - int(0.002 * self.cfg.sample_rate))
        pre_start = max(0, pre_end - int(0.02 * self.cfg.sample_rate))
        pre = ir[pre_start:pre_end]
        if pre.size < 8:
            return {
                "available": False,
                "reason": "not enough pre-arrival samples before direct peak",
                "direct_peak_index": peak_index,
            }
        floor_rms = float(np.sqrt(np.mean(pre ** 2)))
        direct_peak = float(np.max(np.abs(ir)))
        return {
            "available": True,
            "direct_peak_index": peak_index,
            "direct_peak_dbfs": round(_dbfs(direct_peak), 2),
            "pre_arrival_floor_dbfs": round(_dbfs(floor_rms), 2),
            "direct_to_pre_arrival_db": round(
                _dbfs(direct_peak) - _dbfs(floor_rms),
                2,
            ),
            "pre_arrival_window_ms": [
                round(pre_start / self.cfg.sample_rate * 1000.0, 2),
                round(pre_end / self.cfg.sample_rate * 1000.0, 2),
            ],
        }

    def _repeatability_from_arrays(
        self,
        first: np.ndarray,
        repeat: np.ndarray,
        freqs_hz: np.ndarray,
    ) -> dict[str, Any]:
        """Compare two captures at the same physical mic position."""
        if first.shape != repeat.shape or first.shape != freqs_hz.shape:
            return {
                "available": False,
                "level": "unavailable",
                "reason": "repeat and original curves use different shapes",
            }
        mask = (freqs_hz >= 50.0) & (freqs_hz <= min(350.0, self.cfg.peq_f_high))
        if int(mask.sum()) < 3:
            return {
                "available": False,
                "level": "unavailable",
                "reason": "not enough points in the repeatability band",
            }
        delta = first[mask] - repeat[mask]
        abs_delta = np.abs(delta)
        rms_db = float(np.sqrt(np.mean(delta ** 2)))
        p95_abs_db = float(np.percentile(abs_delta, 95))
        max_abs_db = float(np.max(abs_delta))
        if rms_db <= 1.5 and p95_abs_db <= 3.0:
            level = "high"
        elif rms_db <= 2.5 and p95_abs_db <= 5.0:
            level = "medium"
        else:
            level = "low"
        issues: list[dict[str, Any]] = []
        if level == "low":
            issues.append({
                "code": "repeatability_low",
                "severity": "warn",
                "message": (
                    "same-position repeat capture differs enough to limit "
                    "assertive correction"
                ),
            })
        return {
            "available": True,
            "level": level,
            "band_hz": [50.0, min(350.0, self.cfg.peq_f_high)],
            "metrics": {
                "rms_db": round(rms_db, 2),
                "p95_abs_db": round(p95_abs_db, 2),
                "max_abs_db": round(max_abs_db, 2),
            },
            "issues": issues,
        }

    def _smooth_capture(
        self,
        captured_wav_path: Path,
        *,
        capture_kind: str,
        position_index: int | None,
    ) -> tuple[
        np.ndarray,
        np.ndarray,
        quality.CaptureQuality,
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        """Read capture, assess quality, deconvolve, smooth, log-resample.

        Returns (log_freqs, smoothed_db, capture_quality) for both the
        design positions and the verify pass.
        """
        if self.sweep_meta is None:
            raise RuntimeError(
                "no sweep_meta — flow ordering bug (call _ensure_sweep_cache first)"
            )

        captured, sr = sweep.read_wav_mono(captured_wav_path)
        capture_quality = quality.assess_capture(
            captured,
            sample_rate=sr,
            expected_sample_rate=self.cfg.sample_rate,
            sweep_n_samples=self.sweep_meta.n_samples,
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
        )
        for issue in capture_quality.issues:
            logger.warning(
                "capture_quality session=%s code=%s severity=%s detail=%s",
                self.session_id, issue.code, issue.severity, issue.message,
            )
        if capture_quality.failed:
            raise quality.CaptureQualityError(capture_quality)
        sweep_signal, _ = sweep.synchronized_swept_sine(
            f1=self.sweep_meta.f1,
            f2=self.sweep_meta.f2,
            duration_approx_s=self.sweep_meta.duration_s,
            sample_rate=self.sweep_meta.sample_rate,
            amplitude_dbfs=self.sweep_meta.amplitude_dbfs,
        )
        ir = deconv.deconvolve(
            captured.astype(np.float64),
            sweep_signal.astype(np.float64),
            sample_rate=self.cfg.sample_rate,
        )
        direct_arrival = self._direct_arrival_report(ir)
        freqs, mag_db = deconv.magnitude_response(
            ir,
            self.cfg.sample_rate,
            normalize=False,
        )
        smoothed = analysis.smooth_fractional_octave(freqs, mag_db, fraction=48)
        log_freqs, log_mag = analysis.resample_log(freqs, smoothed)
        if self.mic_calibration is not None:
            log_mag = calibration.apply_calibration_curve(
                log_freqs, log_mag, self.mic_calibration.curve,
            )
        log_mag = analysis.normalize_to_band(
            log_freqs,
            log_mag,
            f_low=ANALYSIS_NORMALIZE_BAND_HZ[0],
            f_high=ANALYSIS_NORMALIZE_BAND_HZ[1],
        )
        replay_artifact_info = self._write_capture_replay_artifacts(
            captured_wav_path,
            capture_kind=capture_kind,
            position_index=position_index,
            ir=ir,
            raw_freqs_hz=freqs,
            raw_magnitude_db=mag_db,
            smoothed_magnitude_db=smoothed,
            log_freqs_hz=log_freqs,
            log_magnitude_db=log_mag,
            direct_arrival=direct_arrival,
        )
        return (
            log_freqs,
            log_mag,
            capture_quality,
            direct_arrival,
            replay_artifact_info,
        )

    def _quality_report_dict(
        self,
        report: quality.CaptureQuality,
        *,
        capture_kind: str,
        captured_wav_path: Path,
        position_index: int | None = None,
        noise_report: dict[str, Any] | None = None,
        direct_arrival: dict[str, Any] | None = None,
        replay_artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out = report.to_dict()
        out["capture_kind"] = capture_kind
        out["position_index"] = position_index
        artifact_path = captured_wav_path
        if self.bundle_dir is not None:
            try:
                artifact_path = captured_wav_path.relative_to(self.bundle_dir)
            except ValueError:
                pass
        out["artifact_path"] = str(artifact_path)
        source_noise_floor = None
        source_method = None
        if noise_report and noise_report.get("rms_dbfs") is not None:
            source_noise_floor = float(noise_report["rms_dbfs"])
            source_method = str(noise_report.get("method") or "noise_capture")
            out["noise_artifact_path"] = noise_report.get("artifact_path")
        elif self.noise_floor_db is not None and np.isfinite(self.noise_floor_db):
            source_noise_floor = float(self.noise_floor_db)
            source_method = "browser_autolevel_scalar"
        if source_noise_floor is not None:
            estimated_snr_db = float(report.rms_dbfs - source_noise_floor)
            out["noise_floor_dbfs"] = round(source_noise_floor, 2)
            out["noise_floor_method"] = source_method
            out["estimated_snr_db"] = round(estimated_snr_db, 2)
            band_snr = self._capture_band_snr(captured_wav_path, noise_report)
            if band_snr:
                out["band_snr"] = band_snr
            if estimated_snr_db < 20.0:
                issues = list(out.get("issues") or [])
                issues.append({
                    "code": "capture_snr_low",
                    "severity": "warn",
                    "message": (
                        "capture is less than 20 dB above the measured "
                        "pre-sweep noise floor"
                    ),
                    "details": {
                        "estimated_snr_db": round(estimated_snr_db, 2),
                        "threshold_db": 20.0,
                    },
                })
                out["issues"] = issues
        if direct_arrival is not None:
            out["direct_arrival"] = direct_arrival
        if replay_artifacts is not None:
            out["replay_artifacts"] = replay_artifacts
        return out

    def _design_target(self, freqs: np.ndarray) -> np.ndarray:
        """Resolve target_choice → dB target curve on `freqs`."""
        return strategy.resolve_target_profile(self.target_choice).curve_db(freqs)

    def _build_confidence_report(self) -> dict[str, Any]:
        return confidence.build_confidence_report(
            total_positions=self.total_positions,
            completed_positions=len(self.position_magnitudes),
            has_mic_calibration=self.mic_calibration is not None,
            input_device=self.input_device,
            capture_quality=self.capture_quality,
            strategy_choice=self.strategy_choice,
            browser_audio_report=self.browser_audio_report,
            runtime_integrity=self.runtime_integrity.summary(),
            repeatability_report=self.repeatability_report,
            position_magnitudes=self.position_magnitudes,
            freqs_hz=self.position_freqs,
            correction_band_hz=(self.cfg.peq_f_low, self.cfg.peq_f_high),
        )

    # ------------------------------------------------------------------
    # Phase 1 / Phase 2 measurement flow.
    # ------------------------------------------------------------------

    async def begin_noise_capture(self) -> None:
        """Ask the browser to record pre-sweep room noise.

        The browser flow uses this before each measurement position so
        the bundle carries a real noise artifact instead of only the
        older autolevel scalar. Direct test/legacy callers may still
        call `prepare_and_play_sweep()` from IDLE.
        """
        async with self._lock:
            valid_states = {SessionState.IDLE, SessionState.NEEDS_NEXT_POSITION}
            if self.state not in valid_states:
                raise RuntimeError(
                    f"cannot start noise capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.NEEDS_NOISE_CAPTURE,
                position=self.current_position,
                total_positions=self.total_positions,
            )

    async def on_noise_capture_uploaded(self, noise_wav_path: Path) -> None:
        """Persist the pre-sweep silence WAV and derive noise floors."""
        async with self._lock:
            if self.state != SessionState.NEEDS_NOISE_CAPTURE:
                raise RuntimeError(
                    f"cannot accept noise capture from state {self.state.value}"
                )
            position_index = self.current_position

        self._record_raw_capture_artifact(
            noise_wav_path,
            capture_kind="noise",
            position_index=position_index,
        )
        report = self._noise_report_dict(
            noise_wav_path,
            position_index=position_index,
        )
        self.noise_reports = [
            r for r in self.noise_reports
            if r.get("position_index") != position_index
        ]
        self.noise_reports.append(report)
        self.noise_floor_db = report.get("rms_dbfs")
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
            self._write_info_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle noise capture artifact write failed")

    async def prepare_and_play_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Single sweep. Used both for position[i] within a multi-
        position flow AND for the Phase 1 single-position-only path.

        Flow: PREPARING → SWEEPING → AWAITING_CAPTURE.

        The caller is responsible for the measurement_window —
        either wrapping a single call (Phase 1 single-position) or
        opening once and calling this multiple times (multi-position).
        """
        async with self._lock:
            valid_states = {
                SessionState.IDLE, SessionState.READY,
                SessionState.APPLIED, SessionState.FAILED,
                SessionState.VERIFIED,
                SessionState.NEEDS_NOISE_CAPTURE,
                SessionState.NEEDS_NEXT_POSITION,
            }
            if self.state not in valid_states:
                raise RuntimeError(
                    f"cannot start sweep from state {self.state.value}"
                )
            await self._set_state(
                SessionState.PREPARING,
                position=self.current_position,
                total_positions=self.total_positions,
            )
            position_index = self.current_position

        await self._record_runtime_snapshot(
            "measurement_prepare",
            capture_kind="measurement",
            position_index=position_index,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, meta = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.SWEEPING,
                duration_s=meta.duration_s,
                position=self.current_position,
                total_positions=self.total_positions,
            )

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "measurement_sweep_start",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "measurement_sweep_complete",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "measurement_sweep_failed",
                capture_kind="measurement",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.AWAITING_CAPTURE,
                position=self.current_position,
                total_positions=self.total_positions,
            )

    async def prepare_and_play_repeat_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """Play an optional repeat sweep at the main seat.

        This uses the same sweep and measurement window as a normal
        position but stores the resulting capture separately so bundle
        recompute does not mistake it for another listening position.
        """
        async with self._lock:
            if self.state != SessionState.NEEDS_REPEAT_CAPTURE:
                raise RuntimeError(
                    f"cannot start repeat sweep from state {self.state.value}"
                )
            position_index = 0
            await self._set_state(
                SessionState.PREPARING,
                position=position_index,
                total_positions=self.total_positions,
            )

        await self._record_runtime_snapshot(
            "repeat_prepare",
            capture_kind="repeat",
            position_index=position_index,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, meta = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.SWEEPING,
                duration_s=meta.duration_s,
                position=position_index,
                total_positions=self.total_positions,
            )

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "repeat_sweep_start",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "repeat_sweep_complete",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "repeat_sweep_failed",
                capture_kind="repeat",
                position_index=position_index,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"repeat sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.AWAITING_REPEAT_CAPTURE,
                position=position_index,
                total_positions=self.total_positions,
            )

    async def on_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Position-N capture arrived. Deconv + smooth + store. If
        more positions remain, transition to NEEDS_NEXT_POSITION.
        Otherwise, spatial-average the per-position magnitudes,
        design PEQs, transition to READY."""
        async with self._lock:
            if self.state != SessionState.AWAITING_CAPTURE:
                raise RuntimeError(
                    f"cannot accept capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.ANALYZING,
                position=self.current_position,
            )
            self.last_capture_path = captured_wav_path
            position_index = self.current_position

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="measurement",
            position_index=position_index,
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="measurement",
            position_index=position_index,
        )
        noise_report = self._noise_report_for_position(position_index)

        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = self._smooth_capture(
                captured_wav_path,
                capture_kind="measurement",
                position_index=position_index,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.capture_quality.append(self._quality_report_dict(
                    e.report,
                    capture_kind="measurement",
                    captured_wav_path=captured_wav_path,
                    position_index=position_index,
                    noise_report=noise_report,
                ))
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"analysis failed: {e}")
            raise
        await self._record_runtime_snapshot(
            "measurement_analysis_complete",
            capture_kind="measurement",
            position_index=position_index,
            runtime_probe_async=None,
        )

        if self.position_freqs is None:
            self.position_freqs = log_freqs
        self.position_magnitudes.append(log_mag)
        self.capture_quality.append(self._quality_report_dict(
            capture_quality,
            capture_kind="measurement",
            captured_wav_path=captured_wav_path,
            position_index=position_index,
            noise_report=noise_report,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        ))
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")
        self.current_position += 1

        if (
            self.repeat_main_position
            and position_index == 0
            and self.repeat_quality is None
        ):
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_REPEAT_CAPTURE,
                    position=0,
                    total_positions=self.total_positions,
                )
            return

        if self.current_position < self.total_positions:
            # Wait for the user to move to the next position.
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_NEXT_POSITION,
                    position=self.current_position,
                    total_positions=self.total_positions,
                )
            return

        # All positions captured. Average + design.
        try:
            self._run_design_from_positions()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"PEQ design failed: {e}")
            raise

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json write failed")

        async with self._lock:
            await self._set_state(
                SessionState.READY,
                peq_count=len(self.peqs),
                positions_used=self.total_positions,
            )

    async def on_repeat_capture_uploaded(
        self,
        captured_wav_path: Path,
    ) -> None:
        """Same-position repeat capture arrived for trust scoring."""
        async with self._lock:
            if self.state != SessionState.AWAITING_REPEAT_CAPTURE:
                raise RuntimeError(
                    f"cannot accept repeat capture from state {self.state.value}"
                )
            await self._set_state(
                SessionState.ANALYZING,
                position=0,
            )
            self.last_capture_path = captured_wav_path

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="repeat",
            position_index=0,
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="repeat",
            position_index=0,
        )
        noise_report = self._noise_report_for_position(0)

        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = self._smooth_capture(
                captured_wav_path,
                capture_kind="repeat",
                position_index=0,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.repeat_quality = self._quality_report_dict(
                    e.report,
                    capture_kind="repeat",
                    captured_wav_path=captured_wav_path,
                    position_index=0,
                    noise_report=noise_report,
                )
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"repeat analysis failed: {e}")
            raise

        await self._record_runtime_snapshot(
            "repeat_analysis_complete",
            capture_kind="repeat",
            position_index=0,
            runtime_probe_async=None,
        )
        self.repeat_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.repeat_quality = self._quality_report_dict(
            capture_quality,
            capture_kind="repeat",
            captured_wav_path=captured_wav_path,
            position_index=0,
            noise_report=noise_report,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        )
        if self.position_freqs is not None and self.position_magnitudes:
            self.repeatability_report = self._repeatability_from_arrays(
                self.position_magnitudes[0],
                log_mag,
                self.position_freqs,
            )
        else:
            self.repeatability_report = {
                "available": False,
                "level": "unavailable",
                "reason": "original main-seat capture is unavailable",
            }
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")

        if self.current_position < self.total_positions:
            async with self._lock:
                await self._set_state(
                    SessionState.NEEDS_NEXT_POSITION,
                    position=self.current_position,
                    total_positions=self.total_positions,
                )
            return

        try:
            self._run_design_from_positions()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"PEQ design failed: {e}")
            raise

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json write failed")

        async with self._lock:
            await self._set_state(
                SessionState.READY,
                peq_count=len(self.peqs),
                positions_used=self.total_positions,
            )

    def _run_design_from_positions(self) -> None:
        """Spatial-average per-position magnitudes, run target lookup,
        run PEQ design, fill measured/target/predicted curves."""
        if not self.position_magnitudes or self.position_freqs is None:
            raise RuntimeError(
                "no position data — run capture first"
            )

        averaged_db = analysis.spatial_average_db(self.position_magnitudes)
        log_freqs = self.position_freqs
        design = strategy.design_correction(
            averaged_db,
            log_freqs,
            target_choice=self.target_choice,
            strategy_choice=self.strategy_choice,
            position_magnitudes=self.position_magnitudes,
        )

        self.measured_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=averaged_db.tolist(),
        )
        self.target_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=design.target_db.tolist(),
        )
        self.predicted_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=design.predicted_db.tolist(),
        )
        self.peqs = [PEQJSON.from_peq(p) for p in design.peqs]
        self.design_report = design.report
        self.confidence_report = self._build_confidence_report()
        self.design_report["confidence_report"] = self.confidence_report
        try:
            self._write_position_analysis_json()
        except Exception:  # noqa: BLE001
            self.position_analysis = None
            logger.exception("bundle position_analysis.json write failed")

    # ------------------------------------------------------------------
    # Apply / reset / verify.
    # ------------------------------------------------------------------

    async def apply(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
        camilla_get_config: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        async with self._lock:
            if self.state != SessionState.READY:
                raise RuntimeError(
                    f"cannot apply from state {self.state.value}"
                )

        try:
            self.cfg.config_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.cfg.config_dir / (
                f"correction_{self.session_id}_{int(self.started_at)}.yml"
            )
            peq_objs = [
                PEQ(freq=p.freq_hz, q=p.q, gain=p.gain_db)
                for p in self.peqs
            ]
            from jasper.multiroom.member_config import member_camilla_kwargs
            from jasper.sound.camilla_yaml import (
                emit_sound_config,
            )
            from jasper.sound.profile import build_sound_filters, load_profile
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"YAML emit failed: {e}")
            raise

        def _prepare_config() -> dict[str, int]:
            profile = load_profile()
            # A bonded member correcting its own seat (/correction) needs the
            # SAME grouping transforms as /sound — inv-5 rate_adjust off + its
            # channel-split. One policy owns the decision (member_camilla_kwargs)
            # so this path can't drift from /sound; solo → unchanged.
            emit_sound_config(
                profile,
                room_peqs=peq_objs,
                out_path=out_path,
                profile_id=self.session_id,
                **member_camilla_kwargs(),
            )
            return {
                "room_peq_count": len(peq_objs),
                "sound_filter_count": len(build_sound_filters(profile)),
            }

        try:
            from jasper.dsp_apply import DspApplyError, apply_dsp_config
            await apply_dsp_config(
                source="correction",
                candidate_path=out_path,
                load_config=camilla_set_config,
                get_current_config_path=camilla_get_config,
                prepare=_prepare_config,
                room_peq_count=len(peq_objs),
            )
            self.config_path = out_path
        except DspApplyError as e:
            if e.state.result == "prepare_failed":
                async with self._lock:
                    await self._fail(f"YAML emit failed: {e}")
                raise
            async with self._lock:
                await self._fail(f"CamillaDSP reload failed: {e}")
            if e.state.load_error == "CamillaDSP rejected candidate config path":
                return
            raise
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"CamillaDSP reload failed: {e}")
            raise

        try:
            self._copy_applied_yaml()
        except Exception:  # noqa: BLE001
            logger.exception("bundle applied.yml copy failed")

        async with self._lock:
            await self._set_state(
                SessionState.APPLIED,
                config_path=str(out_path),
            )

    async def reset(
        self,
        camilla_set_config: Callable[[str], Awaitable[bool]],
    ) -> None:
        async with self._lock:
            if self.state in _RESET_BUSY_STATES:
                raise RuntimeError(
                    f"cannot reset while {self.state.value} — a sweep or "
                    "analysis is in progress; wait for it to finish"
                )
        try:
            ok = await camilla_set_config(str(self.cfg.base_config_path))
            if not ok:
                async with self._lock:
                    await self._fail(
                        "CamillaDSP rejected the base config — manual "
                        "intervention required"
                    )
                return
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"reset reload failed: {e}")
            raise

        async with self._lock:
            await self._set_state(
                SessionState.IDLE,
                rolled_back_to=str(self.cfg.base_config_path),
            )

    async def start_verify_sweep(
        self,
        play_sweep_async: Callable[..., Awaitable[Any]],
        *,
        alsa_device: str | None = None,
        runtime_probe_async: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> None:
        """One-position re-measurement after Apply. The result lands
        in self.verify_curve / self.verify_metrics — overlaid on the
        chart so the user can see the correction's actual effect."""
        async with self._lock:
            if self.state != SessionState.APPLIED and self.state != SessionState.VERIFIED:
                raise RuntimeError(
                    f"cannot verify from state {self.state.value}"
                )
            await self._set_state(SessionState.VERIFYING)

        await self._record_runtime_snapshot(
            "verify_prepare",
            capture_kind="verify",
            position_index=None,
            runtime_probe_async=runtime_probe_async,
        )

        try:
            sweep_wav, _ = self._ensure_sweep_cache()
        except Exception as e:  # noqa: BLE001
            async with self._lock:
                await self._fail(f"sweep generation failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.SWEEPING)

        try:
            kwargs = {"alsa_device": alsa_device} if alsa_device else {}
            await self._record_runtime_snapshot(
                "verify_sweep_start",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
            await play_sweep_async(str(sweep_wav), **kwargs)
            await self._record_runtime_snapshot(
                "verify_sweep_complete",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
        except Exception as e:  # noqa: BLE001
            await self._record_runtime_snapshot(
                "verify_sweep_failed",
                capture_kind="verify",
                position_index=None,
                runtime_probe_async=runtime_probe_async,
            )
            async with self._lock:
                await self._fail(f"verify sweep playback failed: {e}")
            raise

        async with self._lock:
            await self._set_state(SessionState.AWAITING_VERIFY_CAPTURE)

    async def on_verify_capture_uploaded(
        self, captured_wav_path: Path,
    ) -> None:
        """Verify capture arrived. Deconv + smooth, store as
        verify_curve, compute deviation metrics. Transition to
        VERIFIED."""
        async with self._lock:
            if self.state != SessionState.AWAITING_VERIFY_CAPTURE:
                raise RuntimeError(
                    f"cannot accept verify capture from state {self.state.value}"
                )
            await self._set_state(SessionState.ANALYZING)
            self.last_capture_path = captured_wav_path

        self._record_raw_capture_artifact(
            captured_wav_path,
            capture_kind="verify",
        )
        self._record_runtime_capture(
            captured_wav_path,
            capture_kind="verify",
            position_index=None,
        )

        try:
            (
                log_freqs,
                log_mag,
                capture_quality,
                direct_arrival,
                replay_artifact_info,
            ) = self._smooth_capture(
                captured_wav_path,
                capture_kind="verify",
                position_index=None,
            )
        except Exception as e:  # noqa: BLE001
            if isinstance(e, quality.CaptureQualityError):
                self.verify_quality = self._quality_report_dict(
                    e.report,
                    capture_kind="verify",
                    captured_wav_path=captured_wav_path,
                )
                self._refresh_acoustic_quality()
                try:
                    self._write_acoustic_quality_json()
                except Exception:  # noqa: BLE001
                    logger.exception("bundle acoustic_quality.json write failed")
            async with self._lock:
                await self._fail(f"verify analysis failed: {e}")
            raise
        await self._record_runtime_snapshot(
            "verify_analysis_complete",
            capture_kind="verify",
            position_index=None,
            runtime_probe_async=None,
        )

        target_db = self._design_target(log_freqs)
        # Use deviation_metrics' DEFAULT band (50-350 Hz) rather than
        # the PEQ design band (20-350 Hz). Below ~50 Hz the iPhone
        # mic's built-in 24 dB/octave HPF dominates the captured
        # signal — including those frequencies in the deviation
        # summary produces alarming numbers ("max 56 dB!") that are
        # mic artifacts, not room reality. PEQ design still goes
        # down to 20 Hz because the mic captures *enough* there
        # to inform a useful filter, just not enough for a clean
        # deviation-from-target readout.
        metrics = analysis.deviation_metrics(
            log_mag, target_db, log_freqs,
            f_high=self.cfg.peq_f_high,
        )

        self.verify_curve = CurveJSON(
            freqs_hz=log_freqs.tolist(),
            magnitude_db=log_mag.tolist(),
        )
        self.verify_metrics = metrics
        self.verify_quality = self._quality_report_dict(
            capture_quality,
            capture_kind="verify",
            captured_wav_path=captured_wav_path,
            direct_arrival=direct_arrival,
            replay_artifacts=replay_artifact_info,
        )
        self._refresh_acoustic_quality()
        try:
            self._write_acoustic_quality_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle acoustic_quality.json write failed")

        try:
            self._write_result_json()
        except Exception:  # noqa: BLE001
            logger.exception("bundle result.json (verify) write failed")

        async with self._lock:
            await self._set_state(
                SessionState.VERIFIED,
                rms_db=metrics["rms_db"],
                max_db=metrics["max_db"],
            )

    # ------------------------------------------------------------------
    # Auto-level.
    # ------------------------------------------------------------------

    async def run_autolevel(
        self,
        *,
        get_main_volume_db: Callable[[], Awaitable[float]],
        set_main_volume_db: Callable[[float], Awaitable[Any]],
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        start_db: float = -40.0,
        end_db: float | None = None,
        end_db_bump: float = 6.0,
        end_db_absolute_max: float = -6.0,
        end_db_absolute_min: float = -20.0,
        step_db: float = 1.0,
        step_interval_s: float = 0.15,
        safety_timeout_s: float = 25.0,
        fade_down_to_db: float = -40.0,
        fade_step_s: float = 0.03,
    ) -> None:
        """Auto-level CamillaDSP main_volume.

        Ramps main_volume from `start_db` up toward `end_db` while a
        continuous tone plays through the music chain. The client
        (iPhone) watches its mic level via AudioWorklet and either
        auto-locks when the captured level enters the target range,
        OR the user taps a manual "Lock now" button. Either path
        POSTs to `/autolevel/lock`, which sets
        `_autolevel_lock_event` and causes this function to freeze
        main_volume at the current ramp value.

        Three exits:
          - LOCKED:     client signalled lock; main_volume stays at
                        the lock value.
          - MAXED_OUT:  ramp reached end_db without lock — speaker /
                        amp combo too quiet (or iOS Safari is silent-
                        AGC'ing the mic readout, which has happened
                        in the field). main_volume stays at end_db;
                        UI tells user to turn up amp OR use the
                        manual Lock button.
          - CANCELLED:  client called /autolevel/cancel OR safety
                        timeout fired. main_volume restored to
                        `original_main_volume_db`.

        Order of operations matters (a real first-user bug fix):
        we set main_volume to `start_db` BEFORE starting the tone
        so the user doesn't hear an initial blast at their normal
        listening level before the ramp drops them to -40 dB. And
        we fade main_volume back DOWN to `fade_down_to_db` before
        killing the tone, so the stop is silent rather than a click.

        On entry, snapshots current main_volume into
        `self.autolevel.original_main_volume_db` so the
        measurement-workflow apply/reset handlers can restore the
        user's listening volume after the workflow ends.

        Safety — DON'T BLOW THE LISTENER'S EARS OUT:

        Originally end_db was hard-capped at -6 dB. First-user
        report: even -6 dB was still way too loud — their listening
        volume was around -20 dB main_volume; -6 dB is 14 dB louder
        (~4x perceived loudness), painfully blasted them.

        end_db now defaults to None, computed RELATIVE TO the user's
        existing main_volume:

            end_db = clamp(
                original_main_volume_db + end_db_bump,
                [end_db_absolute_min, end_db_absolute_max],
            )

        Defaults give +6 dB bump over normal listening, clamped to
        [-20, -6] dB. So:
          - user at -20 dB → autolevel cap -14 dB (only ~6 dB louder)
          - user at -5 dB  → cap -6 dB (absolute max)
          - user at -45 dB → cap -20 dB (floored UP to a usable
            measurement level)

        Combined with the -12 dBFS tone amplitude (matches the
        sweep), worst-case dongle output at the cap is -18 dBFS —
        far quieter than the prior -6 dBFS tone × -6 dB cap = -12
        dBFS that blasted the user.
        """
        al = self.autolevel = AutolevelData()
        self._autolevel_lock_event = asyncio.Event()
        self._autolevel_cancel_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        tone_task: asyncio.Task | None = None

        async def _graceful_stop(lock_value_db: float | None) -> None:
            """Fade main_volume down to `fade_down_to_db`, then kill
            the tone. Avoids the click that bare proc.kill() would
            produce mid-tone. After the fade, the caller's choice of
            final main_volume (locked / restored) is set."""
            try:
                cur = al.current_main_volume_db
                while cur > fade_down_to_db:
                    cur = max(fade_down_to_db, cur - 2.0)
                    try:
                        await set_main_volume_db(cur)
                    except Exception:  # noqa: BLE001
                        break
                    await asyncio.sleep(fade_step_s)
            finally:
                cancel_tone()
                if tone_task is not None:
                    try:
                        await asyncio.wait_for(tone_task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                # After tone is silenced, set the final main_volume.
                if lock_value_db is not None:
                    try:
                        await set_main_volume_db(lock_value_db)
                        al.current_main_volume_db = lock_value_db
                    except Exception:  # noqa: BLE001
                        pass

        try:
            al.original_main_volume_db = float(await get_main_volume_db())
            # Compute the dynamic cap NOW that we know original.
            if end_db is None:
                end_db = compute_autolevel_cap(
                    al.original_main_volume_db,
                    bump_db=end_db_bump,
                    floor_db=end_db_absolute_min,
                    ceil_db=end_db_absolute_max,
                )
                logger.info(
                    "autolevel: dynamic end_db=%.1f dB "
                    "(original=%.1f + bump=%.1f, clamped to [%.1f, %.1f])",
                    end_db, al.original_main_volume_db, end_db_bump,
                    end_db_absolute_min, end_db_absolute_max,
                )
            al.cap_db = float(end_db)
            al.status = AutolevelStatus.RAMPING
            logger.info(
                "autolevel: START original_main_volume=%.1f dB "
                "(ramp %.1f → %.1f dB, step=%.1f dB/%.0f ms)",
                al.original_main_volume_db, start_db, end_db,
                step_db, step_interval_s * 1000,
            )

            # CRITICAL: set main_volume to the quiet start BEFORE
            # starting the tone. Without this, the tone briefly plays
            # at the user's previous (often loud) listening level
            # before the ramp drops it — a real "menace" complaint
            # from the first-user test.
            current_db = float(start_db)
            await set_main_volume_db(current_db)
            al.current_main_volume_db = current_db
            # Brief settle so CamillaDSP's volume change reaches the
            # output before any tone arrives.
            await asyncio.sleep(0.1)

            tone_task = asyncio.create_task(play_continuous_tone())

            start_time = loop.time()

            while current_db < end_db:
                # Subdivide each ramp step so lock/cancel can
                # respond within ~10 ms instead of waiting out the
                # full step interval.
                interval_end = loop.time() + step_interval_s
                while loop.time() < interval_end:
                    await asyncio.sleep(0.01)
                    if self._autolevel_lock_event.is_set():
                        al.status = AutolevelStatus.LOCKED
                        al.locked_main_volume_db = current_db
                        logger.info(
                            "autolevel: LOCKED at main_volume=%.1f dB "
                            "(elapsed %.2f s)",
                            current_db, loop.time() - start_time,
                        )
                        await _graceful_stop(current_db)
                        return
                    if self._autolevel_cancel_event.is_set():
                        al.status = AutolevelStatus.CANCELLED
                        logger.info(
                            "autolevel: CANCELLED at main_volume=%.1f dB "
                            "(elapsed %.2f s) — restoring to %.1f dB",
                            current_db, loop.time() - start_time,
                            al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return
                    if loop.time() - start_time > safety_timeout_s:
                        al.status = AutolevelStatus.CANCELLED
                        al.error = (
                            f"safety timeout after {safety_timeout_s}s"
                        )
                        logger.warning(
                            "autolevel: SAFETY TIMEOUT at "
                            "main_volume=%.1f dB — restoring to %.1f dB",
                            current_db, al.original_main_volume_db,
                        )
                        await _graceful_stop(al.original_main_volume_db)
                        return

                current_db = min(end_db, current_db + step_db)
                await set_main_volume_db(current_db)
                al.current_main_volume_db = current_db
                logger.debug(
                    "autolevel: step main_volume=%.1f dB", current_db,
                )

            # Ramp completed without lock.
            al.status = AutolevelStatus.MAXED_OUT
            al.locked_main_volume_db = end_db
            logger.info(
                "autolevel: MAXED_OUT at main_volume=%.1f dB "
                "(software cap) — user must turn up amplifier OR "
                "tap manual Lock button next time",
                end_db,
            )
            # Even on MAXED_OUT, fade gracefully. main_volume stays
            # at end_db (so a manual Lock follow-up would work).
            await _graceful_stop(end_db)
        except Exception as e:  # noqa: BLE001
            al.status = AutolevelStatus.ERROR
            al.error = str(e)
            logger.exception("autolevel failed")
            try:
                if al.original_main_volume_db is not None:
                    await _graceful_stop(al.original_main_volume_db)
                else:
                    cancel_tone()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._autolevel_lock_event = None
            self._autolevel_cancel_event = None

    async def lock_autolevel(self) -> bool:
        """Signal the running autolevel task to stop ramping and
        lock at the current main_volume. Returns True if a task was
        running."""
        if self._autolevel_lock_event is None:
            return False
        self._autolevel_lock_event.set()
        return True

    async def cancel_autolevel(self) -> bool:
        """Signal the running autolevel task to abort and restore
        the original main_volume."""
        if self._autolevel_cancel_event is None:
            return False
        self._autolevel_cancel_event.set()
        return True

    # ------------------------------------------------------------------
    # Snapshot.
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "total_positions": self.total_positions,
            "current_position": self.current_position,
            "repeat_main_position": self.repeat_main_position,
            "target_choice": self.target_choice,
            "target_profile": strategy.resolve_target_profile(
                self.target_choice,
            ).to_dict(),
            "strategy_choice": self.strategy_choice,
            "correction_strategy": strategy.resolve_correction_strategy(
                self.strategy_choice,
            ).to_dict(),
            "input_device": self.input_device,
            "mic_calibration": (
                self.mic_calibration.public_metadata()
                if self.mic_calibration
                else None
            ),
            "browser_audio_report": self.browser_audio_report,
            "capture_quality": self.capture_quality,
            "noise_reports": self.noise_reports,
            "repeat_quality": self.repeat_quality,
            "repeatability_report": self.repeatability_report,
            "verify_quality": self.verify_quality,
            "confidence_report": self.confidence_report,
            "acoustic_quality": (
                (self.acoustic_quality or {}).get("summary")
                if self.acoustic_quality
                else None
            ),
            "runtime_integrity": self.runtime_integrity.summary(),
            "position_analysis": self.position_analysis,
            "sweep": (
                self.sweep_meta.to_dict() if self.sweep_meta else None
            ),
            "peqs": [p.__dict__ for p in self.peqs],
            "design_report": self.design_report,
            "config_path": (
                str(self.config_path) if self.config_path else None
            ),
            "verify_metrics": self.verify_metrics,
            "autolevel": self.autolevel.snapshot(),
        }
