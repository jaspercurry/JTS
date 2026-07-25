# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The real, on-device :class:`~jasper.bass_extension.bench.runner.RoleExecutor`.

Composes the derivation / render / stimulus / live-proof / cross-check
modules (this PR's new tap-realization logic) with the injected
:class:`PlayAndCapture` collaborator to implement the two-phase
:class:`~jasper.bass_extension.bench.runner.RoleExecutor` protocol.

**Scope boundary — the one injected seam.** Admitting and PLAYING an
ALREADY-PREPARED stimulus through hardware while near-field-capturing the
acoustic response over the phone capture-relay session is the piece the
CLI's original stub named as "the one piece with no in-tree helper to
compose": no existing function in this tree opens a capture-relay session,
waits for a phone to connect and upload, and returns the result end to end,
and doing so correctly is fundamentally an on-device, hardware-verified
exercise. This module takes that ONE step as an injected collaborator
(:class:`PlayAndCapture`) — mirroring the SAME dependency-injection shape
the runner already uses for ``BenchDeps`` (``controller`` / ``floor`` /
``executor``), applied one level further down at the point that is
genuinely hardware/phone-dependent.

Every other piece is fully implemented here, including stimulus GENERATION
and R6 padding: this module generates every role's unpadded stimulus from
the operator-authorized
:class:`~jasper.bass_extension.bench.manifest.StimulusRequest` (the same
hardware-free ``ensure_bandlimited_noise_wav`` toolkit
``digital_transfer_probe`` always used), pads it, and only THEN hands the
one content-addressed padded artifact to :meth:`PlayAndCapture.play` —
never the other way around. This is R6's own requirement: live playback and
the offline pre/post-limiter renders must consume the EXACT SAME bytes, so
:class:`PlayAndCapture` never generates or pads anything itself; it plays
whatever :class:`Path` it is given. The R9 offline renders (including the
fully hardware-free ``digital_transfer_probe``), the R10 cross-check, and
the R6a / R4(a) live-proof predicates are likewise fully implemented here.

**Stimulus-generator judgment call (flagged for review).** The frozen
protocol deliberately pins no stimulus-generation algorithm ("The protocol
contains no stimulus, level, frequency, duration, cooldown, repeat, or
limiter number" — ``limiter-evidence-protocol.md``). Neither existing
generator is literally a frequency sweep (``ensure_sine_wav`` is one fixed
tone; ``ensure_bandlimited_noise_wav`` is static band-limited noise).
``sustain_stress``'s "deterministic band-limited noise program" is an
unambiguous match for ``ensure_bandlimited_noise_wav``;
``sweep_transparency``'s "narrow bass sweep" does not cleanly match either
helper. :func:`_generate_stimulus_wav` uses ``ensure_bandlimited_noise_wav``
uniformly for both roles, banded by the request's own
``requested_stimulus_band_hz`` — consistent with ``digital_transfer_probe``'s
existing, already-accepted pattern for turning a
:class:`~jasper.bass_extension.bench.manifest.StimulusRequest` into a
stimulus, and because it needs no invented parameter (a literal frequency
sweep would need an invented frequency trajectory the manifest does not
carry). Revisit if a real hardware :class:`PlayAndCapture` binding needs a
literal swept-frequency stimulus for ``sweep_transparency``.
"""

from __future__ import annotations

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
from .bundle import build_sustain_record, build_transfer_record
from .manifest import CampaignManifest, StimulusRequest
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

    There is deliberately no stimulus path here: the EXECUTOR generates and
    pads the stimulus and hands the resulting file to
    :meth:`PlayAndCapture.play` (``stimulus_path``) — by the time ``play()``
    returns, the caller already knows exactly what was played (it built the
    file). ``live_peak_all_samples`` is every ``get_playback_peak_all()``
    reading polled at the manifest's recorded interval across the playback
    (R10(c)); each entry is one snapshot, in channel order.

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

    ``stimulus_path`` is the ALREADY-GENERATED, ALREADY-PADDED artifact
    (built and content-addressed by the executor, per R6) — a correct
    implementation admits and plays exactly this file; it never generates or
    modifies stimulus bytes itself. ``reference`` is ``None`` for every call
    except the candidate phase's ``sweep_transparency`` play, where it is the
    phase-1 :class:`~jasper.bass_extension.bench.runner.ReferenceSweepCapture`
    to compare against — the implementation needs it to compute
    ``PlayedStimulus.transparency_verdict``/``transparency_analysis``.
    """

    async def play(
        self,
        *,
        target: TargetPlan,
        role: SweepOrSustain,
        request: StimulusRequest,
        stop: Stop,
        stimulus_path: Path,
        reference: ReferenceSweepCapture | None = None,
    ) -> PlayedStimulus: ...


def _generate_stimulus_wav(
    *, role: str, request: StimulusRequest, sample_rate_hz: int, target_dir: Path
) -> Path:
    """Synthesize ``role``'s unpadded stimulus from the operator-authorized
    request. See the module docstring's "Stimulus-generator judgment call"
    for why ``ensure_bandlimited_noise_wav`` is used uniformly across roles.

    ``ensure_bandlimited_noise_wav`` writes MONO audio; R6a's fan-in mix
    geometry is fixed at ``live_proof.FANIN_MIX_CHANNELS`` (2 —
    ``rust/jasper-fanin/src/mixer.rs``'s ``pub const CHANNELS: u32 = 2``,
    "the mix is always 2-channel … regardless of any one lane's own source
    geometry") REGARDLESS of how many owner channels the downstream
    limiter/pipeline touches, so the stimulus that is actually admitted and
    played must be 2-channel too. The generated mono signal is duplicated to
    both channels (L=R) rather than drawing two independent noise
    realizations — an L=R signal keeps the owner path's split-mixer sum
    (equal-gain from channels 0 and 1) a deterministic, calculable multiple
    of the source rather than a stochastic function of two independent
    draws' phase relationship.
    """

    from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav

    mono_path = ensure_bandlimited_noise_wav(
        f_lo_hz=request.requested_stimulus_band_hz[0],
        f_hi_hz=request.requested_stimulus_band_hz[1],
        duration_s=request.requested_hold_duration_s,
        dbfs=request.requested_stimulus_effective_peak_dbfs,
        sample_rate=sample_rate_hz,
        cache_dir=target_dir,
    )
    stereo_path = target_dir / f"{mono_path.stem}-stereo{mono_path.suffix}"
    if not stereo_path.exists():
        import wave

        with wave.open(str(mono_path), "rb") as source:
            sample_width = source.getsampwidth()
            frame_rate = source.getframerate()
            mono_frames = source.readframes(source.getnframes())
        samples = np.frombuffer(mono_frames, dtype=f"<i{sample_width}")
        stereo = np.repeat(samples, 2)
        with wave.open(str(stereo_path), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(sample_width)
            out.setframerate(frame_rate)
            out.writeframes(stereo.tobytes())
    return stereo_path


def _live_sample_rate_hz(live_active_config_raw: str) -> int:
    import yaml

    live = yaml.safe_load(live_active_config_raw)
    if not isinstance(live, dict) or type(live.get("devices", {}).get("samplerate")) is not int:
        raise ExecutorError("live config has no devices.samplerate")
    return int(live["devices"]["samplerate"])


def estimate_campaign_render_count(manifest: CampaignManifest) -> int:
    """S4: a threaded, campaign-derived ``renders_outstanding`` seed — never
    an invented per-target literal.

    Counts ``targets * (2 discovery pre_limiter renders [sweep_transparency,
    sustain_stress] + 6 one-candidate renders [digital_transfer_probe,
    sweep_transparency, sustain_stress, each pre+post]) = targets * 8``.

    JUDGMENT CALL (flagged for review): the true candidate count per target
    is data-dependent (the discovery pass's DISTINCT measured pre-limiter
    peaks, unknown until discovery runs) and cannot be derived from the
    static manifest alone. This estimates exactly ONE evaluated candidate
    per target — the smallest possible campaign shape — so a target whose
    discovery surfaces multiple distinct candidates under-estimates that
    target's true render count. This is an acceptable direction to be wrong
    in: :func:`~jasper.bass_extension.bench.render.check_free_space` is an
    early-warning disk-space sanity check re-run before EVERY render (not a
    one-time gate), so under-estimating narrows the warning margin rather
    than silently permitting an actual out-of-space failure.
    """

    return len(manifest.requests) * 8


@dataclass(slots=True)
class BenchRoleExecutor:
    """The real :class:`~jasper.bass_extension.bench.runner.RoleExecutor`.

    ``requests`` is keyed ``role -> StimulusRequest`` for THIS target — the
    caller (the CLI) selects the target's slice of the campaign manifest.
    ``margin`` is the campaign's selected :class:`MarginPolicy` (from
    ``campaign_manifest.margin_policy_name``), supplied once by the caller —
    this module never derives it. ``renders_outstanding`` has no default
    (S4): the caller seeds it from :func:`estimate_campaign_render_count`,
    never an invented literal.
    """

    target: TargetPlan
    requests: Mapping[str, StimulusRequest]
    controller: Any  # CamillaController-shaped
    mux_status: Callable[[], Awaitable[Mapping[str, Any]]]
    fanin_status: Callable[[], Awaitable[Mapping[str, Any]]]
    play_and_capture: PlayAndCapture
    binary: render.BinaryIdentity
    margin: MarginPolicy
    renders_outstanding: int

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

    async def _prepare_and_play(
        self,
        *,
        target: TargetPlan,
        role: SweepOrSustain,
        request: StimulusRequest,
        live_active_config_raw: str,
        sink: BundleSink,
        stop: Stop,
        tag: str,
        reference: ReferenceSweepCapture | None = None,
    ) -> tuple[PlayedStimulus, stimulus.PaddedStimulus, ArtifactIdentity]:
        """R6: generate + pad this role's stimulus BEFORE playback, then hand
        the one content-addressed padded artifact to
        :class:`PlayAndCapture` — so live playback and the offline
        pre/post-limiter renders always consume the exact same bytes. Never
        re-derived afterward from a possibly-stale (post-restore) config.
        """

        target_dir = self._target_dir(sink)
        sample_rate_hz = _live_sample_rate_hz(live_active_config_raw)
        raw_path = _generate_stimulus_wav(
            role=role, request=request, sample_rate_hz=sample_rate_hz, target_dir=target_dir
        )
        minima = self._padding_minima(live_active_config_raw)
        padded = stimulus.pad_stimulus_wav(raw_path, minima=minima)
        padded_identity = sink.write_bytes(
            f"{self.target.target_id}/{tag}-padded.wav",
            padded.wav_bytes,
            kind="jts_bass_extension_bench_padded_stimulus",
        )
        sink.write_json(
            f"{self.target.target_id}/{tag}-padding-minima.json",
            minima.to_receipt(),
            kind="jts_bass_extension_bench_padding_receipt",
        )
        padded_path = sink.bundle_dir / padded_identity.relative_path
        played = await self.play_and_capture.play(
            target=target,
            role=role,
            request=request,
            stop=stop,
            stimulus_path=padded_path,
            reference=reference,
        )
        return played, padded, padded_identity

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
        fader_db: float,
    ) -> tuple[derivation.DerivedConfig, Path, int]:
        """Write the padded WAV + derived config, render TWICE (R8's per-shape
        determinism receipt), and return the derived config, the first
        render's output path, and the live pipeline's playback channel count.

        ``fader_db`` (R4(a)'s bracketed, locked-measurement-level main-volume
        reading — or ``0.0`` for the fully-synthetic, no-live-playback
        ``digital_transfer_probe``) is threaded into ``--gain`` (R4(c)/R9):
        every render must reproduce the same fader attenuation the live pass
        (if any) carried, or the rendered peak is systematically off by the
        fader's dB. See ``render.py``'s ``render_config`` docstring.
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
            fader_db=fader_db,
        )
        sink.write_json(
            f"{self.target.target_id}/{role_tag}-{boundary}-determinism.json",
            {
                "config_sha256": render.config_shape_sha256(derived.yaml_text),
                "deterministic": determinism.deterministic,
                "first_sha256": determinism.first.output_sha256,
                "second_sha256": determinism.second.output_sha256,
                "fader_db": fader_db,
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
        padded: stimulus.PaddedStimulus,
        padded_identity: ArtifactIdentity,
        expected_clip_limit_dbfs: float,
        setting_tag: str,
        sink: BundleSink,
    ) -> dict[str, Any]:
        """R9 pre/post renders + R6a/R4(a)/R10 proofs for one role's
        playback; return the completed measurement-core kwargs bag
        :mod:`bundle` expects (``build_sweep_record`` / ``build_sustain_record``).

        ``padded``/``padded_identity`` are the SAME padded stimulus that was
        actually played (computed and recorded by the caller BEFORE
        playback, per R6) — this method renders from and records evidence
        about that one artifact; it never re-pads.
        """

        request = self.requests[role]
        bounds = self._bounds_for(role)
        pre_derived, pre_output, playback_channels = self._derive_and_render(
            role_tag=f"{role}-{setting_tag}",
            boundary="pre_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
            fader_db=played.commanded_main_volume_db,
        )
        post_derived, post_output, _ = self._derive_and_render(
            role_tag=f"{role}-{setting_tag}",
            boundary="post_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
            fader_db=played.commanded_main_volume_db,
        )
        del pre_derived, post_derived  # receipts already written by _derive_and_render

        live_proof.prove_ingress_transparency(
            live_proof.IngressProofInputs(
                mux_status=played.mux_status_start,
                mux_status_end=played.mux_status_end,
                fanin_status_start=played.fanin_status_start,
                fanin_status_end=played.fanin_status_end,
                artifact_header=derivation.ArtifactHeader(
                    sample_rate_hz=padded.sample_rate_hz,
                    channels=padded.channels,
                    bits_per_sample=padded.sample_width_bytes * 8,
                ),
            )
        )
        live_proof.prove_fader_bracket(
            live_proof.FaderBracket(
                before_db=played.fader_before_db,
                before_muted=played.fader_before_muted,
                after_db=played.fader_after_db,
                after_muted=played.fader_after_muted,
                commanded_main_volume_db=played.commanded_main_volume_db,
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

        # S5: the permissive tolerance bound is per-channel — each channel's
        # own rendered envelope, never max-collapsed across channels (two
        # owner channels can legitimately have different envelopes and
        # therefore different permissive bounds).
        computed_bounds_db: dict[int, float] = {
            channel: cross_check.compute_permissive_tolerance_bound_db(
                post_body_by_channel[channel],
                sample_rate_hz=padded.sample_rate_hz,
                poll_interval_s=request.cross_check_poll_interval_s,
            )
            for channel in self.target.owner_channels
        }
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
            computed_bounds_db=computed_bounds_db,
            clipped_samples_before=played.clipped_samples_before,
            clipped_samples_after=played.clipped_samples_after,
        )

        # S7: recorded pre/post PCM artifacts are each owner channel's
        # EXTRACTED stream (R3), never the full interleaved render.
        pre_pcm_ids: dict[int, ArtifactIdentity] = {}
        post_pcm_ids: dict[int, ArtifactIdentity] = {}
        for channel in self.target.owner_channels:
            pre_pcm_ids[channel] = sink.write_bytes(
                f"{self.target.target_id}/{role}-{setting_tag}-{channel}-pre.raw",
                pre_body_by_channel[channel].astype("<f8").tobytes(),
                kind="jts_bass_extension_bench_pre_limiter_pcm",
            )
            post_pcm_ids[channel] = sink.write_bytes(
                f"{self.target.target_id}/{role}-{setting_tag}-{channel}-post.raw",
                post_body_by_channel[channel].astype("<f8").tobytes(),
                kind="jts_bass_extension_bench_post_limiter_pcm",
            )

        # R3: multi-entry owner_channels never collapse before the runner's
        # distinct-peak inventory — but this bag's `pre_limiter_peak_dbfs` /
        # `post_limiter_peak_dbfs` are single numbers by the frozen schema.
        # The MINIMUM across owner channels is R3's pinned conservative
        # collapse rule for exactly this situation; the recorded singular PCM
        # artifact stays evidentially consistent by picking the SAME channel
        # that produced the recorded peak (S7).
        pre_limiter_peak_dbfs = cross_check.min_across_channels(list(pre_peaks.values()))
        post_limiter_peak_dbfs = cross_check.min_across_channels(list(post_peaks.values()))
        min_pre_channel = min(pre_peaks, key=lambda c: pre_peaks[c])
        min_post_channel = min(post_peaks, key=lambda c: post_peaks[c])
        clamp_passed = digital_clamp_passed(pre_limiter_peak_dbfs, self.margin)

        return {
            "stimulus": padded_identity,
            "admission": played.admission,
            "pre_limiter_pcm": pre_pcm_ids[min_pre_channel],
            "post_limiter_pcm": post_pcm_ids[min_post_channel],
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
        live_active_config_raw: str,
        sink: BundleSink,
    ) -> dict[str, Any]:
        """R9's frozen step 4: fully hardware-free — a deterministic,
        content-addressed sample program rendered through an isolated
        CamillaDSP file sink, never reaching hardware. Runs synchronously,
        inside the window, at the safe floor (``fader_db=0.0`` — there is no
        live playback in this step, so there is no commanded main-volume
        level to reproduce; every render still threads an explicit
        ``--gain``, see ``render.py``'s ``render_config`` docstring).

        S8: every owner channel is checked — the verdict never collapses to
        ``owner_channels[0]``. B4: the "deployed" post-limiter artifact
        compared against the reference is the EXTRACTED single-channel body
        (matching the reference transform's channel/length exactly), never
        the whole interleaved render.
        """

        request = self.requests["digital_transfer_probe"]
        target_dir = self._target_dir(sink)
        sample_rate_hz = _live_sample_rate_hz(live_active_config_raw)
        generator_wav = _generate_stimulus_wav(
            role="digital_transfer_probe",
            request=request,
            sample_rate_hz=sample_rate_hz,
            target_dir=target_dir,
        )
        minima = self._padding_minima(live_active_config_raw)
        padded = stimulus.pad_stimulus_wav(generator_wav, minima=minima)
        setting_tag = f"transfer-{candidate_setting_dbfs:g}"
        stimulus_identity = sink.write_bytes(
            f"{self.target.target_id}/{setting_tag}-padded.wav",
            padded.wav_bytes,
            kind="jts_bass_extension_bench_padded_stimulus",
        )

        bounds = self._bounds_for("digital_transfer_probe")
        pre_derived, pre_output, playback_channels = self._derive_and_render(
            role_tag=setting_tag,
            boundary="pre_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
            fader_db=0.0,
        )
        _, post_output, _ = self._derive_and_render(
            role_tag=setting_tag,
            boundary="post_limiter",
            live_active_config_raw=live_active_config_raw,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            padded=padded,
            sink=sink,
            bounds=bounds,
            fader_db=0.0,
        )
        del pre_derived

        per_channel: dict[int, dict[str, Any]] = {}
        for channel in self.target.owner_channels:
            pre_body = self._extract_body(
                pre_output, channel=channel, channels=playback_channels, padded=padded
            )
            post_body = self._extract_body(
                post_output, channel=channel, channels=playback_channels, padded=padded
            )
            reference_body = render.reference_soft_clip(
                pre_body, clip_limit_dbfs=candidate_setting_dbfs
            )
            pre_bytes = pre_body.astype("<f8").tobytes()
            post_bytes = post_body.astype("<f8").tobytes()
            reference_bytes = reference_body.astype("<f8").tobytes()

            pre_pcm_id = sink.write_bytes(
                f"{self.target.target_id}/{setting_tag}-{channel}-pre.raw",
                pre_bytes,
                kind="jts_bass_extension_bench_pre_limiter_pcm",
            )
            post_pcm_id = sink.write_bytes(
                f"{self.target.target_id}/{setting_tag}-{channel}-post.raw",
                post_bytes,
                kind="jts_bass_extension_bench_post_limiter_pcm",
            )
            reference_id = sink.write_bytes(
                f"{self.target.target_id}/{setting_tag}-{channel}-reference-post.raw",
                reference_bytes,
                kind="jts_bass_extension_bench_reference_post_limiter_pcm",
            )
            channel_verdict = transfer_match(
                deployed_sha256=post_pcm_id.sha256,
                deployed_byte_size=len(post_bytes),
                reference_sha256=reference_id.sha256,
                reference_byte_size=len(reference_bytes),
            )
            per_channel[channel] = {
                "pre_limiter_pcm": pre_pcm_id,
                "post_limiter_pcm": post_pcm_id,
                "reference_post_limiter_pcm": reference_id,
                "deployed_sha256": post_pcm_id.sha256,
                "reference_sha256": reference_id.sha256,
                "deployed_byte_size": len(post_bytes),
                "reference_byte_size": len(reference_bytes),
                "verdict": channel_verdict,
            }

        verdict = (
            "pass" if all(entry["verdict"] == "pass" for entry in per_channel.values())
            else "fail"
        )
        analysis_id = sink.write_json(
            f"{self.target.target_id}/{setting_tag}-transfer-analysis.json",
            {
                "owner_channels": list(self.target.owner_channels),
                "per_channel": {
                    str(channel): {
                        "deployed_sha256": entry["deployed_sha256"],
                        "reference_sha256": entry["reference_sha256"],
                        "deployed_byte_size": entry["deployed_byte_size"],
                        "reference_byte_size": entry["reference_byte_size"],
                        "verdict": entry["verdict"],
                    }
                    for channel, entry in per_channel.items()
                },
                "verdict": verdict,
            },
            kind="jts_bass_extension_bench_transfer_analysis",
        )
        # The recorded singular PCM artifacts (the frozen schema's
        # digital_transfer_probe fields are singular, not per-channel) pick
        # the FIRST owner channel deterministically — there is no
        # "worst-case" channel to prefer for a fully-synthetic isolated
        # probe the way there is a MIN peak to prefer in _finish_role; the
        # overall verdict above already requires EVERY channel to pass, so
        # it never collapses to just this one channel's result.
        first_channel = self.target.owner_channels[0]
        # build_transfer_record converts every ArtifactIdentity to its bundle
        # dict shape — build_candidate (and ultimately build_bundle's
        # evidence_fingerprint) expects an already-converted Mapping here,
        # never a raw dataclass instance.
        return build_transfer_record(
            stimulus=stimulus_identity,
            pre_limiter_pcm=per_channel[first_channel]["pre_limiter_pcm"],
            post_limiter_pcm=per_channel[first_channel]["post_limiter_pcm"],
            reference_post_limiter_pcm=per_channel[first_channel][
                "reference_post_limiter_pcm"
            ],
            transfer_analysis=analysis_id,
            verdict=verdict,
        )

    async def run_discovery(
        self,
        *,
        target: TargetPlan,
        active_graph_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> Sequence[DiscoveryCapture]:
        raw = await self.controller.get_active_config_raw()
        captures: list[DiscoveryCapture] = []
        for role in ("sweep_transparency", "sustain_stress"):
            stop.check()
            request = self.requests[role]
            played, padded, padded_identity = await self._prepare_and_play(
                target=target,
                role=role,
                request=request,
                live_active_config_raw=raw,
                sink=sink,
                stop=stop,
                tag=f"discovery-{role}",
            )
            captures.append(
                DiscoveryCapture(
                    stimulus=padded_identity,
                    admission=played.admission,
                    render_inputs={
                        "played": played,
                        "role": role,
                        "active_graph_readback": active_graph_readback,
                        "live_active_config_raw": raw,
                        "padded": padded,
                        "padded_identity": padded_identity,
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
        probes: list[DiscoveryProbe] = []
        for capture in captures:
            stop.check()
            played = cast(PlayedStimulus, capture.render_inputs["played"])
            role = cast(str, capture.render_inputs["role"])
            active_graph_readback = cast(
                ArtifactIdentity, capture.render_inputs["active_graph_readback"]
            )
            raw = cast(str, capture.render_inputs["live_active_config_raw"])
            padded = cast(stimulus.PaddedStimulus, capture.render_inputs["padded"])
            padded_identity = cast(ArtifactIdentity, capture.render_inputs["padded_identity"])

            derived, output_path, playback_channels = self._derive_and_render(
                role_tag=f"discovery-{role}",
                boundary="pre_limiter",
                live_active_config_raw=raw,
                expected_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
                padded=padded,
                sink=sink,
                bounds=self._bounds_for(role),
                fader_db=played.commanded_main_volume_db,
            )
            del derived  # receipt already written

            # S7: each owner channel's EXTRACTED stream is its own distinct
            # artifact — never N identical copies of the whole render.
            peaks_by_channel: dict[int, float] = {}
            pcm_ids: dict[int, ArtifactIdentity] = {}
            for channel in target.owner_channels:
                body = self._extract_body(
                    output_path, channel=channel, channels=playback_channels, padded=padded
                )
                peaks_by_channel[channel] = sample_peak_dbfs(body)
                pcm_ids[channel] = sink.write_bytes(
                    f"{target.target_id}/discovery-{role}-{channel}-pre.raw",
                    body.astype("<f8").tobytes(),
                    kind="jts_bass_extension_bench_pre_limiter_pcm",
                )
            per_channel_peaks = [peaks_by_channel[c] for c in target.owner_channels]
            pre_limiter_peak_dbfs = cross_check.min_across_channels(per_channel_peaks)
            # Keep the recorded singular artifact evidentially consistent
            # with the recorded (min) peak — persist whichever channel
            # achieved it (S7, mirroring _finish_role).
            min_channel = min(peaks_by_channel, key=lambda c: peaks_by_channel[c])
            pcm_id = pcm_ids[min_channel]

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
                    pre_limiter_peak_dbfs=pre_limiter_peak_dbfs,
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
        raw = await self.controller.get_active_config_raw()
        request = self.requests["sweep_transparency"]
        # Disambiguate by the reference activation receipt's own name (which
        # already encodes the candidate setting, e.g.
        # "reference_activation_-3.5") — run_reference_sweep runs once per
        # candidate, so a fixed tag would let a later candidate's reference
        # stimulus silently overwrite an earlier candidate's bundle path.
        readback_tag = Path(reference_readback.relative_path).stem
        played, _padded, padded_identity = await self._prepare_and_play(
            target=target,
            role="sweep_transparency",
            request=request,
            live_active_config_raw=raw,
            sink=sink,
            stop=stop,
            tag=f"reference-sweep-{readback_tag}",
        )
        return ReferenceSweepCapture(
            reference_stimulus=padded_identity,
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
        raw = await self.controller.get_active_config_raw()
        transfer = await self._digital_transfer_probe(
            candidate_setting_dbfs=candidate_setting_dbfs,
            live_active_config_raw=raw,
            sink=sink,
        )
        stop.check()
        sweep_request = self.requests["sweep_transparency"]
        sustain_request = self.requests["sustain_stress"]
        sweep_played, sweep_padded, sweep_padded_identity = await self._prepare_and_play(
            target=target,
            role="sweep_transparency",
            request=sweep_request,
            live_active_config_raw=raw,
            sink=sink,
            stop=stop,
            tag=f"candidate-{candidate_setting_dbfs:g}-sweep_transparency",
            reference=reference,
        )
        sustain_played, sustain_padded, sustain_padded_identity = await self._prepare_and_play(
            target=target,
            role="sustain_stress",
            request=sustain_request,
            live_active_config_raw=raw,
            sink=sink,
            stop=stop,
            tag=f"candidate-{candidate_setting_dbfs:g}-sustain_stress",
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
            sweep_render_inputs={
                "played": sweep_played,
                "setting": candidate_setting_dbfs,
                "live_active_config_raw": raw,
                "padded": sweep_padded,
                "padded_identity": sweep_padded_identity,
            },
            sustain_live={},
            sustain_render_inputs={
                "played": sustain_played,
                "setting": candidate_setting_dbfs,
                "live_active_config_raw": raw,
                "padded": sustain_padded,
                "padded_identity": sustain_padded_identity,
            },
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
        setting_tag = f"{candidate_setting_dbfs:g}"
        sweep_played = cast(PlayedStimulus, capture.sweep_render_inputs["played"])
        sustain_played = cast(PlayedStimulus, capture.sustain_render_inputs["played"])
        sweep_raw = cast(str, capture.sweep_render_inputs["live_active_config_raw"])
        sustain_raw = cast(str, capture.sustain_render_inputs["live_active_config_raw"])
        sweep_padded = cast(stimulus.PaddedStimulus, capture.sweep_render_inputs["padded"])
        sustain_padded = cast(
            stimulus.PaddedStimulus, capture.sustain_render_inputs["padded"]
        )
        sweep_padded_identity = cast(
            ArtifactIdentity, capture.sweep_render_inputs["padded_identity"]
        )
        sustain_padded_identity = cast(
            ArtifactIdentity, capture.sustain_render_inputs["padded_identity"]
        )
        sweep_core = await self._finish_role(
            sweep_played,
            role="sweep_transparency",
            live_active_config_raw=sweep_raw,
            padded=sweep_padded,
            padded_identity=sweep_padded_identity,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            setting_tag=setting_tag,
            sink=sink,
        )
        stop.check()
        sustain_core = await self._finish_role(
            sustain_played,
            role="sustain_stress",
            live_active_config_raw=sustain_raw,
            padded=sustain_padded,
            padded_identity=sustain_padded_identity,
            expected_clip_limit_dbfs=candidate_setting_dbfs,
            setting_tag=setting_tag,
            sink=sink,
        )
        return CandidateMeasurements(
            digital_transfer_probe=capture.digital_transfer_probe,
            # sweep_core stays RAW: the runner merges it with the
            # reference-activation fields it owns via its own
            # bundle.build_sweep_record(**measured.sweep_core, ...) call.
            # sustain_stress has no such runner-side merge step — it goes
            # straight into bundle.build_candidate(sustain_stress=...), so
            # THIS is the only place it can be converted to the bundle
            # dict shape (mirrors digital_transfer_probe above).
            sweep_core=sweep_core,
            sustain_stress=build_sustain_record(**sustain_core),
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
    "estimate_campaign_render_count",
]
