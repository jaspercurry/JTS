# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A re-emit preserves the endpoint the box is LIVE on (#2339, #2337).

THE FAMILY. Four seams rebuild a roleful box's active graph from its immutable
applied snapshot: the deploy/arm-ladder reconcile (``jasper-sound
reconcile-current-dsp``), a ``/sound/`` or ``/eq/`` save, a bass-extension
apply, and the drift check that binds Layer A to the applied profile. The
snapshot is immutable by design, so it keeps naming whichever playback lane was
resolved at Apply time — on a ring-armed box, the snd-aloop lane forever. Every
seam that let that reach the emitter moved the speaker's transport without
anyone asking:

* **#2339, observed on hardware** (jts3 2026-08-11,
  ``captures/r7b-jts3-arm3-20260811T162742Z`` files 14-16). The arm ladder's
  coupling rung runs ``reconcile_current_dsp``, which re-emitted the aloop graph
  over the ring graph rung 1 had just published and re-pointed the statefile at
  it: fan-in and outputd on the ring, CamillaDSP on the aloop pair — silence
  with every daemon healthy, ``writer_alive=False``, Ring A ``drop_no_reader``
  climbing. ``install.sh`` runs that same reconcile on EVERY deploy, so the next
  routine deploy to an armed box ended silent.
* **#2337.** A household EQ save on an armed box re-emitted both halves back to
  the tap; the reconcilers then de-armed the marker and converged the box to
  loopback.
* **The bass-extension apply**, found by the walking guard below rather than by
  reading — the same shape, at the sibling emit site in the same module, and its
  own reproduction check compares only the pre-split PROGRAM layer, so an
  inherited endpoint passes it.
* **The drift check.** ``active_layer_a_fingerprint`` binds ``output_devices``,
  which carries the playback device and the sink's CamillaDSP geometry — so an
  armed box's loaded graph could not match a snapshot-default expectation and
  ``_applied_layer_a_binding`` reported ``mismatch``, blocking room correction
  with "Apply that crossover again".

THE CONTRACT. The three seams that EMIT a graph that will be loaded ask one
derivation —
:func:`jasper.active_speaker.playback_route.resolve_live_active_endpoint` —
"which endpoint is this box on", and it asks the statefile-pointed graph before
the reconciler's marker because the marker is derived FROM the graph. The drift
check emits nothing and instead NEUTRALIZES the transport axis against the very
graph it is comparing, because a third opinion in a two-way comparison would
turn ordinary device-resolution drift into a crossover-drift claim. An unarmed
box is byte-identical to before under all four.

These walk the real functions over real files (a real statefile, a real applied
profile, a real topology) rather than mocking the seam under test: the defect
was a missing argument at a call site, and a mock of that call site would have
passed straight through it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.baseline_profile import (
    STATE_PATH_ENV,
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
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    parse_camilla_devices_config,
)
from jasper.active_speaker import ActiveSpeakerPreset, audible_outputs_for_role
from jasper.active_speaker.camilla_yaml import COMMISSIONING_HEADROOM_DB
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE, RING_CAPTURE_DEVICE
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile


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


def _applied_box(tmp_path: Path, monkeypatch):
    """A commissioned roleful box: real topology + real applied snapshot on disk.

    Returns ``(topology, applied)``. Both are published where production reads
    them (``JASPER_OUTPUT_TOPOLOGY_PATH`` / ``STATE_PATH_ENV``) so the seams
    under test load them the way they do on a Pi.
    """
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
        topology,
        applied_profile=applied,
        playback_device=device,
    )
    assert issues == [], issues
    assert yaml is not None
    return yaml


def _both_halves(yaml_text: str) -> tuple[str | None, str | None]:
    devices = parse_camilla_devices_config(yaml_text)
    return devices.get("capture_device"), devices.get("playback_device")


# --------------------------------------------------------------------------
# 1. THE DERIVATION. Which witness answers, and in which order.
# --------------------------------------------------------------------------


async def test_the_live_endpoint_follows_the_graph_not_the_marker(
    tmp_path, monkeypatch,
):
    """THE GRAPH IS UPSTREAM TRUTH, and mid-arm is the state that proves it.

    Rung 1 has moved the graph onto the ring; the hardware reconciler has not
    run yet, so the marker is still clear. A deploy landing HERE is what used to
    undo rung 1. The marker is *derived from* the graph by
    ``jasper-audio-hardware-reconcile``, so following the graph is following the
    half the reconcilers are converging toward.

    #2285 P2: the opposite window (marker armed, graph on the snd-aloop lane)
    used to be the second half of this pair. It is no longer a disagreement this
    function resolves by ADOPTION — the aloop endpoint was retired, so a graph
    still naming it is DECLINED like any other non-endpoint sink and the chooser
    answers. That direction is pinned below, in the declined-device shapes.
    """
    topology, applied = _applied_box(tmp_path, monkeypatch)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
        name="loaded.yml",
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: False,
    )

    device, source = resolve_live_active_endpoint(topology)

    assert device == RING_ACTIVE_PLAYBACK_DEVICE
    # The SOURCE is asserted, not just the device: the chooser now answers the
    # same name for an active-capable topology, so a device-only assertion would
    # pass while the fall-through was the one answering.
    assert source == LOADED_GRAPH_SOURCE


@pytest.mark.parametrize(
    "shape",
    [
        "no_statefile",
        "dangling_config_path",
        "graph_without_devices",
        "graph_on_a_non_endpoint_device",
        "graph_on_the_retired_aloop_endpoint",
    ],
)
async def test_an_unadoptable_graph_falls_back_to_the_chooser_never_the_snapshot(
    tmp_path, monkeypatch, shape,
):
    """DEFAULT-SAFE, deliberately, and never worse than what it replaced.

    A fresh box has no statefile at all and still has to take a deploy, so a
    graph this derivation cannot adopt is not a refusal. The second witness is
    the CHOOSER (``resolve_active_playback_device``), which answers the ACTIVE
    ring for an active-capable topology — never the applied snapshot, whose lane
    is what re-created the #2339 clobber.

    #2285 P2 renamed this: the second witness used to be the endpoint MARKER,
    and the two marker states below used to answer different devices. The
    chooser no longer reads the marker (there is one legal endpoint to choose),
    so sweeping both states is now the control proving exactly that — a chooser
    that still branched on the marker would answer the retired lane in the
    second half.

    ``graph_on_a_non_endpoint_device`` is the same rule from the other side: a
    device that is not the active lane's transport is not a second answer to
    "which transport", and adopting it would hand the active emitter a device
    its own forbidden-token guard refuses mid-save. There that device is the
    forbidden stereo lane, so the pre-emption is not theoretical.
    ``graph_on_the_retired_aloop_endpoint`` is the shape every box commissioned
    before the retirement is actually in, and the reason
    ``OUTPUTD_ACTIVE_PLAYBACK_DEVICE`` still has a name: it must be DECLINED,
    not followed.
    """
    topology, applied = _applied_box(tmp_path, monkeypatch)
    statefile = tmp_path / "outputd-statefile.yml"
    if shape == "no_statefile":
        pass
    elif shape == "dangling_config_path":
        statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n", encoding="utf-8")
    elif shape == "graph_without_devices":
        graph = tmp_path / "no-devices.yml"
        graph.write_text("pipeline: []\n", encoding="utf-8")
        statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    else:
        from jasper.camilla_config_contract import DEFAULT_PLAYBACK_DEVICE

        declined = (
            OUTPUTD_ACTIVE_PLAYBACK_DEVICE
            if shape == "graph_on_the_retired_aloop_endpoint"
            else DEFAULT_PLAYBACK_DEVICE
        )
        graph = tmp_path / f"{shape}.yml"
        text = _graph_for(topology, applied, None).replace(
            RING_ACTIVE_PLAYBACK_DEVICE, declined
        )
        assert declined in text
        graph.write_text(text, encoding="utf-8")
        statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    for marker_armed in (True, False):
        monkeypatch.setattr(
            "jasper.fanin_coupling.ring_active_endpoint_armed",
            lambda env=None, armed=marker_armed: armed,
        )
        assert resolve_live_active_endpoint(topology) == (
            RING_ACTIVE_PLAYBACK_DEVICE,
            OUTPUTD_ACTIVE_LANE_SOURCE,
        ), marker_armed


async def test_a_declined_non_endpoint_device_is_visible_in_the_journal(
    tmp_path, monkeypatch, caplog,
):
    """Observing a device and declining it is a decision, so it is logged.

    The other fall-through shapes saw nothing; this one SAW a sink and chose not
    to adopt it. The doctor's coupling check reports the resulting incoherence
    later, but without this line the journal cannot distinguish "the derivation
    looked and declined" from "the derivation never looked". DEBUG level: a lab
    box takes this branch on every call, legitimately.
    """
    import logging as _logging

    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_DEVICE

    topology, applied = _applied_box(tmp_path, monkeypatch)
    graph, _statefile = _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, None).replace(
            RING_ACTIVE_PLAYBACK_DEVICE, DEFAULT_PLAYBACK_DEVICE
        ),
        name="stereo-lane.yml",
    )

    with caplog.at_level(_logging.DEBUG, logger="jasper.active_speaker.playback_route"):
        device, source = resolve_live_active_endpoint(topology)

    # The chooser answers the same NAME the adopted case would, so the SOURCE is
    # what says this graph was declined rather than followed.
    assert (device, source) == (
        RING_ACTIVE_PLAYBACK_DEVICE,
        OUTPUTD_ACTIVE_LANE_SOURCE,
    )
    assert "event=active_speaker.live_endpoint" in caplog.text
    assert "result=declined_non_endpoint_device" in caplog.text
    assert f"observed={DEFAULT_PLAYBACK_DEVICE}" in caplog.text
    assert str(graph) in caplog.text

    # A box whose graph names a LEGAL endpoint is not narrated — the line marks
    # the exceptional observation, not every resolution.
    caplog.clear()
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
        name="ring.yml",
    )
    with caplog.at_level(_logging.DEBUG, logger="jasper.active_speaker.playback_route"):
        resolve_live_active_endpoint(topology)
    assert "active_speaker.live_endpoint" not in caplog.text


# --------------------------------------------------------------------------
# 2. #2339 — the deploy / arm-ladder reconcile.
# --------------------------------------------------------------------------


async def test_reconcile_current_dsp_keeps_an_armed_box_on_the_ring(
    tmp_path, monkeypatch,
):
    """THE CLOBBER REPRODUCTION, in the exact shape jts3 was in.

    The statefile points at the ring graph rung 1 published, while the RUNNING
    CamillaDSP is still on the pre-arm ``sound_current.yml`` — nothing in rungs 1
    or 2 reloads Camilla, so that lag is the normal mid-ladder state, and it is
    the state ``reconcile_current_dsp`` was called in at
    ``captures/r7b-jts3-arm3-20260811T162742Z`` file 12. Pre-fix the reconcile
    re-emitted the snapshot's ALSA lane over the ring graph and re-pointed the
    statefile at it. It must now re-emit THROUGH the ring, on both halves.

    #2285 P2: the pre-arm graph is written EXPLICITLY at the retired snd-aloop
    endpoint. It used to be ``_graph_for(..., None)``, which answered that lane
    by default; now the default IS the ring, so leaving it would have made the
    stale graph identical to the live one and quietly emptied this reproduction
    of its contrast. The retired lane is also the real shape on disk for every
    box commissioned before the retirement.
    """
    from jasper.sound.runtime import reconcile_current_dsp

    topology, applied = _applied_box(tmp_path, monkeypatch)
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        ring_graph,
        name="active_speaker_baseline_candidate.yml",
    )

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    stale = config_dir / "sound_current.yml"
    pre_arm = ring_graph.replace(
        RING_ACTIVE_PLAYBACK_DEVICE, OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    ).replace(RING_CAPTURE_DEVICE, DEFAULT_CAPTURE_DEVICE)
    assert pre_arm != ring_graph
    stale.write_text(pre_arm, encoding="utf-8")
    camilla = _FakeCamilla(str(stale))

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=3.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    assert payload["carrier_kind"] == "active"
    emitted = Path(str(camilla.loaded_path)).read_text(encoding="utf-8")
    assert _both_halves(emitted) == (
        RING_CAPTURE_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    ), (
        "the deploy/arm reconcile re-emitted the snapshot's ALSA lane over an "
        "armed box — fan-in and outputd stay on the ring, CamillaDSP does not, "
        "and the speaker goes silent with every daemon healthy (#2339)"
    )


async def test_reconcile_current_dsp_is_byte_identical_to_the_default_recompose(
    tmp_path, monkeypatch,
):
    """The other direction of the same rule: a box already on its endpoint
    does not move.

    The reconcile still exists to refresh the artifact on every deploy (so
    CamillaDSP cannot reopen a stale statefile against freshly-created ring
    files); re-emitting THROUGH the live endpoint keeps that refresh and changes
    nothing else.

    #2285 P2 renamed this from ``..._on_an_unarmed_box``. The distinguishing
    fact was never the marker — it is that the loaded graph already names the
    device the default resolution answers, so re-emitting through it is a no-op.
    With one legal endpoint left, "unarmed" no longer describes a box on a
    different lane, and a title claiming it would have to be read as a promise
    the code stopped making.
    """
    from jasper.sound.runtime import reconcile_current_dsp

    topology, applied = _applied_box(tmp_path, monkeypatch)
    default_graph = _graph_for(topology, applied, None)
    _point_statefile_at(tmp_path, monkeypatch, default_graph, name="loaded.yml")

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    current = config_dir / "sound_current.yml"
    current.write_text(default_graph, encoding="utf-8")
    camilla = _FakeCamilla(str(current))

    profile_path = tmp_path / "sound_profile.json"
    save_profile(SoundProfile(simple_eq=SimpleEq(bass_db=3.0)), profile_path)

    payload = await reconcile_current_dsp(
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
    )

    assert payload["status"] == "reconciled", payload
    emitted = Path(str(camilla.loaded_path)).read_text(encoding="utf-8")
    assert _both_halves(emitted) == (
        RING_CAPTURE_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
    # Byte-for-byte the graph the default resolution would have produced, so a
    # deploy over a box already on its endpoint changes nothing at all.
    expected, _ = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        preference_filters=_preference_filters(profile_path),
        playback_device=None,
    )
    assert emitted == expected


def _preference_filters(profile_path: Path):
    from jasper.sound.profile import build_sound_filters, load_profile

    return build_sound_filters(load_profile(profile_path))


# --------------------------------------------------------------------------
# 3. #2337 — the /sound/ + /eq/ save.
# --------------------------------------------------------------------------


async def test_a_sound_save_preserves_the_boxs_endpoint(tmp_path, monkeypatch):
    """A household EQ save changes the EQ, never the transport.

    ``/eq/`` and ``/sound/setup/`` both land on ``load_profile_config``, which
    resolves the same active carrier; the save must fold the new preference
    filters into the graph the box is running WITHOUT moving which lane it runs
    on. Pre-fix an armed box was silently disarmed by a taste-EQ save (#2337).

    #2285 P2 collapsed the armed/unarmed pair. The unarmed leg passed
    ``playback_device=None`` and expected the snd-aloop halves; that resolution
    now answers the ring, so the leg had become a byte-for-byte duplicate of the
    armed one. Keeping it would have looked like two transports were still being
    distinguished when only one is left.
    """
    from jasper.sound.runtime import load_profile_config

    topology, applied = _applied_box(tmp_path, monkeypatch)
    loaded_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    _point_statefile_at(tmp_path, monkeypatch, loaded_graph, name="loaded.yml")

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    current = config_dir / "sound_current.yml"
    current.write_text(loaded_graph, encoding="utf-8")
    camilla = _FakeCamilla(str(current))

    profile_path = tmp_path / "sound_profile.json"
    profile = SoundProfile(simple_eq=SimpleEq(treble_db=4.5))

    apply_state, out_path, _ = await load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: camilla,
        source="sound_apply",
        persist_profile=True,
    )

    assert apply_state.result == "success", apply_state.to_dict()
    emitted = Path(out_path).read_text(encoding="utf-8")
    # The save did its job...
    assert "sound_simple_treble" in emitted
    # ...without moving the box.
    assert _both_halves(emitted) == (RING_CAPTURE_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)


# --------------------------------------------------------------------------
# 4. The drift check that binds Layer A to the applied snapshot.
# --------------------------------------------------------------------------


async def test_an_armed_box_is_not_reported_as_a_layer_a_drift(
    tmp_path, monkeypatch,
):
    """Arming a box is not crossover drift, and must not block room correction.

    ``active_layer_a_fingerprint`` binds ``output_devices``, so the endpoint and
    the sink's whole CamillaDSP geometry ride in it. With the expectation built
    from the snapshot's lane, an armed box reported
    ``active_applied_profile_graph_mismatch`` — "Apply that crossover again
    before Room correction" — for a transport move nobody asked about. Whether
    the graph names the RIGHT transport is judged by ``check_fanin_coupling`` and
    ``ring_edge_width_ready`` against the marker and the ring's declaring ends;
    Layer A answers for crossover and protection.
    """
    from jasper.active_speaker.setup_status import _applied_layer_a_binding

    topology, applied = _applied_box(tmp_path, monkeypatch)
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    # No statefile is staged on purpose: this check reads the graph it was
    # HANDED, and nothing else.

    binding = _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=ring_graph,
    )
    assert binding["status"] == "current", binding
    assert binding["matches"] is True

    # POSITIVE CONTROL — the check is not simply inert now. A graph whose
    # driver-domain Layer A really did drift still reports mismatch. The
    # crossover corner is chosen deliberately: it is a REFERENCED filter in the
    # post-split suffix, so it is inside the projection the fingerprint binds
    # (the program-domain headroom gain is not, by design).
    drifted = ring_graph.replace("freq: 2500.0000", "freq: 2200.0000", 1)
    assert drifted != ring_graph
    drifted_binding = _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=drifted,
    )
    assert drifted_binding["status"] == "mismatch", drifted_binding


async def test_a_forbidden_playback_lane_is_unverifiable_never_a_traceback(
    tmp_path, monkeypatch,
):
    """An illegal lane in the compared graph is INDETERMINATE, not an exception.

    Passing the compared graph's device into the recomposer put that string in
    front of the emitter's own legality guard for the first time — before this
    change the argument was never supplied, so the path did not exist. A graph
    naming a forbidden lane (``jts_ring_playback``, reachable via a stale
    statefile or the flat-ring cutover class) therefore makes the emitter refuse
    with :class:`ActiveSpeakerConfigError`, and this function is what
    ``/correction/`` asks before offering room correction to a household: it must
    answer with the same ``unavailable`` snapshot it returns for every other
    input it cannot verify, never a traceback.

    Pinned rather than argued: the guarantee rides on
    ``_READINESS_DERIVATION_ERRORS``, and narrowing that tuple is the one edit
    that reopens it. Both halves are asserted here — the legal ring endpoint
    still produces the neutralized comparison, and the forbidden one degrades.
    """
    from jasper.active_speaker.setup_status import _applied_layer_a_binding
    from jasper.fanin_coupling import RING_PLAYBACK_DEVICE

    topology, applied = _applied_box(tmp_path, monkeypatch)

    # Half 1 — the legal ring endpoint: the neutralized comparison still works.
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    assert _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=ring_graph,
    )["status"] == "current"

    # Half 2 — the FORBIDDEN stereo ring in the loaded graph. The emitter really
    # does refuse this device, so the refusal is being routed, not hypothesised.
    with pytest.raises(ActiveSpeakerConfigError):
        recompose_applied_baseline_yaml(
            topology,
            applied_profile=applied,
            playback_device=RING_PLAYBACK_DEVICE,
        )

    forbidden_graph = ring_graph.replace(
        RING_ACTIVE_PLAYBACK_DEVICE, RING_PLAYBACK_DEVICE
    )
    assert RING_PLAYBACK_DEVICE in forbidden_graph
    binding = _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=forbidden_graph,
    )
    assert binding["status"] == "unverifiable", binding
    assert binding["matches"] is False
    assert binding["expected_fingerprint"] is None


async def test_the_drift_check_reads_the_endpoint_out_of_a_round_tripped_readback(
    tmp_path, monkeypatch,
):
    """The real caller passes CamillaDSP's OWN serialization, not our bytes.

    ``/correction/``'s readiness read hands this check
    ``cam.get_active_config_raw()`` — the running graph as CamillaDSP re-emits
    it, which drops our comments and re-renders the scalars. The endpoint is
    read out of that text with the shared device-subset reader, so this pins
    that the reader survives a round trip: if it did not, the armed box would
    quietly fall back to the snapshot's lane and stay blocked, which is a
    failure mode that looks exactly like no fix at all.
    """
    import yaml as yaml_parser

    from jasper.active_speaker.setup_status import _applied_layer_a_binding

    topology, applied = _applied_box(tmp_path, monkeypatch)
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    readback = yaml_parser.safe_dump(
        yaml_parser.safe_load(ring_graph), sort_keys=False
    )
    assert "# Source:" not in readback, "the round trip should drop our comments"
    assert (
        parse_camilla_devices_config(readback).get("playback_device")
        == RING_ACTIVE_PLAYBACK_DEVICE
    )

    binding = _applied_layer_a_binding(
        topology,
        applied_profile=applied,
        active_config_path=None,
        active_config_text=readback,
    )
    assert binding["status"] == "current", binding


# --------------------------------------------------------------------------
# 5. The seams that must route through the one derivation.
# --------------------------------------------------------------------------


# Callers of ``recompose_applied_baseline_yaml`` that deliberately do NOT name an
# endpoint, each with the reason it is allowed to inherit the snapshot's lane.
# The walking guard below holds every OTHER call site to naming one, and fails on
# a stale entry too — an exemption that no longer matches a real call site is a
# rule protecting nothing.
_ENDPOINT_EXEMPT_CALL_SITES = {
    # Commissioning-time identity derivations, exempt because the endpoint
    # CANCELS: each re-emit is FINGERPRINTED (``NormalizedActiveRawIdentity``,
    # which does freeze the devices block) and compared against a fingerprint a
    # PLANNING step recorded from the same snapshot-default recompose, so both
    # ends move together — while feeding the live endpoint to one end and not to
    # the stored other would invalidate every fingerprint already on disk.
    # Changing these is a separate decision about what commissioning evidence
    # identity should bind, not this fix.
    #
    # The isolated producer is fingerprint-ONLY: its ``normal_raw`` is loaded to
    # compute ``active_raw_fingerprint`` and the text is then discarded.
    "jasper/active_speaker/commissioning_isolated_producer.py",
    # ``jasper/active_speaker/commissioning_host.py`` USED to sit here (#2362):
    # its ``normal_active_raw`` was fingerprinted AND rode into
    # ``SummedGraphRequest`` -> ``commissioning_runtime`` -> ``port.apply_active_raw``
    # -> ``camilla.set_active_config_raw``, so the snapshot's ``devices:`` block
    # would have reached a live CamillaDSP verbatim. The exemption rested on the
    # chain being unwired. That seam is now DELETED rather than derived: the host
    # no longer recomposes a baseline at all, so it is no longer a call site and
    # the walk below holds nothing back for it.
    # ``jasper/active_speaker/web_commissioning.py`` USED to sit here (#2344): it
    # WRITES the automatic-summed measurement graph and loads it into CamillaDSP
    # to play the excitation, so inheriting the snapshot's lane swept a ring-armed
    # box into the snd-aloop lane and measured silence. It now reads the same
    # derivation as the other loaded-graph seams and is held by the walk below.
}


async def test_every_recompose_call_site_names_the_endpoint_or_is_exempt():
    """A WALKING guard over the CALL SITES, so a fourth seam cannot arrive quietly.

    The behavioural tests above each pin one seam; this pins the SET, and it
    DISCOVERS the set rather than restating it — every
    ``recompose_applied_baseline_yaml(...)`` call in ``jasper/`` is found by
    parsing, not by a hand-written list, so a new caller that forgets the
    endpoint and silently inherits the applied snapshot's lane fails here.
    """
    import ast

    repo = Path(__file__).resolve().parent.parent
    found: set[str] = set()
    missing: list[str] = []
    for path in sorted((repo / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "recompose_applied_baseline_yaml":
                continue
            rel = str(path.relative_to(repo))
            found.add(rel)
            if any(kw.arg == "playback_device" for kw in node.keywords):
                continue
            if rel in _ENDPOINT_EXEMPT_CALL_SITES:
                continue
            missing.append(f"{rel}:{node.lineno}")

    assert found, "no recompose call sites found — this guard has gone vacuous"
    assert not missing, (
        "these rebuild a roleful box's active graph without naming an endpoint, "
        "so they inherit the applied snapshot's lane and move an armed speaker "
        f"off the ring (#2339/#2337): {missing}"
    )
    stale = _ENDPOINT_EXEMPT_CALL_SITES - found
    assert not stale, f"exemptions that no longer name a real call site: {stale}"


async def test_the_drift_check_neutralizes_the_transport_axis(tmp_path, monkeypatch):
    """The Layer-A expectation is built against the endpoint of the graph it is
    COMPARED to — the caller's readback — not against the box's statefile.

    Two-way comparison: snapshot evidence vs a caller-supplied readback. Taking
    the endpoint from the statefile instead would add a third opinion and make a
    box whose device resolution merely drifted from its snapshot report
    *crossover* drift, which is a different and much louder claim.
    """
    from unittest import mock

    topology, applied = _applied_box(tmp_path, monkeypatch)
    ring_graph = _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE)
    # The statefile deliberately disagrees with the readback, so a statefile-fed
    # expectation would be visible here.
    _point_statefile_at(
        tmp_path, monkeypatch, _graph_for(topology, applied, None), name="other.yml"
    )
    spy = mock.Mock(return_value=(None, []))

    with mock.patch(
        "jasper.active_speaker.setup_status.recompose_applied_baseline_yaml", spy
    ):
        from jasper.active_speaker.setup_status import _applied_layer_a_binding

        _applied_layer_a_binding(
            topology,
            applied_profile=applied,
            active_config_path=None,
            active_config_text=ring_graph,
        )

    assert spy.call_args is not None, "the drift check never reached the recomposer"
    assert (
        spy.call_args.kwargs.get("playback_device") == RING_ACTIVE_PLAYBACK_DEVICE
    )


@pytest.mark.parametrize("seam", ["sound_carrier", "bass_extension"])
async def test_the_re_emit_seams_forward_the_derived_endpoint(
    tmp_path, monkeypatch, seam,
):
    """The derived value REACHES the recomposer, not merely gets computed.

    Naming the kwarg and calling the derivation are two different facts: a seam
    that resolves the endpoint and then drops it on the floor reads as fixed to
    any source-level check while behaving exactly like the defect. A sentinel
    device is threaded through the real seam and asserted at the recomposer's
    own call boundary.
    """
    from unittest import mock

    from jasper.sound.graph_carrier import CarrierCannotHostEq

    topology, applied = _applied_box(tmp_path, monkeypatch)
    sentinel_device = "jts_sentinel_endpoint"
    spy = mock.Mock(return_value=(None, []))
    endpoint_stub = mock.Mock(return_value=(sentinel_device, LOADED_GRAPH_SOURCE))

    with mock.patch(
        "jasper.active_speaker.baseline_profile.recompose_applied_baseline_yaml",
        spy,
    ), mock.patch(
        "jasper.active_speaker.playback_route.resolve_live_active_endpoint",
        endpoint_stub,
    ):
        if seam == "sound_carrier":
            from jasper.sound.graph_carrier import _recompose_active_baseline_with_eq

            with pytest.raises(CarrierCannotHostEq):
                _recompose_active_baseline_with_eq(
                    SoundProfile(enabled=False), out_path=None
                )
        elif seam == "bass_extension":
            from jasper.sound.graph_carrier import (
                recompose_active_baseline_for_bass_extension,
            )

            selected = tmp_path / "selected.yml"
            selected.write_text(
                _graph_for(topology, applied, None), encoding="utf-8"
            )
            preference_path = tmp_path / "pref.json"
            preference_path.write_text(
                json.dumps(SoundProfile(enabled=False).to_dict()), encoding="utf-8"
            )
            settings_path = tmp_path / "sound-settings.json"
            settings_path.write_text("{}", encoding="utf-8")
            with pytest.raises(CarrierCannotHostEq):
                recompose_active_baseline_for_bass_extension(
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
# 6. #2344 — the COMMISSIONING WIZARD's two graphs on an armed box.
#
# The wizard's measurement flows were the one armed-box restriction #2343 did
# not lift, and they break in two DIFFERENT ways:
#
#   * the applied-summed measurement graph re-emits the snapshot and LOADS it to
#     play the excitation — a fifth member of the family above, fixed the same
#     way: it reads the one derivation;
#   * the per-driver / summed COMMISSIONING graph resolves the ring by NAME
#     through the fresh-emit chooser but forwards none of the REST of the device
#     contract, so it emits a ring sink over a ``plug:jasper_capture`` source.
#     That one REFUSES on an armed box instead of being taught the ring, and
#     that refusal is PERMANENT rather than provisional — the reason lives with
#     the guard in ``staging.prepare_driver_commissioning_config``, not here.
#
# Both halves keep the same promise — never silently measure a device nobody
# writes. One keeps it by emitting the right graph, the other by emitting none.
# --------------------------------------------------------------------------


def _stub_camilla_validation(monkeypatch):
    """Accept any emitted graph — CamillaDSP's own syntax check needs a binary."""
    from types import SimpleNamespace

    from jasper import dsp_apply

    monkeypatch.setattr(
        dsp_apply,
        "validate_camilla_config",
        lambda path: SimpleNamespace(
            ok_to_apply=True, to_dict=lambda: {"status": "valid", "path": str(path)}
        ),
    )


async def test_the_measurement_sweep_graph_follows_the_live_endpoint(
    tmp_path, monkeypatch,
):
    """An ARMED box's sweep excites the RING, on BOTH halves of the graph.

    This is the seam #2344 records. It re-emits the applied Layer-A graph and
    hands it to CamillaDSP to play the excitation, so an inherited snapshot lane
    drove the snd-aloop tap that fan-in stops feeding under ``shm_ring`` — an
    excitation into a device nobody reads, recorded as silence with every daemon
    healthy.

    The CAPTURE half is asserted alongside the playback half deliberately: a
    sink-only move is the half-moved graph, which fails in the same silent way.
    """
    from jasper.active_speaker import web_commissioning

    topology, applied = _applied_box(tmp_path, monkeypatch)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
        name="loaded.yml",
    )
    target = tmp_path / "summed_measurement.yml"
    monkeypatch.setenv(web_commissioning.AUTOMATIC_SUMMED_CONFIG_PATH_ENV, str(target))
    _stub_camilla_validation(monkeypatch)
    camilla = _FakeCamilla(str(tmp_path / "normal.yml"))

    payload = await web_commissioning._load_applied_summed_measurement_config(
        topology=topology,
        camilla_factory=lambda: camilla,
    )

    assert payload["load"]["status"] == "loaded", payload
    assert camilla.loaded_path == str(target)
    assert _both_halves(target.read_text(encoding="utf-8")) == (
        RING_CAPTURE_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )


async def test_an_unarmed_box_sweeps_the_byte_identical_graph(tmp_path, monkeypatch):
    """The unarmed fleet sees NO change — asserted byte-for-byte, not argued.

    The family's whole safety case is that deriving the endpoint is a no-op
    wherever the box is not armed, so this compares the emitted FILE against the
    snapshot-default recompose rather than against a device name.
    """
    from jasper.active_speaker import web_commissioning

    topology, applied = _applied_box(tmp_path, monkeypatch)
    _point_statefile_at(
        tmp_path, monkeypatch, _graph_for(topology, applied, None), name="loaded.yml"
    )
    target = tmp_path / "summed_measurement.yml"
    monkeypatch.setenv(web_commissioning.AUTOMATIC_SUMMED_CONFIG_PATH_ENV, str(target))
    _stub_camilla_validation(monkeypatch)

    payload = await web_commissioning._load_applied_summed_measurement_config(
        topology=topology,
        camilla_factory=lambda: _FakeCamilla(str(tmp_path / "normal.yml")),
    )

    assert payload["load"]["status"] == "loaded", payload
    expected, issues = recompose_applied_baseline_yaml(
        topology, applied_profile=applied, playback_device=None
    )
    assert issues == [], issues
    assert target.read_text(encoding="utf-8") == expected


def _commissioning_box():
    """The roleful bench shape the commissioning emitter is driven with."""
    from tests.test_ring_active_endpoint import _active_topology, _mono_two_way_preset

    return _active_topology("mono", "active_2_way"), _mono_two_way_preset()


def _recorded_commissioning_emit(monkeypatch):
    """Record every call the commissioning emitter receives, and pass it through."""
    from jasper.active_speaker import camilla_yaml, staging

    calls: list[dict] = []
    real = camilla_yaml.emit_active_speaker_commissioning_config

    def recording(*args, **kwargs):
        calls.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(
        staging, "emit_active_speaker_commissioning_config", recording
    )
    return calls


@pytest.mark.parametrize("route", ["marker", "explicit"])
async def test_driver_commissioning_emits_a_coherent_graph_on_the_active_ring(
    tmp_path, monkeypatch, route,
):
    """THE CAPABILITY (#2412 Wave 3): a ring box commissions, and both ends agree.

    This test asserted the opposite until Wave 3. The gate here used to be
    ``resolved_playback_device not in RING_PCM_DEVICES`` — a refusal of the ring
    outright, shipped by #2344 on the owner's 2026-08-12 #2254 ruling and
    superseded by the owner's re-opening in #2412 — and the refusal was correct
    for the emitter as it then stood: ``resolve_active_playback_device`` is
    ring-aware, so the emit resolved the ring by NAME while forwarding none of
    the rest of ``active_emit_devices``. The emitted graph had a ring sink over
    ``plug:jasper_capture``, the tap fan-in stops feeding under ``shm_ring``, and
    the sweep would have excited a device nobody reads.

    Wave 1 closed the forwarding half, so the graph a ring box emits is now
    coherent end to end, and the gate proves that rather than refusing the
    transport. What replaces "nothing was emitted" as the safety assertion is
    the PAIR: the sink is the ring the caller asked for AND the source is Ring A,
    the device fan-in fills under that same coupling.

    Two routes to the ACTIVE ring — the production marker and an explicit lab
    override — because the marker route is what a fleet box takes and the
    explicit route is what a bench does. The third member of
    ``RING_PCM_DEVICES`` has its own test below: it is refused before the gate
    by a guard that is not this one.
    """
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    explicit_device = {
        "marker": None,
        "explicit": RING_ACTIVE_PLAYBACK_DEVICE,
    }[route]
    expected_device = RING_ACTIVE_PLAYBACK_DEVICE
    topology, preset = _commissioning_box()
    emits = _recorded_commissioning_emit(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: route == "marker",
    )

    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=explicit_device,
        config_dir=tmp_path / route,
        run_config_check=False,
    )

    assert payload["status"] == "prepared", payload
    assert len(emits) == 1, emits
    assert emits[0]["playback_device"] == expected_device
    assert emits[0]["capture_device"] == RING_CAPTURE_DEVICE
    gate = next(
        g
        for g in payload.get("required_gates") or []
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is True, gate
    # THE RETIRED RUNG IS ASSERTED ABSENT. Asserting the new contract alone
    # would pass over a partial re-point that left the old refusal reachable.
    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_ring_transport_unsupported" not in codes, payload
    assert not [
        i for i in payload.get("issues") or [] if i.get("severity") == "blocker"
    ], payload
    # And the artifact ON DISK carries the pair, because that is what the gate
    # re-reads and what CamillaDSP will open.
    written = Path(payload["config"]["path"]).read_text(encoding="utf-8")
    devices = parse_camilla_devices_config(written)
    assert devices.get("playback_device") == expected_device, written
    assert devices.get("capture_device") == RING_CAPTURE_DEVICE, written


async def test_the_stereo_ring_is_refused_by_the_emitter_not_by_the_transport_gate(
    tmp_path, monkeypatch,
):
    """The third ring PCM, and the guard that actually owns it.

    The refusal this wave lifts was keyed on set membership over
    ``RING_PCM_DEVICES``, and the parametrised test above used to drive a SECOND
    member through it to prove the guard was not keyed on one name. That
    property did not disappear with the refusal — it moved to the derivation:
    ``capture_device_for_playback`` answers Ring A for EVERY member of the set,
    so the pair a ring emit declares is coherent whichever member it names.

    What refuses the stereo ring is a different, pre-existing guard — the
    emitter's own forbidden-active-sink token — and it fires before any graph is
    written. Both halves are ASSERTED, not narrated: the retired parametrised
    case pinned the ordering with ``emits == []`` under a docstring explaining
    that a blocker which still wrote a candidate would leave a half-moved graph
    on disk for the next reader, and that pin travels here as the no-artifact
    assertion below. A docstring making the construction argument while claiming
    to be the guard is the anti-pattern this design quotes at itself.
    """
    from jasper.active_speaker.camilla_yaml import capture_device_for_playback
    from jasper.active_speaker.staging import prepare_driver_commissioning_config
    from jasper.fanin_coupling import RING_PCM_DEVICES, RING_PLAYBACK_DEVICE

    assert RING_PLAYBACK_DEVICE in RING_PCM_DEVICES
    assert RING_PLAYBACK_DEVICE != RING_ACTIVE_PLAYBACK_DEVICE
    for member in RING_PCM_DEVICES:
        assert capture_device_for_playback(member) == RING_CAPTURE_DEVICE, member

    topology, preset = _commissioning_box()
    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=RING_PLAYBACK_DEVICE,
        config_dir=tmp_path / "stereo_ring",
        run_config_check=False,
    )

    assert payload["status"] == "blocked", payload
    # THE ORDERING, ASSERTED: the guard fires before the write, so a refused
    # prepare leaves no half-moved graph behind. Inherited from the retired
    # parametrised case's `emits == []`, expressed here on the artifact rather
    # than on the emitter call, because the failure this bounds is a file the
    # next reader would find. Discriminating, not vacuous: the same field reads
    # True on the coherent-graph tests above, which do prepare.
    assert payload["config"]["exists"] is False, payload["config"]
    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_config_generation_failed" in codes, payload
    # Not this wave's gates, and not the retired rung: the emitter owns this one.
    assert "commissioning_transport_ends_disagree" not in codes, payload
    assert "commissioning_ring_transport_unsupported" not in codes, payload


async def test_a_half_forwarded_device_block_is_refused_by_the_transport_gate(
    tmp_path, monkeypatch,
):
    """THE PIN the lifted gate exists for: six of seven fields is still a defect.

    Wave 1 made the device block a derivation, and
    ``tests/test_ring_active_endpoint.py::test_every_emit_devices_field_reaches_the_emitter``
    walks ``dataclasses.fields(ActiveEmitDevices)`` at every forwarding site so a
    field added there cannot be dropped by one of them. That walk reads the
    kwargs the CALL SITE hands the emitter. This reads the graph that came OUT.

    Neither implies the other, which is why both are required: the walk cannot
    see a field that is forwarded and then lost between the call site and the
    file, and this cannot see a field that never affects the two device names.
    The mutation below is deliberately of the shape the walk is blind to — the
    call site still names every field, and the capture is dropped downstream —
    so the two guards are demonstrably not one guard twice.

    The refusal is at the gate, not before the write. The old predicate could
    refuse ahead of the emit because it only had to look at a device name; a
    re-read proof cannot prove anything about a file that was never written. The
    property that replaces "nothing was written" is that nothing LOADS it: the
    blocker fails ``status``, which fails the load preflight's ``prepared`` gate,
    and the preflight re-runs this builder rather than trusting a candidate on
    disk.
    """
    from jasper.active_speaker import staging as staging_mod
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    real = staging_mod.emit_active_speaker_commissioning_config

    def half_forwarding(preset_arg, **kwargs):
        # The pre-Wave-1 defect, reproduced downstream of the call site: the
        # sink stays the ring, the capture falls back to the emitter's snd-aloop
        # tap default. Under `shm_ring` fan-in stops feeding that tap.
        kwargs.pop("capture_device", None)
        return real(preset_arg, **kwargs)

    monkeypatch.setattr(
        staging_mod, "emit_active_speaker_commissioning_config", half_forwarding
    )

    topology, preset = _commissioning_box()
    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        config_dir=tmp_path / "half",
        run_config_check=False,
    )

    assert payload["status"] == "blocked", payload
    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_transport_ends_disagree" in codes, payload
    gate = next(
        g
        for g in payload["required_gates"]
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is False, gate
    # The graph that reached disk really is the silent-sweep pair — without this
    # the assertions above would also pass if the mutation had broken the emit.
    devices = parse_camilla_devices_config(
        Path(payload["config"]["path"]).read_text(encoding="utf-8")
    )
    assert devices.get("playback_device") == RING_ACTIVE_PLAYBACK_DEVICE
    assert devices.get("capture_device") != RING_CAPTURE_DEVICE
    # CONTROL: the same call with the mutation LIFTED prepares. Without it, a
    # gate that refused every ring box would satisfy every assertion above, and
    # the refusal would not be attributable to the half-forward.
    monkeypatch.undo()
    unmutated = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        config_dir=tmp_path / "unmutated",
        run_config_check=False,
    )
    assert unmutated["status"] == "prepared", unmutated


async def test_the_transport_gate_invents_no_failure_when_no_graph_was_emitted(
    tmp_path,
):
    """A gate about a graph that does not exist must not refuse the transport.

    The proof is a re-read, so when an earlier blocker stops the emit there are
    no ends to disagree. Reporting a transport failure there would misdiagnose a
    box no owner refused — the same rule the load preflight's mirror follows for
    an absent gate. Pinned in both directions: the gate passes, AND the vacuous
    pass cannot make the box loadable, because the earlier blocker still fails
    ``status`` and therefore the preflight's ``prepared``.
    """
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    topology, preset = _commissioning_box()
    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="nosuchrole",
        preset=preset,
        config_dir=tmp_path / "norole",
        run_config_check=False,
    )

    assert payload["status"] == "blocked", payload
    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_target_role_unknown" in codes, payload
    assert "commissioning_transport_ends_disagree" not in codes, payload
    gate = next(
        g
        for g in payload["required_gates"]
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is True, gate


async def test_driver_commissioning_still_emits_on_an_unarmed_box(
    tmp_path, monkeypatch,
):
    """CONTROL: the refusal is keyed on the graph's coherence, not on the marker.

    Without this, a gate that refused every box would satisfy the assertions
    above. The unarmed path must still reach the emitter — and since #2285 P2 it
    reaches it naming the ACTIVE RING, because ``resolve_output_layout`` case 2
    no longer reads the endpoint marker to choose a transport.

    THIS TEST CARRIED AN ``xfail(reason="#2412")`` AND NO LONGER NEEDS ONE. It
    was marked when the emit was genuinely unreachable: before #2412's waves an
    unarmed roleful box's commissioning emit named
    ``outputd_active_content_playback``, a PCM whose definition #2534 had
    deleted (positive control: ``pcm.outputd_content_playback`` IS still found
    in ``deploy/alsa/asoundrc.jasper``), so the device the "control" proved
    reachable was never openable. #2412 Waves 1-3 made ring commissioning work
    and P2 makes the chooser name the ring, so the emit is now both reachable
    and coherent. The marker is REMOVED rather than left non-strict — sealed
    §3.4 rule 3 admits zero xfails, and a passing test wearing an xfail hides
    the very repair it should be reporting (post-seal correction 9).
    """
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    topology, preset = _commissioning_box()
    emits = _recorded_commissioning_emit(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )

    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        config_dir=tmp_path / "unarmed",
        run_config_check=False,
    )

    assert payload["status"] == "prepared", payload
    assert len(emits) == 1, emits
    # The RING, on an UNARMED box — that is the post-P2 meaning of this control.
    # The marker is false above and the emit still names the ring, which is the
    # observable consequence of deleting case 2's marker read.
    assert emits[0]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    gate = next(
        g
        for g in payload["required_gates"]
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is True


# --------------------------------------------------------------------------
# GATE 2 — THE ARMED-TRANSPORT GATE AT THE LOAD ALTITUDE (#2412 Wave 3).
#
# Gate 1 (above) proves the emitted graph's two ends name one transport. It is a
# PURE BUILDER and reads no daemon env, so it proves COHERENCE and not LIVENESS:
# a ring/ring graph on a box whose fan-in is still loopback-coupled, or whose
# outputd endpoint was never armed, is self-consistent and passes it. That graph
# loads cleanly and plays to nobody.
#
# The two conjuncts have two OWNERS — the coupling lives in `fanin.env`, the
# ACTIVE-endpoint marker in `outputd.env`, one reconciler each — so each is
# refuted by a scenario that isolates it, with the other one armed. A crossed
# mutant-to-test mapping is how a campaign invents a survival; the scenario
# names below and the codes they assert are written to read the same way round.
# --------------------------------------------------------------------------


def _ring_transport_state(monkeypatch, tmp_path, *, coupling: str, marker: str):
    """Point BOTH reconciler-owned files at ``tmp_path`` and write the state.

    Real files rather than stubbed predicates: the gate's contract is that it
    reads each file FRESH on every call, and a monkeypatched predicate cannot
    fail that way. Both module constants are imported inside the reader
    functions, so rebinding them here redirects the real read.
    """
    from jasper.fanin_coupling import (
        COUPLING_ENV_VAR,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
    )

    fanin_env = Path(tmp_path) / "fanin.env"
    outputd_env = Path(tmp_path) / "outputd.env"
    fanin_env.write_text(f"{COUPLING_ENV_VAR}={coupling}\n", encoding="utf-8")
    outputd_env.write_text(
        f"{OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR}={marker}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    return fanin_env, outputd_env


def _ring_load_preflight(topology, preset, out_dir):
    from jasper.active_speaker.startup_load import (
        build_driver_commission_load_preflight,
    )

    return build_driver_commission_load_preflight(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        config_dir=out_dir,
        require_physical_identity=False,
    )


def _transport_armed_gate(preflight):
    return next(
        g
        for g in preflight["required_gates"]
        if g.get("id") == "commissioning_transport_armed"
    )


async def test_the_guarded_load_refuses_a_ring_graph_nothing_fills(
    tmp_path, monkeypatch,
):
    """COUPLING conjunct, isolated: the endpoint IS armed, fan-in is not coupled.

    Under `loopback` fan-in writes the snd-aloop substream and nothing fills
    Ring A, so a ring-capture graph sweeps into digital silence with every
    daemon healthy. The marker is armed here on purpose: only the coupling term
    can produce this refusal, so a mutation of the OTHER term cannot be scored
    against this test.
    """
    topology, preset = _commissioning_box()
    _ring_transport_state(monkeypatch, tmp_path, coupling="loopback", marker="1")

    preflight = _ring_load_preflight(topology, preset, tmp_path / "unfed")

    assert preflight["load_allowed"] is False
    assert _transport_armed_gate(preflight)["passed"] is False
    codes = {issue.get("code") for issue in preflight["issues"]}
    assert "commissioning_ring_feed_unarmed" in codes, preflight["issues"]
    assert "commissioning_active_endpoint_unarmed" not in codes, preflight["issues"]
    assert "commissioning_ring_transport_unsupported" not in codes, preflight["issues"]
    refusal = next(
        issue
        for issue in preflight["issues"]
        if issue.get("code") == "commissioning_ring_feed_unarmed"
    )
    assert refusal["severity"] == "blocker"
    # The OPERATOR surface names the executable remedy; the household surfaces
    # never do, which is what the copy guards in tests/test_sound_setup.py pin.
    assert "jasper-fanin-coupling-reconcile shm_ring" in refusal["message"]


async def test_the_guarded_load_refuses_a_ring_graph_nothing_reads(
    tmp_path, monkeypatch,
):
    """MARKER conjunct, isolated: fan-in IS coupled, the endpoint is not armed.

    Post-arm the graph names the ring unconditionally; the marker is what says
    whether outputd reads it. Without this conjunct a ring-sink graph on an
    unarmed box loads cleanly and plays to nobody. The coupling is armed here on
    purpose, for the same isolation reason as its sibling above.
    """
    topology, preset = _commissioning_box()
    _ring_transport_state(monkeypatch, tmp_path, coupling="shm_ring", marker="0")

    preflight = _ring_load_preflight(topology, preset, tmp_path / "unread")

    assert preflight["load_allowed"] is False
    assert _transport_armed_gate(preflight)["passed"] is False
    codes = {issue.get("code") for issue in preflight["issues"]}
    assert "commissioning_active_endpoint_unarmed" in codes, preflight["issues"]
    assert "commissioning_ring_feed_unarmed" not in codes, preflight["issues"]
    assert "commissioning_ring_transport_unsupported" not in codes, preflight["issues"]
    refusal = next(
        issue
        for issue in preflight["issues"]
        if issue.get("code") == "commissioning_active_endpoint_unarmed"
    )
    assert refusal["severity"] == "blocker"
    assert "jasper-audio-hardware-reconcile" in refusal["message"]


async def test_the_guarded_load_admits_a_ring_graph_on_a_fully_armed_box(
    tmp_path, monkeypatch,
):
    """THE CAPABILITY: both conjuncts hold, so the transport stops blocking.

    The positive control for both tests above — without it, a gate that refused
    every ring box would satisfy each of their assertions. This asserts the two
    TRANSPORT gates and the absence of every transport blocker rather than
    `load_allowed`, because a bench topology has no path-safety evidence and no
    calibration floor and is blocked on those for reasons this wave does not
    touch.
    """
    topology, preset = _commissioning_box()
    _ring_transport_state(monkeypatch, tmp_path, coupling="shm_ring", marker="1")

    preflight = _ring_load_preflight(topology, preset, tmp_path / "armed")

    assert _transport_armed_gate(preflight)["passed"] is True, preflight[
        "required_gates"
    ]
    mirrored = next(
        g
        for g in preflight["required_gates"]
        if g.get("id") == "commissioning_transport_supported"
    )
    assert mirrored["passed"] is True, mirrored
    codes = {issue.get("code") for issue in preflight["issues"]}
    assert "commissioning_ring_feed_unarmed" not in codes, preflight["issues"]
    assert "commissioning_active_endpoint_unarmed" not in codes, preflight["issues"]
    assert "commissioning_transport_ends_disagree" not in codes, preflight["issues"]
    assert "commissioning_ring_transport_unsupported" not in codes, preflight["issues"]


async def test_the_guarded_load_reads_no_transport_state_off_the_ring(
    tmp_path, monkeypatch,
):
    """SCOPE: a non-ring graph needs no ring armed, and consults neither file.

    Both files are pointed at paths that do not exist, so both readers would
    fail SAFE — loopback, marker false — and refuse if they were consulted at
    all. The gate passes, which is the proof that an unarmed fleet box on the
    ALSA active lane behaves exactly as it did before this wave.
    """
    from jasper.active_speaker.startup_load import (
        build_driver_commission_load_preflight,
    )

    topology, preset = _commissioning_box()
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH",
        str(tmp_path / "absent" / "fanin.env"),
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "absent" / "outputd.env"),
    )

    preflight = build_driver_commission_load_preflight(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        config_dir=tmp_path / "alsa",
        require_physical_identity=False,
    )

    assert _transport_armed_gate(preflight)["passed"] is True
    codes = {issue.get("code") for issue in preflight["issues"]}
    assert "commissioning_ring_feed_unarmed" not in codes, preflight["issues"]
    assert "commissioning_active_endpoint_unarmed" not in codes, preflight["issues"]


@pytest.mark.parametrize("corrupt", ["fanin", "outputd"])
async def test_the_guarded_load_refuses_a_ring_graph_whose_transport_state_is_corrupt(
    tmp_path, monkeypatch, corrupt,
):
    """A non-UTF-8 reconciler file is an unarmed transport, not a traceback.

    Both readers fail-safe on ``OSError`` and normalise every malformed VALUE,
    so a missing file, an empty one, a typo and garbage keys all already resolve
    to unarmed. One input class escaped both: a non-UTF-8 byte raises
    ``UnicodeDecodeError`` — a ``ValueError``, not an ``OSError`` — and this
    preflight is the first caller to read either file, so the exception would
    leave it and take the blocker with it. On a Pi that is the ordinary shape of
    SD-card corruption or a write truncated by a power cut, and the suppressed
    blocker is the one naming the reconciler that REWRITES the corrupted file.

    Parametrised per file so a fix that guards only one read is caught by the
    other's case. Both conjuncts are asserted unarmed, which is the deliberate
    fail-closed direction: a decode failure says nothing about which file was
    bad, and both remedies are safe to run.
    """
    topology, preset = _commissioning_box()
    fanin_env, outputd_env = _ring_transport_state(
        monkeypatch, tmp_path, coupling="shm_ring", marker="1"
    )
    target = fanin_env if corrupt == "fanin" else outputd_env
    target.write_bytes(b"JASPER_FANIN_CAMILLA_COUPLING=\xff\xfeshm_ring\n")
    # CONTROL: the byte really is undecodable, so this test cannot pass because
    # the file happened to stay readable.
    with pytest.raises(UnicodeDecodeError):
        target.read_text(encoding="utf-8")

    preflight = _ring_load_preflight(topology, preset, tmp_path / f"corrupt-{corrupt}")

    assert preflight["load_allowed"] is False
    assert _transport_armed_gate(preflight)["passed"] is False
    codes = {issue.get("code") for issue in preflight["issues"]}
    assert "commissioning_ring_feed_unarmed" in codes, preflight["issues"]
    assert "commissioning_active_endpoint_unarmed" in codes, preflight["issues"]


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        pytest.param(
            ("loopback", "1"),
            ("shm_ring", "1"),
            "commissioning_ring_feed_unarmed",
            id="coupling",
        ),
        pytest.param(
            ("shm_ring", "0"),
            ("shm_ring", "1"),
            "commissioning_active_endpoint_unarmed",
            id="marker",
        ),
    ],
)
async def test_the_guarded_load_re_reads_the_transport_state_every_call(
    tmp_path, monkeypatch, first, second, code,
):
    """R-3: the state is read FRESH per call, never cached from the first one.

    The hazard this pins is the one the voice-provider reader learned the hard
    way and `ring_active_endpoint_armed`'s docstring documents: this preflight
    runs inside the long-lived control daemon and the socket-activated wizards,
    which never `EnvironmentFile=`d either file and stay alive across a
    reconcile. A reader that resolved once at import — or cached on first call —
    would keep refusing a box an operator had just armed.

    The file is mutated BETWEEN two calls in one process, once per conjunct, so
    a cache on either read is caught by its own case rather than by its sibling.
    """
    topology, preset = _commissioning_box()
    fanin_env, outputd_env = _ring_transport_state(
        monkeypatch, tmp_path, coupling=first[0], marker=first[1]
    )

    blocked = _ring_load_preflight(topology, preset, tmp_path / "before")
    assert code in {issue.get("code") for issue in blocked["issues"]}, blocked["issues"]
    assert _transport_armed_gate(blocked)["passed"] is False

    _ring_transport_state(monkeypatch, tmp_path, coupling=second[0], marker=second[1])
    assert fanin_env.exists() and outputd_env.exists()

    rearmed = _ring_load_preflight(topology, preset, tmp_path / "after")
    assert _transport_armed_gate(rearmed)["passed"] is True, rearmed["required_gates"]
    assert code not in {issue.get("code") for issue in rearmed["issues"]}, rearmed[
        "issues"
    ]


async def test_the_durable_boot_anchor_is_not_refused_on_an_armed_box(
    tmp_path, monkeypatch,
):
    """SCOPE: the refusal covers the AUDIBLE emit only, never the boot anchor.

    ``stage_protected_startup_config`` shares the same context builder but emits
    the all-muted durable startup graph — the crash-recovery anchor a roleful box
    boots from. Refusing THAT on an armed box would leave a speaker unable to
    refresh its own boot config, which is a worse failure than the one being
    fixed. This pins where the blocker lives, so a later tidy-up that lifts it
    into the shared context fails here instead of on a Pi.
    """
    from jasper.active_speaker.staging import stage_protected_startup_config

    topology, preset = _commissioning_box()
    emits = _recorded_commissioning_emit(monkeypatch)
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )

    payload = stage_protected_startup_config(
        topology,
        preset=preset,
        config_dir=tmp_path / "anchor",
        # Pinned to tmp_path like every other call site: the default is the real
        # `/var/lib/jasper/active_speaker_staged_config.json`, so omitting it
        # makes a Pi-side test run overwrite a live speaker's staged metadata.
        metadata_path=tmp_path / "anchor" / "staged_metadata.json",
        run_config_check=False,
    )

    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_ring_transport_unsupported" not in codes, payload
    assert len(emits) == 1, emits
    assert emits[0]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    # ...and it is not merely UNREFUSED, it is COHERENT (#2364). The anchor used
    # to name the ring while every other half of its device contract stayed at
    # the emitter's snd-aloop defaults; asserting only the sink is what let that
    # pass. The full-fidelity assertions live in the two tests below.
    assert emits[0]["capture_device"] == RING_CAPTURE_DEVICE


# --------------------------------------------------------------------------
# THE BOOT ANCHOR'S DEVICE BLOCK (#2364).
#
# `stage_protected_startup_config` forwarded only the device NAME, so a box
# re-staged at the ACTIVE ring got a ring sink over `plug:jasper_capture` — the
# tap fan-in STOPS feeding under `shm_ring` — with the program-lane format and
# the loopback chunk/target/queue geometry, in the artifact it BOOTS from.
# Nothing downstream inspected it: `build_startup_load_preflight`'s gates are
# about staging, identity, protection and level, never the transport.
#
# The fix routes the anchor through `active_emit_devices`, the SAME derivation
# `recompose_applied_baseline_yaml` reads. These two tests are the pair that
# makes that safe to believe: one proves the ring answer is right, the other
# proves nothing else moved.
# --------------------------------------------------------------------------


def _anchor_yaml(topology, preset, out_dir, device):
    """Stage the boot anchor at ``device`` and return the emitted YAML."""
    from jasper.active_speaker.staging import stage_protected_startup_config

    payload = stage_protected_startup_config(
        topology,
        preset=preset,
        playback_device=device,
        config_dir=out_dir,
        metadata_path=out_dir / "staged_metadata.json",
        run_config_check=False,
    )
    blockers = [i for i in payload.get("issues") or [] if i.get("severity") == "blocker"]
    assert payload["status"] == "staged" and not blockers, payload
    return Path(payload["config"]["path"]).read_text(encoding="utf-8")


async def test_boot_anchor_derives_the_ring_device_block(tmp_path):
    """At the ring, every half of the anchor's device contract is the ring's.

    Each value is asserted against its OWNER — `resolve_ring_wire` for the wire
    format, the `RING_CAMILLA_*` constants for the latency geometry — never a
    literal repeated here. A literal would pass just as happily against a graph
    that had drifted away from what fan-in actually declares, which is the exact
    failure this is meant to catch.
    """
    from jasper.active_speaker.camilla_yaml import active_emit_devices
    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
        resolve_ring_wire,
    )

    topology, preset = _commissioning_box()
    yaml = _anchor_yaml(
        topology, preset, tmp_path / "ring", RING_ACTIVE_PLAYBACK_DEVICE
    )

    wire = resolve_ring_wire(topology).sample_format
    # The derivation is the single owner; the emitted graph must agree with it.
    assert active_emit_devices(
        RING_ACTIVE_PLAYBACK_DEVICE, topology=topology
    ).capture_device == RING_CAPTURE_DEVICE

    assert f'device: "{RING_CAPTURE_DEVICE}"' in yaml
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in yaml
    # Both ends of one wire: the three rings share one format, so a graph
    # carrying two different ones is a sheared attach waiting at the arm.
    assert yaml.count(f"format: {wire}") == 2, yaml
    assert f"chunksize: {RING_CAMILLA_CHUNKSIZE}" in yaml
    assert f"target_level: {RING_CAMILLA_TARGET_LEVEL}" in yaml
    assert f"queuelimit: {RING_CAMILLA_QUEUELIMIT}" in yaml
    assert (
        f"enable_rate_adjust: {str(RING_CAMILLA_ENABLE_RATE_ADJUST).lower()}" in yaml
    )
    # The tap is GONE, not merely outnumbered — the whole defect was a ring sink
    # sitting over it.
    assert "plug:jasper_capture" not in yaml, yaml


@pytest.mark.parametrize(
    "device",
    [OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "hw:CARD=Lab,DEV=0"],
    ids=["aloop_active_lane", "lab_override"],
)
async def test_boot_anchor_is_byte_identical_on_every_non_ring_device(
    tmp_path, monkeypatch, device
):
    """Off the ring, the derived block reproduces the PRE-CHANGE bytes exactly.

    This is the blast-radius bound for #2364, and it is what makes the fix safe
    to land on a fleet where zero boxes are armed: every one of them re-stages
    its anchor through this function, so "nothing moved" has to be provable, not
    asserted.

    Proven by REPLAYING the exact emit staging just made, minus the seven device
    kwargs — literally the pre-change call shape — and comparing bytes. The
    replay reuses the recorded BOUND preset rather than the raw one, because
    staging binds the preset to the topology (which relabels the outputs); a
    replay from the unbound preset differs for reasons that have nothing to do
    with this change. A hand-copied table of the old defaults would rot the
    moment the emitter's own defaults changed; this cannot, because the
    defaults are re-read from the emitter's own signature.
    """
    import dataclasses

    from jasper.active_speaker import staging as staging_mod
    from jasper.active_speaker.camilla_yaml import (
        ActiveEmitDevices,
        emit_active_speaker_commissioning_config,
    )

    device_fields = {f.name for f in dataclasses.fields(ActiveEmitDevices)}
    assert device_fields, "ActiveEmitDevices lost its fields; this test is vacuous"

    seen: dict = {}
    real = staging_mod.emit_active_speaker_commissioning_config

    def recording(preset_arg, **kwargs):
        seen["preset"] = preset_arg
        seen["kwargs"] = dict(kwargs)
        return real(preset_arg, **kwargs)

    monkeypatch.setattr(
        staging_mod, "emit_active_speaker_commissioning_config", recording
    )

    topology, preset = _commissioning_box()
    derived = _anchor_yaml(topology, preset, tmp_path / "derived", device)
    assert seen, "staging never reached the emitter; this test proves nothing"

    # The pre-change call shape: same everything, minus the device block.
    replay_kwargs = {
        key: value
        for key, value in seen["kwargs"].items()
        if key not in device_fields
    }
    replay_kwargs["out_path"] = tmp_path / "replay.yml"
    pre_change = emit_active_speaker_commissioning_config(
        seen["preset"], **replay_kwargs
    )
    assert derived == pre_change, (
        "the derived device block changed a NON-ring emit; #2364's blast radius "
        "was supposed to be the ring branch only"
    )


async def test_boot_anchor_refuses_a_typod_ring_wire_instead_of_tracebacking(
    tmp_path, monkeypatch
):
    """A bad ``JASPER_FANIN_RING_WIRE_FORMAT`` is this function's blocker, not a crash.

    The applied path's twin is pinned in `test_active_speaker_baseline_profile.py`
    and the candidate emitter's typed raise in `test_ring_active_endpoint.py`; the
    anchor was the third same-shape site and the only one shipping unpinned.

    Failing loud on a token neither language recognizes is right — jasper-fanin
    parks on the same value rather than guessing a wire. What matters is HOW it
    fails: `stage_protected_startup_config` is called by the `/sound/` wizard as
    well as the CLI, so an unhandled `ValueError` here is a 500 on a household
    page. It has to arrive as an ordinary staging blocker, and nothing may be
    written.
    """
    from jasper.active_speaker.staging import stage_protected_startup_config
    from jasper.fanin_coupling import RING_WIRE_FORMAT_ENV_VAR

    topology, preset = _commissioning_box()
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s32le\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )

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
    codes = [
        issue["code"]
        for issue in payload["issues"]
        if issue.get("severity") == "blocker"
    ]
    assert "ring_wire_declaration_invalid" in codes, payload["issues"]
    detail = next(
        issue["message"]
        for issue in payload["issues"]
        if issue.get("code") == "ring_wire_declaration_invalid"
    )
    assert RING_WIRE_FORMAT_ENV_VAR in detail, detail
    assert "s32le" in detail, "the operator needs to see the value they typed"
    # NOTHING was emitted — the refusal precedes the write, so a bad wire cannot
    # leave a half-formed anchor behind.
    assert not Path(payload["config"]["path"]).exists(), payload["config"]["path"]

    # CONTROL: the same box at the ALSA lane is unaffected. The wire is resolved
    # only for a ring sink, so a typo cannot block an unarmed box's ordinary
    # re-stage — without this the assertion above would also pass if the branch
    # blocked everything.
    alsa_dir = tmp_path / "alsa"
    alsa = stage_protected_startup_config(
        topology,
        preset=preset,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        config_dir=alsa_dir,
        metadata_path=alsa_dir / "staged_metadata.json",
        run_config_check=False,
    )
    assert alsa["status"] == "staged", alsa["issues"]


# --------------------------------------------------------------------------
# THE AUDIBLE EMIT'S DEVICE BLOCK (#2412).
#
# `prepare_driver_commissioning_config` is the anchor's twin: same module, same
# emitter, same seven-field device contract — and it forwarded only the device
# NAME. It now derives that block through `active_emit_devices` like every other
# forwarding site, which `test_ring_active_endpoint.py`'s field walk enumerates.
#
# The blast radius of that derivation OFF THE RING is what this file owns, and
# it is ZERO: `active_emit_devices` hands back the emitter's own defaults for
# every non-ring device, so every box on the ALSA active lane emits the same
# bytes it emitted before, and that is provable rather than asserted. When these
# tests were written the ring arm was unreachable here — the transport gate
# refused every ring device before the emitter — and #2412's Wave 3 lifted that.
# The ring arm's own coverage is the coherent-graph tests above; this pair stays
# scoped to the non-ring bytes, which is the whole of what it ever proved.
# --------------------------------------------------------------------------


def _commissioning_yaml(topology, preset, out_dir, device):
    """Prepare the per-driver commissioning graph at ``device``; return the YAML."""
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

    payload = prepare_driver_commissioning_config(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        playback_device=device,
        config_dir=out_dir,
        run_config_check=False,
    )
    blockers = [
        i for i in payload.get("issues") or [] if i.get("severity") == "blocker"
    ]
    assert payload["status"] == "prepared" and not blockers, payload
    return Path(payload["config"]["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "device",
    [OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "hw:CARD=Lab,DEV=0"],
    ids=["aloop_active_lane", "lab_override"],
)
async def test_driver_commissioning_is_byte_identical_on_every_non_ring_device(
    tmp_path, monkeypatch, device
):
    """Off the ring, the derived block reproduces the PRE-CHANGE bytes exactly.

    This is the blast-radius bound for #2412's device block, and it was the
    whole safety argument for landing Wave 1 ahead of any transport decision:
    the emit it touches is the AUDIBLE one and every roleful box reaches it
    through the same call, so "nothing moved" had to be provable rather than
    asserted. Wave 1 could lean on the ring branch being unreachable — the
    transport gate refused every ring device before the derivation ran. Wave 3
    lifted that, so the bound this test states is now the narrower and permanent
    one: OFF the ring, the derivation reproduces the pre-change bytes exactly.

    Proven the same way the boot anchor's twin is — by REPLAYING the exact emit
    this call just made, minus the seven device kwargs, which is literally the
    pre-change call shape, and comparing bytes. The replay reuses the recorded
    BOUND preset rather than the raw one, because staging binds the preset to
    the topology (which relabels the outputs); a replay from the unbound preset
    differs for reasons that have nothing to do with this change. A hand-copied
    table of the old defaults would rot the moment the emitter's own defaults
    changed; this cannot, because the defaults are re-read from the emitter's
    own signature.
    """
    import dataclasses

    from jasper.active_speaker import staging as staging_mod
    from jasper.active_speaker.camilla_yaml import (
        ActiveEmitDevices,
        emit_active_speaker_commissioning_config,
    )

    device_fields = {f.name for f in dataclasses.fields(ActiveEmitDevices)}
    assert device_fields, "ActiveEmitDevices lost its fields; this test is vacuous"

    seen: dict = {}
    real = staging_mod.emit_active_speaker_commissioning_config

    def recording(preset_arg, **kwargs):
        seen["preset"] = preset_arg
        seen["kwargs"] = dict(kwargs)
        return real(preset_arg, **kwargs)

    monkeypatch.setattr(
        staging_mod, "emit_active_speaker_commissioning_config", recording
    )

    topology, preset = _commissioning_box()
    derived = _commissioning_yaml(topology, preset, tmp_path / "derived", device)
    assert seen, "staging never reached the emitter; this test proves nothing"

    # The pre-change call shape: same everything, minus the device block.
    replay_kwargs = {
        key: value
        for key, value in seen["kwargs"].items()
        if key not in device_fields
    }
    replay_kwargs["out_path"] = tmp_path / "replay.yml"
    pre_change = emit_active_speaker_commissioning_config(
        seen["preset"], **replay_kwargs
    )
    assert derived == pre_change, (
        "the derived device block changed a NON-ring audible emit; #2412's Wave-1 "
        "blast radius was supposed to be nothing at all"
    )


# --- #2412 Wave 4: the transport is on the journal line ----------------------
#
# Finding (C) — a commissioning graph whose sink was the ring while its source
# was still the snd-aloop tap — was invisible in the field even though the
# `driver_commission_prepared` line already named the role and the outputs. The
# fields below are what turn that into one grep. The `load` line's twin pin
# lives with its own harness in `tests/test_active_speaker_commission_load.py`.


@pytest.mark.parametrize(
    "device, expect_transport, expect_capture, expect_wire",
    [
        (
            RING_ACTIVE_PLAYBACK_DEVICE,
            "ring",
            RING_CAPTURE_DEVICE,
            None,  # resolved from the box's own wire below, never a literal
        ),
        # Not a ring end: ADR-0100 left one transport, so the line reports the
        # journal's own "no answer" literal rather than a second name.
        (OUTPUTD_ACTIVE_PLAYBACK_DEVICE, "-", DEFAULT_CAPTURE_DEVICE, "-"),
    ],
    ids=["ring", "aloop_active_lane"],
)
async def test_the_prepared_line_names_the_transport_on_both_polarities(
    tmp_path, monkeypatch, caplog, device, expect_transport, expect_capture, expect_wire
):
    """BOTH polarities, on ONE line, with every value from its owning constant.

    One polarity would pass on a line that hard-coded either answer. The `wire`
    expectation for the ring arm is read from `resolve_ring_wire`, not written
    here, because the box's wire is per-box config an operator can roll back —
    a literal would pin this test to today's default rather than to the
    derivation, and would go green against a line that reported the wrong wire
    on a narrow box.

    Asserted against `lines[0]` rather than `caplog.text` so the fields must
    ride the SAME record: four fields spread across four lines would satisfy a
    substring check while making the one-grep property false.
    """
    import logging

    from jasper.fanin_coupling import resolve_ring_wire

    topology, preset = _commissioning_box()
    if expect_wire is None:
        expect_wire = resolve_ring_wire(topology).sample_format
        assert expect_wire in ("S16_LE", "S32_LE"), expect_wire

    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.staging"):
        _commissioning_yaml(topology, preset, tmp_path / "emit", device)

    lines = [
        record.message
        for record in caplog.records
        if "event=active_speaker.driver_commission_prepared" in record.message
    ]
    assert len(lines) == 1, lines
    assert f"transport={expect_transport}" in lines[0]
    assert f"capture={expect_capture}" in lines[0]
    assert f"playback={device}" in lines[0]
    assert f"wire={expect_wire}" in lines[0]


async def test_the_prepared_line_reports_no_transport_rather_than_guessing(
    tmp_path, monkeypatch, caplog
):
    """A box whose route does not resolve reports `-`, and still reports.

    The failure this forbids is an observability field that either invents a
    transport for a box that has none, or takes the line down with it. `-` is
    the journal's "no answer" token — never an empty value, which reads as
    `transport=` followed by whatever the next field is.
    """
    import logging

    from jasper.active_speaker import staging as staging_mod

    topology, preset = _commissioning_box()
    # No device resolves: the chooser answers nothing, which is the shape a
    # box with no active lane presents.
    monkeypatch.setattr(
        staging_mod, "resolve_active_playback_device", lambda *a, **k: (None, "missing")
    )

    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.staging"):
        staging_mod.prepare_driver_commissioning_config(
            topology,
            speaker_group_id="mono",
            role="woofer",
            preset=preset,
            config_dir=tmp_path / "unresolved",
            run_config_check=False,
        )

    lines = [
        record.message
        for record in caplog.records
        if "event=active_speaker.driver_commission_prepared" in record.message
    ]
    assert len(lines) == 1, lines
    assert "transport=-" in lines[0]
    assert "capture=-" in lines[0]
    assert "playback=-" in lines[0]
    assert "wire=-" in lines[0]


# --- #2412 Wave 5: the hearing-safety evidence, made mechanical --------------
#
# No production change. These turn the design's §1.6 argument — "moving
# commissioning onto the ring changes the TRANSPORT and touches no protection"
# — from prose a panel has to re-derive into assertions a suite re-runs. The
# claim under all of them is the same: the ring changes where the bytes go, and
# nothing about what is audible or how loud it can get.


def _leaf_paths(node, prefix: str = "") -> dict[str, object]:
    """Flatten a parsed YAML document to ``{dotted.path: scalar}``.

    Over the PARSED document, not the text: comments, key order and quoting
    style are not part of the contract, and a text diff would report all three
    as changes while missing a value that moved between two equivalent
    spellings.
    """
    out: dict[str, object] = {}
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
    tmp_path, monkeypatch, wire
):
    """THE LOAD-BEARING ASSERTION: eight device fields at most, and nothing else.

    Everything #2412 claims about hearing safety rests on this one sentence —
    that pointing a commissioning emit at the ring moves the CAPTURE DEVICE, the
    two formats and the four latency/queue knobs, and touches no filter, no
    mixer, no pipeline step, no gain, no mute and no volume limit. §1.6's
    boundary argument, the unchanged-protection claim, and the panel's ability
    to review a bounded change all inherit from it.

    So it is asserted as a STRUCTURAL DIFF over the two emitted graphs rather
    than by naming the things that stay put: an enumeration of what should not
    change cannot notice a field nobody thought to enumerate. The two documents
    come from one preset, one topology and one role, so the playback device is
    the only independent variable and every other difference is a consequence
    this test either expects by name or fails on.

    **BOTH WIRES, because the count is not fixed and a one-wire test would
    report the wrong bound.** The emitter's non-ring formats are `S32_LE`, and
    the shipped ring default is WIDE — so on an ordinary box the two format
    fields hold the SAME value on both transports and only six fields move. They
    move only on a box an operator rolled back to the narrow wire. The design's
    "capture device, the two formats, and the four knobs" is therefore exact as
    an UPPER BOUND and one field pair too many as a count, which is why the
    bound is asserted as a subset with the six unconditional movers required
    inside it, and the format pair asserted to move on exactly the wire where it
    should.
    """
    import yaml

    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
    )

    monkeypatch.setattr(
        "jasper.fanin_coupling.read_declared_ring_wire_format", lambda env=None: wire
    )
    topology, preset = _commissioning_box()
    aloop = yaml.safe_load(
        _commissioning_yaml(
            topology, preset, tmp_path / "aloop", OUTPUTD_ACTIVE_PLAYBACK_DEVICE
        )
    )
    ring = yaml.safe_load(
        _commissioning_yaml(
            topology, preset, tmp_path / "ring", RING_ACTIVE_PLAYBACK_DEVICE
        )
    )

    flat_aloop = _leaf_paths(aloop)
    flat_ring = _leaf_paths(ring)
    assert flat_aloop and flat_ring, "nothing was emitted; this test is vacuous"
    assert len(flat_aloop) > 50, "the graph got small; this bound stopped meaning much"
    changed = {
        path
        for path in set(flat_aloop) | set(flat_ring)
        if flat_aloop.get(path) != flat_ring.get(path)
    }

    formats = {"devices.capture.format", "devices.playback.format"}
    always_move = {
        # The independent variable — what the caller asked for.
        "devices.playback.device",
        # The capture device, which is the whole point: under `shm_ring` fan-in
        # fills Ring A and stops feeding the snd-aloop tap, so a ring sink over
        # `plug:jasper_capture` would sweep a device nobody writes.
        "devices.capture.device",
        # The four latency/queue knobs of the certified ring geometry.
        "devices.chunksize",
        "devices.target_level",
        "devices.queuelimit",
        "devices.enable_rate_adjust",
    }
    # THE BOUND: nothing outside the device block moves, on either wire.
    assert changed <= always_move | formats, changed - (always_move | formats)
    # ...and every one of the six unconditional movers actually did.
    assert always_move <= changed, always_move - changed
    # ...and the format pair moves on exactly the wire where the ring's answer
    # differs from the emitter's non-ring default, never on the other.
    assert (formats <= changed) is (wire != DEFAULT_PLAYBACK_FORMAT), (wire, changed)

    # And the direction of each, so a diff of the right SHAPE but the wrong
    # values cannot pass. Every expectation reads from its owning constant.
    assert flat_aloop["devices.capture.device"] == DEFAULT_CAPTURE_DEVICE
    assert flat_ring["devices.capture.device"] == RING_CAPTURE_DEVICE
    assert flat_ring["devices.capture.format"] == wire
    assert flat_ring["devices.playback.format"] == wire
    assert flat_ring["devices.chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert flat_ring["devices.target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert flat_ring["devices.queuelimit"] == RING_CAMILLA_QUEUELIMIT
    assert flat_ring["devices.enable_rate_adjust"] is RING_CAMILLA_ENABLE_RATE_ADJUST

    # The ceiling is IN the diff's complement, and named anyway because it is
    # the one value a hearing panel will look for by eye.
    assert flat_ring["devices.volume_limit"] == flat_aloop["devices.volume_limit"] == 0.0


async def test_the_unattended_mute_proof_gains_no_caller():
    """`output_terminally_muted`'s caller set is unchanged (§1.6).

    The unattended-silence invariant is not weakened by one predicate, and the
    way that could quietly stop being true is a THIRD caller — a new
    unattended path proving itself silent with the same primitive while
    reasoning differently about what it may then do. The two that exist are
    both unattended proofs ("this box may boot / may arm itself, because
    nothing can make a sound"), and #2412 adds none.

    DISCOVERED by parsing, not restated: every call in `jasper/` is found, so a
    caller added anywhere fails here rather than in a review someone has to
    remember to do. Mirrors the AST caller-set shape this repo already uses for
    the no-second-writer pins (#2537 / #2558).
    """
    import ast

    repo = Path(__file__).resolve().parent.parent
    callers: set[str] = set()
    for path in sorted((repo / "jasper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            func = call.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "output_terminally_muted":
                continue
            enclosing = [
                fn
                for fn in functions
                if fn.lineno <= call.lineno <= (fn.end_lineno or call.lineno)
            ]
            owner = min(
                enclosing,
                key=lambda fn: (fn.end_lineno or fn.lineno) - fn.lineno,
            )
            callers.add(f"{path.relative_to(repo).as_posix()}::{owner.name}")

    assert callers == {
        # "this box may BOOT, because nothing can make a sound"
        "jasper/active_speaker/runtime_contract.py::_flat_output_terminally_muted",
        # "this box may ARM ITSELF onto the ring, for the same reason"
        "jasper/fanin/ring_health.py::_anchor_is_all_muted",
    }, callers


async def test_the_audible_evidence_holds_identically_on_a_ring_graph(tmp_path):
    """The attended-audible proof does not notice the transport (§1.6).

    Both halves of the pair: `driver_commission_audible_evidence` off-device
    over the emitted YAML, and `running_commission_evidence` over the graph read
    back from CamillaDSP. Asserted as EQUALITY of the two check dicts across
    transports rather than as "the ring one passes" — a ring graph that passed
    for a different reason, or passed a smaller set of checks, would satisfy the
    weaker claim.

    The `driver_protection_while_audible` GATE is asserted alongside the
    function that feeds it, because the gate id is what `/sound/` renders and
    the function is what decides it, and this design's claim is about both.
    """
    import yaml

    from jasper.active_speaker.staging import (
        prepare_driver_commissioning_config,
        running_commission_evidence,
    )

    topology, preset = _commissioning_box()

    def _evidence(device, out_dir):
        payload = prepare_driver_commissioning_config(
            topology,
            speaker_group_id="mono",
            role="woofer",
            preset=preset,
            playback_device=device,
            config_dir=out_dir,
            run_config_check=False,
        )
        assert payload["status"] == "prepared", payload
        # The payload's OWN evidence, not a recomputation: staging binds the
        # preset to the topology (which relabels the outputs), so evidence built
        # from the raw preset would answer about a different mask than the one
        # the gate actually judged.
        off_device = payload["audible_evidence"]
        text = Path(payload["config"]["path"]).read_text(encoding="utf-8")
        # CamillaDSP hands the graph back in its own YAML dialect.
        running = running_commission_evidence(
            yaml.safe_dump(yaml.safe_load(text), default_flow_style=False),
            audible_outputs=off_device["audible_outputs"],
            muted_outputs=off_device["muted_outputs"],
            tweeter_outputs=off_device["tweeter_outputs"],
            protective_hp_hz=off_device["protective_highpass_hz"],
            expected_headroom_db=COMMISSIONING_HEADROOM_DB,
        )
        gate = next(
            g
            for g in payload["required_gates"]
            if g["id"] == "driver_protection_while_audible"
        )
        return off_device, running, gate

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

    The ramp is the audible path — the thing that actually raises a driver's
    gain in a room with a person in it — and its gate is where every hearing
    protection is re-proved on the RUNNING graph. #2412's claim is that moving
    the transport does not reach any of them. That is asserted here over the
    WHOLE `checks` dict rather than over a chosen few, so a check that starts
    answering differently on the ring fails even if nobody predicted it could.

    `volume_ceiling_0db` is called out separately after the dict comparison, not
    because the comparison misses it, but because it is the one an equality
    assertion would still satisfy if BOTH sides regressed together — and it is
    the ceiling.
    """
    import yaml

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
        devices = active_emit_devices(device)
        return emit_active_speaker_commissioning_config(
            preset,
            playback_device=device,
            capture_device=devices.capture_device,
            capture_format=devices.capture_format,
            playback_format=devices.playback_format,
            chunksize=devices.chunksize,
            target_level=devices.target_level,
            queuelimit=devices.queuelimit,
            enable_rate_adjust=devices.enable_rate_adjust,
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
        running = _emit_at(device, STARTUP_MUTE_GAIN_DB)
        return build_stage5_ramp_gate(
            running_config_raw=yaml.safe_dump(yaml.safe_load(running)),
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


async def test_the_ramp_comment_states_the_waiver_the_branch_actually_honours():
    """Wave 5's seven checks read against Wave 1's CORRECTED sentence.

    The ack gate is caller-bypassable by design — `auto_retry_pending` replaces
    the pending step instead of refusing it, which is what lets the browser's
    one-click auto-ramp take successive bounded steps. Before Wave 1 both the
    module docstring and the ramp-progress comment asserted the opposite, and a
    hearing panel reviewing this gate would have read a false property about the
    exact mechanism under review.

    THE RETIRED RUNG IS ASSERTED ABSENT, not merely the new one present: a
    partial correction that added the exception in one place and left the bare
    claim in the other would pass a presence-only check. The MECHANISM is
    already pinned in both polarities by
    `tests/test_active_speaker_stage5_ramp.py::test_ramp_step_then_pending_blocks_a_second_step`
    and its `..._auto_retry_pending_replaces_same_driver_pending_step` twin —
    referenced, deliberately not duplicated here. This guards the PROSE those
    two make true.
    """
    import inspect

    from jasper.active_speaker import commission_ramp

    source = inspect.getsource(commission_ramp)
    docstring = commission_ramp.__doc__ or ""

    # The ramp-progress comment block: from its banner to the first line that
    # is not a comment. Sliced rather than grepped whole-file so the two prose
    # sites are checked as the separate claims they are — Wave 1 had to fix
    # both, and a whole-file check passes when only one is corrected.
    lines = source.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("# --- ramp progress state")
    )
    end = next(
        i
        for i in range(start + 1, len(lines))
        if lines[i].strip() and not lines[i].lstrip().startswith("#")
    )
    ramp_progress_comment = "\n".join(lines[start:end])

    sites = {
        "module docstring": docstring,
        "ramp progress state comment": ramp_progress_comment,
    }
    for name, prose in sites.items():
        # Anti-vacuity: this guard means nothing if the prose stopped
        # describing the per-step gate at all. Kept to the concept rather than
        # one spelling — the docstring says a step must be "handled", the
        # comment says "acknowledged", and both are the same claim.
        assert "step" in prose.lower(), f"{name} no longer describes the ramp's steps"
        # THE PROPERTY: any prose here that requires an ack must also name the
        # waiver the branch honours. This is what fails on a partial correction.
        assert "auto_retry_pending" in prose, (
            f"{name} asserts an ack requirement without naming the "
            f"auto_retry_pending waiver the branch actually honours: {prose}"
        )

    # THE SECOND WAIVER, AND WHY IT NEEDS ITS OWN GUARD. Wave 1 corrected THREE
    # sentences here, not two: the ack pair above, plus the woofer-first
    # ORDERING sentence, whose waiver is a different token
    # (`role_order_confirmed_roles` — the web route's identity audition supplies
    # gate-only ordering evidence, flipping `role_order_woofer_first` True with
    # nothing confirmed). Because the ordering sentence shares the docstring
    # with the ack one, `"auto_retry_pending" in prose` is satisfied by the ack
    # sentence alone — so the ordering sentence could revert to its false
    # pre-Wave-1 form with this test green. Measured, not supposed: reverting it
    # left the guard passing until this block existed.
    #
    # Written as a CONDITIONAL over the same two sites rather than as a bare
    # docstring assertion, so it stays correct if the sentence ever moves
    # between them: whichever site makes the ordering claim must name the
    # evidence that waives it. Only the docstring makes it today; the comment
    # block does not, and this is vacuous there by design.
    ordering_claimants = {
        name: prose for name, prose in sites.items() if "lower-frequency" in prose
    }
    # ...and it cannot go vacuous everywhere: deleting the claim from both sites
    # fails here rather than silently disarming the guard below.
    assert ordering_claimants, (
        "no prose site makes the woofer-first ordering claim any more; either it "
        "moved somewhere this test does not slice, or the guard is now inert"
    )
    for name, prose in ordering_claimants.items():
        assert "role_order_confirmed_roles" in prose, (
            f"{name} asserts a floor-confirmation ORDER without naming the "
            f"gate-only ordering evidence (`role_order_confirmed_roles`) that the "
            f"web route's identity audition supplies to waive it: {prose}"
        )
