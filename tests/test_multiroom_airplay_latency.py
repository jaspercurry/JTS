# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bonded-leader AirPlay latency-fit observability (the "Stage D" gap).

Pins the pure fit math, the fail-soft journal reader, the /state snapshot
gate, the grouping doctor check, and the AirPlay-health classification of
shairport's authoritative "too short" warning. Hardware-free: the journal
edge and config loader are injected.
"""
from __future__ import annotations

import subprocess

import pytest

from jasper.multiroom import airplay_latency as al
from jasper.multiroom.config import GroupingConfig


def _cfg(**over) -> GroupingConfig:
    base = dict(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="lr",
        leader_addr="",
        buffer_ms=400,
        codec="flac",
        error=None,
    )
    base.update(over)
    return GroupingConfig(**base)


@pytest.fixture(autouse=True)
def _reset_frames_cache():
    """The notified-frames TTL cache is process-wide module state; keep tests
    isolated from each other's reads."""
    al._notified_frames_cache = None
    yield
    al._notified_frames_cache = None


# ---------- assess_fit: pure math ----------


def test_default_budget_is_the_free_regime():
    """Absence of a Notified-latency line => default 77175 frames => ~2.0 s
    budget, which clears the 0.55 s need + 0.5 s shairport backend buffer
    (~1.05 s threshold) with ~0.95 s to spare."""
    fit = al.assess_fit(buffer_ms=400, notified_frames=None)
    assert fit.budget_source == "default"
    assert fit.negotiated_frames == al.AP2_DEFAULT_NOTIFIED_FRAMES
    assert fit.budget_sec == pytest.approx(2.0002, abs=1e-3)
    assert fit.need_sec == pytest.approx(0.55, abs=1e-9)
    assert fit.tight is False
    assert fit.residual_lag_sec == 0.0


def test_high_buffer_is_tight_even_at_the_default_budget():
    """Corrected math: shairport's tight condition is budget < need + 0.5 s
    backend buffer. So a buffer_ms near its max is tight EVEN at the default
    ~2.0 s budget (need 1.65 + 0.5 = 2.15 > 2.0002), and shairport drops the
    whole offset => residual lag is the FULL need, not a shortfall. A mid-high
    buffer_ms (1300) still fits, pinning the ~1350 ms boundary."""
    tight = al.assess_fit(buffer_ms=1500, notified_frames=None)
    assert tight.need_sec == pytest.approx(1.65, abs=1e-9)
    assert tight.tight is True
    assert tight.residual_lag_sec == pytest.approx(1.65, abs=1e-9)

    fits = al.assess_fit(buffer_ms=1300, notified_frames=None)
    assert fits.tight is False


def test_small_negotiated_budget_is_tight_residual_is_full_need():
    """When shairport drops the offset, the entire pipeline+buffer delay is
    uncompensated, so residual_lag_sec == need_sec (not need - budget)."""
    fit = al.assess_fit(buffer_ms=400, notified_frames=5000)
    assert fit.budget_source == "journal"
    # (5000 + 11035) / 44100 ≈ 0.3636 s budget vs 0.55 s need.
    assert fit.budget_sec == pytest.approx(0.36361, abs=1e-4)
    assert fit.tight is True
    assert fit.residual_lag_sec == pytest.approx(0.55, abs=1e-9)


def test_backend_buffer_band_is_tight_against_production_05s_buffer():
    """The band the old (buffer-omitting) math wrongly called 'fits': a budget
    between need (0.55 s) and need + 0.5 s (1.05 s). shairport DOES warn and
    drop the offset there. Pins the fix against the production 0.5 s backend
    buffer, not just the synthetic 0.1 s classifier string."""
    # frames=24245 -> budget = (24245+11035)/44100 = 0.8 s, inside (0.55, 1.05).
    fit = al.assess_fit(buffer_ms=400, notified_frames=24245)
    assert fit.budget_sec == pytest.approx(0.8, abs=1e-4)
    assert fit.tight is True  # old math: 0.55 > 0.8 -> False (the bug)
    assert fit.residual_lag_sec == pytest.approx(0.55, abs=1e-9)


def test_budget_just_above_need_plus_backend_buffer_is_not_tight():
    # frames=40000 -> budget = 51035/44100 ≈ 1.157 s > 1.05 s threshold.
    assert al.assess_fit(buffer_ms=400, notified_frames=40000).tight is False
    # frames=30000 -> budget = 41035/44100 ≈ 0.930 s < 1.05 s threshold.
    assert al.assess_fit(buffer_ms=400, notified_frames=30000).tight is True


@pytest.mark.parametrize("bad", [0, -1, -77175])
def test_nonpositive_frames_fall_back_to_default(bad):
    """A garbage reading must never be taken as a SMALLER budget than the
    default — that would invent a false tight regime."""
    fit = al.assess_fit(buffer_ms=400, notified_frames=bad)
    assert fit.budget_source == "default"
    assert fit.negotiated_frames == al.AP2_DEFAULT_NOTIFIED_FRAMES
    assert fit.tight is False


def test_to_dict_is_json_shaped():
    d = al.assess_fit(buffer_ms=400, notified_frames=5000).to_dict()
    assert set(d) == {
        "buffer_ms", "negotiated_frames", "budget_source",
        "budget_sec", "need_sec", "tight", "residual_lag_sec",
    }
    assert d["tight"] is True


# ---------- read_notified_frames: thin, fail-soft IO ----------


def _proc(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["journalctl"], returncode=0, stdout=stdout)


def test_read_notified_frames_takes_the_most_recent_line():
    out = "Notified latency is 60000 frames.\nNotified latency is 50000 frames.\n"
    assert al.read_notified_frames(runner=lambda u, lb: _proc(out)) == 50000


def test_read_notified_frames_absent_line_is_none_not_error():
    assert al.read_notified_frames(runner=lambda u, lb: _proc("")) is None
    assert al.read_notified_frames(runner=lambda u, lb: _proc("unrelated\n")) is None


@pytest.mark.parametrize(
    "exc",
    [OSError("no journalctl"), subprocess.TimeoutExpired(cmd="x", timeout=5)],
)
def test_read_notified_frames_is_fail_soft(exc):
    def boom(unit, lookback):
        raise exc

    assert al.read_notified_frames(runner=boom) is None


# ---------- bonded_airplay_latency_snapshot: the /state gate ----------


def test_snapshot_solo_is_not_applicable_and_never_reads_journal():
    def reader_must_not_run():
        raise AssertionError("journal must not be read on the solo path")

    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: _cfg(enabled=False, role=""),
        frames_reader=reader_must_not_run,
    )
    assert snap == {"applicable": False}


def test_snapshot_follower_is_not_applicable():
    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: _cfg(role="follower", leader_addr="10.0.0.7"),
        frames_reader=lambda: 5000,
    )
    assert snap == {"applicable": False}


def test_snapshot_invalid_leader_is_not_applicable():
    # enabled but error set => not an active member, so no journal read.
    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: _cfg(error="bad bond"),
        frames_reader=lambda: 5000,
    )
    assert snap == {"applicable": False}


def test_snapshot_active_leader_default_budget_is_comfortable():
    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: _cfg(buffer_ms=400),
        frames_reader=lambda: None,
    )
    assert snap is not None
    assert snap["applicable"] is True
    assert snap["budget_source"] == "default"
    assert snap["tight"] is False


def test_snapshot_active_leader_tight_budget():
    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: _cfg(buffer_ms=400),
        frames_reader=lambda: 5000,
    )
    assert snap is not None
    assert snap["applicable"] is True
    assert snap["tight"] is True
    assert snap["residual_lag_sec"] > 0


def test_snapshot_is_fail_soft_on_loader_error():
    def boom():
        raise RuntimeError("grouping read blew up")

    assert al.bonded_airplay_latency_snapshot(config_loader=boom) is None


# ---------- doctor check ----------


def _patch_doctor(monkeypatch, cfg, frames):
    import jasper.multiroom.airplay_latency as alm
    import jasper.multiroom.config as cfgmod

    monkeypatch.setattr(cfgmod, "load_config", lambda: cfg)
    monkeypatch.setattr(alm, "read_notified_frames", lambda *a, **k: frames)


def test_doctor_skips_when_not_a_bonded_leader(monkeypatch):
    from jasper.cli.doctor.grouping import check_grouping_airplay_latency

    _patch_doctor(monkeypatch, _cfg(enabled=False, role=""), frames=None)
    res = check_grouping_airplay_latency()
    assert res.status == "ok"
    assert "n/a" in res.detail


def test_doctor_ok_when_budget_fits(monkeypatch):
    from jasper.cli.doctor.grouping import check_grouping_airplay_latency

    _patch_doctor(monkeypatch, _cfg(buffer_ms=400), frames=None)
    res = check_grouping_airplay_latency()
    assert res.status == "ok"
    assert "fits" in res.detail


def test_doctor_warns_when_budget_too_short(monkeypatch):
    from jasper.cli.doctor.grouping import check_grouping_airplay_latency

    _patch_doctor(monkeypatch, _cfg(buffer_ms=400), frames=5000)
    res = check_grouping_airplay_latency()
    assert res.status == "warn"
    assert "residual" in res.detail.lower()
    assert "buffer_ms" in res.detail
    # residual is the FULL need (shairport drops the offset): 0.55 s -> 550 ms.
    # Pins the s->ms scaling in the doctor f-string.
    assert "550 ms" in res.detail
    # Remediation must NOT point at a non-existent /rooms buffer_ms control.
    assert "/rooms" not in res.detail
    assert "JASPER_GROUPING_BUFFER_MS" in res.detail


# ---------- AirPlay-health classification of the ground-truth warning ----------


def test_classify_offset_too_short_warning():
    from jasper.control.airplay_health import SHAIRPORT_UNIT, classify_journal_line

    line = (
        "The stream latency (0.300000 seconds) it too short to accommodate an "
        "offset of 0.550000 seconds and a backend buffer of 0.100000 seconds."
    )
    ev = classify_journal_line(SHAIRPORT_UNIT, line)
    assert ev is not None
    assert ev["type"] == "shairport_offset_too_short"
    assert ev["severity"] == "issue"


def test_classify_ignores_unrelated_shairport_line():
    from jasper.control.airplay_health import SHAIRPORT_UNIT, classify_journal_line

    assert classify_journal_line(SHAIRPORT_UNIT, "Notified latency is 50000 frames.") is None


# ---------- cached_notified_frames: bound journalctl on the /state hot path ----------


def test_cached_notified_frames_serves_within_ttl_then_refreshes():
    """/state is polled ~5 s; the budget changes per session (minutes). The
    cache must serve from one read within the TTL and re-read past it."""
    clock = [100.0]
    calls = []

    def reader():
        calls.append(1)
        return len(calls) * 1000

    now = lambda: clock[0]
    assert al.cached_notified_frames(now=now, reader=reader) == 1000
    assert al.cached_notified_frames(now=now, reader=reader) == 1000  # cache hit
    assert len(calls) == 1

    clock[0] += al._NOTIFIED_FRAMES_TTL_SEC + 1.0
    assert al.cached_notified_frames(now=now, reader=reader) == 2000  # refreshed
    assert len(calls) == 2


def test_cached_notified_frames_caches_none_so_a_wedged_journalctl_is_not_hammered():
    calls = []

    def reader():
        calls.append(1)
        return None

    now = lambda: 50.0
    assert al.cached_notified_frames(now=now, reader=reader) is None
    assert al.cached_notified_frames(now=now, reader=reader) is None
    assert len(calls) == 1  # None is cached, not re-read every poll


# ---------- writer <-> observer lockstep ----------


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg(enabled=False, role=""),            # solo
        _cfg(role="follower", leader_addr="x"),  # follower
        _cfg(error="broken"),                    # enabled-but-invalid leader
        _cfg(),                                  # active leader
    ],
)
def test_offset_write_gate_matches_observability_gate(cfg):
    """The reconciler ARMS the bonded offset (airplay_grouping_env != {}) under
    exactly the condition the observability reports as `applicable`. If these
    ever diverge, /state would claim a fit for an offset that is not armed (or
    hide one that is). Bind both to is_active_leader and pin it here."""
    from jasper.multiroom.config import is_active_leader
    from jasper.multiroom.reconcile import airplay_grouping_env

    armed = bool(airplay_grouping_env(cfg))
    snap = al.bonded_airplay_latency_snapshot(
        config_loader=lambda: cfg, frames_reader=lambda: None,
    )
    assert armed == is_active_leader(cfg) == bool(snap.get("applicable"))
