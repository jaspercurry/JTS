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
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PROFILE_PATH = "/var/lib/jasper/sound_profile.json"
PROFILE_LIBRARY_PATH = "/var/lib/jasper/sound_profiles.json"

# Per-band limit for Simple mode. ±12 dB matches the 5-band sliders in
# the redesigned /sound/ UI; the headroom preamp auto-attenuates, so
# boosts stay clip-safe. The calibration advisor shares this bound (via
# response.py), so model-proposed simple_eq edits get the same range.
SIMPLE_EQ_LIMIT_DB = 12.0
ADVANCED_GAIN_LIMIT_DB = 12.0
MAX_PARAMETRIC_BANDS = 8
MAX_CUSTOM_PROFILES = 24
MAX_PROFILE_NAME_CHARS = 48
MIN_FREQ_HZ = 20.0
MAX_FREQ_HZ = 20000.0
MIN_Q = 0.2
MAX_Q = 10.0
FILTER_EPSILON_DB = 0.05
STOCK_PROFILE_PREFIX = "stock:"
CUSTOM_PROFILE_PREFIX = "custom_"
_CUSTOM_PROFILE_ID_RE = re.compile(r"^custom_[a-f0-9]{12}$")
PREVIEW_POINT_COUNT = 121

DEFAULT_PREVIEW_FREQS: tuple[float, ...] = tuple(
    round(
        MIN_FREQ_HZ * ((MAX_FREQ_HZ / MIN_FREQ_HZ) ** (i / (PREVIEW_POINT_COUNT - 1))),
        3,
    )
    for i in range(PREVIEW_POINT_COUNT)
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
    """Five-band consumer EQ: Sub-bass / Bass / Mid / Presence / Treble.

    Fixed-frequency, taste-shaping bands where only gain is editable per
    band; the slot definitions (frequency, filter type, Q/slope) live in
    SIMPLE_BANDS so the model, the CamillaDSP emitter, and the web UI all
    render from one source. Bounded to ±SIMPLE_EQ_LIMIT_DB; room
    correction and hardware fault compensation live elsewhere.

    Older 3-band profiles (bass/mid/treble only) load unchanged — the two
    new bands default to 0 dB. Note the band centres shifted with the
    redesign (bass 105->150 Hz, treble shelf 4k->10k), so a migrated
    profile's bass/treble values now shape slightly different frequencies.
    """

    sub_bass_db: float = 0.0
    bass_db: float = 0.0
    mid_db: float = 0.0
    presence_db: float = 0.0
    treble_db: float = 0.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "SimpleEq":
        raw = raw if isinstance(raw, dict) else {}

        def band(*keys: str) -> float:
            for key in keys:
                if key in raw:
                    return _clip(
                        _coerce_float(raw.get(key), 0.0),
                        -SIMPLE_EQ_LIMIT_DB,
                        SIMPLE_EQ_LIMIT_DB,
                    )
            return 0.0

        return cls(
            sub_bass_db=band("sub_bass_db", "sub_bass"),
            bass_db=band("bass_db", "bass"),
            mid_db=band("mid_db", "mid"),
            presence_db=band("presence_db", "presence"),
            treble_db=band("treble_db", "treble"),
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "sub_bass_db": round(self.sub_bass_db, 3),
            "bass_db": round(self.bass_db, 3),
            "mid_db": round(self.mid_db, 3),
            "presence_db": round(self.presence_db, 3),
            "treble_db": round(self.treble_db, 3),
        }


@dataclass(frozen=True)
class SimpleBand:
    """Fixed slot for one Simple-mode band. Only gain is user-editable;
    frequency, filter type, and Q/slope are fixed per slot."""

    key: str
    field: str
    label: str
    filter_name: str
    biquad_type: str
    freq_hz: float
    q: float | None = None
    slope: float | None = None


# The five Simple-mode slots, low to high — one source of truth for the
# model (_simple_filters), the web UI (column rendering), and any future
# proposer. Frequencies/types match the redesigned /sound/ mockup.
SIMPLE_BANDS: tuple[SimpleBand, ...] = (
    SimpleBand("sub_bass", "sub_bass_db", "Sub-bass", "sound_simple_sub_bass",
               "Lowshelf", 60.0, slope=6.0),
    SimpleBand("bass", "bass_db", "Bass", "sound_simple_bass",
               "Peaking", 150.0, q=1.0),
    SimpleBand("mid", "mid_db", "Mid", "sound_simple_mid",
               "Peaking", 1000.0, q=1.0),
    SimpleBand("presence", "presence_db", "Presence", "sound_simple_presence",
               "Peaking", 4000.0, q=1.0),
    SimpleBand("treble", "treble_db", "Treble", "sound_simple_treble",
               "Highshelf", 10000.0, slope=6.0),
)

# Field names in canonical order. The calibration advisor's validator
# range-checks exactly these, so deriving it here keeps the two in sync.
SIMPLE_EQ_FIELDS: tuple[str, ...] = tuple(b.field for b in SIMPLE_BANDS)


def simple_bands_payload() -> list[dict[str, Any]]:
    """UI-facing slot metadata so the web page renders the Simple columns
    from data instead of hardcoding the band list."""
    return [
        {
            "key": b.key,
            "field": b.field,
            "label": b.label,
            "freq_hz": b.freq_hz,
            "type": b.biquad_type,
        }
        for b in SIMPLE_BANDS
    ]


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
    profile_id: str = ""
    profile_name: str = ""

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
        profile_id = _normalize_profile_id(raw.get("profile_id", raw.get("id", "")))
        return cls(
            enabled=_coerce_bool(raw.get("enabled"), True),
            curve_id=curve_id,
            simple_eq=SimpleEq.from_mapping(raw.get("simple_eq", raw)),
            parametric_bands=bands,
            updated_at=str(raw.get("updated_at") or _utc_now_iso()),
            profile_id=profile_id,
            profile_name=_normalize_profile_name(
                raw.get("profile_name", raw.get("name", "")),
                default="",
            )
            if profile_id
            else "",
        )

    def with_timestamp(self) -> "SoundProfile":
        return SoundProfile(
            enabled=self.enabled,
            curve_id=self.curve_id,
            simple_eq=self.simple_eq,
            parametric_bands=self.parametric_bands,
            updated_at=_utc_now_iso(),
            profile_id=self.profile_id,
            profile_name=self.profile_name,
        )

    def with_profile_identity(
        self,
        *,
        profile_id: str,
        profile_name: str,
    ) -> "SoundProfile":
        """Return the same DSP profile annotated with its library identity."""

        normalized_id = _normalize_profile_id(profile_id)
        return SoundProfile(
            enabled=self.enabled,
            curve_id=self.curve_id,
            simple_eq=self.simple_eq,
            parametric_bands=self.parametric_bands,
            updated_at=self.updated_at,
            profile_id=normalized_id,
            profile_name=_normalize_profile_name(profile_name, default="")
            if normalized_id
            else "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "curve_id": self.curve_id,
            "simple_eq": self.simple_eq.to_dict(),
            "parametric_bands": [band.to_dict() for band in self.parametric_bands],
            "updated_at": self.updated_at,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
        }


@dataclass(frozen=True)
class ProfileLibraryEntry:
    """One named preference profile.

    Built-in stock entries are generated from ``CURVE_PRESETS`` at
    runtime. Only custom entries are persisted on disk.
    """

    id: str
    name: str
    profile: SoundProfile
    created_at: str
    updated_at: str
    builtin: bool = False
    description: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "ProfileLibraryEntry | None":
        raw = raw if isinstance(raw, dict) else {}
        profile_id = str(raw.get("id") or "").strip()
        if not _CUSTOM_PROFILE_ID_RE.match(profile_id):
            return None
        created_at = str(raw.get("created_at") or _utc_now_iso())
        updated_at = str(raw.get("updated_at") or created_at)
        return cls(
            id=profile_id,
            name=_normalize_profile_name(raw.get("name")),
            profile=SoundProfile.from_mapping(raw.get("profile")),
            created_at=created_at,
            updated_at=updated_at,
            builtin=False,
            description=str(raw.get("description") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "profile": self.profile.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": "stock" if self.builtin else "custom",
            "editable": not self.builtin,
            "description": self.description,
            "profile": self.profile.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def curve_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "filters": [
                {
                    "type": spec.biquad_type,
                    "freq_hz": spec.freq,
                    "gain_db": spec.gain,
                    "q": spec.q,
                    "slope": spec.slope,
                }
                for spec in preset.filters
            ],
        }
        for preset in CURVE_PRESETS
    ]


def _normalize_profile_name(value: Any, default: str = "Custom Profile") -> str:
    name = " ".join(str(value or "").split())
    if not name:
        name = default
    return name[:MAX_PROFILE_NAME_CHARS]


def _normalize_profile_id(value: Any) -> str:
    profile_id = str(value or "").strip()
    if profile_id.startswith(STOCK_PROFILE_PREFIX):
        curve_id = profile_id.removeprefix(STOCK_PROFILE_PREFIX)
        if curve_id in _CURVE_BY_ID:
            return profile_id
    if _CUSTOM_PROFILE_ID_RE.match(profile_id):
        return profile_id
    return ""


def _stock_profile_entries() -> tuple[ProfileLibraryEntry, ...]:
    return tuple(
        ProfileLibraryEntry(
            id=f"{STOCK_PROFILE_PREFIX}{preset.id}",
            name=preset.label,
            profile=SoundProfile(curve_id=preset.id, updated_at="").with_profile_identity(
                profile_id=f"{STOCK_PROFILE_PREFIX}{preset.id}",
                profile_name=preset.label,
            ),
            created_at="",
            updated_at="",
            builtin=True,
            description=preset.description,
        )
        for preset in CURVE_PRESETS
    )


def profile_library_payload(
    custom_entries: Iterable[ProfileLibraryEntry] = (),
) -> list[dict[str, Any]]:
    return [
        *(entry.to_payload() for entry in _stock_profile_entries()),
        *(entry.to_payload() for entry in custom_entries),
    ]


def load_profile_library(path: str | Path | None = None) -> tuple[ProfileLibraryEntry, ...]:
    library_path = Path(
        path or os.environ.get("JASPER_SOUND_PROFILE_LIBRARY_PATH", PROFILE_LIBRARY_PATH)
    )
    try:
        raw = json.loads(library_path.read_text())
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read sound profile library %s: %s", library_path, e)
        return ()
    raw_profiles = raw.get("profiles") if isinstance(raw, dict) else raw
    if not isinstance(raw_profiles, list):
        return ()
    entries: list[ProfileLibraryEntry] = []
    seen: set[str] = set()
    for item in raw_profiles:
        entry = ProfileLibraryEntry.from_mapping(item)
        if entry is None or entry.id in seen:
            continue
        entries.append(entry)
        seen.add(entry.id)
        if len(entries) >= MAX_CUSTOM_PROFILES:
            break
    return tuple(entries)


def save_profile_library(
    entries: Iterable[ProfileLibraryEntry],
    path: str | Path | None = None,
) -> None:
    library_path = Path(
        path or os.environ.get("JASPER_SOUND_PROFILE_LIBRARY_PATH", PROFILE_LIBRARY_PATH)
    )
    library_path.parent.mkdir(parents=True, exist_ok=True)
    custom_entries = [entry for entry in entries if not entry.builtin][
        :MAX_CUSTOM_PROFILES
    ]
    data = (
        json.dumps(
            {
                "version": 1,
                "profiles": [entry.to_dict() for entry in custom_entries],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    _atomic_write_text(library_path, data)


def _new_custom_profile_id(existing: Iterable[ProfileLibraryEntry]) -> str:
    seen = {entry.id for entry in existing}
    while True:
        profile_id = f"{CUSTOM_PROFILE_PREFIX}{uuid.uuid4().hex[:12]}"
        if profile_id not in seen:
            return profile_id


def save_named_profile(
    profile: SoundProfile,
    *,
    name: str | None,
    path: str | Path | None = None,
    profile_id: str | None = None,
) -> ProfileLibraryEntry:
    entries = list(load_profile_library(path))
    now = _utc_now_iso()
    normalized = _normalize_profile_name(name)
    if profile_id and _CUSTOM_PROFILE_ID_RE.match(profile_id):
        for index, entry in enumerate(entries):
            if entry.id == profile_id:
                profile_name = normalized if name is not None else entry.name
                stamped = (
                    profile.with_profile_identity(
                        profile_id=entry.id,
                        profile_name=profile_name,
                    ).with_timestamp()
                )
                updated = ProfileLibraryEntry(
                    id=entry.id,
                    name=profile_name,
                    profile=stamped,
                    created_at=entry.created_at,
                    updated_at=now,
                )
                entries[index] = updated
                save_profile_library(entries, path)
                return updated
    if len(entries) >= MAX_CUSTOM_PROFILES:
        raise ValueError(f"profile library is limited to {MAX_CUSTOM_PROFILES} customs")
    new_id = _new_custom_profile_id(entries)
    stamped = (
        profile.with_profile_identity(
            profile_id=new_id,
            profile_name=normalized,
        ).with_timestamp()
    )
    entry = ProfileLibraryEntry(
        id=new_id,
        name=normalized,
        profile=stamped,
        created_at=now,
        updated_at=now,
    )
    entries.append(entry)
    save_profile_library(entries, path)
    return entry


def rename_named_profile(
    profile_id: str,
    *,
    name: str,
    path: str | Path | None = None,
) -> ProfileLibraryEntry:
    entries = list(load_profile_library(path))
    now = _utc_now_iso()
    for index, entry in enumerate(entries):
        if entry.id == profile_id:
            normalized = _normalize_profile_name(name)
            renamed = ProfileLibraryEntry(
                id=entry.id,
                name=normalized,
                profile=entry.profile.with_profile_identity(
                    profile_id=entry.id,
                    profile_name=normalized,
                ),
                created_at=entry.created_at,
                updated_at=now,
            )
            entries[index] = renamed
            save_profile_library(entries, path)
            return renamed
    raise ValueError(f"unknown custom sound profile: {profile_id}")


def delete_named_profile(profile_id: str, *, path: str | Path | None = None) -> None:
    entries = list(load_profile_library(path))
    kept = [entry for entry in entries if entry.id != profile_id]
    if len(kept) == len(entries):
        raise ValueError(f"unknown custom sound profile: {profile_id}")
    save_profile_library(kept, path)


def _curve_filters(curve_id: str) -> tuple[FilterSpec, ...]:
    return _CURVE_BY_ID.get(curve_id, _CURVE_BY_ID["flat"]).filters


def _simple_filters(simple: SimpleEq) -> tuple[FilterSpec, ...]:
    return tuple(
        FilterSpec(
            band.filter_name,
            band.biquad_type,
            band.freq_hz,
            getattr(simple, band.field),
            q=band.q,
            slope=band.slope,
        )
        for band in SIMPLE_BANDS
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


def loudness_compensation_db(profile: SoundProfile) -> float:
    """Attenuation that offsets how much louder this EQ makes typical music.

    Used by the optional "match loudness" setting so switching profiles
    compares tone, not volume. Loudness-weighted, not peak: music energy is
    roughly pink (equal energy per octave -> uniform across our log-spaced
    preview points), and the ear de-emphasizes the extremes, so we average
    power over the ~40 Hz-16 kHz band and convert back to dB. A narrow +8 dB
    band barely moves loudness (~1 dB); a broad bass shelf moves it more.

    Anchored to attenuation (>= 0): a net-louder profile is turned down
    toward flat loudness; a net-quieter (subtractive) profile is left alone
    rather than boosted, so the compensation can never cause clipping. This
    is an approximation, consistent with response_preview; CamillaDSP owns
    the real filters.
    """

    if not build_sound_filters(profile):
        return 0.0
    band = [
        point
        for point in response_preview(profile)
        if 40.0 <= point["freq_hz"] <= 16000.0
    ]
    if not band:
        return 0.0
    mean_power = sum(10.0 ** (point["db"] / 10.0) for point in band) / len(band)
    if mean_power <= 0.0:
        return 0.0
    return round(max(0.0, 10.0 * math.log10(mean_power)), 3)


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
    _atomic_write_text(profile_path, data)


def _atomic_write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        tmp_name = f.name
    os.replace(tmp_name, path)
