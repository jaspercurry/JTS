# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign's plans, composed from the applied candidate's own field.

The family here is the REAL sealed one and the summaries are the REAL
``graph_summary``; only the ONE composer is stubbed (its own suite proves the
emission), so what these pin is what the bench does with a rung: which graph it
asks for, which authority it proves it against, and what it refuses before any
device is opened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jasper.bass_extension.bench import plan as plan_module
from jasper.bass_extension.bench.plan import (
    REFUSE_GRAPH_UNAVAILABLE,
    REFUSE_MARGIN_MISMATCH,
    REFUSE_NO_APPLIED_FAMILY,
    REFUSE_OWNER_TARGET_UNMAPPED,
    REFUSE_TARGET_NOT_IN_FAMILY,
    bench_role_targets,
    campaign_measured_context,
    target_plans,
)
from jasper.bass_extension.bench.context import transparency_policy_fingerprint
from jasper.bass_extension.bench.runner import BenchRefused
from jasper.bass_extension.candidate_field import graph_summary
from tests.test_bass_extension_candidate_field import bass_extension_field

BOOSTED_ID = "t31.86"
LIMITER = "as_woofer_baseline_limiter"
BASELINE_CLIP_LIMIT_DBFS = -12.0
SAMPLE_RATE_HZ = 48_000


def applied_profile(**overrides: Any) -> dict[str, Any]:
    """One applied baseline snapshot carrying the sealed family."""

    return {
        "recomposition_snapshot": {
            "bass_extension": bass_extension_field(boosted=True, **overrides)
        }
    }


def graph_text(*, limiter: str = LIMITER, target_id: str = "") -> str:
    """A graph shaped only where the plan reads it: the limiter and the rate."""

    return yaml.safe_dump(
        {
            "devices": {"samplerate": SAMPLE_RATE_HZ},
            "filters": {
                limiter: {
                    "type": "Limiter",
                    "parameters": {
                        "clip_limit": BASELINE_CLIP_LIMIT_DBFS,
                        "soft_clip": True,
                    },
                },
            },
            "pipeline": [{"type": "Filter", "channels": [0, 1], "names": [limiter]}],
            "emitted_target": target_id,
        }
    )


@pytest.fixture
def composer(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every rung the ONE composer is asked for, and answer with a graph."""

    calls: list[dict[str, Any]] = []

    def _recompose(topology: Any, **kwargs: Any) -> str:
        calls.append({"topology": topology, **kwargs})
        return graph_text(target_id=str(kwargs["bass_target_id"]))

    monkeypatch.setattr(
        plan_module, "recompose_active_baseline_for_bass_extension", _recompose
    )
    return calls


def _campaign(*target_ids: str, **overrides: Any):
    return target_plans(
        object(),
        overrides.pop("applied", applied_profile()),
        target_ids=target_ids,
        current_config_path=Path("/var/lib/camilladsp/configs/selected.yml"),
        margin_policy_name=overrides.pop("margin_policy_name", "conservative"),
    )


def test_every_named_rung_is_planned_from_the_family_it_was_sized_in(
    composer: list[dict[str, Any]],
) -> None:
    campaign = _campaign(BOOSTED_ID, "natural")
    plans = campaign.plans

    assert campaign.selected_config_path == Path(
        "/var/lib/camilladsp/configs/selected.yml"
    )
    assert [plan.target_id for plan in plans] == [BOOSTED_ID, "natural"]
    assert [call["bass_target_id"] for call in composer] == [BOOSTED_ID, "natural"]
    for plan in plans:
        assert plan.limiter_name == LIMITER
        assert plan.owner_channels == (0, 1)
        assert plan.baseline_clip_limit_dbfs == BASELINE_CLIP_LIMIT_DBFS
        # The graph the plan carries is the one composed for ITS rung.
        assert yaml.safe_load(plan.graph_raw_text)["emitted_target"] == plan.target_id
    # Two rungs of one family are two targets, never one measured twice.
    assert plans[0].target_fingerprint != plans[1].target_fingerprint


def test_a_boosted_rung_is_proved_against_its_own_transform(
    composer: list[dict[str, Any]],
) -> None:
    """The summary a plan carries is the rung's, so the read-back proof accepts
    the transform the graph actually holds instead of the natural identity."""

    boosted, natural = _campaign(BOOSTED_ID, "natural").plans

    assert boosted.profile_summary["natural"]["target_id"] == BOOSTED_ID
    assert boosted.profile_summary["natural"]["lt"]["freq_target"] == pytest.approx(
        boosted.profile_summary["natural"]["fp_hz"]
    )
    assert boosted.profile_summary["boost_cap_db"] == 6.0
    assert boosted.boost_headroom_db == pytest.approx(5.815987996931181)
    assert "lt" not in natural.profile_summary["natural"]
    assert natural.boost_headroom_db == 0.0


@pytest.mark.parametrize(
    ("applied", "reason"),
    [
        (None, REFUSE_NO_APPLIED_FAMILY),
        ({}, REFUSE_NO_APPLIED_FAMILY),
        ({"recomposition_snapshot": {"bass_extension": {"owner": {}}}},
         REFUSE_NO_APPLIED_FAMILY),
    ],
)
def test_a_speaker_with_no_readable_family_has_nothing_to_bench(
    composer: list[dict[str, Any]], applied: Any, reason: str
) -> None:
    with pytest.raises(BenchRefused) as raised:
        _campaign("natural", applied=applied)

    assert raised.value.reason == reason
    assert composer == []


def test_a_rung_the_family_does_not_carry_is_refused(
    composer: list[dict[str, Any]],
) -> None:
    with pytest.raises(BenchRefused) as raised:
        _campaign("t20.00")

    assert raised.value.reason == REFUSE_TARGET_NOT_IN_FAMILY


def test_a_campaign_under_another_margin_policy_is_refused(
    composer: list[dict[str, Any]],
) -> None:
    """The margin policy sizes the family AND bounds the campaign's analysis;
    two of them is two different speakers' evidence in one bundle."""

    with pytest.raises(BenchRefused) as raised:
        _campaign("natural", margin_policy_name="aggressive")

    assert raised.value.reason == REFUSE_MARGIN_MISMATCH
    assert composer == []


def test_a_composer_refusal_lands_as_the_benchs_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    def _refuse(topology: Any, **kwargs: Any) -> str:
        raise CarrierCannotHostEq("bass_extension_recompose_unavailable", "nope")

    monkeypatch.setattr(
        plan_module, "recompose_active_baseline_for_bass_extension", _refuse
    )

    with pytest.raises(BenchRefused) as raised:
        _campaign("natural")

    assert raised.value.reason == REFUSE_GRAPH_UNAVAILABLE


def test_a_graph_without_the_owners_limiter_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plan_module,
        "recompose_active_baseline_for_bass_extension",
        lambda topology, **kwargs: graph_text(limiter="as_tweeter_baseline_limiter"),
    )

    with pytest.raises(BenchRefused) as raised:
        _campaign("natural")

    assert raised.value.reason == REFUSE_GRAPH_UNAVAILABLE


def test_the_sub_owner_is_mapped_to_its_declared_safety_target() -> None:
    """A local subwoofer is no declared driver role, so the conductor's map
    never carries it; without this the re-admission would refuse the segment
    AFTER the rung's graph was already activated."""

    safety = {
        "targets": [
            {"role": "woofer", "target_fingerprint": "w" * 64},
            {"role": "subwoofer", "target_fingerprint": "s" * 64},
        ]
    }

    assert bench_role_targets(
        {"woofer": "w" * 64}, safety, owner_role="subwoofer"
    ) == {"woofer": "w" * 64, "subwoofer": "s" * 64}


def test_an_owner_the_safety_profile_does_not_declare_refuses_before_audio() -> None:
    with pytest.raises(BenchRefused) as raised:
        bench_role_targets(
            {"woofer": "w" * 64},
            {"targets": [{"role": "woofer", "target_fingerprint": "w" * 64}]},
            owner_role="subwoofer",
        )

    assert raised.value.reason == REFUSE_OWNER_TARGET_UNMAPPED


def test_measured_context_carries_exactly_the_frozen_fields(
    composer: list[dict[str, Any]],
) -> None:
    from jasper.bass_extension.bench.manifest import (
        STIMULUS_ROLES,
        author_campaign_manifest,
    )
    from jasper.bass_extension.limiter_evidence import _CONTEXT_FIELDS

    request = {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -30.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 12.0,
        "requested_cooldown_s": 4.0,
        "requested_repeat_count": 2,
        "stimulus_generator_identity": "gen-v1",
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 536_870_912,
        "render_rlimit_cpu_s": 60,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.25,
        "cross_check_read_count": 40,
        "cross_check_tolerance_db": 1.5,
    }
    manifest = author_campaign_manifest(
        {
            "driver_safety_fingerprint": "d" * 64,
            "margin_policy_name": "conservative",
            "margin_policy_fingerprint": "m" * 64,
            "requests": {
                target: {role: dict(request) for role in STIMULUS_ROLES}
                for target in (BOOSTED_ID, "natural")
            },
        },
        target_ids=(BOOSTED_ID, "natural"),
    )
    campaign = _campaign(BOOSTED_ID, "natural")

    context = campaign_measured_context(
        campaign,
        manifest=manifest,
        camilladsp_build_id="camilladsp-v4.1.3-abc",
        tap_implementation_id="t" * 64,
        natural_graph_fingerprint="n" * 64,
    )

    assert set(context) == set(_CONTEXT_FIELDS)
    assert context["target_order"] == [
        {"target_id": plan.target_id, "target_fingerprint": plan.target_fingerprint}
        for plan in campaign.plans
    ]
    # The policy the analysis applies is the one the bundle binds by fingerprint.
    assert context["transparency_policy_fingerprint"] == (
        transparency_policy_fingerprint()
    )
    assert context["owner_channels"] == [0, 1]
    assert context["sample_rate_hz"] == SAMPLE_RATE_HZ
    assert context["limiter_name"] == LIMITER
    assert context["baseline_limiter_clip_limit_dbfs"] == BASELINE_CLIP_LIMIT_DBFS
    assert context["driver_safety_fingerprint"] == "d" * 64


def test_the_planned_graph_is_one_the_benchs_own_read_back_proof_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rung's graph and the rung's authority are two halves of ONE plan.

    Nothing here is stubbed but the file read: the emitter composes the rung,
    the plan carries it beside ``graph_summary``'s authority for that same
    rung, and the seam that runs before any unmute
    (``activation._prove_active_graph``) is what accepts them together.
    """

    from jasper.bass_extension.bench.activation import (
        ActivationError,
        ActivationProof,
        _prove_active_graph,
    )
    from tests.test_active_speaker_runtime_contract import _active_baseline_yaml

    field = bass_extension_field(
        boosted=True, owner={"role": "woofer", "channels": [0]}
    )
    target_id = field["rungs"][0]["target"]["target_id"]
    emitted = _active_baseline_yaml(
        "mono", 2, bass_extension=field, bass_target_id=target_id
    )
    monkeypatch.setattr(
        plan_module,
        "recompose_active_baseline_for_bass_extension",
        lambda topology, **kwargs: emitted,
    )

    (plan,) = target_plans(
        object(),
        {"recomposition_snapshot": {"bass_extension": field}},
        target_ids=(target_id,),
        current_config_path=Path("/var/lib/camilladsp/configs/selected.yml"),
        margin_policy_name="conservative",
    ).plans

    configured = _prove_active_graph(
        yaml.safe_load(plan.graph_raw_text),
        ActivationProof(
            limiter_name=plan.limiter_name,
            owner_channels=plan.owner_channels,
            profile_summary=plan.profile_summary,
            expected_clip_limit_dbfs=plan.baseline_clip_limit_dbfs,
        ),
    )

    assert configured == plan.baseline_clip_limit_dbfs
    # The natural rung's authority does NOT prove this graph: the plan's
    # summary is the rung's own, not the family's last member's.
    with pytest.raises(ActivationError):
        _prove_active_graph(
            yaml.safe_load(plan.graph_raw_text),
            ActivationProof(
                limiter_name=plan.limiter_name,
                owner_channels=plan.owner_channels,
                profile_summary=graph_summary(field, target_id="natural"),
                expected_clip_limit_dbfs=plan.baseline_clip_limit_dbfs,
            ),
        )
