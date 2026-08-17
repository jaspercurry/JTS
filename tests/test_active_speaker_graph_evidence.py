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
    emit_active_speaker_commissioning_config,
    graph_evidence as ge,
)
from jasper.active_speaker.runtime_contract import (
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph as _classify_camilla_graph,
)
from jasper.active_speaker.staging import driver_commission_audible_evidence

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology, _active_yaml


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault("bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY)
    return _classify_camilla_graph(*args, **kwargs)


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


# --- #2625: the live-mask proof inspects per-step `bypassed` ------------------


def _emitted_commission_yaml(audible: set[int]) -> tuple[str, dict]:
    """A two-way per-driver commissioning graph plus the intent it satisfies."""

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    text = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=audible
    )
    intent = driver_commission_audible_evidence(
        text, preset=preset, audible_outputs=audible
    )
    return text, intent


def _live(text: str, intent: dict) -> dict:
    return ge.running_commission_evidence(
        text,
        audible_outputs=intent["audible_outputs"],
        muted_outputs=intent["muted_outputs"],
        tweeter_outputs=intent["tweeter_outputs"],
        protective_hp_hz=intent["protective_highpass_hz"],
    )


def test_running_evidence_rejects_a_bypassed_step_that_still_reads_as_masked():
    """A hand-edited `bypassed` step no longer passes as a masked graph.

    THE HOLE THIS CLOSES. ``running_commission_evidence`` proved the live mask
    with ``output_hard_muted_and_wired``, which reads :class:`GraphView` —
    filters and channels, never the per-step bypass flag. CamillaDSP SKIPS a
    bypassed step entirely, so a graph that carries JTS's own mute filter names
    AND ``bypassed: true`` on the step wiring them read as fully masked while
    the driver ran live.

    The tamper keeps every mute name and every wiring intact and changes
    nothing but the flag, so ``audible_mask_correct`` STAYS TRUE — asserted
    below. That assertion is the point: it shows the old proof would have
    passed this graph, and that the new check, not a side effect of the
    tamper, is what refuses it.
    """
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    text, intent = _emitted_commission_yaml(woofer)

    # Positive control: untampered, the graph passes every check.
    clean = _live(text, intent)
    assert clean["passed"] is True, clean["checks"]
    assert clean["checks"]["no_bypassed_pipeline_step"] is True

    # Tamper: bypass the step that wires the TWEETER's hard mute. Nothing else
    # moves — same filters, same names, same channels.
    mute_name = ge.output_commission_mute_name(sorted(tweeter)[0])
    parsed = yaml_lib.safe_load(text)
    bypassed_steps = 0
    for step in parsed.get("pipeline", []):
        names = step.get("names")
        if isinstance(names, list) and mute_name in names:
            step["bypassed"] = True
            bypassed_steps += 1
    assert bypassed_steps == 1, "expected exactly one step to wire the mute"
    tampered = yaml_lib.safe_dump(parsed)

    live = _live(tampered, intent)

    # The OLD proof still says "masked" — this is the hole, stated as an
    # assertion rather than as prose.
    assert live["checks"]["audible_mask_correct"] is True
    # The NEW check is what refuses it, and the roll-up follows.
    assert live["checks"]["no_bypassed_pipeline_step"] is False
    assert live["passed"] is False


def test_running_evidence_bypass_check_is_wholesale_and_fails_closed():
    """Any bypassed step anywhere refuses, and an unreadable pipeline does too.

    Wholesale by design, matching ``graph_safety.output_terminally_muted``
    fact 3 and both bench derivation checkers: no JTS emitter writes
    ``bypassed``, so its presence means the graph was hand-edited, and picking
    which bypassed step is harmless is exactly the generous reasoning this
    evidence exists to reject.
    """
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    text, intent = _emitted_commission_yaml(woofer)

    # (a) A bypassed step that touches no mute at all still refuses.
    parsed = yaml_lib.safe_load(text)
    parsed["pipeline"].insert(0, {"type": "Filter", "channels": [], "names": [],
                                  "bypassed": True})
    assert _live(yaml_lib.safe_dump(parsed), intent)["passed"] is False

    # (b) Both spellings of CamillaDSP's boolean refuse — the real `True` and
    #     the string `"true"` a text-parse dialect can hand back. That pair is
    #     the whole of `graph_safety.truthy_bool`'s vocabulary, deliberately:
    #     CamillaDSP parses with serde, where `yes`/`on`/`1` are not booleans
    #     at all, and the readback this function inspects is re-serialized from
    #     CamillaDSP's own parsed struct. Reusing that predicate rather than a
    #     wider local one is what keeps this check agreeing with
    #     `output_terminally_muted` instead of drifting beside it.
    for spelling in (True, "true", "TRUE"):
        parsed = yaml_lib.safe_load(text)
        parsed["pipeline"][0]["bypassed"] = spelling
        live = _live(yaml_lib.safe_dump(parsed), intent)
        assert live["checks"]["no_bypassed_pipeline_step"] is False, spelling

    # (c) `bypassed: false` is NOT a refusal — the check reads the value, it
    #     does not just look for the key.
    parsed = yaml_lib.safe_load(text)
    parsed["pipeline"][0]["bypassed"] = False
    assert _live(yaml_lib.safe_dump(parsed), intent)["passed"] is True

    # (d) Fails closed on a graph with no readable pipeline.
    parsed = yaml_lib.safe_load(text)
    parsed["pipeline"] = "not a list"
    live = _live(yaml_lib.safe_dump(parsed), intent)
    assert live["checks"]["no_bypassed_pipeline_step"] is False
    assert live["passed"] is False
