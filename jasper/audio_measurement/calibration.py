# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-microphone calibration registry and parser.

Two input paths -- known-vendor serial lookup and a bring-your-own uploaded
REW/HouseCurve-style text curve -- normalize into ``correction_db``: an
additive dB offset applied to the measured response before target
normalization.

The quirk that matters is the SIGN. A measurement mic's calibration file states
the microphone's own *response*, so the correction is its negation; the
per-vendor declaration is in ``SUPPORTED_MODELS``. Records written before
2026-07-27 stored vendor files under the opposite claim, and
``migrate_stored_sign_conventions`` repairs them in place on deploy.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from jasper.atomic_io import atomic_write_text

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
from jasper.log_event import log_event

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


def supported_model_options() -> tuple[dict[str, Any], ...]:
    """Public, UI-safe model picker options derived from SUPPORTED_MODELS.

    The capture page consumes these via CaptureSpec, so adding a supported
    measurement mic is a registry edit, not a separate page edit.
    """
    return tuple(
        {
            "key": key,
            "label": str(spec["label"]),
            "aliases": model_label_aliases(key),
        }
        for key, spec in SUPPORTED_MODELS.items()
    )


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
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not np.isfinite(float(value))
                for value in raw
            ):
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


class CalibrationLookupError(RuntimeError):
    """Raised when a vendor lookup did not return a usable cal file."""


class CalibrationNotFoundError(CalibrationLookupError):
    """Vendor lookup completed but no calibration exists for the serial."""


class CalibrationUpstreamError(CalibrationLookupError):
    """Vendor lookup could not be completed because the provider failed."""


def serial_hash(serial: str | None) -> str | None:
    if not serial:
        return None
    normalized = re.sub(r"\s+", "", serial.strip().lower())
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


#: What a verb refuses with when no absolute reference can be read, and the
#: sentence it prints. One owner for both: two operator verbs ask this question.
REFUSE_MIC_CALIBRATION_UNAVAILABLE = "mic_calibration_unavailable"
MIC_CALIBRATION_UNAVAILABLE_DETAIL = (
    "no parseable 'Sens Factor' calibration for this microphone — pass "
    "--calibration-file, or store the vendor file via the /correction "
    "wizard. Absolute SPL is never guessed."
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

    ``None`` when no calibration can be read -- the caller REFUSES with
    :data:`REFUSE_MIC_CALIBRATION_UNAVAILABLE`; a guessed sensitivity would
    silently mis-scale every SPL decision.
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
    file_hash = _sha256_text(text)
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
    raw_path.write_text(text)
    raw_path.chmod(0o600)

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
    metadata_path.write_text(json.dumps(record.to_dict(), indent=2))
    metadata_path.chmod(0o600)
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


def preview_curve(
    curve: CalibrationCurve,
    *,
    max_points: int = 80,
) -> dict[str, list[float]]:
    freqs = np.asarray(curve.freqs_hz, dtype=np.float64)
    corr = np.asarray(curve.correction_db, dtype=np.float64)
    if len(freqs) > max_points:
        idx = np.unique(
            np.round(np.linspace(0, len(freqs) - 1, max_points)).astype(int)
        )
        freqs = freqs[idx]
        corr = corr[idx]
    return {
        "freqs_hz": [float(x) for x in freqs],
        "correction_db": [float(x) for x in corr],
    }


UrlOpen = Callable[[urllib.request.Request | str, float], bytes]


def _default_urlopen(req: urllib.request.Request | str, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _decode_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _looks_like_calibration(text: str) -> bool:
    try:
        parse_calibration_text(text)
    except ValueError:
        return False
    return True


_CALIBRATION_SUFFIXES = (".txt", ".cal", ".frd", ".csv", ".omm")


def _extract_links(base_url: str, text: str) -> list[str]:
    links: list[str] = []
    for raw in re.findall(r"""href=["']([^"']+)["']""", text, flags=re.I):
        href = html.unescape(raw)
        resolved = urllib.parse.urljoin(base_url, href)
        # Only ever follow http(s): urljoin lets an absolute href override the
        # scheme, so a `file://…txt` or `http://127.0.0.1…txt` link in the
        # external vendor response would otherwise be an SSRF/LFI sink. A
        # cross-host CDN file is still https, so legitimate hosting still works.
        if urllib.parse.urlsplit(resolved).scheme not in ("http", "https"):
            continue
        split = urllib.parse.urlsplit(href.lower())
        # The calibration filename can live in the URL path (…/abc.txt) or, as
        # Dayton's tool does, only in a query parameter
        # (…/Download?CalibrationFileName=abc.txt&…), so both are checked.
        candidates = [split.path]
        candidates.extend(value for _key, value in urllib.parse.parse_qsl(split.query))
        if any(c.endswith(_CALIBRATION_SUFFIXES) for c in candidates):
            links.append(resolved)
    return links


def fetch_dayton_calibration_text(
    *,
    vendor_model: str,
    serial: str,
    opener: UrlOpen | None = None,
    timeout: float = 15.0,
) -> tuple[str, str]:
    """Fetch a Dayton Audio mic calibration file.

    Dayton's public tool is a regular form POST; a page response is scraped for
    calibration-file links, and a direct text-file response works too.
    """
    opener = opener or _default_urlopen
    url = "https://support.daytonaudio.com/MicrophoneCalibrationTool"
    data = urllib.parse.urlencode({
        "Microphone": vendor_model,
        "SerialNumber": serial.strip(),
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "JTS correction calibration lookup",
        },
        method="POST",
    )
    try:
        body = opener(req, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CalibrationUpstreamError(f"Dayton lookup failed: {e}") from e
    text = _decode_body(body)
    if "Unable To find a Calibration File" in text:
        raise CalibrationNotFoundError(
            f"Dayton did not find {vendor_model} serial {serial.strip()}"
        )
    if _looks_like_calibration(text):
        return text, url
    for link in _extract_links(url, text):
        try:
            linked = _decode_body(opener(link, timeout))
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        if _looks_like_calibration(linked):
            return linked, link
    raise CalibrationUpstreamError(
        "Dayton lookup did not return a parseable calibration file"
    )


def _minidsp_candidate_urls(
    vendor_model: str,
    serial: str,
    *,
    orientation: str = "unknown",
) -> list[str]:
    digits = re.sub(r"[^0-9]", "", serial)
    if not digits:
        return []
    # UMIK ships 0-degree + 90-degree files. Default to 0-degree for two-channel
    # room correction, with the other orientation as a fallback candidate.
    if vendor_model == "umik-1":
        suffixes = (
            [f"{digits}_90deg.txt", f"{digits}.txt"]
            if orientation == "90deg"
            else [f"{digits}.txt", f"{digits}_90deg.txt"]
        )
        # The legacy UMIK-1 direct path is /images/umik/<sn>.txt; keep
        # model-specific folders as secondary probes for site drift.
        dirs = [
            "https://www.minidsp.com/images/umik/",
            "https://www.minidsp.com/images/umik/Umik-1/",
            "https://www.minidsp.com/images/umik/UMIK-1/",
        ]
        return [base + suffix for base in dirs for suffix in suffixes]

    # UMIK-2 serves calibration files through per-orientation PHP scripts, each
    # of which accepts only its own suffix: umik.php ONLY "<serial>.txt"
    # (0-degree), umik90.php ONLY "<serial>_90deg.txt" (90-degree). Crossing the
    # pairing returns HTTP 200 with an error page rather than a 404, so the
    # pairing avoids a wasted round-trip. Verified live 2026-07-15 against a real
    # UMIK-2; the legacy /images/umik... family is dead (404 for every serial),
    # one dir kept below as drift insurance.
    scripts = [
        ("https://www.minidsp.com/scripts/umik2cal/umik.php/", f"{digits}.txt"),
        (
            "https://www.minidsp.com/scripts/umik2cal/umik90.php/",
            f"{digits}_90deg.txt",
        ),
    ]
    if orientation == "90deg":
        scripts.reverse()
    legacy_suffixes = (
        [f"{digits}_90deg.txt", f"{digits}.txt"]
        if orientation == "90deg"
        else [f"{digits}.txt", f"{digits}_90deg.txt"]
    )
    return [base + suffix for base, suffix in scripts] + [
        "https://www.minidsp.com/images/umik/" + suffix
        for suffix in legacy_suffixes
    ]


def fetch_minidsp_calibration_text(
    *,
    vendor_model: str,
    serial: str,
    orientation: str = "unknown",
    opener: UrlOpen | None = None,
    timeout: float = 15.0,
) -> tuple[str, str]:
    """Fetch a miniDSP UMIK calibration file by serial.

    The known static URL families are tried first, falling back to an actionable
    error if none returns a parseable file.
    """
    opener = opener or _default_urlopen
    errors: list[str] = []
    saw_not_found = False
    candidates = _minidsp_candidate_urls(
        vendor_model, serial, orientation=orientation,
    )
    if not candidates:
        raise ValueError("miniDSP serial must contain digits")
    for url in candidates:
        # miniDSP blanket-blocks urllib's default "Python-urllib/x.y" User-Agent
        # site-wide (verified live 2026-07-15: 403, not the real 404), so every
        # request needs an explicit non-default header.
        req = urllib.request.Request(
            url, headers={"User-Agent": "JTS correction calibration lookup"},
        )
        try:
            text = _decode_body(opener(req, timeout))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                saw_not_found = True
            else:
                errors.append(f"HTTP {e.code}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(str(e))
            continue
        if _looks_like_calibration(text):
            return text, url
    detail = f" ({'; '.join(errors[:2])})" if errors else ""
    if saw_not_found and not errors:
        raise CalibrationNotFoundError(
            "miniDSP did not find a calibration file for that serial"
        )
    raise CalibrationUpstreamError(
        "miniDSP lookup did not return a parseable calibration file" + detail
    )


def find_stored_calibration(
    *,
    provider: str,
    model_key: str,
    serial: str,
    orientation: str = "unknown",
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord | None:
    """A stored vendor calibration matching serial + model + orientation.

    A measurement mic's calibration is fixed per unit, so the stored copy is
    authoritative and a repeat lookup skips the vendor round-trip. Returns the
    most recently fetched match, or ``None``; corrupt records are skipped, not
    fatal. ``orientation="unknown"`` (the default) matches ANY stored
    orientation: the write side stamps the REAL inferred orientation, so a
    literal match would permanently miss the cache for the browser capture flow,
    which never declares one. A caller naming "0deg"/"90deg" still matches
    exactly.
    """
    sh = serial_hash(serial)
    if not sh:
        return None
    model_dir = root / _slug(provider) / _slug(model_key)
    best: CalibrationRecord | None = None
    for path in model_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("serial_hash") != sh:
            continue
        stored_orientation = str(data.get("orientation") or "unknown")
        if orientation != "unknown" and stored_orientation != orientation:
            continue
        try:
            rec = CalibrationRecord.from_dict(data)
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or rec.fetched_at > best.fetched_at:
            best = rec
    return best


def find_stored_calibration_by_content_hash(
    *,
    file_sha256: str,
    root: Path = DEFAULT_CALIBRATION_DIR,
) -> CalibrationRecord | None:
    """A stored calibration matching this content hash, regardless of provider,
    model, or serial.

    The additive counterpart to :func:`find_stored_calibration`: a manual upload
    carries no serial, so only the content hash of the file that produced it can
    reach it again. Used by ``jasper.correction.household_mic`` to resolve a
    remembered upload back to its stored file. Corrupt records are skipped, not
    fatal; returns the most recently fetched match.
    """
    if not file_sha256:
        return None
    best: CalibrationRecord | None = None
    for path in root.glob("*/*/*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("file_sha256") != file_sha256:
            continue
        try:
            rec = CalibrationRecord.from_dict(data)
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or rec.fetched_at > best.fetched_at:
            best = rec
    return best


def fetch_vendor_calibration(
    *,
    model_key: str,
    serial: str,
    orientation: str = "unknown",
    root: Path = DEFAULT_CALIBRATION_DIR,
    opener: UrlOpen | None = None,
) -> CalibrationRecord:
    if model_key not in SUPPORTED_MODELS:
        raise ValueError(f"unsupported calibration model: {model_key}")
    if not serial.strip():
        raise ValueError("serial number is required")
    spec = SUPPORTED_MODELS[model_key]
    provider = spec["provider"]
    vendor_model = spec["vendor_model"]
    # serial_hash, never the raw serial — the serial identifies a user's
    # hardware and is treated as private metadata everywhere else.
    log_serial_hash = serial_hash(serial)
    # Re-use a previously-stored calibration for this serial so a repeat lookup
    # never depends on the vendor being reachable.
    cached = find_stored_calibration(
        provider=provider, model_key=model_key, serial=serial,
        orientation=orientation, root=root,
    )
    if cached is not None:
        log_event(
            logger,
            "correction_calibration_lookup",
            provider=provider,
            model=model_key,
            serial_hash=log_serial_hash,
            outcome="cache_hit",
            point_count=cached.point_count,
        )
        return cached
    try:
        if provider == "dayton_audio":
            text, source = fetch_dayton_calibration_text(
                vendor_model=vendor_model,
                serial=serial,
                opener=opener,
            )
        elif provider == "minidsp":
            text, source = fetch_minidsp_calibration_text(
                vendor_model=vendor_model,
                serial=serial,
                orientation=orientation,
                opener=opener,
            )
            # Stamp the orientation the vendor ACTUALLY served, not the
            # pre-fetch hint: every miniDSP candidate URL ends in exactly one of
            # "<serial>.txt" (0-degree) or "<serial>_90deg.txt" (90-degree), so
            # the winning `source` URL is ground truth.
            orientation = "90deg" if source.endswith("_90deg.txt") else "0deg"
        else:
            raise ValueError(f"no fetcher for provider: {provider}")
        record = store_calibration(
            text=text,
            provider=provider,
            model=model_key,
            label=spec["label"],
            source=source,
            serial=serial,
            orientation=orientation,
            # Vendor files are RESPONSE curves; the correction is the negation.
            # The vendor owns this quirk, so the registry states it.
            sign_convention=str(
                spec.get("sign_convention") or DEFAULT_SIGN_CONVENTION
            ),
            root=root,
        )
    except CalibrationNotFoundError:
        log_event(
            logger,
            "correction_calibration_lookup",
            provider=provider,
            model=model_key,
            serial_hash=log_serial_hash,
            outcome="not_found",
        )
        raise
    except CalibrationUpstreamError as e:
        log_event(
            logger,
            "correction_calibration_lookup",
            provider=provider,
            model=model_key,
            serial_hash=log_serial_hash,
            outcome="upstream_error",
            detail=repr(str(e)),
            level=logging.WARNING,
        )
        raise
    log_event(
        logger,
        "correction_calibration_lookup",
        provider=provider,
        model=model_key,
        serial_hash=log_serial_hash,
        outcome="ok",
        point_count=record.point_count,
    )
    return record


def _models_expecting_response() -> set[tuple[str, str]]:
    """``(provider, model)`` pairs the registry declares to be response curves.

    Keyed on the pair, not the provider alone: a provider can hold models that
    disagree, and a provider-level key would silently drag a sibling along.
    """
    return {
        (str(spec["provider"]), model_key)
        for model_key, spec in SUPPORTED_MODELS.items()
        if str(spec.get("sign_convention") or DEFAULT_SIGN_CONVENTION) == "response"
    }


def configured_calibration_root() -> Path:
    """The calibration store this speaker actually uses.

    ``DEFAULT_CALIBRATION_DIR`` is only the default: the ``/correction/`` wizard
    resolves its root through ``JASPER_CORRECTION_CALIBRATION_DIR``. A migration
    that ignored the override would read an empty directory and report
    ``scanned=0`` -- success-shaped, and wrong.
    """
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_CALIBRATION_DIR", str(DEFAULT_CALIBRATION_DIR),
        )
    )


def migrate_stored_sign_conventions(
    *, root: Path | None = None,
) -> dict[str, int]:
    """Repair vendor-fetched records stored under the wrong sign convention.

    Until 2026-07-27 ``fetch_vendor_calibration`` stored every vendor file as
    ``sign_convention="correction"``, so ``correction_db`` held the mic's
    response un-negated and the pipeline added what it should have subtracted.
    Run from ``install.sh`` on every deploy; idempotent.

    * Keyed on the stored convention FIELD, never on the numbers: only a record
      still claiming ``"correction"`` is touched, so a curve can never be
      double-negated back to the bug.
    * Vendor records only, keyed on ``(provider, model)``. A ``manual_upload``
      record carries the household's OWN declaration and is not ours to
      overrule.
    * Re-derived from the retained raw file when its SHA-256 still matches the
      record's ``file_sha256``; otherwise negated in place, which is the same
      number. Re-fetching is impossible: only ``serial_hash`` is persisted.
    * Phase is untouched -- it passes through unchanged under both conventions.

    ONE direction only (``correction`` -> ``response``); a reversal needs its
    own opposite-direction migration, not a re-run of this one. ``root``
    defaults to :func:`configured_calibration_root`. Returns per-outcome counts
    and never raises for one bad record.
    """
    root = configured_calibration_root() if root is None else root
    vendor_models = _models_expecting_response()
    counts = {
        "scanned": 0,
        "migrated_rederived": 0,
        "migrated_negated": 0,
        "already_response": 0,
        "skipped_not_vendor": 0,
        "unreadable": 0,
        "write_failed": 0,
    }
    for path in sorted(root.glob("*/*/*.json")):
        counts["scanned"] += 1
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            counts["unreadable"] += 1
            continue
        if not isinstance(data, dict):
            counts["unreadable"] += 1
            continue
        provider = str(data.get("provider") or "")
        model = str(data.get("model") or "")
        if (provider, model) not in vendor_models:
            counts["skipped_not_vendor"] += 1
            continue
        # Absent reads as "correction": that is what every reader of a
        # legacy record already resolves it to (CalibrationRecord.from_dict).
        stored = str(data.get("sign_convention") or "correction")
        if stored != "correction":
            counts["already_response"] += 1
            continue

        raw_text: str | None = None
        try:
            candidate = path.with_suffix(".txt").read_text()
        except OSError:
            candidate = None
        if candidate is not None and _sha256_text(candidate) == str(
            data.get("file_sha256") or ""
        ):
            raw_text = candidate

        try:
            if raw_text is not None:
                curve = parse_calibration_text(
                    raw_text, sign_convention="response",
                )
                method = "rederived"
            else:
                stored_curve = CalibrationCurve.from_dict(data.get("curve"))
                curve = CalibrationCurve(
                    freqs_hz=list(stored_curve.freqs_hz),
                    correction_db=[-db for db in stored_curve.correction_db],
                )
                method = "negated"
        except (ValueError, TypeError):
            counts["unreadable"] += 1
            continue

        data["curve"] = curve.to_dict()
        data["sign_convention"] = "response"
        data["point_count"] = len(curve.freqs_hz)
        try:
            # Atomic and stat-preserving: a crash mid-migration must leave the
            # OLD record rather than a truncated one, and `preserve_target_stat`
            # keeps the existing owner/mode so this root-run repair cannot
            # re-own a file a de-rooted jasper-correction-web must write.
            atomic_write_text(
                path,
                json.dumps(data, indent=2),
                preserve_target_stat=True,
            )
        except OSError:
            counts["write_failed"] += 1
            continue
        counts[f"migrated_{method}"] += 1
        # WARNING, not INFO: a one-time migration MUTATING household measurement
        # state, and the deploy transcript is where an operator looks. Bounded by
        # the one or two mic records a household owns, so it cannot spam.
        log_event(
            logger,
            "correction_calibration_sign_migrated",
            level=logging.WARNING,
            provider=provider,
            model=model,
            calibration_id=str(data.get("calibration_id") or ""),
            method=method,
            point_count=len(curve.freqs_hz),
        )

    migrated = counts["migrated_rederived"] + counts["migrated_negated"]
    if migrated or counts["unreadable"] or counts["write_failed"]:
        log_event(
            logger,
            "correction_calibration_sign_migration",
            level=logging.WARNING,
            **counts,
        )
    return counts
