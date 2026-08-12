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
    parse_camilla_devices_config,
)
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE, RING_CAPTURE_DEVICE
from jasper.sound.profile import SimpleEq, SoundProfile, save_profile

pytestmark = pytest.mark.asyncio


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


@pytest.mark.parametrize(
    ("graph_device", "marker_armed"),
    [
        # Mid-arm: rung 1 has moved the graph, the hardware reconciler has not
        # run yet. A deploy landing HERE is what used to undo rung 1.
        (RING_ACTIVE_PLAYBACK_DEVICE, False),
        # Mid-rollback (and the #2339 crash window): the marker still says armed
        # while the graph has already gone back to the ALSA lane.
        (OUTPUTD_ACTIVE_PLAYBACK_DEVICE, True),
    ],
)
async def test_the_live_endpoint_follows_the_graph_not_the_marker(
    tmp_path, monkeypatch, graph_device, marker_armed,
):
    """THE GRAPH IS UPSTREAM TRUTH, and these are the states that prove it.

    Both halves of the ladder pass through a window where the graph and the
    marker disagree, in opposite directions. The marker is *derived from* the
    graph by ``jasper-audio-hardware-reconcile``, so following the graph is
    following the half the reconcilers are converging toward; following the
    marker would undo the rung the operator just completed (arm) or re-arm a box
    they just released (rollback).
    """
    topology, applied = _applied_box(tmp_path, monkeypatch)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, graph_device),
        name="loaded.yml",
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: marker_armed,
    )

    device, source = resolve_live_active_endpoint(topology)

    assert device == graph_device
    # The SOURCE is asserted, not just the device: on an unarmed box both
    # witnesses answer the same name, so a device-only assertion would pass
    # while the marker was the one answering.
    assert source == LOADED_GRAPH_SOURCE


@pytest.mark.parametrize(
    "shape",
    [
        "no_statefile",
        "dangling_config_path",
        "graph_without_devices",
        "graph_on_a_non_endpoint_device",
    ],
)
async def test_an_unreadable_graph_falls_back_to_the_marker_never_the_snapshot(
    tmp_path, monkeypatch, shape,
):
    """DEFAULT-SAFE, deliberately, and never worse than what it replaced.

    A fresh box has no statefile at all and still has to take a deploy, so an
    unreadable graph is not a refusal. The second witness is the MARKER, not the
    applied snapshot: on an armed box with an unreadable statefile the marker
    still answers the ring, where the snapshot would have named the ALSA lane
    and re-created the very clobber this fixes.

    ``graph_on_a_non_endpoint_device`` is the same rule from the other side: a
    device that is not one of the active lane's two transports is not a third
    answer to "which transport", and adopting it would hand the active emitter a
    device its own forbidden-token guard refuses mid-save. Here that device IS
    the forbidden stereo lane, so the pre-emption is not theoretical.
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

        graph = tmp_path / "stereo-lane.yml"
        graph.write_text(
            _graph_for(topology, applied, None).replace(
                OUTPUTD_ACTIVE_PLAYBACK_DEVICE, DEFAULT_PLAYBACK_DEVICE
            ),
            encoding="utf-8",
        )
        statefile.write_text(f"config_path: {graph}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )
    assert resolve_live_active_endpoint(topology) == (
        RING_ACTIVE_PLAYBACK_DEVICE,
        OUTPUTD_ACTIVE_LANE_SOURCE,
    )

    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: False
    )
    assert resolve_live_active_endpoint(topology) == (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
        OUTPUTD_ACTIVE_LANE_SOURCE,
    )


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
            OUTPUTD_ACTIVE_PLAYBACK_DEVICE, DEFAULT_PLAYBACK_DEVICE
        ),
        name="stereo-lane.yml",
    )

    with caplog.at_level(_logging.DEBUG, logger="jasper.active_speaker.playback_route"):
        device, source = resolve_live_active_endpoint(topology)

    assert (device, source) == (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
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
    """
    from jasper.sound.runtime import reconcile_current_dsp

    topology, applied = _applied_box(tmp_path, monkeypatch)
    _point_statefile_at(
        tmp_path,
        monkeypatch,
        _graph_for(topology, applied, RING_ACTIVE_PLAYBACK_DEVICE),
        name="active_speaker_baseline_candidate.yml",
    )

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    stale = config_dir / "sound_current.yml"
    stale.write_text(_graph_for(topology, applied, None), encoding="utf-8")
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


async def test_reconcile_current_dsp_is_byte_identical_on_an_unarmed_box(
    tmp_path, monkeypatch,
):
    """The other direction of the same rule: an unarmed box does not move.

    The reconcile still exists to refresh the artifact on every deploy (so
    CamillaDSP cannot reopen a stale statefile against freshly-created ring
    files); re-emitting THROUGH the live endpoint keeps that refresh and changes
    nothing else.
    """
    from jasper.sound.runtime import reconcile_current_dsp

    topology, applied = _applied_box(tmp_path, monkeypatch)
    aloop_graph = _graph_for(topology, applied, None)
    _point_statefile_at(tmp_path, monkeypatch, aloop_graph, name="loaded.yml")

    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    current = config_dir / "sound_current.yml"
    current.write_text(aloop_graph, encoding="utf-8")
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
        DEFAULT_CAPTURE_DEVICE,
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    # Byte-for-byte the graph the snapshot default would have produced, so an
    # unarmed fleet sees no change at all.
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


@pytest.mark.parametrize(
    ("armed", "expected_halves"),
    [
        (True, (RING_CAPTURE_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)),
        (False, (DEFAULT_CAPTURE_DEVICE, OUTPUTD_ACTIVE_PLAYBACK_DEVICE)),
    ],
)
async def test_a_sound_save_preserves_the_boxs_endpoint(
    tmp_path, monkeypatch, armed, expected_halves,
):
    """A household EQ save changes the EQ, never the transport.

    ``/eq/`` and ``/sound/setup/`` both land on ``load_profile_config``, which
    resolves the same active carrier; the save must fold the new preference
    filters into the graph the box is running WITHOUT moving which lane it runs
    on. Pre-fix an armed box was silently disarmed by a taste-EQ save (#2337).
    """
    from jasper.sound.runtime import load_profile_config

    topology, applied = _applied_box(tmp_path, monkeypatch)
    endpoint = RING_ACTIVE_PLAYBACK_DEVICE if armed else None
    loaded_graph = _graph_for(topology, applied, endpoint)
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
    assert _both_halves(emitted) == expected_halves


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
    # The host is NOT fingerprint-only, and an earlier version of this comment
    # said it was. Its ``normal_active_raw`` is fingerprinted AND rides into
    # ``SummedGraphRequest`` -> ``commissioning_runtime`` (which mutates only
    # ``devices["volume_limit"]``) -> ``port.apply_active_raw`` ->
    # ``camilla.set_active_config_raw``, so the snapshot's ``devices:`` block
    # would reach a live CamillaDSP verbatim. It stays exempt because it is BOTH
    # halves — moving its endpoint without moving the stored fingerprints is the
    # coupled decision above — and because the chain is not wired today:
    # ``CommissioningCaptureService.capture_next`` has no production caller, so
    # nothing reaches the apply. Wiring a handler to it re-opens this.
    "jasper/active_speaker/commissioning_host.py",
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
#     That one REFUSES on an armed box instead of being taught the ring, because
#     the ring geometry under sustained excitation is a hardware claim and the
#     live-protection admission report asserts the tap capture route.
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
async def test_driver_commissioning_refuses_the_ring_before_it_emits(
    tmp_path, monkeypatch, route,
):
    """An armed box gets a LOUD refusal, not a half-moved graph.

    ``resolve_active_playback_device`` is already ring-aware, so this emitter
    resolved the ring by NAME while forwarding none of the rest of
    ``active_emit_devices`` — no ring capture lane, no resolved wire format, no
    certified chunk/target/queue geometry. The emitted graph therefore had a ring
    sink over ``plug:jasper_capture``, the tap fan-in stops feeding under
    ``shm_ring``: the sweep excites a device nobody reads and the capture records
    silence with every daemon healthy.

    The refusal is asserted BEFORE the emit, not merely on the returned status —
    a blocker that still wrote a candidate would leave the half-moved graph on
    disk for the next reader. Both routes to a ring device are covered: the
    production marker, and an explicit lab override.
    """
    from jasper.active_speaker.staging import prepare_driver_commissioning_config

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
        playback_device=(
            RING_ACTIVE_PLAYBACK_DEVICE if route == "explicit" else None
        ),
        config_dir=tmp_path / route,
        run_config_check=False,
    )

    assert payload["status"] == "blocked", payload
    assert emits == [], "the commissioning graph was emitted anyway"
    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_ring_transport_unsupported" in codes, payload
    gate = next(
        g
        for g in payload.get("required_gates") or []
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is False
    # The operator has to be told the way OUT, not just that they are stuck.
    message = next(
        issue["message"]
        for issue in payload["issues"]
        if issue.get("code") == "commissioning_ring_transport_unsupported"
    )
    assert "baseline-reemit --endpoint aloop" in message


async def test_driver_commissioning_still_emits_on_an_unarmed_box(
    tmp_path, monkeypatch,
):
    """CONTROL: the refusal is keyed on the ring, not on commissioning at all.

    Without this, a gate that refused every box would satisfy the assertions
    above. The unarmed path must still reach the emitter on the ALSA lane.
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
    assert emits[0]["playback_device"] == OUTPUTD_ACTIVE_PLAYBACK_DEVICE
    gate = next(
        g
        for g in payload["required_gates"]
        if g.get("id") == "commissioning_transport_supported"
    )
    assert gate["passed"] is True


async def test_the_refusal_reaches_the_guarded_load_preflight(tmp_path, monkeypatch):
    """The blocker is RENDERABLE, not just returned to the builder's caller.

    A refusal the operator never sees is the silent failure again wearing a
    different hat. This walks the real gate that stands in front of the guarded
    commissioning load and asserts the message — with the release command in it —
    is in the payload a caller renders, and that the load is not allowed.
    """
    from jasper.active_speaker.startup_load import (
        build_driver_commission_load_preflight,
    )

    topology, preset = _commissioning_box()
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed", lambda env=None: True
    )

    preflight = build_driver_commission_load_preflight(
        topology,
        speaker_group_id="mono",
        role="woofer",
        preset=preset,
        config_dir=tmp_path / "armed",
        require_physical_identity=False,
    )

    assert preflight["load_allowed"] is False
    refusal = next(
        issue
        for issue in preflight["issues"]
        if issue.get("code") == "commissioning_ring_transport_unsupported"
    )
    assert refusal["severity"] == "blocker"
    assert "baseline-reemit --endpoint aloop" in refusal["message"]


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
        run_config_check=False,
    )

    codes = {issue.get("code") for issue in payload.get("issues") or []}
    assert "commissioning_ring_transport_unsupported" not in codes, payload
    assert len(emits) == 1, emits
    assert emits[0]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
