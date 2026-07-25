# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The real, on-device :class:`~jasper.bass_extension.bench.runner.RoleExecutor`.

Composes the derivation / render / stimulus / live-proof / cross-check
modules (this PR's new tap-realization logic) with the injected
:class:`PlayAndCapture` collaborator to implement the two-phase
:class:`~jasper.bass_extension.bench.runner.RoleExecutor` protocol.

**Scope boundary — the one injected seam.** Admitting and PLAYING a stimulus
through hardware while near-field-capturing the acoustic response over the
phone capture-relay session is the piece the CLI's original stub named as
"the one piece with no in-tree helper to compose": no existing function in
this tree opens a capture-relay session, waits for a phone to connect and
upload, and returns the result end to end, and doing so correctly is
fundamentally an on-device, hardware-verified exercise. This module takes
that ONE step as an injected collaborator (:class:`PlayAndCapture`) —
mirroring the SAME dependency-injection shape the runner already uses for
``BenchDeps`` (``controller`` / ``floor`` / ``executor``), applied one level
further down at the point that is genuinely hardware/phone-dependent. Every
other piece — the R6 padding, the R9 offline renders (including the fully
hardware-free ``digital_transfer_probe``), the R10 cross-check, and the R6a /
R4(a) live-proof predicates — is fully implemented here.
"""

from __future__ import annotations

import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import numpy as np

from jasper.active_speaker.camilla_yaml import (
    BASS_EXTENSION_LT_FILTER,
    BASS_EXTENSION_SUBSONIC_FILTER,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.targets import MarginPolicy

from . import cross_check, derivation, live_proof, render, stimulus
from .analysis import digital_clamp_passed, sample_peak_dbfs, transfer_match
from .manifest import StimulusRequest
from .runner import (
    CandidateCapture,
    CandidateMeasurements,
    DiscoveryCapture,
    DiscoveryProbe,
    ReferenceSweepCapture,
    Stop,
    TargetPlan,
)
from .sink import BundleSink

SweepOrSustain = Literal["sweep_transparency", "sustain_stress"]


class ExecutorError(RuntimeError):
    """A live-pass proof, render, or cross-check failed during execution."""


@dataclass(frozen=True, slots=True)
class PlayedStimulus:
    """One admitted stimulus role's complete on-device playback evidence.

    ``stimulus_wav_path`` is the UNPADDED file the existing generators wrote
    (``ensure_sine_wav`` / ``ensure_bandlimited_noise_wav``) — R6 padding is
    applied afterward, by this module, never by :class:`PlayAndCapture`.
    ``live_peak_all_samples`` is every ``get_playback_peak_all()`` reading
    polled at the manifest's recorded interval across the playback (R10(c));
    each entry is one snapshot, in channel order.

    ``quality_verdict`` / ``protection_verdict`` / ``transparency_verdict``
    are ALREADY-COMPUTED verdicts, not raw signals: the frequency-response
    deconvolution, the paired candidate-vs-reference comparison
    (``jasper.bass_extension.bench.analysis.assess_transparency``), and the
    caller-supplied measurement/transparency policy bounds it needs
    (repeat-spread ceiling, SNR floor, transparency RMS bound — none of which
    :class:`~jasper.bass_extension.bench.manifest.StimulusRequest` carries)
    are :class:`PlayAndCapture`'s domain, exactly like ``signal_analysis`` /
    ``protection_analysis`` already are. ``transparency_verdict`` is ``None``
    for ``sustain_stress`` (the frozen schema has no such field there) and
    for the phase-1 reference sweep (``run_reference_sweep`` never reads
    this dataclass); the candidate-phase ``sweep_transparency`` play MUST set
    it.
    """

    stimulus_wav_path: Path
    admission: ArtifactIdentity
    acoustic_capture: ArtifactIdentity
    signal_analysis: ArtifactIdentity
    protection_analysis: ArtifactIdentity
    quality_verdict: str
    protection_verdict: str
    transparency_analysis: ArtifactIdentity | None
    transparency_verdict: str | None
    mux_status_start: Mapping[str, Any]
    mux_status_end: Mapping[str, Any]
    fanin_status_start: Mapping[str, Any]
    fanin_status_end: Mapping[str, Any]
    fader_before_db: float
    fader_before_muted: bool
    fader_after_db: float
    fader_after_muted: bool
    clipped_samples_before: int
    clipped_samples_after: int
    live_peak_all_samples: Sequence[Sequence[float]]
    stimulus_effective_peak_dbfs: float
    commanded_main_volume_db: float
    target_boost_db: float
    hold_duration_s: float
    required_cooldown_s: float
    repeat_count: int


class PlayAndCapture(Protocol):
    """The one on-device seam (see module docstring): admit, play, capture.

    ``reference`` is ``None`` for every call except the candidate phase's
    ``sweep_transparency`` play, where it is the phase-1
    :class:`~jasper.bass_extension.bench.runner.ReferenceSweepCapture` to
    compare against — the implementation needs it to compute
    ``PlayedStimulus.transparency_verdict``/``transparency_analysis``.
    """

    async def play(
        self,
        *,
        target: TargetPlan,
        role: SweepOrSustain,
        request: StimulusRequest,
        stop: Stop,
        reference: ReferenceSweepCapture | None = None,
    ) -> PlayedStimulus: ...


def _wav_header(path: Path) -> derivation.ArtifactHeader:
    with wave.open(str(path), "rb") as source:
        return derivation.ArtifactHeader(
            sample_rate_hz=source.getframerate(),
            channels=source.getnchannels(),
            bits_per_sample=source.getsampwidth() * 8,
        )


@dataclass(slots=True)
class BenchRoleExecutor:
    """The real :class:`~jasper.bass_extension.bench.runner.RoleExecutor`.

    ``requests`` is keyed ``role -> StimulusRequest`` for THIS target — the
    caller (the CLI) selects the target's slice of the campaign manifest.
    ``margin`` is the campaign's selected :class:`MarginPolicy` (from
    ``campaign_manifest.margin_policy_name``), supplied once by the caller —
    this module never derives it.
    """

    target: TargetPlan
    requests: Mapping[str, StimulusRequest]
    controller: Any  # CamillaController-shaped
    mux_status: Callable[[], Awaitable[Mapping[str, Any]]]
    fanin_status: Callable[[], Awaitable[Mapping[str, Any]]]
    play_and_capture: PlayAndCapture
    binary: render.BinaryIdentity
    margin: MarginPolicy
    renders_outstanding: int = 8

    def _bounds_for(self, role: str) -> render.RenderBounds:
        request = self.requests[role]
        return render.RenderBounds(
            timeout_s=request.render_timeout_s,
            rlimit_as_bytes=request.render_rlimit_as_bytes,
            rlimit_cpu_s=request.render_rlimit_cpu_s,
            nice=request.render_nice,
        )

    def _target_dir(self, sink: BundleSink) -> Path:
        target_dir = sink.bundle_dir / self.target.target_id
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def _padding_minima(self, live_active_config_raw: str) -> stimulus.PaddingMinima:
        import yaml

        live = yaml.safe_load(live_active_config_raw)
        if not isinstance(live, dict):
            raise ExecutorError("live active_config_raw is not a mapping")
        pipeline = live.get("pipeline")
        if not isinstance(pipeline, list):
            raise ExecutorError("live config has no pipeline for padding minima")
        owner_set = frozenset(int(c) for c in self.target.owner_channels)
        index, names = derivation._find_owner_step(pipeline, owner_set)
        # boundary_index() call is the R2 existence check for limiter_name in
        # the owner step (raises DerivationError if absent) — the index
        # itself is not needed for padding minima, which use the RETAINED
        # PREFIX up to and including the owner step (the widest possible
        # owner path), not the pre/post-limiter split.
        derivation.boundary_index(names, self.target.limiter_name)
        retained_pipeline = pipeline[: index + 1]
        raw_filters = live.get("filters")
        filters: dict = raw_filters if isinstance(raw_filters, dict) else {}
        raw_mixers = live.get("mixers")
        mixers: dict = raw_mixers if isinstance(raw_mixers, dict) else {}
        owner_path = derivation.owner_path_stages(
            retained_pipeline, mixers, owner_channels=owner_set
        )
        devices = live.get("devices")
        if not isinstance(devices, dict) or type(devices.get("samplerate")) is not int:
            raise ExecutorError("live config has no devices.samplerate")
        filter_defs = [
            filters[name]
            for step in owner_path
            if step.get("type") == "Filter"
            for name in (step.get("names") or ())
            if isinstance(filters, dict) and name in filters
        ]
        return stimulus.compute_padding_minima(
            filter_defs, sample_rate_hz=int(devices["samplerate"])
        )

    def _derive_and_render(
        self,
        *,
        role_tag: str,
        boundary: derivation.Boundary,
        live_active_config_raw: str,
        expected_clip_limit_dbfs: float,
        padded: stimulus.PaddedStimulus,
        sink: BundleSink,
        bounds: render.RenderBounds,
    ) -> tuple[derivation.DerivedConfig, Path, int]:
        """Write the padded WAV + derived config, render TWICE (R8's per-shape
        determinism receipt), and return the derived config, the first
        render's output path, and the live pipeline's playback channel count.
        """

        target_dir = self._target_dir(sink)
        capture_path = target_dir / f"{role_tag}-{boundary}-input.wav"
        capture_path.write_bytes(padded.wav_bytes)
        playback_path = target_dir / f"{role_tag}-{boundary}-output.raw"

        derived = derivation.derive_truncated_config(
            live_active_config_raw,
            boundary=boundary,
            limiter_name=self.target.limiter_name,
            owner_channels=self.target.owner_channels,
            profile_summary=self.target.profile_summary,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
            capture_header=derivation.ArtifactHeader(
                sample_rate_hz=padded.sample_rate_hz,
                channels=padded.channels,
                bits_per_sample=padded.sample_width_bytes * 8,
            ),
            capture_filename=str(capture_path),
            playback_filename=str(playback_path),
            processing_precision=render.DEPLOYED_PROCESSING_PRECISION,
        )
        playback_channels = int(derived.receipt["device_diff"]["playback_channels"])
        sink.write_json(
            f"{self.target.target_id}/{role_tag}-{boundary}-derivation.json",
            derived.receipt,
            kind="jts_bass_extension_bench_derivation_receipt",
        )

        config_path = target_dir / f"{role_tag}-{boundary}-config.yml"
        config_path.write_text(derived.yaml_text, encoding="utf-8")

        total_frames = padded.lead_in_frames + padded.body_frames + padded.lead_out_frames
        render.check_free_space(
            sink.bundle_dir,
            per_render_estimate_bytes=render.estimate_render_bytes(
                padded.sample_rate_hz, playback_channels, total_frames / padded.sample_rate_hz
            ),
            renders_outstanding=self.renders_outstanding,
        )
        self.renders_outstanding = max(0, self.renders_outstanding - 1)

        first_output = playback_path.with_suffix(".first")
        second_output = playback_path.with_suffix(".second")
        determinism = render.render_with_determinism_receipt(
            self.binary.path,
            config_path,
            yaml_text=derived.yaml_text,
            first_output_path=first_output,
            second_output_path=second_output,
            bounds=bounds,
        )
        sink.write_json(
            f"{self.target.target_id}/{role_tag}-{boundary}-determinism.json",
            {
                "config_sha256": render.config_shape_sha256(derived.yaml_text),
                "deterministic": determinism.deterministic,
                "first_sha256": determinism.first.output_sha256,
                "second_sha256": determinism.second.output_sha256,
            },
            kind="jts_bass_extension_bench_determinism_receipt",
        )
        return derived, first_output, playback_channels

    def _extract_body(
        self, output_path: Path, *, channel: int, channels: int, padded: stimulus.PaddedStimulus
    ) -> np.ndarray:
        raw = render.extract_channel(
            output_path, channel_index=channel, channel_count=channels, bytes_per_sample=8
        )
        samples = np.frombuffer(raw, dtype="<f8")
        return samples[padded.lead_in_frames : padded.lead_in_frames + padded.body_frames]

    async def _finish_role(
        self,
        played: PlayedStimulus,
        *,
        role: SweepOrSustain,
        live_active_config_raw: str,
        expected_clip_limit_dbfs: float,
        setting_tag: str,
        sink: BundleSink,
    ) -> dict[str, Any]:
        """R6 padding + R9 pre/post renders + R6a/R4(a)/R10 proofs for one
        role's playback; return the completed measurement-core kwargs bag
        :mod:`bundle` expects (``build_sweep_record`` / ``build_sustain_record``).
        """

        request = self.requests[role]
        minima = self._padding_minima(live_active_config_raw)
        padded = stimulus.pad_stimulus_wav(played.stimulus_wav_path, minima=minima)
        padded_identity = sink.write_bytes(
            f"{self.target.target_id}/{role}-{setting_tag}-padded.wav",
            padded.wav_bytes,
            kind="jts_bass_extension_bench_padded_stimulus",
        )
        sink.write_json(
            f"{self.target.target_id}/{role}-{setting_tag}-padding-minima.json",
            minima.to_receipt(),
            kind="jts_bass_extension_bench_padding_receipt",
        )

        bounds = self._bounds_for(role)
        pre_derived, pre_output, playback_channels = self._derive_and_render(
            role_tag=f"{role}-{setting_tag}",
            boundary="pre_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
        )
        post_derived, post_output, _ = self._derive_and_render(
            role_tag=f"{role}-{setting_tag}",
            boundary="post_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
        )
        del pre_derived, post_derived  # receipts already written by _derive_and_render

        live_proof.prove_ingress_transparency(
            live_proof.IngressProofInputs(
                mux_status=played.mux_status_start,
                fanin_status_start=played.fanin_status_start,
                fanin_status_end=played.fanin_status_end,
                artifact_header=_wav_header(played.stimulus_wav_path),
            )
        )
        live_proof.prove_fader_bracket(
            live_proof.FaderBracket(
                before_db=played.fader_before_db,
                before_muted=played.fader_before_muted,
                after_db=played.fader_after_db,
                after_muted=played.fader_after_muted,
            )
        )

        pre_peaks: dict[int, float] = {}
        post_peaks: dict[int, float] = {}
        pre_body_by_channel: dict[int, np.ndarray] = {}
        post_body_by_channel: dict[int, np.ndarray] = {}
        for channel in self.target.owner_channels:
            pre_body = self._extract_body(
                pre_output, channel=channel, channels=playback_channels, padded=padded
            )
            post_body = self._extract_body(
                post_output, channel=channel, channels=playback_channels, padded=padded
            )
            pre_body_by_channel[channel] = pre_body
            post_body_by_channel[channel] = post_body
            pre_peaks[channel] = sample_peak_dbfs(pre_body)
            post_peaks[channel] = sample_peak_dbfs(post_body)

        live_peak_all_max: list[float] = []
        for snapshot in played.live_peak_all_samples:
            for index, value in enumerate(snapshot):
                while len(live_peak_all_max) <= index:
                    live_peak_all_max.append(float("-inf"))
                live_peak_all_max[index] = max(live_peak_all_max[index], value)

        bound_db = max(
            cross_check.compute_permissive_tolerance_bound_db(
                post_body_by_channel[channel],
                sample_rate_hz=padded.sample_rate_hz,
                poll_interval_s=request.cross_check_poll_interval_s,
            )
            for channel in self.target.owner_channels
        )
        cross_check.cross_check_owner_channels(
            owner_channels=self.target.owner_channels,
            rendered_peaks_dbfs=post_peaks,
            live_peak_all=live_peak_all_max,
            recorded_main_volume_db=played.commanded_main_volume_db,
            # See cross_check.py's module docstring: verified against the
            # pinned CamillaDSP v4.1.3 source, the main fader always precedes
            # the entire pipeline (hence the owner limiter) for this build, so
            # R4(c) always resolves to "reproduce the recorded fader gain" —
            # the render always carries it.
            render_carries_fader_gain=True,
            tolerance_db=request.cross_check_tolerance_db,
            computed_bound_db=bound_db,
            clipped_samples_before=played.clipped_samples_before,
            clipped_samples_after=played.clipped_samples_after,
        )

        pre_pcm_id = sink.record_existing(
            pre_output,
            relative_path=f"{self.target.target_id}/{role}-{setting_tag}-pre.raw",
            kind="jts_bass_extension_bench_pre_limiter_pcm",
        )
        post_pcm_id = sink.record_existing(
            post_output,
            relative_path=f"{self.target.target_id}/{role}-{setting_tag}-post.raw",
            kind="jts_bass_extension_bench_post_limiter_pcm",
        )

        # R3: multi-entry owner_channels never collapse before the runner's
        # distinct-peak inventory — but this bag's `pre_limiter_peak_dbfs` /
        # `post_limiter_peak_dbfs` are single numbers by the frozen schema.
        # The MINIMUM across owner channels is R3's pinned conservative
        # collapse rule for exactly this situation.
        pre_limiter_peak_dbfs = cross_check.min_across_channels(list(pre_peaks.values()))
        post_limiter_peak_dbfs = cross_check.min_across_channels(list(post_peaks.values()))
        clamp_passed = digital_clamp_passed(pre_limiter_peak_dbfs, self.margin)

        return {
            "stimulus": padded_identity,
            "admission": played.admission,
            "pre_limiter_pcm": pre_pcm_id,
            "post_limiter_pcm": post_pcm_id,
            "acoustic_capture": played.acoustic_capture,
            "signal_analysis": played.signal_analysis,
            "protection_analysis": played.protection_analysis,
            "stimulus_band_hz": tuple(request.requested_stimulus_band_hz),
            "stimulus_effective_peak_dbfs": played.stimulus_effective_peak_dbfs,
            "commanded_main_volume_db": played.commanded_main_volume_db,
            "target_boost_db": played.target_boost_db,
            "digital_clamp_passed": clamp_passed,
            "pre_limiter_peak_dbfs": pre_limiter_peak_dbfs,
            "post_limiter_peak_dbfs": post_limiter_peak_dbfs,
            "hold_duration_s": played.hold_duration_s,
            "required_cooldown_s": played.required_cooldown_s,
            "repeat_count": played.repeat_count,
            "quality_verdict": played.quality_verdict,
            "protection_verdict": played.protection_verdict,
        }

    async def _digital_transfer_probe(
        self,
        *,
        candidate_setting_dbfs: float,
        sink: BundleSink,
    ) -> dict[str, Any]:
        """R9's frozen step 4: fully hardware-free — a deterministic,
        content-addressed sample program rendered through an isolated
        CamillaDSP file sink, never reaching hardware. Runs synchronously,
        inside the window, at the safe floor."""

        raw = await self.controller.get_active_config_raw()
        request = self.requests["digital_transfer_probe"]
        target_dir = self._target_dir(sink)
        stimulus_path = target_dir / "transfer-probe-stimulus.wav"
        from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav

        import yaml

        live = yaml.safe_load(raw)
        if not isinstance(live, dict) or type(live.get("devices", {}).get("samplerate")) is not int:
            raise ExecutorError("live config has no devices.samplerate")
        sample_rate_hz = int(live["devices"]["samplerate"])
        generator_wav = ensure_bandlimited_noise_wav(
            f_lo_hz=request.requested_stimulus_band_hz[0],
            f_hi_hz=request.requested_stimulus_band_hz[1],
            duration_s=request.requested_hold_duration_s,
            dbfs=request.requested_stimulus_effective_peak_dbfs,
            sample_rate=sample_rate_hz,
            cache_dir=target_dir,
        )
        stimulus_path.write_bytes(generator_wav.read_bytes())
        minima = self._padding_minima(raw)
        padded = stimulus.pad_stimulus_wav(stimulus_path, minima=minima)
        stimulus_identity = sink.write_bytes(
            f"{self.target.target_id}/transfer-probe-{candidate_setting_dbfs:g}-padded.wav",
            padded.wav_bytes,
            kind="jts_bass_extension_bench_padded_stimulus",
        )

        bounds = self._bounds_for("digital_transfer_probe")
        setting_tag = f"transfer-{candidate_setting_dbfs:g}"
        pre_derived, pre_output, playback_channels = self._derive_and_render(
            role_tag=setting_tag,
            boundary="pre_limiter",
            live_active_config_raw=raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
        )
        _, post_output, _ = self._derive_and_render(
            role_tag=setting_tag,
            boundary="post_limiter",
            live_active_config_raw=raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
        )
        del pre_derived

        channel = self.target.owner_channels[0]
        pre_body = self._extract_body(
            pre_output, channel=channel, channels=playback_channels, padded=padded
        )
        post_body = self._extract_body(
            post_output, channel=channel, channels=playback_channels, padded=padded
        )
        reference_body = render.reference_soft_clip(
            pre_body, clip_limit_dbfs=candidate_setting_dbfs
        )
        reference_bytes = reference_body.astype("<f8").tobytes()

        pre_pcm_id = sink.record_existing(
            pre_output,
            relative_path=f"{self.target.target_id}/{setting_tag}-pre.raw",
            kind="jts_bass_extension_bench_pre_limiter_pcm",
        )
        post_pcm_id = sink.record_existing(
            post_output,
            relative_path=f"{self.target.target_id}/{setting_tag}-post.raw",
            kind="jts_bass_extension_bench_post_limiter_pcm",
        )
        reference_id = sink.write_bytes(
            f"{self.target.target_id}/{setting_tag}-reference-post.raw",
            reference_bytes,
            kind="jts_bass_extension_bench_reference_post_limiter_pcm",
        )
        post_bytes = post_body.astype("<f8").tobytes()
        analysis_id = sink.write_json(
            f"{self.target.target_id}/{setting_tag}-transfer-analysis.json",
            {
                "deployed_sha256": post_pcm_id.sha256,
                "reference_sha256": reference_id.sha256,
                "deployed_byte_size": len(post_bytes),
                "reference_byte_size": len(reference_bytes),
            },
            kind="jts_bass_extension_bench_transfer_analysis",
        )
        verdict = transfer_match(
            deployed_sha256=post_pcm_id.sha256,
            deployed_byte_size=len(post_bytes),
            reference_sha256=reference_id.sha256,
            reference_byte_size=len(reference_bytes),
        )
        return {
            "stimulus": stimulus_identity,
            "pre_limiter_pcm": pre_pcm_id,
            "post_limiter_pcm": post_pcm_id,
            "reference_post_limiter_pcm": reference_id,
            "transfer_analysis": analysis_id,
            "verdict": verdict,
        }

    async def run_discovery(
        self,
        *,
        target: TargetPlan,
        active_graph_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> Sequence[DiscoveryCapture]:
        captures: list[DiscoveryCapture] = []
        for role in ("sweep_transparency", "sustain_stress"):
            stop.check()
            request = self.requests[role]
            played = await self.play_and_capture.play(
                target=target, role=role, request=request, stop=stop
            )
            captures.append(
                DiscoveryCapture(
                    stimulus=played.admission,
                    admission=played.admission,
                    render_inputs={
                        "played": played,
                        "role": role,
                        "active_graph_readback": active_graph_readback,
                    },
                )
            )
        return captures

    async def finish_discovery(
        self,
        captures: Sequence[DiscoveryCapture],
        *,
        target: TargetPlan,
        sink: BundleSink,
        stop: Stop,
    ) -> Sequence[DiscoveryProbe]:
        raw = await self.controller.get_active_config_raw()
        probes: list[DiscoveryProbe] = []
        for capture in captures:
            stop.check()
            played = cast(PlayedStimulus, capture.render_inputs["played"])
            role = cast(str, capture.render_inputs["role"])
            active_graph_readback = cast(
                ArtifactIdentity, capture.render_inputs["active_graph_readback"]
            )
            minima = self._padding_minima(raw)
            padded = stimulus.pad_stimulus_wav(played.stimulus_wav_path, minima=minima)
            padded_identity = sink.write_bytes(
                f"{target.target_id}/discovery-{role}-padded.wav",
                padded.wav_bytes,
                kind="jts_bass_extension_bench_padded_stimulus",
            )
            derived, output_path, playback_channels = self._derive_and_render(
                role_tag=f"discovery-{role}",
                boundary="pre_limiter",
                live_active_config_raw=raw,
                expected_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
                padded=padded,
                sink=sink,
                bounds=self._bounds_for(role),
            )
            del derived  # receipt already written

            per_channel_peaks: list[float] = []
            pcm_id: ArtifactIdentity | None = None
            for channel in target.owner_channels:
                body = self._extract_body(
                    output_path, channel=channel, channels=playback_channels, padded=padded
                )
                per_channel_peaks.append(sample_peak_dbfs(body))
                pcm_id = sink.record_existing(
                    output_path,
                    relative_path=f"{target.target_id}/discovery-{role}-{channel}-pre.raw",
                    kind="jts_bass_extension_bench_pre_limiter_pcm",
                )
            assert pcm_id is not None
            peak_analysis_id = sink.write_json(
                f"{target.target_id}/discovery-{role}-peak-analysis.json",
                {
                    "owner_channels": list(target.owner_channels),
                    "per_channel_peaks_dbfs": per_channel_peaks,
                },
                kind="jts_bass_extension_bench_peak_analysis",
            )
            probes.append(
                DiscoveryProbe(
                    stimulus=padded_identity,
                    admission=played.admission,
                    active_graph_readback=active_graph_readback,
                    pre_limiter_pcm=pcm_id,
                    peak_analysis=peak_analysis_id,
                    pre_limiter_peak_dbfs=cross_check.min_across_channels(per_channel_peaks),
                )
            )
        return probes

    async def run_reference_sweep(
        self,
        *,
        target: TargetPlan,
        reference_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> ReferenceSweepCapture:
        request = self.requests["sweep_transparency"]
        played = await self.play_and_capture.play(
            target=target, role="sweep_transparency", request=request, stop=stop
        )
        stimulus_identity = sink.record_existing(
            played.stimulus_wav_path,
            relative_path=f"{target.target_id}/reference-sweep-stimulus.wav",
            kind="jts_bass_extension_bench_stimulus",
        )
        return ReferenceSweepCapture(
            reference_stimulus=stimulus_identity,
            reference_admission=played.admission,
            reference_acoustic_capture=played.acoustic_capture,
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
        transfer = await self._digital_transfer_probe(
            candidate_setting_dbfs=candidate_setting_dbfs, sink=sink
        )
        stop.check()
        sweep_request = self.requests["sweep_transparency"]
        sustain_request = self.requests["sustain_stress"]
        sweep_played = await self.play_and_capture.play(
            target=target,
            role="sweep_transparency",
            request=sweep_request,
            stop=stop,
            reference=reference,
        )
        sustain_played = await self.play_and_capture.play(
            target=target, role="sustain_stress", request=sustain_request, stop=stop
        )
        # `candidate_readback` names the runner's activation read-back receipt
        # (jasper.bass_extension.bench.runner._readback_receipt's JSON, written
        # BEFORE run_candidate is invoked). By the time this method runs, the
        # runner's temporary_bass_activation has already proven the owner chain
        # order via _prove_active_graph — read the receipt back for its
        # graph_fingerprint rather than re-deriving it.
        import json

        receipt_path = sink.bundle_dir / candidate_readback.relative_path
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            sweep_played.transparency_analysis is None
            or sweep_played.transparency_verdict is None
        ):
            raise ExecutorError(
                "PlayAndCapture.play(role='sweep_transparency', ...) for the "
                "candidate phase must set transparency_analysis/"
                "transparency_verdict (the paired candidate-vs-reference "
                "comparison) — got None"
            )
        return CandidateCapture(
            digital_transfer_probe=transfer,
            sweep_live={},
            sweep_render_inputs={"played": sweep_played, "setting": candidate_setting_dbfs},
            sustain_live={},
            sustain_render_inputs={"played": sustain_played, "setting": candidate_setting_dbfs},
            transparency_analysis=sweep_played.transparency_analysis,
            transparency_verdict=sweep_played.transparency_verdict,
            active_graph_fingerprint=str(receipt["active_graph_fingerprint"]),
            ordered_owner_chain=(
                BASS_EXTENSION_LT_FILTER,
                BASS_EXTENSION_SUBSONIC_FILTER,
                target.limiter_name,
            ),
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
        raw = await self.controller.get_active_config_raw()
        setting_tag = f"{candidate_setting_dbfs:g}"
        sweep_played = cast(PlayedStimulus, capture.sweep_render_inputs["played"])
        sustain_played = cast(PlayedStimulus, capture.sustain_render_inputs["played"])
        sweep_core = await self._finish_role(
            sweep_played,
            role="sweep_transparency",
            live_active_config_raw=raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            setting_tag=setting_tag,
            sink=sink,
        )
        stop.check()
        sustain_core = await self._finish_role(
            sustain_played,
            role="sustain_stress",
            live_active_config_raw=raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            setting_tag=setting_tag,
            sink=sink,
        )
        return CandidateMeasurements(
            digital_transfer_probe=capture.digital_transfer_probe,
            sweep_core=sweep_core,
            sustain_stress=sustain_core,
            transparency_analysis=capture.transparency_analysis,
            transparency_verdict=capture.transparency_verdict,
            active_graph_fingerprint=capture.active_graph_fingerprint,
            ordered_owner_chain=capture.ordered_owner_chain,
            configured_clip_limit_dbfs=capture.configured_clip_limit_dbfs,
        )


__all__ = [
    "BenchRoleExecutor",
    "ExecutorError",
    "PlayAndCapture",
    "PlayedStimulus",
]
