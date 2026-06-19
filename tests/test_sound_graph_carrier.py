"""Graph-carrier dispatch invariants for preference-EQ apply (PR-1).

Design-of-record: docs/HANDOFF-dsp-graph-carrier.md. These tests pin the
safety invariants that keep preference EQ from ever silently dropping driver
protection:

- inv 1: the carrier never resolves a roleful/active graph to a stereo host
  (which would re-emit through the stereo template and drop the
  crossover/limiter/HP); it agrees with the runtime safety classifier on the
  SAME bytes, so the two cannot drift.
- inv 3: the stereo emitter (``emit_sound_config``) is never reachable for an
  active or unknown graph.
- inv 6: refusals are typed with a stable ``reason_code`` and a 200-shaped
  body — no silent failure, no 502.
"""
from __future__ import annotations

from unittest import mock

import pytest

from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_FLAT_FULL_RANGE,
    classify_camilla_graph,
)
from jasper.sound.camilla_yaml import BASE_CONFIG_PATH
from jasper.sound.graph_carrier import (
    CarrierCannotHostEq,
    ReemitResult,
    carrier_for_loaded_config,
)
from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
    _flat_yaml,
    _full_range_stereo,
)

_STEREO_HOST_KINDS = {"base_flat", "sound_or_correction"}


# --- resolution / recognizer mutual-exclusivity -------------------------

def test_base_config_resolves_to_base_flat(tmp_path):
    # is_base_config is an exact-path check; no file read required.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    assert carrier.kind == "base_flat"


def test_jts_generated_names_resolve_to_sound_or_correction(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for name in ("sound_current.yml", "sound_audition.yml", "correction_abc_123.yml"):
        path = config_dir / name
        path.write_text(_flat_yaml())
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        assert carrier.kind == "sound_or_correction", name


def test_active_baseline_resolves_by_source_header_not_path(tmp_path):
    # Detection keys on the '# Source:' header the emitter writes, so it holds
    # regardless of file name/location — including an env-overridden
    # JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH. Name it like a sound config
    # to prove the header (not the name) decides.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active"


def test_non_baseline_active_graph_is_content_fenced(tmp_path):
    # Startup/commissioning graphs are also roleful and must be content-fenced,
    # not filename-fenced: a commissioning config misnamed like a sound config
    # must still resolve to the refusing active carrier — never a stereo host.
    # Closes the non-baseline crossover-drop fence the review flagged.
    from tests.test_active_speaker_runtime_contract import _active_yaml

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"  # deliberately a sound-like name
    path.write_text(_active_yaml("mono", 2, {1}))  # a commissioning graph

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active"
    assert carrier.kind not in _STEREO_HOST_KINDS
    with pytest.raises(CarrierCannotHostEq) as err:
        carrier.reemit(mock.sentinel.profile, profile_id="x")
    assert err.value.reason_code == "eq_on_active_not_wired"


def test_unknown_or_missing_config_fails_closed_to_unknown(tmp_path):
    foreign = tmp_path / "custom.yml"
    foreign.write_text("# handmade\nfilters: {}\n")
    assert carrier_for_loaded_config(str(foreign), config_dir=tmp_path).kind == "unknown"
    # A path the daemon could not read, or no path at all, must not fall
    # through to a stereo host.
    assert carrier_for_loaded_config(str(tmp_path / "gone.yml"), config_dir=tmp_path).kind == "unknown"
    assert carrier_for_loaded_config(None, config_dir=tmp_path).kind == "unknown"
    assert carrier_for_loaded_config("", config_dir=tmp_path).kind == "unknown"


# --- inv 1: carrier kind agrees with the safety classifier --------------

def test_active_baseline_is_roleful_and_never_a_stereo_host(tmp_path):
    # The SAME bytes the safety classifier approves as a roleful active-runtime
    # graph must resolve to the active carrier — never a stereo host. This
    # closes the carrier<->classifier loop (no drift).
    topology = _active_topology("mono", "active_2_way")
    yaml = _active_baseline_yaml("mono", 2)
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(yaml)

    graph = classify_camilla_graph(topology=topology, text=yaml)
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True

    carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
    assert carrier.kind == "active"
    assert carrier.kind not in _STEREO_HOST_KINDS


def test_flat_graph_is_flat_and_hosted_by_a_stereo_carrier(tmp_path):
    # The mirror of the active case: a flat full-range graph the classifier
    # allows IS hostable by the stereo emitter.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_flat_yaml())

    graph = classify_camilla_graph(topology=_full_range_stereo(), text=_flat_yaml())
    assert graph.classification == GRAPH_FLAT_FULL_RANGE

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind in _STEREO_HOST_KINDS


# --- inv 3: emit_sound_config is never reachable for unhostable graphs ---

def test_emit_sound_config_never_called_for_active_or_unknown(tmp_path):
    active = tmp_path / "baseline.yml"
    active.write_text(_active_baseline_yaml("mono", 2))

    with mock.patch("jasper.sound.graph_carrier.emit_sound_config") as emit:
        active_carrier = carrier_for_loaded_config(str(active), config_dir=tmp_path)
        with pytest.raises(CarrierCannotHostEq) as active_err:
            active_carrier.reemit(mock.sentinel.profile, profile_id="x")
        assert active_err.value.reason_code == "eq_on_active_not_wired"

        unknown_carrier = carrier_for_loaded_config(str(tmp_path / "gone.yml"), config_dir=tmp_path)
        with pytest.raises(CarrierCannotHostEq) as unknown_err:
            unknown_carrier.reemit(mock.sentinel.profile, profile_id="x")
        assert unknown_err.value.reason_code == "unknown_config"

        emit.assert_not_called()


def test_base_flat_reemits_with_no_room_peqs(tmp_path):
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml-text"
    ) as emit:
        carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
        result = carrier.reemit(mock.sentinel.profile, profile_id="id", output_trim_db=1.0)

    assert isinstance(result, ReemitResult)
    assert result.yaml == "yaml-text"
    assert result.room_peq_count == 0
    emit.assert_called_once()
    assert emit.call_args.kwargs["room_peqs"] == []
    assert emit.call_args.kwargs["output_trim_db"] == 1.0


def test_sound_carrier_extracts_and_forwards_room_peqs(tmp_path):
    # The SoundOrCorrection carrier preserves room PEQs by extracting them from
    # the loaded config and forwarding them to the emitter (the verbatim
    # relocation of the former arm). This pins the carrier's WIRING; the
    # extractor's own parsing is covered by test_sound_camilla_yaml.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "correction_abc_123.yml"
    path.write_text("# jts sound/correction config\n")
    preserved = [object(), object()]

    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml-text"
    ) as emit, mock.patch(
        "jasper.sound.graph_carrier.extract_room_peqs_from_config",
        return_value=preserved,
    ) as extract:
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        result = carrier.reemit(mock.sentinel.profile, profile_id="id")

    extract.assert_called_once_with(str(path))
    assert emit.call_args.kwargs["room_peqs"] is preserved
    assert result.room_peq_count == 2


# --- inv 6: refusals are typed with a stable reason_code ----------------

@pytest.mark.parametrize(
    "reason_code",
    ["eq_on_active_not_wired", "unknown_config"],
)
def test_refusal_payload_is_typed_and_stable(reason_code):
    err = CarrierCannotHostEq(reason_code, "household-readable message")
    assert isinstance(err, RuntimeError)  # propagates like any other error
    assert err.to_payload() == {
        "status": "blocked",
        "reason_code": reason_code,
        "message": "household-readable message",
    }
