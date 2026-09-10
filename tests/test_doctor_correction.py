# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor correction domain.

Every assertion pins ``status`` and ``reason`` — never ``detail`` prose
(ADR-0233 rule 3). ``correction.REASON_*`` is the closed vocabulary.
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.cli.doctor import _evidence, _shared, correction

from .doctor_test_support import (
    _make_unit_states_fake,
    _own_group,
    _pretend_group_is_jasper,
    _stub_unit_active_states,
    _write_identity_env,
)


# ---------- #1860: long-outstanding idle-exit holds


_LEAKED_HOLD_LINE = (
    "systemd idle-exit deferred: 1 active requests/holds after 7530s "
    "idle, busy for 7530s (threshold 600s, holds: relay:level_ramp:room) "
    "— busy past 7200s, so this is a LEAKED hold, not a long session: "
    "the process can no longer idle-exit and its on-idle-exit hook "
    "cannot run"
)


def _idle_exit_journal(monkeypatch, *, journal, active="active"):
    _stub_unit_active_states(
        monkeypatch, {correction._CORRECTION_WEB_UNIT: active},
    )

    def fake_run(cmd, timeout=5.0):
        assert cmd[0] == "journalctl"
        assert "warning" in cmd  # -p warning: only escalated lines are fetched
        if isinstance(journal, Exception):
            raise journal
        return journal

    monkeypatch.setattr(correction, "_run", fake_run)


def _journal(stdout="", *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        ["journalctl"], returncode, stdout=stdout, stderr=stderr
    )


def test_latest_deferred_hold_keeps_the_newest_line():
    """journalctl returns oldest-first; an older (possibly since-resolved)
    line must not shadow the most recent evidence."""
    older = (
        "systemd idle-exit deferred: 1 active requests/holds after 7300s "
        "idle, busy for 7300s (threshold 600s, holds: relay:crossover_v2:session)"
    )
    newer = (
        "systemd idle-exit deferred: 1 active requests/holds after 7830s "
        "idle, busy for 7830s (threshold 600s, holds: relay:level_ramp:crossover)"
    )

    assert correction._latest_deferred_hold(f"{older}\n{newer}\n") == (
        "7830", "relay:level_ramp:crossover",
    )
    assert correction._latest_deferred_hold("nothing to see\n") is None


# ---------- measurement-page HTTPS assets


def _web_root_with_app_css(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("/* x */", encoding="utf-8")
    return tmp_path


# ---------- state dirs
#
# os.access(W_OK) always reports root's own access, not the dropped jasper-web
# writer's — a root:root 0700 dir read as "ok" while jasper-web was locked out.


@pytest.mark.parametrize(
    "mode, group, expect_flagged",
    [
        (0o2770, None, False),
        (0o2750, None, True),  # the exact regression: group-write stripped
        # setgid lost (2770 -> 0770): a root-run process creating a NEW
        # subdirectory later (e.g. a session folder) would land it
        # group-root, not group-jasper, locking the non-root arm out of it.
        (0o0770, None, True),
        (0o2770, "jts-no-such-group-xyz", True),
        (0o0700, None, True),  # the fresh-install bug: no group bits at all
    ],
    ids=[
        "group-writable", "group-readonly", "setgid-lost", "wrong-group",
        "owner-only-0700",
    ],
)
def test_not_writable_by_group_verdicts(tmp_path, mode, group, expect_flagged):
    d = tmp_path / "sweeps"
    d.mkdir()
    os.chmod(d, mode)

    flagged = correction._not_writable_by_group(
        [d], expected_group=group or _own_group()
    )

    assert (str(d) in flagged) is expect_flagged


def test_check_correction_state_dirs_flags_uploaded_calibrations_needing_review(
    monkeypatch,
    tmp_path,
):
    """Vendor records are repaired automatically on deploy; an UPLOADED
    record's convention is the household's own declaration, so the doctor
    surfaces it for review instead of anyone flipping it silently. This
    advisory rides the state-dirs row (both inspect the correction root) and
    only shows once the dirs themselves are healthy."""
    from jasper.audio_measurement import calibration as cal

    _pretend_group_is_jasper(monkeypatch)
    root = tmp_path / "correction"
    root.mkdir()
    os.chmod(root, 0o2770)
    for name in ("calibration_mics", "tones"):
        d = root / name
        d.mkdir()
        os.chmod(d, 0o2770)
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(root))
    cal_dir = tmp_path / "calibrations"
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(cal_dir))

    assert correction.check_correction_state_dirs().reason == ""

    cal.store_calibration(
        text="20 -1\n1000 0\n20000 2\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        sign_convention="correction",
        root=cal_dir,
    )
    cal.store_calibration(  # a vendor record: not this check's business
        text="20 -1\n1000 0\n20000 2\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="UMIK-2",
        source="vendor_lookup",
        serial="810-8494",
        sign_convention="correction",
        root=cal_dir,
    )

    r = correction.check_correction_state_dirs()
    assert r.status == "ok"
    assert r.reason == correction.REASON_UPLOADED_CALIBRATION_SIGN_REVIEW

    # An upload that already declares the response convention is clean, and
    # the check never fails the doctor either way.
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "clean"))
    cal.store_calibration(
        text="20 -3\n1000 0\n20000 4\n",
        provider="manual_upload",
        model="other",
        label="Other mic",
        source="uploaded:other.txt",
        sign_convention="response",
        root=tmp_path / "clean",
    )
    clean = correction.check_correction_state_dirs()
    assert clean.status == "ok"
    assert clean.reason == ""


def _sound_config_text(room_peqs=()):
    """A real ``emit_sound_config`` graph, so the fixture cannot drift from
    what the reader under test parses."""

    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    return emit_sound_config(SoundProfile(), room_peqs=list(room_peqs))


def _room_peq(freq=63.0, q=2.0, gain=-4.5):
    from jasper.sound.camilla_yaml import PeqFilter

    return PeqFilter(freq=freq, q=q, gain=gain)


_ACTIVE_STAGED_CONFIG = (
    "# Source: jasper.active_speaker.camilla_yaml."
    "emit_active_speaker_startup_config\n"
    "devices:\n"
    "  playback:\n"
    "    device: jts_active\n"
)


def _round_tripped_active_config():
    """An active-speaker graph as CamillaDSP hands it back: the YAML round-trip
    drops every comment, so the `# Source:` marker is gone and only the split
    mixer is left to prove the graph is JTS-generated."""

    import yaml

    return yaml.safe_dump(
        yaml.safe_load(
            _ACTIVE_STAGED_CONFIG
            + "mixers:\n  split_active_2way:\n    channels: { in: 2, out: 4 }\n"
        )
    )


def _hand_written_config_on_the_jts_ring():
    """An operator config with no JTS provenance that plays out of the ring
    JTS itself uses — naming the device is not provenance."""

    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_DEVICE

    return (
        "devices:\n"
        "  playback:\n"
        f"    device: {DEFAULT_PLAYBACK_DEVICE}\n"
        "  volume_limit: 0.0\n"
    )


# ---------- crossover v2 cloud pipeline (+ folded-in applied-grade finding)


def _patch_v2_state(monkeypatch, state):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", lambda: state)
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )


def _cloud_group(*, passed, locked=False, excluded=(), flatness=None):
    pipeline: dict[str, object] = {
        "available": True,
        "spec": {"overall_passed": passed, "bands": []},
        "merged_excluded_bands_hz": list(excluded),
    }
    if flatness is not None:
        pipeline["flatness"] = flatness
    return {"geometry": {"locked": locked}, "pipeline": pipeline}


def _cloud_group_unavailable(*, reason, locked=True):
    """A group that CLOSED but whose pipeline failed to combine/analyze —
    ``assemble_cloud_group_result``'s own ``combined is None`` shape."""
    return {
        "geometry": {"locked": locked},
        "pipeline": {"available": False, "reason": reason},
    }


def _v2_applied_state(**overrides):
    state = {"applied": True, "session_id": "sess-graded"}
    state.update(overrides)
    return state


_FAILED_GAUGE = {
    "max_db": -4.628, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
    "tolerance_db": 1.5, "rms_db": 1.9, "n_bins": 700, "n_excluded": 40,
    "evaluable": True, "passed": False,
}

_PASSING_GAUGE = {**_FAILED_GAUGE, "max_db": 0.9, "passed": True}

_UNMEASURABLE_GAUGE = {
    **_FAILED_GAUGE, "max_db": None, "max_hz": None, "evaluable": False,
}


def _verify_cloud(*, passed, flatness):
    return {"cloud_verify": _cloud_group(
        passed=passed,
        excluded=[[1400.0, 1900.0], [3000.0, 3200.0],
                  [5000.0, 5400.0], [9000.0, 9600.0]],
        flatness=flatness,
    )}


@pytest.mark.parametrize(
    "state, status, reason",
    [
        # ---- no correction applied: the row is the cloud-only verdict.
        pytest.param(
            None, "ok", correction.REASON_CLOUD_NOT_RUN, id="never-run",
        ),
        pytest.param(
            {"cloud": {}}, "ok", correction.REASON_CLOUD_NOT_RUN, id="no-groups",
        ),
        # Only cloud_verify (the post-apply, household-actionable grade) gates
        # the warn: cloud_measure is the uncorrected pre-apply baseline, and
        # gating on it warns forever on a perfectly corrected speaker.
        pytest.param(
            {"cloud": {
                "cloud_measure": _cloud_group(passed=True, locked=True,
                                              excluded=[[8000.0, 9000.0]]),
                "cloud_verify": _cloud_group(passed=False),
            }},
            "warn", correction.REASON_CLOUD_VERIFY_SPEC_FAILED,
            id="verify-failed",
        ),
        pytest.param(
            {"cloud": {
                "cloud_measure": _cloud_group(passed=False,
                                              excluded=[[8000.0, 9000.0]]),
                "cloud_verify": _cloud_group(passed=True),
            }},
            "ok", "", id="pre-apply-failed-only",
        ),
        pytest.param(
            {"cloud": {"cloud_measure": _cloud_group(passed=True)}}, "ok", "",
            id="all-passing",
        ),
        # A closed group whose pipeline never became available is not itself a
        # spec failure.
        pytest.param(
            {"cloud": {
                "cloud_measure": _cloud_group_unavailable(reason="combine_failed"),
            }},
            "ok", "", id="pipeline-unavailable",
        ),
        # ---- applied: the folded-in grade finding takes the row's reason —
        # an un-warned cloud spec cannot see a correction that never got
        # graded, so this is exactly the gap the fold-in closes (#2160).
        # Nothing applied yet: the grade finding is not itself a finding, so
        # it never competes with the cloud verdict's own reason.
        pytest.param(
            _v2_applied_state(applied=False), "ok",
            correction.REASON_CLOUD_NOT_RUN, id="not-applied",
        ),
        # Applied with no post-apply group and no VERIFY outcome — the
        # silence the cloud verdict alone structurally cannot see.
        pytest.param(
            _v2_applied_state(), "ok",
            correction.REASON_APPLIED_GRADE_NEVER_GRADED, id="never-graded",
        ),
        # Express tier omits the post-apply position group, so a passing
        # VERIFY outcome is the whole grade and satisfies the finding on its
        # own — no cloud session either, so the cloud reason shows through.
        pytest.param(
            _v2_applied_state(verify={"outcome": "pass"}), "ok",
            correction.REASON_CLOUD_NOT_RUN, id="verify-alone",
        ),
        pytest.param(
            _v2_applied_state(verify={"outcome": "inconclusive"}), "ok",
            correction.REASON_APPLIED_GRADE_VERIFY_INCONCLUSIVE,
            id="verify-inconclusive",
        ),
        # #2160: a grade that EXISTS is not a grade that PASSED. This printed
        # "applied and graded" beside a cloud line reading spec=fail. The
        # cloud_verify failure here also gates the row's own status, and its
        # reason wins the row's reason on a WARN; the spatial-failed grade
        # detail still rides the detail text.
        pytest.param(
            _v2_applied_state(
                tier="full", verify={"outcome": "pass"},
                cloud=_verify_cloud(passed=False, flatness=_FAILED_GAUGE),
            ),
            "warn", correction.REASON_CLOUD_VERIFY_SPEC_FAILED,
            id="spatial-failed",
        ),
        # passed=False with evaluable=False means "could not be measured", not
        # "failed" — SpecFlatness.passed's own read-it-with-evaluable rule.
        # The cloud reason still wins the row's reason on this WARN.
        pytest.param(
            _v2_applied_state(
                tier="full", verify={"outcome": "pass"},
                cloud=_verify_cloud(passed=False, flatness=_UNMEASURABLE_GAUGE),
            ),
            "warn", correction.REASON_CLOUD_VERIFY_SPEC_FAILED,
            id="spatial-unmeasurable",
        ),
        # #2098: a Full session verified only at the mark is not the claim Full
        # promised.
        pytest.param(
            _v2_applied_state(tier="full", verify={"outcome": "pass"}), "ok",
            correction.REASON_APPLIED_GRADE_MARK_ONLY, id="full-mark-only",
        ),
        # A group that closed but could not combine reaches the same arm — the
        # wording claims delivered evidence only, never "never closed".
        pytest.param(
            _v2_applied_state(
                tier="full", verify={"outcome": "pass"},
                cloud={
                    "cloud_verify": _cloud_group_unavailable(
                        reason="combine_failed"
                    ),
                },
            ),
            "ok", correction.REASON_APPLIED_GRADE_MARK_ONLY,
            id="full-closed-but-unavailable",
        ),
        # The mark IS express's whole promise; a finding here would fire on
        # every express session ever run.
        pytest.param(
            _v2_applied_state(tier="express", verify={"outcome": "pass"}),
            "ok", correction.REASON_CLOUD_NOT_RUN, id="express-mark",
        ),
        pytest.param(
            _v2_applied_state(
                tier="full", verify={"outcome": "pass"},
                cloud=_verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            ),
            "ok", "", id="spatial-passed",
        ),
        # #2464: a failed mark-VERIFY names verify_failed whatever the
        # spatial group says.
        pytest.param(
            _v2_applied_state(
                tier="full",
                verify={
                    "outcome": "fail",
                    "claims": {"integration": {"status": "fail", "max_db": 4.2}},
                },
                cloud=_verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            ),
            "ok", correction.REASON_APPLIED_GRADE_VERIFY_FAILED,
            id="verify-failed-behind-a-passing-group",
        ),
        # The capture was clean and the crossover-region claim missed its
        # tolerance: verify.outcome grades capture health alone, so the claims
        # record is what sees it.
        pytest.param(
            _v2_applied_state(
                tier="full",
                verify={
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "fail", "max_db": 4.31},
                    },
                },
                cloud=_verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            ),
            "ok", correction.REASON_APPLIED_GRADE_VERIFY_FAILED,
            id="failed-absolute-claim",
        ),
        # The cloud spec failure wins the row's reason on a WARN — it is why
        # the row warned, and the mark-VERIFY finding must not hide that
        # cause even though it also found something.
        pytest.param(
            _v2_applied_state(
                tier="full", verify={"outcome": "inconclusive"},
                cloud=_verify_cloud(passed=False, flatness=_FAILED_GAUGE),
            ),
            "warn", correction.REASON_CLOUD_VERIFY_SPEC_FAILED,
            id="inconclusive-behind-a-closed-group",
        ),
        # The result code is DISCLOSED beside the grade and never gates it:
        # the finding grades the CHECKING, which passed completely here,
        # while the household badge honestly reads "Keep the previous sound."
        pytest.param(
            _v2_applied_state(
                tier="full",
                verify={
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "pass", "max_db": 0.8},
                    },
                },
                verify_priors={
                    "predicted_spec": {
                        "comparison": {
                            "reason": "not_an_improvement",
                            "improvement_db": 0.1,
                            "required_db": 0.5,
                        },
                    },
                },
                cloud=_verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            ),
            "ok", "", id="keep-previous-result-does-not-gate",
        ),
        # The same posture one tier over: every instrument that grades the
        # CHECKING passed, and the result code is `inconclusive` anyway because
        # the crossover region carried no spec tolerance for an absolute
        # verdict. A finding here would fire on a healthy commission.
        pytest.param(
            _v2_applied_state(
                tier="express",
                verify={
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {
                            "status": "not_evaluated",
                            "reason": "no_spec_tolerance_for_region",
                        },
                    },
                },
            ),
            "ok", correction.REASON_CLOUD_NOT_RUN,
            id="express-inconclusive-result-does-not-gate",
        ),
    ],
)
def test_check_crossover_v2_cloud_pipeline_verdicts(
    monkeypatch, state, status, reason
):
    _patch_v2_state(monkeypatch, state)

    r = correction.check_crossover_v2_cloud_pipeline()

    assert r.status == status
    assert r.reason == reason


def test_an_unknown_spatial_word_from_a_later_build_is_disclosed(monkeypatch):
    """The direction ``state`` already follows for a value this build cannot
    read is the unrecognized reason code, never the passing wording (S1,
    #2242). The grade is injected directly because no producer path can emit
    it — that is the point."""
    from jasper.web import correction_crossover_v2 as v2host
    from jasper.web import correction_crossover_v2_status as v2status

    monkeypatch.setattr(
        v2status,
        "crossover_v2_status_block",
        lambda: {
            "tier": "full",
            "post_apply_grade": {
                "state": v2host.GRADE_GRADED,
                "graded": True,
                "verify_outcome": "pass",
                "scope": "hemispherical-2027",
                "spatial": "graded_from_orbit",
                "complete": True,
            },
        },
    )

    r = correction.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert r.reason == correction.REASON_APPLIED_GRADE_SPATIAL_UNRECOGNIZED


def test_a_measured_tuning_trial_does_not_require_speaker_verify(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host
    from jasper.web import correction_crossover_v2_status as v2status

    monkeypatch.setattr(
        v2status,
        "crossover_v2_status_block",
        lambda: {
            "post_apply_grade": {
                "state": v2host.GRADE_TUNING_TRIAL_MEASURED,
                "scope": v2host.GRADE_SCOPE_TUNING_TRIAL,
                "verify_outcome": None,
            },
        },
    )

    r = correction.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert r.reason == correction.REASON_CLOUD_NOT_RUN


def test_grade_spatial_and_scope_member_sets_are_pinned_for_their_consumers():
    """Walking-class guard (S1, #2242). ``GRADE_SPATIAL_*`` and ``GRADE_SCOPE_*``
    are consumed by literal membership tests in more than one surface — this
    doctor check and the envelope's done-screen branches
    (``jasper/active_speaker/crossover_envelope_v2.py``, which spells the same
    words as raw literals because it never imports ``jasper.web``). Neither
    iterates the producer's vocabulary, so a NEW member is invisible to both
    until someone teaches each dispatch site the new word by hand.

    If this fails because you added a member: teach
    ``_applied_grade_finding`` and the done-screen branches the new word (or
    confirm the existing fallthrough is what you want), then extend the
    pinned sets below.
    """
    from jasper.web import correction_crossover_v2 as v2host

    spatial_members = {
        value for name, value in vars(v2host).items()
        if name.startswith("GRADE_SPATIAL_") and isinstance(value, str)
    }
    scope_members = {
        value for name, value in vars(v2host).items()
        if name.startswith("GRADE_SCOPE_") and isinstance(value, str)
    }

    assert spatial_members == {
        v2host.GRADE_SPATIAL_ABSENT,
        v2host.GRADE_SPATIAL_PASSED,
        v2host.GRADE_SPATIAL_FAILED,
        v2host.GRADE_SPATIAL_UNMEASURABLE,
    }
    assert scope_members == {
        v2host.GRADE_SCOPE_NONE,
        v2host.GRADE_SCOPE_MARK,
        v2host.GRADE_SCOPE_TUNING_TRIAL,
        v2host.GRADE_SCOPE_SPATIAL,
    }


# ---------- measurement hold + unresolved session volume


def _patch_measurement(monkeypatch, hold=None, error=None):
    """Point check_measurement_hold's control read at a scripted answer."""
    from jasper.platform import control_client

    def fake_get_measurement(**_kwargs):
        if error is not None:
            raise error
        return hold or {}

    monkeypatch.setattr(control_client, "get_measurement", fake_get_measurement)


def _hold(**overrides):
    from jasper.active_speaker.session_volume_plan import MAX_WALL_CLOCK_CEILING_S

    hold = {
        "active": True, "owner": "seat-level", "mode": "gate", "held_for_s": 90.0,
    }
    hold.update(overrides)
    if hold.get("held_for_s") == "over-ceiling":
        hold["held_for_s"] = MAX_WALL_CLOCK_CEILING_S + 1.0
    return hold


@pytest.mark.parametrize(
    "hold, status, reason",
    [
        ({"active": False}, "ok", ""),
        (_hold(), "ok", correction.REASON_MEASUREMENT_HOLD_ACTIVE),
        # The shape TTLs cannot catch: a live holder whose session never ends.
        (
            _hold(owner="correction-measurement", held_for_s="over-ceiling"),
            "warn", correction.REASON_MEASUREMENT_HOLD_STUCK,
        ),
        # A hold whose age cannot be read must not read as healthy.
        (
            _hold(held_for_s=None), "warn",
            correction.REASON_MEASUREMENT_HOLD_AGE_UNREADABLE,
        ),
    ],
    ids=["idle", "live", "stuck", "unreadable-age"],
)
def test_check_measurement_hold_verdicts(monkeypatch, hold, status, reason):
    _patch_measurement(monkeypatch, hold=hold)
    r = correction.check_measurement_hold()
    assert r.status == status
    assert r.reason == reason


def test_measurement_hold_skips_when_control_is_down(monkeypatch):
    from jasper.platform import control_client

    _patch_measurement(monkeypatch, error=control_client.ControlError("refused"))
    r = correction.check_measurement_hold()
    assert r.status == "skipped"
    assert r.reason == correction.REASON_MEASUREMENT_HOLD_CONTROL_UNREACHABLE


def _write_session_volume(path, *, status, opened_at, ceiling_s=1800.0):
    from jasper.active_speaker.session_volume_plan import (
        SCHEMA_VERSION,
        STATE_KIND,
    )

    path.write_text(json.dumps({
        "kind": STATE_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": None if status == "active" else "restore_unconfirmed",
        "opened_at": opened_at,
        "wall_clock_ceiling_s": ceiling_s,
        "measurement_volume_db": -20.0,
        "original_main_volume_db": -6.0,
    }), encoding="utf-8")


@pytest.mark.parametrize(
    "state, age_s, status, reason",
    [
        (None, 0.0, "ok", ""),
        # Nothing in jasper/cli/doctor read needs_recovery before this check: a
        # speaker could sit holding a measurement volume with every check green.
        (
            "unresolved", 0.0, "warn",
            correction.REASON_SESSION_VOLUME_UNRESOLVED,
        ),
        # The flow force-drains this one, so it is reported, not acted on.
        ("active", 4000.0, "ok", correction.REASON_SESSION_VOLUME_STALE_ACTIVE),
        (
            "active", 0.0, "warn",
            correction.REASON_SESSION_VOLUME_ACTIVE_NO_OWNER,
        ),
    ],
    ids=["idle", "unresolved-latch", "stale-active", "active-no-owner"],
)
def test_check_session_volume_unresolved_verdicts(
    monkeypatch, tmp_path, state, age_s, status, reason
):
    import time

    path = tmp_path / "session_volume.json"
    if state is not None:
        _write_session_volume(path, status=state, opened_at=time.time() - age_s)
    monkeypatch.setattr(
        correction, "DEFAULT_SESSION_VOLUME_STATE_PATH", path,
    )

    r = correction.check_session_volume_unresolved()

    assert r.status == status
    assert r.reason == reason


# ---------- seat-SPL measurement reference


def _bank(path, **overrides):
    from jasper.active_speaker.seat_level_reference import (
        SeatLevelTarget,
        write_seat_level_reference,
    )

    payload = dict(
        reference_volume_db=-17.25,
        measured_db_spl=77.4,
        target=SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07, "serial": "8108494"},
        max_main_volume_db=-30.0,
        state_path=path,
    )
    payload.update(overrides)
    return write_seat_level_reference(**payload)


def test_seat_level_reference_absent_is_ok_not_a_warning(tmp_path):
    # A box that never ran the leveling step is healthy: the session falls back
    # to the codified reference and measures exactly as it always did.
    result = correction._classify_seat_level_reference(tmp_path / "absent.json")
    assert result.status == "ok"
    assert result.reason == correction.REASON_SEAT_LEVEL_NOT_MEASURED


def test_seat_level_reference_reports_a_banked_value(tmp_path):
    path = tmp_path / "ref.json"
    _bank(path)
    result = correction._classify_seat_level_reference(path)
    assert result.status == "ok"
    assert result.reason == ""


def test_seat_level_reference_present_but_unusable_warns(tmp_path):
    """The runtime reader falls back silently; the doctor must not. An
    out-of-envelope value reads as absent at runtime — the speaker measures at
    the codified default and says nothing."""
    path = tmp_path / "ref.json"
    path.write_text(
        json.dumps(
            {
                "kind": "jts_active_speaker_seat_level_reference",
                "artifact_schema_version": 1,
                "reference_volume_db": 3.0,
            }
        )
    )
    result = correction._classify_seat_level_reference(path)
    assert result.status == "warn"
    assert result.reason == correction.REASON_SEAT_LEVEL_UNUSABLE


def test_seat_level_reference_unparseable_timestamp_still_reports_the_value(tmp_path):
    path = tmp_path / "ref.json"
    _bank(path)
    raw = json.loads(path.read_text())
    raw["updated_at"] = "not-a-date"
    path.write_text(json.dumps(raw))
    result = correction._classify_seat_level_reference(path)
    assert result.status == "ok"
    assert result.reason == correction.REASON_SEAT_LEVEL_TIMESTAMP_UNREADABLE


# ---------- measurement-page TLS cert
#
# check_correction_cert_hostname compares the cert's SAN against the name the
# speaker actually advertises, so a collision-renamed box stops serving a cert
# nobody's browser will accept.


def _with_cert(monkeypatch, tmp_path, exists=True):
    cert = tmp_path / "jts.local.crt"
    if exists:
        cert.write_text("---")
    real_path = correction.Path
    monkeypatch.setattr(
        correction,
        "Path",
        lambda p: cert if p == "/etc/nginx/ssl/jts.local.crt" else real_path(p),
    )


def _openssl_san(*names: str):
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "X509v3 Subject Alternative Name:\n    "
            + ", ".join(f"DNS:{n}" for n in names)
            + "\n"
        ),
        stderr="",
    )


def test_cert_check_skips_without_a_cert(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path, exists=False)

    r = correction.check_correction_cert_hostname()

    assert r.status == "skipped"
    assert r.reason == correction.REASON_CERT_NOT_INSTALLED


@pytest.mark.parametrize(
    "advertised, san, status, reason",
    [
        (
            "jts3.local", ("jts3.local", "*.jts3.local", "jts.local"), "ok", "",
        ),
        (
            "jts3-2.local", ("jts3.local", "*.jts3.local"), "warn",
            correction.REASON_CERT_SAN_MISMATCH,
        ),
    ],
    ids=["san-covers", "san-misses"],
)
def test_cert_check_compares_the_san_to_the_advertised_name(
    monkeypatch, tmp_path, advertised, san, status, reason
):
    _with_cert(monkeypatch, tmp_path)
    _write_identity_env(
        tmp_path,
        monkeypatch,
        avahi=advertised,
        collision="0" if status == "ok" else "1",
        drift="0" if status == "ok" else "1",
    )

    with patch("subprocess.run", return_value=_openssl_san(*san)):
        r = correction.check_correction_cert_hostname()

    assert r.status == status
    assert r.reason == reason


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("openssl"),
        # PermissionError stands in for every plain OSError the narrower
        # (FileNotFoundError, TimeoutExpired) clause would miss — including
        # a fork failure (ENOMEM) under memory pressure on the Zero 2 W.
        PermissionError("openssl not executable"),
        subprocess.TimeoutExpired(cmd=["openssl"], timeout=5),
    ],
    ids=["absent", "oserror", "timeout"],
)
def test_cert_check_skips_when_the_san_cannot_be_read(
    monkeypatch, tmp_path, failure,
):
    _with_cert(monkeypatch, tmp_path)
    _write_identity_env(tmp_path, monkeypatch, avahi="jts3.local")

    with patch("subprocess.run", side_effect=failure):
        r = correction.check_correction_cert_hostname()

    assert r.status == "skipped"
    assert r.reason == correction.REASON_CERT_SAN_UNREADABLE


def test_cert_check_warns_when_openssl_exits_nonzero(monkeypatch, tmp_path):
    """openssl launched, unlike the skip cases above — but a non-zero exit
    is not unambiguously "read the bytes and rejected them": an
    unprivileged run, or a deploy rewriting the cert between `is_file()`
    and openssl's own open, exits non-zero too. Same reason as the skip
    arm, different status: this one ran."""
    _with_cert(monkeypatch, tmp_path)
    _write_identity_env(tmp_path, monkeypatch, avahi="jts3.local")

    with patch(
        "subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="bad cert"),
    ):
        r = correction.check_correction_cert_hostname()

    assert r.status == "warn"
    assert r.reason == correction.REASON_CERT_SAN_UNREADABLE


# ===========================================================================
# check_correction_web_service / check_correction_idle_exit_holds /
# check_correction_https_assets / check_correction_state_dirs /
# check_correction_current_config — one seed/patch setup per behavior, one
# status+reason assertion tail (AGENTS.md: one altitude per behavior, prefer
# one parametrized test over an example cluster). Test ids equal the old
# per-behavior function names so `pytest -k` and CI history keep working.
# ===========================================================================


def _corr_case_web_service_ok(monkeypatch, tmp_path):
    _stub_unit_active_states(monkeypatch, {"jasper-correction-web.socket": "active"})
    return correction.check_correction_web_service()


def _corr_case_web_service_warns(service_state):
    def _case(monkeypatch, tmp_path):
        _stub_unit_active_states(monkeypatch, {"jasper-correction-web.service": service_state})
        return correction.check_correction_web_service()

    return _case


def _corr_case_web_service_skips_no_systemctl(monkeypatch, tmp_path):
    monkeypatch.setattr(_evidence, "read_unit_states", _make_unit_states_fake(unavailable=True))
    return correction.check_correction_web_service()


def _corr_case_idle_exit_holds(active, journal):
    def _case(monkeypatch, tmp_path):
        _idle_exit_journal(monkeypatch, journal=journal, active=active)
        return correction.check_correction_idle_exit_holds()

    return _case


def _corr_case_idle_exit_holds_skips_no_systemctl(monkeypatch, tmp_path):
    monkeypatch.setattr(_evidence, "read_unit_states", _make_unit_states_fake(unavailable=True))
    return correction.check_correction_idle_exit_holds()


def _corr_case_https_assets(probe):
    def _case(monkeypatch, tmp_path):
        monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
        monkeypatch.setattr(correction, "_probe_https_status", probe)
        return correction.check_correction_https_assets()

    return _case


def _corr_case_https_assets_skips_no_web_root(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("must not probe when the web root is absent")

    monkeypatch.setattr(correction, "_probe_https_status", _boom)
    return correction.check_correction_https_assets()


def _corr_case_https_assets_skips_443_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))

    def _refused(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(correction, "_probe_https_status", _refused)
    return correction.check_correction_https_assets()


def _corr_case_state_dirs_warns_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(tmp_path / "missing"))
    return correction.check_correction_state_dirs()


def _corr_case_state_dirs_warns_locked_out(monkeypatch, tmp_path):
    _pretend_group_is_jasper(monkeypatch)
    root = tmp_path / "correction"
    root.mkdir()
    os.chmod(root, 0o700)
    for name in ("calibration_mics", "tones"):
        d = root / name
        d.mkdir()
        os.chmod(d, 0o700)
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(root))
    return correction.check_correction_state_dirs()


def _corr_case_current_config(relative_path, text):
    def _case(monkeypatch, tmp_path):
        config = tmp_path / relative_path
        if text is not None:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(text)
        statefile = tmp_path / "statefile.yml"
        statefile.write_text(f"config_path: {config}\n")
        monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
        return correction.check_correction_current_config()

    return _case


def _corr_case_current_config_unreadable_config(monkeypatch, tmp_path):
    config = tmp_path / "configs" / "sound_current.yml"
    config.mkdir(parents=True)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return correction.check_correction_current_config()


def _corr_case_current_config_unreadable_statefile(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    return correction.check_correction_current_config()


_C = correction


@pytest.mark.parametrize(
    "setup, expected_status, expected_reason",
    [
        pytest.param(_corr_case_web_service_ok, "ok", "", id="test_check_correction_web_service_ok_when_socket_active"),
        pytest.param(_corr_case_web_service_warns("active"), "warn", _C.REASON_WEB_SOCKET_INACTIVE, id="test_check_correction_web_service_warns_without_the_socket[service-up-socket-down]"),
        pytest.param(_corr_case_web_service_warns("inactive"), "warn", _C.REASON_WEB_INACTIVE, id="test_check_correction_web_service_warns_without_the_socket[both-down]"),
        pytest.param(_corr_case_web_service_skips_no_systemctl, "skipped", _shared.REASON_SYSTEMCTL_UNAVAILABLE, id="test_check_correction_web_service_skips_without_systemctl"),
        pytest.param(_corr_case_idle_exit_holds("inactive", _journal()), "skipped", _C.REASON_IDLE_HOLDS_SERVICE_INACTIVE, id="test_check_correction_idle_exit_holds_verdicts[service-inactive]"),
        pytest.param(_corr_case_idle_exit_holds("active", FileNotFoundError("journalctl not found")), "skipped", _C.REASON_IDLE_HOLDS_JOURNAL_UNAVAILABLE, id="test_check_correction_idle_exit_holds_verdicts[journalctl-raises]"),
        pytest.param(_corr_case_idle_exit_holds("active", _journal(returncode=1, stderr="invalid option -- since")), "skipped", _C.REASON_IDLE_HOLDS_JOURNAL_UNREADABLE, id="test_check_correction_idle_exit_holds_verdicts[journalctl-rc]"),
        pytest.param(_corr_case_idle_exit_holds("active", _journal()), "ok", _C.REASON_IDLE_HOLDS_NONE, id="test_check_correction_idle_exit_holds_verdicts[clean]"),
        pytest.param(_corr_case_idle_exit_holds("active", _journal(_LEAKED_HOLD_LINE + "\n")), "warn", _C.REASON_IDLE_HOLD_LEAKED, id="test_check_correction_idle_exit_holds_verdicts[leaked]"),
        pytest.param(_corr_case_idle_exit_holds_skips_no_systemctl, "skipped", _shared.REASON_SYSTEMCTL_UNAVAILABLE, id="test_check_correction_idle_exit_holds_skips_without_systemctl"),
        pytest.param(_corr_case_https_assets(lambda *a, **k: (200, "")), "ok", "", id="test_check_correction_https_assets_verdicts[served]"),
        pytest.param(_corr_case_https_assets(lambda *a, **k: (308, "http://jts.local/assets/app.css")), "warn", _C.REASON_HTTPS_ASSETS_HTTP_REDIRECT, id="test_check_correction_https_assets_verdicts[http-downgrade]"),
        pytest.param(_corr_case_https_assets(lambda *a, **k: (404, "")), "warn", _C.REASON_HTTPS_ASSETS_UNEXPECTED_STATUS, id="test_check_correction_https_assets_verdicts[unexpected-status]"),
        pytest.param(_corr_case_https_assets_skips_no_web_root, "skipped", _C.REASON_HTTPS_ASSETS_NOT_INSTALLED, id="test_check_correction_https_assets_skips_without_web_root"),
        pytest.param(_corr_case_https_assets_skips_443_unreachable, "skipped", _C.REASON_HTTPS_ASSETS_UNREACHABLE, id="test_check_correction_https_assets_skips_when_443_unreachable"),
        pytest.param(_corr_case_state_dirs_warns_missing, "warn", _C.REASON_STATE_DIRS_MISSING, id="test_check_correction_state_dirs_warns_on_missing"),
        pytest.param(_corr_case_state_dirs_warns_locked_out, "warn", _C.REASON_STATE_DIRS_NOT_WRITABLE, id="test_check_correction_state_dirs_warns_when_locked_out_by_mode"),
        pytest.param(_corr_case_current_config("does-not-exist.yml", None), "fail", _C.REASON_CAMILLA_CONFIG_MISSING, id="test_check_correction_current_config_verdicts[missing-config]"),
        pytest.param(_corr_case_current_config("v1.yml", "# base\n"), "warn", _C.REASON_CURRENT_CONFIG_UNCLASSIFIED, id="test_check_correction_current_config_verdicts[unclassified]"),
        pytest.param(_corr_case_current_config("configs/sound_current.yml", _sound_config_text()), "ok", _C.REASON_CURRENT_CONFIG_MANAGED, id="test_check_correction_current_config_verdicts[jts-sound]"),
        pytest.param(_corr_case_current_config("configs/active_speaker_staged_startup.yml", _ACTIVE_STAGED_CONFIG), "ok", _C.REASON_CURRENT_CONFIG_MANAGED, id="test_check_correction_current_config_verdicts[active-speaker-staged]"),
        pytest.param(_corr_case_current_config("configs/correction_abc_1700000000.yml", _sound_config_text([_room_peq()])), "ok", _C.REASON_CURRENT_CONFIG_ROOM_CORRECTION, id="test_check_correction_current_config_verdicts[generated-correction]"),
        pytest.param(_corr_case_current_config("configs/active_speaker_startup.yml", _round_tripped_active_config()), "ok", _C.REASON_CURRENT_CONFIG_MANAGED, id="test_check_correction_current_config_verdicts[round-tripped-active-graph]"),
        pytest.param(_corr_case_current_config("configs/operator.yml", _hand_written_config_on_the_jts_ring()), "warn", _C.REASON_CURRENT_CONFIG_UNCLASSIFIED, id="test_check_correction_current_config_verdicts[hand-written-on-the-jts-ring]"),
        pytest.param(_corr_case_current_config_unreadable_config, "warn", _C.REASON_CAMILLA_CONFIG_UNREADABLE, id="test_check_correction_current_config_warns_when_the_config_cannot_be_read"),
        pytest.param(_corr_case_current_config_unreadable_statefile, "warn", _C.REASON_CAMILLA_STATEFILE_UNREADABLE, id="test_check_correction_current_config_warns_on_an_unreadable_statefile"),
    ],
)
def test_check_correction_status(monkeypatch, tmp_path, setup, expected_status, expected_reason):
    r = setup(monkeypatch, tmp_path)

    assert r.status == expected_status
    assert r.reason == expected_reason
