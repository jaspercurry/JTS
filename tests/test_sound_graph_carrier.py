# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest
import yaml

from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_FLAT_FULL_RANGE,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph as _classify_camilla_graph,
)
from jasper.camilla_config_contract import PeqFilter
from jasper.fanin_coupling import (
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    capture_kwargs_for_coupling,
)
from jasper.sound.camilla_yaml import BASE_CONFIG_PATH, emit_sound_config
from jasper.sound.graph_carrier import (
    CarrierCannotHostEq,
    ReemitResult,
    carrier_for_loaded_config,
)
from jasper.sound.profile import SimpleEq, SoundProfile, build_sound_filters
from jasper.sound.settings import SoundSettings, output_trim_db
from tests.test_active_speaker_runtime_contract import (
    _applied_baseline,
    _active_baseline_yaml,
    _active_topology,
    _flat_yaml,
    _full_range_stereo,
    _sealed_profile,
)

_STEREO_HOST_KINDS = {"base_flat", "sound_or_correction"}


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault("bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY)
    return _classify_camilla_graph(*args, **kwargs)


def _real_active_applied_baseline(tmp_path):
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from tests.test_active_speaker_baseline_profile import (
        _draft,
        _dual_apple_topology,
        _measurements,
        _valid_config,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=build_crossover_preview(draft),
        measurements=_measurements(topology, tmp_path),
        write=False,
        state_path=tmp_path / "active-speaker-profile.json",
        config_path=tmp_path / "configs" / "active-speaker-baseline.yml",
        validate=_valid_config,
    )
    applied["status"] = "applied"
    return topology, applied


def _write_program_overlay_sources(
    tmp_path,
    *,
    profile: SoundProfile,
    settings: SoundSettings,
):
    preference_path = tmp_path / "sound-profile.json"
    preference_path.write_text(
        json.dumps(profile.to_dict()),
        encoding="utf-8",
    )
    settings_path = tmp_path / "sound-settings.json"
    settings_path.write_text(
        json.dumps(settings.to_dict()),
        encoding="utf-8",
    )
    return preference_path, settings_path


def _program_bake_yaml() -> str:
    from jasper.active_speaker.camilla_yaml import ACTIVE_PROGRAM_BAKE_SOURCE

    return f"""---
# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}
devices:
  samplerate: 48000
  chunksize: 1024
  queuelimit: 4
  target_level: 2048
  volume_limit: 0.0
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "plug:jasper_capture"
    format: S32_LE
  playback:
    type: File
    channels: 2
    filename: "/run/jasper-snapserver/snapfifo"
    format: S16_LE

filters:
  flat:
    type: Gain
    parameters: {{ gain: 0.0000, inverted: false, mute: false }}

mixers:
  master_gain:
    channels: {{ in: 2, out: 2 }}
    mapping: []

pipeline: []
"""


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


def test_active_graph_resolves_by_structure_not_name_or_header(tmp_path):
    # Detection reuses the safety classifier's STRUCTURAL signal (the per-driver
    # split mixer), so a roleful graph is fenced even when (a) it is misnamed
    # like a sound config AND (b) its '# Source:' header has been stripped (a
    # CamillaDSP round-trip drops comments). Content beats both name and
    # comment — this is what makes invariant 1 literally true, not merely true
    # for header-bearing bytes.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    yaml = _active_baseline_yaml("mono", 2)
    stripped = "\n".join(ln for ln in yaml.splitlines() if "Source:" not in ln)
    assert "Source:" not in stripped  # header genuinely removed
    path = config_dir / "sound_current.yml"  # misnamed like a sound config
    path.write_text(stripped)

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active"
    assert carrier.kind not in _STEREO_HOST_KINDS
    with pytest.raises(CarrierCannotHostEq) as err:
        carrier.reemit(mock.sentinel.profile, profile_id="x")
    assert err.value.reason_code == "eq_on_active_not_wired"


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
    # inv 3 still holds with PR-3: a SOLO active baseline now hosts EQ, but it is
    # recomposed via the ACTIVE emitter (recompose helper), never the stereo
    # emit_sound_config. Mock the recompose so the active SUCCESS path is reached
    # without touching emit_sound_config; the unknown carrier still refuses.
    active = tmp_path / "baseline.yml"
    active.write_text(_active_baseline_yaml("mono", 2))

    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config"
    ) as emit, mock.patch(
        "jasper.sound.graph_carrier._recompose_active_baseline_with_eq",
        return_value="active-yaml",
    ) as recompose, mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ):
        active_carrier = carrier_for_loaded_config(str(active), config_dir=tmp_path)
        result = active_carrier.reemit(mock.sentinel.profile, profile_id="x")
        assert isinstance(result, ReemitResult)
        assert result.yaml == "active-yaml"
        recompose.assert_called_once()

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


def test_reemit_forwards_explicit_member_kwargs(tmp_path):
    # The bonded-leader bake injects its already-resolved cfg kwargs (the pipe
    # sink + rate_adjust off) rather than the carrier's default disk read.
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml"
    ) as emit:
        carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
        carrier.reemit(
            mock.sentinel.profile,
            profile_id="id",
            member_kwargs={"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False},
        )
    assert emit.call_args.kwargs["playback_pipe_path"] == "/run/snapfifo"
    assert emit.call_args.kwargs["enable_rate_adjust"] is False


# --- L0: a stereo-host graph can't host EQ under a protected-tweeter topology -

def _persist_topology(topology, tmp_path, monkeypatch):
    path = tmp_path / "output_topology.json"
    path.write_text(json.dumps(topology.to_dict()), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))


def test_stereo_host_refuses_eq_under_protected_tweeter_topology(tmp_path, monkeypatch):
    # The L0 fix: a flat 2-channel program graph carries no per-driver
    # protection, so the stereo-host carrier refuses (typed) when the saved
    # topology assigns a protected tweeter. It refuses in reemit() BEFORE
    # emitting — covering both the live-draft SetConfig path (no out_path, which
    # skips the durable pre-check) and the durable write — so a flat graph can
    # never reach the DAC under a protected-tweeter topology. The route maps the
    # CarrierCannotHostEq to an honest blocked-200 (inv 6).
    from jasper.sound.profile import SoundProfile

    _persist_topology(
        _active_topology("stereo", "active_2_way"),
        tmp_path,
        monkeypatch,
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_flat_yaml())

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind in _STEREO_HOST_KINDS
    # can_host_eq=False makes the durable pre-check refuse early (no spurious
    # prepare_failed); reemit re-asserts for the pre-check-less live-draft path.
    assert carrier.can_host_eq is False
    with pytest.raises(CarrierCannotHostEq) as exc:
        carrier.reemit(SoundProfile(enabled=False), member_kwargs={})  # live-draft shape
    assert exc.value.reason_code == "flat_graph_protected_tweeter"


def test_stereo_host_hosts_eq_under_full_range_topology(tmp_path, monkeypatch):
    # The common passive-stereo speaker: no protected tweeter -> the stereo host
    # still hosts EQ exactly as before (no regression for non-active speakers).
    from jasper.sound.profile import SoundProfile

    _persist_topology(_full_range_stereo(), tmp_path, monkeypatch)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_flat_yaml())

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.can_host_eq is True
    result = carrier.reemit(SoundProfile(enabled=False), member_kwargs={})
    assert isinstance(result, ReemitResult)


def test_program_bake_carrier_hosts_eq_via_pipe_under_active_topology(
    tmp_path,
    monkeypatch,
):
    # JTS5 regression: an active leader runs camilla#1 as a program-domain bake
    # into Snapcast's FIFO. It is flat, but not DAC-bound; re-emission is safe
    # only when grouping policy keeps the File -> pipe sink and rate_adjust off.
    from jasper.sound.profile import SoundProfile

    _persist_topology(
        _active_topology("stereo", "active_2_way"),
        tmp_path,
        monkeypatch,
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "grouping_active_leader_bake.yml"
    path.write_text(_program_bake_yaml(), encoding="utf-8")
    out_path = config_dir / "sound_current.yml"

    with mock.patch(
        "jasper.multiroom.member_config.member_camilla_kwargs",
        return_value={
            "enable_rate_adjust": False,
            "channel_split": None,
            "playback_pipe_path": "/run/jasper-snapserver/snapfifo",
        },
    ):
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        assert carrier.kind == "active_leader_program_bake"
        assert carrier.can_host_eq is True
        result = carrier.reemit(
            SoundProfile(enabled=False),
            room_peqs=[],
            out_path=out_path,
        )

    assert isinstance(result, ReemitResult)
    assert result.room_peq_count == 0
    assert (
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_program_bake_config"
    ) in result.yaml
    assert "# Source: jasper.sound.camilla_yaml.emit_sound_config" not in result.yaml
    assert "/run/jasper-snapserver/snapfifo" in result.yaml
    assert "enable_rate_adjust: false" in result.yaml
    assert out_path.read_text(encoding="utf-8") == result.yaml


def test_generic_jts_pipe_sound_config_resolves_to_program_bake(tmp_path, monkeypatch):
    # JTS5 regression after the first program-bake re-emit: the running graph can
    # be `sound_current.yml` with the generic sound source marker, but still be a
    # DAC-less Snapcast pipe sink. Content proves pipe-safety; do not route it to
    # the DAC-bound flat-graph guard.
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.profile import SoundProfile

    _persist_topology(
        _active_topology("stereo", "active_2_way"),
        tmp_path,
        monkeypatch,
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        ),
        encoding="utf-8",
    )

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active_leader_program_bake"


def test_sound_current_pipe_under_non_protected_topology_stays_sound_or_correction(
    tmp_path,
    monkeypatch,
):
    # Pins the PR #1011 topology-narrowing clause directly (the prior negative
    # test passes via the filename gate, so it can't catch a regression here).
    # This config DOES pass the filename gate (`sound_current.yml`) AND the pipe
    # check — identical to the positive program-bake case above — so the ONLY
    # thing keeping it out of the program-bake carrier is
    # `flat_program_graph_blocked_reason(topology) is not None`. Under a
    # full-range passive topology there is no protected tweeter, so that reason is
    # None: a plain stereo speaker that happens to be a SnapFIFO grouping leader
    # must stay on the ordinary sound/correction carrier, never get re-stamped as
    # an active program bake. Delete the topology clause and this resolves to
    # `active_leader_program_bake` instead — the mutation tripwire.
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.profile import SoundProfile

    _persist_topology(_full_range_stereo(), tmp_path, monkeypatch)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        ),
        encoding="utf-8",
    )

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "sound_or_correction"


def test_grouping_leader_pipe_config_does_not_resolve_to_program_bake(
    tmp_path,
    monkeypatch,
):
    # Passive multiroom leaders also write generic JTS stereo YAML to SnapFIFO.
    # The stale-marker recovery is only for `sound_current.yml`; grouping files
    # must not be reclassified or re-stamped as active program-bake configs.
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.profile import SoundProfile

    _persist_topology(
        _active_topology("stereo", "active_2_way"),
        tmp_path,
        monkeypatch,
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "grouping_leader.yml"
    path.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        ),
        encoding="utf-8",
    )

    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "sound_or_correction"


def test_program_bake_carrier_requires_pipe_sink(tmp_path):
    from jasper.sound.profile import SoundProfile

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "grouping_active_leader_bake.yml"
    path.write_text(_program_bake_yaml(), encoding="utf-8")

    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config"
    ) as emit, mock.patch(
        "jasper.multiroom.member_config.member_camilla_kwargs",
        return_value={
            "enable_rate_adjust": True,
            "channel_split": None,
            "playback_pipe_path": None,
        },
    ):
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        with pytest.raises(CarrierCannotHostEq) as exc:
            carrier.reemit(SoundProfile(enabled=False), room_peqs=[])

    assert exc.value.reason_code == "program_bake_pipe_unavailable"
    emit.assert_not_called()


def test_reemit_defaults_to_disk_read_member_kwargs(tmp_path):
    # With no override (the /sound paths), the carrier reads the member policy
    # from grouping state via member_camilla_kwargs() — unchanged from PR-1.
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml"
    ) as emit, mock.patch(
        "jasper.multiroom.member_config.member_camilla_kwargs",
        return_value={"enable_rate_adjust": True, "channel_split": None, "playback_pipe_path": None},
    ) as disk_read:
        carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
        carrier.reemit(mock.sentinel.profile, profile_id="id")
    disk_read.assert_called_once_with()
    assert emit.call_args.kwargs["playback_pipe_path"] is None


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


def test_sound_carrier_replaces_room_peqs_when_explicit(tmp_path):
    # Room correction apply/start must be able to say "use this exact room
    # layer" instead of preserving whatever was already loaded.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "correction_abc_123.yml"
    path.write_text("# jts sound/correction config\n")
    replacement = [object()]

    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml-text"
    ) as emit, mock.patch(
        "jasper.sound.graph_carrier.extract_room_peqs_from_config"
    ) as extract:
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        result = carrier.reemit(
            mock.sentinel.profile,
            profile_id="id",
            room_peqs=replacement,
        )

    extract.assert_not_called()
    assert emit.call_args.kwargs["room_peqs"] == replacement
    assert result.room_peq_count == 1


# --- PR-3: the SOLO active baseline hosts preference EQ -----------------

def test_solo_active_baseline_can_host_eq(tmp_path):
    # A header-bearing baseline on a solo speaker is now EQ-hostable: the
    # carrier reports can_host_eq so the durable /sound apply proceeds (it
    # recomposes under the dsp-apply lock) instead of refusing in the pre-check.
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ):
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
    assert carrier.kind == "active"
    assert carrier.can_host_eq is True


def test_solo_active_baseline_reemits_via_active_recompose(tmp_path):
    # reemit routes a solo baseline to the ACTIVE recompose helper (never the
    # stereo template), forwarding out_path so the durable apply writes the
    # EQ'd-baseline YAML where the apply transaction will load it.
    out = tmp_path / "sound_current.yml"
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ), mock.patch(
        "jasper.sound.graph_carrier._recompose_active_baseline_with_eq",
        return_value="eqd-active-yaml",
    ) as recompose:
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
        result = carrier.reemit(
            mock.sentinel.profile, out_path=out, profile_id="id", output_trim_db=3.0
        )
    assert isinstance(result, ReemitResult)
    assert result.yaml == "eqd-active-yaml"
    assert result.room_peq_count == 0
    assert recompose.call_args.kwargs["out_path"] == out
    assert recompose.call_args.kwargs["room_peqs"] == []
    # The household's manual headroom / loudness-match trim is forwarded to the
    # active emitter, not silently dropped.
    assert recompose.call_args.kwargs["output_trim_db"] == 3.0


def test_solo_active_baseline_replaces_room_peqs_explicitly(tmp_path):
    out = tmp_path / "sound_current.yml"
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    replacement = [object(), object()]
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ), mock.patch(
        "jasper.sound.graph_carrier._recompose_active_baseline_with_eq",
        return_value="eqd-active-yaml",
    ) as recompose, mock.patch(
        "jasper.sound.graph_carrier.extract_room_peqs_from_config"
    ) as extract:
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
        result = carrier.reemit(
            mock.sentinel.profile,
            out_path=out,
            profile_id="id",
            room_peqs=replacement,
        )

    extract.assert_not_called()
    assert result.room_peq_count == 2
    assert recompose.call_args.kwargs["room_peqs"] == replacement


def test_bonded_active_baseline_refuses_with_stable_reason(tmp_path):
    # inv 7: an active baseline that is a bonded member refuses (the active x
    # grouping decision is deferred to the Distributed-Active track). The
    # carrier is the backstop; /sound's follower-block usually fires first.
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=True
    ):
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
        assert carrier.can_host_eq is False
        with pytest.raises(CarrierCannotHostEq) as err:
            carrier.reemit(mock.sentinel.profile, profile_id="id")
    assert err.value.reason_code == "eq_on_active_bonded_member"


def test_active_baseline_refuses_bonded_leader_bake_via_member_kwargs(tmp_path):
    # inv 7 (bake context): the bonded-leader bake passes member_kwargs even when
    # grouping.env isn't active yet mid-bake. The active baseline refuses on that
    # signal — never recomposing an active graph into a bond.
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ), mock.patch(
        "jasper.sound.graph_carrier._recompose_active_baseline_with_eq"
    ) as recompose:
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
        with pytest.raises(CarrierCannotHostEq) as err:
            carrier.reemit(
                mock.sentinel.profile,
                member_kwargs={"playback_pipe_path": "/run/snapfifo"},
            )
    assert err.value.reason_code == "eq_on_active_bonded_member"
    recompose.assert_not_called()


def test_recompose_wrapper_refuses_when_evidence_unavailable(tmp_path):
    # When the saved evidence can no longer produce a baseline, the recompose
    # wrapper maps the underlying blocker to a typed refusal — it NEVER emits a
    # partial active graph. (Exercises the wrapper's None -> raise mapping; the
    # real evidence-derivation is covered in test_active_speaker_baseline_profile.)
    from jasper.sound.graph_carrier import _recompose_active_baseline_with_eq

    with mock.patch(
        "jasper.sound.profile.build_sound_filters", return_value=()
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        return_value={"status": "applied"},
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        return_value=(None, [{
            "severity": "blocker",
            "code": "baseline_crossover_preview_not_ready",
            "message": "save a fresh crossover preview",
        }]),
    ):
        with pytest.raises(CarrierCannotHostEq) as err:
            _recompose_active_baseline_with_eq(mock.sentinel.profile, out_path=None)
    assert err.value.reason_code == "active_baseline_recompose_unavailable"
    assert "save a fresh crossover preview" in err.value.message


@pytest.mark.parametrize(
    "profile_kind",
    ["accepted", "accepted_deferred", "missing", "bypassed", "stale"],
)
def test_active_recompose_threads_exact_desired_bass_evidence_and_publishes(
    tmp_path,
    profile_kind,
) -> None:
    from jasper.sound.graph_carrier import _recompose_active_baseline_with_eq

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    accepted = _sealed_profile(topology, applied)
    evaluated_profile = accepted
    evaluation_status = "accepted"
    emission_profile = accepted
    proof_profile = accepted
    if profile_kind == "accepted_deferred":
        evaluated_profile = replace(
            accepted,
            enclosure={
                "adapter_id": "ported_v1",
                "adapter_version": 1,
                "cabinet_fingerprint": "ported-cabinet",
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
        emission_profile = evaluated_profile
        proof_profile = evaluated_profile
    if profile_kind == "missing":
        evaluated_profile = None
        evaluation_status = "missing"
        emission_profile = None
        proof_profile = None
    elif profile_kind == "bypassed":
        evaluated_profile = replace(accepted, status="bypassed")
        evaluation_status = "bypassed"
        emission_profile = None
        proof_profile = evaluated_profile
    elif profile_kind == "stale":
        evaluated_profile = replace(accepted, baseline_fingerprint="0" * 64)
        evaluation_status = "stale"
        emission_profile = None
        proof_profile = evaluated_profile
    emitted = _active_baseline_yaml(
        "mono",
        2,
        bass_extension_profile=emission_profile,
    )
    target = tmp_path / "sound_current.yml"
    from jasper.active_speaker.runtime_contract import (
        classify_bass_extension_graph as desired_classifier,
    )

    with mock.patch(
        "jasper.output_topology.load_output_topology",
        return_value=topology,
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        return_value=applied,
    ), mock.patch(
        "jasper.bass_extension.profile.evaluate_bass_extension_profile",
        return_value=SimpleNamespace(
            status=evaluation_status,
            profile=evaluated_profile,
        ),
    ), mock.patch(
        "jasper.sound.profile.build_sound_filters",
        return_value=(),
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        return_value=(emitted, []),
    ) as recompose, mock.patch(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        wraps=desired_classifier,
    ) as prove:
        result = _recompose_active_baseline_with_eq(
            mock.sentinel.profile,
            out_path=target,
        )

    assert result == emitted
    assert target.read_text(encoding="utf-8") == emitted
    assert recompose.call_args.kwargs["bass_extension_profile"] is emission_profile
    assert recompose.call_args.kwargs["out_path"] is None
    assert prove.call_args.kwargs["desired_profile"] is proof_profile


def test_active_recompose_refuses_unsafe_graph_before_publishing(tmp_path) -> None:
    from jasper.sound.graph_carrier import _recompose_active_baseline_with_eq

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    profile = _sealed_profile(topology, applied)
    emitted = _active_baseline_yaml(
        "mono",
        2,
        bass_extension_profile=profile,
    )
    tampered = emitted.replace(
        "names: [as_woofer_woofer_tweeter_lp, bass_ext_lt",
        "names: [bass_ext_lt",
    )
    assert tampered != emitted
    target = tmp_path / "sound_current.yml"
    predecessor = b"predecessor graph\n"
    target.write_bytes(predecessor)

    with mock.patch(
        "jasper.output_topology.load_output_topology",
        return_value=topology,
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        return_value=applied,
    ), mock.patch(
        "jasper.bass_extension.profile.evaluate_bass_extension_profile",
        return_value=SimpleNamespace(status="accepted", profile=profile),
    ), mock.patch(
        "jasper.sound.profile.build_sound_filters",
        return_value=(),
    ), mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        return_value=(tampered, []),
    ) as recompose:
        with pytest.raises(CarrierCannotHostEq) as exc:
            _recompose_active_baseline_with_eq(
                mock.sentinel.profile,
                out_path=target,
            )

    assert exc.value.reason_code == "active_baseline_recompose_unavailable"
    assert target.read_bytes() == predecessor
    assert recompose.call_args.kwargs["out_path"] is None
    assert recompose.call_args.kwargs["bass_extension_profile"] is profile


def test_bass_extension_recompose_reproves_missing_woofer_lowpass(
    tmp_path,
) -> None:
    from jasper.sound.graph_carrier import (
        recompose_active_baseline_for_bass_extension,
    )

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    profile = _sealed_profile(topology, applied)
    emitted = _active_baseline_yaml(
        "mono",
        2,
        bass_extension_profile=profile,
    )
    tampered = emitted.replace(
        "names: [as_woofer_woofer_tweeter_lp, bass_ext_lt",
        "names: [bass_ext_lt",
    )
    assert tampered != emitted
    assert "bass_ext_lt" in tampered
    assert "bass_ext_subsonic" in tampered
    selected = tmp_path / "selected.yml"
    selected.write_text(emitted, encoding="utf-8")
    preference_path = tmp_path / "sound-profile.json"
    preference_path.write_text(
        json.dumps(SoundProfile(enabled=False).to_dict()),
        encoding="utf-8",
    )
    settings_path = tmp_path / "sound-settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")

    with mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        return_value=(tampered, []),
    ):
        with pytest.raises(CarrierCannotHostEq) as exc:
            recompose_active_baseline_for_bass_extension(
                topology,
                applied_profile=applied,
                desired_profile=profile,
                current_config_path=selected,
                preference_profile_path=preference_path,
                sound_settings_path=settings_path,
            )

    assert exc.value.reason_code == "bass_extension_recompose_unavailable"


@pytest.mark.parametrize("profile_kind", ["missing", "bypassed"])
def test_bass_extension_recompose_proves_no_block_predecessors(
    tmp_path,
    profile_kind,
) -> None:
    from jasper.sound.graph_carrier import (
        recompose_active_baseline_for_bass_extension,
    )

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    desired_profile = None
    if profile_kind == "bypassed":
        desired_profile = replace(
            _sealed_profile(topology, applied),
            status="bypassed",
        )
    emitted = _active_baseline_yaml("mono", 2)
    selected = tmp_path / "selected.yml"
    selected.write_text(emitted, encoding="utf-8")
    preference_path = tmp_path / "sound-profile.json"
    preference_path.write_text(
        json.dumps(SoundProfile(enabled=False).to_dict()),
        encoding="utf-8",
    )
    settings_path = tmp_path / "sound-settings.json"
    settings_path.write_text("{}\n", encoding="utf-8")

    with mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        return_value=(emitted, []),
    ):
        result = recompose_active_baseline_for_bass_extension(
            topology,
            applied_profile=applied,
            desired_profile=desired_profile,
            current_config_path=selected,
            preference_profile_path=preference_path,
            sound_settings_path=settings_path,
        )

    assert result == emitted


def test_bass_extension_recompose_preserves_exact_program_overlays(
    tmp_path,
) -> None:
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.sound.graph_carrier import (
        recompose_active_baseline_for_bass_extension,
    )

    topology, applied = _real_active_applied_baseline(tmp_path)
    preference = SoundProfile(
        simple_eq=SimpleEq(bass_db=4.0),
        updated_at="2026-07-19T12:00:00Z",
    )
    settings = SoundSettings(headroom_trim_db=6.0)
    room_peqs = [PeqFilter(freq=83.0, q=4.2, gain=-3.0)]
    current, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        room_peqs=room_peqs,
        preference_filters=build_sound_filters(preference),
        output_trim_db=output_trim_db(preference, settings),
    )
    assert issues == []
    assert current is not None
    assert "room_peq_1" in current
    assert "active_baseline_headroom" in current
    selected = tmp_path / "configs" / "active-speaker-baseline.yml"
    selected.parent.mkdir(exist_ok=True)
    selected.write_text(current, encoding="utf-8")
    preference_path, settings_path = _write_program_overlay_sources(
        tmp_path,
        profile=preference,
        settings=settings,
    )

    recomposed = recompose_active_baseline_for_bass_extension(
        topology,
        applied_profile=applied,
        desired_profile=None,
        current_config_path=selected,
        preference_profile_path=preference_path,
        sound_settings_path=settings_path,
    )

    assert recomposed == current


@pytest.mark.parametrize("changed_source", ["preference", "settings"])
def test_bass_extension_recompose_refuses_program_overlay_reset(
    tmp_path,
    changed_source,
) -> None:
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.sound.graph_carrier import (
        recompose_active_baseline_for_bass_extension,
    )

    topology, applied = _real_active_applied_baseline(tmp_path)
    selected_preference = SoundProfile(
        simple_eq=SimpleEq(bass_db=4.0),
        updated_at="2026-07-19T12:00:00Z",
    )
    selected_settings = SoundSettings(headroom_trim_db=6.0)
    current, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        preference_filters=build_sound_filters(selected_preference),
        output_trim_db=output_trim_db(selected_preference, selected_settings),
    )
    assert issues == []
    assert current is not None
    selected = tmp_path / "configs" / "active-speaker-baseline.yml"
    selected.parent.mkdir(exist_ok=True)
    selected.write_text(current, encoding="utf-8")
    persisted_preference = (
        SoundProfile(enabled=False, updated_at="2026-07-19T12:00:00Z")
        if changed_source == "preference"
        else selected_preference
    )
    persisted_settings = (
        SoundSettings()
        if changed_source == "settings"
        else selected_settings
    )
    preference_path, settings_path = _write_program_overlay_sources(
        tmp_path,
        profile=persisted_preference,
        settings=persisted_settings,
    )

    with pytest.raises(CarrierCannotHostEq) as exc:
        recompose_active_baseline_for_bass_extension(
            topology,
            applied_profile=applied,
            desired_profile=None,
            current_config_path=selected,
            preference_profile_path=preference_path,
            sound_settings_path=settings_path,
        )

    assert exc.value.reason_code == "bass_extension_recompose_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broken_source", "broken_value"),
    [
        ("preference", "missing"),
        ("preference", "malformed"),
        ("settings", "missing"),
        ("settings", "malformed"),
        ("selected_graph", "driver_semantics"),
    ],
)
async def test_bass_apply_refuses_unreproducible_predecessor_before_mutation(
    tmp_path,
    broken_source,
    broken_value,
) -> None:
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.bass_extension import apply_bass_extension
    from jasper.bass_extension.profile import save_bass_extension_profile
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    topology, applied = _real_active_applied_baseline(tmp_path)
    preference = SoundProfile(
        simple_eq=SimpleEq(bass_db=4.0),
        updated_at="2026-07-19T12:00:00Z",
    )
    settings = SoundSettings(headroom_trim_db=6.0)
    current, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        preference_filters=build_sound_filters(preference),
        output_trim_db=output_trim_db(preference, settings),
    )
    assert issues == []
    assert current is not None
    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    selected = configs / "active-speaker-baseline.yml"
    selected.write_text(current, encoding="utf-8")
    selected.chmod(0o664)
    preference_path, settings_path = _write_program_overlay_sources(
        tmp_path,
        profile=preference,
        settings=settings,
    )
    if broken_source == "selected_graph":
        changed_graph = yaml.safe_load(selected.read_text(encoding="utf-8"))
        highpass_name = next(
            name
            for name in changed_graph["filters"]
            if name.startswith("as_tweeter_") and name.endswith("_hp")
        )
        parameters = changed_graph["filters"][highpass_name]["parameters"]
        parameters["freq"] = float(parameters["freq"]) + 1.0
        selected.write_text(
            yaml.safe_dump(changed_graph, sort_keys=False),
            encoding="utf-8",
        )
    else:
        broken_path = (
            preference_path if broken_source == "preference" else settings_path
        )
        if broken_value == "missing":
            broken_path.unlink()
        else:
            broken_path.write_text("{ malformed", encoding="utf-8")
    applied_path = tmp_path / "applied.json"
    applied_path.write_text(json.dumps(applied), encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {selected}\n", encoding="utf-8")
    staged_path = tmp_path / "staged.json"
    staged_path.write_text("{}\n", encoding="utf-8")
    profile_path = tmp_path / "bass-profile.json"
    desired = _sealed_profile(topology, applied)
    predecessor = replace(desired, status="bypassed")
    save_bass_extension_profile(predecessor, profile_path)
    intent_path = tmp_path / "bass-intent.json"

    class NoReloadController:
        def __init__(self) -> None:
            self.reload_count = 0

        async def get_config_file_path(self, *, best_effort=False):
            return str(selected)

        async def get_active_config_raw(self, *, best_effort=False):
            return selected.read_text(encoding="utf-8")

        async def reload(self, *, best_effort=False):
            self.reload_count += 1
            return True

    controller = NoReloadController()
    graph_before = selected.read_bytes()
    graph_mode_before = selected.stat().st_mode
    profile_before = profile_path.read_bytes()
    selector_before = statefile.read_bytes()

    with pytest.raises(CarrierCannotHostEq) as exc:
        await apply_bass_extension(
            desired,
            topology=topology,
            controller=controller,
            statefile_path=statefile,
            applied_baseline_path=applied_path,
            profile_path=profile_path,
            intent_path=intent_path,
            staged_metadata_path=staged_path,
            config_dir=configs,
            preference_profile_path=preference_path,
            sound_settings_path=settings_path,
            validate=lambda _path: SimpleNamespace(ok_to_apply=True),
        )

    assert exc.value.reason_code == "bass_extension_recompose_unavailable"
    assert selected.read_bytes() == graph_before
    assert selected.stat().st_mode == graph_mode_before
    assert profile_path.read_bytes() == profile_before
    assert statefile.read_bytes() == selector_before
    assert controller.reload_count == 0
    assert not intent_path.exists()


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


# --- shm_ring coupling threaded through reemit() ---
#
# The coupling is source/topology-agnostic and always-on while
# JASPER_FANIN_CAMILLA_COUPLING=shm_ring, so every stereo-host /
# active-baseline carrier applies it. Default (None / {}) is byte-identical.
# (imports for these tests live in the top-of-file import block.)

_SHM_RING_KWARGS = capture_kwargs_for_coupling("shm_ring")


def test_base_flat_loopback_coupling_is_byte_identical(tmp_path):
    # The default-OFF proof at the CHOKEPOINT: a reemit with no coupling kwargs
    # (or an empty dict) is byte-for-byte the same emitted YAML as one that never
    # mentioned the coupling. Uses the real emit_sound_config (not mocked) so the
    # comparison is on the actual emitted config string.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    baseline = carrier.reemit(
        SoundProfile(enabled=False), profile_id="x", member_kwargs={}
    ).yaml
    none_coupled = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        member_kwargs={},
        fanin_coupling_capture_kwargs=None,
    ).yaml
    empty_coupled = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        member_kwargs={},
        fanin_coupling_capture_kwargs={},
    ).yaml
    assert none_coupled == baseline
    assert empty_coupled == baseline
    # And it is the ALSA dsnoop capture, untouched.
    assert 'device: "plug:jasper_capture"' in baseline
    assert "type: File" not in baseline


def test_base_flat_shm_ring_coupling_emits_ring_devices(tmp_path):
    # shm_ring at the chokepoint: the base-flat (stereo host) reemit flips capture
    # to the Ring A ioplug device and playback to the Ring B ioplug device.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        member_kwargs={},
        fanin_coupling_capture_kwargs=_SHM_RING_KWARGS,
    ).yaml
    assert f'device: "{RING_CAPTURE_DEVICE}"' in cfg
    assert f'device: "{RING_PLAYBACK_DEVICE}"' in cfg
    assert 'device: "plug:jasper_capture"' not in cfg
    assert "type: AsyncSinc" not in cfg
    assert "enable_rate_adjust: false" in cfg


def test_shm_ring_coupling_is_noop_for_grouped_pipe_sink(tmp_path):
    # PRECEDENCE: a grouped/bonded member writes a SnapFIFO playback pipe with
    # enable_rate_adjust=False. That playback pipe already owns the pipe sink, so
    # the local shm_ring coupling is a no-op there.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        member_kwargs={"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False},
        fanin_coupling_capture_kwargs=_SHM_RING_KWARGS,
    ).yaml
    # ALSA capture preserved (no ring coupling), pipe SINK still on playback.
    assert 'device: "plug:jasper_capture"' in cfg
    assert f'device: "{RING_CAPTURE_DEVICE}"' not in cfg
    assert f'device: "{RING_PLAYBACK_DEVICE}"' not in cfg
    assert "/run/snapfifo" in cfg


def test_program_bake_carrier_ignores_shm_ring_coupling(tmp_path):
    # The program bake is a bonded pipe sink (rate_adjust=False); the coupling
    # keyword is accepted for call-site uniformity but never applied. The emit
    # keeps its ALSA capture; no ring device appears.
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_program_bake_yaml())
    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active_leader_program_bake"
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        out_path=config_dir / "out.yml",
        member_kwargs={"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False},
        fanin_coupling_capture_kwargs=_SHM_RING_KWARGS,
    ).yaml
    assert 'device: "plug:jasper_capture"' in cfg
    assert f'device: "{RING_CAPTURE_DEVICE}"' not in cfg


def test_active_baseline_ignores_stereo_only_shm_ring_coupling(tmp_path):
    # shm_ring is solo-stereo-only; the active baseline keeps its roleful ALSA
    # capture/playback graph regardless of the coupling. The carrier accepts
    # the keyword for call-site uniformity (every carrier's reemit() takes
    # it) but never threads it into active recomposition.
    path = tmp_path / "active_speaker_baseline.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    with mock.patch(
        "jasper.sound.graph_carrier._bonded_active_member", return_value=False
    ), mock.patch(
        "jasper.sound.graph_carrier._recompose_active_baseline_with_eq",
        return_value="active-yaml",
    ) as recompose:
        carrier = carrier_for_loaded_config(str(path), config_dir=tmp_path)
        result = carrier.reemit(
            SoundProfile(enabled=False),
            fanin_coupling_capture_kwargs=_SHM_RING_KWARGS,
        )

    assert result.yaml == "active-yaml"
    assert "coupling_capture_kwargs" not in recompose.call_args.kwargs
