# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import errno
import json
import logging
import stat
from pathlib import Path

import pytest
import yaml

from jasper.active_speaker import bundles
from jasper.active_speaker.camilla_yaml import (
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB,
    audible_outputs_for_role,
    emit_active_speaker_commissioning_config,
)
from jasper.active_speaker.commissioning_admission import (
    ActiveCommissioningAdmissionError,
    MAX_AUTOMATIC_DRIVER_COOLDOWN_S,
    issue_protection_evidence,
    play_admitted_driver_capture,
    prepare_capture_plan,
    running_graph_fingerprint,
    validate_capture_admission_handoff,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.staging import driver_commission_audible_evidence
from jasper.audio_measurement.admitted_playback import AdmittedPlaybackResult
from jasper.audio_measurement.excitation_artifacts import (
    readmit_and_persist_playback_admission,
    readmit_excitation_for_playback,
)
from jasper.audio_measurement.playback import PlaybackResult
from jasper.camilla_config_contract import RETIRED_ALOOP_CAPTURE_DEVICE
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE, RING_CAPTURE_DEVICE
from tests.test_active_speaker_excitation_safety_plan import _profile_and_targets
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_web_commissioning import _driver_comparison_set


def _context(
    tmp_path: Path, monkeypatch, *, woofer_required_filters: list | None = None
):
    topology, profile, targets = _profile_and_targets(
        woofer_required_filters=woofer_required_filters
    )
    opened = bundles.open_bundle(
        topology,
        calibration_id="mic-1",
        sessions_dir=tmp_path,
        now=1.0,
    )
    assert opened is not None
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path))
    comparison = _driver_comparison_set(topology)
    comparison["bundle_session_id"] = opened["session_id"]
    applied = {"baseline_id": "baseline-1", "topology_id": topology.topology_id}
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    audible = audible_outputs_for_role(preset, "woofer")
    raw = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs=audible,
        audible_gain_db=-50.0,
        volume_limit_db=-4.0,
        startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        filter_mode=APPLIED_RESPONSE_FILTER_MODE,
    )
    intent = driver_commission_audible_evidence(
        raw,
        preset=preset,
        audible_outputs=audible,
        filter_mode=APPLIED_RESPONSE_FILTER_MODE,
    )
    load_payload = {
        "preflight": {"audible_evidence": intent},
        "load": {
            "status": "loaded",
            "target": {"speaker_group_id": "mono", "role": "woofer"},
        },
    }
    return topology, profile, targets, comparison, applied, raw, load_payload


def test_prepare_capture_plan_refuses_historical_comparison(tmp_path, monkeypatch):
    topology, profile, _targets, comparison, applied, _raw, _load = _context(
        tmp_path, monkeypatch
    )
    comparison.pop("bundle_session_id")
    with pytest.raises(ActiveCommissioningAdmissionError, match="predates"):
        prepare_capture_plan(
            topology,
            profile,
            comparison,
            applied,
            speaker_group_id="mono",
            role="woofer",
            commissioning_gain_db=-50.0,
            expected_main_volume_db=-4.0,
            expected_graph_fingerprint=running_graph_fingerprint(_raw),
        )


def test_live_proof_binds_required_filter_and_volume(tmp_path, monkeypatch):
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=raw,
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert evidence.current is True
    assert report["passed"] is True

    ceiling_drift = yaml.safe_load(raw)
    ceiling_drift["devices"]["volume_limit"] = 0.0
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=yaml.safe_dump(ceiling_drift),
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert evidence.current is False
    assert report["checks"]["graph_volume_ceiling"] is False

    drifted = raw.replace("LinkwitzRileyLowpass", "LinkwitzRileyHighpass")
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=drifted,
        observed_main_volume_db=-3.5,
        expected_main_volume_db=-4.0,
    )
    assert evidence.current is False
    assert report["checks"]["required_filters_present"] is False
    assert report["checks"]["main_volume_current"] is False

    gain_drift = yaml.safe_load(raw)
    gain_drift["filters"]["as_out0_commission_mute"]["parameters"]["gain"] = 0.0
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=yaml.safe_dump(gain_drift),
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert evidence.current is False
    assert report["checks"]["target_commissioning_gain_current"] is False
    assert report["observed_target_commissioning_gain_db"] == 0.0
    assert report["expected_target_commissioning_gain_db"] == -50.0


def test_required_filters_present_vacuous_when_none_declared(tmp_path, monkeypatch):
    """JTS3 hardware repro (runs 6-7): woofer legitimately declares zero
    required protection filters — protective high-passes are tweeter
    machinery; the woofer's protection is the limiter/headroom/mask checks
    in this same report, computed independently and unaffected here. Before
    the fix, ``bool(filter_checks) and all(filter_checks)`` on an empty list
    turned "no requirements declared" into a hard refusal
    (``required_filters_present=False``, named by
    ``failed_checks=required_filters_present`` with ``filter_checks=[]``)
    on every woofer sweep. This must fail against pre-fix code.
    """
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch, woofer_required_filters=[]
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=raw,
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert report["filter_checks"] == []
    assert report["checks"]["required_filters_present"] is True
    assert evidence.current is True
    assert report["passed"] is True


def test_required_filters_present_false_when_declared_filter_missing(
    tmp_path, monkeypatch
):
    """Requirements declared and one is absent from the live graph: still
    a hard refusal (unchanged all-present/any-missing semantics)."""

    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    drifted = raw.replace("LinkwitzRileyLowpass", "LinkwitzRileyHighpass")
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=drifted,
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert report["filter_checks"] == [False]
    assert report["checks"]["required_filters_present"] is False
    assert evidence.current is False


def test_required_filters_present_false_when_output_index_unresolved(
    tmp_path, monkeypatch
):
    """Requirements declared but the target's output index cannot be
    resolved: fail closed — requirements are declared but unverifiable."""

    import jasper.active_speaker.commissioning_admission as admission_module

    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    real_targets = admission_module.active_driver_targets(topology)
    unresolved = [
        dict(target, output_index=None)
        if target["target_id"] == prepared.target_id
        else target
        for target in real_targets
    ]
    monkeypatch.setattr(
        admission_module, "active_driver_targets", lambda _topology: unresolved
    )
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=raw,
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert report["filter_checks"] == []
    assert report["checks"]["required_filters_present"] is False
    assert evidence.current is False


def test_required_filters_present_true_when_all_declared_filters_present(
    tmp_path, monkeypatch
):
    """Requirements declared and all present: passes (baseline shape)."""

    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=raw,
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    assert report["filter_checks"] == [True]
    assert report["checks"]["required_filters_present"] is True
    assert evidence.current is True
    assert report["passed"] is True


def test_prepare_capture_plan_refuses_unbounded_cooldown(tmp_path, monkeypatch):
    topology, profile, _targets = _profile_and_targets(
        cooldown_s=MAX_AUTOMATIC_DRIVER_COOLDOWN_S + 1.0
    )
    opened = bundles.open_bundle(
        topology,
        calibration_id="mic-1",
        sessions_dir=tmp_path,
        now=1.0,
    )
    assert opened is not None
    comparison = _driver_comparison_set(topology)
    comparison["bundle_session_id"] = opened["session_id"]
    applied = {"baseline_id": "baseline-1", "topology_id": topology.topology_id}
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    raw = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs=audible_outputs_for_role(preset, "woofer"),
        audible_gain_db=-50.0,
        startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        filter_mode=APPLIED_RESPONSE_FILTER_MODE,
    )

    with pytest.raises(ActiveCommissioningAdmissionError, match="bounded"):
        prepare_capture_plan(
            topology,
            profile,
            comparison,
            applied,
            speaker_group_id="mono",
            role="woofer",
            commissioning_gain_db=-50.0,
            expected_main_volume_db=-4.0,
            expected_graph_fingerprint=running_graph_fingerprint(raw),
        )


def test_automatic_cooldown_ceiling_fits_capture_recording_budget():
    from jasper.active_speaker.commissioning_admission import (
        ACTIVE_DRIVER_CAPTURE_GRAPH_AND_STATUS_BUDGET_S,
    )
    from jasper.active_speaker.test_signal_plan import (
        CROSSOVER_AMBIENT_DURATION_S,
        CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
        DRIVER_SWEEP_DURATIONS_S,
    )

    occupied_s = (
        MAX_AUTOMATIC_DRIVER_COOLDOWN_S
        + CROSSOVER_AMBIENT_DURATION_S
        + max(DRIVER_SWEEP_DURATIONS_S.values())
        + ACTIVE_DRIVER_CAPTURE_GRAPH_AND_STATUS_BUDGET_S
    )
    assert MAX_AUTOMATIC_DRIVER_COOLDOWN_S == 5.0
    assert occupied_s <= CROSSOVER_CAPTURE_PLAY_DEADLINE_S


def test_driver_attempt_persists_unique_generation_playback_and_wav(
    tmp_path, monkeypatch
):
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )

    async def fake_play(
        stimulus_bundle_dir,
        *,
        stimulus,
        authority,
        generation,
        issue_current_inputs,
        alsa_device,
        timeout_s,
    ):
        del timeout_s
        current = await issue_current_inputs()
        result = readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        assert result.artifact is not None
        assert Path(stimulus_bundle_dir, stimulus.artifact.relative_path).is_file()
        return AdmittedPlaybackResult(
            playback=PlaybackResult(
                wav_path=Path(stimulus_bundle_dir, stimulus.artifact.relative_path),
                alsa_device=alsa_device,
                returncode=0,
            ),
            admission=result.artifact,
        )

    import jasper.active_speaker.commissioning_admission as admission_module

    monkeypatch.setattr(admission_module, "play_admitted_wav", fake_play)
    cooldowns = []

    async def fake_sleep(delay_s):
        cooldowns.append(delay_s)

    monkeypatch.setattr(admission_module.asyncio, "sleep", fake_sleep)

    async def read_running():
        return raw

    async def read_volume():
        return -4.0

    def current_context():
        return topology, profile, comparison, applied

    async def run_once():
        return await play_admitted_driver_capture(
            topology=topology,
            safety_profile=profile,
            comparison_set=comparison,
            applied_profile=applied,
            speaker_group_id="mono",
            role="woofer",
            commissioning_gain_db=-50.0,
            expected_main_volume_db=-4.0,
            load_payload=load_payload,
            read_running_config=read_running,
            read_main_volume_db=read_volume,
            load_current_context=current_context,
            alsa_device="correction_substream",
            timeout_margin_s=9.0,
        )

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first.handoff.admission_id != second.handoff.admission_id
    assert first.handoff.generation_artifact != second.handoff.generation_artifact
    assert first.handoff.playback_artifact != second.handoff.playback_artifact
    assert first.handoff.stimulus.artifact != second.handoff.stimulus.artifact
    assert cooldowns == [1.0, 1.0]
    assert validate_capture_admission_handoff(
        first.handoff.to_dict(),
        topology=topology,
        comparison_set=comparison,
        speaker_group_id="mono",
        role="woofer",
    ) == first.handoff.to_dict()

    stimulus_path = (
        tmp_path
        / first.handoff.session_id
        / first.handoff.stimulus.artifact.relative_path
    )
    stimulus_path.write_bytes(b"tampered")
    with pytest.raises(ActiveCommissioningAdmissionError, match="identity changed"):
        validate_capture_admission_handoff(
            first.handoff.to_dict(),
            topology=topology,
            comparison_set=comparison,
            speaker_group_id="mono",
            role="woofer",
        )


def test_driver_capture_playback_timeout_derives_from_sweep_duration(
    tmp_path, monkeypatch
):
    """The aplay deadman must scale with the realized stimulus duration.

    Hardware evidence (JTS3 punch #27): a subwoofer/woofer sweep measured
    574,520 frames @ 48 kHz = 11.969 s while the caller passed a hardcoded
    ``timeout_s=12.0`` -- ~31 ms (or negative) margin for aplay spawn + ALSA
    open + EOF drain, so playback always timed out. Reproduced in-repo: the
    same duration_approx_s=12.0 synchronized-sweep search
    (``jasper.audio_measurement.sweep.synchronized_sweep_metadata``) lands at
    ~11.97 s for a low band (f1=20, f2=200) -- the same shape, because the
    kernel's phase-closure rounding can land on either side of the request.

    A budget derived from the artifact's own ``SweepMeta.duration_s`` (like
    the sibling call site at ``play_sweep``) cannot
    go negative-margin this way; a hardcoded literal decoupled from the
    generated duration always can.
    """
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    seen_timeout: dict[str, float] = {}

    async def fake_play(
        stimulus_bundle_dir,
        *,
        stimulus,
        authority,
        generation,
        issue_current_inputs,
        alsa_device,
        timeout_s,
    ):
        seen_timeout["value"] = timeout_s
        current = await issue_current_inputs()
        result = readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        return AdmittedPlaybackResult(
            playback=PlaybackResult(
                wav_path=Path(stimulus_bundle_dir, stimulus.artifact.relative_path),
                alsa_device=alsa_device,
                returncode=0,
            ),
            admission=result.artifact,
        )

    import jasper.active_speaker.commissioning_admission as admission_module

    monkeypatch.setattr(admission_module, "play_admitted_wav", fake_play)

    async def read_running():
        return raw

    async def read_volume():
        return -4.0

    def current_context():
        return topology, profile, comparison, applied

    admitted = asyncio.run(
        play_admitted_driver_capture(
            topology=topology,
            safety_profile=profile,
            comparison_set=comparison,
            applied_profile=applied,
            speaker_group_id="mono",
            role="woofer",
            commissioning_gain_db=-50.0,
            expected_main_volume_db=-4.0,
            load_payload=load_payload,
            read_running_config=read_running,
            read_main_volume_db=read_volume,
            load_current_context=current_context,
            alsa_device="correction_substream",
            timeout_margin_s=5.0,
        )
    )

    assert seen_timeout["value"] == pytest.approx(
        admitted.sweep_meta.duration_s + 5.0
    )
    # The bug this guards against: a hardcoded literal (12.0 in production)
    # decoupled from the actual generated duration.
    assert seen_timeout["value"] != 12.0


def test_driver_cooldown_cancellation_never_reaches_playback(tmp_path, monkeypatch):
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    import jasper.active_speaker.commissioning_admission as admission_module

    playback_called = False

    async def cancel_during_cooldown(delay_s):
        assert delay_s == 1.0
        raise asyncio.CancelledError

    async def unexpected_play(*_args, **_kwargs):
        nonlocal playback_called
        playback_called = True
        raise AssertionError("cooldown cancellation must happen before playback")

    monkeypatch.setattr(admission_module.asyncio, "sleep", cancel_during_cooldown)
    monkeypatch.setattr(admission_module, "play_admitted_wav", unexpected_play)

    async def read_running():
        return raw

    async def read_volume():
        return -4.0

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            play_admitted_driver_capture(
                topology=topology,
                safety_profile=profile,
                comparison_set=comparison,
                applied_profile=applied,
                speaker_group_id="mono",
                role="woofer",
                commissioning_gain_db=-50.0,
                expected_main_volume_db=-4.0,
                load_payload=load_payload,
                read_running_config=read_running,
                read_main_volume_db=read_volume,
                load_current_context=lambda: (
                    topology,
                    profile,
                    comparison,
                    applied,
                ),
                alsa_device="correction_substream",
                timeout_margin_s=9.0,
            )
        )
    assert playback_called is False


@pytest.mark.parametrize(
    ("post_read", "expected_reason"),
    [
        (-8.0, "main_volume_drift"),
        ("cancelled", "post_play_volume_verification_cancelled"),
        ("unavailable", "post_play_volume_unverified"),
    ],
)
def test_post_playback_volume_drift_consumes_attempt_without_capture_handoff(
    tmp_path, monkeypatch, post_read, expected_reason
):
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    import jasper.active_speaker.commissioning_admission as admission_module

    async def fake_play(
        stimulus_bundle_dir,
        *,
        stimulus,
        authority,
        generation,
        issue_current_inputs,
        alsa_device,
        timeout_s,
    ):
        del timeout_s
        current = await issue_current_inputs()
        result = readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        assert result.artifact is not None
        return AdmittedPlaybackResult(
            playback=PlaybackResult(
                wav_path=Path(
                    stimulus_bundle_dir, stimulus.artifact.relative_path
                ),
                alsa_device=alsa_device,
                returncode=0,
            ),
            admission=result.artifact,
        )

    async def no_cooldown(_delay_s):
        return None

    monkeypatch.setattr(admission_module, "play_admitted_wav", fake_play)
    monkeypatch.setattr(admission_module.asyncio, "sleep", no_cooldown)
    volume_reads = 0

    async def read_running():
        return raw

    async def read_volume():
        nonlocal volume_reads
        volume_reads += 1
        if volume_reads <= 2:
            return -4.0
        if post_read == "cancelled":
            raise asyncio.CancelledError
        if post_read == "unavailable":
            from jasper.camilla import CamillaUnavailable

            raise CamillaUnavailable("post-play read failed")
        return post_read

    with pytest.raises(
        admission_module.ActiveCommissioningPlaybackDrift,
        match="driver playback",
    ) as caught:
        asyncio.run(
            play_admitted_driver_capture(
                topology=topology,
                safety_profile=profile,
                comparison_set=comparison,
                applied_profile=applied,
                speaker_group_id="mono",
                role="woofer",
                commissioning_gain_db=-50.0,
                expected_main_volume_db=-4.0,
                load_payload=load_payload,
                read_running_config=read_running,
                read_main_volume_db=read_volume,
                load_current_context=lambda: (
                    topology,
                    profile,
                    comparison,
                    applied,
                ),
                alsa_device="correction_substream",
                timeout_margin_s=9.0,
            )
        )
    assert caught.value.audio_may_have_started is True
    assert caught.value.reason == expected_reason
    assert caught.value.admission_id
    assert caught.value.playback_artifact.relative_path.startswith(
        "admission/v1/playback/"
    )
    if post_read == "cancelled":
        assert isinstance(caught.value.__cause__, asyncio.CancelledError)
    if post_read == "unavailable":
        from jasper.camilla import CamillaUnavailable

        assert isinstance(caught.value.__cause__, CamillaUnavailable)


def test_playback_readmission_refuses_context_drift(tmp_path, monkeypatch):
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    import jasper.active_speaker.commissioning_admission as admission_module

    async def inspect_and_refuse(
        *_args, generation, issue_current_inputs, **_kwargs
    ):
        current = await issue_current_inputs()
        decision = readmit_excitation_for_playback(
            generation.admission,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        assert decision.allowed is False
        assert "excitation_plan_identity_mismatch" in {
            reason.value for reason in decision.refusal_reasons
        }
        raise ActiveCommissioningAdmissionError("fresh context refused")

    monkeypatch.setattr(admission_module, "play_admitted_wav", inspect_and_refuse)

    async def read_running():
        return raw

    async def read_volume():
        return -4.0

    stale = dict(comparison)
    stale["bundle_session_id"] = "other-session"

    with pytest.raises(ActiveCommissioningAdmissionError, match="fresh context"):
        asyncio.run(
            play_admitted_driver_capture(
                topology=topology,
                safety_profile=profile,
                comparison_set=comparison,
                applied_profile=applied,
                speaker_group_id="mono",
                role="woofer",
                commissioning_gain_db=-50.0,
                expected_main_volume_db=-4.0,
                load_payload=load_payload,
                read_running_config=read_running,
                read_main_volume_db=read_volume,
                load_current_context=lambda: (topology, profile, stale, applied),
                alsa_device="correction_substream",
                timeout_margin_s=9.0,
            )
        )


_ADMISSION_LOGGER_NAME = "jasper.active_speaker.commissioning_admission"


def test_stale_protection_report_names_failed_checks_and_persists_report(
    tmp_path, monkeypatch, caplog
):
    """Punch #19: a bare 'protection_evidence_stale' refusal named nothing.

    This pins the fix: the refusal now logs the exact failing check name(s)
    and writes the full live protection report into the session bundle
    directory, so the wall no longer requires re-running the sweep to
    diagnose.

    Issue #1820 moved WHERE those names live without weakening the promise.
    ``web_commissioning`` renders this exception's ``str()`` verbatim in a
    ``/sound/`` issue list, so the raw refusal slugs and failing check names
    were one refusal away from the household's screen. They now ride the
    journal line and the exception's ``refusal_codes`` — both asserted below —
    while the MESSAGE is the one sentence an operator can act on.
    """

    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    ceiling_drift = yaml.safe_load(raw)
    ceiling_drift["devices"]["volume_limit"] = 0.0
    drifted_raw = yaml.safe_dump(ceiling_drift)

    async def read_running():
        return drifted_raw

    async def read_volume():
        return -4.0

    with caplog.at_level(logging.INFO, logger=_ADMISSION_LOGGER_NAME):
        with pytest.raises(ActiveCommissioningAdmissionError) as excinfo:
            asyncio.run(
                play_admitted_driver_capture(
                    topology=topology,
                    safety_profile=profile,
                    comparison_set=comparison,
                    applied_profile=applied,
                    speaker_group_id="mono",
                    role="woofer",
                    commissioning_gain_db=-50.0,
                    expected_main_volume_db=-4.0,
                    load_payload=load_payload,
                    read_running_config=read_running,
                    read_main_volume_db=read_volume,
                    load_current_context=lambda: (
                        topology,
                        profile,
                        comparison,
                        applied,
                    ),
                    alsa_device="correction_substream",
                    timeout_margin_s=9.0,
                )
            )

    # The household-facing half: one actionable sentence, no slugs, no check
    # names, no colon-delimited diagnostic tail (this string reaches the DOM).
    # Asserted against the shared constant, not a re-typed literal: the raise
    # site's comment claims this refusal "names the same action" as the sibling
    # PlaybackAdmissionRefused arm, and a hand-copied literal lets that claim go
    # false with a green suite (#1832).
    from jasper.audio_measurement.admitted_playback import (
        PLAYBACK_READMISSION_REFUSED_MESSAGE,
    )

    message = str(excinfo.value)
    assert message == PLAYBACK_READMISSION_REFUSED_MESSAGE
    for jargon in ("protection_evidence_stale", "graph_volume_ceiling", "refused:"):
        assert jargon not in message
    # The forensic half: slugs on the exception for the caller's payload, and
    # both the slug and the failing check names in the journal.
    assert excinfo.value.refusal_codes == ("protection_evidence_stale",)
    assert (
        "event=active_speaker.driver_capture_protection_evidence_stale"
        in caplog.text
    )
    assert "failed_checks=graph_volume_ceiling" in caplog.text
    assert "refusal_codes=protection_evidence_stale" in caplog.text
    assert "failed_protection_checks=graph_volume_ceiling" in caplog.text

    report_path = (
        tmp_path / comparison["bundle_session_id"] / "protection_report.json"
    )
    assert report_path.exists()
    persisted = json.loads(report_path.read_text())
    assert persisted["checks"]["graph_volume_ceiling"] is False
    assert persisted["passed"] is False


def test_successful_admission_does_not_log_or_persist_protection_report(
    tmp_path, monkeypatch, caplog
):
    """No success-path noise: the report is only logged/persisted on failure."""

    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    import jasper.active_speaker.commissioning_admission as admission_module

    async def fake_play(
        stimulus_bundle_dir,
        *,
        stimulus,
        authority,
        generation,
        issue_current_inputs,
        alsa_device,
        timeout_s,
    ):
        del timeout_s
        current = await issue_current_inputs()
        result = readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        return AdmittedPlaybackResult(
            playback=PlaybackResult(
                wav_path=Path(stimulus_bundle_dir, stimulus.artifact.relative_path),
                alsa_device=alsa_device,
                returncode=0,
            ),
            admission=result.artifact,
        )

    monkeypatch.setattr(admission_module, "play_admitted_wav", fake_play)

    async def no_cooldown(_delay_s):
        return None

    monkeypatch.setattr(admission_module.asyncio, "sleep", no_cooldown)

    async def read_running():
        return raw

    async def read_volume():
        return -4.0

    with caplog.at_level(logging.INFO, logger=_ADMISSION_LOGGER_NAME):
        asyncio.run(
            play_admitted_driver_capture(
                topology=topology,
                safety_profile=profile,
                comparison_set=comparison,
                applied_profile=applied,
                speaker_group_id="mono",
                role="woofer",
                commissioning_gain_db=-50.0,
                expected_main_volume_db=-4.0,
                load_payload=load_payload,
                read_running_config=read_running,
                read_main_volume_db=read_volume,
                load_current_context=lambda: (topology, profile, comparison, applied),
                alsa_device="correction_substream",
                timeout_margin_s=9.0,
            )
        )

    assert "protection_evidence_stale" not in caplog.text
    assert "protection_report_persist_failed" not in caplog.text
    report_path = (
        tmp_path / comparison["bundle_session_id"] / "protection_report.json"
    )
    assert not report_path.exists()


# --- capture_route_current: the running graph must read Ring A --------------
#
# ``capture_route_current`` is a PROTECTION check — "the graph CamillaDSP is
# actually running reads the source I expect" — and it derives that expectation
# from the running graph's own playback device
# (``capture_device_for_playback``). Ring A is the one fan-in → CamillaDSP
# transport (ADR-0100), so every sink derives the same source and the SOURCE is
# the whole verdict: a graph still on the retired snd-aloop tap reads a device
# nothing writes and sweeps into digital silence with every daemon healthy.
#
# BOTH SINKS CARRY BOTH POLARITIES, one cell per test, so no verdict rides on an
# argument: {ring sink, ring source} T1 and {DAC sink, ring source} T3 pass;
# {ring sink, tap} T2 and {DAC sink, tap} T5 refuse. T4 drives T2's graph
# through the whole refusal chain, since a check-level verdict that never
# reaches the operator is not a refusal.


def _graph_with_devices(raw: str, *, playback_device: str, capture_device: str) -> str:
    """The emitted commissioning graph, re-pointed at one device pair.

    Surgery on a real emitted config rather than a fresh emit, because the
    subject IS a running graph's declared devices: this builds the exact pairs a
    box can present CamillaDSP — including the incoherent ones no emitter would
    produce, which are precisely what a gate exists to refuse. Everything else
    in the graph is untouched, so any check that flips is attributable to the
    device pair and nothing else.
    """
    graph = yaml.safe_load(raw)
    graph["devices"]["playback"]["device"] = playback_device
    graph["devices"]["capture"]["device"] = capture_device
    return yaml.safe_dump(graph)


def _report_for_devices(
    tmp_path, monkeypatch, *, playback_device: str, capture_device: str
) -> dict:
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    prepared, _meta = prepare_capture_plan(
        topology,
        profile,
        comparison,
        applied,
        speaker_group_id="mono",
        role="woofer",
        commissioning_gain_db=-50.0,
        expected_main_volume_db=-4.0,
        expected_graph_fingerprint=running_graph_fingerprint(raw),
    )
    _evidence, report = issue_protection_evidence(
        topology=topology,
        safety_profile=profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=_graph_with_devices(
            raw, playback_device=playback_device, capture_device=capture_device
        ),
        observed_main_volume_db=-4.0,
        expected_main_volume_db=-4.0,
    )
    return report


def _failed_checks(report) -> set:
    return {name for name, ok in report["checks"].items() if not ok}


def test_capture_route_current_accepts_ring_playback_with_ring_capture(
    tmp_path, monkeypatch
):
    """T1 — the ring's own coherent pair passes.

    Guards the "ring can never pass" regression. The pair is constructed
    SYNTHETICALLY here, and was unreachable when this test was written: the
    then-shipped transport gate refused every ring device, so nothing emitted
    this graph and what this pinned was that the admission gate would not be
    the thing standing in its way. #2412's Wave 3 replaced that refusal with a
    both-ends coherence proof, so a roleful box now emits exactly this pair —
    the synthetic construction stays because this file's subject is the
    admission report over a graph handed to it, not how the graph was built.
    The pair is not arbitrary — it is exactly the one
    ``fanin.coupling_reconcile.ring_endpoint_anchor_converged`` demands of an
    armed roleful box's staged anchor, which is why the arm gate and this one
    used to point in opposite directions on the same box.
    """
    report = _report_for_devices(
        tmp_path,
        monkeypatch,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        capture_device=RING_CAPTURE_DEVICE,
    )
    assert report["checks"]["capture_route_current"] is True
    assert _failed_checks(report) == set()
    assert report["passed"] is True


def test_capture_route_current_refuses_ring_playback_with_aloop_capture(
    tmp_path, monkeypatch
):
    """T2 — the silent-sweep graph, refused.

    A sink on the ring with a source still on the snd-aloop tap: under
    ``shm_ring`` fan-in has stopped feeding that tap, so this graph captures a
    device nobody writes. It sweeps into digital silence with every daemon
    healthy. The constant this derivation replaced accepted it, and no test
    anywhere caught that — this is the half of the wave's lens question that
    had never been asked.
    """
    report = _report_for_devices(
        tmp_path,
        monkeypatch,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        capture_device=RETIRED_ALOOP_CAPTURE_DEVICE,
    )
    assert report["checks"]["capture_route_current"] is False
    assert _failed_checks(report) == {"capture_route_current"}
    assert report["passed"] is False


def test_capture_route_current_accepts_non_ring_playback_with_ring_capture(
    tmp_path, monkeypatch
):
    """T3 — the control: a direct-DAC sink still commissions.

    Kills an over-tightening that would break commissioning on every box whose
    sink is not a ring. Ring A is the one fan-in → CamillaDSP transport
    (ADR-0100), so the source is the ring here exactly as it is on the ring
    sink — the sink is what differs, and it must not change this verdict.
    """
    report = _report_for_devices(
        tmp_path,
        monkeypatch,
        playback_device="hw:CARD=DAC8x,DEV=0",
        capture_device=RING_CAPTURE_DEVICE,
    )
    assert report["checks"]["capture_route_current"] is True
    assert _failed_checks(report) == set()
    assert report["passed"] is True


def test_capture_route_current_refuses_non_ring_playback_with_aloop_capture(
    tmp_path, monkeypatch
):
    """T5 — the sink does not license the retired tap either.

    The mirror of T2 on the other sink: a graph still sourcing the snd-aloop tap
    reads a device nothing writes whatever it plays into, so the refusal must
    not depend on the sink being a ring. Without this, a check that only
    inspected ring sinks would pass every direct-DAC box back onto the retired
    source.
    """
    report = _report_for_devices(
        tmp_path,
        monkeypatch,
        playback_device="hw:CARD=DAC8x,DEV=0",
        capture_device=RETIRED_ALOOP_CAPTURE_DEVICE,
    )
    assert report["checks"]["capture_route_current"] is False
    assert _failed_checks(report) == {"capture_route_current"}
    assert report["passed"] is False


def test_ring_playback_with_aloop_capture_refuses_at_admission_naming_the_check(
    tmp_path, monkeypatch, caplog
):
    """T4 — T2's graph driven through the whole refusal chain.

    A check-level verdict only matters if it reaches the operator, so this pins
    the three rungs below it: ``ProtectionEvidence(current=False)`` →
    ``PROTECTION_EVIDENCE_STALE`` → ``play_admitted_driver_capture`` raising.
    Without it a future check-level fix could silently stop refusing while T2
    still passed. The household-facing message stays the one actionable
    sentence; the check name rides the journal.
    """
    topology, profile, _targets, comparison, applied, raw, load_payload = _context(
        tmp_path, monkeypatch
    )
    swept = _graph_with_devices(
        raw,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        capture_device=RETIRED_ALOOP_CAPTURE_DEVICE,
    )

    async def read_running():
        return swept

    async def read_volume():
        return -4.0

    with caplog.at_level(logging.INFO, logger=_ADMISSION_LOGGER_NAME):
        with pytest.raises(ActiveCommissioningAdmissionError) as excinfo:
            asyncio.run(
                play_admitted_driver_capture(
                    topology=topology,
                    safety_profile=profile,
                    comparison_set=comparison,
                    applied_profile=applied,
                    speaker_group_id="mono",
                    role="woofer",
                    commissioning_gain_db=-50.0,
                    expected_main_volume_db=-4.0,
                    load_payload=load_payload,
                    read_running_config=read_running,
                    read_main_volume_db=read_volume,
                    load_current_context=lambda: (
                        topology,
                        profile,
                        comparison,
                        applied,
                    ),
                    alsa_device="correction_substream",
                    timeout_margin_s=9.0,
                )
            )

    from jasper.audio_measurement.admitted_playback import (
        PLAYBACK_READMISSION_REFUSED_MESSAGE,
    )

    assert str(excinfo.value) == PLAYBACK_READMISSION_REFUSED_MESSAGE
    assert excinfo.value.refusal_codes == ("protection_evidence_stale",)
    assert (
        "event=active_speaker.driver_capture_protection_evidence_stale" in caplog.text
    )
    assert "failed_checks=capture_route_current" in caplog.text
    assert "failed_protection_checks=capture_route_current" in caplog.text

    report_path = tmp_path / comparison["bundle_session_id"] / "protection_report.json"
    assert report_path.exists()
    persisted = json.loads(report_path.read_text())
    assert persisted["checks"]["capture_route_current"] is False
    assert persisted["passed"] is False


def _stimulus_inputs(tmp_path):
    from jasper.audio_measurement.sweep import synchronized_swept_sine
    from tests.test_audio_measurement_excitation_artifacts import _generation

    authority, generation = _generation(tmp_path)
    _signal, meta = synchronized_swept_sine(
        f1=200.0, f2=2000.0, duration_approx_s=0.2, sample_rate=48000
    )
    return authority, generation, meta


@pytest.mark.parametrize("preexisting", [False, True])
def test_stimulus_directory_creation_syncs_authority_entry_and_mode(
    tmp_path, monkeypatch, preexisting
):
    import jasper.active_speaker.commissioning_admission as admission

    authority, generation, meta = _stimulus_inputs(tmp_path)
    stimuli = authority.directory / "stimuli"
    if preexisting:
        stimuli.mkdir(mode=0o700)
    synced: list[Path] = []
    real_fsync = admission.fsync_directory

    def record_fsync(path):
        synced.append(Path(path))
        real_fsync(path)

    monkeypatch.setattr(admission, "fsync_directory", record_fsync)
    stimulus = admission.persist_synchronized_stimulus_once(
        authority.directory, generation=generation, meta=meta
    )

    assert (authority.directory / stimulus.artifact.relative_path).is_file()
    assert stat.S_IMODE(stimuli.stat().st_mode) == 0o750
    assert (authority.directory in synced) is (not preexisting)
    assert stimuli in synced
    assert not list(stimuli.glob(".stimulus-*"))


def test_stimulus_persist_is_idempotent_for_identical_bytes(tmp_path):
    from jasper.active_speaker.commissioning_admission import (
        persist_synchronized_stimulus_once,
    )

    authority, generation, meta = _stimulus_inputs(tmp_path)
    first = persist_synchronized_stimulus_once(
        authority.directory, generation=generation, meta=meta
    )
    again = persist_synchronized_stimulus_once(
        authority.directory, generation=generation, meta=meta
    )
    assert again.artifact == first.artifact


def test_stimulus_persist_refuses_conflicting_published_bytes(tmp_path):
    from jasper.active_speaker.commissioning_admission import (
        persist_synchronized_stimulus_once,
    )

    authority, generation, meta = _stimulus_inputs(tmp_path)
    target = authority.directory / f"stimuli/{generation.admission_id}.wav"
    target.parent.mkdir(mode=0o750)
    target.write_bytes(b"not the admitted sweep")
    with pytest.raises(ActiveCommissioningAdmissionError):
        persist_synchronized_stimulus_once(
            authority.directory, generation=generation, meta=meta
        )
    assert target.read_bytes() == b"not the admitted sweep"


def test_stimulus_publish_fault_after_link_keeps_target_and_recovers(
    tmp_path, monkeypatch
):
    import jasper.active_speaker.commissioning_admission as admission

    authority, generation, meta = _stimulus_inputs(tmp_path)
    stimuli = authority.directory / "stimuli"
    stimuli.mkdir(mode=0o750)
    real_fsync = admission.fsync_directory

    def failing_fsync(path):
        raise OSError(errno.EIO, "simulated directory fsync fault")

    monkeypatch.setattr(admission, "fsync_directory", failing_fsync)
    with pytest.raises(ActiveCommissioningAdmissionError):
        admission.persist_synchronized_stimulus_once(
            authority.directory, generation=generation, meta=meta
        )
    target = authority.directory / f"stimuli/{generation.admission_id}.wav"
    assert target.is_file()
    assert not list(stimuli.glob(".stimulus-*"))

    monkeypatch.setattr(admission, "fsync_directory", real_fsync)
    recovered = admission.persist_synchronized_stimulus_once(
        authority.directory, generation=generation, meta=meta
    )
    assert (
        authority.directory / recovered.artifact.relative_path
    ).read_bytes() == target.read_bytes()
