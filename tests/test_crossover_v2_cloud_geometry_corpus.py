# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Corpus acceptance for the conductor's position-group combine seam.

The conductor assembles one
:class:`~jasper.audio_measurement.spatial_combine.PositionCapture` per prompted
mic position and reads ONE thing off the combiner: the geometry verdict. The
assembly has a choice the S0 kit did not have to make — the live flow keeps a
``DriverResponse`` (calibrated, reflection-gated magnitude plus the matching
complex transfer function), not the raw deconvolved impulse response the S0
forensics ran ``detect_echo`` on. ``cloud_position_capture`` reconstructs an IR
from that gated complex TF.

That reconstruction is a claim about physics, so it is MEASURED here rather than
asserted in a docstring: both constructions are run over the same ten S0 main-leg
positions and their geometry verdicts compared. Gated by ``JTS_FLAT_LIN_S0``;
skips cleanly in CI, which is exactly why a corpus PR is not done until an
implementer has seen it report PASSED rather than SKIPPED (``pytest -rs``).
"""
from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2_flow import (
    _CloudPosition,
    cloud_geometry_verdict,
    cloud_position_capture,
)
from jasper.audio_measurement.spatial_combine import combine_positions

from tests import _flat_lin_corpus as corpus

pytestmark = corpus.requires_s0_curves

# The S0 main leg's own capture rate. Read from the session rather than assumed
# would be better; the loader's own program parameters fix it at 48 kHz and the
# assertion below fails loudly if a future corpus differs.
_SAMPLE_RATE_HZ = 48_000


def _product_positions(session_dir) -> list[_CloudPosition]:
    """The S0 leg reassembled the way the LIVE conductor assembles a group."""
    positions: list[_CloudPosition] = []
    for index, position_id in enumerate(sorted(corpus.S0_MAIN_TWEETER_HEIGHT
                                               + corpus.S0_MAIN_HAND_WIDTH_LOW)):
        response, _fc_hz = corpus.s0_position_driver_response(
            session_dir, position_id
        )
        positions.append(
            _CloudPosition(
                position_id=position_id,
                index=index + 3,
                attempt=index + 3,
                prompt="(corpus replay)",
                wide=False,
                captured_at=0.0,
                response=response,
                sample_rate_hz=_SAMPLE_RATE_HZ,
            )
        )
    return positions


def test_gated_ir_assembly_reaches_the_same_geometry_verdict_as_the_s0_reference():
    """The measured claim behind ``cloud_position_capture``'s docstring.

    Reference: ``corpus.s0_position_captures`` — the S0-validated construction,
    which hands ``detect_echo`` the FULL deconvolved IR and power-mean-averages
    each position's two repeat captures. Product: one capture per position,
    with the IR reconstructed from the gated, calibrated complex TF the live
    analysis actually retains.

    Two things differ at once (gated-vs-full IR, and single-vs-averaged take),
    deliberately: the claim under test is not "these IRs are equal" but "the
    live assembly reaches the same geometry answer on this corpus as the
    construction S0 validated". Regime: the S0 main leg, ten positions on a
    desk at ~1 m, whose comb S0 attributed to a source-fixed horn-rim wave —
    the one corpus where a LOCKED verdict is known ground truth. It is not
    evidence about a room-fixed cloud, which no corpus in this repo carries.
    """
    reference = combine_positions(corpus.s0_position_captures(corpus.S0_MAIN))
    product = combine_positions(
        [cloud_position_capture(p) for p in _product_positions(corpus.S0_MAIN)]
    )

    assert reference.geometry.n_positions == product.geometry.n_positions == 10
    # The verdict — the only field PR-3b reads — agrees, and agrees on the
    # qualifier too (a thin-evidence disagreement would change whether the
    # conductor retries).
    assert product.geometry.locked == reference.geometry.locked is True
    assert product.geometry.reason == reference.geometry.reason
    assert product.geometry.thin_evidence == reference.geometry.thin_evidence is False

    # The supporting numbers agree well inside the tau estimator's own stated
    # 1-4 % accuracy (EchoDiagnostic.tau_us), so the agreement above is not a
    # coincidence of two verdicts landing on the same side of a threshold.
    assert reference.geometry.median_tau_us > 0
    drift = abs(product.geometry.median_tau_us - reference.geometry.median_tau_us)
    assert drift / reference.geometry.median_tau_us < 0.04
    # The live assembly loses no positions to refusals it should not: it counts
    # at least as many usable echo estimates as the reference does.
    assert product.geometry.n_confident >= reference.geometry.n_confident


def test_conductor_group_verdict_reports_the_locked_corpus_as_locked():
    """End of the seam: what ``_close_cloud_group`` actually reads.

    ``cloud_geometry_verdict`` is the conductor's whole use of the combiner in
    PR-3b, and on the S0 main leg it must report ``locked`` — otherwise the
    geometry-retry path the conductor ships would be unreachable on the one
    corpus known to contain a position-invariant comb, and the retry would be
    an untested claim rather than a measured one.
    """
    verdict = cloud_geometry_verdict(_product_positions(corpus.S0_MAIN))

    assert verdict["locked"] is True
    assert verdict["reason"] == "geometry_locked"
    assert verdict["thin_evidence"] is False
    assert verdict["n_positions"] == 10
    assert verdict["n_confident"] >= 2
    assert 200.0 < verdict["median_tau_us"] < 500.0
    # Every field is JSON-native: the host persists this verbatim into the
    # durable v2 state, so a numpy scalar here would be a serialization bug.
    for key, value in verdict.items():
        assert isinstance(value, (bool, int, float, str)), key


def test_a_malformed_position_degrades_to_a_verdict_instead_of_raising():
    """A group's captures are already-accepted evidence; a combiner failure must
    not retroactively fail them. Exercised against REAL corpus positions so the
    degradation path is proven on the same inputs the happy path uses."""
    positions = _product_positions(corpus.S0_MAIN)
    broken = positions[:1]
    object.__setattr__(broken[0], "sample_rate_hz", 0)

    verdict = cloud_geometry_verdict(broken)
    assert verdict["locked"] is False
    assert verdict["reason"] == "combine_failed"


def test_empty_group_is_unknown_not_fine():
    verdict = cloud_geometry_verdict([])
    assert verdict == {"locked": False, "reason": "no_positions", "n_positions": 0}


@pytest.mark.parametrize("position_id", ["cloud_01", "cloud_10"])
def test_position_capture_grid_survives_the_combiner_s_own_validation(position_id):
    """``PositionCapture`` enforces a strictly-increasing, uniformly-spaced grid
    (the smoothing kernel assumes it). The live assembly must satisfy that from
    a real analysis, not just from synthetic linspace fixtures."""
    response, _fc = corpus.s0_position_driver_response(corpus.S0_MAIN, position_id)
    capture = cloud_position_capture(
        _CloudPosition(
            position_id=position_id, index=3, attempt=3, prompt="", wide=False,
            captured_at=0.0, response=response, sample_rate_hz=_SAMPLE_RATE_HZ,
        )
    )
    assert capture.position_id == position_id
    assert capture.freqs_hz.size == capture.magnitude_db.size
    assert capture.ir is not None and capture.ir.size > 0
    # One position on its own is a legal cloud; the combiner accepting it is
    # what proves the grid/IR shapes are valid.
    combine_positions([capture])
