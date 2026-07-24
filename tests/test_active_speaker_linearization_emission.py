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
from jasper.active_speaker.camilla_yaml import (
    MAX_LINEARIZATION_FILTERS_PER_DRIVER,
    driver_linearization_peak_name,
    driver_linearization_shelf_name,
)
from jasper.active_speaker.linearization_fit import MAX_FILTERS_PER_DRIVER
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


def _peak(freq: float = 1000.0, gain: float = -2.0, q: float = 3.0) -> dict:
    return {"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain}


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


def test_linearization_shelf_uses_fixed_highshelf_slope():
    """The Highshelf FilterSpec must carry the fixed slope (6.0) equivalent
    to the fit engine's fixed Butterworth Q — CamillaDSP's Highshelf reads
    ``slope``, not ``q``, so getting this wrong would silently emit a
    DIFFERENT shelf shape than the fit engine designed."""
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
    assert params["slope"] == pytest.approx(6.0)
    assert "q" not in params


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


@pytest.mark.parametrize("bad_type", ["Lowshelf", "Notch", "", None, 42])
def test_linearization_rejects_unsupported_biquad_type(bad_type):
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="biquad_type"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [{**_peak(), "biquad_type": bad_type}]},
        )


def test_linearization_rejects_positive_gain():
    preset = _preset()
    with pytest.raises(ActiveSpeakerConfigError, match="must not be positive"):
        emit_active_speaker_baseline_config(
            preset, playback_device=ACTIVE_PCM,
            linearization={"woofer": [_peak(gain=2.0)]},
        )


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


@pytest.mark.parametrize("bad_gain", [2.0, 0.5])
def test_reproof_blocks_tampered_positive_gain_linearization_filter(bad_gain):
    """The graph_safety keystone: a positive-gain linearization filter
    tampered directly into the YAML (simulating a corrupted statefile, since
    the emitter itself can never produce one) must fail closed at re-proof."""
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
