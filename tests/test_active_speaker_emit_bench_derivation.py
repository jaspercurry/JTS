# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emit-loop derivation's contract: swap the devices, touch nothing else.

Hardware-free and subprocess-free. Every case starts from the REAL emitter's
output (``emit_active_speaker_baseline_config``), because the whole point of the
derivation is that its input is what the emitter actually wrote — a hand-built
YAML fixture would let the two drift and the tests would still pass.
"""

from __future__ import annotations

import math

import pytest
import yaml

from jasper.active_speaker import ActiveSpeakerPreset, emit_active_speaker_baseline_config
from jasper.active_speaker.bench import derivation
from jasper.active_speaker.bench.derivation import (
    EmitDerivationError,
    derive_offline_render_config,
    device_geometry,
)
from jasper.active_speaker.camilla_yaml import driver_baseline_limiter_name
from jasper.bass_extension.bench.derivation import ArtifactHeader
from jasper.bass_extension.bench.render import DEPLOYED_PROCESSING_PRECISION
from tests.test_active_speaker_profile import _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
SHELF_Q = 1.0 / math.sqrt(2.0)
ROLES = ("woofer", "tweeter")
HEADER = ArtifactHeader(sample_rate_hz=48000, channels=2, bits_per_sample=16)


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _emit(*, linearization=None, layout: str = "mono", **kwargs) -> str:
    return emit_active_speaker_baseline_config(
        _preset(layout),
        playback_device=ACTIVE_PCM,
        linearization=linearization,
        **kwargs,
    )


def _derive(text: str, *, header: ArtifactHeader = HEADER, roles=ROLES):
    return derive_offline_render_config(
        text,
        roles=roles,
        capture_filename="/tmp/stimulus.wav",
        capture_header=header,
        playback_filename="/tmp/out.raw",
        processing_precision=DEPLOYED_PROCESSING_PRECISION,
    )


def _lin() -> dict[str, list[dict[str, float | str]]]:
    return {
        "woofer": [{"biquad_type": "Peaking", "freq": 320.0, "q": 3.0, "gain": -4.0}],
        "tweeter": [
            {"biquad_type": "Highshelf", "freq": 8000.0, "q": SHELF_Q, "gain": -6.0}
        ],
    }


# --------------------------------------------------------------------------- #
# the graph is preserved verbatim
# --------------------------------------------------------------------------- #


def test_filters_mixers_and_pipeline_round_trip_verbatim() -> None:
    text = _emit(linearization=_lin())
    derived = yaml.safe_load(_derive(text).yaml_text)
    source = yaml.safe_load(text)
    assert derived["filters"] == source["filters"]
    assert derived["mixers"] == source["mixers"]
    assert derived["pipeline"] == source["pipeline"]


def test_emitted_linearization_filters_appear_in_the_derived_config() -> None:
    """The filters under test survive derivation with their exact parameters."""

    derived = yaml.safe_load(_derive(_emit(linearization=_lin())).yaml_text)
    shelf = derived["filters"]["as_tweeter_linearization_shelf"]["parameters"]
    assert shelf["type"] == "Highshelf"
    assert shelf["freq"] == pytest.approx(8000.0)
    assert shelf["q"] == pytest.approx(SHELF_Q, abs=1e-6)
    assert shelf["gain"] == pytest.approx(-6.0)
    peak = derived["filters"]["as_woofer_linearization_peak_1"]["parameters"]
    assert (peak["type"], peak["freq"], peak["q"], peak["gain"]) == (
        "Peaking",
        320.0,
        3.0,
        -4.0,
    )


def test_graph_preservation_assert_fires_on_a_tampered_block() -> None:
    """The preservation gate is not a self-comparison that can never fail.

    It re-parses the SOURCE TEXT rather than comparing the derived blocks
    against the mapping they were copied from — this proves the difference,
    which a same-object ``==`` could not.
    """

    text = _emit(linearization=_lin())
    tampered = yaml.safe_load(text)
    tampered["filters"]["as_tweeter_linearization_shelf"]["parameters"]["q"] = 0.4759
    with pytest.raises(EmitDerivationError, match="filters block"):
        derivation._assert_graph_preserved(text, tampered)
    mixed = yaml.safe_load(text)
    mixed["mixers"]["split_active_2way"]["mapping"][0]["sources"][0]["gain"] = 0.0
    with pytest.raises(EmitDerivationError, match="mixers block"):
        derivation._assert_graph_preserved(text, mixed)


# --------------------------------------------------------------------------- #
# the device swap
# --------------------------------------------------------------------------- #


def test_devices_are_swapped_for_file_backed_ones() -> None:
    result = _derive(_emit())
    devices = yaml.safe_load(result.yaml_text)["devices"]
    assert devices["capture"] == {"type": "WavFile", "filename": "/tmp/stimulus.wav"}
    assert devices["playback"] == {
        "type": "File",
        "channels": 2,
        "filename": "/tmp/out.raw",
        "format": DEPLOYED_PROCESSING_PRECISION,
    }
    assert devices["enable_rate_adjust"] is False
    assert devices["samplerate"] == 48000
    assert result.sample_rate_hz == 48000
    assert result.playback_channels == 2


def test_capture_block_carries_no_channels_key() -> None:
    """``CaptureDeviceWavFile`` declares no ``channels`` and denies unknown fields.

    Keeping the live value would make the pinned build reject the derived config
    outright, so the live count is validated against the artifact header and
    recorded in the receipt instead.
    """

    result = _derive(_emit())
    assert "channels" not in yaml.safe_load(result.yaml_text)["devices"]["capture"]
    diff = result.receipt["device_diff"]
    assert diff["capture_channels_live_validated_only"] == 2
    assert diff["capture_type_live"] == "Alsa"
    assert diff["playback_device_key_live"] == ACTIVE_PCM


def test_live_rate_adjust_is_normalised_off() -> None:
    text = _emit()
    assert yaml.safe_load(text)["devices"]["enable_rate_adjust"] is True
    assert _derive(text).receipt["device_diff"]["enable_rate_adjust_live"] is True


@pytest.mark.parametrize(
    "header, message",
    [
        (ArtifactHeader(44100, 2, 16), "sample rate"),
        (ArtifactHeader(48000, 1, 16), "channel count"),
        (ArtifactHeader(48000, 2, 24), "16-bit"),
    ],
)
def test_stimulus_geometry_mismatch_refuses_before_any_render(header, message) -> None:
    with pytest.raises(EmitDerivationError, match=message):
        _derive(_emit(), header=header)


def test_device_geometry_reads_both_sides_of_the_emitted_block() -> None:
    geometry = device_geometry(_emit())
    assert geometry.sample_rate_hz == 48000
    assert geometry.capture_channels == 2
    assert geometry.playback_channels == 2
    with pytest.raises(EmitDerivationError):
        device_geometry("devices: {}")


def test_device_geometry_separates_capture_from_playback_width() -> None:
    """The render writes the PLAYBACK frame, which is the wider one.

    Sizing anything (notably the free-space estimate) at capture width is short
    by exactly this ratio on a multi-way stereo preset, which is why the two
    counts are named rather than returned as an ordered pair.
    """

    geometry = device_geometry(_emit(layout="stereo"))
    assert geometry.capture_channels == 2
    assert geometry.playback_channels == 4


# --------------------------------------------------------------------------- #
# the admission gate
# --------------------------------------------------------------------------- #


def test_async_resampler_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["devices"]["resampler"] = {"type": "AsyncSinc", "profile": "Balanced"}
    with pytest.raises(EmitDerivationError, match="Async"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_unallowlisted_filter_type_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["filters"]["as_woofer_delay"]["type"] = "Dither"
    with pytest.raises(EmitDerivationError, match="outside the offline render"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_bypassed_step_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["pipeline"][0]["bypassed"] = True
    with pytest.raises(EmitDerivationError, match="bypassed"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_unknown_pipeline_step_type_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["pipeline"].append({"type": "Processor", "name": "compressor"})
    with pytest.raises(EmitDerivationError, match="not Filter or Mixer"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_filter_without_a_definition_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["pipeline"][-1]["names"].append("as_tweeter_ghost")
    with pytest.raises(EmitDerivationError, match="no definition"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_empty_or_unparseable_text_refuses() -> None:
    with pytest.raises(EmitDerivationError, match="empty"):
        _derive("   ")
    with pytest.raises(EmitDerivationError, match="not a mapping"):
        _derive("- a\n- b\n")


def test_roles_must_be_non_empty() -> None:
    with pytest.raises(EmitDerivationError, match="roles must be non-empty"):
        _derive(_emit(), roles=())


# --------------------------------------------------------------------------- #
# branch discovery
# --------------------------------------------------------------------------- #


def test_branches_are_discovered_by_their_baseline_limiter() -> None:
    result = _derive(_emit(linearization=_lin()))
    assert [b.role for b in result.branches] == ["woofer", "tweeter"]
    woofer = result.branch("woofer")
    assert woofer.channels == (0,)
    assert woofer.extract_channel == 0
    assert woofer.names[0] == "as_woofer_woofer_tweeter_lp"
    assert woofer.names[-1] == driver_baseline_limiter_name("woofer")
    assert "as_woofer_linearization_peak_1" in woofer.names
    assert result.branch("tweeter").channels == (1,)
    assert woofer.limiter_clip_limit_dbfs == pytest.approx(-1.0)


def test_stereo_layout_branches_carry_both_channels() -> None:
    result = _derive(_emit(layout="stereo"))
    woofer = result.branch("woofer")
    assert len(woofer.channels) == 2
    # The extraction channel is fixed to the lowest so a report is reproducible.
    assert woofer.extract_channel == min(woofer.channels)
    assert set(woofer.channels).isdisjoint(result.branch("tweeter").channels)


def test_missing_branch_limiter_refuses() -> None:
    payload = yaml.safe_load(_emit())
    limiter = driver_baseline_limiter_name("tweeter")
    for step in payload["pipeline"]:
        if step.get("type") == "Filter" and limiter in step.get("names", []):
            step["names"].remove(limiter)
    with pytest.raises(EmitDerivationError, match="exactly one pipeline step"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_a_hard_clip_limiter_refuses() -> None:
    """The soft-clip bound describes the cubic transform and nothing else.

    A hard-clip limiter is a different, discontinuous transform whose
    contribution to the A/B that bound does not describe at all — so the premise
    is read off the graph rather than assumed. (The stand-in binary refuses a
    hard-clip limiter outright, so this has to be driven at the derivation.)
    """

    payload = yaml.safe_load(_emit())
    payload["filters"][driver_baseline_limiter_name("woofer")]["parameters"][
        "soft_clip"
    ] = False
    with pytest.raises(EmitDerivationError, match="not a soft-clip Limiter"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_a_limiter_missing_its_soft_clip_flag_refuses() -> None:
    payload = yaml.safe_load(_emit())
    del payload["filters"][driver_baseline_limiter_name("woofer")]["parameters"][
        "soft_clip"
    ]
    with pytest.raises(EmitDerivationError, match="not a soft-clip Limiter"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_the_emitter_ships_soft_clip_limiters() -> None:
    """The premise above is satisfied by the real emitter, not merely asserted."""

    for branch in _derive(_emit()).branches:
        assert branch.limiter_clip_limit_dbfs <= 0.0
    payload = yaml.safe_load(_emit())
    for role in ROLES:
        params = payload["filters"][driver_baseline_limiter_name(role)]["parameters"]
        assert params["soft_clip"] is True


def test_branch_limiter_that_is_not_a_limiter_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["filters"][driver_baseline_limiter_name("woofer")] = {
        "type": "Gain",
        "parameters": {"gain": 0.0, "inverted": False, "mute": False},
    }
    with pytest.raises(EmitDerivationError, match="is not a Limiter"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_branch_channel_outside_the_playback_frame_refuses() -> None:
    payload = yaml.safe_load(_emit())
    payload["devices"]["playback"]["channels"] = 1
    with pytest.raises(EmitDerivationError, match="outside the 1-channel"):
        _derive(yaml.safe_dump(payload, sort_keys=False))


def test_unknown_role_lookup_refuses() -> None:
    result = _derive(_emit())
    with pytest.raises(EmitDerivationError, match="no branch for role"):
        result.branch("midrange")


# --------------------------------------------------------------------------- #
# the program headroom the A/B has to account for
# --------------------------------------------------------------------------- #


def test_program_headroom_is_read_from_the_emitted_graph() -> None:
    assert _derive(_emit()).program_headroom_db == pytest.approx(0.0)


def test_a_boosting_linearization_moves_the_program_headroom() -> None:
    """The A/B's ``expected_offset_db`` is a real quantity, not always zero.

    A linearization carrying boost is absorbed as pre-split attenuation, so the
    two arms of the A/B sit at different levels by exactly this difference —
    which is why the loop reads it from both arms rather than assuming 0.
    """

    boosted = {
        "tweeter": [
            {"biquad_type": "Lowshelf", "freq": 8000.0, "q": SHELF_Q, "gain": -6.0},
            {"biquad_type": "Peaking", "freq": 12000.0, "q": 2.0, "gain": 4.0},
        ]
    }
    control = _derive(_emit()).program_headroom_db
    treated = _derive(_emit(linearization=boosted)).program_headroom_db
    assert treated < control
    assert treated - control < 0.0


def test_receipt_is_json_shaped_and_records_the_branches() -> None:
    import json

    receipt = _derive(_emit(linearization=_lin())).receipt
    json.dumps(receipt)
    assert receipt["roles"] == ["woofer", "tweeter"]
    assert receipt["processing_precision"] == DEPLOYED_PROCESSING_PRECISION
    assert receipt["sample_rate_hz"] == 48000
    assert receipt["branches"][0]["extract_channel"] == 0
