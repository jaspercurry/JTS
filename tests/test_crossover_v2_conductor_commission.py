# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: commission tiers, the retake/confirm contract, and the courtesy-tone prelude."""

from __future__ import annotations

import asyncio
import re
import types
import pytest
import yaml
from dataclasses import replace
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.programs import courtesy_prelude_for_phase
from jasper.active_speaker.crossover_v2_flow import (
    AUTO_ADVANCE_TAP,
    CAPTURE_ENTRY_MARGIN_MS,
    CAPTURE_PLAN_MAX_ATTEMPTS,
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    CLOUD_POSITION_PROMPTS,
    CLOUD_RETAKE_ALLOWANCE,
    CLOUD_WALK_SHAPE_TAIL,
    CLOUD_WALK_SHAPE_TAIL_POST_APPLY,
    DEFAULT_CLOUD_VERIFY_POSITIONS,
    GEOMETRY_RETRY_OFFSET_CM,
    GEOMETRY_RETRY_POSITIONS,
    MAX_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_OFFSET_CM,
    MIN_CLOUD_VERIFY_POSITIONS,
    POSITION_ROLE_ONAX,
    POSITION_ROLES,
    PILOT_LEVEL_DELTA_DB,
    REVERIFY_NO_REWALK_HEADLINE,
    TIER_EXPRESS,
    WIDE_OFFSET_MIN_CM,
    TIER_FULL,
    TIER_REMOTE,
    VERIFY_ANCHOR_HOLD_MESSAGE,
    CrossoverV2Session,
    CrossoverV2FlowError,
    V2PlanShape,
    _program_duration_ms,
    _min_positions_for_two_wide_offsets,
    _pose,
    build_v2_capture_plan,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
    build_v2_verify_capture_plan,
    build_v2_verify_session_spec,
    cloud_geometry_retry_reach_cm,
    cloud_walk_shape,
    express_cloud_measure_positions,
    format_position_distance,
    resolve_plan_shape,
    session_wall_clock_ceiling_s,
    tier_display_info,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    KIND_COURTESY_TONE,
    RoleBand,
)
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    _GOLDEN_V2_PLAN_BYTES,
    _conductor,
    _dummy_program,
    _preset,
    _roles,
    _run_phase,
)


# --- commission tiers + the retake/confirm contract (flow-simplification) ----


def test_express_is_a_derived_shape_not_a_loosened_floor():
    """§1.2: express is a distinct NAMED plan, validated on its own terms.

    Its N comes from the prompt table (both wide offsets, no more), its M is 1
    (no post-apply group at all), and the FULL tier's validated floor
    ``MIN_CLOUD_MEASURE_POSITIONS`` does not move to accommodate it — the same
    counts are still refused when asked for as a full-tier configuration.
    """
    express = resolve_plan_shape(TIER_EXPRESS)
    assert express == V2PlanShape(
        tier=TIER_EXPRESS,
        cloud_measure_positions=express_cloud_measure_positions(),
        cloud_verify_positions=1,
    )
    assert (express.capture_target, express.max_attempts) == (7, 14)
    assert express.has_cloud_verify_group is False
    # The full tier is unchanged, and would REFUSE express's own counts.
    full = resolve_plan_shape()
    assert full.tier == TIER_FULL
    assert (full.capture_target, full.max_attempts) == (16, 23)
    assert full.has_cloud_verify_group is True
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(
            TIER_FULL,
            cloud_measure_positions=express.cloud_measure_positions,
            cloud_verify_positions=1,
        )
    # Express is a fixed shape, so an explicit count that disagrees is refused
    # rather than quietly honoured.
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(TIER_EXPRESS, cloud_measure_positions=6)


def test_an_unknown_tier_is_refused_and_an_absent_one_means_full():
    """Allowlist, not a guess: absence is the non-breaking default, an
    unrecognised id is a caller asking for an instrument this build does not
    have and must fail loudly rather than measure something else."""
    assert resolve_plan_shape(None).tier == TIER_FULL
    assert resolve_plan_shape("").tier == TIER_FULL
    assert resolve_plan_shape("  EXPRESS  ").tier == TIER_EXPRESS
    for bogus in ("quick", "Full measurement", "expres", "0"):
        with pytest.raises(CrossoverV2FlowError):
            resolve_plan_shape(bogus)


def test_the_post_apply_pose_set_is_a_parameter_with_a_runbook_default():
    """(T1-5) The runbook is a SUGGESTION: the walk takes the set it is given.

    Two halves, and either alone would be a half-fix. The DEFAULT is the
    owner's ratified set — the design axis and the four sides — so a household
    that states nothing gets it. And a caller that states a set gets THAT one,
    down to the prompt copy, because "measure the result at these angles" was
    not a question anyone could ask while the walk re-sliced a fixed table.

    One resolver behind both, so the plan the phone is handed and the session
    that walks it cannot read different tables.
    """
    assert [
        flow.position_angle_deg(p) for p in flow.CLOUD_VERIFY_POSE_PROMPTS
    ] == [0, -7, 7, -22, 22]
    assert flow.verify_pose_table() is flow.CLOUD_VERIFY_POSE_PROMPTS
    assert flow.verify_pose_table(None) is flow.CLOUD_VERIFY_POSE_PROMPTS

    # A caller-supplied set: the same two at-mark-and-one-side poses, and
    # nothing else. Chosen from the shipped table so the assertion is about the
    # SEAM rather than about a hand-built prompt's copy.
    chosen = flow.CLOUD_VERIFY_POSE_PROMPTS[:2]
    assert flow.verify_pose_table(chosen) == chosen

    shape = replace(resolve_plan_shape(), cloud_verify_positions=len(chosen) + 1)
    plan = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=shape, verify_prompts=chosen,
    )
    assert [
        e.screen["title"] for e in plan.entries if e.kind_label == "cloud_verify"
    ] == [p.headline for p in chosen]
    # …and the shape and the set have to agree in BOTH directions.
    #
    # Short table: the walk would prompt fewer spots than the session believes.
    with pytest.raises(CrossoverV2FlowError, match="pose set"):
        build_v2_verify_capture_plan(
            FC_HZ,
            plan_shape=replace(
                resolve_plan_shape(), cloud_verify_positions=len(chosen) + 2,
            ),
            verify_prompts=chosen,
        )
    # LONG table: the quiet one. The poses past ``M - 1`` never reach an entry,
    # so the walk is silently truncated to a prefix — while the orientation
    # sentence is quoted off the WHOLE table and promises a reach the walk does
    # not have. Pinned with a 60 cm sixth pose against the shipped 40 cm walk:
    # unguarded, the plan builds and the consent screen says 70 cm.
    long_table = flow.CLOUD_VERIFY_POSE_PROMPTS + (
        next(p for p in CLOUD_POSITION_PROMPTS if p.offset_cm == 60.0),
    )
    assert flow.cloud_walk_reach_cm_of(long_table) > flow.cloud_walk_reach_cm_of(
        flow.CLOUD_VERIFY_POSE_PROMPTS
    ), "the extra pose must widen the quoted reach, or this pins nothing"
    with pytest.raises(CrossoverV2FlowError, match="pose set"):
        build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(), verify_prompts=long_table,
        )
    # EXPRESS is the shape a bare ``!=`` would break: M = 1 emits no
    # cloud-verify entry at all, so its empty index list must not be measured
    # against the 5-row default. It is correct by construction and builds.
    express = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert express.capture_target == 1
    assert [e.kind_label for e in express.entries] == ["verify"]


def test_the_stage_2_plan_walks_the_tiers_own_verify_shape():
    """Work order D2, owner-confirmed 2026-07-29 — and the re-derivation of
    ``test_an_express_plan_emits_no_cloud_verify_and_ends_on_verify``, whose
    subject (the ``M = 1`` done-screen placement rule) moved out of stage 1's
    builder and into stage 2's along with the post-apply group itself.

    Full's stage 2 is the multi-position spatial walk; Express's is the single
    anchor at the mark. The phone's END screen rides the LAST entry either way
    (``renderPlanAllDone`` reads the final wire index), and Express's copy
    claims LESS because it verified less (§1.3).
    """
    from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS

    full = build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    assert full.capture_target == DEFAULT_CLOUD_VERIFY_POSITIONS == 6
    assert [e.kind_label for e in full.entries] == (
        ["verify"] + ["cloud_verify"] * (DEFAULT_CLOUD_VERIFY_POSITIONS - 1)
    )
    assert [e.index for e in full.entries] == list(range(6))
    # The walk's prompted poses ARE the resolved pose set, in its own order —
    # the 2026-08-24 ruling's design-axis member first, then the four sides.
    assert [
        e.screen["title"] for e in full.entries if e.kind_label == "cloud_verify"
    ] == [p.headline for p in flow.CLOUD_VERIFY_POSE_PROMPTS]
    assert full.entries[-1].screen["done_title"] == "Your speaker is tuned"
    assert "Run a Full measurement" not in full.entries[-1].screen["done_body"]
    # Stage 1's own plan claims nothing about the result any more.
    assert all(
        "done_title" not in e.screen
        for e in build_v2_capture_plan(_roles(), FC_HZ).entries
    )

    express = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert express.capture_target == 1
    assert [e.kind_label for e in express.entries] == ["verify"]
    last = express.entries[-1]
    assert last.screen["done_title"] == "Your speaker is tuned"
    assert "Run a Full measurement" in last.screen["done_body"]
    # The B2-corrected phrase, not the withdrawn one. This line used to pin
    # `"verified-everywhere" in done_body` — an assertion actively holding the
    # overclaim that PR #1780's review had already ruled out on jts.local, so
    # the phone contradicted the wizard on one journey. Pin the shipped wording
    # instead, and pin the withdrawn one OUT so it cannot come back.
    assert (
        "the result checked at several spots around the mark"
        in last.screen["done_body"]
    )
    assert "verified-everywhere" not in last.screen["done_body"]

    # RE-DERIVED budgets. Stage 2 draws its own, from its own target:
    # Full 6 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE, Express 1 + …
    assert full.max_attempts == (
        6 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    assert express.max_attempts == (
        1 + GEOMETRY_RETRY_POSITIONS + CLOUD_RETAKE_ALLOWANCE
    ) <= MAX_CAPTURE_PLAN_ATTEMPTS
    # …and its own walked-away ceiling: 1800 + (6-3)*120 / the plain baseline.
    assert session_wall_clock_ceiling_s(full) == 2160.0
    assert session_wall_clock_ceiling_s(express) == 1800.0

def test_the_stage_2_done_screen_never_pre_commits_a_verdict_it_cannot_know():
    """#1964: every word of the phone's END screen is written when stage 2 is
    ARMED — before the first tone plays — so it may not assert an outcome the
    session has not measured.

    Full's copy read "Verified and applied.", selected only by
    ``plan_shape.has_cloud_verify_group``. The post-apply cloud's SPEC verdict
    is computed from the LAST capture and can FAIL while the tracking
    comparator passes; on such a session jts.local said "Your speaker is
    tuned, **but** the result still measures further from flat than the
    target…" while the phone in the household's hand said "Verified and
    applied." Two surfaces, one session, and the phone always optimistic.

    Two halves are pinned, because either alone is re-breakable:

    * **Structural** — this builder's entire input is a crossover frequency
      and a plan SHAPE. There is no measured outcome in scope to bind copy to,
      so a future "Verified" here would be as unearned as this one was.
    * **Cross-surface** — whatever the phone bakes has to hold under EVERY
      outcome jts.local can report. It does so by being exactly the claim each
      of jts.local's seven done verdicts OPENS with; jts.local owns the
      divergence, as the only surface whose component vocabulary can carry it.
      All seven are pinned, not the two this fix reasoned about: the phone
      bakes one headline for both tiers and all outcomes, so a single
      unpinned variant is enough to reopen the defect.
    """
    import inspect

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )
    from jasper.web.correction_crossover_v2 import _post_apply_grade

    # The whole input, enumerated: a crossover frequency (or, on a speaker with
    # none, its declared measurement band), a plan SHAPE, and the POSE SET the
    # walk takes. Not one of the four is a measured outcome, which is the
    # structural half of the claim above — a pose set says where the microphone
    # goes, and a declared band what the speaker can be swept over, never how
    # the result came out.
    assert set(inspect.signature(build_v2_verify_capture_plan).parameters) == {
        "fc_hz", "measurement_band_hz", "plan_shape", "verify_prompts",
    }

    done = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(),
    ).entries[-1].screen
    body = done["done_body"]
    # No verdict vocabulary: the instrument that grades flatness has not
    # reported when these bytes are written.
    assert "verified" not in body.lower()
    assert "spec" not in body.lower()
    # It names the surface that DOES own the verdict instead of guessing it.
    assert "speaker page" in body

    # ONE headline is baked for BOTH tiers…
    express_done = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
    ).entries[-1].screen
    headline = done["done_title"]
    assert express_done["done_title"] == headline

    def _verdict(**v2) -> str:
        # R19: the done screen reads the PRODUCER's grade for the spatial
        # verdict and the scope/completeness fact, so a fixture that describes
        # a session has to carry what that session's state would produce.
        # Deriving it here rather than hand-writing one keeps this a pin on
        # the real path — a fixture that stops reaching its branch shows up as
        # a collapsed variant below, which is exactly what this test counts.
        block = {
            "phase": "done", "verify": {"outcome": "pass"},
            "applied": True, **v2,
        }
        if "post_apply_grade" not in block:
            block = {**block, "post_apply_grade": _post_apply_grade(block)}
        return build_crossover_envelope_v2({
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "crossover_v2": block,
        })["verdict_text"]

    # …so the invariant holds only if EVERY jts.local done verdict opens with
    # it. There are seven, independently authored across the branches of the
    # PHASE_DONE arm, and pinning only the ones a given fix reasoned about
    # would leave the rest free to drift out from under the phone.
    variants = {
        "express": _verdict(tier=TIER_EXPRESS),
        "generic": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {"overall_within_target": True}},
        ),
        "spec_fail": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {"overall_within_target": False}},
        ),
        # R19/#2160: a group that closed and could not grade anything is a
        # third thing, and used to render as the miss above.
        "spec_unmeasurable": _verdict(
            tier=TIER_FULL,
            cloud={PHASE_CLOUD_VERIFY: {
                "overall_within_target": False, "flatness": {"evaluable": False},
            }},
        ),
        # R19/#2098: Full verified at the mark and never closed its group.
        "scope_incomplete": _verdict(tier=TIER_FULL),
        "grade_inconclusive": _verdict(
            tier=TIER_FULL,
            post_apply_grade={"graded": False, "state": "inconclusive"},
        ),
        "grade_never_finished": _verdict(
            tier=TIER_FULL, post_apply_grade={"graded": False, "state": ""},
        ),
    }
    assert len(set(variants.values())) == 7, (
        "seven DISTINCT verdicts, or a fixture stopped reaching its branch"
    )
    assert "further from flat than the target" in variants["spec_fail"]
    assert "could not read enough of the sound" in variants["spec_unmeasurable"]
    assert "unproven" in variants["scope_incomplete"]
    for name, text in variants.items():
        assert text.startswith(headline), (name, text)


def test_the_recovery_re_verify_plan_is_unchanged_by_the_split():
    """The 1-entry recovery re-arm is byte-identical to what it always was
    (work order D2: "the 1-entry form remains what it is today"), so a failed
    stage 2 still offers one cheap sweep and says so.
    """
    plan = build_v2_verify_capture_plan(FC_HZ)
    assert plan.capture_target == 1
    assert plan.max_attempts == CAPTURE_PLAN_MAX_ATTEMPTS
    (entry,) = plan.entries
    assert entry.kind_label == "verify"
    assert entry.screen["title"] == REVERIFY_NO_REWALK_HEADLINE
    assert entry.screen["body"] == (
        "Put the microphone back on the mark and hold it still."
    )
    assert entry.screen["auto_advance"] == AUTO_ADVANCE_TAP
    # It is a recovery, not the end of a journey: no done copy, no confirm tap.
    assert "done_title" not in entry.screen
    assert "confirm_title" not in entry.screen


def test_every_entry_carries_the_one_server_derived_counter():
    """§2.1: "Measurement N of T" is the ONLY counter, it is server-derived,
    and it counts the whole session — the per-group "Spot i of n" vocabulary
    is retired (it disagreed with the phone's own count on screen)."""
    for tier in (TIER_FULL, TIER_EXPRESS):
        plan = build_v2_capture_plan(_roles(), FC_HZ, tier=tier)
        target = plan.capture_target
        assert [entry.screen["progress"] for entry in plan.entries] == [
            f"Measurement {i} of {target}" for i in range(1, target + 1)
        ]
        for entry in plan.entries:
            assert "Spot " not in entry.screen.get("title", "")
            assert "hold still" not in entry.screen.get("title", "")


def test_the_verify_anchor_keeps_its_confirm_tap_on_stage_2s_own_begin():
    """§2.2's confirm-then-tone tap, RE-ANCHORED (work order D10).

    §2.2 established begin-first-then-confirm and is SHIPPED; what the split
    supersedes is only its ordering premise — that the confirm follows an
    in-session apply. There is no in-session apply any more, so the tap moves
    with the anchor to stage 2's own begin, keeping the same two strings the
    page renders and gates the arm on.

    §2.2's fallback-safety rule is re-derived rather than dropped.
    ``validate_capture_page`` still admits a phone carrying a cached
    pre-redesign bundle, which ignores ``confirm_title``/``confirm_body`` and
    renders ``title``/``body`` instead. Those two used to have to stay the
    apply-hold copy because that page would show them AS the hold heading;
    stage 2 has no hold, so they become the plain pre-arm instruction — which
    is exactly what that page needs them to be, and is true for it.
    """
    verify = build_v2_verify_capture_plan(
        FC_HZ, plan_shape=resolve_plan_shape(),
    ).entries[0]
    assert verify.kind_label == "verify"
    assert verify.screen["confirm_title"] == "Back on the mark, holding still?"
    assert verify.screen["confirm_body"] == (
        "Same spot, same height, pointed at the speaker."
    )
    # No apply to arm on, so no on_apply policy anywhere in either stage.
    assert verify.screen["auto_advance"] == AUTO_ADVANCE_TAP
    # An older cached page reads title/body — and reads something TRUE.
    assert "mark" in verify.screen["title"]
    assert verify.screen["body"]
    assert verify.screen["title"] != "Applying"
    assert verify.screen["body"] != VERIFY_ANCHOR_HOLD_MESSAGE
    # …and the hold copy itself is retained, not deleted (D10): the deferral
    # that carries it is unreachable in a shipped session but still the honest
    # answer for any conductor built without a prior apply.
    assert VERIFY_ANCHOR_HOLD_MESSAGE


def test_the_measure_sweep_fit_rides_the_snapshot():
    """#2923: a duration-fitted MEASURE program's realized length is banked on
    the snapshot, not held only in the live conductor's memory — the durable
    half of #2921's fit, so an offline reader can replay it later.

    A woofer limit below the nominal 4.0 s default forces #2921's fit
    deterministically (the nominal always realizes AT OR ABOVE its own
    request — see ``phase_closing_duration_s``), independent of which band a
    fixture's roles happen to declare.
    """
    import json

    from jasper.active_speaker.crossover_v2 import priors as _priors_mod

    fakes = FakeSeams()
    c = _conductor(
        fakes,
        driver_sweep_duration_limits_s={"woofer": 3.5, "tweeter": 10.0},
    )
    _run_phase(c, 1, 1)  # CHECK solve -> MEASURE composed at the fitted length

    expected = _priors_mod.measure_sweep_durations_s(
        c.program_for_phase(PHASE_MEASURE)
    )
    assert expected is not None
    # The fit actually bit: realized at or below the limit, not the nominal.
    assert expected["woofer"] <= 3.5

    snap = c.snapshot()
    assert snap.measure_sweep_durations_s == pytest.approx(expected)
    assert snap.to_dict()["measure_sweep_durations_s"] == pytest.approx(expected)

    # Round-trips through the exact JSON encoding ``save_v2_state`` uses, so
    # no float precision is lost across the real persistence path — the same
    # encoding ``jasper-round-views distortion --state`` later reads back.
    roundtripped = json.loads(json.dumps(snap.to_dict()))["measure_sweep_durations_s"]
    assert roundtripped == pytest.approx(expected)

    # Before MEASURE is composed (no CHECK accept yet), the field is honestly
    # absent rather than a guessed nominal — mirrors ``gain_plan_db`` beside it.
    undeclared = _conductor(FakeSeams())
    assert undeclared.snapshot().measure_sweep_durations_s is None


def test_the_measure_sweep_fit_survives_conductor_to_rebuild_end_to_end():
    """#2923 gate fix round, nit 2: nothing previously joined this seam
    end to end.

    ``priors.measure_sweep_durations_s`` keys its returned dict by
    ``str(segment.role)`` — whatever the composed program's own roles are
    called. ``harmonic_evidence._banked_sweep_durations_s`` reads it back
    through a hardcoded ``("woofer", "tweeter")``. In this session's own
    2-way convention the two always agree, but nothing walked the WHOLE
    chain — conductor compose -> ``.snapshot()`` -> a durable-state-shaped
    dict -> the offline rebuild — to prove it; a future key-shape change on
    either half should fail here, not on a campaign.

    Caps are widened past the fixture default so the solved gain plan
    clears both ceilings with margin (``back_off_gain`` is then the
    identity for both roles, byte for byte) — the ordinary, non-clipped
    case this reproduction path is meant to serve. This is deliberately
    narrower than a full production-shaped ``candidate`` block:
    ``rebuild_measure_program`` reads only ``candidate.program_id``, so
    that is the only key supplied for it.
    """
    import json

    from jasper.active_speaker.crossover_v2 import harmonic_evidence as he

    fakes = FakeSeams()
    # Constructed directly rather than through ``_conductor()``: that helper
    # hardcodes ``driver_caps_dbfs=CAPS``, which collides with overriding it
    # here. Skipping ``_conductor()``'s entry-baseline stash is safe: that
    # stash is for stage-1 cloud grading this test never reaches, and CHECK's
    # assessor (``capture_dispatch.assess``) does not read it.
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs={"woofer": 0.0, "tweeter": 0.0},
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
        driver_sweep_duration_limits_s={"woofer": 3.5, "tweeter": 10.0},
    )
    _run_phase(c, 1, 1)  # CHECK solve -> MEASURE composed, woofer sweep fitted

    program = c.program_for_phase(PHASE_MEASURE)
    durable = json.loads(json.dumps(c.snapshot().to_dict()))
    state = {
        "gain_plan_db": durable["gain_plan_db"],
        "measure_sweep_durations_s": durable["measure_sweep_durations_s"],
        "candidate": {"program_id": program.program_id},
    }
    bands = {"woofer": (150.0, 6000.0), "tweeter": (300.0, 20000.0)}

    rebuilt, _downstream_db, _prelude = he.rebuild_measure_program(state, bands)

    assert rebuilt.program_id == program.program_id


def test_the_reverify_plan_leads_with_the_no_re_walk_sentence():
    """§2.4: the 2026-07-27 session ABANDONED this recovery because no screen
    said it is one sweep rather than another walk. Both of its surfaces — the
    consent steps and the entry instruction — now lead with the same
    sentence, from one constant so they cannot drift."""
    plan = build_v2_verify_capture_plan(FC_HZ)
    assert plan.capture_target == 1
    assert plan.entries[0].screen["title"] == REVERIFY_NO_REWALK_HEADLINE
    assert "do NOT need to redo the walk" in REVERIFY_NO_REWALK_HEADLINE

    spec = build_v2_verify_session_spec(FC_HZ, acknowledgement_binding="b" * 24)
    steps = next(c for c in spec.screen if c["type"] == "steps")["items"]
    assert steps[0] == REVERIFY_NO_REWALK_HEADLINE


def test_the_summed_consent_heading_names_the_job_not_crossover_crossover():
    """§2.3: the v2 cloud passed ``driver_label="crossover"`` into a heading
    template built for per-driver captures, so the household read
    "Crossover — crossover". A summed capture measures the speaker, not a
    named driver."""
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    heading = next(c for c in spec.screen if c["type"] == "heading")
    assert heading["text"] == "Tune your speaker"


def test_tier_display_info_minutes_hold_across_plausible_topologies():
    """S3 fix (adversarial review of PR #1780): ``tier_display_info``'s fixed
    representative ``RoleBand`` pair does NOT make the realized sweep
    duration invariant to the band (an earlier docstring overclaimed that —
    MESM gaps and Novak sample-count rounding both depend on the swept
    band's edges). What actually holds is narrower: the displayed WHOLE
    MINUTES stay the same across the plausible 2-way band space, because
    ``CapturePlan.estimated_minutes``'s ceil-to-minute quantum absorbs the
    real (small) variance. Swept here across several genuinely different
    plausible topologies — varying woofer/tweeter bands and ``fc_hz`` — each
    built through the REAL ``build_v2_capture_plan``, never re-deriving the
    arithmetic."""
    info = tier_display_info()
    topologies = [
        # (woofer band, tweeter band, fc_hz)
        (FrequencyBand(150.0, 6000.0), FrequencyBand(1800.0, 20000.0), 1600.0),
        (FrequencyBand(80.0, 3000.0), FrequencyBand(1200.0, 20000.0), 1800.0),
        (FrequencyBand(200.0, 4500.0), FrequencyBand(1500.0, 22000.0), 2200.0),
    ]
    for woofer_band, tweeter_band, fc_hz in topologies:
        roles = [
            RoleBand("woofer", 0, woofer_band),
            RoleBand("tweeter", 1, tweeter_band),
        ]
        for tier in (TIER_FULL, TIER_EXPRESS):
            shape = resolve_plan_shape(tier)
            # BOTH stages, because the chooser quotes the whole journey (D2).
            stage1 = build_v2_capture_plan(
                roles, fc_hz, plan_shape=shape,
                include_lateral=False,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            )
            stage2 = build_v2_verify_capture_plan(fc_hz, plan_shape=shape)
            minutes = stage1.estimated_minutes() + stage2.estimated_minutes()
            assert minutes == info[tier]["estimated_minutes"], (
                f"tier={tier} woofer={woofer_band} tweeter={tweeter_band} "
                f"fc={fc_hz}: displayed minutes drifted from tier_display_info()"
            )
            assert (
                stage1.capture_target + stage2.capture_target
                == info[tier]["capture_target"]
            )


def test_the_post_apply_walk_states_its_shape_with_its_own_tail():
    """Stage 2's walk gets the same one-line shape as stage 1's, with its own
    tail: the journey ends there rather than pausing for a decision. Express's
    1-entry stage 2 is not a walk and gets no shape line at all.

    RE-DERIVED for the 2026-08-24 geometry ruling: the post-apply group walks
    its OWN pose set now, so the sentence is quoted off that table rather than
    off a prefix of the pre-apply one.
    """
    full = build_v2_verify_session_spec(
        FC_HZ, acknowledgement_binding="b" * 24, plan_shape=resolve_plan_shape(),
    )
    shape = cloud_walk_shape(flow.CLOUD_VERIFY_POSE_PROMPTS, post_apply=True)
    assert len([c for c in full.screen if c["type"] == "steps"]) == 1
    assert shape in [c["text"] for c in full.screen if c["type"] == "note"]
    # Same derived ceiling and the same retake honesty as stage 1 — the
    # geometry-locked retake is armed on this group too.
    reach = flow.cloud_walk_reach_cm_of(flow.CLOUD_VERIFY_POSE_PROMPTS)
    assert format_position_distance(reach) in shape
    assert cloud_geometry_retry_reach_cm() > reach
    assert "a redo can ask for one step further out" in shape
    assert shape.endswith(CLOUD_WALK_SHAPE_TAIL_POST_APPLY)
    # Stage 2 grades rather than handing back a decision.
    assert CLOUD_WALK_SHAPE_TAIL_POST_APPLY != CLOUD_WALK_SHAPE_TAIL

    express = build_v2_verify_session_spec(
        FC_HZ,
        acknowledgement_binding="b" * 24,
        plan_shape=resolve_plan_shape(TIER_EXPRESS),
    )
    assert len([c for c in express.screen if c["type"] == "steps"]) == 1
    assert cloud_walk_shape(()) == ""
    assert cloud_walk_shape((), post_apply=True) == ""
    # …and an empty shape renders NO note rather than an empty one, so the
    # one-sweep screen never grows a blank section.
    assert all(
        c["text"] for c in express.screen if c["type"] == "note"
    ), "an empty shape must render no note at all"


def test_check_stops_hushing_the_room_before_it_measures_it():
    """Work order D8 / issue #1835. CHECK's ambient window is the SESSION's
    room-noise measurement and is deliberately composed to run BEFORE anyone is
    asked to go quiet — the gain solve reads it, so a pre-hushed room reads
    quieter than reality and the solve under-drives against the noise the later
    sweeps actually face.

    TWO windows are touched and a THIRD is deliberately not: CHECK's step copy
    and the phone's own pre-arm floor note both stop asking for quiet on CHECK
    only. The in-sweep ambient lines — a different measurement with a different
    purpose — are the speaker's own call (``quiet_requested``) and this must not
    collapse them into one string.
    """
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    entries = {e.kind_label: e for e in spec.capture_plan.entries}
    check = entries["check"].screen
    assert "stay quiet" not in check["body"].lower()
    assert "carry on" in check["body"].lower()
    # …and the phone's own sub-second floor window gets its own honest request,
    # because asking for quiet THERE hushes the room a moment before CHECK
    # measures it.
    assert "quiet" not in check["noise_note"].lower()
    assert "carry on" in check["noise_note"].lower()
    # Every OTHER entry supplies no override, so the page keeps its default —
    # which is right for them, since a sweep follows immediately.
    for label, entry in entries.items():
        if label != "check":
            assert "noise_note" not in entry.screen


def test_cloud_prompts_front_load_the_wide_offsets():
    """Fundamental 1's physics, pinned: >=10 cm spread decorrelates HF nulls and
    ~30 cm+ offsets are what support the LF edge. Each group walks its own
    ordered table from the front, so the shortest walk either can be
    CONFIGURED to run — its declared MIN, not its default — must still contain
    at least two wide moves. Reordering a table for readability would
    silently delete the LF half of the measurement — hence this test rather
    than a comment.

    RE-DERIVED 2026-08-24: the two groups walked ONE table until the geometry
    ruling gave the post-apply group its own pose set, so the floors are now
    derived per table rather than one standing in for both. The guarantee is
    unchanged; what moved is which table each floor is checked against.

    Round-2 review NEW-9: this used to compare against
    ``DEFAULT_CLOUD_VERIFY_POSITIONS``, so ``M = 2`` was accepted and voided
    the guarantee the test claims. Both groups now carry a floor, and both
    floors are checked against the SAME derivation the code enforces.

    Flow-simplification §1.2 adds a THIRD number to the same derivation: the
    express tier's pre-apply group size. Express exists precisely because a
    4-position walk still picks up both wide moves for free, so a reorder that
    pushed the second wide move later must move express with it rather than
    ship a silently one-wide "quick tune".
    """
    walked = CLOUD_POSITION_PROMPTS[: MIN_CLOUD_MEASURE_POSITIONS - 1]
    assert sum(1 for prompt in walked if prompt.wide) >= 2
    # …and the same property on the POST-apply group, which walks its own pose
    # set since the 2026-08-24 geometry ruling rather than a prefix of the one
    # above. Two tables, so two derivations — a single ``min()`` over the two
    # floors would now be checking one table against the other's number.
    post_walked = flow.CLOUD_VERIFY_POSE_PROMPTS[: MIN_CLOUD_VERIFY_POSITIONS - 1]
    assert sum(1 for prompt in post_walked if prompt.wide) >= 2
    # The floors are DERIVED from the table each group walks, so a reorder moves
    # them rather than leaving a stale literal behind.
    derived = _min_positions_for_two_wide_offsets()
    assert MIN_CLOUD_VERIFY_POSITIONS == _min_positions_for_two_wide_offsets(
        flow.CLOUD_VERIFY_POSE_PROMPTS
    )
    assert MIN_CLOUD_MEASURE_POSITIONS >= derived
    assert express_cloud_measure_positions() == derived
    # …and the express plan really does walk two wide moves at that size.
    express = resolve_plan_shape(TIER_EXPRESS)
    express_walk = CLOUD_POSITION_PROMPTS[: express.cloud_measure_positions - 1]
    assert sum(1 for prompt in express_walk if prompt.wide) == 2
    assert len(express_walk) == 4


@pytest.mark.parametrize("positions", [MIN_CLOUD_VERIFY_POSITIONS - 1, 0])
def test_a_verify_group_too_short_for_two_wide_offsets_is_refused(positions):
    """The hole NEW-9 named: nothing stopped a caller asking for a post-apply
    group that never reaches a ~30 cm-class offset."""
    with pytest.raises(CrossoverV2FlowError):
        build_v2_capture_plan(_roles(), FC_HZ, cloud_verify_positions=positions)


def test_cloud_prompts_state_numeric_absolute_poses():
    """Every prompt is real household copy, states its distance NUMERICALLY in
    both units, and states a COMPLETE pose measured from the mark.

    RE-DERIVED, not merely relaxed. The pin this replaces asserted the opposite
    (`" cm" not in prompt.text`) under a comment citing "the S0 owner ruling:
    hand-widths and forearms, never centimetres" — the 2026-07-25 studio
    ruling. Two later owner rulings superseded it, and the assertion is now
    what THEY require rather than what the old one banned:

    * 2026-07-28 field session, issue #1805 — "drop body-part units — prompts
      should use inches and/or meters". So numeric units must be PRESENT and
      body-part units ABSENT; deleting the old assertion would have left the
      new rule unpinned, and leaving it would have made the suite assert a rule
      the owner has withdrawn.
    * 2026-07-29 field session, issue #1806 — poses must be absolute, never a
      delta on ambiguous prior state, and the actor is "the microphone" rather
      than the phone (a household may measure with a laptop or a USB mic).
    """
    for prompt in CLOUD_POSITION_PROMPTS:
        assert prompt.headline.strip()
        text = prompt.text
        lowered = text.lower()
        # #1805: numbers, in both units, on every prompted move.
        assert " in (" in text and " cm)" in text, text
        assert re.search(r"\d+ in \(\d+ cm\)", text), text
        # …and no body-part unit anywhere in the copy.
        for banned in ("hand-width", "hand width", "forearm", "arm's length"):
            assert banned not in lowered, text
        # #1806: an absolute pose names the mark it is measured from, and the
        # microphone rather than the phone.
        assert "mark" in lowered, text
        assert "microphone" in lowered, text
        assert "phone" not in lowered.replace("microphone", ""), text
        # …and carries a role the attribution stage can read.
        assert prompt.role in POSITION_ROLES


def test_geometry_retry_prompts_carry_the_same_register():
    """The RETAKE rungs are the other prompt constant carrying the register —
    the work order names both, because a table converted alone would leave the
    household reading inches all session and then "two forearms' length" at the
    one moment the instruction has to be unambiguous."""
    for rung in CLOUD_GEOMETRY_RETRY_PROMPTS:
        lowered = rung.lower()
        assert re.search(r"\d+ in \(\d+ cm\)", rung), rung
        assert "forearm" not in lowered and "hand-width" not in lowered, rung
        assert "microphone" in lowered, rung
        assert "mark" in lowered, rung
    # A rung must ask for a spread the walk itself never reaches, or "wider
    # spot" is a request the household has already satisfied.
    assert GEOMETRY_RETRY_OFFSET_CM > max(
        p.offset_cm for p in CLOUD_POSITION_PROMPTS[:MIN_CLOUD_MEASURE_POSITIONS - 1]
    )


def test_wide_is_derived_from_the_offset_not_hand_set():
    """The wide-offset guarantee survives a copy edit because ``wide`` is
    COMPUTED from the row's distance.

    Before the distances became data, a row could say "a forearm's length" and
    carry ``wide=True`` independently — two facts that could disagree, on the
    one flag ``MIN_CLOUD_VERIFY_POSITIONS`` and ``express_cloud_measure_
    positions()`` are both derived from. Now narrowing the copy narrows the
    flag, which moves the floors, which fails
    ``test_cloud_prompts_front_load_the_wide_offsets`` loudly.
    """
    for prompt in CLOUD_POSITION_PROMPTS:
        assert prompt.wide == (prompt.offset_cm >= WIDE_OFFSET_MIN_CM)
        assert prompt.offset_cm >= MIN_CLOUD_OFFSET_CM
        # The stated distance IS the carried distance — the copy is generated
        # from the number, so these cannot drift.
        assert format_position_distance(prompt.offset_cm) in prompt.headline
    narrowed = replace(CLOUD_POSITION_PROMPTS[2], offset_cm=WIDE_OFFSET_MIN_CM - 1)
    assert narrowed.wide is False
    # …and the HF floor is ENFORCED at table-build time, not documented: a row
    # too short to decorrelate anything is a session minute spent on nothing.
    with pytest.raises(ValueError):
        _pose("Move it {d}", MIN_CLOUD_OFFSET_CM - 1, POSITION_ROLE_ONAX)
    with pytest.raises(ValueError):
        _pose("Move it {d}", 40.0, "sideways")


# --- courtesy-tone prelude (issue #1677): phone-contract duration ------------
#
# The phone's recording window (CapturePlanEntry.duration_ms) is derived from
# build_v2_capture_plan's OWN nominal composition, entirely separate from the
# real playback composition (``crossover_v2.programs``'s SessionExcitation
# methods, reached through the conductor's ``_excitation``). Both must ask the
# SAME ``courtesy_prelude_for_phase`` rule, or the phone would stop recording
# before the real (longer) program finishes -- mirrors the existing +15 s
# MEASURE-lengthening proof from sweep-composition PR-A (#1668).
#
# Since the 2026-08-18 trim the rule answers per PHASE, so this is now also
# where a phase that is announced in the plan but not in playback (or the other
# way round) is caught: each entry is checked against a nominal program composed
# at ITS OWN phase's answer.


def _courtesy_prelude_ms() -> float:
    """What one prelude costs, DERIVED from the composer's own constants."""
    from jasper.audio_measurement.program import (
        COURTESY_TONE_BEEP_COUNT,
        COURTESY_TONE_BEEP_DURATION_S,
        COURTESY_TONE_BEEP_GAP_S,
        COURTESY_TONE_TRAILING_SILENCE_S,
    )

    return 1000.0 * (
        COURTESY_TONE_BEEP_COUNT * COURTESY_TONE_BEEP_DURATION_S
        + (COURTESY_TONE_BEEP_COUNT - 1) * COURTESY_TONE_BEEP_GAP_S
        + COURTESY_TONE_TRAILING_SILENCE_S
    )


def test_capture_plan_duration_matches_courtesy_prelude_program_exactly():
    assert courtesy_prelude_for_phase(PHASE_CHECK) is True
    assert courtesy_prelude_for_phase(PHASE_MEASURE) is False
    plan = build_v2_capture_plan(_roles(), FC_HZ)
    check, measure = plan.entries[0], plan.entries[1]
    # The VERIFY-shaped program's duration now rides STAGE 2's anchor (the
    # split moved the phase, not the arithmetic) — and the cloud entries, which
    # play its unannounced twin, are checked against that twin below.
    stage2 = build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    verify = stage2.entries[0]
    assert verify.kind_label == "verify"

    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_check_program,
        build_measure_program,
        build_verify_program,
    )

    roles = _roles()
    nominal_gains = {rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}
    nominal_check = build_check_program(
        roles, courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
    )
    nominal_measure = build_measure_program(
        nominal_gains, roles,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_MEASURE),
    )
    nominal_verify = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
    )
    nominal_cloud = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
    )
    assert check.duration_ms == _program_duration_ms(nominal_check) + CAPTURE_ENTRY_MARGIN_MS
    assert measure.duration_ms == _program_duration_ms(nominal_measure) + CAPTURE_ENTRY_MARGIN_MS
    assert verify.duration_ms == _program_duration_ms(nominal_verify) + CAPTURE_ENTRY_MARGIN_MS
    # Every prompted position plays the summed sweep's UNANNOUNCED twin, so its
    # recording window must be that program's — a shorter one would truncate
    # the sweep and a longer one would record silence into the analysis.
    cloud_ms = _program_duration_ms(nominal_cloud) + CAPTURE_ENTRY_MARGIN_MS
    cloud_entries = [
        e for e in (*plan.entries, *stage2.entries)
        if e.kind_label.startswith("cloud_")
    ]
    assert cloud_entries
    for entry in cloud_entries:
        assert entry.duration_ms == cloud_ms, entry.kind_label
    # And the trim is real at the phone's own surface: a position's window is
    # exactly the prelude shorter than the anchor's.
    assert verify.duration_ms - cloud_ms == pytest.approx(_courtesy_prelude_ms(), abs=1)
    # The SHIPPED stage-1 plan, whose last entry is the one budget that has to
    # match a program composed for a DIFFERENT phase: the entry baseline plays
    # stage 2's anchor object, so it budgets the ANNOUNCED window even though
    # nothing about its own position asks for a warning.
    shipped = build_v2_capture_plan(
        _roles(), FC_HZ,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    baseline = next(e for e in shipped.entries if e.kind_label == "entry_baseline")
    assert baseline.duration_ms == verify.duration_ms
    # A lateral pose replays MEASURE, so it budgets MEASURE's window.
    for entry in shipped.entries:
        if entry.kind_label == "lateral":
            assert entry.duration_ms == measure.duration_ms


def test_capture_plan_duration_is_longer_than_the_pre_1677_shape():
    """Direct proof the prelude actually lengthens the phone's recording
    budget (not just that the two composition paths agree with EACH OTHER,
    which the previous test already pins) -- the "+15 s"-style regression
    check named in the issue."""
    from jasper.audio_measurement.program import build_check_program

    expected_prelude_ms = _courtesy_prelude_ms()
    roles = _roles()
    legacy_check = build_check_program(roles)
    prelude_check = build_check_program(roles, courtesy_prelude=True)
    delta_ms = _program_duration_ms(prelude_check) - _program_duration_ms(legacy_check)
    assert delta_ms == pytest.approx(expected_prelude_ms, abs=1)

    plan = build_v2_capture_plan(roles, FC_HZ)
    check_entry = plan.entries[0]
    legacy_entry_duration_ms = _program_duration_ms(legacy_check) + CAPTURE_ENTRY_MARGIN_MS
    assert check_entry.duration_ms > legacy_entry_duration_ms
    assert check_entry.duration_ms - legacy_entry_duration_ms == pytest.approx(
        expected_prelude_ms, abs=1,
    )


def test_verify_only_capture_plan_duration_includes_courtesy_prelude():
    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        build_verify_program,
    )

    plan = build_v2_verify_capture_plan(FC_HZ)
    entry = plan.entries[0]
    nominal_verify = build_verify_program(
        FC_HZ,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB, BASE_STIMULUS_PEAK_DBFS
        ),
        courtesy_prelude=True,
    )
    assert entry.duration_ms == _program_duration_ms(nominal_verify) + CAPTURE_ENTRY_MARGIN_MS


def test_conductor_composed_programs_carry_the_prelude_where_the_rule_says():
    """The conductor's REAL playback composition (not the nominal planning path
    above) obeys the same ``courtesy_prelude_for_phase`` rule — including the
    clip-retry rearm, which recomposes MEASURE and must not put the beeps back.
    """
    fakes = FakeSeams()
    c = _conductor(fakes)
    check_tone_ids = {
        s.segment_id for s in c.program_for_phase(PHASE_CHECK).segments if s.kind == KIND_COURTESY_TONE
    }
    assert check_tone_ids == {"courtesy_tone_ch0", "courtesy_tone_ch1"}

    measure_prog = c._compose_measure_program({"woofer": -11.0, "tweeter": -13.0})
    assert not [s for s in measure_prog.segments if s.kind == KIND_COURTESY_TONE]

    verify_tone_ids = {
        s.segment_id for s in c.program_for_phase(PHASE_VERIFY).segments if s.kind == KIND_COURTESY_TONE
    }
    assert verify_tone_ids == {"courtesy_tone_ch0"}  # VERIFY is mono
    assert verify_tone_ids == {
        s.segment_id
        for s in c.program_for_phase(PHASE_ENTRY_BASELINE).segments
        if s.kind == KIND_COURTESY_TONE
    }
    assert not [
        s for s in c.program_for_phase(PHASE_CLOUD_VERIFY).segments
        if s.kind == KIND_COURTESY_TONE
    ]


@pytest.mark.parametrize("lateral_armed", [False, True])
@pytest.mark.parametrize("tier", [TIER_FULL, TIER_EXPRESS, TIER_REMOTE])
def test_the_consent_beeps_sentence_matches_what_the_session_plays(
    tier, lateral_armed,
):
    """The consent screen's beeps sentence, checked against the PROGRAMS.

    The 2026-08-18 gate round found a hand-written "The first measurement has
    three short beeps" shipped against a stage 1 that beeps TWICE — its entry
    baseline plays stage 2's anchor object and announces too. A prior literal
    pin could not see it: a substring assertion is true of a sentence that is
    false of the session.

    So this walks the other way round. For each capture index it asks the
    SESSION what that phase plays and looks for a courtesy tone in the composed
    segments — the ground truth, what the speaker actually does — and then
    requires the rendered sentence to be the one that describes that set. A
    rule change that moves the announced set without moving the copy (or the
    reverse) fails here whichever way it drifts.

    **Both lateral states, and the ARMED one is the case that binds.** No
    stage-1 plan builds the lateral group any more, so the shipped stage 1 is
    three captures at the mark: not a guided walk, and it renders no beeps
    sentence at all — which would leave the two-announcement shape unexercised
    and this pin quietly vacuous. ``lateral_armed=True`` is driven straight
    into the builders below rather than through a flag, because that is
    exactly the shape an operator's staged angle walk produces for THIS
    session (``prepare_v2_session`` sets the same local ``True`` once a walk
    is taken) — and the sentence it renders is the one that was WRONG when the
    gate found it.
    """
    from jasper.audio_measurement.program import KIND_COURTESY_TONE
    from jasper.active_speaker.capture_geometry import (
        CLOUD_WALK_PLACEMENT_POLICY_ID,
    )

    shape = resolve_plan_shape(tier)
    stages = (
        (
            build_v2_session_spec(
                _roles(), FC_HZ, acknowledgement_binding="b" * 24,
                plan_shape=shape,
                include_lateral=lateral_armed,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            ),
            build_v2_cloud_index_phase_map(
                plan_shape=shape,
                include_lateral=lateral_armed,
                include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
            ),
        ),
        (
            build_v2_verify_session_spec(
                FC_HZ, acknowledgement_binding="b" * 24, plan_shape=shape,
            ),
            flow.build_v2_verify_index_phase_map(plan_shape=shape),
        ),
    )
    conductor = _conductor(FakeSeams(), gain_plan_db={"woofer": -30.0, "tweeter": -36.0})

    for spec, index_phase in stages:
        walk = len(index_phase)
        played = tuple(
            index for index, phase in sorted(index_phase.items())
            if any(
                seg.kind == KIND_COURTESY_TONE
                for seg in conductor.program_for_phase(phase).segments
            )
        )
        steps = next(c for c in spec.screen if c["type"] == "steps")["items"]
        sentence = next((i for i in steps if "beeps" in i), "")
        if not sentence:
            # The GUIDED consent surface was not rendered, so there is no
            # sentence to be wrong — and that is checked rather than assumed,
            # because "no sentence" must never be how a guided session passes.
            # Two shipped shapes land here: a single held-still sweep (Express's
            # stage 2, the recovery re-arm) and, since the 2026-08-18 lateral
            # pause, stage 1 itself — three captures all at the mark, so the
            # flow's ``walked`` is False and the stationary copy applies.
            #
            # NOTE the consequence, which is not this PR's to fix: stage 1
            # announces two of its three captures and its consent screen says
            # nothing about beeps, because the stationary copy never carried
            # that sentence. Re-arming the walk restores it.
            assert (
                spec.acknowledgement.id != CLOUD_WALK_PLACEMENT_POLICY_ID
            ), (tier, walk)
            continue
        assert played, (tier, walk)
        if played == tuple(range(1, walk + 1)):
            expected = "Each measurement has"
        elif played == (1,):
            expected = "The first measurement has"
        elif played == (1, walk):
            expected = "The first and last measurements each have"
        else:  # pragma: no cover - a shape the copy refuses to state
            raise AssertionError(f"unstateable announced set {played} of {walk}")
        assert sentence.startswith(expected), (tier, walk, played, sentence)
        # …and the OTHER two openers are pinned out, so a sentence that merely
        # contains the right words in the wrong quantifier cannot pass.
        for other in (
            "Each measurement has",
            "The first measurement has",
            "The first and last measurements each have",
        ):
            if other != expected:
                assert other not in sentence, (tier, other)


def test_a_consent_walk_must_say_which_captures_announce():
    """A guided walk with no announced set is REFUSED, not silently phrased.

    The fail-loud half of the pin above. ``build_crossover_sweep_spec`` is a
    public builder and a caller that declares a walk without saying what it
    announces has no truthful sentence available — rendering "The first
    measurement has…" by default is exactly how the shipped defect happened.
    """
    from jasper.active_speaker.crossover_v2.sweep_spec import (
        CaptureSpecError,
        build_crossover_sweep_spec,
    )

    def _spec(announced):
        return build_crossover_sweep_spec(
            driver_label="crossover",
            driver_role="summed",
            acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
            guided_captures=9,
            announced_captures=announced,
        )

    for announced in ((), (0, 3), (1, 99), (2,), (1, 4)):
        with pytest.raises(CaptureSpecError):
            _spec(announced)

    # …and the third stateable shape, which has no shipped producer since the
    # prelude trim but is the truthful sentence for a plan that announces
    # everything — the pre-trim rule's own shape, and what a re-enable would
    # render. Kept because refusing to describe a describable session is the
    # worse failure, and pinned here so it is exercised rather than assumed.
    steps = next(
        c for c in _spec(tuple(range(1, 10))).screen if c["type"] == "steps"
    )["items"]
    assert any(
        i.startswith("Each measurement has three short beeps") for i in steps
    )


def test_bind_program_playback_seams_is_the_play_transaction_and_confirms_strictly(
    tmp_path,
):
    """What the binding still owns after wave 6b, and what it hands off.

    The graph seams moved to ``MeasurementSessionGraph``; the SetConfig
    transport claim they carried — load and restore ride
    ``set_active_config_raw``, never ``set_config_file_path``, so the statefile
    boot anchor stays put and a crash mid-session reboots onto the staged
    anchor — moved with them and is pinned in
    ``tests/test_crossover_v2_session_graph.py``. ``confirm_graph_is_live``
    moved with the binding to ``crossover_v2.composition``; its strictness is
    still pinned here.
    """
    from jasper.active_speaker.crossover_v2 import composition
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
    )
    from jasper.camilla import CamillaConfigRejected

    calls: list = []

    class _FakeCam:
        """Models the 2026-08-05 hardware probe of CamillaDSP 4.1.3.

        ``GetConfig`` returns a default-filled, value-normalized SUPERSET of
        what was submitted (extra null keys; a submitted ``0`` back as ``0.0``),
        and ``ReadConfig`` — ``normalize_config_raw`` — applies exactly the same
        transform without applying anything. Comparing submitted TEXT against
        the readback would refuse every load on this fake, which is the point.
        """

        live = "prior: graph\n"

        @staticmethod
        def _camilla_serde(text):
            parsed = yaml.safe_load(text) or {}
            filled = {"description": None, "bypassed": None, **parsed}
            return yaml.safe_dump(
                {k: (0.0 if v == 0 else v) for k, v in filled.items()}
            )

        async def get_config_file_path(self, *, best_effort):
            calls.append(("get_path", best_effort))
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(self, text, *, best_effort, duck=True):
            calls.append(("set_raw", text, best_effort))
            self.live = text
            return True

        async def get_active_config_raw(self, *, best_effort):
            calls.append(("get_raw", best_effort))
            return self._camilla_serde(self.live)

        async def normalize_config_raw(self, text, *, best_effort):
            # What a live, healthy CamillaDSP raises for a config it parsed and
            # refused — CamillaController._call already maps pycamilladsp's
            # ConfigValidationError onto this class.
            if "!!not-yaml" in text:
                raise CamillaConfigRejected("camilla rejected the config")
            return self._camilla_serde(text)

        async def set_config_file_path(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("must never repoint the persisted statefile")

    entry = tmp_path / "entry.yml"
    entry.write_text("prior: graph\n", encoding="utf-8")
    cam = _FakeCam()
    seams = bind_program_playback_seams(
        cam,
        bundle_dir=str(tmp_path),
        artifact=object(),
        config_dir=str(tmp_path),
        program=_dummy_program(),
        wav_path=str(tmp_path / "program.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=SESSION_VOLUME_DB,
        graph_yaml="program: graph\n",
    )
    # The count IS the claim, and wave 6b shrank it: the three graph seams
    # moved to ``MeasurementSessionGraph``, which installs one graph per session
    # instead of swapping one in and out per stimulus. What is left here is the
    # play transaction proper.
    assert set(seams) == {"play_wav", "readmit", "writer_lock"}

    from jasper.active_speaker.program_playback import ProgramPlaybackError

    # ``confirm_graph_is_live`` moved WITH the binding to ``composition`` —
    # the session graph calls it, and its strictness is the same three claims
    # it always made.
    #
    # Default-fill tolerance: the readback is a normalized SUPERSET of the
    # submitted text, and a load is still CONFIRMED.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "program: graph\n"))
    # A genuinely different graph is still rejected — the check is strict
    # equality of normalized fingerprints, not a subset comparison.
    cam.live = "different: graph\n"
    with pytest.raises(ProgramPlaybackError, match="load was not confirmed"):
        asyncio.run(
            composition.confirm_graph_is_live(cam, "program: graph\n")
        )
    # Comment-only differences are benign: camilla's serde drops them.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "# a note\nprogram: graph\n"))
    # A submitted config camilla itself refuses is a NAMED refusal, distinct
    # from a mismatch, so hardware triage can tell the two apart.
    with pytest.raises(ProgramPlaybackError, match="normalization failed"):
        asyncio.run(composition.confirm_graph_is_live(cam, "!!not-yaml\n"))


def test_v2_session_spec_is_a_valid_protocol_3_crossover_spec():
    spec = build_v2_session_spec(
        _roles(), FC_HZ, acknowledgement_binding="b" * 24,
    )
    assert spec.kind == "crossover_sweep"
    assert spec.capture_protocol_version == 3
    assert spec.capture_plan is not None
    # Round-trips through the strict boundary validation.
    from jasper.active_speaker.crossover_v2.sweep_spec import CaptureSpec

    reparsed = CaptureSpec.from_dict(spec.to_dict())
    assert reparsed.capture_plan.entries == spec.capture_plan.entries


@pytest.mark.parametrize("positions", [MIN_CLOUD_MEASURE_POSITIONS - 1,
                                       MAX_CLOUD_MEASURE_POSITIONS + 1])
def test_cloud_position_count_outside_the_declared_range_is_refused(positions):
    with pytest.raises(CrossoverV2FlowError):
        build_v2_capture_plan(_roles(), FC_HZ, cloud_measure_positions=positions)


def test_session_wall_clock_ceiling_scales_with_the_plan_and_is_capped():
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_WALL_CLOCK_CEILING_S,
        MAX_WALL_CLOCK_CEILING_S,
    )

    assert session_wall_clock_ceiling_s(
        build_v2_verify_capture_plan(FC_HZ, plan_shape=resolve_plan_shape())
    ) == 2160.0
    assert MAX_WALL_CLOCK_CEILING_S == 3600.0
    assert session_wall_clock_ceiling_s(
        types.SimpleNamespace(capture_target=100)
    ) == MAX_WALL_CLOCK_CEILING_S
    # The 1-entry re-verify never widens the baseline.
    assert (
        session_wall_clock_ceiling_s(build_v2_verify_capture_plan(FC_HZ))
        == DEFAULT_WALL_CLOCK_CEILING_S
    )


def test_shipped_v2_plans_serialize_to_byte_identical_wire_payloads():
    """Every shipped plan's wire bytes are pinned; only an intended edit moves
    them."""
    import hashlib
    import json

    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_verify_capture_plan,
    )

    plans = {
        "stage2-full": build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(),
        ),
        "stage2-express": build_v2_verify_capture_plan(
            FC_HZ, plan_shape=resolve_plan_shape(TIER_EXPRESS),
        ),
        "1-entry": build_v2_verify_capture_plan(FC_HZ),
    }
    assert set(plans) == set(_GOLDEN_V2_PLAN_BYTES)
    for label, plan in plans.items():
        raw = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
        expected_len, expected_sha = _GOLDEN_V2_PLAN_BYTES[label]
        actual_sha = hashlib.sha256(raw).hexdigest()
        assert (len(raw), actual_sha) == (expected_len, expected_sha), (
            f"{label} v2 capture plan wire bytes changed: "
            f"len={len(raw)} sha256={actual_sha}"
        )
