# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the W6 hardware-run bug: ``resolve_conductor_context`` must resolve
the active playback device through
:func:`jasper.active_speaker.playback_route.resolve_active_playback_device`,
never a nonexistent ``topology.playback_device`` attribute.

Before this fix, ``OutputTopology`` (a frozen dataclass with no
``playback_device`` field) always made
``getattr(topology, "playback_device", None)`` resolve to ``None``, so every
call to ``resolve_conductor_context`` — the shared context builder behind
both ``POST /crossover/v2/session`` and ``POST /crossover/v2/verify`` —
refused unconditionally with "the active output device is not declared".
This was 100% reproducible on real hardware and had zero test coverage: every
existing endpoint test short-circuits before reaching this code (an empty or
inactive ``status`` refuses earlier), so the dead seam shipped silently.

These tests build a REAL, verified :class:`~jasper.output_topology.OutputTopology`
(mirroring the fixture builder in ``tests/test_active_speaker_playback_route.py``)
rather than a mock with a hand-set ``playback_device`` attribute — a mock
would not have caught the original bug, since the mock would happily answer
whatever attribute the test author set on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from jasper.active_speaker import commission_wiring, crossover_v2_flow, design_draft
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.web import correction_crossover_v2 as v2host

_TWO_WAY_GROUP = [{
    "id": "mono",
    "label": "Mono",
    "kind": "mono",
    "mode": "active_2_way",
    "channels": [
        {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
        {
            "role": "tweeter",
            "physical_output_index": 1,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
            "protection_status": "present",
        },
    ],
}]


def _topology(device_id: str, count: int, *, card_id: str | None = None) -> OutputTopology:
    """Mirror the fixture builder in tests/test_active_speaker_playback_route.py."""

    hardware: dict[str, Any] = {
        "device_id": device_id,
        "device_label": "Test device",
        "physical_output_count": count,
    }
    if card_id:
        hardware["card_id"] = card_id
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "t",
        "name": "n",
        "status": "draft",
        "hardware": hardware,
        "speaker_groups": _TWO_WAY_GROUP,
        "routing": {"mono_group_id": "mono"},
    })


def _status() -> dict[str, Any]:
    return {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {
            "drivers": [
                {"role": "woofer", "target_fingerprint": "fp-woofer"},
                {"role": "tweeter", "target_fingerprint": "fp-tweeter"},
            ],
        },
    }


@pytest.fixture(autouse=True)
def _stub_non_topology_inputs(monkeypatch):
    """Stub every conductor-context input EXCEPT topology/playback-device
    resolution — the bug under test. Real preset, driver-safety, and volume
    derivation shapes are exercised elsewhere (tests/test_crossover_v2_conductor.py);
    stubbing them here keeps this module focused on the one seam that shipped
    broken and untested.
    """
    preset = load_active_speaker_preset()  # bundled 2-way default, real object
    monkeypatch.setattr(commission_wiring, "resolve_capture_preset", lambda topo: preset)
    monkeypatch.setattr(
        design_draft, "load_design_draft", lambda **kw: {"driver_safety_profile": {}}
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint: (FrequencyBand(20.0, 20000.0), 90.0),
    )
    monkeypatch.setattr(
        crossover_v2_flow, "derive_session_volume_db", lambda safety_profile, fps: -20.0
    )
    monkeypatch.delenv(ACTIVE_PLAYBACK_DEVICE_ENV, raising=False)
    yield


def _patch_topology(monkeypatch, topology: OutputTopology) -> None:
    from jasper import output_topology as output_topology_mod

    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: topology)


# --------------------------------------------------------------------------- #
# resolve_conductor_context — unit tests
# --------------------------------------------------------------------------- #


def test_resolves_real_playback_device_from_a_verified_topology(monkeypatch):
    """A real, resolvable topology (DAC8x with an active outputd lane) must
    produce a non-empty playback_device — the exact case that was refusing
    unconditionally on hardware."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    context = v2host.resolve_conductor_context(_status())

    assert context.playback_device
    assert isinstance(context.playback_device, str)
    assert context.topology is topo


def test_refuses_when_the_layout_has_no_resolvable_playback_route(monkeypatch):
    """An unrecognized DAC id resolves to MISSING_SOURCE (no playback route) —
    the ONE case that should still raise the typed refusal."""
    topo = _topology("generic_single_dac", 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.resolve_conductor_context(_status())

    assert "active output device is not declared" in str(excinfo.value)


def test_explicit_env_playback_device_is_honored(monkeypatch):
    """The explicit-device escape hatch (JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE,
    e.g. lab/CI hardware) must still resolve through the same seam."""
    topo = _topology("generic_single_dac", 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    monkeypatch.setenv(ACTIVE_PLAYBACK_DEVICE_ENV, "hw:Lab")

    context = v2host.resolve_conductor_context(_status())

    assert context.playback_device == "hw:Lab"


# --------------------------------------------------------------------------- #
# endpoint-level: the real resolver actually runs behind prepare_v2_session
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path, monkeypatch):
    from jasper.active_speaker.crossover_flow import CROSSOVER_FLOW_ENV

    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)


def test_prepare_v2_session_runs_the_real_conductor_context_resolver(monkeypatch):
    """Existing endpoint tests in test_correction_crossover_v2_endpoints.py
    call prepare_v2_session with status={}, which refuses BEFORE reaching
    resolve_conductor_context — that's how the dead playback_device seam
    shipped with zero coverage. This drives prepare_v2_session with a full
    status against a real, resolvable topology so resolve_conductor_context
    actually runs; only the evidence-store bundle I/O (a different, already
    fail-soft seam) is stubbed."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store", lambda topology: (object(), "sess-fake")
    )

    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None
    )

    assert prepared.label == v2host.V2_RELAY_KIND_SESSION
