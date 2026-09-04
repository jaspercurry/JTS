# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for environment-value parsing shared by runtime knobs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from jasper.env_load import bounded_env_float, bounded_env_int, read_env_file_or_warn


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


@pytest.mark.parametrize(
    ("present", "unreadable"),
    [
        (True, True),
        (False, False),
    ],
)
def test_read_env_file_or_warn_logs_via_injected_logger_only_when_unreadable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    present: bool,
    unreadable: bool,
) -> None:
    path = tmp_path / "some.env"
    if present:
        path.write_text("JASPER_X=1\n")
        path.chmod(0o000)
        if os.access(path, os.R_OK):
            pytest.skip("running as root: chmod 000 leaves the file readable")

    logger = logging.getLogger("jasper.test_read_env_file_or_warn")
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = read_env_file_or_warn(str(path), logger=logger)

        assert result == {}
        records = [r for r in caplog.records if r.name == logger.name]
        if unreadable:
            assert len(records) == 1
            assert records[0].levelno == logging.WARNING
        else:
            assert records == []
    finally:
        if present:
            path.chmod(0o644)
