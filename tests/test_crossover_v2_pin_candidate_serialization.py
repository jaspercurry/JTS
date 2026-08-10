# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — preserved-behavior pins for candidate serialization.

The strangler migration in #2291 rebuilds the intervention planner around an
immutable ``InterventionProposal`` that carries a complete
:class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate`.
The candidate's own serialization is one of the "strong bones" the issue lists
as preserve-don't-replace, so these pins state the round-trip and identity
properties the new planner has to keep producing.

**Scope: only the gaps.** ``tests/test_active_speaker_measured_crossover_candidate.py``
already pins the field-level story thoroughly — every alignment refusal, the
per-field fingerprint sensitivity for ``program_id`` / ``analysis`` /
``role_attenuations_db`` / ``delay_us`` / ``polarity`` / ``linearization`` /
``linearization_outcome`` / ``exclusion_evidence``, the tamper check, and each
optional field's own era-tolerance round trip. This file adds the three
properties that file does not reach:

1. **``source_preset`` participates in the fingerprint.** It is the only
   member of ``_core()`` with no ``test_fingerprint_changes_with_*`` pin, and
   it is the member that carries the *crossover sections* — the exact identity
   #2291's acceptance criteria name ("Proposal fingerprints change when any
   committed Fc/section/trim/filter/evidence identity changes"). The jts3
   incident this issue is grounded in is a two-Fc defect, so an Fc that can
   move without moving the candidate's identity is precisely the hole the
   migration must not open.
2. **A fully-populated candidate round trips.** The existing round trips vary
   one optional field at a time; a real session's candidate carries alignment,
   linearization, linearization outcome, and exclusion evidence at once.
3. **The round trip survives real JSON text.** ``handle_v2_apply`` reopens the
   candidate through ``_reopen_candidate_artifact``, which is
   ``json.loads(candidate.json)`` — so the live apply path compares a
   text-decoded payload against a freshly-computed ``to_dict()``. Every
   existing round-trip test hands ``from_mapping`` the in-memory dict and never
   crosses that encode/decode boundary.

Values are the jts3 run's own (issue #2291 "Evidence and scope"): the
−13.01298 dB tweeter trim, the 1,648.7 Hz selected candidate against the
2,000 Hz configured session, and ``linearization_outcome="trim_rejected"``.
"""

from __future__ import annotations

import copy
import json

import pytest

from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

# The jts3 run's numbers, so a reader can trace each one back to the incident.
JTS3_CONFIGURED_FC_HZ = 2000.0
JTS3_SELECTED_FC_HZ = 1648.7
JTS3_TWEETER_TRIM_DB = -13.01298


def _preset(*, fc_hz: float = JTS3_SELECTED_FC_HZ, **region: object) -> ActiveSpeakerPreset:
    """A two-way preset whose single crossover region can be varied by field."""

    raw = copy.deepcopy(_two_way_preset())
    raw["crossover_regions"][0]["fc_hz"] = fc_hz
    raw["crossover_regions"][0].update(region)
    return ActiveSpeakerPreset.from_mapping(raw)


def _candidate(
    preset: ActiveSpeakerPreset | None = None, **kwargs: object
) -> MeasuredCrossoverCandidate:
    return MeasuredCrossoverCandidate(
        program_id="prog-abc123",
        analysis={"drift_ppm": 12.5, "sweeps": ["w", "t", "w"]},
        source_preset=preset if preset is not None else _preset(),
        role_attenuations_db={"woofer": 0.0, "tweeter": JTS3_TWEETER_TRIM_DB},
        **kwargs,
    )


def _fully_populated() -> MeasuredCrossoverCandidate:
    """The shape a real session emits: every optional field carries data.

    ``linearization_outcome="trim_rejected"`` is the jts3 value, and the
    alignment is present so the round trip covers the delay/polarity triple
    rather than the ``None``/``None``/``None`` default.
    """

    return _candidate(
        alignment=MeasuredCrossoverAlignment(
            delay_us=245.8, delay_role="tweeter", polarity="invert"
        ),
        linearization={
            "woofer": {
                "filters": [
                    {"f_hz": 347.0, "gain_db": 4.789, "q": 2.0},
                    {"f_hz": 976.0, "gain_db": -6.382, "q": 3.0},
                ],
                "fit_band_hz": [200.0, 1648.7],
                "mic_tier": "reference",
                "n_repeats": 3,
            },
            "tweeter": {"filters": [], "fit_band_hz": [1648.7, 16000.0]},
        },
        linearization_outcome="trim_rejected",
        exclusion_evidence={
            "intervals_hz": [[4000.0, 5600.0]],
            "n_positions": 6,
            "band_spread": {"1000": 1.4, "2000": 2.1},
        },
    )


# --- gap 1: source_preset (and therefore the crossover section) is identity ---


def test_the_fingerprint_moves_when_the_candidates_crossover_section_moves():
    """#2291's headline identity claim, at the candidate boundary.

    The jts3 candidate emitted 1,648.7 Hz while the conductor's own
    ``self._fc_hz`` still read 2,000 Hz. The migration's fix is one
    ``CandidateAcousticContext`` owning both the Fc and the sections, and the
    check that it worked is that the committed Fc is *inside the fingerprint*:
    two candidates that differ only in their crossover section's ``fc_hz`` are
    two different candidates, so a swap cannot pass the ``candidate_tampered``
    gate or the apply endpoint's ``expected_candidate_fingerprint`` review.

    ``source_preset`` is a member of ``_core()`` today, so this passes on
    current main — it is a characterization pin, not a bug report. It is
    written because no existing test asserts it: every OTHER ``_core()``
    member has a ``test_fingerprint_changes_with_*`` in
    ``test_active_speaker_measured_crossover_candidate.py`` and this one does
    not, which is how a refactor could drop the preset from the fingerprinted
    core and take a green suite with it.
    """

    selected = _candidate(_preset(fc_hz=JTS3_SELECTED_FC_HZ))
    configured = _candidate(_preset(fc_hz=JTS3_CONFIGURED_FC_HZ))

    # The two candidates are identical in every other respect — so the
    # fingerprint difference below can only come from the section.
    assert selected.program_id == configured.program_id
    assert selected.analysis == configured.analysis
    assert selected.role_attenuations_db == configured.role_attenuations_db
    assert selected.alignment == configured.alignment
    selected_preset = selected.source_preset.to_dict()
    configured_preset = configured.source_preset.to_dict()
    assert {
        key: value
        for key, value in selected_preset.items()
        if key != "crossover_regions"
    } == {
        key: value
        for key, value in configured_preset.items()
        if key != "crossover_regions"
    }

    assert selected.fingerprint != configured.fingerprint


@pytest.mark.parametrize(
    "region_change",
    [
        pytest.param({"order": 2}, id="filter_order"),
        pytest.param({"upper_polarity": "inverted"}, id="upper_polarity"),
        pytest.param({"delay_target_driver": "tweeter"}, id="delay_target_driver"),
    ],
)
def test_the_fingerprint_moves_when_any_other_committed_section_field_moves(
    region_change: dict[str, object],
):
    """``fc_hz`` is not a special case — the whole section is committed.

    The section's filter order and its polarity/delay routing are as much a
    part of "what graph did we propose" as the corner frequency is, and the
    proposal fingerprint has to cover all of it. Parameterized rather than
    written as one preset mutation so a partial regression (the fingerprint
    still moves for ``fc_hz`` but no longer for ``order``) fails on its own
    row instead of hiding behind a sibling assertion.

    ``target_type`` is deliberately absent: ``SUPPORTED_CROSSOVER_TYPES`` holds
    only ``LinkwitzRiley``, so there is no second valid value to vary and a row
    for it would assert on a construction refusal rather than on identity.
    """

    base = _candidate()
    changed = _candidate(_preset(**region_change))

    assert base.source_preset.to_dict() != changed.source_preset.to_dict()
    assert base.fingerprint != changed.fingerprint


# --- gap 2 + 3: the round trip a real apply performs -------------------------


def test_a_fully_populated_candidate_round_trips_through_from_mapping():
    """Every optional field carries data at once, not one at a time.

    The existing per-field round trips each vary a single optional field over
    an otherwise-default candidate. This is the shape an actual session emits
    — alignment, linearization, outcome, and exclusion evidence together —
    and it is the shape the new ``InterventionProposal`` has to be able to
    carry and re-emit unchanged.
    """

    candidate = _fully_populated()

    reopened = MeasuredCrossoverCandidate.from_mapping(candidate.to_dict())

    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.to_dict() == candidate.to_dict()
    # Field-by-field, so a round trip that silently dropped one optional field
    # and still matched on the others names the field that went missing.
    assert reopened.program_id == candidate.program_id
    assert reopened.analysis == candidate.analysis
    assert reopened.source_preset.to_dict() == candidate.source_preset.to_dict()
    assert reopened.role_attenuations_db == candidate.role_attenuations_db
    assert reopened.alignment.to_dict() == candidate.alignment.to_dict()
    assert reopened.linearization == candidate.linearization
    assert reopened.linearization_outcome == candidate.linearization_outcome
    assert reopened.exclusion_evidence == candidate.exclusion_evidence


def test_a_fully_populated_candidate_round_trips_through_real_json_text():
    """The live apply path's own boundary: ``json.dumps`` → ``json.loads``.

    ``jasper.web.correction_crossover_v2._reopen_candidate_artifact`` reads
    ``candidate.json`` off disk with ``json.loads`` and hands the decoded
    mapping to ``from_mapping``, which compares it against a freshly-computed
    ``to_dict()`` and refuses ``candidate_tampered`` on any difference. So the
    encode/decode step is inside the tamper check's blast radius, and no
    existing test crosses it — they all pass the in-memory dict straight back.

    What this actually pins is that nothing in the candidate survives only as
    a non-JSON type: a float that round trips to a different repr, a tuple
    that decodes as a list, or an ``int``/``float`` distinction the comparison
    would reject. ``-13.01298`` and ``1648.7`` are the jts3 values and are
    exactly the kind of number a naive re-serialization loses.
    """

    candidate = _fully_populated()

    decoded = json.loads(json.dumps(candidate.to_dict()))
    reopened = MeasuredCrossoverCandidate.from_mapping(decoded)

    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.to_dict() == candidate.to_dict()
    # The two numbers the incident turns on survive the text boundary exactly.
    assert reopened.role_attenuations_db["tweeter"] == JTS3_TWEETER_TRIM_DB
    assert (
        reopened.source_preset.crossover_regions[0].fc_hz == JTS3_SELECTED_FC_HZ
    )


def test_a_three_way_candidates_two_sections_both_survive_the_round_trip():
    """"Crossover sections", plural — the case the fixtures never build.

    Every successfully-constructed candidate in
    ``tests/test_active_speaker_measured_crossover_candidate.py`` uses the
    two-way preset: ONE region, two roles. Its three-way fixture appears only
    in a construction-*refusal* test, where the candidate never completes and
    so never reaches ``to_dict()``/``from_mapping()``. Nothing anywhere round
    trips a candidate whose ``crossover_regions`` tuple has length > 1 or
    whose trims cover three roles.

    That matters for #2291 specifically: the planner contract it introduces is
    a ``CandidateAcousticContext`` that "owns the candidate Fc and crossover
    sections together", and #1703 has three-way in view. A serialization bug
    that kept only the first region, or collapsed the tuple, is invisible to
    the current fixtures because they only ever have one region to keep.

    Alignment names the tweeter rather than the mid: the mid belongs to BOTH
    regions, and ``_region_for_role`` fails closed on that ambiguity (already
    pinned by ``test_candidate_rejects_delay_role_ambiguous_across_regions``).
    """

    preset = ActiveSpeakerPreset.from_mapping(copy.deepcopy(_three_way_preset()))
    assert len(preset.crossover_regions) == 2, "the fixture must be multi-section"

    candidate = MeasuredCrossoverCandidate(
        program_id="prog-three-way",
        analysis={"drift_ppm": 12.5},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "mid": -1.25, "tweeter": -6.713},
        alignment=MeasuredCrossoverAlignment(
            delay_us=245.8, delay_role="tweeter", polarity="invert"
        ),
    )

    reopened = MeasuredCrossoverCandidate.from_mapping(
        json.loads(json.dumps(candidate.to_dict()))
    )

    assert reopened.fingerprint == candidate.fingerprint
    assert reopened.to_dict() == candidate.to_dict()
    # Both sections, in order, with their own corner frequencies.
    assert [r.id for r in reopened.source_preset.crossover_regions] == [
        r.id for r in preset.crossover_regions
    ]
    assert [r.fc_hz for r in reopened.source_preset.crossover_regions] == [
        r.fc_hz for r in preset.crossover_regions
    ]
    # All three trims, not just the outer pair.
    assert reopened.role_attenuations_db == {
        "woofer": 0.0, "mid": -1.25, "tweeter": -6.713,
    }


# ``delay_role``'s isolated fingerprint sensitivity was considered here and
# deliberately NOT added. It is the one alignment sub-field without its own
# "only this differs" test, but no mutation isolates it: ``to_dict()`` is
# built from ``_core()``, so anything that drops ``delay_role`` from the
# fingerprint also drops it from the persisted shape, and
# ``test_empty_linearization_is_omitted_from_the_fingerprinted_core`` — which
# hand-builds the whole expected ``_core()`` — already fails on that. A test
# that cannot fail where the suite would not is coverage theatre.
