# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked measured per-driver BASE TRIM — solve, record, and re-key.

These pin the half of the auto-trim step that has no hardware in it: the chain
from ledger-normalized overlap levels to a per-role attenuation, the record's
own envelope, and the refusal an operator sees when the declaration has moved
under a banked trim. The verb that produces the levels lives in
``test_cli_driver_trim.py``; the profile's preference for this record over the
guided captures lives in ``test_active_speaker_level_match.py``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jasper.active_speaker import driver_base_trim as dbt
from jasper.active_speaker.level_trim import MAX_ATTENUATION_DB

TWO_WAY = ("woofer", "tweeter")
TWO_WAY_REGIONS = [("woofer", "tweeter", 2000.0)]
THREE_WAY = ("woofer", "mid", "tweeter")
THREE_WAY_REGIONS = [("woofer", "mid", 300.0), ("mid", "tweeter", 3000.0)]

MIC = {"sens_factor_db": -12.07, "analog_gain_db": 18.0, "serial": "8108494"}


# ---------- solve: deltas -> attenuation ------------------------------------


def test_two_way_solve_attenuates_the_hotter_driver_and_normalizes_up():
    """The tweeter measures 20 dB hotter at the handoff, so it gets -20 dB and
    the woofer — the quieter driver, hence the reference — sits at unity. The
    vector's MAXIMUM is 0 dB: that is the normalize-up."""
    levels = {"mono": {"woofer": {2000.0: -50.0}, "tweeter": {2000.0: -30.0}}}
    trims = dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS)
    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert max(trims.values()) == 0.0


def test_a_uniformly_quiet_pair_still_normalizes_up_to_unity():
    """Both drivers 40 dB down but EQUAL: the trim is a relative answer, so
    common-mode level is given back rather than charged as attenuation
    (#2906's invariant, at the one writer this slice adds)."""
    levels = {"mono": {"woofer": {2000.0: -70.0}, "tweeter": {2000.0: -70.0}}}
    assert dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS) == {
        "woofer": 0.0,
        "tweeter": 0.0,
    }


def test_three_way_chains_both_handoffs_and_leaves_the_quietest_at_unity():
    """Each handoff is +6 dB hotter than the one below, so the chain compounds:
    the tweeter needs 12 dB and the reference — the QUIETEST driver, not the
    loudest — sits at 0 dB, which is what "normalizes up" means here."""
    levels = {
        "mono": {
            "woofer": {300.0: -50.0},
            "mid": {300.0: -44.0, 3000.0: -44.0},
            "tweeter": {3000.0: -38.0},
        }
    }
    trims = dbt.solve_base_trims(levels, THREE_WAY, THREE_WAY_REGIONS)
    assert trims == {"woofer": 0.0, "mid": -6.0, "tweeter": -12.0}
    assert max(trims.values()) == 0.0


def test_two_groups_average_and_a_group_missing_one_role_is_dropped():
    levels = {
        "left": {"woofer": {2000.0: -50.0}, "tweeter": {2000.0: -30.0}},
        "right": {"woofer": {2000.0: -50.0}, "tweeter": {2000.0: -40.0}},
    }
    assert dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS) == {
        "woofer": 0.0,
        "tweeter": -15.0,
    }
    levels["right"].pop("tweeter")
    assert dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS) == {
        "woofer": 0.0,
        "tweeter": -20.0,
    }


@pytest.mark.parametrize(
    "levels, why",
    [
        pytest.param({}, "no groups at all", id="empty"),
        pytest.param(
            {"mono": {"woofer": {2000.0: -50.0}}},
            "the upper driver has no level at the handoff",
            id="missing_role",
        ),
        pytest.param(
            {"mono": {"woofer": {2000.0: -50.0}, "tweeter": {1000.0: -30.0}}},
            "the tweeter's only level is at a frequency nobody declared",
            id="wrong_fc",
        ),
        pytest.param(
            {
                "mono": {
                    "woofer": {2000.0: -50.0},
                    "tweeter": {2000.0: float("nan")},
                }
            },
            "a non-finite level is not evidence",
            id="nan_level",
        ),
    ],
)
def test_solve_fails_closed_to_empty(levels, why):
    """Fail-closed in one direction only: an incomplete group is DROPPED, and
    no qualifying group returns ``{}`` so the caller keeps its estimate."""
    assert dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS) == {}, why


def test_the_attenuation_floor_clamps_an_absurd_delta():
    levels = {"mono": {"woofer": {2000.0: -200.0}, "tweeter": {2000.0: -30.0}}}
    trims = dbt.solve_base_trims(levels, TWO_WAY, TWO_WAY_REGIONS)
    assert trims == {"woofer": 0.0, "tweeter": MAX_ATTENUATION_DB}


# ---------- the record: round-trip, envelope, re-key -------------------------


def _bank(tmp_path: Path, **overrides) -> Path:
    state = tmp_path / "base_trim.json"
    payload = dict(
        trims_db={"woofer": 0.0, "tweeter": -20.0},
        levels_db={"mono": {"woofer": {2000.0: -50.0}, "tweeter": {2000.0: -30.0}}},
        capture_geometries={"mono": {"woofer": "near_field", "tweeter": "near_field"}},
        roles=TWO_WAY,
        regions=TWO_WAY_REGIONS,
        declaration_fingerprint="a" * 64,
        microphone=MIC,
        state_path=state,
    )
    payload.update(overrides)
    dbt.write_base_trim(**payload)
    return state


def test_write_then_load_round_trips_every_field(tmp_path: Path):
    state = _bank(tmp_path)
    record = dbt.load_base_trim(state_path=state)
    assert record is not None
    assert record["kind"] == dbt.BASE_TRIM_KIND
    assert record["artifact_schema_version"] == dbt.SCHEMA_VERSION
    assert record["trims_db"] == {"woofer": 0.0, "tweeter": -20.0}
    assert record["roles"] == ["woofer", "tweeter"]
    assert record["declaration_fingerprint"] == "a" * 64
    assert record["microphone"] == MIC
    assert record["crossovers"] == [
        {"lower_role": "woofer", "upper_role": "tweeter", "declared_fc_hz": 2000.0}
    ]
    assert record["levels_db"] == {
        "mono": {"woofer": {"2000": -50.0}, "tweeter": {"2000": -30.0}}
    }
    # The claim the single mic position can actually support, named so its
    # absence is a decision rather than an oversight.
    assert record["geometry_claim"] == "on_axis_single_position"
    assert record["measured_at"].endswith("Z")


@pytest.mark.parametrize(
    "write, why",
    [
        pytest.param(lambda p: None, "absent file", id="absent"),
        pytest.param(lambda p: p.write_text("{"), "unparseable", id="malformed"),
        pytest.param(
            lambda p: p.write_text(json.dumps({"kind": "something_else"})),
            "a document that is not ours",
            id="wrong_kind",
        ),
        pytest.param(
            lambda p: p.write_text(
                json.dumps({"kind": dbt.BASE_TRIM_KIND, "artifact_schema_version": 99})
            ),
            "a schema this build does not read",
            id="wrong_schema",
        ),
    ],
)
def test_load_is_absent_tolerant_and_never_raises(tmp_path: Path, write, why):
    state = tmp_path / "base_trim.json"
    write(state)
    assert dbt.load_base_trim(state_path=state) is None, why


@pytest.mark.parametrize(
    "trims",
    [
        pytest.param({"woofer": 0.0, "tweeter": 3.0}, id="a_boost"),
        pytest.param({"woofer": 0.0, "tweeter": -80.0}, id="below_the_floor"),
        pytest.param({"woofer": 0.0, "tweeter": float("inf")}, id="not_finite"),
        pytest.param({"woofer": 0.0}, id="a_role_missing"),
        pytest.param({"woofer": 0.0, "tweeter": -20.0, "mid": -3.0}, id="a_role_extra"),
    ],
)
def test_write_refuses_a_record_its_own_reader_would_reject(tmp_path: Path, trims):
    with pytest.raises(dbt.DriverBaseTrimError):
        _bank(tmp_path, trims_db=trims)


def test_write_refuses_a_trim_that_names_no_declaration(tmp_path: Path):
    with pytest.raises(dbt.DriverBaseTrimError):
        _bank(tmp_path, declaration_fingerprint="")


def test_banked_trims_are_returned_when_the_declaration_matches(tmp_path: Path):
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims == {"woofer": 0.0, "tweeter": -20.0}
    assert meta["status"] == dbt.STATUS_APPLIED
    assert meta["declaration_fingerprint"] == "a" * 64


@pytest.mark.parametrize(
    "fingerprint, status",
    [
        pytest.param("b" * 64, dbt.STATUS_DECLARATION_CHANGED, id="a_different_one"),
        pytest.param(None, dbt.STATUS_DECLARATION_CHANGED, id="none_at_all"),
        pytest.param("", dbt.STATUS_DECLARATION_CHANGED, id="empty"),
    ],
)
def test_a_stale_declaration_refuses_loudly_with_one_remediation(
    tmp_path: Path, fingerprint, status
):
    """Refused, and it SAYS so. A banked trim silently dropped is
    indistinguishable from a speaker that was never measured."""
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims(fingerprint, TWO_WAY, state_path=state)
    assert trims == {}
    assert meta["status"] == status
    assert meta["remediation"] == dbt.REMEASURE_REMEDIATION


def test_a_changed_role_set_refuses_rather_than_partly_applying(tmp_path: Path):
    state = _bank(tmp_path)
    trims, meta = dbt.banked_base_trims("a" * 64, THREE_WAY, state_path=state)
    assert trims == {}
    assert meta["status"] == dbt.STATUS_ROLES_CHANGED
    assert meta["remediation"] == dbt.REMEASURE_REMEDIATION


def test_absent_is_normal_and_says_so(tmp_path: Path):
    trims, meta = dbt.banked_base_trims(
        "a" * 64, TWO_WAY, state_path=tmp_path / "nothing.json"
    )
    assert (trims, meta["status"]) == ({}, dbt.STATUS_ABSENT)


@pytest.mark.parametrize(
    "trim, why",
    [
        pytest.param(4.0, "a hand-edited BOOST would make the speaker louder"),
        pytest.param(-999.0, "below the floor the solver clamps to"),
        pytest.param("loud", "not a number at all"),
        pytest.param(True, "a bool is not a level"),
    ],
)
def test_a_tampered_record_can_only_fail_closed(tmp_path: Path, trim, why):
    state = _bank(tmp_path)
    record = json.loads(state.read_text())
    record["trims_db"]["tweeter"] = trim
    state.write_text(json.dumps(record))
    trims, meta = dbt.banked_base_trims("a" * 64, TWO_WAY, state_path=state)
    assert trims == {}, why
    assert meta["status"] == dbt.STATUS_UNUSABLE


def test_the_state_path_honours_the_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "elsewhere.json"))
    assert dbt.base_trim_state_path() == tmp_path / "elsewhere.json"
    assert dbt.base_trim_state_path("/explicit.json") == Path("/explicit.json")


# ---------- one vocabulary: no third estimator may grow here -----------------

_BAND_MATH_NAMES = {
    "mean", "average", "median", "log10", "log2", "sqrt", "trapz",
    "polyfit", "interp", "convolve", "fft", "rfft", "irfft", "welch",
}
_ESTIMATOR_OWNERS = {
    "attenuation_from_group_deltas",
    "analyze_driver_capture",
    "usable_overlap_level_db",
    "verified_driver_excitation",
    "driver_crossover_fcs",
    "driver_passband_hz",
    "solve_base_trims",
}


@pytest.mark.parametrize(
    "relative",
    ["jasper/active_speaker/driver_base_trim.py", "jasper/cli/driver_trim.py"],
)
def test_the_auto_trim_files_mint_no_band_averaging_of_their_own(relative):
    """The one-vocabulary rule, made mechanical.

    The repo already carries three subordinate estimates of one physical
    quantity (the overlap-band point read, the branch power-band average, and
    the core-band median) and two live disclosures comparing them. A fourth
    would be the same defect again, so these two files may CALL the estimator
    and the chain solver by name and may not do band arithmetic themselves.
    """
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")
    )
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name in _BAND_MATH_NAMES:
            offenders.append(f"{relative}:{node.lineno} calls {name}()")
    assert offenders == []
    called = {
        (
            node.func.id if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute)
            else ""
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    # A positive control: the guard is only meaningful while these files do in
    # fact reach the shipped estimator/solver rather than having quietly
    # stopped calling anyone.
    assert called & _ESTIMATOR_OWNERS
