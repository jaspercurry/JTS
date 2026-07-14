# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
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
from tests.test_active_speaker_excitation_safety_plan import _profile_and_targets
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_web_commissioning import _driver_comparison_set


def _context(tmp_path: Path, monkeypatch):
    topology, profile, targets = _profile_and_targets()
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


def test_automatic_cooldown_ceiling_fits_relay_recording_budget():
    from jasper.active_speaker.commissioning_admission import (
        ACTIVE_DRIVER_CAPTURE_GRAPH_AND_RELAY_BUDGET_S,
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
        + ACTIVE_DRIVER_CAPTURE_GRAPH_AND_RELAY_BUDGET_S
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
            timeout_s=9.0,
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
                timeout_s=9.0,
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
                timeout_s=9.0,
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
                timeout_s=9.0,
            )
        )
