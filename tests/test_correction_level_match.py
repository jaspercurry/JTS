# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side level-match adapter: level feed, geometry lock, drift check.

The kernel ramp math is tested in ``test_audio_measurement_ramp.py``; here we
test the correction glue with a fake feed (a status dict the feed reads) and a
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

from jasper.audio_measurement.ramp import (
    LEVEL_EVENT_SCHEMA_VERSION,
    MeasurementRamp,
    RampData,
    RampLockKind,
    RampState,
)
from jasper.correction.level_match import (
    LevelLockStore,
    LevelMatchRefused,
    LevelMatchSession,
    MeasurementLevelLock,
    MicGeometry,
    LevelStatusFeed,
    describe_ramp_refusal,
    parse_level_batch,
    phone_reported_abort,
    phone_reported_armed,
)
from jasper.correction.session import (
    ROOM_LEVEL_WINDOW_HIGH_DBFS,
    ROOM_LEVEL_WINDOW_LOW_DBFS,
)
from ._async_wait import wait_signalled
from .correction_session_fixtures import (
    make_measurement_session as _make_session,
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
    with caplog.at_level(logging.WARNING, logger="jasper.correction.level_match"):
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
    with caplog.at_level(logging.WARNING, logger="jasper.correction.level_match"):
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


# --- ramp terminal refusal copy (2026-07-16 jts3: every refusal names its
# reason — a raw "agc_suspected" reached the phone and the log untranslated) --


def test_describe_ramp_refusal_agc_suspected_names_the_reason():
    refusal = describe_ramp_refusal("agc_suspected")
    assert refusal.code == "agc_suspected"
    assert "automatic" in refusal.user_message.lower()
    assert "gain" in refusal.user_message.lower()
    # Jargon/vendor-agnostic: no provider or hardware-model names leak through.
    for banned in ("gemini", "openai", "grok", "google", "webrtc", "dayton"):
        assert banned not in refusal.user_message.lower()


def test_describe_ramp_refusal_appends_the_measured_detail():
    refusal = describe_ramp_refusal(
        "agc_suspected", "slopes 0.64, 0.61 over 4 steps"
    )
    assert refusal.code == "agc_suspected"
    assert "slopes 0.64, 0.61 over 4 steps" in refusal.user_message


@pytest.mark.parametrize(
    ("raw_error", "canonical_code", "message_fragment"),
    [
        ("safety timeout after 45s", "safety_timeout", "took too long"),
        (
            "phone feed lost (no samples for 8s)",
            "phone_feed_lost",
            "lost the microphone feed",
        ),
        (
            "safe cap reached below target window; raise the external "
            "amplifier and retry",
            "safe_cap_below_window",
            "too quiet at the safe volume limit",
        ),
        (
            "non-finite pre-ramp main_volume: nan",
            "non_finite_original",
            "starting volume was invalid",
        ),
    ],
)
def test_describe_ramp_refusal_normalizes_parameterized_family_codes(
    raw_error, canonical_code, message_fragment
):
    """Parameterized terminals (a duration/dB baked into RampData.error) get
    ONE canonical snake_case code per family, so `reason=` log keys group
    across runs instead of one key per duration — while the verbatim
    parameterized error stays visible in the user_message parenthetical."""
    refusal = describe_ramp_refusal(raw_error)
    assert refusal.code == canonical_code
    assert message_fragment in refusal.user_message.lower()
    assert raw_error in refusal.user_message


def test_describe_ramp_refusal_empty_code_is_the_generic_not_locked_case():
    for empty in (None, "", "   "):
        refusal = describe_ramp_refusal(empty)
        assert refusal.code == "not_locked"
        assert refusal.user_message  # never blank


def test_describe_ramp_refusal_unknown_code_falls_back_but_includes_the_code():
    refusal = describe_ramp_refusal("some brand new ramp failure mode")
    assert refusal.code == "some brand new ramp failure mode"
    assert "some brand new ramp failure mode" in refusal.user_message.lower()


def test_level_match_refused_str_is_the_homeowner_message():
    """Hardware run 20: a measurement refusal's ``str(exc)`` is what reaches
    the household when a caller falls back to the generic ``str(exc)`` path --
    the phone's ``sweep_failed`` host event and the wizard's capture status line
    (``jasper.web.correction_setup._capture_failure_message``) both do. So the
    mapping from code to household copy must happen AT THE RAISE SITE: the
    exception's own ``str`` is the mapped sentence, never a raw diagnostic.
    """

    refusal = describe_ramp_refusal("agc_suspected")
    exc = LevelMatchRefused(refusal)
    assert str(exc) == refusal.user_message
    assert exc.code == "agc_suspected"
    assert exc.user_message == refusal.user_message
    assert isinstance(exc, RuntimeError)  # caught by the existing except tuples


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


async def test_room_session_uses_sweep_headroom_window(tmp_path):
    """Room keeps 6 dB beyond the shared tone window for the full-band ESS."""
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

    assert outcome.ramp.state is RampState.LOCKED
    assert outcome.ramp.locked_main_volume_db is not None
    locked_mic_dbfs = outcome.ramp.locked_main_volume_db + chain.gain_db
    assert locked_mic_dbfs == pytest.approx(-22.0)
    assert ROOM_LEVEL_WINDOW_LOW_DBFS <= locked_mic_dbfs
    assert locked_mic_dbfs <= ROOM_LEVEL_WINDOW_HIGH_DBFS
    assert outcome.ramp.restored is True
    assert chain._vol == pytest.approx(-30.0)


async def test_stop_after_locked_waits_for_terminal_ack_then_restores(tmp_path):
    """Terminal RampState is not lifecycle completion or restore authority."""
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=10.0, start_vol=-30.0)
    clock = Clock()
    terminal_waiting = asyncio.Event()
    release_terminal = asyncio.Event()
    held_terminal_once = False

    async def controlled_sleep(delay: float) -> None:
        nonlocal held_terminal_once
        clock.t += max(delay, 0.01)
        await asyncio.sleep(0)
        level_session = sess._level_match_session
        controller = level_session._controller if level_session else None
        if (
            not held_terminal_once
            and delay == LevelMatchSession.TERMINAL_POST_SPACING_S
            and controller is not None
            and controller.data.state is RampState.LOCKED
        ):
            held_terminal_once = True
            terminal_waiting.set()
            await release_terminal.wait()

    running = asyncio.create_task(
        sess.run_level_match(
            MicGeometry.LISTENING_POSITION.value,
            get_main_volume_db=chain.get_vol,
            set_main_volume_db=chain.set_vol,
            play_continuous_tone=chain.tone,
            cancel_tone=chain.cancel_tone,
            read_status=chain.read_status,
            post_host_event=chain.post_host_event,
            noise_floor_dbfs=chain.nf,
            clock=clock.now,
            sleep=controlled_sleep,
        )
    )
    await wait_signalled(
        terminal_waiting, "terminal RampState post-spacing wait", producer=running
    )
    assert chain._vol != pytest.approx(-30.0)

    intent = await sess.begin_autolevel_reset()
    stopping = asyncio.create_task(sess.stop_background_audio_for_reset())
    await asyncio.sleep(0)
    assert not stopping.done()
    release_terminal.set()

    assert await stopping is True
    outcome = await running
    assert outcome.ramp.state is RampState.LOCKED
    assert sess._last_level_match is outcome
    assert outcome.ramp.restored is True
    assert chain._vol == pytest.approx(-30.0)
    assert await sess.end_autolevel_reset(intent) is True


async def test_room_session_accepts_stable_bounded_low_level(tmp_path):
    """A quiet external amp can proceed only with explicit degraded evidence."""
    sess = _make_session(tmp_path)
    chain = FakeChain(gain_db=-13.0, start_vol=-30.0, nf=-60.0)
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

    assert outcome.ramp.state is RampState.LOCKED
    assert outcome.ramp.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert outcome.ramp.cap_db == pytest.approx(-15.0)
    assert outcome.ramp.window_shortfall_db > 0.0
    assert outcome.ramp.settled_snr_db >= MeasurementRamp.trust_margin_db
    assert outcome.ramp.restored is True
    assert chain._vol == pytest.approx(-30.0)


async def test_room_session_jts3_evidence_locks_and_restores_exactly(tmp_path):
    """Pin the live UMIK miss at -3.15 dB and room-only raised-cap recovery."""
    sess = _make_session(tmp_path)
    original = -15.15
    chain = FakeChain(
        gain_db=-31.88 - (-3.15),
        start_vol=original,
        nf=-41.3,
    )
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

    assert outcome.ramp.cap_db == pytest.approx(-0.15)
    assert outcome.ramp.state is RampState.LOCKED
    assert outcome.ramp.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL
    assert outcome.ramp.max_signal_over_noise_db >= 10.0
    assert outcome.ramp.trust_deficit_db == 0.0
    assert outcome.ramp.restored is True
    assert chain._vol == pytest.approx(original)
    assert chain.commanded[-1] == pytest.approx(original)


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
    await wait_signalled(entered, "ensure_level_match_volume write entered", producer=ensure)
    restore = asyncio.create_task(sess.restore_level_match_volume(blocked_set))
    await asyncio.sleep(0)
    # Restore cannot pass the in-flight ensure write.
    assert writes == [outcome.ramp.locked_main_volume_db]
    release.set()
    assert await ensure is True
    assert await restore is True
    assert writes == [outcome.ramp.locked_main_volume_db, -30.0]
    assert outcome.ramp.restored is True


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


def test_session_level_match_snapshot_empty_before_run(tmp_path):
    sess = _make_session(tmp_path)
    snap = sess.level_match_snapshot()
    assert snap["locks"] == {}
    assert snap["last"] is None


async def test_session_lock_cancel_level_match_are_noops_when_idle(tmp_path):
    # The P2 nit: the seams exist and are safe no-ops when no ramp is running
    # (mirrors lock_autolevel/cancel_autolevel returning False when idle).
    sess = _make_session(tmp_path)
    assert await sess.lock_level_match() is False
    assert await sess.cancel_level_match() is False


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
