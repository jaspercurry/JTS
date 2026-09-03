# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from jasper.correction import runtime_safety as runtime_safety_module
from jasper.active_speaker.runtime_contract import (
    GraphSafety,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
)
from jasper.correction.runtime_safety import (
    CorrectionRuntimeSafetyError,
    assert_correction_graph_safe,
)

from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
    _flat_yaml,
    _full_range_mono,
    _full_range_stereo,
)


def test_assert_correction_graph_safe_rejects_mono_width_mismatch() -> None:
    # A hand-built 2-channel graph on a mono topology puts its second channel on
    # an output the household never declared. Refused; the reason now NAMES the
    # output, because a single (non-composite) sink maps Camilla channel i to
    # physical output i.
    #
    # NOTE what this does and does NOT prove: it pins the CHECKER against bytes
    # it is handed. It says nothing about what the correction lane emits — the
    # lane is now width-matched via the carrier, so correction on a mono box
    # PASSES (see the keystone test below, which drives the real carrier).
    with pytest.raises(
        CorrectionRuntimeSafetyError, match="emits on physical output\\(s\\) 1"
    ):
        assert_correction_graph_safe(_flat_yaml(), topology=_full_range_mono())


def test_correction_carrier_reemit_on_mono_is_width_matched_and_passes(
    tmp_path, monkeypatch
) -> None:
    """The keystone: drive the REAL correction carrier, then judge its output.

    `_SoundOrCorrectionCarrier` inherits the stereo-host width match, so room
    correction on a mono box now emits the unclaimed channel hard muted and
    clears its own safety gate. This is the intended end state for #2180 — and
    it is the assertion that would fail if the inheritance were ever severed,
    which the checker-only test above cannot detect.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import SimpleEq, SoundProfile

    topology = _full_range_mono()
    artifact = tmp_path / "output_topology.json"
    artifact.write_text(json.dumps(topology.to_dict()), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(artifact))

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    loaded = config_dir / "correction_abc_1.yml"
    loaded.write_text(emit_sound_config(SoundProfile(enabled=False)), encoding="utf-8")

    carrier = carrier_for_loaded_config(str(loaded), config_dir=config_dir)
    assert carrier.kind == "sound_or_correction"
    result = carrier.reemit(SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=3.0)))

    assert "as_out1_commission_mute" in result.yaml
    # The gate the correction apply path actually calls, on the real bytes.
    assert_correction_graph_safe(result.yaml, topology=topology)


def test_assert_correction_graph_safe_judges_bytes_not_the_lane() -> None:
    """The asymmetry is at THIS checker, and only here.

    Carrier-produced bytes carry the mute and pass; a bare `emit_sound_config`
    graph does not and is refused. That is a property of
    `assert_correction_graph_safe`, which reads the graph it is handed.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    direct = emit_sound_config(SoundProfile(enabled=False))

    with pytest.raises(
        CorrectionRuntimeSafetyError, match="emits on physical output\\(s\\) 1"
    ):
        assert_correction_graph_safe(direct, topology=_full_range_mono())


def test_assert_correction_graph_safe_preserves_active_baseline_omission() -> None:
    with pytest.raises(
        CorrectionRuntimeSafetyError,
        match="requires explicit bass-extension profile evidence",
    ):
        assert_correction_graph_safe(
            _active_baseline_yaml("mono", 2),
            topology=_active_topology("mono", "active_2_way"),
        )


def test_assert_correction_graph_safe_accepts_explicit_no_profile_evidence() -> None:
    assert_correction_graph_safe(
        _active_baseline_yaml("mono", 2),
        topology=_active_topology("mono", "active_2_way"),
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    )


def test_assert_correction_graph_safe_rejects_non_mapping_before_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def classify(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("malformed evidence must refuse before classification")

    monkeypatch.setattr(runtime_safety_module, "classify_camilla_graph", classify)

    with pytest.raises(
        CorrectionRuntimeSafetyError,
        match="bass authority evidence is invalid",
    ):
        assert_correction_graph_safe(
            _flat_yaml(),
            topology=_full_range_stereo(),
            bass_profile_summary="invalid",
        )

    assert called is False


def test_assert_correction_graph_safe_forwards_mapping_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = {
        "authority_valid": True,
        "runtime_block_required": False,
    }
    observed: list[object] = []

    def classify(*args, bass_profile_summary=None, **kwargs):
        observed.append(bass_profile_summary)
        return GraphSafety(classification="flat_full_range", allowed=True)

    monkeypatch.setattr(runtime_safety_module, "classify_camilla_graph", classify)

    assert_correction_graph_safe(
        _flat_yaml(),
        topology=_full_range_stereo(),
        bass_profile_summary=summary,
    )

    assert observed == [summary]
    assert observed[0] is summary


def test_assert_correction_graph_safe_keeps_flat_omission_legal() -> None:
    assert_correction_graph_safe(
        _flat_yaml(),
        topology=_full_range_stereo(),
    )
