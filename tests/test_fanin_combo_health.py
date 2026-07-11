# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure-policy tests for the USB-combo runtime-fallback watcher
(:mod:`jasper.fanin.combo_health`, defect 2026-07-10). Hardware-free: tick
accounting, the broken-signal classifier, and the marker/tick-state lifecycle —
pinned like tests/test_lib_deploy_direction.py pins classify_deploy_direction."""

from __future__ import annotations

import json

from jasper.fanin import combo_health as ch


# ---- fan-in STATUS extraction ----------------------------------------------


def _status(*, source="direct", health="capturing", reopens=0,
            card_gen_reopens=0, present=True, frames_read=1000, with_direct=True):
    entry = {"label": "usbsink", "source": source, "frames_read": frames_read}
    if with_direct:
        entry["direct"] = {
            "present": present,
            "health": health,
            "reopens": reopens,
            "card_gen_reopens": card_gen_reopens,
        }
    return {"inputs": [{"label": "airplay", "source": "lane"}, entry]}


def test_extract_none_when_no_direct_lane():
    # A non-combo box (usbsink lane is an ordinary aloop lane) -> nothing to watch.
    assert ch.extract_direct_sample(_status(source="lane")) is None
    # Missing / malformed STATUS -> None (fail-soft).
    assert ch.extract_direct_sample(None) is None
    assert ch.extract_direct_sample({"inputs": "nope"}) is None
    assert ch.extract_direct_sample({}) is None


def test_extract_reads_direct_health_and_counters():
    s = ch.extract_direct_sample(
        _status(health="broken", reopens=3, card_gen_reopens=1, frames_read=42)
    )
    assert s is not None
    assert s.health == "broken"
    assert s.reopens == 3
    assert s.card_gen_reopens == 1
    assert s.frames_read == 42
    assert s.present is True


# ---- sample_is_broken -------------------------------------------------------


def _sample(health="capturing", reopens=0, card_gen_reopens=0, present=True,
            frames_read=1000):
    return ch.DirectHealthSample(
        present=present, health=health, reopens=reopens,
        card_gen_reopens=card_gen_reopens, frames_read=frames_read,
    )


def test_broken_on_fanin_health_broken():
    # fan-in's own instantaneous flowing->dead classification is sufficient.
    assert ch.sample_is_broken(_sample(health="broken"), None) is True


def test_idle_and_capturing_are_never_broken_without_churn():
    # The binding constraint: an idle/no-host/capturing sample with NO reopen
    # churn since the last tick is NOT broken.
    prev = _sample(reopens=5, card_gen_reopens=2)
    assert ch.sample_is_broken(_sample(health="idle", reopens=5,
                                       card_gen_reopens=2), prev) is False
    assert ch.sample_is_broken(_sample(health="capturing", reopens=5,
                                       card_gen_reopens=2), prev) is False


def test_broken_on_reopen_churn_while_capturing():
    # A reopen-counter climb is a real break ONLY while the lane is actively
    # capturing: a break of a live, actively-playing stream re-establishes capture
    # within ms of each self-heal reopen, so the ~3-min poll reads "capturing".
    prev = _sample(health="capturing", reopens=5, card_gen_reopens=2)
    # zombie reopen climbed while capturing -> broken.
    assert ch.sample_is_broken(
        _sample(health="capturing", reopens=6, card_gen_reopens=2), prev) is True
    # liveness-probe reopen climbed while capturing -> broken.
    assert ch.sample_is_broken(
        _sample(health="capturing", reopens=5, card_gen_reopens=3), prev) is True


def test_idle_reopen_churn_is_never_broken():
    # Defect 2026-07-11: the reopen counters climb on a purely IDLE box too — a Mac
    # left connected as the default output streams digital silence, and the UAC2
    # gadget routinely re-enumerates (host sleep/wake, USB autosuspend, a /sources/
    # toggle), each rebuild a normal self-heal that bumps the counters. The binding
    # invariant is that an idle/unplugged host must NEVER trip the fallback, so a
    # counter climb while health="idle" must read NOT broken. These are the two
    # exact journal shapes that false-disarmed jts.local.
    # 07:48 disarm cause: health=idle, zombie reopens 7->9 (card_gen flat).
    prev_zombie = _sample(health="idle", reopens=7, card_gen_reopens=0, frames_read=9559273)
    assert ch.sample_is_broken(
        _sample(health="idle", reopens=9, card_gen_reopens=0), prev_zombie) is False
    # 19:11 disarm cause: health=idle, liveness-probe card_gen 1->2 (reopens 0).
    prev_cardgen = _sample(health="idle", reopens=0, card_gen_reopens=1, frames_read=0)
    assert ch.sample_is_broken(
        _sample(health="idle", reopens=0, card_gen_reopens=2), prev_cardgen) is False


def test_fanin_restart_counter_reset_is_not_broken():
    # A fan-in restart zeroes the cumulative counters; cur < prev must read NOT
    # broken (a restart never false-trips the fallback).
    prev = _sample(reopens=9, card_gen_reopens=4)
    assert ch.sample_is_broken(_sample(reopens=0, card_gen_reopens=0), prev) is False


def test_first_tick_no_prev_only_broken_on_health_field():
    # With no previous sample the delta path can't fire; only health=="broken" trips.
    assert ch.sample_is_broken(_sample(reopens=99), None) is False
    assert ch.sample_is_broken(_sample(health="broken"), None) is True


# ---- decide_health_tick (consecutive-broken accounting) --------------------


def test_steady_healthy_is_journal_quiet():
    dec = ch.decide_health_tick(_sample(), ch.TickState.empty())
    assert dec.broken is False
    assert dec.disarm is False
    assert dec.transition == ""  # nothing logs on a healthy tick
    assert dec.next_state.consecutive_broken == 0


def test_first_broken_then_sustained_disarms_at_two():
    prev = ch.TickState.empty()
    # Tick 1: broken via reopen churn (need a prev sample for the delta).
    prev = ch.TickState(consecutive_broken=0, sample=_sample(reopens=1))
    d1 = ch.decide_health_tick(_sample(reopens=2), prev)
    assert d1.broken is True
    assert d1.disarm is False
    assert d1.transition == "first_broken"
    assert d1.next_state.consecutive_broken == 1
    # Tick 2: still broken -> sustained -> disarm.
    d2 = ch.decide_health_tick(_sample(reopens=3), d1.next_state)
    assert d2.disarm is True
    assert d2.transition == "sustained_broken"
    assert d2.next_state.consecutive_broken == 2


def test_recovery_resets_and_logs():
    prev = ch.TickState(consecutive_broken=1, sample=_sample(reopens=5))
    # No churn, healthy -> recovered.
    dec = ch.decide_health_tick(_sample(health="idle", reopens=5), prev)
    assert dec.broken is False
    assert dec.disarm is False
    assert dec.transition == "recovered"
    assert dec.next_state.consecutive_broken == 0


def test_single_transient_broken_tick_never_disarms():
    # One broken tick (consecutive=1) followed by recovery must NOT disarm.
    prev = ch.TickState(consecutive_broken=0, sample=_sample(reopens=1))
    d1 = ch.decide_health_tick(_sample(reopens=2), prev)
    assert d1.disarm is False
    d2 = ch.decide_health_tick(_sample(health="idle", reopens=2), d1.next_state)
    assert d2.disarm is False
    assert d2.next_state.consecutive_broken == 0


# ---- tick-state persistence -------------------------------------------------


def test_tick_state_roundtrip(tmp_path):
    path = str(tmp_path / "tick.json")
    state = ch.TickState(consecutive_broken=1, sample=_sample(reopens=7))
    ch.write_tick_state(state, path)
    back = ch.read_tick_state(path)
    assert back.consecutive_broken == 1
    assert back.sample is not None
    assert back.sample.reopens == 7


def test_tick_state_missing_is_empty(tmp_path):
    assert ch.read_tick_state(str(tmp_path / "nope.json")) == ch.TickState.empty()


def test_tick_state_corrupt_is_empty(tmp_path):
    p = tmp_path / "tick.json"
    p.write_text("{not json")
    assert ch.read_tick_state(str(p)).consecutive_broken == 0


# ---- fallback marker lifecycle ---------------------------------------------


def test_marker_write_read_clear(tmp_path):
    path = str(tmp_path / "fallback.json")
    assert ch.fallback_active(path) is False
    assert ch.read_fallback_marker(path) is None
    assert ch.write_fallback_marker("capture broke", path, now=123.0) is True
    assert ch.fallback_active(path) is True
    marker = ch.read_fallback_marker(path)
    assert marker is not None
    assert marker.reason == "capture broke"
    assert marker.at_epoch == 123.0
    # persisted shape is stable JSON
    assert json.loads((tmp_path / "fallback.json").read_text())["reason"] == "capture broke"
    assert ch.clear_fallback_marker(path) is True
    assert ch.fallback_active(path) is False
    # Clearing an absent marker is a no-op False.
    assert ch.clear_fallback_marker(path) is False


def test_corrupt_marker_reads_as_absent(tmp_path):
    # A corrupt marker must fail toward re-arming (the ordinary auto default), not
    # freeze the box off the combo on one bad byte.
    p = tmp_path / "fallback.json"
    p.write_text("garbage")
    assert ch.read_fallback_marker(str(p)) is None
    assert ch.fallback_active(str(p)) is False
