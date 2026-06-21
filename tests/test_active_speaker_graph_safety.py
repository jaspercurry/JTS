"""Unit tests for the shared CamillaDSP graph-safety primitives.

These pin the behaviour the active-speaker commissioning paths rely on:
fail-closed parsing, the per-output hard-mute/unmute + wiring invariants, and
the shared filter/pipeline predicates the staging evidence functions compose
(including the tweeter protective-HP + limiter wiring). Both adapters must
agree on a graph regardless of which dialect it arrived in.
"""

from __future__ import annotations

import yaml

from jasper.active_speaker import graph_safety as gs

MUTE_GAIN = -120.0

# An emitted-dialect graph: out0 hard-muted+wired, out1 unmuted tweeter wrapped
# by protective HP + limiter and its own (unmuted) commission mute.
EMITTED = """\
filters:
  as_out0_commission_mute:
    type: Gain
    parameters: { gain: -120.0, mute: true, inverted: false }
  as_out1_commission_mute:
    type: Gain
    parameters: { gain: 0.0, mute: false, inverted: false }
  as_tweeter_protective_hp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }
  as_tweeter_startup_limiter:
    type: Limiter
    parameters: { clip_limit: -12.0, soft_clip: true }
pipeline:
  - type: Filter
    channels: [0]
    names: [as_out0_commission_mute]
  - type: Filter
    channels: [1]
    names: [as_tweeter_protective_hp, as_tweeter_startup_limiter, as_out1_commission_mute]
"""


def _dict_graph(*, channel_sugar: bool = False) -> dict:
    """The same graph as a parsed dict, optionally using CamillaDSP's scalar
    ``channel: N`` single-channel sugar in the pipeline."""
    tw_step = {
        "type": "Filter",
        "names": [
            "as_tweeter_protective_hp",
            "as_tweeter_startup_limiter",
            "as_out1_commission_mute",
        ],
    }
    out0_step = {"type": "Filter", "names": ["as_out0_commission_mute"]}
    if channel_sugar:
        tw_step["channel"] = 1
        out0_step["channel"] = 0
    else:
        tw_step["channels"] = [1]
        out0_step["channels"] = [0]
    return {
        "filters": {
            "as_out0_commission_mute": {
                "type": "Gain",
                "parameters": {"gain": -120.0, "mute": True},
            },
            "as_out1_commission_mute": {
                "type": "Gain",
                "parameters": {"gain": 0.0, "mute": False},
            },
            "as_tweeter_protective_hp": {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "LinkwitzRileyHighpass",
                    "freq": 1600.0,
                    "order": 4,
                },
            },
            "as_tweeter_startup_limiter": {
                "type": "Limiter",
                "parameters": {"clip_limit": -12.0, "soft_clip": True},
            },
        },
        "pipeline": [out0_step, tw_step],
    }


# --------------------------------------------------------------------------- #
# Scalar matchers
# --------------------------------------------------------------------------- #


def test_float_matches():
    assert gs.float_matches(-120.0, MUTE_GAIN)
    assert gs.float_matches("-120.0", MUTE_GAIN)
    assert not gs.float_matches(-119.0, MUTE_GAIN)
    assert not gs.float_matches(None, MUTE_GAIN)
    assert not gs.float_matches("nope", MUTE_GAIN)


# --------------------------------------------------------------------------- #
# Adapters: both dialects normalise to an equivalent view
# --------------------------------------------------------------------------- #


def test_adapters_agree_on_the_same_graph():
    views = [
        gs.view_from_emitted_text(EMITTED),
        gs.view_from_camilla_dict(_dict_graph()),
        gs.view_from_camilla_dict(_dict_graph(channel_sugar=True)),
    ]
    for view in views:
        assert view.parsed_ok
        assert gs.output_hard_muted_and_wired(
            view, 0, mute_name="as_out0_commission_mute", mute_gain_db=MUTE_GAIN
        )
        assert gs.output_unmuted_and_wired(
            view, 1, mute_name="as_out1_commission_mute"
        )
        # The tweeter protective-HP + limiter guard is composed by the staging
        # evidence functions (and pinned by their tests) from this primitive;
        # here we just confirm the adapters surface the wiring it relies on.
        assert gs.pipeline_contains_chain(
            view,
            channels={1},
            required_names=(
                "as_tweeter_protective_hp",
                "as_tweeter_startup_limiter",
            ),
        )


def test_camilla_dict_adapter_fails_closed_on_non_dict():
    view = gs.view_from_camilla_dict(None)
    assert not view.parsed_ok
    assert not gs.output_hard_muted_and_wired(
        view, 0, mute_name="as_out0_commission_mute", mute_gain_db=MUTE_GAIN
    )


def test_camilla_dict_channel_sugar_and_list_equivalent():
    sugar = gs.view_from_camilla_dict(_dict_graph(channel_sugar=True))
    listed = gs.view_from_camilla_dict(_dict_graph(channel_sugar=False))
    assert gs.pipeline_contains_chain(
        sugar, channels={0}, required_names=("as_out0_commission_mute",)
    )
    assert gs.pipeline_contains_chain(
        listed, channels={0}, required_names=("as_out0_commission_mute",)
    )
    # bool is never a channel
    weird = gs.view_from_camilla_dict(
        {"pipeline": [{"type": "Filter", "channel": True, "names": ["x"]}]}
    )
    assert not gs.pipeline_contains_chain(weird, channels={1}, required_names=("x",))


# --------------------------------------------------------------------------- #
# filter_param_matches + pipeline_contains_chain
# --------------------------------------------------------------------------- #


def test_filter_param_matches():
    view = gs.view_from_emitted_text(EMITTED)
    assert gs.filter_param_matches(
        view, "as_out0_commission_mute", filter_type="Gain",
        params={"gain": MUTE_GAIN, "mute": True},
    )
    # wrong type, wrong gain, wrong bool, missing filter all fail
    assert not gs.filter_param_matches(
        view, "as_out0_commission_mute", filter_type="Limiter", params={}
    )
    assert not gs.filter_param_matches(
        view, "as_out0_commission_mute", filter_type="Gain", params={"gain": 0.0}
    )
    assert not gs.filter_param_matches(
        view, "as_out0_commission_mute", filter_type="Gain", params={"mute": False}
    )
    assert not gs.filter_param_matches(
        view, "does_not_exist", filter_type="Gain", params={}
    )


def test_pipeline_contains_chain_requires_exact_channels():
    view = gs.view_from_emitted_text(EMITTED)
    assert gs.pipeline_contains_chain(
        view, channels={1},
        required_names=("as_tweeter_protective_hp", "as_tweeter_startup_limiter"),
    )
    # a name present but on a different channel set must not match
    assert not gs.pipeline_contains_chain(
        view, channels={0}, required_names=("as_tweeter_protective_hp",)
    )
    # superset of channels must not match an exact-{1} step
    assert not gs.pipeline_contains_chain(
        view, channels={0, 1}, required_names=("as_tweeter_protective_hp",)
    )


# --------------------------------------------------------------------------- #
# Mute / unmute invariants (incl. fail-closed L0 cases)
# --------------------------------------------------------------------------- #


def test_hard_mute_requires_both_gain_and_wiring():
    view = gs.view_from_emitted_text(EMITTED)
    assert gs.output_hard_muted_and_wired(
        view, 0, mute_name="as_out0_commission_mute", mute_gain_db=MUTE_GAIN
    )
    # out1 is unmuted -> not a hard mute
    assert not gs.output_hard_muted_and_wired(
        view, 1, mute_name="as_out1_commission_mute", mute_gain_db=MUTE_GAIN
    )


def test_muted_but_unwired_fails_closed():
    graph = {
        "filters": {
            "as_out0_commission_mute": {
                "type": "Gain",
                "parameters": {"gain": -120.0, "mute": True},
            }
        },
        "pipeline": [],  # filter defined but never wired
    }
    view = gs.view_from_camilla_dict(graph)
    assert not gs.output_hard_muted_and_wired(
        view, 0, mute_name="as_out0_commission_mute", mute_gain_db=MUTE_GAIN
    )


def test_flat_graph_with_no_mutes_fails_closed():
    # The JTS3 "flat passthrough" shape: no commission mutes at all. Every
    # output-mute assertion must fail closed -> a flat graph can never be
    # proven safe for a roleful (tweeter) topology.
    flat = "filters:\n  flat:\n    type: Gain\n    parameters: { gain: 0.0 }\npipeline:\n  - type: Filter\n    channels: [0, 1]\n    names: [flat]\n"
    view = gs.view_from_emitted_text(flat)
    assert view.parsed_ok
    assert not gs.output_hard_muted_and_wired(
        view, 1, mute_name="as_out1_commission_mute", mute_gain_db=MUTE_GAIN
    )
    # the tweeter protective HP + limiter aren't wired either -> the primitive
    # the staging guard composes from also fails closed on a flat graph
    assert not gs.pipeline_contains_chain(
        view,
        channels={1},
        required_names=("as_tweeter_protective_hp", "as_tweeter_startup_limiter"),
    )


# --------------------------------------------------------------------------- #
# Intentional hardening vs the deleted parsers (uniform across the adapters)
# --------------------------------------------------------------------------- #


def test_bool_is_never_a_channel_in_any_adapter():
    # `bool` subclasses `int`; the deleted emitted-text parser counted
    # `true`/`false` as channels 1/0. Every adapter now excludes them — the
    # protective direction (a wiring check can only get stricter).
    emitted = (
        "filters:\n  m:\n    type: Gain\n    parameters: { gain: 0.0 }\n"
        "pipeline:\n  - type: Filter\n    channels: [true]\n    names: [m]\n"
    )
    assert not gs.pipeline_contains_chain(
        gs.view_from_emitted_text(emitted), channels={1}, required_names=("m",)
    )
    bool_channel = {"pipeline": [{"type": "Filter", "channels": [True], "names": ["m"]}]}
    assert not gs.pipeline_contains_chain(
        gs.view_from_camilla_dict(bool_channel), channels={1}, required_names=("m",)
    )
    assert not gs.pipeline_contains_chain(
        gs.view_from_yaml_dict(bool_channel), channels={1}, required_names=("m",)
    )


def test_none_in_names_is_dropped_not_stringified():
    # A null in `names` is dropped, not turned into the string "None".
    camilla = {"pipeline": [{"type": "Filter", "channels": [0], "names": [None, "m"]}]}
    view = gs.view_from_camilla_dict(camilla)
    assert gs.pipeline_contains_chain(view, channels={0}, required_names=("m",))
    assert not gs.pipeline_contains_chain(view, channels={0}, required_names=("None",))


# --------------------------------------------------------------------------- #
# view_from_yaml_dict — runtime_contract's candidate/unknown-graph adapter.
# Dict-taking (the caller owns the yaml.safe_load, like view_from_camilla_dict);
# list-only, unlike the sugar-reading view_from_camilla_dict.
# --------------------------------------------------------------------------- #


def test_view_from_yaml_dict_matches_other_adapters_on_emitted():
    # The emitted fixture, yaml.safe_load-ed, reads the same graph the emitted/
    # camilla adapters do — and runtime_contract feeds exactly this (its own
    # safe_load of the candidate text). The invariants hold on the result.
    view = gs.view_from_yaml_dict(yaml.safe_load(EMITTED))
    assert view.parsed_ok
    assert gs.output_hard_muted_and_wired(
        view, 0, mute_name="as_out0_commission_mute", mute_gain_db=MUTE_GAIN
    )
    assert gs.output_unmuted_and_wired(view, 1, mute_name="as_out1_commission_mute")
    assert gs.pipeline_contains_chain(
        view,
        channels={1},
        required_names=("as_tweeter_protective_hp", "as_tweeter_startup_limiter"),
    )


def test_view_from_yaml_dict_is_list_only_unlike_camilla_dict():
    # List-only semantics: scalar `channel: N` sugar is ignored, NOT honoured.
    # The contrast with view_from_camilla_dict (which DOES read the sugar) is
    # exactly why runtime_contract must keep using this list-only adapter and not
    # switch to the camilla-dict one.
    sugar = {
        "filters": {"m": {"type": "Gain", "parameters": {"gain": 0.0}}},
        "pipeline": [{"type": "Filter", "channel": 1, "names": ["m"]}],
    }
    assert not gs.pipeline_contains_chain(
        gs.view_from_yaml_dict(sugar), channels={1}, required_names=("m",)
    )
    listed = {
        "filters": {"m": {"type": "Gain", "parameters": {"gain": 0.0}}},
        "pipeline": [{"type": "Filter", "channels": [1], "names": ["m"]}],
    }
    assert gs.pipeline_contains_chain(
        gs.view_from_yaml_dict(listed), channels={1}, required_names=("m",)
    )
    # contrast: view_from_camilla_dict DOES honour the scalar sugar
    assert gs.pipeline_contains_chain(
        gs.view_from_camilla_dict(sugar), channels={1}, required_names=("m",)
    )


def test_view_from_yaml_dict_fails_closed_on_non_dict():
    # runtime_contract pre-validates the dict, but the adapter still fails closed
    # on a non-mapping — defence in depth for any other caller.
    assert not gs.view_from_yaml_dict(None).parsed_ok
    assert not gs.view_from_yaml_dict(["a", "b"]).parsed_ok  # a list document
    assert not gs.view_from_yaml_dict("scalar").parsed_ok


# --------------------------------------------------------------------------- #
# tweeter_guard_present — runtime_contract's LOOSE policy
# --------------------------------------------------------------------------- #

TWEETER_CH = 1
HP_NAME = "as_tweeter_protective_hp"
LIMITER_NAME = "as_tweeter_startup_limiter"
CLIP_CEILING = -12.0


def _tweeter_view(
    *,
    hp_params: dict | None = None,
    limiter_params: dict | None = None,
    include_hp: bool = True,
    include_limiter: bool = True,
    wired: bool = True,
) -> gs.GraphView:
    """A view with the tweeter HP + limiter wired to channel 1, tunable per test.

    Defaults are the emitter's exact values; tests override params (or drop a
    filter / the wiring) to probe the loose policy's bounds."""
    hp_params = (
        {"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 4}
        if hp_params is None
        else hp_params
    )
    limiter_params = (
        {"clip_limit": -12.0, "soft_clip": True}
        if limiter_params is None
        else limiter_params
    )
    filters: dict = {}
    names: list[str] = []
    if include_hp:
        filters[HP_NAME] = {"type": "BiquadCombo", "parameters": hp_params}
        names.append(HP_NAME)
    if include_limiter:
        filters[LIMITER_NAME] = {"type": "Limiter", "parameters": limiter_params}
        names.append(LIMITER_NAME)
    pipeline = (
        [{"type": "Filter", "channels": [TWEETER_CH], "names": names}]
        if wired and names
        else []
    )
    return gs.view_from_camilla_dict({"filters": filters, "pipeline": pipeline})


def _guard(view: gs.GraphView) -> bool:
    return gs.tweeter_guard_present(
        view,
        channels={TWEETER_CH},
        hp_name=HP_NAME,
        limiter_name=LIMITER_NAME,
        limiter_clip_ceiling_db=CLIP_CEILING,
    )


def test_tweeter_guard_accepts_exact_emitter_values():
    assert _guard(_tweeter_view())


def test_tweeter_guard_loose_tolerances_accepted():
    # Each of these would fail staging's exact-match guard but is fine for the
    # loose audible-protection policy:
    assert _guard(  # any positive Fc, not the emitter's exact protective Fc
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": 800.0, "order": 4})
    )
    assert _guard(  # order >= 2 (not exactly 4)
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 2})
    )
    assert _guard(  # order absent entirely
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": 1600.0})
    )
    assert _guard(  # clip_limit BELOW the ceiling (more aggressive limiting)
        _tweeter_view(limiter_params={"clip_limit": -20.0, "soft_clip": True})
    )
    assert _guard(  # clip_limit exactly at the ceiling
        _tweeter_view(limiter_params={"clip_limit": -12.0, "soft_clip": True})
    )


def test_tweeter_guard_loose_differs_from_strict():
    # Pin that loose really is looser than staging's strict composition: a view
    # the loose guard accepts is rejected by an exact filter_param_matches.
    view = _tweeter_view(
        hp_params={"type": "LinkwitzRileyHighpass", "freq": 800.0, "order": 2},
        limiter_params={"clip_limit": -20.0, "soft_clip": True},
    )
    assert _guard(view)
    assert not gs.filter_param_matches(
        view,
        HP_NAME,
        filter_type="BiquadCombo",
        params={"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 4},
    )


def test_tweeter_guard_rejects_bad_highpass():
    assert not _guard(  # non-positive Fc
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": 0.0, "order": 4})
    )
    assert not _guard(
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": -100.0, "order": 4})
    )
    assert not _guard(  # order below 2
        _tweeter_view(hp_params={"type": "LinkwitzRileyHighpass", "freq": 1600.0, "order": 1})
    )
    assert not _guard(  # wrong biquad sub-type
        _tweeter_view(hp_params={"type": "LinkwitzRileyLowpass", "freq": 1600.0, "order": 4})
    )
    assert not _guard(_tweeter_view(include_hp=False))  # filter absent entirely


def test_tweeter_guard_rejects_bad_limiter():
    assert not _guard(  # clip_limit ABOVE the ceiling
        _tweeter_view(limiter_params={"clip_limit": -6.0, "soft_clip": True})
    )
    assert not _guard(  # soft_clip false
        _tweeter_view(limiter_params={"clip_limit": -12.0, "soft_clip": False})
    )
    assert not _guard(  # soft_clip missing
        _tweeter_view(limiter_params={"clip_limit": -12.0})
    )
    assert not _guard(_tweeter_view(include_limiter=False))  # filter absent entirely


def test_tweeter_guard_rejects_unwired():
    # Both filters defined with correct params, but not wired into the pipeline
    # for the tweeter channel -> fails closed.
    assert not _guard(_tweeter_view(wired=False))


def test_tweeter_guard_fails_closed_on_unparsed_view():
    assert not _guard(gs.view_from_camilla_dict(None))
