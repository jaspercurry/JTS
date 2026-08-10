# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What crosses — and what is lost at — the crossover-v2 stage-1 → stage-2 bridge.

The v2 commission runs as TWO relay sessions. Stage 1
(:func:`~jasper.web.correction_crossover_v2.prepare_v2_session`) measures and
stops; the household applies; stage 2
(:func:`~jasper.web.correction_crossover_v2.prepare_v2_verify`) opens a BRAND-NEW
relay session with a BRAND-NEW conductor. Nothing is handed between them in
memory: the only channel is the durable state file
:func:`~jasper.web.correction_crossover_v2.persist_conductor_state` writes and
``prepare_v2_verify`` reads back.

So the bridge is a contract, and this module pins it — the survivals AND the
losses — by driving the REAL production preparers rather than restating the
source. Two of the pins below record CURRENT DEFECTS (the commanded delta and
the rollback seam do not reach stage 2); they are marked as such and tagged to
issue #2291 Phase 3, which is the change that would fix them. Until then these
tests describe what the shipped host does, so Phase 3 has to extend this file
deliberately instead of moving the bridge in silence.

**Read as a table**::

    verify_priors.predicted_sum             survives   -> measure_predicted_sum
    verify_priors.predicted_spec            survives   -> measure_predicted_spec_report
    verify_priors.gate_window_ms            survives   -> measure_gate_window_ms
    verify_priors.pilot_transfer_reference  survives   -> verify_pilot_transfer_prior
    the commanded delta                     LOST       (defect, #2291 Phase 3)
    the rollback seam                       LOST       (defect, #2291 Phase 3)

Related-but-different coverage, deliberately not duplicated here:
``tests/test_correction_crossover_v2_endpoints.py`` pins the host-owned apply
keys that must survive a persist, and
``tests/test_correction_crossover_v2_conductor_context.py`` pins the conductor
context resolver behind both preparers. This module owns the STAGE BOUNDARY.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker import commission_wiring, crossover_v2_flow, design_draft
from jasper.active_speaker import driver_safety as driver_safety_mod
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_MEASURE,
    STAGE1_INCLUDES_CLOUD_MEASURE,
    STAGE1_INCLUDES_LATERAL,
    build_v2_cloud_index_phase_map,
    resolve_plan_shape,
)
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.capture_relay import correction_adapter
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.web import correction_crossover_v2 as v2host

# --------------------------------------------------------------------------- #
# private reaches, all of them, in one place
#
# The four helpers below are the ONLY attribute reaches past a public surface
# in this module. Each one exists because the fact it touches has no public
# accessor today, and each is named for the fact rather than the attribute so a
# future public property can replace the body without touching a test.
# --------------------------------------------------------------------------- #


def _flow_seams(conductor: Any) -> Any:
    """The conductor's injected ``V2FlowSeams``.

    ``CrossoverV2Conductor`` exposes no ``seams`` property — the seams are its
    private I/O boundary — but WHICH seams a preparer binds is exactly the
    stage-1-vs-stage-2 capability difference this module is about.
    """
    return conductor._seams


def _rehydrated_pilot_transfer_prior(conductor: Any) -> dict[str, Any] | None:
    """The G3 reference this conductor was SEEDED with, as a dated record.

    Distinct from the public ``verify_pilot_transfer_reference`` property,
    which reports the reference THIS session established for itself — a
    different fact, and ``None`` on a conductor that has run no capture. The
    seeded prior has no public reader because the conductor may only disclose
    it, never compare against it (#1927).
    """
    values = conductor._verify_pilot_prior
    at = conductor._verify_pilot_prior_at
    if values is None or at is None:
        return None
    return {"values": dict(values), "at": at}


def _install_commanded_delta(conductor: Any, commanded: Any) -> None:
    """Give a conductor a commanded delta it did not measure.

    Only a completed MEASURE fit produces one (``_commanded_delta``), and a
    persist test that skipped it would be pinning "nothing was dropped because
    there was nothing to drop". There is no setter because in production only
    the fit writes this.
    """
    conductor._measure_commanded_delta = commanded


def _delta_probe_given_a_tracking_curve(conductor: Any, tracked: Any) -> Any:
    """Run the delta probe with its OTHER precondition already satisfied.

    ``_run_delta_probe`` needs two inputs: a VERIFY tracking curve (produced by
    a post-apply capture) and the commanded delta (the stage-1 fact this
    module is about). A conductor fresh out of ``prepare_v2_verify`` has
    neither, so asserting "the probe is unavailable" on it would pass for the
    wrong reason and keep passing even if the bridge started carrying the
    commanded delta. Installing the tracking curve isolates the one input the
    bridge is responsible for, so the assertion fails the moment that changes.
    """
    conductor._verify_tracking_curve = tracked
    return conductor._run_delta_probe()


# --------------------------------------------------------------------------- #
# fixtures — a real topology + a real conductor context behind both preparers
#
# Mirrors tests/test_correction_crossover_v2_conductor_context.py's builders:
# a REAL OutputTopology, with only the non-topology context inputs stubbed.
# --------------------------------------------------------------------------- #

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


def _topology() -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "t",
        "name": "n",
        "status": "draft",
        "hardware": {
            "device_id": HIFIBERRY_DAC8X.id,
            "device_label": "Test device",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
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
def _isolated_v2_state(tmp_path):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)


@pytest.fixture(autouse=True)
def _production_host_seams(monkeypatch, tmp_path):
    """Stand in for the hardware/IO seams both preparers open, and nothing else.

    Everything a preparer DECIDES — the plan shape, the index→phase map, the
    seam bindings, the conductor construction, the persist — runs for real.
    """
    from jasper import output_topology as output_topology_mod
    from jasper.active_speaker import model_error_store
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    preset = load_active_speaker_preset()
    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: _topology())
    monkeypatch.setattr(commission_wiring, "resolve_capture_preset", lambda topo: preset)
    monkeypatch.setattr(
        design_draft, "load_design_draft",
        lambda **kw: {"driver_safety_profile": {
            "targets": [
                {
                    "role": role,
                    "target_fingerprint": f"fp-{role}",
                    "required_protection_filters": [{
                        "kind": kind,
                        "cutoff_hz": cutoff,
                        "minimum_slope_db_per_octave": 24.0,
                    }],
                }
                for role, kind, cutoff in (
                    ("woofer", "lowpass", 6000.0), ("tweeter", "highpass", 300.0),
                )
            ],
        }},
    )
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)
    monkeypatch.setattr(
        driver_safety_mod,
        "evaluate_driver_safety_profile",
        lambda profile, topology: driver_safety_mod.DriverSafetyProfileEvaluation(
            "confirmed", True, "f" * 64, (),
        ),
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint, **kw: (FrequencyBand(20.0, 20000.0), 90.0),
    )
    monkeypatch.setattr(
        crossover_v2_flow, "derive_session_volume_db",
        lambda safety_profile, fps, **kw: -20.0,
    )
    monkeypatch.delenv(ACTIVE_PLAYBACK_DEVICE_ENV, raising=False)
    # The attempts-loop store is durable host I/O; point its documented env
    # override at the tmp dir so a run cannot read or write the developer's own
    # commissioning history.
    monkeypatch.setenv(
        model_error_store.STATE_PATH_ENV, str(tmp_path / "model_errors.json")
    )
    # The evidence bundle: `bind_evidence_publishers` reads `store.session_id`
    # eagerly, so the stand-in carries one; nothing in these tests publishes.
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda topology: (SimpleNamespace(session_id="bundle-test"), "bundle-test"),
    )
    # The measurement-volume plan: the REAL one, on a temp state path, so the
    # preparers' volume gates (`needs_recovery`, the stale-ceiling drain, the
    # wall-clock re-arm) run for real against a clean, closed plan.
    v2host.set_volume_plan_for_tests(
        SessionVolumePlan(state_path=tmp_path / "session_volume.json")
    )
    yield


# What the stubbed relay mint hands back as the session id. Both stages bind
# their conductor to whatever ``open_capture`` minted, so this is the id the
# persist rebinds to — deliberately not the seeded stage-1 one.
_MINTED_RELAY_SESSION_ID = "cap_minted_by_this_stage"


def _open_prepared(monkeypatch, prepared: Any) -> tuple[Any, dict[str, Any]]:
    """Run a prepared session's real ``_open`` and return its conductor + state.

    ``_open`` is where BOTH preparers do the work this module is about: it
    builds the seams, constructs the conductor, and persists. The two seams
    stubbed here are the relay mint (network) and the plan runner (a thread);
    the runner stub is also how the conductor is captured, since ``_open``
    hands it over as ``build_v2_run_and_consume``'s first argument and keeps no
    other reference a caller can reach.
    """
    captured: dict[str, Any] = {}

    def _fake_open_capture(_client, spec, **_kwargs):
        return SimpleNamespace(
            pi_session=SimpleNamespace(session_id=_MINTED_RELAY_SESSION_ID), spec=spec,
        )

    def _fake_runner(conductor, **_kwargs):
        captured["conductor"] = conductor

        async def _run(_client, _pi_session):
            return None

        return _run

    monkeypatch.setattr(correction_adapter, "open_capture", _fake_open_capture)
    monkeypatch.setattr(v2host, "build_v2_run_and_consume", _fake_runner)

    prepared.open(object(), "http://relay.test", "http://origin.test", "http://return.test")

    return captured["conductor"], (v2host.load_v2_state() or {})


def _stage_1(monkeypatch) -> tuple[Any, dict[str, Any]]:
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None
    )
    return _open_prepared(monkeypatch, prepared)


def _stage_2(monkeypatch) -> tuple[Any, dict[str, Any]]:
    prepared = v2host.prepare_v2_verify(
        {}, status=_status(), run_async=None, camilla_factory=None
    )
    return _open_prepared(monkeypatch, prepared)


# The stage-1 output a household applies, as `persist_conductor_state` writes
# it. Values are distinct and arbitrary so a rehydration assertion can only
# pass by actually carrying THIS number across.
_PILOT_AT = 1_760_000_000.0
_GATE_WINDOW_MS = 6.5
_PREDICTED_SPEC = {"overall_passed": True, "bands": [{"f_lo_hz": 1000.0, "passed": True}]}


def _seed_applied_stage_1_state() -> dict[str, Any]:
    state = {
        "session_id": "cap_stage1_session",
        "accepted_phases": [PHASE_CHECK, PHASE_MEASURE],
        "session_phases": [PHASE_CHECK, PHASE_MEASURE],
        "applied": True,
        "candidate": {"fingerprint": "fp-stage-1"},
        "gain_plan_db": {"woofer": -3.0, "tweeter": -6.0},
        "verify_priors": {
            "predicted_sum": {
                "freqs_hz": [500.0, 1000.0, 2000.0, 4000.0],
                "magnitude_db": [-1.0, -0.5, 0.5, 1.0],
            },
            "predicted_spec": dict(_PREDICTED_SPEC),
            "gate_window_ms": _GATE_WINDOW_MS,
            "pilot_transfer_reference": {
                "values": {"woofer": -41.5, "tweeter": -39.25}, "at": _PILOT_AT,
            },
        },
    }
    v2host.save_v2_state(state)
    return state


# --------------------------------------------------------------------------- #
# 1. what SURVIVES — the four verify_priors keys
# --------------------------------------------------------------------------- #


def test_persisted_verify_priors_carries_exactly_the_four_bridge_keys(monkeypatch):
    """The write side of the bridge: ``verify_priors`` has FOUR keys.

    Named exhaustively rather than checked for presence, because the whole
    point of the pin is that a fifth key (a commanded delta, say) is a
    deliberate widening of the contract and not an incidental one.
    """
    _conductor, state = _stage_1(monkeypatch)

    assert set(state["verify_priors"]) == {
        "predicted_sum",
        "predicted_spec",
        "gate_window_ms",
        "pilot_transfer_reference",
    }


def test_all_four_verify_priors_reach_the_stage_2_conductor(monkeypatch):
    """The read side: each persisted prior lands on the fresh conductor.

    Driven through the real ``prepare_v2_verify`` against a seeded applied
    state, so this is the actual rehydration path, values and all — not a
    restatement of the ctor argument list. Note the ONE name change across the
    boundary: ``pilot_transfer_reference`` is written by the session that owned
    it and read as the next session's ``verify_pilot_transfer_prior``, because
    stage 2 may only disclose it (#1927).
    """
    _seed_applied_stage_1_state()

    conductor, _state = _stage_2(monkeypatch)

    freqs, magnitude = conductor.measure_predicted_sum
    assert list(freqs) == [500.0, 1000.0, 2000.0, 4000.0]
    assert list(magnitude) == [-1.0, -0.5, 0.5, 1.0]
    assert conductor.measure_predicted_spec_report == _PREDICTED_SPEC
    assert conductor.measure_gate_window_ms == _GATE_WINDOW_MS
    assert _rehydrated_pilot_transfer_prior(conductor) == {
        "values": {"woofer": -41.5, "tweeter": -39.25}, "at": _PILOT_AT,
    }


# --------------------------------------------------------------------------- #
# 2. what is LOST — the commanded delta (CURRENT DEFECT, #2291 Phase 3)
# --------------------------------------------------------------------------- #


def test_a_commanded_delta_is_not_persisted_by_the_bridge(monkeypatch):
    """CURRENT DEFECT (#2291 Phase 3): the commanded delta reaches no state key.

    The delta probe's commanded axis — ``_commanded_delta``'s
    ``(freqs_hz, delta_db)``, the shape the applied filters ask the speaker for
    — is produced in stage 1 and consumed in stage 2, but the durable state has
    no key for it, so it dies with the measuring session's process.

    The conductor here is given a real commanded delta before the persist, so
    the pin says "a value that EXISTS is dropped" rather than the vacuous "a
    conductor with nothing to persist persisted nothing".
    """
    conductor, _state = _stage_1(monkeypatch)
    _install_commanded_delta(conductor, ([500.0, 4000.0], [0.25, -1.75]))
    assert conductor.measure_commanded_delta is not None

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}

    assert "commanded_delta" not in state["verify_priors"]
    assert not [key for key in state if "commanded" in key]


def test_stage_2_conductor_has_no_commanded_delta_and_no_delta_probe(monkeypatch):
    """CURRENT DEFECT (#2291 Phase 3): stage 2 cannot run the delta probe.

    Two assertions, one mechanism. The input is absent
    (``measure_commanded_delta is None`` — there is no ctor argument for it on
    this path, and nothing on disk to fill one), and the consequence is that
    the probe reports unavailable even once its other input is in hand. Per
    ``_run_delta_probe``'s own docstring, ``None`` here is
    ``VERDICT_UNAVAILABLE``: no evidence to refuse on, and no permission
    granted either — so today's stage 2 grades a correction with the
    shortfall-vs-model-error discriminator switched off.
    """
    _seed_applied_stage_1_state()

    conductor, _state = _stage_2(monkeypatch)

    assert conductor.measure_commanded_delta is None
    tracked = (
        [500.0, 1000.0, 2000.0, 4000.0],
        [-1.0, -0.6, 0.4, 0.8],
        [-1.0, -0.5, 0.5, 1.0],
    )
    assert _delta_probe_given_a_tracking_curve(conductor, tracked) is None
    assert conductor.delta_probe is None


# --------------------------------------------------------------------------- #
# 3. what is LOST — the rollback seam (CURRENT DEFECT, #2291 Phase 3)
# --------------------------------------------------------------------------- #


def test_only_stage_1_binds_the_rollback_seam(monkeypatch):
    """CURRENT DEFECT (#2291 Phase 3): rollback authority is on the wrong stage.

    ``bind_delta_probe_rollback`` is wired into exactly one set of seams — the
    STAGE-1 ones, whose own comment says that session carries no VERIFY. Stage
    2 is the session that actually reaches a post-apply verdict, and it is the
    one that cannot press Undo: per ``_delta_probe_refusal``, a conductor with
    no rollback seam still refuses, but under
    ``REASON_CORRECTION_ROLLBACK_FAILED`` — the copy that tells the household
    the correction is STILL APPLIED.
    """
    stage_1_conductor, _state = _stage_1(monkeypatch)
    _seed_applied_stage_1_state()
    stage_2_conductor, _state2 = _stage_2(monkeypatch)

    assert callable(_flow_seams(stage_1_conductor).rollback)
    assert _flow_seams(stage_2_conductor).rollback is None


# --------------------------------------------------------------------------- #
# 4. stage 1 captures no summed pre-apply curve
# --------------------------------------------------------------------------- #


def test_stage_1_plans_no_summed_pre_apply_capture(monkeypatch):
    """``STAGE1_INCLUDES_CLOUD_MEASURE`` is False, so stage 1 never sums.

    ``PHASE_CLOUD_MEASURE`` is the only pre-apply phase that produces a
    spatially combined curve, and stage 1 does not plan one. That is what makes
    the persisted ``predicted_sum`` a MODEL rather than a measurement, and it
    is why the commanded delta above is the only commanded axis stage 2 could
    ever have had.

    Pinned twice on purpose: on the public phase-map builder at the production
    flags, and on what the real ``prepare_v2_session`` actually put in the
    durable state — the builder can be right while a call site passes something
    else. That second half is what the existing coverage does not have:
    ``test_correction_crossover_v2_endpoints.py`` asserts the flag's value and
    reads the preparer's SOURCE for the flag's name, which cannot see a plan
    that was built and then discarded.
    """
    planned = build_v2_cloud_index_phase_map(
        plan_shape=resolve_plan_shape(None),
        include_cloud_measure=STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=STAGE1_INCLUDES_LATERAL,
    )
    assert PHASE_CLOUD_MEASURE not in planned.values()

    _conductor, state = _stage_1(monkeypatch)

    assert PHASE_MEASURE in state["session_phases"]
    assert PHASE_CLOUD_MEASURE not in state["session_phases"]


# --------------------------------------------------------------------------- #
# 5. the whole persisted payload — the rest of the bridge
# --------------------------------------------------------------------------- #

# Every top-level key a ``persist_conductor_state`` write lands on disk — the
# function's own payload plus the three envelope keys ``save_v2_state`` stamps
# on top (``schema_version`` / ``kind`` / ``updated_at``). Exhaustive by
# construction: this is what the bridge carries, so a Phase 3 change that adds
# or drops one has to come here and say so.
_PERSISTED_TOP_LEVEL_KEYS = {
    "accepted_phases",
    "accepted_sound_revision",
    "applied",
    "apply_blocked",
    "attempts_loop",
    "candidate",
    "cloud",
    "cloud_close",
    "evidence",
    "expected_post_apply_offset_db",
    "failure",
    "fc_selection",
    "gain_plan_db",
    "kind",
    "measure",
    "pre_apply_profile",
    "schema_version",
    "session_id",
    "session_phases",
    "sound_design_revision",
    "tier",
    "updated_at",
    "verify",
    "verify_priors",
}


def test_persisted_payload_top_level_keys_are_the_whole_bridge(monkeypatch):
    """Pin the FULL persisted key set, both stages.

    ``verify_priors`` is the part of this payload the two-stage flow was built
    around, but it is not the whole channel: the applied candidate, the
    attempts loop, the cloud verdict, and the host-owned apply keys all cross
    here too. Pinning the whole set means a Phase 3 PR that widens the bridge
    updates this list deliberately.
    """
    _conductor, stage_1_state = _stage_1(monkeypatch)

    assert set(stage_1_state) == _PERSISTED_TOP_LEVEL_KEYS

    _seed_applied_stage_1_state()
    _conductor2, stage_2_state = _stage_2(monkeypatch)

    assert set(stage_2_state) == _PERSISTED_TOP_LEVEL_KEYS


def test_stage_2_persist_does_not_regress_the_stage_1_facts(monkeypatch):
    """Opening stage 2 rebinds the session id and keeps what stage 1 earned.

    The first thing ``prepare_v2_verify``'s ``_open`` does after building its
    conductor is persist it, under the relay session id THIS stage just minted.
    Everything the measuring session banked has to survive that rebind, or the
    household loses it on the very first post-apply screen.
    """
    seeded = _seed_applied_stage_1_state()

    _conductor, state = _stage_2(monkeypatch)

    # The minted id from the relay, not the measuring session's — the rebind
    # is the write this test is about, so name the value rather than assert an
    # inequality the fixture guarantees on its own.
    assert state["session_id"] == _MINTED_RELAY_SESSION_ID
    assert seeded["session_id"] != _MINTED_RELAY_SESSION_ID
    assert state["applied"] is True
    assert state["candidate"]["fingerprint"] == "fp-stage-1"
    assert state["gain_plan_db"] == {"woofer": -3.0, "tweeter": -6.0}
    assert state["verify_priors"]["gate_window_ms"] == _GATE_WINDOW_MS
    assert state["verify_priors"]["predicted_spec"] == _PREDICTED_SPEC
    assert state["verify_priors"]["pilot_transfer_reference"] == {
        "values": {"woofer": -41.5, "tweeter": -39.25}, "at": _PILOT_AT,
    }
