# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.chip_aec_health import ACTION_RECOMMISSION, AlignmentHealth
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


_CHIP_INTENT = AecIntent(mode="auto", chip_aec_enabled=True)
_EXTRA_BEAMS_INTENT = AecIntent(
    mode="auto",
    chip_aec_enabled=True,
    chip_aec_150_enabled=True,
    chip_aec_210_enabled=True,
)
_READY = AlignmentHealth("ready")
_READY_CHIP_RUNTIME = RuntimeAecEnv(
    primary_device="udp:9876",
    chip_enabled=True,
    chip_aec_alignment=_READY,
)
_EXTRA_BEAMS_RUNTIME = RuntimeAecEnv(
    primary_device="udp:9876",
    chip_enabled=True,
    chip_aec_alignment=_READY,
    chip_aec_150_device="udp:9887",
    chip_aec_210_device="udp:9888",
)
_MANAGED_XVF = MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6)
_MISMATCHED_XVF = MicProbe(
    xvf_present=True,
    capture_channels=6,
    recommended_channels=6,
    alsa_card_name="Array",
    variant_id="xvf3800_legacy_square_6ch",
    geometry="square",
    chip_beam_plan="xvf_square_fixed_150_210",
)
_UNCODIFIED_GATE = {
    "status": "needs_calibration",
    "auto_allowed": False,
    "detail": "no codified timing",
}
_REFUSING_GATE = {
    **_UNCODIFIED_GATE,
    "recommended_action": "use_software_aec3_or_enable_testing",
}


def _disclosed_runtime(**kwargs) -> RuntimeAecEnv:
    return RuntimeAecEnv(
        chip_aec_alignment=AlignmentHealth(
            "disclosed_stale",
            "output DAC has no codified chip-AEC calibration",
            "Run sudo jasper-aec-commission",
        ),
        **kwargs,
    )


_STATUS_CASES = {
    "chip-aec-active": {
                "exact_profile": {
                    "selection": "xvf_chip_aec", "requested": "xvf_chip_aec",
                    "active": "xvf_chip_aec",
                    "state": "active", "reason": "Chip-AEC runtime env is applied.",
                    "validation_profile": "xvf_chip_aec", "action": "",
                    "commission_recommended": False,
                },
                "equals": {"microphone": {
                    "processing_mode": "Chip-AEC",
                    "wake_legs": ["Primary chip beam"], "warnings": [],
                }},
    },
    "runtime-publishes-extra-wake-beams": {
                "intent": _EXTRA_BEAMS_INTENT, "runtime": _EXTRA_BEAMS_RUNTIME,
                "equals": {"microphone": {"wake_legs": [
                    "Primary chip beam", "Chip AEC 150", "Chip AEC 210",
                ]}},
    },
    "intent-alone-does-not-publish-extra-wake-beams": {
                "intent": _EXTRA_BEAMS_INTENT,
                "equals": {"microphone": {"wake_legs": ["Primary chip beam"]}},
    },
    "managed-xvf-fault-parks": {
                "runtime": RuntimeAecEnv(
                    primary_device="udp:9876", chip_enabled=False,
                    chip_aec_alignment=AlignmentHealth(
                        "fault",
                        "chip-AEC bridge failed after alignment reapply",
                        "Inspect jasper-aec-bridge, then run the reconciler",
                    ),
                ),
                "equals": {
                    "audio_profile": {
                        "requested": "xvf_chip_aec", "active": None,
                        "state": "fault", "action": (
                            "Inspect jasper-aec-bridge, then run the reconciler"
                        ),
                    },
                    "microphone": {"processing_mode": "Chip-AEC parked"},
                },
                "excludes": {("microphone", "processing_mode"): ("AEC3",)},
    },
    "saved-aec-card-is-stale": {
                "intent": AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
                "runtime": RuntimeAecEnv(
                    primary_device="udp:9876", aec_device="L16K6Ch",
                    chip_enabled=False,
                ),
                "mic": _MISMATCHED_XVF, "bridge_active": False,
                "equals": {"audio_profile": {"state": "waiting_bridge"}},
                "contains": {
                    ("audio_profile", "reason"): (
                        "configured AEC mic L16K6Ch", "detected XVF card Array",
                    ),
                    ("microphone", "warnings"): (
                        "Configured AEC mic L16K6Ch", "detected XVF card Array",
                        "run the reconciler",
                    ),
                },
    },
    "detected-aec-card-must-match": {
                "intent": AecIntent(mode="auto", profile_selection="xvf_chip_aec"),
                "runtime": RuntimeAecEnv(
                    primary_device="udp:9876", aec_device="L16K6Ch",
                    chip_enabled=True, chip_aec_alignment=_READY,
                    chip_aec_150_device="udp:9887", chip_aec_210_device="udp:9888",
                ),
                "mic": _MISMATCHED_XVF,
                "equals": {
                    "audio_profile": {"active": None, "state": "pending"},
                    "microphone": {
                        "processing_mode": "Chip-AEC pending", "wake_legs": [],
                    },
                },
                "contains": {("microphone", "warnings"): (
                    "Configured AEC mic L16K6Ch",
                )},
    },
    "software-aec3-optional-legs": {
                "intent": AecIntent(mode="auto", raw_enabled=True, dtln_enabled=True),
                "runtime": RuntimeAecEnv(
                    primary_device="udp:9876", raw_device="udp:9877",
                    dtln_device="udp:9878",
                ),
                "equals": {
                    "audio_profile": {
                        "requested": "xvf_software_aec3", "selection": "custom",
                        "active": "xvf_software_aec3",
                    },
                    "microphone": {
                        "wake_legs": ["AEC3", "Chip-direct raw", "DTLN"],
                    },
                },
    },
    "disabled-mode-is-direct-mic": {
                "intent": AecIntent(mode="disabled"),
                "runtime": RuntimeAecEnv(
                    primary_device="USB PnP Sound Device", aec_device="Array",
                ),
                "mic": MicProbe(xvf_present=False, capture_channels=None),
                "bridge_active": False, "chip_available": False,
                "equals": {
                    "audio_profile": {
                        "requested": "direct_mic", "selection": "direct_mic",
                        "active": "direct_mic", "state": "disabled",
                    },
                    "microphone": {
                        "detected": True, "name": "Direct mic (USB PnP Sound Device)",
                    },
                },
    },
    "testing-selection-uses-chip-runtime": {
                "intent": AecIntent(
                    mode="auto", profile_selection="xvf_chip_aec_testing",
                ),
                "runtime": _EXTRA_BEAMS_RUNTIME,
                "chip_gate": {
                    "status": "testing", "auto_allowed": False,
                    "detail": "operator validation",
                },
                "equals": {
                    "audio_profile": {
                        "selection": "xvf_chip_aec_testing",
                        "requested": "xvf_chip_aec", "active": "xvf_chip_aec",
                        "validation_profile": "xvf_chip_aec",
                    },
                    "microphone": {"processing_mode": "Chip-AEC"},
                },
    },
    "disclosed-managed-xvf-reports-live-engine": {
                "intent": AecIntent(mode="auto", profile_selection="auto"),
                "runtime": RuntimeAecEnv(
                    primary_device="udp:9876", raw_device="udp:9877",
                    chip_enabled=False,
                    chip_aec_alignment=AlignmentHealth(
                        "disclosed_stale",
                        "output DAC has no codified chip-AEC calibration",
                        "Run sudo jasper-aec-commission",
                    ),
                ),
                "chip_gate": _UNCODIFIED_GATE,
                "equals": {
                    "audio_profile": {
                        "state": "disclosed_stale", "active": "xvf_software_aec3",
                        "reason": "output DAC has no codified chip-AEC calibration",
                        "action": "Run sudo jasper-aec-commission",
                    },
                    "microphone": {"processing_mode": "Software AEC3"},
                },
                "truthy": (("microphone", "wake_legs"),),
    },
    "disclosed-two-channel-xvf-reports-direct-mic": {
                "intent": AecIntent(mode="auto", profile_selection="auto"),
                "runtime": RuntimeAecEnv(
                    primary_device="Array", chip_enabled=False,
                    chip_aec_alignment=AlignmentHealth(
                        "disclosed_stale",
                        "echo cancellation unavailable until DFU flash to "
                        "6-channel firmware",
                        "Re-flash the XVF to 6-channel firmware",
                    ),
                ),
                "mic": MicProbe(
                    xvf_present=True, capture_channels=2, recommended_channels=6,
                ),
                "bridge_active": False, "chip_available": False,
                "equals": {
                    "audio_profile": {"state": "disclosed_stale", "active": "direct_mic"},
                    "microphone": {"wake_legs": ["Direct mic"]},
                },
                "contains": {("audio_profile", "reason"): ("DFU flash",)},
    },
    "custom-chip-leg-runs-on-uncodified-dac": {
                "intent": AecIntent(
                    mode="auto", chip_aec_enabled=True, profile_selection="custom",
                ),
                "chip_gate": _UNCODIFIED_GATE,
                "equals": {
                    "audio_profile": {"active": "xvf_chip_aec"},
                    "microphone": {"processing_mode": "Chip-AEC"},
                },
    },
    "recommission-action-is-recommended": {
        "intent": AecIntent(
            mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec",
        ),
        "runtime": RuntimeAecEnv(
            primary_device="udp:9876", chip_enabled=True,
            chip_aec_alignment=AlignmentHealth(
                "disclosed_stale",
                "proof no longer describes this box",
                ACTION_RECOMMISSION,
            ),
        ),
        "chip_gate": {"status": "approved", "auto_allowed": True},
        "equals": {"audio_profile": {"commission_recommended": True}},
    },
    "other-action-is-not-recommended": {
        "intent": AecIntent(
            mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec",
        ),
        "runtime": RuntimeAecEnv(
            primary_device="udp:9876", chip_enabled=True,
            chip_aec_alignment=AlignmentHealth(
                "disclosed_stale",
                "proof no longer describes this box",
                "Wait for jasper-outputd to restart, then run the reconciler",
            ),
        ),
        "chip_gate": {"status": "approved", "auto_allowed": True},
        "equals": {"audio_profile": {"commission_recommended": False}},
        "not_equals": {("audio_profile", "action"): ""},
    },
    "leftover-alignment-on-mic-less-auto": {
        "intent": AecIntent(mode="auto", profile_selection="auto"),
        "runtime": _disclosed_runtime(),
        "mic": MicProbe(xvf_present=False, capture_channels=None),
        "bridge_active": False, "chip_available": False,
        "equals": {
            "audio_profile": {"state": "waiting_bridge", "active": None},
            "microphone": {"wake_legs": []},
        },
    },
    "leftover-alignment-on-aec-off": {
        "intent": AecIntent(mode="off", profile_selection="direct_mic"),
        "runtime": _disclosed_runtime(primary_device="hw:CARD=Device"),
        "mic": MicProbe(
            xvf_present=False, capture_channels=2, recommended_channels=6,
        ),
        "bridge_active": False, "chip_available": False,
        "equals": {"audio_profile": {"state": "disabled"}},
    },
    "leftover-alignment-on-software-custom": {
        "intent": AecIntent(
            mode="auto", raw_enabled=True, profile_selection="custom",
        ),
        "runtime": _disclosed_runtime(
            primary_device="udp:9876", raw_device="udp:9877",
        ),
        "equals": {"audio_profile": {
            "state": "active", "active": "xvf_software_aec3",
        }},
    },
    "disclosed-managed-box-with-bridge-down": {
        "intent": AecIntent(mode="auto", profile_selection="auto"),
        "runtime": _disclosed_runtime(primary_device="udp:9876"),
        "bridge_active": False,
        "equals": {
            "audio_profile": {"state": "waiting_bridge", "active": None},
            "microphone": {"wake_legs": []},
        },
    },
    "disclosed-custom-box-with-no-capture-device": {
        "intent": AecIntent(
            mode="auto", chip_aec_enabled=True, profile_selection="custom",
        ),
        "runtime": _disclosed_runtime(),
        "mic": MicProbe(xvf_present=False, capture_channels=None),
        "bridge_active": False, "chip_available": False,
        "equals": {
            "audio_profile": {"state": "waiting_bridge", "active": None},
            "microphone": {"wake_legs": []},
        },
    },
    "unqualified-chip-path-with-software-engine": {
        "intent": AecIntent(
            mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec",
        ),
        "runtime": RuntimeAecEnv(
            primary_device="udp:9876", raw_device="udp:9877",
        ),
        "mic": MicProbe(
            xvf_present=False, capture_channels=2, recommended_channels=6,
        ),
        "chip_gate": _REFUSING_GATE,
        "equals": {"audio_profile": {
            "state": "disclosed_stale", "active": "xvf_software_aec3",
            "reason": "no codified timing",
            "action": "Run sudo jasper-aec-commission",
        }},
    },
    "unqualified-chip-path-with-no-engine": {
        "intent": AecIntent(
            mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec",
        ),
        "runtime": RuntimeAecEnv(primary_device=""),
        "mic": MicProbe(
            xvf_present=False, capture_channels=2, recommended_channels=6,
        ),
        "chip_gate": _REFUSING_GATE,
        "equals": {"audio_profile": {
            "state": "unavailable", "active": None, "action": "",
        }},
    },
    "chip-engine-stamps-the-profile-it-runs": {
        "intent": AecIntent(
            mode="auto", raw_enabled=True, profile_selection="custom",
        ),
        "runtime": RuntimeAecEnv(
            primary_device="udp:9876", aec_device="XVF3800",
            chip_enabled=True, chip_aec_alignment=_READY,
        ),
        "mic": MicProbe(
            xvf_present=True, capture_channels=6, recommended_channels=6,
            alsa_card_name="XVF3800",
        ),
        "equals": {
            "audio_profile": {"active": "xvf_chip_aec"},
            "microphone": {
                "processing_mode": "Chip-AEC", "wake_legs": ["Primary chip beam"],
            },
        },
    },
}


@pytest.mark.parametrize("case", _STATUS_CASES.values(), ids=_STATUS_CASES)
def test_audio_profile_status_decision_table(case: dict) -> None:
    status = build_audio_profile_status(
        case.get("intent", _CHIP_INTENT),
        case.get("runtime", _READY_CHIP_RUNTIME),
        case.get("mic", _MANAGED_XVF),
        bridge_active=case.get("bridge_active", True),
        chip_available=case.get("chip_available", True),
        chip_gate=case.get("chip_gate"),
    )

    if "exact_profile" in case:
        assert status["audio_profile"] == case["exact_profile"]
    for section, fields in case.get("equals", {}).items():
        for key, expected in fields.items():
            actual = status[section][key]
            if expected is None or isinstance(expected, bool):
                assert actual is expected
            else:
                assert actual == expected
    for (section, key), unexpected in case.get("not_equals", {}).items():
        assert status[section][key] != unexpected
    for check in ("contains", "excludes"):
        for (section, key), fragments in case.get(check, {}).items():
            value = status[section][key]
            text = " ".join(value) if isinstance(value, list) else value
            for fragment in fragments:
                assert (fragment in text) is (check == "contains")
    for section, key in case.get("truthy", ()):
        assert status[section][key]


@pytest.mark.parametrize(
    ("input_intent", "chip_available", "raw_enabled", "chip_aec_enabled"),
    [
        pytest.param(
            AecIntent(profile_selection="auto", raw_enabled=True),
            True, False, True, id="chip-available",
        ),
        pytest.param(
            AecIntent(profile_selection="auto", chip_aec_enabled=True),
            False, True, False, id="chip-unavailable",
        ),
    ],
)
def test_auto_profile_resolves_by_chip_availability(
    input_intent: AecIntent,
    chip_available: bool,
    raw_enabled: bool,
    chip_aec_enabled: bool,
) -> None:
    intent = resolve_audio_input_intent(input_intent, chip_available=chip_available)

    assert intent.mode == "auto"
    assert intent.raw_enabled is raw_enabled
    assert intent.dtln_enabled is False
    assert intent.chip_aec_enabled is chip_aec_enabled
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
            chip_aec_alignment=AlignmentHealth(
                "disclosed_stale",
                "output DAC has no codified chip-AEC calibration",
                "Run sudo jasper-aec-commission",
                stamp,
            ),
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
    assert profile["commission_recommended"] is (
        action == ACTION_RECOMMISSION
    )
    assert profile["chip_aec_gate"]["status"] == "needs_calibration"
    assert profile["active"] == "xvf_chip_aec"
    assert status["microphone"]["processing_mode"] == "Chip-AEC"
    assert status["microphone"]["wake_legs"] == ["Primary chip beam", "Chip AEC 210"]


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
            chip_aec_alignment=AlignmentHealth(
                alignment_status, "chip-AEC alignment lost its proof",
            ),
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
