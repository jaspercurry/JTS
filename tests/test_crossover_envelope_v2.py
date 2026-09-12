# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Mover screens, preconditions, and shared status projections."""
from __future__ import annotations

import time
from typing import Mapping

import numpy as np
import pytest


from jasper.active_speaker.crossover_envelope_v2 import (
    CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION,
    _PHASE_STEP,
    _per_band_flatness_lines,
    build_crossover_envelope_v2,
    compact_cloud_status,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
    REASON_VERIFY_INCONCLUSIVE,
    reason_message,
    verify_inconclusive_message,
)
from jasper.active_speaker.flat_spec import evaluate_flat_spec, spec_flatness_gauge
from jasper.web.correction_crossover_v2 import (
    _post_apply_grade,
)

V2_STEP_IDS = ("speaker_setup", "microphone_check", "measure", "verify")


def _status(**v2) -> dict:
    failure = v2.get("failure")
    if isinstance(failure, Mapping) and "at" not in failure:
        # #1942: a persisted failure now carries WHEN it happened, and only a
        # fresh one renders its terminal screen. Every fixture below that
        # hands this helper a failure is describing the screen a household is
        # looking at right now, so the helper stamps it fresh — which is what
        # keeps those tests pinning the LIVE path they were written for.
        # The aged and undated (pre-#1942) cases are built inline instead, so
        # a test that means "stale" has to say so out loud.
        v2 = {**v2, "failure": {**failure, "at": time.time()}}
    if "post_apply_grade" not in v2:
        # R19: the envelope reads the PRODUCER's grade — scope, spatial state,
        # completeness — instead of re-deriving any of them from the cloud
        # block. Running the real producer here rather than hand-building the
        # dict is what makes these tests a contract between the two modules:
        # the envelope spells the grade words as literals (jasper.active_speaker
        # never imports jasper.web), and a rename on the producer side stops
        # those branches firing, which fails here rather than shipping.
        # Fixtures that pass their own `post_apply_grade` are describing a
        # state file some OTHER build wrote, and keep it verbatim.
        v2 = {**v2, "post_apply_grade": _post_apply_grade(v2)}
    if "updated_at" not in v2:
        # #1947: the durable state now carries the session's own clock, and
        # only a live session renders its phase screen. Every fixture below is
        # describing the screen a household is looking at right now, so the
        # helper stamps it live — which keeps those tests pinning the LIVE path
        # they were written for. The dead and undated cases say so out loud.
        v2 = {**v2, "updated_at": time.time()}
    return {
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2,
    }


def _step_statuses(env: dict) -> dict[str, str]:
    return {step["id"]: step["status"] for step in env["steps"]}


def _every_screen_envelope() -> dict[str, dict]:
    return {
        "inactive": build_crossover_envelope_v2({"active": False}),
        "speaker_setup": build_crossover_envelope_v2({"active": True}),
        **{phase: build_crossover_envelope_v2(_status(phase=phase))
           for phase in ("check", "measure", "verify", "closing")},
        "finished": build_crossover_envelope_v2({
            **_status(), "capture": {"status": "complete", "run": {"status": "complete"}},
        }),
    }


def test_schema_8_and_v2_step_tuple():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 17
    assert env["flow"] == "v2"
    assert tuple(step["id"] for step in env["steps"]) == V2_STEP_IDS


def test_every_journey_phase_has_a_phase_step_entry():
    """``build_crossover_envelope_v2`` now does a direct ``_PHASE_STEP[phase]``
    lookup (a bare ``.get(phase, "microphone_check")`` used to paper over a
    gap by walking the stepper BACKWARDS to step 1 on the final capture — see
    the table's own comments), so a phase missing from the table raises
    instead of mis-stepping.

    This is the reverse direction from the ``set(_PHASE_STEP)`` tests below
    (search ``others = set(_PHASE_STEP)``): those walk the table's OWN keys
    and assume they are exhaustive. This one walks ``journey``'s ``PHASE_*``
    names — the vocabulary's actual source — and checks the table covers
    every one of them, so a phase added there without a matching entry here
    fails at test time rather than at runtime.
    """
    from jasper.active_speaker.crossover_v2 import journey

    phase_values = {
        value for name, value in vars(journey).items()
        if name.startswith("PHASE_") and isinstance(value, str)
    }
    assert phase_values, "journey should export at least one PHASE_* constant"
    missing = phase_values - set(_PHASE_STEP)
    assert not missing, f"_PHASE_STEP has no entry for: {sorted(missing)}"


def test_legacy_env_still_serves_v2_envelope(monkeypatch):
    """W5b retired the ``JASPER_CROSSOVER_FLOW`` selector and the legacy flow —
    v2 is the only flow now. A box carrying a stale
    ``JASPER_CROSSOVER_FLOW=legacy`` from before the selector was deleted must
    still be served the v2 envelope, not crash or fall back to a deleted legacy
    path. Nothing reads that variable any more, so setting it must be inert.

    This used to run through a ``build_crossover_envelope`` compatibility
    dispatcher. That dispatcher only forwarded to v2 and has been deleted; the
    web flow's entry point is the logged wrapper below, so the contract is
    pinned there instead."""
    from jasper.web.correction_crossover_flow import _build_envelope_logged

    monkeypatch.setenv("JASPER_CROSSOVER_FLOW", "legacy")
    env = _build_envelope_logged(_status(phase="check"))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION == 17
    assert env["flow"] == "v2"


def test_inactive_speaker_gets_not_applicable():
    env = build_crossover_envelope_v2({"active": False})
    assert env["screen"] == "not_applicable"
    assert env["active"] is False
    # Nothing to do on this speaker from this flow, so it mints no action
    # rather than a link into a subsystem that does not apply to it.
    assert env["next_action"] is None
    assert env["alternate_actions"] == []


def test_setup_not_ready_blocks_before_any_capture():
    env = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "blocked"},
        "crossover_v2": {"phase": "check"},
    })
    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["href"] == "/sound/speaker/"
    assert _step_statuses(env)["speaker_setup"] == "active"


def test_awaiting_plan_without_a_staged_run():
    env = build_crossover_envelope_v2(_status(phase="check"))
    assert env["screen"] == "awaiting_plan"
    assert env["next_action"] is None
    assert env["alternate_actions"] == []


@pytest.mark.parametrize("terminal, fault", [
    ("complete", ""), ("stopped", ""),
    ("failed", "measurement_graph_unavailable"), ("failed", "unregistered_fault"),
])
def test_finished_run_uses_the_registry_action_and_reset(terminal, fault):
    env = build_crossover_envelope_v2({
        **_status(phase="verify", candidate={"fingerprint": "candidate"}),
        "capture": {"status": terminal, "run": {"status": terminal, "fault": fault}},
    })
    reset = {"id": "reset", "label": "Start over",
             "endpoint": "/sound/speaker/crossover/reset", "body": {}}
    spec = REASON_REGISTRY.get(fault)
    action = dict(spec.next_action) if spec and spec.next_action else None
    assert env["screen"] == "finished"
    assert env["next_action"] == (action or reset)
    assert env["alternate_actions"] == ([reset] if action else [])
    assert env["capture"] is None
    assert all(env[key] is None for key in ("candidate_review", "prediction", "cloud", "cloud_chart", "round"))


@pytest.mark.parametrize("mover", ["human", "cli", "turntable"])
def test_staged_run_uses_the_measure_screen_and_the_existing_prompt(mover):
    capture = {"status": "awaiting_join", "join": {"mover": mover, "prompt": {"title": "First pose"}}}
    env = build_crossover_envelope_v2({**_status(), "capture": capture})
    assert env["screen"] == "measure"
    assert env["capture"] == capture
    assert env["next_action"] is None


def test_measure_phase_is_phone_driven():
    env = build_crossover_envelope_v2(_status(phase="measure"))
    assert env["screen"] == "measure"
    assert env["next_action"] is None
    assert _step_statuses(env)["measure"] == "active"


def _candidate_summary(**overrides) -> dict:
    base = {
        "fingerprint": "fp-123",
        "program_id": "prog-9",
        "trims_db": {"woofer": -3.1, "tweeter": 0.0},
        "alignment": {"delay_us": 250.0, "delay_role": "woofer", "polarity": "invert"},
        "alignment_confidence": 0.82,
        "predicted_ripple_db": 1.4,
    }
    base.update(overrides)
    return base


def test_verify_phase_screen():
    env = build_crossover_envelope_v2(_status(phase="verify"))
    assert env["screen"] == "verify"
    # STAGE 2's entry point (two-stage work order D2, PR-T3). The measuring
    # session ended at the review screen and the household applied from there,
    # so the post-apply check is a NEW session somebody has to start — and
    # deliberately so, because the session TTL begins ticking at open and the
    # household is still walking back to fetch the phone. It used to be None:
    # the same screen rendered mid-session while the phone drove it, and the
    # shared capture gate still suppresses this action while stage 2's own capture
    # is in flight.
    assert env["next_action"] == {
        "id": "verify_start",
        "label": "Check the result",
        "endpoint": "/sound/speaker/crossover/v2/verify",
        "body": {"stage": "post_apply"},
    }
    assert _step_statuses(env)["verify"] == "active"
    # Full's VERIFY anchor is followed by the post-apply cloud — no
    # express-only disclosure here.
    assert "only check" not in env["verdict_text"].lower()


def test_verify_phase_express_discloses_before_tuning_flatness_from_measure_cloud():
    """B1 fix (adversarial review of PR #1780): express's pre-apply cloud has
    already closed by the time this screen renders (it walks BEFORE VERIFY),
    so its flatness/carve-out disclosure is available here too, not just on
    the done screen — read from CLOUD_MEASURE (express's only cloud), never
    CLOUD_VERIFY (which express never produces)."""
    env = build_crossover_envelope_v2(_status(
        phase="verify", tier="express", cloud=_cloud_measure_flatness_status(),
    ))
    details = env["expert_details"]
    assert details, "express's VERIFY screen must not sit on unread measure-block data"
    assert any("Measured before tuning:" in line for line in details)


def test_volume_recovery_keys_on_needs_recovery_not_unresolved():
    """A crash-hydrated active plan surfaces NO unresolved payload but still
    needs draining — the screen must key on needs_recovery alone."""
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=True))
    assert env["screen"] == "volume_recovery"
    assert env["next_action"]["endpoint"] == "/sound/speaker/crossover/recover-volume"
    # And needs_recovery false ⇒ no recovery screen even with a phase set.
    env = build_crossover_envelope_v2(_status(phase="check", needs_recovery=False))
    assert env["screen"] == "microphone_check"


def _cloud_measure_flatness_status(*, carve_outs=None, **overrides):
    flatness = {
        "max_db": -4.85, "max_hz": 11480.0, "max_band_hz": [8000.0, 16000.0],
        # The frame the deviation is stated against (#1857) — production's
        # ``spec_flatness_gauge`` always emits it, so the fixtures do too.
        "reference_band_hz": [250.0, 8000.0],
        "tolerance_db": 2.5, "rms_db": 1.37, "n_bins": 900, "n_excluded": 42,
        "evaluable": True, "passed": False,
    }
    flatness.update(overrides)
    return {
        PHASE_CLOUD_MEASURE: {
            "geometry_locked": False, "thin_evidence": False,
            "geometry_guidance": "", "spec_bands": [], "overall_within_target": False,
            "excluded_interval_count": 3, "flatness": flatness,
            "carve_outs": carve_outs or [],
        },
    }


def test_compact_cloud_status_reports_positions_accepted_from_the_durable_block():
    """The scoped first step: a household whose walk failed partway through
    should see how much was banked, not just that the group did not close.
    ``positions`` is the durable ``_cloud_summary``'s own key (the surviving
    take per position); the count is real evidence already on disk, only
    never surfaced."""
    compact = compact_cloud_status({
        PHASE_CLOUD_VERIFY: {
            "geometry": {}, "pipeline": {},
            "positions": [{"position_id": f"cloud_verify_0{i}"} for i in range(4)],
        },
    })
    assert compact[PHASE_CLOUD_VERIFY]["positions_accepted"] == 4


@pytest.mark.parametrize("phase", [PHASE_CLOUD_MEASURE, PHASE_LATERAL])
def test_compact_cloud_status_never_fabricates_a_required_count(phase):
    result = compact_cloud_status({phase: {"geometry": {}, "pipeline": {}}})
    assert result[phase]["positions_required"] is None


def _dark_tweeter_compact_cloud(*, phase: str = PHASE_CLOUD_VERIFY):
    """A REAL ``evaluate_flat_spec`` report reproducing #1857's mechanism —
    reproduced from the actual evaluator, not asserted by fiat.

    A narrow +3 dB peak sits in the woofer band; the tweeter band is
    uniformly ~6 dB dark across its ENTIRE passband (no peak, no texture,
    just a whole-band offset); the top band is flat.

    This shape is #1857's misattribution class. While the reference was
    pooled across the woofer+tweeter bands the tweeter's own darkness
    dragged that reference down, and the woofer's narrow (and much smaller)
    peak read a LARGER deviation from it than the tweeter's own uniform
    darkness did. The frame is now the low-mid band alone (ADR-0194), which
    no part of the tweeter band is inside, so the same shape charges each
    band its own deviation — kept here because a shape that USED to
    mis-point is the one worth still rendering the disclosure for.
    """
    n = 1000
    woofer_freqs = np.linspace(250.0, 1999.0, n)
    tweeter_freqs = np.linspace(2000.0, 7999.0, n)
    top_freqs = np.linspace(8000.0, 15999.0, n)
    freqs = np.concatenate([woofer_freqs, tweeter_freqs, top_freqs])

    woofer_curve = np.zeros(n)
    woofer_curve[n // 2] = 3.0  # one narrow +3 dB peak, otherwise flat
    tweeter_curve = np.full(n, -6.0)  # uniformly dark, the WHOLE band
    top_curve = np.zeros(n)
    curve = np.concatenate([woofer_curve, tweeter_curve, top_curve])

    order = np.argsort(freqs)
    freqs, curve = freqs[order], curve[order]

    report = evaluate_flat_spec(freqs, curve, None)
    gauge = spec_flatness_gauge(report)
    pipeline = {
        "available": True,
        "spec": report.to_dict(),
        "flatness": gauge.to_dict(),
        "merged_excluded_bands_hz": [],
        "validity_floor_hz": None,
    }
    compact = compact_cloud_status({phase: {"geometry": {}, "pipeline": pipeline}})
    return compact[phase], report, gauge


def test_the_pre_apply_reading_also_names_every_band():
    """The BEFORE-TUNING branch (``_pre_apply_flatness_lines``) folds every
    ``_flatness_lines_from_block`` line into one ``"Measured before
    tuning: "``-prefixed sentence (the module's own framing rule — these
    numbers must never render bare the way CLOUD-VERIFY renders them). The
    per-band disclosure is the SAME kind of before-tuning claim as the
    pointer it sits beside, so it folds into that SAME sentence rather than
    appearing as a separate, unprefixed line the way carve-outs do."""
    compact, _report, _gauge = _dark_tweeter_compact_cloud(phase=PHASE_CLOUD_MEASURE)
    env = build_crossover_envelope_v2(_status(
        phase="done", verify={"outcome": "pass"},
        cloud={PHASE_CLOUD_MEASURE: compact}, candidate=_candidate_summary(),
    ))
    details = env["expert_details"]
    lead = next(line for line in details if line.startswith("Measured before tuning: "))
    assert "250–2000 Hz +3.00 dB (1.5 dB outside the ±1.5 dB target)" in lead
    assert "2000–8000 Hz -6.00 dB (4.0 dB outside the ±2.0 dB target)" in lead
    assert "8000–16000 Hz -0.00 dB (within the ±2.5 dB target)" in lead


def test_per_band_lines_uniformly_flat_shows_no_alarm():
    """Edge case: nothing wrong anywhere. The new line still renders (every
    band IS evaluable) but shows nothing alarming — three passes, ~0 dB —
    confirming the disclosure does not manufacture a false impression of
    trouble where the pointer already reports none."""
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    report = evaluate_flat_spec(freqs, np.zeros_like(freqs), None)
    spec_bands = [
        {
            "f_lo_hz": b.f_lo_hz, "f_hi_hz": b.f_hi_hz, "within_target": b.within_target,
            "max_deviation_db": b.max_deviation_db, "tolerance_db": b.tolerance_db,
        }
        for b in report.bands
    ]
    lines = _per_band_flatness_lines(spec_bands)
    assert len(lines) == 1
    assert "+0.00 dB (within" in lines[0]
    assert "fail" not in lines[0]


def test_per_band_lines_single_band_defect_leaves_the_others_quiet():
    """Edge case: only the top band is out of spec; woofer and tweeter are
    genuinely flat. The per-band line shows two clean passes and one real
    failure — the ordinary, non-misattribution case, which this disclosure
    must render just as plainly as the drag case above."""
    freqs = np.geomspace(250.0, 16_000.0, 1500)
    curve = np.where(freqs >= 8000.0, -6.0, 0.0)
    report = evaluate_flat_spec(freqs, curve, None)
    spec_bands = [
        {
            "f_lo_hz": b.f_lo_hz, "f_hi_hz": b.f_hi_hz, "within_target": b.within_target,
            "max_deviation_db": b.max_deviation_db, "tolerance_db": b.tolerance_db,
        }
        for b in report.bands
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz +0.00 dB (within" in line
    assert "8000–16000 Hz -6.00 dB (3.5 dB outside" in line


def test_per_band_lines_both_bands_failing_shows_both():
    """Edge case named in #1857's own remedy: BOTH the woofer and tweeter
    genuinely out of spec (not one dragging the other) — the per-band line
    must show both failures, not collapse to the single pointer."""
    spec_bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "within_target": False,
         "max_deviation_db": 3.0, "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "within_target": False,
         "max_deviation_db": -4.5, "tolerance_db": 2.0},
        {"f_lo_hz": 8000.0, "f_hi_hz": 16000.0, "within_target": True,
         "max_deviation_db": 1.0, "tolerance_db": 2.5},
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz +3.00 dB (1.5 dB outside" in line
    assert "2000–8000 Hz -4.50 dB (2.5 dB outside" in line
    assert "8000–16000 Hz +1.00 dB (within" in line


def test_per_band_lines_skips_unevaluable_bands_without_fabricating():
    """A band with no surviving evidence (``within_target`` is ``None``, not a
    bool) contributes no line — the same "unevaluable is not a fabricated
    verdict" rule ``BandResult`` itself follows — rather than printing a
    fake 0 dB reading for a band nothing measured."""
    spec_bands = [
        {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "within_target": None,
         "max_deviation_db": None, "tolerance_db": 1.5},
        {"f_lo_hz": 2000.0, "f_hi_hz": 8000.0, "within_target": False,
         "max_deviation_db": -4.5, "tolerance_db": 2.0},
    ]
    line = _per_band_flatness_lines(spec_bands)[0]
    assert "250–2000 Hz" not in line
    assert "2000–8000 Hz -4.50 dB (2.5 dB outside" in line


def test_per_band_lines_empty_or_malformed_input_renders_nothing():
    """No fabricated line when there is nothing to disclose — mirrors every
    other honesty rule in this module (``[]``, never an empty-looking
    sentence)."""
    assert _per_band_flatness_lines([]) == []
    assert _per_band_flatness_lines(None) == []
    assert _per_band_flatness_lines("not a list") == []
    assert _per_band_flatness_lines([{"within_target": None}, "not a mapping"]) == []


def test_the_registry_holds_the_cause_unknown_rendering_not_a_literal():
    """SSOT: the sentence has ONE writer, and the registry entry is that
    writer's cause-unknown output rather than a second copy of the words that
    could drift from it. Any reader of REASON_REGISTRY therefore gets copy that
    is true, not copy that guesses."""
    assert (
        REASON_REGISTRY[REASON_VERIFY_INCONCLUSIVE].message
        == verify_inconclusive_message(None)
    )
    assert "reflection" not in REASON_REGISTRY[REASON_VERIFY_INCONCLUSIVE].message


def test_no_registry_sentence_names_undo():
    for code, spec in REASON_REGISTRY.items():
        for text in (
            spec.message, spec.banner,
            reason_message(code, spec),
        ):
            assert "undo" not in text.lower(), (code, text)


@pytest.mark.parametrize("code,template", [
    (code, spec.template) for code, spec in REASON_REGISTRY.items()
])
def test_every_registry_code_renders_without_error(code, template):
    env = build_crossover_envelope_v2(_status(phase="measure", failure={"code": code}))
    assert env["schema_version"] == CROSSOVER_V2_ENVELOPE_SCHEMA_VERSION
    assert env["screen"]
    assert env["verdict_text"]


_PRIOR_SESSION_CLOUD = {
    "cloud_measure": {
        "geometry": {"verdict": "ok"},
        "positions": [["mark", 1]],
        "pipeline": {"spec": {"bands": [{"name": "handoff", "max_deviation_db": 6.66,
                                         "tolerance_db": 3.0, "within_target": False}]}},
        "session_id": "cap_dead_session",
    },
}


    # Not the terminal screen's actions.


@pytest.mark.parametrize("phase,screen", [
    ("check", "microphone_check"),
    ("measure", "measure"),
    ("verify", "verify"),
    ("cloud_verify", "verify"),
])
def test_live_session_phase_screen_is_untouched(phase, screen):
    """No regression to the live path: a session the household is inside
    renders the screen it renders today, numbers and all."""
    env = build_crossover_envelope_v2(_status(
        phase=phase, session_id="cap_live", applied=phase.endswith("verify"),
        cloud=_PRIOR_SESSION_CLOUD, tier="full",
    ))
    assert env["screen"] == screen
    assert env["cloud"] == _PRIOR_SESSION_CLOUD
    assert env["nudges"] == []


def test_every_in_flow_action_the_envelope_mints_is_machine_actionable():
    """One flow, N drivers: a decision must be performable without a browser.

    The invariant, stated so it can be checked rather than intended: an action
    whose ``href`` points back INTO this flow is a decision, and a decision has
    to carry an ``endpoint`` a driver can POST. An action pointing at another
    subsystem (``/sound/``, ``/sound/speaker/``) is a navigation and is
    exempt — no endpoint here could perform it, and minting a fake one would be
    worse than the link.

    This is the shape #2641 was: ``review_decline``'s href was
    ``/sound/speaker/crossover/`` — in-flow — with no endpoint, so it looked like
    an exit and behaved like a reload. Every screen is swept rather than the
    one that was reported, because the next instance of this bug will be on a
    different screen.
    """
    offenders: list[tuple[str, str]] = []
    for name, env in _every_screen_envelope().items():
        actions = [env.get("next_action"), *(env.get("alternate_actions") or [])]
        for action in actions:
            if not isinstance(action, dict):
                continue
            href = str(action.get("href") or "")
            if not href.startswith("/sound/speaker/crossover"):
                continue
            if not action.get("endpoint"):
                offenders.append((name, str(action.get("id") or "?")))

    assert offenders == [], (
        "an in-flow action with no endpoint is a decision a driver cannot "
        "take, and a button a household clicks to no effect", offenders,
    )


