# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.volume_diagnostics import build_volume_policy_snapshot


def test_volume_policy_uses_live_guard_when_persistence_claims_clear():
    policy = build_volume_policy_snapshot(
        active_source="spotify",
        listening_level=90,
        main_volume_db=-13.13,
        persisted_main_volume_db=0.0,
        mux_status={"active_source": "spotify"},
        diagnostics={},
    )

    assert policy["source"] == "spotify"
    assert policy["carrier"] == "camilla_guard"
    assert policy["push_guard_active"] is True
    assert policy["guard_db"] == -13.13
    assert policy["guard_reason"] == "derived_from_live_camilla_guard"
