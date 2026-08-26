# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.audio_profile_state import (
    AecIntent,
    MicProbe,
    RuntimeAecEnv,
    build_audio_profile_status,
    profile_env_updates,
    resolve_audio_input_intent,
    runtime_env_from_mapping,
)


def test_runtime_env_from_mapping_prefers_fresh_env_file_over_process_env():
    runtime = runtime_env_from_mapping(
        {"JASPER_AEC_CHIP_AEC_ENABLED": "1"},
        process_env={
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
        },
    )

    assert runtime.chip_enabled is True
    assert runtime.chip_aec_150_device == "udp:9887"


def test_chip_aec_active_requires_bridge_firmware_and_runtime_env():
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"] == {
        "selection": "xvf_chip_aec",
        "requested": "xvf_chip_aec",
        "resolved": "xvf_chip_aec",
        "active": "xvf_chip_aec",
        "state": "active",
        "reason": "Chip-AEC runtime env is applied.",
        "validation_profile": "xvf_chip_aec",
        "action": "",
    }
    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["wake_legs"] == ["Primary chip beam"]
    assert status["microphone"]["warnings"] == []


def test_chip_aec_active_reports_explicit_extra_wake_beams():
    status = build_audio_profile_status(
        AecIntent(
            mode="auto",
            chip_aec_enabled=True,
            chip_aec_150_enabled=True,
            chip_aec_210_enabled=True,
        ),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["microphone"]["wake_legs"] == [
        "Primary chip beam",
        "Chip AEC 150",
        "Chip AEC 210",
    ]


def test_chip_aec_extra_wake_beams_are_runtime_owned():
    status = build_audio_profile_status(
        AecIntent(
            mode="auto",
            chip_aec_enabled=True,
            chip_aec_150_enabled=True,
            chip_aec_210_enabled=True,
        ),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["microphone"]["wake_legs"] == ["Primary chip beam"]


def test_managed_xvf_parks_instead_of_reporting_software_fallback():
    """A kept park (ADR-0101: observed breakage) still reports no engine."""
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=False,
            chip_aec_alignment_status="fault",
            chip_aec_alignment_reason="chip-AEC bridge failed after alignment reapply",
            chip_aec_alignment_action="Inspect jasper-aec-bridge, then run the reconciler",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "fault"
    assert status["audio_profile"]["action"] == (
        "Inspect jasper-aec-bridge, then run the reconciler"
    )
    assert status["microphone"]["processing_mode"] == "Chip-AEC parked"
    assert "AEC3" not in status["microphone"]["processing_mode"]


def test_profile_status_warns_when_saved_aec_card_is_stale():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device="L16K6Ch",
            chip_enabled=False,
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
        bridge_active=False,
        chip_available=True,
    )

    assert status["audio_profile"]["state"] == "waiting_bridge"
    assert "configured AEC mic L16K6Ch" in status["audio_profile"]["reason"]
    assert "detected XVF card Array" in status["audio_profile"]["reason"]
    warnings = " ".join(status["microphone"]["warnings"])
    assert "Configured AEC mic L16K6Ch" in warnings
    assert "detected XVF card Array" in warnings
    assert "run the reconciler" in warnings


def test_chip_aec_active_requires_detected_aec_card_match():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device="L16K6Ch",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "pending"
    assert status["microphone"]["processing_mode"] == "Chip-AEC pending"
    assert status["microphone"]["wake_legs"] == []
    assert "Configured AEC mic L16K6Ch" in " ".join(
        status["microphone"]["warnings"]
    )


def test_software_aec3_profile_reports_optional_legs():
    status = build_audio_profile_status(
        AecIntent(mode="auto", raw_enabled=True, dtln_enabled=True),
        RuntimeAecEnv(
            primary_device="udp:9876",
            raw_device="udp:9877",
            dtln_device="udp:9878",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert status["audio_profile"]["requested"] == "xvf_software_aec3"
    assert status["audio_profile"]["selection"] == "custom"
    assert status["audio_profile"]["active"] == "xvf_software_aec3"
    assert status["microphone"]["wake_legs"] == ["AEC3", "Chip-direct raw", "DTLN"]


def test_disabled_mode_reports_direct_mic_profile():
    status = build_audio_profile_status(
        AecIntent(mode="disabled"),
        RuntimeAecEnv(primary_device="USB PnP Sound Device", aec_device="Array"),
        MicProbe(xvf_present=False, capture_channels=None),
        bridge_active=False,
        chip_available=False,
    )

    assert status["audio_profile"]["requested"] == "direct_mic"
    assert status["audio_profile"]["selection"] == "direct_mic"
    assert status["audio_profile"]["active"] == "direct_mic"
    assert status["audio_profile"]["state"] == "disabled"
    assert status["microphone"]["detected"] is True
    assert status["microphone"]["name"] == "Direct mic (USB PnP Sound Device)"


def test_auto_profile_resolves_to_chip_aec_when_available():
    intent = resolve_audio_input_intent(
        AecIntent(profile_selection="auto", raw_enabled=True),
        chip_available=True,
    )

    assert intent.mode == "auto"
    assert intent.raw_enabled is False
    assert intent.dtln_enabled is False
    assert intent.chip_aec_enabled is True
    assert intent.chip_aec_150_enabled is False
    assert intent.chip_aec_210_enabled is False


def test_auto_profile_falls_back_to_software_aec3_when_chip_unavailable():
    intent = resolve_audio_input_intent(
        AecIntent(profile_selection="auto", chip_aec_enabled=True),
        chip_available=False,
    )

    assert intent.mode == "auto"
    assert intent.raw_enabled is True
    assert intent.dtln_enabled is False
    assert intent.chip_aec_enabled is False
    assert intent.chip_aec_150_enabled is False
    assert intent.chip_aec_210_enabled is False


def test_profile_env_updates_stamp_rollback_safe_legacy_keys():
    assert profile_env_updates("xvf_chip_aec") == {
        "JASPER_AUDIO_INPUT_PROFILE": "xvf_chip_aec",
        "JASPER_AEC_MODE": "auto",
        "JASPER_WAKE_LEG_RAW": "0",
        "JASPER_WAKE_LEG_DTLN": "0",
        "JASPER_WAKE_LEG_CHIP_AEC": "1",
        "JASPER_WAKE_LEG_CHIP_AEC_150": "0",
        "JASPER_WAKE_LEG_CHIP_AEC_210": "0",
    }
    assert profile_env_updates("auto")["JASPER_WAKE_LEG_CHIP_AEC"] == "0"


def test_testing_profile_uses_chip_aec_runtime_but_same_validation_profile():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec_testing"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
            chip_aec_150_device="udp:9887",
            chip_aec_210_device="udp:9888",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
        chip_gate={
            "status": "testing",
            "auto_allowed": False,
            "detail": "operator validation",
        },
    )

    assert status["audio_profile"]["selection"] == "xvf_chip_aec_testing"
    assert status["audio_profile"]["requested"] == "xvf_chip_aec"
    assert status["audio_profile"]["active"] == "xvf_chip_aec"
    assert status["audio_profile"]["validation_profile"] == "xvf_chip_aec"
    assert status["microphone"]["processing_mode"] == "Chip-AEC"


def test_a_disclosed_managed_xvf_reports_the_engine_it_is_actually_running():
    """ADR-0101: disclosed_stale is a RUNNING state, not a park.

    The chip leg is not armed, but the 6-channel mic carries software AEC3 and
    the wake path is up — reporting that as parked with no legs would describe
    a hearing speaker as deaf.
    """
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="auto"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            raw_device="udp:9877",
            chip_enabled=False,
            chip_aec_alignment_status="disclosed_stale",
            chip_aec_alignment_reason="output DAC has no codified chip-AEC calibration",
            chip_aec_alignment_action="Run sudo jasper-aec-commission",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
        chip_gate={
            "status": "needs_calibration",
            "auto_allowed": False,
            "detail": "no codified timing",
        },
    )

    profile = status["audio_profile"]
    assert profile["state"] == "disclosed_stale"
    assert profile["active"] == "xvf_software_aec3"
    assert profile["reason"] == "output DAC has no codified chip-AEC calibration"
    assert profile["action"] == "Run sudo jasper-aec-commission"
    assert status["microphone"]["processing_mode"] == "Software AEC3"
    assert status["microphone"]["wake_legs"]


def test_a_disclosed_two_channel_xvf_reports_its_direct_mic():
    status = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="auto"),
        RuntimeAecEnv(
            primary_device="Array",
            chip_enabled=False,
            chip_aec_alignment_status="disclosed_stale",
            chip_aec_alignment_reason=(
                "echo cancellation unavailable until DFU flash to 6-channel firmware"
            ),
            chip_aec_alignment_action="Re-flash the XVF to 6-channel firmware",
        ),
        MicProbe(xvf_present=True, capture_channels=2, recommended_channels=6),
        bridge_active=False,
        chip_available=False,
    )

    profile = status["audio_profile"]
    assert profile["state"] == "disclosed_stale"
    assert profile["active"] == "direct_mic"
    assert "DFU flash" in profile["reason"]
    assert status["microphone"]["wake_legs"] == ["Direct mic"]


def test_an_explicit_custom_chip_leg_reads_as_chip_aec_on_an_uncodified_dac():
    """Site 11 honours the operator's leg, so the classifier must see it run.

    Reading auto_allowed here would report a chip beam actually carrying :9876
    as software AEC3.
    """
    status = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True, profile_selection="custom"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
        chip_gate={
            "status": "needs_calibration",
            "auto_allowed": False,
            "detail": "no codified timing",
        },
    )

    assert status["audio_profile"]["active"] == "xvf_chip_aec"
    assert status["microphone"]["processing_mode"] == "Chip-AEC"


@pytest.mark.parametrize(
    ("selection", "stamp", "state", "action"),
    [
        # The reconciler writes the record only on managed selections and never
        # clears it on a custom profile, so what a hand-armed custom box holds
        # is a leftover: stamped for the managed selection that wrote it, or
        # predating the stamp entirely. Neither may hand this box a verdict.
        ("custom", "xvf_chip_aec", "active", ""),
        ("custom", "", "active", ""),
        # The selection the record was written for still classifies, and a
        # legacy record answers for the managed selections that alone could
        # have written it.
        ("xvf_chip_aec", "xvf_chip_aec", "disclosed_stale", "Run sudo jasper-aec-commission"),
        ("xvf_chip_aec", "", "disclosed_stale", "Run sudo jasper-aec-commission"),
    ],
)
def test_an_alignment_record_classifies_only_the_selection_it_was_written_for(
    selection: str, stamp: str, state: str, action: str,
) -> None:
    """One owner for the rule, and it is the stamp (issue #3073 item 1).

    The engine half is the kill-test: dropping a leftover verdict must not
    cost the box the chip beam it is demonstrably running, and the
    uncodified-DAC disclosure the leftover used to carry still reaches the
    payload from the gate that actually authored it.
    """
    status = build_audio_profile_status(
        AecIntent(
            mode="auto",
            chip_aec_enabled=True,
            chip_aec_210_enabled=True,
            profile_selection=selection,
        ),
        RuntimeAecEnv(
            primary_device="udp:9876",
            chip_enabled=True,
            chip_aec_210_device="udp:9888",
            chip_aec_alignment_status="disclosed_stale",
            chip_aec_alignment_reason="output DAC has no codified chip-AEC calibration",
            chip_aec_alignment_action="Run sudo jasper-aec-commission",
            chip_aec_alignment_selection=stamp,
        ),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
        chip_gate={
            "status": "needs_calibration",
            "auto_allowed": False,
            "detail": "no codified timing",
        },
    )

    profile = status["audio_profile"]
    assert profile["state"] == state
    assert profile["action"] == action
    assert profile["chip_aec_gate"]["status"] == "needs_calibration"
    assert profile["active"] == "xvf_chip_aec"
    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["wake_legs"] == ["Primary chip beam", "Chip AEC 210"]


def test_a_leftover_alignment_key_cannot_classify_a_selection_it_never_described():
    """The reconciler writes that key only on chip-arming paths.

    It is not written or cleared for auto on a mic-less box, for an explicitly
    AEC-free direct mic, or for a custom profile whose chip leg is off, so a
    leftover value there describes a path nobody is running: claiming an
    engine from it reports a deaf box as hearing, and a deliberately AEC-free
    box as permanently disclosed.
    """
    leftover = {
        "chip_aec_alignment_status": "disclosed_stale",
        "chip_aec_alignment_reason": "output DAC has no codified chip-AEC calibration",
        "chip_aec_alignment_action": "Run sudo jasper-aec-commission",
    }
    mic_less = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="auto"),
        RuntimeAecEnv(**leftover),
        MicProbe(xvf_present=False, capture_channels=None),
        bridge_active=False,
        chip_available=False,
    )
    aec_off = build_audio_profile_status(
        AecIntent(mode="off", profile_selection="direct_mic"),
        RuntimeAecEnv(primary_device="hw:CARD=Device", **leftover),
        MicProbe(xvf_present=False, capture_channels=2, recommended_channels=6),
        bridge_active=False,
        chip_available=False,
    )
    software_custom = build_audio_profile_status(
        AecIntent(mode="auto", raw_enabled=True, profile_selection="custom"),
        RuntimeAecEnv(primary_device="udp:9876", raw_device="udp:9877", **leftover),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=True,
        chip_available=True,
    )

    assert mic_less["audio_profile"]["state"] == "waiting_bridge"
    assert mic_less["audio_profile"]["active"] is None
    assert mic_less["microphone"]["wake_legs"] == []
    assert aec_off["audio_profile"]["state"] == "disabled"
    assert software_custom["audio_profile"]["state"] == "active"
    assert software_custom["audio_profile"]["active"] == "xvf_software_aec3"


def test_a_disclosed_box_with_no_live_engine_waits_instead_of_claiming_one():
    """A disclosure names a running engine or it is not a disclosure.

    Two boxes with nothing on the wake path to name: the bridge died under a
    managed XVF, and an operator's custom chip leg is disclosed on a box with
    no capture device at all, whose mic keys still hold the never-configured
    `Array` default. Both report the pending state the base arms already
    report, not `disclosed_stale` with no legs.
    """
    disclosed = {
        "chip_aec_alignment_status": "disclosed_stale",
        "chip_aec_alignment_reason": "output DAC has no codified chip-AEC calibration",
        "chip_aec_alignment_action": "Run sudo jasper-aec-commission",
    }
    bridge_down = build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="auto"),
        RuntimeAecEnv(primary_device="udp:9876", **disclosed),
        MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6),
        bridge_active=False,
        chip_available=True,
    )
    no_capture_device = build_audio_profile_status(
        AecIntent(mode="auto", chip_aec_enabled=True, profile_selection="custom"),
        RuntimeAecEnv(**disclosed),
        MicProbe(xvf_present=False, capture_channels=None),
        bridge_active=False,
        chip_available=False,
    )

    for status in (bridge_down, no_capture_device):
        assert status["audio_profile"]["state"] == "waiting_bridge"
        assert status["audio_profile"]["active"] is None
        assert status["microphone"]["wake_legs"] == []


def test_an_unqualified_chip_path_discloses_while_an_engine_carries_wake():
    """ADR-0101: refuse only a box with nothing on the wake path.

    The output DAC has no codified chip-AEC timing, so chip-AEC cannot arm on
    its own — but AEC3 is carrying wake, which is a disclosure carrying the
    commissioner, not the `unavailable` a box with no engine at all reports.
    The gate answers in action codes; `action` is operator text.
    """
    gate = {
        "status": "needs_calibration",
        "detail": "no codified timing",
        "auto_allowed": False,
        "recommended_action": "use_software_aec3_or_enable_testing",
    }
    mic = MicProbe(xvf_present=False, capture_channels=2, recommended_channels=6)
    intent = AecIntent(
        mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec"
    )
    running = build_audio_profile_status(
        intent,
        RuntimeAecEnv(primary_device="udp:9876", raw_device="udp:9877"),
        mic,
        bridge_active=True,
        chip_available=True,
        chip_gate=gate,
    )
    stopped = build_audio_profile_status(
        intent,
        RuntimeAecEnv(primary_device=""),
        mic,
        bridge_active=True,
        chip_available=True,
        chip_gate=gate,
    )

    assert running["audio_profile"]["state"] == "disclosed_stale"
    assert running["audio_profile"]["active"] == "xvf_software_aec3"
    assert running["audio_profile"]["reason"] == "no codified timing"
    assert running["audio_profile"]["action"] == "Run sudo jasper-aec-commission"
    assert stopped["audio_profile"]["state"] == "unavailable"
    assert stopped["audio_profile"]["active"] is None
    assert stopped["audio_profile"]["action"] == ""


def _chip_box(
    *,
    aec_device: str,
    alignment_status: str,
    chip_available: bool = True,
    chip_gate: dict | None = None,
) -> dict:
    return build_audio_profile_status(
        AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device=aec_device,
            chip_enabled=True,
            chip_aec_alignment_status=alignment_status,
            chip_aec_alignment_reason="chip-AEC alignment lost its proof",
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="XVF3800",
        ),
        bridge_active=True,
        chip_available=chip_available,
        chip_gate=chip_gate,
    )


@pytest.mark.parametrize(
    ("alignment_status", "aligned_state", "mismatched_active", "mismatched_state",
     "mismatched_mode"),
    [
        ("ready", "active", None, "pending", "Chip-AEC pending"),
        (
            "disclosed_stale",
            "disclosed_stale",
            "xvf_software_aec3",
            "disclosed_stale",
            "Software AEC3",
        ),
    ],
)
def test_a_mic_the_bridge_is_not_using_stops_every_arm_claiming_its_chip_beam(
    alignment_status,
    aligned_state,
    mismatched_active,
    mismatched_state,
    mismatched_mode,
):
    """One resolver answers which engine carries wake, so no arm can dissent.

    A configured AEC mic that is not the detected XVF card means what arrives
    cannot be this chip's beam. The ready arm already refused it while the
    disclosed arm named chip-AEC from the same facts (issue #3073 item 2), so
    one box answered two ways depending only on which arm reported.
    """
    aligned = _chip_box(aec_device="XVF3800", alignment_status=alignment_status)
    mismatched = _chip_box(aec_device="OtherCard", alignment_status=alignment_status)

    assert aligned["audio_profile"]["active"] == "xvf_chip_aec"
    assert aligned["audio_profile"]["state"] == aligned_state
    assert aligned["microphone"]["processing_mode"] == "Chip-AEC"

    assert mismatched["audio_profile"]["active"] == mismatched_active
    assert mismatched["audio_profile"]["state"] == mismatched_state
    assert mismatched["microphone"]["processing_mode"] == mismatched_mode


@pytest.mark.parametrize(
    ("chip_available", "chip_gate"),
    [
        (False, None),
        (True, {"auto_allowed": False, "status": "unsupported"}),
    ],
    ids=["no_beam_plan", "dac_gate_refuses"],
)
def test_the_ready_arm_claims_no_chip_beam_it_was_never_allowed_to_arm(
    chip_available,
    chip_gate,
):
    """Arming policy gates the ready arm, whatever the runtime env applied.

    Every live fact says chip: the leg is enabled, the bridge is up on the
    carrier and the mic matches. Only the beam plan and the DAC gate say this
    box may not arm chip-AEC on its own, and `active` follows them.
    """
    status = _chip_box(
        aec_device="XVF3800",
        alignment_status="ready",
        chip_available=chip_available,
        chip_gate=chip_gate,
    )

    assert status["audio_profile"]["active"] is None
    assert status["audio_profile"]["state"] == "unavailable"
    assert status["microphone"]["processing_mode"] == "Chip-AEC pending"
    assert status["microphone"]["wake_legs"] == []


def test_a_chip_engine_is_never_stamped_with_a_profile_it_is_not_running():
    """The engine named and the profile stamped are one resolver's answer.

    The chip beam is on the carrier under a custom selection whose stored
    intent has no chip leg, so the arm took its profile from the selection
    and reported a Chip-AEC engine running the software profile — one
    payload answering itself two ways (issue #3073 item 2).
    """
    status = build_audio_profile_status(
        AecIntent(mode="auto", raw_enabled=True, profile_selection="custom"),
        RuntimeAecEnv(
            primary_device="udp:9876",
            aec_device="XVF3800",
            chip_enabled=True,
            chip_aec_alignment_status="ready",
        ),
        MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="XVF3800",
        ),
        bridge_active=True,
        chip_available=True,
    )

    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["audio_profile"]["active"] == "xvf_chip_aec"
    assert status["microphone"]["wake_legs"] == ["Primary chip beam"]
