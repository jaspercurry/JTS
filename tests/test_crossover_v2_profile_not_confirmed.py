# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issues #1820 / #1821 — the profile-not-confirmed refusal surface.

From the 2026-07-28 JTS3 dead-end: the owner declared an enclosure kind, which
rotated the driver-safety profile's fingerprint and so cleared its confirmation
by design, and every subsequent crossover measurement was refused. Four things
were wrong with how that refusal reached the household, and one with WHEN:

1. The raw internal slug reached the DOM. ``ProgramPlaybackRefused`` builds its
   message by joining raw enum values, and ``correction_setup``'s capture-failure
   mapper had no branch for the program family, so ``str(exc)`` — "program
   re-admission refused: program_profile_not_confirmed" — was echoed on the
   wizard's capture status line. ``crossover_v2_flow``'s own written contract says
   "never a bare code reaches the household"; NO test pinned it.
2. The advice looped. The copy this refusal inherited said "re-check the driver
   details in speaker setup" — and editing those details rotates the fingerprint
   again, which is the one action that makes it worse.
3. The classification collapsed. Every program-family exception became one
   ``program_unplayable`` code, so a deterministic missing confirmation and a
   real level-ceiling failure were indistinguishable to every caller.
4. (#1821) The confirmation was only evaluated at CHECK-phase program admission
   — after the capture session and phone link existed. The session-open gate
   checked only that a profile object was PRESENT while its refusal text claimed
   confirmation had been checked.

The ``/sound/`` half of defect 3 (the buried confirm control) is pinned in
``tests/test_sound_profile_confirm_deeplink.py``.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_PROGRAM_PROFILE_INCOMPLETE,
    REASON_PROGRAM_PROFILE_MISSING,
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    REASON_PROGRAM_UNPLAYABLE,
    REASON_PROTECTION_NOT_SEPARABLE,
    REASON_PROTECTION_SWEEP_TOO_LOW,
    REASON_REGISTRY,
    TEMPLATE_HARD_STOP,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2FlowError
from jasper.active_speaker.driver_safety import (
    build_driver_safety_profile,
    evaluate_driver_safety_profile,
)
from jasper.active_speaker.program_admission import (
    ProgramAdmission,
    ProgramAdmissionError,
    ProgramAdmissionRefusal,
)
from jasper.active_speaker.program_playback import (
    ProgramPlaybackError,
    ProgramPlaybackRefused,
)
from jasper.active_speaker.crossover_v2 import conductor_context as v2ctx
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_setup
from tests.crossover_v2_fixtures import fake_measurement_mic


def _admission(*refusals: ProgramAdmissionRefusal) -> ProgramAdmission:
    return ProgramAdmission(
        program_id="prog-1",
        phase="check",
        session_volume_db=-20.0,
        segments=(),
        channels=(),
        refusals=refusals,
    )


def _refused(*refusals: ProgramAdmissionRefusal) -> ProgramPlaybackRefused:
    return ProgramPlaybackRefused(_admission(*refusals))


# --------------------------------------------------------------------------- #
# defect 1 — the contract that had no test
# --------------------------------------------------------------------------- #


def _assert_household_copy(code: str, field: str, text: str) -> None:
    """One rendered string must be prose a household can read, never a slug."""

    where = f"{code}.{field}"
    assert text != code, f"{where} is the bare code"
    assert code not in text, f"{where} leaks its own code into its copy"
    # A slug is lower_snake_case; household copy is prose. Any bare snake_case
    # token is a leak of an internal identifier.
    leaked = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)
    assert not leaked, f"{where} carries internal identifiers: {leaked}"
    assert text[0].isupper(), f"{where} does not start a sentence"


def test_every_reason_renders_household_copy_never_a_bare_code():
    """``crossover_v2_flow``'s §5.10 header states the contract in prose —
    "never a bare code reaches the household; the envelope renders each through
    its template copy" — and nothing asserted it. Pin it for the WHOLE registry,
    not just the code this issue was filed about.

    EVERY rendered field is checked independently, not ``message or banner``:
    that short-circuit meant a spec carrying both (the silent-auto-retry codes)
    never had its ``banner`` — the string those codes actually SHOW — examined
    at all, and a mutation that turned a banner into a slug survived this test.
    Same hole for ``next_action.label``, the copy-carrying field this issue
    added."""

    assert REASON_REGISTRY, "the registry must not be empty"
    for code, spec in REASON_REGISTRY.items():
        assert spec.message or spec.banner, f"{code} renders no household copy"
        for field in ("message", "banner"):
            text = getattr(spec, field)
            if text:  # a decision screen has no banner; a retry has no message
                _assert_household_copy(code, field, text)
        if spec.next_action is None:
            continue
        action = spec.next_action
        # The action is a rendered control: its label is household copy, its id
        # is internal, and its destination must be a same-origin management
        # path (never an absolute URL the household could be walked off to).
        assert set(action) >= {"id", "label", "href"}, f"{code} action is incomplete"
        _assert_household_copy(code, "next_action.label", str(action["label"]))
        assert str(action["id"]), f"{code} action has no id"
        href = str(action["href"])
        assert href.startswith("/"), f"{code} action href is not a local path: {href}"
        assert "//" not in href, f"{code} action href is not same-origin: {href}"


def test_program_refusal_reaches_the_wizard_as_copy_not_a_slug():
    """The observed leak. ``str(exc)`` is built from raw enum values at the
    raise site; the wizard's capture status line echoes whatever this mapper
    returns."""

    exc = _refused(ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED)
    assert str(exc) == "program re-admission refused: program_profile_not_confirmed"

    message = correction_setup._capture_failure_message(exc)
    assert message == REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    assert "program_profile_not_confirmed" not in message
    assert "re-admission" not in message


@pytest.mark.parametrize(
    "exc, expected_code",
    [
        (
            _refused(ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED),
            REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
        ),
        (
            _refused(ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP),
            REASON_PROGRAM_UNPLAYABLE,
        ),
        (ProgramPlaybackError("no current DSP config to restore"), REASON_PROGRAM_UNPLAYABLE),
        (ProgramAdmissionError("program must be an ExcitationProgram"), REASON_PROGRAM_UNPLAYABLE),
        # A message the live f-string can actually produce: the bounds are
        # MIN/MAX_CLOUD_MEASURE_POSITIONS (6..12 today), and 9 is the shipped
        # DEFAULT, so the previous literal ("must be 3..7, got 9") described a
        # refusal that cannot happen and read as a captured real message.
        (CrossoverV2FlowError("cloud_measure_positions must be 6..12, got 14"), REASON_PROGRAM_UNPLAYABLE),
    ],
)
def test_whole_program_family_is_mapped_at_the_wizard_boundary(exc, expected_code):
    """Not just the refusal shape: every member of the family the session
    runner classifies must also be mapped here, or the next one to fire leaks
    its own programmer string."""

    assert correction_setup._capture_failure_message(exc) == (
        REASON_REGISTRY[expected_code].message
    )


def test_non_program_exceptions_still_fall_through_unchanged():
    """Scope guard: this fix must not swallow every other exception's message."""

    assert correction_setup._capture_failure_message(
        ValueError("device mismatch")
    ) == "device mismatch"


# --------------------------------------------------------------------------- #
# defect 4 — classification collapse
# --------------------------------------------------------------------------- #


def test_classifier_preserves_refusal_identity_and_slugs():
    profile = v2host.classify_program_failure(
        _refused(ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED)
    )
    assert profile == (
        REASON_PROGRAM_PROFILE_NOT_CONFIRMED, ("program_profile_not_confirmed",)
    )

    over_cap = v2host.classify_program_failure(
        _refused(ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP)
    )
    assert over_cap == (
        REASON_PROGRAM_UNPLAYABLE, ("program_channel_peak_over_cap",)
    )

    # A mixed refusal keeps the specific screen — the confirmation is the one
    # the household can act on, and every other slug still rides out.
    mixed = v2host.classify_program_failure(_refused(
        ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP,
        ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED,
    ))
    assert mixed[0] == REASON_PROGRAM_PROFILE_NOT_CONFIRMED
    assert set(mixed[1]) == {
        "program_channel_peak_over_cap", "program_profile_not_confirmed",
    }


def test_classifier_returns_none_outside_the_program_family():
    """"Not mine" must be distinguishable from "mine, program_unplayable" — the
    capture mapper's fall-through depends on it."""

    assert v2host.classify_program_failure(ValueError("device mismatch")) is None
    assert v2host.classify_program_failure(TimeoutError("read timed out")) is None


def test_classifier_preserves_typed_conditioning_slug():
    from jasper.audio_measurement.program_analysis import (
        ConfiguredPathConditioningError,
        ILL_CONDITIONED_PROTECTION_DEEMBEDDING,
    )

    exc = ConfiguredPathConditioningError("P below -12 dB for tweeter")
    # NOT program_unplayable: that code's copy claims JTS "could not play the
    # measurement signal within the speaker's safe limits", and the program
    # played fine — the offline evidence math refused. The slug still rides out
    # in the detail so the journal and state stay correlatable.
    assert v2host.classify_program_failure(exc) == (
        REASON_PROTECTION_NOT_SEPARABLE, (ILL_CONDITIONED_PROTECTION_DEEMBEDDING,),
    )
    assert REASON_PROTECTION_NOT_SEPARABLE != REASON_PROGRAM_UNPLAYABLE


def test_the_two_conditioning_branches_name_two_different_levers():
    """#1820 again: a refusal must not send the household to a dead control.

    ``abs(P) < floor`` does not involve ``C``, so "change the crossover
    frequency" cannot clear it; ``abs(C/P) > cap`` is the branch Fc moves.
    Same slug either way, so the journal stays correlatable.
    """
    from jasper.audio_measurement.program_analysis import (
        ILL_CONDITIONED_PROTECTION_DEEMBEDDING,
        ConfiguredPathConditioningError,
    )

    ratio = ConfiguredPathConditioningError("C/P above +12 dB for tweeter")
    floor = ConfiguredPathConditioningError(
        "P below -12 dB for tweeter", protection_floor=True,
    )
    ratio_code, ratio_slugs = v2host.classify_program_failure(ratio)
    floor_code, floor_slugs = v2host.classify_program_failure(floor)
    assert ratio_code == REASON_PROTECTION_NOT_SEPARABLE
    assert floor_code == REASON_PROTECTION_SWEEP_TOO_LOW
    assert ratio_slugs == floor_slugs == (ILL_CONDITIONED_PROTECTION_DEEMBEDDING,)

    ratio_copy = REASON_REGISTRY[ratio_code].message
    floor_copy = REASON_REGISTRY[floor_code].message
    # The dead lever must NOT appear on the branch it cannot move.
    assert "crossover frequency" in ratio_copy and "protection" in floor_copy
    assert "crossover frequency" not in floor_copy


def test_the_flow_reason_code_is_the_admission_slug():
    """The 1:1 that makes ``state["failure"]`` correlatable with the journal.
    Pinned so the two vocabularies cannot drift apart silently."""

    assert REASON_PROGRAM_PROFILE_NOT_CONFIRMED == (
        ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED.value
    )


# --------------------------------------------------------------------------- #
# defect 2 — the banner that looped
# --------------------------------------------------------------------------- #


def test_profile_not_confirmed_copy_names_the_save_never_re_editing():
    """The harmful advice, pinned out. "Re-check the driver details" was the one
    action that made this worse. And the copy must no longer name a CONFIRM
    step: there isn't one — the states that still reach this code (``stale``,
    ``malformed``) are cleared by an ordinary save."""

    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]
    copy = spec.message.lower()
    assert "save them again" in copy
    assert "re-check the driver details" not in copy
    assert "confirm" not in copy
    # Terminal: a second identical measurement reproduces it exactly.
    assert spec.template == TEMPLATE_HARD_STOP
    assert spec.retry_budget == 0


def test_profile_not_confirmed_action_deep_links_the_review_callout():
    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]
    assert spec.next_action == {
        "id": "review_safety_limits",
        "label": "Review safety limits",
        "href": "/sound/setup/#confirm-safety-limits",
    }


# --------------------------------------------------------------------------- #
# the two states where "review and save" is the WRONG action (review round,
# S2/S3)
# --------------------------------------------------------------------------- #


def test_missing_profile_never_tells_the_household_to_review_limits():
    """Reproduced by the reviewer on a never-saved / unreadable / pre-crossover
    draft: ``/sound/`` renders NO safety callout when the evaluation is
    ``missing`` (correctly — there is no declaration to review), so copy telling
    the household to review the limits names a panel that is not on the page.
    This state keeps the pre-gate's original, correct action."""

    assert v2ctx.profile_refusal_code("missing") == REASON_PROGRAM_PROFILE_MISSING
    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_MISSING]
    copy = spec.message.lower()
    assert "review the limits" not in copy
    assert "finish the driver details" in copy
    assert spec.template == TEMPLATE_HARD_STOP and spec.retry_budget == 0
    # No fragment: there is no callout to land on in this state.
    assert spec.next_action is not None
    assert spec.next_action["href"] == "/sound/setup/"


def test_incomplete_profile_asks_for_the_values_not_a_save_that_changes_nothing():
    """A save with values missing rebuilds the same ``incomplete`` profile, so
    "save them again" would send the household in a circle. The refusal copy
    names the same action ``/sound/``'s own callout already names for this
    state."""

    assert (
        v2ctx.profile_refusal_code("incomplete") == REASON_PROGRAM_PROFILE_INCOMPLETE
    )
    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_INCOMPLETE]
    copy = spec.message.lower()
    assert "still missing" in copy
    assert "advanced" in copy
    assert spec.next_action is not None
    assert spec.next_action["label"] == "Add the missing limits"
    assert "save" not in spec.next_action["label"].lower()


@pytest.mark.parametrize("status", ["stale", "malformed", ""])
def test_every_other_evaluation_status_is_cleared_by_saving(status):
    """One ordinary save rebuilds the profile from the visible values, so an
    output change and a corrupt artifact end the same way. An unknown status
    falls here too — fail toward the state whose remedy always exists.

    ``unconfirmed`` is deliberately absent: the evaluation no longer produces
    it, because saving the declaration IS declaring it."""

    assert v2ctx.profile_refusal_code(status) == REASON_PROGRAM_PROFILE_NOT_CONFIRMED


def test_hard_stop_screen_renders_the_reasons_own_action():
    """The override reaches the rendered screen, and the generic destination is
    still what every other hard-stop reason gets."""

    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    def _status(code: str) -> dict[str, Any]:
        return {
            "active": True,
            "setup": {"active": True, "status": "ready"},
            # Stamped now: only a failure the household is currently looking
            # at renders its terminal screen (#1942).
            "crossover_v2": {"failure": {"code": code, "at": time.time()}},
        }

    env = build_crossover_envelope_v2(_status(REASON_PROGRAM_PROFILE_NOT_CONFIRMED))
    assert env["screen"] == "hard_stop"
    assert env["next_action"]["href"] == "/sound/setup/#confirm-safety-limits"
    assert env["next_action"]["label"] == "Review safety limits"
    assert env["verdict_text"] == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )

    generic = build_crossover_envelope_v2(_status(REASON_PROGRAM_UNPLAYABLE))
    assert generic["screen"] == "hard_stop"
    assert generic["next_action"] == {
        "id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/setup/",
    }


# --------------------------------------------------------------------------- #
# #1821 — the pre-flight, end to end at session open
# --------------------------------------------------------------------------- #


def _profile(topology, *, saved_at: str = "2026-07-28T12:00:00Z"):
    from tests.test_active_speaker_driver_safety import _manual_settings

    return build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        saved_at=saved_at,
    )


@pytest.fixture()
def session_open(monkeypatch):
    """Drive the REAL session-open path with real driver-safety evaluation.

    Only the seams this test is not about are stubbed: the crossover-preview
    ensure (global disk state) and the evidence-store bundle I/O. The safety
    profile, its evaluation, the topology, and the ceiling/volume derivations
    are all real, so the gate under test is the production one.

    ``calls`` records the two side effects that must NOT happen on a refused
    pre-flight — the evidence-store bundle open (the first durable write after
    the gate) and ``_mint_wired_session`` (the wired-mic session that opens the
    measurement). That seam is wired to raise if it is ever reached, so "no
    session minted" is pinned by construction, not by absence of an assertion.
    """
    # The preparers' mic gate (#2662 W2b S3) resolves the measurement mic
    # before any bundle opens. The gate under test is the SAFETY one, so the
    # mic is named rather than depended on being plugged into whatever
    # machine runs this suite.
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic", fake_measurement_mic,
    )
    from jasper import output_topology as output_topology_mod
    from jasper.active_speaker import commission_wiring, design_draft
    from jasper.active_speaker.tone_plan import load_active_speaker_preset
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology(card_id="DAC8")
    monkeypatch.setattr(
        output_topology_mod, "load_output_topology", lambda *a, **k: topology
    )
    preset = load_active_speaker_preset()
    monkeypatch.setattr(
        commission_wiring, "resolve_capture_preset", lambda topo: preset
    )
    monkeypatch.setattr(v2ctx, "ensure_crossover_preview_ready", lambda: None)

    calls: dict[str, list[Any]] = {"evidence_store": [], "open_capture": []}

    def _evidence_store(topo):
        calls["evidence_store"].append(topo)
        return object(), "sess-fake"

    def _open_capture(*args, **kwargs):
        calls["open_capture"].append(args)
        raise AssertionError(
            "a refused pre-flight must never reach the wired session mint"
        )

    monkeypatch.setattr(v2host, "open_v2_evidence_store", _evidence_store)
    monkeypatch.setattr(v2host, "_mint_wired_session", _open_capture)

    def _install(profile) -> None:
        monkeypatch.setattr(
            design_draft,
            "load_design_draft",
            lambda **kw: {"driver_safety_profile": profile},
        )

    from jasper.active_speaker.measurement import active_driver_targets

    # The REAL per-role target fingerprints — the same values
    # ``build_driver_safety_profile`` stores — so the confirmed case reaches the
    # ceiling resolution instead of refusing on an invented fingerprint.
    status = {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {"drivers": [
            {
                "role": str(target["role"]),
                "target_fingerprint": str(target["target_fingerprint"]),
            }
            for target in active_driver_targets(topology)
        ]},
    }
    return SimpleNamespace(
        topology=topology, status=status, install=_install, calls=calls,
    )


def test_an_unreadable_profile_refuses_at_session_open_with_the_named_reason(
    session_open, caplog,
):
    """No link minted, no session burned: ``prepare_v2_session`` raises BEFORE
    it registers the capture session, and it says the same sentence the phone's
    failure screen would have said four screens later.

    The state exercised is ``malformed`` rather than the retired
    ``unconfirmed``: a declaration JTS cannot read back is one of the two things
    that still legitimately stops the loop."""
    import logging

    env = session_open
    profile = dict(_profile(env.topology))
    profile["profile_fingerprint"] = "0" * 64
    assert evaluate_driver_safety_profile(
        topology=env.topology, profile=profile
    ).status == "malformed"
    env.install(profile)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
            v2host.prepare_v2_session(
                {}, status=env.status, run_async=None, camilla_factory=None
            )

    assert str(excinfo.value) == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )
    assert "save them again" in str(excinfo.value).lower()
    assert excinfo.value.code == REASON_PROGRAM_PROFILE_NOT_CONFIRMED
    assert "event=correction.crossover_v2_profile_not_confirmed" in caplog.text
    assert "gate=session_open" in caplog.text
    assert "profile_status=malformed" in caplog.text
    # Nothing downstream of the gate ran: no evidence bundle opened, and the
    # capture registration that mints the phone link was never reached (its seam
    # raises loudly if it is — see the fixture).
    assert env.calls["evidence_store"] == []
    assert env.calls["open_capture"] == []


def test_a_stale_profile_is_caught_by_the_same_session_open_gate(session_open):
    """The gate evaluates against the LIVE topology, so an output change (which
    the old presence-only check sailed past) refuses here too."""

    env = session_open
    profile = _profile(env.topology)
    stale = dict(profile)
    stale["topology_id"] = "some-other-topology"
    env.install(stale)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status=env.status, run_async=None, camilla_factory=None
        )
    assert str(excinfo.value) == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )
    assert env.calls["open_capture"] == []


@pytest.mark.parametrize(
    "profile, expected",
    [
        (None, REASON_PROGRAM_PROFILE_MISSING),
        ("not-a-mapping", REASON_PROGRAM_PROFILE_MISSING),
    ],
)
def test_a_missing_profile_refuses_with_the_finish_setup_reason(
    session_open, profile, expected,
):
    """The reviewer's reproduction: a never-saved / unreadable / pre-crossover
    draft. The old copy told the household to confirm a control ``/sound/`` does
    not render in this state."""

    env = session_open
    env.install(profile)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status=env.status, run_async=None, camilla_factory=None
        )
    assert excinfo.value.code == expected
    assert str(excinfo.value) == REASON_REGISTRY[expected].message
    assert "confirm the safety limits" not in str(excinfo.value).lower()
    assert env.calls["open_capture"] == []


def test_the_refusal_carries_its_own_resolution_action_for_the_400_body():
    """S1: a pre-flight refusal never reaches the envelope (no persisted
    failure), so the action rides the 400 body instead — same registry entry the
    hard-stop screen reads."""

    refused = v2host.CrossoverV2Refused(
        "copy", code=REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    )
    assert v2host.refusal_next_action(refused) == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].next_action
    )
    # Mutating the response body must not reach back into the registry.
    action = v2host.refusal_next_action(refused)
    assert action is not None
    action["href"] = "/tampered/"
    assert REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].next_action[
        "href"
    ] == "/sound/setup/#confirm-safety-limits"

    # An uncoded refusal (the many whose only honest answer is prose) offers no
    # button rather than a guessed one.
    assert v2host.refusal_next_action(v2host.CrossoverV2Refused("no code")) is None
    assert v2host.refusal_next_action(ValueError("not ours")) is None


def test_a_freshly_edited_and_saved_profile_still_mints_a_session(session_open):
    """The nanny loop, pinned shut at the session-open seam.

    A safety-relevant edit rotates the fingerprint, which USED to clear the
    confirmation and refuse every measurement until a human clicked Confirm --
    the state the crossover-accept seam put jts3 into with its own machine-
    measured write. Saving now re-stamps the declaration, so the session opens
    on the freshly-rotated fingerprint with no human in the loop.

    Also the cannot-over-block half of the "no link minted" pin: the evidence
    store IS opened here, so its emptiness on every refused path above is a
    real observation about the gate rather than a stub that never runs."""

    from tests.test_active_speaker_driver_safety import _manual_settings

    env = session_open
    before = _profile(env.topology)

    edited = _manual_settings()
    edited["drivers"][1]["measurement_band_hz"] = [5000.0, 19000.0]
    profile = build_driver_safety_profile(
        env.topology,
        manual_settings=edited,
        driver_research=None,
        saved_at="2026-07-28T12:05:00Z",
    )
    assert profile["profile_fingerprint"] != before["profile_fingerprint"]
    assert evaluate_driver_safety_profile(
        topology=env.topology, profile=profile
    ).confirmed_and_current is True
    env.install(profile)

    prepared = v2host.prepare_v2_session(
        {}, status=env.status, run_async=None, camilla_factory=None
    )
    assert prepared.label == v2host.V2_CAPTURE_KIND_SESSION
    assert env.calls["evidence_store"] != []


def test_applied_profile_not_confirmed_renders_verify_fail_with_a_working_exit():
    """N3: the applied-session combination.

    With ``applied=True`` the envelope promotes any non-verify_fail code to the
    verify-fail screen (so an applied household is never told "start over" with
    no Undo). That screen's default "Try again" re-verifies, and a re-verify
    runs the same session-open pre-flight — so on THIS code it deterministically
    400s. That is honest as far as it goes (the speaker really is un-measurable
    until the limits are confirmed) and it is no longer a dead end, because the
    400 now carries the confirm action as a button. Undo stays available
    throughout.

    Pinned as the CURRENT behaviour, not endorsed as the final shape: a
    terminal-for-this-session refusal rendering as a retryable verify-fail is a
    two-stage-flow question, noted for that work order rather than redesigned
    here."""

    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": {
            "applied": True,
            # "The way back stays available throughout" is this test's claim,
            # and that takes a prior banked candidate — set explicitly so the
            # screen under test is the one the docstring describes.
            "previous_candidate_fingerprint": "b" * 64,
            # Stamped now: only a failure the household is currently looking
            # at renders its terminal screen (#1942).
            "failure": {
                "code": REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
                "at": time.time(),
            },
        },
    })
    assert env["screen"] == "verify_fail"
    # The household still reads the honest reason and still has the way back.
    assert env["verdict_text"] == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )
    ids = [action["id"] for action in env["alternate_actions"]]
    assert "republish_previous" in ids
    # The retry the screen offers posts the verify route, which is exactly the
    # route the pre-flight guards — so the 400-body action is what keeps this
    # combination from being a loop.
    assert env["next_action"]["endpoint"] == "/sound/speaker/crossover/v2/verify"


# --------------------------------------------------------------------------- #
# #1833 — the rewrap that ran BEFORE classification
# --------------------------------------------------------------------------- #


def test_plan_shape_refusal_is_classified_before_it_is_rewrapped():
    """Both ``resolve_plan_shape`` call sites used to do
    ``raise CrossoverV2Refused(str(exc))``, which is a one-way door: the moment
    a ``CrossoverV2FlowError`` becomes a ``ValueError``,
    :func:`classify_program_failure` stops claiming it and the wizard's 400 arm
    echoes the programmer string into the DOM. Classify first, then rewrap.

    ``test_whole_program_family_is_mapped_at_the_wizard_boundary`` did not and
    could not catch this: it hands the mapper a raw ``CrossoverV2FlowError``,
    which is precisely the shape the rewrap destroys before the mapper is ever
    reached (#1833).
    """
    spec = REASON_REGISTRY[REASON_PROGRAM_UNPLAYABLE]

    with pytest.raises(v2host.CrossoverV2Refused) as raised:
        v2host.prepare_v2_session(
            {"tier": "turbo"},
            status={},
            run_async=None,
            camilla_factory=None,
        )
    assert str(raised.value) == spec.message
    assert raised.value.code == REASON_PROGRAM_UNPLAYABLE
    assert "turbo" not in str(raised.value)
    _assert_household_copy(
        REASON_PROGRAM_UNPLAYABLE, "prepare_v2_session", str(raised.value),
    )

    # The verify-stage site resolves the tier off durable state instead of the
    # request body, and leaked identically.
    with pytest.raises(v2host.CrossoverV2Refused) as verify_raised:
        v2host._verify_plan_shape(
            {v2host.VERIFY_STAGE_KEY: v2host.VERIFY_STAGE_POST_APPLY},
            {"tier": "turbo"},
        )
    assert str(verify_raised.value) == spec.message
    assert verify_raised.value.code == REASON_PROGRAM_UNPLAYABLE


def test_plan_shape_refusal_keeps_the_raw_constraint_in_the_journal(caplog):
    """Household copy names no constraint by design, so the ONE site that
    discards the flow text has to put it somewhere an operator can read.
    Otherwise the fix trades a DOM leak for a debugging dead end."""
    import logging

    caplog.set_level(logging.WARNING, logger=v2host.logger.name)
    with pytest.raises(v2host.CrossoverV2Refused):
        v2host.prepare_v2_session(
            {"tier": "turbo"}, status={}, run_async=None, camilla_factory=None,
        )

    lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(
            "event=correction.crossover_v2_plan_shape_refused"
        )
    ]
    assert len(lines) == 1
    assert "turbo" in lines[0]
    assert f"code={REASON_PROGRAM_UNPLAYABLE}" in lines[0]
    assert "error_type=CrossoverV2FlowError" in lines[0]
