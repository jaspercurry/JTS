# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emit loop end to end, against a stand-in binary. No hardware, no network.

The stand-in (:mod:`tests._fake_camilladsp`) is a real subprocess invoked with
the real argv, so these cases exercise the whole chain — emit, derive, render,
extract, deconvolve, grade — and the argv/determinism contracts with it.

**What they prove and what they cannot.** They prove the loop wires the legs
together, that the A/B cancels everything the two arms share, and that a filter
realized at the wrong shape is caught with the depth the historical defect had.
They cannot prove that the REAL CamillaDSP realizes an emitted biquad the way
the fit models it — that is the question the bench exists to ask, and only the
pinned binary on the Pi answers it.
"""

from __future__ import annotations

import json
import math
import resource
import stat
import sys
from pathlib import Path

import pytest

from jasper.active_speaker import ActiveSpeakerPreset
from jasper.active_speaker.bench import loop
from jasper.active_speaker.bench.compare import ARRIVAL_PRE_MS, SOFT_CLIP_BUDGET_DB
from jasper.active_speaker.bench.loop import EmitLoopError, run_emit_loop
from jasper.active_speaker.delta_probe import VERDICT_MATCHED, VERDICT_MODEL_ERROR
from jasper.bass_extension.bench.render import BinaryIdentity
from jasper.camilla_config_contract import SHELF_Q
from tests._fake_camilladsp import SLOPE6_SHELF_ENV, slope6_shelf_q
from tests.test_active_speaker_profile import _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
FAKE_BINARY = Path(__file__).with_name("_fake_camilladsp.py")
SWEEP_SECONDS = 2.0

# The stand-in binary is a Python interpreter with numpy and scipy loaded, not
# the ~5 MB Rust binary these bounds were sized for — and macOS refuses
# ``setrlimit(RLIMIT_AS)`` at ANY finite value, so a finite bound here would
# make every case in this module fail on the maintainer's own laptop while
# passing in CI. The finite-bound contract is render.py's and is covered by
# tests/test_bass_extension_bench_render.py; what this module tests is the loop.
TEST_BOUNDS = loop.RenderBounds(
    timeout_s=600.0,
    rlimit_as_bytes=resource.RLIM_INFINITY,
    rlimit_cpu_s=600,
    nice=0,
)

DESIGNED = {
    "woofer": [{"biquad_type": "Peaking", "freq": 320.0, "q": 3.0, "gain": -4.0}],
    "tweeter": [
        {"biquad_type": "Lowshelf", "freq": 8400.0, "q": SHELF_Q, "gain": -11.0},
        {"biquad_type": "Peaking", "freq": 4000.0, "q": 2.0, "gain": -2.5},
    ],
}

#: Same ceiling the synthetic-render suite uses for an exact case; see
#: ``tests/test_active_speaker_emit_bench_compare.EXACT_RENDER_CEILING_DB``.
EXACT_RENDER_CEILING_DB = 0.05


def _fake_binary(directory: Path, *, inject_slope6: bool = False) -> BinaryIdentity:
    """A shell shim with the real argv contract, standing in for camilladsp."""

    script = directory / ("camilladsp-slope6" if inject_slope6 else "camilladsp")
    export = f"{SLOPE6_SHELF_ENV}=1 " if inject_slope6 else ""
    script.write_text(
        f'#!/bin/sh\n{export}exec "{sys.executable}" "{FAKE_BINARY}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return BinaryIdentity(
        path=str(script),
        version_output="CamillaDSP 4.1.3",
        sha256="0" * 64,
        camilladsp_build_id="camilladsp-v4.1.3-stand-in",
    )


def _run(directory: Path, *, inject_slope6: bool = False, **kwargs):
    kwargs.setdefault("sweep_seconds", SWEEP_SECONDS)
    return run_emit_loop(
        ActiveSpeakerPreset.from_mapping(_two_way_preset("mono")),
        playback_device=ACTIVE_PCM,
        linearization=DESIGNED,
        work_dir=directory / "work",
        binary=_fake_binary(directory, inject_slope6=inject_slope6),
        bounds=TEST_BOUNDS,
        **kwargs,
    )


@pytest.fixture(scope="module")
def exact_report(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("exact"))


@pytest.fixture(scope="module")
def slope6_report(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("slope6"), inject_slope6=True)


# --------------------------------------------------------------------------- #
# the pair
# --------------------------------------------------------------------------- #


def test_an_exact_render_matches_on_every_branch(exact_report) -> None:
    assert exact_report.matched
    assert [b.role for b in exact_report.branches] == ["woofer", "tweeter"]
    for branch in exact_report.branches:
        assert branch.verdict.verdict == VERDICT_MATCHED
        assert branch.band_max_error_db < EXACT_RENDER_CEILING_DB
        assert branch.soft_clip_bound_db < SOFT_CLIP_BUDGET_DB
        assert branch.n_bins > 1000


def test_a_shelf_the_dsp_realizes_at_slope6_q_is_caught(slope6_report) -> None:
    """The 2026-07-27 defect class, injected at the RENDERER.

    The emitted graph asks for the Butterworth Q; the stand-in realizes the Q
    ``slope: 6`` produces. Nothing inside the fit could see that — its gate,
    residual, and VERIFY prediction all evaluate the shelf the config claims.
    """

    assert not slope6_report.matched
    tweeter = next(b for b in slope6_report.branches if b.role == "tweeter")
    assert tweeter.verdict.verdict == VERDICT_MODEL_ERROR
    assert tweeter.verdict.rollback
    assert tweeter.band_max_error_db == pytest.approx(1.7, abs=0.15)
    assert tweeter.verdict.exceedance_octaves > 1.0 / 3.0
    assert slope6_report.worst_branch is tweeter


def test_the_defect_is_isolated_to_the_branch_that_carries_a_shelf(
    slope6_report,
) -> None:
    """A harness that flags everything has told you nothing.

    The woofer's linearization is one Peaking filter, which the injection does
    not touch, so its branch must still land at the instrument's floor while
    the tweeter's is 300x above it.
    """

    woofer = next(b for b in slope6_report.branches if b.role == "woofer")
    tweeter = next(b for b in slope6_report.branches if b.role == "tweeter")
    assert woofer.verdict.verdict == VERDICT_MATCHED
    assert woofer.band_max_error_db < EXACT_RENDER_CEILING_DB
    assert tweeter.band_max_error_db > 30 * woofer.band_max_error_db


def test_the_injection_moves_only_the_shelf(slope6_report, exact_report) -> None:
    realized_q = slope6_shelf_q(DESIGNED["tweeter"][0]["gain"])
    assert realized_q == pytest.approx(0.4759, abs=1e-3)
    exact_tweeter = next(b for b in exact_report.branches if b.role == "tweeter")
    slope6_tweeter = next(b for b in slope6_report.branches if b.role == "tweeter")
    assert exact_tweeter.valid_band_hz == slope6_tweeter.valid_band_hz


# --------------------------------------------------------------------------- #
# the render contract
# --------------------------------------------------------------------------- #


def test_render_argv_is_the_pinned_one_token_gain_form(exact_report) -> None:
    for arm in (exact_report.control, exact_report.treated):
        argv = arm.determinism_receipt["argv"]
        assert len(argv) == 3
        assert argv[1] == f"--gain={loop.RENDER_FADER_DB}"
        assert argv[2] == str(arm.config_path)
        assert not any(
            token.split("=", 1)[0] in {"--statefile", "-p", "--port", "-a", "--address"}
            for token in argv
        )


def test_both_arms_render_deterministically(exact_report) -> None:
    for arm in (exact_report.control, exact_report.treated):
        receipt = arm.determinism_receipt
        assert receipt["deterministic"] is True
        assert receipt["output_sha256"] == receipt["repeat_output_sha256"]
        assert receipt["output_byte_size"] > 0


def test_the_two_arms_differ_only_by_the_filters_under_test(exact_report) -> None:
    """Everything the A/B cancels is byte-identically present in both configs."""

    import yaml

    control = yaml.safe_load(exact_report.control.config_path.read_text())
    treated = yaml.safe_load(exact_report.treated.config_path.read_text())
    assert control["mixers"] == treated["mixers"]
    assert control["devices"]["samplerate"] == treated["devices"]["samplerate"]
    assert control["devices"]["capture"] == treated["devices"]["capture"]
    linearization = {
        name for name in treated["filters"] if "_linearization_" in name
    }
    assert linearization
    assert linearization.isdisjoint(control["filters"])
    assert set(control["filters"]) == set(treated["filters"]) - linearization
    # Every shared filter DEFINITION is identical, not merely present.
    for name in control["filters"]:
        assert control["filters"][name] == treated["filters"][name]
    # And the pipeline differs only by those names being spliced in — the
    # premise the whole A/B rests on, stated as an assertion rather than left
    # to the emitter's good behaviour.
    stripped = [
        {**step, "names": [n for n in step["names"] if n not in linearization]}
        if step.get("type") == "Filter"
        else step
        for step in treated["pipeline"]
    ]
    assert stripped == control["pipeline"]


def test_a_cut_only_linearization_moves_no_program_headroom(exact_report) -> None:
    assert exact_report.expected_offset_db == pytest.approx(0.0)
    assert exact_report.control.derived.program_headroom_db == pytest.approx(0.0)


def test_the_stimulus_lead_in_covers_the_analysis_window(exact_report) -> None:
    """The window's pre-arrival span must fit inside the silence before the sweep.

    If it did not, the window would clamp at index 0 and put a step at its
    leading edge, whose rectangular-window leakage floors the whole spectrum —
    measured at about −83 dB on this bench's own case, against a true response
    that continues past −148 dB.
    """

    assert loop.STIMULUS_LEAD_IN_S * 1000.0 > ARRIVAL_PRE_MS
    assert exact_report.stimulus["lead_in_s"] == loop.STIMULUS_LEAD_IN_S


def test_the_report_is_json_serialisable(exact_report) -> None:
    payload = json.loads(json.dumps(exact_report.to_dict()))
    assert payload["matched"] is True
    assert payload["binary"]["camilladsp_version"] == "v4.1.3"
    assert payload["stimulus"]["capture_channels"] == 2
    assert payload["treated"]["derivation_receipt"]["roles"] == ["woofer", "tweeter"]
    assert len(payload["branches"]) == 2
    assert payload["branches"][0]["verdict"]["verdict"] == VERDICT_MATCHED


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_emit_kwargs_may_not_carry_the_linearization(tmp_path) -> None:
    with pytest.raises(EmitLoopError, match="must not carry 'linearization'"):
        _run(tmp_path, emit_kwargs={"linearization": {}})


def test_a_failing_binary_refuses_rather_than_grading(tmp_path) -> None:
    broken = tmp_path / "camilladsp"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(EmitLoopError, match="could not be rendered"):
        run_emit_loop(
            ActiveSpeakerPreset.from_mapping(_two_way_preset("mono")),
            playback_device=ACTIVE_PCM,
            linearization=DESIGNED,
            work_dir=tmp_path / "work",
            binary=BinaryIdentity(
                path=str(broken), version_output="CamillaDSP 4.1.3",
                sha256="0" * 64, camilladsp_build_id="broken",
            ),
            bounds=TEST_BOUNDS,
            sweep_seconds=SWEEP_SECONDS,
        )


def test_a_hot_render_refuses_instead_of_grading_an_unattributable_residual(
    tmp_path,
) -> None:
    """A render near the clip limit is not graded, because it cannot be.

    The soft clip is always on, so a branch driven hot compresses each arm by a
    different amount and the difference stops being the filters. The loop
    refuses rather than reporting a residual it cannot attribute.
    """

    # The refusal lands before any deconvolution, so this case needs a render,
    # not an accurate one — the shortest sweep keeps it cheap.
    with pytest.raises(EmitLoopError, match="soft clip"):
        _run(
            tmp_path,
            sweep_seconds=0.5,
            emit_kwargs={"limiter_clip_limit_db": -25.0},
        )


def test_a_stimulus_past_the_fft_cap_refuses_instead_of_being_truncated(
    tmp_path,
) -> None:
    """The deconvolution truncates past its cap; a truncated arm is not evidence.

    The refusal must land BEFORE any render, so this case is cheap despite the
    long sweep it asks for.
    """

    with pytest.raises(EmitLoopError, match="FFT cap"):
        _run(tmp_path, sweep_seconds=loop.MAX_STIMULUS_SECONDS + 10.0)
    assert not (tmp_path / "work" / "control.raw").exists()


def test_the_default_stimulus_fits_inside_the_fft_cap() -> None:
    from jasper.audio_measurement.sweep import synchronized_sweep_metadata

    meta = synchronized_sweep_metadata(duration_approx_s=loop.STIMULUS_SWEEP_SECONDS)
    total = loop.STIMULUS_LEAD_IN_S + meta.duration_s + loop.STIMULUS_TAIL_S
    assert total < loop.MAX_STIMULUS_SECONDS


def test_the_default_stimulus_level_clears_the_soft_clip_budget() -> None:
    """The stimulus level is CHOSEN against the budget; this pins the claim.

    At the emitter's own baseline clip limit, a branch can be no louder than the
    stimulus (the crossover and the split mixer only attenuate), so the peak
    below is the worst case.
    """

    from jasper.active_speaker.camilla_yaml import BASELINE_LIMITER_CLIP_LIMIT_DB
    from jasper.active_speaker.bench.compare import soft_clip_error_bound_db

    peak = 10.0 ** (loop.OFFLINE_STIMULUS_PEAK_DBFS / 20.0)
    bound = soft_clip_error_bound_db(
        peak, peak, clip_limit_dbfs=BASELINE_LIMITER_CLIP_LIMIT_DB
    )
    assert 0.0 < bound < SOFT_CLIP_BUDGET_DB / 5.0
    assert math.isfinite(bound)
