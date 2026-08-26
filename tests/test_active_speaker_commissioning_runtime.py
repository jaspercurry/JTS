# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from jasper.active_speaker import commissioning_runtime as runtime


from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.audio_measurement.excitation_admission import (
    FrequencyBand,
)
from jasper.audio_measurement.null_walk import NullWalkSpec
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_HASH_D = "d" * 64
_HASH_E = "e" * 64
_TOPOLOGY = mono_output_topology()


def _raw(graph: dict) -> str:
    return yaml.safe_dump(graph, sort_keys=False)


def _request(
    kind: runtime.SummedGraphKind = "normal",
    *,
    topology=_TOPOLOGY,
    normal_active_raw: str | None = None,
    lower_role: str = "woofer",
    upper_role: str = "tweeter",
    lower_channels: tuple[int, ...] = (0,),
    upper_channels: tuple[int, ...] = (1,),
) -> runtime.SummedGraphRequest:
    values = dict(
        kind=kind,
        normal_active_raw=normal_active_raw or _active_baseline_yaml("mono", 2),
        lower_role=lower_role,
        upper_role=upper_role,
        lower_channels=lower_channels,
        upper_channels=upper_channels,
        listening_volume_db=-32.0,
        topology_id=topology.topology_id,
        topology_fingerprint=topology_config_fingerprint(topology),
    )
    if kind == "delay":
        spec = NullWalkSpec(
            crossover_fc_hz=2000.0,
            geometry_seed_us=0.0,
            positive_delay_target=lower_role,
            negative_delay_target=upper_role,
        )
        values.update(
            delay_spec=spec,
            delay_candidate=spec.dsp_candidate(100.0),
            delay_scope="active_crossover",
        )
    return runtime.SummedGraphRequest(**values)


def test_summed_candidate_does_not_relax_a_quieter_inherited_volume_limit() -> None:
    request = _request()
    graph = yaml.safe_load(request.normal_active_raw)
    graph["devices"]["volume_limit"] = -40.0
    request = replace(request, normal_active_raw=_raw(graph))

    normal = runtime._normal_graph(
        request,
        runtime._topology_binding(request, _TOPOLOGY),
    )

    assert normal["devices"]["volume_limit"] == -40.0


def test_two_driver_profile_composition_intersects_existing_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = mono_output_topology()
    current = runtime.active_driver_targets(topology)
    fingerprints = tuple(target["target_fingerprint"] for target in current)
    profile = {
        "targets": [
            {
                "target_fingerprint": fingerprints[0],
                "hard_excitation_band_hz": [1.0, 40000.0],
                "measurement_band_hz": [1.0, 30000.0],
                "required_protection_filters": [{"kind": "lowpass"}],
                # This fixture DECLARES a peak on purpose; the key is optional
                # (2026-08-23) and the omitted shape has its own case, see
                # ``test_summed_excitation_composes_when_no_driver_declares_a_level_limit``.
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -24.0,
                    "max_sweep_duration_s": 20.0,
                    "max_repeat_count": 3,
                    "minimum_cooldown_s": 0.5,
                },
            },
            {
                "target_fingerprint": fingerprints[1],
                "hard_excitation_band_hz": [2.0, 50000.0],
                "measurement_band_hz": [2.0, 40000.0],
                "required_protection_filters": [{"kind": "highpass"}],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -48.0,
                    "max_sweep_duration_s": 10.0,
                    "max_repeat_count": 2,
                    "minimum_cooldown_s": 2.0,
                },
            },
        ]
    }
    monkeypatch.setattr(
        runtime,
        "evaluate_driver_safety_profile",
        lambda *_args: SimpleNamespace(
            confirmed_and_current=True, profile_fingerprint=_HASH_B
        ),
    )

    prepared = runtime.prepare_summed_excitation(
        topology,
        profile,
        target_fingerprints=fingerprints,
        evidence_target_fingerprint=_HASH_A,
        band=FrequencyBand(1950.0, 2050.0),
        effective_peak_dbfs=-50.0,
        duration_s=0.8,
        excitation_plan_fingerprint=_HASH_D,
    )

    # Upper edge is the global MAX_DRIVER_TEST_FREQUENCY_HZ ceiling (sweep-
    # composition PR-A, #1668: 20_000.0 -> 23_000.0) -- both drivers' hard
    # AND measurement bands here sit well above it, so the shared constant
    # is what binds, not either driver's own declared band.
    assert prepared.limits.permitted_band == FrequencyBand(20.0, 23000.0)
    assert prepared.limits.maximum_effective_peak_dbfs == -48.0
    assert prepared.limits.maximum_duration_s == 8.0
    assert prepared.limits.maximum_repeat_count == 1
    assert prepared.request.repeat_count == 1
    assert prepared.request.target_fingerprint == _HASH_A
    assert prepared.minimum_cooldown_s == 2.0


def test_summed_excitation_composes_when_no_driver_declares_a_level_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary reply shape, which used to raise here (2026-08-23).

    ``max_effective_peak_dbfs`` became optional when the research ask stopped
    demanding a class-default figure. This function indexed it, so a profile
    from a pair of drivers whose makers publish no level limit raised
    ``KeyError`` and surfaced as "driver safety profile target limits are
    incomplete" -- a message describing nothing wrong. The fixture above
    hard-codes the key present and so cannot see that; this one omits it.

    With no declaration the ceiling is each role's class default, and the
    summed path takes the ``min`` because one signal reaches both drivers: the
    woofer's 0.0 dBFS against the dome tweeter's -65.0.

    Mutation guard: index the key again and this raises
    ``CommissioningRuntimeError``.
    """

    topology = mono_output_topology()
    current = runtime.active_driver_targets(topology)
    fingerprints = tuple(target["target_fingerprint"] for target in current)

    def _target(fingerprint: str, role: str, style: str | None) -> dict:
        return {
            "target_fingerprint": fingerprint,
            "role": role,
            **({"driver_style": style} if style else {}),
            "hard_excitation_band_hz": [100.0, 40000.0],
            "measurement_band_hz": [500.0, 30000.0],
            "required_protection_filters": [
                {"kind": "lowpass" if role == "woofer" else "highpass"}
            ],
            # No max_effective_peak_dbfs: neither maker publishes one.
            "level_duration_limits": {
                "max_sweep_duration_s": 20.0,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0.5,
            },
        }

    profile = {
        "targets": [
            _target(fingerprints[0], "woofer", None),
            _target(fingerprints[1], "tweeter", "dome_tweeter"),
        ]
    }
    monkeypatch.setattr(
        runtime,
        "evaluate_driver_safety_profile",
        lambda *_args: SimpleNamespace(
            confirmed_and_current=True, profile_fingerprint=_HASH_B
        ),
    )

    prepared = runtime.prepare_summed_excitation(
        topology,
        profile,
        target_fingerprints=fingerprints,
        evidence_target_fingerprint=_HASH_A,
        band=FrequencyBand(1950.0, 2050.0),
        effective_peak_dbfs=-70.0,
        duration_s=0.8,
        excitation_plan_fingerprint=_HASH_D,
    )
    assert prepared.limits.maximum_effective_peak_dbfs == -65.0
    assert prepared.minimum_cooldown_s == 0.5


def test_summed_low_edge_is_owned_by_the_declared_measurement_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summed low edge is the highest declared ``measurement_band_hz[0]``.

    The band above binds on the shared ``MIN_DRIVER_TEST_FREQUENCY_HZ``
    constant at both drivers, so nothing there exercises the low edge's own
    rule. This does: both measurement floors sit above the constant, and the
    higher of the two must win.

    #2603 removed a second ``max()`` over ``hard_excitation_band_hz[0]`` from
    this edge. No test can DISCRIMINATE that removal, because the term was
    dead on every input the function can receive -- ``apply_driver_low_limit``
    stamps ``measurement[0]`` at or above ``hard[0]``, and a profile that
    escapes that nesting is refused by ``_target_issues`` before
    ``confirmed_and_current`` can be true. So this pins the rule that SURVIVES,
    with a fixture that honours the nesting invariant the removal rests on
    (each ``measurement`` band sits inside its own ``hard`` band).
    """

    topology = mono_output_topology()
    current = runtime.active_driver_targets(topology)
    fingerprints = tuple(target["target_fingerprint"] for target in current)
    profile = {
        "targets": [
            {
                "target_fingerprint": fingerprints[0],
                "hard_excitation_band_hz": [100.0, 40000.0],
                "measurement_band_hz": [500.0, 30000.0],
                "required_protection_filters": [{"kind": "lowpass"}],
                # This fixture DECLARES a peak on purpose; the key is optional
                # (2026-08-23) and the omitted shape has its own case, see
                # ``test_summed_excitation_composes_when_no_driver_declares_a_level_limit``.
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -24.0,
                    "max_sweep_duration_s": 20.0,
                    "max_repeat_count": 3,
                    "minimum_cooldown_s": 0.5,
                },
            },
            {
                "target_fingerprint": fingerprints[1],
                "hard_excitation_band_hz": [200.0, 50000.0],
                "measurement_band_hz": [900.0, 20000.0],
                "required_protection_filters": [{"kind": "highpass"}],
                "level_duration_limits": {
                    "max_effective_peak_dbfs": -48.0,
                    "max_sweep_duration_s": 10.0,
                    "max_repeat_count": 2,
                    "minimum_cooldown_s": 2.0,
                },
            },
        ]
    }
    monkeypatch.setattr(
        runtime,
        "evaluate_driver_safety_profile",
        lambda *_args: SimpleNamespace(
            confirmed_and_current=True, profile_fingerprint=_HASH_B
        ),
    )

    prepared = runtime.prepare_summed_excitation(
        topology,
        profile,
        target_fingerprints=fingerprints,
        evidence_target_fingerprint=_HASH_A,
        band=FrequencyBand(1000.0, 1100.0),
        effective_peak_dbfs=-50.0,
        duration_s=0.8,
        excitation_plan_fingerprint=_HASH_D,
    )

    # 900 is the higher declared measurement floor -- not 500 (the lower one),
    # not 200 (the higher HARD floor), and not the 20 Hz shared constant.
    assert prepared.limits.permitted_band == FrequencyBand(900.0, 20000.0)


def test_summed_excitation_uses_role_pair_ssot_not_channel_tuple_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _active_topology("mono", "active_3_way").to_dict()
    channels = raw["speaker_groups"][0]["channels"]
    raw["speaker_groups"][0]["channels"] = [
        channels[0],
        channels[2],
        channels[1],
    ]
    topology = OutputTopology.from_mapping(raw)
    by_role = {
        target["role"]: target
        for target in runtime.active_driver_targets(topology)
    }
    monkeypatch.setattr(
        runtime,
        "evaluate_driver_safety_profile",
        lambda *_args: SimpleNamespace(
            confirmed_and_current=True,
            profile_fingerprint=_HASH_B,
        ),
    )

    with pytest.raises(runtime.CommissioningRuntimeError, match="must be adjacent"):
        runtime.prepare_summed_excitation(
            topology,
            {"targets": []},
            target_fingerprints=(
                by_role["woofer"]["target_fingerprint"],
                by_role["tweeter"]["target_fingerprint"],
            ),
            evidence_target_fingerprint=_HASH_A,
            band=FrequencyBand(1950.0, 2050.0),
            effective_peak_dbfs=-50.0,
            duration_s=0.8,
            excitation_plan_fingerprint=_HASH_D,
        )
