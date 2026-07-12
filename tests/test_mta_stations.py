# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jasper.transit import _mta_stations


@pytest.fixture(autouse=True)
def _clear_station_cache():
    """Each case must exercise resource I/O, not a prior cached result."""

    _mta_stations.load_stations.cache_clear()
    yield
    _mta_stations.load_stations.cache_clear()


def _point_loader_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(_mta_stations.resources, "files", lambda _package: root)


def _assert_fails_soft_with_error(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=_mta_stations.__name__):
        assert _mta_stations.load_stations() == ()

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("mta_stations.csv unreadable")
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_load_stations_returns_empty_when_resource_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _point_loader_at(monkeypatch, tmp_path)

    _assert_fails_soft_with_error(caplog)


def test_load_stations_returns_empty_when_resource_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A directory at the resource path exercises a real Path.open failure
    # without depending on the test runner's user being unable to bypass mode 0.
    (tmp_path / "mta_stations.csv").mkdir()
    _point_loader_at(monkeypatch, tmp_path)

    _assert_fails_soft_with_error(caplog)


def test_load_stations_returns_empty_for_invalid_utf8(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "mta_stations.csv").write_bytes(
        b"stop_id,stop_name,borough,lines,lat,lon,north_label,south_label\n"
        b"B12,\xff,Bk,D,40.646,-73.994,Manhattan,Coney Island\n"
    )
    _point_loader_at(monkeypatch, tmp_path)

    _assert_fails_soft_with_error(caplog)
