"""R16 — the lateral-evidence producer (crossover-linearization plan §4.4).

Hardware-free. The harness is the conductor suite's own, imported rather than
re-built: two copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker.measurement_programs import Pose
from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2 import pose_curve
from jasper.active_speaker.crossover_v2 import programs
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.audio_measurement import evidence_grid
from jasper.audio_measurement.evidence_grid import lateral_evidence_grid_hz
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.audio_measurement.program import build_verify_program
from jasper.audio_measurement.program_analysis import (
    DriverResponse,
)

from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _conductor,
    _measure_analysis,
    _roles,
    _run_phase,
)

LATERAL_PROMPTS = tuple(
    capture_plan.CloudPositionPrompt("pose", pose=Pose(azimuth, 0)) for azimuth in (0, -7, 7, -22, 22, 0)
)
LATERAL_COUNT = len(LATERAL_PROMPTS)
FIRST_LATERAL_INDEX = 3
LAST_LATERAL_INDEX = FIRST_LATERAL_INDEX + LATERAL_COUNT - 1


def _lateral_map(poses: int) -> dict[int, str]:
    """CHECK, MEASURE, then ``poses`` lateral poses."""
    return {1: PHASE_CHECK, 2: PHASE_MEASURE, **{3 + offset: PHASE_LATERAL for offset in range(poses)}}


def _lateral_conductor(fakes: FakeSeams, **kwargs):
    """A conductor whose stage 1 is CHECK + MEASURE + the lateral walk."""
    return _conductor(fakes, index_phase_map=_lateral_map(LATERAL_COUNT), lateral_prompts=LATERAL_PROMPTS, **kwargs)


def _walk(conductor, *, through: int = LAST_LATERAL_INDEX) -> list[dict]:
    """CHECK, MEASURE, then the lateral poses up to and including ``through``."""
    out = [_run_phase(conductor, 1, 1), _run_phase(conductor, 2, 1)]
    for index in range(FIRST_LATERAL_INDEX, through + 1):
        out.append(_run_phase(conductor, index, 1))
    return out


@pytest.mark.parametrize("purpose", ["speaker", "room"])
def test_inline_summed_lateral_entries_budget_the_requested_sweep(purpose):
    request = ac.AngleCaptureRequest((
        ac.AngleStop(Pose(22, 0), ac.REGIME_SUMMED, candidate_id="trial", purpose=purpose),
    ), candidates=("trial",))
    captures = prepare_plan_captures(request, roles_bands=_roles())
    plan = capture_plan.build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, c.stop.candidate_id) for c in captures],
        roles_bands=_roles(), fc_hz=FC_HZ, acknowledgement_binding="b" * 24,
        retries_per_pose=0,
    ).capture_plan
    (entry,) = plan.entries
    assert entry.kind_label == PHASE_LATERAL
    # No purpose names a summed sweep's band (ADR-0400).
    assert captures[0].spec.sweep_band_hz == ()
    # The plan's only take opens its run, so its entry budgets the prelude (ADR-0417).
    program = build_verify_program(FC_HZ, measurement_band_hz=programs.measurement_band_hz(_roles()),
                                   courtesy_prelude=True)
    assert entry.duration_ms == capture_plan._program_duration_ms(program) + capture_plan.CAPTURE_ENTRY_MARGIN_MS
    assert entry.screen[capture_plan.POSITION_DEG_KEY] == "22"


def test_a_pose_is_analyzed_neutrally_while_the_anchor_is_composed():
    """§4.2's composition is per-candidate, so it is the consumer's step.

    The anchor carries the configured ``C``/``P``/polarity maps; a pose carries
    none of them, which is what leaves its retained curve as ``M``.
    """
    fakes = FakeSeams()
    # A protected-neutral session's anchor really is composed, and MEASURE now
    # builds its candidate right there — so the fake has to say so or the fitter
    # refuses the capture before a pose is ever analyzed.
    fakes.measure = lambda program: replace(
        _measure_analysis(program), configured_path_composed=True,
    )
    c = _lateral_conductor(
        fakes,
        measurement_protection_sections_by_role={"woofer": (), "tweeter": ()},
    )
    _walk(c, through=FIRST_LATERAL_INDEX)
    # FIRST call per phase, not last: a phase may analyze its capture more than
    # once under one phase name, so a last-wins read could compare a pose
    # against something other than the anchor's priors.
    by_phase: dict[str, object] = {}
    for phase, _pp, _r, priors, _g in fakes.analyzed:
        by_phase.setdefault(phase, priors)
    anchor = by_phase[PHASE_MEASURE]
    pose = by_phase[PHASE_LATERAL]
    assert anchor.configured_crossover_response_by_role is not None
    assert anchor.measurement_protection_response_by_role is not None
    assert anchor.configured_polarity_sign_by_role is not None
    for field in (
        "configured_crossover_response_by_role",
        "measurement_protection_response_by_role",
        "configured_polarity_sign_by_role",
        "candidate_required_band_hz_by_role",
        "predicted_sum",
        "alignment_delay_bounds_us",
        # A pose commits no alignment, so it is never told the one the speaker
        # already plays (#2617) — the same withholding, one field over.
        "applied_alignment",
    ):
        assert getattr(pose, field) is None, field
    # Still MEASURE-shaped in every other respect: the analyzer needs the Fc
    # and CHECK's ambient floor to grade the pose's own SNR.
    assert pose.crossover_fc_hz == anchor.crossover_fc_hz
    assert pose.ambient_report == anchor.ambient_report


# ``lateral_pose_curve`` indexes its input's own frequency axis
# (``freqs[left]`` after a ``searchsorted``/``clip``), so a degenerate response
# with an EMPTY axis is an ``IndexError`` rather than a zero-length curve —
# measured, not inferred: ``index -2 is out of bounds for axis 0 with size 0``.


def _empty_axis_response(role: str):
    """A driver response the analyzer could emit and the resampler cannot take.

    Degenerate rather than absent: it carries a role the sweep-band map knows
    and ``repeat_index is None``, so it passes the comprehension's filter and
    reaches ``lateral_pose_curve`` — which is the only way to exercise the
    hazard. Every array is empty, which is the shape a capture that located
    nothing reduces to.
    """
    return DriverResponse(
        role=role,
        freqs_hz=np.asarray([], dtype=float),
        magnitude_db=np.asarray([], dtype=float),
        complex_tf=np.asarray([], dtype=complex),
        gating={"applied": True, "window_ms": 8.0},
        snr=None,
        validity_floor_hz=None,
    )


def test_the_resampler_really_does_raise_on_an_empty_axis():
    """Building a curve from an empty-axis response raises rather than
    returning a zero-length curve."""
    with pytest.raises(IndexError):
        pose_curve.lateral_pose_curve(_empty_axis_response("woofer"), (100.0, 20000.0))


def test_the_evidence_basis_is_a_bounded_log_grid():
    grid = lateral_evidence_grid_hz()
    lo, hi = evidence_grid.LATERAL_EVIDENCE_BAND_HZ
    assert grid[0] == pytest.approx(lo)
    assert grid[-1] == pytest.approx(hi)
    ratios = grid[1:] / grid[:-1]
    assert np.allclose(ratios, ratios[0])
    # The declared density is NOMINAL — the point count rounds to an integer so
    # the grid lands exactly on both band edges — so this is a bound, not an
    # equality, because that is what is actually true.
    per_octave = math.log(2.0) / math.log(ratios[0])
    nominal = evidence_grid.LATERAL_EVIDENCE_POINTS_PER_OCTAVE
    assert abs(per_octave - nominal) / nominal < 0.01
    # Bounded: a few thousand complex values, not the analysis grid's hundreds
    # of thousands.
    assert grid.size * 2 * LATERAL_COUNT < 2000
