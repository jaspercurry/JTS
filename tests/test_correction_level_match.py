# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side level-match adapter: relay feed, geometry lock, drift check.

The kernel ramp math is tested in ``test_audio_measurement_ramp.py``; here we
test the correction glue with a fake relay (a status dict the feed reads) and a
fake clock — no network, no CamillaDSP.
"""
from __future__ import annotations

import pytest

from jasper.audio_measurement.ramp import (
    LEVEL_EVENT_SCHEMA_VERSION,
    MeasurementRamp,
    RampState,
)
from jasper.correction.level_match import (
    DriftVerdict,
    LevelLockStore,
    LevelMatchSession,
    MeasurementLevelLock,
    MicGeometry,
    RelayLevelFeed,
    check_level_drift,
    parse_level_batch,
    phone_reported_abort,
)


# --- level-batch parsing ------------------------------------------------------


def _batch(samples, **extra):
    return {
        "level_batch": {
            "schema": LEVEL_EVENT_SCHEMA_VERSION,
            "samples": samples,
            **extra,
        }
    }


def test_parse_level_batch_reads_samples():
    event = _batch(
        [
            {"seq": 1, "t_client_ms": 100, "rms_dbfs": -30.0, "peak_dbfs": -26.0},
            {"seq": 2, "t_client_ms": 200, "rms_dbfs": -28.0, "peak_dbfs": -24.0},
        ]
    )
    got = parse_level_batch(event)
    assert [s.seq for s in got] == [1, 2]
    assert got[0].rms_dbfs == -30.0


def test_parse_level_batch_schema_mismatch_yields_empty():
    event = _batch([{"seq": 1, "rms_dbfs": -30.0}])
    event["level_batch"]["schema"] = 999
    assert parse_level_batch(event) == []


def test_parse_level_batch_tolerates_garbage():
    assert parse_level_batch({}) == []
    assert parse_level_batch({"level_batch": "nope"}) == []
    assert parse_level_batch({"level_batch": {"samples": "nope"}}) == []
    # A malformed sample is skipped, good ones survive.
    event = _batch([{"seq": 1}, {"seq": 2, "rms_dbfs": -20.0}])
    got = parse_level_batch(event)
    assert [s.seq for s in got] == [2]


def test_parse_level_batch_applies_batch_agc_flag():
    event = _batch(
        [{"seq": 1, "rms_dbfs": -30.0}], agc_frozen=False
    )
    got = parse_level_batch(event)
    assert got[0].agc_frozen is False  # batch-level superset applies


def test_phone_reported_abort_from_superset_and_toplevel():
    assert phone_reported_abort(_batch([], aborted=True)) == "phone_aborted"
    ev = _batch([], aborted=True, abort_reason="backgrounded")
    assert phone_reported_abort(ev) == "backgrounded"
    assert phone_reported_abort({"aborted": True, "reason": "x"}) == "x"
    assert phone_reported_abort({}) is None


# --- relay feed dedupes by seq ------------------------------------------------


@pytest.mark.asyncio
async def test_relay_feed_dedupes_and_detects_abort():
    status = {"event": _batch([{"seq": 1, "rms_dbfs": -30.0}])}
    posted: list[dict] = []
    feed = RelayLevelFeed(
        read_status=lambda: status, post_host_event=lambda e: posted.append(e)
    )
    first = await feed.next_samples()
    assert [s.seq for s in first] == [1]
    # Same slot re-read (last-write-wins) → nothing new.
    again = await feed.next_samples()
    assert again == []
    # New batch arrives.
    status["event"] = _batch([{"seq": 2, "rms_dbfs": -25.0}])
    assert [s.seq for s in await feed.next_samples()] == [2]
    # Abort superset stops the feed.
    status["event"] = _batch([], aborted=True, abort_reason="backgrounded")
    assert await feed.next_samples() == []
    assert feed.aborted_reason == "backgrounded"


@pytest.mark.asyncio
async def test_relay_feed_survives_a_failing_status_read():
    def boom():
        raise RuntimeError("relay down")

    feed = RelayLevelFeed(read_status=boom)
    assert await feed.next_samples() == []  # transient read failure ≠ crash


# --- geometry lock store ------------------------------------------------------


def test_lock_store_is_per_geometry():
    store = LevelLockStore()
    near = MeasurementLevelLock(
        geometry=MicGeometry.NEAR_FIELD_DRIVER.value,
        main_volume_db=-40.0,
        gain_map_db=30.0,
        settled_mic_dbfs=-10.0,
        noise_floor_dbfs=-80.0,
    )
    listen = MeasurementLevelLock(
        geometry=MicGeometry.LISTENING_POSITION.value,
        main_volume_db=-18.0,
        gain_map_db=2.0,
        settled_mic_dbfs=-16.0,
        noise_floor_dbfs=-70.0,
    )
    store.put(near)
    store.put(listen)
    # Two coexisting locks — neither clobbers the other.
    assert store.get(MicGeometry.NEAR_FIELD_DRIVER.value).main_volume_db == -40.0
    assert store.get(MicGeometry.LISTENING_POSITION.value).main_volume_db == -18.0
    assert set(store.snapshot()) == {
        MicGeometry.NEAR_FIELD_DRIVER.value,
        MicGeometry.LISTENING_POSITION.value,
    }


# --- drift check (raw band levels, uniform-shift rule) ------------------------


def test_drift_uniform_shift_is_amp_moved():
    ref = [70.0, 72.0, 68.0, 71.0]
    cur = [66.0, 68.0, 64.0, 67.0]  # uniform -4 dB
    r = check_level_drift(ref, cur, same_geometry=True)
    assert r.verdict == DriftVerdict.AMP_MOVED
    assert r.mean_shift_db == pytest.approx(-4.0)
    assert r.max_band_deviation_db == pytest.approx(0.0, abs=1e-9)


def test_drift_geometry_change_is_not_flagged():
    ref = [70.0, 72.0, 68.0, 71.0]
    cur = [50.0, 52.0, 48.0, 51.0]  # uniform -20 dB but geometry changed
    r = check_level_drift(ref, cur, same_geometry=False)
    assert r.verdict == DriftVerdict.GEOMETRY_CHANGED  # expected, never "amp moved"


def test_drift_non_uniform_is_acoustic():
    ref = [70.0, 70.0, 70.0, 70.0]
    cur = [70.0, 70.0, 62.0, 70.0]  # one band dipped: a room mode, not a level shift
    r = check_level_drift(ref, cur, same_geometry=True)
    assert r.verdict == DriftVerdict.ACOUSTIC


def test_drift_small_change_is_ok():
    ref = [70.0, 72.0, 68.0]
    cur = [70.5, 71.5, 68.2]
    assert check_level_drift(ref, cur, same_geometry=True).verdict == DriftVerdict.OK


def test_drift_agc_unfrozen_disables_the_rule():
    ref = [70.0, 72.0, 68.0]
    cur = [60.0, 62.0, 58.0]  # would be AMP_MOVED, but reference is AGC-compressed
    r = check_level_drift(ref, cur, same_geometry=True, agc_frozen=False)
    assert r.verdict == DriftVerdict.UNKNOWN


def test_drift_mismatched_bands_is_unknown():
    assert (
        check_level_drift([70.0], [70.0, 71.0], same_geometry=True).verdict
        == DriftVerdict.UNKNOWN
    )
    assert check_level_drift([], [], same_geometry=True).verdict == DriftVerdict.UNKNOWN


def test_drift_env_knobs_override_thresholds(monkeypatch):
    # Loosen the uniform threshold so a 4 dB shift no longer counts as an amp move.
    monkeypatch.setenv("JASPER_RAMP_DRIFT_UNIFORM_DB", "6.0")
    ref = [70.0, 70.0, 70.0]
    cur = [66.0, 66.0, 66.0]  # uniform -4 dB, now below the 6 dB threshold
    assert check_level_drift(ref, cur, same_geometry=True).verdict == DriftVerdict.OK


# --- LevelMatchSession end-to-end with a fake relay ---------------------------


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(s, 0.01)


class FakeChain:
    """Fake speaker+relay: the mic level tracks commanded volume + gain, streamed
    back through a mutable relay status dict as level batches."""

    def __init__(self, *, gain_db, start_vol, nf=-80.0):
        self.gain_db = gain_db
        self.nf = nf
        self._vol = start_vol
        self.commanded = []
        self._seq = 0
        self.status = {"event": {}}

    async def get_vol(self):
        return self._vol

    async def set_vol(self, db):
        self._vol = db
        self.commanded.append(db)

    async def tone(self):
        pass

    def cancel_tone(self):
        pass

    def read_status(self):
        # Report the mic level at the CURRENT commanded volume as a fresh batch.
        self._seq += 1
        mic = self._vol + self.gain_db
        self.status["event"] = {
            "level_batch": {
                "schema": LEVEL_EVENT_SCHEMA_VERSION,
                "samples": [
                    {
                        "seq": self._seq,
                        "t_client_ms": self._seq * 100,
                        "rms_dbfs": mic,
                        "peak_dbfs": mic + 3.0,
                        "clip": False,
                        "agc_frozen": True,
                    }
                ],
            }
        }
        return self.status


@pytest.mark.asyncio
async def test_level_match_session_locks_and_stores_geometry_lock():
    store = LevelLockStore()
    cfg = MeasurementRamp(settle_hold_s=0.5, max_loop_latency_s=0.5)
    sess = LevelMatchSession(session_id="s", store=store, config=cfg)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()

    outcome = await sess.run_for_geometry(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=None,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert outcome.ramp.state == RampState.LOCKED
    assert outcome.locked
    lock = store.get(MicGeometry.LISTENING_POSITION.value)
    assert lock is not None
    assert lock.main_volume_db == pytest.approx(outcome.ramp.locked_main_volume_db)
    # Cap never exceeded even through the adapter.
    cap = cfg.dynamic_cap(-30.0)
    for v in chain.commanded:
        assert v <= cap + 1e-9


@pytest.mark.asyncio
async def test_level_match_session_honors_phone_abort():
    store = LevelLockStore()
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    sess = LevelMatchSession(session_id="s", store=store, config=cfg)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()

    # After a few reads, the phone posts an abort superset.
    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        if reads["n"] >= 3:
            return {"event": {"aborted": True, "abort_reason": "backgrounded"}}
        return base()

    outcome = await sess.run_for_geometry(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=read_status,
        post_host_event=None,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert outcome.ramp.state == RampState.CANCELLED
    assert outcome.aborted_reason == "backgrounded"
    # No lock stored on an aborted geometry.
    assert store.get(MicGeometry.LISTENING_POSITION.value) is None
    # Restored to the original level.
    assert chain.commanded[-1] == pytest.approx(-30.0)


@pytest.mark.asyncio
async def test_level_match_manual_lock():
    store = LevelLockStore()
    cfg = MeasurementRamp(settle_hold_s=5.0, max_loop_latency_s=2.0)
    sess = LevelMatchSession(session_id="s", store=store, config=cfg)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()

    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        return base()

    # Lock after a couple of ticks via a side task is awkward with the sync feed;
    # instead trigger the manual lock by wrapping read_status.
    async def run():
        return await sess.run_for_geometry(
            MicGeometry.NEAR_FIELD_DRIVER.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=chain.set_vol,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=lambda: (
                _maybe_lock(sess, reads, base)
            ),
            post_host_event=None,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=clock.sleep,
        )

    def _maybe_lock(session, counter, base_read):
        counter["n"] += 1
        if counter["n"] == 3:
            # Manual lock request (fire-and-forget into the running loop).
            session._controller._lock_requested = True
        return base_read()

    outcome = await run()
    assert outcome.ramp.state == RampState.LOCKED
    assert store.get(MicGeometry.NEAR_FIELD_DRIVER.value) is not None


# --- MeasurementSession seam (run_level_match) --------------------------------


def _make_session(tmp_path):
    from jasper.correction.session import MeasurementSession, SessionConfig

    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = SessionConfig(
        sweep_dir=tmp_path / "sweeps",
        capture_dir=tmp_path / "captures",
        config_dir=tmp_path / "configs",
        base_config_path=tmp_path / "v1.yml",
        duration_s=1.0,
    )
    cfg.base_config_path.write_text("# stub\n")
    return MeasurementSession(cfg)


@pytest.mark.asyncio
async def test_session_run_level_match_stores_geometry_lock(tmp_path):
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = FakeClock()

    outcome = await sess.run_level_match(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=None,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert outcome.ramp.state == RampState.LOCKED
    # The session's per-geometry store carries the lock, and the snapshot exposes
    # it for /status.
    snap = sess.level_match_snapshot()
    assert MicGeometry.LISTENING_POSITION.value in snap["locks"]
    assert snap["last"]["geometry"] == MicGeometry.LISTENING_POSITION.value
    assert snap["last"]["ramp"]["state"] == "locked"


def test_session_level_match_snapshot_empty_before_run(tmp_path):
    sess = _make_session(tmp_path)
    snap = sess.level_match_snapshot()
    assert snap["locks"] == {}
    assert snap["last"] is None
