# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One post-apply round, staged: the harness the round suites drive it through.

A fixture library that IS a fixture library, kept OUT of any collected test
module on purpose: the census's recurring finding is test files doubling as
fixture libraries (S7 step b, *"deleting a 'dead' file can break a class-A
survivor"*), and a shared harness that lives in a collected test module is
how that happens.

**Nothing here asserts.** Every function stages a fact — a durable state, a
conductor, a comparable "before", an applied graph, one post-apply capture —
and returns it. The claims stay in the modules that own them.

**What is deliberately NOT here.** The two autouse fixtures
(``_isolated_v2_state``, ``_production_host_seams``) stay imported by each test
module directly: ``pytest`` activates an autouse fixture by its presence in the
COLLECTED module's namespace, so re-exporting them from an uncollected helper
module would silently deactivate them. ``real_bundle`` stays with the receipt
pins that need a real evidence store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jasper.active_speaker import baseline_profile as baseline_profile_mod
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.round_evidence import (
    EntryBaseline,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.contracts import REFERENCE_MARK_DESIGN_AXIS
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status

from tests.crossover_v2_fixtures import _in_room_summed_db, _verify_analysis
from tests.test_crossover_v2_stage_bridge import (
    _COMMANDED_FREQS_HZ,
    _open_prepared,
    _seed_applied_stage_1_state,
    _status,
)

__all__ = [
    "_PREVIOUS_CANDIDATE_FINGERPRINT",
    "_bg_run_async",
    "_consume_verify",
    "_household_sentence",
    "_install_applied_graph",
    "_install_entry_baseline",
    "_post_apply_analysis",
    "_restoring_stage_2",
    "_seed_round_state",
    "_stub_restore_doors",
    "_tracking_curve_change_from_entry",
]


def _install_entry_baseline(conductor: Any, *, scale: float) -> EntryBaseline:
    """Give a stage-2 conductor the "before" stage 1 would have handed it.

    Production rehydrates this from the durable bridge key, and
    ``tests/test_crossover_v2_entry_baseline.py`` owns that path. What a ROUND
    test needs is a baseline that is genuinely *comparable* with the post-apply
    capture it will be differenced against — same program id, same grid, same
    mark — and the only way to get that is to build it from THIS conductor's own
    ``_verify_program``, which does not exist until the conductor does. A
    seeded state file cannot: it would have to guess the program id the
    conductor is about to compose, and a lookalike grades
    ``incomparable_program`` instead of grading the speaker.

    ``scale`` multiplies the fixture's in-room deviation, so a scale ABOVE the
    post-apply capture's is a speaker that measurably improved and one below it
    is a measured regression. Higher deviation is worse.
    """
    measured = measured_response_from_analysis(
        _verify_analysis(
            conductor.program_for_phase(flow.PHASE_VERIFY), summed_db=_in_room_summed_db() * scale,
        ),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    assert measured is not None
    baseline = EntryBaseline.from_measurement(
        measured,
        graph_fingerprint="fp-entry-graph",
        captured_at="2026-08-10T00:00:00Z",
        artifact_ref="entry_baseline_09_a01",
    )
    conductor._measure_entry_baseline = baseline
    return baseline


def _tracking_curve_change_from_entry(
    conductor: Any, *, change_db: float, louder_spike_db: float | None = None,
) -> tuple:
    """A post-apply tracking curve that missed by ``change_db``, both ways.

    ``(freqs, measured, predicted)`` for ``verify_tracking_curve``, built so the
    delta probe's TWO readings of the miss agree and are both flat ``change_db``:

        measured_post − predicted_post                   (the model's departure)
        (measured_post − measured_pre) − commanded       (the anchored excess)

    which needs ``predicted_post == measured_pre + commanded`` — the statement
    that the applied graph's prediction IS the entry measurement plus what the
    apply commands, i.e. a model that was right about the speaker going in.

    Since series-2 D1 the two directional findings are differenced against the
    pre-apply capture, so a fixture claiming "the speaker came out 2 dB quieter
    than it was asked for" has to say that about **two captures of one speaker**.
    Stating it against a flat model curve alone — which is what these fixtures
    did while the finding was ``realized − commanded`` — leaves the direction to
    whatever shape the entry baseline happens to carry, and that shape is the
    fixture's BENEFIT knob, not its direction knob. Production reads one entry
    baseline for both, so a fixture that decouples them is testing a wiring the
    speaker does not have.

    ``louder_spike_db`` adds that much at ONE bin. It is what separates a
    ``model_error`` the #2559 deferral does not spare from a hearing hazard, and
    the separation is the structured-run rule: one bin is enough for
    ``realized_louder_than_commanded`` (unstructured, so the lenience is
    withheld) and far too narrow for ``boost_over_declared_bound`` (which needs
    a 1/3-octave run before it will call anything a hazard). A fixture that
    wants the probe's rollback CLASS to take the graph off — rather than the
    safety axis, which outranks it — has to sit in exactly that gap.

    Requires ``_install_entry_baseline`` to have run.
    """
    import numpy as np

    freqs = np.asarray(_COMMANDED_FREQS_HZ, dtype=float)
    baseline = conductor.measure_entry_baseline
    assert baseline is not None, "install the entry baseline first"
    measured_pre = np.interp(
        freqs,
        np.asarray(baseline.curve.hz, dtype=float),
        np.asarray(baseline.curve.db, dtype=float),
    )
    commanded = conductor.measure_commanded_delta
    assert commanded is not None, "the commanded axis is what change_db is relative to"
    commanded_db = np.interp(
        freqs,
        np.asarray(commanded[0], dtype=float),
        np.asarray(commanded[1], dtype=float),
    )
    predicted = measured_pre + commanded_db
    measured = predicted + change_db
    if louder_spike_db is not None:
        measured = measured.copy()
        measured[len(measured) // 2] += louder_spike_db
    return (freqs, measured, predicted)


def _install_applied_graph(monkeypatch, *, boosts: bool) -> None:
    """Put a real applied profile on the speaker — boosted, or cut-only.

    **The input the PRODUCTION predicate reads, not an injection into the
    conductor.** An earlier version of this helper assigned
    ``conductor._candidate`` directly, and that is exactly how #2318's
    fail-closed cell stayed green over dead code: stage 2 builds a fresh
    conductor with no ``candidate=`` ctor parameter, so the production read was
    always ``None`` and ``boosted`` was always ``False``, while the suite
    supplied by hand the one thing the shipped path never has.

    So this sets the applied-profile SSOT instead, and every link after it is
    production: ``_applied_graph_boosts`` → ``profile_linearization`` (which
    prefers the recomposition snapshot) → ``camilla_yaml.linearization_has_boost``.
    Stubbing the loader is unavoidable — there is no profile on disk in a
    hardware-free test — but it is the boundary of the system, not a step
    inside the rule under test.

    The mapping is the REDUCED ``{role: [filter_dict, ...]}`` shape a frozen
    profile actually carries, deliberately not the unreduced candidate shape:
    passing the latter through ``linearization_filters_by_role`` would be a
    silent ``{}``, which reads as "this graph boosts nothing".
    """
    gain_db = 3.0 if boosts else -3.0
    monkeypatch.setattr(
        baseline_profile_mod,
        "load_applied_baseline_profile_state",
        lambda *a, **k: {
            "candidate_fingerprint": "fp-live-graph",
            "recomposition_snapshot": {
                "linearization": {"woofer": [{"gain": gain_db}]},
            },
        },
    )


def _post_apply_analysis(conductor: Any, *, scale: float = 1.0, max_db: float = 0.9):
    """One post-apply VERIFY analysis on this conductor's own program.

    ``max_db`` drives BOTH the flow's tracking gate and the realization verdict
    (they read the same ``max_db_notch_excluded``), so a value inside
    ``VERIFY_TOLERANCE_DB`` is an accepted capture with a MATCHED realization.
    """
    return _verify_analysis(
        conductor.program_for_phase(flow.PHASE_VERIFY),
        max_db=max_db,
        summed_db=_in_room_summed_db() * scale,
    )


#: The index the harness banks a VERIFY take under when a caller states none.
#: Not a production index — production threads the walk's own — so the take it
#: mints is ``verify_00_a01``.
_HARNESS_VERIFY_INDEX = 0


def _consume_verify(
    conductor: Any, analysis: Any, *, attempt: int = 1,
    index: int = _HARNESS_VERIFY_INDEX, result: Any = None,
) -> Any:
    """Drive the production VERIFY trigger site.

    ``_consume_verify`` is where the Express tier grades its round, and it is
    the real entry point the capture runner calls — not a test-only shim. Reached
    directly because the runner in between is a thread and a websocket.

    **The take this mints under the defaults is a PHANTOM: do not assert on
    it.** ``index``/``result`` default because every caller here drives the
    GRADING trigger rather than the banking, so the banked take comes out
    ``verify_00_a01`` with a ``None`` digest — an identity production never
    mints (its indexes start at 1) and a digest that only means "this call
    passed no bytes". It is banked because banking is what an accepted VERIFY
    does, and suppressing it here would make the harness diverge from the
    production arm on the very branch these tests exercise. Any test that
    wants to READ a banked VERIFY take must state ``index`` and ``result``.

    ``phase`` is stated rather than defaulted: a default would let the
    harness bank under a label the caller never chose — the mislabel
    :meth:`_consume_verify` documents.
    """
    return conductor._consume_verify(
        index, attempt, analysis, result, phase=flow.PHASE_VERIFY,
    )


#: The prior MEASURED candidate the seeded pre-apply stash names — the
#: republish target an adoption restore resolves. Distinct from the stash's
#: own composed-baseline fingerprint so a wrong-key read cannot pass.
_PREVIOUS_CANDIDATE_FINGERPRINT = "fp-previous"


def _seed_round_state(*, previous_candidate: bool = True) -> dict[str, Any]:
    """The durable state a household reaches stage 2 with, post-apply.

    ``previous_candidate`` decides whether the state records a prior measured
    candidate — the one fact the adoption table's ``rollback_available``
    reads, and the fingerprint the restore republishes. ``False`` is every
    first-ever apply.
    """
    state = _seed_applied_stage_1_state()
    # The seeded entry baseline sits on a five-point grid of its own, which
    # cannot be compared with a real capture. Rounds in this module install a
    # comparable one on the conductor; drop the placeholder so a test that
    # forgets is INDETERMINATE rather than quietly graded against a stranger.
    state["verify_priors"]["entry_baseline"] = None
    if previous_candidate:
        state["previous_candidate_fingerprint"] = _PREVIOUS_CANDIDATE_FINGERPRINT
        # The pairing rule's other half: the pointer was recorded by the apply
        # of the candidate this state grades ("fp-stage-1", the seeded
        # published candidate), so the automatic revert is armed.
        state["previous_candidate_displaced_by"] = "fp-stage-1"
    v2host.save_v2_state(state)
    return state


def _bg_run_async(coro: Any, *, timeout: Any = None) -> Any:
    import asyncio

    return asyncio.run(coro)


def _stub_restore_doors(monkeypatch) -> list[int]:
    """Stand in for the two normal-path doors an adoption restore presses.

    The restore is republish-then-apply through the production handlers
    (``bind_delta_probe_rollback``); what a ROUND test needs stubbed is those
    two doors, not a DSP transaction underneath them — the doors have their
    own endpoint suites. Each stub REFUSES any fingerprint other than the
    seeded prior candidate's, so a seam that resolved the wrong identity
    turns every restore outcome into a failed one and the outcome pins say so.

    Returns the counter of how many times the APPLY door — the leg that
    changes the speaker — actually ran.
    """
    from jasper.web import correction_crossover_backend
    from jasper.web import correction_crossover_v2_republish as republish_door

    attempts: list[int] = []

    # Hermetic stand-ins for the doors' surroundings: the displacement gate
    # reads the applied-profile SSOT (absent here unless a test installs one),
    # and the way-back preflight reads the candidate bank (absent in a round
    # test); each answers its production shape for the seeded prior.
    monkeypatch.setattr(
        baseline_profile_mod,
        "load_applied_baseline_profile_state",
        lambda *a, **k: None,
    )

    def _preflight(fingerprint: str) -> str | None:
        return None if fingerprint == _PREVIOUS_CANDIDATE_FINGERPRINT else "not_found"

    monkeypatch.setattr(republish_door, "republish_preflight", _preflight)

    def _republish(raw: Any, **_kwargs: Any) -> dict[str, Any]:
        if str(raw.get("fingerprint") or "") != _PREVIOUS_CANDIDATE_FINGERPRINT:
            raise v2host.CrossoverV2Refused("that candidate cannot be republished")
        return {"status": "republished"}

    def _apply(raw: Any, _run_async: Any, _camilla: Any, *, status: Any,
               ) -> dict[str, Any]:
        del status
        expected = str(raw.get("expected_candidate_fingerprint") or "")
        if expected != _PREVIOUS_CANDIDATE_FINGERPRINT:
            raise v2host.CrossoverV2Refused(
                "the reviewed crossover is no longer current"
            )
        attempts.append(1)
        return {"status": "applied"}

    monkeypatch.setattr(republish_door, "handle_v2_republish", _republish)
    monkeypatch.setattr(v2host, "handle_v2_apply", _apply)
    # The auto-revert reads a fresh status for the apply preflight; hand it
    # the same fixture status the preparers were given.
    monkeypatch.setattr(correction_crossover_backend, "status_payload", _status)
    return attempts


def _restoring_stage_2(monkeypatch) -> tuple[Any, list[int]]:
    """A real stage 2 whose rollback seam can actually complete a restore.

    ``tests/test_crossover_v2_stage_bridge.py``'s ``_stage_2`` passes
    ``run_async=None`` / ``camilla_factory=None``, which is right for a module
    about what crosses the bridge and wrong here: with them the rollback seam
    raises inside the apply door, and every adoption restore would read as a
    failed one. This binds the real seam over stubbed doors
    (:func:`_stub_restore_doors`), and returns a counter of how many times the
    speaker-changing leg actually ran.
    """
    attempts = _stub_restore_doors(monkeypatch)
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=_bg_run_async,
        camilla_factory=lambda: SimpleNamespace(), verify_only=True,
    )
    conductor, _state = _open_prepared(monkeypatch, prepared)
    return conductor, attempts


def _household_sentence(conductor: Any, code: str) -> str:
    """The verdict text the wizard renders, through the production envelope.

    Not the reason code: the code is an internal identity, and the regression
    that matters is a household reading "the new tuning is STILL APPLIED" about
    a speaker that has already been put back. So the assertion has to reach the
    string on the screen.
    """
    v2host.persist_conductor_state(conductor, failure_code=code)
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    })
    return str(envelope["verdict_text"])
