# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-microphone calibration registry and parser.

Two input paths -- a vendor file fetched by serial
(:mod:`jasper.cli._vendor_calibration`) and a bring-your-own uploaded
REW/HouseCurve-style text curve -- normalize into ``correction_db``: an
additive dB offset applied to the measured response before target
normalization.

The quirk that matters is the SIGN. A measurement mic's calibration file states
the microphone's own *response*, so the correction is its negation; the
per-vendor declaration is in ``SUPPORTED_MODELS``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import numpy as np

from jasper.platform.atomic_io import atomic_write_text
from jasper.platform.json_fields import finite_float, sha256_text

# The model registry -- SUPPORTED_MODELS, DEFAULT_SIGN_CONVENTION,
# measurement_mic_usb_ids, mic_tier_for_model -- lives in the numpy-free leaf
# module jasper.audio_measurement.mic_identity, so the reconciler's hotplug
# bridge can read it without paying this module's numpy import. Re-exported
# here (the `X as X` form) because this module is the established import
# surface for the wizard/web consumers; the leaf stays the one owner.
from jasper.audio_measurement.mic_identity import (
    DEFAULT_SIGN_CONVENTION as DEFAULT_SIGN_CONVENTION,
    SUPPORTED_MODELS as SUPPORTED_MODELS,
    measurement_mic_usb_ids as measurement_mic_usb_ids,
    mic_tier_for_model as mic_tier_for_model,
)
from jasper.platform.log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_CALIBRATION_DIR = Path("/var/lib/jasper/correction/calibration_mics")


def model_label_aliases(model_key: str) -> list[str]:
    """OS device-label tokens that identify this mic for label-based inference.

    Matched case- and punctuation-insensitively, so an alias need only be a
    distinctive substring of the device label (``iMM-6`` matches ``iMM-6C``).
    A registry entry may set ``label_aliases``; the default is the vendor model.
    """
    spec = SUPPORTED_MODELS.get(model_key, {})
    aliases = spec.get("label_aliases") or [spec.get("vendor_model", "")]
    return [str(a) for a in aliases if a]


@dataclass(frozen=True)
class CalibrationCurve:
    freqs_hz: list[float]
    correction_db: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs_hz": self.freqs_hz,
            "correction_db": self.correction_db,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CalibrationCurve":
        """Strictly parse the curve shared by records and replay evidence."""

        if not isinstance(data, Mapping):
            raise ValueError("calibration curve must be an object")

        def numeric_array(name: str) -> list[float]:
            raw = data.get(name)
            if not isinstance(raw, list) or len(raw) < 2:
                raise ValueError(f"calibration curve {name} needs at least two points")
            if any(finite_float(value) is None for value in raw):
                raise ValueError(f"calibration curve {name} must be finite numbers")
            return [float(value) for value in raw]

        freqs = numeric_array("freqs_hz")
        correction = numeric_array("correction_db")
        if len(freqs) != len(correction):
            raise ValueError("calibration curve arrays must be length-matched")
        if any(freq <= 0.0 for freq in freqs) or any(
            right <= left for left, right in zip(freqs, freqs[1:])
        ):
            raise ValueError(
                "calibration curve frequencies must be positive and strictly increasing"
            )
        return cls(freqs_hz=freqs, correction_db=correction)


@dataclass(frozen=True)
class CalibrationRecord:
    calibration_id: str
    provider: str
    model: str
    label: str
    source: str
    raw_path: str
    metadata_path: str
    file_sha256: str
    serial_hash: str | None
    orientation: str
    sign_convention: str
    fetched_at: float
    point_count: int
    curve: CalibrationCurve

    def public_metadata(self) -> dict[str, Any]:
        """Metadata safe to show in UI and write into bundles.

        Vendor lookup URLs often carry the mic serial in the file name, so the
        raw source is never exposed here.
        """
        return {
            "calibration_id": self.calibration_id,
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "source": _public_source(self.source),
            "file_sha256": self.file_sha256,
            "serial_hash": self.serial_hash,
            "orientation": self.orientation,
            "sign_convention": self.sign_convention,
            "fetched_at": self.fetched_at,
            "point_count": self.point_count,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.public_metadata()
        data.update({
            "raw_path": self.raw_path,
            "metadata_path": self.metadata_path,
            "curve": self.curve.to_dict(),
        })
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationRecord":
        return cls(
            calibration_id=str(data["calibration_id"]),
            provider=str(data["provider"]),
            model=str(data["model"]),
            label=str(data["label"]),
            source=str(data["source"]),
            raw_path=str(data["raw_path"]),
            metadata_path=str(data["metadata_path"]),
            file_sha256=str(data["file_sha256"]),
            serial_hash=(
                str(data["serial_hash"])
                if data.get("serial_hash") is not None
                else None
            ),
            orientation=str(data.get("orientation") or "unknown"),
            sign_convention=str(data.get("sign_convention") or "correction"),
            fetched_at=float(data["fetched_at"]),
            point_count=int(data["point_count"]),
            curve=CalibrationCurve.from_dict(data["curve"]),
        )


def serial_hash(serial: str | None) -> str | None:
    if not serial:
        return None
    normalized = re.sub(r"\s+", "", serial.strip().lower())
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _public_source(source: str) -> str:
    """Redact source details that may carry serial numbers."""
    if source.startswith(("http://", "https://")):
        return "vendor_lookup"
    if source.startswith("uploaded:"):
        return "uploaded_file"
    return _slug(source)


def _slug(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return out.lower() or "calibration"


_NUMBER_RE = re.compile(
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)


#: A tag anywhere makes the text a page: markup is never a calibration file,
#: however many of its lines open with a number (a CSS keyframe does).
_MARKUP_RE = re.compile(r"<[A-Za-z!/]")


def parse_calibration_text(
    text: str,
    *,
    sign_convention: str = "correction",
) -> CalibrationCurve:
    """Parse a broad REW/HouseCurve-style calibration text file.

    Accepted rows start with a numeric frequency and carry at least frequency +
    dB; further columns (a vendor phase column) are read past, because the
    correction this feeds is magnitude-only. ``sign_convention`` of
    ``correction`` means the second column is already the dB value to add;
    ``response`` means it is the mic response, so the correction is negated.
    """
    if sign_convention not in {"correction", "response"}:
        raise ValueError(
            "sign_convention must be 'correction' or 'response'"
        )
    if _MARKUP_RE.search(text):
        raise ValueError("calibration file is markup, not a curve")

    rows: list[tuple[float, float]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not (line[0].isdigit() or line[0] in "+-."):
            continue
        nums = _NUMBER_RE.findall(line)
        if len(nums) < 2:
            continue
        try:
            freq = float(nums[0])
            gain = float(nums[1])
        except ValueError:
            continue
        if not np.isfinite(freq) or not np.isfinite(gain) or freq <= 0:
            continue
        correction = -gain if sign_convention == "response" else gain
        rows.append((freq, correction))

    if len(rows) < 2:
        raise ValueError("calibration file must contain at least 2 rows")

    rows.sort(key=lambda r: r[0])
    deduped: list[tuple[float, float]] = []
    for row in rows:
        if deduped and abs(row[0] - deduped[-1][0]) < 1e-9:
            deduped[-1] = row
        else:
            deduped.append(row)
    if len(deduped) < 2:
        raise ValueError("calibration file must contain at least 2 frequencies")

    return CalibrationCurve(
        freqs_hz=[float(r[0]) for r in deduped],
        correction_db=[float(r[1]) for r in deduped],
    )


# The acoustic calibrator level the vendor's ``Sens Factor`` is quoted against:
# 1 Pa == 94 dB SPL, the standard pistonphone reference. Fixed physics.
CALIBRATOR_REFERENCE_DB_SPL = 94.0

_SENS_FACTOR_RE = re.compile(
    r"Sens\s*Factor\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB", re.IGNORECASE
)
_ANALOG_GAIN_RE = re.compile(
    r"AGain\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB", re.IGNORECASE
)
_SERNO_RE = re.compile(r"SERNO\s*:\s*([A-Za-z0-9._-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class MicSensitivity:
    """A measurement mic's ABSOLUTE level reference, read from its cal file.

    ``sens_factor_db`` is the vendor's ``Sens Factor``: the dBFS the mic reports
    when driven by a 94 dB SPL calibrator, so

        dB SPL = dBFS - sens_factor_db + 94

    Precondition, per REW's own cal-file documentation: the factor is valid
    only at the same mic interface gain and input volume it was measured at,
    which is capture input volume at MAXIMUM. Capture gain below that reads
    LOW, which would push a closed-loop level ramp LOUDER than the operator
    asked for, so any consumer that drives a speaker from this number must
    carry its own level-domain ceiling. ``analog_gain_db`` (the UMIK-2's
    ``AGain``, absent on a UMIK-1) is carried verbatim for that disclosure,
    never folded into the arithmetic -- it is already inside the vendor's
    measured ``sens_factor_db``.
    """

    sens_factor_db: float
    analog_gain_db: float | None = None
    serial: str | None = None

    def db_spl_from_dbfs(self, dbfs: float) -> float:
        """Convert one capture dBFS reading to dB SPL at the microphone."""
        return float(dbfs) - self.sens_factor_db + CALIBRATOR_REFERENCE_DB_SPL

    def dbfs_from_db_spl(self, db_spl: float) -> float:
        """Convert a dB SPL target to the capture dBFS that realizes it."""
        return float(db_spl) + self.sens_factor_db - CALIBRATOR_REFERENCE_DB_SPL

    def to_dict(self) -> dict[str, Any]:
        return {
            "sens_factor_db": self.sens_factor_db,
            "analog_gain_db": self.analog_gain_db,
            "serial": self.serial,
            "calibrator_reference_db_spl": CALIBRATOR_REFERENCE_DB_SPL,
        }


def parse_calibration_sensitivity(text: str) -> MicSensitivity | None:
    """Read the absolute-level header of a REW/miniDSP calibration file.

    The header is the file's first line and the ONE line
    :func:`parse_calibration_text` deliberately skips (it does not start with a
    number). Verbatim shapes::

        "Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"   # UMIK-2
        "Sens Factor =-.9099dB, SERNO: 7031234"                # UMIK-1, no AGain

    Returns ``None`` when no parseable ``Sens Factor`` is present -- a mic with
    no absolute reference. Callers must refuse, never default.
    """
    match = _SENS_FACTOR_RE.search(text)
    if match is None:
        return None
    try:
        sens_factor_db = float(match.group(1))
    except ValueError:  # pragma: no cover - the regex admits only floats
        return None
    if not np.isfinite(sens_factor_db):
        return None
    gain_match = _ANALOG_GAIN_RE.search(text)
    analog_gain_db: float | None = None
    if gain_match is not None:
        candidate = float(gain_match.group(1))
        analog_gain_db = candidate if np.isfinite(candidate) else None
    serno_match = _SERNO_RE.search(text)
    return MicSensitivity(
        sens_factor_db=sens_factor_db,
        analog_gain_db=analog_gain_db,
        serial=serno_match.group(1) if serno_match else None,
    )


def _resolve_calibration_source(
    *,
    calibration_file: str | Path | None,
    mic_serial: str | None,
    mic_provider: str,
    mic_model: str,
) -> tuple[Path, str, str] | None:
    """``(path, text, sign convention)`` for this run's mic, or ``None``.

    An explicit file wins; otherwise the stored record for this serial is used,
    and that record's OWN recorded convention is authoritative. An explicit
    file has no record behind it, so the model registry's declaration is the
    best available statement of which way its second column points.
    """
    path: Path | None = None
    convention = str(
        (SUPPORTED_MODELS.get(mic_model) or {}).get("sign_convention")
        or DEFAULT_SIGN_CONVENTION
    )
    if calibration_file:
        path = Path(calibration_file)
    elif mic_serial:
        record = find_stored_calibration(
            provider=mic_provider, model_key=mic_model, serial=mic_serial
        )
        if record is not None:
            path = Path(record.raw_path)
            convention = record.sign_convention
    if path is None:
        return None
    try:
        return path, path.read_text(encoding="utf-8", errors="replace"), convention
    except OSError:
        return None


def resolve_mic_sensitivity(
    *,
    calibration_file: str | Path | None = None,
    mic_serial: str | None = None,
    mic_provider: str = "minidsp",
    mic_model: str = "minidsp_umik2",
) -> MicSensitivity | None:
    """The mic's absolute reference, from an explicit file or the stored record.

    ``None`` when no calibration can be read, and the caller refuses: a guessed
    sensitivity would silently mis-scale every SPL decision.
    """
    source = _resolve_calibration_source(
        calibration_file=calibration_file,
        mic_serial=mic_serial,
        mic_provider=mic_provider,
        mic_model=mic_model,
    )
    return parse_calibration_sensitivity(source[1]) if source is not None else None


def apply_calibration_curve(
    freqs_hz: np.ndarray,
    magnitude_db: np.ndarray,
    curve: CalibrationCurve | None,
) -> np.ndarray:
    """Apply an additive mic-correction curve on the given grid."""
    if curve is None:
        return magnitude_db.astype(np.float64)
    cal_freqs = np.asarray(curve.freqs_hz, dtype=np.float64)
    cal_db = np.asarray(curve.correction_db, dtype=np.float64)
    measure_freqs = freqs_hz.astype(np.float64)
    correction = np.interp(
        np.log(np.maximum(measure_freqs, cal_freqs[0])),
        np.log(cal_freqs),
        cal_db,
        left=cal_db[0],
        right=cal_db[-1],
    )
    return (magnitude_db.astype(np.float64) + correction).astype(np.float64)


def _record_id(
    *,
    provider: str,
    model: str,
    file_sha256: str,
    serial_hash_value: str | None,
) -> str:
    if serial_hash_value:
        seed = hashlib.sha256(
            f"{serial_hash_value}:{model}:{file_sha256}".encode("utf-8")
        ).hexdigest()
    else:
        seed = file_sha256
    return f"{_slug(provider)}-{_slug(model)}-{seed[:12]}"


def store_calibration(
    *,
    text: str,
    provider: str,
    model: str,
    label: str | None = None,
    source: str,
    serial: str | None = None,
    orientation: str = "unknown",
    sign_convention: str = "correction",
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord:
    curve = parse_calibration_text(text, sign_convention=sign_convention)
    file_hash = sha256_text(text)
    serial_hash_value = serial_hash(serial)
    calibration_id = _record_id(
        provider=provider,
        model=model,
        file_sha256=file_hash,
        serial_hash_value=serial_hash_value,
    )

    dest_dir = root / _slug(provider) / _slug(model)
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    raw_path = dest_dir / f"{calibration_id}.txt"
    metadata_path = dest_dir / f"{calibration_id}.json"
    # 0640 + the parent directory's group. The registry root is installed
    # `2770 -g jasper` and the writer (`jasper-mic-calibration`) runs under
    # sudo, so a root-owned 0600 file is unreadable to every daemon that
    # resolves a calibration. The curve carries no secrets: the serial is
    # stored only as a one-way hash.
    atomic_write_text(raw_path, text, mode=0o640)

    record = CalibrationRecord(
        calibration_id=calibration_id,
        provider=provider,
        model=model,
        label=label or model,
        source=source,
        raw_path=str(raw_path),
        metadata_path=str(metadata_path),
        file_sha256=file_hash,
        serial_hash=serial_hash_value,
        orientation=orientation,
        sign_convention=sign_convention,
        fetched_at=time.time(),
        point_count=len(curve.freqs_hz),
        curve=curve,
    )
    atomic_write_text(
        metadata_path, json.dumps(record.to_dict(), indent=2), mode=0o640,
    )
    return record


def load_calibration_record(
    calibration_id: str,
    *,
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord:
    safe_id = _slug(calibration_id)
    matches = list(root.glob(f"*/*/{safe_id}.json"))
    if not matches:
        raise FileNotFoundError(f"calibration not found: {calibration_id}")
    data = json.loads(matches[0].read_text())
    return CalibrationRecord.from_dict(data)


def _stored_records(paths: Iterable[Path]) -> Iterator[CalibrationRecord]:
    """The parseable records among ``paths``; each unreadable one is journaled, then skipped."""
    for path in paths:
        try:
            record = CalibrationRecord.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            log_event(
                logger,
                "correction.calibration_record_unreadable",
                level=logging.WARNING,
                path=str(path),
                reason=type(exc).__name__,
            )
            continue
        yield record


def find_stored_calibration(
    *,
    provider: str,
    model_key: str,
    serial: str,
    orientation: str = "unknown",
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord | None:
    """Newest stored calibration for this unit; unknown orientation accepts either."""
    sh = serial_hash(serial)
    if not sh:
        return None
    hashes: list[str | None] = [sh]
    normalized = re.sub(r"\s+", "", serial)
    if (
        provider == "minidsp" and model_key in ("minidsp_umik1", "minidsp_umik2")
        and re.fullmatch(r"[0-9]{3}-?[0-9]{4}", normalized)
    ):
        # UMIK labels use ###-####; calibration headers use #######.
        digits = normalized.replace("-", "")
        hashes.extend(serial_hash(value) for value in (digits, f"{digits[:3]}-{digits[3:]}"))
    model_dir = root / _slug(provider) / _slug(model_key)
    matches = (
        rec for rec in _stored_records(model_dir.glob("*.json"))
        if rec.serial_hash in hashes and orientation in ("unknown", rec.orientation)
    )
    return max(matches, key=lambda rec: rec.fetched_at, default=None)


def find_stored_calibration_by_content_hash(
    *,
    file_sha256: str,
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord | None:
    """A stored calibration matching this content hash, regardless of provider,
    model, or serial.

    The additive counterpart to :func:`find_stored_calibration`: a manual upload
    carries no serial, so only the content hash of the file that produced it can
    reach it again. Used by ``jasper.audio_measurement.household_mic`` to resolve a
    remembered upload back to its stored file. Corrupt records are skipped, not
    fatal; returns the most recently fetched match.
    """
    if not file_sha256:
        return None
    matches = (
        rec for rec in _stored_records(root.glob("*/*/*.json"))
        if rec.file_sha256 == file_sha256
    )
    return max(matches, key=lambda rec: rec.fetched_at, default=None)


def configured_calibration_root() -> Path:
    """The calibration store this speaker actually uses.

    ``DEFAULT_CALIBRATION_DIR`` is only the default: the measurement daemon
    resolves its root through ``JASPER_CORRECTION_CALIBRATION_DIR``, and a
    reader that ignored the override would scan an empty directory.
    """
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_CALIBRATION_DIR", str(DEFAULT_CALIBRATION_DIR),
        )
    )
