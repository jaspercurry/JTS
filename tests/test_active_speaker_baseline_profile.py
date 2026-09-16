# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
import yaml as yaml_lib

from jasper.active_speaker import driver_base_trim as dbt
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.candidate_parts import compose_candidate
from jasper.active_speaker.crossover_v2.planning import applied_profile_timing
import jasper.active_speaker.baseline_profile as baseline_profile_mod
from jasper.active_speaker import (
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.baseline_profile import (
    MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB,
    PROVENANCE_MANUAL,
    PROVENANCE_MEASURED,
    PROVENANCE_RECOMMENDED_START,
    _GAIN_SOURCE_TO_PROVENANCE,
    active_layer_a_fingerprint,
    baseline_candidate_fingerprint,
)
from jasper.active_speaker.crossover_preview import (
    build_crossover_preview,
)
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.active_speaker.measurement import (
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import (
    declared_profile_fixture, declared_graph_fixture, standard_design_draft,
    mono_output_topology,
    valid_camilla_config as _valid_config,  # noqa: F401 - shared fixture export
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_measured_crossover_candidate import _room_correction
from tests._log_events import event_field_maps


_MEASURE_EVIDENCE = {
    "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4},
    "alignment_confidence": 0.82,
}


@pytest.fixture(autouse=True)
def _trial_store(tmp_path, monkeypatch):
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: tmp_path / "sessions")


def _topology(
    *,
    tweeter_output: int = 1,
    tweeter_verified: bool = True,
    device_id: str = "hifiberry_dac8x",
    device_label: str = "HiFiBerry DAC8x",
    physical_output_count: int = 8,
    card_id: str = "DAC8",
) -> OutputTopology:
    return mono_output_topology(
        tweeter_output=tweeter_output,
        tweeter_verified=tweeter_verified,
        topology_name="Bench mono",
        device_id=device_id,
        device_label=device_label,
        physical_output_count=physical_output_count,
        card_id=card_id,
    )


def _dual_apple_topology() -> OutputTopology:
    return _topology(
        device_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        device_label="Dual Apple USB-C DACs",
        physical_output_count=4,
        card_id="",
    )


def _safe_session(
    *,
    role: str,
    output_index: int,
    playback_id: str,
) -> dict:
    target = {
        "speaker_group_id": "mono",
        "role": role,
        "driver_role": role,
        "output_index": output_index,
    }
    return {
        "status": "armed",
        "quiet_start": {
            "status": "floor_confirmed",
            "floor_audio_confirmed": True,
            "current_target": target,
            "last_operator_result": {
                "accepted": True,
                "outcome": "heard_correct_driver",
                "playback_id": playback_id,
                "target": target,
            },
        },
    }


def _research(*, tweeter_gain_db: float = -18.5, with_subwoofer: bool = False) -> dict:
    drivers = [
        {
            "role": "woofer",
            "model": "Epique E150HE-44",
            "recommended_lowpass_hz": 2500,
            "usable_frequency_range_hz": [45, 5000],
            "sources": ["https://example.test/woofer"],
        },
        {
            "role": "tweeter",
            "model": "F110M-8",
            # 2000, not 2500, and the gap is load-bearing. Since #2603 this one
            # field IS the declared protection floor, and the apply-time gate
            # (camilla_yaml._assert_tweeter_crossover_honours_declared_floor)
            # refuses any crossover below it. Declaring 2500 while ALSO crossing
            # at 2500 left this fixture speaker with zero downward headroom, so
            # every alternative-Fc test below (the 2500 -> 2250 nudge the #2292
            # undo pins use) was exercising a crossover its own driver
            # declaration forbids. 2000 keeps the fixture honest -- still far
            # above this driver's own do_not_test_below_hz of 1200 -- and lets
            # those tests go on testing apply/undo mechanics rather than
            # tripping a safety gate. Raise it here to test the gate itself;
            # tests/test_active_speaker_protection_floor.py owns those pins.
            "recommended_highpass_hz": 2000,
            "do_not_test_below_hz": 1200,
            "gain_offset_db": tweeter_gain_db,
            "sources": ["https://example.test/tweeter"],
        },
    ]
    if with_subwoofer:
        drivers.append({
            "role": "subwoofer",
            "model": "Sub driver",
            "recommended_lowpass_hz": 80,
            "usable_frequency_range_hz": [20, 200],
            "sources": ["https://example.test/sub"],
        })
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            }
        ],
    }


def _draft(
    topology: OutputTopology,
    *,
    tweeter_gain_db: float = -18.5,
    with_subwoofer: bool = False,
) -> dict:
    return build_design_draft(
        topology,
        driver_research=_research(
            tweeter_gain_db=tweeter_gain_db, with_subwoofer=with_subwoofer
        ),
        created_at="2026-06-14T12:00:00Z",
    )


def _measurements(topology: OutputTopology, tmp_path: Path) -> dict:
    state_path = tmp_path / "measurements.json"
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42.0,
                "test_level_dbfs": -68.0,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
            now=f"2026-06-14T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-audible",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed-playback-audible.wav",
                    "metadata_basename": "tone_summed-playback-audible.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-14T12:02:30Z",
    )
    return record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "polarity": "normal",
            "delay_ms": 0.0,
            "summed_test_id": "summed-playback-audible",
        },
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )


def test_baseline_source_binds_exact_normalized_preview_candidate(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    changed_preview = deepcopy(preview)
    changed_preview["groups"][0]["crossovers"][0]["candidate"][
        "confidence"
    ] = "high"
    measurements = _measurements(topology, tmp_path)

    first = baseline_profile_mod._source_payload(
        topology, draft, preview, measurements
    )
    changed = baseline_profile_mod._source_payload(
        topology, draft, changed_preview, measurements
    )

    assert (
        first["crossover_preview_fingerprint"]
        != changed["crossover_preview_fingerprint"]
    )
    assert first["fingerprint"] != changed["fingerprint"]


# --- Fail-safe level trim derived from the driver sensitivity gap -------------
#
def _applied_layer_a_yaml(tmp_path: Path) -> str:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    applied = declared_profile_fixture(
        topology,
        design_draft=draft,
        measurements=_measurements(topology, tmp_path),
        write=False,
        config_path=tmp_path / "active_speaker_baseline.yml",
    )
    applied["status"] = "applied"
    from tests.active_speaker_fixtures import compile_applied_fixture

    text, issues = compile_applied_fixture(
        topology,
        applied_profile=applied,
    )
    assert issues == []
    assert text is not None
    return text


@pytest.mark.parametrize("mutation", ["playback", "mixer", "pipeline_suffix"])
def test_layer_a_fingerprint_rejects_every_bound_domain_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    changed = yaml_lib.safe_load(baseline_yaml)
    split_index = next(
        index
        for index, step in enumerate(changed["pipeline"])
        if step.get("type") == "Mixer"
    )
    split_name = changed["pipeline"][split_index]["name"]
    if mutation == "playback":
        changed["devices"]["playback"]["device"] = "unexpected_output"
    elif mutation == "mixer":
        source = changed["mixers"][split_name]["mapping"][0]["sources"][0]
        source["gain"] = float(source.get("gain", 0.0)) - 0.25
    else:
        driver_step = changed["pipeline"][split_index + 1]
        driver_step["channels"] = [int(driver_step["channels"][0]) + 1]

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(changed)) != (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_layer_a_fingerprint_ignores_capture_only_mutation(tmp_path: Path) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    changed = yaml_lib.safe_load(baseline_yaml)
    changed["devices"]["capture"] = {
        "type": "Alsa",
        "channels": 2,
        "device": "alternate_program_capture",
        "format": "S32_LE",
    }

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(changed)) == (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_layer_a_fingerprint_ignores_camilla_readback_null_defaults(
    tmp_path: Path,
) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    readback = yaml_lib.safe_load(baseline_yaml)
    readback["devices"].update({
        "adjust_period": None,
        "multithreaded": None,
        "volume_ramp_time": None,
    })
    split_index = next(
        index
        for index, step in enumerate(readback["pipeline"])
        if step.get("type") == "Mixer"
    )
    split_name = readback["pipeline"][split_index]["name"]
    for step in readback["pipeline"][split_index:]:
        step.update({"bypassed": None, "description": None})
    for route in readback["mixers"][split_name]["mapping"]:
        route["mute"] = None
        for source in route["sources"]:
            source.update({"mute": None, "scale": None})
    for step in readback["pipeline"][split_index:]:
        for name in step.get("names", []):
            readback["filters"][name]["description"] = None
            readback["filters"][name]["parameters"]["scale"] = None

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(readback)) == (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_measured_vs_datasheet_tolerance_clears_its_own_error_budget() -> None:
    """The tolerance's derivation, pinned rather than left in prose: it must
    clear the sum of what CAN honestly differ between the two frames
    (~2 dB datasheet spec + ~2 dB realized pad impedance + ~1.3 dB measured
    frame spread + ~0.5 dB estimator systematic) and stay well under the ~12 dB
    defect it exists to catch."""
    honest_worst_case_db = 2.0 + 2.0 + 1.3 + 0.5
    assert MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB > honest_worst_case_db
    assert MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB < 12.0


# --- Persisted working-crossover values (Slice 0): polarity/delay ------------
#
# Precedence: [automatic tuning_owner + fresh authorized measured alignment
# evidence] > [persisted working-crossover values from the preview/preset] >
# [preserved_applied_profile carryover] > [schema defaults]. Manual tuning
# never consults measured alignment evidence for these two sub-parameters —
# mirrors the shipped gain rule that a manual pin is never silently replaced.


def test_gain_source_to_provenance_migration_mapping_pinned():
    # SC-3's migration table, verbatim: explicit/operator_pinned -> manual,
    # measured -> measured, sensitivity/estimate -> recommended_start,
    # none -> no entry (an untouched role makes no provenance claim).
    assert _GAIN_SOURCE_TO_PROVENANCE["measured"] == PROVENANCE_MEASURED
    assert _GAIN_SOURCE_TO_PROVENANCE["operator_pinned"] == PROVENANCE_MANUAL
    assert _GAIN_SOURCE_TO_PROVENANCE["explicit"] == PROVENANCE_MANUAL
    assert _GAIN_SOURCE_TO_PROVENANCE["sensitivity"] == PROVENANCE_RECOMMENDED_START
    assert _GAIN_SOURCE_TO_PROVENANCE["estimate"] == PROVENANCE_RECOMMENDED_START
    assert "none" not in _GAIN_SOURCE_TO_PROVENANCE


def test_baseline_config_emits_single_net_inversion_not_double():
    raw = _two_way_preset()
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(raw)

    yaml_text = emit_active_speaker_baseline_config(
        preset,
        playback_device="hw:ActiveDAC",
        corrections={
            "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": True},
        },
    )
    parsed = yaml_lib.safe_load(yaml_text)

    # The per-driver baseline gain filter is the sole inverter.
    assert parsed["filters"]["as_tweeter_baseline_gain"]["parameters"]["inverted"] is True
    assert parsed["filters"]["as_woofer_baseline_gain"]["parameters"]["inverted"] is False
    # The split mixer's source for the tweeter output does NOT also invert.
    tweeter_index = next(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == "tweeter"
    )
    mixer = parsed["mixers"][f"split_active_{preset.way_count}way"]
    dest = next(entry for entry in mixer["mapping"] if entry["dest"] == tweeter_index)
    assert all(source["inverted"] is False for source in dest["sources"])


# --- Spec-promise guard 1: trim-only apply preserves manual polarity/delay --


# --- corrections_provenance block on the candidate/applied payload ---------


# --- lifecycle events (lane E, docs/active-crossover-information-design.md
# "Structured events") -------------------------------------------------------

_BASELINE_LOGGER = "jasper.active_speaker.baseline_profile"




def _v2_candidate(
    preset: ActiveSpeakerPreset,
    *,
    delay_us: float = 250.0,
    delay_role: str = "tweeter",
    polarity: str = "invert",
    tweeter_gain_db: float = -2.0,
    **extra: Any,
) -> MeasuredCrossoverCandidate:
    return MeasuredCrossoverCandidate(
        program_id="prog-v2-1",
        analysis={"drift_ppm": 3.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": tweeter_gain_db},
        alignment=MeasuredCrossoverAlignment(
            delay_us=delay_us, delay_role=delay_role, polarity=polarity
        ),
        **extra,
    )


_ROOM_CORRECTION: dict[str, Any] = {
    "sides": {
        "mono": [
            {"freq": 48.0, "q": 3.0, "gain": -4.0},
            {"freq": 120.0, "q": 2.0, "gain": -2.5},
            {"freq": 62.0, "q": 4.0, "gain": 3.0},
        ]
    },
    "ceiling_hz": 350.0,
    "ceiling_source": "applied_candidate",
    "basis": {
        "round_id": "round-7",
        "room_median_sha256": "b" * 64,
        "admitted_boosts_hz": [62.0],
    },
    "boost_db_total": 3.0,
    "level_cost_db": 3.0,
}


_TRIM_DECISION = {
    "strategy": "resolved_committed",
    "committed_side": "resolved",
    "anchor_drift_db": 1.25,
}


def test_frozen_applied_profile_carries_linearization_top_level():
    """Gap 3c: _frozen_applied_profile (a field-by-field allowlist, unlike
    persist_applied_baseline_profile's whole-object spread) must copy
    "linearization" -- otherwise a candidate saved OVER an applied,
    linearized profile silently loses it from the retained
    applied_recomposition_profile sidecar."""
    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {"woofer": {"gain_db": 0.0}},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        "recomposition_snapshot": {
            "schema_version": 1,
            "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        },
    }
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization"] == {"woofer": [{"biquad_type": "Peaking"}]}
    assert frozen["recomposition_snapshot"]["linearization"] == {
        "woofer": [{"biquad_type": "Peaking"}]
    }


def test_frozen_applied_profile_defaults_linearization_when_absent():
    """Era-tolerant: a pre-PR-D applied dict with no "linearization" key at
    all must not raise, defaulting to {}."""
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "recomposition_snapshot": {"schema_version": 1},
    }
    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization"] == {}


def test_frozen_applied_profile_carries_linearization_outcome_top_level():
    """Gauge fix (2026-07-24): mirrors
    test_frozen_applied_profile_carries_linearization_top_level for the new
    field — this is a field-by-field allowlist, so a candidate saved OVER
    an applied, linearized profile must not silently lose
    linearization_outcome from the retained applied_recomposition_profile
    sidecar (the same Gap 3c bug class "linearization" itself was fixed
    for)."""
    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {"woofer": {"gain_db": 0.0}},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        "linearization_outcome": "fitted",
        "recomposition_snapshot": {
            "schema_version": 1,
            "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        },
    }
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization_outcome"] == "fitted"


def test_frozen_applied_profile_defaults_linearization_outcome_when_absent():
    """Era-tolerant: a pre-gauge-fix applied dict with no
    "linearization_outcome" key at all must not raise, defaulting to ""."""
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "recomposition_snapshot": {"schema_version": 1},
    }
    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization_outcome"] == ""




# ---------- the measured base trim replaces the datasheet prefill ------------


def _bank_events(caplog) -> list[dict[str, str]]:
    """The base-trim seam's events as FIELDS, never as prose."""
    return event_field_maps(caplog, "dsp.baseline_base_trim_banked")




def _applied_with_sources(tmp_path: Path, sources: dict[str, str]) -> dict[str, Any]:
    topology = _dual_apple_topology()
    candidate = declared_profile_fixture(
        topology, design_draft=_draft(topology), measurements={},
        config_path=tmp_path / "baseline.yml", write=True,
    )
    candidate["corrections_source"] = dict(sources)
    candidate["level_match"] = {"comparison": "strict_measured_candidate", "groups_total": 1, "groups_measured": 1,
                                "applied": True, "newest_capture_at": "2026-09-13T00:00:00Z"}
    candidate["automatic_candidate"] = {"measured_group_ids": ["mono"]}
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)
    return candidate


@pytest.mark.parametrize(
    "sources, banked, result, reason",
    [
        (
            {"woofer": "measured", "tweeter": "measured"},
            True,
            "ok",
            None,
        ),
        (
            {"woofer": "measured", "tweeter": "operator_pinned"},
            True,
            "left_standing",
            "partly_measured",
        ),
        (
            {"woofer": "sensitivity", "tweeter": "sensitivity"},
            False,
            "cleared",
            "unmeasured",
        ),
    ],
    ids=["all-measured", "one-operator-pin", "none-measured"],
)
def test_a_partly_pinned_profile_neither_banks_nor_clears(
    tmp_path: Path, caplog, sources, banked, result, reason
) -> None:
    """Three answers, not two.

    ``_bank_applied_base_trim`` required EVERY role to be sourced ``measured``
    while ``crossover_contract._snapshot_owner`` — the predicate the seam's own
    docstring claims to mirror — needs only ANY. A candidate with one
    operator-pinned driver therefore read as ``automatic`` to the contract and
    as unmeasured to the bank, and the apply DESTROYED a good banked record on
    the strength of a single pin. A pin does not un-measure a speaker: the
    prior full measurement is still the best evidence anyone has, so the
    middle arm leaves it alone and says so.

    The third arm is unchanged and deliberately so: a role that fell back to
    the DATASHEET is weaker evidence, not a pin, and still clears.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    candidate = _applied_with_sources(tmp_path, sources)
    # A record from an earlier, fully measured apply is standing before each
    # arm runs -- the arms differ only in what they do to it.
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(
            tmp_path / "prior", {"woofer": "measured", "tweeter": "measured"}
        ),
        apply_state={"result": "success"},
        state_path=tmp_path / "prior_applied.json",
    )
    assert dbt.load_base_trim() is not None
    caplog.clear()

    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    assert (dbt.load_base_trim() is not None) is banked
    events = _bank_events(caplog)
    assert [event["result"] for event in events] == [result]
    if reason is not None:
        assert events[0]["reason"] == reason


@pytest.mark.parametrize(
    "named_chain",
    [
        pytest.param("c" * 64, id="a_candidate_resolved_it"),
        pytest.param(None, id="the_guided_captures_did"),
    ],
)
def test_the_banked_trim_names_the_chain_it_was_co_fitted_with(
    tmp_path: Path, named_chain
) -> None:
    """#3479: the seam banks the FRAME beside the number.

    A trim is degenerate with the chain it was resolved against, so the apply
    passes the resolving candidate's own fingerprint — already on the profile's
    source block — through to the record, and it reaches the level-match ledger
    every downstream reader looks at. A profile the guided captures levelled
    names no candidate and banks no frame, which is a different fact from
    naming the bare one.
    """
    candidate = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )
    source = dict(candidate["source"])
    if named_chain is None:
        source.pop("measured_candidate_fingerprint", None)
    else:
        source["measured_candidate_fingerprint"] = named_chain
    candidate["source"] = source
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)

    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    record = dbt.load_base_trim()
    assert record is not None
    assert record["chain_fingerprint"] == named_chain
    _trims, meta = dbt.banked_base_trims(
        record["declaration_fingerprint"], record["roles"]
    )
    assert meta["chain_fingerprint"] == named_chain


def test_a_measured_profile_that_cannot_be_banked_drops_the_stale_record(
    tmp_path: Path, caplog
) -> None:
    """Absent beats wrong.

    A refused write left the PREVIOUS apply's record standing, so the box went
    on levelling a ``--level-matched`` walk by numbers describing a graph it
    had stopped playing. The resolver's fallback (guided captures, then the
    datasheet) is conservative; a stale record is not.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(tmp_path, {"woofer": "measured", "tweeter": "measured"}),
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    assert dbt.load_base_trim() is not None
    caplog.clear()

    doomed = _applied_with_sources(
        tmp_path / "next", {"woofer": "measured", "tweeter": "measured"}
    )
    # The declaration the record would be keyed by is unreadable, so the
    # writer refuses -- the seam must not leave the prior record behind.
    doomed["source"] = {**doomed["source"], "crossover_preview_fingerprint": ""}
    doomed["candidate_fingerprint"] = baseline_candidate_fingerprint(doomed)
    baseline_profile_mod.persist_applied_baseline_profile(
        doomed,
        apply_state={"result": "success"},
        state_path=tmp_path / "next_applied.json",
    )

    assert dbt.load_base_trim() is None
    results = [event["result"] for event in _bank_events(caplog)]
    assert results == ["failed", "cleared"]
    assert _bank_events(caplog)[0]["reason"] == dbt.REFUSE_NO_DECLARATION
    assert _bank_events(caplog)[1]["reason"] == dbt.BANK_WRITE_REFUSED


def test_a_malformed_correction_entry_refuses_instead_of_escaping(
    tmp_path: Path, caplog
) -> None:
    """The seam promises never to fail a successful apply, and broke it.

    ``float((entry or {}).get("gain_db"))`` raises AttributeError on a
    correction entry that is not a Mapping, and AttributeError was not in the
    seam's except tuple -- so a malformed entry propagated out of
    ``persist_applied_baseline_profile`` and turned an apply whose graph was
    already live and read back into a failure.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    candidate = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )
    candidate["corrections"] = {**candidate["corrections"], "tweeter": "-12.0"}
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)

    payload = baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    assert payload["status"] == "applied"
    events = _bank_events(caplog)
    assert [event["result"] for event in events] == ["left_standing"]
    assert events[0]["reason"] == dbt.BANK_CORRECTION_ENTRY_UNREADABLE


def test_the_two_unreadable_guards_no_longer_share_one_slug(
    tmp_path: Path, caplog
) -> None:
    """``profile_unreadable`` meant two unrelated things -- a profile naming no
    corrections at all, and a measured profile whose readiness block was not
    kept -- so an operator reading the reason could not tell which had
    happened, and the two arms now behave differently besides."""
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    base = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )

    no_corrections = deepcopy(base)
    no_corrections["corrections"] = "not-a-mapping"
    no_corrections["candidate_fingerprint"] = baseline_candidate_fingerprint(
        no_corrections
    )
    baseline_profile_mod.persist_applied_baseline_profile(
        no_corrections,
        apply_state={"result": "success"},
        state_path=tmp_path / "a.json",
    )
    no_readiness = deepcopy(base)
    no_readiness.pop("automatic_candidate", None)
    no_readiness["candidate_fingerprint"] = baseline_candidate_fingerprint(no_readiness)
    baseline_profile_mod.persist_applied_baseline_profile(
        no_readiness,
        apply_state={"result": "success"},
        state_path=tmp_path / "b.json",
    )

    reasons = [event["reason"] for event in _bank_events(caplog)]
    assert reasons == [
        dbt.BANK_CORRECTIONS_UNREADABLE,
        dbt.BANK_READINESS_UNREADABLE,
    ]


def test_a_follower_domain_graph_never_touches_the_solo_base_trim(
    tmp_path: Path,
) -> None:
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(tmp_path, {"woofer": "measured", "tweeter": "measured"}),
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    banked = deepcopy(dbt.load_base_trim())
    assert banked is not None

    follower = _applied_with_sources(
        tmp_path / "follower", {"woofer": "sensitivity", "tweeter": "sensitivity"}
    )
    follower["recomposition_snapshot"] = {
        **follower["recomposition_snapshot"], "domain": "driver",
    }
    follower["candidate_fingerprint"] = baseline_candidate_fingerprint(follower)
    baseline_profile_mod.persist_applied_baseline_profile(
        follower,
        apply_state={"result": "success"},
        state_path=tmp_path / "follower_applied.json",
    )

    assert dbt.load_base_trim() == banked


@pytest.mark.parametrize("source", ["measured", "composed", "document", "saved", "cleared", "base"])
@pytest.mark.parametrize("delay", [-37.5, 22.0])
def test_timing_record_round_trip_apply_to_priors(tmp_path, monkeypatch, source, delay):
    load_applied = baseline_profile_mod.load_applied_baseline_profile_state
    topology = _topology()
    draft = standard_design_draft(topology)
    declaration, base = declared_graph_fixture(topology, draft)
    fields = {"margin_db": .6, "residual_rms_db": .2, "repeat_spread_db": .1, "repeat_spread_us": 2, "repeat_count": 3}
    identity = {"round_id": "r1", "take_id": "t2", "graph_fingerprint": "graph", "at": "2026-09-15T12:00:00Z"}
    incumbent = {"delay_us": delay, "polarity": "inverted", "provenance": "measured",
                 "measured": {**fields, **identity, "at": "2026-09-14T12:00:00Z"}}
    candidate = replace(base, alignment=MeasuredCrossoverAlignment(abs(delay), "tweeter" if delay > 0 else "woofer", "invert"),
        analysis={"measurement_status": "unmeasured", "resolution": {"alignment": "measured" if source == "composed" else source}, "evidence": {"commissioning": {"alignment": {
            "timing_verdict": "measured", "committed": {"delay_us": delay, "polarity": "inverted"}, **fields, **identity}}}})
    if source == "composed":
        candidate = compose_candidate(publish_authored_candidate(candidate), sections={"room": _room_correction()},
                                      evidence={"packet_fingerprint": "room-round"})
    monkeypatch.setattr(baseline_profile_mod, "_bank_applied_base_trim", lambda *a: None)
    monkeypatch.setattr(baseline_profile_mod, "release_staged_startup_hold", lambda: None)
    prepared = baseline_profile_mod.prepare_applied_baseline_profile(candidate, declaration=declaration,
        design_draft=draft, measurements={}, applied_at=identity["at"], provenance={} if source == "composed" else {"timing": incumbent})
    path = tmp_path / "applied.json"
    baseline_profile_mod.persist_applied_baseline_profile(prepared, apply_state={"result": "success"}, state_path=path)
    applied = load_applied(path)
    assert all(not ({"delay_ms", "inverted"} & set(values)) for values in applied["corrections_provenance"].values())
    if source in ("cleared", "base"):
        assert "timing" not in applied
        assert applied_profile_timing(applied) is None
        return
    expected = {"delay_us": delay, "polarity": "inverted",
                "provenance": "measured" if source in ("measured", "composed") else "authored_by_model"}
    if source in ("measured", "composed"):
        expected["measured"] = {**fields, **identity}
    elif source == "saved":
        expected = incumbent
    assert applied["timing"] == expected
    assert {key: value for key, value in asdict(applied_profile_timing(applied)).items() if value is not None} == expected
    corrections = applied["corrections"]
    assert 1000 * (corrections["tweeter"]["delay_ms"] - corrections["woofer"]["delay_ms"]) == pytest.approx(delay)
    later = baseline_profile_mod.prepare_applied_baseline_profile(replace(candidate, analysis={"measurement_status": "unmeasured"}), declaration=declaration,
        design_draft=draft, measurements={}, provenance=applied)
    assert later["timing"] == expected
    assert later["corrections"] == corrections
    applied.pop("timing")
    assert applied_profile_timing(applied) is None


@pytest.mark.parametrize("record", [
    {"delay_us": 22, "polarity": "normal", "provenance": "measured", "extra": 1},
    {"polarity": "normal", "provenance": "measured"},
])
def test_timing_reader_returns_none_for_invalid_record(record):
    assert applied_profile_timing({"timing": record}) is None
