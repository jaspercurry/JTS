# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared field helpers every JSON-artifact writer now consumes."""
from __future__ import annotations

import hashlib
import re

import pytest

from jasper.active_speaker.design_draft import ActiveSpeakerDesignDraftError
from jasper.active_speaker.driver_pad import DriverPadError
from jasper.active_speaker.driver_safety import DriverSafetyProfileError
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.rear_calibration import RearCalibrationError
from jasper.output_topology import OutputTopologyError
from jasper.json_fields import (
    CodedFieldError,
    JsonFields,
    _HASH_CHUNK_BYTES,
    as_float,
    canonical_json_bytes,
    finite_float,
    json_fingerprint,
    require_finite,
    require_sha256_hex,
    sha256_file,
    utc_now_iso,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, None),
        ("1.5", None),
        (10**400, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (None, None),
        (3, 3.0),
        (2.5, 2.5),
    ],
)
def test_finite_float_reads_only_a_real_number(value, expected):
    result = finite_float(value)
    assert result == expected
    assert result is None or type(result) is float


class _Refused(RuntimeError):
    pass


@pytest.mark.parametrize("value,positive,expected", [
    (2.5, False, 2.5), (0, False, 0.0), (-1, False, -1.0), (3, True, 3.0),
    (True, False, _Refused), ("1.5", False, _Refused), (None, False, _Refused),
    (float("nan"), False, _Refused), (10**400, False, _Refused),
    (0.0, True, _Refused), (-2.0, True, _Refused),
])
def test_require_finite_answers_a_real_number_or_the_callers_refusal(value, positive, expected):
    if expected is _Refused:
        with pytest.raises(_Refused):
            require_finite(value, field="x", error=_Refused, positive=positive)
    else:
        assert require_finite(value, field="x", error=_Refused, positive=positive) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(True, 1.0), (" 1.5 ", 1.5), ("-inf", float("-inf")), (3, 3.0),
     (None, None), ("junk", None), ([1], None),
     pytest.param(10**400, None, id="int-past-float-range")],
)
def test_as_float_coerces_whatever_float_accepts(value, expected):
    result = as_float(value)
    assert result == expected
    assert result is None or type(result) is float


def test_utc_now_iso_is_a_second_resolution_zulu_stamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now_iso())


def test_sha256_file_digests_the_whole_file_across_chunk_boundaries(tmp_path):
    size = _HASH_CHUNK_BYTES * 2 + 7  # exercise a trailing partial chunk
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    target = tmp_path / "blob.bin"
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_json_fingerprint_ignores_key_order_but_not_values():
    assert json_fingerprint({"a": 1, "b": [2, {"c": 3}]}) == json_fingerprint(
        {"b": [2, {"c": 3}], "a": 1}
    )
    assert json_fingerprint({"a": 1}) != json_fingerprint({"a": 2})


def test_canonical_json_bytes_is_sorted_compact_ascii_and_finite_only():
    assert canonical_json_bytes({"b": [1.5, None], "a": "é"}) == b'{"a":"\\u00e9","b":[1.5,null]}'
    with pytest.raises(ValueError):
        canonical_json_bytes({"a": float("nan")})


@pytest.mark.parametrize("value,accepted", [
    ("0123456789abcdef" * 4, True), ("A" * 64, False), (" " + "a" * 63, False),
    ("a" * 64 + "\n", False), ("a" * 63, False), ("g" * 64, False),
    ("\u0660" * 64, False), (b"a" * 64, False), (None, False),
])
def test_require_sha256_hex_answers_only_a_lowercase_digest(value, accepted):
    if accepted:
        assert require_sha256_hex(value, field="x", error=_Refused) == value
    else:
        with pytest.raises(_Refused):
            require_sha256_hex(value, field="x", error=_Refused)


@pytest.mark.parametrize("error_type", [
    CodedFieldError, ActiveSpeakerDesignDraftError, DriverSafetyProfileError,
    DriverPadError, ActiveSpeakerConfigError, RearCalibrationError, OutputTopologyError,
])
@pytest.mark.parametrize("method,bad,good,extra,code", [
    ("mapping", [], {}, (), "field_not_object"),
    ("sequence", {}, [], (), "field_not_list"),
    ("require_id", "", "mono:woofer", (), "field_required"),
    ("require_id", "bad id", "id", (), "field_invalid_id"),
    ("require_id", "x" * 81, "x" * 80, (), "field_invalid_id"),
    ("text", None, "text", (), "field_required"),
    ("text", "x" * 121, "x" * 120, (), "field_too_long"),
    ("optional_text", 1, "text", (), "field_not_string"),
    ("optional_text", "  ", None, (), "field_required"),
    ("optional_text", "x" * 241, "x" * 240, (), "field_too_long"),
    ("integer", "1.5", "2", (), "field_not_integer"),
    ("integer", float("inf"), 2, (), "field_not_integer"),
    ("integer", float("-inf"), 2, (), "field_not_integer"),
    ("strict_boolean", 1, True, (), "field_not_boolean"),
    ("enum", 1, "one", ({"one"},), "field_not_string"),
    ("enum", "two", "one", ({"one"},), "field_unsupported"),
    ("finite_number", "bad", "1.5", (), "field_not_numeric"),
    ("finite_number", 10**400, 1.0, (), "field_not_numeric"),
    ("finite_number", float("inf"), 1.0, (), "field_not_finite"),
    ("finite_number", float("nan"), 1.0, (), "field_not_finite"),
])
def test_field_refusals_override_domain_defaults(error_type, method, bad, good, extra, code):
    parse = getattr(JsonFields(error_type), method)
    parse(good, "field", *extra)
    with pytest.raises(error_type) as caught:
        parse(bad, "field", *extra)
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code


@pytest.mark.parametrize("error,code", [
    (CodedFieldError("x"), "d"),
    (CodedFieldError("x", code="c"), "c"),
    (ActiveSpeakerDesignDraftError("x"), "invalid_design_draft"),
])
def test_field_error_preserves_consumer_fallback(error, code):
    assert getattr(error, "code", "d") == code
