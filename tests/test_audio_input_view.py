# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.audio_input_view import build_microphone_settings_view, valid_profile_ids


def _base_status() -> dict:
    return {
        "bridge_active": True,
        "audio_profile": {
            "selection": "xvf_chip_aec",
            "requested": "xvf_chip_aec",
            "active": "xvf_chip_aec",
            "state": "active",
            "reason": "Chip-AEC runtime env is applied.",
        },
        "chip_aec_gate": {
            "status": "approved",
            "detail": "Apple USB-C dongle is approved",
            "mic_available": True,
            "production_available": True,
            "testing_available": True,
        },
        "software_aec3": {
            "configured": False,
            "active": False,
            "bypassed": True,
            "reason": "Chip-AEC profile selected.",
        },
        "legs": {
            "raw": {"configured": False},
            "dtln": {"configured": False},
            "chip_aec": {
                "configured": True,
                "production_available": True,
                "testing_available": True,
            },
        },
        "microphone": {
            "detected": True,
            "name": "Legacy square/circular XVF3800 USB 6-channel",
            "firmware": {"state": "ok", "label": "6-channel firmware"},
            "processing_mode": "Chip-AEC",
            "wake_legs": ["Primary chip beam", "Chip AEC 150", "Chip AEC 210"],
            "variant_id": "xvf3800_legacy_square_6ch",
            "geometry": "square",
            "chip_beam_plan": "xvf_square_fixed_150_210",
            "warnings": [],
        },
        "wake_word": {"label": "Jarvis", "pronunciation": "Say Jarvis"},
        "threshold": 0.3,
    }


def _choice(view: dict, profile: str) -> dict:
    for choice in view["echo"]["choices"]:
        if choice["profile"] == profile:
            return choice
    raise AssertionError(f"missing choice for {profile}")


def test_chip_aec_status_becomes_simple_hardware_echo_view() -> None:
    view = build_microphone_settings_view(_base_status())

    assert view["mic"]["kind"] == "xvf3800"
    assert view["mic"]["chip_aec_capable"] is True
    assert view["echo"]["mode"] == "hardware_chip_aec"
    assert "hardware echo cancellation" in view["echo"]["title"].lower()
    assert view["echo"]["software_aec3"]["bypassed"] is True
    assert _choice(view, "xvf_chip_aec")["selected"] is True
    assert _choice(view, "xvf_chip_aec")["enabled"] is True
    assert view["fusion"]["summary"] == "Default hardware beam fusion"
    assert view["fusion"]["wake_legs"] == [
        "Primary chip beam",
        "Chip AEC 150",
        "Chip AEC 210",
    ]


def test_no_mic_disables_echo_choices() -> None:
    status = _base_status()
    status["audio_profile"] = {
        "selection": "direct_mic",
        "requested": "direct_mic",
        "active": "direct_mic",
        "state": "disabled",
    }
    status["microphone"] = {
        "detected": False,
        "firmware": {"state": "absent", "label": "not detected"},
        "warnings": ["XVF3800 mic is not detected."],
    }
    status["chip_aec_gate"] = {
        "status": "needs_calibration",
        "mic_available": False,
        "production_available": False,
        "testing_available": False,
    }
    status["software_aec3"] = {
        "configured": False,
        "active": False,
        "bypassed": False,
    }

    view = build_microphone_settings_view(status)

    assert view["mic"]["kind"] == "none"
    assert view["echo"]["mode"] == "no_mic"
    assert all(not choice["enabled"] for choice in view["echo"]["choices"])


def test_absent_mic_preserves_hidden_testing_profile_as_hardware_choice() -> None:
    status = _base_status()
    status["audio_profile"] = {
        "selection": "xvf_chip_aec_testing",
        "requested": "xvf_chip_aec_testing",
        "active": None,
        "state": "unavailable",
    }
    status["microphone"] = {
        "detected": False,
        "firmware": {"state": "absent", "label": "not detected"},
        "warnings": ["XVF3800 mic is not detected."],
    }
    status["chip_aec_gate"] = {
        "status": "needs_calibration",
        "mic_available": False,
        "production_available": False,
        "testing_available": False,
    }

    view = build_microphone_settings_view(status)
    hardware = _choice(view, "xvf_chip_aec")

    assert hardware["selected"] is True
    assert hardware["visible"] is True
    assert hardware["enabled"] is False
    assert sum(choice["selected"] for choice in view["echo"]["choices"]) == 1


def test_xvf_without_chip_plan_reads_as_running_aec3_with_the_chip_choice_shut() -> None:
    status = _base_status()
    status["audio_profile"] = {
        "selection": "auto",
        "requested": "xvf_chip_aec",
        "active": "xvf_software_aec3",
        "state": "disclosed_stale",
        "reason": "mic has no validated production chip-AEC beam plan",
        "action": "Run sudo jasper-aec-commission",
        "commission_recommended": True,
    }
    status["chip_aec_gate"] = {
        "status": "needs_calibration",
        "detail": "mic has no validated production chip-AEC beam plan",
        "mic_available": False,
        "production_available": False,
        "testing_available": False,
    }
    status["software_aec3"] = {
        "configured": True,
        "active": True,
        "bypassed": False,
    }
    status["legs"] = {
        "raw": {"configured": True},
        "dtln": {"configured": False},
        "chip_aec": {
            "configured": False,
            "production_available": False,
            "testing_available": True,
        },
    }
    status["microphone"].update({
        "name": "ReSpeaker Flex XVF3800 LINEAR-4 16 kHz 6-channel",
        "processing_mode": "Software AEC3",
        "variant_id": "xvf3800_flex_linear_6ch",
        "geometry": "linear",
        "chip_beam_plan": "",
        "wake_legs": ["AEC3", "Chip-direct raw"],
    })

    view = build_microphone_settings_view(status)

    assert view["mic"]["kind"] == "xvf3800"
    assert view["mic"]["chip_aec_capable"] is False
    # ADR-0101: the managed XVF is hearing through AEC3, so the headline names
    # that engine instead of waiting on a chip path nothing is applying.
    assert view["echo"]["mode"] == "software_aec3"
    assert view["echo"]["state"] == "disclosed_stale"
    hardware = _choice(view, "xvf_chip_aec")
    assert hardware["visible"] is True
    assert hardware["enabled"] is False
    assert hardware["status"] == "needs calibration"
    assert _choice(view, "xvf_software_aec3")["selected"] is False
    assert _choice(view, "xvf_software_aec3")["visible"] is False
    assert _choice(view, "auto")["selected"] is True
    # The re-commission remedy is the page's own button, so its SSH prose is
    # dropped from the web wire (doctor keeps it).
    assert view["echo"]["action"] == ""
    assert view["advanced"] == {}


def test_a_disclosed_direct_mic_xvf_reads_as_running_without_bridge_streams() -> None:
    status = _base_status()
    status["bridge_active"] = False
    status["audio_profile"] = {
        "selection": "auto",
        "requested": "xvf_chip_aec",
        "active": "direct_mic",
        "state": "disclosed_stale",
        "reason": "echo cancellation unavailable until DFU flash to 6-channel firmware",
        "action": "Re-flash the XVF to 6-channel firmware",
    }
    status["microphone"].update({
        "processing_mode": "Direct mic",
        "wake_legs": ["Direct mic"],
        "firmware": {"state": "warn", "label": "2-channel firmware"},
    })

    view = build_microphone_settings_view(status)

    assert view["echo"]["mode"] == "direct_mic"
    assert view["echo"]["state"] == "disclosed_stale"
    assert view["echo"]["action"] == "Re-flash the XVF to 6-channel firmware"
    assert all(
        toggle["enabled"] is False for toggle in view["fusion"]["toggles"]
    )


def test_direct_mic_view_keeps_no_aec_danger_choice_visible() -> None:
    status = _base_status()
    status["audio_profile"] = {
        "selection": "direct_mic",
        "requested": "direct_mic",
        "active": "direct_mic",
        "state": "disabled",
    }
    status["software_aec3"] = {
        "configured": False,
        "active": False,
        "bypassed": False,
    }
    status["microphone"].update({
        "name": "USB microphone",
        "variant_id": "generic_usb_mic",
        "geometry": "",
        "chip_beam_plan": "",
    })

    view = build_microphone_settings_view(status)
    direct = _choice(view, "direct_mic")

    assert view["echo"]["mode"] == "direct_mic"
    assert direct["selected"] is True
    assert direct["danger"] is True
    assert all(not toggle["enabled"] for toggle in view["fusion"]["toggles"])
    assert {
        toggle["disabled_reason"]
        for toggle in view["fusion"]["toggles"]
    } == {"Advanced wake streams require the AEC bridge."}


def test_stored_testing_profile_maps_to_hardware_without_legacy_control() -> None:
    status = _base_status()
    status["audio_profile"] = {
        "selection": "xvf_chip_aec_testing",
        "requested": "xvf_chip_aec_testing",
        "active": "xvf_software_aec3",
        "state": "fallback",
        "reason": "chip-AEC cannot be armed; using software AEC3.",
    }
    status["chip_aec_gate"] = {
        "status": "needs_calibration",
        "detail": "output DAC has no codified chip-AEC calibration",
        "mic_available": True,
        "production_available": False,
        "testing_available": False,
    }

    view = build_microphone_settings_view(status)
    hardware = _choice(view, "xvf_chip_aec")

    assert hardware["selected"] is True
    assert hardware["visible"] is True
    assert hardware["enabled"] is False
    assert hardware["status"] == "needs calibration"
    assert view["advanced"] == {}
    assert "xvf_chip_aec_testing" not in valid_profile_ids()
