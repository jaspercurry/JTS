# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5a-iii: what the analyzer is told, and what it is deliberately not.

Every function in :mod:`jasper.active_speaker.crossover_v2.priors` is a decision
about what to WITHHOLD, and a withholding is exactly the kind of claim that
survives in a docstring while quietly dying in the code — nothing fails when a
prior is threaded back in, the analyzer simply starts making a claim its capture
cannot support.  So these pin the ABSENCES, not the presences.

The presences are covered elsewhere and better: the dual-run in this PR compares
every field of all six phases against the pre-extraction conductor, and the
suites that consume the priors (``test_crossover_v2_entry_baseline.py``,
``test_crossover_v2_lateral_evidence.py``, ``test_crossover_v2_fc_selector_wiring.py``)
grade what the analyzer does with them.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.branch_chain import sections_by_role
from jasper.active_speaker.crossover_v2 import priors

from tests.test_crossover_v2_conductor import FC_HZ, _preset

PRESET = _preset()
PROTECTION = sections_by_role(PRESET.crossover_regions)


# --------------------------------------------------------------------------- #
# 1. the withholdings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("factory", "why"),
    [
        (priors.entry_baseline_priors,
         "nothing is applied yet, so there is no prediction to track"),
        (priors.cloud_priors,
         "the mic is off the design axis, so divergence is what is being sampled"),
    ],
    ids=["entry_baseline", "cloud"],
)
def test_the_captures_that_cannot_support_a_tracking_claim_are_told_nothing(
    factory, why,
):
    """No ``predicted_sum`` means ``verify_tracking`` stays ``None``.

    That ``None`` is what
    :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_realization`
    reads as UNAVAILABLE rather than as a pass, so the whole honesty of both
    verdicts rests on this field being absent.
    """
    got = factory(fc_hz=FC_HZ)

    assert got.predicted_sum is None, why
    # …and the R18 absolute-claim pair, dropped for the same shape of reason.
    assert got.configured_crossover_response_by_role is None
    assert got.configured_polarity_sign_by_role is None
    # The band clamps exist only for a tracking comparison there is none of.
    assert got.measure_tweeter_sweep_lo_hz is None
    assert got.measure_woofer_sweep_hi_hz is None
    # Fc is KEPT: it is the session's declaration, not a claim about this
    # capture, and the analyzer places its bands with it.
    assert got.crossover_fc_hz == FC_HZ


def test_a_lateral_pose_is_analyzed_as_M_and_keeps_the_room_floor():
    """§4.2's composition is per-candidate and offline; a pose must not bake it.

    Baking the configured ``C`` in here would make the retained evidence answer
    for one corner alone — which is precisely what R17's comparison exists to
    avoid. The ambient report is kept, because a pose still grades its own SNR.
    """
    got = priors.lateral_priors(fc_hz=FC_HZ, ambient_report={"floor_db": -70.0})

    assert got.configured_crossover_response_by_role is None
    assert got.configured_polarity_sign_by_role is None
    assert got.measurement_protection_response_by_role is None
    assert got.predicted_sum is None
    assert got.alignment_delay_bounds_us is None
    assert got.ambient_report == {"floor_db": -70.0}


def test_check_is_told_the_corner_and_nothing_else():
    """Withholding Fc is not neutral — the solve would go LOUDER without it.

    Its gain solve scopes each band's SNR requirement by whether the band lies
    inside the crossover overlap window; absent the corner it applies the
    ALIGNMENT requirement everywhere. So this prior can only make MEASURE
    quieter, which is why it is the one field CHECK gets.
    """
    got = priors.check_priors(fc_hz=FC_HZ)

    assert got.crossover_fc_hz == FC_HZ
    assert got.ambient_report is None
    assert got.predicted_sum is None
    assert got.configured_crossover_response_by_role is None


# --------------------------------------------------------------------------- #
# 2. the configured-path trio moves together
# --------------------------------------------------------------------------- #


def test_the_configured_path_priors_are_all_present_or_all_absent():
    """``_compose_configured_path_ir`` RAISES on a partial set.

    So "protection is absent" must clear the response map, the polarity map and
    the required-band map together. A half-filled set would refuse the
    composition outright — the session would produce no candidate at all rather
    than a slightly-worse one.
    """
    with_protection = priors.measure_priors(
        fc_hz=FC_HZ, source_preset=PRESET,
        protection_sections_by_role=PROTECTION,
        ambient_report=None, alignment_delay_bounds_us=None,
    )
    without = priors.measure_priors(
        fc_hz=FC_HZ, source_preset=PRESET,
        protection_sections_by_role=None,
        ambient_report=None, alignment_delay_bounds_us=None,
    )

    present = (
        with_protection.measurement_protection_response_by_role,
        with_protection.configured_crossover_response_by_role,
        with_protection.configured_polarity_sign_by_role,
        with_protection.candidate_required_band_hz_by_role,
    )
    absent = (
        without.measurement_protection_response_by_role,
        without.configured_crossover_response_by_role,
        without.configured_polarity_sign_by_role,
        without.candidate_required_band_hz_by_role,
    )

    assert all(v is not None for v in present), present
    assert all(v is None for v in absent), absent


def test_verify_carries_the_design_target_unguarded_by_protection():
    """VERIFY's absolute claim (R18) does not need ``P``, unlike MEASURE's.

    MEASURE wants the configured crossover only as the ``C_c`` half of a
    de-embedding that cannot run without the protection filter. For VERIFY the
    configured crossover IS the design target, so it rides regardless — and that
    asymmetry is a decision, not an oversight.
    """
    got = priors.verify_priors(
        fc_hz=FC_HZ, source_preset=PRESET, predicted_sum=None,
        sweep_bounds=(None, None),
    )

    assert got.configured_crossover_response_by_role is not None
    assert got.configured_polarity_sign_by_role is not None
    assert got.measurement_protection_response_by_role is None


# --------------------------------------------------------------------------- #
# 3. the sweep bounds are MEASURED, not derived
# --------------------------------------------------------------------------- #


def test_the_sweep_bounds_come_off_the_composed_program():
    """"What did this session sweep" has one answer, and it is the program's.

    Deriving them from Fc instead would bound the tracking comparison and R17's
    scoring band by a number nothing was excited at.
    """
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from tests.test_crossover_v2_conductor import CAPS, SESSION_VOLUME_DB, _roles

    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB, fc_hz=FC_HZ,
    ).measure_program({"woofer": -32.0, "tweeter": -38.0})

    lo, hi = priors.measure_sweep_bounds(program)

    assert lo == program.segment("sweep_t").f1_hz
    assert hi == program.segment("sweep_w").f2_hz
    assert lo is not None and hi is not None
    # Not the corner, and not a derivation from it.
    assert lo != FC_HZ and hi != FC_HZ


def test_no_composed_program_means_no_bounds_rather_than_a_guess():
    assert priors.measure_sweep_bounds(None) == (None, None)


# --------------------------------------------------------------------------- #
# 4. the kernel boundary
# --------------------------------------------------------------------------- #


def test_role_transfers_hands_over_callables_never_sections():
    """The kernel may not import this package, so it gets behaviour, not data.

    Pinned as "callable, and it answers" rather than as a type check, because
    what the boundary needs is something the kernel can evaluate without knowing
    what a ``CrossoverSection`` is.
    """
    import numpy as np

    got = priors.role_transfers(PROTECTION)

    assert got is not None
    for role, transfer in got.items():
        assert callable(transfer), role
        out = np.asarray(transfer(np.array([100.0, 2000.0, 10000.0])))
        assert out.shape == (3,)
        assert np.all(np.isfinite(out))


def test_no_sections_means_no_map_rather_than_an_empty_one():
    """``None`` and ``{}`` mean different things downstream: absent evidence
    versus a declared-empty filter set."""
    assert priors.role_transfers(None) is None
    assert priors.role_transfers({}) == {}


def test_the_flow_re_export_resolves_to_the_one_definition():
    from jasper.active_speaker import crossover_v2_flow as flow

    assert flow._role_transfers is priors.role_transfers


# --------------------------------------------------------------------------- #
# 5. what the conductor's delegates actually hand over
#
# The module above cannot be wrong about a value it was never given. These pin
# the other half — that the conductor passes its accumulated session evidence
# in — and they exist because a mutation that blanked one of these arguments
# survived 351 conductor and entry-baseline tests.
# --------------------------------------------------------------------------- #


def _wired_conductor(**kwargs):
    from jasper.active_speaker import crossover_v2_flow as flow
    from tests.test_crossover_v2_conductor import (
        CAPS, SESSION, SESSION_VOLUME_DB, FakeSeams, _roles,
    )

    return flow.CrossoverV2Conductor(
        session_id=SESSION, source_preset=PRESET, roles_bands=_roles(),
        fc_hz=FC_HZ, driver_caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(), driver_spacing_m=0.15, **kwargs,
    )


def test_verify_is_handed_the_prediction_the_tracking_comparison_needs():
    """The dead-input class, pinned before it happens again.

    ``verify_tracking`` is the ONLY input
    :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_realization`
    grades "did the graph do what its filters commanded" from, and the analyzer
    computes it only when it is handed ``predicted_sum``. Blanking this one
    argument would leave every VERIFY's realization verdict UNAVAILABLE — the
    same shape as #2323's dead ``boosted`` read, which sat green for a phase.
    A mutation that did exactly that survived 351 tests in the two suites that
    look most likely to catch it.
    """
    sentinel = ("freqs", "db")
    conductor = _wired_conductor(measure_predicted_sum=sentinel)

    assert conductor._verify_priors().predicted_sum is sentinel


def test_measure_is_handed_the_room_floor_and_the_declared_delay_bounds():
    """CHECK's ambient report and the preset's delay range both reach MEASURE.

    Without the first, ``DriverResponse.snr`` is None on every v2 session (issue
    #1830 — a shipped instrument reading nothing). Without the second the
    flatness search has no declared magnitude window to centre its lobe in.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        alignment_delay_search_bounds_us,
    )

    conductor = _wired_conductor()
    conductor._check_ambient_report = {"floor_db": -71.5}

    got = conductor._measure_priors()

    assert got.ambient_report == {"floor_db": -71.5}
    assert got.alignment_delay_bounds_us == alignment_delay_search_bounds_us(PRESET)
    assert got.alignment_delay_bounds_us is not None, (
        "the fixture preset must declare a delay range, or this pins nothing"
    )


# --------------------------------------------------------------------------- #
# 6. the guard this extraction earned
# --------------------------------------------------------------------------- #


def test_no_class_in_the_flow_defines_a_method_twice():
    """A shadowed method is a silent extraction failure, and it happened here.

    This phase's first cut replaced five priors methods with delegates but left
    the originals further down the class body — the slice ran to a marker that
    turned out to sit in the middle of the group. Python's later definition won,
    so the delegates were dead and the originals were still running, and EVERY
    signal was green for the wrong reason: the suites passed because the old
    code was executing, and the byte-identical dual-run compared the old
    behaviour against itself. Only a mutation aimed at a delegate — which did
    not change the result — exposed it.

    A whole strangler phase moves methods out of one class one organ at a time,
    so this is a defect the process can produce again. Parsed rather than
    imported, because a duplicate definition is legal Python: ``ast`` is the
    only place it is visible at all.
    """
    import ast
    from pathlib import Path

    module = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2_flow.py"
    )
    tree = ast.parse(module.read_text(), filename=str(module))

    duplicates: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen: set[str] = set()
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # A property and its setter legitimately share a name; they are
            # distinguished by the decorator, not by the ``def``.
            if any(
                isinstance(d, ast.Attribute) and d.attr in {"setter", "deleter"}
                for d in item.decorator_list
            ):
                continue
            if item.name in seen:
                duplicates.setdefault(node.name, []).append(item.name)
            seen.add(item.name)

    assert duplicates == {}, (
        f"a later definition silently shadows an earlier one: {duplicates}"
    )


def test_the_duplicate_guard_sees_a_planted_shadow(tmp_path):
    """The guard's positive control — an ``ast`` walk that matched nothing would
    report every class clean and read exactly like compliance."""
    import ast

    planted = tmp_path / "_shadow_probe.py"
    planted.write_text(
        "class C:\n"
        "    def f(self):\n        return 1\n"
        "    def f(self):\n        return 2\n"
    )
    names = [
        item.name
        for node in ast.walk(ast.parse(planted.read_text()))
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert names == ["f", "f"], "the walk must see both definitions"
