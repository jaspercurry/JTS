# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Driver repeat aggregation and placement proof."""

from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.commissioning_capture import (
    DEFAULT_REPEAT_TARGET,
    REPEAT_OUTLIER_DB,
    aggregate_driver_repeats,
    record_driver_repeat_aggregate,
)
from tests._log_events import event_fields

def _repeat(
    level_dbfs: float,
    *,
    verdict: str = "present",
    clipping: bool = False,
    artifact_path: str | None = None,
    snr_verdict: str | None = None,
    above_validity_floor: bool | None = None,
) -> dict:
    acoustic: dict = {"observed_mic_dbfs": level_dbfs, "mic_clipping": clipping}
    if snr_verdict is not None:
        acoustic["snr"] = {"verdict": snr_verdict}
    if above_validity_floor is not None:
        acoustic["gating"] = {"above_validity_floor": above_validity_floor}
    return {"verdict": verdict, "acoustic": acoustic, "artifact_path": artifact_path}


def _reference_axis_proof(
    *, comparison_hex: str = "a", capture_session_id: str = "capture-reference"
) -> dict:
    return {
        "schema_version": 1,
        "accepted": True,
        "confirmation_source": "capture_begin",
        "acknowledgement_binding_sha256": "d" * 64,
        "capture_session_id": capture_session_id,
        "capture_protocol_version": 3,
        "capture_page_build": "20260712.1",
        "policy_id": "driver_reference_axis_v1",
        "comparison_set_id": comparison_hex * 32,
        "comparison_set_fingerprint": comparison_hex * 64,
        "target_fingerprint": "c" * 64,
        "speaker_group_id": "mono",
        "role": "woofer",
    }


def test_placement_proof_allowlist_contains_the_current_capture_protocol():
    """The allowlist duplicates its members as literals because
    crossover_v2.sweep_spec imports capture_geometry (not the other way round),
    so the module cannot import the constant back without inverting that
    dependency. Pin CONTAINMENT here — not equality: the allowlist is
    deliberately WIDER than the emitted protocol, because it reads persisted
    proofs (see the next test)."""
    from jasper.active_speaker.capture_geometry import (
        PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS,
    )
    from jasper.active_speaker.crossover_v2.sweep_spec import CAPTURE_PROTOCOL_VERSION

    assert (
        CAPTURE_PROTOCOL_VERSION in PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS
    )


def test_placement_proof_still_accepts_a_page_protocol_2_persisted_proof():
    """Regression: dropping 2 from the allowlist when the Pi stopped EMITTING
    protocol 2 would retroactively invalidate real persisted evidence.

    `normalized_placement_proof` stamps the PAGE's `capture_protocol_version`,
    and the published capture page build 20260712.3 advertised protocol 2. Any
    proof captured against it carries 2 forever. Invalidating those breaks
    repeat admission, crossover readiness, and replay for already-commissioned
    speakers."""
    from jasper.active_speaker.capture_geometry import placement_proof_shape_valid

    # A proof exactly as the published 20260712.3 page caused it to be written.
    proof = {
        **_reference_axis_proof(),
        "capture_protocol_version": 2,
        "capture_page_build": "20260712.3",
    }
    assert placement_proof_shape_valid(
        proof,
        policy_id="driver_reference_axis_v1",
        role="woofer",
        speaker_group_id="mono",
        target_fingerprint="c" * 64,
    )


@pytest.mark.parametrize(
    ("protocol", "valid"), [(1, False), (2, True), (3, True), (4, False)]
)
def test_placement_proof_capture_protocol_is_an_explicit_allowlist(
    protocol, valid
):
    """PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS is a deliberate
    allowlist, not a ``>= 2`` floor: protocol 1 has no acknowledgement
    machinery for a proof to stand on, 2 and 3 authenticate the same
    acknowledgement through the same choreography, and a future protocol 4
    must be a deliberate addition here, never a silent pass-through (see the
    constant's comment in jasper.active_speaker.capture_geometry)."""
    from jasper.active_speaker.capture_geometry import (
        placement_proof_shape_valid,
    )

    proof = {**_reference_axis_proof(), "capture_protocol_version": protocol}
    assert placement_proof_shape_valid(
        proof,
        policy_id="driver_reference_axis_v1",
        role="woofer",
        speaker_group_id="mono",
        target_fingerprint="c" * 64,
    ) is valid


def test_aggregate_three_accepted_repeats_is_normal_confidence():
    repeats = [_repeat(-30.0), _repeat(-30.3), _repeat(-29.8)]

    result = aggregate_driver_repeats(repeats)

    assert result["target"] == DEFAULT_REPEAT_TARGET == 3
    assert result["accepted"] == 3
    assert result["rejected"] == 0
    assert result["aggregate"] == "median_magnitude"
    assert result["confidence"] == "normal"
    assert result["spread_db_p90"] is not None
    assert result["spread_db_p90"] <= 2.0
    assert result["needed_recapture"] is False
    assert result["recaptured"] is False
    assert len(result["per_repeat"]) == 3
    assert all(entry["accepted"] for entry in result["per_repeat"])
    assert all(entry["reject_reason"] is None for entry in result["per_repeat"])


def test_aggregate_one_outlier_needs_recapture_once():
    # Repeat 2 deviates from the running median (built from repeats 0-1,
    # ~ -30.1) by 9.9 dB, well past REPEAT_OUTLIER_DB (3.0).
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 2
    assert result["rejected"] == 1
    assert result["needed_recapture"] is True
    assert result["recaptured"] is False
    outlier = result["per_repeat"][2]
    assert outlier["accepted"] is False
    assert outlier["reject_reason"] == "level_outlier"


def test_aggregate_bounded_recapture_completes_at_three():
    first_pass = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]
    assert aggregate_driver_repeats(first_pass)["needed_recapture"] is True

    # The caller took the ONE bounded extra attempt and appends it.
    recaptured = [*first_pass, _repeat(-30.1)]

    result = aggregate_driver_repeats(recaptured)

    assert result["accepted"] == 3
    assert result["rejected"] == 1
    assert result["recaptured"] is True
    assert result["needed_recapture"] is False
    assert result["confidence"] == "normal"


def test_aggregate_refusing_recapture_proceeds_with_two_reduced_confidence():
    # Same 3-attempt, 1-rejected, 2-accepted list as the "needs recapture"
    # case — the caller simply stops here instead of trying a 4th time.
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 2
    assert result["confidence"] == "reduced"


def test_aggregate_rejects_clipping_and_unusable_capture():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, clipping=True),
        _repeat(-29.9, verdict="unusable_capture"),
    ]

    result = aggregate_driver_repeats(repeats)

    reasons = [entry["reject_reason"] for entry in result["per_repeat"]]
    assert reasons == [None, "clipping", "unusable_capture"]
    assert result["accepted"] == 1


def test_aggregate_rejects_on_snr_insufficient_when_lane_b_block_present():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, snr_verdict="insufficient"),
        _repeat(-29.9),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["per_repeat"][1]["reject_reason"] == "snr_insufficient"
    assert result["accepted"] == 2


def test_aggregate_accepts_exact_warn_boundary_reduced_snr():
    repeats = [
        _repeat(-30.0, snr_verdict="reduced"),
        _repeat(-30.1, snr_verdict="reduced"),
        _repeat(-29.9, snr_verdict="reduced"),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 3
    assert all(entry["reject_reason"] is None for entry in result["per_repeat"])


def test_aggregate_rejects_below_validity_floor_when_lane_a_block_present():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, above_validity_floor=False),
        _repeat(-29.9),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["per_repeat"][1]["reject_reason"] == "below_validity_floor"
    assert result["accepted"] == 2


def test_aggregate_rejects_reference_axis_when_validity_floor_is_unknown():
    repeat = _repeat(-30.0)
    repeat["acoustic"].update({
        "capture_geometry": "reference_axis",
        "gating": {
            "applied": False,
            "f_valid_floor_hz": None,
            "exempt_reason": None,
        },
        "overlap_levels": [{
            "fc_hz": 200.0,
            "above_validity_floor": None,
            "usable": False,
        }],
    })
    repeat["placement_proof"] = _reference_axis_proof()

    result = aggregate_driver_repeats([repeat])

    assert result["accepted"] == 0
    assert result["per_repeat"][0]["reject_reason"] == "validity_floor_unknown"
    assert result["per_repeat"][0]["above_validity_floor"] is None


@pytest.mark.parametrize("floor_hz", (None, True, 0.0, -1.0, float("nan")))
def test_aggregate_rejects_reference_axis_without_finite_positive_floor(
    floor_hz: float | None,
) -> None:
    repeat = _repeat(-30.0)
    repeat["acoustic"].update({
        "capture_geometry": "reference_axis",
        "gating": {"applied": True, "f_valid_floor_hz": floor_hz},
        "overlap_levels": [{
            "fc_hz": 1000.0,
            "above_validity_floor": True,
            "usable": True,
        }],
    })
    repeat["placement_proof"] = _reference_axis_proof()

    result = aggregate_driver_repeats([repeat])

    assert result["accepted"] == 0
    assert result["per_repeat"][0]["reject_reason"] == "validity_floor_unknown"


def test_aggregate_reference_axis_requires_bound_server_placement_proof():
    repeat = _repeat(-30.0)
    repeat["acoustic"].update({
        "capture_geometry": "reference_axis",
        "gating": {"applied": True, "f_valid_floor_hz": 150.0},
        "overlap_levels": [{
            "fc_hz": 1000.0,
            "above_validity_floor": True,
            "usable": True,
        }],
    })

    result = aggregate_driver_repeats([repeat])

    assert result["accepted"] == 0
    assert (
        result["per_repeat"][0]["reject_reason"]
        == "reference_axis_placement_unbound"
    )


def test_aggregate_never_mixes_near_field_and_reference_axis_repeats():
    near = _repeat(-30.0)
    near["acoustic"]["capture_geometry"] = "near_field"
    reference = _repeat(-30.1)
    reference["acoustic"].update({
        "capture_geometry": "reference_axis",
        "gating": {"applied": True, "f_valid_floor_hz": 150.0},
        "overlap_levels": [{
            "fc_hz": 1000.0,
            "above_validity_floor": True,
            "usable": True,
        }],
    })
    reference["placement_proof"] = _reference_axis_proof()

    result = aggregate_driver_repeats([near, reference])

    assert result["accepted"] == 1
    assert result["per_repeat"][1]["reject_reason"] == "capture_context_mismatch"


def test_aggregate_never_mixes_reference_axis_placement_bindings():
    repeats = []
    for comparison_hex, level in (("a", -30.0), ("b", -30.1)):
        repeat = _repeat(level)
        repeat["acoustic"].update({
            "capture_geometry": "reference_axis",
            "gating": {"applied": True, "f_valid_floor_hz": 150.0},
            "overlap_levels": [{
                "fc_hz": 1000.0,
                "above_validity_floor": True,
                "usable": True,
            }],
        })
        repeat["placement_proof"] = _reference_axis_proof(
            comparison_hex=comparison_hex
        )
        repeats.append(repeat)

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 1
    assert result["per_repeat"][1]["reject_reason"] == "capture_context_mismatch"


def test_aggregate_accepts_fresh_capture_binding_for_each_fixed_axis_repeat():
    repeats = []
    for index, level in enumerate((-30.0, -30.1, -29.9), start=1):
        repeat = _repeat(level)
        repeat["acoustic"].update({
            "capture_geometry": "reference_axis",
            "gating": {"applied": True, "f_valid_floor_hz": 150.0},
            "overlap_levels": [{
                "fc_hz": 1000.0,
                "above_validity_floor": True,
                "usable": True,
            }],
        })
        repeat["placement_proof"] = _reference_axis_proof(
            capture_session_id=f"capture-reference-{index}"
        )
        repeat["placement_proof"]["acknowledgement_binding_sha256"] = (
            f"{index:x}" * 64
        )
        repeats.append(repeat)

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 3
    assert all(item["reject_reason"] is None for item in result["per_repeat"])


def test_aggregate_woofer_bad_bottom_band_keeps_good_required_overlap():
    repeat = _repeat(-30.0, snr_verdict="insufficient")
    repeat["acoustic"]["overlap_levels"] = [
        {
            "fc_hz": 800.0,
            "usable": True,
            "snr_verdict": "ok",
            "above_validity_floor": True,
        }
    ]
    result = aggregate_driver_repeats([repeat])
    assert result["accepted"] == 1
    assert result["per_repeat"][0]["reject_reason"] is None


def test_aggregate_three_way_mid_accepts_one_of_two_usable_handoffs():
    repeat = _repeat(-30.0, snr_verdict="insufficient")
    repeat["acoustic"]["overlap_levels"] = [
        {
            "fc_hz": 250.0,
            "usable": False,
            "snr_verdict": "insufficient",
            "above_validity_floor": False,
        },
        {
            "fc_hz": 2400.0,
            "usable": True,
            "snr_verdict": "reduced",
            "above_validity_floor": True,
        },
    ]
    result = aggregate_driver_repeats([repeat])
    assert result["accepted"] == 1
    assert result["per_repeat"][0]["above_validity_floor"] is True


def test_aggregate_reads_real_driver_overlap_validity_shape_with_partial_pass():
    below = _repeat(-30.1)
    below["acoustic"]["overlap_levels"] = [
        {"fc_hz": 200.0, "above_validity_floor": False, "usable": False}
    ]
    partial = _repeat(-29.9)
    partial["acoustic"]["overlap_levels"] = [
        {"fc_hz": 200.0, "above_validity_floor": False, "usable": False},
        {"fc_hz": 2000.0, "above_validity_floor": True, "usable": True},
    ]

    result = aggregate_driver_repeats([_repeat(-30.0), below, partial])

    assert result["per_repeat"][1]["reject_reason"] == "below_validity_floor"
    assert result["per_repeat"][1]["above_validity_floor"] is False
    assert result["per_repeat"][2]["reject_reason"] is None
    assert result["per_repeat"][2]["above_validity_floor"] is True
    assert result["accepted"] == 2


def test_aggregate_absent_snr_and_gating_blocks_do_not_reject_everything():
    """Lane A/B's snr/gating blocks are Slice 0 sibling work that may not
    have landed yet; their absence must degrade to level-outlier-only
    detection, never to rejecting every repeat outright."""

    repeats = [_repeat(-30.0), _repeat(-30.1), _repeat(-29.9)]
    assert "snr" not in repeats[0]["acoustic"]
    assert "gating" not in repeats[0]["acoustic"]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 3
    assert result["rejected"] == 0


def test_aggregate_empty_repeats_degrades_gracefully():
    result = aggregate_driver_repeats([])

    assert result["accepted"] == 0
    assert result["rejected"] == 0
    assert result["aggregate_repeat"] is None
    assert result["spread_db_p90"] is None
    assert result["confidence"] == "reduced"
    assert result["needed_recapture"] is True
    assert result["recaptured"] is False


def test_aggregate_repeat_group_id_is_unique_per_call():
    repeats = [_repeat(-30.0), _repeat(-30.1), _repeat(-29.9)]

    first = aggregate_driver_repeats(repeats)
    second = aggregate_driver_repeats(repeats)

    assert first["repeat_group_id"] != second["repeat_group_id"]


def test_aggregate_target_is_configurable():
    repeats = [_repeat(-30.0), _repeat(-30.1)]

    result = aggregate_driver_repeats(repeats, target=2)

    assert result["target"] == 2
    assert result["accepted"] == 2
    assert result["needed_recapture"] is False
    assert result["confidence"] == "normal"


# --- Spec-promise guard: outlier rejection, never noise-floor reduction ----


def test_aggregate_spec_promise_no_complex_ir_input_and_no_averaged_curve():
    """aggregate_driver_repeats' only numeric input is a scalar magnitude
    (observed_mic_dbfs) per repeat -- there is no complex/IR parameter
    anywhere in its signature or in the per-repeat shape it reads, and the
    winning repeat's full acoustic block (including any SNR block) is
    reused byte-for-byte, never synthesized from an average across
    repeats."""

    import inspect

    sig = inspect.signature(aggregate_driver_repeats)
    assert "complex" not in str(sig).lower()
    assert "ir" not in {p.lower() for p in sig.parameters}

    repeats = [
        _repeat(-30.0, artifact_path="r0.wav"),
        _repeat(-30.05, artifact_path="r1.wav"),
        _repeat(-29.95, artifact_path="r2.wav"),
    ]
    # Give each a distinguishable SNR reading so an averaged/synthesized
    # value (e.g. the mean of 28/30/32 = 30) would be detectable.
    repeats[0]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 28.0}}
    repeats[1]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 30.0}}
    repeats[2]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 32.0}}

    result = aggregate_driver_repeats(repeats)

    winner = result["aggregate_repeat"]
    assert winner is not None
    # The winner is EXACTLY one of the input repeats (object equality on the
    # nested acoustic dict), not a new dict with blended/averaged fields.
    assert winner["acoustic"] in (
        repeats[0]["acoustic"],
        repeats[1]["acoustic"],
        repeats[2]["acoustic"],
    )
    winning_snr = winner["acoustic"]["snr"]["worst_relevant"]["estimated_snr_db"]
    assert winning_snr in (28.0, 30.0, 32.0)  # one repeat's real value
    assert winning_snr != sum([28.0, 30.0, 32.0]) / 3  # never the average


def test_record_driver_repeat_aggregate_emits_lifecycle_event(caplog):
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-29.8)]

    with caplog.at_level(logging.INFO):
        result = record_driver_repeat_aggregate(
            speaker_group_id="mono",
            role="woofer",
            repeats=repeats,
            session_id="sess-1",
        )

    assert result["accepted"] == 3
    fields = event_fields(caplog, "correction.crossover_repeats_aggregated")
    assert fields["session"] == "sess-1"
    assert fields["group"] == "mono"
    assert fields["role"] == "woofer"
    assert fields["accepted"] == "3"
    assert fields["rejected"] == "0"


def test_repeat_outlier_threshold_constant_is_positive_and_documented():
    assert REPEAT_OUTLIER_DB > 0
