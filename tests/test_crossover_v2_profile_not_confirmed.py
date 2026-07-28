# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issues #1820 / #1821 — the profile-not-confirmed refusal surface.

From the 2026-07-28 JTS3 dead-end: the owner declared an enclosure kind, which
rotated the driver-safety profile's fingerprint and so cleared its confirmation
by design, and every subsequent crossover measurement was refused. Four things
were wrong with how that refusal reached the household, and one with WHEN:

1. The raw internal slug reached the DOM. ``ProgramPlaybackRefused`` builds its
   message by joining raw enum values, and ``correction_setup``'s relay-failure
   mapper had no branch for the program family, so ``str(exc)`` — "program
   re-admission refused: program_profile_not_confirmed" — was echoed on the
   wizard's relay status line. ``crossover_v2_flow``'s own written contract says
   "never a bare code reaches the household"; NO test pinned it.
2. The advice looped. The copy this refusal inherited said "re-check the driver
   details in speaker setup" — and editing those details rotates the fingerprint
   again, which is the one action that makes it worse.
3. The classification collapsed. Every program-family exception became one
   ``program_unplayable`` code, so a deterministic missing confirmation and a
   real level-ceiling failure were indistinguishable to every caller.
4. (#1821) The confirmation was only evaluated at CHECK-phase program admission
   — after the relay session and phone link existed. The session-open gate
   checked only that a profile object was PRESENT while its refusal text claimed
   confirmation had been checked.

The ``/sound/`` half of defect 3 (the buried confirm control) is pinned in
``tests/test_sound_profile_confirm_deeplink.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2_flow import (
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    REASON_PROGRAM_UNPLAYABLE,
    REASON_REGISTRY,
    TEMPLATE_HARD_STOP,
    CrossoverV2FlowError,
)
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
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_setup


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


def test_every_reason_renders_household_copy_never_a_bare_code():
    """``crossover_v2_flow``'s §5.10 header states the contract in prose —
    "never a bare code reaches the household; the envelope renders each through
    its template copy" — and nothing asserted it. Pin it for the WHOLE registry,
    not just the code this issue was filed about: every entry must carry
    renderable copy that is a sentence, and must never be (or contain) its own
    snake_case identifier."""

    assert REASON_REGISTRY, "the registry must not be empty"
    for code, spec in REASON_REGISTRY.items():
        rendered = spec.message or spec.banner
        assert rendered, f"{code} renders no household copy at all"
        assert rendered != code
        assert code not in rendered, f"{code} leaks its own code into its copy"
        # A slug is lower_snake_case; household copy is prose. Any bare
        # snake_case token in the copy is a leak of an internal identifier.
        leaked = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", rendered)
        assert not leaked, f"{code} copy carries internal identifiers: {leaked}"
        assert rendered[0].isupper(), f"{code} copy does not start a sentence"


def test_program_refusal_reaches_the_wizard_as_copy_not_a_slug():
    """The observed leak. ``str(exc)`` is built from raw enum values at the
    raise site; the wizard's relay status line echoes whatever this mapper
    returns."""

    exc = _refused(ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED)
    assert str(exc) == "program re-admission refused: program_profile_not_confirmed"

    message = correction_setup._relay_failure_message(exc)
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
        (CrossoverV2FlowError("cloud_measure_positions must be 3..7, got 9"), REASON_PROGRAM_UNPLAYABLE),
    ],
)
def test_whole_program_family_is_mapped_at_the_wizard_boundary(exc, expected_code):
    """Not just the refusal shape: every member of the family the session
    runner classifies must also be mapped here, or the next one to fire leaks
    its own programmer string."""

    assert correction_setup._relay_failure_message(exc) == (
        REASON_REGISTRY[expected_code].message
    )


def test_non_program_exceptions_still_fall_through_unchanged():
    """Scope guard: this fix must not swallow every other exception's message."""

    assert correction_setup._relay_failure_message(
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
    relay mapper's fall-through depends on it."""

    assert v2host.classify_program_failure(ValueError("device mismatch")) is None
    assert v2host.classify_program_failure(TimeoutError("read timed out")) is None


def test_the_flow_reason_code_is_the_admission_slug():
    """The 1:1 that makes ``state["failure"]`` correlatable with the journal.
    Pinned so the two vocabularies cannot drift apart silently."""

    assert REASON_PROGRAM_PROFILE_NOT_CONFIRMED == (
        ProgramAdmissionRefusal.PROFILE_NOT_CONFIRMED.value
    )


# --------------------------------------------------------------------------- #
# defect 2 — the banner that looped
# --------------------------------------------------------------------------- #


def test_profile_not_confirmed_copy_names_confirmation_never_re_editing():
    """The harmful advice, pinned out. Editing the driver details rotates the
    profile fingerprint (``build_driver_safety_profile``), which CLEARS an
    existing confirmation — so "re-check the driver details" is a loop, not a
    fix, for this one reason."""

    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]
    copy = spec.message.lower()
    assert "confirm the safety limits" in copy
    assert "re-check the driver details" not in copy
    # Terminal: a second identical measurement reproduces it exactly.
    assert spec.template == TEMPLATE_HARD_STOP
    assert spec.retry_budget == 0


def test_profile_not_confirmed_action_deep_links_the_confirm_control():
    spec = REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED]
    assert spec.next_action == {
        "id": "confirm_safety_limits",
        "label": "Confirm safety limits",
        "href": "/sound/#confirm-safety-limits",
    }


def test_hard_stop_screen_renders_the_reasons_own_action():
    """The override reaches the rendered screen, and the generic destination is
    still what every other hard-stop reason gets."""

    from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2

    def _status(code: str) -> dict[str, Any]:
        return {
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": {"failure": {"code": code}},
        }

    env = build_crossover_envelope_v2(_status(REASON_PROGRAM_PROFILE_NOT_CONFIRMED))
    assert env["screen"] == "hard_stop"
    assert env["next_action"]["href"] == "/sound/#confirm-safety-limits"
    assert env["next_action"]["label"] == "Confirm safety limits"
    assert env["verdict_text"] == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )

    generic = build_crossover_envelope_v2(_status(REASON_PROGRAM_UNPLAYABLE))
    assert generic["screen"] == "hard_stop"
    assert generic["next_action"] == {
        "id": "speaker_setup", "label": "Back to speaker setup", "href": "/sound/",
    }


# --------------------------------------------------------------------------- #
# #1821 — the pre-flight, end to end at session open
# --------------------------------------------------------------------------- #


def _profile(topology, *, confirm: bool):
    from tests.test_active_speaker_driver_safety import _manual_settings

    return build_driver_safety_profile(
        topology,
        manual_settings=_manual_settings(),
        driver_research=None,
        confirm=confirm,
        confirmed_at="2026-07-28T12:00:00Z" if confirm else None,
    )


@pytest.fixture()
def session_open(monkeypatch):
    """Drive the REAL session-open path with real driver-safety evaluation.

    Only the seams this test is not about are stubbed: the crossover-preview
    ensure (global disk state) and the evidence-store bundle I/O. The safety
    profile, its evaluation, the topology, and the ceiling/volume derivations
    are all real, so the gate under test is the production one.
    """
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
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store", lambda topo: (object(), "sess-fake")
    )

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
    return topology, status, _install


def test_unconfirmed_profile_refuses_at_session_open_with_the_named_reason(
    session_open, caplog,
):
    """No link minted, no session burned: ``prepare_v2_session`` raises BEFORE
    it registers the relay session, and it says the same sentence the phone's
    failure screen would have said four screens later."""
    import logging

    topology, status, install = session_open
    profile = _profile(topology, confirm=False)
    assert evaluate_driver_safety_profile(topology=topology, profile=profile).status == (
        "unconfirmed"
    )
    install(profile)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
            v2host.prepare_v2_session(
                {}, status=status, run_async=None, camilla_factory=None
            )

    assert str(excinfo.value) == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )
    assert "confirm the safety limits" in str(excinfo.value).lower()
    assert "event=correction.crossover_v2_profile_not_confirmed" in caplog.text
    assert "gate=session_open" in caplog.text
    assert "profile_status=unconfirmed" in caplog.text


def test_a_stale_profile_is_caught_by_the_same_session_open_gate(session_open):
    """The gate evaluates against the LIVE topology, so an output change (which
    the old presence-only check sailed past) refuses here too."""

    topology, status, install = session_open
    profile = _profile(topology, confirm=True)
    stale = dict(profile)
    stale["topology_id"] = "some-other-topology"
    install(stale)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.prepare_v2_session(
            {}, status=status, run_async=None, camilla_factory=None
        )
    assert str(excinfo.value) == (
        REASON_REGISTRY[REASON_PROGRAM_PROFILE_NOT_CONFIRMED].message
    )


def test_a_confirmed_profile_still_mints_a_session(session_open):
    """The other half of the gate: confirming is a real exit, not a new wall."""

    topology, status, install = session_open
    profile = _profile(topology, confirm=True)
    assert evaluate_driver_safety_profile(
        topology=topology, profile=profile
    ).confirmed_and_current is True
    install(profile)

    prepared = v2host.prepare_v2_session(
        {}, status=status, run_async=None, camilla_factory=None
    )
    assert prepared.label == v2host.V2_RELAY_KIND_SESSION
