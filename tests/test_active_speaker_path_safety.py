# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jasper.active_speaker import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    ActiveSpeakerConfigError,
    build_startup_load_path_safety_evidence,
    evaluate_path_safety_evidence,
    requirements_payload,
    write_path_safety_evidence,
)
from jasper.active_speaker.calibration_level import calibration_level_payload
from jasper.active_speaker.path_safety import _startup_muted_by_candidate
from jasper.active_speaker.staging import stage_protected_startup_config
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)


def test_path_safety_writer_preserves_private_mode(tmp_path: Path) -> None:
    path = write_path_safety_evidence(
        {"safe": True},
        path=tmp_path / "evidence.json",
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert json.loads(path.read_text()) == {"safe": True}


def _topology(*, identity_verified: bool = True) -> OutputTopology:
    return mono_output_topology(identity_verified=identity_verified)


def _staged(tmp_path: Path) -> dict:
    return stage_protected_startup_config(
        _topology(),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )


def _passing_evidence() -> dict:
    paths = {}
    for requirement in requirements_payload()["requirements"]:
        paths[requirement["id"]] = {
            check: True for check in requirement["checks"]
        }
    return {
        "artifact_schema_version": 1,
        "kind": PATH_SAFETY_EVIDENCE_KIND,
        "evidence_source": OPERATOR_EVIDENCE_SOURCE,
        "paths": paths,
    }


def test_requirements_payload_lists_required_paths():
    payload = requirements_payload()

    ids = {item["id"] for item in payload["requirements"]}
    assert "music_renderers" in ids
    assert "tts_cues" in ids
    assert "correction_sweeps" in ids
    assert "rollback_configs" in ids
    assert payload["kind"] == "jts_active_speaker_path_safety_requirements"


def test_path_safety_evidence_passes_when_every_required_check_is_true():
    report = evaluate_path_safety_evidence(_passing_evidence())

    assert report["status"] == "pass"
    assert report["requirements_met"] is True
    assert report["hardware_probe_backed"] is False
    assert report["ok_to_load_active_config"] is False
    assert report["load_gate"] == "hardware_probe_required"
    assert report["blocker_count"] == 0


def test_path_safety_allows_load_only_for_probe_backed_passing_evidence():
    evidence = _passing_evidence()
    evidence["evidence_source"] = HARDWARE_PROBE_EVIDENCE_SOURCE

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "pass"
    assert report["requirements_met"] is True
    assert report["hardware_probe_backed"] is True
    assert report["ok_to_load_active_config"] is True
    assert report["load_gate"] == "ready"


def test_path_safety_blocks_missing_required_path():
    evidence = _passing_evidence()
    del evidence["paths"]["tts_cues"]

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert report["ok_to_load_active_config"] is False
    assert any(issue["code"] == "missing_path_evidence" for issue in report["issues"])


def test_path_safety_blocks_false_required_check():
    evidence = _passing_evidence()
    evidence["paths"]["correction_sweeps"]["level_controlled"] = False

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "level_controlled_not_verified"
        for issue in report["issues"]
    )


def test_path_safety_blocks_missing_required_check_without_schema_error():
    evidence = _passing_evidence()
    del evidence["paths"]["test_tones"]["level_controlled"]

    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "level_controlled_missing"
        for issue in report["issues"]
    )


def test_path_safety_rejects_unknown_evidence_source():
    evidence = _passing_evidence()
    evidence["evidence_source"] = "trust_me"

    with pytest.raises(ActiveSpeakerConfigError, match="evidence source"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_requires_evidence_source():
    evidence = _passing_evidence()
    del evidence["evidence_source"]

    with pytest.raises(ActiveSpeakerConfigError, match="evidence source is required"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_rejects_non_boolean_check():
    evidence = _passing_evidence()
    evidence["paths"]["music_renderers"]["route_verified"] = "yes"

    with pytest.raises(ActiveSpeakerConfigError, match="must be boolean"):
        evaluate_path_safety_evidence(evidence)


def test_path_safety_rejects_missing_schema_metadata():
    evidence = _passing_evidence()
    del evidence["artifact_schema_version"]

    with pytest.raises(ActiveSpeakerConfigError, match="schema version"):
        evaluate_path_safety_evidence(evidence)

    evidence = _passing_evidence()
    del evidence["kind"]

    with pytest.raises(ActiveSpeakerConfigError, match="kind"):
        evaluate_path_safety_evidence(evidence)


def test_startup_load_path_probe_passes_with_protected_rollback(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
        generated_at="2026-06-04T12:00:00Z",
    )

    report = evaluate_path_safety_evidence(evidence)

    assert evidence["evidence_source"] == HARDWARE_PROBE_EVIDENCE_SOURCE
    assert evidence["evidence_mode"] == "startup_load_preflight"
    assert evidence["scope"] == "load_only_no_audio"
    assert report["ok_to_load_active_config"] is True
    assert report["load_gate"] == "ready"


def test_startup_load_path_probe_has_explicit_identity_audition_mode(
    tmp_path: Path,
) -> None:
    topology = _topology(identity_verified=False)
    staged = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-04T12:00:00Z",
    )

    strict = build_startup_load_path_safety_evidence(
        topology,
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
        generated_at="2026-06-04T12:00:00Z",
    )
    strict_report = evaluate_path_safety_evidence(strict)

    assert strict_report["load_gate"] == "requirements_blocked"
    assert "physical_identity_unverified" in {
        issue["code"] for issue in strict["observed_issues"]
    }

    audition = build_startup_load_path_safety_evidence(
        topology,
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
        generated_at="2026-06-04T12:00:00Z",
        require_physical_identity=False,
    )
    audition_report = evaluate_path_safety_evidence(audition)

    assert audition["evidence_mode"] == "identity_audition_startup_load"
    assert audition["scope"] == "identity_audition_load_only_no_audio"
    assert audition["provenance"]["physical_identity_required"] is False
    assert audition_report["ok_to_load_active_config"] is True
    assert audition_report["load_gate"] == "ready"


def test_startup_load_path_probe_allows_bounded_normal_rollback_target(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    prior = tmp_path / "prior_stereo.yml"
    prior.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "devices:\n"
        "  volume_limit: 0\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: outputd_content_playback\n"
        "    channels: 2\n",
        encoding="utf-8",
    )

    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=prior,
    )
    report = evaluate_path_safety_evidence(evidence)

    assert report["status"] == "pass"
    assert report["ok_to_load_active_config"] is True
    rollback = evidence["paths"]["rollback_configs"]
    assert rollback["rollback_target_available"] is True
    assert rollback["rollback_target_restore_limited"] is True
    assert rollback["rollback_target_protected"] is False
    assert any(
        issue["code"] == "rollback_target_restores_previous_profile"
        for issue in evidence["observed_issues"]
    )


def test_write_path_safety_evidence_persists_probe_payload(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=staged["config"]["path"],
    )

    path = write_path_safety_evidence(evidence, path=tmp_path / "path_safety.json")

    assert path.read_text(encoding="utf-8").startswith("{\n")


def test_startup_muted_prefers_fully_muted_gate_over_text_scan(tmp_path: Path) -> None:
    # _startup_muted_by_candidate prefers the precise staged_candidate_fully_muted
    # gate (every per-output mute -120 dB AND wired) over the coarse text scan.
    payload = _staged(tmp_path)
    assert payload["status"] == "staged"
    gate = next(
        g for g in payload["required_gates"]
        if g["id"] == "staged_candidate_fully_muted"
    )
    assert gate["passed"] is True
    assert _startup_muted_by_candidate(payload) is True

    # If the gate reports NOT fully muted, the precise gate must win even though
    # the on-disk config (and the loose text scan) still shows other muted
    # outputs — the looseness the text fallback could not catch.
    for g in payload["required_gates"]:
        if g["id"] == "staged_candidate_fully_muted":
            g["passed"] = False
    assert _startup_muted_by_candidate(payload) is False


# --- #2135 blocker F1: a PARKED graph is a legitimate rollback target ---------
# Excluding the parked classification from `restore_classifications` made
# `rollback_target_available` false, which fails the `rollback_configs`
# requirement, which blocks `evaluate_path_safety_evidence`, which makes
# `/sound/setup/`'s commission-startup anchor return
# `commission_startup_anchor_path_safety_blocked`. Net effect: a parked box
# could not START commissioning — the first of the two exits parking tells the
# household to take was itself refused.


def _parked_config(tmp_path: Path) -> Path:
    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config

    path = tmp_path / "active_speaker_parked.yml"
    path.write_text(emit_active_speaker_parked_config(output_count=2), encoding="utf-8")
    return path


def test_parked_graph_is_an_accepted_rollback_target(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    parked = _parked_config(tmp_path)

    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=parked,
    )
    report = evaluate_path_safety_evidence(evidence)

    rollback = evidence["paths"]["rollback_configs"]
    assert rollback["rollback_target_available"] is True
    # Rolling back to proven silence is a restore, not a protected graph: the
    # parked graph carries no driver protection because it drives no driver.
    assert rollback["rollback_target_protected"] is False
    assert report["ok_to_load_active_config"] is True
    assert report["load_gate"] == "ready"


def test_parked_rollback_target_reaches_the_commission_startup_anchor(
    tmp_path: Path,
) -> None:
    """The whole chain, end to end: parked current config -> anchor not blocked.

    `/sound/setup/`'s `_active_speaker_ensure_commission_startup_anchor` returns
    `commission_startup_anchor_path_safety_blocked` whenever
    `evaluate_path_safety_evidence` raises. This walks the same two calls it
    makes, so a regression anywhere between the restore set and the load gate
    closes the "finish crossover preview" exit again and fails here.
    """
    staged = _staged(tmp_path)
    parked = _parked_config(tmp_path)

    evidence = build_startup_load_path_safety_evidence(
        _topology(),
        staged_config=staged,
        calibration_level=calibration_level_payload(),
        current_config_path=parked,
    )
    report = evaluate_path_safety_evidence(evidence)  # must NOT raise

    assert report["status"] == "pass"
    assert report["load_gate"] == "ready"
    assert not [
        issue
        for issue in evidence["observed_issues"]
        if issue.get("severity") == "blocker"
    ]


# --- D4 (wide-output-path program): parked graph's pipe format is pinned -----


def test_parked_config_format_stays_pinned_regardless_of_playback_format():
    """The parked graph's /dev/null File sink always emits
    DEFAULT_PIPE_SINK_FORMAT, decoupled from playback_format — mirrors the
    bonded-leader pipe sink in jasper.sound.camilla_yaml.emit_sound_config.
    No monkeypatch needed — the guard is a pure ``is not None`` presence
    check now (SF1 fix: the old ``!= DEFAULT_PLAYBACK_FORMAT`` comparison
    mixed a live global with a def-time-bound default and could diverge)."""
    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config

    yaml = emit_active_speaker_parked_config(output_count=2)
    # Scope the assertion to the playback BLOCK — the capture block legitimately
    # emits format: S32_LE always (DEFAULT_CAPTURE_FORMAT, unrelated to this
    # axis), so a whole-document substring check would false-pass here.
    playback_block = yaml.split("playback:", 1)[1]
    assert "type: File" in playback_block
    assert "format: S16_LE" in playback_block


def test_parked_config_with_explicit_playback_format_raises_regardless_of_value():
    """The ValueError case: playback_format is not applicable to this
    /dev/null File sink at all, so passing it EXPLICITLY — even a value that
    happens to equal today's DEFAULT_PLAYBACK_FORMAT — is refused. Genuinely
    a presence check (``is not None``), not a "does it match the current
    default" check."""
    import pytest

    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    with pytest.raises(ActiveSpeakerConfigError, match="different axes"):
        emit_active_speaker_parked_config(
            output_count=2,
            playback_format="S16_LE",  # == today's DEFAULT_PLAYBACK_FORMAT —
            # still refused: the guard is presence-based, not value-based.
        )


def test_parked_config_omitted_never_raises_even_after_the_default_rebinds(
    monkeypatch,
):
    """SF1 regression (PR-1 gate review): the bug this reproduces — the
    parked emitter's ONLY production caller
    (jasper.active_speaker.runtime_contract) never passes playback_format, so
    this call shape must NEVER raise, even if DEFAULT_PLAYBACK_FORMAT is
    rebound after this module was imported (the exact scenario a
    ``!= DEFAULT_PLAYBACK_FORMAT`` body-level comparison gets wrong — it
    compares a LIVE global against playback_format's def-time-bound default,
    two different bindings the moment they diverge). Before this fix that
    divergence surfaced as ``parked_graph_emit_failed`` — a box that cannot
    park. The sentinel (``str | None = None``) guard sidesteps this
    unconditionally."""
    import jasper.active_speaker.camilla_yaml as camilla_yaml

    monkeypatch.setattr(camilla_yaml, "DEFAULT_PLAYBACK_FORMAT", "S32_LE")
    yaml = camilla_yaml.emit_active_speaker_parked_config(
        output_count=2
        # playback_format deliberately omitted — the real calling convention.
    )
    playback_block = yaml.split("playback:", 1)[1]
    assert "format: S16_LE" in playback_block  # DEFAULT_PIPE_SINK_FORMAT, untouched
