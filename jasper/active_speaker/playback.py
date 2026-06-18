"""Playback lifecycle for active-speaker tone plans.

The normal artifact backend never emits audio. Product commissioning routes
continuous tones through the protected active-speaker graph. The only generic
audio backend kept here is an explicit lab ``aplay`` hook pointed at a dedicated
test PCM by environment.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import subprocess
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from ._common import issue as _issue
from .calibration_level import (
    DEFAULT_TEST_LEVEL_DBFS,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
)
from .audible_policy import (
    audible_policy_payload,
    audible_role_allowed,
    audible_role_block_code,
    audible_role_block_message,
)
from jasper.audio_lab import (
    AUDIO_LAB_APLAY_BACKEND,
    AUDIO_LAB_TEST_PCM_ENV,
    AUDIO_LAB_TONE_BACKEND_ENV,
)
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_DEVICE,
)

from .camilla_yaml import _forbidden_playback_token
from .driver_protection import driver_protection_payload, normalise_driver_role
from .safe_playback import (
    floor_audio_confirmed_for_target,
    floor_audio_retry_allowed_for_target,
)
from .tone_plan import (
    DEFAULT_TONE_DURATION_MS,
    MAX_TONE_DURATION_MS,
    MIN_TONE_DURATION_MS,
    TONE_PLAN_KIND,
)

SCHEMA_VERSION = 1
TONE_PLAYBACK_RESULT_KIND = "jts_active_speaker_tone_playback_result"
TONE_PLAYBACK_ARTIFACT_KIND = "jts_active_speaker_tone_playback_artifact"
TONE_BACKEND_STATUS_KIND = "jts_active_speaker_tone_backend_status"
DEFAULT_ARTIFACT_DIR = Path("/var/lib/jasper/active_speaker_tone_artifacts")
DEFAULT_SAMPLE_RATE_HZ = 48_000
MIN_ARTIFACT_SAMPLE_RATE_HZ = 8_000
MAX_ARTIFACT_SAMPLE_RATE_HZ = DEFAULT_SAMPLE_RATE_HZ
MAX_ARTIFACT_CHANNELS = 64
DEFAULT_ARTIFACT_RETENTION = 24
MAX_ARTIFACT_RETENTION = 100
MIN_PLAYBACK_FREQUENCY_HZ = 20.0
MAX_PLAYBACK_FREQUENCY_HZ = 20_000.0
INT16_PEAK = 32767
DEFAULT_APLAY_BINARY = "aplay"
DEFAULT_AUDIO_BACKEND = "wav_artifact"
APLAY_AUDIO_BACKEND = AUDIO_LAB_APLAY_BACKEND
APLAY_BINARY_ENV = "JASPER_APLAY"
APLAY_TIMEOUT_PAD_SEC = 1.0
FORBIDDEN_TEST_PCM_TOKENS = (
    DEFAULT_PLAYBACK_DEVICE,
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    "jasper_out",
    "outputd_content_capture",
    "outputd_active_content_capture",
    "outputd_dac",
)

logger = logging.getLogger(__name__)

AplayRunner = Callable[
    [Sequence[str], float],
    subprocess.CompletedProcess[str],
]


class TonePlaybackBackend(Protocol):
    """Backend seam for current dry-runs and future hardware playback."""

    backend_id: str
    audio_backend: bool

    def start(
        self,
        plan: dict[str, Any],
        *,
        playback_id: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        """Start the backend and return backend-specific result fields."""

    def stop(
        self,
        *,
        playback_id: str | None,
        reason: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        """Stop any backend-owned work."""


def _utc_from_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _now() -> float:
    return time.time()


def _artifact_dir(path: str | Path | None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR")
        or DEFAULT_ARTIFACT_DIR
    )


def _has_blocker(issues: list[dict[str, str]] | tuple[dict[str, str], ...]) -> bool:
    return any(issue.get("severity") == "blocker" for issue in issues)


def _exception_summary(exc: Exception, *, limit: int = 220) -> str:
    detail = str(exc).strip().replace("\n", " ")
    if not detail:
        return ""
    return detail[:limit]


def _finite_float(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _positive_int(value: Any, *, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _bounded_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return min(max(out, lo), hi)


def _bounded_float(value: Any, *, default: float, lo: float, hi: float) -> float:
    out = _finite_float(value, default=default)
    return min(max(out, lo), hi)


def _artifact_retention(value: Any = None) -> int:
    configured = (
        value
        if value is not None
        else os.environ.get("JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_RETENTION")
    )
    return _bounded_int(
        configured,
        default=DEFAULT_ARTIFACT_RETENTION,
        lo=1,
        hi=MAX_ARTIFACT_RETENTION,
    )


def _aplay_runner(
    argv: Sequence[str],
    timeout_sec: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def _forbidden_test_pcm_token(pcm: str) -> str | None:
    """Return a daemon-owned PCM token that must not be used as a test writer."""

    lowered = str(pcm or "").lower()
    for token in FORBIDDEN_TEST_PCM_TOKENS:
        if token.lower() in lowered:
            return token
    return _forbidden_playback_token(str(pcm or ""))


def tone_backend_status(
    env: dict[str, str] | None = None,
    *,
    default_pcm: str | None = None,
) -> dict[str, Any]:
    """Return the current active-speaker tone backend boundary.

    The artifact backend is always available and never emits audio. The aplay
    backend is explicit audio-lab mode: it is considered audio-enabled only
    when the operator selects it and points it at a dedicated lab test PCM.
    Daemon-owned CamillaDSP / outputd lanes are intentionally forbidden: they
    are sinks/readers in the runtime graph, not test-tone injection points.
    """

    source = env if env is not None else os.environ
    requested = str(
        source.get(AUDIO_LAB_TONE_BACKEND_ENV) or DEFAULT_AUDIO_BACKEND
    ).strip()
    requested = requested.lower() or DEFAULT_AUDIO_BACKEND
    audio_backend_requested = requested == APLAY_AUDIO_BACKEND
    pcm = str(source.get(AUDIO_LAB_TEST_PCM_ENV) or default_pcm or "").strip()
    issues: list[dict[str, str]] = []
    if requested not in {
        DEFAULT_AUDIO_BACKEND,
        APLAY_AUDIO_BACKEND,
    }:
        issues.append(
            _issue(
                "blocker",
                "unknown_tone_backend",
                "audio-lab tone backend is not recognized",
            )
        )
    if audio_backend_requested and not pcm:
        issues.append(
            _issue(
                "blocker",
                "test_pcm_required",
                f"{AUDIO_LAB_TEST_PCM_ENV} must name the audio-lab test PCM",
            )
        )
    forbidden_token = _forbidden_test_pcm_token(pcm) if pcm else None
    if audio_backend_requested and forbidden_token is not None:
        logger.warning(
            "event=audio_lab.tone_backend.forbidden_test_pcm "
            "pcm=%r token=%r",
            pcm,
            forbidden_token,
        )
        issues.append(
            _issue(
                "blocker",
                "test_pcm_forbidden_main_lane",
                f"{AUDIO_LAB_TEST_PCM_ENV} targets a daemon-owned audio lane "
                f"('{forbidden_token}'); audible channel tests must use a "
                f"dedicated audio-lab test PCM",
            )
        )
    audio_enabled = audio_backend_requested and bool(pcm) and forbidden_token is None
    if issues:
        status = "blocked"
    elif audio_enabled:
        status = "audio_enabled"
    else:
        status = "artifact_only"
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_BACKEND_STATUS_KIND,
        "status": status,
        "backend": requested
        if requested in {
            DEFAULT_AUDIO_BACKEND,
            APLAY_AUDIO_BACKEND,
        }
        else requested,
        "artifact_backend": DEFAULT_AUDIO_BACKEND,
        "audio_backend": requested if audio_enabled else None,
        "tone_playback_implemented": audio_enabled,
        "audio_enabled": audio_enabled,
        "tone_backend_env": AUDIO_LAB_TONE_BACKEND_ENV,
        "test_pcm_env": AUDIO_LAB_TEST_PCM_ENV,
        "test_pcm": pcm or None,
        "issues": issues,
        "next_step": (
            "Audio-lab tone playback is explicitly enabled."
            if audio_enabled
            else (
                "Artifact verification is available; audio-lab playback "
                f"requires {AUDIO_LAB_TONE_BACKEND_ENV}=aplay and "
                f"{AUDIO_LAB_TEST_PCM_ENV}."
            )
        ),
    }


def enabled_audio_backend(
    *,
    env: dict[str, str] | None = None,
    runner: AplayRunner = _aplay_runner,
    artifact_dir: str | Path | None = None,
    default_pcm: str | None = None,
) -> "AplayTonePlaybackBackend | None":
    """Return the configured audio backend, or ``None`` when not enabled."""

    status = tone_backend_status(
        env,
        default_pcm=default_pcm,
    )
    if not status["audio_enabled"] or status.get("audio_backend") != APLAY_AUDIO_BACKEND:
        return None
    return AplayTonePlaybackBackend(
        pcm=str(status["test_pcm"]),
        runner=runner,
        artifact_dir=artifact_dir,
        aplay_binary=(env or os.environ).get(APLAY_BINARY_ENV) or DEFAULT_APLAY_BINARY,
    )


def _target_output_index(plan: dict[str, Any]) -> int | None:
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    value = target.get("output_index")
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _target_output_indices(plan: dict[str, Any]) -> list[int]:
    indices: list[int] = []
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        try:
            output_index = int(target.get("output_index"))
        except (TypeError, ValueError):
            continue
        if output_index >= 0:
            indices.append(output_index)
    if not indices:
        output_index = _target_output_index(plan)
        if output_index is not None:
            indices.append(output_index)
    return sorted(set(indices))


def _channel_count(plan: dict[str, Any], output_indices: list[int]) -> int:
    highest_output = max(output_indices)
    channel_map = (
        plan.get("channel_map")
        if isinstance(plan.get("channel_map"), dict)
        else {}
    )
    declared = _positive_int(
        channel_map.get("output_count"),
        default=highest_output + 1,
    )
    return max(declared, highest_output + 1)


def _bounded_channel_count(plan: dict[str, Any], output_indices: list[int]) -> int:
    channel_count = _channel_count(plan, output_indices)
    if channel_count > MAX_ARTIFACT_CHANNELS:
        raise ValueError(
            f"tone artifact channel count {channel_count} exceeds "
            f"the no-audio safety cap {MAX_ARTIFACT_CHANNELS}"
        )
    return channel_count


def _validate_plan_for_dry_backend(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    require_safe_session: bool,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    issues.extend(
        issue for issue in plan.get("issues", [])
        if isinstance(issue, dict)
    )
    if require_safe_session and safe_session.get("status") != "armed":
        issues.append(
            _issue(
                "blocker",
                "safe_session_not_armed",
                "active-speaker safe session must be armed and unexpired",
            )
        )
    if plan.get("kind") != TONE_PLAN_KIND:
        issues.append(
            _issue("blocker", "invalid_tone_plan", "tone plan kind is unsupported")
        )
    if plan.get("status") != "ready":
        issues.append(
            _issue("blocker", "tone_plan_not_ready", "tone plan is not ready")
        )
    output_indices = _target_output_indices(plan)
    if not output_indices:
        issues.append(
            _issue(
                "blocker",
                "target_output_missing",
                "tone plan does not identify a target output channel",
            )
        )
        return issues
    channel_count = _channel_count(plan, output_indices)
    if channel_count > MAX_ARTIFACT_CHANNELS:
        issues.append(
            _issue(
                "blocker",
                "too_many_artifact_channels",
                f"tone artifact channel count {channel_count} exceeds "
                f"the no-audio safety cap {MAX_ARTIFACT_CHANNELS}",
            )
        )
    return issues


def _tone_fields(plan: dict[str, Any]) -> dict[str, Any]:
    tone = plan.get("tone") if isinstance(plan.get("tone"), dict) else {}
    waveform = str(tone.get("waveform") or "sine").lower()
    if waveform != "sine":
        waveform = "sine"
    duration_ms = _bounded_int(
        tone.get("duration_ms"),
        default=DEFAULT_TONE_DURATION_MS,
        lo=MIN_TONE_DURATION_MS,
        hi=MAX_TONE_DURATION_MS,
    )
    ramp_ms = _bounded_int(
        tone.get("ramp_ms"),
        default=20,
        lo=0,
        hi=max(0, duration_ms // 2),
    )
    return {
        "waveform": waveform,
        "frequency_hz": _bounded_float(
            tone.get("frequency_hz"),
            default=1000.0,
            lo=MIN_PLAYBACK_FREQUENCY_HZ,
            hi=MAX_PLAYBACK_FREQUENCY_HZ,
        ),
        "level_dbfs": _bounded_float(
            tone.get("level_dbfs"),
            default=DEFAULT_TEST_LEVEL_DBFS,
            lo=MIN_TEST_LEVEL_DBFS,
            hi=MAX_TEST_LEVEL_DBFS,
        ),
        "duration_ms": duration_ms,
        "ramp_ms": ramp_ms,
    }


def _tone_band_limit(plan: dict[str, Any]) -> dict[str, Any] | None:
    tone = plan.get("tone") if isinstance(plan.get("tone"), dict) else {}
    band = tone.get("band_limit")
    return band if isinstance(band, dict) else None


def _plan_driver_protection(plan: dict[str, Any]) -> dict[str, Any] | None:
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    if isinstance(plan.get("driver_protection"), dict):
        return plan["driver_protection"]
    if isinstance(safety.get("driver_protection"), dict):
        return safety["driver_protection"]
    return None


def _driver_protection_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    driver_role = str(target.get("driver_role") or target.get("role") or "")
    plan_protection = _plan_driver_protection(plan)
    matching_plan_protection = (
        plan_protection
        if (
            isinstance(plan_protection, dict)
            and normalise_driver_role(plan_protection.get("role"))
            == normalise_driver_role(driver_role)
        )
        else None
    )
    if (
        normalise_driver_role(driver_role) == "summed"
        and matching_plan_protection
        and "audio_allowed" in matching_plan_protection
    ):
        return dict(matching_plan_protection)
    driver_style = target.get("driver_style")
    if driver_style is None and matching_plan_protection:
        driver_style = matching_plan_protection.get("driver_style")
    return driver_protection_payload(
        driver_role,
        driver_style=driver_style,
        protection_status=(
            matching_plan_protection.get("protection_status")
            if matching_plan_protection
            else None
        ),
        band_limit=_tone_band_limit(plan),
    )


def _tone_at_floor(tone: dict[str, Any]) -> bool:
    return float(tone.get("level_dbfs") or 0.0) <= MIN_TEST_LEVEL_DBFS + 1e-6


def _plan_with_bounded_tone(plan: dict[str, Any], tone: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(plan)
    bounded["tone"] = {
        **(plan.get("tone") if isinstance(plan.get("tone"), dict) else {}),
        **tone,
    }
    return bounded


def _plan_with_output_count(plan: dict[str, Any], channel_count: int) -> dict[str, Any]:
    bounded_count = _bounded_int(
        channel_count,
        default=2,
        lo=1,
        hi=MAX_ARTIFACT_CHANNELS,
    )
    out = dict(plan)
    channel_map = (
        dict(plan.get("channel_map"))
        if isinstance(plan.get("channel_map"), dict)
        else {}
    )
    output_indices = _target_output_indices(plan)
    existing_count = _channel_count(plan, output_indices) if output_indices else 0
    channel_map["output_count"] = max(bounded_count, existing_count)
    out["channel_map"] = channel_map
    return out


def _tone_sample(
    *,
    sample_index: int,
    sample_rate_hz: int,
    frequency_hz: float,
    amplitude: float,
    total_samples: int,
    ramp_samples: int,
) -> int:
    envelope = 1.0
    if ramp_samples > 0:
        envelope = min(
            envelope,
            sample_index / ramp_samples,
            (total_samples - 1 - sample_index) / ramp_samples,
        )
        envelope = max(0.0, envelope)
    sample = math.sin(2.0 * math.pi * frequency_hz * sample_index / sample_rate_hz)
    return int(round(sample * amplitude * envelope * INT16_PEAK))


def _write_multichannel_wav(
    *,
    path: Path,
    plan: dict[str, Any],
    sample_rate_hz: int,
) -> dict[str, Any]:
    sample_rate_hz = _bounded_int(
        sample_rate_hz,
        default=DEFAULT_SAMPLE_RATE_HZ,
        lo=MIN_ARTIFACT_SAMPLE_RATE_HZ,
        hi=MAX_ARTIFACT_SAMPLE_RATE_HZ,
    )
    output_indices = _target_output_indices(plan)
    if not output_indices:
        raise ValueError("target output index is required")
    channel_count = _bounded_channel_count(plan, output_indices)
    tone = _tone_fields(plan)
    duration_ms = tone["duration_ms"]
    sample_count = max(1, int(round(sample_rate_hz * duration_ms / 1000.0)))
    ramp_samples = min(
        sample_count // 2,
        int(round(sample_rate_hz * tone["ramp_ms"] / 1000.0)),
    )
    amplitude = min(1.0, 10 ** (tone["level_dbfs"] / 20.0))
    peak = 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channel_count)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate_hz)
        for i in range(sample_count):
            target_sample = _tone_sample(
                sample_index=i,
                sample_rate_hz=sample_rate_hz,
                frequency_hz=tone["frequency_hz"],
                amplitude=amplitude,
                total_samples=sample_count,
                ramp_samples=ramp_samples,
            )
            peak = max(peak, abs(target_sample))
            frame = [0] * channel_count
            for output_index in output_indices:
                frame[output_index] = target_sample
            wav.writeframesraw(struct.pack("<" + "h" * channel_count, *frame))

    peak_dbfs = -120.0 if peak <= 0 else 20.0 * math.log10(peak / INT16_PEAK)
    return {
        "path": str(path),
        "basename": path.name,
        "sample_rate_hz": sample_rate_hz,
        "sample_format": "pcm_s16le",
        "channel_count": channel_count,
        "target_output_index": output_indices[0],
        "target_output_indices": output_indices,
        "frame_count": sample_count,
        "duration_ms": duration_ms,
        "peak_dbfs": round(peak_dbfs, 1),
    }


def _artifact_group_mtime(paths: list[Path]) -> float:
    mtimes: list[float] = []
    for path in paths:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=0.0)


def _prune_artifacts(artifact_dir: Path, *, keep: int) -> int:
    """Keep the newest bounded artifact sets and remove older generated files."""

    groups: dict[str, list[Path]] = {}
    try:
        candidates = list(artifact_dir.glob("tone_*.*"))
    except OSError:
        return 0
    for path in candidates:
        if path.suffix not in {".wav", ".json"}:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        groups.setdefault(path.stem, []).append(path)

    ordered = sorted(
        groups.values(),
        key=lambda paths: (_artifact_group_mtime(paths), paths[0].stem),
        reverse=True,
    )
    removed = 0
    for paths in ordered[max(1, keep):]:
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _metadata_for_result(
    *,
    playback_id: str,
    plan: dict[str, Any],
    wav: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    tone = _tone_fields(plan)
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    driver_protection = _driver_protection_for_plan(plan)
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAYBACK_ARTIFACT_KIND,
        "playback_id": playback_id,
        "created_at": created_at,
        "audio_emitted": False,
        "target": {
            "side": target.get("side"),
            "driver_role": target.get("driver_role"),
            "output_index": target.get("output_index"),
            "label": target.get("label"),
        },
        "targets": [target for target in targets if isinstance(target, dict)],
        "tone": tone,
        "audible_test": audible_policy_payload(
            target.get("driver_role") or target.get("role"),
            driver_protection=driver_protection,
        ),
        "driver_protection": driver_protection,
        "safety": {
            "protected_startup_loaded": bool(safety.get("protected_startup_loaded")),
            "safe_session_id": safety.get("safe_session_id"),
        },
        "wav": {
            key: value
            for key, value in wav.items()
            if key not in {"path"}
        },
    }


class NullTonePlaybackBackend:
    """Dry backend for tests and control-flow checks."""

    backend_id = "null"
    audio_backend = False

    def start(
        self,
        plan: dict[str, Any],
        *,
        playback_id: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        return {
            "backend": self.backend_id,
            "status": "completed",
            "audio_emitted": False,
            "artifact": None,
        }

    def stop(
        self,
        *,
        playback_id: str | None,
        reason: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        return {
            "backend": self.backend_id,
            "status": "stopped",
            "playback_id": playback_id,
            "reason": reason,
            "audio_emitted": False,
        }


class WavArtifactTonePlaybackBackend:
    """Render a bounded multi-channel WAV artifact without playback."""

    backend_id = "wav_artifact"
    audio_backend = False

    def __init__(
        self,
        *,
        artifact_dir: str | Path | None = None,
        sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
        artifact_retention: int | None = None,
    ) -> None:
        self.artifact_dir = _artifact_dir(artifact_dir)
        self.sample_rate_hz = _bounded_int(
            sample_rate_hz,
            default=DEFAULT_SAMPLE_RATE_HZ,
            lo=MIN_ARTIFACT_SAMPLE_RATE_HZ,
            hi=MAX_ARTIFACT_SAMPLE_RATE_HZ,
        )
        self.artifact_retention = _artifact_retention(artifact_retention)

    def start(
        self,
        plan: dict[str, Any],
        *,
        playback_id: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        created_at = _utc_from_epoch(now_epoch)
        wav_path = self.artifact_dir / f"tone_{playback_id}.wav"
        meta_path = self.artifact_dir / f"tone_{playback_id}.json"
        wav = _write_multichannel_wav(
            path=wav_path,
            plan=plan,
            sample_rate_hz=self.sample_rate_hz,
        )
        metadata = _metadata_for_result(
            playback_id=playback_id,
            plan=plan,
            wav=wav,
            created_at=created_at,
        )
        meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        retention_removed = _prune_artifacts(
            self.artifact_dir,
            keep=self.artifact_retention,
        )
        return {
            "backend": self.backend_id,
            "status": "completed",
            "audio_emitted": False,
            "artifact": {
                "wav_path": wav["path"],
                "wav_basename": wav["basename"],
                "metadata_path": str(meta_path),
                "metadata_basename": meta_path.name,
                "sample_rate_hz": wav["sample_rate_hz"],
                "sample_format": wav["sample_format"],
                "channel_count": wav["channel_count"],
                "target_output_index": wav["target_output_index"],
                "target_output_indices": wav["target_output_indices"],
                "frame_count": wav["frame_count"],
                "duration_ms": wav["duration_ms"],
                "peak_dbfs": wav["peak_dbfs"],
                "retention_keep": self.artifact_retention,
                "retention_removed": retention_removed,
            },
        }

    def stop(
        self,
        *,
        playback_id: str | None,
        reason: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        return {
            "backend": self.backend_id,
            "status": "stopped",
            "playback_id": playback_id,
            "reason": reason,
            "audio_emitted": False,
        }


class AplayTonePlaybackBackend:
    """Play a bounded generated artifact through an explicitly configured PCM."""

    backend_id = APLAY_AUDIO_BACKEND
    audio_backend = True

    def __init__(
        self,
        *,
        pcm: str,
        aplay_binary: str = DEFAULT_APLAY_BINARY,
        runner: AplayRunner = _aplay_runner,
        artifact_dir: str | Path | None = None,
        sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
        artifact_retention: int | None = None,
    ) -> None:
        self.pcm = str(pcm or "").strip()
        if not self.pcm:
            raise ValueError("audio-lab test PCM is required")
        forbidden_token = _forbidden_test_pcm_token(self.pcm)
        if forbidden_token is not None:
            logger.warning(
                "event=audio_lab.tone_backend.forbidden_test_pcm "
                "pcm=%r token=%r",
                self.pcm,
                forbidden_token,
            )
            raise ValueError(
                f"audio-lab test PCM '{self.pcm}' targets a daemon-owned "
                f"audio lane ('{forbidden_token}'); audible channel tests must "
                f"use a dedicated audio-lab PCM"
            )
        self.aplay_binary = str(aplay_binary or DEFAULT_APLAY_BINARY)
        self.runner = runner
        self.artifact_backend = WavArtifactTonePlaybackBackend(
            artifact_dir=artifact_dir,
            sample_rate_hz=sample_rate_hz,
            artifact_retention=artifact_retention,
        )

    def start(
        self,
        plan: dict[str, Any],
        *,
        playback_id: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        artifact_result = self.artifact_backend.start(
            plan,
            playback_id=playback_id,
            now_epoch=now_epoch,
        )
        artifact = artifact_result.get("artifact") or {}
        wav_path = str(artifact.get("wav_path") or "")
        if not wav_path:
            raise RuntimeError("tone artifact was not generated")
        duration_sec = (
            _bounded_int(
                artifact.get("duration_ms"),
                default=DEFAULT_TONE_DURATION_MS,
                lo=MIN_TONE_DURATION_MS,
                hi=MAX_TONE_DURATION_MS,
            )
            / 1000.0
        )
        argv = [self.aplay_binary, "-q", "-D", self.pcm, wav_path]
        completed = self.runner(argv, duration_sec + APLAY_TIMEOUT_PAD_SEC)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip().splitlines()
            detail = stderr[0][:160] if stderr else f"exit {completed.returncode}"
            raise RuntimeError(f"aplay failed: {detail}")
        return {
            "backend": self.backend_id,
            "status": "completed",
            "audio_emitted": True,
            "audio_device": {
                "pcm": self.pcm,
                "command": Path(self.aplay_binary).name,
            },
            "artifact": artifact,
        }

    def stop(
        self,
        *,
        playback_id: str | None,
        reason: str,
        now_epoch: float,
    ) -> dict[str, Any]:
        return {
            "backend": self.backend_id,
            "status": "stopped",
            "playback_id": playback_id,
            "reason": reason,
            "audio_emitted": False,
        }


def start_tone_playback(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    backend: TonePlaybackBackend | None = None,
    allow_audio: bool = False,
    now: Any = _now,
) -> dict[str, Any]:
    """Run a tone plan through a bounded playback backend."""

    now_epoch = float(now())
    playback_id = uuid.uuid4().hex
    selected = backend or WavArtifactTonePlaybackBackend()
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    tone = _tone_fields(plan)
    bounded_plan = _plan_with_bounded_tone(plan, tone)
    audio_backend = bool(getattr(selected, "audio_backend", False))
    requires_protected_startup = bool(
        getattr(selected, "requires_protected_startup", True)
    )
    issues = _validate_plan_for_dry_backend(
        plan,
        safe_session=safe_session,
        require_safe_session=audio_backend or allow_audio,
    )
    driver_role = str(target.get("driver_role") or target.get("role") or "")
    driver_protection = _driver_protection_for_plan(plan)
    audible_policy = audible_policy_payload(
        driver_role,
        driver_protection=driver_protection,
    )
    if audio_backend and not allow_audio:
        issues.append(
            _issue(
                "blocker",
                "audio_playback_not_authorized",
                "audible channel tests require an explicit per-request authorization",
            )
        )
    if audio_backend and not plan.get("playback_allowed"):
        issues.append(
            _issue(
                "blocker",
                "playback_not_allowed_by_readiness",
                "readiness gates did not authorize audible playback for this target",
            )
        )
    if (
        audio_backend
        and requires_protected_startup
        and not safety.get("protected_startup_loaded")
    ):
        issues.append(
            _issue(
                "blocker",
                "protected_startup_config_not_loaded",
                (
                    "audible channel tests require the protected startup DSP "
                    "to be loaded and current"
                ),
            )
        )
    if audio_backend and not audible_role_allowed(
        driver_role,
        driver_protection=driver_protection,
    ):
        issues.append(
            _issue(
                "blocker",
                audible_role_block_code(driver_role),
                audible_role_block_message(driver_role),
            )
        )
    if audio_backend:
        for issue in driver_protection.get("issues", []):
            if isinstance(issue, dict):
                issues.append(issue)
        max_auto_level = driver_protection.get("max_auto_level_dbfs")
        try:
            max_auto_level = float(max_auto_level)
        except (TypeError, ValueError):
            max_auto_level = MAX_TEST_LEVEL_DBFS
        if tone["level_dbfs"] > max_auto_level + 1e-6:
            issues.append(
                _issue(
                    "blocker",
                    "driver_auto_level_cap_exceeded",
                    "tone level exceeds the driver-specific closed-loop cap",
                )
            )
    if (
        audio_backend
        and not _tone_at_floor(tone)
        and not floor_audio_confirmed_for_target(safe_session, target)
        and not floor_audio_retry_allowed_for_target(safe_session, target)
    ):
        issues.append(
            _issue(
                "blocker",
                "floor_audio_not_confirmed",
                (
                    "audible channel tests above the calibration floor require "
                    "a successful floor-level audible test for the same target "
                    "and safety session"
                ),
            )
        )
    if issues:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": TONE_PLAYBACK_RESULT_KIND,
            "status": "blocked",
            "backend": selected.backend_id if audio_backend else None,
            "playback_id": playback_id,
            "created_at": _utc_from_epoch(now_epoch),
            "audio_emitted": False,
            "confirmable": False,
            "target": {
                "side": target.get("side"),
                "speaker_group_id": target.get("speaker_group_id"),
                "role": target.get("role"),
                "driver_role": target.get("driver_role"),
                "output_index": target.get("output_index"),
                "label": target.get("label"),
            },
            "targets": [item for item in targets if isinstance(item, dict)],
            "tone": tone,
            "audible_test": audible_policy,
            "driver_protection": driver_protection,
            "artifact": None,
            "issues": issues,
        }

    try:
        backend_result = selected.start(
            bounded_plan,
            playback_id=playback_id,
            now_epoch=now_epoch,
        )
    except Exception as exc:  # noqa: BLE001
        detail = _exception_summary(exc)
        message = (
            "tone playback backend failed; successful audio emission "
            f"was not confirmed: {type(exc).__name__}"
        )
        if detail:
            message = f"{message}: {detail}"
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": TONE_PLAYBACK_RESULT_KIND,
            "status": "failed",
            "backend": selected.backend_id,
            "playback_id": playback_id,
            "created_at": _utc_from_epoch(now_epoch),
            "audio_emitted": False,
            "confirmable": False,
            "target": {
                "side": target.get("side"),
                "speaker_group_id": target.get("speaker_group_id"),
                "role": target.get("role"),
                "driver_role": target.get("driver_role"),
                "output_index": target.get("output_index"),
                "label": target.get("label"),
            },
            "targets": [item for item in targets if isinstance(item, dict)],
            "tone": tone,
            "audible_test": audible_policy,
            "driver_protection": driver_protection,
            "artifact": None,
            "backend_error": {
                "type": type(exc).__name__,
                "message": detail or None,
            },
            "issues": [
                _issue(
                    "blocker",
                    "tone_backend_failed",
                    message,
                )
            ],
        }
    result_issues = [
        issue for issue in backend_result.get("issues", [])
        if isinstance(issue, dict)
    ]
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAYBACK_RESULT_KIND,
        "status": str(backend_result.get("status") or "completed"),
        "backend": selected.backend_id,
        "playback_id": playback_id,
        "created_at": _utc_from_epoch(now_epoch),
        "audio_emitted": bool(backend_result.get("audio_emitted")),
        "confirmable": (
            bool(backend_result.get("audio_emitted"))
            and bool(backend_result.get("confirmable", True))
            and not _has_blocker(result_issues)
        ),
        "audio_device": backend_result.get("audio_device"),
        "target": {
            "side": target.get("side"),
            "speaker_group_id": target.get("speaker_group_id"),
            "role": target.get("role"),
            "driver_role": target.get("driver_role"),
            "output_index": target.get("output_index"),
            "label": target.get("label"),
        },
        "targets": [item for item in targets if isinstance(item, dict)],
        "tone": tone,
        "audible_test": audible_policy,
        "driver_protection": driver_protection,
        "artifact": backend_result.get("artifact"),
        "issues": result_issues,
    }


def stop_tone_playback(
    *,
    playback_id: str | None = None,
    reason: str = "operator_stop",
    backend: TonePlaybackBackend | None = None,
    now: Any = _now,
) -> dict[str, Any]:
    """Stop backend-owned playback work.

    Current backends do not hold audio devices or background processes; this is
    still useful as the stable stop contract that the real backend will inherit.
    """

    now_epoch = float(now())
    selected = backend or NullTonePlaybackBackend()
    stopped = selected.stop(
        playback_id=playback_id,
        reason=reason or "operator_stop",
        now_epoch=now_epoch,
    )
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAYBACK_RESULT_KIND,
        "status": "stopped",
        "backend": selected.backend_id,
        "playback_id": playback_id,
        "created_at": _utc_from_epoch(now_epoch),
        "audio_emitted": bool(stopped.get("audio_emitted")),
        "target": None,
        "tone": None,
        "artifact": None,
        "issues": [],
        "reason": stopped.get("reason") or reason or "operator_stop",
    }
