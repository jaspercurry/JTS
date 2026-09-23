# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: commission tiers, the retake/confirm contract, and the courtesy-tone prelude."""

from __future__ import annotations

import asyncio
import re
import pytest
import yaml
from dataclasses import replace
from jasper.active_speaker.capture_geometry import SUMMED_PLACEMENT_POLICY_ID
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    CAPTURE_ENTRY_MARGIN_MS,
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    CLOUD_POSITION_PROMPTS,
    GEOMETRY_RETRY_OFFSET_CM,
    MAX_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_MEASURE_POSITIONS,
    MIN_CLOUD_OFFSET_CM,
    MIN_CLOUD_VERIFY_POSITIONS,
    WIDE_OFFSET_MIN_CM,
    _program_duration_ms,
    _pose,
    format_position_distance,
    resolve_plan_shape,
)
from jasper.active_speaker.crossover_v2.spatial import POSITION_ROLE_ONAX, POSITION_ROLES
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session
from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
from jasper.audio_measurement.program import (
    KIND_COURTESY_TONE, BASE_STIMULUS_PEAK_DBFS,
    build_check_program, build_measure_program,
)
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    SESSION,
    SESSION_VOLUME_DB,
    _conductor,
    _inline_spec,
    _dummy_program,
    _preset,
    _roles,
    _run_phase,
)


# --- commission tiers + the retake/confirm contract (flow-simplification) ----


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


@pytest.mark.parametrize("positions", [MIN_CLOUD_VERIFY_POSITIONS - 1, 0])
def test_a_verify_group_too_short_for_two_wide_offsets_is_refused(positions):
    """The hole NEW-9 named: nothing stopped a caller asking for a post-apply
    group that never reaches a ~30 cm-class offset."""
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(cloud_verify_positions=positions)


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
    plan = _inline_spec().capture_plan
    entries = {entry.kind_label: entry for entry in plan.entries}
    roles = _roles()
    check = build_check_program(roles, courtesy_prelude=True)
    measure = build_measure_program({rb.role: BASE_STIMULUS_PEAK_DBFS for rb in roles}, roles)
    for phase, program in ((PHASE_CHECK, check), (PHASE_MEASURE, measure)):
        assert entries[phase].duration_ms == _program_duration_ms(program) + CAPTURE_ENTRY_MARGIN_MS
    assert entries[PHASE_CHECK].duration_ms - (
        _program_duration_ms(build_check_program(roles)) + CAPTURE_ENTRY_MARGIN_MS
    ) == pytest.approx(_courtesy_prelude_ms(), abs=1)



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


def test_inline_session_spec_is_a_valid_protocol_3_crossover_spec():
    spec = _inline_spec()
    assert spec.kind == "crossover_sweep"
    assert spec.capture_protocol_version == 3
    assert spec.capture_plan is not None
    assert spec.acknowledgement.id == SUMMED_PLACEMENT_POLICY_ID


@pytest.mark.parametrize("positions", [MIN_CLOUD_MEASURE_POSITIONS - 1,
                                       MAX_CLOUD_MEASURE_POSITIONS + 1])
def test_cloud_position_count_outside_the_declared_range_is_refused(positions):
    with pytest.raises(CrossoverV2FlowError):
        resolve_plan_shape(cloud_measure_positions=positions)
