# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.chip_aec import health as health
from jasper.audio_profile_state import (
    AecIntent, MicProbe, build_audio_profile_status, runtime_env_from_mapping,
)

_CHIP_INTENT = AecIntent(
    mode="auto", chip_aec_enabled=True, profile_selection="xvf_chip_aec"
)
_MANAGED_XVF = MicProbe(xvf_present=True, capture_channels=6, recommended_channels=6)
_APPROVED_GATE = {"status": "approved", "auto_allowed": True}

# Every disposition the judge answers for, against the status/action pair
# consumers match on.  `reason` is operator prose: the pin is that it is
# non-empty, and byte-identical for the three the ladder snapshots below carry.
_APPLIED_VERDICTS = {
    "applied-clean-is-ready": (
        {}, health.STATUS_READY, "",
    ),
    "applied-on-a-shipped-class-row-discloses": (
        {"shipped_label": "xvf3800-square/pcm5122"},
        health.STATUS_DISCLOSED_STALE, health.ACTION_RECOMMISSION,
    ),
    "per-unit-divergence-discloses": (
        {"identity_diff": ("xvf_serial", "output_hardware_key")},
        health.STATUS_DISCLOSED_STALE, health.ACTION_RECOMMISSION,
    ),
    "class-divergence-discloses": (
        {"identity_diff": ("xvf_serial", "output_rate")},
        health.STATUS_DISCLOSED_STALE, health.ACTION_RECOMMISSION,
    ),
}
_FAILURE_VERDICTS = {
    health.COMMISSION_REQUIRED: (
        health.STATUS_DISCLOSED_STALE, health.ACTION_RECOMMISSION,
    ),
    health.OUTPUTD_ENV_STALE: (
        health.STATUS_DISCLOSED_STALE, health.ACTION_WAIT_FOR_OUTPUTD,
    ),
    health.REAPPLY_FAILED: (health.STATUS_FAULT, health.ACTION_INSPECT_ALIGNMENT),
    health.REFERENCE_PRODUCER_DOWN: (
        health.STATUS_FAULT, health.ACTION_INSPECT_OUTPUTD,
    ),
    health.BRIDGE_FAILED: (health.STATUS_FAULT, health.ACTION_INSPECT_BRIDGE),
}


def _assert_verdict(verdict, status, action):
    assert (verdict.status, verdict.action) == (status, action)
    assert verdict.selection == "xvf_chip_aec"
    assert verdict.reason
    assert health.AlignmentHealth.from_env(verdict.to_env()) == verdict


@pytest.mark.parametrize(
    "kwargs,status,action",
    list(_APPLIED_VERDICTS.values()),
    ids=list(_APPLIED_VERDICTS),
)
def test_an_applied_pass_is_ready_or_discloses(kwargs, status, action):
    _assert_verdict(
        health.alignment_health(health.APPLIED, selection="xvf_chip_aec", **kwargs),
        status,
        action,
    )


@pytest.mark.parametrize(
    "disposition,expected",
    list(_FAILURE_VERDICTS.items()),
    ids=list(_FAILURE_VERDICTS),
)
def test_every_failed_pass_names_a_remedy(disposition, expected):
    _assert_verdict(
        health.alignment_health(disposition, selection="xvf_chip_aec"), *expected
    )


def test_alignment_health_refuses_an_unknown_disposition():
    with pytest.raises(ValueError):
        health.alignment_health("half_applied")


def test_from_env_reads_an_absent_record_as_no_verdict():
    assert health.AlignmentHealth.from_env({}) == health.AlignmentHealth("")


# What `/state` published for each record on the ladder before the record
# became one typed object — captured from origin/main at f116741ee and asserted
# unchanged.  `chip_aec_gate` is echoed back verbatim and is elided.
_LADDER_SNAPSHOTS = {
    "ready": (
        health.alignment_health(health.APPLIED, selection="xvf_chip_aec"),
        {
            "selection": "xvf_chip_aec",
            "requested": "xvf_chip_aec",
            "active": "xvf_chip_aec",
            "state": "active",
            "reason": "Chip-AEC runtime env is applied.",
            "validation_profile": "xvf_chip_aec",
            "action": "",
            "commission_recommended": False,
        },
    ),
    "disclosed_stale": (
        health.alignment_health(
            health.APPLIED,
            selection="xvf_chip_aec",
            identity_diff=("xvf_serial", "output_hardware_key"),
        ),
        {
            "selection": "xvf_chip_aec",
            "requested": "xvf_chip_aec",
            "active": "xvf_chip_aec",
            "state": "disclosed_stale",
            "reason": "commissioned alignment was measured on a different unit "
                      "(xvf_serial, output_hardware_key)",
            "validation_profile": "xvf_chip_aec",
            "action": "Run sudo jasper-aec-commission",
            "commission_recommended": True,
        },
    ),
    "fault": (
        health.alignment_health(health.REAPPLY_FAILED, selection="xvf_chip_aec"),
        {
            "selection": "xvf_chip_aec",
            "requested": "xvf_chip_aec",
            "active": None,
            "state": "fault",
            "reason": "silent chip-AEC alignment reapply failed",
            "validation_profile": "xvf_chip_aec",
            "action": "Inspect jasper-aec-init and jasper-outputd, then run the "
                      "reconciler",
            "commission_recommended": False,
        },
    ),
}


@pytest.mark.parametrize(
    "verdict,expected", list(_LADDER_SNAPSHOTS.values()), ids=list(_LADDER_SNAPSHOTS)
)
def test_state_ladder_is_unchanged_by_the_typed_record(verdict, expected):
    status = build_audio_profile_status(
        _CHIP_INTENT,
        runtime_env_from_mapping(
            {
                "JASPER_MIC_DEVICE": "udp:9876",
                "JASPER_MIC_DEVICE_RAW": "udp:9877",
                "JASPER_AEC_CHIP_AEC_ENABLED": "1",
                **verdict.to_env(),
            }
        ),
        _MANAGED_XVF,
        bridge_active=True,
        chip_available=True,
        chip_gate=_APPROVED_GATE,
    )
    published = dict(status["audio_profile"])
    published.pop("chip_aec_gate")

    assert published == expected
