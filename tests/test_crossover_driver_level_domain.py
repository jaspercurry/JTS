# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations



def test_effective_excitation_includes_driver_main_lock():
    from jasper.active_speaker.baseline_profile import _effective_excitation_dbfs

    locked = {
        "schema_version": 1,
        "scope": "sweep_plus_role_gain_and_driver_level_lock",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -6.0,
        "locked_main_volume_db": -4.0,
        "effective_peak_dbfs": -22.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": "bench_mono",
        "role": "woofer",
    }
    assert _effective_excitation_dbfs({"excitation": locked}) == -22.0

    varying = {
        **locked,
        "scope": "sweep_plus_role_varying_commission_gain",
        "effective_peak_dbfs": -18.0,
    }
    varying.pop("locked_main_volume_db")
    assert _effective_excitation_dbfs({"excitation": varying}) == -18.0

    assert _effective_excitation_dbfs({
        "excitation": {**locked, "sweep_peak_dbfs": "-12"}
    }) is None
