# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Channel-routed program graph emission (Wave 2 deliverable A).

The v2 crossover conductor plays one 2-channel program WAV through a static
CamillaDSP graph that routes program capture ch0 -> woofer output path and ch1 ->
tweeter output path (design §5.4). These tests pin: role-routed mixing, the
APPLIED_RESPONSE filter set on the physical output channels, the 0 dB ceiling,
the build-time protective-floor gate, and the build-and-prove return contract —
including the adversarial pre-split-HP shape that ``tweeter_guard_present`` must
reject even where ``output_highpass_protected`` alone would false-PASS.
"""
from __future__ import annotations

import logging

import pytest

from jasper.active_speaker.branch_chain import CrossoverSection
import yaml as yaml_lib

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    emit_active_speaker_program_config,
)
from jasper.active_speaker.camilla_yaml import (
    _assert_program_graph_proven,
    _driver_limiter_name,
    emit_active_speaker_baseline_config, protected_neutral_program_origin,
)
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.graph_safety import (
    output_highpass_protected,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)

# Reuse the canonical preset fixtures (mono 2-way == JTS3 single cabinet:
# output 0 = woofer, output 1 = tweeter).
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
ROLE_CHANNELS = {"woofer": 0, "tweeter": 1}


def _preset(layout: str = "mono") -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))


def _confirmed_protection():
    profile = {"targets": [
        {
            "role": "woofer", "target_fingerprint": "w",
            "required_protection_filters": [{
                "kind": "lowpass", "cutoff_hz": 3000.0,
                "minimum_slope_db_per_octave": 24.0,
            }],
        },
        {
            "role": "tweeter", "target_fingerprint": "t",
            "required_protection_filters": [{
                "kind": "highpass", "cutoff_hz": 1800.0,
                "minimum_slope_db_per_octave": 24.0,
            }],
        },
    ]}
    return confirmed_protection_sections(
        profile, {"woofer": "w", "tweeter": "t"}
    )


def _low_fc_preset() -> ActiveSpeakerPreset:
    """A 2-way preset whose tweeter crosses too low for the protective floor."""
    builder = _two_way_preset("mono")
    builder["crossover_regions"][0]["fc_hz"] = 300  # < TWEETER_PROTECTIVE_HP floor
    return ActiveSpeakerPreset.from_mapping(builder)


def test_program_config_routes_each_channel_to_its_driver_output():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)

    # Capture is the program channel count; playback the physical output count.
    assert parsed["devices"]["capture"]["channels"] == 2
    assert parsed["devices"]["volume_limit"] == 0.0

    mixer = parsed["mixers"]["split_active_2way"]
    routing = {
        entry["dest"]: [s["channel"] for s in entry["sources"]]
        for entry in mixer["mapping"]
    }
    # ch0 -> woofer output 0, ch1 -> tweeter output 1 (role-routed, not side-routed).
    assert routing == {0: [0], 1: [1]}


def test_program_config_carries_target_crossover_not_bringup_hp():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)

    # APPLIED_RESPONSE: the extra bring-up protective HP is dropped; the tweeter
    # is protected by its TARGET crossover high-pass, on the OUTPUT channel.
    assert "as_tweeter_protective_hp" not in parsed["filters"]
    hp = parsed["filters"]["as_tweeter_woofer_tweeter_hp"]["parameters"]
    assert hp == {"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 4}
    # Program headroom is the commissioning headroom (0 dB) so the effective-peak
    # ledger is main_volume + program peak with no hidden graph attenuation.
    assert parsed["filters"]["active_startup_headroom"]["parameters"]["gain"] in (
        0.0,
        -0.0,
    )


def test_protected_neutral_program_config_contains_only_declared_safety_shaping():
    builder = _two_way_preset("mono")
    builder["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(builder)
    protection = _confirmed_protection()
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=protection,
    )
    parsed = yaml_lib.safe_load(out)
    filters = parsed["filters"]
    assert set(filters) == {
        "active_startup_headroom",
        "as_woofer_program_protection_0", "as_tweeter_program_protection_0",
        "as_woofer_startup_limiter", "as_tweeter_startup_limiter",
        "as_out0_commission_mute", "as_out1_commission_mute",
    }
    assert filters["as_woofer_program_protection_0"]["parameters"] == {
        "type": "LinkwitzRileyLowpass", "freq": 3000.0, "order": 4,
    }
    assert filters["as_tweeter_program_protection_0"]["parameters"] == {
        "type": "LinkwitzRileyHighpass", "freq": 1800.0, "order": 4,
    }
    assert filters["active_startup_headroom"]["parameters"]["gain"] == pytest.approx(0.0)
    for role in ("woofer", "tweeter"):
        limiter = filters[f"as_{role}_startup_limiter"]
        assert limiter["type"] == "Limiter"
        assert limiter["parameters"] == {
            "soft_clip": True, "clip_limit": -12.0,
        }
    assert parsed["pipeline"][2]["names"] == [
        "as_woofer_program_protection_0", "as_woofer_startup_limiter",
    ]
    assert parsed["pipeline"][3]["names"] == [
        "as_tweeter_program_protection_0", "as_tweeter_startup_limiter",
    ]
    assert parsed["mixers"]["split_active_2way"]["mapping"][1]["sources"][0][
        "inverted"
    ] is False
    view = view_from_emitted_text(out)
    assert tweeter_guard_present(
        view, channels={1}, hp_name="as_tweeter_program_protection_0",
        limiter_name="as_tweeter_startup_limiter", limiter_clip_ceiling_db=-12.0,
    )
    assert protected_neutral_program_origin(out) is True


def test_protected_neutral_origin_excludes_other_and_mutated_graphs():
    preset = _preset("mono")
    neutral = yaml_lib.safe_load(emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    ))
    legacy = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
    )
    applied = emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    assert (protected_neutral_program_origin(legacy),
            protected_neutral_program_origin(applied)) == (None, None)
    partial = yaml_lib.safe_load(yaml_lib.safe_dump(neutral))
    partial["filters"].pop("as_tweeter_program_protection_0")
    assert protected_neutral_program_origin(partial) is False
    neutral["filters"]["as_room_extra"] = {"type": "Gain", "parameters": {"gain": -1.0}}
    assert protected_neutral_program_origin(neutral) is False


def test_program_config_passes_all_graph_safety_proofs():
    preset = _preset("mono")
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    view = view_from_emitted_text(out)
    tweeter = {1}

    assert unprotected_tweeter_outputs(view, tweeter_channels=tweeter) == ()
    assert output_highpass_protected(view, channel=1, allowed_channels=tweeter)
    assert tweeter_guard_present(
        view,
        channels=tweeter,
        hp_name="as_tweeter_woofer_tweeter_hp",
        limiter_name=_driver_limiter_name("tweeter"),
        limiter_clip_ceiling_db=-12.0,
    )


def test_program_config_stereo_routes_both_woofers_and_both_tweeters():
    preset = _preset("stereo")  # outputs 0,2 woofer; 1,3 tweeter
    out = emit_active_speaker_program_config(
        preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
    )
    parsed = yaml_lib.safe_load(out)
    routing = {
        entry["dest"]: entry["sources"][0]["channel"]
        for entry in parsed["mixers"]["split_active_2way"]["mapping"]
    }
    # Both woofer outputs take program ch0; both tweeter outputs take ch1.
    assert routing == {0: 0, 1: 1, 2: 0, 3: 1}
    view = view_from_emitted_text(out)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1, 3}) == ()


def test_program_config_refuses_tweeter_hp_below_protective_floor():
    # Explicit floor above the 1600 Hz crossover -> build-time refusal.
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        emit_active_speaker_program_config(
            _preset("mono"),
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
            protective_hp_min_corner_hz=2000.0,
        )
    # A preset that natively crosses the tweeter at 300 Hz -> refused at default floor.
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        emit_active_speaker_program_config(
            _low_fc_preset(),
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
        )


def test_program_config_discloses_a_shallow_tweeter_crossover_never_refuses_it(
    caplog,
):
    """The 2026-08-23 owner ruling, at the gate that would otherwise have moved
    the nanny one stage later.

    This branch — the one taken when the caller omits
    ``protection_sections_by_role`` — is the VERIFY stage's call shape
    (``correction_crossover_v2.py`` builds ``bind_production_play`` twice and
    only the MEASURE one supplies that mapping). Until #2897 it refused a
    tweeter crossover at ``order * 6 < 24`` against a hardcoded figure no
    datasheet contains, so an order-2 pin admitted at the topology gate was
    measured, applied, and THEN refused here. The corner refusal above is
    unchanged — that one names a damage mechanism; this one is a code floor and
    so discloses.
    """
    raw = _two_way_preset("mono")
    raw["crossover_regions"][0]["order"] = 2
    raw["crossover_regions"][0]["fc_hz"] = 2400
    order_2 = ActiveSpeakerPreset.from_mapping(raw)

    with caplog.at_level(logging.WARNING):
        out = emit_active_speaker_program_config(
            order_2, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        )

    # Emitted, and the graph is still a real one the tweeter is protected in.
    view = view_from_emitted_text(out)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == ()
    # …and the shortfall reached the journal instead of the caller.
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "result=tweeter_hp_slope_below_commissioning_floor" in m
        and "slope_db_per_octave=12" in m
        and "commissioning_floor_db_per_octave=24" in m
        for m in messages
    ), messages
    assert not any("blocked_tweeter_hp_slope_below_floor" in m for m in messages)


def test_program_config_still_refuses_a_crossover_below_the_declared_corner():
    """The half that survives, asserted beside the half that did not.

    De-nannying the slope did not widen the corner: an order-2 crossover BELOW
    the declared protective floor is still refused, so "shallower is disclosed"
    can never be read as "anything is emitted".
    """
    raw = _two_way_preset("mono")
    raw["crossover_regions"][0]["order"] = 2
    raw["crossover_regions"][0]["fc_hz"] = 2400
    order_2 = ActiveSpeakerPreset.from_mapping(raw)
    with pytest.raises(ActiveSpeakerConfigError, match="below the declared protective"):
        emit_active_speaker_program_config(
            order_2,
            role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
            protective_hp_min_corner_hz=3000.0,
        )


@pytest.mark.parametrize(
    ("sections", "match"),
    [
        # 399 Hz: corner below the 400 Hz floor.
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": (CrossoverSection(399.0, 4, True),)}, "program floor"),
        # Slope below the 24 dB/oct floor at a LEGAL corner — the motivating
        # case: order 2 is the downstream hole (output_highpass_protected only
        # checks freq >= 400, tweeter_guard_present accepts order >= 2).
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": (CrossoverSection(1800.0, 2, True),)}, "program floor"),
        # No tweeter high-pass at all; then a role missing entirely.
        ({"woofer": (CrossoverSection(3000.0, 4, False),),
          "tweeter": ()}, "one tweeter protection high-pass"),
        ({"tweeter": (CrossoverSection(1800.0, 4, True),)},
         "cover every driver role"),
    ],
)
def test_protected_neutral_emit_refuses_unsafe_tweeter_protection(
    sections, match, caplog,
):
    """The neutral path's tweeter floor gate — its SOLE slope-floor rail.

    REPLACES ``_assert_tweeter_crossover_hp_satisfies_floor`` (pinned above) as
    the proof between a confirmed-profile value and a compression driver. §4.1
    wants the same rails; the panel found this one had zero tests.
    """
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ActiveSpeakerConfigError, match=match):
            emit_active_speaker_program_config(
                _preset("mono"), role_channels=ROLE_CHANNELS,
                playback_device=ACTIVE_PCM, protection_sections_by_role=sections,
            )
    # …and the floor refusal reaches the journal, as its predecessor's does.
    logged = any(
        "result=blocked_tweeter_protection_below_floor" in r.getMessage()
        for r in caplog.records
    )
    assert logged is (match == "program floor"), [r.getMessage() for r in caplog.records]


def test_program_config_refuses_local_subwoofer_preset():
    builder = _two_way_preset("mono")
    builder["local_subwoofer"] = {
        "physical_output_index": 2,
        "crossover_fc_hz": 80,
        "label": "sub",
    }
    preset = ActiveSpeakerPreset.from_mapping(builder)
    with pytest.raises(ActiveSpeakerConfigError, match="local subwoofer"):
        emit_active_speaker_program_config(
            preset, role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM
        )


def test_program_config_refuses_outputd_playback_lane():
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS, playback_device="jasper_out"
        )


def test_program_config_refuses_non_two_way_preset():
    # W2 scope: the conductor's program topology is designed for a 2-way; a
    # 3-way needs a designed reshape, not a silent generalization.
    preset = ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo"))
    with pytest.raises(ActiveSpeakerConfigError, match="scoped to 2-way"):
        emit_active_speaker_program_config(
            preset,
            role_channels={"woofer": 0, "mid": 1, "tweeter": 2},
            playback_device=ACTIVE_PCM,
        )


# --- Adversarial: pre-split per-channel HP must be rejected (contract §1) -----
#
# On the 2-way preset program ch1 numerically coincides with tweeter output 1,
# so a high-pass emitted PRE-mixer on channel [1] can false-PASS
# ``output_highpass_protected`` (its channel set [1] is a subset of the tweeter
# role set {1}). ``tweeter_guard_present`` is the discriminator: it requires the
# high-pass AND the limiter together on exactly the tweeter output channels in
# ONE post-mixer step, which a pre-split HP can never satisfy.

_TWEETER_HP = "as_tweeter_woofer_tweeter_hp"
_TWEETER_LIMITER = _driver_limiter_name("tweeter")

_FILTERS_BLOCK = f"""filters:
  {_TWEETER_HP}:
    type: BiquadCombo
    parameters: {{ type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }}
  as_tweeter_delay:
    type: Delay
    parameters: {{ delay: 0.0, unit: ms }}
  {_TWEETER_LIMITER}:
    type: Limiter
    parameters: {{ soft_clip: true, clip_limit: -12.0 }}
"""

_ROUTED_PIPELINE = f"""pipeline:
  - type: Mixer
    name: split_active_2way
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}, as_tweeter_delay, {_TWEETER_LIMITER}]
"""

_PRESPLIT_PIPELINE = f"""pipeline:
  - type: Filter
    channels: [1]
    names: [{_TWEETER_HP}]
  - type: Mixer
    name: split_active_2way
  - type: Filter
    channels: [1]
    names: [as_tweeter_delay, {_TWEETER_LIMITER}]
"""


def test_routed_hp_variant_passes_both_proofs():
    view = view_from_emitted_text(_FILTERS_BLOCK + "\n" + _ROUTED_PIPELINE)
    assert output_highpass_protected(view, channel=1, allowed_channels={1})
    assert tweeter_guard_present(
        view,
        channels={1},
        hp_name=_TWEETER_HP,
        limiter_name=_TWEETER_LIMITER,
        limiter_clip_ceiling_db=-12.0,
    )


def test_pre_split_hp_variant_rejected_by_tweeter_guard():
    view = view_from_emitted_text(_FILTERS_BLOCK + "\n" + _PRESPLIT_PIPELINE)
    # output_highpass_protected alone false-PASSES the coincident-channel HP...
    assert output_highpass_protected(view, channel=1, allowed_channels={1})
    # ...but tweeter_guard_present rejects it: no single step wires HP + limiter
    # together on the tweeter output channels.
    assert not tweeter_guard_present(
        view,
        channels={1},
        hp_name=_TWEETER_HP,
        limiter_name=_TWEETER_LIMITER,
        limiter_clip_ceiling_db=-12.0,
    )


def test_build_and_prove_refuses_pre_split_hp_graph():
    preset = _preset("mono")
    doctored = (
        "---\n"
        + _FILTERS_BLOCK
        + "\nmixers:\n  split_active_2way:\n    channels: { in: 2, out: 2 }\n"
        + _PRESPLIT_PIPELINE
    )
    with pytest.raises(ActiveSpeakerConfigError, match="provably high-pass"):
        _assert_program_graph_proven(doctored, preset, min_corner_hz=400.0)


# --------------------------------------------------------------------------- #
# R-1's delay lane — measurement emitter ONLY
# --------------------------------------------------------------------------- #


def _program(**kwargs):
    return yaml_lib.safe_load(emit_active_speaker_program_config(
        _preset("mono"),
        role_channels=ROLE_CHANNELS,
        playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
        **kwargs,
    ))


def test_no_delay_asked_for_emits_the_graph_it_always_did():
    # The scoping guarantee that matters most: this emitter is shared with
    # CHECK and MEASURE, so a caller that names no delay must get byte-identical
    # output. `None` and `{}` are the same "no" as omitting the argument.
    baseline = emit_active_speaker_program_config(
        _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    )
    for empty in (None, {}):
        assert emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
            protection_sections_by_role=_confirmed_protection(),
            measurement_delays_us=empty,
        ) == baseline


def test_a_named_role_carries_a_delay_filter_at_the_head_of_its_chain():
    from jasper.active_speaker.camilla_yaml import driver_delay_name
    from jasper.audio_measurement.delay_graph import quantized_delay_ms

    # A coordinate whose two plausible recipes DISAGREE: fmt(us/1000) rounds to
    # 4 decimal places of ms (0.2502) where a raw divide keeps 0.25015006948647.
    # A round number here would pass under either recipe and pin nothing.
    delay_us = 250.15006948647
    assert quantized_delay_ms(delay_us) != delay_us / 1000.0

    parsed = _program(measurement_delays_us={"tweeter": delay_us})
    name = driver_delay_name("tweeter")

    spec = parsed["filters"][name]
    assert spec["type"] == "Delay"
    assert spec["parameters"]["unit"] == "ms"
    # Folded through the ONE quantizer, so the graph proof that recomputes the
    # expected value from the same delay_us agrees exactly.
    assert spec["parameters"]["delay"] == quantized_delay_ms(delay_us)

    chain = next(
        step["names"] for step in parsed["pipeline"]
        if step.get("type") == "Filter" and name in (step.get("names") or ())
    )
    # Head of the chain. A pure delay commutes with every stage here, so this
    # pins the emitter's choice rather than an acoustic requirement.
    assert chain[0] == name


def test_only_the_named_role_is_touched():
    from jasper.active_speaker.camilla_yaml import driver_delay_name

    parsed = _program(measurement_delays_us={"tweeter": 250.0})
    assert driver_delay_name("woofer") not in parsed["filters"]
    for step in parsed["pipeline"]:
        assert driver_delay_name("woofer") not in (step.get("names") or ())


def test_the_delay_lane_cannot_reach_a_graph_a_household_plays():
    # The scoping mechanism is that the parameter exists on THIS emitter and
    # nowhere else: the applied/baseline emitter takes its per-driver delay from
    # the profile's corrections and has no argument a sweep could pass.
    import inspect

    from jasper.active_speaker import camilla_yaml

    emitters = {
        name: getattr(camilla_yaml, name)
        for name in dir(camilla_yaml)
        if name.startswith("emit_active_speaker_")
        and callable(getattr(camilla_yaml, name))
    }
    assert "emit_active_speaker_program_config" in emitters
    for name, emitter in emitters.items():
        has_knob = "measurement_delays_us" in inspect.signature(emitter).parameters
        assert has_knob == (name == "emit_active_speaker_program_config"), name


def test_the_delay_lane_does_not_move_the_ceiling_or_the_limiter():
    plain = _program()
    delayed = _program(measurement_delays_us={"tweeter": 250.0})
    assert delayed["devices"]["volume_limit"] == plain["devices"]["volume_limit"] == 0.0
    assert delayed["filters"][_driver_limiter_name("tweeter")] == (
        plain["filters"][_driver_limiter_name("tweeter")]
    )


def test_a_delayed_program_still_passes_the_emitter_s_own_proofs():
    # The build-time protective-floor and tweeter-guard proofs run inside every
    # emit and raise on failure, so a graph that comes back at all has passed
    # them. A delay lane is not a way past them, on either branch.
    text = emit_active_speaker_program_config(
        _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
        measurement_delays_us={"tweeter": 250.0, "woofer": 0.0},
    )
    view = view_from_emitted_text(text)
    assert not unprotected_tweeter_outputs(
        view, tweeter_channels={ROLE_CHANNELS["tweeter"]}
    )


def test_a_delay_on_an_unprotected_low_crossover_is_still_refused():
    # The protective-floor gate refuses this preset with or without a delay.
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_config(
            _low_fc_preset(), role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
            measurement_delays_us={"tweeter": 250.0},
        )


def test_the_emitter_fails_closed_on_a_delay_it_cannot_honour():
    # The emitter proves its tweeter guard and bounds its ceilings; the delay
    # lane fails closed the same way rather than emitting a graph whose YAML
    # disagrees with what was asked for.
    protection = _confirmed_protection()
    for bad in (
        {"midrange": 250.0},              # no such branch on a 2-way preset
        {"tweeter": -1.0},                # negative delay
        {"tweeter": float("inf")},        # non-finite
        {"tweeter": 10_000_000.0},        # past the DSP ceiling
    ):
        with pytest.raises(ActiveSpeakerConfigError):
            emit_active_speaker_program_config(
                _preset("mono"), role_channels=ROLE_CHANNELS,
                playback_device=ACTIVE_PCM,
                protection_sections_by_role=protection,
                measurement_delays_us=bad,
            )


def test_a_delay_on_the_unprotected_shape_is_refused_not_silently_zeroed():
    # That shape defines its own zeroed delay lane for every role, so a named
    # delay would emit a duplicate mapping key whose later zero wins on parse:
    # a capture that plays undelayed and banks as a delayed take.
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_config(
            _preset("mono"), role_channels=ROLE_CHANNELS,
            playback_device=ACTIVE_PCM,
            protection_sections_by_role=None,
            measurement_delays_us={"tweeter": 250.0},
        )


def test_the_graph_says_which_delay_coordinate_it_carries():
    # Beside `# inverted_roles=`, and for its reason: a record names the graph
    # by fingerprint, and a reader must be able to see which coordinate that
    # fingerprint stands for without reconstructing the Delay filter body.
    text = emit_active_speaker_program_config(
        _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
        measurement_delays_us={"tweeter": 250.0},
    )
    assert "# measurement_delays_us={'tweeter': 250.0}" in text

    # And absent entirely when there is no coordinate: an unconditional line
    # would change the fingerprint of every CHECK and MEASURE graph in the tree.
    plain = emit_active_speaker_program_config(
        _preset("mono"), role_channels=ROLE_CHANNELS, playback_device=ACTIVE_PCM,
        protection_sections_by_role=_confirmed_protection(),
    )
    assert "measurement_delays_us" not in plain
