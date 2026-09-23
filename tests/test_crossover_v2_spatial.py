# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a banked take records: its identity, its pose, and its curves."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from jasper.audio_measurement.gating import FLOOR_SEARCH_BOUND
from jasper.active_speaker.crossover_v2 import pose_curve, spatial
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KINDS,
    POLARITY_INVERTED,
    REFERENCE_MARK_DESIGN_AXIS,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_EVIDENCE_KIND,
)
from jasper.audio_measurement import gating, program


# the records — the two fields a replay is joined on


def _cloud_record(**overrides):
    """One retained cloud position, with only the field under test named."""
    fields = {
        "position_id": "cloud_measure_03", "phase": PHASE_CLOUD_MEASURE,
        "index": 3, "attempt": 7, "prompt": "stand here", "wide": False,
        "role": "onax",
        "geometry": spatial.PositionGeometry(
            axis=spatial.POSITION_AXIS_HORIZONTAL, degrees=-7,
            mark_distance_m=1.0,
        ),
        "captured_at": 1.0, "session_id": "sess", "gate_window_ms": 12.0,
        "gate_floor_source": "reflection", "gate_disclosure": "a wall",
        "gate_moved_rms_db": 2.59, "gate_reflection_delay_ms": 5.33,
        "gate_entanglement_floor_hz": 1000.0,
        "gate_entanglement_floor_source": gating.ENTANGLEMENT_SOURCE_MEASURED,
        "validity_floor_hz": 100.0, "gating_applied": True,
        "summed_ripple_db": 1.0, "glitch_detected": False, "wav_sha256": "abc",
    }
    return spatial.cloud_position_record(**{**fields, **overrides})


def test_a_position_take_id_is_qualified_by_the_attempt():
    """A geometry retake reuses its position id, so the id alone is not a take.

    The evidence store is write-once, so an unqualified id would make the
    retake's sidecar a PATH_CONFLICT and leave the REPLACED take as the only
    record of a curve that is not in the cloud. Zero-padded so a lexical sort
    of the bundle is also a chronological one.

    Mutation-selected: dropping the attempt suffix left 374 tests green.
    """
    record = _cloud_record()

    assert record["take_id"] == "cloud_measure_03_a07"
    assert record["position_id"] == "cloud_measure_03"


def test_every_take_builder_states_one_identity_under_one_vocabulary():
    """The common core, asserted as a SET rather than key by key.

    A cloud position, a walk pose, an entry baseline and an unprompted-phase
    capture are different captures and their grading columns are never
    meaningful for each other — but the six facts that say WHICH take this is
    are the same question four times, and a reader that had to spell them
    differently per kind is the duplication row 4b names. A builder that spells
    one of these its own way turns this red — which is why the fourth one is
    here rather than pinned apart: the promise this docstring made was empty
    for as long as the tuple below listed only three.
    """
    core = {"phase", "index", "attempt", "take_id", "session_id", "wav_sha256"}
    cloud = _cloud_record()
    pose = _pose_record()
    entry = _entry_record(index=3, attempt=7, session_id="sess", wav_sha256="abc")
    unprompted = _phase_record(
        index=3, attempt=7, session_id="sess", wav_sha256="abc",
    )

    for record in (cloud, pose, entry, unprompted):
        assert core <= set(record)
        assert record["attempt"] == 7
        assert record["session_id"] == "sess"
        assert record["wav_sha256"] == "abc"
        # The verifier is not the index: a take id is derivable from the id and
        # the attempt, and never from the digest.
        assert record["wav_sha256"] not in record["take_id"]
    # Each kind keeps its OWN word for the place it names, which is the
    # role-tagged extension the common core sits under.
    assert cloud["position_id"] == "cloud_measure_03"
    assert pose["pose_id"] == "lateral_03"
    assert "pose_id" not in cloud and "position_id" not in pose


def _pose_record(**overrides):
    """One retained lateral pose, with only the field under test named."""
    fields = {
        "lateral_consumer": "fc_selector",
        "session_id": "sess", "graph_fingerprint": "fp-applied",
        "captured_at": "2026-08-26T00:00:00Z", "wav_sha256": "abc",
    }
    geometry = spatial.PositionGeometry(
        spatial.POSITION_AXIS_HORIZONTAL, -22, spatial.MARK_DISTANCE_M,
        overrides.pop("vertical_deg", 0),
    )
    return spatial.lateral_pose_record(
        spatial.LateralPose(
            pose_id="lateral_03", index=3, attempt=7, prompt="step left",
            role="offax", offset_cm=25.0, at_mark=False, curves=(),
        ),
        geometry=geometry,
        **{**fields, **overrides},
    )


def _entry_record(**overrides):
    """One retained entry-baseline take, with only the field under test named."""
    fields = {
        "index": 9, "attempt": 1, "session_id": "sess", "program_id": "prog",
        "reference_mark": REFERENCE_MARK_DESIGN_AXIS,
        "graph_fingerprint": "fp", "captured_at": "2026-08-11T00:00:00Z",
        "freqs_hz": (200.0, 400.0), "magnitude_db": (-1.5, 0.5),
        "excluded": (True, False),
        "validity_floor_hz": 100.0, "gate_window_ms": 12.0,
        "summed_ripple_db": 1.0, "glitch_detected": False, "wav_sha256": "abc",
    }
    return spatial.entry_baseline_record(**{**fields, **overrides})


def _phase_record(**overrides):
    """One retained unprompted-phase take (CHECK / MEASURE / VERIFY).

    The fourth builder. It joins every family pin below rather than getting
    pins of its own: "does this builder spell the family's vocabulary" is one
    question, and asking it three times while a fourth builder answers
    separately is how the shapes drift apart.
    """
    fields = {
        "phase": PHASE_VERIFY, "index": 3, "attempt": 1, "session_id": "sess",
        "graph_fingerprint": "fp", "captured_at": "2026-08-11T00:00:00Z",
        "wav_sha256": "abc",
    }
    return spatial.phase_capture_record(**{**fields, **overrides})


def test_every_retained_take_kind_states_when_it_was_captured():
    """``captured_at`` on every builder, not on some of them.

    A walk pose was the one retained take carrying no clock, so a banked round
    could say WHERE each capture was taken and in what ORDER the walk served
    them, but never WHEN. Sorting or windowing banked rounds by time had to
    fall back on file mtime, which WO-0 measured actively misrouting.
    """
    for record in (
        _cloud_record(), _pose_record(), _entry_record(), _phase_record(),
    ):
        assert record["captured_at"]


def test_two_walks_at_one_pose_are_told_apart_by_the_applied_candidate():
    """The pose identity cannot separate them; the graph fingerprint can.

    A walk re-run at the same bearing under a different applied candidate mints
    the SAME take id — the id is the pose plus the attempt, and neither moved.
    Without this column the two records are indistinguishable, which is what
    forced a reader wanting to know which graph a walk ran under to open the
    capture-retention ring's sidecar instead of the take itself.
    """
    before = _pose_record(graph_fingerprint="fp-entry")
    after = _pose_record(graph_fingerprint="fp-candidate")

    assert before["take_id"] == after["take_id"]
    assert before["graph_fingerprint"] != after["graph_fingerprint"]


#: Every fact the engine's own record carries
#: (``session.TuningSession._record``), under the name a retained take spells
#: it. A take banked by the flow and a take banked by the engine must be ONE
#: shape: offline re-analysis (ruling S3) reads the bank, and a reader that had
#: to ask which of two shapes it was holding could not run the same analysis
#: over both.
#:
#: One name differs, and only while the flow still publishes through an
#: envelope: the engine's ``kind`` is ``measure_kind`` here, because ``kind`` in
#: a published take is this package's document-type discriminator. See
#: ``spatial._take_identity``.
_ENGINE_RECORD_FIELDS = (
    "session_id", "measure_kind", "baseline_record_id", "position_deg",
    "position_axis", "vertical_deg", "prompt", "candidate_id", "regime",
    "polarity", "level_matched", "graph_fingerprint", "level_db",
    "stimulus_dbfs", "incident", "wav_path",
)


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_every_take_builder_carries_the_whole_engine_record(builder):
    """All of them, on all four — parametrized, because it is one question.

    Six of these were banked by NO builder before this pin: the comparand
    (``baseline_record_id``), which candidate was under test (``candidate_id``),
    which way the driver was wired (``polarity``), the PROVEN fader level
    (``level_db``), the ladder rung (``stimulus_dbfs``) and what went wrong
    (``incident``) — and ``wav_path``, the pointer that lets a banked record
    reach its own capture, was carried by none of the four.

    Presence, not value: what a caller does not state is honestly empty, and
    the fields' contents are each other tests' subject.
    """
    record = builder()

    assert set(_ENGINE_RECORD_FIELDS) <= set(record)


#: The six that no builder banked, each with a value nothing else on a record
#: could be mistaken for. Presence alone would go green against carriers that
#: emitted a constant empty, which is what these are until the retention lift
#: states them.
_STATED_CLAIM = spatial.TakeClaim(
    baseline_record_id="rec-before-7",
    candidate_id="cand-fp-42",
    polarity=POLARITY_INVERTED,
    level_matched=True,
    level_match_trims_db={"tweeter": -9.5},
    level_db=-18.5,
    stimulus_dbfs=-9.0,
    incident="unproven_level",
)


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
@pytest.mark.parametrize(
    "engine_field, stated",
    [
        ("baseline_record_id", "rec-before-7"),
        ("candidate_id", "cand-fp-42"),
        ("polarity", POLARITY_INVERTED),
        ("level_matched", True),
        ("level_match_trims_db", {"tweeter": -9.5}),
        ("level_db", -18.5),
        ("stimulus_dbfs", -9.0),
        ("incident", "unproven_level"),
    ],
)
def test_a_stated_claim_reaches_the_record_it_was_stated_for(
    builder, engine_field, stated,
):
    """Each of the six CARRIES, rather than merely appearing.

    A carrier that emitted a constant empty would satisfy a presence pin
    forever — and these six are inert in production until the retention lift
    binds a claim, so a presence pin is exactly the shape that would rot
    unnoticed. Distinct values per field, so a builder that stamped one of them
    into another's slot is red rather than lucky.

    ``level_db`` and ``stimulus_dbfs`` are two numbers on purpose: a ladder
    moves the stimulus, never the claim.
    """
    record = builder(claim=_STATED_CLAIM)

    assert record[engine_field] == stated


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_a_take_record_does_not_overwrite_the_envelopes_document_type(builder):
    """A retained take is published INSIDE an envelope, and it splats in last.

    ``kind`` in a published take names the DOCUMENT — the same convention every
    other artifact in this package follows — and both readers of a banked take
    refuse anything whose ``kind`` is not ``POSITION_EVIDENCE_KIND``. A record
    that spelled its measure kind ``kind`` would replace the discriminator on
    the way out and make every banked take unreadable, silently: the walk index
    would report a round with no poses rather than an error.
    """
    record = builder()

    # The key itself, not just the surviving value: an emission that wrote the
    # RIGHT word under the WRONG key would leave the envelope below intact and
    # still be the collision this pin exists for.
    assert "kind" not in record
    assert {"schema_version": 1, "kind": POSITION_EVIDENCE_KIND, **record}[
        "kind"
    ] == POSITION_EVIDENCE_KIND


@pytest.mark.parametrize("builder", [_cloud_record, _pose_record, _entry_record, _phase_record])
@pytest.mark.parametrize("kind", ["", *MEASURE_KINDS])
def test_take_kind_is_the_declared_capture_kind(builder, kind):
    record = builder(graph_fingerprint="fp-entry", claim=spatial.TakeClaim(measure_kind=kind))
    assert record["measure_kind"] == kind


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_a_take_carries_the_path_of_the_capture_it_was_reduced_from(builder):
    """The pointer, round-tripped — what makes offline analysis reachable.

    A capture's bundle-relative path is NOT derivable from the take id:
    ``bundles.capture_artifact_relpath`` appends a ``uuid4`` hex, so whoever
    mints the path before the write is the only party that can state it. Banked
    without it, a record names a curve whose audio nothing can find again.
    """
    minted = "captures/summed/cloud_measure_03_a07-9f2c.wav"

    record = builder(claim=spatial.TakeClaim(wav_path=minted))

    assert record["wav_path"] == minted
    # The verifier and the pointer are two facts, not one: the digest says
    # whether the bytes are the right ones, the path says where they are.
    assert record["wav_sha256"] != record["wav_path"]


def test_a_banked_pose_curve_reconstructs_the_complex_transfer_function():
    """Ruling S3's whole point, as arithmetic: PHASE survives the bank.

    ``complex_tf`` had no serializer, so every offline re-analysis re-derived
    phase from the WAVs and the forward model could not run from the bank at
    all.  The pin is the round trip — magnitude and phase back to the complex
    value the transform produced — because a record that banked magnitude and
    a zero, or magnitude and an unwrapped view of the phase, would look
    perfectly well-formed and answer a different question.

    The fixture's phases are deliberately spread outside (-pi, pi] so a
    reconstruction that agreed only on the principal branch fails here.
    """
    import numpy as np

    freqs = np.array([100.0, 1000.0, 5000.0, 12000.0])
    tf = np.array([0.5, 2.0, 0.25, 1.0]) * np.exp(
        1j * np.array([0.3, 3.0, -2.9, 4.2])
    )
    record = pose_curve.pose_curve_record(
        pose_curve.LateralPoseCurve(
            role="woofer", freqs_hz=freqs, complex_tf=tf, band_hz=(80.0, 14000.0),
        )
    )

    assert record["role"] == "woofer"
    assert record["band_hz"] == [80.0, 14000.0]
    assert record["freqs_hz"] == [100.0, 1000.0, 5000.0, 12000.0]

    rebuilt = 10.0 ** (np.asarray(record["magnitude_db"]) / 20.0) * np.exp(
        1j * np.radians(np.asarray(record["phase_deg"]))
    )
    assert np.allclose(rebuilt, tf)


def test_a_banked_pose_curve_floors_a_deep_null_instead_of_banking_minus_infinity():
    """A bin that cancelled to exactly zero is not JSON as ``-inf``.

    Same 1e-12 floor, at the same place in the arithmetic, that
    ``magnitude_response`` applies for the same reason.
    """
    import numpy as np

    record = pose_curve.pose_curve_record(
        pose_curve.LateralPoseCurve(
            role="tweeter",
            freqs_hz=np.array([1000.0]),
            complex_tf=np.array([0.0 + 0.0j]),
            band_hz=(1500.0, 20000.0),
        )
    )

    assert np.isfinite(record["magnitude_db"]).all()


def test_the_pose_record_banks_one_curve_per_driver_it_measured():
    """The pose's OWN curves ride its record — not a re-derivation, and not a
    count the walk bookkeeping supplied separately."""
    import numpy as np

    def _curve(role: str) -> pose_curve.LateralPoseCurve:
        return pose_curve.LateralPoseCurve(
            role=role, freqs_hz=np.array([1000.0]),
            complex_tf=np.array([1.0 + 0.0j]), band_hz=(80.0, 20000.0),
        )

    record = spatial.lateral_pose_record(
        spatial.LateralPose(
            pose_id="lateral_03", index=3, attempt=7, prompt="step left",
            role="offax", offset_cm=25.0, at_mark=False,
            curves=(_curve("woofer"), _curve("tweeter")),
        ),
        geometry=spatial.PositionGeometry(
            spatial.POSITION_AXIS_HORIZONTAL, -22, spatial.MARK_DISTANCE_M,
        ),
        lateral_consumer="fc_selector", session_id="sess",
        graph_fingerprint="fp-applied", captured_at="2026-08-26T00:00:00Z",
        wav_sha256="abc",
    )

    assert [c["role"] for c in record["curves"]] == ["woofer", "tweeter"]
    assert all("phase_deg" in c for c in record["curves"])


@pytest.mark.parametrize("vertical_deg", [0, 20, -20])
def test_a_pose_records_the_elevation_it_was_GIVEN_on_the_horizontal_axis(
    vertical_deg,
):
    """A COMPOUND pose states both numbers, and the axis word does not move.

    ``position_axis`` names the plane a pose's stated BEARING lies in, and
    every pose reaching this builder commands one — ``vertical`` is reserved
    for a pose that commands NO bearing (``PositionGeometry``). So a pose swung
    AND raised stays ``horizontal`` and carries the rise beside the bearing;
    flipping the axis word instead would delete the bearing from the record.

    0 is the honest default rather than an unstated fact: a pose nobody raised
    IS at mark height, which is why the field needs no ``None``.
    """
    record = _pose_record(vertical_deg=vertical_deg)

    assert record["vertical_deg"] == vertical_deg
    assert record["position_deg"] == -22
    assert record["position_axis"] == spatial.POSITION_AXIS_HORIZONTAL
    assert _pose_record()["vertical_deg"] == 0


@pytest.mark.parametrize(
    ("geometry", "fields"),
    [
        (
            spatial.PositionGeometry(spatial.POSITION_AXIS_HORIZONTAL, 7, spatial.MARK_DISTANCE_M),
            {"mark_distance_m": 1.0, "gating_applied": False},
        ),
        (
            spatial.PositionGeometry(
                spatial.POSITION_AXIS_HORIZONTAL, 0, None, kind="seat",
                seat_offset_m=(0.3, 0.0, 0.0),
            ),
            {
                "pose_kind": "seat", "seat_offset_m": [0.3, 0.0, 0.0],
                "mark_distance_m": None, "gating_applied": False,
            },
        ),
        (
            spatial.PositionGeometry(spatial.POSITION_AXIS_HORIZONTAL, 0, 0.3, kind="close"),
            {
                "pose_kind": "close", "seat_offset_m": None,
                "mark_distance_m": 0.3, "gating_applied": False,
            },
        ),
    ],
    ids=["bearing", "seat", "close"],
)
def test_pose_records_keep_distance_and_gating_for_each_coordinate_kind(geometry, fields):
    assert spatial.pose_kind_fields(geometry, gating_applied=False) == fields


def _sweep_program(*segments):
    """A program that declares only the sweep bands under test."""
    return SimpleNamespace(segments=list(segments))


def _sweep_segment(kind: str, role: str | None, f1_hz: float, f2_hz: float):
    return SimpleNamespace(kind=kind, role=role, f1_hz=f1_hz, f2_hz=f2_hz)


def _response(role: str, freqs, tf):
    import numpy as np

    from jasper.audio_measurement.program_analysis import DriverResponse

    return DriverResponse(
        role=role, freqs_hz=np.asarray(freqs, dtype=float),
        magnitude_db=np.zeros(len(freqs)),
        complex_tf=np.asarray(tf, dtype=complex),
        gating={}, snr=None, validity_floor_hz=None,
    )


def _analysis_of_shape(shape: str):
    """One analysis of each shape ``program_analysis`` produces, plus its program.

    Returns the analysis, the program that drove it, and the responses by role
    so a caller can compare a record against the values it came from.
    """
    import numpy as np

    freqs = np.geomspace(20.0, 20000.0, 512)
    tf = (0.3 + np.linspace(0.0, 2.0, 512)) * np.exp(
        1j * np.linspace(-9.0, 9.0, 512)
    )
    if shape == "per_driver":
        bands = {"woofer": (100.0, 6000.0), "tweeter": (2000.0, 20000.0)}
        sources = {
            role: _response(role, freqs, tf * (n + 1))
            for n, role in enumerate(bands)
        }
        analysis = SimpleNamespace(
            driver_responses=tuple(sources.values()), summed_response=None,
        )
        segments = [
            _sweep_segment(program.KIND_SWEEP, role, lo, hi)
            for role, (lo, hi) in bands.items()
        ]
    else:
        bands = {"summed": (30.0, 20000.0)}
        sources = {"summed": replace(_response("summed", freqs, tf), late_energy={
            "t0_ms": 5.0, "early_late_db": 2.2, "energy_db": -21.8, "centroid_ms": 6.6,
        })}
        analysis = SimpleNamespace(
            driver_responses=(), summed_response=sources["summed"],
        )
        segments = [
            _sweep_segment(program.KIND_SUMMED_SWEEP, None, 30.0, 20000.0),
        ]
    return analysis, _sweep_program(*segments), sources, bands


@pytest.mark.parametrize(
    "shape,expected_roles",
    [("per_driver", ["woofer", "tweeter"]), ("summed", ["summed"])],
)
def test_every_shape_of_analysis_banks_its_complex_response(
    shape, expected_roles,
):
    """Bank the response and gate metadata without interpolating samples."""
    import numpy as np

    analysis, prog, sources, bands = _analysis_of_shape(shape)
    for source in sources.values():
        source.gating.update(window_ms=7.0, floor_source=FLOOR_SEARCH_BOUND)

    records = spatial.analysis_curve_records(analysis, prog)

    assert [record["role"] for record in records] == expected_roles
    for record in records:
        source = sources[record["role"]]
        assert record["gate_window_ms"] == 7.0
        assert record["floor_source"] == FLOOR_SEARCH_BOUND
        assert record["late_energy"] == source.late_energy
        rebuilt = 10.0 ** (np.asarray(record["magnitude_db"]) / 20.0) * np.exp(
            1j * np.radians(np.asarray(record["phase_deg"]))
        )
        at = {float(hz): i for i, hz in enumerate(source.freqs_hz)}
        sampled = [at[hz] for hz in record["freqs_hz"]]
        assert np.allclose(rebuilt, np.asarray(source.complex_tf)[sampled])
        assert np.any(np.abs(np.asarray(record["phase_deg"])) > 1.0)
        # The band the PROGRAM declared for that role, not a guessed one.
        assert record["band_hz"] == list(bands[record["role"]])


def test_an_analysis_that_measured_no_response_banks_an_empty_list():
    """CHECK, honestly. It solves gains off pilots and computes no transfer
    function at all, so there is nothing to bank and the record says so rather
    than claiming a clean curve.

    The same answer for a response whose band the program never declared: an
    unbanked curve, not one banked on a guessed band.
    """
    import numpy as np

    check = SimpleNamespace(driver_responses=(), summed_response=None)
    unbanded = SimpleNamespace(
        driver_responses=(
            _response("woofer", np.array([1000.0]), np.array([1.0 + 0.0j])),
        ),
        summed_response=None,
    )
    program_with_bands = _sweep_program(
        _sweep_segment(program.KIND_SWEEP, "tweeter", 300.0, 20000.0),
    )

    assert spatial.analysis_curve_records(check, program_with_bands) == []
    assert spatial.analysis_curve_records(unbanded, program_with_bands) == []


_A_BANKED_CURVE = {
    "role": "summed", "band_hz": [30.0, 20000.0], "freqs_hz": [100.0, 200.0],
    "magnitude_db": [-1.0, 0.5], "phase_deg": [12.5, -170.25],
}


@pytest.mark.parametrize("builder", ["cloud", "entry", "phase"])
def test_the_carry_adds_curves_and_changes_nothing_else(builder):
    """Additive at the builder: one key, and every other field untouched.

    The three kinds that gained the carry spell the SAME key the walk pose
    already did, so the reader the cutover's flip row builds parses one shape
    rather than four. Driven THROUGH the ``curves=`` keyword — a test that
    wrote the key onto a finished dict would pass with the carry reverted.
    """
    build = {
        "cloud": _cloud_record, "entry": _entry_record, "phase": _phase_record,
    }[builder]
    without, carrying = build(), build(curves=[_A_BANKED_CURVE])

    assert carrying["curves"] == [_A_BANKED_CURVE]
    assert without["curves"] == []
    assert {k: v for k, v in without.items() if k != "curves"} == {
        k: v for k, v in carrying.items() if k != "curves"
    }


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_an_unmatched_take_states_no_level_match_trims_at_all(builder):
    """Additive at the builder, on ``vertical_deg``'s terms: the numbers key
    is ABSENT on a take that declared no level match, so a record banked before
    this existed and one banked by an unmatched take are the same shape. An
    empty mapping would be a third state a reader has to interpret.
    """
    without = builder()
    carrying = builder(claim=spatial.TakeClaim(
        level_matched=True, level_match_trims_db={"tweeter": -9.5},
    ))

    assert without["level_matched"] is False
    assert "level_match_trims_db" not in without
    assert carrying["level_match_trims_db"] == {"tweeter": -9.5}
    assert {
        k: v for k, v in without.items() if k != "level_matched"
    } == {
        k: v for k, v in carrying.items()
        if k not in ("level_matched", "level_match_trims_db")
    }


@pytest.mark.parametrize(
    "analysis_phase, composed, protection_emitted, stamped",
    [
        (program.PROGRAM_PHASE_MEASURE, True, True, "crossover_composed"),
        (program.PROGRAM_PHASE_MEASURE, False, True, "protection_retained"),
        # Nothing was emitted for the curve to retain.
        (program.PROGRAM_PHASE_MEASURE, False, False, None),
        # The analyzer that composes never ran, so the flag is a default.
        (program.PROGRAM_PHASE_VERIFY, False, True, None),
    ],
)
def test_a_take_states_which_phase_composition_its_curves_carry(
    analysis_phase, composed, protection_emitted, stamped,
):
    """docs/tuning-methodology.md §4 step 1's question, answered by the record.

    A MEASURE analysis either divided the emitted protection out and multiplied
    the configured crossover in or it did not, and the phase COMMANDED does not
    say which — a lateral walk runs the same analyzer without the priors that
    compose. So the fact is read off the analysis and stamped, and the delay
    proposal echoes it instead of the operator stating it by hand.

    Three-valued on purpose, and ABSENT on ``level_match_trims_db``'s terms for
    the third: a capture whose analyzer never composes and a box that emitted
    no protection would both otherwise read as ``protection_retained``, which
    is a contamination claim that is untrue of either.
    """
    record = _phase_record(
        phase=analysis_phase,
        claim=spatial.TakeClaim(
            phase_composition=spatial.phase_composition(
                SimpleNamespace(
                    phase=analysis_phase, configured_path_composed=composed,
                ),
                protection_emitted=protection_emitted,
            ),
        ),
    )

    assert record.get("phase_composition") == stamped
    assert ("phase_composition" in record) is (stamped is not None)


def test_a_record_banked_before_curves_existed_still_reads_clean(tmp_path):
    """Additive at the reader: the field's ABSENCE changes nothing it sees.

    A take banked before ``curves`` existed carries no such key. The reader
    narrows to its own field list, so a legacy record and a carrying one read
    identically — the whole claim behind widening the schema instead of
    versioning it.

    Asserted against a record with the key REMOVED, not one built empty: an
    empty list is what a new take with nothing to bank writes, and it is a
    different shape from the one already on disk.

    The entry baseline is the subject because it is the one kind that BOTH
    gained the carry and has a shipped reader; ``read_lateral_take`` narrows
    through the same comprehension and the pose record is unchanged here. A
    cloud seat and an unprompted-phase take are rejected on ``phase`` by both
    readers, so asserting over them would be two ``None``s agreeing.
    """
    import json

    from jasper.active_speaker.crossover_v2 import position_cycle

    carrying = {
        **_entry_record(curves=[_A_BANKED_CURVE]), "kind": POSITION_EVIDENCE_KIND,
    }
    legacy = {k: v for k, v in carrying.items() if k != "curves"}
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text(json.dumps(legacy))
    new.write_text(json.dumps(carrying))

    assert position_cycle.read_entry_baseline_take(new) is not None
    assert position_cycle.read_entry_baseline_take(
        old
    ) == position_cycle.read_entry_baseline_take(new)


def test_the_storage_seam_names_the_take_the_record_names():
    """One index convention, minted ONCE — by the builder, read by the seam.

    The seam names the bundle path and the builder names the record inside it.
    While the two spelled the convention separately a change to one silently
    made the path and its contents disagree about which take it was; worse, for
    the entry baseline (whose ``position_id`` IS a take id already) the second
    mint appended a second ``_aNN`` every time. The seam re-mints nothing now:
    the record carries ``take_id`` and the store names the artifact from it.
    """
    import asyncio

    from tests.engine_twin import retained_take_writer

    minted: list[str] = []

    class _Store:
        bundle_dir = "/no-such-bundle"
        session_id = "sess"

        def publish_json_artifact(self, path, payload):
            minted.append(path)
            return SimpleNamespace(fingerprint="fp")

        def identify_artifact(self, path):
            return SimpleNamespace(fingerprint="fp")

    seam = retained_take_writer(_Store(), "capture", asyncio.run)
    take_id = spatial.take_id_for("cloud_measure_03", 7)
    seam(
        SimpleNamespace(wav=None),
        {"attempt": 7, "take_id": take_id, "measure_kind": ""},
    )

    assert minted == [f"crossover_v2/capture/positions/{take_id}.json"]
    assert take_id == "cloud_measure_03_a07"


def test_an_entry_baseline_take_id_carries_index_and_attempt():
    """The same rule on the phase that is NOT a group member.

    The entry baseline rides the same retention seam and lands in the same
    ``position_artifacts`` namespace, so it needs the same collision-free id —
    but nothing in the group bookkeeping would give it one.

    Mutation-selected: dropping the attempt suffix left 21 tests green.
    """
    record = _entry_record(index=9, attempt=2)

    assert record["take_id"] == "entry_baseline_09_a02"
    assert record["position_id"] == record["take_id"]


def test_the_three_comparability_facts_ride_the_entry_record():
    """WHAT was played, WHERE from, and THROUGH WHICH graph.

    A before→after claim is only as good as those three matching on both sides,
    and they are the whole reason this is a separate builder rather than a
    keyword on the position one.
    """
    record = _entry_record(program_id="prog-42", graph_fingerprint="fp-entry")

    assert record["program_id"] == "prog-42"
    assert record["reference_mark"] == REFERENCE_MARK_DESIGN_AXIS
    assert record["graph_fingerprint"] == "fp-entry"


def test_the_entry_records_curve_is_the_durable_copy_of_the_before():
    """The arrays ride the write-once take, not only the rewritten state file.

    Fragment ``02``'s duplication #2: the flow state file's ``verify_priors``
    is rebuilt from the conductor on every persist, so before this the round's
    "before" stopped existing the moment the next round persisted. The names
    are ``EntryBaseline.from_dict``'s so one reader covers both.
    """
    record = _entry_record(
        freqs_hz=(200.0, 400.0), magnitude_db=(-1.5, 0.5), excluded=(True, False),
    )

    assert record["freqs_hz"] == [200.0, 400.0]
    assert record["magnitude_db"] == [-1.5, 0.5]
    assert record["excluded"] == [True, False]


# the module's own boundary


def _spatial_tree():
    import ast
    from pathlib import Path

    return ast.parse("\n".join(
        path.read_text() for path in sorted(Path(spatial.__file__).parent.glob("*.py"))
    ))


def _spatial_imports() -> set[str]:
    """Every module spatial imports, including inside functions."""
    import ast

    names: set[str] = set()
    for node in ast.walk(_spatial_tree()):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_module_reaches_neither_the_flow_nor_the_web_layer():
    """The dependency direction every module in this package states."""
    imported = _spatial_imports()

    assert not [n for n in imported if "crossover_v2_flow" in n], imported
    assert not [n for n in imported if n.startswith("jasper.web")], imported


def test_the_module_writes_no_journal_lines():
    """The package is side-effect free: it journals nothing."""
    import ast

    assert "jasper.log_event" not in _spatial_imports()
    assert not hasattr(spatial, "logger")

    called = {
        node.func.id for node in ast.walk(_spatial_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "log_event" not in called
