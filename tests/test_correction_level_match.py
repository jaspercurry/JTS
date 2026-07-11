# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side level-match adapter: relay feed, geometry lock, drift check.

The kernel ramp math is tested in ``test_audio_measurement_ramp.py``; here we
test the correction glue with a fake relay (a status dict the feed reads) and a
fake clock — no network, no CamillaDSP. The protocol-honesty items the review
demanded are pinned here: run-token scoping (a previous run's persisted slot
never cancels or feeds a retry), seq-regression as a new stream (phone page
reload), the armed gate (no tone until the phone armed), the latched
journal-spam warnings, and the terminal host event re-posted until the relay
echoes it back.
"""
from __future__ import annotations

import asyncio
import logging

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
    phone_reported_armed,
)

FAST = dict(settle_hold_s=0.5, max_loop_latency_s=0.5, settle_min_samples=2)


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
    seen: list = []
    assert parse_level_batch(event, on_schema_mismatch=seen.append) == []
    assert seen == [999]


def test_parse_level_batch_tolerates_garbage():
    assert parse_level_batch({}) == []
    assert parse_level_batch({"level_batch": "nope"}) == []
    assert parse_level_batch({"level_batch": {"samples": "nope"}}) == []
    # A malformed sample is skipped, good ones survive.
    event = _batch([{"seq": 1}, {"seq": 2, "rms_dbfs": -20.0}])
    got = parse_level_batch(event)
    assert [s.seq for s in got] == [2]


def test_parse_level_batch_drops_non_finite_samples():
    # A hand-crafted '"rms_dbfs": "NaN"' JSON string parses through float() —
    # the parse boundary must drop it (the NaN-pierce fix).
    event = _batch(
        [
            {"seq": 1, "rms_dbfs": "NaN"},
            {"seq": 2, "rms_dbfs": "Infinity"},
            {"seq": 3, "rms_dbfs": -20.0},
        ]
    )
    got = parse_level_batch(event)
    assert [s.seq for s in got] == [3]


def test_parse_level_batch_applies_batch_agc_flag():
    event = _batch([{"seq": 1, "rms_dbfs": -30.0}], agc_frozen=False)
    got = parse_level_batch(event)
    assert got[0].agc_frozen is False  # batch-level superset applies


def test_parse_level_batch_token_scoping():
    event = _batch([{"seq": 1, "rms_dbfs": -30.0}], run_token="run-A")
    assert parse_level_batch(event, run_token="run-A") != []
    assert parse_level_batch(event, run_token="run-B") == []  # another run's slot
    # A tokenless batch is not consumable by a tokened feed.
    tokenless = _batch([{"seq": 1, "rms_dbfs": -30.0}])
    assert parse_level_batch(tokenless, run_token="run-B") == []


def test_phone_reported_abort_from_superset_and_toplevel():
    assert phone_reported_abort(_batch([], aborted=True)) == "phone_aborted"
    ev = _batch([], aborted=True, abort_reason="backgrounded")
    assert phone_reported_abort(ev) == "backgrounded"
    assert phone_reported_abort({"aborted": True, "reason": "x"}) == "x"
    assert phone_reported_abort({}) is None


def test_phone_reported_abort_token_scoping():
    stale = _batch([], aborted=True, abort_reason="old-run", run_token="run-A")
    # A previous run's persisted abort must not cancel this run.
    assert phone_reported_abort(stale, run_token="run-B") is None
    assert phone_reported_abort(stale, run_token="run-A") == "old-run"
    # A tokened feed ignores the unscopeable legacy top-level abort.
    assert phone_reported_abort({"aborted": True}, run_token="run-B") is None


def test_phone_reported_armed_token_scoping():
    armed = _batch([], armed=True, run_token="run-A")
    assert phone_reported_armed(armed, run_token="run-A") is True
    assert phone_reported_armed(armed, run_token="run-B") is False
    assert phone_reported_armed({"armed": True}) is True  # legacy, tokenless only
    assert phone_reported_armed({"armed": True}, run_token="run-B") is False


# --- relay feed: dedup, regression, abort, rate limit, latched warnings -------


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        # Advance fake time AND yield so sibling tasks (tone, lock_now) run.
        self.t += max(s, 0.01)
        await asyncio.sleep(0)


def _feed(status_ref, clock, **kw):
    kw.setdefault("min_read_interval_s", 0.0)
    return RelayLevelFeed(
        read_status=lambda: status_ref["status"],
        monotonic=clock.now,
        **kw,
    )


@pytest.mark.asyncio
async def test_relay_feed_dedupes_and_detects_abort():
    clock = Clock()
    ref = {"status": {"event": _batch([{"seq": 1, "rms_dbfs": -30.0}])}}
    feed = _feed(ref, clock)
    first = await feed.next_samples()
    assert [s.seq for s in first] == [1]
    # Same slot re-read (last-write-wins) → nothing new.
    assert await feed.next_samples() == []
    ref["status"] = {"event": _batch([{"seq": 2, "rms_dbfs": -25.0}])}
    assert [s.seq for s in await feed.next_samples()] == [2]
    ref["status"] = {"event": _batch([], aborted=True, abort_reason="backgrounded")}
    assert await feed.next_samples() == []
    assert feed.aborted_reason == "backgrounded"


@pytest.mark.asyncio
async def test_relay_feed_seq_regression_is_a_new_stream():
    # A phone page reload mid-ramp resets its counter; the feed must consume
    # the new stream rather than dropping every sample as stale (the review's
    # permanent-starvation case).
    clock = Clock()
    ref = {"status": {"event": _batch([{"seq": 50, "rms_dbfs": -30.0}])}}
    feed = _feed(ref, clock)
    assert [s.seq for s in await feed.next_samples()] == [50]
    ref["status"] = {"event": _batch([{"seq": 1, "rms_dbfs": -28.0}])}
    got = await feed.next_samples()
    assert [s.seq for s in got] == [1]  # consumed, not starved
    # And dedup continues within the new stream.
    assert await feed.next_samples() == []


@pytest.mark.asyncio
async def test_relay_feed_ignores_stale_previous_run_slot():
    # The previous run's final event (abort superset + samples, another token)
    # persists in the slot: a fresh tokened feed must ignore it completely —
    # no insta-cancel, no stale samples.
    clock = Clock()
    stale = _batch(
        [{"seq": 9, "rms_dbfs": -14.0}],
        aborted=True,
        abort_reason="backgrounded",
        run_token="run-OLD",
    )
    ref = {"status": {"event": stale}}
    feed = _feed(ref, clock, run_token="run-NEW")
    assert await feed.next_samples() == []
    assert feed.aborted_reason is None
    # The new run's first batch arrives and is consumed normally.
    ref["status"] = {
        "event": _batch([{"seq": 1, "rms_dbfs": -30.0}], run_token="run-NEW")
    }
    assert [s.seq for s in await feed.next_samples()] == [1]


@pytest.mark.asyncio
async def test_relay_feed_rate_limits_reads():
    clock = Clock()
    calls = {"n": 0}

    def read_status():
        calls["n"] += 1
        return {"event": {}}

    feed = RelayLevelFeed(
        read_status=read_status, monotonic=clock.now, min_read_interval_s=0.25
    )
    # 100 calls over 1 s of fake time → at most ~5 HTTP reads.
    for _ in range(100):
        await feed.next_samples()
        clock.t += 0.01
    assert calls["n"] <= 5


@pytest.mark.asyncio
async def test_relay_feed_latches_read_failure_warning(caplog):
    clock = Clock()
    calls = {"n": 0}

    def read_status():
        calls["n"] += 1
        raise RuntimeError("relay down")

    feed = RelayLevelFeed(
        read_status=read_status, monotonic=clock.now, min_read_interval_s=0.0
    )
    with caplog.at_level(logging.WARNING, logger="jasper.correction.level_match"):
        for _ in range(50):
            assert await feed.next_samples() == []
    warnings = [r for r in caplog.records if "status read failed" in r.message]
    assert len(warnings) == 1  # latched, not per tick


@pytest.mark.asyncio
async def test_relay_feed_latches_schema_mismatch_warning(caplog):
    clock = Clock()
    bad = _batch([{"seq": 1, "rms_dbfs": -30.0}])
    bad["level_batch"]["schema"] = 999
    ref = {"status": {"event": bad}}
    feed = _feed(ref, clock)
    with caplog.at_level(logging.WARNING, logger="jasper.correction.level_match"):
        for _ in range(50):
            assert await feed.next_samples() == []
    warnings = [r for r in caplog.records if "schema mismatch" in r.message]
    assert len(warnings) == 1  # a stale mismatched slot warns once, not per tick


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


def test_drift_large_shift_with_scatter_is_suspected_not_ok():
    # The review's classifier hole: a genuine 6+ dB amp move measured with
    # >2 dB band scatter matched neither AMP_MOVED nor ACOUSTIC and fell
    # through to "level is consistent". That quadrant must be non-OK.
    ref = [70.0, 70.0, 70.0, 70.0]
    cur = [64.0, 64.0, 64.0, 58.5]  # mean -7.4, max_dev 4.1
    r = check_level_drift(ref, cur, same_geometry=True)
    assert r.verdict == DriftVerdict.LEVEL_SHIFT_SUSPECTED
    assert "re-level" in r.message


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


class FakeChain:
    """Fake speaker+relay: the mic level tracks commanded volume + gain, streamed
    back through a mutable relay status dict as armed level batches. The Pi's
    host events land in the same status dict (host_event echo works)."""

    def __init__(self, *, gain_db, start_vol, nf=-80.0, run_token=""):
        self.gain_db = gain_db
        self.nf = nf
        self.run_token = run_token
        self._vol = start_vol
        self.commanded = []
        self._seq = 0
        self.status = {"event": {}}
        self.host_events: list[dict] = []
        self._tone = asyncio.Event()

    async def get_vol(self):
        return self._vol

    async def set_vol(self, db):
        self._vol = db
        self.commanded.append(db)

    async def tone(self):
        try:
            await asyncio.wait_for(self._tone.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

    def cancel_tone(self):
        self._tone.set()

    def post_host_event(self, event):
        self.host_events.append(event)
        self.status["host_event"] = event  # the echo path (worker getStatus)

    def read_status(self):
        # Report the mic level at the CURRENT commanded volume as a fresh batch.
        self._seq += 1
        mic = self._vol + self.gain_db
        self.status["event"] = {
            "level_batch": {
                "schema": LEVEL_EVENT_SCHEMA_VERSION,
                "run_token": self.run_token,
                "armed": True,
                "aborted": False,
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


def _session(store=None, **cfg_kw):
    cfg = MeasurementRamp(**{**FAST, **cfg_kw})
    return LevelMatchSession(session_id="s", store=store or LevelLockStore(), config=cfg)


async def _run_geometry(sess, chain, geometry, *, clock=None, **kw):
    clock = clock or Clock()
    return await sess.run_for_geometry(
        geometry,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=kw.pop("read_status", chain.read_status),
        post_host_event=kw.pop("post_host_event", chain.post_host_event),
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
        **kw,
    )


@pytest.mark.asyncio
async def test_level_match_session_locks_and_stores_geometry_lock():
    store = LevelLockStore()
    sess = _session(store)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)
    assert outcome.ramp.state == RampState.LOCKED
    assert outcome.locked
    lock = store.get(MicGeometry.LISTENING_POSITION.value)
    assert lock is not None
    assert lock.main_volume_db == pytest.approx(outcome.ramp.locked_main_volume_db)
    cap = sess.config.dynamic_cap(-30.0)
    # Ramp commands respect the cap (the exact-restore final is exempt but a
    # LOCKED run's final is the lock value, itself <= cap).
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


@pytest.mark.asyncio
async def test_level_match_maxed_out_restores_and_stores_no_lock():
    store = LevelLockStore()
    sess = _session(store)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)

    outcome = await _run_geometry(
        sess, chain, MicGeometry.LISTENING_POSITION.value
    )

    assert outcome.ramp.state == RampState.MAXED_OUT
    assert outcome.locked is False
    assert outcome.lock is None
    assert outcome.ramp.locked_main_volume_db is None
    assert chain._vol == -30.0
    assert store.get(MicGeometry.LISTENING_POSITION.value) is None


@pytest.mark.asyncio
async def test_level_match_terminal_state_reposted_until_echoed():
    # The relay event slot is a read-modify-write race: the terminal state is
    # re-posted until /status echoes it back. FakeChain echoes on the first
    # post, so the loop stops early — and at least one post carries the state.
    sess = _session()
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)
    assert outcome.ramp.state == RampState.LOCKED
    terminal_posts = [
        e for e in chain.host_events if e.get("ramp", {}).get("state") == "locked"
    ]
    assert terminal_posts, "terminal ramp state was never posted"
    # Echo detected on the first verify → no full 5-attempt blast.
    assert len(terminal_posts) <= 2


@pytest.mark.asyncio
async def test_level_match_terminal_state_reposts_without_echo():
    # If the echo never appears (a phone post keeps clobbering host_event),
    # the post is re-attempted the full bounded budget — never exactly once.
    sess = _session()
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)

    def post_no_echo(event):
        chain.host_events.append(event)  # swallowed: never lands in status

    outcome = await _run_geometry(
        sess,
        chain,
        MicGeometry.LISTENING_POSITION.value,
        post_host_event=post_no_echo,
    )
    assert outcome.ramp.state == RampState.LOCKED
    terminal_posts = [
        e for e in chain.host_events if e.get("ramp", {}).get("state") == "locked"
    ]
    assert len(terminal_posts) == LevelMatchSession.TERMINAL_POST_ATTEMPTS


@pytest.mark.asyncio
async def test_level_match_session_honors_phone_abort():
    store = LevelLockStore()
    sess = _session(store)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)

    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        if reads["n"] >= 5:
            return {"event": {"aborted": True, "abort_reason": "backgrounded"}}
        return base()

    outcome = await _run_geometry(
        sess, chain, MicGeometry.LISTENING_POSITION.value, read_status=read_status
    )
    assert outcome.ramp.state == RampState.CANCELLED
    assert outcome.aborted_reason == "backgrounded"
    assert store.get(MicGeometry.LISTENING_POSITION.value) is None
    assert chain.commanded[-1] == pytest.approx(-30.0)  # restored


@pytest.mark.asyncio
async def test_level_match_waits_for_armed_and_times_out():
    # No armed superset ever appears: the run must end without touching the
    # volume or the tone (a premature call must not burn a tone climb).
    sess = _session()
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    outcome = await _run_geometry(
        sess,
        chain,
        MicGeometry.LISTENING_POSITION.value,
        read_status=lambda: {"event": {}},
        armed_timeout_s=3.0,
    )
    assert outcome.ramp.state == RampState.ERROR
    assert outcome.ramp.error == "phone never armed"
    assert chain.commanded == []  # volume untouched
    assert not chain._tone.is_set()  # tone never started/cancelled
    assert outcome.lock is None


@pytest.mark.asyncio
async def test_level_match_token_scoped_retry_ignores_stale_abort():
    # Run 2 of the same relay session: the slot still holds run 1's abort
    # superset. The tokened feed must ignore it and complete run 2 normally.
    sess = _session()
    chain = FakeChain(gain_db=10.0, start_vol=-30.0, run_token="run-2")
    stale_abort = {
        "event": {
            "level_batch": {
                "schema": LEVEL_EVENT_SCHEMA_VERSION,
                "run_token": "run-1",
                "armed": True,
                "aborted": True,
                "abort_reason": "backgrounded",
                "samples": [],
            }
        }
    }
    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        if reads["n"] <= 3:
            return stale_abort  # run 1's persisted slot
        return base()  # then the phone posts run-2 batches

    outcome = await _run_geometry(
        sess,
        chain,
        MicGeometry.LISTENING_POSITION.value,
        read_status=read_status,
        run_token="run-2",
    )
    assert outcome.ramp.state == RampState.LOCKED  # not insta-cancelled
    assert outcome.aborted_reason is None


@pytest.mark.asyncio
async def test_level_match_manual_lock_via_public_seam():
    store = LevelLockStore()
    sess = _session(store, settle_hold_s=5.0, max_loop_latency_s=2.0)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)

    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        if reads["n"] == 6:
            # Manual lock through the PUBLIC seam (the review: don't poke
            # private controller attributes).
            asyncio.get_running_loop().create_task(sess.lock_now())
        return base()

    outcome = await _run_geometry(
        sess,
        chain,
        MicGeometry.NEAR_FIELD_DRIVER.value,
        read_status=read_status,
    )
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
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()

    outcome = await sess.run_level_match(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
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
    assert snap["last"]["ramp"]["restored"] is True
    assert chain._vol == pytest.approx(-30.0)


@pytest.mark.asyncio
async def test_session_level_restore_is_retryable_and_exact_once(tmp_path):
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    await sess.run_level_match(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )

    # A successful level check restores before returning. Reassert the stored
    # target as a sweep window would, then exercise retryable restoration.
    assert sess.level_match_snapshot()["last"]["ramp"]["restored"] is True
    assert await sess.ensure_level_match_volume(chain.set_vol) is True
    assert sess.level_match_snapshot()["last"]["ramp"]["restored"] is False

    async def refused(_db):
        return False

    assert await sess.restore_level_match_volume(refused) is False
    assert sess.level_match_snapshot()["last"]["ramp"]["restored"] is False

    calls = []

    async def restored(db):
        calls.append(db)
        await asyncio.sleep(0)
        return True

    results = await asyncio.gather(
        sess.restore_level_match_volume(restored),
        sess.restore_level_match_volume(restored),
    )
    assert sorted(results) == [False, True]
    assert calls == [-30.0]


@pytest.mark.asyncio
async def test_session_level_match_refuses_to_return_with_restore_unapplied(tmp_path):
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()

    async def setter(db):
        await chain.set_vol(db)
        # The ramp writes succeed; only the post-lock listening restore is
        # refused. This pins the fail-loud, still-retryable lease state.
        if db == -30.0:
            return False
        return True

    with pytest.raises(RuntimeError, match="could not be restored"):
        await sess.run_level_match(
            MicGeometry.LISTENING_POSITION.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=setter,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=chain.read_status,
            post_host_event=chain.post_host_event,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=clock.sleep,
        )

    ramp = sess._last_level_match.ramp
    assert ramp.state is RampState.LOCKED
    assert ramp.restored is False

    async def retry(db):
        await chain.set_vol(db)
        return True

    assert await sess.restore_level_match_volume(retry) is True
    assert ramp.restored is True


@pytest.mark.asyncio
async def test_session_reasserts_locked_volume_before_sweep(tmp_path):
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    outcome = await sess.run_level_match(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    locked_db = outcome.ramp.locked_main_volume_db
    assert outcome.ramp.restored is True
    chain._vol = -48.0
    assert await sess.ensure_level_match_volume(chain.set_vol) is True
    assert chain._vol == locked_db
    assert outcome.ramp.restored is False

    with pytest.raises(RuntimeError, match="already locked"):
        await sess.run_level_match(
            MicGeometry.LISTENING_POSITION.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=chain.set_vol,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=chain.read_status,
            post_host_event=chain.post_host_event,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=clock.sleep,
        )


@pytest.mark.asyncio
async def test_session_ensure_and_restore_share_one_transition_lock(tmp_path):
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    outcome = await sess.run_level_match(
        MicGeometry.LISTENING_POSITION.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    writes: list[float] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_set(db):
        writes.append(db)
        entered.set()
        await release.wait()
        return True

    ensure = asyncio.create_task(sess.ensure_level_match_volume(blocked_set))
    await entered.wait()
    restore = asyncio.create_task(sess.restore_level_match_volume(blocked_set))
    await asyncio.sleep(0)
    # Restore cannot pass the in-flight ensure write.
    assert writes == [outcome.ramp.locked_main_volume_db]
    release.set()
    assert await ensure is True
    assert await restore is True
    assert writes == [outcome.ramp.locked_main_volume_db, -30.0]
    assert outcome.ramp.restored is True


@pytest.mark.asyncio
async def test_crossover_lease_restores_then_scopes_target_to_sweep_window():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    outcome = await lease.run_level_match(
        MicGeometry.NEAR_FIELD_DRIVER.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
        wait_for_armed=False,
        context_id="profile-a",
    )

    assert outcome.ramp.restored is True
    assert chain._vol == pytest.approx(-30.0)
    assert lease.context_id == "profile-a"
    assert await lease.ensure_level_match_volume(chain.set_vol) is True
    assert chain._vol == pytest.approx(outcome.ramp.locked_main_volume_db)
    assert outcome.ramp.restored is False
    assert await lease.restore_level_match_volume(chain.set_vol) is True
    assert chain._vol == pytest.approx(-30.0)
    assert outcome.ramp.restored is True


@pytest.mark.asyncio
async def test_crossover_start_supplies_scheduler_ports(monkeypatch):
    """The production crossover caller need not inject test scheduler seams."""
    from types import SimpleNamespace

    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    seen = {}

    async def fake_run(_session, geometry, *, clock, sleep, **_ports):
        seen.update(geometry=geometry, clock=clock, sleep=sleep)
        return SimpleNamespace(locked=False)

    monkeypatch.setattr(LevelMatchSession, "run_for_geometry", fake_run)
    lease = CrossoverLevelLease()

    async def get_volume():
        return -30.0

    async def set_volume(_db):
        return True

    async def play_tone():
        return None

    await lease.run_level_match(
        MicGeometry.NEAR_FIELD_DRIVER.value,
        get_main_volume_db=get_volume,
        set_main_volume_db=set_volume,
        play_continuous_tone=play_tone,
        cancel_tone=lambda: None,
        read_status=lambda: {},
        post_host_event=None,
        noise_floor_dbfs=-60.0,
    )

    assert seen["geometry"] == MicGeometry.NEAR_FIELD_DRIVER.value
    assert callable(seen["clock"])
    assert seen["sleep"] is asyncio.sleep


def test_session_level_match_snapshot_empty_before_run(tmp_path):
    sess = _make_session(tmp_path)
    snap = sess.level_match_snapshot()
    assert snap["locks"] == {}
    assert snap["last"] is None


@pytest.mark.asyncio
async def test_session_lock_cancel_level_match_are_noops_when_idle(tmp_path):
    # The P2 nit: the seams exist and are safe no-ops when no ramp is running
    # (mirrors lock_autolevel/cancel_autolevel returning False when idle).
    sess = _make_session(tmp_path)
    assert await sess.lock_level_match() is False
    assert await sess.cancel_level_match() is False


@pytest.mark.asyncio
async def test_session_run_level_match_clears_retained_session(tmp_path):
    # The retained LevelMatchSession is cleared after the run so a stale
    # controller can't be locked/cancelled once the ramp has settled.
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    outcome = await sess.run_level_match(
        MicGeometry.NEAR_FIELD_DRIVER.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=chain.read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert outcome.ramp.state == RampState.LOCKED
    # Retained session is torn down; the seams are inert again.
    assert sess._level_match_session is None
    assert await sess.lock_level_match() is False
    assert await sess.cancel_level_match() is False


@pytest.mark.asyncio
async def test_session_lock_level_match_reaches_running_ramp(tmp_path):
    # The whole point of retaining the session (the P2 nit): a Lock issued
    # through the SESSION seam while the ramp is in flight actually reaches the
    # running RampController and locks it. Without retention this was impossible
    # (the LevelMatchSession local was discarded the instant run awaited).
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = Clock()

    locked = {"fired": False}
    reads = {"n": 0}
    base = chain.read_status

    def read_status():
        reads["n"] += 1
        # While the ramp runs, the session must be retained and lockable.
        assert sess._level_match_session is not None
        if reads["n"] == 4 and not locked["fired"]:
            locked["fired"] = True
            # Lock EARLY (the ramp is still climbing from -50 dB, nowhere near
            # the auto-lock window) through the retained SESSION seam — so the
            # lock is provably the manual one, not the auto-settle path.
            asyncio.get_running_loop().create_task(sess.lock_level_match())
        return base()

    outcome = await sess.run_level_match(
        MicGeometry.NEAR_FIELD_DRIVER.value,
        get_main_volume_db=chain.get_vol,
        set_main_volume_db=chain.set_vol,
        play_continuous_tone=chain.tone,
        cancel_tone=chain.cancel_tone,
        read_status=read_status,
        post_host_event=chain.post_host_event,
        noise_floor_dbfs=chain.nf,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert locked["fired"]
    assert outcome.ramp.state == RampState.LOCKED
    # A manual lock freezes the ramp well below the auto-lock window: the settled
    # mic level is far under the safe window's top, proving it wasn't auto-lock.
    assert sess._level_match_session is None


@pytest.mark.asyncio
async def test_session_run_level_match_is_single_flight(tmp_path):
    # Should-fix (review): the retained slot is per-run, so overlapping runs
    # must be REFUSED (mirrors /autolevel/start's "already in progress" guard)
    # — otherwise a second run would stomp the slot and the first's clear would
    # orphan the second's LIVE ramp from its Cancel seam. While the first run is
    # in flight: a second run raises, the seam still reaches the FIRST ramp, and
    # the identity-guarded clear leaves the slot reusable afterwards.
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=2.0, start_vol=-30.0)
    clock = Clock()

    task = asyncio.get_running_loop().create_task(
        sess.run_level_match(
            MicGeometry.NEAR_FIELD_DRIVER.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=chain.set_vol,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=chain.read_status,
            post_host_event=chain.post_host_event,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=clock.sleep,
        )
    )
    # Let the first run start and claim the slot (the ramp climbs from -50 dB,
    # so it is still mid-flight after a few scheduler turns).
    for _ in range(10):
        await asyncio.sleep(0)
        if sess._level_match_session is not None:
            break
    assert sess._level_match_session is not None
    first_session = sess._level_match_session

    with pytest.raises(RuntimeError, match="already in progress"):
        await sess.run_level_match(
            MicGeometry.LISTENING_POSITION.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=chain.set_vol,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=chain.read_status,
            post_host_event=chain.post_host_event,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=clock.sleep,
        )
    # The refused second run did not stomp the first's slot.
    assert sess._level_match_session is first_session

    # The Cancel seam reaches the FIRST (still-running) ramp...
    assert await sess.cancel_level_match() is True
    outcome = await task
    assert outcome.ramp.state == RampState.CANCELLED
    # ...and the identity-guarded clear released the slot for the next run.
    assert sess._level_match_session is None
    assert await sess.cancel_level_match() is False
