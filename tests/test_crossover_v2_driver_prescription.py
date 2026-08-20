# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The per-driver prescription class: full-band, cuts only, classify first.

Four things are pinned here and they fail in different ways:

* **the bounds are the fit engine's own** — every ceiling this gate applies is
  numerically the constant ``linearization_fit`` already emits up to, so a
  prescriber can never be granted a move the deterministic path could not make,
  and re-deriving one of them moves both;
* **the classification bar is load-bearing** — a cut with no
  ``defect-cuttable`` verdict for its target is refused, and the mutation that
  removes the check makes an accepted document out of a refused one;
* **the boost class is structurally unreachable** — two independent gates, and
  each is proved by disabling the other;
* **an accepted prescription reaches the emitted graph through the SAME
  per-branch seam the fit uses**, and survives the emitter's own independent
  re-validation there.

Synthetic bundle on ``tmp_path``, for the reason the blend suite states:
``captures/`` is gitignored and a suite that needed it would only run on one
laptop.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.crossover_v2 import driver_prescription as dp
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.crossover_v2.blend_prescription import (
    PRESCRIPTION_KIND,
    PRESCRIPTION_SCHEMA_VERSION,
    BlendPrescriptionRefused,
)
from jasper.active_speaker.crossover_v2.driver_prescription import (
    DRIVER_MAX_COMPOSED_CUT_DB,
    DRIVER_MAX_CUT_Q,
    DRIVER_MAX_FILTER_CUT_DB,
    DRIVER_MAX_FILTERS_PER_ROLE,
    DRIVER_MIN_CUT_DB,
    DRIVER_MIN_Q,
    DRIVER_PRESCRIPTION_KIND,
    DRIVER_PRESCRIPTION_REFUSAL_REASONS,
    DRIVER_PRESCRIPTION_SCHEMA_VERSION,
    LINEARIZATION_CANDIDATE_FIELD,
    DriverPrescription,
    driver_passbands_from_safety_profile,
    driver_prescription_from_mapping,
    driver_prescription_response_format,
    driver_prescription_route,
    driver_prescription_to_candidate_fields,
    read_driver_prescription,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    PACKET_SCHEMA_VERSION,
    build_crossover_evidence_packet,
    packet_driver_passbands_hz,
    packet_feature_classifications,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    DEFECT_BOOSTABLE,
    DEFECT_CUTTABLE,
    INTERFERENCE_BARRED,
    ROOM,
    UNRESOLVED,
    VERDICT_MATCH_TOLERANCE_OCTAVES,
    defect_cuttable_at,
    read_feature_verdicts,
)
from jasper.active_speaker.linearization_fit import (
    MAX_FILTERS_PER_DRIVER,
    MAX_NORMALIZATION_SPEND_DB,
    PER_FILTER_CUT_CAP_DB,
    linearization_filters_by_role,
)
from jasper.cli import crossover_prescriber as cli

from tests.test_crossover_v2_blend_prescription import _bundle

#: The synthetic speaker: a woofer declared 40 Hz-4 kHz with a protective
#: low-pass at 3 kHz, and a tweeter declared 1 kHz-20 kHz with a protective
#: high-pass at 1.6 kHz — the B&C DE250 figure the shipped JTS3 preset carries.
WOOFER_BAND = (40.0, 3000.0)
TWEETER_BAND = (1600.0, 20000.0)

#: One classified feature per driver, both `defect-cuttable`.
WOOFER_FEATURE_HZ = 900.0
TWEETER_FEATURE_HZ = 5000.0


def _draft() -> dict[str, Any]:
    """A design draft carrying a confirmed driver-safety profile."""
    return {
        "kind": "jts_active_speaker_design_draft",
        "driver_safety_profile": {
            "kind": "jts_active_speaker_driver_safety_profile",
            "confirmation": {"confirmed_fingerprint": "abc", "method": "operator"},
            "targets": [
                {
                    "role": "woofer",
                    "measurement_band_hz": [40.0, 4000.0],
                    "hard_excitation_band_hz": [30.0, 5000.0],
                    "required_protection_filters": [
                        {"kind": "lowpass", "cutoff_hz": 3000.0,
                         "minimum_slope_db_per_octave": 24},
                    ],
                },
                {
                    "role": "tweeter",
                    "measurement_band_hz": [1000.0, 20000.0],
                    "hard_excitation_band_hz": [900.0, 22000.0],
                    "required_protection_filters": [
                        {"kind": "highpass", "cutoff_hz": 1600.0,
                         "minimum_slope_db_per_octave": 24},
                    ],
                },
            ],
        },
    }


def _verdict(
    hz: float,
    classification: str = DEFECT_CUTTABLE,
    **over: Any,
) -> dict[str, Any]:
    row = {
        "hz": hz,
        "classification": classification,
        "egd_verdict": "MIN-PHASE",
        "gate_verdict": "STABLE",
        "confidence": "high",
        "measured_q": 5.1,
        "vertical_blind": False,
        # A lab row carries thirty-odd more columns; the reader takes what it
        # needs and the rest ride along, which is what this pair proves.
        "z_local": 4.2,
        "frac_of_nmp": 0.11,
    }
    row.update(over)
    return row


def _classification(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if rows is None:
        rows = [_verdict(WOOFER_FEATURE_HZ), _verdict(TWEETER_FEATURE_HZ)]
    return {"schema": 1, "thresholds": {"frac_of_nmp": 0.35}, "rows": rows}


def _speaker(
    tmp_path: Path,
    *,
    draft: dict[str, Any] | None = _draft(),
    classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A bundle plus the two per-driver evidence sources, as a packet."""
    session, _ = _bundle(tmp_path)
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    if classification is None:
        classification = _classification()
    if classification is not False:
        (round_dir / "feature_classification.json").write_text(
            json.dumps(classification)
        )
    draft_path = None
    if draft is not None:
        draft_path = tmp_path / "draft.json"
        draft_path.write_text(json.dumps(draft))
    return build_crossover_evidence_packet(session, driver_draft_path=draft_path)


@pytest.fixture
def packet(tmp_path: Path) -> dict[str, Any]:
    return _speaker(tmp_path)


def _gate(packet: dict[str, Any], document: Any) -> Any:
    """The gate, called the one way its three inputs are meant to be derived."""
    return read_driver_prescription(
        document,
        packet_fingerprint=packet.get("packet_fingerprint"),
        passbands_hz=packet_driver_passbands_hz(packet),
        classifications=packet_feature_classifications(packet),
    )


def _cut(
    role: str = "tweeter",
    gain: float = -3.0,
    freq: float = TWEETER_FEATURE_HZ,
    q: float = 5.0,
) -> dict[str, Any]:
    return {
        "role": role, "biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain,
    }


def _document(filters: Any, packet: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_schema_version": DRIVER_PRESCRIPTION_SCHEMA_VERSION,
        "kind": DRIVER_PRESCRIPTION_KIND,
        "packet_fingerprint": packet["packet_fingerprint"],
        "prescriber": {"model": "claude-opus-5", "operator": "jasper"},
        "filters": filters,
        "rationale": "the tweeter's 5 kHz breakup mode is classified cuttable",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# the band — the driver's OWN declaration, not the crossover's
# --------------------------------------------------------------------------- #


def test_the_band_is_the_published_range_narrowed_by_declared_protection():
    """Published range in, protective corners applied, one band per role.

    The tweeter's published 1 kHz floor is raised to its declared 1.6 kHz
    protective high-pass (#2736's floor, read through its one owner); the
    woofer's published 4 kHz ceiling is lowered to its declared 3 kHz
    protective low-pass. Neither edge is invented where none is declared.
    """
    bands = driver_passbands_from_safety_profile(
        _draft()["driver_safety_profile"]
    )

    assert bands == {"woofer": WOOFER_BAND, "tweeter": TWEETER_BAND}


def test_an_undeclared_protection_corner_leaves_the_published_edge_standing():
    """Never-nanny: no declaration means no substituted class default.

    ``driver_protection``'s own rule, applied here. Substituting the style's
    5 kHz policy corner for an undeclared tweeter floor would refuse honest
    proposals on a number the operator never declared.
    """
    profile = _draft()["driver_safety_profile"]
    for target in profile["targets"]:
        target.pop("required_protection_filters", None)

    assert driver_passbands_from_safety_profile(profile) == {
        "woofer": (40.0, 4000.0), "tweeter": (1000.0, 20000.0),
    }


@pytest.mark.parametrize("broken", [
    {"measurement_band_hz": None},
    {"measurement_band_hz": [100.0]},
    {"measurement_band_hz": ["a", "b"]},
    {"measurement_band_hz": [4000.0, 40.0]},
    {"measurement_band_hz": [0.0, 4000.0]},
    {"role": ""},
    # A declared protection pair narrower than the published range admits.
    {"required_protection_filters": [
        {"kind": "highpass", "cutoff_hz": 9000.0},
        {"kind": "lowpass", "cutoff_hz": 500.0},
    ]},
])
def test_an_unreadable_declaration_omits_the_role_rather_than_guessing(broken):
    """A role with no honest band is left out, and the gate refuses it by name."""
    profile = _draft()["driver_safety_profile"]
    profile["targets"][0].update(broken)

    assert "woofer" not in driver_passbands_from_safety_profile(profile)


def test_a_declared_band_past_nyquist_is_clamped_not_dropped():
    """The one edge that is not a declaration.

    A supertweeter published to 40 kHz is an honest datasheet fact, and a
    composed-cut bound evaluated past half the sample rate is an aliased number
    wearing a safety bound's name. The role keeps its band; the top of it is
    the evaluator's own limit, and the packet publishes the clamped value so a
    prescriber is shown the band it will actually be judged against.
    """
    from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

    profile = _draft()["driver_safety_profile"]
    profile["targets"][1]["measurement_band_hz"] = [1000.0, 40000.0]
    profile["targets"][1]["required_protection_filters"] = []

    bands = driver_passbands_from_safety_profile(profile)

    assert bands["tweeter"] == (1000.0, RESPONSE_SAMPLE_RATE_HZ / 2.0)


def test_the_band_is_not_the_crossover_region(packet):
    """The class's whole point: a driver is correctable outside the handoff.

    The packet's own crossover region is 824-3297 Hz. The tweeter's declared
    band reaches 20 kHz, and a cut at 5 kHz — six times the region's own
    centre — is accepted here and would be refused ``filter_outside_region`` by
    the blend gate at any Q.
    """
    region = packet["crossover_region"]["band_hz"]
    assert TWEETER_FEATURE_HZ > region[1]

    prescription = _gate(packet, _document([_cut()], packet))

    assert prescription.filters[0]["freq"] == TWEETER_FEATURE_HZ


# --------------------------------------------------------------------------- #
# the packet
# --------------------------------------------------------------------------- #


def test_the_packet_carries_the_bands_and_the_verdicts(packet):
    assert packet["drivers"]["available"] is True
    assert packet["drivers"]["passbands_hz"] == {
        "tweeter": [TWEETER_BAND[0], TWEETER_BAND[1]],
        "woofer": [WOOFER_BAND[0], WOOFER_BAND[1]],
    }
    assert packet["feature_classification"]["available"] is True
    assert packet["feature_classification"]["n_rows_readable"] == 2


def test_a_missing_draft_is_reported_not_papered_over(tmp_path):
    packet = _speaker(tmp_path, draft=None)

    assert packet["drivers"]["available"] is False
    assert packet["drivers"]["reason"] == "no driver design draft was supplied"
    assert any(
        entry["field"] == "drivers.passbands_hz"
        for entry in packet["not_evaluated"]
    )


def test_a_missing_classification_is_reported_not_papered_over(tmp_path):
    packet = _speaker(tmp_path, classification=False)

    assert packet["feature_classification"]["available"] is False
    assert packet["feature_classification"]["reason"] == "source_absent"


def test_the_not_built_disclosure_stops_being_printed_once_one_is_banked(
    tmp_path, packet
):
    """"We did not look" must not be printed beside the thing we looked at.

    The ``per_bin_minimum_phase_class`` entry was unconditional before a round
    could carry banked verdicts. Left unconditional it would be the packet's own
    honesty block telling a reader to disregard a block two keys above it.
    """
    without = _speaker(tmp_path / "b", classification=False)

    fields = {entry["field"] for entry in packet["not_evaluated"]}
    assert "per_bin_minimum_phase_class" not in fields
    assert "per_bin_minimum_phase_class" in {
        entry["field"] for entry in without["not_evaluated"]
    }


def test_an_unreadable_verdict_row_is_dropped_not_admitted_as_ambiguous(tmp_path):
    """Fail-closed: a row that cannot be typed can never vouch for anything.

    Both counts are published so the drop is visible rather than a denominator
    that moved silently.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(TWEETER_FEATURE_HZ),
        {"hz": None, "classification": DEFECT_CUTTABLE},
        {"hz": 4000.0},
        "not even a row",
    ]))

    block = packet["feature_classification"]
    assert block["n_rows_banked"] == 4
    assert block["n_rows_readable"] == 1


def test_the_two_contracts_sit_side_by_side_and_neither_reads_the_round(tmp_path):
    """Both response formats are pure constants, on the packet's own rule.

    A packet's instructions are the same bytes whatever the round measured,
    which is what makes injection through the packet structurally impossible
    rather than merely filtered.
    """
    one = _speaker(tmp_path / "a")
    two = _speaker(tmp_path / "b", classification=_classification([
        _verdict(1234.0, INTERFERENCE_BARRED),
    ]))

    assert one["driver_response_format"] == two["driver_response_format"]
    assert one["response_format"] == two["response_format"]
    assert one["driver_response_format"]["kind"].endswith("driver_prescription_contract")


def test_the_response_format_states_every_bound_the_gate_applies():
    """A contract that omitted a bound would send a prescriber into a refusal."""
    bounds = driver_prescription_response_format()["bounds"]

    assert bounds["q_min"] == DRIVER_MIN_Q
    assert bounds["q_max"] == DRIVER_MAX_CUT_Q
    assert bounds["min_cut_db"] == DRIVER_MIN_CUT_DB
    assert bounds["max_filter_cut_db"] == DRIVER_MAX_FILTER_CUT_DB
    assert bounds["max_composed_cut_db"] == DRIVER_MAX_COMPOSED_CUT_DB
    assert bounds["max_filters_per_role"] == DRIVER_MAX_FILTERS_PER_ROLE
    fmt = driver_prescription_response_format()
    assert fmt["classification_bar"]["eligible_classification"] == DEFECT_CUTTABLE
    assert fmt["cuts_only"]["refusal"] == dp.BOOST_ROUTE_UNAVAILABLE
    assert set(fmt["refusal_reasons"]) == DRIVER_PRESCRIPTION_REFUSAL_REASONS


# --------------------------------------------------------------------------- #
# the gate — acceptance, then every bound, by name, both sides
# --------------------------------------------------------------------------- #


def test_a_well_formed_per_driver_cut_is_accepted_and_classified(packet):
    prescription = _gate(packet, _document([_cut()], packet))

    assert prescription.prescription_class == "cut"
    assert prescription.roles == ("tweeter",)
    assert prescription.prescriber_model == "claude-opus-5"
    assert prescription.classification_basis[0].verdict.classification == DEFECT_CUTTABLE
    assert prescription.classification_basis[0].filter_freq_hz == TWEETER_FEATURE_HZ


def test_no_prescription_is_the_deterministic_path_untouched(packet):
    assert _gate(packet, None) is None
    assert driver_prescription_to_candidate_fields(None, fitted=None) == {}


@pytest.mark.parametrize(("freq", "ok"), [
    (TWEETER_BAND[0], True),
    (TWEETER_BAND[1], True),
    (TWEETER_BAND[0] - 0.1, False),
    (TWEETER_BAND[1] + 0.1, False),
])
def test_the_passband_edges_are_inclusive_and_refuse_by_name(packet, freq, ok):
    """Exactness is legal; one step past is `driver_filter_outside_passband`.

    Both sides, because a bound tested on one side only is a bound whose
    direction nothing pins.
    """
    rows = [_verdict(freq)]
    document = _document([_cut(freq=freq)], packet)
    packet = dict(packet)
    packet["feature_classification"] = {
        "available": True, "verdicts": [read_feature_verdicts(rows)[0].to_dict()],
    }
    if ok:
        assert _gate(packet, document).filters[0]["freq"] == freq
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == dp.FILTER_OUTSIDE_PASSBAND


@pytest.mark.parametrize(("q", "ok"), [
    (DRIVER_MIN_Q, True),
    (DRIVER_MAX_CUT_Q, True),
    (DRIVER_MIN_Q - 0.01, False),
    (DRIVER_MAX_CUT_Q + 0.01, False),
])
def test_the_q_bounds_are_inclusive_and_refuse_by_name(packet, q, ok):
    document = _document([_cut(q=q)], packet)
    if ok:
        assert _gate(packet, document).filters[0]["q"] == q
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == dp.FILTER_Q_OUT_OF_RANGE
    assert excinfo.value.evidence["q_max"] == DRIVER_MAX_CUT_Q


@pytest.mark.parametrize(("gain", "reason"), [
    (-DRIVER_MIN_CUT_DB, None),
    (-DRIVER_MAX_FILTER_CUT_DB, None),
    (-DRIVER_MIN_CUT_DB + 0.01, dp.FILTER_CUT_TOO_SHALLOW),
    (0.0, dp.FILTER_CUT_TOO_SHALLOW),
    (-DRIVER_MAX_FILTER_CUT_DB - 0.01, dp.FILTER_CUT_TOO_DEEP),
])
def test_the_depth_bounds_are_inclusive_and_refuse_by_name(packet, gain, reason):
    """A zero-gain filter is TOO SHALLOW, not a boost and not a cut.

    It is inert whatever its Q, and spending one of the branch's eight slots on
    an inaudible filter is the thing the floor exists to stop. Sorting it into
    the shallow arm rather than the boost arm keeps the boost refusal meaning
    exactly one thing.
    """
    document = _document([_cut(gain=gain)], packet)
    if reason is None:
        assert _gate(packet, document).filters[0]["gain"] == gain
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == reason


def test_a_role_the_speaker_declares_no_band_for_is_refused_by_name(packet):
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut(role="midrange")], packet))

    assert excinfo.value.reason == dp.ROLE_UNKNOWN
    assert excinfo.value.evidence["declared_roles"] == ["tweeter", "woofer"]


def test_a_packet_with_no_declared_band_refuses_rather_than_inventing_one(tmp_path):
    packet = _speaker(tmp_path, draft=None)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet))

    assert excinfo.value.reason == dp.PASSBAND_UNAVAILABLE


def test_the_per_role_filter_count_is_the_branchs_own_ceiling(packet, tmp_path):
    """Eight per role, and the ninth refuses — the emitter's own number.

    A prescription past it would be accepted here and refused at emission, which
    is the one failure shape a gate exists to prevent.
    """
    rows = [_verdict(2000.0 + 400.0 * i) for i in range(9)]
    packet = _speaker(tmp_path / "many", classification=_classification(rows))
    filters = [_cut(freq=2000.0 + 400.0 * i, q=8.0) for i in range(9)]

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(filters, packet))
    assert excinfo.value.reason == dp.FILTER_COUNT_EXCEEDED
    assert excinfo.value.evidence["max_filters"] == DRIVER_MAX_FILTERS_PER_ROLE

    # Eight is legal, and two roles do not share the ceiling.
    assert len(_gate(packet, _document(filters[:8], packet)).filters) == 8


def test_the_composed_cap_is_evaluated_per_role_not_summed(packet, tmp_path):
    """Two filters at the same frequency deliver more than either alone.

    Each is inside the per-filter ceiling; the cascade is not. Checked on the
    evaluated response through the one biquad evaluator, so this gate and the
    emitter cannot disagree about what CamillaDSP will realize.
    """
    packet = _speaker(tmp_path / "deep", classification=_classification([
        _verdict(TWEETER_FEATURE_HZ),
    ]))
    stacked = [_cut(gain=-10.0, q=1.0), _cut(gain=-10.0, q=1.0)]

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(stacked, packet))

    assert excinfo.value.reason == dp.COMPOSED_CUT_EXCEEDED
    assert excinfo.value.evidence["role"] == "tweeter"
    assert excinfo.value.evidence["composed_cut_db"] < -DRIVER_MAX_COMPOSED_CUT_DB


def test_one_roles_composed_spend_is_not_charged_to_the_other(packet, tmp_path):
    """Per role, because the quantity is one branch's own ledger."""
    packet = _speaker(tmp_path / "split", classification=_classification([
        _verdict(TWEETER_FEATURE_HZ), _verdict(WOOFER_FEATURE_HZ),
    ]))
    both = [
        _cut(gain=-11.0, q=1.0),
        _cut(role="woofer", freq=WOOFER_FEATURE_HZ, gain=-11.0, q=1.0),
    ]

    assert len(_gate(packet, _document(both, packet)).filters) == 2


@pytest.mark.parametrize(("over", "reason"), [
    ({"kind": PRESCRIPTION_KIND}, dp.DRIVER_PRESCRIPTION_MALFORMED),
    ({"kind": None}, dp.DRIVER_PRESCRIPTION_MALFORMED),
    ({"artifact_schema_version": 2}, dp.DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED),
    ({"packet_fingerprint": "not-this-round"}, dp.DRIVER_PRESCRIPTION_PACKET_MISMATCH),
    ({"prescriber": {"model": "m"}}, dp.DRIVER_PRESCRIPTION_PROVENANCE_MISSING),
    ({"prescriber": {"model": "m", "operator": " "}},
     dp.DRIVER_PRESCRIPTION_PROVENANCE_MISSING),
    ({"typo": 1}, dp.DRIVER_PRESCRIPTION_MALFORMED),
    ({"rationale": 7}, dp.DRIVER_PRESCRIPTION_MALFORMED),
    ({"rationale": "x" * 1_201}, dp.DRIVER_PRESCRIPTION_MALFORMED),
])
def test_the_gate_refuses_a_malformed_identity_or_provenance(packet, over, reason):
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet, **over))

    assert excinfo.value.reason == reason


@pytest.mark.parametrize("filters", [
    None, {}, "filters", [None], [[]],
    [{"role": "tweeter", "biquad_type": "Highshelf", "freq": 5000.0, "q": 2.0,
      "gain": -3.0}],
    [{"biquad_type": "Peaking", "freq": 5000.0, "q": 2.0, "gain": -3.0}],
    [{"role": " ", "biquad_type": "Peaking", "freq": 5000.0, "q": 2.0, "gain": -3.0}],
    [{"role": "tweeter", "biquad_type": "Peaking", "freq": 0.0, "q": 2.0,
      "gain": -3.0}],
    [{"role": "tweeter", "biquad_type": "Peaking", "freq": "5000", "q": 2.0,
      "gain": -3.0}],
    [{"role": "tweeter", "biquad_type": "Peaking", "freq": 5000.0, "q": True,
      "gain": -3.0}],
    [{"role": "tweeter", "biquad_type": "Peaking", "freq": 10**400, "q": 2.0,
      "gain": -3.0}],
    [{"role": "tweeter", "biquad_type": "Peaking", "freq": float("nan"), "q": 2.0,
      "gain": -3.0}],
    [dict(_cut(), extra=1)],
])
def test_the_gate_refuses_every_malformed_filter(packet, filters):
    """Peaking only, role required, no coercion, and no unknown field.

    A shelf is refused because per-driver LEVEL is the trim's fact and the fit's
    normalization ledger, neither of which an intake may reach past.
    """
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(filters, packet))

    assert excinfo.value.reason == dp.FILTER_MALFORMED


@pytest.mark.parametrize("payload", [
    {"volume_db": -3},
    {"camilladsp_config": {}},
    {"delay_us": 120},
    {"role_attenuations_db": {"tweeter": -2.0}},
    {"prescriber": {"model": "m", "operator": "o", "shell": "rm -rf /"}},
])
def test_a_prescription_may_not_reach_past_numbers_into_a_fixed_shape(packet, payload):
    """The blocklist is the family's one set, walked by the family's one walk.

    ``role_attenuations_db`` stays prohibited even though this class names a
    role per FILTER: naming which driver a filter belongs to is this seam's
    subject, naming a driver's level is not.
    """
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet, **payload))

    assert excinfo.value.reason == dp.DRIVER_PRESCRIPTION_PROHIBITED_FIELD


# --------------------------------------------------------------------------- #
# the classification bar — stage P3 rule 1, and the mutation that proves it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("classification", [
    INTERFERENCE_BARRED, ROOM, DEFECT_BOOSTABLE, UNRESOLVED, "something-new",
])
def test_only_a_defect_cuttable_verdict_admits_a_cut(tmp_path, classification):
    """Every other verdict is refused, and the refusal names which it was.

    A cancellation is lowered along with the direct sound; a room arrival is not
    the speaker's to correct; a minimum-phase DIP would be deepened by a cut,
    not filled. An unknown string satisfies nothing, which is why the register
    keeps it rather than rejecting it — a refusal can quote it.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(TWEETER_FEATURE_HZ, classification),
    ]))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_CUTTABLE
    assert excinfo.value.evidence["classification"] == classification


def test_an_unclassified_frequency_is_go_and_measure_not_no(tmp_path):
    """The two refusals are different instructions and must stay apart.

    "Nothing was classified there" sends a prescriber to run the classifier;
    "the feature there is barred" tells it the answer is no and that a different
    filter will not fix it.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(WOOFER_FEATURE_HZ),
    ]))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED


def test_a_packet_with_no_banked_classification_refuses_every_cut(tmp_path):
    packet = _speaker(tmp_path, classification=False)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut()], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED


def test_a_prescription_that_cuts_nothing_needs_no_classification(tmp_path):
    """Ordering, not leniency: the bar's whole content is per-filter.

    Refusing an empty document for want of a classification would report a
    missing-evidence problem to an author who did not ask to correct anything.
    It names no role, so it contributes no per-branch field and changes nothing.
    """
    packet = _speaker(tmp_path, classification=False)

    accepted = _gate(packet, _document([], packet))

    assert accepted.filters == ()
    assert accepted.classification_basis == ()
    assert driver_prescription_to_candidate_fields(accepted, fitted=None) == {
        LINEARIZATION_CANDIDATE_FIELD: {}
    }


@pytest.mark.parametrize("octaves", [0.0, 0.16, -0.16, 0.17, -0.17, 0.5])
def test_a_filter_may_not_borrow_a_distant_features_verdict(tmp_path, octaves):
    """The match radius is the resolution the verdict was located at.

    Inside it the verdict decides; outside it nothing was classified there.

    **It is NOT what keeps a filter off its neighbour's verdict**, and the first
    version of this docstring said it was — on the strength of three of the
    2026-08-19 record's nine features and its two widest gaps. All eight gaps
    are 0.439 / 0.549 / 1.012 / **0.143** / 0.236 / 0.211 / 0.450 / **0.157**
    octaves, and the two in bold are narrower than this tolerance. Both are
    peak/dip pairs. Borrowing is prevented by the NEAREST-verdict rule instead,
    which needs no minimum separation at all; this radius only bounds how far a
    filter may sit from the feature it is judged against.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(TWEETER_FEATURE_HZ),
    ]))
    freq = TWEETER_FEATURE_HZ * (2.0 ** octaves)
    document = _document([_cut(freq=freq)], packet)

    if abs(octaves) <= VERDICT_MATCH_TOLERANCE_OCTAVES:
        assert _gate(packet, document).filters[0]["freq"] == pytest.approx(freq)
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED


def test_the_classification_check_is_load_bearing_not_decorative(
    packet, tmp_path, monkeypatch
):
    """The mutation: remove the check and a refused document is accepted.

    An argument in place of a guard guards nothing. This is the positive
    control for every refusal above — without it, they would all still pass on
    a gate whose classification arm had been deleted, because each of them
    ALSO clears a shape bound.
    """
    barred = _speaker(tmp_path / "barred", classification=_classification([
        _verdict(TWEETER_FEATURE_HZ, INTERFERENCE_BARRED),
    ]))
    document = _document([_cut()], barred)

    with pytest.raises(BlendPrescriptionRefused):
        _gate(barred, document)

    monkeypatch.setattr(dp, "_check_classification", lambda filters, verdicts: ())
    accepted = _gate(barred, document)

    assert accepted.filters[0]["freq"] == TWEETER_FEATURE_HZ
    assert accepted.classification_basis == ()


def test_a_defect_verdict_is_necessary_and_the_contract_says_not_sufficient():
    """Run-log §9.2, carried into the document a prescriber reads.

    Every EQ arm played on 2026-08-19 measured worse. A contract that offered
    the verdict as a recommendation would be inviting the mistake the campaign
    had already made.
    """
    bar = driver_prescription_response_format()["classification_bar"]

    assert "does not say EQ will help" in bar["necessary_not_sufficient"]


def test_the_nearest_verdict_decides_and_a_further_cuttable_one_cannot_vouch():
    """Two features inside the radius: the CLOSEST claim owns the frequency.

    **This test asserted the opposite shape when it was written**, and pinning
    that shape is what let the bug ship: a cuttable verdict standing further
    away used to vouch while the nearer, barred one only wrote the refusal
    message it never got to raise. On the real record that made three of the
    four minimum-phase dips cuttable — see the regression below.

    The reader still returns both values, because a refusal has to tell "nothing
    was classified here" from "the thing here is barred". What changed is which
    one decides.
    """
    verdicts = read_feature_verdicts([
        _verdict(5000.0, INTERFERENCE_BARRED),
        _verdict(5200.0, DEFECT_CUTTABLE),
    ])

    vouching, nearest = defect_cuttable_at(verdicts, 5050.0)

    assert nearest.classification == INTERFERENCE_BARRED
    assert vouching is None

    # And aiming at the cuttable one instead is how a prescriber gets it.
    vouching, nearest = defect_cuttable_at(verdicts, 5190.0)
    assert nearest.classification == DEFECT_CUTTABLE
    assert vouching is nearest


def test_an_equidistant_tie_falls_closed():
    """Pathological rather than impossible, and it must not depend on row order.

    A rule whose answer came from the artifact's ordering would be a rule the
    record could silently change. The non-cuttable verdict wins either way.
    """
    # Powers of two, so "equidistant" is exact in binary rather than nearly so:
    # log2(2000/1000) and log2(4000/2000) are both exactly 1.0.
    rows = [_verdict(1000.0, DEFECT_CUTTABLE), _verdict(4000.0, DEFECT_BOOSTABLE)]

    for ordered in (rows, list(reversed(rows))):
        vouching, nearest = defect_cuttable_at(
            read_feature_verdicts(ordered), 2000.0, tolerance_octaves=1.0
        )
        assert vouching is None
        assert nearest.classification == DEFECT_BOOSTABLE


#: The 2026-08-19 banked record, verbatim — nine classified features, four of
#: them minimum-phase DIPS. Reproduced as a literal rather than read from
#: ``captures/`` (gitignored: a suite that needed it would run on one laptop),
#: and the two peak/dip gaps narrower than the match tolerance are the whole
#: reason this fixture exists.
_BANKED_RECORD = (
    (1037.0, DEFECT_BOOSTABLE, "med", 6.596),
    (1406.0, DEFECT_CUTTABLE, "med", 5.132),
    (2057.0, DEFECT_CUTTABLE, "med", 3.949),
    (4149.0, DEFECT_CUTTABLE, "med", 12.066),
    (4582.0, DEFECT_BOOSTABLE, "high", 12.066),
    (5396.0, DEFECT_CUTTABLE, "high", 12.066),
    (6245.0, DEFECT_BOOSTABLE, "high", 10.401),
    (8530.0, DEFECT_BOOSTABLE, "high", 18.397),
    (9509.0, DEFECT_CUTTABLE, "high", 18.397),
)


def _banked_verdicts():
    return read_feature_verdicts([
        _verdict(hz, cls, confidence=conf, measured_q=q)
        for hz, cls, conf, q in _BANKED_RECORD
    ])


def test_two_real_gaps_sit_inside_the_match_tolerance_and_both_are_peak_dip():
    """The fixture's own control: the hazard this rule exists for is present.

    A regression suite whose record no longer contained a sub-tolerance peak/dip
    pair would pass for the wrong reason — every row would be decided by its own
    verdict because nothing else was near enough to borrow. Naming the two gaps
    keeps the test honest about what it is exercising.
    """
    hz = [row[0] for row in _BANKED_RECORD]
    gaps = [math.log2(b / a) for a, b in zip(hz, hz[1:], strict=False)]
    inside = [
        (hz[i], hz[i + 1]) for i, gap in enumerate(gaps)
        if gap <= VERDICT_MATCH_TOLERANCE_OCTAVES
    ]

    assert [round(g, 3) for g in gaps] == [
        0.439, 0.549, 1.012, 0.143, 0.236, 0.211, 0.45, 0.157
    ]
    assert inside == [(4149.0, 4582.0), (8530.0, 9509.0)]
    for lower, upper in inside:
        pair = {
            dict(zip(("hz", "cls"), row[:2], strict=False))["cls"]
            for row in _BANKED_RECORD if row[0] in (lower, upper)
        }
        assert pair == {DEFECT_CUTTABLE, DEFECT_BOOSTABLE}


@pytest.mark.parametrize(("freq_hz", "role", "accepted"), [
    # The four dips. Every one of them refuses, and 4582 / 8530 are the two the
    # old any-cuttable-vouches rule ACCEPTED by borrowing 4149 / 9509.
    (1037.0, "woofer", False),
    (4582.0, "tweeter", False),
    (6245.0, "tweeter", False),
    (8530.0, "tweeter", False),
    # The peaks, including both halves of the two sub-tolerance pairs.
    (4149.0, "tweeter", True),
    (5396.0, "tweeter", True),
    (9509.0, "tweeter", True),
])
def test_the_banked_record_refuses_every_dip_and_accepts_every_peak(
    tmp_path, freq_hz, role, accepted
):
    """The regression, on the real record, at the frequencies that mattered.

    Cutting a minimum-phase dip deepens it — the harm ``driver_feature_not_cuttable``
    names in its own docstring — so a rule that let a neighbouring peak vouch was
    the gate accepting the exact proposal it exists to stop.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(hz, cls, confidence=conf, measured_q=q)
        for hz, cls, conf, q in _BANKED_RECORD
    ]))
    # The role is whichever driver's declared band holds the frequency — 1037 Hz
    # is under the tweeter's 1.6 kHz protective floor, so it is the woofer's.
    document = _document([_cut(role=role, freq=freq_hz)], packet)

    if accepted:
        assert _gate(packet, document).filters[0]["freq"] == freq_hz
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == dp.FEATURE_NOT_CUTTABLE
    # The refusal quotes the feature the filter is ON, never the neighbour.
    assert excinfo.value.evidence["hz"] == freq_hz
    assert excinfo.value.evidence["classification"] == DEFECT_BOOSTABLE
    assert "deepens it" in excinfo.value.detail


def test_the_old_any_cuttable_rule_would_have_accepted_the_dips():
    """The mutation, spelled out: restore the old rule and the probe stops biting.

    Written as a re-implementation rather than a monkeypatch because the defect
    was a CHOICE of which verdict decides, not a missing guard — there is
    nothing to delete, so "does the test still pass with the check removed" has
    no meaning here. This reproduces the shipped-then-fixed rule exactly and
    shows it disagreeing with the current one on the two frequencies the review
    found.
    """
    verdicts = _banked_verdicts()

    def any_cuttable_vouches(freq_hz):  # the pre-fix rule, verbatim in effect
        best = None
        best_distance = math.inf
        for verdict in verdicts:
            distance = abs(math.log2(verdict.freq_hz / freq_hz))
            if distance > VERDICT_MATCH_TOLERANCE_OCTAVES:
                continue
            if verdict.is_defect_cuttable and distance < best_distance:
                best, best_distance = verdict, distance
        return best

    for dip_hz, borrowed_peak_hz in ((4582.0, 4149.0), (8530.0, 9509.0)):
        stale = any_cuttable_vouches(dip_hz)
        assert stale is not None, "the old rule must accept, or this proves nothing"
        assert stale.freq_hz == borrowed_peak_hz
        assert defect_cuttable_at(verdicts, dip_hz)[0] is None

    # The control: the two rules still agree everywhere the record is not a trap.
    for peak_hz in (5396.0, 1406.0, 2057.0):
        assert any_cuttable_vouches(peak_hz) is defect_cuttable_at(verdicts, peak_hz)[0]


def test_the_response_format_tells_a_prescriber_the_dip_rule_exists():
    """A bar a prescriber cannot read is a bar it will keep walking into.

    The contract has to say three things: dips are barred, cutting one deepens
    it, and the nearest verdict decides — otherwise a model that correctly
    identifies a minimum-phase defect still cannot tell which half of the pair
    it may aim at.
    """
    bar = driver_prescription_response_format()["classification_bar"]

    assert DEFECT_BOOSTABLE in bar["dips_are_barred_too"]
    assert "deepens it" in bar["dips_are_barred_too"]
    assert "NEAREST" in bar["dips_are_barred_too"]


# --------------------------------------------------------------------------- #
# the parked boost class — two gates, each proved by disabling the other
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gain", [0.01, 1.0, 3.0, 12.0])
def test_a_positive_gain_is_refused_at_the_bounds_gate(packet, gain):
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut(gain=gain)], packet))

    assert excinfo.value.reason == dp.BOOST_ROUTE_UNAVAILABLE


def test_the_route_refuses_a_boost_the_bounds_gate_never_saw(packet):
    """The second gate, on a prescription built directly rather than parsed.

    ``read_driver_prescription`` is not the only way a value object comes into
    existence — the durable read-back and a direct construction both bypass it —
    so the promise that a boost can never populate the candidate field has to be
    a property of the SEAM, not of the current call graph.
    """
    accepted = _gate(packet, _document([_cut()], packet))
    boost = DriverPrescription(
        filters=({**dict(accepted.filters[0]), "gain": 2.0},),
        prescription_class="cut",  # laundered
        packet_fingerprint=accepted.packet_fingerprint,
        prescriber_model="m",
        prescriber_operator="o",
        passbands_hz=accepted.passbands_hz,
    )

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        driver_prescription_to_candidate_fields(boost, fitted=None)
    assert excinfo.value.reason == dp.BOOST_ROUTE_UNAVAILABLE

    with pytest.raises(BlendPrescriptionRefused):
        driver_prescription_route(boost)


def test_the_bounds_gate_refuses_a_boost_the_route_never_saw(packet, monkeypatch):
    """The mirror mutation: disable the route and the bounds gate still refuses.

    Together with the test above this is what "two independent gates" means —
    each one is shown to refuse while the other is inert.
    """
    monkeypatch.setattr(
        dp, "driver_prescription_route", lambda p: LINEARIZATION_CANDIDATE_FIELD
    )

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_cut(gain=2.0)], packet))

    assert excinfo.value.reason == dp.BOOST_ROUTE_UNAVAILABLE


# --------------------------------------------------------------------------- #
# emission — the SAME per-branch seam, and the emitter's own re-validation
# --------------------------------------------------------------------------- #


def test_the_candidate_field_is_the_per_branch_one_not_the_shared_stage(packet):
    """Stage P3 rule 3: a per-driver defect gets a per-driver filter.

    A shared filter is the wrong instrument for a one-driver problem and charges
    both branches for it, so this class can land in exactly one field.
    """
    prescription = _gate(packet, _document([_cut()], packet))

    fields = driver_prescription_to_candidate_fields(prescription, fitted=None)

    assert list(fields) == [LINEARIZATION_CANDIDATE_FIELD]
    assert list(fields[LINEARIZATION_CANDIDATE_FIELD]) == ["tweeter"]


def test_the_candidate_field_reduces_to_exactly_the_prescribed_filters(packet):
    """Through the shipped reducer, so the shape cannot be right only here.

    ``linearization_filters_by_role`` is what every rich-candidate call site
    uses to hand the emitter its input; a prescription that did not reduce
    through it would silently emit nothing.
    """
    prescription = _gate(
        packet,
        _document([_cut(), _cut(freq=TWEETER_FEATURE_HZ * 1.05, gain=-2.0)], packet),
    )
    fields = driver_prescription_to_candidate_fields(prescription, fitted=None)

    reduced = linearization_filters_by_role(fields[LINEARIZATION_CANDIDATE_FIELD])

    assert reduced == {"tweeter": [
        {"biquad_type": "Peaking", "freq": TWEETER_FEATURE_HZ, "q": 5.0,
         "gain": -3.0},
        {"biquad_type": "Peaking", "freq": TWEETER_FEATURE_HZ * 1.05, "q": 5.0,
         "gain": -2.0},
    ]}


def test_the_emitters_own_gate_re_validates_and_accepts_the_prescribed_filters(
    packet,
):
    """The emitter never trusts a caller, and this proves it does not have to.

    ``_validated_linearization`` is an independent fail-closed re-validation of
    whatever a persisted candidate claims. A prescription that cleared this
    gate and then failed that one would be a bound stated in only one of the two
    places it has to hold.
    """
    from jasper.active_speaker.profile import ActiveSpeakerPreset

    from tests.test_active_speaker_profile import _two_way_preset

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    prescription = _gate(
        packet, _document([_cut(gain=-DRIVER_MAX_FILTER_CUT_DB, q=DRIVER_MAX_CUT_Q)],
                          packet),
    )
    reduced = linearization_filters_by_role(
        driver_prescription_to_candidate_fields(prescription, fitted=None)[
            LINEARIZATION_CANDIDATE_FIELD
        ]
    )

    safe = camilla_yaml._validated_linearization(preset, reduced)

    assert safe["tweeter"] == [{
        "biquad_type": "Peaking", "freq": TWEETER_FEATURE_HZ,
        "q": DRIVER_MAX_CUT_Q, "gain": -DRIVER_MAX_FILTER_CUT_DB,
    }]


def test_a_one_role_document_leaves_the_other_roles_fitted_filters_alone(packet):
    """MERGE BY ROLE — the ruling, pinned at the layer that implements it.

    The response format promises "a role you do not name is not changed", and
    the blend precedent's wholesale replace would have broken that promise
    silently: a prescriber correcting the tweeter would un-linearize the woofer
    without either of them saying so. The named role's filters are the
    document's; every other role's are the fit's, byte for byte.
    """
    fitted = {
        "woofer": {
            "filters": [{"biquad_type": "Peaking", "freq": 95.0, "q": 3.0,
                         "gain": -4.0}],
            "fit_band_hz": [40.0, 3000.0],
            "residual_rms_db": 0.8,
        },
        "tweeter": {
            "filters": [{"biquad_type": "Peaking", "freq": 7000.0, "q": 2.0,
                         "gain": -1.0}],
            "fit_band_hz": [1600.0, 20000.0],
            "residual_rms_db": 1.1,
        },
    }
    prescription = _gate(packet, _document([_cut()], packet))

    merged = driver_prescription_to_candidate_fields(prescription, fitted=fitted)[
        LINEARIZATION_CANDIDATE_FIELD
    ]

    # The unnamed role is the fit's own record, untouched — residuals and all.
    assert merged["woofer"] == fitted["woofer"]
    # The named role is the document's, and carries no fit-quality claim.
    assert set(merged["tweeter"]) == {"filters", "prescribed_by"}
    assert merged["tweeter"]["filters"][0]["freq"] == TWEETER_FEATURE_HZ
    # Both roles still reduce for the emitter.
    assert set(linearization_filters_by_role(merged)) == {"woofer", "tweeter"}
    assert linearization_filters_by_role(merged)["woofer"][0]["freq"] == 95.0


def test_a_prescription_never_invents_a_role_the_fit_did_not_produce(packet):
    """Merge, not union-with-defaults: a named role the fit skipped still lands.

    The complement of the test above, and the reason the merge is written as
    "fit first, document over the top" rather than as a per-role lookup: a
    document may name a driver the fit declined to correct, and that is an
    ordinary prescription rather than an error.
    """
    prescription = _gate(packet, _document([_cut()], packet))

    merged = driver_prescription_to_candidate_fields(prescription, fitted={})[
        LINEARIZATION_CANDIDATE_FIELD
    ]

    assert list(merged) == ["tweeter"]


def test_the_prescribed_field_enters_at_candidate_build_and_is_tamper_protected(
    packet,
):
    """The reason the seam returns FIELDS rather than stamping a candidate.

    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)`` — a
    content hash the caller cannot set, re-derived on read and refused as
    ``candidate_tampered``. A prescription applied after construction would
    either be invisible to that hash or would break it; entering at build time
    makes a prescribed branch tamper-protected exactly like a fitted one.
    """
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidateError,
    )

    from tests.test_crossover_v2_blend_prescription import _candidate

    prescription = _gate(packet, _document([_cut()], packet))
    fields = driver_prescription_to_candidate_fields(prescription, fitted=None)

    candidate = _candidate(**fields)
    assert candidate.linearization["tweeter"]["filters"][0]["gain"] == -3.0
    assert candidate.linearization["tweeter"]["prescribed_by"]["operator"] == "jasper"

    deeper = _candidate(
        linearization={"tweeter": {"filters": [
            {k: v for k, v in _cut(gain=-9.0).items() if k != "role"}
        ]}}
    )
    assert deeper.fingerprint != candidate.fingerprint

    with pytest.raises(MeasuredCrossoverCandidateError):
        _candidate(linearization={"tweeter": object()})


def test_the_prescribed_branch_names_its_author_without_claiming_to_be_a_fit(
    packet,
):
    """No zeroed residual, no invented fit band — and a marker that says so.

    A record that carried ``fit_band_hz`` and a 0.0 residual would bank a
    fit-quality claim nothing measured, which is the one thing this harness
    exists not to do.
    """
    prescription = _gate(packet, _document([_cut()], packet))

    branch = driver_prescription_to_candidate_fields(prescription, fitted=None)[
        LINEARIZATION_CANDIDATE_FIELD
    ]["tweeter"]

    assert set(branch) == {"filters", "prescribed_by"}
    assert branch["prescribed_by"]["model"] == "claude-opus-5"
    assert branch["prescribed_by"]["packet_fingerprint"] == packet["packet_fingerprint"]


# --------------------------------------------------------------------------- #
# the bounds are the fit engine's own — a pinned lockstep, not a coincidence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("freq_hz", [40.0, 60.0, 100.0, 1000.0, 5000.0, 20000.0])
@pytest.mark.parametrize(("q", "gain_db"), [
    (DRIVER_MAX_CUT_Q, -DRIVER_MAX_FILTER_CUT_DB),
    (DRIVER_MAX_CUT_Q, -DRIVER_MIN_CUT_DB),
    (DRIVER_MIN_Q, -DRIVER_MAX_FILTER_CUT_DB),
])
def test_a_cut_at_every_corner_of_the_envelope_is_a_stable_biquad_at_48_khz(
    freq_hz, q, gain_db
):
    """The low-frequency extreme is NEW here, so the stability claim is too.

    The blend class's region is a few hundred to a few thousand Hz; this class
    reaches a woofer's declared band, where a narrow biquad's poles crowd the
    unit circle — the pole radius of an RBJ peaking section rises toward 1 as
    the centre falls.

    **40 Hz is this sweep's FLOOR, not a bound the class enforces.** Nothing
    here stops a driver declaring lower: the band comes from the driver's own
    ``measurement_band_hz``, and a subwoofer would legitimately declare 20 Hz or
    less. So the number that matters is not the margin at 40 Hz but how it
    SCALES — ``1 - r`` falls roughly in proportion to the centre frequency (the
    section's ``alpha`` is ``sin(w0)/2Q``, and ``sin(w0) ~= w0`` down here), which
    :func:`test_the_pole_radius_margin_scales_with_the_centre_frequency` pins.
    At the worst corner measured — 40 Hz, Q 8, -12 dB — the margin is
    ``1 - r ~= 6.5e-4``, and the proportionality says a 10 Hz declaration would
    still hold ``~1.6e-4``, three orders of magnitude above float64 epsilon.
    The realized magnitude at the centre matches the design exactly throughout.

    Checked on the RBJ coefficients CamillaDSP actually realizes rather than on
    the evaluator alone, because the question is about the filter that runs.
    """
    import math

    import numpy as np

    from jasper.active_speaker.branch_chain import chain_response

    fs = 48_000.0
    amplitude = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / fs
    alpha = math.sin(w0) / (2.0 * q)
    a2 = (1.0 - alpha / amplitude) / (1.0 + alpha / amplitude)
    pole_radius = math.sqrt(abs(a2))

    assert pole_radius < 1.0
    assert 1.0 - pole_radius > 1e-6

    realized = 20.0 * np.log10(np.abs(np.asarray(chain_response(
        [{"biquad_type": "Peaking", "freq": freq_hz, "q": q, "gain": gain_db}],
        np.array([freq_hz]),
    ))))[0]
    assert realized == pytest.approx(gain_db, abs=1e-6)


def test_the_pole_radius_margin_scales_with_the_centre_frequency():
    """The SCALING, because the sweep's 40 Hz floor is not a bound anything enforces.

    A driver declares its own band, so "we measured 40 Hz and it was fine" says
    nothing about the subwoofer somebody declares at 15 Hz. What does carry is
    the proportionality: down here ``sin(w0) ~= w0``, so the section's ``alpha``
    — and with it the margin ``1 - r`` — falls in proportion to the centre.
    Halving the frequency roughly halves the margin, and never collapses it.

    Pinned as a ratio rather than as a table of radii so the claim survives a
    change of sample rate, and so a future edit that inverted the relationship
    fails here rather than in a comment nobody re-derived.
    """
    import math

    def margin(freq_hz: float) -> float:
        amplitude = 10.0 ** (-DRIVER_MAX_FILTER_CUT_DB / 40.0)
        w0 = 2.0 * math.pi * freq_hz / 48_000.0
        alpha = math.sin(w0) / (2.0 * DRIVER_MAX_CUT_Q)
        radius = math.sqrt(abs((1.0 - alpha / amplitude) / (1.0 + alpha / amplitude)))
        return 1.0 - radius

    assert margin(10.0) < margin(20.0) < margin(40.0) < margin(1000.0)
    # Proportional to within a few percent across the decade that matters.
    for lower in (10.0, 20.0, 40.0, 80.0):
        assert margin(2 * lower) / margin(lower) == pytest.approx(2.0, rel=0.02)
    # And even an implausible 10 Hz declaration keeps a workable margin.
    assert margin(10.0) > 1e-4


def test_the_class_size_cap_is_reachable_and_clears_the_largest_honest_document():
    """The two caps do two jobs, and this one has to be able to fire.

    The family's 64 KiB ceiling stops a pathological input being PARSED — it
    must run before the document names its class, so its refusal cannot belong
    to a class vocabulary. This one is a content bound applied once the class is
    known, which is what makes ``driver_prescription_too_large`` a slug the class
    can raise rather than one it only advertises.

    Both directions: the largest document this schema admits must clear it by a
    wide margin, and a document past it must refuse in THIS class's vocabulary
    rather than the blend one.
    """
    largest = json.dumps({
        "artifact_schema_version": DRIVER_PRESCRIPTION_SCHEMA_VERSION,
        "kind": DRIVER_PRESCRIPTION_KIND,
        "packet_fingerprint": "f" * 64,
        "prescriber": {"model": "m" * 64, "operator": "o" * 64},
        "rationale": "r" * 1200,
        "filters": [
            {"role": f"role{r}", "biquad_type": "Peaking",
             "freq": 12345.678, "q": 7.654321, "gain": -11.987654}
            for r in range(4) for _ in range(DRIVER_MAX_FILTERS_PER_ROLE)
        ],
    }, indent=2).encode()

    # The measured figure the constant's own comment quotes, and the margin it
    # claims. Derived here from the schema's constants rather than copied, so a
    # future ceiling that made documents larger fails here rather than silently
    # eating the headroom.
    assert 6_000 <= len(largest) <= 6_100, f"largest honest document: {len(largest)}"
    assert len(largest) * 5 <= dp.DRIVER_PRESCRIPTION_MAX_BYTES, (
        f"the cap must keep a wide margin over the largest honest document "
        f"({len(largest)} bytes)"
    )
    dp.check_driver_document_size(largest)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        dp.check_driver_document_size(b"x" * (dp.DRIVER_PRESCRIPTION_MAX_BYTES + 1))
    assert excinfo.value.reason == dp.DRIVER_PRESCRIPTION_TOO_LARGE
    assert excinfo.value.reason in DRIVER_PRESCRIPTION_REFUSAL_REASONS
    # Exactly at the cap is legal, on the family's inclusive-bounds rule.
    dp.check_driver_document_size(b"x" * dp.DRIVER_PRESCRIPTION_MAX_BYTES)


def test_an_oversized_driver_document_refuses_in_its_own_vocabulary(
    tmp_path, capsys
):
    """End to end through the CLI, because the wiring is the point.

    A slug published to a prescriber in ``refusal_reasons`` and raised by
    nothing is the two-vocabularies failure this module argues against, wearing
    the module's own name.
    """
    session, _ = _bundle(tmp_path / "bundle")
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    (round_dir / "feature_classification.json").write_text(json.dumps(_classification()))
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(_draft()))
    packet = build_crossover_evidence_packet(session, driver_draft_path=draft_path)
    document = _document([_cut()], packet)
    document["rationale"] = "x" * 900  # legal
    blob = json.dumps(document)
    # Pad with whitespace: still valid JSON, still this class, just too big.
    padded = blob[:-1] + " " * (dp.DRIVER_PRESCRIPTION_MAX_BYTES - len(blob) + 8) + "}"
    prescription_path = tmp_path / "p.json"
    prescription_path.write_text(padded)

    code = cli.main([
        "propose", str(session), "--drivers", str(draft_path),
        "--prescription", str(prescription_path), "--json",
    ])

    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_REFUSED
    assert out["reason"] == dp.DRIVER_PRESCRIPTION_TOO_LARGE
    assert out["evidence"]["max_bytes"] == dp.DRIVER_PRESCRIPTION_MAX_BYTES


def test_every_bound_is_the_constant_the_fit_engine_already_emits_up_to():
    """Restated rather than imported, so drift is PINNED rather than inherited.

    This gate is an independent re-validation of a document the fit engine did
    not write, so it must not inherit that engine's policy constant and take a
    future change to it silently. The duplication is the design; this test is
    what stops it becoming a divergence.
    """
    from jasper.active_speaker.linearization_fit import (
        _MIN_FILTER_GAIN_DB,
        _PEAKING_Q_MAX,
    )

    assert DRIVER_MAX_CUT_Q == _PEAKING_Q_MAX
    assert DRIVER_MAX_FILTER_CUT_DB == PER_FILTER_CUT_CAP_DB
    assert DRIVER_MAX_COMPOSED_CUT_DB == MAX_NORMALIZATION_SPEND_DB
    assert DRIVER_MIN_CUT_DB == _MIN_FILTER_GAIN_DB
    assert DRIVER_MAX_FILTERS_PER_ROLE == MAX_FILTERS_PER_DRIVER
    assert DRIVER_MAX_FILTERS_PER_ROLE == (
        camilla_yaml.MAX_LINEARIZATION_FILTERS_PER_DRIVER
    )


def test_the_q_floor_is_the_prescription_familys_not_the_boost_drop_radius():
    """`_PEAKING_Q_MIN` is the #1967 boost-exclusion drop radius, not a cut bound.

    Adopting it here would borrow a number for the one reason it was not chosen,
    and would refuse a broad cut the fit engine's own cut loops already make.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        PRESCRIPTION_MIN_Q,
    )
    from jasper.active_speaker.linearization_fit import _PEAKING_Q_MIN

    assert DRIVER_MIN_Q == PRESCRIPTION_MIN_Q
    assert DRIVER_MIN_Q != _PEAKING_Q_MIN


# --------------------------------------------------------------------------- #
# the schema-version decision, pinned against the rules that decide it
# --------------------------------------------------------------------------- #


def test_a_new_class_does_not_bump_the_blend_classs_schema_version(tmp_path):
    """The rule: bump when an OLDER PRESCRIBER's output would no longer satisfy.

    A blend document written before this class existed is parsed by the same
    reader, against the same bounds, to the same answer. Nothing about its shape
    changed, so nothing about its version does — and versioning the two together
    would force every future change to either class to invalidate the other's
    in-flight documents.
    """
    from tests.test_crossover_v2_blend_prescription import (
        _cut as _blend_cut,
        _document as _blend_document,
        _gate as _blend_gate,
    )

    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)

    assert PRESCRIPTION_SCHEMA_VERSION == 1
    assert DRIVER_PRESCRIPTION_SCHEMA_VERSION == 1
    accepted = _blend_gate(packet, _blend_document([_blend_cut()], packet))
    assert accepted.prescription_class == "cut"


def test_added_packet_blocks_do_not_bump_the_packet_schema_version(packet):
    """The rule: bump when a reader that understood v1 would MISREAD v2.

    Two blocks and one contract were ADDED. Every v1 field is unchanged and a v1
    reader ignores what it does not know, so nothing is misread — the version
    stays where it is rather than invalidating every banked packet.
    """
    assert PACKET_SCHEMA_VERSION == 1
    assert packet["artifact_schema_version"] == 1
    assert packet["response_format"]["artifact_schema_version"] == (
        PRESCRIPTION_SCHEMA_VERSION
    )


def test_an_older_reader_refuses_a_newer_envelope_rather_than_misreading_it(tmp_path):
    """The spool rule, tested as the failure it would be rather than asserted.

    An envelope carrying a per-driver document, handed to a taker that speaks
    only the blend class, must refuse by name. That refusal — not the version
    number — is what makes the shared slot safe, so it is what gets pinned.
    """
    _stage_driver(tmp_path, ordinal=4)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(round_ordinal=4)

    assert excinfo.value.reason == spool.PRESCRIPTION_CLASS_NOT_ACCEPTED
    assert spool.SPOOL_SCHEMA_VERSION == 1


# --------------------------------------------------------------------------- #
# the lifecycle — stage, take, refuse, undo, digest, ordinal
# --------------------------------------------------------------------------- #


def _stage_driver(
    tmp_path: Path,
    *,
    ordinal: int = 4,
    filters: Any = None,
) -> tuple[dict[str, Any], bytes]:
    """One accepted per-driver prescription, banked in a temporary spool."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    packet = _speaker(tmp_path / "bundle")
    document = _document(filters or [_cut()], packet)
    payload = json.dumps(document).encode()
    prescription = _gate(packet, document)
    spool.stage_prescription(payload, prescription, for_round_ordinal=ordinal)
    return packet, payload


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path):
    yield
    spool.set_prescription_spool_path_for_tests(None)


def test_a_staged_per_driver_prescription_round_trips_through_the_one_door(tmp_path):
    packet, payload = _stage_driver(tmp_path, ordinal=4)

    staged = spool.take_staged_prescription(
        round_ordinal=4, accepts=spool.STAGEABLE_KINDS
    )

    assert staged.prescription_kind == DRIVER_PRESCRIPTION_KIND
    assert isinstance(staged.prescription, DriverPrescription)
    assert staged.for_round_ordinal == 4
    assert staged.prescription_sha256 == spool.prescription_sha256(payload)
    assert staged.record()["kind"] == DRIVER_PRESCRIPTION_KIND


def test_the_staged_event_names_the_class_rather_than_twinning_the_event(
    tmp_path, caplog
):
    """One event for "a prescription was staged", extended with the class.

    A second event name would make an operator grepping the journal for staging
    see half of them.
    """
    with caplog.at_level("INFO"):
        _stage_driver(tmp_path)

    line = next(
        r.getMessage() for r in caplog.records
        if "crossover_v2.prescription_staged" in r.getMessage()
    )
    assert f"prescription_kind={DRIVER_PRESCRIPTION_KIND}" in line


def test_a_document_staged_for_another_round_is_refused_by_name(tmp_path):
    _stage_driver(tmp_path, ordinal=4)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=5, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == spool.PRESCRIPTION_NOT_STAGED_FOR_THIS_ROUND


def test_a_hand_edited_document_is_caught_by_the_digest(tmp_path):
    _stage_driver(tmp_path, ordinal=4)
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["gain"] = -20.0
    envelope["document"] = json.dumps(document)
    path.write_text(json.dumps(envelope))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=4, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == spool.SPOOL_MALFORMED


def test_a_document_edited_past_a_bound_is_refused_even_with_a_fresh_digest(tmp_path):
    """The re-run gate, with the anchors the staging step banked."""
    _stage_driver(tmp_path, ordinal=4)
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["gain"] = -DRIVER_MAX_FILTER_CUT_DB - 1.0
    payload = json.dumps(document).encode()
    envelope["document"] = payload.decode()
    envelope["prescription_sha256"] = spool.prescription_sha256(payload)
    path.write_text(json.dumps(envelope))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=4, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == dp.FILTER_CUT_TOO_DEEP


def test_a_filter_moved_off_its_verdict_refuses_on_the_classification_bar(tmp_path):
    """The banked verdicts make the take's bar as strong as the staging step's.

    Only the verdicts that vouched are offered back, so an edit that moves a
    filter onto an unclassified feature refuses on the bar itself — by its own
    name, rather than on a substitute like a missing-evidence slug.
    """
    _stage_driver(tmp_path, ordinal=4)
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    assert envelope["classifications"][0]["hz"] == TWEETER_FEATURE_HZ
    document = json.loads(envelope["document"])
    document["filters"][0]["freq"] = 12_000.0
    payload = json.dumps(document).encode()
    envelope["document"] = payload.decode()
    envelope["prescription_sha256"] = spool.prescription_sha256(payload)
    path.write_text(json.dumps(envelope))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=4, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED


def test_a_refused_document_is_consumed_too(tmp_path):
    """It has had its round; left pending it would refuse every round after it."""
    _stage_driver(tmp_path, ordinal=4)

    with pytest.raises(BlendPrescriptionRefused):
        spool.take_staged_prescription(round_ordinal=4)

    assert spool.staged_prescription_pending() is False


def test_a_taken_prescription_is_never_offered_to_a_second_round(tmp_path):
    _stage_driver(tmp_path, ordinal=4)
    assert spool.take_staged_prescription(
        round_ordinal=4, accepts=spool.STAGEABLE_KINDS
    ) is not None

    assert spool.take_staged_prescription(
        round_ordinal=4, accepts=spool.STAGEABLE_KINDS
    ) is None


def test_a_household_undo_withdraws_a_staged_per_driver_prescription(tmp_path):
    """Same instruction from a different author, derived from the same evidence
    the restore just discarded — so it is withdrawn the same way."""
    _stage_driver(tmp_path, ordinal=4)

    assert spool.withdraw_staged_prescription() is True
    assert spool.staged_prescription_pending() is False
    assert spool.withdraw_staged_prescription() is False


def test_a_withdrawn_document_is_not_filed_among_the_ones_that_ran(tmp_path):
    _stage_driver(tmp_path, ordinal=4)

    spool.withdraw_staged_prescription()

    consumed = spool.prescription_spool_path().with_suffix(".consumed.json")
    assert not consumed.exists()


def test_the_blend_class_still_stages_and_takes_unchanged(tmp_path):
    """The one door carries both, and the older class's default is unchanged.

    A caller that has not learned the per-driver class passes no ``accepts`` and
    keeps working byte-identically.
    """
    from tests.test_crossover_v2_blend_prescription import (
        _cut as _blend_cut,
        _document as _blend_document,
        _gate as _blend_gate,
    )

    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    session, _ = _bundle(tmp_path / "b")
    packet = build_crossover_evidence_packet(session)
    document = _blend_document([_blend_cut()], packet)
    payload = json.dumps(document).encode()
    spool.stage_prescription(
        payload, _blend_gate(packet, document), for_round_ordinal=7
    )

    staged = spool.take_staged_prescription(round_ordinal=7)

    assert staged.prescription_kind == PRESCRIPTION_KIND
    assert staged.prescription.prescription_class == "cut"


def test_an_envelope_written_before_the_class_existed_reads_as_the_blend_one(tmp_path):
    """Absence is the blend class: every envelope predating this change holds one.

    Reading absence as anything else would misdate the corpus.
    """
    from tests.test_crossover_v2_blend_prescription import (
        _cut as _blend_cut,
        _document as _blend_document,
        _gate as _blend_gate,
    )

    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    session, _ = _bundle(tmp_path / "b")
    packet = build_crossover_evidence_packet(session)
    document = _blend_document([_blend_cut()], packet)
    payload = json.dumps(document).encode()
    spool.stage_prescription(
        payload, _blend_gate(packet, document), for_round_ordinal=7
    )
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    del envelope[spool.ENVELOPE_KIND_FIELD]
    path.write_text(json.dumps(envelope))

    staged = spool.take_staged_prescription(round_ordinal=7)

    assert staged.prescription_kind == PRESCRIPTION_KIND


# --------------------------------------------------------------------------- #
# the durable read-back
# --------------------------------------------------------------------------- #


def test_an_accepted_prescription_round_trips_through_the_durable_reader(packet):
    prescription = _gate(packet, _document([_cut()], packet))

    read_back = driver_prescription_from_mapping(prescription.to_dict())

    assert read_back.filters == prescription.filters
    assert read_back.passbands_hz == prescription.passbands_hz
    assert read_back.prescriber_operator == "jasper"


def test_a_mangled_durable_block_reads_as_absent_never_as_half_a_prescription():
    assert driver_prescription_from_mapping(None) is None
    assert driver_prescription_from_mapping({"kind": "nope"}) is None
    assert driver_prescription_from_mapping({}) is None


def test_a_gate_written_class_cannot_launder_a_boost_into_a_cut(packet):
    """The class is re-derived from the gains, never trusted from the document."""
    prescription = _gate(packet, _document([_cut()], packet))
    record = prescription.to_dict()
    record["filters"][0]["gain"] = 2.0

    read_back = driver_prescription_from_mapping(record)

    assert read_back.prescription_class == "boost"
    with pytest.raises(BlendPrescriptionRefused):
        driver_prescription_to_candidate_fields(read_back, fitted=None)


# --------------------------------------------------------------------------- #
# the CLI's one door
# --------------------------------------------------------------------------- #


def test_the_document_names_which_gate_reads_it(tmp_path, capsys, monkeypatch):
    """One door, and the kind is what routes — no flag, no shape inference."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    session, _ = _bundle(tmp_path / "bundle")
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    (round_dir / "feature_classification.json").write_text(
        json.dumps(_classification())
    )
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(_draft()))
    packet = build_crossover_evidence_packet(session, driver_draft_path=draft_path)
    prescription_path = tmp_path / "p.json"
    prescription_path.write_text(json.dumps(_document([_cut()], packet)))

    code = cli.main([
        "propose", str(session), "--drivers", str(draft_path),
        "--prescription", str(prescription_path), "--json",
    ])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["accepted"] is True
    assert list(out["candidate_fields"]) == [LINEARIZATION_CANDIDATE_FIELD]


def test_the_cli_refuses_a_per_driver_document_without_the_drivers_flag(
    tmp_path, capsys
):
    """The packet's honesty rule reaching the operator: no band, named refusal."""
    session, _ = _bundle(tmp_path / "bundle")
    packet = build_crossover_evidence_packet(session)
    prescription_path = tmp_path / "p.json"
    prescription_path.write_text(json.dumps(_document([_cut()], packet)))

    code = cli.main([
        "propose", str(session), "--prescription", str(prescription_path), "--json",
    ])

    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_REFUSED
    assert out["reason"] == dp.PASSBAND_UNAVAILABLE
