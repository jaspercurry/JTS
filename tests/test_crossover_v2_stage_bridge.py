# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What crosses — and what is lost at — the crossover-v2 stage-1 → stage-2 bridge.

The v2 commission runs as TWO relay sessions, from one preparer
(:func:`~jasper.web.correction_crossover_v2.prepare_v2_session`) under its two
``verify_only`` settings. Stage 1 measures and stops; the household applies;
stage 2 opens a BRAND-NEW relay session with a BRAND-NEW conductor. Nothing is
handed between them in memory: the only channel is the durable state file
:func:`~jasper.web.correction_crossover_v2.persist_conductor_state` writes and
the verify-only prepare reads back.

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
differences its own capture against. Like ``predicted_sum`` and
``commanded_delta`` it needs NO carry-forward — it is seeded into the same
conductor field its own capture writes, so a stage-2 persist re-writes the
record that conductor was constructed with. (A carry-forward branch was
written first and deleted: mutation-verified as unreachable. See
``persist_conductor_state``'s own comment for why keeping it would have
weakened the pin that matters.)

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

import asyncio
import importlib
import math
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests._async_wait import wait_signalled
from tests.conftest import seat_process_volume_owner

from jasper.active_speaker import commission_wiring, crossover_v2_flow, delta_probe
from jasper.active_speaker import design_draft
from jasper.active_speaker import driver_safety as driver_safety_mod
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2_flow import (
    STAGE1_INCLUDES_CLOUD_MEASURE,
    STAGE1_INCLUDES_ENTRY_BASELINE,
    build_v2_cloud_index_phase_map,
    resolve_plan_shape,
)
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.web import correction_crossover_v2 as v2host
from tests.crossover_v2_fixtures import fake_measurement_mic


# Production refuses a session with no volume owner; stand one up.
# ``_real_seam_session`` then replaces this stand-in with an owner over
# its own fixture fader.
pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

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

    ``CrossoverV2Session`` exposes no ``seams`` property — the seams are its
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
    """Run the delta probe with its OTHER preconditions already satisfied.

    ``_run_delta_probe`` needs three inputs: a VERIFY tracking curve and the
    band that capture's own gate trusts (both produced by a post-apply capture)
    and the commanded delta (the stage-1 fact this module is about). A
    conductor fresh out of the verify-only prepare has none of them, so asserting
    "the probe is unavailable" on it would pass for the wrong reason and keep
    passing even if the bridge started carrying the commanded delta. Installing
    the two capture-side inputs isolates the one input the bridge is
    responsible for, so the assertion fails the moment that changes.

    The band spans the whole fixture grid, which is what the pre-#2521 code
    effectively graded — this helper is about the bridge, not about the clamp.
    """
    conductor._verify_tracking_curve = tracked
    conductor._verify_trusted_band_hz = (
        float(min(tracked[0])), float(max(tracked[0])),
    )
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
    # The preparers' mic gate (#2662 W2b, gate fix round S3) resolves the
    # measurement mic BEFORE any evidence bundle opens; the disclosure it
    # raises with none plugged in has its own pin in
    # tests/test_correction_crossover_v2_wired.py.
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic", fake_measurement_mic,
    )
    from jasper import output_topology as output_topology_mod
    from jasper.active_speaker import model_error_store
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    preset = load_active_speaker_preset()
    # Remember the REAL functions before anything is patched — the sweep at the
    # end of this block needs identity, not name, to tell a stale binding from
    # a module that simply has an attribute of the same name.
    _originals = {
        "load_output_topology": output_topology_mod.load_output_topology,
        "resolve_capture_preset": commission_wiring.resolve_capture_preset,
        "load_design_draft": design_draft.load_design_draft,
        "evaluate_driver_safety_profile": driver_safety_mod.evaluate_driver_safety_profile,
        "resolve_driver_excitation_ceilings": (
            excitation_safety_plan_mod.resolve_driver_excitation_ceilings
        ),
    }
    _home = {
        "load_output_topology": output_topology_mod,
        "resolve_capture_preset": commission_wiring,
        "load_design_draft": design_draft,
        "evaluate_driver_safety_profile": driver_safety_mod,
        "resolve_driver_excitation_ceilings": excitation_safety_plan_mod,
    }
    # Modules that bind these names at MODULE scope and are imported lazily, so
    # they would otherwise first appear DURING the patched window. Imported here
    # so the sweep below can find them; see that sweep for why it matters.
    for _late_binder in (
        "jasper.active_speaker.program_admission",
        "jasper.active_speaker.web_commissioning",
        "jasper.web.sound_setup",
    ):
        importlib.import_module(_late_binder)
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
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint, **kw: (
            FrequencyBand(20.0, 20000.0),
            90.0,
        ),
    )
    monkeypatch.setattr(
        driver_safety_mod,
        "evaluate_driver_safety_profile",
        lambda profile, topology: driver_safety_mod.DriverSafetyProfileEvaluation(
            "confirmed", True, "f" * 64, (),
        ),
    )
    # Patch each name at EVERY binding, not only its home module (#2312).
    #
    # ``web_commissioning`` and ``sound_setup`` bind these at module scope via
    # ``from … import``. When one of them is imported for the FIRST time inside
    # this fixture's patched window — which is exactly what a co-run with this
    # harness does — its module-level name captures the FAKE, and monkeypatch
    # never restores it, because monkeypatch only owns the attribute it set on
    # the home module. The fake then answers for the rest of the PROCESS.
    #
    # That is the whole #2312 signature: 38 endpoints failures downstream, all
    # ``crossover_preview_topology_mismatch``, all of them the anchor/Undo
    # rollback guards, and green in isolation.
    #
    # Swept by identity rather than by a hand-kept list of importers, so a new
    # module-level ``from … import`` cannot reintroduce it silently; monkeypatch
    # unwinds every binding together.
    for _symbol, _original in _originals.items():
        _fake = getattr(_home[_symbol], _symbol)
        for _module in list(sys.modules.values()):
            if not getattr(_module, "__name__", "").startswith("jasper"):
                continue
            if getattr(_module, _symbol, None) is _original:
                monkeypatch.setattr(_module, _symbol, _fake)
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)
    # The conductor context reads the per-role sweep-duration ceiling off the
    # same confirmed target as the caps above (#2921). These suites carry a
    # fixture profile with no ``level_duration_limits`` on it, so the real
    # reader is faked alongside its sibling; the derivation itself is covered
    # in tests/test_correction_crossover_v2_conductor_context.py against a real
    # profile.
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "effective_sweep_duration_limit_s",
        lambda safety_profile, fingerprint: 6.0,
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
    # eagerly, so the stand-in carries one. It also ACCEPTS writes, because
    # every accepted capture banks a take of its own — CHECK, MEASURE and
    # VERIFY included, since W1-c/2 gave the three unprompted phases their own
    # banked record. What lands is not what these tests read; that they can
    # land without a real bundle on disk is what keeps this module about the
    # bridge.
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda topology: (_AcceptingStore(tmp_path / "bundle"), "bundle-test"),
    )
    # The measurement-volume plan: the REAL one, on a temp state path, so the
    # preparers' volume gates (`needs_recovery`, the stale-ceiling drain, the
    # wall-clock re-arm) run for real against a clean, closed plan.
    v2host.set_volume_plan_for_tests(
        SessionVolumePlan(state_path=tmp_path / "session_volume.json")
    )
    yield


# What the stubbed mint hands back as the session id. Both stages bind their
# conductor to whatever the mint returned, so this is the id the persist
# rebinds to — deliberately not the seeded stage-1 one.
_MINTED_RELAY_SESSION_ID = "cap_minted_by_this_stage"


def _open_prepared(monkeypatch, prepared: Any) -> tuple[Any, dict[str, Any]]:
    """Run a prepared session's real ``_open`` and return its conductor + state.

    ``_open`` is where BOTH preparers do the work this module is about: it
    builds the seams, constructs the conductor, and persists. The two seams
    stubbed here are the session mint and the plan runner (a thread); the
    runner stub is also how the conductor is captured, since ``_open`` hands
    it over as the runner builder's first argument and keeps no other
    reference a caller can reach.
    """
    captured: dict[str, Any] = {}

    def _fake_mint(_device, spec):
        return SimpleNamespace(
            pi_session=SimpleNamespace(session_id=_MINTED_RELAY_SESSION_ID), spec=spec,
        )

    def _fake_runner(conductor, **_kwargs):
        captured["conductor"] = conductor

        async def _run(_client, _pi_session):
            return None

        return _run

    monkeypatch.setattr(v2host, "_mint_wired_session", _fake_mint)
    monkeypatch.setattr(v2host, "_build_wired_run", _fake_runner)

    prepared.open(object(), "http://relay.test", "http://origin.test", "http://return.test")

    return captured["conductor"], (v2host.load_v2_state() or {})


def _stage_1(monkeypatch) -> tuple[Any, dict[str, Any]]:
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None
    )
    return _open_prepared(monkeypatch, prepared)


def _stage_2(monkeypatch, *, camilla_factory: Any = None) -> tuple[Any, dict[str, Any]]:
    """``camilla_factory`` defaults to ``None`` for every caller that never
    reaches the rollback seam's apply leg. A caller that actually FIRES a
    completable rollback must stub the republish/apply doors instead — see
    ``crossover_v2_round_harness._stub_restore_doors``.
    """
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=camilla_factory,
        verify_only=True,
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
        "reference_mark": contracts.REFERENCE_MARK_DESIGN_AXIS,
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


class _AcceptingStore:
    """An evidence store stand-in that takes a write and answers an identity.

    Not a recorder: nothing in this module reads what was published. It exists
    because banking is no longer optional on the happy path — a stand-in that
    could not accept a write would turn every accepted capture into a logged
    retention failure and make this module's subject (the bridge) depend on a
    bundle being on disk.

    **Scope: the position-take path only.** It answers the two calls that path
    makes and nothing else — no ``reopen_json_artifact``, so a caller that
    verifies a candidate or a receipt through it fails loudly rather than
    quietly agreeing. That is the intended shape: this module tests the
    bridge, and a stand-in that grew to cover every store caller would start
    passing tests about the store.

    ``bundle_dir`` is a real temp directory rather than ``""``. Empty resolves
    to the pytest CWD, so the day the WAV leg of the banking path fires under
    these tests it would write into the checkout instead of failing — the
    quiet direction, and the one the previous double at least got loud.
    """

    session_id = "bundle-test"

    def __init__(self, bundle_dir: Any) -> None:
        self.bundle_dir = str(bundle_dir)

    def publish_json_artifact(self, relpath: str, payload: Any) -> Any:
        return SimpleNamespace(fingerprint=f"fp-{relpath}")

    def identify_artifact(self, relpath: str) -> Any:
        return SimpleNamespace(fingerprint=f"fp-{relpath}")


# --------------------------------------------------------------------------- #
# 0. the seam binding neither stage's own run would notice losing
# --------------------------------------------------------------------------- #


class _RecordingCheckStore:
    """An evidence store that keeps what was published through it."""

    session_id = "bundle-check-pin"
    bundle_dir = "/var/lib/jasper/bundle-check-pin"

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish_json_artifact(self, relpath: str, payload: Any) -> Any:
        self.published.append((relpath, payload))
        return SimpleNamespace(fingerprint="fp-check-pin")


@pytest.mark.parametrize(
    "open_stage_under_test",
    [pytest.param(_stage_1, id="stage-1"), pytest.param(_stage_2, id="stage-2")],
)
def test_each_stage_binds_its_own_sessions_check_publisher(
    monkeypatch, open_stage_under_test,
):
    """Both stages bind the REAL check publisher, for THIS session's bundle.

    Stage 2 never runs CHECK — its conductor is constructed with CHECK already
    accepted, so ``_consume_check`` is unreachable — which means no verdict, no
    artifact and no screen would notice if its ``publish_check`` were dropped,
    swapped for the other publisher, or left pointing at another session's
    bundle. The seam is the only place that binding is observable, so the seam
    is what this reads: call what the conductor holds and require the artifact
    to land in this stage's own bundle under the relay session id this stage
    minted.
    """
    from jasper.audio_measurement.program_analysis import GainPlan

    store = _RecordingCheckStore()
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store",
        lambda topology: (store, store.session_id),
    )
    if open_stage_under_test is _stage_2:
        # Stage 2's own preceding gate: an applied durable state.
        _seed_applied_stage_1_state()

    conductor, _state = open_stage_under_test(monkeypatch)

    conductor._seams.publish_check(
        GainPlan(
            gain_db={"woofer": -11.0}, predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
        {"bands": []},
    )

    (relpath, payload), = store.published
    assert relpath == f"crossover_v2/{_MINTED_RELAY_SESSION_ID}/check.json"
    assert payload["gain_plan_db"] == {"woofer": -11.0}


# --------------------------------------------------------------------------- #
# 1. what SURVIVES — the seven verify_priors keys
# --------------------------------------------------------------------------- #


def test_persisted_verify_priors_carries_exactly_the_fourteen_bridge_keys(monkeypatch):
    """The write side of the bridge: ``verify_priors`` has FOURTEEN keys.

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

    **Deliberate widening (#2392): ``proposal_fingerprint``.** The seventh, and
    the same shape as the fifth: the identity of the ``InterventionProposal``
    stage 1 committed, which the round receipt names. Stage 2 grades the round
    and plans nothing, so the fingerprint has no other channel — and it is the
    fingerprint rather than the proposal because a proposal reassembled from
    the decimated priors around it would digest to a different value.

    **Deliberate widening (#2522): ``verify_measured``.** The eighth, and the
    MEASURED side of the comparison whose other two sides — predicted and
    commanded — this bridge already carried. Without it a disputed delta-probe
    verdict could only be re-examined by measuring the speaker again, because
    the evidence that produced it lived in one process's memory and died with
    it. Unlike its seven neighbours this one is written by the stage that
    VERIFIES rather than the stage that fits, so a stage-1 persist writes
    ``None`` here and the key is still present.

    **Deliberate widening (#2614): ``declared_transfer``.** The ninth, and the
    delta probe's STATE axis beside the CHANGE axis the fifth carries: what the
    applied graph itself declares against the uncorrected crossover, which is
    the mask the two directional safety rules read. Same producer, same
    consumer, same reason as the fifth — without it a stage-2 probe stops
    watching every band a repeat round left alone.

    **Deliberate widening (#2662): ``alignment_prescription``.** The tenth. The
    round's delay may have come from an explicit, bounded, provenance-carrying
    prescription rather than from the aligner's own search, and the stage that
    GRADES the round has to be able to say what that number was derived from —
    an adoption record naming a candidate without naming its measured basis is
    the unauditable half of the thing. Same producer, same consumer, same
    channel as the fifth; ``None`` on every ordinary round, which is what makes
    an empty provenance block on a receipt mean exactly one thing.

    **Deliberate widening (#2662): ``alignment_objective``.** The eleventh, and
    the OUTCOME half of the tenth. A prescription alone records what a round
    ASKED for; there is a reachable rail on which the machinery commits the
    estimator's own seed while the round still carries the prescribed
    candidate's name, and a receipt that could not tell those apart would let a
    candidate that never ran be graded "measured better". Its own key rather
    than a field inside the prescription because the two are written at
    different moments by different owners, and a round that prescribed nothing
    still has an objective.

    **Deliberate widening (A9): ``blend_prescription``.** The twelfth, and the
    tenth's blend-region twin — the round's SHAPE correction may likewise have
    come from an explicit, bounded, provenance-carrying prescription rather
    than from decision 10's solver. It crosses on this bridge for the tenth's
    reason exactly: an operator stages a prescription against stage 1, and the
    stage that GRADES the round runs in another process, so without this key
    the attribution dies with the session that took it and no series can ever
    be read back as prescribed-versus-solved. ``None`` on every ordinary round.

    **Deliberate widening (A9): ``blend_prescription_sha256``.** The thirteenth,
    and the OUTCOME/REQUEST split the eleventh already draws, arrived at the
    same way: the record above says WHAT was prescribed, this says WHICH
    DOCUMENT asked, and only the second can find the evidence packet and the
    conversation behind the numbers. Its own key is not stylistic here — the
    record must round-trip through ``blend_prescription_from_mapping`` for stage
    2 to rehydrate it, and that reader refuses an unknown field rather than
    ignoring it, so a digest nested inside made the whole record unreadable.
    ``""`` on every ordinary round.

    **Deliberate widening (topology pin): ``topology_prescription``.** The
    fourteenth, and the one that carries the most weight per byte: stage 2
    RE-OPENS its grading session at the topology this names. Without it a
    pinned round's VERIFY would be graded against the incumbent corner's design
    target and its overlap band — the applied graph judged for not being the
    crossover it deliberately replaced. Like the eleventh it must survive a
    round trip through a reader that refuses unknown fields, which is why it is
    exactly ``to_dict()`` and why it gets its own key rather than nesting.
    ``None`` on every ordinary round.

    The top-level payload is unchanged by all ten widenings — each new key is
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
        "declared_transfer",
        "entry_baseline",
        "proposal_fingerprint",
        "verify_measured",
        "alignment_prescription",
        "alignment_objective",
        "blend_prescription",
        "blend_prescription_sha256",
        "topology_prescription",
    }


def test_the_simple_verify_priors_reach_the_stage_2_conductor(monkeypatch):
    """The read side: each persisted prior lands on the fresh conductor.

    Driven through the real verify-only prepare against a seeded applied
    state, so this is the actual rehydration path, values and all — not a
    restatement of the ctor argument list. Note the ONE name change across the
    boundary: ``pilot_transfer_reference`` is written by the session that owned
    it and read as the next session's ``verify_pilot_transfer_prior``, because
    stage 2 may only disclose it (#1927).

    **This covers four of the write side's seven keys**, and the name no longer
    carries the count because the count has drifted twice: ``commanded_delta``
    earned its own read-side pin in section 2 below, ``entry_baseline``'s lives
    in ``tests/test_crossover_v2_entry_baseline.py`` (section 5,
    ``test_the_baseline_crosses_the_bridge_with_its_values_intact``) beside the
    rest of that field's contract — section 3 here is the rollback seam — and
    ``proposal_fingerprint`` (#2392) is read back through the real
    verify-only prepare in
    ``tests/test_crossover_v2_round_wiring.py::test_the_receipt_names_the_proposal_that_was_made``,
    where the receipt it feeds can be asserted in the same breath. A number in
    a test name that no assertion owns is a comment that rots.
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
# 2. per-key round trips — a value crosses intact, an absence stays an absence
# --------------------------------------------------------------------------- #


def _install_declared_transfer(conductor: Any, declared: Any) -> None:
    """The state-axis twin of ``_install_commanded_delta``, for its reason."""
    conductor._measure_declared_transfer = declared


# One row = one ``verify_priors`` key in one direction. ``persisted`` is both
# the expected write and the record seeded back for the read; ``None`` marks the
# absence control, whose read half is seeded with the key MISSING — the era
# case, a state file written before that key shipped.
_BRIDGE_KEY_ROUND_TRIPS = [
    pytest.param(
        "commanded_delta",
        lambda c: _install_commanded_delta(c, ([500.0, 4000.0], [0.25, -1.75])),
        {"freqs_hz": [500.0, 4000.0], "delta_db": [0.25, -1.75]},
        lambda c: c.measure_commanded_delta, id="commanded-delta-present",
    ),
    pytest.param("commanded_delta", None, None,
                 lambda c: c.measure_commanded_delta, id="commanded-delta-absent"),
    pytest.param(
        "declared_transfer",
        lambda c: _install_declared_transfer(c, ([500.0, 4000.0], [3.5, -0.25])),
        {"freqs_hz": [500.0, 4000.0], "delta_db": [3.5, -0.25]},
        lambda c: c.measure_declared_transfer, id="declared-transfer-present",
    ),
    pytest.param("declared_transfer", None, None,
                 lambda c: c.measure_declared_transfer, id="declared-transfer-absent"),
    pytest.param("alignment_prescription", None, None,
                 lambda c: c.alignment_prescription_record,
                 id="alignment-prescription-absent"),
]


@pytest.mark.parametrize(
    ("key", "install", "persisted", "read"), _BRIDGE_KEY_ROUND_TRIPS
)
def test_a_bridge_key_crosses_with_its_values_and_never_invents_them(
    monkeypatch, key, install, persisted, read,
):
    """Both directions of one ``verify_priors`` key, through the REAL preparers.

    VALUES, never key presence: a persist that wrote the right key with someone
    else's numbers in it would be as broken as one that wrote nothing. And
    absence stays absence, because a manufactured empty curve hands stage 2 an
    axis the fit never produced — the probe reads ``None`` as
    ``VERDICT_UNAVAILABLE``, no evidence to refuse on and no permission granted
    either, and a trims-only candidate legitimately commands nothing.

    ``read`` names the conductor property that answers for ``key``. Curve keys
    are ``delta_db`` rather than ``magnitude_db``: a difference of two dB
    predictions, not a magnitude.
    """
    conductor, _state = _stage_1(monkeypatch)
    if install is not None:
        install(conductor)
    # The install took, or there was nothing to drop: an absence pin against a
    # conductor silently never given the fact passes for the wrong reason.
    assert (read(conductor) is None) is (persisted is None)

    v2host.persist_conductor_state(conductor, failure_code=None)
    assert (v2host.load_v2_state() or {})["verify_priors"][key] == persisted

    state = _seed_applied_stage_1_state()
    if persisted is None:
        state["verify_priors"].pop(key, None)
    else:
        state["verify_priors"][key] = persisted
    v2host.save_v2_state(state)
    stage_2, _state2 = _stage_2(monkeypatch)

    if persisted is None:
        assert read(stage_2) is None
        return
    freqs, values = read(stage_2)
    assert list(freqs) == persisted["freqs_hz"]
    assert list(values) == persisted["delta_db"]


# --------------------------------------------------------------------------- #
# 2b. the delay prescription — CROSSES, from the request body to the receipt
# --------------------------------------------------------------------------- #


_PRESCRIPTION_BODY = {
    "kind": "jts_crossover_alignment_prescription",
    "artifact_schema_version": 1,
    "delay_us": -450.0,
    "basis_delay_us": -405.7,
    "basis_artifacts": [
        "captures/xover-series2-2026-08-17/diagnosis/landscape_delay_polarity_r1b.json",
    ],
    "basis_note": "direct arrival gap -405.7 +/- 3.3 us, n=33",
}


def test_a_delay_prescription_crosses_from_the_request_body_to_the_bridge(monkeypatch):
    """#2662, both halves, through the REAL boundaries.

    The write half starts at the request body rather than at a field poked onto
    a conductor: the gate that validates a prescription and the thread that
    carries it to the session are the two things that could silently not be
    wired, and a test that installed the record by hand would pass with either
    one missing.

    The read half is the reason the durable key exists at all — the stage that
    GRADES a round builds a fresh session and holds nothing stage 1 measured,
    so an adopted candidate could otherwise reach its adoption record with no basis
    named. VALUES, not key presence, for ``commanded_delta``'s reason.
    """
    prepared = v2host.prepare_v2_session(
        {"alignment_prescription": dict(_PRESCRIPTION_BODY)},
        status=_status(), run_async=None, camilla_factory=None,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    assert conductor.alignment_prescription_record["delay_us"] == -450.0

    v2host.persist_conductor_state(conductor, failure_code=None)
    banked = (v2host.load_v2_state() or {})["verify_priors"]["alignment_prescription"]
    assert banked == {
        "kind": "jts_crossover_alignment_prescription",
        "artifact_schema_version": 1,
        "delay_us": -450.0,
        "basis_delay_us": -405.7,
        # Derived on the way out, never carried in: the receipt states the
        # residual so a reader does not have to subtract two conventions.
        "residual_us": pytest.approx(-44.3),
        "basis_artifacts": list(_PRESCRIPTION_BODY["basis_artifacts"]),
        "basis_note": _PRESCRIPTION_BODY["basis_note"],
        # …and WHAT the residual cleared: this fixture's rig crosses at
        # 2500 Hz, whose half-period lobe is 200 µs. Without the pair, a
        # reader finding a residual on a receipt cannot tell which lobe it was
        # measured against, and the corner is nowhere else in the block.
        "checked_at_fc_hz": pytest.approx(2500.0),
        "lobe_us": pytest.approx(200.0),
        # The optional basin pin, absent on this delay-only candidate — banked as an
        # explicit ``None`` rather than an absent key so a reader of the receipt
        # can tell "this round left the basin to the fit" from "this receipt
        # predates the field".
        "polarity": None,
    }

    # ...and the read half, on a fresh stage-2 conductor.
    state = _seed_applied_stage_1_state()
    state["verify_priors"]["alignment_prescription"] = {
        k: v for k, v in banked.items() if k != "residual_us"
    }
    state["verify_priors"]["alignment_objective"] = "explicit_prescription_committed"
    v2host.save_v2_state(state)
    stage_2, _ = _stage_2(monkeypatch)
    assert stage_2.alignment_prescription_record["basis_delay_us"] == -405.7
    # The OUTCOME crosses beside the request. Without it the grading stage can
    # bank a candidate's provenance for a round that committed the estimator's seed.
    assert stage_2.measure_alignment_objective == "explicit_prescription_committed"


@pytest.mark.parametrize(
    ("polarity", "sign"),
    [
        pytest.param("invert", -1, id="pinned-basin-reaches-the-fit"),
        # Today's shipped callers, which must keep reaching the automatic path:
        # ``None`` is what leaves every delay-only candidate byte-identical.
        pytest.param(None, None, id="unpinned-leaves-the-basin-to-the-fit"),
    ],
)
def test_a_prescribed_basin_reaches_the_fit_only_when_it_was_pinned(
    monkeypatch, polarity, sign,
):
    """The basin half of the same crossing, through the REAL gate.

    The pin is useless unless it arrives at the fit, and the hop that could
    silently not be wired is the conductor's own ``_measure_priors`` — a field
    parsed, banked, and then never handed down would leave every surface saying
    "invert" while the round re-rolled the basin anyway. So this asserts the
    PRIOR, which is one hop from the selection that reads it.
    """
    body = dict(_PRESCRIPTION_BODY)
    if polarity is not None:
        body["polarity"] = polarity
    prepared = v2host.prepare_v2_session(
        {"alignment_prescription": body},
        status=_status(), run_async=None, camilla_factory=None,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)

    assert conductor.alignment_prescription_record["polarity"] == polarity
    priors = conductor._measure_priors()
    assert priors.explicit_alignment_polarity_sign == sign
    assert priors.explicit_alignment_delay_us == -450.0


@pytest.mark.parametrize("value", ("inverted", "review", "flip", -1))
def test_an_unknown_basin_refuses_the_session_before_it_opens(value):
    """Fail-closed at the tap, by name — never a silently-dropped pin.

    Same shape as the out-of-lobe refusal beside it: the operator learns at the
    tap rather than after a ten-minute measurement, and the reason slug is in
    the message so a scripted sweep can branch on it.
    """
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {"alignment_prescription": {**_PRESCRIPTION_BODY, "polarity": value}},
            status=_status(), run_async=None, camilla_factory=None,
        )

    assert "prescription_polarity_invalid" in str(excinfo.value)


def test_an_out_of_lobe_prescription_refuses_the_session_before_it_opens(monkeypatch):
    """Fail-closed at the tap, not after a ten-minute capture.

    The ``0 µs`` control candidate is the honest fixture here: against the measured
    basis it leaves 405.7 µs of residual, outside the half-period lobe at this
    rig's corner, and it is refused with its reason named rather than clamped
    onto the boundary and measured under the candidate's name.
    """
    del monkeypatch
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {"alignment_prescription": {**_PRESCRIPTION_BODY, "delay_us": 0.0}},
            status=_status(), run_async=None, camilla_factory=None,
        )
    assert "prescription_out_of_lobe" in str(excinfo.value)


def test_the_tap_asks_the_preset_for_its_own_declared_window(monkeypatch):
    """The gate must call the hardware's declaration, not just accept one.

    The declared window is the only bound in the prescription gate that does
    not rest on a number the request supplied, so "the tap forgot to pass it"
    is the failure that silently removes it. Derived here from the SAME public
    helper the tap uses, against the fixture's own preset, so this cannot pass
    by agreeing with a hard-coded number.

    ``basis_delay_us`` is set equal to ``delay_us``, which puts the residual at
    zero and takes the measurement's lobe out of the question entirely: only
    the window can refuse what follows.
    """
    del monkeypatch
    from jasper.active_speaker.crossover_v2_flow import (
        alignment_delay_search_bounds_us,
    )

    context = v2host.resolve_conductor_context(_status())
    bounds = alignment_delay_search_bounds_us(context.preset)
    assert bounds is not None, "this fixture's preset must declare a window"
    hi_us = max(abs(float(b)) for b in bounds)

    def _post(magnitude_us):
        body = {
            **_PRESCRIPTION_BODY,
            "delay_us": -magnitude_us,
            "basis_delay_us": -magnitude_us,
        }
        return v2host.prepare_v2_session(
            {"alignment_prescription": body},
            status=_status(), run_async=None, camilla_factory=None,
        )

    # One microsecond past the declaration is refused, by the window's name.
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _post(hi_us + 1.0)
    assert "prescription_outside_declared_window" in str(excinfo.value)
    # …and exactly AT it is accepted, so the guard is a bound and not a ban.
    assert _post(hi_us) is not None


def test_a_committed_candidate_teaches_the_session_which_commitment_it_was(
    monkeypatch,
):
    """The objective reaches the session from the candidate's frozen evidence.

    ``planning.analysis_json`` already puts ``alignment_objective`` on the
    candidate, so the commit seam reads the SAME answer the candidate's
    fingerprint covers rather than a second reading of a live analysis. If it
    read nothing, every round receipt would report ``committed: None`` and the
    candidate-ran question would be permanently unanswerable.
    """
    import dataclasses

    conductor, _state = _stage_1(monkeypatch)
    assert conductor.measure_alignment_objective == ""

    # The commit seam's LAST act is publishing the candidate to the evidence
    # store, which this harness does not stand up. Stubbed so the test exercises
    # the session-state write it is about; the ordering the seam guarantees
    # (every attribute write completes before the first side effect) is
    # ``test_crossover_v2_conductor``'s to pin, not this file's.
    conductor._seams = dataclasses.replace(
        conductor._seams, publish_candidate=lambda _candidate: None,
    )
    # A REAL candidate, not a stub: the point is that the objective is read off
    # the frozen ``analysis`` evidence ``planning.analysis_json`` writes, so a
    # stub carrying only that key would prove the read and not the route.
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
    )
    from jasper.active_speaker.profile import ActiveSpeakerPreset

    from tests.test_active_speaker_profile import _two_way_preset

    conductor.commit_intervention_proposal(
        MeasuredCrossoverCandidate(
            program_id="prog-abc123",
            analysis={
                "alignment_objective": "explicit_prescription_committed",
                "delay_us": -450.0,
            },
            source_preset=ActiveSpeakerPreset.from_mapping(_two_way_preset("mono")),
            role_attenuations_db={"woofer": 0.0, "tweeter": -3.5},
        ),
        predicted_sum=None,
        commanded_delta=None,
        level_frame_finding=None,
    )
    assert conductor.measure_alignment_objective == "explicit_prescription_committed"

    v2host.persist_conductor_state(conductor, failure_code=None)
    assert (v2host.load_v2_state() or {})["verify_priors"]["alignment_objective"] == (
        "explicit_prescription_committed"
    )


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
# 2b. the MEASURED curve, retained so a verdict can be re-graded (#2522)
#
# The bridge above carries what the correction PREDICTED and COMMANDED. Until
# this landed the third side of the comparison — what the speaker actually did —
# lived in one process's memory and died with it, so a disputed ``model_error``
# could only be re-examined by measuring the speaker again.
# --------------------------------------------------------------------------- #


def _regradable_fixture() -> tuple[Any, Any, Any]:
    """``(freqs, commanded, error)`` on a grid dense enough to be decimated.

    2,048 bins against the 512-point persist ceiling, so the round-trip below
    really exercises the block average rather than passing through untouched.
    The error is a localized bump — a shape no fitted frame can absorb — so the
    live verdict is a rollback and a re-grade that quietly lost the evidence
    would show up as a changed verdict, not merely a changed decimal.
    """
    import numpy as np

    freqs = np.logspace(math.log10(100.0), math.log10(20_000.0), 2048)
    commanded = np.full_like(freqs, 6.0)
    error = np.where((freqs >= 2_000.0) & (freqs <= 3_000.0), 6.0, 0.0)
    return freqs, commanded, error


def test_the_measured_verify_curve_is_persisted_beside_the_priors(monkeypatch):
    """The write side: the pair the probe graded reaches durable state.

    Both curves, not just the measured one — the graded error is their
    difference, and a record carrying one of them could not reproduce it.
    """
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    conductor._verify_tracking_curve = (freqs, predicted + error, predicted)

    v2host.persist_conductor_state(conductor, failure_code=None)
    record = (v2host.load_v2_state() or {})["verify_priors"]["verify_measured"]

    assert set(record) == {"freqs_hz", "measured_db", "predicted_db"}
    n = len(record["freqs_hz"])
    assert 0 < n <= v2host.MAX_PERSISTED_SUM_POINTS
    assert len(record["measured_db"]) == n
    assert len(record["predicted_db"]) == n
    # Decimated in dB, which is linear — so the difference of the two persisted
    # curves is the decimated difference, exactly the quantity that gets graded.
    persisted_error = np.asarray(record["measured_db"]) - np.asarray(
        record["predicted_db"]
    )
    assert float(np.max(persisted_error)) == pytest.approx(6.0, abs=1e-9)
    assert float(np.min(persisted_error)) == pytest.approx(0.0, abs=1e-9)


def test_a_session_with_no_verify_capture_persists_no_measured_curve(monkeypatch):
    """Absent means "not re-gradable offline", which is the honest reading for
    a stage-1 persist — and for every state file written before this key
    shipped. It is never a fabricated empty curve."""
    conductor, _state = _stage_1(monkeypatch)
    v2host.persist_conductor_state(conductor, failure_code=None)
    priors = (v2host.load_v2_state() or {})["verify_priors"]
    assert priors["verify_measured"] is None
    assert v2host.verify_measured_curve_from_state({"verify_priors": priors}) is None


def test_a_verdict_can_be_re_graded_from_the_store_alone(monkeypatch):
    """**#2522's whole point.** The persisted record reproduces the live
    verdict without the speaker.

    Everything the re-grade needs is now durable: the measured/predicted pair
    from this key, the commanded axis from its neighbour, and the band and the
    declared offset from the ``delta_probe`` record itself. The re-grade below
    reads all of it back off disk and re-runs the SAME classifier the session
    ran, and lands on the same verdict — through the 2048 → 512 decimation.
    """
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    _install_commanded_delta(conductor, (freqs, commanded))
    live = _delta_probe_given_a_tracking_curve(
        conductor, (freqs, predicted + error, predicted),
    )
    assert live is not None
    assert live.rollback is True

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}

    stored_freqs, stored_measured, stored_predicted = (
        v2host.verify_measured_curve_from_state(state)
    )
    # The decimation really happened — otherwise this test would be pinning a
    # pass-through and would keep passing if the block average broke.
    assert stored_freqs.size < freqs.size
    stored_commanded = v2host.commanded_delta_prior_from_state(state)
    commanded_on_grid = np.interp(
        stored_freqs, stored_commanded[0], stored_commanded[1]
    )
    regraded = delta_probe.classify_delta_probe(
        stored_freqs,
        (stored_measured - stored_predicted) + commanded_on_grid,
        commanded_on_grid,
        band_hz=live.requested_band_hz,
        expected_offset_db=live.expected_offset_db,
    )

    assert regraded.verdict == live.verdict
    assert regraded.reason == live.reason
    assert regraded.rollback == live.rollback
    # …and the numbers behind it survive the decimation, not merely the label.
    assert regraded.max_error_db == pytest.approx(live.max_error_db, abs=0.05)
    assert regraded.exceedance_octaves == pytest.approx(
        live.exceedance_octaves, abs=0.05
    )


def test_an_anchored_verdict_is_re_gradable_from_the_store_alone(monkeypatch):
    """**#2533 keeps #2522's contract.** The residual is now measured against the
    PRE-apply capture, and that capture was already durable.

    Nothing new is retained for it: ``verify_priors.entry_baseline`` is #2291's
    key, and the raw-branch prediction the anchor is expressed against is
    recovered from the stored pair the same way the live session recovers it
    (``predicted_post − commanded``). So a disputed level verdict stays a
    laptop-side experiment rather than another hardware run — which is the whole
    reason both curves are on disk.
    """
    import numpy as np

    from jasper.active_speaker.crossover_v2.contracts import ResponseCurve
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    conductor, _state = _stage_1(monkeypatch)
    freqs, _flat_commanded, error = _regradable_fixture()
    # ``_regradable_fixture`` commands 6 dB across the WHOLE grid, so it has no
    # quiet bins and therefore no residual to anchor. Same grid and same
    # localized error — the shape no fitted frame absorbs, so the verdict stays a
    # rollback — but commanded only over the midband, which is what leaves bins
    # for the anchor to be measured in.
    commanded = np.where((freqs >= 300.0) & (freqs <= 8_000.0), 6.0, 0.0)
    predicted = np.zeros_like(freqs)
    _install_commanded_delta(conductor, (freqs, commanded))

    # A pre-apply capture that already sat 2.5 dB under its own prediction: the
    # standing anchoring term this fix exists to stop reporting as a level move.
    # Installed rather than walked, exactly as the commanded delta above is —
    # this test is about what crosses the bridge, not about the capture loop.
    anchor_db = -2.5
    conductor._measure_entry_baseline = EntryBaseline(
        # This SESSION's verify program, not a stand-in: since series-2 D1 the
        # probe refuses an anchor measured through another program, so a
        # placeholder here would exercise that refusal instead of the bridge.
        program_id=conductor.program_for_phase(
            crossover_v2_flow.PHASE_VERIFY
        ).program_id,
        reference_mark="design_axis_mark",
        curve=ResponseCurve(freqs, (predicted - commanded) + anchor_db),
        excluded=tuple(False for _ in freqs),
        graph_fingerprint="fingerprint",
        captured_at="2026-08-15T00:00:00Z",
    )

    live = _delta_probe_given_a_tracking_curve(
        conductor, (freqs, predicted + error, predicted),
    )
    assert live is not None
    assert live.entry_anchor_offset_db == pytest.approx(anchor_db, abs=1e-6)

    v2host.persist_conductor_state(conductor, failure_code=None)
    state = v2host.load_v2_state() or {}

    stored_freqs, stored_measured, stored_predicted = (
        v2host.verify_measured_curve_from_state(state)
    )
    assert stored_freqs.size < freqs.size  # the decimation really happened
    stored_commanded = v2host.commanded_delta_prior_from_state(state)
    commanded_on_grid = np.interp(
        stored_freqs, stored_commanded[0], stored_commanded[1]
    )
    stored_entry = v2host.entry_baseline_prior_from_state(state)
    assert stored_entry is not None
    entry_on_grid = np.interp(
        stored_freqs,
        np.asarray(stored_entry.curve.hz, dtype=float),
        np.asarray(stored_entry.curve.db, dtype=float),
    )
    regraded = delta_probe.classify_delta_probe(
        stored_freqs,
        (stored_measured - stored_predicted) + commanded_on_grid,
        commanded_on_grid,
        band_hz=live.requested_band_hz,
        expected_offset_db=live.expected_offset_db,
        # measured_pre − predicted_raw, and predicted_raw is the stored
        # post-apply prediction with the command taken back out.
        entry_delta_db=(entry_on_grid - stored_predicted) + commanded_on_grid,
    )

    assert regraded.verdict == live.verdict
    assert regraded.reason == live.reason
    assert regraded.rollback == live.rollback
    assert regraded.entry_anchor_offset_db == pytest.approx(
        live.entry_anchor_offset_db, abs=0.05,
    )
    assert regraded.residual_offset_db == pytest.approx(
        live.residual_offset_db, abs=0.05,
    )
    assert regraded.quiet_probe_coverage == pytest.approx(
        live.quiet_probe_coverage, abs=0.05,
    )


def test_a_truncated_measured_record_reads_as_absent_not_as_a_curve(monkeypatch):
    """Three arrays that are not one curve are an absence, not a grid mismatch.

    The same rule ``commanded_delta_prior_from_state`` applies to its own pair,
    and for the same reason: a hand-edited or truncated record must not reach
    the classifier wearing the clothes of a measurement.
    """
    import numpy as np

    conductor, _state = _stage_1(monkeypatch)
    freqs, _commanded, error = _regradable_fixture()
    predicted = np.zeros_like(freqs)
    conductor._verify_tracking_curve = (freqs, predicted + error, predicted)
    v2host.persist_conductor_state(conductor, failure_code=None)

    state = v2host.load_v2_state() or {}
    assert v2host.verify_measured_curve_from_state(state) is not None
    state["verify_priors"]["verify_measured"]["measured_db"] = (
        state["verify_priors"]["verify_measured"]["measured_db"][:-3]
    )
    assert v2host.verify_measured_curve_from_state(state) is None


# --------------------------------------------------------------------------- #
# 3. the rollback seam — bound where VERIFY runs (#2291 Phase 3a)
# --------------------------------------------------------------------------- #


def test_only_stage_2_binds_the_rollback_seam(monkeypatch):
    """Rollback authority sits on the stage that reaches a verdict.

    ``bind_delta_probe_rollback`` is wired into exactly one set of seams, and
    #2291 Phase 3a moved it to the right one. Stage 1 carries no VERIFY, so it
    can never reach the delta probe whose verdict presses this button; stage 2
    is the session that produces the post-apply verdict. A conductor with no
    rollback seam still refuses — but under
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


def test_stage_2_rollback_refuses_cleanly_with_no_prior_candidate(monkeypatch):
    """#1863's neighbour: an automatic rollback with nothing to roll back to.

    Binding rollback on stage 2 means it can now actually FIRE, so what it
    does on a first-ever apply — where no prior candidate fingerprint was
    recorded to republish — is part of the contract rather than a
    hypothetical. The seam reports "not restored" and does NOT raise: it
    refuses before pressing either normal-path door, and the verdict that
    asked for the rollback still reaches the household under
    ``REASON_CORRECTION_ROLLBACK_FAILED``.

    Issue #1863 proper — not OFFERING a way back when no prior candidate
    exists — is a render-side affordance question on the done / verify-fail /
    applied-failure screens, and is untouched here.
    """
    _seed_applied_stage_1_state()  # applied, but no prior candidate recorded
    conductor, _state = _stage_2(monkeypatch, camilla_factory=lambda: SimpleNamespace())

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
        include_lateral=False,
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
    # Deliberate widening (Fc/slope apply path, 2026-08-19). What the accept
    # DISPLACED in the ``/sound`` declaration, written in the same state write
    # as the revision above and scoped to exactly that token — so it crosses
    # with `accepted_sound_revision`'s session gate, not the way-back
    # pointer's unconditional one. It has to cross at all because once the
    # declaration carries the candidate's crossover, nothing live can still
    # say what it replaced, and a retry (which skips the completed Sound
    # save) still needs that inverse to rebuild its change.
    "accepted_sound_declaration_change",
    "applied",
    "apply_blocked",
    "attempts_loop",
    "candidate",
    "cloud",
    "cloud_close",
    "evidence",
    "expected_post_apply_offset_db",
    "failure",
    "gain_plan_db",
    "kind",
    "measure",
    # Deliberate widening (#2923). Banked in the SAME state write as
    # `gain_plan_db` beside it, on the same terms: the round's realized
    # per-role MEASURE sweep length, possibly shortened by #2921's duration
    # fit, so an offline rebuild can replay a fitted round instead of
    # refusing PROGRAM_NOT_REPRODUCIBLE.
    "measure_sweep_durations_s",
    # The way back's pointer: the measured candidate the applied graph
    # displaced, written by ``observe_apply_success`` and carried forward
    # unconditionally by every ordinary persist (the deferred VERIFY re-arm
    # is a different session).
    "previous_candidate_fingerprint",
    # The pointer's pairing (which apply recorded it) — the automatic
    # revert's arming fact, host-owned like the pointer and carried on
    # identical terms.
    "previous_candidate_displaced_by",
    # Deliberate widening (B5). How many times the round ORDINAL sequence has
    # been reset. Both doors that replace durable state wholesale while leaving
    # a measured graph on the speaker — the republish and Start-Over's applied
    # branch — drop ``round_receipt`` below, which is that sequence's only
    # memory, so the next round is ordinal 1 again on an already-tuned speaker.
    # It crosses this boundary for a sharper version of the way-back
    # pointer's reason: the conductor cannot contribute it (only those two
    # doors write it), and it has to outlive the very session they mint — a
    # session-scoped
    # marker would be erased by the first persist after the reset, which is
    # exactly the round it exists to label. Disclosure only; nothing gates on it.
    "round_ordinal_epoch",
    # Deliberate widening (#2291 Phase 3c). WHERE this round's receipt landed —
    # round id plus the bundle artifact's fingerprint — so the next round can
    # resolve the previous one by identity rather than scanning bundles. It
    # crosses for the way-back pointer's reason: it describes the graph
    # currently on the speaker, which outlives the session that wrote it.
    "round_receipt",
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

    The first thing the verify-only prepare's ``_open`` does after building its
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


# --------------------------------------------------------------------------- #
# the join: a whole TuningSession, through the REAL preparer
# --------------------------------------------------------------------------- #


def _real_seam_session(monkeypatch, cam_factory=None, graph=None) -> dict:
    """The session `_open()` builds with the REAL engine binder.

    No `FakeSeams`. The original end-to-end pin substituted the binder, so it
    never met `SessionVolumePlan` — and that is exactly why it passed while
    every real session was refusing on `assert_ready`. A pin that swaps the
    subject cannot see the subject's coupling.

    The same rule now seats a REAL `VolumeOwner` over this fixture's fader:
    the claim is the fader's authority after W5-c1, so a substituted owner
    would hide exactly the coupling these pins exist to check.
    """
    _cam_for_owner = (cam_factory or (lambda: None))()
    if _cam_for_owner is not None:
        async def _owner_set(db):
            return await _cam_for_owner.set_volume_db(db, best_effort=True)

        async def _owner_get():
            return await _cam_for_owner.get_volume_db(best_effort=True)
    else:
        _standin = {"db": -20.0}

        async def _owner_set(db):
            _standin["db"] = float(db)
            return True

        async def _owner_get():
            return _standin["db"]

    seat_process_volume_owner(monkeypatch, _owner_set, _owner_get)

    captured: dict[str, Any] = {}
    real_hooks = v2host._volume_hooks

    def _capturing_hooks(camilla_factory, context, *, tuning, **kw):
        captured["tuning"] = tuning
        captured["hooks"] = real_hooks(camilla_factory, context, tuning=tuning, **kw)
        return captured["hooks"]

    monkeypatch.setattr(v2host, "_volume_hooks", _capturing_hooks)
    # ONLY the graph seam is substituted: emitting a real measurement graph
    # needs a preset whose protection satisfies the program floor, which this
    # fixture speaker does not carry. The VOLUME seam stays the real
    # ``MeasurementVolumeClaim`` over the owner seated above, because the fader
    # authority is the whole subject here and a substituted one could not see
    # the coupling these pins exist to check.
    real_binder = v2host.bind_v2_engine_seams
    graph_override = graph

    def _twin_graph_binder(**kwargs):
        import dataclasses as _dc

        from tests.engine_twin import FakeGraph

        seams = real_binder(**kwargs)
        graph = graph_override or FakeGraph()
        captured["graph"] = graph
        return _dc.replace(seams, graph=graph)

    monkeypatch.setattr(v2host, "bind_v2_engine_seams", _twin_graph_binder)
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=cam_factory,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    captured["conductor"] = conductor
    return captured


async def test_the_session_opens_after_the_plan_has_armed_its_durable_intent(
    monkeypatch,
):
    """Opening the session is the hooks' SECOND act, and the reason changed.

    It used to be that the interim claim verified the plan, so a session opened
    first refused on ``assert_ready``. W5-c1 deleted that claim: the session's
    claim is now the first thing that MOVES the fader, and the plan's durable
    intent has to be on disk before any mutation — a crash in the gap must
    hydrate as a recoverable state rather than a forgotten one. The ordering is
    still a contract, and still kept in the hooks' open arm; what it protects
    is crash recovery rather than an assertion. Production says so at
    ``correction_crossover_v2``'s open arm.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(v2host, "acquire_session_measurement_pause", _no_pause)
    monkeypatch.setattr(v2host, "release_session_measurement_pause", _no_pause)
    v2host.set_volume_plan_for_tests(SessionVolumePlan())

    class _Cam:
        def __init__(self) -> None:
            self.db = -15.0

        async def get_volume_db(self, best_effort: bool = False) -> float:
            return self.db

        async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
            self.db = float(db)
            return True

    cam = _Cam()
    captured = _real_seam_session(monkeypatch, cam_factory=lambda: cam)
    hooks = captured["hooks"]
    session = captured["tuning"]

    assert not session.is_open, "the preparer must not open the session"

    opened = await hooks.open()

    assert str(getattr(opened, "value", opened)) == "opened"
    assert session.is_open, (
        "the session did not open with the plan it verifies — B1's shape"
    )

    await hooks.close()

    assert not session.is_open
    v2host.set_volume_plan_for_tests(None)


async def test_a_session_that_takes_the_level_but_not_the_graph_keeps_neither(
    monkeypatch,
):
    """The give-back the new placement owes.

    Opening the session is now the hooks' second act, so a graph install that
    fails happens with the plan already open and the pause already held.
    Leaving either would strand the speaker at measurement volume with voice
    paused and no session to drain it — the worse half of the failure the
    ordering exists to avoid.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan
    from tests.engine_twin import FakeGraph

    released: list[str] = []

    async def _no_pause() -> None:
        return None

    async def _record_release() -> None:
        released.append("pause")

    monkeypatch.setattr(v2host, "acquire_session_measurement_pause", _no_pause)
    monkeypatch.setattr(
        v2host, "release_session_measurement_pause", _record_release,
    )
    plan = SessionVolumePlan()
    v2host.set_volume_plan_for_tests(plan)

    class _Cam:
        def __init__(self) -> None:
            self.db = -15.0

        async def get_volume_db(self, best_effort: bool = False) -> float:
            return self.db

        async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
            self.db = float(db)
            return True

    cam = _Cam()
    captured = _real_seam_session(
        monkeypatch, cam_factory=lambda: cam, graph=FakeGraph(install_raises=True),
    )

    with pytest.raises(Exception):
        await captured["hooks"].open()

    assert plan.measurement_volume_db is None, "the plan was left open"
    assert released == ["pause"], "voice was left paused with no session"
    v2host.set_volume_plan_for_tests(None)


def test_both_runner_volume_arms_classify_a_real_graph_install_failure():
    """The note: a real install raises SessionGraphError, not RuntimeError.

    Both runners classify a failed ``volume.open()`` by exception type. A
    ``SessionGraphError`` is a sibling of ``RuntimeError``, not a subclass, so
    before this it escaped both arms: the give-back still ran (no strand), but
    ``_purge_best_effort`` was skipped — leaking the relay session to worker
    TTL, the exact leak that arm exists to prevent — and the terminal failure
    was never persisted, leaving the wizard's failure view blank.

    A source-text pin because the property is which TYPES the arm names, and a
    type absent from a tuple has no behaviour to observe.
    """
    import ast
    from pathlib import Path as _Path

    for module in (
        "jasper/web/correction_crossover_v2_relay.py",
        "jasper/web/correction_crossover_v2_wired.py",
    ):
        source = _Path(__file__).resolve().parents[1] / module
        tree = ast.parse(source.read_text(encoding="utf-8"))
        caught: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = {
                getattr(inner.func, "attr", None)
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
            }
            if "open" not in calls:
                continue
            for handler in node.handlers:
                names = handler.type
                items = names.elts if isinstance(names, ast.Tuple) else [names]
                caught.update(
                    getattr(item, "id", "") for item in items if item is not None
                )

        assert "SessionGraphError" in caught, (
            f"{module}: a real graph install failure escapes the volume arm — "
            "the relay session leaks to worker TTL and the wizard shows a "
            "blank failure view"
        )


async def test_a_cancel_inside_the_give_back_still_drains_the_level(monkeypatch):
    """NB2: the give-back is shielded, so a cancel cannot abort the drain.

    This is the failure that does not self-heal. An aborted drain leaves the
    fader at measurement level with nobody holding it — and because the plan
    never latched, ``needs_recovery`` answers False, so the recovery screen
    never offers it. In-process, only the wall-clock ceiling would ever notice.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan
    from tests.engine_twin import FakeGraph

    async def _no_pause() -> None:
        return None

    monkeypatch.setattr(v2host, "acquire_session_measurement_pause", _no_pause)
    monkeypatch.setattr(v2host, "release_session_measurement_pause", _no_pause)
    plan = SessionVolumePlan()
    v2host.set_volume_plan_for_tests(plan)

    reached = asyncio.Event()

    class _SlowDrainPlan(SessionVolumePlan):
        async def abandon(self, *a, **kw):
            reached.set()
            for _ in range(50):
                await asyncio.sleep(0)
            return await super().abandon(*a, **kw)

    plan.__class__ = _SlowDrainPlan

    class _Cam:
        def __init__(self) -> None:
            self.db = -15.0

        async def get_volume_db(self, best_effort: bool = False) -> float:
            return self.db

        async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
            self.db = float(db)
            return True

    cam = _Cam()
    captured = _real_seam_session(
        monkeypatch, cam_factory=lambda: cam, graph=FakeGraph(install_raises=True),
    )

    opening = asyncio.ensure_future(captured["hooks"].open())
    await wait_signalled(reached, "the give-back started", producer=opening)
    opening.cancel()

    with pytest.raises(BaseException):
        await opening

    assert plan.measurement_volume_db is None, (
        "the cancel aborted the drain — the fader is stranded at measurement "
        "level with nobody holding it"
    )
    assert cam.db == -15.0, "the fader never came back"
    v2host.set_volume_plan_for_tests(None)


async def test_a_raising_pause_release_does_not_lose_the_give_back_drain(
    monkeypatch,
):
    """MN2c: the drain runs FIRST, so a failing pause release cannot eat it.

    The give-back's inner order is load-bearing and was correct-but-unpinned
    until here. A fader left at measurement level is worse than voice left
    paused, so the drain goes first and the pause release rides a ``finally``.
    Swap the two statements and this pin is the one that reds: the release
    raises, the drain never runs, and the speaker is left at measurement
    level with the plan holding an intent nobody will drain.
    """
    from jasper.active_speaker.session_volume_plan import SessionVolumePlan
    from tests.engine_twin import FakeGraph

    async def _no_pause() -> None:
        return None

    async def _raising_release() -> None:
        raise RuntimeError("the measurement pause could not be released")

    monkeypatch.setattr(v2host, "acquire_session_measurement_pause", _no_pause)
    monkeypatch.setattr(
        v2host, "release_session_measurement_pause", _raising_release,
    )
    plan = SessionVolumePlan()
    v2host.set_volume_plan_for_tests(plan)

    class _Cam:
        def __init__(self) -> None:
            self.db = -15.0

        async def get_volume_db(self, best_effort: bool = False) -> float:
            return self.db

        async def set_volume_db(self, db: float, best_effort: bool = False) -> bool:
            self.db = float(db)
            return True

    cam = _Cam()
    captured = _real_seam_session(
        monkeypatch, cam_factory=lambda: cam, graph=FakeGraph(install_raises=True),
    )

    with pytest.raises(BaseException):
        await captured["hooks"].open()

    assert plan.measurement_volume_db is None, (
        "the raising pause release swallowed the drain — the plan still "
        "holds an intent nobody will drain"
    )
    assert cam.db == -15.0, "the fader never came back"
    v2host.set_volume_plan_for_tests(None)


async def test_a_volume_that_did_not_confirm_installs_no_graph_at_all(
    monkeypatch,
):
    """NB1: a non-OPENED plan.open must RETURN, not fall through.

    A volume that did not confirm is not a volume anything may be admitted
    against, so installing the measurement graph past it buys two CamillaDSP
    swaps — install and restore — for a session that never plays a stimulus.
    And the result has to reach the runners: their non-OPENED branch is what
    tells the household why the session refused, and a fall-through makes that
    branch unreachable.
    """
    from tests.engine_twin import FakeGraph

    async def _no_pause() -> None:
        return None

    from jasper.active_speaker.session_volume_plan import SessionVolumePlan

    class _RefusingPlan(SessionVolumePlan):
        """A real plan whose open never confirms — the shape NB1 is about."""

        async def open(self, *_a, **_kw):
            return "refused"

        def assert_ready(self, now=None):
            raise AssertionError("a refused open must not reach the claim")

    monkeypatch.setattr(v2host, "acquire_session_measurement_pause", _no_pause)
    monkeypatch.setattr(v2host, "release_session_measurement_pause", _no_pause)
    v2host.set_volume_plan_for_tests(_RefusingPlan())

    graph = FakeGraph()
    captured = _real_seam_session(monkeypatch, graph=graph)

    opened = await captured["hooks"].open()

    assert str(opened) == "refused", "the runners' branch never saw the result"
    assert graph.installs == 0, "a graph was swapped in for a session that cannot play"
    assert graph.restores == 0
    v2host.set_volume_plan_for_tests(None)


async def test_a_stage_two_session_swaps_no_graph_for_a_walk_with_no_captures(
    monkeypatch,
):
    """B5: verify-class stages grade through the APPLIED graph.

    A measurement graph installed for a stage that takes no routed capture is
    a full DSP swap and restore that buys nothing and carries every stranding
    exposure an installed graph carries.
    """
    captured: dict[str, Any] = {}
    real_hooks = v2host._volume_hooks

    def _capturing_hooks(camilla_factory, context, *, tuning, **kw):
        captured["tuning"] = tuning
        return real_hooks(camilla_factory, context, tuning=tuning, **kw)

    monkeypatch.setattr(v2host, "_volume_hooks", _capturing_hooks)
    _seed_applied_stage_1_state()
    _stage_2(monkeypatch)
    session = captured["tuning"]

    from jasper.active_speaker.crossover_v2.composition import (
        NoRoutedPhasesGraph,
    )

    assert isinstance(session.seams.graph, NoRoutedPhasesGraph)
    assert await session.seams.graph.install() == "", (
        "a stage that measures through no graph cannot name one"
    )


def _session_from_real_open(monkeypatch, fakes) -> Any:
    """The ``TuningSession`` the real ``_open()`` constructs, against twin seams.

    Two seams are substituted and nothing else: the engine binder (so the five
    slots are the twin's rather than the box's) and the volume hooks (which is
    where ``_open`` hands the session over, and therefore the only place a
    caller can reach the object it built). The preparer itself is real.
    """
    captured: dict[str, Any] = {}
    real_hooks = v2host._volume_hooks

    monkeypatch.setattr(
        v2host, "bind_v2_engine_seams", lambda **_kw: fakes.seams(),
    )

    def _capturing_hooks(camilla_factory, context, *, tuning, **kw):
        captured["tuning"] = tuning
        return real_hooks(camilla_factory, context, tuning=tuning, **kw)

    monkeypatch.setattr(v2host, "_volume_hooks", _capturing_hooks)
    conductor, _state = _stage_1(monkeypatch)
    captured["conductor"] = conductor
    return captured


def test_the_real_preparer_builds_a_session_over_the_five_seams(monkeypatch):
    """The join, end to end: construction, identity, and the declared level.

    This is what makes ``crossover-v2-engine-design.md``'s *"constructed only
    in tests"* false. The session's identity is the RELAY session id — the
    directory name every banked path carries and every reader globs — and its
    one declared level is the context's, because a session measuring at a level
    the host did not declare is the defect the one-level rule deletes.
    """
    from tests.engine_twin import FakeSeams

    fakes = FakeSeams()
    captured = _session_from_real_open(monkeypatch, fakes)
    session = captured["tuning"]

    assert session.session_id == _MINTED_RELAY_SESSION_ID
    # ONE declared level, shared with the conductor the same _open() built.
    assert session.measurement_level_db == captured["conductor"]._session_volume_db
    assert session.measurement_level_db < 0.0, "the hearing clamp is never relaxed"
    assert session.seams.graph is fakes.graph
    assert session.seams.records is fakes.records
    assert not session.is_open, "opening is the run's, not the preparer's"


async def test_a_session_from_the_real_preparer_drives_the_measure_verb(monkeypatch):
    """The bar: a whole session through the real preparer against twin seams.

    Every verb, then a close that gives both slots back — driven on the object
    production actually constructs rather than one a test assembled to look
    like it.
    """
    from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_BASELINE
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from tests.engine_twin import FakeSeams

    fakes = FakeSeams()
    session = _session_from_real_open(monkeypatch, fakes)["tuning"]

    await session.open()
    measured = await session.measure(MeasureSpec(kind=MEASURE_KIND_BASELINE))
    await session.close()

    assert measured.record_ids == session.banked_record_ids
    assert measured.record_ids != ()
    # One install at open plus one prove-or-install per stimulus (MS-13/S6:
    # the idempotent install IS the health check); this walk played one.
    assert fakes.graph.installs == 2 and fakes.graph.restores == 1
    assert not fakes.volume.held, "the claim went back"
    assert not session.is_open
