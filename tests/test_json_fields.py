# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared field helpers every JSON-artifact writer now consumes."""
from __future__ import annotations

import hashlib
import re

import pytest

from jasper.json_fields import (
    finite_float,
    json_fingerprint,
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


def test_utc_now_iso_is_a_second_resolution_zulu_stamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now_iso())


def test_sha256_file_digests_the_whole_file_across_chunk_boundaries(tmp_path):
    payload = bytes(range(256)) * 1024
    target = tmp_path / "blob.bin"
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_json_fingerprint_ignores_key_order_but_not_values():
    assert json_fingerprint({"a": 1, "b": [2, {"c": 3}]}) == json_fingerprint(
        {"b": [2, {"c": 3}], "a": 1}
    )
    assert json_fingerprint({"a": 1}) != json_fingerprint({"a": 2})
