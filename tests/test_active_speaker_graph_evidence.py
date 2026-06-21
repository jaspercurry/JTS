# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""graph_evidence single-source agreement.

The active-speaker safety verifiers used to each hardcode/re-derive the filter
names the emitter writes (three copies of ``"as_tweeter_startup_limiter"`` etc.).
They now import those names from one place — the emitter, re-exposed via
``graph_evidence`` — so a name change can't silently desync a gate from the
graph it inspects.

These tests pin that single-sourcing *behaviourally*, not by asserting the
aliases equal each other (which would be tautological):

  1. the canonical names actually appear in an emitted config, so an emitter
     rename that isn't mirrored in graph_evidence fails loudly here; and
  2. the two INDEPENDENT verifiers (``runtime_contract.classify_camilla_graph``
     off-disk, ``staging.driver_commission_audible_evidence`` against the
     emitted text) both fail closed when the canonical tweeter-guard name is
     changed — i.e. they agree, keyed off the same name.
"""

from __future__ import annotations

import yaml as yaml_lib

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    audible_outputs_for_role,
    graph_evidence as ge,
)
from jasper.active_speaker.runtime_contract import classify_camilla_graph
from jasper.active_speaker.staging import driver_commission_audible_evidence

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology, _active_yaml


def test_canonical_names_appear_in_the_emitted_commissioning_config():
    # A tweeter-audible per-driver commissioning config must literally contain
    # the names graph_evidence exposes. If the emitter ever renames a filter
    # without updating the canonical alias, this assertion fails — instead of a
    # verifier silently looking for a filter that no longer exists.
    text = _active_yaml("mono", 2, {1})  # mono 2-way: woofer=0, tweeter=1
    assert ge.protective_tweeter_hp_name("tweeter") in text
    assert ge.driver_limiter_name("tweeter") in text
    assert ge.output_commission_mute_name(1) in text


def test_both_verifiers_reject_a_renamed_tweeter_guard():
    topology = _active_topology("mono", "active_2_way")
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    text = _active_yaml("mono", 2, tweeter)

    # Baseline: the legit tweeter-audible config is accepted by BOTH verifiers.
    assert classify_camilla_graph(topology=topology, text=text).allowed is True
    assert (
        driver_commission_audible_evidence(
            text, preset=preset, audible_outputs=tweeter
        )["passed"]
        is True
    )

    # Tamper: rename the protective-high-pass filter (definition + every pipeline
    # wiring) so the canonical name no longer exists in the graph.
    hp = ge.protective_tweeter_hp_name("tweeter")
    parsed = yaml_lib.safe_load(text)
    parsed["filters"]["as_tweeter_renamed_hp"] = parsed["filters"].pop(hp)
    for step in parsed.get("pipeline", []):
        names = step.get("names")
        if isinstance(names, list):
            step["names"] = [
                "as_tweeter_renamed_hp" if n == hp else n for n in names
            ]
    renamed = yaml_lib.safe_dump(parsed)

    # Both verifiers, keyed off the single-sourced name, now fail closed — they
    # agree on the canonical tweeter-guard name.
    assert classify_camilla_graph(topology=topology, text=renamed).allowed is False
    assert (
        driver_commission_audible_evidence(
            renamed, preset=preset, audible_outputs=tweeter
        )["passed"]
        is False
    )
