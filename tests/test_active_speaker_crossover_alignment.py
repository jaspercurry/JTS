"""Driver excitation and applied-profile contracts."""

from __future__ import annotations


from jasper.active_speaker.crossover_contract import (
    preset_matches_applied_profile,
    verified_driver_excitation,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset

from tests.test_active_speaker_profile import _two_way_preset

def _two_way() -> ActiveSpeakerPreset:
    # Mono 2-way: woofer=lower, tweeter=upper, crossover at 1600 Hz.
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _applied_profile() -> dict:
    preset = _two_way()
    corrections = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in ("woofer", "tweeter")
    }
    return {
        "status": "applied",
        "baseline_id": "baseline-test",
        "candidate_fingerprint": "protected-profile",
        "source": {"fingerprint": "protected-profile"},
        "recomposition_snapshot": {
            "topology_id": "test-topology",
            "preset": preset.to_dict(),
            "corrections": corrections,
        },
    }


def _driver_excitation(*, locked: bool) -> dict:
    value = {
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
        assert normalized["baseline_id"] == source["baseline_id"]
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
        {**locked, "baseline_id": False},
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


def test_applied_profile_context_rejects_same_fc_setting_change() -> None:
    preset = _two_way()
    corrections = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in ("woofer", "tweeter")
    }
    applied = {
        "recomposition_snapshot": {
            "preset": preset.to_dict(),
            "corrections": corrections,
        }
    }
    changed = preset.to_dict()
    changed["crossover_regions"][0]["order"] = 2

    assert preset_matches_applied_profile(preset, applied) is True
    assert preset_matches_applied_profile(
        preset, applied, candidate_corrections=corrections
    ) is True
    changed_corrections = {role: dict(values) for role, values in corrections.items()}
    changed_corrections["tweeter"]["gain_db"] = -1.0
    assert preset_matches_applied_profile(
        preset, applied, candidate_corrections=changed_corrections
    ) is False
    assert preset_matches_applied_profile(
        ActiveSpeakerPreset.from_mapping(changed), applied
    ) is False
    assert preset_matches_applied_profile(preset, {}) is False
