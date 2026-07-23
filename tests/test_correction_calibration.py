# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement import calibration


SAMPLE_CAL = """# freq correction phase
20 -2.0 0
100 0.5 12
1000 1.5 20
20000 -1.0 0
"""


def test_parse_calibration_text_accepts_common_curve_shape():
    curve = calibration.parse_calibration_text(SAMPLE_CAL)
    assert curve.freqs_hz == [20.0, 100.0, 1000.0, 20000.0]
    assert curve.correction_db == [-2.0, 0.5, 1.5, -1.0]
    assert curve.phase_deg == [0.0, 12.0, 20.0, 0.0]


def test_parse_calibration_text_can_invert_response_curve():
    curve = calibration.parse_calibration_text(
        "20 -2\n100 3\n",
        sign_convention="response",
    )
    assert curve.correction_db == [2.0, -3.0]


def test_apply_calibration_curve_interpolates_on_measurement_grid():
    curve = calibration.parse_calibration_text("20 -2\n100 0\n1000 2\n")
    freqs = np.array([20.0, 60.0, 100.0, 1000.0])
    mag = np.array([0.0, 0.0, 0.0, 0.0])
    corrected = calibration.apply_calibration_curve(freqs, mag, curve)
    assert corrected[0] == -2.0
    assert corrected[2] == 0.0
    assert corrected[3] == 2.0
    assert -2.0 < corrected[1] < 0.0


def test_apply_calibration_curve_interpolates_in_log_frequency():
    curve = calibration.parse_calibration_text("10 0\n1000 2\n")
    freqs = np.array([100.0])
    corrected = calibration.apply_calibration_curve(
        freqs, np.array([0.0]), curve,
    )
    assert corrected[0] == pytest.approx(1.0)


def test_calibration_curve_from_dict_is_strict_persisted_boundary():
    valid = {
        "freqs_hz": [20, 1000.0],
        "correction_db": [-1, 2.0],
        "phase_deg": [0, 10.0],
        "future_metadata": "allowed",
    }
    assert calibration.CalibrationCurve.from_dict(valid).to_dict() == {
        "freqs_hz": [20.0, 1000.0],
        "correction_db": [-1.0, 2.0],
        "phase_deg": [0.0, 10.0],
    }
    for payload in (
        {**valid, "freqs_hz": [20.0, 20.0]},
        {**valid, "freqs_hz": [20.0, -100.0]},
        {**valid, "freqs_hz": [20.0, float("nan")]},
        {**valid, "freqs_hz": [20.0, "1000"]},
        {**valid, "correction_db": [-1.0]},
        {**valid, "correction_db": [False, 1.0]},
        {**valid, "phase_deg": [0.0]},
    ):
        with pytest.raises(ValueError):
            calibration.CalibrationCurve.from_dict(payload)


def test_store_load_roundtrip_redacts_serial_from_public_metadata(tmp_path: Path):
    record = calibration.store_calibration(
        text=SAMPLE_CAL,
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        serial="SECRET-123",
        root=tmp_path,
    )
    loaded = calibration.load_calibration_record(
        record.calibration_id,
        root=tmp_path,
    )
    assert loaded.calibration_id == record.calibration_id
    assert loaded.curve.freqs_hz == record.curve.freqs_hz
    public = loaded.public_metadata()
    assert public["serial_hash"]
    assert public["source"] == "uploaded_file"
    assert "SECRET" not in str(public)
    assert Path(loaded.raw_path).exists()
    assert Path(loaded.metadata_path).exists()
    assert (Path(loaded.raw_path).stat().st_mode & 0o777) == 0o600
    assert (Path(loaded.metadata_path).stat().st_mode & 0o777) == 0o600


def test_load_calibration_record_rejects_corrupt_persisted_curve(tmp_path: Path):
    record = calibration.store_calibration(
        text=SAMPLE_CAL,
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path,
    )
    metadata_path = Path(record.metadata_path)
    payload = json.loads(metadata_path.read_text())
    payload["curve"]["freqs_hz"][1] = "100"
    metadata_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="finite numbers"):
        calibration.load_calibration_record(record.calibration_id, root=tmp_path)


def test_dayton_fetch_posts_form_and_follows_calibration_link():
    calls: list[urllib.request.Request | str] = []

    def fake_open(req, timeout):
        calls.append(req)
        if isinstance(req, urllib.request.Request):
            data = urllib.parse.parse_qs(req.data.decode())
            assert data["Microphone"] == ["UMM-6"]
            assert data["SerialNumber"] == ["ABC123"]
            return b'<html><a href="/files/umm6_abc123.txt">cal</a></html>'
        assert req == "https://support.daytonaudio.com/files/umm6_abc123.txt"
        return b"20 -1\n100 0\n1000 1\n"

    text, source = calibration.fetch_dayton_calibration_text(
        vendor_model="UMM-6",
        serial="ABC123",
        opener=fake_open,
    )
    assert "1000 1" in text
    assert source.endswith("umm6_abc123.txt")
    assert len(calls) == 2


def test_dayton_fetch_follows_query_param_download_link():
    """Regression: Dayton's tool returns the calibration filename only in a
    query parameter (…/Download?CalibrationFileName=cmm31555.txt&…), not in
    the URL path. _extract_links must detect that; otherwise every real
    Dayton serial lookup fails with "did not return a parseable calibration
    file" even though the form found the file. Reproduces the cmm31555 iMM-6C
    failure observed on hardware 2026-06-04.
    """
    calls: list[urllib.request.Request | str] = []

    def fake_open(req, timeout):
        calls.append(req)
        if isinstance(req, urllib.request.Request):
            data = urllib.parse.parse_qs(req.data.decode())
            assert data["Microphone"] == ["iMM-6"]
            assert data["SerialNumber"] == ["cmm31555"]
            # Exact anchor shape returned by Dayton's live tool: filename is
            # in the query string, path is the extension-less Download route.
            return (
                b'<html><a href="/MicrophoneCalibrationTool/Download?'
                b"CalibrationFileName=cmm31555.txt&amp;"
                b"CalibrationFilePath=~%2Fcontent%2Fdata%2F"
                b"MicrophoneCalibrations%2FiMM-6%2Fcmm31555.txt&amp;"
                b"CalibrationFileReady=True&amp;Microphone=iMM-6&amp;"
                b'SerialNumber=cmm31555">cmm31555.txt</a></html>'
            )
        # The followed link must carry the original query params intact.
        assert "CalibrationFileName=cmm31555.txt" in req
        return b"*1000Hz\t-38.2\n\n20.00\t-0.1\n1000\t0.0\n20000\t-2.5\n"

    text, source = calibration.fetch_dayton_calibration_text(
        vendor_model="iMM-6",
        serial="cmm31555",
        opener=fake_open,
    )
    assert "20.00" in text
    assert "Download" in source
    assert len(calls) == 2


def test_dayton_fetch_never_follows_non_http_links():
    """SSRF/LFI guard: a non-http(s) link in the (external) vendor response
    must never be fetched. urljoin lets an absolute href override the scheme,
    so without the guard a file:// link would be opened by the Pi's web
    process.
    """
    followed: list[str] = []

    def fake_open(req, timeout):
        if isinstance(req, urllib.request.Request):
            # Only link is a file:// URL whose path ends in a cal suffix.
            return b'<html><a href="file:///etc/passwd.txt">x</a></html>'
        followed.append(req)
        return b"20 -1\n100 0\n1000 1\n"

    with pytest.raises(calibration.CalibrationUpstreamError):
        calibration.fetch_dayton_calibration_text(
            vendor_model="iMM-6",
            serial="cmm31555",
            opener=fake_open,
        )
    assert followed == []  # the file:// link was never opened


def test_minidsp_fetch_uses_serial_url_candidates():
    seen: list[str] = []

    def fake_open(req, timeout):
        assert isinstance(req, urllib.request.Request)
        seen.append(req.full_url)
        if req.full_url.endswith("/7001234.txt"):
            return b"20 -1\n100 0\n1000 1\n"
        raise OSError("not found")

    text, source = calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-1",
        serial="700-1234",
        opener=fake_open,
    )
    assert "1000 1" in text
    assert source.endswith("/7001234.txt")
    assert seen[0] == "https://www.minidsp.com/images/umik/7001234.txt"


def test_minidsp_fetch_prefers_90deg_file_when_requested():
    seen: list[str] = []

    def fake_open(req, timeout):
        assert isinstance(req, urllib.request.Request)
        seen.append(req.full_url)
        if req.full_url.endswith("/7001234_90deg.txt"):
            return b"20 -1\n100 0\n1000 1\n"
        raise OSError("not found")

    _text, source = calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-1",
        serial="700-1234",
        orientation="90deg",
        opener=fake_open,
    )
    assert source.endswith("/7001234_90deg.txt")
    assert seen[0].endswith("/7001234_90deg.txt")


def test_minidsp_requests_carry_a_non_default_user_agent():
    """miniDSP blanket-blocks urllib's default Python-urllib/x.y User-Agent
    site-wide (verified live 2026-07-15: HTTP 403 on both the /images/ and
    /scripts/ families). Every candidate request must carry an explicit
    header so the fetch doesn't get bot-blocked before it even reaches a
    real 404-vs-200 outcome.
    """
    seen_headers: list[dict] = []

    def fake_open(req, timeout):
        assert isinstance(req, urllib.request.Request)
        seen_headers.append(dict(req.header_items()))
        if req.full_url.endswith("/7001234.txt"):
            return b"20 -1\n100 0\n1000 1\n"
        raise OSError("not found")

    calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-1",
        serial="700-1234",
        opener=fake_open,
    )
    assert seen_headers
    for headers in seen_headers:
        ua = headers.get("User-agent", "")
        assert ua and "python-urllib" not in ua.lower()


def test_minidsp_umik2_candidates_try_scripts_endpoints_first():
    """The 2026-07-15 bug: every /images/umik... candidate 404s for UMIK-2.
    Verified live against a real UMIK-2 that the actual endpoints are the
    per-orientation PHP scripts, and that each script only accepts its own
    suffix (umik.php <-> "<serial>.txt", umik90.php <-> "<serial>_90deg.txt").
    Scripts endpoints must be probed first, with one legacy /images/ dir kept
    only as a trailing fallback.
    """
    urls = calibration._minidsp_candidate_urls(
        "umik-2", "810-1234", orientation="unknown",
    )
    assert urls == [
        "https://www.minidsp.com/scripts/umik2cal/umik.php/8101234.txt",
        "https://www.minidsp.com/scripts/umik2cal/umik90.php/8101234_90deg.txt",
        "https://www.minidsp.com/images/umik/8101234.txt",
        "https://www.minidsp.com/images/umik/8101234_90deg.txt",
    ]


def test_minidsp_umik2_candidates_respect_orientation_priority():
    urls = calibration._minidsp_candidate_urls(
        "umik-2", "810-1234", orientation="90deg",
    )
    assert urls[0] == (
        "https://www.minidsp.com/scripts/umik2cal/umik90.php/8101234_90deg.txt"
    )
    assert urls[1] == "https://www.minidsp.com/scripts/umik2cal/umik.php/8101234.txt"


def test_minidsp_umik2_fetch_uses_scripts_endpoint():
    seen: list[str] = []

    def fake_open(req, timeout):
        seen.append(req.full_url)
        if req.full_url == "https://www.minidsp.com/scripts/umik2cal/umik.php/8101234.txt":
            return b"20 -1\n100 0\n1000 1\n"
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    text, source = calibration.fetch_minidsp_calibration_text(
        vendor_model="umik-2",
        serial="810-1234",
        opener=fake_open,
    )
    assert "1000 1" in text
    assert source == "https://www.minidsp.com/scripts/umik2cal/umik.php/8101234.txt"
    assert seen[0] == source  # scripts endpoint tried first, no wasted round-trip


def test_minidsp_umik2_fetch_rejects_http_200_error_page():
    """The scripts endpoints return HTTP 200 with an HTML "Unable to locate
    calibration data" page for an unknown serial (verified live 2026-07-15),
    never a 404. _looks_like_calibration must reject that body so the fetch
    falls through the remaining candidates; with the legacy fallbacks
    404ing, the outcome is CalibrationNotFoundError, not a stored HTML page.
    """
    seen: list[str] = []
    error_page = (
        b'<p style="color:red;">Unable to locate calibration data. Please '
        b'contact <a href="https://minidsp.desk.com">miniDSP support</a>.</p>'
    )

    def fake_open(req, timeout):
        seen.append(req.full_url)
        if "/scripts/umik2cal/" in req.full_url:
            return error_page
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    with pytest.raises(calibration.CalibrationNotFoundError):
        calibration.fetch_minidsp_calibration_text(
            vendor_model="umik-2",
            serial="810-1234",
            opener=fake_open,
        )
    # Every candidate was tried — the 200 error page was not accepted.
    assert len(seen) == 4


def test_fetch_vendor_calibration_stores_known_mic_record(tmp_path: Path):
    def fake_open(req, timeout):
        return b"20 -1\n100 0\n1000 1\n"

    record = calibration.fetch_vendor_calibration(
        model_key="minidsp_umik1",
        serial="700-1234",
        root=tmp_path,
        opener=fake_open,
    )
    assert record.provider == "minidsp"
    assert record.model == "minidsp_umik1"
    assert record.source.endswith("/7001234.txt")
    public = record.public_metadata()
    assert public["source"] == "vendor_lookup"
    assert "7001234" not in str(public)
    assert record.serial_hash
    assert Path(record.raw_path).exists()


# --- F1: registry-driven label inference -----------------------------------
def test_model_label_aliases_default_and_unknown():
    assert calibration.model_label_aliases("dayton_imm6") == ["iMM-6"]
    assert calibration.model_label_aliases("minidsp_umik2") == ["umik-2"]
    assert calibration.model_label_aliases("nope") == []  # no crash on unknown


# --- mic_tier_for_model: correction-envelope trust-tier resolution (#1668 PR-B)
def test_mic_tier_for_model_known_reference_mics():
    assert calibration.mic_tier_for_model("minidsp_umik1") == "reference"
    assert calibration.mic_tier_for_model("minidsp_umik2") == "reference"


def test_mic_tier_for_model_known_consumer_mics():
    assert calibration.mic_tier_for_model("dayton_imm6") == "consumer"
    assert calibration.mic_tier_for_model("dayton_umm6") == "consumer"


def test_mic_tier_for_model_other_is_consumer():
    assert calibration.mic_tier_for_model("other") == "consumer"


def test_mic_tier_for_model_none_is_phone_the_most_conservative():
    assert calibration.mic_tier_for_model(None) == "phone"


def test_mic_tier_for_model_unknown_key_falls_back_to_consumer_not_a_crash():
    assert calibration.mic_tier_for_model("some_future_mic_not_yet_registered") == "consumer"


def test_supported_models_every_entry_declares_a_valid_tier():
    valid_tiers = {"reference", "consumer", "phone"}
    for key, spec in calibration.SUPPORTED_MODELS.items():
        assert spec.get("tier") in valid_tiers, key


# --- F2: repeat lookup re-uses the stored calibration (no vendor round-trip)
def test_fetch_vendor_calibration_reuses_stored_record(tmp_path: Path):
    calls = {"n": 0}

    def fake_open(req, timeout):
        calls["n"] += 1
        if isinstance(req, urllib.request.Request):
            return (
                b'<html><a href="/MicrophoneCalibrationTool/Download?'
                b"CalibrationFileName=cmm31555.txt&amp;"
                b'CalibrationFilePath=~%2Fx%2Fcmm31555.txt">dl</a></html>'
            )
        return b"*1000Hz\t-38.2\n\n20.00\t-0.1\n1000\t0.0\n20000\t-2.5\n"

    r1 = calibration.fetch_vendor_calibration(
        model_key="dayton_imm6", serial="cmm31555", root=tmp_path, opener=fake_open,
    )
    after_first = calls["n"]
    assert after_first > 0  # first lookup hit the vendor
    r2 = calibration.fetch_vendor_calibration(
        model_key="dayton_imm6", serial="cmm31555", root=tmp_path, opener=fake_open,
    )
    assert calls["n"] == after_first  # repeat lookup did NOT hit the vendor
    assert r2.calibration_id == r1.calibration_id


def test_find_stored_calibration_respects_orientation(tmp_path: Path):
    # miniDSP ships 0deg + 90deg files; a 0deg store must not satisfy 90deg.
    calibration.store_calibration(
        text="20 -1\n1000 0\n20000 1\n", provider="minidsp",
        model="minidsp_umik1", label="UMIK-1", source="vendor",
        serial="7001234", orientation="0deg", root=tmp_path,
    )
    assert calibration.find_stored_calibration(
        provider="minidsp", model_key="minidsp_umik1", serial="7001234",
        orientation="0deg", root=tmp_path,
    ) is not None
    assert calibration.find_stored_calibration(
        provider="minidsp", model_key="minidsp_umik1", serial="7001234",
        orientation="90deg", root=tmp_path,
    ) is None


# --- household-mic persistence: additive content-hash lookup for uploads ----
# A manual upload is stored with `serial=None` (store_calibration(provider=
# "manual_upload", ...)), so `find_stored_calibration` above — keyed by
# serial_hash + model + orientation — can never reach it again. This is the
# additive counterpart jasper.correction.household_mic relies on to make an
# uploaded calibration findable purely from its content hash.
def test_find_stored_calibration_by_content_hash_resolves_upload_with_no_serial(
    tmp_path: Path,
):
    record = calibration.store_calibration(
        text=SAMPLE_CAL,
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path,
    )
    assert record.serial_hash is None  # the upload path never has a serial

    found = calibration.find_stored_calibration_by_content_hash(
        file_sha256=record.file_sha256, root=tmp_path,
    )
    assert found is not None
    assert found.calibration_id == record.calibration_id


def test_find_stored_calibration_by_content_hash_also_resolves_vendor_records(
    tmp_path: Path,
):
    # Not upload-only: any stored calibration (vendor OR upload) can be
    # found again purely from its content hash.
    record = calibration.store_calibration(
        text="20 -1\n1000 0\n20000 1\n", provider="minidsp",
        model="minidsp_umik1", label="UMIK-1", source="vendor",
        serial="7001234", orientation="0deg", root=tmp_path,
    )
    found = calibration.find_stored_calibration_by_content_hash(
        file_sha256=record.file_sha256, root=tmp_path,
    )
    assert found is not None
    assert found.calibration_id == record.calibration_id


def test_find_stored_calibration_by_content_hash_misses_are_none(tmp_path: Path):
    assert calibration.find_stored_calibration_by_content_hash(
        file_sha256="0" * 64, root=tmp_path,
    ) is None
    assert calibration.find_stored_calibration_by_content_hash(
        file_sha256="", root=tmp_path,
    ) is None
