# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked 2026-08-10 jts3 crossover incident (#2291), as fixture builders.

The small JSON set under ``tests/fixtures/crossover_v2_incident_20260810/`` is
derived from the gitignored capture bank by
``scripts/derive-crossover-incident-fixture.py``, which has a ``--check`` mode.
The per-driver measured responses were never retained as arrays, so the
branches below are synthetic.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest


from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_db,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session, V2FlowSeams, V2RecordPublishers
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.audio_measurement.program_analysis import (
    DriverResponse,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "crossover_v2_incident_20260810"
ROLES = ("woofer", "tweeter")
SESSION_ID = "cap_test_incident_20260810"
# Enough bins for compose_envelope's grid resampling to have something to work
# with; the same order the conductor's own linearizable fixtures use.
FREQS_HZ = np.linspace(100.0, 20000.0, 2048)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


SESSION_CONTEXT = _fixture("session_context")
CANDIDATE_FIT = _fixture("candidate_fit")
EXPECTED_OUTCOME = _fixture("expected_outcome")

CONFIGURED_FC_HZ = SESSION_CONTEXT["configured_fc_hz"]
SELECTED_FC_HZ = SESSION_CONTEXT["selected_fc_hz"]
COMMITTED_DB = EXPECTED_OUTCOME["committed_attenuations_db"]

# fixture -> production objects


def _session_preset() -> ActiveSpeakerPreset:
    """The preset the SESSION ran, rebuilt from the candidate's own copy.

    The build publishes each candidate with the session preset
    re-cornered at that candidate's Fc (id and ``fc_hz`` are the only fields it
    touches), so the banked candidate preset sits at 1648.7 Hz. Putting the
    corner back at the banked ``configured_fc_hz`` recovers the session's own.
    """
    preset = ActiveSpeakerPreset.from_mapping(CANDIDATE_FIT["source_preset"])
    return replace(preset, crossover_regions=tuple(
        replace(region, fc_hz=CONFIGURED_FC_HZ) for region in preset.crossover_regions
    ))


def _roles_bands() -> list[RoleBand]:
    bands = SESSION_CONTEXT["sweep_band_hz"]
    return [
        RoleBand("woofer", 0, FrequencyBand(*bands["woofer"])),
        RoleBand("tweeter", 1, FrequencyBand(*bands["tweeter"])),
    ]


def _branch_db(role: str) -> np.ndarray:
    """One synthetic measured branch, at the incident's own inter-driver level.

    Flat behind its own committed crossover shape, with the tweeter placed
    exactly ``|committed tweeter trim|`` above the woofer: the offset is read
    off the incident rather than tuned.
    """
    section = CrossoverSection(
        fc_hz=SELECTED_FC_HZ, order=CANDIDATE_FIT["crossover_region"]["order"],
        highpass=role == "tweeter",
    )
    level = abs(float(COMMITTED_DB["tweeter"])) if role == "tweeter" else 0.0
    return level + crossover_response_db(FREQS_HZ, (section,))


def _response(role: str) -> DriverResponse:
    magnitude_db = _branch_db(role)

    def one() -> DriverResponse:
        return DriverResponse(
            role=role, freqs_hz=FREQS_HZ, magnitude_db=magnitude_db,
            complex_tf=(10.0 ** (magnitude_db / 20.0)).astype(complex),
            gating={
                "applied": True,
                "window_ms": SESSION_CONTEXT["capture_context"]["gate_window_ms"],
                "floor_source": SESSION_CONTEXT["capture_context"]["gate_floor_source"],
            },
            snr=None,
            validity_floor_hz=SESSION_CONTEXT["capture_context"]["validity_floor_hz"],
        )

    # 1 primary + 2 repeats clears LINEARIZATION_MIN_PAIRED_OCCURRENCES, the
    # paired-N half of the fit's eligibility gate. The incident's own fits
    # record ``n_repeats`` 2.
    return replace(one(), repeat_responses=(one(), one()))


def _conductor() -> CrossoverV2Session:
    """A conductor at the incident's CONFIGURED corner, with inert seams.

    Nothing here plays, captures, applies or publishes: the replay drives one
    method, and every seam exists only because the constructor wants one.
    """
    seams = V2FlowSeams(
        analyze=lambda *a, **k: None,
        records=V2RecordPublishers(
            check=lambda plan, ambient: None,
            candidate=lambda candidate: None,
        ),
        apply_complete=lambda: False,
        apply_failed=lambda: "",
    )
    return CrossoverV2Session(
        session_id=SESSION_ID,
        source_preset=_session_preset(),
        roles_bands=_roles_bands(),
        fc_hz=CONFIGURED_FC_HZ,
        driver_caps_dbfs={role: 0.0 for role in ROLES},
        session_volume_db=-20.0,
        seams=seams,
        driver_spacing_m=0.15,
        # The incident's own CHECK solve, so the MEASURE program the fit reads
        # its sweep bounds from is composed at construction — the same state a
        # session reaches by walking CHECK, without walking it.
        gain_plan_db=SESSION_CONTEXT["gain_plan_db"],
    )


# the banked record


def test_the_fixture_is_the_incident_as_banked():
    """Guards the fixture itself: these are the numbers #2291 is about.

    A fixture re-derived from a different session fails here before it can
    quietly move a pin.
    """
    fingerprint = "3df7a4da7f33f5dfaa55866334cfaf7ebdb32bfa76dd0405f41fcc8a79d0941d"
    assert CANDIDATE_FIT["fingerprint"] == fingerprint
    assert EXPECTED_OUTCOME["fingerprint"] == fingerprint
    assert EXPECTED_OUTCOME["applied"]["measured_candidate_fingerprint"] == fingerprint
    assert CONFIGURED_FC_HZ == 2000.0
    assert SELECTED_FC_HZ == 1648.7
    assert CANDIDATE_FIT["crossover_region"]["fc_hz"] == SELECTED_FC_HZ
    assert EXPECTED_OUTCOME["linearization_outcome"] == "trim_rejected"
    assert COMMITTED_DB == pytest.approx({"tweeter": -13.012979363787029, "woofer": 0.0})
    # The trim that shipped is the one the household then heard measured back:
    # a failing absolute claim and 7.727 dB of flatness error where 1.5 dB is
    # the tolerance. Banked verbatim — the retained curves are decimated for
    # display and cannot recompute these, so the verdicts travel as scalars.
    post_apply = EXPECTED_OUTCOME["post_apply"]
    assert post_apply["verify_claims"]["absolute"]["status"] == "fail"
    assert post_apply["cloud_flatness"]["passed"] is False
    assert post_apply["cloud_flatness"]["max_db"] > post_apply["cloud_flatness"][
        "tolerance_db"
    ]
    assert EXPECTED_OUTCOME["applied"]["corrections"]["tweeter"]["gain_db"] == pytest.approx(
        COMMITTED_DB["tweeter"]
    )
