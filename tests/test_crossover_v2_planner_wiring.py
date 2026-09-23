# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The conductor's half of the planner cutover (#2291 Phase 2b).

The planner's own behaviour is pinned in
``tests/test_crossover_v2_intervention_dual_run.py``, and the fixed prescription
is pinned end to end on the banked incident in
``tests/test_crossover_v2_incident_replay.py``. What is left, and what this
module owns, is the seam between them: which measurement objects become which
named planner input, how the returned journal reaches this session's log, and
what the conductor does when the planner refuses.

Each of the three is a place a cutover can go wrong silently — a request built
from the wrong corner still plans, a journal nobody forwards still returns a
plan, and a refusal nobody catches still fails a household's capture.
"""

from __future__ import annotations


import inspect


from jasper.active_speaker.crossover_v2 import planning


# the request: which measurement object becomes which named input


# the journal port


# refusals: the planner declines, the conductor degrades


# the build's own disclosure port (#2361)


def test_log_event_is_called_from_exactly_one_site_in_planning():
    """Pins the module docstring's "One exception" paragraph to a count,
    not prose alone: ``planning.py`` otherwise writes nothing and logs
    nothing itself, exactly like ``intervention.py`` and ``fc_sweep.py`` —
    the SF2 guard below is the one, deliberate exception. A second call
    site landing here quietly would mean the module grew a second one
    without anybody updating that claim. Source-scanned rather than asserted
    behaviorally, because the property is about how many PLACES in the source
    can log, not about what any one call does.
    """
    source = inspect.getsource(planning)
    assert source.count("log_event(") == 1


# the no-candidate precondition: kept, on evidence (#2291 Phase 5c-iii)
