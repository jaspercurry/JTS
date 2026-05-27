"""Sound-curve and preference-EQ model.

This module is intentionally pure Python and import-cheap. The web
wizard, future voice/LLM proposal path, and CamillaDSP YAML emitter all
share this one contract:

  stock sound curve -> simple bass/mid/treble -> advanced PEQ bands

The curve/preset labels are user-facing, but the output is deliberately
deterministic DSP data. Future AI help should propose bounded edits to
this model, not own a parallel EQ representation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PROFILE_PATH = "/var/lib/jasper/sound_profile.json"

SIMPLE_EQ_LIMIT_DB = 6.0
ADVANCED_GAIN_LIMIT_DB = 12.0
MAX_PARAMETRIC_BANDS = 8
MIN_FREQ_HZ = 20.0
MAX_FREQ_HZ = 20000.0
MIN_Q = 0.2
MAX_Q = 10.0
FILTER_EPSILON_DB = 0.05

DEFAULT_PREVIEW_FREQS: tuple[float, ...] = (
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400,
    500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000,
    6300, 8000, 10000, 12500, 16000, 20000,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _clip(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


@dataclass(frozen=True)
class FilterSpec:
    """A bounded CamillaDSP-friendly filter definition."""

    name: str
    biquad_type: str
    freq: float
    gain: float
    q: float | None = None
    slope: float | None = None

    def active(self) -> bool:
        return abs(self.gain) >= FILTER_EPSILON_DB


@dataclass(frozen=True)
class CurvePreset:
    """A stock sound curve shown to users as an EQ profile."""

    id: str
    label: str
    description: str
    filters: tuple[FilterSpec, ...] = ()


CURVE_PRESETS: tuple[CurvePreset, ...] = (
    CurvePreset(
        id="flat",
        label="Flat",
        description="No stock sound curve.",
        filters=(),
    ),
    CurvePreset(
        id="harman",
        label="Harman-style",
        description="Gentle bass lift with a mild downward high-frequency tilt.",
        filters=(
            FilterSpec("sound_curve_harman_bass", "Lowshelf", 105.0, 4.0, slope=6.0),
            FilterSpec("sound_curve_harman_tilt", "Highshelf", 3500.0, -2.0, slope=3.0),
        ),
    ),
    CurvePreset(
        id="bk",
        label="B&K-style",
        description="Classic in-room downward tilt, approximated as broad shelves.",
        filters=(
            FilterSpec("sound_curve_bk_bass", "Lowshelf", 120.0, 3.0, slope=6.0),
            FilterSpec("sound_curve_bk_tilt", "Highshelf", 2500.0, -4.5, slope=3.0),
        ),
    ),
)

_CURVE_BY_ID = {preset.id: preset for preset in CURVE_PRESETS}


@dataclass(frozen=True)
class SimpleEq:
    """Three-band consumer EQ.

    Bass / Mid / Treble is the vocabulary users already know. The
    controls are intentionally bounded to taste-shaping ranges; room
    correction and hardware fault compensation live elsewhere.
    """

    bass_db: float = 0.0
    mid_db: float = 0.0
    treble_db: float = 0.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "SimpleEq":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            bass_db=_clip(
                _coerce_float(raw.get("bass_db", raw.get("bass", 0.0)), 0.0),
                -SIMPLE_EQ_LIMIT_DB,
                SIMPLE_EQ_LIMIT_DB,
            ),
            mid_db=_clip(
                _coerce_float(raw.get("mid_db", raw.get("mid", 0.0)), 0.0),
                -SIMPLE_EQ_LIMIT_DB,
                SIMPLE_EQ_LIMIT_DB,
            ),
            treble_db=_clip(
                _coerce_float(raw.get("treble_db", raw.get("treble", 0.0)), 0.0),
                -SIMPLE_EQ_LIMIT_DB,
                SIMPLE_EQ_LIMIT_DB,
            ),
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "bass_db": round(self.bass_db, 3),
            "mid_db": round(self.mid_db, 3),
            "treble_db": round(self.treble_db, 3),
        }


@dataclass(frozen=True)
class ParametricBand:
    """One advanced EQ band.

    The touch UI and future AI proposals both use this deterministic
    bounded filter substrate; neither path owns a parallel EQ model.
    """

    enabled: bool = True
    biquad_type: str = "Peaking"
    freq_hz: float = 1000.0
    gain_db: float = 0.0
    q: float = 1.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "ParametricBand":
        raw = raw if isinstance(raw, dict) else {}
        kind = str(raw.get("type", raw.get("biquad_type", "Peaking"))).strip()
        aliases = {
            "peaking": "Peaking",
            "peak": "Peaking",
            "lowshelf": "Lowshelf",
            "low_shelf": "Lowshelf",
            "highshelf": "Highshelf",
            "high_shelf": "Highshelf",
        }
        biquad_type = aliases.get(kind.lower(), "Peaking")
        return cls(
            enabled=_coerce_bool(raw.get("enabled"), True),
            biquad_type=biquad_type,
            freq_hz=_clip(
                _coerce_float(raw.get("freq_hz", raw.get("freq", 1000.0)), 1000.0),
                MIN_FREQ_HZ,
                MAX_FREQ_HZ,
            ),
            gain_db=_clip(
                _coerce_float(raw.get("gain_db", raw.get("gain", 0.0)), 0.0),
                -ADVANCED_GAIN_LIMIT_DB,
                ADVANCED_GAIN_LIMIT_DB,
            ),
            q=_clip(_coerce_float(raw.get("q", 1.0), 1.0), MIN_Q, MAX_Q),
        )

    def to_dict(self) -> dict[str, float | bool | str]:
        return {
            "enabled": self.enabled,
            "type": self.biquad_type,
            "freq_hz": round(self.freq_hz, 3),
            "gain_db": round(self.gain_db, 3),
            "q": round(self.q, 3),
        }


@dataclass(frozen=True)
class SoundProfile:
    """A persisted preference profile."""

    enabled: bool = True
    curve_id: str = "flat"
    simple_eq: SimpleEq = field(default_factory=SimpleEq)
    parametric_bands: tuple[ParametricBand, ...] = ()
    updated_at: str = field(default_factory=_utc_now_iso)

    @classmethod
    def from_mapping(cls, raw: Any) -> "SoundProfile":
        raw = raw if isinstance(raw, dict) else {}
        curve_id = str(raw.get("curve_id", raw.get("curve", "flat"))).strip()
        if curve_id not in _CURVE_BY_ID:
            curve_id = "flat"
        raw_bands = raw.get("parametric_bands", raw.get("bands", ()))
        if not isinstance(raw_bands, list):
            raw_bands = []
        bands = tuple(
            ParametricBand.from_mapping(item)
            for item in raw_bands[:MAX_PARAMETRIC_BANDS]
        )
        return cls(
            enabled=_coerce_bool(raw.get("enabled"), True),
            curve_id=curve_id,
            simple_eq=SimpleEq.from_mapping(raw.get("simple_eq", raw)),
            parametric_bands=bands,
            updated_at=str(raw.get("updated_at") or _utc_now_iso()),
        )

    def with_timestamp(self) -> "SoundProfile":
        return SoundProfile(
            enabled=self.enabled,
            curve_id=self.curve_id,
            simple_eq=self.simple_eq,
            parametric_bands=self.parametric_bands,
            updated_at=_utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "curve_id": self.curve_id,
            "simple_eq": self.simple_eq.to_dict(),
            "parametric_bands": [band.to_dict() for band in self.parametric_bands],
            "updated_at": self.updated_at,
        }


def curve_payload() -> list[dict[str, str]]:
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
        }
        for preset in CURVE_PRESETS
    ]


def _curve_filters(curve_id: str) -> tuple[FilterSpec, ...]:
    return _CURVE_BY_ID.get(curve_id, _CURVE_BY_ID["flat"]).filters


def _simple_filters(simple: SimpleEq) -> tuple[FilterSpec, ...]:
    return (
        FilterSpec("sound_simple_bass", "Lowshelf", 105.0, simple.bass_db, slope=6.0),
        FilterSpec("sound_simple_mid", "Peaking", 1000.0, simple.mid_db, q=0.8),
        FilterSpec(
            "sound_simple_treble", "Highshelf", 4000.0, simple.treble_db, slope=6.0,
        ),
    )


def _advanced_filters(bands: Iterable[ParametricBand]) -> tuple[FilterSpec, ...]:
    specs = []
    for i, band in enumerate(bands, start=1):
        if not band.enabled:
            continue
        if band.biquad_type in {"Lowshelf", "Highshelf"}:
            specs.append(
                FilterSpec(
                    f"sound_advanced_{i}",
                    band.biquad_type,
                    band.freq_hz,
                    band.gain_db,
                    slope=6.0,
                )
            )
        else:
            specs.append(
                FilterSpec(
                    f"sound_advanced_{i}",
                    "Peaking",
                    band.freq_hz,
                    band.gain_db,
                    q=band.q,
                )
            )
    return tuple(specs)


def build_sound_filters(profile: SoundProfile) -> tuple[FilterSpec, ...]:
    """Return active sound filters in canonical order."""

    if not profile.enabled:
        return ()
    filters = (
        *_curve_filters(profile.curve_id),
        *_simple_filters(profile.simple_eq),
        *_advanced_filters(profile.parametric_bands),
    )
    return tuple(spec for spec in filters if spec.active())


def _filter_response_db(spec: FilterSpec, freqs: Iterable[float]) -> list[float]:
    out: list[float] = []
    for freq in freqs:
        safe_freq = max(float(freq), 1e-6)
        if spec.biquad_type == "Lowshelf":
            # Smooth log-frequency shelf approximation. This is only
            # for preview/headroom; CamillaDSP computes the real biquad.
            x = math.log2(safe_freq / spec.freq)
            out.append(spec.gain / (1.0 + math.exp(3.0 * x)))
        elif spec.biquad_type == "Highshelf":
            x = math.log2(safe_freq / spec.freq)
            out.append(spec.gain / (1.0 + math.exp(-3.0 * x)))
        else:
            q = spec.q or 1.0
            delta_oct = math.log2(safe_freq / spec.freq)
            bw = 1.0 / max(q, 1e-3)
            out.append(spec.gain / (1.0 + (delta_oct / bw) ** 2))
    return out


def response_preview(
    profile: SoundProfile,
    freqs: Iterable[float] = DEFAULT_PREVIEW_FREQS,
) -> list[dict[str, float]]:
    """Approximate magnitude response for UI preview and headroom.

    The preview is intentionally labelled approximate in the UI. The
    actual audio path uses CamillaDSP's filter implementation.
    """

    freq_list = [float(freq) for freq in freqs]
    totals = [0.0 for _ in freq_list]
    for spec in build_sound_filters(profile):
        for i, db in enumerate(_filter_response_db(spec, freq_list)):
            totals[i] += db
    return [
        {"freq_hz": round(freq, 3), "db": round(db, 3)}
        for freq, db in zip(freq_list, totals)
    ]


def response_component_payload(
    profile: SoundProfile,
    freqs: Iterable[float] = DEFAULT_PREVIEW_FREQS,
) -> dict[str, Any]:
    """Approximate component responses for UI overlays.

    This is intentionally the same cheap preview math used by
    ``response_preview``. The graph is an interaction aid, not the DSP
    authority; CamillaDSP owns the actual filter implementation.
    """

    freq_list = [float(freq) for freq in freqs]

    def _points(specs: Iterable[FilterSpec]) -> list[dict[str, float]]:
        totals = [0.0 for _ in freq_list]
        active = False
        for spec in specs:
            if not spec.active():
                continue
            active = True
            for i, db in enumerate(_filter_response_db(spec, freq_list)):
                totals[i] += db
        if not active:
            return []
        return [
            {"freq_hz": round(freq, 3), "db": round(db, 3)}
            for freq, db in zip(freq_list, totals)
        ]

    if not profile.enabled:
        return {"curve": [], "simple": [], "advanced": []}

    advanced = []
    for index, band in enumerate(profile.parametric_bands):
        specs = _advanced_filters((band,))
        advanced.append(
            {
                "index": index,
                "enabled": band.enabled,
                "preview": _points(specs),
            }
        )
    return {
        "curve": _points(_curve_filters(profile.curve_id)),
        "simple": _points(_simple_filters(profile.simple_eq)),
        "advanced": advanced,
    }


def estimate_headroom_db(profile: SoundProfile) -> float:
    """Digital preamp attenuation needed before preference boosts."""

    filters = build_sound_filters(profile)
    if not filters:
        return 0.0
    dense_freqs = [
        MIN_FREQ_HZ * ((MAX_FREQ_HZ / MIN_FREQ_HZ) ** (i / 240))
        for i in range(241)
    ]
    sample_freqs = sorted({
        *DEFAULT_PREVIEW_FREQS,
        *dense_freqs,
        *(spec.freq for spec in filters),
    })
    preview = response_preview(profile, sample_freqs)
    if not preview:
        return 0.0
    max_boost = max(point["db"] for point in preview)
    return round(max(0.0, max_boost), 3)


def estimate_compare_headroom_db(profiles: Iterable[SoundProfile]) -> float:
    """Common attenuation anchor for level-matched A/B auditions.

    The compare path uses one shared preamp across Bypass / Saved /
    Draft so the louder-seeming option is not just the one with less
    safety attenuation. This is not a psychoacoustic loudness model; it
    is a deterministic, clipping-safe comparison anchor.
    """

    return round(
        max((estimate_headroom_db(profile) for profile in profiles), default=0.0), 3
    )


def load_profile(path: str | Path | None = None) -> SoundProfile:
    profile_path = Path(
        path or os.environ.get("JASPER_SOUND_PROFILE_PATH", PROFILE_PATH)
    )
    try:
        return SoundProfile.from_mapping(json.loads(profile_path.read_text()))
    except FileNotFoundError:
        return SoundProfile(updated_at="")
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read sound profile %s: %s", profile_path, e)
        return SoundProfile(updated_at="")


def save_profile(profile: SoundProfile, path: str | Path | None = None) -> None:
    profile_path = Path(
        path or os.environ.get("JASPER_SOUND_PROFILE_PATH", PROFILE_PATH)
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        dir=profile_path.parent,
        prefix=f".{profile_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(data)
        tmp_name = f.name
    os.replace(tmp_name, profile_path)
