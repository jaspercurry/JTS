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
``test_crossover_v2_lateral_evidence.py``) grade what the analyzer does with
them.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.branch_chain import radiating_band_hz, sections_by_role
from jasper.active_speaker.crossover_v2 import priors
from jasper.audio_measurement.comparison_bands import overlap_band_hz

from tests.crossover_v2_fixtures import FC_HZ, _preset

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
    # The band clamp exists only for a tracking comparison there is none of.
    assert got.measure_excited_band_hz is None
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
        applied_alignment=None, explicit_alignment_delay_us=None,
        explicit_alignment_polarity_sign=None,
    )
    without = priors.measure_priors(
        fc_hz=FC_HZ, source_preset=PRESET,
        protection_sections_by_role=None,
        ambient_report=None, alignment_delay_bounds_us=None,
        applied_alignment=None, explicit_alignment_delay_us=None,
        explicit_alignment_polarity_sign=None,
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
        sweep_bounds=None,
    )

    assert got.configured_crossover_response_by_role is not None
    assert got.configured_polarity_sign_by_role is not None
    assert got.measurement_protection_response_by_role is None


# --------------------------------------------------------------------------- #
# 2b. the candidate-required union has ONE owner
# --------------------------------------------------------------------------- #


def test_the_required_band_covers_both_declarations_without_widening():
    """A superset of radiating ∪ overlap, and no wider than the wider edge.

    Stated as a COVERAGE property against the two declarations obtained
    independently, not as a restatement of the union expression: a swapped
    ``min``/``max`` produces the INTERSECTION, which fails the coverage half,
    and a hard-coded ``(0.0, inf)`` fails the tightness half.  Anchoring on
    ``candidate_required_band_hz`` itself would pass under both.
    """
    sections = sections_by_role(PRESET.crossover_regions)
    overlap_lo, overlap_hi = overlap_band_hz(float(FC_HZ))

    got = priors.candidate_required_band_hz(sections, fc_hz=FC_HZ)

    assert set(got) == set(sections)
    for role, section in sections.items():
        lo, hi = got[role]
        radiating_lo, radiating_hi = radiating_band_hz(section)
        # Covers both — the superset direction the docstring calls the safe
        # side for a required mask.
        assert lo <= radiating_lo and lo <= overlap_lo, role
        assert hi >= radiating_hi and hi >= overlap_hi, role
        # …and is one of the two declared edges rather than something wider,
        # so "superset" cannot quietly become "everything".
        assert lo in (radiating_lo, overlap_lo), role
        assert hi in (radiating_hi, overlap_hi), role


def test_measure_priors_asks_the_owner_rather_than_re_spelling_it():
    """Whatever corner a round runs at, the band answers from one formula."""

    got = priors.measure_priors(
        fc_hz=FC_HZ, source_preset=PRESET,
        protection_sections_by_role=PROTECTION,
        ambient_report=None, alignment_delay_bounds_us=None,
        applied_alignment=None, explicit_alignment_delay_us=None,
        explicit_alignment_polarity_sign=None,
    )

    assert got.candidate_required_band_hz_by_role == (
        priors.candidate_required_band_hz(
            sections_by_role(PRESET.crossover_regions), fc_hz=FC_HZ,
        )
    )


# --------------------------------------------------------------------------- #
# 3. the sweep bounds are MEASURED, not derived
# --------------------------------------------------------------------------- #


def test_the_sweep_bounds_come_off_the_composed_program():
    """"What did this session sweep" has one answer, and it is the program's.

    Deriving them from Fc instead would bound the tracking comparison and R17's
    scoring band by a number nothing was excited at.
    """
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from tests.crossover_v2_fixtures import CAPS, SESSION_VOLUME_DB, _roles

    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB, fc_hz=FC_HZ,
        sweep_duration_limits_s={},
    ).measure_program({"woofer": -32.0, "tweeter": -38.0})

    bounds = priors.measure_sweep_bounds(program)
    assert bounds is not None
    lo, hi = bounds

    assert lo == program.segment("sweep_t").f1_hz
    assert hi == program.segment("sweep_w").f2_hz
    # Not the corner, and not a derivation from it.
    assert lo != FC_HZ and hi != FC_HZ


def test_no_composed_program_means_no_bounds_rather_than_a_guess():
    assert priors.measure_sweep_bounds(None) is None


def test_the_sweep_durations_come_off_the_composed_program_and_reflect_a_fit():
    """#2923: the banked figure is what actually played, fit included.

    A woofer limit BELOW the 4.0 s nominal default forces #2921's fit on every
    band (the nominal always realizes at or above its own request), so this is
    a deterministic way to exercise the fit without depending on which band a
    fixture happens to use.
    """
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from tests.crossover_v2_fixtures import CAPS, SESSION_VOLUME_DB, _roles

    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB, fc_hz=FC_HZ,
        sweep_duration_limits_s={"woofer": 3.5},
    ).measure_program({"woofer": -32.0, "tweeter": -38.0})

    durations = priors.measure_sweep_durations_s(program)

    assert durations is not None
    rate = program.sample_rate_hz
    assert durations["woofer"] == pytest.approx(
        program.segment("sweep_w").n_samples / rate
    )
    assert durations["tweeter"] == pytest.approx(
        program.segment("sweep_t").n_samples / rate
    )
    # The fit actually bit: realized at or below the limit, not the nominal
    # ~4.0 s default a naive read would otherwise report.
    assert durations["woofer"] <= 3.5
    # The tweeter carried no limit, so its nominal ~3.0 s default stands.
    assert durations["tweeter"] == pytest.approx(3.0, abs=0.05)


def test_no_composed_program_means_no_durations_rather_than_a_guess():
    assert priors.measure_sweep_durations_s(None) is None


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
    from tests.crossover_v2_fixtures import (
        CAPS, SESSION, SESSION_VOLUME_DB, FakeSeams, _roles,
    )

    return flow.CrossoverV2Session(
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


def shadowed_methods(source: str) -> dict[str, list[str]]:
    """Class name → the method names a LATER definition silently shadows.

    The guard's predicate, factored out so its positive control can exercise
    **this function** rather than ``ast`` in general. A control that re-walks the
    tree by hand proves the standard library works; it says nothing about
    whether the rule below still finds anything.

    Parsed rather than imported, because a duplicate definition is legal Python
    and leaves no runtime trace: ``ast`` is the only place it is visible at all.
    """
    import ast

    duplicates: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(source)):
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
    return duplicates


#: The defect this guard exists for, in the shape it actually took: a delegate
#: written where the original was expected to have been removed, with the
#: original still further down the class body. Private name, decorated exactly
#: as the real pair was, and separated by an unrelated method — every property
#: a naive predicate could be blind to.
SHADOW_PROBE = '''\
class CrossoverV2Session:
    def _check_priors(self):
        return _priors.check_priors(fc_hz=self._fc_hz)

    def _measure_sweep_bounds(self):
        return (None, None)

    def _check_priors(self):
        return MeasurementPriors(crossover_fc_hz=self._fc_hz)

    @property
    def tier(self):
        return self._tier

    @tier.setter
    def tier(self, value):
        self._tier = value
'''


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
    so this is a defect the process can produce again.
    """
    from pathlib import Path

    module = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2_flow.py"
    )

    duplicates = shadowed_methods(module.read_text())

    assert duplicates == {}, (
        f"a later definition silently shadows an earlier one: {duplicates}"
    )


def test_the_guard_finds_the_defect_it_exists_for():
    """The positive control, on the REAL shape and through the REAL predicate.

    :data:`SHADOW_PROBE` is the 5a-iii defect reproduced: a private,
    underscore-named delegate; its original further down; an unrelated method
    between them; and a legitimate property/setter pair alongside, so a
    predicate that went blind to any of those would show here.

    Verified against three deliberate breaks of :func:`shadowed_methods` — the
    decorator skip replaced with an unconditional ``continue``, a
    leading-underscore exclusion, and dropping the ``seen.add`` that makes the
    second definition detectable at all. Each one turns this assertion red,
    which is the property a control has to have: it controls the GUARD, not the
    parser.
    """
    assert shadowed_methods(SHADOW_PROBE) == {
        "CrossoverV2Session": ["_check_priors"]
    }


def test_the_guard_does_not_cry_wolf_on_a_property_setter():
    """The other direction, which is what stops the guard from being deleted.

    A property and its setter share a name legitimately and appear in every
    conductor-shaped class; a guard that flagged them would be turned off within
    a week, and then the real defect walks through.
    """
    legitimate = '''\
class C:
    @property
    def tier(self):
        return self._tier

    @tier.setter
    def tier(self, value):
        self._tier = value

    @tier.deleter
    def tier(self):
        del self._tier
'''

    assert shadowed_methods(legitimate) == {}
