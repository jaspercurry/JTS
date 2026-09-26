# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fetch a measurement mic's calibration file from its vendor, by serial.

The network half of ``jasper-mic-calibration fetch``: Dayton Audio's lookup
form and miniDSP's per-serial files. What a fetch returns is parsed and filed
by :mod:`jasper.audio_measurement.calibration`.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from jasper.audio_measurement.calibration import (
    DEFAULT_CALIBRATION_DIR,
    CalibrationRecord,
    find_stored_calibration,
    parse_calibration_text,
    serial_hash,
    store_calibration,
)
from jasper.audio_measurement.mic_identity import (
    DEFAULT_SIGN_CONVENTION,
    SUPPORTED_MODELS,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


class CalibrationLookupError(RuntimeError):
    """Raised when a vendor lookup did not return a usable cal file."""


class CalibrationNotFoundError(CalibrationLookupError):
    """Vendor lookup completed but no calibration exists for the serial."""


class CalibrationUpstreamError(CalibrationLookupError):
    """Vendor lookup could not be completed because the provider failed."""


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
        # scheme, so a `file://…txt` link in the external vendor response would
        # otherwise read a local file.
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
    # pairing avoids a wasted round-trip. The legacy /images/umik... family
    # answers 404 for every UMIK-2 serial; one dir is kept below as drift
    # insurance.
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
        # site-wide (a 403, not the real 404), so every request needs an
        # explicit non-default header.
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
