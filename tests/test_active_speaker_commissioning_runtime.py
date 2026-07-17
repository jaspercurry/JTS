# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jasper.active_speaker import commissioning_runtime as runtime
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GraphSafety,
)


@pytest.fixture(autouse=True)
def _stable_no_bass_graph_authority(monkeypatch):
    """Legacy in-memory ports inherit one explicit canonical host decision."""

    async def classify(*_args, **_kwargs):
        return GraphSafety(
            classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            allowed=True,
            details={
                "bass_extension_profile_summary": {
                    "authority_valid": True,
                    "runtime_block_required": False,
                }
            },
        )

    monkeypatch.setattr(
        runtime,
        "classify_active_bass_extension_graph",
        classify,
    )
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.runtime_contract import (
    GRAPH_GUARDED_COMMISSIONING,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_camilla_graph as _classify_camilla_graph,
)
from jasper.audio_measurement.admitted_playback import GeneratedExcitationWav
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionAuthority,
    GenerationAdmissionArtifact,
    PlaybackAdmissionArtifact,
    admission_artifact_relative_path,
    canonical_admission_bytes,
)
from jasper.audio_measurement.null_walk import NullWalkSpec
from jasper.dsp_apply import (
    DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    DspWriterLockTimeout,
    dsp_writer_lock,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_runtime_contract import (
    _active_baseline_yaml,
    _active_topology,
)


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault(
        "bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY
    )
    return _classify_camilla_graph(*args, **kwargs)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_HASH_D = "d" * 64
_HASH_E = "e" * 64
_TOPOLOGY = mono_output_topology()


def _graph(*, woofer_delay: float, tweeter_delay: float) -> dict:
    return {
        "devices": {"volume_limit": -12.0},
        "filters": {
            "as_woofer_delay": {
                "type": "Delay",
                "parameters": {"delay": woofer_delay, "unit": "ms"},
            },
            "as_woofer_baseline_gain": {
                "type": "Gain",
                "parameters": {"gain": -1.0, "inverted": False, "mute": False},
            },
            "as_tweeter_delay": {
                "type": "Delay",
                "parameters": {"delay": tweeter_delay, "unit": "ms"},
            },
            "as_tweeter_baseline_gain": {
                "type": "Gain",
                "parameters": {"gain": -2.0, "inverted": False, "mute": False},
            },
        },
        "pipeline": [
            {
                "type": "Filter",
                "channels": [0],
                "names": ["as_woofer_delay", "as_woofer_baseline_gain"],
            },
            {
                "type": "Filter",
                "channels": [1],
                "names": ["as_tweeter_delay", "as_tweeter_baseline_gain"],
            },
        ],
    }


def _raw(graph: dict) -> str:
    return yaml.safe_dump(graph, sort_keys=False)


def _fake_predecessor_raw() -> str:
    text = _active_baseline_yaml("mono", 2)
    return (
        text.replace(
            "  as_woofer_delay:\n"
            "    type: Delay\n"
            "    parameters:\n"
            "      delay: 0.0000\n",
            "  as_woofer_delay:\n"
            "    type: Delay\n"
            "    parameters:\n"
            "      delay: 0.7000\n",
            1,
        )
        .replace(
            "  as_woofer_baseline_gain:\n"
            "    type: Gain\n"
            "    parameters: { gain: 0.0000, inverted: false, mute: false }\n",
            "  as_woofer_baseline_gain:\n"
            "    type: Gain\n"
            "    parameters: { gain: -1.0000, inverted: false, mute: false }\n",
            1,
        )
        .replace(
            "  as_tweeter_delay:\n"
            "    type: Delay\n"
            "    parameters:\n"
            "      delay: 0.0000\n",
            "  as_tweeter_delay:\n"
            "    type: Delay\n"
            "    parameters:\n"
            "      delay: 0.1000\n",
            1,
        )
        .replace(
            "  as_tweeter_baseline_gain:\n"
            "    type: Gain\n"
            "    parameters: { gain: 0.0000, inverted: false, mute: false }\n",
            "  as_tweeter_baseline_gain:\n"
            "    type: Gain\n"
            "    parameters: { gain: -2.0000, inverted: false, mute: false }\n",
            1,
        )
    )


class FakePort:
    def __init__(self) -> None:
        self.raw = _fake_predecessor_raw()
        self._authority_tmp = tempfile.TemporaryDirectory(
            prefix="jts-bass-authority-"
        )
        self._authority_dir = Path(self._authority_tmp.name)
        self.path = "/etc/camilladsp/applied.yml"
        self._authority_config_path = self._authority_dir / "applied.yml"
        self._authority_config_path.write_text(self.raw, encoding="utf-8")
        self._statefile_path = self._authority_dir / "statefile.yml"
        self._statefile_path.write_text(
            f"config_path: {self._authority_config_path}\n"
            "volume: -28.0\nmute: false\n",
            encoding="utf-8",
        )
        self._applied_baseline_path = self._authority_dir / "applied.json"
        self._applied_baseline_path.write_text(json.dumps({}), encoding="utf-8")
        self.volume = -28.0
        self.apply_calls: list[str] = []
        self.volume_calls: list[float] = []
        self.events: list[str] = []
        self.fail_apply_call: int | None = None
        self.fail_volume_call: int | None = None
        self.drift_path = False
        self.drift_volume = False
        self.corrupt_next_read = False

    async def read_active_raw(self) -> str:
        if self.corrupt_next_read:
            self.corrupt_next_read = False
            graph = yaml.safe_load(self.raw)
            graph["filters"]["as_woofer_baseline_gain"]["parameters"]["gain"] = -9.0
            return _raw(graph)
        return self.raw

    async def apply_active_raw(self, raw: str) -> bool:
        self.events.append("graph")
        self.apply_calls.append(raw)
        if self.fail_apply_call == len(self.apply_calls):
            return False
        self.raw = raw
        if self.drift_path:
            self.path = "/tmp/drift.yml"
        return True

    async def read_config_path(self) -> str:
        return self.path

    async def read_volume(self) -> float:
        return self.volume + (1.0 if self.drift_volume else 0.0)

    async def set_volume(self, value: float) -> bool:
        self.events.append("volume")
        self.volume_calls.append(value)
        if self.fail_volume_call == len(self.volume_calls):
            return False
        self.volume = value
        return True

    def port(self) -> runtime.CommissioningRuntimePort:
        return runtime.CommissioningRuntimePort(
            read_active_raw=self.read_active_raw,
            apply_active_raw=self.apply_active_raw,
            read_config_path=self.read_config_path,
            read_listening_volume_db=self.read_volume,
            set_listening_volume_db=self.set_volume,
            _bass_extension_authority_paths={
                "statefile_path": self._statefile_path,
                "applied_baseline_path": self._applied_baseline_path,
                "profile_path": self._authority_dir / "bass-profile.json",
                "intent_path": self._authority_dir / "bass-intent.json",
                "staged_metadata_path": self._authority_dir / "staged.json",
            },
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


def _commissioning_offsets(graph: dict) -> dict[tuple[int, ...], float]:
    offsets: dict[tuple[int, ...], float] = {}
    for step in graph["pipeline"]:
        names = step.get("names", [])
        offset_names = [
            name
            for name in names
            if isinstance(name, str)
            and name.startswith("as_commission_")
            and name.endswith("_offset")
        ]
        if offset_names:
            offsets[tuple(step["channels"])] = graph["filters"][offset_names[0]][
                "parameters"
            ]["delay"]
    return offsets


def _artifact(path: str, raw: bytes, *, bundle_id: str = "run-1") -> ArtifactIdentity:
    return ArtifactIdentity(
        bundle_kind="test_bundle",
        bundle_id=bundle_id,
        relative_path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def _admitted(payload: object = "capture") -> runtime.AdmittedCaptureCallbackResult:
    limits = ExcitationLimits(
        permitted_band=FrequencyBand(1900.0, 2100.0),
        maximum_effective_peak_dbfs=-20.0,
        maximum_duration_s=1.0,
        maximum_repeat_count=1,
        target_fingerprint=_HASH_A,
        safety_profile_fingerprint=_HASH_B,
        protection_requirement_fingerprint=_HASH_C,
        excitation_plan_fingerprint=_HASH_D,
    )
    request = ExcitationRequest(
        band=FrequencyBand(1950.0, 2050.0),
        effective_peak_dbfs=-24.0,
        duration_s=0.5,
        repeat_count=1,
        target_fingerprint=_HASH_A,
        safety_profile_fingerprint=_HASH_B,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=_HASH_D,
    )
    evidence = ProtectionEvidence(
        target_fingerprint=_HASH_A,
        safety_profile_fingerprint=_HASH_B,
        protection_requirement_fingerprint=_HASH_C,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=_HASH_D,
        evidence_fingerprint=_HASH_E,
        current=True,
    )
    decision = admit_excitation(request, limits, protection_evidence=evidence)
    marker = _artifact("admission_authority.json", b"marker")
    authority = AdmissionAuthority(
        directory=Path("/tmp/run-1"),
        bundle_kind="test_bundle",
        bundle_id="run-1",
        marker=marker,
        fingerprint=_HASH_A,
    )
    admission_id = "attempt-1"
    canonical = canonical_admission_bytes(decision)
    generation = GenerationAdmissionArtifact(
        authority=authority,
        admission_id=admission_id,
        admission=decision,
        artifact=_artifact(
            admission_artifact_relative_path("generation", admission_id), canonical
        ),
    )
    playback = PlaybackAdmissionArtifact(
        generation=generation,
        admission=decision,
        artifact=_artifact(
            admission_artifact_relative_path("playback", admission_id), canonical
        ),
    )
    stimulus = GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.artifact.fingerprint,
        excitation_plan_fingerprint=_HASH_D,
        artifact=_artifact("stimuli/attempt-1.wav", b"RIFFfake"),
    )
    return runtime.AdmittedCaptureCallbackResult(
        generation=generation,
        playback=playback,
        stimulus=stimulus,
        protection_evidence=evidence,
        payload=payload,
    )


def _journal(
    *,
    intents: list | None = None,
    restores: list | None = None,
) -> runtime.CommissioningMutationJournal:
    async def record_intent(predecessor) -> None:
        if intents is not None:
            intents.append(predecessor)

    async def record_restored(observation) -> None:
        if restores is not None:
            restores.append(observation)

    return runtime.CommissioningMutationJournal(record_intent, record_restored)


@pytest.mark.asyncio
async def test_normal_capture_holds_candidate_and_restores_exact_predecessor(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    predecessor_raw = fake.raw
    observed: list[runtime.CommissioningLiveContext] = []
    intents: list = []
    restores: list = []

    async def capture(context: runtime.CommissioningLiveContext):
        observed.append(context)
        candidate = yaml.safe_load(fake.raw)
        assert candidate == context.graph.normalized_active_raw
        assert candidate["filters"]["as_out0_commission_mute"]["parameters"] == {
            "gain": 0.0,
            "inverted": False,
            "mute": False,
        }
        assert candidate["filters"]["as_out1_commission_mute"]["parameters"] == {
            "gain": 0.0,
            "inverted": False,
            "mute": False,
        }
        return _admitted({"null_depth_db": 8.0})

    result = await runtime.run_summed_capture(
        fake.port(),
        _request(),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(intents=intents, restores=restores),
        config_dir=tmp_path,
    )

    assert result.capture.payload == {"null_depth_db": 8.0}
    assert result.graph_fingerprint == observed[0].graph.active_raw_fingerprint
    assert fake.raw == predecessor_raw
    assert fake.volume == -28.0
    assert fake.path == "/etc/camilladsp/applied.yml"
    assert len(fake.apply_calls) == 2
    assert fake.events[:2] == ["volume", "graph"]
    assert intents == [result.predecessor]
    assert restores == [result.restore]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["normal", "reverse", "delay"])
async def test_every_summed_candidate_caps_volume_at_the_measurement_level(
    tmp_path: Path,
    kind: runtime.SummedGraphKind,
) -> None:
    fake = FakePort()

    async def capture(context: runtime.CommissioningLiveContext):
        assert context.graph.normalized_active_raw["devices"]["volume_limit"] == -32.0
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        _request(kind),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


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


@pytest.mark.asyncio
async def test_fresh_readback_rereads_every_live_value_on_every_call(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    base = fake.port()
    reads = {"graph": 0, "path": 0, "volume": 0}
    retained: list[runtime.FreshCommissioningReadback] = []

    async def read_graph() -> str | None:
        reads["graph"] += 1
        return await base.read_active_raw()

    async def read_path() -> str | None:
        reads["path"] += 1
        return await base.read_config_path()

    async def read_volume() -> float | None:
        reads["volume"] += 1
        return await base.read_listening_volume_db()

    port = runtime.CommissioningRuntimePort(
        read_active_raw=read_graph,
        apply_active_raw=base.apply_active_raw,
        read_config_path=read_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=base.set_listening_volume_db,
    )

    async def capture(context: runtime.CommissioningLiveContext):
        retained.append(context.fresh_readback)
        before = dict(reads)
        first = await context.fresh_readback()
        assert reads == {name: count + 1 for name, count in before.items()}
        second = await context.fresh_readback()
        assert reads == {name: count + 2 for name, count in before.items()}
        for observation in (first, second):
            assert observation.graph == context.graph
            assert observation.active_raw == context.active_raw
            assert observation.config_path == context.config_path
            assert observation.listening_volume_db == context.listening_volume_db
            assert observation.delay_confirmation is None
        return _admitted()

    await runtime.run_summed_capture(
        port,
        _request(),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )

    with pytest.raises(runtime.CommissioningRuntimeError, match="live callback"):
        await retained[0]()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("graph", "post_capture_graph_drift"),
        ("path", "post_capture_config_path_drift"),
        ("volume", "post_capture_volume_drift"),
    ],
)
async def test_fresh_readback_refuses_drift_before_low_level_play(
    tmp_path: Path,
    drift: str,
    code: str,
) -> None:
    fake = FakePort()
    base = fake.port()
    drift_next = False
    low_level_play_calls = 0

    async def read_graph() -> str | None:
        nonlocal drift_next
        raw = await base.read_active_raw()
        if drift_next and drift == "graph":
            drift_next = False
            graph = yaml.safe_load(raw)
            graph["filters"]["as_woofer_baseline_gain"]["parameters"]["gain"] = -9.0
            return _raw(graph)
        return raw

    async def read_path() -> str | None:
        nonlocal drift_next
        if drift_next and drift == "path":
            drift_next = False
            return "/tmp/drift.yml"
        return await base.read_config_path()

    async def read_volume() -> float | None:
        nonlocal drift_next
        value = await base.read_listening_volume_db()
        if drift_next and drift == "volume":
            drift_next = False
            assert value is not None
            return value + 1.0
        return value

    port = runtime.CommissioningRuntimePort(
        read_active_raw=read_graph,
        apply_active_raw=base.apply_active_raw,
        read_config_path=read_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=base.set_listening_volume_db,
    )

    async def capture(context: runtime.CommissioningLiveContext):
        nonlocal drift_next, low_level_play_calls
        drift_next = True
        await context.fresh_readback()
        low_level_play_calls += 1
        return _admitted()

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            port,
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == code
    assert low_level_play_calls == 0
    assert raised.value.side_effects.restore_succeeded is True


@pytest.mark.asyncio
async def test_mutation_intent_failure_refuses_before_live_apply(tmp_path: Path) -> None:
    fake = FakePort()

    async def fail_intent(_predecessor) -> None:
        raise OSError("intent persistence failed")

    async def restored(_observation) -> None:
        pytest.fail("no restore marker exists without an intent")

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("capture cannot begin without a durable rollback anchor")

    journal = runtime.CommissioningMutationJournal(fail_intent, restored)
    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=journal,
            config_dir=tmp_path,
        )

    assert raised.value.code == "mutation_intent_failed"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_safe_volume_failure_refuses_before_graph_apply_and_audio(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    fake.fail_volume_call = 1

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("capture cannot begin before the safe volume is proven")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "volume_apply_failed"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert raised.value.side_effects.audio_may_have_emitted is False
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.events == ["volume", "graph", "volume"]


@pytest.mark.asyncio
async def test_cancellation_during_safe_volume_set_restores_without_audio(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    volume_started = asyncio.Event()
    calls = 0

    async def set_volume(value: float) -> bool:
        nonlocal calls
        calls += 1
        fake.events.append("volume")
        fake.volume_calls.append(value)
        if calls == 1:
            volume_started.set()
            await asyncio.Event().wait()
        fake.volume = value
        return True

    base_port = fake.port()
    port = runtime.CommissioningRuntimePort(
        read_active_raw=base_port.read_active_raw,
        apply_active_raw=base_port.apply_active_raw,
        read_config_path=base_port.read_config_path,
        read_listening_volume_db=base_port.read_listening_volume_db,
        set_listening_volume_db=set_volume,
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("capture cannot begin before the safe volume is proven")

    task = asyncio.create_task(
        runtime.run_summed_capture(
            port,
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )
    )
    await volume_started.wait()
    task.cancel()
    with pytest.raises(runtime.CommissioningRuntimeCancelled) as raised:
        await task

    assert raised.value.side_effects.graph_may_have_mutated is False
    assert raised.value.side_effects.audio_may_have_emitted is False
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.events == ["volume", "graph", "volume"]


@pytest.mark.asyncio
async def test_restore_marker_failure_reports_known_live_restore(tmp_path: Path) -> None:
    fake = FakePort()
    predecessor_raw = fake.raw

    async def intent(_predecessor) -> None:
        return None

    async def fail_restored(_observation) -> None:
        raise OSError("terminal marker failed")

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=runtime.CommissioningMutationJournal(
                intent, fail_restored
            ),
            config_dir=tmp_path,
        )

    assert raised.value.code == "restore_record_failed"
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.raw == predecessor_raw


@pytest.mark.asyncio
async def test_durable_predecessor_can_be_exactly_recovered(tmp_path: Path) -> None:
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    result = await runtime.run_summed_capture(
        fake.port(),
        _request(),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )
    fake.raw = _request("reverse").normal_active_raw
    fake.volume = -10.0

    recovery = await runtime.recover_summed_predecessor(
        fake.port(), result.predecessor, config_dir=tmp_path
    )

    assert recovery.cancelled is False
    assert recovery.observation.graph.normalized_active_raw == (
        result.predecessor.state["normalized_active_raw"]
    )
    assert recovery.observation.config_path == result.predecessor.state["config_path"]
    assert fake.volume == -28.0


@pytest.mark.asyncio
async def test_normal_graph_may_equal_predecessor_and_volume_is_still_restored(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    request = _request()
    fake.raw = request.normal_active_raw

    async def capture(context: runtime.CommissioningLiveContext):
        assert context.graph.normalized_active_raw == yaml.safe_load(fake.raw)
        assert fake.volume == -32.0
        return _admitted()

    result = await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )

    assert result.predecessor.state["listening_volume_db"] == -28.0
    assert fake.raw == request.normal_active_raw
    assert fake.volume == -28.0


@pytest.mark.asyncio
async def test_reverse_adds_only_upper_scoped_inversion_lane(tmp_path: Path) -> None:
    fake = FakePort()
    normal = yaml.safe_load(_request().normal_active_raw)

    async def capture(context: runtime.CommissioningLiveContext):
        reverse = context.graph.normalized_active_raw
        expected = yaml.safe_load(_request().normal_active_raw)
        assert reverse["filters"]["as_tweeter_baseline_gain"] == expected[
            "filters"
        ]["as_tweeter_baseline_gain"]
        lanes = _commissioning_lanes(reverse)
        assert len(lanes) == 1
        step, delay, identity = lanes[0]
        assert step["channels"] == [1]
        assert delay == {"delay": 0.0, "unit": "ms"}
        assert identity == {"gain": 0.0, "inverted": True, "mute": False}
        assert reverse != normal
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        _request("reverse"),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_delay_uses_zero_relative_snapshot_and_exact_confirmation(
    tmp_path: Path,
) -> None:
    fake = FakePort()

    async def capture(context: runtime.CommissioningLiveContext):
        assert context.delay_confirmation is not None
        assert context.delay_confirmation.relative_delay_us == 100.0
        fresh = await context.fresh_readback()
        assert fresh.delay_confirmation == context.delay_confirmation
        assert fresh.graph == context.graph
        graph = context.graph.normalized_active_raw
        lanes = {tuple(step["channels"]): (delay, identity) for step, delay, identity in _commissioning_lanes(graph)}
        assert lanes[(0,)][0]["delay"] == 0.1
        assert lanes[(0,)][1]["inverted"] is False
        assert lanes[(1,)][0]["delay"] == 0.0
        assert lanes[(1,)][1]["inverted"] is True
        return _admitted()

    result = await runtime.run_summed_capture(
        fake.port(),
        _request("delay"),
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )

    assert result.delay_confirmation is not None
    assert len(fake.apply_calls) == 3  # zero-relative, candidate, exact restore


@pytest.mark.asyncio
async def test_zero_delay_reuses_the_proven_zero_relative_graph(tmp_path: Path) -> None:
    fake = FakePort()
    request = _request("delay")
    assert request.delay_spec is not None
    request = replace(
        request,
        delay_candidate=request.delay_spec.dsp_candidate(0.0),
    )

    async def capture(context: runtime.CommissioningLiveContext):
        assert context.delay_confirmation is not None
        assert context.delay_confirmation.relative_delay_us == 0.0
        assert context.delay_confirmation.readback_relative_delay_us == 0.0
        return _admitted()

    result = await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )

    assert result.delay_confirmation is not None
    assert len(fake.apply_calls) == 2  # zero-relative plus exact restore


@pytest.mark.asyncio
async def test_delay_scoped_offsets_zero_unequal_emitter_baseline(
    tmp_path: Path,
) -> None:
    original_raw = _request("delay").normal_active_raw
    graph = yaml.safe_load(original_raw)
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = 0.7
    graph["filters"]["as_tweeter_delay"]["parameters"]["delay"] = 0.1
    source = next(line for line in original_raw.splitlines() if line.startswith("# Source:"))
    request = replace(
        _request("delay"),
        normal_active_raw=f"{source}\n{_raw(graph)}",
    )
    fake = FakePort()
    fake.raw = request.normal_active_raw

    async def capture(context: runtime.CommissioningLiveContext):
        assert context.delay_confirmation is not None
        assert context.delay_confirmation.readback_relative_delay_us == 100.0
        candidate = context.graph.normalized_active_raw
        lanes = {
            tuple(step["channels"]): delay["delay"]
            for step, delay, _identity in _commissioning_lanes(candidate)
        }
        offsets = _commissioning_offsets(candidate)
        lower_total_ms = 0.7 + offsets[(0,)] + lanes[(0,)]
        upper_total_ms = 0.1 + offsets[(1,)] + lanes[(1,)]
        assert lower_total_ms == pytest.approx(0.8)
        assert upper_total_ms == pytest.approx(0.7)
        assert (lower_total_ms - upper_total_ms) * 1000.0 == pytest.approx(100.0)
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=_TOPOLOGY,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_delay_rechecks_safe_volume_before_second_graph_apply(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    reads = 0

    async def read_volume() -> float:
        nonlocal reads
        reads += 1
        if reads == 4:
            return -31.0
        return fake.volume

    base_port = fake.port()
    port = runtime.CommissioningRuntimePort(
        read_active_raw=base_port.read_active_raw,
        apply_active_raw=base_port.apply_active_raw,
        read_config_path=base_port.read_config_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=base_port.set_listening_volume_db,
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("volume drift must refuse before the delay candidate apply")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            port,
            _request("delay"),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "volume_readback_mismatch"
    assert raised.value.side_effects.graph_may_have_mutated is True
    assert raised.value.side_effects.audio_may_have_emitted is True
    assert raised.value.side_effects.restore_succeeded is True
    assert len(fake.apply_calls) == 2  # zero-relative plus exact restore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("way_count", "lower_role", "upper_role", "lower_channel", "upper_channel"),
    [
        (2, "woofer", "tweeter", 0, 1),
        (2, "woofer", "tweeter", 2, 3),
        (3, "mid", "tweeter", 1, 2),
        (3, "mid", "tweeter", 4, 5),
    ],
)
async def test_stereo_delay_lanes_are_scoped_to_one_adjacent_group(
    tmp_path: Path,
    way_count: int,
    lower_role: str,
    upper_role: str,
    lower_channel: int,
    upper_channel: int,
) -> None:
    mode = f"active_{way_count}_way"
    topology = _active_topology("stereo", mode)
    request = _request(
        "delay",
        topology=topology,
        normal_active_raw=_active_baseline_yaml("stereo", way_count),
        lower_role=lower_role,
        upper_role=upper_role,
        lower_channels=(lower_channel,),
        upper_channels=(upper_channel,),
    )
    fake = FakePort()
    fake.raw = request.normal_active_raw

    async def capture(context: runtime.CommissioningLiveContext):
        graph = context.graph.normalized_active_raw
        normal = yaml.safe_load(request.normal_active_raw)
        assert graph["pipeline"][: len(normal["pipeline"])] == normal["pipeline"]
        assert all(
            graph["filters"][name] == definition
            for name, definition in normal["filters"].items()
        )
        assert classify_camilla_graph(
            topology=topology,
            text=context.active_raw,
        ).allowed
        lanes = _commissioning_lanes(graph)
        assert {tuple(step["channels"]) for step, _, _ in lanes} == {
            (lower_channel,),
            (upper_channel,),
        }
        by_channel = {
            tuple(step["channels"]): (delay, identity)
            for step, delay, identity in lanes
        }
        assert by_channel[(lower_channel,)][0]["delay"] == 0.1
        assert by_channel[(upper_channel,)][0]["delay"] == 0.0
        assert by_channel[(upper_channel,)][1]["inverted"] is True
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=topology,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_three_way_capture_mutes_sibling_and_other_speaker_outputs(
    tmp_path: Path,
) -> None:
    topology = _active_topology("stereo", "active_3_way")
    request = _request(
        topology=topology,
        normal_active_raw=_active_baseline_yaml("stereo", 3),
        lower_role="mid",
        upper_role="tweeter",
        lower_channels=(1,),
        upper_channels=(2,),
    )
    fake = FakePort()
    fake.raw = request.normal_active_raw

    async def capture(context: runtime.CommissioningLiveContext):
        graph = context.graph.normalized_active_raw
        mute_states = {
            index: graph["filters"][f"as_out{index}_commission_mute"][
                "parameters"
            ]["mute"]
            for index in range(6)
        }
        assert mute_states == {
            0: True,
            1: False,
            2: False,
            3: True,
            4: True,
            5: True,
        }
        assert graph["pipeline"][-6:] == [
            {
                "type": "Filter",
                "channels": [index],
                "names": [f"as_out{index}_commission_mute"],
            }
            for index in range(6)
        ]
        safety = classify_camilla_graph(topology=topology, text=context.active_raw)
        assert safety.allowed is True
        assert safety.classification == GRAPH_GUARDED_COMMISSIONING
        assert safety.details["baseline_commissioning_group"] == "left"
        assert safety.details["baseline_commissioning_roles"] == ["mid", "tweeter"]
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=topology,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("way_count", "upper_role", "lower_channel", "upper_channel"),
    [
        (2, "tweeter", 0, 1),
        (2, "tweeter", 2, 3),
        (3, "mid", 0, 1),
        (3, "mid", 3, 4),
    ],
)
async def test_stereo_reverse_is_scoped_to_one_group(
    tmp_path: Path,
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
    fake = FakePort()
    fake.raw = request.normal_active_raw

    async def capture(context: runtime.CommissioningLiveContext):
        assert classify_camilla_graph(
            topology=topology,
            text=context.active_raw,
        ).allowed
        lanes = _commissioning_lanes(context.graph.normalized_active_raw)
        assert len(lanes) == 1
        step, delay, identity = lanes[0]
        assert step["channels"] == [upper_channel]
        assert delay["delay"] == 0.0
        assert identity["inverted"] is True
        return _admitted()

    await runtime.run_summed_capture(
        fake.port(),
        request,
        capture,
        topology=topology,
        mutation_journal=_journal(),
        config_dir=tmp_path,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_ms", [-0.1, 20.1])
async def test_normal_graph_refuses_delay_outside_shared_ceiling(
    tmp_path: Path,
    delay_ms: float,
) -> None:
    fake = FakePort()
    graph = yaml.safe_load(_request().normal_active_raw)
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = delay_ms
    request = replace(_request(), normal_active_raw=_raw(graph))

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("out-of-bounds delay must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "normal_graph_invalid"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["mono", "stereo"])
async def test_normal_graph_refuses_unsafe_non_target_driver_delay(
    tmp_path: Path,
    layout: str,
) -> None:
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
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("an unsafe non-target role must refuse the whole graph")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=topology,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "normal_graph_invalid"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_delay_walk_refuses_without_headroom_above_emitter_baseline(
    tmp_path: Path,
) -> None:
    original_raw = _request("delay").normal_active_raw
    graph = yaml.safe_load(original_raw)
    graph["filters"]["as_woofer_delay"]["parameters"]["delay"] = 19.9
    graph["filters"]["as_tweeter_delay"]["parameters"]["delay"] = 19.9
    source = next(line for line in original_raw.splitlines() if line.startswith("# Source:"))
    request = replace(
        _request("delay"),
        normal_active_raw=f"{source}\n{_raw(graph)}",
    )
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("delay walk without physical headroom must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "normal_graph_invalid"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lower_role", "upper_role", "lower_channels", "upper_channels"),
    [
        ("woofer", "tweeter", (0,), (2,)),
        ("woofer", "mid", (0,), (4,)),
    ],
)
async def test_request_refuses_non_adjacent_or_cross_group_binding_before_lock(
    tmp_path: Path,
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
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("invalid topology binding must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeError, match="adjacent region"):
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=topology,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_callback_failure_restores_and_classifies_possible_audio(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    predecessor = fake.raw

    async def capture(_context: runtime.CommissioningLiveContext):
        raise RuntimeError("capture worker failed")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "capture_failed"
    assert raised.value.side_effects.audio_may_have_emitted is True
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.raw == predecessor


@pytest.mark.asyncio
async def test_cleanup_failure_dominates_callback_cancellation(tmp_path: Path) -> None:
    fake = FakePort()
    fake.fail_apply_call = 2

    async def capture(_context: runtime.CommissioningLiveContext):
        raise asyncio.CancelledError

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "restore_failed"
    assert raised.value.cancelled is True
    assert raised.value.side_effects.restore_succeeded is False


@pytest.mark.asyncio
async def test_restore_continues_after_one_adapter_raises(tmp_path: Path) -> None:
    fake = FakePort()
    base = fake.port()
    apply_calls = 0
    restoring = False
    restore_events: list[str] = []

    async def apply_active_raw(raw: str) -> bool:
        nonlocal apply_calls, restoring
        apply_calls += 1
        if apply_calls == 2:
            restoring = True
            raise RuntimeError("restore graph transport failed")
        return await base.apply_active_raw(raw)

    async def set_volume(value: float) -> bool:
        if restoring:
            restore_events.append("volume")
        return await base.set_listening_volume_db(value)

    async def read_active_raw() -> str | None:
        if restoring:
            restore_events.append("graph_readback")
        return await base.read_active_raw()

    async def read_config_path() -> str | None:
        if restoring:
            restore_events.append("path_readback")
        return await base.read_config_path()

    async def read_volume() -> float | None:
        if restoring:
            restore_events.append("volume_readback")
        return await base.read_listening_volume_db()

    port = runtime.CommissioningRuntimePort(
        read_active_raw=read_active_raw,
        apply_active_raw=apply_active_raw,
        read_config_path=read_config_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=set_volume,
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            port,
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "restore_failed"
    assert fake.volume == -32.0
    assert restore_events == [
        "graph_readback",
        "path_readback",
    ]


@pytest.mark.asyncio
async def test_cancellation_during_restore_continues_remaining_cleanup(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    base = fake.port()
    apply_calls = 0
    restore_started = asyncio.Event()
    restore_events: list[str] = []

    async def apply_active_raw(raw: str) -> bool:
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 2:
            restore_started.set()
            await asyncio.Event().wait()
        return await base.apply_active_raw(raw)

    async def set_volume(value: float) -> bool:
        if restore_started.is_set():
            restore_events.append("volume")
        return await base.set_listening_volume_db(value)

    async def read_config_path() -> str | None:
        if restore_started.is_set():
            restore_events.append("path_readback")
        return await base.read_config_path()

    async def read_volume() -> float | None:
        if restore_started.is_set():
            restore_events.append("volume_readback")
        return await base.read_listening_volume_db()

    port = runtime.CommissioningRuntimePort(
        read_active_raw=base.read_active_raw,
        apply_active_raw=apply_active_raw,
        read_config_path=read_config_path,
        read_listening_volume_db=read_volume,
        set_listening_volume_db=set_volume,
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    task = asyncio.create_task(
        runtime.run_summed_capture(
            port,
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )
    )
    await restore_started.wait()
    task.cancel()
    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await task

    assert raised.value.code == "restore_failed"
    assert raised.value.cancelled is True
    assert fake.volume == -32.0
    assert restore_events == ["path_readback"]


@pytest.mark.asyncio
async def test_external_cancellation_stops_interruptible_capture_and_restores(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    started = asyncio.Event()
    interrupted = asyncio.Event()

    async def capture(_context: runtime.CommissioningLiveContext):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    task = asyncio.create_task(
        runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(runtime.CommissioningRuntimeCancelled) as raised:
        await task

    assert raised.value.completed_result is None
    assert interrupted.is_set()
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.volume == -28.0


@pytest.mark.asyncio
async def test_cancel_suppressed_by_late_callback_reports_completed_result(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    started = asyncio.Event()

    async def capture(_context: runtime.CommissioningLiveContext):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _admitted("late")

    task = asyncio.create_task(
        runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(runtime.CommissioningRuntimeCancelled) as raised:
        await task

    assert raised.value.completed_result is not None
    assert raised.value.completed_result.capture.payload == "late"
    assert raised.value.side_effects.restore_succeeded is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("graph", "post_capture_graph_drift"),
        ("volume", "post_capture_volume_drift"),
    ],
)
async def test_post_capture_drift_refuses_result_and_restores(
    tmp_path: Path,
    drift: str,
    code: str,
) -> None:
    fake = FakePort()
    predecessor_raw = fake.raw

    async def capture(_context: runtime.CommissioningLiveContext):
        if drift == "graph":
            graph = yaml.safe_load(fake.raw)
            graph["filters"]["as_woofer_baseline_gain"]["parameters"][
                "gain"
            ] = -9.0
            fake.raw = _raw(graph)
        else:
            fake.volume = -31.0
        return _admitted()

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == code
    assert raised.value.side_effects.restore_succeeded is True
    assert fake.raw == predecessor_raw
    assert fake.volume == -28.0


@pytest.mark.asyncio
async def test_unsafe_normal_graph_is_refused_before_mutation(tmp_path: Path) -> None:
    fake = FakePort()
    request = replace(
        _request(),
        normal_active_raw=_raw(_graph(woofer_delay=0.2, tweeter_delay=0.1)),
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("unsafe graph must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "unsafe_normal_graph"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filter_name", "definition"),
    [
        (
            "forged_post_limiter_gain",
            {
                "type": "Gain",
                "parameters": {"gain": 60.0, "inverted": False, "mute": False},
            },
        ),
        (
            "forged_post_limiter_peq",
            {
                "type": "Biquad",
                "parameters": {
                    "type": "Peaking",
                    "freq": 2000.0,
                    "q": 1.0,
                    "gain": 60.0,
                },
            },
        ),
    ],
)
async def test_forged_post_limiter_filter_is_refused_before_mutation(
    tmp_path: Path,
    filter_name: str,
    definition: dict,
) -> None:
    fake = FakePort()
    base = _request().normal_active_raw
    graph = yaml.safe_load(base)
    graph["filters"][filter_name] = definition
    graph["pipeline"].append(
        {
            "type": "Filter",
            "channels": [0, 1],
            "names": [filter_name],
        }
    )
    source = next(line for line in base.splitlines() if line.startswith("# Source:"))
    request = replace(_request(), normal_active_raw=f"{source}\n{_raw(graph)}")

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("unapproved post-limiter filter must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "unsafe_normal_graph"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_normal_graph_cannot_predeclare_reserved_runtime_lane(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    base = _request().normal_active_raw
    graph = yaml.safe_load(base)
    graph["filters"]["as_commission_forged_delay"] = {
        "type": "Delay",
        "parameters": {"delay": 1.0, "unit": "ms"},
    }
    graph["pipeline"].append(
        {
            "type": "Filter",
            "channels": [0],
            "names": ["as_commission_forged_delay"],
        }
    )
    source = next(line for line in base.splitlines() if line.startswith("# Source:"))
    request = replace(_request(), normal_active_raw=f"{source}\n{_raw(graph)}")

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("caller-supplied runtime lane must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "normal_graph_invalid"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_normal_graph_cannot_supply_output_isolation_mutes(
    tmp_path: Path,
) -> None:
    fake = FakePort()
    base = _request().normal_active_raw
    graph = yaml.safe_load(base)
    graph["filters"]["as_out0_commission_mute"] = {
        "type": "Gain",
        "parameters": {"gain": 0.0, "inverted": False, "mute": False},
    }
    graph["pipeline"].append(
        {
            "type": "Filter",
            "channels": [0],
            "names": ["as_out0_commission_mute"],
        }
    )
    source = next(line for line in base.splitlines() if line.startswith("# Source:"))
    request = replace(_request(), normal_active_raw=f"{source}\n{_raw(graph)}")

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("caller-supplied isolation mute must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            request,
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "normal_graph_invalid"
    assert raised.value.side_effects.graph_may_have_mutated is False
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_fresh_candidate_readback_is_reclassified_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePort()
    predecessor_raw = fake.raw
    classify_calls = 0

    def classify(*args, **kwargs):
        nonlocal classify_calls
        classify_calls += 1
        decision = classify_camilla_graph(*args, **kwargs)
        if classify_calls == 2:
            return replace(
                decision,
                allowed=False,
                issues=({"severity": "blocker", "code": "probe_unsafe"},),
            )
        return decision

    monkeypatch.setattr(runtime, "classify_camilla_graph", classify)

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("unsafe fresh readback must not reach capture")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "graph_readback_unsafe"
    assert raised.value.side_effects.graph_may_have_mutated is True
    assert raised.value.side_effects.audio_may_have_emitted is True
    assert raised.value.side_effects.restore_succeeded is True
    assert classify_calls == 2
    assert fake.raw == predecessor_raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drift", "code"),
    [("path", "config_path_drift"), ("volume", "volume_readback_mismatch")],
)
async def test_fresh_readback_drift_refuses_and_restores_when_possible(
    tmp_path: Path, drift: str, code: str
) -> None:
    fake = FakePort()
    if drift == "path":
        fake.drift_path = True
    else:
        fake.drift_volume = True

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("capture must not start after readback drift")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            fake.port(),
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "restore_failed"
    assert code in {"config_path_drift", "volume_readback_mismatch"}
    assert raised.value.side_effects.audio_may_have_emitted is (drift == "path")


@pytest.mark.asyncio
async def test_wrong_graph_readback_refuses_before_audio_and_restores(
    tmp_path: Path,
) -> None:
    fake = FakePort()

    async def apply_with_corrupt_read(raw: str) -> bool:
        applied = await FakePort.apply_active_raw(fake, raw)
        if len(fake.apply_calls) == 1:
            fake.corrupt_next_read = True
        return applied

    port = fake.port()
    port = runtime.CommissioningRuntimePort(
        read_active_raw=port.read_active_raw,
        apply_active_raw=apply_with_corrupt_read,
        read_config_path=port.read_config_path,
        read_listening_volume_db=port.read_listening_volume_db,
        set_listening_volume_db=port.set_listening_volume_db,
    )

    async def capture(_context: runtime.CommissioningLiveContext):
        pytest.fail("capture must not start after graph readback drift")

    with pytest.raises(runtime.CommissioningRuntimeFailure) as raised:
        await runtime.run_summed_capture(
            port,
            _request(),
            capture,
            topology=_TOPOLOGY,
            mutation_journal=_journal(),
            config_dir=tmp_path,
        )

    assert raised.value.code == "graph_readback_mismatch"
    assert raised.value.side_effects.audio_may_have_emitted is True
    assert raised.value.side_effects.restore_succeeded is True


@pytest.mark.asyncio
async def test_shared_lock_default_and_explicit_bound_refuse_before_mutation(
    tmp_path: Path,
) -> None:
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    async with dsp_writer_lock(tmp_path, source="test_holder"):
        contender = asyncio.create_task(
            runtime.run_summed_capture(
                fake.port(),
                _request(),
                capture,
                topology=_TOPOLOGY,
                mutation_journal=_journal(),
                config_dir=tmp_path,
                lock_timeout_s=0.001,
            )
        )
        with pytest.raises(DspWriterLockTimeout) as raised:
            await contender

    assert (
        runtime.DEFAULT_SUMMED_RUNTIME_LOCK_TIMEOUT_S
        == DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S
    )
    assert raised.value.timeout_s == 0.001
    assert fake.apply_calls == []


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_lock_is_pre_mutation(
    tmp_path: Path,
) -> None:
    fake = FakePort()

    async def capture(_context: runtime.CommissioningLiveContext):
        return _admitted()

    async with dsp_writer_lock(tmp_path, source="test_holder"):
        task = asyncio.create_task(
            runtime.run_summed_capture(
                fake.port(),
                _request(),
                capture,
                topology=_TOPOLOGY,
                mutation_journal=_journal(),
                config_dir=tmp_path,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(runtime.CommissioningRuntimeCancelled) as raised:
            await task

    assert raised.value.side_effects == runtime.RuntimeSideEffectState(
        False, False, False, None
    )
    assert fake.apply_calls == []


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

    assert prepared.limits.permitted_band == FrequencyBand(20.0, 20000.0)
    assert prepared.limits.maximum_effective_peak_dbfs == -48.0
    assert prepared.limits.maximum_duration_s == 8.0
    assert prepared.limits.maximum_repeat_count == 1
    assert prepared.request.repeat_count == 1
    assert prepared.request.target_fingerprint == _HASH_A
    assert prepared.minimum_cooldown_s == 2.0


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
