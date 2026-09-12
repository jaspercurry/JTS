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
  the shipped ``CaptureBeginDeferred`` soft-hold, so no browser change is
  required; and
* the promise that adding all of it changed NOTHING for ``full`` / ``express``.
  That last one is load-bearing: the golden wire digests in
  ``tests/crossover_v2_fixtures`` already prove byte-identity, and the pins here
  say in words what those digests say in hashes.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import secrets
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.refusal_copy import (
    CrossoverV2Refused,
    REASON_REGISTRY,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    AUTO_ADVANCE_TAP,
    POSITION_DEG_KEY,
    POSITION_ROLE_KEY,
    POSITION_ROLE_OFFAX,
    POSITION_ROLE_ONAX,
    POSITION_ROLE_XOVR,
    CrossoverV2FlowError,
    build_v2_capture_plan,
    build_v2_verify_capture_plan,
    position_angle_deg,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
)
from jasper.web._common import CSRF_COOKIE_NAME
from jasper.active_speaker.crossover_v2.position_gate import (
    POSITION_HOLD_CODE,
    POSITION_HOLD_EXPIRED_CODE,
    POSITION_READY_ENDPOINT,
    RETAKE_ENDPOINT,
    COMPLETE_ENDPOINT,
    POSITION_TARGET_MISSING_CODE,
    REMOTE_POSITION_HOLD_BUDGET_S,
    SESSION_CEILING_EXPIRED_CODE,
    PositionGate,
)

from tests._log_events import event_fields, event_records
from tests.crossover_v2_fixtures import (
    FC_HZ,
)

# The stage-bridge harness: one definition of "what a real preparer needs
# stubbed", borrowed exactly as ``tests/test_crossover_v2_round_wiring.py``
# borrows it, so the journal pin at the bottom of this file reads a REAL
# ``prepare_v2_session`` rather than a restatement of its source. The two
# autouse fixtures come with it by name, under the redundant-alias form that
# says the module-level name is deliberate.
from tests.test_crossover_v2_stage_bridge import (
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)
from jasper.web import correction_capture, correction_handlers


#: The bearings the target run is specified in — the whole point of the tier, so
#: they are written down here ONCE as the acceptance criterion and everything
#: else in this file derives from the product code.
#:
#: These are the SEQUENCE of stops in walk order, not the SET of angles served —
#: an angle can appear twice because two adjacent stops share a pose. Stage 2
#: opens on two of them since the 2026-08-24 geometry ruling: VERIFY's anchor at
#: the mark, whose sweep the tracking verdict consumes, and then the first pose
#: of ``CLOUD_VERIFY_POSE_PROMPTS``, whose sweep joins the post-apply GROUP. The
#: microphone does not move between them.
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


def _stage2_of(shape):
    """The shipped stage-2 plan for a RESOLVED shape — the twin of
    :func:`_stage1_of`, and the only builder that reaches
    ``_positioned_prompt`` in a shipped shape (stage 1's cloud group is off)."""
    return build_v2_verify_capture_plan(FC_HZ, plan_shape=shape)


def _entry(degrees, role=POSITION_ROLE_ONAX):
    """The one thing the gate reads off a plan entry."""
    return SimpleNamespace(
        screen={POSITION_DEG_KEY: str(degrees), POSITION_ROLE_KEY: role}
    )


# --------------------------------------------------------------------------- #
# the shape
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the angles
# --------------------------------------------------------------------------- #


def test_the_angle_is_derived_from_the_offset_and_signed_by_the_bearing():
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
            math.degrees(
                math.atan2(prompt.offset_cm / 100.0, flow.MARK_DISTANCE_M)
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


# --------------------------------------------------------------------------- #
# auto-advance + the hand-walked tiers' byte-identity
# --------------------------------------------------------------------------- #


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
    pending = gate.published()["pending"]
    assert pending["index"] == 3
    assert pending["attempt"] == 3
    assert pending["degrees"] == -7
    assert pending["role"] == POSITION_ROLE_ONAX
    assert pending["actions"][0]["endpoint"] == POSITION_READY_ENDPOINT
    assert pending["actions"][0]["body"] == {
        "index": 3, "attempt": 3, "degrees": -7, "vertical_deg": 0,
    }
    # The phone re-posts the SAME begin throughout a hold; each one defers again
    # without spending anything.
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(3, 3, entry)
    gate.release(3, 3)
    assert gate.published()["pending"] is None
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
    gate.release(5, 5)
    gate.gate(5, 5, entry)
    # Same index, next attempt: a fresh hold.
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(5, 6, entry)


@pytest.mark.parametrize("stale", [False, True])
def test_an_unmatched_release_is_refused_without_admitting_a_future_pose(stale):
    gate = PositionGate()
    if stale:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(3, 3, _entry(-7))
        gate.release(3, 3)
        gate.gate(3, 3, _entry(-7))
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
    with pytest.raises(CrossoverV2Refused) as refused:
        gate.release(3, 3)
    assert refused.value.code == "capture_slot_busy"
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(4, 4, _entry(7))
    assert gate.published()["pending"]["index"] == 4


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
    assert gate.published()["pending"] is None


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
    assert gate.published()["pending"] is None


def test_the_modal_ceiling_death_announces_no_hold_it_is_about_to_refuse(caplog):
    """The shape a real slow-driver run actually dies in.

    The ceiling is crossed while the session is BETWEEN holds far more often
    than during one: the driver releases position N, the page posts the begin
    for N+1, and that begin is the first thing to meet the latch. Deciding the
    refusal before publishing keeps the journal honest — one
    ``session_ceiling_expired``, rather than a ``position_pending`` announcing
    a hold that is refused in the same breath and never waited a second.
    """
    logger_name = "jasper.active_speaker.crossover_v2.position_gate"
    # POSITIVE CONTROL FIRST. ``position_pending`` is an INFO line, so a
    # WARNING-level capture would swallow it and the absence assertion below
    # would pass against ANY implementation — instrument silence read as
    # evidence. Prove the line reaches this capture before trusting its absence.
    healthy = PositionGate()
    with caplog.at_level(logging.INFO, logger=logger_name):
        with pytest.raises(CaptureBeginDeferred):
            healthy.gate(4, 4, _entry(7))
    assert event_records(caplog, "correction.crossover_v2_position_pending")

    caplog.clear()
    gate = PositionGate()
    with caplog.at_level(logging.INFO, logger=logger_name):
        gate.note_session_ceiling_expired()
        with pytest.raises(CaptureBeginRefused) as refused:
            gate.gate(4, 4, _entry(7))  # a hold this gate has never opened
    assert refused.value.code == SESSION_CEILING_EXPIRED_CODE
    assert not event_records(caplog, "correction.crossover_v2_position_pending")
    ceiling = event_fields(caplog, "correction.crossover_v2_session_ceiling_expired")
    assert ceiling["waited_s"] == "0.0"
    assert gate.published()["pending"] is None


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
    gate.release(3, 3)
    gate.note_session_ceiling_expired()
    gate.gate(3, 3, entry)  # admitted — no raise


def _label_at(degrees):
    """The release action's label for one bearing."""
    gate = PositionGate()
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 1, _entry(degrees))
    return gate.published()["pending"]["actions"][0]["label"]


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


@pytest.mark.parametrize("mover", ac.MOVERS)
@pytest.mark.parametrize("policy", [AUTO_ADVANCE_TAP, AUTO_ADVANCE_COUNTDOWN, ""])
def test_pending_and_join_actions_belong_to_the_mover(mover, policy):
    gate = PositionGate(mover=mover)
    entry = SimpleNamespace(screen={POSITION_DEG_KEY: "0", "auto_advance": policy})
    invitation = gate.invitation(entry)
    assert gate.published()["pending"] is None
    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 1, entry)
    pending = gate.published()["pending"]
    assert invitation == {**pending, "actions": pending["actions"][:1]}
    assert pending["mover"] == mover
    assert [(a["id"], a["endpoint"], a["body"]) for a in pending["actions"]] == ([
        ("position_ready", POSITION_READY_ENDPOINT,
         {"index": 1, "attempt": 1, "degrees": 0, "vertical_deg": 0}),
        ("retake", RETAKE_ENDPOINT, {}),
        ("done", COMPLETE_ENDPOINT, {}),
    ] if mover == ac.MOVER_HUMAN else [])


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
    assert gate.published()["pending"] is None


# --------------------------------------------------------------------------- #
# the household surfaces
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the second gated shape: a person releases the holds (#2879)
# --------------------------------------------------------------------------- #


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
    from jasper.web import correction_capture

    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot(
        "crossover_v2:session", position_gate=gate,
    )
    try:
        yield
    finally:
        correction_capture._set_capture_slot(None)


def test_a_live_hold_reaches_the_envelope_on_the_capture_block():
    """The driver's read path: the gate owns the fact, the capture block carries
    it, and the envelope copies that block through verbatim."""
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(2, 2, _entry(-22, POSITION_ROLE_OFFAX))
        capture = correction_capture._get_capture_slot_for("crossover_v2:")
        pending = capture["position_pending"]
        assert pending["degrees"] == -22
        assert pending["index"] == 2
        assert pending["actions"][0]["endpoint"] == POSITION_READY_ENDPOINT
        # Another flow's reader must never see this session's hold.
        assert correction_capture._get_capture_slot_for("sync:") is None
        # A hold is not an execution: nothing is recording while the gate waits.
        assert "position_current" not in capture
        gate.release(2, 2)
        capture = correction_capture._get_capture_slot_for("crossover_v2:")
        assert "position_pending" not in capture
        assert "position_current" not in capture
        # The runner's re-entry into its own grant is what publishes the entry
        # now being recorded, batch identity included.
        gate.gate(2, 2, _entry(-22, POSITION_ROLE_OFFAX))
        current = correction_capture._get_capture_slot_for("crossover_v2:")["position_current"]
        assert (current["index"], current["attempt"]) == (2, 2)
        assert current["batch"] == {"start": 2, "size": 1, "ordinal": 1}


def test_a_finished_session_stops_advertising_its_hold():
    """The strand check. A hold published into durable state could outlive the
    session holding it; riding the capture slot means the existing terminal
    transition drops it, with no new cleanup path to forget."""
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(1, 1, _entry(0))
        assert correction_capture._get_capture_slot_for("crossover_v2:")["position_pending"]
        # …and the same for a session that ends mid-entry rather than mid-hold:
        # the executing entry is published on the same slot and must go with it.
        gate.release(1, 1)
        gate.gate(1, 1, _entry(0))
        assert correction_capture._get_capture_slot_for("crossover_v2:")["position_current"]
        # The runner's own terminal publish, verbatim in shape.
        correction_capture._set_capture_slot(
            {"status": "complete", "kind": "crossover_v2:session"}
        )
        assert correction_capture._capture_position_gate is None
        capture = correction_capture._get_capture_slot_for("crossover_v2:")
        assert "position_pending" not in capture
        assert "position_current" not in capture
        # …and a late driver POST cannot reach a gate nobody is holding.
        with pytest.raises(CrossoverV2Refused) as refused:
            correction_handlers._handle_crossover_v2_position_ready(_json_handler('{"index": 1, "attempt": 1}'))
        assert refused.value.code == "capture_slot_busy"


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
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        correction_capture._enforce_session_volume_ceiling(quiet)
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        correction_capture._enforce_session_volume_ceiling(stale)
        with pytest.raises(CaptureBeginRefused) as refused:
            gate.gate(7, 7, _entry(-22, POSITION_ROLE_OFFAX))
        assert refused.value.code == SESSION_CEILING_EXPIRED_CODE
    # A tap-paced session (every ungated round) registers no gate at all, and
    # the same poll must still enforce the ceiling rather than raise on the
    # missing gate.
    correction_capture._enforce_session_volume_ceiling(stale)


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
    assert gate.published()["pending"]["index"] == 2

    gate.abandon_hold()
    assert gate.published()["pending"] is None
    gate.abandon_hold()  # idempotent, and safe with nothing open

    with pytest.raises(CaptureBeginDeferred):
        gate.gate(1, 3, _entry(0))
    pending = gate.published()["pending"]
    assert (pending["index"], pending["attempt"], pending["degrees"]) == (1, 3, 0)
    gate.release(1, 3)
    gate.gate(1, 3, _entry(0))


def test_the_release_route_admits_the_pending_capture():
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        body = correction_handlers._handle_crossover_v2_position_ready(_json_handler('{"index": 4, "attempt": 4}'))
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
    from jasper.web import (
        correction_setup,
    )

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
                "/crossover/v2/position-ready", b'{"index": 9, "attempt": 4}',
            )
            assert status == 409, payload
            assert json.loads(payload)["code"] == "capture_slot_busy"
            # The good release still answers 200 on the same server.
            ok_status, ok_payload = post(
                "/crossover/v2/position-ready", b'{"index": 4, "attempt": 4}',
            )
        assert ok_status == 200, ok_payload
        assert json.loads(ok_payload)["ok"] is True
        gate.gate(4, 4, _entry(7))  # admitted — no raise


def test_the_release_route_demands_an_index_it_can_check():
    """An untargeted release is the hazard: a mismatched index is refused
    rather than applied to whatever happens to be pending."""
    gate = PositionGate()
    with _live_remote_slot(gate):
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))
        with pytest.raises(CrossoverV2Refused) as refused:
            correction_handlers._handle_crossover_v2_position_ready(_json_handler('{"index": 9, "attempt": 4}'))
        assert refused.value.code == "capture_slot_busy"
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(4, 4, _entry(7))


def test_the_release_route_is_allowlisted_and_matches_the_minted_action():
    """The endpoint the envelope mints must be one the dispatcher will accept —
    a self-describing action pointing at an unrouted path is a dead contract."""
    from jasper.web import (
        correction_setup,
    )

    assert POSITION_READY_ENDPOINT.endswith("/crossover/v2/position-ready")
    assert "/crossover/v2/position-ready" in correction_setup._POST_ROUTES


def test_a_tap_paced_session_registers_no_gate_at_all():
    """A session opened with no gate advertises no hold — the capture round's
    shape, and the one a household paces with its own taps on the page."""
    from jasper.web import (
        correction_capture,
    )

    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot("crossover_v2:session")
    try:
        assert correction_capture._capture_position_gate is None
        capture = correction_capture._get_capture_slot_for("crossover_v2:")
        assert "position_pending" not in capture
    finally:
        correction_capture._set_capture_slot(None)
