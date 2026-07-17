# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""L0 graph-safety emit gate: no active-speaker graph ships an unprotected tweeter.

Pins the load-bearing L0 hearing-safety invariant
(docs/HANDOFF-audio-measurement-core.md): an output carrying a tweeter /
compression-driver role MUST have a protective high-pass (its crossover
high-pass and/or a dedicated protective high-pass) whose corner is high enough
to keep the low-frequency excursion hazard band off the driver. A compression
driver is ~25 dB more sensitive than the woofer, so a graph that routes
full-range program to a bare (or too-low-crossed) tweeter output is a shrill /
hot-tweeter hazard.

Two layers are pinned:

* the shared normalise-then-predicate primitive
  (``graph_safety.unprotected_tweeter_outputs`` over a ``GraphView``): a flat
  graph with a tweeter role is flagged; a properly crossed-over graph is clean;
  a graph with no tweeter role (passive full-range) is clean — never over-blocked;
  a too-low corner is flagged; a pre-split program-bus high-pass does NOT
  false-PASS a post-split tweeter output;
* the fail-closed wiring at the ``camilla_yaml`` active-speaker emit gate: EVERY
  one of the four DAC emitters refuses (raises + logs) a graph whose tweeter
  output lost its high-pass, and passes a normal protected graph.

The active emitters wire the protection by construction, so the "flat + tweeter
role" emit case is provoked by stripping the tweeter high-pass from the chain
builder each emitter uses — proving the gate on each is a real guard, not
decorative (deleting a gate call from one emitter would then ship red).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

import pytest
import yaml
import numpy as np

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    emit_active_speaker_baseline_config,
    emit_active_speaker_commissioning_config,
    emit_active_speaker_driver_domain_config,
    emit_active_speaker_startup_config,
)
import jasper.active_speaker.camilla_yaml as camilla_yaml
from jasper.active_speaker.graph_safety import (
    TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    output_highpass_protected,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    classify_camilla_graph,
)

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_bass_extension_profile import _profile

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _preset(layout: str = "mono", way: int = 2) -> ActiveSpeakerPreset:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return ActiveSpeakerPreset.from_mapping(raw)


def _hp_stripping(original: Callable[..., list[str]]) -> Callable[..., list[str]]:
    """Wrap a chain builder so the tweeter chain loses its high-pass filter(s).

    Simulates a regression that drops the tweeter's crossover / protective
    high-pass — the exact "flat + tweeter role" hazard the emit gate must refuse.
    """

    def _stripped(
        preset: ActiveSpeakerPreset, role: str, *args, **kwargs
    ) -> list[str]:
        names = original(preset, role, *args, **kwargs)
        if role == "tweeter":
            return [name for name in names if not name.endswith("_hp")]
        return names

    return _stripped


def _sealed_profile(*, channels=(0, 2), status="accepted"):
    return replace(
        _profile(status=status),
        bass_owner={
            "kind": "woofer_way",
            "roles": ["woofer"],
            "channels": list(channels),
        },
    )


def _sealed_summary(profile):
    natural = profile.targets[-1]
    return {
        "authority_valid": True,
        "runtime_block_required": True,
        "bass_owner_channels": list(profile.bass_owner["channels"]),
        "natural": {
            "fp_hz": natural.fp_hz,
            "qp": natural.qp,
            "boost_headroom_db": natural.boost_headroom_db,
            "subsonic": dict(natural.subsonic),
        },
    }


# --- the three required cases, at the shared-predicate layer ----------------- #
#
# The predicate is the reusable normalise-then-predicate core; expressing the
# three cases directly on a GraphView is the cleanest statement of the L0 rule.


_FLAT_TWEETER_GRAPH = """\
filters:
  flat:
    type: Gain
    parameters: { gain: 0.0, mute: false }
pipeline:
  - type: Filter
    channels: [0, 1]
    names: [flat]
"""

# A 2-way active graph: woofer on ch0 (low-pass), tweeter on ch1 wrapped by the
# crossover high-pass at 1600 Hz. This is the shape the DE250-horn + woofer
# preset emits (well above the 400 Hz corner floor).
_PROTECTED_ACTIVE_GRAPH = """\
filters:
  as_woofer_lp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyLowpass, freq: 1600.0, order: 4 }
  as_tweeter_hp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }
pipeline:
  - type: Filter
    channels: [0]
    names: [as_woofer_lp]
  - type: Filter
    channels: [1]
    names: [as_tweeter_hp]
"""


def test_predicate_flat_graph_with_tweeter_role_is_flagged() -> None:
    # FLAT + tweeter role: ch1 carries no high-pass, so it is flagged unsafe.
    view = view_from_emitted_text(_FLAT_TWEETER_GRAPH)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == (1,)


def test_predicate_protected_active_graph_is_allowed() -> None:
    # Properly crossed-over: the tweeter output (ch1) carries the crossover HP.
    view = view_from_emitted_text(_PROTECTED_ACTIVE_GRAPH)
    assert output_highpass_protected(view, channel=1, allowed_channels={1}) is True
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == ()


def test_predicate_passive_full_range_graph_is_not_over_blocked() -> None:
    # Passive full-range: no tweeter role -> nothing to protect -> not blocked,
    # even though the flat graph itself has no high-pass anywhere.
    view = view_from_emitted_text(_FLAT_TWEETER_GRAPH)
    assert unprotected_tweeter_outputs(view, tweeter_channels=set()) == ()


def test_predicate_lowpass_does_not_satisfy_highpass_protection() -> None:
    # A low-pass is NOT high-pass protection: it band-limits from ABOVE, leaving
    # low-frequency energy on the driver. Fail closed on the wrong LR variant.
    view = view_from_emitted_text(_PROTECTED_ACTIVE_GRAPH)
    assert output_highpass_protected(view, channel=0, allowed_channels={0}) is False


def test_predicate_fails_closed_on_empty_graph() -> None:
    view = view_from_emitted_text("")
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == (1,)


# --- corner-frequency floor (#2) --------------------------------------------- #


_TOO_LOW_CORNER_GRAPH = """\
filters:
  as_tweeter_hp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyHighpass, freq: 100.0, order: 4 }
pipeline:
  - type: Filter
    channels: [1]
    names: [as_tweeter_hp]
"""


def test_predicate_too_low_tweeter_corner_is_flagged() -> None:
    # A tweeter "high-pass" at 100 Hz leaves the excursion hazard band on a
    # ~25 dB-hotter driver: below the 400 Hz corner floor -> flagged unsafe.
    assert TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ == 400.0
    view = view_from_emitted_text(_TOO_LOW_CORNER_GRAPH)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == (1,)


def test_predicate_corner_at_floor_is_allowed() -> None:
    # Exactly at the floor is acceptable (>=), and the real 1600 Hz presets are
    # far above it — so a genuine crossover is never over-blocked.
    graph = _TOO_LOW_CORNER_GRAPH.replace("freq: 100.0", "freq: 400.0")
    view = view_from_emitted_text(graph)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == ()


def test_predicate_real_1600hz_preset_is_well_above_floor() -> None:
    # Guard the "can't over-block a real preset" claim directly: the shipped
    # DE250 + woofer crossover at 1600 Hz clears the 400 Hz floor with margin.
    yaml = emit_active_speaker_baseline_config(
        _preset("mono", 2), playback_device=ACTIVE_PCM, baseline_id="b"
    )
    view = view_from_emitted_text(yaml)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == ()


# --- mixer-boundary / subset-of-role guard (#3) ------------------------------ #


_PRE_SPLIT_PROGRAM_HP_GRAPH = """\
filters:
  program_hp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }
pipeline:
  - type: Filter
    channels: [0, 1]
    names: [program_hp]
"""


def test_predicate_pre_split_program_bus_hp_does_not_false_pass_tweeter() -> None:
    # GraphView drops the split Mixer, so a high-pass on the stereo PROGRAM bus
    # [0, 1] must NOT "cover" a post-split tweeter output. The tweeter role owns
    # only {1}; the [0, 1] step is not a subset of {1}, so it does not protect
    # ch1. This is the drift class the gate exists to catch (not reachable today:
    # preference EQ emits a plain Biquad, not a BiquadCombo).
    view = view_from_emitted_text(_PRE_SPLIT_PROGRAM_HP_GRAPH)
    assert (
        output_highpass_protected(view, channel=1, allowed_channels={1}) is False
    )
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == (1,)


def test_predicate_folded_per_role_step_covers_both_stereo_tweeters() -> None:
    # The emitter folds a role's chain into ONE step targeting BOTH stereo
    # tweeters (e.g. [1, 3]); that IS a subset of the tweeter-role set {1, 3}, so
    # both outputs are protected.
    graph = """\
filters:
  as_tweeter_hp:
    type: BiquadCombo
    parameters: { type: LinkwitzRileyHighpass, freq: 1600.0, order: 4 }
pipeline:
  - type: Filter
    channels: [1, 3]
    names: [as_tweeter_hp]
"""
    view = view_from_emitted_text(graph)
    assert unprotected_tweeter_outputs(view, tweeter_channels={1, 3}) == ()


@pytest.mark.parametrize(
    ("driver_domain", "classification"),
    [
        (False, GRAPH_APPROVED_ACTIVE_RUNTIME),
        (True, GRAPH_DRIVER_DOMAIN_BASELINE),
    ],
)
def test_sealed_natural_pair_emits_and_reproves_on_solo_and_driver_domain(
    driver_domain,
    classification,
) -> None:
    preset = _preset("stereo", 2)
    profile = _sealed_profile()
    kwargs = {
        "playback_device": ACTIVE_PCM,
        "bass_extension_profile": profile,
    }
    if driver_domain:
        text = emit_active_speaker_driver_domain_config(
            preset,
            program_channel="left",
            **kwargs,
        )
    else:
        text = emit_active_speaker_baseline_config(preset, **kwargs)
    payload = yaml.safe_load(text)

    assert payload["filters"]["bass_ext_lt"] == {
        "type": "Biquad",
        "parameters": {
            "type": "LinkwitzTransform",
            "freq_act": 61.2,
            "q_act": 0.72,
            "freq_target": 61.2,
            "q_target": 0.72,
        },
    }
    assert payload["filters"]["bass_ext_subsonic"] == {
        "type": "BiquadCombo",
        "parameters": {
            "type": "ButterworthHighpass",
            "freq": 22.0,
            "order": 4,
        },
    }
    owner_steps = [
        step for step in payload["pipeline"]
        if step.get("channels") == [0, 2]
        and "bass_ext_lt" in step.get("names", [])
    ]
    assert len(owner_steps) == 1
    names = owner_steps[0]["names"]
    assert names.index("bass_ext_lt") < names.index("bass_ext_subsonic")
    assert names.index("bass_ext_subsonic") < names.index("as_woofer_delay")
    limiter = payload["filters"]["as_woofer_baseline_limiter"]
    assert limiter["parameters"] == {"clip_limit": -1.0, "soft_clip": True}
    assert payload["devices"]["volume_limit"] <= 0.0

    proof = classify_camilla_graph(
        topology=_active_topology("stereo", "active_2_way"),
        text=text,
        bass_profile_summary=_sealed_summary(profile),
    )
    assert proof.allowed is True
    assert proof.classification == classification


def test_natural_pair_tamper_is_unsafe_and_missing_subsonic_trips_emit_gate(
    monkeypatch,
) -> None:
    preset = _preset("stereo", 2)
    profile = _sealed_profile()
    text = emit_active_speaker_baseline_config(
        preset,
        playback_device=ACTIVE_PCM,
        bass_extension_profile=profile,
    )
    tampered = text.replace("freq_target: 61.2000", "freq_target: 45.0000")
    assert classify_camilla_graph(
        topology=_active_topology("stereo", "active_2_way"),
        text=tampered,
        bass_profile_summary=_sealed_summary(profile),
    ).allowed is False

    monkeypatch.setattr(camilla_yaml, "emit_butterworth_highpass", lambda *_a, **_k: [])
    with pytest.raises(ActiveSpeakerConfigError, match="bass-extension"):
        emit_active_speaker_baseline_config(
            preset,
            playback_device=ACTIVE_PCM,
            bass_extension_profile=profile,
        )


@pytest.mark.parametrize("profile_kind", ["missing", "bypassed", "ported"])
def test_deferred_or_inactive_profiles_preserve_ordinary_baseline_bytes(
    profile_kind,
) -> None:
    preset = _preset("stereo", 2)
    ordinary = emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM
    )
    profile = None
    if profile_kind == "bypassed":
        profile = _sealed_profile(status="bypassed")
    elif profile_kind == "ported":
        profile = replace(
            _sealed_profile(),
            enclosure={
                "adapter_id": "ported_v1",
                "adapter_version": 1,
                "cabinet_fingerprint": "cabinet-a",
            },
            natural={
                "fb_hz": 43.1,
                "knee_hz": 55.0,
                "knee_slope_db_oct": 21.0,
                "fit_rms_db": 0.4,
                "natural_curve": {
                    "freqs_hz": np.geomspace(10.0, 500.0, 96).tolist(),
                    "magnitude_db": [0.0] * 96,
                },
                "notes": [],
            },
        )

    emitted = emit_active_speaker_baseline_config(
        preset,
        playback_device=ACTIVE_PCM,
        bass_extension_profile=profile,
    )

    assert emitted == ordinary
    assert "bass_ext_" not in emitted


# --- the required cases, at the camilla_yaml emit gate (all FOUR emitters) ---- #
#
# Each emitter is monkeypatched at the SPECIFIC chain builder it uses so its own
# gate call is exercised — deleting the gate from any one emitter would then ship
# red. startup + commissioning build via _driver_filter_chain; baseline +
# driver-domain build via _driver_baseline_filter_chain.

_REFUSAL_CASES = [
    pytest.param(
        "_driver_filter_chain",
        lambda p: emit_active_speaker_startup_config(p, playback_device=ACTIVE_PCM),
        id="startup",
    ),
    pytest.param(
        "_driver_filter_chain",
        lambda p: emit_active_speaker_commissioning_config(
            p, playback_device=ACTIVE_PCM
        ),
        id="commissioning",
    ),
    pytest.param(
        "_driver_baseline_filter_chain",
        lambda p: emit_active_speaker_baseline_config(
            p, playback_device=ACTIVE_PCM, baseline_id="broken"
        ),
        id="baseline",
    ),
    pytest.param(
        "_driver_baseline_filter_chain",
        lambda p: emit_active_speaker_driver_domain_config(
            p, playback_device=ACTIVE_PCM, program_channel="left"
        ),
        id="driver_domain",
    ),
]


@pytest.mark.parametrize("chain_attr, emit", _REFUSAL_CASES)
def test_emit_gate_refuses_flat_graph_with_tweeter_role(
    monkeypatch, chain_attr: str, emit: Callable[[ActiveSpeakerPreset], str]
) -> None:
    # "flat + tweeter role" at the emitter: strip the tweeter's crossover /
    # protective high-pass from the chain builder THIS emitter uses, so the
    # emitted graph routes full-range program to the compression driver. The
    # fail-closed gate on each emitter must refuse it (raise) rather than ship it.
    original = getattr(camilla_yaml, chain_attr)
    monkeypatch.setattr(camilla_yaml, chain_attr, _hp_stripping(original))
    with pytest.raises(ActiveSpeakerConfigError, match="protective high-pass"):
        emit(_preset("mono", 2))


def test_emit_gate_names_the_unprotected_output(monkeypatch) -> None:
    # The refusal names the offending DAC output (1-based) so an operator/log has
    # an honest hint. The mono 2-way preset puts the tweeter on DAC output 2.
    original = camilla_yaml._driver_baseline_filter_chain
    monkeypatch.setattr(
        camilla_yaml, "_driver_baseline_filter_chain", _hp_stripping(original)
    )
    with pytest.raises(ActiveSpeakerConfigError, match=r"output\(s\) 2"):
        emit_active_speaker_baseline_config(
            _preset("mono", 2), playback_device=ACTIVE_PCM, baseline_id="broken"
        )


def test_emit_gate_logs_before_raising(monkeypatch, caplog) -> None:
    # No silent failure: the block emits a structured event before raising.
    original = camilla_yaml._driver_baseline_filter_chain
    monkeypatch.setattr(
        camilla_yaml, "_driver_baseline_filter_chain", _hp_stripping(original)
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(ActiveSpeakerConfigError):
            emit_active_speaker_baseline_config(
                _preset("mono", 2), playback_device=ACTIVE_PCM, baseline_id="broken"
            )
    assert "event=active_speaker.emit_gate" in caplog.text
    assert "blocked_unprotected_tweeter" in caplog.text


@pytest.mark.parametrize("layout", ["mono", "stereo"])
@pytest.mark.parametrize("way", [2, 3])
def test_emit_gate_allows_protected_active_baseline(layout: str, way: int) -> None:
    # A properly crossed-over active baseline (the real emitter output) passes the
    # gate for every supported layout/way and returns YAML.
    yaml = emit_active_speaker_baseline_config(
        _preset(layout, way),
        playback_device=ACTIVE_PCM,
        baseline_id=f"baseline-{layout}-{way}way",
    )
    assert "pipeline:" in yaml


def test_emit_gate_allows_protected_startup_and_commissioning() -> None:
    # The muted startup + per-output-masked commissioning graphs still wire the
    # tweeter high-pass, so the gate passes them too.
    startup = emit_active_speaker_startup_config(
        _preset("mono", 2), playback_device=ACTIVE_PCM
    )
    assert "pipeline:" in startup
    commissioning = emit_active_speaker_commissioning_config(
        _preset("mono", 2), playback_device=ACTIVE_PCM
    )
    assert "pipeline:" in commissioning


def test_emit_gate_allows_protected_driver_domain_follower() -> None:
    yaml = emit_active_speaker_driver_domain_config(
        _preset("mono", 2),
        playback_device=ACTIVE_PCM,
        program_channel="left",
    )
    assert "pipeline:" in yaml
