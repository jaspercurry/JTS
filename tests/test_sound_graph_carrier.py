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
from unittest import mock

import pytest

from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_FLAT_FULL_RANGE,
    classify_camilla_graph,
)
from jasper.fanin_coupling import capture_kwargs_for_coupling
from jasper.sound.camilla_yaml import BASE_CONFIG_PATH, emit_sound_config
from jasper.sound.graph_carrier import (
    CarrierCannotHostEq,
    ReemitResult,
    carrier_for_loaded_config,
)
from jasper.sound.profile import SoundProfile
from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
    _flat_yaml,
    _full_range_stereo,
)

_STEREO_HOST_KINDS = {"base_flat", "sound_or_correction"}


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
        "jasper.active_speaker.baseline_profile.recompose_baseline_yaml",
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


# --- Stage-4b lean lane: capture_kwargs carrier fidelity ----------------

_LEAN_CAPTURE_KWARGS = {
    "capture_pipe_path": "/run/jasper-usbsink/lean.pipe",
    "resampler_type": "AsyncSinc",
    "resampler_profile": "Balanced",
    "enable_rate_adjust": True,
}


def test_lean_capture_kwargs_forward_to_emit_on_stereo_host(tmp_path):
    # The fidelity fix: a stereo-host carrier re-emits the lean File-capture
    # config WITH the preserved room PEQs + trim AND the lean capture kwargs —
    # one carrier-preserved re-emit, not a raw emit that drops room correction.
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml"
    ) as emit:
        carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
        carrier.reemit(
            mock.sentinel.profile,
            profile_id="id",
            output_trim_db=2.5,
            capture_kwargs=_LEAN_CAPTURE_KWARGS,
        )
    kw = emit.call_args.kwargs
    assert kw["capture_pipe_path"] == "/run/jasper-usbsink/lean.pipe"
    assert kw["resampler_type"] == "AsyncSinc"
    assert kw["enable_rate_adjust"] is True
    # Carrier fidelity: the trim + (here empty base) room PEQs still ride along.
    assert kw["output_trim_db"] == 2.5
    assert kw["room_peqs"] == []


def test_lean_capture_kwargs_preserve_room_peqs_on_correction_host(tmp_path):
    # A sound/correction host preserves its room PEQs THROUGH the lean re-emit —
    # the exact regression the 4b-iii staged (preference-only) path warned about.
    from jasper.camilla_config_contract import PeqFilter

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_flat_yaml())

    peqs = [PeqFilter(freq=120.0, q=1.0, gain=-3.0)]
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml"
    ) as emit, mock.patch(
        "jasper.sound.graph_carrier.extract_room_peqs_from_config",
        return_value=peqs,
    ):
        carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
        result = carrier.reemit(
            mock.sentinel.profile,
            profile_id="id",
            capture_kwargs=_LEAN_CAPTURE_KWARGS,
        )
    assert result.room_peq_count == 1
    assert emit.call_args.kwargs["room_peqs"] == peqs
    assert emit.call_args.kwargs["capture_pipe_path"] == "/run/jasper-usbsink/lean.pipe"


def test_lean_default_none_capture_kwargs_is_byte_identical(tmp_path):
    # Every existing caller passes no capture_kwargs -> emit_sound_config sees no
    # lean keys at all (byte-identical contract for the buffered re-emit).
    with mock.patch(
        "jasper.sound.graph_carrier.emit_sound_config", return_value="yaml"
    ) as emit:
        carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
        carrier.reemit(mock.sentinel.profile, profile_id="id")
    kw = emit.call_args.kwargs
    assert "capture_pipe_path" not in kw
    assert "resampler_type" not in kw


def test_lean_refused_on_unknown_graph(tmp_path):
    carrier = carrier_for_loaded_config(str(tmp_path / "gone.yml"), config_dir=tmp_path)
    with pytest.raises(CarrierCannotHostEq) as exc:
        carrier.reemit(mock.sentinel.profile, capture_kwargs=_LEAN_CAPTURE_KWARGS)
    assert exc.value.reason_code == "unknown_config"


def test_lean_refused_on_active_graph(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_active_baseline_yaml("mono", 2))
    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active"
    with pytest.raises(CarrierCannotHostEq) as exc:
        carrier.reemit(mock.sentinel.profile, capture_kwargs=_LEAN_CAPTURE_KWARGS)
    assert exc.value.reason_code == "lean_on_active"


def test_lean_refused_on_program_bake_graph(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    path = config_dir / "sound_current.yml"
    path.write_text(_program_bake_yaml())
    carrier = carrier_for_loaded_config(str(path), config_dir=config_dir)
    assert carrier.kind == "active_leader_program_bake"
    with pytest.raises(CarrierCannotHostEq) as exc:
        carrier.reemit(
            mock.sentinel.profile,
            member_kwargs={"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False},
            capture_kwargs=_LEAN_CAPTURE_KWARGS,
        )
    assert exc.value.reason_code == "lean_on_program_bake"


# --- FIFO coupling threaded through reemit() (the SHARED fan-in→Camilla hop) ---
#
# Distinct from the lean lane: the coupling is source/topology-agnostic and
# always-on while JASPER_FANIN_CAMILLA_COUPLING=fifo, so EVERY stereo-host /
# active-baseline carrier applies it. Default (None / {}) is byte-identical.
# (imports for these tests live in the top-of-file import block.)

_FIFO_COUPLING_KWARGS = capture_kwargs_for_coupling("fifo")
_FANIN_PIPE = _FIFO_COUPLING_KWARGS["capture_pipe_path"]


def test_base_flat_loopback_coupling_is_byte_identical(tmp_path):
    # The default-OFF proof at the CHOKEPOINT: a reemit with no coupling kwargs
    # (or an empty dict) is byte-for-byte the same emitted YAML as one that never
    # mentioned the coupling. Uses the real emit_sound_config (not mocked) so the
    # comparison is on the actual emitted config string.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    baseline = carrier.reemit(SoundProfile(enabled=False), profile_id="x").yaml
    none_coupled = carrier.reemit(
        SoundProfile(enabled=False), profile_id="x", fanin_coupling_capture_kwargs=None
    ).yaml
    empty_coupled = carrier.reemit(
        SoundProfile(enabled=False), profile_id="x", fanin_coupling_capture_kwargs={}
    ).yaml
    assert none_coupled == baseline
    assert empty_coupled == baseline
    # And it is the ALSA dsnoop capture, untouched.
    assert 'device: "plug:jasper_capture"' in baseline
    assert "type: File" not in baseline


def test_base_flat_fifo_coupling_emits_file_capture(tmp_path):
    # =fifo at the chokepoint: the base-flat (stereo host) reemit flips its
    # capture block to a File pipe + async resampler.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        fanin_coupling_capture_kwargs=_FIFO_COUPLING_KWARGS,
    ).yaml
    # RawFile, not File — CamillaDSP v4 has no `File` capture variant.
    assert "type: RawFile" in cfg
    assert "type: File" not in cfg
    assert _FANIN_PIPE in cfg
    assert 'device: "plug:jasper_capture"' not in cfg
    assert "type: AsyncSinc" in cfg
    assert "enable_rate_adjust: true" in cfg


def test_fifo_coupling_lean_capture_wins(tmp_path):
    # PRECEDENCE: when a live lean capture_kwargs is set (the more-exclusive solo
    # File capture), the coupling is a no-op for that emit — the lean usbsink pipe
    # stays the capture source, NOT the fan-in pipe.
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        capture_kwargs=_LEAN_CAPTURE_KWARGS,
        fanin_coupling_capture_kwargs=_FIFO_COUPLING_KWARGS,
    ).yaml
    # The lean usbsink pipe wins; the fan-in coupling pipe must NOT appear.
    assert _LEAN_CAPTURE_KWARGS["capture_pipe_path"] in cfg
    assert _FANIN_PIPE not in cfg


def test_fifo_coupling_is_noop_for_grouped_pipe_sink(tmp_path):
    # PRECEDENCE: a grouped/bonded member writes a SnapFIFO playback pipe with
    # enable_rate_adjust=False — mutually exclusive with the File capture's
    # required rate_adjust=True. So the coupling is a no-op there; the capture
    # stays the ALSA fan-in tap (the grouped capture topology is deferred).
    carrier = carrier_for_loaded_config(str(BASE_CONFIG_PATH), config_dir=tmp_path)
    cfg = carrier.reemit(
        SoundProfile(enabled=False),
        profile_id="x",
        member_kwargs={"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False},
        fanin_coupling_capture_kwargs=_FIFO_COUPLING_KWARGS,
    ).yaml
    # ALSA capture preserved (no File coupling), pipe SINK still on playback.
    assert 'device: "plug:jasper_capture"' in cfg
    assert _FANIN_PIPE not in cfg
    assert "/run/snapfifo" in cfg


def test_program_bake_carrier_ignores_fifo_coupling(tmp_path):
    # The program bake is a bonded pipe sink (rate_adjust=False); the coupling
    # keyword is accepted for call-site uniformity but never applied. The emit
    # keeps its ALSA capture; no fan-in pipe appears.
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
        fanin_coupling_capture_kwargs=_FIFO_COUPLING_KWARGS,
    ).yaml
    assert 'device: "plug:jasper_capture"' in cfg
    assert _FANIN_PIPE not in cfg
