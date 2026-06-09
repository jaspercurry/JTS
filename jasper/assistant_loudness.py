"""Provider-agnostic assistant loudness profiles.

The live voice providers do not emit audio at the same perceived level.
This module gives Python two small responsibilities:

* maintain a persisted source-loudness profile per provider/model/voice;
* measure the actual assistant PCM we send to outputd so profiles improve
  from real live replies, not only from a synthetic test phrase.

Final gain policy stays in the active TTS IPC owner. In the packaged
topology that is jasper-fanin, so TTS/cues enter before CamillaDSP.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_PATH = "/var/lib/jasper/assistant_loudness_profiles.json"
PROFILE_VERSION = 1
CALIBRATION_TEXT = "This is me talking normally."

INPUT_RATE = 24_000
MEASURE_RATE = 48_000
FULL_SCALE = 32768.0
FULL_SCALE_SQ = FULL_SCALE * FULL_SCALE
BS1770_OFFSET_DB = -0.691
ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0
BLOCK_SEC = 0.400
BLOCK_STEP_SEC = 0.100
MIN_VOICED_SEC = 0.30
MAX_PROFILE_AUDIO_SEC = 30.0
_PROFILE_LOCK = threading.RLock()


@dataclass(frozen=True)
class LoudnessMeasurement:
    source_lufs: float
    source_peak_dbfs: float
    voiced_duration_sec: float
    total_duration_sec: float


@dataclass(frozen=True)
class AssistantLoudnessProfile:
    provider: str
    model: str
    voice: str
    source_lufs: float
    source_peak_dbfs: float
    confidence: float
    updated_at: str
    method: str
    sample_rate: int = MEASURE_RATE
    phrase_hash: str = ""
    version: int = PROFILE_VERSION


def active_voice_identity(cfg: Any) -> tuple[str, str, str]:
    """Return the active provider/model/voice tuple from Config-like cfg."""
    provider = getattr(cfg, "voice_provider", "")
    if provider == "openai":
        return provider, getattr(cfg, "openai_model", ""), getattr(cfg, "openai_voice", "")
    if provider == "gemini":
        return provider, getattr(cfg, "gemini_model", ""), getattr(cfg, "gemini_voice", "")
    if provider == "grok":
        return provider, getattr(cfg, "grok_model", ""), getattr(cfg, "grok_voice", "")
    return provider, "", ""


def silence_target_lufs_for_level(level: int | float | None) -> float:
    """Map user listening level to a conservative quiet-room target.

    This only applies when outputd has no content loudness to anchor on.
    With music playing, outputd uses the measured content baseline instead.
    """
    try:
        pct = float(level)
    except (TypeError, ValueError):
        pct = 50.0
    pct = max(0.0, min(100.0, pct))
    return -54.0 + (26.0 * pct / 100.0)


def load_profile(
    provider: str,
    model: str,
    voice: str,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
) -> AssistantLoudnessProfile | None:
    data = _load_payload(path)
    for item in data.get("profiles", []):
        if not isinstance(item, dict):
            continue
        if (
            item.get("provider") == provider
            and item.get("model") == model
            and item.get("voice") == voice
        ):
            return _profile_from_mapping(item)
    return None


def profile_for_outputd(
    provider: str,
    model: str,
    voice: str,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
) -> AssistantLoudnessProfile | None:
    profile = load_profile(provider, model, voice, path=path)
    if profile is None:
        return None
    if profile.source_lufs >= 0.0 or profile.source_peak_dbfs > 0.0:
        return None
    return profile


def save_profile(
    profile: AssistantLoudnessProfile,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
) -> None:
    with _PROFILE_LOCK:
        _save_profile_unlocked(profile, path=path)


def _save_profile_unlocked(
    profile: AssistantLoudnessProfile,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
) -> None:
    payload = _load_payload(path)
    profiles = [
        item for item in payload.get("profiles", [])
        if not (
            isinstance(item, dict)
            and item.get("provider") == profile.provider
            and item.get("model") == profile.model
            and item.get("voice") == profile.voice
        )
    ]
    profiles.append(asdict(profile))
    profiles.sort(key=lambda item: (
        str(item.get("provider", "")),
        str(item.get("model", "")),
        str(item.get("voice", "")),
    ))
    _write_payload({"version": PROFILE_VERSION, "profiles": profiles}, path)


def update_profile_from_measurement(
    provider: str,
    model: str,
    voice: str,
    measurement: LoudnessMeasurement,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
    method: str,
    confidence: float,
    phrase: str = "",
) -> AssistantLoudnessProfile:
    with _PROFILE_LOCK:
        existing = load_profile(provider, model, voice, path=path)
        confidence = max(0.0, min(1.0, float(confidence)))
        source_lufs = measurement.source_lufs
        source_peak = measurement.source_peak_dbfs
        if existing is not None:
            old_w = max(0.10, existing.confidence)
            new_w = max(0.10, confidence)
            # Smooth loudness in the energy domain. This avoids one short
            # phrase yanking the profile around while still letting actual
            # live speech quickly overtake the synthetic seed.
            old_e = 10.0 ** (existing.source_lufs / 10.0)
            new_e = 10.0 ** (measurement.source_lufs / 10.0)
            source_lufs = 10.0 * math.log10(
                ((old_e * old_w) + (new_e * new_w)) / (old_w + new_w)
            )
            source_peak = max(existing.source_peak_dbfs, measurement.source_peak_dbfs)
            confidence = max(existing.confidence, confidence)
        profile = AssistantLoudnessProfile(
            provider=provider,
            model=model,
            voice=voice,
            source_lufs=round(float(source_lufs), 2),
            source_peak_dbfs=round(float(source_peak), 2),
            confidence=round(confidence, 2),
            updated_at=_now_iso(),
            method=method,
            phrase_hash=_phrase_hash(phrase) if phrase else "",
        )
        _save_profile_unlocked(profile, path=path)
    logger.info(
        "event=assistant_loudness.profile_saved provider=%s model=%s "
        "voice=%s method=%s source_lufs=%.1f peak_dbfs=%.1f confidence=%.2f",
        provider, model, voice, method,
        profile.source_lufs, profile.source_peak_dbfs, profile.confidence,
    )
    return profile


class AssistantSourceMeter:
    """Bounded accumulator for one assistant response segment."""

    def __init__(self, *, max_audio_sec: float = MAX_PROFILE_AUDIO_SEC) -> None:
        max_bytes = int(INPUT_RATE * 2 * max_audio_sec)
        self._max_bytes = max(0, max_bytes)
        self._buf = bytearray()
        self._truncated = False

    def observe_pcm_24k(self, pcm: bytes) -> None:
        if not pcm or self._max_bytes <= 0:
            return
        remaining = self._max_bytes - len(self._buf)
        if remaining <= 0:
            self._truncated = True
            return
        self._buf.extend(pcm[:remaining])
        if len(pcm) > remaining:
            self._truncated = True

    def finish(self) -> LoudnessMeasurement | None:
        if not self._buf:
            return None
        try:
            measurement = measure_pcm_24k_mono(bytes(self._buf))
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant loudness source measurement failed: %s", e)
            return None
        if measurement.voiced_duration_sec < MIN_VOICED_SEC:
            return None
        return measurement

    @property
    def truncated(self) -> bool:
        return self._truncated


def confidence_for_measurement(
    measurement: LoudnessMeasurement,
    *,
    seed: bool = False,
) -> float:
    duration_score = max(0.0, min(1.0, measurement.voiced_duration_sec / 1.5))
    peak_penalty = 0.20 if measurement.source_peak_dbfs > -0.5 else 0.0
    confidence = max(0.25, duration_score - peak_penalty)
    if seed:
        confidence = min(confidence, 0.65)
    return round(confidence, 2)


def ensure_seed_profile(
    cfg: Any,
    *,
    path: str | os.PathLike[str] = DEFAULT_PROFILE_PATH,
    force: bool = False,
    max_attempts: int | None = None,
    retry_backoff_sec: float | None = None,
) -> AssistantLoudnessProfile | None:
    """Best-effort silent calibration using the active provider's TTS API.

    This seeds the profile before real replies have been observed. The
    passive live-reply meter will refine it later.
    """
    provider, model, voice = active_voice_identity(cfg)
    if not provider or not model or not voice:
        return None
    existing = load_profile(provider, model, voice, path=path)
    if (
        existing is not None
        and existing.source_lufs < 0.0
        and existing.confidence >= 0.60
        and not force
    ):
        return existing
    backend = _build_active_seed_backend(
        cfg,
        max_attempts=max_attempts,
        retry_backoff_sec=retry_backoff_sec,
    )
    if backend is None:
        return None
    logger.info(
        "event=assistant_loudness.seed_start provider=%s model=%s voice=%s",
        provider, model, voice,
    )
    result = backend.synthesise(CALIBRATION_TEXT)
    measurement = measure_pcm_24k_mono(result.pcm_24k)
    confidence = confidence_for_measurement(measurement, seed=True)
    return update_profile_from_measurement(
        provider,
        model,
        voice,
        measurement,
        path=path,
        method="seed_tts",
        confidence=confidence,
        phrase=CALIBRATION_TEXT,
    )


def measure_pcm_24k_mono(pcm: bytes) -> LoudnessMeasurement:
    import numpy as np

    mono = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if mono.size == 0:
        raise ValueError("empty PCM")
    if mono.size % 2:
        mono = mono[:-1]
    upsampled = _upsample_24k_to_48k(mono)
    weighted = _k_weighted_stereo_energy(upsampled)
    lufs, voiced_frames = _gated_lufs(weighted, MEASURE_RATE)
    peak = float(np.max(np.abs(upsampled))) / FULL_SCALE
    peak_dbfs = -120.0 if peak <= 0.0 else 20.0 * math.log10(peak)
    return LoudnessMeasurement(
        source_lufs=round(lufs, 2),
        source_peak_dbfs=round(max(-120.0, peak_dbfs), 2),
        voiced_duration_sec=round(voiced_frames / MEASURE_RATE, 3),
        total_duration_sec=round(len(upsampled) / MEASURE_RATE, 3),
    )


def _upsample_24k_to_48k(samples: "Any") -> "Any":
    # Production TtsPlayout uses scipy.resample_poly. For tests and
    # lightweight developer environments without scipy, linear
    # interpolation is close enough for a loudness profile seed.
    try:
        from scipy.signal import resample_poly  # type: ignore
    except Exception:  # noqa: BLE001
        import numpy as np
        if samples.size <= 1:
            return np.repeat(samples, 2)
        out = np.empty(samples.size * 2, dtype=np.float64)
        out[0::2] = samples
        out[1:-1:2] = (samples[:-1] + samples[1:]) * 0.5
        out[-1] = samples[-1]
        return out
    return resample_poly(samples, up=2, down=1).astype("float64")


def _k_weighted_stereo_energy(samples_48k: "Any") -> "Any":
    import numpy as np

    pre = _biquad(
        samples_48k,
        1.53512485958697,
        -2.69169618940638,
        1.19839281085285,
        -1.69065929318241,
        0.73248077421585,
    )
    rlb = _biquad(
        pre,
        1.0,
        -2.0,
        1.0,
        -1.99004745483398,
        0.99007225036621,
    )
    # Outputd measures duplicated mono as stereo by summing both channel
    # energies per frame, then dividing by frame count.
    return 2.0 * np.square(rlb)


def _biquad(
    x: "Any",
    b0: float,
    b1: float,
    b2: float,
    a1: float,
    a2: float,
) -> "Any":
    import numpy as np

    y = np.empty_like(x, dtype=np.float64)
    x1 = x2 = y1 = y2 = 0.0
    for i, x0 in enumerate(x):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[i] = y0
        x2 = x1
        x1 = float(x0)
        y2 = y1
        y1 = float(y0)
    return y


def _gated_lufs(energy_per_frame: "Any", sample_rate: int) -> tuple[float, int]:
    import numpy as np

    frames = int(len(energy_per_frame))
    if frames == 0:
        raise ValueError("empty weighted PCM")
    block = max(1, int(sample_rate * BLOCK_SEC))
    step = max(1, int(sample_rate * BLOCK_STEP_SEC))
    block_energies: list[tuple[float, int]] = []
    if frames < block:
        block_energies.append((float(np.sum(energy_per_frame)), frames))
    else:
        for start in range(0, frames - block + 1, step):
            chunk = energy_per_frame[start:start + block]
            block_energies.append((float(np.sum(chunk)), block))
    absolute = [
        (energy, count) for energy, count in block_energies
        if _lufs_from_energy(energy, count) >= ABSOLUTE_GATE_LUFS
    ]
    if not absolute:
        raise ValueError("PCM contains no voiced audio above loudness gate")
    ungated_lufs = _lufs_from_energy(
        sum(energy for energy, _ in absolute),
        sum(count for _, count in absolute),
    )
    relative_gate = ungated_lufs + RELATIVE_GATE_LU
    gated = [
        (energy, count) for energy, count in absolute
        if _lufs_from_energy(energy, count) >= relative_gate
    ] or absolute
    total_energy = sum(energy for energy, _ in gated)
    total_frames = sum(count for _, count in gated)
    return _lufs_from_energy(total_energy, total_frames), total_frames


def _lufs_from_energy(energy: float, frames: int) -> float:
    if frames <= 0 or energy <= 0.0 or not math.isfinite(energy):
        return -120.0
    relative = (energy / float(frames)) / FULL_SCALE_SQ
    if relative <= 0.0 or not math.isfinite(relative):
        return -120.0
    return BS1770_OFFSET_DB + 10.0 * math.log10(relative)


def _seed_retry_kwargs(
    *,
    max_attempts: int | None,
    retry_backoff_sec: float | None,
) -> dict[str, int | float]:
    kwargs: dict[str, int | float] = {}
    if max_attempts is not None:
        kwargs["max_attempts"] = max(1, int(max_attempts))
    if retry_backoff_sec is not None:
        kwargs["retry_backoff_sec"] = max(0.0, float(retry_backoff_sec))
    return kwargs


def _build_active_seed_backend(
    cfg: Any,
    *,
    max_attempts: int | None = None,
    retry_backoff_sec: float | None = None,
) -> Any | None:
    retry_kwargs = _seed_retry_kwargs(
        max_attempts=max_attempts,
        retry_backoff_sec=retry_backoff_sec,
    )
    provider = getattr(cfg, "voice_provider", "")
    if provider == "openai" and getattr(cfg, "openai_api_key", ""):
        from .cues.generator import OpenAITTSGenerator
        return OpenAITTSGenerator(
            api_key=cfg.openai_api_key,
            voice=cfg.openai_voice,
            **retry_kwargs,
        )
    if provider == "gemini" and getattr(cfg, "gemini_api_key", ""):
        from .cues.generator import GEMINI_TTS_MODEL, GeminiTTSGenerator
        return GeminiTTSGenerator(
            api_key=cfg.gemini_api_key,
            voice=cfg.gemini_voice,
            model=getattr(cfg, "gemini_tts_model", "") or GEMINI_TTS_MODEL,
            **retry_kwargs,
        )
    if provider == "grok" and getattr(cfg, "grok_api_key", ""):
        from .cues.generator import GrokTTSGenerator
        return GrokTTSGenerator(
            api_key=cfg.grok_api_key,
            voice=cfg.grok_voice,
            **retry_kwargs,
        )
    return None


def _load_payload(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": PROFILE_VERSION, "profiles": []}
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning("assistant loudness profile read failed: %s", e)
        return {"version": PROFILE_VERSION, "profiles": []}
    if not isinstance(data, dict):
        return {"version": PROFILE_VERSION, "profiles": []}
    if not isinstance(data.get("profiles"), list):
        data["profiles"] = []
    return data


def _write_payload(payload: dict[str, Any], path: str | os.PathLike[str]) -> None:
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=".assistant_loudness.",
        suffix=".tmp",
        dir=str(dst.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, dst)
        try:
            os.chmod(dst, 0o644)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _profile_from_mapping(item: dict[str, Any]) -> AssistantLoudnessProfile | None:
    try:
        profile = AssistantLoudnessProfile(
            provider=str(item["provider"]),
            model=str(item["model"]),
            voice=str(item["voice"]),
            source_lufs=float(item["source_lufs"]),
            source_peak_dbfs=float(item["source_peak_dbfs"]),
            confidence=max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
            updated_at=str(item.get("updated_at", "")),
            method=str(item.get("method", "")),
            sample_rate=int(item.get("sample_rate", MEASURE_RATE)),
            phrase_hash=str(item.get("phrase_hash", "")),
            version=int(item.get("version", PROFILE_VERSION)),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        math.isfinite(profile.source_lufs)
        and math.isfinite(profile.source_peak_dbfs)
        and -120.0 <= profile.source_lufs <= 0.0
        and -120.0 <= profile.source_peak_dbfs <= 0.0
    ):
        return None
    return profile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")


def _phrase_hash(phrase: str) -> str:
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:12]
