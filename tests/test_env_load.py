# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for environment-value parsing shared by runtime knobs."""

from __future__ import annotations

import pytest

from jasper.env_load import bounded_env_float, bounded_env_int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 1.25 ", 1.25),
        ("-2.0", -2.0),
        ("3", 3.0),
        ("", 9.0),
        ("not-a-number", 9.0),
        ("nan", 9.0),
        ("inf", 9.0),
        ("-2.01", 9.0),
        ("3.01", 9.0),
    ],
)
def test_bounded_env_float_preserves_parse_range_and_fallback_contract(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: float,
) -> None:
    monkeypatch.setenv("JASPER_TEST_BOUNDED_FLOAT", raw)
    assert (
        bounded_env_float(
            "JASPER_TEST_BOUNDED_FLOAT",
            9.0,
            lo=-2.0,
            hi=3.0,
        )
        == expected
    )


def test_bounded_env_float_missing_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JASPER_TEST_BOUNDED_FLOAT", raising=False)
    assert bounded_env_float(
        "JASPER_TEST_BOUNDED_FLOAT",
        9.0,
        lo=-2.0,
        hi=3.0,
    ) == 9.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 2 ", 2),
        ("-2", -2),
        ("3", 3),
        ("", 9),
        ("2.0", 9),
        ("not-an-integer", 9),
        ("-3", 9),
        ("4", 9),
    ],
)
def test_bounded_env_int_preserves_parse_range_and_fallback_contract(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: int,
) -> None:
    monkeypatch.setenv("JASPER_TEST_BOUNDED_INT", raw)
    assert (
        bounded_env_int(
            "JASPER_TEST_BOUNDED_INT",
            9,
            lo=-2,
            hi=3,
        )
        == expected
    )


def test_bounded_env_int_missing_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JASPER_TEST_BOUNDED_INT", raising=False)
    assert bounded_env_int(
        "JASPER_TEST_BOUNDED_INT",
        9,
        lo=-2,
        hi=3,
    ) == 9
