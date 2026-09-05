# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor correction domain.

Every assertion pins ``status`` and ``reason`` — never ``detail`` prose
(ADR-0233 rule 3). ``correction.REASON_*`` is the closed vocabulary.
"""

import grp
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.cli.doctor import _evidence, correction
from jasper.correction import bundles

from .correction_bundle_fixtures import write_golden_correction_bundle

from .doctor_test_support import _pretend_group_is_jasper, _write_identity_env


def _stub_unit_active_states(monkeypatch, active: dict[str, str]) -> None:
    """`_evidence.read_unit_states` stand-in: units in `active` report that
    ActiveState; every other rostered unit reads inactive."""

    def fake(units, *, timeout):
        return {
            unit: {
                "unit": unit,
                "load_state": "loaded",
                "active_state": active.get(unit, "inactive"),
                "sub_state": "running" if active.get(unit) == "active" else "dead",
                "unit_file_state": "enabled",
                "n_restarts": 0,
                "main_pid": 0,
            }
            for unit in units
        }

    monkeypatch.setattr(_evidence, "read_unit_states", fake)


def test_check_correction_web_service_ok_when_socket_active(monkeypatch):
    _stub_unit_active_states(
        monkeypatch, {"jasper-correction-web.socket": "active"},
    )
    r = correction.check_correction_web_service()
    assert r.status == "ok"
    assert r.reason == ""


@pytest.mark.parametrize(
    "service_state, reason",
    [
        ("active", correction.REASON_WEB_SOCKET_INACTIVE),
        ("inactive", correction.REASON_WEB_INACTIVE),
    ],
    ids=["service-up-socket-down", "both-down"],
)
def test_check_correction_web_service_warns_without_the_socket(
    monkeypatch, service_state, reason
):
    _stub_unit_active_states(
        monkeypatch, {"jasper-correction-web.service": service_state},
    )
    r = correction.check_correction_web_service()
    assert r.status == "warn"
    assert r.reason == reason


# ---------- #1860: long-outstanding idle-exit holds


_LEAKED_HOLD_LINE = (
    "systemd idle-exit deferred: 1 active requests/holds after 7530s "
    "idle, busy for 7530s (threshold 600s, holds: relay:level_ramp:room) "
    "— busy past 7200s, so this is a LEAKED hold, not a long session: "
    "the process can no longer idle-exit and its on-idle-exit hook "
    "cannot run"
)


def _idle_exit_journal(monkeypatch, *, journal, active="active"):
    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{active}\n", stderr="")
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


@pytest.mark.parametrize(
    "active, journal, status, reason",
    [
        (
            "inactive", _journal(), "skipped",
            correction.REASON_IDLE_HOLDS_SERVICE_INACTIVE,
        ),
        # Nothing was observed, so these are `skipped`, never a green tick. A
        # broken invocation also stays distinguishable from a clean run: the
        # exception and the non-zero exit are separate reasons.
        (
            "active", FileNotFoundError("journalctl not found"), "skipped",
            correction.REASON_IDLE_HOLDS_JOURNAL_UNAVAILABLE,
        ),
        (
            "active", _journal(returncode=1, stderr="invalid option -- since"),
            "skipped", correction.REASON_IDLE_HOLDS_JOURNAL_UNREADABLE,
        ),
        ("active", _journal(), "ok", correction.REASON_IDLE_HOLDS_NONE),
        (
            "active", _journal(_LEAKED_HOLD_LINE + "\n"), "warn",
            correction.REASON_IDLE_HOLD_LEAKED,
        ),
    ],
    ids=["service-inactive", "journalctl-raises", "journalctl-rc", "clean", "leaked"],
)
def test_check_correction_idle_exit_holds_verdicts(
    monkeypatch, active, journal, status, reason
):
    _idle_exit_journal(monkeypatch, journal=journal, active=active)
    r = correction.check_correction_idle_exit_holds()
    assert r.status == status
    assert r.reason == reason


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


# ---------- /correction/ HTTPS assets


def _web_root_with_app_css(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("/* x */", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "probe, status, reason",
    [
        (lambda *a, **k: (200, ""), "ok", ""),
        # The bug signature: an HTTPS asset downgraded to http:// → browsers
        # mixed-content-block it.
        (
            lambda *a, **k: (308, "http://jts.local/assets/app.css"),
            "warn", correction.REASON_HTTPS_ASSETS_HTTP_REDIRECT,
        ),
        (
            lambda *a, **k: (404, ""),
            "warn", correction.REASON_HTTPS_ASSETS_UNEXPECTED_STATUS,
        ),
    ],
    ids=["served", "http-downgrade", "unexpected-status"],
)
def test_check_correction_https_assets_verdicts(
    monkeypatch, tmp_path, probe, status, reason
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(correction, "_probe_https_status", probe)
    r = correction.check_correction_https_assets()
    assert r.status == status
    assert r.reason == reason


def test_check_correction_https_assets_skips_without_web_root(monkeypatch, tmp_path):
    # Dev checkout: no /usr/share/jasper-web/assets/app.css → skip, never probes.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("must not probe when the web root is absent")

    monkeypatch.setattr(correction, "_probe_https_status", _boom)
    r = correction.check_correction_https_assets()
    assert r.status == "skipped"
    assert r.reason == correction.REASON_HTTPS_ASSETS_NOT_INSTALLED


def test_check_correction_https_assets_skips_when_443_unreachable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))

    def _refused(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(correction, "_probe_https_status", _refused)
    r = correction.check_correction_https_assets()
    assert r.status == "skipped"
    assert r.reason == correction.REASON_HTTPS_ASSETS_UNREACHABLE


# ---------- state dirs
#
# os.access(W_OK) always reports root's own access, not the dropped jasper-web
# writer's — a root:root 0700 dir read as "ok" while jasper-web was locked out.


def _own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


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


def test_check_correction_state_dirs_warns_on_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(tmp_path / "missing"))
    r = correction.check_correction_state_dirs()
    assert r.status == "warn"
    assert r.reason == correction.REASON_STATE_DIRS_MISSING


def test_check_correction_state_dirs_warns_when_locked_out_by_mode(
    monkeypatch, tmp_path
):
    """_pretend_group_is_jasper pins the group match so MODE ALONE (0700 — the
    fresh-install shape) is what fails this, not an incidental group mismatch
    with the dev/CI box's own group. The group-mismatch and setgid-loss arms are
    covered by test_not_writable_by_group_verdicts above."""
    _pretend_group_is_jasper(monkeypatch)
    root = tmp_path / "correction"
    root.mkdir()
    os.chmod(root, 0o700)
    for name in ("sweeps", "captures", "sessions", "calibration_mics"):
        d = root / name
        d.mkdir()
        os.chmod(d, 0o700)
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(root))

    r = correction.check_correction_state_dirs()

    assert r.status == "warn"
    assert r.reason == correction.REASON_STATE_DIRS_NOT_WRITABLE


def test_check_correction_uploaded_calibration_sign_flags_only_uploads(
    monkeypatch,
    tmp_path,
):
    """Vendor records are repaired automatically on deploy; an UPLOADED
    record's convention is the household's own declaration, so the doctor
    surfaces it for review instead of anyone flipping it silently."""
    from jasper.audio_measurement import calibration as cal

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    cal.store_calibration(
        text="20 -1\n1000 0\n20000 2\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        sign_convention="correction",
        root=tmp_path,
    )
    cal.store_calibration(  # a vendor record: not this check's business
        text="20 -1\n1000 0\n20000 2\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="UMIK-2",
        source="vendor_lookup",
        serial="810-8494",
        sign_convention="correction",
        root=tmp_path,
    )

    r = correction.check_correction_uploaded_calibration_sign()
    assert r.status == "ok"
    assert r.reason == correction.REASON_UPLOADED_CALIBRATION_SIGN_REVIEW

    # An upload that already declares the response convention is clean, and
    # the check never fails the doctor either way.
    cal.store_calibration(
        text="20 -3\n1000 0\n20000 4\n",
        provider="manual_upload",
        model="other",
        label="Other mic",
        source="uploaded:other.txt",
        sign_convention="response",
        root=tmp_path,
    )
    assert correction.check_correction_uploaded_calibration_sign().status == "ok"

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "empty"))
    clean = correction.check_correction_uploaded_calibration_sign()
    assert clean.status == "ok"
    assert clean.reason == ""


_JTS_SOUND_CONFIG = (
    "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
    "filters:\n"
    "  flat:\n"
    "    type: Gain\n"
)

_JTS_BASELINE_CONFIG = (
    "# Source: jasper.active_speaker.camilla_yaml."
    "emit_active_speaker_baseline_config\n"
    "filters:\n"
    "  active_baseline_headroom:\n"
    "    type: Gain\n"
)

_JTS_PROGRAM_BAKE_CONFIG = (
    "# Source: jasper.active_speaker.camilla_yaml."
    "emit_active_speaker_program_bake_config\n"
    "devices:\n"
    "  playback:\n"
    "    type: File\n"
)


@pytest.mark.parametrize(
    "relative_path, text, status, reason",
    [
        (
            "does-not-exist.yml", None, "fail",
            correction.REASON_CAMILLA_CONFIG_MISSING,
        ),
        (
            "v1.yml", "# base\n", "warn",
            correction.REASON_CURRENT_CONFIG_UNCLASSIFIED,
        ),
        (
            "configs/sound_current.yml", _JTS_SOUND_CONFIG, "ok",
            correction.REASON_CURRENT_CONFIG_MANAGED,
        ),
        (
            "configs/active_speaker_baseline.yml", _JTS_BASELINE_CONFIG, "ok",
            correction.REASON_CURRENT_CONFIG_MANAGED,
        ),
        (
            "configs/grouping_active_leader_bake.yml", _JTS_PROGRAM_BAKE_CONFIG,
            "ok", correction.REASON_CURRENT_CONFIG_MANAGED,
        ),
        (
            "configs/correction_abc_1700000000.yml",
            "filters:\n  room_peq_1:\n    type: Biquad\n",
            "ok", correction.REASON_CURRENT_CONFIG_ROOM_CORRECTION,
        ),
    ],
    ids=[
        "missing-config", "unclassified", "jts-sound", "active-speaker-baseline",
        "active-leader-bake", "generated-correction",
    ],
)
def test_check_correction_current_config_verdicts(
    monkeypatch, tmp_path, relative_path, text, status, reason
):
    config = tmp_path / relative_path
    if text is not None:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = correction.check_correction_current_config()

    assert r.status == status
    assert r.reason == reason


def test_check_correction_current_config_warns_on_an_unreadable_statefile(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    r = correction.check_correction_current_config()
    assert r.status == "warn"
    assert r.reason == correction.REASON_CAMILLA_STATEFILE_UNREADABLE


# ---------- correction bundles


def test_check_correction_latest_bundle_warns_without_calibration(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "abc"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
            "started_at": 1000,
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        {"bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION},
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="tests.test_doctor",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = correction.check_correction_latest_bundle()

    assert r.status == "warn"
    assert r.reason == correction.REASON_LATEST_BUNDLE_UNCALIBRATED_MIC


def test_check_correction_latest_bundle_warns_on_bundle_issues(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "failed"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "failed",
            "state": "failed",
            "started_at": 1000,
            "error": "analysis failed: capture clipped",
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = correction.check_correction_latest_bundle()

    assert r.status == "warn"
    assert r.reason == correction.REASON_LATEST_BUNDLE_ISSUES


def test_check_correction_latest_bundle_ok_on_a_golden_collection(
    monkeypatch,
    tmp_path,
):
    """The collection's own counts and sizes are pinned at their owner
    (tests/test_correction_bundles.py); here only the verdict is."""
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "old", started_at=1000)
    write_golden_correction_bundle(sessions, "new", started_at=2000)
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = correction.check_correction_latest_bundle()

    assert r.status == "ok"
    assert r.reason == ""


def test_check_correction_latest_bundle_warns_when_the_walk_was_capped(
    monkeypatch,
    tmp_path,
):
    """A capped walk makes the storage totals a lower bound, so the row says
    so rather than reporting the undercount as fact."""
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "new", started_at=2000)
    monkeypatch.setattr(bundles, "BUNDLE_WALK_MAX_ENTRIES", 1)
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = correction.check_correction_latest_bundle()

    assert r.status == "warn"
    assert r.reason == correction.REASON_LATEST_BUNDLE_SUMMARY_TRUNCATED


def test_check_correction_latest_bundle_ok_when_none_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(tmp_path / "sessions"))
    r = correction.check_correction_latest_bundle()
    assert r.status == "ok"
    assert r.reason == correction.REASON_LATEST_BUNDLE_NONE


# ---------- crossover v2 cloud pipeline


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


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (None, "ok", correction.REASON_CLOUD_NOT_RUN),
        ({"cloud": {}}, "ok", correction.REASON_CLOUD_NOT_RUN),
        # Only cloud_verify (the post-apply, household-actionable grade) gates
        # the warn: cloud_measure is the uncorrected pre-apply baseline, and
        # gating on it warns forever on a perfectly corrected speaker.
        (
            {"cloud": {
                "cloud_measure": _cloud_group(passed=True, locked=True,
                                              excluded=[[8000.0, 9000.0]]),
                "cloud_verify": _cloud_group(passed=False),
            }},
            "warn", correction.REASON_CLOUD_VERIFY_SPEC_FAILED,
        ),
        (
            {"cloud": {
                "cloud_measure": _cloud_group(passed=False,
                                              excluded=[[8000.0, 9000.0]]),
                "cloud_verify": _cloud_group(passed=True),
            }},
            "ok", "",
        ),
        ({"cloud": {"cloud_measure": _cloud_group(passed=True)}}, "ok", ""),
        # A closed group whose pipeline never became available is not itself a
        # spec failure.
        (
            {"cloud": {
                "cloud_measure": _cloud_group_unavailable(reason="combine_failed"),
            }},
            "ok", "",
        ),
    ],
    ids=[
        "never-run", "no-groups", "verify-failed", "pre-apply-failed-only",
        "all-passing", "pipeline-unavailable",
    ],
)
def test_check_crossover_v2_cloud_pipeline_verdicts(
    monkeypatch, state, status, reason
):
    _patch_v2_state(monkeypatch, state)

    r = correction.check_crossover_v2_cloud_pipeline()

    assert r.status == status
    assert r.reason == reason


# ---------- crossover v2: is the applied profile graded?


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
    "overrides, status, reason",
    [
        # Nothing applied: never a manufactured finding.
        pytest.param(
            {"applied": False}, "ok",
            correction.REASON_APPLIED_GRADE_NOT_APPLIED, id="not-applied",
        ),
        # Applied with no post-apply group and no VERIFY outcome — the silence
        # `check_crossover_v2_cloud_pipeline` structurally cannot see.
        pytest.param(
            {}, "ok", correction.REASON_APPLIED_GRADE_NEVER_GRADED,
            id="never-graded",
        ),
        # Express tier omits the post-apply position group, so a passing VERIFY
        # outcome is the whole grade and satisfies the check on its own.
        pytest.param(
            {"verify": {"outcome": "pass"}}, "ok", "", id="verify-alone",
        ),
        pytest.param(
            {"verify": {"outcome": "inconclusive"}}, "ok",
            correction.REASON_APPLIED_GRADE_VERIFY_INCONCLUSIVE,
            id="verify-inconclusive",
        ),
        # #2160: a grade that EXISTS is not a grade that PASSED. This printed
        # "applied and graded" beside a cloud line reading spec=fail.
        pytest.param(
            {
                "tier": "full",
                "verify": {"outcome": "pass"},
                "cloud": _verify_cloud(passed=False, flatness=_FAILED_GAUGE),
            },
            "ok", correction.REASON_APPLIED_GRADE_SPATIAL_FAILED,
            id="spatial-failed",
        ),
        # passed=False with evaluable=False means "could not be measured", not
        # "failed" — SpecFlatness.passed's own read-it-with-evaluable rule.
        pytest.param(
            {
                "tier": "full",
                "verify": {"outcome": "pass"},
                "cloud": _verify_cloud(passed=False, flatness=_UNMEASURABLE_GAUGE),
            },
            "ok", correction.REASON_APPLIED_GRADE_SPATIAL_UNMEASURABLE,
            id="spatial-unmeasurable",
        ),
        # #2098: a Full session verified only at the mark is not the claim Full
        # promised.
        pytest.param(
            {"tier": "full", "verify": {"outcome": "pass"}}, "ok",
            correction.REASON_APPLIED_GRADE_MARK_ONLY, id="full-mark-only",
        ),
        # A group that closed but could not combine reaches the same arm — the
        # wording claims delivered evidence only, never "never closed".
        pytest.param(
            {
                "tier": "full",
                "verify": {"outcome": "pass"},
                "cloud": {
                    "cloud_verify": _cloud_group_unavailable(
                        reason="combine_failed"
                    ),
                },
            },
            "ok", correction.REASON_APPLIED_GRADE_MARK_ONLY,
            id="full-closed-but-unavailable",
        ),
        # The mark IS express's whole promise; a finding here would fire on
        # every express session ever run.
        pytest.param(
            {"tier": "express", "verify": {"outcome": "pass"}}, "ok", "",
            id="express-mark",
        ),
        pytest.param(
            {
                "tier": "full",
                "verify": {"outcome": "pass"},
                "cloud": _verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            },
            "ok", "", id="spatial-passed",
        ),
        # #2464: a failed mark-VERIFY names verify_failed whatever the
        # spatial group says.
        pytest.param(
            {
                "tier": "full",
                "verify": {
                    "outcome": "fail",
                    "claims": {"integration": {"status": "fail", "max_db": 4.2}},
                },
                "cloud": _verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            },
            "ok", correction.REASON_APPLIED_GRADE_VERIFY_FAILED,
            id="verify-failed-behind-a-passing-group",
        ),
        # The capture was clean and the crossover-region claim missed its
        # tolerance: verify.outcome grades capture health alone, so the claims
        # record is what sees it.
        pytest.param(
            {
                "tier": "full",
                "verify": {
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "fail", "max_db": 4.31},
                    },
                },
                "cloud": _verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            },
            "ok", correction.REASON_APPLIED_GRADE_VERIFY_FAILED,
            id="failed-absolute-claim",
        ),
        pytest.param(
            {
                "tier": "full",
                "verify": {"outcome": "inconclusive"},
                "cloud": _verify_cloud(passed=False, flatness=_FAILED_GAUGE),
            },
            "ok", correction.REASON_APPLIED_GRADE_VERIFY_INCONCLUSIVE,
            id="inconclusive-behind-a-closed-group",
        ),
        # The result code is DISCLOSED beside the grade and never gates it:
        # this check grades the CHECKING, which passed completely here, while
        # the household badge honestly reads "Keep the previous sound."
        pytest.param(
            {
                "tier": "full",
                "verify": {
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {"status": "pass", "max_db": 0.8},
                    },
                },
                "verify_priors": {
                    "predicted_spec": {
                        "comparison": {
                            "reason": "not_an_improvement",
                            "improvement_db": 0.1,
                            "required_db": 0.5,
                        },
                    },
                },
                "cloud": _verify_cloud(passed=True, flatness=_PASSING_GAUGE),
            },
            "ok", "", id="keep-previous-result-does-not-gate",
        ),
        # The same posture one tier over: every instrument that grades the
        # CHECKING passed, and the result code is `inconclusive` anyway because
        # the crossover region carried no spec tolerance for an absolute
        # verdict. A finding here would fire on a healthy commission.
        pytest.param(
            {
                "tier": "express",
                "verify": {
                    "outcome": "pass",
                    "claims": {
                        "integration": {"status": "pass", "max_db": 0.7},
                        "absolute": {
                            "status": "not_evaluated",
                            "reason": "no_spec_tolerance_for_region",
                        },
                    },
                },
            },
            "ok", "", id="express-inconclusive-result-does-not-gate",
        ),
    ],
)
def test_check_crossover_v2_applied_is_graded_verdicts(
    monkeypatch, overrides, status, reason
):
    _patch_v2_state(monkeypatch, _v2_applied_state(**overrides))

    r = correction.check_crossover_v2_applied_is_graded()

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

    r = correction.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert r.reason == correction.REASON_APPLIED_GRADE_SPATIAL_UNRECOGNIZED


def test_grade_spatial_and_scope_member_sets_are_pinned_for_their_consumers():
    """Walking-class guard (S1, #2242). ``GRADE_SPATIAL_*`` and ``GRADE_SCOPE_*``
    are consumed by literal membership tests in more than one surface — this
    doctor check and the envelope's done-screen branches
    (``jasper/active_speaker/crossover_envelope_v2.py``, which spells the same
    words as raw literals because it never imports ``jasper.web``). Neither
    iterates the producer's vocabulary, so a NEW member is invisible to both
    until someone teaches each dispatch site the new word by hand.

    If this fails because you added a member: teach
    ``check_crossover_v2_applied_is_graded`` and the done-screen branches the
    new word (or confirm the existing fallthrough is what you want), then
    extend the pinned sets below.
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
        v2host.GRADE_SCOPE_SPATIAL,
    }


# ---------- measurement hold + unresolved session volume


def _patch_measurement(monkeypatch, hold=None, error=None):
    """Point check_measurement_hold's control read at a scripted answer."""
    from jasper.control import client as control_client

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
    from jasper.control import client as control_client

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


# ---------- /correction/ TLS cert
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


def test_cert_check_warns_when_the_san_cannot_be_read(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path)
    _write_identity_env(tmp_path, monkeypatch, avahi="jts3.local")

    with patch("subprocess.run", side_effect=FileNotFoundError("openssl")):
        r = correction.check_correction_cert_hostname()

    assert r.status == "warn"
    assert r.reason == correction.REASON_CERT_SAN_UNREADABLE
