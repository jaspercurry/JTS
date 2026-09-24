# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.control import audio_route_claim

from .active_speaker_fixtures import (
    PASSIVE_ONLY_DAC_ID,
    PASSIVE_ONLY_DAC_LABEL,
    register_passive_only_dac,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.output_topology_store import save_output_topology
from .audio_health_fixtures import _RETIRED_ACTIVE_LANE, _compose


def _plan_for(outputd_env: dict[str, str] | None = None):
    """A plan stub carrying the transport faces the real ``AudioRuntimePlan`` has.

    ``transport_topology`` is built by the production resolver rather than
    written as a literal, so the SHAPE name a test exercises is whatever the
    shipped code actually derives for that outputd marker set.
    """
    from types import SimpleNamespace

    from jasper.transport_coherence import transport_topology_for_coupling

    return SimpleNamespace(
        transport_topology=transport_topology_for_coupling(
            outputd_env=dict(outputd_env or {})
        ),
        # The merged outputd env the real plan carries, so the sampler reads the
        # plan's copy instead of re-merging the two env files itself.
        outputd_env=dict(outputd_env or {}),
    )


def _armed_active_outputd_env(**overrides: str) -> dict[str, str]:
    """``outputd.env`` for a box armed on the ACTIVE ring — the jts3 shape.

    Keys come from the constants (they are the drift axis); the wire format is
    resolved rather than spelled, so the premise stays coherent by construction
    instead of shearing the moment the shipped ring wire changes.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        OUTPUTD_RING_PATH_ENV_VAR,
        resolve_ring_wire,
    )

    env = {
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: "1",
        OUTPUTD_CONTENT_BRIDGE_ENV_VAR: "shm_ring",
        OUTPUTD_RING_PATH_ENV_VAR: DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        "JASPER_OUTPUTD_CONTENT_FORMAT": resolve_ring_wire().sample_format,
    }
    env.update(overrides)
    return env


def _armed_active_camilla_devices() -> dict[str, str]:
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
    )

    return {
        "capture_device": RING_CAPTURE_DEVICE,
        "playback_device": RING_ACTIVE_PLAYBACK_DEVICE,
    }


def _armed_active_transport_read(monkeypatch, tmp_path, capture_device=None, **env_overrides):
    """Run ``_read_transport_state`` against the armed-ACTIVE-ring premise."""
    from jasper import audio_runtime_plan

    outputd_env = _armed_active_outputd_env(**env_overrides)
    devices = _armed_active_camilla_devices()
    if capture_device is not None:
        devices["capture_device"] = capture_device
    env_file = tmp_path / "outputd.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in outputd_env.items()),
        encoding="utf-8",
    )
    # The FIRST layer of the merge every surface now reads (`outputd.env`, then
    # `grouping-outputd.env`); the grouping layer is absent on this box.
    monkeypatch.setattr("jasper.env_load.OUTPUTD_ENV_PATH", str(env_file))
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(
            devices=devices
        ),
    )
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "absent.json"))
    return audio_route_claim._read_transport_state(_plan_for(outputd_env))


@pytest.mark.parametrize("capture_device, parked", [
    ("jts_ring_capture", False), ("jts_ring_grouping", False), ("plug:jasper_capture", True),
])
def test_armed_active_ring_reports_only_broken_capture_routes(
    monkeypatch, tmp_path, capture_device, parked,
) -> None:
    """#2376: an armed roleful box must not be reported as parked.

    Observed on jts3 while audio was demonstrably playing: ``/state.audio_health``
    said "parked" while the SAME ``/state`` reported both rings armed. The health
    model passed ``plan.transport_topology.name`` where the report wanted a
    coupling TOKEN; on this box that name is ``shm_ring_active``, which is not a
    coupling, so the resolver fail-SAFED it and the detector compared a
    ring-armed outputd against a plan it had invented. There is no token left to
    substitute, which is what closes the class.
    """
    from jasper.fanin_coupling import TRANSPORT_SHM_RING_ACTIVE

    plan = _plan_for(_armed_active_outputd_env())
    assert plan.transport_topology.name == TRANSPORT_SHM_RING_ACTIVE

    state = _armed_active_transport_read(monkeypatch, tmp_path, capture_device=capture_device)

    assert bool(state["coherence_errors"]) is parked
    health = _compose(transport=state)
    assert (health["signal_path"]["code"] == "transport_parked") is parked


def _no_lane_active_two_way():
    """Roleful active 2-way saved against a DAC with no active outputd lane.

    Uses the synthetic passive-only profile: every DAC in the shipped registry
    now declares an active lane, so the capability-gap surfaces are pinned
    against the stand-in for the next lane-less board rather than against
    whichever real profile happens not to have been flipped yet. Callers must
    register it with ``register_passive_only_dac(monkeypatch)``.
    """
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "default",
        "name": "Mono active 2-way",
        "status": "verified",
        "hardware": {
            "device_id": PASSIVE_ONLY_DAC_ID,
            "device_label": PASSIVE_ONLY_DAC_LABEL,
            "physical_output_count": 2,
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main active speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "main"},
    })


def test_transport_state_pairs_the_route_error_with_the_dac_capability_reason(
    monkeypatch,
) -> None:
    """One derivation feeds the health model: the same detector doctor uses,
    plus the DAC-capability reason resolved from the saved topology."""
    register_passive_only_dac(monkeypatch)
    state = audio_route_claim._transport_state(
        outputd_env={"JASPER_OUTPUTD_CONTENT_PCM": "outputd_content_capture"},
        camilla_devices={"playback_device": "outputd_active_content_playback"},
        topology=_no_lane_active_two_way(),
    )

    assert any(
        _RETIRED_ACTIVE_LANE in error for error in state["coherence_errors"]
    )
    assert state["capability_gap"] == {
        "device_id": PASSIVE_ONLY_DAC_ID,
        "device_label": PASSIVE_ONLY_DAC_LABEL,
    }


def test_parked_graph_keeps_the_speaker_reported_as_parked(
    monkeypatch,
    tmp_path,
) -> None:
    """#2135's parked graph must not read as ready through #2130's surface.

    The parked graph writes to a File sink on purpose, so it names no outputd
    endpoint and the transport detector sees no contradiction to report. Without
    this, seeding it would silence the parked headline on a box that is
    deliberately, permanently silent — trading one false "Audio is ready" for
    another.
    """
    from jasper import audio_runtime_plan
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph

    register_passive_only_dac(monkeypatch)
    topology = _no_lane_active_two_way()
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "active_speaker_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")

    # Real premise, not a devices=None stub: point BOTH statefile constants at
    # real parked statefiles and let output_endpoint_evidence_from_statefiles
    # actually read them. Its verdict on a parked graph is
    # devices=<populated>, endpoint_recognized=False — a different shape from
    # the degraded devices=None read, and the one this branch must handle.
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA_STATEFILE", statefile)
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA2_STATEFILE", statefile)
    evidence = audio_runtime_plan.output_endpoint_evidence_from_statefiles(
        statefile, statefile
    )
    assert evidence.devices is not None  # populated...
    assert evidence.endpoint_recognized is False  # ...but names no outputd lane

    topology_path = tmp_path / "output_topology.json"

    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_route_claim._read_transport_state(_plan_for())

    assert state["coherence_errors"]
    assert "parked graph" in state["coherence_errors"][0]
    assert "cannot drive an active speaker layout" in state["coherence_errors"][0]
    assert "reset output setup" in state["coherence_errors"][0]
    assert "choose an explicit passive layout" in state["coherence_errors"][0]
    # The DAC-capability clause still rides along after this reason.
    assert state["capability_gap"] == {
        "device_id": PASSIVE_ONLY_DAC_ID,
        "device_label": PASSIVE_ONLY_DAC_LABEL,
    }

    # The third transport-state constructor keeps the same shape as the other
    # two, so no reader needs a `.get(...) or []` fallback.
    assert set(state) == set(audio_route_claim._empty_transport())

    health = _compose(transport=state)
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["status"] == "issue"


def test_unconfigured_parked_graph_names_the_layout_action(monkeypatch, tmp_path) -> None:
    """A fresh/reset speaker is intentionally silent, never a hidden outage."""
    from jasper.active_speaker.runtime_contract import (
        UNCONFIGURED_PARKED_EXIT,
        build_parked_muted_graph,
    )
    from tests.test_active_speaker_runtime_contract import _topology

    topology = _topology([])
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA_STATEFILE", statefile)
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA2_STATEFILE", statefile)
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_route_claim._read_transport_state(_plan_for())

    assert state["coherence_errors"] == [
        "CamillaDSP is holding the parked graph, so every output is muted "
        f"({UNCONFIGURED_PARKED_EXIT})"
    ]
    health = _compose(transport=state)
    assert health["signal_path"]["code"] == "transport_parked"
    assert health["overall"]["status"] == "issue"


def test_corrupt_layout_is_not_relabelled_as_unconfigured_silence(
    monkeypatch, tmp_path
) -> None:
    """A safe parked graph does not conceal corrupt persisted intent."""
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph
    from tests.test_active_speaker_runtime_contract import _topology

    text, graph = build_parked_muted_graph(_topology([]))
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA_STATEFILE", statefile)
    monkeypatch.setattr("jasper.paths.DEFAULT_CAMILLA2_STATEFILE", statefile)
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = audio_route_claim._read_transport_state(_plan_for())

    assert state["coherence_errors"] == [
        "Saved speaker layout is unavailable or invalid; run jasper-doctor"
    ]
    assert "mono or stereo speaker layout" not in state["coherence_errors"][0]


def test_a_degraded_transport_read_cannot_poison_later_reads(monkeypatch) -> None:
    """The no-contradictions transport state must be fresh per read.

    A shallow copy of a module-level constant shares one ``coherence_errors``
    list, so a single append by any consumer would make every later degraded
    read report the box as parked for the lifetime of jasper-control.
    """
    from jasper import audio_runtime_plan

    monkeypatch.setattr(
        "jasper.audio_runtime_plan.output_endpoint_evidence_from_statefiles",
        lambda *paths: audio_runtime_plan.OutputEndpointEvidence(devices=None),
    )
    plan = _plan_for()

    first = audio_route_claim._read_transport_state(plan)
    first["coherence_errors"].append("poisoned")
    second = audio_route_claim._read_transport_state(plan)

    assert second["coherence_errors"] == []


def test_transport_state_is_clean_when_the_ring_pair_is_undeclared(monkeypatch) -> None:
    """The converged box declares NOTHING, and `/state` must call it clean.

    An undeclared bridge is the ring — that is what outputd runs — so this is
    the ordinary healthy shape, not a half-configured one. Reading absence the
    other way put a playing speaker's pair on the parked card.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    register_passive_only_dac(monkeypatch)
    state = audio_route_claim._transport_state(
        outputd_env={},
        camilla_devices={
            "capture_device": RING_CAPTURE_DEVICE,
            "playback_device": RING_PLAYBACK_DEVICE,
        },
        topology=_no_lane_active_two_way(),
    )

    assert state["coherence_errors"] == []
    # The capability gap is reported independently of the route error so a
    # surface can explain a fault it is also detecting through the transport.
    assert state["capability_gap"] is not None


def test_every_transport_state_constructor_builds_coherence_errors_fresh(
    monkeypatch,
) -> None:
    """`_empty_transport` and `_transport_state` agree on the shape, and
    neither shares its `coherence_errors` list across calls.

    They are independent dict literals, so a reader that has to guard
    `.get("coherence_errors") or []` is one where the shape drifted; pin the
    shape instead. The third constructor, `_parked_graph_transport`, is pinned
    the same way inside
    :func:`test_parked_graph_keeps_the_speaker_reported_as_parked`, which
    already stages a real parked graph on disk.
    """
    register_passive_only_dac(monkeypatch)
    empty = audio_route_claim._empty_transport()
    live = audio_route_claim._transport_state(
        outputd_env={"JASPER_OUTPUTD_CONTENT_PCM": "outputd_content_capture"},
        camilla_devices={"playback_device": "outputd_content_playback"},
        topology=_no_lane_active_two_way(),
    )

    assert set(empty) == set(live)
    # Built per call, never shared: an append through one reader must not be
    # visible to the next, exactly as `_empty_transport`'s docstring requires.
    empty["coherence_errors"].append("leak")
    assert audio_route_claim._empty_transport()["coherence_errors"] == []
