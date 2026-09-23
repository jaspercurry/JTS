# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import time

import pytest

import jasper.control.airplay_health as airplay_health
import jasper.control.camilla_rate_storm as camilla_rate_storm
from jasper.service_units import CAMILLA_SERVICE
from tests._log_events import event_fields, event_records
from tests.test_airplay_health import _material_short_read_lines, _storm_sampler


def test_seconds_since_camilla_restart_reads_the_shared_unit_state_reader(
    monkeypatch,
) -> None:
    # Fixed rather than the host's real CLOCK_MONOTONIC: a container whose
    # own uptime is under 600s would otherwise see a negative timestamp.
    now_us = 10_000.0 * 1e6
    started_us = int(now_us - 600.0 * 1e6)
    monkeypatch.setattr(
        time, "clock_gettime", lambda _clock: now_us / 1e6,
    )
    monkeypatch.setattr(
        camilla_rate_storm,
        "read_unit_states",
        lambda units, **_kw: {
            CAMILLA_SERVICE: {
                "active_enter_timestamp_monotonic": started_us,
            },
        },
    )

    age = camilla_rate_storm._seconds_since_camilla_restart()

    assert age == pytest.approx(600.0, abs=1.0)


def _camilla_reader(pending):
    def reader(_units, _since, _until):
        return [
            (airplay_health.CAMILLA_UNIT, line) for line in pending["lines"]
        ]
    return reader


def test_storm_onset_event_captures_controller_and_context(tmp_path, caplog) -> None:
    now = [1000.0]
    pending = {"lines": []}
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(tmp_path / "rate-storms"),
        storm_exit_debounce_sec=1.0,
    )
    # First scan only establishes the journal cursor (its window spans from 0,
    # so the rate is ~0 regardless of content) — never a storm.
    sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is False

    # Next scan: 100 material short reads across a 30 s window = 200/min -> storm.
    now[0] += 30.0
    pending["lines"] = _material_short_read_lines(100)
    with caplog.at_level(logging.WARNING, logger="jasper.control.camilla_rate_storm"):
        sampler._tick()

    snap = sampler.snapshot()
    assert snap["storm"]["active"] is True
    assert snap["storm"]["count"] == 1
    onset = snap["storm"]["onset"]
    assert onset["material_per_min"] == 200.0
    assert onset["rate_adjust"] == 1.0002
    assert onset["capture_rate"] == 48125
    assert onset["buffer_level"] == 2040
    assert onset["active_source"] == "airplay"
    assert onset["soc_temp_c"] == 52.0
    assert onset["cpu_governor"] == "ondemand"
    assert onset["sec_since_camilla_restart"] == 600.0
    assert onset["sec_since_deploy"] == 7200.0
    assert event_records(caplog, "camilla_rate.storm_onset")

    # Tier 2: a bounded trajectory artifact exists with header + the onset row.
    files = list((tmp_path / "rate-storms").glob("storm-*.csv"))
    assert len(files) == 1
    rows = files[0].read_text().splitlines()
    assert rows[0] == (
        "t_sec,rate_adjust,capture_rate,buffer_level,"
        "soc_temp_c,cpu_freq_khz,material_per_min"
    )
    assert len(rows) >= 2
    assert rows[1].split(",")[1] == "1.0002"  # rate_adjust column, onset row


def test_storm_offset_event_fires_after_debounced_clear(tmp_path, caplog) -> None:
    now = [1000.0]
    pending = {"lines": []}
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(tmp_path / "rate-storms"),
        storm_exit_debounce_sec=1.0,
    )
    sampler._tick()                                   # cursor
    now[0] += 30.0
    pending["lines"] = _material_short_read_lines(100)
    sampler._tick()                                   # onset
    assert sampler.snapshot()["storm"]["active"] is True

    # First quiet scan arms the debounce but does not yet clear.
    now[0] += 30.0
    pending["lines"] = []
    sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is True

    # Second quiet scan is past the (short) debounce -> offset.
    now[0] += 30.0
    with caplog.at_level(logging.WARNING, logger="jasper.control.camilla_rate_storm"):
        sampler._tick()
    snap = sampler.snapshot()
    assert snap["storm"]["active"] is False
    assert snap["storm"]["count"] == 1
    assert "duration_sec" in event_fields(caplog, "camilla_rate.storm_offset")


def test_storm_ignores_short_scan_window(tmp_path) -> None:
    # A high count over a sub-15 s scan window must not false-trigger a storm.
    now = [1000.0]
    pending = {"lines": []}
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(tmp_path / "rate-storms"),
        journal_interval_sec=10.0,
    )
    sampler._tick()
    now[0] += 10.0
    pending["lines"] = _material_short_read_lines(100)  # 600/min, but window=10 s
    sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is False


def test_no_storm_below_enter_threshold(tmp_path) -> None:
    now = [1000.0]
    pending = {"lines": []}
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(tmp_path / "rate-storms"),
    )
    sampler._tick()
    now[0] += 30.0
    pending["lines"] = _material_short_read_lines(30)  # 60/min < 120 enter floor
    sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is False


def test_storm_capture_is_failsoft_when_artifact_dir_unwritable(
    tmp_path, caplog,
) -> None:
    # A trajectory directory that can't be created must not break the Tier-1
    # onset/offset events: forensics is observability-only.
    now = [1000.0]
    pending = {"lines": []}
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(blocker / "sub"),  # makedirs under a file -> OSError
        storm_exit_debounce_sec=1.0,
    )
    sampler._tick()
    now[0] += 30.0
    pending["lines"] = _material_short_read_lines(100)
    with caplog.at_level(logging.WARNING, logger="jasper.control.camilla_rate_storm"):
        sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is True
    assert event_records(caplog, "camilla_rate.storm_onset")

    now[0] += 30.0
    pending["lines"] = []
    sampler._tick()
    now[0] += 30.0
    with caplog.at_level(logging.WARNING, logger="jasper.control.camilla_rate_storm"):
        sampler._tick()
    assert sampler.snapshot()["storm"]["active"] is False
    # No artifact dir, rendered as null.
    assert event_fields(caplog, "camilla_rate.storm_offset")["artifact"] == "null"


def test_sampler_snapshot_keeps_its_keys_and_storm_block(tmp_path) -> None:
    # audio_health copies the sampler snapshot's storm block into /state
    # verbatim, so its keys and values are a contract.
    now = [1000.0]
    pending = {"lines": []}
    sampler = _storm_sampler(
        now,
        reader=_camilla_reader(pending),
        tmp_dir=str(tmp_path / "rate-storms"),
        storm_exit_debounce_sec=1.0,
    )
    sampler._tick()
    now[0] += 30.0
    pending["lines"] = _material_short_read_lines(100)
    sampler._tick()  # onset: trajectory row 0
    now[0] += 5.0
    sampler._tick()  # storm-cadence Camilla sample: row 1

    snap = sampler.snapshot()
    assert set(snap) == {
        "last_sample_at", "maintenance_suppressed",
        "maintenance_suppressed_until", "warmup_active",
        "connect_grace_until", "suppressed_reason", "status", "reason",
        "current", "summary_5m", "summary_30m", "storm", "events",
    }
    assert snap["storm"] == {
        "active": True,
        "count": 1,
        "started_at": 1030.0,
        "material_per_min": 200.0,
        "peak_per_min": 200.0,
        "samples": 2,
        "onset": {
            "material_per_min": 200.0,
            "rate_adjust": 1.0002,
            "capture_rate": 48125,
            "buffer_level": 2040,
            "active_source": "airplay",
            "soc_temp_c": 52.0,
            "cpu_governor": "ondemand",
            "cpu_freq_khz": 1_500_000,
            "sec_since_camilla_restart": 600.0,
            "sec_since_deploy": 7200.0,
        },
    }

    pending["lines"] = []
    for _ in range(2):  # one quiet scan arms the debounce, the next clears it
        now[0] += 30.0
        sampler._tick()
    assert sampler.snapshot()["storm"] == {
        "active": False,
        "count": 1,
        "started_at": None,
        "material_per_min": 0.0,
        "peak_per_min": None,
        "samples": 0,
        "onset": None,
    }
