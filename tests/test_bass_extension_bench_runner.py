# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign orchestrator, end to end with mocked collaborators.

The happy path runs discovery + the two-phase candidate pass for two targets and
the emitted bundle is accepted by the frozen producer. Further tests pin the
2026-07-19b candidate/result branches, distinct-setting dedup, and operator Stop.
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
    ReferenceSweepCapture,
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
    async def to_floor(self) -> None:
        pass

    async def assert_at_floor(self) -> None:
        pass


def _sweep_core(
    target_id: str,
    *,
    quality: str = "pass",
    protection: str = "pass",
    clamp: bool = True,
) -> dict[str, Any]:
    stimulus_payload = f"{target_id}-sweep-stimulus"
    admission_payload = f"{target_id}-sweep-admission"
    return {
        "stimulus": _artifact(f"{target_id}-sweep-stim", payload=stimulus_payload),
        "admission": _artifact(f"{target_id}-sweep-adm", payload=admission_payload),
        "pre_limiter_pcm": _artifact(f"{target_id}-sweep-pre"),
        "post_limiter_pcm": _artifact(f"{target_id}-sweep-post"),
        "acoustic_capture": _artifact(f"{target_id}-sweep-cap"),
        "signal_analysis": _artifact(f"{target_id}-sweep-sig"),
        "protection_analysis": _artifact(f"{target_id}-sweep-prot"),
        "stimulus_band_hz": (30.0, 200.0),
        "stimulus_effective_peak_dbfs": -30.0,
        "commanded_main_volume_db": -35.0,
        "target_boost_db": 6.0,
        "digital_clamp_passed": clamp,
        "pre_limiter_peak_dbfs": -15.0,
        "post_limiter_peak_dbfs": -16.0,
        "hold_duration_s": 12.0,
        "required_cooldown_s": 4.0,
        "repeat_count": 2,
        "quality_verdict": quality,
        "protection_verdict": protection,
    }


def _sustain(target_id: str, *, quality: str = "pass", protection: str = "pass") -> dict[str, Any]:
    return bundle.build_sustain_record(
        stimulus=_artifact(f"{target_id}-sus-stim"),
        admission=_artifact(f"{target_id}-sus-adm"),
        pre_limiter_pcm=_artifact(f"{target_id}-sus-pre"),
        post_limiter_pcm=_artifact(f"{target_id}-sus-post"),
        acoustic_capture=_artifact(f"{target_id}-sus-cap"),
        signal_analysis=_artifact(f"{target_id}-sus-sig"),
        protection_analysis=_artifact(f"{target_id}-sus-prot"),
        stimulus_band_hz=(30.0, 200.0),
        stimulus_effective_peak_dbfs=-30.0,
        commanded_main_volume_db=-35.0,
        target_boost_db=6.0,
        digital_clamp_passed=True,
        pre_limiter_peak_dbfs=-15.0,
        post_limiter_peak_dbfs=-16.0,
        hold_duration_s=12.0,
        required_cooldown_s=4.0,
        repeat_count=2,
        quality_verdict=quality,
        protection_verdict=protection,
    )


def _transfer(target_id: str, setting: float, *, verdict: str = "pass") -> dict[str, Any]:
    post = f"{target_id}-transfer-post-{setting}"
    return bundle.build_transfer_record(
        stimulus=_artifact(f"{target_id}-tr-stim-{setting}"),
        pre_limiter_pcm=_artifact(f"{target_id}-tr-pre-{setting}"),
        post_limiter_pcm=_artifact(f"{target_id}-tr-post-{setting}", payload=post),
        reference_post_limiter_pcm=_artifact(f"{target_id}-tr-ref-{setting}", payload=post),
        transfer_analysis=_artifact(f"{target_id}-tr-analysis-{setting}"),
        verdict=verdict,
    )


class FakeExecutor:
    """Canned, producer-consistent measured records (no hardware).

    ``peaks`` maps target id -> the discovery pre-limiter peaks; ``verdicts``
    maps (target_id, setting) -> a dict of verdict overrides.
    """

    def __init__(
        self,
        peaks: dict[str, list[float]],
        *,
        verdicts: dict[tuple[str, float], dict[str, Any]] | None = None,
    ) -> None:
        self._peaks = peaks
        self._verdicts = verdicts or {}

    async def run_discovery(
        self, *, target: TargetPlan, active_graph_readback: ArtifactIdentity, sink: BundleSink, stop: Stop
    ) -> Sequence[DiscoveryProbe]:
        return [
            DiscoveryProbe(
                stimulus=_artifact(f"{target.target_id}-src-stim-{i}"),
                admission=_artifact(f"{target.target_id}-src-adm-{i}"),
                active_graph_readback=active_graph_readback,
                pre_limiter_pcm=_artifact(f"{target.target_id}-src-pcm-{i}"),
                peak_analysis=_artifact(f"{target.target_id}-src-analysis-{i}"),
                pre_limiter_peak_dbfs=peak,
            )
            for i, peak in enumerate(self._peaks[target.target_id])
        ]

    async def run_reference_sweep(
        self, *, target: TargetPlan, reference_readback: ArtifactIdentity, sink: BundleSink, stop: Stop
    ) -> ReferenceSweepCapture:
        tid = target.target_id
        return ReferenceSweepCapture(
            reference_stimulus=_artifact(f"{tid}-ref-stim", payload=f"{tid}-sweep-stimulus"),
            reference_admission=_artifact(f"{tid}-ref-adm", payload=f"{tid}-sweep-admission"),
            reference_acoustic_capture=_artifact(f"{tid}-ref-cap"),
        )

    async def run_candidate(
        self,
        *,
        target: TargetPlan,
        candidate_setting_dbfs: float,
        candidate_readback: ArtifactIdentity,
        reference: ReferenceSweepCapture,
        sink: BundleSink,
        stop: Stop,
    ) -> CandidateMeasurements:
        tid = target.target_id
        cfg = self._verdicts.get((tid, candidate_setting_dbfs), {})
        return CandidateMeasurements(
            digital_transfer_probe=_transfer(
                tid, candidate_setting_dbfs, verdict=cfg.get("transfer", "pass")
            ),
            sweep_core=_sweep_core(
                tid,
                quality=cfg.get("sweep_quality", "pass"),
                protection=cfg.get("sweep_protection", "pass"),
                clamp=cfg.get("sweep_clamp", True),
            ),
            sustain_stress=_sustain(
                tid,
                quality=cfg.get("sustain_quality", "pass"),
                protection=cfg.get("sustain_protection", "pass"),
            ),
            transparency_analysis=_artifact(f"{tid}-transp-{candidate_setting_dbfs}"),
            transparency_verdict=cfg.get("transparency", "pass"),
            active_graph_fingerprint=_sha(f"{tid}-active-graph-{candidate_setting_dbfs}"),
            ordered_owner_chain=[
                BASS_EXTENSION_LT_FILTER,
                BASS_EXTENSION_SUBSONIC_FILTER,
                LIMITER_NAME,
            ],
            configured_clip_limit_dbfs=candidate_setting_dbfs,
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


def _context(*target_ids: str) -> dict[str, Any]:
    return build_measured_context(
        target_family_fingerprint=_sha("family"),
        target_order=[(tid, _target_fp(tid)) for tid in target_ids],
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


def _targets(*target_ids: str) -> list[TargetPlan]:
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
        for tid in target_ids
    ]


def _manifest(*target_ids: str):
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
        "requests": {tid: {role: dict(request) for role in STIMULUS_ROLES} for tid in target_ids},
    }
    return author_campaign_manifest(inputs, target_ids=target_ids)


def _retained() -> dict[str, ArtifactIdentity]:
    return {
        name: _artifact(f"retained-{name}")
        for name in ("sweep", "sustain", "commanded_level", "stimulus_peak", "boost", "digital_clamp")
    }


async def _run(
    tmp_path: Path,
    *,
    peaks,
    verdicts=None,
    target_ids=("deep", "natural"),
    run_id="r",
) -> tuple[dict[str, Any], FakeController]:
    config = tmp_path / "active.yml"
    config.write_text(PREDECESSOR_YAML, encoding="utf-8")
    controller = FakeController(config)
    stop = Stop()
    deps = _deps(controller, FakeExecutor(peaks, verdicts=verdicts), stop)
    sink = BundleSink(tmp_path / "bundle", bundle_id=run_id)
    emitted = await run_campaign(
        deps,
        manifest=_manifest(*target_ids),
        measured_context=_context(*target_ids),
        targets=_targets(*target_ids),
        retained_facts=_retained(),
        sink=sink,
    )
    return emitted, controller


async def test_campaign_emits_a_bundle_the_producer_accepts(tmp_path: Path) -> None:
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    emitted, controller = await _run(tmp_path, peaks={"deep": [-20.0], "natural": [-10.0]})
    context = _context("deep", "natural")

    result = produce_limiter_thresholds(emitted, required_context=context)
    assert isinstance(result, LimiterThresholdSet), result
    assert [t.limiter_threshold_dbfs for t in result.targets] == [-20.0, -10.0]
    assert "reload" in controller.calls  # every activation restored
    assert (tmp_path / "bundle" / "bundle.json").exists()


async def test_transparency_only_failure_advances_then_accepts(tmp_path: Path) -> None:
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    # First candidate (-20) transparency-fails and advances; second (-10) accepts.
    emitted, controller = await _run(
        tmp_path,
        peaks={"deep": [-20.0, -10.0]},
        verdicts={("deep", -20.0): {"transparency": "fail"}},
        target_ids=("deep",),
    )
    candidates = emitted["targets"][0]["result"]["candidates_least_to_most_permissive"]
    assert [c["disposition"] for c in candidates] == ["limiter_transparency_failed", "accepted"]

    result = produce_limiter_thresholds(emitted, required_context=_context("deep"))
    assert isinstance(result, LimiterThresholdSet)
    assert result.targets[0].limiter_threshold_dbfs == -10.0
    assert "reload" in controller.calls


async def test_honest_verdict_failure_refuses_the_target(tmp_path: Path) -> None:
    emitted, controller = await _run(
        tmp_path,
        peaks={"deep": [-20.0]},
        verdicts={("deep", -20.0): {"transfer": "fail"}},
        target_ids=("deep",),
    )
    result = emitted["targets"][0]["result"]
    assert result["disposition"] == "refused"
    assert "stop_receipt" in result
    assert "reload" in controller.calls  # predecessor restored


async def test_no_candidate_at_or_below_baseline_refuses(tmp_path: Path) -> None:
    emitted, _ = await _run(tmp_path, peaks={"deep": [3.0]}, target_ids=("deep",))
    result = emitted["targets"][0]["result"]
    assert result["disposition"] == "refused"


async def test_empty_discovery_inventory_refuses(tmp_path: Path) -> None:
    emitted, _ = await _run(tmp_path, peaks={"deep": []}, target_ids=("deep",))
    result = emitted["targets"][0]["result"]
    assert result["disposition"] == "refused"


async def test_duplicate_measured_peaks_dedupe_to_one_candidate(tmp_path: Path) -> None:
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    # Two near-silent probes collide at the same floored peak; dedup keeps one so
    # the producer does not refuse the campaign for non-strictly-increasing
    # settings.
    emitted, _ = await _run(tmp_path, peaks={"deep": [-120.0, -120.0]}, target_ids=("deep",))
    candidates = emitted["targets"][0]["result"]["candidates_least_to_most_permissive"]
    assert len(candidates) == 1

    result = produce_limiter_thresholds(emitted, required_context=_context("deep"))
    assert isinstance(result, LimiterThresholdSet)
    assert result.targets[0].limiter_threshold_dbfs == -120.0


async def test_operator_stop_ends_the_target_aborted_and_restores(tmp_path: Path) -> None:
    config = tmp_path / "active.yml"
    config.write_text(PREDECESSOR_YAML, encoding="utf-8")
    controller = FakeController(config)
    stop = Stop()

    class StoppingExecutor(FakeExecutor):
        async def run_discovery(self, **kwargs: Any):  # type: ignore[override]
            stop.stop()  # operator presses Stop during discovery
            raise BenchAborted("operator Stop requested")

    deps = _deps(controller, StoppingExecutor({"deep": [-20.0]}), stop)
    sink = BundleSink(tmp_path / "bundle", bundle_id="stop")
    emitted = await run_campaign(
        deps,
        manifest=_manifest("deep"),
        measured_context=_context("deep"),
        targets=_targets("deep"),
        retained_facts=_retained(),
        sink=sink,
    )
    target = emitted["targets"][0]
    assert target["result"]["disposition"] == "aborted"
    assert "stop_receipt" in target["result"]
    assert "reload" in controller.calls  # predecessor restored on abort
