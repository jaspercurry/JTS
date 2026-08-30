# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The delay sweep driving the admitted capture producer on a live graph."""

import asyncio
import hashlib
import math

import pytest
import yaml
from typing import Mapping

from jasper.active_speaker.commissioning_evidence import (
    RegionEvidenceTarget,
    delay_point_target_fingerprint,
    evidence_attempt_target_id,
)
from jasper.active_speaker.commissioning_run import (
    CommissioningAttemptHandle,
    CommissioningRunHandle,
)
from jasper.active_speaker.delay_sweep import (
    DelaySweepPlan,
    DelaySweepRefused,
    run_delay_sweep,
    sweep_spec,
)
from jasper.active_speaker.delay_sweep_live import (
    REFUSE_NO_LIVE_GRAPH,
    REFUSE_POSES,
    LiveDelaySweepHost,
)
from jasper.active_speaker.runtime_contract import NO_BASS_EXTENSION_PROFILE_SUMMARY
from jasper.control import measurement_hold

FC_HZ = 1800.0
ROLE_CHANNELS = {"tweeter": (1,), "woofer": (0,)}


def _fp(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _hex(seed: str) -> str:
    """A deterministic lowercase UUID-hex id (the shape these handles require)."""
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _live_yaml() -> str:
    return yaml.safe_dump(
        {
            "devices": {"volume_limit": 0.0, "samplerate": 48000},
            "filters": {
                "as_woofer_delay": {
                    "type": "Delay",
                    "parameters": {"delay": 0.15, "unit": "ms"},
                },
                "as_tweeter_delay": {
                    "type": "Delay",
                    "parameters": {"delay": 0.4, "unit": "ms"},
                },
                "as_woofer_baseline_gain": {
                    "type": "Gain",
                    "parameters": {"gain": -3.5, "inverted": False, "mute": False},
                },
                "as_tweeter_baseline_gain": {
                    "type": "Gain",
                    "parameters": {"gain": -6.0, "inverted": False, "mute": False},
                },
            },
            "pipeline": [
                {"type": "Filter", "channels": [0],
                 "names": ["as_woofer_delay", "as_woofer_baseline_gain"]},
                {"type": "Filter", "channels": [1],
                 "names": ["as_tweeter_delay", "as_tweeter_baseline_gain"]},
            ],
        }
    )


def _target() -> RegionEvidenceTarget:
    return RegionEvidenceTarget(
        speaker_group_id="main",
        region_id="lowmid",
        region_fingerprint=_fp("region"),
        lower_role="woofer",
        upper_role="tweeter",
        electrical_fc_hz=FC_HZ,
        electrical_family="linkwitz_riley",
        electrical_order=4,
        normal_target_fingerprint=_fp("nt"),
        normal_context_base_fingerprint=_fp("nc"),
        reverse_target_fingerprint=_fp("rt"),
        reverse_context_base_fingerprint=_fp("rc"),
        delay_target_base_fingerprint=_fp("dt"),
        delay_context_base_fingerprint=_fp("dc"),
    )


def _run_handle() -> CommissioningRunHandle:
    return CommissioningRunHandle(
        session_id=_hex("session"),
        session_fingerprint=_fp("session"),
        run_id=_hex("run"),
        owner_id=_hex("owner"),
        owner_generation=1,
    )


def _depth(applied_us: float, true_us: float) -> float:
    residual = (applied_us - true_us) * 1e-6
    ratio = abs(2.0 * math.sin(math.pi * FC_HZ * residual))
    if ratio < 1e-6:
        return 40.0
    return min(40.0, max(0.0, -20.0 * math.log10(ratio / 2.0)))


class _Store:
    """Echoes back the durable attempt the host asked to reserve."""

    def __init__(self):
        self.reserved = []

    def reserve_attempt(self, handle, *, target_id, target_fingerprint, reuse_existing):
        self.reserved.append(
            {"target_id": target_id, "target_fingerprint": target_fingerprint,
             "reuse_existing": reuse_existing}
        )
        return CommissioningAttemptHandle(
            run=handle,
            attempt_id=_hex(f"attempt-{len(self.reserved)}"),
            attempt_number=len(self.reserved),
            target_id=target_id,
            target_fingerprint=target_fingerprint,
        )


class _Artifact:
    def __init__(self, key):
        self.key = key


class _Capture:
    def __init__(self, key):
        self.analysis_input_artifact = _Artifact(key)


class _Payload:
    def __init__(self, key):
        self.capture = _Capture(key)


class _Result:
    def __init__(self, key):
        self.payload = _Payload(key)


class _EvidenceStore:
    def __init__(self, producer):
        self._producer = producer

    def reopen_json_artifact(self, artifact):
        return self._producer.analyses[artifact.key]


class _Producer:
    """Stands in for SummedCaptureProducer: records what it was asked to play."""

    def __init__(self, *, true_offset_us=100.0):
        self.plan_fingerprint = _fp("plan")
        self.evidence_store = _EvidenceStore(self)
        self.operations = []
        self.contexts = []
        self.fresh = []
        self.analyses = {}
        self.true_offset_us = true_offset_us

    async def capture(self, operation, context):
        self.operations.append(operation)
        self.contexts.append(context)
        # The real producer re-reads through this seam inside its own lock,
        # while the coordinate's graph is still live.
        self.fresh.append(await context.fresh_readback())
        graph = yaml.safe_load(context.active_raw)
        tweeter = graph["filters"]["as_tweeter_delay"]["parameters"]["delay"]
        woofer = graph["filters"]["as_woofer_delay"]["parameters"]["delay"]
        realized_us = (tweeter - woofer) * 1000.0
        key = f"analysis-{len(self.operations)}"
        self.analyses[key] = {
            "acoustic": {
                "null_depth_db": _depth(realized_us, self.true_offset_us),
                "null_depth_capped": False,
                "mic_clipping": False,
                "calibrated": True,
                "expect_null": True,
                "crossover_fc_hz": FC_HZ,
                "gating": {"applied": True},
                "above_validity_floor": True,
                "snr": {"decision_class": "alignment", "verdict": "ok"},
                "verdict": "blend_ok",
            }
        }
        return _Result(key)


class _Cam:
    def __init__(self, raw):
        self.raw = raw
        self.entry_raw = raw
        self.loaded = []
        self.ducked = []
        self.reloads = 0

    async def get_active_config_raw(self, *, best_effort=False):
        return self.raw

    async def get_config_file_path(self, *, best_effort=False):
        return "/etc/camilladsp/applied.yml"

    async def normalize_config_raw(self, config, *, best_effort=False):
        return config

    async def get_volume_db(self):
        return -32.0

    async def set_active_config_raw(self, text, *, best_effort=False, duck=True):
        self.loaded.append(text)
        self.ducked.append(duck)
        self.raw = text
        return True

    async def reload(self, *, best_effort=False):
        self.reloads += 1
        return True


def _host(tmp_path, *, cam=None, producer=None, store=None, plan=None):
    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    return LiveDelaySweepHost(
        cam=cam or _Cam(_live_yaml()),
        producer=producer or _Producer(),
        run_store=store or _Store(),
        run_handle=_run_handle(),
        target=_target(),
        plan=plan or DelaySweepPlan(
            spec=spec, inverted_role="tweeter", role_channels=ROLE_CHANNELS
        ),
        placement_fingerprint=_fp("placement"),
        driver_target_fingerprints=(_fp("woofer-driver"), _fp("tweeter-driver")),
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
        topology_id="topology-under-test",
        config_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def _no_live_session(monkeypatch):
    measurement_hold.reset_for_tests()
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.live_measurement_session",
        lambda *, action: None,
    )
    yield
    measurement_hold.reset_for_tests()


# --------------------------------------------------------------------------- #
# the walk drives the producer
# --------------------------------------------------------------------------- #


def test_every_capture_goes_through_the_producer_as_a_delay_null_operation(tmp_path):
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    assert producer.operations, "the sweep played nothing"
    for operation in producer.operations:
        assert operation.evidence_kind == "delay_null"
        assert operation.null_walk_spec == host.plan.spec
        assert operation.lower_channels == ROLE_CHANNELS["woofer"]
        assert operation.upper_channels == ROLE_CHANNELS["tweeter"]
        assert 0 <= operation.capture_ordinal < operation.required_capture_count


def test_the_producer_is_handed_the_coordinate_the_walk_is_measuring(tmp_path):
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    coordinates = sorted({op.relative_delay_us for op in producer.operations})
    assert coordinates == list(host.plan.spec.coarse_candidate_delays_us())
    # The operation's durable identity is bound to that same coordinate.
    for operation in producer.operations:
        assert operation.attempt.target_fingerprint == delay_point_target_fingerprint(
            host.target, host.plan.spec, operation.relative_delay_us
        )
        assert operation.attempt.target_id == evidence_attempt_target_id(
            "delay_null", operation.attempt.target_fingerprint
        )


def test_the_graph_the_producer_plays_carries_the_inversion_and_both_lanes(tmp_path):
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    for operation, context in zip(producer.operations, producer.contexts):
        graph = yaml.safe_load(context.active_raw)
        gain = graph["filters"]["as_tweeter_baseline_gain"]["parameters"]
        assert gain["inverted"] is True
        assert graph["devices"]["volume_limit"] == 0.0
        tweeter = graph["filters"]["as_tweeter_delay"]["parameters"]["delay"]
        woofer = graph["filters"]["as_woofer_delay"]["parameters"]["delay"]
        # The applied graph carried 0.4/0.15 ms; the sweep zeroes the lane it is
        # not delaying, so the realized relative delay IS the coordinate.
        assert (tweeter - woofer) * 1000.0 == pytest.approx(
            operation.relative_delay_us
        )


def test_one_durable_attempt_per_coordinate_not_per_capture(tmp_path):
    store = _Store()
    host = _host(tmp_path, store=store)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    assert {row["reuse_existing"] for row in store.reserved} == {True}
    coarse = host.plan.spec.coarse_candidate_delays_us()
    assert len({row["target_fingerprint"] for row in store.reserved}) == len(coarse)


# --------------------------------------------------------------------------- #
# the graph, and putting it back
# --------------------------------------------------------------------------- #


def test_the_sweep_puts_back_the_exact_graph_it_displaced(tmp_path):
    cam = _Cam(_live_yaml())
    host = _host(tmp_path, cam=cam)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    # Every swap was set_active_config_raw; the persisted anchor is untouched,
    # which is what makes reload() a complete restore.
    assert cam.loaded, "no graph was ever applied"
    # The sweep puts back the exact bytes it displaced, not the persisted
    # anchor: an audition that was live when it started is still live after.
    assert cam.loaded[-1] == cam.entry_raw
    assert cam.reloads == 0


def test_the_graph_is_put_back_when_a_capture_fails(tmp_path):
    class _Boom(_Producer):
        async def capture(self, operation, context):
            raise RuntimeError("capture exploded")

    cam = _Cam(_live_yaml())
    host = _host(tmp_path, cam=cam, producer=_Boom())
    with pytest.raises(RuntimeError):
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert cam.loaded[-1] == cam.entry_raw


def test_the_graph_is_put_back_when_the_sweep_is_cancelled(tmp_path):
    cam = _Cam(_live_yaml())
    host = _host(tmp_path, cam=cam)

    async def drive():
        task = asyncio.ensure_future(run_delay_sweep(host.plan, host.seams()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert cam.loaded[-1] == cam.entry_raw


def test_a_box_running_no_graph_refuses_before_anything_is_applied(tmp_path):
    cam = _Cam("")
    host = _host(tmp_path, cam=cam)
    with pytest.raises(DelaySweepRefused) as excinfo:
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert excinfo.value.reason == REFUSE_NO_LIVE_GRAPH
    assert cam.loaded == []


# --------------------------------------------------------------------------- #
# admission
# --------------------------------------------------------------------------- #


def test_an_active_measurement_claim_refuses_before_the_graph_moves(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.live_measurement_session",
        lambda *, action: "a room sweep holds the speaker",
    )
    cam = _Cam(_live_yaml())
    producer = _Producer()
    host = _host(tmp_path, cam=cam, producer=producer)
    with pytest.raises(DelaySweepRefused):
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert cam.loaded == []
    assert producer.operations == []
    assert cam.reloads == 0


def test_the_host_asks_the_one_session_arbiter_and_passes_its_words_through(
    tmp_path, monkeypatch
):
    seen = {}

    def _claim(*, action):
        seen["action"] = action
        return None

    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.live_measurement_session", _claim
    )
    host = _host(tmp_path)
    assert host.session_claim() is None
    assert seen["action"], "the arbiter is told what door is asking"


# --------------------------------------------------------------------------- #
# the artifact
# --------------------------------------------------------------------------- #


def test_a_live_walk_banks_the_graded_artifact_end_to_end(tmp_path):
    producer = _Producer(true_offset_us=100.0)
    host = _host(tmp_path, producer=producer)
    artifact = asyncio.run(run_delay_sweep(host.plan, host.seams()))

    assert artifact["kind"] == "jts_inter_driver_delay_sweep"
    assert artifact["verdict"]["verdict"] == "delay_resolved_robust"
    assert artifact["verdict"]["selected_delay_target"] == "tweeter"
    assert artifact["verdict"]["selected_relative_delay_us"] == pytest.approx(100.0)
    assert len(artifact["steps"]) == len(producer.operations)
    assert set(artifact["rows"])


# --------------------------------------------------------------------------- #
# what the real producer requires of a delay_null caller
# --------------------------------------------------------------------------- #


def test_every_capture_carries_a_real_delay_confirmation(tmp_path):
    # The producer computes `delay_current = readback.delay_confirmation is not
    # None` for delay_null, and admission refuses PROTECTION_EVIDENCE_STALE when
    # it is None. A None here means the sweep cannot play a single tone.
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    assert producer.contexts
    for operation, context in zip(producer.operations, producer.contexts):
        confirmation = context.delay_confirmation
        assert confirmation is not None
        # And it is the confirmation for THIS coordinate, proven against the
        # zero-relative snapshot staged at sweep start.
        assert confirmation.relative_delay_us == pytest.approx(
            operation.relative_delay_us
        )
        # And what the DSP actually read back equals what was requested.
        assert confirmation.readback_relative_delay_us == pytest.approx(
            operation.relative_delay_us
        )


def test_every_capture_carries_host_proved_bass_authority(tmp_path):
    # `_protection_evidence` refuses `graph_authority_unproven` without it.
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    for context in producer.contexts:
        assert isinstance(context.bass_profile_summary, Mapping)
        assert context.bass_profile_summary == NO_BASS_EXTENSION_PROFILE_SUMMARY


def test_the_fresh_readback_seam_also_carries_both(tmp_path):
    # The producer re-reads through `fresh_readback` inside its own lock; a
    # confirmation present only on the first observation would still refuse.
    producer = _Producer()
    host = _host(tmp_path, producer=producer)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    assert producer.fresh
    for readback in producer.fresh:
        assert readback.delay_confirmation is not None
        assert isinstance(readback.bass_profile_summary, Mapping)


# --------------------------------------------------------------------------- #
# the hold, the fader, and poses
# --------------------------------------------------------------------------- #


def test_the_sweep_holds_the_box_for_its_whole_duration_and_lets_go(tmp_path):
    seen = []

    class _Watching(_Producer):
        async def capture(self, operation, context):
            seen.append(measurement_hold.held())
            return await super().capture(operation, context)

    host = _host(tmp_path, producer=_Watching())
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    # Checking the door is not the same as standing in it: without taking the
    # hold, a seat-level or angle-capture door could move the fader mid-sweep.
    assert seen and all(seen), "the hold was not held while capturing"
    assert measurement_hold.held() is False
    assert measurement_hold.owner() is None


def test_the_hold_is_released_even_when_the_sweep_fails(tmp_path):
    class _Boom(_Producer):
        async def capture(self, operation, context):
            raise RuntimeError("capture exploded")

    host = _host(tmp_path, producer=_Boom())
    with pytest.raises(RuntimeError):
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert measurement_hold.held() is False


def test_a_hold_another_owner_already_has_refuses_the_sweep(tmp_path):
    measurement_hold.acquire("somebody-else")
    cam = _Cam(_live_yaml())
    producer = _Producer()
    host = _host(tmp_path, cam=cam, producer=producer)
    with pytest.raises(DelaySweepRefused):
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert cam.loaded == []
    assert producer.operations == []


def test_no_graph_swap_pays_the_fader_bracket(tmp_path):
    cam = _Cam(_live_yaml())
    host = _host(tmp_path, cam=cam)
    asyncio.run(run_delay_sweep(host.plan, host.seams()))

    # duck=True would pay ~0.94 s on every coordinate and, because its release
    # is best-effort, could strand the box a step down for the rest of the run.
    assert cam.ducked and not any(cam.ducked)


def test_a_multi_pose_plan_is_refused_because_nothing_here_moves_the_mic(tmp_path):
    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    plan = DelaySweepPlan(
        spec=spec, inverted_role="tweeter", role_channels=ROLE_CHANNELS,
        poses_deg=(0, 30),
    )
    with pytest.raises(DelaySweepRefused) as excinfo:
        _host(tmp_path, plan=plan)
    assert excinfo.value.reason == REFUSE_POSES


def test_an_unreadable_listening_volume_refuses_rather_than_crashing(tmp_path):
    class _Mute(_Cam):
        async def get_volume_db(self):
            return None

    host = _host(tmp_path, cam=_Mute(_live_yaml()))
    with pytest.raises(DelaySweepRefused):
        asyncio.run(run_delay_sweep(host.plan, host.seams()))
    assert measurement_hold.held() is False
