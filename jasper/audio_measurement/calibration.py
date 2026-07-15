# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-microphone calibration registry and parser.

The correction wizard supports two happy paths:

* known vendor lookup, where the user enters a model + serial and JTS
  fetches the calibration file server-side; and
* bring-your-own calibrated mic, where the user uploads a REW/
  HouseCurve-style text curve.

Every input path normalizes into ``correction_db``: an additive dB
offset applied to the measured response before target normalization.
Provider-specific quirks stay here so the DSP pipeline only sees one
shape.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_CALIBRATION_DIR = Path("/var/lib/jasper/correction/calibration_mics")
# Single source of truth for supported measurement mics. Adding a mic here
# wires the vendor lookup, the model picker, the wrong-mic guard, AND the
# wizard's label-based auto-inference (see model_label_aliases). Optional
# `label_aliases` overrides the default (the vendor_model) when a mic's OS
# device label doesn't contain its vendor model string.
SUPPORTED_MODELS: dict[str, dict[str, Any]] = {
    "dayton_imm6": {
        "provider": "dayton_audio",
        "vendor_model": "iMM-6",
        "label": "Dayton Audio iMM-6 / iMM-6C",
    },
    "dayton_umm6": {
        "provider": "dayton_audio",
        "vendor_model": "UMM-6",
        "label": "Dayton Audio UMM-6",
    },
    "minidsp_umik1": {
        "provider": "minidsp",
        "vendor_model": "umik-1",
        "label": "miniDSP UMIK-1",
    },
    "minidsp_umik2": {
        "provider": "minidsp",
        "vendor_model": "umik-2",
        "label": "miniDSP UMIK-2",
    },
}


def model_label_aliases(model_key: str) -> list[str]:
    """OS device-label tokens that identify this mic for the wizard's
    label-based auto-inference (e.g. ``iMM-6`` matches a browser-reported
    label of ``iMM-6C``). Defaults to the vendor model; a registry entry may
    set ``label_aliases`` for mics whose OS label differs from the model name.
    The wizard matches case- and punctuation-insensitively, so aliases need
    only be a distinctive substring of the device label.
    """
    spec = SUPPORTED_MODELS.get(model_key, {})
    aliases = spec.get("label_aliases") or [spec.get("vendor_model", "")]
    return [str(a) for a in aliases if a]


def supported_model_options() -> tuple[dict[str, Any], ...]:
    """Public, UI-safe model picker options derived from SUPPORTED_MODELS.

    Keep browser surfaces data-driven from this registry. The phone relay page
    uses these options via CaptureSpec so adding a supported measurement mic is a
    registry edit, not a separate Cloudflare page edit.
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
    phase_deg: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs_hz": self.freqs_hz,
            "correction_db": self.correction_db,
            "phase_deg": self.phase_deg,
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
        phase = None
        if data.get("phase_deg") is not None:
            phase = numeric_array("phase_deg")
            if len(phase) != len(freqs):
                raise ValueError("calibration curve phase must match frequency length")
        return cls(freqs_hz=freqs, correction_db=correction, phase_deg=phase)


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

        Vendor lookup URLs often include the mic serial in the file
        name, so do not expose the raw source. The full calibration
        curve/file is stored separately in the session bundle when a
        measurement uses it.
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
    """Redact source details that may carry serial numbers.

    We keep enough provenance for UX/debugging while avoiding raw
    vendor URLs in public metadata and bundles.
    """
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

    Accepted rows start with a numeric frequency and contain at least
    frequency + dB. A third numeric column is treated as phase degrees.

    ``sign_convention``:
      - ``correction`` means the second column is already the dB value
        to add to the measured response.
      - ``response`` means the second column is the mic response, so
        the correction is the negated value.
    """
    if sign_convention not in {"correction", "response"}:
        raise ValueError(
            "sign_convention must be 'correction' or 'response'"
        )

    rows: list[tuple[float, float, float | None]] = []
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
            phase = float(nums[2]) if len(nums) >= 3 else None
        except ValueError:
            continue
        if not np.isfinite(freq) or not np.isfinite(gain) or freq <= 0:
            continue
        if phase is not None and not np.isfinite(phase):
            phase = None
        correction = -gain if sign_convention == "response" else gain
        rows.append((freq, correction, phase))

    if len(rows) < 2:
        raise ValueError("calibration file must contain at least 2 rows")

    rows.sort(key=lambda r: r[0])
    deduped: list[tuple[float, float, float | None]] = []
    for row in rows:
        if deduped and abs(row[0] - deduped[-1][0]) < 1e-9:
            deduped[-1] = row
        else:
            deduped.append(row)
    if len(deduped) < 2:
        raise ValueError("calibration file must contain at least 2 frequencies")

    phase_values = [r[2] for r in deduped]
    phase = (
        [float(p) if p is not None else 0.0 for p in phase_values]
        if any(p is not None for p in phase_values)
        else None
    )
    return CalibrationCurve(
        freqs_hz=[float(r[0]) for r in deduped],
        correction_db=[float(r[1]) for r in deduped],
        phase_deg=phase,
    )


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
        # Only ever follow http(s). urljoin lets an absolute href override
        # the scheme, so without this guard a `file://…txt` or
        # `http://127.0.0.1…txt` link in the (external, semi-trusted) vendor
        # response would be fetched by the Pi's web process — an SSRF/LFI
        # sink. A cross-host CDN file is still https, so this does not
        # constrain legitimate vendor hosting.
        if urllib.parse.urlsplit(resolved).scheme not in ("http", "https"):
            continue
        split = urllib.parse.urlsplit(href.lower())
        # The calibration filename can live in the URL path (…/abc.txt) or,
        # as Dayton's tool does, only in a query parameter
        # (…/Download?CalibrationFileName=abc.txt&CalibrationFilePath=…txt).
        # Checking the path alone silently drops Dayton's real download link
        # and the whole serial lookup fails with "did not return a parseable
        # calibration file".
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

    Dayton's public tool is a regular form POST. If the response is a
    page, we scrape calibration-file links and fetch the first parseable
    one. If Dayton ever returns the text file directly, that path works
    too.
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
    # UMIK ships 0-degree + 90-degree files. Default to 0-degree for
    # two-channel room correction, but include the other orientation
    # as a fallback candidate.
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

    # UMIK-2 serves calibration files through per-orientation PHP scripts,
    # each of which only accepts its own suffix — umik.php ONLY resolves
    # "<serial>.txt" (0-degree) and umik90.php ONLY resolves
    # "<serial>_90deg.txt" (90-degree). Crossing the pairing (e.g.
    # umik.php/<serial>_90deg.txt) returns HTTP 200 with an "Unable to
    # locate calibration data" page rather than a 404, so getting the
    # pairing right avoids a wasted round-trip; _looks_like_calibration
    # still guards against ever accepting that error page.
    # Verified live 2026-07-15 against a real UMIK-2 (serial 8108494):
    # both script URLs return 200 with real cal data, while every
    # /images/umik... family URL below now 404s. The legacy family is
    # kept as a trailing fallback only in case the site reverts.
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
    legacy_dirs = [
        "https://www.minidsp.com/images/umik/",
        "https://www.minidsp.com/images/umik/Umik-2/",
        "https://www.minidsp.com/images/umik/UMIK-2/",
        "https://www.minidsp.com/images/umik-2/",
    ]
    return [base + suffix for base, suffix in scripts] + [
        base + suffix for base in legacy_dirs for suffix in legacy_suffixes
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

    miniDSP officially documents the product-page serial form. The
    underlying static URLs have been stable for years, so we try the
    known URL families first and fall back to an actionable error if
    none returns a parseable file.
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
        # request needs an explicit non-default header, same as the Dayton path.
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
    """Return a previously-stored vendor calibration matching this
    serial + model + orientation, or None. A measurement mic's calibration is
    fixed per unit, so the stored copy is authoritative — this lets a repeat
    lookup skip the vendor round-trip (resilient to the vendor being down, and
    faster). Returns the most recently fetched match. Corrupt records are
    skipped, not fatal.
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
        if str(data.get("orientation") or "unknown") != orientation:
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
    # (e.g. the wizard auto-fetching a remembered serial) never depends on the
    # vendor being reachable.
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
            sign_convention="correction",
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
