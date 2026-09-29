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
import http.client
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
from jasper.platform.log_event import log_event

from .mic_calibration import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)


class CalibrationLookupError(RuntimeError):
    """Raised when a vendor lookup did not return a usable cal file."""


class CalibrationNotFoundError(CalibrationLookupError):
    """Vendor lookup completed but no calibration exists for the serial."""


class CalibrationUpstreamError(CalibrationLookupError):
    """Vendor lookup could not be completed because the provider failed."""


class CalibrationTooLargeError(CalibrationUpstreamError):
    """The vendor answered with more than :data:`MAX_UPLOAD_BYTES`."""


class CalibrationLinkRefused(CalibrationUpstreamError):
    """The vendor sends this lookup somewhere other than its own https host."""


UrlOpen = Callable[[urllib.request.Request | str, float], bytes]

#: Every way a request can fail short of an answer. ``IncompleteRead`` and its
#: ``http.client`` siblings are not ``OSError``s; a link urllib cannot encode
#: raises ``UnicodeError``.
_FETCH_ERRORS = (OSError, http.client.HTTPException, UnicodeError)


def _on_vendor_host(base_url: str, link: str) -> bool:
    """Whether ``link`` is https to ``base_url``'s own host and port.

    urljoin lets an absolute href, and a server its redirect, replace the
    scheme and host, so a vendor could otherwise send this lookup to a local
    file, a LAN or loopback service, or over cleartext.
    """
    try:
        base, target = urllib.parse.urlsplit(base_url), urllib.parse.urlsplit(link)
        base_port, target_port = base.port or 443, target.port or 443
    except ValueError:
        return False
    return (
        target.scheme == "https"
        and (target.hostname or "").rstrip(".") == (base.hostname or "").rstrip(".")
        and target_port == base_port
    )


class _VendorHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only where :func:`_on_vendor_host` follows a link."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _on_vendor_host(req.full_url, newurl):
            raise CalibrationLinkRefused(
                "the vendor redirected this lookup off its own https host"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_VendorHostRedirects)


def _default_urlopen(req: urllib.request.Request | str, timeout: float) -> bytes:
    with _OPENER.open(req, timeout=timeout) as resp:
        body = resp.read(MAX_UPLOAD_BYTES + 1)
        owed = getattr(resp, "length", None)
    if len(body) > MAX_UPLOAD_BYTES:
        raise CalibrationTooLargeError(
            f"the vendor answered with more than {MAX_UPLOAD_BYTES} bytes"
        )
    if owed:
        # A capped read stops short without raising when the body is cut off;
        # the bytes its declared length still owes say so.
        raise http.client.IncompleteRead(body, owed)
    return body


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
        try:
            resolved = urllib.parse.urljoin(base_url, href)
            split = urllib.parse.urlsplit(href.lower())
        except ValueError:
            continue
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
    except _FETCH_ERRORS as e:
        raise CalibrationUpstreamError(f"Dayton lookup failed: {e}") from e
    text = _decode_body(body)
    if "Unable To find a Calibration File" in text:
        raise CalibrationNotFoundError(
            f"Dayton did not find {vendor_model} serial {serial.strip()}"
        )
    if _looks_like_calibration(text):
        return text, url
    links = _extract_links(url, text)
    followable = [link for link in links if _on_vendor_host(url, link)]
    if links and not followable:
        raise CalibrationLinkRefused(
            "Dayton's page links its calibration file off its own https host"
        )
    for link in followable:
        try:
            linked = _decode_body(opener(link, timeout))
        except _FETCH_ERRORS:
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
    for url in _minidsp_candidate_urls(vendor_model, serial, orientation=orientation):
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
        except _FETCH_ERRORS as e:
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


def lookup_invalid(model_key: str, serial: str) -> str | None:
    """Why no vendor lookup can be made for ``model_key`` and ``serial``, or None."""
    spec = SUPPORTED_MODELS.get(model_key)
    if spec is None:
        return f"unsupported calibration model: {model_key}"
    if not serial.strip():
        return "serial number is required"
    if spec["provider"] == "minidsp" and not _minidsp_candidate_urls(spec["vendor_model"], serial):
        return "miniDSP serial must contain digits"
    return None


def fetch_vendor_calibration(
    *,
    model_key: str,
    serial: str,
    orientation: str = "unknown",
    root: Path = DEFAULT_CALIBRATION_DIR,
    opener: UrlOpen | None = None,
) -> CalibrationRecord:
    problem = lookup_invalid(model_key, serial)
    if problem:
        raise ValueError(problem)
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
