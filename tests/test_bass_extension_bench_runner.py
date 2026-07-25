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
    CandidateCapture,
    CandidateMeasurements,
    DiscoveryCapture,
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

    Split into run_*/finish_* per R9's re-sequencing (sweep/sustain tap
    renders move to after the activation window closes): run_* is the
    "inside the window" phase (there is no real window in these tests, so it
    just captures inputs), finish_* is the "after it closes" phase that
    completes the final records the runner's bundle emitter consumes. The
    canned values are identical either way — this fixture exercises the
    STRUCTURAL two-call split, not different content per phase.
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
    ) -> Sequence[DiscoveryCapture]:
        return [
            DiscoveryCapture(
                stimulus=_artifact(f"{target.target_id}-src-stim-{i}"),
                admission=_artifact(f"{target.target_id}-src-adm-{i}"),
                render_inputs={
                    "active_graph_readback": active_graph_readback,
                    "peak": peak,
                    "index": i,
                },
            )
            for i, peak in enumerate(self._peaks[target.target_id])
        ]

    async def finish_discovery(
        self,
        captures: Sequence[DiscoveryCapture],
        *,
        target: TargetPlan,
        sink: BundleSink,
        stop: Stop,
    ) -> Sequence[DiscoveryProbe]:
        return [
            DiscoveryProbe(
                stimulus=capture.stimulus,
                admission=capture.admission,
                active_graph_readback=capture.render_inputs["active_graph_readback"],
                pre_limiter_pcm=_artifact(
                    f"{target.target_id}-src-pcm-{capture.render_inputs['index']}"
                ),
                peak_analysis=_artifact(
                    f"{target.target_id}-src-analysis-{capture.render_inputs['index']}"
                ),
                pre_limiter_peak_dbfs=capture.render_inputs["peak"],
            )
            for capture in captures
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
    ) -> CandidateCapture:
        tid = target.target_id
        cfg = self._verdicts.get((tid, candidate_setting_dbfs), {})
        return CandidateCapture(
            digital_transfer_probe=_transfer(
                tid, candidate_setting_dbfs, verdict=cfg.get("transfer", "pass")
            ),
            sweep_live={},
            sweep_render_inputs={"tid": tid, "setting": candidate_setting_dbfs, "cfg": cfg},
            sustain_live={},
            sustain_render_inputs={"tid": tid, "setting": candidate_setting_dbfs, "cfg": cfg},
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

    async def finish_candidate(
        self,
        capture: CandidateCapture,
        *,
        target: TargetPlan,
        candidate_setting_dbfs: float,
        sink: BundleSink,
        stop: Stop,
    ) -> CandidateMeasurements:
        tid = capture.sweep_render_inputs["tid"]
        cfg = capture.sweep_render_inputs["cfg"]
        return CandidateMeasurements(
            digital_transfer_probe=capture.digital_transfer_probe,
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
            transparency_analysis=capture.transparency_analysis,
            transparency_verdict=capture.transparency_verdict,
            active_graph_fingerprint=capture.active_graph_fingerprint,
            ordered_owner_chain=capture.ordered_owner_chain,
            configured_clip_limit_dbfs=capture.configured_clip_limit_dbfs,
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
        "render_timeout_s": 30.0,
        "render_rlimit_as_bytes": 536_870_912,
        "render_rlimit_cpu_s": 60,
        "render_nice": 10,
        "cross_check_poll_interval_s": 0.25,
        "cross_check_read_count": 40,
        "cross_check_tolerance_db": 1.5,
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


async def test_round_trip_with_the_real_tap_implementation_id_fingerprint(
    tmp_path: Path,
) -> None:
    """Extends the round-trip above: a bundle carrying the REAL, computed
    ``tap_implementation_id`` (render.BinaryIdentity's string SHA-256
    fingerprint over {camilladsp_version, binary_sha256,
    derivation_function_version}, excluding binary_path) still round-trips
    through the frozen producer — proving the executor-binding PR's actual
    identity computation, not just a literal placeholder string.
    """

    from jasper.bass_extension.bench.render import BinaryIdentity
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    binary = BinaryIdentity(
        path="/opt/camilladsp/camilladsp",
        version_output="CamillaDSP 4.1.3",
        sha256=_sha("fake-camilladsp-binary"),
        camilladsp_build_id="camilladsp-v4.1.3-" + _sha("fake-camilladsp-binary")[:12],
    )
    tap_id = binary.tap_implementation_id
    assert len(tap_id) == 64
    assert tap_id == binary.tap_implementation_id  # deterministic
    identity_artifact = binary.identity_artifact()
    assert identity_artifact["binary_path"] == "/opt/camilladsp/camilladsp"
    # binary_path is excluded from the fingerprinted object: two identities
    # differing ONLY in path (same version/sha256/derivation version) fingerprint
    # identically, while the retained identity artifact still names each path.
    same_binary_other_path = BinaryIdentity(
        path="/some/other/path/camilladsp",
        version_output=binary.version_output,
        sha256=binary.sha256,
        camilladsp_build_id=binary.camilladsp_build_id,
    )
    assert same_binary_other_path.tap_implementation_id == tap_id
    assert same_binary_other_path.identity_artifact()["binary_path"] != identity_artifact["binary_path"]

    config = tmp_path / "active.yml"
    config.write_text(PREDECESSOR_YAML, encoding="utf-8")
    controller = FakeController(config)
    stop = Stop()
    deps = _deps(controller, FakeExecutor({"deep": [-20.0]}), stop)
    sink = BundleSink(tmp_path / "bundle", bundle_id="tap-id")
    context = build_measured_context(
        target_family_fingerprint=_sha("family"),
        target_order=[("deep", _target_fp("deep"))],
        driver_safety_fingerprint=_sha("ds"),
        margin_policy_fingerprint=_sha("mp"),
        transparency_policy_fingerprint=_sha("tp"),
        natural_graph_fingerprint=_sha("natural-graph"),
        baseline_limiter_clip_limit_dbfs=BASELINE,
        camilladsp_build_id=binary.camilladsp_build_id,
        owner_channels=[2],
        sample_rate_hz=48_000,
        limiter_name=LIMITER_NAME,
        tap_implementation_id=tap_id,
    )
    emitted = await run_campaign(
        deps,
        manifest=_manifest("deep"),
        measured_context=context,
        targets=_targets("deep"),
        retained_facts=_retained(),
        sink=sink,
    )
    sink.write_json(
        "tap-implementation-identity.json",
        identity_artifact,
        kind="jts_bass_extension_bench_tap_implementation_identity",
    )
    result = produce_limiter_thresholds(emitted, required_context=context)
    assert isinstance(result, LimiterThresholdSet), result
    assert result.targets[0].limiter_threshold_dbfs == -20.0
