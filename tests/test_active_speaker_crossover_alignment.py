"""Driver excitation contracts."""

from __future__ import annotations


from jasper.active_speaker.crossover_contract import verified_driver_excitation


def _driver_excitation(*, locked: bool) -> dict:
    value = {
        "schema_version": 1,
        "scope": "sweep_plus_role_gain_and_driver_level_lock",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -6.0,
        "locked_main_volume_db": -4.0,
        "effective_peak_dbfs": -22.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "topology_id": "bench_mono",
        "role": "woofer",
    }
    if not locked:
        value.update({
            "scope": "sweep_plus_role_varying_commission_gain",
            "effective_peak_dbfs": -18.0,
        })
        value.pop("locked_main_volume_db")
    return value


def test_driver_excitation_verifier_normalizes_both_scopes_idempotently() -> None:
    for locked in (False, True):
        source = _driver_excitation(locked=locked)
        normalized = verified_driver_excitation(source)
        assert normalized is not None
        # The verifier re-derives the ledger's own keys and invents none.
        assert set(normalized) == set(source)
        assert ("locked_main_volume_db" in normalized) is locked
        assert normalized["gain_source"] == source["gain_source"]
        assert normalized["topology_id"] == source["topology_id"]
        assert normalized["role"] == source["role"]
        assert verified_driver_excitation(normalized) == normalized

    tolerance_edge = {
        **_driver_excitation(locked=False),
        "effective_peak_dbfs": -17.96,
    }
    normalized_edge = verified_driver_excitation(tolerance_edge)
    assert normalized_edge is not None
    assert normalized_edge["effective_peak_dbfs"] == -18.0
    assert set(normalized_edge) == set(tolerance_edge)
    assert verified_driver_excitation(normalized_edge) == normalized_edge

    for unity in (
        {
            **_driver_excitation(locked=False),
            "commissioning_gain_db": 0.0,
            "effective_peak_dbfs": -12.0,
        },
        {
            **_driver_excitation(locked=True),
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": 0.0,
            "effective_peak_dbfs": -12.0,
        },
    ):
        assert verified_driver_excitation(unity) is not None


def test_driver_excitation_verifier_fails_closed_without_throwing() -> None:
    locked = _driver_excitation(locked=True)
    varying = _driver_excitation(locked=False)
    invalid = (
        {**locked, "schema_version": True},
        {**locked, "scope": ["sweep_plus_role_gain_and_driver_level_lock"]},
        {**locked, "scope": {"unexpected": "mapping"}},
        {**locked, "scope": "unknown"},
        {**locked, "sweep_peak_dbfs": "-12"},
        {**locked, "commissioning_gain_db": False},
        {**locked, "effective_peak_dbfs": float("nan")},
        {**locked, "effective_peak_dbfs": -18.0},
        {key: value for key, value in locked.items() if key != "locked_main_volume_db"},
        {**varying, "locked_main_volume_db": 0.0},
        {**locked, "gain_source": ""},
        {**locked, "topology_id": None},
        {**locked, "role": "   "},
        {**varying, "commissioning_gain_db": 0.1, "effective_peak_dbfs": -11.9},
        {**locked, "locked_main_volume_db": 0.1, "effective_peak_dbfs": -17.9},
        {
            **varying,
            "sweep_peak_dbfs": 3.0,
            "commissioning_gain_db": -6.0,
            "effective_peak_dbfs": -3.0,
        },
        {
            **varying,
            "sweep_peak_dbfs": 0.0,
            "commissioning_gain_db": 0.0,
            "effective_peak_dbfs": 0.01,
        },
    )
    for value in invalid:
        assert verified_driver_excitation(value) is None
