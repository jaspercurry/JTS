# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.volume_diagnostics import build_volume_policy_snapshot


def _snapshot(**overrides):
    values = {
        "active_source": "spotify",
        "listening_level": 50,
        "main_volume_db": 0.0,
        "persisted_main_volume_db": 0.0,
        "mux_status": {},
        "diagnostics": {},
    }
    values.update(overrides)
    return build_volume_policy_snapshot(**values)


def test_volume_policy_uses_live_guard_when_persistence_claims_clear():
    policy = _snapshot(
        listening_level=90,
        main_volume_db=-13.13,
        mux_status={"active_source": "spotify"},
    )

    assert policy["source"] == "spotify"
    assert policy["carrier"] == "camilla_guard"
    assert policy["push_guard_active"] is True
    assert policy["guard_db"] == -13.13
    assert policy["guard_reason"] == "derived_from_live_camilla_guard"


def test_persisted_guard_wins_and_matching_diagnostics_add_context():
    last_push = {"ok": False, "reason": "no_active_device"}
    last_clear = {"ok": True, "reason": "push_confirmed"}
    last_handoff = {"from": "airplay", "to": "spotify"}

    policy = _snapshot(
        main_volume_db=-13.13,
        persisted_main_volume_db=-7.126,
        mux_status={"last_handoff": last_handoff},
        diagnostics={
            "push_guard": {
                "source": "spotify",
                "reason": "source_handoff_push_failed",
                "context": "handoff",
                "previous_db": -2.345,
            },
            "last_source_push_result": last_push,
            "last_clear_event": last_clear,
        },
    )

    assert policy["guard_db"] == -7.13
    assert policy["guard_reason"] == "source_handoff_push_failed"
    assert policy["guard_context"] == "handoff"
    assert policy["previous_db"] == -2.35
    assert policy["last_source_push_result"] is last_push
    assert policy["last_clear_event"] is last_clear
    assert policy["last_handoff"] is last_handoff


def test_mismatched_guard_diagnostics_cannot_describe_active_source():
    policy = _snapshot(
        persisted_main_volume_db=-6.0,
        diagnostics={
            "push_guard": {
                "source": "bluetooth",
                "reason": "wrong-source",
                "context": "stale",
                "previous_db": -4.0,
            }
        },
    )

    assert policy["guard_reason"] == "derived_from_persisted_camilla_guard"
    assert policy["guard_context"] is None
    assert policy["previous_db"] is None


def test_push_source_without_guard_uses_source_carrier_at_epsilon_boundary():
    policy = _snapshot(main_volume_db=-1.0, persisted_main_volume_db=None)

    assert policy["volume_mode"] == "push"
    assert policy["carrier"] == "source"
    assert policy["push_guard_active"] is False
    assert policy["guard_db"] is None
    assert policy["guard_reason"] is None


def test_camilla_master_source_ignores_negative_guard_values():
    policy = _snapshot(
        active_source="airplay",
        main_volume_db=-8.0,
        persisted_main_volume_db=-7.0,
    )

    assert policy["source"] == "airplay"
    assert policy["volume_mode"] == "camilla_master"
    assert policy["carrier"] == "camilla"
    assert policy["push_guard_active"] is False
    assert policy["guard_db"] is None


def test_source_falls_back_through_mux_fields_then_idle():
    assert _snapshot(
        active_source="unknown",
        mux_status={"selected_source": "bluetooth"},
    )["source"] == "bluetooth"
    idle = _snapshot(
        active_source=None,
        mux_status={"active_source": 3, "winner": "unknown"},
        diagnostics={"last_source_push_result": [], "last_clear_event": "bad"},
    )
    assert idle["source"] == "idle"
    assert idle["last_source_push_result"] is None
    assert idle["last_clear_event"] is None
    assert idle["last_handoff"] is None
