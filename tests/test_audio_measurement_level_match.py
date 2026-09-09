# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level-match adapter: level feed, geometry lock, drift check.

The kernel ramp math is tested in ``test_audio_measurement_ramp.py``; here we
test the adapter glue with a fake feed (a status dict the feed reads) and a
fake clock — no network, no CamillaDSP. The protocol-honesty items the review
demanded are pinned here: run-token scoping (a previous run's persisted slot
never cancels or feeds a retry), seq-regression as a new stream (phone page
reload), the armed gate (no tone until the phone armed), the latched
journal-spam warnings, and the terminal host event re-posted until the capture
echoes it back.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.audio_measurement.level_match import (
    LevelLockStore,
    LevelMatchSession,
    MeasurementLevelLock,
    MicGeometry,
    LevelStatusFeed,
    parse_level_batch,
    phone_reported_abort,
    phone_reported_armed,
)
from jasper.audio_measurement.ramp import (
    LEVEL_EVENT_SCHEMA_VERSION,
    MeasurementRamp,
    RampData,
    RampLockKind,
    RampState,
)

FAST = dict(settle_hold_s=0.5, max_loop_latency_s=0.5, settle_min_samples=2)


def test_room_cap_keeps_attenuated_stimulus_inside_digital_envelope():
    from jasper.audio_measurement.excitation import (
        AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    )
    from jasper.audio_measurement.ramp import (
        LISTENING_POSITION_CAP_BUMP_DB,
        LISTENING_POSITION_CAP_CEIL_DB,
    )

    shared = MeasurementRamp()
    room = MeasurementRamp(
        cap_bump_db=LISTENING_POSITION_CAP_BUMP_DB,
        cap_ceil_db=LISTENING_POSITION_CAP_CEIL_DB,
    )

    assert shared.cap_ceil_db == -3.0
    assert LISTENING_POSITION_CAP_BUMP_DB == 15.0
    assert room.cap_ceil_db == 0.0
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert (
        room.cap_ceil_db + AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    ) == -12.0


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


def test_parse_level_batch_applies_batch_agc_unattested_flag():
    # New-client wire shape for an unattested (undefined AGC) phone: the
    # batch superset carries agc_unattested even when a per-sample entry
    # omits it (mirrors the existing agc_frozen cascade).
    event = _batch(
        [{"seq": 1, "rms_dbfs": -30.0}], agc_frozen=False, agc_unattested=True
    )
    got = parse_level_batch(event)
    assert got[0].agc_frozen is False
    assert got[0].agc_unattested is True

    # Per-sample values win over the batch superset when both are present.
    event2 = _batch(
        [{"seq": 1, "rms_dbfs": -30.0, "agc_unattested": False}],
        agc_frozen=False,
        agc_unattested=True,
    )
    got2 = parse_level_batch(event2)
    assert got2[0].agc_unattested is False


def test_parse_level_batch_old_server_shape_ignores_unattested_field():
    # Mixed-version safety: an OLD server (this parser, before agc_unattested
    # existed) reading a NEW client's unattested batch sees only agc_frozen —
    # always false for an unattested chain at the wire level (never true) —
    # so it falls back to the pre-existing "never trust" behavior instead of
    # silently trusting an unproven chain. Simulated here by parsing the
    # batch and confirming agc_frozen alone (ignoring agc_unattested) already
    # carries the safe signal.
    event = _batch(
        [{"seq": 1, "rms_dbfs": -30.0}], agc_frozen=False, agc_unattested=True
    )
    got = parse_level_batch(event)
    assert got[0].agc_frozen is False


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


# --- level feed: dedup, regression, abort, rate limit, latched warnings -------


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
    return LevelStatusFeed(
        read_status=lambda: status_ref["status"],
        monotonic=clock.now,
        **kw,
    )


async def test_level_feed_dedupes_and_detects_abort():
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


async def test_level_feed_seq_regression_is_a_new_stream():
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


async def test_level_feed_ignores_stale_previous_run_slot():
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


async def test_level_feed_rate_limits_reads():
    clock = Clock()
    calls = {"n": 0}

    def read_status():
        calls["n"] += 1
        return {"event": {}}

    feed = LevelStatusFeed(
        read_status=read_status, monotonic=clock.now, min_read_interval_s=0.25
    )
    # 100 calls over 1 s of fake time → at most ~5 HTTP reads.
    for _ in range(100):
        await feed.next_samples()
        clock.t += 0.01
    assert calls["n"] <= 5


async def test_level_feed_latches_read_failure_warning(caplog):
    clock = Clock()
    calls = {"n": 0}

    def read_status():
        calls["n"] += 1
        raise RuntimeError("feed down")

    feed = LevelStatusFeed(
        read_status=read_status, monotonic=clock.now, min_read_interval_s=0.0
    )
    with caplog.at_level(logging.WARNING, logger="jasper.audio_measurement.level_match"):
        for _ in range(50):
            assert await feed.next_samples() == []
    warnings = [r for r in caplog.records if "status read failed" in r.message]
    assert len(warnings) == 1  # latched, not per tick


async def test_level_feed_latches_schema_mismatch_warning(caplog):
    clock = Clock()
    bad = _batch([{"seq": 1, "rms_dbfs": -30.0}])
    bad["level_batch"]["schema"] = 999
    ref = {"status": {"event": bad}}
    feed = _feed(ref, clock)
    with caplog.at_level(logging.WARNING, logger="jasper.audio_measurement.level_match"):
        for _ in range(50):
            assert await feed.next_samples() == []
    warnings = [r for r in caplog.records if "schema mismatch" in r.message]
    assert len(warnings) == 1  # a stale mismatched slot warns once, not per tick


# --- MeasurementLevelLock.from_ramp sources agc_frozen from agc_trusted -------


def test_lock_from_ramp_attested_is_byte_identical():
    data = RampData(
        state=RampState.LOCKED,
        locked_main_volume_db=-18.0,
        lock_kind=RampLockKind.IN_WINDOW,
        agc_frozen=True,
    )
    lock = MeasurementLevelLock.from_ramp(MicGeometry.LISTENING_POSITION.value, data)
    assert lock.agc_frozen is True


def test_lock_from_ramp_unattested_verified_reads_as_trustworthy():
    # A verified-unattested run has agc_frozen=False at the wire/RampData
    # level (by design — see LevelSample), but agc_verified=True. The lock
    # must read as trustworthy (agc_frozen=True) so a downstream consumer
    # treats it identically to an attested lock.
    data = RampData(
        state=RampState.LOCKED,
        locked_main_volume_db=-18.0,
        lock_kind=RampLockKind.IN_WINDOW,
        agc_frozen=False,
        agc_unattested=True,
        agc_verified=True,
    )
    assert data.agc_trusted is True
    lock = MeasurementLevelLock.from_ramp(MicGeometry.LISTENING_POSITION.value, data)
    assert lock.agc_frozen is True


def test_lock_from_ramp_explicit_agc_on_reads_as_untrustworthy():
    data = RampData(
        state=RampState.LOCKED,
        locked_main_volume_db=-18.0,
        lock_kind=RampLockKind.MANUAL,
        agc_frozen=False,
    )
    lock = MeasurementLevelLock.from_ramp(MicGeometry.LISTENING_POSITION.value, data)
    assert lock.agc_frozen is False


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
    store.discard(MicGeometry.NEAR_FIELD_DRIVER.value)
    assert store.get(MicGeometry.NEAR_FIELD_DRIVER.value) is None
    assert store.get(MicGeometry.LISTENING_POSITION.value) is listen


# --- LevelMatchSession end-to-end with a fake feed ---------------------------


class FakeChain:
    """Fake speaker+feed: the mic level tracks commanded volume + gain, streamed
    back through a mutable status dict as armed level batches. The Pi's
    host events land in the same status dict (host_event echo works)."""

    def __init__(
        self,
        *,
        gain_db,
        start_vol,
        nf=-80.0,
        run_token="",
        agc_unattested=False,
    ):
        self.gain_db = gain_db
        self.nf = nf
        self.run_token = run_token
        self.agc_unattested = agc_unattested
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
                        "agc_frozen": not self.agc_unattested,
                        "agc_unattested": self.agc_unattested,
                    }
                ],
            }
        }
        return self.status


def _session(store=None, **cfg_kw):
    cfg = MeasurementRamp(**{**FAST, **cfg_kw})
    return LevelMatchSession(
        session_id="s", store=store or LevelLockStore(), config=cfg
    )


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


async def test_level_match_session_locks_and_stores_geometry_lock():
    store = LevelLockStore()
    sess = _session(store)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)
    assert outcome.ramp.state == RampState.LOCKED
    assert outcome.ramp.lock_kind is RampLockKind.IN_WINDOW
    assert outcome.locked
    lock = store.get(MicGeometry.LISTENING_POSITION.value)
    assert lock is not None
    assert lock.main_volume_db == pytest.approx(outcome.ramp.locked_main_volume_db)
    cap = sess.config.dynamic_cap(-30.0)
    # Ramp commands respect the cap (the exact-restore final is exempt but a
    # LOCKED run's final is the lock value, itself <= cap).
    for vol in chain.commanded:
        assert vol <= cap + 1e-9


async def test_level_match_session_unattested_verified_locks_like_attested():
    """End-to-end through the full feed adapter: an unattested (undefined
    AGC) chain — the wire-level agc_frozen is false on every sample — still
    locks IN_WINDOW once the staircase's slope is empirically verified, and
    the stored lock reads as trustworthy (agc_frozen=True), identically to
    the attested test above."""
    store = LevelLockStore()
    sess = _session(store)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0, agc_unattested=True)
    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)
    assert outcome.ramp.state == RampState.LOCKED
    assert outcome.ramp.lock_kind is RampLockKind.IN_WINDOW
    assert outcome.ramp.agc_unattested is True
    assert outcome.ramp.agc_verified is True
    assert outcome.locked
    lock = store.get(MicGeometry.LISTENING_POSITION.value)
    assert lock is not None
    assert lock.agc_frozen is True  # verified-unattested reads as trustworthy


async def test_level_match_maxed_out_restores_and_stores_no_lock():
    store = LevelLockStore()
    sess = _session(store, cap_bump_db=6.0, cap_ceil_db=-6.0)
    # Unknown ambient floor cannot satisfy the bounded-low evidence contract.
    chain = FakeChain(gain_db=2.0, start_vol=-30.0, nf=None)

    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)

    assert outcome.ramp.state == RampState.MAXED_OUT
    assert outcome.locked is False
    assert outcome.lock is None
    assert outcome.ramp.locked_main_volume_db is None
    assert chain._vol == -30.0
    assert store.get(MicGeometry.LISTENING_POSITION.value) is None


async def test_level_match_persists_bounded_low_evidence_in_lock_snapshot():
    store = LevelLockStore()
    sess = _session(store, allow_bounded_low_level=True)
    original = -15.15
    cap = sess.config.dynamic_cap(original)
    chain = FakeChain(
        gain_db=-33.07 - cap,
        start_vol=original,
        nf=-44.53,
    )

    outcome = await _run_geometry(sess, chain, MicGeometry.NEAR_FIELD_DRIVER.value)

    assert outcome.locked is True
    assert outcome.bounded_low_level is True
    assert outcome.ramp.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    lock = store.get(MicGeometry.NEAR_FIELD_DRIVER.value)
    assert lock is not None
    assert lock.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    snapshot = outcome.snapshot()
    assert snapshot["ramp"]["lock_kind"] == "bounded_low_level"
    assert snapshot["ramp"]["settled_mic_dbfs"] == -33.07
    assert snapshot["ramp"]["settled_snr_db"] == 11.46
    assert snapshot["ramp"]["window_shortfall_db"] == 13.07
    assert snapshot["lock"]["lock_kind"] == "bounded_low_level"
    assert snapshot["lock"]["settled_mic_dbfs"] == -33.07
    assert snapshot["lock"]["settled_snr_db"] == 11.46
    assert snapshot["lock"]["window_shortfall_db"] == 13.07


async def test_level_match_terminal_state_repost_never_stops_on_first_echo():
    # The capture event slot is a whole-meta read-modify-write race: a phone
    # batch post whose read predates the Pi's terminal write reverts
    # host_event when it lands. Stopping the re-post schedule on a single
    # confirmed echo left exactly that revert window with nobody re-posting —
    # the phone then missed the terminal and reported a false timeout
    # (2026-07-15 JTS3 tweeter ramp: locked at 33.8 s server-side, phone
    # showed "did not finish the level check before the timeout"). The full
    # bounded schedule must run even when the echo confirms immediately.
    sess = _session()
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    outcome = await _run_geometry(sess, chain, MicGeometry.LISTENING_POSITION.value)
    assert outcome.ramp.state == RampState.LOCKED
    terminal_posts = [
        e for e in chain.host_events if e.get("ramp", {}).get("state") == "locked"
    ]
    # FakeChain echoes on the first post; the schedule still runs to its end.
    assert len(terminal_posts) == LevelMatchSession.TERMINAL_POST_ATTEMPTS


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


async def test_level_match_token_scoped_retry_ignores_stale_abort():
    # Run 2 of the same capture session: the slot still holds run 1's abort
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


def test_crossover_lease_phone_timeout_never_undercuts_server_safety_timeout():
    """The phone's hard capture deadline must always exceed the server's own
    ``MeasurementRamp.safety_timeout`` for the SAME ramp config, with the
    documented grace margin — otherwise the phone can declare a false timeout
    failure while the Pi's ramp is still legitimately running (the JTS3
    2026-07-15 crossover level-ramp incident: the phone's flat, disconnected
    hard-timeout constant undercut the server's real ~58 s safety timeout)."""

    import math

    from jasper.active_speaker.crossover_level_run import PHONE_TRANSPORT_GRACE_S
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    for geometry in (
        "near_field_driver:mono:woofer",
        "reference_axis_driver:mono:tweeter",
    ):
        server_safety_timeout_s = lease._ramp_config_for_geometry(
            geometry
        ).safety_timeout
        phone_timeout_ms = lease.phone_hard_timeout_ms(geometry)

        assert phone_timeout_ms == math.ceil(
            (server_safety_timeout_s + PHONE_TRANSPORT_GRACE_S) * 1000.0
        )
        assert phone_timeout_ms > server_safety_timeout_s * 1000.0


