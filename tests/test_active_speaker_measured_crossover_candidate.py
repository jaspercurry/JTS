# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wave 4 (crossover measurement v2, §5.8): the new measured-crossover
candidate — trims + optional delay/polarity — and its apply extension.

Covers: candidate/alignment validation and refusal reasons, backward-compat
trims-only behavior (alignment absent), the preset-layer delay/polarity
write-through, camilla_yaml emission (delay ms conversion, single Delay
filter, inversion without double-inversion), the delay_graph + graph_safety
proofs, fingerprint sensitivity to every new field, and the
``from_mapping``/tamper round trip.
"""

from __future__ import annotations

import pytest
import yaml as yaml_lib

from jasper.active_speaker.crossover_alignment import POLARITY_INVERT, POLARITY_KEEP
from jasper.active_speaker.measured_crossover_candidate import (
    CANDIDATE_KIND,
    SCHEMA_VERSION,
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    build_and_prove_candidate_config,
    compile_candidate_config,
    driver_corrections,
    effective_preset,
    prove_candidate_config,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _three_way(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_three_way_preset(layout))


def _candidate(
    *,
    preset: ActiveSpeakerPreset | None = None,
    trims: dict[str, float] | None = None,
    alignment: MeasuredCrossoverAlignment | None = None,
    program_id: str = "prog-abc123",
    linearization: dict | None = None,
) -> MeasuredCrossoverCandidate:
    preset = preset or _preset()
    trims = trims if trims is not None else {"woofer": 0.0, "tweeter": -3.5}
    kwargs = {}
    if alignment is not None:
        kwargs["alignment"] = alignment
    if linearization is not None:
        kwargs["linearization"] = linearization
    return MeasuredCrossoverCandidate(
        program_id=program_id,
        analysis={"drift_ppm": 12.5, "sweeps": ["w", "t", "w"]},
        source_preset=preset,
        role_attenuations_db=trims,
        **kwargs,
    )


# --- MeasuredCrossoverAlignment validation ----------------------------------


def test_alignment_defaults_to_absent():
    alignment = MeasuredCrossoverAlignment()
    assert alignment.delay_us is None
    assert alignment.delay_role is None
    assert alignment.polarity is None


def test_alignment_requires_all_three_fields_together():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverAlignment(delay_us=100.0, delay_role="tweeter")
    assert excinfo.value.code == "alignment_partial"


def test_alignment_rejects_delay_below_zero():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverAlignment(delay_us=-1.0, delay_role="tweeter", polarity="keep")
    assert excinfo.value.code == "delay_us_out_of_range"


def test_alignment_rejects_delay_above_dsp_ceiling():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverAlignment(
            delay_us=MAX_DSP_DELAY_US + 1.0, delay_role="tweeter", polarity="keep"
        )
    assert excinfo.value.code == "delay_us_out_of_range"


def test_alignment_accepts_delay_at_dsp_ceiling():
    # The 20 ms ceiling itself is admissible (boundary is inclusive).
    alignment = MeasuredCrossoverAlignment(
        delay_us=MAX_DSP_DELAY_US, delay_role="tweeter", polarity="keep"
    )
    assert alignment.delay_us == MAX_DSP_DELAY_US


def test_alignment_rejects_unknown_polarity_vocabulary():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverAlignment(delay_us=100.0, delay_role="tweeter", polarity="reverse")
    assert excinfo.value.code == "polarity_invalid"


def test_alignment_reuses_crossover_alignment_polarity_vocabulary():
    # Both existing propose_crossover_alignment tokens are accepted verbatim.
    MeasuredCrossoverAlignment(delay_us=1.0, delay_role="tweeter", polarity=POLARITY_KEEP)
    MeasuredCrossoverAlignment(delay_us=1.0, delay_role="tweeter", polarity=POLARITY_INVERT)


# --- MeasuredCrossoverCandidate validation ----------------------------------


def test_candidate_requires_nonempty_program_id():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(program_id="")
    assert excinfo.value.code == "program_id_invalid"


def test_candidate_requires_trims_for_every_role():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(trims={"woofer": 0.0})
    assert excinfo.value.code == "role_attenuations_incomplete"


def test_candidate_rejects_positive_attenuation():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(trims={"woofer": 0.0, "tweeter": 3.0})
    assert excinfo.value.code == "attenuation_out_of_range"


def test_candidate_rejects_attenuation_below_floor():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(trims={"woofer": 0.0, "tweeter": -61.0})
    assert excinfo.value.code == "attenuation_out_of_range"


def test_candidate_rejects_delay_role_outside_preset_roles():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(
            alignment=MeasuredCrossoverAlignment(
                delay_us=100.0, delay_role="midrange", polarity="keep"
            )
        )
    assert excinfo.value.code == "delay_role_unknown"


def test_candidate_rejects_delay_role_ambiguous_across_regions():
    # A 3-way's "mid" role sits between two crossover regions — a single
    # relative-delay candidate cannot name it without ambiguity.
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(
            preset=_three_way(),
            trims={"woofer": 0.0, "mid": 0.0, "tweeter": 0.0},
            alignment=MeasuredCrossoverAlignment(
                delay_us=100.0, delay_role="mid", polarity="keep"
            ),
        )
    assert excinfo.value.code == "delay_role_ambiguous"


def test_candidate_requires_nonempty_analysis():
    with pytest.raises(MeasuredCrossoverCandidateError):
        MeasuredCrossoverCandidate(
            program_id="p1",
            analysis={},
            source_preset=_preset(),
            role_attenuations_db={"woofer": 0.0, "tweeter": 0.0},
        )


# --- fingerprint sensitivity -------------------------------------------------


def test_fingerprint_changes_with_delay_us():
    base = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="keep"
        )
    )
    changed = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=101.0, delay_role="tweeter", polarity="keep"
        )
    )
    assert base.fingerprint != changed.fingerprint


def test_fingerprint_changes_with_polarity():
    base = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="keep"
        )
    )
    changed = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="invert"
        )
    )
    assert base.fingerprint != changed.fingerprint


def test_fingerprint_changes_with_trims():
    base = _candidate(trims={"woofer": 0.0, "tweeter": -3.0})
    changed = _candidate(trims={"woofer": 0.0, "tweeter": -4.0})
    assert base.fingerprint != changed.fingerprint


def test_fingerprint_changes_with_analysis():
    preset = _preset()
    a = MeasuredCrossoverCandidate(
        program_id="p1",
        analysis={"drift_ppm": 1.0},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": 0.0},
    )
    b = MeasuredCrossoverCandidate(
        program_id="p1",
        analysis={"drift_ppm": 2.0},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": 0.0},
    )
    assert a.fingerprint != b.fingerprint


def test_fingerprint_changes_with_program_id():
    base = _candidate(program_id="prog-1")
    changed = _candidate(program_id="prog-2")
    assert base.fingerprint != changed.fingerprint


def test_absent_alignment_fingerprint_differs_from_keep_alignment():
    # "no alignment claim at all" is not the same evidence as "keep, 0 delay" —
    # the fingerprint must distinguish a candidate that says nothing about
    # alignment from one that positively asserts a zero/keep result.
    trims_only = _candidate()
    zero_keep = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=0.0, delay_role="tweeter", polarity="keep"
        )
    )
    assert trims_only.fingerprint != zero_keep.fingerprint


# --- from_mapping round trip + tamper rejection ------------------------------


def test_from_mapping_round_trips():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=250.0, delay_role="tweeter", polarity="invert"
        )
    )
    reopened = MeasuredCrossoverCandidate.from_mapping(candidate.to_dict())
    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.to_dict() == candidate.to_dict()


def test_from_mapping_rejects_tampered_payload():
    candidate = _candidate()
    raw = dict(candidate.to_dict())
    raw["role_attenuations_db"] = {**raw["role_attenuations_db"], "tweeter": -9.0}
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverCandidate.from_mapping(raw)
    assert excinfo.value.code == "candidate_tampered"


def test_from_mapping_rejects_unknown_fields():
    candidate = _candidate()
    raw = {**candidate.to_dict(), "extra_field": 1}
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverCandidate.from_mapping(raw)
    assert excinfo.value.code == "candidate_malformed"


# --- linearization field (#1668 PR-C) ---------------------------------------


def test_linearization_defaults_to_empty_dict():
    candidate = _candidate()
    assert candidate.linearization == {}
    assert candidate.to_dict()["linearization"] == {}


def test_linearization_empty_dict_is_the_old_shape_and_round_trips():
    """Empty dict = no linearization -- the shape every pre-PR-C candidate
    has -- must round-trip through from_mapping exactly like any other
    field, with zero special-casing."""
    candidate = _candidate()
    reopened = MeasuredCrossoverCandidate.from_mapping(candidate.to_dict())
    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.linearization == {}


def test_linearization_populated_round_trips():
    payload = {
        "woofer": {
            "role": "woofer", "filters": [], "fit_band_hz": [150.0, 3951.5],
            "target_level_db": -20.22, "residual_rms_db": 4.16,
            "residual_max_db": 12.21, "reason_summary": {"250": "envelope_fitted"},
            "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 2,
        },
        "tweeter": {
            "role": "tweeter",
            "filters": [{"biquad_type": "Peaking", "freq": 4063.6, "q": 1.89, "gain": -3.38}],
            "fit_band_hz": [2020.0, 13905.2], "target_level_db": -8.63,
            "residual_rms_db": 2.63, "residual_max_db": 7.13,
            "reason_summary": {"2000": "envelope_fitted", "20000": "envelope_limited_by_mic_tier"},
            "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 2,
        },
    }
    candidate = _candidate(linearization=payload)
    assert candidate.linearization == payload
    reopened = MeasuredCrossoverCandidate.from_mapping(candidate.to_dict())
    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.linearization == payload


def test_linearization_participates_in_the_fingerprint():
    preset = _preset()
    base = _candidate(preset=preset, linearization={})
    with_fit = _candidate(preset=preset, linearization={"woofer": {"filters": []}})
    assert base.fingerprint != with_fit.fingerprint


def test_linearization_tampering_trips_the_tamper_check():
    """Verified, not assumed: mutating the persisted linearization payload
    without updating the fingerprint must be caught by from_mapping's
    exact-JSON re-derivation, the SAME mechanism every other field uses."""
    candidate = _candidate(linearization={"woofer": {"filters": [], "target_level_db": -20.0}})
    raw = dict(candidate.to_dict())
    tampered = dict(raw)
    tampered["linearization"] = {
        **raw["linearization"], "woofer": {"filters": [], "target_level_db": -99.0},
    }
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverCandidate.from_mapping(tampered)
    assert excinfo.value.code == "candidate_tampered"


def test_linearization_must_be_a_mapping():
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(linearization=["not", "a", "mapping"])
    assert excinfo.value.code == "linearization_invalid"


def test_linearization_must_be_exact_json_data():
    """Mirrors `analysis`'s own JSON-purity contract (DspPredecessor) —
    a numpy array (or any non-JSON value) anywhere inside linearization
    must be refused at construction time, not silently accepted and only
    fail later when something tries to persist it."""
    import numpy as np

    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        _candidate(linearization={"woofer": {"allowed_depth_db": np.zeros(4)}})
    assert excinfo.value.code == "linearization_invalid"


# --- linearization era tolerance (adversarial review P1 blocker, 2026-07-24) -
#
# Every candidate persisted before #1668 PR-C landed lacks the
# "linearization" key entirely — the field, and even the possibility of a
# non-empty value, did not exist yet. jasper.web.correction_crossover_v2's
# _reopen_candidate_artifact reads candidate.json straight off disk, so a
# candidate published moments before a deploy can be reopened moments after
# it by code that now expects the newer shape. from_mapping must treat
# "linearization" as OPTIONAL on read (absent -> {}), and the fingerprint of
# an empty-linearization candidate must be identical whether or not the key
# was ever present — otherwise every pre-PR-C candidate in flight at a
# deploy trips a false candidate_tampered refusal the moment this PR ships.
# (Non-empty linearization round-tripping AND staying tamper-protected is
# already pinned above by test_linearization_populated_round_trips and
# test_linearization_tampering_trips_the_tamper_check — unaffected by any
# of this, since a non-empty value is never era-tolerant-omitted.)


def test_empty_linearization_is_omitted_from_the_fingerprinted_core():
    """The independent proof, not a self-referential round trip: hand-build
    the EXACT pre-#1668-PR-C ``_core()`` shape (this module's own history
    shows that shape never had a "linearization" key at all — PR-C added
    it) and confirm ``json_fingerprint`` of THAT dict equals the current
    code's fingerprint for an empty-linearization candidate. This never
    calls ``_core()`` or any of this module's own linearization code, so it
    cannot be fooled by a bug in how ``_core()`` itself decides to omit the
    key — it is the byte-for-byte compatibility guarantee, verified from
    first principles."""
    from jasper.audio_measurement.evidence_identity import json_fingerprint

    candidate = _candidate()  # linearization defaults to {}
    pre_prc_core = {
        "schema_version": SCHEMA_VERSION,
        "kind": CANDIDATE_KIND,
        "program_id": candidate.program_id,
        "analysis": candidate.analysis,
        "source_preset": candidate.source_preset.to_dict(),
        "role_attenuations_db": dict(candidate.role_attenuations_db),
        "alignment": candidate.alignment.to_dict(),
        # Deliberately no "linearization" key — every candidate persisted
        # before #1668 PR-C looked exactly like this.
    }
    assert json_fingerprint(pre_prc_core) == candidate.fingerprint


def test_from_mapping_accepts_pre_prc_shape_missing_linearization_key():
    """Era tolerance (the P1 blocker): _reopen_candidate_artifact
    (jasper/web/correction_crossover_v2.py) can hand from_mapping a
    candidate.json published by a build that predates the "linearization"
    field. That payload must load cleanly — not refuse candidate_malformed
    — default to linearization=={}, and its RECOMPUTED fingerprint must
    equal the ORIGINAL persisted one (independently proven identical to the
    pre-PR-C shape by the previous test), or the reviewed-fingerprint check
    in jasper.web.correction_crossover_v2 trips a false candidate_tampered
    / fingerprint-mismatch refusal on every pre-PR-C candidate straddling a
    deploy."""
    candidate = _candidate()
    raw = candidate.to_dict()
    original_fingerprint = raw["fingerprint"]
    assert "linearization" in raw  # to_dict is always the current, full shape
    del raw["linearization"]  # simulate the pre-PR-C persisted shape

    reopened = MeasuredCrossoverCandidate.from_mapping(raw)

    assert reopened.linearization == {}
    assert reopened.fingerprint == original_fingerprint


def test_from_mapping_still_rejects_other_missing_fields():
    """"linearization" is the ONE optional field — every sibling
    (alignment, role_attenuations_db, ...) stays strictly required, exactly
    as before this PR."""
    candidate = _candidate()
    raw = candidate.to_dict()
    del raw["role_attenuations_db"]
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverCandidate.from_mapping(raw)
    assert excinfo.value.code == "candidate_malformed"


def test_to_dict_canonical_shape_always_includes_linearization_key():
    """Canonical to_dict shape (forward-shape consistency, chosen over
    omitting the key when empty): every NEWLY-serialized candidate always
    carries "linearization", populated or not — only an OLD,
    already-persisted pre-PR-C payload is ever missing the key. This is
    what lets from_mapping's tamper check compare raw's fields byte-for-
    byte without a special case for a fresh empty-linearization write:
    to_dict() and a freshly-built raw dict always agree. ``_core()`` (the
    fingerprint input) intentionally does NOT follow this rule — see
    test_empty_linearization_is_omitted_from_the_fingerprinted_core for why
    the two disagree."""
    candidate = _candidate()
    assert candidate.linearization == {}
    raw = candidate.to_dict()
    assert raw["linearization"] == {}
    assert set(raw) == {
        "schema_version", "kind", "program_id", "analysis", "source_preset",
        "role_attenuations_db", "alignment", "linearization", "fingerprint",
    }


def test_from_mapping_rejects_non_mapping_linearization():
    candidate = _candidate()
    raw = {**candidate.to_dict(), "linearization": "not-a-mapping"}
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        MeasuredCrossoverCandidate.from_mapping(raw)
    assert excinfo.value.code == "linearization_malformed"


# --- effective_preset / driver_corrections: backward-compat trims-only ------


def test_absent_alignment_preset_is_unchanged():
    candidate = _candidate()
    assert effective_preset(candidate) is candidate.source_preset


def test_absent_alignment_corrections_are_trims_only():
    candidate = _candidate(trims={"woofer": 0.0, "tweeter": -3.5})
    corrections = driver_corrections(candidate)
    assert corrections == {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
        "tweeter": {"gain_db": -3.5, "delay_ms": 0.0, "inverted": False},
    }


# --- effective_preset / driver_corrections: alignment present ---------------


def test_alignment_writes_delay_into_region_fields():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=340.0, delay_role="tweeter", polarity="keep"
        )
    )
    preset = effective_preset(candidate)
    region = preset.crossover_regions[0]
    assert region.delay_target_driver == "tweeter"
    assert region.delay_ms == pytest.approx(0.34)


def test_alignment_keep_leaves_region_polarity_untouched():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="keep"
        )
    )
    preset = effective_preset(candidate)
    assert preset.crossover_regions[0].upper_polarity == "non-inverted"


def test_alignment_invert_flips_region_upper_polarity():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="invert"
        )
    )
    preset = effective_preset(candidate)
    assert preset.crossover_regions[0].upper_polarity == "inverted"


def test_alignment_invert_twice_returns_to_non_inverted():
    # Region polarity is a persisted claim, not a running toggle: inverting an
    # already-inverted region's *source* preset flips it back.
    raw = _two_way_preset()
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    already_inverted = ActiveSpeakerPreset.from_mapping(raw)
    candidate = _candidate(
        preset=already_inverted,
        alignment=MeasuredCrossoverAlignment(
            delay_us=100.0, delay_role="tweeter", polarity="invert"
        ),
    )
    preset = effective_preset(candidate)
    assert preset.crossover_regions[0].upper_polarity == "non-inverted"


def test_alignment_on_lower_driver_still_flips_upper_polarity():
    # polarity always describes the region's upper (tweeter) driver, whichever
    # driver actually carries the timing delay.
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=50.0, delay_role="woofer", polarity="invert"
        )
    )
    corrections = driver_corrections(candidate)
    assert corrections["tweeter"]["inverted"] is True
    assert corrections["woofer"]["delay_ms"] == pytest.approx(0.05)
    assert corrections["tweeter"]["delay_ms"] == 0.0


def test_driver_corrections_delay_only_on_named_role():
    candidate = _candidate(
        trims={"woofer": -1.0, "tweeter": -2.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=340.0, delay_role="tweeter", polarity="invert"
        ),
    )
    corrections = driver_corrections(candidate)
    assert corrections["tweeter"] == {
        "gain_db": -2.0,
        "delay_ms": pytest.approx(0.34),
        "inverted": True,
    }
    assert corrections["woofer"] == {
        "gain_db": -1.0,
        "delay_ms": 0.0,
        "inverted": False,
    }


# --- camilla_yaml emission ---------------------------------------------------


def test_compile_emits_single_delay_filter_with_ms_conversion():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=340.0, delay_role="tweeter", polarity="keep"
        )
    )
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    parsed = yaml_lib.safe_load(yaml_text)
    assert parsed["filters"]["as_tweeter_delay"] == {
        "type": "Delay",
        "parameters": {"delay": 0.34, "unit": "ms"},
    }
    # The un-delayed driver keeps an explicit zero Delay filter (unchanged
    # emitter shape) — never a second, alternate emission path.
    assert parsed["filters"]["as_woofer_delay"]["parameters"]["delay"] == 0.0
    # Exactly one Delay filter definition per role — no duplicate lane.
    delay_filters = [
        name for name, spec in parsed["filters"].items() if spec.get("type") == "Delay"
    ]
    assert sorted(delay_filters) == ["as_tweeter_delay", "as_woofer_delay"]


def test_compile_emits_inversion_via_gain_not_mixer_no_double_inversion():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=0.0, delay_role="tweeter", polarity="invert"
        )
    )
    preset = effective_preset(candidate)
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    parsed = yaml_lib.safe_load(yaml_text)

    assert parsed["filters"]["as_tweeter_baseline_gain"]["parameters"]["inverted"] is True
    assert parsed["filters"]["as_woofer_baseline_gain"]["parameters"]["inverted"] is False

    # The split mixer stays a no-op inverter (baseline emits
    # apply_region_polarity=False): the tweeter output's mixer source is NOT
    # also inverted, or the two inversions would cancel to a net non-inversion
    # (the double-inversion regression this module's docstring calls out).
    tweeter_index = next(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == "tweeter"
    )
    mixer = parsed["mixers"][f"split_active_{preset.way_count}way"]
    dest = next(entry for entry in mixer["mapping"] if entry["dest"] == tweeter_index)
    assert all(source["inverted"] is False for source in dest["sources"])


def test_compile_emits_trims():
    candidate = _candidate(trims={"woofer": -1.5, "tweeter": -6.0})
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    parsed = yaml_lib.safe_load(yaml_text)
    assert parsed["filters"]["as_woofer_baseline_gain"]["parameters"]["gain"] == -1.5
    assert parsed["filters"]["as_tweeter_baseline_gain"]["parameters"]["gain"] == -6.0


# --- proofs: delay_graph + graph_safety -------------------------------------


def test_prove_candidate_config_passes_for_a_correctly_compiled_graph():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=340.0, delay_role="tweeter", polarity="invert"
        )
    )
    yaml_text = build_and_prove_candidate_config(candidate, playback_device="hw:ActiveDAC")
    assert "as_tweeter_delay" in yaml_text


def test_prove_candidate_config_passes_for_trims_only_candidate():
    candidate = _candidate()
    yaml_text = build_and_prove_candidate_config(candidate, playback_device="hw:ActiveDAC")
    assert "as_tweeter_delay" in yaml_text


def test_prove_candidate_config_rejects_tampered_delay_value():
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=500.0, delay_role="tweeter", polarity="keep"
        )
    )
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    tampered = yaml_text.replace("delay: 0.5", "delay: 0.9")
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        prove_candidate_config(candidate, tampered)
    assert excinfo.value.code == "delay_graph_proof_failed"


def test_high_precision_delay_round_trips_the_proof():
    # Regression (adversarial gate S1): the candidate fold used
    # round(µs/1000, 6) before the emitter's 4-decimal fmt while the proof
    # used a single fmt over the raw µs — two quantizers that disagree on
    # ~0.4% of the valid range. This exact value reproduced the spurious
    # fail-closed refusal; quantized_delay_ms is now the one shared owner.
    candidate = _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=11382.15006948647, delay_role="tweeter", polarity="keep"
        )
    )
    yaml_text = build_and_prove_candidate_config(candidate, playback_device="hw:ActiveDAC")
    assert "as_tweeter_delay" in yaml_text


def test_randomized_delay_sweep_never_trips_the_proof():
    # Deterministic-seed sweep over the full 0–20 ms DSP range: every
    # candidate built through the production compile path must prove clean —
    # zero quantization mismatches between fold and proof.
    import random

    rng = random.Random(20260718)
    for _ in range(200):
        candidate = _candidate(
            alignment=MeasuredCrossoverAlignment(
                delay_us=rng.uniform(0.0, MAX_DSP_DELAY_US),
                delay_role="tweeter",
                polarity="keep",
            )
        )
        build_and_prove_candidate_config(candidate, playback_device="hw:ActiveDAC")


def test_prove_candidate_config_rejects_unprotected_tweeter():
    candidate = _candidate()
    yaml_text = compile_candidate_config(candidate, playback_device="hw:ActiveDAC")
    # Strip the tweeter's protective high-pass filter reference from its
    # pipeline step (simulating an emitter drift the graph_safety proof must
    # catch independently of camilla_yaml's own internal emit gate).
    tampered = yaml_text.replace(
        "as_tweeter_woofer_tweeter_hp, as_tweeter_delay", "as_tweeter_delay"
    )
    assert tampered != yaml_text, "fixture no longer matches the emitter's pipeline shape"
    with pytest.raises(MeasuredCrossoverCandidateError) as excinfo:
        prove_candidate_config(candidate, tampered)
    assert excinfo.value.code == "tweeter_unprotected"
