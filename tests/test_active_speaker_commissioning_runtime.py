# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from jasper.active_speaker import commissioning_runtime as runtime
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.runtime_contract import (
    GRAPH_GUARDED_COMMISSIONING,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph as _classify_camilla_graph,
)
from jasper.audio_measurement.excitation_admission import (
    FrequencyBand,
)
from jasper.audio_measurement.null_walk import NullWalkSpec
from jasper.output_topology import OutputTopology
from tests._async_wait import wait_signalled
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
)


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault("bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY)
    return _classify_camilla_graph(*args, **kwargs)


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_D = "d" * 64
_TOPOLOGY = mono_output_topology()


def _raw(graph: dict) -> str:
    return yaml.safe_dump(graph, sort_keys=False)


def _regained(raw: str) -> str:
    """One exactly-identifiable graph mutation, for predecessor-vs-live tests."""

    graph = yaml.safe_load(raw)
    graph["filters"]["as_woofer_baseline_gain"]["parameters"]["gain"] = -9.0
    return _raw(graph)


class FakePort:
    """In-memory DSP double that echoes the applied graph back verbatim.

    Real CamillaDSP default-fills its readback instead
    (``tests/_camilla_readback_double.py``); the exact-restore proofs below
    only need the echo.
    """

    def __init__(self) -> None:
        self.raw = _active_baseline_yaml("mono", 2)
        self.path = "/etc/camilladsp/applied.yml"
        self.volume = -28.0

    async def read_active_raw(self) -> str:
        return self.raw

    async def canonicalize_raw(self, raw: str) -> str:
        return raw

    async def apply_active_raw(self, raw: str) -> bool:
        self.raw = raw
        return True

    async def read_config_path(self) -> str:
        return self.path

    async def read_volume(self) -> float:
        return self.volume

    async def set_volume(self, value: float) -> bool:
        self.volume = value
        return True

    def port(self) -> runtime.CommissioningRuntimePort:
        return runtime.CommissioningRuntimePort(
            read_active_raw=self.read_active_raw,
            apply_active_raw=self.apply_active_raw,
            read_config_path=self.read_config_path,
            read_listening_volume_db=self.read_volume,
            set_listening_volume_db=self.set_volume,
            canonicalize_raw=self.canonicalize_raw,
        )


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


def _candidate(request: runtime.SummedGraphRequest, topology) -> dict:
    binding = runtime._topology_binding(request, topology)
    return runtime._stationary_candidate(
        request, runtime._normal_graph(request, binding), binding
    )


def _commissioning_lanes(graph: dict) -> list[tuple[dict, dict, dict]]:
    lanes: list[tuple[dict, dict, dict]] = []
    for step in graph["pipeline"]:
        names = step.get("names", [])
        scoped = [
            name
            for name in names
            if isinstance(name, str) and name.startswith("as_commission_")
        ]
        if not scoped:
            continue
        delay_name = next(name for name in scoped if name.endswith("_delay"))
        identity_name = next(name for name in scoped if name.endswith("_identity"))
        lanes.append(
            (
                step,
                graph["filters"][delay_name]["parameters"],
                graph["filters"][identity_name]["parameters"],
            )
        )
    return lanes


@pytest.mark.parametrize("kind", ["normal", "reverse", "delay"])
def test_every_summed_candidate_caps_volume_at_the_measurement_level(
    kind: runtime.SummedGraphKind,
) -> None:
    request = _request(kind)
    inherited = yaml.safe_load(request.normal_active_raw)["devices"]["volume_limit"]
    assert inherited > -32.0, "the cap, not the inherited ceiling, must be what binds"

    normal = runtime._normal_graph(
        request, runtime._topology_binding(request, _TOPOLOGY)
    )

    assert normal["devices"]["volume_limit"] == -32.0


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


def test_reverse_adds_only_upper_scoped_inversion_lane() -> None:
    request = _request("reverse")
    binding = runtime._topology_binding(request, _TOPOLOGY)
    normal = runtime._normal_graph(request, binding)

    candidate = runtime._stationary_candidate(request, normal, binding)

    assert _commissioning_lanes(normal) == []
    assert (
        candidate["filters"]["as_tweeter_baseline_gain"]
        == normal["filters"]["as_tweeter_baseline_gain"]
    )
    lanes = _commissioning_lanes(candidate)
    assert len(lanes) == 1
    step, delay, identity = lanes[0]
    assert step["channels"] == [1]
    assert delay == {"delay": 0.0, "unit": "ms"}
    assert identity == {"gain": 0.0, "inverted": True, "mute": False}


@pytest.mark.parametrize(
    ("way_count", "upper_role", "lower_channel", "upper_channel"),
    [
        (2, "tweeter", 0, 1),
        (2, "tweeter", 2, 3),
        (3, "mid", 0, 1),
        (3, "mid", 3, 4),
    ],
)
def test_stereo_reverse_is_scoped_to_one_group(
    way_count: int,
    upper_role: str,
    lower_channel: int,
    upper_channel: int,
) -> None:
    topology = _active_topology("stereo", f"active_{way_count}_way")
    request = _request(
        "reverse",
        topology=topology,
        normal_active_raw=_active_baseline_yaml("stereo", way_count),
        lower_role="woofer",
        upper_role=upper_role,
        lower_channels=(lower_channel,),
        upper_channels=(upper_channel,),
    )

    lanes = _commissioning_lanes(_candidate(request, topology))

    assert len(lanes) == 1
    step, delay, identity = lanes[0]
    assert step["channels"] == [upper_channel]
    assert delay["delay"] == 0.0
    assert identity["inverted"] is True


def test_three_way_candidate_mutes_sibling_and_other_speaker_outputs() -> None:
    topology = _active_topology("stereo", "active_3_way")
    baseline_raw = _active_baseline_yaml("stereo", 3)
    request = _request(
        topology=topology,
        normal_active_raw=baseline_raw,
        lower_role="mid",
        upper_role="tweeter",
        lower_channels=(1,),
        upper_channels=(2,),
    )

    candidate = _candidate(request, topology)

    mute_states = {
        index: candidate["filters"][f"as_out{index}_commission_mute"]["parameters"][
            "mute"
        ]
        for index in range(6)
    }
    assert mute_states == {0: True, 1: False, 2: False, 3: True, 4: True, 5: True}
    assert candidate["pipeline"][-6:] == [
        {
            "type": "Filter",
            "channels": [index],
            "names": [f"as_out{index}_commission_mute"],
        }
        for index in range(6)
    ]
    # The composer/classifier seam: the mute tail this module emits is what the
    # host recognises as a guarded commissioning graph. The classifier's own
    # verdict detail is pinned in tests/test_active_speaker_runtime_contract.py.
    safety = classify_camilla_graph(
        topology=topology,
        text=runtime._dump_graph(
            candidate, source_header=runtime._source_header(baseline_raw)
        ),
    )
    assert safety.allowed is True
    assert safety.classification == GRAPH_GUARDED_COMMISSIONING


@pytest.mark.parametrize("delay_ms", [-0.1, 20.1])
def test_normal_graph_refuses_delay_outside_shared_ceiling(delay_ms: float) -> None:
    graph = yaml.safe_load(_request().normal_active_raw)
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = delay_ms
    request = replace(_request(), normal_active_raw=_raw(graph))

    with pytest.raises(runtime.CommissioningRuntimeError):
        runtime._normal_graph(
            request, runtime._topology_binding(request, _TOPOLOGY)
        )


@pytest.mark.parametrize("layout", ["mono", "stereo"])
def test_normal_graph_refuses_unsafe_non_target_driver_delay(layout: str) -> None:
    topology = _active_topology(layout, "active_3_way")
    graph = yaml.safe_load(_active_baseline_yaml(layout, 3))
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = 25.0
    request = _request(
        topology=topology,
        normal_active_raw=_raw(graph),
        lower_role="mid",
        upper_role="tweeter",
        lower_channels=(1,),
        upper_channels=(2,),
    )

    with pytest.raises(runtime.CommissioningRuntimeError):
        runtime._normal_graph(
            request, runtime._topology_binding(request, topology)
        )


def test_delay_walk_refuses_without_headroom_above_emitter_baseline() -> None:
    original_raw = _request("delay").normal_active_raw
    graph = yaml.safe_load(original_raw)
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = 19.9
    graph["filters"]["as_tweeter_delay"]["parameters"]["delay"] = 19.9
    source = next(
        line for line in original_raw.splitlines() if line.startswith("# Source:")
    )
    request = replace(
        _request("delay"), normal_active_raw=f"{source}\n{_raw(graph)}"
    )

    with pytest.raises(runtime.CommissioningRuntimeError):
        runtime._normal_graph(
            request, runtime._topology_binding(request, _TOPOLOGY)
        )

    # Both baselines are inside the per-role 0-20 ms bound, so the refusal above
    # is the walk's headroom rule and not that bound: the same walk from a
    # zero-delay baseline is admitted.
    admitted = _request("delay")
    runtime._normal_graph(
        admitted, runtime._topology_binding(admitted, _TOPOLOGY)
    )


@pytest.mark.parametrize(
    ("filter_name", "definition"),
    [
        (
            "as_commission_forged_delay",
            {"type": "Delay", "parameters": {"delay": 1.0, "unit": "ms"}},
        ),
        (
            "as_out0_commission_mute",
            {
                "type": "Gain",
                "parameters": {"gain": 0.0, "inverted": False, "mute": False},
            },
        ),
    ],
    ids=["runtime_lane", "output_mute"],
)
def test_normal_graph_refuses_caller_supplied_runtime_lane_or_mute(
    filter_name: str,
    definition: dict,
) -> None:
    base = _request().normal_active_raw
    graph = yaml.safe_load(base)
    graph["filters"][filter_name] = definition
    graph["pipeline"].append(
        {"type": "Filter", "channels": [0], "names": [filter_name]}
    )
    source = next(line for line in base.splitlines() if line.startswith("# Source:"))
    request = replace(_request(), normal_active_raw=f"{source}\n{_raw(graph)}")

    with pytest.raises(runtime.CommissioningRuntimeError):
        runtime._normal_graph(
            request, runtime._topology_binding(request, _TOPOLOGY)
        )


@pytest.mark.parametrize(
    ("lower_role", "upper_role", "lower_channels", "upper_channels"),
    [
        ("woofer", "tweeter", (0,), (2,)),
        ("woofer", "mid", (0,), (4,)),
    ],
    ids=["non_adjacent", "cross_group"],
)
def test_binding_refuses_non_adjacent_or_cross_group_roles(
    lower_role: str,
    upper_role: str,
    lower_channels: tuple[int, ...],
    upper_channels: tuple[int, ...],
) -> None:
    topology = _active_topology("stereo", "active_3_way")
    request = _request(
        topology=topology,
        normal_active_raw=_active_baseline_yaml("stereo", 3),
        lower_role=lower_role,
        upper_role=upper_role,
        lower_channels=lower_channels,
        upper_channels=upper_channels,
    )

    with pytest.raises(runtime.CommissioningRuntimeError):
        runtime._topology_binding(request, topology)


async def test_restore_continues_after_one_adapter_raises() -> None:
    fake = FakePort()
    base = fake.port()
    predecessor = await runtime.snapshot_exact_dsp_state(base)
    fake.raw = _regained(fake.raw)
    fake.volume = -32.0
    events: list[str] = []

    async def apply_active_raw(_raw_text: str) -> bool:
        raise RuntimeError("restore graph transport failed")

    async def set_volume(value: float) -> bool:
        events.append("volume")
        return await fake.set_volume(value)

    async def read_active_raw() -> str:
        events.append("graph_readback")
        return await fake.read_active_raw()

    async def read_config_path() -> str:
        events.append("path_readback")
        return await fake.read_config_path()

    async def read_volume() -> float:
        events.append("volume_readback")
        return await fake.read_volume()

    port = replace(
        base,
        apply_active_raw=apply_active_raw,
        read_active_raw=read_active_raw,
        read_config_path=read_config_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=set_volume,
    )

    with pytest.raises(runtime.CommissioningRuntimeError):
        await runtime.restore_exact_dsp_state_locked(port, predecessor)

    # The raise does not abort the transaction: graph and path are still read
    # back, and the unproved graph stops the volume write rather than the
    # exception doing it.
    assert events == ["graph_readback", "path_readback"]
    assert fake.volume == -32.0


async def test_cancellation_during_restore_continues_remaining_cleanup() -> None:
    fake = FakePort()
    base = fake.port()
    predecessor = await runtime.snapshot_exact_dsp_state(base)
    fake.raw = _regained(fake.raw)
    fake.volume = -32.0
    restore_started = asyncio.Event()
    events: list[str] = []

    async def apply_active_raw(raw_text: str) -> bool:
        restore_started.set()
        await asyncio.Event().wait()
        return await fake.apply_active_raw(raw_text)

    async def set_volume(value: float) -> bool:
        events.append("volume")
        return await fake.set_volume(value)

    async def read_config_path() -> str:
        events.append("path_readback")
        return await fake.read_config_path()

    async def read_volume() -> float:
        events.append("volume_readback")
        return await fake.read_volume()

    port = replace(
        base,
        apply_active_raw=apply_active_raw,
        read_config_path=read_config_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=set_volume,
    )
    task = asyncio.create_task(
        runtime.restore_exact_dsp_state_locked(port, predecessor)
    )
    await wait_signalled(restore_started, "restore apply began", producer=task)
    task.cancel()

    with pytest.raises(runtime.CommissioningRuntimeError):
        await task

    assert events == ["path_readback"]
    assert fake.volume == -32.0


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
