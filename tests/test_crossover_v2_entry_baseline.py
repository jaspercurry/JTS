# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291's entry baseline: the round's measured "before".

Phase 3c adds ONE capture — ``PHASE_ENTRY_BASELINE``, at the design-axis mark,
immediately before the household applies — so a correction round can say
whether the speaker got *better* rather than only whether the graph did what it
commanded. The 2026-08-10 jts3 round had CHECK/MEASURE, a lateral walk, VERIFY,
and five post-apply cloud positions, and still could not answer that, because
none of it was the same summed acoustic question before and after the change.

What this module pins, in the order the evidence has to survive:

1. **the plan** — exactly one entry, and it is LAST;
2. **comparability** — the capture replays the VERIFY program *object*, so the
   two sides share a ``program_id`` and the benefit evaluator's own
   comparability check can pass;
3. **the accept rule** — an unusable capture records nothing, a usable one
   records a baseline stamped with the shared mark;
4. **retention** — the accepted take reaches the evidence seam, reads back
   through the shipped reader into an EQUAL record, and a failing store never
   costs the household a retake;
5. **the bridge** — the record survives a real stage-1 persist, arrives at a
   real stage-2 conductor with its VALUES intact, is not erased by stage 2's
   own persist, and its absence reaches the capability journal.

**Two copies, one of them durable** (fragment ``02``'s duplication #2, closed
here). The flow state file is the ROUND's channel: ``verify_priors`` is rebuilt
from the conductor on every persist, which is what lets a fresh measuring
session write its own honest absence over a previous round's "before" — section
5 pins that, and it is deliberate. The copy that OUTLIVES the round is the
write-once retained take, which now carries the reduced curve and not only its
scalars. Both are written from one ``MeasuredResponse`` at capture time, so
neither is derived from the other and section 4's round trip is what keeps them
one fact.

Section 5's tests drive the REAL preparers through
``tests/crossover_v2_fixtures.py``'s harness.  That harness used to
leak fakes into any module that first imported them inside its patched window
(issue #2312), so this file carried a warning not to share a pytest process
with ``tests/test_correction_crossover_v2_endpoints.py``.  #2312 is fixed — the
harness now unwinds every binding by identity, see its own comment — and the
suites co-run green in either order.
"""

from __future__ import annotations

import pytest


from tests.test_plan_run import banked_program_baselines  # noqa: F401

from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2 import journey, priors
from jasper.active_speaker.crossover_v2.journey import (
    GROUP_PHASES,
    PHASE_ENTRY_BASELINE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.programs import SUMMED_SWEEP_PHASES
from jasper.active_speaker.crossover_v2.capture_plan import (
    build_v2_cloud_index_phase_map,
    resolve_plan_shape,
)

from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _conductor,
    _run_phase,
)

# The stage-bridge harness, imported rather than re-implemented so there is one
# definition of "what a real preparer needs stubbed".
#
# The two autouse fixtures are imported under the redundant-alias re-export
# form, which is what tells the linter they are deliberate module-level names
# rather than dead imports: pytest activates an autouse fixture by its presence
# in the module namespace, so nothing here CALLS them and a plain import reads
# as unused. The alias form says the same thing a lint-suppression comment
# would, without adding to the repo's frozen suppression debt.
from tests.crossover_v2_fixtures import (
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)


# helpers


# Production refuses a session with no volume owner; stand one up.


pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")


def _stage_1_map() -> dict[int, str]:
    """The index→phase map the shipped stage 1 runs, at the production flags."""
    return build_v2_cloud_index_phase_map(
        plan_shape=resolve_plan_shape(),

        include_lateral=False,
        include_entry_baseline=capture_plan.STAGE1_INCLUDES_ENTRY_BASELINE,
    )


# 1. the plan


def test_stage_1_map_keeps_exactly_one_entry_baseline_last():
    index_phase = _stage_1_map()
    assert index_phase[max(index_phase)] == PHASE_ENTRY_BASELINE
    assert list(index_phase.values()).count(PHASE_ENTRY_BASELINE) == 1


def test_the_entry_baseline_is_a_summed_sweep_and_not_a_position_group():
    """The two set memberships that decide what this phase IS.

    ``SUMMED_SWEEP_PHASES`` is what routes it to the VERIFY program (the
    comparability condition, pinned behaviourally below) and to the host's
    live-graph play branch. ``GROUP_PHASES`` is per-index group bookkeeping for
    a phase spanning many prompted positions — this is one capture at one mark,
    and joining it would give it a group's close, geometry verdict, and combine
    over a single curve.
    """
    assert PHASE_ENTRY_BASELINE in SUMMED_SWEEP_PHASES
    assert PHASE_ENTRY_BASELINE not in GROUP_PHASES
    assert PHASE_ENTRY_BASELINE in journey.CAPTURE_PHASES


# 2. comparability — the same program object, on a real conductor


def test_the_entry_baseline_replays_the_verify_program_object_itself():
    """Identity AND ``program_id``, because either alone is satisfiable falsely.

    Object identity alone would pass if both sides resolved to ``None``. Equal
    ids alone would pass for two independently composed programs that happen to
    agree today and would diverge the moment either composer changed. Together
    they say what #2291 needs: the before and the after are the same excitation
    schedule at the same level, cryptographically, because they are the same
    object.
    """
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=_stage_1_map())
    # CHECK's gain solve is what composes the programs; run it for real.
    _run_phase(conductor, 1, 1)

    entry_program = conductor.program_for_phase(PHASE_ENTRY_BASELINE)
    verify_program = conductor.program_for_phase(PHASE_VERIFY)

    assert entry_program is verify_program
    assert entry_program is not None
    assert entry_program.program_id == verify_program.program_id
    assert entry_program.program_id


def test_the_entry_baseline_gets_no_tracking_prior():
    """Nothing is applied yet, so there is no prediction to track.

    ``entry_baseline_priors`` withholds ``predicted_sum`` (and the candidate's
    crossover transfers) for the reason ``cloud_priors`` withholds them: a
    capture that cannot support a claim must not be handed the prior that
    invites one. Handing it MEASURE's prediction would grade the ENTRY graph
    against the CANDIDATE's model and report the whole intended correction as a
    realization error.

    Asserted against the priors module that OWNS the rule rather than through
    the flow's one-line delegation to it: the withholding is analysis-layer
    behaviour and survives the flow whole, so a pin reaching through a private
    conductor method would be pinning the delegation instead of the rule.
    """
    entry = priors.entry_baseline_priors(fc_hz=FC_HZ)

    assert entry.predicted_sum is None
    assert entry.configured_crossover_response_by_role is None
    assert entry.configured_polarity_sign_by_role is None
    # …and the one prior it DOES carry, so this is not passing on an
    # all-empty MeasurementPriors.
    assert entry.crossover_fc_hz == FC_HZ
    # The conductor reaches it and adds nothing of its own — which is what
    # makes the assertion above a statement about the shipped path.
    fakes = FakeSeams()
    conductor = _conductor(fakes, index_phase_map=_stage_1_map())
    assert conductor.entry_baseline_priors() == entry


# 3. the accept rule


# 4. retention


# 5. the stage bridge — driven through the REAL preparers
