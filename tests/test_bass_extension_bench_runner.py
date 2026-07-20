# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign orchestrator, end to end with mocked collaborators.

The happy path runs discovery + candidate for two targets and the emitted bundle
is accepted by the frozen producer. The abort path proves an operator Stop ends
the target through the ``aborted`` arm with the predecessor restored.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

import pytest

from jasper.active_speaker.camilla_yaml import (
    BASS_EXTENSION_LT_FILTER,
    BASS_EXTENSION_SUBSONIC_FILTER,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.bench import activation, bundle
from jasper.bass_extension.bench.context import build_measured_context
from jasper.bass_extension.bench.manifest import STIMULUS_ROLES, author_campaign_manifest
from jasper.bass_extension.bench.runner import (
    BenchAborted,
    BenchDeps,
    CandidateMeasurements,
    DiscoveryProbe,
    Stop,
    TargetPlan,
    run_campaign,
)
from jasper.bass_extension.bench.sink import BundleSink

PREDECESSOR_YAML = "filters: {}\npipeline: []\n"
LIMITER_NAME = "baseline_limiter_woofer"
BASELINE = -1.0


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(label: str, *, payload: str | None = None) -> ArtifactIdentity:
    body = payload or label
    return ArtifactIdentity(
        bundle_kind="jts_bass_extension_limiter_bench",
        bundle_id="fixture",
        relative_path=f"{label}.json",
        sha256=_sha(body),
        byte_size=len(body.encode("utf-8")),
    )


def _target_fp(target_id: str) -> str:
    return _sha(f"target:{target_id}")


class FakeController:
    def __init__(self, config_path: Path) -> None:
        self.active_raw = PREDECESSOR_YAML
        self.config_path = config_path
        self.calls: list[str] = []

    async def get_active_config_raw(self) -> str:
        return self.active_raw

    async def get_config_file_path(self) -> str:
        return str(self.config_path)

    async def set_active_config_raw(self, raw: str) -> bool:
        self.calls.append("set_active_config_raw")
        self.active_raw = raw
        return True

    async def patch_config(self, patch: dict[str, Any]) -> bool:
        self.calls.append("patch_config")
        return True

    async def reload(self) -> bool:
        self.calls.append("reload")
        self.active_raw = PREDECESSOR_YAML
        return True


class FakeFloor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def to_floor(self) -> None:
        self.calls.append("to_floor")

    async def assert_at_floor(self) -> None:
        self.calls.append("assert_at_floor")


def _measurement_core(target_id: str, role: str) -> dict[str, Any]:
    stimulus_payload = f"{target_id}-{role}-stimulus"
    admission_payload = f"{target_id}-{role}-admission"
    return {
        "stimulus": _artifact(f"{target_id}-{role}-stimulus", payload=stimulus_payload),
        "admission": _artifact(f"{target_id}-{role}-admission", payload=admission_payload),
        "pre_limiter_pcm": _artifact(f"{target_id}-{role}-pre"),
        "post_limiter_pcm": _artifact(f"{target_id}-{role}-post"),
        "acoustic_capture": _artifact(f"{target_id}-{role}-cap"),
        "signal_analysis": _artifact(f"{target_id}-{role}-sig"),
        "protection_analysis": _artifact(f"{target_id}-{role}-prot"),
        "stimulus_band_hz": (30.0, 200.0),
        "stimulus_effective_peak_dbfs": -30.0,
        "commanded_main_volume_db": -35.0,
        "target_boost_db": 6.0,
        "digital_clamp_passed": True,
        "pre_limiter_peak_dbfs": -15.0,
        "post_limiter_peak_dbfs": -16.0,
        "hold_duration_s": 12.0,
        "required_cooldown_s": 4.0,
        "repeat_count": 2,
        "quality_verdict": "pass",
        "protection_verdict": "pass",
    }


class FakeExecutor:
    """Returns canned, producer-consistent measured records (no hardware)."""

    def __init__(self, threshold_by_target: dict[str, float]) -> None:
        self._threshold = threshold_by_target

    async def run_discovery(
        self, *, target: TargetPlan, active_graph_readback: ArtifactIdentity, sink: BundleSink, stop: Stop
    ) -> Sequence[DiscoveryProbe]:
        threshold = self._threshold[target.target_id]
        return [
            DiscoveryProbe(
                stimulus=_artifact(f"{target.target_id}-src-stim"),
                admission=_artifact(f"{target.target_id}-src-adm"),
                active_graph_readback=active_graph_readback,
                pre_limiter_pcm=_artifact(f"{target.target_id}-src-pcm"),
                peak_analysis=_artifact(f"{target.target_id}-src-analysis"),
                pre_limiter_peak_dbfs=threshold,
            )
        ]

    async def run_candidate(
        self,
        *,
        target: TargetPlan,
        candidate_setting_dbfs: float,
        candidate_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> CandidateMeasurements:
        tid = target.target_id
        transfer_post = f"{tid}-transfer-post-{candidate_setting_dbfs}"
        core = _measurement_core(tid, "sweep")
        sweep = bundle.build_sweep_record(
            reference_activation_receipt=_artifact(f"{tid}-ref-act"),
            reference_stimulus=_artifact(f"{tid}-ref-stim", payload=f"{tid}-sweep-stimulus"),
            reference_admission=_artifact(f"{tid}-ref-adm", payload=f"{tid}-sweep-admission"),
            reference_acoustic_capture=_artifact(f"{tid}-ref-cap"),
            transparency_analysis=_artifact(f"{tid}-transp"),
            reference_target_fingerprint=_target_fp(tid),
            reference_active_graph_fingerprint=_sha(f"{tid}-ref-graph"),
            reference_configured_clip_limit_dbfs=BASELINE,
            transparency_verdict="pass",
            **core,
        )
        transfer = bundle.build_transfer_record(
            stimulus=_artifact(f"{tid}-tr-stim"),
            pre_limiter_pcm=_artifact(f"{tid}-tr-pre"),
            post_limiter_pcm=_artifact(f"{tid}-tr-post", payload=transfer_post),
            reference_post_limiter_pcm=_artifact(f"{tid}-tr-ref", payload=transfer_post),
            transfer_analysis=_artifact(f"{tid}-tr-analysis"),
            verdict="pass",
        )
        sustain = bundle.build_sustain_record(**_measurement_core(tid, "sustain"))
        return CandidateMeasurements(
            digital_transfer_probe=transfer,
            sweep_transparency=sweep,
            sustain_stress=sustain,
            active_graph_fingerprint=_sha(f"{tid}-active-graph-{candidate_setting_dbfs}"),
            ordered_owner_chain=[
                BASS_EXTENSION_LT_FILTER,
                BASS_EXTENSION_SUBSONIC_FILTER,
                LIMITER_NAME,
            ],
            configured_clip_limit_dbfs=candidate_setting_dbfs,
            accepted=True,
        )


@pytest.fixture(autouse=True)
def _stub_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        activation, "_prove_active_graph", lambda config, proof: proof.expected_clip_limit_dbfs
    )


def _deps(controller: FakeController, executor: FakeExecutor, stop: Stop) -> BenchDeps:
    @asynccontextmanager
    async def open_window():
        yield None

    return BenchDeps(
        open_window=open_window,
        controller=controller,
        floor=FakeFloor(),
        executor=executor,
        stop=stop,
    )


def _context() -> dict[str, Any]:
    return build_measured_context(
        target_family_fingerprint=_sha("family"),
        target_order=[("deep", _target_fp("deep")), ("natural", _target_fp("natural"))],
        driver_safety_fingerprint=_sha("ds"),
        margin_policy_fingerprint=_sha("mp"),
        transparency_policy_fingerprint=_sha("tp"),
        natural_graph_fingerprint=_sha("natural-graph"),
        baseline_limiter_clip_limit_dbfs=BASELINE,
        camilladsp_build_id="build",
        owner_channels=[2],
        sample_rate_hz=48_000,
        limiter_name=LIMITER_NAME,
        tap_implementation_id="tap",
    )


def _targets() -> list[TargetPlan]:
    return [
        TargetPlan(
            target_id=tid,
            target_fingerprint=_target_fp(tid),
            graph_raw_text="filters:\n  x: {}\npipeline: []\n",
            limiter_name=LIMITER_NAME,
            owner_channels=(2,),
            profile_summary={"runtime_block_required": True},
            baseline_clip_limit_dbfs=BASELINE,
        )
        for tid in ("deep", "natural")
    ]


def _manifest():
    request = {
        "requested_stimulus_band_hz": [30.0, 200.0],
        "requested_stimulus_effective_peak_dbfs": -30.0,
        "requested_commanded_main_volume_db": -35.0,
        "requested_hold_duration_s": 12.0,
        "requested_cooldown_s": 4.0,
        "requested_repeat_count": 2,
        "stimulus_generator_identity": "gen-v1",
    }
    inputs = {
        "driver_safety_fingerprint": _sha("ds"),
        "margin_policy_name": "conservative",
        "margin_policy_fingerprint": _sha("mp"),
        "requests": {
            tid: {role: dict(request) for role in STIMULUS_ROLES}
            for tid in ("deep", "natural")
        },
    }
    return author_campaign_manifest(inputs, target_ids=("deep", "natural"))


def _retained() -> dict[str, ArtifactIdentity]:
    return {
        name: _artifact(f"retained-{name}")
        for name in ("sweep", "sustain", "commanded_level", "stimulus_peak", "boost", "digital_clamp")
    }


async def test_campaign_emits_a_bundle_the_producer_accepts(tmp_path: Path) -> None:
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    config = tmp_path / "active.yml"
    config.write_text(PREDECESSOR_YAML, encoding="utf-8")
    controller = FakeController(config)
    stop = Stop()
    deps = _deps(controller, FakeExecutor({"deep": -20.0, "natural": -10.0}), stop)
    context = _context()
    sink = BundleSink(tmp_path / "bundle", bundle_id="run-1")

    emitted = await run_campaign(
        deps,
        manifest=_manifest(),
        measured_context=context,
        targets=_targets(),
        retained_facts=_retained(),
        sink=sink,
    )

    result = produce_limiter_thresholds(emitted, required_context=context)
    assert isinstance(result, LimiterThresholdSet), result
    assert [t.limiter_threshold_dbfs for t in result.targets] == [-20.0, -10.0]
    assert "reload" in controller.calls  # every activation restored
    assert (tmp_path / "bundle" / "bundle.json").exists()


async def test_operator_stop_ends_the_target_aborted_and_restores(tmp_path: Path) -> None:
    config = tmp_path / "active.yml"
    config.write_text(PREDECESSOR_YAML, encoding="utf-8")
    controller = FakeController(config)
    stop = Stop()

    class StoppingExecutor(FakeExecutor):
        async def run_discovery(self, **kwargs: Any):  # type: ignore[override]
            stop.stop()  # operator presses Stop during discovery
            raise BenchAborted("operator Stop requested")

    deps = _deps(controller, StoppingExecutor({"deep": -20.0, "natural": -10.0}), stop)
    sink = BundleSink(tmp_path / "bundle", bundle_id="run-2")

    emitted = await run_campaign(
        deps,
        manifest=_manifest(),
        measured_context=_context(),
        targets=_targets()[:1],
        retained_facts=_retained(),
        sink=sink,
    )

    target = emitted["targets"][0]
    assert target["result"]["disposition"] == "aborted"
    assert "stop_receipt" in target["result"]
    assert "reload" in controller.calls  # predecessor restored on abort
