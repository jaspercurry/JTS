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

So the bridge is a contract, and this module pins it by driving the REAL
production preparers rather than restating the source.

**#2291 Phase 3 closed the two holes this module was written to hold open.**
Until then, two pins recorded CURRENT DEFECTS — the commanded delta reached no
state key, and the rollback seam was bound on the stage that never verifies —
and they were tagged to the change that would fix them. Phase 3a is that
change, so those pins are now flipped to their fixed shape, with the same
discriminating rigor: the commanded delta is asserted to arrive with its VALUES
intact and the probe is asserted to actually RUN, because "not None" would pass
for a conductor handed anything at all.

**Read as a table**::

    verify_priors.predicted_sum             survives  -> measure_predicted_sum
    verify_priors.predicted_spec            survives  -> measure_predicted_spec_report
    verify_priors.gate_window_ms            survives  -> measure_gate_window_ms
    verify_priors.pilot_transfer_reference  survives  -> verify_pilot_transfer_prior
    verify_priors.commanded_delta           survives  -> measure_commanded_delta
    verify_priors.entry_baseline            survives  -> measure_entry_baseline
    the rollback seam                       bound on the stage that VERIFIES

``entry_baseline`` is #2291 Phase 3c's sixth key: the summed capture stage 1
takes at the mark immediately before apply, which stage 2's benefit verdict
differences its own capture against. It is the one key with a CARRY-FORWARD —
stage 2 never captures one, so without it stage 2's own first persist would
erase the measurement it exists to grade against.

Which stage binds which seam is no longer decided at two hand-assembled call
sites: ``bind_v2_stage_seams`` builds both, from the ``V2StageCapabilities``
declarations, and this module pins those declarations too.

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

from jasper.active_speaker import commission_wiring, crossover_v2_flow, delta_probe
from jasper.active_speaker import design_draft
from jasper.active_speaker import driver_safety as driver_safety_mod
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    STAGE1_INCLUDES_CLOUD_MEASURE,
    STAGE1_INCLUDES_ENTRY_BASELINE,
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

# A commanded delta shaped like one the fit really emits — a rising shelf, with
# a quiet skirt at the bottom. Ten of its thirteen bins clear
# ``DELTA_PROBE_MIN_COMMANDED_DB`` (0.5 dB), which is what puts it over
# ``DELTA_PROBE_MIN_BINS`` (8) and lets the probe reach a verdict rather than
# report ``nothing_commanded``. A flat or tiny delta would rehydrate just as
# well and prove nothing about the probe downstream of it.
_COMMANDED_FREQS_HZ = [
    500.0, 630.0, 800.0, 1000.0, 1250.0, 1600.0, 2000.0,
    2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0,
]
_COMMANDED_DELTA_DB = [
    0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0, 2.2, 2.4, 2.5, 2.5, 2.5, 2.5,
]

# #2291 Phase 3c's entry baseline, as ``EntryBaseline.to_dict`` writes it.
# Distinct, arbitrary values for the same reason as everything else here: a
# rehydration assertion can only pass by actually carrying THESE numbers.
_ENTRY_BASELINE_PROGRAM_ID = "prog-entry-baseline-stage-1"
_ENTRY_BASELINE_GRAPH = "fp-entry-graph"
_ENTRY_BASELINE_CAPTURED_AT = "2026-08-10T12:34:56Z"
_ENTRY_BASELINE_FREQS_HZ = [200.0, 400.0, 800.0, 1600.0, 3200.0]
_ENTRY_BASELINE_DB = [-2.5, -1.25, 0.0, 1.25, 2.5]
# One screened bin, at the bottom — the gate-validity clamp's own shape, and
# not all-False, so a mask that failed to cross would be visible.
_ENTRY_BASELINE_EXCLUDED = [True, False, False, False, False]


def _entry_baseline_record() -> dict[str, Any]:
    from jasper.active_speaker.crossover_v2.round_evidence import (
        ENTRY_BASELINE_KIND,
    )

    return {
        "kind": ENTRY_BASELINE_KIND,
        "program_id": _ENTRY_BASELINE_PROGRAM_ID,
        "reference_mark": crossover_v2_flow.REFERENCE_MARK_DESIGN_AXIS,
        "freqs_hz": list(_ENTRY_BASELINE_FREQS_HZ),
        "magnitude_db": list(_ENTRY_BASELINE_DB),
        "excluded": list(_ENTRY_BASELINE_EXCLUDED),
        "graph_fingerprint": _ENTRY_BASELINE_GRAPH,
        "captured_at": _ENTRY_BASELINE_CAPTURED_AT,
        "artifact_ref": "entry_baseline_09_a01",
    }


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
            "commanded_delta": {
                "freqs_hz": list(_COMMANDED_FREQS_HZ),
                "delta_db": list(_COMMANDED_DELTA_DB),
            },
            "entry_baseline": _entry_baseline_record(),
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


def test_persisted_verify_priors_carries_exactly_the_six_bridge_keys(monkeypatch):
    """The write side of the bridge: ``verify_priors`` has SIX keys.

    Named exhaustively rather than checked for presence, because a new key is a
    deliberate widening of the contract and not an incidental one.

    **Deliberate widening (#2291 Phase 3a): ``commanded_delta``.** It is the
    fifth key this pin's earlier four-key form anticipated by name — the delta
    probe's commanded axis, produced by the stage-1 fit and consumed by the
    stage-2 probe, which is a different process against a conductor that never
    ran a fit.

    **Deliberate widening (#2291 Phase 3c): ``entry_baseline``.** The sixth: the
    measured "before" the round's benefit verdict differences against, captured
    at the mark immediately before apply.

    The top-level payload is unchanged by both widenings — each new key is
    nested inside ``verify_priors``, so
    ``test_persisted_payload_top_level_keys_are_the_whole_bridge`` below still
    pins the same set.
    """
    _conductor, state = _stage_1(monkeypatch)

    assert set(state["verify_priors"]) == {
        "predicted_sum",
        "predicted_spec",
        "gate_window_ms",
        "pilot_transfer_reference",
        "commanded_delta",
        "entry_baseline",
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
# 2. the commanded delta — CROSSES (was a defect until #2291 Phase 3a)
# --------------------------------------------------------------------------- #


def test_a_commanded_delta_is_persisted_by_the_bridge(monkeypatch):
    """The commanded delta reaches a durable state key, values intact.

    The delta probe's commanded axis — ``_commanded_delta``'s
    ``(freqs_hz, delta_db)``, the shape the applied filters ask the speaker for
    — is produced in stage 1 and consumed in stage 2. Before #2291 Phase 3a the
    durable state had no key for it, so it died with the measuring session's
    process; this is the write half of the repair.

    The VALUES are asserted, not the key's presence: a persist that wrote the
    right key with someone else's numbers in it would be exactly as broken as
    one that wrote nothing, and the whole point of a bridge pin is that what
    arrives is what left. The key is named ``delta_db`` rather than
    ``magnitude_db`` because the curve is a difference of two dB predictions,
    not a magnitude.
    """
    conductor, _state = _stage_1(monkeypatch)
    _install_commanded_delta(conductor, ([500.0, 4000.0], [0.25, -1.75]))
    assert conductor.measure_commanded_delta is not None

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}

    assert state["verify_priors"]["commanded_delta"] == {
        "freqs_hz": [500.0, 4000.0],
        "delta_db": [0.25, -1.75],
    }


def test_a_conductor_with_no_commanded_delta_persists_none(monkeypatch):
    """The control for the pin above: absent stays absent, never invented.

    ``None`` is the probe's ``VERDICT_UNAVAILABLE`` — no evidence to refuse on,
    and no permission granted either — and a trims-only candidate legitimately
    commands nothing. A persist that manufactured an empty curve here would
    hand stage 2 a commanded axis the fit never produced.
    """
    conductor, _state = _stage_1(monkeypatch)
    assert conductor.measure_commanded_delta is None

    v2host.persist_conductor_state(conductor, failure_code=None)

    assert (v2host.load_v2_state() or {})["verify_priors"]["commanded_delta"] is None


def test_stage_2_rehydrates_the_commanded_delta_and_the_delta_probe_runs(monkeypatch):
    """The read half: stage 2 gets the axis, and the probe reaches a verdict.

    Two assertions, one mechanism — the mirror of the shape this pin held while
    the bridge was broken. The input arrives with the seeded numbers on it, and
    the consequence is that the probe RUNS once its other input is in hand,
    instead of reporting unavailable and grading the correction with the
    shortfall-vs-model-error discriminator switched off.

    The tracking curve is measured == predicted, so the graded error is zero
    and the verdict is ``matched``. That is a real verdict off a real
    classification, not a "not None" that any object would satisfy —
    ``test_the_rehydrated_commanded_delta_is_what_the_probe_grades_against``
    below moves the measurement away from the prediction and shows the verdict
    follows.
    """
    _seed_applied_stage_1_state()

    conductor, _state = _stage_2(monkeypatch)

    freqs, delta = conductor.measure_commanded_delta
    assert list(freqs) == _COMMANDED_FREQS_HZ
    assert list(delta) == _COMMANDED_DELTA_DB

    predicted = [-1.0, -0.9, -0.7, -0.4, 0.0, 0.3, 0.6, 0.8, 1.0, 1.1, 1.1, 1.1, 1.1]
    probe = _delta_probe_given_a_tracking_curve(
        conductor, (list(_COMMANDED_FREQS_HZ), list(predicted), list(predicted)),
    )

    assert probe is not None
    assert probe.verdict == delta_probe.VERDICT_MATCHED
    assert conductor.delta_probe is probe


def test_the_rehydrated_commanded_delta_is_what_the_probe_grades_against(monkeypatch):
    """The rehydrated axis is USED, not merely carried.

    Same seeded bridge, same tracking grid, one change: the speaker measured
    2 dB below the prediction across the band. That is a shortfall against what
    the filters commanded, and it has to stop being ``matched`` — otherwise the
    curve could be arriving intact and being ignored, which reads identically
    to a working probe from the assertion above alone.
    """
    _seed_applied_stage_1_state()

    conductor, _state = _stage_2(monkeypatch)

    predicted = [-1.0, -0.9, -0.7, -0.4, 0.0, 0.3, 0.6, 0.8, 1.0, 1.1, 1.1, 1.1, 1.1]
    measured = [value - 2.0 for value in predicted]
    probe = _delta_probe_given_a_tracking_curve(
        conductor, (list(_COMMANDED_FREQS_HZ), measured, list(predicted)),
    )

    assert probe is not None
    assert probe.verdict != delta_probe.VERDICT_MATCHED


def test_a_pre_phase_3a_state_file_leaves_the_probe_unavailable(monkeypatch):
    """A state file written before this key shipped stays honest.

    The era case, and the reason ``requires`` is observability rather than a
    gate: a household mid-journey across the deploy has an applied correction
    and a state file with no commanded axis in it. Stage 2 still opens and
    still verifies — it simply cannot run the delta probe, which is the exact
    pre-Phase-3a behaviour and is reported as unavailable rather than passed.
    """
    state = _seed_applied_stage_1_state()
    del state["verify_priors"]["commanded_delta"]
    v2host.save_v2_state(state)

    conductor, _state = _stage_2(monkeypatch)

    assert conductor.measure_commanded_delta is None
    probe = _delta_probe_given_a_tracking_curve(
        conductor,
        (list(_COMMANDED_FREQS_HZ), list(_COMMANDED_DELTA_DB), list(_COMMANDED_DELTA_DB)),
    )
    assert probe is None
    assert conductor.delta_probe is None


def test_the_commanded_delta_survives_a_real_stage_1_persist_into_stage_2(monkeypatch):
    """END TO END through the two REAL preparers, with no seeded state file.

    Every other pin in this section seeds one side of the bridge by hand. This
    one drives the whole channel: stage 1 opens for real, is given a commanded
    delta, and persists; the host then marks it applied exactly as the apply
    endpoint does; stage 2 opens for real and reads whatever stage 1 actually
    left behind. If the write shape and the read shape ever disagree — a
    renamed key, a changed serialization — the two half-tests above would both
    still pass and only this one would fail.
    """
    conductor, _state = _stage_1(monkeypatch)
    _install_commanded_delta(
        conductor, (list(_COMMANDED_FREQS_HZ), list(_COMMANDED_DELTA_DB)),
    )
    v2host.persist_conductor_state(conductor, failure_code=None)

    applied = v2host.load_v2_state() or {}
    applied["applied"] = True
    applied["candidate"] = {"fingerprint": "fp-stage-1"}
    v2host.save_v2_state(applied)

    stage_2_conductor, _state2 = _stage_2(monkeypatch)

    freqs, delta = stage_2_conductor.measure_commanded_delta
    assert list(freqs) == _COMMANDED_FREQS_HZ
    assert list(delta) == _COMMANDED_DELTA_DB


# --------------------------------------------------------------------------- #
# 3. the rollback seam — bound where VERIFY runs (#2291 Phase 3a)
# --------------------------------------------------------------------------- #


def test_only_stage_2_binds_the_rollback_seam(monkeypatch):
    """Rollback authority sits on the stage that reaches a verdict.

    ``bind_delta_probe_rollback`` is wired into exactly one set of seams, and
    #2291 Phase 3a moved it to the right one. Stage 1 carries no VERIFY, so it
    can never reach the delta probe that presses this button; stage 2 is the
    session that produces the post-apply verdict. Per ``_delta_probe_refusal``,
    a conductor with no rollback seam still refuses — but under
    ``REASON_CORRECTION_ROLLBACK_FAILED``, the copy that tells the household
    the correction is STILL APPLIED. That sentence is now true only when the
    restore genuinely failed, rather than being the only sentence available.

    Both directions, because "stage 2 binds it" alone would keep passing if the
    seam were bound on both and the duplication is the thing the capability
    declarations exist to prevent.
    """
    stage_1_conductor, _state = _stage_1(monkeypatch)
    _seed_applied_stage_1_state()
    stage_2_conductor, _state2 = _stage_2(monkeypatch)

    assert _flow_seams(stage_1_conductor).rollback is None
    assert callable(_flow_seams(stage_2_conductor).rollback)


def test_only_stage_1_binds_the_findings_publisher(monkeypatch):
    """The other asymmetry, pinned the same way and for the same reason.

    A level-frame finding is banked only by the MEASURE candidate's own gate
    (#1866), and stage 2 builds no MEASURE candidate. The two asymmetries now
    live in one place (``V2StageCapabilities.provides``), so this pins the
    second one alongside the first rather than leaving it implicit.
    """
    stage_1_conductor, _state = _stage_1(monkeypatch)
    _seed_applied_stage_1_state()
    stage_2_conductor, _state2 = _stage_2(monkeypatch)

    assert callable(_flow_seams(stage_1_conductor).publish_findings)
    assert _flow_seams(stage_2_conductor).publish_findings is None


def test_stage_2_rollback_refuses_cleanly_with_no_pre_apply_profile(monkeypatch):
    """#1863's neighbour: an automatic rollback with nothing to roll back to.

    Binding rollback on stage 2 means it can now actually FIRE, so what it does
    on a first-ever apply — where no ``pre_apply_profile`` was stashed — is part
    of this PR's contract rather than a hypothetical. The seam reports "not
    restored" and does NOT raise: ``handle_v2_restore`` refuses with
    ``CrossoverV2Refused``, which is the ordinary outcome for an automatic
    caller, and ``bind_delta_probe_rollback`` catches exactly that. The verdict
    that asked for the rollback then still reaches the household under
    ``REASON_CORRECTION_ROLLBACK_FAILED``, with the Undo button on the screen.

    Issue #1863 proper — not OFFERING Undo when no restorable profile exists —
    is a render-side affordance question on the done / verify-fail / applied-
    failure screens, and is untouched here.
    """
    _seed_applied_stage_1_state()  # applied, but no ``pre_apply_profile``
    conductor, _state = _stage_2(monkeypatch)

    rollback = _flow_seams(conductor).rollback

    assert rollback("model_error") is False


# --------------------------------------------------------------------------- #
# 4. stage 1 captures no pre-apply CLOUD, and exactly one summed capture
# --------------------------------------------------------------------------- #


def test_stage_1_plans_no_pre_apply_cloud_and_one_entry_baseline(monkeypatch):
    """What stage 1 measures before the apply: no cloud, one summed capture.

    Two facts, pinned together because they are easy to confuse and they mean
    opposite things about ``predicted_sum``:

    * **No pre-apply cloud.** ``STAGE1_INCLUDES_CLOUD_MEASURE`` is False, and
      ``PHASE_CLOUD_MEASURE`` is the only pre-apply phase producing a spatially
      COMBINED curve. That absence is what keeps the persisted
      ``predicted_sum`` a MODEL rather than a measurement.
    * **One entry baseline.** #2291 Phase 3c added exactly one summed
      at-the-mark capture, LAST, immediately before apply — the round's
      measured "before". So stage 1 does now sum, once; what it still does not
      do is sum ACROSS POSITIONS.

    (This test's earlier form said "stage 1 never sums" and called the commanded
    delta "the only commanded axis stage 2 could ever have had". Both were true
    of the shape before Phase 3c and are false now.)

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
        include_entry_baseline=STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    assert PHASE_CLOUD_MEASURE not in planned.values()
    assert list(planned.values()).count(PHASE_ENTRY_BASELINE) == 1

    _conductor, state = _stage_1(monkeypatch)

    assert PHASE_MEASURE in state["session_phases"]
    assert PHASE_CLOUD_MEASURE not in state["session_phases"]
    assert state["session_phases"].count(PHASE_ENTRY_BASELINE) == 1


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
    # Deliberate widening (#2292). The declaration half of Undo has to cross
    # this boundary for the same reason ``pre_apply_profile`` does: both are
    # written by ``observe_apply_success`` in one state write, and the deferred
    # VERIFY that arms right after every apply is a DIFFERENT session — so a
    # key that did not cross would leave stage 2's Undo able to restore the
    # graph but not the crossover ``/sound`` declares for it.
    "sound_declaration_undo",
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


# --------------------------------------------------------------------------- #
# 6. the stage declarations themselves (#2291 Phase 3a)
#
# ``bind_v2_stage_seams`` builds both stage shapes from these two constants, so
# they are now the single place "which stage binds what" is decided. Pinning
# them is pinning the decision; the seam assertions in section 3 pin that the
# preparers really route through it.
# --------------------------------------------------------------------------- #


def test_the_two_stages_declare_the_capabilities_that_differ():
    """Exhaustive per stage, both fields.

    Named as whole sets rather than membership checks, for the same reason the
    ``verify_priors`` pin is exhaustive: a capability appearing on both stages
    is the duplication this factory removed, and a capability appearing on
    neither is a seam that silently stopped being bound.
    """
    measure = v2host.STAGE_MEASURE_CAPABILITIES
    verify = v2host.STAGE_VERIFY_CAPABILITIES

    assert measure.stage == "measure"
    assert measure.provides == {v2host.CAPABILITY_FINDINGS}
    assert measure.requires == frozenset()

    assert verify.stage == "verify"
    assert verify.provides == {v2host.CAPABILITY_ROLLBACK}
    assert verify.requires == {
        v2host.CAPABILITY_COMMANDED_DELTA,
        v2host.CAPABILITY_PREDICTED_SUM,
        # #2291 Phase 3c. Genuinely required, not merely nice to have: without
        # the entry baseline the round can say the graph did what it commanded
        # and cannot say the speaker got better — the question the issue exists
        # for — so its absence has to reach the capability journal.
        v2host.CAPABILITY_ENTRY_BASELINE,
    }


def test_no_capability_is_provided_by_both_stages():
    """The property the factory exists to hold, stated as a property.

    The rollback defect was one seam bound in the wrong place; the shape of the
    bug was two hand-assembled lists free to disagree. This asserts the thing
    that was actually wrong — an overlap — rather than re-listing the members
    a third time.
    """
    measure = v2host.STAGE_MEASURE_CAPABILITIES
    verify = v2host.STAGE_VERIFY_CAPABILITIES

    assert not (measure.provides & verify.provides)


def test_stage_2_logs_its_capabilities_and_names_a_missing_prior(monkeypatch, caplog):
    """One declaration line per stage open, plus a warning when a prior is absent.

    The observable half of the repair. A stage that opens without a required
    prior still runs — ``requires`` is observability, not a gate — so without
    this line the resulting ``unavailable`` verdict would have nothing anywhere
    saying why. Asserted on the rendered event line because that string is what
    a support read actually greps for.
    """
    state = _seed_applied_stage_1_state()
    del state["verify_priors"]["commanded_delta"]
    v2host.save_v2_state(state)

    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        _conductor, _state = _stage_2(monkeypatch)

    lines = [record.getMessage() for record in caplog.records]
    declared = [
        line for line in lines
        if "event=correction.crossover_v2_stage_capabilities" in line
    ]
    assert len(declared) == 1
    assert "stage=verify" in declared[0]
    # Anchored on the NEXT field, so a widened list cannot satisfy these by
    # prefix: ``provides=rollback`` is a substring of ``provides=findings,
    # rollback``, and a mutation that bound rollback on both stages slipped
    # through an unanchored form of this assertion.
    assert "provides=rollback requires=" in declared[0]
    assert (
        "requires=commanded_delta,entry_baseline,predicted_sum missing="
        in declared[0]
    )
    assert declared[0].endswith("missing=commanded_delta")

    unavailable = [
        line for line in lines
        if "event=correction.crossover_v2_stage_capability_unavailable" in line
    ]
    assert len(unavailable) == 1
    assert "missing=commanded_delta" in unavailable[0]


def test_a_stage_with_every_prior_present_logs_no_unavailable_event(
    monkeypatch, caplog,
):
    """The control: the warning is about a real absence, not about opening.

    Without this, the assertion above would keep passing if the unavailable
    event fired unconditionally — which would make the one line a support read
    depends on mean nothing.
    """
    _seed_applied_stage_1_state()

    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        _conductor, _state = _stage_2(monkeypatch)

    lines = [record.getMessage() for record in caplog.records]
    declared = [
        line for line in lines
        if "event=correction.crossover_v2_stage_capabilities" in line
    ]
    assert len(declared) == 1
    assert "provides=rollback requires=" in declared[0]
    assert (
        "requires=commanded_delta,entry_baseline,predicted_sum missing="
        in declared[0]
    )
    assert 'missing=""' in declared[0]
    assert not [
        line for line in lines
        if "event=correction.crossover_v2_stage_capability_unavailable" in line
    ]


def test_stage_1_declares_itself_too(monkeypatch, caplog):
    """Both stages declare; the measuring one needs nothing handed to it."""
    with caplog.at_level("INFO", logger="jasper.web.correction_crossover_v2"):
        _conductor, _state = _stage_1(monkeypatch)

    declared = [
        record.getMessage() for record in caplog.records
        if "event=correction.crossover_v2_stage_capabilities" in record.getMessage()
    ]
    assert len(declared) == 1
    assert "stage=measure" in declared[0]
    # Anchored on the next field — see the stage-2 declaration test for why an
    # unanchored ``provides=findings`` is satisfied by ``findings,rollback``.
    # logfmt renders an empty field value as ``""`` — stage 1 is the first
    # stage, so it needs nothing handed to it and can be missing nothing.
    assert "provides=findings requires=" in declared[0]
    assert 'requires="" missing=""' in declared[0]


# --------------------------------------------------------------------------- #
# 7. the commanded delta's persist-time reduction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "n_bins",
    # Deliberately a CLASS of lengths, not one. The block width is
    # ``ceil(n / 512)``, which is a step function — so a single length is a
    # lucky point that a drifted rule can land on unchanged (measured: at
    # n=3000, ``ceil(n/512)`` and ``ceil(n/511)`` are both 6, and a one-off
    # version of this test could not see the difference at all). These span the
    # identity path (n below the ceiling), an exact multiple, and three lengths
    # whose block width moves under a perturbed divisor.
    [400, 512, 513, 1023, 1536, 4096],
)
def test_the_commanded_delta_persists_on_the_same_grid_as_the_predicted_sum(n_bins):
    """The grid agreement ``_decimate_delta``'s docstring promises.

    The two curves cross the bridge together, and their block rule lives in
    ``_decimate_to_analysis_grid`` but is restated in ``_decimate_delta`` (which
    must average in dB, not in linear power, because it reduces a DIFFERENCE of
    dB curves). This is the guard on that restatement: if the shared owner's
    blocking ever changes, the two persisted curves stop lining up.
    """
    import numpy as np

    freqs = np.linspace(20.0, 24000.0, n_bins)
    curve = np.sin(np.log10(freqs) * 7.0)

    reduced_delta = v2host._decimate_delta((freqs, curve))
    reduced_sum = v2host._decimate_sum((freqs, curve))

    assert len(reduced_delta["freqs_hz"]) <= v2host.MAX_PERSISTED_SUM_POINTS
    assert reduced_delta["freqs_hz"] == reduced_sum["freqs_hz"]


def test_the_commanded_delta_is_block_averaged_in_db_not_in_power():
    """The domain choice, pinned by the case that distinguishes them.

    A curve that swings symmetrically about 0 dB inside one block has an
    arithmetic mean of 0 and a POWER mean strictly above it (Jensen), so the two
    estimators are separable here by construction. Against a commanded floor of
    0.5 dB, that difference decides which bins the probe grades at all.
    """
    import numpy as np

    freqs = np.linspace(20.0, 24000.0, 2 * v2host.MAX_PERSISTED_SUM_POINTS)
    swing = np.tile([6.0, -6.0], v2host.MAX_PERSISTED_SUM_POINTS)

    reduced_delta = v2host._decimate_delta((freqs, swing))
    reduced_sum = v2host._decimate_sum((freqs, swing))

    assert reduced_delta["delta_db"] == pytest.approx([0.0] * len(swing[::2]))
    assert reduced_sum["magnitude_db"][0] > 1.0


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
