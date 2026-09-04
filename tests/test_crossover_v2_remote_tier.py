# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The remote commission tier — Full's walk, driven by an external positioner.

Hardware-free. Three things are being pinned, and they are deliberately
separate concerns:

* the SHAPE (a fixed (N, M) whose M drops exactly the axis a positioner cannot
  reach) and the ANGLES derived from the same prompt table the hand-walked
  tiers read;
* the position GATE, which replaces the tap a hand-walked pose gets — held on
  the shipped ``CaptureBeginDeferred`` soft-hold, so no capture-page change is
  involved; and
* the promise that adding all of it changed NOTHING for ``full`` / ``express``.
  That last one is load-bearing: the golden wire digests in
  ``tests/crossover_v2_fixtures`` already prove byte-identity, and the pins here
  say in words what those digests say in hashes.
"""

from __future__ import annotations

import dataclasses
import inspect
import io
import json
import logging
import re
import secrets
import threading
import contextlib
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_envelope_v2 import (
    _TIER_CLAIMS,
    _TIER_LABELS,
    _done_nudges,
    _tier_choice_actions,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_MEASURE
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_GEOMETRY_RETAKE_UNREACHABLE,
    REASON_REGISTRY,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_COUNTDOWN_S,
    AUTO_ADVANCE_TAP,
    POSITION_DEG_KEY,
    POSITION_ROLE_KEY,
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR,
    TIER_EXPRESS,
    TIER_FULL,
    TIER_REMOTE,
    CrossoverV2FlowError,
    build_v2_capture_plan,
    build_v2_verify_capture_plan,
    position_angle_deg,
    remote_cloud_verify_positions,
    resolve_plan_shape,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)
from jasper.web._common import CSRF_COOKIE_NAME
from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.web.correction_crossover_v2 import (
    POSITION_HOLD_CODE,
    POSITION_HOLD_EXPIRED_CODE,
    POSITION_READY_ENDPOINT,
    POSITION_TARGET_MISSING_CODE,
    REMOTE_POSITION_HOLD_BUDGET_S,
    SESSION_CEILING_EXPIRED_CODE,
    PositionGate,
)

from tests.crossover_v2_fixtures import (
    CLOUD_MEASURE_INDEXES,
    CLOUD_VERIFY_INDEXES,
    FC_HZ,
    STAGE2_MAP,
    VERIFY_INDEX,
    FakeSeams,
    _cloud_conductor,
    _conductor,
    _lock,
    _run_phase,
    _walk,
)

# The stage-bridge harness: one definition of "what a real preparer needs
# stubbed", borrowed exactly as ``tests/test_crossover_v2_round_wiring.py``
# borrows it, so the journal pin at the bottom of this file reads a REAL
# ``prepare_v2_session`` rather than a restatement of its source. The two
# autouse fixtures come with it by name, under the redundant-alias form that
# says the module-level name is deliberate.
from tests.test_crossover_v2_stage_bridge import (
    _isolated_v2_state as _isolated_v2_state,
    _open_prepared,
    _production_host_seams as _production_host_seams,
    _status,
)

HAND_WALKED = (TIER_FULL, TIER_EXPRESS)

#: The bearings the target run is specified in — the whole point of the tier, so
#: they are written down here ONCE as the acceptance criterion and everything
#: else in this file derives from the product code.
#:
#: These are the SEQUENCE of stops in walk order, not the SET of angles served —
#: an angle can appear twice because two adjacent stops share a pose. Stage 2
#: opens on two of them since the 2026-08-24 geometry ruling: VERIFY's anchor at
#: the mark, whose sweep the tracking verdict consumes, and then the first pose
#: of ``CLOUD_VERIFY_POSE_PROMPTS``, whose sweep joins the post-apply GROUP. The
#: microphone does not move between them, and ``jasper-angle-capture serve``'s
#: ``--expect-angles`` compares SETS, so a repeat adds nothing to state there.
STAGE1_ANGLES = (0, -7, 7, -22, 22, 0)
STAGE2_ANGLES = (0, 0, -7, 7, -22, 22)



# Production refuses a session with no volume owner; stand one up.
pytestmark = pytest.mark.usefixtures("a_process_with_a_volume_owner")

def _stage1_of(shape):
    """The shipped stage-1 plan for a RESOLVED shape — the flags are the
    shipped ones so a plan built here is the plan a session runs."""
    return build_v2_capture_plan(
        flow._DISPLAY_ROLES_BANDS,
        flow._DISPLAY_FC_HZ,
        plan_shape=shape,
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )


def _stage1(tier):
    return _stage1_of(resolve_plan_shape(tier))


def _stage2_of(shape):
    """The shipped stage-2 plan for a RESOLVED shape — the twin of
    :func:`_stage1_of`, and the only builder that reaches
    ``_positioned_prompt`` in a shipped shape (stage 1's cloud group is off)."""
    return build_v2_verify_capture_plan(FC_HZ, plan_shape=shape)


def _stage2(tier):
    return _stage2_of(resolve_plan_shape(tier))


def _entry(degrees, role=POSITION_ROLE_ONAX):
    """The one thing the gate reads off a plan entry."""
    return SimpleNamespace(
        screen={POSITION_DEG_KEY: str(degrees), POSITION_ROLE_KEY: role}
    )


# --------------------------------------------------------------------------- #
# the shape
# --------------------------------------------------------------------------- #


def test_remote_is_a_fixed_shape_that_takes_fulls_stage_1():
    """Remote's stage 1 IS Full's stage 1 — the same walk, a different operator
    — so its N must be Full's, and only its post-apply M differs."""
    remote = resolve_plan_shape(TIER_REMOTE)
    full = resolve_plan_shape(TIER_FULL)
    assert remote.tier == TIER_REMOTE
    assert remote.cloud_measure_positions == full.cloud_measure_positions
    assert remote.cloud_verify_positions == remote_cloud_verify_positions()
    # Never LONGER than Full's — ``remote_cloud_verify_positions`` clamps to
    # Full's default, and a remote walk that asked for more than the tier it
    # borrows stage 1 from would be a shape nobody chose. Equal is the shipped
    # state since 2026-08-18, and permanently so since the 2026-08-24 geometry
    # ruling gave the post-apply group its own vertical-free pose table: there
    # is no vertical row left for remote to stop before.
    assert remote.cloud_verify_positions <= full.cloud_verify_positions
    # A named shape, not a configurable range: a caller stating a different
    # count is a bug, not a preference. The count asked for here is deliberately
    # NOT the tier's own — it has to disagree for the refusal to be under test.
    other = flow.DEFAULT_CLOUD_VERIFY_POSITIONS + 1
    with pytest.raises(CrossoverV2FlowError, match="the remote tier is a fixed shape"):
        resolve_plan_shape(TIER_REMOTE, cloud_verify_positions=other)
    # …and express's identical refusal still names EXPRESS, not a shared word.
    with pytest.raises(CrossoverV2FlowError, match="the express tier is a fixed shape"):
        resolve_plan_shape(TIER_EXPRESS, cloud_verify_positions=other)


def test_an_unknown_tier_is_still_refused_and_remote_is_admitted():
    """Adding a tier must widen the allowlist, never open it."""
    assert flow.normalize_tier(" REMOTE ") == TIER_REMOTE
    assert flow.normalize_tier(None) == TIER_FULL
    with pytest.raises(CrossoverV2FlowError) as excinfo:
        flow.normalize_tier("turbo")
    # The refusal enumerates what this build actually has, so the message a
    # caller reads cannot go stale behind the tuple.
    assert "remote" in str(excinfo.value)


def test_remotes_verify_walk_is_derived_as_fulls_minus_the_vertical():
    """``remote_cloud_verify_positions`` is DERIVED off the POST-APPLY pose
    table: it is the longest prefix with no vertical pose, so editing that table
    moves the number instead of stranding it.

    RE-DERIVED 2026-08-24: the derivation used to run over
    ``CLOUD_POSITION_PROMPTS``, the pre-apply table, because both groups walked
    it. The geometry ruling gave the post-apply group its own table, which is
    vertical-free BY CONSTRUCTION — so the subtraction is a no-op today and
    remote's walk IS Full's. That is the property under test: the derivation
    still reads the table the walk actually takes, so the day a vertical row is
    added to it remote shortens rather than aiming a positioner at a pose it
    cannot reach.
    """
    positions = remote_cloud_verify_positions()
    walked = flow.CLOUD_VERIFY_POSE_PROMPTS[: positions - 1]
    assert all(p.role != POSITION_ROLE_XOVR for p in walked)
    # Nothing is left over: there is no vertical row for the prefix to stop
    # before, so remote walks the whole post-apply table.
    assert walked == flow.CLOUD_VERIFY_POSE_PROMPTS
    assert positions == flow.DEFAULT_CLOUD_VERIFY_POSITIONS
    # The wide-offset guarantee still holds on the walk (this is why the floor
    # exists, and why the derivation refuses below it).
    assert sum(1 for p in walked if p.wide) >= 2
    assert positions >= flow.MIN_CLOUD_VERIFY_POSITIONS


def test_remotes_stage_1_n_states_the_assumption_that_makes_it_safe(monkeypatch):
    """N4. Remote takes Full's N only because the shipped stage 1 walks the
    LATERAL poses; the ``[:N - 1]`` prefix of the cloud table contains vertical
    rows at that N. Flipping the flag back on must trip a NAMED refusal that
    says what to do, not an incidental raise from the angle helper."""
    assert flow.remote_cloud_measure_positions() == flow.DEFAULT_CLOUD_MEASURE_POSITIONS
    # The flag and the function that reads it both live in
    # ``crossover_v2.capture_plan``; the flow only re-exports the name.
    monkeypatch.setattr(capture_plan, "STAGE1_INCLUDES_CLOUD_MEASURE", True)
    with pytest.raises(CrossoverV2FlowError, match="cannot walk a pre-apply cloud"):
        flow.remote_cloud_measure_positions()
    # The refusal names the fix, not just the symptom.
    with pytest.raises(CrossoverV2FlowError, match="remote_cloud_verify_positions"):
        resolve_plan_shape(TIER_REMOTE)


# --------------------------------------------------------------------------- #
# the angles
# --------------------------------------------------------------------------- #


def test_the_angle_is_derived_from_the_offset_and_signed_by_the_bearing():
    """One number, two statements of it, and they cannot disagree: the angle
    comes from ``offset_cm`` at the nominal mark distance, and its sign from the
    row's own LEFT/RIGHT word."""
    for prompt in flow.CLOUD_POSITION_PROMPTS + flow.LATERAL_POSE_PROMPTS:
        if prompt.role == POSITION_ROLE_XOVR:
            continue
        degrees = position_angle_deg(prompt)
        if prompt.offset_cm == 0:
            assert degrees == 0
            continue
        # LEFT rows read negative, RIGHT rows positive — checked against the
        # row's rendered word, which is the thing a reader would trust.
        assert ("LEFT" in prompt.headline) == (degrees < 0)
        assert ("RIGHT" in prompt.headline) == (degrees > 0)
        # The magnitude really is the bearing to that offset, not a table.
        expected = round(
            flow.math.degrees(
                flow.math.atan2(prompt.offset_cm / 100.0, flow.MARK_DISTANCE_M)
            )
        )
        assert abs(degrees) == expected


def test_an_unsigned_lateral_pose_is_refused_as_loudly_as_a_vertical_one():
    """S4b. The geometry-locked retake builds its pose by hand
    (``_prompt_shown_for``), so it carries an offset and NO side. Before this
    guard that read back as 0° — "already on the design axis" — so a driver
    would have been told to stay put for a capture the plan believed was 75 cm
    off-axis, and the evidence would have recorded an offset the microphone
    never had."""
    unsigned = flow.CloudPositionPrompt(
        headline="Same measurement, wider spot.",
        offset_cm=flow.GEOMETRY_RETRY_OFFSET_CM,
        role=POSITION_ROLE_OFFAX,
    )
    assert unsigned.lateral_sign == 0
    with pytest.raises(CrossoverV2FlowError, match="declares no side"):
        position_angle_deg(unsigned)
    # An at-mark pose is unsigned too, and that one is genuinely 0°.
    assert position_angle_deg(flow.LATERAL_MARK_PROMPT) == 0


def test_a_vertical_pose_has_no_bearing_and_says_so():
    """Silently answering 0° would aim a positioner at the mark while the plan
    believed it had sampled the crossover axis."""
    vertical = next(
        p for p in flow.CLOUD_POSITION_PROMPTS if p.role == POSITION_ROLE_XOVR
    )
    with pytest.raises(CrossoverV2FlowError, match="no horizontal bearing"):
        position_angle_deg(vertical)


def test_the_remote_walks_are_the_specified_bearings():
    """The acceptance criterion, stated as the angles a driver will be asked
    for — and read back off the PLAN, not off the helper that builds it.

    No stage-1 plan builds the lateral group any more, so the shipped remote
    stage 1 asks a positioner for nothing but the axis. The bearings it would
    ask for if one WERE included are still the acceptance criterion — an
    operator's staged angle walk feeds a positioner the same table, and a walk
    that quietly stopped matching ``STAGE1_ANGLES`` would be discovered by the
    first one taken — so they are asserted against a directly-built
    lateral-included plan rather than deleted with the group.
    """
    stage1 = [int(e.screen[POSITION_DEG_KEY]) for e in _stage1(TIER_REMOTE).entries]
    stage2 = [int(e.screen[POSITION_DEG_KEY]) for e in _stage2(TIER_REMOTE).entries]
    # Shipped: CHECK, MEASURE and the entry baseline, every one on the axis.
    assert stage1 == [0, 0, 0]
    lateral_included_plan = build_v2_capture_plan(
        flow._DISPLAY_ROLES_BANDS,
        flow._DISPLAY_FC_HZ,
        plan_shape=resolve_plan_shape(TIER_REMOTE),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=True,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    armed = [
        int(e.screen[POSITION_DEG_KEY]) for e in lateral_included_plan.entries
    ]
    # CHECK and MEASURE are design-axis captures ahead of the walk itself.
    assert armed[:2] == [0, 0]
    assert tuple(armed[2:-1]) == STAGE1_ANGLES
    assert armed[-1] == 0  # the entry baseline, back on the axis
    assert tuple(stage2) == STAGE2_ANGLES
    # No vertical anywhere: that is the tier's whole coverage claim.
    for plan in (_stage1(TIER_REMOTE), _stage2(TIER_REMOTE)):
        assert all(
            e.screen[POSITION_ROLE_KEY] != POSITION_ROLE_XOVR for e in plan.entries
        )


def test_a_remote_entry_keeps_the_role_the_wide_offset_rule_gives_it():
    """The role is NOT re-derived from the angle — it rides the same
    ``WIDE_OFFSET_MIN_CM`` rule a hand-walked pose gets, so the durable evidence
    a remote session records stays comparable with Full's."""
    roles = {
        abs(int(e.screen[POSITION_DEG_KEY])): e.screen[POSITION_ROLE_KEY]
        for e in _stage2(TIER_REMOTE).entries
    }
    assert roles == {0: POSITION_ROLE_ONAX, 7: POSITION_ROLE_ONAX,
                     22: POSITION_ROLE_OFFAX}


def test_every_remote_prompt_names_its_angle():
    """A prompt that still says "12 cm to the LEFT" is an instruction a
    positioner cannot follow."""
    for plan in (_stage1(TIER_REMOTE), _stage2(TIER_REMOTE)):
        for entry in plan.entries:
            degrees = int(entry.screen[POSITION_DEG_KEY])
            if degrees == 0:
                continue
            assert f"{degrees:+d}°" in entry.screen["title"]
            assert not re.search(r"\d+\s*cm", entry.screen["title"])


# --------------------------------------------------------------------------- #
# auto-advance + the hand-walked tiers' byte-identity
# --------------------------------------------------------------------------- #


def test_a_remote_plan_auto_advances_every_entry():
    for plan in (_stage1(TIER_REMOTE), _stage2(TIER_REMOTE)):
        for entry in plan.entries:
            assert entry.screen["auto_advance"] == AUTO_ADVANCE_COUNTDOWN
            assert entry.screen["countdown_s"] == str(AUTO_ADVANCE_COUNTDOWN_S)


@pytest.mark.parametrize("tier", HAND_WALKED)
def test_a_hand_walked_plan_still_taps_every_entry_and_names_no_angle(tier):
    """The regression this whole change had to avoid. Making the policy
    shape-derived must leave both shipped tiers exactly as they were — no
    countdown, no countdown_s, and no position keys at all."""
    for plan in (_stage1(tier), _stage2(tier)):
        for entry in plan.entries:
            assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
            assert "countdown_s" not in entry.screen
            assert POSITION_DEG_KEY not in entry.screen
            assert POSITION_ROLE_KEY not in entry.screen


def test_the_recovery_re_verify_has_no_tier_and_keeps_its_tap():
    entry = build_v2_verify_capture_plan(FC_HZ).entries[0]
    assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
    assert "countdown_s" not in entry.screen
    assert POSITION_DEG_KEY not in entry.screen


def test_a_remote_stage_2_anchor_drops_the_confirm_tap_it_cannot_answer():
    """``entryConfirmsBeforeArming`` (capture-page/js/main.js) holds the tone
    until somebody taps whenever ``confirm_title`` is present. Carrying it into
    an unattended session would park the anchor forever; the position gate makes
    the same promise instead."""
    remote_anchor = _stage2(TIER_REMOTE).entries[0].screen
    assert "confirm_title" not in remote_anchor
    assert "confirm_body" not in remote_anchor
    for tier in HAND_WALKED:
        anchor = _stage2(tier).entries[0].screen
        assert anchor["confirm_title"] == "Back on the mark, holding still?"


def test_remote_costs_no_more_attempts_than_full_does():
    remote = resolve_plan_shape(TIER_REMOTE)
    full = resolve_plan_shape(TIER_FULL)
    assert remote.max_attempts <= full.max_attempts
    for plan in (_stage1(TIER_REMOTE), _stage2(TIER_REMOTE)):
        assert plan.max_attempts <= MAX_CAPTURE_PLAN_ATTEMPTS


# --------------------------------------------------------------------------- #
# the position gate
# --------------------------------------------------------------------------- #


def test_the_gate_defers_until_the_driver_releases_and_then_admits():
    """The whole choreography, in one test: hold → publish → release → admit."""
    gate = PositionGate()
    entry = _entry(-7, POSITION_ROLE_ONAX)
    with pytest.raises(CaptureBeginDeferred) as held:
        gate.gate(3, 3, entry)
    assert held.value.code == POSITION_HOLD_CODE
    pending = gate.pending()
    assert pending["index"] == 3
    assert pending["attempt"] == 3
    assert pending["degrees"] == -7
    assert pending["role"] == POSITION_ROLE_ONAX
    assert pending["action"]["endpoint"] == POSITION_READY_ENDPOINT
    assert pending["action"]["body"] == {"index": 3, "degrees": -7}
    # The phone re-posts the SAME begin throughout a hold; each one defers again
    # without spending anything.
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 3, entry)
    gate.release(3)
    assert gate.pending() is None
    gate.gate(3, 3, entry)  # admitted — no raise
    # A released capture stays released across the re-posts still in flight.
    gate.gate(3, 3, entry)


def test_the_gate_holds_each_attempt_separately():
    """Gating is per ``(index, attempt)``, so a retake re-gates rather than
    inheriting the previous attempt's release — the arm has to be confirmed for
    the capture that is about to run, not for one that already did."""
    gate = PositionGate()
    entry = _entry(22, POSITION_ROLE_OFFAX)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(5, 5, entry)
    gate.release(5)
    gate.gate(5, 5, entry)
    # Same index, next attempt: a fresh hold.
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(5, 6, entry)


def test_a_stale_release_cannot_open_the_next_position():
    """The one hazard an untargeted latch would introduce: a driver retrying its
    POST after the capture already began must not release the NEXT angle."""
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 3, _entry(-7))
    gate.release(3)
    gate.gate(3, 3, _entry(-7))
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(4, 4, _entry(7))
    # The retry still names position 3, which is no longer what is pending.
    with pytest.raises(ValueError, match="measurement 4 is waiting, not 3"):
        gate.release(3)
    # …and 4 is still held, so nothing was quietly admitted.
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(4, 4, _entry(7))


def test_releasing_nothing_is_refused_rather_than_remembered():
    """A release that arrives with no hold open must not be banked against a
    future one — that would admit the next capture without a report."""
    gate = PositionGate()
    with pytest.raises(ValueError, match="no measurement is waiting"):
        gate.release(1)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 1, _entry(0))


def test_a_hold_whose_driver_never_answers_expires_loudly():
    """A hold is unbounded as far as the transport is concerned — the phone
    re-posts forever and rearms the runner's clock — so a dead driver would pin
    the measurement volume, the paused voice, and the capture slot indefinitely.
    The gate ends it instead of holding for good."""
    now = {"t": 0.0}
    gate = PositionGate(clock=lambda: now["t"])
    entry = _entry(-22, POSITION_ROLE_OFFAX)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(2, 2, entry)
    now["t"] = REMOTE_POSITION_HOLD_BUDGET_S  # exactly at the budget: still held
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(2, 2, entry)
    now["t"] = REMOTE_POSITION_HOLD_BUDGET_S + 1.0
    with pytest.raises(CaptureBeginRefused) as refused:
        gate.gate(2, 2, entry)
    assert refused.value.code == POSITION_HOLD_EXPIRED_CODE
    # The expired hold stops being advertised, so the envelope cannot keep
    # asking a driver to move an arm for a capture that has been refused.
    assert gate.pending() is None


def test_a_walk_that_outlives_its_ceiling_is_named_rather_than_left_generic():
    """The CUMULATIVE bound, named (issue #2506).

    ``REMOTE_POSITION_HOLD_BUDGET_S`` catches a driver that STOPPED. It cannot
    catch one that answers every position too slowly to finish: stage 1 gates
    nine begins under a 2520 s ceiling, so ~280 s a move exhausts the session
    with no single hold anywhere near 600 s. That death used to limp on to the
    capture session's own expiry and reach the household as ``capture_timeout`` — a
    claim about a transport that never failed. It ends here instead, by name.
    """
    now = {"t": 0.0}
    gate = PositionGate(clock=lambda: now["t"])
    entry = _entry(-7, POSITION_ROLE_ONAX)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(9, 9, entry)
    # A driver that is merely slow: nowhere near its own hold budget.
    now["t"] = 280.0
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(9, 9, entry)
    assert now["t"] < REMOTE_POSITION_HOLD_BUDGET_S
    gate.note_session_ceiling_expired()
    with pytest.raises(CaptureBeginRefused) as refused:
        gate.gate(9, 9, entry)
    assert refused.value.code == SESSION_CEILING_EXPIRED_CODE
    assert refused.value.code != POSITION_HOLD_EXPIRED_CODE
    # …and the refused hold stops being advertised, so a driver is not still
    # being asked to move an arm for a capture that will never run.
    assert gate.pending() is None


def test_the_modal_ceiling_death_announces_no_hold_it_is_about_to_refuse(caplog):
    """The shape a real slow-driver run actually dies in.

    The ceiling is crossed while the session is BETWEEN holds far more often
    than during one: the driver releases position N, the page posts the begin
    for N+1, and that begin is the first thing to meet the latch. Deciding the
    refusal before publishing keeps the journal honest — one
    ``session_ceiling_expired``, rather than a ``position_pending`` announcing
    a hold that is refused in the same breath and never waited a second.
    """
    logger_name = "jasper.web.correction_crossover_v2"
    # POSITIVE CONTROL FIRST. ``position_pending`` is an INFO line, so a
    # WARNING-level capture would swallow it and the absence assertion below
    # would pass against ANY implementation — instrument silence read as
    # evidence. Prove the line reaches this capture before trusting its absence.
    healthy = PositionGate()
    with caplog.at_level(logging.INFO, logger=logger_name):
        with pytest.raises(CaptureBeginDeferred):
            healthy.gate(4, 4, _entry(7))
    assert any(
        "crossover_v2_position_pending" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]

    caplog.clear()
    gate = PositionGate()
    with caplog.at_level(logging.INFO, logger=logger_name):
        gate.note_session_ceiling_expired()
        with pytest.raises(CaptureBeginRefused) as refused:
            gate.gate(4, 4, _entry(7))  # a hold this gate has never opened
    assert refused.value.code == SESSION_CEILING_EXPIRED_CODE
    lines = [rec.getMessage() for rec in caplog.records]
    assert not any("crossover_v2_position_pending" in ln for ln in lines), lines
    ceiling = [ln for ln in lines if "crossover_v2_session_ceiling_expired" in ln]
    assert len(ceiling) == 1, lines
    assert "waited_s=0.0" in ceiling[0]
    assert gate.pending() is None


def test_a_stalled_driver_keeps_its_own_name_when_both_bounds_are_past():
    """Order is load-bearing. On a walk long enough to reach the ceiling BOTH
    bounds can be past at once, and "nothing answered this position" is the
    more actionable of the two sentences — so the per-hold budget is tested
    first and the cumulative name never absorbs a genuine stall."""
    now = {"t": 0.0}
    gate = PositionGate(clock=lambda: now["t"])
    entry = _entry(22, POSITION_ROLE_OFFAX)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(6, 6, entry)
    gate.note_session_ceiling_expired()
    now["t"] = REMOTE_POSITION_HOLD_BUDGET_S + 1.0
    with pytest.raises(CaptureBeginRefused) as refused:
        gate.gate(6, 6, entry)
    assert refused.value.code == POSITION_HOLD_EXPIRED_CODE


def test_the_ceiling_latch_leaves_an_already_released_begin_alone():
    """The latch ends a HOLD; it is not a second admission check.

    A begin the driver already released is past this gate, and the measurement
    volume is the thing that fails closed on a session past its ceiling
    (``SessionVolumePlan.assert_ready`` refuses a stale-active plan). Refusing
    here as well would put a second owner on that decision.
    """
    gate = PositionGate()
    entry = _entry(0)
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 3, entry)
    gate.release(3)
    gate.note_session_ceiling_expired()
    gate.gate(3, 3, entry)  # admitted — no raise


def test_a_hold_carries_the_plans_own_words_and_says_who_releases_it():
    """What a BROWSER needs to render a hold it cannot tap through (#2881).

    Built from real shipped plans rather than a hand-made ``screen`` bag,
    because the claim is that the gate re-publishes the capture plan's copy
    rather than composing a second one: an edit to the prompt table has to
    reach this screen, and a test that supplied its own sentences would keep
    passing while the two drifted.

    ``hand_released`` is the question a surface offering a release control must
    answer, and it is NOT the transport. Both shapes below are gated and both
    publish the identical hold; only one of them has a person standing at the
    speaker, and the plan already states which per entry.
    """
    remote = resolve_plan_shape(TIER_REMOTE)
    by_hand = dataclasses.replace(
        resolve_plan_shape(TIER_FULL), hand_released_positions=True
    )
    for shape, hand_released in ((by_hand, True), (remote, False)):
        entry = _stage2_of(shape).entries[0]
        gate = PositionGate()
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(1, 1, entry)
        pending = gate.pending()
        assert pending["hand_released"] is hand_released
        # Verbatim, not paraphrased: same strings, same three slots.
        assert pending["prompt"] == {
            "progress": entry.screen["progress"],
            "title": entry.screen["title"],
            "body": entry.screen.get("body", ""),
        }
        # A person has to be able to act on it — an empty headline is a silent
        # hold, which is the failure this whole block exists to prevent.
        assert pending["prompt"]["title"].strip()
        # The machine facts the external driver reads are untouched beside it.
        assert pending["degrees"] == int(entry.screen[POSITION_DEG_KEY])
        assert pending["action"]["endpoint"] == POSITION_READY_ENDPOINT


def _label_at(degrees):
    """The release action's label for one bearing."""
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 1, _entry(degrees))
    return gate.pending()["action"]["label"]


def test_the_release_label_signs_a_bearing_but_never_signs_zero():
    """The two properties, not the sentence — this is a button a HOUSEHOLD
    presses now, so its wording will be edited and a frozen string would only
    be re-typed here.

    The sign distinguishes the two off-axis sides, so it has to survive. At
    the design axis it distinguishes nothing and "+0°" reads as a typo beside
    a prompt that calls the same position the design axis.
    """
    assert "0" in _label_at(0)
    assert "+0" not in _label_at(0)
    assert _label_at(7) != _label_at(-7)
    assert "+7" in _label_at(7)


def test_hand_released_tracks_the_entrys_own_advance_policy():
    """The derivation, pinned against its two inputs rather than its output.

    ``hand_released`` is read off the entry's ``auto_advance`` — the plan's own
    statement of whether a person is expected to act — so a shape that stops
    advancing by tap stops offering a browser release in the same edit. The
    mutation that matters is the middle case: a hold whose entry says
    ``countdown`` must never claim a hand.
    """
    for policy, expected in (
        (AUTO_ADVANCE_TAP, True),
        (AUTO_ADVANCE_COUNTDOWN, False),
        ("", False),
    ):
        gate = PositionGate()
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(1, 1, SimpleNamespace(screen={
                POSITION_DEG_KEY: "0",
                POSITION_ROLE_KEY: POSITION_ROLE_ONAX,
                "auto_advance": policy,
            }))
        assert gate.pending()["hand_released"] is expected


def test_the_ceiling_refusal_is_a_registry_code_the_teardown_leaves_published():
    """Both halves of what makes a gate refusal honest, for the new code.

    The teardown arm trusts a gate refusal's own code only when the registry
    knows it (else it degrades to ``capture_timeout``), and re-posts a terminal
    host event only for codes the runner has NOT already published — so a code
    missing from either set reaches the household as the transport lie the
    other two gate codes exist to avoid.
    """
    from jasper.web.correction_crossover_v2 import POSITION_GATE_TERMINAL_CODES

    assert SESSION_CEILING_EXPIRED_CODE in REASON_REGISTRY
    assert SESSION_CEILING_EXPIRED_CODE in POSITION_GATE_TERMINAL_CODES
    spec = REASON_REGISTRY[SESSION_CEILING_EXPIRED_CODE]
    assert spec.retry_budget == 0
    # The sentence must not be the per-hold one: the whole point is that
    # nothing stalled.
    assert spec.message != REASON_REGISTRY[POSITION_HOLD_EXPIRED_CODE].message


#: Words that name ONE of the two movers. The gate's own copy may not use any
#: of them: a hand-released round and an arm round reach the same three
#: sentences, so a sentence that names either mover is false to the other half
#: of its readership (#2879 round-3 nit 4). Matched on word boundaries so
#: ``arrives`` and ``warm`` are not false hits, and deliberately SMALL — it is
#: the minimal set the three comments asserting this invariant actually name
#: (``refusal_copy``'s "the copy below therefore names neither mover", the
#: budget constant's "covers BOTH movers", and ``PositionGate``'s "never asks
#: WHO"), not a general banned-words list.
_MOVER_WORDS = ("positioner", "positioners", "driver", "drivers", "arm", "arms")


def test_the_gates_three_refusals_name_neither_mover():
    """The invariant three comments assert and nothing pinned.

    Weakening this is a one-word edit — the copy this replaced said "once the
    positioner is answering again" — and it fails on no test, reaches no
    screen a suite renders, and is only wrong for the half of the readership
    that is a person holding a microphone.

    Read off ``POSITION_GATE_TERMINAL_CODES`` rather than a hand-listed triple,
    so a fourth gate refusal inherits the rule the day it is written.
    """
    import re

    from jasper.web.correction_crossover_v2 import POSITION_GATE_TERMINAL_CODES

    assert POSITION_GATE_TERMINAL_CODES, "the gate has terminal codes to check"
    pattern = re.compile(r"\b(" + "|".join(_MOVER_WORDS) + r")\b", re.IGNORECASE)
    for code in sorted(POSITION_GATE_TERMINAL_CODES):
        spec = REASON_REGISTRY[code]
        for slot, text in (("message", spec.message), ("banner", spec.banner)):
            found = pattern.findall(text or "")
            assert not found, f"{code}.{slot} names a mover: {found} in {text!r}"
        # ...and it still says what it is about, so "names no mover" cannot be
        # satisfied by saying nothing.
        assert "microphone" in spec.message


def test_an_entry_with_no_target_is_refused_not_measured():
    """A remote plan emits a target on EVERY entry, so a missing one means the
    plan and the gate disagree about the session's shape."""
    gate = PositionGate()
    with pytest.raises(CaptureBeginRefused) as refused:
        gate.gate(1, 1, SimpleNamespace(screen={}))
    assert refused.value.code == POSITION_TARGET_MISSING_CODE
    assert gate.pending() is None


def test_the_deferral_the_gate_raises_is_the_shipped_non_terminal_hold():
    """The mechanism is deliberately the one the capture page already handles —
    ``capture_deferred`` parks it and re-posts the identical begin — because that
    page is a separately deployed artifact this tier must not couple to."""
    assert issubclass(CaptureBeginDeferred, RuntimeError)
    assert not issubclass(CaptureBeginDeferred, CaptureBeginRefused)


# --------------------------------------------------------------------------- #
# the household surfaces
# --------------------------------------------------------------------------- #


def test_remote_is_never_offered_in_the_household_chooser():
    """Consenting to this tier means owning a positioner the chooser cannot see,
    so it is reached by API only — the chooser keeps offering exactly two."""
    for applied in ("none", "automatic"):
        status = {
            "crossover_v2": {"tier": TIER_FULL},
            "applied_crossover": {"state": applied},
        }
        primary, alternates = _tier_choice_actions(status)
        offered = {primary["id"], *(a["id"] for a in alternates)}
        assert offered == {"start_v2_session_full", "start_v2_session_express"}
        assert all(TIER_REMOTE not in action_id for action_id in offered)


def test_every_tier_has_household_copy_on_both_surfaces():
    """Two label tables already exist (the wizard's and the consent screen's,
    across a boundary that forbids an import). A tier missing from either leaks
    a raw slug or drops a consent line."""
    from jasper.active_speaker.crossover_v2.sweep_spec import _GUIDED_TIER_LABELS

    assert set(_TIER_LABELS) == set(flow.TIERS)
    assert set(_TIER_CLAIMS) == set(flow.TIERS)
    assert set(_GUIDED_TIER_LABELS) == set(flow.TIERS)
    assert _TIER_LABELS[TIER_REMOTE] == "Remote automated"
    # The two tables are mirrors, so they must agree word for word.
    assert _GUIDED_TIER_LABELS == {t: _TIER_LABELS[t] for t in flow.TIERS}


def test_a_remote_done_screen_discloses_the_axis_it_could_not_sample():
    """One line, once, disclose-and-recommend — never a block, and never on a
    tier that did sample the vertical."""
    verify = {"outcome": "pass"}
    remote = _done_nudges(verify, spec_passed=True, tier=TIER_REMOTE)
    codes = [n["code"] for n in remote]
    assert codes.count("crossover_v2_remote_horizontal_only") == 1
    disclosure = next(
        n for n in remote if n["code"] == "crossover_v2_remote_horizontal_only"
    )
    assert disclosure["severity"] == "info"
    assert "horizontal" in disclosure["text"].lower()
    for tier in HAND_WALKED:
        others = _done_nudges(verify, spec_passed=True, tier=tier)
        assert not any(
            n["code"] == "crossover_v2_remote_horizontal_only" for n in others
        )
    # It survives the other badge branch too, so the disclosure is owed however
    # the result graded.
    outcome = _done_nudges(
        verify, spec_passed=None, result_outcome="keep_previous", tier=TIER_REMOTE
    )
    assert any(
        n["code"] == "crossover_v2_remote_horizontal_only" for n in outcome
    )


def test_the_flow_states_the_vertical_gap_in_one_place():
    assert "vertical" in flow.REMOTE_VERTICAL_DISCLOSURE.lower()


def test_a_geometry_locked_remote_group_refuses_instead_of_prompting(monkeypatch):
    """S4a. Both retake rungs are out of an external positioner's reach — rung 1
    is 75 cm off the mark, past every pose in the walk, and rung 2 adds a move
    ABOVE mark height, the axis this tier excludes by construction. Prompting
    anyway asked for a move that cannot be made and then recorded it as though
    it had been.

    The branch is phase-agnostic, so it is exercised here through the cloud
    group the shared fixtures already drive.
    """
    fakes = FakeSeams()
    remote = _cloud_conductor(fakes, tier=TIER_REMOTE)
    attempt = _walk(remote, (1, 2), 1)
    attempt = _walk(remote, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    verdict = _run_phase(remote, last, attempt)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_GEOMETRY_RETAKE_UNREACHABLE
    # It REFUSES — it does not hand back a prompt for a pose nobody can reach.
    assert not verdict.get("prompt")
    # …and it recommends the instrument that can, without blocking anything.
    message = REASON_REGISTRY[REASON_GEOMETRY_RETAKE_UNREACHABLE].message
    assert "Full measurement" in message
    assert "by hand" in message
    # Nothing was spent and nothing was dropped: this is not a retry.
    assert last in {
        int(pid.rsplit("_", 1)[1])
        for pid in remote.group_positions(PHASE_CLOUD_MEASURE)
    }


def test_a_geometry_locked_hand_released_group_refuses_too(monkeypatch, caplog):
    """S4a's predicate is the GATE, not the tier (#2879 round-2 SF2).

    Driven on the shape the finding names: a hand-walked Full **stage 2**,
    gated because it opened on the wired source, walking its ``cloud_verify``
    group into a lock. The person could perfectly well walk to 75 cm — what
    they could not do is be told two places at once, which is what prompting
    here produces: the retry re-authorizes the SAME plan entry, so the position
    gate goes on publishing that entry's original bearing while the screen
    names the wider spot.

    It also covers the ARM's stage 2, which reached this branch unrefused
    before the predicate moved: the verify-only prepare constructs its session
    with no ``tier`` at all, so the old ``tier_is_externally_positioned`` read
    answered False for every stage-2 group including remote's.
    """
    fakes = FakeSeams()
    fakes.apply_done = True
    held = _conductor(
        fakes,
        tier=TIER_FULL,
        positions_gated=True,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(flow.PHASE_CHECK, flow.PHASE_MEASURE),
        applied=True,
    )
    attempt = _walk(held, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES[:-1]), 1)
    last = CLOUD_VERIFY_INDEXES[-1]
    _lock(monkeypatch)
    caplog.set_level(logging.WARNING, logger=flow.__name__)

    verdict = _run_phase(held, last, attempt)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_GEOMETRY_RETAKE_UNREACHABLE
    # The journal names the PREDICATE, not just the tier: a stage-2 session is
    # constructed without one, so `tier=` alone would say nothing about why
    # this refused.
    refused = [
        r.getMessage() for r in caplog.records
        if "crossover_v2_geometry_retake_unreachable" in r.getMessage()
    ]
    assert len(refused) == 1, refused
    assert "gated=true" in refused[0]
    # ONE surface owns the answer: no prompt is handed back for a spot the gate
    # would go on contradicting.
    assert not verdict.get("prompt")
    # Nothing was spent and nothing was dropped: this is not a retry.
    assert last in {
        int(pid.rsplit("_", 1)[1])
        for pid in held.group_positions(flow.PHASE_CLOUD_VERIFY)
    }


def test_the_same_stage_2_group_still_prompts_when_nothing_holds_its_begins(
    monkeypatch,
):
    """The control for the test above, ONE field apart: the ordinary phone
    round walks the identical stage-2 group with no gate, so there is no second
    answer to contradict and the household IS asked for the wider spot."""
    fakes = FakeSeams()
    fakes.apply_done = True
    ungated = _conductor(
        fakes,
        tier=TIER_FULL,
        index_phase_map=STAGE2_MAP,
        accepted_phases=(flow.PHASE_CHECK, flow.PHASE_MEASURE),
        applied=True,
    )
    attempt = _walk(ungated, (VERIFY_INDEX, *CLOUD_VERIFY_INDEXES[:-1]), 1)
    last = CLOUD_VERIFY_INDEXES[-1]
    _lock(monkeypatch)

    verdict = _run_phase(ungated, last, attempt)
    assert verdict["accepted"] is False
    assert verdict["code"] == flow.REASON_CLOUD_GEOMETRY_LOCKED
    assert verdict["prompt"] == flow.CLOUD_GEOMETRY_RETRY_PROMPTS[0]


def test_a_hand_walked_group_still_gets_its_wider_retake_prompt(monkeypatch):
    """The other half of S4a: the refusal is scoped to GATED sessions, and a
    household whose begins nothing holds is still asked to walk to 75 cm."""
    fakes = FakeSeams()
    walked = _cloud_conductor(fakes, tier=TIER_FULL)
    attempt = _walk(walked, (1, 2), 1)
    attempt = _walk(walked, CLOUD_MEASURE_INDEXES[:-1], attempt)
    last = CLOUD_MEASURE_INDEXES[-1]
    _lock(monkeypatch)

    verdict = _run_phase(walked, last, attempt)
    assert verdict["accepted"] is False
    assert verdict["code"] == flow.REASON_CLOUD_GEOMETRY_LOCKED
    assert verdict["prompt"] == flow.CLOUD_GEOMETRY_RETRY_PROMPTS[0]


def test_one_predicate_answers_who_positions_the_microphone():
    """The conductor holds a tier STRING and the plan builders hold a resolved
    shape; both must answer this the same way, and a durable state file's stale
    or absent tier must answer "hand-walked" rather than raising."""
    assert flow.tier_is_externally_positioned(TIER_REMOTE)
    assert flow.tier_is_externally_positioned("  Remote ")
    for benign in ("", None, TIER_FULL, TIER_EXPRESS, "a-tier-from-a-later-build"):
        assert flow.tier_is_externally_positioned(benign) is False
    # The shape's property is the same answer, not a second one.
    for tier in flow.TIERS:
        assert (
            resolve_plan_shape(tier).externally_positioned
            is flow.tier_is_externally_positioned(tier)
        )


# --------------------------------------------------------------------------- #
# the second gated shape: a person releases the holds (#2879)
# --------------------------------------------------------------------------- #


def _hand_released(tier=TIER_FULL):
    """A hand-walked shape told a PERSON releases its begins — what the host
    hands a wired round through ``_hand_released_plan_shape``."""
    return dataclasses.replace(
        resolve_plan_shape(tier), hand_released_positions=True,
    )


def test_the_two_shape_facts_are_independent():
    """The unweld, as a truth table.

    ``externally_positioned`` is the ADVANCE axis and ``positions_gated`` the
    POSE-STATEMENT one. Three of the four combinations are real shapes; the
    fourth — auto-advance with no gate, the countdown firing into a
    microphone nobody promised had arrived — is unreachable by construction
    because gating reads the advance fact as one of its own disjuncts.
    """
    for tier in (TIER_FULL, TIER_EXPRESS):
        shape = resolve_plan_shape(tier)
        assert (shape.externally_positioned, shape.positions_gated) == (
            False, False,
        )
        assert (_hand_released(tier).externally_positioned,
                _hand_released(tier).positions_gated) == (False, True)
    remote = resolve_plan_shape(TIER_REMOTE)
    assert (remote.externally_positioned, remote.positions_gated) == (True, True)
    # And no shape can claim both movers at once.
    with pytest.raises(CrossoverV2FlowError, match="external driver"):
        dataclasses.replace(remote, hand_released_positions=True)


def test_a_hand_released_shape_states_bearings_and_keeps_the_tap():
    """``(degrees, tap)`` — the combination the shipped tiers could not say.

    The pose statement follows the GATE (a person is given the bearing the
    gate is waiting for, in copy and in the machine keys the gate reads), and
    the advance policy follows the MOVER (a person is there to tap, so no
    countdown fires while they are still walking).
    """
    plan = _stage1_of(_hand_released())
    for entry in plan.entries:
        assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
        assert "countdown_s" not in entry.screen
        assert POSITION_DEG_KEY in entry.screen
        assert entry.screen[POSITION_ROLE_KEY] == POSITION_ROLE_ONAX
    baseline, = [e for e in plan.entries if e.kind_label == "entry_baseline"]
    assert "0°" in baseline.screen["title"]


def test_a_hand_released_stage_2_states_its_SPOTS_as_bearings():
    """The pose-statement axis where it is actually READ (#2879 gate S2).

    ``_positioned_prompt`` is reached by exactly one shipped builder — stage
    2's post-apply group — because stage 1's cloud group is off
    (``STAGE1_INCLUDES_CLOUD_MEASURE``) and its lateral walk is opt-in. So a
    stage-1-only test cannot see it, and welding that read back to
    ``externally_positioned`` passed the entire suite while flipping a
    household's copy to a tape-measure instruction against a gate publishing
    degrees. This asserts the COPY, which is the half the machine keys cannot
    stand in for: the two are the same statement in two vocabularies, and a
    session that says one thing to a person and another to the gate is the
    whole defect the split exists to prevent.
    """
    plan = _stage2_of(_hand_released())
    prompted = [e for e in plan.entries if int(e.screen[POSITION_DEG_KEY]) != 0]
    assert prompted, "a Full stage 2 walks prompted spots off the axis"
    for entry in prompted:
        degrees = int(entry.screen[POSITION_DEG_KEY])
        side = "LEFT" if degrees < 0 else "RIGHT"
        # The copy names the SAME bearing the gate will publish and wait for.
        assert f"Turn the microphone to {degrees:+d}°" in entry.screen["title"]
        assert f"{abs(degrees)}° {side} of the design axis" in entry.screen["title"]
        # ...and it is a tap, not a countdown: a person is holding the tape.
        assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
        assert "countdown_s" not in entry.screen
    assert [int(e.screen[POSITION_DEG_KEY]) for e in plan.entries] == list(
        STAGE2_ANGLES
    )


def test_the_post_apply_walk_serves_the_design_axis_as_a_prompted_pose():
    """(T1-5) 0° is servable end to end, and the group gets a curve at it.

    Before the 2026-08-24 geometry ruling the post-apply pose plan structurally
    excluded the design axis: VERIFY's anchor stood at the mark, but its sweep
    goes to the TRACKING verdict, so the group combined four off-axis curves and
    banked no on-axis position record. The campaign paid for that by running an
    extra minimal MEASURE round just to get one on-axis summed response of the
    graph it had applied.

    Three things have to hold together for "servable", and one alone is not
    enough — the anchor already published a 0° target and it is not what was
    missing:

    * the SESSION issues a 0° prompt of its own — a ``cloud_verify`` entry, not
      just the anchor;
    * the GATE is given a target for it, so a driver waiting on
      ``position_pending`` is released rather than left holding;
    * the set of bearings this plan publishes CONTAINS 0, which is what
      ``serve --expect-angles`` compares against (it matches a set, so
      the anchor's repeat neither helps nor hurts).
    """
    plan = _stage2(TIER_REMOTE)
    prompted = [e for e in plan.entries if e.kind_label == "cloud_verify"]

    on_axis = [e for e in prompted if int(e.screen[POSITION_DEG_KEY]) == 0]
    assert len(on_axis) == 1, "the walk prompts the design axis exactly once"
    assert "design axis (0°)" in on_axis[0].screen["title"]
    assert on_axis[0].screen[POSITION_ROLE_KEY] == POSITION_ROLE_ONAX
    # Every entry states a target, so nothing in this walk can strand the gate
    # on a hold it has no bearing for (``POSITION_TARGET_MISSING_CODE``).
    assert all(POSITION_DEG_KEY in e.screen for e in plan.entries)
    assert {int(e.screen[POSITION_DEG_KEY]) for e in plan.entries} == {
        0, 7, -7, 22, -22
    }


def test_a_hand_released_stage_2_anchor_reads_in_degrees_and_keeps_its_confirm():
    """The second unpinned read: stage 2's ANCHOR copy (#2879 gate S2).

    Its title/body follow the pose statement (a gated operator is given the
    bearing) while its confirm tap follows the advance policy (only a
    machine-advanced session has no hand to answer one). Welding the first back
    to ``externally_positioned`` also passed the whole suite.
    """
    anchor = _stage2_of(_hand_released()).entries[0].screen
    assert "design axis (0°)" in anchor["title"]
    assert f"{flow.MARK_DISTANCE_M:g} m out" in anchor["body"]
    # The hand keeps its confirm — byte-identical to every tap-paced shape.
    assert anchor["confirm_title"] == "Back on the mark, holding still?"
    # ...which the ARM still does not get, because there is no hand to answer.
    assert "confirm_title" not in _stage2(TIER_REMOTE).entries[0].screen
    # ...and a tap-paced shape keeps the tape-measure copy verbatim.
    assert "Back at the mark" in _stage2(TIER_FULL).entries[0].screen["title"]


def test_the_gate_can_read_a_hand_released_plans_own_entries():
    """The join the split exists for: the gate refuses an entry that does not
    say where the microphone should be (``position_target_missing``), so a
    gated shape whose plan forgot the keys would fail every begin. It holds
    instead, naming the bearing the person is being asked for."""
    plan = _stage1_of(_hand_released())
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred) as caught:
        gate.gate(1, 1, plan.entry_for_index(1))
    assert caught.value.code == POSITION_HOLD_CODE
    pending = gate.pending()
    assert pending["degrees"] == 0
    assert pending["action"]["endpoint"] == POSITION_READY_ENDPOINT
    # ...and the same release verb admits it, with the same minted payload.
    gate.release(1)
    gate.gate(1, 1, plan.entry_for_index(1))


#: The arm's two SHIPPED plans, as wire bytes. Captured from the #2879 SPLIT
#: build and then re-run against the PRE-SPLIT ``crossover_v2_flow`` (that
#: module checked out at the merge base, this test unchanged): both digests
#: matched, which is the tier's byte-identity promise as a measurement rather
#: than as a claim.
#:
#: Deliberately NOT added to ``_GOLDEN_V2_PLAN_BYTES``: that table builds each
#: plan from the BUILDER's defaults, and ``include_cloud_measure`` defaults True
#: — which for remote's N=9 walks a vertical pose and makes
#: ``position_angle_deg`` refuse before a digest exists. Remote is only
#: constructible through the flags a session actually uses
#: (:data:`STAGE1_INCLUDES_CLOUD_MEASURE`), so its digest belongs beside its own
#: contract rather than in a table whose convention it cannot satisfy.
_GOLDEN_REMOTE_PLAN_BYTES = {
    "stage1-remote": (
        1322,
        "fc27865bbd695be7a4fe08611efe2c825f88820666c82fc59e1eb93176dd3b5e",
    ),
    # RE-DERIVED 2026-08-24 — the geometry ruling's post-apply pose set. Stage 2
    # gained one prompted entry (the design axis, now a member of the walk
    # rather than only the anchor in front of it), so its bytes moved and
    # stage 1's did not: 1797 B → 2103 B. The UNCHANGED stage-1 digest is the
    # load-bearing half — the ruling reached the post-apply walk and nothing
    # else.
    "stage2-remote": (
        2103,
        "0205565e6ecd1a2f4b2a3421c50e21e1ca8f9159bac7b045b968c27bbccaeb67",
    ),
}


def test_the_arms_shipped_plans_are_byte_identical():
    """The tier's own promise, as bytes rather than as spot-checked fields.

    ``test_the_arm_keeps_its_countdown_when_a_person_gains_the_gate`` below
    names the two fields the split could plausibly have disturbed; this says
    nothing at all moved, which is the claim #2879 actually made.
    """
    import hashlib
    import json

    plans = {
        "stage1-remote": _stage1(TIER_REMOTE),
        "stage2-remote": _stage2(TIER_REMOTE),
    }
    assert set(plans) == set(_GOLDEN_REMOTE_PLAN_BYTES)
    for label, plan in plans.items():
        raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
        actual = (len(raw), hashlib.sha256(raw).hexdigest())
        assert actual == _GOLDEN_REMOTE_PLAN_BYTES[label], (
            f"{label} wire bytes changed: len={actual[0]} sha256={actual[1]}"
        )


def test_the_arm_keeps_its_countdown_when_a_person_gains_the_gate():
    """The byte-identity promise, restated for the new fact: nothing about the
    arm's plan moves. (``_GOLDEN_REMOTE_PLAN_BYTES`` proves it in hashes; this
    says which two fields the split could plausibly have disturbed.)"""
    plan = _stage1(TIER_REMOTE)
    for entry in plan.entries:
        assert entry.screen["auto_advance"] == AUTO_ADVANCE_COUNTDOWN
        assert entry.screen["countdown_s"] == str(AUTO_ADVANCE_COUNTDOWN_S)
        assert POSITION_DEG_KEY in entry.screen


def test_only_a_hand_walked_shape_is_told_a_person_releases_its_begins():
    """A hand-walked round has nothing pacing it, so its begins are held and
    released by hand. Every other shape is returned untouched."""
    from jasper.web.correction_crossover_v2 import _hand_released_plan_shape

    full = resolve_plan_shape(TIER_FULL)
    assert _hand_released_plan_shape(full).hand_released_positions
    remote = resolve_plan_shape(TIER_REMOTE)
    assert _hand_released_plan_shape(remote) == remote
    # The tier-less recovery re-arm: one sweep at the mark, nowhere to walk to.
    assert _hand_released_plan_shape(None) is None


def test_the_preparer_builds_the_gate_from_the_shapes_own_answer():
    """One question, one predicate, one construction site — the drift this pins
    is a stage gaining the second gated shape while another silently keeps
    running a hand-walked wired round with no hold at all. The two stages used
    to build the gate at two sites; they now share one, so the count is what
    says a second one has not grown back."""
    from jasper.web import correction_crossover_v2 as v2host

    source = inspect.getsource(v2host)
    assert source.count("PositionGate()") == 1
    assert source.count("if plan_shape is not None and plan_shape.positions_gated") == 1
    assert "PositionGate() if plan_shape.externally_positioned" not in source


def test_a_hand_walked_wired_round_opens_with_a_gate_and_a_retake(
    caplog, monkeypatch,
):
    """The acceptance criterion, through the REAL preparer.

    A hand-walked round has nothing pacing it: without a hold the local runner
    fires every capture back to back while the household is still walking. So
    a hand-walked shape opens gated, announces WHO releases the holds, and
    carries the local retake seam.
    """
    from jasper.web import correction_crossover_v2 as v2host

    caplog.set_level(logging.INFO, logger=v2host.__name__)
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    prepared = v2host.prepare_v2_session(
        {"tier": TIER_FULL}, status=_status(), run_async=None, camilla_factory=None,
    )

    assert prepared.position_gate is not None
    assert prepared.request_retake is not None
    assert prepared.request_complete is not None
    opens = [
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_remote_session_open" in r.getMessage()
    ]
    assert len(opens) == 1, opens
    assert f"tier={TIER_FULL}" in opens[0] and "hand_released=true" in opens[0]


def test_a_hand_walked_wired_re_verify_opens_with_a_gate(caplog, monkeypatch):
    """STAGE 2 through the real preparer (#2879 gate S2).

    The gate is built at TWO construction sites, and a source pin says they
    read the same predicate — but only a drive proves stage 2 rebinds the shape
    at all. Its plan is built inside ``_open``, so a rebind that landed one
    line too late would emit a plan whose entries the gate cannot read.
    """
    from jasper.web import correction_crossover_v2 as v2host

    v2host.save_v2_state({"applied": True, "tier": TIER_FULL})
    caplog.set_level(logging.INFO, logger=v2host.__name__)
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    prepared = v2host.prepare_v2_session(
        {v2host.VERIFY_STAGE_KEY: v2host.VERIFY_STAGE_POST_APPLY},
        status=_status(), run_async=None, camilla_factory=None, verify_only=True,
    )

    assert prepared.position_gate is not None
    assert prepared.request_retake is not None
    opens = [
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_remote_session_open" in r.getMessage()
    ]
    assert len(opens) == 1, opens
    assert "stage=2" in opens[0] and "hand_released=true" in opens[0]


def _opened_conductor(monkeypatch, v2host, prepared):
    """Run a prepared session's real ``_open`` and hand back its conductor.

    ``tests.test_crossover_v2_stage_bridge._open_prepared`` does this by
    stubbing the runner builder, which is also where it catches the conductor;
    this is the same capture point, kept local so the two suites do not share
    a harness across modules.
    """
    captured: dict = {}

    def _builder(conductor, **_kwargs):
        captured["conductor"] = conductor

        async def _run(_client, _pi_session):
            return None

        return _run

    monkeypatch.setattr(v2host, "_build_wired_run", _builder)
    prepared.open()
    return captured["conductor"]


def test_both_preparers_tell_their_conductor_whether_its_begins_are_held(
    monkeypatch,
):
    """The wiring the two behavioural tests above cannot see (#2879 round-2).

    ``test_a_geometry_locked_hand_released_group_refuses_too`` drives a
    conductor directly, so it proves the RULE. This proves each REAL preparer
    hands its conductor the fact that rule reads — the gate the host builds and
    the fact the conductor decides with come off ONE shape, or they are two
    answers again. Stage 2 needs saying most: its ctor is handed no ``tier`` at
    all, so before this it decided from a tier it never had. (Its argument also
    landed one call too early during this fix round, inside ``open_stage``,
    which every existing test tolerated because nothing opened a stage-2
    session on this path.)

    Read privately on purpose. The fact selects a refusal branch and is
    rendered nowhere, so it has no public surface; walking a whole opened
    stage-2 session into a geometry lock would restate the behavioural test
    rather than pin the wiring.
    """
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    stage_1 = v2host.prepare_v2_session(
        {"tier": TIER_FULL}, status=_status(), run_async=None, camilla_factory=None,
    )
    assert stage_1.position_gate is not None
    assert _opened_conductor(monkeypatch, v2host, stage_1)._positions_gated is True

    v2host.save_v2_state({"applied": True, "tier": TIER_FULL})
    stage_2 = v2host.prepare_v2_session(
        {v2host.VERIFY_STAGE_KEY: v2host.VERIFY_STAGE_POST_APPLY},
        status=_status(), run_async=None, camilla_factory=None, verify_only=True,
    )
    assert stage_2.position_gate is not None
    assert _opened_conductor(monkeypatch, v2host, stage_2)._positions_gated is True


def test_a_wired_recovery_re_arm_carries_no_retake_it_could_not_serve(monkeypatch):
    """The one-sweep recovery re-verify has no gate and no group (#2879 gate N6).

    So it reaches neither retake window, and carrying the seam would answer a
    household's POST with ``ok: true`` and then do nothing — a signal that
    reports success and serves nobody. The route's own 409 ("no wired
    measurement is waiting to re-take a spot") becomes the honest answer.
    """
    from jasper.web import correction_crossover_v2 as v2host
    from jasper.web import correction_setup

    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic",
        lambda: SimpleNamespace(card_id="hw:9,0", model_key="umik2"),
    )
    v2host.save_v2_state({"applied": True, "tier": TIER_FULL})
    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None,
        verify_only=True,
    )
    assert prepared.position_gate is None      # the recovery shape
    assert prepared.request_retake is None
    # ...and the completion signal is untouched: it has a real reader.
    assert prepared.request_complete is not None

    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:verify",
        request_complete=prepared.request_complete,
        request_retake=prepared.request_retake,
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_setup._handle_crossover_v2_retake(
                SimpleNamespace(
                    headers={"Content-Length": "2"}, rfile=io.BytesIO(b"{}"),
                )
            )
    finally:
        correction_setup._set_capture_slot(None)


def test_a_person_may_be_asked_for_a_bearing_the_arm_cannot_reach():
    """±80° is the GEOMETRY's ceiling and the person's reach; ±45° is the
    arm's own travel. A walk stated for a person is judged against the
    person's bound, and the session that hosts it is the hand-walked one."""
    from jasper.active_speaker import angle_capture as ac

    assert ac.MOVER_MAX_ANGLE_DEG[ac.MOVER_HUMAN] == ac.MAX_ANGLE_DEG == 80
    assert ac.MOVER_MAX_ANGLE_DEG[ac.MOVER_ARM] == ac.ARM_ENVELOPE_DEG == 45
    reach = ac.per_driver_at([80, -80], mover=ac.MOVER_HUMAN)
    prompts = ac.session_lateral_walk(
        reach,
        # The hand-released shape's OWN answer, not a literal: what makes a
        # person's walk admissible is that a gated session still advances by
        # tap, and a regression that gave it the countdown would refuse this
        # walk rather than quietly measuring through it.
        externally_positioned=_hand_released().externally_positioned,
        base_entries=3,
        plans_cloud_group=False,
    )
    assert [position_angle_deg(p) for p in prompts] == [80, -80]
    with pytest.raises(ac.LateralWalkRefused) as caught:
        ac.per_driver_at([46], mover=ac.MOVER_ARM)
    assert caught.value.reason == ac.WALK_OVER_MOVER_ENVELOPE


# --------------------------------------------------------------------------- #
# the driver's transport
# --------------------------------------------------------------------------- #


def _json_handler(payload: str):
    """The two attributes ``read_json_object`` actually reads."""
    body = payload.encode()
    return SimpleNamespace(
        headers={"Content-Length": str(len(body))}, rfile=io.BytesIO(body),
    )


@contextmanager
def _live_remote_slot(gate):
    """Claim the process's single capture slot for a crossover v2 session."""
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:session", position_gate=gate,
    )
    try:
        yield correction_setup
    finally:
        correction_setup._set_capture_slot(None)


def test_a_live_hold_reaches_the_envelope_on_the_capture_block():
    """The driver's read path: the gate owns the fact, the capture block carries
    it, and the envelope copies that block through verbatim."""
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(2, 2, _entry(-22, POSITION_ROLE_OFFAX))
        capture = setup._get_capture_slot_for("crossover_v2:")
        pending = capture["position_pending"]
        assert pending["degrees"] == -22
        assert pending["index"] == 2
        assert pending["action"]["endpoint"] == POSITION_READY_ENDPOINT
        # Another flow's reader must never see this session's hold.
        assert setup._get_capture_slot_for("sync:") is None
        gate.release(2)
        assert "position_pending" not in setup._get_capture_slot_for("crossover_v2:")


def test_a_finished_session_stops_advertising_its_hold():
    """The strand check. A hold published into durable state could outlive the
    session holding it; riding the capture slot means the existing terminal
    transition drops it, with no new cleanup path to forget."""
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(1, 1, _entry(0))
        assert setup._get_capture_slot_for("crossover_v2:")["position_pending"]
        # The runner's own terminal publish, verbatim in shape.
        setup._set_capture_slot(
            {"status": "complete", "kind": "crossover_v2:session"}
        )
        assert setup._capture_position_gate is None
        capture = setup._get_capture_slot_for("crossover_v2:")
        assert "position_pending" not in capture
        # …and a late driver POST cannot reach a gate nobody is holding.
        with pytest.raises(ValueError, match="no remote measurement is waiting"):
            setup._handle_crossover_v2_position_ready(_json_handler('{"index": 1}'))


def test_the_ceiling_detector_reaches_the_live_gate_and_only_when_it_fires():
    """The wiring behind the cumulative name (issue #2506).

    Detection has ONE owner — the lazy enforcement the wizard/driver poll
    already runs, which is the only thing that reads the plan's ``opened_at``
    against the stage's own stamped ceiling. It also DRAINS what it finds, so a
    gate sampling the plan on its own 1.5 s cadence would race that drain and
    lose the fact; it is told instead. This pins both directions: a poll that
    finds nothing stale must not latch anything.
    """
    gate = PositionGate()
    quiet = SimpleNamespace(enforce_session_volume_ceiling_if_stale=lambda *_: False)
    stale = SimpleNamespace(enforce_session_volume_ceiling_if_stale=lambda *_: True)
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        setup._enforce_session_volume_ceiling(quiet)
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        setup._enforce_session_volume_ceiling(stale)
        with pytest.raises(CaptureBeginRefused) as refused:
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        assert refused.value.code == SESSION_CEILING_EXPIRED_CODE
    # A tap-paced session (every ungated round) registers no gate at all, and
    # the same poll must still enforce the ceiling rather than raise on the
    # missing gate.
    setup._enforce_session_volume_ceiling(stale)


def test_an_abandoned_hold_stops_being_the_advertised_position():
    """A hold whose begin nobody is running any more must not be published.

    ``gate`` publishes a NEW ``pending`` only when no hold is open — the
    idempotence that lets a re-posted begin re-enter its own hold without
    restarting the clock. So a caller that walks AWAY from a held begin (the
    wired runner, abandoning one to re-open the previous slot as a retake) has
    to say so, or the next begin reads as a continuation and the envelope keeps
    naming a position nothing is measuring.
    """
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(2, 2, _entry(22, POSITION_ROLE_OFFAX))
    assert gate.pending()["index"] == 2

    gate.abandon_hold()
    assert gate.pending() is None
    gate.abandon_hold()  # idempotent, and safe with nothing open

    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 3, _entry(0))
    pending = gate.pending()
    assert (pending["index"], pending["attempt"], pending["degrees"]) == (1, 3, 0)
    # A release already given stays given — abandoning a hold is not a rewind.
    gate.release(1)
    gate.gate(1, 3, _entry(0))


def test_the_release_route_admits_the_pending_capture():
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        body = setup._handle_crossover_v2_position_ready(_json_handler('{"index": 4}'))
        assert body["ok"] is True
        assert body["released"]["degrees"] == 7
        gate.gate(4, 4, _entry(7))  # admitted — no raise


@contextmanager
def _serving():
    """The REAL wizard server on a loopback port, plus a valid CSRF pair.

    Route-level rather than handler-level on purpose. A handler-level
    ``pytest.raises(BadRequest)`` pins what the *function* does and says nothing
    about what the *client* receives — and the two disagreed: the re-raise
    escaped ``do_POST`` into ``socketserver``'s error handler, which logs a
    traceback and drops the connection with no response at all. Only a real
    request over a real socket can tell "raised a 400-shaped error" apart from
    "answered 400".
    """
    from jasper.web import correction_setup

    server = correction_setup.make_server(("127.0.0.1", 0), hostname="jts.local")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = secrets.token_urlsafe(32)

    def post(path: str, body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            method="POST",
            headers={
                "Host": "jts.local",
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Cookie": f"{CSRF_COOKIE_NAME}={token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    try:
        yield post
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Every body a driver can get wrong, and the status each must ANSWER with.
# `{not json` is the parse failure; the rest are shape failures this route
# owns. None of them may reach the client as a dropped connection.
MALFORMED_BODIES = [
    b"{not json",
    b"{}",
    b'{"index": "abc"}',
    b'{"index": null}',
    b'{"index": 1.5}',
    b'{"index": true}',
    b"[]",
]


@pytest.mark.parametrize("body", MALFORMED_BODIES)
def test_the_release_route_answers_400_on_a_malformed_body(body):
    """B1. Every one of these used to close the socket with no response."""
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        with _serving() as post:
            status, payload = post("/crossover/v2/position-ready", body)
        assert status == 400, payload
        assert json.loads(payload)  # a JSON error body, not an empty close
        # …and the hold survived every refusal.
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))


def test_the_release_route_answers_409_on_a_stale_index():
    """The retry-crossed-a-capture case, over the wire: a CONFLICT with a
    readable reason, never a 400 and never a dropped connection."""
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        with _serving() as post:
            status, payload = post(
                "/crossover/v2/position-ready", b'{"index": 9}',
            )
            assert status == 409, payload
            assert b"waiting" in payload
            # The good release still answers 200 on the same server.
            ok_status, ok_payload = post(
                "/crossover/v2/position-ready", b'{"index": 4}',
            )
        assert ok_status == 200, ok_payload
        assert json.loads(ok_payload)["ok"] is True
        gate.gate(4, 4, _entry(7))  # admitted — no raise


def test_the_release_route_demands_an_index_it_can_check():
    """An untargeted release is the hazard: a mismatched index is refused
    rather than applied to whatever happens to be pending."""
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        with pytest.raises(ValueError, match="measurement 4 is waiting, not 9"):
            setup._handle_crossover_v2_position_ready(_json_handler('{"index": 9}'))
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))


def test_the_release_route_is_allowlisted_and_matches_the_minted_action():
    """The endpoint the envelope mints must be one the dispatcher will accept —
    a self-describing action pointing at an unrouted path is a dead contract."""
    from jasper.web import correction_setup

    assert POSITION_READY_ENDPOINT.endswith("/crossover/v2/position-ready")
    assert "/crossover/v2/position-ready" in correction_setup._POST_ROUTES


def test_a_tap_paced_session_registers_no_gate_at_all():
    """A session opened with no gate advertises no hold — the capture round's
    shape, and the one a household paces with its own taps on the page."""
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot("crossover_v2:session")
    try:
        assert correction_setup._capture_position_gate is None
        capture = correction_setup._get_capture_slot_for("crossover_v2:")
        assert "position_pending" not in capture
    finally:
        correction_setup._set_capture_slot(None)


def _tier_resolved_by_prepare(body, state, tmp_path):
    """Which tier ``prepare_v2_session`` hands to ``resolve_plan_shape``.

    Recorded at the resolver rather than inferred from a later refusal: the
    preparer runs several gates this harness cannot satisfy, and "it failed
    somewhere after the tier gate" is not evidence about the tier.
    """
    from jasper.active_speaker.crossover_v2 import capture_plan as plan_mod
    from jasper.web import correction_crossover_v2 as v2host

    seen: list = []
    original = plan_mod.resolve_plan_shape

    def _record(tier=None, **kwargs):
        seen.append(tier)
        return original(tier, **kwargs)

    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        v2host.save_v2_state(state)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(plan_mod, "resolve_plan_shape", _record)
            with contextlib.suppress(Exception):
                v2host.prepare_v2_session(
                    body, status={}, run_async=None, camilla_factory=None,
                )
    finally:
        v2host.set_state_path_for_tests(None)
    assert seen, "the preparer never reached the tier resolver"
    return seen[0]


@pytest.mark.parametrize(
    "tier",
    ["remote", "express", "full"],
    ids=["remote_stays_remote", "express_stays_express", "full_stays_full"],
)
def test_a_re_measure_with_no_tier_inherits_the_lapsed_sessions(tier, tmp_path):
    """#2639: every re-measure action the envelope mints posts ``{}``.

    ``resolve_plan_shape`` is strict about unknown names and LENIENT about
    absence, so an empty body resolved to ``full`` — and the envelope has no
    way to name a tier, because the action does not know what the session was.
    Observed on a live round-2 review screen: a REMOTE session's own retry
    silently minted a tier the turntable rig cannot walk (full is not
    externally positioned, and its verify plan raises in
    ``position_angle_deg``). Express households were demoted by the same line.

    All three tiers are walked rather than just the reported one: the defect
    is the ABSENT-tier path, and a fix that special-cased remote would leave
    express demoted exactly as it was.
    """
    resolved = _tier_resolved_by_prepare(
        {}, {"session_id": "cap_lapsed", "tier": tier}, tmp_path,
    )

    assert resolved == tier


def test_the_tier_a_household_explicitly_chooses_still_wins(tmp_path):
    """The control. Inheriting must not turn the tier chooser into a no-op.

    The Express done screen's "Run a Full measurement" posts an explicit tier
    over a lapsed express session, and that is a household changing
    instrument rather than retrying one.
    """
    from jasper.web import correction_crossover_v2 as v2host  # noqa: F401
    from jasper.active_speaker.crossover_v2_flow import TIER_FULL

    resolved = _tier_resolved_by_prepare(
        {"tier": TIER_FULL}, {"session_id": "cap_lapsed", "tier": "express"},
        tmp_path,
    )

    assert resolved == TIER_FULL


def test_a_first_session_with_nothing_to_inherit_keeps_the_shipped_default(
    tmp_path,
):
    """No lapsed session is not a tier. ``None`` must still reach the resolver
    as ``None`` so the shipped default answers, rather than becoming an empty
    string the strict half would refuse."""
    from jasper.web import correction_crossover_v2 as v2host  # noqa: F401
    assert _tier_resolved_by_prepare({}, {"session_id": "cap_first"}, tmp_path) is None


def _walked_index_map(conductor):
    """The index→phase map this conductor was OPENED with.

    Reached past the session's public surface because the count it carries has
    no public reader — ``session_phases`` de-duplicates, so it answers "which
    phases" and never "how many captures". Named for the fact so a future
    public property can replace the body.
    """
    return conductor._journey.plan.index_phase_map


def test_a_remote_session_open_announces_the_captures_it_will_actually_take(
    caplog, monkeypatch,
):
    """The positioner's only sizing surface must be the plan, not the shape.

    ``crossover_v2_remote_session_open`` is emitted for an externally
    positioned session and read by whoever is driving the arm. Why the old
    value was wrong, which direction it fails in, and the 2026-08-19 near-miss
    are written ONCE, at the emitter — ``prepare_v2_session``'s comment on this
    field. Deliberately not restated here: a fact restated in a second place is
    a fact that drifts, and this one already did.

    Driven through the REAL preparer and compared against the map the conductor
    is actually opened with, so the pin is "these two agree" rather than "the
    line prints a 3". The control underneath is what makes that meaningful: the
    shape target is a genuinely DIFFERENT number, so a regression to it fails
    here instead of tying.
    """
    from jasper.web import correction_crossover_v2 as v2host

    caplog.set_level(logging.INFO, logger=v2host.__name__)

    prepared = v2host.prepare_v2_session(
        {"tier": TIER_REMOTE}, status=_status(), run_async=None, camilla_factory=None,
    )

    opens = [
        r.getMessage() for r in caplog.records
        if "correction.crossover_v2_remote_session_open" in r.getMessage()
    ]
    assert len(opens) == 1, opens
    assert "stage=1" in opens[0]
    announced = int(re.search(r"captures=(\d+)", opens[0]).group(1))

    conductor, _state = _open_prepared(monkeypatch, prepared)
    walked = len(_walked_index_map(conductor))

    assert announced == walked
    # The discriminating control, and the defect in one line: the number this
    # journal used to carry is not the number of captures anyone walks.
    assert resolve_plan_shape(TIER_REMOTE).measure_capture_target != walked
