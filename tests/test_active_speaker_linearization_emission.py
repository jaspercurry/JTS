# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Layer-1a driver-linearization EMISSION (#1668 PR-D).

Mirrors ``test_active_speaker_local_subwoofer.py``'s keystone-safety-net
shape: the emitter (``camilla_yaml``) and the matched graph re-proof
(``runtime_contract.classify_camilla_graph`` via ``graph_evidence`` /
``_baseline_output_chain``) are built together so they cannot drift — this
module emits a linearization-bearing baseline, re-proves it against the
saved topology (must be allowed), then TAMPERS a linearization filter and
proves the re-proof fails closed. Also pins the emitter's own independent
validation gate (``_validated_linearization``) and the chain-insertion order
("immediately after the crossover HP/LP and before bass-extension").
"""

from __future__ import annotations

import pytest
import yaml

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.baseline_profile import (
    applied_program_level_delta_db,
    profile_program_headroom_db,
)
from jasper.camilla_config_contract import PeqFilter
from jasper.active_speaker.camilla_yaml import (
    MAX_LINEARIZATION_BOOST_DB,
    MAX_LINEARIZATION_FILTERS_PER_DRIVER,
    MAX_PROGRAM_HEADROOM_DB,
    driver_linearization_peak_name,
    driver_linearization_shelf_name,
    driver_linearization_taper_name,
    linearization_headroom_db,
)
from jasper.active_speaker.crossover_v2.driver_prescription import (
    DRIVER_MAX_COMPOSED_BOOST_DB,
    DRIVER_MAX_FILTER_BOOST_DB,
    MAX_SPL_SPEND_BOUND_DB,
)
from jasper.active_speaker.linearization_fit import (
    MAX_FILTERS_PER_DRIVER,
    PER_FILTER_BOOST_CAP_DB,
    _HIGHSHELF_Q,
)
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph as _classify_camilla_graph,
)

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_bass_extension_profile import _applied_baseline, _profile

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault("bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY)
    return _classify_camilla_graph(*args, **kwargs)


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _shelf(freq: float = 8000.0, gain: float = -3.0) -> dict:
    return {"biquad_type": "Highshelf", "freq": freq, "q": 0.7071067811865476, "gain": gain}


def _lowshelf(freq: float = 8400.0, gain: float = -11.0) -> dict:
    return {"biquad_type": "Lowshelf", "freq": freq, "q": 0.7071067811865476, "gain": gain}


def _taper(freq: float = 20500.0, gain: float = -5.0) -> dict:
    return {"biquad_type": "Highshelf", "freq": freq, "q": 0.7071067811865476, "gain": gain}


def _peak(freq: float = 1000.0, gain: float = -2.0, q: float = 3.0) -> dict:
    return {"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain}


def _headroom_gain_db(text: str) -> float:
    payload = yaml.safe_load(text)
    return float(payload["filters"]["active_baseline_headroom"]["parameters"]["gain"])


def _pipeline_names(text: str, *, channel: int) -> list[str]:
    payload = yaml.safe_load(text)
    for step in payload["pipeline"]:
        if step.get("channels") == [channel]:
            return list(step["names"])
    raise AssertionError(f"no pipeline step for channel {channel}")


# --------------------------------------------------------------------------- #
# constants pinning
# --------------------------------------------------------------------------- #


def test_max_linearization_filters_matches_fit_engine_cap():
    """LOCKSTEP pin (camilla_yaml.py's own comment): the emitter's count cap
    must equal the fit engine's own MAX_FILTERS_PER_DRIVER, or a legitimate
    fit-engine-produced candidate could be silently rejected by the emitter's
    independent re-validation."""
    assert MAX_LINEARIZATION_FILTERS_PER_DRIVER == MAX_FILTERS_PER_DRIVER


def test_max_linearization_boost_matches_fit_engine_cap():
    """The PR-L5 twin of the count pin, for the same reason: the emitter holds
    its own copy of the per-filter boost ceiling so it re-validates rather than
    trusts, and a drift between the two would silently refuse a legitimate
    fit-engine boost at the emitter boundary."""
    assert MAX_LINEARIZATION_BOOST_DB == PER_FILTER_BOOST_CAP_DB


# --------------------------------------------------------------------------- #
# chain order + byte-identical-absent
# --------------------------------------------------------------------------- #


def test_linearization_absent_is_byte_identical_to_no_linearization_param():
    """The empty default and an explicitly-empty linearization mapping must
    produce byte-identical YAML — the invariant every existing caller of
    emit_active_speaker_baseline_config relies on."""
    preset = _preset()
    default = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    explicit_empty = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization={},
    )
    assert default == explicit_empty
    assert "linearization" not in default


def test_linearization_chain_order_after_crossover_before_bass_extension():
    """Pins the RULED insertion slot: linearization sits immediately after
    the crossover HP/LP and before bass-extension, for both the tweeter
    (shelf + peak) and the woofer (peak only)."""
    preset = _preset()
    linearization = {
        "tweeter": [_shelf(), _peak(3400.0, -1.5)],
        "woofer": [_peak(900.0, -1.0)],
    }
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=linearization,
    )
    tweeter_names = _pipeline_names(text, channel=1)
    woofer_names = _pipeline_names(text, channel=0)

    assert tweeter_names == [
        "as_tweeter_woofer_tweeter_hp",
        driver_linearization_shelf_name("tweeter"),
        driver_linearization_peak_name("tweeter", 1),
        "as_tweeter_delay",
        "as_tweeter_baseline_gain",
        "as_tweeter_baseline_limiter",
    ]
    assert woofer_names == [
        "as_woofer_woofer_tweeter_lp",
        driver_linearization_peak_name("woofer", 1),
        "as_woofer_delay",
        "as_woofer_baseline_gain",
        "as_woofer_baseline_limiter",
    ]


def test_linearization_shelf_uses_the_fit_engines_butterworth_q():
    """The emitted Highshelf must carry the fit engine's own Butterworth Q as
    CamillaDSP's ``q``, and no ``slope``.

    Getting this wrong emits a DIFFERENT shelf than the fit designed, and
    invisibly: the fit's realization gate, residual, and VERIFY prediction all
    evaluate the Butterworth shelf. That is exactly what ``slope: 6.0`` did
    here until 2026-07-27 (CamillaDSP's Butterworth is ``slope: 12``; at
    ``slope: 6`` the realized Q falls with gain — 0.476 at -11 dB)."""
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_shelf(freq=6500.0, gain=-4.0)]},
    )
    payload = yaml.safe_load(text)
    params = payload["filters"][driver_linearization_shelf_name("tweeter")]["parameters"]
    assert params["type"] == "Highshelf"
    assert params["freq"] == pytest.approx(6500.0)
    assert params["gain"] == pytest.approx(-4.0)
    assert params["q"] == pytest.approx(_HIGHSHELF_Q, abs=5e-8)
    assert "slope" not in params


def test_linearization_lowshelf_led_chain_order():
    """CD-horn sibling of the chain-order test: a Lowshelf-led chain (backbone
    at position 0 + peaks) names position 0 with the shelf name, exactly like
    a Highshelf-led chain — the emitter treats both shelf types identically."""
    preset = _preset()
    linearization = {"tweeter": [_lowshelf(), _peak(6000.0, -5.0), _peak(11000.0, -1.0)]}
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=linearization,
    )
    tweeter_names = _pipeline_names(text, channel=1)
    assert tweeter_names == [
        "as_tweeter_woofer_tweeter_hp",
        driver_linearization_shelf_name("tweeter"),
        driver_linearization_peak_name("tweeter", 1),
        driver_linearization_peak_name("tweeter", 2),
        "as_tweeter_delay",
        "as_tweeter_baseline_gain",
        "as_tweeter_baseline_limiter",
    ]


def test_linearization_lowshelf_uses_the_same_butterworth_q():
    """The CD-horn Lowshelf backbone carries the same Butterworth ``q`` as the
    Highshelf — one shelf steepness for both types, no ``slope``."""
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_lowshelf(freq=8200.0, gain=-9.0)]},
    )
    payload = yaml.safe_load(text)
    params = payload["filters"][driver_linearization_shelf_name("tweeter")]["parameters"]
    assert params["type"] == "Lowshelf"
    assert params["freq"] == pytest.approx(8200.0)
    assert params["gain"] == pytest.approx(-9.0)
    assert params["q"] == pytest.approx(_HIGHSHELF_Q, abs=5e-8)
    assert "slope" not in params


def test_linearization_taper_gets_its_own_name_and_definition():
    """The trailing Highshelf taper (after a Lowshelf lead) gets the distinct
    taper name and a Highshelf definition with the fixed shelf slope — so a
    backbone shelf and a taper never collide on one filter name."""
    preset = _preset()
    linearization = {"tweeter": [_lowshelf(), _peak(6000.0, -4.0), _taper(freq=20500.0, gain=-5.0)]}
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=linearization,
    )
    tweeter_names = _pipeline_names(text, channel=1)
    assert tweeter_names == [
        "as_tweeter_woofer_tweeter_hp",
        driver_linearization_shelf_name("tweeter"),
        driver_linearization_peak_name("tweeter", 1),
        driver_linearization_taper_name("tweeter"),
        "as_tweeter_delay",
        "as_tweeter_baseline_gain",
        "as_tweeter_baseline_limiter",
    ]
    payload = yaml.safe_load(text)
    params = payload["filters"][driver_linearization_taper_name("tweeter")]["parameters"]
    assert params["type"] == "Highshelf"
    assert params["freq"] == pytest.approx(20500.0)
    assert params["gain"] == pytest.approx(-5.0)
    assert params["q"] == pytest.approx(_HIGHSHELF_Q, abs=5e-8)
    assert "slope" not in params


def test_baseline_write_logs_the_shelf_realization(tmp_path, caplog):
    """The write-time event dates the PR-L2 emission change per speaker.

    Re-emitting an ALREADY-APPLIED profile changes what the speaker plays (the
    same stored design is finally realized at the Q it was designed at), so the
    transition has to be legible after the fact — ``grep
    event=active_speaker_baseline_config_written``. Pinned because
    docs/HANDOFF-sound-preferences.md tells operators to look for these fields.
    """
    preset = _preset()
    out = tmp_path / "baseline.yml"
    with caplog.at_level("INFO", logger="jasper.active_speaker.camilla_yaml"):
        emit_active_speaker_baseline_config(
            preset,
            playback_device=ACTIVE_PCM,
            out_path=out,
            linearization={
                "tweeter": [_lowshelf(), _peak(6000.0, -4.0), _taper()],
                "woofer": [_peak(300.0, -2.0)],
            },
        )
    written = [
        record.getMessage()
        for record in caplog.records
        if "event=active_speaker_baseline_config_written" in record.getMessage()
    ]
    assert len(written) == 1
    # Two shelf-slot filters on the tweeter (backbone + taper), none on the
    # woofer's peak-only chain.
    assert "linearization_shelves=2" in written[0]
    assert f"shelf_q={_HIGHSHELF_Q:.7f}" in written[0]
    # ...and the field reports what the file actually carries.
    params = yaml.safe_load(out.read_text())["filters"][
        driver_linearization_shelf_name("tweeter")
    ]["parameters"]
    assert params["q"] == pytest.approx(_HIGHSHELF_Q, abs=5e-8)


def test_linearization_peak_carries_its_own_q():
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(1200.0, -2.5, q=4.25)]},
    )
    payload = yaml.safe_load(text)
    params = payload["filters"][driver_linearization_peak_name("woofer", 1)]["parameters"]
    assert params["type"] == "Peaking"
    assert params["freq"] == pytest.approx(1200.0)
    assert params["q"] == pytest.approx(4.25)
    assert params["gain"] == pytest.approx(-2.5)


def test_linearization_multiple_peaks_number_in_fit_order():
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(500.0, -1.0), _peak(2000.0, -3.0)]},
    )
    names = _pipeline_names(text, channel=0)
    assert names[1] == driver_linearization_peak_name("woofer", 1)
    assert names[2] == driver_linearization_peak_name("woofer", 2)


def test_linearization_coexists_with_bass_extension_in_ruled_order():
    """The two addons compose: linearization then bass-extension, both
    between the crossover and the delay/gain/limiter tail."""
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    from dataclasses import replace

    profile = replace(
        _profile(topology=topology, applied_baseline=applied),
        bass_owner={"kind": "woofer_way", "roles": ["woofer"], "channels": [0]},
    )
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(900.0, -1.0)]},
        bass_extension_profile=profile,
    )
    names = _pipeline_names(text, channel=0)
    assert names == [
        "as_woofer_woofer_tweeter_lp",
        driver_linearization_peak_name("woofer", 1),
        "bass_ext_lt",
        "bass_ext_subsonic",
        "as_woofer_delay",
        "as_woofer_baseline_gain",
        "as_woofer_baseline_limiter",
    ]


# --------------------------------------------------------------------------- #
# _validated_linearization — the independent re-validation gate
# --------------------------------------------------------------------------- #


def test_linearization_unknown_role_is_dropped_not_raised():
    """Mirrors _validated_driver_corrections's own unknown-role handling."""
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"midrange": [_peak()]},
    )
    assert "midrange" not in text


# "Lowshelf" is now a SUPPORTED shelf type (#1668 CD-horn backbone), so it is
# no longer in this bad-type list; a single-entry Lowshelf-led chain is a valid
# leading shelf. Its acceptance + placement rules are pinned separately below.
@pytest.mark.parametrize("bad_type", ["Notch", "", None, 42])
def test_linearization_rejects_unsupported_biquad_type(bad_type):
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="biquad_type"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [{**_peak(), "biquad_type": bad_type}]},
        )


def test_linearization_rejects_boost_above_the_per_filter_cap():
    """PR-L5 replaced the emitter's blanket ``gain > 0`` refusal with the
    per-filter realization cap. The refusal itself did not go away — a single
    biquad asked for more than :data:`MAX_LINEARIZATION_BOOST_DB` stops being
    a faithful realization of the shape the fit drew, exactly as on the cut
    side, and is still refused rather than clamped."""
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="must not exceed"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [_peak(gain=MAX_LINEARIZATION_BOOST_DB + 0.5)]},
        )


def test_linearization_boost_is_accepted_and_absorbed_by_baseline_headroom():
    """The PR-L5 doctrine amendment, at the emitter: a boost is emitted as
    asked, and the program-domain ``active_baseline_headroom`` gain grows by
    what the worst branch's emitted CHAIN actually puts above unity, so the
    boosted band still lands at or under it. That absorption — not a numeric
    cap on the correction — is what keeps CamillaDSP's 0 dB ceiling a hard rail
    while total boost stays uncapped.

    The charged number is the chain's realized peak plus
    ``branch_chain.HEADROOM_MARGIN_DB``, not the sum of the two gains (#1808).
    This fixture is exactly the shape that separates them: a +3.0 dB bell at
    1000 Hz and a +1.5 dB bell at 2200 Hz never meet, and BOTH are already
    down the woofer's own 1600 Hz low-pass — the 2200 Hz one by 4.6 dB, inside
    the stopband. The sum charged 4.5 dB for a chain that peaks at 1.86 dB."""
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    boosted = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(gain=3.0), _peak(freq=2200.0, gain=1.5)]},
    )
    assert "gain: 3.000" in boosted
    charged = _headroom_gain_db(flat) - _headroom_gain_db(boosted)
    assert charged == pytest.approx(2.8693, abs=1e-3)
    assert charged < 4.5  # the retired sum-of-positives rule


def test_linearization_headroom_takes_the_worst_branch_not_the_sum():
    """Driver chains run in PARALLEL after the split, so no single sample path
    sees two branches' boosts — absorbing their sum would throw away max SPL
    for a clip that cannot happen. (#1808 changed the per-branch number this
    maxes over, not this adjudication.)"""
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    both = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={
            "woofer": [_peak(gain=3.0)],
            "tweeter": [_peak(freq=6000.0, gain=2.0)],
        },
    )
    def _charged(linearization) -> float:
        return _headroom_gain_db(flat) - _headroom_gain_db(
            emit_active_speaker_baseline_config(
                preset, playback_device=ACTIVE_PCM, linearization=linearization,
            )
        )

    woofer_alone = _charged({"woofer": [_peak(gain=3.0)]})
    tweeter_alone = _charged({"tweeter": [_peak(freq=6000.0, gain=2.0)]})
    # The pair costs exactly what the WORSE branch alone costs, never the sum.
    # (Here that is the tweeter: its +2 dB bell at 6 kHz sits in its own
    # passband, while the woofer's +3 dB bell at 1 kHz is already 1.2 dB down
    # its own 1600 Hz low-pass — which is #1808's point restated.)
    assert _headroom_gain_db(both) == pytest.approx(
        _headroom_gain_db(flat) - max(woofer_alone, tweeter_alone), abs=1e-3
    )
    assert max(woofer_alone, tweeter_alone) == pytest.approx(2.9641, abs=1e-3)
    assert _headroom_gain_db(both) > _headroom_gain_db(flat) - (
        woofer_alone + tweeter_alone
    )


def test_linearization_headroom_is_zero_for_a_cut_only_correction():
    """Every pre-PR-L5 fit is cut-only, and its emitted graph must be
    byte-identical to what it was before boost existed."""
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    cut_only = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(gain=-4.0)]},
    )
    assert linearization_headroom_db(
        {"woofer": [_peak(gain=-4.0)]}, branch_context={},
    ) == 0.0
    assert _headroom_gain_db(cut_only) == pytest.approx(_headroom_gain_db(flat))


# --------------------------------------------------------------------------- #
# what a PRESCRIBED per-driver boost costs (PR-A)
#
# The gate in `crossover_v2.driver_prescription` bounds what an outside reader
# may propose; these pin what that bound COSTS once it reaches the emitter, on
# the same LR4-at-1600 two-way preset the rest of this module uses. The claim
# under review is that the whole cost is maximum SPL — the graph attenuates
# before the split, so a boosted graph is never louder at any frequency than an
# unboosted one at full scale.
# --------------------------------------------------------------------------- #


def test_a_prescribable_boost_is_charged_its_realized_chain_peak():
    """The deepest ONE admissible prescribed boost, priced.

    +3.0 dB at Q 8 on 6245 Hz is exactly ``DRIVER_MAX_FILTER_BOOST_DB`` on a
    feature the 2026-08-19 record classifies boostable. It sits in the
    tweeter's own passband, 0.0301 dB down its 1600 Hz LR4 high-pass — so the
    realized chain peak is 2.9699 dB, not the 3.0 the filter asks for, and the
    charge is that plus ``branch_chain.HEADROOM_MARGIN_DB``.
    """
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    boosted = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6245.0, DRIVER_MAX_FILTER_BOOST_DB, q=8.0)]},
    )

    assert DRIVER_MAX_FILTER_BOOST_DB == 3.0, "the fixture is the ceiling itself"
    assert _headroom_gain_db(flat) == 0.0
    assert _headroom_gain_db(boosted) == pytest.approx(-3.9699, abs=1e-3)
    assert _headroom_gain_db(boosted) > -MAX_SPL_SPEND_BOUND_DB


def test_the_prescription_boost_cap_is_what_keeps_the_charge_under_the_bound():
    """The load-bearing arithmetic: the gate's 4.0 dB composed cap IS the 5.0.

    Two +3.0 dB Q-8 boosts 0.12 octaves apart compose to 3.993 dB on the gate's
    own evaluation — just inside ``DRIVER_MAX_COMPOSED_BOOST_DB`` — and the
    emitter charges 4.970 dB for them. Move them 0.005 octaves closer and the
    gate REFUSES at 4.062 dB composed, which would have charged 5.021 dB. So
    the published bound is not a slogan beside the cap; it is the cap.
    """
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)

    def charged(separation_octaves: float) -> float:
        text = emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"tweeter": [
                _peak(6245.0, 3.0, q=8.0),
                _peak(6245.0 * 2 ** separation_octaves, 3.0, q=8.0),
            ]},
        )
        return _headroom_gain_db(flat) - _headroom_gain_db(text)

    assert MAX_SPL_SPEND_BOUND_DB == DRIVER_MAX_COMPOSED_BOOST_DB + 1.0
    assert charged(0.12) == pytest.approx(4.9704, abs=1e-3)
    assert charged(0.12) < MAX_SPL_SPEND_BOUND_DB
    # The first arrangement past the gate's cap, and it is past the bound too.
    assert charged(0.115) == pytest.approx(5.0211, abs=1e-3)
    assert charged(0.115) > MAX_SPL_SPEND_BOUND_DB


@pytest.mark.parametrize(("freq", "gain", "expected_charge"), [
    # The shallowest admissible boost in the branch's own passband: the charge
    # is a STEP, because the margin is paid in full the moment a chain crosses
    # unity at all. This is what "the first dB costs about 1.5" means.
    (6245.0, 0.5, 1.4699),
    (6245.0, 1.5, 2.4699),
    (6245.0, 3.0, 3.9699),
    # Buried in the tweeter's own crossover stopband (58.3 dB down at 300 Hz):
    # the chain never exceeds unity, so nothing is charged and the household
    # gives up no maximum SPL at all.
    (300.0, 3.0, 0.0),
])
def test_the_boost_charge_is_a_step_function_not_a_proportional_one(
    freq, gain, expected_charge,
):
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(freq, gain, q=8.0)]},
    )

    assert _headroom_gain_db(flat) - _headroom_gain_db(text) == pytest.approx(
        expected_charge, abs=1e-3
    )


def test_a_prescribable_boost_reproves_against_the_graph_it_emitted():
    """The runtime contract re-derives the chain peak off the graph TEXT.

    Nothing about the prescription reaches this proof — it reads the emitted
    filters, the emitted crossover and the emitted headroom gain — so an
    admitted boost has to survive a re-derivation that never saw the gate.
    """
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6245.0, 3.0, q=8.0)]},
    )

    graph = classify_camilla_graph(topology=topology, text=text)

    assert graph.allowed is True, graph.issues
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


def test_the_emitter_still_refuses_a_boost_past_its_own_rail():
    """The gate's 3.0 dB ceiling is the FIRST of two, never the only one.

    PR-A opens a narrower permission inside a rail that already existed and is
    unchanged: the emitter raises rather than clamps at 12 dB, whatever any
    intake accepted.
    """
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_baseline_config(
            _preset(), playback_device=ACTIVE_PCM,
            linearization={"tweeter": [
                _peak(6245.0, MAX_LINEARIZATION_BOOST_DB + 0.1, q=8.0),
            ]},
        )


def test_the_headroom_charge_cannot_be_asked_for_without_a_branch_context():
    """**Required, not defaulted.** With no context the charge would silently
    become the naked linearization cascade's peak — a strict over-estimate, so
    still SAFE for an attenuation, but wrong for anyone reading it as what the
    correction costs, and wrong in the LOUD direction for a delta between two
    of them. There is no signature that lets a caller skip it by accident."""
    with pytest.raises(TypeError, match="branch_context"):
        linearization_headroom_db({"woofer": [_peak(gain=3.0)]})  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# the apply-boundary level delta (#1811)
# --------------------------------------------------------------------------- #


def _delta_profile(linearization: dict | None, *, mirror_only: bool = False) -> dict:
    """A profile mapping carrying what the delta reader consumes.

    The snapshot carries the ``preset`` and ``corrections`` too, because since
    #1808 the headroom charge is evaluated over the branch's WHOLE chain
    (crossover ⊗ linearization ⊗ trim) — a fixture without them would read the
    naked-cascade fallback and the "proven against the emitter" test below
    would be comparing two different chains.
    """
    if linearization is None:
        return {"kind": "active_speaker_baseline_profile"}
    if mirror_only:
        # An era-older frozen profile: the top-level convenience mirror without
        # a snapshot copy — and so, deliberately, without a branch context.
        return {"linearization": linearization}
    return {
        "linearization": linearization,
        "recomposition_snapshot": {
            "linearization": linearization,
            "preset": _preset().to_dict(),
            "corrections": {
                role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
                for role in ("woofer", "tweeter")
            },
        },
    }


def test_applied_program_level_delta_is_the_emitted_headroom_move():
    """#1811's declared offset is the graph's OWN broadband move — proven
    against the emitter, not asserted about it.

    ``classify_delta_probe`` subtracts this number from the post-apply capture
    before grading it, so a drift between this reader and
    ``active_baseline_headroom`` would put a systematic error straight into
    every verdict. Both sides come from ``linearization_headroom_db``; this
    test is what keeps that true.
    """
    preset = _preset()
    lin = {"woofer": [_peak(gain=3.0), _peak(freq=2200.0, gain=1.5)]}
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    boosted = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=lin,
    )
    emitted_move_db = _headroom_gain_db(boosted) - _headroom_gain_db(flat)
    # The PEAK-rule value (#1808), not the old sum's -4.5: the +1.5 dB bell at
    # 2.2 kHz sits deep in the woofer's own 1600 Hz low-pass and so adds ~0.05
    # dB to the branch's realized peak rather than its full 1.5. The margin is
    # already inside this number.
    assert emitted_move_db == pytest.approx(-2.8693, abs=1e-3)
    # Tolerance is the EMITTED config's own quantization (the gain is written
    # to 4 dp), not slack in the agreement: the reader returns
    # -2.8692747…, the YAML carries -2.8693.
    assert applied_program_level_delta_db(
        _delta_profile({}), _delta_profile(lin),
    ) == pytest.approx(emitted_move_db, abs=1e-4)


def test_applied_program_level_delta_reads_any_magnitude_including_small():
    """Read from the profiles, never assumed — whatever the charge rule of the
    day produces, at whatever size, in either direction."""
    big = _delta_profile({"woofer": [_peak(gain=22.458)]})
    small = _delta_profile({"woofer": [_peak(gain=4.0)]})
    none_ = _delta_profile({})
    assert applied_program_level_delta_db(none_, big) == pytest.approx(
        -22.2373, abs=1e-3
    )
    assert applied_program_level_delta_db(none_, small) == pytest.approx(
        -3.8101, abs=1e-3
    )
    # Re-tuning a heavily-charged profile down to a light one hands headroom
    # BACK — a positive move, and a downward correction for the probe.
    assert applied_program_level_delta_db(big, small) == pytest.approx(
        18.4271, abs=1e-3
    )


def test_applied_program_level_delta_is_era_tolerant_and_snapshot_first():
    # First-ever apply: nothing to compare against, so the whole applied charge
    # is the move.
    assert applied_program_level_delta_db(
        None, _delta_profile({"woofer": [_peak(gain=3.0)]}),
    ) == pytest.approx(-2.8194, abs=1e-3)
    # A pre-linearization profile on either side contributes 0.0, never a raise.
    assert profile_program_headroom_db(_delta_profile(None)) == 0.0
    assert profile_program_headroom_db(None) == 0.0
    assert applied_program_level_delta_db(_delta_profile(None), _delta_profile(None)) == 0.0
    # A cut-only correction costs nothing, so it moves nothing.
    assert applied_program_level_delta_db(
        _delta_profile({}), _delta_profile({"woofer": [_peak(gain=-4.0)]}),
    ) == 0.0
    # The snapshot copy is authoritative; the top-level mirror is the
    # era-older fallback. Both read 4.0 here, not 2.82, because neither of
    # these two profiles carries a preset/corrections snapshot — so the charge
    # falls back to the naked cascade's peak plus margin, the documented
    # over-estimate. What is under test is WHICH linearization was read.
    lin = {"woofer": [_peak(gain=3.0)]}
    assert profile_program_headroom_db(
        _delta_profile(lin, mirror_only=True),
    ) == pytest.approx(4.0, abs=1e-3)
    assert profile_program_headroom_db({
        "linearization": {"woofer": [_peak(gain=9.0)]},
        "recomposition_snapshot": {"linearization": lin},
    }) == pytest.approx(4.0, abs=1e-3)


def test_a_profile_without_a_branch_context_over_estimates_rather_than_under():
    """The fallback direction is load-bearing (#1811 + #1808).

    A profile whose snapshot cannot yield a preset/corrections pair is charged
    over the naked linearization cascade — no crossover credited, no trim —
    which is a strict OVER-estimate of what the branch really does. For a
    declared apply-boundary offset that is the safe direction: the probe
    subtracts more than the graph moved, so the difference stays visible in
    ``residual_offset_db`` instead of being quietly absorbed. Under-estimating
    would hide it.
    """
    lin = {"woofer": [_peak(gain=3.0)]}
    with_context = profile_program_headroom_db(_delta_profile(lin))
    without = profile_program_headroom_db(
        {"recomposition_snapshot": {"linearization": lin}},
    )
    assert without > with_context
    # …and a snapshot whose preset cannot be parsed degrades the same way
    # rather than raising on the apply path.
    assert profile_program_headroom_db({
        "recomposition_snapshot": {
            "linearization": lin, "preset": {"nonsense": True},
            "corrections": {"woofer": {"gain_db": 0.0}},
        },
    }) == pytest.approx(without, abs=1e-6)


@pytest.mark.parametrize("bad_freq", [0.0, -100.0, float("nan"), float("inf")])
def test_linearization_rejects_non_positive_or_non_finite_freq(bad_freq):
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [{**_peak(), "freq": bad_freq}]},
        )


@pytest.mark.parametrize("bad_q", [0.0, -1.0, float("nan")])
def test_linearization_rejects_non_positive_or_non_finite_q(bad_q):
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [{**_peak(), "q": bad_q}]},
        )


def test_linearization_count_cap_raises():
    preset = _preset()
    filters = [_peak(1000.0 + 100.0 * i, -0.5) for i in range(MAX_LINEARIZATION_FILTERS_PER_DRIVER + 1)]
    with pytest.raises(ActiveSpeakerConfigError, match="exceeds"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": filters},
        )


def test_linearization_at_exactly_the_cap_is_allowed():
    preset = _preset()
    filters = [_peak(1000.0 + 100.0 * i, -0.5) for i in range(MAX_LINEARIZATION_FILTERS_PER_DRIVER)]
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": filters},
    )
    names = _pipeline_names(text, channel=0)
    assert sum(1 for n in names if "linearization_peak" in n) == MAX_LINEARIZATION_FILTERS_PER_DRIVER


def test_linearization_non_list_filters_raises():
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="must be a list"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": "not-a-list"},  # type: ignore[dict-item]
        )


def test_linearization_non_mapping_entry_raises():
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="must be a mapping"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": ["not-a-mapping"]},  # type: ignore[list-item]
        )


def test_linearization_lowshelf_led_chain_is_accepted():
    """A single leading Lowshelf, and a Lowshelf-led chain with a trailing
    Highshelf taper, are BOTH valid shelf structures (#1668) — no raise."""
    preset = _preset()
    # single leading Lowshelf
    emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization={"tweeter": [_lowshelf()]},
    )
    # Lowshelf lead + peaks + trailing taper
    emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_lowshelf(), _peak(6000.0, -3.0), _taper()]},
    )


def test_linearization_rejects_two_leading_shelves():
    """Two shelf-type entries where the second is not a valid trailing taper
    (here a Highshelf mid-chain after a Lowshelf lead) must fail closed."""
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="shelf placement"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"tweeter": [_lowshelf(), _shelf(6000.0, -2.0), _peak(3000.0, -1.0)]},
        )


def test_linearization_rejects_taper_without_lowshelf_lead():
    """A trailing Highshelf taper is only valid after a LOWshelf lead — after a
    peak-led (or Highshelf-led) chain it is an illegal second shelf."""
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="shelf placement"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"tweeter": [_peak(3000.0, -1.0), _taper()]},
        )


def test_linearization_rejects_taper_not_last():
    """A Highshelf taper that is not the trailing entry (a shelf mid-chain) is
    rejected — the fit engine only ever emits it last."""
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="shelf placement"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"tweeter": [_lowshelf(), _taper(), _peak(3000.0, -1.0)]},
        )


# --------------------------------------------------------------------------- #
# runtime_contract re-proof — the emit<->re-proof keystone safety net
# --------------------------------------------------------------------------- #


def test_linearized_baseline_reproves_as_approved_active_runtime():
    topology = _active_topology("mono", "active_2_way")
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={
            "tweeter": [_shelf(), _peak(3400.0, -1.5)],
            "woofer": [_peak(900.0, -1.0)],
        },
    )
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, graph.issues
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


def test_boosted_baseline_reproves_as_approved_active_runtime():
    """PR-L5: a boost the emitter itself absorbed re-proves as approved. The
    graph carries its own evidence — the ``active_baseline_headroom`` gain the
    boost was paid for with — so the proof stays self-contained, exactly as
    ``_consume_linearization_chain`` promises."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6000.0, 3.0), _peak(9000.0, 1.5)]},
    )
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, graph.issues
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


def test_reproof_blocks_boost_beyond_the_absorbed_headroom():
    """The boost proof's teeth: a boost tampered UP past what the graph's own
    headroom gain paid for would drive the chain past unity, and fails closed.
    Tampered on a graph that legitimately carries absorbed boost, so what is
    being caught is the EXCESS, not the presence of boost."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6000.0, 3.0), _peak(9000.0, 1.5)]},
    )
    payload = yaml.safe_load(text)
    # The realized peak of two non-overlapping bells is the taller one, plus
    # the margin — 4.108, not the 4.5 their sum would have charged (#1808).
    assert payload["filters"]["active_baseline_headroom"]["parameters"]["gain"] == \
        pytest.approx(-4.1165, abs=1e-3)
    name = driver_linearization_peak_name("tweeter", 1)
    payload["filters"][name]["parameters"]["gain"] = 8.0  # total 9.5 > 4.5
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_reproof_blocks_boost_stacked_within_per_filter_cap_but_over_allowance():
    """Per-branch TOTAL, not per-filter maximum. Two boosts each individually
    inside the allowance can still add past it in one chain — which is the
    whole reason the emitter absorbs the sum."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6000.0, 3.0), _peak(9000.0, 1.5)]},
    )
    payload = yaml.safe_load(text)
    for i, gain in ((1, 4.0), (2, 4.0)):  # 8.0 total vs a 4.5 allowance
        payload["filters"][driver_linearization_peak_name("tweeter", i)][
            "parameters"
        ]["gain"] = gain
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


@pytest.mark.parametrize("bad_gain", [2.0, 0.5])
def test_reproof_blocks_tampered_positive_gain_linearization_filter(bad_gain):
    """The graph_safety keystone: a positive-gain linearization filter
    tampered directly into the YAML (simulating a corrupted statefile, since
    the emitter itself can never produce one) must fail closed at re-proof.

    PR-L5 made the rail "boost within the graph's own absorbed headroom"
    rather than "no boost at all", and this test still passes UNCHANGED —
    which is the point. A cut-only correction absorbs nothing, so its
    allowance is 0.0 and any tampered-in boost is still refused. The allowance
    can only grow when the same emit that placed a boost also paid for it."""
    topology = _active_topology("mono", "active_2_way")
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(3400.0, -1.5)]},
    )
    payload = yaml.safe_load(text)
    name = driver_linearization_peak_name("tweeter", 1)
    payload["filters"][name]["parameters"]["gain"] = bad_gain
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_reproof_blocks_tampered_wrong_biquad_subtype():
    """A linearization-named filter whose actual TYPE was tampered away from
    Peaking must also fail closed (not just the gain sign)."""
    topology = _active_topology("mono", "active_2_way")
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(3400.0, -1.5)]},
    )
    payload = yaml.safe_load(text)
    name = driver_linearization_peak_name("tweeter", 1)
    payload["filters"][name]["parameters"]["type"] = "Highshelf"
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_reproof_blocks_reversed_shelf_and_peak_order():
    """The fit's construction guarantee is shelf-before-peaks (see
    _driver_linearization_chain_names's docstring); the prover does not
    re-derive that order, it proves the emitted names match it positionally
    (_consume_linearization_chain). Reversing the shelf and its first peak
    in the compiled pipeline must fail closed, not silently accept a
    reordered chain -- #1668 PR-D review SF2."""
    topology = _active_topology("mono", "active_2_way")
    preset = _preset()
    text = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_shelf(), _peak(3400.0, -1.5)]},
    )
    payload = yaml.safe_load(text)
    shelf_name = driver_linearization_shelf_name("tweeter")
    peak_name = driver_linearization_peak_name("tweeter", 1)
    for step in payload["pipeline"]:
        if step.get("channels") == [1]:
            names = step["names"]
            i, j = names.index(shelf_name), names.index(peak_name)
            names[i], names[j] = names[j], names[i]
            break
    else:
        raise AssertionError("no pipeline step for channel 1")
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_reproof_allows_unrelated_filter_name_between_crossover_and_tail():
    """A stray filter name that does NOT match the linearization naming
    convention is not silently consumed — it correctly falls through to the
    ordinary tail check and fails closed as an unrecognized chain (proves
    _consume_linearization_chain's "unrecognized name -> zero consumed"
    contract doesn't accidentally widen into a general bypass)."""
    topology = _active_topology("mono", "active_2_way")
    preset = _preset()
    text = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    payload = yaml.safe_load(text)
    # Inject an unrelated Biquad filter into the tweeter's post-crossover
    # chain, named OUTSIDE the linearization convention.
    payload["filters"]["as_tweeter_mystery"] = {
        "type": "Biquad",
        "parameters": {"type": "Peaking", "freq": 5000.0, "q": 1.0, "gain": -1.0},
    }
    for step in payload["pipeline"]:
        if step.get("channels") == [1]:
            step["names"].insert(1, "as_tweeter_mystery")
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


# --------------------------------------------------------------------------- #
# adversarial-review regressions (round 2)
# --------------------------------------------------------------------------- #


def test_room_peq_boost_is_not_spendable_as_linearization_headroom():
    """**S1 regression.** ``active_baseline_headroom`` absorbs room-correction
    boost AND linearization boost. Reading its whole magnitude as the
    linearization allowance let a tampered linearization filter spend headroom
    already committed to the room PEQs — the two together could then clip while
    the graph proved safe.

    Here the emitted linearization is CUT-ONLY, so linearization's own share of
    the headroom is zero; the 8 dB of room boost sitting in the same gain must
    not license a tampered +5 dB linearization filter.
    """
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        room_peqs=(PeqFilter(freq=120.0, q=1.0, gain=8.0),),
        linearization={"tweeter": [_peak(3400.0, -1.5)]},
    )
    payload = yaml.safe_load(text)
    # The graph really does carry 8 dB of absorbed room boost…
    assert payload["filters"]["active_baseline_headroom"]["parameters"]["gain"] == \
        pytest.approx(-8.0, abs=1e-3)
    # …and none of it is linearization's to spend.
    payload["filters"][driver_linearization_peak_name("tweeter", 1)][
        "parameters"
    ]["gain"] = 5.0
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_linearization_boost_beside_room_peq_boost_still_proves():
    """The control: when the emitter charged for BOTH, both are covered and the
    graph proves — the fix attributes the share, it does not forbid coexistence."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        room_peqs=(PeqFilter(freq=120.0, q=1.0, gain=8.0),),
        linearization={"tweeter": [_peak(6000.0, 3.0)]},
    )
    payload = yaml.safe_load(text)
    # 8 dB of room boost + the linearization branch's own realized peak
    # (3 dB of bell, less a hair of the tweeter's high-pass at 6 kHz) + margin.
    assert payload["filters"]["active_baseline_headroom"]["parameters"]["gain"] == \
        pytest.approx(-11.9641, abs=1e-3)
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, graph.issues


def test_runaway_program_headroom_is_refused_by_name_not_silently_muted():
    """**N7.** Total boost is uncapped, and absorption turns every dB of it into
    a dB of pre-split attenuation — so left unbounded the failure mode is a
    graph that is, to a household, simply mute, with nothing naming why. Past a
    generous ceiling the emitter REFUSES instead.

    **COINCIDENT boosts since #1808.** The charge is the chain's realized peak,
    so spreading N per-filter-capped bells across the spectrum no longer adds
    up to anything — which is the whole point of the change. Stacking them at
    ONE frequency does add, and that is the shape that can still run the
    program-domain headroom away."""
    preset = _preset()
    over = MAX_PROGRAM_HEADROOM_DB + 1.0
    n = int(over // MAX_LINEARIZATION_BOOST_DB) + 1
    filters = [
        _peak(6000.0, min(MAX_LINEARIZATION_BOOST_DB, over - i * MAX_LINEARIZATION_BOOST_DB))
        for i in range(n)
    ]
    filters = [f for f in filters if f["gain"] > 0.0]
    with pytest.raises(ActiveSpeakerConfigError, match="exceeds"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"tweeter": filters},
        )


def test_a_generous_program_headroom_still_emits():
    """The ceiling is a refusal for a defect upstream, not a cap on ordinary
    correction — a realistic deep boost is well inside it."""
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6000.0, 9.0), _peak(9000.0, 6.0)]},
    )
    # The taller bell plus the margin — the two never meet, so #1808 charges
    # 10.6 dB where the sum charged 15.
    assert _headroom_gain_db(text) == pytest.approx(-10.606, abs=1e-3)


def test_the_production_emit_path_uses_the_default_baseline_headroom():
    """The boost proof subtracts the module DEFAULT ``BASELINE_HEADROOM_DB``
    where the emitter adds a CALLER-SUPPLIED ``baseline_headroom_db`` (0..40).
    They agree only because every production emit path takes the default — so
    that coincidence is pinned here rather than left to be discovered.

    A caller passing a non-default value would make the allowance generous by
    exactly that amount (never tight — the same direction as the documented
    ``output_trim_db`` slack), and this is what catches it.
    """
    import inspect

    from jasper.active_speaker.camilla_yaml import (
        BASELINE_HEADROOM_DB, emit_active_speaker_baseline_config,
    )

    assert BASELINE_HEADROOM_DB == 0.0
    signature = inspect.signature(emit_active_speaker_baseline_config)
    assert (
        signature.parameters["baseline_headroom_db"].default == BASELINE_HEADROOM_DB
    )
    # …and a default emit really does put nothing but the correction's own
    # charge into the gain the proof reads.
    text = emit_active_speaker_baseline_config(_preset(), playback_device=ACTIVE_PCM)
    assert _headroom_gain_db(text) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# the charge and the proof read one chain (#1808)
# --------------------------------------------------------------------------- #


def test_the_charge_credits_the_crossover_the_graph_itself_emits():
    """The emitter charges over ``crossover ⊗ linearization ⊗ trim``, and the
    crossover term is read off the SAME preset region that becomes the emitted
    Linkwitz-Riley filter three lines later.

    A +6 dB boost deep in the woofer's own stopband cannot drive the branch
    past unity, so under #1808 it costs the household nothing — where the
    retired sum-of-positives rule charged the full 6 dB for it."""
    preset = _preset()
    flat = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    buried = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(6000.0, 6.0)]},
    )
    assert "gain: 6.000" in buried  # the filter really is emitted…
    assert _headroom_gain_db(buried) == pytest.approx(
        _headroom_gain_db(flat), abs=1e-3  # …and costs nothing
    )


def test_the_charge_credits_the_branch_trim():
    """A branch already attenuated by its own baseline gain needs that much
    less program-domain headroom. This is what makes the conductor's
    ``normalize_shift_db`` net-neutral: shifting every branch's trim down by S
    lowers the charge by S, so the pre-split attenuation gives back exactly
    what the branches gave up."""
    preset = _preset()
    boost = {"tweeter": [_peak(6000.0, 4.0)]}
    untrimmed = _headroom_gain_db(emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=boost,
    ))
    trimmed = _headroom_gain_db(emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, linearization=boost,
        corrections={"tweeter": {"gain_db": -1.5}},
    ))
    assert trimmed == pytest.approx(untrimmed + 1.5, abs=1e-3)


def test_a_stopband_boost_proves_against_a_graph_that_charged_nothing_for_it():
    """The proof follows the charge. A boost the crossover fully removes is
    charged 0.0, so the graph carries no allowance for it — and it must still
    re-prove, because the quantity being proved is the CHAIN's peak, not the
    filter's gain. Under the retired sum rule this graph would have refused
    itself."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(6000.0, 6.0)]},
    )
    assert _headroom_gain_db(text) == pytest.approx(0.0, abs=1e-3)
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, graph.issues


def test_reproof_blocks_a_tampered_boost_moved_into_the_passband():
    """…and the same filter moved where the crossover does NOT remove it is
    refused, on a graph whose headroom never paid for it. The freedom the peak
    rule grants is exactly "attenuation the graph provably applies", nothing
    else."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"woofer": [_peak(6000.0, 6.0)]},
    )
    payload = yaml.safe_load(text)
    payload["filters"][driver_linearization_peak_name("woofer", 1)][
        "parameters"
    ]["freq"] = 400.0
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_reproof_blocks_a_tampered_branch_trim_that_removes_the_attenuation():
    """The trim is credited, so it is also proved. Tampering the branch gain
    back to unity on a graph whose charge counted that attenuation must fail
    closed — otherwise crediting the trim would be a way to spend headroom
    twice."""
    topology = _active_topology("mono", "active_2_way")
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": [_peak(6000.0, 4.0)]},
        corrections={"tweeter": {"gain_db": -3.0}},
    )
    assert classify_camilla_graph(topology=topology, text=text).allowed is True
    payload = yaml.safe_load(text)
    payload["filters"]["as_tweeter_baseline_gain"]["parameters"]["gain"] = 0.0
    source = next(line for line in text.splitlines() if line.startswith("# Source:"))
    tampered = f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"

    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False


def test_the_emitter_and_the_prover_read_the_same_chain():
    """One number, two readers. The emitter's charge and the contract's
    allowance are the same quantity over the same three terms — if they ever
    disagreed by more than the proof's float slack, a graph correct by
    construction would refuse itself on hardware."""
    from jasper.active_speaker.branch_chain import (
        CrossoverSection, branch_headroom_db,
    )

    filters = [_peak(6000.0, 4.0), _peak(9000.0, 2.0)]
    text = emit_active_speaker_baseline_config(
        _preset(), playback_device=ACTIVE_PCM,
        linearization={"tweeter": filters},
        corrections={"tweeter": {"gain_db": -1.0}},
    )
    payload = yaml.safe_load(text)
    hp_name = next(
        name for name in payload["filters"]
        if name.startswith("as_tweeter_") and name.endswith("_hp")
    )
    fc_hz = float(payload["filters"][hp_name]["parameters"]["freq"])
    order = int(payload["filters"][hp_name]["parameters"]["order"])
    assert -_headroom_gain_db(text) == pytest.approx(
        branch_headroom_db(
            filters,
            sections=(CrossoverSection(fc_hz=fc_hz, order=order, highpass=True),),
            trim_db=-1.0,
        ),
        abs=1e-3,
    )
