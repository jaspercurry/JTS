# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""L0 graph-safety emit gate: no active-speaker graph ships an unprotected tweeter.

Pins the load-bearing L0 hearing-safety invariant
(docs/HANDOFF-audio-measurement-core.md): an output carrying a tweeter /
compression-driver role MUST have a protective high-pass (its crossover
high-pass and/or a dedicated protective high-pass). A compression driver is
~25 dB more sensitive than the woofer, so a graph that routes full-range program
to a bare tweeter output is a shrill / hot-tweeter hazard.

Two layers are pinned:

* the shared normalise-then-predicate primitive
  (``graph_safety.unprotected_tweeter_outputs`` over a ``GraphView``): a flat
  graph with a tweeter role is flagged; a properly crossed-over graph is clean;
  a graph with no tweeter role (passive full-range) is clean — never over-blocked;
* the fail-closed wiring at the ``camilla_yaml`` active-speaker emit gate: every
  DAC emitter refuses (raises + logs) a graph whose tweeter output lost its
  high-pass, and passes a normal protected graph and a tweeterless graph.

The active emitters wire the protection by construction, so the "flat + tweeter
role" emit case is provoked by stripping the tweeter high-pass from the chain
builder — proving the gate is a real guard, not decorative.
"""

from __future__ import annotations

import pytest

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
    output_highpass_protected,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _preset(layout: str = "mono", way: int = 2) -> ActiveSpeakerPreset:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return ActiveSpeakerPreset.from_mapping(raw)


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
# crossover high-pass. This is the shape the DE250-horn + woofer preset emits.
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
    assert output_highpass_protected(view, channel=1) is True
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
    assert output_highpass_protected(view, channel=0) is False


def test_predicate_fails_closed_on_empty_graph() -> None:
    view = view_from_emitted_text("")
    assert unprotected_tweeter_outputs(view, tweeter_channels={1}) == (1,)


# --- the three required cases, at the camilla_yaml emit gate ------------------ #


def test_emit_gate_refuses_flat_graph_with_tweeter_role(monkeypatch) -> None:
    # "flat + tweeter role" at the emitter: strip the tweeter's crossover /
    # protective high-pass so the emitted graph routes full-range program to the
    # compression driver. The fail-closed gate must refuse it (raise) rather than
    # ship it. Proves the gate is a real guard.
    original = camilla_yaml._driver_baseline_filter_chain

    def _hp_stripped(preset: ActiveSpeakerPreset, role: str) -> list[str]:
        names = original(preset, role)
        if role == "tweeter":
            return [name for name in names if not name.endswith("_hp")]
        return names

    monkeypatch.setattr(camilla_yaml, "_driver_baseline_filter_chain", _hp_stripped)

    with pytest.raises(ActiveSpeakerConfigError, match="protective high-pass"):
        emit_active_speaker_baseline_config(
            _preset("mono", 2),
            playback_device=ACTIVE_PCM,
            baseline_id="broken",
        )


def test_emit_gate_names_the_unprotected_output(monkeypatch) -> None:
    # The refusal names the offending DAC output (1-based) so an operator/log has
    # an honest hint. The mono 2-way preset puts the tweeter on DAC output 2.
    original = camilla_yaml._driver_baseline_filter_chain

    def _hp_stripped(preset: ActiveSpeakerPreset, role: str) -> list[str]:
        names = original(preset, role)
        if role == "tweeter":
            return [name for name in names if not name.endswith("_hp")]
        return names

    monkeypatch.setattr(camilla_yaml, "_driver_baseline_filter_chain", _hp_stripped)

    with pytest.raises(ActiveSpeakerConfigError, match=r"output\(s\) 2"):
        emit_active_speaker_baseline_config(
            _preset("mono", 2),
            playback_device=ACTIVE_PCM,
            baseline_id="broken",
        )


def test_emit_gate_logs_before_raising(monkeypatch, caplog) -> None:
    # No silent failure: the block emits a structured event before raising.
    original = camilla_yaml._driver_baseline_filter_chain

    def _hp_stripped(preset: ActiveSpeakerPreset, role: str) -> list[str]:
        names = original(preset, role)
        if role == "tweeter":
            return [name for name in names if not name.endswith("_hp")]
        return names

    monkeypatch.setattr(camilla_yaml, "_driver_baseline_filter_chain", _hp_stripped)

    with caplog.at_level("ERROR"):
        with pytest.raises(ActiveSpeakerConfigError):
            emit_active_speaker_baseline_config(
                _preset("mono", 2),
                playback_device=ACTIVE_PCM,
                baseline_id="broken",
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
