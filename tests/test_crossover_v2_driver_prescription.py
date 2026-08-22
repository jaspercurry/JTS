# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The per-driver prescription class: full-band, both signs, classify first.

Four things are pinned here and they fail in different ways:

* **the bounds are the fit engine's own** — every CUT ceiling this gate applies
  is numerically the constant ``linearization_fit`` already emits up to, so a
  prescriber can never be granted a move the deterministic path could not make,
  and re-deriving one of them moves both. The two BOOST ceilings are the
  sibling prescription class's instead, and pinned as such;
* **the classification bar is load-bearing** — a filter with no verdict of its
  own sign for its target is refused, and the mutation that removes the check
  makes an accepted document out of a refused one;
* **a boost is admitted only against measured evidence** — a nearest
  ``defect-boostable`` verdict that reported its own depth, bounded by that
  depth and by a composed budget; two independent gates, each proved while
  the other is inert;
* **an accepted prescription reaches the emitted graph through the SAME
  per-branch seam the fit uses**, and survives the emitter's own independent
  re-validation there.

Synthetic bundle on ``tmp_path``, for the reason the blend suite states:
``captures/`` is gitignored and a suite that needed it would only run on one
laptop.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.crossover_v2 import driver_prescription as dp
from jasper.active_speaker.crossover_v2 import prescription_spool as spool
from jasper.active_speaker.crossover_v2.blend_prescription import (
    PRESCRIPTION_KIND,
    PRESCRIPTION_MAX_BYTES,
    PRESCRIPTION_SCHEMA_VERSION,
    BlendPrescriptionRefused,
)
from jasper.active_speaker.crossover_v2.driver_prescription import (
    DRIVER_MAX_COMPOSED_BOOST_DB,
    DRIVER_MAX_COMPOSED_CUT_DB,
    DRIVER_MAX_CUT_Q,
    DRIVER_MAX_FILTER_BOOST_DB,
    DRIVER_MAX_FILTER_CUT_DB,
    DRIVER_MAX_FILTERS_PER_ROLE,
    DRIVER_MIN_BOOST_DB,
    DRIVER_MIN_CUT_DB,
    DRIVER_MIN_Q,
    MAX_SPL_SPEND_BOUND_DB,
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
    LAB_ROW_FIELDS,
    LAB_ROW_NOT_AN_UNCERTAINTY,
    LAB_ROW_UNCERTAINTY,
    ROOM,
    UNCERTAINTY_KINDS,
    UNRESOLVED,
    VERDICT_MATCH_TOLERANCE_OCTAVES,
    defect_boostable_at,
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
        # A lab row carries every column in ``LAB_ROW_FIELDS``; the typed reader
        # takes the seven it needs and the rest ride along, which is what this
        # pair proves.
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


#: The boost fixture's own feature: a tweeter dip, banked with a depth the
#: default boost (+3.0 dB) exactly fits. Distinct from ``TWEETER_FEATURE_HZ``
#: so a document can carry a cut and a boost at once without either borrowing
#: the other's verdict.
TWEETER_DIP_HZ = 6245.0
TWEETER_DIP_DEPTH_DB = 3.0


def _boost(
    role: str = "tweeter",
    gain: float = 3.0,
    freq: float = TWEETER_DIP_HZ,
    q: float = 8.0,
) -> dict[str, Any]:
    return {
        "role": role, "biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain,
    }


def _dip(
    hz: float = TWEETER_DIP_HZ,
    depth_db: float | None = TWEETER_DIP_DEPTH_DB,
    **over: Any,
) -> dict[str, Any]:
    """One banked ``defect-boostable`` row that clears every boost bar."""
    return _verdict(hz, DEFECT_BOOSTABLE, depth_db=depth_db, **over)


def _boostable(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The default classification plus a boostable, depth-carrying dip."""
    return _classification(
        [_verdict(WOOFER_FEATURE_HZ), _verdict(TWEETER_FEATURE_HZ), *(rows or [_dip()])]
    )


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


def _lab_row(hz: float, **over: Any) -> dict[str, Any]:
    """A banked row carrying EVERY column the register enumerates.

    Built FROM :data:`LAB_ROW_FIELDS` rather than spelled out, so it cannot
    become a second, staler list of the row's shape. The values are
    placeholders: what these tests are about is whether the whole row reaches
    the packet intact, not what any one number means. The two the typed reader
    needs to accept a row at all are real.
    """
    row: dict[str, Any] = dict.fromkeys(LAB_ROW_FIELDS, "<placeholder>")
    row["hz"] = hz
    row["classification"] = DEFECT_CUTTABLE
    row.update(over)
    return row


def test_the_packet_carries_the_whole_lab_row_beside_the_gate_view(tmp_path):
    """Both views, neither one standing in for the other.

    ``verdicts[]`` stays exactly the seven keys the register types — that is
    what a gate acts on and widening it would put the classifier's working in
    front of a decision the classifier already made. ``lab_rows[]`` carries the
    artifact's own row, column for column, for a READER auditing how the
    verdict was reached.
    """
    row = _lab_row(WOOFER_FEATURE_HZ)
    packet = _speaker(tmp_path, classification=_classification([row]))
    block = packet["feature_classification"]

    # Imported, not restated: the gate view is whatever the register types.
    assert block["verdicts"] == [read_feature_verdicts([row])[0].to_dict()]
    assert len(block["verdicts"][0]) == 7
    # …and every column of the artifact's own row, unchanged.
    assert block["lab_rows"] == [row]
    assert block["redacted_fields"] == []


def test_a_lab_column_outside_the_allowlist_is_withheld_and_named(tmp_path):
    """The packet's allowlist rule reaches the lab rows too.

    A classification artifact can be an operator's own banked lab result rather
    than this product's output, so an unknown column is copied nowhere and its
    NAME is published — the same posture ``positions[]`` already keeps, and the
    reason ``wav_path`` never reaches a reader from anywhere else in this packet.
    """
    row = _lab_row(WOOFER_FEATURE_HZ)
    path = "/var/lib/jasper/captures/verify-0001.wav"
    packet = _speaker(tmp_path, classification=_classification([
        {**row, "wav_path": path},
    ]))
    block = packet["feature_classification"]

    assert block["lab_rows"] == [row]
    # The NAME is published — that is the point of an allowlist that reports
    # what it dropped — and the VALUE reaches nowhere in the document.
    assert block["redacted_fields"] == ["wav_path"]
    assert path not in json.dumps(packet)


def test_a_row_the_typed_reader_dropped_keeps_its_working_and_reaches_no_gate(
    tmp_path,
):
    """Why a row was dropped is readable; the dropped row still vouches for nothing.

    ``lab_rows`` is the artifact's record and carries a row the typed reader
    refused, which is how a reader sees WHAT was wrong with it. It reaches no
    gate because no gate reads that key — ``packet_feature_classifications`` is
    the one door, and it goes through ``verdicts``.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _lab_row(TWEETER_FEATURE_HZ),
        _lab_row(WOOFER_FEATURE_HZ, classification="   "),
        "not even a row",
    ]))
    block = packet["feature_classification"]

    assert block["n_rows_banked"] == 3
    assert block["n_rows_readable"] == 1
    assert len(block["verdicts"]) == 1
    # Two row OBJECTS were banked; the string is not a row and carries none.
    assert [row["hz"] for row in block["lab_rows"]] == [
        TWEETER_FEATURE_HZ, WOOFER_FEATURE_HZ,
    ]
    assert block["lab_rows"][1]["classification"] == "   "

    classifications = packet_feature_classifications(packet)
    assert classifications is not None
    assert [verdict.freq_hz for verdict in classifications] == [TWEETER_FEATURE_HZ]


def test_every_published_uncertainty_labels_itself_random_or_systematic(packet):
    """The Wave-1 rule, on the block that publishes the numbers it governs.

    Each spread the rows carry says which KIND it is and what it is a spread
    of, and the columns that merely look like one say why they are not. The two
    kinds are never pooled into a single published figure, which is why
    ``gate_slack`` — the larger of a fixed floor and a random 3-sigma — is on
    the second list rather than labelled as either.
    """
    uncertainty = packet["feature_classification"]["uncertainty"]
    fields = uncertainty["fields"]
    not_uncertainties = uncertainty["not_uncertainties"]

    assert fields and not_uncertainties
    for name, entry in fields.items():
        assert name in LAB_ROW_FIELDS, name
        assert entry["kind"] in UNCERTAINTY_KINDS, name
        assert entry["of"].strip(), name
    # Both kinds are live. A vocabulary with one unused half is a vocabulary
    # whose distinction nothing has had to make yet.
    assert {entry["kind"] for entry in fields.values()} == set(UNCERTAINTY_KINDS)

    for name, why in not_uncertainties.items():
        assert name in LAB_ROW_FIELDS, name
        assert why.strip(), name
    assert not set(fields) & set(not_uncertainties)
    assert "gate_slack" in not_uncertainties

    # The packet publishes the register's answer, and cannot be a route to
    # editing it: a caller holding the packet holds a copy.
    fields["excursion_sd_us"]["kind"] = "mutated"
    not_uncertainties["gate_slack"] = "mutated"
    assert LAB_ROW_UNCERTAINTY["excursion_sd_us"]["kind"] != "mutated"
    assert LAB_ROW_NOT_AN_UNCERTAINTY["gate_slack"] != "mutated"


def test_a_non_finite_lab_column_becomes_null_and_is_named(tmp_path):
    """A NaN the instrument really writes must not cost the round its packet.

    ``_compose`` emits ``float("nan")`` for ``z_local`` when a feature's
    neighbourhood scatter is zero and for ``frac_of_nmp`` when the control scale
    is, and ``json.dumps`` banks both verbatim. The packet's fingerprint refuses
    a non-finite number, so a row copied straight through would raise
    ``CrossoverEvidencePacketError`` — no packet at all for a round that
    classified fine. It becomes ``null``, its column is named, and the nested
    per-gate tables are reached too.

    ``clean`` rides along to pin the claim that a BOOLEAN column needs no guard
    of its own: ``bool`` subclasses ``int``, never ``float``, so it is passed
    through rather than mistaken for a number.
    """
    packet = _speaker(tmp_path, classification=_classification([
        _lab_row(
            WOOFER_FEATURE_HZ,
            z_local=float("nan"),
            frac_of_nmp=float("inf"),
            excess_loss_vs_null={"3": float("nan"), "7": 0.0},
            clean=True,
            is_dip=False,
        ),
    ]))
    block = packet["feature_classification"]

    assert block["lab_rows"][0]["z_local"] is None
    assert block["lab_rows"][0]["frac_of_nmp"] is None
    assert block["lab_rows"][0]["excess_loss_vs_null"] == {"3": None, "7": 0.0}
    assert block["lab_rows"][0]["clean"] is True
    assert block["lab_rows"][0]["is_dip"] is False
    assert block["non_finite_fields"] == [
        "excess_loss_vs_null", "frac_of_nmp", "z_local",
    ]
    # …and the document is still exact JSON, which is the whole point.
    assert packet["packet_fingerprint"]


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
    assert bounds["min_boost_db"] == DRIVER_MIN_BOOST_DB
    assert bounds["max_filter_boost_db"] == DRIVER_MAX_FILTER_BOOST_DB
    assert bounds["max_composed_boost_db"] == DRIVER_MAX_COMPOSED_BOOST_DB
    fmt = driver_prescription_response_format()
    # Per SIGN and as a PAIR: a reader walking the keys must find a bar for
    # each sign, not one key that structurally only covers cuts.
    bar = fmt["classification_bar"]
    assert bar["eligible_classification_for_a_cut"] == DEFECT_CUTTABLE
    assert bar["eligible_classification_for_a_boost"] == DEFECT_BOOSTABLE
    assert "eligible_classification" not in bar
    assert fmt["boosts"]["eligible_classification"] == DEFECT_BOOSTABLE
    assert fmt["boosts"]["max_spl_spend_bound_db"] == MAX_SPL_SPEND_BOUND_DB
    # ONE thing, and the matching-sign verdict is NOT it: a cut owes a
    # `defect-cuttable` verdict identically, so the sign rule is stated once for
    # both signs under `classification_bar`. Counting it as a boost's own debt
    # told a prescriber a symmetric bar was asymmetric.
    owed = fmt["boosts"]["a_boost_owes_one_thing_a_cut_does_not"]
    assert "depth_db" in owed
    assert "classification_bar" in owed
    assert set(fmt["refusal_reasons"]) == DRIVER_PRESCRIPTION_REFUSAL_REASONS
    # Every boost refusal the gate can raise is named in the block a prescriber
    # reads, so a bar it can walk into is a bar it was told about.
    assert set(fmt["boosts"]["refusals"]) <= DRIVER_PRESCRIPTION_REFUSAL_REASONS
    assert set(fmt["boosts"]["refusals"]) == {
        dp.FEATURE_NOT_BOOSTABLE,
        dp.FEATURE_DEPTH_UNAVAILABLE,
        dp.BOOST_EXCEEDS_FEATURE_DEPTH,
        dp.BOOST_IN_CROSSOVER_OVERLAP,
        dp.FILTER_BOOST_TOO_HIGH,
        dp.FILTER_BOOST_TOO_SHALLOW,
        dp.COMPOSED_BOOST_EXCEEDED,
        dp.BOOST_UNVOUCHED,
    }
    assert "OVERLAP" in fmt["boosts"]["not_at_the_crossover_knee"].upper()


def test_a_cut_only_document_names_no_role_as_having_spent(tmp_path):
    """`composed_boost_role` is None when nothing rose above unity.

    0.0 dB attributed to a role reads as "the tweeter spent nothing", which is
    a claim; "no role spent" is the fact. Same distinction this record already
    draws between a measured 0.0 and an uncomputed None.
    """
    packet = _speaker(tmp_path, classification=_boostable())
    prescription = _gate(packet, _document([_cut()], packet))

    assert prescription.composed_boost_db == 0.0
    assert prescription.composed_boost_role is None
    assert prescription.to_dict()["composed_boost_role"] is None


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


def test_the_contract_names_the_row_list_a_bar_actually_reads():
    """"the classification block reports…" stopped being unambiguous.

    The block carries two row lists since the packet widened, and BOTH report a
    ``measured_q`` — so an instruction that named only the block left a model to
    guess which one a bar reads. It reads ``verdicts[]``, never the ``lab_rows[]``
    working beside it, and both instruction points say so by name.
    """
    contract = driver_prescription_response_format()

    assert "verdicts[]" in contract["bounds"]["match_a_cut_to_its_feature"]
    assert "verdicts[]" in contract["classification_bar"]["note"]
    assert "lab_rows[]" in contract["classification_bar"]["note"]


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


#: The 2026-08-19 banked record — nine classified features, four of them
#: minimum-phase DIPS, and not one of them carrying a depth. Reproduced as a
#: literal rather than read from ``captures/`` (gitignored: a suite that needed
#: it would run on one laptop).
#:
#: **Every column a BAR reads is here, and that completeness is the point.** An
#: earlier version of this fixture silently dropped one while its comment still
#: said "verbatim", and the omission was not inert: a re-derivation run against
#: it reported the wrong refusal for 1037 Hz, confidently. Columns the reader
#: ignores may be dropped; a column a bar reads may not. The dropped column then
#: was ``vertical_blind``, which no longer exists — the register stopped
#: carrying it when the boost door opened (2026-08-21) — so the record is now
#: complete in four columns rather than five, and the lesson outlives the field.
_BANKED_RECORD = (
    # hz, classification, confidence, measured_q
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


def _banked_rows(**over: Any) -> list[dict[str, Any]]:
    """The real record's nine rows, every column the gate reads included."""
    return [
        _verdict(hz, cls, confidence=conf, measured_q=q, **over)
        for hz, cls, conf, q in _BANKED_RECORD
    ]


def _banked_verdicts():
    return read_feature_verdicts(_banked_rows())


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
    packet = _speaker(tmp_path, classification=_classification(_banked_rows()))
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


def test_the_response_format_tells_a_prescriber_the_sign_rule_exists():
    """A bar a prescriber cannot read is a bar it will keep walking into.

    The contract has to say four things: which verdict admits a cut, which
    admits a boost, what each wrong-sign filter does to its feature, and that
    the nearest verdict decides — otherwise a model that correctly identifies a
    minimum-phase defect still cannot tell which half of the pair it may aim at.
    """
    bar = driver_prescription_response_format()["classification_bar"]
    rule = bar["the_sign_must_match_the_feature"]

    assert DEFECT_CUTTABLE in rule
    assert DEFECT_BOOSTABLE in rule
    assert "deepens it" in rule
    assert "grows it" in rule
    assert "NEAREST" in rule


# --------------------------------------------------------------------------- #
# the boost class — every admission rule, each with the mutation that flips it
# --------------------------------------------------------------------------- #


def test_a_boost_is_admitted_against_a_banked_boostable_dip(tmp_path):
    """The happy path, and the receipt records what admitted it.

    A boost is the one filter this seam carries that spends the household's
    maximum SPL, so the accepted record has to name the verdict, the evaluated
    composed boost, and the ceiling it was measured against — the three numbers
    a reader six weeks later needs to re-derive the decision.
    """
    packet = _speaker(tmp_path, classification=_boostable())

    prescription = _gate(packet, _document([_boost()], packet))

    assert prescription.prescription_class == "boost"
    assert prescription.filters[0]["gain"] == 3.0
    basis = prescription.classification_basis[0]
    assert basis.verdict.classification == DEFECT_BOOSTABLE
    assert basis.verdict.freq_hz == TWEETER_DIP_HZ
    assert basis.verdict.depth_db == TWEETER_DIP_DEPTH_DB
    # The gate's own evaluation of the cascade, banked rather than recomputed.
    assert prescription.composed_boost_db == pytest.approx(3.0, abs=0.01)
    record = prescription.to_dict()
    assert record["composed_boost_db"] == prescription.composed_boost_db
    assert record["max_spl_spend_bound_db"] == MAX_SPL_SPEND_BOUND_DB


def test_a_document_may_carry_a_cut_and_a_boost_and_each_pays_its_own_bar(tmp_path):
    """The class names what a document CAN do; each filter is judged by its sign.

    A prescriber that found a peak and a dip on the same driver should not have
    to split them across two rounds, and neither filter may borrow the other's
    verdict to clear its own bar.
    """
    packet = _speaker(tmp_path, classification=_boostable())

    prescription = _gate(
        packet, _document([_cut(), _boost()], packet)
    )

    assert prescription.prescription_class == "boost"
    assert [b.verdict.classification for b in prescription.classification_basis] == [
        DEFECT_CUTTABLE, DEFECT_BOOSTABLE,
    ]


# --- the classification bar, boost half ------------------------------------- #


def test_a_boost_aimed_at_a_peak_is_refused_by_the_peaks_own_verdict(tmp_path):
    """9509 Hz on the real record is a cuttable PEAK. Boosting one grows it."""
    packet = _speaker(tmp_path, classification=_classification(_banked_rows(depth_db=6.0)))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(freq=9509.0)], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_BOOSTABLE
    assert excinfo.value.evidence["hz"] == 9509.0
    assert excinfo.value.evidence["classification"] == DEFECT_CUTTABLE
    assert "grows it" in excinfo.value.detail


def test_a_boost_may_not_borrow_a_neighbouring_dips_verdict(tmp_path):
    """The mirror of the cut class's borrowed-neighbour bug, on the real record.

    4149 Hz is a cuttable PEAK with the 4582 Hz boostable DIP 0.143 octaves
    away — inside the match tolerance. A rule that let any boostable verdict in
    radius vouch would ACCEPT a boost sitting squarely on the peak, which is the
    precise proposal ``defect_boostable_at``'s nearest-decides rule exists to
    refuse. Same shape at 9509 (peak) beside 8530 (dip).
    """
    packet = _speaker(tmp_path, classification=_classification(_banked_rows(depth_db=6.0)))

    for peak_hz, borrowable_dip_hz in ((4149.0, 4582.0), (9509.0, 8530.0)):
        gap = abs(math.log2(borrowable_dip_hz / peak_hz))
        assert gap < VERDICT_MATCH_TOLERANCE_OCTAVES, "or this proves nothing"
        with pytest.raises(BlendPrescriptionRefused) as excinfo:
            _gate(packet, _document([_boost(freq=peak_hz)], packet))
        assert excinfo.value.reason == dp.FEATURE_NOT_BOOSTABLE
        # The refusal quotes the feature the filter is ON, never the neighbour.
        assert excinfo.value.evidence["hz"] == peak_hz


def test_the_borrowed_neighbour_bar_is_as_strong_at_the_take_as_at_staging(tmp_path):
    """#2752's whole-row banking is load-bearing for BOOSTS too, not only cuts.

    The take re-runs the gate against the verdicts the staging step banked. If
    that set dropped the PEAKS, a boost edited onto 4149 Hz would find only the
    4582 Hz dip, nothing to outrank it, and would be accepted a round after it
    was refused. Banking the whole row set is what makes the two answers equal.
    """
    _stage_driver(
        tmp_path,
        ordinal=2,
        filters=[_boost(freq=4582.0)],
        classification=_classification(_banked_rows(depth_db=6.0)),
    )
    # The edit: move the boost off its dip and onto the peak beside it.
    envelope = json.loads(spool.prescription_spool_path().read_text())
    tampered = json.loads(envelope["document"])
    tampered["filters"][0]["freq"] = 4149.0
    payload = json.dumps(tampered).encode()
    envelope["document"] = payload.decode()
    envelope["prescription_sha256"] = spool.prescription_sha256(payload)
    spool.prescription_spool_path().write_text(json.dumps(envelope))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=2, accepts=frozenset({DRIVER_PRESCRIPTION_KIND}),
        )

    assert excinfo.value.reason == dp.FEATURE_NOT_BOOSTABLE
    assert excinfo.value.evidence["hz"] == 4149.0


def test_a_boost_at_an_unclassified_frequency_is_go_and_measure_not_no(tmp_path):
    """12 kHz is past every banked feature. Different instruction, different slug."""
    packet = _speaker(tmp_path, classification=_boostable())

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(freq=12000.0)], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED
    assert "feeds" in excinfo.value.detail


def test_a_boost_is_admitted_on_the_horizontal_records_own_evidence(tmp_path):
    """The open boost door. Owner ruling, 2026-08-21.

    1037 Hz's real row, from a turntable walk — the capture geometry that used
    to refuse every boost by name (``driver_boost_vertically_blind``). No
    horizontal capture can vouch that a boost generalises off its plane, and
    that is a QUALITY risk: reversible, and measured by the round that follows.
    Component safety is somewhere else entirely and is untouched — the branch
    chain charges this boost its realized peak before the split, and the
    emitted graph is re-proved against that charge
    (``tests/test_active_speaker_linearization_emission.py``).

    So what a boost still owes is MEASUREMENT, and only measurement: a nearest
    boostable dip, and a depth to be bounded by. Give the real row the depth
    the record never carried and the door is open.
    """
    hz, classification, confidence, measured_q = _BANKED_RECORD[0]
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(hz, classification, confidence=confidence, measured_q=measured_q,
                 depth_db=2.0),
    ]))

    gated = _gate(
        packet, _document([_boost(role="woofer", freq=hz, gain=2.0)], packet),
    )

    assert gated.filters[0]["gain"] == 2.0
    assert gated.classification_basis[0].verdict.freq_hz == hz
    assert driver_prescription_route(gated) == LINEARIZATION_CANDIDATE_FIELD


def test_the_row_that_refused_for_blindness_now_refuses_only_for_depth(tmp_path):
    """The disposition that was reported wrong twice, re-derived at HEAD.

    1037.0 Hz refused ``driver_boost_vertically_blind`` while every OTHER row
    on that depthless record refused ``driver_feature_depth_unavailable`` —
    blindness was checked BEFORE depth, and the two send a prescriber to
    different instruments. With the door open the record answers with one
    voice: the only thing between it and a boost is a measurement nobody
    banked. Re-bank with a ``depth_db`` per row and propose again.
    """
    packet = _speaker(tmp_path, classification=_classification(_banked_rows()))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(role="woofer", freq=1037.0)], packet))
    assert excinfo.value.reason == dp.FEATURE_DEPTH_UNAVAILABLE


def test_a_verdict_with_no_depth_refuses_rather_than_guessing_one(tmp_path):
    """The real 2026-08-19 rows verbatim: NOT ONE of them carries a depth.

    This refusal is therefore the operational flag as well as the safety bar —
    it is what the record on disk today produces, and re-banking with a
    ``depth_db`` per row is a measurement session's job rather than this gate's.
    A boost bounded by a guessed depth is a boost bounded by nothing.
    """
    packet = _speaker(tmp_path, classification=_classification(_banked_rows()))
    assert all(v.depth_db is None for v in packet_feature_classifications(packet))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(freq=4582.0)], packet))

    assert excinfo.value.reason == dp.FEATURE_DEPTH_UNAVAILABLE
    assert excinfo.value.evidence["depth_db"] is None


def test_a_boost_may_not_be_deeper_than_the_dip_it_is_aimed_at(tmp_path):
    """The feature is the bound, not the ceiling. 1.46 dB dip, +1.6 dB boost.

    Both numbers are far inside every policy ceiling this gate applies, which is
    the point: the measured depth is a TIGHTER bound than the constants, and it
    is the one that binds here.
    """
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=1.46)]))

    assert _gate(
        packet, _document([_boost(gain=1.46)], packet)
    ).filters[0]["gain"] == 1.46

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(gain=1.6)], packet))

    assert excinfo.value.reason == dp.BOOST_EXCEEDS_FEATURE_DEPTH
    assert excinfo.value.evidence["depth_db"] == 1.46
    assert excinfo.value.evidence["gain_db"] == 1.6


@pytest.mark.parametrize(("depth", "reason"), [
    # A depth that cannot bound anything refuses every boost, rather than
    # admitting one because the subtraction happened to come out favourably.
    (0.0, dp.BOOST_EXCEEDS_FEATURE_DEPTH),
    (-2.0, dp.BOOST_EXCEEDS_FEATURE_DEPTH),
    # `_finite` rejects bool (it is an int in Python) and non-finite values, so
    # both arrive as "no depth reported" rather than as a number.
    (True, dp.FEATURE_DEPTH_UNAVAILABLE),
    (float("inf"), dp.FEATURE_DEPTH_UNAVAILABLE),
    ("3.0", dp.FEATURE_DEPTH_UNAVAILABLE),
])
def test_a_depth_that_cannot_bound_a_boost_refuses_it(tmp_path, depth, reason):
    """Fail-closed on every unusable depth — the bar is never skipped."""
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=depth)]))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(gain=1.0)], packet))

    assert excinfo.value.reason == reason


def test_a_nonsense_depth_is_still_capped_by_the_policy_ceilings(tmp_path):
    """Defence in depth, the other direction: the measured depth is normally
    the TIGHTER bound, but a banked row claiming a 500 dB dip does not widen
    what may be prescribed — the per-filter ceiling and the composed budget
    still bind, so neither bound is trusted on its own."""
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=500.0)]))

    assert _gate(
        packet, _document([_boost(gain=DRIVER_MAX_FILTER_BOOST_DB)], packet)
    ).filters[0]["gain"] == DRIVER_MAX_FILTER_BOOST_DB

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(
            [_boost(gain=DRIVER_MAX_FILTER_BOOST_DB + 0.01)], packet
        ))
    assert excinfo.value.reason == dp.FILTER_BOOST_TOO_HIGH


def test_the_depth_bar_is_load_bearing_not_decorative(tmp_path, monkeypatch):
    """The mutation: strip the depth from the verdicts and the over-deep boost
    is ADMITTED. A bar no mutation can flip is a bar nothing is resting on."""
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=1.46)]))
    document = _document([_boost(gain=1.6)], packet)

    with pytest.raises(BlendPrescriptionRefused):
        _gate(packet, document)

    # The pre-fix shape: a gate that never consulted the measured depth.
    real = dp._boost_basis

    def depth_blind(position, role, freq, gain, verdicts):
        return real(position, role, freq, gain, tuple(
            dataclasses.replace(v, depth_db=max(gain, v.depth_db or 0.0))
            for v in verdicts
        ))

    monkeypatch.setattr(dp, "_boost_basis", depth_blind)
    assert _gate(packet, document).filters[0]["gain"] == 1.6


# --- the shape bounds, boost half ------------------------------------------- #


@pytest.mark.parametrize(("gain", "accepted"), [
    (0.49, False), (0.5, True), (3.0, True), (12.0, True), (12.01, False),
])
def test_the_per_filter_boost_bounds_are_inclusive(tmp_path, gain, accepted):
    """0.5 dB is cosmetic-floor; 12.0 dB is this class's ceiling since R8.

    3.0 stays in the table as an ordinary mid-range case — it was the ceiling
    itself until 2026-08-22, and the row that used to assert 12.0 is REFUSED now
    asserts it is admitted, which is the whole of what R8 changed here.

    12.0 being ACCEPTED is the load-bearing row: it is the rail the emitter
    re-validates against, so a document at the ceiling has to survive emission
    rather than be accepted here and refused downstream. It reaches the composed
    cap at the same time (the two caps are equal), which is why that comparison
    carries ``_COMPOSED_BOOST_EVAL_TOL_DB``.
    """
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=20.0)]))
    document = _document([_boost(gain=gain)], packet)

    if accepted:
        assert _gate(packet, document).filters[0]["gain"] == gain
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == (
        dp.FILTER_BOOST_TOO_SHALLOW if gain < DRIVER_MIN_BOOST_DB
        else dp.FILTER_BOOST_TOO_HIGH
    )


@pytest.mark.parametrize(("q", "accepted"), [(0.5, True), (8.0, True), (8.1, False)])
def test_a_boost_uses_the_same_q_envelope_as_a_cut(tmp_path, q, accepted):
    """A filter's width is a property of the feature, not of the filter's sign."""
    packet = _speaker(tmp_path, classification=_boostable([_dip(depth_db=20.0)]))
    document = _document([_boost(q=q)], packet)

    if accepted:
        assert _gate(packet, document).filters[0]["q"] == q
        return
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, document)
    assert excinfo.value.reason == dp.FILTER_Q_OUT_OF_RANGE


def test_two_admissible_boosts_may_still_compose_past_the_budget(tmp_path):
    """The composed cap, on the EVALUATED cascade, and it is not a sum of gains.

    Two +10.0 dB filters 0.2 octaves apart are each inside the per-filter
    ceiling. At Q 8 they barely interact (11.54 dB composed, admitted); at Q 4
    their skirts overlap enough to reach 14.24 dB and the budget refuses. The
    same document, the same gains, a different width — which is exactly why
    this bound is measured rather than added up.

    Re-scaled from +3.0 dB (3.41 / 4.30 against the old 4.0 dB cap) when R8
    widened the caps; the property under test is unchanged.
    """
    apart = TWEETER_DIP_HZ * 2 ** 0.2
    packet = _speaker(tmp_path, classification=_boostable(
        [_dip(depth_db=20.0), _dip(hz=apart, depth_db=20.0)]
    ))

    wide = _gate(packet, _document(
        [_boost(gain=10.0, q=8.0), _boost(gain=10.0, freq=apart, q=8.0)], packet,
    ))
    assert wide.composed_boost_db == pytest.approx(11.543, abs=0.01)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(
            [_boost(gain=10.0, q=4.0), _boost(gain=10.0, freq=apart, q=4.0)], packet,
        ))

    assert excinfo.value.reason == dp.COMPOSED_BOOST_EXCEEDED
    assert excinfo.value.evidence["composed_boost_db"] == pytest.approx(14.239, abs=0.01)
    assert excinfo.value.evidence["max_composed_boost_db"] == DRIVER_MAX_COMPOSED_BOOST_DB
    # The refusal states the household-facing consequence, not only the bound.
    assert excinfo.value.evidence["max_spl_spend_bound_db"] == MAX_SPL_SPEND_BOUND_DB


def test_the_composed_boost_cap_is_load_bearing_not_decorative(tmp_path, monkeypatch):
    """The mutation: raise the ceiling and the refused document is ADMITTED."""
    apart = TWEETER_DIP_HZ * 2 ** 0.2
    packet = _speaker(tmp_path, classification=_boostable(
        [_dip(depth_db=20.0), _dip(hz=apart, depth_db=20.0)]
    ))
    document = _document(
        [_boost(gain=10.0, q=4.0), _boost(gain=10.0, freq=apart, q=4.0)], packet
    )

    with pytest.raises(BlendPrescriptionRefused):
        _gate(packet, document)

    monkeypatch.setattr(dp, "DRIVER_MAX_COMPOSED_BOOST_DB", 99.0)
    assert _gate(packet, document).composed_boost_db == pytest.approx(14.239, abs=0.01)


def test_the_composed_grid_sees_a_narrow_boost_at_a_wide_bands_edge(tmp_path):
    """SF-GRID. A fixed log grid under-reads a high-Q filter near a wide band's
    top edge, and the bound then reads low because the DECLARATION was wide.

    Two ``+7.0 dB`` Q-8 boosts stacked at 23800 Hz on a driver declared to
    24 kHz compose to a true 14.00 dB — stacking identical filters doubles the
    dB exactly. A fixed log grid under-reads that badly at the band's top edge
    and would ADMIT them; unioning the filter centres reads 14.00 and refuses.
    The emitter always charged the true peak, so the defect was a false
    published spend bound rather than an under-absorption — which is exactly
    why only a test can catch it.

    Re-scaled from ``+3.0 dB`` (a true 6.00 against the old 4.0 dB cap) when R8
    widened the caps; the grid property under test is unchanged.
    """
    wide = _draft()
    wide["driver_safety_profile"]["targets"][1]["measurement_band_hz"] = [
        1000.0, 24000.0,
    ]
    wide["driver_safety_profile"]["targets"][1]["hard_excitation_band_hz"] = [
        900.0, 26000.0,
    ]
    wide["driver_safety_profile"]["targets"][1]["required_protection_filters"] = []
    packet = _speaker(
        tmp_path, draft=wide,
        classification=_boostable([_dip(hz=23800.0, depth_db=20.0)]),
    )
    assert packet_driver_passbands_hz(packet)["tweeter"] == (1000.0, 24000.0)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(
            [_boost(gain=7.0, freq=23800.0), _boost(gain=7.0, freq=23800.0)], packet,
        ))

    assert excinfo.value.reason == dp.COMPOSED_BOOST_EXCEEDED
    assert excinfo.value.evidence["composed_boost_db"] == pytest.approx(14.0, abs=0.01)


#: A MIXED-SIGN cascade whose extremum sits BELOW its own declared band.
#: Six broad boosts at the woofer's 40 Hz floor and two narrow cuts just above
#: it: every filter is in-band and inside every per-filter bound, and the
#: composed extremum lands at ~30.7 Hz, where the woofer branch has no
#: protective high-pass. Eight filters — exactly the per-role ceiling.
#:
#: The boosts were +3.0 dB (composing to 9.75) until R8 widened the composed cap
#: past that; +4.0 dB restores a refusal without touching the shape being tested.
_DOMAIN_ATTACK = [
    {"role": "woofer", "biquad_type": "Peaking", "freq": 40.0, "q": 0.7, "gain": 4.0},
] * 6 + [
    {"role": "woofer", "biquad_type": "Peaking", "freq": 48.0, "q": 2.0, "gain": -12.0},
] * 2

#: The same shape moved into the middle of the band. The ONLY variable is where
#: the extremum lands, which is what makes this a control rather than a second
#: case: it was refused before the domain fix and is refused after it.
_DOMAIN_CONTROL = [
    {"role": "woofer", "biquad_type": "Peaking", "freq": 400.0, "q": 0.7, "gain": 4.0},
] * 6 + [
    {"role": "woofer", "biquad_type": "Peaking", "freq": 480.0, "q": 2.0, "gain": -12.0},
] * 2


def _woofer_dips(freqs):
    return _classification([
        _verdict(hz, DEFECT_BOOSTABLE, depth_db=20.0) for hz in freqs
    ])


@pytest.mark.parametrize(("filters", "freqs", "expected"), [
    (_DOMAIN_ATTACK, (40.0, 48.0), 14.879),
    (_DOMAIN_CONTROL, (400.0, 480.0), 14.884),
])
def test_a_cascade_peaking_outside_its_band_is_still_refused(
    tmp_path, filters, freqs, expected
):
    """SF-DOMAIN. The gate reads the cascade on the span the CHARGE is taken on.

    The emitter charges ``branch_chain_peak_db`` over the whole spectrum, so a
    band-limited reading was measuring a different interval from the one it
    claimed to bound. This attack put every filter inside the woofer's declared
    40-3000 Hz band and inside every per-filter bound, and drove the composed
    extremum to ~30.7 Hz — BELOW the band, where that branch has no protective
    high-pass. The old reading passed it at 3.58 dB against a real 10.75 dB
    charge; both arms now refuse at ~14.88.

    The two arms differ in the fourth significant figure because the extremum
    lands at a different frequency in each, which is the ONE thing this pair
    varies; they are pinned separately rather than to a shared number so that
    difference stays visible.
    """
    packet = _speaker(tmp_path, classification=_woofer_dips(freqs))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document(filters, packet))

    assert excinfo.value.reason == dp.COMPOSED_BOOST_EXCEEDED
    assert excinfo.value.evidence["composed_boost_db"] == pytest.approx(
        expected, abs=0.01
    )


def test_the_terms_the_composed_cap_ignores_are_non_positive(tmp_path):
    """The premise the spend bound rests on, MEASURED rather than assumed.

    `_check_composed` reads the cascade alone. That is an upper bound on the
    emitter's charge only if every term it drops can just subtract — so the
    crossover response is checked across every supported LR order and a range
    of corners, and the trim's own non-positive clamp is pinned by
    `tests/test_crossover_v2_single_datum_owner.py`.

    **The bar is ``<= 1e-9``, not ``<= 0``, and that is deliberate**: a section's
    true maximum is a small POSITIVE number of order 1e-10 dB rather than an
    exact zero. A 40 Hz corner was added when a 600-corner sweep put the worst
    at LR8 / 41.5 Hz lowpass (+4.150e-10 dB) — BELOW the 80 Hz that had been
    this pin's lowest corner, so the sampled set was missing the region that
    maximises the term it exists to bound.
    """
    import numpy as np

    from jasper.active_speaker.branch_chain import (
        CrossoverSection, _evaluation_grid, crossover_response_db,
    )
    from jasper.active_speaker.profile import SUPPORTED_LR_ORDERS

    grid = np.unique(np.concatenate([
        _evaluation_grid([], None), np.geomspace(1.0, 23995.0, 20000),
    ]))
    for order in SUPPORTED_LR_ORDERS:
        for highpass in (True, False):
            for fc in (40.0, 80.0, 400.0, 1600.0, 3000.0, 8000.0):
                worst = float(np.max(crossover_response_db(
                    grid, (CrossoverSection(fc, order, highpass),)
                )))
                assert worst <= 1e-9, f"LR{order} hp={highpass} fc={fc} -> {worst}"


def test_the_composed_grid_change_moved_no_pinned_number(tmp_path):
    """The control for the grid fix: it sharpens a blind spot, it does not
    re-tune the class. Every figure this PR publishes is read off the new grid
    and is the same figure the old grid read."""
    apart = TWEETER_DIP_HZ * 2 ** 0.2
    packet = _speaker(tmp_path, classification=_boostable(
        [_dip(depth_db=20.0), _dip(hz=apart, depth_db=20.0)]
    ))

    single = _gate(packet, _document([_boost()], packet))
    assert single.composed_boost_db == pytest.approx(3.0, abs=0.001)

    pair = _gate(packet, _document([_boost(), _boost(freq=apart)], packet))
    assert pair.composed_boost_db == pytest.approx(3.414, abs=0.001)


def test_the_worst_role_is_the_documents_composed_boost_not_the_last_role(tmp_path):
    """SF-UNPINNED-GUARDS (b). `_check_composed` folds by MAX across roles.

    A last-role-wins fold would report the woofer's smaller boost as the
    document's, under-reporting the spend into the receipt and the round's
    event — a wrong number in the one place a reader goes to see what was
    spent. Roles are walked in sorted order, so the woofer is evaluated LAST
    and a broken fold would report its number.
    """
    # 1200 Hz: inside the woofer's band, below the 1600 Hz overlap floor, and
    # far enough from the fixture's 900 Hz cuttable peak that the nearest-decides
    # rule gives the dip its own frequency.
    packet = _speaker(tmp_path, classification=_boostable([
        _dip(depth_db=20.0), _dip(hz=1200.0, depth_db=20.0),
    ]))

    prescription = _gate(packet, _document(
        [_boost(), _boost(role="woofer", freq=1200.0, gain=0.5)], packet,
    ))

    assert sorted(prescription.roles) == ["tweeter", "woofer"]
    assert prescription.composed_boost_db == pytest.approx(3.0, abs=0.01)
    assert prescription.composed_boost_role == "tweeter"
    assert prescription.to_dict()["composed_boost_role"] == "tweeter"


# --- the crossover knee: a boost may not move the summed response ----------- #


def test_a_boost_in_the_declared_band_overlap_is_refused(tmp_path):
    """The knee ruling (2026-08-19). Both drivers radiate in the overlap, so a
    per-driver boost there moves the SUMMED response the crossover stage owns
    and is charged nothing for it."""
    packet = _speaker(tmp_path, classification=_boostable([
        _dip(hz=2000.0, depth_db=20.0),
    ]))

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(freq=2000.0)], packet))

    assert excinfo.value.reason == dp.BOOST_IN_CROSSOVER_OVERLAP
    # The tweeter's 1.6 kHz protective floor up to the woofer's 3 kHz ceiling,
    # derived from the declared bands the gate already holds.
    assert excinfo.value.evidence["overlap_hz"] == [1600.0, 3000.0]
    assert excinfo.value.evidence["overlap_roles"] == ["tweeter", "woofer"]


def test_a_cut_in_the_overlap_is_untouched_by_the_knee_ruling(tmp_path):
    """Shipped behaviour, deliberately unchanged: a cut past the handoff is
    ordinary useful work, and no round has observed it failing."""
    packet = _speaker(tmp_path, classification=_classification([
        _verdict(2000.0, DEFECT_CUTTABLE),
    ]))

    assert _gate(
        packet, _document([_cut(freq=2000.0)], packet)
    ).filters[0]["freq"] == 2000.0


@pytest.mark.parametrize("freq", [4582.0, 6245.0, 8530.0])
def test_the_knee_ruling_does_not_reach_tonights_targets(tmp_path, freq):
    """The three live 2026-08-19 dips all sit above the 3 kHz overlap ceiling."""
    packet = _speaker(tmp_path, classification=_boostable([
        _dip(hz=freq, depth_db=20.0),
    ]))

    assert _gate(
        packet, _document([_boost(freq=freq)], packet)
    ).filters[0]["freq"] == freq


# --- the route: the SECOND gate, on every value object however it was built -- #


def test_the_route_refuses_a_boost_carrying_no_vouching_verdict(tmp_path):
    """A prescription built directly has no classification basis and no route.

    ``read_driver_prescription`` is not the only way a value object comes into
    existence, so the promise that an unvouched boost can never populate the
    candidate field has to be a property of the SEAM, not of the call graph.
    """
    packet = _speaker(tmp_path, classification=_boostable())
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
    assert excinfo.value.reason == dp.BOOST_UNVOUCHED
    assert excinfo.value.evidence["unvouched"][0]["gain_db"] == 2.0

    with pytest.raises(BlendPrescriptionRefused):
        driver_prescription_route(boost)


def test_a_rehydrated_boost_refuses_because_the_reader_rebuilds_no_basis(tmp_path):
    """The durable read-back applies no bound and reconstructs no verdict.

    That is deliberate — the bounds have one owner and it is the boundary — so
    the route is what stops a banked boost re-entering a candidate through a
    reader that never re-asked the evidence.
    """
    packet = _speaker(tmp_path, classification=_boostable())
    accepted = _gate(packet, _document([_boost()], packet))
    assert accepted.classification_basis  # the gate DID vouch for it

    read_back = driver_prescription_from_mapping(accepted.to_dict())

    assert read_back.prescription_class == "boost"
    assert read_back.classification_basis == ()
    assert read_back.composed_boost_db is None
    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        driver_prescription_to_candidate_fields(read_back, fitted=None)
    assert excinfo.value.reason == dp.BOOST_UNVOUCHED


def test_a_cut_shaped_basis_cannot_vouch_for_a_boost_at_the_route(tmp_path):
    """SF-UNPINNED-GUARDS (a). The route filters the basis by VERDICT CLASS.

    A basis entry exists for every admitted filter, cuts included — so matching
    on `(role, freq)` alone would let a CUT's `defect-cuttable` basis vouch for
    a boost laundered onto the same frequency. The route's
    `verdict.classification == DEFECT_BOOSTABLE` filter is the only thing
    between that document and the candidate field.
    """
    packet = _speaker(tmp_path, classification=_boostable())
    accepted = _gate(packet, _document([_cut()], packet))
    assert accepted.classification_basis[0].verdict.classification == DEFECT_CUTTABLE

    # Same role, same frequency, same (real, gate-written) basis — sign flipped.
    laundered = dataclasses.replace(
        accepted,
        filters=({**dict(accepted.filters[0]), "gain": 2.0},),
        prescription_class="boost",
    )

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        driver_prescription_route(laundered)
    assert excinfo.value.reason == dp.BOOST_UNVOUCHED


def test_a_basis_for_a_different_filter_cannot_vouch_for_this_one(tmp_path):
    """Matching is by ``(role, freq)``, never by position in the list."""
    packet = _speaker(tmp_path, classification=_boostable())
    accepted = _gate(packet, _document([_boost()], packet))
    moved = dataclasses.replace(
        accepted,
        filters=({**dict(accepted.filters[0]), "freq": TWEETER_FEATURE_HZ},),
    )

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        driver_prescription_route(moved)
    assert excinfo.value.reason == dp.BOOST_UNVOUCHED


def test_the_route_is_load_bearing_and_the_classification_bar_is_the_other_gate(
    tmp_path, monkeypatch,
):
    """Two independent gates, each shown refusing while the other is inert.

    Disable the route and the classification bar still refuses an unvouchable
    boost at the boundary; the previous tests disable the bar (by constructing
    around it) and show the route refusing.
    """
    packet = _speaker(tmp_path, classification=_boostable())
    monkeypatch.setattr(
        dp, "driver_prescription_route", lambda p: LINEARIZATION_CANDIDATE_FIELD
    )

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        _gate(packet, _document([_boost(freq=TWEETER_FEATURE_HZ)], packet))

    assert excinfo.value.reason == dp.FEATURE_NOT_BOOSTABLE


def test_an_all_cuts_document_routes_exactly_as_it_did_before_the_boost_class(
    tmp_path,
):
    """The regression that matters most: the cut path is byte-identical.

    A boost route that changed what a cut does would be a hearing-safety change
    smuggled in as a feature addition.
    """
    packet = _speaker(tmp_path, classification=_boostable())
    prescription = _gate(packet, _document([_cut()], packet))

    assert prescription.prescription_class == "cut"
    assert prescription.composed_boost_db == 0.0
    assert driver_prescription_route(prescription) == LINEARIZATION_CANDIDATE_FIELD
    assert driver_prescription_to_candidate_fields(prescription, fitted=None) == {
        LINEARIZATION_CANDIDATE_FIELD: {
            "tweeter": {
                "filters": [{
                    "biquad_type": "Peaking", "freq": TWEETER_FEATURE_HZ,
                    "q": 5.0, "gain": -3.0,
                }],
                "prescribed_by": {
                    "model": "claude-opus-5", "operator": "jasper",
                    "packet_fingerprint": packet["packet_fingerprint"],
                },
            },
        },
    }


# --- the numbers, pinned against the constants they were restored from ------- #


def test_the_boost_ceilings_are_the_emitters_rail_not_the_sibling_classs():
    """LOCKSTEP pin. Ruling **R8**, as an assertion — and the overturn it carries.

    Until 2026-08-22 this test asserted the OPPOSITE, on the owner's 2026-08-18
    ruling that "a new permission should not open at the ceiling of an old one":
    both boost ceilings equalled the blend class's (3.0 / 4.0) and both sat
    strictly under the fit engine's 12 dB rail. R8 overturns that on its own
    terms — the tournament banks a pre-registered expected delta per candidate,
    which is the closed-loop prediction whose absence was the reason to open low.

    So the per-filter ceiling is now the emitter's own rail, which is what makes
    a prescription at the ceiling emittable instead of accepted here and refused
    downstream; and both ceilings have LEFT the blend class, which keeps its own
    3.0 / 4.0 because R8 moved only this class.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        PRESCRIPTION_MAX_FILTER_BOOST_DB,
        PRESCRIPTION_MAX_TOTAL_BOOST_DB,
    )
    from jasper.active_speaker.linearization_fit import PER_FILTER_BOOST_CAP_DB

    assert DRIVER_MAX_FILTER_BOOST_DB == 12.0
    assert DRIVER_MAX_COMPOSED_BOOST_DB == 12.0
    # The per-filter ceiling IS the emitter's rail, in lockstep with both owners
    # of that number, so a document at the ceiling survives emission.
    assert DRIVER_MAX_FILTER_BOOST_DB == camilla_yaml.MAX_LINEARIZATION_BOOST_DB
    assert DRIVER_MAX_FILTER_BOOST_DB == PER_FILTER_BOOST_CAP_DB
    # ...and the blend class did NOT move with it.
    assert PRESCRIPTION_MAX_FILTER_BOOST_DB == 3.0
    assert PRESCRIPTION_MAX_TOTAL_BOOST_DB == 4.0
    assert DRIVER_MAX_FILTER_BOOST_DB > PRESCRIPTION_MAX_FILTER_BOOST_DB
    assert DRIVER_MAX_COMPOSED_BOOST_DB > PRESCRIPTION_MAX_TOTAL_BOOST_DB


def test_the_max_spl_spend_bound_is_derived_from_the_charge_formula():
    """It is a CONSEQUENCE of ``headroom_charge_db``, not a number chosen here.

    ``charge(peak) = peak + HEADROOM_MARGIN_DB`` above the epsilon, and the
    composed cap bounds the peak — so a margin that moved must move this too,
    which is why it is imported rather than restated.

    **Re-proved at 13.0 dB for R8's widened caps.** Each step of the derivation
    the constant's own comment states gets an assertion here, so the published
    number cannot drift from the arithmetic that produced it.
    """
    from jasper.active_speaker.branch_chain import (
        HEADROOM_MARGIN_DB, headroom_charge_db,
    )

    # Step 1: the charge formula is one addition, and nothing else.
    assert HEADROOM_MARGIN_DB == 1.0
    assert headroom_charge_db(7.5) == 7.5 + HEADROOM_MARGIN_DB
    # Step 2: the composed cap bounds an accepted document's peak.
    assert DRIVER_MAX_COMPOSED_BOOST_DB == 12.0
    # Steps 1+2 compose to the published bound, and it lands at 13.0.
    assert MAX_SPL_SPEND_BOUND_DB == DRIVER_MAX_COMPOSED_BOOST_DB + HEADROOM_MARGIN_DB
    assert MAX_SPL_SPEND_BOUND_DB == 13.0
    assert headroom_charge_db(DRIVER_MAX_COMPOSED_BOOST_DB) == MAX_SPL_SPEND_BOUND_DB


def test_the_span_clause_is_what_makes_the_bound_sound():
    """Step 3 of the derivation, the one the whole proof rests on.

    The gate's composed reading is taken on the CHARGE's own span —
    ``branch_chain._evaluation_grid`` IMPORTED, not mirrored — unioned with a
    dense sweep of the role's band. If the gate ever read a NARROWER domain than
    the charge, "peak <= 12" would stop bounding the charge's input and the
    13.0 dB number would be unsound rather than merely loose. That is not
    hypothetical: a band-limited gate once passed a cascade at 3.58 dB that the
    emitter charged 10.75 dB for.

    Pinned two ways: the grid is a superset of the charge's own span, and a
    cascade whose extremum sits OUTSIDE the declared band is still seen.
    """
    from jasper.active_speaker.branch_chain import CHAIN_GRID_HZ, _evaluation_grid
    from jasper.active_speaker.crossover_v2 import driver_prescription as dp

    role_filters = [
        {"type": "Peaking", "freq": 40.0, "q": 0.7, "gain": 3.0},
        {"type": "Peaking", "freq": 48.0, "q": 2.0, "gain": -12.0},
    ]
    grid = dp._composed_grid(role_filters, 40.0, 3000.0)

    # The charge's whole span is inside the gate's grid — every point of it.
    charge_span = _evaluation_grid(role_filters, CHAIN_GRID_HZ)
    assert np.isin(charge_span, grid).all(), (
        "the gate must read at least everywhere the charge does"
    )
    # And the grid reaches outside the declared band, which is the domain half.
    assert grid.min() < 40.0
    assert grid.max() > 3000.0


def test_the_bound_is_attained_by_one_filter_at_the_per_filter_rail():
    """13.0 dB is a tight maximum, not a ceiling nothing reaches.

    One filter at ``DRIVER_MAX_FILTER_BOOST_DB``, at any Q, composes to exactly
    the composed cap and charges exactly the bound. This is what R8 means by the
    worst-case max-SPL spend moving 5 -> 13 dB, and it is only expressible
    because R8 set the two caps equal.
    """
    from jasper.active_speaker.branch_chain import chain_response, headroom_charge_db
    from jasper.active_speaker.crossover_v2 import driver_prescription as dp

    for q in (0.5, 3.0, 8.0):
        one = [{"type": "Peaking", "freq": 6245.0, "q": q,
                "gain": DRIVER_MAX_FILTER_BOOST_DB}]
        grid = dp._composed_grid(one, 1600.0, 20000.0)
        peak = float(np.max(20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(one, grid))), 1e-12)
        )))
        assert peak == pytest.approx(DRIVER_MAX_COMPOSED_BOOST_DB, abs=1e-9)
        assert headroom_charge_db(peak) == pytest.approx(
            MAX_SPL_SPEND_BOUND_DB, abs=1e-9
        )


def test_no_admissible_cascade_charges_more_than_the_published_bound():
    """The safety claim itself, swept rather than argued.

    The four-step derivation says an admitted document cannot be charged past
    ``MAX_SPL_SPEND_BOUND_DB``. This walks random filter sets over the gate's
    OWN rails — 1-4 Peaking filters, gains 0.5 to the per-filter ceiling, Q
    across the whole envelope, centres anywhere in a tweeter's band — keeps the
    ones ``_check_composed`` would admit, and charges each through
    ``headroom_charge_db``.

    Evidence over a sample, not a proof over the space (the derivation is the
    proof); this is what would catch the derivation being wrong. The seed is
    pinned so the two numbers in the constant's own comment are reproducible.
    """
    from jasper.active_speaker.branch_chain import chain_response, headroom_charge_db

    rng = np.random.default_rng(20260822)
    lo, hi = 1600.0, 20000.0
    admitted = 0
    worst = 0.0
    for _ in range(2000):
        filters = [
            {
                "type": "Peaking",
                "freq": float(np.exp(rng.uniform(np.log(lo), np.log(hi)))),
                "q": float(rng.uniform(dp.DRIVER_MIN_Q, dp.DRIVER_MAX_CUT_Q)),
                "gain": float(rng.uniform(
                    dp.DRIVER_MIN_BOOST_DB, DRIVER_MAX_FILTER_BOOST_DB
                )),
            }
            for _i in range(int(rng.integers(1, 5)))
        ]
        grid = dp._composed_grid(filters, lo, hi)
        peak = float(np.max(20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(filters, grid))), 1e-12)
        )))
        if peak > DRIVER_MAX_COMPOSED_BOOST_DB + dp._COMPOSED_BOOST_EVAL_TOL_DB:
            continue  # the gate refuses it, so it is not this bound's problem
        admitted += 1
        worst = max(worst, headroom_charge_db(peak))

    assert admitted == 1538, "the seed moved; re-derive the comment's numbers"
    assert worst == pytest.approx(12.999377, abs=1e-5)
    assert worst <= MAX_SPL_SPEND_BOUND_DB


def test_the_composed_tolerance_absorbs_only_the_evaluators_own_noise():
    """The tolerance R8's equal caps made necessary, bounded at both ends.

    Untolerated, a filter AT the per-filter rail is admitted or refused by the
    biquad's low bits — sign depending on centre and Q — so the published
    ceiling would refuse at its own value. The tolerance must be big enough to
    cover that and far too small to hide a real cascade.

    The residue is a SWEPT figure: worst |residue| 2.416e-13 dB at 2015.4 Hz /
    Q 6.89 over 4 000 random draws at the rail, ~3x the largest value a
    hand-picked grid finds (7.8e-14) and 4 139x under the tolerance. The lower
    bound below is set against the swept number, not the hand-picked one.
    """
    from jasper.active_speaker.crossover_v2 import driver_prescription as dp

    assert dp._COMPOSED_BOOST_EVAL_TOL_DB == 1e-9
    # Covers the SWEPT evaluator residue (2.416e-13) with ~4 orders to spare.
    # The bar sits above that measurement rather than at it, because a search
    # reports a minimum of the worst case, never a maximum.
    assert dp._COMPOSED_BOOST_EVAL_TOL_DB > 1e-11
    # ...and is orders below the 4-decimal precision every charge is published
    # at, so the 13.0 dB bound is unmoved at every digit anyone reads.
    assert dp._COMPOSED_BOOST_EVAL_TOL_DB < 0.5e-4
    assert round(MAX_SPL_SPEND_BOUND_DB + dp._COMPOSED_BOOST_EVAL_TOL_DB, 4) == 13.0


def test_the_boost_floor_is_the_cut_floor_because_it_is_the_same_argument():
    """One literal, two names, asserted AT SOURCE.

    ``_MIN_FILTER_GAIN_DB``'s "inaudible, wastes a filter slot" does not depend
    on the sign, so the pair is DEFINED together rather than restated beside
    each other. An `is` check cannot prove that — CPython interns equal float
    constants, so two independent `= 0.5` literals would also pass it — so the
    source line is what gets read.
    """
    import inspect

    from jasper.active_speaker.linearization_fit import _MIN_FILTER_GAIN_DB

    assert DRIVER_MIN_BOOST_DB == DRIVER_MIN_CUT_DB == _MIN_FILTER_GAIN_DB
    source = inspect.getsource(dp)
    assert "DRIVER_MIN_BOOST_DB = DRIVER_MIN_CUT_DB" in source, (
        "the boost floor must be DEFINED BY the cut floor, not restated as a "
        "second literal that could drift"
    )


def test_defect_boostable_at_is_the_cut_readers_mirror(tmp_path):
    """Same nearest-decides rule, same fail-closed tie, opposite eligible class."""
    verdicts = _banked_verdicts()

    # Nearest decides: the dip owns its own frequency, the peak owns its own.
    assert defect_boostable_at(verdicts, 4582.0)[0].freq_hz == 4582.0
    assert defect_boostable_at(verdicts, 4149.0)[0] is None
    assert defect_boostable_at(verdicts, 4149.0)[1].freq_hz == 4149.0
    assert defect_cuttable_at(verdicts, 4582.0)[0] is None
    # Nothing classified, and a degenerate frequency: a finding, not a raise.
    assert defect_boostable_at(verdicts, 12000.0) == (None, None)
    assert defect_boostable_at(verdicts, -1.0) == (None, None)


def test_an_equidistant_tie_falls_closed_away_from_boostable():
    """Powers of two, so "equidistant" is exact in binary. Row order must not
    decide, and the non-boostable verdict wins the tie either way."""
    rows = [_verdict(1000.0, DEFECT_BOOSTABLE), _verdict(4000.0, DEFECT_CUTTABLE)]

    for ordered in (rows, list(reversed(rows))):
        vouching, nearest = defect_boostable_at(
            read_feature_verdicts(ordered), 2000.0, tolerance_octaves=1.0
        )
        assert vouching is None
        assert nearest.classification == DEFECT_CUTTABLE


def test_the_staged_event_reports_what_the_document_will_spend(tmp_path, caplog):
    """The journal line carries the numbers that decided, not just that a
    document was banked.

    ``boost_filters`` is pinned at a NON-ZERO count on purpose: a hardcoded 0
    satisfies every cut-only staging in the suite, so only a boosting document
    can tell a real count from a constant. Same for the role — a document that
    boosts one branch and cuts another must name the branch that spent.
    """
    apart = TWEETER_DIP_HZ * 2 ** 0.2
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.crossover_v2.prescription_spool"
    ):
        _stage_driver(
            tmp_path,
            ordinal=5,
            filters=[_cut(), _boost(), _boost(freq=apart)],
            classification=_boostable(
                [_dip(depth_db=20.0), _dip(hz=apart, depth_db=20.0)]
            ),
        )

    assert "event=crossover_v2.prescription_staged" in caplog.text
    assert "prescription_class=boost" in caplog.text
    assert "boost_filters=2" in caplog.text
    assert "composed_boost_role=tweeter" in caplog.text
    assert "max_spl_spend_bound_db=13.0" in caplog.text


def test_a_cut_only_staging_reports_no_spend_at_all(tmp_path, caplog):
    """The control: the same line on a cut-only document says zero, not blank."""
    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.crossover_v2.prescription_spool"
    ):
        _stage_driver(tmp_path, ordinal=6, filters=[_cut()])

    assert "prescription_class=cut" in caplog.text
    assert "boost_filters=0" in caplog.text
    assert "composed_boost_db=0.0" in caplog.text
    # A document that spent nothing has no role that spent. Naming one would
    # attribute 0.0 dB to whichever role happened to sort first.
    assert "composed_boost_role=null" in caplog.text


def test_depth_rides_the_banked_rows_end_to_end_with_no_schema_bump(tmp_path):
    """``depth_db`` round-trips artifact → packet → gate → spool → take.

    It is an ordinary optional field on an artifact identified by filename and
    row shape, so nothing versions on it: a row without one reads as ``None``
    and an older reader handed one ignores it.
    """
    packet, _ = _stage_driver(
        tmp_path, ordinal=3, filters=[_boost()], classification=_boostable(),
    )
    dip = next(
        v for v in packet_feature_classifications(packet)
        if v.classification == DEFECT_BOOSTABLE
    )
    assert dip.depth_db == TWEETER_DIP_DEPTH_DB
    assert dip.to_dict()["depth_db"] == TWEETER_DIP_DEPTH_DB
    assert read_feature_verdicts([dip.to_dict()])[0] == dip

    envelope = json.loads(spool.prescription_spool_path().read_text())
    assert any(row["depth_db"] == TWEETER_DIP_DEPTH_DB
               for row in envelope["classifications"])

    taken = spool.take_staged_prescription(
        round_ordinal=3, accepts=frozenset({DRIVER_PRESCRIPTION_KIND}),
    )
    assert taken.prescription.classification_basis[0].verdict.depth_db == (
        TWEETER_DIP_DEPTH_DB
    )


def test_no_bar_reads_a_vertical_blindness_flag_off_a_row(tmp_path):
    """#2783, closed by deletion rather than by a stricter reader.

    The defect was one field name with two producers behind it: this register
    meant the PLANE, and the 2026-08-19 lab tool computed "fewer than two gates
    resolved" — so a horizontal walk banked rows the honest semantic called
    blind, and the boost bar honoured them. With the bar gone the field is gone
    with it, and a row that still carries one is an ignored lab column like the
    thirty others beside it. A boost's admission does not move when it flips.
    """
    sighted, blind = (
        _speaker(
            tmp_path / str(flag),
            classification=_classification([_dip(vertical_blind=flag)]),
        )
        for flag in (False, True)
    )

    for packet in (sighted, blind):
        gated = _gate(packet, _document([_boost()], packet))
        # The submitted gain, unchanged by the flag — which is the whole claim.
        assert gated.filters[0]["gain"] == TWEETER_DIP_DEPTH_DB
    assert not any(
        "vertical" in key
        for key in read_feature_verdicts([_dip()])[0].to_dict()
    )


def test_an_older_reader_tolerates_the_new_field_and_a_row_without_it(tmp_path):
    """Unknown-field tolerance, both directions: the reader takes what it needs.

    A row carrying lab columns it has never heard of reads fine, and so does one
    predating ``depth_db`` — which is every row on disk today.
    """
    rows = read_feature_verdicts([
        {"hz": 900.0, "classification": DEFECT_CUTTABLE, "z_local": 4.2},
        _verdict(1200.0, DEFECT_BOOSTABLE, depth_db=2.0, some_future_column="x"),
    ])

    assert [v.depth_db for v in rows] == [None, 2.0]


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


def test_an_admitted_boost_is_still_charged_and_re_proved_at_the_graph(tmp_path):
    """The open door does not reach past the level bound. It cannot.

    Owner ruling, 2026-08-21: a boost is refused no longer for the capture
    geometry it was measured on. That bar was about whether a correction
    GENERALISES off the horizontal plane — quality, reversible, graded by the
    round that follows. What bounds a driver is a different machine entirely,
    and this walks it end to end on the same document the gate just admitted:

      gate → ``driver_prescription_to_candidate_fields`` → the ``linearization``
      candidate field → the emitter → ``active_baseline_headroom`` → the
      runtime contract's re-derivation off the graph TEXT.

    6245 Hz is one of the three real boosts #2783 named as admitted off the
    banked 2026-08-19 rows. +3.0 dB at Q 8 realizes 2.9699 dB there (it sits
    0.0301 dB down the tweeter's 1600 Hz LR4 high-pass), so the graph
    attenuates the program by that plus ``HEADROOM_MARGIN_DB`` BEFORE the split
    — the boosted graph is never louder at any frequency than the flat one at
    full scale, it reaches full scale at a lower volume setting. The charge is
    computed from the FILTERS, so it cannot be bypassed by how they arrived,
    and the last reader never sees the prescription at all.

    (The excitation ledger is a different subsystem again: it bounds the sweep
    a MEASUREMENT plays, not the EQ an accepted round applies. Neither this
    change nor this test touches it.)
    """
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_baseline_config,
    )

    from tests.test_active_speaker_linearization_emission import (
        ACTIVE_PCM,
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        _active_topology,
        _headroom_gain_db,
        _two_way_preset,
        classify_camilla_graph,
    )

    packet = _speaker(tmp_path, classification=_boostable())
    prescription = _gate(packet, _document([_boost()], packet))
    # Bounded here by the VOUCHED DEPTH, not by the per-filter ceiling: the
    # default banked dip measures 3.0 dB and a boost may not exceed the dip it
    # is aimed at. Those two bounds were the same number until R8 widened the
    # ceiling to 12.0, so this asserts the one that actually binds.
    assert prescription.filters[0]["gain"] == TWEETER_DIP_DEPTH_DB
    assert TWEETER_DIP_DEPTH_DB < DRIVER_MAX_FILTER_BOOST_DB

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    emitted = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization=linearization_filters_by_role(
            driver_prescription_to_candidate_fields(prescription, fitted=None)[
                LINEARIZATION_CANDIDATE_FIELD
            ]
        ),
    )
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    # The boost is PAID for, in maximum SPL, before the split.
    assert _headroom_gain_db(flat) == 0.0
    assert _headroom_gain_db(emitted) == pytest.approx(-3.9699, abs=1e-3)
    # And what it may cost is still the published bound, not the open door.
    assert -_headroom_gain_db(emitted) < MAX_SPL_SPEND_BOUND_DB

    # The re-proof reads the emitted graph and nothing else.
    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=emitted,
    )
    assert graph.allowed is True, graph.issues
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


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
        "rationale": "r" * dp.RATIONALE_MAX_CHARS,
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

    Two blocks and one contract were ADDED, ``feature_classification`` was
    later WIDENED with the classifier's own lab rows beside the gate view it
    already published, ``lateral_poses``/``capture_snr`` were added after that,
    and ``positions`` was then widened with ``cross_seat_sigma``. Every v1 field
    is unchanged in all of those cases and a v1 reader ignores what it does not
    know, so nothing is misread — the version stays where it is rather than
    invalidating every banked packet.

    The widening is the case worth naming, because "the block a v1 reader
    already read grew" sounds like the misreading case and is not one: the seven
    keys of ``verdicts[]`` still say exactly what they said, and a reader that
    never looks at ``lab_rows`` reaches every conclusion it reached before.
    ``positions.cross_seat_sigma`` is the same shape of change — every position
    row, the grid and the flat reference are byte-identical beside it, and
    ``packet_positional_evidence`` reads exactly the three it always read.

    ``positions.angle_deg`` is the OTHER case worth naming, and it is the
    closest call here: its ``reason`` prose changed, from a false corpus-wide
    claim to a true statement about the cloud record's own shape. The FIELD
    still says what it said — ``not_evaluated`` for a cloud row's angle — so a
    v1 reader reaches the same conclusion from it; only the sentence explaining
    why got accurate.
    """
    assert PACKET_SCHEMA_VERSION == 1
    assert packet["artifact_schema_version"] == 1
    assert packet["response_format"]["artifact_schema_version"] == (
        PRESCRIPTION_SCHEMA_VERSION
    )
    assert packet["feature_classification"]["lab_rows"]
    assert len(packet["feature_classification"]["verdicts"][0]) == 7
    assert packet["lateral_poses"]["available"] is False
    assert packet["capture_snr"]["available"] is False
    assert packet["positions"]["angle_deg"]["status"] == "not_evaluated"
    assert packet["positions"]["cross_seat_sigma"]["available"] is True


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
    classification: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """One accepted per-driver prescription, banked in a temporary spool."""
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    packet = _speaker(tmp_path / "bundle", classification=classification)
    document = _document(filters or [_cut()], packet)
    payload = json.dumps(document).encode()
    prescription = _gate(packet, document)
    spool.stage_prescription(
        payload,
        prescription,
        for_round_ordinal=ordinal,
        classifications=packet_feature_classifications(packet),
    )
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


#: A minimum-phase DIP 0.143 octaves above the tweeter's cuttable peak — the
#: 2026-08-19 record's own 4149/4582 gap, rebuilt around this fixture's feature.
#: Inside :data:`VERDICT_MATCH_TOLERANCE_OCTAVES`, so the peak is a verdict the
#: dip can be mistaken for whenever the dip is not in front of the gate.
TWEETER_NEARBY_DIP_HZ = TWEETER_FEATURE_HZ * (2.0 ** 0.143)


def _move_staged_filter(freq_hz: float) -> None:
    """Aim the staged document's first filter somewhere else, digest and all.

    The digest is recomputed because catching this edit is not its job — a
    hand-edit that forgot to is already refused as malformed, and an edit that
    stops at the digest never reaches the bar under test.
    """
    path = spool.prescription_spool_path()
    envelope = json.loads(path.read_text())
    document = json.loads(envelope["document"])
    document["filters"][0]["freq"] = freq_hz
    payload = json.dumps(document).encode()
    envelope["document"] = payload.decode()
    envelope["prescription_sha256"] = spool.prescription_sha256(payload)
    path.write_text(json.dumps(envelope))


def test_the_take_is_offered_the_whole_classification_not_the_vouching_subset(
    tmp_path,
):
    """What is banked is what the staging gate READ, dips included.

    ``classification_basis`` holds only the verdicts that passed the bar, so
    every verdict in it is cuttable by construction — banking that subset would
    drop exactly the dips the nearest-verdict-decides rule needs to say no.
    """
    rows = [
        _verdict(WOOFER_FEATURE_HZ),
        _verdict(TWEETER_FEATURE_HZ),
        _verdict(TWEETER_NEARBY_DIP_HZ, DEFECT_BOOSTABLE),
    ]
    _stage_driver(tmp_path, ordinal=4, classification=_classification(rows))

    banked = json.loads(spool.prescription_spool_path().read_text())

    assert [row["hz"] for row in banked["classifications"]] == [
        row["hz"] for row in rows
    ]


def test_a_filter_moved_onto_a_nearby_dip_refuses_at_take_on_the_dips_own_verdict(
    tmp_path,
):
    """The STRONG claim: the take's bar is the staging step's, not a weaker one.

    A cut honestly aimed at the tweeter's classified peak, moved a seventh of an
    octave onto the minimum-phase dip beside it, is the exact proposal
    ``defect_cuttable_at``'s nearest-verdict rule exists to refuse — and it
    still has a cuttable verdict inside the match radius to borrow. It must
    refuse on the DIP, which it can only do if the dip was banked.
    """
    _stage_driver(
        tmp_path,
        ordinal=4,
        classification=_classification([
            _verdict(WOOFER_FEATURE_HZ),
            _verdict(TWEETER_FEATURE_HZ),
            _verdict(TWEETER_NEARBY_DIP_HZ, DEFECT_BOOSTABLE),
        ]),
    )
    _move_staged_filter(TWEETER_NEARBY_DIP_HZ)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=4, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == dp.FEATURE_NOT_CUTTABLE
    assert excinfo.value.evidence["classification"] == DEFECT_BOOSTABLE
    assert excinfo.value.evidence["hz"] == pytest.approx(TWEETER_NEARBY_DIP_HZ)


def test_a_filter_moved_onto_a_peak_no_filter_targeted_is_admitted_at_take(tmp_path):
    """The other half of the equality claim, and the subset got it wrong too.

    ``classification_basis`` drops every cuttable feature no filter happened to
    aim at, so banking that subset would refuse this one as UNCLASSIFIED — a
    refusal the staging gate, holding the whole artifact, would never have
    given. The take re-runs the bar rather than diffing the document; a document
    that clears the bar clears it, and that is what makes the two answers the
    same answer.
    """
    unclaimed_hz = 6000.0
    assert abs(math.log2(unclaimed_hz / TWEETER_FEATURE_HZ)) > (
        VERDICT_MATCH_TOLERANCE_OCTAVES
    ), "must be out of the prescribed filter's own match radius to discriminate"
    _stage_driver(
        tmp_path,
        ordinal=4,
        classification=_classification([
            _verdict(WOOFER_FEATURE_HZ),
            _verdict(TWEETER_FEATURE_HZ),
            _verdict(unclaimed_hz),
        ]),
    )
    _move_staged_filter(unclaimed_hz)

    staged = spool.take_staged_prescription(
        round_ordinal=4, accepts=spool.STAGEABLE_KINDS
    )

    assert staged.prescription.filters[0]["freq"] == unclaimed_hz
    assert staged.prescription.classification_basis[0].verdict.freq_hz == unclaimed_hz


def test_a_filter_moved_off_its_verdict_refuses_on_the_classification_bar(tmp_path):
    """The far move, refused for the other of the bar's two reasons.

    12 kHz is outside every banked verdict's match radius, so nothing was
    classified there at all — a different instruction to the prescriber than the
    dip above, and the reason the bar returns two slugs rather than a boolean.
    """
    _stage_driver(tmp_path, ordinal=4)
    _move_staged_filter(12_000.0)

    with pytest.raises(BlendPrescriptionRefused) as excinfo:
        spool.take_staged_prescription(
            round_ordinal=4, accepts=spool.STAGEABLE_KINDS
        )

    assert excinfo.value.reason == dp.FEATURE_NOT_CLASSIFIED


def _cli_stage(tmp_path: Path, rows: list[dict[str, Any]]) -> int:
    """Drive the real ``stage`` verb end to end, the way an operator does.

    Not ``_stage_driver``: that calls :func:`stage_prescription` directly, so it
    cannot see whether the CLI hands the spool the verdicts its own gate read.
    Everything here is a path on disk and the only entry point is ``cli.main``.
    """
    spool.set_prescription_spool_path_for_tests(tmp_path / "spool.json")
    session, _ = _bundle(tmp_path / "bundle")
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    (round_dir / "feature_classification.json").write_text(
        json.dumps(_classification(rows))
    )
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(_draft()))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"round_receipt": {"round_ordinal": 3}}))
    # The packet the CLI itself will build, from the same three inputs — a
    # document written against any other one refuses on the fingerprint.
    packet = build_crossover_evidence_packet(
        session, state_path=state, driver_draft_path=draft
    )
    document = tmp_path / "prescription.json"
    document.write_bytes(json.dumps(_document([_cut()], packet)).encode())
    return cli.main([
        "stage", str(session),
        "--state", str(state),
        "--drivers", str(draft),
        "--prescription", str(document),
    ])


def test_the_stage_verb_banks_the_verdicts_its_own_gate_read_dips_included(
    tmp_path, caplog,
):
    """The CLI's half of the anchor, against the 2026-08-19 record itself.

    ``_gate`` reads the classification out of the packet and hands the SAME
    tuple to the spool rather than letting ``stage`` re-read the packet, so what
    is banked cannot be a set the gate never saw. Driven through ``cli.main``
    because that wiring is the subject: a unit test calling
    :func:`stage_prescription` directly passes whatever it passes.

    The four minimum-phase DIPS are the assertion that matters — they are what
    the pre-fix vouching subset dropped, and the cut here (5,396 Hz's peak
    vouches for it) would have banked none of them.
    """
    rows = _banked_rows()

    with caplog.at_level("INFO"):
        exit_code = _cli_stage(tmp_path, rows)

    assert exit_code == cli.EXIT_OK
    banked = json.loads(spool.prescription_spool_path().read_text())
    assert [row["hz"] for row in banked["classifications"]] == [
        row[0] for row in _BANKED_RECORD
    ]
    assert [
        row["hz"] for row in banked["classifications"]
        if row["classification"] == DEFECT_BOOSTABLE
    ] == [1037.0, 4582.0, 6245.0, 8530.0]
    # And the newly-unbounded dimension is visible without opening the file.
    line = next(
        r.getMessage() for r in caplog.records
        if "crossover_v2.prescription_staged" in r.getMessage()
    )
    assert f"classifications={len(_BANKED_RECORD)}" in line


def test_the_banked_classification_stays_far_inside_the_envelope_cap(tmp_path):
    """``SPOOL_MAX_BYTES``' derivation, re-derived rather than trusted.

    The comment on that constant quotes a per-row cost, and a row's cost is the
    length of its own strings rather than a constant — so the number is measured
    HERE, by the route the comment names (stage twice through the real writer,
    diff the bytes on disk), and what is asserted is the conclusion that has to
    hold: the room left over is orders of magnitude past any real artifact.
    """
    record = _banked_rows()
    # The anchor row is in BOTH envelopes, so it cancels exactly and the delta
    # is the record's nine rows and nothing else. It is here because the staged
    # document has to clear the bar in both runs to be staged at all.
    anchor = [_verdict(TWEETER_FEATURE_HZ)]
    _stage_driver(tmp_path / "with", classification=_classification(record + anchor))
    with_rows = spool.prescription_spool_path().stat().st_size
    _stage_driver(tmp_path / "without", classification=_classification(anchor))
    without = spool.prescription_spool_path().stat().st_size

    per_row = (with_rows - without) / len(_BANKED_RECORD)
    # The escaping bound `SPOOL_MAX_BYTES`' own comment derives, so this test
    # moves with that constant instead of restating a byte count.
    headroom = spool.SPOOL_MAX_BYTES - 6 * PRESCRIPTION_MAX_BYTES
    assert 200 <= per_row <= 320, f"per verdict row: {per_row:.1f} bytes"
    assert headroom / per_row > 400, (
        f"room for {headroom / per_row:.0f} rows; the record has "
        f"{len(_BANKED_RECORD)}"
    )
    assert with_rows < spool.SPOOL_MAX_BYTES // 100


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
        payload,
        _blend_gate(packet, document),
        for_round_ordinal=7,
        classifications=None,
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
        payload,
        _blend_gate(packet, document),
        for_round_ordinal=7,
        classifications=None,
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
