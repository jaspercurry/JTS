# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared numeric reader every displayable-number guard now consumes."""
from __future__ import annotations

import pytest

from jasper.json_fields import finite_float


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
