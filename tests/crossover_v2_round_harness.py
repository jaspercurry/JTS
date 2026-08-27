# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One post-apply round, staged: the harness two test modules drive it through.

A fixture library that IS a fixture library. These helpers were private to
``tests/test_crossover_v2_round_wiring.py`` until the round's
restore/undo/anchor pins moved to
``tests/test_crossover_v2_undo_and_anchor.py`` — a subject that
``docs/REFACTOR-TUNING-2026-08.md`` §3 marks *"not a target. Ever."* and that
therefore must not be sitting inside a suite the strangler will dissolve.
Putting the harness here rather than importing it from one test module into
another is the point: the census's recurring finding is test files doubling as
fixture libraries (S7 step b, *"deleting a 'dead' file can break a class-A
survivor"*), and a shared harness that lives in a collected test module is how
that happens.

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
from jasper.active_speaker.crossover_v2_flow import REFERENCE_MARK_DESIGN_AXIS
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_fixtures import _in_room_summed_db, _verify_analysis
from tests.test_crossover_v2_stage_bridge import (
    _COMMANDED_FREQS_HZ,
    _open_prepared,
    _seed_applied_stage_1_state,
    _status,
)

__all__ = [
    "_bg_run_async",
    "_consume_verify",
    "_household_sentence",
    "_install_applied_graph",
    "_install_entry_baseline",
    "_post_apply_analysis",
    "_restored_ok",
    "_restoring_stage_2",
    "_seed_round_state",
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


def _consume_verify(
    conductor: Any, analysis: Any, *, attempt: int = 1,
    index: int = 0, result: Any = None,
) -> Any:
    """Drive the production VERIFY trigger site.

    ``_consume_verify`` is where the Express tier grades its round, and it is
    the real entry point the relay runner calls — not a test-only shim. Reached
    directly because the runner in between is a thread and a websocket.

    ``index`` and ``result`` are the capture's identity and its bytes, which
    VERIFY gained when it started banking a take of its own. Defaulted because
    every caller here is driving the GRADING trigger, not the banking: a
    ``None`` result banks a take that honestly carries no digest, which is the
    same answer any capture with no bytes gets.
    """
    return conductor._consume_verify(index, analysis, result, attempt=attempt)


def _seed_round_state(*, anchor: bool = True) -> dict[str, Any]:
    """The durable state a household reaches stage 2 with, post-apply.

    ``anchor`` decides whether an Undo target exists. The stashed profile
    carries no ``source.topology_fingerprint``, which the shipped predicate
    allows through (a stash that predates the fingerprint is validated by the
    restore path itself) — so this is a genuinely valid anchor, not one that
    passes by skipping a check nobody could satisfy here.
    """
    state = _seed_applied_stage_1_state()
    # The seeded entry baseline sits on a five-point grid of its own, which
    # cannot be compared with a real capture. Rounds in this module install a
    # comparable one on the conductor; drop the placeholder so a test that
    # forgets is INDETERMINATE rather than quietly graded against a stranger.
    state["verify_priors"]["entry_baseline"] = None
    if anchor:
        state["pre_apply_profile"] = {"candidate_fingerprint": "fp-previous"}
    v2host.save_v2_state(state)
    return state


async def _restored_ok(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """The DSP half of Undo, stubbed — and ONLY that half.

    Everything else on the restore path runs for real: the anchor predicate,
    the refusal, ``observe_restore``'s durable clear, and therefore the
    non-idempotence a second Undo meets. That is what makes the exactly-once
    pin below discriminating — a once-guard removed from
    ``bind_delta_probe_rollback`` really does produce the false
    "still applied" sentence, because the second call really does refuse.
    """
    return {"status": "restored"}


def _bg_run_async(coro: Any, *, timeout: Any = None) -> Any:
    import asyncio

    return asyncio.run(coro)


def _restoring_stage_2(monkeypatch) -> tuple[Any, list[int]]:
    """A real stage 2 whose rollback seam can actually complete a restore.

    ``tests/test_crossover_v2_stage_bridge.py``'s ``_stage_2`` passes
    ``run_async=None`` / ``camilla_factory=None``, which is right for a module
    about what crosses the bridge and wrong here: with them the rollback seam
    raises before it reaches ``handle_v2_restore``, and every adoption restore
    would read as a failed one. This binds the real endpoint behind a stubbed
    DSP leg, and returns a counter of how many times that leg actually ran.
    """
    attempts: list[int] = []

    async def _counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        return await _restored_ok(*args, **kwargs)

    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _counted,
    )
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
        "crossover_v2": v2host.crossover_v2_status_block(),
    })
    return str(envelope["verdict_text"])
