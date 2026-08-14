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

import io
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_envelope_v2 import (
    _TIER_CLAIMS,
    _TIER_LABELS,
    _done_nudges,
    _tier_choice_actions,
)
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_COUNTDOWN,
    PHASE_CLOUD_MEASURE,
    REASON_GEOMETRY_RETAKE_UNREACHABLE,
    REASON_REGISTRY,
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
from jasper.capture_relay.session import CaptureBeginDeferred, CaptureBeginRefused
from jasper.web._common import CSRF_COOKIE_NAME
from jasper.capture_relay.spec import MAX_CAPTURE_PLAN_ATTEMPTS
from jasper.web.correction_crossover_v2 import (
    POSITION_HOLD_CODE,
    POSITION_HOLD_EXPIRED_CODE,
    POSITION_READY_ENDPOINT,
    POSITION_TARGET_MISSING_CODE,
    REMOTE_POSITION_HOLD_BUDGET_S,
    PositionGate,
)

from tests.crossover_v2_fixtures import (
    CLOUD_MEASURE_INDEXES,
    FC_HZ,
    FakeSeams,
    _cloud_conductor,
    _lock,
    _run_phase,
    _walk,
)

HAND_WALKED = (TIER_FULL, TIER_EXPRESS)

#: The bearings the target run is specified in — the whole point of the tier, so
#: they are written down here ONCE as the acceptance criterion and everything
#: else in this file derives from the product code.
STAGE1_ANGLES = (0, -7, 7, -22, 22, 0)
STAGE2_ANGLES = (0, -7, 7, -22, 22)


def _stage1(tier):
    return build_v2_capture_plan(
        flow._DISPLAY_ROLES_BANDS,
        flow._DISPLAY_FC_HZ,
        plan_shape=resolve_plan_shape(tier),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )


def _stage2(tier):
    return build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape(tier))


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
    assert remote.cloud_verify_positions < full.cloud_verify_positions
    # A named shape, not a configurable range: a caller stating a different
    # count is a bug, not a preference.
    with pytest.raises(CrossoverV2FlowError, match="the remote tier is a fixed shape"):
        resolve_plan_shape(TIER_REMOTE, cloud_verify_positions=6)
    # …and express's identical refusal still names EXPRESS, not a shared word.
    with pytest.raises(CrossoverV2FlowError, match="the express tier is a fixed shape"):
        resolve_plan_shape(TIER_EXPRESS, cloud_verify_positions=6)


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
    """``remote_cloud_verify_positions`` is DERIVED off the prompt table: it is
    the longest prefix with no vertical pose, so reordering that table moves the
    number instead of stranding it."""
    positions = remote_cloud_verify_positions()
    walked = flow.CLOUD_POSITION_PROMPTS[: positions - 1]
    assert all(p.role != POSITION_ROLE_XOVR for p in walked)
    # …and it stops exactly THERE — the very next prompt is the vertical one.
    assert flow.CLOUD_POSITION_PROMPTS[positions - 1].role == POSITION_ROLE_XOVR
    # The wide-offset guarantee still holds on the shortened walk (this is why
    # the floor exists, and why the derivation refuses below it).
    assert sum(1 for p in walked if p.wide) >= 2
    assert positions >= flow.MIN_CLOUD_VERIFY_POSITIONS


def test_remotes_stage_1_n_states_the_assumption_that_makes_it_safe(monkeypatch):
    """N4. Remote takes Full's N only because the shipped stage 1 walks the
    LATERAL poses; the ``[:N - 1]`` prefix of the cloud table contains vertical
    rows at that N. Flipping the flag back on must trip a NAMED refusal that
    says what to do, not an incidental raise from the angle helper."""
    assert flow.remote_cloud_measure_positions() == flow.DEFAULT_CLOUD_MEASURE_POSITIONS
    monkeypatch.setattr(flow, "STAGE1_INCLUDES_CLOUD_MEASURE", True)
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
    for — and read back off the PLAN, not off the helper that builds it."""
    stage1 = [int(e.screen[POSITION_DEG_KEY]) for e in _stage1(TIER_REMOTE).entries]
    stage2 = [int(e.screen[POSITION_DEG_KEY]) for e in _stage2(TIER_REMOTE).entries]
    # CHECK and MEASURE are design-axis captures ahead of the walk itself.
    assert stage1[:2] == [0, 0]
    assert tuple(stage1[2:-1]) == STAGE1_ANGLES
    assert stage1[-1] == 0  # the entry baseline, back on the axis
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


def test_remote_costs_the_relay_no_more_than_full_does():
    remote = resolve_plan_shape(TIER_REMOTE)
    full = resolve_plan_shape(TIER_FULL)
    assert remote.max_attempts <= full.max_attempts
    for plan in (_stage1(TIER_REMOTE), _stage2(TIER_REMOTE)):
        assert plan.max_attempts <= MAX_CAPTURE_PLAN_ATTEMPTS
    # The worst-case guard is tier-independent and must stay satisfied.
    flow.assert_cloud_plan_fits_relay_capacity()


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
    the measurement volume, the paused voice, and the relay slot indefinitely.
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
    """Two label tables already exist (the wizard's and the capture page's,
    across a layering boundary that forbids an import). A tier missing from
    either leaks a raw slug or drops a consent line."""
    from jasper.capture_relay.spec import _GUIDED_TIER_LABELS

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


def test_a_hand_walked_group_still_gets_its_wider_retake_prompt(monkeypatch):
    """The other half of S4a: the refusal is scoped to externally positioned
    sessions, and a household that CAN walk to 75 cm is still asked to."""
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
    """Claim the process's single relay slot for a crossover v2 session."""
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture(
        "crossover_v2:session", position_gate=gate,
    )
    try:
        yield correction_setup
    finally:
        correction_setup._set_relay_capture(None)


def test_a_live_hold_reaches_the_envelope_on_the_relay_block():
    """The driver's read path: the gate owns the fact, the relay block carries
    it, and the envelope copies that block through verbatim."""
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(2, 2, _entry(-22, POSITION_ROLE_OFFAX))
        relay = setup._get_relay_capture_for("crossover_v2:")
        pending = relay["position_pending"]
        assert pending["degrees"] == -22
        assert pending["index"] == 2
        assert pending["action"]["endpoint"] == POSITION_READY_ENDPOINT
        # Another flow's reader must never see this session's hold.
        assert setup._get_relay_capture_for("sync:") is None
        gate.release(2)
        assert "position_pending" not in setup._get_relay_capture_for("crossover_v2:")


def test_a_finished_session_stops_advertising_its_hold():
    """The strand check. A hold published into durable state could outlive the
    session holding it; riding the relay slot means the existing terminal
    transition drops it, with no new cleanup path to forget."""
    gate = PositionGate()
    with _live_remote_slot(gate) as setup:
        with pytest.raises(CaptureBeginDeferred):
            gate.gate(1, 1, _entry(0))
        assert setup._get_relay_capture_for("crossover_v2:")["position_pending"]
        # The runner's own terminal publish, verbatim in shape.
        setup._set_relay_capture(
            {"status": "complete", "kind": "crossover_v2:session", "tap_link": "x"}
        )
        assert setup._relay_position_gate is None
        relay = setup._get_relay_capture_for("crossover_v2:")
        assert "position_pending" not in relay
        # …and a late driver POST cannot reach a gate nobody is holding.
        with pytest.raises(ValueError, match="no remote measurement is waiting"):
            setup._handle_crossover_v2_position_ready(_json_handler('{"index": 1}'))


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


def test_a_hand_walked_session_registers_no_gate_at_all():
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture("crossover_v2:session")
    try:
        assert correction_setup._relay_position_gate is None
        relay = correction_setup._get_relay_capture_for("crossover_v2:")
        assert "position_pending" not in relay
    finally:
        correction_setup._set_relay_capture(None)
