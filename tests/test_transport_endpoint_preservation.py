# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A re-emit preserves the endpoint the box is LIVE on (#2339, #2337, #2344).

Four seams rebuild a roleful box's active graph from its immutable applied
snapshot: the deploy/arm-ladder reconcile (``jasper-sound
reconcile-current-dsp``), a ``/sound/`` or ``/eq/`` save, a bass-extension
apply, and the drift check that binds Layer A to the applied profile. The
snapshot keeps naming whichever playback lane was resolved at Apply time, so a
seam that lets that reach the emitter moves the speaker's transport without
anyone asking: on jts3 that was silence with every daemon healthy
(#2339, ``captures/r7b-jts3-arm3-20260811T162742Z`` files 14-16,
``writer_alive=False``, Ring A ``drop_no_reader`` climbing), and ``install.sh``
runs that same reconcile on every deploy.

The seams that EMIT a graph ask one derivation,
:func:`jasper.active_speaker.playback_route.resolve_live_active_endpoint`. The
drift check emits nothing and instead NEUTRALIZES the transport axis against the
graph it is comparing: a third opinion in a two-way comparison turns ordinary
device-resolution drift into a crossover-drift claim. An unarmed box is
byte-identical to before under all four.

These walk the real functions over real files (a real statefile, a real applied
profile, a real topology) rather than mocking the seam under test: the defect
was a missing argument at a call site, and a mock of that call site would have
passed straight through it.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml as yaml_parser

from jasper.active_speaker.state_paths import (
    BASELINE_PROFILE_STATE_ENV as STATE_PATH_ENV,
)
from jasper.active_speaker.baseline_profile import (
    recompose_applied_baseline_yaml,
)
from jasper.active_speaker.playback_route import (
    LOADED_GRAPH_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    resolve_live_active_endpoint,
)
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.active_speaker.runtime_contract import OUTPUTD_ACTIVE_PLAYBACK_DEVICE
from jasper.camilla_config_contract import (
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    RETIRED_ALOOP_CAPTURE_DEVICE,
    parse_camilla_devices_config,
)
from jasper.active_speaker import ActiveSpeakerPreset, audible_outputs_for_role
from jasper.active_speaker.camilla_yaml import COMMISSIONING_HEADROOM_DB
from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    TRANSPORT_RING,
)
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile

ROUTE_LOGGER = "jasper.active_speaker.playback_route"
STAGING_LOGGER = "jasper.active_speaker.staging"


class _FakeCamilla:
    """Reports one loaded config path and records what it was asked to load.

    Deliberately independent of the statefile the derivation reads: the jts3
    clobber happened while CamillaDSP was still on the PRE-arm graph and the
    statefile already pointed at the ring one, and a stub that conflated the two
    could not express that state.
    """

    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        self.loaded_path = path
        return True


# --------------------------------------------------------------------------
# Boxes, graphs, payload readers.
# --------------------------------------------------------------------------


@pytest.fixture
def applied_box(tmp_path, monkeypatch):
    """A commissioned roleful box: ``(topology, applied)``, published where
    production reads them so the seams load them the way they do on a Pi."""
    from tests.test_sound_graph_carrier import _real_active_applied_baseline

    topology, applied = _real_active_applied_baseline(tmp_path)
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(topology.to_dict()), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    state_path = tmp_path / "active_speaker_baseline_profile.json"
    state_path.write_text(json.dumps(applied), encoding="utf-8")
    monkeypatch.setenv(STATE_PATH_ENV, str(state_path))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return topology, applied


@pytest.fixture
def commissioning_box():
    """The roleful bench shape the commissioning emitter is driven with."""
    from tests.test_ring_active_endpoint import _active_topology, _mono_two_way_preset

    return _active_topology("mono", "active_2_way"), _mono_two_way_preset()


def _point_statefile_at(tmp_path: Path, monkeypatch, graph_text: str, *, name: str):
    """Publish ``graph_text`` and point the durable statefile at it."""
    graph = tmp_path / name
    graph.write_text(graph_text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return graph, statefile


def _graph_for(topology, applied, device: str | None) -> str:
    yaml, issues = recompose_applied_baseline_yaml(
        topology, applied_profile=applied, playback_device=device
    )
    assert issues == [], issues
    assert yaml is not None
    return yaml


def _both_halves(yaml_text: str) -> tuple[str | None, str | None]:
    devices = parse_camilla_devices_config(yaml_text)
    return devices.get("capture_device"), devices.get("playback_device")


def _preference_filters(profile_path: Path):
    from jasper.sound.profile import build_sound_filter_slots, load_profile

    return build_sound_filter_slots(load_profile(profile_path))


def _codes(payload: dict) -> set[str]:
    return {issue.get("code") for issue in payload.get("issues") or []}


def _issue(payload: dict, code: str) -> dict:
    return next(i for i in payload["issues"] if i.get("code") == code)


def _gate(payload: dict, gate_id: str) -> dict:
    return next(g for g in payload["required_gates"] if g.get("id") == gate_id)


def _event_fields(records, event: str) -> dict[str, str]:
    """The ONE record carrying ``event=<event>``, as its ``k=v`` field map.

    One record, not ``caplog.text``: fields spread over several lines satisfy a
    substring check while making the one-grep property false.
    """
    lines = [r.message for r in records if f"event={event}" in r.message]
    assert len(lines) == 1, lines
    return {
        key: value.strip('"')
        for key, _, value in (
            part.partition("=") for part in lines[0].split() if "=" in part
        )
    }


def _jasper_calls(name: str):
    """Every call to ``name`` under ``jasper/``, DISCOVERED by parsing, as
    ``(relative_path, module_tree, call_node)``.

    Discovery rather than a hand-written list is the point of both guards below:
    a new call site fails them instead of waiting to be noticed in review.
    """
    repo = Path(__file__).resolve().parent.parent
    for path in sorted((repo / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if called == name:
                yield path.relative_to(repo).as_posix(), tree, node


# --------------------------------------------------------------------------
# 1. THE DERIVATION. Which witness answers, and in which order.
#
# The statefile-pointed graph is upstream truth: the marker is derived FROM it
# by `jasper-audio-hardware-reconcile`, so mid-arm (graph moved, marker still
# clear) the graph answers and a deploy landing there stops undoing rung 1. A
# fresh box has no statefile and still has to take a deploy, so an unadoptable
# graph falls through to the CHOOSER rather than refusing. Every case asserts
# the SOURCE, since both witnesses answer the same device name, and sweeps both
# marker states: ADR-0100 left one legal endpoint and the chooser stopped
# reading the marker, so one that still branched on it would answer the retired
# lane in one sweep — the shape every pre-retirement box is in.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        "adoptable_ring_graph",
        "no_statefile",
        "dangling_config_path",
        "graph_without_devices",
        "graph_on_a_non_endpoint_device",
        "graph_on_the_retired_aloop_endpoint",
    ],
)
async def test_which_witness_answers_for_the_live_endpoint(
    applied_box, tmp_path, monkeypatch, shape,
):
    """An adoptable graph answers; every unadoptable shape falls through to the
    CHOOSER, never to the applied snapshot whose lane re-created #2339."""
    topology, applied = applied_box
    source = (
        LOADED_GRAPH_SOURCE
        if shape == "adoptable_ring_graph"
        else OUTPUTD_ACTIVE_LANE_SOURCE
    )
    statefile = tmp_path / "outputd-statefile.yml"
    if shape == "adoptable_ring_graph":
        _point_statefile_at(
            tmp_path,
            monkeypatch,
            _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
            name="loaded.yml",
        )
    elif shape == "dangling_config_path":
        statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n", encoding="utf-8")
    elif shape == "graph_without_devices":
        graph = tmp_path / "no-devices.yml"
        graph.write_text("pipeline: []\n", encoding="utf-8")
        statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    elif shape != "no_statefile":
        declined = (
            OUTPUTD_ACTIVE_PLAYBACK_DEVICE
            if shape == "graph_on_the_retired_aloop_endpoint"
            else DEFAULT_PLAYBACK_DEVICE
        )
        graph = tmp_path / f"{shape}.yml"
        graph.write_text(
            _graph_for(topology, applied, None).replace(
                RING_ACTIVE_PLAYBACK_DEVICE, declined
            ),
            encoding="utf-8",
        )
        assert declined in graph.read_text(encoding="utf-8")
        statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    for marker_armed in (True, False):
        monkeypatch.setattr(
            "jasper.fanin_coupling.ring_active_endpoint_armed",
            lambda env=None, armed=marker_armed: armed,
        )
        assert resolve_live_active_endpoint(topology) == (
            RING_ACTIVE_PLAYBACK_DEVICE,
            source,
        ), marker_armed


async def test_a_declined_non_endpoint_device_is_visible_in_the_journal(
    applied_box, tmp_path, monkeypatch, caplog,
):
    """Declining an observed sink is a decision, so it is logged; adopting a
    legal one is not narrated. Without the line the journal cannot tell "looked
    and declined" from "never looked". DEBUG, because a lab box takes this
    branch on every call, legitimately."""
    topology, applied = applied_box
    graph, _statefile = _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, None).replace(
            RING_ACTIVE_PLAYBACK_DEVICE, DEFAULT_PLAYBACK_DEVICE
        ),
        name="stereo-lane.yml",
    )

    with caplog.at_level(logging.DEBUG, logger=ROUTE_LOGGER):
        answer = resolve_live_active_endpoint(topology)

    assert answer == (RING_ACTIVE_PLAYBACK_DEVICE, OUTPUTD_ACTIVE_LANE_SOURCE)
    fields = _event_fields(caplog.records, "active_speaker.live_endpoint")
    assert fields["result"] == "declined_non_endpoint_device"
    assert fields["observed"] == DEFAULT_PLAYBACK_DEVICE
    assert fields["config"] == str(graph)

    caplog.clear()
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
        name="ring.yml",
    )
    with caplog.at_level(logging.DEBUG, logger=ROUTE_LOGGER):
        resolve_live_active_endpoint(topology)
    assert not [r for r in caplog.records if "live_endpoint" in r.message]


# --------------------------------------------------------------------------
# 2. #2339 / #2337 — the deploy reconcile and the /sound/ + /eq/ save.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("running", ["retired_aloop_lane", "already_current"])
async def test_reconcile_current_dsp_re_emits_through_the_live_endpoint(
    applied_box, tmp_path, monkeypatch, running,
):
    """THE #2339 REPRODUCTION: the statefile is on the ring graph rung 1
    published while the RUNNING CamillaDSP still has the pre-arm one.

    Nothing in rungs 1-2 reloads Camilla, so that lag is the normal mid-ladder
    state jts3 was clobbered in. ``already_current`` is the other direction:
    the deploy still refreshes the artifact and must change nothing else.
    """
    from jasper.sound.runtime import reconcile_current_dsp

    topology, applied = applied_box
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    _point_statefile_at(tmp_path, monkeypatch, ring_graph, name="candidate.yml")

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    pre_arm = ring_graph.replace(
        RING_ACTIVE_PLAYBACK_DEVICE, OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    ).replace(RING_CAPTURE_DEVICE, RETIRED_ALOOP_CAPTURE_DEVICE)
    assert pre_arm != ring_graph
    current = config_dir / "sound_current.yml"
    current.write_text(
        pre_arm if running == "retired_aloop_lane" else ring_graph, encoding="utf-8"
    )
    camilla = _FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=3.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert (payload["status"], payload["carrier_kind"]) == ("reconciled", "active")
    emitted = Path(str(camilla.loaded_path)).read_text(encoding="utf-8")
    assert _both_halves(emitted) == (RING_CAPTURE_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)
    expected, _ = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        preference_filters=_preference_filters(profile_path),
        playback_device=None,
    )
    assert emitted == expected


async def test_a_sound_save_preserves_the_boxs_endpoint(
    applied_box, tmp_path, monkeypatch,
):
    """A household EQ save changes the EQ, never the transport (#2337).

    ``/eq/`` and ``/sound/setup/`` both land on ``load_profile_config``; pre-fix
    a taste-EQ save disarmed the box and the reconcilers converged it to
    loopback."""
    from jasper.sound.runtime import load_profile_config

    topology, applied = applied_box
    loaded_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    _point_statefile_at(tmp_path, monkeypatch, loaded_graph, name="loaded.yml")
    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    current = config_dir / "sound_current.yml"
    current.write_text(loaded_graph, encoding="utf-8")

    apply_state, out_path, _ = await load_profile_config(
        SoundProfile(simple_eq=SimpleEq(treble_db=4.5)),
        profile_path=tmp_path / "sound_profile.json",
        config_dir=config_dir,
        camilla_factory=lambda: _FakeCamilla(str(current)),
        source="sound_apply",
        persist_profile=True,
    )

    assert apply_state.result == "success", apply_state.to_dict()
    emitted = Path(out_path).read_text(encoding="utf-8")
    assert "sound_simple_treble" in emitted
    assert _both_halves(emitted) == (RING_CAPTURE_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)


# --------------------------------------------------------------------------
# 3. The drift check that binds Layer A to the applied snapshot.
# --------------------------------------------------------------------------


def _layer_a_binding(topology, applied, text: str) -> dict:
    from jasper.active_speaker.setup_status import _applied_layer_a_binding

    # No statefile is staged on purpose: this check reads the graph it was
    # HANDED, and nothing else.
    return _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=text,
    )


@pytest.mark.parametrize(
    "mutation, status",
    [
        ("none", "current"),
        ("camilla_readback", "current"),
        ("crossover_drift", "mismatch"),
        ("forbidden_playback_lane", "unverifiable"),
    ],
)
async def test_the_layer_a_binding_judges_crossover_never_the_transport(
    applied_box, mutation, status,
):
    """Arming a box is not crossover drift and must not block room correction.

    ``camilla_readback`` is what ``/correction/`` actually hands this check
    (``get_active_config_raw()``: comments dropped, scalars re-rendered);
    ``crossover_drift`` moves a REFERENCED post-split filter, inside the
    projection the fingerprint binds, so the check is not merely inert.
    """
    topology, applied = applied_box
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    text = ring_graph
    if mutation == "camilla_readback":
        text = yaml_parser.safe_dump(yaml_parser.safe_load(ring_graph), sort_keys=False)
        assert "# Source:" not in text, "the round trip should drop our comments"
        assert _both_halves(text)[1] == RING_ACTIVE_PLAYBACK_DEVICE
    elif mutation == "crossover_drift":
        text = ring_graph.replace("freq: 2500.0000", "freq: 2200.0000", 1)
    elif mutation == "forbidden_playback_lane":
        text = ring_graph.replace(RING_ACTIVE_PLAYBACK_DEVICE, RING_PLAYBACK_DEVICE)
    assert (text != ring_graph) is (mutation != "none")

    binding = _layer_a_binding(topology, applied, text)

    assert binding["status"] == status, binding
    assert binding["matches"] is (status == "current")
    if status == "unverifiable":
        assert binding["expected_fingerprint"] is None
        # The emitter really does refuse this device, so the degradation is
        # routed rather than hypothesised.
        with pytest.raises(ActiveSpeakerConfigError):
            recompose_applied_baseline_yaml(
                topology, applied_profile=applied, playback_device=RING_PLAYBACK_DEVICE
            )


async def test_the_drift_check_neutralizes_the_transport_axis(
    applied_box, tmp_path, monkeypatch,
):
    """The Layer-A expectation is built against the endpoint of the graph it is
    COMPARED to — the caller's readback — not the box's statefile, which is
    staged here deliberately disagreeing."""
    from unittest import mock

    topology, applied = applied_box
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        ring_graph.replace(RING_ACTIVE_PLAYBACK_DEVICE, DEFAULT_PLAYBACK_DEVICE),
        name="other.yml",
    )
    spy = mock.Mock(return_value=(None, []))

    with mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml", spy
    ):
        _layer_a_binding(topology, applied, ring_graph)

    assert spy.call_args is not None, "the drift check never reached the recomposer"
    assert spy.call_args.kwargs.get("playback_device") == RING_ACTIVE_PLAYBACK_DEVICE


# --------------------------------------------------------------------------
# 4. The seams that must route through the one derivation.
# --------------------------------------------------------------------------


# Exempt because the endpoint CANCELS: this producer's re-emit is
# FINGERPRINTED (``NormalizedActiveRawIdentity``, which freezes the devices
# block) and compared against a fingerprint a planning step recorded from the
# same snapshot-default recompose, so both ends move together — feeding the live
# endpoint to one end and not the stored other would invalidate every
# fingerprint already on disk. Fingerprint-ONLY: the text is then discarded.
_ENDPOINT_EXEMPT_CALL_SITES = {
    "jasper/active_speaker/commissioning_isolated_producer.py",
}


async def test_every_recompose_call_site_names_the_endpoint_or_is_exempt():
    """A WALKING guard over the CALL SITES, so a fourth seam cannot arrive
    quietly; a stale exemption fails too, being a rule protecting nothing."""
    found: set[str] = set()
    missing: list[str] = []
    for rel, _tree, call in _jasper_calls("recompose_applied_baseline_yaml"):
        found.add(rel)
        if any(kw.arg == "playback_device" for kw in call.keywords):
            continue
        if rel not in _ENDPOINT_EXEMPT_CALL_SITES:
            missing.append(f"{rel}:{call.lineno}")

    assert found, "no recompose call sites found — this guard has gone vacuous"
    assert not missing, (
        "these rebuild a roleful box's active graph without naming an endpoint, "
        "so they inherit the applied snapshot's lane and move an armed speaker "
        f"off the ring (#2339/#2337): {missing}"
    )
    assert not _ENDPOINT_EXEMPT_CALL_SITES - found, "exemption names no call site"


@pytest.mark.parametrize("seam", ["sound_carrier", "bass_extension"])
async def test_the_re_emit_seams_forward_the_derived_endpoint(
    applied_box, tmp_path, monkeypatch, seam,
):
    """The derived value REACHES the recomposer, not merely gets computed: a
    seam that resolves the endpoint and drops it reads as fixed to any
    source-level check while behaving exactly like the defect, so a sentinel is
    threaded through the real seam and caught at the recomposer's boundary."""
    from unittest import mock

    from jasper.sound import graph_carrier

    topology, applied = applied_box
    sentinel_device = "jts_sentinel_endpoint"
    spy = mock.Mock(return_value=(None, []))

    with mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml", spy
    ), mock.patch(
        "jasper.active_speaker.playback_route.resolve_live_active_endpoint",
        mock.Mock(return_value=(sentinel_device, LOADED_GRAPH_SOURCE)),
    ), pytest.raises(graph_carrier.CarrierCannotHostEq):
        if seam == "sound_carrier":
            graph_carrier._recompose_active_baseline_with_eq(
                SoundProfile(enabled=False), out_path=None
            )
        else:
            selected = tmp_path / "selected.yml"
            selected.write_text(_graph_for(topology, applied, None), encoding="utf-8")
            preference_path = tmp_path / "pref.json"
            preference_path.write_text(
                json.dumps(SoundProfile(enabled=False).to_dict()), encoding="utf-8"
            )
            settings_path = tmp_path / "sound-settings.json"
            settings_path.write_text("{}", encoding="utf-8")
            graph_carrier.recompose_active_baseline_for_bass_extension(
                topology,
                applied_profile=applied,
                desired_profile=None,
                current_config_path=selected,
                preference_profile_path=preference_path,
                sound_settings_path=settings_path,
            )

    assert spy.call_args is not None, f"{seam} never reached the recomposer"
    assert spy.call_args.kwargs.get("playback_device") == sentinel_device


# --------------------------------------------------------------------------
# 5. #2344 / #2412 — the COMMISSIONING WIZARD's graphs on an armed box.
#
# The per-driver emit used to resolve the ring by NAME through the fresh-emit
# chooser while forwarding none of the REST of the device contract, so it emitted
# a ring sink over `plug:jasper_capture` — the tap fan-in stops feeding under
# `shm_ring`. What replaces "nothing was emitted" as the safety property is the
# PAIR: the sink is the ring the caller asked for AND the source is Ring A. A
# refusal that fires before the emit must still leave nothing on disk for the
# next reader, and one that fires earlier still must not invent a transport
# failure for a box no owner refused.
# --------------------------------------------------------------------------


def _recorded_commissioning_emit(monkeypatch) -> list[dict]:
    """Record every commissioning-emitter call as ``{"preset", "kwargs"}`` and
    pass it through. The preset recorded is the BOUND one: staging binds it to
    the topology, which relabels the outputs."""
    from jasper.active_speaker import camilla_yaml, staging

    calls: list[dict] = []
    real = camilla_yaml.emit_active_speaker_commissioning_config

    def recording(preset_arg, **kwargs):
        calls.append({"preset": preset_arg, "kwargs": dict(kwargs)})
        return real(preset_arg, **kwargs)

    monkeypatch.setattr(staging, "emit_active_speaker_commissioning_config", recording)
    return calls


def _prepare_commissioning(topology, preset, out_dir, device, *, role="woofer"):
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    return prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role=role,
        preset=preset,
        playback_device=device,
        config_dir=out_dir,
        run_config_check=False,
    )


def _blockers(payload: dict) -> list[dict]:
    return [i for i in payload.get("issues") or [] if i.get("severity") == "blocker"]


def _commissioning_yaml(topology, preset, out_dir, device) -> str:
    payload = _prepare_commissioning(topology, preset, out_dir, device)
    assert payload["status"] == "prepared" and not _blockers(payload), payload
    return Path(payload["config"]["path"]).read_text(encoding="utf-8")


def _anchor_yaml(topology, preset, out_dir, device) -> str:
    """Stage the durable boot anchor at ``device``; return the emitted YAML."""
    from jasper.active_speaker.staging import stage_protected_startup_config

    payload = stage_protected_startup_config(
        topology,
        preset=preset,
        playback_device=device,
        config_dir=out_dir,
        # Pinned to tmp_path like every other call site: the default is the real
        # `/var/lib/jasper/active_speaker_staged_config.json`, so omitting it
        # makes a Pi-side run overwrite a live speaker's staged metadata.
        metadata_path=out_dir / "staged_metadata.json",
        run_config_check=False,
    )
    assert payload["status"] == "staged" and not _blockers(payload), payload
    # The audible emit's transport refusal never reaches the anchor: a speaker
    # that cannot refresh its own boot config is a worse failure than the one
    # #2412 fixed, so the retired rung is asserted absent at every call site.
    assert "commissioning_ring_transport_unsupported" not in _codes(payload), payload
    return Path(payload["config"]["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("route", ["marker", "explicit", "unarmed"])
async def test_driver_commissioning_emits_a_coherent_graph_on_the_active_ring(
    commissioning_box, tmp_path, monkeypatch, route,
):
    """THE CAPABILITY (#2412 Wave 3): a ring box commissions, both ends agree.

    Three routes to the ACTIVE ring — the production marker, an explicit lab
    override, and an UNARMED box, the control that the verdict is keyed on the
    graph's coherence rather than on the marker.
    """
    topology, preset = commissioning_box
    emits = _recorded_commissioning_emit(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: route == "marker",
    )

    payload = _prepare_commissioning(
        topology,
        preset,
        tmp_path / route,
        RING_ACTIVE_PLAYBACK_DEVICE if route == "explicit" else None,
    )

    assert payload["status"] == "prepared" and not _blockers(payload), payload
    assert len(emits) == 1, emits
    assert emits[0]["kwargs"]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert emits[0]["kwargs"]["capture_device"] == RING_CAPTURE_DEVICE
    assert _gate(payload, "commissioning_transport_supported")["passed"] is True
    assert "commissioning_ring_transport_unsupported" not in _codes(payload), payload
    # The artifact ON DISK carries the pair: that is what the gate re-reads and
    # what CamillaDSP will open.
    written = Path(payload["config"]["path"]).read_text(encoding="utf-8")
    assert _both_halves(written) == (RING_CAPTURE_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)


async def test_every_ring_pcm_shares_one_capture_transport():
    """Ring A is the source for EVERY ring sink, so a ring emit's pair is
    coherent whichever member it names (ADR-0100)."""
    from jasper.active_speaker.camilla_yaml import capture_device_for_playback
    from jasper.fanin_coupling import RING_PCM_DEVICES

    assert RING_PLAYBACK_DEVICE in RING_PCM_DEVICES
    assert RING_PLAYBACK_DEVICE != RING_ACTIVE_PLAYBACK_DEVICE
    for member in RING_PCM_DEVICES:
        assert capture_device_for_playback(member) == RING_CAPTURE_DEVICE, member


@pytest.mark.parametrize(
    "scenario, code, gate_passed, wrote_config",
    [
        ("stereo_ring", "commissioning_config_generation_failed", True, False),
        ("half_forwarded_block", "commissioning_transport_ends_disagree", False, True),
        ("unknown_role", "commissioning_target_role_unknown", True, False),
    ],
)
async def test_a_blocked_prepare_names_the_guard_that_actually_owns_it(
    commissioning_box, tmp_path, monkeypatch, scenario, code, gate_passed, wrote_config,
):
    """Three refusals, three owners, none of them the retired transport rung.

    ``half_forwarded_block`` mutates the capture DOWNSTREAM of the call site —
    the shape ``test_ring_active_endpoint.py``'s field walk is blind to, so the
    two guards are demonstrably not one guard twice. Its refusal is at the gate
    rather than before the write because the gate is a re-read; what stops the
    load is ``status``, which fails the preflight's ``prepared``.
    """
    from jasper.active_speaker import staging as staging_mod

    topology, preset = commissioning_box
    device = {"stereo_ring": RING_PLAYBACK_DEVICE}.get(
        scenario, RING_ACTIVE_PLAYBACK_DEVICE
    )
    if scenario == "half_forwarded_block":
        real = staging_mod.emit_active_speaker_commissioning_config
        monkeypatch.setattr(
            staging_mod,
            "emit_active_speaker_commissioning_config",
            # The pre-Wave-1 defect: ring sink, retired tap for a source.
            lambda preset_arg, **kw: real(
                preset_arg, **{**kw, "capture_device": RETIRED_ALOOP_CAPTURE_DEVICE}
            ),
        )

    payload = _prepare_commissioning(
        topology,
        preset,
        tmp_path / scenario,
        device,
        role="nosuchrole" if scenario == "unknown_role" else "woofer",
    )

    assert payload["status"] == "blocked", payload
    codes = _codes(payload)
    assert code in codes, payload["issues"]
    assert "commissioning_ring_transport_unsupported" not in codes, payload
    assert _gate(payload, "commissioning_transport_supported")["passed"] is gate_passed
    assert payload["config"]["exists"] is wrote_config, payload["config"]
    if scenario == "half_forwarded_block":
        # The graph that reached disk really is the silent-sweep pair, so the
        # assertions above cannot be satisfied by a broken emit.
        capture, playback = _both_halves(
            Path(payload["config"]["path"]).read_text(encoding="utf-8")
        )
        assert (playback, capture != RING_CAPTURE_DEVICE) == (
            RING_ACTIVE_PLAYBACK_DEVICE,
            True,
        )
    else:
        assert "commissioning_transport_ends_disagree" not in codes, payload


# --------------------------------------------------------------------------
# 6. THE ARMED-TRANSPORT GATE AT THE LOAD ALTITUDE (#2412 Wave 3).
#
# The prepare gate is a pure builder and reads no daemon env, so it proves
# COHERENCE, not LIVENESS: a ring/ring graph on a box whose fan-in is loopback-
# coupled, or whose endpoint was never armed, is self-consistent, loads cleanly
# and plays to nobody. The two conjuncts have two OWNERS — the coupling in
# `fanin.env`, the ACTIVE-endpoint marker in `outputd.env`, one reconciler each —
# so each case moves ONE term with the other armed, and a crossed
# mutant-to-test mapping cannot score a survival.
# --------------------------------------------------------------------------


_ABSENT_FILE = object()

_FEED_UNARMED = "commissioning_ring_feed_unarmed"
_ENDPOINT_UNARMED = "commissioning_active_endpoint_unarmed"
# Each code carries its OWN reconciler's remedy and the two are not
# interchangeable; no structured field carries the command, so it is asserted in
# the household message.
_REMEDY = {
    _FEED_UNARMED: "jasper-fanin-coupling-reconcile",
    _ENDPOINT_UNARMED: "jasper-audio-hardware-reconcile",
}


def _ring_transport_state(monkeypatch, tmp_path, *, coupling, marker: str):
    """Point BOTH reconciler-owned files at ``tmp_path`` and write the state.

    Real files rather than stubbed predicates: the gate's contract is that it
    reads each file FRESH on every call, and a monkeypatched predicate cannot
    fail that way. ``coupling=None`` writes the file with the key ABSENT and
    ``_ABSENT_FILE`` writes no file at all — the two shapes a box carries before
    the reconciler has ever named a transport.
    """
    from jasper.fanin_coupling import (
        COUPLING_ENV_VAR,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
    )

    fanin_env = Path(tmp_path) / "fanin.env"
    outputd_env = Path(tmp_path) / "outputd.env"
    if coupling is not _ABSENT_FILE:
        fanin_env.write_text(
            "" if coupling is None else f"{COUPLING_ENV_VAR}={coupling}\n",
            encoding="utf-8",
        )
    outputd_env.write_text(
        f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}={marker}\n", encoding="utf-8"
    )
    monkeypatch.setattr("jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env))
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    return fanin_env, outputd_env


def _ring_load_preflight(topology, preset, out_dir, device=RING_ACTIVE_PLAYBACK_DEVICE):
    from jasper.active_speaker.commission_load import (
        build_driver_commission_load_preflight,
    )

    return build_driver_commission_load_preflight(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=device,
        config_dir=out_dir,
        require_physical_identity=False,
    )


@pytest.mark.parametrize(
    "coupling, marker, corrupt, blocked_by",
    [
        ("shm_ring", "1", None, frozenset()),
        ("", "1", None, frozenset()),
        (None, "1", None, frozenset()),
        (_ABSENT_FILE, "1", None, frozenset()),
        ("loopback", "1", None, frozenset({_FEED_UNARMED})),
        ("shm_ring", "0", None, frozenset({_ENDPOINT_UNARMED})),
        ("shm_ring", "1", "fanin", frozenset({_FEED_UNARMED, _ENDPOINT_UNARMED})),
        ("shm_ring", "1", "outputd", frozenset({_FEED_UNARMED, _ENDPOINT_UNARMED})),
    ],
    ids=["declared", "empty_value", "absent_key", "absent_file", "refused_token",
         "endpoint_unarmed", "corrupt_fanin", "corrupt_outputd"],
)
async def test_the_guarded_load_verdict_follows_the_two_reconciler_files(
    commissioning_box, tmp_path, monkeypatch, coupling, marker, corrupt, blocked_by,
):
    """The load gate's verdict, one term at a time, with the fully-armed control.

    ADR-0100 left one transport: ``jasper-fanin`` serves an absent key, an empty
    value and ``shm_ring`` alike and refuses anything else as a config-class
    fault (exit 78, the unit parks), so only a refused value says the ring is
    unfed. A non-UTF-8 file raises ``UnicodeDecodeError`` — a ``ValueError``,
    not an ``OSError`` — which would leave this preflight, the first caller to
    read either file, and take the blocker with it; both conjuncts fail closed
    there because a decode failure says nothing about which file was bad.
    """
    topology, preset = commissioning_box
    fanin_env, outputd_env = _ring_transport_state(
        monkeypatch, tmp_path, coupling=coupling, marker=marker
    )
    if corrupt is not None:
        target = fanin_env if corrupt == "fanin" else outputd_env
        target.write_bytes(b"JASPER_FANIN_CAMILLA_COUPLING=\xff\xfeshm_ring\n")
        # The byte really is undecodable, so this case cannot pass because the
        # file happened to stay readable.
        with pytest.raises(UnicodeDecodeError):
            target.read_text(encoding="utf-8")

    preflight = _ring_load_preflight(topology, preset, tmp_path / "load")

    codes = _codes(preflight)
    assert codes & set(_REMEDY) == blocked_by, preflight["issues"]
    assert "commissioning_ring_transport_unsupported" not in codes, preflight["issues"]
    assert _gate(preflight, "commissioning_transport_armed")["passed"] is (
        not blocked_by
    )
    for code in blocked_by:
        issue = _issue(preflight, code)
        assert (issue["severity"], _REMEDY[code] in issue["message"]) == (
            "blocker",
            True,
        ), issue
    if blocked_by:
        assert preflight["load_allowed"] is False
    else:
        assert _gate(preflight, "commissioning_transport_supported")["passed"] is True
        assert "commissioning_transport_ends_disagree" not in codes, preflight["issues"]


async def test_the_guarded_load_reads_no_transport_state_off_the_ring(
    commissioning_box, tmp_path, monkeypatch,
):
    """SCOPE: a non-ring graph needs no ring armed and consults neither file —
    both are pointed at paths that do not exist, so the readers would refuse if
    consulted, and the gate passing is what says an unarmed fleet box on the
    ALSA active lane behaves as it did before the wave."""
    topology, preset = commissioning_box
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(tmp_path / "gone" / "fanin.env")
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "gone" / "outputd.env"),
    )

    preflight = _ring_load_preflight(
        topology, preset, tmp_path / "alsa", OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    )

    assert _gate(preflight, "commissioning_transport_armed")["passed"] is True
    assert not _codes(preflight) & set(_REMEDY), preflight["issues"]


@pytest.mark.parametrize(
    "first, second, code",
    [
        (("loopback", "1"), ("shm_ring", "1"), _FEED_UNARMED),
        (("shm_ring", "0"), ("shm_ring", "1"), _ENDPOINT_UNARMED),
    ],
    ids=["coupling", "marker"],
)
async def test_the_guarded_load_re_reads_the_transport_state_every_call(
    commissioning_box, tmp_path, monkeypatch, first, second, code,
):
    """The state is read FRESH per call, never cached from the first one.

    This preflight runs inside the long-lived control daemon and the
    socket-activated wizards, which never ``EnvironmentFile=``d either file and
    stay alive across a reconcile: a reader that cached would keep refusing a
    box an operator had just armed.
    """
    topology, preset = commissioning_box
    _ring_transport_state(monkeypatch, tmp_path, coupling=first[0], marker=first[1])

    blocked = _ring_load_preflight(topology, preset, tmp_path / "before")
    assert code in _codes(blocked), blocked["issues"]
    assert _gate(blocked, "commissioning_transport_armed")["passed"] is False

    _ring_transport_state(monkeypatch, tmp_path, coupling=second[0], marker=second[1])
    rearmed = _ring_load_preflight(topology, preset, tmp_path / "after")

    assert _gate(rearmed, "commissioning_transport_armed")["passed"] is True
    assert code not in _codes(rearmed), rearmed["issues"]


# --------------------------------------------------------------------------
# 7. THE BOOT ANCHOR'S DEVICE BLOCK (#2364) and the audible emit's (#2412).
#
# `stage_protected_startup_config` forwarded only the device NAME, so a box
# re-staged at the ACTIVE ring got a ring sink over `plug:jasper_capture` — the
# tap fan-in STOPS feeding under `shm_ring` — with the loopback chunk/target/
# queue geometry, in the artifact it BOOTS from, and nothing downstream
# inspected it. `prepare_driver_commissioning_config` is its twin: same module,
# same emitter, same seven-field contract. Both now route through
# `active_emit_devices`, the derivation the recomposer already reads.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["marker", "explicit"])
async def test_boot_anchor_derives_the_ring_device_block(
    commissioning_box, tmp_path, monkeypatch, route,
):
    """On an armed box the anchor is NOT refused, and every half of its device
    contract is the ring's — each value asserted against its OWNER, since a
    literal would pass against a graph that had drifted from what fan-in
    declares. Both routes: the production marker and an explicit override."""
    from jasper.active_speaker.camilla_yaml import active_emit_devices
    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
        resolve_ring_wire,
    )

    topology, preset = commissioning_box
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    yaml = _anchor_yaml(
        topology,
        preset,
        tmp_path / route,
        RING_ACTIVE_PLAYBACK_DEVICE if route == "explicit" else None,
    )
    wire = resolve_ring_wire(topology).sample_format

    assert (
        active_emit_devices(RING_ACTIVE_PLAYBACK_DEVICE, topology=topology).capture_device
        == RING_CAPTURE_DEVICE
    )
    assert f'device: "{RING_CAPTURE_DEVICE}"' in yaml
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in yaml
    # Both ends of one wire: the three rings share one format, so a graph
    # carrying two is a sheared attach waiting at the arm.
    assert yaml.count(f"format: {wire}") == 2, yaml
    assert f"chunksize: {RING_CAMILLA_CHUNKSIZE}" in yaml
    assert f"target_level: {RING_CAMILLA_TARGET_LEVEL}" in yaml
    assert f"queuelimit: {RING_CAMILLA_QUEUELIMIT}" in yaml
    assert f"enable_rate_adjust: {str(RING_CAMILLA_ENABLE_RATE_ADJUST).lower()}" in yaml
    # The tap is GONE, not merely outnumbered.
    assert "plug:jasper_capture" not in yaml, yaml


@pytest.mark.parametrize("stage", ["boot_anchor", "driver_commissioning"])
@pytest.mark.parametrize(
    "device",
    [OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "hw:CARD=Lab,DEV=0"],
    ids=["aloop_active_lane", "lab_override"],
)
async def test_the_derived_device_block_is_byte_identical_off_the_ring(
    commissioning_box, tmp_path, monkeypatch, stage, device,
):
    """Off the ring the derived block reproduces the PRE-CHANGE bytes exactly.

    The blast-radius bound for #2364 and #2412 Wave 1, proven by REPLAYING the
    emit staging just made minus the device kwargs — literally the pre-change
    call shape, with the defaults re-read from the emitter's own signature
    rather than hand-copied into a table that would rot.
    """
    from jasper.active_speaker.camilla_yaml import (
        ActiveEmitDevices,
        emit_active_speaker_commissioning_config,
    )

    device_fields = {f.name for f in dataclasses.fields(ActiveEmitDevices)}
    assert device_fields, "ActiveEmitDevices lost its fields; this test is vacuous"
    topology, preset = commissioning_box
    emits = _recorded_commissioning_emit(monkeypatch)
    build = _anchor_yaml if stage == "boot_anchor" else _commissioning_yaml

    derived = build(topology, preset, tmp_path / "derived", device)

    assert len(emits) == 1, "staging never reached the emitter; this proves nothing"
    replay = {
        key: value
        for key, value in emits[0]["kwargs"].items()
        if key not in device_fields
    }
    replay["out_path"] = tmp_path / "replay.yml"
    assert derived == emit_active_speaker_commissioning_config(
        emits[0]["preset"], **replay
    ), f"the derived device block changed a NON-ring {stage} emit"


async def test_boot_anchor_refuses_a_typod_ring_wire_instead_of_tracebacking(
    commissioning_box, tmp_path, monkeypatch,
):
    """A bad ``JASPER_FANIN_RING_WIRE_FORMAT`` is this function's blocker, not a
    crash: the ``/sound/`` wizard calls it too, so an unhandled ``ValueError``
    is a 500 on a household page. Nothing may be written, and the ALSA control
    says the branch did not block everything."""
    from jasper.active_speaker.staging import stage_protected_startup_config
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    topology, preset = commissioning_box
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s32le\n", encoding="utf-8")
    monkeypatch.setattr("jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env))

    out_dir = tmp_path / "ring"
    payload = stage_protected_startup_config(
        topology,
        preset=preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        config_dir=out_dir,
        metadata_path=out_dir / "staged_metadata.json",
        run_config_check=False,
    )

    assert payload["status"] == "blocked", payload
    issue = _issue(payload, "ring_wire_declaration_invalid")
    assert issue["severity"] == "blocker", issue
    # No structured field carries the typed token; the operator has to see it.
    assert RING_WIRE_FORMAT_ENV_VAR in issue["message"], issue
    assert "s32le" in issue["message"], issue
    # The refusal precedes the write, so a bad wire leaves no half-formed anchor.
    assert not Path(payload["config"]["path"]).exists(), payload["config"]["path"]
    assert _anchor_yaml(
        topology, preset, tmp_path / "alsa", OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    )


# --------------------------------------------------------------------------
# 8. #2412 Wave 4 — the transport is on the journal line.
#
# A commissioning graph whose sink was the ring while its source was still the
# snd-aloop tap was invisible in the field even though the
# `driver_commission_prepared` line already named the role and the outputs. The
# `load` line's twin pin lives in `tests/test_active_speaker_commission_load.py`.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case, device, transport, capture, wire",
    [
        # `wire` None is read from the box's own declaration below, not a literal.
        ("ring", RING_ACTIVE_PLAYBACK_DEVICE, TRANSPORT_RING, RING_CAPTURE_DEVICE, None),
        # Not a ring end: ADR-0100 left one transport, so the line reports the
        # journal's own "no answer" literal rather than a second name.
        ("aloop", OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "-", RING_CAPTURE_DEVICE, "-"),
        ("unresolved", None, "-", "-", "-"),
    ],
)
async def test_the_prepared_line_names_the_transport_it_actually_emitted(
    commissioning_box, tmp_path, monkeypatch, caplog, case, device, transport,
    capture, wire,
):
    """Every polarity on ONE record, each value from its owning constant.

    One polarity would pass on a line that hard-coded either answer, and the
    ring wire is per-box config an operator can roll back, so a literal would go
    green against a line reporting the wrong wire on a narrow box.
    ``unresolved`` must report ``-`` rather than invent a transport or take the
    record down — never an empty value, which reads as the next field.
    """
    from jasper.active_speaker import staging as staging_mod
    from jasper.fanin_coupling import resolve_ring_wire

    topology, preset = commissioning_box
    if wire is None:
        wire = resolve_ring_wire(topology).sample_format
        assert wire in ("S16_LE", "S32_LE"), wire

    with caplog.at_level(logging.INFO, logger=STAGING_LOGGER):
        if case == "unresolved":
            monkeypatch.setattr(
                staging_mod,
                "resolve_active_playback_device",
                lambda *a, **k: (None, "missing"),
            )
            _prepare_commissioning(topology, preset, tmp_path / case, device)
        else:
            _commissioning_yaml(topology, preset, tmp_path / case, device)

    fields = _event_fields(caplog.records, "active_speaker.driver_commission_prepared")
    assert (fields["transport"], fields["capture"], fields["wire"]) == (
        transport,
        capture,
        wire,
    )
    assert fields["playback"] == (device or "-")


# --------------------------------------------------------------------------
# 9. #2412 Wave 5 — the hearing-safety evidence, made mechanical.
#
# No production change. These turn the design's §1.6 argument — "moving
# commissioning onto the ring changes the TRANSPORT and touches no protection" —
# into assertions the suite re-runs.
# --------------------------------------------------------------------------


def _leaf_paths(node, prefix: str = "") -> dict[str, Any]:
    """Flatten a parsed YAML document to ``{dotted.path: scalar}``.

    Over the PARSED document: comments, key order and quoting style are not part
    of the contract, and a text diff would report all three as changes while
    missing a value that moved between two equivalent spellings.
    """
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_leaf_paths(value, f"{prefix}{key}."))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.update(_leaf_paths(value, f"{prefix}{index}."))
    else:
        out[prefix.rstrip(".")] = node
    return out


@pytest.mark.parametrize("wire", ["S32_LE", "S16_LE"], ids=["wide", "narrow"])
async def test_the_ring_emit_changes_the_transport_and_nothing_else(
    commissioning_box, tmp_path, monkeypatch, wire,
):
    """THE LOAD-BEARING ASSERTION: the seven device fields at most, nothing else.

    Everything #2412 claims about hearing safety rests on this, so it is a
    STRUCTURAL DIFF rather than an enumeration of what should stay put, which
    cannot notice a field nobody thought to enumerate. BOTH WIRES, because the
    count is not fixed: the emitter's non-ring formats are ``S32_LE`` and the
    shipped ring default is WIDE, so on an ordinary box the format pair holds
    one value on both transports and only five fields move — the design's
    "device, two formats, four knobs" is exact only as an upper bound.
    """
    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
    )

    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda: wire
    )
    topology, preset = commissioning_box
    flat = {
        lane: _leaf_paths(
            yaml_parser.safe_load(
                _commissioning_yaml(topology, preset, tmp_path / lane, sink)
            )
        )
        for lane, sink in (
            ("aloop", OUTPUTD_ACTIVE_PLAYBACK_DEVICE),
            ("ring", RING_ACTIVE_PLAYBACK_DEVICE),
        )
    }
    aloop, ring = flat["aloop"], flat["ring"]
    assert len(aloop) > 50, "the graph got small; this bound stopped meaning much"

    changed = {p for p in set(aloop) | set(ring) if aloop.get(p) != ring.get(p)}
    formats = {"devices.capture.format", "devices.playback.format"}
    always_move = {
        "devices.playback.device",
        "devices.chunksize",
        "devices.target_level",
        "devices.queuelimit",
        "devices.enable_rate_adjust",
    }
    assert changed <= always_move | formats, changed - (always_move | formats)
    assert always_move <= changed, always_move - changed
    # The capture device is NOT among them: Ring A is the only fan-in →
    # CamillaDSP transport (ADR-0100), so the source does not depend on the sink.
    assert "devices.capture.device" not in changed
    # The format pair moves on exactly the wire where the ring's answer differs
    # from the emitter's non-ring default, never on the other.
    assert (formats <= changed) is (wire != DEFAULT_PLAYBACK_FORMAT), (wire, changed)

    # ...and the direction of each, every expectation from its owning constant.
    assert aloop["devices.capture.device"] == RING_CAPTURE_DEVICE
    assert ring["devices.capture.device"] == RING_CAPTURE_DEVICE
    assert ring["devices.capture.format"] == ring["devices.playback.format"] == wire
    assert ring["devices.chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert ring["devices.target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert ring["devices.queuelimit"] == RING_CAMILLA_QUEUELIMIT
    assert ring["devices.enable_rate_adjust"] is RING_CAMILLA_ENABLE_RATE_ADJUST
    # The ceiling is in the diff's complement, and named anyway because it is the
    # one value a hearing panel looks for by eye.
    assert ring["devices.volume_limit"] == aloop["devices.volume_limit"] == 0.0


async def test_the_unattended_mute_proof_gains_no_caller():
    """``output_terminally_muted``'s caller set is unchanged (§1.6): the way the
    unattended-silence invariant could quietly stop holding is a THIRD caller,
    proving itself silent with the same primitive while reasoning differently
    about what it may then do."""
    callers: set[str] = set()
    for rel, tree, call in _jasper_calls("output_terminally_muted"):
        enclosing = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno <= call.lineno <= (node.end_lineno or call.lineno)
        ]
        owner = min(enclosing, key=lambda fn: (fn.end_lineno or fn.lineno) - fn.lineno)
        callers.add(f"{rel}::{owner.name}")

    assert callers == {
        # "this box may BOOT, because nothing can make a sound"
        "jasper/active_speaker/runtime_contract.py::_flat_output_terminally_muted",
        # "this box may ARM ITSELF onto the ring, for the same reason"
        "jasper/fanin/ring_health.py::_anchor_is_all_muted",
    }, callers


async def test_the_audible_evidence_holds_identically_on_a_ring_graph(
    commissioning_box, tmp_path,
):
    """The attended-audible proof does not notice the transport (§1.6).

    Both halves — off-device over the emitted YAML, and over the graph read back
    from CamillaDSP — asserted as EQUALITY of the check dicts across transports:
    a ring graph that passed for another reason, or ran fewer checks, satisfies
    "the ring one passes". The gate rides along because its id is what
    ``/sound/`` renders while the function is what decides it.
    """
    from jasper.active_speaker.staging import running_commission_evidence

    topology, preset = commissioning_box

    def _evidence(device, out_dir):
        payload = _prepare_commissioning(topology, preset, out_dir, device)
        assert payload["status"] == "prepared", payload
        # The payload's OWN evidence, not a recomputation: staging binds the
        # preset to the topology, so evidence rebuilt from the raw preset would
        # answer about a different mask than the one the gate judged.
        off_device = payload["audible_evidence"]
        text = Path(payload["config"]["path"]).read_text(encoding="utf-8")
        running = running_commission_evidence(
            # CamillaDSP hands the graph back in its own YAML dialect.
            yaml_parser.safe_dump(yaml_parser.safe_load(text), default_flow_style=False),
            audible_outputs=off_device["audible_outputs"],
            muted_outputs=off_device["muted_outputs"],
            tweeter_outputs=off_device["tweeter_outputs"],
            protective_hp_hz=off_device["protective_highpass_hz"],
            expected_headroom_db=COMMISSIONING_HEADROOM_DB,
        )
        return off_device, running, _gate(payload, "driver_protection_while_audible")

    aloop_off, aloop_live, aloop_gate = _evidence(
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE, tmp_path / "aloop"
    )
    ring_off, ring_live, ring_gate = _evidence(
        RING_ACTIVE_PLAYBACK_DEVICE, tmp_path / "ring"
    )

    assert aloop_off["passed"] is True and aloop_live["passed"] is True
    assert ring_off["checks"] == aloop_off["checks"], (ring_off, aloop_off)
    assert ring_live["checks"] == aloop_live["checks"], (ring_live, aloop_live)
    assert ring_off["audible_outputs"] == aloop_off["audible_outputs"]
    assert ring_off["muted_outputs"] == aloop_off["muted_outputs"]
    assert ring_gate["passed"] is aloop_gate["passed"] is True


async def test_the_ramp_gate_holds_identically_on_a_ring_graph():
    """All seven Stage-5 checks, same verdicts, both transports.

    The ramp is the audible path and its gate re-proves every hearing
    protection on the RUNNING graph. Asserted over the WHOLE ``checks`` dict so
    a check that answers differently on the ring fails even unpredicted.
    """
    from jasper.active_speaker.calibration_level import MIN_TEST_LEVEL_DBFS
    from jasper.active_speaker.camilla_yaml import (
        STARTUP_MUTE_GAIN_DB,
        active_emit_devices,
        emit_active_speaker_commissioning_config,
    )
    from jasper.active_speaker.commission_ramp import build_stage5_ramp_gate
    from jasper.active_speaker.staging import driver_commission_audible_evidence
    from tests.test_active_speaker_profile import _two_way_preset

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    audible = set(audible_outputs_for_role(preset, "woofer"))

    def _emit_at(device: str, gain: float) -> str:
        # Every ActiveEmitDevices field maps 1:1 onto an emitter parameter.
        return emit_active_speaker_commissioning_config(
            preset,
            playback_device=device,
            **dataclasses.asdict(active_emit_devices(device)),
            audible_outputs=audible,
            audible_gain_db=gain,
            startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        )

    def _checks(device: str) -> dict:
        evidence = driver_commission_audible_evidence(
            _emit_at(device, MIN_TEST_LEVEL_DBFS),
            preset=preset,
            audible_outputs=audible,
            expected_headroom_db=COMMISSIONING_HEADROOM_DB,
        )
        return build_stage5_ramp_gate(
            running_config_raw=yaml_parser.safe_dump(
                yaml_parser.safe_load(_emit_at(device, STARTUP_MUTE_GAIN_DB))
            ),
            role="woofer",
            present_roles=frozenset({"woofer", "tweeter"}),
            audible_outputs=evidence["audible_outputs"],
            muted_outputs=evidence["muted_outputs"],
            tweeter_outputs=evidence["tweeter_outputs"],
            protective_hp_hz=evidence["protective_highpass_hz"],
            current_gain_db=STARTUP_MUTE_GAIN_DB,
            next_gain_db=MIN_TEST_LEVEL_DBFS,
            confirmed_roles=frozenset(),
            prior_step_cleared=False,
        )

    aloop = _checks(OUTPUTD_ACTIVE_PLAYBACK_DEVICE)
    ring = _checks(RING_ACTIVE_PLAYBACK_DEVICE)

    assert set(aloop["checks"]) == {
        "gain_within_envelope",
        "gain_step_bounded",
        "live_mask_and_highpass",
        "volume_ceiling_0db",
        "driver_limiter_present",
        "role_order_woofer_first",
        "prior_step_acknowledged",
    }, aloop["checks"]
    assert ring["checks"] == aloop["checks"], (ring["checks"], aloop["checks"])
    assert ring["passed"] is aloop["passed"] is True
    # The ceiling, named: an equality assertion is satisfied by two wrongs.
    assert ring["checks"]["volume_ceiling_0db"] is True
