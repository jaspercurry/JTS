"""No-audio playback lifecycle for active-speaker tone plans.

This module is the seam a future real playback backend must implement. The
current backends deliberately do not open ALSA devices, reload CamillaDSP, or
emit sound. They either simulate completion or render a bounded multi-channel
WAV artifact that can be inspected before hardware playback exists.
"""

from __future__ import annotations

import json
import math
import os
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Protocol

from .calibration_level import (
    DEFAULT_TEST_LEVEL_DBFS,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
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
DEFAULT_ARTIFACT_DIR = Path("/var/lib/jasper/active_speaker_tone_artifacts")
DEFAULT_SAMPLE_RATE_HZ = 48_000
MIN_ARTIFACT_SAMPLE_RATE_HZ = 8_000
MAX_ARTIFACT_SAMPLE_RATE_HZ = DEFAULT_SAMPLE_RATE_HZ
MAX_ARTIFACT_CHANNELS = 16
DEFAULT_ARTIFACT_RETENTION = 24
MAX_ARTIFACT_RETENTION = 100
MIN_PLAYBACK_FREQUENCY_HZ = 20.0
MAX_PLAYBACK_FREQUENCY_HZ = 20_000.0
INT16_PEAK = 32767


class TonePlaybackBackend(Protocol):
    """Backend seam for current dry-runs and future hardware playback."""

    backend_id: str

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


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


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


def _target_output_index(plan: dict[str, Any]) -> int | None:
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    value = target.get("output_index")
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _channel_count(plan: dict[str, Any], output_index: int) -> int:
    channel_map = (
        plan.get("channel_map")
        if isinstance(plan.get("channel_map"), dict)
        else {}
    )
    declared = _positive_int(channel_map.get("output_count"), default=output_index + 1)
    return max(declared, output_index + 1)


def _bounded_channel_count(plan: dict[str, Any], output_index: int) -> int:
    channel_count = _channel_count(plan, output_index)
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
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if safe_session.get("status") != "armed":
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
    output_index = _target_output_index(plan)
    if output_index is None:
        issues.append(
            _issue(
                "blocker",
                "target_output_missing",
                "tone plan does not identify a target output channel",
            )
        )
        return issues
    channel_count = _channel_count(plan, output_index)
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


def _plan_with_bounded_tone(plan: dict[str, Any], tone: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(plan)
    bounded["tone"] = {
        **(plan.get("tone") if isinstance(plan.get("tone"), dict) else {}),
        **tone,
    }
    return bounded


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
    output_index = _target_output_index(plan)
    if output_index is None:
        raise ValueError("target output index is required")
    channel_count = _bounded_channel_count(plan, output_index)
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
            frame[output_index] = target_sample
            wav.writeframesraw(struct.pack("<" + "h" * channel_count, *frame))

    peak_dbfs = -120.0 if peak <= 0 else 20.0 * math.log10(peak / INT16_PEAK)
    return {
        "path": str(path),
        "basename": path.name,
        "sample_rate_hz": sample_rate_hz,
        "sample_format": "pcm_s16le",
        "channel_count": channel_count,
        "target_output_index": output_index,
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
    tone = _tone_fields(plan)
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
        "tone": tone,
        "wav": {
            key: value
            for key, value in wav.items()
            if key not in {"path"}
        },
    }


class NullTonePlaybackBackend:
    """Dry backend for tests and control-flow checks."""

    backend_id = "null"

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


def start_tone_playback(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    backend: TonePlaybackBackend | None = None,
    now: Any = _now,
) -> dict[str, Any]:
    """Run a tone plan through a no-audio playback backend."""

    now_epoch = float(now())
    playback_id = uuid.uuid4().hex
    issues = _validate_plan_for_dry_backend(plan, safe_session=safe_session)
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    tone = _tone_fields(plan)
    bounded_plan = _plan_with_bounded_tone(plan, tone)
    if issues:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": TONE_PLAYBACK_RESULT_KIND,
            "status": "blocked",
            "backend": None,
            "playback_id": playback_id,
            "created_at": _utc_from_epoch(now_epoch),
            "audio_emitted": False,
            "target": {
                "side": target.get("side"),
                "driver_role": target.get("driver_role"),
                "output_index": target.get("output_index"),
                "label": target.get("label"),
            },
            "tone": tone,
            "artifact": None,
            "issues": issues,
        }

    selected = backend or WavArtifactTonePlaybackBackend()
    try:
        backend_result = selected.start(
            bounded_plan,
            playback_id=playback_id,
            now_epoch=now_epoch,
        )
    except Exception as exc:
        return {
            "artifact_schema_version": SCHEMA_VERSION,
            "kind": TONE_PLAYBACK_RESULT_KIND,
            "status": "failed",
            "backend": selected.backend_id,
            "playback_id": playback_id,
            "created_at": _utc_from_epoch(now_epoch),
            "audio_emitted": False,
            "target": {
                "side": target.get("side"),
                "driver_role": target.get("driver_role"),
                "output_index": target.get("output_index"),
                "label": target.get("label"),
            },
            "tone": tone,
            "artifact": None,
            "issues": [
                _issue(
                    "blocker",
                    "tone_backend_failed",
                    f"tone playback backend failed before emitting audio: "
                    f"{type(exc).__name__}",
                )
            ],
        }
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": TONE_PLAYBACK_RESULT_KIND,
        "status": str(backend_result.get("status") or "completed"),
        "backend": selected.backend_id,
        "playback_id": playback_id,
        "created_at": _utc_from_epoch(now_epoch),
        "audio_emitted": bool(backend_result.get("audio_emitted")),
        "target": {
            "side": target.get("side"),
            "driver_role": target.get("driver_role"),
            "output_index": target.get("output_index"),
            "label": target.get("label"),
        },
        "tone": tone,
        "artifact": backend_result.get("artifact"),
        "issues": [],
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
