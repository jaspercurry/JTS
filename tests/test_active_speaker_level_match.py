# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured per-driver level-match trim (L1 phone level matching).

These pin the heart of the measured refinement of the datasheet sensitivity
trim: the overlap-band level delta -> per-driver attenuation chain, and the
fail-closed fallback to the datasheet trim when the measurement is not usable.

The trim math is tested directly against ``_measured_level_trims`` with
synthetic overlap evidence (a duck-typed preset carries only the way count +
crossover regions the function reads). The end-to-end override / provisional
behaviour through ``build_baseline_profile_candidate`` with real synthesized
phone captures lives in ``test_active_speaker_baseline_profile.py``.

The second half of this file pins which of the TWO evidence sources wins: a
banked measured base trim (written by the apply seam) is preferred over guided
captures that are not newer than it (newer captures supersede — ruling S20,
pinned in ``test_active_speaker_baseline_profile.py``), and a trim measured
against a different declaration is refused loudly rather than applied to a
speaker it does not describe.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from jasper.active_speaker import driver_base_trim as dbt
from jasper.active_speaker.baseline_profile import (
    _measured_level_trims,
    _overlap_level_at,
)
from jasper.active_speaker.crossover_preview import crossover_preview_fingerprint


def _preset(way_count: int, regions):
    """A minimal stand-in carrying only what _measured_level_trims reads."""
    return SimpleNamespace(
        way_count=way_count,
        crossover_regions=tuple(
            SimpleNamespace(lower_driver=lo, upper_driver=up, fc_hz=fc)
            for lo, up, fc in regions
        ),
    )


def _overlap(fc: float, level_db: float, *, usable: bool = True) -> dict:
    return {
        "fc_hz": fc,
        "level_db": level_db,
        "usable": usable,
        "lo_hz": fc / 1.414,
        "hi_hz": fc * 1.414,
        "bins": 128,
    }


def _measurements(*driver_specs) -> dict:
    """Build a measurements dict from (group, role, verdict, [overlap, ...])."""
    from jasper.active_speaker.capture_geometry import comparison_set_fingerprint

    comparison_set = {
        "schema_version": 2,
        "comparison_set_id": "1" * 32,
        "created_at": "2026-07-11T12:00:00Z",
        "topology_id": "test-topology",
        "profile_context_id": "protected-profile",
        "setup_sha256": "3" * 64,
        "device_sha256": "4" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            f"{group}:{role}": {
                "target_id": f"{group}:{role}",
                "speaker_group_id": group,
                "role": role,
                "tone_frequency_hz": 1000.0,
                "tone_peak_dbfs": -12.0,
                "commissioning_gain_db": -40.0,
                "locked_main_volume_db": -12.0,
            }
            for group, role, _verdict, _overlaps in driver_specs
        },
    }
    comparison_set["fingerprint"] = comparison_set_fingerprint(comparison_set)
    latest: dict = {}
    for group, role, verdict, overlaps in driver_specs:
        target_fingerprint = hashlib.sha256(
            f"{group}:{role}".encode("utf-8")
        ).hexdigest()
        latest[f"{group}:{role}"] = {
            "speaker_group_id": group,
            "role": role,
            "target_fingerprint": target_fingerprint,
            "excitation": {
                "schema_version": 1,
                "scope": "sweep_plus_role_varying_commission_gain",
                "sweep_peak_dbfs": -12.0,
                "commissioning_gain_db": -40.0,
                "effective_peak_dbfs": -52.0,
            },
            "acoustic": {"verdict": verdict, "overlap_levels": list(overlaps)},
            "placement_proof": {
                "schema_version": 1,
                "policy_id": "driver_same_distance_v1",
                "accepted": True,
                "confirmation_source": "capture_begin",
                "acknowledgement_binding_sha256": "5" * 64,
                "capture_session_id": f"relay-{group}-{role}",
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
                "speaker_group_id": group,
                "role": role,
                "target_fingerprint": target_fingerprint,
                "comparison_set_id": comparison_set["comparison_set_id"],
                "comparison_set_fingerprint": comparison_set["fingerprint"],
            },
        }
    return {
        "latest_by_target": latest,
        "active_comparison_set": comparison_set,
    }


TWO_WAY = [("woofer", "tweeter", 2000.0)]
THREE_WAY = [("woofer", "mid", 300.0), ("mid", "tweeter", 3000.0)]


# ---------- overlap-band delta -> trim math ---------------------------------


def test_two_way_attenuates_the_hotter_driver():
    # Tweeter measures 20 dB hotter than the woofer at the 2 kHz handoff.
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert meta["groups_measured"] == 1
    assert meta["groups_total"] == 1
    assert meta["deltas"][0]["delta_db"] == 20.0  # + => upper hotter


def test_reference_is_the_quietest_driver():
    # Woofer hotter than the tweeter -> the WOOFER is attenuated, tweeter is ref.
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -30.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -50.0)]),
    )
    trims, _meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {"woofer": -20.0, "tweeter": 0.0}


def test_matched_levels_yield_no_trim():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -42.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -42.0)]),
    )
    trims, _meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {"woofer": 0.0, "tweeter": 0.0}


def test_three_way_chain_propagates_through_both_crossovers():
    # mid 5 dB hotter than woofer @300; tweeter 24 dB hotter than mid @3000.
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(300.0, -50.0)]),
        (
            "mono",
            "mid",
            "present",
            [_overlap(300.0, -45.0), _overlap(3000.0, -44.0)],
        ),
        ("mono", "tweeter", "present", [_overlap(3000.0, -20.0)]),
    )
    trims, _meta = _measured_level_trims(_preset(3, THREE_WAY), measurements)
    # raw: woofer 0; mid = 0 + (-50) - (-45) = -5; tweeter = -5 + (-44) - (-20) = -29
    assert trims == {"woofer": 0.0, "mid": -5.0, "tweeter": -29.0}


def test_stereo_groups_are_averaged():
    measurements = _measurements(
        ("left", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("left", "tweeter", "present", [_overlap(2000.0, -30.0)]),   # left: -20
        ("right", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("right", "tweeter", "present", [_overlap(2000.0, -28.0)]),  # right: -22
    )
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert meta["groups_measured"] == 2
    assert trims == {"woofer": 0.0, "tweeter": -21.0}  # mean(-20, -22)


def test_attenuation_is_clamped_to_floor():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -10.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -120.0)]),  # woofer ~110 hotter
    )
    trims, _meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims["woofer"] == -60.0  # clamped, never below the -60 dB floor
    assert trims["tweeter"] == 0.0


# ---------- fail-closed fallback --------------------------------------------


def test_fail_closed_when_a_driver_is_missing():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        # tweeter not recorded at all
    )
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {}
    assert meta["groups_measured"] == 0


def test_fail_closed_when_an_overlap_is_unusable():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0, usable=False)]),
    )
    trims, _meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {}


def test_fail_closed_when_a_driver_is_not_present():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "out_of_band", [_overlap(2000.0, -30.0)]),
    )
    trims, _meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert trims == {}


def test_fail_closed_when_overlap_fc_does_not_match_crossover():
    # The recorded overlap is for a different Fc than the preset's crossover.
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )
    trims, _meta = _measured_level_trims(_preset(2, [("woofer", "tweeter", 1200.0)]), measurements)
    assert trims == {}


def test_partial_stereo_keeps_the_usable_group():
    # Left fully usable; right tweeter unusable -> only left contributes.
    measurements = _measurements(
        ("left", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("left", "tweeter", "present", [_overlap(2000.0, -30.0)]),
        ("right", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("right", "tweeter", "present", [_overlap(2000.0, -30.0, usable=False)]),
    )
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)
    assert meta["groups_total"] == 2
    assert meta["groups_measured"] == 1
    assert trims == {"woofer": 0.0, "tweeter": -20.0}


def test_no_measurements_returns_empty():
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), {})
    assert trims == {}
    assert meta["groups_measured"] == 0


def test_normalizes_driver_excitation_that_differs_by_40_db():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -50.0)]),
    )
    # The old flow could play the woofer at -20 dB and tweeter at -60 dB,
    # then compare their raw captures as if they shared a reference.
    measurements["latest_by_target"]["mono:woofer"]["excitation"].update({
        "commissioning_gain_db": -20.0,
        "effective_peak_dbfs": -32.0,
    })
    measurements["latest_by_target"]["mono:tweeter"]["excitation"].update({
        "commissioning_gain_db": -60.0,
        "effective_peak_dbfs": -72.0,
    })

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)

    # Equal captured level at 40 dB less digital drive means the tweeter is
    # 40 dB more sensitive. The normalized trim attenuates it accordingly.
    assert trims == {"woofer": 0.0, "tweeter": -40.0}
    assert meta["groups_measured"] == 1
    assert meta["comparison"] == "placement_attested_gain_ledger_normalized"
    assert meta["incomparable_groups"] == []


def test_fail_closed_when_excitation_ledger_is_missing():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )
    measurements["latest_by_target"]["mono:tweeter"].pop("excitation")

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)

    assert trims == {}
    assert meta["incomparable_groups"][0]["reason"] == (
        "excitation_ledger_missing_or_invalid"
    )


def test_legacy_geometryless_records_cannot_drive_automatic_trim():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )
    measurements["latest_by_target"]["mono:woofer"].pop("placement_proof")

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)

    assert trims == {}
    assert meta["incomparable_groups"] == [{
        "speaker_group_id": "mono",
        "reason": "placement_or_comparison_set_missing_or_invalid",
        "roles": ["woofer"],
    }]


def test_driver_records_from_different_comparison_sets_cannot_be_mixed():
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )
    measurements["latest_by_target"]["mono:tweeter"]["placement_proof"][
        "comparison_set_id"
    ] = "9" * 32

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)

    assert trims == {}
    assert meta["incomparable_groups"][0]["roles"] == ["tweeter"]


# ---------- _overlap_level_at gate ------------------------------------------


def test_overlap_level_at_requires_present_and_usable():
    present = {"acoustic": {"verdict": "present", "overlap_levels": [_overlap(2000.0, -42.0)]}}
    assert _overlap_level_at(present, 2000.0) == -42.0
    # within the matching tolerance
    assert _overlap_level_at(present, 2000.5) == -42.0

    silent = {"acoustic": {"verdict": "silent", "overlap_levels": [_overlap(2000.0, -42.0)]}}
    assert _overlap_level_at(silent, 2000.0) is None

    unusable = {"acoustic": {"verdict": "present", "overlap_levels": [_overlap(2000.0, -42.0, usable=False)]}}
    assert _overlap_level_at(unusable, 2000.0) is None

    assert _overlap_level_at(None, 2000.0) is None
    assert _overlap_level_at({}, 2000.0) is None


# ---------- which evidence source wins --------------------------------------

# Any mapping fingerprints; what matters is that the WRITER and the READER
# derive the key from the same declaration document.
PREVIEW = {
    "kind": "jts_active_speaker_crossover_preview",
    "status": "ready_for_protected_staging",
    "drivers": {"woofer": {"sensitivity_db_2v83_1m": 88.0},
                "tweeter": {"sensitivity_db_2v83_1m": 106.0}},
}


def _bank_base_trim(tmp_path, monkeypatch, *, trims, declaration):
    state = tmp_path / "driver_base_trim.json"
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(state))
    dbt.write_base_trim(
        trims_db=trims,
        roles=tuple(trims),
        speaker_group_ids=["mono"],
        declaration_fingerprint=declaration,
        trim_source="strict_measured_candidate",
        state_path=state,
    )
    return state


def test_a_banked_base_trim_is_preferred_over_the_guided_captures(
    tmp_path, monkeypatch
):
    """Both sources are present and disagree; the deliberate measurement wins,
    and the ledger says which one produced the number."""
    _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),  # would be -20
    )

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements, PREVIEW)

    assert trims == {"woofer": 0.0, "tweeter": -6.0}
    assert meta["source"] == "banked_base_trim"
    assert meta["base_trim"]["status"] == dbt.STATUS_APPLIED
    # The guided walk did not run, and the ledger does not pretend it did.
    assert meta["deltas"] == []
    # But the groups the banked record DID level are reported under the keys
    # readiness gates on -- see the readiness test below.
    assert meta["measured_group_ids"] == ["mono"]
    assert meta["groups_measured"] == 1


def test_a_banked_trim_for_another_declaration_is_refused_and_the_guided_path_runs(
    tmp_path, monkeypatch
):
    _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration="f" * 64,
    )
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements, PREVIEW)

    assert trims == {"woofer": 0.0, "tweeter": -20.0}  # the guided answer
    assert meta["source"] == "guided_captures"
    assert meta["base_trim"]["status"] == dbt.STATUS_DECLARATION_CHANGED
    assert meta["base_trim"]["remediation"] == dbt.REMEASURE_REMEDIATION


def test_without_a_declaration_only_the_guided_captures_are_considered(
    tmp_path, monkeypatch
):
    """The estimator cross-check calls this reader with no preview. A banked
    trim cannot be keyed there, so it is not silently applied."""
    _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )
    measurements = _measurements(
        ("mono", "woofer", "present", [_overlap(2000.0, -50.0)]),
        ("mono", "tweeter", "present", [_overlap(2000.0, -30.0)]),
    )

    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), measurements)

    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert meta["source"] == "guided_captures"


def test_no_banked_trim_leaves_the_guided_path_exactly_as_it_was():
    trims, meta = _measured_level_trims(_preset(2, TWO_WAY), {}, PREVIEW)
    assert trims == {}
    assert meta["source"] == "guided_captures"
    assert meta["base_trim"]["status"] == dbt.STATUS_ABSENT


@pytest.mark.parametrize(
    "tamper, status",
    [
        pytest.param(
            lambda r: r.__setitem__("declaration_fingerprint", "f" * 64),
            dbt.STATUS_DECLARATION_CHANGED,
            id="declaration_moved",
        ),
        pytest.param(
            lambda r: r["trims_db"].__setitem__("tweeter", 4.0),
            dbt.STATUS_UNUSABLE,
            id="hand_edited_to_a_boost",
        ),
        pytest.param(
            lambda r: r["trims_db"].__setitem__("supertweeter", -3.0),
            dbt.STATUS_ROLES_CHANGED,
            id="roles_do_not_match",
        ),
    ],
)
def test_every_refused_status_is_reported_on_the_ledger(
    tmp_path, monkeypatch, tamper, status
):
    """All three refusals reach the ledger. A record dropped without a word
    reads exactly like a speaker nobody ever measured."""
    state = _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )
    record = json.loads(state.read_text())
    tamper(record)
    state.write_text(json.dumps(record))

    _trims, meta = _measured_level_trims(_preset(2, TWO_WAY), {}, PREVIEW)

    assert meta["base_trim"]["status"] == status
    assert meta["base_trim"]["status"] in dbt.REFUSED_STATUSES
    assert meta["base_trim"]["remediation"] == dbt.REMEASURE_REMEDIATION


def test_a_banked_trim_reports_the_groups_it_levelled_to_readiness(
    tmp_path, monkeypatch
):
    """The field is a gate, not a decoration.

    ``automatic_candidate_readiness`` compares the ledger's measured-group SET
    against the topology's required one, so a banked trim reporting no groups
    would leave a fully-levelled speaker reading
    ``automatic_crossover_measurements_incomplete``.
    """
    from jasper.active_speaker.crossover_contract import (
        automatic_candidate_readiness,
    )

    state = _bank_base_trim(
        tmp_path, monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},
        declaration=crossover_preview_fingerprint(PREVIEW),
    )
    record = json.loads(state.read_text())
    record["speaker_group_ids"] = ["left", "right"]
    state.write_text(json.dumps(record))

    _trims, meta = _measured_level_trims(_preset(2, TWO_WAY), {}, PREVIEW)

    assert meta["measured_group_ids"] == ["left", "right"]
    assert meta["groups_measured"] == 2
    readiness = automatic_candidate_readiness(
        required_group_ids=["left", "right"],
        level_match=meta,
        measurement_summary={},
    )
    assert readiness["measured_group_ids"] == ["left", "right"]
    # A pair whose trim levelled only ONE cabinet is still incomplete.
    assert automatic_candidate_readiness(
        required_group_ids=["left", "right", "centre"],
        level_match=meta,
        measurement_summary={},
    )["measured_group_ids"] == ["left", "right"]
